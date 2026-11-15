"""伏笔超期告警的「回收章净化」：说不通的计划回收章不该变成告警。

## 实测背景（main 侧 2026-10-29）

发布版冒烟库 140 条伏笔里：

- `planned_payoff_chapter = 0`：**84 条**（第 0 章不存在）；
- `50/30/12`：**24 条**（其中 18 条恰好等于抽取提示 JSON 示例的字面值 50 —— 模型照抄示例）。

旧判据只比大小 ⇒ `0 < current_chapter` 恒成立 ⇒ **84 条纯噪音告警**，
文案是「计划第 0 章回收，已超期 9 章仍未揭晓」。

本文件钉住：

1. 章号证据只认「第 N 章 / 第 N 节」，**不认「回」**（「第一回」是第一次）；
2. 数值不变式：章号 ≥ 1、必须晚于埋下章；`closed` 行只判「≥ 1」；
3. `check_foreshadow_expiry` 对 `pf=0` / 无正文证据的章号**不再告警**；
4. **对照**：同一批 84 条脏数据，旧判据会报 84 条，新判据报 0 条。
"""
from __future__ import annotations

import pytest


# ====================================================================
# 正文证据（kg/chapter_evidence.py）
# ====================================================================


def test_chinese_number_parsing():
    from app.kg.chapter_evidence import parse_chinese_number

    assert parse_chinese_number("十二") == 12
    assert parse_chinese_number("一百三十") == 130
    assert parse_chinese_number("三") == 3
    assert parse_chinese_number("十") == 10
    assert parse_chinese_number("二十") == 20
    assert parse_chinese_number("零") == 0
    assert parse_chinese_number("12") == 12
    assert parse_chinese_number("abc") is None


def test_mentioned_chapters_reads_chapters_not_rounds():
    """「第一回」是「第一次」，不是第 1 章 —— 不能给它背书。"""
    from app.kg.chapter_evidence import mentioned_chapters

    assert sorted(mentioned_chapters("（第一章。）他说，第三章再说")) == [1, 3]
    assert sorted(mentioned_chapters("第一百三十章")) == [130]
    assert mentioned_chapters("停过两回，第一回是傍晚，第二回是换防以后") == set()
    assert sorted(mentioned_chapters("第 12 章")) == [12]


def test_payoff_chapter_in_text():
    from app.kg.chapter_evidence import payoff_chapter_in_text

    assert payoff_chapter_in_text(1, "（第一章。）") is True
    assert payoff_chapter_in_text(12, "（第一章。）") is False
    assert payoff_chapter_in_text(2, "停过两回，第二回是换防以后") is False
    assert payoff_chapter_in_text("3", "第三章") is False  # 非 int 不认


# ====================================================================
# 回收章判据（kg/foreshadow_quality.py）
# ====================================================================


def test_plausible_payoff_chapter():
    from app.kg.foreshadow_quality import plausible_payoff_chapter

    assert plausible_payoff_chapter(0, 1) is False          # 第 0 章不存在
    assert plausible_payoff_chapter(None, 1) is False
    assert plausible_payoff_chapter(1, 1) is False          # 同章回收 = 没埋
    assert plausible_payoff_chapter(60, 1) is True          # 计划远景合法
    assert plausible_payoff_chapter(60, 1, text="（第一章。）") is False  # 无证据
    assert plausible_payoff_chapter(3, 1, text="（第三章。）") is True
    assert plausible_payoff_chapter(1, 1, text="（第一章。）") is False  # 同章回收 = 没埋
    assert plausible_payoff_chapter(1, 1, closed=True) is True   # 历史记录不重裁
    assert plausible_payoff_chapter(0, 1, closed=True) is False  # 但 0 仍非法


def test_sanitize_returns_none_for_unprovable_values():
    from app.kg.foreshadow_quality import sanitize_payoff_chapter

    assert sanitize_payoff_chapter(0, 1) is None
    assert sanitize_payoff_chapter(50, 1, text="") is None   # 取不到正文 ⇒ 不放行
    assert sanitize_payoff_chapter(3, 1, text="第三章") == 3


# ====================================================================
# judge：噪音是否真的消失
# ====================================================================


@pytest.fixture()
def db_session():
    from app.db import Base, SessionLocal, engine
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _seed_project(db, *, dirty: int = 84):
    from app.models import Generation, KgForeshadow, Project

    project = Project(title="伏笔净化项目", target_length=3000,
                      generation_length=1200, genre="都市异能")
    db.add(project)
    db.flush()
    # 已写 9 段正文：正文里**唯一**出现的章号是「第三章」
    for i in range(1, 10):
        db.add(Generation(project_id=project.id, segment_no=i, chapter_range=f"{i}-{i}",
                          text=("他在灶前坐了很久。" if i != 2 else "他打算第三章再问。")))
    # 84 条 pf=0 的脏行（与 main 实测一致）
    for i in range(dirty):
        db.add(KgForeshadow(project_id=project.id, name=f"脏线索{i}", status="planted",
                            planted_chapter=1, planned_payoff_chapter=0))
    # 一条有正文证据的真超期（计划第 3 章，已写到第 9 章）
    db.add(KgForeshadow(project_id=project.id, name="有证据的线索", status="planted",
                        planted_chapter=1, planned_payoff_chapter=3))
    # 一条「照抄示例」的 50（正文里没有第 50 章）
    db.add(KgForeshadow(project_id=project.id, name="照抄示例的线索", status="planted",
                        planted_chapter=1, planned_payoff_chapter=50))
    db.commit()
    return project


