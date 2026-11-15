"""冻结权威层适配器：局部盘点结论**永远不能**升级为全局验收。

这里钉的是 `validate_authority_request` 的准入纪律（main 原用例的等价物）：
- 只有「闭集 + 明确违例 + 两端证据可逐字复核」才允许出修复卡；
- 报告/观测/契约/项目任何一处漂移 ⇒ 拒绝（stale / mismatch）；
- 准入是只读的：不写正文、不写 run。
- 少了主区间 / 区间越界 / 区间落在非保留段 ⇒ 拒绝。

`read_capability` 在 feat 侧还缺 `style.scope` / `prompt_audit` / `consent`
这些运行时依赖（属后续批次），所以用同形状的观测替身，只测适配器本身。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents import authority_body_repair as abr  # noqa: E402
from app.agents.diagnostic import digest  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AgentRun, Generation, Project  # noqa: E402

BODY = "她戴上手套。"
AUTHORITY = "清单完整：一本账册。不得新增物品。"
CONTRACT = {"authority": {"premise": AUTHORITY}, "node_id": "n1"}


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


def _observation(db, project_id, capability, scope):
    """`read_closed_inventory` 的等价替身：**从当前库状态**读，才能真正测出漂移。

    真实实现在 feat 侧还缺 `style.scope` / `prompt_audit` / `consent`（后续批次），
    但它的输出形状就是 `{capability, data:{segments,sources}, input_fingerprint}`，
    这里按同一形状从库现读。
    """
    project = db.get(Project, project_id)
    contracts = (((project.plot_timeline or {}).get("planning") or {})
                 .get("segment_contracts") or {})
    segments, sources = [], {}
    for no in scope.get("segment_nos") or []:
        segment_no = int(no)
        gens = (db.query(Generation)
                .filter_by(project_id=project_id, segment_no=segment_no).all())
        if not gens:
            continue
        text = "\n".join(g.text or "" for g in gens)
        generation_id = gens[0].id
        contract = contracts.get(str(segment_no))
        segments.append({"segment_no": segment_no, "generation_id": generation_id,
                         "contract": contract})
        sources["G1P001"] = {"source_id": f"generation:{generation_id}", "quote": text,
                             "start": 0, "end": len(text)}
        premise = ((contract or {}).get("authority") or {}).get("premise")
        if premise:
            sources["C1S001"] = {"source_id": f"contract:{segment_no}",
                                 "path": "/authority/premise", "quote": premise,
                                 "start": 0, "end": len(premise)}
    data = {"segments": segments, "sources": sources}
    return {"capability": capability, "data": data, "input_fingerprint": digest(data)}


def _case(db, *, monkeypatch, mode="closed", status="violated",
          project_id=None, run_status="success"):
    project = Project(title="权威修复", target_length=5000, mode="quick",
                      status="created", generation_length=3000)
    project.plot_timeline = {"planning": {"segment_contracts": {"1": CONTRACT}}}
    db.add(project)
    db.flush()
    generation = Generation(project_id=project.id, segment_no=1, text=BODY)
    db.add(generation)
    db.flush()
    run = AgentRun(project_id=project.id, role="assistant", status=run_status,
                   request_hash="t")
    db.add(run)
    db.commit()

    scope = {"character": "", "cursor": 0, "limit": 5, "segment_nos": [1],
             "inventory_kinds": ["object"]}
    obs = _observation(db, project.id, "read_closed_inventory", scope)
    sources = obs["data"]["sources"]

    def cite(sid):
        return dict(sources[sid], source_id=sid,
                    body_source_id=sources[sid]["source_id"], location_verified=True)

    report = {
        "run_id": run.id, "project_id": project.id,
        "agent_role": "diagnostic", "provider_role": "assistant",
        "model_participated": True,
        "skill": {"id": "closed_inventory", "version": 4, "status": "experimental"},
        "scope": scope,
        "observations": [obs],
        "input_fingerprint": digest([obs]),
        "acceptance": [{"id": "full_scope_coverage", "requires_evidence": True}],
        "inventory": {"input_fingerprint": obs["input_fingerprint"], "candidates": [{
            "id": "N001",
            "constraint": {"kind": "object", "mode": mode, "source_id": "C1S001",
                           "quote": AUTHORITY, "segment_no": 1},
            "mention": {"source_id": "G1P001"},
            "verdict": {"status": status, "body_source_id": "G1P001",
                        "authority_source_id": "C1S001"},
        }]},
        "checks": [{"id": "object_N001", "status": "contradicted",
                    "reason": "手套未授权",
                    "citations": [cite("C1S001"), cite("G1P001")]}],
        "unresolved": ["inventory_partial_coverage"],
    }
    run.note = json.dumps(report)
    db.commit()

    # `read_capability` 的替身：每次调用都从当前库现读，漂移才能被测出来
    monkeypatch.setattr(
        abr, "read_capability",
        lambda session, proj_id, capability, sc: _observation(session, proj_id, capability, sc))

    args = dict(project_id=project_id or project.id, diagnostic_run_id=run.id,
                generation_id=generation.id, start=0, end=len(BODY),
                objective="消除手套，不添加事实",
                preserve_source_ids=["C1S001"], conflict_source_id="G1P001",
                allow_delete=False)
    return db, project, generation, run, args, report


def test_closed_authority_admission_is_read_only(db, monkeypatch):
    db, _, generation, run, args, _ = _case(db, monkeypatch=monkeypatch)
    before = (generation.text, run.note)

    payload = abr.validate_authority_request(db, args["project_id"], run, generation,
                                             json.loads(run.note), args["start"], args["end"],
                                             args["objective"], args["preserve_source_ids"],
                                             args["conflict_source_id"], args["allow_delete"])

    assert payload["selected_text"] == BODY
    assert payload["preserve_evidence"] == [{"source_id": "C1S001", "quote": AUTHORITY}]
    assert payload["frozen_contract"] == CONTRACT
    assert payload["contract_hash"] == digest(CONTRACT)
    assert payload["diagnostic_binding"]["target_check_id"] == "object_N001"
    assert payload["boundary_read_only"] == {"before": "", "after": ""}
    assert payload["allow_delete"] is False
    assert before == (generation.text, run.note)
    assert not db.dirty


@pytest.mark.parametrize("mutation", ["open", "allowed", "fingerprint", "citation",
                                     "project", "contract", "run_status"])
def test_untrusted_or_drifted_authority_is_rejected(db, monkeypatch, mutation):
    db, project, generation, run, args, report = _case(db, monkeypatch=monkeypatch)
    if mutation == "open":
        report["inventory"]["candidates"][0]["constraint"]["mode"] = "open"
    if mutation == "allowed":
        report["inventory"]["candidates"][0]["verdict"]["status"] = "allowed"
    if mutation == "fingerprint":
        report["observations"][0]["input_fingerprint"] = "bad"
    if mutation == "citation":
        report["checks"][0]["citations"][0]["quote"] = "fabricated"
    if mutation == "project":
        report["project_id"] = 999
    if mutation == "contract":
        project.plot_timeline = {"planning": {"segment_contracts": {"1": {"authority": {"premise": "其他"}}}}}
        db.add(project)
    if mutation == "run_status":
        run.status = "failed"
        db.add(run)
    run.note = json.dumps(report)
    db.commit()

    with pytest.raises((ValueError, PermissionError)):
        abr.validate_authority_request(db, args["project_id"], run, generation,
                                       report, args["start"], args["end"], args["objective"],
                                       args["preserve_source_ids"], args["conflict_source_id"],
                                       args["allow_delete"])


def test_scope_must_be_bounded_and_have_a_primary_range(db, monkeypatch):
    db, project, generation, run, args, report = _case(db, monkeypatch=monkeypatch)
    # 全是依赖区间 ⇒ 没有主区间
    with pytest.raises(ValueError, match="repair_requires_clarification"):
        abr.validate_authority_request(db, project.id, run, generation, report, 0, len(BODY),
                                       args["objective"], args["preserve_source_ids"],
                                       args["conflict_source_id"], False,
                                       ranges=[{"start": 0, "end": len(BODY),
                                                "conflict_source_id": "G1P001",
                                                "dependency": True}])
    # 越界
    with pytest.raises(ValueError, match="repair_requires_clarification"):
        abr.validate_authority_request(db, project.id, run, generation, report, 0, len(BODY) + 1,
                                       args["objective"], args["preserve_source_ids"],
                                       args["conflict_source_id"], False)
    # 空目标 / 保留段空
    with pytest.raises(ValueError, match="repair_requires_clarification"):
        abr.validate_authority_request(db, project.id, run, generation, report, 0, len(BODY),
                                       "  ", args["preserve_source_ids"],
                                       args["conflict_source_id"], False)
    with pytest.raises(ValueError, match="repair_requires_clarification"):
        abr.validate_authority_request(db, project.id, run, generation, report, 0, len(BODY),
                                       args["objective"], [], args["conflict_source_id"], False)
