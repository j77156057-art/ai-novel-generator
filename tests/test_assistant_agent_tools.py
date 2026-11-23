"""助手自主调度：4 个只读工具 + 待确认卡片（第 11 步验收）。

助手页从 6 个操作页签瘦身为「只有问答框」后，诊断 / 日志分析 / 三类审核
从「前端点按钮」改为「agent 自己判断需要时调用」。本测试守的是这次改造的
四条不变量。

**(1) 4 个新工具可被 `invoke_tool` 调用，且都在白名单内、都是只读**
  - `audit_corpus` / `audit_kg` / `audit_generation` / `analyze_logs`
    全部注册成功、返回结构与 diagnose 同构（issues/count/rules_evaluated）；
  - 规则命中是真的跑出来的（造脏数据 → 报对应 rule），不是空壳返回；
  - `novel_ids=[]` **必须报错**，不能静默广播全库（回归 P1）。

**(2) `analyze_logs` 的参数校验与 `/runs` 逐字节一致**
  - 非法 status → 报错，不静默返回空（否则读起来像「没调用记录」）；
  - `limit=-1` / `0` / 超上限 → 报错，不悄悄取全表
    （回归 QA8 的 P2：同一参数 `/runs` 400、工具里 200）。

**(3) `pending_actions` 缺省为 `[]`，既有响应契约不变**
  - 普通提问不带生成指令 → 空列表，前端不渲染该区域；
  - `/qa` 既有字段一个不少，且新增字段是纯增量。

**(4) 卡片只「提出」不执行**
  - 卡片六字段齐全，endpoint/method 由 kind 唯一决定（前端不参与构造）；
  - 点确认前**零写入**：Generation 行数不变、AgentRun 不多出「执行」记录；
  - 危险动作（重写已有段）与非危险动作（续写新段）可被前端区分开。

不依赖真实网络：Writer 单例换 LocalFallbackLLMProvider，Assistant provider
视用例需要 monkeypatch。
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
from app.agents.pending_actions import (  # noqa: E402
    PENDING_ACTION_KINDS,
    build_pending_action,
    extract_segment_no,
    propose_invoke_generation,
)
from app.agents.registry import (  # noqa: E402
    ALLOWED_READ_ONLY_TOOLS,
    FORBIDDEN_PREFIXES,
    get_tool_registry,
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
    KgNode,
    NovelChunk,
    Project,
    RawNovel,
)
from app.routers import assistant as assistant_router  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


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
    proj = Project(
        title="自主调度测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        generation_length=3000,
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


# ====================================================================
# 种子数据小工具
# ====================================================================


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


def _seed_run(db, project_id: int, *, status: str = "success",
              model: str = "test-model", duration_s: float = 1.0,
              note: str | None = None, evidence_count: int = 1) -> int:
    from datetime import datetime, timedelta, timezone

    started = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)
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


def _rules_of(result: dict) -> set[str]:
    return {i["rule"] for i in result["issues"]}


def _business_snapshot(db) -> tuple:
    """业务表快照（用于「只读」断言）。"""
    return (
        db.scalar(select(func.count()).select_from(Generation)),
        db.scalar(select(func.count()).select_from(Fact)),
        db.scalar(select(func.count()).select_from(KgNode)),
        db.scalar(select(func.count()).select_from(NovelChunk)),
        db.scalar(select(func.count()).select_from(RawNovel)),
    )


# ====================================================================
# (1) 4 个新工具：注册 / 可调用 / 结构同构 / 规则真跑
# ====================================================================


def test_four_new_tools_registered_and_read_only():
    """4 个新工具都在白名单里、都是 read_only=True、名字不命中黑名单前缀。"""
    reg = get_tool_registry()
    new_tools = ("audit_corpus", "audit_kg", "audit_generation", "analyze_logs")

    for name in new_tools:
        assert name in ALLOWED_READ_ONLY_TOOLS, f"{name} 未进白名单"
        assert name in reg, f"{name} 未注册到注册表"
        spec = reg.resolve(name)
        assert spec.read_only is True, f"{name} 必须是 read_only=True"
        for prefix in FORBIDDEN_PREFIXES:
            assert not name.startswith(prefix), (
                f"{name} 命中黑名单前缀 {prefix!r}——register() 应当直接拒绝"
            )


def test_registry_names_match_whitelist_exactly():
    """注册表名字集合 == 白名单（不多不少，避免后门工具混入）。"""
    assert set(get_tool_registry().names()) == set(ALLOWED_READ_ONLY_TOOLS)


def test_audit_corpus_tool_detects_short_chunks(db_session, project_id):
    """audit_corpus：超短分片 → empty_or_short_chunks（真跑规则，不是空壳）。"""
    novel_id = _seed_novel(db_session)
    bad_id = _seed_chunk(db_session, novel_id, "短")
    _seed_chunk(db_session, novel_id, "正常长度的分片内容。" * 10, idx=1)

    result = invoke_tool("audit_corpus", db=db_session, project_id=project_id)

    assert "empty_or_short_chunks" in _rules_of(result)
    issue = next(i for i in result["issues"]
                 if i["rule"] == "empty_or_short_chunks")
    assert str(novel_id) in issue["claim"]
    assert str(bad_id) in [ev["ref_id"] for ev in issue["evidence"]]


def test_audit_corpus_tool_scopes_to_novel_ids(db_session, project_id):
    """audit_corpus：给定 novel_ids 只审这些（范围不能被静默放大）。"""
    a = _seed_novel(db_session, title="语料A")
    b = _seed_novel(db_session, title="语料B")
    _seed_chunk(db_session, a, "短")
    _seed_chunk(db_session, b, "短")

    result = invoke_tool("audit_corpus", db=db_session, project_id=project_id,
                         novel_ids=[a])

    assert result["stats"]["novels_audited"] == 1
    assert result["stats"]["chunks_audited"] == 1
    assert result["novel_ids"] == [a]


def test_audit_corpus_tool_rejects_empty_novel_ids(db_session, project_id):
    """`novel_ids=[]` 必须 422，**不能**被当成「审全部」。

    回归 P1（历史真实 bug）：`[]` 是 falsy，旧的 `if novel_ids:` 让 `[]`
    与 `None` 撞进同一分支，把「一本都没选」执行成「广播审全库」。真库
    7941 分片时用户完全看不出来。工具可以被 router 之外的路径直接调用，
    所以不能依赖端点侧的 `_require_novels` 已经拒过。
    """
    from fastapi import HTTPException

    _seed_novel(db_session, title="语料A")
    _seed_novel(db_session, title="语料B")

    with pytest.raises(HTTPException) as exc:
        invoke_tool("audit_corpus", db=db_session, project_id=project_id,
                    novel_ids=[])
    assert exc.value.status_code == 422

    # 反证：不传 novel_ids 仍是「审全部」（既有语义不能被改变）
    result = invoke_tool("audit_corpus", db=db_session, project_id=project_id)
    assert result["stats"]["novels_audited"] == 2


def test_audit_kg_tool_detects_orphan_nodes(db_session, project_id):
    """audit_kg：无边连接的 KG 节点 → orphan_kg_nodes。"""
    _seed_character_node(db_session, project_id, "独孤求败")

    result = invoke_tool("audit_kg", db=db_session, project_id=project_id)

    assert "orphan_kg_nodes" in _rules_of(result)
    assert result["issues"][0]["evidence"]


def test_audit_generation_tool_detects_empty_text(db_session, project_id):
    """audit_generation：空正文段 → empty_segment_text（error）。"""
    _seed_segment(db_session, project_id, 1, "   ")

    result = invoke_tool("audit_generation", db=db_session, project_id=project_id)

    issue = next(i for i in result["issues"] if i["rule"] == "empty_segment_text")
    assert issue["severity"] == "error"


def test_analyze_logs_tool_detects_local_fallback(db_session, project_id):
    """analyze_logs：(local_fallback) 出现 → local_fallback_degradation。"""
    _seed_run(db_session, project_id, model="(local_fallback)")

    result = invoke_tool("analyze_logs", db=db_session, project_id=project_id)

    assert "local_fallback_degradation" in _rules_of(result)
    assert result["runs_analyzed"] == 1


def test_four_tool_results_share_same_structure(db_session, project_id):
    """4 个工具返回结构同构：agent 的提示词只需写一套读法。

    不同构的返回会让 agent 每种工具记一套解析规则——而模型恰恰是最容易
    在「第三种结构」上读错字段的那一环。
    """
    _seed_novel(db_session)
    _seed_chunk(db_session, _seed_novel(db_session, title="另一本"), "短")
    _seed_character_node(db_session, project_id, "孤独求败")
    _seed_segment(db_session, project_id, 1, "   ")
    _seed_run(db_session, project_id, model="(local_fallback)")

    results = {
        name: invoke_tool(name, db=db_session, project_id=project_id)
        for name in ("audit_corpus", "audit_kg", "audit_generation", "analyze_logs")
    }
    for name, result in results.items():
        assert isinstance(result, dict), f"{name} 必须返回 dict"
        for key in ("issues", "count", "findings", "rules_evaluated", "stats"):
            assert key in result, f"{name} 返回缺字段 {key}"
        assert result["findings"] == result["issues"]
        assert result["count"] == len(result["issues"])
        for issue in result["issues"]:
            # 九字段与 diagnose / analyze-logs 同构
            for field in ("id", "type", "severity", "title", "claim",
                          "evidence", "suggestion", "rule"):
                assert issue[field] not in (None, ""), f"{name} 的 issue.{field} 为空"


def test_four_tools_are_read_only_on_business_tables(db_session, project_id):
    """4 个工具零业务表写入（只读纪律的正面验证）。"""
    _seed_novel(db_session)
    _seed_chunk(db_session, _seed_novel(db_session, title="另一本"), "短")
    _seed_character_node(db_session, project_id, "孤独求败")
    _seed_segment(db_session, project_id, 1, "   ")
    _seed_run(db_session, project_id, model="(local_fallback)")

    before = _business_snapshot(db_session)
    for name in ("audit_corpus", "audit_kg", "audit_generation", "analyze_logs"):
        invoke_tool(name, db=db_session, project_id=project_id)
    db_session.expire_all()
    assert _business_snapshot(db_session) == before, "自主调度工具不得写业务表"


def test_audit_kg_tool_404_unknown_project(db_session):
    """project 不存在 → 404（不静默返回「图谱很干净」）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        invoke_tool("audit_kg", db=db_session, project_id=9999)
    assert exc.value.status_code == 404


