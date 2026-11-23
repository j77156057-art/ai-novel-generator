"""规则正文剥离（卡表判定与 L4 词表共用）。

卡表把参考书场景原句以「（证据：……）」附在规则后面，`rule` 工具的返回还可能
再拼一行证据。证据是**参考书**的原句，与本项目正文天然低重合，混进判定会把
真实改写全部压到阈值以下。两处调用必须用同一把尺，故集中到这里。
"""
from __future__ import annotations

import re

#: 规则正文里的「（证据：……）」。
_EVIDENCE_BLOCK = re.compile(r"[（(]\s*证据\s*[:：]\s*(.*?)[）)]", re.S)


def rule_body(text: str) -> str:
    """只保留规则正文：去掉「（证据：……）」与单独回传的证据行。"""
    raw = text or ""
    evidence = [m.strip() for m in _EVIDENCE_BLOCK.findall(raw)]
    body = _EVIDENCE_BLOCK.sub("", raw)
    if evidence:
        kept = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 单独回传的 evidence 行通常就是括号里那一段（可能截断），去掉。
            if any(ev and (stripped in ev or ev.startswith(stripped[:12]))
                   for ev in evidence):
                continue
            kept.append(line)
        body = "\n".join(kept)
    return body.strip()
