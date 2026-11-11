# -*- coding: utf-8 -*-
"""重写前快照的写入原语：begin / finalize / fail / 心跳 / 收割 / 清理。

设计见 ``.trae/documents/rewrite-snapshot-design.md``。这里只碰
``generation_snapshots`` 与 ``generations.text/summary``，不改别的表。

## 两段式

1. :func:`begin_snapshot` —— 调模型**前**写 ``pending``（独立短事务）。
   同段已有在途重写时抛 :class:`SnapshotBusy`（部分唯一索引兜底）。
2. :func:`finalize_snapshot` —— 模型返回后**在调用方的事务里**CAS 写正文
   并把快照置 ``applied``；**本函数不 commit**，由引擎那次 commit 一起落。
3. :func:`fail_snapshot` —— 失败/超时/中断留痕 ``failed``（24 小时后清理）。

## 在途行是一条租约（2026-11-12）

``uq_gs_pending``（``(project_id, segment_no) WHERE status='pending'``）会把
「进程崩在半路」留下的 pending 变成该段的锁。旧实现靠固定 30 分钟超时自愈，
有两个洞：进程活着但流被遗弃时最多卡 30 分钟；多 worker 启动时会把别人的
在途行当遗留清掉。

现在 pending 行同时是租约：``owner_pid`` + ``owner_started_at`` +
``heartbeat_at``。三条判据各管一段，互不替代：

1. **所有者已死**（pid 不存在 / 启动时刻对不上）→ 进入前与启动时直接清。
   这是 :func:`reap_orphan_pending`，不需要用户操作。
2. **所有者活着但心跳超时**（``now - heartbeat_at > max(3×间隔, 60s)``）→
   **不自动清**。慢模型/长任务不是死人；返回 409，由用户显式「强制接管」。
   接管用条件 UPDATE 做 CAS（id + 原 owner_pid + 原 heartbeat_at），
   只有 CAS 成功才改归属；失败一律人话，不把 SQL/约束错误抛给用户。
3. **多 worker 启动** → 只清判据 1 的行；同批其他存活 worker 的行不动。

为什么不用固定超时：回滚链路会重放全书 KG，一次可能远超 5 分钟；把
「活得久」当「死了」会收割正在正常干活的任务。心跳阈值与 ``HEARTBEAT_INTERVAL_SECONDS``
的关系：间隔 20s、判据 60s，允许连续丢 2 次心跳；缩短判据等于要求每次
心跳都准时落库，反而更容易误杀。

## 存量 pending 的回填

迁移只加列、**不回填**：``owner_pid`` / ``owner_started_at`` /
``heartbeat_at`` 全部保持 NULL。理由：按 ``created_at`` 补一个心跳等于给
僵尸行发一张新租约，会让真正的崩溃残留多活 60 秒；补一个假 pid 更糟——
可能撞上被复用的 pid。NULL 的语义是「升级前写入、没有租约证据」：
启动收割按判据 1（pid 不存在）清掉，显式接管按判据 2（心跳 NULL 视为已
过期）处理。因此迁移应在**重启前**执行；若在线执行且旧进程仍在跑，它
写下的 pending 没有租约，可能被新进程接管——这是升级窗口的已知代价。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Generation, GenerationSnapshot, Project

log = logging.getLogger(__name__)

#: 期望的心跳间隔（秒）。流式输出期间按这个间隔节流写库。
HEARTBEAT_INTERVAL_SECONDS = 20.0
#: 心跳超时判据：``max(3 × 间隔, 60s)``。固定 30 分钟超时已删除。
HEARTBEAT_TIMEOUT_SECONDS = max(3 * HEARTBEAT_INTERVAL_SECONDS, 60.0)
#: failed 行留 24 小时（「重试过一次」的痕迹）。
FAILED_RETENTION_HOURS = 24
#: 每段保留最新几条已生效快照。
APPLIED_KEEP_PER_SEGMENT = 3
#: 7 天内的快照不删（哪怕超出条数）。
APPLIED_MIN_RETENTION_DAYS = 7
#: 保留策略的触发间隔：写入路径上顺带清理，不开新线程。
PURGE_INTERVAL_SECONDS = 3600

_last_purge_at: datetime | None = None
#: 本进程启动时刻指纹（只算一次；pid 复用检测的自身一侧）。
_self_started_at: str | None = None


class SnapshotError(RuntimeError):
    """快照层错误基类。"""


class SnapshotBusy(SnapshotError):
    """同段已有在途重写（唯一索引挡下的那一刻）。

    ``detail`` 是给 HTTP 层直接用的结构：文案与字段都已在服务层算好，
    router 不再拼 SQL/异常原文，避免把数据库错误漏给用户。
    """

    code = "segment_busy"

    def __init__(
        self,
        segment_no: int,
        *,
        owner_pid: int | None = None,
        heartbeat_at: datetime | None = None,
        stale_seconds: float | None = None,
        can_takeover: bool = False,
        takeover_failed: bool = False,
    ):
        self.segment_no = segment_no
        self.owner_pid = owner_pid
        self.heartbeat_at = heartbeat_at
        self.stale_seconds = stale_seconds
        self.can_takeover = can_takeover
        self.takeover_failed = takeover_failed

        who = f"pid={owner_pid if owner_pid is not None else '未知'}"
        stale = int(stale_seconds or 0)
        if owner_pid is None and stale_seconds is None:
            # 唯一索引冲突后回查时行已消失（刚被别的请求收割/接管）。
            msg = (
                f"第 {segment_no} 段正在重写中，状态刚刚变化，请稍后重试"
                "（同一段同时只能有一个重写）。"
            )
        elif takeover_failed:
            msg = (
                f"第 {segment_no} 段正在重写中：这段正由另一个进程写入"
                f"（{who}，心跳仍新鲜），未能接管；请稍后重试，或等原写入结束。"
            )
        elif can_takeover:
            msg = (
                f"第 {segment_no} 段正在重写中（{who}，心跳已停 {stale} 秒），"
                "可强制接管；否则请等这次完成后再试（同一段同时只能有一个重写）。"
            )
        else:
            msg = (
                f"第 {segment_no} 段正在重写中（{who}，心跳正常，"
                f"{stale} 秒前更新），请等这次完成后再试"
                "（同一段同时只能有一个重写）。"
            )
        super().__init__(msg)

    @property
    def detail(self) -> dict:
        out: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "segment_no": self.segment_no,
            "can_takeover": bool(self.can_takeover),
        }
        if self.owner_pid is not None:
            out["owner_pid"] = self.owner_pid
        if self.heartbeat_at is not None:
            out["heartbeat_at"] = self.heartbeat_at.isoformat()
        if self.stale_seconds is not None:
            out["stale_seconds"] = int(self.stale_seconds)
        if self.can_takeover:
            out["options"] = [
                {"id": "takeover", "label": "强制接管（原写入者心跳已停）"},
                {"id": "cancel", "label": "取消"},
            ]
        return out


class ConcurrentEdit(SnapshotError):
    """提交时发现正文已被别的入口改过（CAS 失败）。"""

    code = "concurrent_edit"

    def __init__(self, segment_no: int):
        self.segment_no = segment_no
        super().__init__(
            f"第 {segment_no} 段在重写期间被改过了（另一个入口写入了新正文），"
            "本次结果没有写回；请刷新后重试。"
        )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回来是 naive，统一按 UTC 补齐（与 style.jobs 同口径）。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def text_hash(text: Any) -> str:
    """正文 sha256；None 与空串等价（CAS 基线用）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def bible_dict_hash(bible: Any) -> str | None:
    """plot_bible 内容指纹；空设定返回 None（与「设定为空」等价）。"""
    if not bible:
        return None
    return hashlib.sha256(canonical_json(bible).encode("utf-8")).hexdigest()


