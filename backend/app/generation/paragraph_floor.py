"""段落数下限兜底：不增删一个字，只把过长段落按句末切开。

真机回归（2026-11-22 复跑第 5 章）：闸门要求段数 ≥70，实际 69，只差 1 段
就被判不合格。段落数是排版事实，适合用确定性规则兜底，不该为 1 段再调一次模型。
"""
from __future__ import annotations

import re

#: 句末标点（保留在切分点前面）。
_SENTENCE_END = re.compile(r"(?<=[。！？!?])")

#: 可切分的段落最短长度：太短的段落切完会变成两个极短段，反而触发别的闸门。
MIN_SPLIT_LEN = 40


def _split_point(paragraph: str) -> int | None:
    """在段落中间附近找一个句末切分点；找不到返回 None。"""
    points = [m.end() for m in _SENTENCE_END.finditer(paragraph)
              if 0 < m.end() < len(paragraph)]
    if not points:
        return None
    mid = len(paragraph) / 2
    return min(points, key=lambda p: abs(p - mid))


def ensure_min_paragraphs(text: str, min_paragraphs: int) -> tuple[str, dict]:
    """把非空段落数补到 `min_paragraphs`（只切分，不改字）。

    优先切「不含引号」的长段落（叙述），没有再切含对白的段落；
    每次切在当前最长、且可切的那一段。切不动就停，不会无限循环。
    """
    info = {"min": int(min_paragraphs or 0), "before": 0, "after": 0,
            "applied": False, "reason": None}
    if not text or min_paragraphs is None or int(min_paragraphs) <= 0:
        info["reason"] = "no_limit"
        return text, info

    paras = [p for p in text.split("\n") if p.strip()]
    info["before"] = len(paras)
    if len(paras) >= int(min_paragraphs):
        info["after"] = len(paras)
        info["reason"] = "already_met"
        return text, info

    need = int(min_paragraphs) - len(paras)
    for _ in range(need):
        cands = [
            (len(p), i, "“" not in p and "”" not in p)
            for i, p in enumerate(paras)
            if len(p) >= MIN_SPLIT_LEN and len(_SENTENCE_END.findall(p)) >= 2
        ]
        if not cands:
            info["reason"] = "no_splittable_paragraph"
            break
        # 先优先叙述段，再按长度取最长
        cands.sort(key=lambda x: (not x[2], -x[0]))
        _, idx, _ = cands[0]
        para = paras[idx]
        cut = _split_point(para)
        if cut is None:
            info["reason"] = "no_split_point"
            break
        paras[idx:idx + 1] = [para[:cut].strip(), para[cut:].strip()]

    got = "\n".join(paras)
    info["after"] = len(paras)
    info["applied"] = info["after"] > info["before"]
    if info["applied"]:
        info["reason"] = None
    return got, info
