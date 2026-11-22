"""对白引号口径：按**参考书自己的写法**做确定性归一化。

## 为什么要有这一步（2026-11-22 五章真机回归）

第 3 章整章用了「」，同一本书的其它四章都是“”；导出后读者能看到
两种引号混在一条时间线上。更隐蔽的问题是：声纹检查的正则只认“”，
整章「」会让所有角色都被判成「没有台词」，后续的定向重写全建立在错误诊断上。

## 目标从哪来

不是拍一个「一律用“”」：

1. 先量参考语料（最近读入的若干 chunk）的主导引号形态；
2. 参考语料量不出来（没参考书 / 全是英文引号）⇒ 退回**本文自身**的主导形态；
3. 两者都量不出来 ⇒ 不动正文。

只做「成对引号」之间的转换（“”‘’ ↔ 「」『』），并把半角双引号 `"` 按行内
顺序配对改成目标形态（真机实测：模型改写对白时会写成 `"..."`）。半角单引号
`'` 同时是英文撇号，不做转换。
"""
from __future__ import annotations

from sqlalchemy import select

from app.models import NovelChunk

#: 成对引号的三种形态。只支持前两种之间互转。
CURLY = ("“", "”", "‘", "’")
CORNER = ("「", "」", "『", "』")

_PAIR_STYLES: dict[str, tuple[str, str, str, str]] = {
    "curly": CURLY,
    "corner": CORNER,
}

#: 读参考语料时最多取多少个 chunk（只用于判断引号形态，不参与生成）。
REFERENCE_CHUNKS = 12


def detect_quote_style(text: str) -> str | None:
    """量一段文本的主导引号形态：`curly` / `corner` / `straight` / `None`。

    按「开+内开」与「闭+内闭」的较小值计分，避免只有半边的脏文本被判成某种形态。
    """
    t = text or ""
    scores = {
        "curly": min(t.count(CURLY[0]) + t.count(CURLY[2]),
                     t.count(CURLY[1]) + t.count(CURLY[3])),
        "corner": min(t.count(CORNER[0]) + t.count(CORNER[2]),
                      t.count(CORNER[1]) + t.count(CORNER[3])),
        "straight": t.count('"') // 2,
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else None


def _pair_straight_quotes(line: str, dst: tuple[str, str, str, str]) -> tuple[str, int]:
    """把一行里的半角双引号按出现顺序两两改成 `dst` 的开/闭引号。

    只处理双引号 `"`；半角单引号同时是英文撇号，盲转会破坏正文。
    落单的最后一个 `"` 保持不变（无法判断开闭）。
    """
    if '"' not in line:
        return line, 0
    count = line.count('"')
    usable = count if count % 2 == 0 else count - 1
    if usable <= 0:
        return line, 0
    out: list[str] = []
    opened = False
    used = 0
    for ch in line:
        if ch == '"' and used < usable:
            out.append(dst[0] if not opened else dst[1])
            opened = not opened
            used += 1
        else:
            out.append(ch)
    return "".join(out), usable


def normalize_quotes(text: str, target: str) -> tuple[str, int]:
    """把成对引号统一成 `target` 形态，返回 `(新文本, 替换字符数)`。

    处理两件事：①成对的中文引号互转（“”‘’ ↔ 「」『』）；
    ②半角双引号按行内顺序配对转成目标形态。半角单引号不动。
    `target` 不是 `curly` / `corner` 时原样返回。
    """
    if target not in _PAIR_STYLES or not text:
        return text, 0
    dst = _PAIR_STYLES[target]
    mapping: dict[int, str] = {}
    for style, chars in _PAIR_STYLES.items():
        if style == target:
            continue
        for src, out in zip(chars, dst):
            if src != out:
                mapping[ord(src)] = out
    replaced = sum(text.count(chr(code)) for code in mapping) if mapping else 0
    got = text.translate(mapping) if mapping and replaced else text
    lines: list[str] = []
    straight = 0
    for line in got.split("\n"):
        fixed, n = _pair_straight_quotes(line, dst)
        lines.append(fixed)
        straight += n
    if not replaced and not straight:
        return text, 0
    return "\n".join(lines), replaced + straight


def reference_quote_style(db, novel_ids) -> str | None:
    """量参考语料的主导引号形态；读不到 / 量不出返回 None。"""
    ids = [int(i) for i in (novel_ids or []) if isinstance(i, int) or str(i).isdigit()]
    if db is None or not ids:
        return None
    try:
        rows = db.execute(
            select(NovelChunk.text)
            .where(NovelChunk.novel_id.in_(ids))
            .order_by(NovelChunk.id)
            .limit(REFERENCE_CHUNKS)
        ).all()
    except Exception:  # noqa: BLE001 —— 读不到参考语料不能拖垮生成
        return None
    text = "".join(str(r[0] or "") for r in rows)
    style = detect_quote_style(text)
    return style if style in _PAIR_STYLES else None


def normalize_to_reference(db, project, text: str, novel_ids=None) -> tuple[str, dict]:
    """按参考书口径统一本文引号；返回 `(正文, 留痕)`。"""
    ids = list(novel_ids if novel_ids is not None
               else (getattr(project, "reference_novel_ids", None) or []))
    ref = reference_quote_style(db, ids)
    target = ref or detect_quote_style(text)
    info = {
        "reference_style": ref,
        "target_style": target,
        "replaced": 0,
        "applied": False,
    }
    if target not in _PAIR_STYLES:
        info["skipped"] = "no_paired_quotes"
        return text, info
    got, replaced = normalize_quotes(text, target)
    info["replaced"] = replaced
    info["applied"] = replaced > 0
    return got, info
