"""一次性标定脚本：为「嵌套变体广度 >= N」这一必要条件选 N（第 8 步 P0 修复用）。

对真实网文枚举与 `corpus_health` 完全同口径的候选（长度 3~6 的连续汉字 n-gram、
计数 >= 60、紧跟『。』的比例 >= 0.9），打印每个候选的：
- count
- ratio('。')
- breadth = **不同前导字的种类数**（真水印被程序粘在任意前导字上 → breadth 大）

只读原文。用法（backend/ 下）：
    python _calibrate_breadth.py
"""
from __future__ import annotations

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.ingestion.encoding import read_text_file  # noqa: E402

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LENGTHS = (3, 4, 5, 6)
_MIN_COUNT = 60
_MIN_RATIO = 0.9

FILES = [
    "C:/Users/h'h'h/Downloads/斗破苍穹.txt",
    "C:/Users/h'h'h/Downloads/大奉打更人.txt",
    "C:/Users/h'h'h/Downloads/豪门宠婚，爵少你别闹.txt",
]


def candidates(text: str) -> list[dict]:
    cand: Counter = Counter()
    for rm in _CJK_RUN.finditer(text):
        s = rm.group(0)
        for L in _LENGTHS:
            for i in range(len(s) - L + 1):
                cand[s[i:i + L]] += 1

    n = len(text)
    out: list[dict] = []
    for s, approx in cand.items():
        if approx < _MIN_COUNT:
            continue
        occ = [(m.start(), m.end()) for m in re.finditer(re.escape(s), text)]
        cnt = len(occ)
        if cnt < _MIN_COUNT:
            continue
        final = sum(1 for _, e in occ if e < n and text[e] == "。")
        ratio = final / cnt
        if ratio < _MIN_RATIO:
            continue
        leads = {text[st - 1] for st, _ in occ if st > 0}
        out.append({
            "text": s, "count": cnt, "final": final,
            "ratio": round(ratio, 4), "breadth": len(leads),
        })
    out.sort(key=lambda d: (-d["count"], -d["breadth"]))
    return out


def main() -> int:
    for path in FILES:
        if not os.path.isfile(path):
            print(f"[skip] {path}")
            continue
        text, enc = read_text_file(path)
        cands = candidates(text)
        print("=" * 78)
        print(f"{os.path.basename(path)}  enc={enc}  chars={len(text)}  "
              f"候选数={len(cands)}")
        print(f"{'串':<10}{'count':>8}{'ratio(。)':>11}{'breadth':>9}")
        for c in cands[:25]:
            print(f"{c['text']:<10}{c['count']:>8}{c['ratio']:>11}{c['breadth']:>9}")
        if len(cands) > 25:
            print(f"  … 另 {len(cands) - 25} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
