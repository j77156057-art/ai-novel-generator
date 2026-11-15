"""原文锚点定位（R19）：把一条规则的 `chunk_id` / `evidence_chapter_no` / 证据句
还原成**章内字符区间**，并按需带上下文。

## 为什么单独一层

蒸馏块是 5 章一块，规则只带「块」和「证据实际所在章」两个坐标。检索工具要给
作者 agent 回原文，就必须自己把证据句落到某一章的字符位置上。落不上时**不能
返回空**——空与「没查」在轨迹里分不开（见 `trace.py` 的写入纪律），调用方也
无法区分「这条规则没证据」和「定位器有 bug」。所以失败一律显式标
`anchor_missing=True` 并把 `method` 置为 `anchor_missing`，同时降级返回块级正文，
由调用方决定是否渲染。

## 三种定位口径（与设计稿 §4.1 一致）

1. `exact`：证据 ≥ `ANCHOR_EXACT_MIN_CHARS` 且能在章内逐字命中；
2. `fuzzy`：证据 < 15 字，或整句命中失败时，用首尾各 3 字两端锚定；
3. `anchor_missing`：两端锚定也失败。此时 `char_start/char_end` 为 None，
   `context` 退回所在分片的正文前 `_DEGRADE_CHARS` 字，**不静默返回空**。

匹配前做一次轻量归一化（去空白），并保留归一化位置到原始位置的映射，
所以返回的 `char_start/char_end` 永远指向**原始章文本**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.models import NovelChunk
from app.retrieval.catalog_config import ANCHOR_EXACT_MIN_CHARS

#: 两端锚定的首尾长度。3 字是设计稿定的口径，不随书改。
_FUZZY_EDGE_CHARS = 3

#: 定位失败时降级返回的分片正文字数。够人眼确认「这段确实没有该证据」。
_DEGRADE_CHARS = 400


@dataclass(frozen=True)
class Anchor:
    """一条证据的章内落点。"""

    chunk_id: int | None
    chapter_no: int | None
    evidence_chapter_no: int | None
    char_start: int | None
    char_end: int | None
    excerpt: str
    method: str
    anchor_missing: bool
    context: str = ""
    #: 降级说明。`anchor_missing=False` 时为空串。
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "chapter_no": self.chapter_no,
            "evidence_chapter_no": self.evidence_chapter_no,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "excerpt": self.excerpt,
            "method": self.method,
            "anchor_missing": self.anchor_missing,
            "context": self.context,
            "reason": self.reason,
        }


def _normalize(text: str) -> tuple[str, list[int]]:
    """去空白，并返回「归一化下标 → 原文下标」映射。"""
    chars: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def _find_span(haystack: str, needle: str) -> tuple[int, int] | None:
    """在原文里找 needle；先原样找，找不到再去空白后找。失败返回 None。"""
    if not needle:
        return None
    pos = haystack.find(needle)
    if pos >= 0:
        return pos, pos + len(needle)
    norm_hay, idx = _normalize(haystack)
    norm_needle, _ = _normalize(needle)
    if not norm_needle:
        return None
    pos = norm_hay.find(norm_needle)
    if pos < 0:
        return None
    start = idx[pos]
    end = idx[pos + len(norm_needle) - 1] + 1
    return start, end


def _chapter_text(db, novel_id: int, chapter_no: int) -> str:
    """把一章的所有分片按 (idx, id) 拼回整章正文。"""
    rows = db.scalars(
        select(NovelChunk.text)
        .where(
            NovelChunk.novel_id == novel_id,
            NovelChunk.chapter_no == chapter_no,
        )
        .order_by(NovelChunk.idx, NovelChunk.id)
    ).all()
    return "".join(rows or [])


def _fuzzy_edges(evidence: str) -> tuple[str, str] | None:
    """首尾各取 3 字。证据本身只有 3–5 字时首尾会重叠，仍按整句锚。"""
    body = "".join(evidence.split())
    if len(body) < _FUZZY_EDGE_CHARS:
        return None
    return body[:_FUZZY_EDGE_CHARS], body[-_FUZZY_EDGE_CHARS:]


def locate_anchor(
    db,
    *,
    evidence: str,
    chunk_id: int | None = None,
    evidence_chapter_no: int | None = None,
    novel_id: int | None = None,
    chapter_no: int | None = None,
    span: int = 300,
) -> Anchor:
    """定位一条证据句，返回章内字符区间与上下文。

    Args:
        evidence: 规则里的证据句（`rule` 里「证据：」之后的部分）。
        chunk_id: 蒸馏块 id（`novel_chunks.id`）。用来取 `novel_id` / 兜底章号。
        evidence_chapter_no: 证据实际所在章，优先于块的起始章。
        novel_id / chapter_no: `chunk_id` 缺失时的兜底坐标。
        span: 上下文左右各取多少字；0 表示不要上下文。

    Returns:
        `Anchor`。定位失败时 `anchor_missing=True`、`method="anchor_missing"`，
        `context` 退回分片正文，**不会返回空对象**。
    """
    chunk = None
    if chunk_id is not None:
        chunk = db.get(NovelChunk, int(chunk_id))
    if novel_id is None and chunk is not None:
        novel_id = chunk.novel_id
    target_chapter = evidence_chapter_no or (
        chunk.chapter_no if chunk is not None else None
    ) or chapter_no

    base = Anchor(
        chunk_id=int(chunk.id) if chunk is not None else chunk_id,
        chapter_no=int(chunk.chapter_no) if chunk is not None and chunk.chapter_no else chapter_no,
        evidence_chapter_no=target_chapter,
        char_start=None,
        char_end=None,
        excerpt="",
        method="anchor_missing",
        anchor_missing=True,
    )

    if not evidence or not evidence.strip():
        return _degrade(base, chunk, "证据为空串，无法定位")
    if target_chapter is None or novel_id is None:
        return _degrade(base, chunk, "缺少证据章号或 novel_id，无法定位")

    text = _chapter_text(db, int(novel_id), int(target_chapter))
    if not text:
        return _degrade(base, chunk, f"第 {target_chapter} 章正文缺失，无法定位")
    # 1) exact：整句逐字命中（含去空白后的归一化命中）。
    if len(evidence.strip()) >= ANCHOR_EXACT_MIN_CHARS:
        hit = _find_span(text, evidence)
        if hit:
            return _with_context(
                base, text, hit, span,
                method="exact", excerpt=evidence.strip(),
            )

    # 2) fuzzy：两端各 3 字锚定。
    edges = _fuzzy_edges(evidence)
    if edges:
        head, tail = edges
        head_hit = _find_span(text, head)
        if head_hit:
            tail_hit = _find_span(text, tail)
            start = head_hit[0]
            if tail_hit and tail_hit[1] > start:
                end = tail_hit[1]
            else:
                end = min(len(text), start + len("".join(evidence.split())))
            return _with_context(
                base, text, (start, end), span,
                method="fuzzy", excerpt=text[start:end],
            )

    # 3) 两端也锚不上：显式缺失 + 降级到分片/章正文。
    return _degrade(base, chunk, "整句与首尾两端均未命中", text)


def locate_rule_anchor(db, item: dict, *, span: int = 300) -> Anchor:
    """规则字典（`rules[*]` 的一条）→ `Anchor`。取 `chunk_id` / `evidence_chapter_no`
    / `rule` 里的「证据：」段；任一项缺失都走 `locate_anchor` 的显式降级。"""
    rule = str(item.get("rule") or "")
    evidence = item.get("evidence") or ""
    if not evidence and "证据：" in rule:
        evidence = rule.split("证据：", 1)[1].strip().rstrip("）")
    return locate_anchor(
        db,
        evidence=evidence,
        chunk_id=item.get("chunk_id"),
        evidence_chapter_no=item.get("evidence_chapter_no"),
        novel_id=item.get("novel_id"),
    )


def _with_context(base: Anchor, text: str, hit: tuple[int, int], span: int,
                  *, method: str, excerpt: str) -> Anchor:
    start, end = hit
    if span > 0:
        ctx_start = max(0, start - span)
        ctx_end = min(len(text), end + span)
        context = text[ctx_start:ctx_end]
    else:
        context = text[start:end]
    return Anchor(
        chunk_id=base.chunk_id,
        chapter_no=base.chapter_no,
        evidence_chapter_no=base.evidence_chapter_no,
        char_start=start,
        char_end=end,
        excerpt=excerpt,
        method=method,
        anchor_missing=False,
        context=context,
    )


def _degrade(base: Anchor, chunk, reason: str, text: str = "") -> Anchor:
    """定位失败的唯一出口：标 `anchor_missing`，带分片正文兜底。

    兜底顺序：分片正文 → 章正文。两者都没有时 `context` 为空，但
    `anchor_missing=True` 与 `reason` 仍在——调用方不会把「没定位到」当成空结果。
    """
    context = ""
    excerpt = ""
    if chunk is not None and chunk.text:
        context = chunk.text[:_DEGRADE_CHARS]
        excerpt = context
    elif text:
        context = text[:_DEGRADE_CHARS]
        excerpt = context
    return Anchor(
        chunk_id=base.chunk_id,
        chapter_no=base.chapter_no,
        evidence_chapter_no=base.evidence_chapter_no,
        char_start=None,
        char_end=None,
        excerpt=excerpt,
        method="anchor_missing",
        anchor_missing=True,
        context=context,
        reason=reason,
    )
