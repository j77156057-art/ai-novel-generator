"""KG v3.0 读侧 API：检索切片 / 关系演变 / 人物情报 / 节奏 / 校验 / 作者豁免。

路由组挂在 /api/projects/{project_id}/kg-v3 下，全部只读（豁免是唯一写入）。
数据源是 v3.0 的 9 张表——由 batch_worker 异步批抽落库（挂生成主链后自动有数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import KgEvent, KgEventLink, Project

router = APIRouter(prefix="/api/projects/{project_id}/kg-v3", tags=["kg-v3"])


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    return proj


@router.get("/slice")
def get_slice(
    project_id: int,
    segment_no: int = Query(..., description="当前段号（按此动态取窗口）"),
    window: int = Query(5, ge=1, le=50),
    max_entities: int = Query(20, ge=1, le=100),
    characters: str | None = Query(None, description="逗号分隔的角色名，缺省用本段 mention"),
    db: Session = Depends(get_db),
) -> dict:
    """按当前剧情点取 KG 切片（generate 注入 prompt 的同款数据）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import slice_for_prompt

    chars = [c.strip() for c in characters.split(",")] if characters else None
    return slice_for_prompt(db, project_id, segment_no,
                            window=window, max_entities=max_entities,
                            active_characters=chars)


@router.get("/slice/prompt")
def get_slice_prompt(
    project_id: int,
    segment_no: int = Query(...),
    window: int = Query(5, ge=1, le=50),
    max_entities: int = Query(20, ge=1, le=100),
    characters: str | None = Query(None, description="逗号分隔的角色名，缺省用本段 mention"),
    db: Session = Depends(get_db),
) -> dict:
    """渲染成 prompt 文本（调试用：看 generate 实际注入什么）。

    参数与 /slice 完全对齐——两边参数不一致时，这里看到的和实际注入的
    就不是同一份切片（角色状态/情报块只在 active_characters 非空时渲染）。
    """
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import slice_for_prompt, format_slice_for_prompt

    chars = [c.strip() for c in characters.split(",")] if characters else None
    sl = slice_for_prompt(db, project_id, segment_no, window=window,
                          max_entities=max_entities, active_characters=chars)
    return {"text": format_slice_for_prompt(sl)}


@router.get("/relations/curve")
def get_relation_curve(
    project_id: int,
    from_name: str = Query(..., alias="from"),
    to_name: str = Query(..., alias="to"),
    db: Session = Depends(get_db),
) -> dict:
    """关系演变曲线（strength 历史）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import relation_curve

    return {"from": from_name, "to": to_name, "curve": relation_curve(db, project_id, from_name, to_name)}


@router.get("/knowledge")
def can_character_know(
    project_id: int,
    character: str = Query(...),
    fact: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    """人物情报追踪：character 此刻是否知道 fact。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import can_character_know as _check

    return {"character": character, "fact": fact, "knows": _check(db, project_id, character, fact)}


