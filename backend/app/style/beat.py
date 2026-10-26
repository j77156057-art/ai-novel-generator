"""beat 插入：**选点**（纯机械，可验收）+ **提示词**。

## 这是什么

对话戏里「缺中间的反应叙述」这个问题的**局部解法**：
在连续对白段的**开头**插入一小段动作/神态/环境叙述，让那段从
「光台词」变成「动作+台词」。

## ⚠️ 这个方向已被真跑推翻（默认关闭）

**2026-10-25 的实测**：15 本真书的「光台词段」（A 类）占比是
**中位 72%**（min 7% / max 88%）—— **雪中悍刀行（7%）是特例**。
我们生成的是 61%，**已在真书区间下沿**。

按雪中的标准去插：真跑曲线显示动作密度升到真书 **5 倍**（=「网球法」），
而 A 类只降到 35%，**采纳率还随量下降**。

⇒ **代码与判据保留**（① 目标比例取自参考书自己，对动作密集的书仍有意义；
② 它是「局部插入」这条路的实现与判据范例），但**默认关闭**。

## 为什么是「局部插入」而不是「整段形态改写」

整段改写（`style/form.py` 的 `correct_form`）已实测**不可靠**：
正式测量 0/20、受控重复 0/4。局部插入的任务小得多：
上下文现成、目标单一、输出可逐段验收。实测采纳率 5/6（83%）。

外部写作方法论里的对应概念是 **beat**（stage business / blocking）——
「对话进行中插入的动作叙述」；参考书正是「动作与台词写在同一段」的标准做法
（[Beats in the Dialogue](https://thejamespack.com/tag/dialogue/)）。

## 选点判据（全部来自真书实测，见 docs/dialogue-form-external-solutions.md §4.1.1）

| 判据 | 值 | 依据 |
|---|---|---|
| 插入位置 | 对白连排的**前几位** | 真书 A 类在连排里的位次：第1位 38%、第2位 21%、第3位 14% |
| 插入长度 | **10~30 字** | 真书 C 类引号前叙述：p25 7 · p50 14 · p75 27 |
| 插入比例 | 降到**参考书自己的** A 类占比 | 15 本真书：min 7% · **中位 72%** · max 88% —— 雪中是特例 |
| 禁止 | 每句台词都插 | 「网球法」：读者只见动作不见人 |

**为什么不是「永远插在第 1 位」**：那样会让 A 类全跑到第 2 位去，
形态会漂（真书第 1 位 38% / 第 2 位 21%，是**分散**的）。
⇒ 选点用**轮转**：第 1、2、3 位按参考书位次分布轮流取，
   并且**跳过已经是 C 类的段**（它已经有动作了）。

## 判定的两把尺

「A 类」（光台词）与「C 类」（动作+台词）要能机检 —— 本模块用
`style/stylometry.plain_dialogue_ratio` 那一套（**剥掉说话人+提示语**）。

**注意**：档案里那个「纯台词段占比」是**旧尺**（把提示语算成叙述），
它测的是「这段有多少叙述」，与本模块的 A 类**不是同一个数**
（真书雪中：旧尺 22% / 新尺 9%；斩神：旧尺 8% / 新尺 80%）。
**跨书比较只能用同一把尺**。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.style.stylometry import (
    _is_speech_tag_only,
    paragraphs as _paragraphs,
)

#: 插入长度区间（真书 C 类引号前叙述 p25~p75 向上取整）。
BEAT_MIN_CHARS = 10
BEAT_MAX_CHARS = 30

#: 目标 A 类占比（占含引号段）。
#:
#: **不要写死这个数** —— 它只是「取不到参考书时的兜底」。
#:
#: 实测（15 本真书，191905 个含引号段）：A 类占比 **中位 72%**，
#: 分布是 **min 7%（雪中悍刀行）· p25 64% · p75 81% · max 88%**。
#: ⇒ **雪中与魔道祖师是特例**（动作叙述极密，每百字 0.6 / 1.1 个），
#:   多数书在 64%~88%。
#:
#: 引擎侧会从**参考书自己的原文**算这个比例（`_reference_plain_dialogue_ratio`），
#: 写死的常量只在取不到时兜底。
DEFAULT_TARGET_A_RATIO = 0.72

#: 真书里 A 类在连排里的位次分布（雪中实测 651 个 A 类）。
#:
#: 用它来**分散**选点：若一律插在第 1 位，A 类会全跑到第 2 位，形态会漂。
_POS_WEIGHTS = ((1, 0.38), (2, 0.21), (3, 0.14), (4, 0.08))


@dataclass
class BeatSlot:
    """一个待插入的位置。"""

    #: 段索引（在传入的段落列表里）
    index: int
    #: 这一段前后的上下文（给模型看）
    before: str
    after: str
    #: 这一段是连排里的第几位
    rank: int
    #: 连排长度
    run_len: int


def has_quote(text: str) -> bool:
    """这一段有没有成对引号。"""
    from app.style.stylometry import _QUOTE_PAIRS

    return any(q in text for q, _ in _QUOTE_PAIRS)


def action_narration_len(text: str) -> int:
    """这一段引号前、**剥掉说话人+提示语后**剩下的动作/神态叙述有多少字。

    0 = 光台词（A 类）；>0 = 有动作（C 类）。

    实现走 `stylometry._strip_speech_tag` ——**唯一实现**，
    避免又出现「同一个概念两个数」。
    """
    from app.style.stylometry import _strip_speech_tag

    return len(_strip_speech_tag(text))


def is_plain_dialogue(text: str) -> bool:
    """A 类：只有引号里的话，引号前**没有任何动作/神态**。"""
    from app.style.stylometry import _QUOTE_PAIRS

    if not any(q in text for q, _ in _QUOTE_PAIRS):
        return False
    return action_narration_len(text) < 3


def has_action(text: str) -> bool:
    """C 类：引号前有动作/神态叙述。"""
    from app.style.stylometry import _QUOTE_PAIRS

    if not any(q in text for q, _ in _QUOTE_PAIRS):
        return False
    return action_narration_len(text) >= 3


def find_slots(
    paragraphs: Sequence[str],
    *,
    target_ratio: float = DEFAULT_TARGET_A_RATIO,
    max_slots: int = 8,
) -> list[BeatSlot]:
    """找出该插入 beat 的位置（**纯机械**，不调模型）。

    判据（全部来自真书实测）：
    1. 只考虑**含引号的段**；
    2. 只考虑 **A 类**（光台词，引号前没有动作）—— C 类已经有了，不动；
    3. 目标是把 A 类占比降到 `target_ratio`（参考书自己的比例）；
    4. 选点**按真书位次分布分散**：优先第 1 位（38%），其次第 2 位（21%）…；
    5. beat 是**并进**那一段（让它从 A 类变成 C 类），不是另起一段。
    """
    ps = list(paragraphs)
    quote_idx = [i for i, p in enumerate(ps) if has_quote(p)]
    if not quote_idx:
        return []
    # 连续对白段分组
    runs: list[list[int]] = []
    for i in quote_idx:
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])

    a_count = sum(1 for i in quote_idx if is_plain_dialogue(ps[i]))
    current = a_count / len(quote_idx)
    if current <= target_ratio:
        return []

    need = int(round((current - target_ratio) * len(quote_idx)))
    if need <= 0:
        return []

    cands: list[tuple[float, int, int, int]] = []  # (优先级, 段索引, 位次, 连排长度)
    for run in runs:
        for rank, i in enumerate(run, 1):
            if not is_plain_dialogue(ps[i]):
                continue
            w = 0.05
            for r, p_ in _POS_WEIGHTS:
                if r == rank:
                    w = p_
                    break
            cands.append((-w, i, rank, len(run)))
    cands.sort()

    slots: list[BeatSlot] = []
    for _w, i, rank, run_len in cands:
        if len(slots) >= min(need, max_slots):
            break
        slots.append(BeatSlot(
            index=i,
            before=ps[i - 1] if i > 0 else "",
            after=ps[i] if i < len(ps) - 1 else "",
            rank=rank,
            run_len=run_len,
        ))
    slots.sort(key=lambda s: s.index)
    return slots


def beat_system_prompt() -> str:
    return (
        "你是一位中文小说编辑，只负责在对话里补一处**动作/神态叙述**。"
    )


def beat_user_prompt(before: str, after: str, *, min_chars: int = BEAT_MIN_CHARS,
                     max_chars: int = BEAT_MAX_CHARS) -> str:
    """构造插入提示。

    关键约束（都来自真书实测 + 外部方法论）：
    - 长度 **10~30 字**（真书 C 类引号前叙述 p25~p75 = 7~27 字）；
    - 只写**动作/神态/环境**，不写心理活动（那是叙述层的事）；
    - **不引号**（不写台词）、不写新的人名/地名/物件（不能凭空造事实）；
    - 不推进剧情（不能改变后面那段话的前提）。
    """
    return f"""在下面这段对话里，**{before}** 和 **{after}** 之间，
