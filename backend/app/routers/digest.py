"""全书摘要（BookDigest）的端点：提交、查看、验收。

## 为什么摘要的「提交」是项目级，而产物是书级

- **产物按书存**（`book_digests`）：一本书的摘要与哪个项目无关，
  任何项目都能复用 —— 这正是它替代「整本进上下文」的价值所在；
- **提交挂在项目下**（`POST /api/projects/{id}/digests`）：后台任务表
  （`distill_runs`）的项目列是必填的，任务进度也天然属于「某个项目的操作」。

设计稿 §6 写的是 `POST /api/books/{novel_id}/digest`；实现改成项目级提交 +
书级查询，**效果完全一样**（提交时仍要指定是哪本书），但能复用既有的任务表与轮询。
文档已同步（`docs/final-distillation-design.md` §6）。

## 三个端点的分工

- `POST /api/projects/{id}/digests`：提交一次摘要（后台跑，立即返回 job_id）；
- `GET /api/books/{novel_id}/digest`：取这本书最新的摘要（含渲染文本）；
- `GET /api/books/{novel_id}/digest/verify`：取三项验收的结论
  （压缩比 / 主线覆盖 / 角色覆盖 / 证据缺失率）。

**没有摘要时返回 404** 而不是空对象：空对象会被前端当成「有摘要但没内容」，
而这两种情况要走的路完全不同（前者是「去跑一次」，后者是「内容坏了」）。
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Project, RawNovel
from app.schemas import StrictRequest

router = APIRouter(prefix="/api", tags=["digest"])


class DigestStartRequest(StrictRequest):
    """`POST /api/projects/{id}/digests` 的请求体。

    继承 `StrictRequest`：拼错字段立刻 422，而不是静默按默认值跑一次
    分钟级的模型调用。
    """

    #: 要做摘要的语料（raw_novels.id）。
    novel_id: int
    #: 抽取档位：`auto`（按书大小与渠道能力自动选）/ `whole` / `retrieval`。
    mode: str = "auto"
    #: 压缩比目标（输出 ≤ 输入 / 该值）。默认 10:1。
    target_ratio: float = 10.0
    #: True = 忽略已有摘要强制重算。
    force: bool = False


@router.post("/projects/{project_id}/digests")
def start_digest(
    project_id: int,
    body: DigestStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """提交一次全书摘要：**立即返回** job_id，摘要在后台跑。

    摘要要读整本书、调模型、要几十秒到几分钟 ⇒ 必须走后台任务
    （与文风蒸馏同一套 `distill_runs` 表与轮询协议）。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    novel = db.get(RawNovel, body.novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="语料不存在")

    from app.style.digest import MODE_AUTO, MODE_RETRIEVAL, MODE_WHOLE
    from app.style.jobs import create_distill_run, find_active_book_run

    if body.mode not in (MODE_AUTO, MODE_WHOLE, MODE_RETRIEVAL):
        raise HTTPException(
            status_code=400,
            detail=f"mode={body.mode!r} 不认识；可选：{MODE_AUTO} / {MODE_WHOLE} / {MODE_RETRIEVAL}",
        )
    if not (1.0 <= body.target_ratio <= 100.0):
        raise HTTPException(status_code=400, detail="target_ratio 必须在 1~100 之间")

    # 并发防重：同一本书已有在跑的摘要就别再起一条（那是一次模型调用级别的开销）。
    active = find_active_book_run(db, body.novel_id)
    if active is not None and not body.force:
        return {
            "job_id": active.id,
            "status": active.status,
            "novel_id": body.novel_id,
            "reused": True,
        }

    run = create_distill_run(
        db, project_id, "full", kind="book_digest", novel_id=body.novel_id
    )
    background_tasks.add_task(
        _run_digest,
        run.id,
        project_id,
        body.novel_id,
        body.mode,
        body.target_ratio,
        body.force,
    )
    return {
        "job_id": run.id,
        "status": run.status,
        "novel_id": body.novel_id,
        "reused": False,
    }


def _run_digest(
    job_id: int,
    project_id: int,
    novel_id: int,
    mode: str,
    target_ratio: float,
    force: bool,
) -> None:
    """把后台任务丢给 `jobs.run_book_digest_job`（薄封装，便于测试替换）。"""
    from app.style.jobs import run_book_digest_job

    run_book_digest_job(job_id, project_id, novel_id, mode, target_ratio, force)


@router.get("/books/{novel_id}/digest")
def get_digest(novel_id: int, db: Session = Depends(get_db)) -> dict:
    """取这本书最新的摘要（含渲染后的文本）。

    没有摘要时 404 —— 空对象会被前端当成「有摘要但内容为空」。
    """
    from app.style.digest import digest_summary, latest_digest

    row = latest_digest(db, novel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="这本书还没有摘要")
    out = digest_summary(row)
    out["payload"] = row.payload or {}
    out["text"] = row.text or ""
    return out


@router.get("/books/{novel_id}/digest/verify")
def get_digest_verify(novel_id: int, db: Session = Depends(get_db)) -> dict:
    """取三项验收的结论（压缩比 / 主线覆盖 / 角色覆盖 / 证据缺失率）。"""
    from app.style.digest import latest_digest

    row = latest_digest(db, novel_id)
    if row is None:
        raise HTTPException(status_code=404, detail="这本书还没有摘要")
    return {
        "novel_id": novel_id,
        "digest_id": row.id,
        "passed": bool((row.verify_json or {}).get("passed")),
        "checks": (row.verify_json or {}).get("checks") or {},
        "stats": row.stats_json or {},
    }