def bible_hash(project: Project | None) -> str | None:
    if project is None:
        return None
    return bible_dict_hash(project.plot_bible)


# ====================================================================
# 租约：进程身份 / 判活 / 心跳
# ====================================================================


def _process_identity_uncached(pid: int) -> str | None:
    """进程启动时刻的稳定指纹（同一 pid 复用后一定不同）。"""
    if pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
        )
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        # PROCESS_QUERY_LIMITED_INFORMATION：够读启动时刻，不需要 PROCESS_ALL_ACCESS
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                return None
            stamp = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return f"win:{stamp}"
        finally:
            kernel32.CloseHandle(handle)

    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # comm 里可能有空格/括号，从**最后一个** ')' 之后开始切；字段 3 起。
    try:
        fields = data[data.rindex(b")") + 2:].split()
        return f"proc:{fields[19].decode()}"  # 字段 22 = starttime
    except (ValueError, IndexError):
        return None


def process_identity(pid: int | None = None) -> str | None:
    """本进程（或指定 pid）的启动时刻指纹；拿不到返回 None。"""
    global _self_started_at
    if pid is None:
        if _self_started_at is None:
            _self_started_at = _process_identity_uncached(os.getpid())
        return _self_started_at
    return _process_identity_uncached(int(pid))


def owner_alive(pid: int | None, started_at: str | None) -> bool:
    """租约持有者是否还活着。

    主判据是 :func:`process_identity`（Windows ``GetProcessTimes`` /
    POSIX ``/proc/<pid>/stat``）：读得到启动时刻就同时判活与判 pid 复用；
    读不到才退回 ``kg.batch_worker._pid_alive`` 的 ``OpenProcess``
    （Windows 上 ``os.kill(pid, 0)`` 会终止进程，绝不能用）。pid 存活但
    ``owner_started_at`` 对不上 = pid 被复用，按「已死」处理。
    """
    if not pid or int(pid) <= 0:
        return False
    actual = process_identity(int(pid))
    if actual is None:
        # 读不到启动时刻（权限/进程刚退出）：用现有 OpenProcess 判活兜底。
        # 兜底也拿不到结论时按「活着」处理——宁可不清，交给心跳/显式接管，
        # 也不能把同批存活 worker 误杀。
        try:
            from app.kg.batch_worker import _pid_alive

            if not _pid_alive(int(pid)):
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("pid %s 判活失败（按活着处理）：%s", pid, exc)
        return True
    # 启动时刻一致才算原持有者；对不上 = pid 被复用，按已死处理。
    return started_at == actual


