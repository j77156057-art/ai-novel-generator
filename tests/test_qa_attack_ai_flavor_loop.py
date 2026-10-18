"""QA 攻击式验证（Edward）：AI 腔反馈闭环三层交付的证伪探针。

被验对象（branch feat/resume-push-2026-10, HEAD 2fd9c1b）：
  - 18f017c AIFlavorMemory 模型 + agents/ai_flavor_memory.py 读写层
  - 4227c73 _detect_ai_flavor 扩展 occurrences + 子串去重 + prompt override 接入
  - 2fd9c1b routers/assistant_generation.py 挂闭环 _run_ai_flavor_feedback()

本文件**只新增测试断言，不修改任何业务代码**。运行方式：

  backend/venv/Scripts/python.exe -m pytest tests/test_qa_attack_ai_flavor_loop.py -v \
      --basetemp=D:/Temp/gate_qa

攻击面（实现者声称已覆盖之外的角度）：
  A. 闭环污染：跨项目泄漏 / global 行误读 / Writer 直连路径 / 人工提案覆写被踩
  B. override 膨胀：20 轮增长曲线 / 去抖不更新洞 / MAX_EXAMPLES 与预算裁剪 / 提示词长度
  C. 检查者-生成者分离：正文逐字节不变 / 零额外 LLM / 闭环失败的中间态
  D. 检测器回归：长模式优先 / 占用区间 / occurrences offset / 阈值语义 / 旧算法等价
  E. 注入兼容：与 4227c73^ 逐字节比对 / 纯空白 override / 注入位置
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import ai_flavor_memory  # noqa: E402
from app.agents.prompt_overrides import (  # noqa: E402
    MAX_OVERRIDE_LEN,
    PromptKey,
    get_active_override,
    resolve_prompt_block,
    set_active_override,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation import engine as engine_mod  # noqa: E402
from app.generation import prompts as prompts_new  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AIFlavorMemory,
    AgentRun,
    Generation,
    PromptOverride,
    Project,
)
from app.routers import assistant_generation as gen_router  # noqa: E402
from app.routers import generation as writer_router  # noqa: E402

# ---------------------------------------------------------------------------
# 确定性测试数据
# ---------------------------------------------------------------------------

SEVERE_TEXT = "仿佛" * 9                      # total=9，过记忆线 8
SEVERE_TWO_PATTERN_TEXT = "仿佛" * 5 + "宛如" * 4   # total=9，双模式
CLEAN_TEXT = "林夜走在长街上。夜风微凉，街灯昏黄。他停步回望，身影被拉得很长。"

#: 一条「思考链 + 正文」的流式文本：_strip_thinking 会丢掉 <think>…</think>，
#: 但路由器闭环拿到的是**未 strip 的拼接原文**（用于探针 C-4）。
#: 注意 engine._strip_thinking 的字面量是带前导空格的 " <think>" / " </think>"。
THINK_TAGGED_STREAM = " <think>就在这时，显然。 </think>" + "仿佛" * 6


class StubProvider:
    """确定性假 Writer（与实现者测试同款，便于对齐口径）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "stub-writer"
        self.systems: list[str] = []
        self.users: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.systems.append(system)
        self.users.append(user)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        self.systems.append(system)
        self.users.append(user)
        yield self.text


