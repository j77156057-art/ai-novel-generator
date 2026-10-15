"""Assistant 审核 / 日志分析 / 调动生成测试（第 7 步交付 2/3/4 验收）。

覆盖 team-lead 派单列出的验收项：

**交付 2（日志读取与分析）**
  (a) GET /api/assistant/runs 字段完整（id/status/model/note/时间/耗时/
      request_hash/evidence count），status 非法值 400，limit 越界 400；
  (b) GET /api/assistant/runs/{id} 详情带全部 Evidence；404（不存在 /
      跨项目）；
  (c) POST /api/assistant/analyze-logs 五条规则各自可命中（失败率异常 /
      registry 拦截 / 本地兜底降级 / 空 evidence / 耗时离群），issues 走
      diagnose 同款九字段结构；
  (d) 分析是**只读**的：不写任何业务表；且把「这次分析自己」排除在被判
      历史之外。

**交付 3（审核）**
  (e) audit-corpus / audit-kg / audit-generation 各自至少一条规则命中，
      返回统一 {answer, issues, evidence, confidence, agent_run_id}；
  (f) 语料审核的 issue 定位 novel_id/chunk_id；KG 审核的 name_conflict
      与 diagnose **同口径**（复用同一规则函数）；
  (g) 全部只读；未知 project / novel → 404。

**交付 4（调动生成）**
  (h) invoke-generation 复用 `app.generation.engine`（generate_next /
      regenerate_segment_stream），不绕过引擎直写 Generation；
  (i) 落 role='assistant' 的 AgentRun，note 写明「这次生成是助手调用的」，
      响应回显 Writer 实际模型；
  (j) 无显式 instruction → 422；regenerate 缺 segment_no → 422；
      generate_next 带 segment_no → 422；段不存在 → 404；
  (k) 生成失败 → run 记 failed 且不返回假的 200；
  (l) 工具注册表**不含**任何直写工具（助手没有后门）。

不依赖真实网络：Writer 单例换成 LocalFallbackLLMProvider（确定性模板），
Assistant provider 视用例需要 monkeypatch 成记录器 / 炸锅器。
"""

from __future__ import annotations

import os
import sys

# ─── bootstrap ──────────────────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import invoke_tool  # noqa: E402
from app.agents.registry import (  # noqa: E402
    ALLOWED_READ_ONLY_TOOLS,
    reset_tool_registry,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    Evidence,
    Fact,
    Generation,
    KgEdge,
    KgNode,
    NovelChunk,
    Project,
    RawNovel,
)
from app.routers import assistant as assistant_router  # noqa: E402
from app.routers import assistant_audit as audit_router  # noqa: E402
from app.routers import assistant_generation as gen_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例：清 provider / tool 注册表与库；Writer 换确定性兜底。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Writer 单例 = 本地兜底：生成可离线复现，文本带可断言标记
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    yield
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def project_id(db_session):
    proj = Project(title="审核测试项目", target_length=5000, mode="quick",
                   status="created", generation_length=3000)
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


# ====================================================================
# 种子数据小工具
# ====================================================================


def _seed_run(db, project_id: int, *, status: str = "success",
              model: str = "test-model", duration_s: float = 1.0,
              note: str | None = None, evidence_count: int = 1) -> int:
    """造一条 AgentRun（+ 若干 Evidence），返回 run id。"""
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    run = AgentRun(
        project_id=project_id, role="assistant", status=status,
        started_at=started,
        finished_at=started + timedelta(seconds=duration_s) if status != "running" else None,
        request_hash="0" * 64, model=model, note=note,
    )
    db.add(run)
    db.flush()
    for i in range(evidence_count):
        db.add(Evidence(
            run_id=run.id, kind="run", source="agent_runs",
            ref_id=f"seed:{run.id}:{i}", snippet="种子证据", score="0.5",
        ))
    db.commit()
    return run.id


def _seed_novel(db, title: str = "参考语料", *, style_profile: dict | None = None) -> int:
    novel = RawNovel(title=title, author="某作者", status="imported",
                     style_profile=style_profile)
    db.add(novel)
    db.commit()
    db.refresh(novel)
    return novel.id


def _seed_chunk(db, novel_id: int, text: str, *, idx: int = 0,
                embedding: str | None = None) -> int:
    chunk = NovelChunk(novel_id=novel_id, chapter_no=1, idx=idx, text=text,
                       embedding=embedding)
    db.add(chunk)
    db.commit()
    db.refresh(chunk)
    return chunk.id


