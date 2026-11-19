"""续写分支必须把续写书并入 `reference_novel_ids`（2026-11-11）。

## 被钉住的缺陷

文风档案的数据源是 `project.reference_novel_ids`（`style/pipeline.py`、
`style/narrative.py` 都只读它）。但向导续写分支只写 `generation_length` 与
`continuation_novel_id`，从不写参考书 → **续写项目永远拿不到档案**，而且
`novel_chunks` 没语料时蒸馏会静默产出 0 条规则。

## 本文件守护的

  C1  续写分支写入后 `reference_novel_ids` 含续写书，且不动用户已有的参考书。
  C2  重复提交幂等（不重复追加）。
  C3  续写书没有语料 → 409 + 可操作提示，且不写任何字段。
  C4  档案读取路径（`_style_source_ids` + `_narrative_block`）能按续写书取到
      数据源，且别的书的角色规则不会混进 system prompt。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi import BackgroundTasks, HTTPException  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import _style_source_ids  # noqa: E402
from app.generation.prompts import _narrative_block  # noqa: E402
from app.models import AssistantWizardSession, NovelChunk, Project, RawNovel  # noqa: E402
from app.routers.assistant_wizard import _commit  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _novel(db, title: str, chunks: int) -> RawNovel:
    novel = RawNovel(title=title, status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(chunks):
        db.add(NovelChunk(novel_id=novel.id, chapter_no=i + 1, idx=i, text="第一章正文。"))
    db.commit()
    return novel


def _project(db, refs: list[int] | None = None) -> Project:
    project = Project(
        title="续写项目", target_length=5000, mode="quick", status="created",
        reference_novel_ids=refs,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _session(db, project: Project, scope: int | None) -> AssistantWizardSession:
    session = AssistantWizardSession(
        project_id=project.id,
        kind="continuation",
        step_key="confirm",
        answers={"branch": "continuation", "length": 3000, "book_scope": scope},
        overrides={},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_continuation_merges_scope_into_existing_reference_books(db):
    """C1：续写书并入参考书，用户手工设的其他参考书保留。"""
    source = _novel(db, "续写书", chunks=3)
    other = _novel(db, "用户自己加的文风参考", chunks=2)
    project = _project(db, refs=[other.id])

    result = _commit(db, project, _session(db, project, source.id), BackgroundTasks())
    db.refresh(project)

    assert project.continuation_novel_id == source.id
    assert project.reference_novel_ids == [other.id, source.id], (
        f"续写书没并进参考书，档案读不到数据源：{project.reference_novel_ids}"
    )
    assert "reference_novel_ids" in result["result"]["applied"]


def test_continuation_commit_is_idempotent(db):
    """C2：同一选择重复提交不会把同一本书追加两遍。"""
    source = _novel(db, "续写书", chunks=2)
    project = _project(db)

    _commit(db, project, _session(db, project, source.id), BackgroundTasks())
    _commit(db, project, _session(db, project, source.id), BackgroundTasks())
    db.refresh(project)

    assert project.reference_novel_ids == [source.id]


def test_continuation_without_corpus_is_rejected_with_actionable_message(db):
    """C3：续写书没语料 → 409，提示先去导入，且不改字段。"""
    empty = _novel(db, "空书", chunks=0)
    project = _project(db, refs=[999])

    with pytest.raises(HTTPException) as exc:
        _commit(db, project, _session(db, project, empty.id), BackgroundTasks())

    assert exc.value.status_code == 409
    assert "导入" in exc.value.detail and "空书" in exc.value.detail
    db.refresh(project)
    assert project.continuation_novel_id is None
    assert project.reference_novel_ids == [999], "校验失败却改了参考书"


def _voice(novel_id: int, name: str) -> dict:
    return {
        "novel_id": novel_id,
        "rule": (
            f"{name}：自称「我」，口头禅「罢了」，句长中短，语气克制，"
            "节奏偏快，关系动态：与同伴互相信任，代表台词：「走吧。」"
        ),
    }


def test_read_path_scopes_narrative_to_continuation_book(db):
    """C4：档案读取路径只取续写书来源的规则。"""
    source = _novel(db, "续写书", chunks=2)
    other = _novel(db, "另一本参考书", chunks=2)
    project = _project(db)
    _commit(db, project, _session(db, project, source.id), BackgroundTasks())
    db.refresh(project)

    narrative = {
        "rules": {
            "角色声音词典": [_voice(source.id, "许七安"), _voice(other.id, "宫爵")],
            # 规则必须带「证据：」+ 章号才过硬过滤（`narrative._HARD_FILTER_MASK`：
            # 无证据 / 无章号的规则不会被渲染）。本用例钉的是来源隔离，不是证据门槛，
            # 所以 fixture 要满足蒸馏产出的真实形状，否则整维会被清空。
            "人物描写手法": [
                {
                    "novel_id": source.id, "chapter_no": 3,
                    "rule": "先写环境再写人（续写书规则）（证据：“他站在廊下，雨丝斜斜落进来，先看见的是灯，再看见人。”）",
                },
                {
                    "novel_id": other.id, "chapter_no": 7,
                    "rule": "先写奢侈品再写人（另一本书规则）（证据：“她进门先看见的是那盏水晶灯，灯下才坐着人。”）",
                },
            ],
        }
    }

    allowed = _style_source_ids(project)
    assert allowed == {source.id}, f"续写项目的档案来源没收窄到续写书：{allowed}"

    block = _narrative_block(narrative, allowed)
    assert "许七安" in block and "续写书规则" in block
    assert "宫爵" not in block and "另一本书规则" not in block, (
        "别的参考书的角色/手法混进了续写项目的 system prompt"
    )


def test_original_project_scopes_to_reference_books(db):
    """原创项目没有「本书」，来源范围就是当前参考书集合。"""
    project = _project(db, refs=[3, 7])
    assert _style_source_ids(project) == {3, 7}


def test_generation_path_keeps_archive_when_refs_narrowed(db):
    """refs 从 [5, 6] 收窄到 [5] 后，生成路径仍复用「来源是超集」的档案。

    指纹要求 source_ids 完全相等时，收窄 refs 会让整份档案消失（比跨书规则
    更糟）。生成路径应当继续读这份档案，由渲染侧的来源过滤去掉别的书。
    """
    from app.style.narrative import project_narrative_safe  # noqa: PLC0415

    project = _project(db, refs=[5])
    project.style_narrative = {
        "fingerprint": {"source_ids": [5, 6], "tier": "balanced"},
        "rules": {
            "角色声音词典": [
                _voice(5, "许七安"),
                _voice(6, "宫爵"),
            ],
        },
        "forbidden": {},
    }
    db.commit()

    got = project_narrative_safe(db, project, persist_library=False)
    assert got is not None, "refs 收窄后生成路径把整份档案丢了"
    block = _narrative_block(got, _style_source_ids(project))
    assert "许七安" in block
    assert "宫爵" not in block, "超集档案里的跨书条目没有被渲染侧过滤"


# ====================================================================
# 预检与取档必须共用同一份判据（2026-11-12）
#
# 缺陷：取档侧已放宽到「档案来源覆盖当前参考书即可复用」，预检
# `_require_distill_ready` 却仍要求指纹完全相等。项目 5 收窄到 [5] 后
# 预检先 raise，生成直接 500，放宽的代码永远走不到。
# ====================================================================


def _archive(source_ids: list[int]) -> dict:
    return {
        "fingerprint": {"source_ids": source_ids, "tier": "balanced"},
        "rules": {
            "人物描写手法": [
                {"novel_id": source_ids[0], "rule": "先写环境再写人"},
            ],
        },
        "forbidden": {},
    }


def test_precheck_accepts_superset_archive_without_distill(db, monkeypatch):
    """超集放行：缓存是 [5,6]、项目要 [5] → 就绪，且一次 hash 都不算。"""
    from app.generation.engine import _require_distill_ready  # noqa: PLC0415
    import app.style.narrative as narrative_mod  # noqa: PLC0415

    def _no_hash(*a, **k):
        raise AssertionError("缓存超集命中不应计算 corpus_content_hash")

    monkeypatch.setattr(narrative_mod, "corpus_content_hash", _no_hash)
    project = _project(db, refs=[5])
    project.style_narrative = _archive([5, 6])
    db.commit()

    _require_distill_ready(db, project)  # 不抛异常即通过


def test_precheck_rejects_when_archive_does_not_cover_project(db, monkeypatch):
    """反向拒绝：档案只覆盖 [5]、项目要 [5,6] → 仍要求先蒸馏。"""
    from app.generation.engine import _require_distill_ready  # noqa: PLC0415

    project = _project(db, refs=[5, 6])
    project.style_narrative = _archive([5])
    db.commit()

    with pytest.raises(ValueError, match="尚未提炼"):
        _require_distill_ready(db, project)


def test_precheck_and_retrieval_share_one_criterion(db, monkeypatch):
    """判据只有一份：预检直接问 `reusable_narrative`，改它的返回值即可翻转结论。"""
    from app.generation.engine import _require_distill_ready  # noqa: PLC0415
    import app.style.narrative as narrative_mod  # noqa: PLC0415

    project = _project(db, refs=[5])
    project.style_narrative = _archive([5])

    monkeypatch.setattr(narrative_mod, "reusable_narrative", lambda *a, **k: None)
    with pytest.raises(ValueError, match="尚未提炼"):
        _require_distill_ready(db, project)

    monkeypatch.setattr(
        narrative_mod, "reusable_narrative", lambda *a, **k: _archive([5])
    )
    _require_distill_ready(db, project)  # 不抛 → 预检读的就是这个 helper
