"""省略号习惯校正（第十道闸门那条泄露线的修法）。

===============================================================================
为什么这个文件的第一批用例是「阳性对照」
===============================================================================

2026-10-31 的盲评包里，「**有省略号的那侧 = 人类侧**」这条规则
可判 7 对、判对 7 对、**精确率 100%** ⇒ 一条真泄露线。

根因不是「AI 不写省略号」，是**没教**：
我们 0.068 个/百字 vs 参考书 0.684 个/百字（差 10 倍）。

⇒ 这一层做的事与归属校正**同构**：目标从参考书实测 ⇒ 按比例本地补。

下面这批用例把尺子钉住：分母是「对白句数」、位置是「对白结尾」、
数量由本地定（模型数不准）、只动对白不动叙述。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.style.ellipsis_fix import (  # noqa: E402
    apply_ellipsis_habit,
    ellipsis_rate,
    ellipsis_target,
)

#: 真实文本里的**阳性对照**（含省略号的对白，必须被量到）。
POSITIVE = [
    "“我保护你……”他说完就走了。",
    "“别动……求你了。”",
    "“他是怎么走到这一步的……”",
]

#: **阴性对照**：不含省略号的对白（不能被算进去）。
NEGATIVE = [
    "“我知道。”",
    "“走吧。”",
    "他站在廊下，没有再往前一步。",
]

#: 一段够长的正文（超过 `MIN_CHARS`）—— 四句对白 + 叙述。
_BODY = ("“我知道。”\n“你说什么。”\n“我没听清。”\n“我知道了。”\n“你怎么在这儿。”\n"
         "他站在廊下，没有再往前一步。风从堂前穿过去，把案上的纸吹起一角，"
         "纸角翻了两下，又落回原处。屋里没有人说话，只有窗外的雨声一阵一阵，"
         "檐下的水滴落在石阶上，砸出一串细碎的响。他把纸压好，抬头看了看天色，"
         "又低下去，手指在桌沿敲了两下，像是在数什么，又像是在等什么人开口。\n"
         "廊外的灯笼被风推了一下，光影在地上晃了晃，随即又稳住了。"
         "远处有人关门，声音隔着雨传过来，闷闷的一声。\n")


def test_尺认得出含省略号的对白():
    for s in POSITIVE:
        m = ellipsis_rate(s)
        assert m["with_ellipsis"] >= 1, (s, m)
    for s in NEGATIVE:
        m = ellipsis_rate(s)
        assert m["with_ellipsis"] == 0, (s, m)


def test_分母是对白句数不是每百字():
    """同一段文字，多加一句对白 ⇒ 比例应下降（按每百字算就不会）。"""
    text = "“我知道……”\n" + "他说了一句话，屋里没有人应声。\n" * 20
    m = ellipsis_rate(text)
    assert m["quotes"] == 1, m
    assert m["rate"] == 1.0, m


def test_能从参考书段落里量出目标():
    paras = ["“我保护你……”\n“你说什么。”\n"] * 40
    t = ellipsis_target(paras)
    assert t is not None, "量不出目标"
    assert abs(t["rate"] - 0.5) < 0.01, t


def test_对白太少量不出目标():
    assert ellipsis_target(["“走吧。”"]) is None


def test_补省略号只动对白结尾():
    out, info = apply_ellipsis_habit(_BODY, 0.5)
    assert info.get("applied"), info
    m = ellipsis_rate(out)
    assert m["quotes"] == 5, m
    assert m["with_ellipsis"] == 2, (m, out)
    # 叙述句一个字没动
    assert "他站在廊下，没有再往前一步。" in out, out


def test_补完的省略号在引语里面且不重复句号():
    out, _ = apply_ellipsis_habit(_BODY, 0.4)
    assert "。……”" not in out, out
    assert "“我知道……”" in out or "“我知道了……”" in out, out


def test_本来就达标就不动():
    text = _BODY.replace("“我知道。”", "“我知道……”")
    out, info = apply_ellipsis_habit(text, 0.25)
    assert info.get("applied") is False, info
    assert out == text


def test_多了就减掉():
    text = _BODY.replace("“我知道。”", "“我知道……”").replace(
        "“你说什么。”", "“你说什么……”")
    out, info = apply_ellipsis_habit(text, 0.25)
    assert info.get("applied"), info
    assert info.get("removed") == 1, info
    m = ellipsis_rate(out)
    assert m["with_ellipsis"] == 1, (m, out)


def test_可复现_同样的输入得到同样的结果():
    a, _ = apply_ellipsis_habit(_BODY, 0.5)
    b, _ = apply_ellipsis_habit(_BODY, 0.5)
    assert a == b, "配平必须可复现（否则缓存与复核都失效）"


def test_正文太短不动():
    out, info = apply_ellipsis_habit("“走吧。”", 0.5)
    assert info.get("applied") is False, info
