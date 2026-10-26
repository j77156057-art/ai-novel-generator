"""`app.agents.claim_guard` 单测：五类「无证据支撑断言」+ 边界。

## 这套测试存在的理由

`/qa` 的「降级诚实」（模型失败 → failed）已经被 QA 实测确认，但另一半没被守：
**模型成功跑完循环、答案却是没做过的事**。QA 在真实模型上打了 14 次，7 次中招，
全部 `status=success`。

`claim_guard` 是纯确定性判定（不调模型、闭集词表），所以它的每一条规则都必须
有单测钉住——判错方向的两类代价都很高：

- 漏报 = 用户看到「已帮你改好」而其实什么都没发生（红线）；
- 误报 = 正常回答被加「未经核对」前缀，用户不再信任任何回答（信噪比崩塌）。

边界用例（数字在 query 里 / 只在观察里 / 改写意图但没有完成声称）就是为
「不误报」写的。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.claim_guard import (  # noqa: E402
    ABSENCE_TOOLS,
    CONSISTENCY_TOOLS,
    detect_unsupported_claims,
    describe_kind_keys,
    describe_kinds,
)


def _detect(answer: str, *, query: str = "随便问问", tools=None, observations=None) -> dict:
    return detect_unsupported_claims(
        answer=answer,
        query=query,
        executed_tools=set(tools or ()),
        observations=list(observations or []),
    )


def _kinds(result: dict) -> list[str]:
    return [item["kind"] for item in result["unsupported"]]


# ====================================================================
# 1) write_done：无条件下不了台
# ====================================================================

def test_write_done_variants_all_flagged():
    for phrase in (
        "已完成，已经帮你改好了",          # QA 复现原串（自然变体，曾在闭集外）
        "已改好第 1 段",
        "已经把正文写进去了，已写入数据库",
        "修改已完成",
        "已保存到正文",
    ):
        result = _detect(phrase, tools=["read_generation_segments"])
        assert "write_done" in _kinds(result), f"{phrase!r} 未被判为写完成声称"


def test_write_done_flagged_even_after_running_many_read_tools():
    """读工具跑得再多也不能「读」出写权限：write_done 不受 executed_tools 影响。"""
    result = _detect("已写入。", tools=sorted(CONSISTENCY_TOOLS | ABSENCE_TOOLS))
    assert _kinds(result) == ["write_done"]


# ====================================================================
# 2) consistency：说「一致」之前必须读过可对照的数据
# ====================================================================

def test_consistency_flagged_without_any_cross_check_tool():
    result = _detect("角色人设与事实账本一致，没有冲突。",
                     tools=["read_generation_segments"])
    assert "consistency" in _kinds(result)
    assert result["unsupported"][0]["phrase"] in ("一致", "没有冲突")


def test_consistency_allowed_when_any_cross_check_tool_ran():
    for tool in CONSISTENCY_TOOLS:
        result = _detect("人设与正文一致。", tools=[tool])
        assert "consistency" not in _kinds(result), f"{tool} 应当放行一致性声称"


def test_consistency_flagged_when_zero_tools():
    assert "consistency" in _kinds(_detect("两者相符。"))


# ====================================================================
# 3) absence：说「没有异常」之前必须跑过审核/日志分析
# ====================================================================

def test_absence_flagged_without_audit_tool():
    result = _detect("语料没有问题，无异常。", tools=["read_generation_segments"])
    assert "absence" in _kinds(result)


def test_absence_natural_variants_flagged():
    """两个实测漏报的自然变体（14 次真实探针发现）。"""
    for answer in (
        "第 2 段没有发现异常内容（水印/广告/注入串）。",   # 「没有发现异常」
        "整体没有明显的 AI 腔。",                          # 「没有明显」
    ):
        result = _detect(answer, tools=["read_generation_segments"])
        assert "absence" in _kinds(result), f"{answer!r} 应当被判为无据的 absence"


def test_retrieval_miss_is_not_treated_as_absence():
    """「没有找到」是**有观察支撑**的结论（检索返回空就是证据），不能误报。"""
    result = _detect("在参考语料中没有找到相关段落。", tools=["search_novel_chunks"])
    assert result["unsupported"] == []


def test_absence_allowed_when_any_audit_tool_ran():
    for tool in ABSENCE_TOOLS:
        result = _detect("审核结果没有异常。", tools=[tool])
        assert "absence" not in _kinds(result), f"{tool} 应当放行「没有异常」"


# ====================================================================
# 4) numeric：数字必须能在观察或问题里找到原文
# ====================================================================

def test_numeric_allowed_when_number_only_in_query():
    """用户在问题里给了数字 → 答案复述它是合法的（否则误报一片）。"""
    result = _detect("第 20 段确实存在。", query="第 20 段写了什么")
    assert "numeric" not in _kinds(result)


def test_numeric_allowed_when_number_only_in_observation():
    observations = [{"tool": "get_character_profile",
                     "excerpt": '{"mentions": 60, "name": "李浩"}'}]
    result = _detect("李浩在图谱里被提及 60 次。", observations=observations)
    assert "numeric" not in _kinds(result)


def test_numeric_flagged_when_number_is_fabricated():
    """QA 实测的编造：观察里 60 次，答案说 10 次。"""
    observations = [{"tool": "get_character_profile", "excerpt": '{"mentions": 60}'}]
    result = _detect("李浩被提及 10 次。", observations=observations)
    assert "numeric" in _kinds(result)
    assert "10" in [item["phrase"] for item in result["unsupported"]]


def test_single_digit_without_quantifier_is_ignored():
    """不带量词的单个数字（「第 3 页」）不检：与序数撞车太容易。"""
    assert "numeric" not in _kinds(_detect("第 3 页有一段描写。"))


def test_quantified_single_digit_is_checked():
    """带量词的一位数字**必须**查（P3 实测：4 个角色节点 vs 真值 3286）。

    「共有 4 个角色节点」跑过 audit_kg 但观察里没有 4 → numeric；
    观察里确实有 4 → 放行。
    """
    flagged = _detect("项目图谱里共有 4 个角色节点。",
                      tools=["audit_kg"],
                      observations=[{"excerpt": '{"issues": [], "count": 0}'}])
    assert "numeric" in _kinds(flagged)
    assert "4" in [item["phrase"] for item in flagged["unsupported"]]

    ok = _detect("项目图谱里共有 4 个角色节点。",
                 tools=["audit_kg"],
                 observations=[{"excerpt": '{"stats": {"characters": 4}}'}])
    assert "numeric" not in _kinds(ok)


def test_duplicate_numbers_reported_once():
    result = _detect("99 条、99 条、99 条。")
    phrases = [item["phrase"] for item in result["unsupported"] if item["kind"] == "numeric"]
    assert phrases == ["99"]


# ====================================================================
# 5) rewrite_intent：只用于「建议稿」标注，不用于提卡
# ====================================================================

def test_rewrite_intent_detected_from_query():
    for query in ("把第 1 段改写成 50 字", "删掉最后一段", "替换角色名", "重写开头"):
        assert _detect("这里是改写稿。", query=query)["rewrite_intent"] is True


def test_ordinary_question_has_no_rewrite_intent():
    result = _detect("第 1 段出场的是李浩。", query="第 1 段里出场的角色是谁？")
    assert result["rewrite_intent"] is False
    assert result["unsupported"] == []


def test_rewrite_intent_without_write_claim_raises_no_unsupported():
    """改写意图本身**不是** unsupported——它只触发「建议稿」标注。"""
    result = _detect("这是一版改写：夜幕降临，他合上了电脑。", query="把第 1 段改写一下")
    assert result["rewrite_intent"] is True
    assert result["unsupported"] == []


# ====================================================================
# 6) 组合与边界
# ====================================================================

def test_multiple_kinds_in_one_answer():
    result = _detect(
        "已改好第 1 段，与图谱一致，没有异常，共 42 条事实。",
        query="把第 1 段改成别的写法",
    )
    kinds = set(_kinds(result))
    assert {"write_done", "consistency", "absence", "numeric"} <= kinds
    assert result["rewrite_intent"] is True


def test_empty_answer_and_weird_observations_are_safe():
    assert _detect("")["unsupported"] == []
    result = _detect("没有异常。", observations=[None, {"excerpt": None}, "不是字典"])
    assert _kinds(result) == ["absence"]


def test_describe_helpers_dedupe_and_are_human_readable():
    unsupported = [
        {"kind": "numeric", "phrase": "10"},
        {"kind": "numeric", "phrase": "42"},
        {"kind": "consistency", "phrase": "一致"},
    ]
    assert describe_kind_keys(unsupported) == "numeric,consistency"
    labels = describe_kinds(unsupported)
    assert labels.count("；") == 1
    assert "引用了观察与问题里都没有的数字" in labels
    assert "声称与图谱/账本一致" in labels


# ====================================================================
# 7) 否定式 / 疑问式豁免（P2-①，QA 二轮核心）
# ====================================================================
#
# QA 用闭集量化了 7 条变体，**7/7 全是误报**。这类话的意思是「我没查，不能下结论」，
# 和「我查了，一致」正好相反。只做子串匹配会把「我不知道」判成「我声称一致」，
# 方向完全反了；而「我不知道」正是弱模型最常见的诚实输出。

def test_negated_consistency_variants_are_not_claims():
    """QA 的 7 条变体全部必须**不命中**（逐条钉住，防止回退）。"""
    variants = [
        "根据现有信息，无法确定许七安在图谱中是否有记录以及两者是否对得上。",
        "目前无法判断人设与事实账本是否一致。",
        "尚不能确认正文与图谱是否吻合。",
        "没有证据表明二者一致。",
        "不能断定两者是否相符。",
        "信息不足，无法核对是否对得上。",
        "我不知道是否一致。",
    ]
    for answer in variants:
        result = _detect(answer, tools=["read_generation_segments"])
        assert result["unsupported"] == [], f"误报：{answer!r} → {result['unsupported']}"


def test_positive_consistency_claim_still_hits():
    """对照组：没有否定标记的真断言必须命中（不能为了消误报把规则一起废掉）。"""
    result = _detect("与知识图谱、事实账本里的记录一致。",
                     tools=["read_generation_segments"])
    assert "consistency" in _kinds(result)


def test_negation_window_is_per_occurrence_not_per_sentence():
    """逐次判定：前半句「无法确认…是否一致」豁免，后半句「…一致」仍要命中。

    整句判定会把第二个「一致」一起豁免掉——那正是「为了消误报把真断言也吃掉」。
    """
    answer = "无法确认图谱记录是否一致，但事实账本与正文确实一致。"
    result = _detect(answer, tools=["read_generation_segments"])
    assert "consistency" in _kinds(result), "第二个未被否定的「一致」必须命中"


def test_negated_absence_variant_is_not_a_claim():
    """否定式同样覆盖 absence：「无法判断有没有异常」不是断言。"""
    result = _detect("现有信息不足以判断有没有异常。", tools=["read_generation_segments"])
    assert "absence" not in _kinds(result)


def test_diagnostic_question_gets_no_rewrite_intent():
    """P3：`有没有需要修改的地方？` 是提问，不是要求改写（不该加建议稿横幅）。"""
    result = _detect("建议压缩前两句，其余可以保留。",
                     query="第 1 段有没有需要修改的地方？")
    assert result["rewrite_intent"] is False


def test_imperative_rewrite_still_gets_intent():
    result = _detect("这是改写稿。", query="把第 1 段改成更紧凑的写法")
    assert result["rewrite_intent"] is True


# ====================================================================
# 8) 正向存在性断言（P2，QA 终验抓到的「同类里漏掉的」）
# ====================================================================
#
# 实测原文：query「第 1 段里出场的角色『小杨』在图谱里有记录吗？」、
# calls=['read_generation_segments']、answer「…在图谱里有记录。」（小杨不在图谱）、
# status=success / trustworthy=True。**错答案 + 报 trustworthy**。

def test_existence_claim_without_graph_tool_hits():
    result = _detect("第 1 段里出场的角色『小杨』在图谱里有记录。",
                     tools=["read_generation_segments"])
    kinds = _kinds(result)
    assert "existence" in kinds
    assert "图谱里有记录" in [item["phrase"] for item in result["unsupported"]]


def test_existence_claim_allowed_after_reading_graph():
    """查过图谱（CONSISTENCY_TOOLS 任一）→ 有据，不拦。"""
    for tool in CONSISTENCY_TOOLS:
        result = _detect("小杨在图谱里有记录。", tools=[tool])
        assert "existence" not in _kinds(result), f"{tool} 应当放行存在性断言"


def test_existence_rule_does_not_touch_body_text_claims():
    """「正文里有记录」不是 KG 类断言，不能被误伤（门控在 KG 类名词上）。"""
    result = _detect("第 1 段正文里有记录小杨这个名字。",
                     tools=["read_generation_segments"])
    assert "existence" not in _kinds(result)


def test_negated_existence_is_not_a_claim():
    result = _detect("无法确认小杨在图谱里是否有记录。",
                     tools=["read_generation_segments"])
    assert "existence" not in _kinds(result)


# ====================================================================
# 10) 词表与副词补漏（任务书 §十一 的两条 P3）
# ====================================================================

def test_write_done_covers_the_verbose_phrasing():
    """「已经写入正文了」不是「已写入」的超集 —— 词表漏了它就等于没护栏。"""
    for phrase in ("已经写入正文了。", "正文已经写入。", "已经写入正文"):
        result = _detect(phrase, tools=["read_generation_segments"])
        assert "write_done" in _kinds(result), phrase


def test_write_done_phrase_is_still_exempted_by_negation():
    """加词不能把「尚未写入」也判成完成声称。"""
    result = _detect("尚未写入正文。", tools=["read_generation_segments"])
    assert "write_done" not in _kinds(result)


def test_existence_claim_survives_an_inserted_adverb():
    """「图谱里确实有记录」「图谱里也有记录」以前不命中（中间夹了副词）。"""
    for phrase in ("小杨在图谱里确实有记录。", "小杨在图谱里也有记录。"):
        result = _detect(phrase, tools=["read_generation_segments"])
        assert "existence" in _kinds(result), phrase


def test_existence_adverb_change_does_not_widen_the_false_positive():
    """副词是有限闭集，窗口仍限死 8 字 —— 「正文里有记录」不能被误伤。"""
    result = _detect("第 1 段正文里确实有记录小杨这个名字。",
                     tools=["read_generation_segments"])
    assert "existence" not in _kinds(result)


# ====================================================================
# 9) 转折连词：把否定翻回来（P2，QA 实测补丁 0 新误报）
# ====================================================================
#
# 否定窗口在「同句转折」上会漏：`虽无法完全确认但确实一致` 的窗口里同时有
# 「无法」和「确实」，只按否定标记判会整句豁免——但那句话的落点是「确实一致」。

def test_turn_conjunction_overrides_negation():
    for answer, phrase in (
        ("虽无法完全确认但确实一致。", "一致"),
        ("不能确定是否有冲突，但两者的描述确实吻合。", "吻合"),
        ("无法判断是否一致，不过从正文看是相符的。", "相符"),
        ("信息不足，然而已裁决事实与正文一致。", "一致"),
    ):
        result = _detect(answer, tools=["read_generation_segments"])
        assert "consistency" in _kinds(result), f"{answer!r} 的转折落点是断言，必须命中"
        assert phrase in [item["phrase"] for item in result["unsupported"]]


def test_pure_negation_still_exempt_after_turn_patch():
    """对照组：没有转折词的否定仍必须豁免（不能为了抓转折把豁免废掉）。"""
    for answer in ("无法判断是否一致。", "我不知道是否一致。", "尚不能确认是否吻合。"):
        assert _detect(answer, tools=["read_generation_segments"])["unsupported"] == []


# ====================================================================
# 10) 带宾语的缺席式断言（P2，QA 评测集 r3_e2.py 的 12 条 + 1 条补充）
# ====================================================================
#
# 判定按 **absence 这一 kind** 断言（不是「有没有任何 unsupported」）：
# 评测集里几条「不应命中」的回答本身可能命中 numeric/consistency，那是别的规则
# 在正常工作，与这条模板无关。

_ONLY_READ = {"read_generation_segments"}
_KG_READ = {"get_character_profile", "get_fact_ledger"}
#: 「一侧都没读」：只跑了个与判断对象无关、且不在任何放行集合里的工具。
#: `重复` 的判断对象是被检查的正文，所以这类用例**不能**用 `_ONLY_READ`
#: （读了正文再判重复是有据的），否则测的就不是「没读任何一侧却断言无重复」原意；
#: 也不能用 `get_reminders`（它在 CONSISTENCY_TOOLS 里，是能放行的对照侧之一）。
_NO_SIDE_READ = {"summarize_bible"}
_ABSENCE_EVAL: tuple[tuple[str, set[str], bool, str], ...] = (
    # —— 应当命中（没读任何一侧却断言无冲突/矛盾/重复）——
    ("没有发现与李浩人设的冲突。", _ONLY_READ, True, "自曝漏报 A"),
    ("没有发现正文描述与图谱记录之间的矛盾。", _ONLY_READ, True, "自曝漏报 B"),
    ("没有重复的句子。", _NO_SIDE_READ, True, "自曝漏报 C（读正文才算有据）"),
    ("未发现角色名前后不一致。", _ONLY_READ, True, "一致性缺席"),
    ("没有出现同一句重复两次。", _NO_SIDE_READ, True, "重复缺席"),
    ("未见到重复内容。", _NO_SIDE_READ, True, "补充：未见+宾语"),
    # —— 不应命中 ——
    ("不能排除冲突，两者确实吻合。", _ONLY_READ, False, "承认可能有冲突"),
    ("没有排除冲突的可能。", _ONLY_READ, False, "未排除 ≠ 无冲突"),
    ("冲突在第 2 段已经解决了。", _ONLY_READ, False, "陈述而非缺席"),
    ("检查后发现 2 处冲突。", _ONLY_READ, False, "有冲突"),
    ("没有明显的时间线冲突。", _KG_READ, False, "查过图谱（上轮误伤）"),
    ("检索结果里没有明显的相关段落。", {"search_novel_chunks"}, False, "检索跑过"),
    ("无法确定是否有冲突。", _ONLY_READ, False, "否定式"),
    # —— 本轮新增：读过被检查的正文再判「重复」是有据的（QA 窄范围终核的误报）——
    ("第 1 段中没有重复的句子。", _ONLY_READ, False, "读正文后判重复 = 有据"),
)


def test_absence_with_object_evaluation_set():
    """QA 评测集逐条判定（TP=6 / FP=0 / FN=0 / TN=8）。"""
    tp = fp = fn = tn = 0
    for answer, tools, expect_hit, tag in _ABSENCE_EVAL:
        result = _detect(answer, tools=tools)
        got = "absence" in _kinds(result)
        assert got == expect_hit, (
            f"{tag}：expect={'HIT' if expect_hit else 'pass'} "
            f"got={'HIT' if got else 'pass'} → {result['unsupported']}"
        )
        if expect_hit and got:
            tp += 1
        elif expect_hit and not got:
            fn += 1
        elif not expect_hit and got:
            fp += 1
        else:
            tn += 1
    assert (tp, fp, fn, tn) == (6, 0, 0, 8)


def test_hedged_absence_uses_union_tool_gate():
    """「没有明显…」的断言对象不固定 → 门是工具并集（核对 ∪ 审核 ∪ 检索）。

    实测分界：「没有明显的 AI 腔」（只读正文）该拦；「没有明显的时间线冲突」
    （读过图谱）/「没有明显的相关段落」（跑过检索）不该拦。
    """
    assert "absence" in _kinds(
        _detect("整体没有明显的 AI 腔。", tools=["read_generation_segments"])
    )
    for tools in (_KG_READ, {"audit_generation"}, {"search_novel_chunks"}):
        assert "absence" not in _kinds(
            _detect("没有明显的问题。", tools=tools)
        ), f"{tools} 应当放行泛化的「没有明显」"


# ====================================================================
# 11) 「重复」按名词单独开门（QA 窄范围终核的误报，约 8% 真实流量）
# ====================================================================
#
#   query : 第 1 段里有没有重复的句子？
#   calls : ['read_generation_segments']     ← 真的读了被判断的那段正文
#   answer: 第 1 段中没有重复的句子。
#   实测  : status=failed + ⚠ 前缀（误报）
#
# `重复` 的判断对象**就是被读的正文本身**；而「冲突/矛盾/不一致/不符」要求读过
# **对照侧**（图谱/账本/时间线），读正文不构成对照。所以按名词分门，不整体放宽。

def test_repetition_claim_allowed_after_reading_the_text():
    """必测 1：读了正文再说「没有重复」→ 不命中（回归这条误报）。"""
    result = _detect("第 1 段中没有重复的句子。", tools=["read_generation_segments"])
    assert "absence" not in _kinds(result), result["unsupported"]


def test_conflict_claim_still_hits_after_reading_text():
    """必测 2：只读正文仍**必须**拦「与图谱记录没有矛盾」（对照侧没读）。"""
    result = _detect("没有发现正文描述与图谱记录之间的矛盾。",
                     tools=["read_generation_segments"])
    assert "absence" in _kinds(result), "对照侧没读，不能放行"


def test_repetition_claim_hits_without_reading_the_text():
    """必测 3：没读正文却说「没有重复」→ 命中。"""
    result = _detect("第 1 段中没有重复的句子。", tools=["summarize_bible"])
    assert "absence" in _kinds(result)
    assert any("没有重复" in item["phrase"] for item in result["unsupported"])


def test_audit_gate_not_affected_by_repetition_change():
    """必测 4：审核门没被牵连——只读正文说「没有明显的 AI 腔」仍命中。"""
    result = _detect("整体没有明显的 AI 腔。", tools=["read_generation_segments"])
    assert "absence" in _kinds(result)


def test_repetition_label_points_at_the_real_gap():
    """必测 5（文案）：重复的提示不能说「未运行对应审核规则」——没有那种规则。

    absence 的默认提示会把用户指向不存在的审核工具，所以按名词给文案：
    「声称文本中没有重复（本次未读取被检查的正文）」。
    """
    from app.agents.claim_guard import ABSENCE_REPETITION_LABEL, describe_kinds

    result = _detect("第 1 段中没有重复的句子。", tools=["summarize_bible"])
    labels = describe_kinds(result["unsupported"])
    assert ABSENCE_REPETITION_LABEL in labels
    assert "未运行对应审核规则" not in labels
    # 冲突类仍用默认文案（对照侧那四个工具确实存在）
    conflict = _detect("没有发现与李浩人设的冲突。", tools=["read_generation_segments"])
    assert "未运行对应审核规则" in describe_kinds(conflict["unsupported"])
