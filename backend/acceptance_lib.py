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

import hashlib
import json
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
    names, _unjudged = detect_proper_nouns_detailed(
        text, provider, candidates=candidates, cache_path=cache_path, log=log)
    return names


def detect_proper_nouns_detailed(text: str, provider, *, candidates: dict[str, int] | None = None,
                                  cache_path=None, log=None,
                                  batch_size: int | None = None,
                                  keep_all_names: bool = False) -> tuple[list[str], list[str]]:
    """同 `detect_proper_nouns`，但**多返回一份「没判出来」的词**。

    返回 ``(专名列表, 没判出来的词列表)``。

    ## `keep_all_names`：盲评脱敏**不丢碎片**（2026-10-31 修）

    残片剔除（`drop_fragments`）按「独立出现率」判断一个词是不是某个更长专名的
    碎片：independent = count(A) − Σ count(更长的含 A 的词)。

    ⚠️ **盲评脱敏必须关掉它**，否则会漏遮真名：
    实测《回声年代》里的「潇潇」—— 整本书里它几乎只作为「冷潇潇」的一部分出现
    （独立出现率 < 0.15）⇒ 被当碎片丢掉 ⇒ **不遮**；
    可它在我们的 400 字窗口里是**单独出现**的 ⇒ 真名原样留在题面里。

    ⇒ 盲评链路传 `keep_all_names=True`：**判定为专名的词一律遮**。
    「替换后剩下半个名字」这个问题由**长名优先替换**解决（`mask_names` 先替长名，
    「冷潇潇」先于「潇潇」被替掉），不需要靠丢碎片 ⇒ 关掉它没有副作用。

    ⚠️ 主链路（建实体表）**不传** ⇒ 仍按全书统计，行为不变。

    ## 为什么必须把这两者分开

    「判出来不是专名」和「没判出来」是**相反**的两种状态：

    - 判出来不是专名 ⇒ 不遮（遮了会毁掉正常词汇）
    - 没判出来 ⇒ **可能是个真名**，当普通词处理 = 把名字留在题面里 = 盲评作废

    实测（2026-10-30）：判定批次撞上 max_tokens 被截断时整批回包解析失败，
    旧实现把这一批**全部**记成「不是专名」并写进缓存 ⇒ 18 题盲评包的人类侧
    1896 个候选里 1880 个是 null，真名「张山」「李香玲」原样出现在题面里。
    """
    from app.style.entities import drop_fragments
    from app.style.entity_judge import UNJUDGED, judge_proper_nouns

    cand = candidates if candidates is not None else proper_noun_candidates(text)
    kwargs = {"cache_path": cache_path, "log": log}
    if batch_size:
        kwargs["batch_size"] = batch_size
    verdicts = judge_proper_nouns(list(cand.keys()), provider, **kwargs)

    layer_of = {
        "character": "characters", "place": "places",
        "faction": "factions", "item": "items",
    }
    table: dict[str, list[str]] = {k: [] for k in layer_of.values()}
    unjudged: list[str] = []
    for word, verdict in verdicts.items():
        if verdict == UNJUDGED:
            unjudged.append(word)
            continue
        layer = layer_of.get(verdict or "")
        if layer:
            table[layer].append(word)

    # 残片剔除：「赵长」这种只是「赵长安」一部分的碎片要丢掉，
    # 否则替换后正文里会剩下半个名字。
    #
    # ⚠️ 盲评脱敏传 `keep_all_names=True` ⇒ **不丢碎片** ——
    # 碎片剔除按整本书的独立出现率算，而盲评只遮 400 字窗口里的词：
    # 「潇潇」在整本书里几乎只作为「冷潇潇」的一部分出现 ⇒ 被当碎片丢掉
    # ⇒ 真名留在题面里（实测）。「半个名字」的问题由长名优先替换解决。
    if keep_all_names:
        names = {n for layer in table.values() for n in layer}
    else:
        kept, _dropped = drop_fragments(table, text)
        names = {n for layer in kept.values() for n in layer}
    return sorted(names, key=lambda s: (-len(s), s)), sorted(unjudged)


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


class MaskingIncomplete(Exception):
    """脱敏没做完（有一批词**没判出来**）—— 这个包不能当盲评包发出去。

    为什么要专门抛这个而不是默默放过：没判出来的词**可能是真名**，
    当普通词留着 = 名字直接暴露出处。盲评包宁可作废也不能带名字出去。
    """


#: 盲评脱敏的判定批次大小。
#:
#: 为什么从 30 提到 100：**判定成本 = 候选数 ÷ 批大小 × 单批耗时**，
#: 而单批耗时与批大小基本无关（输入输出都小，耗时在网络与推理）。
#: 实测 2026-10-31：人类侧一个臂的候选有 1000+ 个 ⇒ 30 一批要 35 批
#: （单批 3~10 分钟）⇒ 两个小时都跑不完，整个包出不来。
#:
#: ⚠️ 截断风险（回包太长被 max_tokens 砍掉）**不会致命**：
#: `entity_judge._judge_resilient` 会把没判到的词**拆成更小的批**重判
#: ⇒ 批大了最多多花一次调用，不会静默丢判定。
#: （静默丢判定才是当初调小它的原因 —— 那个坑已经用 `UNJUDGED` 三态补上了。）
JUDGE_BATCH_SIZE = 100


def mask_side(snippets: list[str], source_text: str, provider, *,
              cache_path=None, log=None) -> tuple[list[str], list[str]]:
    """把一侧的专名遮掉 —— 返回 ``(脱敏后的片段, 实际遮掉的词)``。

    **两侧必须调同一个函数**：召回口径、判定提示词、占位符格式全一样，
    否则「处理痕迹」本身就泄露哪边是人写的。

    流程：召回（`recall_proper_nouns`）→ LLM 判定（`detect_proper_nouns`）
    → 残片剔除 → 等长占位符替换（`mask_names`）。

    `source_text` 是该侧全文，用于整本级高频召回与残片独立出现率计算；
    `snippets` 是真正要放进题面的片段。

    ## 为什么「没判出来」要抛异常而不是默默放过

    判定分三种状态：判出是专名 / 判出不是专名 / **没判出来**。
    前两种都有明确的处置方式；第三种（批次失败、输出被截断）意味着
    「这个词有没有可能是真名，我不知道」—— 留着它 = 把名字留在题面里。

    所以这里 **fail closed**：抛 `MaskingIncomplete`，由出包流程把整个包
    判成不是盲评包（题面根本不发出去）。
    """
    cand = recall_proper_nouns(snippets, source_text)
    names, unjudged = detect_proper_nouns_detailed(
        source_text, provider, candidates=cand, cache_path=cache_path, log=log,
        batch_size=JUDGE_BATCH_SIZE,
        # 盲评脱敏**不丢碎片** —— 否则「潇潇」这种在整本书里只作为
        # 「冷潇潇」一部分出现的词会被丢掉，可它在 400 字窗口里是单独出现的
        # ⇒ 真名留在题面里（实测 2026-10-31）。
        keep_all_names=True)
    if unjudged:
        raise MaskingIncomplete(
            f"有 {len(unjudged)} 个候选词没判出来（判定批次失败或被截断），"
            f"它们可能是真名：{unjudged[:8]}{' …' if len(unjudged) > 8 else ''}")
    out: list[str] = []
    used: set[str] = set()
    for s in snippets:
        masked, hit = mask_names(s, names)
        out.append(masked)
        used |= set(hit)
    return out, sorted(used)


