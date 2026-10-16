"""重生成路径的 KG 簿记：清空 + 全量 replay（先红后绿）。

## 缺陷（docs/architecture/2026-10-17-kg-regenerate-replay.md §2）

1. **重生成零簿记**：`regenerate_segment_stream`（engine）替换目标段后只调
   `rebuild_summary_tree`，从不触碰 KG。旧正文抽出的角色/坑/关系/别名/哨兵状态
   永久残留，新正文的事实永不入账 → reminders、坑结算、诊断全部与正文漂移。
2. **`build-kg-from-generations` 半份 + 段号丢失**：只做图谱一步（账本/别名/哨兵
   全不更新），且 chapter 落的是 **chunk 序号而非真实段号**。

## 修复

- 新增 `app.kg.build.rebuild_generated_kg`：per-project RLock 内，按 segment_no
  升序读全部 Generation → 清 generated 侧图谱与账本 → 逐段 `merge_segment_into_kg`。
- `regenerate_segment_stream`：正文落库（`db.commit()`）后调 rebuild，`done` 事件带 `kg`。
- `wizard.build_kg_from_generations` 改走 rebuild。

## 本文件（T1–T6，对应设计文档 §7）

  T1 重生成第 2 段（新文本不含旧角色）→ 旧角色从 KgNode/Fact(generated) 消失
  T2 重生成后哨兵状态按新文本重建（旧 dead 不残留）
  T3 replay 后 KgNode.first_chapter/last_chapter == 真实段号（非 chunk 序号）
  T4 幂等：连续两次 rebuild，节点/Fact 计数与内容一致
  T5 `build-kg-from-generations` 端点走 rebuild → Fact 与图谱同源、段号正确
  T6 注入 merge 异常 → done 事件仍发出、带 rebuilt:False，正文已落库（不阻断）

测试真实调用 `rebuild_generated_kg` / `regenerate_segment_stream` / 端点，不 mock 被测逻辑。
"""

from __future__ import annotations

import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import regenerate_segment_stream  # noqa: E402
from app.kg.build import merge_segment_into_kg, rebuild_generated_kg  # noqa: E402
from app.kg.sentinel import load_state  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Fact, Generation, KgNode, Project  # noqa: E402


# ---------------------------------------------------------------------------
# 语料（已用 extract_from_text 实测确认形状：每个「X说道：」都产出该角色）
# ---------------------------------------------------------------------------

SEG1 = "林夜推开沉重的木门。\n林夜说道：“我回来了。”"
SEG2_OLD = "白光抬起头，望向窗外。\n白光说道：“天快亮了。”"
SEG3 = "林夜点了点头。\n林夜说道：“走吧。”"
# 重生成第 2 段用的新文本：不含旧角色「白光」，改由「秦九」出场
SEG2_NEW = "秦九走进院子。\n秦九说道：“好久不见。”"

# 哨兵状态用例：段 2 先把「沈砚」写死，再重生成成「活着」的版本
DEATH_SEG1 = "沈砚推开门，缓步走入。\n沈砚说道：“我来了。”"
DEATH_SEG2 = "沈砚死了。"
ALIVE_SEG2 = "沈砚转过身，望向远处的山谷。"


class StubProvider:
    """确定性假 provider：chat / stream_chat 返回同一份文本，summarize 返回固定串。

    不实现 `summarize_rollup`——让 engine 的 `_rollup_fn` 回落到 LLMProvider 的
    确定性兜底（与既有流式簿记测试同一手法，证明不强求 provider 有滚上能力）。
    """

    def __init__(self, text: str = SEG2_NEW) -> None:
        self.text = text

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        yield self.text


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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


