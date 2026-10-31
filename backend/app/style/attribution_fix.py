"""归属校正：把对白的「说话人归属」掰到**参考书自己的写法**。

## 为什么要有这一步（实测，不是推测）

参考书《魔道祖师》**76.6%** 的对白带说话人归属（`X道：“…”`），
我们的产出只有 **3.2%** —— 提示层加了一句口径之后升到 **14.2%**，
**还差 5 倍**。

⇒ 盲评里「出现『X道：“…”』的那侧 = 人类侧」这条规则**精确率 100%**
⇒ 这是**泄露面**，不修就出不了能用的盲评包。

## 为什么必须是**独立改写**，不是提示词

`app/style/form.py` 的实测结论：提示层**挪不动**对白形态（四轮提示层实验
全部无效或更差），而**事后改写能改**（实测 Flash 改写把纯台词段 42% → 6%）。

归属位置是对白形态的一个维度 ⇒ 走同一条路：**低温的第二次改写 + 四道判据**。

## 尺子 = 参考书自己

目标不是拍的，是**参考书自己的**「每句对白的归属率」与**主导形态**
（`X道：“…”` / `X说：“…”` / `“…。”X说`）。
换一本书 ⇒ 目标自动跟着换 ⇒ **通用机制**，不是写死的「一律用道」。

## 四道判据（与 `form.py` 同构）

1. **对白内容守恒** —— 字符多重集口径（对「合并/拆分」免疫，见 `form.py` 的说明）；
2. **字数不失控**（归属改写主要在「挪位置」，不该大幅变长）；
3. **归属率必须向目标靠近**（不靠近 ⇒ 不采纳）；
4. **不许过冲**（矫枉过正 = 把一种偏置换成另一种）。

## 失败策略

任何一道判据不过 ⇒ **原样返回**（保留原文，不糊弄）。
校正失败绝不能影响正文。
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

from app.style.attribution import (
    attribution_convention,
    attribution_profile,
)

logger = logging.getLogger(__name__)

#: 一句对白（含引号）。**测率与挑实例都用这一把尺**。
QUOTE_RE = re.compile(r"[“「『][^”」』]{0,400}[”」』]")

#: 校正调用的温度。这是「变换」不是「创作」⇒ 与 `form.py` 同一档。
ATTRIBUTION_TEMPERATURE = 0.2

#: 归属率的**过冲**上限（绝对百分点）。
#:
#: 为什么是 0.25 而不是 `form.py` 那样的 0.08：
#: 归属率是「有多少句对白带归属」，模型改写时**几乎总是拉满**（把每句都加上
#: `X道：“…”`）—— 参考书 76.6% 时改完是 100%，冲过去 **23 个百分点**。
#: 按 0.08 判 ⇒ 实测会**把唯一能用的改写全部拒掉**（采纳率 0）。
#: ⇒ 过冲容差按「实际会冲多远」定：**0.25 个百分点**。
#: 仍然拦得住真正的矫枉过正（参考书 40% 时改到 71% ⇒ 冲了 31 点 ⇒ 拒）。
MAX_OVERSHOOT = 0.25

#: 字数增长上限（绝对 + 相对）。归属改写是「挪位置」，不该大幅变长。
MAX_LEN_GROWTH_ABS = 60
MAX_LEN_GROWTH_RATIO = 0.20

#: 校正时给的**正确实例**条数。
EXAMPLE_LIMIT = 3

#: 归属率低于这个数就**不值得**花一次模型调用（参考书自己都几乎没有）。
MIN_RATE_TO_CORRECT = 0.05

#: 离目标差这么远才值得校正（差得不多就别动）。
MIN_GAP_TO_CORRECT = 0.20

#: 文本被截断 / 过短时不做校正。
MIN_CHARS = 200

#: 一次改写**一段**（按段落切，单次送模型的字数上限）。
#:
#: 为什么必须切：实测一次改写 2840 字时，模型返回**空输出**
#: （reasoning 吃光了 token 预算 —— 与主生成同一个症状）。
#: 切成 ~1000 字一段后，同样的预算**绰绰有余**（实测 4/4 采纳）。
#:
#: 为什么按段落切、不按字数硬切：段落是句子/对白的边界，
#: 切断一个对白会让模型看到半句话 ⇒ 改写质量塌掉。
CHUNK_TARGET_CHARS = 1000
CHUNK_MAX_CHARS = 1600

#: 单段改写的**重试**次数（空输出时重试 —— 空输出是**偶发**，不是必然）。
CHUNK_RETRIES = 2


def _split_chunks(text: str) -> list[str]:
    """按段落把正文切成若干段（每段 ≤ `CHUNK_MAX_CHARS` 字）。"""
    paras = [p for p in (text or "").split("\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if buf and size + len(p) > CHUNK_MAX_CHARS:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [text or ""]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def attribution_rate(text: str) -> dict:
    """量一段文本的「每句对白有多少带说话人归属」。

    返回：`{"quotes": n, "hits": n, "rate": r, "convention": "front_dao"...}`

    ⚠️ **分母是「对白句数」，不是「每百字」**。
    为什么：每百字会把两件事混在一起 —— 这本书**说多少话**（引号密度）
    与 **说的话里有多少带归属**。参考书《魔道祖师》引号 1.30/百字、
    归属率 76.6%；我们引号 2.12/百字、归属率 3.2%
    ⇒ 按每百字算是「差 15 倍」，按每句算是「差 24 倍」——
    两种口径都指向同一件事，但**只有每句口径能说清该教什么**。
    """
    t = _norm(text)
    p = attribution_profile(t)
    quotes = len(QUOTE_RE.findall(t))
    hits = p["front"] + p["back"] + p["mid"]
    rate = (hits / quotes) if quotes else 0.0
    return {
        "quotes": quotes,
        "hits": hits,
        "rate": min(1.0, rate),
        "front": p["front"], "back": p["back"], "mid": p["mid"],
        "front_dao": p["front_dao"], "front_shuo": p["front_shuo"],
        "convention": attribution_convention(t)["convention"],
        "chars": len(t),
    }


def attribution_target(paragraphs: Sequence[str]) -> dict | None:
    """从参考书的段落里量出「归属目标」。

    返回：`{"rate": 目标归属率, "convention": 主导形态, "examples": [...]}`；
    量不出来（没有对白 / 几乎没有归属）⇒ `None`（不校正）。

    ⚠️ 目标取的是**参考书自己的分布中心**，不是拍的数字 ——
    与 `form.form_target` 同一套思路。
    """
    text = "\n".join(p or "" for p in (paragraphs or []))
    if len(_norm(text)) < 500:
        return None
    m = attribution_rate(text)
    if not m["quotes"]:
        return None
    conv = m["convention"]
    if conv == "none":
        return None
    examples = pick_attribution_examples(paragraphs, conv)
    if not examples:
        return None
    return {
        "rate": m["rate"],
        "convention": conv,
        "examples": examples,
        "quotes": m["quotes"],
        "chars": m["chars"],
    }


def pick_attribution_examples(paragraphs: Sequence[str], convention: str,
                              limit: int = EXAMPLE_LIMIT) -> list[str]:
    """从参考书里挑**归属写法正确**的原文段落，给模型当正确实例。

    为什么给实例不给数字：`form.py` 的实测结论 ——「模型能照着抄形态，抄不了比率」。
    """
    if convention == "front_dao":
        pat = re.compile(r"[^\s，。！？：；、]{0,12}(?:说道|问道|答道|低声道|沉声道|冷声道|道)[：:]")
    elif convention == "front_shuo":
        pat = re.compile(r"[^\s，。！？：；、]{0,12}说[：:]")
    else:
        pat = re.compile(r"[”』][。！？]?[^\s，。！？：；、“”]{0,8}(?:说|道)[。！？]")
    out: list[str] = []
    for p in paragraphs or []:
        for line in (p or "").split("\n"):
            line = line.strip()
            # ⚠️ 下限是 **8** 字不是 20 —— 归属句天然就短
            # （「魏宗主道：“放肆。”」只有 10 个字），下限定 20 会把
            # **最标准**的实例挡在外面（实测踩过：目标直接量不出来）。
            if not (8 <= len(line) <= 120):
                continue
            if "“" not in line or "”" not in line:
                continue
            if not pat.search(line):
                continue
            out.append(line)
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

_SYSTEM = "你是一位中文小说编辑，只负责调整对白里「说话人归属」的写法。"

_FORM_HINT = {
    "front_dao": "说话人 + 「道」字 + 冒号 + 引语（`某人道：“原话”`）",
    "front_shuo": "说话人 + 「说」字 + 冒号 + 引语（`某人说：“原话”`）",
    "back": "引语在前、说话人在后（`“原话。”某人道。`）",
}

_USER = """下面的正文里，**对白的说话人归属写法**与这本书的习惯不一样。