@router.get("/rhythm")
def get_rhythm(
    project_id: int,
    window: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """节奏分析：最近 N 段每实体的 mention 分布（谁在唱主角）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import rhythm_analysis

    return rhythm_analysis(db, project_id, window=window)


@router.get("/events")
def get_events(
    project_id: int,
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = Query(None),
    novel_id: int | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """事件列表（含因果链），按 sequence 降序。

    ``novel_id`` 缺省取项目当前书。事件表按书隔离，项目挂两本书时不能把
    别书的事件混进前端列表；未指定书（None）时才返回项目全部事件。
    """
    proj = _require_project(db, project_id)
    from app.models import KgEventLink

    book = novel_id if novel_id is not None else proj.active_novel_id
    q = select(KgEvent).where(
        KgEvent.project_id == project_id,
        KgEvent.is_current.is_(True),  # 同章旧稿事件只留档，不进渲染
    )
    if book is not None:
        q = q.where(KgEvent.novel_id.in_((book, None)))
    if event_type:
        q = q.where(KgEvent.event_type == event_type)
    events = db.scalars(q.order_by(KgEvent.sequence.desc()).limit(limit)).all()
    event_ids = {e.id for e in events}
    links = db.scalars(
        select(KgEventLink).where(
            KgEventLink.project_id == project_id,
            KgEventLink.from_event_id.in_(event_ids),
            KgEventLink.to_event_id.in_(event_ids),
        )
    ).all() if event_ids else []
    seq_to_id = {e.sequence: e.id for e in events}
    return {
        "events": [
            {
                "sequence": e.sequence, "chapter": e.chapter, "segment_no": e.segment_no,
                "event_type": e.event_type, "summary": e.summary,
                "in_story_date": e.in_story_date,
            }
            for e in sorted(events, key=lambda x: x.sequence)
        ],
        "links": [
            {"from_event_id": lk.from_event_id, "to_event_id": lk.to_event_id, "link_type": lk.link_type}
            for lk in links
        ],
    }


@router.get("/worldview")
def get_worldview(
    project_id: int,
    novel_id: int = Query(..., description="要出的书（raw_novels.id）"),
    db: Session = Depends(get_db),
) -> dict:
    """世界观清单：势力/组织、地点、体系与境界、资源与规则，各带证据章号。"""
    _require_project(db, project_id)
    from app.kg.story_context import worldview

    return worldview(db, project_id, novel_id)


@router.get("/timeline")
def get_timeline(
    project_id: int,
    novel_id: int = Query(..., description="要出的书（raw_novels.id）"),
    from_chapter: int = Query(1, ge=1),
    to_chapter: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    include_gaps: bool = Query(False, description="是否返回没有事件覆盖的章节空段"),
    db: Session = Depends(get_db),
) -> dict:
    """剧情时间线：按章排序的事件序列（可分页），每条给 who/where/what/result。"""
    _require_project(db, project_id)
    from app.kg.story_context import timeline

    return timeline(db, project_id, novel_id, from_chapter=from_chapter,
                    to_chapter=to_chapter, offset=offset, limit=limit,
                    include_gaps=include_gaps)


@router.get("/story-context")
def get_story_context(
    project_id: int,
    novel_id: int = Query(..., description="要续写的书（raw_novels.id）"),
    up_to_chapter: int = Query(..., ge=1, description="当前已写到第 N 章"),
    recent_events: int = Query(30, ge=1, le=200),
    top_characters: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """按章截取：此前发生了什么 / 哪些伏笔未回收 / 关键人物当前状态。"""
    _require_project(db, project_id)
    from app.kg.story_context import story_context

    return story_context(db, project_id, novel_id, up_to_chapter,
                         recent_events=recent_events,
                         top_characters=top_characters)


class ExemptRequest(BaseModel):
    """作者豁免请求体。"""
    finding_id: int | None = None
    rule: str
    entity: str = ""
    note: str


@router.post("/exemptions")
def mark_intentional(
    project_id: int,
    body: ExemptRequest,
    db: Session = Depends(get_db),
) -> dict:
    """作者豁免（Novelium mark as intentional）：用户认定某告警是故意的，Judge 以后不再报。

    豁免按 (rule, entity) 记录——同角色同规则不再告警。note 留痕。
    """
    _require_project(db, project_id)
    from app.models import Fact

    key = f"exempt:{body.rule}:{body.entity}"
    existing = db.scalar(select(Fact).where(Fact.project_id == project_id, Fact.key == key))
    if existing is None:
        db.add(Fact(
            project_id=project_id, type="exemption", key=key,
            props={"rule": body.rule, "entity": body.entity, "note": body.note,
                   "finding_id": body.finding_id},
            source="author", confidence="high",
        ))
        db.commit()
    return {"exempted": True, "rule": body.rule, "entity": body.entity}


@router.get("/exemptions")
def list_exemptions(project_id: int, db: Session = Depends(get_db)) -> dict:
    from app.models import Fact

    rows = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == "exemption"
    )).all()
    return {"exemptions": [
        {"rule": r.props.get("rule"), "entity": r.props.get("entity"),
         "note": r.props.get("note")} for r in rows
    ]}


@router.delete("/exemptions")
def revoke_exemption(
    project_id: int,
    rule: str = Query(..., description="要撤销的告警规则名"),
    entity: str = Query("", description="要撤销的实体名（可为空串）"),
    db: Session = Depends(get_db),
) -> dict:
    """撤销作者豁免：删掉对应 Fact(type=exemption)，Judge 恢复报告该告警。

    加错的豁免（规则名拼错/后悔了）必须有出口——永久静音比误报更糟。
    """
    _require_project(db, project_id)
    from app.models import Fact

    rows = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == "exemption"
    )).all()
    removed = 0
    for r in rows:
        p = r.props or {}
        if (p.get("rule") or "") == rule and (p.get("entity") or "") == entity:
            db.delete(r)
            removed += 1
    db.commit()
    return {"revoked": removed, "rule": rule, "entity": entity}


@router.get("/judge/findings")
def list_judge_findings(project_id: int, db: Session = Depends(get_db)) -> dict:
    """Judge 告警历史（落库的，按段号倒序）。只读，不触发重新判定。

    与哨兵 /alerts 同形状：items + by_severity + rules 清单——
    只看告警列表会以为「没告警=没问题」，实际有规则可能沉默。
    """
    _require_project(db, project_id)
    from app.kg.judge import RULES, load_judge_findings

    items = load_judge_findings(db, project_id)
    # 已豁免的 (rule, entity) 单独标出——历史清单里能看到「这条曾被豁免」
    from app.models import Fact

    exempt_rows = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == "exemption"
    )).all()
    exempt = {(r.props.get("rule") or "", r.props.get("entity") or "") for r in exempt_rows}
    for it in items:
        it["exempted"] = (it["rule"], it["entity"]) in exempt
    return {
        "items": items,
        "count": len(items),
        "by_severity": {
            s: sum(1 for i in items if i["severity"] == s) for s in ("err", "warn")
        },
        "rules": {name: {"severity": sev, "desc": desc} for name, (sev, desc) in RULES.items()},
    }


@router.get("/characters")
def list_characters(
    project_id: int,
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """项目角色清单（人物情报页的左列）。mention 降序。"""
    _require_project(db, project_id)
    from app.models import KgEntity

    rows = db.scalars(
        select(KgEntity)
        .where(KgEntity.project_id == project_id, KgEntity.kind == "character")
        .order_by(KgEntity.mention_count.desc())
        .limit(limit)
    ).all()
    return {"characters": [
        {
            "id": r.id, "name": r.canonical_name,
            "mention_count": r.mention_count or 0,
            "has_secrets": bool((r.attrs or {}).get("secrets")),
        }
        for r in rows
    ]}


@router.get("/relations")
def list_relations(
    project_id: int,
    limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict:
    """关系清单（关系图/列表数据源）。strength 降序。

    边带两端实体名与 kind（画图不用二次查）；自环边（from==to）丢弃。
    """
    _require_project(db, project_id)
    from app.models import KgEntity, KgRelation

    rows = db.scalars(
        select(KgRelation)
        .where(KgRelation.project_id == project_id,
               KgRelation.status == "active",  # 候选边（集合端点门降级）不进关系图
               KgRelation.is_current.is_(True))  # 同章旧稿关系只留档，不进图
        .order_by(KgRelation.strength.desc())
        .limit(limit)
    ).all()
    ents: dict[int, KgEntity] = {}
    out = []
    for r in rows:
        if r.from_id == r.to_id:
            continue
        a = ents.get(r.from_id) or db.get(KgEntity, r.from_id)
        b = ents.get(r.to_id) or db.get(KgEntity, r.to_id)
        if a is not None:
            ents[r.from_id] = a
        if b is not None:
            ents[r.to_id] = b
        if a is None or b is None:
            continue
        out.append({
            "from": a.canonical_name, "to": b.canonical_name,
            "from_kind": a.kind, "to_kind": b.kind,
            "rel_type": r.rel_type, "strength": r.strength,
        })
    return {"relations": out}


# --------------------------------------------------------------------------- #
# 批抽 worker 可观测性（AgentRun role=kg_batch_extract 的行）
# --------------------------------------------------------------------------- #

@router.get("/worker/status")
def get_worker_status(project_id: int, db: Session = Depends(get_db)) -> dict:
    """批抽 worker 状态：线程存活 / 队列积压 / 汇总计数 / 最近 50 行。

    没告警 ≠ 没问题：worker 线程死了服务照跑，KG 表只是停止生长——
    这个端点就是让「停止生长」可见。
    """
    _require_project(db, project_id)
    from app.kg.batch_worker import worker_status

    return worker_status(db, project_id)


#: error 文本 → 失败归类。**只是展示用的归类**，原始 error 一律原样返回，
#: 归类判断错了也不会丢证据。顺序有意义：渠道类标记先判，避免
#: 「未配置 API Key → 远端返回空 → 解析失败」被误归成纯解析问题。
_FAILURE_CHANNEL_MARKERS = (
    "渠道不可用", "未配置", "api key", "apikey", "unauthorized",
    "401", "403", "connection", "connect error",
)


def _classify_window_failure(error: str | None) -> str:
    """把窗口 error 归成 parse_failed / channel_unavailable / other。"""
    text = (error or "").strip()
    low = text.lower()
    if not low:
        return "other"
    if any(marker in low for marker in _FAILURE_CHANNEL_MARKERS):
        return "channel_unavailable"
    if low.startswith("parse_failed") or "无法从模型输出解析" in text:
        return "parse_failed"
    return "other"


@router.get("/corpus-windows")
def get_corpus_windows(
    project_id: int,
    novel_id: int | None = Query(None, description="只统计某本书；缺省为项目全部书"),
    db: Session = Depends(get_db),
) -> dict:
    """参考语料摄取的窗口进度聚合（只读）。

    ## 为什么需要这个出口

    `kg_corpus_windows` 是 corpus_ingest 的断点续跑账本，此前只有写侧。
    整批失败（例如没配渠道、本地模板兜底解析不出 JSON）时，KG 页面仍显示
    「还没有角色 / 窗口内无实体」，用户会误读成「本书没实体」，而不是
    「摄取根本没跑成」。

    返回口径：
    - `ingested=False`（窗口表为空）= **尚未摄取**，不是「没有实体」；
    - `failed>0` 时前端必须渲染失败状态条，不得退化成空数据文案；
    - `failed_reasons` 是按 error 文本做的展示归类，原始 error 在
      `failures[].error` 里保留；
    - `suspected_local_fallback` 统计 error 里带「离线兜底」的窗口数——
      这类失败本质是渠道不可用，但错误文本是解析失败。
    """
    _require_project(db, project_id)
    from app.models import KgCorpusWindow

    stmt = select(KgCorpusWindow).where(KgCorpusWindow.project_id == project_id)
    if novel_id is not None:
        stmt = stmt.where(KgCorpusWindow.novel_id == novel_id)
    rows = list(db.scalars(stmt.order_by(KgCorpusWindow.chapter_start)))

    counts = {"pending": 0, "running": 0, "success": 0, "failed": 0}
    failed_reasons = {"parse_failed": 0, "channel_unavailable": 0, "other": 0}
    failures: list[dict] = []
    suspected_local_fallback = 0
    last_run = None

    for row in rows:
        status = (row.status or "").lower()
        if status == "done":
            counts["success"] += 1
        elif status in ("pending", "running", "failed"):
            counts[status] += 1
        if row.status == "failed":
            reason = _classify_window_failure(row.error)
            failed_reasons[reason] += 1
            if "离线兜底" in (row.error or ""):
                suspected_local_fallback += 1
            failures.append({
                "window_key": row.window_key,
                "chapter_start": row.chapter_start,
                "chapter_end": row.chapter_end,
                "attempts": row.attempts,
                "reason": reason,
                "error": row.error,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            })
        stamp = row.updated_at or row.created_at
        if stamp is not None and (last_run is None or stamp > last_run):
            last_run = stamp

    return {
        "ingested": bool(rows),
        "total": len(rows),
        "counts": counts,
        # 顶层冗余一份关键计数，前端状态条不必再往下钻。
        "pending": counts["pending"],
        "running": counts["running"],
        "success": counts["success"],
        "failed": counts["failed"],
        "failed_reasons": failed_reasons,
        "suspected_local_fallback": suspected_local_fallback,
        "failures": failures[:50],
        "last_run_at": last_run.isoformat() if last_run else None,
    }


@router.get("/text-gate/no-generation")
def get_text_gate_no_generation(project_id: int,
                                db: Session = Depends(get_db)) -> dict:
    """写侧文本归属门的 no_generation 累计（不阻断，只留痕报警）。

    段号写错 / 写到不存在的段都会落进 no_generation；同项目累计超过阈值时
    ``AgentRun.note.text_gate_alert`` 置告警，本端点给出同一口径的汇总。
    """
    _require_project(db, project_id)
    from app.kg.batch_worker import text_gate_no_generation_summary

    return text_gate_no_generation_summary(db, project_id)


@router.get("/style-warnings")
def get_style_warnings(project_id: int, db: Session = Depends(get_db)) -> dict:
    """v2.1：文风告警历史（按 segment_no 倒序）。

    数据源：`projects.style_narrative.style_warnings[]`（engine 落库）。
    返回：{warnings, truncated, total}
    - `truncated=True` 表示已超过 500 条被截断（见 warning_store._MAX_HISTORY）
    """
    _require_project(db, project_id)
    from app.style import get_style_warnings as _read, is_truncated as _trunc
    project = db.get(Project, project_id)
    history = _read(project)
    return {
        "warnings": history,
        "total": len(history),
        "truncated": _trunc(project),
    }


class RequeueRequest(BaseModel):
    """重捞请求体：要重跑的 AgentRun id 列表（必须是本项目 failed 行）。"""

    run_ids: list[int]


@router.post("/worker/requeue")
def requeue_worker(project_id: int, body: RequeueRequest, db: Session = Depends(get_db)) -> dict:
    """重捞失败的段：从 Generation 回读正文重新入队，行重置 running。

    自动重试只覆盖网络抖动；持续挂（key 失效/配额耗尽）修复后由人手动重捞——
    毒批不自动重入队。**超时 running 行（stuck）也可用本端点重捞**：
    进程重启后内存队列丢失，库里只剩 running 行，不能只统计不处理。
    """
    _require_project(db, project_id)
    from app.kg.batch_worker import requeue_failed

    return requeue_failed(db, project_id, body.run_ids)


@router.post("/worker/recover")
def recover_worker(
    project_id: int,
    stuck_only: bool = Query(True, description="只重捞超过 STUCK_MINUTES 的 running 行"),
    db: Session = Depends(get_db),
) -> dict:
    """把停在 running 的批抽行重新入队（正文从 Generation 回读）。

    ``stuck_only=false`` 等同启动恢复语义：本项目所有 running 行都重入队
    （进程重启后内存队列一定为空，不必等 30 分钟超时）。
    正文缺失的行会被标 failed + error，不会静默跳过。
    """
    _require_project(db, project_id)
    from app.kg.batch_worker import ensure_worker, recover_running

    ensure_worker()
    return recover_running(db, project_id=project_id, stuck_only=stuck_only)


@router.post("/worker/backfill")
def backfill_worker(project_id: int, db: Session = Depends(get_db)) -> dict:
    """为「压根没有入队行」的已生成段主动补建 AgentRun 并入队。

    覆盖 ``requeue`` 够不到的一类：段早于入队功能生成（或入队行随进程/事务
    丢失），库里一行都没有。返回逐段 enqueued/skipped 明细；已存在行的段
    不会被覆盖（reason 带现有 status）。
    """
    _require_project(db, project_id)
    from app.kg.batch_worker import backfill_project, ensure_worker

    ensure_worker()
    return backfill_project(db, project_id)