def _make_project(db, title: str = "重生成簿记测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料直接放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project, segment_no: int, text: str) -> None:
    """按「生成」的等价效果落库：写 Generation 行 + 走四步段级合并。

    这就是 `_commit_segment` + `_merge_segment_into_graph` 的合并效果，
    让测试的起点与真实生成链路一致（不 mock 任何被测逻辑）。
    """
    db.add(
        Generation(
            project_id=project.id,
            segment_no=segment_no,
            text=text,
            summary={"text": f"段{segment_no}摘要"},
        )
    )
    db.commit()
    merge_segment_into_kg(db, project.id, text, segment_no, source="generated")


def _char_names(nodes: list[KgNode]) -> set[str]:
    return {
        (n.props or {}).get("name")
        for n in nodes
        if n.type == "character" and (n.props or {}).get("name")
    }


def _graph_nodes(db, project_id: int) -> list[KgNode]:
    return list(
        db.scalars(
            select(KgNode).where(
                KgNode.project_id == project_id, KgNode.source == "generated"
            )
        ).all()
    )


def _facts(db, project_id: int) -> list[Fact]:
    return list(
        db.scalars(
            select(Fact).where(
                Fact.project_id == project_id, Fact.source == "generated"
            )
        ).all()
    )


def _node_snapshot(db, project_id: int) -> list[tuple]:
    return sorted(
        (
            n.type,
            (n.props or {}).get("name") or (n.props or {}).get("question") or "",
            n.first_chapter,
            n.last_chapter,
        )
        for n in _graph_nodes(db, project_id)
    )


def _fact_snapshot(db, project_id: int) -> list[tuple]:
    return sorted(
        (
            f.type,
            f.key,
            f.first_segment,
            f.last_segment,
            json.dumps(f.props or {}, sort_keys=True, ensure_ascii=False),
        )
        for f in _facts(db, project_id)
    )


# ---------------------------------------------------------------------------
# T1：重生成第 2 段后，旧角色从 KgNode / Fact(generated) 消失
# ---------------------------------------------------------------------------


def test_regenerate_drops_old_character_from_graph_and_ledger(db_session):
    """生成 3 段 → 重生成第 2 段（新文本不含旧角色「白光」）。

    修复前：重生成路径零簿记 → 白光的 KgNode 与 character Fact 永久残留 → 红。
    """
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, SEG1)
    _seed_segment(db_session, proj, 2, SEG2_OLD)
    _seed_segment(db_session, proj, 3, SEG3)

    db_session.expire_all()
    before = _char_names(_graph_nodes(db_session, proj.id))
    assert "白光" in before, f"种子阶段应已抽出白光，实际 {before}"

    # 重生成第 2 段：stub 让模型返回不含白光的新文本
    events = list(
        regenerate_segment_stream(
            db_session, proj, segment_no=2, provider=StubProvider(SEG2_NEW)
        )
    )
    done = [e for e in events if e.get("type") == "done"]
    assert done, "重生成未产出 done 事件"
    assert done[0].get("kg", {}).get("rebuilt") is True, f"done.kg 异常：{done[0].get('kg')}"

    db_session.expire_all()
    names = _char_names(_graph_nodes(db_session, proj.id))
    assert "白光" not in names, (
        f"重生成后旧角色「白光」仍残留在图谱中（零簿记缺陷）：{names}"
    )

    # 图谱新增的角色「秦九」应同时进入事实账本（同源）
    ledger_names = {f.key for f in _facts(db_session, proj.id) if f.type == "character"}
    assert "秦九" in ledger_names, f"新角色「秦九」未入账本：{ledger_names}"
    assert "白光" not in ledger_names, f"账本残留旧角色「白光」：{ledger_names}"

    # 其它段落的数据不受影响（回归护栏）
    assert "林夜" in names, f"段 1/3 的角色「林夜」被误删：{names}"


# ---------------------------------------------------------------------------
# T2：重生成后哨兵状态按新文本重建（旧 dead 不残留）
# ---------------------------------------------------------------------------


def test_regenerate_rebuilds_sentinel_state(db_session):
    """段 2 先把「沈砚」写死 → 重生成成「活着」的版本 → 旧 dead 状态不残留。

    修复前：`Fact(type=state)` 不随重生成更新，`life:沈砚=dead` 永久残留 → 红。
    """
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, DEATH_SEG1)
    _seed_segment(db_session, proj, 2, DEATH_SEG2)

    db_session.expire_all()
    state = load_state(db_session, proj.id, source="generated")
    assert state.get(("life", "沈砚"), {}).get("value") == "dead", (
        f"种子阶段应记录沈砚已死，实际 {state}"
    )

    # 重生成第 2 段：新文本不再写死沈砚
    events = list(
        regenerate_segment_stream(
            db_session, proj, segment_no=2, provider=StubProvider(ALIVE_SEG2)
        )
    )
    assert any(e.get("type") == "done" for e in events), "重生成未产出 done 事件"

    db_session.expire_all()
    state = load_state(db_session, proj.id, source="generated")
    life = state.get(("life", "沈砚"), {})
    assert life.get("value") != "dead", (
        f"重生成后旧 dead 状态仍残留（哨兵未按新文本重建）：{life}"
    )
    # 角色本身仍在（只是不再有死亡状态）
    ledger_names = {f.key for f in _facts(db_session, proj.id) if f.type == "character"}
    assert "沈砚" in ledger_names, f"角色事实不应被误删：{ledger_names}"


