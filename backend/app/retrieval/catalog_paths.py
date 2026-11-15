"""五级目录的路径词汇与路径拼装（设计稿 §2.2–§2.5、§5.12.1）。

`catalog_config.py` 负责「可调参数」（场景词表、先验权重、骨架阈值）；
本模块负责「结构表」：L1 资源族、L2 场景域、L3 节拍、L4 技法。
两处都是人工核对入口，但职责不同：改词表去 config，改层级去这里。

L4 的 8 个技法名与设计稿 §2.2 一致。设计稿 §5.12.1 的骨架样例里出现过
「拟声 / 标点 / 白描 / 留白 / 数字 / 虚词」等更细的标签，那些不在 8 个
L4 内；本轮**不擅自扩表**，无 8 类信号的规则落 `未细分`，由上层决定要不要
新增第 9–N 类（见交付报告里的「设计稿冲突」清单）。

路径拼装口径（2026-11-13 裁决）：
- 有场景：`style/<场景>/<技法>`，structural 中间再插 `<节拍>`；
- 无场景：退 `style/<技法>`（L4 仍在第 2 段，但**它是 L4，不是 L2**）；
- L4 在 structural / technique / global 三种形状下都挂；
- 取 L2 必须按层级词表认领（`classify_path`），不能按字符串第 2 段硬取。
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from app.retrieval.catalog_config import SHAPE_BY_DIM

# ---------------------------------------------------------------- L1 资源族
L1_STYLE = "style"
L1_VOICE = "voice"
L1_WORLD = "world"
L1_TIMELINE = "timeline"
L1_METRIC = "metric"

L1_ORDER: tuple[str, ...] = (
    L1_STYLE, L1_VOICE, L1_WORLD, L1_TIMELINE, L1_METRIC,
)
L1_LABELS: dict[str, str] = {
    L1_STYLE: "文风规则",
    L1_VOICE: "角色声音",
    L1_WORLD: "世界观",
    L1_TIMELINE: "剧情时间线",
    L1_METRIC: "量化指标",
}

#: 蒸馏维度 → L1 资源族。表里没有的维度默认进 style。
DIM_TO_L1: dict[str, str] = {
    "角色声音词典": L1_VOICE,
    "世界观呈现": L1_WORLD,
    "文笔量化审计": L1_METRIC,
}

# ---------------------------------------------------------------- L2 场景域
#: 与 `catalog_config.SCENE_LEXICON` 的 key 一一对应；这里只给中文短名。
L2_LABELS: dict[str, str] = {
    "combat": "战斗对峙",
    "daily": "日常起居",
    "groupchat": "群聊社交",
    "trade": "交易买卖",
    "cultivate": "修炼突破",
    "travel": "赶路移动",
    "interrogate": "审讯逼问",
    "reveal": "揭示真相",
    "intimacy": "亲密情感",
    "farewell": "离别送行",
    "banquet": "宴会聚餐",
    "misunderstand": "误会冲突",
    "rescue": "救援脱困",
    "night": "夜谈独处",
}

# ---------------------------------------------------------------- L3 节拍
L3_ORDER: tuple[str, ...] = (
    "setup", "build", "climax", "aftermath", "hook", "transition",
)
L3_LABELS: dict[str, str] = {
    "setup": "建置",
    "build": "升温",
    "climax": "爆发",
    "aftermath": "余波",
    "hook": "新钩子",
    "transition": "过渡",
}

#: 档案里的节拍前缀 → L3。实测 `节拍序列` 用的是「铺垫」而不是「建置」，
#: 这是同一档的同义写法；「建置」保留以兼容新档案。
#: 不在表里的前缀（反转/加压/对抗/收束/升级/顶点/高潮）**不猜**，落「未分节拍」。
L3_ALIASES: dict[str, str] = {
    "铺垫": "setup",
    "建置": "setup",
    "升温": "build",
    "爆发": "climax",
    "余波": "aftermath",
    "新钩子": "hook",
    "钩子": "hook",
    "过渡": "transition",
}
_BEAT_PREFIX_RE = re.compile(
    r"^\s*(铺垫|建置|升温|爆发|余波|新钩子|钩子|过渡)\s*[：:]"
)

# ---------------------------------------------------------------- L4 技法
#: 2026-11-13 裁决：原 8 类保持不动，新增 7 类只做「原 8 类无命中」时的兜底，
#: 保证已分配的 21,163 条不被重排。总 15 类。
L4_LEGACY_ORDER: tuple[str, ...] = (
    "dialogue", "action", "psych", "environment",
    "pacing", "pov", "infocontrol", "rhetoric",
)
L4_NEW_ORDER: tuple[str, ...] = (
    "subtext", "conflict", "tension", "portrayal",
    "contrast", "syntax", "callback",
)
L4_ORDER: tuple[str, ...] = L4_LEGACY_ORDER + L4_NEW_ORDER
L4_LABELS: dict[str, str] = {
    "dialogue": "对白",
    "action": "动作",
    "psych": "心理",
    "environment": "环境",
    "pacing": "节奏",
    "pov": "视角",
    "infocontrol": "信息控制",
    "rhetoric": "修辞",
    "subtext": "潜台词",
    "conflict": "冲突",
    "tension": "张力",
    "portrayal": "刻画",
    "contrast": "反差",
    "syntax": "句式",
    "callback": "回收",
}

#: 规则正文里的「典型信号」（设计稿 §2.5 只给了描述，没有给词表）。
#: 这里是最小可用词表：只收 ≥2 字、能独立指向某一技法的词。
#: 词表会直接影响 L4 分布，调整前先看交付报告里的 L4 分布与 `未细分` 占比。
L4_LEXICON: dict[str, tuple[str, ...]] = {
    "dialogue": (
        "对白", "对话", "台词", "引号", "称谓", "敬语", "语气", "口头禅",
        "抢白", "反问", "说话", "开口", "回应", "称呼",
    ),
    "action": (
        "动作", "招式", "打斗", "出手", "拳脚", "刀剑", "厮杀", "连招",
        "拟声", "动词", "身法", "扑", "挥", "踢",
    ),
    "psych": (
        "心理", "内心", "情绪", "念头", "犹豫", "吐槽", "感受", "紧张",
        "恐惧", "心动", "心思", "心声",
    ),
    "environment": (
        "环境", "天气", "光线", "景物", "器物", "空间", "房间", "街道",
        "氛围", "场景", "五感", "气味", "声音",
    ),
    "pacing": (
        "节奏", "长短句", "停顿", "快慢", "压缩", "段落", "切分", "跳跃",
        "一句成段", "短句", "长句", "推进速度",
    ),
    "pov": (
        "视角", "第三人称", "限知", "旁观", "切换", "镜头", "主观", "全知",
        "视点",
    ),
    "infocontrol": (
        "伏笔", "悬念", "留白", "延迟", "揭示", "误导", "暗示", "反转",
        "信息控制", "铺垫", "埋线", "抖包袱",
    ),
    "rhetoric": (
        "比喻", "夸张", "排比", "反讽", "拟人", "修辞", "象征", "对比",
        "重复", "通感", "借代", "双关",
    ),
    # ---- 以下 7 类为 2026-11-13 新增，只在原 8 类无命中时兜底 ----
    "subtext": (
        "潜台词", "言外之意", "嘴上", "实际是", "真实意图", "掩饰", "反话",
        "正话反说", "话反说", "转移话题", "不正面", "不直接说", "不直说",
        "口是心非", "嘴硬", "装作", "假装", "表面", "包装成", "关心藏在",
        "藏着", "试探", "回避", "绕开", "不接", "明贬实褒", "明褒实贬",
        "命令式", "命令口吻", "表达担心", "担心", "保护", "安抚", "护短",
        "叮嘱", "搪塞", "敷衍", "应承", "改口", "避重就轻", "真实目的",
        "真实动机", "隐去", "遮掩", "掩盖", "不答应", "不拒绝", "轻轻带过",
        "换方案", "更正", "否认", "自我安慰", "说服", "逞强", "赌气", "嗔怪",
    ),
    "conflict": (
        "冲突", "加码", "滚雪球", "威胁", "危机", "信息差", "误判", "误会",
        "赌约", "底牌", "压力", "追逐", "对峙", "敌对", "代价", "引爆",
        "激化", "多方", "势力", "筹码", "陷阱", "报复", "挑衅", "拉仇恨",
        "反杀", "袭击", "追捕", "围攻", "交锋", "交手", "决战", "决斗",
        "伏击", "围杀", "追杀", "宣战", "翻脸", "撕破脸", "大打出手",
        "混战", "厮杀", "争夺", "逼宫", "施压", "反制", "反击", "算计",
        "合围", "封锁",
    ),
    "tension": (
        "不点破", "先压", "垫高", "压迫", "不安", "危机感", "悬念", "埋雷",
        "反常", "未知", "前兆", "预兆", "蓄势", "紧迫", "紧张", "绷紧",
        "屏息", "冷汗", "不祥", "预感", "悬着", "一触即发", "剑拔弩张",
        "暗流", "山雨欲来", "倒计时", "危险", "凶险", "诡异", "吊胃口",
        "卖关子", "欲言又止", "悬而未决", "压迫感", "危机四伏", "心跳",
        "窒息", "僵住", "发麻", "毛骨悚然", "头皮发麻", "死寂",
        "安静得可怕", "压迫感十足",
    ),
    "portrayal": (
        "外貌", "长相", "容貌", "服饰", "衣着", "打扮", "身形", "体态",
        "面容", "五官", "头发", "装束", "姿态", "标签", "侧写", "侧面",
        "群体反应", "围观", "旁人", "表情", "身体部位", "身体反应", "生理",
        "形态", "道具", "物件", "标志物", "辨识度", "人物形象", "角色形象",
        "面部", "手部", "眼神", "眸子", "酒窝", "面纱", "自嘲", "旁白评论",
        "观察者", "气质", "错位", "瞳孔", "低吼", "感叹词", "集体反应",
        "异常外观", "笑容", "牙齿", "神态", "语塞", "无奈",
    ),
    "contrast": (
        "反差", "反衬", "对比", "错位", "荒诞", "冷幽默", "喜剧", "尴尬",
        "乐极生悲", "先扬后抑", "先抑后扬", "预期落空", "落差", "反差萌",
        "强烈反差", "形成对比", "制造反差", "视觉反差", "情绪反差", "语气反差",
    ),
    "syntax": (
        "独立成段", "短段", "长段", "分段", "分节", "分行", "空行", "方括号",
        "系统提示", "聊天消息", "省略号", "问号", "感叹号", "破折号", "断句",
        "短问句", "长句", "短句", "一句成段", "排比", "对称", "对仗", "句式",
        "符号", "标点", "独立成句", "分点", "短答", "短问答", "时间词",
        "跳切", "过渡", "转场", "单句成段", "短过渡", "路标词", "独立段",
        "问答链", "问答式", "短回合",
    ),
    "callback": (
        "回收", "闭环", "呼应", "兑现", "后文", "前文", "预埋", "埋设",
        "倒叙", "线索", "伏线", "补上", "再次现身", "短线回收",
    ),
}


def shape_for(dim: str, l1: str | None = None) -> str | None:
    """维度 → 形状。world / voice / metric 不是三形状，返回 None。"""
    shape = SHAPE_BY_DIM.get(dim)
    if shape:
        return shape
    if (l1 or l1_for_dim(dim)) == L1_STYLE:
        return "technique"
    return None


def l1_for_dim(dim: str) -> str:
    return DIM_TO_L1.get(dim, L1_STYLE)


def strip_beat_prefix(text: str) -> str:
    """去掉「铺垫：」「爆发：」这类节拍前缀，避免前缀被当成技法信号。"""
    return _BEAT_PREFIX_RE.sub("", text or "", count=1).strip()


def beat_key(text: str) -> str | None:
    """规则正文的节拍前缀 → L3 key；认不出返回 None。"""
    m = _BEAT_PREFIX_RE.match(text or "")
    if not m:
        return None
    return L3_ALIASES.get(m.group(1))


def assign_l4(text: str, lexicon: dict[str, tuple[str, ...]] | None = None,
              seed_lexicon: dict[str, tuple[str, ...]] | None = None,
              seed_weight: int = 2
              ) -> tuple[str | None, list[str], dict[str, int]]:
    """按词表给一条规则挑主技法 + 其余技法。

    Args:
        lexicon: 词表；默认用内置 `L4_LEXICON`。建卡时传入按该书语料推导的
            词表（见 `retrieval/l4_lexicon.py`），匹配口径保持不变。
        seed_lexicon: 该书 live 的旧词（`l4_lexicon` 报告里的 `seed_terms`）。
            旧词计 `seed_weight` 分、派生新词计 1 分，避免新增词把原本正确
            的主技法抢走；不传则所有词等权。
        seed_weight: 旧词权重，默认 2（权重口径见 `l4_lexicon.DEFAULT_SEED_WEIGHT`）。

    Returns:
        (primary, tags, scores)。primary 为 None 表示「未细分」。
        主技法取命中词数最多者；并列按 `L4_ORDER` 的固定顺序，保证可复现。

    2026-11-13 起新类只做兜底：原 8 类只要有命中，就只在原 8 类里排序；
    原 8 类全为 0 时才用新增 7 类，避免重排已分配的 21,163 条。
    """
    lex = lexicon or L4_LEXICON
    seed = seed_lexicon or {}
    body = strip_beat_prefix(text)
    scores: dict[str, int] = {}
    for key in L4_ORDER:
        seed_set = set(seed.get(key, ()))
        n = 0
        for word in lex.get(key, ()):
            n += body.count(word) * (seed_weight if word in seed_set else 1)
        if n:
            scores[key] = n
    if not scores:
        return None, [], {}
    legacy = {k: v for k, v in scores.items() if k in L4_LEGACY_ORDER}
    pool = legacy or scores
    ranked = sorted(pool.items(), key=lambda kv: (-kv[1], L4_ORDER.index(kv[0])))
    primary = ranked[0][0]
    tags = [k for k, _ in ranked[1:4]]
    return primary, tags, scores


def classify_path(path: str) -> dict[str, str | None]:
    """path 字符串 → 结构化层级。

    不按段位置硬取：`style/dialogue` 的第 2 段是 L4 技法，不是 L2 场景。
    每个 token 只在对应的层级词表里认领；认不出就留空。
    """
    out: dict[str, str | None] = {"l1": None, "l2": None, "l3": None, "l4": None}
    for seg in (path or "").split("/"):
        if not seg:
            continue
        if seg in L1_ORDER:
            out["l1"] = seg
        elif seg in L2_LABELS:
            out["l2"] = seg
        elif seg in L3_ORDER:
            out["l3"] = seg
        elif seg in L4_ORDER:
            out["l4"] = seg
    return out


def join_path(*parts: str | None) -> str:
    return "/".join(p for p in parts if p)


def path_depth(path: str) -> int:
    return len([p for p in (path or "").split("/") if p])


def path_labels(path: str) -> list[str]:
    """路径 → 中文标签（给 browse 的 children 用）。"""
    out: list[str] = []
    for i, seg in enumerate([p for p in (path or "").split("/") if p]):
        if i == 0:
            out.append(L1_LABELS.get(seg, seg))
        elif i == 1:
            out.append(L2_LABELS.get(seg, seg))
        elif i == 2:
            out.append(L3_LABELS.get(seg, seg))
        else:
            out.append(L4_LABELS.get(seg, seg))
    return out


def label_for(level: int, key: str) -> str:
    if level <= 1:
        return L1_LABELS.get(key, key)
    if level == 2:
        return L2_LABELS.get(key, key)
    if level == 3:
        return L3_LABELS.get(key, key)
    return L4_LABELS.get(key, key)


def unassigned_key(level: int) -> str:
    return "_unassigned"


def unassigned_label(level: int) -> str:
    return ("未分配", "未挂场景", "未分节拍", "未细分")[max(0, min(3, level - 1))]


# ---------------------------------------------------------------- 路径归一化
#: 模型从骨架/中文标签里抄来的路径分隔符。实测出现过 `技法/战斗对峙`、
#: `战斗对峙`、`style、combat` 三种写法，统一按这些分隔符拆段。
_PATH_SPLIT_RE = re.compile(r"[/\\、，,;；>·>\s]+")
_LABEL_FIELDS = {1: "l1_label", 2: "l2_label", 3: "l3_label", 4: "l4_label"}


@dataclass(frozen=True)
class PathResolution:
    """一次 path 归一化的结果。`changed=True` 表示输入不是规范键。"""

    input: str
    path: str
    tokens: tuple[tuple[int, str], ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return (self.input or "").strip().strip("/") != self.path

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_input": self.input,
            "path_resolved": self.path,
            "path_changed": self.changed,
            "path_unknown": list(self.unknown),
        }


class PathVocab:
    """key ↔ 中文标签 ↔ 规范路径前缀。

    映射**从卡表数据构建**，不在代码里写死任何一本书的分类名：

    - 每级有哪些 key：取 `cards` 里实际出现的 `l1…l4` 值；
    - 每个 key 的父路径：取该 key 在卡片 `path` 中最常见的规范前缀；
    - 标签：优先读卡片的 `l1_label…l4_label`（换一套分类词表时随卡表带过来），
      没有才退回本模块的通用词表；都查不到就用 key 本身。

    换一本书只要重新建卡，`PathVocab` 自动跟着变；本模块的通用词表只是
    「没有标签字段时的兜底」，不是某本书的分类表。
    """

    def __init__(self, cards: Iterable[dict]) -> None:
        self.keys: dict[int, set[str]] = {1: set(), 2: set(), 3: set(), 4: set()}
        self.labels: dict[int, dict[str, str]] = {1: {}, 2: {}, 3: {}, 4: {}}
        parent_counts: dict[tuple[int, str], Counter] = {}
        for card in cards:
            for level, field in enumerate(("l1", "l2", "l3", "l4"), 1):
                key = card.get(field)
                if not key:
                    continue
                key = str(key)
                self.keys[level].add(key)
                label = card.get(_LABEL_FIELDS[level])
                if label and key not in self.labels[level]:
                    self.labels[level][key] = str(label)
            # 逐段记录「这个节点在规范路径里的父前缀」。
            parent = ""
            for seg in [p for p in (card.get("path") or "").split("/") if p]:
                level = self.level_of(seg)
                if level is None:
                    continue
                parent_counts.setdefault((level, seg), Counter())[parent] += 1
                parent = f"{parent}/{seg}" if parent else seg

        self.parents: dict[tuple[int, str], str] = {}
        for node, counter in parent_counts.items():
            # 并列时取更短的前缀（更靠近根），保证结果稳定且不会多出一层。
            self.parents[node] = sorted(
                counter.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0])
            )[0][0]
        self.alias: dict[str, tuple[int, str]] = {}
        for level in (1, 2, 3, 4):
            for key in self.keys[level]:
                self.alias.setdefault(key, (level, key))
                self.alias.setdefault(
                    self.label(level, key), (level, key)
                )
        # 档案里的节拍同义写法（铺垫/建置…）仍然按 L3 别名认领。
        for label, key in L3_ALIASES.items():
            if key in self.keys[3]:
                self.alias.setdefault(label, (3, key))

    # ----- 查询 -----
    def label(self, level: int, key: str) -> str:
        table = (L1_LABELS, L2_LABELS, L3_LABELS, L4_LABELS)[level - 1]
        return self.labels.get(level, {}).get(key) or table.get(key, key)

    def level_of(self, token: str) -> int | None:
        for level in (1, 2, 3, 4):
            if token in self.keys[level]:
                return level
        return None

    def node_path(self, level: int, key: str) -> str:
        parent = self.parents.get((level, key), "")
        return f"{parent}/{key}" if parent else key

    def keys_at(self, level: int, *, limit: int = 12) -> list[dict[str, str]]:
        """给调用方的「合法键速查」，按固定层级顺序，尾部补卡表里多出来的 key。"""
        order: tuple[str, ...] = {
            1: L1_ORDER,
            2: tuple(L2_LABELS),
            3: L3_ORDER,
            4: L4_ORDER,
        }[level]
        ordered = [k for k in order if k in self.keys[level]]
        ordered += sorted(self.keys[level] - set(ordered))
        return [
            {"key": k, "label": self.label(level, k)}
            for k in ordered[:limit]
        ]

    # ----- 归一化 -----
    def resolve(self, raw: str) -> PathResolution:
        """把中文标签/混写路径归一成卡表里的规范路径。

        认不出的段（如「技法」这种不是任何一级 key 的词）直接丢弃并记在
        `unknown` 里；能认出的段按父子关系重新拼路径，缺 L1 时自动补
        （`combat` → `style/combat`，`dialogue` → `style/dialogue`）。
        """
        text = (raw or "").strip()
        tokens = [t for t in _PATH_SPLIT_RE.split(text) if t]
        resolved: list[tuple[int, str]] = []
        unknown: list[str] = []
        for token in tokens:
            hit = self.alias.get(token) or self.alias.get(token.lower())
            if hit is None:
                # 「战斗对峙类」「场景域」这类口语后缀，去掉后再试一次。
                trimmed = token.rstrip("类族域层")
                hit = self.alias.get(trimmed) if trimmed else None
            if hit is None:
                unknown.append(token)
            elif hit not in resolved:
                resolved.append(hit)

        path = ""
        for level, key in resolved:
            if level == 1:
                path = key
                continue
            parent = self.parents.get((level, key), "")
            if not path:
                path = parent
            elif parent and path != parent and not path.startswith(parent + "/"):
                # 模型给的两个段不在同一条分支上：以更深一层的父路径为准。
                path = parent
            path = f"{path}/{key}" if path else key
        return PathResolution(
            input=text, path=path.strip("/"),
            tokens=tuple(resolved), unknown=tuple(unknown),
        )

    def path_for_level(self, path: str, level: int) -> str:
        """`browse(level, path)` 的父路径：模型可能把目标层自己当成了父路径。"""
        if level <= 1:
            return ""
        segs = [p for p in (path or "").split("/") if p]
        return "/".join(segs[: level - 1])


_VOCAB_CACHE: dict[tuple[Any, str], PathVocab] = {}


def vocab_for(catalog: Any) -> PathVocab:
    """按 `distill_id + catalog_version` 缓存；卡表重建后自动失效。"""
    cache_key = (getattr(catalog, "distill_id", None),
                 str(getattr(catalog, "catalog_version", "")))
    cached = _VOCAB_CACHE.get(cache_key)
    if cached is None:
        cached = PathVocab(catalog.cards)
        _VOCAB_CACHE[cache_key] = cached
    return cached


@dataclass(frozen=True)
class PathParts:
    l1: str
    l2: str | None = None
    l3: str | None = None
    l4: str | None = None

    @property
    def path(self) -> str:
        return join_path(self.l1, self.l2, self.l3, self.l4)

    @property
    def depth(self) -> int:
        return path_depth(self.path)