class CountingWriter:
    """计数型假 Writer：记录每次 LLM 面方法调用（探针 C-2 用）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "counting-writer"
        self.counts = {"chat": 0, "stream_chat": 0, "summarize": 0}
        self.systems: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.counts["chat"] += 1
        self.systems.append(system)
        return self.text

    def summarize(self, text: str) -> str:
        self.counts["summarize"] += 1
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        self.counts["stream_chat"] += 1
        self.systems.append(system)
        yield self.text


@pytest.fixture(autouse=True)
def _clean_state():
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
# harness
# ---------------------------------------------------------------------------


def _make_project(db, title: str = "QA 攻击验证") -> Project:
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


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


def _install_writer(monkeypatch, provider) -> None:
    monkeypatch.setattr(llm_provider_mod, "_PROVIDER", provider)


def _invoke(db, **kwargs) -> dict:
    body = gen_router.AssistantInvokeGenerationRequest(**kwargs)
    return gen_router.assistant_invoke_generation(body=body, db=db)


def _memories(db) -> list:
    return list(db.scalars(select(AIFlavorMemory).order_by(AIFlavorMemory.id)).all())


def _active_constraint_override(db, project_id: int):
    return get_active_override(db, project_id, PromptKey.CONSTRAINT)


def _old_prompts_module(tmp_path):
    """从 git 取 4227c73^（改动前）的 prompts.py 并以独立模块加载。"""
    out = subprocess.run(
        ["git", "show", "4227c73^:backend/app/generation/prompts.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    )
    p = tmp_path / "prompts_pre_4227c73.py"
    p.write_text(out.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("prompts_pre_change", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# A. 闭环污染面
# ===========================================================================


def test_a1_cross_project_no_leak(db_session, monkeypatch):
    """项目 A 的闭环记忆/覆写，不得以任何形式进入项目 B 的提示词。"""
    proj_a = _make_project(db_session, title="项目A")
    proj_b = _make_project(db_session, title="项目B")

    # A 触发闭环（严重命中）
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    _invoke(db_session, project_id=proj_a.id, action="generate_next",
            instruction="A 写一段")

    rows_a = _memories(db_session)
    assert len(rows_a) == 1 and rows_a[0].project_id == proj_a.id
    ov_a = _active_constraint_override(db_session, proj_a.id)
    assert ov_a is not None and "仿佛" in ov_a.override_text

    # 数据层：B 的 load_memories 看不到 A 的项目行
    items_b = ai_flavor_memory.load_memories(db_session, proj_b.id)
    assert items_b == [], f"B 不应读到 A 的项目级记忆，实际 {len(items_b)} 条"
    assert ai_flavor_memory.render_memory_block(items_b) == ""
    assert gen_router._render_flavor_constraint(db_session, proj_b.id) == ""
    assert engine_mod._prompt_overrides(db_session, proj_b.id)[PromptKey.CONSTRAINT] == ""
    assert _active_constraint_override(db_session, proj_b.id) is None

    # 端点层：B 生成（干净文本），Writer 收到的 system prompt 不得含 A 的教训
    clean_writer = StubProvider(CLEAN_TEXT)
    _install_writer(monkeypatch, clean_writer)
    result_b = _invoke(db_session, project_id=proj_b.id, action="generate_next",
                       instruction="B 写一段")
    assert result_b["segment"]["text"] == CLEAN_TEXT
    assert clean_writer.systems, "B 的 Writer 应收到过 system prompt"
    leaked = [s for s in clean_writer.systems
              if gen_router._FLAVOR_OVERRIDE_HEADER in s or "仿佛" in s]
    assert leaked == [], "A 的记忆泄漏进了 B 的 Writer 系统提示词"
    assert _active_constraint_override(db_session, proj_b.id) is None

    # B 自己也严重命中（不同模式）：各写各的，互不串
    _install_writer(monkeypatch, StubProvider("宛如" * 9))
    _invoke(db_session, project_id=proj_b.id, action="generate_next",
            instruction="B 再写一段")
    assert "仿佛" not in _active_constraint_override(db_session, proj_b.id).override_text
    assert "宛如" not in _active_constraint_override(db_session, proj_a.id).override_text
    keys_b = {r.issue_key for r in _memories(db_session) if r.project_id == proj_b.id}
    keys_a = {r.issue_key for r in _memories(db_session) if r.project_id == proj_a.id}
    assert keys_a == {"仿佛"} and keys_b == {"宛如"}


def test_a2_global_row_enters_every_project_by_design(db_session, monkeypatch):
    """scope='global', project_id=NULL 的行会被合并进**每个**项目的 override。

    这是 load_memories 的既定设计（模块 docstring：「合并 global + 本项目」），
    本探针实测确认该行为存在且可控：闭环自身只写 project 行，global 行只能
    来自显式写入；同 key 时项目行胜出。
    """
    proj = _make_project(db_session)
    db_session.add(AIFlavorMemory(
        scope="global", project_id=None, issue_key="全局通用腔",
        summary="全局教训：任何项目都别写「综上所述」",
        hit_count=7, severity="high", active=True,
        examples=[{"segment_no": 0, "offset": 0, "snippet": "综上所述……"}],
    ))
    db_session.commit()

    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="写一段")

    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None
    assert "全局教训" in ov.override_text, "global 行按设计进入项目 override"
    assert "仿佛" in ov.override_text
    # 闭环只写 project 行，没有顺手写 global 行（库里唯一的 global 行是
    # 本探针手工插入的那条）
    global_rows = [r for r in _memories(db_session) if r.scope == "global"]
    assert len(global_rows) == 1
    assert global_rows[0].issue_key == "全局通用腔"

    # 同 key 时项目行胜出（不被 global 复读）
    proj2 = _make_project(db_session, title="同key项目")
    db_session.add(AIFlavorMemory(
        scope="global", project_id=None, issue_key="仿佛",
        summary="全局版仿佛教训", hit_count=99, severity="high", active=True,
        examples=[],
    ))
    db_session.commit()
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    _invoke(db_session, project_id=proj2.id, action="generate_next",
            instruction="再写一段")
    ov2 = _active_constraint_override(db_session, proj2.id)
    assert "全局版仿佛教训" not in ov2.override_text
    assert "本段命中 9 次" in ov2.override_text


def test_a3_writer_direct_path_never_triggers_loop(db_session, monkeypatch):
    """前端直连 POST /api/generate（不经 Assistant）：闭环不得触发。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    body = writer_router.GenerateBody(project_id=proj.id)
    result = writer_router.generate(body=body, db=db_session)

    assert result["segment_no"] == 1
    assert result["text"] == SEVERE_TEXT
    assert db_session.scalar(
        select(func.count()).select_from(AIFlavorMemory)) == 0, \
        "Writer 直连路径不得写 ai_flavor_memories"
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0, \
        "Writer 直连路径不得写 prompt_overrides"

    # generate-loop 同样（批量入口也不挂闭环）
    writer_router.generate_loop_endpoint(
        body=writer_router.GenerateLoopBody(project_id=proj.id, segments=1),
        db=db_session,
    )
    assert db_session.scalar(
        select(func.count()).select_from(AIFlavorMemory)) == 0
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0


