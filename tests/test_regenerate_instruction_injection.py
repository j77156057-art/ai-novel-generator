"""重写卡的 instruction 真正进入生成（2026-11-11）。

## 被钉住的缺陷

`/api/assistant/invoke-generation` 的 `regenerate_segment` 分支此前只把
`instruction` 写进 `AgentRun.request_hash` 与响应文案，调用
`engine.regenerate_segment_stream` 时**没有传**，引擎侧也没有这个参数。
用户看到「已按你的指令重写」，但指令对正文零效果——话说了，没生效。

## 本文件守护的

  B2-1  带指令时：user prompt 里出现【本次重写要求】块且含指令原文；
        system prompt 不变；指令块位于【续写指令】之后，不追加到待改正文末尾。
  B2-2  done 事件与路由响应都带 `instruction_injected=True`。
  B2-3  除该指令块外，user prompt 与不带指令时逐字节相同（含物品持有块与
        constraints 的位置），证明参数化没有改变原文生成口径。
  B2-4  constraints 与物品持有块仍正确注入、两次运行逐项相等，没被指令挤掉。
  B2-5  instruction 为 None / "" / 纯空白：提示词逐字节一致，
        `instruction_injected=False`，落库正文不含指令文本。
  B2-6  路由：指令透传到 engine，响应带 `instruction_injected`；
        未真正注入时不得声称「已按你的指令」。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.generation.engine as engine_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import regenerate_segment_stream  # noqa: E402
from app.generation.timeline import plan_timeline, state_to_dict  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Generation, Project  # noqa: E402
from app.routers import assistant_generation as ag  # noqa: E402

#: 刻意不含「只/仅/保留/段尾」等局部关键词——含局部关键词的指令现在会分流
#: 到句子级补丁（见 test_local_patch.py），那条路径不经过这里要钉的整段
#: 重写提示词。
INSTRUCTION = "把这一段写得更紧凑，减少环境描写，突出两人对峙的动作。"

ITEM_FACT = "铜钥匙（现在在林夜手上）"

PROMPT_BLOCK_HEADER = "【本次重写要求（用户指定；与已发生事实、物品归属冲突时以事实为准）】"

SEG1 = "林夜推开沉重的木门。\n林夜说道：“我回来了。”"
SEG2 = "白光抬起头，望向窗外。\n白光说道：“天快亮了。”"

PLOT_BIBLE = {
    "characters": [
        {"name": "林夜", "role": "主角"},
        {"name": "白光", "role": "同伴"},
    ],
    "foreshadowing": ["黑铁钥匙的来历", "白光的真实身份"],
}

#: 假 provider 的固定正文：落库正文必须与它逐字相同（不掺指令/约束）。
DEFAULT_TEXT = "他站在原地没动，手指慢慢收拢。"


class RecordingProvider:
    def __init__(self, text: str = DEFAULT_TEXT) -> None:
        self.text = text
        self.systems: list[str] = []
        self.users: list[str] = []
        self.constraints: list[dict | None] = []

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.systems.append(system)
        self.users.append(user)
        self.constraints.append(constraints)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def summarize_rollup(self, texts: list[str], level: str) -> str:
        return f"{level}滚上摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        self.systems.append(system)
        self.users.append(user)
        self.constraints.append(constraints)
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


def _isolate(monkeypatch) -> None:
    """隔离静态档案、蒸馏预检与物品持有读通道（各自有独立测试）。"""
    monkeypatch.setattr(engine_mod, "_require_distill_ready", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_style_profile", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: None)
    monkeypatch.setattr(engine_mod, "_item_holding_text", lambda db, p, n: ITEM_FACT)


def _make_project(db, title: str) -> Project:
    proj = Project(
        title=title,
        target_length=5000,
        generation_length=3000,
        mode="quick",
        status="created",
        plot_bible=dict(PLOT_BIBLE),
        outline=["林夜回到旧宅", "白光在窗边等他"],
        premise="一个雨夜的重逢",
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    # 时间线语义合流后，「有 outline 但计划是占位」会让生成入口 409 暂停。
    # 本文件钉的是重写指令的注入位置，给一份可用计划让重生成正常走完。
    st = plan_timeline(total_segments=3)
    for i, node in enumerate(st.nodes.values(), 1):
        node.title = f"旧宅节点{i}"
        node.entry_condition = "林夜回到旧宅"
        node.exit_condition = f"完成第{i}件事"
    proj.plot_timeline = state_to_dict(st)
    db.commit()
    db.add(
        Generation(
            project_id=proj.id,
            segment_no=1,
            text=SEG1,
            summary={"text": "段1摘要"},
        )
    )
    db.add(
        Generation(
            project_id=proj.id,
            segment_no=2,
            text=SEG2,
            summary={"text": "段2摘要"},
        )
    )
    db.commit()
    return proj


def _run(db, proj, instruction=None):
    prov = RecordingProvider()
    events = list(
        regenerate_segment_stream(
            db, proj, segment_no=2, provider=prov, instruction=instruction
        )
    )
    done = next(e for e in events if e.get("type") == "done")
    return prov, done


def _first_constraints(prov: RecordingProvider):
    return next(c for c in prov.constraints if c is not None)


def _generation_prompt(prov: RecordingProvider) -> tuple[str, str]:
    """真正的生成调用 = 第一个**带 constraints** 的 chat/stream_chat。

    时间线节点规划会在它之前发一次不带 constraints 的 `provider.chat`
    （见 test_regenerate_no_wasted_context.py 的同款说明），所以不能取 [0]。
    """
    idx = next(i for i, c in enumerate(prov.constraints) if c is not None)
    return prov.systems[idx], prov.users[idx]


# ---------------------------------------------------------------------------
# B2-1 / B2-2 / B2-3 / B2-4
# ---------------------------------------------------------------------------


def test_instruction_reaches_user_prompt_without_touching_anything_else(db_session, monkeypatch):
    _isolate(monkeypatch)
    # 项目标题会进 system prompt，两次对照必须同题，否则比的是标题差异
    proj_ctrl = _make_project(db_session, "指令注入对照")
    proj_instr = _make_project(db_session, "指令注入对照")

    prov_ctrl, done_ctrl = _run(db_session, proj_ctrl)
    prov_instr, done_instr = _run(db_session, proj_instr, instruction=INSTRUCTION)

    control_system, control_user = _generation_prompt(prov_ctrl)
    instr_system, instr_user = _generation_prompt(prov_instr)

    # system 是静态层：指令只进 user 的指令区，不该改动 system
    assert instr_system == control_system, "instruction 不应改动 system prompt"

    # 指令块真的在 user prompt 里
    assert PROMPT_BLOCK_HEADER in instr_user, "user prompt 缺少【本次重写要求】块"
    assert INSTRUCTION in instr_user, "user prompt 缺少指令原文"
    assert PROMPT_BLOCK_HEADER not in control_user

    # 位置：在【续写指令】之后（指令区），不是追加在待改正文/物品块/constraints 里
    assert instr_user.index(PROMPT_BLOCK_HEADER) > instr_user.index("【续写指令】")

    # 除指令块外逐字节相同（含物品持有块与 constraints 的位置）
    assert instr_user == control_user + "\n\n" + PROMPT_BLOCK_HEADER + "\n" + INSTRUCTION, (
        "带指令的 user prompt 并非「在不带指令版本末尾追加指令块」，"
        "说明参数化改变了原文生成口径"
    )

    # B2-4 物品持有块与 constraints 都在，且未被挤掉
    assert "【物品持有事实（已写内容最新一段；续写须与此一致）】" in instr_user
    assert ITEM_FACT in instr_user
    assert instr_user.index(ITEM_FACT) < instr_user.index(PROMPT_BLOCK_HEADER)

    ctrl_constraints = _first_constraints(prov_ctrl)
    instr_constraints = _first_constraints(prov_instr)
    assert ctrl_constraints and instr_constraints
    assert "林夜" in instr_constraints["characters"]
    assert "黑铁钥匙的来历" in instr_constraints["plotholes"]
    assert json.dumps(ctrl_constraints, sort_keys=True, ensure_ascii=False) == json.dumps(
        instr_constraints, sort_keys=True, ensure_ascii=False
    ), "instruction 改动影响了 constraints（角色/伏笔锁定名单被挤掉或改写）"

    # B2-2 done 标记
    assert done_ctrl.get("instruction_injected") is False
    assert done_instr.get("instruction_injected") is True

    # 落库正文不因指令被追加内容（正文只来自 provider）
    gen = db_session.scalars(
        select(Generation).where(
            Generation.project_id == proj_instr.id, Generation.segment_no == 2
        )
    ).first()
    assert gen is not None
    assert gen.text == DEFAULT_TEXT, "重写正文被指令/约束文本污染"
    assert INSTRUCTION not in gen.text
    assert "【本次重写要求" not in gen.text


# ---------------------------------------------------------------------------
# B2-5 空指令逐字节兼容
# ---------------------------------------------------------------------------


def test_empty_instruction_keeps_prompt_byte_identical(db_session, monkeypatch):
    _isolate(monkeypatch)
    # 同标题：system prompt 含书名，标题不同会比出与参数化无关的差异
    proj_a = _make_project(db_session, "空指令对照")
    proj_b = _make_project(db_session, "空指令对照")
    proj_c = _make_project(db_session, "空指令对照")

    prov_a, done_a = _run(db_session, proj_a, instruction=None)
    prov_b, done_b = _run(db_session, proj_b, instruction="")
    prov_c, done_c = _run(db_session, proj_c, instruction="  \n\t ")

    sys_a, user_a = _generation_prompt(prov_a)
    sys_b, user_b = _generation_prompt(prov_b)
    sys_c, user_c = _generation_prompt(prov_c)

    digests = [
        hashlib.md5((s + "\x00" + u).encode("utf-8")).hexdigest()
        for s, u in ((sys_a, user_a), (sys_b, user_b), (sys_c, user_c))
    ]
    assert digests[0] == digests[1] == digests[2], (
        "空串/纯空白指令改变了提示词字节，破坏「与改动前逐字一致」："
        f"{digests}"
    )
    assert PROMPT_BLOCK_HEADER not in user_b
    assert PROMPT_BLOCK_HEADER not in user_c
    for done in (done_a, done_b, done_c):
        assert done.get("instruction_injected") is False


# ---------------------------------------------------------------------------
# B2-6 路由：透传 + 诚实文案
# ---------------------------------------------------------------------------


def _fake_regen_factory(db_session, gen_id: int, injected: bool, captured: dict):
    def _fake(db, project, segment_no, **kwargs):
        captured.update(kwargs)
        captured["segment_no"] = segment_no
        yield {"type": "chunk", "text": "新正文"}
        yield {
            "type": "done",
            "id": gen_id,
            "segment_no": segment_no,
            "chars": len("新正文"),
            "summary": "新摘要",
            "summary_tree": {},
            "instruction_injected": injected,
        }

    return _fake


def _route_fixture(db_session):
    proj = _make_project(db_session, "路由指令")
    gen = db_session.scalars(
        select(Generation).where(
            Generation.project_id == proj.id, Generation.segment_no == 2
        )
    ).first()
    return proj, gen


def test_route_passes_instruction_and_reports_injected(db_session, monkeypatch):
    proj, gen = _route_fixture(db_session)
    captured: dict = {}
    monkeypatch.setattr(
        ag, "regenerate_segment_stream", _fake_regen_factory(db_session, gen.id, True, captured)
    )
    monkeypatch.setattr(ag, "_run_ai_flavor_feedback", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_writer_model_label", lambda: "test-model")

    client = TestClient(fastapi_app)
    resp = client.post(
        "/api/assistant/invoke-generation",
        json={
            "project_id": proj.id,
            "action": "regenerate_segment",
            "segment_no": 2,
            "instruction": INSTRUCTION,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert captured.get("instruction") == INSTRUCTION, (
        f"路由没有把 instruction 透传给引擎：{captured}"
    )
    assert body["instruction_injected"] is True
    assert "已按你的指令重写第 2 段" in body["answer"], body["answer"]


def test_route_does_not_claim_instruction_applied_when_not_injected(db_session, monkeypatch):
    proj, gen = _route_fixture(db_session)
    captured: dict = {}
    monkeypatch.setattr(
        ag, "regenerate_segment_stream", _fake_regen_factory(db_session, gen.id, False, captured)
    )
    monkeypatch.setattr(ag, "_run_ai_flavor_feedback", lambda *a, **k: None)
    monkeypatch.setattr(ag, "_writer_model_label", lambda: "test-model")

    client = TestClient(fastapi_app)
    resp = client.post(
        "/api/assistant/invoke-generation",
        json={
            "project_id": proj.id,
            "action": "regenerate_segment",
            "segment_no": 2,
            "instruction": INSTRUCTION,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["instruction_injected"] is False
    assert "已按你的指令" not in body["answer"], (
        "指令没有进 prompt，响应却声称「已按你的指令」——又回到「话了没生效」"
    )
    assert "没能进入这次生成" in body["answer"], body["answer"]
