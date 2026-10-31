"""归属校正：把对白的「说话人归属」掰到**参考书自己的写法**。

## 为什么要有这一步（实测，不是推测）

参考书《道途魔典》**76.6%** 的对白带说话人归属（`X道：“…”`），
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

## 两遍改写（2026-10-31 实测后加的）

模型「把没有归属的台词都补上」会**拉到接近 100%**（实测 91%），
而参考书《记忆曝光》只有 **25%** ⇒ 过冲同样是泄露面
（规则会反过来指认我们这侧）⇒ 光判「靠近目标」不够。

⇒ **第二遍**让模型**去掉多余的归属**（保留约 M 处，位置要自然、不要有规律），
实测把 91% 压到 **21%**（目标 25%）。
两遍的结果都过判据 1/2，取**离目标最近**的那个，再判 3/4。

## 失败策略

任何一道判据不过 ⇒ **原样返回**（保留原文，不糊弄）。
校正失败绝不能影响正文。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
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

#: 第一遍改写后，离目标还差这么多就**再跑一遍「去冗余」**。
#:
#: 为什么需要第二遍（实测 2026-10-31，都市臂第 1 段）：
#: 第一遍「把没有归属的都补上」把归属率拉到 **91%**，参考书目标只有 **25%**
#: ⇒ **过冲同样是泄露面**（规则「出现『X道：“…”』的那侧 = 人类侧」
#: 会反过来指认我们这侧）⇒ 只判「靠近目标」不够，还要把过冲压下去。
#: 实测第二遍（让模型去掉多余的归属）把 91% 压到 **21%**。
TRIGGER_GAP = 0.12

#: 调整循环：最多几遍。
MAX_ADJUST_ROUNDS = 3

#: 离目标这么近就收手（再调就是浪费调用）。
ADJUST_TOL = 0.08

#: 前置归属的「（名字）+ 道/说 + 冒号」前缀（**不含引号**）。
#:
#: 用于**机械配平**：模型改写完，数量由我们本地定。
#:
#: ⚠️ 名字部分**可以为空** —— 最常见的写法是「叙述，**道：**“…”」
#: （说话人在前面的叙述里，归属只剩一个动词 + 冒号）。
#: 第一版正则要求名字至少 1 个字 ⇒ 这类**一个都匹配不到** ⇒ 配平削不动。
_FRONT_PREFIX_RE = re.compile(
    r"([^\s，。！？：；、“”]{0,12})(道|说)[：:](?=“|「|『)")

#: 名字部分超过这个长度 ⇒ 当**叙述**处理（只删动词，别把叙述也删了）。
#: 「林可低声道：」是 4 个字（名字 + 语气）⇒ 整段删；
#: 「隔了两三口气的工夫才道：」是 12 个字（叙述）⇒ 只删「道：」。
_NAMELIKE_MAX = 6

#: 叙述型前缀末尾的**语气/连接词**（只删动词时顺带去掉一个，免得留下「才：“…”」）。
_TRAILING_PARTICLES = "才又便就"

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

#: 校正调用的**独立输出预算**（不跟生成链共用）。
#:
#: 为什么单独给：生成链的 `max_tokens` 是按「写一整段」定的，
#: 而校正是**改写**（输出长度≈输入长度）⇒ 预算要够大，
#: 否则被 `max_tokens` 拦腰截断（`finish_reason=length`）。
CORRECTION_MAX_TOKENS = 16000

#: 校正调用**关思考**。
#:
#: 实测 2026-10-31（都市臂第 1 段）：3 个分块 × 3 次尝试**全部空输出**，
#: 整段校正判死 ⇒ 那段以「0 归属率」留在盲评包里 = 泄露面。
#: 症状与主生成同源：**推理块吃光了输出预算** ⇒ 正文一个字都没吐。
#: ⇒ 校正是**机械变换**，不需要思考 ⇒ 关掉思考，并给一份独立的预算。
CORRECTION_THINKING_OFF = True


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
    与 **说的话里有多少带归属**。参考书《道途魔典》引号 1.30/百字、
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
            # （「秦昭道：“放肆。”」只有 10 个字），下限定 20 会把
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

#: 第二遍提示：**把归属数量调整到目标**（多了就减，少了就补）。
#:
#: 为什么需要第二遍（实测 2026-10-31，都市臂第 1 段）：
#: 第一遍「把没有归属的都补上」会把归属率拉到 **91%**，
#: 而参考书《记忆曝光》只有 **25%** ⇒ **过冲同样是泄露面**
#: （规则「出现『X道：“…”』的那侧 = 人类侧」会反过来指认我们这侧）。
#:
#: ⚠️ 但**靠模型数数是不准的**（同一段实测落在 21% / 6% / 58% / 35%）
#: ⇒ 数量优先用**机械配平**（`_trim_attribution_to_count`），
#: 这一遍只在模型**没补够**时兜底。
_ADJUST_USER = """下面这段正文里，**对白的说话人归属**需要调整。

