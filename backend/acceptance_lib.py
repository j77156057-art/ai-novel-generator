"""验收跑共享库：指标 + 种子 + 人类基线（离线跑与真模型跑共用）。

为什么单独抽一个库
------------------------------------------------------------------------------
两轮验收必须用**同一套指标、同一份阈值**，否则两轮的数不能比：

- `verify_acceptance.py`      —— 离线确定性 provider（无模型、无网络）
- `verify_acceptance_real.py`  —— 真模型端到端（本次新增）

阈值不是拍的：先在**真实语料**上量出人类散文的分布，阈值取该分布的分位数
（重复率 p90 / 比喻密度 p90 / 句长 CV p10）。基线语料从环境变量
`ACCEPTANCE_CORPUS` 指定，默认取仓库内 `backend/novel_dev.db`（开发库）。

⚠️ 基线语料一旦换了，**阈值必须重算** —— 不能沿用别的语料上的分位数
（人类基线只有 3 本网文这件事本身就是已知局限，见 docs/acceptance-metrics.md §8）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


# --------------------------------------------------------------------------- #
# 指标实现（纯函数）
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """去掉空白与换行：中文正文统计不该被排版空行影响。"""
    return _WS.sub("", text or "")


def grams(text: str, n: int = 3) -> set[str]:
    t = _clean(text)
    if len(t) < n:
        return set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 比喻是 AI 腔最稳的硬特征之一：真人写得再花，也不会每百字挂一个「像…」
_METAPHOR = re.compile(r"(像[^，。！？；：]{1,12}|如同|宛如|仿佛|彷佛)")


def metaphor_per100(text: str) -> float:
    t = _clean(text)
    if not t:
        return 0.0
    return 100.0 * len(_METAPHOR.findall(t)) / len(t)


_SENT_SPLIT = re.compile(r"[。！？…]+")


def sent_cv(text: str) -> float | None:
    """句长变异系数：AI 腔句子长度往往过于整齐，真人散文起伏更大。

    方向是「越大越像人」——所以阈值是**下界**（不得低于人类 p10）。
    句子太少时返回 None（不参与判定），而不是硬算一个噪音值。
    """
    sents = [s for s in _SENT_SPLIT.split(_clean(text)) if len(s) >= 4]
    if len(sents) < 5:
        return None
    m = statistics.mean(len(s) for s in sents)
    if m <= 0:
        return None
    return statistics.pstdev(len(s) for s in sents) / m


def pctl(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


def text_metrics(text: str) -> dict:
    """一段正文的三项指标 + 字数（真模型跑与离线跑共用同一把尺）。"""
    cv = sent_cv(text)
    return {
        "chars": len(_clean(text)),
        "rep": 0.0,              # 相邻段重复率需要上下文，单独算
        "meta": metaphor_per100(text),
        "cv": cv,
        "grams": grams(text),
    }


# --------------------------------------------------------------------------- #
# 种子：一个够长的短篇设定，逐段续写
# --------------------------------------------------------------------------- #

SEED_CHARS = [
    {"name": "林夜", "role": "主角", "trait": "手指会发光；记不清自己是怎么倒下的"},
    {"name": "苏晚", "role": "接线人", "trait": "冷静，话短，先给选择再给解释"},
    {"name": "陆见微", "role": "对手", "trait": "总是笑着说话，目的不明"},
]
SEED_FORES = ["那道金光到底是什么", "陆见微为何认得这枚铜钥匙"]
SEED_REALMS = ["炼气", "筑基", "金丹", "元婴"]

PLOT_BIBLE = {
    "synopsis": "林夜在巷口醒来，霓虹的余光还黏在视网膜上。他记不清自己是怎么倒下的，"
                "只记得胸口那道凉意。他的手指正在发光，像没调好的灯管。",
    "direction": "三卷：巷口 → 接线人 → 锚。第一卷弄清金光来历，第二卷被迫站队。",
    "characters": SEED_CHARS,
    "foreshadowing": SEED_FORES,
    "conflicts": ["林夜要弄清金光的来历", "陆见微在利用他，而他还没察觉"],
    "relationships": [{"from": "林夜", "to": "苏晚", "type": "同路人"}],
    "realm_order": SEED_REALMS,
}


# --------------------------------------------------------------------------- #
# 人类散文基线 —— 阈值的来源
# --------------------------------------------------------------------------- #

def corpus_texts(spec: str | os.PathLike, max_chars: int | None = None) -> list[tuple[str, str]]:
    """读基线语料，返回 ``[(来源名, 全文)]``。

    支持三种来源：
    - ``.db``：sqlite，取 ``novel_chunks`` 的 ``text``（按 novel_id 聚成整本）
    - ``.txt``：单个文本文件
    - 目录：目录下所有 ``.txt``（忽略 README 等说明文件）

    读进来的文本会过一遍**项目自带的语料清洗**（`app.ingestion.clean`）——
    盗版站注入的 SEO 水印会稀释句长/比喻密度等指标（见 app/corpus_health.py）。

    `max_chars`：**先截断再清洗**（顺序反了会白洗几百万字 —— 清洗是最贵的一步）。
    """
    path = Path(spec)
    if not path.exists():
        raise FileNotFoundError(f"基线语料不存在：{path}")

    out: list[tuple[str, str]] = []
    if path.is_dir():
        for p in sorted(path.glob("*.txt")):
            if p.name.startswith("README"):
                continue
            out.append((p.stem, p.read_text(encoding="utf-8", errors="replace")))
    elif path.suffix.lower() == ".db":
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select novel_id, text from novel_chunks order by novel_id, chapter_no, idx"
            ).fetchall()
        finally:
            con.close()
        by_novel: dict[int, list[str]] = {}
        for nid, t in rows:
            by_novel.setdefault(nid, []).append(t)
        for nid, parts in by_novel.items():
            out.append((f"novel{nid}", "".join(parts)))
    else:
        out.append((path.stem, path.read_text(encoding="utf-8", errors="replace")))

    return [(name, _clean_corpus(text[:max_chars] if max_chars else text))
            for name, text in out]


def _clean_corpus(text: str) -> str:
    """清洗基线语料（复用项目自己的清洗实现，不另造一套）。

    大文本走 `clean_consumption_text`（正则精确删站点标记，快）；
    它解决不了的水印（句末植入式）由 `clean_novel_text` 的统计判据兜底。
    """
    try:
        from app.ingestion.clean import clean_consumption_text, clean_novel_text
    except Exception:  # noqa: BLE001 —— 清洗不可用时**原样返回**（并在报告里体现）
        return text
    try:
        cleaned, _ = clean_consumption_text(text)
    except Exception:  # noqa: BLE001
        cleaned = text
    try:
        cleaned2, _ = clean_novel_text(cleaned)
        return cleaned2
    except Exception:  # noqa: BLE001
        return cleaned


def human_baseline(texts: list[tuple[str, str]], chunk_chars: int = 3000,
                   max_chars: int | None = None) -> dict:
    """在真实语料上算同样的三个指标，作为阈值来源。

    切法与 `verify_sentinel.py` H 段一致（3000 字一段），避免两处口径不同
    导致「基线」和「被测对象」其实不是一回事。

    `max_chars`：每本源语料最多取多少字（默认全取）。用来把大语料截到
    与原基线同量级，避免基线被一本超长书主导。
    """
    reps: list[float] = []
    metas: list[float] = []
    cvs: list[float] = []
    n_chars = 0
    n_segs = 0
    books = []
    for name, text in texts:
        if max_chars:
            text = text[:max_chars]
        segs = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]
        n_chars += len(text)
        n_segs += len(segs)
        books.append({"name": name, "chars": len(text), "segs": len(segs)})
        gs = [grams(s) for s in segs]
        reps.extend(jaccard(a, b) for a, b in zip(gs, gs[1:]))
        for s in segs:
            metas.append(metaphor_per100(s))
            c = sent_cv(s)
            if c is not None:
                cvs.append(c)
    return {
        "books": books, "novels": len(texts), "chars": n_chars, "segs": n_segs,
        "rep": reps, "meta": metas, "cv": cvs,
    }


def thresholds(base: dict) -> dict:
    """从基线导出三条阈值（重复率上界、比喻密度上界、句长 CV 下界）。"""
    return {
        "rep_max": pctl(base["rep"], 0.9),
        "meta_max": pctl(base["meta"], 0.9),
        "cv_min": pctl(base["cv"], 0.1),
    }


def baseline_corpus_spec() -> str:
    """基线语料来源：环境变量优先，否则用开发库。"""
    env = os.environ.get("ACCEPTANCE_CORPUS", "").strip()
    if env:
        return env
    return str(HERE / "novel_dev.db")


# --------------------------------------------------------------------------- #
# 盲评用的「专名识别」与「对称脱敏」
# --------------------------------------------------------------------------- #

#: 盲评里**两侧必须同一套流程** —— 只脱敏一侧等于直接告诉评者哪边是人的。
#:
#: ## 为什么不能靠正则猜名字（实测证伪）
#:
#: 第一版用 `[一-鿿]{2,3}` 滑窗扫「像人名的词」。中文没有词边界，滑窗必然切错：
#: 实测「王爷公子上前」被切成「王爷公」「子上前」⇒ 正常词被当人名涂掉，
#: 而且人名「赵长风」这类三字名也会被切成两段漏掉。
#:
#: ## 现在的做法（复用项目里已有的两段式链路）
#:
#: 1. **召回**：PMI 结合度（`app.style.entities.pmi_candidates`）—— 语言无关、高召回。
#:    实测 60 万字古风语料上能同时召回「赵长安」「赵北山」「柳三更」这类三字全名
#:    （KG 抽取器只召回到截断形态「赵长」，三字名会漏）。
#: 2. **判定**：LLM 逐词判「是不是专名」（`app.style.entity_judge.judge_proper_nouns`），
#:    判据是「能否脱离本书语境独立成立」—— 不绑定任何一本书，换书照样用。
#:
#: 两侧各自跑一遍同样的召回 + 判定，再用同样的占位符格式替换 ⇒ 处理痕迹一致。

#: 占位符字符（与 `app/style/entities.py` 的 `Redactor` 同款：全角井号）。
_MASK_CHAR = "＃"


def proper_noun_candidates(text: str, *, min_count: int = 20) -> dict[str, int]:
    """PMI 召回专名候选（语言无关、高召回），返回 ``{候选词: 频次}``。

    `min_count` 是**绝对频次**下限：真实人名在一本书里出现几十上百次，
    虚词组合不会。实测 60 万字语料上 `min_count=20` 能召回三字全名。
    """
    from app.style.entities import pmi_candidates

    return pmi_candidates(text, min_count=min_count)


def shape_candidates(text: str) -> set[str]:
    """片段级的**人名形状**候选（`_valid_name` 通过的 2~4 字 n-gram）。

    纯召回，不做判定 —— 命中的是「长得像人名」，是不是专名交给 LLM。
    """
    from app.kg.extractor import name_shape_candidates

    return name_shape_candidates(text)


def recall_proper_nouns(snippets: list[str], source_text: str, *,
                         min_count: int = 20) -> dict[str, int]:
    """把两侧片段要遮的专名**召回来**（只召回，不判定）。

    两套召回合并，互补：

    - **整本/整批高频（PMI）** ⇒ 抓得住全书高频的主配角名（主角不会漏）
    - **片段级形状（KG 判据）** ⇒ 抓得住「在这 400 字里只出现一次」的次要角色名

    为什么必须两套：PMI 按绝对频次召回，**在 400 字片段上召回 0 个**（实测）——
    片段里出现 1 次的词达不到频次阈值。而盲评脱敏恰恰是片段级场景。

    只保留**真的出现在片段里**的候选 —— 判定是按词付费的，
    召回一堆用不上的词纯属浪费（实测两侧合并 454 个候选，
    而整本 PMI 候选合计两千多）。

    `source_text` 是该侧的**全文**（我们侧 = 全部段落拼接；人类侧 = 整本），
    用来跑整本级高频召回。
    """
    cand: dict[str, int] = {}
    try:
        for w, c in proper_noun_candidates(source_text, min_count=min_count).items():
            cand[w] = c
    except Exception:  # noqa: BLE001 - 召回失败不该拖垮整包（退化成只用形状召回）
        pass
    for s in snippets:
        for w in shape_candidates(s):
            cand.setdefault(w, 0)
    joined = "\n".join(snippets)
    return {w: c for w, c in cand.items() if w in joined}


def detect_proper_nouns(text: str, provider, *, candidates: dict[str, int] | None = None,
                        cache_path=None, log=None) -> list[str]:
    """找出文本里的专名（人名 / 地名 / 势力名 / 物件名）。

    两段式：PMI 召回 → LLM 判定（与 `app/style/entity_judge.py` 同一条链路）。
    返回按「长名优先」排序的专名列表 —— 替换时要长名先替，否则「赵长安」
    会被先替成「赵长」再留下一个孤零零的「年」。

    `candidates` 可传入**已过滤的候选集**（例如只保留出现在盲评样本里的词），
    省掉对用不上的词的 LLM 判定开销。
    """
    from app.style.entities import drop_fragments
    from app.style.entity_judge import judge_proper_nouns

    cand = candidates if candidates is not None else proper_noun_candidates(text)
    verdicts = judge_proper_nouns(list(cand.keys()), provider, cache_path=cache_path, log=log)

    layer_of = {
        "character": "characters", "place": "places",
        "faction": "factions", "item": "items",
    }
    table: dict[str, list[str]] = {k: [] for k in layer_of.values()}
    for word, verdict in verdicts.items():
        layer = layer_of.get(verdict or "")
        if layer:
            table[layer].append(word)

    # 残片剔除：「赵长」这种只是「赵长安」一部分的碎片要丢掉，
    # 否则替换后正文里会剩下半个名字。
    kept, _dropped = drop_fragments(table, text)
    names = {n for layer in kept.values() for n in layer}
    return sorted(names, key=lambda s: (-len(s), s))


def mask_names(text: str, names: list[str]) -> tuple[str, list[str]]:
    """把专名换成**等长占位符**，返回 ``(脱敏后的文本, 实际替换掉的专名)``。

    ## 为什么必须等长

    占位符比原词长会把句长指标推高（项目里 `app/style/entities.py` 实测过：
    `【人名3】` 让脱敏文本涨 23%、句长涨 23%）⇒ 指标失真。

    ## 为什么占位符**不带编号**

    带编号（`＃1` / `＃2`）会额外泄露「这一侧有多少个不同的专名」——
    出版小说的人名密度天然高于我们这侧，编号个数差本身就是线索。
    所以统一用**等长的井号串**：一个两字名 → `＃＃`，三字名 → `＃＃＃`。
    长度守恒 ⇒ 占位符字符数 == 原名字符数，两侧痕迹只反映「多少字是专名」。

    ## 为什么两侧用同一个函数

    只脱敏一侧 = 直接告诉评者哪边是人的。同一个函数、同一套占位符、
    同样的候选召回与判定口径 ⇒ 处理痕迹一致。
    """
    if not text or not names:
        return text, []
    from app.kg.extractor import looks_like_cut_artifact, trim_cut_artifact

    used: list[str] = []
    out = text
    # 长名优先：「赵长安」要先于「赵长」被替掉
    for name in sorted(set(names), key=lambda s: (-len(s), s)):
        if not name:
            continue
        # 切串残渣（「上苍梧山」）：削成核心再遮 ——
        # 直接丢掉就等于把真名留在题面里，比多遮一个字严重得多。
        target = name
        if looks_like_cut_artifact(name):
            core = trim_cut_artifact(name)
            if not core or core == name:
                continue
            target = core
        if target in out:
            out = out.replace(target, _MASK_CHAR * len(target))
            used.append(target)
    return out, sorted(used)


def mask_density(text: str) -> float:
    """占位符密度（每百字多少个井号）。

    用来查「处理痕迹本身会不会泄露」：两侧密度差一个量级时，
    评者数一数井号就知道哪一侧专名更密 —— 那一侧是出版小说。
    """
    if not text:
        return 0.0
    n = len(_clean(text)) or 1
    return 100.0 * text.count(_MASK_CHAR) / n


def mask_side(snippets: list[str], source_text: str, provider, *,
              cache_path=None, log=None) -> tuple[list[str], list[str]]:
    """把一侧的专名遮掉 —— 返回 ``(脱敏后的片段, 实际遮掉的词)``。

    **两侧必须调同一个函数**：召回口径、判定提示词、占位符格式全一样，
    否则「处理痕迹」本身就泄露哪边是人写的。

    流程：召回（`recall_proper_nouns`）→ LLM 判定（`detect_proper_nouns`）
    → 残片剔除 → 等长占位符替换（`mask_names`）。

    `source_text` 是该侧全文，用于整本级高频召回与残片独立出现率计算；
    `snippets` 是真正要放进题面的片段。
    """
    cand = recall_proper_nouns(snippets, source_text)
    names = detect_proper_nouns(source_text, provider, candidates=cand,
                                cache_path=cache_path, log=log)
    out: list[str] = []
    used: set[str] = set()
    for s in snippets:
        masked, hit = mask_names(s, names)
        out.append(masked)
        used |= set(hit)
    return out, sorted(used)


#: 脱敏痕迹密度比超过这个倍数 ⇒ 判「痕迹泄露」。
#:
#: 出处：出版物的人名/地名密度天然高于一段 AI 正文（实测我们 30 段里
#: 人名少得多）。两侧密度差一大截，评者数一数井号就能指出哪边是出版物。
#: 2.5 倍是「数得出来但不显眼」的量级 —— 超过它，盲评就不成立了。
DENSITY_LEAK_RATIO = 2.5

#: 密度比之外还要看绝对量：两侧都几乎没有专名时，比值再大也没有信息量。
DENSITY_LEAK_FLOOR = 0.5


def density_leak(density_a: float, density_b: float) -> bool:
    """两侧脱敏痕迹密度差到「数得出来」⇒ 判为泄露。"""
    hi, lo = max(density_a, density_b), min(density_a, density_b)
    if hi < DENSITY_LEAK_FLOOR:
        return False
    if lo <= 0:
        return True
    return (hi / lo) > DENSITY_LEAK_RATIO


#: 年代/题材标记词（用来判断两侧是不是同一个世界）。
#:
#: 词表**实测筛出来的**：拿我们那 30 段（现代都市）与一部古风武侠语料
#: 各 60 万字，逐词比频次，只留「一侧 ≥3 次/万字、另一侧 ≤0.5 次/万字」
#: 的那些（一次性探测脚本，跑完即弃）。拍脑袋想的词没用 ——
#: 实测「监控/卷宗/公交/地铁/数据」在两侧都是 0 次。
#:
#: ⚠️ 两侧用**同一张表** —— 表不一样就不是同一个尺。
_ERA_MARKERS = {
    "古风": ("剑", "刀", "枪", "世子", "道士", "江湖", "师父", "公子", "高手",
             "将军", "师兄", "师姐", "师弟", "师妹", "铁骑", "剑阵", "剑气",
             "朝廷", "陛下", "郡主", "王爷", "内力", "真气", "轻功", "内劲",
             "招式", "武学", "兵器", "秘籍", "世道", "帮主", "教主", "镖局",
             "镖师", "掌门", "门派", "宗门", "洞府", "丹药", "符箓", "法宝",
             "圣旨", "武林", "大内", "世家", "闭关", "经脉", "和尚方宝剑"),
    "现代": ("手机", "屏幕", "键盘", "监控", "卷宗", "公交", "地铁", "出租",
             "外卖", "快递", "数据", "文件", "电脑", "电梯", "超市", "便利店",
             "路灯", "公寓", "警察", "咖啡", "短信", "视频", "医院", "工位",
             "加班", "工资", "老板", "银行卡", "口罩", "耳机", "充电", "奶茶",
             "啤酒", "盒饭", "饭团", "方向盘", "车灯", "面包车", "后视镜",
             "仪表盘", "油表", "刹车", "油门", "安全带", "引擎", "玻璃",
             "水泥", "霓虹", "防盗网", "塑料袋", "打火机", "烟盒", "门禁卡",
             "巷口", "钥匙", "网约车", "地铁站", "公交车", "路灯杆", "红绿灯"),
}

#: 判定「主导年代」所需的最小命中数。
#:
#: 为什么要最小命中数：单看「份额」会被噪声骗 ——
#: 一段 400 字里只出现 1 个「手机」，份额就是 100%，但那是噪声不是年代。
ERA_MIN_HITS = 2

#: 「主导年代」的份额门槛：某侧标记命中里 ≥75% 属于同一代，才算这一代的。
ERA_DOMINANT_SHARE = 0.75

#: 「另一侧属于同一代」的份额上限：≤25% 才算「基本不属于那一代」。
ERA_OTHER_SHARE = 0.25


#: 章节标记行（「第74章 报复」「第 3 节」「第12回」……）。
#:
#: 为什么要在盲评取样前剥掉：盗版站的文本**每段单独一行、段首带缩进、
#: 章节标题单独一行**。直接切 400 字出来，章节标题会被拼进正文中间
#: （实测包里出现过「…落地了。 第74章 报复 “事情搞定了？”…」这种），
#: 而我们这侧**没有**章节标记 ⇒ **处理痕迹不对称**，
#: 评者一眼就能看出哪边是从书里粘的。
_CHAPTER_LINE = re.compile(
    r"^第\s*[0-9零一二三四五六七八九十百千万两]{1,8}\s*[章节回卷部篇]\s*\S{0,24}$"
)


def normalize_for_blind(text: str) -> str:
    """把一段文本归一到**中性版式**，供盲评取样用（两侧同一个函数）。

    做三件事，每件都是为了消掉「处理痕迹」：

    1. **去掉段首缩进与空行** —— 盗版站文本每段带四个空格缩进，
       我们这侧没有 ⇒ 缩进本身就是一个非对称特征。
    2. **去掉章节标记行** —— 见 `_CHAPTER_LINE`。
    3. **统一成「一段一行」** —— 不保留原文的空行分布。

    ⚠️ 这一步**两侧都要做**（同一个函数、同一套规则）。
    只做一侧 = 直接告诉评者哪边是出版物。
    """
    out: list[str] = []
    for raw in (text or "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        if _CHAPTER_LINE.match(s):
            continue
        out.append(s)
    return "\n".join(out)


def era_counts(text: str) -> dict[str, int]:
    """年代标记的**命中数**（不是密度）。"""
    t = _clean(text)
    return {era: sum(t.count(w) for w in words) for era, words in _ERA_MARKERS.items()}


def era_profile(text: str) -> dict[str, float]:
    """量一段文字的年代标记密度（每百字命中数）。

    用来**自检盲评包两侧是不是同一个世界** ——
    一侧全是「师兄/王爷/剑阵」、另一侧全是「手机/监控/方向盘」时，
    评者不看内容就能分出哪边是哪边 ⇒ 那个包**不是盲评**。

    ⚠️ 判定用 `era_mismatch`（份额 + 命中数），**不要**直接拿这里的密度
    当阈值 —— 绝对密度阈值会被噪声骗（实测我们侧现代密度 0.0438，
    阈值 0.05 就漏检了）。
    """
    t = _clean(text)
    n = max(len(t), 1)
    out = {}
    for era, words in _ERA_MARKERS.items():
        hits = sum(t.count(w) for w in words)
        out[era] = 100.0 * hits / n
    return out


def era_shares(text: str) -> dict[str, float]:
    """各年代的**份额**（该代命中 ÷ 总命中）。没有命中则份额为 0。"""
    c = era_counts(text)
    total = c["古风"] + c["现代"]
    if not total:
        return {"古风": 0.0, "现代": 0.0}
    return {k: v / total for k, v in c.items()}


def _era_dominant(counts: dict[str, int], era: str) -> bool:
    """这一侧是不是**明确**属于 `era` 那一代。"""
    other = "现代" if era == "古风" else "古风"
    total = counts[era] + counts[other]
    if counts[era] < ERA_MIN_HITS:
        return False
    return counts[era] / total >= ERA_DOMINANT_SHARE


def era_mismatch(a: str, b: str) -> tuple[bool, dict]:
    """两侧是否明显不在同一个年代/题材。返回 ``(是否不一致, 证据)``。

    `a` / `b` 应当传**整包文本**（两侧所有样本拼起来），不是单对样本。

    ## 判据为什么是「份额 + 命中数」而不是绝对密度

    第一版用「密度 > 0.05/百字」当阈值，实测**漏检**了：
    我们侧现代密度 0.0438（差一点点），人类侧古风 0.1768 —— 一侧过、一侧不过
    ⇒ 判成「不是跨代」。这是**被噪声骗**的典型：400~2400 字里标记词
    本来就稀疏，绝对阈值卡在哪都会有一边掉出去。

    现在的判据：
    - 一侧**明确**是古风（古风命中 ≥2 且占命中 ≥75%），另一侧**基本没有**
      古风标记（古风份额 ≤25%）⇒ 跨代；
    - 或两侧一古一今，各自主导 ⇒ 跨代。

    命中数下限 `ERA_MIN_HITS` 是为了把「只出现 1 次」当噪声，不当年代。
    """
    ca, cb = era_counts(a), era_counts(b)
    sa, sb = era_shares(a), era_shares(b)

    a_old = _era_dominant(ca, "古风")
    b_old = _era_dominant(cb, "古风")
    a_new = _era_dominant(ca, "现代")
    b_new = _era_dominant(cb, "现代")

    bad = (a_old and b_new) or (b_old and a_new)
    # 一侧明确古风、另一侧几乎没有古风标记 ⇒ 也是跨代
    if not bad:
        bad = (a_old and sb["古风"] <= ERA_OTHER_SHARE) or \
              (b_old and sa["古风"] <= ERA_OTHER_SHARE)

    return bad, {
        "counts": {"A": ca, "B": cb},
        "shares": {"A": sa, "B": sb},
        "density": {"A": era_profile(a), "B": era_profile(b)},
        "dominant": {"A": "古风" if a_old else ("现代" if a_new else "?"),
                     "B": "古风" if b_old else ("现代" if b_new else "?")},
    }


def blind_verdict(ours_text: str, human_text: str,
                  density_ours: float, density_human: float,
                  *, ours_full: str = "", human_full: str = "") -> dict:
    """判定这个盲评包**是不是真盲评** —— 返回 ``{"blind": bool, "reasons": [...], ...}``。

    三条硬约束，任一不成立 ⇒ 不是盲评：

    1. **题材/年代一致**：一侧全是古风武侠、另一侧全是现代都市 ⇒
       评者不看内容就能分出哪边是哪边（实测逐样本只检出 2/6，
       **整包级别**检出 100% ⇒ 必须整包算）。
    2. **脱敏痕迹对称**：两侧的占位符密度不能差到「数得出来」，
       否则数一数井号就知道哪边是出版物。
    3. **人类侧语料存在**：没有人类语料的包是「我们 vs 空气」，
       根本不是对照。

    `ours_full` / `human_full` 是两侧的**全文**（我们 = 全部段落，
    人类 = 整本）。给了就**额外**在全文上再判一次年代：
    整包只有 2400 字，标记词稀疏，容易漏检（实测整包判据在某一版上
    漏过一次）；全文几百万字，判据稳得多。任一处判出跨代 ⇒ 不是盲评。

    判成不是盲评时，**不出统计结论**（`verify_blind_eval.py` 会拒绝）——
    这是硬闸门：拿一个跨代样本做出来的「人类更自然」结论，
    量的其实是年代差，不是文风差。
    """
    reasons: list[str] = []
    evidence: dict = {}

    if not human_text.strip():
        reasons.append("没有人类侧语料：这个包不是对照")
        evidence["no_human_corpus"] = True
        return {"blind": False, "reasons": reasons, "evidence": evidence}

    bad, prof = era_mismatch(ours_text, human_text)
    evidence["era_profiles"] = prof
    if bad:
        reasons.append(
            "两侧题材/年代不同（一边古风武侠、一边现代都市）："
            "评者不看内容就能分出哪边是哪边")

    if ours_full.strip() and human_full.strip():
        bad_full, prof_full = era_mismatch(ours_full, human_full)
        evidence["era_profiles_full"] = prof_full
        if bad_full and not bad:
            reasons.append(
                "两侧的**全文**就是两个年代（一边古风武侠、一边现代都市）："
                "抽出来的片段大概率也带着这个差别")

    evidence["mask_density"] = {"ours": density_ours, "human": density_human}
    if density_leak(density_ours, density_human):
        reasons.append(
            f"脱敏痕迹密度差得出来（我们 {density_ours:.2f} vs 人类 "
            f"{density_human:.2f} 个井号/百字）：数一数就能认出出版物那一侧")

    return {"blind": not reasons, "reasons": reasons, "evidence": evidence}

