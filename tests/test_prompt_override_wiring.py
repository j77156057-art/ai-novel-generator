"""prompt override 接入 Writer 生成链（让「自动微调提示词」真正生效）。

背景：`resolve_prompt_block()`（app/agents/prompt_overrides.py:113）一直只在
模块内部自调，Writer 生成链从不读它——三个槽位（style_suffix /
constraint_note / directive_suffix）写了也不生效。本文件守护接入后的行为：

  O1 注入生效：设置覆写后，`_build_generation_prompts` 产出的 system/user
     确实包含对应块，且落点符合 prompt_overrides.py:40-66 的既有语义
     （【风格】区后 / 【续写纪律】后 / 【续写指令】后），块标题标明来源。
  O2 零变化：未设置覆写时，engine 传空串、输出不含覆写标记，且与「改动前
     的旧参数直调」逐字节一致——老项目不受影响是硬要求。
  O3 prompts 纯函数层：空串 / None / 纯空白入参不产生任何字符。

DB 用 tests/conftest.py 强制的独立临时库；StubProvider 确定性返回，零 LLM。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.generation.engine as engine_mod  # noqa: E402
from app.agents.prompt_overrides import PromptKey, set_active_override  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    _build_generation_prompts,
    generate_next_segment,
    generate_next_segment_stream,
)
from app.generation.prompts import (  # noqa: E402
    _OVERRIDE_CONSTRAINT_HEADER,
    _OVERRIDE_DIRECTIVE_HEADER,
    _OVERRIDE_STYLE_HEADER,
    build_system_prompt,
    build_user_prompt,
)
from app.generation.summary_tree import (  # noqa: E402
    budget_for_generation,
    compose_prior,
    l1_chain,
)
from app.models import Project  # noqa: E402

#: 覆写文本：刻意选不可能出现在代码默认提示词里的串
STYLE_TEXT = "调性补充测试串：冷峻、短句、少连接词"
CONSTRAINT_TEXT = "硬约束测试串：每段至少两次环境描写"
DIRECTIVE_TEXT = "额外指令测试串：结尾必须落在意外动作上"

#: 干净正文：不含任何 AI_FLAVOR_PATTERNS，也不触发重写
CLEAN_TEXT = "林夜走在长街上。夜风微凉，街灯昏黄。他停步回望，身影被拉得很长。"


class RecordingProvider:
    """确定性假 provider：记录收到的 system/user，零 LLM。"""

    def __init__(self, text: str = CLEAN_TEXT) -> None:
        self.text = text
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


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_project(db, title: str = "提示词覆写接入测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检，`_style_profile` / `_style_narrative` 返回 None。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _set_override(db, project_id: int, prompt_key: str, text: str) -> None:
    """走 `set_active_override`（apply 事务调用的同一实现）落一条 active 覆写。"""
    set_active_override(db, project_id=project_id, prompt_key=prompt_key, override_text=text)
    db.commit()


# ---------------------------------------------------------------------------
# O1：注入生效 + 落点符合既有语义
# ---------------------------------------------------------------------------


def test_overrides_reach_generated_prompts(db_session):
    """三个槽位全部设置后，regen 用的 `_build_generation_prompts` 必须带上它们。"""
    proj = _make_project(db_session)
    _set_override(db_session, proj.id, PromptKey.STYLE, STYLE_TEXT)
    _set_override(db_session, proj.id, PromptKey.CONSTRAINT, CONSTRAINT_TEXT)
    _set_override(db_session, proj.id, PromptKey.DIRECTIVE, DIRECTIVE_TEXT)

    system, user, _ = _build_generation_prompts(db_session, proj)

    # style_suffix：落在【风格】区之后、【文风要求】之前（与既有语义一致）
    assert STYLE_TEXT in system, "style_suffix 未进入 system"
    assert (
        system.index("【风格】")
        < system.index(STYLE_TEXT)
        < system.index("【文风要求：真人小说感，忌 AI 腔】")
    ), "style_suffix 落点错误：应夹在【风格】区与【文风要求】之间"

    # constraint_note：落在【续写纪律】之后（system 末尾）
    assert CONSTRAINT_TEXT in system, "constraint_note 未进入 system"
    assert system.index("【续写纪律】") < system.index(CONSTRAINT_TEXT), (
        "constraint_note 落点错误：应排在【续写纪律】之后"
    )

    # directive_suffix：落在【续写指令】之后（user 末尾）
    assert DIRECTIVE_TEXT in user, "directive_suffix 未进入 user"
    assert user.index("【续写指令】") < user.index(DIRECTIVE_TEXT), (
        "directive_suffix 落点错误：应排在【续写指令】之后"
    )

    # 来源可区分：块标题标明是作者助理微调，不是参考作者的规则
    assert f"【{_OVERRIDE_STYLE_HEADER}】" in system
    assert f"【{_OVERRIDE_CONSTRAINT_HEADER}】" in system
    assert f"【{_OVERRIDE_DIRECTIVE_HEADER}】" in user


def test_prompt_overrides_helper_reads_active_rows(db_session):
    """读取层：未设置全空串；设置后对应键拿到覆写文本，其余仍为空。"""
    proj = _make_project(db_session)

    empty = engine_mod._prompt_overrides(db_session, proj.id)
    assert empty == {
        PromptKey.STYLE: "",
        PromptKey.CONSTRAINT: "",
        PromptKey.DIRECTIVE: "",
    }, f"未设置覆写时应全为空串：{empty}"

    _set_override(db_session, proj.id, PromptKey.CONSTRAINT, CONSTRAINT_TEXT)
    after = engine_mod._prompt_overrides(db_session, proj.id)
    assert after[PromptKey.CONSTRAINT] == CONSTRAINT_TEXT
    assert after[PromptKey.STYLE] == "" and after[PromptKey.DIRECTIVE] == ""


def test_continue_paths_carry_override_end_to_end(db_session):
    """两条续写入口（非流式 / 流式）真正把覆写交给 provider，不止组装函数里有。"""
    proj = _make_project(db_session)
    _set_override(db_session, proj.id, PromptKey.CONSTRAINT, CONSTRAINT_TEXT)

    provider = RecordingProvider()
    result = generate_next_segment(db_session, proj, provider=provider)
    assert CONSTRAINT_TEXT in provider.systems[0], "非流式续写未把覆写下发"
    assert CONSTRAINT_TEXT in result["system_prompt"]

    stream_provider = RecordingProvider()
    events = list(generate_next_segment_stream(db_session, proj, provider=stream_provider))
    assert any(e.get("type") == "done" for e in events), "流式续写未产出 done"
    assert CONSTRAINT_TEXT in stream_provider.systems[0], "流式续写未把覆写下发"


# ---------------------------------------------------------------------------
# O2：未设置覆写 → 零变化
# ---------------------------------------------------------------------------


def test_no_override_output_unchanged(db_session):
    """未设置覆写时：无覆写标记，且与「改动前旧参数直调」逐字节一致。"""
    proj = _make_project(db_session)

    system, user, max_chars = _build_generation_prompts(db_session, proj)

    # 1) 不含任何覆写标记
    for marker in (_OVERRIDE_STYLE_HEADER, _OVERRIDE_CONSTRAINT_HEADER, _OVERRIDE_DIRECTIVE_HEADER):
        assert marker not in system, f"未设置覆写却出现 {marker}"
        assert marker not in user, f"未设置覆写却出现 {marker}"

    # 2) 逐字节复刻改动前的调用（engine 原样：6 个位置参数、无 override 关键字参数）
    legacy_system = build_system_prompt(
        proj.title,
        engine_mod.get_reminders(db_session, proj.id, source="generated"),
        engine_mod._style_info(proj), proj.plot_bible,
        engine_mod._style_profile(db_session, proj),
        engine_mod._style_narrative(db_session, proj),
    )
    tree = engine_mod._summary_tree(proj)
    blocks, _ = compose_prior(
        tree, budget_chars=budget_for_generation(proj.generation_length)
    )
    legacy_user = build_user_prompt(
        l1_chain(tree),
        [],
        proj.outline,
        proj.generation_length or 3000,
        proj.plot_bible,
        proj.premise,
        prior_blocks=blocks,
    )

    assert system == legacy_system, "未设置覆写时 system 与改动前不一致——接入改变了既有行为"
    assert user == legacy_user, "未设置覆写时 user 与改动前不一致——接入改变了既有行为"
    assert max_chars == (proj.generation_length or 3000)


def test_engine_passes_empty_overrides_when_unset(db_session, monkeypatch):
    """未设置覆写时，engine 传给 prompts 的 override 参数必须是空串（不是 None 泄漏）。"""
    proj = _make_project(db_session)
    seen: dict[str, dict] = {}
    real_system = engine_mod.build_system_prompt
    real_user = engine_mod.build_user_prompt

    def spy_system(*a, **k):
        seen["system"] = k
        return real_system(*a, **k)

    def spy_user(*a, **k):
        seen["user"] = k
        return real_user(*a, **k)

    monkeypatch.setattr(engine_mod, "build_system_prompt", spy_system)
    monkeypatch.setattr(engine_mod, "build_user_prompt", spy_user)

    _build_generation_prompts(db_session, proj)

    assert seen["system"]["style_suffix"] == "", (
        f"未设置覆写时 style_suffix 应为空串：{seen['system'].get('style_suffix')!r}"
    )
    assert seen["system"]["constraint_note"] == ""
    assert seen["user"]["directive_suffix"] == ""


# ---------------------------------------------------------------------------
# O3：prompts 纯函数层——空值不产生任何字符
# ---------------------------------------------------------------------------


def test_empty_override_params_are_byte_identical():
    """空串 / None / 纯空白入参 → 与不传参逐字节一致（零变化的最后一道防线）。"""
    sys_args = (
        "测试项目",
        {"character_constraints": [], "unresolved_plotholes": []},
        {"类型": "仙侠"},
    )
    base = build_system_prompt(*sys_args)
    assert build_system_prompt(*sys_args, style_suffix="", constraint_note="") == base
    assert build_system_prompt(*sys_args, style_suffix=None, constraint_note=None) == base
    assert build_system_prompt(*sys_args, style_suffix="  \n\t ", constraint_note="") == base

    user_args = (["1. 林夜回到老宅。"], [], "大纲", 3000)
    ubase = build_user_prompt(*user_args)
    assert build_user_prompt(*user_args, directive_suffix="") == ubase
    assert build_user_prompt(*user_args, directive_suffix=None) == ubase
    assert build_user_prompt(*user_args, directive_suffix=" \n ") == ubase


def test_override_blocks_land_at_documented_positions():
    """非空覆写的精确落点与块形状（标题行 + 正文 + 换行）。"""
    sys_args = ("测试项目", {}, {"类型": "仙侠"})
    system = build_system_prompt(
        *sys_args, style_suffix=STYLE_TEXT, constraint_note=CONSTRAINT_TEXT
    )

    assert f"【{_OVERRIDE_STYLE_HEADER}】\n{STYLE_TEXT}\n" in system
    assert system.index(STYLE_TEXT) < system.index("【文风要求：真人小说感，忌 AI 腔】")
    assert system.endswith(f"【{_OVERRIDE_CONSTRAINT_HEADER}】\n{CONSTRAINT_TEXT}\n")

    user = build_user_prompt(
        ["1. 林夜回到老宅。"], [], None, 3000, directive_suffix=DIRECTIVE_TEXT
    )
    assert user.endswith(f"【{_OVERRIDE_DIRECTIVE_HEADER}】\n{DIRECTIVE_TEXT}")
    assert user.index("【续写指令】") < user.index(DIRECTIVE_TEXT)
