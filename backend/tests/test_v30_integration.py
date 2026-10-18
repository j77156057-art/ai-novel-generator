"""v3.0 主链接入 + 读侧 API + 作者豁免测试。

覆盖：
- engine 挂载：同步版/流式版都调 _enqueue_v3_extraction，return/done 带 v3_extraction 键
- _enqueue_v3_extraction：正常入队 / worker 未起时自起 / 异常兜底
- 读侧 API 路由（TestClient）：slice / slice/prompt / relations/curve / knowledge /
  rhythm / events / exemptions POST+GET
- 作者豁免：POST 豁免后 judge_segment 不再报同 (rule, entity)
- 404：不存在的项目

跑法：python tests/test_v30_integration.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from app.db import Base
import app.models  # noqa: F401
from app.models import (
    Fact, Generation, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow,
    KgMention, KgRelation, KgRelationEvolution, Project,
)

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


# —— 内存库 + TestClient ——
# StaticPool 共享单连接：worker 线程自建的 SessionLocal 打开的是**同一个**内存库
# （否则 :memory: 每连接独立，worker 查到空库 no such table）
from sqlalchemy.pool import StaticPool  # noqa: E402

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    future=True,
)
Base.metadata.create_all(engine)
TestingSession = sessionmaker(bind=engine, future=True)

# batch_worker 的后台线程自建 SessionLocal（不经 FastAPI 依赖注入），
# 必须把 app.db.SessionLocal 也指到测试引擎，否则 worker 查的是真空库（no such table）
import app.db as _appdb  # noqa: E402

_appdb.SessionLocal = TestingSession
_appdb.engine = engine

from app.main import app  # noqa: E402


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


from app.db import get_db as _get_db  # noqa: E402

app.dependency_overrides[_get_db] = override_get_db
client = TestClient(app)

# —— 预置数据 ——
db = TestingSession()
proj = Project(title="T", target_length=5000, mode="quick", generation_length=1500, status="created")
db.add(proj)
db.commit()
db.refresh(proj)
PID = proj.id

ly = KgEntity(project_id=PID, kind="character", canonical_name="林悠然", attrs={"secrets": ["母亲之死"]})
gy = KgEntity(project_id=PID, kind="character", canonical_name="顾辰逸")
office = KgEntity(project_id=PID, kind="location", canonical_name="办公室")
db.add_all([ly, gy, office])
db.commit()
ev1 = KgEvent(project_id=PID, sequence=1, segment_no=1, event_type="incident", summary="入职", in_story_date="2024年春")
ev2 = KgEvent(project_id=PID, sequence=2, segment_no=2, event_type="revelation", summary="真相")
db.add_all([ev1, ev2])
db.commit()
from app.models import KgEventLink  # noqa: E402

db.add_all([
    Generation(project_id=PID, segment_no=1, text="林悠然推开门"),
    KgMention(project_id=PID, entity_id=ly.id, segment_no=1, context_window="林悠然推开门"),
    KgMention(project_id=PID, entity_id=ly.id, segment_no=2, context_window="林悠然盯着"),
    KgMention(project_id=PID, entity_id=gy.id, segment_no=2, context_window="顾辰逸赶来"),
    KgRelation(project_id=PID, from_id=ly.id, to_id=gy.id, rel_type="romantic", strength=0.5),
    KgRelationEvolution(project_id=PID, edge_id=1, chapter=5, strength=0.3, note="初见"),
    KgForeshadow(project_id=PID, name="测试伏笔", status="developing", planted_chapter=1, planned_payoff_chapter=50),
    KgEntityKnowledge(project_id=PID, entity_id=ly.id, known_entity_id=office.id, known_kind="entity", learned_at_chapter=1),
    KgEventLink(project_id=PID, from_event_id=ev2.id, to_event_id=ev1.id, link_type="caused_by"),
])
db.commit()

# ====================================================================
# 1. engine 挂载（静态检查）
# ====================================================================
engine_py = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
check("engine 调 _enqueue_v3_extraction 次数", engine_py.count("_enqueue_v3_extraction(db, project, segment_no, text)"), 2)
check("同步版 return 带 v3_extraction", engine_py.count('"v3_extraction": v3_extraction'), 2)

# ====================================================================
# 2. _enqueue_v3_extraction 行为
# ====================================================================
from app.generation.engine import _enqueue_v3_extraction  # noqa: E402

proj_obj = db.get(Project, PID)
res = _enqueue_v3_extraction(db, proj_obj, 1, "林悠然推开门，顾辰逸在等她。")
check_true("enqueue 成功", res.get("enqueued") is True)
check_true("pending 计数返回", isinstance(res.get("pending"), int))

# 入队后 flush_project 抽干队列 → 落 v3.0 表（mock LLM 不可用时会降级跳过抽取，不炸）
from app.kg import batch_worker as bw  # noqa: E402

bw.flush_project(PID)
check("flush 后 pending 归零", bw._queue.pending_count(PID), 0)

# ====================================================================
# 3. 读侧 API
# ====================================================================
r = client.get(f"/api/projects/{PID}/kg-v3/slice", params={"segment_no": 2, "characters": "林悠然,顾辰逸"})
check("slice 状态码", r.status_code, 200)
data = r.json()
check("slice entities 数", len(data["recent_entities"]), 2)
check_true("slice 有 active foreshadow", len(data["active_foreshadows"]) == 1)
check_true("slice known_facts 有记录", len(data["known_facts"]) == 1)

r = client.get(f"/api/projects/{PID}/kg-v3/slice/prompt", params={"segment_no": 2})
check("slice/prompt 状态码", r.status_code, 200)
check_true("prompt 文本含实体", "林悠然" in r.json()["text"])

r = client.get(f"/api/projects/{PID}/kg-v3/relations/curve", params={"from": "林悠然", "to": "顾辰逸"})
check("curve 状态码", r.status_code, 200)
check("curve 点数", len(r.json()["curve"]), 1)
check("curve strength", r.json()["curve"][0]["strength"], 0.3)

r = client.get(f"/api/projects/{PID}/kg-v3/knowledge", params={"character": "林悠然", "fact": "办公室"})
check("knowledge 状态码", r.status_code, 200)
check("林悠然知道办公室", r.json()["knows"], True)

r = client.get(f"/api/projects/{PID}/kg-v3/knowledge", params={"character": "顾辰逸", "fact": "办公室"})
check("顾辰逸不知道办公室", r.json()["knows"], False)

r = client.get(f"/api/projects/{PID}/kg-v3/rhythm")
check("rhythm 状态码", r.status_code, 200)
check("rhythm top1 是林悠然", r.json()["top_entities"][0]["name"], "林悠然")

r = client.get(f"/api/projects/{PID}/kg-v3/events")
check("events 状态码", r.status_code, 200)
check("events 数", len(r.json()["events"]), 2)
check("links 数", len(r.json()["links"]), 1)
check("link_type", r.json()["links"][0]["link_type"], "caused_by")

# ====================================================================
# 4. 作者豁免：POST 后 judge 不再报同 (rule, entity)
# ====================================================================
# 先确认 judge 现在会报 foreshadow_expiry（测试伏笔 planned_payoff=50 但事件 chapter 最大 2 → 不超期）
# 构造超期：把伏笔 planned_payoff 改成 1
db.query(KgForeshadow).filter(KgForeshadow.project_id == PID).update({"planned_payoff_chapter": 1})
db.commit()

from app.kg.judge import judge_segment  # noqa: E402

before = judge_segment(db, PID, "正文", segment_no=3, current_chapter=10)
fs_findings = [f for f in before["findings"] if f["rule"] == "foreshadow_expiry"]
check_true("豁免前报 foreshadow_expiry", len(fs_findings) == 1)

# POST 豁免
r = client.post(f"/api/projects/{PID}/kg-v3/exemptions", json={
    "rule": "foreshadow_expiry", "entity": "测试伏笔", "note": "作者故意留的长线伏笔",
})
check("exemption POST 状态码", r.status_code, 200)
check("exemption POST 返回", r.json()["exempted"], True)

# judge 再跑：同 (rule, entity) 不再报
after = judge_segment(db, PID, "正文", segment_no=3, current_chapter=10)
fs_after = [f for f in after["findings"] if f["rule"] == "foreshadow_expiry"]
check("豁免后不再报同 (rule, entity)", fs_after, [])

# GET 豁免清单
r = client.get(f"/api/projects/{PID}/kg-v3/exemptions")
check("exemption GET 状态码", r.status_code, 200)
check("豁免清单 1 条", len(r.json()["exemptions"]), 1)
check("豁免 note 留痕", r.json()["exemptions"][0]["note"], "作者故意留的长线伏笔")

# 幂等：重复 POST 不重复插入
client.post(f"/api/projects/{PID}/kg-v3/exemptions", json={
    "rule": "foreshadow_expiry", "entity": "测试伏笔", "note": "重复",
})
n_exempt = len(client.get(f"/api/projects/{PID}/kg-v3/exemptions").json()["exemptions"])
check("豁免幂等", n_exempt, 1)

# ====================================================================
# 5. 404 / 边界
# ====================================================================
r = client.get("/api/projects/9999/kg-v3/slice", params={"segment_no": 1})
check("不存在项目 404", r.status_code, 404)

r = client.get(f"/api/projects/{PID}/kg-v3/slice", params={"segment_no": 1})
check("缺 characters 参数也能跑（mention 反查）", r.status_code, 200)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
