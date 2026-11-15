"""五级目录的配置面：路径形状、L2 场景词表、检索先验、证据门槛。

## 为什么单独一个模块

这些值会被人工反复核对（尤其是场景词表），散落在脚本里没法审。集中在这里，
改词表 / 改先验权重只动本文件；临时统计脚本与将来的建卡代码都从这里 import。

## 整词匹配

中文没有空格，用「最长匹配」代替分词：在文本每个位置按词长从长到短匹配，
命中后整段跳过，避免 `战斗` 在 `战斗机` 里被重复计数。`SCENE_COMPOUND_BLOCKLIST`
里的词（如 `战车`、`挑战`）按更长的匹配优先命中但不计入任何场景，
因此不会再出现「战车 → combat」这种单字误伤。

**单字场景词已全部移除**（战/法/杀/爆/轰/刀/剑/拳），它们只会在 `战斗`
`战车` `爆炸` 里乱命中。新增词必须 ≥2 字。
"""
from __future__ import annotations

from collections import Counter

# ---------------------------------------------------------------- 路径形状
#: 维度 → 形状。封闭映射，不允许运行时改判。
SHAPE_BY_DIM: dict[str, str] = {
    "节拍序列": "structural",
    "剧情编排模式": "technique",
    "伏笔与悬念设计": "technique",
    "对白风格": "technique",
    "人物描写手法": "technique",
    "场景构建": "technique",
    "情绪表达": "technique",
    "高频场景模板": "technique",
    "段落编排": "global",
    "节奏控制": "global",
    "潜台词模式": "global",
}

#: 维度不在表里时的兜底：style 族默认 technique，其他 L1 族按族名走。
DEFAULT_SHAPE_BY_L1: dict[str, str] = {"style": "technique"}

# ---------------------------------------------------------------- L2 场景词表
#: 14 个场景域 → 场景词（全部 ≥2 字）。人工核对入口。
SCENE_LEXICON: dict[str, tuple[str, ...]] = {
    "combat": ("战斗", "交手", "出手", "攻击", "招式", "打斗", "对峙", "拳脚",
               "剑法", "刀法", "厮杀", "围攻", "交锋", "血战", "战场"),
    "daily": ("日常", "吃饭", "做饭", "厨房", "睡觉", "洗漱", "家务", "闲聊",
              "日子", "早餐", "晚饭", "起居"),
    "groupchat": ("群聊", "群里", "聊天", "消息", "语音", "论坛", "帖子", "弹幕",
                  "群友", "聊天记录"),
    "trade": ("交易", "拍卖", "买卖", "价钱", "谈价", "交换", "灵石", "付账",
              "店铺", "商贩"),
    "cultivate": ("修炼", "打坐", "疗伤", "突破", "灵气", "丹药", "闭关",
                  "经脉", "运功"),
    "travel": ("赶路", "飞行", "御剑", "路上", "马车", "前往", "赶赴", "旅途",
               "出发"),
    "interrogate": ("审讯", "盘问", "质问", "逼问", "拷问", "审问", "追问"),
    "reveal": ("揭示", "真相", "揭穿", "揭露", "爆料", "公布", "识破", "秘密"),
    "intimacy": ("亲密", "拥抱", "亲吻", "牵手", "告白", "心动", "暧昧", "情意"),
    "farewell": ("离别", "告别", "送别", "道别", "再见", "目送"),
    "banquet": ("宴席", "宴会", "聚餐", "酒席", "饭局", "宾客", "敬酒"),
    "misunderstand": ("误会", "误解", "冲突", "争执", "吵架", "矛盾", "翻脸",
                      "争吵"),
    "rescue": ("救援", "营救", "脱困", "逃生", "解救", "救出", "相救"),
    "night": ("夜谈", "深夜", "独处", "夜晚", "月光", "夜里", "灯下"),
}

#: 不是场景、但包含场景词的复合词。最长匹配优先命中它们，但不计场景。
SCENE_COMPOUND_BLOCKLIST: tuple[str, ...] = (
    "战斗机", "战车", "挑战", "挑战赛", "战术", "战斗力", "战斗服", "战友",
    "战利品", "战况",
)

#: 场景判定最少命中词数。低于它视为「判不出」。
SCENE_MIN_SCORE = 2

# ---------------------------------------------------------------- 检索先验
#: 是否启用 query 场景先验（对照实验时置 False）。
SCENE_PRIOR_ENABLED = True
#: 命中 query 场景的卡片，在余弦相似度上加这么多分。
#:
#: 2026-11-12 按全量形状比例重抽 500 条后的扫描（Q1 combat / Q2 groupchat+daily）：
#:   0.00 → 50% / 80%；0.10 → 80% / 100%；0.20 → 90% / 100%；0.35 → 100% / 100%。
#: 取 0.20：既把通用词压下去（0 权重时 Q1 只有 50%），又不把结果锁死成单一场景
#: （0.35 时 Q1 top10 全是 combat，分散度=1）。Q2 对权重不敏感，无副作用。
SCENE_PRIOR_WEIGHT = 0.20

# ---------------------------------------------------------------- 锚点 / 证据
#: 证据句至少这么长才走章内精确匹配；短于它走 n-gram 模糊定位。
ANCHOR_EXACT_MIN_CHARS = 15
#: 新蒸馏要求证据句至少这么长；短于它不作为证据（写进抽取提示词）。
EVIDENCE_MIN_CHARS = 20

# ---------------------------------------------------------------- 骨架
#: 骨架只列条数 ≥ 这个值的路径；更少的并到上级只留总数。
SKELETON_MIN_PATH_COUNT = 5
#: 骨架字数上限（含标题与截断提示）。
SKELETON_CHAR_CAP = 2200


class SceneMatcher:
    """最长匹配的场景词扫描器。零依赖、可复现。"""

    def __init__(self, lexicon: dict[str, tuple[str, ...]],
                 blocklist: tuple[str, ...] = ()) -> None:
        # 首字 → [(词, 场景 or None)]，按词长降序；blocklist 的词场景为 None。
        by_first: dict[str, list[tuple[str, str | None]]] = {}
        for scene, words in lexicon.items():
            for word in words:
                if len(word) < 2:
                    raise ValueError(f"场景词必须 ≥2 字：{word!r}")
                by_first.setdefault(word[0], []).append((word, scene))
        for word in blocklist:
            by_first.setdefault(word[0], []).append((word, None))
        self._by_first = {
            ch: tuple(sorted(items, key=lambda x: len(x[0]), reverse=True))
            for ch, items in by_first.items()
        }

    def scores(self, text: str) -> Counter[str]:
        """返回每个场景的命中词数。整词最长匹配，命中后跳过该词。"""
        out: Counter[str] = Counter()
        i, n = 0, len(text or "")
        while i < n:
            for word, scene in self._by_first.get(text[i], ()):
                if text.startswith(word, i):
                    if scene:
                        out[scene] += 1
                    i += len(word)
                    break
            else:
                i += 1
        return out

    def best(self, text: str, min_score: int = SCENE_MIN_SCORE) -> str | None:
        scores = self.scores(text)
        if not scores:
            return None
        scene, n = scores.most_common(1)[0]
        return scene if n >= min_score else None

    def query_scenes(self, query: str, min_score: int = 1) -> list[str]:
        """query 里命中的场景（先验用；阈值 1 即可）。"""
        return [s for s, n in self.scores(query).most_common() if n >= min_score]


SCENE_MATCHER = SceneMatcher(SCENE_LEXICON, SCENE_COMPOUND_BLOCKLIST)
