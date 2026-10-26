"""形态校正：把生成正文的「对白组织方式」掰到参考书自己的形态。

## 为什么要有这一步（实测，不是推测）

两个模型的先验都偏「比参考书更裸台词」——生成的对白里「一行一句台词」的比例
系统性高于参考书：

| 参考书（目标） | MiniMax-M3 | MiniMax-M3.1-Flash |
|---|---|---|
| 《北荒刀歌》（逐单元均值 16.2%） | 58.1% | 27.7% |
| 《镜中局》 | 90% | 85% |

**提示层挪不动它**：四轮提示层实验（口径改写 ×2、注入统计数字、样例复验）全部
在噪声内或更差；而**事后改写能改**（实测 Flash 改写把纯台词段 42% → 6%）。
所以形态校正必须是**独立的第二次改写**，不是改提示词。

## 三个设计决定

1. **尺子 = 参考书自己的逐单元分布**（p10~p90）——不是拍脑袋的容差。
   段落级「对白形态」与「句长」在档案里是**两个不同尺子**，别混用。
2. **目标 = 参考书的逐单元均值**（分布中心），且**不许过冲**（不许把「裸台词
   偏多」改成「裸台词偏少」——那是把一种偏置换成另一种）。
3. **样例 = 同一本书里形态正确��原文段落**。给「形态的正确实例」比给统计
   数字有用：模型能照着抄形态，抄不了比率。

## 验收（不达标就保留原文）

形态必须**向目标靠近**且不过冲；字数、对白内容（引号里的字）、引号数都要守恒。
改写是**风格增强**不是内容重写：内容被动过就整段丢弃。
"""
from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

#: 纯台词行的判定：去掉引号内容后剩下的非标点字符不超过这个数。
#: 与 `style/stylometry.py` 的 `_BARE_SEG_TAIL_MAX` 同一口径（这里复制一份是为了
#: 本模块不依赖测量模块的私有常量；但**两侧必须一致** —— 改一处就要改两处）。
BARE_TAIL_MAX = 6

#: 校正调用的温度。这是「变换」不是「创作」——0.7（创作档）下同一份输入的
#: 输出会大幅漂移：同一段正文、同一提示，一次把纯台词段 76% 改到 20%，
#: 一次改到 92%（方向都反了）。低温把它变成**可复现**的操作。
FORM_TEMPERATURE = 0.2

#: 形态样例给的条数。
FORM_EXAMPLE_LIMIT = 4

#: 形态样例的**理想长度**（好照抄：太短的没信息，太长的抄不动）。
FORM_EXAMPLE_LEN = 60

#: 校正后允许的**过冲**（相对目标）。超过就是「矫枉过正」，宁可不做。
MAX_OVERSHOOT = 0.08

#: 字数增长上限：绝对 + 相对。
#:
#: 为什么不是「不许变长」：形态校正**天然会让正文变长** —— 给一条光秃秃的台词
#: 补上引导语，每段都要多几个字（实测一段 2400 字的正文要 +20% 左右）。
#: 所以上限按「结构」给：
#:   - 每段对白允许增加的引导文字（加的是引导语，不是新内容）；
#:   - 全文增长的绝对/相对余量（挡住「不新增对白、只灌水」）。
#: 真正的内容守恒由**对白内容逐字保留**那道判据负责（见下）。
MAX_LEN_GROWTH_ABS = 45
MAX_LEN_GROWTH_RATIO = 0.35
MAX_LEADIN_CHARS_PER_LINE = 20

#: 对白内容（引号里的字）允许的**丢失**比例。
MAX_DIALOGUE_LOSS = 0.10


def _is_bare_line(line: str) -> bool:
    """这一行是不是「光秃秃一句台词」（去掉引号内容后只剩标点/空白）。"""
    from app.style.stylometry import _strip_quoted

    rest = _strip_quoted(line).strip("：:，。！？…、；; \t　")
    return len(rest) <= BARE_TAIL_MAX


