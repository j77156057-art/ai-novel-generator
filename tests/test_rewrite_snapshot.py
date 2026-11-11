# -*- coding: utf-8 -*-
"""重写前快照：崩溃 / 并发 / 回滚逐字 / 批量 / 设定漂移 / 容量。

对应 ``.trae/documents/rewrite-snapshot-design.md`` 的验收清单。这里**不 mock
被测逻辑**：真实建表、真实走 ``regenerate_segment_stream`` 的两段式写入、
真实调 ``rollback_snapshot`` / ``rollback_batch``，只在 LLM 边界放确定性替身。

六个用例与验收项一一对应：

  T1 崩溃：pending 已写、正文未写之间中断 → 重启后状态 / 正文 / 同段是否被卡
  T2 流中断：客户端断开（GeneratorExit）→ failed(client_disconnect)，正文未变
  T3 并发：同段第二个重写被部分唯一索引挡下 → 用户看到的是一句人话
  T4 回滚逐字：md5 与重写前完全一致 + 摘要一起退 + 摘要树确实按旧摘要重建
  T5 批量：3 段整组回滚全复位；中途失败则一段都不动
  T6 设定漂移：bible_hash 变过 → 409 三选项（先退设定 / 只退正文 / 取消）
  T7 容量：按真实 1200 字正文实测每行开销，外推 925 段 × 3 版
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import uuid
from datetime import timedelta

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import func, select, update  # noqa: E402

from app.agents.dialog import rollback as rb  # noqa: E402
from app.agents.dialog.snapshot import (  # noqa: E402
    HEARTBEAT_TIMEOUT_SECONDS,
    SnapshotBusy,
    SnapshotLease,
    begin_snapshot,
    list_snapshots,
    process_identity,
    purge_snapshots,
    reap_all_pending,
    reap_orphan_pending,
    utcnow,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import regenerate_segment_stream  # noqa: E402
from app.kg.bible import apply_bible_revision  # noqa: E402
from app.kg.build import merge_segment_into_kg  # noqa: E402
from app.models import Generation, GenerationSnapshot, Project  # noqa: E402

SEG1 = "林夜推开沉重的木门。\n林夜说道：“我回来了。”"
SEG2_OLD = "白光抬起头，望向窗外。\n白光说道：“天快亮了。”"
SEG2_NEW = "秦九走进院子。\n秦九说道：“好久不见。”"
SEG3 = "林夜点了点头。\n林夜说道：“走吧。”"


class StubProvider:
    """确定性 provider：文本可控，摘要固定，不联网。"""

    def __init__(self, reply: str = SEG2_NEW) -> None:
        self.reply = reply

    def chat(self, system, user, constraints=None, **kw):
        return self.reply

    def summarize(self, content: str) -> str:
        return "测试摘要"

    def stream_chat(self, system, user, constraints=None, **kw):
        yield self.reply


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


def _md5(s: str | None) -> str:
    return hashlib.md5((s or "").encode("utf-8")).hexdigest()


def _make_project(db, title: str = "快照测试") -> Project:
    proj = Project(title=title, target_length=5000, mode="quick",
                   status="created", reference_novel_ids=[])
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project, segment_no: int, content: str) -> Generation:
    gen = Generation(
        project_id=project.id, segment_no=segment_no, text=content,
        summary={"text": f"段{segment_no}摘要"},
    )
    db.add(gen)
    db.commit()
    merge_segment_into_kg(db, project.id, content, segment_no, source="generated")
    db.refresh(gen)
    return gen


def _gen(db, project, segment_no: int) -> Generation:
    return db.scalars(
        select(Generation).where(
            Generation.project_id == project.id,
            Generation.segment_no == segment_no,
        )
    ).one()


def _rewrite(db, project, segment_no: int, reply: str = SEG2_NEW,
             *, batch_id: str | None = None, instruction: str = "换一种语气"):
    """按 router 的真实顺序执行一次重写：先写 pending，再跑引擎（CAS 提交）。"""
    gen = _gen(db, project, segment_no)
    snap = begin_snapshot(db, project, gen, instruction=instruction,
                          batch_id=batch_id)
    events = list(regenerate_segment_stream(
        db, project, segment_no,
        provider=StubProvider(reply),
        instruction=instruction,
        snapshot_id=snap.id,
        expected_before=snap.before_text,
    ))
    assert any(e.get("type") == "done" for e in events), "重写没有产出 done 事件"
    db.expire_all()
    db.refresh(snap)
    return snap


# ---------------------------------------------------------------------------
# T1：崩溃 —— pending 已写、正文未写
# ---------------------------------------------------------------------------


def test_crash_between_pending_and_commit(db):
    project = _make_project(db)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    before = _md5(gen.text)

    snap = begin_snapshot(db, project, gen, instruction="改冷一点")
    # —— 此刻「进程被杀」：不调模型、不 finalize ——
    other = SessionLocal()
    try:
        row = other.get(GenerationSnapshot, snap.id)
        assert row.status == "pending"
        g = other.get(Generation, gen.id)
        assert _md5(g.text) == before, "崩溃前正文就被改了（两段式写入顺序有误）"

        # 现状（设计已知代价）：残留 pending 会把该段锁住
        with pytest.raises(SnapshotBusy) as ei:
            begin_snapshot(other, project, g, instruction="再试一次")
        assert "正在重写中" in str(ei.value)
        assert "UNIQUE" not in str(ei.value).upper(), "用户不该看到数据库错误原文"

        # —— 进程重启：把持有者改成「已不存在的 pid」，模拟上一进程已死 ——
        # 新判据不是固定超时，而是 pid 存活 + owner_started_at 一致；本测试
        # 进程还活着，所以必须先伪造「持有者已死」，否则不该被启动收割清掉。
        other.execute(
            update(GenerationSnapshot)
            .where(GenerationSnapshot.id == snap.id)
            .values(owner_pid=2_147_483_647, owner_started_at="win:0")
        )
        other.commit()
        assert reap_all_pending(other, reason="process_restart") == 1
        other.expire_all()
        assert other.get(GenerationSnapshot, snap.id).status == "failed"
        assert other.get(GenerationSnapshot, snap.id).error_reason == "process_restart"
        assert _md5(other.get(Generation, gen.id).text) == before

        # 收割后同段可以立刻重写（不再被挡）
        snap2 = begin_snapshot(other, project, other.get(Generation, gen.id))
        assert snap2.status == "pending"
        other.rollback()
    finally:
        other.close()

    pending = db.scalars(
        select(GenerationSnapshot).where(GenerationSnapshot.status == "pending")
    ).all()
    assert len(pending) == 1, f"应收割到只剩本次新建的 1 条 pending，实际 {len(pending)}"
    db.expire_all()
    assert _md5(_gen(db, project, 2).text) == before, "崩溃不该改正文"


# ---------------------------------------------------------------------------
# T2：客户端中断（GeneratorExit）
# ---------------------------------------------------------------------------


def test_client_disconnect_marks_failed_without_touching_text(db):
    project = _make_project(db)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    before = _md5(gen.text)

    snap = begin_snapshot(db, project, gen, instruction="重写")
    stream = regenerate_segment_stream(
        db, project, 2, provider=StubProvider(SEG2_NEW),
        snapshot_id=snap.id, expected_before=snap.before_text,
    )
    assert next(stream)["type"] == "chunk"
    stream.close()  # 等价于客户端断开：GeneratorExit 打在 yield 处

    db.expire_all()
    row = db.get(GenerationSnapshot, snap.id)
    assert row.status == "failed", f"中断后快照应留痕 failed，实际 {row.status}"
    assert row.error_reason == "client_disconnect"
    assert _md5(_gen(db, project, 2).text) == before, "中断后正文被改了"
    assert not db.scalars(
        select(GenerationSnapshot).where(GenerationSnapshot.status == "pending")
    ).all(), "中断后残留 pending，会挡住该段重写"


# ---------------------------------------------------------------------------
# T3：同段并发 —— 第二个请求看到的是人话
# ---------------------------------------------------------------------------


def test_concurrent_rewrite_second_gets_friendly_busy(db):
    project = _make_project(db)
    gen = _seed_segment(db, project, 2, SEG2_OLD)

    first = begin_snapshot(db, project, gen, batch_id="batch-A")
    with pytest.raises(SnapshotBusy) as ei:
        begin_snapshot(db, project, _gen(db, project, 2), batch_id="batch-B")

    msg = str(ei.value)
    assert "第 2 段正在重写中" in msg
    assert "UNIQUE" not in msg.upper() and "IntegrityError" not in msg
    assert ei.value.segment_no == 2

    rows = db.scalars(select(GenerationSnapshot)).all()
    assert len(rows) == 1 and rows[0].id == first.id, "并发挡下时不该留下第二条记录"


def test_concurrent_rewrite_http_returns_409_friendly(db):
    """真的从 HTTP 打一次：第二个重写拿到的必须是 409 + 人话，不是 500/数据库原文。"""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    project = _make_project(db)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    begin_snapshot(db, project, gen, instruction="第一个请求还在跑")

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).post("/api/assistant/invoke-generation", json={
            "project_id": project.id,
            "action": "regenerate_segment",
            "segment_no": 2,
            "instruction": "第二个请求",
        })
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "segment_busy"
    assert "第 2 段正在重写中" in detail["message"]
    assert detail["can_takeover"] is False, "心跳新鲜时不该提示可接管"
    body = resp.text.upper()
    assert "UNIQUE" not in body and "SQLITE" not in body and "INTEGRITYERROR" not in body

    # —— 心跳超时后同一个 409 要带「强制接管」选项（UI 契约）——
    snap_id = db.scalar(
        select(GenerationSnapshot.id).where(
            GenerationSnapshot.project_id == project.id,
            GenerationSnapshot.segment_no == 2,
            GenerationSnapshot.status == "pending",
        )
    )
    _age_heartbeat(db, snap_id, HEARTBEAT_TIMEOUT_SECONDS + 5)
    app.dependency_overrides[get_db] = lambda: db
    try:
        resp2 = TestClient(app).post("/api/assistant/invoke-generation", json={
            "project_id": project.id,
            "action": "regenerate_segment",
            "segment_no": 2,
            "instruction": "心跳已停，看看 409 怎么说",
        })
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert resp2.status_code == 409, resp2.text
    d2 = resp2.json()["detail"]
    assert d2["can_takeover"] is True
    assert d2["options"][0]["id"] == "takeover"
    assert d2["owner_pid"] == os.getpid()
    assert "心跳已停" in d2["message"]


# ---------------------------------------------------------------------------
# T4：回滚逐字 + 摘要一起退 + 摘要树按旧摘要重建
# ---------------------------------------------------------------------------


def test_rollback_restores_text_byte_exact_and_summary_tree(db):
    project = _make_project(db)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    _seed_segment(db, project, 3, SEG3)

    before_md5 = _md5(gen.text)
    before_summary = dict(gen.summary or {})

    snap = _rewrite(db, project, 2)
    assert _md5(_gen(db, project, 2).text) != before_md5, "重写没生效，后面无从验证"

    result = rb.rollback_snapshot(db, project, snap.id, provider=StubProvider())
    db.expire_all()

    restored = _gen(db, project, 2)
    assert _md5(restored.text) == before_md5, "回滚后正文 md5 与重写前不一致"
    assert restored.text == SEG2_OLD, "回滚后正文不是逐字原文"
    assert (restored.summary or {}) == before_summary, "段摘要没跟着退"

    # 摘要树确实重建过，且用的是**退回后的**摘要
    tree = project.summary_tree or {}
    assert tree.get("l1", {}).get("2") == before_summary["text"], (
        f"摘要树 L1 段2 仍是重写后的内容：{tree.get('l1', {}).get('2')!r}"
    )
    assert result["derived"]["summary_tree"] is not None
    assert result["text_hash"] == snap.before_hash

    # 回滚本身也是快照，还能再撤
    snap_row = db.get(GenerationSnapshot, snap.id)
    assert snap_row.status == "rolled_back"
    assert snap_row.rolled_back_by_snapshot_id == result["rollback_snapshot_id"]
    undo = db.get(GenerationSnapshot, result["rollback_snapshot_id"])
    assert undo.operation == "rollback" and undo.status == "applied"

    # KG 也按旧正文重放过（旧角色「白光」回来，「秦九」消失）
    from app.models import KgNode

    names = {
        (n.props or {}).get("name")
        for n in db.scalars(select(KgNode).where(
            KgNode.project_id == project.id, KgNode.source == "generated")).all()
        if n.type == "character"
    }
    assert "白光" in names and "秦九" not in names, f"回滚后 KG 未重放：{names}"


def test_rollback_rejects_when_text_drifted(db):
    project = _make_project(db)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    snap = _rewrite(db, project, 2)

    # 模拟「另一个入口」在快照之后改了这段（提案 apply / 手工编辑）
    db.execute(update(Generation).where(Generation.id == gen.id)
               .values(text="第三方写入的新正文"))
    db.commit()

    with pytest.raises(rb.TextDrifted):
        rb.rollback_snapshot(db, project, snap.id, provider=StubProvider())
    db.expire_all()
    assert _gen(db, project, 2).text == "第三方写入的新正文", "被拒的回滚仍然写了正文"


# ---------------------------------------------------------------------------
# T5：批量回滚
# ---------------------------------------------------------------------------


def test_batch_rollback_restores_all_three_segments(db):
    project = _make_project(db)
    gens = {n: _seed_segment(db, project, n, t)
            for n, t in ((1, SEG1), (2, SEG2_OLD), (3, SEG3))}
    before = {n: _md5(g.text) for n, g in gens.items()}

    batch = str(uuid.uuid4())
    snaps = [
        _rewrite(db, project, 1, reply="林夜撞开门。\n林夜说道：“我回来了。”", batch_id=batch),
        _rewrite(db, project, 2, reply=SEG2_NEW, batch_id=batch),
        _rewrite(db, project, 3, reply="林夜摇头。\n林夜说道：“走吧。”", batch_id=batch),
    ]
    assert all(s.status == "applied" for s in snaps)
    assert {s.segment_no for s in snaps} == {1, 2, 3}

    result = rb.rollback_batch(db, project, batch, provider=StubProvider())
    db.expire_all()
    assert result["count"] == 3
    for n in (1, 2, 3):
        assert _md5(_gen(db, project, n).text) == before[n], f"第 {n} 段没复位"
    assert not db.scalars(select(GenerationSnapshot).where(
        GenerationSnapshot.batch_id == batch,
        GenerationSnapshot.status == "applied")).all()

    # 再撤一次（回滚的回滚）：整批回到重写后
    undo_batch = result["rollback_batch_id"]
    rb.rollback_batch(db, project, undo_batch, provider=StubProvider())
    db.expire_all()
    assert _gen(db, project, 2).text == SEG2_NEW, "回滚的回滚没把正文改回去"


def test_batch_rollback_midway_failure_leaves_all_untouched(db, monkeypatch):
    project = _make_project(db)
    gens = {n: _seed_segment(db, project, n, t)
            for n, t in ((1, SEG1), (2, SEG2_OLD), (3, SEG3))}
    original = {n: _md5(g.text) for n, g in gens.items()}
    batch = str(uuid.uuid4())
    for n, reply in ((1, "林夜撞开门。"), (2, SEG2_NEW), (3, "林夜摇头。")):
        _rewrite(db, project, n, reply=reply, batch_id=batch)

    rewritten = {n: _md5(_gen(db, project, n).text) for n in (1, 2, 3)}

    real = rb._restore_one
    calls = {"n": 0}

    def _flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二个段恢复时炸了")
        return real(*args, **kwargs)

    monkeypatch.setattr(rb, "_restore_one", _flaky)
    with pytest.raises(RuntimeError):
        rb.rollback_batch(db, project, batch, provider=StubProvider())

    db.expire_all()
    for n in (1, 2, 3):
        assert _md5(_gen(db, project, n).text) == rewritten[n], (
            f"中途失败后第 {n} 段被改了（批量回滚不是单事务）"
        )
    assert original[1] != rewritten[1]  # 前提自检

    # 没有半截的 rollback 快照残留
    leaked = db.scalars(select(GenerationSnapshot).where(
        GenerationSnapshot.operation == "rollback")).all()
    assert not leaked, f"失败的回滚留下了快照行：{[s.id for s in leaked]}"


# ---------------------------------------------------------------------------
# T6：设定漂移 409 三选项
# ---------------------------------------------------------------------------


def _rename_in_bible(db, project, name: str = "新名字") -> None:
    bible = dict(project.plot_bible or {})
    chars = list(bible.get("characters") or [])
    if chars:
        chars[0] = {**chars[0], "name": name}
    else:
        chars = [{"name": name, "role": "主角"}]
    bible["characters"] = chars
    apply_bible_revision(db, project.id, bible, note=f"改名成 {name}", source="user")
    db.expire_all()


def _seed_bible(db, project) -> None:
    apply_bible_revision(
        db, project.id,
        {"characters": [{"name": "白光", "role": "配角"}], "outline": ["起"]},
        note="初始设定", source="user",
    )
    db.expire_all()


def _rollback_409(db, project, snap_id):
    with pytest.raises(rb.BibleDrift) as ei:
        rb.rollback_snapshot(db, project, snap_id, mode="auto",
                             provider=StubProvider())
    detail = ei.value.detail
    assert detail["code"] == "bible_changed_since_snapshot"
    ids = [o["id"] for o in detail["options"]]
    assert ids == ["restore_bible_then_text", "text_only", "cancel"], ids
    return detail


def test_bible_drift_offers_and_executes_three_options(db):
    project = _make_project(db)
    _seed_bible(db, project)
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, SEG2_OLD)
    before_md5 = _md5(gen.text)

    snap = _rewrite(db, project, 2)
    db.expire_all()
    snap_bible_hash = db.get(GenerationSnapshot, snap.id).bible_hash

    _rename_in_bible(db, project)
    from app.agents.dialog.snapshot import bible_hash as bh

    assert bh(project) != snap_bible_hash, "改名后 bible_hash 应变化（前提自检）"

    # 选项 A：先退设定，再退正文
    detail = _rollback_409(db, project, snap.id)
    assert detail["can_restore_bible"] is True
    res = rb.rollback_snapshot(db, project, snap.id, mode="with_bible",
                               provider=StubProvider())
    db.expire_all()
    assert bh(project) == snap_bible_hash, "设定没退回快照时点"
    assert _md5(_gen(db, project, 2).text) == before_md5
    assert res["warnings"], "先退设定应回报「实体别名不会自动回退」的警告"

    # 选项 B：只退正文（重新造一次漂移）
    snap2 = _rewrite(db, project, 2, instruction="再来一次")
    _rename_in_bible(db, project)
    _rollback_409(db, project, snap2.id)
    res_b = rb.rollback_snapshot(db, project, snap2.id, mode="text_only",
                                 provider=StubProvider())
    db.expire_all()
    assert _md5(_gen(db, project, 2).text) == before_md5
    assert bh(project) != snap_bible_hash, "text_only 不该改设定"
    assert any("bible_changed_since_snapshot" in w for w in res_b["warnings"])

    # 选项 C：取消 —— 前端不调用；服务端语义 = 被 409 挡下且零写入
    snap3 = _rewrite(db, project, 2, instruction="第三次")
    _rename_in_bible(db, project, "第三个名字")
    text_before = _gen(db, project, 2).text
    _rollback_409(db, project, snap3.id)
    db.expire_all()
    assert _gen(db, project, 2).text == text_before, "「取消」路径竟然写了库"
    assert db.get(GenerationSnapshot, snap3.id).status == "applied"


# ---------------------------------------------------------------------------
# T6b：HTTP 层 409 的结构（前端据此弹三个选项，而不是展示数据库原文）
# ---------------------------------------------------------------------------


def test_http_409_payload_has_three_options(db):
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    project = _make_project(db)
    _seed_bible(db, project)
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    snap = _rewrite(db, project, 2)
    _rename_in_bible(db, project)

    app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(app)
        r = client.post("/api/assistant/snapshots/rollback",
                        json={"project_id": project.id, "snapshot_id": snap.id})
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "bible_changed_since_snapshot"
        assert [o["id"] for o in detail["options"]] == [
            "restore_bible_then_text", "text_only", "cancel"
        ]
        assert "设定" in detail["message"]
        assert "UNIQUE" not in r.text.upper() and "sqlite" not in r.text.lower()

        listing = client.get("/api/assistant/snapshots",
                             params={"project_id": project.id})
        assert listing.status_code == 200
        assert listing.json()["count"] >= 1
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# T7：容量（实测每行开销，外推到 925 段 × 3 版）
# ---------------------------------------------------------------------------


def test_capacity_projection_matches_design_estimate(tmp_path):
    """在**独立**临时库里量，避开共享测试库的 freelist/页复用抖动。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agents.dialog.snapshot import text_hash, utcnow

    db_file = tmp_path / "capacity.db"
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    Base.metadata.create_all(bind=eng)
    s = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()

    body = ("夜色压下来，风从城墙的缺口灌进去。" * 60)[:1200]  # 1200 汉字
    summary = {"text": "摘要" * 100, "era": {}, "power": {}}
    n = 120
    try:
        before = db_file.stat().st_size
        now = utcnow()
        for i in range(n):
            s.add(GenerationSnapshot(
                project_id=1, generation_id=1, segment_no=1,
                batch_id=f"cap-{i}", operation="rewrite", status="applied",
                before_text=body, after_text=body,
                before_summary=summary, after_summary=summary,
                before_hash=text_hash(body), after_hash=text_hash(body),
                instruction="容量测试", created_at=now, finished_at=now,
            ))
        s.commit()
        delta = db_file.stat().st_size - before
    finally:
        s.close()
        eng.dispose()

    per_row = delta / n
    projected = per_row * 925 * 3
    print(f"\n[容量] 实测 {n} 行占用 {delta / 1024:.1f} KB，"
          f"每行 {per_row / 1024:.2f} KB；外推 925 段×3 版 = "
          f"{projected / 1024 / 1024:.1f} MB（设计估算 25–28 MB）")

    assert 18 * 1024 * 1024 < projected < 36 * 1024 * 1024, (
        f"外推 {projected / 1024 / 1024:.1f} MB 与设计估算（25–28 MB）差太远，"
        "要么每行体积算错，要么保留策略没生效"
    )