# ====================================================================
# (2) analyze_logs 参数校验必须与 /runs 同口径（回归 QA8 的 P2）
# ====================================================================


def test_analyze_logs_tool_rejects_invalid_status(db_session, project_id):
    """非法 status → 报错，不静默返回空。

    静默把非法 status 当「没有匹配的 run」，读起来就是「这段时间没有调用
    记录」——而真实原因是拼写错了。这类静默失效比报错难查得多。
    """
    from fastapi import HTTPException

    _seed_run(db_session, project_id)
    with pytest.raises(HTTPException) as exc:
        invoke_tool("analyze_logs", db=db_session, project_id=project_id,
                    status="totally-bogus")
    assert exc.value.status_code == 400


def test_analyze_logs_tool_rejects_out_of_range_limit(db_session, project_id):
    """`limit=-1` 在 SQLite 是 `LIMIT -1`（= 无上限，取全表）→ 必须报错。

    与 `GET /runs` / `POST /analyze-logs` 逐字节同口径：同一个参数在三个
    入口给出不同结论，是本项目已经踩过的真实 bug。
    """
    from fastapi import HTTPException

    _seed_run(db_session, project_id)
    for bad in (-1, 0, 501, 10**6):
        with pytest.raises(HTTPException) as exc:
            invoke_tool("analyze_logs", db=db_session, project_id=project_id,
                        limit=bad)
        assert exc.value.status_code == 400, f"limit={bad} 应 400"


