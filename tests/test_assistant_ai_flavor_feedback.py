"""Assistant AI 腔反馈闭环回归测试（「AI 腔反馈闭环」第三层：主 agent 检查 sub 产出）。

对应实现：`backend/app/routers/assistant_generation.py::_run_ai_flavor_feedback`
（Assistant 是主 agent：生成成功后由它检查 Writer 的产出、总结本次 AI 腔问题、
写 `ai_flavor_memories`、并微调 `prompt_overrides` 的 constraint_note 槽位——
微调对象是下次给 Writer 的输入，不是重写本次文本）。

覆盖任务列出的六类断言 + 两个挂载点 + 零 LLM 纪律：

  ① 命中且严重（total >= 记忆线 8）：写记忆（hit_count=1、带例句）且
     constraint_note 被写入非空文本（含模式名/例句）；
  ② 未达阈值（0 < total < 8，且过了重写告警线 5）：不写记忆、不写 override；
  ③ 干净文本（total == 0）：同上，什么都不写；
  ④ 闭环异常不影响主流程：monkeypatch 让 upsert_memory 在第二条记忆上炸锅，
     端点仍正常返回、正文已落库，且没有半截状态（记忆表/覆写表均为空）；
  ⑤ 幂等/累加：同一模式连续两次检查命中，hit_count 累加为 2 而不是两行；
  ⑥ override 可被下次生成读到：engine._prompt_overrides 取到闭环写的文本，
     且它真的出现在下一次生成的 Writer 系统提示词里；
  ⑦ regenerate_segment 分支同样挂闭环（两个挂载点行为一致，不漂移）；
  ⑧ 闭环自身零 LLM：直接驱动闭环函数，provider 的 chat/stream 一次都没被碰。

真跑端点函数 `assistant_invoke_generation`（不 mock 被测逻辑），Writer 单例换
确定性 StubProvider；DB 用 tests/conftest.py 强制的独立临时库。
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
from app.agents import ai_flavor_memory  # noqa: E402
from app.agents.prompt_overrides import PromptKey  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation import engine as engine_mod  # noqa: E402
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
# 测试数据：三段确定性文本，total 分别落在 9（严重）/ 6（轻微）/ 0（干净）
# ---------------------------------------------------------------------------

#: 严重命中：同一模式重复 9 次 → total=9，>= 记忆线 8（也 >= 重写告警线 5）
SEVERE_TEXT = "仿佛" * 9

#: 双模式严重文本：仿佛×5 + 宛如×4 → total=9（用于「半截状态」用例：让第一条
#: 记忆写成功、第二条炸锅，验证闭环整体回滚）
SEVERE_TWO_PATTERN_TEXT = "仿佛" * 5 + "宛如" * 4

#: 轻微命中：同一模式重复 6 次 → total=6，过了重写告警线（>=5）但未达记忆线
MILD_TEXT = "仿佛" * 6

#: 干净散文：不含任何 AI_FLAVOR_PATTERNS 串（与既有告警测试同款）
CLEAN_TEXT = "林夜走在长街上。夜风微凉，街灯昏黄。他停步回望，身影被拉得很长。"


class StubProvider:
    """确定性假 Writer：chat/stream_chat 恒返回同一文本，并记录收到的提示词。

    `model` 属性备着：端点用 `_writer_model_label()` 回显 Writer 模型名。
    不实现 summarize_rollup——让 engine 的 `_rollup_fn` 回落到确定性兜底
    （与 test_regenerate_ai_flavor_alert.py 同一手法）。
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "stub-writer"
        self.systems: list[str] = []
        self.chat_calls = 0
        self.stream_calls = 0

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.chat_calls += 1
        self.systems.append(system)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        self.stream_calls += 1
        self.systems.append(system)
        yield self.text


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例：清 provider 注册表与库（Writer 单例由各用例自行安装 stub）。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 小工具（harness）
# ---------------------------------------------------------------------------


def _make_project(db, title: str = "AI 腔反馈闭环测试") -> Project:
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


def _install_writer(monkeypatch, provider: StubProvider) -> None:
    """把 Writer 单例换成确定性 stub（端点内部走 get_llm_provider() 拿到它）。"""
    monkeypatch.setattr(llm_provider_mod, "_PROVIDER", provider)


def _invoke(db, **kwargs) -> dict:
    """真跑端点函数（不 mock 被测逻辑；只换 Writer provider）。"""
    body = gen_router.AssistantInvokeGenerationRequest(**kwargs)
    return gen_router.assistant_invoke_generation(body=body, db=db)


def _memories(db) -> list:
    return list(
        db.scalars(select(AIFlavorMemory).order_by(AIFlavorMemory.id)).all()
    )


def _active_constraint_override(db, project_id: int) -> PromptOverride | None:
    return db.scalars(
        select(PromptOverride).where(
            PromptOverride.project_id == project_id,
            PromptOverride.prompt_key == PromptKey.CONSTRAINT,
            PromptOverride.active.is_(True),
        )
    ).first()