def test_a4_loop_clobbers_human_proposal_override(db_session, monkeypatch):
    """【修复后防回归锁】人工提案批准的 constraint_note 覆写，闭环不得顶掉。

    原为 QA 缺陷探针（F1，中低危）：闭环无仲裁地写入 constraint_note，把
    人工/提案来源的 active 覆写降级为 inactive——等于自动微调静默覆盖了
    用户审批过的决策，违背 `prompt_overrides` 「覆写只能经 proposals apply
    落库」的治理口径。修复（3574ba0）：写入前看当前 active 行的 created_by，
    非本闭环标识即跳过写入（记忆仍正常记账，记账与微调互不影响）。

    本条保留原有的攻击性构造（人工覆写 + 严重命中），断言翻到修复后的
    正确行为：人工覆写原样生效、记忆照写、闭环报告未写覆写。
    """
    proj = _make_project(db_session)
    # 模拟 proposals.write_target(prompt_override) 的落库效果
    set_active_override(
        db_session, project_id=proj.id, prompt_key=PromptKey.CONSTRAINT,
        override_text="人工提案：只写甜宠线，本段禁止死人",
        created_by="assistant_proposal",
    )
    db_session.commit()

    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")

    # 人工覆写必须原样生效：不被降级、文本不被改
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None
    assert ov.override_text == "人工提案：只写甜宠线，本段禁止死人"
    assert ov.active is True
    assert "仿佛" not in ov.override_text
    # 不得产生任何 inactive 的人工行（没有被顶掉过）
    inactive_human = db_session.scalars(
        select(PromptOverride).where(
            PromptOverride.project_id == proj.id,
            PromptOverride.project_id == proj.id,
            PromptOverride.prompt_key == PromptKey.CONSTRAINT,
            PromptOverride.active.is_(False),
        )
    ).all()
    assert inactive_human == []
    # 记账不受影响：记忆仍写入（微调跳过 ≠ 记账跳过）
    rows = _memories(db_session)
    assert rows, "人工覆写生效时闭环仍应记账"
    assert any("仿佛" in r.issue_key for r in rows)
    # 闭环如实报告本次未写覆写（端点返回体里的闭环摘要）
    feedback = result.get("ai_flavor_feedback") or {}
    assert feedback.get("override_written") is False, (
        "人工覆写生效时闭环应报告未写覆写"
    )


# ===========================================================================
# B. override 膨胀面
# ===========================================================================


def test_b1_override_growth_curve_20_rounds(db_session, monkeypatch):
    """连续 20 轮闭环（每轮 8 个互异模式，32 模式池循环）测 constraint_note 增长。"""
    proj = _make_project(db_session)
    patterns = list(engine_mod.AI_FLAVOR_PATTERNS)
    writer = StubProvider("")
    _install_writer(monkeypatch, writer)

    curve: list[tuple[int, int, int, int]] = []  # (round, rows, ov_len, ov_rows)
    stale_rounds: list[int] = []                 # 新记忆写进去但 override 没动的轮次
    prev_rows, prev_ov = 0, None
    for r in range(20):
        chosen = [patterns[(r * 8 + j) % len(patterns)] for j in range(8)]
        writer.text = "。".join(p for p in chosen)
        _invoke(db_session, project_id=proj.id, action="generate_next",
                instruction=f"第 {r} 轮")
        rows = len(_memories(db_session))
        ov = _active_constraint_override(db_session, proj.id)
        ov_len = len(ov.override_text) if ov else 0
        ov_rows = db_session.scalar(
            select(func.count()).select_from(PromptOverride))
        curve.append((r, rows, ov_len, ov_rows))
        if rows > prev_rows and prev_ov is not None and ov_len == prev_ov:
            stale_rounds.append(r)
        prev_rows, prev_ov = rows, ov_len

    print("\n[增长曲线] (轮次, 记忆行数, override字符数, 覆写历史行数)")
    for c in curve:
        print("  ", c)

    assert prev_rows == len(patterns), f"32 个模式应各有一行，实际 {prev_rows}"
    final_ov = _active_constraint_override(db_session, proj.id)
    assert final_ov is not None
    assert len(final_ov.override_text) <= MAX_OVERRIDE_LEN
    # 渲染行数受 DEFAULT_LIMIT=12 硬闸
    body_lines = final_ov.override_text.split("\n")[1:]
    assert len([ln for ln in body_lines if ln.startswith("- 【")]) <= 12
    assert stale_rounds == [], f"存在新记忆写库但 override 不更新的轮次: {stale_rounds}"
    # 覆写历史表随触发线性增长（prompt_overrides「历史永不删除」的既定设计）
    assert curve[-1][3] == 20


def test_b1b_debounce_hole_new_lesson_never_enters_prompt(db_session, monkeypatch):
    """去抖 + 条数裁剪的联合后果：第 13 条起的低严重度新教训永远进不了提示词。"""
    proj = _make_project(db_session)
    patterns = list(engine_mod.AI_FLAVOR_PATTERNS)

    # 第 1 轮：12 个模式各×4 → 12 条 high 记忆（severity 排序永久占据前 12）
    first12 = patterns[:12]
    writer = StubProvider("。".join(p * 4 for p in first12))
    _install_writer(monkeypatch, writer)
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一轮")
    ov1 = _active_constraint_override(db_session, proj.id)
    assert ov1 is not None
    assert len(_memories(db_session)) == 12
    assert all(r.severity == "high" for r in _memories(db_session))

    # 第 2 轮：8 个**全新**模式各×1（low）→ 写入 8 条新记忆，但全部被 limit 裁掉
    new8 = patterns[12:20]
    writer.text = "。".join(new8)
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第二轮")
    rows = _memories(db_session)
    assert len(rows) == 20, f"DB 应累计 20 行，实际 {len(rows)}"
    ov2 = _active_constraint_override(db_session, proj.id)
    # 去抖命中：渲染文本与上一轮逐字节相同 → 没有写新覆写行
    assert ov2.override_text == ov1.override_text
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 1, \
        "内容相同则跳过写的去抖生效（历史表不堆行）"
    # 代价：8 条新教训在 DB 里存在，但不在任何生效提示词里
    for p in new8:
        assert p not in ov2.override_text, f"新模式 {p} 不在 override 里（预期被裁）"
        assert any(r.issue_key == p for r in rows), f"{p} 应在 DB 有行"