def test_analyze_logs_tool_valid_params_still_work(db_session, project_id):
    """反证：合法参数不受校验影响（别把修复做成全拒）。"""
    _seed_run(db_session, project_id, status="failed")
    _seed_run(db_session, project_id, status="success")

    result = invoke_tool("analyze_logs", db=db_session, project_id=project_id,
                         status="failed", limit=50)

    assert result["runs_analyzed"] == 1
    assert result["status_filter"] == "failed"
    assert result["limit"] == 50


def test_analyze_logs_tool_validation_matches_runs_endpoint(db_session, project_id):
    """同一组参数：工具与 `GET /runs` 必须给出**同样**的判定。

    这条是「为什么必须复用 `_validate_run_status` / `_validate_runs_limit`」
    的直接证据：断言两者行为一致，而不是断言「工具报了错」——后者在
    有人给工具单独写一份宽松校验时同样会通过。
    """
    from fastapi import HTTPException

    _seed_run(db_session, project_id)

    for kwargs in ({"status": "bogus"}, {"limit": -1}, {"limit": 0}, {"limit": 999}):
        with pytest.raises(HTTPException) as tool_exc:
            invoke_tool("analyze_logs", db=db_session, project_id=project_id,
                        **kwargs)
        endpoint_kwargs = {
            "project_id": project_id, "status": kwargs.get("status"),
            "limit": kwargs.get("limit", 100),
        }
        with pytest.raises(HTTPException) as ep_exc:
            assistant_router.assistant_runs(db=db_session, **endpoint_kwargs)
        assert tool_exc.value.status_code == ep_exc.value.status_code == 400, (
            f"参数 {kwargs} 在工具与 /runs 的判定不一致"
        )


# ====================================================================
# (3) pending_actions 缺省为 []，既有响应契约不变
# ====================================================================


def test_qa_pending_actions_defaults_to_empty(db_session, project_id):
    """普通提问 → `pending_actions == []`（前端据此完全不渲染该区域）。"""
    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="谁是主角？"),
        db=db_session,
    )
    assert result["pending_actions"] == []


def test_qa_keeps_existing_response_contract(db_session, project_id):
    """新增字段是纯增量：/qa 既有字段一个不少，且默认值合法。"""
    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="讲讲剧情"),
        db=db_session,
    )
    for key in ("answer", "evidence", "confidence", "agent_run_id",
                "tool_results", "version_id"):
        assert key in result, f"/qa 响应丢了既有字段 {key}"
    assert isinstance(result["pending_actions"], list)