def _seed_segment(db, project_id: int, segment_no: int, text: str) -> int:
    gen = Generation(
        project_id=project_id, segment_no=segment_no,
        chapter_range=f"段{segment_no}", text=text,
        summary={"text": f"第 {segment_no} 段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen.id


def _seed_character_node(db, project_id: int, name: str, mentions: int = 6) -> int:
    node = KgNode(project_id=project_id, type="character",
                  props={"name": name, "mentions": mentions},
                  first_chapter=1, last_chapter=2, source="generated")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node.id


def _seed_character_fact(db, project_id: int, key: str,
                         aliases: list[str] | None = None) -> int:
    fact = Fact(project_id=project_id, type="character", key=key,
                props={"mentions": 6, "speaking": True, "named": True},
                aliases=list(aliases or []), source="generated",
                confidence="high", first_segment=1, last_segment=2)
    db.add(fact)
    db.commit()
    db.refresh(fact)
    return fact.id


def _business_snapshot(db) -> tuple:
    """业务表快照（用于「只读」断言）。"""
    return (
        db.scalar(select(func.count()).select_from(Generation)),
        db.scalar(select(func.count()).select_from(Fact)),
        db.scalar(select(func.count()).select_from(KgNode)),
        db.scalar(select(func.count()).select_from(KgEdge)),
        db.scalar(select(func.count()).select_from(NovelChunk)),
        db.scalar(select(func.count()).select_from(RawNovel)),
    )


def _rules_of(result: dict) -> set[str]:
    return {i["rule"] for i in result["issues"]}


# ====================================================================
# 交付 2 (a)(b)：日志读取端点
# ====================================================================


def test_get_runs_returns_full_fields(db_session, project_id):
    """GET /runs：每条 run 带齐 id/status/model/note/时间/耗时/hash/证据数。"""
    _seed_run(db_session, project_id, status="success", model="m-1",
              duration_s=2.5, note="诊断", evidence_count=2)
    _seed_run(db_session, project_id, status="failed", model="(local_fallback)",
              duration_s=0.5, evidence_count=0)

    out = assistant_router.assistant_runs(
        project_id=project_id, status=None, limit=100, db=db_session
    )

    assert out["count"] == 2
    assert out["by_status"] == {"running": 0, "success": 1, "failed": 1}
    newest = out["runs"][0]
    for field in ("id", "role", "status", "model", "note", "started_at",
                  "finished_at", "duration_ms", "request_hash", "evidence_count"):
        assert field in newest, f"run 出参缺字段 {field}"
    assert newest["status"] == "failed"  # 新的在前
    assert newest["model"] == "(local_fallback)"
    assert newest["duration_ms"] == 500
    assert newest["evidence_count"] == 0
    older = out["runs"][1]
    assert older["evidence_count"] == 2
    assert older["duration_ms"] == 2500
    assert older["started_at"] and older["finished_at"]


def test_get_runs_filters_by_status(db_session, project_id):
    """status 过滤只留该状态的 run。"""
    _seed_run(db_session, project_id, status="success")
    _seed_run(db_session, project_id, status="failed")

    out = assistant_router.assistant_runs(
        project_id=project_id, status="failed", limit=100, db=db_session
    )
    assert out["count"] == 1
    assert out["runs"][0]["status"] == "failed"
    assert out["status_filter"] == "failed"


def test_get_runs_rejects_invalid_status(db_session, project_id):
    """非法 status → 400（不静默返回空列表）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_runs(
            project_id=project_id, status="done", limit=100, db=db_session
        )
    assert exc.value.status_code == 400


def test_get_runs_rejects_out_of_range_limit(db_session, project_id):
    """limit 越界 → 400（不悄悄截断）。"""
    from fastapi import HTTPException

    for bad in (0, -1, 501):
        with pytest.raises(HTTPException) as exc:
            assistant_router.assistant_runs(
                project_id=project_id, status=None, limit=bad, db=db_session
            )
        assert exc.value.status_code == 400


def test_get_runs_requires_existing_project(db_session):
    """project 不存在 → 404。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_runs(
            project_id=9999, status=None, limit=100, db=db_session
        )
    assert exc.value.status_code == 404


def test_get_runs_is_read_only(db_session, project_id):
    """读日志端点不写任何表（连 AgentRun 都不开）。"""
    _seed_run(db_session, project_id)
    before = _business_snapshot(db_session)
    runs_before = db_session.scalar(select(func.count()).select_from(AgentRun))

    assistant_router.assistant_runs(
        project_id=project_id, status=None, limit=100, db=db_session
    )

    assert _business_snapshot(db_session) == before
    assert db_session.scalar(select(func.count()).select_from(AgentRun)) == runs_before


def test_get_run_detail_returns_all_evidence(db_session, project_id):
    """GET /runs/{id}：详情 + 该 run 全部 Evidence（score 转 float）。"""
    run_id = _seed_run(db_session, project_id, model="detail-model",
                       evidence_count=3)

    out = assistant_router.assistant_run_detail(
        run_id=run_id, project_id=None, db=db_session
    )
    assert out["run"]["id"] == run_id
    assert out["run"]["model"] == "detail-model"
    assert out["run"]["evidence_count"] == 3
    assert len(out["evidence"]) == 3
    for ev in out["evidence"]:
        assert isinstance(ev["score"], float)
        assert ev["kind"] == "run"
        assert ev["source"] == "agent_runs"


def test_get_run_detail_404_for_unknown_run(db_session, project_id):
    """run 不存在 → 404。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_run_detail(
            run_id=4242, project_id=None, db=db_session
        )
    assert exc.value.status_code == 404


def test_get_run_detail_404_for_cross_project(db_session, project_id):
    """别家项目的 run → 404（而不是 403：不泄漏「存在但不属于你」）。"""
    other = Project(title="别的项目", target_length=1000, mode="quick",
                    status="created")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    run_id = _seed_run(db_session, project_id)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_run_detail(
            run_id=run_id, project_id=other.id, db=db_session
        )
    assert exc.value.status_code == 404


def test_get_runs_over_http(db_session, project_id):
    """HTTP 层：GET /api/assistant/runs 200 + 分页参数生效。"""
    _seed_run(db_session, project_id)
    with TestClient(fastapi_app) as client:
        resp = client.get(f"/api/assistant/runs?project_id={project_id}&limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["runs"][0]["evidence_count"] == 1
        bad = client.get(f"/api/assistant/runs?project_id={project_id}&status=nope")
        assert bad.status_code == 400


# ====================================================================
# 交付 2 (c)：analyze-logs 五条规则
# ====================================================================


def test_analyze_logs_detects_failure_rate(db_session, project_id):
    """失败率 ≥30% 且样本足够 → run_failure_rate_anomaly。"""
    for i in range(8):
        _seed_run(db_session, project_id,
                  status="failed" if i < 3 else "success")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert "run_failure_rate_anomaly" in _rules_of(result)
    issue = next(i for i in result["issues"]
                 if i["rule"] == "run_failure_rate_anomaly")
    assert issue["severity"] in ("warn", "error")
    assert "3/8" in issue["claim"] or "38%" in issue["claim"]


def test_analyze_logs_small_sample_does_not_judge_rate(db_session, project_id):
    """样本不足 5 条不判失败率（小样本波动大，报了会训练用户无视告警）。"""
    for i in range(3):
        _seed_run(db_session, project_id, status="failed" if i == 0 else "success")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert "run_failure_rate_anomaly" not in _rules_of(result)


def test_analyze_logs_detects_blocked_attempts(db_session, project_id):
    """(blocked_by_registry) 出现即报 error——真的发生过越权尝试。"""
    _seed_run(db_session, project_id, status="failed",
              model="(blocked_by_registry)")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"]
                 if i["rule"] == "registry_blocked_attempts")
    assert issue["severity"] == "error"
    assert issue["type"] == "security"


def test_analyze_logs_detects_local_fallback(db_session, project_id):
    """(local_fallback) 出现即报 warn——用户以为助手在思考，其实在念模板。"""
    _seed_run(db_session, project_id, model="(local_fallback)")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"]
                 if i["rule"] == "local_fallback_degradation")
    assert issue["severity"] == "warn"


def test_analyze_logs_detects_empty_evidence_runs(db_session, project_id):
    """evidence 为 0 的 run 被报出（结论不可复核）。"""
    for _ in range(3):
        _seed_run(db_session, project_id, evidence_count=0)

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert "empty_evidence_runs" in _rules_of(result)


def test_analyze_logs_detects_duration_outliers(db_session, project_id):
    """耗时超过绝对上限（30s）→ run_duration_outliers（哪怕全体都慢也报）。"""
    for i in range(6):
        _seed_run(db_session, project_id, duration_s=1.0 + i * 0.1)
    _seed_run(db_session, project_id, duration_s=90.0)  # 离群

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert "run_duration_outliers" in _rules_of(result)


def test_analyze_logs_response_shape_matches_diagnose(db_session, project_id):
    """响应与 diagnose 同构：answer/issues/findings/evidence/confidence/
    agent_run_id/tool_results/rules_evaluated + runs_analyzed。"""
    _seed_run(db_session, project_id, model="(local_fallback)")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    for key in ("answer", "issues", "findings", "evidence", "confidence",
                "agent_run_id", "tool_results", "rules_evaluated", "runs_analyzed"):
        assert key in result, f"响应缺字段 {key}"
    assert result["findings"] == result["issues"]
    assert result["runs_analyzed"] == 1
    assert 0.0 <= result["confidence"] <= 1.0
    # 五条规则全部执行（runs_analyzed 与规则看到的是同一批）
    assert result["rules_evaluated"] == [
        "run_failure_rate_anomaly", "registry_blocked_attempts",
        "local_fallback_degradation", "empty_evidence_runs",
        "run_duration_outliers",
    ]
    for issue in result["issues"]:
        for field in ("id", "type", "severity", "title", "claim", "evidence",
                      "suggestion", "rule"):
            assert issue[field] not in (None, ""), f"issue.{field} 为空"
        for ev in issue["evidence"]:
            assert ev["kind"] == "run"


def test_analyze_logs_is_read_only_and_excludes_itself(db_session, project_id):
    """只读分析 + 不把自己算进被分析历史（否则 empty_evidence 会自报）。"""
    for _ in range(4):
        _seed_run(db_session, project_id)

    before = _business_snapshot(db_session)
    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )

    assert _business_snapshot(db_session) == before, "分析不得写业务表"
    assert result["runs_analyzed"] == 4, "分析必须把自己排除在外"
    # 自报检查：命中的 issue 不得引用这次分析自己的 run id
    my_run = result["agent_run_id"]
    for ev in result["evidence"]:
        assert ev["ref_id"] != str(my_run)
    # 分析自身也落 AgentRun（role=assistant，note 标明只读分析）
    run = db_session.get(AgentRun, my_run)
    assert run.role == "assistant"
    assert run.status == "success"
    assert run.note and "日志分析" in run.note


def test_analyze_logs_skips_model_when_clean_and_no_query(db_session, project_id):
    """干净历史 + 无 query → 不调模型，answer 明说这一点。"""
    _seed_run(db_session, project_id, evidence_count=2)

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert result["issues"] == []
    assert "未调用 Assistant 模型" in result["answer"]
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "(local_fallback)"


def test_analyze_logs_calls_model_when_issues_hit(db_session, project_id, monkeypatch):
    """规则命中 → 调 Assistant 模型写解释；run.model 记模型名。"""
    calls: list[tuple[str, str]] = []

    class Recorder:
        model = "log-recorder-1"

        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "（模型解释）失败集中在 provider 不可用。"

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: Recorder()
    )
    _seed_run(db_session, project_id, model="(local_fallback)")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert result["answer"] == "（模型解释）失败集中在 provider 不可用。"
    assert len(calls) == 1
    assert "local_fallback_degradation" in calls[0][1]
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "log-recorder-1"


def test_analyze_logs_query_triggers_model_even_without_issues(
    db_session, project_id, monkeypatch
):
    """带 query 但零命中 → 仍然调模型解释用户的额外问题。"""
    calls: list[tuple[str, str]] = []

    class Recorder:
        model = "log-recorder-2"

        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "（模型解释）历史里没有异常。"

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: Recorder()
    )
    _seed_run(db_session, project_id, evidence_count=2)

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(
            project_id=project_id, query="最近哪几次失败了？"
        ),
        db=db_session,
    )
    assert result["issues"] == []
    assert len(calls) == 1
    assert "最近哪几次失败了？" in calls[0][1]


def test_analyze_logs_provider_failure_degrades(db_session, project_id, monkeypatch):
    """Assistant provider 炸锅 → 纯规则降级，请求照常 200，Writer 不受影响。"""
    writer_before = llm_provider_mod._PROVIDER

    def boom():
        raise RuntimeError("simulated assistant build failure")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)
    _seed_run(db_session, project_id, model="(local_fallback)")

    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    assert "local_fallback_degradation" in _rules_of(result)
    assert "Assistant provider 不可用" in result["answer"]
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "(local_fallback)"
    assert llm_provider_mod._PROVIDER is writer_before


def test_analyze_logs_rejects_write_fields_over_http(db_session, project_id):
    """请求体携带写语义字段 → 422（StrictRequest）。"""
    _seed_run(db_session, project_id)
    with TestClient(fastapi_app) as client:
        resp = client.post("/api/assistant/analyze-logs", json={
            "project_id": project_id, "patch_json": {"text": "改"},
        })
        assert resp.status_code == 422


def test_analyze_logs_404_unknown_project(db_session):
    """project 不存在 → 404。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_analyze_logs(
            body=assistant_router.AssistantAnalyzeLogsRequest(project_id=9999),
            db=db_session,
        )
    assert exc.value.status_code == 404


# ====================================================================
# 交付 3 (e)：audit-corpus
# ====================================================================


def test_audit_corpus_detects_empty_and_short_chunks(db_session, project_id):
    """空/超短分片 → empty_or_short_chunks（定位 novel_id/chunk_id）。"""
    novel_id = _seed_novel(db_session)
    bad_id = _seed_chunk(db_session, novel_id, "短")       # < 50 字
    _seed_chunk(db_session, novel_id, "正常长度的分片内容。" * 10, idx=1)

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert "empty_or_short_chunks" in _rules_of(result)
    issue = next(i for i in result["issues"]
                 if i["rule"] == "empty_or_short_chunks")
    assert str(novel_id) in issue["claim"]
    refs = [ev["ref_id"] for ev in issue["evidence"]]
    assert str(bad_id) in refs
    assert all(ev["kind"] == "chunk" for ev in issue["evidence"])


def test_audit_corpus_detects_duplicate_chunks(db_session, project_id):
    """文本完全相同的分片 → duplicate_chunks。"""
    novel_id = _seed_novel(db_session)
    text = "完全相同的两段原文，用于验证重复分片判据。"
    _seed_chunk(db_session, novel_id, text, idx=0)
    _seed_chunk(db_session, novel_id, text, idx=1)

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert "duplicate_chunks" in _rules_of(result)


def test_audit_corpus_detects_missing_style_profile(db_session, project_id):
    """语料未拟合 style_profile → missing_style_profile（info，不影响可用性）。"""
    _seed_novel(db_session, style_profile=None)

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"]
                 if i["rule"] == "missing_style_profile")
    assert issue["severity"] == "info"
    assert issue["evidence"] and issue["evidence"][0]["kind"] == "novel"


def test_audit_corpus_detects_chunks_without_embedding(db_session, project_id):
    """未建 embedding 的分片 → chunks_without_embedding（向量路失效）。"""
    novel_id = _seed_novel(db_session)
    _seed_chunk(db_session, novel_id, "没有向量的分片内容。" * 10)

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert "chunks_without_embedding" in _rules_of(result)


def test_audit_corpus_scopes_to_given_novel_ids(db_session, project_id):
    """显式 novel_ids → 只审这些（前端可先审项目引用的）。"""
    a = _seed_novel(db_session, title="语料A")
    b = _seed_novel(db_session, title="语料B")
    _seed_chunk(db_session, a, "短")
    _seed_chunk(db_session, b, "短")

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(
            project_id=project_id, novel_ids=[a]),
        db=db_session,
    )
    assert result["tool_results"]["novels_audited"] == 1
    assert result["tool_results"]["chunks_audited"] == 1
    # 精确断言：不能写 `str(b) not in payload`——数字 2 会撞上「1200 字/片」
    # 这类无关文案。语料规则的 evidence.source 一律是 `novel:{id}`，据此判。
    payload = json_dumps(result)
    assert f"novel:{a}" in payload
    assert f"novel:{b}" not in payload


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


