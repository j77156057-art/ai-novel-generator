"""文风改写：把粗稿的句式改写成参考语料的节奏。

## 为什么需要这一步

同一个模型、同一套风格要求，只因任务形态不同，句长差 3.7 倍：

    一次性生成 3000 字正文   MiniMax-M3 关思考   句长 16.3   4.7 min
    改写已有的 900 字        MiniMax-M3 关思考   句长 60.7   19s

试过 6 个模型、13 种推理参数、5 组 prompt 结构对照，结论一致：
**「一次性生成大量新内容」会把任何模型压回短句安全区**，而「改写已有文本」
能稳定贴近参考语料的句长。所以让粗稿负责情节，改写负责文风。

## 改写只做一件事：断句

不动情节、不动人物、不动细节，只把「一句一动作」的碎句合成有呼吸的复合句。
这是有意的收窄——它带来两个好处：

1. 判据明确可验收：情节词、人物名、对白引号的数量与位置应当保持
2. 失败可检测：改写掉了内容就丢弃改写结果，保留粗稿（宁短句勿丢情节）

## 实测（MiniMax-M3，关闭思考，900 字输入）

    句长 19.0 → 60.7     句号/千字 47.3 → 17.1     对白引号 16 → 16
    19s

`M3.1-Flash` 质量相当（59-69）但关不掉思考，61-88s，不作默认。
"""

from __future__ import annotations

import re
import statistics

#: 改写提示词。刻意不写「少用句号」——实测会被理解成「不用标点」，
#: 输出 1000 字零标点（句长指标直接爆到 1036）。必须写「把句号改成逗号」。
_REWRITE_SYSTEM = "你是一位中文小说改稿编辑，只负责调整断句节奏。"

