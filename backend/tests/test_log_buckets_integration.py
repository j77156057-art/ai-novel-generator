"""test_log_buckets_integration.py —— Step 3 端到端验证

5 段覆盖：
F. build_reference_profile 返 v2 buckets 嵌套结构
G. describe(profile) 在 v2 profile 也能输出「文体基线」段
H. feature_table(profile) 在 v2 profile 也能返 20 特征行
I. 端到端：fit_profile_v2 3 套 + describe 描述 + style_distance 选 bucket 算 percentile
J. v1 老 profile（仅顶层 feat）仍能工作（向后兼容）
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


def _fake_chapter(seed: int = 0, n_paras: int = 200, para_len: int = 250) -> str:
    """~50KB fake 章节。"""
    parts = []
    for i in range(n_paras):
        s = f"第{seed}章第{i}段。" * (para_len // 8)
        parts.append(s)
    return "\n".join(parts)


# ============================================================================
# F. build_reference_profile 返 v2 buckets 嵌套结构
# ============================================================================

def test_f_build_reference_profile_v2():
    """build_reference_profile 调 fit_profile_v2，返 version=2 + buckets 嵌套。"""
    try:
        from app.style.pipeline import build_reference_profile
    except ImportError as e:
        check("F1: build_reference_profile imported", False, detail=f"ERR {e}")
        return
    text = _fake_chapter(0)
    profile = build_reference_profile({"test_novel": text}, unit_chars=3000, clean=False)
    check("F1: profile 返 dict", isinstance(profile, dict))
    check("F2: profile.version == 2", profile.get("version") == 2,
          detail=f"got version={profile.get('version')}")
    check("F3: profile 含 buckets 嵌套", "buckets" in profile)
    check("F4: buckets 至少含 medium",
          "medium" in profile.get("buckets", {}),
          detail=f"buckets={list(profile.get('buckets', {}).keys())}")
    check("F5: medium bucket 含 feat + holdout",
          all(k in profile.get("buckets", {}).get("medium", {}) for k in ("feat", "holdout")))


# ============================================================================
# G. describe(profile) 兼容 v2
# ============================================================================

def test_g_describe_v2():
    """describe(profile) 在 v2 profile 也应输出【文体基线】段（取 medium bucket.feat）。"""
    try:
        from app.style.pipeline import build_reference_profile
        from app.style.stylometry import describe
    except ImportError as e:
        check("G1: describe imported", False, detail=f"ERR {e}")
        return
    text = _fake_chapter(0)
    profile = build_reference_profile({"test_novel": text}, unit_chars=3000, clean=False)
    desc = describe(profile)
    check("G1: describe 返非空", bool(desc))
    check("G2: describe 含「文体基线」", "文体基线" in desc)
    check("G3: describe 含 unit_chars 数字", "3000" in desc or "1500" in desc)


# ============================================================================
# H. feature_table(profile) 兼容 v2
# ============================================================================

def test_h_feature_table_v2():
    """feature_table(profile) 在 v2 profile 返 20 行（从 medium bucket.feat）。"""
    try:
        from app.style.pipeline import build_reference_profile
        from app.style.stylometry import feature_table
    except ImportError as e:
        check("H1: feature_table imported", False, detail=f"ERR {e}")
        return
    text = _fake_chapter(0)
    profile = build_reference_profile({"test_novel": text}, unit_chars=3000, clean=False)
    rows = feature_table(profile)
    check("H1: feature_table 返 list", isinstance(rows, list))
    check("H2: 20 行（20 特征）", len(rows) == 20, detail=f"got {len(rows)}")
    check("H3: 第一行含 name/note/mean", all(k in rows[0] for k in ("name", "note", "mean", "std")))


# ============================================================================
# I. 端到端：fit → describe → style_distance
# ============================================================================

def test_i_end_to_end():
    """完整链路：fit profile → describe → style_distance 选 bucket 算 percentile。"""
    try:
        from app.style.pipeline import build_reference_profile
        from app.style.stylometry import describe, style_distance, FEATURES
    except ImportError as e:
        check("I1: end-to-end imports", False, detail=f"ERR {e}")
        return
    text = _fake_chapter(0)
    profile = build_reference_profile({"test_novel": text}, unit_chars=3000, clean=False)
    # describe 应有文体基线
    desc = describe(profile)
    check("I1: describe 输出非空", "文体基线" in desc)
    # style_distance 对 6000 字段（v2.10 智谱段 1 长度）—— 选 long bucket
    text_6000 = text[:6000]
    dist = style_distance(text_6000, profile)
    check("I2: 6000 字段 distance 返回", dist is not None)
    check("I3: distance.bucket == 'long'", dist.get("bucket") == "long",
          detail=f"got {dist.get('bucket')}")
    check("I4: distance 含 mean_abs_z", "mean_abs_z" in dist)
    check("I5: distance 含 scale_ratio", "scale_ratio" in dist)
    # 短字段应选 short bucket
    text_1000 = text[:1000]
    dist_short = style_distance(text_1000, profile)
    check("I6: 1000 字段 distance.bucket == 'short'", dist_short.get("bucket") == "short",
          detail=f"got {dist_short.get('bucket')}")


# ============================================================================
# J. v1 老 profile 兼容（向后兼容）
# ============================================================================

def test_j_v1_compat():
    """v1 profile（顶层 feat/holdout）仍能算 style_distance + describe。"""
    try:
        from app.style.stylometry import (
            fit_profile, style_distance, describe, PROFILE_VERSION
        )
    except ImportError as e:
        check("J1: v1 compat imports", False, detail=f"ERR {e}")
        return
    text = _fake_chapter(0)
    # 强制 v1 路径
    v1_profile = fit_profile([text], unit_chars=3000)
    check("J1: fit_profile 仍存在", isinstance(v1_profile, dict))
    # style_distance 在 v1 path 应走 else 分支（bucket="v1"）
    dist = style_distance(text[:3000], v1_profile)
    check("J2: v1 profile distance 返 v1 bucket",
          dist.get("bucket") == "v1",
          detail=f"got {dist.get('bucket')}")
    # describe 在 v1 profile 也应工作
    desc = describe(v1_profile)
    check("J3: v1 profile describe 返文体基线", "文体基线" in desc)


# ============================================================================
# 跑全部
# ============================================================================

def main():
    print("=== test_log_buckets_integration.py (Step 3 integration) ===")
    test_f_build_reference_profile_v2()
    test_g_describe_v2()
    test_h_feature_table_v2()
    test_i_end_to_end()
    test_j_v1_compat()
    print(f"\n通过 {len(PASSED)} 断言, 失败 {len(FAILED)} 断言")
    if FAILED:
        print(f"失败: {FAILED}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
