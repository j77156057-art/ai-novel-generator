"""AI 腔反馈闭环 QA 三缺陷的回归测试（F3 / F1 / F2，钉死修复）。

被修对象（branch feat/resume-push-2026-10，QA 攻击式验证挖出的三个真实缺陷）：

- **F3**（`assistant_generation.py` regen 分支）：闭环曾收到**未 strip** 的
  流式拼接原文，provider 思维链里的 AI 腔模式被当成正文问题写进记忆、
  再进提示词——幻记。修法：regen 分支改传已落库的 `gen.text`（引擎落库前
  做过 `_strip_thinking`，与落库同源）；
- **F1**（`_run_ai_flavor_feedback` 微调步）：闭环曾无条件写
  constraint_note 覆写，把人工/提案审批过的覆写顶成 inactive——自动微调
  静默覆盖人的决策。修法：写覆写前看当前 active 行的 `created_by`，不是
  本闭环标识（`assistant_ai_flavor_feedback`）就跳过写入（记忆照写）；
- **F2**（`ai_flavor_memory.upsert_memory` + `_render_flavor_constraint`）：
  一条超长 summary 的记忆行会渲染出超限覆写 → `set_active_override` 抛错 →
  闭环整体回滚 → 该项目自动微调**永久静默罢工**。修法：写入端 summary
  截断到 MAX_SUMMARY_LEN（治本）+ 渲染端按 MAX_OVERRIDE_LEN 截断（治标，
  兜底既有脏数据）。

与 QA 探针的关系：`tests/test_qa_attack_ai_flavor_loop.py` 的 a4 / b4 / c4
三个探针钉的是**修复前**的缺陷行为，本文件钉的是**修复后**的正确行为；
两者互为反面教材（QA 文件未提交、不在本文件改动范围）。
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
from app.agents.prompt_overrides import (  # noqa: E402
    MAX_OVERRIDE_LEN,
    PromptKey,
    get_active_override,
    set_active_override,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation import engine as engine_mod  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AIFlavorMemory,
    Generation,
    PromptOverride,
    Project,
)
from app.routers import assistant_generation as gen_router  # noqa: E402


# ---------------------------------------------------------------------------
# 确定性测试数据
# ---------------------------------------------------------------------------

#: 严重命中正文：同一模式 9 次 → strip 后 total=9（>= 记忆线 8）。
SEVERE_TEXT = "仿佛" * 9

#: 思维链片段（engine._strip_thinking 的字面量是带前导空格的 " <think>" / " </think>"）：
#: 落入思维链的「就在这时」「显然」都是 AI 腔模式，但**不是正文**。
THINK_CHAIN = " <think>就在这时，显然。 </think>"

#: 人工/提案来源覆写的文本（F1 场景）。
HUMAN_OVERRIDE_TEXT = "人工提案：只写甜宠线，本段禁止死人"

#: 超长脏 summary（F2 场景，QA 实测同款）。
DIRTY_SUMMARY = "霸" * 5000


class StubProvider:
    """确定性假 Writer：chat/stream_chat 恒返回同一文本（与既有测试同款）。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "stub-writer"
        self.systems: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.systems.append(system)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        self.systems.append(system)
        yield self.text


class ThinkingStreamStubProvider:
    """流式吐出「思维链 + 正文」两段 pieces 的假 Writer（复现 QA C-4 场景）。

    与 StubProvider 的唯一差别：stream_chat 分多段 yield，让引擎侧
    `"".join(pieces)` 得到含思维链的原文、而落库的 `gen.text` 是 strip 后的。
    """

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.model = "stub-writer"
        self.systems: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.systems.append(system)
        return "".join(self.pieces)

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        self.systems.append(system)
        yield from self.pieces


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
# harness（与既有闭环测试同款）
# ---------------------------------------------------------------------------


def _make_project(db, title: str = "QA 修复回归测试") -> Project:
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
    return list(
        db.scalars(select(AIFlavorMemory).order_by(AIFlavorMemory.id)).all()
    )


def _active_constraint_override(db, project_id: int) -> PromptOverride | None:
    return get_active_override(db, project_id, PromptKey.CONSTRAINT)


# ===========================================================================
# F3：regen 分支闭环检查的是 strip 后已落库的正文（不是流式拼接原文）
# ===========================================================================


