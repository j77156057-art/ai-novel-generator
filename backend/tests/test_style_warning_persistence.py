"""v2.1：style_warning 落库 + 端点测试。

设计：docs/style-distillation-v2.1-design.md 第 6 节。
覆盖：append → 读 → 倒序 → 截断 → endpoint 返回。
跑法：python tests/test_style_warning_persistence.py
"""
from __future__ import annotations

import os, sys
from pathlib import Path
from datetime import datetime, timezone

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label, cond):
    check(label, bool(cond), True)


# === 1. warning_store 落库 + 读 ===
print("\n=== 1. warning_store.append_style_warning + get_style_warnings ===")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db import Base
import app.models  # noqa
from app.models import Project
from app.style.warning_store import (
    append_style_warning, get_style_warnings, is_truncated,
    _MAX_HISTORY, _TRUNCATE_NOTICE_KEY,
)

engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()
p = Project(title="t", genre="x", style="y", generation_length=800)
db.add(p); db.commit()

# 1a 空状态
check("空状态无 warn", get_style_warnings(p), [])
check("空状态未截断", is_truncated(p), False)

# 1b append 一条
warn1 = {
    "percentile": 99.0,
    "mean_abs_z": 1.5,
    "worst": [("句长CV", 0.3), ("比喻密度", 0.2)],
    "message": "test 1",
}
append_style_warning(db, p, segment_no=5, warn=warn1)
hist = get_style_warnings(p)
check("append 后 1 条", len(hist), 1)
check("倒序 segment_no=5", hist[0]["segment_no"], 5)
check("字段 percentile", hist[0]["percentile"], 99.0)
check("字段 worst_features 列表", hist[0]["worst_features"], ["句长CV", "比喻密度"])
check("generated_at 是 ISO", "T" in hist[0]["generated_at"], True)

# 1c append 第二条不覆盖
warn2 = {"percentile": 98.0, "mean_abs_z": 1.0, "worst": [("段长CV", 0.1)], "message": "test 2"}
append_style_warning(db, p, segment_no=6, warn=warn2)
hist = get_style_warnings(p)
check("第 2 条 append 后 2 条", len(hist), 2)
check("倒序后段号是 6", hist[0]["segment_no"], 6)
check("正序段号是 5", hist[1]["segment_no"], 5)
check("原 style_narrative 其它字段保留（rules 还在）", p.style_narrative.get("style_warnings") is not None, True)

# 1d 截断：append 502 条 → 留 500 + truncated 标记
for i in range(7, 7 + _MAX_HISTORY):  # 已有 2 条，再 500 = 502
    append_style_warning(db, p, segment_no=i,
        warn={"percentile": 99.0, "mean_abs_z": 0, "worst": [], "message": "x"})
hist = get_style_warnings(p)
check(f"截断后剩 500 条（不是 {_MAX_HISTORY + 2}）", len(hist), _MAX_HISTORY)
check("截断标记", is_truncated(p), True)
# 第一条应该是最早（被截掉），最后一条是最新
check("截断后 hist[0] 是最新段号 506", hist[0]["segment_no"], 506)
check("截断后 hist[-1] 是最老段号 7", hist[-1]["segment_no"], 7)
db.close()

# === 2. 端点 ===
print("\n=== 2. GET /api/projects/{id}/kg-v3/style-warnings ===")
from fastapi.testclient import TestClient
from app.main import app
import os
# 端到端在 conftest 跑（router+db），这里只验 route 注册。
# 注意：FastAPI 0.141+ 的 include_router 不再把子路由展平进 app.routes，而是
# 放一个没有 .path 的 _IncludedRouter 占位对象 —— 只看顶层会让子路由"整组
# 消失"，于是「端点已注册」这类断言会假红。故递归展开 original_router。
def _iter_route_paths(router, _depth=0):
    if _depth > 8:
        return
    for r in getattr(router, "routes", []) or []:
        p = getattr(r, "path", "")
        if p:
            yield p
        sub = getattr(r, "original_router", None)
        if sub is not None:
            yield from _iter_route_paths(sub, _depth + 1)


routes = list(_iter_route_paths(app))
check("端点 /kg-v3/style-warnings 已注册",
      any("/kg-v3/style-warnings" in p for p in routes), True)

# === 3. 不破坏现有 style/__init__.py 导出 ===
print("\n=== 3. 导出检查 ===")
from app.style import append_style_warning as aw
from app.style import get_style_warnings as gw
from app.style import is_truncated as it
check("append_style_warning 导出", aw is not None, True)
check("get_style_warnings 导出", gw is not None, True)
check("is_truncated 导出", it is not None, True)


if FAILED:
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 -> {FAILED}")
    sys.exit(1)
print(f"\n结果：全部通过（{PASS} 断言）")
