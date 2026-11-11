# -*- coding: utf-8 -*-
"""重写回滚：单段 / 整批 + 派生数据（摘要树 / KG）重建编排。

设计见 ``.trae/documents/rewrite-snapshot-design.md`` §7–§9。三条硬语义：

1. **回滚也是新快照**：恢复前的内容记成一条 ``operation='rollback'`` 的
   ``applied`` 快照，原快照置 ``rolled_back`` 并指回它。于是「撤错了」还能再撤。
2. **正文 + 摘要一起退**：``generations.summary`` 是摘要树重建的输入
   （``engine.rebuild_summary_tree`` 逐段读 ``summary['text']``），只退正文
   会让重建出来的 L2/L3 仍是改写后的内容。
3. **一起退派生数据**：提交后重建摘要树 + 全量 replay KG。代价是慢（KG 是
   O(全段) 且非原子），回报里用 ``kg_complete`` 如实暴露。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update

from app.agents.dialog.snapshot import (
    SnapshotError,
    aware,
    bible_dict_hash,
    bible_hash,
    snapshot_to_dict,
    text_hash,
    utcnow,
)
from app.models import Generation, GenerationSnapshot, KgBibleRevision, Project

log = logging.getLogger(__name__)


class RollbackError(SnapshotError):
    """回滚失败（router 统一转 409 / 404）。"""

    status_code = 409
    code = "rollback_error"

    def __init__(self, message: str, **detail: Any):
        self.message = message
        self.detail: dict[str, Any] = {"code": self.code, "message": message}
        self.detail.update({k: v for k, v in detail.items() if v is not None})
        super().__init__(message)


class SnapshotNotFound(RollbackError):
    status_code = 404
    code = "snapshot_not_found"


class TextDrifted(RollbackError):
    code = "text_drifted"


class BibleDrift(RollbackError):
    """回滚前发现设定变过：给「先退设定 / 只退正文 / 取消」三个选项。"""

    code = "bible_changed_since_snapshot"

    def __init__(self, message: str, **detail: Any):
        super().__init__(message, **detail)
        self.detail["options"] = [
            {"id": "restore_bible_then_text", "label": "先退设定，再回滚正文"},
            {"id": "text_only", "label": "只回滚正文（接受设定不一致）"},
            {"id": "cancel", "label": "取消"},
        ]


class BatchBlocked(RollbackError):
    code = "batch_not_rollbackable"


def _load_snapshot(db, snapshot_id: int) -> GenerationSnapshot:
    snap = db.get(GenerationSnapshot, snapshot_id)
    if snap is None:
        raise SnapshotNotFound(f"快照 #{snapshot_id} 不存在")
    return snap


def _current_text(gen: Generation) -> str:
    return gen.text or ""


def _find_bible_revision(db, project_id: int, target_hash: str | None):
    """找到 plot_bible 内容哈希等于快照时点的 revision（用于「先退设定」）。"""
    if not target_hash:
        return None
    rows = db.scalars(
        select(KgBibleRevision)
        .where(KgBibleRevision.project_id == project_id)
        .order_by(KgBibleRevision.revision_no.desc())
    ).all()
    for rev in rows:
        if bible_dict_hash(rev.bible_json) == target_hash:
            return rev
    return None


def _restore_one(
    db,
    *,
    snap: GenerationSnapshot,
    gen: Generation,
    current_text: str,
    rollback_batch_id: str,
    now: datetime,
    current_bible_hash: str | None,
) -> GenerationSnapshot:
    """单段恢复（调用方负责 commit）。CAS 失败抛 :class:`TextDrifted`。"""
    current_summary = gen.summary
    res = db.execute(
        update(Generation)
        .where(
            Generation.id == gen.id,
            func.coalesce(Generation.text, "") == current_text,
        )
        .values(
            text=snap.before_text or "",
            summary=snap.before_summary,
        )
    )
    if res.rowcount != 1:
        raise TextDrifted(
            f"第 {snap.segment_no} 段的正文在回滚前被改过了，未覆盖",
            segment_no=snap.segment_no,
        )

    new_snap = GenerationSnapshot(
        project_id=snap.project_id,
        generation_id=snap.generation_id,
        segment_no=snap.segment_no,
        batch_id=rollback_batch_id,
        operation="rollback",
        status="applied",
        before_text=current_text,
        before_summary=current_summary,
        before_hash=text_hash(current_text),
        after_text=snap.before_text or "",
        after_summary=snap.before_summary,
        after_hash=snap.before_hash,
        instruction=f"回滚快照 #{snap.id}",
        bible_hash=current_bible_hash,
        created_at=now,
        finished_at=now,
    )
    db.add(new_snap)
    db.flush()
    db.execute(
        update(GenerationSnapshot)
        .where(GenerationSnapshot.id == snap.id)
        .values(
            status="rolled_back",
            rolled_back_at=now,
            rolled_back_by_snapshot_id=new_snap.id,
        )
    )
    return new_snap


def _rebuild_derived(db, project: Project, provider=None) -> dict:
    """回滚提交后重建摘要树 + 全量 replay KG（一次，不逐段）。"""
    from app.generation.engine import rebuild_summary_tree
    from app.kg.build import rebuild_generated_kg

    if provider is None:
        from app.llm.provider import get_llm_provider

        provider = get_llm_provider()

    db.refresh(project)
    tree_stats: dict = {}
    try:
        tree_stats = rebuild_summary_tree(db, project, provider) or {}
    except Exception as exc:  # noqa: BLE001 —— 正文已回滚，摘要重建失败要如实回报
        log.warning("回滚后重建摘要树失败（正文已回滚）：%s", exc)
        tree_stats = {"error": str(exc)[:200]}

    kg_stats: dict = {}
    kg_complete = False
    try:
        kg_stats = rebuild_generated_kg(db, project.id) or {}
        kg_complete = bool(kg_stats.get("complete", not kg_stats.get("failed")))
    except Exception as exc:  # noqa: BLE001 —— KG 重建非原子，失败不吞
        log.warning("回滚后 KG replay 失败（正文与摘要已回滚）：%s", exc)
        kg_stats = {"rebuilt": False, "reason": str(exc)[:200], "failed": []}
        kg_complete = False
    return {
        "summary_tree": tree_stats,
        "kg": kg_stats,
        "kg_complete": kg_complete,
    }


def rollback_snapshot(
    db,
    project: Project,
    snapshot_id: int,
    *,
    mode: str = "auto",
    actor: str | None = None,
    provider=None,
) -> dict:
    """把某一段退回到 ``snapshot.before_text``。

    Args:
        mode: ``auto``（默认）/ ``text_only``（用户明确的选项 B）/
            ``with_bible``（选项 A：先把设定退到快照时点，再退正文）。
            设定变过且 mode=auto → 抛 :class:`BibleDrift`（409 + 三个选项）。
    """
    snap = _load_snapshot(db, snapshot_id)
    if snap.project_id != project.id:
        raise SnapshotNotFound(f"快照 #{snapshot_id} 不属于项目 {project.id}")
    if snap.status != "applied":
        raise RollbackError(
            f"快照 #{snapshot_id} 当前状态是 {snap.status}，只有 applied 能回滚"
        )
    gen = db.get(Generation, snap.generation_id)
    if gen is None:
        raise RollbackError(
            f"快照 #{snapshot_id} 指向的正文行 #{snap.generation_id} 已不存在"
        )

    current_text = _current_text(gen)
    if text_hash(current_text) != snap.after_hash:
        raise TextDrifted(
            f"第 {snap.segment_no} 段的正文在快照之后又被改过"
            "（可能是别处的手工编辑或另一次重写），回滚会覆盖它，已拒绝",
            segment_no=snap.segment_no,
            expected_hash=snap.after_hash,
            current_hash=text_hash(current_text),
        )

    warnings: list[str] = []
    now_bible = bible_hash(project)
    if now_bible != snap.bible_hash:
        if mode == "with_bible":
            rev = _find_bible_revision(db, project.id, snap.bible_hash)
            if rev is None:
                raise BibleDrift(
                    "设定在快照之后变过，但历史里找不到快照时点的设定版本，"
                    "无法自动退设定；请手工改回设定，或选择只回滚正文",
                    segment_no=snap.segment_no,
                    can_restore_bible=False,
                    snapshot_bible_hash=snap.bible_hash,
                    current_bible_hash=now_bible,
                )
            from app.kg.bible import apply_bible_revision

            apply_bible_revision(
                db, project.id, rev.bible_json,
                note=f"回滚正文前先把设定退到 revision {rev.revision_no}"
                     f"（快照 #{snap.id}）",
                source="rollback",
            )
            db.refresh(project)
            now_bible = bible_hash(project)
            warnings.append(
                f"已把设定退回到 revision {rev.revision_no}；"
                "图谱实体/别名表里的新名不会自动删，需要时请手工清理"
            )
        elif mode == "text_only":
            warnings.append("bible_changed_since_snapshot：只回滚了正文，设定保持现状")
        else:
            rev = _find_bible_revision(db, project.id, snap.bible_hash)
            raise BibleDrift(
                "这段正文重写之后，设定（plot_bible）被改过。只退正文会留下"
                "「正文用旧设定、名单用新设定」的不一致，请选择怎么处理",
                segment_no=snap.segment_no,
                can_restore_bible=rev is not None,
                restorable_revision_no=rev.revision_no if rev is not None else None,
                snapshot_bible_hash=snap.bible_hash,
                current_bible_hash=now_bible,
            )

    rollback_batch_id = str(uuid.uuid4())
    now = utcnow()
    try:
        new_snap = _restore_one(
            db, snap=snap, gen=gen, current_text=current_text,
            rollback_batch_id=rollback_batch_id, now=now,
            current_bible_hash=now_bible,
        )
        db.commit()
    except RollbackError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(gen)
    derived = _rebuild_derived(db, project, provider)
    db.refresh(project)
    return {
        "ok": True,
        "restored_snapshot_id": snap.id,
        "rollback_snapshot_id": new_snap.id,
        "rollback_batch_id": rollback_batch_id,
        "segment_no": snap.segment_no,
        "chars": len(gen.text or ""),
        "text_hash": text_hash(gen.text),
        "operation": "rollback",
        "actor": actor,
        "warnings": warnings,
        "derived": derived,
    }


def rollback_batch(
    db,
    project: Project,
    batch_id: str,
    *,
    mode: str = "auto",
    actor: str | None = None,
    provider=None,
) -> dict:
    """按 ``batch_id`` 整组回滚：要么全退，要么一行不改。

    批量**重写**本身没有事务原子性（引擎要求先提交正文再 replay KG），
    「不能退一半」由这里的单事务恢复兜底。
    """
    snaps = db.scalars(
        select(GenerationSnapshot)
        .where(
            GenerationSnapshot.project_id == project.id,
            GenerationSnapshot.batch_id == batch_id,
            GenerationSnapshot.status == "applied",
        )
        .order_by(GenerationSnapshot.segment_no)
    ).all()
    if not snaps:
        raise RollbackError(
            f"批次 {batch_id} 下没有可回滚的已生效快照（可能已经退过或全部失败）"
        )

    # —— 预检：全部通过才动库，任一不过 → 409，零写入 ——
    blockers: list[dict] = []
    gens: dict[int, Generation] = {}
    for snap in snaps:
        gen = db.get(Generation, snap.generation_id)
        if gen is None:
            blockers.append({"snapshot_id": snap.id, "segment_no": snap.segment_no,
                             "reason": "正文行已不存在"})
            continue
        gens[snap.id] = gen
        later = db.scalar(
            select(GenerationSnapshot)
            .where(
                GenerationSnapshot.project_id == project.id,
                GenerationSnapshot.segment_no == snap.segment_no,
                GenerationSnapshot.status == "applied",
                GenerationSnapshot.id > snap.id,
            )
            .order_by(GenerationSnapshot.id.desc())
        )
        if later is not None:
            blockers.append({
                "snapshot_id": snap.id, "segment_no": snap.segment_no,
                "reason": f"该段在那批之后又被改过（快照 #{later.id}），先撤销后面的",
            })
            continue
        if text_hash(_current_text(gen)) != snap.after_hash:
            blockers.append({
                "snapshot_id": snap.id, "segment_no": snap.segment_no,
                "reason": "正文在快照之后被改过",
            })

    now_bible = bible_hash(project)
    drifted = [s for s in snaps if s.bible_hash != now_bible]
    if drifted and mode == "auto":
        rev = _find_bible_revision(db, project.id, drifted[0].bible_hash)
        raise BibleDrift(
            "这批重写之后设定（plot_bible）被改过，整批回滚会让正文与设定不一致",
            segment_no=drifted[0].segment_no,
            affected_segments=[s.segment_no for s in drifted],
            can_restore_bible=rev is not None,
            restorable_revision_no=rev.revision_no if rev is not None else None,
        )
    if blockers:
        raise BatchBlocked(
            "这批里有段落在之后又被改过，整批回滚会覆盖别人的改动，已拒绝（未写入任何内容）",
            blockers=blockers,
        )

    warnings: list[str] = []
    if drifted and mode == "with_bible":
        rev = _find_bible_revision(db, project.id, drifted[0].bible_hash)
        if rev is None:
            raise BibleDrift(
                "设定变过，但找不到快照时点的设定版本，无法自动退设定",
                can_restore_bible=False,
            )
        from app.kg.bible import apply_bible_revision

        apply_bible_revision(
            db, project.id, rev.bible_json,
            note=f"整批回滚正文前先退设定到 revision {rev.revision_no}",
            source="rollback",
        )
        db.refresh(project)
        now_bible = bible_hash(project)
        warnings.append(f"已把设定退回到 revision {rev.revision_no}")
    elif drifted:
        warnings.append("bible_changed_since_snapshot：只回滚了正文，设定保持现状")

    rollback_batch_id = str(uuid.uuid4())
    now = utcnow()
    results: list[dict] = []
    try:
        for snap in snaps:
            gen = gens[snap.id]
            new_snap = _restore_one(
                db, snap=snap, gen=gen,
                current_text=_current_text(gen),
                rollback_batch_id=rollback_batch_id, now=now,
                current_bible_hash=now_bible,
            )
            results.append({
                "segment_no": snap.segment_no,
                "restored_snapshot_id": snap.id,
                "rollback_snapshot_id": new_snap.id,
                "chars": len(snap.before_text or ""),
            })
        db.commit()
    except RollbackError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    for gen in gens.values():
        db.refresh(gen)
    derived = _rebuild_derived(db, project, provider)
    return {
        "ok": True,
        "batch_id": batch_id,
        "rollback_batch_id": rollback_batch_id,
        "segments": results,
        "count": len(results),
        "operation": "rollback",
        "actor": actor,
        "warnings": warnings,
        "derived": derived,
    }


def rollback_latest(
    db,
    project: Project,
    segment_no: int,
    *,
    mode: str = "auto",
    actor: str | None = None,
    provider=None,
) -> dict:
    """「撤销上一次」：该段最近一条 applied 快照。"""
    from app.agents.dialog.snapshot import latest_applied

    snap = latest_applied(db, project_id=project.id, segment_no=segment_no)
    if snap is None:
        raise SnapshotNotFound(f"第 {segment_no} 段没有可撤销的重写记录")
    return rollback_snapshot(db, project, snap.id, mode=mode, actor=actor,
                             provider=provider)


def list_batches(db, *, project_id: int, limit: int = 20) -> list[dict]:
    """按批次聚合的撤销入口（UI「整批撤销」列表）。"""
    rows = db.scalars(
        select(GenerationSnapshot)
        .where(
            GenerationSnapshot.project_id == project_id,
            GenerationSnapshot.status.in_(("applied", "rolled_back", "failed")),
        )
        .order_by(GenerationSnapshot.id.desc())
    ).all()
    grouped: dict[str, dict] = {}
    for snap in rows:
        item = grouped.setdefault(snap.batch_id, {
            "batch_id": snap.batch_id,
            "operation": snap.operation,
            "created_at": None,
            "segments": [],
            "applied": 0,
            "failed": 0,
            "rolled_back": 0,
        })
        item["segments"].append(snap.segment_no)
        item[snap.status] = item.get(snap.status, 0) + 1
        ts = aware(snap.created_at)
        if ts is not None and (
            item["created_at"] is None or ts.isoformat() > item["created_at"]
        ):
            item["created_at"] = ts.isoformat()
    out = []
    for item in grouped.values():
        item["segments"] = sorted(set(item["segments"]))
        item["undoable"] = item["applied"] > 0
        out.append(item)
    out.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return out[: max(1, min(limit, 100))]


def snapshot_payload(snap: GenerationSnapshot) -> dict:
    return snapshot_to_dict(snap)