def test_qa_over_http_pending_actions_present(db_session, project_id):
    """HTTP 层：pending_actions 出现在 JSON 里且缺省为空数组。"""
    from fastapi.testclient import TestClient

    from app.main import app as fastapi_app

    with TestClient(fastapi_app) as client:
        resp = client.post("/api/assistant/qa", json={
            "project_id": project_id, "query": "谁是主角？",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["pending_actions"] == []

        # 档 3 也在动作目录里，但只能是 frontend_only + 需确认，且不会变成可执行卡片。
        resp2 = client.post("/api/assistant/qa", json={
            "project_id": project_id, "query": "能不能回滚到快照",
        })
        assert resp2.status_code == 200, resp2.text
        actions = {a["key"]: a for a in resp2.json()["available_actions"]}
        assert actions["snapshot_rollback"]["policy"] == "frontend_only"
        assert actions["snapshot_rollback"]["requires_confirmation"] is True
        assert resp2.json()["pending_actions"] == []


# ====================================================================
# (4) 卡片只「提出」不执行
# ====================================================================


def test_propose_generation_card_for_rewrite(db_session, project_id):
    """用户说「重写第 1 段」→ 提一张完整的 regenerate 卡片。"""
    _seed_segment(db_session, project_id, 1, "第一段旧正文。" * 20)

    card = propose_invoke_generation(
        project_id=project_id, query="帮我重写第 1 段，加强氛围",
        has_segment=lambda n: True,
    )

    assert card is not None
    assert card["kind"] == "invoke_generation"
    assert card["body"]["action"] == "regenerate_segment"
    assert card["body"]["segment_no"] == 1
    assert card["body"]["project_id"] == project_id
    # 用户原话进 instruction：审计链需要它，也是 invoke-generation 的硬要求
    assert card["body"]["instruction"] == "帮我重写第 1 段，加强氛围"
    assert card["detail"]["destructive"] is True
    assert "重写第 1 段" in card["title"]


def test_propose_generation_card_for_continue(db_session, project_id):
    """用户说「续写下一段」→ generate_next 卡（body 不带 segment_no）。"""
    card = propose_invoke_generation(
        project_id=project_id, query="续写下一段，写主角夜探祠堂",
        has_segment=lambda n: True,
    )

    assert card is not None
    assert card["body"]["action"] == "generate_next"
    # generate_next 带 segment_no 会被 AssistantInvokeGenerationRequest 拒（422）
    assert "segment_no" not in card["body"]
    assert card["detail"]["destructive"] is False


def test_propose_card_omitted_for_ordinary_question(db_session, project_id):
    """普通提问 → 不提卡（宁可漏提不可错提：误提要用户点一次无意义的确认）。"""
    for query in ("谁是主角？", "这段写得好吗", "帮我看看第 3 段"):
        card = propose_invoke_generation(
            project_id=project_id, query=query, has_segment=lambda n: True
        )
        assert card is None, f"{query!r} 不该提卡，却得到 {card}"


def test_propose_card_omitted_when_segment_missing(db_session, project_id):
    """段不存在 → 不提卡（提了就是让用户点一个必然 404 的按钮）。"""
    _seed_segment(db_session, project_id, 1, "只有第 1 段")

    card = propose_invoke_generation(
        project_id=project_id, query="重写第 7 段",
        has_segment=lambda n: n == 1,
    )
    assert card is None


def test_propose_card_omitted_when_rewrite_has_no_segment(db_session, project_id):
    """「重写一下」但没说哪段 → 不提卡（确认后必然 422）。"""
    card = propose_invoke_generation(
        project_id=project_id, query="帮我重写一下",
        has_segment=lambda n: True,
    )
    assert card is None


def test_propose_card_handles_chinese_numeral_segment(db_session, project_id):
    """`第三段` 这类汉字数字段号也要认（中文语境下比阿拉伯数字更常见）。"""
    card = propose_invoke_generation(
        project_id=project_id, query="把第三段重写一下",
        has_segment=lambda n: True,
    )
    assert card is not None
    assert card["body"]["segment_no"] == 3


def test_extract_segment_no_known_forms():
    """段号解析：阿拉伯数字 / 汉字数字 / segment 记法都覆盖。"""
    assert extract_segment_no("重写第 3 段") == 3
    assert extract_segment_no("重写第3段") == 3
    assert extract_segment_no("重写第十段") == 10
    assert extract_segment_no("重写第十九段") == 19
    assert extract_segment_no("regenerate segment 5") == 5
    assert extract_segment_no("seg=2 那段") == 2
    assert extract_segment_no("续写下一段") is None
    assert extract_segment_no("随便聊聊") is None


def test_card_fields_complete_and_endpoint_derived_from_kind():
    """卡片六字段齐全；endpoint/method 由 kind 唯一决定（前端不参与构造）。"""
    card = build_pending_action(
        kind="invoke_generation",
        title="续写下一段",
        detail={"action": "generate_next"},
        body={"project_id": 1, "action": "generate_next", "instruction": "续写"},
    )
    for field in ("kind", "title", "detail", "endpoint", "method", "body"):
        assert field in card, f"卡片缺字段 {field}"
    assert card["endpoint"] == "/api/assistant/invoke-generation"
    assert card["method"] == "POST"

    proposal_card = build_pending_action(
        kind="proposal", title="改写第 2 段",
        detail={"type": "text_edit"}, body={"project_id": 1},
    )
    assert proposal_card["endpoint"] == "/api/assistant/proposals"
    # 两类动作不得串端点
    assert proposal_card["endpoint"] != card["endpoint"]
    assert set(PENDING_ACTION_KINDS) == {
        "proposal", "invoke_generation", "ingest_attachment", "choice",
        "action_proposal"}


def test_build_pending_action_validates_inputs():
    """构造器 fail loud：未知 kind / 空 title / 非 mapping body 全部拒绝。"""
    with pytest.raises(ValueError):
        build_pending_action(kind="run_generation_now", title="x",
                             detail={}, body={})
    with pytest.raises(ValueError):
        build_pending_action(kind="proposal", title="   ", detail={}, body={})
    with pytest.raises(ValueError):
        build_pending_action(kind="proposal", title="x", detail=[],
                             body={"project_id": 1})
    with pytest.raises(ValueError):
        build_pending_action(kind="proposal", title="x", detail={},
                             body="not-a-mapping")


def test_qa_card_does_not_execute_anything(db_session, project_id):
    """**提出不等于执行**：出卡片时零写入，用户确认前什么都不发生。

    这是「助手不得自行 apply 提案或触发生成」这条红线的回归。
    """
    _seed_segment(db_session, project_id, 1, "第一段旧正文。" * 20)
    before_gens = db_session.scalar(select(func.count()).select_from(Generation))
    before_business = _business_snapshot(db_session)
    old_text = db_session.scalars(
        select(Generation).where(Generation.segment_no == 1)
    ).first().text

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="帮我重写第 1 段，加强氛围"),
        db=db_session,
    )

    # 提了卡
    assert len(result["pending_actions"]) == 1
    card = result["pending_actions"][0]
    assert card["kind"] == "invoke_generation"
    assert card["body"]["action"] == "regenerate_segment"
    # 但什么都没做
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Generation)) == before_gens
    assert _business_snapshot(db_session) == before_business
    new_text = db_session.scalars(
        select(Generation).where(Generation.segment_no == 1)
    ).first().text
    assert new_text == old_text, "卡片只是提案，正文不得被改动"
    # 审计表里也没有「执行生成」的记录（只有这次问答自己的 run）
    notes = [r.note or "" for r in db_session.scalars(select(AgentRun)).all()]
    assert not any("这次生成是助手调用的" in n for n in notes)


