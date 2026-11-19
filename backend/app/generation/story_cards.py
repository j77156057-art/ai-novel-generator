"""设定卡：人物 / 势力 / 场景 / 道具 / 规则 / 自定义的字段表、校验与提示词渲染。

卡片是**作者意图**，不是从正文抽取的事实（`kg_*` / `facts` 那套是已发生事实）。
所以本模块只做三件事：

1. 定义字段白名单（后端唯一权威，前端用 GET 接口拿同一份 schema 渲染表单）；
2. 校验用户提交（未知字段键直接拒绝，不静默丢弃）；
3. 把启用的卡片渲染成提示块。

**空块零影响**：项目没有任何启用卡片时 `render_block` 返回空串，
`build_user_prompt` 不会追加任何字符，与旧版提示逐字节一致。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func

from app.models import KgEntity, StoryCard

#: 卡片类型（顺序即提示词里的分组顺序）。
#:
#: 为什么是这 6 类：生成时真正会缺的信息只有「写谁 / 谁背后的集体 / 在哪 /
#: 用什么 / 世界的运行规则」这几块，其余长尾（种族、怪物、术语…）用自定义卡兜底。
#: 关系不单独建卡（人物卡已有人际关系字段），伏笔与事件也不建卡——它们分别由
#: plot_bible.foreshadowing / KG / 时间线 / 章纲承载，再建一套会出现两处权威。
KINDS = ("character", "faction", "scene", "item", "rule", "custom")

KIND_LABELS = {
    "character": "人物卡",
    "faction": "势力卡",
    "scene": "场景卡",
    "item": "道具卡",
    "rule": "规则卡",
    "custom": "自定义卡",
}

#: 每张卡最多保留多少字（去空白后）；超出直接拒绝，不静默截断用户输入。
MAX_FIELD_CHARS = 1000

#: 卡片名称上限。与 models.StoryCard.title 的 160 留余量，按字符计。
MAX_TITLE_CHARS = 80

#: 自定义卡的字段数量与字段名长度上限（防止界面和提示词被无限撑大）。
MAX_CUSTOM_FIELDS = 20
MAX_CUSTOM_KEY_CHARS = 30

#: 整个设定卡提示块的长度上限。超出时按卡整张省略并注明，不切半张卡。
MAX_BLOCK_CHARS = 6000

#: 字段表。key 进数据库 JSON，label 进提示词与界面，hint 只给界面做占位说明。
FIELD_SCHEMA: dict[str, list[dict[str, Any]]] = {
    "character": [
        {"key": "identity", "label": "身份 / 定位",
         "hint": "职业、阶层、在故事中的位置（如「外冷内热的急诊科主治」）",
         "multiline": True, "max_chars": 600},
        {"key": "alias", "label": "别名 / 称呼",
         "hint": "别人怎么叫他，多个用顿号分隔", "multiline": False, "max_chars": 200},
        {"key": "appearance", "label": "外貌",
         "hint": "长相、穿着、标志性细节", "multiline": True, "max_chars": 600},
        {"key": "personality", "label": "性格",
         "hint": "核心性格与矛盾点，避免只写形容词堆砌", "multiline": True, "max_chars": 600},
        {"key": "motivation", "label": "动机 / 目标",
         "hint": "他想要什么、怕失去什么、为什么", "multiline": True, "max_chars": 600},
        {"key": "speech", "label": "说话方式",
         "hint": "口头禅、语气、称呼习惯、句长偏好", "multiline": True, "max_chars": 600},
        {"key": "status", "label": "当前状态",
         "hint": "下一次生成开始时他处在什么状态（伤病、情绪、身份暴露程度）",
         "multiline": True, "max_chars": 600},
        {"key": "relations", "label": "人际关系",
         "hint": "与主要人物的关系及张力，一行一条", "multiline": True, "max_chars": 800},
        {"key": "taboo", "label": "禁忌 / 不会做的事",
         "hint": "用来防止模型写崩人设（如「绝不主动求人」）", "multiline": True, "max_chars": 600},
        {"key": "notes", "label": "备注",
         "hint": "其它必须记住的设定", "multiline": True, "max_chars": 800},
    ],
    "scene": [
        {"key": "scene_type", "label": "场景类型",
         "hint": "如「都市夜景」「宗门大殿」「密闭车厢」", "multiline": False, "max_chars": 200},
        {"key": "location", "label": "地点",
         "hint": "具体到可辨认的位置与周边环境", "multiline": True, "max_chars": 600},
        {"key": "time", "label": "时间",
         "hint": "故事内时间、季节、天气", "multiline": False, "max_chars": 300},
        {"key": "atmosphere", "label": "氛围",
         "hint": "读者进入这个场景时应有的情绪基调", "multiline": True, "max_chars": 600},
        {"key": "senses", "label": "感官细节",
         "hint": "声音、气味、温度、触感等可调用的具体细节", "multiline": True, "max_chars": 800},
        {"key": "characters", "label": "出场人物",
         "hint": "本场景通常会出现谁（不是强制名单，是默认阵容）",
         "multiline": True, "max_chars": 600},
        {"key": "rules", "label": "场景规则",
         "hint": "这个空间的特殊规则、禁忌、危险（如「午夜后不能开灯」）",
         "multiline": True, "max_chars": 800},
        {"key": "notes", "label": "备注",
         "hint": "其它必须记住的设定", "multiline": True, "max_chars": 800},
    ],
    "item": [
        {"key": "category", "label": "类别",
         "hint": "如「法器」「证件」「日常小物」「关键信物」", "multiline": False, "max_chars": 200},
        {"key": "appearance", "label": "外观",
         "hint": "材质、尺寸、颜色、磨损、标识", "multiline": True, "max_chars": 600},
        {"key": "origin", "label": "来源",
         "hint": "谁造的、从哪来、怎么到手", "multiline": True, "max_chars": 600},
        {"key": "owner", "label": "持有者",
         "hint": "现在在谁手上；无人持有时写清放在哪", "multiline": False, "max_chars": 300},
        {"key": "function", "label": "功能",
         "hint": "它能做什么，具体到可验证的效果", "multiline": True, "max_chars": 800},
        {"key": "limits", "label": "限制 / 代价",
         "hint": "使用条件、消耗、副作用、失效方式", "multiline": True, "max_chars": 800},
        {"key": "first_appearance", "label": "首次出现",
         "hint": "第几章 / 哪一段首次出现，便于保持连续", "multiline": False, "max_chars": 200},
        {"key": "notes", "label": "备注",
         "hint": "其它必须记住的设定", "multiline": True, "max_chars": 800},
    ],
    "faction": [
        {"key": "faction_type", "label": "类型",
         "hint": "宗门 / 公司 / 家族 / 军方 / 教团 / 帮派…", "multiline": False, "max_chars": 200},
        {"key": "scale", "label": "规模 / 实力",
         "hint": "人数、层级、在势力版图中的位置", "multiline": False, "max_chars": 300},
        {"key": "leader", "label": "首领 / 核心人物",
         "hint": "谁说了算，核心圈还有谁", "multiline": True, "max_chars": 600},
        {"key": "stance", "label": "立场 / 阵营",
         "hint": "与主角、与其它势力的敌友关系", "multiline": True, "max_chars": 600},
        {"key": "goal", "label": "目标 / 诉求",
         "hint": "这个组织现在最想得到或阻止什么", "multiline": True, "max_chars": 600},
        {"key": "resources", "label": "资源 / 底蕴",
         "hint": "钱、人、情报、技术、靠山等可调用资源", "multiline": True, "max_chars": 800},
        {"key": "style", "label": "行事风格",
         "hint": "对外处事方式与内部规矩", "multiline": True, "max_chars": 600},
        {"key": "territory", "label": "势力范围 / 据点",
         "hint": "控制哪些地方、常在哪些场景活动", "multiline": True, "max_chars": 600},
        {"key": "relations", "label": "与其他势力的关系",
         "hint": "一行一条，写明合作、敌对或表面和平", "multiline": True, "max_chars": 800},
        {"key": "weakness", "label": "弱点 / 内部矛盾",
         "hint": "可以用来制造冲突的裂缝", "multiline": True, "max_chars": 600},
        {"key": "notes", "label": "备注",
         "hint": "其它必须记住的设定", "multiline": True, "max_chars": 800},
    ],
    "rule": [
        {"key": "rule_type", "label": "类型",
         "hint": "功法 / 科技 / 异能 / 世界规则 / 禁忌", "multiline": False, "max_chars": 200},
        {"key": "origin", "label": "来源 / 体系",
         "hint": "谁定的、从哪里学来、属于哪套体系", "multiline": True, "max_chars": 600},
        {"key": "scope", "label": "生效范围 / 适用对象",
         "hint": "对谁有效、在什么条件下有效", "multiline": True, "max_chars": 600},
        {"key": "effect", "label": "效果 / 表现",
         "hint": "具体到可验证的效果，不要只写「很强」", "multiline": True, "max_chars": 800},
        {"key": "cost", "label": "代价 / 限制",
         "hint": "消耗、反噬、冷却、失效条件", "multiline": True, "max_chars": 800},
        {"key": "tiers", "label": "等级 / 阶段",
         "hint": "从低到高的阶梯，写清每级的判定标准", "multiline": True, "max_chars": 800},
        {"key": "users", "label": "已知使用者",
         "hint": "谁掌握或正在修炼；写清熟练度", "multiline": True, "max_chars": 600},
        {"key": "counters", "label": "破解 / 克制方式",
         "hint": "怎么打断、免疫或反制", "multiline": True, "max_chars": 600},
        {"key": "notes", "label": "备注",
         "hint": "其它必须记住的设定", "multiline": True, "max_chars": 800},
    ],
    # 自定义卡不走白名单：字段名由用户填写，校验只看数量与长度。
    "custom": [],
}


def _kind_order(kind: str) -> int:
    return KINDS.index(kind) if kind in KINDS else len(KINDS)


def field_schema_payload() -> list[dict[str, Any]]:
    """给前端渲染表单用的 schema（与提示词字段共用同一份常量）。

    `freeform=True` 表示该类型没有固定字段表：界面渲染「字段名 + 内容」的
    可增删行，校验走自定义分支（只限数量与长度）。
    """
    return [
        {
            "kind": kind,
            "label": KIND_LABELS[kind],
            "fields": FIELD_SCHEMA[kind],
            "freeform": kind == "custom",
        }
        for kind in KINDS
    ]


def normalize_card_input(
    kind: str, title: str, fields: dict | None
) -> tuple[str, str, dict[str, str]]:
    """校验并归一化一张卡；非法输入抛 `ValueError`（路由转 422）。

    返回 `(kind, title, fields)`，其中 `fields` 只保留白名单内的非空文本。
    """
    normalized_kind = str(kind or "").strip()
    if normalized_kind not in KINDS:
        raise ValueError(f"未知卡片类型「{kind}」，只支持：{' / '.join(KINDS)}")

    normalized_title = str(title or "").strip()
    if not normalized_title:
        raise ValueError("卡片名称不能为空")
    if len(normalized_title) > MAX_TITLE_CHARS:
        raise ValueError(f"卡片名称不能超过 {MAX_TITLE_CHARS} 字")

    source = fields if isinstance(fields, dict) else {}

    # 自定义卡：字段名由用户填写，不做白名单；空值保留，避免界面空行在一次
    # 失焦保存后消失。数量与长度仍设上限，防止提示词被无限撑大。
    if normalized_kind == "custom":
        if len(source) > MAX_CUSTOM_FIELDS:
            raise ValueError(f"自定义卡最多 {MAX_CUSTOM_FIELDS} 个字段")
        custom_cleaned: dict[str, str] = {}
        for key, raw in source.items():
            field_key = str(key).strip()
            if not field_key:
                raise ValueError("自定义字段名不能为空")
            if len(field_key) > MAX_CUSTOM_KEY_CHARS:
                raise ValueError(
                    f"自定义字段名不能超过 {MAX_CUSTOM_KEY_CHARS} 字"
                )
            if field_key in custom_cleaned:
                raise ValueError(f"自定义字段名重复：「{field_key}」")
            value = str(raw if raw is not None else "").strip()
            if len(value) > MAX_FIELD_CHARS:
                raise ValueError(
                    f"字段「{field_key}」超过 {MAX_FIELD_CHARS} 字，请精简后再保存"
                )
            custom_cleaned[field_key] = value
        return normalized_kind, normalized_title, custom_cleaned

    allowed = {spec["key"] for spec in FIELD_SCHEMA[normalized_kind]}
    cleaned: dict[str, str] = {}
    for key, raw in source.items():
        field_key = str(key)
        if field_key not in allowed:
            raise ValueError(
                f"{KIND_LABELS[normalized_kind]}没有字段「{field_key}」"
            )
        value = str(raw if raw is not None else "").strip()
        if len(value) > MAX_FIELD_CHARS:
            raise ValueError(
                f"字段「{field_key}」超过 {MAX_FIELD_CHARS} 字，请精简后再保存"
            )
        if value:
            cleaned[field_key] = value
    return normalized_kind, normalized_title, cleaned


def render_block(cards: Iterable[StoryCard]) -> str:
    """把启用的卡片渲染成提示块；没有任何启用卡片时返回空串。"""
    picked: list[tuple[str, int, int, str, StoryCard]] = []
    for card in cards:
        kind = str(getattr(card, "kind", "") or "")
        title = str(getattr(card, "title", "") or "").strip()
        if kind not in KINDS or not title or not bool(getattr(card, "enabled", True)):
            continue
        picked.append((
            kind,
            int(getattr(card, "sort_order", 0) or 0),
            int(getattr(card, "id", 0) or 0),
            title,
            card,
        ))
    if not picked:
        return ""
    picked.sort(key=lambda item: (_kind_order(item[0]), item[1], item[2]))

    lines: list[str] = []
    used = 0
    truncated = False
    for kind in KINDS:
        group = [item for item in picked if item[0] == kind]
        if not group:
            continue
        header = f"◆ {KIND_LABELS[kind]}"
        lines.append(header)
        used += len(header) + 1
        for index, (_, _, _, title, card) in enumerate(group, 1):
            entry = [f"{index}. {title}"]
            fields = getattr(card, "fields", None) or {}
            if kind == "custom":
                # 自定义卡：字段名就是 label，按用户填写顺序输出。
                field_items = [
                    (str(key), str(value or "").strip())
                    for key, value in fields.items()
                ]
            else:
                field_items = [
                    (spec["label"], str(fields.get(spec["key"]) or "").strip())
                    for spec in FIELD_SCHEMA[kind]
                ]
            for label, value in field_items:
                if not value:
                    continue
                # 字段内换行折成空格：提示词里每张卡保持紧凑，不撑成多段。
                value = value.replace("\r\n", "\n").replace("\n", " ")
                entry.append(f"   - {label}：{value}")
            entry_text = "\n".join(entry)
            if used + len(entry_text) + 1 > MAX_BLOCK_CHARS:
                truncated = True
                break
            lines.append(entry_text)
            used += len(entry_text) + 1
        if truncated:
            break
    if truncated:
        lines.append(
            "（设定卡超出长度上限，其余卡片已省略；可精简卡片内容或关闭部分卡片后重新生成。）"
        )
    return "\n".join(lines)


def render_for_project(db, project_id: int) -> str:
    """按项目读取卡片并渲染；供 4 个生成/预检调用点使用。"""
    cards = (
        db.query(StoryCard)
        .filter(StoryCard.project_id == project_id)
        .all()
    )
    return render_block(cards)


# ---------------------------------------------------------------------------
# 从项目已有内容同步卡片（剧情圣经 + 知识图谱）
# ---------------------------------------------------------------------------


#: KG 实体类型 → 卡片类型。`info` 是情报/秘密，没有对应卡片形态，忽略。
KG_KIND_MAP = {
    "character": "character",
    "faction": "faction",
    "location": "scene",
    "item": "item",
    "worldrule": "rule",
    "power_system": "rule",
}

#: 每类最多同步多少张。上限按提示词预算定：设定卡整块 6000 字，
#: 无脑导入上百张会让后面的卡被截断——导了但不生效，比不导更误导。
MAX_IMPORT_PER_KIND = {
    "character": 22,
    "faction": 6,
    "scene": 8,
    "item": 8,
    "rule": 6,
}

#: 这些是抽取器常产出的占位名，导成卡片只会占位置。
KG_NAME_STOPLIST = {"未知", "无", "其它", "其他", "不详", "未命名", "未知地点"}


def _clip(value: Any, limit: int = 900) -> str:
    """同步出来的字段统一截到 limit 字，避免超过 normalize 的 1000 字上限。"""
    return str(value if value is not None else "").strip()[:limit]


def _bible_character_fields(char: dict) -> dict[str, str]:
    """剧情圣经角色 → 人物卡字段（只搬运已有的，不编造）。

    **优先读新角色卡**（`char["card"]` 的分组结构：身份/能力/声纹/量级/关系），
    旧 persona 的 `voice` 平铺结构作为兜底。这样角色卡生成后，设定卡视图
    能直接看到身份、说话方式、禁忌与量级，而不只是「名字 + 定位」。
    """
    card = char.get("card") if isinstance(char.get("card"), dict) else {}
    ident = card.get("身份") if isinstance(card.get("身份"), dict) else {}
    cvoice = card.get("声纹") if isinstance(card.get("声纹"), dict) else {}
    ability = card.get("能力") if isinstance(card.get("能力"), dict) else {}
    scale = card.get("量级") if isinstance(card.get("量级"), dict) else {}
    crel = card.get("关系") if isinstance(card.get("关系"), dict) else {}

    # —— 旧 persona（voice 平铺）兜底 ——
    voice = char.get("voice") if isinstance(char.get("voice"), dict) else {}

    def _text(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return "、".join(str(x).strip() for x in value if str(x).strip())
        return str(value or "").strip()

    # 身份 / 外貌
    identity_bits = [
        _text(char.get("role")),
        _text(ident.get("身份职业")),
        _text(ident.get("出身背景")),
    ]
    appearance = _text(ident.get("外貌"))

    # 说话方式：新卡优先，旧 voice 兜底
    speech: list[str] = []
    if cvoice:
        for label, key in (("自称", "自称"), ("称呼", "称呼他人"),
                           ("句长", "句长"), ("句末", "句末")):
            value = _text(cvoice.get(key))
            if value:
                speech.append(f"{label}：{value}")
        tics = _text(cvoice.get("口头禅"))
        if tics:
            speech.append("口头禅：" + tics)
    else:
        # 没有新角色卡时才用旧 persona，避免两套描述叠在一起。
        if voice.get("sentence"):
            speech.append(_text(voice["sentence"]))
        if voice.get("under_pressure"):
            speech.append("压力下：" + _text(voice["under_pressure"]))
        if isinstance(voice.get("tics"), list) and voice["tics"]:
            speech.append("口头禅：" + "、".join(str(x) for x in voice["tics"][:6]))

    # 禁忌：禁用词 + 能力上限（都是「他不会做/说」的边界）
    taboo: list[str] = []
    forbid = _text(cvoice.get("禁用词"))
    if forbid:
        taboo.append("不说：" + forbid)
    caps = _text(ability.get("能力上限"))
    if caps:
        taboo.append("做不到：" + caps)
    old_taboo = _text(voice.get("taboo"))
    if old_taboo and not cvoice:
        taboo.append(old_taboo)

    # 关系：新卡 关系 dict 优先，旧 address 兜底
    relations = ""
    if crel:
        relations = "\n".join(f"{k}：{_text(v)}" for k, v in list(crel.items())[:15])
    elif isinstance(voice.get("address"), dict) and voice["address"]:
        relations = "\n".join(
            f"{name}：{how}" for name, how in list(voice["address"].items())[:15]
        )

    # 性格：角色卡没有「性格」字段，语气底色是最近的行为倾向，显式标来源
    personality = ""
    tone = _text(cvoice.get("语气底色"))
    if tone:
        personality = f"（据角色卡语气底色）{tone}"
    elif voice.get("under_pressure") or voice.get("sentence"):
        bits = []
        if voice.get("under_pressure"):
            bits.append("压力下：" + _text(voice["under_pressure"]))
        if voice.get("sentence"):
            bits.append("处事与说话：" + _text(voice["sentence"]))
        personality = "（据已有人设整理）" + "；".join(bits)

    # 备注：性别/年龄/量级
    notes: list[str] = []
    for label, key in (("性别", "性别"), ("年龄", "年龄")):
        value = _text(ident.get(key))
        if value:
            notes.append(f"{label}：{value}")
    if scale:
        bits = [f"{k}：{_text(v)}" for k, v in scale.items()
                if _text(v) and k in ("量级", "出手规模", "压制")]
        if bits:
            notes.append("量级：" + "；".join(bits))
    for label, key in (("首次出场", "首次出场章节"), ("来源", "来源")):
        if char.get(key):
            notes.append(f"{label}：{char[key]}")

    return {
        "identity": _clip("；".join(x for x in identity_bits if x)),
        "appearance": _clip(appearance),
        "personality": _clip(personality),
        "motivation": _clip(char.get("motivation")),
        "speech": _clip("；".join(speech)),
        "relations": _clip(relations),
        "taboo": _clip("；".join(taboo)),
        "notes": _clip("\n".join(notes)),
    }


def character_fields_of(char: dict) -> dict[str, str]:
    """公开别名：角色卡 → 设定卡八字段（路由用它算同步基线）。"""
    return _bible_character_fields(char)


def _kg_fields(card_kind: str, attrs: dict, mentions: int) -> dict[str, str]:
    """KG 实体 attrs → 卡片字段（尽力映射，映射不到就留空让作者补）。"""
    data = attrs if isinstance(attrs, dict) else {}

    def pick(*keys: str) -> str:
        for key in keys:
            value = data.get(key)
            if isinstance(value, (list, tuple)):
                text = "、".join(str(x).strip() for x in value if str(x).strip())
            else:
                text = str(value or "").strip()
            if text:
                return _clip(text)
        return ""

    origin_note = f"来自知识图谱（提及 {mentions} 次）"

    if card_kind == "character":
        return {
            "status": pick("current_goal"),
            "notes": "；".join(
                x for x in (pick("arc"), pick("secrets"), origin_note) if x
            ),
        }
    if card_kind == "faction":
        return {
            "faction_type": pick("type"),
            "leader": pick("leader"),
            "stance": pick("stance"),
            "goal": pick("current_goal", "arc"),
            "resources": pick("members"),
            "territory": pick("location", "parent"),
            "relations": pick("parent"),
            "notes": "；".join(
                x for x in (pick("description"), pick("secrets"), origin_note) if x
            ),
        }
    if card_kind == "scene":
        return {
            "scene_type": pick("type"),
            "location": pick("location", "description"),
            "time": pick("status"),
            "atmosphere": pick("stance"),
            "characters": pick("members"),
            "notes": "；".join(
                x for x in (pick("description"), pick("secrets"), origin_note) if x
            ),
        }
    if card_kind == "item":
        return {
            "category": pick("type"),
            "origin": pick("crafted_by"),
            "owner": pick("held_by"),
            "function": pick("description"),
            "limits": pick("status"),
            "notes": "；".join(
                x for x in (pick("system"), pick("grade", "tier"), origin_note) if x
            ),
        }
    return {
        "rule_type": pick("type"),
        "origin": pick("system"),
        "effect": pick("description"),
        "cost": pick("status"),
        "tiers": pick("tier", "grade"),
        "users": pick("held_by"),
        "notes": "；".join(
            x for x in (pick("arc"), pick("current_goal"), origin_note) if x
        ),
    }


def _merge_character_fields(
    current: dict[str, str],
    source: dict[str, str],
    snapshot: dict[str, str],
) -> tuple[dict[str, str], bool, bool]:
    """按快照智能合并一张人物卡的字段。

    规则：
    - `current` 为空且快照也为空 → 写入；
    - `current` 等于快照 → 写入（作者没改过）；
    - 否则视为作者手改，保留。

    返回 `(新字段, 是否有更新, 是否有字段被手改保留)`。
    """
    merged = dict(current)
    changed = False
    kept = False
    for key, value in source.items():
        if not value:
            continue
        current_text = str(merged.get(key) or "").strip()
        snapshot_text = str(snapshot.get(key) or "").strip()
        if (not current_text and not snapshot_text) or (
            current_text and current_text == snapshot_text
        ):
            if current_text != value:
                merged[key] = value
                changed = True
        else:
            kept = True
    return merged, changed, kept


def retire_story_card(db, project, name: str) -> dict:
    """角色从剧情圣经移除后处理对应设定卡。

    - 可证明未手改（有快照、八字段全等、无快照外非空字段、卡仍是启用态）→ 删除；
    - 其它情况（有手改 / 有额外字段 / 快照缺失 / 原本已停用）→ 停用并加备注。
    """
    target = str(name or "").strip().lower()
    if not target:
        return {"deleted": False, "disabled": False}
    card = (
        db.query(StoryCard)
        .filter(StoryCard.project_id == project.id, StoryCard.kind == "character")
        .all()
    )
    match = next(
        (c for c in card if str(c.title or "").strip().lower() == target), None
    )
    if match is None:
        return {"deleted": False, "disabled": False}

    snapshot = match.sync_snapshot if isinstance(match.sync_snapshot, dict) else None
    fields = dict(match.fields or {})
    untouched = False
    if snapshot:
        extras = [
            key for key, value in fields.items()
            if key not in snapshot and str(value or "").strip()
        ]
        untouched = not extras and all(
            str(fields.get(key) or "").strip() == str(value or "").strip()
            for key, value in snapshot.items()
        )

    if untouched and match.enabled:
        db.delete(match)
        return {"deleted": True, "disabled": False}

    note = str(fields.get("notes") or "").rstrip()
    marker = "角色已从剧情圣经移除"
    if marker not in note:
        fields["notes"] = f"{note}\n{marker}".strip()
    match.fields = fields
    match.enabled = False
    return {"deleted": False, "disabled": True}


def sync_from_project(db, project, *, previous_fields: dict[str, dict[str, str]] | None = None) -> dict:
    """把剧情圣经与知识图谱里已有的实体补成卡片。

    - 新卡：按 `(kind, 标题)` 去重后新增，剧情圣经人物卡同时写入同步快照；
    - 已有角色卡：按快照「智能合并」——作者手改过的字段保留，其余随角色卡更新；
    - 首次接入（无快照）：有 `previous_fields` 时用旧 source 当基线合并，
      否则只写快照、本轮不改字段（避免一次性覆盖手改）；
    - 每类有导入上限，保证整块提示词不被截断；
    - 写入 `story_cards_seeded_at`，调用方提交后即视为「已同步过一次」。
    """
    cards = (
        db.query(StoryCard)
        .filter(StoryCard.project_id == project.id)
        .all()
    )
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    bible_characters: list[tuple[str, dict[str, str]]] = []
    bible_by_name: dict[str, dict[str, str]] = {}
    for char in bible.get("characters") or []:
        if not isinstance(char, dict):
            continue
        name = str(char.get("name") or "").strip()
        if not name:
            continue
        fields = _bible_character_fields(char)
        bible_characters.append((name, fields))
        bible_by_name[name.lower()] = fields

    # 已存在的角色卡：智能合并（手改字段保留）。
    previous = {
        str(key).strip().lower(): value
        for key, value in (previous_fields or {}).items()
        if isinstance(value, dict)
    }
    updated = 0
    kept = 0
    for card in cards:
        if card.kind != "character":
            continue
        title_key = str(card.title or "").strip().lower()
        source = bible_by_name.get(title_key)
        if not source:
            continue
        current = dict(card.fields or {})
        snapshot = card.sync_snapshot if isinstance(card.sync_snapshot, dict) else None
        if snapshot is None:
            baseline = previous.get(title_key)
            if not baseline:
                # 首次接入且没有旧 source 可对照：沿用旧的「只补空」语义，
                # 再把 source 记为基线。这样空字段能补上，已有手写内容不动。
                merged = dict(current)
                for key, value in source.items():
                    if value and not str(merged.get(key) or "").strip():
                        merged[key] = value
                if merged != current:
                    card.fields = merged
                    updated += 1
                card.sync_snapshot = dict(source)
                continue
            snapshot = baseline
        merged, changed, hand_edited = _merge_character_fields(
            current, source, snapshot
        )
        card.fields = merged
        card.sync_snapshot = dict(source)
        if changed:
            updated += 1
        if hand_edited:
            kept += 1

    existing = {
        (card.kind, str(card.title or "").strip().lower()) for card in cards
    }
    next_order: dict[str, int] = {}
    for card in cards:
        next_order[card.kind] = max(
            next_order.get(card.kind, 0), (card.sort_order or 0) + 1
        )

    created: list[StoryCard] = []
    # 上限按「该类型现有卡片总数」计，而不是只计本次新建——否则每点一次
    # 手动同步，就又能再塞进一批，直到把提示词预算撑爆。
    created_per_kind: dict[str, int] = {}
    for card in cards:
        created_per_kind[card.kind] = created_per_kind.get(card.kind, 0) + 1
    skipped = 0

    def add(
        kind: str,
        title: Any,
        fields: dict[str, str],
        snapshot: dict[str, str] | None = None,
    ) -> None:
        nonlocal skipped
        title_text = str(title or "").strip()
        if not title_text or title_text in KG_NAME_STOPLIST:
            skipped += 1
            return
        key = (kind, title_text.lower())
        if key in existing:
            skipped += 1
            return
        if created_per_kind.get(kind, 0) >= MAX_IMPORT_PER_KIND.get(kind, 10):
            skipped += 1
            return
        try:
            kind_value, clean_title, clean_fields = normalize_card_input(
                kind, title_text, fields
            )
        except ValueError:
            skipped += 1
            return
        existing.add(key)
        card = StoryCard(
            project_id=project.id,
            kind=kind_value,
            title=clean_title,
            fields=clean_fields,
            # 剧情圣经人物卡写入同步基线；KG 导入卡没有基线（只能手动补空）。
            sync_snapshot=dict(snapshot) if snapshot else None,
            enabled=True,
            sort_order=next_order.get(kind_value, 0),
        )
        next_order[kind_value] = card.sort_order + 1
        created_per_kind[kind_value] = created_per_kind.get(kind_value, 0) + 1
        db.add(card)
        created.append(card)

    # 1) 剧情圣经（作者构思，优先）
    for name, fields in bible_characters:
        add("character", name, fields, snapshot=fields)
    for name in bible.get("derived_item_names") or []:
        add("item", name, {
            "notes": "由剧情圣经推导的道具名；请补充外观、功能与限制。",
        })

    # 2) 知识图谱（当前书口径；每类按提及次数取前 N）
    from app.kg.book_scope import prompt_scope_book_ids, scope_sql_values

    scope = scope_sql_values(prompt_scope_book_ids(project))
    for kg_kind, card_kind in KG_KIND_MAP.items():
        # 粗取比上限更多的行：前面可能有与剧情圣经重名的，需要跳过。
        limit = MAX_IMPORT_PER_KIND[card_kind] * 4
        rows = (
            db.query(KgEntity)
            .filter(
                KgEntity.project_id == project.id,
                KgEntity.kind == kg_kind,
                func.coalesce(KgEntity.novel_id, -1).in_(scope),
            )
            .order_by(KgEntity.mention_count.desc())
            .limit(limit)
            .all()
        )
        for row in rows:
            add(
                card_kind,
                row.canonical_name,
                _kg_fields(card_kind, row.attrs or {}, row.mention_count or 0),
            )

    project.story_cards_seeded_at = datetime.now(timezone.utc)
    db.flush()
    return {
        "created": len(created),
        "skipped": skipped,
        # `filled` 保留旧键（调用方兼容），语义升级为「本次有字段被更新的卡数」。
        "filled": updated,
        "updated": updated,
        "kept": kept,
    }