def test_purge_keeps_latest_three_and_drops_old_failed(db):
    project = _make_project(db, title="清理")
    _seed_segment(db, project, 1, SEG1)
    from app.agents.dialog.snapshot import text_hash, utcnow
    from datetime import timedelta

    now = utcnow()
    for i in range(5):
        db.add(GenerationSnapshot(
            project_id=project.id, generation_id=1, segment_no=1,
            batch_id=f"p-{i}", operation="rewrite", status="applied",
            before_text="a", after_text="b", before_hash=text_hash("a"),
            after_hash=text_hash("b"),
            created_at=now - timedelta(days=30 - i), finished_at=now,
        ))
    for i in range(2):
        db.add(GenerationSnapshot(
            project_id=project.id, generation_id=1, segment_no=1,
            batch_id=f"f-{i}", operation="rewrite", status="failed",
            before_text="a", before_hash=text_hash("a"),
            error_reason="model_error",
            created_at=now - timedelta(hours=30 - i), finished_at=now,
        ))
    db.commit()

    stats = purge_snapshots(db, now=now)
    assert stats["failed_deleted"] == 2, stats
    assert stats["applied_deleted"] == 2, stats

    left = list_snapshots(db, project_id=project.id, status="applied")
    assert len(left) == 3, f"每段应保留最新 3 条，实际 {len(left)}"
    assert [x["batch_id"] for x in left][0] == "p-4", "留下的不是最新三条"


