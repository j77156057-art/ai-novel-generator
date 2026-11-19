"""文风提炼后台任务（`DistillRun`）的**执行层**。

## 为什么要有这一层

提炼是**分钟级**的长任务（833 章语料跑完整档实测 42 分钟，见
`app.style.narrative` 档位注释里的唯一那处计时）。HTTP 请求挂不了这么久：
代理会超时、前端会白屏、用户刷新一下页面就什么都没了。

所以提炼拆成三步：

- `POST /api/projects/{id}/distill` 只**登记一条 `DistillRun`** 并立刻返回 job_id；
- 真正的提炼跑在 FastAPI `BackgroundTasks` 的线程池里（`run_distill_job`）；
- 前端按 `GET .../distill/jobs/{job_id}` 轮询看进度，离开页面再回来也能续上
  （`GET .../distill/jobs` 拉最近一条）。

## 三条硬约束

1. **绝不复用请求 session**：`get_db()` 给的 session 在响应返回时就关了，
   后台任务再用它是 use-after-close。这里一律 `SessionLocal()` 自己开、自己关
   ——与 `app.routers.generation` 的 SSE 独立 session 是同款做法。
2. **异常必须落库**：任何抛错都写 `status=failed` + `error`，绝不让任务永远
   停在 `running`。停在 running 的表现是「进度条一直转但不报错」，比失败更
   难排查，所以 `finally` 里还会兜一层 finished_at。
3. **进度回调只报进度，不碰提炼逻辑**：`progress(stage, current, total)` 只写
   `stage/progress_current/progress_total` 三列，不改采样、不改维度、不改
   provider 选择 —— 提炼产出必须与加进度之前逐字节一致。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AssistantWizardSession, DistillRun, Project, WizardNotice

logger = logging.getLogger(__name__)

#: 任务状态取值域（与 `DistillRun.status` 注释里的状态机一一对应）。
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"

#: 终态：轮询到此二者即可停表。
TERMINAL_STATUSES = (STATUS_SUCCESS, STATUS_FAILED)
#: 未终态：前端「恢复显示正在跑的任务」时认这两种。
ACTIVE_STATUSES = (STATUS_PENDING, STATUS_RUNNING)

# ---------------------------------------------------------------------------
# 看护判据（全部是**具体分钟数**，不写「一段时间」）
# ---------------------------------------------------------------------------
#
# 实测参照：文档里唯一一次计时是 764 块跑 42 分钟，约 3.3 秒/块。单块里包含
# 该档维度的多次 LLM 调用，慢模型/限流下单块几十秒到几分钟都出现过。所以
# 「无进度心跳」的阈值必须显著大于单块耗时，又不能长到用户干等一小时。
#
# - 排队超过 5 分钟还没被后台线程接手：BackgroundTasks 线程池满/进程异常，
#   正常提交几秒内就会进 running。
# - 运行中 15 分钟没有任何进度更新：≈ 单块正常耗时的 20 倍以上，基本可以判定
#   不是「这一块比较慢」，而是卡住了（LLM 挂死 / 数据库写锁 / 线程没了）。
# - 已用时超过 max(2.5 × 预计, 预计 + 30 分钟)：预计本身是外推值，2.5 倍还
#   没跑完就不是「估少了」，而是任务真的异常。
STALL_PENDING_MINUTES = 5
STALL_RUNNING_MINUTES = 15
OVERRUN_FACTOR = 2.5
OVERRUN_GRACE_MINUTES = 30

#: 心跳刷新 `stage` 文本的最小间隔（秒）。
#:
#: 为什么是 15：前提是「progress_at 每条必刷」，这里节流的只是那一句人话
#: （「已完成 37/92」）。界面上进度是 3 秒轮询一次，15 秒 ≈ 5 个轮询周期，
#: 用户能稳定看到计数在动；再密（<10 秒）只是同一个数字闪几下、白写一列
#: 255 字符；再疏（>30 秒）又容易在两次刷新之间看成「没变化」。
#: 首条与收尾（done == total）不受这个间隔限制，避免收尾停在「已完成 80/92」。
HEARTBEAT_STAGE_MIN_SECONDS = 15

#: 健康状态（**派生值，不入库**）：DB 里的 status 状态机保持
#: pending → running → success|failed 不变，这里只是给它叠一层「观感」。
HEALTH_QUEUED = "queued"
HEALTH_RUNNING = "running"
HEALTH_STALLED = "stalled"
HEALTH_SUCCESS = "success"
HEALTH_FAILED = "failed"

#: 进度回调签名：`(阶段人话文本, 已完成数, 总数)`。总数 0 = 还没探明。
ProgressCallback = Callable[[str, int, int], None]

#: `error` 列的截断上限。LLM 异常信息可能带整段 prompt 回显，不截断会把
#: 库撑大、也会让前端一条报错刷屏。4K 足够定位，且远小于 Text 列的实用上限。
_MAX_ERROR_LEN = 4000

#: 收割（reap）时写进 `error` 的原因。**必须说清「为什么」而不是只说「失败」**：
#: 用户看到自己的蒸馏挂了，第一反应是「是不是我的语料有问题 / 模型配错了」；
#: 真正的原因是进程没了，重跑一次即可。写错原因会让人去查根本不存在的配置问题。
REAPED_ERROR = (
    "服务重启中断：提炼跑在进程内的后台线程里，服务重启/重载后不会自动续跑。"
    "任务本身没有失败，已完成的块已保留——点「继续提炼」从断点接着跑。"
)


def _utcnow() -> datetime:
    """统一的时间源（UTC，tz-aware），与 `app.models._utcnow` 同口径。"""
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 字符串（前端直接用 `new Date(s)` 解析）。"""
    return dt.isoformat() if dt is not None else None


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite 读回来的是 naive datetime，统一按 UTC 补齐时区再参与计算。

    不做这一步，`now(tz-aware) - created_at(naive)` 会直接抛 TypeError，
    而这里一旦抛错，整个进度接口就 500——看护功能反而让状态页不可用。
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _minutes_since(dt: datetime | None, now: datetime) -> float | None:
    d = _aware(dt)
    if d is None:
        return None
    return max(0.0, (now - d).total_seconds() / 60.0)


