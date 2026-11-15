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

import re
from collections import Counter

from app.corpus_health import scan_watermarks, strip_watermarks

#: 清洗报告的**形状**版本号。任何会改变报告字段的改动都要 +1，
#: 便于事后区分「旧报告」与「新报告」（老报告缺字段时按缺失处理）。
#: v2：新增 rejected_candidates / thresholds（记录「考虑过但排除」的候选）。
#: v3：新增 duplicate_paragraphs（整段重复/近乎双份）与 cross_novel（跨书污染）。
CLEAN_REPORT_VERSION = 3

#: 报告里最多留几条水印结论、每条留几个原文样本。
#: 至少 2 条样本（team-lead 的验收要求），这里取 3 条留余量；
#: 但报告要进 JSON 列、还要经 API 下发，故设上限防止膨胀。
MAX_MARK_ROWS = 8
MAX_SAMPLES_PER_MARK = 3

#: 报告里最多留几条**被拒候选**（含拒绝原因）。数量本就不大，设上限只为防极端。
MAX_REJECTED_ROWS = 20

#: —— 段落级清洗的判据 ——
#: 整段重复：归一化（去空白）后 ≥30 汉字才参与查重。短句（对白、拟声词）天然会重复，
#: 按 30 字下限避开；长段落整段重复基本都是「双份粘贴」。
DUP_MIN_CJK = 30
#: 整段重复字数占总字数 ≥35% → 判为「近乎整本双份」，报告里单独标形态。
NEAR_FULL_DOUBLE_RATIO = 0.35
#: 跨书污染：同一段落在全书重复 ≥3 次才进入判定；太低的重复更可能是正常复述。
CROSS_MIN_CJK = 40
CROSS_MIN_REPEAT = 3
#: 判定为跨书污染需要至少 2 个「只在该重复组里出现、全书别处一次都没有」的 4 字专名。
CROSS_MIN_TOKENS = 2
#: 但只有「≥2 个」远不够：本书自己的内容也会被整段重复（作者复述、设定说明），
#: 这些段落里同样会有一两个词只在重复组里出现过。实测《我不是戏神》的
#: 「剧目的综合观众期待值」段重复 12 次，40 个候选词里 20 个组外 0 次 —— 只看个数
#: 必误删。真异书段的特征是**整段词汇都不属于本书**：《十日终焉》里混入的
#: 「名师大陆 / 灵魂契约」段，候选词组外 0 次的比例 0.8+；《我在精神病院学斩神》
#: 里混入的斗罗大陆段（唐三 / 美公子）也是 0.68~0.97。
#: 故要求「组外 0 次」的词占该组候选词的 ≥60%，且候选词本身至少 6 个才有统计意义。
CROSS_MIN_TOKEN_RATIO = 0.6
CROSS_MIN_SAMPLED = 6
#: 性能上限：最多判定 60 组、每组取 40 个候选专名。
#: 全局词表按「组数 × 每组上限」给足，避免后面的组因词表被截断而无法判定
#: （`_count_tokens` 是单遍滑窗，词表大小只影响字典查表，不影响扫描复杂度）。
MAX_CROSS_GROUPS = 60
MAX_TOKENS_PER_GROUP = 40
MAX_CROSS_TOKENS = MAX_CROSS_GROUPS * MAX_TOKENS_PER_GROUP


def _cjk_len(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s))


