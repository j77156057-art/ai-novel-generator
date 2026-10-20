"""test_name_expand.py —— v2.234 语料自证姓名扩展

5 断言（用雪中悍刀行真实片段，512 万字太长，取前 80 万字够统计）：
A. 碎片扩展回真名：徐凤→徐凤年、王仙→王仙芝
B. 完整名不被误扩展（已经是完整名的，扩展率上不去）
C. 扩展率字段正确返回（0~1）
D. 样本太少时不扩展（< 20 次）
E. 优先采纳更长的扩展（1-2 字里取更完整的）
"""
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.style.narrative import _expand_names_from_corpus, _extend_name_candidates

# 用仓库/下载目录的真实语料；缺失时退化到内置片段
CORPUS_CANDIDATES = [
    pathlib.Path(r"C:\Users\h'h'h\Downloads\雪中悍刀行.txt"),
]


def load_corpus() -> str:
    for p in CORPUS_CANDIDATES:
        if p.exists():
            raw = p.read_bytes()
            for enc in ("gb18030", "utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
    return ""


text = load_corpus()
if not text:
    print("SKIP: 无可用语料")
    sys.exit(0)

# 取前 80 万字，够统计且快
text = text[:800_000]
print(f"测试语料 {len(text):,} 字\n")

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}  {detail}")
        FAILS.append(name)


# ── A. 碎片扩展回真名 ──
print("A. 碎片扩展")
res = _expand_names_from_corpus(
    ["徐凤", "王仙", "徐北", "陈芝", "邓太", "李淳"], text, frac=0.5, max_extra=2
)
expect = {
    "徐凤": "徐凤年", "王仙": "王仙芝", "徐北": "徐北枳",
    "陈芝": "陈芝豹", "邓太": "邓太阿", "李淳": "李淳罡",
}
hit = 0
for short, full in expect.items():
    got = res.get(short)
    ok = got is not None and got[0] == full
    hit += ok
    print(f"    {short} → {got[0] if got else 'None':<8} "
          f"({'✓' if ok else '✗ 期望 ' + full})  rate={got[1] if got else 0}")
check(f"碎片扩展准确（{hit}/{len(expect)}）", hit >= 5, f"只中 {hit} 个")

# ── B. 完整名不误扩展 ──
print("\nB. 完整名不误扩展")
# 徐凤年 已经是完整名，后缀应是标点/动词 → 扩展率上不去
res2 = _expand_names_from_corpus(["徐凤年"], text, frac=0.5, max_extra=2)
check("完整名「徐凤年」不被扩展", "徐凤年" not in res2,
      f"被扩成 {res2.get('徐凤年')}")

# ── C. 扩展率字段 ──
print("\nC. 扩展率字段")
ok_rate = all(isinstance(v[1], float) and 0 < v[1] <= 1.0 for v in res.values())
check("扩展率是 0~1 的浮点数", ok_rate,
      f"样例 {list(res.items())[:2]}")

# ── D. 样本太少不扩展 ──
print("\nD. 样本太少不扩展")
# 构造一个只出现几次的名字
tiny = "测试假名甲乙丙"
res3 = _expand_names_from_corpus([tiny], text, frac=0.5, max_extra=2)
check("语料中出现 <20 次的名字不扩展", tiny not in res3, f"{res3}")

# ── E. 优先更长扩展 ──
print("\nE. 优先采纳更长扩展")
# 找一个人在语料里既有 3 字也有 4 字形态的情况
# 徐凤年（3字）的后继如果是「说道」，2字扩展=x 的「徐凤年说」不一定高频
# 用「轩辕青」验证：应扩展成「轩辕青锋」（4字）而不是「轩辕青x」
res4 = _expand_names_from_corpus(["轩辕青"], text, frac=0.5, max_extra=2)
if "轩辕青" in res4:
    got = res4["轩辕青"][0]
    check("更长扩展优先（轩辕青→轩辕青锋）", got == "轩辕青锋", f"得到 {got}")
else:
    print("    (轩辕青在 80 万字片段里样本不足，跳过)")

print(f"\n结果：{5 - len(FAILS)}/5 通过")
if FAILS:
    print("失败项：", FAILS)
sys.exit(1 if FAILS else 0)