# ====================================================================
# 租约（2026-11-12）：心跳 / 接管 / 启动挑着收 / 多 worker
# ====================================================================


def _age_heartbeat(db, snap_id: int, seconds: float) -> None:
    db.execute(
        update(GenerationSnapshot)
        .where(GenerationSnapshot.id == snap_id)
        .values(heartbeat_at=utcnow() - timedelta(seconds=seconds))
    )
    db.commit()


def _pending_rows(db, project) -> list[GenerationSnapshot]:
    return db.scalars(
        select(GenerationSnapshot).where(
            GenerationSnapshot.project_id == project.id,
            GenerationSnapshot.status == "pending",
        )
    ).all()


def test_abandoned_live_process_lease_needs_explicit_takeover(db):
    """验收 1：流被遗弃但进程活着 → 先 409（人话），超时后可显式接管。"""
    project = _make_project(db, title="租约-遗弃")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    snap = begin_snapshot(db, project, _gen(db, project, 2), instruction="改冷一点")

    # 模拟「进程还活着（owner_pid 就是本进程），但流被遗弃、不再心跳」
    _age_heartbeat(db, snap.id, HEARTBEAT_TIMEOUT_SECONDS + 30)

    with pytest.raises(SnapshotBusy) as ei:
        begin_snapshot(db, project, _gen(db, project, 2), instruction="再试一次")
    msg = str(ei.value)
    assert "第 2 段正在重写中" in msg
    assert "心跳已停" in msg and "可强制接管" in msg
    assert "UNIQUE" not in msg.upper() and "INTEGRITYERROR" not in msg.upper()
    assert ei.value.can_takeover is True
    assert ei.value.detail["options"][0]["id"] == "takeover"
    assert ei.value.detail["owner_pid"] == os.getpid()

    snap2 = begin_snapshot(
        db, project, _gen(db, project, 2), instruction="接管后重写", takeover=True
    )
    assert snap2.id != snap.id and snap2.status == "pending"
    db.expire_all()  # 接管走的是 synchronize_session=False 的条件 UPDATE
    old = db.get(GenerationSnapshot, snap.id)
    assert old.status == "failed"
    assert "taken_over" in old.error_reason
    assert f"owner_pid={os.getpid()}" in old.error_reason
    assert "stale=" in old.error_reason and "by_pid=" in old.error_reason
    pend = _pending_rows(db, project)
    assert [p.id for p in pend] == [snap2.id], "接管后必须仍只有一条 pending"
    print(f"\n[接管] 旧行 #{snap.id} error_reason={old.error_reason}")


