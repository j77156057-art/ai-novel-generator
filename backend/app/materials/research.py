# -*- coding: utf-8 -*-
"""素材调研：按章纲推断主题、联网检索、解析年份、10 条一批交模型整理。

设计边界：
- 网络与模型调用只在后台 worker 线程发生；本模块不持有任何请求级状态。
- 检索源自动选择：模型设置页已配置 → `web_search.service`；否则回退
  `retrieval.brief.backend.FreeSearchBackend`（免费、无需 key）。
- 任何一步失败都降级：主题推断失败用规则查询，整理失败用启发式标注，
  已入库素材不丢。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.materials.settings import MaterialSettings, load_settings
from app.models import Project, ProjectMaterial

logger = logging.getLogger("materials.research")

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
#: 完整日期（含月日）才算强证据，用于区分「发布日期」与「正文里的年份」。
_FULL_DATE_RE = re.compile(
    r"((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"
)
#: URL 里的日期路径：/2023/05/12、/20230512/、t20230512_、?date=2023-05。
_URL_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"/((?:19|20)\d{2})/(\d{1,2})/(\d{1,2})(?:[/?#]|$)"),
    re.compile(r"/((?:19|20)\d{2})[-_](\d{1,2})[-_](\d{1,2})(?:[/?#]|$)"),
    re.compile(r"(?:^|[/_\-])t?((?:19|20)\d{2})(\d{2})(\d{2})(?:[/_.\-]|$)"),
    re.compile(
        r"(?:date|pubdate|publish|publish_time|pub_time|time)=((?:19|20)\d{2})"
        r"(?:[-/.]?(\d{1,2}))?(?:[-/.]?(\d{1,2}))?"
    ),
)
#: 发布日期措辞：命中后紧邻的年份才算发布年。
_PUBLISH_MARKERS = ("发布", "发表", "更新", "刊发", "日期", "时间")
#: 这些词附近的年份多半是「机构成立时间 / 历史起源」，不是素材年份。
_CONTENT_YEAR_CONTEXT = (
    "成立", "创立", "创办", "始建", "诞生", "起源", "创建", "组建",
    "设立", "开业", "上市", "建于", "生于", "卒于", "成立于", "创办于",
)


def parse_year(text: str | None) -> int | None:
    """从一段文本里取一个像年份的四位数；找不到返回 None。"""
    match = _YEAR_RE.search(str(text or ""))
    if not match:
        return None
    year = int(match.group(1))
    if 1950 <= year <= 2100:
        return year
    return None


def _year_from_url(url: str) -> int | None:
    text = str(url or "")
    for pattern in _URL_DATE_RES:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group(1))
        if 1950 <= year <= 2100:
            return year
    return None


def _has_content_context(text: str, start: int, end: int) -> bool:
    """年份附近是否出现「成立/创立/始建于」等历史语境词。"""
    window = text[max(0, start - 12) : min(len(text), end + 12)]
    return any(word in window for word in _CONTENT_YEAR_CONTEXT)


def _year_with_marker(text: str) -> int | None:
    """只接受紧邻「发布/更新/日期」等措辞的年份。"""
    for match in _YEAR_RE.finditer(text):
        year = int(match.group(1))
        if not (1950 <= year <= 2100):
            continue
        before = text[max(0, match.start() - 14) : match.start()]
        after = text[match.end() : match.end() + 6]
        if any(word in before or word in after for word in _PUBLISH_MARKERS):
            return year
    return None


def resolve_year(
    title: str,
    snippet: str,
    url: str = "",
    published_at: str = "",
) -> tuple[int | None, str, str]:
    """返回 (年份, 原文表述, 证据强度)。

    证据优先级：渠道发布时间 > URL 日期 > 完整日期 > 发布措辞 > 无历史语境的裸年份。
    「2007 年成立」这类正文历史年份会被最后一步跳过，避免把机构成立年当成素材年份。
    证据强度：source / url / date / publish / content / none。
    """
    # 1) 检索渠道直接给的发布时间（Tavily/Brave/Serper 等）
    for source in (published_at,):
        if not source:
            continue
        year = parse_year(source)
        if year is not None:
            return year, str(source)[:64], "source"

    # 2) URL 里的日期路径
    year = _year_from_url(url)
    if year is not None:
        return year, str(year), "url"

    # 3) 标题/摘要里的完整日期（年月日才算发布证据）
    for text in (snippet, title):
        if not text:
            continue
        match = _FULL_DATE_RE.search(str(text))
        if match:
            return int(match.group(1)), match.group(0), "date"

    # 4) 明确带「发布/更新/日期」措辞的年份
    for text in (snippet, title):
        if not text:
            continue
        year = _year_with_marker(str(text))
        if year is not None:
            return year, str(year), "publish"

    # 5) 裸年份：跳过「成立/创立/始建于」等历史语境
    for text in (snippet, title):
        if not text:
            continue
        raw = str(text)
        for match in _YEAR_RE.finditer(raw):
            year = int(match.group(1))
            if not (1950 <= year <= 2100):
                continue
            if _has_content_context(raw, match.start(), match.end()):
                continue
            return year, str(year), "content"

    return None, "", "none"


def find_year(title: str, snippet: str, url_str: str = "") -> tuple[int | None, str]:
    """兼容旧调用：只返回 (年份, 原文表述)。"""
    year, text, _evidence = resolve_year(title, snippet, url_str)
    return year, text


# --------------------------------------------------------------------------- #
# 页面级年份补全（免费搜索源的摘要常常不带日期）
# --------------------------------------------------------------------------- #

_META_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|pubdate|'
        r'publishdate|publish_date|datePublished|date)["\'][^>]+content=["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']'
        r'(?:article:published_time|pubdate|publishdate|publish_date|datePublished|date)["\']',
        re.I,
    ),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'"dateModified"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\']', re.I),
)
#: 页面正文里的第一个完整日期（兜底；前面没有 meta 时才会用到）。
_ANY_FULL_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")


def parse_published_year_html(html: str) -> tuple[int | None, str]:
    """从页面 HTML 里取发布年份：meta / JSON-LD / time 优先，完整日期兜底。

    兜底只接受「页面头部 + 附近有发布/更新/日期/时间字样」的完整日期；
    正文里的历史年份（如「2001 年烟台确立新定位」）不再当发布年份。
    """
    text = str(html or "")
    for pattern in _META_DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year = parse_year(match.group(1))
        if year is not None:
            return year, str(match.group(1))[:64]

    head = text[:20000]
    for match in _ANY_FULL_DATE_RE.finditer(head):
        window = head[max(0, match.start() - 40) : match.end() + 40]
        if not any(
            marker in window
            for marker in ("发布", "发表", "更新", "日期", "时间", "发表于")
        ):
            continue
        year = int(match.group(1))
        if 1950 <= year <= 2100:
            return year, match.group(0)[:64]
    return None, ""


def fetch_published_year(url: str, *, timeout_s: float = 5.0) -> tuple[int | None, str]:
    """抓一次页面补发布年份；失败/非 http/跳转链接一律返回 (None, "")。"""
    target = str(url or "").strip()
    if not target.startswith(("http://", "https://")):
        return None, ""
    # 百度/DDG 的跳转链接需要过一次反爬，成功率低且容易触发验证码，直接跳过。
    if "baidu.com/link" in target or "duckduckgo.com/l/" in target:
        return None, ""
    try:
        import httpx

        from app.retrieval.brief.backend import DEFAULT_USER_AGENT

        response = httpx.get(
            target,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            timeout=timeout_s,
            follow_redirects=True,
        )
        if response.status_code >= 400:
            return None, ""
        return parse_published_year_html(response.text)
    except Exception:  # noqa: BLE001 —— 补年份失败不影响素材本身
        return None, ""


# --------------------------------------------------------------------------- #
# 章前提要（供模型推断主题）
# --------------------------------------------------------------------------- #

def _outline_text(project: Project, chapter: int) -> str:
    try:
        from app.generation.prompts import chapter_outline_entry

        return str(chapter_outline_entry(project.outline, chapter) or "").strip()
    except Exception:  # noqa: BLE001 —— 章纲缺失不影响调研
        return ""


def _timeline_node(project: Project, chapter: int) -> str:
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not saved:
        return ""
    try:
        from app.generation.engine import _outline_entries, _total_segments
        from app.generation.timeline import node_for_chapter, state_from_dict

        state = state_from_dict(
            saved,
            total_segments=_total_segments(project),
            total_chapters=len(_outline_entries(project)) or None,
        )
        if state is None:
            return ""
        node = state.nodes.get(node_for_chapter(state, chapter) or "")
        if node is None:
            return ""
        parts = [node.title, node.entry_condition, node.exit_condition, *node.beats]
        return "；".join(x for x in parts if str(x or "").strip())
    except Exception:  # noqa: BLE001
        return ""


def _flatten(value: Any, limit: int = 600) -> str:
    """把剧情圣经里的 str/list/dict 压成一段可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(
                    "；".join(
                        f"{key}：{_flatten(val, 120)}"
                        for key, val in item.items()
                        if val
                    )
                )
            else:
                parts.append(_flatten(item, 120))
        text = "；".join(p for p in parts if p)
    elif isinstance(value, dict):
        text = "；".join(
            f"{key}：{_flatten(val, 120)}" for key, val in value.items() if val
        )
    else:
        text = str(value)
    return " ".join(text.split())[:limit]