_REWRITE_USER = """把下面这段小说改写成长句节奏。

**必须遵守**：
1. 把原本的句号改成逗号，用逗号串联连续的动作、观察与心理；
2. 必须保留标点，不要把整段连成一串，段落间仍要用句号收束；
3. **对白必须保留引号**，不要把对话并进叙述句里；
4. 平均每句 45 到 70 个字；
5. 情节、人物、细节、对白内容一律不变，只调整断句；
6. **不要扩写**：改写后的字数要和原文差不多（上下浮动 10% 以内），
   不要增加新的对白、动作或情节 —— 你的活儿只是「重新断句」，不是「续写」。

只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""


def _stats(text: str) -> tuple[float, float, int]:
    """(平均句长, 句号密度/千字, 对白引号数)"""
    ss = [s for s in re.split(r"[。！？…]", text) if len(s.strip()) > 3]
    periods = text.count("。") + text.count("！") + text.count("？")
    fresh = re.findall(r"[“「]", text)
    return (
        statistics.mean(len(s) for s in ss) if ss and periods else 0.0,
        periods / max(len(text), 1) * 1000,
        len(fresh),
    )


#: 改写结果字数低于粗稿这个比例时判为「改坏了」——实测正常改写压缩 15-25%，
#: 低于 60% 说明模型大段删了内容。
MIN_LEN_RATIO = 0.60

#: 对白引号允许的丢失比例。改写常见的小瑕疵是吞掉一两处引号，
#: 超过 20% 说明对白被并进叙述，那是明显的质量退化。
MAX_QUOTE_LOSS = 0.20

#: 改写后字数允许**增长**的上限。
#:
#: 为什么要挡「膨胀」：原来的校验只挡「缩水」（< 60% 判改坏了），**不挡膨胀**。
#: 实测（真书真实正文，M3 改写，2026-10-25）：**3011 字 → 5979 字（翻了一倍）** ——
#: 模型把提示里的「平均每句 45 到 70 个字」当成了「多写点」，凭空扩出一大段
#: 对白（引号数 60 → 161）。后果不只是字数：
#:   - **对白形态被改坏**：纯台词段 52% → 84%（参考书 21%）；
#:   - 多出来的内容是**模型编的**（新对白、新情节），没有事实依据。
#: 所以「膨胀」和「缩水」一样是改坏了，只是方向相反。
#:
#: 上限 = **绝对 + 相对**取大者：短文本给 45 字的余量（模型顺手补个错字、
#: 改个连接词是常事），长文本按 15% 算（1200 字的正文多出 180 字就该怀疑了）。
#: 实测的「好改写」都是**略微收缩**（-1%~-2.6%），远在余量之内；
#: 生产里的段都是 2400 字以上，那时绝对余量让位给相对余量。
MAX_LEN_GROWTH_ABS = 45
MAX_LEN_GROWTH_RATIO = 0.15

#: 纯台词段占比允许上升的幅度（同样来自上面那次实测）。
#:
#: 改写只做断句，**不该动对白形态**。如果改完「一行一句台词」变多了，
#: 那说明它把叙述与台词拆开了 —— 即使句长变好了，这个代价也不该默默接受。
MAX_BARE_RATIO_INCREASE = 0.05

#: 改写用的**温度**。
#:
#: 断句是「变换」不是「创作」，高温会让同一份输入的输出漂移
#: （与形态校正那次「同一段正文、一次 76%→24%、一次 76%→92%」同一个性质：
#: MiniMax 在这类任务上的采样本身会漂）。
#:
#: **实测（2026-10-25）**：低温**治不了**「凭空扩写」——
#: 同一份 3616 字真实正文、同一提示，M3 在 0.7 与 0.2 下**都没扩写**
#: （3614 / 3618 字，引号 80 / 78）。那次的 3011 → 5979 是**采样漂**，
#: 不是温度造成的 —— 膨胀由那五道判据挡。
#:
#: ⇒ 低温的价值是「**防漂**」，不是「防膨胀」；仍然值得给。
REWRITE_TEMPERATURE = 0.2


def _bare_ratio(text: str) -> float:
    """纯台词段占比（去掉引号内容后只剩标点的段，占含引号段的比例）。

    口径与 `style/stylometry.dialogue_segment_form` 同一份实现 ——
    这里不另写一套，避免「同一个概念两个数」。
    """
    from app.style.stylometry import dialogue_segment_form

    from app.style.stylometry import paragraphs as _paras

    _, ratio = dialogue_segment_form(_paras(text))
    return ratio


def rewrite_style(
    text: str,
    provider,
    *,
    target_sentence_len: float | None = None,
) -> tuple[str, dict]:
    """把 text 改写成参考语料的断句节奏，返回 (结果文本, 诊断)。

    诊断字段：
        applied      是否采纳了改写（False 时原样返回 text）
        reason       未采纳的原因
        before/after 改写前后的 (句长, 句号/千字, 引号数)
        chars        (原字数, 新字数)

    ## 五道判据（全过才采纳）

    1. 字数不能缩水过多（< 60%）；
    2. **字数不能膨胀过多**（> 125%）—— 模型把「每句 45~70 字」当成「多写点」
       时会凭空扩写（实测 3011 字 → 5979 字，引号 60 → 161）；
    3. 对白引号不能明显丢失（< 80%）；
    4. 句长必须真的变长（> 1.2×），否则这一步没意义；
    5. **对白形态不能被改坏**（纯台词段占比不能上升 > 5 个百分点）——
       改写只做断白，不该让「一行一句台词」变多（实测一次把 52% 改到 84%）。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}

    before = _stats(text)
    try:
        out = provider.chat(
            _REWRITE_SYSTEM, _REWRITE_USER.format(text=text),
            temperature=REWRITE_TEMPERATURE,
        ) or ""
    except Exception as exc:  # noqa: BLE001 - 改写失败绝不能阻断生成
        return text, {"applied": False, "reason": f"调用失败: {type(exc).__name__}", "before": before}

    out = out.strip()
    if not out:
        return text, {"applied": False, "reason": "空输出", "before": before}

    after = _stats(out)

    # 判据 1：内容不能大段丢失
    if len(out) < len(text) * MIN_LEN_RATIO:
        return text, {
            "applied": False,
            "reason": f"字数缩水过多 {len(text)}→{len(out)}",
            "before": before, "after": after,
        }

    # 判据 2：对白不能明显丢失（先于「膨胀」判：引号全丢是更严重的内容问题）
    if before[2] > 0 and after[2] < before[2] * (1 - MAX_QUOTE_LOSS):
        return text, {
            "applied": False,
            "reason": f"对白引号丢失 {before[2]}→{after[2]}",
            "before": before, "after": after,
        }

    # 判据 3：也不能**膨胀**（模型把「每句 45~70 字」当成「多写点」时会凭空扩写）
    growth_limit = max(MAX_LEN_GROWTH_ABS, int(len(text) * MAX_LEN_GROWTH_RATIO))
    if len(out) > len(text) + growth_limit:
        return text, {
            "applied": False,
            "reason": f"字数膨胀过多 {len(text)}→{len(out)}（上限 +{growth_limit}）",
            "before": before, "after": after,
        }

    # 判据 4：句长必须真的变长了，否则这一步没有意义
    if after[0] <= before[0] * 1.2:
        return text, {
            "applied": False,
            "reason": f"句长未改善 {before[0]:.1f}→{after[0]:.1f}",
            "before": before, "after": after,
        }

    # 判据 5：对白形态不能被改坏（改写只做断句，不该让「一行一句台词」变多）
    try:
        bare_before = _bare_ratio(text)
        bare_after = _bare_ratio(out)
    except Exception:  # noqa: BLE001 —— 量不出来就不拦，绝不能阻断生成
        bare_before = bare_after = None
    if bare_before is not None and bare_after > bare_before + MAX_BARE_RATIO_INCREASE:
        return text, {
            "applied": False,
            "reason": (f"对白形态被改坏（纯台词段 {bare_before:.0%}→{bare_after:.0%}）"),
            "before": before, "after": after,
            "bare_ratio": (bare_before, bare_after),
        }

    return out, {
        "applied": True,
        "before": before,
        "after": after,
        "chars": (len(text), len(out)),
        "target": target_sentence_len,
    }