def test_audit_corpus_404_unknown_novel(db_session, project_id):
    """novel_ids 里有不存在的语料 → 404（不静默审个空集）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        audit_router.assistant_audit_corpus(
            body=audit_router.AssistantAuditCorpusRequest(
                project_id=project_id, novel_ids=[9999]),
            db=db_session,
        )
    assert exc.value.status_code == 404


def test_audit_corpus_is_read_only(db_session, project_id):
    """语料审核只读：RawNovel/NovelChunk 行数与内容不变。"""
    _seed_novel(db_session)
    _seed_chunk(db_session, _seed_novel(db_session, title="另一本"), "短")

    def snapshot() -> tuple:
        return (
            db_session.scalar(select(func.count()).select_from(RawNovel)),
            db_session.scalar(select(func.count()).select_from(NovelChunk)),
            sorted((c.id, c.novel_id, c.idx, (c.text or "")[:20])
                   for c in db_session.scalars(select(NovelChunk)).all()),
        )

    before = snapshot()
    audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    db_session.expire_all()
    assert snapshot() == before


# ====================================================================
# 交付 3 (e)：audit-kg
# ====================================================================


def test_audit_kg_detects_orphan_nodes(db_session, project_id):
    """无边连接的 KG 节点 → orphan_kg_nodes。"""
    _seed_character_node(db_session, project_id, "独孤求败")

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "orphan_kg_nodes" in _rules_of(result)
    issue = next(i for i in result["issues"] if i["rule"] == "orphan_kg_nodes")
    assert issue["evidence"] and issue["evidence"][0]["kind"] == "kg"


def test_audit_kg_name_conflict_matches_diagnose(db_session, project_id):
    """KG 审核的 name_conflict 与 diagnose 同口径（复用同一规则函数）。

    判据：同一份库，两个端点对「别名独立成节点」必须给出一致的结论——
    复制出来的第二套判据一定会和第一套漂移，这是复用而非重写的原因。
    """
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_character_node(db_session, project_id, "宁折")

    audit_result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    audit_issue = next(i for i in audit_result["issues"]
                       if i["rule"] == "name_conflict")

    diag_result = assistant_router.assistant_diagnose(
        body=assistant_router.AssistantDiagnoseRequest(
            project_id=project_id, scope="all"),
        db=db_session,
    )
    diag_issue = next(i for i in diag_result["issues"]
                      if i["rule"] == "name_conflict")

    assert audit_issue["id"] == diag_issue["id"]
    assert audit_issue["claim"] == diag_issue["claim"]
    assert audit_issue["severity"] == diag_issue["severity"]


def test_audit_kg_detects_dead_character_open_holes(db_session, project_id):
    """哨兵标记已死 + 未填坑仍挂着该角色 → dead_character_open_holes。"""
    db_session.add(Fact(
        project_id=project_id, type="state", key="life:陆见微",
        props={"value": "dead", "since_segment": 2, "text": "陆见微已经死了。"},
        aliases=[], source="generated", confidence="high",
        first_segment=2, last_segment=2,
    ))
    db_session.commit()
    db_session.add(KgNode(
        project_id=project_id, type="plothole",
        props={"question": "陆见微的玉佩究竟意味着什么？", "status": "open"},
        first_chapter=1, last_chapter=1, source="generated",
    ))
    db_session.commit()

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "dead_character_open_holes" in _rules_of(result)


def test_audit_kg_detects_cooccurrence_without_edge(db_session, project_id):
    """两个角色在多个相同段落出现但彼此无边 → cooccurrence_without_edge。"""
    _seed_character_node(db_session, project_id, "甲大")
    _seed_character_node(db_session, project_id, "乙小")
    _seed_segment(db_session, project_id, 1, "甲大和乙小一起走进客栈。")
    _seed_segment(db_session, project_id, 2, "甲大看了乙小一眼，没有说话。")

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "cooccurrence_without_edge" in _rules_of(result)
    issue = next(i for i in result["issues"]
                 if i["rule"] == "cooccurrence_without_edge")
    assert issue["severity"] == "info"  # 共现不等于有关系，只让人看一眼


def test_audit_kg_does_not_report_linked_pairs(db_session, project_id):
    """已有边的共现对不报（判据是「关系漏建」，不是「有共现」）。"""
    a = _seed_character_node(db_session, project_id, "甲大")
    b = _seed_character_node(db_session, project_id, "乙小")
    # KgEdge 没有 project_id 列：边经 from_id/to_id 挂在节点上，项目归属
    # 由节点决定（见 models.py）
    db_session.add(KgEdge(from_id=a, to_id=b, type="相识", props={}))
    db_session.commit()
    _seed_segment(db_session, project_id, 1, "甲大和乙小一起走进客栈。")
    _seed_segment(db_session, project_id, 2, "甲大看了乙小一眼。")

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "cooccurrence_without_edge" not in _rules_of(result)


def test_audit_kg_is_read_only(db_session, project_id):
    """KG 审核只写自己的审计表，不碰 KgNode/KgEdge/Fact/Generation。"""
    _seed_character_node(db_session, project_id, "孤独求败")
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_segment(db_session, project_id, 1, "宁折枝推开院门。")

    before = _business_snapshot(db_session)
    audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    db_session.expire_all()
    assert _business_snapshot(db_session) == before


# ====================================================================
# 交付 3 (e)：audit-generation
# ====================================================================


def test_audit_generation_detects_empty_text(db_session, project_id):
    """段落正文为空 → empty_segment_text（error，脏数据）。"""
    _seed_segment(db_session, project_id, 1, "   ")

    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"]
                 if i["rule"] == "empty_segment_text")
    assert issue["severity"] == "error"


def test_audit_generation_detects_length_outliers(db_session, project_id):
    """段长远超/远低于 generation_length → segment_length_outliers。"""
    _seed_segment(db_session, project_id, 1, "只有十几个字的极短段落。")

    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    assert "segment_length_outliers" in _rules_of(result)
    issue = next(i for i in result["issues"]
                 if i["rule"] == "segment_length_outliers")
    assert "3000" in issue["claim"]  # 以项目设定为基准


def test_audit_generation_no_segments_reports_info(db_session, project_id):
    """项目没有任何生成段 → no_generations（info，不是 error）。"""
    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"] if i["rule"] == "no_generations")
    assert issue["severity"] == "info"


def test_audit_generation_detects_bible_character_absent(db_session, project_id):
    """plot_bible 登记的角色在全部生成段零出现 → bible_character_absent。"""
    proj = db_session.get(Project, project_id)
    proj.plot_bible = {"characters": [{"name": "从未出场的角色"}]}
    db_session.commit()
    _seed_segment(db_session, project_id, 1, "晨光漫过窗台，故事继续推进。" * 30)

    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    assert "bible_character_absent" in _rules_of(result)


def test_audit_generation_detects_character_not_in_bible(db_session, project_id):
    """高频出场角色不在 plot_bible → generation_character_not_in_bible（info）。"""
    proj = db_session.get(Project, project_id)
    proj.plot_bible = {"characters": [{"name": "陆见微"}]}
    db_session.commit()
    _seed_character_node(db_session, project_id, "新角色", mentions=9)
    _seed_segment(db_session, project_id, 1, "陆见微推开院门。" * 40)

    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    issue = next(i for i in result["issues"]
                 if i["rule"] == "generation_character_not_in_bible")
    assert issue["severity"] == "info"
    assert "新角色" in issue["claim"]


def test_audit_generation_clean_project_has_no_issues(db_session, project_id):
    """干净项目（正常段长 + 角色已登记）→ 不误报。"""
    proj = db_session.get(Project, project_id)
    proj.plot_bible = {"characters": [{"name": "陆见微"}]}
    db_session.commit()
    _seed_character_node(db_session, project_id, "陆见微")
    _seed_segment(db_session, project_id, 1, "陆见微推开院门，晨雾未散。" * 200)

    result = audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )
    assert result["issues"] == []
    assert "未调用 Assistant 模型" in result["answer"]


def test_audit_endpoints_share_unified_response_shape(db_session, project_id):
    """三个审核端点响应同构（前端一份渲染逻辑复用）。"""
    _seed_novel(db_session)
    _seed_character_node(db_session, project_id, "孤独求败")
    _seed_segment(db_session, project_id, 1, "正常段落。" * 100)

    results = [
        audit_router.assistant_audit_corpus(
            body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
            db=db_session,
        ),
        audit_router.assistant_audit_kg(
            body=audit_router.AssistantAuditKGRequest(project_id=project_id),
            db=db_session,
        ),
        audit_router.assistant_audit_generation(
            body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
            db=db_session,
        ),
    ]
    for result in results:
        for key in ("answer", "issues", "findings", "evidence", "confidence",
                    "agent_run_id", "tool_results", "rules_evaluated"):
            assert key in result, f"审核响应缺字段 {key}"
        assert result["findings"] == result["issues"]
        run = db_session.get(AgentRun, result["agent_run_id"])
        assert run.role == "assistant" and run.status == "success"
        assert run.note  # 调用性质留痕


def test_audit_endpoints_reject_write_fields_over_http(db_session, project_id):
    """审核端点同样继承 StrictRequest：写语义字段 → 422。"""
    with TestClient(fastapi_app) as client:
        for path in ("/api/assistant/audit-corpus", "/api/assistant/audit-kg",
                     "/api/assistant/audit-generation"):
            resp = client.post(path, json={
                "project_id": project_id, "commit_hash": "deadbeef",
            })
            assert resp.status_code == 422, f"{path} 应拒 commit_hash"


def test_audit_endpoints_404_unknown_project(db_session):
    """project 不存在 → 404。"""
    from fastapi import HTTPException

    bodies = [
        (audit_router.assistant_audit_corpus,
         audit_router.AssistantAuditCorpusRequest(project_id=9999)),
        (audit_router.assistant_audit_kg,
         audit_router.AssistantAuditKGRequest(project_id=9999)),
        (audit_router.assistant_audit_generation,
         audit_router.AssistantAuditGenerationRequest(project_id=9999)),
    ]
    for fn, body in bodies:
        with pytest.raises(HTTPException) as exc:
            fn(body=body, db=db_session)
        assert exc.value.status_code == 404


def test_audit_calls_model_only_when_issues_or_query(db_session, project_id, monkeypatch):
    """规则命中 → 调模型；provider 炸锅 → 纯规则降级（请求不失败）。"""
    calls: list[tuple[str, str]] = []

    class Recorder:
        model = "audit-recorder-1"

        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "（模型解释）建议清理重复分片。"

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: Recorder()
    )
    novel_id = _seed_novel(db_session)
    text = "重复内容重复内容重复内容。"
    _seed_chunk(db_session, novel_id, text, idx=0)
    _seed_chunk(db_session, novel_id, text, idx=1)

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert len(calls) == 1
    assert result["answer"] == "（模型解释）建议清理重复分片。"
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "audit-recorder-1"

    # provider 炸锅 → 纯规则降级
    def boom():
        raise RuntimeError("assistant down")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)
    # registry 与 Assistant 单例缓存里还留着上一个 Recorder——必须显式失效，
    # 否则第二次调用命中缓存拿到旧实例，「provider 炸锅」场景根本不会发生
    reset_provider_registry()
    llm_provider_mod.reset_assistant_provider_cache()
    result2 = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert "duplicate_chunks" in _rules_of(result2)
    assert "Assistant provider 不可用" in result2["answer"]
    run2 = db_session.get(AgentRun, result2["agent_run_id"])
    assert run2.model == "(local_fallback)"


# ====================================================================
# 交付 4 (h)(i)：invoke-generation 复用引擎 + 留痕
# ====================================================================


def _invoke(db, **kwargs) -> dict:
    body = gen_router.AssistantInvokeGenerationRequest(**kwargs)
    return gen_router.assistant_invoke_generation(body=body, db=db)


def test_invoke_generation_generate_next_reuses_engine(db_session, project_id):
    """generate_next：走引擎落库一段，响应回显 Writer 模型。"""
    before = db_session.scalar(select(func.count()).select_from(Generation))

    result = _invoke(db_session, project_id=project_id, action="generate_next",
                     instruction="继续写下一段，写主角夜探祠堂")

    after = db_session.scalar(select(func.count()).select_from(Generation))
    assert after == before + 1, "必须通过引擎新增一段（不绕过、也不多写）"
    seg = result["segment"]
    assert seg["segment_no"] == 1
    # 文本来自 Writer 单例（本地兜底 provider 的确定性模板，不是空壳）
    assert seg["text"] and "离线兜底" in seg["text"]
    assert result["writer_model"] == "(local_fallback)"
    assert result["chars"] == len(seg["text"])
    assert "第 1 段" in result["answer"]

    # 引擎副作用照常发生（图谱合并 + 摘要树），证明走的是同一条链路
    assert result["kg_update"] is not None
    assert result["summary_tree"] is not None
    assert result["debug"]["system_prompt"]  # 下发过的提示词可复核


def test_invoke_generation_records_assistant_run(db_session, project_id):
    """落 role='assistant' 的 AgentRun：note 写明「这次生成是助手调用的」。"""
    result = _invoke(db_session, project_id=project_id, action="generate_next",
                     instruction="写一段雨夜追逐")

    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run is not None
    assert run.role == "assistant"
    assert run.status == "success"
    assert run.project_id == project_id
    assert "这次生成是助手调用的" in run.note
    assert "写一段雨夜追逐" in run.note
    # model 回显 Writer 实际模型（与「助手模型」区分：note 才是调用来源）
    assert run.model == result["writer_model"]
    # Evidence 指向产出的段：助手日志详情页能顺着跳到生成段
    rows = db_session.scalars(
        select(Evidence).where(Evidence.run_id == run.id)
    ).all()
    assert rows and all(r.kind == "generation" for r in rows)
    assert rows[0].ref_id == str(result["segment"]["id"])


def test_invoke_generation_regenerate_reuses_stream_engine(db_session, project_id):
    """regenerate_segment：走引擎的流式路径，只替换目标段。"""
    seg_id = _seed_segment(db_session, project_id, 1, "这是第一段的旧正文。" * 20)

    result = _invoke(db_session, project_id=project_id,
                     action="regenerate_segment", segment_no=1,
                     instruction="重写第一段，加强氛围")

    seg = db_session.get(Generation, seg_id)
    assert seg.text != "这是第一段的旧正文。" * 20, "旧正文必须被替换"
    assert "离线兜底" in seg.text
    assert result["segment"]["id"] == seg_id
    assert result["segment"]["segment_no"] == 1
    # 段数不变（重生成不是新增）
    count = db_session.scalar(select(func.count()).select_from(Generation))
    assert count == 1

    run = db_session.get(AgentRun, result["agent_run_id"])
    assert "这次生成是助手调用的" in run.note
    assert "segment_no=1" in run.note


def test_invoke_generation_requires_explicit_instruction(db_session, project_id):
    """无 instruction → 422（生成必须是显式用户指令，不是内部副作用）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _invoke(db_session, project_id=project_id, action="generate_next",
                instruction="   ")

    with pytest.raises(ValidationError):
        _invoke(db_session, project_id=project_id, action="generate_next",
                instruction="")


