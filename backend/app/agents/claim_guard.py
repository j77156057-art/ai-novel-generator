"""答案的「无证据支撑断言」检测器（P1-B，纯确定性，**不调模型**）。

## 为什么需要它

真 ReAct 循环能保证「工具调用有轨迹」，但保证不了「答案只讲观察里有的东西」。
QA 在真实模型上打了 14 次 `/qa`，**7 次（50%）** 答案含观察无法支撑的断言，
而且全部 `status="success"`：

- 3 次只读了 `read_generation_segments`，就在答案里写「与知识图谱、事实账本一致」
  —— 它**从未**查过图谱或账本；
- 2 次把改写后的正文当答案返回，没有「建议稿 / 未写入」标注，`pending_actions=[]`；
- 2 次正文里埋了注入串，却答「没有异常内容」；
- 1 次零工具调用直接 `final="已完成，已经帮你改好了"` → `success` + `evidence=[]`。

这些都是「声称做过没做过的事」。「降级诚实」（模型失败就 failed）守住了，
但「成功但内容无据」这条路没有守——同一个红线的另一半。

## 设计原则

1. **纯函数、闭集词表、零模型调用**：判定必须是可单测、可复现、可解释的。
   用模型来判「模型有没有编」会引入第二处不可靠。
2. **只标注不阻断**：检测结果用于给答案加前缀 + 把 run 标成 failed，
   **不**删改模型答案正文——用户仍能看到模型说了什么，同时看到「未经核对」。
3. **提卡口径不在这里**：`rewrite_intent` 只用于「答案要不要标注建议稿」，
   **不**用于决定提不提卡片。提卡的唯一真源是
   `app/agents/pending_actions.py`（本模块不 import 它，也不复制它的关键词表
   以外的判定逻辑）。两个地方各写一套提卡判据必然漂移，所以这里连
   「什么算明确生成指令」都不判断，只判断「用户在要求改写」。

## 五类判定（全部闭集）

| kind | 触发词（答案里） | 放行条件 |
|---|---|---|
| `write_done` | 已改好/已经改好/已完成修改/已写入/已保存/已更新正文/修改已完成 | **无**——ReAct 没有任何写路径，一律 unsupported |
| `consistency` | 一致/相符/吻合/对得上/没有冲突/无冲突 | `executed_tools` ∩ 核对类工具非空 |
| `absence` | 没有异常/未发现异常/无异常/没有问题/未见明显问题 | `executed_tools` ∩ 审核类工具非空 |
| `numeric` | 每个长度 ≥2 的数字串 | 该数字串出现在（观察 excerpt ∪ 用户 query）里 |
| （`rewrite_intent`）| —— | 用户 query 含 改成/改为/修改/替换/删掉/删除/重写/改写 |

`numeric` 是最有效的一条：弱模型最爱编「提及 10 次」（实际 60 次）、
「共 3 条事实」（从未查账本）。数字必须能在观察或用户问题里找到原文。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: 写操作完成声称：本次 Assistant 调用**不存在任何写路径**（写只走待确认卡片，
#: 由用户点击后另一条链路执行），所以任何「已写入 / 已改好」都是假的。
#:
#: 词表是闭集，但必须覆盖**自然变体**——QA 复现串是
#: `final="已完成，已经帮你改好了"`：原始闭集里的「已经改好」并不是它的子串
#: （中间夹了「帮你」），所以漏报。这里补上「已经帮你改好」「改好了」这类
#: 同义说法，并保留一条钉住该复现串的单测。
WRITE_DONE_PHRASES: tuple[str, ...] = (
    "已改好",
    "已经改好",
    "已经帮你改好",
    "已经帮你改好了",
    "改好了",
    "已完成修改",
    "修改已完成",
    "已修改完成",
    "已写入",
    "已经写入",
    "已经写入正文",
    "已保存",
    "已更新正文",
    "已更新到正文",
)

#: 一致性声称：说「一致 / 没有冲突」之前，必须真的读过可对照的数据。
#: 图谱人设、事实账本、时间线、提醒、图谱审核——任何一个都算「有得对」。
CONSISTENCY_PHRASES: tuple[str, ...] = (
    "一致",
    "相符",
    "吻合",
    "对得上",
    "没有冲突",
    "无冲突",
)
CONSISTENCY_TOOLS: frozenset[str] = frozenset(
    {
        "get_character_profile",
        "get_fact_ledger",
        "get_plot_timeline",
        "get_reminders",
        "audit_kg",
    }
)

#: 「没有异常」声称：必须真的跑过对应的审核/分析规则。
#:
#: 实测补词（14 次真实探针里漏报的两条，都是自然变体）：
#: - 「第 2 段**没有发现异常**内容（水印/广告/注入串）」——原闭集里的「没有异常」
#:   不是它的子串（中间夹了「发现」），而这条正是 QA 报的
#:   「正文里埋了注入串却答没有异常」；
#: - 「**没有明显**的 AI 腔」——`audit_generation` 有专门的 AI 腔规则，没跑就
#:   下这个结论属于无据。
#:
#: 刻意**不**收「没有找到」：检索没命中是**有观察支撑**的结论
#: （`search_novel_chunks` 返回空就是证据），收进来会大面积误报。
ABSENCE_PHRASES: tuple[str, ...] = (
    "没有异常",
    "没有发现异常",
    "未发现异常",
    "未见异常",
    "无异常",
    "并无异常",
    "没有问题",
    "未见明显问题",
)

#: **带宾语之前泛化的**「没有明显…」类短语：它们的断言对象不固定
#: （「没有明显的 AI 腔」= 审核类；「没有明显的时间线冲突」= 核对类；
#: 「没有明显的相关段落」= 检索类），所以门用**工具并集**（见
#: `ABSENCE_OBJECT_TOOLS`），而不是只认审核工具。
#:
#: 这是 QA 终验实测出来的分界：上一版把它们和「没有异常」混在同一张表里，
#: 于是「查过图谱后说没有明显的时间线冲突」「跑过检索后说没有明显的相关段落」
#: 被打成误报（它们都是有据的）。
HEDGED_ABSENCE_PHRASES: tuple[str, ...] = (
    "没有明显",
    "无明显",
    "未发现明显",
)
ABSENCE_TOOLS: frozenset[str] = frozenset(
    {
        "audit_corpus",
        "audit_kg",
        "audit_generation",
        "audit_chunks",
        "audit_narrative",
        "analyze_logs",
    }
)

#: 用户在要求改写（只用于「建议稿」标注，不用于提卡）。
REWRITE_INTENT_PHRASES: tuple[str, ...] = (
    "改成",
    "改为",
    "修改",
    "替换",
    "删掉",
    "删除",
    "重写",
    "改写",
)

#: kind → 给人看的中文名（拼进 answer 前缀，避免出现英文枚举值）。
KIND_LABELS: dict[str, str] = {
    "write_done": "声称已写入正文（本次没有任何写权限）",
    "consistency": "声称与图谱/账本一致（本次未读取对照数据）",
    "absence": "声称没有异常（本次未运行对应审核规则）",
    "existence": "声称图谱/账本里有记录（本次未读取图谱或账本）",
    "numeric": "引用了观察与问题里都没有的数字",
}

# --------------------------------------------------------------------
# 正向存在性断言（P2，QA 终验抓到的「同类里漏掉的」）
# --------------------------------------------------------------------
#
# 实测原文（39024e1 上，QA 跑出来的）：
#
#   query : 第 1 段里出场的角色『小杨』在图谱里有记录吗？
#   calls : ['read_generation_segments']      ← 只读正文，从未查图谱
#   answer: 第 1 段里出场的角色『小杨』在图谱里有记录。   ← 事实错误（小杨不在图谱）
#   status: success | trustworthy: True | confidence: 0.6 | unsupported: []
#
# 这是「**错答案 + 报 trustworthy**」，正是这套护栏要消灭的形态。原有 5 类闭集
# 里没有「正向存在性」（consistency 只认「一致/相符」，absence 只认「没有异常」），
# 所以完全裸奔。
#
# 门控放在 **KG 类名词**上：`(?:图谱|账本|时间线|KG)` + ≤8 字 + 存在性短语。
# 这样「正文里有记录」不会被误伤（没有 KG 类名词）；「查过图谱再说有记录」也不会
# 被误伤（`executed_tools ∩ CONSISTENCY_TOOLS` 非空）。QA 已验证该形状零误报。
#
# 名词与存在性短语之间允许插**副词**（实测漏报：「图谱里确实有记录」「图谱里也有
# 记录」都不命中 —— 「里确实有记录」里不含连续的「里有记录」）。副词是有限闭集，
# 不写成 `.*` —— 窗口仍限死 8 字，误报面不会变大。
EXISTENCE_RE = re.compile(
    r"(?:图谱|账本|时间线|KG)[^。，；]{0,8}?(?:确实|真的|已经|也|肯定|就)?"
    r"(?:有记录|中有该条目|已收录|能查到)"
)

# --------------------------------------------------------------------
# 带宾语的缺席式断言（P2，QA 给的可测原型 TP=6 FP=0 FN=0）
# --------------------------------------------------------------------
#
# 上一轮我自曝的 3 条漏报都是这个形状：`没有发现与李浩人设的冲突` /
# `没有发现正文与图谱的矛盾` / `没有重复的句子` —— 否定词与「冲突/矛盾」之间
# 夹了一段宾语，纯子串闭集覆盖不到。
#
# 两条保护，避免重演「用误报换漏报」：
#   1. `gap` 里出现「排除/避免/否认/否定/确认/断定/保证」→ **不算缺席断言**
#      （`不能排除冲突`、`没有排除冲突的可能` 说的是「可能有冲突」）；
#   2. 工具并集门（CONSISTENCY ∪ ABSENCE ∪ {search_novel_chunks}）非空 → 不拦
#      （查过图谱/跑过审核/跑过检索再说「没有明显冲突」是有据的）。
#      **刻意不放宽成「读过任意工具」**：QA 判定我上轮自曝的 B、C 两条本来就不该拦
#      （它们分别读了图谱/正文），真正需要补的只有「没读任何一侧却断言无冲突」。
#
#   3. 按**名词**分门（QA 窄范围终核）：`重复` 的判断对象是被读的正文本身，所以它的
#      门多认 `read_generation_segments`（见 `ABSENCE_REPETITION_TOOLS`）；
#      而 `冲突/矛盾/不一致/不符` 要求读过**对照侧**，门不变。
ABSENCE_WITH_OBJECT_RE = re.compile(
    r"(?:没有|没能|未|并无|无(?!法))(?:发现|出现|看到|存在|看出|查到)?"
    r"(?P<gap>[^。，；！？\n]{0,15}?)(?P<noun>冲突|矛盾|重复|不一致|不符)"
)
#: gap 里出现这些词 → 语义是「不能排除 / 不要否认」，不是「不存在」。
ABSENCE_OBJECT_BLOCK: tuple[str, ...] = (
    "排除",
    "避免",
    "否认",
    "否定",
    "确认",
    "断定",
    "保证",
)
#: 带宾语缺席式断言的放行工具并集（**冲突类**名词用）。
ABSENCE_OBJECT_TOOLS: frozenset[str] = (
    CONSISTENCY_TOOLS | ABSENCE_TOOLS | {"search_novel_chunks"}
)

#: 「重复」类名词的放行集合：在并集之外**再多认** `read_generation_segments`。
#:
#: 为什么单独开一条（QA 窄范围终核实测的误报，约 8% 真实流量）：
#:
#:   query : 第 1 段里有没有重复的句子？
#:   calls : ['read_generation_segments']     ← 真的读了被判断的那段正文
#:   answer: 第 1 段中没有重复的句子。
#:   → status=failed + ⚠ 前缀（误报）
#:
#: `重复` 的判断对象**就是被读的那段文本本身**，`read_generation_segments` 正是
#: 那一侧的证据；而「冲突/矛盾/不一致/不符」的判断对象在**另一侧**（图谱/账本/
#: 时间线），读正文不构成对照，所以那四个名词的门**保持不变**。
#: 按名词分门，不整体放宽——整体放宽会把「没有发现正文与图谱记录之间的矛盾」
#: 这条真命中一起放掉（QA 已确认它必须继续拦）。
REPETITION_NOUN = "重复"
ABSENCE_REPETITION_TOOLS: frozenset[str] = ABSENCE_OBJECT_TOOLS | {
    "read_generation_segments"
}

#: 「重复」命中时专用的中文提示。
#:
#: 原来 absence 统一说「本次未运行对应审核规则」，但**「重复」根本没有对应审核
#: 规则**（`audit_chunks` 查的是切分边界，不是重复句），这句会把用户指向一个不
#: 存在的工具。按名词给文案。
ABSENCE_REPETITION_LABEL = "声称文本中没有重复（本次未读取被检查的正文）"

#: 转折/确信副词：出现即把「否定」翻回来（P2，QA 实测补丁 0 新误报）。
#:
#: 现有否定窗口在**同句转折**上会漏：`虽无法完全确认但确实一致` 的窗口里同时有
#: 「无法」和「确实」，只按 `NEGATION_MARKERS` 判会整句豁免——但那句话的落点是
#: 「确实一致」，是断言。所以窗口内出现这些词 → **不豁免**（按断言算）。
TURN_PRONOUN_MARKERS: tuple[str, ...] = (
    "但",
    "却",
    "不过",
    "然而",
    "实际上",
    "确实",
    "反而",
)

#: 数字串：长度 ≥2 的连续阿拉伯数字（"第 1 段" 的 1 不算，"60 次" 的 60 算）。
_NUMBER_RE = re.compile(r"\d{2,}")

#: 量词前的一位/多位数字：`4 个角色节点` / `3 条事实` / `2 处冲突`。
#: 一位数在这里必须查（P3 实测：调完 audit_kg 答「共有 4 个角色节点」，真值 3286，
#: 却因为 `\d{2,}` 不看一位数而判 trustworthy=True）。
#: 只覆盖阿拉伯数字 + 中文量词；**中文数字（「三条」「十次」）不覆盖**——那需要
#: 中文数字解析，收益低、误报风险高，明确写在注释里而不是假装覆盖了。
_QUANTIFIED_NUMBER_RE = re.compile(
    r"(\d+)\s*(?:个|条|次|处|章|段|本|名|位|种)"
)

#: 单个 kind 最多记多少条（防一次答案把 trace 撑成几万字）。
_MAX_PER_KIND = 8

#: 否定式 / 疑问式豁免（P2-①，QA 二轮的核心发现）。
#:
#: ## 为什么必须做
#:
#: QA 用闭集量化了 7 条变体，**7/7 全是误报**：
#:   `无法确定…是否对得上` / `无法判断…是否一致` / `尚不能确认…是否吻合` /
#:   `没有证据表明二者一致` / `不能断定…是否相符` /
#:   `信息不足，无法核对是否对得上` / `我不知道是否一致`
#:
#: 这类话的意思是「**我没查，不能下结论**」——和「我查了，一致」正好相反。
#: 只做子串匹配会把「我不知道」判成「我声称一致」，方向完全反了；而「我不知道」
#: 恰恰是弱模型最常见的诚实输出，真实流量里必现。
#:
#: ## 判定方式：逐次、看前 20 字符窗口
#:
#: 对每个命中的短语**单独**取它前面 20 字符，若含任一标记 → 这一次命中不算断言。
#: 逐次而不是整句：`无法确认 A 是否一致，但事实账本与正文一致` 里第二个「一致」
#: 仍然要命中（它前面没有否定标记），整句判定会把它一起豁免掉。
NEGATION_MARKERS: tuple[str, ...] = (
    "无法",
    "不能",
    "难以",
    "不确定",
    "是否",
    "能否",
    "尚不",
    "尚未",
    "不知道",
    "无从",
    "不足以",
    "未能",
    "未敢",
    # 「没有证据表明二者一致」：不是断言，是「拿不出证据」。
    "没有证据",
    "无证据",
)

#: 否定标记的判定窗口（命中短语之前的字符数）。
_NEGATION_WINDOW = 20

#: 窗口在**最近一个分句边界**处截断。
#:
#: 只按固定 20 字符取窗口会跨逗号把前一句的否定词带过来：
#: `无法确认图谱记录是否一致，但事实账本与正文确实一致` 里第二个「一致」前 20 字符
#: 已经跨过了「无法」，于是真断言被误豁免——等于用消误报换漏报。按分句边界截断后，
#: 窗口只看**本分句**的否定词，语义上正是中文否定的作用域。
_CLAUSE_SEPARATORS: tuple[str, ...] = ("。", "，", "；", "！", "？", "、", "\n", ",", ";", "!", "?")

#: 疑问标记：query 里出现这些 → 用户是在**问**要不要改，而不是**要求**改。
#: 实测误伤：`第 1 段有没有需要修改的地方？` 被贴了「（以下仅为建议稿，尚未写入
#: 正文）」，可答案只是「建议压缩前两句」的分析。
QUESTION_MARKERS: tuple[str, ...] = (
    "有没有",
    "是否",
    "哪些",
    "怎么",
    "如何",
    "能否",
    "哪里",
    "吗",
)


def negation_window(text: str, start: int) -> str:
    """命中位置之前的判定窗口：最多 20 字符，且在最近一个分句边界处截断。"""
    window = text[max(0, start - _NEGATION_WINDOW) : start]
    cut = max((window.rfind(sep) for sep in _CLAUSE_SEPARATORS), default=-1)
    return window[cut + 1 :] if cut >= 0 else window


def _phrase_hits(text: str, phrases: Iterable[str]) -> list[str]:
    """返回**所有未被否定的**命中短语（按出现顺序、去重）。

    对每个短语的每一次出现，取它前 `_NEGATION_WINDOW` 个字符（按分句边界截断），
    若其中含 `NEGATION_MARKERS` 任一 → 这次出现是「无法判断」而不是「断言」，跳过。
    """
    hits: list[str] = []
    for phrase in phrases:
        start = text.find(phrase)
        while start != -1:
            if not is_negated(negation_window(text, start)):
                if phrase not in hits:
                    hits.append(phrase)
                break  # 该短语已判定为断言，后续同短语的出现无需再判
            start = text.find(phrase, start + 1)
    return hits


def is_negated(window: str) -> bool:
    """窗口里是否出现否定/疑问标记（公开给单测与调用方复用）。

    转折优先：窗口里一旦出现 `但/却/不过/然而/实际上/确实/反而`，说明这句话的
    落点被翻回来了（`虽无法完全确认但确实一致` 的落点是「确实一致」），
    此时**不**豁免——按断言算。
    """
    if any(marker in window for marker in TURN_PRONOUN_MARKERS):
        return False
    return any(marker in window for marker in NEGATION_MARKERS)


def _is_question(query: str) -> bool:
    """query 是否是「提问」而不是「指令」。"""
    text = (query or "").strip()
    if not text:
        return False
    if text.endswith(("？", "?")):
        return True
    return any(marker in text for marker in QUESTION_MARKERS)


def _numeric_evidence(answer: str, haystack: str) -> list[str]:
    """答案里「该有出处却没有」的数字。

    两条规则（任一命中即算无据）：
      1. 长度 ≥2 的连续数字必须能在 haystack 里找到原文；
      2. 量词前的一位/多位数字（`4 个` / `3 条` / `2 处`）同样必须在 haystack 里。

    只看「有没有出现」而不做数值比较：观察里写 `mentions: 60`、答案写 `60 次`
    —— 字符串里都有 `60`，通过；答案写 `10 次` 而观察只有 60 —— 不通过。
    数值比较需要知道字段语义，字符串包含已经足够抓住 QA 实测的两类编造
    （次数、节点数）。**中文数字不覆盖**（见 `_QUANTIFIED_NUMBER_RE` 注释）。
    """
    seen: list[str] = []

    def _check(token: str) -> None:
        if token in seen:
            return
        if token not in haystack:
            seen.append(token)

    for token in _NUMBER_RE.findall(answer or ""):
        _check(token)
        if len(seen) >= _MAX_PER_KIND:
            return seen
    for token in _QUANTIFIED_NUMBER_RE.findall(answer or ""):
        _check(token)
        if len(seen) >= _MAX_PER_KIND:
            return seen
    return seen


def detect_unsupported_claims(
    *,
    answer: str,
    query: str,
    executed_tools: set[str],
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    """检测答案里「观察支撑不了」的断言。

    Args:
        answer: 模型给出的最终回答（原文，未加任何前缀）。
        query: 用户问题原文。
        executed_tools: 本轮**真正执行成功**的工具名集合
            （`react.observations` 里 `ok=True` 的 `tool`）。
        observations: 全部观察（每项至少含 `excerpt`；额外键忽略）。

    Returns:
        `{"unsupported": [{"kind": str, "phrase": str}], "rewrite_intent": bool}`

    `unsupported` 为空且 `rewrite_intent` 为假时，这条答案才可以标成可信。
    注意：本函数**不**判断答案是否「正确」，只判断它是否「讲了没发生过的事」；
    「无法确定是否一致」这类**否定式/疑问式**表述不算断言（见 `NEGATION_MARKERS`）。
    """
    text = answer or ""
    query_text = query or ""
    tools = set(executed_tools or ())

    haystack_parts: list[str] = [query_text]
    for ob in observations or ():
        if not isinstance(ob, dict):
            continue
        haystack_parts.append(str(ob.get("excerpt") or ""))
    haystack = "\n".join(haystack_parts)

    unsupported: list[dict[str, str]] = []

    # 1) 写操作完成声称：无条件下不了台——本次调用没有任何写路径。
    #    注意也过否定豁免：「无法写入」不是「已写入」。
    for phrase in _phrase_hits(text, WRITE_DONE_PHRASES):
        unsupported.append({"kind": "write_done", "phrase": phrase})

    # 2) 一致性声称：必须先读过可对照的数据。
    if not tools & CONSISTENCY_TOOLS:
        for phrase in _phrase_hits(text, CONSISTENCY_PHRASES):
            unsupported.append({"kind": "consistency", "phrase": phrase})

    # 3) 「没有异常」声称：必须先跑过审核/日志分析。
    #    `没有证据表明二者一致` 这类同时含 absence/consistency 词的句子，
    #    两条规则各自走**自己的**否定豁免，不会互相放大。
    if not tools & ABSENCE_TOOLS:
        for phrase in _phrase_hits(text, ABSENCE_PHRASES):
            unsupported.append({"kind": "absence", "phrase": phrase})

    # 3b) 带宾语的缺席式断言 + 泛化的「没有明显…」：门是**工具并集**
    #     （核对类 ∪ 审核类 ∪ 检索），不是「任意工具」——见 ABSENCE_OBJECT_TOOLS。
    #     「重复」按名词单独开门（多认 read_generation_segments）：判断对象就是正文本身。
    object_gate_open = not tools & ABSENCE_OBJECT_TOOLS
    repetition_gate_open = not tools & ABSENCE_REPETITION_TOOLS
    if object_gate_open:
        for phrase in _phrase_hits(text, HEDGED_ABSENCE_PHRASES):
            unsupported.append({"kind": "absence", "phrase": phrase})
    for match in ABSENCE_WITH_OBJECT_RE.finditer(text):
        noun = match.group("noun") or ""
        if noun == REPETITION_NOUN:
            if not repetition_gate_open:
                continue
        elif not object_gate_open:
            continue
        gap = match.group("gap") or ""
        if any(block in gap for block in ABSENCE_OBJECT_BLOCK):
            continue  # 「不能排除冲突」= 可能有冲突，不是缺席断言
        phrase = match.group(0).strip()
        if any(item["kind"] == "absence" and item["phrase"] == phrase
               for item in unsupported):
            continue
        item = {"kind": "absence", "phrase": phrase}
        if noun == REPETITION_NOUN:
            item["label"] = ABSENCE_REPETITION_LABEL
        unsupported.append(item)
        if len(unsupported) >= _MAX_PER_KIND * 2:
            break

    # 3c) 正向存在性断言：`…在图谱里有记录` —— 必须先真的读过图谱/账本。
    #     门控在 **KG 类名词**上（`EXISTENCE_RE` 自带该前缀），所以「正文里有记录」
    #     不会被误伤；查过图谱再说有记录也不会被误伤（`CONSISTENCY_TOOLS` 门）。
    if not tools & CONSISTENCY_TOOLS:
        for match in EXISTENCE_RE.finditer(text):
            window = negation_window(text, match.start())
            if is_negated(window):
                continue  # `无法确认图谱里是否有记录` 不是断言
            unsupported.append({"kind": "existence", "phrase": match.group(0).strip()})
            if len(unsupported) >= _MAX_PER_KIND * 2:
                break

    # 4) 数字必须能在观察或用户问题里找到原文。
    for token in _numeric_evidence(text, haystack):
        unsupported.append({"kind": "numeric", "phrase": token})

    # 5) 改写意图（只用于「建议稿」标注；提卡口径以 pending_actions 为准）。
    #    疑问句不算：`有没有需要改的地方？` 是在问，不是在要求改写。
    rewrite_intent = bool(
        _phrase_hits(query_text, REWRITE_INTENT_PHRASES)
    ) and not _is_question(query_text)

    return {"unsupported": unsupported, "rewrite_intent": rewrite_intent}


def describe_kinds(unsupported: list[dict[str, Any]]) -> str:
    """把 unsupported 列表压成「去重保序的中文 kind 名」串（给前缀与 degraded_reason）。

    例：`声称已写入正文（本次没有任何写权限）；声称与图谱/账本一致（本次未读取对照数据）`

    单条可带 `label` 覆盖 kind 级默认文案（目前只有「重复」用它：absence 的默认
    提示说「未运行对应审核规则」，而重复并没有对应审核规则，会误导用户）。
    """
    labels: list[str] = []
    for item in unsupported or ():
        kind = str(item.get("kind") or "").strip()
        label = str(item.get("label") or "").strip() or KIND_LABELS.get(kind, kind)
        if label and label not in labels:
            labels.append(label)
    return "；".join(labels)


def describe_kind_keys(unsupported: list[dict[str, Any]]) -> str:
    """去重保序的英文 kind 串（给 `degraded_reason="unsupported_claims:<kinds>"`）。"""
    keys: list[str] = []
    for item in unsupported or ():
        kind = str(item.get("kind") or "").strip()
        if kind and kind not in keys:
            keys.append(kind)
    return ",".join(keys)