def format_duration(seconds: float) -> str:
    """秒 → 人话时长（回执里用，避免「0.4 分钟」这种看不懂的写法）。"""
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total} 秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60} 分钟"


def distill_health(
    run: DistillRun, now: datetime | None = None
) -> tuple[str, str | None]:
    """把 `status + 时间戳` 派生成「排队 / 运行中 / 疑似卡住 / 完成 / 失败」。

    **不写库**：DB 的状态机仍是 pending → running → success|failed，四个值不变。
    「疑似卡住」是**按当前时间算出来的观感**，同一行在不同时刻可能从 running
    变成 stalled 再变回 running（进度又动了）——这正是不该持久化成 status 的原因。

    判据（具体分钟数见文件头常量）：
    - pending 超过 `STALL_PENDING_MINUTES` → stalled；
    - running 距最后一次进度心跳超过 `STALL_RUNNING_MINUTES` → stalled；
    - running 已用时超过 `max(OVERRUN_FACTOR × 预计, 预计 + 宽限)` → stalled；
    - 其余按 status 映射。

    Returns:
        `(health, note)`。note 是给用户看的一句人话，**带上具体分钟数**；
        没有可说的就是 None（例如成功）。
    """
    now = now or _utcnow()
    if run.status == STATUS_SUCCESS:
        return HEALTH_SUCCESS, None
    if run.status == STATUS_FAILED:
        return HEALTH_FAILED, run.error or None

    created = _aware(run.created_at)
    started = _aware(run.started_at)
    last_progress = _aware(run.progress_at) or started or created

    if run.status == STATUS_PENDING:
        waited = _minutes_since(created, now) or 0.0
        if waited >= STALL_PENDING_MINUTES:
            return (
                HEALTH_STALLED,
                f"已排队 {int(waited)} 分钟仍未被后台线程接手"
                f"（阈值 {STALL_PENDING_MINUTES} 分钟）。正常提交几秒内就会开始，"
                "建议重新提交一次。",
            )
        return HEALTH_QUEUED, f"已排队 {int(waited)} 分钟，等待后台线程接手。"

    idle = _minutes_since(last_progress, now) or 0.0
    elapsed = _minutes_since(started, now) or 0.0
    expected = int(run.expected_minutes or 0)

    if idle >= STALL_RUNNING_MINUTES:
        return (
            HEALTH_STALLED,
            f"{int(idle)} 分钟没有进度更新（阈值 {STALL_RUNNING_MINUTES} 分钟），"
            f"已跑 {int(elapsed)} 分钟；最后阶段：{run.stage or '未知'}。"
            # 不能建议「重新提交」：running 任务会被 start_distill 的
            # find_active_run 复用成同一个任务，重新提交等于没提交。
            # 真正的出口是重启后端 → 该任务被 reap 成「中断」→ 回执上再续跑。
            f"建议再等 {STALL_RUNNING_MINUTES} 分钟；仍不动就重启后端，"
            "重启后点「中断」回执上的「继续提炼」接着跑。",
        )

    if expected > 0:
        limit = max(OVERRUN_FACTOR * expected, expected + OVERRUN_GRACE_MINUTES)
        if elapsed > limit:
            return (
                HEALTH_STALLED,
                f"已跑 {int(elapsed)} 分钟，超过预计 {expected} 分钟的 "
                f"{OVERRUN_FACTOR} 倍阈值（{int(limit)} 分钟）。"
                "预计值本身是按字数外推、不是实测；建议再等一会，仍不动就重启后端，"
                "在「中断」回执上点「继续提炼」续跑（已完成的块不会重跑）。",
            )

    note = "刚启动" if elapsed < 1 else f"已运行 {int(elapsed)} 分钟"
    if expected > 0:
        note += f"，预计约 {expected} 分钟（按字数外推，非实测）"
    if run.progress_total:
        note += f"，已处理 {int(run.progress_current or 0)}/{int(run.progress_total)} 块"
    else:
        note += "，总块数还没探明"
    return HEALTH_RUNNING, note


