"""伏笔条目的**质量判据**：哪些条目其实是对话碎片，不该当悬念进提示词。

## 为什么要有这个

实测（2026-10-28，验收库 6 段真实正文 + 发布版生产代码）：
KG 抽取的「未填伏笔」**19 条里 14 条是对话碎片**
（`“你把我当什么？”`、`还是留在这里？`、`身后跟的是什么？`……）——
抽取器把**台词**当成了悬念。⇒ 它们照原样进了 system 提示的约束块：
白占位置，还可能让模型误以为那些是「要在后续慢慢推进的悬念」。

## 判据只判「像不像对话」，不判「是不是好悬念」

宁可**放过**（模型多看一条无害），不可**误杀**（误杀 = 模型丢了一条真约束）。
⇒ 只在**高置信**的对话特征上判不合格：

1. **含问号**（？/?）—— 疑问句是对话；
2. **含「冒号 + 引号」**（：“）—— 那是有人**说话**；
3. **以引号开头**（“「『）—— 引号里是台词。

其它一律放过 —— 包括「他一路没怎么说话。」这种**说不清**是不是悬念的
（放过 = 只多占一行；误杀 = 真约束丢了）。

## 两个使用点，一条规则

- **渲染层**（`generation/prompts.py`）：只过滤 **KG 抽取来的**伏笔，
  **用户自己在剧情圣经里写的伏笔一个字都不动**（那是权威）。
- **抽取层**（`kg/batch_worker.py`）：命中时**只留痕**（计数 + 警告日志），
  **不删、不改账本** —— 账本里的东西还在，只是提示词不再拿它当约束。
"""

from __future__ import annotations

from typing import Iterable

#: 以这些符号开头 ⇒ 引号里是台词。
_QUOTE_HEADS = ("“", "「", "『", "\"", "'")

#: 「冒号 + 引号」⇒ 有人在说话（中文/英文冒号都行）。
_SPEECH_MARKS = ("：“", ":\"", "：'", ":\"")

#: 疑问句标志。
_QUESTION_MARKS = ("？", "?")


def looks_like_dialogue_fragment(text: str | None) -> bool:
    """这条伏笔其实是一句**对话/台词**吗？（高置信才判 True）

    >>> looks_like_dialogue_fragment("“你把我当什么？”")
    True
    >>> looks_like_dialogue_fragment("那人弯腰看向脚边的磨刀石：“磨了做什么？")
    True
    >>> looks_like_dialogue_fragment("断刀的刀镡上刻着一个沈砚不认识的姓")
    False
    >>> looks_like_dialogue_fragment("周郎中留下一句「刀别离手」")
    False
    """
    s = (text or "").strip()
    if not s:
        return False
    if any(m in s for m in _QUESTION_MARKS):
        return True
    if any(m in s for m in _SPEECH_MARKS):
        return True
    if s.startswith(_QUOTE_HEADS):
        return True
    return False


def split_quality(texts: list[str]) -> tuple[list[str], list[str]]:
    """把一组伏笔条目分成 (保留, 疑似对话碎片)。"""
    keep: list[str] = []
    junk: list[str] = []
    for t in texts or []:
        (junk if looks_like_dialogue_fragment(t) else keep).append(t)
    return keep, junk


#: 章号最小合法值（章从 1 开始）。
_MIN_CHAPTER = 1


