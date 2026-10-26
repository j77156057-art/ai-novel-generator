"""M5 创作向导路由：选语料 → 类型/风格 → 内容 → quick/expert 构思 → 生成后建图。

提案和追问保留旧离线模板；圣经和大纲显式报告模型失败，保留旧规划，
成功产物也仅为待复核草案。
"""

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.kg.build import build_graph_from_text
from app.llm.provider import get_llm_provider
from app.models import Generation, NovelChunk, Project
from app.generation.planning import (OutlineRequest, OutlineApply, SYSTEM, source_fingerprint,
    validate_nodes, validate_planning_candidate, build_clarification_report, timeline_for,
    OutlineClarify, submit_clarification_candidate, BudgetRevision, model_json)
from app.schemas import (
    ExpertQuestionOut,
    PlotBibleOut,
    PremiseProposalOut,
    StrictRequest,
)

router = APIRouter(prefix="/api/projects", tags=["wizard"])

_GENRE_HINT = {
    "科幻": "硬核设定、近未来或远未来、技术与人的张力",
    "都市": "现代城市、职场/悬疑/情感、接地气的日常与冲突",
    "悬疑": "谜团驱动、反转、不可靠叙事、真相延迟揭示",
    "言情": "情感关系主线、人物羁绊、细腻心理",
    "奇幻": "架空世界、魔法/种族/史诗、宏大设定",
    "仙侠": "修真飞升、门派恩怨、从凡到仙的成长",
    "历史": "真实或架空的过去、权谋、时代洪流下的个人",
    "游戏": "系统/副本/升级、爽文节奏、数值化成长",
}
_STYLE_HINT = {
    "番茄": "快节奏、强钩子、口语化、每章留悬念、信息密度高",
    "起点": "设定严谨、长线伏笔、世界观完整、热血或深沉",
    "晋江": "情感细腻、人物内心、CP 张力、文风清新或华丽",
    "豆瓣阅读": "文学质感、现实向、克制、留白与隐喻",
    "知乎盐选": "悬疑短钩子、第一人称代入、反转结尾",
}


class ExpertAnswersIn(StrictRequest):
    answers: dict[str, str] | None = None  # question -> answer
    preview: bool = False


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _genre_style_text(proj: Project) -> str:
    g = (proj.genre or "未指定").strip()
    s = (proj.style or "未指定").strip()
    gh = _GENRE_HINT.get(g, "")
    sh = _STYLE_HINT.get(s, "")
    return (
        f"类型：{g}{('（' + gh + '）') if gh else ''}\n"
        f"风格平台：{s}{('（' + sh + '）') if sh else ''}"
    )


def _style_sample(db: Session, novel_ids: list[int] | None, per: int = 4) -> tuple[str, dict]:
    """抽取选定参考语料的开篇若干分片，作为风格样本（只取风格纹理，不混整库）。

    逐片做**消费清洗**；带**未决残留告警**的片段（平台广告行等）**整片跳过** ——
    既不静默改字（那是扩大屏蔽），也不把广告行喂进提示词。

    返回 (样本文本, {'chunks': 用了几片, 'skipped': 跳过几片, 'markers': 跳过的标记}）。
    """
    info = {"chunks": 0, "skipped": 0, "markers": []}
    if not novel_ids:
        return "", info
    from app.ingestion.clean import clean_consumption_text

    out: list[str] = []
    for nid in novel_ids[:5]:
        chunks = db.scalars(
            select(NovelChunk)
            .where(NovelChunk.novel_id == nid)
            .order_by(NovelChunk.id)
            .limit(per)
        ).all()
        for c in chunks:
            cleaned, report = clean_consumption_text(c.text or "")
            unresolved = report.get("unresolved") or []
            if unresolved:
                info["skipped"] += 1
                for item in unresolved[:3]:
                    marker = str(item.get("marker") or "")
                    if marker and marker not in info["markers"]:
                        info["markers"].append(marker[:40])
                continue
            text = cleaned.strip()
            if not text:
                continue
            out.append(text)
            info["chunks"] += 1
    return "\n---\n".join(out), info


def _has_real_llm() -> bool:
    if settings.llm_provider == "openrouter" and settings.openrouter_api_key:
        return True
    if settings.llm_provider == "dashscope" and settings.dashscope_api_key:
        return True
    if settings.llm_provider == "zhipu" and settings.zhipu_api_key:
        return True
    if settings.llm_provider == "siliconflow" and settings.siliconflow_api_key:
        return True
    if settings.llm_provider == "minimax" and settings.minimax_api_key:
        return True
    return False