def test_invoke_generation_validates_action_fields(db_session, project_id):
    """动作与字段必须配套：regenerate 要 segment_no；generate_next 不要。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _invoke(db_session, project_id=project_id,
                action="regenerate_segment", instruction="重写第 2 段")

    with pytest.raises(ValidationError):
        _invoke(db_session, project_id=project_id, action="generate_next",
                segment_no=1, instruction="续写一段")


def test_invoke_generation_rejects_unknown_action(db_session, project_id):
    """未知 action → 422（不静默按默认动作执行）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _invoke(db_session, project_id=project_id, action="delete_all",
                instruction="删掉全部")


def test_invoke_generation_404_missing_segment(db_session, project_id):
    """目标段不存在 → 404，且不留半开的 AgentRun。"""
    from fastapi import HTTPException

    _seed_segment(db_session, project_id, 1, "第一段。")
    runs_before = db_session.scalar(select(func.count()).select_from(AgentRun))

    with pytest.raises(HTTPException) as exc:
        _invoke(db_session, project_id=project_id,
                action="regenerate_segment", segment_no=7,
                instruction="重写第 7 段")
    assert exc.value.status_code == 404
    assert db_session.scalar(
        select(func.count()).select_from(AgentRun)) == runs_before, (
        "404 发生在开 run 之前，不该留下空跑的审计记录"
    )


