"""定向返修计划：把「文风偏离」翻译成几条可执行的修改要求。

## 为什么单独一层

生成链路里的 `_style_warning` / `_rewrite_gate` 只回答「要不要重写」，
不回答「重写时要改什么」。整段重写既贵，又容易把本来没问题的情节一起改掉。

这里只做**只读诊断**：拿项目已落库的统计档案 + 叙事规则，找出
「超出参考书自己分布」的指标和「命中了反向禁忌」的表达，压成一份
几行字的返修指令。真正执行仍走既有的 `invoke-generation` 重写链路，
因此快照、回滚、审计一个都不缺。

诊断不调模型，失败也不影响正文。
"""
from __future__ import annotations

from typing import Any, Iterable

from app.style.deterministic_repair import (
    find_dialogue_punctuation_issues,
    find_multi_speaker_paragraphs,
    find_number_word_mismatch,
    find_tics,
)
from app.style.stylometry import FEATURE_NOTES, band_check

#: 参与诊断的特征 → (维度, 标题, 方向, 修法)。
#:
#: 方向：
#: - ``low``  = 低于参考书 p10 才算偏离（句子偏碎、段落等长等）；
#: - ``high`` = 高于参考书 p90 才算偏离（感叹号、比喻、情绪直陈等）；
#: - ``both`` = 两端都可能失衡（对白占比）。
#:
#: 修法文案刻意不出现「只 / 仅 / 保留 / 局部 / 段尾 / 结尾 / 第 N 句」
#: 这些词——它们会触发 `scope_guard.declared_local`，把整段返修误判成
#: 局部改写并弹出澄清候选。
_FEATURE_RULES: dict[str, tuple[str, str, str, str]] = {
    "句长均值": (
        "节奏控制",
        "句子偏碎",
        "low",
        "把叙述里的连续短句合并成有停顿、有呼吸的长句；用动作、场景或旁人反应承载节奏，"
        "不要靠删内容来凑长度。",
    ),
    "句号密度": (
        "节奏控制",
        "断句过密",
        "high",
        "减少叙述里一句一断的碎句，把同一动作链上的短句合成一句。",
    ),
    "短句占比": (
        "节奏控制",
        "短句扎堆",
        "high",
        "把叙述里的连续短句并成完整动作句，留少量短句做顿挫。",
    ),
    "句长CV": (
        "节奏控制",
        "句长过于整齐",
        "low",
        "长短句交错，避免每句长度接近。",
    ),
    "段长CV": (
        "段落编排",
        "段落等长",
        "low",
        "让段落长短有变化：关键动作另起一段，过渡句并入相邻段。",
    ),
    "感叹号密度": (
        "情绪表达",
        "感叹号过密",
        "high",
        "把感叹号换成动作、停顿或语气词，让情绪从场景里出来。",
    ),
    "比喻密度": (
        "人物描写手法",
        "比喻堆叠",
        "high",
        "删掉重复的明喻，改用具体动作、物件或感官细节。",
    ),
    "情绪直陈密度": (
        "情绪表达",
        "情绪直陈",
        "high",
        "删掉「心中充满 / 眼中闪过」这类标签，用动作和生理反应承载情绪。",
    ),
    "段尾升华率": (
        "情绪表达",
        "升华式收束偏多",
        "high",
        "把总结拔高的句子收掉，让段落停在一个动作、物件或对白上。",
    ),
    "对白字占比": (
        "对白风格",
        "对白占比失衡",
        "both",
        "对白偏少就补进人物反应与信息交换；对白偏多就把部分内容转成动作与场景。",
    ),
    "四字格密度": (
        "文笔量化审计",
        "四字格偏密",
        "high",
        "减少成语式四字堆叠，换成更具体的口语或动作。",
    ),
}

#: 反向禁忌维度的展示名。原始维度里可能含「段尾」等会触发局部改写判定的词。
_FORBIDDEN_DIM_LABEL: dict[str, str] = {
    "禁用段尾拔高": "禁用拔高式收束",
    "禁用转折词": "禁用转折词",
    "禁用情绪直陈": "禁用情绪直陈",
    "禁用解释性对白": "禁用解释性对白",
}

#: 保险：即使规则文案以后被改写，也不让局部改写关键词漏进指令。
_LOCAL_TRIGGER_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("段尾", "收束"),
    ("段首", "段初"),
    ("结尾", "收束"),
    ("开头", "起笔"),
    ("局部", "片段"),
    ("此处", "这里"),
    ("某处", "相关位置"),
    ("这一句", "这句话"),
    ("这两句", "这两句话"),
)

_MAX_FORBIDDEN_ISSUES = 3
_MIN_TEXT_CHARS = 200

