"""形态校正（机械版）：只改段落边界，不新增一个字。

## 为什么可能是它

LLM 改写的实测采纳率太低（同一段输入重复 4 次 0/4；正式测量 0/20；
三次成功都是偶然）。但目标形态本身是**结构性**的：

    参考书：叙述 + 台词   写在同一段
    生成：  台词         单独一段

要从前者到后者，**把前面那段叙述并到台词段**就够了 —— 这是纯粹的**段落边界
调整**，不新增内容、不改对白、不改情节，**100% 可复现、零模型调用**。

## 怎么并

1. 找出「纯台词段」（去掉引号内容后只剩标点）；
2. 找它**前面**那段纯叙述（同场景的上一段，没有引号、长度够）；
3. 把叙述并进台词段（在两者之间断开），叙述在前、台词在后 —— 与参考书一致。

并多少段由**目标比例**定：要达到目标裸台词占比 t，需并 k = n(1-t) 段
（n = 纯台词段数）。并的顺序从后往前（前面段的引用才不错位）。

## 反方向（目标偏高：对白密集型书）

把「叙述 + 台词」拆成两段：叙述段收尾的「：」改成「。」，台词另起一段。
这一步会动一个标点（：→。），所以单独标注。
"""
from __future__ import annotations

import re
from typing import Sequence

#: 纯台词段判定：去掉引号内容后剩下的非标点字符不超过这个数
#: （与 style/stylometry.py 的 `_BARE_SEG_TAIL_MAX` 同一口径）。
BARE_TAIL_MAX = 6

_OPENERS = "“「『"
_CLOSERS = "”」』"


def _strip_quoted(text: str) -> str:
    """去掉成对引号及其内容（与 stylometry 同实现）。"""
    out = text
    for o, c in (("“", "”"), ("「", "」"), ("『", "』")):
        while o in out and c in out:
            a = out.index(o)
            b = out.find(c, a)
            if b < 0:
                out = out.replace(o, "")
            else:
                out = out[:a] + out[b + 1:]
    return out


def is_bare_paragraph(p: str) -> bool:
    """这一段是不是「光秃秃一句台词」。"""
    if not any(q in p for q in _OPENERS):
        return False
    rest = _strip_quoted(p).strip("：:，。！？…、；; \t　")
    return len(rest) <= BARE_TAIL_MAX


def _has_quote(p: str) -> bool:
    return any(q in p for q in _OPENERS)


def merge_bare_dialogue(
    paragraphs: Sequence[str],
    target_ratio: float,
    *,
    min_narration_chars: int = 20,
) -> tuple[list[str], dict]:
    """把「纯台词段」并进它前面那段叙述，逼近目标裸台词占比。

    返回 (新段落列表, 诊断)。诊断字段：
        merged     并了几段
        before/after  裸台词段占比（并之前/并之后）
        target     目标
        reason     为什么没并（没得并 / 已达目标 / 找不到可并的前段）
    """
    paras = list(paragraphs)
    bare_idx = [i for i, p in enumerate(paras) if is_bare_paragraph(p)]
    n = len(bare_idx)
    quote_n = sum(1 for p in paras if _has_quote(p))
    before = (n / quote_n) if quote_n else 0.0
    info = {"before": before, "after": before, "target": target_ratio, "merged": 0}

    if not n:
        info["reason"] = "没有纯台词段"
        return paras, info
    if before <= target_ratio:
        info["reason"] = f"已经低于目标（{before:.0%} ≤ {target_ratio:.0%}）"
        return paras, info

    # 需要并多少段：并 k 段 → 裸台词段 n-k、含引号段仍 quote_n
    need = n - int(round(target_ratio * quote_n))
    need = max(0, min(need, n))
    if need == 0:
        info["reason"] = "已经贴近目标"
        return paras, info

    # 从后往前并：每并一段，它前面那段叙述就归它所有
    merged = 0
    drop: set[int] = set()
    for i in reversed(bare_idx):
        if merged >= need:
            break
        # 找它前面那段纯叙述（跳过已经被并掉的）
        j = i - 1
        while j >= 0 and j in drop:
            j -= 1
        if j < 0:
            continue
        prev = paras[j]
        if _has_quote(prev) or len(prev.strip()) < min_narration_chars:
            continue
        paras[i] = f"{prev.strip()}{paras[i].strip()}"
        drop.add(j)
        merged += 1

    new_paras = [p for i, p in enumerate(paras) if i not in drop]
    n_after = sum(1 for p in new_paras if is_bare_paragraph(p))
    q_after = sum(1 for p in new_paras if _has_quote(p))
    info.update({
        "merged": merged,
        "after": (n_after / q_after) if q_after else 0.0,
        "reason": "已并" if merged else "找不到可并的前段叙述",
    })
    return new_paras, info