这本书的习惯：大约 **{pct:.0%}** 的对白带说话人归属（`某人道：“原话”`），
其余对白不带（说话人由叙述交代）。

这一段共 **{n}** 句对白，现在有 **{now}** 处带归属，目标约 **{m}** 处。
⇒ 请调整为**约 {m}** 处：
  - 现在**多了** {diff} 处 ⇒ 去掉 {diff} 处（把 `某人道：“原话”` 改回 `“原话”`），
    **最多去掉 {diff} 处，别删过头**；
  - 现在**少了** {diff} 处 ⇒ 补上 {diff} 处（写成 `某人道：“原话”`）；
  - 允许有 1~2 处的出入，但别差太多。

要求：
1. 除了归属本身，别的一律不动 —— 台词**逐字**保留，情节、动作、叙述顺序都不动；
2. 动的地方要**自然**：说话人已经由上一句叙述交代清楚的那些可以不写归属，
   别人猜不出说话人的那些要写；
3. 不要有规律性 —— 不要每几句一处、也不要集中在开头或结尾。

只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""


def _call_once(provider, system: str, user: str, max_tokens: int | None = None) -> str:
    """调一次模型，返回正文（空串 = 没拿到）。"""
    try:
        if max_tokens is not None:
            out = provider.chat(system, user, temperature=ATTRIBUTION_TEMPERATURE,
                                max_tokens=max_tokens) or ""
        else:
            out = provider.chat(system, user, temperature=ATTRIBUTION_TEMPERATURE) or ""
    except TypeError:
        # 老 provider 签名不接 temperature / max_tokens
        try:
            out = provider.chat(system, user) or ""
        except TypeError:
            out = provider.chat(system, user, temperature=ATTRIBUTION_TEMPERATURE) or ""
    return (out or "").strip()


def _correction_provider(provider):
    """拿一个**关思考 + 独立预算**的 provider 变体。

    provider 不认 `variant()`（老 provider / 本地兜底）⇒ 原样返回。
    """
    try:
        return provider.variant(thinking_off=CORRECTION_THINKING_OFF,
                                 max_tokens=CORRECTION_MAX_TOKENS)
    except Exception:  # noqa: BLE001 —— 变体失败就用原 provider
        return provider


def _front_prefix_spans(text: str) -> list[tuple[int, int]]:
    """前置归属「（名字）+ 道/说 + 冒号」的**可删区间**（不含引号本身）。

    删多删少有讲究：
    - 「，**道：**“…”」⇒ 说话人在前面的叙述里 ⇒ 只删「道：」⇒ 剩下「，“…”」；
    - 「**林可低声道：**“…”」⇒ 名字 + 语气 ⇒ 整段删 ⇒ 剩下「“…”」；
    - 「隔了两三口气的工夫才**道：**“…”」⇒ 那是**叙述** ⇒ 只删「道：」
      （顺带去掉末尾的语气词「才」，免得留下「才：“…”」）。
    """
    spans: list[tuple[int, int]] = []
    for m in _FRONT_PREFIX_RE.finditer(text or ""):
        name = m.group(1) or ""
        verb_start = m.start(2)
        if not name:
            spans.append((verb_start, m.end()))
            continue
        if len(name) <= _NAMELIKE_MAX:
            spans.append((m.start(), m.end()))
            continue
        # 叙述型：只删动词 + 冒号（末尾的语气词也去掉）
        start = verb_start
        if name[-1] in _TRAILING_PARTICLES:
            start -= 1
        spans.append((start, m.end()))
    return spans