def _character_lines(project: Project) -> list[str]:
    """人物只取「名字 + 身份/角色 + 动机」，不要把整张人物卡塞进提示词。"""
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    chars = bible.get("characters")
    out: list[str] = []
    if isinstance(chars, list):
        for item in chars:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            role = _flatten(item.get("role"), 60)
            motivation = _flatten(item.get("motivation"), 60)
            line = name
            if role:
                line += f"（{role}）"
            if motivation:
                line += f"；动机：{motivation}"
            out.append(line)
    return out[:6]


def chapter_brief(project: Project, chapter: int) -> dict[str, Any]:
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    return {
        "chapter": chapter,
        "genre": str(project.genre or "").strip(),
        "premise": str(project.premise or "").strip()[:600],
        "title": str(project.title or "").strip(),
        "synopsis": _flatten(bible.get("synopsis"), 700),
        "conflicts": _flatten(bible.get("conflicts"), 500),
        "background": _flatten(bible.get("background"), 500),
        "direction": _flatten(bible.get("direction"), 400),
        "outline": _outline_text(project, chapter),
        "node": _timeline_node(project, chapter),
        "characters": _character_lines(project),
    }


# --------------------------------------------------------------------------- #
# 主题推断
# --------------------------------------------------------------------------- #

_TOPIC_SYSTEM = (
    "你是中文网文的资料研究员。只输出 JSON 对象，不要 Markdown，不要解释。"
)

