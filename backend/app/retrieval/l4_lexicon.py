"""L4 技法词表的语料化推导（2026-11-14）。

## 为什么改

`catalog_paths.L4_LEXICON` 是手写词表：换一本书后，书里不用的词还在、书里
常用的说法又不在，L4 既不贴合也不覆盖。这里保留 15 个类目名与排序
（`L4_ORDER` / `L4_LABELS`），只把**关键词来源**改成蒸馏语料统计。

## 推导口径

1. 语料：蒸馏档案里所有会挂 L4 的维度（technique / global / structural）的
   规则正文；角色名/别名从档案 chunk_meta 汇总后排除。实测把「（证据：……）」
   一并纳入抽词比剥掉更稳（证据提供了本书特有的语义簇），因此默认保留。
2. 种子：旧词表中**本书真实出现**的词（live）。旧词只作冷启动先验，本书没
   出现的词会被自然淘汰。同一个词在多个类目里出现时保留全部归属（如
   「悬念」同属 infocontrol / tension），否则会把旧类目挤空。
3. 候选：jieba 词性标注抽 2–4 字**名词**，去掉停用词、专名（nr/ns/nt/nz）、
   边界虚词，以及「被更长词盖过」的碎片。只收名词是 2026-11-14 抽检后的
   收紧：动词/成语候选（「借题」「发挥」「影射」）会把潜台词卡成批带偏到
   action。jieba 是硬依赖，缺它直接报错，不静默退化成没有词表。
4. 选词：以种子标注且无并列的规则为弱标签，按类目 precision、log-odds、
   支持度选词，每类设上限；跑两轮自训练（用上一轮词表重标注再选）。
5. 计分：旧词（本书 live 的种子）权重 2、派生新词权重 1。新增词只补覆盖，
   不抢原本正确的主技法；`assign_l4` 必须用同一权重，否则标签口径不一致。
6. 校核：`derive_lexicon` 同时返回推导报告（标注条数、各类 live/新增词数），
   `--lexicon-only` 可只推导不写卡表；词表哈希写进 `catalog_version`，换书
   或调参都会生成新版本卡表，不覆盖旧文件。

## 换书

`python -m app.retrieval.build_cards --distill-id X` 自动按该档案重建词表；
`--lexicon-only` 先看词表与报告。无需手改任何词表。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from typing import Iterable

from app.retrieval.catalog_paths import (
    L4_LEGACY_ORDER,
    L4_LEXICON,
    L4_ORDER,
    strip_beat_prefix,
)

#: 推导口径版本。调参或改候选规则时 +1，让 catalog_version 跟着变。
L4_LEXICON_VERSION = "l4-lex-v2"

#: 2026-11-14 实测（distill 4/5，名词候选 + 旧词权重 2）：未细分 -5/-15，
#: 主类目变化 0.43% / 0.89%；80 条人工抽检新词表 64% vs 旧词表 38%。
#: 再放宽（动词候选、低 precision）会明显增加误配（subtext→action、
#: dialogue→psych），因此不要为了压「未细分」下调这些阈值。
DEFAULT_MAX_TERMS = 12
DEFAULT_PRECISION = 0.90
DEFAULT_LOG_ODDS = 3.0
DEFAULT_ITERS = 2
DEFAULT_MIN_RATIO = 0.002

#: 旧词权重 / 新增词权重 = 2:1。2026-11-14 人工抽检：等权时新增词会把
#: 场景模板、潜台词卡误抢到 action / dialogue；降权后错误翻转明显减少，
#: 又保留新词对「未细分」卡的兜底覆盖。设为 0 则退化为「只做兜底填充」。
DEFAULT_SEED_WEIGHT = 2

_EVIDENCE_BLOCK = re.compile(r"[（(]\s*证据\s*[:：]\s*(.*?)[）)]", re.S)
_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")

#: 写作说明里的通用词，不能指向某一技法。
STOPWORDS: frozenset[str] = frozenset({
    "使用", "采用", "通过", "进行", "需要", "可以", "能够", "尽量", "避免",
    "保持", "形成", "造成", "出现", "描写", "描述", "表达", "呈现", "体现",
    "突出", "强调", "增加", "减少", "加强", "制造", "营造", "达到", "效果",
    "作用", "方式", "方法", "情况", "时候", "过程", "内容", "读者", "人物",
    "角色", "情节", "故事", "画面", "部分", "同时", "以及", "或者", "因为",
    "所以", "但是", "如果", "这样", "这种", "一个", "一种", "没有", "就是",
    "不是", "而是", "已经", "还是", "只是", "非常", "比较", "更加", "最为",
    "一定", "必须", "直接", "间接", "具体", "整体", "细节", "重点", "主要",
    "此处", "这里", "然后", "于是", "因此", "其中", "用来", "作为", "成为",
    "基本", "明显", "通常", "往往", "反而", "总是", "一直", "之外", "以内",
    "以外", "留给", "放进", "展现", "优先", "活动", "发展", "现象", "答案",
    "名称", "单位", "很强", "发表", "影响", "丰富", "灵活", "严格", "场合",
    "没事", "好好", "明明", "不容", "之事", "别人", "提起", "重要", "关键",
})

#: 出现在词首/词尾的虚词，命中的多半是跨词碎片。
EDGE_CHARS: frozenset[str] = frozenset(
    "的地得了着是和与及或而就也都更又还之其以为于从到将则即并但如若因"
    "所被把让给上下里中内外前后时个些这那"
)

_OLD_TERMS = frozenset(w for ws in L4_LEXICON.values() for w in ws)
_OLD_CLASSES: dict[str, set[str]] = defaultdict(set)
for _cls, _words in L4_LEXICON.items():
    for _w in _words:
        _OLD_CLASSES[_w].add(_cls)

try:  # 推导依赖：jieba（纯 Python，见 requirements.txt）
    import logging

    import jieba
    import jieba.posseg as _pseg

    logging.getLogger("jieba").setLevel(logging.WARNING)
    _HAS_JIEBA = True
except Exception:  # noqa: BLE001 - 延迟到真正推导时再报错
    _pseg = None  # type: ignore[assignment]
    _HAS_JIEBA = False


def _require_jieba() -> None:
    """缺依赖时明确报错，不允许静默降级成没有词表的建卡。"""
    if not _HAS_JIEBA:
        raise RuntimeError(
            "L4 词表推导需要 jieba：请在 backend/ 下执行 "
            "`pip install -r requirements.txt`（jieba==0.42.1）后重试。"
        )


def rule_body(text: str) -> str:
    """剥掉规则正文末尾的「（证据：……）」及单独回传的证据行。"""
    raw = text or ""
    evidence = [m.strip() for m in _EVIDENCE_BLOCK.findall(raw)]
    body = _EVIDENCE_BLOCK.sub("", raw)
    if evidence:
        kept = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(ev and (stripped in ev or ev.startswith(stripped[:12]))
                   for ev in evidence):
                continue
            kept.append(line)
        body = "\n".join(kept)
    return body.strip()


def _candidates(text: str) -> set[str]:
    """规则正文 → 候选词集合（2–4 字，jieba 词性标注）。"""
    _require_jieba()
    out = set()
    for word, flag in _pseg.cut(text or ""):
        word = word.strip()
        if not (2 <= len(word) <= 4) or word in STOPWORDS:
            continue
        if not _CJK_RE.match(word):
            continue
        if flag in ("nr", "ns", "nt", "nz") or flag.startswith("nr"):
            continue
        # 只收名词性候选：技法关键词基本是名词（悬念/全景/站位/敬称）。
        # 动词/成语候选会跨类目误伤——「借题发挥」被切成「借题」「发挥」后
        # 从潜台词卡学到 action，导致 subtext→action 成批翻错（2026-11-14）。
        if not flag.startswith("n"):
            continue
        out.add(word)
    return out


def _score(text: str, lexicon: dict[str, Iterable[str]],
           seed: dict[str, Iterable[str]] | None = None,
           seed_weight: int = DEFAULT_SEED_WEIGHT) -> dict[str, int]:
    """与 `catalog_paths.assign_l4` 同一口径：子串计数（旧词加权）。"""
    scores: dict[str, int] = {}
    for key in L4_ORDER:
        seed_set = set(seed.get(key, ())) if seed else set()
        n = 0
        for word in lexicon.get(key, ()):
            n += text.count(word) * (seed_weight if word in seed_set else 1)
        if n:
            scores[key] = n
    return scores


def _ranked(scores: dict[str, int]) -> list[tuple[str, int]]:
    legacy = {k: v for k, v in scores.items() if k in L4_LEGACY_ORDER}
    pool = legacy or scores
    return sorted(pool.items(), key=lambda kv: (-kv[1], L4_ORDER.index(kv[0])))


def _labels(texts: list[str], lexicon: dict[str, tuple[str, ...]],
            seed: dict[str, Iterable[str]] | None = None,
            seed_weight: int = DEFAULT_SEED_WEIGHT) -> list[str | None]:
    """弱标签：只取无并列的主类目；并列视为判不了。"""
    out: list[str | None] = []
    for text in texts:
        ranked = _ranked(_score(text, lexicon, seed, seed_weight))
        if not ranked:
            out.append(None)
        elif len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            out.append(None)
        else:
            out.append(ranked[0][0])
    return out


def derive_lexicon(
    texts: Iterable[str],
    *,
    derive_texts: Iterable[str] | None = None,
    exclude: Iterable[str] = (),
    seed: dict[str, Iterable[str]] | None = None,
    max_terms: int = DEFAULT_MAX_TERMS,
    precision: float = DEFAULT_PRECISION,
    log_odds: float = DEFAULT_LOG_ODDS,
    iters: int = DEFAULT_ITERS,
    min_ratio: float = DEFAULT_MIN_RATIO,
    seed_weight: int = DEFAULT_SEED_WEIGHT,
) -> tuple[dict[str, tuple[str, ...]], dict]:
    """从蒸馏语料推导 L4 词表。

    Args:
        texts: 用于判定类目归属的规则正文（生产上就是卡面 `text`）。
        derive_texts: 用于抽候选词的文本，默认同 `texts`；生产上用剥证据的正文。
        exclude: 需要排除的专名（角色名/别名）。
        seed: 冷启动词表，默认 `L4_LEXICON`；换书后可传上一份派生词表。
        max_terms: 每个类目最多派生多少个新词。
        precision: 候选词在本类目弱标签里的纯度下限。
        log_odds: 类目 vs 其余语料的 log-odds 下限。
        iters: 自训练轮数。
        min_ratio: 最小支持度占该类目标注量的比例。
        seed_weight: 旧词（seed live）相对新增词的计分权重，默认 2。生产上
            `assign_l4` 必须用同一权重，否则自训练标签与最终分类口径不一致。

    Returns:
        (lexicon, report)。lexicon 的每类 = live 旧词 + 派生新词（去重、保序）。
    """
    seed_lex = {c: tuple(seed.get(c, ())) if seed else L4_LEXICON[c] for c in L4_ORDER}
    cls_texts = [strip_beat_prefix(t or "") for t in texts]
    if derive_texts is None:
        # 默认用卡面原文（规则 + 证据）抽词：证据是本书特有的语义簇，
        # 去掉后词表会退化成通用写作词，反而把 action/psych 抢乱。
        ext_texts = cls_texts
    else:
        ext_texts = [strip_beat_prefix(t or "") for t in derive_texts]
    if len(ext_texts) != len(cls_texts):
        ext_texts = ext_texts[:len(cls_texts)]
    exclude_set = set(exclude or ())

    seed_live = _live_seed_with(seed_lex, cls_texts)
    lex = {c: list(v) for c, v in seed_live.items()}
    per_class: Counter[str] = Counter()
    labeled = 0

    for _ in range(max(1, iters)):
        labels = _labels(cls_texts, {c: tuple(v) for c, v in lex.items()}, seed_live,
                         seed_weight)
        per_class = Counter(x for x in labels if x)
        labeled = sum(per_class.values())
        df: dict[str, Counter[str]] = defaultdict(Counter)
        gdf: Counter[str] = Counter()
        for body, ext, cls in zip(cls_texts, ext_texts, labels):
            if not cls:
                continue
            for term in _candidates(ext):
                if term in _OLD_TERMS or term in exclude_set:
                    continue
                df[term][cls] += 1
                gdf[term] += 1

        # 碎片过滤：更长的候选词 df 相近，说明当前词只是它的一部分。
        dominant: Counter[str] = Counter()
        for word, freq in gdf.items():
            if len(word) < 3 or freq < 3:
                continue
            for size in range(2, len(word)):
                for start in range(0, len(word) - size + 1):
                    sub = word[start:start + size]
                    if freq > dominant[sub]:
                        dominant[sub] = freq

        new_lex: dict[str, list[str]] = {}
        for cls in L4_ORDER:
            n_cls = per_class[cls]
            n_other = max(1, labeled - n_cls)
            need = max(3, int(min_ratio * max(1, n_cls)))
            scored: list[tuple[float, int, str]] = []
            for term, by_cls in df.items():
                hit = by_cls.get(cls, 0)
                if hit < need:
                    continue
                if term[0] in EDGE_CHARS or term[-1] in EDGE_CHARS:
                    continue
                if dominant.get(term, 0) >= 0.8 * gdf[term]:
                    continue
                other = gdf[term] - hit
                purity = hit / gdf[term]
                if purity < precision:
                    continue
                rest = n_cls - hit
                neither = n_other - other
                if rest < 0 or neither < 0:
                    continue
                lo = (math.log((hit + 0.5) / (rest + 0.5))
                      - math.log((other + 0.5) / (neither + 0.5)))
                if lo < log_odds:
                    continue
                scored.append((lo, hit, term))
            scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
            picked = [t for _, _, t in scored[:max_terms]]
            new_lex[cls] = list(seed_live[cls]) + [t for t in picked if t not in seed_live[cls]]
        lex = new_lex

    lexicon = {c: tuple(dict.fromkeys(lex[c])) for c in L4_ORDER}
    report = {
        "version": L4_LEXICON_VERSION,
        "params": {
            "max_terms": max_terms, "precision": precision, "log_odds": log_odds,
            "iters": iters, "min_ratio": min_ratio, "seed_weight": seed_weight,
        },
        "tokenizer": "jieba-posseg",
        "texts": len(cls_texts),
        "labeled": labeled,
        "labeled_by_class": {c: per_class[c] for c in L4_ORDER if per_class[c]},
        "terms": {c: len(lexicon[c]) for c in L4_ORDER},
        "seed_live": {c: len(seed_live[c]) for c in L4_ORDER},
        # 实际生效的旧词（本书 live 子集），build_cards 用它给旧词加权。
        "seed_terms": {c: list(seed_live[c]) for c in L4_ORDER},
        "added": {c: len([w for w in lexicon[c] if w not in _OLD_TERMS]) for c in L4_ORDER},
        "excluded_terms": len(exclude_set),
    }
    return lexicon, report


def _live_seed_with(seed_lex: dict[str, Iterable[str]],
                    texts: list[str]) -> dict[str, list[str]]:
    """按给定种子词表算 live（本书真实出现的词），保留跨类目重复归属。"""
    classes_by_word: dict[str, set[str]] = defaultdict(set)
    for cls in L4_ORDER:
        for word in seed_lex.get(cls, ()):
            classes_by_word[word].add(cls)
    live: dict[str, set[str]] = {c: set() for c in L4_ORDER}
    for word, classes in classes_by_word.items():
        if any(word in text for text in texts):
            for cls in classes:
                live[cls].add(word)
    return {c: [w for w in seed_lex.get(c, ()) if w in live[c]] for c in L4_ORDER}


def lexicon_hash(lexicon: dict[str, Iterable[str]]) -> str:
    payload = {c: list(lexicon.get(c, ())) for c in L4_ORDER}
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]


def lexicon_added(lexicon: dict[str, Iterable[str]]) -> dict[str, list[str]]:
    """各类目相对旧词表新增的词（给 --lexicon-only 核对用）。"""
    return {c: [w for w in lexicon.get(c, ()) if w not in _OLD_TERMS] for c in L4_ORDER}
