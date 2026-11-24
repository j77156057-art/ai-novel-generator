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
from app.kg.build import rebuild_generated_kg
from app.llm.provider import get_llm_provider
from app.models import Generation, NovelChunk, Project
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
    """当前配置能否真正调用模型。

    判据必须与 `get_llm_provider()` 一致：历史版本只认 openrouter / dashscope /
    zhipu / siliconflow / minimax 五个渠道，于是把 command-code 等新渠道一律
    当成「没有模型」——剧情圣经静默降级成本地模板（角色变成「主角/关键配角」）。
    """
    try:
        from app.llm.provider import LocalFallbackLLMProvider, get_llm_provider

        return not isinstance(get_llm_provider(), LocalFallbackLLMProvider)
    except Exception:  # noqa: BLE001 —— 配不起来就按离线处理，不抛栈
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


def _bible_user_prompt(
    proj: Project,
    answers: dict,
    sample: str,
    digest: str,
) -> str:
    """剧情圣经的 user prompt（纯函数，便于测试注入顺序）。"""
    user_p = _genre_style_text(proj)
    if proj.premise:
        user_p += f"\n大概内容：{proj.premise}"
    if answers:
        qa = "\n".join(f"Q：{k}\nA：{v}" for k, v in answers.items())
        user_p += f"\n—— 创作者问答 ——\n{qa}"
    if digest:
        user_p += (
            "\n—— 蒸馏文风档案（参考语料的叙事节奏与对白手法，"
            "只借写法，不要照搬情节、人名、专名） ——\n"
            f"{digest}"
        )
    if sample:
        # 有蒸馏档案时原始样本只作纹理补充，避免提示词膨胀。
        user_p += (
            "\n—— 参考语料风格样本（仅取风格纹理） ——\n"
            f"{sample[:1200 if digest else 2000]}"
        )
    return user_p


@router.post("/{project_id}/build-bible", response_model=PlotBibleOut)
def build_bible(
    project_id: int, body: ExpertAnswersIn | None = None, db: Session = Depends(get_db)
) -> dict:
    """quick：直接据类型/风格/内容/参考语料构思剧情圣经；expert：附带追问答案。"""
    proj = _require_project(db, project_id)
    answers = (body.answers if body else None) or {}
    sample = _style_sample(db, proj.reference_novel_ids)
    # 蒸馏档案优先：它把参考语料的叙事节奏/对白规则压成了可执行条目。
    try:
        from app.style.narrative import narrative_digest

        digest = narrative_digest(
            getattr(proj, "style_narrative", None),
            max_chars=2400,
            allowed_novel_ids=proj.reference_novel_ids,
        )
    except Exception:  # noqa: BLE001 —— 档案不可用不阻断构思
        digest = ""

    if not _has_real_llm():
        bible = _offline_bible(proj, answers)
    else:
        try:
            sys_p = (
                "你是网文主编。综合用户给的类型、风格平台调性、大概内容（与可选的追问答案）、"
                "以及参考语料的风格样本，为这部小说构思一份「剧情圣经」。\n"
                "若提供了蒸馏文风档案，优先遵循其中的叙事节奏与对白规则。\n"
                "严格输出 JSON：\n"
                "{\"synopsis\": 完整剧情梗概(300字内), "
                "\"characters\": [{\"name\":角色名,\"role\":身份设定,\"motivation\":核心动机}], "
                "\"relationships\": [{\"a\":角色,\"b\":角色,\"relation\":关系描述}], "
                "\"direction\": 后续走向概述, "
                "\"conflicts\": [冲突1, 冲突2], "
                "\"foreshadowing\": [{\"name\": 伏笔短名, \"tier\": \"main|arc|chapter\", "
                "\"truth\": 真相（仅 main 必填，其余留空）, \"hints\": [暗示点1, 暗示点2], "
                "\"reveal_ratio\": 揭晓起点占全书比例（仅 main，0–1，建议 0.85）, "
                "\"reveal_span_ratio\": 揭晓跨度占全书比例（仅 main，0–1，建议 0.15）}]}\n"
                "要求：角色名用中文；关系要具体。伏笔必须分级，不要把所有悬念都写成伏笔：\n"
                "1. **main 恰好 1 条**：贯穿全书的终极伏笔，开头或前几章埋下、临近结局才揭晓。"
                "truth 写真正的答案（例如轮回/系统的真面目、前世或未来自己的布局、反派或天道的收割计划），"
                "并至少设计一层**反转或局中局**（前面看起来的坏人/真相，最后一层被推翻）；"
                "hints 写 3–5 个能在正文里反复出现的具体细节（物、动作、口头禅、时间点），"
                "只暗示、不揭示答案；main 的揭晓窗口用**百分比**：reveal_ratio 填 0.85、"
                "reveal_span_ratio 填 0.15（最后 15% 的章节逐层揭晓，三层均匀分布，"
                "不要连续三章各翻一层）；不要写死具体章号。\n"
                "2. **arc 1–2 条**：卷/篇级伏笔，几十章内回收。\n"
                "3. **chapter 2–3 条**：小节级伏笔，几章内埋下并回收；"
                "必须有具体载体（物件/身份/预言/异常），**不要把普通对话或反问写成伏笔**。"
            )
            user_p = _bible_user_prompt(proj, answers, sample, digest)
            txt = get_llm_provider().chat(sys_p, user_p)
            bible = _safe_parse(txt, _offline_bible(proj, answers))
        except Exception:  # noqa: BLE001 - 限流等异常：降级本地圣经，保证向导闭环
            bible = _offline_bible(proj, answers)
            bible["note"] = (
                "模型调用受限（如免费档限流），已使用本地模板剧情圣经；可稍后重试获取模型版。"
            )

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
            {
                "name": "凌晨三点十七分固定响起的信号",
                "tier": "main",
                "truth": "那通信号是主角未来自己发回的回溯提示，用来阻止一次会让所有人失败的抉择",
                "hints": [
                    "凌晨三点十七分，座机总会响一声就停",
                    "听筒里有一次极轻的呼吸，节奏和主角自己一模一样",
                    "每次事件后，主角口袋里会多出一张写着自己笔迹的便签",
                ],
                "reveal_chapter": None,
            },
            {
                "name": "配角那句看似随口的话",
                "tier": "arc",
                "truth": "",
                "hints": [],
                "reveal_chapter": None,
            },
        ],
    }


@router.post("/{project_id}/build-kg-from-generations")
def build_kg_from_generations(project_id: int, db: Session = Depends(get_db)) -> dict:
    """按已生成的各段**全量重建** generated 侧 KG（图谱 + 账本 + 别名 + 哨兵）。

    2026-10-17：此前走 `build_graph_from_text`，只做图谱一步（账本/别名/哨兵全不
    更新 → 与图谱漂移），且 `_split_text_to_chunks` 落的 chapter 是**chunk 序号
    而非段号**（段号语义丢失）。现改走 `rebuild_generated_kg`：清空 generated 侧
    全部派生数据后按**真实段号**逐段 replay，Fact 与图谱同源、段号正确。
    """
    proj = _require_project(db, project_id)
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no)
    ).all()
    text = "\n\n".join((g.text or "") for g in gens).strip()
    if not text:
        raise HTTPException(status_code=400, detail="尚无生成文本，无法建图")
    stats = rebuild_generated_kg(db, project_id)
    return stats
