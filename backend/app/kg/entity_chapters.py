# -*- coding: utf-8 -*-
"""实体章号回填：``kg_entities.first_chapter`` / ``last_chapter`` 只由可确定的章号聚合。

口径（严格，无最近邻兜底）：

1. **语料路径**：``kg_mentions.generation_id IS NULL`` 且 ``novel_id`` 非空
   → 章号 = ``segment_no``。依据是 ``corpus_ingest`` 的写入约定：语料摄取直接
   用章号当段号（见该模块开头的「用章号当 segment_no」）。
2. **生成路径**：``generation_id`` 非空 → 只认 ``generations.chapter_range`` 里
   显式的「第 N 章」（``第9章《大勇知道了》`` → 9）。``段1`` / ``段2`` 这类写法
   **不认**，也不做「往前找最近一个带章号的 generation」这种推断。
3. 两条都归不出 → **未知**：保持 NULL，读侧显示「章号未知」，任何路径都不回落
   到第 1 章。

为什么不猜：本书 412 个实体的章号历史上是 NULL。硬填成 1 会让切片声称「第 1 章
就出现了」，比未知更错——读侧将无法区分「真在第 1 章」与「我们不知道」。

用法（默认 dry-run，只读；``--apply`` 才写）::

    python -m app.kg.entity_chapters --db backend/novel_dev.db
    python -m app.kg.entity_chapters --db backend/novel_dev.db --apply

``--apply`` 只写与现值不同的行，并输出漂移清单与实际写入行数；未知的实体不写。
"""
from __future__ import annotations

import argparse
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


@dataclass
class EntityPlan:
    """一个实体的章号回填计划（现值 → 新值）。"""

    entity_id: int
    project_id: int
    novel_id: int | None
    kind: str
    name: str
    current_first: int | None
    current_last: int | None
    new_first: int | None
    new_last: int | None
    corpus_mentions: int = 0
    generation_mentions: int = 0
    unknown_mentions: int = 0

    @property
    def resolved(self) -> bool:
        """是否聚合出可确定的章号区间。"""
        return self.new_first is not None and self.new_last is not None

    @property
    def drift(self) -> bool:
        """是否与现值不同（只有这些行会被 ``--apply`` 写入）。"""
        return (
            self.resolved
            and (self.new_first != self.current_first or self.new_last != self.current_last)
        )

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "project_id": self.project_id,
            "novel_id": self.novel_id,
            "kind": self.kind,
            "name": self.name,
            "current": [self.current_first, self.current_last],
            "new": [self.new_first, self.new_last],
            "corpus_mentions": self.corpus_mentions,
            "generation_mentions": self.generation_mentions,
            "unknown_mentions": self.unknown_mentions,
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


def _corpus_bounds(db) -> dict[int, tuple[int, int, int]]:
    """语料路径：entity_id → (min_chapter, max_chapter, mention 数)。

    只认 ``generation_id IS NULL`` 且 ``novel_id`` 非空的提及；``segment_no``
    为空的提及无法定章，跳过（不猜）。
    """
    rows = db.execute(text(
        "SELECT entity_id, MIN(segment_no), MAX(segment_no), COUNT(*)"
        " FROM kg_mentions"
        " WHERE generation_id IS NULL AND novel_id IS NOT NULL"
        "   AND segment_no IS NOT NULL"
        " GROUP BY entity_id"
    )).fetchall()
    return {int(r[0]): (int(r[1]), int(r[2]), int(r[3])) for r in rows}


def _generation_bounds(db, result: PlanResult) -> dict[int, tuple[int, int, int, int]]:
    """生成路径：entity_id → (min_chapter, max_chapter, 有标签数, 无标签数)。

    ``chapter_range`` 只认显式「第 N 章」；``段N`` 计入无标签，不推断。
    """
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
    """算出每个实体的章号区间计划（纯读，不写库）。"""
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
        c_min, c_max, c_cnt = corpus.get(eid, (None, None, 0))
        g_min, g_max, g_cnt, g_bad = generation.get(eid, (None, None, 0, 0))
        firsts = [x for x in (c_min, g_min) if x is not None]
        lasts = [x for x in (c_max, g_max) if x is not None]
        result.plans.append(EntityPlan(
            entity_id=eid,
            project_id=int(pid),
            novel_id=None if novel_id is None else int(novel_id),
            kind=str(kind),
            name=str(name),
            current_first=None if cur_first is None else int(cur_first),
            current_last=None if cur_last is None else int(cur_last),
            new_first=min(firsts) if firsts else None,
            new_last=max(lasts) if lasts else None,
            corpus_mentions=c_cnt,
            generation_mentions=g_cnt,
            unknown_mentions=g_bad,
        ))
    return result