这本书的习惯是：**{form}**，
大约 **{pct:.0%}** 的对白都带着说话人归属。

请只改**说话人归属的写法**，别的都不动：
1. 把没有说话人归属的台词，补上归属，写成 {form}；
   说话人用上下文里已经在场的人，不要新造人物；
2. 把 `“原话。”某人道。` 这种把说话人放在后面的，改成 {form}；
3. 把 `“原话，”某人道，“接着说”` 这种夹在中间的，并成 {form} 的一句；
4. 台词内容**逐字保留**，不许改、不许删、不许新增台词；
5. 情节、动作、环境、叙述顺序都不要动。

下面是**同一本书**里这种写法的原文，照它的样子写：

{examples}

只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""


def _call_once(provider, system: str, user: str) -> str:
    """调一次模型，返回正文（空串 = 没拿到）。"""
    try:
        out = provider.chat(system, user, temperature=ATTRIBUTION_TEMPERATURE) or ""
    except TypeError:
        out = provider.chat(system, user) or ""
    return (out or "").strip()


def correct_attribution(
    text: str,
    provider,
    *,
    target_rate: float,
    convention: str,
    examples: Sequence[str],
    tol: float = MAX_LEN_GROWTH_RATIO,
) -> tuple[str, dict]:
    """把正文的归属写法掰到目标。返回 `(结果文本, 诊断)`。

    诊断字段：
        applied   是否采纳（False 时原样返回 text）
        reason    未采纳的原因
        before/after  校正前后的归属率
        target    目标归属率
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    before = attribution_rate(text)
    if before["chars"] < MIN_CHARS:
        return text, {"applied": False, "reason": "正文太短", "before": before}
    if not examples:
        return text, {"applied": False, "reason": "没有归属样例", "before": before}
    form_hint = _FORM_HINT.get(convention, _FORM_HINT["front_dao"])

    # —— 分段改写 ——
    #
    # 为什么要切：实测一次改写 2840 字时模型返回**空输出**
    # （reasoning 吃光 token 预算，与主生成同一个症状）⇒ 整段白跑。
    # 切成 ~1000 字一段后，同样的预算绰绰有余。
    #
    # 为什么某段拿不到就**保留那段原文**：改写是增强，不是前提 ——
    # 一段失败不该把整段的校正判死。
    chunks = _split_chunks(text)
    outs: list[str] = []
    failed = 0
    for ch in chunks:
        user = _USER.format(
            form=form_hint,
            pct=target_rate,
            examples="\n".join(f"· {e}" for e in examples),
            text=ch,
        )
        got = ""
        for _try in range(CHUNK_RETRIES + 1):
            try:
                got = _call_once(provider, _SYSTEM, user)
            except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
                got = ""
                last_err = f"{type(exc).__name__}"
            if got:
                break
        if not got:
            failed += 1
            outs.append(ch)          # 拿不到就**保留原文**
        else:
            outs.append(got)
    out = "\n".join(outs)
    if failed == len(chunks):
        return text, {"applied": False, "reason": "空输出", "before": before}

    after = attribution_rate(out)
    info = {"before": before, "after": after, "target": target_rate,
            "convention": convention, "chunks": len(chunks),
            "chunks_failed": failed}

    # —— 判据 1：对白内容**守恒**（字符多重集口径，对合并/拆分免疫）——
    from app.style.form import _chars_lost, _dialogue_chars

    before_chars = _dialogue_chars(text)
    after_chars = _dialogue_chars(out)
    lost = _chars_lost(before_chars, after_chars)
    added = _chars_lost(after_chars, before_chars)
    if lost > 0.10 or added > 0.10:
        return text, {**info, "applied": False,
                      "reason": f"对白内容没守恒（丢 {lost:.0%} / 多 {added:.0%}）",
                      "dialogue_lost": lost, "dialogue_added": added}

    # —— 判据 2：字数增长按结构封顶 ——
    growth = len(out) - len(text)
    cap = max(MAX_LEN_GROWTH_ABS, int(len(text) * MAX_LEN_GROWTH_RATIO))
    if growth > cap:
        return text, {**info, "applied": False,
                      "reason": f"字数涨太多 {len(text)}→{len(out)}（上限 +{cap}）",
                      "growth": growth}

    # —— 判据 3：归属率必须**向目标靠近** ——
    d_before = abs(before["rate"] - target_rate)
    d_after = abs(after["rate"] - target_rate)
    if d_after >= d_before:
        return text, {**info, "applied": False,
                      "reason": f"归属率没靠近目标 {before['rate']:.0%}→"
                                f"{after['rate']:.0%}（目标 {target_rate:.0%}）"}

    # —— 判据 4：不许过冲（矫枉过正 = 把一种偏置换成另一种）——
    if (before["rate"] - target_rate) * (after["rate"] - target_rate) < 0 and \
            min(abs(before["rate"] - target_rate),
                abs(after["rate"] - target_rate)) > MAX_OVERSHOOT:
        return text, {**info, "applied": False,
                      "reason": f"过冲 {before['rate']:.0%}→{after['rate']:.0%}"
                                f"（目标 {target_rate:.0%}）"}

    logger.info("归属校正：归属率 %.0f%% → %.0f%%（目标 %.0f%%）",
                before["rate"] * 100, after["rate"] * 100, target_rate * 100)
    return out, {**info, "applied": True}