def test_f3_regen_loop_checks_stripped_stored_text(db_session, monkeypatch):
    """思维链 pieces + 正文 9 命中：闭环只认正文模式，不幻记思维链。

    先钉检测器口径：同一文本未 strip 时 total=11（思维链里的「就在这时」
    「显然」也被计入），strip 后 total=9。闭环若还像修复前那样收未 strip
    的拼接原文，就会把 11 个命中全部记账。
    """
    raw_stream = THINK_CHAIN + SEVERE_TEXT
    assert engine_mod._detect_ai_flavor(raw_stream)["total"] == 11
    assert engine_mod._detect_ai_flavor(SEVERE_TEXT)["total"] == 9

    proj = _make_project(db_session)
    _seed_segment(db_session, proj.id, 1, "这是第一段的旧正文。" * 20)
    _install_writer(monkeypatch, ThinkingStreamStubProvider([THINK_CHAIN, SEVERE_TEXT]))

    result = _invoke(db_session, project_id=proj.id,
                     action="regenerate_segment", segment_no=1,
                     instruction="重写第一段，加强氛围")

    # 落库正文是 strip 之后的纯正文（引擎行为，闭环修复的前提）
    assert result["segment"]["text"] == SEVERE_TEXT
    assert "就在这时" not in result["segment"]["text"]
    assert "显然" not in result["segment"]["text"]

    # 闭环只写正文里的模式：一行「仿佛」；思维链里的两个模式绝不入账
    rows = _memories(db_session)
    keys = {r.issue_key for r in rows}
    assert keys == {"仿佛"}, f"闭环只该记正文模式，实际记了：{keys}"
    assert "就在这时" not in keys and "显然" not in keys
    assert rows[0].hit_count == 1
    assert rows[0].last_segment_no == 1

    # 覆写文本同样只有正文模式（不会把思维链问题注入下次生成的提示词）
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None and "仿佛" in ov.override_text
    assert "就在这时" not in ov.override_text
    assert "显然" not in ov.override_text


def test_f3_unstripped_input_would_pollute_memory_repro(db_session):
    """反面钉死：若把未 strip 的原文喂给闭环，思维链模式确实会入账。

    直接驱动闭环函数（绕开端点），证明上面的用例「没记思维链」不是因为
    场景本身不触发，而是因为 regen 分支现在传的是 gen.text。
    """
    proj = _make_project(db_session)
    raw_stream = THINK_CHAIN + SEVERE_TEXT

    out = gen_router._run_ai_flavor_feedback(
        db_session, proj, raw_stream, 1, "重写第一段"
    )

    assert out is not None and out["total"] == 11
    keys = {r.issue_key for r in _memories(db_session)}
    assert "就在这时" in keys and "显然" in keys, (
        f"未 strip 原文应把思维链模式也记账（缺陷复现），实际：{keys}")
    assert "仿佛" in keys


# ===========================================================================
# F1：闭环不顶掉人工/提案来源的 constraint_note 覆写（记忆照写）
# ===========================================================================


@pytest.mark.parametrize("human_created_by", [None, "assistant_proposal"])
def test_f1_human_override_survives_loop(db_session, monkeypatch, human_created_by):
    """人工来源覆写（created_by=None 的提案 apply 实路 / 提案标识）不被顶掉。

    提案 apply 路径（proposals.write_target 的 prompt_override 分支）调
    set_active_override 时不传 created_by → 落库为 NULL；两种取值都代表
    「用户审批过的决策」，闭环都必须让路。
    """
    proj = _make_project(db_session)
    set_active_override(
        db_session, project_id=proj.id, prompt_key=PromptKey.CONSTRAINT,
        override_text=HUMAN_OVERRIDE_TEXT, created_by=human_created_by,
    )
    db_session.commit()
    human_row = _active_constraint_override(db_session, proj.id)
    assert human_row is not None

    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")

    # 主流程照常成功
    assert result["segment"]["text"] == SEVERE_TEXT

    # 人工覆写原封不动：仍 active、文本未改、历史表没有新增行
    db_session.refresh(human_row)
    assert human_row.active is True
    assert human_row.override_text == HUMAN_OVERRIDE_TEXT
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 1

    # 记账不受影响：闭环自己的记忆行照常写入
    rows = _memories(db_session)
    assert len(rows) == 1
    assert rows[0].issue_key == "仿佛" and rows[0].hit_count == 1


