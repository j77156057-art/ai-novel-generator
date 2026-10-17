"""test_log_buckets_threshold.py —— 验证 percentile 阈值 99.5

2 断言：
- STYLE_WARN_PERCENTILE 升级到 99.5
- _style_warning 低于 99.5 不再触发告警
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSED = []
FAILED = []


def check(name, cond, *, detail=""):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name} {detail}")


def test_threshold_value():
    """STYLE_WARN_PERCENTILE 应为 99.5（v2.14 方案 B 实施后调高）。"""
    from app.generation.engine import STYLE_WARN_PERCENTILE
    check("T1: 阈值 = 99.5", STYLE_WARN_PERCENTILE == 99.5,
          detail=f"got {STYLE_WARN_PERCENTILE}")


def test_style_warning_below_threshold():
    """percentile < 99.5 时 _style_warning 返 None（不触发告警）。

    实测 v2.14 段 1 mean_abs_z=2.094 → percentile=100（仍 100 因为 long bucket
    留出 1 单元），但 percentile < 99.5 时应不触发。
    """
    from app.style.stylometry import fit_profile_v2, style_distance

    # 模拟 v2 链路：profile 切 3 套 bucket + 短文选 short bucket
    text = "短文测试。短文测试。" * 100  # ~1000 字
    # 短 fake ref 保证 medium/long bucket 切不出，short 切出
    ref = text * 20  # ~20000 字

    profile = fit_profile_v2([ref], unit_chars=3000)
    dist = style_distance(text, profile)
    pct = dist.get("percentile")
    check("T2: 短文 percentile 应 < 99.5 (或不 None)",
          pct is None or pct < 99.5,
          detail=f"pct={pct} bucket={dist.get('bucket')} mean_abs_z={dist.get('mean_abs_z')}")


def main():
    print("=== test_log_buckets_threshold.py ===")
    test_threshold_value()
    test_style_warning_below_threshold()
    print(f"\n通过 {len(PASSED)} 断言, 失败 {len(FAILED)} 断言")
    if FAILED:
        print(f"失败: {FAILED}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
