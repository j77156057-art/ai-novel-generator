"""P1 生成后文风告警：test_style_warning.py

覆盖：
- 正常文本不触发告警（同源语料片段）
- 明显偏离的文本触发告警
- 无档案时静默跳过（档案是增强，不是前提）
- 空文本不触发
- style_distance 抛异常时不阻断
- 告警字段结构完整
- v2.231 双路径：留出充足走 percentile / 不足走绝对 z 兜底

## 语料选择（v2.231 修正）

旧版用 12 个段落 × 200 轮复制构造语料，段尾加 `（round_i-j）` 标记。
这些单元彼此几乎无差异，导致档案 std 趋近 0（实测段均句数 std = 0.008），
任何真实文本的 z 都爆到 27~90，必然触发告警——测试自身构造有缺陷。

改用仓库自带的真实白话小说 `data/seed/豆棚闲话.txt`（11.2 万字，人工写作，
天然有方差）。同源片段实测 mean_abs_z 0.47~1.09，AI 腔 6.05~36.91，分离度 5.6 倍。
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.generation.engine import (
    STYLE_WARN_PERCENTILE, STYLE_WARN_ABS_Z, _style_warning,
)
from app.style.stylometry import fit_profile, _MIN_HOLDOUT_FOR_PERCENTILE

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


#: 仓库自带真实语料（人工写作，有自然方差）
SEED_CORPUS = BACKEND.parent / "data" / "seed" / "豆棚闲话.txt"


def make_profile():
    """用真实白话小说拟合档案。

    豆棚闲话 11.2 万字 → 37 个单元（28 训练 / 9 留出）。
    9 个留出 < _MIN_HOLDOUT_FOR_PERCENTILE(20)，所以走绝对 z 兜底路径——
    这正是 v2.231 要覆盖的场景。
    """
    text = SEED_CORPUS.read_text(encoding="utf-8")
    return fit_profile([text], unit_chars=3000), text


def main():
    if not SEED_CORPUS.exists():
        print(f"SKIP: 语料不存在 {SEED_CORPUS}")
        return 0

    profile, corpus = make_profile()
    check("档案有留出单元", profile.get("n_holdout", 0) > 0)

    # ── 1. 同源文本不触发 ──
    # 从语料切出若干片段（与档案同源、同风格），全部不该告警
    chunks = [corpus[i:i + 3000] for i in range(0, len(corpus), 3000)
              if len(corpus[i:i + 3000]) >= 2000]
    triggered = [i for i, c in enumerate(chunks) if _style_warning(c, profile) is not None]
    check(f"同源 {len(chunks)} 个片段全不触发（误报 {len(triggered)} 个）", not triggered)

    # ── 2. 明显偏离的文本触发告警 ──
    weird_text = (
        "像火一样燃烧！\n"
        "如同山崩地裂！\n"
        "仿佛世界末日！\n"
        "宛如流星划过！\n"
        "犹如天崩地裂！\n"
    ) * 60
    result = _style_warning(weird_text, profile)
    check("偏离文本触发告警", result is not None)
    if result:
        check("告警含 mean_abs_z", "mean_abs_z" in result)
        check("告警含 worst", "worst" in result)
        check("告警含 message", "message" in result)

    # ── 3. 三类 AI 腔都要被抓到（v2.231 兜底路径的召回率） ──
    ai_cases = {
        "等长段+升华": "他知道，这一切都将改变。\n他明白，命运已经注定。\n他相信，未来终会光明。\n" * 60,
        "情绪直陈": ("他的眼中闪过一丝悲凉，心中充满了无尽的痛苦。\n"
                     "她的眼神里露出深深的绝望，内心充满了难以言说的悲伤。\n") * 40,
        "短句刷屏": "好。\n" * 500,
    }
    for name, t in ai_cases.items():
        check(f"AI 腔「{name}」被抓到", _style_warning(t, profile) is not None)

    # ── 4. 无档案时静默跳过 ──
    check("无档案返回 None", _style_warning(chunks[0], None) is None)

    # ── 5. 空文本不触发 ──
    check("空文本返回 None", _style_warning("", profile) is None)
    check("None 文本返回 None", _style_warning(None, profile) is None)

    # ── 6. style_distance 抛异常时不阻断 ──
    class BrokenProfile(dict):
        def get(self, key, default=None):
            if key == "feat":
                raise ValueError("模拟档案损坏")
            return super().get(key, default)

    result = _style_warning(chunks[0], BrokenProfile(profile))
    check("档案损坏不抛异常", result is None)

    # ── 7. 阈值常量合理 ──
    check("percentile 阈值是浮点数", isinstance(STYLE_WARN_PERCENTILE, float))
    check("percentile 阈值在高分位范围", 50 < STYLE_WARN_PERCENTILE <= 100)
    check("绝对 z 阈值是浮点数", isinstance(STYLE_WARN_ABS_Z, float))
    check("绝对 z 阈值合理", 1.0 < STYLE_WARN_ABS_Z < 10.0)

    # ── 8. 留出不足时 percentile 应为 None（v2.231 契约） ──
    if profile.get("n_holdout", 0) < _MIN_HOLDOUT_FOR_PERCENTILE:
        from app.style.stylometry import style_distance
        d = style_distance(chunks[0], profile)
        check("留出不足时 percentile 为 None", d.get("percentile") is None)

    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
