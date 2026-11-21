"""A1 章节身份：章号推导 / 四个入口接线 / 两套编号隔离。

验证边界（用户 2026-11-15 定）：只到「构造请求 → 断言引擎收到的 chapter_range
与落库标签为 `第N章`」这一层，用桩替换引擎入口，**不调模型、不跑真实生成**。

覆盖：
- chapter_labels 单元：入参解析、推导、冲突、区间、标题缺失留痕
- 四个入口：/generate、/generate-stream、/generate-loop、/regenerate-stream
- 两套编号隔离：`段N` 不计入章号；章号不改 segment_no
- `_commit_segment` / 重写路径写 `quality_json.chapter_meta`

跑法：cd backend && python tests/test_chapter_identity.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.db import Base
import app.models  # noqa: F401
from app.models import Generation, Project
from app.generation import chapter_labels
from app.generation.chapter_labels import ChapterRangeError
from app.generation.engine import _commit_segment, _next_segment_no

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


def expect_error(label: str, fn) -> None:
    try:
        fn()
    except ChapterRangeError as exc:
        check_true(f"{label}（{str(exc)[:40]}）", True)
    except Exception as exc:  # noqa: BLE001
        check(f"{label} 抛错类型", type(exc).__name__, "ChapterRangeError")
    else:
        check(label, "未抛错", "ChapterRangeError")


# —— 内存库 + TestClient（StaticPool：SSE 生成器自建的 SessionLocal 同库）——
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)

import app.db as _appdb  # noqa: E402

_appdb.SessionLocal = Session
_appdb.engine = engine

from app.main import app  # noqa: E402
from app.db import get_db as _get_db  # noqa: E402


def override_get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[_get_db] = override_get_db
client = TestClient(app)

OUTLINE = [f"第{i}章《题{i}》：梗概" for i in range(1, 51)]


def make_project(title: str, outline: list | None = None, labels: list[str] | None = None) -> int:
    db = Session()
    p = Project(title=title, target_length=30000, generation_length=1500,
                mode="quick", status="created", outline=OUTLINE if outline is None else outline)
    db.add(p)
    db.commit()
    db.refresh(p)
    for idx, label in enumerate(labels or [], start=1):
        db.add(Generation(project_id=p.id, segment_no=idx, chapter_range=label, text="占位"))
    db.commit()
    pid = p.id
    db.close()
    return pid


# ====================================================================
# 1. 入参解析 / 标签
# ====================================================================
print("\n=== 1. parse_requested / build_label ===")
check("空入参 → None", chapter_labels.parse_requested(None), None)
check("空白入参 → None", chapter_labels.parse_requested("  "), None)
check("'44'", chapter_labels.parse_requested("44"), (44, 44))
check("'44-46'", chapter_labels.parse_requested("44-46"), (44, 46))
check("全角区间 '44–46'", chapter_labels.parse_requested("44–46"), (44, 46))
check("'44~46'", chapter_labels.parse_requested("44~46"), (44, 46))
expect_error("章号 0 拒绝", lambda: chapter_labels.parse_requested("0"))
expect_error("区间倒序拒绝", lambda: chapter_labels.parse_requested("46-44"))
expect_error("贴标签式入参拒绝", lambda: chapter_labels.parse_requested("第44章"))
check("单章带标题", chapter_labels.build_label(44, 44, "到期"), "第44章《到期》")
check("单章无标题", chapter_labels.build_label(44, 44, None), "第44章")
check("区间不挂标题", chapter_labels.build_label(44, 46, "到期"), "第44–46章")

# ====================================================================
# 2. 章号推导边界
# ====================================================================
print("\n=== 2. next_chapter_number / resolve ===")
pid_empty = make_project("空项目")
db = Session()
p_empty = db.get(Project, pid_empty)
plan = chapter_labels.resolve(db, p_empty, None)
check("空项目推导第 1 章", plan["label"], "第1章《题1》")
check("空项目 derived=True", plan["derived"], True)
check("标题来源 outline", plan["title_source"], "outline")

# 段标签不计入章号：同一项目里 段1..段43 存在时，下一章仍是第 1 章
pid_seg = make_project("段标签项目", labels=[f"段{i}" for i in range(1, 44)])
p_seg = db.get(Project, pid_seg)
check("段标签不产生章号", chapter_labels.existing_chapters(db, pid_seg), [])
check("段1–43 后推导第 1 章", chapter_labels.next_chapter_number(db, pid_seg), 1)
check("段标签下 resolve", chapter_labels.resolve(db, p_seg, None)["label"], "第1章《题1》")

# 已有第43章 → 推导 44
pid_ch43 = make_project("写到 43", labels=["第43章《题43》"])
p_ch43 = db.get(Project, pid_ch43)
plan = chapter_labels.resolve(db, p_ch43, None)
check("第43章后推导 44", plan["label"], "第44章《题44》")
check("推导章号", plan["chapter"], 44)

# 区间标签展开：第44–46章 → 下一章 47
pid_span = make_project("区间章", labels=["第44–46章"])
check("区间标签计入末章", chapter_labels.existing_chapters(db, pid_span), [44, 45, 46])
check("区间后推导 47", chapter_labels.next_chapter_number(db, pid_span), 47)

# 显式入参优先
check("显式 '45'", chapter_labels.resolve(db, p_ch43, "45")["label"], "第45章《题45》")
expect_error("显式撞已存在章号拒绝", lambda: chapter_labels.resolve(db, p_ch43, "43"))
check("allow_existing 放行重写",
      chapter_labels.resolve(db, p_ch43, "43", allow_existing=True)["label"], "第43章《题43》")

# outline 取不到标题 → 第N章 + missing_outline
pid_short = make_project("大纲只到 3", outline=OUTLINE[:3], labels=["第43章《题43》"])
p_short = db.get(Project, pid_short)
plan = chapter_labels.resolve(db, p_short, None)
check("越出 outline 仍出章", plan["label"], "第44章")
check("标题缺失留痕", plan["title_source"], "missing_outline")

# ====================================================================
# 3. 多段（loop）标签
# ====================================================================
print("\n=== 3. resolve_many ===")
check("count=1 保留区间标签",
      chapter_labels.resolve_many(db, p_ch43, "44-46", 1)[0]["label"], "第44–46章")
labels = [x["label"] for x in chapter_labels.resolve_many(db, p_ch43, None, 2)]
check("推导两段各占一章", labels, ["第44章《题44》", "第45章《题45》"])
labels = [x["label"] for x in chapter_labels.resolve_many(db, p_ch43, "44-46", 2)]
check("区间按段展开", labels, ["第44章《题44》", "第45章《题45》"])
expect_error("区间不够覆盖段数拒绝",
             lambda: chapter_labels.resolve_many(db, p_ch43, "44", 2))

# ====================================================================
# 4. commit_meta（落库旁路）
# ====================================================================
print("\n=== 4. commit_meta / _commit_segment ===")
check("段标签不写 chapter_meta", chapter_labels.commit_meta(p_ch43, "段7"), None)
meta = chapter_labels.commit_meta(p_ch43, "第44章《题44》")
check("章标签 title_source", meta["title_source"], "outline")
check("章标签 chapter", meta["chapter"], 44)
meta = chapter_labels.commit_meta(db.get(Project, pid_short), "第44章")
check("缺大纲 title_source", meta["title_source"], "missing_outline")

pid_commit = make_project("落库章", labels=["第43章《题43》"])
db_commit = Session()
p_commit = db_commit.get(Project, pid_commit)
gen, seg_no = _commit_segment(db_commit, p_commit, "正文正文正文。", "摘要",
                              quality={}, chapter_range="第44章《题44》")
q = json.loads(gen.quality_json)
check("落库标签", gen.chapter_range, "第44章《题44》")
check("chapter_meta.title_source", q["chapter_meta"]["title_source"], "outline")
check("chapter_meta.chapter", q["chapter_meta"]["chapter"], 44)
check("章号不影响段号（已有 1 行 → 2）", seg_no, 2)

gen2, seg2 = _commit_segment(db_commit, p_commit, "另一段。", "摘要2", quality={})
q2 = json.loads(gen2.quality_json)
check("段模式仍写段标签", gen2.chapter_range, f"段{seg2}")
check("段模式无 chapter_meta", "chapter_meta" in q2, False)
check("段号递增不受章号影响", seg2, 3)
db_commit.close()

# ====================================================================
# 5. 四个入口接线（桩替换引擎）
# ====================================================================
print("\n=== 5. 路由接线 ===")
import app.routers.generation as gen_router  # noqa: E402
import app.generation.engine as engine_mod  # noqa: E402

calls: list = []


def _append_gen(db, project, label):
    seg = _next_segment_no(db, project.id)
    db.add(Generation(project_id=project.id, segment_no=seg, chapter_range=label, text="正文"))
    db.commit()
    return seg


def fake_generate(db, project, novel_ids=None, max_chars=None, chapter_range=None, **kw):
    calls.append(("generate", chapter_range))
    _append_gen(db, project, chapter_range)
    return {"repetition": {}, "length": {}, "summary_style": None, "warnings": [],
            "system_prompt": "S", "user_prompt": "U", "constraints": {}, "rag_context": ""}


def fake_generate_loop(db, project, novel_ids=None, provider=None, segments=1,
                       max_chars=None, chapter_labels=None):
    calls.append(("loop", list(chapter_labels or [])))
    out = []
    for lab in chapter_labels or []:
        seg = _append_gen(db, project, lab)
        out.append({"segment_no": seg, "summary": "s", "system_prompt": "S", "constraints": {}})
    return out


def fake_next_stream(db, project, novel_ids=None, provider=None, max_chars=None,
                     chapter_range=None, **kw):
    calls.append(("stream", chapter_range))
    yield {"type": "done", "segment_no": 1}


def fake_regenerate(db, project, segment_no, novel_ids=None, provider=None, max_chars=None,
                    chapter_label=None, **kw):
    calls.append(("regenerate", chapter_label))
    # 桩不落正文：必须把端点先建的 pending 快照收尾，否则同一段的下一次
    # 重写会被唯一索引挡成 SnapshotBusy（409）。
    from app.agents.dialog.snapshot import fail_snapshot
    fail_snapshot(db, kw.get("snapshot_id"), "fake_regenerate")
    yield {"type": "done", "segment_no": segment_no}


gen_router.generate_next_segment = fake_generate
gen_router.generate_loop = fake_generate_loop
engine_mod.generate_next_segment_stream = fake_next_stream
engine_mod.regenerate_segment_stream = fake_regenerate

# 5a /generate：不传 → 推导 44
pid_a = make_project("入口A", labels=["第43章《题43》"])
r = client.post("/api/generate", json={"project_id": pid_a})
check("/generate 状态码", r.status_code, 200)
check("/generate 引擎收到", calls[-1], ("generate", "第44章《题44》"))
db_a = Session()
last = db_a.scalars(select(Generation).where(Generation.project_id == pid_a)
                    .order_by(Generation.segment_no.desc())).first()
check("/generate 落库标签", last.chapter_range, "第44章《题44》")
db_a.close()

# 5b /generate 显式入参
r = client.post("/api/generate", json={"project_id": pid_a, "chapter_range": "45"})
check("显式章号 45", calls[-1], ("generate", "第45章《题45》"))
# 5c 撞章 → 400，且不调引擎
before = len(calls)
r = client.post("/api/generate", json={"project_id": pid_a, "chapter_range": "44"})
check("撞章 400", r.status_code, 400)
check_true("撞章不调引擎", len(calls) == before)
check_true("撞章错误可读", "第44章" in r.json()["detail"])
# 5d 非法入参 → 400
r = client.post("/api/generate", json={"project_id": pid_a, "chapter_range": "第44章"})
check("非法入参 400", r.status_code, 400)

# 5e /generate-stream：不传 → 推导
pid_b = make_project("入口B", labels=["第43章《题43》"])
r = client.post("/api/generate-stream", json={"project_id": pid_b})
check("/generate-stream 状态码", r.status_code, 200)
check("/generate-stream 引擎收到", calls[-1], ("stream", "第44章《题44》"))
check_true("/generate-stream SSE 有 done", '"done"' in r.text)

# 5f /generate-loop：两段各占一章
pid_c = make_project("入口C", labels=["第43章《题43》"])
r = client.post("/api/generate-loop", json={"project_id": pid_c, "segments": 2})
check("/generate-loop 状态码", r.status_code, 200)
check("/generate-loop 逐段标签", calls[-1],
      ("loop", ["第44章《题44》", "第45章《题45》"]))
check("/generate-loop 返回段数", len(r.json()["segments"]), 2)
# 5g loop 撞章 → 400
r = client.post("/api/generate-loop", json={"project_id": pid_c, "segments": 2,
                                            "chapter_range": "44-45"})
check("loop 撞章 400", r.status_code, 400)

# 5h /regenerate-stream：不传 → 保留原标签（不推导）
pid_d = make_project("入口D", labels=["第43章《题43》"])
r = client.post("/api/regenerate-stream", json={"project_id": pid_d, "segment_no": 1})
check("/regenerate-stream 状态码", r.status_code, 200)
check("/regenerate-stream 不传保留标签", calls[-1], ("regenerate", None))
# 5i 传了 → 允许指向已存在的章（重写声明）
r = client.post("/api/regenerate-stream",
                json={"project_id": pid_d, "segment_no": 1, "chapter_range": "43"})
check("/regenerate-stream 改标签", calls[-1], ("regenerate", "第43章《题43》"))
# 5j 段不存在 → 404
r = client.post("/api/regenerate-stream", json={"project_id": pid_d, "segment_no": 99})
check("/regenerate-stream 缺段 404", r.status_code, 404)

# ====================================================================
# 6. 章号开关：不按章号分支（SENTENCE_SCOPE_FROM_CHAPTER 只写不读）
# ====================================================================
print("\n=== 6. 按章号判断的开关 ===")
from app.coherence import chapter_index  # noqa: E402
from app.generation.engine import (  # noqa: E402
    _chapter_last_ordinal,
    _chapter_ordinal,
    _timeline_progress,
)

check("第44章解析", chapter_index("第44章《题44》"), 44)
check("段标签不解析成章号", chapter_index("段44"), None)
check("纯数字不解析成章号", chapter_index("44"), None)
# 一致性层的正则是 `第N章`，区间标签由 chapter_labels 兜住（不另改共用函数）
check("区间标签 _chapter_ordinal 取起章", _chapter_ordinal("第44–46章"), 44)
check("区间标签 _chapter_last_ordinal 取末章", _chapter_last_ordinal("第44–46章"), 46)
check("单章 last == 起章", _chapter_last_ordinal("第44章《题44》"), 44)
db_t = Session()
p_span = db_t.get(Project, pid_span)
check("区间章推进量按末章", _timeline_progress(db_t, p_span), (46, True))
p_seg_t = db_t.get(Project, pid_seg)
check("纯段标签回退行数", _timeline_progress(db_t, p_seg_t), (43, False))
db_t.close()

engine_src = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
# 只写不读：除 import 与两处 dict 字面量（gate_limits 元数据）外，无任何分支读它
readers = [
    ln for ln in engine_src.splitlines()
    if "SENTENCE_SCOPE_FROM_CHAPTER" in ln
    and not ln.strip().startswith(("SENTENCE_SCOPE_FROM_CHAPTER,", "fallback[", '"sentence_scope_from_chapter"'))
    and '"sentence_scope_from_chapter": SENTENCE_SCOPE_FROM_CHAPTER' not in ln
]
check("无按章号分支的读取点", readers, [])

print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
