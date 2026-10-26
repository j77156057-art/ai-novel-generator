"""水印检测回归（第 8 步 P0 修复）。

P0 背景：判据实现把 `final_ratio` 取成了「**任意**句末标点（`。！？`）的并集」，
而模块文档标定的是「紧跟**『。』**的比例」。现代网文充斥对白问句（以『？』收尾），
于是一批正常短语越过阈值被当成水印删掉 —— 用真实《焚天录》实测，`怎么办`/`没事吧`/
`怎么了` 共 936 字正常对白被误删。

本文件把那次复现钉成回归：
  1. 真实文本：只应检出 `风动乾坤`，不得含那三个正常问句短语；
  2. 合成对白：满篇 `怎么办？`/`没事吧？`/`怎么了？`/`是吗？` → 零命中；
  3. 真水印不回归：`风动乾坤` 仍被检出、仍能清掉 ~40498 处、残留个位数。

标 `slow`（真实文本一跑数分钟）；`pytest -m "not slow"` 可跳过。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.corpus_health import (  # noqa: E402
    DEFAULT_MIN_NESTING_BREADTH,
    find_repeated_watermarks,
    scan_watermarks,
    strip_watermarks,
)
from app.ingestion.encoding import read_text_file  # noqa: E402

_DOUPO = os.environ.get("NOVEL_CORPUS") or "__no_corpus__"
_FALSE_POSITIVES = ("怎么办", "没事吧", "怎么了")


# ====================================================================
# 1) 真实文本回归（把 team-lead 的复现钉死）
# ====================================================================


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isfile(_DOUPO), reason="本机无《焚天录》原文")
def test_doupo_detects_only_the_real_watermark():
    text, _enc = read_text_file(_DOUPO)
    hits = find_repeated_watermarks(text)
    names = [h["text"] for h in hits]

    # 只含真水印，且就是风动乾坤
    assert names == ["风动乾坤"], f"检出结果应为 ['风动乾坤']，实际 {names}"
    for bad in _FALSE_POSITIVES:
        assert bad not in names, f"正常问句短语 {bad!r} 被误判为水印"

    # 被拒候选里能看到这些误删风险候选 + 原因（不静默丢弃）
    scan = scan_watermarks(text)
    rejected = {r["text"] for r in scan["rejected"]}
    assert {"怎么办", "没事吧", "怎么了"} <= rejected, (
        f"误删风险候选未记入 rejected：{[r['text'] for r in scan['rejected']]}"
    )


@pytest.mark.slow
@pytest.mark.skipif(not os.path.isfile(_DOUPO), reason="本机无《焚天录》原文")
def test_doupo_strip_removes_watermark_without_touching_dialogue():
    text, _enc = read_text_file(_DOUPO)
    hits = find_repeated_watermarks(text)
    assert hits, "未检出真水印"
    top = hits[0]
    assert top["text"] == "风动乾坤"
    assert top["final_ratio"] >= 0.99
    assert top["nesting_breadth"] >= DEFAULT_MIN_NESTING_BREADTH
    # 仍能清掉约 40498 处（紧随『。』的那些）
    assert top["final"] >= 40_490, top["final"]

    cleaned = strip_watermarks(text, hits)

    # 清洗后残留水印应是个位数（未被『。』跟随的那几处故意不删）
    assert cleaned.count("风动乾坤") <= 10
    # 正常对白一个都不能少
    for q in _FALSE_POSITIVES:
        assert cleaned.count(q) == text.count(q), f"{q} 被误删"


# ====================================================================
# 2) 合成对白：零命中
# ====================================================================


def _dialogue() -> str:
    qs = ("怎么办", "没事吧", "怎么了", "是吗", "对吧")
    return "".join(f"“{qs[i % 5]}？”" for i in range(400))


def test_synthetic_dialogue_zero_hits():
    d = _dialogue()
    assert find_repeated_watermarks(d) == []


# ====================================================================
# 3) 真水印不回归（合成注入，快速）
# ====================================================================


def test_synthetic_injection_still_detected():
    # 水印粘在 12 种不同前导字后、紧跟『。』→ 必须检出，且去嵌套后只剩核心串
    tails = ("说道", "点头", "微笑", "叹气", "皱眉", "转身",
             "沉吟", "开口", "出手", "抬眼", "喃喃", "低语")
    text = "".join(f"正文内容若干{tails[i % 12]}测试水印。" for i in range(90))
    hits = find_repeated_watermarks(text)
    assert [h["text"] for h in hits] == ["测试水印"]
    assert hits[0]["final"] == 90
    assert hits[0]["nesting_breadth"] >= DEFAULT_MIN_NESTING_BREADTH
    assert strip_watermarks(text, hits).count("测试水印") == 0


def test_terminator_ratio_is_what_discriminates():
    """把同一短语分别造在『。』前（真水印形态）与『？』前（正常问句）——对比判据。"""
    at_period = "".join(f"内容若干{t}关键串。" for t in
                        ("说道", "点头", "微笑", "叹气", "皱眉", "转身",
                         "沉吟", "开口", "出手", "抬眼", "喃喃", "低语") * 8)
    at_question = at_period.replace("关键串。", "关键串？")
    assert [h["text"] for h in find_repeated_watermarks(at_period)] == ["关键串"]
    assert find_repeated_watermarks(at_question) == []