def summarize(result: PlanResult) -> dict:
    """dry-run 汇总：可填 / 未知 / 漂移的分布，按项目拆开。"""
    per_project: dict[int, dict] = {}
    for p in result.plans:
        slot = per_project.setdefault(p.project_id, {
            "entities": 0, "resolved": 0, "unknown": 0,
            "null_chapter_before": 0, "drift": 0,
        })
        slot["entities"] += 1
        if p.resolved:
            slot["resolved"] += 1
        else:
            slot["unknown"] += 1
        if p.current_first is None or p.current_last is None:
            slot["null_chapter_before"] += 1
        if p.drift:
            slot["drift"] += 1
    unknown = [p for p in result.plans if not p.resolved]
    return {
        "entities": len(result.plans),
        "resolved": sum(1 for p in result.plans if p.resolved),
        "unknown": len(unknown),
        "null_chapter_before": sum(
            1 for p in result.plans if p.current_first is None or p.current_last is None
        ),
        "fillable_now": sum(
            1 for p in result.plans
            if p.resolved and (p.current_first is None or p.current_last is None)
        ),
        "already_consistent": sum(1 for p in result.plans if p.resolved and not p.drift),
        "drift": sum(1 for p in result.plans if p.drift),
        "per_project": dict(sorted(per_project.items())),
        "unknown_sample": [
            {"entity_id": p.entity_id, "project_id": p.project_id,
             "novel_id": p.novel_id, "kind": p.kind, "name": p.name,
             "corpus_mentions": p.corpus_mentions,
             "generation_mentions": p.generation_mentions,
             "unknown_mentions": p.unknown_mentions}
            for p in unknown[:50]
        ],
        "orphan_mentions": result.orphan_mentions,
        "unlabeled_generation_mentions": result.unlabeled_generation_mentions,
        "dangling_generation_mentions": result.dangling_generation_mentions,
    }


def apply_plan(db, result: PlanResult) -> int:
    """只写与现值不同的行；返回实际写入的实体行数。"""
    rows = [p for p in result.plans if p.drift]
    for p in rows:
        db.execute(text(
            "UPDATE kg_entities SET first_chapter = :nf, last_chapter = :nl"
            " WHERE id = :eid"
        ), {"nf": p.new_first, "nl": p.new_last, "eid": p.entity_id})
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
    ap.add_argument("--limit", type=int, default=40, help="漂移清单打印条数")
    ap.add_argument("--json-out", help="把完整报告写到该路径")
    args = ap.parse_args(argv)

    if args.db:
        eng: Engine | None = _make_engine(args.db)
        session_factory = lambda: Session(eng)  # noqa: E731
    else:
        from app.db import SessionLocal

        eng = None
        session_factory = SessionLocal

    db = session_factory()
    try:
        result = build_plan(db, project_id=args.project)
        report = summarize(result)
        report["mode"] = "apply" if args.apply else "dry-run"
        drift = [p.as_dict() for p in result.plans if p.drift]
        report["drift_rows"] = drift
        written = 0
        if args.apply:
            written = apply_plan(db, result)
            db.commit()
        report["written"] = written
        swiped = report["drift_rows"][: args.limit]
        print(f"[{report['mode']}] 实体 {report['entities']} 个："
              f"可定章 {report['resolved']}，未知 {report['unknown']}；"
              f"章号 NULL {report['null_chapter_before']}，可填 {report['fillable_now']}；"
              f"漂移 {report['drift']}，已写 {written}")
        for row in swiped:
            print(f"  #{row['entity_id']} p{row['project_id']} book={row['novel_id']} "
                  f"{row['kind']}/{row['name']}: {row['current']} → {row['new']}")
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1)
            print(f"报告已写：{args.json_out}")
    finally:
        db.close()
        if eng is not None:
            eng.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