# ---------------------------------------------------------------------------
# ① 命中且严重：写记忆（hit_count=1）+ 微调 constraint_note
# ---------------------------------------------------------------------------


def test_severe_hit_writes_memory_and_override(db_session, monkeypatch):
    """total=9（>= 记忆线 8）：闭环写一行项目级记忆，并覆写 constraint_note。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段氛围铺垫")

    # 主流程照常成功（闭环不影响生成）
    assert result["segment"]["segment_no"] == 1
    assert result["segment"]["text"] == SEVERE_TEXT

    # 记忆：一行项目级记忆，hit_count=1，severity=high（本段 9 次 >= 4），带例句
    rows = _memories(db_session)
    assert len(rows) == 1, f"同一模式只写一行，实际 {len(rows)} 行"
    row = rows[0]
    assert row.scope == "project"
    assert row.project_id == proj.id
    assert row.issue_key == "仿佛"
    assert row.hit_count == 1
    assert row.severity == "high"
    assert row.last_segment_no == 1
    assert row.summary and "9" in row.summary, f"总结应含本段密度：{row.summary}"
    assert row.examples, "必须带上例句证据"
    assert row.examples[0]["segment_no"] == 1
    assert row.examples[0]["offset"] == 0
    assert "仿佛" in row.examples[0]["snippet"]

    # override：constraint_note 槽位被写入非空文本，内容含命中的模式名
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None, "严重命中必须微调 constraint_note"
    assert ov.override_text
    assert "仿佛" in ov.override_text
    assert gen_router._FLAVOR_OVERRIDE_HEADER in ov.override_text
    assert ov.created_by == gen_router._FLAVOR_OVERRIDE_CREATED_BY
    # 「本轮优先避免」来自历史教训，不是用户本轮指令的话
    assert "氛围铺垫" not in ov.override_text


# ---------------------------------------------------------------------------
# ② 未达阈值（过了重写告警线 5，但未达记忆线 8）：只告警，不写任何东西
# ---------------------------------------------------------------------------


def test_mild_hit_below_memory_line_writes_nothing(db_session, monkeypatch):
    """total=6（>=5 告警线、<8 记忆线）：不写记忆、不写 override。"""
    # 先把边界钉住：这条文本确实「过了告警线但未达记忆线」
    detected = engine_mod._detect_ai_flavor(MILD_TEXT)
    assert detected["total"] == 6
    assert detected["total"] >= 5, "应已跨过 _should_rewrite 的告警线"
    assert detected["total"] < gen_router._AI_FLAVOR_MEMORY_MIN_TOTAL

    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(MILD_TEXT))

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")

    # 主流程不受影响（引擎侧的告警/重写与闭环无关）
    assert result["segment"]["text"] == MILD_TEXT
    # 闭环侧：什么都没写
    assert _memories(db_session) == []
    assert _active_constraint_override(db_session, proj.id) is None
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0


# ---------------------------------------------------------------------------
# ③ 干净文本（total == 0）：不写任何东西
# ---------------------------------------------------------------------------


def test_clean_text_writes_nothing(db_session, monkeypatch):
    """total=0：闭环提前返回，不写记忆、不写 override。"""
    assert engine_mod._detect_ai_flavor(CLEAN_TEXT)["total"] == 0

    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(CLEAN_TEXT))

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段日常过渡")

    assert result["segment"]["text"] == CLEAN_TEXT
    assert _memories(db_session) == []
    assert _active_constraint_override(db_session, proj.id) is None
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0


# ---------------------------------------------------------------------------
# ④ 闭环异常不影响主流程（monkeypatch upsert_memory 半路炸锅）
# ---------------------------------------------------------------------------


def test_feedback_failure_does_not_break_generation(db_session, monkeypatch):
    """闭环第二步（写记忆）炸锅：端点仍成功返回，且没有半截状态。

    实测方式：双模式严重文本（仿佛×5 + 宛如×4），patch 后的 upsert_memory
    在**第二条**记忆上抛异常——第一条已 flush 成功。断言接口照常返回、
    AgentRun=success、正文已落库，且记忆表/覆写表均为空（第一条的半截写入
    被闭环自己的 rollback 丢掉）。
    """
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TWO_PATTERN_TEXT))

    calls = {"n": 0}
    real_upsert = ai_flavor_memory.upsert_memory

    def _flaky_upsert(db, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("第二条记忆写入炸锅（模拟半截状态）")
        return real_upsert(db, **kwargs)

    monkeypatch.setattr(ai_flavor_memory, "upsert_memory", _flaky_upsert)

    # 不抛异常——闭环的失败被吞掉，主流程的成功照常返回
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段会触发闭环炸锅的")

    assert result["segment"]["segment_no"] == 1
    assert result["segment"]["text"] == SEVERE_TWO_PATTERN_TEXT
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run is not None and run.status == "success"
    assert calls["n"] == 2, "闭环确实尝试写了两模式两条记忆"

    # 没有半截状态：第一条已 flush 的记忆行被整体回滚，覆写也没写
    assert _memories(db_session) == [], "半截记忆行必须被 rollback 丢掉"
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0
    # 正文仍在（主流程的提交不受闭环回滚影响）
    assert db_session.scalar(
        select(func.count()).select_from(Generation)) == 1


# ---------------------------------------------------------------------------
# ⑤ 幂等/累加：同一模式连续两次命中，hit_count 累加为 2 而不是两行
# ---------------------------------------------------------------------------


def test_repeated_hits_accumulate_single_row(db_session, monkeypatch):
    """同一模式两次检查命中：一行累加 hit_count=2；override 刷新且仍只有一条 active。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一段")
    rows = _memories(db_session)
    assert len(rows) == 1 and rows[0].hit_count == 1

    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第二段")
    rows = _memories(db_session)
    assert len(rows) == 1, f"同一模式只累加一行，实际 {len(rows)} 行"
    assert rows[0].hit_count == 2, "第二次检查应累加为 2"
    assert rows[0].last_segment_no == 2

    # override 随之刷新（累计次数变了），但同一 (project, key) 仍只有一条 active
    actives = db_session.scalars(
        select(PromptOverride).where(
            PromptOverride.project_id == proj.id,
            PromptOverride.prompt_key == PromptKey.CONSTRAINT,
            PromptOverride.active.is_(True),
        )
    ).all()
    assert len(actives) == 1
    assert "累计 2 次" in actives[0].override_text
    # 历史不删：旧的那条被降级保留
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 2