def _strip_json_block(text: str) -> str:
    text = text.strip()
    # MiniMax M2 系列默认吐思维链：剥掉  包裹的推理内容，只留正文
    if " <think>" in text or " </think>" in text:
        end = text.rfind(" </think>")
        text = text[end + len(" </think>"):].strip() if end != -1 else text
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        return m.group(1).strip()
    # 退一步：截取第一个 { 到最后一个 } 之间的内容
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        return text[s : e + 1]
    return text


def _safe_parse(text: str, default: dict) -> dict:
    try:
        return json.loads(_strip_json_block(text))
    except Exception:  # noqa: BLE001
        return default


def _coerce_string_list(data: dict | None, key: str, limit: int) -> list[str]:
    """从模型 JSON 中抽取干净的字符串列表，兼容两类常见偏差：

    - 标准：  {"key": ["a", "b", "c"]}
    - 偏差：  {"key": [{"方案1": "a"}, {"方案2": "b"}]}  ← 模型把每条包成对象

    统一落为「元素即文本」的字符串数组，避免把 dict 直接 str() 成 {'方案1': '...'} 的脏输出。
    """
    items = (data or {}).get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        items = [items]
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            vals = [v for v in it.values() if isinstance(v, str) and v.strip()]
            out.append(vals[0] if vals else json.dumps(it, ensure_ascii=False))
        elif it is not None:
            s = str(it)
            if s.strip():
                out.append(s)
    return [x for x in out if x.strip()][:limit]


