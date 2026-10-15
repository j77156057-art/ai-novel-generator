"""Assistant 只读结构化诊断测试（ADR-2026-10-12 第 5 步第 3 步验收）。

覆盖 team-lead 派单列出的六项验收：

  (a) 规则命中 → 产出结构化 issue（九字段齐全）；
  (b) 无问题 → `issues == []`；
  (c) provider 失败 → 降级为纯规则结果，请求不失败，**Writer 单例不变**；
  (d) 请求体携带 `patch_json` / `commit_hash` → 422（HTTP 层与模型层都验）；
  (e) evidence 的 source 隔离：`generated` 与 `novel:{id}` 不串；
  (f) 响应字段完整性（顶层必备键 + issue 结构 + Evidence 落库语义）。

外加两条：
  (g) 新增的两条规则（`name_conflict` / `dead_character_reappearance`）可复现；
  (h) 诊断全程不写业务表（Generation / Fact / KgNode 行数与调用前一致）。

不依赖真实 DB / 真实 LLM：内存 sqlite（Base.metadata.create_all）+
monkeypatch provider 工厂。`test_assistant_readonly.py` 已建立同样的隔离范式，
本文件沿用它的 fixture 约定，避免两套测试互相污染单例缓存。
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
from app.agents.diagnosis import (  # noqa: E402
    ALLOWED_EVIDENCE_KINDS,
    ALLOWED_SEVERITIES,
    RULE_ORDER,
    DiagnosticIssue,
    IssueEvidence,
    run_diagnostic_rules,
)
from app.agents.registry import reset_tool_registry  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    Evidence,
    Fact,
    Generation,
    KgNode,
    NovelChunk,
    Project,
    RawNovel,
)
from app.routers import assistant as assistant_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures：内存库 + provider 单例隔离
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空 provider / tool 注册表与内存库。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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
    proj = Project(title="诊断测试项目", target_length=5000, mode="quick", status="created")
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


def _diagnose(project_id: int, db, **kwargs):
    """构造请求体并直接调 router 函数（与 test_assistant_readonly 的调用方式一致）。"""
    from app.routers.assistant import AssistantDiagnoseRequest

    body = AssistantDiagnoseRequest(project_id=project_id, **kwargs)
    return assistant_router.assistant_diagnose(body=body, db=db)


def _seed_open_plothole(db, project_id: int, question: str, chapter: int = 3) -> int:
    node = KgNode(
        project_id=project_id,
        type="plothole",
        props={"question": question, "status": "open"},
        first_chapter=chapter,
        last_chapter=chapter,
        source="generated",
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node.id


def _seed_character_node(db, project_id: int, name: str) -> int:
    node = KgNode(
        project_id=project_id,
        type="character",
        props={"name": name, "mentions": 4},
        first_chapter=1,
        last_chapter=2,
        source="generated",
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node.id


def _seed_character_fact(db, project_id: int, key: str, aliases: list[str] | None = None,
                         source: str = "generated", confidence: str = "high") -> int:
    fact = Fact(
        project_id=project_id, type="character", key=key,
        props={"mentions": 4, "speaking": True, "named": True},
        aliases=list(aliases or []), source=source, confidence=confidence,
        first_segment=1, last_segment=2,
    )
    db.add(fact)
    db.commit()
    db.refresh(fact)
    return fact.id


def _seed_segment(db, project_id: int, segment_no: int, text: str) -> int:
    gen = Generation(
        project_id=project_id, segment_no=segment_no,
        chapter_range=f"{segment_no}-{segment_no}", text=text,
        summary={"short": f"第 {segment_no} 段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen.id


# ====================================================================
# (a) 规则命中 → 结构化 issue
# ====================================================================


def test_diagnose_returns_structured_issues(db_session, project_id):
    """命中未填坑规则 → issues 非空，且每条九字段齐全、severity 合法。"""
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")

    result = _diagnose(project_id, db_session, scope="all")

    assert result["issues"], "有未填坑时 issues 不应为空"
    issue = next(i for i in result["issues"] if i["rule"] == "open_plothole_accumulation")
    for field in ("id", "type", "severity", "title", "claim", "evidence",
                  "suggestion", "rule"):
        assert field in issue, f"issue 缺字段 {field}"
        assert issue[field] not in (None, ""), f"issue.{field} 为空"
    assert issue["severity"] in ALLOWED_SEVERITIES
    assert issue["type"] == "plothole"
    assert isinstance(issue["evidence"], list) and issue["evidence"], "issue 必须挂证据"
    for ev in issue["evidence"]:
        assert ev["kind"] in ALLOWED_EVIDENCE_KINDS
        assert 0.0 <= ev["score"] <= 1.0


def test_all_four_rules_are_registered():
    """规则表必须含第 2 步两条 + 第 3 步新增两条，且顺序稳定（决定展示顺序）。"""
    assert RULE_ORDER == (
        "open_plothole_accumulation",
        "low_confidence_facts_overflow",
        "name_conflict",
        "dead_character_reappearance",
    )


def test_low_confidence_facts_overflow_fires(db_session, project_id):
    """低置信事实 > 阈值 → 命中（且统计口径覆盖全部 Fact 类型，不只 character）。"""
    for i in range(7):
        db_session.add(Fact(
            project_id=project_id, type="relationship", key=f"甲→乙{i}",
            props={}, aliases=[], source="generated", confidence="low",
            first_segment=1, last_segment=1,
        ))
    db_session.commit()

    result = _diagnose(project_id, db_session, scope="all")
    rules = {i["rule"] for i in result["issues"]}
    assert "low_confidence_facts_overflow" in rules


def test_name_conflict_detects_alias_split_from_kg(db_session, project_id):
    """新规则 ①：账本 aliases 与 KG 角色节点名交叉 —— 别名独立成节点即报。"""
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_character_node(db_session, project_id, "宁折")  # 图谱没跟上归并

    result = _diagnose(project_id, db_session, scope="all")
    issues = [i for i in result["issues"] if i["rule"] == "name_conflict"]
    assert issues, "别名已独立成 KG 节点时必须报 name_conflict"
    assert "宁折" in issues[0]["claim"]
    kinds = {ev["kind"] for ev in issues[0]["evidence"]}
    assert kinds == {"fact", "kg"}


def test_name_conflict_detects_alias_colliding_with_canonical(db_session, project_id):
    """新规则 ②形态：别名与另一条规范名撞名 → error 级（方向不可判，交人裁决）。"""
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_character_fact(db_session, project_id, "宁折")

    result = _diagnose(project_id, db_session, scope="all")
    issues = [i for i in result["issues"] if i["rule"] == "name_conflict"]
    assert issues, "别名撞规范名必须报"
    assert any(i["severity"] == "error" for i in issues)


def test_dead_character_reappearance_fires(db_session, project_id):
    """新规则 ③：哨兵状态表标记死亡后，后续生成段再次出现该角色 → 报。"""
    db_session.add(Fact(
        project_id=project_id, type="state", key="life:陆见微",
        props={"value": "dead", "since_segment": 2,
               "text": "陆见微已经死了。", "scale": None, "order": None,
               "span": [10, 18]},
        aliases=[], source="generated", confidence="high",
        first_segment=2, last_segment=2,
    ))
    db_session.commit()
    _seed_segment(db_session, project_id, 1, "陆见微推开院门。")
    _seed_segment(db_session, project_id, 2, "陆见微已经死了。")
    _seed_segment(db_session, project_id, 5, "陆见微又出现在门口，衣袖带血。")

    result = _diagnose(project_id, db_session, scope="all")
    issues = [i for i in result["issues"] if i["rule"] == "dead_character_reappearance"]
    assert issues, "死亡后再次出现必须报"
    assert issues[0]["severity"] == "error"
    assert "陆见微" in issues[0]["title"]
    # 证据 snippet 必须是库中正文的**原样子串**，不是复述
    gen_ev = [e for e in issues[0]["evidence"] if e["kind"] == "generation"]
    assert gen_ev, "必须挂生成正文证据"
    assert "陆见微又出现在门口" in gen_ev[0]["snippet"]


def test_dead_character_not_reported_when_absent_after_death(db_session, project_id):
    """死亡后不再出现 → 不报（宁可漏报也不误报，延续 sentinel 的统一取舍）。"""
    db_session.add(Fact(
        project_id=project_id, type="state", key="life:陆见微",
        props={"value": "dead", "since_segment": 3, "text": "陆见微死了。"},
        aliases=[], source="generated", confidence="high",
        first_segment=3, last_segment=3,
    ))
    db_session.commit()
    _seed_segment(db_session, project_id, 4, "天光大亮，街上空无一人。")

    result = _diagnose(project_id, db_session, scope="all")
    assert not [i for i in result["issues"] if i["rule"] == "dead_character_reappearance"]


# ====================================================================
# (b) 无问题 → 空 issues
# ====================================================================


def test_diagnose_returns_empty_issues_when_nothing_wrong(db_session, project_id):
    """干净项目（无坑 / 无低置信事实 / 无别名冲突 / 无死亡记录）→ issues == []。"""
    _seed_character_fact(db_session, project_id, "陆见微")
    _seed_segment(db_session, project_id, 1, "陆见微推开院门，晨雾未散。")

    result = _diagnose(project_id, db_session, scope="all")
    assert result["issues"] == []
    assert result["findings"] == [], "findings 是 issues 的兼容别名，必须同步为空"
    assert result["evidence"] == []
    # 未命中且无 query → 不调模型，answer 明说这一点
    assert "未调用 Assistant 模型" in result["answer"]


def test_low_confidence_below_threshold_does_not_fire(db_session, project_id):
    """低置信事实恰好等于阈值（5 条）→ 不报（判据是「超过」而非「达到」）。"""
    for i in range(5):
        db_session.add(Fact(
            project_id=project_id, type="character", key=f"角色{i}",
            props={}, aliases=[], source="generated", confidence="low",
            first_segment=1, last_segment=1,
        ))
    db_session.commit()

    result = _diagnose(project_id, db_session, scope="all")
    assert not [i for i in result["issues"] if i["rule"] == "low_confidence_facts_overflow"]


# ====================================================================
# (c) provider 失败 → 降级 + Writer 单例不变
# ====================================================================


def test_provider_failure_degrades_to_pure_rules(db_session, project_id, monkeypatch):
    """Assistant provider 构造抛错 → 仍 200 / 仍有 issues，答案标明是纯规则结果。"""
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_before = llm_provider_mod._PROVIDER

    def boom():
        raise RuntimeError("simulated assistant build failure")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")

    result = _diagnose(project_id, db_session, scope="all")

    # 规则结果一个不少
    assert [i for i in result["issues"] if i["rule"] == "open_plothole_accumulation"]
    assert "Assistant provider 不可用" in result["answer"]
    assert "纯规则结果" in result["answer"]
    # 审计表标明这次没过模型
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.status == "success"
    assert run.model == "(local_fallback)"
    # Writer 单例原样保留
    assert llm_provider_mod._PROVIDER is writer_before


def test_provider_chat_failure_also_degrades(db_session, project_id, monkeypatch):
    """provider 能构造但 chat() 抛错 → 同样降级，不得让请求失败。"""
    class BoomProvider:
        model = "assistant-test-model"

        def chat(self, system: str, user: str) -> str:
            raise TimeoutError("chat timeout")

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: BoomProvider()
    )
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")

    result = _diagnose(project_id, db_session, scope="all")
    assert result["issues"], "模型挂掉不应吃掉规则结果"
    assert "Assistant provider 不可用" in result["answer"]
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "(assistant-test-model)" or run.model == "(local_fallback)"


def test_query_triggers_model_even_without_issues(db_session, project_id, monkeypatch):
    """请求带 query 时才调模型 —— 即便规则一条都没命中（用于解释用户的额外问题）。"""
    calls: list[tuple[str, str]] = []

    class RecordingProvider:
        model = "recorder-1"

        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "（模型解释）没有命中已知模式。"

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: RecordingProvider()
    )

    result = _diagnose(project_id, db_session, scope="all", query="主角的动机是什么？")
    assert result["issues"] == []
    assert len(calls) == 1, "带 query 时必须调一次 Assistant 模型"
    assert "主角的动机是什么？" in calls[0][1]
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run.model == "recorder-1"


def test_rules_alone_trigger_model(db_session, project_id, monkeypatch):
    """规则命中 → 调模型写解释；issue 的 claim 原样进 prompt（模型不得改判据）。"""
    calls: list[tuple[str, str]] = []

    class RecordingProvider:
        model = "recorder-2"

        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "解释文本"

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: RecordingProvider()
    )
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")

    result = _diagnose(project_id, db_session, scope="all")
    assert result["answer"] == "解释文本"
    assert len(calls) == 1
    assert "铜钥匙究竟是谁的" in calls[0][1]
    assert "open_plothole_accumulation" in calls[0][1]


def test_single_rule_failure_does_not_break_diagnosis(db_session, project_id, monkeypatch):
    """单条规则抛错 → 其余规则照常产出 issue，诊断不 500。"""
    from app.agents import diagnosis as diag_mod

    def boom(_snap, _taken):
        raise RuntimeError("rule exploded")

    monkeypatch.setitem(diag_mod.RULE_REGISTRY, "name_conflict", boom)
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")

    result = _diagnose(project_id, db_session, scope="all")
    assert [i for i in result["issues"] if i["rule"] == "open_plothole_accumulation"]
    assert result["tool_results"]["rule:name_conflict"]["error"] == "rule exploded"


# ====================================================================
# (d) 写权限字段 → 422
# ====================================================================


def test_diagnose_request_rejects_write_fields_at_model_layer():
    """AssistantDiagnoseRequest 携带 patch_json / commit_hash → pydantic 拒（ValueError）。"""
    from app.routers.assistant import AssistantDiagnoseRequest

    for bad in ({"patch_json": {"x": 1}}, {"commit_hash": "deadbeef"},
                {"target_table": "chapters"}, {"write_segment": 7}):
        with pytest.raises(ValueError):
            AssistantDiagnoseRequest(project_id=1, scope="all", **bad)


def test_diagnose_endpoint_returns_422_over_http(db_session):
    """HTTP 层：携带 patch_json / commit_hash 的请求体 → 422（端到端验 StrictRequest）。"""
    with TestClient(fastapi_app) as client:
        for bad in ({"patch_json": {"rewrite": "..."}}, {"commit_hash": "deadbeef"}):
            resp = client.post("/api/assistant/diagnose",
                               json={"project_id": 1, "scope": "all", **bad})
            assert resp.status_code == 422, (
                f"携带 {list(bad)} 应得 422，实际 {resp.status_code}"
            )


def test_diagnose_endpoint_still_accepts_valid_body(db_session):
    """合法请求体不被误拒（对照组：422 只对写语义字段生效）。"""
    with TestClient(fastapi_app) as client:
        resp = client.post("/api/assistant/diagnose",
                           json={"project_id": 1, "scope": "all"})
        # project 1 不存在 → 404；关键是**不是** 422
        assert resp.status_code in (200, 404)


# ====================================================================
# (e) evidence 的 source 隔离
# ====================================================================


def test_evidence_source_isolates_generated_from_novel(db_session, project_id):
    """参考语料证据 source 恒为 novel:{id}，生成侧恒为 generated —— 两者不串。"""
    # 参考语料侧：一本小说 + 若干分片
    novel = RawNovel(title="参考语料", author="某作者", status="imported")
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    db_session.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=0,
                              text="铜钥匙在雪夜里泛着冷光。"))
    db_session.commit()

    proj = db_session.get(Project, project_id)
    proj.reference_novel_ids = [novel.id]
    db_session.commit()

    # 生成侧：正文 + 事实
    _seed_segment(db_session, project_id, 1, "他攥紧铜钥匙，推开院门。")
    _seed_character_fact(db_session, project_id, "陆见微")

    result = _diagnose(project_id, db_session, scope="all",
                       query="铜钥匙在第几章出现")

    kinds = {e["kind"] for e in result["evidence"]}
    assert "chunk" in kinds, "带 query 时必须产出参考语料检索证据"

    for ev in result["evidence"]:
        src = ev["source"]
        if ev["kind"] == "chunk":
            assert src == f"novel:{novel.id}", (
                f"参考语料证据 source 必须是 novel:{novel.id}，实际 {src!r}"
            )
        else:
            assert src == "generated", (
                f"生成侧证据 source 必须是 generated，实际 {src!r}"
            )
        assert 0.0 <= ev["score"] <= 1.0
        assert isinstance(ev["snippet"], str) and ev["snippet"]


def test_novel_sourced_facts_keep_novel_label(db_session, project_id):
    """来自参考语料侧的 Fact（source=novel:{id}）证据不得被标成 generated。"""
    _seed_character_fact(db_session, project_id, "林黛玉", source="novel:42")
    _seed_character_node(db_session, project_id, "林黛玉")
    # 让它成为低置信事实之一以产出 evidence
    db_session.add(Fact(
        project_id=project_id, type="character", key="妙玉",
        props={}, aliases=[], source="novel:42", confidence="low",
        first_segment=1, last_segment=1,
    ))
    for i in range(5):
        db_session.add(Fact(
            project_id=project_id, type="character", key=f"红楼{i}",
            props={}, aliases=[], source="novel:42", confidence="low",
            first_segment=1, last_segment=1,
        ))
    db_session.commit()

    report = run_diagnostic_rules(db_session, db_session.get(Project, project_id),
                                  scope="all")
    novel_evs = [e for i in report.issues for e in i.evidence
                 if e.source.startswith("novel:")]
    assert novel_evs, "novel:42 的事实证据应带 novel: 前缀"
    assert all(e.source == "novel:42" for e in novel_evs)
    assert not [e for i in report.issues for e in i.evidence
                if e.kind == "fact" and e.ref_id == "fact:林黛玉"
                and e.source == "generated"]


def test_query_evidence_survives_when_no_rule_hits(db_session, project_id):
    """规则一条没命中但带了 query → 检索证据仍必须回到用户手里。

    这是一条曾经真实存在的缺陷：检索证据原先被挂到「最后一条 issue」上，
    于是「没有 issue」时证据凭空消失 —— 而「没报问题」被读成「没问题」，
    是这类助手最危险的失效方式（PRD 验收 2：无证据时必须明确说明，
    有证据时绝不能不给）。
    """
    novel = RawNovel(title="参考语料", author="某作者", status="imported")
    db_session.add(novel)
    db_session.commit()
    db_session.refresh(novel)
    db_session.add(NovelChunk(novel_id=novel.id, chapter_no=2, idx=0,
                              text="雪停了，铜钥匙上的血迹冻成暗褐色。"))
    db_session.commit()
    proj = db_session.get(Project, project_id)
    proj.reference_novel_ids = [novel.id]
    db_session.commit()

    result = _diagnose(project_id, db_session, scope="all", query="铜钥匙上的血迹")

    assert result["issues"] == [], "本用例不应命中任何规则"
    chunk_evs = [e for e in result["evidence"] if e["kind"] == "chunk"]
    assert chunk_evs, "无 issue 时检索证据不得丢失"
    assert all(e["source"] == f"novel:{novel.id}" for e in chunk_evs)
    # 同样要落库（审计表不能只在有 issue 时才有东西）
    rows = db_session.scalars(
        select(Evidence).where(Evidence.run_id == result["agent_run_id"])
    ).all()
    assert [r for r in rows if r.kind == "chunk"], "chunk 证据必须落 Evidence 表"


def test_snippet_is_verbatim_from_database(db_session, project_id):
    """snippet 必须是库中原文的真实片段（不是模型复述、不是改写）。"""
    gen_id = _seed_segment(db_session, project_id, 1, "陆见微推开院门，晨雾未散。")
    db_session.add(Fact(
        project_id=project_id, type="state", key="life:陆见微",
        props={"value": "dead", "since_segment": 0, "text": "陆见微死了，院里安静。"},
        aliases=[], source="generated", confidence="high",
        first_segment=1, last_segment=1,
    ))
    db_session.commit()

    result = _diagnose(project_id, db_session, scope="all")
    stored_text = db_session.get(Generation, gen_id).text
    for ev in result["evidence"]:
        if ev["kind"] == "generation":
            assert ev["snippet"] in stored_text, (
                f"generation evidence 必须是正文原样子串：{ev['snippet']!r}"
            )


# ====================================================================
# (f) 响应字段完整性
# ====================================================================


def test_diagnose_response_has_all_required_fields(db_session, project_id):
    """顶层必备字段：answer / issues / evidence / confidence / agent_run_id / version_id。"""
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")
    result = _diagnose(project_id, db_session, scope="all", version_id=None)

    for key in ("answer", "issues", "evidence", "confidence",
                "agent_run_id", "version_id", "tool_results", "rules_evaluated"):
        assert key in result, f"响应缺字段 {key}"
    assert isinstance(result["answer"], str) and result["answer"]
    assert isinstance(result["issues"], list)
    assert isinstance(result["evidence"], list) and result["evidence"]
    assert 0.0 <= result["confidence"] <= 1.0
    assert isinstance(result["agent_run_id"], int)
    assert result["version_id"] is None
    assert result["rules_evaluated"] == list(RULE_ORDER)
    # findings 兼容别名与 issues 一致
    assert result["findings"] == result["issues"]


def test_diagnose_evidence_persisted_with_valid_kind_source_score(db_session, project_id):
    """Evidence 落库语义完整：run_id 正确、kind 合法、source 隔离、score ∈ [0,1]。"""
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_character_node(db_session, project_id, "宁折")

    result = _diagnose(project_id, db_session, scope="all")
    run_id = result["agent_run_id"]

    rows = db_session.scalars(select(Evidence).where(Evidence.run_id == run_id)).all()
    assert rows, "Evidence 必须落库"
    for row in rows:
        assert row.kind in ALLOWED_EVIDENCE_KINDS
        assert row.source == "generated" or row.source.startswith("novel:")
        assert isinstance(row.ref_id, str) and row.ref_id
        score = float(row.score)
        assert 0.0 <= score <= 1.0
        assert len(row.snippet or "") <= 1000

    # 同一条证据被两条 issue 引用时，Evidence 表里只留一行（去重）
    ref_ids = [r.ref_id for r in rows]
    assert len(ref_ids) == len(set(ref_ids))


def test_diagnose_persists_agent_run(db_session, project_id):
    """AgentRun 落库字段完整（role / status / 时间 / request_hash / model）。"""
    result = _diagnose(project_id, db_session, scope="all")
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run is not None
    assert run.project_id == project_id
    assert run.role == "assistant"
    assert run.status == "success"
    assert run.started_at is not None and run.finished_at is not None
    assert run.finished_at >= run.started_at
    assert isinstance(run.request_hash, str) and len(run.request_hash) == 64


def test_issue_id_is_deterministic_across_runs(db_session, project_id):
    """同一份库、同一 scope 跑两次 → issue id 完全一致（可复现是硬要求）。"""
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])
    _seed_character_node(db_session, project_id, "宁折")

    first = _diagnose(project_id, db_session, scope="all")
    second = _diagnose(project_id, db_session, scope="all")
    assert [i["id"] for i in first["issues"]] == [i["id"] for i in second["issues"]]
    assert [i["claim"] for i in first["issues"]] == [i["claim"] for i in second["issues"]]


# ====================================================================
# (h) 只读：诊断不得写业务表
# ====================================================================


def test_diagnose_does_not_write_business_tables(db_session, project_id):
    """诊断前后 Generation / Fact / KgNode 行数与内容一致（只写 AgentRun + Evidence）。"""
    _seed_segment(db_session, project_id, 1, "陆见微推开院门。")
    _seed_open_plothole(db_session, project_id, "铜钥匙究竟是谁的？")
    _seed_character_fact(db_session, project_id, "宁折枝", aliases=["宁折"])

    def snapshot() -> tuple:
        return (
            db_session.scalar(select(func.count()).select_from(Generation)),
            db_session.scalar(select(func.count()).select_from(Fact)),
            db_session.scalar(select(func.count()).select_from(KgNode)),
            sorted(
                (f.id, f.key, tuple(f.aliases or []), f.confidence)
                for f in db_session.scalars(select(Fact)).all()
            ),
            sorted(
                (n.id, n.type, n.first_chapter, tuple(sorted((n.props or {}).items())))
                for n in db_session.scalars(select(KgNode)).all()
            ),
        )

    before = snapshot()
    _diagnose(project_id, db_session, scope="all")
    db_session.expire_all()
    assert snapshot() == before, "诊断不得修改 Generation / Fact / KgNode"


def test_router_exposes_no_write_endpoints():
    """静态扫描：assistant router 不含 /apply / /commit / /rollback / /patch 端点。"""
    import inspect

    src = inspect.getsource(assistant_router)
    for forbidden in ("@router.post(\"/apply", "@router.post(\"/commit",
                      "@router.post(\"/rollback", "@router.post(\"/patch",
                      "/proposals", "/versions"):
        assert forbidden not in src, f"assistant router 不应出现 {forbidden!r}"


def test_role_lock_violation_fails_loud_not_degraded(db_session, project_id, monkeypatch):
    """诊断规则链路上出现 PermissionError → 403 + run 记 failed，不降级成空结果。

    降级在这里是**错的**：规则取不到数据会被读成「没问题」，而真实原因是
    「Assistant 试图调写工具」。这两种读法对用户的行动建议完全相反。
    """
    from fastapi import HTTPException

    from app.agents import diagnosis as diag_mod

    def boom(_snap, _taken):
        raise PermissionError("Assistant 拒绝调用黑名单前缀工具 'write_x'")

    monkeypatch.setitem(diag_mod.RULE_REGISTRY, "name_conflict", boom)

    with pytest.raises(HTTPException) as exc:
        _diagnose(project_id, db_session, scope="all")
    assert exc.value.status_code == 403
    assert "越权" in str(exc.value.detail)

    # 越权尝试必须留痕：AgentRun 记 failed + blocked_by_registry
    run = db_session.scalars(
        select(AgentRun).where(AgentRun.project_id == project_id)
    ).all()
    assert len(run) == 1
    assert run[0].status == "failed"
    assert run[0].model == "(blocked_by_registry)"
    assert run[0].finished_at is not None


# ====================================================================
# 数据结构自校验（fail loud）
# ====================================================================


def test_issue_rejects_invalid_severity():
    """非法 severity 直接构造失败（fail loud，不静默降级成 warn）。"""
    with pytest.raises(ValueError):
        DiagnosticIssue(
            id="x", type="plothole", severity="critical", title="t",
            claim="c", suggestion="s", rule="r",
        )


def test_evidence_rejects_invalid_kind():
    """非法 evidence kind 直接构造失败。"""
    with pytest.raises(ValueError):
        IssueEvidence(kind="paraphrase", source="generated",
                      ref_id="1", snippet="s", score=0.5)


def test_evidence_score_is_clamped():
    """score 越界在构造点被钳回 [0,1]（不靠消费端兜底）。"""
    hi = IssueEvidence(kind="chunk", source="novel:1", ref_id="1", snippet="s", score=7.5)
    lo = IssueEvidence(kind="chunk", source="novel:1", ref_id="2", snippet="s", score=-3.0)
    assert hi.to_dict()["score"] == 1.0
    assert lo.to_dict()["score"] == 0.0