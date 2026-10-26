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
from app.corpus_health import scan_watermarks, strip_watermarks

#: 清洗报告的**形状**版本号。任何会改变报告字段的改动都要 +1，
#: 便于事后区分「旧报告」与「新报告」（老报告缺字段时按缺失处理）。
#: v2：新增 rejected_candidates / thresholds（记录「考虑过但排除」的候选）。
#: v3：cleaned 按所有清洗阶段的实际文本变化判断。
CLEAN_REPORT_VERSION = 3
#: 消费清洗的**行为**版本号。它参与归档语料的 content_hash 计算
#: （`distill-corpus-binding-2:consumption-<版本>`）——清洗行为变了，
#: 绑定就必须变，否则旧绑定会指向一份「其实没被这样清洗过」的语料。
#: v5：新增站点块的**结构判据**（闭合括号块 / 域名 token / 以站点短语开头的整行）。
CONSUMPTION_CLEAN_VERSION = 5
# Bounded templates: never consume an arbitrary line or infer a book-title boundary.
#: 站点块的**标记词**：只放盗版站固定短语，不放任何普通中文词。
#: 「收藏」「红票」「纵横」这类在正文里常见的词**不在这里** —— 它们只进
#: `_SUSPECT_RESIDUE`（只标记不删），否则会把正常句子当垃圾删掉。
#: 「天才一秒记住本站地址」是盗版站的固定标语，作为一个整体进标记表；
#: 单独的「天才」是常用词，不能进（否则「天才知道…」那类正文行会被整行删掉）。
_SITE_BLOCK_MARKERS = (r'天才[一壹]秒[记記]住|一秒记住|壹秒記住|最快更新|更新快|小说网站|'
                        r'无弹窗|精彩小说|手机用户可访问|手机请访问|看本书最新章节')
