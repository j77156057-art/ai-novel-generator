"""Assistant 只读检索端点测试（ADR-2026-10-12 第 5 步第 2 步验收）。

覆盖需求（与 team-lead 派单一致）：
  (a) Assistant 端点无写权限字段 → 请求体携带 write_*/commit_*/patch_json 等字段
      必须被 pydantic v2 拒绝（422），且 router 代码里没有任何"写"SQL 路径。
  (b) 调用 Assistant provider 失败 → Writer 单例不被污染。
  (c) AgentRun / Evidence 落库字段完整：id / project_id / role='assistant' /
      status / started_at / finished_at / request_hash / model 全部存在且语义正确。
  (d) 写工具注册被 Assistant 注册表拒绝：register(spec with read_only=False) →
      PermissionError；register(spec with name 以 write_/commit_ 等黑名单前缀开头) →
      PermissionError；invoke 黑名单前缀 tool → PermissionError。

不依赖真实 DB：所有路径走 sqlite 内存库（init_db + Base.metadata.create_all），
不会污染 backend/novel_dev.db。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

# ─── bootstrap ──────────────────────────────────────────────────────────
# 把 backend/ 加进 sys.path；与既有 test_provider_*.py 风格保持一致。
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents.registry import (  # noqa: E402
    ALLOWED_READ_ONLY_TOOLS,
    FORBIDDEN_PREFIXES,
    AssistantToolRegistry,
    ToolSpec,
    invoke_tool,
    reset_tool_registry,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LLMProvider, LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import (  # noqa: E402
    ASSISTANT_ROLE,
    WRITER_ROLE,
    reset_provider_registry,
)
from app.models import AgentRun, Evidence, Project  # noqa: E402
from app.routers import assistant as assistant_router  # noqa: E402
from sqlalchemy import select  # noqa: E402


# ====================================================================
# fixtures：内存库 + provider 单例隔离
# ====================================================================

@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空：provider registry / writer 单例 / assistant 单例 / tool registry / 内存库。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    # init_db 会在 lifespan 里跑；测试里直接 create_all 即可（Base.metadata 已含 4 张新表）
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
    proj = Project(title="Assistant 测试项目", target_length=5000, mode="quick", status="created")
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


# ====================================================================
# (a) Assistant 端点无写权限字段
# ====================================================================


def test_assistant_request_body_rejects_write_fields():
    """AssistantQARequest 携带 write_* / commit_* / patch_json / target_table 等写字段

    必须被 pydantic v2 拒（422）；StrictRequest 的 extra='forbid' 是兜底防线。
    """
    from app.routers.assistant import AssistantDiagnoseRequest, AssistantQARequest

    # QA 路径：拼写 patch_json / target_table 写字段 → 拒
    with pytest.raises(ValueError):
        AssistantQARequest(
            project_id=1,
            query="测试",
            patch_json={"chapters": [{"rewrite": "..."}]},
        )

    # 诊断路径：scope + write_segment 字段 → 拒
    with pytest.raises(ValueError):
        AssistantDiagnoseRequest(
            project_id=1,
            scope="all",
            write_segment=7,
        )

    # 即便字段名是"读"语义但落在黑名单前缀（commit_*）→ 同样被拒
    with pytest.raises(ValueError):
        AssistantQARequest(
            project_id=1,
            query="测试",
            commit_hash="deadbeef",
        )


def test_assistant_router_has_no_write_path():
    """静态扫描：assistant router 源码里不出现 INSERT/UPDATE/DELETE/SQLAlchemy session.add() 写路径。

    例外：AgentRun / Evidence 是助手**自身**审计表（架构允许助手写自己的 run + evidence）；
    但 chapters / characters / beats / plot_bible / fact / kg_nodes / generations 等业务表
    任何写操作都不在 router 中出现。
    """
    import inspect

    src = inspect.getsource(assistant_router)
    forbidden_writes = (
        "INSERT INTO chapters",
        "INSERT INTO characters",
        "INSERT INTO beats",
        "UPDATE chapters SET",
        "UPDATE characters SET",
        "UPDATE beats SET",
        "DELETE FROM chapters",
        "DELETE FROM characters",
        "DELETE FROM beats",
    )
    for snippet in forbidden_writes:
        assert snippet not in src, f"assistant router 不应写业务表，但出现: {snippet!r}"

    # 编辑提案 / 版本表的写入端点也不应暴露（ADR §4 第 4 步之前）
    assert "/proposals" not in src, "assistant router 不应暴露 EditProposal 写入端点"
    assert "/versions" not in src, "assistant router 不应暴露 Version 写入端点"


# ====================================================================
# (b) 调用 Assistant provider 失败不污染 Writer 单例
# ====================================================================


def test_assistant_provider_failure_does_not_corrupt_writer(monkeypatch):
    """Assistant 构造抛错时 Writer 已缓存实例原样保留；Assistant 不入缓存。

    这是 ProviderRegistry 已通过测试的回归确认 + 与 assistant router 端点的
    端到端联动：即路由层调 `get_provider_registry().get('assistant')` 时
    失败不应触发 Writer 句柄丢失。
    """
    # 1) Writer 健康入缓存
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_health = llm_provider_mod._PROVIDER
    assert writer_health is not None

    # 2) 让 assistant 工厂抛错
    def boom():
        raise RuntimeError("assistant build boom")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)

    # 3) 通过 router 路径调一次 → 端到端验证
    with pytest.raises(RuntimeError, match="assistant build boom"):
        assistant_router._ensure_assistant_provider()

    # 4) Writer 单例原样保留（未受波及）
    assert llm_provider_mod._PROVIDER is writer_health


def test_assistant_qa_endpoint_does_not_touch_writer_singleton(monkeypatch, db_session, project_id):
    """直接调 /api/assistant/qa：assistant provider 抛错 → 响应仍合法 + Writer 不丢。"""
    # 锁住 Writer
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_before = llm_provider_mod._PROVIDER

    # 把 assistant 工厂设为抛错
    def boom():
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)

    # 走 router 函数（FastAPI 路由的内部函数；签名兼容）
    from app.routers.assistant import AssistantQARequest
    body = AssistantQARequest(project_id=project_id, query="测试问题")
    result = assistant_router.assistant_qa(body=body, db=db_session)

    # 响应合法（包含必备字段）
    for key in ("answer", "evidence", "confidence", "agent_run_id"):
        assert key in result, f"qa 响应缺字段: {key}"
    # answer 是降级文案；evidence 仍可能有 reminder/segment 条目
    assert "Assistant provider 不可用" in result["answer"]

    # Writer 单例仍然健康
    assert llm_provider_mod._PROVIDER is writer_before


# ====================================================================
# (c) AgentRun / Evidence 落库字段完整
# ====================================================================


def test_agent_run_and_evidence_persisted(db_session, project_id):
    """AssistantQARequest 跑一次 → AgentRun + Evidence 行落库，字段全、语义对。

    ## 契约写清：这条用例守的是**降级三件套必落 evidence**，不是「任何工具都产证据」

    本用例跑在「assistant provider 未配 key → LocalFallbackLLMProvider」这条路径上，
    此时 `/qa` 走确定性降级：固定跑 `read_generation_segments` /
    `search_novel_chunks` / `get_reminders` 三件套，三件套**必须**落 evidence。
    断言里的 kind/source 白名单就是这口径。

    真 ReAct 路径的口径不同（这也是为什么下面的 `react` 断言把路径显式绑定住）：

    - `get_character_profile` / `get_plot_timeline` / `audit_kg` → `kind="kg"`；
    - `get_fact_ledger` → `kind="fact"`；
    - `summarize_bible` / `analyze_logs` / `audit_corpus` 等**按设计不产业务证据**
      （回执只在 `react.rounds` / `trace_json` 里）——若在只调 `summarize_bible`
      的路径上要求 `evidence_rows` 非空，那是**测试契约错**，不是产品缺陷，
      **不要为了让它绿而去给 summarize_bible 造 evidence**。
      真 ReAct 的等价覆盖在
      `tests/test_assistant_react_loop.py::test_profile_tool_writes_kg_evidence`。
    """
    from app.routers.assistant import AssistantQARequest

    body = AssistantQARequest(project_id=project_id, query="谁是主角？")
    result = assistant_router.assistant_qa(body=body, db=db_session)

    # 显式绑定路径：provider 是本地桩 → 确定性降级 → 三件套
    react = result.get("react") or {}
    assert react.get("provider_kind") == "stub", (
        "本用例守的是「provider 不可用 → 三件套必落 evidence」这条路径；"
        f"当前 provider_kind={react.get('provider_kind')!r}。"
        "若要以真实/假模型跑，请改用 test_assistant_react_loop.py 里的 profile 用例。"
    )
    assert react.get("mode") == "deterministic_fallback"

    # AgentRun 落库
    run_id = result["agent_run_id"]
    run = db_session.get(AgentRun, run_id)
    assert run is not None, "AgentRun 未落库"
    assert run.project_id == project_id
    assert run.role == "assistant", f"role 必须为 assistant，实际 {run.role!r}"
    assert run.status == "success"
    assert run.started_at is not None
    assert run.finished_at is not None
    # finished_at >= started_at
    assert run.finished_at >= run.started_at
    # request_hash 必须是 64 位 sha256 hex
    assert isinstance(run.request_hash, str) and len(run.request_hash) == 64

    # Evidence 落库（至少 1 条；本测试不强制条数）
    evidence_rows = db_session.scalars(
        select(Evidence).where(Evidence.run_id == run_id)
    ).all()
    assert evidence_rows, "Evidence 未落库"
    for row in evidence_rows:
        # 字段完整性
        assert row.id is not None
        assert row.run_id == run_id
        assert row.kind in ("generation", "chunk", "reminder", "fact", "kg")
        assert row.source in ("generation", "novel", "fact", "kg", "reminder")
        assert isinstance(row.ref_id, str) and row.ref_id != ""
        # score 解析（sqlite 上存为字符串）
        score_val = float(row.score) if row.score is not None else 0.0
        assert 0.0 <= score_val <= 1.0


def test_diagnose_run_persists_with_model_field(db_session, project_id):
    """诊断路径也要落 AgentRun（status / model 字段填好）。"""
    from app.routers.assistant import AssistantDiagnoseRequest

    body = AssistantDiagnoseRequest(project_id=project_id, scope="all")
    result = assistant_router.assistant_diagnose(body=body, db=db_session)

    run_id = result["agent_run_id"]
    run = db_session.get(AgentRun, run_id)
    assert run is not None
    assert run.role == "assistant"
    assert run.status in ("success", "failed")
    # finished_at 已填
    assert run.finished_at is not None


# ====================================================================
# (d) 写工具注册被 Assistant 注册表拒绝
# ====================================================================


def test_register_rejects_write_only_tool():
    """register(ToolSpec with read_only=False) → PermissionError，不写入 _tools。"""
    reg = AssistantToolRegistry()

    def fake_write():
        return {}

    bad_spec = ToolSpec(
        name="write_anything",  # 同时命中黑名单前缀 + read_only=False
        description="恶意写工具",
        read_only=False,
        callable=fake_write,
    )
    with pytest.raises(PermissionError):
        reg.register(bad_spec)
    assert "write_anything" not in reg.names()

    # 即便 read_only=True，名字以 write_ 开头 → 仍被拒（双重门）
    sneaky = ToolSpec(
        name="write_novel_chunks",
        description="试图绕过",
        read_only=True,
        callable=fake_write,
    )
    with pytest.raises(PermissionError):
        reg.register(sneaky)
    assert "write_novel_chunks" not in reg.names()

    # 黑名单前缀还有 commit_ / update_ / delete_ / patch_ / rollback_ / apply_proposal
    for prefix in ("commit_x", "update_y", "delete_z", "patch_w", "rollback_v", "apply_proposal_uvw"):
        with pytest.raises(PermissionError):
            reg.register(ToolSpec(name=prefix, description="x", read_only=True,
                                  callable=fake_write))
        assert prefix not in reg.names()


def test_invoke_rejects_blacklist_prefix():
    """invoke(name='write_*') → PermissionError，即便工具**没**注册（双重门）。"""
    reg = AssistantToolRegistry()

    for name in ("write_anything", "commit_x", "rollback_now"):
        with pytest.raises(PermissionError):
            reg.invoke(name, db=None, project_id=1)


def test_default_tool_set_is_read_only_and_white_listed():
    """get_tool_registry() 默认挂的工具集 = ALLOWED_READ_ONLY_TOOLS，且全是 read_only=True。"""
    reg = AssistantToolRegistry()  # 不用单例，自构造以便隔离
    # 注册所有默认 spec
    from app.agents.registry import _register_default_tools
    _register_default_tools(reg)

    names = set(reg.names())
    # 默认白名单必须全部到位
    assert set(ALLOWED_READ_ONLY_TOOLS) <= names, (
        f"默认工具集缺白名单项；缺: {set(ALLOWED_READ_ONLY_TOOLS) - names}"
    )
    # 所有已注册工具都是 read_only=True
    for n in names:
        spec = reg.resolve(n)
        assert spec.read_only is True, f"工具 {n!r} 不是 read_only=True"
        # 名字也不在黑名单前缀
        for prefix in FORBIDDEN_PREFIXES:
            assert not n.startswith(prefix), (
                f"工具 {n!r} 命中黑名单前缀 {prefix!r}（注册表漏检）"
            )


def test_invoke_tool_dispatches_to_readonly_callable(db_session, project_id):
    """invoke_tool 透传 db / project_id / kwargs 到底层只读 callable；返回 dict。"""
    # 直接走 _impl_summarize_bible（最简单、无外部依赖的路径）
    result = invoke_tool("summarize_bible", db=db_session, project_id=project_id)
    assert isinstance(result, dict)
    # project 存在 → 返回 plot_bible/premise/outline（项目刚建，全是 None / 空）
    assert "plot_bible" in result


def test_router_request_unknown_tool_path_is_handled(monkeypatch, db_session, project_id):
    """如果 assistant 工具内部调一个未注册的 name → KeyError；router 当前不主动触发此路径，

    但我们要保证 AssistantToolRegistry 自身的契约行为，避免静默吞错。
    """
    reg = AssistantToolRegistry()
    with pytest.raises(KeyError):
        reg.invoke("never_registered_tool", db=db_session, project_id=project_id)
