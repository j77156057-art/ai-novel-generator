"""高潮密度蒸馏端点（B6）：把 `plot_rhythm` 从「只有函数没有调用方」接上。

为什么做成**显式端点**而不是挂在生成主流程里：`compute_plot_rhythm` 每章一次
LLM 调用，600 章语料就是 600 次请求。生成是热路径，不能因为「第一次生成」就
把几百次调用塞进去；改成用户/向导显式触发，成本可见、可重跑。

写入的是 `projects.plot_rhythm`（JSON）：`avg_node_segments` 会被
`engine._timeline_state_for` 读取，决定首次 `plan_timeline` 的主干节点密度。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.rhythm import compute_plot_rhythm
from app.llm.provider import LocalFallbackLLMProvider
from app.models import Project
from app.routers.projects import _provider_or_none
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["rhythm"])

#: 没有可用渠道时的拒绝文案（与章纲起草同口径：说清原因与去哪配）。
RHYTHM_NO_CHANNEL = (
    "未配置可用的模型渠道：当前没有可用的 API Key，或所选渠道不可用。"
    "请到「模型设置」页选择渠道并填写 API Key 后重试；"
    "本地模板兜底只会把每章标成「日常」，得到的节奏参数没有意义。"
)

#: 单本书的采样上限护栏（端点上界；None 表示全量）。
MAX_SAMPLED_CHAPTERS = 200


class RhythmDistillIn(StrictRequest):
    """`novel_ids` 缺省取项目参考语料；`max_chapters` 是每本的等距采样上限。"""

    novel_ids: list[int] | None = None
    max_chapters: int | None = None


@router.post("/{project_id}/rhythm/distill")
def distill_rhythm(
    project_id: int, body: RhythmDistillIn, db: Session = Depends(get_db)
) -> dict:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")

    novel_ids = list(body.novel_ids or proj.reference_novel_ids or [])
    if not novel_ids:
        raise HTTPException(
            status_code=400,
            detail="项目没有参考语料：先导入语料并绑定到项目，或显式传 novel_ids",
        )
    if body.max_chapters is not None and not (1 <= body.max_chapters <= MAX_SAMPLED_CHAPTERS):
        raise HTTPException(
            status_code=400,
            detail=f"max_chapters 必须在 1–{MAX_SAMPLED_CHAPTERS} 之间（None = 全量）",
        )

    provider = _provider_or_none()
    if provider is None or isinstance(provider, LocalFallbackLLMProvider):
        raise HTTPException(status_code=503, detail=RHYTHM_NO_CHANNEL)

    rhythm = compute_plot_rhythm(
        db, proj, novel_ids, provider, max_chapters=body.max_chapters
    )
    if not rhythm:
        raise HTTPException(
            status_code=422,
            detail="参考语料没有可标注的章节（语料为空或尚未切章），未产出节奏参数",
        )

    proj.plot_rhythm = rhythm
    db.add(proj)
    db.commit()
    db.refresh(proj)

    return {
        "project_id": proj.id,
        "version": rhythm["version"],
        "avg_node_segments": rhythm["avg_node_segments"],
        "branch_ratio": rhythm["branch_ratio"],
        "sampled_chapters": rhythm["sampled_chapters"],
        "max_chapters": rhythm["max_chapters"],
        "chapter_count_per_novel": rhythm["chapter_count_per_novel"],
        "distilled_at": rhythm["distilled_at"],
    }