def test_f1_loop_skip_reflected_in_return_flag(db_session):
    """闭环直接驱动时：返回 override_written=False，明确记录「跳过了微调」。"""
    proj = _make_project(db_session)
    set_active_override(
        db_session, project_id=proj.id, prompt_key=PromptKey.CONSTRAINT,
        override_text=HUMAN_OVERRIDE_TEXT, created_by="assistant_proposal",
    )
    db_session.commit()

    out = gen_router._run_ai_flavor_feedback(
        db_session, proj, SEVERE_TEXT, 1, "写一段"
    )

    assert out is not None
    assert out["total"] == 9
    assert out["override_written"] is False, "人工覆写在场时必须跳过微调写入"
    # 人工覆写未动，记忆已写
    assert _active_constraint_override(
        db_session, proj.id).override_text == HUMAN_OVERRIDE_TEXT
    assert len(_memories(db_session)) == 1


def test_f1_loop_takes_over_after_human_override_expires(db_session, monkeypatch):
    """人工覆写失效后，后续闭环接手微调（让路是暂时的，不是永久放弃）。"""
    proj = _make_project(db_session)
    set_active_override(
        db_session, project_id=proj.id, prompt_key=PromptKey.CONSTRAINT,
        override_text=HUMAN_OVERRIDE_TEXT, created_by="assistant_proposal",
    )
    db_session.commit()
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    # 第一轮：人工覆写在场 → 跳过
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一段")
    assert _active_constraint_override(
        db_session, proj.id).override_text == HUMAN_OVERRIDE_TEXT
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 1

    # 人工覆写失效（模拟被后续流程置为 inactive）→ 第二轮闭环接手
    human_row = _active_constraint_override(db_session, proj.id)
    human_row.active = False
    db_session.commit()
    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第二段")

    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None
    assert ov.created_by == gen_router._FLAVOR_OVERRIDE_CREATED_BY
    assert "仿佛" in ov.override_text
    assert HUMAN_OVERRIDE_TEXT not in ov.override_text
    # 历史保留：人工行（inactive）+ 闭环行（active）共两条
    assert db_session.scalar(
        select(func.count()).select_from(PromptOverride)) == 2


def test_f1_loop_still_refreshes_its_own_override(db_session, monkeypatch):
    """闭环自己写的覆写（created_by=本闭环标识）照常刷新，不被 F1 误伤。"""
    proj = _make_project(db_session)
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第一段")
    ov1 = _active_constraint_override(db_session, proj.id)
    assert ov1 is not None
    assert ov1.created_by == gen_router._FLAVOR_OVERRIDE_CREATED_BY

    _invoke(db_session, project_id=proj.id, action="generate_next",
            instruction="第二段")
    ov2 = _active_constraint_override(db_session, proj.id)
    assert ov2 is not None
    assert ov2.created_by == gen_router._FLAVOR_OVERRIDE_CREATED_BY
    assert ov2.id != ov1.id, "内容变了应写新行（旧行降级保留）"
    assert "累计 2 次" in ov2.override_text
    assert ov2.override_text != ov1.override_text


# ===========================================================================
# F2：summary 超长不再让闭环微调永久静默罢工
# ===========================================================================


def test_f2_upsert_truncates_oversized_summary(db_session):
    """写入端治本：5000 字 summary 截断到 MAX_SUMMARY_LEN，不抛错、行照写。"""
    proj = _make_project(db_session)

    row = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="霸腔", summary=DIRTY_SUMMARY, hits_delta=1,
    )
    assert len(row.summary) == ai_flavor_memory.MAX_SUMMARY_LEN == 200
    assert row.summary == "霸" * 200

    # 更新路径同样截断（同一去重键累加，不插新行）
    row2 = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="霸腔", summary=DIRTY_SUMMARY, hits_delta=1,
    )
    assert row2.id == row.id and row2.hit_count == 2
    assert len(row2.summary) == ai_flavor_memory.MAX_SUMMARY_LEN

    # 正常长度的 summary 原样保留（截断只针对超长）
    row3 = ai_flavor_memory.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="禁字夸张：不禁/忍不住 使用过密",
    )
    assert row3.summary == "禁字夸张：不禁/忍不住 使用过密"

    db_session.commit()
    assert db_session.query(AIFlavorMemory).count() == 2