def _trim_attribution_to_count(text: str, keep: int) -> tuple[str, int]:
    """把前置归属**机械配平**到 `keep` 处。

    ## 为什么需要它（实测 2026-10-31）

    模型对**数量**没有掌控力：同一段文本、同一目标（25%），
    迭代调整分别落在 **25% / 10% / 45% / 6% / 58%** ⇒ 让模型反复调是赌。

    ⇒ 换个分工：
    - **模型**负责「给对白安上人名」+「放在自然的位置」（它擅长这个）；
    - **数量**由我们本地定：超过目标的部分**机械去掉**（把 `裴远道：“…”`
      还原成 `“…”`），保留哪些按**内容哈希**挑（分布不规则、且可复现）。

    ⚠️ 去掉的是「人名+道/说+：」整段 ⇒ 引语本身逐字不变（对白内容守恒）。
    """
    spans = _front_prefix_spans(text)
    if keep <= 0:
        keep = 0
    if len(spans) <= keep:
        return text, 0
    # 按**引语内容**的哈希排序（不是按「人名+道：」—— 那常常重名，hash 会一样）
    order = sorted(
        range(len(spans)),
        key=lambda i: hashlib.sha256(
            text[spans[i][1]:spans[i][1] + 30].encode("utf-8")).hexdigest())
    drop = set(order[keep:])
    out = text
    for i in sorted(drop, reverse=True):
        s, e = spans[i]
        out = out[:s] + out[e:]
    return out, len(drop)


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
    #
    # 校正用**关思考 + 独立预算**的 provider 变体
    # （见 CORRECTION_THINKING_OFF 的说明）
    prov = _correction_provider(provider)
    chunks = _split_chunks(text)

    def _run(run_chunks, make_user) -> tuple[str, int, int, str]:
        """跑一遍分块改写。返回 (文本, 失败块数, 空调用次数, 最后错误)。

        ⚠️ `run_chunks` 是**这一遍要改写的文本**切出来的块 ——
        第二遍必须作用在**第一遍的结果**上（不是原文）。
        `make_user(chunk)` 返回提示词；返回 `None` 表示这块**不用改**
        （原样保留，不消耗调用）。
        """
        outs: list[str] = []
        failed = 0
        empty = 0
        last_err = ""
        for ch in run_chunks:
            user = make_user(ch)
            if user is None:
                outs.append(ch)
                continue
            got = ""
            for _try in range(CHUNK_RETRIES + 1):
                try:
                    got = _call_once(prov, _SYSTEM, user)
                except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
                    got = ""
                    last_err = f"{type(exc).__name__}"
                if got:
                    break
                empty += 1
                time.sleep(2)
            if got:
                outs.append(got)
            else:
                failed += 1
                outs.append(ch)      # 拿不到就**保留原文**
        return "\n".join(outs), failed, empty, last_err

    def _add_prompt(ch: str):
        return _USER.format(
            form=form_hint,
            pct=target_rate,
            examples="\n".join(f"· {e}" for e in examples),
            text=ch,
        )

    out1, failed, empty_calls, last_err = _run(chunks, _add_prompt)
    if failed == len(chunks):
        return text, {"applied": False, "reason": "空输出", "before": before,
                      "chunks": len(chunks), "chunks_failed": failed,
                      "empty_calls": empty_calls, "last_error": last_err}

    r1 = attribution_rate(out1)
    candidates: list[tuple[str, dict]] = [(out1, r1)]
    current = out1
    rounds = 0
    trimmed = 0

    # —— **机械配平**：把归属数量调到目标 ——
    #
    # 模型负责「安人名、放自然位置」，**数量**由我们本地定：
    # 超过目标的部分直接去掉（`裴远道：“…”` → `“…”`）。
    # 为什么不靠模型调数量：同一段、同一目标（25%），模型分别落在
    # 25% / 10% / 45% / 6% / 58% ⇒ 让它反复调是赌。
    if r1["quotes"]:
        want = max(0, round(target_rate * r1["quotes"]))
        have = r1["front"] + r1["back"] + r1["mid"]
        if have > want:
            trimmed_text, dropped = _trim_attribution_to_count(current, want)
            if dropped:
                current = trimmed_text
                trimmed = dropped
                candidates.append((current, attribution_rate(current)))

    # —— 还差得远（模型没补够）⇒ 让模型再调几遍 ——
    #
    # 为什么不是「一遍到位」：模型对**比率**的掌控很差（实测同一段：
    # 一遍去冗余分别落在 21% / 6% / 58% / 35%，目标 25%）
    # ⇒ 只能**迭代**：每遍把「现在 N 处 / 目标 M 处」写进提示，让它按数改。
    # 实测（都市臂）：0% → 补后 78% → 调 1 遍 → **25%**（目标 25%）。
    while rounds < MAX_ADJUST_ROUNDS:
        rc = attribution_rate(current)
        if abs(rc["rate"] - target_rate) <= ADJUST_TOL:
            break

        def _adjust_prompt(ch: str):
            m = attribution_rate(ch)
            n = m["quotes"]
            now = m["front"] + m["back"] + m["mid"]
            keep = max(1, round(target_rate * n)) if n else 0
            return _ADJUST_USER.format(
                pct=target_rate, n=n, now=now, m=keep,
                diff=abs(now - keep), text=ch)

        out_r, failed_r, empty_r, _ = _run(_split_chunks(current), _adjust_prompt)
        rounds += 1
        if failed_r >= len(chunks):
            break
        current = out_r
        candidates.append((current, attribution_rate(current)))

    # —— 判据 1、对白内容**守恒**（字符多重集口径，对合并/拆分免疫）——
    from app.style.form import _chars_lost, _dialogue_chars

    before_chars = _dialogue_chars(text)
    best: tuple[float, str, dict] | None = None
    rejects: list[str] = []
    for cand, rate in candidates:
        after_chars = _dialogue_chars(cand)
        lost = _chars_lost(before_chars, after_chars)
        added = _chars_lost(after_chars, before_chars)
        if lost > 0.10 or added > 0.10:
            rejects.append(f"对白内容没守恒（丢 {lost:.0%} / 多 {added:.0%}）")
            continue
        # —— 判据 2：字数增长按结构封顶 ——
        growth = len(cand) - len(text)
        cap = max(MAX_LEN_GROWTH_ABS, int(len(text) * MAX_LEN_GROWTH_RATIO))
        if growth > cap:
            rejects.append(f"字数涨太多 {len(text)}→{len(cand)}（上限 +{cap}）")
            continue
        dist = abs(rate["rate"] - target_rate)
        if best is None or dist < best[0]:
            best = (dist, cand, rate)

    info = {"before": before, "target": target_rate, "convention": convention,
            "chunks": len(chunks), "chunks_failed": failed,
            "empty_calls": empty_calls, "trimmed": trimmed,
            "pass1_rate": r1["rate"],
            "candidates": [round(c[1]["rate"], 3) for c in candidates]}
    if best is None:
        return text, {**info, "applied": False,
                      "reason": rejects[0] if rejects else "没有可用候选",
                      "rejects": rejects}

    dist, out, after = best
    info["after"] = after

    # —— 判据 3：归属率必须**向目标靠近** ——
    d_before = abs(before["rate"] - target_rate)
    if dist >= d_before:
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

    logger.info("归属校正：归属率 %.0f%% → %.0f%%（目标 %.0f%%；第二遍去冗余=%s）",
                before["rate"] * 100, after["rate"] * 100, target_rate * 100,
                trimmed)
    return out, {**info, "applied": True}
