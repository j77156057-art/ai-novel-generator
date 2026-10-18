"""Assistant「AI 腔记忆库 skill + 闭环匹配复用」回归测试（本轮新增，独立文件）。

对应实现：
- `backend/app/agents/registry.py::_impl_query_ai_flavor_memory`——
  记忆库注册为 Assistant 的**只读**工具（skill）；
- `backend/app/routers/assistant_generation.py::_run_ai_flavor_feedback`——
  闭环从「无脑渲染全部记忆」改为「按本次实际问题解析：该复用的复用、
  该规划的新规划」（resolve_tuning + 惰性 LLM + exclude_flagged 渲染）。

覆盖六类断言：

  ① `query_ai_flavor_memory` 已注册、`read_only=True`、在白名单内、不命中
     黑名单前缀，且注册表名字集合仍与白名单逐一对应；
  ② 工具行为：给 patterns → matched/unmatched（matched 带 solution /
     times_reused / recurrences / flagged_ineffective 等字段）；省略
     patterns → 在册记忆列表；纯只读（调用前后记忆行数与 prompt_overrides
     行数不变）；
  ③ 闭环复用（核心）：第一轮严重命中 → planned（新方案入库）；第二轮同
     模式严重命中 → reused，且 `solution` 与第一轮一字不差、`times_reused=2`
     ——证明「不用再重新规划微调方法，直接复用」；
  ④ 冷门模式才 LLM：模板覆盖内的模式 → provider.chat **零调用**；模板
     覆盖外的假模式 → chat 被调用（计数 provider）；
  ⑤ 闭环失败不影响主流程：monkeypatch 让 resolve_tuning 抛错 → 端点仍
     成功、正文已落库、无半截状态；
  ⑥ `ai_flavor_feedback` 返回体两个分支（generate_next / regenerate_segment）
     同键同形状。

依赖 `ai_flavor_memory` 的方案库 API（`match_memories` / `resolve_tuning` /
`load_memories(exclude_flagged=...)`）——签名以任务契约为准。若运行时该
API 尚未就绪，③④⑥ 会失败，那是要如实上报的信号：**不弱化断言**去凑绿。

库隔离由 tests/conftest.py 强制（独立临时文件库）；Writer 单例换确定性
StubProvider，Assistant provider 换计数 stub——不依赖真实网络。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import ai_flavor_memory, invoke_tool  # noqa: E402
from app.agents.registry import (  # noqa: E402
    ALLOWED_READ_ONLY_TOOLS,
    FORBIDDEN_PREFIXES,
    get_tool_registry,
    reset_tool_registry,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AIFlavorMemory,
    AgentRun,
    Generation,
    PromptOverride,
    Project,
)
from app.routers import assistant_generation as gen_router  # noqa: E402

# ---------------------------------------------------------------------------
# 测试数据
# ---------------------------------------------------------------------------

#: 严重命中：同一模式重复 9 次 → total=9，>= 记忆线 8（与既有闭环测试同口径）
SEVERE_TEXT = "仿佛" * 9

#: 模板覆盖外的假模式名（冷门模式）：`_detect_ai_flavor` 只认 engine 的 32 个
#: 既有模式，冷门模式无法从检测侧产生，只能从解析侧直接驱动——这正是「冷门
#: 模式才 LLM」的入口。用例里会再断言它确实不在 `_PATTERN_TYPES` 覆盖内。
COLD_PATTERN = "量子波动叙事症"

#: ⑥ 要求的反馈摘要键集合（两个分支必须同键同形状）
_FEEDBACK_KEYS = {
    "total", "summary", "alerted", "override_written",
    "reused", "replanned", "planned",
}


class StubProvider:
    """确定性假 Writer：chat/stream_chat 恒返回同一文本（与既有闭环测试同款）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "stub-writer"
        self.systems: list[str] = []
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.chat_calls += 1
        self.systems.append(system)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        self.stream_calls += 1
        self.systems.append(system)
        yield self.text