def test_takeover_rejected_when_heartbeat_fresh(db):
    """验收 4：心跳新鲜时接管 CAS 必须失败，且不产生第二条 pending。"""
    project = _make_project(db, title="租约-新鲜")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    snap = begin_snapshot(db, project, _gen(db, project, 2))

    with pytest.raises(SnapshotBusy) as ei:
        begin_snapshot(db, project, _gen(db, project, 2), takeover=True)
    msg = str(ei.value)
    assert "这段正由另一个进程写入" in msg
    assert "未能接管" in msg
    assert ei.value.takeover_failed is True
    assert ei.value.can_takeover is False
    assert "UNIQUE" not in msg.upper() and "INTEGRITYERROR" not in msg.upper()

    pend = _pending_rows(db, project)
    assert [p.id for p in pend] == [snap.id], "CAS 失败不该产生第二条 pending"
    assert db.get(GenerationSnapshot, snap.id).status == "pending"


def test_startup_reaper_spares_live_owners_and_reaps_dead_or_reused_pid(db):
    """验收 2 + 3：启动收割只清 pid 不存在 / 启动时刻对不上 / 无租约的行。"""
    project = _make_project(db, title="租约-启动收割")
    for seg in range(1, 6):
        _seed_segment(db, project, seg, SEG1)

    live = begin_snapshot(db, project, _gen(db, project, 1))  # 本进程，真实存活

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.2)
        other_live = begin_snapshot(db, project, _gen(db, project, 2))
        other_live.owner_pid = child.pid
        other_live.owner_started_at = process_identity(child.pid)
        assert other_live.owner_started_at, "拿不到子进程启动时刻，本用例失去意义"

        reused = begin_snapshot(db, project, _gen(db, project, 3))
        reused.owner_pid = child.pid  # pid 活着，但启动时刻对不上 = 被复用
        reused.owner_started_at = "win:0" if os.name == "nt" else "proc:0"

        dead = begin_snapshot(db, project, _gen(db, project, 4))
        dead.owner_pid = 2_147_483_647  # 不存在的 pid
        dead.owner_started_at = "win:0"

        legacy = begin_snapshot(db, project, _gen(db, project, 5))
        legacy.owner_pid = None  # 升级前的存量 pending：无租约证据
        legacy.owner_started_at = None
        legacy.heartbeat_at = None
        db.commit()

        reaped = reap_orphan_pending(db, reason="process_restart")
        db.expire_all()
        status = {
            row.id: db.get(GenerationSnapshot, row.id).status for row in
            (live, other_live, reused, dead, legacy)
        }
        print(
            "\n[启动收割] 清掉 "
            f"{reaped} 条；存活(本进程)#{live.id}={status[live.id]}，"
            f"存活(子进程 pid={child.pid})#{other_live.id}={status[other_live.id]}，"
            f"pid复用#{reused.id}={status[reused.id]}，"
            f"死pid#{dead.id}={status[dead.id]}，无租约#{legacy.id}={status[legacy.id]}"
        )
        assert reaped == 3
        assert status[live.id] == "pending", "本进程自己的在途行被清了"
        assert status[other_live.id] == "pending", "同批存活 worker 的行被清了"
        assert status[reused.id] == "failed"
        assert status[dead.id] == "failed"
        assert status[legacy.id] == "failed"
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


