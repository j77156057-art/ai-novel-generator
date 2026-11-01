"""文风改写：目标句长来自档案、实质改善下限、不许修过头、**尺必须与档案一致**。

## 为什么要这几条（2026-11-01 实测）

跨书对照的逐特征诊断：产出「不像参考书」的头号短板是**句号密度**
（94% 出带、中位 1.66x）。追下去发现：

1. **目标句长是写死的** —— 提示词里写「平均每句 45 到 70 个字」，
   而参考书实测的句长带只有 22~27 字 ⇒ 模型必然把句子拉过头
   （实测一句冲到 41.2 字，档案 p90 才 26.7）。
2. **句号密度 ≈ 1000 / 句长**（实测 39.1 vs 38.3、24.3 vs 24.5）
   ⇒ 句长冲过头，句号密度跟着一起出带。
   ⇒ 所以「给改写加句号密度目标」不是独立的一维，**它就是句长目标**。
3. **只有下限没有上限** —— 冲过头也算「改好了」。
4. **下限是硬阈值 1.2×** —— 实测两段真实改善（+19.1% / +19.8%）
   因为差半个百分点被静默丢掉。
5. **尺不是同一把** —— 改写内部自己切句量出来的句长，在**参考书**上
   比档案的 `stylometry` 句长均值高 25%~40% ⇒ 拿档案的带去卡它是苹果比橘子，
   「不许修过头」会误杀改得最好的那类结果。

⇒ 这一组测试钉住：目标来自档案、尺与档案一致、实质改善就算、离带更远就拒。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.generation.rewrite import (  # noqa: E402
    MIN_SENTENCE_LEN_GAIN_ABS,
    MIN_SENTENCE_LEN_GAIN_RATIO,
    _sentence_target_line,
    _stats,
    rewrite_style,
)


class _Provider:
    """回一句固定文本；顺手记下它收到的提示词。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.users: list[str] = []

    def chat(self, system, user, **kw):
        self.users.append(user)
        return self.reply


def _sentences(sent_len: int, count: int) -> str:
    """造 count 个**档案尺句长**恰好为 sent_len 的句子。

    档案尺（`stylometry`）把终结符算进句长，所以正文部分是 sent_len - 1 个字。
    """
    return "".join("甲" * (sent_len - 1) + "。" for _ in range(count))


def test_stats_uses_the_archive_ruler():
    """改写内部量的句长必须与档案**同一把尺**（`stylometry` 的句长均值）。

    这条是「不许修过头」能不能用的前提：拿档案的带卡自己量的数，
    两把尺必须在同一口径上。

    ⚠️ `unit_chars` 设成这段文本的**汉字数** ⇒ 长度归一化系数 = 1
    ⇒ 比的是裸值（归一化是「把不同长度的文本拉到同一基准」，
    它随长度变，不随尺子变 —— 混进来比就说明没比在同一件事上）。
    """
    from app.style.stylometry import cjk_len, features

    text = ("“走。”他说。\n\n雨停了。\n\n"
            "屋里只剩下灯芯爆响的声音，谁也没有再开口说话，"
            "窗外的巷子空荡荡的，只有脚步声一下一下地远去。")
    mine = _stats(text)[0]
    theirs = float(features(text, unit_chars=cjk_len(text))["句长均值"])
    assert abs(mine - theirs) < 1e-6, (mine, theirs)


class _VariantProvider(_Provider):
    """认 `variant()` 的假 provider：把变体参数记下来。"""

    def __init__(self, reply: str):
        super().__init__(reply)
        self.variants: list[dict] = []

    def variant(self, *, thinking_off: bool = False, max_tokens: int | None = None):
        self.variants.append({"thinking_off": thinking_off, "max_tokens": max_tokens})
        return self


class _NoVariantProvider(_Provider):
    """不认 `variant()` 的 provider（老 provider / 本地兜底）。"""

    def variant(self, **kw):  # noqa: D401
        raise AttributeError("no variant")