_TOPIC_USER = """正在写的小说：{title}
题材标签：{genre}
剧情圣经·故事梗概：{synopsis}
剧情圣经·核心冲突：{conflicts}
世界观/背景：{background}
后续方向：{direction}
人物与动机：{characters}

本章章纲：{outline}
时间线节点：{node}

先判断（不要被表层设定带偏）：
- theme：这本书真正的题材内核（一句话，如「都市修仙 / 民间怪谈 / 行业商战」）；
- surface：故事载体/表层设定（如「租房、交房、水电过户」），它们只是舞台，不是检索主题；
- needs：本章最缺的 4–8 个素材方向。

再给 queries，按用途分组（每组 2–6 条）：
- theme：题材内核的现实原型（宗教/民俗/地质/工艺/政策等）；
- event：本章核心事件对应的现实知识；
- character：人物职业/能力对应的现实知识；
- texture：场景氛围与生活质感；
- surface：故事载体本身（租房、交房、水电等）——**最多 2 条**，没有就留空；
- reference：同题材网文参考（每条必须带「小说/网文/推荐/书单/代表作」等词），
  找同题材代表作、常见世界观、读者爽点/雷点；只作写法参考，不要照抄。

共同要求：
- 核心原则：theme/event/character/texture 查的是**现实世界**的知识原型，不是同名小说/设定集。把超凡元素翻译成现实对应物再检索：
  灵石→矿石/玉石交易与鉴定；灵脉→风水龙脉/地质断裂带；丹炉→古代炼丹器具/中药炮制；
  禁制→民间符咒/建筑风水禁忌；修真隐匿→道教隐士/民间修行。
- 若题材含「系统 / 无敌 / 穿越 / 诡异 / 无限流」等网文流派，needs 要覆盖该流派的现实原型：
  系统→游戏化任务/奖励/面板机制；无敌→战力体系与爽点节奏；穿越→目标时代的制度与生活；
  诡异→民俗禁忌/心理恐惧/都市传说。
- 优先结合剧情圣经里的超凡设定、核心冲突与人物动机，而不是只查载体流程；
- 每条 6–20 字，关键词式短句，不要问句/长句；
- theme/event/character/texture/surface 中禁止出现「小说 / 设定 / 世界观 / 桥段 / 同人 / 章节 / 全文阅读」等词；
- 不得出现本书特有的人名、地名、楼号、组织名（如「槐树巷17号」要换成「老城区旧楼」）；
- 不要写小说剧情、不要写人物名组合。

严格返回 JSON：
{{"theme":"...","surface":["..."],"needs":["..."],"topics":["..."],
 "queries":{{"theme":["..."],"event":["..."],"character":["..."],"texture":["..."],"surface":["..."],"reference":["..."]}}}}
"""

#: 故事载体类查询的总配额（不是每个词 2 条，而是总共最多 2 条）。
MAX_SURFACE_QUERIES = 2
#: 小说专有地名/楼号：这类词拿去搜索只会命中同人讨论或广告。
_LOCATION_NOUN_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}(?:巷|街|路|号|大厦|小区|公寓)")
#: 查询词里出现这些词，说明在搜「另一本小说」而不是现实知识，直接丢弃。
_QUERY_BANNED = (
    "小说", "设定", "世界观", "桥段", "同人", "章节", "全文阅读",
    "全文", "书评", "剧情", "故事",
)
#: 结果页里的网文站/同人站域名（命中即不入库）。
_FICTION_DOMAINS = (
    "fanqienovel.com", "qidian.com", "chuangshi.qq.com", "jjwxc.net",
    "hongxiu.com", "17k.com", "zongheng.com", "xxsy.net", "sfacg.com",
    "ciweimao.com", "biquge", "69shu", "dingdian", "shuqi.com",
    "zhangyue.com", "ireader.com", "yunqi.qq.com", "readnovel.com",
)
#: 结果标题/摘要里的网文标记。
_FICTION_WORDS = (
    "小说", "最新章节", "全文阅读", "免费阅读", "章节列表", "书评",
    "同人", "设定集", "笔趣阁", "无弹窗", "txt下载", "在线阅读",
    "起点中文网", "番茄小说", "创世中文网", "晋江文学城", "掌阅",
    "书旗", "qq阅读", "nga", "贴吧", "网文", "书荒", "神作",
)


def is_fiction_result(title: str, snippet: str = "", url: str = "") -> bool:
    """判断检索结果是不是另一本小说/同人页（保留作参考，不再丢弃）。"""
    haystack = f"{title or ''} {snippet or ''} {url or ''}".lower()
    if any(domain in haystack for domain in _FICTION_DOMAINS):
        return True
    return any(word in haystack for word in _FICTION_WORDS)


#: 同类小说的统一目录分类：保留为参考，但和现实知识分开，避免混在一起。
FICTION_CATEGORY = "同类小说参考"


def _blocked_terms(project: Project) -> list[str]:
    """本书专有人名：查询里出现就丢弃，避免搜到同人内容。"""
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    names: list[str] = []
    for item in bible.get("characters") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if len(name) >= 2:
                names.append(name)
    return names[:10]


def _normalize_query(text: str) -> str:
    return " ".join(str(text or "").split()).strip("。！？?!；;，,、")


