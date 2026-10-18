"""重生成路径的 system 提示应携带叙事档案块（口径统一，刻意变更）。

## 背景

`_build_generation_prompts`（供 `regenerate_segment_stream` 组装提示词）原只向
`build_system_prompt` 传 5 个位置参数，缺第 6 个 `style_narrative`；而
`generate_next_segment` / `generate_next_segment_stream` / generate-plan 三个入口
都传了 6 个。

**这是作者有意排除，不是同步失误。** `93a6337`（feat(style): P2 叙事档案提炼）的提交信息明确写着：
「三个生成函数（generate_next_segment / stream / generate-plan）同步传入。
重生成路径刻意不带（与「重生成不带 RAG」口径一致）。」

## 为什么仍要改

- 重生成路径**同期已传入 `_style_profile`**（engine.py:907-910），它同样是参考语料
  派生的静态档案块。若「不带参考语料」是统一口径，profile 也应剔除——现状自相矛盾。
- RAG 注入的是**剧情素材**，叙事档案是**写法规则**，二者不同类，不宜用同一条
  排除理由。

故本文件守护的行为是：**重生成路径应与两条续写入口的静态档案块口径一致**。
这是对 `93a6337` 有意排除的一次**刻意变更（行为变更，不是修 bug）**，理由如上。

## 影响面

已蒸馏过叙事档案的项目，「重新生成」的输出会发生变化（补上叙事手法/对白风格/
节奏控制等规则块）。无档案项目行为不变（见 T3）。

## 本文件

  T1 单元级：`_build_generation_prompts` 传给 `build_system_prompt` 的第 6 个
     位置参数必须非 None 且 == `_style_narrative(db, project)`；**同时断言续写路径
     也传了**，否则无法区分「改好了」与「根本没跑到」。
  T2 路径级：走真实 `regenerate_segment_stream` 生成器，断言最终交给 provider
     的 system **含**叙事档案块；对照 `generate_next_segment_stream` 同样含。
  T3（文档型断言，0 变异覆盖）：`_style_narrative` 返回 None（无参考语料）时，
     重生成路径的 system 与「5 参调用」逐字节一致。由于 `build_system_prompt` 的
     第 6 参默认值就是 None，「传 5 参」与「传 None 当第 6 参」语义必然等价，
     故本条对本次变更**恒真**——它只记录「无档案项目行为不变」这一不变量，
     不构成变异检测，不计入红→绿信用。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.generation.engine as engine_mod  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    _build_generation_prompts,
    generate_next_segment_stream,
    regenerate_segment_stream,
)
from app.generation.prompts import build_system_prompt  # noqa: E402
from app.kg.build import merge_segment_into_kg  # noqa: E402
from app.models import Generation, Project  # noqa: E402

#: `describe_narrative`（app/style/narrative.py:2123）产出的块首行——用它做路径级断言，
#: 不依赖具体规则文字，避免测试与档案内容耦合。
NARRATIVE_BLOCK_HEADER = "【叙事档案：从参考语料提炼，生成时请遵循以下叙事手法】"

#: 一份形状合法（rules + forbidden）的叙事档案，`describe_narrative` 会渲染出非空块。
NARRATIVE = {
    "rules": {"人物描写手法": [{"rule": "出场先写环境再写人（证据：她抬起头）"}]},
    "forbidden": {"禁用转折词": ["就在这时"]},
}

SEG1 = "林夜推开沉重的木门。\n林夜说道：“我回来了。”"
SEG2 = "白光抬起头，望向窗外。\n白光说道：“天快亮了。”"


class RecordingProvider:
    """确定性假 provider：chat / stream_chat 返回同一份文本，并记录收到的 system。"""

    def __init__(self, text: str = "他站在原地没动，手指慢慢收拢。") -> None:
        self.text = text
        self.systems: list[str] = []

    def chat(self, system: str, user: str, constraints: dict | None = None, **kwargs) -> str:
        self.systems.append(system)
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None, **kwargs):
        self.systems.append(system)
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


def _make_project(db, title: str = "叙事档案口径测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料直接放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project, segment_no: int, text: str) -> None:
    db.add(
        Generation(
            project_id=project.id,
            segment_no=segment_no,
            text=text,
            summary={"text": f"段{segment_no}摘要"},
        )
    )
    db.commit()
    merge_segment_into_kg(db, project.id, text, segment_no, source="generated")


# ---------------------------------------------------------------------------
# T1：单元级——第 6 个位置参数必须被传且非 None（两条路径对照）
# ---------------------------------------------------------------------------


def test_regenerate_path_forwards_style_narrative_as_sixth_arg(db_session, monkeypatch):
    """`_build_generation_prompts` 必须把 `_style_narrative` 作为第 6 个位置参数传下去。

    变更前：只传 5 个 → `args[5]` 不存在 → 红。
    对照：续写路径同断言，证明测试真的驱动到了两条不同路径（防空转）。
    """
    proj = _make_project(db_session)

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        engine_mod,
        "build_system_prompt",
        lambda *a, **k: (calls.append((a, k)) or "SYS"),
    )
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: NARRATIVE)

    # —— 重生成路径 ——
    _build_generation_prompts(db_session, proj, before_segment=1)
    assert calls, "重生成路径未调用 build_system_prompt"
    regen_args, _ = calls[-1]
    assert len(regen_args) >= 6, (
        f"重生成路径只传了 {len(regen_args)} 个位置参数（应为 6，第 6 个是叙事档案）："
        f"args={regen_args}"
    )
    assert regen_args[5] is not None, (
        "重生成路径第 6 个位置参数是 None——叙事档案块未传入（重生成应与续写入口口径一致）"
    )
    assert regen_args[5] == NARRATIVE, (
        f"重生成路径第 6 个位置参数不是 _style_narrative 的返回值：{regen_args[5]!r}"
    )

    # —— 对照：续写路径同样传了（否则本测试分不清「改好了」和「没跑到」）——
    calls.clear()
    list(generate_next_segment_stream(db_session, proj, provider=RecordingProvider()))
    assert calls, "续写路径未调用 build_system_prompt"
    cont_args, _ = calls[-1]
    assert len(cont_args) >= 6, f"续写路径应传 6 个位置参数，实际 {len(cont_args)}"
    assert cont_args[5] == NARRATIVE, (
        f"续写路径第 6 个位置参数异常：{cont_args[5]!r}（对照组失效，测试无意义）"
    )


# ---------------------------------------------------------------------------
# T2：路径级——最终交给 provider 的 system 含叙事档案块
# ---------------------------------------------------------------------------


def test_regenerate_stream_system_includes_narrative_block(db_session, monkeypatch):
    """走真实 `regenerate_segment_stream`，断言 provider 收到的 system 含叙事档案块。

    变更前：重生成 system 缺该块 → 与续写入口口径不一致 → 红。
    对照：`generate_next_segment_stream` 的 system 必含该块。
    """
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: NARRATIVE)

    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, SEG1)
    _seed_segment(db_session, proj, 2, SEG2)

    # —— 重生成路径 ——
    regen = RecordingProvider()
    events = list(
        regenerate_segment_stream(db_session, proj, segment_no=2, provider=regen)
    )
    assert any(e.get("type") == "done" for e in events), "重生成未产出 done 事件"
    assert regen.systems, "重生成未把 system 交给 provider"
    assert NARRATIVE_BLOCK_HEADER in regen.systems[0], (
        "重生成路径的 system 缺少【叙事档案】块——与两条续写入口的静态档案块口径不一致（重生成应同样携带叙事档案）"
    )

    # —— 对照：续写路径同样含该块 ——
    cont = RecordingProvider()
    list(generate_next_segment_stream(db_session, proj, provider=cont))
    assert cont.systems, "续写未把 system 交给 provider"
    assert NARRATIVE_BLOCK_HEADER in cont.systems[0], (
        "续写路径的 system 应含【叙事档案】块（对照组失效，测试无意义）"
    )


# ---------------------------------------------------------------------------
# T3（文档型断言，0 变异覆盖）：无叙事档案时，重生成 system 与 5 参调用逐字节一致
# ---------------------------------------------------------------------------


def test_no_archive_project_output_unchanged_documented_equivalence(db_session, monkeypatch):
    """无叙事档案时，本次变更不改变重生成 system 的任何字节（不变量记录）。

    **本条对本次变更恒真（0 变异覆盖）**：`build_system_prompt` 的第 6 参默认值
    就是 None，「传 5 参」与「传 None 当第 6 参」语义必然等价。它只用于记录
    「无档案项目行为不变」这一不变量，不是变异检测，不计入红→绿信用。
    """
    proj = _make_project(db_session)
    monkeypatch.setattr(engine_mod, "_style_narrative", lambda db, p: None)

    system, _, _ = _build_generation_prompts(db_session, proj, before_segment=1)

    # 逐字节复刻变更前那条调用（engine.py:907-910，只传 5 个位置参数）
    legacy = build_system_prompt(
        proj.title, engine_mod.get_reminders(db_session, proj.id, source="generated"),
        engine_mod._style_info(proj), proj.plot_bible,
        engine_mod._style_profile(db_session, proj),
    )

    assert system == legacy, (
        "无叙事档案项目上，重生成 system 与变更前不一致——变更越界改变了行为"
    )
    assert NARRATIVE_BLOCK_HEADER not in system
