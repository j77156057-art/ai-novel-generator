"""正文的**文字质量**审核：标点事故（error）与外来英文（warn）。

要守的两条：
1. **规则判得死的才 error**（标点）—— error 会挡住工序链，误报就堵死真活；
2. **判不准的只报不拦**（外来词）—— 小说里合法地有英文（专名/型号/引文）。

main 原用例里的两条 registry 工具（`audit_text_quality` / `propose_text_fix`）
属于第 2 批 registry 的消费端，feat 尚未注册，这里改用模块自身的
`suggest_punctuation_fixes` 覆盖同一行为（`kind=proposal` 的卡形状见
`registry` 搬过来后补测）。
"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.text_quality import (  # noqa: E402
    fix_punctuation,
    run_text_quality_audit,
    suggest_punctuation_fixes,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import EditProposal, Generation, Project  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def project(db):
    p = Project(title="文字质量", target_length=5000, mode="quick",
                status="created", generation_length=3000)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _seed(db, p, text):
    db.add(Generation(project_id=p.id, segment_no=1, text=text))
    db.commit()


def test_halfwidth_punctuation_between_chinese_is_an_error(db, project):
    # 半角逗号（显式拼出来，免得源码里看不出是哪个）
    _seed(db, project, "他" + "," + "将砂粒捏碎，放在鼻下闻了闻。")
    report, stats = run_text_quality_audit(db, project)
    errors = [i for i in report.issues if i.severity == "error"]
    assert len(errors) == 1, report.issues
    assert errors[0].rule == "text_punctuation_accident"
    assert stats["punctuation_hits"] == 1
    # 证据要能指到位置
    assert errors[0].evidence


def test_repeated_punctuation_is_an_error(db, project):
    _seed(db, project, "他愣住了。。船还在晃。")
    report, _stats = run_text_quality_audit(db, project)
    errors = [i for i in report.issues if i.severity == "error"]
    assert len(errors) == 1, report.issues


def test_clean_chinese_prose_reports_nothing(db, project):
    _seed(db, project, "少年把竹篙插进三石缝里，撑了两下，船身才稳住。")
    report, stats = run_text_quality_audit(db, project)
    assert not report.issues, report.issues
    assert stats["blocking"] == 0


def test_a_latin_word_glued_to_chinese_is_reported_but_does_not_block(db, project):
    """「Already惹了。」—— 紧贴中文的英文词：报出来，但**不挡**。"""
    _seed(db, project, "“Already惹了。”陆长风捂着后脑退回老周身边。")
    report, stats = run_text_quality_audit(db, project)
    warns = [i for i in report.issues if i.severity == "warn"]
    assert len(warns) == 1, report.issues
    assert warns[0].rule == "text_foreign_latin"
    assert stats["foreign_latin_hits"] == 1
    assert stats["foreign_latin_glued"] == 1
    assert stats["blocking"] == 0


def test_a_quoted_english_phrase_is_not_reported(db, project):
    """引文里**分开**的外文是合法的（“Hello” 他说）—— 不该报。"""
    _seed(db, project, "“Hello，”陆长风说，“你听我解释。”")
    report, stats = run_text_quality_audit(db, project)
    assert stats["foreign_latin_hits"] == 0, report.issues


def test_a_registered_proper_noun_is_not_reported(db, project):
    """已登记的专名（剧情圣经里的角色）出现是合法的。"""
    project.plot_bible = {"characters": [{"name": "Alice"}]}
    db.commit()
    _seed(db, project, "Alice把伞收起来，站在门口看了看天。")
    _, stats = run_text_quality_audit(db, project)
    assert stats["foreign_latin_hits"] == 0


def test_a_model_code_is_not_reported(db, project):
    _seed(db, project, "桌上摊着 PC-2000 的说明书，他翻了两页。")
    _, stats = run_text_quality_audit(db, project)
    assert stats["foreign_latin_hits"] == 0


def test_the_agent_can_run_the_text_quality_audit(db, project):
    from app.agents.registry import get_tool_registry
    _seed(db, project, "“Already惹了。”陆长风捂着后脑退回老周身边。")
    out = get_tool_registry().invoke("audit_text_quality", db, project.id)
    assert out["found"] is True, out
    assert out["errors"] == 0
    assert out["warnings"] == 1
    assert out["issues"][0]["rule"] == "text_foreign_latin"


def test_the_punctuation_fix_is_computed_by_the_rule_not_the_model():
    """修法是**机械唯一**的：半角→全角、重复标点收成一个。"""
    fixed, changes = fix_punctuation("他,将砂粒捏碎。。船身晃了晃。")
    assert changes == 2
    assert fixed == "他，将砂粒捏碎。船身晃了晃。"


def test_the_fix_keeps_english_decimal_points():
    """英文里的半角小数点不能被改成全角（规则只改命中中文旁的那几处）。"""
    fixed, changes = fix_punctuation("水温 3.5 度，他把桨放下。")
    assert fixed == "水温 3.5 度，他把桨放下。"
    assert changes == 0


def test_fix_suggestions_are_draft_only_and_whole_segment(db, project):
    """审核 → 修复：只算卡（整段 after_text），不落库。"""
    _seed(db, project, "他,将砂粒捏碎，放在鼻下闻了闻。")
    generation = db.query(Generation).filter_by(project_id=project.id).one()

    cards = suggest_punctuation_fixes(db, project)

    assert len(cards) == 1
    card = cards[0]
    assert card["generation_id"] == generation.id
    assert card["after"] == "他，将砂粒捏碎，放在鼻下闻了闻。"
    assert card["before"] == generation.text
    assert card["changes"] == 1
    # 只构造卡：不落库
    assert db.query(EditProposal).count() == 0


def test_no_fix_card_without_punctuation_problems(db, project):
    """没有可自动修正的问题 → 不给卡（外来英文词机器不替人改）。"""
    _seed(db, project, "“Already惹了。”陆长风捂着后脑退回老周身边。")
    assert suggest_punctuation_fixes(db, project) == []


# ----------------------------------------------------------------------
# 事实自洽（模型那一层）：只报不挡
# ----------------------------------------------------------------------


class _FakeProvider:
    """假渠道：回一段固定答案。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def chat(self, system, user, **kwargs):
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_a_fact_contradiction_is_reported_but_never_blocks(db, project):
    """模型判出的属性矛盾 → **warn**，不阻塞（模型会错：时间推进/撒谎/猜测都像矛盾）。"""
    _seed(db, project, "他那年十四岁。\n沈娘子笑他：十三岁的娃娃，也敢撑船。")
    provider = _FakeProvider(
        '{"contradictions":[{"entity":"陆见川","attribute":"年龄",'
        '"values":["十四岁","十三岁"],"source_ids":["G1P001","G1P002"],'
        '"quote":"十三岁的娃娃","reason":"同一段里两种说法"}]}')
    report, stats = run_text_quality_audit(db, project, provider=provider)
    warns = [i for i in report.issues if i.severity == "warn"]
    assert len(warns) == 1, report.issues
    assert warns[0].rule == "text_fact_contradiction"
    assert "年龄" in warns[0].title
    assert stats["blocking"] == 0
    assert stats["fact_contradictions"] == 1
    assert provider.calls == 1


