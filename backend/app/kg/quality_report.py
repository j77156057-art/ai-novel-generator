# -*- coding: utf-8 -*-
"""KG 每 5 章质量表：与人工核对时同一套口径，随章节批抽落库自动生成。

为什么有这个模块：2026-11-14 之前这张表全靠监控线的人（agent）在对话里
手工跑临时脚本汇总，代码里**没有**任何生成/落盘路径——人一停，表就断（第
17–32 章就是这么缺的）。这里把口径固化成代码，批抽收尾时自动刷新对应块，
不再依赖谁记得跑。

口径（与人工表逐项一致）：
- 事件：``kg_events.segment_no`` 落在该章覆盖段、且 ``COALESCE(novel_id,-1)``
  在本书 scope（原创项目 = NULL 行）
- 版本：``is_current=1`` 的 version 去重个数（应恰好 1）
- 章归属错：当前版事件里 ``chapter <> 本章`` 的条数（应 0）
- 并批归属疑点：一个批覆盖 ≥2 章时，本批落的关系 ``since_chapter`` 全落在
  最早章 → 报「chX 吸附 N 条（与 chY 并批）」。关系表没有段号，旧实现取批内
  最小章，后几章的关系会被前章「吸附」；这条只做可见性，不改数据
- 角色状态：同段、``created_at >= 2026-11-13``（v2.6 旧行不计）
- 关系：``since_chapter = 本章`` 且两端实体都在本书 scope
- mentions：本书 scope 的提及（总/当前）、脏行（mention 在本书但指向别书实体）
- run：该段最新 ``kg_batch_extract`` 行

输出：``reports/kg-quality-report.md``，按「块」写 section（HTML 注释做幂等
标记），同一块重复触发只替换不追加。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

#: 报告落盘位置（项目根 / reports / kg-quality-report.md）
_REPORT_PATH = Path(__file__).resolve().parents[3] / "reports" / "kg-quality-report.md"
#: 角色状态只统计 v3.0 之后写入的行（与人工表口径一致）
_STATE_SINCE = "2026-11-13"

#: 报告里的块标记（成对出现）：<!-- kg-quality:p6:ch31-ch35:start --> … :end -->
_QUALITY_BLOCK_RE = re.compile(
    r"<!-- kg-quality:(?P<key>p\d+:ch\d+-ch\d+):start -->"
    r".*?"
    r"<!-- kg-quality:(?P=key):end -->\n*",
    re.DOTALL,
)


def _drop_contained_blocks(content: str, project_id: int,
                           ch_from: int, ch_to: int, keep: str) -> str:
    """删掉被新区间完整覆盖的同项目旧块（key 不同）。

    为什么需要：尾块区间会随章数增长（31–32 → 31–35），旧 key 不再被精确
    命中，「同 key 覆盖」拦不住它——不清理的话每个增长的尾块都会留下一个
    过期副本（实测 2026-11-15 ch31-ch32 与 ch31-ch35 并存）。
    """
    prefix = f"p{project_id}:"
    removed = False
    out: list[str] = []
    last = 0
    for m in _QUALITY_BLOCK_RE.finditer(content):
        key = m.group("key")
        if key == keep or not key.startswith(prefix):
            continue
        a, b = (int(x) for x in key[len(prefix) + 2:].split("-ch"))
        if ch_from <= a and b <= ch_to:
            out.append(content[last:m.start()])
            last = m.end()
            removed = True
    if not removed:
        return content
    out.append(content[last:])
    return re.sub(r"\n{3,}", "\n\n", "".join(out))


def _chapter_index(label: str | None) -> int | None:
    if not label:
        return None
    m = re.search(r"第\s*(\d+)\s*章", label) or re.search(r"(\d+)\s*章", label)
    return int(m.group(1)) if m else None


def _seg_chapter_map(db: Session, project_id: int) -> dict[int, int]:
    """段号 → 章号。优先用 chapter_range 里的「第 N 章」，无标签的段按邻近段归属。"""
    rows = db.execute(text(
        "select segment_no, chapter_range from generations where project_id=:p "
        "order by segment_no, id"), {"p": project_id}).fetchall()
    labeled = []
    for seg, label in rows:
        ch = _chapter_index(label)
        if ch is not None:
            labeled.append((seg, ch))
    out: dict[int, int] = {}
    for seg, label in rows:
        ch = _chapter_index(label)
        if ch is not None:
            out[seg] = ch
            continue
        prev = nxt = None
        for s, c in labeled:
            if s < seg:
                prev = c
            elif s > seg and nxt is None:
                nxt = c
        if prev is not None or nxt is not None:
            out[seg] = prev if prev is not None else nxt
    return out


def _book_scope(db: Session, project_id: int) -> tuple[int | None, tuple[int, ...]]:
    row = db.execute(text(
        "select active_novel_id, continuation_novel_id from projects where id=:p"),
        {"p": project_id}).fetchone()
    if row is None:
        return None, (-1,)
    own = row[1] if row[1] is not None else row[0]
    return own, ((-1,) if own is None else (own, -1))


def build_rows(db: Session, project_id: int, ch_from: int, ch_to: int) -> list[dict]:
    """按章汇总四类产物（与人工五章表同列）。"""
    seg_ch = _seg_chapter_map(db, project_id)
    ch_segs: dict[int, list[int]] = {}
    for seg, ch in seg_ch.items():
        ch_segs.setdefault(ch, []).append(seg)
    _, scope = _book_scope(db, project_id)
    sq = ",".join(str(int(x)) for x in scope)

    # 该段最新一条批抽行
    run_rows = db.execute(text(
        "select id, status, note from agent_runs where role='kg_batch_extract' "
        "and project_id=:p order by id"), {"p": project_id}).fetchall()
    latest: dict[int, tuple[int, str]] = {}
    empty_by_seg: dict[int, list[int]] = {}
    for rid, status, note in run_rows:
        try:
            n = json.loads(note) if note else {}
        except (TypeError, ValueError):
            n = {}
        if isinstance(n, dict) and n.get("segment_no") is not None:
            seg = int(n["segment_no"])
            latest[seg] = (int(rid), status)
            # 批 success 但 events_added=0 且 entities>0：落库层看不出来的空事件
            if n.get("empty_events"):
                empty_by_seg.setdefault(seg, []).append(int(rid))

    out: list[dict] = []
    for ch in range(ch_from, ch_to + 1):
        segs = sorted(ch_segs.get(ch, []))
        row: dict = {"chapter": ch, "segments": segs}
        if not segs:
            row.update(events=(0, 0), versions=0, bad_chapter=0, states=(0, 0),
                       relations=(0, 0), mentions=(0, 0), dirty=0, runs=[],
                       empty_runs=[])
            out.append(row)
            continue
        q = ",".join(str(int(s)) for s in segs)
        ev = db.execute(text(
            f"select count(*), coalesce(sum(is_current),0), "
            f"count(distinct case when is_current=1 then version end), "
            f"coalesce(sum(case when is_current=1 and chapter<>:ch then 1 else 0 end),0) "
            f"from kg_events where project_id=:p and segment_no in ({q}) "
            f"and coalesce(novel_id,-1) in ({sq})"),
            {"ch": ch, "p": project_id}).fetchone()
        st = db.execute(text(
            f"select count(*), coalesce(sum(is_current),0), "
            f"count(distinct case when is_current=1 then version end) "
            f"from kg_character_state where project_id=:p and segment_no in ({q}) "
            f"and created_at >= :since"),
            {"p": project_id, "since": _STATE_SINCE}).fetchone()
        rel = db.execute(text(
            f"select count(*), coalesce(sum(r.is_current),0) from kg_relations r "
            f"where r.project_id=:p and r.since_chapter=:ch "
            f"and exists (select 1 from kg_entities e where e.id=r.from_id "
            f"  and coalesce(e.novel_id,-1) in ({sq})) "
            f"and exists (select 1 from kg_entities e where e.id=r.to_id "
            f"  and coalesce(e.novel_id,-1) in ({sq}))"),
            {"p": project_id, "ch": ch}).fetchone()
        men = db.execute(text(
            f"select coalesce(sum(case when coalesce(m.novel_id,-1) in ({sq}) then 1 else 0 end),0), "
            f"coalesce(sum(case when coalesce(m.novel_id,-1) in ({sq}) and m.is_current=1 then 1 else 0 end),0), "
            f"coalesce(sum(case when coalesce(m.novel_id,-1) in ({sq}) "
            f"  and coalesce(e.novel_id,-1) not in ({sq}) then 1 else 0 end),0) "
            f"from kg_mentions m left join kg_entities e on e.id=m.entity_id "
            f"where m.project_id=:p and m.segment_no in ({q})"),
            {"p": project_id}).fetchone()
        row.update(
            events=(ev[0], ev[1]), versions=ev[2], bad_chapter=ev[3],
            states=(st[0], st[1]), relations=(rel[0], rel[1]),
            mentions=(men[0], men[1]), dirty=men[2],
            runs=[latest.get(s) for s in segs],
            empty_runs=[rid for s in segs for rid in empty_by_seg.get(s, [])],
        )
        out.append(row)
    return out


def _batch_attr_suspects(db: Session, project_id: int, seg_ch: dict[int, int],
                         ch_from: int, ch_to: int) -> dict[int, list[str]]:
    """并批归属疑点：一个批覆盖 ≥2 章、且本批落的关系 since_chapter 落在最早章。

    批次识别只能靠 note 里的 ``claim_owner`` + ``finished_at``（note 没有 batch
    id；同一批的行 finished_at 完全一致）。关系没有段号，只能按写入时间窗
    归到批：``kg_relations.created_at`` 落在 ``[批起始, 批结束]``。同一关系
    可能落进相邻批的重叠窗口，按 finished_at 升序先到先得，避免重复计数。

    这里**只报告不改数据**：ch19/ch20 并批时 7 条关系全记到 ch19，正是该列
    此前看不见的串章；第 17 章之后的质量表需要这个回归信号。
    """
    rows = db.execute(text(
        "select id, status, started_at, finished_at, note from agent_runs "
        "where role='kg_batch_extract' and project_id=:p "
        "and finished_at is not null"),
        {"p": project_id}).fetchall()
    batches: dict[tuple[str, str], dict] = {}
    for _rid, status, started, finished, note in rows:
        if status not in ("success", "failed"):
            continue  # 在飞/未抽的批不出表，也不报疑点
        try:
            n = json.loads(note) if note else {}
        except (TypeError, ValueError):
            n = {}
        if not isinstance(n, dict):
            n = {}
        # 同批 = 同一认领者 + 同一 finished_at（毫秒级一致）
        key = (str(n.get("claim_owner") or ""), str(finished))
        b = batches.setdefault(key, {"segs": [], "start": started, "end": finished})
        if n.get("segment_no") is not None:
            b["segs"].append(int(n["segment_no"]))
        if started is not None and (b["start"] is None or str(started) < str(b["start"])):
            b["start"] = started

    suspects: dict[int, list[str]] = {}
    assigned: set[int] = set()
    for _key, b in sorted(batches.items(), key=lambda kv: str(kv[1]["end"])):
        chs = {seg_ch.get(s) for s in b["segs"]}
        if None in chs or len(chs) < 2:
            continue
        anchor = min(chs)
        rel_ids = [int(x[0]) for x in db.execute(text(
            "select id from kg_relations where project_id=:p and since_chapter=:ch "
            "and created_at>=:a and created_at<=:z"),
            {"p": project_id, "ch": anchor,
             "a": b["start"], "z": b["end"]}).fetchall()]
        hit = [i for i in rel_ids if i not in assigned]
        assigned.update(rel_ids)
        if not hit:
            continue
        others = "、".join(f"ch{c}" for c in sorted(chs - {anchor}))
        suspects.setdefault(anchor, []).append(
            f"ch{anchor} 吸附 {len(hit)} 条关系（与 {others} 并批）")
    return {ch: v for ch, v in suspects.items() if ch_from <= ch <= ch_to}


def _anomalies(rows: list[dict], suspects: dict[int, list[str]] | None = None) -> list[str]:
    bad: list[str] = []
    for r in rows:
        ch = r["chapter"]
        if not r["segments"]:
            bad.append(f"ch{ch}: 无段落")
            continue
        if sum(r["events"]) + sum(r["states"]) + sum(r["relations"]) + sum(r["mentions"]) == 0:
            bad.append(f"ch{ch}: 整章零产物")
        elif r["events"][0] == 0:
            # 有实体/提及但一条事件都没有 = 该章没有当前版事件，属「current 缺版」
            bad.append(f"ch{ch}: 零事件（无当前版）")
        if r["versions"] > 1:
            bad.append(f"ch{ch}: 多版本 current（{r['versions']}）")
        # 「success 但零事件」在状态列看不出来，必须单独报：实体抽到了却一条
        # 事件没落，读侧这条章等于没有剧情。只报可见，不改判定。
        for rid in r.get("empty_runs", []):
            bad.append(f"ch{ch}: run{rid} 空事件（success 但 events_added=0）")
        if r["bad_chapter"]:
            bad.append(f"ch{ch}: {r['bad_chapter']} 条事件落错章")
        for run in r["runs"]:
            if run and run[1] != "success":
                bad.append(f"ch{ch}: run{run[0]} 状态 {run[1]}")
        if suspects:
            bad.extend(suspects.get(ch, []))
    return bad


def render_block(db: Session, project_id: int, ch_from: int, ch_to: int) -> str:
    """渲染一个章区间的表（Markdown，含幂等标记）。"""
    rows = build_rows(db, project_id, ch_from, ch_to)
    # 报告文件是共享的（同一份），key 必须带项目号：否则别的项目跑出同号章节
    # 会覆盖本项目的块（实测出现过一次「第 1–5 章」空块顶掉本项目 section）。
    key = f"p{project_id}:ch{ch_from}-ch{ch_to}"
    lines = [
        f"<!-- kg-quality:{key}:start -->",
        f"## 第 {ch_from}–{ch_to} 章（项目 {project_id}）",
        "",
        f"_生成时间 {datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        f"（自动生成，口径见文件头）_",
        "",
        "| 章 | seg | 事件(总/当前) | 版本 | 章归属错 | 角色状态 | 关系 "
        "| mentions 本书/当前 | 脏行 | run |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        segs = ",".join(map(str, r["segments"])) or "—"
        runs = " ".join(
            f"run{x[0]}/{x[1]}" if x else "无run" for x in r["runs"]) or "—"
        lines.append(
            f"| ch{r['chapter']} | {segs} | {r['events'][0]}/{r['events'][1]} "
            f"| v{r['versions']} | {r['bad_chapter']} | {r['states'][0]}/{r['states'][1]} "
            f"| {r['relations'][0]}/{r['relations'][1]} "
            f"| {r['mentions'][0]}/{r['mentions'][1]} | {r['dirty']} | {runs} |")
    bad = _anomalies(rows, _batch_attr_suspects(
        db, project_id, _seg_chapter_map(db, project_id), ch_from, ch_to))
    lines += ["", f"**异常**：{'；'.join(bad) if bad else '无'}", "",
              f"<!-- kg-quality:{key}:end -->", ""]
    return "\n".join(lines)


def write_block(db: Session, project_id: int, ch_from: int, ch_to: int,
                path: Path | None = None,
                supersede_contained: bool = False) -> Path:
    """把一个区间的表写进报告文件（同 key 覆盖，不存在则追加）。

    ``supersede_contained=True``（自动触发路径用）：顺手删掉被本区间完整覆盖
    的旧块，防止尾块区间增长后旧副本累积。手工补录（``python -m``）保持旧行为，
    不动区间外的任何块。
    """
    target = path or _REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    block = render_block(db, project_id, ch_from, ch_to)
    key = f"p{project_id}:ch{ch_from}-ch{ch_to}"
    if target.exists():
        content = target.read_text(encoding="utf-8")
    else:
        content = (
            "# KG 质量表（每 5 章）\n\n"
            "> 口径：本书 scope（原创项目 = novel_id 为 NULL 的行）；"
            "事件按段落章归属、角色状态只计 v3.0 之后、mention 脏行 = "
            "本书提及指向别书实体。本文件由 `app/kg/quality_report.py` "
            "随批抽自动维护，同区间重复触发只替换不追加。\n\n")
    if supersede_contained:
        content = _drop_contained_blocks(content, project_id, ch_from, ch_to, keep=key)
    start = f"<!-- kg-quality:{key}:start -->"
    end = f"<!-- kg-quality:{key}:end -->"
    if start in content and end in content:
        head, rest = content.split(start, 1)
        _, tail = rest.split(end, 1)
        content = head + block + tail
    else:
        content = content.rstrip() + "\n\n" + block + "\n"
    target.write_text(content, encoding="utf-8")
    return target


def _latest_run_status(db: Session, project_id: int, segment_no: int) -> str | None:
    row = db.execute(text(
        "select status from agent_runs where role='kg_batch_extract' "
        "and project_id=:p and json_extract(note,'$.segment_no')=:s "
        "order by id desc limit 1"),
        {"p": project_id, "s": segment_no}).fetchone()
    return row[0] if row else None


def maybe_emit_after_batch(db: Session, project_id: int, chapters: set[int]) -> None:
    """批抽收尾调用：某章所在 5 章块的表在该块**所有段落都跑完**时刷新。

    判定口径是「每段最新一条批抽行都是 success/failed」：
    - 不以锚点章的 ``note.chapter`` 判定——历史行的章号与段号口径不一致
      （实测 ch20 命中一条 11-13 的旧行），会把在飞的批当成已完成；
    - 有 pending/running 就不出表，避免把在飞/未抽的章渲染成零产物。
    """
    if not chapters:
        return
    blocks = {(((ch - 1) // 5 + 1) * 5, ) for ch in chapters}
    for (end,) in sorted(blocks):
        start = max(1, end - 4)
        # 该块里任何一章找不到段落（旧稿「段N」标签、整章未生成）就不出表：
        # 否则会把「没有段落」渲染成零产物，误导读者。
        seg_ch = _seg_chapter_map(db, project_id)
        ch_segs: dict[int, list[int]] = {}
        for seg, ch in seg_ch.items():
            ch_segs.setdefault(ch, []).append(seg)
        if any(ch not in ch_segs for ch in range(start, end + 1)):
            continue
        segs = [s for ch in range(start, end + 1) for s in ch_segs[ch]]
        if any(_latest_run_status(db, project_id, s) not in ("success", "failed")
               for s in segs):
            continue
        write_block(db, project_id, start, end, supersede_contained=True)
        _log.info("kg 质量表已刷新：第 %s–%s 章 -> %s", start, end, _REPORT_PATH)


if __name__ == "__main__":  # 手工补录/重算：python -m app.kg.quality_report 17 32
    from app.db import SessionLocal

    lo = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    hi = int(sys.argv[2]) if len(sys.argv) > 2 else lo
    proj = int(sys.argv[3]) if len(sys.argv) > 3 else 6
    s = SessionLocal()
    try:
        out = write_block(s, proj, lo, hi)
        print(f"wrote {out}")
    finally:
        s.close()