def test_rewrite_uses_thinking_off_variant_with_own_budget():
    """改写必须**关思考 + 用自己的预算**。

    根因（2026-11-01 实测）：闸门触发了 17/18 段，改写却每次**返回空输出** ——
    推理块吃光了 `max_tokens=8000`，正文一个字没吐 ⇒「改了等于没改」。
    仓库里归属校正已有同一套路（`attribution_fix.CORRECTION_THINKING_OFF`）。
    """
    from app.generation.rewrite import (
        REWRITE_MAX_TOKENS,
        REWRITE_THINKING_OFF,
    )

    p = _VariantProvider(_sentences(9, 12))
    rewrite_style(_sentences(9, 12), p)
    assert p.variants, "改写没走 provider.variant()"
    assert p.variants[0]["thinking_off"] is True
    assert p.variants[0]["max_tokens"] == REWRITE_MAX_TOKENS
    assert REWRITE_MAX_TOKENS >= 16000
    assert REWRITE_THINKING_OFF is True


def test_rewrite_falls_back_when_provider_has_no_variant():
    """provider 不认 `variant()` ⇒ 原样调用，不该炸。"""
    p = _NoVariantProvider(_sentences(9, 12))
    _o, info = rewrite_style(_sentences(9, 12), p)
    assert info.get("applied") is not None
    assert p.users, "provider 没被调用"


def test_sentence_target_comes_from_profile_range():
    """给了目标带 ⇒ 提示词里的目标句长就是那条带（不是写死的 45~70）。"""
    p = _Provider(_sentences(8, 12))
    rewrite_style(_sentences(8, 12), p, target_sentence_range=(22.0, 27.0))
    assert p.users, "provider 没被调用"
    user = p.users[0]
    assert "22" in user and "27" in user, user[:400]
    assert "45 到 70" not in user, "目标句长还是写死的那个"
    assert "参考书自己的节奏" in user


def test_no_target_keeps_default_wording():
    """没给目标 ⇒ 用兜底说法，且提示词仍然完整。"""
    assert "45 到 70" in _sentence_target_line(None)
    p = _Provider(_sentences(8, 12))
    rewrite_style(_sentences(8, 12), p)
    assert "45 到 70" in p.users[0]


def test_real_gain_just_under_old_threshold_is_accepted():
    """+20% 的改善刚好卡在旧硬阈值（1.2×）上 ⇒ 现在应当被采纳。

    旧判据是 `after > before * 1.2` 才过；档案尺 31 → 37 正好
    37 ≤ 31×1.2 = 37.2 ⇒ 旧判据会拒。
    实测有两段真实改善（+19.1% / +19.8%）就是这样被丢掉的。
    """
    text = _sentences(31, 8)
    out = _sentences(37, 7)          # 句长 31 → 37（+19.4%），字数基本不变
    _o, info = rewrite_style(text, _Provider(out), target_sentence_range=(20.0, 40.0))
    assert info["applied"] is True, info
    assert MIN_SENTENCE_LEN_GAIN_ABS <= 2.0
    assert 0 < MIN_SENTENCE_LEN_GAIN_RATIO <= 0.2


def test_overshoot_is_rejected():
    """冲过目标带的另一端 ⇒ 拒绝（哪怕它「变长了很多」）。"""
    text = _sentences(9, 12)          # 句长 9（带 10~20 ⇒ 偏短 1 字）
    out = _sentences(61, 2)           # 句长 61 ⇒ 冲到带外另一端
    res, info = rewrite_style(text, _Provider(out), target_sentence_range=(10.0, 20.0))
    assert info["applied"] is False, info
    assert "修过头" in info["reason"], info
    assert res == text


def test_closer_to_band_is_accepted():
    """离带更近了 ⇒ 采纳（哪怕结果刚好还在带内边缘）。"""
    text = _sentences(9, 12)          # 句长 9（带 10~20 ⇒ 偏短 1 字）
    out = _sentences(15, 7)           # 句长 15（进带）
    _o, info = rewrite_style(text, _Provider(out), target_sentence_range=(10.0, 20.0))
    assert info["applied"] is True, info


def test_staying_inside_band_is_not_overshoot():
    """本来就在带内、改完还在带内 ⇒ **不算修过头**（`>` 而不是 `>=`）。"""
    text = _sentences(21, 10)
    out = _sentences(24, 10)
    _o, info = rewrite_style(text, _Provider(out), target_sentence_range=(20.0, 25.0))
    assert info["applied"] is True, info


def test_info_carries_target():
    """诊断里要带上目标带（事后能查「当时是按什么改的」）。"""
    text = _sentences(9, 12)
    out = _sentences(15, 7)
    _o, info = rewrite_style(text, _Provider(out), target_sentence_range=(10.0, 20.0))
    assert info.get("target") == [10.0, 20.0], info
