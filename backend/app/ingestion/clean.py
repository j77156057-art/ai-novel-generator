"""入库前的语料清洗：检出并剔除盗版站注入的 SEO 水印，**并留下可复核的清洗报告**。

## 为什么

`corpus_health` 早在第 6 步就能检出重复水印（位置分布判据：水印 100% 落句末、
紧跟「。」），但它此前**只在拟合文体档案时**被调用（`style/pipeline.py`）——
导入链路上完全没接，脏文本照样进 `novel_chunks` 参与检索召回。

实测事故：`novel_dev.db` 里《斗破苍穹》语料中「武动乾坤」（另一本书的书名）
被注入 3,750+ 次，几乎每处都焊在句末（`…带起了一阵嘲讽的骚动武动乾坤。`）。

## 两条硬约束

1. **留痕**：清洗是**有损**的，删了什么、删了多少必须能查、能复核，
   所以每串都附**原始上下文样本**（沿用 `corpus_health` 自己的做法），
   并把整份报告持久化到 `raw_novels.clean_report`（JSON），经 API 可读。
2. **确定性**：同一输入两次清洗必须逐字节一致，便于事后复核。实现上只依赖
   `corpus_health` 的确定性检出 + 取**首次出现**的样本，不含任何随机/时间因素。
"""
from __future__ import annotations

from app.corpus_health import scan_watermarks, strip_watermarks

#: 清洗报告的**形状**版本号。任何会改变报告字段的改动都要 +1，
#: 便于事后区分「旧报告」与「新报告」（老报告缺字段时按缺失处理）。
#: v2：新增 rejected_candidates / thresholds（记录「考虑过但排除」的候选）。
CLEAN_REPORT_VERSION = 2

#: 报告里最多留几条水印结论、每条留几个原文样本。
#: 至少 2 条样本（team-lead 的验收要求），这里取 3 条留余量；
#: 但报告要进 JSON 列、还要经 API 下发，故设上限防止膨胀。
MAX_MARK_ROWS = 8
MAX_SAMPLES_PER_MARK = 3

#: 报告里最多留几条**被拒候选**（含拒绝原因）。数量本就不大，设上限只为防极端。
MAX_REJECTED_ROWS = 20


def detect_marks(text: str, **kwargs) -> list[dict]:
    """检出可疑水印串（`corpus_health.scan_watermarks` 的薄封装，只取 flagged）。

    单独留一个入口，是为了让入库链路（`store.ingest_text`）既能拿到「检出结论」
    写进报告，又能拿同一批结论去清洗 `chapter_meta` 里各章 body，保证
    「章元数据路径」与「文本解析路径」清洗口径一致。
    """
    return scan_watermarks(text, **kwargs)["flagged"]


def build_clean_report(
    raw_text: str,
    cleaned_text: str,
    marks: list[dict],
    *,
    rejected: list[dict] | None = None,
    thresholds: dict | None = None,
) -> dict:
    """把「检出结论 + 被拒候选 + 清洗前后字数」组装成可持久化、可复核的清洗报告。

    Args:
        raw_text: 清洗前原文。
        cleaned_text: 清洗后文本。
        marks: `scan_watermarks(...)["flagged"]`（**清洗前**检出）。
        rejected: `scan_watermarks(...)["rejected"]` —— 考虑过但被排除的候选
            （含拒绝原因）。这是「检测器没有静默丢弃」的凭证。
        thresholds: `scan_watermarks(...)["thresholds"]` —— 本次判定用的阈值快照。

    Returns:
        可 JSON 序列化的报告 dict，字段稳定，确定性。
    """
    rows: list[dict] = []
    for m in marks[:MAX_MARK_ROWS]:
        samples = list(m.get("samples") or [])[:MAX_SAMPLES_PER_MARK]
        rows.append({
            "text": m["text"],
            "count": m["count"],
            # 「紧跟刻度标点（『。』）」的次数 = 真正会被删掉的次数；
            # 没有它就无法复核删字这笔账。
            "final": m["final"],
            "final_ratio": m["final_ratio"],
            "pooled_ratio": m.get("pooled_ratio"),
            "final_char": m.get("final_char"),
            "nesting_breadth": m.get("nesting_breadth"),
            "para_end_ratio": m.get("para_end_ratio"),
            "nested_variants": list(m.get("nested_variants") or []),
            "samples": samples,
        })

    rejected_rows: list[dict] = []
    for r in (rejected or [])[:MAX_REJECTED_ROWS]:
        rejected_rows.append({
            "text": r["text"],
            "count": r["count"],
            "final_ratio": r.get("final_ratio"),
            "pooled_ratio": r.get("pooled_ratio"),
            "final_char": r.get("final_char"),
            "nesting_breadth": r.get("nesting_breadth"),
            "reason": r.get("reason"),
            "samples": list(r.get("samples") or [])[:2],
        })

    chars_in = len(raw_text)
    chars_out = len(cleaned_text)
    removed = chars_in - chars_out
    return {
        "version": CLEAN_REPORT_VERSION,
        "cleaned": bool(marks),
        "chars_in": chars_in,
        "chars_out": chars_out,
        "removed_chars": removed,
        "removed_ratio": round(removed / chars_in, 6) if chars_in else 0.0,
        "detected_count": len(marks),
        "marks": rows,
        # 被拒候选（考虑过但因判据不达标而排除）+ 原因：让人看得出「为什么没删」。
        "rejected_count": len(rejected or []),
        "rejected_candidates": rejected_rows,
        # 本次判定用的阈值快照（可复现性）。
        "thresholds": thresholds or {},
    }


def clean_novel_text(text: str, **kwargs) -> tuple[str, dict]:
    """检出并剔除水印，返回 ``(清洗后文本, 清洗报告)``。

    未检出任何水印时**原样返回**文本（不做任何改动），报告里 ``cleaned=False``、
    ``removed_chars=0``，但 ``rejected_candidates`` 仍会记录被排除的候选 ——
    这样「干净语料」与「有候选但都没达标」在报告里可区分。
    """
    scan = scan_watermarks(text, **kwargs)
    hits = scan["flagged"]
    cleaned = strip_watermarks(text, hits)
    return cleaned, build_clean_report(
        text, cleaned, hits,
        rejected=scan["rejected"], thresholds=scan["thresholds"],
    )