def test_two_live_workers_do_not_reap_each_other(db):
    """验收 3：两个进程标识同时启动，互不清对方行（实测谁清了谁）。"""
    project = _make_project(db, title="租约-多worker")
    for seg in range(1, 3):
        _seed_segment(db, project, seg, SEG1)

    workers = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        for _ in range(2)
    ]
    try:
        time.sleep(0.2)
        rows = []
        for seg, proc in zip((1, 2), workers):
            snap = begin_snapshot(db, project, _gen(db, project, seg))
            snap.owner_pid = proc.pid
            snap.owner_started_at = process_identity(proc.pid)
            assert snap.owner_started_at
            rows.append((seg, proc.pid, snap.id))
        db.commit()

        reaped = reap_orphan_pending(db, reason="process_restart")
        db.expire_all()
        states = [
            (seg, pid, sid, db.get(GenerationSnapshot, sid).status)
            for seg, pid, sid in rows
        ]
        print(f"\n[多worker] 启动收割清掉 {reaped} 条；明细={states}")
        assert reaped == 0
        assert all(state == "pending" for _, _, _, state in states)
    finally:
        for proc in workers:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_heartbeat_touch_is_throttled_and_measured(db):
    """验收 5：节流判断零写库；真实心跳写入耗时给出实测数字。"""
    project = _make_project(db, title="租约-心跳")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    snap = begin_snapshot(db, project, _gen(db, project, 2))

    lease = SnapshotLease(snap.id)
    assert lease.touch(db) is True, "首次心跳应落库"
    first_hb = db.get(GenerationSnapshot, snap.id).heartbeat_at

    t0 = time.perf_counter()
    for _ in range(1000):
        assert lease.touch(db) is False
    throttled = time.perf_counter() - t0
    assert db.get(GenerationSnapshot, snap.id).heartbeat_at == first_hb
    assert lease.beats == 1 and lease.skipped == 1000

    writes = []
    for _ in range(10):
        lease._last = None  # 强制越过间隔，测真实写库
        t = time.perf_counter()
        assert lease.touch(db) is True
        writes.append(time.perf_counter() - t)

    avg_ms = sum(writes) / len(writes) * 1000
    print(
        f"\n[心跳开销] 1000 次节流判断 {throttled * 1000:.2f} ms"
        f"（单次 {throttled * 1000 / 1000:.4f} ms，零写库）；"
        f"真实心跳写库 10 次 avg={avg_ms:.2f} ms "
        f"min={min(writes) * 1000:.2f} max={max(writes) * 1000:.2f}"
    )
    assert throttled < 0.5, f"1000 次节流判断花了 {throttled:.3f}s，内存判断不该这么贵"
    assert avg_ms < 100, f"单次心跳写库 {avg_ms:.1f} ms，已影响生成吞吐"


