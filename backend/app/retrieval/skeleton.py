"""第二步：把 L1–L5 路径压成一张静态常驻骨架（设计稿 §5.12.1）。

骨架只做三件事：
1. 让模型先看到「这本书有哪些场景 / 节拍 / 技法、各多少条」；
2. 用固定结构层（14/6/8）保证跨书可比，计数层随档案变；
3. 把字数压在 2,200 字以内，超出的行不硬塞，明确告诉模型用 browse 下钻。

缓存键是 `distill_id + catalog_version`：卡表重建后版本号变，骨架自动失效。
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.retrieval.catalog_config import SKELETON_CHAR_CAP, SKELETON_MIN_PATH_COUNT
from app.retrieval.catalog_paths import (
    L1_ORDER,
    L2_LABELS,
    L3_LABELS,
    L4_LABELS,
    L4_NEW_ORDER,
    L4_ORDER,
)
from app.retrieval.catalog_store import (
    Catalog,
    clear_cache,
    load_catalog,
    skeleton_path,
    write_json_atomic,
)

MAX_HARD_LIMIT = 2250
_TRUNC_NOTE_RESERVE = 70
#: 每行最多列 8 个 L4；其余合并成「其他N」。新增 7 类必须优先露出，
#: 不能被高频旧类挤出骨架（2026-11-13 裁决）。
L4_LINE_CAP = 8


def _l4_parts(cards: list[dict]) -> str:
    """`：对白12 心理5 未细分3`；每行 Top 8 + 其他N，未细分永远最后。"""
    counts = Counter(c.get("l4") for c in cards)
    items = [
        (k, v)
        for k, v in counts.items()
        if k and v >= SKELETON_MIN_PATH_COUNT
    ]
    items.sort(key=lambda kv: (-kv[1], L4_ORDER.index(kv[0])))

    chosen = items[:L4_LINE_CAP]
    chosen_keys = {k for k, _ in chosen}
    mandatory = [
        (k, v) for k, v in items
        if k in L4_NEW_ORDER and k not in chosen_keys
    ]
    if mandatory:
        # 新类优先：先保新类，再用其余高分项补满；被挤掉的进「其他N」。
        chosen = mandatory + [
            it for it in items if it not in mandatory
        ][: max(0, L4_LINE_CAP - len(mandatory))]
        chosen.sort(key=lambda kv: (-kv[1], L4_ORDER.index(kv[0])))

    chosen_set = set(chosen)
    rest = sum(v for k, v in counts.items() if k and v >= SKELETON_MIN_PATH_COUNT
               and (k, v) not in chosen_set)
    parts = [f"{L4_LABELS.get(k, k)}{n}" for k, n in chosen]
    if rest:
        parts.append(f"其他{rest}")
    unassigned = counts.get(None, 0)
    if unassigned:
        parts.append(f"未细分{unassigned}")
    return ("：" + " ".join(parts)) if parts else ""


def _global_lines(cards: list[dict]) -> list[tuple[str, int]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        groups[str(c.get("dim") or "未知")].append(c)
    lines: list[tuple[str, int]] = []
    other = 0
    for dim, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(group)
        if n < SKELETON_MIN_PATH_COUNT:
            other += n
            continue
        lines.append((f"  {dim} {n}{_l4_parts(group)}", n))
    if other:
        lines.append((f"  其他通用 {other}", other))
    return lines


def _structural_lines(cards: list[dict], *, expand_top: int = 3,
                      expand_min: int = 20) -> list[tuple[str, int]]:
    """结构节按条数降序；只有最靠前、体量够大的 L2 才展开 L3。

    设计稿没有要求「每个 L2 都必须展开 L3」，但要求三节都留在 2,200 字内。
    13,000+ 条技法如果因为结构节展开过细而被整节截掉，骨架就失去意义；
    因此这里用「前 N 个 L2 展开、其余只给总数」的预算策略，计数不丢，
    下钻交给 browse。N 与最小体量都写在参数上，方便按实测调。
    """
    groups: dict[str | None, list[dict]] = defaultdict(list)
    for c in cards:
        groups[c.get("l2")].append(c)

    lines: list[tuple[str, int]] = []
    other_scene = 0
    unassigned = groups.pop(None, [])
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for rank, (l2, group) in enumerate(ordered):
        n = len(group)
        if n < SKELETON_MIN_PATH_COUNT:
            other_scene += n
            continue
        label = L2_LABELS.get(str(l2), str(l2))
        lines.append((f"    {label} {n}", n))
        if rank >= expand_top or n < expand_min:
            continue
        by_l3: dict[str | None, list[dict]] = defaultdict(list)
        for c in group:
            by_l3[c.get("l3")].append(c)
        no_beat = by_l3.pop(None, [])
        for l3, sub in sorted(by_l3.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
            m = len(sub)
            if m < SKELETON_MIN_PATH_COUNT:
                continue
            l3_label = L3_LABELS.get(str(l3), str(l3))
            # 小节拍只给总数，不铺 L4，避免一行吃掉 60 字。
            suffix = _l4_parts(sub) if m >= 10 else ""
            lines.append((f"      {l3_label} {m}{suffix}", m))
        if no_beat:
            lines.append((f"      未分节拍 {len(no_beat)}", len(no_beat)))
    if other_scene:
        lines.append((f"    其他场景 {other_scene}", other_scene))
    if unassigned:
        lines.append((f"    未挂场景 {len(unassigned)}", len(unassigned)))
    return lines


def _technique_lines(cards: list[dict]) -> list[tuple[str, int]]:
    groups: dict[str | None, list[dict]] = defaultdict(list)
    for c in cards:
        groups[c.get("l2")].append(c)

    lines: list[tuple[str, int]] = []
    other_scene = 0
    for l2, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        n = len(group)
        if n < SKELETON_MIN_PATH_COUNT:
            other_scene += n
            continue
        label = "跨场景" if l2 is None else L2_LABELS.get(str(l2), str(l2))
        lines.append((f"    {label} {n}{_l4_parts(group)}", n))
    if other_scene:
        lines.append((f"    其他场景 {other_scene}", other_scene))
    return lines


def render_skeleton(catalog: Catalog) -> tuple[str, dict]:
    started = time.perf_counter()
    # 骨架是导航，不列 deprecated（高频场景模板 3,395 条保留但默认不展示）。
    cards = catalog.active_cards
    global_cards = [c for c in cards if c.get("shape") == "global"]
    structural = [c for c in cards if c.get("shape") == "structural"]
    technique = [c for c in cards if c.get("shape") == "technique"]

    l1_counts = Counter(c.get("l1") for c in cards if c.get("l1"))
    header_bits = [
        f"{key} {l1_counts[key]}"
        for key in L1_ORDER
        if l1_counts.get(key, 0)
    ]
    header = f"【目录骨架】总 {len(cards)} 条（{' / '.join(header_bits)}）"

    sections = [
        ("通用", len(global_cards), _global_lines(global_cards)),
        ("结构", len(structural), _structural_lines(structural)),
        ("技法", len(technique), _technique_lines(technique)),
    ]

    # 先把所有行摊平，再按字数上限贪心截断；每行记着它代表多少条卡。
    all_lines: list[tuple[str, int, str]] = [(header, len(cards), "头部")]
    section_chars: Counter[str] = Counter()
    section_body_chars: Counter[str] = Counter()
    section_line_counts: Counter[str] = Counter()
    for name, total, lines in sections:
        heading = f"{name} {total}"
        all_lines.append((heading, total, name))
        section_chars[name] += len(heading)
        section_line_counts[name] += 1
        for line, n in lines:
            all_lines.append((line, n, name))
            section_chars[name] += len(line) + 1
            section_body_chars[name] += len(line) + 1
            section_line_counts[name] += 1

    body_cap = SKELETON_CHAR_CAP - _TRUNC_NOTE_RESERVE
    kept: list[tuple[str, int, str]] = []
    used = 0
    omitted_lines = 0
    omitted_cards = 0
    for idx, (line, n, section) in enumerate(all_lines):
        cost = len(line) + 1
        # 头部和第一节标题必须留下，否则截断后不可读。
        if idx < 2 or used + cost <= body_cap:
            kept.append((line, n, section))
            used += cost
        else:
            omitted_lines = len(all_lines) - idx
            omitted_cards = sum(x[1] for x in all_lines[idx:])
            break

    text = "\n".join(line for line, _, _ in kept)
    if omitted_lines:
        note = (
            f"\n…另有 {omitted_lines} 行 / {omitted_cards} 条因 "
            f"{SKELETON_CHAR_CAP} 字上限省略，用 browse 继续下钻。"
        )
        if len(text) + len(note) > MAX_HARD_LIMIT:
            # 再退一行，给提示留位置；正常情况下不会走到这里。
            text = "\n".join(line for line, _, _ in kept[:-1])
        text += note

    stats = {
        "distill_id": catalog.distill_id,
        "catalog_version": catalog.catalog_version,
        "total_cards": len(cards),
        "actual_chars": len(text),
        "char_cap": SKELETON_CHAR_CAP,
        "hard_limit": MAX_HARD_LIMIT,
        "truncated": bool(omitted_lines),
        "omitted_lines": omitted_lines,
        "omitted_cards": omitted_cards,
        "section_cards": {name: total for name, total, _ in sections},
        "section_chars": dict(section_chars),
        "section_body_chars": dict(section_body_chars),
        "section_line_counts": dict(section_line_counts),
        "l1_counts": dict(l1_counts),
        "render_seconds": round(time.perf_counter() - started, 3),
    }
    return text, stats


def skeleton_for_project(db, project) -> str | None:
    """按项目找到对应档案 → 返回可直接注入系统提示的骨架文本。

    取不到（项目未配参考书 / 档案未建卡 / 缓存损坏）一律返回 None：骨架是
    增强，不是生成的前提，不能因为目录没建就阻断正文生成。
    """
    try:
        novel_ids: list[int] = []
        for attr in ("continuation_novel_id", "active_novel_id"):
            value = getattr(project, attr, None)
            if value:
                novel_ids.append(int(value))
        refs = getattr(project, "reference_novel_ids", None) or []
        if isinstance(refs, str):
            refs = json.loads(refs)
        novel_ids.extend(int(x) for x in refs)
        if not novel_ids:
            return None

        from sqlalchemy import select

        from app.models import StyleDistill

        rows = db.execute(
            select(StyleDistill.id, StyleDistill.source_ids)
            .where(StyleDistill.status == "active")
            .order_by(StyleDistill.id.desc())
        ).all()
        for distill_id, source_ids in rows:
            ids = source_ids or []
            if isinstance(ids, str):
                ids = json.loads(ids)
            if set(novel_ids) & {int(x) for x in ids}:
                return build_skeleton(int(distill_id))["text"]
    except Exception:  # noqa: BLE001 - 骨架失败不阻断生成
        import logging

        logging.getLogger(__name__).warning(
            "目录骨架读取失败（project=%s）", getattr(project, "id", None),
            exc_info=True,
        )
    return None


def build_skeleton(distill_id: int, *, force: bool = False) -> dict:
    catalog = load_catalog(distill_id)
    path = skeleton_path(distill_id)
    if path.exists() and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 坏缓存直接重建
            cached = None
        if cached and cached.get("catalog_version") == catalog.catalog_version:
            cached["cache_hit"] = True
            return cached
    text, stats = render_skeleton(catalog)
    payload = {
        "distill_id": distill_id,
        "catalog_version": catalog.catalog_version,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "stats": stats,
        "cache_hit": False,
    }
    write_json_atomic(path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成静态常驻目录骨架")
    parser.add_argument("--distill-id", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    clear_cache()
    payload = build_skeleton(args.distill_id, force=args.force)
    print(payload["text"])
    print("\n--- stats ---")
    print(json.dumps(payload["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
