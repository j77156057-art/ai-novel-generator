"""原文边界的界面入口 —— 后端这半边的接线。

两件事：

1. **「项目锚定在参考书上」→ 生成走续写路径。**
   以前只有请求里显式传 `profile=continuation` 才生效；项目里存了
   `story_authority='reference_anchor'` 和边界，生成时却按原创路径跑 ——
   参考书原文被整本挡掉，边界填了等于没填。
2. **语料列表带出最大章节号。** 界面上的边界输入框要有上界，
   否则用户只能靠后端 400 才知道自己填错了。

界面那半边（前端向导的输入与提交）见 `frontend/src/utils/referenceBoundary.ts`。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.generation.engine import build_context
from app.generation.profiles import resolve_profile
from app.models import NovelChunk, Project, RawNovel


def fixture():
    """一本 7 章的参考书；第 6、7 章是「未来」（边界之后不该被看见）。"""
    db = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(db.bind)
    p = Project(
        title="支线",
        reference_novel_ids=[1],
        premise="留在东院",
        generation_length=1000,
    )
    db.add_all([p, RawNovel(id=1, title="参考")])
    for ch in range(1, 8):
        db.add(
            NovelChunk(
                novel_id=1,
                chapter_no=ch,
                idx=0,
                text=(f"张三第{ch}章" + ("未来哨兵" if ch > 5 else "过去原文")) * 90,
            )
        )
    db.commit()
    return db, p


# ── 路径推导 ────────────────────────────────────────────────────────────


def test_anchored_project_resolves_to_continuation():
    db, p = fixture()
    p.story_authority = "reference_anchor"
    assert resolve_profile(p, None) == "continuation"


def test_branch_project_resolves_to_original():
    db, p = fixture()
    p.story_authority = "generated_branch"
    assert resolve_profile(p, None) == "original"


def test_explicit_profile_wins_over_authority():
    """显式指定就尊重显式值 —— 调用方最清楚自己要什么。"""
    db, p = fixture()
    p.story_authority = "reference_anchor"
    assert resolve_profile(p, "original") == "original"
    assert resolve_profile(p, "roleplay") == "roleplay"


def test_blank_profile_is_treated_as_absent():
    """空串不是「显式指定空路径」，而是没指定 → 照项目推导。"""
    db, p = fixture()
    p.story_authority = "reference_anchor"
    assert resolve_profile(p, "") == "continuation"
    assert resolve_profile(p, "   ") == "continuation"


def test_unknown_explicit_profile_falls_back_to_original():
    """拼错的路径名退回可见范围最小的那条，不报错也不放行。"""
    db, p = fixture()
    assert resolve_profile(p, "contiuation") == "original"


def test_resolve_is_idempotent():
    """解析出来的路径再解析，还是同一个值（路由层与引擎层各调一次是安全的）。"""
    db, p = fixture()
    p.story_authority = "reference_anchor"
    once = resolve_profile(p, None)
    assert resolve_profile(p, once) == once


# ── 推导出来的路径真的生效 ─────────────────────────────────────────────


def test_anchored_project_without_explicit_profile_reads_reference_up_to_cutoff():
    """不传 profile 的锚定项目：能读到边界为止，读不到边界之后。"""
    db, p = fixture()
    p.story_authority = "reference_anchor"
    p.reference_cutoffs = [{"novel_id": 1, "chapter": 5}]
    db.commit()

    ctx = build_context(db, p)
    assert ctx["rag_items"], "锚定项目应当取得到参考书原文（续写路径）"
    assert ctx["_profile"] == "continuation"
    blob = " ".join(str(h.get("text") or "") for h in ctx["rag_items"])
    assert "过去原文" in blob
    assert "未来哨兵" not in blob, "边界之后的章节不能进上下文"


def test_anchored_project_keeps_whole_book_out_of_reach_without_cutoff():
    """锚定但没填边界：仍然 fail closed（整本不可见），而不是悄悄放开。"""
    db, p = fixture()
    p.story_authority = "reference_anchor"
    db.commit()

    ctx = build_context(db, p)
    assert not ctx["rag_items"]
    warnings = ctx["visibility_diagnostics"]["warnings"]
    assert any("待定位" in w for w in warnings)


def test_branch_project_without_explicit_profile_keeps_original_behaviour():
    """自己的故事：参考书只当写法材料，全书可见，检索关闭。"""
    db, p = fixture()
    p.story_authority = "generated_branch"
    db.commit()

    ctx = build_context(db, p)
    assert ctx["_profile"] == "original"
    assert not ctx["rag_items"], "原创路径不做剧情检索"
    warnings = ctx["visibility_diagnostics"]["warnings"]
    assert not any("待定位" in w for w in warnings), "写法参考路径下全书可见，不该报待定位"


# ── 语料列表带出章节上界 ────────────────────────────────────────────────


def test_novel_list_reports_max_chapter_no():
    from app.routers.novels import list_novels

    db, p = fixture()
    novels = list_novels(db)
    assert len(novels) == 1
    assert novels[0].max_chapter_no == 7


def test_novel_without_chunks_reports_zero():
    from app.routers.novels import list_novels

    db, p = fixture()
    db.add(RawNovel(id=2, title="空书"))
    db.commit()
    novels = {n.id: n for n in list_novels(db)}
    assert novels[2].max_chapter_no == 0


# ── 诊断不能是流水账 ───────────────────────────────────────────────────


def test_exclusions_are_summarized_not_itemized():
    """排除项按 (kind, reason) 归并：总数 + 有限几个例子，不逐条记账。

    真跑发现的：真书 990 章 + 边界在第 100 章时，样例采样挡掉的候选有
    4903 段，逐条记账把预检响应撑到 470 KB。诊断要回答「多少、为什么、举例」，
    不是把整本书列一遍。
    """
    from app.generation.visibility import EXCLUDED_EXAMPLE_LIMIT, VisibilityPolicy

    class _P:
        story_authority = "reference_anchor"
        reference_cutoffs = [{"novel_id": 1, "chapter": 5}]

    vis = VisibilityPolicy(_P(), [1])
    hits = [
        {"novel_id": 1, "chapter_no": ch, "chunk_id": ch, "idx": 0, "text": "原文" * 50}
        for ch in range(6, 60)
    ]
    assert not vis.filter_hits(hits, "rag")
    record = next(r for r in vis.diagnostics["excluded"] if r["kind"] == "rag")
    assert record["reason"] == "future_source"
    assert record["count"] == 54
    assert len(record["examples"]) == EXCLUDED_EXAMPLE_LIMIT
    assert record["examples"][0]["sources"][0]["chapter_no"] == 6
    # 例子只留**能定位**的字段，不带正文（诊断不是正文仓库）
    for example in record["examples"]:
        assert "text" not in example
        assert all("text" not in s for s in example["sources"])


def test_exclusion_reasons_are_still_visible():
    """归并之后，原因仍然一眼可见（下游按 reason 判定）。"""
    from app.generation.visibility import VisibilityPolicy

    class _P:
        story_authority = "reference_anchor"
        reference_cutoffs = [{"novel_id": 1, "chapter": 5}]

    vis = VisibilityPolicy(_P(), [1])
    vis.filter_hits([{"novel_id": 2, "chapter_no": 1, "chunk_id": 1}], "rag")
    reasons = {r["reason"] for r in vis.diagnostics["excluded"]}
    assert "missing_or_unselected_novel" in reasons

