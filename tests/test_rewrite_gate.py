"""文风改写的**闸门**：偏离参考书越远的那段才值得多花一次模型调用。

## 为什么要有闸门

改写要额外一次模型调用（M3 关思考实测 19s/900 字；换成 M3.1-Flash 更慢），
而它**只修得动「碎」** —— 把「一句一动作」的碎句合成有呼吸的复合句。
对一段本来就够长的正文动它，纯属白花一次调用（还可能压缩 15-25% 字数）。

## 尺子从哪来

**参考书自己的分布**：句长均值 < p10（比 90% 的人类样本还碎）、
句号密度 > p90（比 90% 的人类样本还碎）。不拍脑袋定阈值：语料换了尺子也跟着换。

## 要守住的

1. 偏离不到 → **不调用**改写（省一次模型调用）；
2. 偏离到了 → 调；
3. 没有档案 → 不改写（没有「像不像」的基准）；
4. 项目显式设过（projects.style_rewrite=1）→ 每段都改（显式选择优先）；
5. 请求体的 style_rewrite → 单次覆盖，且 **False 真的要能关掉**
   （旧实现写进项目，库里 0 与「从没设过」分不开，False 形同虚设）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.generation.engine import (  # noqa: E402
    _maybe_rewrite_style,
    _rewrite_decision,
    _rewrite_gate,
)
from app.style.stylometry import profile_band  # noqa: E402


class _Project:
    def __init__(self, style_rewrite=0):
        self.style_rewrite = style_rewrite


class _Provider:
    """会记调用次数的假 provider。"""

    def __init__(self, reply="改写后的正文。"):
        self.calls = 0
        self.reply = reply

    def chat(self, system, user, **kwargs):
        self.calls += 1
        return self.reply


#: 一份 v2 档案：句长 p10=45、p90=70；句号密度 p10=10、p90=16。
PROFILE = {
    "version": 2,
    "_meta": {"base_unit_chars": 3000},
    "buckets": {
        "medium": {
            "unit_chars": 3000,
            "feat": {
                "句长均值": {"mean": 55.0, "std": 12.0, "p10": 45.0,
                             "p50": 54.0, "p90": 70.0, "min": 30.0, "max": 90.0},
                "句号密度": {"mean": 12.0, "std": 2.0, "p10": 10.0,
                             "p50": 12.0, "p90": 16.0, "min": 8.0, "max": 20.0},
            },
            "holdout": {},
        },
    },
}


def _long_text(sentence: str, count: int) -> str:
    """造一段够长的文本（闸门要跳过太短的文本 —— 统计噪声大）。"""
    return "\n".join([sentence] * count)


def test_profile_band_reads_v2_bucket():
    band = profile_band(PROFILE, "句长均值", "medium")
    assert band["p10"] == 45.0
    # 没有这个特征 → 空 dict（不是 None，调用方直接 .get）
    assert profile_band(PROFILE, "纯台词段占比", "medium") == {}


def test_profile_band_reads_v1_flat_profile():
    v1 = {"feat": {"句长均值": {"mean": 50.0, "p10": 40.0, "p90": 66.0}}}
    assert profile_band(v1, "句长均值")["p10"] == 40.0


def test_decision_modes():
    assert _rewrite_decision(_Project(0)) == "gate"
    assert _rewrite_decision(_Project(1)) == "always"
    assert _rewrite_decision(_Project(0), True) == "always"
    assert _rewrite_decision(_Project(0), False) == "never"
    # 项目显式开着，单次请求说「别改」→ 单次覆盖优先
    assert _rewrite_decision(_Project(1), False) == "never"


def test_gate_fires_on_shattered_text():
    """句长远低于档案 p10 → 严重偏离。"""
    text = _long_text("他走了。", 300)  # 每句 4 字 → 句长约 4
    severe, reasons = _rewrite_gate(text, PROFILE)
    assert severe, reasons
    assert any(r["feature"] == "句长均值" for r in reasons), reasons
    assert all(r["detail"] for r in reasons), reasons


def test_gate_silent_on_long_sentences():
    """句子够长 → 不偏离，不该动改写。

    注意：句长要**明显**高于档案 p10（45 字）。写 33 字的句子是真的比
    90% 的人类样本还碎 —— 闸门判它「碎」是对的。
    """
    text = _long_text(
        "他慢慢地把手里的那枚铜钱放回木匣子里，指尖在匣盖上停了一瞬，"
        "像是舍不得，又像是终于下定了什么决心，屋子里安静得能听见外头的雨。", 30)
    severe, reasons = _rewrite_gate(text, PROFILE)
    assert not severe, reasons


def test_gate_without_profile_is_never_severe():
    """没有档案就没有「像不像」的基准 → 不改写。"""
    text = _long_text("他走了。", 300)
    severe, reasons = _rewrite_gate(text, None)
    assert severe is False
    assert reasons == []
    severe, reasons = _rewrite_gate(text, {})
    assert severe is False


def test_gate_skips_short_text():
    """太短的文本统计噪声大 → 不当成偏离（和诊断规则同口径）。"""
    severe, reasons = _rewrite_gate("他走了。", PROFILE)
    assert severe is False, reasons


def test_maybe_rewrite_not_called_when_below_gate():
    """偏离不到 → **一次模型调用都不该有**。"""
    provider = _Provider()
    text = _long_text(
        "他慢慢地把手里的那枚铜钱放回木匣子里，指尖在匣盖上停了一瞬，"
        "像是舍不得，又像是终于下定了什么决心，屋子里安静得能听见外头的雨。", 30)
    out, info = _maybe_rewrite_style(_Project(0), text, provider, profile=PROFILE)
    assert provider.calls == 0, info
    assert out == text
    assert info["applied"] is False
    assert "闸门" in info["reason"]


def test_maybe_rewrite_called_when_above_gate():
    """偏离到了 → 调改写（改写自己的三道校验仍照旧把关）。"""
    provider = _Provider()
    text = _long_text("他走了。", 300)
    out, info = _maybe_rewrite_style(_Project(0), text, provider, profile=PROFILE)
    assert provider.calls == 1, info
    # 假 provider 回的正文很短 → 会被「字数缩水」判据挡下，保留原文
    assert out == text
    assert info["applied"] is False


def test_explicit_project_setting_skips_gate():
    """项目显式设过 → 每段都改（不看出身偏离）。"""
    provider = _Provider()
    text = _long_text(
        "他慢慢地把手里的那枚铜钱放回木匣子里，指尖在匣盖上停了一瞬，"
        "像是舍不得，又像是终于下定了什么决心，屋子里安静得能听见外头的雨。", 30)
    _out, info = _maybe_rewrite_style(_Project(1), text, provider, profile=PROFILE)
    assert provider.calls == 1, info


def test_request_override_false_never_calls():
    """请求体说「这段别改」→ 一次调用都不该有（且不受项目设置影响）。"""
    provider = _Provider()
    text = _long_text("他走了。", 300)
    _out, info = _maybe_rewrite_style(_Project(1), text, provider, profile=PROFILE,
                                       override=False)
    assert provider.calls == 0, info
    assert info["reason"] == "开关关闭"