def test_b2_system_prompt_length_with_saturated_override(db_session, monkeypatch):
    """闭环写满（12 行）后，system prompt 的绝对长度实测（真实形态 + 合成最坏形态）。"""
    proj = _make_project(db_session)
    patterns = list(engine_mod.AI_FLAVOR_PATTERNS)

    # 形态一（闭环真实产出）：12 个模式各×4（high），短总结 + 普通长度例句
    writer = StubProvider("。".join(p * 4 for p in patterns[:12]))
    _install_writer(monkeypatch, writer)
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="写满它")
    ov = _active_constraint_override(db_session, proj.id).override_text

    # 形态二（合成最坏）：12 条记忆全部带 200 字段例（MAX_SNIPPET_LEN 上限）。
    # last_segment_no 必须大于闭环行的 1：排序键是 严重度→hit_count→段号→
    # updated_at，不设段号（None→0）会排到闭环行之后、被 limit 裁掉。
    for i, p in enumerate(patterns[:12]):
        db_session.add(AIFlavorMemory(
            scope="project", project_id=proj.id, issue_key=f"最坏{i:02d}",
            summary=f"总结{i}", hit_count=1, severity="high", active=True,
            last_segment_no=2,
            examples=[{"segment_no": 2, "offset": 0, "snippet": "例" * 200}],
        ))
    db_session.commit()
    worst_items = ai_flavor_memory.load_memories(db_session, proj.id)
    worst_block = ai_flavor_memory.render_memory_block(worst_items)

    base_sys = prompts_new.build_system_prompt("标题", {})
    full_sys = prompts_new.build_system_prompt(
        "标题", {}, constraint_note=ov)
    worst_sys = prompts_new.build_system_prompt(
        "标题", {}, constraint_note=worst_block)
    base_user = prompts_new.build_user_prompt([], [], None, 3000)
    full_user = prompts_new.build_user_prompt(
        [], [], None, 3000, directive_suffix=worst_block)
    print(f"\n[提示词长度] 真实 override={len(ov)}；合成最坏 block={len(worst_block)}；"
          f"system 基线={len(base_sys)} → 真实={len(full_sys)} → 最坏={len(worst_sys)}；"
          f"user 基线={len(base_user)} → 最坏={len(full_user)}")

    assert len(ov) <= MAX_OVERRIDE_LEN
    assert len(worst_block) <= MAX_OVERRIDE_LEN
    # 有界性：覆写贡献不超过 MAX_OVERRIDE_LEN + 块信封
    assert len(full_sys) <= len(base_sys) + MAX_OVERRIDE_LEN + 64
    assert len(worst_sys) <= len(base_sys) + MAX_OVERRIDE_LEN + 64
    # 读取端字符预算真的在管事（首条必收 + 2000 预算）
    first_line_len = len(ai_flavor_memory._render_line(worst_items[0]))
    assert len(worst_block) <= ai_flavor_memory.DEFAULT_CHAR_BUDGET + first_line_len
    # user 侧增量 == "\n\n" + "【header】\n" + 覆写本体（信封共 19 字符）
    assert len(full_user) - len(base_user) == len(worst_block) + len(
        prompts_new._OVERRIDE_DIRECTIVE_HEADER) + 5


def test_b3_max_examples_and_budget_trimming(db_session):
    """写入端 MAX_EXAMPLES=3 / MAX_SNIPPET_LEN=200 与读取端双重裁剪实测。"""
    # 新行 5 条例句 → 只留最后 3 条
    row = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=1, issue_key="仿佛",
        examples=[{"segment_no": 1, "offset": i, "snippet": f"例{i}"}
                  for i in range(5)],
    )
    assert len(row.examples) == ai_flavor_memory.MAX_EXAMPLES == 3
    assert [e["snippet"] for e in row.examples] == ["例2", "例3", "例4"]

    # 同 (segment_no, offset) 在一次调用里去重
    row2 = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=1, issue_key="宛如",
        examples=[{"segment_no": 2, "offset": 9, "snippet": "甲"},
                  {"segment_no": 2, "offset": 9, "snippet": "乙"}],
    )
    assert len(row2.examples) == 1 and row2.examples[0]["snippet"] == "乙"

    # 单条 snippet 超 200 字符被截断
    row3 = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=1, issue_key="犹如",
        examples=[{"segment_no": 3, "offset": 0, "snippet": "x" * 500}],
    )
    assert len(row3.examples[0]["snippet"]) == ai_flavor_memory.MAX_SNIPPET_LEN == 200

    # 已有 3 条 + 2 条新证据 → 仍 3 条，新的挤掉最旧的
    ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=1, issue_key="仿佛",
        examples=[{"segment_no": 9, "offset": 0, "snippet": "新甲"},
                  {"segment_no": 9, "offset": 5, "snippet": "新乙"}],
    )
    assert [e["snippet"] for e in row.examples] == ["例4", "新甲", "新乙"]

    # 读取端：20 行、每条 200 字段例 → 条数/字符双裁剪
    for i in range(20):
        db_session.add(AIFlavorMemory(
            scope="project", project_id=2, issue_key=f"模式{i:02d}",
            summary=f"总结{i}", hit_count=1, severity="low", active=True,
            examples=[{"segment_no": 1, "offset": 0, "snippet": "y" * 200}],
        ))
    db_session.commit()
    items = ai_flavor_memory.load_memories(db_session, 2)
    assert len(items) <= ai_flavor_memory.DEFAULT_LIMIT == 12
    block = ai_flavor_memory.render_memory_block(items)
    # 首条必收 + 预算 2000：总量不超过「预算 + 首条长度」
    first_line_len = len(ai_flavor_memory._render_line(items[0]))
    assert len(block) <= ai_flavor_memory.DEFAULT_CHAR_BUDGET + first_line_len
    # 显式小 limit 也生效
    assert len(ai_flavor_memory.load_memories(db_session, 2, limit=3)) == 3