class CountingProvider:
    """计数假 Assistant provider：记录 chat 调用次数，恒返回确定性方案文本。

    冷门模式现场规划时，resolve_tuning 会把它的返回值当作微调方案沉淀——
    这里给一句确定性命中文本，让「chat 真被调用过」可被断言。
    """

    def __init__(self, reply: str = "冷门模式微调方案：删掉该表达，改为具体动作描写。") -> None:
        self.reply = reply
        self.model = "counting-assistant"
        self.chat_calls = 0
        self.systems: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.chat_calls += 1
        self.systems.append(system)
        return self.reply


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例：清 provider / tool 注册表与库（Writer 由各用例自行安装 stub）。"""
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
    proj = Project(
        title="AI 腔记忆 skill 测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        generation_length=3000,
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


# ---------------------------------------------------------------------------
# 小工具（harness）
# ---------------------------------------------------------------------------


def _make_project(db, title: str = "AI 腔记忆 skill 测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project_id: int, segment_no: int, text: str) -> int:
    """预置一段旧正文（regenerate 的目标段）。"""
    gen = Generation(
        project_id=project_id, segment_no=segment_no,
        chapter_range=f"段{segment_no}", text=text,
        summary={"text": f"第 {segment_no} 段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen.id


def _seed_memory(db, project_id: int, issue_key: str) -> None:
    """用既有 upsert API 种一条项目级记忆（solution 由方案库 API 管理）。"""
    ai_flavor_memory.upsert_memory(
        db,
        scope="project",
        project_id=project_id,
        issue_key=issue_key,
        summary=f"{issue_key} 式 AI 腔：本段命中 9 次（全段共 9 次）",
        hits_delta=1,
        examples=[{"segment_no": 1, "offset": 0, "snippet": issue_key}],
        segment_no=1,
        severity="high",
    )


def _install_writer(monkeypatch, provider: StubProvider) -> None:
    """把 Writer 单例换成确定性 stub（端点内部走 get_llm_provider() 拿到它）。"""
    monkeypatch.setattr(llm_provider_mod, "_PROVIDER", provider)


def _install_counting_assistant(monkeypatch, provider: CountingProvider) -> None:
    """把 Assistant provider 单例换成计数 stub（惰性句柄最终会取到它）。"""
    monkeypatch.setattr(llm_provider_mod, "_ASSISTANT_PROVIDER", provider)


def _invoke(db, **kwargs) -> dict:
    """真跑端点函数（不 mock 被测逻辑；只换 Writer provider）。"""
    body = gen_router.AssistantInvokeGenerationRequest(**kwargs)
    return gen_router.assistant_invoke_generation(body=body, db=db)


def _memories(db) -> list:
    return list(
        db.scalars(select(AIFlavorMemory).order_by(AIFlavorMemory.id)).all()
    )


# ---------------------------------------------------------------------------
# ① 注册与只读属性
# ---------------------------------------------------------------------------


def test_query_ai_flavor_memory_registered_and_read_only():
    """工具已注册、read_only=True、在白名单内、不命中黑名单前缀。"""
    reg = get_tool_registry()
    name = "query_ai_flavor_memory"

    assert name in ALLOWED_READ_ONLY_TOOLS, "新只读工具必须先进白名单"
    assert name in reg, "工具未注册到 Assistant 注册表"
    spec = reg.resolve(name)
    assert spec.read_only is True, "项目红线：Assistant 只能调只读工具"
    for prefix in FORBIDDEN_PREFIXES:
        assert not name.startswith(prefix), (
            f"工具名命中黑名单前缀 {prefix!r}——register() 应当直接拒绝"
        )
    # 注册表名字集合仍与白名单逐一对应（不多不少，避免后门工具混入）
    assert set(reg.names()) == set(ALLOWED_READ_ONLY_TOOLS)


# ---------------------------------------------------------------------------
# ② 工具行为：patterns / 无 patterns / 纯只读
# ---------------------------------------------------------------------------


def test_tool_with_patterns_returns_matched_and_unmatched(db_session, project_id):
    """给 patterns：matched 带 solution/times_reused 等字段，unmatched 原样带回。"""
    _seed_memory(db_session, project_id, "仿佛")

    result = invoke_tool(
        "query_ai_flavor_memory", db=db_session, project_id=project_id,
        patterns=["仿佛", COLD_PATTERN],
    )

    assert [m["issue_key"] for m in result["matched"]] == ["仿佛"]
    assert result["unmatched"] == [COLD_PATTERN]
    item = result["matched"][0]
    for field in ("issue_key", "summary", "solution", "times_reused",
                  "recurrences", "flagged_ineffective", "hit_count"):
        assert field in item, (
            f"matched item 缺字段 {field}——助手要靠这些字段判断"
            "「这个方案还靠不靠谱」"
        )


def test_tool_without_patterns_returns_registered(db_session, project_id):
    """省略 patterns：返回在册记忆列表（memories + count）。"""
    _seed_memory(db_session, project_id, "仿佛")
    _seed_memory(db_session, project_id, "宛如")

    result = invoke_tool(
        "query_ai_flavor_memory", db=db_session, project_id=project_id
    )

    assert result["count"] == 2
    assert {m["issue_key"] for m in result["memories"]} == {"仿佛", "宛如"}


def test_tool_is_pure_read_only(db_session, project_id):
    """纯只读：调用前后记忆行数与 prompt_overrides 行数不变。"""
    _seed_memory(db_session, project_id, "仿佛")

    def _counts() -> tuple:
        db_session.expire_all()
        return (
            db_session.scalar(select(func.count()).select_from(AIFlavorMemory)),
            db_session.scalar(select(func.count()).select_from(PromptOverride)),
        )

    before = _counts()
    invoke_tool(
        "query_ai_flavor_memory", db=db_session, project_id=project_id,
        patterns=["仿佛", COLD_PATTERN],
    )
    invoke_tool("query_ai_flavor_memory", db=db_session, project_id=project_id)
    assert _counts() == before, "只读工具不得写任何表"


# ---------------------------------------------------------------------------
# ③ 闭环复用（核心）：第一轮 planned，第二轮 reused 且 solution 一字不差
# ---------------------------------------------------------------------------


def test_second_hit_reuses_planned_solution(db_session, monkeypatch):
    """第一轮严重命中 → planned 入库；第二轮同模式 → reused、solution 不差、times_reused=2。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    first = _invoke(db_session, project_id=proj.id, action="generate_next",
                    instruction="第一段")
    fb1 = first["ai_flavor_feedback"]
    assert fb1 is not None, "闭环应走通（方案库 API 未就绪时这里会先失败）"
    assert [p["issue_key"] for p in fb1["planned"]] == ["仿佛"], (
        f"第一轮应规划新方案：{fb1['planned']}"
    )
    assert fb1["reused"] == [] and fb1["replanned"] == []
    solution_1 = fb1["planned"][0]["solution"]
    assert solution_1, "规划出的 solution 不能为空"

    second = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="第二段")
    fb2 = second["ai_flavor_feedback"]
    assert fb2 is not None
    assert [r["issue_key"] for r in fb2["reused"]] == ["仿佛"], (
        f"第二轮应复用老方案：{fb2['reused']}"
    )
    assert fb2["planned"] == []
    # 「不用再重新规划微调方法，直接复用」的一字不差证据
    assert fb2["reused"][0]["solution"] == solution_1
    assert fb2["reused"][0]["times_reused"] == 2

    # 同一模式仍只有一行记忆（复用不插新行、不换行）
    rows = _memories(db_session)
    assert len(rows) == 1 and rows[0].issue_key == "仿佛"

    # skill 与闭环同源：工具侧查到的也是同一份方案、同一个复用计数
    tool_result = invoke_tool(
        "query_ai_flavor_memory", db=db_session, project_id=proj.id,
        patterns=["仿佛"],
    )
    matched = tool_result["matched"]
    assert len(matched) == 1
    assert matched[0]["solution"] == solution_1
    assert matched[0]["times_reused"] == 2