#: 占比类特征按百分比展示；0.17 写成「17%」比「0.2」更可读，也不会出现
#: 「当前 0.2，参考 0.1–0.2」这种看起来没超标的假象。
_RATIO_FEATURES = frozenset(
    {"短句占比", "长句占比", "对白字占比", "对白段占比", "段尾升华率"}
)


def _fmt(value: float) -> str:
    """紧凑数字：12.0 → 12，12.34 → 12.3。"""
    text = f"{float(value):.1f}"
    return text[:-2] if text.endswith(".0") else text


def _fmt_feature(feature: str, value: float) -> str:
    if feature in _RATIO_FEATURES:
        return f"{_fmt(float(value) * 100)}%"
    return _fmt(value)


def _band_z(value: float, band: dict[str, Any]) -> float:
    try:
        mean = float(band.get("mean") or 0.0)
        std = float(band.get("std") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if std <= 0:
        return 0.0
    return (value - mean) / std


def _metric_issue(feature: str, value: float, band: dict[str, Any]) -> dict[str, Any] | None:
    """按参考书自己的 p10/p90 判断一项指标是否出带。"""
    dimension, title, direction, fix = _FEATURE_RULES[feature]
    try:
        p10 = float(band.get("p10") or 0.0)
        p50 = float(band.get("p50") or 0.0)
        p90 = float(band.get("p90") or 0.0)
    except (TypeError, ValueError):
        return None

    low = direction in ("low", "both") and p10 and value < p10
    high = direction in ("high", "both") and p90 and value > p90
    if not low and not high:
        return None

    if direction == "both":
        title = "对白偏少" if low else "对白偏多"
    target = (
        f"参考区间 {_fmt_feature(feature, p10)}–{_fmt_feature(feature, p90)}"
        f"（中位 {_fmt_feature(feature, p50)}）"
    )
    note = FEATURE_NOTES.get(feature)
    label = f"{feature}（{note}）" if note else feature
    detail = f"{label}：当前 {_fmt_feature(feature, value)}，{target}"
    z = _band_z(value, band)
    return {
        "kind": "metric",
        "dimension": dimension,
        "title": title,
        "detail": detail,
        "fix": fix,
        "severity": "high" if abs(z) >= 2.0 else "medium",
        "feature": feature,
        "value": round(value, 2),
        "z": round(z, 2),
    }


def _forbidden_items(narrative: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """从叙事档案的 forbidden 层取出可做字面命中的短表达。"""
    forbidden = narrative.get("forbidden")
    if not isinstance(forbidden, dict):
        return
    for dimension, items in forbidden.items():
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                label = str(item.get("rule") or item.get("text") or "")
            else:
                label = str(item or "")
            label = label.strip().strip("「」『』\"'“”")
            # 只扫短表达；长句是「规则描述」，不是可直接检索的禁用词。
            if 1 < len(label) <= 16:
                yield str(dimension), label


def _forbidden_issues(text: str, narrative: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    counts: dict[str, int] = {}
    for dimension, label in _forbidden_items(narrative):
        count = text.count(label)
        if count <= 0:
            continue
        label_name = _FORBIDDEN_DIM_LABEL.get(dimension, dimension)
        grouped.setdefault(label_name, []).append(f"「{label}」×{count}")
        counts[label_name] = counts.get(label_name, 0) + count

    issues: list[dict[str, Any]] = []
    for dimension, hits in grouped.items():
        issues.append(
            {
                "kind": "forbidden",
                "dimension": dimension,
                "title": "命中反向禁忌",
                "detail": "、".join(hits),
                "fix": "逐个删掉或换成具体动作，不要换一个同义词继续写。",
                "severity": "high",
                "hits": hits,
                "z": 0.0,
            }
        )
    issues.sort(key=lambda item: -counts.get(item["dimension"], 0))
    return issues[:_MAX_FORBIDDEN_ISSUES]


def _sanitize_instruction(text: str) -> str:
    for source, target in _LOCAL_TRIGGER_REPLACEMENTS:
        text = text.replace(source, target)
    return text


def _hard_issues(text: str) -> list[dict[str, Any]]:
    """不依赖文风档案的硬伤诊断：标点、双人同段、口头禅、数字自洽。

    这些项修法明确、可验证，排在量化偏离之前。``deterministic_repair``
    能先修掉前两项；自动返修链路跑完确定性修复后，这里通常只剩口头禅
    与数字两项，交给模型改。
    """
    issues: list[dict[str, Any]] = []

    punct = find_dialogue_punctuation_issues(text)
    if punct:
        issues.append({
            "kind": "hard",
            "dimension": "标点硬伤",
            "title": "引语标点不完整",
            "detail": (
                f"{len(punct)} 处引语缺句末标点、或引号外多一个逗号，"
                f"如「{punct[0][:40]}」"
            ),
            "fix": "给引语补上句末标点（问句用问号），并去掉引号外多余的逗号。",
            "severity": "high",
            "z": 6.0,
        })

    multi = find_multi_speaker_paragraphs(text)
    if multi:
        issues.append({
            "kind": "hard",
            "dimension": "对白归属",
            "title": "两个说话人挤在同一段",
            "detail": f"{len(multi)} 段里有两个人的台词，如「{multi[0][:40]}」",
            "fix": "按说话人拆成独立段落，每段承载一个人的台词与紧邻动作。",
            "severity": "high",
            "z": 6.0,
        })

    tics = find_tics(text)
    for item in tics:
        if item["limit"] <= 1:
            keep = "一处"
        elif item["limit"] == 2:
            keep = "一两处"
        else:
            keep = "两三处"
        issues.append({
            "kind": "hard",
            "dimension": "人物声纹",
            "title": f"口头禅「{item['word']}」过密",
            "detail": (
                f"当前出现 {item['count']} 次，建议不超过 {item['limit']} 次"
            ),
            "fix": (
                f"把「{item['word']}」压到 {item['limit']} 次以内，"
                f"{keep}留作人物声纹，其余改成动作、语气或直接删，"
                "不要换一个同义词继续堆。"
            ),
            "severity": "high",
            "z": 4.0,
        })

    mismatches = find_number_word_mismatch(text)
    for item in mismatches:
        issues.append({
            "kind": "hard",
            "dimension": "事实自洽",
            "title": "字数回指对不上",
            "detail": (
                f"写「{item['declared']} 个字」，但前文引语实际是 "
                f"{item['actual']} 个字：「{item['quote'][:20]}」"
            ),
            "fix": "把数字改成与引语实际字数一致，或改成不写具体数字的说法。",
            "severity": "high",
            "z": 5.0,
        })

    return issues


def _build_instruction(issues: list[dict[str, Any]]) -> str:
    lines = [
        "这是定向返修。保持剧情、人物、事实不变；对白内容与说话人归属必须"
        "原样带过来，不删台词，对白字占比不得低于改前。"
        "不要改变段落数量与顺序，不要合并或拆分段落；对白段保持原样。"
        "按下列问题调整表达；不要新增情节，输出修改后的正文。",
    ]
    for index, issue in enumerate(issues, 1):
        if issue["kind"] == "forbidden":
            lines.append(
                f"{index}. 【{issue['dimension']}】{issue['detail']}。{issue['fix']}"
            )
        else:
            lines.append(
                f"{index}. 【{issue['dimension']}】{issue['title']}："
                f"{issue['detail']}。{issue['fix']}"
            )
    return _sanitize_instruction("\n".join(lines))


def build_repair_plan(
    text: str,
    profile: dict[str, Any] | None,
    narrative: dict[str, Any] | None,
    *,
    max_issues: int = 6,
) -> dict[str, Any]:
    """生成定向返修计划（不调模型）。"""
    narrative = narrative if isinstance(narrative, dict) else {}
    text = text or ""
    # 硬伤不依赖文风档案；放最前面，避免被 6 条上限挤掉。
    issues: list[dict[str, Any]] = _hard_issues(text)

    if profile and text.strip() and len(text.strip()) >= _MIN_TEXT_CHARS:
        try:
            checks = band_check(text, profile, tuple(_FEATURE_RULES))
        except Exception:  # noqa: BLE001 —— 诊断失败不能影响正文
            checks = {}
        for feature in _FEATURE_RULES:
            check = checks.get(feature)
            if not check:
                continue
            issue = _metric_issue(feature, float(check.get("value") or 0.0), check.get("band") or {})
            if issue:
                issues.append(issue)

    issues.extend(_forbidden_issues(text, narrative))

    # 反向禁忌是硬规则，排在量化偏离之前；同类按偏离强度降序。
    issues.sort(
        key=lambda item: (
            0 if item["severity"] == "high" else 1,
            -abs(float(item.get("z") or 0.0)),
        )
    )
    issues = issues[:max_issues]

    if issues:
        instruction = _build_instruction(issues)
        summary = f"发现 {len(issues)} 处可定向修的表达问题"
        reason = ""
    elif not profile and not narrative:
        instruction = ""
        summary = "还没有可用的文风档案"
        reason = "no_archive"
    elif not text.strip():
        instruction = ""
        summary = "这一段还没有正文"
        reason = "empty_text"
    else:
        instruction = ""
        summary = "这一段没有检测到明显偏离"
        reason = "clean"

    return {
        "ok": bool(issues),
        "summary": summary,
        "reason": reason,
        "issues": issues,
        "instruction": instruction,
        "archive": {
            "has_profile": bool(profile),
            "has_rules": bool(narrative),
        },
    }