# ---------------------------------------------------------------------------
# ⑥ override 内容可被下次生成读到（engine._prompt_overrides + 真进提示词）
# ---------------------------------------------------------------------------


def test_override_readable_by_next_generation(db_session, monkeypatch):
    """闭环写出的 constraint_note：_prompt_overrides 读得到，且进了下次的系统提示。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一段")

    expected = _active_constraint_override(db_session, proj.id).override_text
    overrides = engine_mod._prompt_overrides(db_session, proj.id)
    assert overrides[PromptKey.CONSTRAINT] == expected
    assert "仿佛" in overrides[PromptKey.CONSTRAINT]

    # 下一次生成（干净文本）时，Writer 收到的系统提示词里带着这段约束
    clean_writer = StubProvider(CLEAN_TEXT)
    _install_writer(monkeypatch, clean_writer)
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="第二段")
    assert result["segment"]["text"] == CLEAN_TEXT
    assert clean_writer.systems, "Writer 应收到过系统提示词"
    assert any(
        gen_router._FLAVOR_OVERRIDE_HEADER in s for s in clean_writer.systems
    ), "闭环写的约束必须出现在下次生成的系统提示词里"
    # 干净文本不触发闭环，覆写保持上一轮的内容
    assert _active_constraint_override(
        db_session, proj.id).override_text == expected


# ---------------------------------------------------------------------------
# ⑦ regenerate_segment 分支同样挂闭环（两个挂载点行为一致）
# ---------------------------------------------------------------------------


def test_regenerate_branch_runs_feedback(db_session, monkeypatch):
    """重生成分支：主流程提交后同样走闭环（记忆 + 微调），与续写分支不漂移。"""
    proj = _make_project(db_session)
    _seed_segment(db_session, proj.id, 1, "这是第一段的旧正文。" * 20)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    result = _invoke(db_session, project_id=proj.id,
                     action="regenerate_segment", segment_no=1,
                     instruction="重写第一段，加强氛围")

    assert result["segment"]["text"] == SEVERE_TEXT
    rows = _memories(db_session)
    assert len(rows) == 1
    assert rows[0].hit_count == 1
    assert rows[0].last_segment_no == 1
    assert rows[0].issue_key == "仿佛"
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None and "仿佛" in ov.override_text


# ---------------------------------------------------------------------------
# ⑧ 闭环自身零 LLM（直接驱动闭环函数，provider 一次都没被碰）
# ---------------------------------------------------------------------------


def test_feedback_loop_makes_zero_llm_calls(db_session, monkeypatch):
    """闭环只用纯本地检测 + DB 读写：安装着 Writer 单例，但它一次都没被调。"""
    proj = _make_project(db_session)
    writer = StubProvider(SEVERE_TEXT)
    _install_writer(monkeypatch, writer)

    out = gen_router._run_ai_flavor_feedback(
        db_session, proj, SEVERE_TEXT, 1, "写一段"
    )

    assert writer.chat_calls == 0, "闭环绝不许调 provider.chat"
    assert writer.stream_calls == 0, "闭环绝不许调 provider.stream_chat"
    assert out is not None and out["total"] == 9
    assert out["summary"] and "仿佛" in out["summary"]
    # 闭环自己持有事务边界：不依赖调用方再 commit，行已可见
    rows = _memories(db_session)
    assert len(rows) == 1 and rows[0].hit_count == 1