def test_expiry_warnings_drop_from_84_to_0_for_dirty_payoff(db_session):
    """对照：84 条 `pf=0` 旧判据全部告警，新判据一条都不报。"""
    from app.kg.judge import check_foreshadow_expiry

    project = _seed_project(db_session)
    findings = check_foreshadow_expiry(db_session, project.id, text="", segment_no=9,
                                       current_chapter=9)
    rules = [f for f in findings if f.rule == "foreshadow_expiry"]
    # 只有「有证据的线索」该报；84 条 pf=0 与 pf=50 全部静音
    assert len(rules) == 1, [f.message for f in rules]
    assert rules[0].entity == "有证据的线索"
    assert "计划第 3 章" in rules[0].message

    # —— 旧判据的读数（用于对照，不依赖旧代码）——
    from app.models import KgForeshadow
    from sqlalchemy import select
    rows = db_session.scalars(
        select(KgForeshadow).where(KgForeshadow.status.in_(["planted", "developing"]),
                                   KgForeshadow.planned_payoff_chapter.isnot(None))
    ).all()
    old_would_warn = sum(1 for r in rows if r.planned_payoff_chapter < 9)
    assert old_would_warn == 85  # 84 条 pf=0 + 1 条 pf=3


# ====================================================================
# 三态：正文「取不到」不许被当成「没有」
# ====================================================================


def test_unavailable_text_reports_with_explicit_flag(db_session):
    """空库（无 Generation 行）：不能静默 → 按数值不变式照报，并标证据不可用。"""
    from app.kg.judge import check_foreshadow_expiry
    from app.models import KgForeshadow, Project

    project = Project(title="空库", target_length=3000, generation_length=1200)
    db_session.add(project)
    db_session.flush()
    db_session.add(KgForeshadow(project_id=project.id, name="无正文可核", status="planted",
                                planted_chapter=1, planned_payoff_chapter=5))
    db_session.commit()

    findings = check_foreshadow_expiry(db_session, project.id, text="", segment_no=1,
                                       current_chapter=9)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "foreshadow_expiry"
    assert f.evidence_unavailable is True
    assert "正文证据不可用" in f.message
    assert "evidence_unavailable" in f.evidence
    assert f.to_dict()["evidence_unavailable"] is True


def test_explicit_text_available_false_skips_evidence_check(db_session):
    """显式 `text_available=False`（读取失败）同样照报，不靠猜空串语义。"""
    from app.kg.judge import check_foreshadow_expiry
    from app.models import KgForeshadow, Project

    project = Project(title="读取失败", target_length=3000, generation_length=1200)
    db_session.add(project)
    db_session.flush()
    db_session.add(KgForeshadow(project_id=project.id, name="读不到正文", status="planted",
                                planted_chapter=1, planned_payoff_chapter=5))
    db_session.commit()

    findings = check_foreshadow_expiry(db_session, project.id, text="第三章", segment_no=1,
                                       current_chapter=9, text_available=False)
    assert len(findings) == 1 and findings[0].evidence_unavailable is True


def test_available_text_without_evidence_stays_silent(db_session):
    """正文可用且确无该章 ⇒ 仍静音（84→0 的降噪不被三态改动破坏）。"""
    from app.kg.judge import check_foreshadow_expiry
    from app.models import Generation, KgForeshadow, Project

    project = Project(title="有正文", target_length=3000, generation_length=1200)
    db_session.add(project)
    db_session.flush()
    db_session.add(Generation(project_id=project.id, segment_no=1,
                              chapter_range="1-1", text="他打算第三章再问。"))
    db_session.add(KgForeshadow(project_id=project.id, name="无证据的远景", status="planted",
                                planted_chapter=1, planned_payoff_chapter=5))
    db_session.commit()

    findings = check_foreshadow_expiry(db_session, project.id, text="", segment_no=9,
                                       current_chapter=9)
    assert findings == []


def test_flagged_findings_survive_save_and_load(db_session):
    """`evidence_unavailable` 落库后能读回（props 里是显式布尔，不是文案）。"""
    from app.kg.judge import JudgeFinding, load_judge_findings, save_judge_findings
    from app.models import Project

    project = Project(title="落库", target_length=3000, generation_length=1200)
    db_session.add(project)
    db_session.commit()

    n = save_judge_findings(db_session, project.id, [JudgeFinding(
        rule="foreshadow_expiry", severity="warn", entity="x", message="m",
        evidence_unavailable=True).to_dict()], segment_no=3)
    assert n == 1
    loaded = load_judge_findings(db_session, project.id)
    assert loaded[0]["evidence_unavailable"] is True