def test_b4_oversized_summary_row_silently_disables_loop_override(db_session, monkeypatch):
    """【修复后防回归锁】超长 summary 脏行不得让闭环微调永久静默罢工。

    原为 QA 缺陷探针（F2，低危）：`upsert_memory` 不校验 summary 长度，一条
    5000 字的脏行排到首条后，渲染出的覆写超过 `set_active_override` 的
    MAX_OVERRIDE_LEN 闸 → 抛错 → 闭环整体回滚 → 该项目自动微调从此悄无声息
    地罢工（只有日志，用户完全看不出微调已失效）。
    修复（3574ba0）两层防护：`upsert_memory` 入口按 MAX_SUMMARY_LEN 归一
    （治本，管未来写入）+ `_render_flavor_constraint` 按 MAX_OVERRIDE_LEN
    截断（治标，兜底历史脏行）。本条断言修复后的正确行为：主流程不受影响、
    覆写仍能成功写入且不超闸、脏行被截断而非让闭环炸掉。
    """
    proj = _make_project(db_session)
    # hit_count=99 + severity=high：排序键上这条脏行恒排首条 → 命中
    # load_memories「首条必收」规则 → 渲染文本必然超过 4000 字闸。
    db_session.add(AIFlavorMemory(
        scope="project", project_id=proj.id, issue_key="霸腔",
        summary="霸" * 5000, hit_count=99, severity="high", active=True,
        last_segment_no=99,
        examples=[{"segment_no": 1, "offset": 0, "snippet": "s" * 200}],
    ))
    db_session.commit()

    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")

    # 主流程不受影响
    assert result["segment"]["text"] == SEVERE_TEXT
    # 修复后：闭环微调不再罢工——覆写成功写入且不超闸
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None, "脏行不应让闭环微调失败（防静默罢工）"
    assert len(ov.override_text) <= MAX_OVERRIDE_LEN
    assert "仿佛" in ov.override_text, "本次的教训必须进得了提示词"
    # 闭环自己的记忆行不再被一起回滚（半截状态不残留的语义保留：
    # 成功路径下自己的行要落库）
    rows = _memories(db_session)
    assert any(r.issue_key == "仿佛" for r in rows), "闭环自身记账应成功"
    # 历史脏行仍在（不删历史），但其 summary 被源头截断规则管住未来写入
    dirty = [r for r in rows if r.issue_key == "霸腔"]
    assert len(dirty) == 1 and dirty[0].hit_count == 99


def test_b5_severity_downgrade_on_later_mild_hit(db_session, monkeypatch):
    """严重度只反映**最近一段**：high 行被后续轻微命中降级为 low 并改写提示词。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一段")
    row = _memories(db_session)[0]
    assert row.severity == "high" and "9 次" in row.summary

    # 第二段：仿佛只出现 1 次（外加 7 个其它模式凑够记忆线 8）
    others = [p for p in engine_mod.AI_FLAVOR_PATTERNS if p != "仿佛"][:7]
    text = "仿佛。" + "。".join(others)
    assert engine_mod._detect_ai_flavor(text)["total"] == 8
    monkeypatch.setattr(llm_provider_mod, "_PROVIDER", StubProvider(text))
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第二段")
    db_session.refresh(row)
    assert row.severity == "low", "严重度被最近一段的密度覆盖"
    assert "本段命中 1 次" in row.summary
    ov = _active_constraint_override(db_session, proj.id)
    assert "【low】" in ov.override_text and "【high】" not in ov.override_text


# ===========================================================================
# C. 检查者-生成者分离底线
# ===========================================================================


def test_c1_stored_text_byte_identical(db_session, monkeypatch):
    """闭环只读不改：落库正文与 Writer 产出逐字节相同（两个分支都验）。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TWO_PATTERN_TEXT))
    r1 = _invoke(db_session, project_id=proj.id, action="generate_next",
                 instruction="续写")
    assert r1["segment"]["text"] == SEVERE_TWO_PATTERN_TEXT
    gen1 = db_session.get(Generation, r1["segment"]["id"])
    assert gen1.text == SEVERE_TWO_PATTERN_TEXT
    assert gen1.text.encode("utf-8") == SEVERE_TWO_PATTERN_TEXT.encode("utf-8")

    _seed_segment(db_session, proj.id, 2, "旧正文。" * 30)
    _install_writer(monkeypatch, StubProvider(SEVERE_TWO_PATTERN_TEXT))
    r2 = _invoke(db_session, project_id=proj.id,
                 action="regenerate_segment", segment_no=2,
                 instruction="重写")
    assert r2["segment"]["text"] == SEVERE_TWO_PATTERN_TEXT
    gen2 = db_session.get(Generation, r2["segment"]["id"])
    assert gen2.text == SEVERE_TWO_PATTERN_TEXT
    # 闭环跑过之后，正文依然没被碰
    assert db_session.get(Generation, r1["segment"]["id"]).text == \
        SEVERE_TWO_PATTERN_TEXT


