"""文体档案的「量尺 / 规格书」指纹：**加指纹不能让老档案失效**。

## 为什么这条要单独测

档案指纹原来只有 `source_ids / unit_chars / version / schema`。搬 main 时新增
`measurement`（同一段文字**怎么量**：分句规则 / 密度分母 / 特征集）与
`spec`（规格书版本）。老档案没有这两个键 ⇒ 字典不相等。

如果照旧走「不等就重拟」，**加指纹这个动作本身就会把所有项目档案重蒸一遍** ——
那是用户没同意的写操作（覆盖 `projects.style_profile` 并 commit）。

所以把失配分两类：

- **软失配**（只差 `measurement` / `spec`）：老档案继续用，状态标 `stale`，可查询；
- **硬失配**（语料集合 / 粒度 / 结构版本 / schema 变了）：必须重拟（否则算错）。

本文件钉住：老档案被读时**不会被改写**、状态可查询、硬失配仍然重拟。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def db_session():
    from app.db import Base, SessionLocal, engine
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _project(db, *, profile: dict | None, ids=(1,), gen_len=1200):
    from app.models import Project

    p = Project(title="指纹项目", target_length=3000, generation_length=gen_len,
                genre="都市异能", reference_novel_ids=list(ids),
                style_profile=profile or {})
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _legacy_fingerprint(ids=(1,), unit_chars=1200) -> dict:
    """老档案的指纹：**没有** measurement / spec 两个键。"""
    from app.style.pipeline import PIPELINE_SCHEMA
    from app.style.stylometry import PROFILE_VERSION

    return {
        "source_ids": sorted(ids),
        "unit_chars": unit_chars,
        "version": PROFILE_VERSION,
        "schema": PIPELINE_SCHEMA,
    }


def test_fingerprint_records_measurement_and_spec():
    from app.style.pipeline import SPEC_VERSION, profile_fingerprint
    from app.style.stylometry import MEASUREMENT_VERSION

    fp = profile_fingerprint([1], 1200)
    assert fp["measurement"] == MEASUREMENT_VERSION
    assert fp["spec"] == SPEC_VERSION
    assert set(fp) >= {"source_ids", "unit_chars", "version", "schema",
                       "measurement", "spec"}


def test_legacy_profile_is_stale_but_not_recomputed(db_session):
    """老档案只差两个新键 ⇒ 返回老档案本身，**一个字都不写库**。"""
    from app.style.pipeline import project_profile

    legacy = _legacy_fingerprint()
    profile = {"version": 2, "buckets": {}, "fingerprint": legacy}
    project = _project(db_session, profile=profile)

    got = project_profile(db_session, project, unit_chars=1200)
    assert got is not None
    assert got["fingerprint"] == legacy, "老档案被重拟了"
    db_session.refresh(project)
    assert "measurement" not in (project.style_profile.get("fingerprint") or {}), (
        "读一次档案就改写了库里的指纹（这正是要避免的写操作）"
    )


def test_fingerprint_state_is_queryable(db_session):
    """失配状态可查询：stale ≠ needs_recompute，hard 才是。"""
    from app.routers.projects import get_style_profile_fingerprint
    from app.style.pipeline import profile_fingerprint_state

    project = _project(db_session, profile={"fingerprint": _legacy_fingerprint()})

    st = profile_fingerprint_state(db_session, project, unit_chars=1200)
    assert st["state"] == "stale"
    assert set(st["mismatched"]) == {"measurement", "spec"}
    assert st["needs_recompute"] is False

    # 端点直接查得到同一份结论（只读）
    assert get_style_profile_fingerprint(project.id, db_session)["state"] == "stale"


def test_hard_mismatch_still_recomputes(db_session):
    """语料集合变了 ⇒ 硬失配，必须重拟（否则拿错语料算出来的档案继续用）。"""
    from app.style.pipeline import project_profile, profile_fingerprint_state

    profile = {"fingerprint": _legacy_fingerprint(ids=(1,))}
    project = _project(db_session, profile=profile)

    st = profile_fingerprint_state(db_session, project, unit_chars=1200)
    # unit_chars 一致、source_ids 一致 ⇒ 仍是 stale；把粒度改一下才是 hard
    assert st["state"] == "stale"
    st2 = profile_fingerprint_state(db_session, project, unit_chars=3000)
    assert st2["state"] == "hard"
    assert "unit_chars" in st2["mismatched"]
    assert st2["needs_recompute"] is True
    # 硬失配时会去重拟；这里没有语料 1，抛 LookupError 即证明确实没走「沿用老档案」
    with pytest.raises(LookupError):
        project_profile(db_session, project, unit_chars=3000)


def test_no_fingerprint_is_none_not_stale(db_session):
    """早于指纹机制的老档案：`none`（要重拟），不能当成 `stale` 混过去。"""
    from app.style.pipeline import profile_fingerprint_state

    project = _project(db_session, profile={"version": 2})
    st = profile_fingerprint_state(db_session, project, unit_chars=1200)
    assert st["state"] == "none"
    assert st["needs_recompute"] is True
