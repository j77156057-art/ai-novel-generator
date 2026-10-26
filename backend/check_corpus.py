"""检查下载语料：编码、繁简、章节标记、正文占比。

Gutenberg 中文书多为繁体，需要转简体（OpenCC 或内置映射），
否则 M2 检索召回与 M3 抽取的 CJK 正则都会偏。
"""
import os
import re
import sys

SEED = os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                    "data", "seed"))

for fn in sorted(f for f in os.listdir(SEED) if f.endswith(".txt")):
    p = os.path.join(SEED, fn)
    raw = open(p, "rb").read()
    # 编码探测
    enc = None
    for cand in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(cand)
            enc = cand
            break
        except UnicodeDecodeError:
            continue
    if enc is None:
        print(f"{fn}:解码失败")
        continue

    # Gutenberg 页眉页脚切除
    m = re.search(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*",
                  text, re.S)
    body = text[m.end():] if m else text
    m2 = re.search(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", body)
    if m2:
        body = body[:m2.start()]

    body = body.strip()
    # 章节标记
    ch_re = re.compile(r"^\s*(第[一二三四五六七八九十百千零〇0-9]+[章回節节卷])",
                       re.M)
    chapters = ch_re.findall(body)
    # CJK 比例
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    total = max(len(body), 1)
    # 传统字检测（抽样高频）
    trad = re.findall(r"[個們這麼說話時後對沒麼樣發現經實現點動長門問間]",
                      body)
    print(f"{fn}:")
    print(f"  编码={enc} 总字符={len(body)} 正文占比={cjk/total:.1%}")
    print(f"  章节标记={len(chapters)} 个样例={chapters[:5]}")
    print(f"  繁体字命中≈{len(trad)}")
    print(f"  正文开头: {body[:90].replace(chr(10),' ')}")
    print()