#: 题面级专名复查的提示词 —— **读成段正文、只报专名**。
#:
#: 为什么要有这一遍（第六道闸门）：逐词判定会漏。漏了就等于把真名留在
#: 题面里，盲评当场作废。而这一遍是**读正文**——模型自己看上下文挑名字，
#: 比「看着一个孤零零的词猜它是不是人名」可靠得多；输出又只是「名字列表」，
#: 不会像逐词判定那样动辄几百个 JSON 对象 ⇒ 不容易被 max_tokens 截断。
#: 题面级复查里，模型报的专名的**假阳性**特征。
#:
#: 模型报的词不是 100% 是专名 —— 逐题复查实测（2026-10-31）两类假阳性
#: 把整包卡死（18 题包卡在「＃＃＃、带柳」上，题面始终发不出去）：
#:
#: ① **占位符本身**：模型把已经遮好的「＃＃＃」当成专名报回来。
#:    提示词里明明写了「已经变成全角井号的位置不用管」—— **提示词挡不住**，
#:    只能靠事后过滤。遮它会把「＃＃＃」变成「＃＃＃＃＃」越遮越长。
#: ② **切串残渣**：《道途魔典》「他带柳入林而入」里的「带柳」——
#:    它是**跨词**的（原文说的是「带柳入林」，不是「带柳」这个人），
#:    不是人名；模型把跨词的「带柳」当成了专名。
#:
#: ⚠️ 规则必须**窄**：宁可多遮（多几个井号），不可漏遮（漏了 = 真名暴露出处）。
#:    ② 只在「**两字** + 首字是动词 + 次字是常见姓氏」时命中 ——
#:    三字以上的（落雁坳、慧照峰、**藏经阁**）**一律不动**：
#:    「藏」也在动词表里，若按动词首字一刀切，真地名会被当成残渣丢掉 ⇒ 漏遮。
#:
#: ⚠️ **带杂质字符的**（如「灶|房」，竖线来自抓取残留）**不丢** ——
#:    丢掉 = 让那道竖线原样留在题面上（评者一看就知道这文本被处理过，
#:    还会以为是机器那边被处理了 ⇒ 反而偏向另一侧）。
#:    遮掉反而干净：等长占位符会把竖线一起吃掉。
_RESIDUAL_CUT_VERB_LEAD = set(
    "传递送取拿带领收接留走奔跑逃躲藏找寻望听闻问答说讲写画唱念数称叫唤喊呼"
    "打放摆挂抱提端捧握拍推拉拖举挥甩踏踩跨越爬登钻挤挨靠贴系绑拴套罩盖蒙"
    "掩露显呈献供予给"
)

#: 名字里混进杂质字符时，最多只当作这么长的东西遮（再长就不像名字了）。
_RESIDUAL_IMPURE_MAX_LEN = 6

#: 专名的合法字符：汉字、字母、数字。
_RESIDUAL_NAME_CHARS = re.compile(r"^[一-鿿A-Za-z0-9]+$")


def _drop_residual_false_positives(names: list[str]) -> list[str]:
    """去掉模型报的**假阳性**专名（见上面两类的说明）。

    只丢**确定不是专名**的东西：占位符、以及「动词 + 姓」的两字残渣。
    其余一律保留 —— 宁可多遮几个字（多几个井号），不可漏遮（漏了 = 暴露出处）。
    """
    from app.kg.extractor import _SURNAME_CHARS

    out: list[str] = []
    for n in names:
        if _MASK_CHAR in n:
            # ① 占位符（含「＃＃阁」这种只剩遮蔽符的）
            continue
        if len(n) > _RESIDUAL_IMPURE_MAX_LEN and not _RESIDUAL_NAME_CHARS.match(n):
            # 杂质字符 + 很长 ⇒ 多半是模型把一整句话抄回来了，不是名字
            continue
        if (len(n) == 2 and n[0] in _RESIDUAL_CUT_VERB_LEAD
                and n[1] in _SURNAME_CHARS):
            # ② 「动词 + 姓」的两字切串残渣（如「带柳」）
            continue
        out.append(n)
    return out


_RESIDUAL_SYSTEM = """你是中文小说正文的校对。下面这段正文里可能还残留着专有名词（人名、地名、势力名、物件名）。

请把**确实是专名**的词，按它在原文里的字面形式列出来。
不要列普通词：职业、泛称、动作、状态、形容词、虚词、章节标题都不要列。
**年代/生活名词也不算专名**：供销社、搪瓷缸、粮票、车间、拖拉机、厂子、澡堂、
邮局、街办、粮站、门市部、铝饭盒、二八大杠 —— 这些是那个年代人人都有的东西，
不是「这本书独有的名字」，一律不要列。
只有当一个词在文里指的是**具体的、专有的**对象时才列（例如某座城、某个门派、
某个人）；泛指一类东西的词不要列。

**称呼型的专名同样算专名**（只要它在文里特指某个人/某个家族/某个组织）：
- 亲昵称呼：「阿福」「老郑」「小安」这类；
- 带姓氏的头衔：「王夫人」「赵先生」「李小姐」这类；
- 以特征代称的外号：「麻子」「大个儿」这类。
判断标准是「这个词在文里是不是**特指**某个具体的人或家族」，
而不是「它看起来像不像一个常见词」——
像常见词但特指某人的，照样要列（漏掉它，评者就可能认出这是哪本书）。

已经变成全角井号（＃）的位置不用管。

只输出 JSON 数组（没有就输出 []），例如：["张三","青丘城"]"""


