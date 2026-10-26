"""语料切分质量审核规则。

## 为什么需要

切分质量决定了所有下游产物的上限。实测（雪中 5191 chunk，旧字符窗口切分）：

    半句结尾 85.0%    引号不配平 35.5%

这个破损率直接污染蒸馏：场景模板从场景中间开始（模型看不到谁在做什么）、
角色声音抽到「说到一半的话」、语序编排抽不出顺序（边界本身被切坏）。

**这是最容易漏掉的一类问题**：语料看起来「有内容」，审核规则的
`empty_or_short_chunks` 不报（文本不空不短）、`duplicate_chunks` 不报
（内容不重复），但每条规则都建立在不完整信息上。

## 判据必须通用

不假设语言/文体。中文小说、英文小说、散文都适用：
  - 半句结尾：末字符不是句末标点集合中的任何一个
  - 半句开头：首字符是标点（逗号/句号/顿号…）
  - 引号不配平：成对符号数量不等

不检测「具体某本书的问题」，只检测「切分是否落在边界上」这个结构性事实。
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.diagnosis import DiagnosticIssue, IssueEvidence, make_issue_id

#: 句末标点（中英）。chunk 结尾命中任一即视为「落在句子边界」。
#: 不包含 `…` 吗？包含——中文里省略号常作句末。
SENTENCE_END_CHARS = "。！？…」』”’）)」』.!?"

#: 首字符若是这些，说明切在半句上。
LEADING_PUNCT_CHARS = "，。、；：！？）」』”’，,.;:!?)"

#: 成对符号（引号/括号/书名号）。不配平说明把一段话切开了。
PAIRED_SYMBOLS: tuple[tuple[str, str], ...] = (
    ("「", "」"), ("『", "』"), ("“", "”"), ("‘", "’"),
    ("（", "）"), ("(", ")"), ("《", "》"),
)

#: 判定为「切分事故」的阈值。超过即报警。
#: 为什么是 10%：段落/句子边界切分实测能到 <5%，字符窗口是 60-85%。
#: 10% 留出余量给语料本身不规整的情况（原文漏引号等）。
BROKEN_RATE_THRESHOLD = 0.10


def _is_balanced(text: str) -> bool:
    return all(text.count(a) == text.count(b) for a, b in PAIRED_SYMBOLS)


def scan_chunks(chunks: list[str]) -> dict[str, Any]:
    """扫描一批 chunk，返回切分质量指标（纯函数，无 DB 依赖）。

    Returns:
        {n, tail_broken, head_broken, unbalanced, 各自 pct, samples}
    """
    n = 0
    tail = head = unbal = 0
    tail_samples: list[str] = []
    head_samples: list[str] = []
    unbal_samples: list[str] = []

    for c in chunks:
        if not c or not c.strip():
            continue
        n += 1
        t = c.strip()
        if t[-1] not in SENTENCE_END_CHARS:
            tail += 1
            if len(tail_samples) < 3:
                tail_samples.append(t[-60:])
        if t[0] in LEADING_PUNCT_CHARS:
            head += 1
            if len(head_samples) < 3:
                head_samples.append(t[:60])
        if not _is_balanced(t):
            unbal += 1
            if len(unbal_samples) < 3:
                unbal_samples.append(t[-60:])

    if n == 0:
        return {"n": 0, "tail_broken": 0, "head_broken": 0, "unbalanced": 0,
                "tail_pct": 0.0, "head_pct": 0.0, "unbalanced_pct": 0.0,
                "samples": {}}

    return {
        "n": n,
        "tail_broken": tail,
        "head_broken": head,
        "unbalanced": unbal,
        "tail_pct": round(tail / n * 100, 1),
        "head_pct": round(head / n * 100, 1),
        "unbalanced_pct": round(unbal / n * 100, 1),
        "samples": {
            "tail": tail_samples,
            "head": head_samples,
            "unbalanced": unbal_samples,
        },
    }


def rule_chunk_boundary_damage(
    db: Any, novel_ids: list[int] | None = None, taken: set[str] | None = None,
) -> tuple[list[DiagnosticIssue], dict[str, Any]]:
    """检测切分是否落在句子/段落边界上。

    **这是切分器的正确性检查，不是语料内容检查。** 切开的位置不对，
    说明切分逻辑有缺陷（或用了字符窗口切分），需要重切。

    与 `empty_or_short_chunks` 的区别：那条查「文本是否为空/过短」，
    这条查「文本是否在句子中间被切断」。一段 1200 字的完整文本可以
    既不空也不短，却是从半句话开始的。

    Returns:
        (issues, stats)
    """
    from sqlalchemy import select

    from app.models import NovelChunk, RawNovel

    q = select(NovelChunk.novel_id, NovelChunk.text)
    if novel_ids is not None:
        if not novel_ids:
            # 空列表 → 明确拒，不广播全库（与 audit_corpus 同一纪律）
            return [], {"skipped": "novel_ids 为空"}
        q = q.where(NovelChunk.novel_id.in_(novel_ids))

    by_novel: dict[int, list[str]] = {}
    for nid, text in db.execute(q).all():
        by_novel.setdefault(nid, []).append(text or "")

    titles = {
        r.id: r.title
        for r in db.execute(select(RawNovel)).scalars().all()
    }

    issues: list[DiagnosticIssue] = []
    stats: dict[str, Any] = {"novels_scanned": 0, "chunks_scanned": 0}
    taken = taken if taken is not None else set()

    for nid, chunks in by_novel.items():
        s = scan_chunks(chunks)
        stats["novels_scanned"] += 1
        stats["chunks_scanned"] += s["n"]
        title = titles.get(nid, f"novel:{nid}")

        # 半句结尾
        if s["tail_pct"] > BROKEN_RATE_THRESHOLD * 100:
            issues.append(DiagnosticIssue(
                id=make_issue_id("chunk_tail_broken", f"novel{nid}", taken),
                type="corpus_quality",
                severity="error" if s["tail_pct"] > 50 else "warn",
                title=f"《{title}》{s['tail_pct']}% 的分片结尾不是句末标点",
                claim=(
                    f"{s['tail_broken']}/{s['n']} 个分片停在半句话中间。"
                    f"这说明切分是在固定字符位置硬切的，没有对齐句子边界。"
                    f"后果：所有从这里抽出的规则都基于不完整的句子。"
                ),
                suggestion=(
                    "重切语料（用段落/句子边界感知的切分器）。"
                    "抽查结尾： / ".join(repr(x) for x in s["samples"]["tail"])
                ),
                rule="chunk_tail_broken",
                evidence=(IssueEvidence(
                    kind="chunk", source=f"novel:{nid}", ref_id=str(nid),
                    snippet=(f"{s['tail_broken']}/{s['n']} 个分片停在半句中间；抽查："
                             + " / ".join(repr(x) for x in s["samples"]["tail"][:3])),
                    score=0.5,
                ),),
            ))

        # 半句开头
        if s["head_pct"] > BROKEN_RATE_THRESHOLD * 100:
            issues.append(DiagnosticIssue(
                id=make_issue_id("chunk_head_broken", f"novel{nid}", taken),
                type="corpus_quality",
                severity="error" if s["head_pct"] > 50 else "warn",
                title=f"《{title}》{s['head_pct']}% 的分片以标点开头",
                claim=(
                    f"{s['head_broken']}/{s['n']} 个分片的第一字符是逗号/句号等标点，"
                    f"说明它从上文的中间开始。"
                ),
                suggestion="重切语料。抽查开头： / ".join(
                    repr(x) for x in s["samples"]["head"]),
                rule="chunk_head_broken",
                evidence=(IssueEvidence(
                    kind="chunk", source=f"novel:{nid}", ref_id=str(nid),
                    snippet=(f"{s['head_broken']}/{s['n']} 个分片以标点开头；抽查："
                             + " / ".join(repr(x) for x in s["samples"]["head"][:3])),
                    score=0.5,
                ),),
            ))

        # 引号不配平
        if s["unbalanced_pct"] > BROKEN_RATE_THRESHOLD * 100:
            issues.append(DiagnosticIssue(
                id=make_issue_id("chunk_quote_unbalanced", f"novel{nid}", taken),
                type="corpus_quality",
                severity="warn",
                title=f"《{title}》{s['unbalanced_pct']}% 的分片引号不配平",
                claim=(
                    f"{s['unbalanced']}/{s['n']} 个分片的引号/括号数量不等。"
                    f"两种可能：(1) 切分把对白从中间切开了；"
                    f"(2) 原文本身跨段写对白（上一段只写前引号）。"
                    f"第一种要重切，第二种是语料排版习惯。"
                ),
                suggestion=(
                    "先用 tail_broken 判断是哪种：若 tail_broken 也高，"
                    "说明是切分问题；若 tail_broken 低而不配平高，"
                    "则是原文跨段对白，可忽略。抽查： "
                    + " / ".join(repr(x) for x in s["samples"]["unbalanced"])
                ),
                rule="chunk_quote_unbalanced",
                evidence=(IssueEvidence(
                    kind="chunk", source=f"novel:{nid}", ref_id=str(nid),
                    snippet=(f"{s['unbalanced']}/{s['n']} 个分片引号不配平；抽查："
                             + " / ".join(repr(x) for x in s["samples"]["unbalanced"][:3])),
                    score=0.5,
                ),),
            ))

    return issues, stats