def test_invoke_generation_404_unknown_project(db_session):
    """project 不存在 → 404。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _invoke(db_session, project_id=9999, action="generate_next",
                instruction="写一段")
    assert exc.value.status_code == 404


def test_invoke_generation_marks_run_failed_on_provider_error(
    db_session, project_id, monkeypatch
):
    """Writer 炸锅 → run 记 failed + 5xx，不返回假的 200。"""
    from fastapi import HTTPException

    class BoomProvider:
        model = "boom-writer"

        def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
            raise RuntimeError("writer chat exploded")

        def summarize(self, text: str) -> str:
            return text[:50]

        def stream_chat(self, system: str, user: str, constraints: dict | None = None):
            raise RuntimeError("writer stream exploded")
            yield  # pragma: no cover - 让它成为生成器

    monkeypatch.setattr(llm_provider_mod, "_PROVIDER", BoomProvider())

    with pytest.raises(HTTPException) as exc:
        _invoke(db_session, project_id=project_id, action="generate_next",
                instruction="写一段会失败的")
    assert exc.value.status_code == 500

    run = db_session.scalars(select(AgentRun)).first()
    assert run is not None, "失败也必须留痕"
    assert run.status == "failed"
    assert "失败" in run.note
    # 失败不得落库任何生成段
    assert db_session.scalar(select(func.count()).select_from(Generation)) == 0


def test_invoke_generation_does_not_bypass_engine(db_session, project_id):
    """静态纪律：invoke-generation 路由不直接构造 Generation（不绕过引擎）。"""
    import inspect

    src = inspect.getsource(gen_router)
    assert "Generation(" not in src, (
        "路由层直写 Generation = 绕过角色锁定/摘要树/图谱闭环（b58c763 红线）"
    )
    assert "generate_next_segment" in src
    assert "regenerate_segment_stream" in src


def test_tool_registry_has_no_write_tools(db_session):
    """助手工具注册表只有只读工具；write_* 调用被 PermissionError 拒绝。"""
    from app.agents.registry import get_tool_registry

    reg = get_tool_registry()
    for name in reg.names():
        assert not name.startswith(("write_", "commit_", "delete_", "update_")), (
            f"注册表出现直写工具 {name}——助手不该有后门"
        )
    assert set(reg.names()) == set(ALLOWED_READ_ONLY_TOOLS)
    with pytest.raises(PermissionError):
        invoke_tool("write_generation", db_session, project_id=1, text="x")


def test_read_and_audit_endpoints_never_generate(db_session, project_id):
    """没有显式指令就不会生成：只读/审核端点零 Generation 写入。"""
    _seed_run(db_session, project_id)
    _seed_novel(db_session)
    _seed_character_node(db_session, project_id, "孤独求败")
    _seed_segment(db_session, project_id, 1, "第一段。" * 100)
    before = db_session.scalar(select(func.count()).select_from(Generation))

    assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(project_id=project_id),
        db=db_session,
    )
    audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    audit_router.assistant_audit_generation(
        body=audit_router.AssistantAuditGenerationRequest(project_id=project_id),
        db=db_session,
    )

    assert db_session.scalar(
        select(func.count()).select_from(Generation)) == before


def test_invoke_generation_over_http(db_session, project_id):
    """HTTP 层：合法请求 200；缺 instruction → 422。"""
    with TestClient(fastapi_app) as client:
        ok = client.post("/api/assistant/invoke-generation", json={
            "project_id": project_id, "action": "generate_next",
            "instruction": "续写一段夜色中的告别",
        })
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["segment"]["text"]
        assert "这次生成是助手调用的" in (
            db_session.get(AgentRun, body["agent_run_id"]).note or ""
        )

        bad = client.post("/api/assistant/invoke-generation", json={
            "project_id": project_id, "action": "generate_next",
        })
        assert bad.status_code == 422


def test_audit_corpus_empty_novel_ids_is_rejected_not_broadcast(db_session, project_id):
    """`novel_ids=[]` 必须 422，**不能**被当成「审全部」。

    回归自 commit 45fcde1 时代的静默范围放大：路由层 `_require_novels` 对 `[]`
    返回 `[]`，`run_corpus_audit` 又写 `if novel_ids:`——空列表 falsy，于是
    `[]` 与 `None` 撞进同一分支，把「一本都没选」执行成「广播审全库」。
    真库 7941 分片时后果不小，且调用方完全看不出来。
    """
    a = _seed_novel(db_session, title="语料A")
    b = _seed_novel(db_session, title="语料B")
    _seed_chunk(db_session, a, "短")
    _seed_chunk(db_session, b, "短")

    from fastapi import HTTPException

    # 空列表：必须在路由层就被拒，不得进入审核逻辑
    with pytest.raises(HTTPException) as exc:
        audit_router.assistant_audit_corpus(
            body=audit_router.AssistantAuditCorpusRequest(
                project_id=project_id, novel_ids=[]),
            db=db_session,
        )
    assert exc.value.status_code == 422

    # 反证：不传 novel_ids 仍是「审全部」，既有语义不能被本次修复改变
    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    assert result["tool_results"]["novels_audited"] == 2

    # 反证：给了 id 仍只审这些
    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(
            project_id=project_id, novel_ids=[a]),
        db=db_session,
    )
    assert result["tool_results"]["novels_audited"] == 1


def test_audit_kg_short_name_does_not_ride_on_long_name(db_session, project_id):
    """短名不得搭长名的便车：只有「冷轩月」出现时，「冷轩」不算命中。

    回归 P2（QA8 发现）：`rule_kg_cooccurrence_without_edge` 的注释写着
    「长名优先匹配，避免张三抢先命中张三丰的段」，但实现是
    `[n for n in names_sorted if n in text]`——对每个名字独立做子串判断、
    不挖掉已匹配区间，于是「冷轩月」出现时「冷轩」同样算命中，报出实际
    从未出现过的角色对。
    """
    _seed_character_node(db_session, project_id, "冷轩")
    _seed_character_node(db_session, project_id, "冷轩月")
    # 两段里都只有「冷轩月」，从没有独立的「冷轩」
    _seed_segment(db_session, project_id, 1, "冷轩月推门进来，扫了一圈。")
    _seed_segment(db_session, project_id, 2, "冷轩月把剑放在桌上，没有说话。")

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "cooccurrence_without_edge" not in _rules_of(result), (
        "只出现长名「冷轩月」时不该报出 (冷轩月, 冷轩) 的对——短名从未独立出现"
    )


def test_audit_kg_both_names_independently_present_still_reported(db_session, project_id):
    """反证：短名**真的独立出现**时仍要报（别把修复做成漏报）。"""
    _seed_character_node(db_session, project_id, "冷轩")
    _seed_character_node(db_session, project_id, "冷轩月")
    _seed_segment(db_session, project_id, 1, "冷轩月推门进来，冷轩抬头看了她一眼。")
    _seed_segment(db_session, project_id, 2, "冷轩把剑放在桌上，冷轩月没有说话。")

    result = audit_router.assistant_audit_kg(
        body=audit_router.AssistantAuditKGRequest(project_id=project_id),
        db=db_session,
    )
    assert "cooccurrence_without_edge" in _rules_of(result)


def test_analyze_logs_rejects_bogus_status(db_session, project_id):
    """`analyze-logs` 的 status 必须与 `GET /runs` 同口径 fail loud。

    回归 P2（QA8 发现）：本端点原先不校验 status，拼错 → 200 且
    runs_analyzed=0，读起来像「这段时间没有调用记录」，而真实原因是拼写
    错了——这类静默失效比报错难查得多。
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_analyze_logs(
            body=assistant_router.AssistantAnalyzeLogsRequest(
                project_id=project_id, status="totally-bogus"),
            db=db_session,
        )
    assert exc.value.status_code == 400


