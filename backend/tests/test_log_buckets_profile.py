"""test_log_buckets_profile.py —— Step 1 验证对数粒度 profile 设计

方案 B 实施的第一步：建测试覆盖 5 个核心契约，先 RED 后 GREEN。

20 断言覆盖：
A. fit_profile_v2 切 3 套 bucket（短/中/长）（4 断言）
B. pick_bucket 选 bucket 规则（5 断言）
C. fingerprint 升级（schema 字段）（3 断言）
D. style_distance 用 bucket 选 profile（4 断言）
E. 退化路径：短语料切不出长 bucket（4 断言）

设计文档：docs/style_log_buckets_design.md
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


# 准备参考语料：~30 KB 大奉 5 章 = ~50 个 medium 单元（每 3000 字）
# 短/中/长 bucket 都要能切出至少 4 个单元
def _make_reference():
    """从大奉 5 章（~50K 字）合成 5 个 fake 章节，~50KB 总长，保证三档都够切。"""
    from pathlib import Path
    dafeng5 = Path(r"C:/Users/h'h'h/AppData/Local/hermes/cache/scratch/dafeng_前15章.txt")
    if not dafeng5.exists():
        return None
    return dafeng5.read_text(encoding="utf-8")


def _fake_chapter(seed: int, n_paras: int = 200, para_len: int = 250) -> str:
    """合成 ~50KB 的假章节（用 1000 个不同段落）——保证 cut_units 切得动。"""
    parts = []
    for i in range(n_paras):
        # 每段 ~para_len 字
        s = f"第{seed}章第{i}段。" * (para_len // 8)
        parts.append(s)
    return "\n".join(parts)


# ============================================================================
# A. fit_profile_v2 切 3 套 bucket（4 断言）
# ============================================================================

def test_a_fit_profile_v2_creates_buckets():
    """fit_profile_v2 应返回 profile['buckets'] 含 3 套（短/中/长）。"""
    try:
        from app.style.stylometry import fit_profile_v2  # Step 2 才会实现
    except ImportError:
        check("A1: fit_profile_v2 imported", False)
        return
    # 5 章大奉做参考
    ref = _make_reference() or _fake_chapter(0)
    if len(ref) < 6000:
        check("A1: reference 足够长", False, detail=f"len={len(ref)}")
        return
    profile = fit_profile_v2([ref], unit_chars=3000)
    check("A1: profile 是 dict", isinstance(profile, dict))
    check("A1: profile 含 buckets", "buckets" in profile)
    check("A1: buckets 含 short/medium/long",
          all(k in profile.get("buckets", {}) for k in ("short", "medium", "long")),
          detail=f"got {list(profile.get('buckets', {}).keys())}")


def test_a_bucket_unit_chars():
    """bucket unit_chars 应为 base/2 + base + base*2。"""
    try:
        from app.style.stylometry import fit_profile_v2
    except ImportError:
        check("A2: bucket unit_chars", False, detail="(not implemented yet)")
        return
    ref = _make_reference() or _fake_chapter(0)
    if len(ref) < 6000:
        return
    profile = fit_profile_v2([ref], unit_chars=3000)
    buckets = profile.get("buckets", {})
    if "short" in buckets and "medium" in buckets and "long" in buckets:
        check("A2: short unit_chars = 1500", buckets["short"].get("unit_chars") == 1500)
        check("A2: medium unit_chars = 3000", buckets["medium"].get("unit_chars") == 3000)
        check("A2: long unit_chars = 6000", buckets["long"].get("unit_chars") == 6000)
    else:
        check("A2: bucket unit_chars 全部正确", False, detail="(buckets 缺失)")


def test_a_bucket_has_feat_and_holdout():
    """每个 bucket 应有 feat（20 特征）+ holdout 分布。"""
    try:
        from app.style.stylometry import fit_profile_v2
    except ImportError:
        check("A3: bucket feat/holdout", False, detail="(not implemented yet)")
        return
    ref = _make_reference() or _fake_chapter(0)
    profile = fit_profile_v2([ref], unit_chars=3000)
    for bname in ("short", "medium", "long"):
        bucket = profile.get("buckets", {}).get(bname)
        if bucket is None:
            continue
        check(f"A3: {bname} 有 feat", "feat" in bucket and len(bucket["feat"]) >= 20)
        check(f"A3: {bname} 有 holdout.unit_mean_abs_z", "holdout" in bucket and "unit_mean_abs_z" in bucket["holdout"])


def test_a_bucket_n_units_increases():
    """同一参考，short bucket 单元数应 >= medium bucket >= long bucket。"""
    try:
        from app.style.stylometry import fit_profile_v2
    except ImportError:
        check("A4: bucket 单元数", False, detail="(not implemented yet)")
        return
    ref = _make_reference() or _fake_chapter(0)
    if len(ref) < 12000:
        return
    profile = fit_profile_v2([ref], unit_chars=3000)
    buckets = profile.get("buckets", {})
    n_short = buckets.get("short", {}).get("n_units", 0)
    n_medium = buckets.get("medium", {}).get("n_units", 0)
    n_long = buckets.get("long", {}).get("n_units", 0)
    check("A4: short 单元数 > medium 单元数", n_short > n_medium,
          detail=f"short={n_short} medium={n_medium}")
    check("A4: medium 单元数 >= long 单元数", n_medium >= n_long,
          detail=f"medium={n_medium} long={n_long}")


# ============================================================================
# B. pick_bucket 选 bucket 规则（5 断言）
# ============================================================================

def test_b_pick_bucket_thresholds():
    """按 text 长度选 bucket：< 0.6*base -> short; 0.6-1.7*base -> medium; >= 1.7*base -> long。"""
    try:
        from app.style.stylometry import pick_bucket
    except ImportError:
        check("B1-B5: pick_bucket", False, detail="(not implemented yet)")
        return
    # base = 3000
    base = 3000
    check("B1: 1000 字选 short (0.33x)", pick_bucket(1000, base) == "short")
    check("B2: 1500 字选 short (0.5x, < 0.6x)", pick_bucket(1500, base) == "short")
    check("B3: 1800 字选 medium (0.6x 边界)", pick_bucket(1800, base) == "medium")
    check("B4: 5000 字选 medium (1.67x, < 1.7x)", pick_bucket(5000, base) == "medium")
    check("B5: 6000 字选 long (2.0x)", pick_bucket(6000, base) == "long")


# ============================================================================
# C. fingerprint 升级（3 断言）
# ============================================================================

def test_c_fingerprint_includes_schema():
    """新 fingerprint 应含 schema="log_buckets_v1" 字段，让老 profile 自动失效。"""
    try:
        from app.style.pipeline import profile_fingerprint
        from app.style.stylometry import PROFILE_VERSION
    except ImportError:
        check("C1-C3: fingerprint schema", False, detail="(not implemented yet)")
        return
    fp = profile_fingerprint([1, 2, 3], unit_chars=3000)
    check("C1: fingerprint 含 schema 字段", "schema" in fp)
    check("C2: schema == 'log_buckets_v1'", fp.get("schema") == "log_buckets_v1")
    check("C3: PROFILE_VERSION 升到 2", PROFILE_VERSION == 2)


# ============================================================================
# D. style_distance 用 bucket 选 profile（4 断言）
# ============================================================================

def test_d_style_distance_uses_bucket():
    """style_distance 应按 text 长度自动选 bucket 算 percentile，不再 100% 触发。"""
    try:
        from app.style.stylometry import fit_profile_v2, style_distance
    except ImportError:
        check("D1-D4: style_distance bucket", False, detail="(not implemented yet)")
        return
    ref = _make_reference() or _fake_chapter(0)
    if len(ref) < 6000:
        return
    profile = fit_profile_v2([ref], unit_chars=3000)
    # 用 6000 字（v2.10 智谱段 1 长度）测 distance，应走 long bucket
    text_6000 = ref * 1  # 截到 6000 字
    text_6000 = text_6000[:6000]
    dist = style_distance(text_6000, profile)
    check("D1: distance 返回 bucket 字段", "bucket" in dist)
    check("D2: 6000 字选 long bucket (2.0x)", dist.get("bucket") == "long")
    # 关键断言：percentile < 100（vs v2.10 智谱段 1 100% 触发）
    pct = dist.get("percentile")
    check("D3: 6000 字段 percentile < 100 (vs 旧版 100% 触发)", pct is not None and pct < 100,
          detail=f"pct={pct}")
    # scale_ratio 应有
    check("D4: distance 返回 scale_ratio", "scale_ratio" in dist)


# ============================================================================
# E. 退化路径：短语料切不出长 bucket（4 断言）
# ============================================================================

def test_e_short_reference_long_bucket_omitted():
    """短参考（如 1 章 1.5 万字）切不出 long bucket（6000 字单元 4 个）时，buckets['long'] 应被跳过。"""
    try:
        from app.style.stylometry import fit_profile_v2
    except ImportError:
        check("E1-E4: 退化路径", False, detail="(not implemented yet)")
        return
    # 短参考：3000 字刚好 short 切 1 个，medium 切 1 个，long 切 0 个（floor=3000 不达标）
    short_ref = _fake_chapter(0, n_paras=20, para_len=200)  # ~5KB
    profile = fit_profile_v2([short_ref], unit_chars=3000)
    buckets = profile.get("buckets", {})
    # 短参考切不出 long bucket
    check("E1: 短参考 long bucket 被省略", "long" not in buckets)
    # 但 medium 应该还有（如果够切）
    if len(short_ref) >= 3000:
        check("E2: 短参考 medium bucket 仍存在", "medium" in buckets)
    # style_distance 退化：长 text 选不到 long bucket 应回退到 medium
    try:
        from app.style.stylometry import style_distance
        long_text = short_ref * 4  # ~20KB
        dist = style_distance(long_text, profile)
        check("E3: 长 text 退化到 medium bucket", dist.get("bucket") == "medium",
              detail=f"got {dist.get('bucket')}")
        check("E4: 退化 distance 不崩", dist.get("percentile") is not None)
    except Exception as e:
        check("E3: 退化路径", False, f"ERR {e}")


# ============================================================================
# 跑全部
# ============================================================================

def main():
    print("=== test_log_buckets_profile.py (Step 1 RED) ===")
    test_a_fit_profile_v2_creates_buckets()
    test_a_bucket_unit_chars()
    test_a_bucket_has_feat_and_holdout()
    test_a_bucket_n_units_increases()
    test_b_pick_bucket_thresholds()
    test_c_fingerprint_includes_schema()
    test_d_style_distance_uses_bucket()
    test_e_short_reference_long_bucket_omitted()
    print(f"\n通过 {len(PASSED)} 断言, 失败 {len(FAILED)} 断言")
    if FAILED:
        print(f"失败: {FAILED}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
