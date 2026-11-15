"""把一本书的 KG 导出成可直接读的世界观清单与剧情时间线。

数据源与 API 同源（``story_context.worldview/timeline``），导出只是把它们
落成 markdown + JSON。时间线按 200 章分节，并在文末列出没有事件覆盖的空段，
避免「中间不留空段」只能靠人肉翻页确认。

用法（在 backend/ 下）::

    python -m app.kg.export_story --novel-id 7 --project-id 6 \
        --out-dir ../reports/kg-修真聊天群 --title 修真聊天群
"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter

from sqlalchemy import func, select

from app.db import SessionLocal
from app.kg.story_context import timeline, worldview
from app.models import KgEntity, KgEvent, KgRelation, NovelChunk

BUCKET = 200


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "／").replace("\n", " ").strip()


def _write_worldview(path: pathlib.Path, title: str, world: dict) -> None:
    lines = [
        f"# 《{title}》世界观清单（KG v3）",
        "",
        f"- 书：novel_id={world['novel_id']}",
        f"- 规模：势力/组织 {world['counts']['faction']}、地点 {world['counts']['location']}、"
        f"体系 {world['counts']['power_system']}、资源 {world['counts']['item']}、"
        f"规则 {world['counts']['worldrule']}",
        "- 每条都带证据章号；没有事件直接引用的条目退回首现章（标 first_appearance）。",
        "",
    ]
    sections = [
        ("一、势力与组织", "factions", "faction"),
        ("二、地点", "locations", "location"),
        ("三、体系与境界", "power_systems", "power_system"),
        ("四、资源与物品", "resources", "item"),
        ("五、规则与制度", "rules", "worldrule"),
    ]
    for heading, key, kind in sections:
        items = world[key]
        lines.append(f"## {heading}（{len(items)}）")
        lines.append("")
        for it in items:
            span = f"{it['first_chapter']} – {it['last_chapter']}"
            lines.append(f"### {it['name']}")
            if it.get("kind_origin") and it["kind_origin"] != kind:
                lines.append(f"- 跨界收录：原名归类为「{it['kind_origin']}」，"
                             "因名称含门派后缀同时列入本栏")
            lines.append(f"- 章号区间：{span} ｜ 提及 {it['mention_count']} 次")
            if it["aliases"]:
                lines.append(f"- 别名：{'、'.join(it['aliases'][:6])}")
            attrs = {k: v for k, v in (it["attrs"] or {}).items()
                     if v not in (None, "", "unknown")}
            if attrs:
                brief = "；".join(f"{k}={v}" for k, v in list(attrs.items())[:5]
                                  if not isinstance(v, (list, dict)))
                if brief:
                    lines.append(f"- 属性：{_md_escape(brief)}")
            lines.append("- 证据：")
            for ev in it["evidence"][:3]:
                lines.append(f"  - 第{ev['chapter']}章【{ev['event_type']}】"
                             f"{_md_escape(ev['summary'])}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_timeline(path: pathlib.Path, title: str, novel_id: int, project_id: int,
                    db, max_chapter: int, gaps: list[list[int]]) -> int:
    total = db.scalar(select(func.count(KgEvent.id)).where(
        KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None),
        KgEvent.is_current.is_(True))) or 0
    lines = [
        f"# 《{title}》剧情时间线（KG v3）",
        "",
        f"- 书：novel_id={novel_id} ｜ 事件 {total} 条 ｜ 覆盖 1–{max_chapter} 章",
        "- 每条格式：`第N章 ｜ 谁 ｜ 在哪 ｜ 做了什么 ｜ 结果`",
        "- 数据来自逐章窗口抽取，`result` 是抽取时的事件摘要（含结果）；"
        "没有事件的章节集中列在文末「空段」。",
        "",
    ]
    written = 0
    for start in range(1, max_chapter + 1, BUCKET):
        end = min(max_chapter, start + BUCKET - 1)
        page = timeline(db, project_id, novel_id, from_chapter=start,
                        to_chapter=end, limit=2000)
        lines.append(f"## 第 {start}–{end} 章（{page['total']} 条）")
        lines.append("")
        if not page["events"]:
            lines.append("_本段无事件_")
            lines.append("")
            continue
        for ev in page["events"]:
            who = _md_escape(ev["who"]) or "—"
            where = _md_escape(ev["where"]) or "—"
            what = _md_escape(ev["what"]) or "—"
            result = _md_escape(ev["result"]) or "—"
            lines.append(f"- 第{ev['chapter']}章 ｜ {who} ｜ {where} ｜ {what} ｜ {result}")
            written += 1
        lines.append("")
    lines.append("## 空段（有正文、无事件）")
    lines.append("")
    if gaps:
        for a, b in gaps:
            lines.append(f"- 第 {a}–{b} 章" if a != b else f"- 第 {a} 章")
    else:
        lines.append("- 无：1–%d 章每章至少一条事件。" % max_chapter)
    lines.append("")
    lines.append("## 按章截取接口（续写时用）")
    lines.append("")
    lines.append("```")
    lines.append("GET /api/projects/{project_id}/kg-v3/story-context"
                 "?novel_id={novel_id}&up_to_chapter={N}")
    lines.append("→ {coverage, recent_events, open_foreshadows, characters}")
    lines.append("")
    lines.append("GET /api/projects/{project_id}/kg-v3/timeline"
                 "?novel_id={novel_id}&from_chapter=1&to_chapter={N}&include_gaps=true")
    lines.append("GET /api/projects/{project_id}/kg-v3/worldview?novel_id={novel_id}")
    lines.append("```")
    lines.append("")
    lines.append("story-context 返回字段（续写直接消费）：")
    lines.append("")
    lines.append("```json")
    lines.append('{"novel_id": 7, "up_to_chapter": N,')
    lines.append(' "coverage": {"events_before": 0, "chapters_with_events": 0,')
    lines.append('              "coverage_ratio": 0.0, "gaps": [[起, 止], ...]},')
    lines.append(' "recent_events": [{"chapter", "event_type", "who", "where", "what", "result"}],')
    lines.append(' "open_foreshadows": [{"name", "status", "planted_chapter",')
    lines.append('                      "planned_payoff_chapter", "age_chapters",')
    lines.append('                      "description"}],')
    lines.append(' "characters": [{"name", "alive", "location", "location_source",')
    lines.append('                 "goal", "mood", "last_seen_chapter", "last_chapter",')
    lines.append('                 "mention_count", "recent": [{"chapter", "summary", "where"}]}]}')
    lines.append("```")
    lines.append("")
    lines.append("- `coverage.gaps` 为空说明「此前每章都有事件」；非空时列出的章"
                 "是 KG 盲区，续写前建议回读原文。")
    lines.append("- `location_source=latest_event` 表示该人物没有显式状态变更记录，"
                 "位置取自其最后一条事件。")
    path.write_text("\n".join(lines), encoding="utf-8")
    return written


def _gaps(db, novel_id: int, max_chapter: int) -> list[list[int]]:
    corpus = {c for (c,) in db.execute(
        select(func.distinct(NovelChunk.chapter_no)).where(
            NovelChunk.novel_id == novel_id,
            NovelChunk.chapter_no <= max_chapter))}
    covered = {c for (c,) in db.execute(
        select(func.distinct(KgEvent.chapter)).where(
            KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None),
            KgEvent.is_current.is_(True),
            KgEvent.chapter <= max_chapter))}
    gaps: list[list[int]] = []
    start = prev = None
    for ch in sorted(corpus - covered):
        if start is None:
            start = prev = ch
        elif prev is not None and ch == prev + 1:
            prev = ch
        else:
            gaps.append([start, prev])
            start = prev = ch
    if start is not None:
        gaps.append([start, prev])
    return gaps


def export(novel_id: int, project_id: int, out_dir: pathlib.Path, title: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        world = worldview(db, project_id, novel_id)
        max_chapter = db.scalar(select(func.max(KgEvent.chapter)).where(
            KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None),
            KgEvent.is_current.is_(True))) or 0
        corpus_max = db.scalar(select(func.max(NovelChunk.chapter_no)).where(
            NovelChunk.novel_id == novel_id)) or 0
        max_chapter = max(max_chapter, 0)
        gaps = _gaps(db, novel_id, corpus_max)
        kinds = Counter(k for (k,) in db.execute(
            select(KgEntity.kind).where(
                KgEntity.project_id == project_id, KgEntity.novel_id == novel_id)))
        n_events = db.scalar(select(func.count(KgEvent.id)).where(
            KgEvent.novel_id == novel_id, KgEvent.is_current.is_(True))) or 0
        n_rel = db.scalar(select(func.count(KgRelation.id)).where(
            KgRelation.project_id == project_id,
            KgRelation.is_current.is_(True))) or 0

        _write_worldview(out_dir / f"{title}-世界观清单.md", title, world)
        written = _write_timeline(out_dir / f"{title}-剧情时间线.md", title,
                                  novel_id, project_id, db, corpus_max, gaps)
        # 机器可读：世界观全量 + 时间线全量（分页拼接）
        (out_dir / f"{title}-世界观.json").write_text(
            json.dumps(world, ensure_ascii=False, indent=1), encoding="utf-8")
        events: list[dict] = []
        for start in range(1, corpus_max + 1, BUCKET):
            page = timeline(db, project_id, novel_id, from_chapter=start,
                            to_chapter=min(corpus_max, start + BUCKET - 1),
                            limit=2000)
            events.extend(page["events"])
        (out_dir / f"{title}-剧情时间线.json").write_text(
            json.dumps({"novel_id": novel_id, "events": events, "gaps": gaps},
                       ensure_ascii=False, indent=1), encoding="utf-8")

    return {
        "novel_id": novel_id,
        "corpus_chapters": corpus_max,
        "events": n_events,
        "relations": n_rel,
        "timeline_lines": written,
        "entity_kinds": dict(kinds),
        "gaps": len(gaps),
        "out_dir": str(out_dir),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="KG → 世界观清单 + 剧情时间线")
    ap.add_argument("--novel-id", type=int, required=True)
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--out-dir", type=pathlib.Path, required=True)
    ap.add_argument("--title", required=True)
    args = ap.parse_args()
    print(json.dumps(export(args.novel_id, args.project_id, args.out_dir, args.title),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
