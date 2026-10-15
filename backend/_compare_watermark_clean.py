"""一次性脚本：对《斗破苍穹》跑一遍编码探测 + 语料清洗，产出对比数据（第 8 步交付 4）。

只读 Downloads 里原文，不改动。输出 JSON 到 stdout（供人抄进 docs/corpus/ 对比报告）。

用法（在 backend/ 目录下）：
    python _compare_watermark_clean.py "C:/Users/h'h'h/Downloads/斗破苍穹.txt"
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.corpus_health import scan_watermarks, strip_watermarks  # noqa: E402
from app.ingestion.encoding import read_text_file  # noqa: E402

#: 残留检查用的特征串（水印 / 盗版站标记 / 乱码）。
RESIDUAL_TOKENS = ("武动乾坤", "最新章节", "请记住", "www.", "。武动乾坤")


def _count_overlaps(text: str, needle: str) -> int:
    return len(re.findall(re.escape(needle), text))


def _samples_around(text: str, needle: str, n: int = 5, pad: int = 40) -> list[dict]:
    """取前 n 处出现的原文上下文（各 ±pad 字），用于人工复核。"""
    out = []
    for i, m in enumerate(re.finditer(re.escape(needle), text)):
        if i >= n:
            break
        a, b = max(0, m.start() - pad), min(len(text), m.end() + pad)
        out.append({
            "pos": m.start(),
            "before": text[a:m.start()].replace("\n", "⏎"),
            "hit": needle,
            "after": text[m.end():b].replace("\n", "⏎"),
        })
    return out


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Downloads/斗破苍穹.txt")
    text, detected = read_text_file(path)

    scan = scan_watermarks(text)
    before = scan["flagged"]
    cleaned = strip_watermarks(text, before)

    residual = scan_watermarks(cleaned)["flagged"]

    primary = before[0]["text"] if before else ""
    report = {
        "file": os.path.basename(path),
        "path": path,
        "detected_encoding": detected,
        "chars_in": len(text),
        "chars_out": len(cleaned),
        "removed_chars": len(text) - len(cleaned),
        "removed_ratio": round((len(text) - len(cleaned)) / max(len(text), 1), 6),
        "thresholds": scan["thresholds"],
        "before_marks": [
            {
                "text": m["text"],
                "count": m["count"],
                "final": m["final"],
                "final_ratio": m["final_ratio"],
                "pooled_ratio": m["pooled_ratio"],
                "nesting_breadth": m["nesting_breadth"],
                "para_end_ratio": m["para_end_ratio"],
                "nested_variants": m["nested_variants"][:10],
            }
            for m in before
        ],
        "rejected_candidates": [
            {
                "text": r["text"],
                "count": r["count"],
                "final_ratio": r["final_ratio"],
                "pooled_ratio": r["pooled_ratio"],
                "reason": r["reason"],
            }
            for r in scan["rejected"]
        ],
        "residual_marks": [
            {"text": m["text"], "count": m["count"], "final_ratio": m["final_ratio"]}
            for m in residual
        ],
        "residual_token_counts_before": {t: _count_overlaps(text, t) for t in RESIDUAL_TOKENS},
        "residual_token_counts_after": {t: _count_overlaps(cleaned, t) for t in RESIDUAL_TOKENS},
        "replacement_char_before": text.count("\ufffd"),
        "replacement_char_after": cleaned.count("\ufffd"),
        "head_60": text[:60].replace("\r", "\\r").replace("\n", "\\n"),
        "samples": _samples_around(text, primary, n=5, pad=40) if primary else [],
    }
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
