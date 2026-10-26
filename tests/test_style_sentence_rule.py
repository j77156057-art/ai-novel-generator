"""诊断规则「句子被切碎」（`style_sentence_length_deviation`）。

## 为什么要有这条规则

实测（真书 + MiniMax-M3）：参考书原文 900 字切 **10 句**（平均 89 字/句），
模型生成同样 900 字切 **45 句**（平均 19 字/句）。提示里写了「句长 56±18.8 字」，
模型写出 10.9 字/句。这个差距此前**没有任何规则看得见**。

## 判据（全部来自库内既有数据，零模型调用）

- 基准 = 项目已落库的文风档案（就是真正注入提示词的那一份）；
- 尺度 = 参考书**自己**的「句长均值」分布：比它自己的 p10 还短 = 短于 90% 的人类样本
  （warn），比它自己的 min 还短 = 短于**任何**一段人类样本（error）；
- 对白豁免：对白占比比参考书里最密的那份还高时，句子短是合理的，不判。

要守住的：
1. **碎**才报（不误报）——参考书那种长句子不能被判成问题；
2. **真碎**必报，且带**可复核的证据**（原文章节 + 那一段的原文）；
3. 对白密的那段不报（对话本来就是短句）；
4. 没有档案的项目不报（没有基准就没有「像不像」）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402

from app.agents.diagnosis import (  # noqa: E402
    RULE_ORDER,
    DiagnosticSnapshot,
    rule_style_sentence_length_deviation,
    run_diagnostic_rules,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Generation, Project  # noqa: E402
from app.style.stylometry import features  # noqa: E402


# 参考书自己的「句长均值」分布（真书实测：mean 56.0 / p10 34.0 / min 24.9）。
PROFILE = {
    "version": 2,
    "_meta": {"base_unit_chars": 3000},
    "buckets": {
        "medium": {
            "unit_chars": 3000,
            "feat": {
                "句长均值": {"mean": 56.0, "std": 18.8, "p10": 34.0,
                             "p50": 53.5, "p90": 84.2, "min": 24.9, "max": 109.7},
                "对白字占比": {"mean": 0.175, "std": 0.122, "p10": 0.027,
                               "p50": 0.155, "p90": 0.358, "min": 0.0, "max": 0.553},
            },
        },
    },
}


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _text(sent_len: int, dialogue: bool = False) -> str:
    """造一段指定平均句长的文本（约 3000 字 → 落 medium bucket，缩放系数≈1）。

    句子里**不能自带句号**（那会把一句话切成两句，平均句长就变了两句的）。
    对白版要给闭合引号 —— 引号不成对时对白占比算出来是 0。
    """
    unit = "北凉王府的灯在风里晃了晃像是谁在檐下轻轻咳了一声管家便把灯芯挑亮了些"
    body = (unit * 3)[:sent_len - 1]
    per_para = max(1, 500 // sent_len)
    lines = []
    for _ in range(6):
        if dialogue:
            lines.append("".join(f"“{body}。”" for _ in range(per_para)))
        else:
            lines.append("".join(body + "。" for _ in range(per_para)))
    return "\n".join(lines)


def _project(with_profile: bool = True) -> Project:
    db = SessionLocal()
    proj = Project(title="句长诊断测试", target_length=5000, mode="quick", status="created",
                   style_profile=PROFILE if with_profile else None)
    db.add(proj)
    db.commit()
    db.refresh(proj)
    db.close()
    return proj


def _segment(proj_id: int, text: str, segment_no: int = 1) -> int:
    db = SessionLocal()
    gen = Generation(project_id=proj_id, segment_no=segment_no,
                     chapter_range=f"{segment_no}-{segment_no}", text=text,
                     summary={"short": f"第 {segment_no} 段摘要"})
    db.add(gen)
    db.commit()
    db.refresh(gen)
    gid = gen.id
    db.close()
    return gid


def _rule(project_id: int):
    db = SessionLocal()
    try:
        proj = db.get(Project, project_id)
        snap = DiagnosticSnapshot(db=db, project=proj, scope="all")
        return rule_style_sentence_length_deviation(snap, set())
    finally:
        db.close()


# ====================================================================
# 1) 碎才报
# ====================================================================

def test_the_rule_is_registered():
    assert "style_sentence_length_deviation" in RULE_ORDER


def test_choppy_text_is_reported_as_error():
    """比参考书自己最碎的一段还碎 → error。"""
    proj = _project()
    _segment(proj.id, _text(sent_len=12))
    issues = _rule(proj.id)
    assert len(issues) == 1, issues
    issue = issues[0]
    assert issue.rule == "style_sentence_length_deviation"
    assert issue.type == "style"
    assert issue.severity == "error"
    assert "句子" in issue.title
    # claim 里要给出可复算的数字（参考书的尺子 + 本段的实测）
    assert "56.0" in issue.claim and "24.9" in issue.claim
    # 证据必须是库里的原文章节，且带得出可定位的段落
    assert issue.evidence[0].kind == "generation"
    assert issue.evidence[0].source == "generated"
    assert issue.evidence[0].ref_id.startswith(f"generation:{_last_gen_id(proj.id)}")


def _last_gen_id(project_id: int) -> int:
    db = SessionLocal()
    try:
        g = db.query(Generation).filter(Generation.project_id == project_id).order_by(
            Generation.segment_no).all()
        return g[-1].id
    finally:
        db.close()


def test_long_sentence_text_is_not_reported():
    """参考书那种长句子不能被判成问题（不误报）。"""
    proj = _project()
    _segment(proj.id, _text(sent_len=60))
    assert _rule(proj.id) == []


def test_the_middle_band_is_warn_not_error():
    """比 p10 短但还比 min 长 → warn（判得不够死就不给 error）。"""
    proj = _project()
    _segment(proj.id, _text(sent_len=30))
    issues = _rule(proj.id)
    assert len(issues) == 1, issues
    assert issues[0].severity == "warn"


# ====================================================================
# 2) 对白豁免
# ====================================================================

def test_a_dialogue_heavy_segment_is_exempt():
    """对白占比比参考书里最密的那份还高 → 句子短是合理的，不报。"""
    proj = _project()
    text = _text(sent_len=12, dialogue=True)
    _segment(proj.id, text)
    vec = features(text)
    assert vec["对白字占比"] > 0.553, vec["对白字占比"]
    assert _rule(proj.id) == []


# ====================================================================
# 3) 没有基准就不报
# ====================================================================

def test_a_project_without_a_style_profile_is_not_judged():
    proj = _project(with_profile=False)
    _segment(proj.id, _text(sent_len=10))
    assert _rule(proj.id) == []


def test_a_project_without_segments_is_not_judged():
    proj = _project()
    assert _rule(proj.id) == []


def test_a_very_short_segment_is_ignored():
    """太短的段（试写/占位）统计噪声大，不参与判定。"""
    proj = _project()
    _segment(proj.id, "他走了。她点头。灯灭了。")
    assert _rule(proj.id) == []


# ====================================================================
# 4) 端到端：跑全量规则时它在列，且不拖垮其余规则
# ====================================================================

def test_run_diagnostic_rules_includes_the_style_issue():
    db = SessionLocal()
    try:
        proj = _project()
        _segment(proj.id, _text(sent_len=12))
        proj = db.get(Project, proj.id)
        report = run_diagnostic_rules(db, proj, scope="all")
        rules = [i.rule for i in report.issues]
        assert "style_sentence_length_deviation" in rules
        assert "style_sentence_length_deviation" in report.rules_evaluated
    finally:
        db.close()


# ====================================================================
# 5) 闭环：issue → 改写卡（证据由服务端带，不让模型抄）
# ====================================================================

def test_the_issue_can_back_a_rewrite_proposal():
    """诊断结论 → 改写卡：建议与证据由服务端按 issue id 取，不靠模型转述。"""
    from app.agents.registry import _impl_propose_text_edit

    db = SessionLocal()
    try:
        proj = _project()
        text = _text(sent_len=12)
        seg_id = _segment(proj.id, text)
        proj = db.get(Project, proj.id)
        report = run_diagnostic_rules(db, proj, scope="all")
        issue = next(i for i in report.issues
                     if i.rule == "style_sentence_length_deviation")

        # 「改正文某段」是整段替换 → 草稿必须是完整新正文（长度相当）。
        draft = text.replace("北凉王府的灯", "北凉王府那盏灯")
        card = _impl_propose_text_edit(
            db=db, project_id=proj.id, type="text_edit", target_kind="generation_text",
            target_id=str(seg_id), draft=draft, from_issue=issue.id,
        )
        assert card["ok"] is True, card
        body = card["pending_action"]["body"]
        assert body["from_issue"] == issue.id
        # reason 默认取那条结论的建议（模型没写就由服务端补）
        assert body["reason"] == issue.suggestion
        # 证据由服务端带进提案，用户在卡上能看到「为什么改这条」
        assert body["evidence"], body
        assert body["evidence"][0]["ref_id"] == issue.evidence[0].ref_id
        # 卡上标出原文多长（用户要能一眼看出这一改是不是删了太多）
        assert card["pending_action"]["detail"]["target_chars"] == len(text)
    finally:
        db.close()


def test_a_partial_draft_is_refused_with_a_way_out():
    """草稿明显短于原文 → 拒绝，并说清该怎么改。

    「改正文某段」是**整段替换**：只写了一部分就落库，等于静默删掉后半段。
    """
    from app.agents.registry import _impl_propose_text_edit

    db = SessionLocal()
    try:
        proj = _project()
        text = _text(sent_len=12)
        seg_id = _segment(proj.id, text)
        card = _impl_propose_text_edit(
            db=db, project_id=proj.id, type="text_edit", target_kind="generation_text",
            target_id=str(seg_id), draft=text[:200],
        )
        assert card["ok"] is False
        note = card["note"]
        assert "整段替换" in note
        assert "start_char" in note, note
    finally:
        db.close()


def test_an_unknown_issue_id_is_reported_not_silently_dropped():
    """issue id 写错要说清，不能悄悄提一张没有依据的卡。"""
    from app.agents.registry import _impl_propose_text_edit

    db = SessionLocal()
    try:
        proj = _project()
        seg_id = _segment(proj.id, _text(sent_len=12))
        card = _impl_propose_text_edit(
            db=db, project_id=proj.id, type="text_edit", target_kind="generation_text",
            target_id=str(seg_id), draft="草稿", from_issue="style_sentence_length_deviation:seg999",
        )
        assert card["ok"] is False
        assert "issue" in card["note"]
    finally:
        db.close()