def test_c2_zero_extra_llm_calls_vs_loop_disabled(db_session, monkeypatch):
    """闭环零 LLM：开/关闭环的两次端到端调用，provider 面方法计数完全相同。"""
    proj = _make_project(db_session)

    w1 = CountingWriter(SEVERE_TEXT)
    _install_writer(monkeypatch, w1)
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="带闭环跑一次")
    counts_with = dict(w1.counts)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    proj = _make_project(db_session)

    w2 = CountingWriter(SEVERE_TEXT)
    _install_writer(monkeypatch, w2)
    monkeypatch.setattr(gen_router, "_run_ai_flavor_feedback",
                        lambda *a, **k: None)
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="关闭环跑一次")
    counts_without = dict(w2.counts)

    print(f"\n[LLM 计数] 开闭环={counts_with} 关闭环={counts_without}")
    assert counts_with == counts_without, "闭环不得产生任何额外 LLM 调用"
    assert counts_with["chat"] >= 2, "Writer 确实被用了（chat + 引擎级重写）"


@pytest.mark.parametrize("patch_target", [
    "ai_flavor_memory.upsert_memory",   # 写记忆第二步炸锅
    "ai_flavor_memory.load_memories",   # 读回渲染时炸锅（记忆已 flush）
    "set_active_override",              # 微调写入时炸锅
])
def test_c3_loop_failure_leaves_no_half_state(db_session, monkeypatch, patch_target):
    """闭环任一步炸锅：端点成功、正文落库、无「半条记忆 + 一条 override」。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TWO_PATTERN_TEXT))

    if patch_target == "set_active_override":
        def _boom(db, **kwargs):
            raise RuntimeError("覆写写入炸锅（模拟）")
        monkeypatch.setattr(gen_router, "set_active_override", _boom)
    else:
        mod_name, fn_name = patch_target.split(".")
        mod = ai_flavor_memory if mod_name == "ai_flavor_memory" else None
        real = getattr(mod, fn_name)

        def _boom(*a, **k):
            raise RuntimeError(f"{patch_target} 炸锅（模拟）")
        monkeypatch.setattr(mod, fn_name, _boom)
        assert real is not None

    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="会让闭环炸锅的一段")

    # 主流程的成功不被吞
    assert result["segment"]["text"] == SEVERE_TWO_PATTERN_TEXT
    assert result["answer"].startswith("已按你的指令续写第 1 段")
    run = db_session.get(AgentRun, result["agent_run_id"])
    assert run is not None and run.status == "success"
    assert db_session.scalar(
        select(func.count()).select_from(Generation)) == 1
    # 无中间态残留
    assert _memories(db_session) == [], "半截记忆行必须被回滚"
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 0, \
        "不得留下半条 override"


def test_c4_regenerate_loop_checks_unstripped_stream_text(db_session, monkeypatch):
    """【修复后防回归锁】regen 分支闭环必须检查 strip 后已落库的正文。

    原为 QA 缺陷探针（F3，低危）：regen 分支传给闭环的是 `"".join(pieces)`
    （流式拼接的**未 strip** 原文），而引擎落库的是 `_strip_thinking(...)`
    之后的正文。provider 若吐出思维链，闭环会把思维链里的 AI 腔模式当成
    正文问题写进记忆、进而进提示词——「幻记」。
    修复（3574ba0）：regen 分支改传已落库的 `gen.text`（strip 后正文）。

    ⚠️ 数据构造要点：正文自身必须**超过记忆线**（`_AI_FLAVOR_MEMORY_MIN_TOTAL=8`），
    否则 strip 后 total 掉到线下、闭环按设计不记账，本条就退化成「什么都没验」。
    原探针的正文只有「仿佛」×6，靠思维链里的「就在这时」「显然」才凑到 8——
    那正好是缺陷的成因（幻记把 total 顶过了线）；修复后正文必须自己够重，
    才能区分「没记账是因为掉线」与「没记账是因为 bug」。
    """
    proj = _make_project(db_session)
    _seed_segment(db_session, proj.id, 1, "旧正文。" * 30)
    # 正文 9 次「仿佛」：自身已超记忆线，不依赖思维链凑数
    _install_writer(monkeypatch, StubProvider(
        " 就在这时，显然。 </think>" + "仿佛" * 9
    ))

    result = _invoke(db_session, project_id=proj.id,
                     action="regenerate_segment", segment_no=1,
                     instruction="重写第一段")

    stored = result["segment"]["text"]
    assert stored == "仿佛" * 9, "落库正文应是 strip 之后的纯正文"
    assert "就在这时" not in stored and "显然" not in stored

    rows = _memories(db_session)
    keys = {r.issue_key for r in rows}
    # 修复后：正文自身够重 → 正常记账；思维链里的模式绝不入账
    assert "就在这时" not in keys and "显然" not in keys, (
        f"闭环把思维链里的模式当成了正文问题记账：{keys}")
    assert keys == {"仿佛"}, f"只应记正文里的模式，实际 {keys}"


# ===========================================================================
# D. 检测器回归（4227c73）
# ===========================================================================


def test_d1_long_pattern_first_no_double_count():
    """「毫无疑问/毫无疑问地」同处不再双计：长模式优先占位。"""
    d = engine_mod._detect_ai_flavor("毫无疑问地")
    assert d["hits"].get("毫无疑问地") == 1
    assert d["hits"].get("毫无疑问", 0) == 0
    assert d["total"] == 1

    d2 = engine_mod._detect_ai_flavor("毫无疑问。")
    assert d2["hits"].get("毫无疑问") == 1
    assert "毫无疑问地" not in d2["hits"]
    assert d2["total"] == 1

    mixed = "毫无疑问，毋庸置疑。毫无疑问地"
    d3 = engine_mod._detect_ai_flavor(mixed)
    assert d3["hits"] == {"毫无疑问": 1, "毋庸置疑": 1, "毫无疑问地": 1}
    assert d3["total"] == 3
    # 旧算法（逐模式 count 求和）在同一文本上会虚高到 4
    old_total = sum(mixed.count(p) for p in engine_mod.AI_FLAVOR_PATTERNS)
    assert old_total == 4 and d3["total"] == 3, "修 bug 后 total 不再虚高"


def test_d2_occupancy_counts_adjacent_and_repeated():
    """占用区间法：相邻/重复的同模式命中全部计到（与 str.count 同语义）。"""
    far = "前情铺垫。" + "仿佛" + "中场过渡，情节推进了一些。" + "仿佛" + "收尾。"
    d = engine_mod._detect_ai_flavor(far)
    assert d["hits"]["仿佛"] == 2 == far.count("仿佛")

    adjacent = "仿佛仿佛"
    assert engine_mod._detect_ai_flavor(adjacent)["total"] == 2
    triple = "仿佛仿佛仿佛"
    assert engine_mod._detect_ai_flavor(triple)["total"] == 3 == triple.count("仿佛")

    # 模式库中唯一可重叠的一对就是 无疑/无疑地（D-1 已覆盖）；
    # 扫描全库确认无其它子串/交叠关系，故不存在其它漏计面
    pats = list(engine_mod.AI_FLAVOR_PATTERNS)
    overlapping = {
        frozenset({a, b}) for a in pats for b in pats
        if a != b and (a in b or b in a)
    }
    assert overlapping == {frozenset({"毫无疑问", "毫无疑问地"})}


def test_d3_occurrences_offset_cross_check():
    """occurrences 的 offset 与 text.find 交叉验证，snippet 含原模式。"""
    text = ("开篇就在这时。他不禁深吸一口气。中场：仿佛如梦。"
            "尾声显然如此。结尾原来如此。")
    d = engine_mod._detect_ai_flavor(text)
    assert d["occurrences"], "应有 occurrences"
    for pattern, occs in d["occurrences"].items():
        prev = -1
        for occ in occs:
            off = occ["offset"]
            assert text[off:off + len(pattern)] == pattern, (
                f"offset {off} 处不是 {pattern}")
            nxt = text.find(pattern, prev + 1)
            assert off == nxt, f"offset 应与顺序 find 一致：{off} vs {nxt}"
            assert pattern in occ["snippet"]
            prev = off


def test_d4_should_rewrite_threshold_semantics():
    """_should_rewrite 阈值语义不变：4 次不告警 / 5 次告警。"""
    assert engine_mod._should_rewrite("仿佛" * 4, None) == (False, [])
    ok, reasons = engine_mod._should_rewrite("仿佛" * 5, None)
    assert ok and reasons and "5 次" in reasons[0]
    # 去重修复后：5 个「毫无疑问地」按真实命中计 5 → 告警（旧代码双计成 10 也告警，
    # 但 4 个时旧代码 total=8 会误告警，现在正确地不告警）
    assert engine_mod._should_rewrite("毫无疑问地" * 4, None) == (False, [])
    ok5, _ = engine_mod._should_rewrite("毫无疑问地" * 5, None)
    assert ok5


def test_d5_hits_total_backward_compatible_without_overlap_pair():
    """不含 无疑 对的文本上，新 hits/total 与旧逐模式 count 算法完全一致。"""
    text = ("就在这时，他不禁深吸一口气。心中充满了疑惑，眼中闪过一丝光芒。"
            "嘴角勾起一抹笑意。她知道，他终于忍不住开口。"
            "不仅如此，更是由此可见。总的来说，原来如此。"
            "她明白，这意味着结束。从此，宛如隔世。犹如梦中。"
            "像是似乎显然毋庸置疑。值得一提的是，不难发现。"
            "突然之间，仿佛还在昨天。")
    new = engine_mod._detect_ai_flavor(text)
    old_hits = {p: text.count(p) for p in engine_mod.AI_FLAVOR_PATTERNS
                if text.count(p)}
    old_total = sum(old_hits.values())
    assert new["hits"] == old_hits, f"{new['hits']} != {old_hits}"
    assert new["total"] == old_total


# ===========================================================================
# E. override 注入兼容性
# ===========================================================================


_REMINDERS_CASES = [
    {},
    {"character_constraints": [{"name": "林夜", "role": "主角"}],
     "unresolved_plotholes": [{"question": "祠堂里的刀是谁的"}]},
]
_BIBLE_CASES = [
    None,
    {"characters": [{"name": "林夜", "role": "主角", "motivation": "复仇"}],
     "foreshadowing": ["祠堂的刀"], "synopsis": "少年复仇记",
     "direction": "南下", "conflicts": ["杀父之仇"],
     "relationships": [{"a": "林夜", "b": "苏7", "relation": "青梅竹马"}]},
]
_DIMS_CASES = [None, {"类型": "悬疑", "风格平台": "番茄快节奏"}]


def test_e1_byte_identical_without_override_vs_pre_change(tmp_path):
    """未设置 override 时，与 4227c73^ 的 prompts 输出逐字节相同（system+user）。"""
    old = _old_prompts_module(tmp_path)
    cases = 0
    for reminders in _REMINDERS_CASES:
        for bible in _BIBLE_CASES:
            for dims in _DIMS_CASES:
                old_sys = old.build_system_prompt(
                    "测试项目", reminders, dims, bible, None, None)
                new_sys = prompts_new.build_system_prompt(
                    "测试项目", reminders, dims, bible, None, None,
                    style_suffix=None, constraint_note=None)
                assert new_sys == old_sys, "无覆写时 system prompt 必须逐字节相同"
                new_sys_empty = prompts_new.build_system_prompt(
                    "测试项目", reminders, dims, bible, None, None,
                    style_suffix="", constraint_note="")
                assert new_sys_empty == old_sys, "空串覆写等价于未设置"
                cases += 1

    user_cases = [
        dict(prior_summaries=["第一段摘要", "第二段摘要"]),
        dict(prior_summaries=[], prior_blocks=["[主线] 骨架", "[近情·段 2] 近况"]),
        dict(prior_summaries=[]),
        dict(prior_summaries=["s"], premise="一个前提",
             rag_context=["素材A", "素材B"], rag_has_forbidden=True,
             style_exemplars=[{"novel_title": "《语料》", "text": "样例原文"}],
             outline=["大纲一", "大纲二"]),
        dict(prior_summaries=["s"], outline="纯文本大纲", plot_bible=_BIBLE_CASES[1]),
    ]
    for kw in user_cases:
        args = dict(
            prior_summaries=kw.get("prior_summaries", []),
            rag_context=kw.get("rag_context", []),
            outline=kw.get("outline"),
            max_chars=3000,
            plot_bible=kw.get("plot_bible"),
            premise=kw.get("premise"),
            prior_blocks=kw.get("prior_blocks"),
            style_exemplars=kw.get("style_exemplars"),
            rag_has_forbidden=kw.get("rag_has_forbidden", False),
        )
        old_user = old.build_user_prompt(**args)
        new_user = prompts_new.build_user_prompt(**args, directive_suffix=None)
        assert new_user == old_user, "无覆写时 user prompt 必须逐字节相同"
        assert prompts_new.build_user_prompt(**args, directive_suffix="") == old_user
        cases += 1
    print(f"\n[逐字节比对] system 3×2×3 + user {len(user_cases)} 共 {cases} 组全部相同")


def test_e2_whitespace_override_treated_as_unset():
    """纯空白 override 被当作未设置（不拼出只有标题的空块）。"""
    for ws in ("", "   ", "  \n\t ", None):
        sys_ws = prompts_new.build_system_prompt(
            "标题", {}, constraint_note=ws, style_suffix=ws)
        sys_base = prompts_new.build_system_prompt("标题", {})
        assert sys_ws == sys_base, f"纯空白 {ws!r} 必须等价未设置"
        user_ws = prompts_new.build_user_prompt(
            [], [], None, 3000, directive_suffix=ws)
        assert user_ws == prompts_new.build_user_prompt([], [], None, 3000)
    # 非空白：strip 后注入，块信封固定
    sys_ov = prompts_new.build_system_prompt(
        "标题", {}, constraint_note="  别写套话  ")
    assert f"【{prompts_new._OVERRIDE_CONSTRAINT_HEADER}】\n别写套话\n" in sys_ov
    assert "  别写套话  " not in sys_ov


def test_e3_override_injection_positions_match_slot_semantics():
    """注入位置与 prompt_overrides.py:40-66 的槽位语义一致。"""
    sys_p = prompts_new.build_system_prompt(
        "标题", {}, style_dims={"类型": "悬疑"},
        style_suffix="调性补充", constraint_note="纪律补充")
    i_style_block = sys_p.index("【风格】")
    i_style_ov = sys_p.index(prompts_new._OVERRIDE_STYLE_HEADER)
    i_wind = sys_p.index("【文风要求")
    i_disc = sys_p.index("【续写纪律】")
    i_const_ov = sys_p.index(prompts_new._OVERRIDE_CONSTRAINT_HEADER)
    assert i_style_block < i_style_ov < i_wind, "style_suffix 须在【风格】区之后"
    assert i_disc < i_const_ov, "constraint_note 须在【续写纪律】之后"
    assert sys_p.rstrip().endswith("纪律补充"), "constraint_note 落在 system 末尾"

    user_p = prompts_new.build_user_prompt(
        ["前情"], [], None, 3000, directive_suffix="指令补充")
    assert user_p.index("【续写指令】") < user_p.index(
        prompts_new._OVERRIDE_DIRECTIVE_HEADER), \
        "directive_suffix 须在【续写指令】之后"


def test_e4_whitespace_row_in_db_is_neutralized_at_render(db_session):
    """库里误存空白覆写行：渲染层 strip 兜底，最终 prompt 不受污染。"""
    proj = _make_project(db_session)
    db_session.add(PromptOverride(
        project_id=proj.id, prompt_key=PromptKey.CONSTRAINT,
        override_text="  \n\t ", active=True,
    ))
    db_session.commit()

    text, source = resolve_prompt_block(db_session, proj.id, PromptKey.CONSTRAINT)
    assert text == "  \n\t " and source == "runtime_override"  # 读取层原样返回
    # 但渲染层 strip → 提示词与未设置逐字节相同
    sys_dirty = prompts_new.build_system_prompt(
        "标题", {}, constraint_note=engine_mod._prompt_overrides(
            db_session, proj.id)[PromptKey.CONSTRAINT])
    assert sys_dirty == prompts_new.build_system_prompt("标题", {})
    # 闭环的「内容相同则跳过写」会把它替换掉，不会卡死
    report = {r["prompt_key"]: r for r in
              __import__("app.agents.prompt_overrides", fromlist=["x"])
              .active_prompt_report(db_session, proj.id)}
    assert report[PromptKey.CONSTRAINT]["source"] == "runtime_override"
