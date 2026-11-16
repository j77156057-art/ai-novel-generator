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

实测（9 段真实产物，MiniMax-M3.1-Flash，effort=low，**两轮独立复跑**）：

| | 纯台词段占比（汇总口径） |
|---|---|
| 校正前 | 71.5% |
| 机械合并后 | 42.2% |
| **加引导语后** | **20.9% / 21.8%** |

参考书 22.7%、目标 18.7% ⇒ 进了参考书的分布带。

采纳率 97% / 93%，正文字数 +7.0~7.1%，9 段共 135~165 秒（并发 6）。
两轮的差别只来自模型采样（温度 0.2 不是 0）⇒ **单次数字不能当结论**。

## 判据（七道，全过才采纳）

1. **长度** 10~30 字（真书引导语 p25~p75 = 7~27 字）；
2. **不含引号** + **像叙述**（默认是叙述，只在明确是说理时才拒）；
3. **不含新的人名/地名/物件**、**不报数**（不能凭空造事实）；
   —— 模型自报的专名必须在「专名表 / 上下文 / **参考书原文**」里
   （参考书原文当**专名白名单**用：整串精确匹配，没有误杀风险）；
4. **不与已采纳的引导语重复**（2-gram Jaccard ≥ 0.40 或字符集 Jaccard ≥ 0.60）；
5. **不与上下文局部重复**（滑动窗口 2-gram Jaccard ≥ 0.25）——
   整段 Jaccard 会把短句跟长段比，相似度被稀释 ⇒ 漏检（实测漏掉 10/14）；
6. **不与本段自身重复**（引导语 vs 本段引号外，2-gram Jaccard ≥ 0.20）——
   「管家点头，…：管家点头：“…”」这种硬伤就是漏在这里（实测 5/115）。
7. **说话人一致**（2026-10-27 加）—— 见下面「说话人校验」一节。

## 说话人校验（判据 7）

引导语是**贴在台词前面**的动作叙述，它的主语会被读者当成说话人 ⇒
主语点错人 = 读者把这句话记到别人头上。这是这条路的已知代价。

做法：**让模型自报它认为的说话人**（`说话人：某某`，拿不准写 `说话人：不明`），
服务端拿它跟两个**确定**的参照物比对：

- **引导语自己的主语**（模型自报的专名里，出现在引导语**开头**的那个）；
- **本段的归属语**（引号外点名的人 —— 本段明说谁在说）。

判得死的三种冲突（其余一律放过，只留痕）：

| # | 冲突 | 依据 |
|---|---|---|
| 1 | 声明「拿不准」，却把**主语**写成了某个角色 | 拿不准 ⇒ 不该安名字 |
| 2 | 引导语开头的主语是角色表里的人，但不是声明的说话人 | 主语 ≠ 说话人 |
| 3 | 本段归属语点了**唯一一个人**，声明的说话人却是别人 | 本段明说谁在说 |

判不准的**放过**（误杀会堵死真活）：模型没声明、说话人写的是代词/泛称、
归属语点了 ≥2 个人、说话人不在角色表里、**角色表拿不到**（三条都以角色名作锚）。

⚠️ 「开头」和「角色表」这两个限定是**真跑逼出来的**：第一版没有它们，
121 次候选里拒掉的 6 条**逐条看全是误杀**（代词主语 + 宾语里带人名、
把物件当人、把泛称当人名）。详见 `_speaker_check` 的 docstring。

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

