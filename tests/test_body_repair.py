"""局部修订协议：只在显式授权下产出候选，**永不直接应用**。

main 原用例依赖 `test_causal_timeline.fixture`（未搬），这里用等价的最小项目夹具；
其余断言逐条保留：
- 制备卡是纯读（无 INSERT/UPDATE/DELETE），不落 EditProposal；
- Writer 只看得到「选中文本 + 目标」，看不到上下文/整段（避免整段重写）；
- 候选的语义验收恒为 unknown —— 生成成功 ≠ 语义成立，落地必须独立验收；
- 服务端能从矛盾 source 推出区间与保留集（模型不用算字符偏移）；
- 推不出范围就要求澄清；显式范围优先于推导；
- 删除必须显式授权；边界回显必须拒绝。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.body_consistency import build_body_candidate  # noqa: E402
from app.agents.body_repair import (  # noqa: E402
    collect_body_repair_cards,
    create_body_repair,
    prepare_body_repair,
)
from app.agents.diagnostic import run_diagnostic_task  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (AgentRun, EditProposal, Generation, Project)  # noqa: E402

TEXT = "他在账册写下页数。\n账册从未使用。\n灯灭了。"
ANSWER = {"assessment": "observed", "reason": "同一账册冲突", "findings": [
    {"dimension": "object_state", "assessment": "contradicted",
     "reason": "已写却否认使用", "source_ids": ["G1P001", "G1P002"]}]}


class Reply:
    def __init__(self, answer):
        self.answer = answer

    def chat(self, system, user):
        return json.dumps(self.answer, ensure_ascii=False)


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


def fixture(db):
    project = Project(title="局部修订", target_length=5000, mode="quick",
                      status="created", generation_length=3000)
    project.plot_timeline = {"planning": {"segment_contracts": {
        "1": {"authority": {"premise": "账册只有一本。"}}}}}
    db.add(project)
    db.commit()
    db.refresh(project)
    return db, project


def _diagnose(db, project, answer=ANSWER, segment_nos=(1,)):
    return run_diagnostic_task(db, project.id, task_type="body_consistency",
                               skill_id="body_consistency", allow_experimental=True,
                               segment_nos=list(segment_nos), provider=Reply(answer))


DATA_BLOCKERS = {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"}


def test_production_repair_is_pending_and_requires_independent_acceptance(db):
    db, project = fixture(db)
    text = TEXT
    g = Generation(project_id=project.id, segment_no=1, text=text)
    db.add(g)
    db.commit()

    diag = _diagnose(db, project)
    start = text.index("账册从未")
    end = start + len("账册从未使用。")
    args = dict(diagnostic_run_id=diag["run_id"], generation_id=g.id, start=start, end=end,
                objective="保留账册已写入事实；消除全否定，不添加行动或结果",
                preserve_source_ids=["G1P001"], conflict_source_id="G1P002")

    from sqlalchemy import event

    def no_writes(conn, cursor, sql, *a):
        assert sql.lstrip().split()[0].upper() not in DATA_BLOCKERS

    event.listen(db.bind, "before_cursor_execute", no_writes)
    with db.no_autoflush:
        card = prepare_body_repair(db, project.id, **args)
        # preserve 集合不能空着走：能从矛盾另一端推就用推出来的
        derived = prepare_body_repair(db, project.id,
                                      **dict(args, preserve_source_ids=[]))
    event.remove(db.bind, "before_cursor_execute", no_writes)

    assert derived["pending_action"]["body"]["preserve_source_ids"] == ["G1P001"]
    assert card["pending_action"]["endpoint"] == "/api/assistant/body-repairs"
    assert db.query(EditProposal).count() == 0

    class Writer:
        calls = 0

        def chat(self, system, user):
            self.calls += 1
            data = json.loads(user)
            assert "context" not in data and "full_paragraph" not in data
            assert data["selected_text"] == "账册从未使用。"
            assert data["objective"] == args["objective"]
            return json.dumps({"replacement": "账册尚未使用。"})

    writer = Writer()
    result = create_body_repair(
        db, project.id, **args,
        base_content_hash=card["pending_action"]["body"]["base_content_hash"],
        writer=writer, assistant=Reply(ANSWER))

    assert writer.calls == 1
    assert result["semantic_accepted"] is False
    assert result["verification_acceptance"]["verification_scope"] == "local_repair_verification"
    assert result["verification_acceptance"]["local_repair_verification"] == "unknown"
    assert result["proposal"]["status"] == "pending"
    assert db.get(Generation, g.id).text == text
    assert result["verification"]["checks"][0]["status"] == "contradicted"


def test_apply_refuses_repair_without_independent_acceptance(db):
    db, project = fixture(db)
    g = Generation(project_id=project.id, segment_no=1, text="前文。错误。后文。")
    run = AgentRun(project_id=project.id, role="assistant", status="success",
                   request_hash="x")
    db.add_all([g, run])
    db.commit()

    from app.agents.proposals import InvalidTransition, apply_proposal

    proposal = EditProposal(project_id=project.id, run_id=run.id, type="text_edit",
                            target_kind="generation_text", target_table="generations",
                            target_id=str(g.id), status="pending", reason="定点修订",
                            before_text=g.text, after_text="前文。正确。后文。",
                            scope_json={"independent_verification_required": True,
                                        "semantic_accepted": False,
                                        "local_apply_authorized": False})
    db.add(proposal)
    db.commit()
    proposal.status = "approved"
    db.commit()
    with pytest.raises(InvalidTransition, match="semantic"):
        apply_proposal(db, proposal=proposal)


@pytest.mark.parametrize("text,replacement", [
    ('星舰传来暗号。\n回声重复暗号。\n恒星仍在燃烧。', '回声再次重复暗号。'),
    ('“门没有开。”他撒谎。\n门开着。\n她继续看书。', '门仍开着。'),
    ('听说剑已折。\n她只听到传闻。\n远山仍在。', '她尚未核实传闻。'),
])
def test_normal_repetition_denial_dialogue_and_rumour_are_not_deduplicated(text, replacement):
    start = text.index("\n") + 1
    end = text.index("\n", start)
    candidate = build_body_candidate(text, start, end, replacement)
    assert candidate["after_text"] == text[:start] + replacement + text[end:]


def test_a_repair_without_any_derivable_scope_is_still_refused(db):
    db, project = fixture(db)
    db.add(Generation(project_id=project.id, segment_no=1, text=TEXT))
    db.commit()
    diag = _diagnose(db, project)
    with pytest.raises(ValueError, match="clarification"):
        prepare_body_repair(db, project.id, diagnostic_run_id=diag["run_id"],
                            objective="消除全否定")


def test_the_server_resolves_the_range_from_the_conflict_source(db):
    db, project = fixture(db)
    g = Generation(project_id=project.id, segment_no=1, text=TEXT)
    db.add(g)
    db.commit()
    diag = _diagnose(db, project)

    card = prepare_body_repair(
        db, project.id, diagnostic_run_id=diag["run_id"], conflict_source_id="G1P002",
        objective="保留账册已写入事实；消除全否定，不添加行动或结果")

    body = card["pending_action"]["body"]
    assert body["generation_id"] == g.id
    assert body["start"] == TEXT.index("账册从未使用。")
    assert body["end"] == body["start"] + len("账册从未使用。")
    assert body["preserve_source_ids"] == ["G1P001"]
    assert db.query(EditProposal).count() == 0


def test_the_derived_preserve_set_stays_inside_the_edited_segment(db):
    db, project = fixture(db)
    g1 = Generation(project_id=project.id, segment_no=1, text=TEXT)
    g2 = Generation(project_id=project.id, segment_no=2, text="天亮了。\n风停了。")
    db.add_all([g1, g2])
    db.commit()
    answer = {"assessment": "observed", "reason": "跨段矛盾", "findings": [
        {"dimension": "time", "assessment": "contradicted",
         "reason": "第1段与第2段对不上",
         "source_ids": ["G1P001", "G1P002", "G2P001"]}]}
    diag = _diagnose(db, project, answer, segment_nos=(1, 2))

    card = prepare_body_repair(db, project.id, diagnostic_run_id=diag["run_id"],
                               conflict_source_id="G1P002", objective="消除矛盾")
    body = card["pending_action"]["body"]
    assert body["generation_id"] == g1.id
    assert body["preserve_source_ids"] == ["G1P001"]


def test_an_explicit_range_still_wins_over_the_resolved_one(db):
    db, project = fixture(db)
    db.add(Generation(project_id=project.id, segment_no=1, text=TEXT))
    db.commit()
    diag = _diagnose(db, project)
    start = TEXT.index("账册从未")
    end = start + len("账册从未使用。")
    card = prepare_body_repair(
        db, project.id, diagnostic_run_id=diag["run_id"], conflict_source_id="G1P002",
        start=start, end=end, objective="消除全否定", preserve_source_ids=["G1P001"])
    body = card["pending_action"]["body"]
    assert (body["start"], body["end"]) == (start, end)


def test_proposal_rejects_outside_fixed_range_changes(db):
    db, project = fixture(db)
    g = Generation(project_id=project.id, segment_no=1, text="前文。错误。后文。")
    run = AgentRun(project_id=project.id, role="assistant", status="success",
                   request_hash="x")
    db.add_all([g, run])
    db.commit()

    from app.agents.proposals import PatchRejected, build_proposal

    with pytest.raises(PatchRejected, match="fixed_range"):
        build_proposal(db, run_id=run.id, project_id=project.id, proposal_type="text_edit",
                       target_kind="generation_text", target_id=str(g.id),
                       after_text="改前文。正确。后文。", reason="定点修订",
                       scope={"fixed_range": {"start": 3, "end": 6, "replacement": "正确。"}})


def test_read_only_cards_are_collected_without_execution():
    action = {"kind": "body_repair", "endpoint": "/api/assistant/body-repairs",
              "method": "POST", "body": {"project_id": 4}}
    assert collect_body_repair_cards({"prepare_body_repair": {"pending_action": action}}, 4) == [action]
    assert collect_body_repair_cards({"other": {"pending_action": action}}, 4) == []
    assert collect_body_repair_cards({"prepare_body_repair": {"pending_action": action}}, 5) == []


def test_deletion_requires_explicit_noncritical_authorization():
    text = "已登记。\n从未登记。\n继续。"
    start = text.index("从未")
    end = start + len("从未登记。")
    with pytest.raises(ValueError):
        build_body_candidate(text, start, end, "")
    assert build_body_candidate(text, start, end, "", allow_delete=True)["after_text"] == (
        text[:start] + text[end:])


def test_actual_writer_suffix_echo_is_rejected():
    text = "陈舟写下第一行。\n桌面上的刷、压板和登记本都还没有使用，旧图册保持摊开的状态，等着下一步。"
    start = text.index("桌面")
    end = start + len("桌面上的刷、压板和登记本都还没有使用")
    with pytest.raises(ValueError, match="candidate_boundary_echo"):
        build_body_candidate(text, start, end,
                             "桌面上的刷、压板和登记本都尚未使用，旧图册保持摊开的状态，等着下一步。")
