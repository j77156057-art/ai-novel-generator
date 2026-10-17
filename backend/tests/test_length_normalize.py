"""test_length_normalize.py —— 方案 C Step 1 RED 测试

25 断言覆盖 5 段：
A. features 签名升级（unit_chars 关键字）+ 旧调用兼容（4 断言）
B. _length_scale_factor 在 1x/2x/0.5x 处的值（4 断言）
C. 8 个非密度特征 vs 12 个密度特征分组（3 断言）
D. 跨长度一致性：相同语料 + 不同长度 → z-score 差异应 < 阈值（5 断言）
E. 退化路径：unit_chars=0/None 不崩（4 断言）
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


# ============================================================================
# A. features 签名升级（unit_chars 关键字）+ 旧调用兼容（4 断言）
# ============================================================================

def test_a_features_signature():
    """features 应接受 unit_chars 关键字；旧调用不带 unit_chars 也兼容（默认 3000）。"""
    try:
        from app.style.stylometry import features
    except ImportError as e:
        check("A1: features imported", False, detail=f"ERR {e}")
        return
    text = "短句。短句。短句。\n\n" * 50  # ~500 字
    # 新签名带 unit_chars=1500
    try:
        vec = features(text, unit_chars=1500)
        check("A1: features 接受 unit_chars 关键字", isinstance(vec, dict))
        check("A2: features 返 20 特征", len(vec) == 20, detail=f"got {len(vec)}")
    except TypeError as e:
        check("A1: features 接受 unit_chars 关键字", False, detail=f"TypeError: {e}")
        return
    # 旧调用不带 unit_chars 仍兼容
    try:
        vec_old = features(text)
        check("A3: 旧调用 features(text) 不报错", isinstance(vec_old, dict))
        check("A4: 旧调用与新调用返同 20 特征", set(vec_old.keys()) == set(vec.keys()))
    except TypeError as e:
        check("A3: 旧调用兼容", False, detail=f"TypeError: {e}")


# ============================================================================
# B. _length_scale_factor 在 1x/2x/0.5x 处的值（4 断言）
# ============================================================================

def test_b_length_scale_factor():
    """归一化因子：1x=1.0 / 2x=0.7 / 0.5x=1.3 / 0.01x 趋近 0。"""
    try:
        from app.style.stylometry import _length_scale_factor
    except ImportError:
        check("B1-B4: _length_scale_factor", False, detail="(not implemented yet)")
        return
    # 1x 基准
    check("B1: 1x 长度时 scale = 1.0",
          abs(_length_scale_factor(3000, 3000) - 1.0) < 0.01,
          detail=f"got {_length_scale_factor(3000, 3000)}")
    # 2x 长度
    check("B2: 2x 长度时 scale ≈ 0.81 (双向收敛)",
          abs(_length_scale_factor(6000, 3000) - 0.81) < 0.05,
          detail=f"got {_length_scale_factor(6000, 3000)}")
    # 0.5x 长度（双向收敛：2x 跟 0.5x 收敛到同一值）
    check("B3: 0.5x 长度时 scale ≈ 0.81 (对称收敛)",
          abs(_length_scale_factor(1500, 3000) - 0.81) < 0.05,
          detail=f"got {_length_scale_factor(1500, 3000)}")
    # unit_chars=0 退化
    try:
        v = _length_scale_factor(3000, 0)
        check("B4: unit_chars=0 不崩（返 1.0）", v == 1.0, detail=f"got {v}")
    except Exception as e:
        check("B4: unit_chars=0 退化", False, detail=f"ERR {e}")


# ============================================================================
# C. 8 个非密度特征 vs 12 个密度特征分组（3 断言）
# ============================================================================

def test_c_feature_groups():
    """方案 C 文档识别 8 个非密度特征；其余 12 个是密度类。"""
    try:
        from app.style.stylometry import NON_DENSITY_FEATURES, DENSITY_FEATURES
    except ImportError:
        check("C1-C3: NON_DENSITY_FEATURES / DENSITY_FEATURES", False, detail="(not implemented yet)")
        return
    from app.style.stylometry import FEATURES
    check("C1: NON_DENSITY_FEATURES 集合存在", isinstance(NON_DENSITY_FEATURES, (set, frozenset, tuple)))
    check("C2: NON_DENSITY + DENSITY = FEATURES 全部 20 特征",
          set(NON_DENSITY_FEATURES) | set(DENSITY_FEATURES) == set(FEATURES))
    check("C3: NON_DENSITY 与 DENSITY 不重叠",
          set(NON_DENSITY_FEATURES) & set(DENSITY_FEATURES) == set())


# ============================================================================
# D. 跨长度一致性（5 断言）
# ============================================================================

def test_d_cross_length_consistency():
    """相同语料 + 不同长度 → fit 同一 profile 后，z-score 差异应 < 阈值。

    真链路 8 轮冒烟验证：v2.10 智谱段 1 (6264 字) vs v2.10 智谱段 2 (4747 字)，
    percentile 都 100%——证明 z 在不同长度上系统性偏大。方案 C 应让 z 接近。
    """
    try:
        from app.style.stylometry import fit_profile_v2, style_distance, features
    except ImportError:
        check("D1-D3: 跨长度一致性", False, detail="(not implemented yet)")
        return
    # 制造两份语料：相同密度但不同长度
    # 2000 字 + 6000 字（3x 长度）
    text_2k = "甲乙丙丁戊己庚辛壬癸" * 200  # 2000 字
    text_6k = "甲乙丙丁戊己庚辛壬癸" * 600  # 6000 字
    # 拟合 profile（用 2000 字 ref）
    try:
        profile = fit_profile_v2([text_2k], unit_chars=2000)
    except TypeError:
        check("D1-D3: fit_profile_v2", False, detail="(unit_chars not supported yet)")
        return
    # style_distance 算 distance
    d_2k = style_distance(text_2k, profile)
    d_6k = style_distance(text_6k, profile)
    check("D1: 2k 字段 distance 不为 None", d_2k.get("mean_abs_z") is not None)
    check("D2: 6k 字段 distance 不为 None", d_6k.get("mean_abs_z") is not None)
    # 关键断言：归一化后，2k vs 6k 字段的 distance 比例应 < 3x（未归一化时差 5x+）
    ratio = d_6k["mean_abs_z"] / max(d_2k["mean_abs_z"], 0.01)
    check(f"D3: 6k/2k distance 比例 < 3.0 (方案 C 归一化有效)",
          ratio < 3.0,
          detail=f"d_2k={d_2k['mean_abs_z']} d_6k={d_6k['mean_abs_z']} ratio={ratio:.2f}")


# ============================================================================
# E. 退化路径（4 断言）
# ============================================================================

def test_e_degradation():
    """unit_chars=0/None 时 features/style_distance 不崩。"""
    try:
        from app.style.stylometry import features, style_distance
        from app.style.stylometry import fit_profile_v2
    except ImportError:
        check("E1-E4: 退化路径", False, detail="(not implemented yet)")
        return
    text = "短句。短句。\n\n" * 100
    # unit_chars=0
    try:
        vec = features(text, unit_chars=0)
        check("E1: features(text, unit_chars=0) 不崩", isinstance(vec, dict))
        check("E2: 返 20 特征", len(vec) == 20)
    except TypeError:
        check("E1: features unit_chars=0", False, detail="(keyword not supported yet)")
        check("E2: 返 20 特征", False, detail="(skipped)")
    except Exception as e:
        check("E1: features unit_chars=0", False, detail=f"ERR {e}")
    # unit_chars=None
    try:
        vec_none = features(text, unit_chars=None)
        check("E3: features(text, unit_chars=None) 不崩", isinstance(vec_none, dict))
    except Exception as e:
        check("E3: features unit_chars=None", False, detail=f"ERR {e}")
    # style_distance 在 unit_chars=0 的 profile 上不崩
    try:
        prof = fit_profile_v2([text * 5], unit_chars=3000)
        d = style_distance(text, prof)
        check("E4: style_distance 不崩", d is not None and "mean_abs_z" in d)
    except Exception as e:
        check("E4: style_distance 退化", False, detail=f"ERR {e}")


# ============================================================================
# 跑全部
# ============================================================================

def main():
    print("=== test_length_normalize.py (Step 1 RED) ===")
    test_a_features_signature()
    test_b_length_scale_factor()
    test_c_feature_groups()
    test_d_cross_length_consistency()
    test_e_degradation()
    print(f"\n通过 {len(PASSED)} 断言, 失败 {len(FAILED)} 断言")
    if FAILED:
        print(f"失败: {FAILED}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