#: 「补到带的上沿」之后**再多给几处余量**。
#:
#: ## 为什么不是「补到均值」（2026-10-27 量出来的）
#:
#: 补 L 处 ⇒ 裸台词段少 L、含引号段数不变 ⇒ `ratio' = (b-L)/q`。
#: 所以在**同一份真实正文**上，两个目标差多少是可以直接算的
#: （两份真实产物是同一批正文的两个快照，**去重后 7 段**；其中机械合并后
#: 仍出带的 5 段，两份快照各自跑一遍，每段数字一致）：
#:
#: | 补到哪 | 每段调用 | 补完是否进带（按采纳率 0.94 估） |
#: |---|---|---|
#: | 均值（旧行为） | **11.7 / 11.8** | 5/5 |
#: | 带的上沿 p90 | 4.1 / 4.5 | **2/7、2/6 ✗**（不留给量，采纳损失把它顶出带） |
#: | **上沿 + 2 处**（单轮） | **5.6 / 5.8** | 5/5（按 94% 采纳率**估**的） |
#: | 均值与上沿的中点 | 7.9 / 8.2 | 5/5 |
#:
#: ⇒ 目标是「**进带**」而不是「打到均值」，所以补到上沿就够；
#: 但**必须留给量** —— 采纳率不是 100%，补 8 处只成 7~8 处，
#: 正好落在上沿附近会被顶出去。2 处约等于一次小幅补偿。
#:
#: 代价对比：11.7 → 5.6 次/段（**-52%**）。
#: ⚠️ 这个「2 处」是按**采纳率 94%** 估的 —— 换个模型不一定是这个数，
#: 所以下面还有一层**自适应**兜底（`LEAD_IN_MAX_ROUNDS`）。
LEAD_IN_SAFETY_SLOTS = 2

#: 最多补几轮（每轮都是「补到上沿 + 余量」）。
#:
#: ## 为什么要有「第二轮」（2026-10-28 真跑逼出来的）
#:
#: 上面那个「余量 2 处」是按**采纳率 94%** 估的。换成真实中转站的模型，
#: 实测采纳率只有 **67%~82%** —— 于是余量 2 处**有时候不够**：
#: 真跑有两段只差 **1 个槽位**（0.350 / 0.352，上沿 0.335）就掉在带外。
#:
#: ⇒ 固定余量救不了这种情况 —— **采纳率是模型决定的，不是常量**，
#: 写死一个数只能赌某个模型；换模型就会翻车。
#:
#: ⇒ 改成**自适应**：补一轮 → 量一下 → 还差就**再补一轮**
#: （只补**剩下的**那些段：已经补过的段不再是「裸台词段」，不会被重复挑中）。
#: 代价只在**真的不够**时才付（实测 5 段里 2 段要第二轮）。
LEAD_IN_MAX_ROUNDS = 3

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
    「赵长安揉了下鼻梁：“得看什么账。”」整段比是 0.235（漏检），
    只比引号外的「赵长安揉了下鼻梁」是 0.286（抓到）。
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
另外再另起一行写 `说话人：某某` —— 写**这句台词是谁说的**（引导语的主语就该是他）；
拿不准就写 `说话人：不明`，这时引导语里**不要**出现人名，只用「他/她/那人」这类指代。

【上文】
{before}

【这句台词】
{dialogue}