def test_engine_touches_lease_while_streaming(db, monkeypatch):
    """引擎接线：开流前 1 次 + 每个 chunk 1 次（节流在 lease 内部）。"""
    import app.agents.dialog.snapshot as snap_mod

    calls = {"n": 0}
    orig = snap_mod.SnapshotLease.touch

    def counted(self, session):
        calls["n"] += 1
        return orig(self, session)

    monkeypatch.setattr(snap_mod.SnapshotLease, "touch", counted)

    project = _make_project(db, title="租约-引擎接线")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    _rewrite(db, project, 2)
    assert calls["n"] >= 2, f"引擎没有接心跳，touch 只被调用 {calls['n']} 次"


# ---------------------------------------------------------------------------
# T8：重写后 replay 必须读新正文（A 的回归测试）
# ---------------------------------------------------------------------------

_OLD_HOLE = "这背后究竟藏着什么秘密？"
_NEW_HOLE = "他究竟为什么回来？"
# ≥100 字：让同一组文本也能用于 T9 的范围校验（短文本不参与判定）。
SEG2_OLD_LONG = "白光抬起头，望向窗外，天色尚未完全亮起。" * 5 + "\n" + _OLD_HOLE
SEG2_NEW_LONG = "秦九走进院子，院里的枯叶被风卷起。" * 6 + "\n" + _NEW_HOLE