def test_analyze_logs_rejects_out_of_range_limit(db_session, project_id):
    """`limit=-1` 曾在 SQLite 变成 `LIMIT -1`（= 无上限，取全表）。

    回归 P2（QA8 发现）：`/runs` 对同样参数 400，`analyze-logs` 却 200。
    """
    from fastapi import HTTPException

    for bad in (-1, 0, 10**6):
        with pytest.raises(HTTPException) as exc:
            assistant_router.assistant_analyze_logs(
                body=assistant_router.AssistantAnalyzeLogsRequest(
                    project_id=project_id, limit=bad),
                db=db_session,
            )
        assert exc.value.status_code == 400, f"limit={bad} 应 400"


def test_analyze_logs_valid_params_still_work(db_session, project_id):
    """反证：合法参数不受本次校验影响（别把修复做成全拒）。"""
    _seed_run(db_session, project_id, status="success")
    result = assistant_router.assistant_analyze_logs(
        body=assistant_router.AssistantAnalyzeLogsRequest(
            project_id=project_id, status="success", limit=50),
        db=db_session,
    )
    assert "issues" in result


def test_audit_corpus_whitespace_chunk_snippet_shows_placeholder(db_session, project_id):
    """空白分片（如 "   "）的 evidence.snippet 应显示占位符而不是空串。

    回归 P2-4（QA8 发现，nitpick）：旧写法 `_truncate(c.text or "(空)")`，
    而 `"   "` 是 truthy，`_truncate` 内部再 strip 就得到 `""` —— 占位符
    永远显示不出来。修法是把兜底放到 strip **之后**：
    `_truncate(c.text or "") or "(空)"`。
    """
    nid = _seed_novel(db_session, title="空白分片语料")
    _seed_chunk(db_session, nid, "   ")

    result = audit_router.assistant_audit_corpus(
        body=audit_router.AssistantAuditCorpusRequest(project_id=project_id),
        db=db_session,
    )
    snippets = [
        ev["snippet"]
        for issue in result["issues"]
        for ev in issue.get("evidence", [])
        if ev.get("kind") == "chunk" and ev.get("source") == f"novel:{nid}"
    ]
    assert snippets, "空白分片应被 empty_or_short_chunks 检出"
    assert all(s == "(空)" for s in snippets), f"snippet 应为占位符，实得 {snippets}"