_QUERY_GROUPS = ("theme", "event", "character", "texture", "surface")
_REFERENCE_GROUP = "reference"
#: reference 查询必须命中这些词，确保真的在搜同题材网文而不是普通书名。
_REFERENCE_HINTS = (
    "小说", "网文", "推荐", "书单", "同人", "代表作", "作品", "排行", "榜单",
)


def _split_query_values(values: list[str]) -> list[str]:
    """模型常把两个主题用「、」拼进一条查询；拆开分别检索效果更好。"""
    out: list[str] = []
    for value in values:
        for piece in re.split(r"[、,，;；]+", str(value or "")):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def _queries_from_payload(raw: Any) -> tuple[list[str], list[str]]:
    """兼容两种模型输出：按用途分组的 dict，或旧的平铺 list。

    返回 (知识查询, 同题材网文 reference 查询)。分组模式下 surface 最多 2 条，
    其余知识组各最多 6 条，reference 最多 6 条。
    """
    if isinstance(raw, dict):
        out: list[str] = []
        for key in _QUERY_GROUPS:
            items = raw.get(key) or []
            if isinstance(items, str):
                items = [items]
            values = _split_query_values(
                [str(x).strip() for x in items if str(x).strip()]
            )
            out.extend(values[: MAX_SURFACE_QUERIES if key == "surface" else 6])
        ref_items = raw.get(_REFERENCE_GROUP) or []
        if isinstance(ref_items, str):
            ref_items = [ref_items]
        refs = _split_query_values(
            [str(x).strip() for x in ref_items if str(x).strip()]
        )[:6]
        return out, refs
    if isinstance(raw, list):
        return _split_query_values([str(x).strip() for x in raw if str(x).strip()]), []
    return [], []


def _clean_reference_queries(
    queries: list[str],
    *,
    blocked_terms: list[str] | None = None,
    exclude: list[str] | None = None,
    limit: int = 6,
) -> list[str]:
    """reference 查询允许出现小说/网文词，但仍要挡本书人名、地名与重复项。"""
    blocked = [t for t in (blocked_terms or []) if t]
    skip = set(exclude or [])
    out: list[str] = []
    for raw in queries:
        query = _normalize_query(raw)
        if not query or query in skip:
            continue
        if any(term in query for term in blocked):
            continue
        if _LOCATION_NOUN_RE.search(query):
            continue
        # 只要「同题材网文」方向的查询，不要具体书名（《...》会搜到单本书）。
        if "《" in query or "》" in query:
            continue
        if not any(hint in query for hint in _REFERENCE_HINTS):
            continue
        if query not in out:
            out.append(query)
    return out[:limit]


#: 常见网文流派词：优先从标题/简介/类型里提取，作为 reference 检索词。
_GENRE_HINTS = (
    "规则怪谈", "无限流", "克苏鲁", "修仙", "仙侠", "修真", "玄幻", "都市",
    "诡异", "悬疑", "灵异", "恐怖", "穿越", "重生", "系统", "无敌", "末日",
    "历史", "武侠", "种田", "克系",
)

#: 流派对应的现实知识原型查询。模型偶尔只给生活/校园类查询，这里兜底保证
#: 「系统/无敌/穿越/诡异」等流派的机制也有素材可查。
_SUBGENRE_QUERIES: dict[str, tuple[str, ...]] = {
    "修仙": ("道教 内丹 修炼 体系", "古代 炼丹 术 与 器具", "民间 修行 传说"),
    "仙侠": ("道教 内丹 修炼 体系", "古代 剑术 与 兵器", "名山 宫观 文化"),
    "修真": ("道教 内丹 修炼 体系", "古代 炼丹 术 与 器具", "民间 修行 传说"),
    "玄幻": ("神话 史诗 权力 结构", "古代 战争 战术", "血脉 遗传 科学"),
    "都市": ("城市 行业 职业 生态", "现代 城市 社区 治理", "都市 生活 细节"),
    "规则怪谈": ("规则 设计 心理学 实验", "密闭空间 恐惧 心理学", "都市传说 传播 机制"),
    "诡异": ("民俗 禁忌 都市传说", "民间 驱邪 仪式", "心理 恐惧 触发 机制"),
    "悬疑": ("刑侦 调查 流程", "犯罪 心理 画像", "法医 鉴定 常识"),
    "灵异": ("民间 灵异 传说", "民俗 禁忌", "心理 暗示 机制"),
    "恐怖": ("民俗 禁忌 都市传说", "心理 恐惧 触发 机制", "恐怖 电影 叙事"),
    "穿越": ("古代 县级 行政 制度", "古代 赋税 户籍 制度", "古代 宗族 地方治理"),
    "重生": ("记忆 决策 偏差 研究", "人生 重来 心理 研究", "时间循环 叙事 结构"),
    "系统": ("游戏任务 设计 奖励 机制", "即时反馈 行为 成瘾 机制", "游戏 UI 面板 交互 设计"),
    "无敌": ("战力 分级 体系 设计", "英雄之旅 叙事 结构", "竞技体育 实力 差距 心理"),
    "无限流": ("极限 生存 决策 心理", "灾难 逃生 案例", "游戏 关卡 设计"),
    "克苏鲁": ("洛夫克拉夫特 神话 体系", "未知 恐惧 心理学", "海洋 怪谈 传说"),
    "末日": ("灾难 生存 物资 储备", "应急 管理 灾害 案例", "末日 社会 秩序"),
    "历史": ("古代 官制 税制 科举", "古代 城市 生活", "史料 考据 方法"),
    "武侠": ("武术 流派 历史", "江湖 帮会 历史", "古代 兵器 器械"),
    "种田": ("古代 农业 技术", "乡村 治理 宗族", "手工业 商业 历史"),
}
_SUBGENRE_KEYS = tuple(sorted(_SUBGENRE_QUERIES, key=len, reverse=True))