def measure_bare_ratio(paragraphs: Sequence[str]) -> float:
    """一段文本的「纯台词段占比」：含引号的段里，光秃秃台词段占多少。

    分母只算**含引号**的段（不含引号的段是叙述，不参与对白形态的度量）。
    """
    quote_lines = [p for p in paragraphs if ("“" in p or "「" in p or "『" in p)]
    if not quote_lines:
        return 0.0
    bare = sum(1 for p in quote_lines if _is_bare_line(p))
    return bare / len(quote_lines)


def measure_form(text: str) -> dict:
    """量一段文本的对白形态。

    用 `style/stylometry.features()` 的**逐单元口径**（与档案里的分布同一把尺），
    而不是整段汇总 —— 两个口径的数差得很远（真书：逐单元均值 16.2% vs
    全样本汇总 24.7%），混用会把「偏离」算错。
    """
    from app.style.stylometry import features, paragraphs

    paras = paragraphs(text)
    quote_paras = [p for p in paras if ("“" in p or "「" in p or "『" in p)]
    bare_paras = [p for p in quote_paras if _is_bare_line(p)]
    ratio = (len(bare_paras) / len(quote_paras)) if quote_paras else 0.0
    # 档案里的特征是逐单元算的（带长度归一化），所以这里也用同一把尺
    vec = features(text)
    return {
        "bare_ratio": ratio,            # 整段汇总（用于对白内容/样例判断）
        "unit_ratio": float(vec.get("纯台词段占比") or 0.0),   # 逐单元（与档案同尺）
        "quote_paras": len(quote_paras),
        "bare_paras": len(bare_paras),
    }


def form_target(profile: dict, bucket: str = "medium") -> dict | None:
    """从档案里取出「形态目标」：目标值 + 参考书自己的分布带。

    目标 = 该 bucket 的逐单元均值（分布中心）；分布带 = p10~p90。
    没有档案 / 档案里没有这项 → None（不校正）。
    """
    if not profile:
        return None
    feat = ((profile.get("buckets") or {}).get(bucket) or {}).get("feat") or profile.get("feat") or {}
    band = feat.get("纯台词段占比") or {}
    if not band or band.get("mean") is None:
        return None
    return {
        "target": float(band["mean"]),
        "p10": float(band.get("p10") or 0.0),
        "p90": float(band.get("p90") or 1.0),
        "bucket": bucket,
    }


def pick_form_examples(
    paragraphs: Iterable[str],
    target_ratio: float,
    limit: int = FORM_EXAMPLE_LIMIT,
) -> list[str]:
    """从参考书的段落里挑「形态最接近目标」的对白段原文。

    目标低（叙述+台词同段为主）→ 挑**带叙述尾巴**的对白段；
    目标高（裸台词为主）→ 挑**纯台词**的对白段。
    一律只挑**含引号**的段 —— 形态是对白的事。

    ## 长度要「中等」（实测踩过的坑）

    第一版按「形态最接近 + 段落最长」挑 → 挑出来的是 150~200 字的长段，
    **校正采纳率 0/20**；改成「形态最接近 + 长度中等（~60 字）」后同一段输入
    一次就采纳（65% → 21%）。长段的问题是**不可照抄**：模型看到一大段叙述
    加一句台词，抄不到「在台词前面补一句动作」这个动作本身。
    """
    quote_paras = [p for p in paragraphs
                   if ("“" in p or "「" in p or "『" in p) and 3 <= len(p) <= 200]
    if not quote_paras:
        return []
    scored = []
    for p in quote_paras:
        bare = 1.0 if _is_bare_line(p) else 0.0
        # 离目标形态越近越优先（目标 0.24 → 偏好 bare=0 的段；目标 0.8 → 偏好 bare=1），
        # 同等条件下偏好**中等长度**的段（好照抄）。
        scored.append((abs(bare - target_ratio), abs(len(p) - FORM_EXAMPLE_LEN), p))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _d, _l, p in scored[:limit]]


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #

_FORM_SYSTEM = "你是一位中文小说编辑，只负责调整对白在段落里的组织方式。"