# ---------------------------------------------------------------------------
# T3：replay 段号正确（真实段号，非 chunk 序号）
# ---------------------------------------------------------------------------


def test_rebuild_uses_real_segment_numbers_not_chunk_index(db_session):
    """段号取真实 Generation.segment_no（2 与 5），不是 chunk 序号（1 与 2）。

    修复前：`build_graph_from_text` 走 `_split_text_to_chunks` 自增伪章节号 →
    落成 1/2；且它只建图谱不建账本。
    """
    proj = _make_project(db_session)
    # 段号刻意不连续：若按 chunk 序号会落成 1、2
    _seed_segment(db_session, proj, 2, SEG2_OLD)
    _seed_segment(db_session, proj, 5, SEG1)

    stats = rebuild_generated_kg(db_session, proj.id, source="generated")
    assert stats["rebuilt"] is True
    assert stats["segments"] == 2, f"segments 计数异常：{stats}"

    db_session.expire_all()
    by_name = {
        (n.props or {}).get("name"): n
        for n in _graph_nodes(db_session, proj.id)
        if n.type == "character"
    }
    assert by_name["白光"].first_chapter == 2, (
        f"白光的段号应为真实段号 2，实际 {by_name['白光'].first_chapter}（chunk 序号缺陷）"
    )
    assert by_name["林夜"].first_chapter == 5, (
        f"林夜的段号应为真实段号 5，实际 {by_name['林夜'].first_chapter}（chunk 序号缺陷）"
    )


# ---------------------------------------------------------------------------
# T4：幂等
# ---------------------------------------------------------------------------


def test_rebuild_is_idempotent(db_session):
    """连续两次 rebuild，KgNode / Fact 的计数与内容完全一致。"""
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, SEG1)
    _seed_segment(db_session, proj, 2, SEG2_OLD)
    _seed_segment(db_session, proj, 3, SEG3)

    rebuild_generated_kg(db_session, proj.id, source="generated")
    db_session.expire_all()
    nodes_1 = _node_snapshot(db_session, proj.id)
    facts_1 = _fact_snapshot(db_session, proj.id)

    rebuild_generated_kg(db_session, proj.id, source="generated")
    db_session.expire_all()
    nodes_2 = _node_snapshot(db_session, proj.id)
    facts_2 = _fact_snapshot(db_session, proj.id)

    assert len(nodes_1) == len(nodes_2), (
        f"两次 rebuild 节点数不一致：{len(nodes_1)} vs {len(nodes_2)}"
    )
    assert len(facts_1) == len(facts_2), (
        f"两次 rebuild 事实数不一致：{len(facts_1)} vs {len(facts_2)}"
    )
    assert nodes_1 == nodes_2, "两次 rebuild 图谱内容不一致（非幂等）"
    assert facts_1 == facts_2, "两次 rebuild 事实内容不一致（非幂等）"


# ---------------------------------------------------------------------------
# T5：端点收归 build-kg-from-generations → rebuild
# ---------------------------------------------------------------------------