def _norm_para(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _split_blocks(text: str) -> list[str]:
    """按空行切段；若全文几乎没有空行（单换行分段），退化按行切。"""
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(blocks) <= 3 and len(lines) > 50:
        return lines
    return blocks


def _join_blocks(blocks: list[str], text: str) -> str:
    """按原文分段风格重组，并保留首尾空白（不吞文件末尾的换行）。"""
    sep = "\n\n" if "\n\n" in text else "\n"
    joined = sep.join(blocks)
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    if lead and not joined.startswith(lead):
        joined = lead + joined
    if trail and not joined.endswith(trail):
        joined = joined + trail
    return joined


def _count_tokens(text: str, inside: dict[str, int]):
    """一次扫描统计若干 4 字词在全文的出现次数，产出 ``(词, 全文次数, 组内次数)``。

    用单遍滑窗 + 集合查表，而不是对每个词各跑一次 `str.count`：后者是
    O(词数 × 全文)，百万字级语料 × 几百个候选词会拖到几十秒。
    """
    found = dict.fromkeys(inside, 0)
    for i in range(len(text) - 3):
        w = text[i:i + 4]
        if w in found:
            found[w] += 1
    return [(t, found[t], inside[t]) for t in inside]


def strip_cross_novel_paragraphs(text: str) -> tuple[str, dict]:
    """剔除「同一段落反复出现且带异书专名」的跨书污染段（全部副本一起删）。

    判据（不依赖任何书名硬编码）：
    1. 段落归一化后 ≥40 汉字，且在全书中**完全重复** ≥3 次；
    2. 从该段落里取 4 字窗口，作为「专名候选」；
    3. 某候选在全书出现次数 == 它在这些重复组里的次数（即**组外 0 次**），
       说明这个词只有这段重复内容用过 —— 本书正文从未出现过；
    4. 这样的词占该组候选词的 ≥60%（且候选词 ≥6 个）→ 判定为异书内容。
       只要求「≥2 个」会把本书自创设定误删（见 `CROSS_MIN_TOKEN_RATIO` 注释）。

    为什么会命中「名师大陆 / 灵魂契约」这类：它们只在混入段里出现，本书其余
    200 多万字一次没有；而本书自己的专名（角色名、地名）在正文里高频出现，
    组外次数远大于 0，不会被判成污染。
    """
    blocks = _split_blocks(text)
    keys = [_norm_para(b) for b in blocks]
    counts = Counter(k for k in keys if _cjk_len(k) >= CROSS_MIN_CJK)
    groups = {k: v for k, v in counts.items() if v >= CROSS_MIN_REPEAT}
    if not groups:
        return text, {"enabled": False, "detected_groups": 0,
                      "removed_paragraphs": 0, "removed_chars": 0, "samples": []}

    # 安全阀：整本近乎双份（重复字占比 ≥35%，且**过半段落**都有多份副本）时，
    # 每个段落的 4 字窗口都只出现在它自己的几份副本里（组外 0 次），会被误判成
    # 「异书内容」而全删。这种形态交给 `strip_duplicate_paragraphs`（保留首次）处理。
    # 两个条件都要满足，且统计口径用**全部段落**（不能用 ≥40 字的子集：短样本里
    # 几段异书内容就可能占满子集，把「整本双份」误判出来）。
    all_counts = Counter(k for k in keys if k)
    total_cjk = sum(_cjk_len(k) for k in keys if k)  # 含重复副本
    dup_cjk = sum((v - 1) * _cjk_len(k) for k, v in all_counts.items() if v > 1)
    repeat_share = len(groups) / len(all_counts) if all_counts else 0.0
    if (
        total_cjk
        and repeat_share > 0.5
        and dup_cjk / total_cjk >= NEAR_FULL_DOUBLE_RATIO
    ):
        return text, {"enabled": False, "skipped": "near_full_duplicate",
                      "duplicate_ratio": round(dup_cjk / total_cjk, 6),
                      "repeat_share": round(repeat_share, 3),
                      "detected_groups": 0, "removed_paragraphs": 0,
                      "removed_chars": 0, "samples": []}

    # 只取重复次数最高的若干组做判定（防极端文件拖垮入库）
    picked = sorted(groups.items(), key=lambda kv: -kv[1])[:MAX_CROSS_GROUPS]
    token_inside: dict[str, int] = {}
    token_groups: dict[str, set[str]] = {}
    for key, cnt in picked:
        toks = set()
        for i in range(len(key) - 3):
            w = key[i:i + 4]
            if key.count(w) == 1 and re.fullmatch(r"[\u4e00-\u9fff]{4}", w):
                toks.add(w)
                if len(toks) >= MAX_TOKENS_PER_GROUP:
                    break
        for t in toks:
            if t not in token_inside and len(token_inside) >= MAX_CROSS_TOKENS:
                continue
            token_inside[t] = token_inside.get(t, 0) + cnt
            token_groups.setdefault(t, set()).add(key)

    # 全文只扫一遍：每个候选词算「组外出现次数」，0 次才是异书专名。
    outside = {
        t: n - inside
        for t, n, inside in _count_tokens(text, token_inside)
    }

    cross_keys: set[str] = set()
    evidence: list[dict] = []
    for key, cnt in picked:
        sampled = [t for t, gs in token_groups.items() if key in gs]
        if len(sampled) < CROSS_MIN_SAMPLED:
            continue
        exclusive = [t for t in sampled if outside[t] <= 0]
        if len(exclusive) < CROSS_MIN_TOKENS:
            continue
        # 关键：不只看「有几个组外 0 次的词」，还看占比 —— 本书内容整段重复时
        # 多数候选词在别处照样出现，占比会明显偏低。
        if len(exclusive) < len(sampled) * CROSS_MIN_TOKEN_RATIO:
            continue
        cross_keys.add(key)
        evidence.append({
            "count": cnt,
            "chars": _cjk_len(key),
            "exclusive_ratio": round(len(exclusive) / len(sampled), 3),
            "tokens": sorted(exclusive, key=len, reverse=True)[:3],
            "text": key[:120],
        })

    if not cross_keys:
        return text, {"enabled": True, "detected_groups": 0,
                      "removed_paragraphs": 0, "removed_chars": 0, "samples": []}

    kept: list[str] = []
    removed_paras = 0
    removed_chars = 0
    for b, k in zip(blocks, keys):
        if k in cross_keys:
            removed_paras += 1
            removed_chars += _cjk_len(k)
        else:
            kept.append(b)
    report = {
        "enabled": True,
        "detected_groups": len(cross_keys),
        "removed_paragraphs": removed_paras,
        "removed_chars": removed_chars,
        "samples": evidence[:3],
    }
    return _join_blocks(kept, text), report


def strip_duplicate_paragraphs(text: str) -> tuple[str, dict]:
    """删掉「整段重复」的副本，**只保留首次出现**。

    覆盖两种形态：
    - `partial`：零散段落被复制粘贴（保留第一次）；
    - `near_full_double`：重复字数 ≥ 全文 35%，即整本近乎双份（《记忆曝光》实测 49.5%）。
    """
    blocks = _split_blocks(text)
    seen: set[str] = set()
    kept: list[str] = []
    examples: dict[str, str] = {}
    counts: Counter = Counter()
    removed_paras = 0
    removed_chars = 0
    total_chars = 0

    for b in blocks:
        key = _norm_para(b)
        n = _cjk_len(key)
        total_chars += n
        if n < DUP_MIN_CJK:
            kept.append(b)
            continue
        if key in seen:
            removed_paras += 1
            removed_chars += n
            counts[key] += 1
            continue
        seen.add(key)
        examples[key] = b.strip()
        kept.append(b)

    ratio = round(removed_chars / total_chars, 6) if total_chars else 0.0
    if not removed_paras:
        form = "none"
    elif ratio >= NEAR_FULL_DOUBLE_RATIO:
        form = "near_full_double"
    else:
        form = "partial"
    samples = [
        {"count": c + 1, "text": examples.get(k, "")[:120]}
        for k, c in counts.most_common(3)
    ]
    report = {
        "enabled": True,
        "form": form,
        "removed_paragraphs": removed_paras,
        "removed_chars": removed_chars,
        "duplicate_ratio": ratio,
        "samples": samples,
    }
    return _join_blocks(kept, text), report


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
    duplicate_report: dict | None = None,
    cross_novel_report: dict | None = None,
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
    dup = duplicate_report or {"enabled": False, "removed_paragraphs": 0, "removed_chars": 0}
    cross = cross_novel_report or {"enabled": False, "removed_paragraphs": 0, "removed_chars": 0}
    return {
        "version": CLEAN_REPORT_VERSION,
        "cleaned": bool(marks or dup.get("removed_paragraphs") or cross.get("removed_paragraphs")),
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
        # 段落级清洗：整段重复 / 跨书污染（各自带删除量 + 3 条原文样本）。
        "duplicate_paragraphs": dup,
        "cross_novel": cross,
    }


def clean_novel_text(text: str, **kwargs) -> tuple[str, dict]:
    """完整清洗：水印 → 跨书污染 → 整段重复，返回 ``(清洗后文本, 报告)``。

    顺序有讲究：跨书污染要**先**判（它依赖「同一段落重复 N 次」这个信号，
    去重之后重复就没了）；整段重复再去掉剩余的副本、只留首次出现。

    未检出任何问题时**原样返回**文本，报告里 ``cleaned=False``、``removed_chars=0``，
    但 ``rejected_candidates`` 仍记录被排除的候选 —— 干净语料与「有候选但都没达标」
    在报告里可区分。
    """
    scan = scan_watermarks(text, **kwargs)
    hits = scan["flagged"]
    cleaned = strip_watermarks(text, hits)
    cleaned, cross_rep = strip_cross_novel_paragraphs(cleaned)
    cleaned, dup_rep = strip_duplicate_paragraphs(cleaned)
    return cleaned, build_clean_report(
        text, cleaned, hits,
        rejected=scan["rejected"], thresholds=scan["thresholds"],
        duplicate_report=dup_rep, cross_novel_report=cross_rep,
    )