# ---------------------------------------------------------------------------
# ④ 冷门模式才 LLM（计数 provider）
# ---------------------------------------------------------------------------


def test_covered_pattern_makes_zero_llm_calls(db_session, monkeypatch):
    """模板覆盖内的模式（仿佛）：模板优先，provider.chat 零调用。"""
    proj = _make_project(db_session)
    counting = CountingProvider()
    _install_counting_assistant(monkeypatch, counting)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")

    assert result["segment"]["text"] == SEVERE_TEXT
    assert counting.chat_calls == 0, (
        f"已覆盖模式必须走模板，chat 却被调了 {counting.chat_calls} 次"
    )


def test_cold_pattern_triggers_llm(db_session, monkeypatch):
    """模板覆盖外的假模式：现场规划，provider.chat 被调用。"""
    proj = _make_project(db_session)
    counting = CountingProvider()
    _install_counting_assistant(monkeypatch, counting)

    # 测试前提：该模式必须不在模板覆盖内（契约字段 _PATTERN_TYPES）
    covered = set(getattr(ai_flavor_memory, "_PATTERN_TYPES", None) or {})
    assert COLD_PATTERN not in covered, (
        f"{COLD_PATTERN!r} 不该被模板覆盖，否则本用例失去意义"
    )

    # 检测侧换成只报冷门模式的假检测：_detect_ai_flavor 只认 engine 的 32 个
    # 既有模式，冷门模式只能从解析侧驱动。注意 _should_rewrite 仍走 engine
    # 真检测（告警线与本用例无关）。
    monkeypatch.setattr(gen_router, "_detect_ai_flavor", lambda text: {
        "hits": {COLD_PATTERN: 9},
        "total": 9,
        "occurrences": {COLD_PATTERN: [{"offset": 0, "snippet": COLD_PATTERN}]},
    })

    out = gen_router._run_ai_flavor_feedback(
        db_session, proj, COLD_PATTERN * 9, 1, "测试冷门模式"
    )

    assert out is not None
    assert counting.chat_calls >= 1, (
        f"冷门模式应现场规划（走 LLM），chat 却零调用"
        f"（chat_calls={counting.chat_calls}）"
    )
    assert [p["issue_key"] for p in out["planned"]] == [COLD_PATTERN]