@router.post("/{project_id}/propose-premise", response_model=PremiseProposalOut)
def propose_premise(project_id: int, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    if not _has_real_llm():
        # 离线降级：基于类型/风格给几个模板化方案，保证闭环
        return _offline_proposals(proj)

    sys_p = (
        "你是网文策划编辑。根据用户给的类型与风格平台调性，构思 3 个「小说大概内容」方案。"
        "每个方案是一段 100–180 字的故事设定，必须包含：主角处境、核心冲突、开篇钩子。"
        "只输出 JSON，且 proposals 必须是【字符串数组】，每个元素就是方案文本本身，"
        "不要包成 {\"方案1\": \"...\"} 这样的对象。格式示例："
        "{\"proposals\": [\"方案文本1\", \"方案文本2\", \"方案文本3\"]}，不要多余解释。"
    )
    user_p = _genre_style_text(proj)
    try:
        txt = get_llm_provider().chat(sys_p, user_p)
    except Exception:  # noqa: BLE001 - 真实模型调用失败（如免费档 429 限流）：降级本地模板
        return _offline_proposals(proj, degraded=True)
    data = _safe_parse(txt, {"proposals": []})
    return {"proposals": _coerce_string_list(data, "proposals", 5)}


@router.post("/{project_id}/expert-questions", response_model=ExpertQuestionOut)
def expert_questions(project_id: int, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    if not _has_real_llm():
        return _offline_questions()

    sys_p = (
        "你是 wayfinder 创作教练。根据用户给出的类型、风格平台调性、以及（可选的）大概内容，"
        "提出 5–7 个关键追问，帮助用户把剧情想透。问题要具体、可答、能逼出人物动机与核心冲突。"
        "只输出 JSON，且 questions 必须是【字符串数组】，每个元素就是问题文本本身，"
        "不要包成对象。格式示例：{\"questions\": [\"问题1\", \"问题2\", ...]}，不要多余解释。"
    )
    user_p = _genre_style_text(proj)
    if proj.premise:
        user_p += f"\n大概内容：{proj.premise}"
    try:
        txt = get_llm_provider().chat(sys_p, user_p)
    except Exception:  # noqa: BLE001 - 同上：限流等异常时降级本地追问清单
        return _offline_questions(degraded=True)
    data = _safe_parse(txt, {"questions": []})
    return {"questions": _coerce_string_list(data, "questions", 8)}


@router.post("/{project_id}/build-bible", response_model=PlotBibleOut)
def build_bible(
    project_id: int, body: ExpertAnswersIn | None = None, db: Session = Depends(get_db)
) -> dict:
    """quick：直接据类型/风格/内容/参考语料构思剧情圣经；expert：附带追问答案。"""
    proj = _require_project(db, project_id)
    answers = (body.answers if body else None) or {}
    preview = body.preview if body else False
    if not preview and db.scalar(select(Generation.id).where(Generation.project_id == project_id).limit(1)):
        raise HTTPException(409, "已有正文，不能覆盖剧情规划")
    provider = get_llm_provider()
    from app.llm.provider import LocalFallbackLLMProvider
    if provider is None or isinstance(provider, LocalFallbackLLMProvider):
        return _planning_failure("writer_unavailable", False)
    sample = ""
    sample_note = ""
    if proj.reference_novel_ids:
        # 参考语料风格样本：只取风格纹理。**必须先清洗再脱敏**——
        # 原文自带世界观与专名，直接进提示词会把参考书的人名地名灌进用户的剧情圣经
        # （实测大纲写「北漠」、正文写「北莽」）。脱敏不可用时**不给原文**（fail closed）。
        try:
            from app.ingestion.clean import clean_consumption_text
            from app.style.entities import Redactor
            from app.style.entity_store import entity_entries, ensure_table
            raw, sample_info = _style_sample(db, [int(n) for n in proj.reference_novel_ids], per=4)
            if raw:
                # 建表器以前没有调用点，专名表永远是空的 → 脱敏是空操作 →
                # 参考原文（带专名）会直接进提示词。这里按需建（只发生一次）。
                table = ensure_table(db, int(proj.reference_novel_ids[0]), provider=provider)
                if entity_entries(table):
                    red = Redactor(table)
                    sample = red.redact(raw)[:2000]
                    sample_note = (f"风格样本：{sample_info['chunks']} 片原文（跳过 {sample_info['skipped']} "
                                   f"片带残留告警），已脱敏（{red.size} 个专名换成等长占位符，"
                                   f"只留句法/节奏/标点）")
                else:
                    sample_note = ("风格样本：专名表不可用（未构建或判定失败）→ "
                                   "不给原文样本，避免把参考书世界观灌进圣经")
        except Exception as exc:  # noqa: BLE001 - 取样本失败不该阻断圣经生成
            sample_note = f"风格样本：读取失败（{type(exc).__name__}）→ 不给原文样本"
    if provider is not None:
        try:
            sys_p = (
                "你是网文主编。综合用户给的类型、风格平台调性、大概内容（与可选的追问答案）、"
                "以及参考语料的风格样本，为这部小说构思一份「剧情圣经」。\n"
                "严格输出 JSON：\n"
                "{\"synopsis\": 完整剧情梗概(300字内), "
                "\"characters\": [{\"name\":角色名,\"role\":身份设定,\"motivation\":核心动机}], "
                "\"relationships\": [{\"a\":角色,\"b\":角色,\"relation\":关系描述}], "
                "\"direction\": 后续走向概述, "
                "\"conflicts\": [冲突1, 冲突2], "
                "\"foreshadowing\": [伏笔1, 伏笔2]}\n"
                "要求：角色名用中文；关系要具体；foreshadowing 是『暂不打烊的悬念』，不要在此兑现。"
            )
            user_p = _genre_style_text(proj)
            if proj.premise:
                user_p += f"\n大概内容：{proj.premise}"
            if answers:
                qa = "\n".join(f"Q：{k}\nA：{v}" for k, v in answers.items())
                user_p += f"\n—— 创作者问答 ——\n{qa}"
            # 剧情结构参考：作者**怎么搭故事**（节拍/场景模板/伏笔/情节组织）。
            # 以前这一层完全不在圣经提示里 → 模型只能给通用网文套路。
            from app.generation.plot_structure import for_project
            structure, structure_note = for_project(
                db, proj, context=f"{user_p}\n{(proj.plot_bible or {}).get('synopsis', '')}")
            if structure:
                user_p += ("\n—— 剧情结构参考（只借鉴结构，不得照搬其中人物、地名与具体情节） ——\n"
                           + json.dumps(structure, ensure_ascii=False, indent=2))
            if sample:
                user_p += f"\n—— 参考语料风格样本（仅取风格纹理；{sample_note}） ——\n{sample}"
            # 照搬校验放进**重试循环内**：命中就把片段回灌给模型让它自己重写，
            # 而不是产出一份照搬的稿子（用户已确认：接进门槛 + 回灌自改）。
            # 比对的规则是**真正进了提示词的那部分**（structure）—— 模型看不到的
            # 规则与它产出相同只可能是巧合，不能算照搬。
            from app.generation.plot_structure import assert_no_copy

            def _parse(raw: str):
                parsed = _parse_bible(raw)
                texts = [parsed.get('synopsis') or "", parsed.get('direction') or ""]
                texts += list(parsed.get('conflicts') or []) + list(parsed.get('foreshadowing') or [])
                return parsed, assert_no_copy(structure, texts)

            parsed, failures = model_json(provider, sys_p, user_p, _parse)
            if parsed is None:
                code = ('possible_copy' if any(f['code'] == 'possible_copy' for f in failures)
                        else 'model_request_failed'
                        if failures and all(f['code'] == 'model_request_failed' for f in failures)
                        else 'invalid_model_structure')
                # 模型参与过吗？请求失败=模型没回（False）；格式/照搬失败=模型回了但不能用（True）。
                participated = any(f['code'] in ('invalid_model_structure', 'possible_copy')
                                   for f in failures)
                return _planning_failure(code, participated,
                                        details={'attempts': len(failures), 'failures': failures})
            bible_dict, copy_report = parsed
            bible = PlotBibleOut.model_validate({**bible_dict, "model_participated": True, "degraded": False,
                                                "status": "bible_draft", "error": None,
                                                "plot_structure_note": structure_note,
                                                "note": " ｜ ".join(x for x in (sample_note, structure_note) if x)
                                                        or bible_dict.get("note")}).model_dump()
            # 机制级校验结果（clean / possible_copy）随结果返回，便于前端显示。
            bible["structure_copy_check"] = copy_report
        except Exception:
            return _planning_failure("invalid_model_structure", True)

    if preview:
        return bible

    # 落库：剧情圣经存进 project.plot_bible
    proj.plot_bible = bible
    proj.status = "bible_draft"
    proj.plot_timeline = None
    db.commit()
    db.refresh(proj)
    return bible


def _parse_bible(raw: str) -> dict:
    """解析圣经 JSON。结构不合格就抛错——由 model_json 的限次重试兜住。"""
    bible = json.loads(_strip_json_block(raw))
    if not isinstance(bible, dict) or not all(
            isinstance(bible.get(k), str) and bible[k].strip() for k in ("synopsis", "direction")):
        raise ValueError("missing story goals")
    if (not isinstance(bible.get("characters"), list) or not bible["characters"]
            or not all(isinstance(c, dict) and all(
                isinstance(c.get(k), str) and c[k].strip() for k in ("name", "role", "motivation"))
                for c in bible["characters"])):
        raise ValueError("missing character motivation")
    return bible


def _planning_failure(code: str, participated: bool, exc: Exception | None = None,
                       details: dict | None = None) -> dict:
    error: dict = {"code": code}
    if exc is not None:
        error["type"] = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            error["http_status"] = status
    if details:
        error.update(details)
        # 请求类失败保留第一次的异常类型（旧契约里有这一项，别丢）。
        if error['code'] == 'model_request_failed':
            first = next((f for f in details.get('failures', []) if f.get('type')), None)
            if first:
                error.setdefault('type', first['type'])
    return {"status": "needs_planning", "model_participated": participated, "degraded": True, "error": error}


@router.post("/{project_id}/generate-outline")
def generate_outline(project_id: int, body: OutlineRequest, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    if not proj.premise or not proj.plot_bible or not proj.plot_bible.get("synopsis") or not proj.plot_bible.get("direction"):
        return _planning_failure("missing_premise_or_bible", False)
    provider = get_llm_provider()
    from app.llm.provider import LocalFallbackLLMProvider
    if provider is None or isinstance(provider, LocalFallbackLLMProvider):
        return _planning_failure("writer_unavailable", False)
    source = source_fingerprint(proj)
    # 剧情结构参考：作者**怎么搭故事**（节拍序列 / 场景模板 / 伏笔 / 情节组织）。
    # 以前规划层一份也看不到 → 节点安排只能来自模型默认的网文套路。
    from app.generation.plot_structure import for_project
    context = f"{proj.premise or ''}\n{(proj.plot_bible or {}).get('synopsis', '')}\n" \
              f"{(proj.plot_bible or {}).get('direction', '')}\n" + " ".join(body.goals or [])
    structure, structure_note = for_project(db, proj, context=context)
    user = json.dumps({"premise": proj.premise, "confirmed_bible": proj.plot_bible,
                       "goals": body.goals, "node_count": body.node_count,
                       "plot_structure": structure}, ensure_ascii=False)
    # 照搬校验放进**重试循环内**：命中就把片段回灌给模型让它自己重写，
    # 而不是产出一份照搬的稿子（用户已确认：接进门槛 + 回灌自改）。
    # 比对的规则是**真正进了提示词的那部分**（structure）。
    from app.generation.plot_structure import assert_no_copy

    def _parse_nodes(raw: str):
        parsed = validate_nodes(json.loads(_strip_json_block(raw))["nodes"], body.node_count)
        return parsed, assert_no_copy(structure, parsed)

    parsed, failures = model_json(provider, SYSTEM, user, _parse_nodes)
    if parsed is None:
        code = ('possible_copy' if any(f['code'] == 'possible_copy' for f in failures)
                else 'model_request_failed'
                if failures and all(f['code'] == 'model_request_failed' for f in failures)
                else 'invalid_model_structure')
        participated = any(f['code'] in ('invalid_model_structure', 'possible_copy')
                           for f in failures)
        return _planning_failure(code, participated,
                                 details={'attempts': len(failures), 'failures': failures})
    nodes, copy_report = parsed
    clarification = build_clarification_report(nodes, source)
    candidate = {"status": clarification["status"] if not clarification["verified"] else "outline_draft",
                 "model_participated": True, "degraded": False, "error": None,
                 "plot_structure_note": structure_note,
                 "structure_copy_check": copy_report,
                 "source_fingerprint": source, "goals": body.goals, "nodes": nodes,
                 "outline": [n["title"] for n in nodes], **clarification}
    if not body.preview:
        return apply_outline(project_id, OutlineApply(candidate=candidate), db)
    return candidate


@router.post("/{project_id}/outline/clarify")
def clarify_outline(project_id: int, body: OutlineClarify, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    try:
        return submit_clarification_candidate(body.candidate, body.answers, source_fingerprint(proj))
    except ValueError as exc:
        if "fingerprint" in str(exc):
            raise HTTPException(409, "规划输入已变化，请重新生成或复核") from exc
        raise HTTPException(422, "仅能回答当前待澄清字段，答案必须符合节点结构") from exc


@router.post("/{project_id}/outline/revise-budget")
def revise_budget(project_id: int, body: BudgetRevision, db: Session = Depends(get_db)) -> dict:
    from copy import deepcopy
    proj = _require_project(db, project_id)
    if body.source_fingerprint != source_fingerprint(proj):
        raise HTTPException(409, "规划输入已变化，请重新复核")
    raw = deepcopy(proj.plot_timeline or {})
    node = raw.get('nodes', {}).get(body.node_id)
    if not node or raw.get('current_node') != body.node_id or body.node_id in raw.get('done', []):
        raise HTTPException(422, "仅能修订当前未完成节点预算")
    old = node.get('segment_budget', 1)
    used = raw.get('seg_counts', {}).get(body.node_id, 0)
    if body.segment_budget <= max(old, used):
        raise HTTPException(422, "新增预算须大于原预算及已用段数")
    node['segment_budget'] = body.segment_budget
    planning = raw.setdefault('planning', {})
    planning.setdefault('budget_revisions', []).append({
        'node_id': body.node_id, 'before': old, 'after': body.segment_budget,
        'reason': body.reason, 'source_fingerprint': body.source_fingerprint,
        'authorized_by': 'explicit_user_confirmation'})
    planning['verified'] = False
    proj.plot_timeline = raw
    db.commit()
    db.refresh(proj)
    return {'saved': True, 'plot_timeline': proj.plot_timeline,
            'source_fingerprint': source_fingerprint(proj), 'verified': False}


@router.post("/{project_id}/outline/apply")
def apply_outline(project_id: int, body: OutlineApply, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    candidate = body.candidate
    if db.scalar(select(Generation.id).where(Generation.project_id == project_id).limit(1)):
        raise HTTPException(409, "已有正文，不能覆盖剧情规划")
    if candidate.get("status") not in {"outline_draft", "clarification_required"} or candidate.get("degraded") is not False:
        raise HTTPException(422, "只能保存完整规划草案")
    try:
        validated = validate_planning_candidate(candidate, source_fingerprint(proj))
    except ValueError as exc:
        if "fingerprint" in str(exc):
            raise HTTPException(409, "规划输入已变化，请重新生成或复核") from exc
        raise HTTPException(422, "因果节点结构不完整") from exc
    if not validated["structurally_complete"]:
        raise HTTPException(409, {"code": "clarification_required", **validated})
    try:
        nodes = validated["nodes"]
    except Exception:
        raise HTTPException(422, "因果节点结构不完整")
    metadata = {k: candidate[k] for k in ("status", "model_participated", "degraded", "error", "source_fingerprint", "goals") if k in candidate}
    metadata.update({k: validated[k] for k in ('status', 'verified', 'structurally_complete', 'unknown_fields', 'candidate_version')})
    proj.plot_timeline = timeline_for(nodes, metadata, max((proj.target_length or 5000)//max(proj.generation_length or 3000, 1), 1))
    proj.outline = [n["title"] for n in nodes]
    proj.status = "outline_draft"
    db.commit()
    db.refresh(proj)
    return {**candidate, **validated, "outline": proj.outline, "saved": True, "project_id": project_id}


def _offline_proposals(proj: Project, degraded: bool = False) -> dict:
    """无 LLM（或真实模型调用失败降级）时的模板化「大概内容」方案，保证向导闭环。"""
    g = proj.genre or "都市"
    s = proj.style or "番茄"
    out = {
        "proposals": [
            f"【{g}·{s}】一个被生活压到谷底的普通人，在某天凌晨接到一通不该响起的电话，"
            "从此被拖进一场他本不想参与的棋局。钩子：对方知道他一个不为人知的秘密。",
            f"【{g}·{s}】主角身负一个被所有人隐瞒的身世，直到某件小事撕开裂缝，"
            "他才发现自己过去二十年是别人写好的剧本。",
            f"【{g}·{s}】一座看似普通的城，每隔七年就会少一个人，没人记得消失的是谁。"
            "主角是唯一还留着记忆的人，他开始逆着遗忘追查。",
        ]
    }
    if degraded:
        out["note"] = "模型调用受限（如免费档限流），已使用本地模板方案；可稍后重试获取模型版。"
    return out


def _offline_questions(degraded: bool = False) -> dict:
    """无 LLM（或真实模型调用失败降级）时的追问清单，保证向导闭环。"""
    out = {
        "questions": [
            "主角的核心欲望是什么？他最害怕失去什么？",
            "故事最大的外部冲突来自哪一方势力/人物？",
            "你希望读者读完第一章后最想知道的悬念是什么？",
            "世界观里有没有一条「反常识」的规则，让故事成立？",
            "主角和关键配角之间，最锋利的情感张力是什么？",
            "你设想的结局偏向「爽」还是「虐」，或开放？",
        ]
    }
    if degraded:
        out["note"] = "模型调用受限（如免费档限流），已使用本地追问清单；可稍后重试获取模型版。"
    return out


def _offline_bible(proj: Project, answers: dict[str, str]) -> dict:
    """无 LLM 时的确定性剧情圣经，保证向导闭环。"""
    premise = (proj.premise or "").strip() or "（未填写，凭类型与风格自由构思）"
    g = proj.genre or "都市"
    s = proj.style or "番茄"
    return {
        "synopsis": f"【{g}·{s}】{premise}",
        "characters": [
            {"name": "主角", "role": f"{g}故事核心人物", "motivation": "在冲突中守住自己想要的东西"},
            {"name": "关键配角", "role": "与主角是锋利张力来源", "motivation": "推动主角直面真相"},
        ],
        "relationships": [{"a": "主角", "b": "关键配角", "relation": "彼此试探、互相掣肘"}],
        "direction": "从开篇钩子出发，逐步揭开更大的局，主角被迫成长。",
        "conflicts": [f"{g}世界的核心矛盾", "主角内心与处境的撕扯"],
        "foreshadowing": [
            "开篇那通不该响起的信号，会在中段以另一种形式回响",
            "配角一句看似随意的话，实则是结局的伏笔",
        ],
    }


@router.post("/{project_id}/build-kg-from-generations")
def build_kg_from_generations(project_id: int, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no)
    ).all()
    text = "\n\n".join((g.text or "") for g in gens).strip()
    if not text:
        raise HTTPException(status_code=400, detail="尚无生成文本，无法建图")
    stats = build_graph_from_text(db, project_id, text, source="generated")
    return stats