def plausible_payoff_chapter(planned, planted=None, text=None, *,
                             closed: bool = False) -> bool:
    """这个「计划回收章」说得通吗？

    判据是**与剧情无关的通用不变式**，不猜内容：

    1. **章号必须 ≥ 1** —— `0` 不存在（实测 97 条 active 里 **72 条是 0**）。
    2. **回收章必须晚于埋下章** —— 同一章回收等于没埋；
       早于埋下章是不可能的。`planted` 未知时只判第 1 条。
    3. **正文证据** —— 这个章号得**在正文里真的出现过**
       （「第 N 章」/「第 N 节」）。挡住「数值合法但凭空来」的值。

    ## `closed=True`：已收尾的行只判第 1 条

    `closed` = `paid_off`（已回收）/ `dropped`（已丢弃）——
    回收章记的是**当时的计划**，是**历史记录**：

    - `paid_off`：`pf == planted`（同章埋了又收）是**合法**的
      （实测 #91 `pf=1/planted=1`、#43 `pf=4/planted=4`）⇒ 不重新裁决；
    - `dropped`：线索被废弃、从未回收，没有「同章回收」这层语义。

    ⇒ 对 `closed` 行**只判「章号 ≥ 1」**（`0` 不是章号，别的照旧留着），
    不拿现在的正文去重新裁决历史。

    ## ⚠️ 为什么**没有**「回收章不能超过已写章数」这条上界

    想过加（`pf ≤ 已写段数`），量过之后**否掉了**：

    `planned_payoff_chapter` 是「**计划**回收章」——作者对**将来**的规划。
    「埋@3 → 计划第 60 章」是**合法且有用**的路线图信息（长篇里伏笔几十章后
    才收是常态）。用「已写章数」当上界会把这条功能直接杀掉：
    用户才写 9 章就想规划第 60 章回收，也会被判成非法。

    ⇒ 上界改用**证据**判：这个章号正文里说过没有。
    实测（发布版冒烟库 140 条）：生成正文里「第 N 章」**零命中**，
    而参考书语料 151 处命中 ⇒ **阳性对照自证**：匹配器能命中真章号，
    所以生成正文的零命中是**真阴性**。所有脏值（`0`/`50`/`30`/`12`）
    在正文里都没有任何证据。

    >>> plausible_payoff_chapter(0, 1)
    False
    >>> plausible_payoff_chapter(1, 1)
    False
    >>> plausible_payoff_chapter(60, 1)
    True
    >>> plausible_payoff_chapter(60, 1, text="（第一章。）")
    False
    >>> plausible_payoff_chapter(1, 1, text="（第一章。）")
    True
    >>> plausible_payoff_chapter(1, 1, closed=True)
    True
    >>> plausible_payoff_chapter(0, 1, closed=True)
    False
    >>> plausible_payoff_chapter(None, 1)
    False
    """
    if not isinstance(planned, int) or isinstance(planned, bool):
        return False
    if planned < _MIN_CHAPTER:
        return False
    if closed:
        # 历史记录：不拿现在的正文/埋下章去重新裁决它。
        return True
    if isinstance(planted, int) and not isinstance(planted, bool):
        if planned <= planted:
            return False
    if text is not None:
        # 正文证据：章号没在正文里出现过 ⇒ 不采信。
        # ⚠️ `text=""`（正文取不到）与 `text=None`（不做证据判）不同：
        # 空串 ⇒ 证据判**不放行**任何章号（宁可置空，也不让凭空来的值进账）。
        from app.kg.chapter_evidence import payoff_chapter_in_text

        if not payoff_chapter_in_text(planned, text):
            return False
    return True


def sanitize_payoff_chapter(planned, planted=None, text=None, *, closed: bool = False):
    """说得通就返回原值，说不通返回 None（**当作未知**，不猜一个数补上）。"""
    return planned if plausible_payoff_chapter(planned, planted, text, closed=closed) else None


# ====================================================================
# 近似变体：同一条线索的两种说法
# ====================================================================

#: 判定「近似变体」的相似度阈值（字符 bigram Jaccard）。
#:
#: ## 阈值是量出来的，不是拍的
#:
#: 拿账上 102 条真名字两两算相似度，高分对逐条看过：
#:
#: ```
#: 0.889  断刀刀镡上的陌生姓氏   ||  断刀刀镡上的陌生姓          ← 同一件事
#: 0.545  沈铁山临死前未说完的话 ||  沈铁山未说完的话            ← 同一件事
#: ---- 以下就是另一档了（有的同一件、有的两件，不能一刀切）----
#: 0.435  瘸腿男人、陈叔、周郎中与沈家在二十年前的旧事 || 周郎中与陈叔二十年前的旧事
#: 0.429  沈铁山未说完的话 || 沈铁山未说完的师承与断刀来历
#: 0.417  断刀刀鞘上的陌生刻痕 || 断刀刀镡上的陌生姓            ← 刻痕 ≠ 姓，两件事
#: ...
#: 0.278  沈铁山临死前未说完的话 || 沈铁山未说完的师承与断刀来历
#: 0.167  断刀上的‘萧’字 || 断刀上的“柳”字与柳家往事           ← 一字之差，两件事
#: ```
#:
#: ⇒ **0.435 与 0.545 之间是一段空档**（这一段里没有别的对），
#: 阈值取 **0.50**：落在空档正中，把两对「同一件事」收进来，
#: 把「刻痕 vs 姓」「萧 vs 柳」这类**两件事**挡在外面。
#:
#: 再往下（0.15~0.30）真同假混 ⇒ 宁可少报（放过），不可误报
#: （把两条线索说成一条，比漏报更坏）。
NEAR_VARIANT_THRESHOLD = 0.50


def _bigrams(text: str) -> set[str]:
    s = ''.join(ch for ch in (text or '') if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def name_similarity(a: str, b: str) -> float:
    """两个伏笔名的相似度（字符 bigram Jaccard，0~1）。

    >>> round(name_similarity("断刀刀镡上的陌生姓", "断刀刀镡上的陌生姓氏"), 3)
    0.889
    >>> name_similarity("断刀上的萧字", "断刀上的柳字与柳家往事") < 0.3
    True
    """
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def nearest_existing_name(name: str, existing: Iterable[str]
                          ) -> tuple[str, float] | None:
    """在已有名字里找与 `name` 最像的那条。

    返回 `(最像的名字, 相似度)`；没有一条达到 `NEAR_VARIANT_THRESHOLD` ⇒ 返回 `None`。
    """
    best: tuple[str, float] | None = None
    for other in existing or []:
        if not other or other == name:
            continue
        sim = name_similarity(name, other)
        if sim < NEAR_VARIANT_THRESHOLD:
            continue
        if best is None or sim > best[1]:
            best = (str(other), sim)
    return best
