"""引导语补写：给**裸台词段**补一句动作/神态叙述，把它变成「叙述 + 台词同段」。

## 为什么需要它（实测，2026-10-27）

机械校正（`form_merge.merge_bare_dialogue`）只改段落边界，能把 9 段真实产物的
「纯台词段占比」从 **71.5% 拉到 42.2%** —— 但**到不了参考书的 22.7%**：
「只动段落边界」这条路本身的上限就在那儿（生成物那边理论上限 47.6%）。

剩下的差距只能靠**新增内容**补：给裸台词段加一句引导语。

## 与已排除的 beat 插入有什么不同（这是本模块存在的理由）

`beat_insert` 也写动作/神态叙述，但它把叙述作为**独立的一段**加在连续对白前 ⇒
目标指标（A 类占比）**一点没降**。真跑的量-效曲线显示插到真书 5 倍密度，
A 类也只降到 35%，采纳率还随量下降 ⇒ 那条路被否掉（`style_beat_insert`，默认关）。

本模块把引导语**并进台词段本身**（冒号形态 `叙述：“台词”`）⇒
**裸台词段直接变成混合段**，指标当场下降。

实测（9 段真实产物，MiniMax-M3.1-Flash，effort=low）：

| | 纯台词段占比（汇总口径） |
|---|---|
| 校正前 | 71.5% |
| 机械合并后 | 42.2% |
| **加引导语后** | **21.3%**（参考书 22.7%，目标 18.7%） |

采纳率 90%（115/128），9 段共 109 秒，正文字数 +7.2%。

## 判据（六道，全过才采纳）

1. **长度** 10~30 字（真书引导语 p25~p75 = 7~27 字）；
2. **不含引号** + **像叙述**（默认是叙述，只在明确是说理时才拒）；
3. **不含新的人名/地名/物件**、**不报数**（不能凭空造事实）；
4. **不与已采纳的引导语重复**（2-gram Jaccard ≥ 0.40 或字符集 Jaccard ≥ 0.60）；
5. **不与上下文局部重复**（滑动窗口 2-gram Jaccard ≥ 0.25）——
   整段 Jaccard 会把短句跟长段比，相似度被稀释 ⇒ 漏检（实测漏掉 10/14）；
6. **不与本段自身重复**（引导语 vs 本段引号外，2-gram Jaccard ≥ 0.20）——
   「管家点头，…：管家点头：“…”」这种硬伤就是漏在这里（实测 5/115）。

## 失败策略

任何一道不过 → **这一处不补**（其余照补）。补写失败绝不能影响正文。
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

from app.style.beat_insert import (  # noqa: F401  （复用已验证的判据实现）
    _clean_beat,
    _find_duplicate,
    _looks_like_narration,
    _split_declared_nouns,
    _too_similar_to_context,
    check_no_new_facts,
)
from app.style.form_merge import _has_quote, is_bare_paragraph

#: 引导语长度区间（真书引号前叙述 p25~p75 = 7~27 字）。
LEAD_IN_MIN_CHARS = 10
LEAD_IN_MAX_CHARS = 30

#: 一次最多补几处（防止模型调用失控）。
MAX_LEAD_INS = 24

#: 并发调用数。0 = 不并发（串行）。
#:
#: 串行实测每段约 73 秒（9 段 661 秒）—— 慢到不能默认开。
#: provider 用 `urllib`（**没有共享 session**）⇒ 多线程安全；
#: MiniMax-M3.1 的实测安全并发是 15 ⇒ 这里取 6（留余量）。
LEAD_IN_CONCURRENCY = 6

#: 插入的模型调用用**低温**（「变换」不是「创作」，见 `beat_insert`）。
LEAD_IN_TEMPERATURE = 0.2

#: 与**本段自身**（引号外）的相似度到这个值就拒。
#:
#: 实测 115 条采纳的引导语：阈值 0.20 命中 5 条，**逐条看全是真重复**
#: （「管家点头，…」对「管家点头：…」这种）。
#: 再高（0.25）就漏掉了「管家点头」那条（0.231）。
_SELF_DUP_JACCARD = 0.20

#: 与**上下文局部**（滑动窗口）的相似度到这个值就拒。
#:
#: 为什么不用整段 Jaccard：引导语短（~14 字）、上下文段长（~60 字），
#: 整段比会把相似度稀释掉 —— 实测同一批数据上，整段 Jaccard 只抓到 2 条，
#: 滑动窗口抓到 14 条。
_CTX_WINDOW_JACCARD = 0.25

_STRIP_PUNCT = "：:，。！？…、；; \t　"


def _cjk(text: str) -> str:
    return "".join(ch for ch in (text or "") if "一" <= ch <= "鿿")


def _bigrams(text: str) -> set[str]:
    s = _cjk(text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _bigram_jaccard(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _max_window_jaccard(lead: str, context: str) -> float:
    """短引导语 vs 长上下文段 —— 取**最像的一段窗口**的相似度。

    为什么不能整段比：引导语 14 字、上下文 60 字 ⇒ 整段 Jaccard 被稀释，
    「他揉着鼻梁的手微微一顿」对着上一段「他揉了下鼻梁」整段只有 0.09，
    滑动窗口却是 0.29 —— 前者会漏检这种「紧接着重复」。

    ## 为什么比的是上下文的**引号外**部分

    引导语是叙述，它要跟**叙述**竞争，不跟台词竞争。
    拿引号里的台词一起算会把相似度稀释掉：
    「徐凤年揉了下鼻梁：“得看什么账。”」整段比是 0.235（漏检），
    只比引号外的「徐凤年揉了下鼻梁」是 0.286（抓到）。
    """
    if not lead or not context:
        return 0.0
    # 比的是引号外的叙述；引号外为空（整段都是台词）才退回整段
    s = _cjk(_outside_of_paragraph(context)) or _cjk(context)
    n = len(_cjk(lead))
    if n < 2 or len(s) < n:
        return _bigram_jaccard(lead, s)
    best = 0.0
    for i in range(len(s) - n + 1):
        j = _bigram_jaccard(lead, s[i:i + n])
        if j > best:
            best = j
            if best >= _CTX_WINDOW_JACCARD:
                break
    return best


def _outside_of_paragraph(para: str) -> str:
    """段里引号外的内容（用来查「引导语是不是把本段开头又说了一遍」）。"""
    out = para or ""
    for o, c in (("\u201c", "\u201d"), ("\u300c", "\u300d"), ("\u300e", "\u300f")):
        while o in out and c in out:
            a = out.index(o)
            b = out.find(c, a)
            if b < 0:
                out = out.replace(o, "")
            else:
                out = out[:a] + out[b + 1:]
    return out.strip(_STRIP_PUNCT)


def lead_in_system_prompt() -> str:
    return (
        "你是一位中文小说编辑，只负责为一句台词补一处**动作/神态叙述**"
        "（引导语），让读者看得见说话人的动作与神态。"
    )


def lead_in_user_prompt(before: str, dialogue: str, after: str) -> str:
    """构造补引导语的提示。

    关键约束（都来自真书实测 + 本项目真跑）：
    - 长度 **10~30 字**（这一本书的作者习惯是十几字）；
    - 只写**动作/神态/环境**，不写心理活动；
    - **不引号**（不写台词）+ **像叙述**；
    - **不出现新的人名/地名/物件**（不能凭空造事实）；
    - **不报数**；
    - **主语**：能确定是谁在说话就**点名** —— 这一本书的作者习惯是点名
      （实测参考书前 40 万字：混合段的引导语里 **70% 带名字**，只有 1% 用「他/她」）；
      **实在拿不准**才用「他/她」，**不要乱安名字**；
    - **不要重复上文刚写过的动作**（同一个动作不要写两遍）。
    """
    return f"""给下面这句台词补一句**引导语** —— 读者要能看见说话人的动作或神态。