_SITE_RESIDUE_PATTERNS = (
    re.compile(r"<strong>小说txt下载</strong>|<长-风>文学", re.I),
    # Exact closed templates only; malformed/bare domains remain review evidence.
    re.compile(r"\.info\[\]|\(\.\.info\)|\[\.\.info超多好看小说\]|（\.\.info无弹窗广告）", re.I),
    re.compile(r"一秒记住[ \t]*[【\[][^\x0d\n】\]]{0,100}\.info[】\]](?:[，,]?[ \t]*为您提供精彩小说阅读[。.]?)?", re.I),
    re.compile(r"无限大冒险全文阅读|封禁之神小说5200"),
    re.compile(r"(?m)^[ \t]*为您提供精彩小说阅读[。.]?[ \t]*(?=\x0d?$)"),
    re.compile(r"更新时间[ \t]*[:：][ \t]*\d{4}-\d{1,2}-\d{1,2}(?:[ \t]+\d{1,2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"[【\[][^\x0d\n【】\[\]]{1,40}(?:全文阅读|小说5200)[】\]]"),
    # ---- v5：站点块的**结构判据** ----
    # 盗版站把广告插进正文，载体是「域名 token」/「括号块」/「整行」。
    # 这三种载体都**自带边界**，所以只删边界以内的那一段，块外的字一律不动。
    # 统计判据（scan_watermarks）只认「紧跟句号的高频汉字串」，对这三种载体
    # 天生不适用（实测本书 296 个残留窗口，统计口径 0 命中）。
    #
    # (1) 域名尾巴 token：wap..info / .info / <strong>..info</strong>
    #     **只在边界上删**（紧跟句末标点、行首，或被 <strong> 包着）。
    #     嵌在词里的裸域名（`甲.info乙`）**保留为复核证据** ——
    #     这条策略是既有的契约（tests/test_distill_residual_policy.py），
    #     「宁可漏报，不可误删」。站点真正的注入点都在句读边界上，
    #     所以按边界收口既守住契约，又不漏掉实测的 259 个注入点。
    re.compile(r"(?:</?strong>)[ \t]*(?:[A-Za-z0-9]{0,8}\.{1,2}info)(?:[ \t]*</?strong>)", re.I),
    re.compile(r"(?<=[\u3002\uff01\uff1f\x0d\n])(?:</?strong>){0,2}[ \t]*"
               r"(?:[A-Za-z0-9]{0,8}\.{1,2}info)(?:[ \t]*</?strong>){0,2}", re.I),
    # (2) 括号块：块内出现站点标记 → 整块删（块的边界就是括号）
    #     只认**闭合**的括号块；不闭合的形态（`（..info好看的小说`）保留为复核证据。
    re.compile(r"[\[【（(][^\[\]【】（）()\x0d\n]{0,200}?(?:" + _SITE_BLOCK_MARKERS + r"|\.{1,2}info)"
                r"[^\[\]【】（）()\x0d\n]{0,200}?[\]】）)]", re.I),
    # (3) 以站点短语**开头**的整行：行的边界就是行
    #     结尾用 (?=\x0d?$) 而不是 $ —— 语料里 \r\n 与 \n 两种行尾都存在。
    re.compile(r"(?m)^[ \t]*(?:" + _SITE_BLOCK_MARKERS + r")[^\x0d\n]{0,120}(?=\x0d?$)", re.I),
)
_SUSPECT_RESIDUE = re.compile(r"一秒记住|为您提供精彩小说阅读|更新时间|全文阅读|小说5200|红票|收藏|\.{1,2}info", re.I)

#: 站点/公告类**行级**标记（高精度）。
#:
#: 为什么要单独一套：`_SITE_RESIDUE_PATTERNS` 删的是**有边界**的块（域名 token、
#: 闭合括号块、整行广告），删完还能拼回原句。但盗版站还有一类是**把 SEO 词插进
#: 句子中间**（实测真书 182 行）：
#:
#:     立即隐相最新章节去别院拿来三根黄瓜
#:     姜泥这才狠狠说道最低调的巨星最新章节：“你说的那笔买卖还作数？”
#:     跟这种草包过日子轻笑忘最新章节，岂不是要被天下人耻笑。
#:
#: 原来的词**已经被替换掉了**，删掉标记词也拼不回原文（硬修只会造出另一句假话），
#: 所以这类只能**整行不取用**，不能修。
#:
#: 精度要求：这些串在小说正文里基本不出现。「收藏」「更新」「免费」这类正文常用词
#: **不在表里** —— 只有带站点语境的完整短语才算。
SITE_LINE_MARKERS = re.compile(
    r"最新章节|首发域名|手机版阅读|阅读网址|微信公众号|一秒记住|最快更新|无弹窗|"
    r"精彩小说|求收藏|求推荐票|求月票|笔趣|txt下载|请记住本书|免费阅读|"
    r"未完待续|本章完|\.{1,2}info"
)

#: 报告里最多留几条水印结论、每条留几个原文样本。
#: 至少 2 条样本（team-lead 的验收要求），这里取 3 条留余量；
#: 但报告要进 JSON 列、还要经 API 下发，故设上限防止膨胀。
MAX_MARK_ROWS = 8
MAX_SAMPLES_PER_MARK = 3

#: 报告里最多留几条**被拒候选**（含拒绝原因）。数量本就不大，设上限只为防极端。
MAX_REJECTED_ROWS = 20


def is_site_marker_line(line: str) -> bool:
    """这一行是不是站点/公告的残留（不是小说正文）。"""
    return bool(SITE_LINE_MARKERS.search(line or ""))


def strip_site_marker_lines(text: str) -> tuple[str, int]:
    """删掉含站点标记的**整行**，返回 (新文本, 删掉的行数)。

    保留其余字符与行序（含换行结构）。没有命中时原样返回。

    ## 为什么是「删行」而不是「修字」

    站点 SEO 词是被**插进句子中间**的，原词已被替换 —— 删掉标记词拼不回原句。
    硬修会造出另一句假话，比留着站点词更坏（模型会把站点词当普通词学）。
    所以这条只用在**取用侧**（风格样例）：被污染的行对「学写法」没有价值，
    不取用即可；**入库语料本身不动**（那是另一回事，见模块文档的留痕约定）。
    """
    raw = text or ""
    lines = raw.split("\n")
    kept = [ln for ln in lines if not is_site_marker_line(ln)]
    dropped = len(lines) - len(kept)
    if not dropped:
        return raw, 0
    return "\n".join(kept), dropped


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
        # All cleaning stages contribute, even when no statistical watermark was detected.
        "cleaned": cleaned_text != raw_text,
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


def clean_consumption_text(text: str) -> tuple[str, dict]:
    """Delete exact spans only; preserve all other characters, including newlines."""
    raw = text or ""
    spans = sorted((m.start(), m.end()) for p in _SITE_RESIDUE_PATTERNS for m in p.finditer(raw))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    parts, cursor = [], 0
    for start, end in merged:
        parts.append(raw[cursor:start])
        cursor = end
    parts.append(raw[cursor:])
    cleaned = "".join(parts)
    unresolved = [{"start": m.start(), "end": m.end(), "marker": m.group(),
                   "excerpt": cleaned[max(0, m.start()-40):m.end()+60],
                   "reason": "no_safe_template_boundary"}
                  for m in _SUSPECT_RESIDUE.finditer(cleaned)]
    return cleaned, {
        "version": CONSUMPTION_CLEAN_VERSION, "cleaned": cleaned != raw,
        "chars_in": len(raw), "chars_out": len(cleaned),
        "removed_chars": len(raw)-len(cleaned), "removed_count": len(merged),
        "removed_spans": [{"start": a, "end": b, "text": raw[a:b]} for a, b in merged],
        "unresolved": unresolved,
    }
