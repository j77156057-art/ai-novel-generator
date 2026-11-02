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

⚠️ 上面这组数字是**旧尺**（自己切句）量的。2026-11-01 起 `_stats` 改成
**档案那把尺**（`stylometry` 的句长均值 / 句号密度）—— 见 `_stats` 的说明。
新旧尺在参考书上差 25%~40%，历史数字不再与现在可比。
"""

from __future__ import annotations

import re
import statistics
from typing import Sequence

#: 改写提示词。刻意不写「少用句号」——实测会被理解成「不用标点」，
#: 输出 1000 字零标点（句长指标直接爆到 1036）。必须写「把句号改成逗号」。
_REWRITE_SYSTEM = "你是一位中文小说改稿编辑，只负责调整断句节奏。"

_REWRITE_USER = """把下面这段小说改写成长句节奏。

**必须遵守**：
1. 把原本的句号改成逗号，用逗号串联连续的动作、观察与心理；
2. 必须保留标点，不要把整段连成一串，段落间仍要用句号收束；
3. **对白必须保留引号**，不要把对话并进叙述句里；
4. {sentence_target}；
5. 情节、人物、细节、对白内容一律不变，只调整断句；
6. **不要扩写**：改写后的字数要和原文差不多（上下浮动 10% 以内），
   不要增加新的对白、动作或情节 —— 你的活儿只是「重新断句」，不是「续写」。
{spec_hint}
只输出改写后的正文，不要解释，不要加标题。

