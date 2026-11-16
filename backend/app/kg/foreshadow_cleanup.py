"""伏笔回收章的**应用层**：证据从哪来、脏值怎么清。

## 三处应用（必须用同一把尺）

1. **落库**（`batch_worker._persist_extraction`）—— 新抽出的值过关才写；
2. **读取**（`kg/retrieval_v3.slice_for_prompt`）—— 老数据在送模型/切片时
   再过一遍，**不必等清理**就不会带脏值进提示词；
3. **存量清理**（本模块 `clean_project_foreshadows`）—— 一次性把脏值置空。

## 「尺」是什么

判据在 `app/kg/foreshadow_quality.plausible_payoff_chapter`：

1. 章号 ≥ 1（第 0 章不存在）；
2. 回收章晚于埋下章（**已回收的行例外**：同章埋了又收合法）；
3. **正文证据** —— 这个章号得在正文里真的出现过（「第 N 章」/「第 N 节」）。

## ⚠️ 为什么**不**用「回收章 ≤ 已写章数」当上界

想过加，量过之后**否掉了**：`planned_payoff_chapter` 是「**计划**回收章」——
作者对**将来**的规划。「埋@3 → 计划第 60 章」是**合法且有用**的路线图信息
（长篇里伏笔几十章后才收是常态）。用「已写章数」当上界会把这条功能杀掉：
用户才写 9 章就想规划第 60 章回收，也会被判成非法。

⇒ 上界改用**证据**判。实测（发布版冒烟库 140 条）：生成正文里「第 N 章」
**零命中**，参考书语料 151 处命中 ⇒ 阳性对照自证：匹配器能命中真章号，
所以生成正文的零命中是**真阴性**。所有脏值（`0`/`50`/`30`/`12`）在正文里
都没有任何证据。

## 状态敏感性：哪些行能清

| 状态 | 语义 | 处理 |
|---|---|---|
| `planted` / `developing` | 未回收 ⇒ 回收章必须晚于埋下章 | 照常判 |
| `paid_off` | 已回收 ⇒ 回收章记的是**当时的计划**，`pf == planted`（同章埋了又收）**合法** | 保留（只清 `pf=0`） |
| `dropped` | 已废弃、**从未回收** ⇒ 没有「同章回收」这层语义 | `pf=0` 是纯脏值 ⇒ 清 |

实测库里 #91（`pf=1/planted=1/paid_off`）、#43（`pf=4/planted=4/paid_off`）
都是合法同章回收 —— **一条都不许清**。

⚠️ `dropped` 与 `paid_off` **不能一锅端**（这是真机跑出来的）：
第一版规则写成「已回收/已丢弃都不清」，结果库里剩下 1 条 `pf=0`
—— id=46 是 `dropped` 状态。已废弃的线索没有「同章回收」这层语义，
`pf=0` 就是纯脏值（第 0 章不存在）。

⇒ 已收尾的行（`closed`）**只判「章号 ≥ 1」**，不拿现在的正文/埋下章
去重新裁决历史。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kg.foreshadow_quality import sanitize_payoff_chapter
from app.models import Generation, KgForeshadow

#: 已回收/已丢弃 —— 回收章记的是当时的计划，同章回收合法，**不清**。
SETTLED_STATUSES = ("paid_off", "dropped")


def project_written_text(db: Session, project_id: int) -> str:
    """这个项目已写正文的全文（章号证据用）。

    取不到就返回空串 —— 空串会让正文证据判据**不放行**任何章号
    （宁可置空，也不让凭空来的值进账）。
    """
    try:
        rows = db.scalars(
            select(Generation.text).where(
                Generation.project_id == project_id,
                Generation.text.isnot(None),
            ).order_by(Generation.segment_no)
        ).all()
    except Exception:  # noqa: BLE001 —— 取不到证据就当没有（不放行）
        return ""
    return "\n".join(str(r or "") for r in rows)


def clean_foreshadow_row(fs: KgForeshadow, *, text: str | None) -> bool:
    """把一行伏笔的回收章按判据清洗。**返回是否发生了修改**。

    - `pf` 合法 ⇒ 不动；
    - `pf` 非法 ⇒ 置 `None`（**当作未知**，不猜一个数补上）。

    ⚠️ **已收尾的行（`paid_off`/`dropped`）只清「`pf=0`」** ——
    回收章记的是**当时的计划**，是历史记录，不拿现在的正文去重新裁决它：
    - `paid_off`：`pf == planted`（同章埋了又收）合法 ⇒ 保留
      （实测 #91 `pf=1/planted=1`、#43 `pf=4/planted=4`）；
    - `dropped`：线索被废弃、从未回收，没有「同章回收」这层语义
      ⇒ `pf=0` 就是纯脏值（第 0 章不存在）⇒ 清。
    """
    if fs.planned_payoff_chapter is None:
        return False
    closed = fs.status in SETTLED_STATUSES
    kept = sanitize_payoff_chapter(
        fs.planned_payoff_chapter,
        fs.planted_chapter,
        text,
        closed=closed,
    )
    if kept == fs.planned_payoff_chapter:
        return False
    fs.planned_payoff_chapter = kept
    return True


def clean_project_foreshadows(db: Session, project_id: int) -> dict:
    """清理一个项目的存量脏回收章。返回统计（留痕用）。

    判据确定（数值不变式 + 正文证据）⇒ 零误伤；
    置空的方向是「不知道」⇒ 不可逆损失为零。
    """
    text = project_written_text(db, project_id)
    rows = list(db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project_id,
            KgForeshadow.planned_payoff_chapter.isnot(None),
        )
    ))
    changed = 0
    kept = 0
    for fs in rows:
        if clean_foreshadow_row(fs, text=text):
            changed += 1
        else:
            kept += 1
    if changed:
        db.commit()
        logging.getLogger(__name__).info(
            "伏笔回收章清理：项目 %s 共 %d 条有值，清掉脏值 %d 条、保留 %d 条",
            project_id, len(rows), changed, kept,
        )
    return {"total": len(rows), "cleaned": changed, "kept": kept}


def clean_all_foreshadows(db: Session) -> dict:
    """清理全库（启动时调用一次）。返回汇总统计。"""
    project_ids = [int(p) for p in db.scalars(
        select(KgForeshadow.project_id).distinct()
    ).all() if p is not None]
    total = cleaned = kept = 0
    for pid in project_ids:
        st = clean_project_foreshadows(db, pid)
        total += st["total"]
        cleaned += st["cleaned"]
        kept += st["kept"]
    if cleaned:
        logging.getLogger(__name__).info(
            "伏笔回收章存量清理：全库 %d 条有值，清掉 %d 条、保留 %d 条",
            total, cleaned, kept,
        )
    return {"projects": len(project_ids), "total": total,
            "cleaned": cleaned, "kept": kept}
