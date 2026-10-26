"""文风改写的五道判据：**膨胀**与**对白形态变坏**这两道是 2026-10-25 补的。

## 为什么补

真跑（真书真实正文，M3 改写）发现：改写把 **3011 字撑成 5979 字**（翻一倍），
引号数 60 → 161 —— 模型把提示里的「平均每句 45 到 70 个字」当成了「多写点」，
**凭空扩写**出一大段新对白。后果：

- 多出来的内容是**模型编的**（新对白、新情节），没有事实依据；
- **对白形态被改坏**：纯台词段 52% → 84%（参考书 21%）。

原来的校验只挡「缩水」（< 60%），不挡「膨胀」，也不看对白形态。

## 要守住的

1. 膨胀超过上限 → 拒绝，保留原文；
2. 纯台词段占比明显上升 → 拒绝，保留原文；
3. 正常的改写（字数略缩、句长变长、引号保住）→ 照常采纳。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.generation.rewrite import (  # noqa: E402
    MAX_LEN_GROWTH_ABS,
    MAX_LEN_GROWTH_RATIO,
    REWRITE_TEMPERATURE,
    _bare_ratio,
    rewrite_style,
)


class _Provider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0
        self.temps = []

    def chat(self, system, user, **kw):
        self.calls += 1
        self.temps.append(kw.get("temperature"))
        return self.reply


def test_rewrite_uses_low_temperature():
    """改写是「变换」不是「创作」⇒ 必须用低温，否则同一份输入的输出会漂。

    （实测：低温**治不了**凭空扩写 —— 那次 3011→5979 是采样漂，不是温度。
    但低温能防漂，而漂正是这步最需要防的。）
    """
    p = _Provider("改写后的正文，改写后的正文，改写后的正文。")
    rewrite_style(DRAFT, p)
    assert p.temps == [REWRITE_TEMPERATURE]
    assert REWRITE_TEMPERATURE < 0.7


DRAFT = (
    "沈砚蹲在江边，湿石子硌得他脚底生疼。\n\n"
    "“爹。”沈砚开口。\n\n"
    "“嗯。”沈铁山应了一声。\n\n"
    "“刀我拿着。”沈砚说。"
)


def _long_draft(n: int = 60) -> str:
    """造一段够长的碎稿（判据 3 的相对余量按长度算，测试要用长文本）。"""
    return "\n\n".join([DRAFT] * n)


def test_bare_ratio_helper():
    """纯台词段占比：**含引号的段**里「去掉引号内容后只剩标点」的比例。

    两条口径要记住：
    1. 不含引号的段根本不参与（它是叙述，不是对白段）；
    2. 「只剩标点」的判定是**去掉引号后剩下的非标点字符 ≤ 6 个**
       （`stylometry._BARE_SEG_TAIL_MAX`）—— 所以「他沉声道。」这种短尾巴
       仍然算「几乎是纯台词」，尾巴要够长才算「叙述与台词同段」。
    """
    only_bare = "“都退后。”\n\n他抬起头。"
    with_lead = "徐骁沉声道，声音压得很低：“都退后。”\n\n他抬起头。"
    assert _bare_ratio(only_bare) == 1.0, _bare_ratio(only_bare)
    assert _bare_ratio(with_lead) == 0.0, _bare_ratio(with_lead)
    # 一半一半
    mixed = "“都退后。”\n\n徐骁沉声道，声音压得很低：“你过来。”"
    assert _bare_ratio(mixed) == 0.5, _bare_ratio(mixed)


def test_growth_rejected():
    """膨胀超过上限 → 拒绝（模型凭空扩写时必须挡住）。"""
    text = _long_draft()
    bloated = text + ("\n\n“后来呢？”沈砚又问。\n\n老人没有回答。\n\n" * 40)
    out, info = rewrite_style(text, _Provider(bloated))
    assert info["applied"] is False, info
    assert "膨胀" in info["reason"], info
    assert out == text


def test_growth_within_allowance_accepted():
    """只略微变长（模型顺手补几个字）不该被当成膨胀。"""
    text = _long_draft()
    slightly_longer = text + "\n\n他没有再说话。"
    out, info = rewrite_style(text, _Provider(slightly_longer))
    # 句长改善了吗？没改善就走「句长未改善」那道，但**不该**是「膨胀」
    assert "膨胀" not in (info.get("reason") or ""), info


def test_allowance_is_absolute_plus_relative():
    """余量 = 绝对 + 相对取大者（短文本给绝对值，长文本按比例）。"""
    assert MAX_LEN_GROWTH_ABS >= 30
    assert 0 < MAX_LEN_GROWTH_RATIO < 0.5


def test_bare_ratio_regression_rejected():
    """改写把「一行一句台词」变多 → 拒绝（它只该动断句，不该动对白形态）。

    构造一个「三道判据里前几道都过、只有对白形态变坏」的改写：
    - 句长确实改善了（长叙述句把平均句长拉上去了）；
    - 字数没膨胀（甚至略缩）；
    - 但纯台词段从 0% 变成 75%（把对白拆成了一行一句）。
    """
    line = "“都退后。”徐骁沉声道，声音压得很低。"
    text = "\n\n".join([line] * 20)
    assert _bare_ratio(text) == 0.0, _bare_ratio(text)

    long_narration = (
        "“都退后。”屋里没有人动，徐骁沉声道，声音压得很低，"
        "徐凤年握刀不动，檐下的灯笼被风吹得转了半圈，灰尘落了一小撮。"
    )
    bare = "“你过来。”"
    split = "\n\n".join([long_narration] * 5 + [bare] * 15)
    out, info = rewrite_style(text, _Provider(split))
    assert info["applied"] is False, info
    assert "对白形态" in info["reason"], info
    assert out == text