【原文】
{text}"""


def _spec_hint_lines(deviations: Sequence[str] | None) -> str:
    """把规格书的偏差句拼成改写提示里的一段（可选）。

    ## 为什么要给模型看「哪里偏了」

    原来只有「目标句长带」一句话，模型只知道**该到哪**，不知道**现在差多少**
    ⇒ 它只能凭感觉拉句子长度（实测：普遍冲到带上限以上）。
    给出偏差量（实测 6.2，目标 0.7~4.5）后，模型知道该**收**还是该**放**。

    ## 为什么只给区间型出带项

    上限型（比喻/情绪直陈/段尾拔高）超出只告警、不回灌 ——
    改写只动断句，让它去「少用比喻」是越权（它改不了，只会乱删）。

    ## 没有偏差句时

    返回空串 ⇒ 提示词与改造前**逐字节相同**（不凭空加一句「请注意质量」）。
    """
    lines = [d for d in (deviations or []) if d]
    if not lines:
        return ""
    body = "\n".join(f"   - {d}" for d in lines)
    return (
        "\n\n**另外，这一段与参考书的实测写法有以下差距（请一并靠拢）**：\n"
        f"{body}\n"
        "   （只调整断句与节奏去靠拢这些数字；情节与内容仍然一律不变。）"
    )


#: 目标句长的**兜底**说法（没有参考书档案时用）。
#:
#: ⚠️ 这个数字以前是**写死在提示词里**的（45~70 字），而参考书自己的句长带
#: 实测只有 22~27 字 ⇒ 模型必然把句子拉过头：
#: 实测一句从 25.6 字冲到 **41.2** 字（档案 p90 才 26.7）。
#: 又因为**句号密度 ≈ 1000 / 句长**（实测 39.1 vs 38.3、24.3 vs 24.5），
#: 句长冲过头 ⇒ 句号密度跟着一起出带 ⇒ 「产出不像参考书」的头号短板。
#: ⇒ 目标必须来自档案；写死的数字只能当兜底。
DEFAULT_SENTENCE_TARGET = "平均每句 45 到 70 个字"


def _sentence_target_line(target_range: tuple[float, float] | None) -> str:
    """目标句长那句话 —— 有档案就用档案的带，没有才用兜底。

    ## 为什么写成「硬上限 + 宁可少合并」（实测 2026-11-01）

    原来这句是「平均每句 X 到 Y 个字」—— 模型把 Y 当成**目标值**，
    实测改写出来的句长普遍比档案带高 10%~40%（一次冲到 29.9，带上限 26.6）。

    探针对照（同一份真实草稿 × 3 种写法）：
    | 写法 | 落在带内 | 冲过头的那些 |
    |---|---|---|
    | 只给区间 | 2/3 | 29.9 |
    | **加硬上限 + 宁可少合并** | 2/3 | **27.1** |
    | 再加参考书真实句子做样例 | 2/3 | 27.5 |
    ⇒ 「任何一句都不要超过 Y 个字」能把冲过头的幅度压掉约 3 字；
    样例句没有带来额外收益（本轮不进，见 §10.4「本版不做什么」）。
    """
    if not target_range:
        return f"{DEFAULT_SENTENCE_TARGET}（没有参考目标，按这个来）"
    lo, hi = target_range
    mid = (lo + hi) / 2
    return (f"每句的长度控制在 **{lo:.0f} 到 {hi:.0f} 个字**之间"
            f"（平均约 {mid:.0f} 个字）；"
            f"**任何一句都不要超过 {hi:.0f} 个字**；"
            f"宁可少合并一点，也不要把句子拉得太长"
            f"（这是参考书自己的节奏）")


def _stats(text: str) -> tuple[float, float, int]:
    """(平均句长, 句号密度/千字, 对白引号数) —— **与档案同一把尺**。

    ## 为什么必须换成档案那把尺（2026-11-01 实测）

    这里原先自己切句：按 `[。！？…]` 切、**丢掉 ≤3 字的碎片**、**不算终结符**。
    实测这把尺在**参考书**上比档案的 `stylometry` 句长均值高 **25%~40%**
    （参考书三段 3000 字窗口：改写尺 27.3 / 29.3 / 30.0，
    档案尺 19.0 / 23.3 / 20.8），而在生成文本上只差 2%~5%。

    ⇒ 拿档案的句长带去卡改写尺量的数，是**苹果比橘子**：
    一段「改得跟参考书一模一样」的文本，在改写尺上读 27~30（比档案带 18~27 高），
    于是判据「不许修过头」会**系统性误杀**改得最好的那类结果。

    实测（2026-11-01，A/B 六段真实产出）：换成同一把尺后，
    「修过头」这道判据的判决与**最终 21 项带内率**完全对得上 ——
    拒绝的 A1/A2/B2 是 6/21、13/21、12/21，采纳的 B1/A3 是 17/21、14/21；
    换成旧尺时 B1（最好的那段，17/21）会被误拒。

    所以这里直接复用 `app.style.stylometry` 的切句与计数
    （终结符算进句长、闭合引号跟句、句号密度只数「。」）——
    与 `band_check`、档案分布、闸门判定**同一把尺**。
    """
    from app.style.stylometry import paragraphs as _paras
    from app.style.stylometry import sentences as _sents

    lens = [len(s) for p in _paras(text) for s in _sents(p)]
    n = max(len(text), 1)
    return (
        statistics.fmean(lens) if lens else 0.0,
        text.count("。") / n * 1000,
        len(re.findall(r"[“「]", text)),
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

#: 句长「实质改善」的判定：**+≥ 2 字** 或 **+≥ 10%**（取其一即可）。
#:
#: 为什么不再是原来的硬阈值「≥ 1.2×」：实测两段**真实改善**
#: （旧尺 +19.1% / +19.8%，档案尺 +21.6% / +18.8%）
#: 因为差半个百分点的相对阈值被**静默丢掉** ⇒ 白花一次模型调用，
#: 而且丢掉的那两段其实把句号密度拉近了 15%。
MIN_SENTENCE_LEN_GAIN_ABS = 2.0
MIN_SENTENCE_LEN_GAIN_RATIO = 0.10


def _distance_to_band(value: float, lo: float | None, hi: float | None) -> float:
    """值离目标带的距离（带内 = 0）。没有带 ⇒ 视为 0（不拦）。"""
    if lo is None or hi is None:
        return 0.0
    if value < lo:
        return lo - value
    if value > hi:
        return value - hi
    return 0.0

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


#: 改写调用**关思考**。
#:
#: 实测 2026-11-01（跨书对照 18 段 + 单独探针）：闸门触发了 **17/18 段**，
#: 而改写每次都**返回空输出**（`applied=False, reason=空输出`）⇒
#: 「改了等于没改」，产出侧完全看不出来。
#: 根因与归属校正同源：**推理块吃光了输出预算**（max_tokens=8000），
#: 正文一个字都没吐。
#:
#: ⇒ 改写是**机械变换**（只动断句），不需要思考 ⇒ 关掉思考，
#: 并给一份**独立预算**（下面那个）。仓库里已有同一套路：
#: `app/style/attribution_fix.py` 的 `CORRECTION_THINKING_OFF`。
REWRITE_THINKING_OFF = True

#: 改写调用的**独立输出预算**（不跟生成链共用）。
#:
#: 为什么单独给：生成链的 `max_tokens` 是按「写一整段正文」定的，
#: 而改写是**改写**（输出长度 ≈ 输入长度）⇒ 预算要够大，
#: 否则被 `max_tokens` 拦腰截断（`finish_reason=length`）。
#:
#: 实测（A/B，2026-11-01）：开思考 + 8000 ⇒ 空输出；开思考 + 16000 ⇒ 2/3 采纳；
#: 关思考 + 8000 ⇒ 2/3 采纳且快 2.5 倍。⇒ 关思考 + 16000 是两头都占的组合。
REWRITE_MAX_TOKENS = 16000


def _call_provider(provider):
    """改写调用用哪个 provider：**关思考 + 独立预算**的变体。

    provider 不认 `variant()`（老 provider / 本地兜底 / 测试假 provider）⇒ 原样返回。
    """
    try:
        return provider.variant(thinking_off=REWRITE_THINKING_OFF,
                                max_tokens=REWRITE_MAX_TOKENS)
    except Exception:  # noqa: BLE001 —— 变体失败就用原 provider
        return provider


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
    target_sentence_range: tuple[float, float] | None = None,
    target_sentence_len: float | None = None,
    deviations: Sequence[str] | None = None,
) -> tuple[str, dict]:
    """把 text 改写成参考语料的断句节奏，返回 (结果文本, 诊断)。

    诊断字段：
        applied      是否采纳了改写（False 时原样返回 text）
        reason       未采纳的原因
        before/after 改写前后的 (句长, 句号/千字, 引号数)
        chars        (原字数, 新字数)
        target       目标句长带（来自参考书档案的 p10~p90）

    ## `target_sentence_range`：目标句长带（p10, p90），来自参考书档案

    给了它，两件事同时变：
    1. **提示词里的目标句长**从写死的「45~70 字」换成这条带 ——
       写死的数字与参考书实测的 22~27 字差一倍，模型必然把句子拉过头；
    2. **判据多一条**：改完的句长**离这条带不能更远**
       （不许修过头：实测一句冲到 41.2 字，而档案 p90 才 26.7）。

    ## `deviations`：规格书的偏差句（可选，2026-11-01）

    来自 `app.style.spec.spec_deviation_lines`（**只含区间型出带项**），
    拼进提示词让模型知道**现在差多少**，而不只是**该到哪**。
    不给 ⇒ 提示词与从前逐字节相同。

    ## 判据（全过才采纳）

    1. 字数不能缩水过多（< 60%）；
    2. **字数不能膨胀过多**（> 125%）—— 模型把「每句 45~70 字」当成「多写点」
       时会凭空扩写（实测 3011 字 → 5979 字，引号 60 → 161）；
    3. 对白引号不能明显丢失（< 80%）；
    4. 句长必须有**实质改善**（+≥ 2 字或 +≥ 10%）；
    5. **不许修过头**：句长离目标带不能比改写前更远（给了目标带才判）；
    6. **对白形态不能被改坏**（纯台词段占比不能上升 > 5 个百分点）——
       改写只做断白，不该让「一行一句台词」变多（实测一次把 52% 改到 84%）。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}

    if target_sentence_range is None and target_sentence_len:
        target_sentence_range = (target_sentence_len * 0.75, target_sentence_len * 1.25)

    before = _stats(text)
    user_prompt = _REWRITE_USER.format(
        text=text,
        sentence_target=_sentence_target_line(target_sentence_range),
        spec_hint=_spec_hint_lines(deviations),
    )
    # 先用「关思考 + 独立预算」的变体；万一这个变体不被接受（例如强制
    # adaptive thinking 的模型会 400），退回原 provider 再试一次。
    out = ""
    last_exc: Exception | None = None
    for prov in (_call_provider(provider), provider):
        try:
            out = prov.chat(
                _REWRITE_SYSTEM, user_prompt,
                temperature=REWRITE_TEMPERATURE,
            ) or ""
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 - 改写失败绝不能阻断生成
            last_exc = exc
            out = ""
    if last_exc is not None:
        return text, {"applied": False, "reason": f"调用失败: {type(last_exc).__name__}", "before": before}

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

    # 判据 4：句长必须有**实质改善**（不再用「≥ 1.2×」那个硬阈值）
    # 句长用**档案那把尺**（见 `_stats`），所以这里的 % 与档案带同口径。
    gain_abs = after[0] - before[0]
    gain_ratio = (gain_abs / before[0]) if before[0] else 0.0
    if not (gain_abs >= MIN_SENTENCE_LEN_GAIN_ABS
            or gain_ratio >= MIN_SENTENCE_LEN_GAIN_RATIO):
        return text, {
            "applied": False,
            "reason": f"句长未改善 {before[0]:.1f}→{after[0]:.1f}",
            "before": before, "after": after,
        }

    # 判据 5：**不许修过头** —— 句长离目标带不能比改写前更远。
    # 实测（A 臂第 1 段）：句长被拉到档案带的两倍（档案尺 39.5 vs 带上限 26.7），
    # ⇒ 越过带子的另一端；又因为句号密度 ≈ 1000/句长，句号密度跟着一起出带。
    # 那一段的**最终 21 项带内率只有 6/21**（六段里最差）——
    # 所以这道判据不是洁癖，是实打实挡住一次质量崩塌。
    #
    # ## 为什么「不许离带更远」只有句长一道？（2026-11-02 拍板：不加）
    #
    # 曾经考虑给句号密度等特征也各设一道，实测后否了：
    #
    # - 判据 5 本身是**活的**：跨书 v2（21 段）+ v3（12 段）里真拦下过 5 段
    #   （23.1→35.8、24.7→35.7、21.0→43.5、23.8→42.0、21.3→29.8 ——
    #   全部冲到目标带上限的 1.3~1.7 倍）；
    # - 而再加一道「句号密度也不许离带更远」在 §10 的 A/B/C 跑次里
    #   （**10 次被采纳的改写**）**额外拦下 0 段**；
    # - 根因是两项同进同退：句号密度 ≈ 1000 / 句长，方向相反 ⇒
    #   「句长冲过头」与「句号密度掉出下界」是同一件事的两种说法。
    #
    # 而且拒绝是有代价的：拒绝 = 保留更碎的原文 = 那段更不像参考书
    # （当时采纳率只有 13/21、9/12）。⇒ 收益为零、代价为实的判据不加。
    #
    # ## 纪律：以后要加，先拿真跑数据证明它会拦下什么
    #
    # 「额外拦下数 = 0」⇒ 不加。额外拦下的每一段都要人工看一眼，
    # 确认它确实「改坏了」而不是「模型换了个写法」。
    # 详见 `docs/rewrite-criteria-and-writer-budget-design.md` §3。
    if target_sentence_range:
        lo, hi = target_sentence_range
        d_before = _distance_to_band(before[0], lo, hi)
        d_after = _distance_to_band(after[0], lo, hi)
        # 注意是「更远」而不是「不更近」：`d_after > d_before` 才算修过头。
        # 用 `>=` 会把「本来就在带内、改完还在带内」也拒掉（0 > 0 不成立，
        # 0 >= 0 成立）—— 那是误杀。
        if d_after > d_before:
            return text, {
                "applied": False,
                "reason": (f"句长修过头 {before[0]:.1f}→{after[0]:.1f}"
                           f"（目标带 {lo:.1f}~{hi:.1f}，离带更远了）"),
                "before": before, "after": after,
            }

    # 判据 6：对白形态不能被改坏（改写只做断句，不该让「一行一句台词」变多）
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
        "target": list(target_sentence_range) if target_sentence_range else None,
    }
