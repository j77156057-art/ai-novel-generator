"""文风蒸馏后台任务（`DistillRun`）的**执行层**。

## 为什么要有这一层

蒸馏是**分钟级**的长任务（833 章语料跑完整档实测 42 分钟，见
`app.style.narrative` 档位注释里的唯一那处计时）。HTTP 请求挂不了这么久：
代理会超时、前端会白屏、用户刷新一下页面就什么都没了。

所以蒸馏拆成三步：

- `POST /api/projects/{id}/distill` 只**登记一条 `DistillRun`** 并立刻返回 job_id；
- 真正的蒸馏跑在 FastAPI `BackgroundTasks` 的线程池里（`run_distill_job`）；
- 前端按 `GET .../distill/jobs/{job_id}` 轮询看进度，离开页面再回来也能续上
  （`GET .../distill/jobs` 拉最近一条）。

## 三条硬约束

1. **绝不复用请求 session**：`get_db()` 给的 session 在响应返回时就关了，
   后台任务再用它是 use-after-close。这里一律 `SessionLocal()` 自己开、自己关
   ——与 `app.routers.generation` 的 SSE 独立 session 是同款做法。
2. **异常必须落库**：任何抛错都写 `status=failed` + `error`，绝不让任务永远
   停在 `running`。停在 running 的表现是「进度条一直转但不报错」，比失败更
   难排查，所以 `finally` 里还会兜一层 finished_at。
3. **进度回调只报进度，不碰蒸馏逻辑**：`progress(stage, current, total)` 只写
   `stage/progress_current/progress_total` 三列，不改采样、不改维度、不改
   provider 选择 —— 蒸馏产出必须与加进度之前逐字节一致。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import DistillRun, Project

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

#: 进度回调签名：`(阶段人话文本, 已完成数, 总数)`。总数 0 = 还没探明。
ProgressCallback = Callable[[str, int, int], None]

#: `error` 列的截断上限。LLM 异常信息可能带整段 prompt 回显，不截断会把
#: 库撑大、也会让前端一条报错刷屏。4K 足够定位，且远小于 Text 列的实用上限。
_MAX_ERROR_LEN = 4000

#: 收割（reap）时写进 `error` 的原因。**必须说清「为什么」而不是只说「失败」**：
#: 用户看到自己的蒸馏挂了，第一反应是「是不是我的语料有问题 / 模型配错了」；
#: 真正的原因是进程没了，重跑一次即可。写错原因会让人去查根本不存在的配置问题。
REAPED_ERROR = (
    "服务重启中断：蒸馏跑在进程内的后台线程里，服务重启/重载后不会自动续跑。"
    "任务本身没有失败，重新提交一次即可。"
)


def _utcnow() -> datetime:
    """统一的时间源（UTC，tz-aware），与 `app.models._utcnow` 同口径。"""
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    """datetime → ISO 字符串（前端直接用 `new Date(s)` 解析）。"""
    return dt.isoformat() if dt is not None else None


def distill_run_to_dict(run: DistillRun) -> dict[str, Any]:
    """`DistillRun` 行 → API 状态对象（前端 `DistillJob` 与之逐字段对应）。

    出参**不带** `style_narrative` 全文：那份 JSON 有几万字符，1.5s 一次轮询
    传它是纯浪费。要看结论用这里的摘要字段就够了。
    """
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
    }


def create_distill_run(db: Session, project_id: int, tier: str) -> DistillRun:
    """登记一条蒸馏任务（pending），返回已 flush 的行（拿到 id）。

    只登记、不开工：开工由调用方把 `run_distill_job` 丢进 BackgroundTasks，
    这样 `POST /distill` 才能在毫秒级返回 job_id。
    """
    run = DistillRun(
        project_id=project_id,
        tier=tier,
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


def find_active_run(db: Session, project_id: int) -> DistillRun | None:
    """取该项目最近一条**未终态**任务（pending/running），没有则 None。

    用于「并发防重」：用户连点两次「开始蒸馏」不该烧两遍 LLM 钱。
    """
    return db.scalar(
        select(DistillRun)
        .where(
            DistillRun.project_id == project_id,
            DistillRun.status.in_(ACTIVE_STATUSES),
        )
        .order_by(DistillRun.id.desc())
    )


def _make_progress_writer(job_id: int, db: Session) -> ProgressCallback:
    """造一个写回 `DistillRun` 的进度回调。

    用的是**任务自己的 session**（与蒸馏共用同一条连接），于是进度写入与档案
    落库在同一个事务节奏里，不会出现「进度显示 100% 但档案没写进去」的撕裂。

    回调内部**吞掉一切异常**：进度写失败（锁冲突/连接闪断）最多是进度条慢一拍，
    绝不能因此中断一次已经跑了半小时的蒸馏。
    """

    def _write(stage: str, current: int, total: int) -> None:
        try:
            run = db.get(DistillRun, job_id)
            if run is None:
                return
            run.stage = str(stage)[:255]
            run.progress_current = max(0, int(current))
            run.progress_total = max(0, int(total))
            db.commit()
        except Exception as exc:  # noqa: BLE001 —— 进度写失败不阻断蒸馏
            logger.warning("蒸馏进度写入失败 job_id=%s: %s", job_id, exc)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass

    return _write


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
        run.stage = "蒸馏失败"
        run.error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_LEN]
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("蒸馏失败状态写回也失败了 job_id=%s", job_id)


def reap_stale_runs(db: Session) -> int:
    """把所有还停在 pending / running 的任务收割成 failed，返回收割条数。

    ## 为什么必须有这一步

    蒸馏跑在 FastAPI `BackgroundTasks` 的线程池里 —— 那是**进程内**的线程。
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
    行绝不能被改写 —— 那是用户上一次蒸馏的结论，覆盖了就再也找不回来。

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
    count = len(stale)
    logger.info("启动时收割了 %s 条被中断的蒸馏任务（服务重启导致）", count)
    return count