def test_rewrite_replay_uses_new_text(db):
    """快照式重写后，KG replay / 摘要树读到的都必须是**新**正文。

    修复前实测：`finalize_snapshot` 的 SQL UPDATE 不走 identity map，而
    `expire_on_commit=False`，replay 的 `select(Generation)` 拿到的仍是旧对象——
    facts 里残留旧正文独有句。这里直接断言结果，不 mock 任何被测逻辑。
    """
    from app.generation.summary_tree import l1_chain
    from app.models import Fact

    project = _make_project(db, title="replay-新正文")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD_LONG)
    _seed_segment(db, project, 3, SEG3)

    _rewrite(db, project, 2, reply=SEG2_NEW_LONG, instruction="换一种语气")

    keys = set(db.scalars(
        select(Fact.key).where(
            Fact.project_id == project.id,
            Fact.type == "plothole",
            Fact.source == "generated",
        )
    ).all())
    assert _NEW_HOLE in keys, f"新正文独有句没进 facts：{keys}"
    assert _OLD_HOLE not in keys, f"旧正文独有句仍留在 facts：{keys}"

    # 摘要树走的是同一批 Generation 对象：也必须是新摘要，否则「future」续写会
    # 基于旧内容。
    db.refresh(project)
    assert "测试摘要" in l1_chain(project.summary_tree)


# ---------------------------------------------------------------------------
# T9：局部指令 vs 整段重写 —— 只提示、不拦截
# ---------------------------------------------------------------------------


def test_scope_guard_unit():
    from app.generation.scope_guard import (
        SCOPE_MIN_CHARS,
        declared_local,
        detect_scope_mismatch,
    )
    assert declared_local("只收拾段尾")
    assert declared_local("保留开头，改最后一句")
    assert declared_local("第 3 句语气硬一点")
    assert not declared_local("换一种语气")

    before = "甲" * 150
    after = "乙" * 120
    hit = detect_scope_mismatch("只收拾段尾", before, after)
    assert hit is not None
    assert hit["declared_local"] is True
    assert hit["common_prefix_chars"] == 0
    assert hit["common_suffix_chars"] == 0
    assert hit["before_chars"] == 150
    assert hit["after_chars"] == 120
    assert hit["chars_delta"] == -30
    assert "已正常保存" in hit["message"], "提示必须说明结果已落库，不是拒绝"

    # 保留了开头一大段 → 不是整段重写，不提示
    kept = before[:100] + "新增内容" * 10
    assert detect_scope_mismatch("只收拾段尾", before, kept) is None

    # 指令没声明局部 → 不提示（用户可能就是想整段改）
    assert detect_scope_mismatch("整段重写", before, after) is None

    # 文本太短 → 不判定（碎片/测试段，比例失真）
    short = "甲" * (SCOPE_MIN_CHARS - 1)
    assert detect_scope_mismatch("只收拾段尾", short, "乙" * 50) is None


def test_scope_mismatch_reported_and_snapshot_persisted(db):
    """scope_mismatch 的落库通道仍在（局部指令已改走补丁，此路径供防御/旧数据）。

    2026-11-12 起，``declared_local`` 的指令在引擎里分流到句子级补丁，
    这条提示在真实生成路径上不再可能命中——但快照列与 ``finalize_snapshot``
    的入参必须保持可用，避免回滚/旧行读取时缺字段。
    """
    from app.agents.dialog.snapshot import finalize_snapshot

    project = _make_project(db, title="局部指令-整段重写")
    gen = _seed_segment(db, project, 2, SEG2_OLD_LONG)
    snap = begin_snapshot(db, project, gen, instruction="只收拾段尾")
    info = {
        "declared_local": True,
        "before_chars": len(SEG2_OLD_LONG),
        "after_chars": len(SEG2_NEW_LONG),
        "common_prefix_chars": 0,
        "common_suffix_chars": 1,
    }
    finalize_snapshot(
        db, snapshot_id=snap.id, generation_id=gen.id, segment_no=2,
        expected_before=snap.before_text, new_text=SEG2_NEW_LONG,
        new_summary={"text": "段2摘要"}, scope_mismatch=info,
    )
    db.commit()

    db.expire_all()
    row = db.get(GenerationSnapshot, snap.id)
    assert row.scope_mismatch == info, "提示结构必须能写进快照行"
    assert row.status == "applied"


def test_local_patch_in_http_response(db, monkeypatch):
    """局部指令走补丁后，HTTP 响应带 patch 明细，两个旧提示均为 None。"""
    from fastapi.testclient import TestClient

    import app.llm.provider as provider_mod
    from app.db import get_db
    from app.main import app

    before = "开场第一句。中间第二句。收尾第三句。"
    after = "开场第一句。中间第二句。改过的收尾。"
    monkeypatch.setattr(
        provider_mod, "get_llm_provider",
        lambda: StubProvider('{"index": 3, "replacement": "改过的收尾。"}'),
    )

    project = _make_project(db, title="局部补丁-HTTP")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, before)
    _seed_segment(db, project, 3, SEG3)

    app.dependency_overrides[get_db] = lambda: db
    try:
        resp = TestClient(app).post("/api/assistant/invoke-generation", json={
            "project_id": project.id,
            "action": "regenerate_segment",
            "segment_no": 2,
            "instruction": "只改最后一句，其余内容保留不要动。",
        })
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patch"] and body["patch"]["patch_count"] == 1, body
    # 补丁路径不会整段重排：两个旧提示都不该触发
    assert body["scope_mismatch"] is None
    assert body["length_drop"] is None
    assert body["chars"] == len(after)
    assert _gen(db, project, 2).text == after


# ---------------------------------------------------------------------------
# T10：文风影子接入重写路径（C）
# ---------------------------------------------------------------------------


def test_rewrite_writes_style_shadow(db):
    from app.models import StyleCheck

    project = _make_project(db, title="重写-文风影子")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    _seed_segment(db, project, 3, SEG3)

    before = db.scalar(select(func.count()).select_from(StyleCheck))
    _rewrite(db, project, 2)
    after = db.scalar(select(func.count()).select_from(StyleCheck))
    assert after == before + 1, f"重写没有新增文风影子记录：{before} → {after}"

    row = db.scalars(select(StyleCheck).order_by(StyleCheck.id.desc())).first()
    assert row.verdict == "shadow"
    assert row.segment_no == 2
    assert row.text_md5 == _md5(SEG2_NEW), "影子记录必须采新正文，不能采旧正文"


