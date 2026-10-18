"""Writer 重写环节「原文对照」单测（Phase 0 原型，仅完整档开启）。

背景：现在 `_rewrite_prompt` 的 user 只有「AI 腔问题清单 + 待改正文」，
没有任何原文对照 —— 等于让人改文章却不给范文。系统里其实已有现成原料：
哨兵历史告警（自带更早段的反证原文片段）与参考语料检索。

本文件 4 项（先红后绿）：
  T1 完整档 + 库里有历史 findings → 重写 prompt 的 user **包含** findings 的
     evidence 原文片段（修复前不含 → 红）
  T2 快速档 → 不触发检索、不带证据块，行为与现在一致（防过度）
  T3 检索抛错（monkeypatch hybrid_search）→ 重写照常完成、不抛异常（熔断生效）
  T4 无 findings 且无需检索 → prompt 回退到现有形状（向后兼容）

测试真实调用 generate_next_segment / _rewrite_prompt，不 mock 被测逻辑本身。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.llm as llm_mod  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    generate_next_segment,
    generate_next_segment_stream,
)
from app.models import Fact, NovelChunk, Project, RawNovel  # noqa: E402
from app.style.narrative import TIER_FAST, TIER_FULL, project_narrative  # noqa: E402


# ---------------------------------------------------------------------------
# 语料 / 项目 / provider
# ---------------------------------------------------------------------------

_AI_TEXT = (
    "就在这时他不禁深吸一口气，心中充满了怒火，他知道，要出事了。"
    "他明白，已经晚了。突然之间灯灭了。她明白，门被推开。仿佛有人在笑。"
)
_REWRITTEN = (
    "灯灭了。他站在原地没动，手指慢慢收拢，喉咙里那口气直到此刻才吐出来。"
    "走廊尽头传来脚步声，一下，又一下。"
)
# 哨兵告警的 evidence 原文片段（T1 断言它必须出现在重写 prompt 里）
_EVIDENCE_SNIPPET = "陆见微倒在雪地里，胸口那道伤口已经不再流血，手指僵在半空。"


class DistillStubProvider:
    """蒸馏用的假 provider：返回带维度键的合法 JSON。

    与 Writer 分开是必须的——蒸馏要 JSON、生成要正文，共用一个 stub 会让
    蒸馏把正文当 JSON 解析而失败（实测踩过）。
    """

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        return '{"人物描写手法": ["出场先写环境再写人（证据：医院里她抬起头）"]}'

    def summarize(self, text: str) -> str:
        return "蒸馏摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        yield "蒸馏正文"


class SelfcheckStubProvider:
    """Writer 用的假 provider：第 1 次 chat 返回带 AI 腔的正文（触发重写），
    第 2 次起返回改写后的正文；记录每次 chat 收到的 (system, user)。

    为什么要记 user：本文件断言的是「重写 prompt 里有没有原文对照」，
    必须能看到真正送进模型的那串文本，而不是另造一份去比对。
    """

    def __init__(self) -> None:
        self.chat_calls = 0
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.chat_calls += 1
        self.prompts.append((system, user))
        # 第 1 次 = 生成正文（要带 AI 腔才会触发重写）；第 2 次起 = 重写
        return _AI_TEXT if self.chat_calls == 1 else _REWRITTEN

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        yield _AI_TEXT


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_corpus(db, n_chapters: int = 3) -> int:
    novel = RawNovel(title=f"自检测试语料{n_chapters}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id, chapter_no=i + 1, idx=i,
                text=f"第{i + 1}章正文：她抬起头看了他一眼，没有说话，只是把手缩回袖子里。",
            )
        )
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, tier: str) -> Project:
    proj = Project(
        title="自检测试项目", target_length=5000, mode="quick", status="created",
        reference_novel_ids=[novel_id], narrative_tier=tier,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _stub_distill_chain(monkeypatch, stub):
    """蒸馏链路换成 JSON stub、Writer 链路换成正文 stub（两者返回物不同，必须分开）。"""
    monkeypatch.setattr(llm_mod, "get_distiller_provider", lambda: DistillStubProvider())
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: stub)


def _add_finding(db, project_id: int, segment_no: int = 3,
                 entity: str = "陆见微", severity: str = "err") -> None:
    """写一条哨兵历史告警（形同 sentinel 落库的 alert Fact）。"""
    db.add(
        Fact(
            project_id=project_id,
            type="alert",
            key=f"dead_speaks:{entity}:{segment_no}:0",
            props={
                "entity": entity,
                "message": f"「{entity}」已记于第 {segment_no} 段死亡，本段却有台词。",
                "severity": severity,
                "spans": [],
                "evidence": [
                    {
                        "segment_no": segment_no,
                        "span": [0, 10],
                        "text": _EVIDENCE_SNIPPET,
                        "kind": "death",
                    }
                ],
            },
            aliases=[],
            source="generated",
            confidence="high",
            first_segment=segment_no,
            last_segment=segment_no,
        )
    )
    db.commit()


def _rewrite_user_of(stub: SelfcheckStubProvider) -> str:
    """取重写那次 chat 的 user。

    按 system 内容识别（重写 system 含「你是一位资深中文网文编辑」），
    **不按调用序号猜**——流式路径的正文走 stream_chat，重写的 chat 是第 1 次
    而不是第 2 次（实测踩过：按序号取会取错并误判为「重写未触发」）。
    """
    for system, user in stub.prompts:
        if "你是一位资深中文网文编辑" in system:
            return user
    raise AssertionError(
        f"未找到重写 prompt，共 {len(stub.prompts)} 次 chat："
        f"{[s[:40] for s, _ in stub.prompts]}"
    )


# ---------------------------------------------------------------------------
# T1：完整档 + 有 findings → 重写 prompt 必须带 findings 的原文片段
# ---------------------------------------------------------------------------


def test_full_tier_rewrite_prompt_includes_finding_evidence(db_session, monkeypatch):
    """完整档（narrative_tier=full）+ 库里有历史告警 → 重写 prompt 的 user
    必须包含告警 evidence 里的原文片段。

    修复前：_rewrite_prompt 的 user 只有「问题清单 + 待改正文」，
    没有任何原文对照 → 红。
    """
    stub = SelfcheckStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FULL)
    project_narrative(db_session, proj, tier=TIER_FULL)  # 建档案缓存，放行 preflight
    _add_finding(db_session, proj.id)

    result = generate_next_segment(db_session, proj, provider=stub)

    assert result.get("rewrite_info"), "重写未发生（AI 腔正文没触发重写），测试前提不成立"
    user = _rewrite_user_of(stub)
    assert _EVIDENCE_SNIPPET in user, (
        f"重写 prompt 缺少 findings 的 evidence 原文片段（缺陷：盲重写）\n"
        f"实际 user 前 400 字：{user[:400]}"
    )
    # 来源标注也要在，否则模型不知道这段是"前文事实"还是"参考语料"
    assert "前文第" in user, "证据条目缺来源标注（应标明「前文第 N 段」）"
    assert "不要照抄" in user, "缺「不要照抄内容」的指令"


# ---------------------------------------------------------------------------
# T2：快速档 → 零新增调用、无证据块（防过度）
# ---------------------------------------------------------------------------


def test_fast_tier_does_not_gather_evidence(db_session, monkeypatch):
    """快速档（balanced/fast）不得触发检索、不得带证据块 —— 行为与修复前一致。

    防过度：Phase 0 只对完整档开，其余档位必须零新增开销。
    """
    import app.retrieval.search as search_mod

    calls = {"n": 0}

    def _counting_hybrid(db, novel_id, query, k=10):
        calls["n"] += 1
        raise AssertionError("快速档不应触发检索")

    monkeypatch.setattr(search_mod, "hybrid_search", _counting_hybrid)

    stub = SelfcheckStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)
    project_narrative(db_session, proj, tier=TIER_FAST)
    _add_finding(db_session, proj.id)  # 即使库里有 findings 也不该被读进 prompt

    result = generate_next_segment(db_session, proj, provider=stub)

    assert calls["n"] == 0, f"快速档触发了 {calls['n']} 次检索 —— 违反零新增开销"
    assert result.get("rewrite_info"), "重写未发生，测试前提不成立"
    user = _rewrite_user_of(stub)
    assert "【原文对照】" not in user, "快速档不应带【原文对照】块"
    assert _EVIDENCE_SNIPPET not in user, "快速档不应读 findings 进 prompt"
    # 既有字段语义不变
    assert "reasons" in result["rewrite_info"]


# ---------------------------------------------------------------------------
# T3：检索抛错 → 熔断，重写照常完成
# ---------------------------------------------------------------------------


def test_retrieval_failure_is_tripped_not_fatal(db_session, monkeypatch):
    """hybrid_search 抛错时重写必须照常完成、不抛异常，并在 selfcheck 里记熔断。

    修复前：根本没有这一层，selfcheck 统计不存在 → 红。
    """
    import app.retrieval.search as search_mod

    def _boom(db, novel_id, query, k=10):
        raise RuntimeError("检索后端炸了")

    monkeypatch.setattr(search_mod, "hybrid_search", _boom)

    stub = SelfcheckStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FULL)
    project_narrative(db_session, proj, tier=TIER_FULL)
    _add_finding(db_session, proj.id)

    # 不能抛异常
    result = generate_next_segment(db_session, proj, provider=stub)

    info = result.get("rewrite_info") or {}
    assert info, "检索熔断后重写仍应完成"
    selfcheck = info.get("selfcheck")
    assert selfcheck is not None, "rewrite_info 缺 selfcheck 统计（缺陷：不可观测）"
    assert selfcheck.get("retrieval_tripped") is True, (
        f"检索失败应记 retrieval_tripped=True，实际 {selfcheck}"
    )
    # findings 是零成本本地读取，检索炸了也不该受影响
    assert selfcheck.get("findings_used", 0) >= 1, (
        f"检索熔断不应影响 findings 收集，实际 {selfcheck}"
    )
    assert _EVIDENCE_SNIPPET in _rewrite_user_of(stub), "熔断后 findings 证据仍应在 prompt 里"


# ---------------------------------------------------------------------------
# T4：无 findings 且无需检索 → prompt 回退现有形状（向后兼容）
# ---------------------------------------------------------------------------


def test_no_evidence_falls_back_to_legacy_prompt(db_session, monkeypatch):
    """完整档但库里无 findings、且检索无命中 → user 回退到修复前的形状。

    向后兼容：证据为空时逐字节不变，不引入空的【原文对照】块。
    （检索无命中用 monkeypatch 返回 [] 模拟——真实场景里语料没匹配片段、
     或项目压根没配参考语料时就是这条路径。）
    """
    import app.retrieval.search as search_mod

    monkeypatch.setattr(search_mod, "hybrid_search", lambda *a, **kw: [])

    stub = SelfcheckStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FULL)
    project_narrative(db_session, proj, tier=TIER_FULL)
    # 不写任何 findings

    result = generate_next_segment(db_session, proj, provider=stub)

    assert result.get("rewrite_info"), "重写未发生，测试前提不成立"
    user = _rewrite_user_of(stub)
    assert "【原文对照】" not in user, (
        f"无证据时不应出现【原文对照】块，实际 user 前 300 字：{user[:300]}"
    )
    # 回退形状 = 问题清单 + 待改正文
    assert "以下文本存在以下 AI 腔问题" in user
    assert "请改写" in user


# ---------------------------------------------------------------------------
# T5：两个入口都要接 —— 流式路径不能漏
# ---------------------------------------------------------------------------


def test_stream_entry_also_gets_evidence(db_session, monkeypatch):
    """派单要求「两个入口都要走新逻辑」：流式续写的重写同样必须带原文对照。

    只改 generate_next_segment 会让「流式与非流式重写结果不同」——这种不一致
    在生产上极难排查（用户点一次看到改了、点另一次没改）。
    """
    stub = SelfcheckStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FULL)
    project_narrative(db_session, proj, tier=TIER_FULL)
    _add_finding(db_session, proj.id)

    events = list(generate_next_segment_stream(db_session, proj, provider=stub))
    done = [e for e in events if e.get("type") == "done"]
    assert done, "流式未产出 done 事件"
    info = done[0].get("rewrite_info") or {}
    assert info.get("selfcheck"), f"流式路径缺 selfcheck 统计，实际 {info}"
    assert _EVIDENCE_SNIPPET in _rewrite_user_of(stub), (
        "流式路径的重写 prompt 缺原文对照（只改了非流式入口）"
    )