_FORM_USER_BARE_HIGH = """下面的正文里，**对白被写成了「一行一句台词」**，
而这本书的习惯是「先写说话人的动作/神态（或环境、心理），再接台词」写在同一段里。

请只改**对白的组织方式**：
1. 把光秃秃一行台词的对白，改成「动作/神态/环境描写 + 台词」写在同一段；
   **引导语要是一整句描写**（像下面的参考样例那样），
   不要只加「他说：」「她答道：」三个字 —— 那不算改；
2. 情节、人物、对话内容、细节一律不变；对白内容逐字保留，
   **不许增加新的对白**，也不许删改任何一句台词；
3. 字数最多比原来多 {tol:.0%}（给引导语留的字数），不要扩写、不要灌水；
4. 不同人说的对白仍然各自成段，不要并进同一段；
5. 叙述段、场景、节奏都不要动。

下面是**同一本书**里这种写法的原文，照它的样子写：

{examples}

只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""

_FORM_USER_BARE_LOW = """下面的正文里，**叙述和台词混在同一段**，
而这本书的习惯是「对白单独成段，一段一句台词」。

请只改**对白的组织方式**：
1. 把「叙述 + 台词」混在一段的对白，拆成「单独一行台词」，叙述留在另一段；
2. 情节、人物、对话内容、细节一律不变；对白内容逐字保留；
3. 字数与原来差不多（上下浮动 {tol:.0%} 以内），不要扩写、不要加新对白；
4. 叙述的顺序与内容不要动。

下面是**同一本书**里这种写法的原文，照它的样子写：

{examples}

只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""


def _dialogue_contents(text: str) -> Counter:
    """引号里的对白内容（归一化后计数）——用来验「对白内容没被改」。"""
    out: Counter = Counter()
    for m in re.finditer(r"[“「『]([^”」』]{1,200})[”」』]", text or ""):
        s = re.sub(r"\s+", "", m.group(1)).strip("：:，。！？…、；;")
        if s:
            out[s] += 1
    return out


def _dialogue_chars(text: str) -> Counter:
    """引号内内容的**字符多重集** —— 内容守恒的**主口径**。

    为什么不能只用逐条计数（`_content_lost`）：
    形态校正的活儿之一就是**把同一个人连续说的多段台词并成一段**
    （「一段一句台词」的流水账 → 一段一口气说完）。
    一合并，逐条计数就认为"少了两条" ⇒ 实测把一次**内容一字未丢**的
    好改写判成"丢 15%"（2026-10-26 真跑，字符多重集口径是 0.0%），
    于是超过 MAX_DIALOGUE_LOSS 被自己的守门判据拒掉 ——
    **形态校正最该发挥的时候被误杀**。

    字符多重集对「合并 / 拆分」免疫（字还在，只是分块变了），
    同时仍然能挡住真正的丢失与凭空扩写：
    同义改写会让"丢"和"多"同时变大 ⇒ 仍被拦。
    """
    out: Counter = Counter()
    for m in re.finditer(r"[“「『]([^”」』]{1,200})[”」』]", text or ""):
        s = re.sub(r"\s+", "", m.group(1)).strip("：:，。！？…、；;")
        if s:
            out.update(s)
    return out


def _chars_lost(before: Counter, after: Counter) -> float:
    """字符级的内容丢失比例（0~1）。"""
    total = sum(before.values())
    if not total:
        return 0.0
    lost = sum(max(0, n - after.get(ch, 0)) for ch, n in before.items())
    return lost / total


def _content_lost(before: Counter, after: Counter) -> float:
    """对白内容丢了多少（按条数计）。"""
    total = sum(before.values())
    if not total:
        return 0.0
    lost = 0
    for text, n in before.items():
        lost += max(0, n - after.get(text, 0))
    return lost / total