def residual_proper_nouns(text: str, provider, *, cache_path=None,
                           log=None) -> list[str]:
    """题面级复查：这段正文里**还剩哪些专名**。

    返回词列表（只保留**确实出现在正文里**、且**不是假阳性**的词 ——
    模型偶尔会凭空补词，也会把占位符/切串残渣当专名报回来）。

    ## 这是**独立于逐词判定**的一遍

    - 逐词判定：把候选词一个一个丢给模型判「是不是专名」—— 会漏
    - 这一遍：把**成段正文**丢给模型，让它自己挑出还是专名的词

    两遍的输入、提示词都不同 ⇒ 一遍漏了另一遍大概率能抓住。

    ## ⚠️ 这里**故意不缓存**

    安全闸门缓存**否定结论**是危险的：模型是非确定性的 ——
    同一段文字问两次，答案可能不同（实测：s06 的「麻子」第一次没报出来、
    第二次报出来了）。一旦「这段是干净的」被缓存下来，
    之后每次重建都会白捡这个**假阴性** ⇒ 题面上留着真名还以为查过了。

    代价是每次出包都要重新问一遍（实测约 25 分钟）；
    这个代价远小于「漏遮 ⇒ 盲评当场作废」。

    （`cache_path` 参数保留只为兼容旧调用；**不要再传**。）
    """
    if not text or not text.strip():
        return []

    raw = ""
    try:
        raw = provider.chat(_RESIDUAL_SYSTEM, text)
    except Exception as exc:  # noqa: BLE001 - 复查失败要报出来，不能当「查过了」
        raise MaskingIncomplete(f"题面级专名复查失败：{exc}") from exc
    names: list[str] = []
    m = re.search(r"\[.*\]", raw or "", re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            names = [str(x).strip() for x in arr
                     if isinstance(x, str) and x.strip()]
        except Exception:  # noqa: BLE001
            names = []
    # 模型偶尔会凭空补词 —— 只认正文里真有的
    names = sorted({n for n in names if n in text})
    # ⚠️ 去掉**假阳性**：占位符、「动词 + 姓」的两字切串残渣。
    # 见 `_drop_residual_false_positives` 上面的三类说明。
    names = _drop_residual_false_positives(names)
    if log:
        log(f"题面复查：残留专名 {len(names)} 个 {names[:8]}")
    return names


def residual_proper_nouns_per_item(
    items: list[tuple[str, str]], provider, *, cache_path=None, log=None,
) -> dict[str, list[str]]:
    """**逐题**做题面级专名复查 —— 返回 ``{题号: 残留专名}``。

    ⚠️ `cache_path` 已**弃用**（保留参数只为兼容旧调用）：安全闸门不能缓存
    否定结论 —— 模型的答案不是确定的，缓存会把一次走运的「干净」固化成
    每次重建都在白捡的假阴性。详见 `residual_proper_nouns` 的说明。

    ## 为什么必须逐题问，不能整包一次问

    实测（2026-10-31）：把整包 17 题（14422 字）一次性丢给模型问「还有哪些专名」，
    模型返回的是 **空数组** —— 它读不完那么长的一段，直接放弃了 ⇒
    **第六道闸门整个是假阴性**（包被判为「题面干净」，实际漏了一堆专名）。
    同一份文本**逐题问**（每题约 800 字），立刻报出 6 题残留专名，
    其中包括《道途魔典》的组织名「聚金阁」和我们侧自己的原创专名「落雁坳」「慧照峰」。

    ⇒ 长文本会让模型**沉默**而不是报错 —— 「没报出来」被当成了「没有」。
    这类假阴性比报错危险得多：报错会让整包作废，假阴性让坏包蒙混过关。

    ## 为什么「报出来」还要当真的

    逐题问报出的名字**逐个都真的在正文里**（`residual_proper_nouns` 已过滤）。
    宁可多遮几个（遮错了只是多一点井号），不可漏遮（漏了 = 真名暴露出处）。
    """
    out: dict[str, list[str]] = {}
    for sid, text in items:
        try:
            # ⚠️ **不传 cache_path**：安全闸门不缓存否定结论（见上面的说明）
            out[sid] = residual_proper_nouns(text, provider, log=log)
        except Exception as exc:  # noqa: BLE001
            raise MaskingIncomplete(f"题面级专名复查失败（{sid}）：{exc}") from exc
    return out


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


#: 题材指纹词表（**子题材**，不是年代）—— **粗筛口径**，不是判据。
#:
#: ## 为什么要有这道闸门
#:
#: 年代闸门只分了「古风 / 现代」两桶，而实测的泄露是**同代不同题材**：
#: 人类侧那本用「厉鬼 / 复苏 / 规则」这套灵异术语，我们侧是都市市井悬疑 ——
#: 评者读一两题就知道哪边是出版物，**年代闸门全绿也没抓到**。
#:
#: ## 词表只当粗筛：实测证明「按词判题材」很弱
#:
#: 2026-10-31 实测（18 本本地语料，逐词统计）：
#: **「剑」「刀」「江湖」「师父」「将军」「系统」「掌柜」「伙计」「巷子」这类词，
#: 在过半的书里都出现过** —— 它们是网文的通用词，不是题材专有词。
#: 用它们当标记 ⇒ 现代都市小说被误判成古风武侠（实测 18 本里有 9 本被标错）。
#:
#: 所以：
#: ① 词表里**只留区分度高的词**（在多数书里根本不出现或明显集中在某一题材）；
#: ② **权威口径是 LLM 判题材**（`judge_genres`）—— 它读正文、看世界观，
#:    不靠几个词；③ 词表只用来**粗筛候选窗口**，省掉大量 LLM 调用。
#:
#: 选词标准：一个词要么在**某个题材里集中出现**，要么是**该题材的硬名词**
#: （「供销社」「镖局」「人皮纸」这种别处根本不用的词）。
GENRE_MARKERS = {
    "灵异克苏鲁": (
        "驭鬼者", "鬼域", "邪祟", "凶宅", "灵异事件", "诡异事件", "收容所",
        "怨灵", "凶灵", "尸鬼", "鬼眼",
    ),
    "规则怪谈": (
        "终焉", "主办方", "人皮纸", "生肖", "副本",
    ),
    "都市异能": (
        "面板", "异兽", "商城", "签到", "异能者", "异能", "超能力", "宿主",
    ),
    "都市市井": (
        "冰柜", "后厨", "饭馆", "面馆", "粉笔", "手电", "城管", "筒子楼",
        "招待所", "灶眼", "巷口", "街口",
    ),
    "年代八五": (
        "供销社", "工分", "粮票", "布票", "个体户", "国营", "车间", "厂子",
        "媒人", "介绍对象", "洋房", "收音机", "公社", "万元", "下乡", "返城",
        "搪瓷缸", "拖拉机",
    ),
    "古风武侠": (
        "内力", "真气", "镖局", "朝廷", "世子", "郡主", "教主", "帮主",
        "师妹", "剑客", "轻功", "剑法", "刀法",
    ),
    "东方玄幻": (
        "修仙", "炼气", "筑基", "金丹", "元婴", "化神", "洞府", "宗门",
        "灵根", "符箓", "阵法", "丹药", "法宝", "妖修", "灵石", "渡劫",
        "剑修", "仙门", "仙宗", "灵兽", "神识", "元神", "秘境", "功法",
        "御剑", "妖兽", "灵脉", "结丹",
    ),
    "言情豪门": (
        "白月光", "闪婚", "前妻", "千亿", "总裁",
    ),
}


def genre_profile(text: str) -> dict[str, float]:
    """题材指纹：每个子题材的标记词密度（每百字命中数）。

    用来查「两侧是不是在讲同一类故事」——
    一侧满篇「厉鬼/复苏/规则」、另一侧是市井巷子 ⇒ 评者不看文风就能分出哪边是出版物。
    """
    t = _clean(text)
    n = max(len(t), 1)
    return {g: 100.0 * sum(t.count(w) for w in words) / n for g, words in GENRE_MARKERS.items()}


def genre_dominant(profile: dict[str, float], *, min_hits: float = 0.05) -> str:
    """主导子题材（密度最高且过阈值的那个）；都不够格 ⇒ 空串。

    为什么要最小密度：单个词命中一次就算「主导」的话，噪声会赢。
    """
    best, best_v = "", -1.0
    for g, v in sorted(profile.items(), key=lambda x: -x[1]):
        if v > best_v:
            best, best_v = g, v
    return best if best_v >= min_hits else ""


def genre_distance(a: str, b: str) -> float:
    """两段文本的题材指纹距离（0 = 完全一样，1 = 毫无共同点）。

    口径（2026-10-31 修过一次）：
    - 两侧主导题材**都明确且相同** ⇒ 0.0
    - 两侧主导题材**都明确但不同** ⇒ 1.0
    - 只有一侧有主导 ⇒ 0.5（半信半疑：一边露了题材指纹，另一边没露）
    - **两侧都没有主导 ⇒ 0.0**

    ⚠️ 最后一条是修掉的 bug：原来这一档算成 **1.0**（"毫无共同点"），
    与事实相反 —— 两侧都没露题材指纹，说明**没有题材线索可用**，不是题材不同。
    口径错了的后果很实：都市臂的人类侧整本没有题材标记，于是每一题
    都被算成"最大不同"，一道题都出不来。
    """
    pa, pb = genre_profile(a), genre_profile(b)
    da, db = genre_dominant(pa), genre_dominant(pb)
    if da and db:
        return 0.0 if da == db else 1.0
    if da or db:
        return 0.5          # 一边有明确题材、另一边没有 ⇒ 半信半疑
    # 两边都没有明确主导 ⇒ **没有题材线索可用**（不是「题材不同」）
    return 0.0


#: 两侧题材指纹距离超过这个值 ⇒ 判「不是同一个题材」。
#:
#: 口径：距离 0 = 主导题材一致（或两侧都没露）；0.5 = 一边有题材一边没有；
#: 1 = 主导题材明确不同。
GENRE_MAX_DISTANCE = 0.5


def profile_mean(texts: list[str]) -> dict[str, float]:
    """一组文本的**平均**题材指纹（当锚点用）。

    为什么要平均：多题材臂里「我们侧」有若干段（可能跨场景），
    单段的主导题材会飘；取平均等于「这一臂大致是什么味」。
    """
    ps = [genre_profile(t) for t in texts if t]
    if not ps:
        return {g: 0.0 for g in GENRE_MARKERS}
    return {g: sum(p.get(g, 0.0) for p in ps) / len(ps) for g in GENRE_MARKERS}


def profile_distance(p: dict[str, float], q: dict[str, float]) -> float:
    """两个题材指纹的 L1 距离（越大越不像；**只看排序用**）。

    ⚠️ 这是**粗筛**口径，不是判据：词表把 18 本语料里的 9 本判错过。
    它只用来把「明显不像」的候选窗口排到后面，省掉大量模型调用；
    真正的题材一致与否由模型判（`judge_genres`）。
    """
    return sum(abs(p.get(g, 0.0) - q.get(g, 0.0)) for g in set(p) | set(q))


# --------------------------------------------------------------------------- #
# 第八道闸门：世界观级子题材指纹（**只用于剔除，绝不用于放行**）
# --------------------------------------------------------------------------- #

#: **世界观级**的子题材词表。
#:
#: 为什么要有它（2026-10-31 实测）：模型判题材的闭集只有 9 类，
#: 而「军旅训练营」「古风宅斗言情」「耽美荤话」「职场公文」「系统流」
#: **都不在闭集里** ⇒ 模型只能判「看不出来」⇒ 闸门按「两边都看不出来」
#: 放行 ⇒ 但评者**一眼就能看出题材不同**。
#: 实测 13 题里 5 题栽在这上面：s01 宅斗言情 vs 修真、s11 荤话 vs 查案、
#: s12 军旅训练 vs 职场悬疑、s10 押送囚犯 vs 修车铺子、s04 超能力 vs 市井写实。
#:
#: ⚠️ **词表只用来剔除，不用来放行**：判不准时只会少几道题，
#: 不会放进坏题 —— 这是词表唯一安全的用法（粗筛放行才是拿词表当判据）。
#:
#: ⚠️ **词是按实测命中率挑的**（2026-10-31）：
#: 指纹词要在「题材窗口」里出现得**不罕见、但也不普遍** ——
#: 太泛的词（「军」「符」「单字」）会把整本书都染成这一类，
#: 太稀的词则几乎撞不上。实测（400 字窗口命中率）：
#: 「据点」3.3% / 「囚犯」4.7% / 「看守所」3.3% / 「任务」7.9% / 「系统」4.2% /
#: 「直播」3.7% / 「魂魄」4.3% / 「阵法」2.2% / 「厉鬼」1.8%；
#: 而「军」37.7% / 「符」5.1% ⇒ 单字太泛，已剔除。
FLAVOR_CLASSES: dict[str, tuple[str, ...]] = {
    # 修真/仙侠：只收**修真专属**的词（「弟子/师兄」这种角色词会误伤武侠，不收）
    "修真仙侠": ("宗门", "外门", "内门", "亲传", "修真", "筑基", "渡劫", "灵根",
                  "结丹", "元婴", "金丹", "妖兽", "御剑", "灵石", "玉牌", "洞府",
                  "仙门", "道袍", "拂尘", "修为", "闭关", "灵气", "剑诀", "法宝",
                  "灵兽", "符纸", "阵法"),
    "古风武侠": ("内力", "真气", "剑气", "镖局", "江湖", "朝廷", "世子", "王爷",
                  "剑客", "轻功", "点穴"),
    "古风宅斗言情": ("成亲", "娶了", "妾", "侯爷", "王府", "赐婚", "凤冠", "嫡女",
                      "庶女", "后宅", "掌家", "相公", "娘子", "夫君", "花轿", "休书",
                      "和离", "纳妾", "侍寝", "嫁妆", "聘礼", "姨娘", "世子妃",
                      # ↓ 2026-10-31 补（情爱纠纷/八卦线）：
                      #   旧词表只收「成亲/王府/纳妾」这类**制度性**词 ⇒
                      #   《道途魔典》里那段八卦/艳遇（风流谈资/洞底救美/颠倒黑白）
                      #   整段漏网 ⇒ s16 混进了坏题。
                      "风流谈资", "风流韵事", "洞底", "救美", "强词夺理",
                      "颠倒黑白", "死心塌地", "风流", "家纹", "家丑", "洞房",
                      "族谱", "指婚", "换亲", "私通", "妾室", "抬进门", "婚事",
                      "家主", "洞房花烛", "青梅竹马", "红颜祸水"),
    "灵异克苏鲁": ("厉鬼", "复苏", "收容", "邪祟", "凶宅", "诡秘", "诅咒", "魂魄",
                    "神祠", "阴魂", "棺木", "坟堆"),
    "规则怪谈": ("违规", "通关", "主办方", "守则", "副本", "规则"),
    "言情豪门": ("总裁", "闪婚", "替身", "白月光", "千亿", "豪门", "婚约"),
    # ⚠️ 不收单字「军」「兵」（实测「军」命中 37.7% 的窗口 ⇒ 太泛）
    # ⚠️ 「军部/将领/外敌/安全屋」是 2026-10-31 逐题肉眼看漏掉后补的：
    #   那类窗口（别墅安全屋、军用卫星、情报机关）肉眼看一眼就是谍战/军旅，
    #   而旧词表只收「兵王/据点/看守所」⇒ **整类漏掉**。
    "军旅军事": ("兵王", "据点", "分队", "班组", "连长", "营长", "军衔", "实弹",
                  "射击", "军车", "军用", "部队", "战友", "班长", "军官", "军装",
                  "阅兵", "看守所", "囚犯", "押送", "军令", "营房", "特种兵",
                  "军部", "将领", "外敌", "安全屋", "士兵", "军区", "武装",
                  "情报机关", "特工", "卧底"),
    "都市系统流": ("面板", "宿主", "积分", "抽奖", "技能栏", "属性点", "升级",
                    "委托", "任务奖励", "商城", "系统", "任务", "技能", "经验"),
    "都市异能": ("异能", "超能力", "觉醒", "强化", "读心", "透视", "念力", "超凡"),
    "都市直播围观": ("直播", "直播间", "弹幕", "观众", "主播", "打赏", "礼物",
                      "粉丝", "围观"),
    "职场公文": ("离职", "工牌", "交接单", "权限", "IT部", "工位", "出货",
                  "出差", "报销", "签字", "部门", "单子"),
    "耽美成人": ("射进来", "唇瓣", "含住", "喘息", "春药", "合欢", "双修",
                  "欢好", "私处", "缠绵", "情欲"),
    # ⚠️ 以下三类是 2026-10-31 **逐题肉眼复核**后补的（第八道闸门第二次整类漏网）：
    #
    #: 实测经过：旧词表 14 类漏掉了这三整类 ⇒ 三道好题混进了坏题：
    #:   · s04 人类侧 = 深山寻人（景区/深山/老林/失踪）×，我们侧 = 都市系统流
    #:   · s06 人类侧 = 军事谍战（信号基站/撤离/情报/任务）×，我们侧 = 都市系统流
    #:   · s16 人类侧 = 古风情爱纠纷（风流谈资/洞底/救美/颠倒黑白）×，我们侧 = 修真
    #:
    #: ⇒ **根因是「闭集分类器 + 有限词表」只会在闭集内正确**：
    #: 模型判题材用 9 类闭集，「军事谍战」「深山寻人」「情爱纠纷」都不在里面
    #: ⇒ 模型只能判「看不出来」；而词表没收录 ⇒ 两道闸门同时放行。
    #:
    #: ⚠️ **词是按实测命中率挑的**（2026-10-31，全书 400 字窗口）：
    #: 「军事」类：情报 4.8% / 任务 5.9% / 部队 2.7% / 支援 2.0% / 潜入 1.8%
    #:   / 接应 1.7% / 军人 1.5% / 撤离 1.4% / 保密 0.9% / 敌方 0.6%
    #:   / 小队 0.5% / 上将 0.2% / 军车 0.3% / 伏击 0.2% / 行动组 0% / 哨所 0%
    #: 「野外寻人」类：山里 1.5% / 深山 1.1% / 景区 0.7% / 老林 0.7% / 失踪 0.8%
    #:   / 帐篷 0.9% / 营地 0.5% / 进山 0.5% / 山路 0.6% / 篝火 0.2% / 采药 0.2%
    #: 「古风情爱」类：家主 6.9% / 家纹 1.0% / 洞底 0.7% / 风流 0.5%
    #:   / 风流谈资 0.1% / 救美 0.2% / 强词夺理 0.1% / 颠倒黑白 0.3% / 死心塌地 0.1%
    #:
    #: ⚠️ **「任务」「情报」「失踪」「山路」「主人」这类泛词不进表**：
    #: 它们在**两侧都常见**（任务 5.9% 但我们侧 0% ⇒ 方向对，可进），
    #: 但「行动」「男人」「女人」「线路」这类命中率高到会把整类染色 ⇒ 不收。
    #: 判据：**该词必须在我们侧（另一题材）的窗口里出现得少**。
    #:
    #: ⚠️ **2026-10-31 定稿时按实测又收紧了**（拿最终 17 题包逐题验算）：
    #: 「情报」「任务」「支援」「部队」「潜入」这些**泛词**收进来后，
    #: 会把 s10/s11/s14 三道**好题**误判成不对称（各只命中 1 次）⇒
    #: 误伤比漏网更贵（好题被砍 = 白跑一趟模型判定）。
    #: ⇒ 只收**该题材的类型标志**：出现了读的人就知道是哪种题材。
    #: 定稿后：抓到坏题 3/3（s04/s06/s16），误伤好题 0/14。
    "军旅谍战": ("信号基站", "基站", "撤离", "接应", "军人", "敌方", "小队",
                 "上将", "军车", "伏击", "哨所", "行动组", "指挥部", "前线",
                 "牺牲", "部署", "首长", "保密"),
    "都市野外寻人": ("山里", "深山", "景区", "老林", "失踪", "帐篷", "营地",
                      "进山", "篝火", "采药", "失联", "荒郊", "野地", "无人区",
                      "荒野", "山洞", "迷雾", "野兽"),
}

#: 一侧命中几次就算「这一侧有这个指纹」。
#:
#: ⚠️ 为什么是 **2 次**（不是 1 次）：实测 2026-10-31 ——
#: 「任务」「系统」「部门」「单子」这类词在正文里**顺口就来**，
#: 一侧出现 1 次、另一侧 0 次，**不构成题材指纹**
#: （实测都市臂 26 个我们侧窗口里，有 6 个只因「系统流差 1 个词」被否掉）。
#: ⇒ 2 次才算「这一侧有这类题材」。
FLAVOR_MIN_ON = 2

#: **例外**：这几类的命中词本身就是类型标志，1 次就够。
#:
#: 「射进来」「厉鬼」「据点」「成亲」「违规」—— 这些词出现一次，
#: 读的人就知道这一段是哪种题材；反过来也一样（另一侧没有 ⇒ 定位得了）。
#:
#: ⚠️ 2026-10-31 补三类（实测按「去泛词」配置验算：抓到坏题 3/3、误伤好题 1/14）：
#: 「信号基站」「风流谈资」「景区」这类词出现**一次**就够定性 ——
#: 它们不是「顺口就来」的词（任务/系统/部门 才是），而是该题材的类型标志。
FLAVOR_STRONG_MIN_ON = 1

#: 上面那几类「1 次就算」的子题材。
FLAVOR_STRONG_CLASSES = (
    "军旅军事", "耽美成人", "灵异克苏鲁", "规则怪谈", "古风宅斗言情",
    # ↓ 2026-10-31 新增两类（类型标志，1 次即定性）
    "军旅谍战", "都市野外寻人",
)

#: 另一侧命中几次以内算「这一侧没有这个指纹」（0 = 一次都没有）。
FLAVOR_MAX_OFF = 0


def flavor_hits(text: str) -> dict[str, int]:
    """一段文本里各子题材词表的命中次数（只记命中的）。"""
    out: dict[str, int] = {}
    t = text or ""
    for cls, marks in FLAVOR_CLASSES.items():
        n = sum(t.count(m) for m in marks)
        if n:
            out[cls] = n
    return out


def flavor_profile(text: str) -> dict[str, float]:
    """子题材指纹（每百字的命中次数）。窗口等长时与次数等价。"""
    t = text or ""
    if not t:
        return {}
    scale = 100.0 / len(t)
    return {cls: n * scale for cls, n in flavor_hits(t).items()}


def flavor_distance(p: dict[str, float], q: dict[str, float]) -> float:
    """两段文本的子题材指纹 L1 距离（**只用于排序**，同上）。"""
    return sum(abs(p.get(g, 0.0) - q.get(g, 0.0)) for g in set(p) | set(q))


def flavor_mean(texts: list[str]) -> dict[str, float]:
    """一组文本的平均子题材指纹（当锚点用）。"""
    ps = [flavor_profile(t) for t in texts if t]
    if not ps:
        return {}
    keys = {g for p in ps for g in p}
    return {g: sum(p.get(g, 0.0) for p in ps) / len(ps) for g in keys}


def flavor_present(hits: dict[str, int]) -> tuple[str, ...]:
    """按**阈值口径**判定「哪些类算这一侧有」。

    ⚠️ **必须与 `flavor_asymmetry` 用同一套阈值** ——
    候选池按类集合分组时如果用「命中 1 次就算」，而配对时用「命中 2 次才算」，
    两边口径就对不上：我们侧窗口被分到「系统流」组（池子给它准备了
    系统流的人类侧窗口），配对时它却按「无指纹」处理（需要无指纹的窗口）
    ⇒ 白白浪费池子名额（实测都市臂只出题 5/10）。
    """
    out: list[str] = []
    for cls, n in hits.items():
        on = (FLAVOR_STRONG_MIN_ON if cls in FLAVOR_STRONG_CLASSES
              else FLAVOR_MIN_ON)
        if n >= on:
            out.append(cls)
    return tuple(sorted(out))


def flavor_signature(text: str) -> tuple[str, ...]:
    """一段文本的「子题材类集合」（按阈值口径，见 `flavor_present`）。"""
    return flavor_present(flavor_hits(text))


def flavor_asymmetry(a: str, b: str) -> list[dict]:
    """**一侧有某个子题材指纹、另一侧完全没有** ⇒ 泄露。

    这正是用户点破的那件事（2026-10-31）：
    「一个厉鬼的，另外一个不知道什么题材 ⇒ 判断一两个就能分出哪边是人写的」。

    ⚠️ 判定口径是**非对称**而不是「题材相同」：
    泄露的本质是「一侧比另一侧更扎眼 ⇒ 评者能定位」。
    两边都露（哪怕露的是不同子题材）不在这条规则的射程内 ——
    那种情况由模型判题材那一关管。

    ⚠️ **命中次数有下限**（`FLAVOR_MIN_ON`，强类型 1 次、普通 2 次）：
    「任务」「系统」这类词顺口就来，差 1 个词不构成题材指纹。

    返回空列表 = 对称，没问题。
    """
    ha, hb = flavor_hits(a), flavor_hits(b)
    bad: list[dict] = []
    for cls in set(ha) | set(hb):
        na, nb = ha.get(cls, 0), hb.get(cls, 0)
        on = (FLAVOR_STRONG_MIN_ON if cls in FLAVOR_STRONG_CLASSES
              else FLAVOR_MIN_ON)
        if na >= on and nb <= FLAVOR_MAX_OFF:
            bad.append({"class": cls, "only_in": "A", "hits_a": na, "hits_b": nb})
        elif nb >= on and na <= FLAVOR_MAX_OFF:
            bad.append({"class": cls, "only_in": "B", "hits_a": na, "hits_b": nb})
    return bad


# --------------------------------------------------------------------------- #
# 题材判定（权威口径）：让模型读正文判题材
# --------------------------------------------------------------------------- #

#: 题材分类的**闭集**（子题材级）。判定模型只能原样选一个。
#:
#: ⚠️ 为什么是闭集：开放式提问会让模型自由发挥，同一段文字两次判出两个类，
#: 那这道闸门就成了随机数。闭集 + 「看不出来就选其他」才有可复现性。
GENRE_CLASSES = (
    "现代都市（市井生活流）",
    "现代都市异能（系统流/超能力）",
    "年代穿越（1980前后/改革开放）",
    "古风武侠（江湖门派/内力招式）",
    "东方玄幻（修真/仙侠/御剑）",
    "灵异克苏鲁（厉鬼复苏/收容/邪祟）",
    "规则怪谈（终焉之地/违规/通关）",
    "言情豪门（总裁/婚约/替身）",
    "其他（以上都不是，或看不出来）",
)

#: 「看不出来」的那一类。两边都落在它上面 ⇒ **不算题材不一致**
#:（都没露题材指纹 ⇒ 没有题材线索可用）。
GENRE_UNKNOWN = GENRE_CLASSES[-1]

#: **强题材**：术语本身就很扎眼，一侧有、另一侧没有 ⇒ 评者能一眼分辨
#: （「厉鬼/复苏/收容」「终焉/违规/通关」「供销社/粮票/厂子」「总裁/闪婚/替身」
#:   「内力/真气/镖局」「筑基/渡劫/灵根/御剑」）。
#:
#: 弱题材（现代都市市井、现代都市异能）的词汇是**日常生活词**，
#: 一侧多一点、另一侧少一点，评者很难据此判断「哪边是出版物」⇒ 弱题材的
#: 「一侧看不出来」不判为不一致（但会记进证据，供人复核）。
#:
#: ⚠️ 2026-10-31 修正：古风武侠与东方玄幻原先被算作弱题材，这是**错的** ——
#: 「内力/真气/剑气」「筑基/渡劫/灵根/御剑」是**类型标志**，不是日常词；
#: 一侧满篇修仙、另一侧是市井，评者一眼就分得出来 ⇒ 与灵异/年代同级。
GENRE_STRONG = (
    "灵异克苏鲁（厉鬼复苏/收容/邪祟）",
    "规则怪谈（终焉之地/违规/通关）",
    "年代穿越（1980前后/改革开放）",
    "言情豪门（总裁/婚约/替身）",
    "古风武侠（江湖门派/内力招式）",
    "东方玄幻（修真/仙侠/御剑）",
)

#: 题材判定提示词 —— **通用模板，不含任何具体书名/人物/地名**。
_GENRE_JUDGE_SYSTEM = """你是中文网络小说的题材分类员。给你几段正文片段，逐段判断它属于哪一类故事。

可选类别（只能原样选一个）：
- 现代都市（市井生活流）：市井、底层生活、小店/街坊、现实向的犯罪或悬疑
- 现代都市异能（系统流/超能力）：系统、面板、觉醒、强化、超凡、异兽
- 年代穿越（1980前后/改革开放）：供销社、工厂、粮票、返城、下乡、万元
- 古风武侠（江湖门派/内力招式）：门派、镖局、内力、真气、朝廷、世子
- 东方玄幻（修真/仙侠/御剑）：宗门、筑基、渡劫、灵根、妖兽、御剑
- 灵异克苏鲁（厉鬼复苏/收容/邪祟）：厉鬼、复苏、收容所、凶宅、诡秘
- 规则怪谈（终焉之地/违规/通关）：规则、违规、通关、主办方、副本
- 言情豪门（总裁/婚约/替身）：总裁、闪婚、替身、白月光、千亿
- 其他（以上都不是，或看不出来）

判据：
1. 只看片段里的**世界观与场景**（年代、地点、力量体系、日常物件），**不看文笔好坏**。
2. 看不出来就选「其他」，**不要猜**。
3. 片段可能从段落中间开始，缺上下文 —— 按能判断到的写。

只输出 JSON 数组，每个片段一次，id 与输入一致：
[{"id": 1, "genre": "现代都市（市井生活流）"}]"""
#: 一次判多少个片段。片段约 400 字，4 个一批输入约 1600 字、输出很短。
#:
#: 为什么提到 12：题材判定是整条链上**最贵**的一步（实测一次调用 3~10 分钟），
#: 批越小 ⇒ 批数越多 ⇒ 整包跑不完。输出只是 `{"id": n, "genre": "..."}`，
#: 12 个也只有几百字 ⇒ 不会被 max_tokens 截断（截断会让整批判定消失，
#: ⇒ 判不出来 ⇒ 整包作废，见 `MaskingIncomplete`）。
GENRE_JUDGE_BATCH = 12

#: 题材判定缓存的版本号（判定逻辑升级 ⇒ 旧缓存整份作废）。
GENRE_CACHE_VERSION = 1


def _genre_cache_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _load_genre_cache(cache_path: Path | None) -> dict[str, str]:
    if not cache_path or not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict) or data.get("__v__") != GENRE_CACHE_VERSION:
        return {}
    got = data.get("genres")
    return dict(got) if isinstance(got, dict) else {}