def distill_run_to_dict(run: DistillRun) -> dict[str, Any]:
    """`DistillRun` 行 → API 状态对象（前端 `DistillJob` 与之逐字段对应）。

    出参**不带** `style_narrative` 全文：那份 JSON 有几万字符，1.5s 一次轮询
    传它是纯浪费。要看结论用这里的摘要字段就够了。

    `health / health_note` 是**派生字段**（不落库），见 `distill_health`。
    `progress_at` 是最后一次进度心跳，前端可以用它判断「这个百分比是不是刚更新过」。
    """
    now = _utcnow()
    health, health_note = distill_health(run, now)
    started = _aware(run.started_at)
    end = _aware(run.finished_at) or now
    elapsed_minutes = (
        int((end - started).total_seconds() // 60) if started is not None else None
    )
    return {
        "id": run.id,
        "project_id": run.project_id,
        "tier": run.tier,
        "status": run.status,
        "stage": run.stage,
        "progress_current": int(run.progress_current or 0),
        "progress_total": int(run.progress_total or 0),
        "n_chapters": run.n_chapters,
        "rule_count": run.rule_count,
        "rejected": run.rejected,
        "error": run.error,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        # —— 看护字段 ——
        "progress_at": _iso(run.progress_at),
        "expected_minutes": run.expected_minutes,
        "elapsed_minutes": elapsed_minutes,
        "health": health,
        "health_note": health_note,
    }


def create_distill_run(db: Session, project_id: int, tier: str,
                       kind: str = "narrative",
                       scope: dict | None = None,
                       novel_id: int | None = None) -> DistillRun:
    """登记一条提炼任务（pending），返回已 flush 的行（拿到 id）。

    只登记、不开工：开工由调用方把 `run_distill_job` 丢进 BackgroundTasks，
    这样 `POST /distill` 才能在毫秒级返回 job_id。

    `kind` 区分产物种类（narrative / book_digest）：两者共用同一张任务表，
    进度与轮询逻辑完全一样。`novel_id` 是「按书」的产物针对的那本书；
    叙事提炼一次蒸多本参考语料，没有单本书 ⇒ 留空。
    """
    run = DistillRun(
        project_id=project_id,
        tier=tier,
        kind=kind,
        scope_json=scope,
        novel_id=novel_id,
        status=STATUS_PENDING,
        stage="已排队，等待后台开始",
        progress_current=0,
        progress_total=0,
        created_at=_utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def find_active_run(db: Session, project_id: int,
                    kind: str = "narrative") -> DistillRun | None:
    """取该项目最近一条**未终态**任务（pending/running），没有则 None。

    用于「并发防重」：用户连点两次「开始提炼」不该烧两遍 LLM 钱。
    按 `kind` 分开防重：全书摘要与叙事提炼是两种产物，互不阻塞。
    """
    return db.scalar(
        select(DistillRun)
        .where(
            DistillRun.project_id == project_id,
            DistillRun.kind == kind,
            DistillRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(DistillRun.id.desc())
    )


def find_active_book_run(db: Session, novel_id: int) -> DistillRun | None:
    """取这本书最近一条**未终态**的全书摘要任务（没有则 None）。

    与 `find_active_run` 分开：叙事提炼按项目防重，
    全书摘要按**书**防重（同一本书在两个项目里各跑一次是纯浪费）。
    """
    return db.scalar(
        select(DistillRun)
        .where(
            DistillRun.novel_id == int(novel_id),
            DistillRun.kind == "book_digest",
            DistillRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(DistillRun.id.desc())
    )


def _make_progress_writer(job_id: int, db: Session) -> ProgressCallback:
    """造一个写回 `DistillRun` 的进度回调。

    用的是**任务自己的 session**（与提炼共用同一条连接），于是进度写入与档案
    落库在同一个事务节奏里，不会出现「进度显示 100% 但档案没写进去」的撕裂。

    回调内部**吞掉一切异常**：进度写失败（锁冲突/连接闪断）最多是进度条慢一拍，
    绝不能因此中断一次已经跑了半小时的提炼。
    """

    def _write(stage: str, current: int, total: int) -> None:
        try:
            run = db.get(DistillRun, job_id)
            if run is None:
                return
            run.stage = str(stage)[:255]
            run.progress_current = max(0, int(current))
            run.progress_total = max(0, int(total))
            # 心跳：看护判据「多久没有进度更新」唯一的时间源。
            # 必须在**每次**回调都刷新，否则进度明明在动也会被判成卡住。
            run.progress_at = _utcnow()
            db.commit()
        except Exception as exc:  # noqa: BLE001 —— 进度写失败不阻断提炼
            logger.warning("提炼进度写入失败 job_id=%s: %s", job_id, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return _write


def _make_heartbeat_writer(
    job_id: int, db: Session
) -> Callable[[int, int, str], None]:
    """造一个心跳回调：刷新 `progress_at`，并按间隔更新一句人话 `stage`。

    为什么需要它：叙事提炼是「一本语料的所有 prompt 一次性 gather」，块级进度
    要等整本书的请求都回来才写一次。一本大语料的 gather 可能十几二十分钟，
    期间进度表一动不动——只看块级进度会把**正在正常干活**的任务误判成卡住。

    回调入参是 `(本批已完成数, 本批总数, 阶段前缀)`：

    - `progress_at` **每条请求返回都刷**。卡住判据只看这一列，省掉它等于把
      看护弄坏；这里没有任何「攒够几条再写」的优化。
    - `stage` 只是顺带的人话（「语料 2/2：并发提炼中（已完成 37/92）」），
      按 `HEARTBEAT_STAGE_MIN_SECONDS` 节流：一批几十条调用眨眼间回来时，
      没必要把同一列反复改几十遍。
    - 首条与 `done == total` 强制写 stage：首条让用户立刻看到「开始了」，
      收尾保证不会停在「已完成 80/92」这种少一截的数字上（随后块级进度
      写入会接着把 stage 改成「提炼第 n/n 块」，不会卡住）。

    心跳失败一律吞掉：它只是观测，绝不能因为写库失败中断一次已经跑了半小时的提炼。
    """
    # 上次写 stage 文本的时刻（None = 还没写过）。只活在这次任务的进程内，
    # 不需要落库——重启后任务本来就会被收割（reap），不存在跨进程续用。
    last_stage_at: datetime | None = None

    def _beat(done: int, total: int, prefix: str) -> None:
        nonlocal last_stage_at
        try:
            run = db.get(DistillRun, job_id)
            if run is None:
                return
            now = _utcnow()
            # 硬要求：每条请求都刷新心跳，不受 stage 节流影响。
            run.progress_at = now
            elapsed = (
                (now - last_stage_at).total_seconds() if last_stage_at else None
            )
            if (
                elapsed is None  # 首条：立刻给人看到「已开始」
                or done >= total  # 收尾：保证最后一条不被节流吃掉
                or elapsed >= HEARTBEAT_STAGE_MIN_SECONDS
            ):
                run.stage = f"{prefix}（已完成 {done}/{total}）"[:255]
                last_stage_at = now
            db.commit()
        except Exception:  # noqa: BLE001 —— 心跳失败不阻断提炼
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return _beat


def _estimate_expected_minutes(
    db: Session, project: Project, tier_key: str
) -> int | None:
    """提交时按**语料字数**外推预计耗时（分钟）；算不出来就返回 None。

    与外推口径同源：`app.wizard_facts` 的唯一实测点（76.5 万字 → 42 分钟）。
    这里只用于判「超时倍数」，不写进任何产出，也**不是实测值**。
    """
    try:
        from app import wizard_facts as facts
        from app.style.narrative import resolve_tier, tier_with_sampling

        ids = [int(i) for i in (project.reference_novel_ids or [])]
        if not ids:
            return None
        books = facts.novel_stats(db, ids)
        cfg = tier_with_sampling(
            resolve_tier(tier_key), getattr(project, "narrative_sample_chapters", None)
        )
        chars = sum(
            facts.sampled_chars_for(cfg, b["chapters"], b["chars"]) for b in books
        )
        if chars <= 0:
            return None
        return int(facts.estimate_minutes(chars)["minutes"])
    except Exception:  # noqa: BLE001 —— 估算失败只让「超时倍数」判据失效
        logger.warning("预计耗时估算失败 project_id=%s tier=%s", project.id, tier_key)
        return None


# ---------------------------------------------------------------------------
# 持久化通知（完成回执 / 疑似卡住提醒）
# ---------------------------------------------------------------------------


def _next_prompt(db: Session, project_id: int) -> str:
    """回执末尾那句「下一步问什么」——按引导会话的真实状态决定，不猜。"""
    session = db.scalar(
        select(AssistantWizardSession)
        .where(AssistantWizardSession.project_id == project_id)
        .order_by(AssistantWizardSession.id.desc())
    )
    if session is not None and session.status == "active":
        # 注意：不能写「蒸馏在后台跑，跑完我会回报」——这句话出现在**回执本身**里，
        # 任务已经结束了，那样写会自相矛盾。
        return "引导还没走完；你可以接着回答刚才那一步，也可以直接说「开始写」。"
    stage = (session.result or {}).get("stage") if session and session.result else None
    if stage == "outline":
        return "接着问：大纲已经生成，现在开始写吗？"
    if stage in ("ready", "done"):
        return "接着问：要现在开始写吗？"
    return "接着问：要现在开始写，还是先去项目工作台核对文风档案？"


def _resume_progress(db: Session, run: DistillRun) -> tuple[int, int]:
    """中断任务「已保留多少块 / 一共多少块」，用于回执里的续跑提示。

    返回 `(已完成块数, 总块数)`。总块数取「心跳里估算的总数」与「已完成数」的
    较大者——估算偏小时也不能报出 `8/6` 这种自相矛盾的数字。
    任何一步算不出来都退化成 `(0, 心跳总数)`：回执里少一个数字可以接受，
    因为算不出来而发不出回执不行。
    """
    try:
        from app.style.narrative import (
            count_done_chunks,
            distill_resume_scope,
            resolve_tier,
            tier_with_sampling,
        )

        project = db.get(Project, run.project_id)
        if project is None:
            return 0, int(run.progress_total or 0)
        ids = [int(i) for i in (project.reference_novel_ids or [])]
        if not ids:
            return 0, int(run.progress_total or 0)
        cfg = resolve_tier(run.tier or getattr(project, "narrative_tier", None))
        cap = getattr(project, "narrative_sample_chapters", None)
        if cap is not None:
            cfg = tier_with_sampling(cfg, int(cap))
        done = count_done_chunks(db, project.id, distill_resume_scope(ids, cfg))
        return done, max(int(run.progress_total or 0), done)
    except Exception:  # noqa: BLE001 —— 回执不能因为算不出块数就发不出去
        logger.warning("续跑进度读取失败 job_id=%s", run.id)
        return 0, int(run.progress_total or 0)


def _terminal_payload(
    db: Session,
    run: DistillRun,
    kind: str,
    *,
    n_dimensions: int | None = None,
    failed_chunks: int = 0,
    interrupted: bool = False,
) -> dict[str, Any]:
    """终态回执：把用户要求的数字（章/规则/维度/失败块/实际耗时）逐项写全。"""
    started = _aware(run.started_at)
    finished = _aware(run.finished_at) or _utcnow()
    seconds = (finished - started).total_seconds() if started else 0
    elapsed_text = format_duration(seconds)

    from app.style.narrative import TIER_REGISTRY

    tier_cfg = TIER_REGISTRY.get(run.tier or "")
    payload: dict[str, Any] = {
        "job_id": run.id,
        "tier": run.tier,
        "tier_name": tier_cfg.name if tier_cfg else run.tier,
        "status": run.status,
        "n_chapters": run.n_chapters,
        "rule_count": run.rule_count,
        "rejected": run.rejected,
        "failed_chunks": failed_chunks,
        "n_dimensions": n_dimensions,
        "elapsed_seconds": int(round(seconds)),
        "elapsed_text": elapsed_text,
        "interrupted": interrupted,
    }

    if kind == "distill_done":
        bits = [f"处理 {run.n_chapters or 0} 章"]
        if n_dimensions:
            bits.append(f"{n_dimensions} 个维度")
        bits.append(f"提炼 {run.rule_count or 0} 条规则")
        if run.rejected:
            bits.append(f"拒伪 {int(run.rejected)} 条")
        bits.append(f"失败块 {int(failed_chunks)} 个")
        bits.append(f"实际用时 {elapsed_text}")
        head = "提炼完成" if not failed_chunks else "提炼完成（部分块失败）"
        payload["message"] = f"{head}：" + "，".join(bits) + "。"
    elif interrupted:
        # 中断要**留痕**（用户必须知道这次没跑完），但留痕不等于作废：已完成的
        # 块都在 `distill_chunks` 里。回执把「还能接着跑」说清楚，并带上可点
        # 「继续蒸馏」的标记；不做自动续跑（无人值守烧 LLM 费用且可能崩溃循环）。
        done, total = _resume_progress(db, run)
        payload["completed_chunks"] = done
        payload["total_chunks"] = total
        payload["resumable"] = done > 0
        if done and total:
            keep = (
                f"已保留 {done}/{total} 块，可从这里继续——点「继续提炼」接着跑，"
                "已完成的块不会重跑。"
            )
        elif done:
            keep = f"已保留 {done} 块，可从这里继续——点「继续提炼」接着跑。"
        else:
            keep = "这次还没跑完任何块，重新提交一次即可。"
        payload["message"] = (
            "提炼被服务重启中断：后台线程随进程一起没了，任务没有跑完。"
            f"中断前已跑 {elapsed_text}。{keep}"
        )
    else:
        err = (run.error or "未知错误").strip()
        payload["message"] = f"提炼失败：{err}。已用时 {elapsed_text}。"

    payload["next"] = _next_prompt(db, run.project_id)
    return payload


def emit_notice(
    db: Session, run: DistillRun, kind: str, payload: dict[str, Any]
) -> WizardNotice | None:
    """写一条通知（同一任务同一 type 只写一次）。失败不往外抛。

    去重靠 `(job_id, kind)` 唯一约束 + 先查后插；重复调用返回已有行。
    """
    try:
        existing = db.scalar(
            select(WizardNotice).where(
                WizardNotice.job_id == run.id, WizardNotice.kind == kind
            )
        )
        if existing is not None:
            return existing
        row = WizardNotice(
            project_id=run.project_id,
            job_id=run.id,
            kind=kind,
            payload=payload,
            created_at=_utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        # 同时进对话历史：用户关掉页面再回来，回执仍在对话流里（不是只活在前端内存）。
        # 幂等键用 notice:{id}，重复投递不会写第二条。
        try:
            from app.assistant_store import record_message

            record_message(
                db,
                row.project_id,
                role="assistant",
                kind="notice",
                text=str(payload.get("message") or ""),
                # notice_id / notice_kind 让前端在回放历史时能识别「这条回执
                # 已经出现过」，从而不把它再当作未消费通知补一遍。
                payload=dict(payload or {}, notice_id=row.id, notice_kind=kind),
                dedupe_key=f"notice:{row.id}",
            )
        except Exception:  # noqa: BLE001 —— 历史是增强，不能影响通知投递
            logger.warning("通知同步进对话历史失败 notice_id=%s", row.id)
        return row
    except Exception:  # noqa: BLE001 —— 通知写失败不该让任务本身失败
        logger.exception("提炼通知写入失败 job_id=%s kind=%s", run.id, kind)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _mark_failed(db: Session, job_id: int, exc: BaseException) -> None:
    """把任务置 failed + error。自身也不许往外抛（否则 finally 会被绕过）。"""
    try:
        db.rollback()  # 先清掉失败事务，否则后续 commit 会连带再炸一次
    except Exception:  # noqa: BLE001
        pass
    try:
        run = db.get(DistillRun, job_id)
        if run is None:
            return
        run.status = STATUS_FAILED
        run.stage = "提炼失败"
        run.error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("提炼失败状态写回也失败了 job_id=%s", job_id)


def reap_stale_runs(db: Session) -> int:
    """把所有还停在 pending / running 的任务收割成 failed，返回收割条数。

    ## 为什么必须有这一步

    提炼跑在 FastAPI `BackgroundTasks` 的线程池里 —— 那是**进程内**的线程。
    uvicorn reload、多 worker、服务重启、容器被杀，都会让执行线程凭空消失，
    而它登记的 `DistillRun` 行还留在库里、状态还是 `running`。

    后果不是「报错」，而是**进度条永远转**：前端每 1.5s 拉一次，每次都拿到
    running，用户只能干等一个永远不会结束的任务。这比直接失败难排查得多
    （没有任何错误信号），所以必须在启动时把这些行收掉。

    ## 为什么是 failed 而不是「继续跑」

    后台线程已经没了，没人能接着跑完它；把状态留成 running 等于撒谎。
    诚实的做法是标记失败 + 说明原因 + 提示「重跑一次即可」。

    ## 只碰未终态

    `status in (pending, running)` 是**唯一**的筛选条件：已 success / failed 的
    行绝不能被改写 —— 那是用户上一次提炼的结论，覆盖了就再也找不回来。

    Args:
        db: 调用方的 session（lifespan 里传独立 `SessionLocal()`）。

    Returns:
        被收割的条数（0 表示没有卡住的任务）。
    """
    now = _utcnow()
    stale = (
        db.scalars(
            select(DistillRun).where(DistillRun.status.in_(ACTIVE_STATUSES))
        )
        .all()
    )
    if not stale:
        return 0

    for run in stale:
        run.status = STATUS_FAILED
        run.stage = "服务重启中断"
        run.error = REAPED_ERROR
        # pending 的行可能还没来得及写 started_at；补上，否则「跑了多久」算不出来
        if run.started_at is None:
            run.started_at = now
        run.finished_at = now

    db.commit()

    # **重启恢复的关键一步**：这些任务是在跑到一半时被进程带走的，用户可能已经
    # 关掉页面。如果不落一条失败通知，等用户回来时只会看到一条历史 failed 行
    # （甚至被前端三分钟窗口过滤掉），"完成唤醒"就断了。这里补写一条
    # interrupted=True 的回执，前端下次轮询就能捡起来。
    for run in stale:
        emit_notice(
            db,
            run,
            "distill_failed",
            _terminal_payload(db, run, "distill_failed", interrupted=True),
        )

    count = len(stale)
    logger.info("启动时收割了 %s 条被中断的提炼任务（服务重启导致）", count)
    return count


def run_distill_job(
    job_id: int, project_id: int, tier: str, force: bool = False,
    content_hash: str | None = None, resume: bool = True,
) -> None:
    """后台提炼的执行体（跑在 FastAPI BackgroundTasks 的线程池里）。

    Args:
        job_id: `DistillRun.id`（已登记为 pending）。
        project_id: 目标项目。
        tier: **已归一化**的档位 key（调用方负责 `resolve_tier` 校验）。
        force: True 时忽略指纹缓存，同档位也重算一遍。
        content_hash: 调用方（`start_distill` 的缓存预测）已算好的
            `corpus_content_hash`。None 时由 `project_narrative` 自行计算——
            同一条提交链路里重复全量加载语料算哈希是纯浪费。
        resume: 是否复用 `distill_chunks` 里已完成的块（断点续跑）。默认 True：
            重新提交与「继续提炼」走同一条路，能复用就复用——同 scope 的块
            是同一份计算的中间结果，复用它不会改变结论。`force=True` 时强制关闭。

    成功：写 `status=success` + 摘要字段（n_chapters / rule_count / rejected）。
    失败：写 `status=failed` + `error`。**任何路径都会写 finished_at**——
    漏了它，前端「跑了多久」就永远算不出来，也没法区分「卡住」和「刚提交」。
    """
    from app.style.narrative import project_narrative

    db = SessionLocal()
    try:
        run = db.get(DistillRun, job_id)
        if run is None:
            logger.error("提炼任务行不存在 job_id=%s", job_id)
            return

        run.status = STATUS_RUNNING
        run.started_at = _utcnow()
        # 进入 running 也是一次心跳：否则「刚开工、还没探明块数」这段时间
        # 会被当成「从 created_at 开始就没动」，白白多扣掉排队时长。
        run.progress_at = run.started_at
        run.stage = "已受理，正在准备语料"
        run.progress_current = 0
        run.progress_total = 0
        run.error = None
        db.commit()

        project = db.get(Project, project_id)
        if project is None:
            raise ValueError(f"project {project_id} 不存在")
        if not (project.reference_novel_ids or []):
            raise ValueError("项目未选参考语料，无法提炼（请先选语料或走自由生成）")

        # 预计耗时：仅用于「超时倍数」判据，界面上必须标注「非实测」。
        run.expected_minutes = _estimate_expected_minutes(db, project, tier)
        db.commit()

        progress = _make_progress_writer(job_id, db)
        if (run.kind or "narrative") == "character_template":
            # 批 5 前置：人物塑造模板是独立产物（每块一次调用，约 1/8 的量），
            # 抽它不必重蒸整本档案，旧档案照旧有效。
            from app.style.narrative import extract_character_template

            row = extract_character_template(
                db, project, list(project.reference_novel_ids or []),
                tier=tier, progress=progress,
            )
            rules: dict = {}
            failed_chunks = 0
            run.status = STATUS_SUCCESS
            run.stage = "人物塑造模板已就绪"
            run.n_chapters = getattr(row, "n_chunks", None)
            run.rejected = getattr(row, "rejected", None)
            run.rule_count = len(getattr(row, "rules", None) or [])
        else:
            profile = project_narrative(
                db, project, force=force, tier=tier, progress=progress,
                content_hash=content_hash,
                heartbeat=_make_heartbeat_writer(job_id, db),
                resume=resume and not force,
            )
            if not isinstance(profile, dict):
                raise ValueError("提炼未产出档案（可能是档位不合法或语料为空）")

            rules = profile.get("rules") or {}
            # 部分块失败（LLM 超时/限流被跳过）：任务算成功，但如实写进 stage——
            # 「蒸馏完成」和「蒸馏完成（有块被跳过）」对用户是两件事。
            failed_chunks = int(profile.get("failed_chunks") or 0)
            if failed_chunks:
                logger.warning(
                    "提炼任务部分块失败 job_id=%s project_id=%s failed_chunks=%s",
                    job_id, project_id, failed_chunks,
                )
            run.status = STATUS_SUCCESS
            run.stage = (
                "提炼完成" if not failed_chunks
                else f"提炼完成（{failed_chunks} 个语料块调用失败，已跳过）"
            )
            run.n_chapters = profile.get("n_chapters")
            run.rejected = profile.get("rejected")
            run.rule_count = sum(
                len(v) for v in rules.values() if isinstance(v, list)
            )
        # 收尾对齐：命中缓存时进度是 (1,1)，正常跑完时 total 可能大于 current
        # （估算法会把未探明的余量算进去），统一收成 100% 免得显示 97% 却成功。
        if not run.progress_total or run.progress_current < run.progress_total:
            run.progress_total = max(run.progress_current, 1)
            run.progress_current = run.progress_total
        run.error = None
        # 终态也要收进 finished_at：回执里的「实际用时」用它，不能等到 finally
        # 再补——那样通知里会退化成「到今天为止」的时长。
        run.finished_at = _utcnow()
        run.progress_at = run.finished_at
        db.commit()

        # 完成唤醒：回执落库（不是只返回给当前页面）。用户关掉页面/服务重启后，
        # 前端轮询 notices 仍能把这条捡起来。
        n_dimensions = len(
            [k for k, v in rules.items() if isinstance(v, list) and v]
        )
        emit_notice(
            db,
            run,
            "distill_done",
            _terminal_payload(
                db,
                run,
                "distill_done",
                n_dimensions=n_dimensions,
                failed_chunks=failed_chunks,
            ),
        )
    except Exception as exc:  # noqa: BLE001 —— 后台任务不得往外抛
        logger.exception(
            "提炼任务失败 job_id=%s project_id=%s tier=%s", job_id, project_id, tier
        )
        _mark_failed(db, job_id, exc)
        run = db.get(DistillRun, job_id)
        if run is not None:
            emit_notice(
                db,
                run,
                "distill_failed",
                _terminal_payload(db, run, "distill_failed"),
            )
    finally:
        try:
            run = db.get(DistillRun, job_id)
            if run is not None and run.finished_at is None:
                run.finished_at = _utcnow()
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("提炼任务收尾时间戳写入失败 job_id=%s", job_id)
        finally:
            db.close()


def run_book_digest_job(
    job_id: int,
    project_id: int,
    novel_id: int,
    mode: str = "auto",
    target_ratio: float = 10.0,
    force: bool = False,
) -> None:
    """后台执行一次**全书摘要**（BookDigest）。

    与 `run_distill_job` 同款纪律：

    1. 自己开 session（不复用请求 session）；
    2. 任何异常都落 `failed` + `error`，绝不停在 `running`；
    3. 进度只报进度，不碰摘要逻辑。

    Args:
        job_id: `DistillRun.id`（已登记为 pending）。
        project_id: 触发这次摘要的项目（只用于归属与存在性校验）。
        novel_id: 要做摘要的语料。
        mode: 抽取档位（`auto` / `whole` / `retrieval`）。
        target_ratio: 压缩比目标（输出 ≤ 输入 / 该值）。
        force: True 时即便已有摘要也重算（并覆盖旧行）。
    """
    from app.style.digest import (
        build_book_digest,
        digest_content_hash,
        digest_plan,
        find_reusable_digest,
        save_digest,
    )
    from app.style.narrative import resolve_distill_provider

    db = SessionLocal()
    try:
        run = db.get(DistillRun, job_id)
        if run is None:
            logger.error("全书摘要任务行不存在 job_id=%s", job_id)
            return

        run.status = STATUS_RUNNING
        run.started_at = _utcnow()
        run.stage = "已受理，正在读取全书"
        run.progress_current = 0
        run.progress_total = 0
        run.error = None
        db.commit()

        if db.get(Project, project_id) is None:
            raise ValueError(f"project {project_id} 不存在")

        provider = resolve_distill_provider()
        progress = _make_progress_writer(job_id, db)

        # —— 复用判定必须**在花钱之前** ——
        # 旧顺序是「先 build（整本一次模型调用）→ 再看要不要复用」，
        # 于是「命中已有摘要，未重新抽取」是假的：模型跑完了、额度花了，只是没保存。
        # 现在先用 digest_plan() 把参数算出来（纯本地计算），拿它当复用键比对。
        plan = digest_plan(
            db, novel_id, provider=provider, mode=mode, target_ratio=target_ratio
        )
        content_hash = digest_content_hash(db, novel_id)
        if not force:
            existing = find_reusable_digest(db, novel_id, content_hash, plan["plan_key"])
            if existing is not None:
                # 同一本书、同一版正文、同一套参数 ⇒ 直接复用，不重算（幂等且不花钱）。
                payload = existing.payload or {}
                stats = existing.stats_json or {}
                run.status = STATUS_SUCCESS
                run.stage = "命中已有摘要，未重新抽取"
                run.n_chapters = len(plan["chapters"])
                run.rule_count = (
                    len(payload.get("timeline") or [])
                    + len(payload.get("characters") or [])
                    + len(payload.get("reactions") or [])
                )
                run.rejected = stats.get("dropped")
                run.progress_total = max(int(run.progress_total or 0), 1)
                run.progress_current = run.progress_total
                db.commit()
                return

        digest = build_book_digest(
            db, novel_id, provider=provider, progress=progress,
            target_ratio=target_ratio, plan=plan,
        )

        row = save_digest(db, digest, content_hash)

        run.status = STATUS_SUCCESS
        run.stage = "全书摘要已就绪"
        run.n_chapters = (digest.get("source") or {}).get("n_chapters")
        run.rule_count = _digest_entry_count(digest)
        run.rejected = (digest.get("stats") or {}).get("dropped")
        run.progress_total = max(int(run.progress_total or 0), 1)
        run.progress_current = run.progress_total
        run.error = None
        db.commit()
        logger.info("全书摘要完成 novel_id=%s digest_id=%s", novel_id, row.id)
    except Exception as exc:  # noqa: BLE001 —— 后台任务不得往外抛
        logger.exception(
            "全书摘要任务失败 job_id=%s novel_id=%s", job_id, novel_id
        )
        _mark_failed(db, job_id, exc)
    finally:
        try:
            run = db.get(DistillRun, job_id)
            if run is not None and run.finished_at is None:
                run.finished_at = _utcnow()
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("全书摘要任务收尾时间戳写入失败 job_id=%s", job_id)
        finally:
            db.close()


def _digest_entry_count(digest: dict) -> int:
    """摘要的条目总数（时间线 + 角色 + 反应）。"""
    return (
        len(digest.get("timeline") or [])
        + len(digest.get("characters") or [])
        + len(digest.get("reactions") or [])
    )