要求：
1. 长度 **{LEAD_IN_MIN_CHARS} 到 {LEAD_IN_MAX_CHARS} 个字**（这一本书的作者习惯是十几字）；
2. 只写**动作/神态/环境**（如「他低头看了看桌上的账册」「栏里的青灰马忽然扬头」）；
   **不写心理活动**、不写评价；
3. **不要加引号**，不要写任何人的台词；
4. **不出现新的人名、地名、物件** —— 只用上文里已有的；
5. **不报数** —— 不要出现任何数量（几匹、十二口、多远这类一律不要）；
6. **主语**：能确定是谁在说话就**点名**（这一本书的作者习惯是点名）；
   实在拿不准才用「他/她」，**不要乱安名字**；
7. **不要重复上文刚写过的动作** —— 同一个动作不要写两遍；
8. 语气与上下文一致，不要突然文绉绉。

只输出这一句引导语，不要解释，不要加标题。
如果句子里用到了人名/地名/物件的名称，在句子**后面**另起一行写
`专名：甲、乙`（没有就不用写这一行）。

【上文】
{before}

【这句台词】
{dialogue}

【下文】
{after}"""


def pick_lead_in_slots(
    paragraphs: Sequence[str],
    target_ratio: float,
    *,
    max_slots: int = MAX_LEAD_INS,
) -> list[int]:
    """挑哪些裸台词段要补引导语。返回段落下标。

    ## 怎么挑

    1. 先算还差几段：裸台词段 n、含引号段 q ⇒ 目标裸台词段数 = target×q，
       差多少就补多少；
    2. **均匀摊开**（每隔几个挑一个）—— 参考书里裸台词段是**散着**的
       （B→B 只占 176/2662），全挤在一处会像补过。
    """
    ps = list(paragraphs)
    quote_n = sum(1 for p in ps if _has_quote(p))
    bare = [i for i, p in enumerate(ps) if is_bare_paragraph(p)]
    if not quote_n or not bare:
        return []
    need = len(bare) - int(round(target_ratio * quote_n))
    need = max(0, min(need, len(bare), max_slots))
    if need <= 0:
        return []
    step = max(1, len(bare) // need)
    return bare[::step][:need]


def insert_lead_ins(
    paragraphs: Sequence[str],
    provider,
    *,
    target_ratio: float,
    proper_nouns: set[str] | None = None,
    max_lead_ins: int = MAX_LEAD_INS,
    concurrency: int = LEAD_IN_CONCURRENCY,
) -> tuple[list[str], dict]:
    """给裸台词段补引导语。返回 (新段落列表, 诊断)。

    ## 流程

    1. `pick_lead_in_slots` 选点（纯机械）；
    2. 并发调模型写引导语（低温）；
    3. **逐条验收**（六道判据，见模块 docstring）；
    4. 通过的并进那一段（冒号形态 `引导语：“台词”`）。

    ## 为什么要并发，以及为什么验收仍然按顺序

    一次要写十几句引导语，串行下来每段约 73 秒（实测 9 段 661 秒）——
    并发 6 之后降到约 15 秒。provider 用的是 `urllib`（**没有共享 session**）
    ⇒ 多线程安全；MiniMax-M3.1 的实测安全并发是 15。

    但**验收必须按槽位顺序**做：判据 4（不与已补的引导语重复）依赖
    「已采纳列表」的顺序，若并发返回顺序不定，同一份输入的判定就会飘。
    ⇒ 调用并发、判定串行。

    ## 失败策略

    - 某处写不出合格引导语 → **跳过这一处**（不影响别的）；
    - 全部失败 → 原文返回，诊断写明。
    """
    ps = list(paragraphs)
    info = {"applied": False, "inserted": 0, "slots": 0, "rejected": []}
    if not ps:
        return ps, info

    slots = pick_lead_in_slots(ps, target_ratio, max_slots=max_lead_ins)
    info["slots"] = len(slots)
    if not slots:
        info["reason"] = "不需要补（已经在目标附近）"
        return ps, info

    # ── 1) 并发调用 ──────────────────────────────────────────────
    def _call(idx: int) -> tuple[int, str | None, str | None]:
        before = ps[idx - 1] if idx > 0 else ""
        after = ps[idx + 1] if idx + 1 < len(ps) else ""
        dialogue = ps[idx]
        try:
            raw = provider.chat(
                lead_in_system_prompt(),
                lead_in_user_prompt(before, dialogue, after),
                temperature=LEAD_IN_TEMPERATURE,
            )
        except Exception as exc:  # noqa: BLE001 —— 一处失败不该毁掉整段
            logging.getLogger(__name__).warning("引导语补写失败: %s", exc)
            return idx, None, f"调用失败 {type(exc).__name__}"
        return idx, raw, None

    results: dict[int, tuple[str | None, str | None]] = {}
    if concurrency > 1 and len(slots) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(concurrency, len(slots))) as pool:
            for idx, raw, err in pool.map(_call, slots):
                results[idx] = (raw, err)
    else:
        for idx in slots:
            i2, raw, err = _call(idx)
            results[i2] = (raw, err)

    # ── 2) 验收（**按槽位顺序**，保证判定确定）──────────────────
    accepted: list[tuple[int, str]] = []
    for idx in slots:
        raw, err = results.get(idx, (None, "没跑"))
        if err or raw is None:
            info["rejected"].append({"index": idx, "reason": err or "没跑"})
            continue
        before = ps[idx - 1] if idx > 0 else ""
        after = ps[idx + 1] if idx + 1 < len(ps) else ""
        dialogue = ps[idx]
        lead, declared = _split_declared_nouns(raw)
        lead = _clean_beat(raw)
        n_cjk = sum(1 for ch in lead if "一" <= ch <= "鿿")

        # 判据 1：长度
        if not (LEAD_IN_MIN_CHARS <= n_cjk <= LEAD_IN_MAX_CHARS):
            info["rejected"].append({"index": idx,
                                     "reason": f"长度 {n_cjk} 字不在 {LEAD_IN_MIN_CHARS}~{LEAD_IN_MAX_CHARS}",
                                     "text": lead[:40]})
            continue
        # 判据 2：无引号 + 像叙述
        if not _looks_like_narration(lead):
            info["rejected"].append({"index": idx, "reason": "不像叙述或含引号",
                                     "text": lead[:40]})
            continue
        # 判据 3：不能造事实
        new_words, degraded = check_no_new_facts(
            lead, f"{before}\n{dialogue}\n{after}\n" + "\n".join(ps),
            proper_nouns=proper_nouns, declared_nouns=declared,
        )
        if degraded:
            info["degraded"] = True
        if new_words:
            info["rejected"].append({"index": idx,
                                     "reason": f"可能造事实 {new_words[:3]}",
                                     "text": lead[:40]})
            continue
        # 判据 4：不能跟已采纳的引导语重复
        dup = _find_duplicate(lead, [b for _i, b in accepted])
        if dup:
            info["rejected"].append({"index": idx, "reason": f"与已补的一句重复（{dup}）",
                                     "text": lead[:40]})
            continue
        # 判据 5：不能跟上下文局部重复
        ctx_jac = max(_max_window_jaccard(lead, before), _max_window_jaccard(lead, after))
        if ctx_jac >= _CTX_WINDOW_JACCARD:
            info["rejected"].append({"index": idx,
                                     "reason": f"与上下文重复（相似度 {ctx_jac:.2f}）",
                                     "text": lead[:40]})
            continue
        # 判据 6：不能跟**本段自身**重复
        self_jac = _bigram_jaccard(lead, _outside_of_paragraph(dialogue))
        if self_jac >= _SELF_DUP_JACCARD:
            info["rejected"].append({"index": idx,
                                     "reason": f"与本段重复（相似度 {self_jac:.2f}）",
                                     "text": lead[:40]})
            continue

        accepted.append((idx, lead))

    if not accepted:
        info["reason"] = "没有一处通过验收"
        return ps, info

    out = list(ps)
    for idx, lead in accepted:
        core = lead.rstrip("。；;，,")
        out[idx] = core + "：" + ps[idx]
    info.update({
        "applied": True,
        "inserted": len(accepted),
        "leads": [{"index": i, "lead": l} for i, l in accepted],
    })
    logging.getLogger(__name__).info(
        "引导语补写：%d 处候选，%d 处通过验收", len(slots), len(accepted),
    )
    return out, info