def test_f2b_render_fallback_truncates_when_many_rows_exceed(db_session):
    """渲染端兜底：单行都截断后累计仍超闸时，整段按 MAX_OVERRIDE_LEN 截断。

    与 test_f2_render_truncates_oversized_block_to_max_override_len 分工：
    那条验「单行超长」被 `_render_line` 的单行截断吃掉（总长远低于闸口）；
    本条验「多条各自不超单行上限、但累计超闸」时，`_render_flavor_constraint`
    的整段兜底截断仍然生效——两层都在，谁也不许退化成死代码。
    """
    proj = _make_project(db_session)
    # 30 条各自刚好卡在单行上限内的记忆：单行截断管不了它们，
    # 只能靠整段兜底。直接插库绕过 upsert 与 limit 裁剪（load_memories 的
    # limit 是另一道闸，这里要逼的是 render 层的兜底）。
    for i in range(30):
        db_session.add(AIFlavorMemory(
            scope="project", project_id=proj.id, issue_key=f"脏{i:02d}",
            summary="脏" * ai_flavor_memory.MAX_SUMMARY_LEN,
            hit_count=50 - i, severity="mid", active=True, last_segment_no=i,
        ))
    db_session.commit()

    # 绕过 load_memories 的条数闸，直接喂全量 30 条给渲染器
    items = [ai_flavor_memory._row_to_item(r) for r in
             db_session.scalars(select(AIFlavorMemory)).all()]
    block = ai_flavor_memory.render_memory_block(items)
    assert len(block) > MAX_OVERRIDE_LEN, "构造应使渲染块超过闸口"

    # 经端点的渲染入口：整段兜底必须把它压回闸内
    text = gen_router._render_flavor_constraint(db_session, proj.id)
    assert len(text) <= MAX_OVERRIDE_LEN


def test_f2_render_truncates_oversized_block_to_max_override_len(db_session):
    """渲染端治标：既有超长脏行渲染出的覆写按 MAX_OVERRIDE_LEN 截断。"""
    proj = _make_project(db_session)
    # 直接插库绕过 upsert（模拟历史脏数据：源头截断管不到它）
    db_session.add(AIFlavorMemory(
        scope="project", project_id=proj.id, issue_key="霸腔",
        summary=DIRTY_SUMMARY, hit_count=99, severity="high", active=True,
        last_segment_no=99,
        examples=[{"segment_no": 1, "offset": 0, "snippet": "s" * 200}],
    ))
    db_session.commit()

    text = gen_router._render_flavor_constraint(db_session, proj.id)

    assert text.startswith(gen_router._FLAVOR_OVERRIDE_HEADER)
    # 意图是「既有脏数据不得让渲染结果超限」——不是「必须恰好截到闸口」。
    # 单行截断（_render_line）先生效时总长远低于闸口，那是更健康的状态；
    # 断言用 <= 才是守住了意图本身。兜底层仍由
    # test_f2b_render_fallback_truncates_when_many_rows_exceed 覆盖。
    assert len(text) <= MAX_OVERRIDE_LEN, (
        f"超长脏行渲染结果不得超过 {MAX_OVERRIDE_LEN}，实际 {len(text)}")
    # 单行截断生效：脏行的 5000 字 summary 不会原样出现在渲染结果里
    assert DIRTY_SUMMARY not in text


def test_f2_loop_writes_override_despite_dirty_row(db_session, monkeypatch):
    """端到端：脏行在场时闭环仍成功写覆写（不再整体回滚、静默罢工）。"""
    proj = _make_project(db_session)
    db_session.add(AIFlavorMemory(
        scope="project", project_id=proj.id, issue_key="霸腔",
        summary=DIRTY_SUMMARY, hit_count=99, severity="high", active=True,
        last_segment_no=99,
        examples=[{"segment_no": 1, "offset": 0, "snippet": "s" * 200}],
    ))
    db_session.commit()
    _install_writer(monkeypatch, StubProvider(SEVERE_TEXT))

    # 不抛错、端点正常返回（闭环没被 set_active_override 的长度闸炸掉）
    result = _invoke(db_session, project_id=proj.id, action="generate_next",
                     instruction="写一段")
    assert result["segment"]["text"] == SEVERE_TEXT

    # 闭环微调成功：有生效覆写、写入者标识是本闭环、且不超长度闸
    ov = _active_constraint_override(db_session, proj.id)
    assert ov is not None, "脏行不得让闭环微调静默罢工"
    assert ov.created_by == gen_router._FLAVOR_OVERRIDE_CREATED_BY
    assert len(ov.override_text) <= MAX_OVERRIDE_LEN
    # 本次的教训必须进得了提示词（脏行不得把它挤掉）
    assert "仿佛" in ov.override_text, "脏行不得顶掉本次真正的教训"

    # 闭环自己的记忆行已落库（修复前会被整体回滚丢掉）；脏行原封不动
    rows = _memories(db_session)
    keys = {r.issue_key for r in rows}
    assert "仿佛" in keys, "闭环记忆行必须落库"
    dirty = [r for r in rows if r.issue_key == "霸腔"]
    assert len(dirty) == 1
    assert dirty[0].hit_count == 99 and len(dirty[0].summary) == 5000