def correct_form(
    text: str,
    provider,
    *,
    target_ratio: float,
    examples: Sequence[str],
    tol: float = MAX_LEN_GROWTH_RATIO,
) -> tuple[str, dict]:
    """把正文的对白形态掰到目标形态。返回 (结果文本, 诊断)。

    诊断字段：
        applied   是否采纳（False 时原样返回 text）
        reason    未采纳的原因
        before/after  校正前后的形态（纯台词段占比）
        target    目标形态
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    before = measure_form(text)
    if not examples:
        return text, {"applied": False, "reason": "没有形态样例", "before": before}

    tpl = (_FORM_USER_BARE_LOW if target_ratio < 0.5 else _FORM_USER_BARE_HIGH)
    user = tpl.format(
        text=text,
        examples="\n".join(f"· {e}" for e in examples),
        tol=tol,
    )
    try:
        # **低温**：这是「变换」不是「创作」。0.7 下同一份输入的输出会大幅漂移
        # （实测同一段正文、同一提示：一次把纯台词段 76% 改到 20%，一次改到 92%
        #  —— 方向都反了），校正就会变成随机事件。
        out = provider.chat(_FORM_SYSTEM, user, temperature=FORM_TEMPERATURE) or ""
    except TypeError:
        # 老 provider 签名不接 temperature（不接就退回默认温度）
        out = provider.chat(_FORM_SYSTEM, user) or ""
    except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
        return text, {"applied": False, "reason": f"调用失败: {type(exc).__name__}", "before": before}
    out = out.strip()
    if not out:
        return text, {"applied": False, "reason": "空输出", "before": before}

    after = measure_form(out)
    info = {"before": before, "after": after, "target": target_ratio}

    # 判据 1：对白内容**守恒** —— 不许丢，也不许多。
    # 这是内容守恒的主闸：改写凭空扩写时**会增加新的对白**（实测引号 60 → 161），
    # 靠这条就能挡住；只查「丢失」会漏掉「新增」。
    #
    # **口径用字符多重集**（`_dialogue_chars`），不用逐条计数 ——
    # 形态校正的活儿之一就是把连续多段台词**并成一段**，逐条计数会把它
    # 误判成"内容丢失"（实测一次一字未丢的好改写被判成丢 15% ⇒ 被误杀）。
    before_dlg = _dialogue_contents(text)
    after_dlg = _dialogue_contents(out)
    # 条数口径：只作**提示**（合并/拆分会让它失真），不作判据。
    lost_items = _content_lost(before_dlg, after_dlg)
    added_items = _content_lost(after_dlg, before_dlg)
    # 字符口径：主判据（对合并/拆分免疫，但仍能挡住丢失与凭空扩写）。
    before_chars = _dialogue_chars(text)
    after_chars = _dialogue_chars(out)
    lost = _chars_lost(before_chars, after_chars)
    added = _chars_lost(after_chars, before_chars)
    if lost > MAX_DIALOGUE_LOSS or added > MAX_DIALOGUE_LOSS:
        return text, {**info, "applied": False,
                      "reason": f"对白内容没守恒（丢 {lost:.0%} / 多 {added:.0%}）",
                      "dialogue_lost": lost, "dialogue_added": added,
                      "dialogue_lost_items": lost_items,
                      "dialogue_added_items": added_items}

    # 判据 2：字数增长按**结构**封顶（给引导语留的字数，不是「不许变长」）。
    growth = len(out) - len(text)
    cap = max(MAX_LEN_GROWTH_ABS,
               int(len(text) * MAX_LEN_GROWTH_RATIO),
               MAX_LEADIN_CHARS_PER_LINE * before["quote_paras"])
    if growth > cap:
        return text, {**info, "applied": False,
                      "reason": f"字数涨太多 {len(text)}→{len(out)}（上限 +{cap}）",
                      "growth": growth}

    # 判据 3：形态必须**向目标靠近**
    b = before["bare_ratio"]
    a = after["bare_ratio"]
    d_before = abs(b - target_ratio)
    d_after = abs(a - target_ratio)
    if d_after >= d_before:
        return text, {**info, "applied": False,
                      "reason": f"形态没靠近目标 {b:.0%}→{a:.0%}（目标 {target_ratio:.0%}）"}

    # 判据 4：不许过冲（矫枉过正 = 把一种偏置换成另一种）
    if (b - target_ratio) * (a - target_ratio) < 0 and min(
            abs(b - target_ratio), abs(a - target_ratio)) > MAX_OVERSHOOT:
        return text, {**info, "applied": False,
                      "reason": f"过冲 {b:.0%}→{a:.0%}（目标 {target_ratio:.0%}）"}

    logger.info("形态校正：纯台词段 %.0f%% → %.0f%%（目标 %.0f%%）",
                b * 100, a * 100, target_ratio * 100)
    return out, {**info, "applied": True,
                 "chars": (len(text), len(out)),
                 "dialogue_lost": lost, "dialogue_added": added}