def test_qa_card_not_proposed_for_other_project_segment(db_session, project_id):
    """别家项目的段号 → 不提卡（跨项目的段对确认后必然 404）。"""
    other = Project(title="别的项目", target_length=1000, mode="quick",
                    status="created", generation_length=3000)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    _seed_segment(db_session, other.id, 1, "别家项目的第一段")

    # 本项目没有第 1 段
    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="重写第 1 段"),
        db=db_session,
    )
    assert result["pending_actions"] == []


def test_agent_router_has_no_write_tool_backdoor(db_session):
    """注册表不含任何直写工具：助手没有后门（写操作只走待确认卡片）。"""
    for name in get_tool_registry().names():
        assert not name.startswith(FORBIDDEN_PREFIXES), (
            f"注册表出现直写工具 {name}——助手不该有后门"
        )
    for forbidden in ("write_generation", "apply_proposal", "delete_novel"):
        with pytest.raises(PermissionError):
            invoke_tool(forbidden, db=db_session, project_id=1)


# ====================================================================
# (5) function calling：只有只读白名单工具能进 tools 数组
# ====================================================================


class _ScriptedProvider:
    """按脚本逐次返回 `complete_message` 结果的假 provider。

    `/qa` 的工具循环要验证的是**路由行为**（步数上限、未知工具不执行、
    失败回落），不需要真实网络。`chat()` 留作确定性回落腿的应答。
    """

    model = "scripted-tool-model"

    def __init__(self, script, chat_reply: str = "确定性兜底回答"):
        self._script = list(script)
        self._chat_reply = chat_reply
        self.calls: list[dict] = []
        self.chat_calls = 0

    def complete_message(self, messages, *, tools=None, tool_choice=None,
                         stop_sequences=None) -> dict:
        # 快照入参：消息列表后续会被 loop 继续 append，这里必须拷一份。
        self.calls.append({
            "messages": [dict(m) for m in messages],
            "tools": tools,
            "tool_choice": tool_choice,
        })
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return _tool_text("（脚本已用尽）")

    def chat(self, system, user, constraints=None) -> str:
        self.chat_calls += 1
        return self._chat_reply