def _detect_subgenres(project: Project, theme: str) -> list[str]:
    """按「书名 > 简介 > 类型 > theme」的优先级识别流派。

    书名里的流派词最可靠（如《开局就无敌》），不能被 theme 里的「修仙/都市」
    先占满 3 个名额，否则无敌/系统这类核心流派会漏掉。
    """
    sources = (
        (str(project.title or ""), 4),
        (str(project.premise or ""), 3),
        (str(project.genre or ""), 2),
        (str(theme or ""), 1),
    )
    found: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for text, priority in sources:
        for key in _SUBGENRE_KEYS:
            if key in text and key not in seen:
                seen.add(key)
                found.append((priority, len(found), key))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [key for _priority, _order, key in found[:3]]


def _ensure_subgenre_queries(
    queries: list[str], project: Project, theme: str, *, limit: int
) -> list[str]:
    """给识别到的流派各补 1 条现实原型查询，保证机制类素材不被漏掉。"""
    out = list(queries)
    for key in _detect_subgenres(project, theme):
        for candidate in _SUBGENRE_QUERIES.get(key, ())[:1]:
            if candidate not in out:
                out.append(candidate)
    return out[:limit]


def _reference_basis(
    project: Project, theme: str, topics: list[str] | None
) -> list[str]:
    """reference 查询的题材词：标题/简介里的流派词优先，其次主题与方向。"""
    out: list[str] = []

    def add(term: str) -> None:
        term = " ".join(str(term or "").split()).strip()
        if 2 <= len(term) <= 16 and term not in out:
            out.append(term)

    for source in (project.title, project.premise, project.genre):
        text = str(source or "")
        for keyword in _GENRE_HINTS:
            if keyword in text:
                add(keyword)
    for piece in re.split(r"[/、,，;；|]+", str(theme or "")):
        add(piece)
    for topic in topics or []:
        add(str(topic))
    if not out:
        add(str(project.genre or "同类题材"))
    return out[:6]


def fallback_reference_queries(
    project: Project,
    *,
    theme: str = "",
    topics: list[str] | None = None,
    limit: int = 4,
) -> list[str]:
    """模型没给 reference 时的兜底：用题材/方向拼同题材网文检索词。"""
    basis = _reference_basis(project, theme, topics)
    suffixes = (" 网文 代表作", " 小说 推荐", " 网文 推荐", " 小说 书单")
    out: list[str] = []
    for index, term in enumerate(basis):
        query = f"{term}{suffixes[index % len(suffixes)]}".strip()
        if query not in out:
            out.append(query)
        if len(out) >= limit:
            break
    return out


def _sanitize_term(text: str) -> str:
    """去掉术语里的「世界观/设定」等检索噪声，而不是整条丢弃。"""
    cleaned = str(text or "")
    for word in _QUERY_BANNED:
        cleaned = cleaned.replace(word, "")
    return " ".join(cleaned.split()).strip("。！？?!；;，,、")


def _char_bigrams(text: str) -> set[str]:
    raw = re.sub(r"\s+", "", str(text or "")).lower()
    if len(raw) < 2:
        return {raw} if raw else set()
    return {raw[i : i + 2] for i in range(len(raw) - 1)}


#: 常见「都市/租房」载体词。模型给的 surface 往往只列一两个词，这里再兜一层。
_CARRIER_HINTS = (
    "租房", "出租", "转租", "承租", "收租", "催租", "招租", "租赁",
    "租客", "房东", "押金", "交房", "水电", "抄表", "二手房", "空置房",
    "物业", "中介", "合同",
)


def _looks_like_carrier(
    query: str, carrier_grams: set[str], theme_grams: set[str]
) -> bool:
    """查询是否主要在复述故事载体（简介里的租房/交房/水电），而非主题知识。"""
    if any(hint in query for hint in _CARRIER_HINTS):
        return True
    grams = _char_bigrams(query)
    if not grams:
        return False
    carrier_hits = len(grams & carrier_grams)
    theme_hits = len(grams & theme_grams)
    return carrier_hits >= 2 and carrier_hits > theme_hits


def _split_terms(items: Any, limit: int = 8) -> list[str]:
    """把模型返回的「a、b、c」拆成独立词条，避免整串被当成一个词。"""
    out: list[str] = []
    if isinstance(items, str):
        items = [items]
    for item in items or []:
        for piece in re.split(r"[、,，;；/\s]+", str(item or "")):
            piece = piece.strip()
            if 2 <= len(piece) <= 32 and piece not in out:
                out.append(piece)
            if len(out) >= limit:
                return out
    return out



def _provider():
    from app.llm.provider import build_role_provider

    return build_role_provider("assistant")


