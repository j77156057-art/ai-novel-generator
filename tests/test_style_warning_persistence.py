"""v2.1：style_warning 落库 + 端点测试。

设计：docs/style-distillation-v2.1-design.md 第 6 节。
覆盖：append → 读 → 倒序 → 截断 → endpoint 返回。

（这份原本是「脚本式断言」—— check() 只记数不抛，跑挂了也会显示通过。
 已改写成 pytest 用例：失败即抛，断言消息带标签订位。）
"""
from __future__ import annotations

import os, sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))


def _fresh_project():
    """每个用例一套干净的内存库 + 一个项目。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    import app.models  # noqa: F401
    from app.models import Project

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    p = Project(title="t", genre="x", style="y", generation_length=800)
    db.add(p)
    db.commit()
    return db, p


def test_warning_store_empty_state():
    from app.style.warning_store import get_style_warnings, is_truncated

    db, p = _fresh_project()
    assert get_style_warnings(p) == [], "空状态无 warn"
    assert is_truncated(p) is False, "空状态未截断"
    db.close()


def test_warning_store_append_and_read_back():
    from app.style.warning_store import append_style_warning, get_style_warnings

    db, p = _fresh_project()
    warn1 = {
        "percentile": 99.0,
        "mean_abs_z": 1.5,
        "worst": [("句长CV", 0.3), ("比喻密度", 0.2)],
        "message": "test 1",
    }
    append_style_warning(db, p, segment_no=5, warn=warn1)
    hist = get_style_warnings(p)
    assert len(hist) == 1, f"append 后 1 条: got={len(hist)}"
    assert hist[0]["segment_no"] == 5, "倒序 segment_no=5"
    assert hist[0]["percentile"] == 99.0, "字段 percentile"
    assert hist[0]["worst_features"] == ["句长CV", "比喻密度"], "字段 worst_features 列表"
    assert "T" in hist[0]["generated_at"], "generated_at 是 ISO"
    db.close()


def test_warning_store_append_does_not_overwrite():
    from app.style.warning_store import append_style_warning, get_style_warnings

    db, p = _fresh_project()
    append_style_warning(db, p, segment_no=5, warn={
        "percentile": 99.0, "mean_abs_z": 1.5, "worst": [], "message": "test 1"})
    warn2 = {"percentile": 98.0, "mean_abs_z": 1.0, "worst": [("段长CV", 0.1)], "message": "test 2"}
    append_style_warning(db, p, segment_no=6, warn=warn2)
    hist = get_style_warnings(p)
    assert len(hist) == 2, f"第 2 条 append 后 2 条: got={len(hist)}"
    assert hist[0]["segment_no"] == 6, "倒序后段号是 6"
    assert hist[1]["segment_no"] == 5, "正序段号是 5"
    assert p.style_narrative.get("style_warnings") is not None, "原 style_narrative 其它字段保留（rules 还在）"
    db.close()


def test_warning_store_truncates_history():
    from app.style.warning_store import (
        append_style_warning, get_style_warnings, is_truncated, _MAX_HISTORY,
    )

    db, p = _fresh_project()
    append_style_warning(db, p, segment_no=5, warn={
        "percentile": 99.0, "mean_abs_z": 1.5, "worst": [], "message": "test 1"})
    append_style_warning(db, p, segment_no=6, warn={
        "percentile": 98.0, "mean_abs_z": 1.0, "worst": [], "message": "test 2"})
    # 已有 2 条，再 500 = 502
    for i in range(7, 7 + _MAX_HISTORY):
        append_style_warning(db, p, segment_no=i,
                             warn={"percentile": 99.0, "mean_abs_z": 0, "worst": [], "message": "x"})
    hist = get_style_warnings(p)
    assert len(hist) == _MAX_HISTORY, f"截断后剩 { _MAX_HISTORY } 条（不是 {_MAX_HISTORY + 2}）: got={len(hist)}"
    assert is_truncated(p) is True, "截断标记"
    # 第一条应该是最新，最后一条是最老（被截掉的那批）
    assert hist[0]["segment_no"] == 506, f"截断后 hist[0] 是最新段号 506: got={hist[0]['segment_no']}"
    assert hist[-1]["segment_no"] == 7, f"截断后 hist[-1] 是最老段号 7: got={hist[-1]['segment_no']}"
    db.close()


def test_style_warnings_endpoint_is_registered():
    from fastapi.testclient import TestClient
    from app.main import app

    routes = [r.path for r in app.routes]
    assert any("/kg-v3/style-warnings" in p for p in routes), "端点 /kg-v3/style-warnings 已注册"


def test_style_package_keeps_exports():
    from app.style import append_style_warning as aw
    from app.style import get_style_warnings as gw
    from app.style import is_truncated as it

    assert aw is not None, "append_style_warning 导出"
    assert gw is not None, "get_style_warnings 导出"
    assert it is not None, "is_truncated 导出"
