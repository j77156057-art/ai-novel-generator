"""题材行情卡：独立素材层（**不进**文风规则库、**不进**蒸馏档案）。

为什么单独一层：题材热度是**有寿命的外部情报**，和「怎么写」的文风规则、
「这本书是什么」的蒸馏档案是三种不同的东西。混进后两者会让档案被外部
数据污染，也会让过期热梗一直留在提示词里。

本模块只做三件事：

1. `load_cards()` —— 读 `backend/data/market_cards/*.json`，校验必填字段，
   **丢掉超出时间窗的卡**（`today > window.to` 的卡不参与任何回答）。
2. `genre_block()` / `protagonist_directions()` —— 按题材给出 2–3 个方向，
   每条都带来源、采集日期、置信度、适用时间窗，便于用户自己判断可信度。
3. 无卡 / 过期 / 题材不匹配时返回空字符串或空列表，调用方退回原有的静态
   题材族表，不报错、不硬凑。

卡里的结论一律来自真实检索（`source.url` 可回查），并且必须保留
`caveats`：榜单与平台扶持都是幸存者偏差，不能当成「写这个就有人看」。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

#: 默认素材目录。用目录而不是单文件：不同来源/不同批次可以独立增删，
#: 过期卡直接删文件即可，不需要改代码。
CARD_DIR = Path(__file__).resolve().parent.parent / "data" / "market_cards"

#: 卡片必填字段。少一个就当坏卡处理（生产跳过，测试 strict 报错）。
REQUIRED_FIELDS = (
    "card_id",
    "genres",
    "captured_at",
    "window",
    "confidence",
    "source",
    "signals",
)

CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}
_CONFIDENCE_WEIGHT = {"high": 3, "medium": 2, "low": 1}


# ---------------------------------------------------------------------------
# 读取与校验
# ---------------------------------------------------------------------------


def _as_date(value: Any, field: str, card_id: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{card_id} 的 {field} 不是 YYYY-MM-DD：{value!r}") from exc


def _validate(card: dict[str, Any]) -> None:
    card_id = str(card.get("card_id") or "<无 id>")
    for field in REQUIRED_FIELDS:
        if field not in card:
            raise ValueError(f"{card_id} 缺少字段 {field}")
    if not isinstance(card["genres"], list) or not card["genres"]:
        raise ValueError(f"{card_id} 的 genres 必须是非空数组")
    if card["confidence"] not in CONFIDENCE_LABEL:
        raise ValueError(f"{card_id} 的 confidence 只能是 high/medium/low")
    source = card["source"] or {}
    if not str(source.get("url") or "").startswith(("http://", "https://")):
        raise ValueError(f"{card_id} 的 source.url 必须是可回查的 http(s) 链接")
    if not source.get("title"):
        raise ValueError(f"{card_id} 的 source.title 不能为空")
    window = card["window"] or {}
    start = _as_date(card["captured_at"], "captured_at", card_id)
    end = _as_date(window.get("to"), "window.to", card_id)
    if end < start:
        raise ValueError(f"{card_id} 的 window.to 早于 captured_at")


def in_window(card: dict[str, Any], today: date) -> bool:
    """时间窗判断：采集日之前、窗口结束后都不引用。"""
    window = card.get("window") or {}
    try:
        start = date.fromisoformat(str(window.get("from") or card.get("captured_at")))
        end = date.fromisoformat(str(window.get("to")))
    except ValueError:
        return False
    return start <= today <= end


def _score(card: dict[str, Any]) -> tuple[int, str]:
    """排序：置信度优先，其次来源更新的卡优先。"""
    return (
        _CONFIDENCE_WEIGHT.get(str(card.get("confidence")), 0),
        str((card.get("source") or {}).get("published_at") or card.get("captured_at") or ""),
    )


def load_cards(
    directory: Path | str | None = None,
    today: date | None = None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    """读取当前有效（未过期）的行情卡，按可信度/新鲜度排序。

    Args:
        directory: 素材目录；默认 `backend/data/market_cards`。
        today: 判定时间窗的「今天」；测试可注入固定日期。
        strict: True 时坏卡直接抛错（测试用）；False 时跳过坏卡（线上不因
            一张卡写错就把引导打挂）。
    """
    base = Path(directory) if directory else CARD_DIR
    today = today or date.today()
    cards: list[dict[str, Any]] = []
    if not base.is_dir():
        return cards
    for path in sorted(base.glob("*.json")):
        try:
            card = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(card, dict):
                raise ValueError("卡片根节点必须是对象")
            _validate(card)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if strict:
                raise ValueError(f"{path.name}: {exc}") from exc
            continue
        if not in_window(card, today):
            continue
        card = dict(card)
        card["_path"] = str(path)
        cards.append(card)
    cards.sort(key=_score, reverse=True)
    return cards


# ---------------------------------------------------------------------------
# 引用文案
# ---------------------------------------------------------------------------


def citation(card: dict[str, Any]) -> str:
    """来源 / 采集日期 / 置信度 / 适用时间窗，四件套缺一不可。"""
    source = card.get("source") or {}
    window = card.get("window") or {}
    conf = CONFIDENCE_LABEL.get(str(card.get("confidence")), str(card.get("confidence")))
    return (
        f"来源：{source.get('publisher') or source.get('title')}"
        f"《{source.get('title')}》（{source.get('published_at') or '未标注'}）；"
        f"采集 {card.get('captured_at')}；置信度 {conf}；"
        f"适用至 {window.get('to')}"
    )


def _caveats(cards: list[dict[str, Any]], limit: int = 2) -> list[str]:
    seen: list[str] = []
    for card in cards:
        for item in card.get("caveats") or []:
            text = str(item).strip()
            if text and text not in seen:
                seen.append(text)
    return seen[:limit]


def _first(card: dict[str, Any], key: str) -> str:
    values = (card.get("signals") or {}).get(key) or []
    return str(values[0]).strip() if values else ""


def _few(card: dict[str, Any], key: str, n: int = 3) -> str:
    values = [str(v).strip() for v in ((card.get("signals") or {}).get(key) or []) if str(v).strip()]
    return "、".join(values[:n])


# ---------------------------------------------------------------------------
# 题材 / 主角两步的方向
# ---------------------------------------------------------------------------


def genre_directions(
    today: date | None = None,
    limit: int = 3,
    directory: Path | str | None = None,
) -> list[dict[str, str]]:
    """2–3 个题材方向；每个方向都带四件套引用。"""
    cards = load_cards(directory=directory, today=today)
    out: list[dict[str, str]] = []
    used: set[str] = set()
    for card in cards:
        genre = str(card["genres"][0])
        if genre in used:
            continue
        used.add(genre)
        bits: list[str] = []
        direction = _first(card, "direction")
        tropes = _few(card, "tropes", 3)
        if direction:
            bits.append(direction)
        if tropes:
            bits.append(f"常见梗：{tropes}")
        text = "；".join(bits) or str((card.get("signals") or {}).get("direction") or "")
        if not text:
            continue
        out.append(
            {
                "genre": genre,
                "text": text,
                "citation": citation(card),
                "card_id": str(card.get("card_id")),
            }
        )
        if len(out) >= limit:
            break
    return out


def genre_block(
    today: date | None = None,
    limit: int = 3,
    directory: Path | str | None = None,
) -> str:
    """题材步的「有依据的方向」段落；没有有效卡时返回空串。"""
    cards = load_cards(directory=directory, today=today)
    directions = genre_directions(today=today, limit=limit, directory=directory)
    if not directions:
        return ""
    lines = [f"{i}. {d['genre']}：{d['text']}（{d['citation']}）" for i, d in enumerate(directions, 1)]
    caveats = _caveats(cards)
    tail = ("\n\n注意：" + "；".join(caveats)) if caveats else ""
    return (
        "【题材行情参考（外部情报，不是平台结论）】\n"
        + "\n".join(lines)
        + "\n\n这些是近期平台口径/编辑解读，只能当方向参考；"
        "合不合适还要看你想写的故事。" + tail
    )


def protagonist_directions(
    genre: str | None,
    today: date | None = None,
    limit: int = 3,
    directory: Path | str | None = None,
) -> list[dict[str, str]]:
    """该题材下的人设方向（只取卡里明确写过的，没有就返回空）。"""
    if not genre:
        return []
    out: list[dict[str, str]] = []
    for card in load_cards(directory=directory, today=today):
        if genre not in (card.get("genres") or []):
            continue
        for item in (card.get("signals") or {}).get("protagonist") or []:
            text = str(item).strip()
            if not text:
                continue
            out.append(
                {
                    "text": f"{text}（{citation(card)}）",
                    "citation": citation(card),
                    "card_id": str(card.get("card_id")),
                }
            )
            if len(out) >= limit:
                return out
    return out


def protagonist_note(
    genre: str | None,
    today: date | None = None,
    directory: Path | str | None = None,
) -> str:
    """主角步的短提示：有没有行情依据、风险是什么。"""
    dirs = protagonist_directions(genre, today=today, directory=directory)
    if not dirs:
        return ""
    cards = [c for c in load_cards(directory=directory, today=today) if genre in (c.get("genres") or [])]
    caveats = _caveats(cards, limit=1)
    tail = f"（{caveats[0]}）" if caveats else ""
    return (
        "下面的「人设方向」来自近期行情卡，每条都附了来源、采集日期、置信度和适用时间窗；"
        "它只能说明「有人这么做」，不代表这么做就讨喜" + tail + "。"
    )


def active_genres(today: date | None = None, directory: Path | str | None = None) -> list[str]:
    """当前有效卡覆盖的题材（解码/排查用）。"""
    genres: list[str] = []
    for card in load_cards(directory=directory, today=today):
        for genre in card.get("genres") or []:
            if genre not in genres:
                genres.append(str(genre))
    return genres