def test_a_broken_model_answer_degrades_to_the_deterministic_rules(db, project):
    """模型答得不合格/直接报错 → 不猜，只跑确定性规则（不拖垮整次审核）。"""
    _seed(db, project, "他,将竹篙插进石缝。")
    for payload in (RuntimeError("boom"), "not json at all",
                    '{"contradictions": "not a list"}'):
        provider = _FakeProvider(payload)
        report, stats = run_text_quality_audit(db, project, provider=provider)
        assert stats["blocking"] == 1, stats  # 标点那条照旧
        assert not [i for i in report.issues if i.rule == "text_fact_contradiction"]


def test_a_crash_in_the_fact_layer_is_visible_in_the_stats(db, project):
    """这一层挂掉**不静默**：统计里要看得见，否则「没报」会被读成「查过没问题」。"""
    _seed(db, project, "他那年十四岁。")

    class _Boom:
        def chat(self, *args, **kwargs):
            raise ValueError("provider exploded")

    report, stats = run_text_quality_audit(db, project, provider=_Boom())
    assert "fact_error" in stats, stats
    assert "ValueError" in stats["fact_error"]


def test_the_fact_check_is_skipped_without_a_provider(db, project):
    """不给渠道就不调模型（工序链的读路径上就是这个口径）。"""
    _seed(db, project, "他那年十四岁。")
    report, stats = run_text_quality_audit(db, project)
    assert stats["fact_checked"] is False
    assert not [i for i in report.issues if i.rule == "text_fact_contradiction"]