def _tool_text(content: str, model: str = "scripted-tool-model") -> dict:
    return {"content": content, "tool_calls": [], "finish_reason": "stop",
            "usage": None, "model": model}


def _tool_call(name: str, arguments: str = "{}", call_id: str = "call-1") -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def test_model_tools_are_whitelisted_readonly_and_have_schema():
    """`describe_tools(only=model_tool_names())` 只导出白名单只读工具，且带 JSON Schema。"""
    from app.agents.action_catalog import model_tool_names
    from app.agents.registry import describe_tools

    names = model_tool_names()
    assert names, "动作目录没有可暴露的只读工具，模型将无工具可用"
    described = describe_tools(only=names)
    got = {item["function"]["name"] for item in described}
    # 交集内每一个都应导出（导出环节不能再偷偷吞掉）
    assert got == set(names)
    assert got <= set(ALLOWED_READ_ONLY_TOOLS)
    for item in described:
        assert item["type"] == "function"
        assert item["function"]["description"].strip()
        params = item["function"]["parameters"]
        assert params["type"] == "object"
        assert isinstance(params.get("properties"), dict)


def test_confirm_required_actions_never_become_tools():
    """档 2 / 档 3 只在 `available_actions` 文本里：既不是工具名，也导不出 schema。"""
    from app.agents.action_catalog import ACTIONS, model_tool_names
    from app.agents.registry import describe_tools

    tool_names = set(model_tool_names())
    tiers23 = [s for s in ACTIONS if s.policy in ("propose_write", "frontend_only")]
    assert tiers23, "动作目录里没有档 2/3，本断言失去意义"
    for spec in tiers23:
        assert spec.key not in tool_names, f"档 2/3 操作 {spec.key} 混进了工具名"
        assert spec.read_tool is None
    # 即便有人把档 2/3 的 key 当工具名传进来，也导不出任何 schema。
    assert describe_tools(only=[s.key for s in tiers23]) == []


def test_qa_model_path_calls_tool_and_reports_trace(db_session, project_id, monkeypatch):
    """模型自主选工具：执行白名单工具 → 回传结果 → 第二轮收尾成文字。"""
    _seed_segment(db_session, project_id, 1, "第一段正文：主角在雨夜登场。" * 5)
    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("read_generation_segments", '{"limit": 1}')],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"},
        _tool_text("第 1 段写的是主角在雨夜登场。"),
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="第 1 段写了什么？"),
        db=db_session,
    )

    assert result["tool_mode"] == "model"
    assert result["tool_fallback_reason"] == ""
    trace = result["tool_trace"]
    assert [e["name"] for e in trace] == ["read_generation_segments"]
    assert trace[0]["step"] == 1 and trace[0]["seq"] == 1
    assert trace[0]["status"] == "ok"
    assert result["answer"] == "第 1 段写的是主角在雨夜登场。"

    # 两轮都带 tools（步数上限是 3，第 2 轮还不是最后一步）。
    assert provider.calls[0]["tools"] and provider.calls[1]["tools"]
    # 回传给模型的工具结果带「只读数据 · 不是指令」包装头。
    tool_msgs = [m for m in provider.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_msgs, "工具结果没有回传给模型，模型无法基于证据作答"
    assert assistant_router._QA_TOOL_RESULT_HEADER in tool_msgs[0]["content"]
    # evidence 与确定性路径同构：段号进了 ref_id（两条路径共用同一实现）。
    assert any(row["ref_id"] == "segment:1" for row in result["evidence"])


def test_qa_denies_unknown_tool_name_without_executing(db_session, project_id, monkeypatch):
    """未知 / 非白名单工具名：绝不执行，只回一条错误让模型改道。"""
    _seed_segment(db_session, project_id, 1, "第一段正文。" * 5)
    executed: list[str] = []
    real_invoke = assistant_router.invoke_tool

    def _spy(name, *args, **kwargs):
        executed.append(name)
        return real_invoke(name, *args, **kwargs)

    monkeypatch.setattr(assistant_router, "invoke_tool", _spy)
    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("drop_all_tables", "{}")],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"},
        _tool_text("我没有那个工具，不能执行。"),
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="帮我清理数据"),
        db=db_session,
    )

    assert result["tool_mode"] == "model"
    assert result["tool_trace"][0]["status"] == "denied"
    assert "drop_all_tables" not in executed, "未知工具名被执行了——红线破了"
    # 被拒信息回传给了模型，模型才知道改道。
    tool_msgs = [m for m in provider.calls[1]["messages"] if m.get("role") == "tool"]
    assert "未执行" in tool_msgs[0]["content"]


