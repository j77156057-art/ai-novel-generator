# -*- coding: utf-8 -*-
"""实体章号回填：``kg_entities.first_chapter`` / ``last_chapter`` 只由可确定的章号聚合。

口径（严格，无最近邻兜底）：

1. **章号空间按实体所属书切分**：``novel_id`` 非空的实体只用**该书语料**的
   mention（``generation_id IS NULL`` 且 ``novel_id`` 相同），章号 = ``segment_no``
   —— 依据 ``corpus_ingest`` 用章号当段号的写入约定。本项目自己生成的内容是
   另一个章号空间（``第1章`` 是项目正文第 1 章，不是那本书的第 1 章），
   **不吸收**。
2. **项目级实体**（``novel_id IS NULL``）只认生成路径：``generations.chapter_range``
   里显式的「第 N 章」（``第9章《大勇知道了》`` → 9）。``段1`` / ``段2`` 这类写法
   **不认**，也不做「往前找最近一个带章号的 generation」这种推断。
3. 归不出 → **未知**：保持 NULL，读侧显示「章号未知」，任何路径都不回落到第 1 章。

写入口径（2026-11-17 拍板）：**只填 NULL，不覆盖已有非 NULL**。
历史非 NULL 值与当前 mention 聚合不一致的有数千条，根因是 ``persist_window``
每轮整窗重写 mention，底数本身会丢信息；拿它去覆盖现有值（尤其收窄）不可逆，
会让生成侧误判「角色还没出场」而重新介绍。这类不一致只计数、只落漂移清单，
**不当成已修复**。

用法（默认 dry-run，只读；``--apply`` 才写）::

    python -m app.kg.entity_chapters --db backend/novel_dev.db --drift-out reports/kg-chapter-range-drift.csv
    python -m app.kg.entity_chapters --db backend/novel_dev.db --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

#: 生成路径唯一认可的章号写法。``段1`` / ``第 1 段`` / 纯数字都不匹配。
_CHAPTER_LABEL = re.compile(r"第\s*(\d+)\s*章")


def chapter_of_label(label: str | None) -> int | None:
    """从 ``generations.chapter_range`` 里取显式章号；取不到返回 ``None``。

    只认「第 N 章」：``第9章《大勇知道了》`` → 9；``段1`` / ``44-46`` / ``""``
    → ``None``（未知），不做任何就近推断。
    """
    if not label:
        return None
    m = _CHAPTER_LABEL.search(str(label))
    if not m:
        return None
    n = int(m.group(1))
    return n if n >= 1 else None


def format_chapter_range(first: int | None, last: int | None) -> str:
    """把章号区间渲染成读侧唯一两种形态：「第a–b章」或「章号未知」。

    绝不返回 ``None`` / ``""``：调用方（切片、人物列表、前端状态条）拿到的
    永远是可直接显示的字符串，避免「未知」在各处被各自渲染成空、0 或第 1 章。
    """
    if first is None or last is None:
        return "章号未知"
    return f"第{first}章" if first == last else f"第{first}–{last}章"


@dataclass
class EntityPlan:
    """一个实体的章号计划：现值、mention 重算值、待填值、漂移方向。"""

    entity_id: int
    project_id: int
    novel_id: int | None
    kind: str
    name: str
    current_first: int | None
    current_last: int | None
    recomputed_first: int | None
    recomputed_last: int | None
    corpus_mentions: int = 0
    generation_mentions: int = 0
    unknown_mentions: int = 0
    ignored_generation_mentions: int = 0

    @property
    def resolved(self) -> bool:
        """mention 聚合是否给出可确定区间。"""
        return self.recomputed_first is not None and self.recomputed_last is not None

    @property
    def fill_first(self) -> int | None:
        """该写的 first：现有为 NULL 才填，非 NULL 一律不动。"""
        return self.current_first if self.current_first is not None else self.recomputed_first

    @property
    def fill_last(self) -> int | None:
        return self.current_last if self.current_last is not None else self.recomputed_last

    @property
    def fillable(self) -> bool:
        """是否有 NULL 列可以填（``--apply`` 只写这些行）。"""
        return self.resolved and (self.current_first is None or self.current_last is None)

    @property
    def drift(self) -> bool:
        """已有值且与 mention 聚合不一致。只计数、不覆盖。"""
        return (
            self.resolved
            and self.current_first is not None
            and self.current_last is not None
            and (self.recomputed_first != self.current_first
                 or self.recomputed_last != self.current_last)
        )

    @property
    def drift_direction(self) -> str:
        if not self.drift:
            return ""
        narrow = (self.recomputed_first > self.current_first) or (
            self.recomputed_last < self.current_last
        )
        extend = (self.recomputed_first < self.current_first) or (
            self.recomputed_last > self.current_last
        )
        if narrow and extend:
            return "both"
        return "narrow" if narrow else "extend"

    def drift_row(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "project_id": self.project_id,
            "novel_id": "" if self.novel_id is None else self.novel_id,
            "kind": self.kind,
            "name": self.name,
            "current_first": self.current_first,
            "current_last": self.current_last,
            "recomputed_first": self.recomputed_first,
            "recomputed_last": self.recomputed_last,
            "direction": self.drift_direction,
            "corpus_mentions": self.corpus_mentions,
            "generation_mentions": self.generation_mentions,
            "unknown_mentions": self.unknown_mentions,
        }

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "project_id": self.project_id,
            "novel_id": self.novel_id,
            "kind": self.kind,
            "name": self.name,
            "current": [self.current_first, self.current_last],
            "recomputed": [self.recomputed_first, self.recomputed_last],
            "fill": [self.fill_first, self.fill_last] if self.fillable else None,
            "direction": self.drift_direction,
        }


@dataclass
class PlanResult:
    plans: list[EntityPlan] = field(default_factory=list)
    #: 无法归属书的 mention 数（``novel_id IS NULL`` 且无 generation）
    orphan_mentions: int = 0
    #: 生成路径里章号标签不可用（``段N`` 等）的 mention 数
    unlabeled_generation_mentions: int = 0
    #: 生成路径 mention 指向的 generation 行已不存在（脏引用）
    dangling_generation_mentions: int = 0
    #: 带书实体名下、被「章号空间隔离」挡掉的生成路径 mention 数
    ignored_generation_mentions: int = 0


def _corpus_bounds(db) -> dict[tuple[int, int], tuple[int, int, int]]:
    """语料路径：(entity_id, novel_id) → (min, max, mention 数)。

    按书拆键，避免把 A 书实体的章号空间和 B 书 mention 混在一起。
    """
    rows = db.execute(text(
        "SELECT entity_id, novel_id, MIN(segment_no), MAX(segment_no), COUNT(*)"
        " FROM kg_mentions"
        " WHERE generation_id IS NULL AND novel_id IS NOT NULL"
        "   AND segment_no IS NOT NULL"
        " GROUP BY entity_id, novel_id"
    )).fetchall()
    return {
        (int(r[0]), int(r[1])): (int(r[2]), int(r[3]), int(r[4])) for r in rows
    }


def _generation_bounds(db, result: PlanResult) -> dict[int, tuple[int, int, int, int]]:
    """生成路径：entity_id → (min, max, 有标签数, 无标签数)。"""
    rows = db.execute(text(
        "SELECT m.entity_id, g.chapter_range, COUNT(*)"
        " FROM kg_mentions m"
        " LEFT JOIN generations g ON g.id = m.generation_id"
        " WHERE m.generation_id IS NOT NULL"
        " GROUP BY m.entity_id, g.chapter_range"
    )).fetchall()
    acc: dict[int, list[int]] = {}
    for entity_id, label, cnt in rows:
        cnt = int(cnt)
        slot = acc.setdefault(int(entity_id), [None, None, 0, 0])
        chapter = chapter_of_label(label)
        if chapter is None:
            slot[3] += cnt
            result.unlabeled_generation_mentions += cnt
            if label is None:
                result.dangling_generation_mentions += cnt
            continue
        slot[0] = chapter if slot[0] is None else min(slot[0], chapter)
        slot[1] = chapter if slot[1] is None else max(slot[1], chapter)
        slot[2] += cnt
    return {k: tuple(v) for k, v in acc.items()}  # type: ignore[misc]


def build_plan(db, project_id: int | None = None) -> PlanResult:
    """算出每个实体的章号计划（纯读，不写库）。"""
    result = PlanResult()
    result.orphan_mentions = int(db.execute(text(
        "SELECT COUNT(*) FROM kg_mentions"
        " WHERE generation_id IS NULL AND novel_id IS NULL"
    )).scalar() or 0)

    corpus = _corpus_bounds(db)
    generation = _generation_bounds(db, result)

    where = "" if project_id is None else " WHERE project_id = :pid"
    params = {} if project_id is None else {"pid": project_id}
    entities = db.execute(text(
        "SELECT id, project_id, novel_id, kind, canonical_name,"
        " first_chapter, last_chapter FROM kg_entities" + where
    ), params).fetchall()

    for eid, pid, novel_id, kind, name, cur_first, cur_last in entities:
        eid = int(eid)
        book = None if novel_id is None else int(novel_id)
        c_min = c_max = None
        c_cnt = 0
        g_min = g_max = None
        g_cnt = g_bad = 0
        ignored = 0
        if book is not None:
            # 章号空间隔离：带书实体只吃本书语料；生成路径的章号属于项目正文，
            # 混进来会让「手机」因为项目自写第 1 章而声称在书里第 1 章就出现。
            c_min, c_max, c_cnt = corpus.get((eid, book), (None, None, 0))
            g_raw = generation.get(eid)
            if g_raw:
                g_cnt, g_bad = g_raw[2], g_raw[3]
                ignored = g_cnt + g_bad
                result.ignored_generation_mentions += ignored
        else:
            g_min, g_max, g_cnt, g_bad = generation.get(eid, (None, None, 0, 0))
        firsts = [x for x in (c_min, g_min) if x is not None]
        lasts = [x for x in (c_max, g_max) if x is not None]
        result.plans.append(EntityPlan(
            entity_id=eid,
            project_id=int(pid),
            novel_id=book,
            kind=str(kind),
            name=str(name),
            current_first=None if cur_first is None else int(cur_first),
            current_last=None if cur_last is None else int(cur_last),
            recomputed_first=min(firsts) if firsts else None,
            recomputed_last=max(lasts) if lasts else None,
            corpus_mentions=c_cnt,
            generation_mentions=g_cnt,
            unknown_mentions=g_bad,
            ignored_generation_mentions=ignored,
        ))
    return result


def summarize(result: PlanResult) -> dict:
    """dry-run 汇总：可填 / 未知 / 漂移的分布，按项目拆开。"""
    per_project: dict[int, dict] = {}
    for p in result.plans:
        slot = per_project.setdefault(p.project_id, {
            "entities": 0, "resolved": 0, "unknown": 0,
            "null_chapter_before": 0, "fillable": 0, "drift": 0,
        })
        slot["entities"] += 1
        slot["resolved"] += 1 if p.resolved else 0
        slot["unknown"] += 0 if p.resolved else 1
        slot["null_chapter_before"] += (
            1 if p.current_first is None or p.current_last is None else 0
        )
        slot["fillable"] += 1 if p.fillable else 0
        slot["drift"] += 1 if p.drift else 0
    directions = {"narrow": 0, "extend": 0, "both": 0}
    for p in result.plans:
        if p.drift:
            directions[p.drift_direction] += 1
    return {
        "entities": len(result.plans),
        "resolved": sum(1 for p in result.plans if p.resolved),
        "unknown": sum(1 for p in result.plans if not p.resolved),
        "null_chapter_before": sum(
            1 for p in result.plans if p.current_first is None or p.current_last is None
        ),
        "fillable_now": sum(1 for p in result.plans if p.fillable),
        "already_consistent": sum(
            1 for p in result.plans if p.resolved and not p.drift and not p.fillable
        ),
        "drift": sum(1 for p in result.plans if p.drift),
        "drift_directions": directions,
        "per_project": dict(sorted(per_project.items())),
        "orphan_mentions": result.orphan_mentions,
        "unlabeled_generation_mentions": result.unlabeled_generation_mentions,
        "dangling_generation_mentions": result.dangling_generation_mentions,
        "ignored_generation_mentions": result.ignored_generation_mentions,
    }


def apply_plan(db, result: PlanResult) -> int:
    """只填 NULL 列；返回实际写入的实体行数（已有非 NULL 的行永不进这里）。"""
    rows = [p for p in result.plans if p.fillable]
    for p in rows:
        sets = []
        params: dict = {"eid": p.entity_id}
        if p.current_first is None:
            sets.append("first_chapter = :nf")
            params["nf"] = p.fill_first
        if p.current_last is None:
            sets.append("last_chapter = :nl")
            params["nl"] = p.fill_last
        if not sets:
            continue
        db.execute(text(
            f"UPDATE kg_entities SET {', '.join(sets)} WHERE id = :eid"
        ), params)
    return len(rows)


def chapter_range_drift_count(db, project_id: int | None = None) -> int:
    """与当前 mention 聚合不一致的实体数（含收窄/扩张，**未被修复**）。"""
    return sum(1 for p in build_plan(db, project_id).plans if p.drift)


def write_drift_csv(plans: list[EntityPlan], path: str) -> int:
    """把漂移明细落成 CSV（按实体可查），返回条数。"""
    rows = [p.drift_row() for p in plans if p.drift]
    import os

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "entity_id", "project_id", "novel_id", "kind", "name",
            "current_first", "current_last", "recomputed_first", "recomputed_last",
            "direction", "corpus_mentions", "generation_mentions", "unknown_mentions",
        ])
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _make_engine(db_path: str):
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(eng, "connect")
    def _busy(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return eng


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="实体章号严格聚合回填（默认 dry-run）")
    ap.add_argument("--db", help="sqlite 库路径；缺省用 settings.database_url")
    ap.add_argument("--project", type=int, help="只处理某个项目")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只读）")
    ap.add_argument("--limit", type=int, default=20, help="漂移清单打印条数")
    ap.add_argument("--json-out", help="把汇总写到该 JSON 路径")
    ap.add_argument("--drift-out", help="把漂移明细落成 CSV（按实体可查）")
    args = ap.parse_args(argv)

    eng: Engine | None = None
    if args.db:
        eng = _make_engine(args.db)
        session_factory = lambda: Session(eng)  # noqa: E731
    else:
        from app.db import SessionLocal

        session_factory = SessionLocal

    db = session_factory()
    try:
        result = build_plan(db, project_id=args.project)
        report = summarize(result)
        report["mode"] = "apply" if args.apply else "dry-run"
        drift_rows = [p.drift_row() for p in result.plans if p.drift]
        report["drift_rows"] = drift_rows
        if args.drift_out:
            report["drift_file"] = args.drift_out
            report["drift_file_rows"] = write_drift_csv(result.plans, args.drift_out)
        written = 0
        if args.apply:
            written = apply_plan(db, result)
            db.commit()
        report["written"] = written
        print(f"[{report['mode']}] 实体 {report['entities']} 个："
              f"可定章 {report['resolved']}，未知 {report['unknown']}；"
              f"章号 NULL {report['null_chapter_before']}，可填 {report['fillable_now']}；"
              f"漂移 {report['drift']}（{report['drift_directions']}）；已写 {written}")
        for row in drift_rows[: args.limit]:
            print(f"  #{row['entity_id']} p{row['project_id']} book={row['novel_id']} "
                  f"{row['kind']}/{row['name']}: "
                  f"[{row['current_first']},{row['current_last']}] → "
                  f"[{row['recomputed_first']},{row['recomputed_last']}] "
                  f"{row['direction']}")
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            print(f"汇总已写：{args.json_out}")
    finally:
        db.close()
        if eng is not None:
            eng.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