def _extract_json(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # 取「最先出现」的开括号类型：JSON 数组以 `[` 开头时不能被内部对象的 `{` 抢先。
    candidates: list[tuple[int, str, str]] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start >= 0 and end > start:
            candidates.append((start, opener, closer))
    for _, opener, closer in sorted(candidates, key=lambda item: item[0]):
        start, end = text.find(opener), text.rfind(closer)
        try:
            return json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            continue
    return None


def fallback_queries(
    project: Project,
    chapter: int,
    limit: int = 20,
    *,
    theme: str = "",
    needs: list[str] | None = None,
) -> list[str]:
    """规则兜底：主题/需求优先，其次章纲、节点、人物身份与标题。

    不再把整段 premise 当种子——简介里全是「租房、交房、水电」这类载体词，
    直接拆词会把检索带偏到生活流程上。
    """
    brief = chapter_brief(project, chapter)
    seeds: list[str] = []
    if theme:
        seeds.append(theme)
    seeds.extend(str(n) for n in (needs or []) if str(n).strip())
    for raw in (
        brief["genre"],
        brief["outline"],
        brief["node"],
        "；".join(brief["characters"]),
        brief["title"],
    ):
        for piece in re.split(r"[，。；、,.;\s《》【】]+", str(raw or "")):
            piece = piece.strip()
            if 2 <= len(piece) <= 18:
                seeds.append(piece)
    seen: set[str] = set()
    base: list[str] = []
    for seed in seeds:
        if seed in seen:
            continue
        seen.add(seed)
        base.append(seed)
    subjects = base[:14] or [theme or brief["genre"] or "写作背景"]
    out: list[str] = []
    for subject in subjects:
        for suffix in (" 设定 背景", " 细节 常识"):
            query = f"{subject}{suffix}".strip()
            if query not in out:
                out.append(query)
            if len(out) >= limit:
                return out
    return out or ["生活细节 资料"]


def _clean_queries(
    queries: list[str],
    *,
    surface: list[str],
    theme: str,
    needs: list[str],
    limit: int,
    blocked_terms: list[str] | None = None,
    premise: str = "",
    topics: list[str] | None = None,
) -> list[str]:
    """给载体词设配额，并用主题/需求补足，避免查询被同一个表层设定刷屏。"""
    blocked = [t for t in (blocked_terms or []) if t]
    unique: list[str] = []
    for raw in queries:
        query = _normalize_query(raw)
        if not query:
            continue
        if any(term in query for term in blocked):
            continue
        if _LOCATION_NOUN_RE.search(query):
            continue
        if any(word in query for word in _QUERY_BANNED):
            continue
        if query not in unique:
            unique.append(query)

    # 语义层：简介里有、但 theme/needs 里没有的词，视为「载体词」。
    premise_grams = _char_bigrams(premise)
    theme_grams = _char_bigrams(" ".join([theme, *needs, *(topics or [])]))
    carrier_grams = premise_grams - theme_grams

    surface_like_kept = 0
    kept: list[str] = []
    for query in unique:
        is_surface_like = False
        hit = next((term for term in surface if term and term in query), None)
        if hit:
            is_surface_like = True
        if carrier_grams and _looks_like_carrier(query, carrier_grams, theme_grams):
            is_surface_like = True
        if is_surface_like:
            if surface_like_kept >= MAX_SURFACE_QUERIES:
                continue
            surface_like_kept += 1
        kept.append(query)

    extras: list[str] = []
    for need in needs:
        term = _sanitize_term(need)
        if not term:
            continue
        extras.append(f"{term} 现实 资料")
    for query in extras:
        if len(kept) >= 8:
            break
        if query not in kept:
            kept.append(query)
    return kept[:limit]


def infer_topics(
    db: Session,
    project: Project,
    chapter: int,
    *,
    settings: MaterialSettings | None = None,
    provider: Any | None = None,
) -> dict[str, Any]:
    """模型推断调研主题与查询词；失败回退规则查询。"""
    cfg = settings or load_settings()
    brief = chapter_brief(project, chapter)
    queries: list[str] = []
    reference_queries: list[str] = []
    topics: list[str] = []
    theme = ""
    surface: list[str] = []
    needs: list[str] = []
    source = "model"
    try:
        llm = provider or _provider()
        user = _TOPIC_USER.format(
            title=brief["title"] or "未命名",
            genre=brief["genre"] or "未指定",
            synopsis=brief["synopsis"] or brief["premise"] or "（未填写）",
            conflicts=brief["conflicts"] or "（暂无）",
            background=brief["background"] or "（暂无）",
            direction=brief["direction"] or "（暂无）",
            outline=brief["outline"] or "（本章暂无章纲）",
            node=brief["node"] or "（暂无时间线节点）",
            characters="；".join(brief["characters"]) or "（暂无）",
        )
        data = _extract_json(str(llm.chat(_TOPIC_SYSTEM, user) or ""))
        if isinstance(data, dict):
            theme = str(data.get("theme") or "").strip()
            surface = _split_terms(data.get("surface"), limit=8)
            needs = _split_terms(data.get("needs"), limit=8)
            topics = _split_terms(data.get("topics"), limit=8)
            queries, reference_queries = _queries_from_payload(data.get("queries"))
    except Exception as exc:  # noqa: BLE001 —— 模型失败退回规则
        logger.warning("素材主题推断失败，退回规则查询：%s", exc)
        source = "fallback"

    blocked = _blocked_terms(project)
    queries = _clean_queries(
        queries,
        surface=surface,
        theme=theme,
        needs=needs,
        limit=cfg.max_queries,
        blocked_terms=blocked,
        premise=brief["premise"],
        topics=topics,
    )
    if len(queries) < 4:
        extra = fallback_queries(
            project, chapter, cfg.max_queries, theme=theme, needs=needs
        )
        queries = _clean_queries(
            _dedupe(queries + extra),
            surface=surface,
            theme=theme,
            needs=needs,
            limit=cfg.max_queries,
            blocked_terms=blocked,
            premise=brief["premise"],
            topics=topics,
        )
        source = "fallback" if source == "model" else source

    # 流派机制兜底：系统/无敌/穿越/诡异等至少各补 1 条现实原型查询。
    queries = _ensure_subgenre_queries(queries, project, theme, limit=cfg.max_queries)

    # 同题材网文参考：允许小说/网文词，单独清洗，不参与知识查询的载体配额。
    refs = _clean_reference_queries(
        reference_queries,
        blocked_terms=blocked,
        exclude=queries,
        limit=6,
    )
    # 始终补两条按题材生成的标准 reference 查询，避免模型只给出无关书名。
    fallback_refs = fallback_reference_queries(
        project, theme=theme, topics=topics or needs, limit=4
    )
    refs = _dedupe(fallback_refs[:2] + refs)[:6]
    if refs:
        queries = _dedupe(queries[: max(1, cfg.max_queries - len(refs))] + refs)

    if not topics:
        topics = needs or _dedupe(queries)[:6]
    return {
        "chapter": chapter,
        "theme": theme,
        "surface": surface,
        "needs": needs,
        "topics": topics,
        "queries": queries,
        "reference_queries": refs,
        "source": source,
        "brief": brief,
    }


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #

#: 免费源被验证码拦截后的进程级冷却时间表（provider -> monotonic 截止时间）。
_COOLDOWN_UNTIL: dict[str, float] = {}
_COOLDOWN_LOCK = threading.Lock()
FREE_PROVIDER_KEY = "free:baidu+ddg+bing"


def reset_cooldowns() -> None:
    """测试 / 手动恢复用：清空所有源的冷却状态。"""
    with _COOLDOWN_LOCK:
        _COOLDOWN_UNTIL.clear()


class SearchRunner:
    """按配置选择检索源；一个任务复用一个实例（免费源有全局节流）。"""

    def __init__(self, db: Session, settings: MaterialSettings | None = None) -> None:
        self.settings = settings or load_settings()
        self._db = db
        self._free = None
        self._web = False
        try:
            from app.web_search import service

            self._web = bool(service.is_configured(db))
        except Exception:  # noqa: BLE001 —— 配置读不到就走免费源
            self._web = False
        if not self._web:
            from app.retrieval.brief.backend import FreeSearchBackend

            self._free = FreeSearchBackend(
                timeout_s=8.0,
                min_interval_s=self.settings.search_min_interval_s,
            )
        self._free_key = FREE_PROVIDER_KEY

    @property
    def provider_name(self) -> str:
        if self._web:
            try:
                from app.web_search import service

                return f"web_search:{service.load_config(self._db).provider}"
            except Exception:  # noqa: BLE001
                return "web_search"
        return FREE_PROVIDER_KEY

    def search(self, query: str, *, count: int | None = None) -> tuple[list[dict[str, Any]], str]:
        """返回 (结果列表, 错误)。结果项字段：title/snippet/url/published_at。"""
        n = max(1, int(count or self.settings.search_per_query))
        if self._web:
            try:
                from app.web_search import service

                bundle = service.search(self._db, query, force=True, max_results=n)
                if bundle is None:
                    return [], "搜索服务未返回结果"
                if bundle.error:
                    return [], str(bundle.error)
                return (
                    [
                        {
                            "title": item.title,
                            "snippet": item.snippet,
                            "url": item.url,
                            # 渠道发布时间是判断素材是否过期的第一优先证据。
                            "published_at": item.published_at or "",
                        }
                        for item in bundle.results
                    ],
                    "",
                )
            except Exception as exc:  # noqa: BLE001
                return [], repr(exc)
        if self._free is None:
            return [], "未配置检索后端"
        with _COOLDOWN_LOCK:
            until = _COOLDOWN_UNTIL.get(self._free_key, 0.0)
        if time.monotonic() < until:
            return [], "source_cooldown"
        from app.retrieval.brief.backend import SearchError

        try:
            results = self._free.search(query, n)
            return (
                [
                    {
                        "title": r.title,
                        "snippet": r.snippet,
                        "url": r.url,
                        "published_at": "",
                    }
                    for r in results
                ],
                "",
            )
        except SearchError as exc:
            # 被验证码/反爬拦下：进入进程级冷却，期间所有任务不再打这个源。
            if getattr(exc, "blocked", False):
                with _COOLDOWN_LOCK:
                    _COOLDOWN_UNTIL[self._free_key] = (
                        time.monotonic()
                        + max(1.0, float(self.settings.block_cooldown_s))
                    )
                return [], f"blocked: {exc}"[:200]
            return [], repr(exc)
        except Exception as exc:  # noqa: BLE001 —— 网络异常也降级
            return [], repr(exc)


# --------------------------------------------------------------------------- #
# 整理（每批 10 条）
# --------------------------------------------------------------------------- #

_CONSOLIDATE_SYSTEM = (
    "你是中文网文的资料整理编辑。只输出 JSON 数组，不要 Markdown，不要解释。"
)

_CONSOLIDATE_USER = """下面是从网上检索到的原始素材。请逐条整理成可用于小说写作的素材卡：
要求：
- 保留原文的事实，不要编造；无法确认的信息不要写；
- summary：不超过 120 字，写成可直接查用的事实摘要；
- use_case：明确写「可用在什么地方」，必须针对这条素材本身，不能套用批次里其他条目的章节/场景；
- category：4–8 字的目录分类；
- published_year：原文能看出的年份填整数，看不出填 null；
- tags：2–4 个关键词。

严格返回 JSON 数组，每项：{{"index": 1, "summary": "...", "use_case": "...", "category": "...", "published_year": null, "tags": ["..."]}}

素材：
{items}
"""


def consolidate(
    db: Session,
    rows: list[ProjectMaterial],
    *,
    provider: Any | None = None,
    settings: MaterialSettings | None = None,
) -> int:
    """把一批 raw 素材整理成 usable；返回成功整理条数。

    模型不可用或输出不可解析时，逐条走启发式标注，保证素材不会卡在 raw。
    """
    if not rows:
        return 0
    cfg = settings or load_settings()
    payload = []
    for index, row in enumerate(rows, 1):
        payload.append(
            {
                "index": index,
                "title": row.title,
                "snippet": (row.snippet or "")[:400],
                "url": row.url or "",
                "source_query": row.query or "",
            }
        )
    result: dict[int, dict[str, Any]] = {}
    by_model = False
    try:
        llm = provider or _provider()
        user = _CONSOLIDATE_USER.format(
            items=json.dumps(payload, ensure_ascii=False, indent=1)
        )
        data = _extract_json(str(llm.chat(_CONSOLIDATE_SYSTEM, user) or ""))
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    idx = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                result[idx] = item
            by_model = bool(result)
    except Exception as exc:  # noqa: BLE001 —— 模型失败走启发式
        logger.warning("素材整理模型调用失败，逐条启发式标注：%s", exc)

    done = 0
    for index, row in enumerate(rows, 1):
        item = result.get(index) or {}
        summary = str(item.get("summary") or "").strip()
        use_case = str(item.get("use_case") or "").strip()
        category = str(item.get("category") or "").strip()
        year = item.get("published_year")
        tags = item.get("tags")
        if by_model and summary:
            row.summary = summary[:400]
            row.use_case = (use_case or row.use_case or "")[:300] or None
            row.category = (category or row.category or "")[:64] or None
            # 模型常把年份写成「2023 年 / 2023-05」；能解析且能在原文里找到同一年才采纳，
            # 防止模型编出一个原文没有的年份。
            model_year = _coerce_model_year(year, row)
            if model_year is not None:
                row.published_year = model_year
                row.published_at = str(model_year)
            if isinstance(tags, list) and tags:
                row.tags = [str(t)[:24] for t in tags[:6]]
            row.consolidated_by = "model"
        else:
            row.summary = row.summary or (row.snippet or row.title)[:200]
            row.use_case = row.use_case or _heuristic_use_case(row)
            row.category = row.category or _heuristic_category(row)
            row.consolidated_by = "heuristic"
        # 同类小说页保留为参考，但统一归到「同类小说参考」，并提示只学写法不照抄。
        if is_fiction_result(row.title or "", row.snippet or "", row.url or ""):
            row.category = FICTION_CATEGORY
            row.use_case = row.use_case or "参考同类小说的世界观、桥段与写法，不要照抄"
            tags = [str(t) for t in (row.tags or []) if str(t).strip()]
            if FICTION_CATEGORY not in tags:
                tags.append(FICTION_CATEGORY)
            row.tags = tags[:6]
        row.status = "usable"
        done += 1
    db.commit()
    return done


def _coerce_model_year(value: Any, row: ProjectMaterial) -> int | None:
    """把模型给的年份变成整数，并要求该年份能在素材原文里找到。"""
    if value in (None, ""):
        return None
    match = _YEAR_RE.search(str(value))
    if not match:
        return None
    year = int(match.group(1))
    if not (1950 <= year <= 2100):
        return None
    haystack = f"{row.title or ''} {row.snippet or ''} {row.url or ''} {row.published_at or ''}"
    return year if str(year) in haystack else None


def _heuristic_use_case(row: ProjectMaterial) -> str:
    query = str(row.query or "").strip()
    if query:
        return f"补充「{query[:40]}」相关细节"
    return "补充背景细节"


def finalize_fiction_rows(db: Session, project_id: int) -> int:
    """同类小说参考不占可用素材目标，但也要进大目录：直接标 usable。

    这些页面不是知识素材，不再额外调模型整理；保留标题/摘要即可供参考。
    """
    rows = db.scalars(
        select(ProjectMaterial).where(
            ProjectMaterial.project_id == project_id,
            ProjectMaterial.status == "raw",
            ProjectMaterial.category == FICTION_CATEGORY,
        )
    ).all()
    for row in rows:
        row.summary = row.summary or (row.snippet or row.title or "")[:200]
        row.use_case = row.use_case or "参考同类小说的世界观、桥段与写法，不要照抄"
        row.status = "usable"
        row.consolidated_by = "heuristic"
    if rows:
        db.commit()
    return len(rows)


def _heuristic_category(row: ProjectMaterial) -> str:
    text = f"{row.query or ''} {row.title or ''}"
    for keyword, label in (
        ("历史", "历史背景"),
        ("职业", "职业流程"),
        ("城市", "城市地理"),
        ("制度", "制度规则"),
        ("器物", "器物生活"),
        ("科技", "科技常识"),
        ("医学", "医学常识"),
        ("军事", "军事常识"),
    ):
        if keyword in text:
            return label
    return "综合素材"


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = " ".join(str(item or "").split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