def test_build_kg_endpoint_uses_rebuild_and_real_segment_numbers(db_session):
    """POST /{project_id}/build-kg-from-generations 走 rebuild → 图谱与账本同源、段号正确。

    修复前：端点走 build_graph_from_text → 只建图谱不提账本，且段号是 chunk 序号。
    """
    proj = _make_project(db_session, title="端点收归")
    # 直接落 Generation 行（不预先建 KG）——端点负责全量重建
    for seg_no, text in ((2, SEG2_OLD), (5, SEG1)):
        db_session.add(
            Generation(
                project_id=proj.id, segment_no=seg_no, text=text,
                summary={"text": f"段{seg_no}摘要"},
            )
        )
    db_session.commit()

    with TestClient(fastapi_app) as client:
        resp = client.post(f"/api/projects/{proj.id}/build-kg-from-generations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("rebuilt") is True, f"端点未走 rebuild：{body}"
    assert body.get("segments") == 2, f"segments 计数异常：{body}"

    db_session.expire_all()
    nodes = _graph_nodes(db_session, proj.id)
    facts = _facts(db_session, proj.id)
    assert nodes, "端点未建出图谱"
    assert facts, "端点未更新事实账本（半份簿记缺陷）"

    # 段号正确（真实段号，非 chunk 序号）
    by_name = {
        (n.props or {}).get("name"): n for n in nodes if n.type == "character"
    }
    assert by_name["白光"].first_chapter == 2
    assert by_name["林夜"].first_chapter == 5

    # 图谱与账本同源：图谱里的角色名都能在账本 character 事实里找到
    graph_names = _char_names(nodes)
    ledger_names = {f.key for f in facts if f.type == "character"}
    assert graph_names <= ledger_names, (
        f"图谱与账本不同源：图谱 {graph_names} vs 账本 {ledger_names}"
    )


# ---------------------------------------------------------------------------
# T6：簿记失败不阻断（done 事件仍发出、正文已落库）
# ---------------------------------------------------------------------------


def test_regenerate_kg_failure_does_not_break_stream(db_session, monkeypatch):
    """注入 rebuild 内部的 merge 异常 → done 事件仍发出、带 rebuilt:False，正文已落库。"""
    import app.kg.build as build_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("重生成 replay 炸了")

    # rebuild_generated_kg 内部按全局名调用 merge_segment_into_kg → 打桩即生效
    monkeypatch.setattr(build_mod, "merge_segment_into_kg", _boom)

    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, SEG1)
    _seed_segment(db_session, proj, 2, SEG2_OLD)

    # 不得抛异常
    events = list(
        regenerate_segment_stream(
            db_session, proj, segment_no=2, provider=StubProvider(SEG2_NEW)
        )
    )
    done = [e for e in events if e.get("type") == "done"]
    assert done, "簿记失败把 done 事件吞了——SSE 会看起来卡死（违反硬约束）"
    kg = done[0].get("kg") or {}
    assert kg.get("rebuilt") is False, f"失败时 done.kg 应带 rebuilt:False，实际 {kg}"
    assert kg.get("reason"), f"失败时应给出原因，实际 {kg}"

    # 正文照常落库（簿记失败不该回滚已写成的段）
    db_session.expire_all()
    gen = db_session.scalars(
        select(Generation).where(
            Generation.project_id == proj.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2_NEW, f"新正文未照常落库，实际 {gen.text!r}"


# ---------------------------------------------------------------------------
# R1–R4：§8.1 失败语义 —— 失败步必须 rollback（不留半成品）+ partial 标记
#
# 缺陷：`_merge_segment_into_kg_locked` 三处 except 只吞异常、不 rollback。
# 而第 4 步 `audit_segment` 结尾有 `db.commit()`（sentinel.py）—— 于是「前一步失败
# 留下的 pending 行」会被哨兵步的 commit **静默持久化**。这条路径在每段生成链路
# （非流式 + 流式）同样存在，不限于 rebuild。
# ---------------------------------------------------------------------------


def _add_generation(db, project, segment_no: int, text: str) -> None:
    """只落 Generation 行（不预先 merge）——供「让 rebuild 自己 replay」的用例。"""
    db.add(
        Generation(
            project_id=project.id, segment_no=segment_no, text=text,
            summary={"text": f"段{segment_no}摘要"},
        )
    )
    db.commit()


def test_ledger_failure_does_not_leak_pending_rows(db_session, monkeypatch):
    """R1（核心回归）：账本步失败留下 pending 行 → 哨兵步 commit 后库里必须没有它。

    修复前：三处 except 缺 db.rollback → 泄漏行被哨兵步的 db.commit() 静默持久化 → 红。
    """
    import app.kg.ledger as ledger

    proj = _make_project(db_session)

    def _leaky_upsert(db, project_id, facts, source="generated"):
        # 先留下一条 pending 行，再抛异常（模拟「写了一半就炸」）
        db.add(
            Fact(
                project_id=project_id, type="character", key="泄漏行", props={},
                aliases=[], source=source, confidence="high",
                first_segment=1, last_segment=1,
            )
        )
        db.flush()
        raise RuntimeError("账本步炸了")

    monkeypatch.setattr(ledger, "upsert_facts", _leaky_upsert)

    st = merge_segment_into_kg(db_session, proj.id, SEG1, 1, source="generated")
    assert st.get("merged") is not False, f"图谱步应成功，实际 {st}"

    # 哨兵步结尾会 db.commit() —— 若账本步未 rollback，泄漏行会被它持久化
    db_session.expire_all()
    leaked = db_session.scalars(
        select(Fact).where(Fact.project_id == proj.id, Fact.key == "泄漏行")
    ).all()
    assert not leaked, (
        "账本步失败留下的 pending 行被哨兵步的 commit 静默持久化（缺 db.rollback）——"
        "生成链路同样受影响"
    )


def test_partial_step_flags_segment_and_rebuild_complete_false(db_session, monkeypatch):
    """R2：账本步（resolve_aliases）抛异常 → 本段 stats 带 partial:True；
    rebuild 计入 failed 且 complete is False（rebuilt 语义不变）。"""
    import app.kg.ledger as ledger

    def _boom(*args, **kwargs):
        raise RuntimeError("别名归一炸了")

    monkeypatch.setattr(ledger, "resolve_aliases", _boom)

    proj = _make_project(db_session)
    _add_generation(db_session, proj, 1, SEG1)
    _add_generation(db_session, proj, 2, SEG2_OLD)

    st = merge_segment_into_kg(db_session, proj.id, SEG3, 3, source="generated")
    assert st.get("partial") is True, f"账本步失败应标 partial:True，实际 {st}"
    assert st.get("merged") is not False, f"图谱步应已成功，实际 {st}"

    stats = rebuild_generated_kg(db_session, proj.id, source="generated")
    assert stats["rebuilt"] is True, "rebuilt 语义不变（= 操作跑过）"
    assert stats["complete"] is False, f"有失败段时 complete 应 False，实际 {stats}"
    assert stats["failed"], "失败段必须记入 failed（否则缺段被当成功）"
    assert {f["segment_no"] for f in stats["failed"]} == {1, 2}, (
        f"失败段号集合不符，实际 {stats['failed']}"
    )


def test_rebuild_complete_true_when_all_segments_succeed(db_session):
    """R3：全段成功 → complete is True、failed == []。"""
    proj = _make_project(db_session)
    _add_generation(db_session, proj, 1, SEG1)
    _add_generation(db_session, proj, 2, SEG2_OLD)

    stats = rebuild_generated_kg(db_session, proj.id, source="generated")

    assert stats["rebuilt"] is True
    assert stats["complete"] is True, f"全成功应 complete:True，实际 {stats}"
    assert stats["failed"] == [], f"全成功 failed 应为空，实际 {stats}"


def test_graph_failure_leaves_no_residual(db_session, monkeypatch):
    """R4：图谱步失败 → merged is False，且 Session 无 pending/dirty 残留。

    修复前：图谱步 except 未 rollback → 泄漏的 KgNode 留在会话里 → 红。
    """
    import app.kg.build as build_mod

    proj = _make_project(db_session)

    def _leaky_graph(db, project_id, result, **kwargs):
        db.add(
            KgNode(
                project_id=project_id, type="character", props={"name": "幽灵"},
                first_chapter=1, last_chapter=1, source="generated",
            )
        )
        db.flush()
        raise RuntimeError("图谱步炸了")

    monkeypatch.setattr(build_mod, "merge_result_into_graph", _leaky_graph)

    st = merge_segment_into_kg(db_session, proj.id, SEG1, 1, source="generated")
    assert st.get("merged") is False, f"图谱步失败应 merged:False，实际 {st}"

    # 不得有 pending / dirty 对象残留
    assert not db_session.new, f"存在未清理的 pending 对象：{list(db_session.new)}"
    assert not db_session.dirty, f"存在未清理的 dirty 对象：{list(db_session.dirty)}"

    db_session.expire_all()
    ghosts = db_session.scalars(
        select(KgNode).where(
            KgNode.project_id == proj.id, KgNode.source == "generated"
        )
    ).all()
    assert not ghosts, f"图谱步失败留下幽灵节点：{[g.props for g in ghosts]}"
