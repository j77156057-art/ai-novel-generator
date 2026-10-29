"""世界背景与硬规则（背景组）——数据形状 / 提示词接线 / 接口行为。

## 为什么有这份测试

背景组这一块踩过的坑里有两条是**静默**的：

1. **`PlotBibleOut` 是白名单** —— pydantic 的 `model_dump()` 会把未声明的
   字段**静默丢掉**。模型给了 `background`，落库前消失，不报错。
   （与 `StrictRequest` 那条同源：拼错字段名不报错，只是没生效。）
2. **提示词里「定义了却没发」** —— 上一次体系那轮就是这样：
   `_SYSTEM_PROMPT` 定义了却从没被引用，端点只发了一行，
   单测 / build / CI 全绿，只有真机点出来才发现。

所以这里钉住：① 提示词里**真的**有背景块，且位置在动态指引之前；
② 时间线模式**也**有；③ 没有背景组时提示词逐字节不变（老项目不受影响）；
④ 白名单不吞字段；⑤ AI 补全**不落库**、保存**要**落库。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.generation.background import (  # noqa: E402
    FIELD_FACTIONS, FIELD_RULES, FIELD_SUMMARY, background_of,
    normalize_background, render_background,
)
from app.generation.prompts import build_user_prompt  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Project  # noqa: E402


# ====================================================================
# 1) 归一化：脏形状不抛，坏字段只丢自己
# ====================================================================

def test_normalize_keeps_well_formed():
    out = normalize_background({
        FIELD_SUMMARY: "  灵气在三十年前枯竭。  ",
        FIELD_RULES: ["灵力只在夜间可用", "  ", "修行者死后不留遗体"],
        FIELD_FACTIONS: ["矿盟——三家垄断残存矿脉"],
    })
    assert out[FIELD_SUMMARY] == "灵气在三十年前枯竭。"
    assert out[FIELD_RULES] == ["灵力只在夜间可用", "修行者死后不留遗体"]
    assert out[FIELD_FACTIONS] == ["矿盟——三家垄断残存矿脉"]


def test_normalize_drops_bad_fields_without_raising():
    """字符串当数组、dict 当字符串 —— 丢字段，不抛。"""
    out = normalize_background({
        FIELD_SUMMARY: {"不是": "字符串"},
        FIELD_RULES: "灵力只在夜间可用；修行者死后不留遗体",
        FIELD_FACTIONS: 123,
    })
    assert FIELD_SUMMARY not in out
    # 字符串被拆成两行（模型常把多条塞进一个字符串）
    assert out[FIELD_RULES] == ["灵力只在夜间可用", "修行者死后不留遗体"]
    assert FIELD_FACTIONS not in out


def test_normalize_accepts_aliases():
    """模型输出不稳定 ⇒ 别名要认。"""
    out = normalize_background({"世界观": "末法时代", "世界规则": ["灵力稀缺"], "势力": ["矿盟"]})
    assert out[FIELD_SUMMARY] == "末法时代"
    assert out[FIELD_RULES] == ["灵力稀缺"]
    assert out[FIELD_FACTIONS] == ["矿盟"]


def test_normalize_empty_is_empty():
    assert normalize_background({}) == {}
    assert normalize_background(None) == {}
    assert normalize_background("不是字典") == {}


def test_normalize_caps_lengths():
    out = normalize_background({
        FIELD_SUMMARY: "长" * 500,
        FIELD_RULES: [f"规则{i}" for i in range(50)],
        FIELD_FACTIONS: [f"地点{i}" for i in range(50)],
    })
    assert len(out[FIELD_SUMMARY]) <= 300
    assert len(out[FIELD_RULES]) <= 20
    assert len(out[FIELD_FACTIONS]) <= 30


def test_render_empty_returns_empty_string():
    """空组 ⇒ 空串（调用方据此整块不输出）。"""
    assert render_background({}) == ""
    assert render_background(None) == ""


def test_render_contains_all_three_parts():
    text = render_background({
        FIELD_SUMMARY: "灵气枯竭",
        FIELD_RULES: ["灵力只在夜间可用"],
        FIELD_FACTIONS: ["矿盟——垄断矿脉"],
    })
    assert "一句话世界：灵气枯竭" in text
    assert "硬规则：灵力只在夜间可用" in text
    assert "势力与地点：矿盟——垄断矿脉" in text


def test_render_has_no_double_punctuation():
    """条目自带句号 + 用「；」连接 ⇒ 不能出现「…黑暗。；灵气…」。"""
    text = render_background({
        FIELD_SUMMARY: "灵气枯竭。",
        FIELD_RULES: ["灵力只在夜间可用。", "修行者死后不留遗体。"],
        FIELD_FACTIONS: ["矿盟——垄断矿脉。"],
    })
    assert "。；" not in text
    assert "硬规则：灵力只在夜间可用；修行者死后不留遗体" in text


def test_background_of_reads_plot_bible():
    assert background_of({"background": {FIELD_SUMMARY: "x"}}) == {FIELD_SUMMARY: "x"}
    assert background_of({}) == {}
    assert background_of(None) == {}


# ====================================================================
# 2) 提示词接线（这是本轮的核心）
# ====================================================================

_BG = {"background": {
    FIELD_SUMMARY: "灵气在三十年前枯竭，修行靠残存矿脉活着。",
    FIELD_RULES: ["灵力只在夜间可用"],
    FIELD_FACTIONS: ["矿盟——三家联合垄断残存矿脉"],
}}


def test_background_block_appears_in_prompt():
    prompt = build_user_prompt([], [], None, 3000, plot_bible=_BG)
    assert "【世界背景" in prompt
    assert "灵气在三十年前枯竭" in prompt
    assert "灵力只在夜间可用" in prompt


def test_background_block_is_before_dynamic_guidance():
    """顺序有理由：恒定设定在前，会变的在后。"""
    prompt = build_user_prompt([], [], None, 3000, plot_bible=_BG)
    assert prompt.index("【世界背景") < prompt.index("【剧情圣经·动态指引】")


def test_background_block_present_in_timeline_mode():
    """时间线模式也注入 —— 世界设定不含走向，剔掉只会让模型自己现编。"""
    prompt = build_user_prompt([], [], None, 3000, plot_bible=_BG,
                               node_prompt="【当前剧情进度】写第一段")
    assert "【世界背景" in prompt
    assert "灵气在三十年前枯竭" in prompt


def test_timeline_mode_still_drops_direction():
    """走向仍然要剔 —— 背景进来不等于走向放行。"""
    bible = {**_BG, "synopsis": "少年负剑下山", "direction": "一路向北"}
    prompt = build_user_prompt([], [], None, 3000, plot_bible=bible,
                               node_prompt="【当前剧情进度】写第一段")
    assert "少年负剑下山" not in prompt
    assert "一路向北" not in prompt
    # 但背景还在
    assert "灵气在三十年前枯竭" in prompt


def test_no_background_block_when_absent():
    """没有背景组 ⇒ 整块不出现（空块是负面暗示）。"""
    prompt = build_user_prompt([], [], None, 3000, plot_bible={"synopsis": "少年负剑下山"})
    assert "【世界背景" not in prompt
    assert "一句话世界" not in prompt


def test_prompt_unchanged_without_background():
    """老项目提示词逐字节不变（回归护栏）。"""
    from app.generation import prompts as prompts_mod

    a = prompts_mod.build_user_prompt(["第一段写到少年下山。"], [], ["大纲一"], 3000,
                                       {"synopsis": "少年负剑下山", "direction": "一路向北"},
                                       "基调：冷")
    b = prompts_mod.build_user_prompt(["第一段写到少年下山。"], [], ["大纲一"], 3000,
                                       {"synopsis": "少年负剑下山", "direction": "一路向北"},
                                       "基调：冷")
    assert a == b
    assert "【世界背景" not in a


def test_background_block_only_when_non_empty():
    """background 存在但内容全空 ⇒ 不出块。"""
    prompt = build_user_prompt([], [], None, 3000,
                               plot_bible={"background": {FIELD_SUMMARY: "  ", FIELD_RULES: []}})
    assert "【世界背景" not in prompt


# ====================================================================
# 3) 白名单：PlotBibleOut 不能把 background 吞掉
# ====================================================================

def test_plot_bible_out_keeps_background():
    """pydantic 的 model_dump() 会静默丢掉未声明字段 ⇒ 必须显式声明。"""
    from app.schemas import PlotBibleOut

    out = PlotBibleOut.model_validate({
        "synopsis": "少年负剑下山",
        "direction": "一路向北",
        "background": {FIELD_SUMMARY: "灵气枯竭", FIELD_RULES: ["灵力稀缺"]},
    }).model_dump()
    assert out.get("background") == {FIELD_SUMMARY: "灵气枯竭", FIELD_RULES: ["灵力稀缺"]}


# ====================================================================
# 4) 接口：保存落库 / AI 补全不落库
# ====================================================================

def _fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = Session(engine)
    # premise 要有内容 —— AI 补全背景组要拿它当参考（没有参考就明说「没有可参考的信息」）。
    p = Project(title="测试项目", target_length=5000, mode="quick",
                genre="仙侠", style="起点", premise="末法时代灵气枯竭，主角在矿盟废墟上拜师")
    db.add(p)
    db.commit()
    db.refresh(p)

    def _override():
        # **不要 close()**：StaticPool 下关掉就把测试要再读的对象 detach 了
        # （DetachedInstanceError）。测试结束自己在 _teardown 里关。
        yield db

    app.dependency_overrides[get_db] = _override
    return db, p, TestClient(app)


def _teardown(db):
    app.dependency_overrides.pop(get_db, None)
    db.close()


def _reload(db, project_id):
    """重新从库里读一次（绕开 identity map 里的旧对象）。"""
    db.expire_all()
    return db.get(Project, project_id)


def test_save_background_persists():
    db, p, client = _fixture()
    try:
        r = client.post(f"/api/projects/{p.id}/background", json={
            "background": {
                FIELD_SUMMARY: "灵气在三十年前枯竭。",
                FIELD_RULES: ["灵力只在夜间可用"],
                FIELD_FACTIONS: ["矿盟——三家垄断矿脉"],
            }
        })
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        saved = background_of(_reload(db, p.id).plot_bible)
        assert saved[FIELD_SUMMARY] == "灵气在三十年前枯竭。"
        assert saved[FIELD_RULES] == ["灵力只在夜间可用"]
    finally:
        _teardown(db)


def test_save_background_rejects_unknown_field():
    """StrictRequest：拼错字段名直接 422，不静默丢弃。"""
    db, p, client = _fixture()
    try:
        r = client.post(f"/api/projects/{p.id}/background", json={"backgrounds": {}})
        assert r.status_code == 422, r.text
    finally:
        _teardown(db)


def test_clear_background_removes_the_key():
    db, p, client = _fixture()
    try:
        client.post(f"/api/projects/{p.id}/background",
                    json={"background": {FIELD_SUMMARY: "x", FIELD_RULES: ["y"]}})
        r = client.post(f"/api/projects/{p.id}/background", json={"background": {}})
        assert r.status_code == 200, r.text
        assert "background" not in (_reload(db, p.id).plot_bible or {})
    finally:
        _teardown(db)


def test_generate_background_does_not_persist():
    """AI 补全只建议，不落库 —— 采纳与否由人点。"""
    db, p, client = _fixture()
    try:
        class _FakeProvider:
            def chat(self, system, user):
                return ('{"一句话世界": "灵气枯竭", "硬规则": ["灵力只在夜间可用"],'
                        ' "势力与地点": ["矿盟"]}')

        import app.routers.projects as projects_router
        original = projects_router._provider_or_none
        projects_router._provider_or_none = lambda: _FakeProvider()
        try:
            r = client.post(f"/api/projects/{p.id}/background/generate", json={"note": ""})
        finally:
            projects_router._provider_or_none = original
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert r.json()["background"][FIELD_SUMMARY] == "灵气枯竭"
        # **没落库**
        assert "background" not in (_reload(db, p.id).plot_bible or {})
    finally:
        _teardown(db)


def test_generate_background_rejects_empty_result():
    """闸门：一句话世界 + 硬规则都要有，别让空壳覆盖作者写好的。"""
    db, p, client = _fixture()
    try:
        class _BadProvider:
            def chat(self, system, user):
                return '{"硬规则": ["只有规则"]}'

        import app.routers.projects as projects_router
        original = projects_router._provider_or_none
        projects_router._provider_or_none = lambda: _BadProvider()
        try:
            r = client.post(f"/api/projects/{p.id}/background/generate", json={"note": ""})
        finally:
            projects_router._provider_or_none = original
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is False
        assert "原文" in r.json()
    finally:
        _teardown(db)


def test_get_background_returns_empty_when_unset():
    db, p, client = _fixture()
    try:
        r = client.get(f"/api/projects/{p.id}/background")
        assert r.status_code == 200, r.text
        assert r.json()["background"] == {}
    finally:
        _teardown(db)
