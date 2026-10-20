"""验证 _sample_chapters_v6 的字数下限保护：短篇全取，长篇保底 30 万字。"""
import sys
sys.path.insert(0, ".")

from app.style.narrative import MIN_SAMPLE_CHARS, SAMPLE_RATIO, _sample_chapters_v6

FAILS = []


def check(desc, got, want):
    ok = got == want
    if not ok:
        FAILS.append(desc)
    print(f"{'PASS' if ok else 'FAIL'}  {desc}: got={got!r} want={want!r}")


def mk_chapters(n, ch_chars):
    """造 n 章，每章 ch_chars 字。"""
    return [(i + 1, "字" * ch_chars) for i in range(n)]


# ── 短篇：全量不足 30 万字 → 全取 ──
# 50 章 × 5000 字 = 25 万字 < 30 万 → 全取 50 章
short = mk_chapters(50, 5000)
r = _sample_chapters_v6(short)
check("短篇 25 万字全取", len(r), 50)
check("短篇返回全章节号", [c[0] for c in r], list(range(1, 51)))

# 100 章 × 3000 = 30 万字，刚好等于下限 → 全取（<= 判断）
edge = mk_chapters(100, 3000)
r = _sample_chapters_v6(edge)
check("刚好 30 万字全取", len(r), 100)

# 空输入
check("空输入返回空", _sample_chapters_v6([]), [])

# ── 长篇：全量超下限，采样须保底字数 ──
# 600 章 × 3000 字 = 180 万字。SAMPLE_RATIO 采样后若 < 30 万下限，
# 应扩容到 100 章（30万/3000），而非停在比例值。
long_novel = mk_chapters(600, 3000)
r = _sample_chapters_v6(long_novel)
sampled_chars = sum(len(t) for _, t in r)
print(f"\n长篇 600章/180万字: 采样 {len(r)} 章 / {sampled_chars} 字")
check("长篇采样章节数 >= 字数下限所需", len(r) >= 100, True)
check("长篇采样字数 >= 30万下限", sampled_chars >= MIN_SAMPLE_CHARS, True)
# 不应膨胀到全量
check("长篇采样不膨胀到全量", len(r) < 600, True)

# 章长大时长篇：600 章 × 5000 = 300 万字。比例采样目标 > 30 万下限，
# 应恰好是 SAMPLE_RATIO × 600 章，不被下限撑大
# 用 SAMPLE_RATIO 动态算期望值——改采样比例时这个断言自动跟随，不会失效
long_big = mk_chapters(600, 5000)
r = _sample_chapters_v6(long_big)
sc = sum(len(t) for _, t in r)
expected = int(600 * SAMPLE_RATIO)
print(f"大章长篇 600章/300万字: 采样 {len(r)} 章 / {sc} 字 "
      f"(SAMPLE_RATIO={SAMPLE_RATIO} → 期望 {expected} 章)")
check(f"大章长篇按比例采样({expected}章)", len(r), expected)

# 超长篇但每章极短：2000 章 × 500 字 = 100 万字。
# 15% = 300 章 = 15 万字 < 30 万 → 扩容到 600 章（30万/500）
long_thin = mk_chapters(2000, 500)
r = _sample_chapters_v6(long_thin)
sc = sum(len(t) for _, t in r)
print(f"短章超长篇 2000章/100万字: 采样 {len(r)} 章 / {sc} 字")
check("短章长篇采样字数 >= 30万", sc >= MIN_SAMPLE_CHARS, True)
check("短章长篇未全取", len(r) < 2000, True)

print(f"\n结果：{'全部通过' if not FAILS else f'{len(FAILS)} 项失败 -> {FAILS}'}")
sys.exit(1 if FAILS else 0)