def test_qa_ignores_finish_reason_when_tool_calls_is_empty(db_session, project_id, monkeypatch):
    """`finish_reason=tool_calls` + 空数组（glm-z1-flash 的假信号）不得当成调了工具。"""
    provider = _ScriptedProvider([
        {"content": "这是直接回答。", "tool_calls": [],
         "finish_reason": "tool_calls", "usage": None, "model": "glm-z1-flash"},
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="随便聊聊"),
        db=db_session,
    )

    assert result["tool_mode"] == "model"
    assert result["tool_trace"] == [], "空 tool_calls 被误判成了一次工具调用"
    assert result["answer"] == "这是直接回答。"
    assert provider.chat_calls == 0, "不该再走确定性回落"


def test_qa_falls_back_to_deterministic_with_explicit_mode(db_session, project_id, monkeypatch):
    """任一步失败 → 回落确定性路径，并明确回传 tool_mode=deterministic。"""
    _seed_segment(db_session, project_id, 1, "第一段正文。" * 5)
    provider = _ScriptedProvider([RuntimeError("provider 503")])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="第 1 段写了什么？"),
        db=db_session,
    )

    assert result["tool_mode"] == "deterministic"
    assert result["tool_trace"] == []
    assert "provider 503" in result["tool_fallback_reason"]
    # 回落腿真的跑了：确定性三工具进了 tool_results，模型被 chat() 调了一次。
    assert "read_generation_segments" in result["tool_results"]
    assert provider.chat_calls == 1
    assert result["answer"] == "确定性兜底回答"


def test_qa_tool_loop_is_bounded_and_exhaustion_falls_back(db_session, project_id, monkeypatch):
    """模型每步都硬要调工具：循环最多到配置上限，之后回落并如实回传原因。"""
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_qa_max_tool_steps", 4, raising=False)
    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("get_reminders", "{}", f"c{i}")],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"}
        for i in range(4)
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="有什么提醒"),
        db=db_session,
    )

    assert len(provider.calls) == 4
    # 最后一次请求**不带** tools：即便模型还想调，也只剩收尾成文字这条路。
    assert provider.calls[-1]["tools"] is None
    assert result["tool_mode"] == "deterministic"
    assert "工具循环未产出" in result["tool_fallback_reason"]


def test_qa_tool_step_limit_defaults_to_eight():
    """默认步数从 6 提到 8：复杂问题（目录 → 多次检索 → 取详情）需要两轮余量。"""
    from app.config import settings

    assert settings.assistant_qa_max_tool_steps == 8
    assert assistant_router._qa_max_tool_steps() == 8


def test_qa_reports_tool_steps_and_forced_wrap_up(db_session, project_id, monkeypatch):
    """最后一步靠「不带 tools」收尾时，响应要能看出用了多少步、是否被强制收尾。"""
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_qa_max_tool_steps", 4, raising=False)
    script = [
        {"content": "", "tool_calls": [_tool_call("get_reminders", "{}", f"c{i}")],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"}
        for i in range(3)
    ]
    script.append(_tool_text("基于工具结果的收尾回答。"))
    provider = _ScriptedProvider(script)
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="有什么提醒"),
        db=db_session,
    )

    assert result["tool_mode"] == "model"
    assert result["tool_steps"] == 4
    assert result["forced_wrap_up"] is True
    assert provider.calls[-1]["tools"] is None
    assert result["answer"] == "基于工具结果的收尾回答。"


def test_gather_only_stops_after_tools_and_leaves_answer_empty(db_session, project_id, monkeypatch):
    """流式聊天先取证：模型调完工具就停，不生成正文（正文由流式那一步负责）。"""
    _seed_segment(db_session, project_id, 1, "第一段正文：主角在雨夜登场。" * 5)
    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("read_generation_segments", '{"limit": 1}')],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"},
        _tool_text("这段不该被当成最终回答"),
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    project = db_session.get(Project, project_id)
    outcome = assistant_router._qa_answer_with_tools(
        db=db_session,
        project=project,
        run_id=1,
        query="第 1 段写了什么？",
        action_block="",
        trace_ctx={},
        gather_only=True,
    )

    assert outcome["answer"] == "", "取证模式不该产出正文"
    assert [e["name"] for e in outcome["tool_trace"]] == ["read_generation_segments"]
    assert outcome["tool_steps"] == 2
    assert outcome["forced_wrap_up"] is False
    assert any("[正文 第1段]" in line for line in outcome["evidence_lines"])


