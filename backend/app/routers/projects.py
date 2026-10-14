"""项目 + 知识图谱路由：建项目 → 分步更新 → 建图谱 → 查询角色/坑 → 生成前提醒 → 导出txt。"""

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.engine import _style_info
from app.kg.build import build_graph
from app.kg.queries import get_character, get_graph, get_reminders
from app.models import Generation, Project
from app.schemas import ProjectCreate, ProjectOut, ProjectPatch

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    proj = Project(
        title=body.title,
        target_length=body.target_length,
        mode=body.mode,
        style_dims=body.style_dims,
        outline=body.outline,
        status="created",
        genre=body.genre,
        style=body.style,
        premise=body.premise,
        reference_novel_ids=body.reference_novel_ids,
        generation_length=body.generation_length,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    """列出全部项目（服务端为单一事实来源；前端不再依赖 localStorage 缓存）。"""
    projects = list(db.scalars(select(Project).order_by(Project.id.desc())).all())
    for p in projects:
        p.segment_count = db.scalar(
            select(func.count(Generation.id)).where(Generation.project_id == p.id)
        ) or 0
    return projects


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


@router.patch("/{project_id}", response_model=ProjectOut)
def patch_project(
    project_id: int, body: ProjectPatch, db: Session = Depends(get_db)
) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(proj, field, value)
    db.commit()
    db.refresh(proj)
    return proj


@router.post("/{project_id}/build-kg")
def build_kg(
    project_id: int,
    novel_id: int | None = Query(default=None),
    all_corpora: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return build_graph(db, project_id, novel_id, all_corpora=all_corpora)


@router.post("/{project_id}/build-narrative")
def build_narrative(
    project_id: int,
    force: bool = Query(default=False),
    max_chapters: int = Query(default=30),
    db: Session = Depends(get_db),
) -> dict:
    """提炼叙事档案（LLM 从参考语料提炼 L2/L3 叙事规则，附证据账本）。

    幂等：已存且指纹一致（source_ids + narrative_version）则直接返回缓存。
    force=True 强制重算。这是**一次性成本**——提炼结果落库到
    `Project.style_narrative`，后续生成直接读缓存，不再调 LLM。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    ids = [int(i) for i in (proj.reference_novel_ids or [])]
    if not ids:
        raise HTTPException(status_code=400, detail="项目未选参考语料，无法提炼叙事档案")
    from app.style.narrative import project_narrative
    profile = project_narrative(db, proj, force=force, max_chapters=max_chapters)
    if profile is None:
        raise HTTPException(status_code=500, detail="叙事档案提炼失败")
    return {
        "version": profile.get("version"),
        "source_ids": profile.get("source_ids"),
        "n_chapters": profile.get("n_chapters"),
        "rejected": profile.get("rejected"),
        "rule_count": sum(len(v) for v in (profile.get("rules") or {}).values()),
        "dimensions": {
            dim: len(items)
            for dim, items in (profile.get("rules") or {}).items()
            if items
        },
    }


@router.get("/{project_id}/kg")
def kg(
    project_id: int,
    type_: str = Query(default="all"),
    source: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return get_graph(db, project_id, type_, source=source)


@router.get("/{project_id}/kg/character/{name}")
def character(project_id: int, name: str, db: Session = Depends(get_db)) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    result = get_character(db, project_id, name)
    if result is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return result


@router.get("/{project_id}/reminders")
def reminders(
    project_id: int,
    source: str = Query(default="generated"),
    db: Session = Depends(get_db),
) -> dict:
    """生成前提醒（未填坑 / 角色约束）。

    `source` 与 /kg 对齐，但**默认值刻意不同**：/kg 默认 `None`＝全量图谱（供浏览），
    这里默认 `"generated"`＝本次创作自己的图谱。原因是这两个端点服务的目标相反：

    - 提醒会注入提示词去约束**接下来写什么**，参考语料的角色/悬念与之无关，
      混进来就是污染（P0-1 修的就是这个：默认不限 source 时林黛玉会进生成约束）；
    - 图谱是给人看的，默认看全部才符合「先建图再浏览」的心智。

    要查参考语料那一侧的提醒，显式传 `?source=novel:{id}`。
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    # top_k=None：人工查阅的 API 要完整列出全部角色/伏笔；封顶只发生在生成路径
    # （app.kg.queries.get_reminders 的默认 REMINDER_TOP_K），避免每轮注入随账本膨胀。
    return get_reminders(db, project_id, source=source, top_k=None)


@router.get("/{project_id}/export-txt")
def export_txt(project_id: int, db: Session = Depends(get_db)) -> PlainTextResponse:
    """把所有已生成段落拼接成一份 txt 文档，直接触发浏览器下载。

    正文衔接靠段级总结链（短摘要）而非重复搬运全文，保证长文连贯且不被会话压缩。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no)
    ).all()
    body = "\n\n".join((g.text or "") for g in gens).strip()
    if not body:
        raise HTTPException(status_code=400, detail="尚无生成内容，无法导出")

    mode_label = "wayfinder 追问构思" if (proj.mode or "quick") == "expert" else "直接构思"
    header = (
        f"《{proj.title or '未命名'}》\n"
        f"类型：{proj.genre or '未指定'} · 风格：{proj.style or '未指定'} · "
        f"构思方式：{mode_label} · 共 {len(gens)} 段\n"
        f"{'—' * 24}\n\n"
    )
    content = header + body
    # 文件名可能含中文：ASCII 回退名 + RFC 5987 UTF-8 名，避免 HTTP 头 latin-1 编码报错
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", proj.title or "novel")
    ascii_name = re.sub(r'[^\x00-\x7f]+', "", safe) or "novel"
    utf_name = quote(f"{safe}.txt", safe="")
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}.txt"; filename*=UTF-8\'\'{utf_name}'
        },
    )