补一句**动作或神态叙述** —— 读者能看到人物的反应，但**不写台词**。

要求：
1. 长度 **{min_chars} 到 {max_chars} 个字**（这一本书的作者习惯是十几字）；
2. 只写**动作/神态/环境**（如「老黄低下头继续擦马镫」「栏里的青灰马忽然扬头」）；
   **不写心理活动**、不写评价；
3. **不要加引号**，不要写任何人的台词；
4. **不出现新的人名、地名、物件** —— 只用上下文里已有的；
5. **不报数** —— 不要出现任何数量（几匹、十二口、多远这类一律不要）；
6. **不推进剧情** —— 后面那段话的前提不能被改变；
7. 语气与上下文一致，不要突然文绉绉。

只输出这一句叙述，不要解释，不要加标题。
如果句子里用到了人名/地名/物件的名称，在句子**后面**另起一行写
`专名：甲、乙`（没有就不用写这一行）。

【上文】
{before}

【下文】
{after}"""


def merge_beat(paragraphs: Sequence[str], slot_index: int, beat: str) -> list[str]:
    """把 beat **并进**第 `slot_index` 段的前面（不是另起一段）。

    返回新的段落列表（不改动原列表）。

    ## 为什么是「并进」而不是「另起一段」

    真跑暴露的（2026-10-25）：第一版把 beat **另起一段**插在台词段前面，
    结果 **A 类占比一点没降**（61% → 61%）——
    因为 A 类的定义是「这一段只有引号里的话」，
    而那段的引号**后面**已经有了叙述（`“正着更认不全。”徐凤年把账册…`），
    插在它**前面**只是给整段前面多加了一句，与它**是不是 A 类**无关。

    而 A 类的真正形态是：**整段只有引号**（`“世子想听实话？”`）。
    ⇒ beat 必须**并进那一段的引号前面**（变成「动作 + 台词」），
    整段从 A 类变成 C 类 —— 这才是「补上反应叙述」的意思，
    也正是真书 93% 的形态。
    """
    ps = list(paragraphs)
    if not beat.strip():
        return ps
    if slot_index < 0 or slot_index >= len(ps):
        return ps
    b = beat.strip()
    # 并进那一段的前面（保持「动作 + 台词」的顺序）
    ps[slot_index] = f"{b}{ps[slot_index]}"
    return ps