def test_gather_only_marks_step_limit_exhausted(db_session, project_id, monkeypatch):
    """取证模式步数用满仍在调工具：停止检索并标记 forced_wrap_up。"""
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_qa_max_tool_steps", 4, raising=False)
    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("get_reminders", "{}", f"c{i}")],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"}
        for i in range(4)
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    project = db_session.get(Project, project_id)
    outcome = assistant_router._qa_answer_with_tools(
        db=db_session,
        project=project,
        run_id=1,
        query="有什么提醒",
        action_block="",
        trace_ctx={},
        gather_only=True,
    )

    assert outcome["answer"] == ""
    assert outcome["tool_steps"] == 4
    assert outcome["forced_wrap_up"] is True
    assert len(outcome["tool_trace"]) == 4
    # 取证模式没有「强制收尾」这一轮：4 步全是带 tools 的请求。
    assert all(call["tools"] for call in provider.calls)


def test_small_talk_skips_tool_round_but_data_questions_do_not():
    """寒暄跳过工具轮；只要带检索/写作意图（哪怕是「你好，素材…」）就照常跑。"""
    from app.routers.assistant import _is_small_talk

    for query in ("你好", "您好！", "hi", "谢谢啦", "在吗？", "嗯嗯", "辛苦了"):
        assert _is_small_talk(query), f"{query!r} 应判定为寒暄"
    for query in (
        "你好，素材包里有什么？",
        "第1段写了什么？",
        "帮我续写下一段",
        "你能做什么",
        "谢谢，顺便看看第2章",
    ):
        assert not _is_small_talk(query), f"{query!r} 不应判定为寒暄"


def test_needs_tool_round_skips_only_short_casual_messages():
    """短句且无意图词才跳过工具轮；带人物/段落/关系等意图词一律照查。"""
    from app.routers.assistant import _needs_tool_round

    for query in ("你好", "谢谢啦", "继续", "好的", "今天天气不错", "就这样吧"):
        assert not _needs_tool_round(query), f"{query!r} 不应跑工具轮"
    for query in (
        "许照是谁",
        "素材包里有什么",
        "第1段写了什么",
        "帮我续写下一段",
        "这段怎么样",
        "钱多多和许照什么关系",
        "今天写得挺顺的，我们接着聊聊后面的安排",
    ):
        assert _needs_tool_round(query), f"{query!r} 应跑工具轮"


def test_qa_tool_loop_respects_wall_clock_budget(db_session, project_id, monkeypatch):
    """整轮墙钟预算：第二步开始时已超 120s → 中止并回落，不再发下一次请求。"""
    import time as _time
    import types

    ticks = iter([0.0, 0.0, 121.0])
    shim = types.SimpleNamespace(
        monotonic=lambda: next(ticks, 121.0),
        perf_counter=_time.perf_counter,
    )
    monkeypatch.setattr(assistant_router, "time", shim)

    provider = _ScriptedProvider([
        {"content": "", "tool_calls": [_tool_call("get_reminders", "{}")],
         "finish_reason": "tool_calls", "usage": None, "model": "scripted-tool-model"},
    ])
    monkeypatch.setattr(assistant_router, "_ensure_assistant_provider", lambda: provider)

    result = assistant_router.assistant_qa(
        body=assistant_router.AssistantQARequest(
            project_id=project_id, query="有什么提醒"),
        db=db_session,
    )

    assert len(provider.calls) == 1, "超预算后仍发起了下一次模型请求"
    assert result["tool_mode"] == "deterministic"
    assert "预算" in result["tool_fallback_reason"]


def test_complete_is_a_thin_wrapper_over_complete_message(monkeypatch):
    """Writer 链路硬约束：`_complete()` 只做「取 content 转字符串」，无自有逻辑。"""
    from app.llm.provider import OpenAICompatibleChatProvider

    captured: list[dict] = []

    def _fake_complete_message(self, messages, *, tools=None, tool_choice=None,
                               stop_sequences=None):
        captured.append({"messages": messages, "tools": tools,
                         "tool_choice": tool_choice, "stop": stop_sequences})
        return {"content": "  正文内容  ", "tool_calls": [], "finish_reason": "stop",
                "usage": None, "model": "m"}

    monkeypatch.setattr(OpenAICompatibleChatProvider, "complete_message",
                        _fake_complete_message)
    provider = OpenAICompatibleChatProvider(
        api_key="test-key", base_url="https://example.com/v1", model="m")

    # Writer 走的是 chat(system, user) → _complete(messages) → complete_message。
    assert provider.chat("SYS", "USR", stop_sequences=["\n"]) == "  正文内容  "
    assert captured == [{
        "messages": [{"role": "system", "content": "SYS"},
                     {"role": "user", "content": "USR"}],
        "tools": None, "tool_choice": None, "stop": ["\n"],
    }]