【下文】
{after}"""


#: 模型自报「拿不准」的几种写法（都当「不明」处理）。
_UNKNOWN_SPEAKERS = ("不明", "不知道", "不确定", "无人", "无", "待定")

#: 代词 / 泛指 —— 拿它当说话人无法核对（判不准，放过）。
_PRONOUN_SPEAKERS = ("他", "她", "它", "那人", "此人", "对方", "两人", "有人")


def _split_declared_speaker(raw: str) -> tuple[str, str]:
    """把模型输出拆成 (叙述句, 自报的说话人)。

    模型被要求在句后另起一行写 `说话人：某某`（拿不准写 `说话人：不明`）——
    这样「它以为是谁在说话」由**模型自己声明**，服务端拿它跟确定的东西比对，
    **不用猜谁是说话人**（猜 = 判不准 = 误杀）。
    """
    text = (raw or "").strip()
    speaker = ""
    kept: list[str] = []
    for ln in (x.strip() for x in text.split("\n")):
        if not ln:
            continue
        m = re.match(r"^说话人[：:]\s*(.+)$", ln)
        if m:
            speaker = m.group(1).strip()
            continue
        kept.append(ln)
    return "\n".join(kept).strip(), speaker


def _speaker_check(
    lead: str,
    speaker: str,
    declared_nouns: Sequence[str],
    attribution: str,
    context: str,
    character_names: set[str] | None,
) -> tuple[str | None, str | None]:
    """判据 7：说话人一致。返回 **(冲突原因, 备注)**。

    冲突原因非空 ⇒ 拒这一处引导语；备注非空 ⇒ 记进诊断（**不拦**）。

    ## 判得死 ⇒ 拒（三种）

    1. 声明「拿不准」，却把**主语**写成了某个角色 —— 拿不准就不该安名字；
    2. 引导语**开头**的主语是角色表里的人，但不是声明的说话人；
    3. 本段归属语点了**唯一一个人**，而声明的说话人是别人。

    ## 判不准 ⇒ 放过（只记备注）

    - 模型没声明说话人；说话人写的是代词/泛称；
    - 归属语点了 ≥2 个人（不知道谁在说）；
    - 说话人不在角色表里（可能是泛称，也可能是模型编的 —— 分不出来就不拦）；
    - **角色表拿不到**（三条都失效 —— 它们全都以角色名作锚）。

    ## 为什么「开头」和「角色表」这两个限定不能省

    2026-10-27 第一次真跑（9 段真实正文、121 次候选）时，这两条限定还没有 ⇒
    判据 7 拒掉的 6 条**逐条看全是误杀**：

    - 「他抬眼看了沈铁山一下。」（说话人：不明）—— 主语是「他」，「沈铁山」是**被看的人**；
    - 「那人抬起头，望向周郎中。」（说话人：不明）—— 同上；
    - 「他垂下眼，目光落向盒子里。」（说话人：不明）—— 「盒子」是**物件**，不是人；
    - 「门内的人举起油灯。」（说话人：门内的人）—— 「门内的人」是**泛称**，不是人名。

    ⇒ 规则 1 改成「**开头**点了**角色表里的人**才算」；
    「说话人不在场」**降级成备注** —— 泛称与编造的人名分不出来，
    按项目纪律「判不准的别当场拒」，不拦、只留痕。
    """
    if not speaker:
        return None, None                   # 没声明 ⇒ 判不准
    speaker = speaker.strip()
    roster = character_names or set()
    nouns = [n for n in (declared_nouns or []) if n]

    # 引导语**开头**的主语（模型自报的名字里，开头的那个）
    subject = None
    for n in nouns:
        if (lead or "").startswith(n) and (subject is None or len(n) > len(subject)):
            subject = n
    subject_is_person = bool(subject) and subject in roster
    speaker_is_person = speaker in roster

    # 1. 拿不准却把主语安成了某个角色
    if speaker in _UNKNOWN_SPEAKERS or speaker in _PRONOUN_SPEAKERS:
        if subject_is_person:
            return f"拿不准是谁在说，却把主语写成了 {subject}", None
        return None, None

    # 2. 主语是角色表里的人，但不是声明的说话人
    if subject_is_person and subject != speaker:
        return f"引导语写的是 {subject}，说话人却是 {speaker}", None

    # 3. 本段归属语点了唯一一个人 ⇒ 那就是说话人
    attr_names = {n for n in roster if n and n in (attribution or "")}
    if len(attr_names) == 1 and speaker_is_person:
        y = next(iter(attr_names))
        if y != speaker:
            return f"本段明说 {y} 在说，声明的说话人却是 {speaker}", None

    # 判不准：说话人不在角色表里（泛称 or 模型编的）—— 只留痕，不拦
    if roster and not speaker_is_person and speaker not in (context or ""):
        return None, f"说话人 {speaker} 不在角色表里，也不在这一段里"
    return None, None


def should_fill_lead_ins(ratio: float, *, p10: float, p90: float) -> bool:
    """这段正文**该不该**花钱补引导语。

    ## 为什么只看**上沿**（这道闸以前写成「不在带内」）

    补引导语的效果是单向的：它只会让「裸台词段 / 含引号段」**变小**。
    ⇒ 比值**低于 p10**（裸台词太少）时补写只会把它压得更低、离带更远，
    那次调用是纯浪费（虽然末端闸门会把它丢掉，钱已经花了）。

    ⇒ 只有 `ratio > p90`（裸台词**太多**）才值得补。

    ## 为什么正好等于 p90 也不补

    落在带上沿已经在带内；再补就进到带中间，等于白付调用去买「更靠中间」。
    本次改动的目标就是「进带」，不是「打到均值」。
    """
    return float(ratio) > float(p90)


def pick_lead_in_slots(
    paragraphs: Sequence[str],
    target_ratio: float,
    *,
    max_slots: int = MAX_LEAD_INS,
    safety_slots: int = 0,
) -> list[int]:
    """挑哪些裸台词段要补引导语。返回段落下标。

    ## 怎么挑

    1. 先算还差几段：裸台词段 n、含引号段 q ⇒ 目标裸台词段数 = **floor**(target×q)，
       差多少就补多少；
       ⚠️ **必须向下取整**（不是 round）—— 详见代码里的注释：
       round 会允许「补完仍比上沿高半段」的结果，而且下一轮会误判成
       「已经在目标附近」不再补 ⇒ 卡在带外（真跑抓到的）。
    2. **再多给 `safety_slots` 处**（只在确实要补时给）—— 采纳率不是 100%，
       补 8 处只成 7~8 处，正好冲着上沿去就会被顶出带（见
       `LEAD_IN_SAFETY_SLOTS` 里那张实测表）；
    3. **均匀摊开**（每隔几个挑一个）—— 参考书里裸台词段是**散着**的
       （B→B 只占 176/2662），全挤在一处会像补过。

    Args:
        target_ratio: 补到哪个比例为止。**线上传的是带的上沿**（p90），
            不是均值 —— 目的是「进带」，不是「打到平均」。
        safety_slots: 额外的余量处数。0 = 不留给量（默认，向后兼容）。
    """
    ps = list(paragraphs)
    quote_n = sum(1 for p in ps if _has_quote(p))
    bare = [i for i, p in enumerate(ps) if is_bare_paragraph(p)]
    if not quote_n or not bare:
        return []
    # ⚠️ 这里**必须向下取整**（floor），不能用 round：
    #   round(0.3346 * 68) = 23 段 ⇒ 允许的比值 23/68 = **0.3382 > 上沿 0.3346**
    #   ⇒ 补完仍然在带外，而且下一轮会认为「已经在目标附近」不再补 ⇒ 卡在带外。
    #   floor(0.3346 * 68) = 22 段 ⇒ 22/68 = 0.3235 ≤ 上沿 ✓
    #   （这是真跑抓到的：gid4 差 0.003 掉在带外，根因就是这个取整方向。）
    allowed_bare = int(float(target_ratio) * quote_n)
    need = len(bare) - allowed_bare
    if need <= 0:
        return []
    need += max(0, int(safety_slots))
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
    safety_slots: int = 0,
    proper_nouns: set[str] | None = None,
    corpus_text: str | None = None,
    character_names: set[str] | None = None,
    max_lead_ins: int = MAX_LEAD_INS,
    concurrency: int = LEAD_IN_CONCURRENCY,
) -> tuple[list[str], dict]:
    """给裸台词段补引导语。返回 (新段落列表, 诊断)。

    ## 流程

    1. `pick_lead_in_slots` 选点（纯机械）；
    2. 并发调模型写引导语（低温）；
    3. **逐条验收**（七道判据，见模块 docstring）；
    4. 通过的并进那一段（冒号形态 `引导语：“台词”`）。

    ## `target_ratio` / `safety_slots` 是干什么的

    线上传的是**带的上沿 p90** + `LEAD_IN_SAFETY_SLOTS` 处余量 ——
    目的是「进带」而不是「打到均值」，`-52%` 的调用量就是这么来的
    （见 `LEAD_IN_SAFETY_SLOTS` 的实测表）。这两个参数只是「补到哪」的旋钮，
    判据与形态不受影响。

    ## `character_names` 是干什么的

    判据 7（说话人一致）要拿「角色表」当参照：说话人得是**这个故事里的人**，
    归属语点了谁也要靠它认。拿不到（空集）⇒ 判据 7 的第 3/4 条自动失效，
    只剩第 1/2 条（都是模型自报 vs 模型自报，不需要外部参照）。

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
    info = {"applied": False, "inserted": 0, "slots": 0, "rejected": [],
            "fill_ratio": round(float(target_ratio), 4),
            "safety_slots": int(safety_slots)}
    if not ps:
        return ps, info

    slots = pick_lead_in_slots(
        ps, target_ratio, max_slots=max_lead_ins, safety_slots=safety_slots,
    )
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
        # 先摘掉「说话人：」那行（_clean_beat 只认「专名：」那行）
        raw, speaker = _split_declared_speaker(raw)
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
            corpus_text=corpus_text,
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
        # 判据 7：说话人一致
        conflict, note = _speaker_check(
            lead, speaker, declared,
            _outside_of_paragraph(dialogue),
            f"{before}\n{dialogue}\n{after}",
            character_names,
        )
        if conflict:
            info["rejected"].append({"index": idx,
                                     "reason": f"说话人不对：{conflict}",
                                     "text": lead[:40]})
            continue
        if note:
            info.setdefault("notes", []).append({"index": idx, "note": note,
                                                 "text": lead[:40]})

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


