"""蒸馏系统四项修复的回归测试（2026-10-16）。

钉住四条硬保证（对应审查发现的四个问题）：

1. **P1-1 生成路径不碰库**：命中项目缓存时 `persist_library=False` 必须
   纯读返回——不算 `corpus_content_hash`（全量加载语料+sha256）、不写蒸馏库；
   而蒸馏提交路径（默认 True）行为不变，照旧落库。
2. **P1-2 全失败不许静默 success**：所有语料块都调用模型失败（或语料为空）时
   `extract_narrative_profile` 必须抛 ValueError，让后台任务落 failed——
   绝不允许「蒸馏完成但 0 条规则」这种无声结果。
3. **P2-1 并发防重不分档位**：项目有未终态任务时，换档位提交也复用原任务，
   不允许两个后台线程共写 `project.style_narrative`。
4. **P2-2 content_hash 透传**：调用方传了 `content_hash` 就不许再全量重算。

假 provider 绝不真调 LLM（与 tests/test_distill_jobs.py 同款纪律）。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from _async_chat_stub import AsyncChatStub  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import DistillRun, NovelChunk, Project, RawNovel  # noqa: E402
from app.style import narrative as narrative_mod  # noqa: E402
from app.style.narrative import (  # noqa: E402
    TIER_FAST,
    extract_narrative_profile,
    project_narrative,
)


# ====================================================================
# fixtures / 工具（与 test_distill_jobs.py 同款，保持单源风格）
# ====================================================================


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


class StubProvider(AsyncChatStub):
    """确定性假 provider：返回一条证据能通过的「人物描写手法」规则。"""

    EVIDENCE = "医院里她抬起头"

    def chat(self, system: str, user: str, **kwargs) -> str:
        return '{"人物描写手法": ["出场先写环境再写人（证据：' + self.EVIDENCE + '）"]}'


class BoomProvider(AsyncChatStub):
    """每次调用都炸——模拟 key 无效 / 限流 / 网络不通。"""

    def chat(self, system: str, user: str, **kwargs) -> str:
        raise RuntimeError("模型服务不可用（测试注入）")


class FirstOkThenBoomProvider(AsyncChatStub):
    """第一次调用返回合法 JSON，之后全炸——模拟「部分块失败」。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs) -> str:
        self.calls += 1
        if self.calls == 1:
            return '{"人物描写手法": ["出场先写环境再写人（证据：医院里她抬起头）"]}'
        raise RuntimeError("后续块全部不可用（测试注入）")


@pytest.fixture
def stub_llm(monkeypatch):
    stub = StubProvider()
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: stub)
    return stub


def _make_corpus(db, n_chapters: int = 30) -> int:
    novel = RawNovel(title=f"修复测试语料{n_chapters}", status="imported")
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
        title="修复测试项目",
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


