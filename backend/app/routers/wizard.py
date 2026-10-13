"""M5 创作向导路由：选语料 → 类型/风格 → 内容 → quick/expert 构思 → 生成后建图。

所有「大模型」步骤都做了离线降级：未配 LLM key（走本地兜底）或解析失败时，
返回由输入推导的结构化默认结果，保证向导闭环可跑、前端不卡死。
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
from app.schemas import (
    ProtagonistsIn,
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


@router.post("/{project_id}/characters")
def save_characters(
    project_id: int, body: ProtagonistsIn, db: Session = Depends(get_db)
) -> dict:
    """保存用户锁定角色与关系，保留剧情圣经其它字段。"""
    proj = _require_project(db, project_id)
    bible = dict(proj.plot_bible or {})
    bible["characters"] = [c.model_dump() for c in body.characters]
    if body.relationships is not None:
        bible["relationships"] = [r.model_dump() for r in body.relationships]
    proj.plot_bible = bible
    db.commit()
    db.refresh(proj)
    return {"characters": bible["characters"], "relationships": bible.get("relationships") or []}


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


def _style_sample(db: Session, novel_ids: list[int] | None, per: int = 4) -> str:
    """抽取选定参考语料的开篇若干分片，作为风格样本（只取风格纹理，不混整库）。"""
    if not novel_ids:
        return ""
    out: list[str] = []
    for nid in novel_ids[:5]:
        chunks = db.scalars(
            select(NovelChunk)
            .where(NovelChunk.novel_id == nid)
            .order_by(NovelChunk.id)
            .limit(per)
        ).all()
        for c in chunks:
            out.append(c.text.strip())
    return "\n---\n".join(out)


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


def _pacing_block(proj: Project) -> str:
    target = proj.target_total_chars
    length = max(1, int(proj.generation_length or 3000))
    if not target:
        return "整本目标字数：未指定；不强求分段规划。"
    segments = max(1, (target + length - 1) // length)
    daily = int(segments * 0.15)
    conflict = int(segments * 0.2)
    if target < length:
        warning = "警告：目标不足 1 段长，建议目标至少达到单段长度；仍按试水段生成。"
    else:
        warning = ""
    return (
        f"整本目标字数：{target} 字；按每段 {length} 字约 {segments} 段。\n"
        f"节拍分配：约 {daily} 段写日常/补世界观/换幕，约 {conflict} 段做冲突升级/转折，其余段推进主线。\n"
        "在 JSON 的 pacing 数组内，按段给出 segment、conflict、character_state、daily；"
        "节拍内容合计不超过 800 字。\n"
        f"{warning}"
    )


@router.post("/{project_id}/propose-premise", response_model=PremiseProposalOut)
def propose_premise(project_id: int, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    if not _has_real_llm():
        # 离线降级：基于类型/风格给几个模板化方案，保证闭环
        return _offline_proposals(proj)

    sys_p = (
        "你是网文策划编辑。根据用户给的类型与风格平台调性，构思 3 个「小说大概内容」方案。"
        "每个方案是一段 100–180 字的故事设定，必须包含：主角处境、核心冲突、开篇钩子。"
        "只输出 JSON，格式：{\"proposals\": [\"方案1\", \"方案2\", \"方案3\"]}，不要多余解释。"
    )
    user_p = _genre_style_text(proj)
    try:
        txt = get_llm_provider().chat(sys_p, user_p)
    except Exception:  # noqa: BLE001 - 真实模型调用失败（如免费档 429 限流）：降级本地模板
        return _offline_proposals(proj, degraded=True)
    data = _safe_parse(txt, {"proposals": []})
    proposals = data.get("proposals") or []
    if not isinstance(proposals, list):
        proposals = [str(proposals)]
    return {"proposals": [str(p) for p in proposals if str(p).strip()][:5]}


@router.post("/{project_id}/expert-questions", response_model=ExpertQuestionOut)
def expert_questions(project_id: int, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, project_id)
    if not _has_real_llm():
        return _offline_questions()

    sys_p = (
        "你是 wayfinder 创作教练。根据用户给出的类型、风格平台调性、以及（可选的）大概内容，"
        "提出 5–7 个关键追问，帮助用户把剧情想透。问题要具体、可答、能逼出人物动机与核心冲突。"
        "只输出 JSON：{\"questions\": [\"问题1\", ...]}，不要多余解释。"
    )
    user_p = _genre_style_text(proj)
    if proj.premise:
        user_p += f"\n大概内容：{proj.premise}"
    try:
        txt = get_llm_provider().chat(sys_p, user_p)
    except Exception:  # noqa: BLE001 - 同上：限流等异常时降级本地追问清单
        return _offline_questions(degraded=True)
    data = _safe_parse(txt, {"questions": []})
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        questions = [str(questions)]
    return {"questions": [str(q) for q in questions if str(q).strip()][:8]}


@router.post("/{project_id}/build-bible", response_model=PlotBibleOut)
def build_bible(
    project_id: int, body: ExpertAnswersIn | None = None, db: Session = Depends(get_db)
) -> dict:
    """quick：直接据类型/风格/内容/参考语料构思剧情圣经；expert：附带追问答案。"""
    proj = _require_project(db, project_id)
    answers = (body.answers if body else None) or {}
    sample = _style_sample(db, proj.reference_novel_ids)

    if not _has_real_llm():
        bible = _offline_bible(proj, answers)
    else:
        try:
            sys_p = (
                "你是网文主编。综合用户给的类型、风格平台调性、大概内容（与可选的追问答案）、"
                "以及参考语料的风格样本，为这部小说构思一份「剧情圣经」。\n"
                "严格输出 JSON：\n"
                "{\"synopsis\": 完整剧情梗概, "
                "\"characters\": [{\"name\":角色名,\"role\":身份设定,\"motivation\":核心动机,\"arc_state\":当前人物弧线状态}], "
                "\"relationships\": [{\"a\":角色,\"b\":角色,\"relation\":关系描述}], "
                "\"direction\": 后续走向概述, "
                "\"conflicts\": [冲突1, 冲突2], "
                "\"foreshadowing\": [伏笔1, 伏笔2], "
                "\"pacing\": [{\"segment\":段号,\"conflict\":核心冲突,\"character_state\":角色状态,\"daily\":是否含日常}]}\n"
                "要求：角色名用中文；关系要具体；foreshadowing 是『暂不打烊的悬念』，不要在此兑现。\n"
                + _pacing_block(proj)
            )
            user_p = _genre_style_text(proj)
            if proj.premise:
                user_p += f"\n大概内容：{proj.premise}"
            locked = (proj.plot_bible or {}).get("characters") or []
            if locked:
                user_p += "\n—— 用户锁定角色（必须原名使用，不得改名） ——\n" + json.dumps(
                    locked, ensure_ascii=False
                )
            if answers:
                qa = "\n".join(f"Q：{k}\nA：{v}" for k, v in answers.items())
                user_p += f"\n—— 创作者问答 ——\n{qa}"
            if sample:
                user_p += f"\n—— 参考语料风格样本（仅取风格纹理） ——\n{sample[:2000]}"
            txt = get_llm_provider().chat(sys_p, user_p)
            bible = _safe_parse(txt, _offline_bible(proj, answers))
        except Exception:  # noqa: BLE001 - 限流等异常：降级本地圣经，保证向导闭环
            bible = _offline_bible(proj, answers)
            bible["note"] = (
                "模型调用受限（如免费档限流），已使用本地模板剧情圣经；可稍后重试获取模型版。"
            )

    # 用户锁定的角色是高于模型构思的输入，离线与降级路径也必须保留。
    locked = (proj.plot_bible or {}).get("characters") or []
    if locked:
        bible["characters"] = locked
        bible["relationships"] = (proj.plot_bible or {}).get("relationships") or []

    # 落库：剧情圣经存进 project.plot_bible
    proj.plot_bible = bible
    proj.status = "planned"
    db.commit()
    db.refresh(proj)
    return bible


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
    pacing: list[dict] = []
    if proj.target_total_chars:
        n = max(1, (proj.target_total_chars + (proj.generation_length or 3000) - 1) // (proj.generation_length or 3000))
        pacing = [
            {"segment": i, "conflict": "日常与世界观铺垫" if i % 7 == 0 else "主线推进", "character_state": "继续承受当前选择", "daily": i % 7 == 0}
            for i in range(1, min(n, 120) + 1)
        ]
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
        "pacing": pacing,
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