# ---------------------------------------------------------------------------
# ⑤ 闭环失败不影响主流程
# ---------------------------------------------------------------------------


def test_feedback_failure_does_not_break_generation(db_session, monkeypatch):
    """resolve_tuning 抛错：端点仍成功、正文已落库、闭环摘要 None、无半截状态。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    def _boom(db, project_id, evidence, *, llm=None):
        raise RuntimeError("resolve_tuning 炸锅（模拟解析失败）")

    monkeypatch.setattr(ai_flavor_memory, "resolve_tuning", _boom)

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段会触发闭环炸锅的")

    assert result["segment"]["text"] == SEVERE_TEXT
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run is not None and run.status == "success"
    assert result["ai_flavor_feedback"] is None, "闭环失败时摘要为 None"
    assert _memories(db_session) == [], "半截写入必须被 rollback 丢掉"
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0
    # 正文仍在（主流程的提交不受闭环回滚影响）
    assert db_session.scalar(
        select(func.count()).select_from(Generation)) == 1


# ---------------------------------------------------------------------------
# ⑥ 两个分支同键同形状
# ---------------------------------------------------------------------------


def test_feedback_keys_identical_across_branches(db_session, monkeypatch):
    """ai_flavor_feedback：generate_next 与 regenerate_segment 同键同形状。

    历史教训：两处各写一遍必漂移。这里直接钉死键集合，任一侧增删字段都会
    在这里失败，而不是漂到前端才被发现。
    """
    proj = _make_project(db_session)
    _seed_segment(db_session, proj.id, 1, "这是第一段的旧正文。" * 20)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    next_fb = _invoke(
        db_session, project_id=proj.id, action="generate_next",
        instruction="续写一段",
    )["ai_flavor_feedback"]
    regen_fb = _invoke(
        db_session, project_id=proj.id, action="regenerate_segment",
        segment_no=1, instruction="重写第一段",
    )["ai_flavor_feedback"]

    assert next_fb is not None and regen_fb is not None, (
        "两个分支都应走通闭环（方案库 API 未就绪时这里会先失败）"
    )
    assert set(next_fb) == _FEEDBACK_KEYS, (
        f"generate_next 分支键集合漂移：{sorted(next_fb)}"
    )
    assert set(regen_fb) == _FEEDBACK_KEYS, (
        f"regenerate_segment 分支键集合漂移：{sorted(regen_fb)}"
    )
