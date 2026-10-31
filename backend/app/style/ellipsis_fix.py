"""省略号习惯校正：让对白里的省略号密度对齐**参考书自己**。

## 为什么要有（实测，不是推测）

2026-10-31 的盲评包里，「**有省略号的那侧 = 人类侧**」这条规则
可判 7 对、判对 7 对、**精确率 100%** ⇒ 一条真泄露线。

根因：我们 0.068 个/百字 vs《魔道祖师》0.684 个/百字，**差 10 倍**。
（注意这跟「归属」是同一类问题：**写法指纹**没对齐。）

## 尺 = 参考书自己

- `ellipsis_rate(text)`：**含省略号的对白**占对白句的比例（分母 = 对白句数）
- 目标从参考书实测（与 `attribution_target` 同一套思路 ⇒ 换书自动跟着换）

## 参考书的省略号用在哪（实测 1232 处）

| 位置 | 占比 |
|---|---|
| 对白**结尾**（说话人欲言又止） | 32% |
| 对白开头 | 26% |
| 对白中间 | 21% |
| 叙述里 | 20% |

⇒ 79% 在对白里，主形态是**结尾** ⇒ 我们按目标比例，在对白的结尾补「……」。

## 为什么是**机械**补，不是让模型改

模型对数量没有掌控力（归属校正实测：同一段同一目标，四种措辞分别落在
58% / 35% / 10% / 25%~45%）⇒ 让模型反复调是赌。
而省略号的**位置**有明确惯例（对白结尾）⇒ 数量由我们本地定，位置照惯例。

保留哪几句按**引语内容的哈希**挑 ⇒ 分布不规则、且可复现。

## 失败策略

正文太短 / 对白太少 ⇒ 不动。校正失败绝不能影响正文。
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Sequence

logger = logging.getLogger(__name__)

#: 一句对白（含引号）。**分母用这一把尺**。
QUOTE_RE = re.compile(r"[“「『][^”」』]{1,400}[”」』]")

#: 引语里的内容（不含引号）。
_INNER_RE = re.compile(r"([“「『])([^”」』]{1,400})([”」』])")

#: 省略号（中文省略号，可能连续多个）。
_ELLIPSIS_RE = re.compile(r"…+|\.{3,}")

#: 引语末尾的**句末标点**（补省略号时要先把它去掉 ——
#: 参考书写的是「我保护你……」而不是「我保护你。……」）。
_TAIL_PUNCT = "。！？!?；;，,"

#: 参考书的目标形态：省略号落在**对白结尾**。
ELLIPSIS_SHAPE = "tail"

#: 正文太短 / 对白太少就不做。
MIN_CHARS = 200
MIN_QUOTES = 5


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def ellipsis_rate(text: str) -> dict:
    """量一段文本的「含省略号的对白占多少」。

    返回 `{"quotes": n, "with_ellipsis": k, "rate": r, "chars": c}`。

    ⚠️ **分母是「对白句数」**，不是每百字 —— 与归属校正同一口径
    （每百字会把「说多少话」和「说的话里怎么用标点」混在一起）。
    """
    t = _norm(text)
    spans = list(_INNER_RE.finditer(t))
    with_ell = 0
    for m in spans:
        if _ELLIPSIS_RE.search(m.group(2)):
            with_ell += 1
    n = len(spans)
    return {"quotes": n, "with_ellipsis": with_ell,
            "rate": (with_ell / n) if n else 0.0, "chars": len(t)}


def ellipsis_target(paragraphs: Sequence[str]) -> dict | None:
    """从参考书的段落里量出「省略号目标」。

    返回 `{"rate": 目标比例, "quotes": n, "with_ellipsis": k}`；
    量不出来（对白太少）⇒ `None`（不校正）。
    """
    text = "\n".join(p or "" for p in (paragraphs or []))
    if len(_norm(text)) < 500:
        return None
    m = ellipsis_rate(text)
    if m["quotes"] < MIN_QUOTES:
        return None
    return {"rate": m["rate"], "quotes": m["quotes"],
            "with_ellipsis": m["with_ellipsis"], "shape": ELLIPSIS_SHAPE}


def _quote_spans(text: str) -> list[tuple[int, int, str]]:
    """引语区间（开引号位置, 闭引号位置, 引语内容）。"""
    out: list[tuple[int, int, str]] = []
    for m in _INNER_RE.finditer(text):
        out.append((m.start(2), m.end(2), m.group(2)))
    return out


def apply_ellipsis_habit(text: str, target_rate: float) -> tuple[str, dict]:
    """把对白里的省略号密度调到目标。返回 `(文本, 诊断)`。

    诊断字段：`applied` / `reason` / `before` / `after` / `inserted` / `removed`。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    before = ellipsis_rate(text)
    if before["chars"] < MIN_CHARS:
        return text, {"applied": False, "reason": "正文太短", "before": before}
    if before["quotes"] < MIN_QUOTES:
        return text, {"applied": False, "reason": "对白太少", "before": before}

    want = int(round(target_rate * before["quotes"]))
    have = before["with_ellipsis"]
    if have == want:
        return text, {"applied": False, "reason": "已经达标", "before": before}

    spans = _quote_spans(text)
    if want > have:
        # —— 补：挑不含省略号的引语，按**内容哈希**排序（分布不规则、可复现）——
        cand = [s for s in spans if not _ELLIPSIS_RE.search(s[2])]
        need = min(want - have, len(cand))
        if need <= 0:
            return text, {"applied": False, "reason": "没有可补的引语", "before": before}
        order = sorted(
            range(len(cand)),
            key=lambda i: hashlib.sha256(cand[i][2].encode("utf-8")).hexdigest())
        picked = {order[i] for i in range(need)}
        out = text
        inserted = 0
        # 从后往前改 ⇒ 前面位置的偏移不受影响
        for i in sorted(picked, reverse=True):
            s, e, inner = cand[i]
            body = inner.rstrip()
            # 末尾的句末标点**去掉**（不保留）——
            # 参考书写的是「我保护你……」而不是「我保护你。……」
            while body and body[-1] in _TAIL_PUNCT:
                body = body[:-1]
            new_inner = body + "……"
            out = out[:s] + new_inner + out[e:]
            inserted += 1
        after = ellipsis_rate(out)
        info = {"applied": True, "before": before, "after": after,
                "inserted": inserted, "removed": 0, "target": target_rate}
        logger.info("省略号校正：%d → %d 句带省略号（目标 %d）",
                    have, after["with_ellipsis"], want)
        return out, info

    # —— 减：去掉多余的省略号 ——
    cand = [s for s in spans if _ELLIPSIS_RE.search(s[2])]
    need = have - want
    order = sorted(
        range(len(cand)),
        key=lambda i: hashlib.sha256(cand[i][2].encode("utf-8")).hexdigest())
    picked = {order[i] for i in range(min(need, len(cand)))}
    out = text
    removed = 0
    for i in sorted(picked, reverse=True):
        s, e, inner = cand[i]
        new_inner = _ELLIPSIS_RE.sub("", inner)
        out = out[:s] + new_inner + out[e:]
        removed += 1
    after = ellipsis_rate(out)
    info = {"applied": True, "before": before, "after": after,
            "inserted": 0, "removed": removed, "target": target_rate}
    logger.info("省略号校正：%d → %d 句带省略号（目标 %d）",
                have, after["with_ellipsis"], want)
    return out, info