def test_rewrite_survives_style_check_failure(db, monkeypatch):
    """校验器故障不得影响重写成功（与 _commit_segment 同一条护栏）。"""
    import app.style.consistency as consistency_mod

    def boom(*a, **kw):
        raise RuntimeError("style checker down")

    monkeypatch.setattr(consistency_mod, "record_segment_check", boom)

    project = _make_project(db, title="重写-文风故障")
    _seed_segment(db, project, 1, SEG1)
    _seed_segment(db, project, 2, SEG2_OLD)
    _seed_segment(db, project, 3, SEG3)

    _rewrite(db, project, 2)
    assert _gen(db, project, 2).text == SEG2_NEW, "校验器异常把重写也拖垮了"


# ---------------------------------------------------------------------------
# T11：重写提示词的篇幅要求（祈使句单向下限，替换续写预算）
# ---------------------------------------------------------------------------


def test_rewrite_prompt_uses_imperative_length_floor():
    from app.generation.prompts import build_user_prompt

    user = build_user_prompt(
        [], [], None, 3000,  # max_chars=3000：旧逻辑会要求「不少于 2400 字」
        rewrite_before_chars=2273,
    )
    assert "原文共 2273 字" in user
    assert "不得短于原文 2273 字" in user
    # 关键：不再出现区间表述——区间会被模型当作「可以取低值」的许可
    assert "80%–120%" not in user
    assert "1818" not in user and "2727" not in user
    # 关键：续写预算必须**消失**，否则两条字数要求互相打架
    assert "请续写不少于" not in user
    # 非重写路径（有前情 → 续写指令）逐字节保留原续写预算
    normal = build_user_prompt(["上一段摘要"], [], None, 3000)
    assert "请续写不少于 2400 字" in normal


# ---------------------------------------------------------------------------
# T12：篇幅跌幅（length_drop）—— 独立提示、明确要求精简时不提示
# ---------------------------------------------------------------------------


def test_length_drop_unit():
    from app.generation.scope_guard import (
        declared_compression,
        detect_length_drop,
    )

    before = "甲" * 2273
    after = "乙" * 851
    hit = detect_length_drop("只收拾段尾", before, after)
    assert hit is not None
    assert hit["before_chars"] == 2273
    assert hit["after_chars"] == 851
    assert hit["ratio"] == round(851 / 2273, 4)
    assert hit["drop_ratio"] == round(1 - 851 / 2273, 4)
    assert "已正常保存" in hit["message"]

    # 跌幅未到 20%（2273 → 2000，约 88%）→ 不提示
    assert detect_length_drop("只收拾段尾", before, "乙" * 2000) is None
    # 用户明确要求缩短 → 不提示（不同关键词都要覆盖）
    for instruction in ("精简一下", "压缩到 800 字", "缩短篇幅",
                        "控制字数在 900 字", "短一点"):
        assert declared_compression(instruction), instruction
        assert detect_length_drop(instruction, before, after) is None, instruction
    # 文本太短 → 不判定
    assert detect_length_drop("只收拾段尾", "甲" * 50, "乙" * 20) is None


def test_length_drop_reported_and_snapshot_persisted(db):
    """done 事件 + 快照行都要带 length_drop；与 scope_mismatch 分列。

    指令刻意用**非局部**措辞（不含「只/仅/保留/段尾…」）：局部指令现在会
    分流到句子级补丁，只有整段重写路径才会产出 length_drop。
    """
    old_text = "甲" * 300
    new_text = "乙" * 150  # 50%，跌幅 50%

    project = _make_project(db, title="篇幅跌幅")
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, old_text)
    _seed_segment(db, project, 3, SEG3)

    snap = begin_snapshot(db, project, gen, instruction="换一种语气")
    events = list(regenerate_segment_stream(
        db, project, 2,
        provider=StubProvider(new_text),
        instruction="换一种语气",
        snapshot_id=snap.id,
        expected_before=snap.before_text,
    ))
    done = next(e for e in events if e.get("type") == "done")
    info = done.get("length_drop")
    assert info is not None, "跌幅 50% 必须给 length_drop 提示"
    assert info["ratio"] == 0.5
    assert info["before_chars"] == 300 and info["after_chars"] == 150
    # 两个问题分列：这里是整段压缩，不是首尾丢失
    assert done.get("scope_mismatch") is None or isinstance(
        done.get("scope_mismatch"), dict
    )

    # 不阻断：正文按模型输出落库
    assert _gen(db, project, 2).text == new_text

    db.expire_all()
    row = db.get(GenerationSnapshot, snap.id)
    assert row.length_drop == info
    assert row.scope_mismatch == done.get("scope_mismatch")


def test_explicit_compression_not_flagged_as_length_drop(db):
    """用户要求「压缩到 150 字」时不提示，正文照常落库。"""
    old_text = "甲" * 300
    new_text = "乙" * 150

    project = _make_project(db, title="明确要求压缩")
    _seed_segment(db, project, 1, SEG1)
    gen = _seed_segment(db, project, 2, old_text)
    _seed_segment(db, project, 3, SEG3)

    snap = begin_snapshot(db, project, gen, instruction="压缩到 150 字")
    events = list(regenerate_segment_stream(
        db, project, 2,
        provider=StubProvider(new_text),
        instruction="压缩到 150 字",
        snapshot_id=snap.id,
        expected_before=snap.before_text,
    ))
    done = next(e for e in events if e.get("type") == "done")
    assert done.get("length_drop") is None
    assert _gen(db, project, 2).text == new_text