def run_distill_job(
    job_id: int, project_id: int, tier: str, force: bool = False
) -> None:
    """后台蒸馏的执行体（跑在 FastAPI BackgroundTasks 的线程池里）。

    Args:
        job_id: `DistillRun.id`（已登记为 pending）。
        project_id: 目标项目。
        tier: **已归一化**的档位 key（调用方负责 `resolve_tier` 校验）。
        force: True 时忽略指纹缓存，同档位也重算一遍。

    成功：写 `status=success` + 摘要字段（n_chapters / rule_count / rejected）。
    失败：写 `status=failed` + `error`。**任何路径都会写 finished_at**——
    漏了它，前端「跑了多久」就永远算不出来，也没法区分「卡住」和「刚提交」。
    """
    from app.style.narrative import project_narrative

    db = SessionLocal()
    try:
        run = db.get(DistillRun, job_id)
        if run is None:
            logger.error("蒸馏任务行不存在 job_id=%s", job_id)
            return

        run.status = STATUS_RUNNING
        run.started_at = _utcnow()
        run.stage = "已受理，正在准备语料"
        run.progress_current = 0
        run.progress_total = 0
        run.error = None
        db.commit()

        project = db.get(Project, project_id)
        if project is None:
            raise ValueError(f"project {project_id} 不存在")
        if not (project.reference_novel_ids or []):
            raise ValueError("项目未选参考语料，无法蒸馏（请先选语料或走自由生成）")

        progress = _make_progress_writer(job_id, db)
        profile = project_narrative(
            db, project, force=force, tier=tier, progress=progress
        )
        if not isinstance(profile, dict):
            raise ValueError("蒸馏未产出档案（可能是档位不合法或语料为空）")

        rules = profile.get("rules") or {}
        run.status = STATUS_SUCCESS
        run.stage = "蒸馏完成"
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
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— 后台任务不得往外抛
        logger.exception(
            "蒸馏任务失败 job_id=%s project_id=%s tier=%s", job_id, project_id, tier
        )
        _mark_failed(db, job_id, exc)
    finally:
        try:
            run = db.get(DistillRun, job_id)
            if run is not None and run.finished_at is None:
                run.finished_at = _utcnow()
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("蒸馏任务收尾时间戳写入失败 job_id=%s", job_id)
        finally:
            db.close()
