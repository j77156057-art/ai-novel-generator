"""Gutenberg 公版中文书预处理：切页眉页脚 → 繁转简 → 清洗 → 输出干净 txt。

为什么需要：
1. Gutenberg 文件头尾有 Project Gutenberg 的版权声明与许可全文（约占 20%+
   的非正文），直接入库会污染检索（RAG 召回一大段英文声明）；
2. 中文书多为繁体，M2 检索与 M3 抽取的正则都按简体 CJK 设计，
   不转简会显著降低召回与抽取质量。

用法：
    python prep_gutenberg.py            # 处理 data/seed/pg*.txt
    python prep_gutenberg.py --keep-t2s # 跳过繁简转换（保留原文件）
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = os.path.abspath(os.path.join(HERE, "..", "data", "seed"))

# Gutenberg 页眉页脚
START_RE = re.compile(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG.*?\*\*\*", re.S)
END_RE = re.compile(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", re.S)
# 章节标题行：第X回 / 第X章 / 第X节（中英文数字，含全角空格）
CH_TITLE_RE = re.compile(
    r"^\s*(第[一二三四五六七八九十百千零〇两0-9]+[章回節节卷部篇折])[ \u3000]*"
    r"([^ \n\u3000]{0,40})[ \t]*$"
)
# 行内 Project Gutenberg 噪音
NOISE_RE = re.compile(r"(Project Gutenberg|Produced by|END OF THE PROJECT|"
                       r"www\.gutenberg\.org|Proofread by|\[Illustration)", re.I)


def strip_gutenberg(text: str) -> str:
    m = START_RE.search(text)
    if m:
        text = text[m.end():]
    m2 = END_RE.search(text)
    if m2:
        text = text[:m2.start()]
    return text


def clean(lines: list[str]) -> list[str]:
    """去空行连排、去噪音行、把章节标题规整成「第X回标题」独立成行。"""
    out: list[str] = []
    for raw in lines:
        line = raw.replace("\u3000", " ").replace("\xa0", " ").strip()
        if NOISE_RE.search(line):
            continue
        # 纯分隔线（长横线/等号）
        if line and re.fullmatch(r"[-=—–_*\s]{3,}", line):
            continue
        m = CH_TITLE_RE.match(line)
        if m:
            no, title = m.group(1), m.group(2).strip()
            out.append(f"{no}　{title}" if title else no)
            continue
        out.append(line)
    # 折叠 3+ 连续空行
    cleaned: list[str] = []
    blanks = 0
    for line in out:
        if line:
            blanks = 0
            cleaned.append(line)
        else:
            blanks += 1
            if blanks <= 1:
                cleaned.append("")
    return cleaned


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-t2s", action="store_true", help="跳过繁简转换")
    args = ap.parse_args()

    try:
        from opencc import OpenCC
        cc = None if args.keep_t2s else OpenCC("t2s")
    except ImportError:
        print("[warn] opencc 未安装，跳过繁简转换（pip install "
              "opencc-python-reimplemented）")
        cc = None

    files = sorted(f for f in os.listdir(SEED)
                   if f.startswith("pg") and f.endswith(".txt"))
    if not files:
        print(f"{SEED} 下没有 pg*.txt，无需处理")
        return 0

    for fn in files:
        src = os.path.join(SEED, fn)
        text = open(src, "r", encoding="utf-8-sig", errors="replace").read()
        body = strip_gutenberg(text)
        body = "\n".join(clean(body.splitlines()))
        if cc is not None:
            body = cc.convert(body)

        # 保留中文名，便于入库时识别书名
        stem = fn[:-4]
        dst = os.path.join(SEED, f"{stem}.clean.txt")
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

        cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
        # 章节统计用全文 findall（与 M1 parse_txt 的口径一致），
        # 不用逐行 match —— 红楼梦这类标题是「第一回 甄士隐梦幻识通灵 贾雨村风尘怀闺秀」
        # 的对仗双词组，含空格，逐行 match 会被截断误判为非标题。
        chapters = re.findall(r"^\s*第[一二三四五六七八九十百千零〇两0-9]+"
                              r"[章回節节卷部篇折][ \u3000]", body, re.M)
        print(f"{fn} -> {os.path.basename(dst)}")
        print(f"  字符={len(body)} 汉字={cjk} 章节={len(chapters)}")
        print(f"  开头: {body[:70].replace(chr(10),' / ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())