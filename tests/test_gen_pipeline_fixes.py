"""live 验证发现的两个新问题的复现测试（2026-10-17）。

钉住两条硬保证：

1. **merge_graph_from_text 必须双端同步**：KG 修复时 engine._merge_segment_into_graph
   做了 KgNode/Fact 双端同步，但 build.py 的公开入口 merge_graph_from_text 单独
   调用只同步 KgNode—— Fact 账本漏更新（live 验证 S2 实测复现）。
   任何调用方用它，账本都会与图谱漂移。
2. **无缓存档案时生成必须明确拒绝**：项目有参考语料但叙事档案既不在项目缓存
   也不在蒸馏库时，generate_next_segment* 会**同步**触发全量蒸馏（分钟级），
   HTTP 240s 必超时（live 验证实测复现）。必须在调用 LLM 之前明确 raise，
   提示先去蒸馏。有缓存（项目级或蒸馏库命中）时必须放行——防过度拦截。

另有一个防回归对照：profile 已缓存时生成正常进行（不能被误拦）。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm as llm_mod  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import generate_next_segment  # noqa: E402
from app.kg.build import merge_graph_from_text  # noqa: E402
from app.models import Fact, KgNode, NovelChunk, Project, RawNovel  # noqa: E402
from app.style.narrative import (  # noqa: E402
    TIER_FAST,
    project_narrative,
)


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


class GenStubProvider:
    """蒸馏/生成两用的确定性假 provider（不真调 LLM）。

    chat 对 distill prompt 返回带维度键的合法 JSON（证据句在语料里），
    对生成 prompt 同样返回它（当作正文即可）；summarize/stream_chat 兜底。
    """

    EVIDENCE = "医院里她抬起头"

    def __init__(self) -> None:
        self.chat_calls = 0

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.chat_calls += 1
        return '{"人物描写手法": ["出场先写环境再写人（证据：' + self.EVIDENCE + '）"]}'

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        yield "测试正文"


def _make_corpus(db, n_chapters: int = 30) -> int:
    novel = RawNovel(title=f"管线修复测试语料{n_chapters}", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text="医院里她抬起头，看着走廊尽头的光。" + "他说：走吧。" * (i % 3 + 1),
            )
        )
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, tier: str | None = None) -> Project:
    proj = Project(
        title="管线修复测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        reference_novel_ids=[novel_id],
        narrative_tier=tier,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _stub_distill_chain(monkeypatch, stub):
    """蒸馏链路（distiller 优先、writer 回落）全部换成 stub。"""
    monkeypatch.setattr(llm_mod, "get_distiller_provider", lambda: stub)
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: stub)


# ====================================================================
# 问题 1：merge_graph_from_text 必须双端同步（KgNode + Fact）
# ====================================================================


def test_merge_graph_from_text_syncs_fact_ledger_too(db_session):
    """公开入口合并一段文本后，Fact 账本的坑状态必须与 KgNode 一致。

    修复前：merge_graph_from_text 只调 merge_result_into_graph（纯图谱），
    KgNode 结算了、Fact 账本还是 open——账本与图谱漂移（live S2 实测复现）。
    """
    proj = Project(
        title="双端同步测试", target_length=5000, mode="quick", status="created",
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)

    # 段 1：开坑（含 cue=秘密 的悬念句）
    merge_graph_from_text(
        db_session, proj.id,
        "林夜始终不明白，父亲守着的秘密是否与他有关？",
        segment_no=1,
    )
    node = db_session.scalar(
        __import__("sqlalchemy").select(KgNode).where(
            KgNode.project_id == proj.id, KgNode.type == "plothole",
            KgNode.source == "generated",
        )
    )
    assert node is not None, "段 1 没开出坑——语料形状不对，测试前提不成立"
    assert (node.props or {}).get("status") == "open"

    # 段 2：含 cue + 强解决短语 → 结算
    merge_graph_from_text(
        db_session, proj.id,
        "揭晓的答案并不复杂：那秘密是母亲留下的嫁妆单子。",
        segment_no=2,
    )
    db_session.expire_all()
    node = db_session.get(KgNode, node.id)
    assert (node.props or {}).get("status") == "resolved", "KgNode 侧应已结算"

    # 命门断言：Fact 账本同坑也必须 resolved
    fact = db_session.scalar(
        __import__("sqlalchemy").select(Fact).where(
            Fact.project_id == proj.id, Fact.type == "plothole",
            Fact.source == "generated",
        )
    )
    assert fact is not None, "Fact 账本里没有这个坑——簿记没跑"
    assert (fact.props or {}).get("status") == "resolved", (
        f"Fact 账本未随 KgNode 同步结算（实际 {fact.props}!r）——"
        "merge_graph_from_text 这个公开入口会漏账本"
    )


# ====================================================================
# 问题 2：无缓存档案时生成必须明确拒绝（而不是同步跑分钟级蒸馏）
# ====================================================================


def test_generate_requires_distill_first(db_session, monkeypatch):
    """有语料、无档案、库未命中 → 必须在调 LLM 之前 raise，提示先蒸馏。

    修复前：直接触发 extract_narrative_profile（分钟级），HTTP 240s 必超时，
    用户看到的是无限转圈（live 验证实测复现）。
    """
    stub = GenStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)  # narrative_tier=None，无缓存

    with pytest.raises(ValueError, match="蒸馏"):
        generate_next_segment(db_session, proj, provider=stub)

    assert stub.chat_calls == 0, (
        f"preflight 之后还调了 {stub.chat_calls} 次 LLM——拦截点不在 LLM 之前"
    )


def test_generate_proceeds_when_profile_cached(db_session, monkeypatch):
    """防过度拦截：项目缓存命中蒸馏档案时必须放行，正常生成。"""
    stub = GenStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    # 先蒸馏建缓存（fast 档，stub provider）
    profile = project_narrative(db_session, proj, tier=TIER_FAST)
    assert profile and profile.get("fingerprint")

    calls_before = stub.chat_calls
    result = generate_next_segment(db_session, proj, provider=stub)
    assert result.get("text"), "缓存命中却被拦截或没生成——过度拦截"
    assert stub.chat_calls > calls_before, "缓存命中却没调生成 LLM——没真生成"


def test_generate_proceeds_without_reference_corpus(db_session, monkeypatch):
    """防过度拦截：无参考语料的项目（自由生成）不受 preflight 影响。"""
    stub = GenStubProvider()
    _stub_distill_chain(monkeypatch, stub)
    proj = Project(
        title="自由生成项目", target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)

    result = generate_next_segment(db_session, proj, provider=stub)
    assert result.get("text"), "无语料项目的自由生成被误拦"
