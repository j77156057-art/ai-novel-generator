# -*- coding: utf-8 -*-
"""篇幅上限的落库闸：压缩后仍超 120% 的终稿不得成为正式正文。

为什么单独一层：压缩本身在 `_rewrite_rounds` 末尾复用现成
`_compression_round` 跑过一次（`path="compression"`）。这里只做写库前的
最后一道判定——把「压缩后仍超」从一句留痕升级成阻断，且与生成链共用
`quality.length_path` 那一把尺，不新增常量、不改 `LENGTH_LIMIT_RATIO`。

下限（<80%）不在这里处理：仍按既有口径只留痕，本轮只补上限这一侧。
"""
from __future__ import annotations

from app.style.consistency import hanzi_count, length_limits


class LengthBlocked(RuntimeError):
    """终稿超过篇幅上限、压缩后仍未压回：拒绝落库。

    与 `StructureBlocked` 同一条纪律——在 `_commit_segment` 写库前抛，本章
    不产生 Generation 行。异常自带 `to_dict()`，路由把它翻成 422 + blocked 明细。
    """

    def __init__(self, *, segment_no: int, label: str, evidence: dict):
        self.segment_no = segment_no
        self.label = label
        self.evidence = evidence
        super().__init__(
            f"拒绝落库第{segment_no}段（{label}）：终稿 {evidence['hanzi']} 汉字"
            f" > 上限 {evidence['limit']}（路径={evidence.get('path')}，"
            f"压缩已接受={evidence.get('compression_accepted')}）"
        )

    def to_dict(self) -> dict:
        return {
            "status": "blocked",
            "blocked_by": "length_over_limit",
            "segment_no": self.segment_no,
            "chapter_label": self.label,
            **self.evidence,
        }


def length_block_evidence(project, text: str,
                          quality: dict | None) -> dict | None:
    """终稿超上限的证据；未超返回 None。

    上限优先取生成链已算好的 `quality.length_path.limit`（与压缩轮同一把尺）；
    缺省（直连 engine 的旧调用）才按项目既有 `generation_length` 现算。
    """
    path = (quality or {}).get("length_path") or {}
    limit = path.get("limit")
    if limit is None:
        budget = getattr(project, "generation_length", None)
        if not budget:
            return None
        limit = length_limits(budget)["max"]
    hanzi = hanzi_count(text)
    if hanzi <= int(limit):
        return None
    return {
        "hanzi": hanzi,
        "limit": int(limit),
        "path": path.get("path"),
        "compression_accepted": bool(path.get("compression_accepted")),
        "still_over_limit": True,
    }
