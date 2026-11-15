"""蒸馏断点续跑与 force 清理的互斥关系（回归测试）。

背景：2026-11-14 项目 5 重蒸后要补 19 个失败块，调用
`project_narrative(force=True, resume=True)`，结果从第 1 块重新跑——因为
force 分支先 `_clear_scope_chunks(本 scope)`，把断点清空后 resume 才去读。
清理只应作用于「非 resume 的整轮重跑」；resume=True 必须先保留断点。

覆盖：
1. force=True + resume=True：断点一块都不能少，且 extract 确实收到 resume=True。
2. force=True + resume=False：本 scope 断点被清空（保持「整轮重算」语义）。
3. resume=True + 已有断点：已完成块不建 prompt、不调 LLM，只跑剩余块，
   缓存里的规则与 chunk_meta 原样回灌。

跑法：python tests/test_distill_resume_force.py  或 pytest tests/test_distill_resume_force.py
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.models import DistillChunk, NovelChunk, Project  # noqa: E402
from app.llm.provider import LLMProvider  # noqa: E402
from app.style import narrative as N  # noqa: E402


def _db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _scope(ids: list[int], max_chapters: int) -> str:
    return N.distill_resume_scope(ids, N.resolve_tier("full"), max_chapters)


def _seed_chunks(db, project_id: int, scope: str, n: int = 2) -> None:
    for i in range(n):
        N._save_done_chunk(
            db, project_id, scope, 5, f"key-{i}", f"{i * 5 + 1}-{i * 5 + 5}",
            {"rules": {}, "forbidden": {}, "rejected": 0},
            provider="test", endpoint="test://",
        )
    db.commit()


def _count_chunks(db, project_id: int, scope: str) -> int:
    return len(
        db.scalars(
            select(DistillChunk).where(
                DistillChunk.project_id == project_id,
                DistillChunk.scope == scope,
            )
        ).all()
    )


def test_force_resume_keeps_breakpoints():
    """force=True + resume=True：断点必须保留，否则续跑永远从零开始。"""
    db = _db()
    project = Project(id=5, title="续跑测试", reference_novel_ids=[5])
    db.add(project)
    db.commit()
    scope = _scope([5], 99999)
    _seed_chunks(db, 5, scope, 2)

    seen: dict = {}

    def fake_extract(db_, project_, novel_ids, **kw):
        seen["resume"] = kw.get("resume")
        seen["chunks_at_entry"] = _count_chunks(db_, project_.id, scope)
        return {
            "version": 1, "source_ids": list(novel_ids), "narrative_version": 7,
            "tier": "full", "n_chapters": 10, "rules": {}, "rejected": 0,
        }

    orig = N.extract_narrative_profile
    N.extract_narrative_profile = fake_extract
    try:
        N.project_narrative(
            db, project, force=True, resume=True, tier="full",
            max_chapters=99999, persist_library=True,
        )
    finally:
        N.extract_narrative_profile = orig

    assert seen["resume"] is True
    assert seen["chunks_at_entry"] == 2, "resume=True 时 force 不得清空本 scope 断点"
    assert _count_chunks(db, 5, scope) == 2


def test_force_without_resume_clears_breakpoints():
    """非 resume 的整轮重算仍要清断点（不能拼上一次的半成品）。"""
    db = _db()
    project = Project(id=5, title="整轮重算", reference_novel_ids=[5])
    db.add(project)
    db.commit()
    scope = _scope([5], 99999)
    _seed_chunks(db, 5, scope, 2)

    seen: dict = {}

    def fake_extract(db_, project_, novel_ids, **kw):
        seen["chunks_at_entry"] = _count_chunks(db_, project_.id, scope)
        return {
            "version": 1, "source_ids": list(novel_ids), "narrative_version": 7,
            "tier": "full", "n_chapters": 10, "rules": {}, "rejected": 0,
        }

    orig = N.extract_narrative_profile
    N.extract_narrative_profile = fake_extract
    try:
        N.project_narrative(
            db, project, force=True, resume=False, tier="full",
            max_chapters=99999, persist_library=True,
        )
    finally:
        N.extract_narrative_profile = orig

    assert seen["chunks_at_entry"] == 0
    assert _count_chunks(db, 5, scope) == 0


class _FakeProvider(LLMProvider):
    """记录每次 chat 的 prompt；块级抽取统一回空 JSON（不产规则）。"""

    label = "fake"
    base_url = "fake://"

    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.prompts.append((system, user))
        return "{}"


def test_resume_skips_done_chunks_and_only_runs_rest():
    """resume=True + 已有断点：已完成块零调用，剩余块照常提炼。"""
    db = _db()
    project = Project(id=5, title="跳过已完成块", reference_novel_ids=[5])
    db.add(project)
    db.commit()

    # 10 章 → 固定 5 章/块 → 两块：1-5、6-10。短篇（<30 万字）采样全取、保持原序。
    texts = []
    for ch in range(1, 11):
        marker = "上游标记A" if ch <= 5 else "下游标记B"
        text = f"第{ch}章 {marker} 他看到门开着，没说话，只把伞收起来靠在墙边。"
        texts.append(text)
        db.add(NovelChunk(novel_id=5, chapter_no=ch, idx=0, text=text))
    db.commit()

    chunk_a_text = "\n\n".join(texts[:5])
    key_a = N._chunk_key(5, "1-5", chunk_a_text)
    scope = N.distill_resume_scope([5], N.resolve_tier("full"), 99999)
    N._save_done_chunk(
        db, 5, scope, 5, key_a, "1-5",
        {
            "rules": {"人物描写手法": [{
                "rule": "缓存规则甲：用收伞代替回答（证据：他把伞收起来靠在墙边，一句话也没说）",
                "novel_id": 5, "chapter_no": 1,
            }]},
            "forbidden": {},
            "rejected": 2,
            "meta": {"chapter_range": "1-5", "summary": "缓存摘要", "primary_scene": "daily",
                     "scene_labels": ["daily"]},
        },
        provider="test", endpoint="test://",
    )
    db.commit()

    prov = _FakeProvider()
    orig_graphs, orig_voice = N._ensure_reference_graphs, N._extract_character_voices
    N._ensure_reference_graphs = lambda *a, **kw: None
    N._extract_character_voices = lambda *a, **kw: []
    try:
        profile = N.extract_narrative_profile(
            db, project, [5], provider=prov, max_chapters=99999, tier="full",
            resume=True, max_concurrent=2,
        )
    finally:
        N._ensure_reference_graphs, N._extract_character_voices = orig_graphs, orig_voice

    assert prov.prompts, "剩余块应当至少发起一次 LLM 调用"
    assert all("上游标记A" not in user for _, user in prov.prompts), \
        "已完成块不得再建 prompt"
    assert any("下游标记B" in user for _, user in prov.prompts), \
        "剩余块必须照常提炼"

    rules = profile.get("rules") or {}
    texts_out = [str(r.get("rule") or "") for r in rules.get("人物描写手法") or []]
    assert any("缓存规则甲" in t for t in texts_out), "断点规则必须回灌"
    assert profile.get("chunk_meta"), "断点的 chunk_meta 必须回灌"
    assert profile["chunk_meta"][0].get("chapter_range") == "1-5"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("全部通过")