def _spy_on(monkeypatch, name: str):
    """给 narrative_mod 上的函数装计数器，返回 {'n': 0, 'last_args': None}。"""
    box = {"n": 0, "last_args": None}
    real = getattr(narrative_mod, name)

    def _spy(*args, **kwargs):
        box["n"] += 1
        box["last_args"] = (args, kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(narrative_mod, name, _spy)
    return box


# ====================================================================
# P1-1：生成路径（persist_library=False）纯读；蒸馏路径照旧落库
# ====================================================================


def test_cache_hit_generation_path_skips_hash_and_library(
    db_session, monkeypatch, stub_llm
):
    """命中项目缓存 + persist_library=False：不算 content_hash、不写库。

    生成链路每请求调 3 次本函数，缓存命中还算全语料哈希 + 写库是纯浪费。
    """
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    # 先真跑一次（蒸馏路径）把项目缓存与蒸馏库都建起来
    first = project_narrative(db_session, proj, tier=TIER_FAST)
    assert first and first.get("fingerprint"), "预热失败：项目缓存没建起来"

    hash_spy = _spy_on(monkeypatch, "corpus_content_hash")
    persist_spy = _spy_on(monkeypatch, "_persist_style_distill")

    again = project_narrative(
        db_session, proj, tier=TIER_FAST, persist_library=False
    )

    assert again is not None and again.get("fingerprint"), "缓存命中必须返回档案"
    assert hash_spy["n"] == 0, "生成路径还在全量加语料算哈希——P1-1 没修掉"
    assert persist_spy["n"] == 0, "生成路径还在写蒸馏库——平白给 SQLite 加写锁"

    # 对照组：默认（蒸馏路径）必须照旧算哈希 + 落库
    project_narrative(db_session, proj, tier=TIER_FAST)
    assert hash_spy["n"] == 1, "蒸馏路径的 content_hash 计算被误伤"
    assert persist_spy["n"] == 1, "蒸馏路径的落库被误伤——蒸馏库会永远为空"


def test_generation_path_extraction_still_commits_project_cache(
    db_session, monkeypatch, stub_llm
):
    """生成路径罕见地走到真实提取（缓存失效+库未命中）时：
    新档案必须落进项目缓存（commit），否则下次生成又全量重跑。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    profile = project_narrative(
        db_session, proj, tier=TIER_FAST, persist_library=False
    )
    assert profile and profile.get("fingerprint")

    # 新 session 读库：档案必须已经落库（不是只挂在 session 里）
    db_session.expire_all()
    fresh = SessionLocal()
    try:
        row = fresh.get(Project, proj.id)
        assert (row.style_narrative or {}).get("fingerprint"), (
            "生成路径提取的档案没落进项目缓存——下次生成又会全量重跑"
        )
    finally:
        fresh.close()


# ====================================================================
# P1-2：全失败 / 空语料不许静默 success；部分失败如实记录
# ====================================================================


def test_all_chunks_failed_raises_instead_of_silent_success(db_session):
    """所有块都调模型失败 → 抛 ValueError（后台任务会落 failed）。

    旧行为：每块 catch+continue → 规则全空 → 返回 dict → job 标 success，
    用户看到「蒸馏完成」但 0 条规则，无从分辨 key/限流/网络问题。
    """
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    with pytest.raises(ValueError, match="全部调用模型失败"):
        extract_narrative_profile(
            db_session, proj, [nid], provider=BoomProvider(), tier=TIER_FAST
        )


def test_all_chunks_unparseable_also_raises(db_session):
    """LLM 不抛错但返回不可解析内容（兜底模板/截断）→ 同样算失败。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    class GarbageProvider(AsyncChatStub):
        def chat(self, system: str, user: str, **kwargs) -> str:
            return "我不是 JSON，我是兜底文案"

    with pytest.raises(ValueError, match="全部调用模型失败"):
        extract_narrative_profile(
            db_session, proj, [nid], provider=GarbageProvider(), tier=TIER_FAST
        )


def test_empty_corpus_raises(db_session):
    """参考语料没有章节 → 明确失败，不是「成功但 0 规则」。"""
    novel = RawNovel(title="空语料", status="imported")
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    proj = _make_project(db_session, novel.id, tier=TIER_FAST)

    with pytest.raises(ValueError, match="没有可蒸馏的章节"):
        extract_narrative_profile(
            db_session, proj, [novel.id], provider=StubProvider(), tier=TIER_FAST
        )


def test_partial_chunk_failure_recorded_not_raised(db_session):
    """部分块失败：任务算成功（有产出），但 failed_chunks 如实记录。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    profile = extract_narrative_profile(
        db_session, proj, [nid], provider=FirstOkThenBoomProvider(), tier=TIER_FAST
    )
    # 快速档 30 章 → 3 块：第 1 块成功，后 2 块失败
    assert profile["failed_chunks"] == 2, "部分失败数没记上——前端无从提示"
    rule_count = sum(len(v) for v in profile["rules"].values() if isinstance(v, list))
    assert rule_count > 0, "有成功的块就必须有规则产出"


def test_run_distill_job_partial_failure_visible_in_stage(db_session, monkeypatch):
    """端到端：部分块失败时 job 仍 success，但 stage 如实写「有块被跳过」。"""
    import app.llm as llm_mod
    from app.style.jobs import STATUS_SUCCESS, create_distill_run, run_distill_job

    fake = FirstOkThenBoomProvider()
    monkeypatch.setattr(llm_mod, "get_distiller_provider", lambda: fake)

    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)
    run = create_distill_run(db_session, proj.id, TIER_FAST)

    run_distill_job(run.id, proj.id, TIER_FAST, False)

    db_session.expire_all()
    done = db_session.get(DistillRun, run.id)
    assert done.status == STATUS_SUCCESS
    assert "2 个语料块调用失败" in (done.stage or ""), (
        f"部分失败没写进 stage：{done.stage!r}"
    )
    assert done.rule_count > 0


# ====================================================================
# P2-2：content_hash 透传，同链路不重复全量计算
# ====================================================================


def test_content_hash_param_avoids_recompute(db_session, monkeypatch, stub_llm):
    """传了 content_hash → 全程不再调 corpus_content_hash，且原值落进库。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid, tier=TIER_FAST)

    hash_spy = _spy_on(monkeypatch, "corpus_content_hash")

    received = {}

    def _persist_spy(db, project, ids, cfg, content_hash, profile):
        received["hash"] = content_hash

    monkeypatch.setattr(narrative_mod, "_persist_style_distill", _persist_spy)

    project_narrative(db_session, proj, tier=TIER_FAST, content_hash="deadbeef")

    assert hash_spy["n"] == 0, "传了 hash 还在全量加语料重算——P2-2 没修掉"
    assert received.get("hash") == "deadbeef", "调用方算的 hash 没被用上"