def stale_seconds(snap: GenerationSnapshot, now: datetime | None = None) -> float | None:
    """距离最后一次生命迹象的秒数（心跳优先，没有心跳退回 created_at 仅用于展示）。"""
    now = now or utcnow()
    ref = aware(snap.heartbeat_at) or aware(snap.created_at)
    if ref is None:
        return None
    return max(0.0, (now - ref).total_seconds())


def lease_expired(snap: GenerationSnapshot, now: datetime | None = None) -> bool:
    """心跳是否已超时（判据：``now - heartbeat_at > max(3×间隔, 60s)``）。

    ``heartbeat_at IS NULL``（升级前的存量行）视为已过期：没有租约证据，
    但**绝不**用 created_at 补一个假心跳。
    """
    now = now or utcnow()
    hb = aware(snap.heartbeat_at)
    if hb is None:
        return True
    return (now - hb).total_seconds() > HEARTBEAT_TIMEOUT_SECONDS


class SnapshotLease:
    """流式重写期间的进程内心跳租约（节流写库）。

    不能每个 chunk 都写：SQLite 写事务串行，心跳会和生成/抽取抢写锁。
    ``touch`` 先在内存里看「距上次写是否超过间隔」——没超过直接返回，
    一次数据库调用都不发；超过了才发一条短 UPDATE + commit。

    不启用后台线程：本项目的流式 provider 以 token 级产出为主，循环里
    顺带 touch 足够；代价是「模型整段无输出超过 60s」时另一进程可能判定
    心跳超时（要接管仍需用户显式确认，不会静默抢走）。
    """

    def __init__(
        self, snapshot_id: int, *, interval: float = HEARTBEAT_INTERVAL_SECONDS
    ) -> None:
        self.snapshot_id = snapshot_id
        self.interval = interval
        self.beats = 0          # 真正落库的心跳次数
        self.skipped = 0        # 因节流跳过的次数
        self.lost = False       # 心跳没命中（已被收割/接管）
        self._last: datetime | None = None

    def touch(self, db) -> bool:
        """到达间隔才写一次心跳。**永不抛异常**（心跳失败不能打断生成）。"""
        now = utcnow()
        if self._last is not None and (
            now - self._last
        ).total_seconds() < self.interval:
            self.skipped += 1
            return False
        try:
            res = db.execute(
                update(GenerationSnapshot)
                .where(
                    GenerationSnapshot.id == self.snapshot_id,
                    GenerationSnapshot.status == "pending",
                    GenerationSnapshot.owner_pid == os.getpid(),
                )
                .values(heartbeat_at=now)
                # 不让 ORM 在 Python 里重放 WHERE（naive/aware 混比会 TypeError）；
                # 我们只认 rowcount，不需要 session 同步。
                .execution_options(synchronize_session=False)
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001 —— 心跳是增强，不是生成的前提
            log.warning("快照 #%s 心跳失败（已忽略）：%s", self.snapshot_id, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
            self._last = now
            return False
        self._last = now
        if res.rowcount:
            self.beats += 1
            return True
        if not self.lost:
            self.lost = True
            log.warning("快照 #%s 心跳未命中（已被收割或接管）", self.snapshot_id)
        return False


# ====================================================================
# 写入
# ====================================================================


def _new_pending(
    *,
    project: Project,
    generation: Generation,
    now: datetime,
    instruction: str | None,
    agent_run_id: int | None,
    batch_id: str | None,
    operation: str,
) -> GenerationSnapshot:
    return GenerationSnapshot(
        project_id=project.id,
        generation_id=generation.id,
        segment_no=generation.segment_no,
        batch_id=batch_id or str(uuid.uuid4()),
        operation=operation,
        status="pending",
        before_text=generation.text or "",
        before_summary=generation.summary,
        before_hash=text_hash(generation.text),
        instruction=(instruction or "").strip() or None,
        agent_run_id=agent_run_id,
        bible_hash=bible_hash(project),
        owner_pid=os.getpid(),
        owner_started_at=process_identity(),
        heartbeat_at=now,
        created_at=now,
    )


def _pending_for(
    db, project_id: int, segment_no: int
) -> GenerationSnapshot | None:
    return db.scalar(
        select(GenerationSnapshot).where(
            GenerationSnapshot.project_id == project_id,
            GenerationSnapshot.segment_no == segment_no,
            GenerationSnapshot.status == "pending",
        )
    )


def _busy(
    blocker: GenerationSnapshot | None,
    segment_no: int,
    *,
    takeover_failed: bool = False,
) -> SnapshotBusy:
    if blocker is None:
        return SnapshotBusy(segment_no, takeover_failed=takeover_failed)
    now = utcnow()
    return SnapshotBusy(
        segment_no,
        owner_pid=blocker.owner_pid,
        heartbeat_at=aware(blocker.heartbeat_at),
        stale_seconds=stale_seconds(blocker, now),
        can_takeover=lease_expired(blocker, now),
        takeover_failed=takeover_failed,
    )


def _takeover(
    db,
    *,
    blocker: GenerationSnapshot,
    project: Project,
    generation: Generation,
    instruction: str | None,
    agent_run_id: int | None,
    batch_id: str | None,
    operation: str,
    now: datetime,
) -> GenerationSnapshot:
    """强制接管：条件 UPDATE 做 CAS，只有原租约确实过期且 CAS 命中才改归属。

    CAS 条件包含 ``owner_pid`` 与 ``heartbeat_at`` 的**原值**：原持有者只要
    在这之间写了一次心跳，UPDATE 就不会命中——不会把活人正在写的行抢走。
    成功时原行置 ``failed``，``error_reason`` 记录原持有者与停跳时长；
    新 pending 行记录接管者（当前进程）。证据两行都在，不额外加列。
    """
    cutoff = now - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
    stale = stale_seconds(blocker, now)
    me = os.getpid()
    reason = (
        f"taken_over: owner_pid={blocker.owner_pid}; "
        f"owner_started_at={blocker.owner_started_at}; "
        f"stale={int(stale or 0)}s; by_pid={me}"
    )[:255]
    res = db.execute(
        update(GenerationSnapshot)
        .where(
            GenerationSnapshot.id == blocker.id,
            GenerationSnapshot.status == "pending",
            GenerationSnapshot.owner_pid == blocker.owner_pid,
            GenerationSnapshot.heartbeat_at == blocker.heartbeat_at,
            or_(
                GenerationSnapshot.heartbeat_at.is_(None),
                GenerationSnapshot.heartbeat_at < cutoff,
            ),
        )
        .values(status="failed", error_reason=reason, finished_at=now)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        db.rollback()
        raise _busy(
            _pending_for(db, project.id, generation.segment_no),
            generation.segment_no,
            takeover_failed=True,
        )

    snap = _new_pending(
        project=project, generation=generation, now=now,
        instruction=instruction, agent_run_id=agent_run_id,
        batch_id=batch_id, operation=operation,
    )
    db.add(snap)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise _busy(
            _pending_for(db, project.id, generation.segment_no),
            generation.segment_no,
            takeover_failed=True,
        ) from None
    db.refresh(snap)
    log.warning(
        "快照接管：第 %s 段，原持有者 pid=%s（心跳停 %s 秒），接管者 pid=%s",
        generation.segment_no, blocker.owner_pid, int(stale or 0), me,
    )
    return snap


def begin_snapshot(
    db,
    project: Project,
    generation: Generation,
    *,
    instruction: str | None = None,
    agent_run_id: int | None = None,
    batch_id: str | None = None,
    operation: str = "rewrite",
    takeover: bool = False,
) -> GenerationSnapshot:
    """调模型**前**写 pending（独立短事务，立即 commit）。

    Args:
        takeover: 仅当 409 返回 ``can_takeover`` 后由用户显式要求时传 True。
            接管本身仍要过 CAS（原心跳确实超时才可能命中）。

    Raises:
        SnapshotBusy: 同段已有在途重写；所有者已死的行会先被自动清掉，
            活着但心跳超时的行不会被静默清，必须显式接管。
    """
    now = utcnow()
    _maybe_purge(db, now=now)
    # 只清「所有者已死」的行：活着的（哪怕心跳超时）不在这里动。
    reap_orphan_pending(
        db, project_id=project.id, segment_no=generation.segment_no, now=now
    )

    snap = _new_pending(
        project=project, generation=generation, now=now,
        instruction=instruction, agent_run_id=agent_run_id,
        batch_id=batch_id, operation=operation,
    )
    db.add(snap)
    try:
        db.commit()
    except IntegrityError:
        # 部分唯一索引 uq_gs_pending 命中：另一个写入者正在重写同段。
        db.rollback()
        blocker = _pending_for(db, project.id, generation.segment_no)
        if takeover and blocker is not None:
            return _takeover(
                db, blocker=blocker, project=project, generation=generation,
                instruction=instruction, agent_run_id=agent_run_id,
                batch_id=batch_id, operation=operation, now=now,
            )
        raise _busy(blocker, generation.segment_no) from None
    db.refresh(snap)
    return snap


def find_clarification_by_fingerprint(
    db, *, project_id: int, segment_no: int, fingerprint: str,
) -> GenerationSnapshot | None:
    rows = db.scalars(
        select(GenerationSnapshot)
        .where(
            GenerationSnapshot.project_id == project_id,
            GenerationSnapshot.segment_no == segment_no,
            GenerationSnapshot.status == "needs_clarification",
        )
        .order_by(GenerationSnapshot.id.desc())
    ).all()
    for snap in rows:
        data = snap.clarification if isinstance(snap.clarification, dict) else {}
        if data.get("fingerprint") == fingerprint:
            return snap
    return None


def begin_clarification(
    db,
    project: Project,
    generation: Generation,
    *,
    instruction: str,
    round_no: int,
    candidates: list[dict],
    fingerprint: str,
    note: str | None = None,
    agent_run_id: int | None = None,
) -> GenerationSnapshot:
    """记一条 ``needs_clarification`` 澄清快照（**不建 pending、不写正文**）。

    幂等：同一 ``fingerprint``（项目/段/正文哈希/指令/轮次/补充说明）已存在
    澄清行时直接复用，不调模型、不生成第二批候选。澄清行不参与
    ``uq_gs_pending``（只约束 status='pending'），也不会被 rollback 命中。
    """
    existing = find_clarification_by_fingerprint(
        db, project_id=project.id,
        segment_no=generation.segment_no, fingerprint=fingerprint,
    )
    if existing is not None:
        return existing

    now = utcnow()
    snap = GenerationSnapshot(
        project_id=project.id,
        generation_id=generation.id,
        segment_no=generation.segment_no,
        batch_id=str(uuid.uuid4()),
        operation="clarify",
        status="needs_clarification",
        before_text=generation.text or "",
        before_summary=generation.summary,
        before_hash=text_hash(generation.text),
        instruction=(instruction or "").strip() or None,
        agent_run_id=agent_run_id,
        bible_hash=bible_hash(project),
        owner_pid=os.getpid(),
        owner_started_at=process_identity(),
        heartbeat_at=now,
        created_at=now,
        finished_at=now,
        clarification={
            "round": round_no,
            "candidates": candidates,
            "fingerprint": fingerprint,
            "note": (note or "").strip() or None,
        },
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def find_clarification(db, clarification_id: int) -> GenerationSnapshot | None:
    return db.get(GenerationSnapshot, clarification_id)


def mark_clarification_clarified(db, clarification_id: int) -> None:
    """选定并成功写入后把澄清行置为 ``clarified``（一次性，best-effort）。"""
    snap = db.get(GenerationSnapshot, clarification_id)
    if snap is None or snap.status != "needs_clarification":
        return
    try:
        snap.status = "clarified"
        snap.finished_at = utcnow()
        db.commit()
    except Exception:  # noqa: BLE001 - 留痕失败不能反过来影响已落库的正文
        db.rollback()
        log.warning("澄清快照 %s 标记 clarified 失败", clarification_id, exc_info=True)


def finalize_snapshot(
    db,
    *,
    snapshot_id: int,
    generation_id: int,
    segment_no: int,
    expected_before: str | None,
    new_text: str,
    new_summary: Any,
    scope_mismatch: dict | None = None,
    length_drop: dict | None = None,
) -> None:
    """CAS 写正文 + 置 applied。**不 commit**，由调用方那次事务一起落。

    Args:
        scope_mismatch: 「指令限定局部、结果却像整段重写」的提示结构
            （见 ``app.generation.scope_guard``）。只留痕，不参与任何拦截或
            重试；与 applied 同一事务写入快照行，便于统计模型服从度。
        length_drop: 「篇幅跌幅超过 20%」的提示结构，**单独一列**——与
            scope_mismatch 分开记账，避免两类模型不服从混在一起。

    Raises:
        ConcurrentEdit: 正文已被别的入口改过（rowcount != 1）。
        SnapshotError: 快照不是 pending（被清理/接管/重复提交）。
    """
    now = utcnow()
    res = db.execute(
        update(Generation)
        .where(
            Generation.id == generation_id,
            func.coalesce(Generation.text, "") == (expected_before or ""),
        )
        .values(
            text=new_text,
            summary=new_summary,
            chapter_range=f"段{segment_no}",
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise ConcurrentEdit(segment_no)

    res = db.execute(
        update(GenerationSnapshot)
        .where(
            GenerationSnapshot.id == snapshot_id,
            GenerationSnapshot.status == "pending",
        )
        .values(
            status="applied",
            after_text=new_text,
            after_hash=text_hash(new_text),
            after_summary=new_summary,
            scope_mismatch=scope_mismatch,
            length_drop=length_drop,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise SnapshotError(
            f"快照 #{snapshot_id} 不是 pending（可能已被清理或重复提交），本次结果未记账"
        )


def fail_snapshot(
    db, snapshot_id: int | None, reason: str, *, commit: bool = True
) -> bool:
    """把 pending 置 failed 并留痕。**永不抛异常**（失败处理本身不能二次失败）。"""
    if not snapshot_id:
        return False
    try:
        res = db.execute(
            update(GenerationSnapshot)
            .where(
                GenerationSnapshot.id == snapshot_id,
                GenerationSnapshot.status == "pending",
            )
            .values(status="failed", error_reason=(reason or "")[:255],
                    finished_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if commit:
            db.commit()
        return bool(res.rowcount)
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不能盖住原始错误
        log.warning("快照 #%s 失败留痕失败（已忽略）：%s", snapshot_id, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


# ====================================================================
# 收割 / 清理
# ====================================================================


def reap_orphan_pending(
    db,
    *,
    project_id: int | None = None,
    segment_no: int | None = None,
    now: datetime | None = None,
    reason: str = "owner_gone",
) -> int:
    """把「所有者已死」的 pending 置 failed（进入前与启动时共用）。

    判据是 pid 存活 + ``owner_started_at`` 一致，**不是**心跳超时：
    多 worker 同时启动时，别的 worker 的心跳可能只是暂时陈旧，但它还活着，
    不能清。心跳超时的活人交给显式接管。
    """
    now = now or utcnow()
    stmt = select(GenerationSnapshot).where(
        GenerationSnapshot.status == "pending"
    )
    if project_id is not None:
        stmt = stmt.where(GenerationSnapshot.project_id == project_id)
    if segment_no is not None:
        stmt = stmt.where(GenerationSnapshot.segment_no == segment_no)
    try:
        rows = db.scalars(stmt).all()
        doomed = [
            row.id for row in rows
            if not owner_alive(row.owner_pid, row.owner_started_at)
        ]
        if not doomed:
            return 0
        res = db.execute(
            update(GenerationSnapshot)
            .where(
                GenerationSnapshot.id.in_(doomed),
                GenerationSnapshot.status == "pending",
            )
            .values(status="failed", error_reason=reason, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if res.rowcount:
            log.info("收割无主 pending %s 条（%s）", res.rowcount, reason)
        return int(res.rowcount or 0)
    except Exception as exc:  # noqa: BLE001 —— 收割失败不该打断进入
        log.warning("收割无主 pending 失败（已忽略）：%s", exc)
        db.rollback()
        return 0


def reap_all_pending(db, *, reason: str = "process_restart") -> int:
    """兼容旧调用名：启动收割现在**只挑所有者已死的**行。"""
    return reap_orphan_pending(db, reason=reason, now=utcnow())


def _maybe_purge(db, *, now: datetime) -> None:
    """写入路径上顺带清理（每小时最多一次），避免长跑进程里垃圾只增不减。"""
    global _last_purge_at
    if _last_purge_at is not None and (
        now - _last_purge_at
    ).total_seconds() < PURGE_INTERVAL_SECONDS:
        return
    _last_purge_at = now
    purge_snapshots(db, now=now)


def purge_snapshots(db, *, now: datetime | None = None) -> dict:
    """保留策略：failed 留 24 小时；applied/rolled_back 每段最新 3 条且 7 天内不删。"""
    now = now or utcnow()
    out = {"failed_deleted": 0, "applied_deleted": 0}
    try:
        res = db.execute(
            delete(GenerationSnapshot).where(
                GenerationSnapshot.status == "failed",
                GenerationSnapshot.created_at
                < now - timedelta(hours=FAILED_RETENTION_HOURS),
            )
            .execution_options(synchronize_session=False)
        )
        out["failed_deleted"] = int(res.rowcount or 0)

        rows = db.scalars(
            select(GenerationSnapshot)
            .where(GenerationSnapshot.status.in_(("applied", "rolled_back")))
            .order_by(GenerationSnapshot.project_id,
                      GenerationSnapshot.segment_no,
                      GenerationSnapshot.id.desc())
        ).all()
        keep_cutoff = now - timedelta(days=APPLIED_MIN_RETENTION_DAYS)
        seen: dict[tuple[int, int], int] = {}
        doomed: list[int] = []
        for row in rows:
            key = (row.project_id, row.segment_no)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= APPLIED_KEEP_PER_SEGMENT:
                continue
            created = aware(row.created_at)
            if created is not None and created >= keep_cutoff:
                continue  # 7 天内的不删
            doomed.append(row.id)
        if doomed:
            res = db.execute(
                delete(GenerationSnapshot)
                .where(GenerationSnapshot.id.in_(doomed))
                .execution_options(synchronize_session=False)
            )
            out["applied_deleted"] = int(res.rowcount or 0)
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— 清理是后台增强，失败不该拖垮调用方
        log.warning("快照清理失败（已忽略）：%s", exc)
        db.rollback()
    return out


# ====================================================================
# 读
# ====================================================================


def snapshot_to_dict(snap: GenerationSnapshot) -> dict:
    now = utcnow()
    return {
        "id": snap.id,
        "project_id": snap.project_id,
        "generation_id": snap.generation_id,
        "segment_no": snap.segment_no,
        "batch_id": snap.batch_id,
        "operation": snap.operation,
        "status": snap.status,
        "before_hash": snap.before_hash,
        "after_hash": snap.after_hash,
        "instruction": snap.instruction,
        # 局部指令被整段重写的提示（None = 未命中）。只读留痕，不阻断。
        "scope_mismatch": snap.scope_mismatch,
        # 篇幅跌幅提示（None = 未命中/用户要求精简）。与 scope_mismatch 分列。
        "length_drop": snap.length_drop,
        "agent_run_id": snap.agent_run_id,
        "bible_hash": snap.bible_hash,
        "error_reason": snap.error_reason,
        # 含糊指令的候选澄清（round/candidates/fingerprint/note）；None = 非澄清行。
        "clarification": snap.clarification,
        "owner_pid": snap.owner_pid,
        "owner_started_at": snap.owner_started_at,
        "heartbeat_at": (
            aware(snap.heartbeat_at).isoformat() if snap.heartbeat_at else None
        ),
        "lease_stale_seconds": (
            int(stale_seconds(snap, now)) if snap.status == "pending" else None
        ),
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
        "finished_at": snap.finished_at.isoformat() if snap.finished_at else None,
        "rolled_back_at": (
            snap.rolled_back_at.isoformat() if snap.rolled_back_at else None
        ),
        "rolled_back_by_snapshot_id": snap.rolled_back_by_snapshot_id,
        "before_chars": len(snap.before_text or ""),
        "after_chars": len(snap.after_text or ""),
        "undoable": snap.status == "applied",
    }


def list_snapshots(
    db,
    *,
    project_id: int,
    segment_no: int | None = None,
    batch_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict]:
    stmt = select(GenerationSnapshot).where(
        GenerationSnapshot.project_id == project_id
    )
    if segment_no is not None:
        stmt = stmt.where(GenerationSnapshot.segment_no == segment_no)
    if batch_id is not None:
        stmt = stmt.where(GenerationSnapshot.batch_id == batch_id)
    if status is not None:
        stmt = stmt.where(GenerationSnapshot.status == status)
    stmt = stmt.order_by(GenerationSnapshot.id.desc()).limit(max(1, min(limit, 200)))
    return [snapshot_to_dict(s) for s in db.scalars(stmt).all()]


def latest_applied(
    db, *, project_id: int, segment_no: int
) -> GenerationSnapshot | None:
    """该段最近一条可撤销的 applied 快照（UI「撤销上一次」的默认目标）。"""
    return db.scalar(
        select(GenerationSnapshot)
        .where(
            GenerationSnapshot.project_id == project_id,
            GenerationSnapshot.segment_no == segment_no,
            GenerationSnapshot.status == "applied",
        )
        .order_by(GenerationSnapshot.id.desc())
    )