def fill_lead_ins_until_in_band(
    paragraphs: Sequence[str],
    provider,
    *,
    p10: float,
    p90: float,
    safety_slots: int = LEAD_IN_SAFETY_SLOTS,
    max_rounds: int = LEAD_IN_MAX_ROUNDS,
    **kwargs,
) -> tuple[list[str], dict]:
    """补引导语，直到**进带**为止（自适应轮数）。返回 (新段落列表, 诊断)。

    ## 为什么要有轮数

    单轮的余量（`safety_slots`）是按**某个采纳率**估出来的，而采纳率是
    **模型决定的常量**：同一个模型上 94%，换个模型可能只有 67%。
    ⇒ 写死余量只能赌某个模型；真跑（2026-10-28）就撞上了：两段只差
    **1 个槽位**（0.350 / 0.352，上沿 0.335）掉在带外。

    ⇒ 改成**补一轮、量一次、还差就再来一轮**：
    第二轮只补**剩下的**裸台词段（已经补过的段不再是「裸台词段」，
    不会被重复挑中），所以不会补重、也不会重复花钱。

    ## 什么时候停

    - 已经进带（`ratio <= p90`）⇒ 立刻停，不再多花钱；
    - 这一轮**一处都没成** ⇒ 停（再来一轮也是白花钱：模型就是写不出来）；
    - 轮数用完 ⇒ 停，如实把「还差多少」写进诊断（**不假装成功**）。

    ## 诊断里会写什么

    `rounds`（跑了几轮）、`inserted`（一共补了几处）、`slots`（一共花了几次调用）、
    `final_ratio`（最后的比值）、`in_band`（有没有进带）、`rounds_detail`（逐轮）。
    """
    from app.style.form import measure_form

    ps = list(paragraphs)
    info = {"applied": False, "inserted": 0, "slots": 0, "rounds": 0,
            "rejected": [], "notes": [], "rounds_detail": [],
            "safety_slots": int(safety_slots)}
    if not ps:
        return ps, info

    for rnd in range(1, max(1, int(max_rounds)) + 1):
        ratio = measure_form("\n".join(ps))["bare_ratio"]
        if ratio <= p90:
            break
        nxt, li = insert_lead_ins(
            ps, provider, target_ratio=p90, safety_slots=safety_slots, **kwargs
        )
        inserted = int(li.get("inserted") or 0)
        info["rounds"] = rnd
        info["slots"] += int(li.get("slots") or 0)
        info["inserted"] += inserted
        info["rejected"] += list(li.get("rejected") or [])
        info["notes"] += list(li.get("notes") or [])
        info["rounds_detail"].append({
            "round": rnd,
            "ratio_before": round(ratio, 4),
            "slots": int(li.get("slots") or 0),
            "inserted": inserted,
            "reason": li.get("reason"),
        })
        if inserted <= 0:
            # 一处都没成 ⇒ 再来一轮也是白花钱（模型写不出来 / 全被判据拒了）
            break
        ps = nxt

    final_ratio = measure_form("\n".join(ps))["bare_ratio"]
    info["final_ratio"] = round(final_ratio, 4)
    info["in_band"] = bool(float(p10) <= final_ratio <= float(p90))
    info["applied"] = info["inserted"] > 0
    if not info["in_band"]:
        # **如实说没进带** —— 不许「补了几处」就当成成功。
        if info["inserted"] > 0:
            info["reason"] = (
                f"补了 {info['inserted']} 处仍未进带"
                f"（{final_ratio:.3f} > 上沿 {float(p90):.3f}）"
            )
        else:
            info["reason"] = (
                f"一处都没补上，仍未进带（{final_ratio:.3f} > 上沿 {float(p90):.3f}）"
            )
    return ps, info