def _save_genre_cache(cache_path: Path | None, genres: dict[str, str]) -> None:
    if not cache_path:
        return
    try:
        cache_path.write_text(
            json.dumps({"__v__": GENRE_CACHE_VERSION, "genres": genres},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 缓存失败不影响主流程
        pass


def _genre_judge_batch(provider, items: list[tuple[int, str]]) -> dict[int, str]:
    """判一批片段的题材。返回 ``{片段序号: 题材}``（判不出来的直接不出现）。

    ⚠️ **输入用 JSON 传，不要用「空行分段」**：正文里本来就有换行，
    用空行做分隔符会把片段切碎、编号冲掉 —— 实测因此让 5/90 段
    「没判出来」，整包被误判为作废（fail closed 把它暴露出来了，但这是一次误伤）。
    """
    payload = [{"id": sid, "text": text} for sid, text in items]
    user = ("以下是待分类的正文片段（JSON 数组，id 字段是编号）：\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n按 id 逐个给出 genre。")
    raw = ""
    for _ in range(2):
        try:
            raw = provider.chat(_GENRE_JUDGE_SYSTEM, user)
            break
        except Exception:  # noqa: BLE001 - 单批失败不影响其余批次
            continue
    m = re.search(r"\[.*\]", raw or "", re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[int, str] = {}
    for it in arr:
        if not isinstance(it, dict):
            continue
        try:
            sid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        g = str(it.get("genre") or "").strip()
        if not g:
            continue
        # 类别必须是闭集里的：**原样**匹配，或模型只写了类别的短名（「现代都市」）。
        # ⚠️ 短名有**歧义**（「现代都市」既是「市井生活流」的前缀，也是「都市异能」的前缀）
        # ⇒ 歧义时**不认**（当作没判出来），宁可 fail closed 也不要错分。
        exact = [c for c in GENRE_CLASSES if g == c]
        if exact:
            out[sid] = exact[0]
            continue
        pref = [c for c in GENRE_CLASSES if g.startswith(c.split("（")[0])]
        if pref and len({len(c.split("（")[0]) for c in pref}) == 1:
            out[sid] = pref[0]
    return out


def judge_genres(items: list[tuple[str, str]], provider, *,
                  cache_path: Path | None = None,
                  batch_size: int = GENRE_JUDGE_BATCH,
                  log=None) -> dict[str, str]:
    """给若干片段判题材。``items`` 是 ``[(键, 文本), ...]``。

    返回 ``{键: 题材}``；**判不出来的键不会出现在返回值里**（调用方要按
    「没判出来」保守处理 —— 题材闸门里等于这题作废）。

    ## 为什么要有这个函数

    词表口径实测太弱：2026-10-31 在 18 本本地语料上统计，「剑」「刀」「江湖」
    「系统」「掌柜」「伙计」这类词**在过半的书里都出现** ⇒ 按词判题材会把
    现代都市小说误判成古风武侠（9/18 判错）。模型读正文判题材才是权威口径。

    缓存按**文本 sha256**，与书无关：同一段文本换批次也复用。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    cache = _load_genre_cache(cache_path)
    if cache:
        _log(f"复用题材判定缓存 {len(cache)} 条")

    out: dict[str, str] = {}
    pending: list[tuple[str, str]] = []
    for key, text in items:
        ck = _genre_cache_key(text)
        if ck in cache:
            out[key] = cache[ck]
        else:
            pending.append((key, text))
    _log(f"待判题材 {len(pending)} 个（已缓存 {len(items) - len(pending)} 个）")

    nb = (len(pending) + batch_size - 1) // batch_size
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        # 片段序号用「批内序号」，回包时映射回 key
        numbered = [(n + 1, t) for n, (_k, t) in enumerate(batch)]
        got = _genre_judge_batch(provider, numbered)
        for n, (key, text) in enumerate(batch, 1):
            g = got.get(n)
            if not g:
                continue
            out[key] = g
            cache[_genre_cache_key(text)] = g
        _log(f"批 {i // batch_size + 1}/{nb} 判出 {len(got)}/{len(batch)} 个")
        _save_genre_cache(cache_path, cache)

    return out


#: **兼容题材**：两个题材**共享大量词汇**，评者分不出「是哪种」⇒ 算一致。
#:
#: 为什么要这一对：盲评包要「每题两侧同题材」，但**同一种世界观下的两个子类型**
#: 分不开 —— 「江湖/门派/剑/修行/术法」在武侠和修真文里都满地都是，
#: 评者看到「内力」和「渡劫」都会归成「古风仙侠」，
#: **没法用题材定位哪边是出版物**。
#:
#: ⚠️ 注意这里的判断标准是**非对称**而不是「题材相同」：
#: 泄露的本质是「一侧比另一侧更扎眼 ⇒ 评者能定位」，
#: 两侧同样扎眼（哪怕题材不同）并不泄露。
GENRE_COMPAT = {
    "古风武侠（江湖门派/内力招式）": "东方玄幻（修真/仙侠/御剑）",
    "东方玄幻（修真/仙侠/御剑）": "古风武侠（江湖门派/内力招式）",
}


#: 「强烈呈现某子题材」的门槛（每百字命中数）。
#:
#: 为什么要有这道：当**两侧都**强烈呈现同一个子题材时，
#: **评者没法用题材定位哪边是出版物**（两边一样扎眼）⇒ 不算泄露。
#: 这正是「泄露的本质是**非对称**」这条原则的落地：
#: 模型标签可能一边判「年代穿越」、一边判「都市市井」（模型也判不准），
#: 但只要两侧的**类型信号都拉满**，评者看到的仍是「两边都是同一个年代」。
#:
#: ⚠️ 门槛定得高（0.10/百字 ≈ 400 字里命中 40 次）⇒ 只有**类型词刷屏**
#: 的窗口才算「强烈」——一般的题材氛围不算。
GENRE_STRONG_DENSITY = 0.10


def genre_mismatch(a: str, b: str, labels: tuple[str, str] | None = None) -> tuple[bool, dict]:
    """两侧是不是**明显不是同一题材**。返回 ``(是否不一致, 证据)``。

    ``labels`` 传 LLM 判出来的题材（权威口径）时以它为准。三种情况：

    - 两边**都判得出来且一样** ⇒ 一致（两边都是「其他」也算：都没露题材指纹）
    - 两边**都判得出来但不一样** ⇒ 不一致
    - **一边看不出来、另一边题材明确** ⇒ **不能算一致**（保守起见当不一致）：
      一侧满篇题材术语、另一侧看不出题材，评者照样能靠题材分辨出来。

    ⚠️ 还要分清两种「没有标签」：
      - ``""`` = **没判出来**（批次失败/截断）⇒ fail closed，当不一致；
      - ``GENRE_UNKNOWN`` = 判出来了但看不出题材 ⇒ 可以和另一侧的「其他」配对。
    """
    if labels is not None:
        la, lb = labels
        ev = {"judged": {"A": la, "B": lb}, "source": "llm"}
        if not la or not lb:
            # 没判出来 ⇒ **不能算一致**（fail closed）
            return True, {**ev, "reason": "有一侧题材没判出来"}
        if la == lb:
            return False, {**ev, "reason": "两侧题材判得一样"}
        if GENRE_COMPAT.get(la) == lb or GENRE_COMPAT.get(lb) == la:
            # 同属一个世界观下的两个子类型（武侠↔玄幻）⇒ 评者分不出
            return False, {**ev, "reason": "同属兼容题材（武侠↔玄幻）：评者分不出是哪种古风",
                           "weak": True}
        # 模型标签不同，但**两侧都强烈呈现同一个子题材** ⇒ 不算泄露。
        # 理由：泄露的本质是**非对称**（一侧比另一侧更扎眼 ⇒ 评者能定位）；
        # 两侧类型信号都拉满 ⇒ 评者看到的仍是「两边同一个调子」，定位不了。
        pa, pb = genre_profile(a), genre_profile(b)
        da, db = genre_dominant(pa), genre_dominant(pb)
        if da and da == db and pa[da] >= GENRE_STRONG_DENSITY \
                and pb[db] >= GENRE_STRONG_DENSITY:
            return False, {**ev,
                           "reason": f"两侧都强烈呈现「{da}」：两边一样扎眼，评者定位不了",
                           "weak": True,
                           "profiles": {"A": round(pa[da], 3), "B": round(pb[db], 3)}}
        if GENRE_UNKNOWN in (la, lb):
            other = lb if la == GENRE_UNKNOWN else la
            if other in GENRE_STRONG:
                # 一侧满是强题材术语、另一侧看不出题材 ⇒ 评者能靠题材分辨出来
                return True, {**ev, "reason": "一侧是强题材、另一侧看不出来"}
            # 弱题材的一侧看不出来：只是词汇分布略有差别，不足以分辨来源
            return False, {**ev, "reason": "弱题材的一侧看不出来（记为弱题材标记）",
                           "weak": True}
        return True, {**ev, "reason": "两侧题材判得不一样"}
    d = genre_distance(a, b)
    pa, pb = genre_profile(a), genre_profile(b)
    return d > GENRE_MAX_DISTANCE, {
        "distance": round(d, 3),
        "dominant": {"A": genre_dominant(pa), "B": genre_dominant(pb)},
        "profile": {"A": {k: round(v, 3) for k, v in pa.items() if v > 0},
                    "B": {k: round(v, 3) for k, v in pb.items() if v > 0}},
        "source": "wordlist",
    }


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

