"""叙事切分：段落 + 句子边界感知。

## 为什么不能按字符窗口切

旧实现是纯字符窗口（`text[start:end]`），实测（雪中 5191 个 chunk）：

    尾部不是句末标点（半句话结尾）   4411 / 5191 = 85.0%
    引号不配平（对白被切断）         1844 / 5191 = 35.5%
    首部以标点开头（半句话开始）      358 / 5191 =  6.9%

这个破坏率对蒸馏是致命的，因为它直接污染三类产物：

1. **场景模板**：chunk 从场景中间开始 → 模型看不到「谁在哪里做什么」，
   只能抽出一堆「某某站在…」，或者干脆复述被切断的片段。
2. **角色声音**：对白被切成两半 → 抽到的是「说到一半的话」，
   模型学到的口头禅是残片。
3. **语序编排**：句子被切断 → 抽不出「先写什么后写什么」，
   因为边界本身被破坏了。

反过来说，85% 的破损率意味着**每条规则都建立在不完整的信息上**。

## 切分策略（三级回退）

    1. 段落边界（`\\n\\n`）—— 最理想，保留完整场景
    2. 句子边界（`。！？…」` 之后）—— 段落内生硬处切开
    3. 字符边界 —— 兜底（单句超长时，如无标点的散文诗）

## 重叠的处理

旧实现的 overlap 是「字符数」，重叠部分可能从半句开始。
新实现让重叠对齐到句子边界：先找到目标起点的前一个句子边界，
从那里开始重叠。这样重叠给的是**完整的上下文句子**，而不是残片。

## 一个容易忽略的点

切分后要保证 **每个 chunk 的引号配平**。如果一段对白跨了 chunk 边界，
要么把对白整体推到下一个 chunk，要么把它留在上一个——不能从中间劈开。
"""
from __future__ import annotations

import re

#: 段落分隔（连续空行）。中文小说里段落之间常是 `\n\n` 或 `\n    ` 缩进。
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")

#: 句子边界：句末标点后紧跟可选的后引号/括号。
#: 注意 `」』` 要跟在前一个标点后面才算结束（`。」` 是一个句子结尾）。
_SENT_END_RE = re.compile(r"[。！？…]+[」』”’）)]*")

#: 引号对，用于配平检查。
#:
#: 单引号 `‘’` 必须包含：实测雪中用 `一把‘绣冬’` 标刀名，漏掉它会让
#: 配平检查误判（把已配平的 chunk 判成不配平，`_units` 因此不肯断句，
#: 整段被塞进一个 chunk 导致尾部停在半句）。
_QUOTE_PAIRS = (
    ("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"),
    ("（", "）"), ("(", ")"), ("《", "》"),
)


def _is_balanced(text: str) -> bool:
    """chunk 的引号是否配平。不配平说明对白被切断了。"""
    return all(text.count(a) == text.count(b) for a, b in _QUOTE_PAIRS)


def _split_sentences(para: str) -> list[str]:
    """把段落拆成句子列表（保留句末标点）。"""
    out: list[str] = []
    start = 0
    for m in _SENT_END_RE.finditer(para):
        end = m.end()
        seg = para[start:end]
        if seg:
            out.append(seg)
        start = end
    if start < len(para):
        tail = para[start:]
        if tail.strip():
            out.append(tail)
    return out


def _units(para: str) -> list[str]:
    """段内切成「句子组」：保持引号配平的相邻句子会合并。

    为什么不直接按句子切：一段里常有

        他说：「你今天怎么不去？」
        我摇摇头。

    按句子切会把「他说：」和「「你今天怎么不去？」」分开（前者没引号、
    后者引号不配平）。所以这里把**引号不配平的句子与下一句合并**，
    直到配平为止。
    """
    sents = _split_sentences(para)
    out: list[str] = []
    buf = ""
    for s in sents:
        buf += s
        if _is_balanced(buf):
            out.append(buf)
            buf = ""
    if buf:
        # 段末仍不配平：原文本身有问题（漏了后引号）。整段尾巴一起给，
        # 不丢弃——丢掉会让正文缺字。
        out.append(buf)
    return out


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """按段落/句子边界切分。签名与旧实现一致，行为兼容。

    Args:
        text: 全文。
        chunk_size: 目标 chunk 大小（字符数）。**软上限**——为了不切断句子，
            实际 chunk 可能比它略大（最多超出最后一个完整句子的长度）。
        overlap: 重叠字符数（软目标）。实际重叠会对齐到句子边界，
            所以可能比它略多。

    Returns:
        chunk 列表。每个 chunk 尽量以句子边界开始和结束。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # 1) 先拆段落，记录每段在全文里的位置
    paras: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA_SPLIT_RE.finditer(text):
        if m.start() > pos:
            paras.append((pos, m.start()))
        pos = m.end()
    if pos < len(text):
        paras.append((pos, len(text)))
    if not paras:
        paras = [(0, len(text))]

    # 2) 段落超长时切成句子组
    pieces: list[tuple[int, int]] = []
    for s, e in paras:
        para = text[s:e]
        if e - s <= chunk_size:
            pieces.append((s, e))
            continue
        off = s
        for u in _units(para):
            pieces.append((off, off + len(u)))
            off += len(u)

    # 2.5) 单个 piece 仍超长（整段没有一个可断的句末标点，如长段无标点散文）
    #      必须硬切兜底，否则它会被整体塞进一个 chunk，尾部停在任意字符上。
    expanded: list[tuple[int, int]] = []
    for s0, e0 in pieces:
        if e0 - s0 <= chunk_size:
            expanded.append((s0, e0))
            continue
        off = s0
        while off < e0:
            end = min(off + chunk_size, e0)
            expanded.append((off, end))
            off = end
    pieces = expanded

    # 3) 贪心装箱：尽量把完整 piece 塞进 chunk_size
    chunks: list[str] = []
    cur_start = pieces[0][0]
    cur_end = pieces[0][1]
    for s, e in pieces[1:]:
        if e - cur_start <= chunk_size:
            cur_end = e
            continue
        chunks.append(text[cur_start:cur_end].strip())
        # 计算重叠起点：回退 overlap 个字符，再对齐到 piece 边界
        target = max(cur_end - overlap, cur_start)
        new_start = s
        for ps, _pe in pieces:
            if ps >= target:
                new_start = ps
                break
        cur_start = min(new_start, s)
        cur_end = e
    chunks.append(text[cur_start:cur_end].strip())

    return [c for c in chunks if c]


def chunk_quality(chunks: list[str]) -> dict:
    """切分质量自检：给调用方 / 测试用的可测指标。

    三个指标都应该是 0：
      tail_broken  结尾不是句末标点（半句话结尾）
      head_broken  开头是标点（半句话开始）
      unbalanced   引号不配平（对白被切断）
    """
    tail = head = unbal = 0
    for c in chunks:
        if not c:
            continue
        if c[-1] not in "。！？…」』”’）)":
            tail += 1
        if c[0] in "，。、；：！？）」』":
            head += 1
        if not _is_balanced(c):
            unbal += 1
    n = max(len(chunks), 1)
    return {
        "n": len(chunks),
        "tail_broken": tail,
        "tail_pct": round(tail / n * 100, 1),
        "head_broken": head,
        "head_pct": round(head / n * 100, 1),
        "unbalanced": unbal,
        "unbalanced_pct": round(unbal / n * 100, 1),
    }
