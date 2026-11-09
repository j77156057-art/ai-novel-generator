"""时代越界护栏接入产品链路的回归测试。

盯七件事：
1. 默认关闭（`Settings.era_guard_enabled` 的默认值就是 False）；
2. 基线必须由项目显式声明；没有声明 → 不注入、meta 记 `baseline_unknown`；
3. 显式声明但认不出 → 同样不注入，绝不回落王朝；
4. meta 三态可区分：`disabled` / `baseline_unknown` / `active`；
5. 默认 report：扫描但不改正文；
6. 改写必须显式开关（report 下 provider 一次都不许被调）；
7. system 提示注入是纯新增：不传约束时逐字节不变。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.generation.prompts import build_system_prompt
from app.kg.era_check import (
    apply_to_text,
    constraint_for,
    project_profile,
    world_baseline,
)


def _project(*, genre: str = "", **bible) -> SimpleNamespace:
    return SimpleNamespace(
        title="测试书", plot_bible=bible, genre=genre, generation_length=3000
    )


@pytest.fixture
def era_on(monkeypatch):
    """显式打开开关（默认是关的），模式为 report。"""
    monkeypatch.setattr(settings, "era_guard_enabled", True)
    monkeypatch.setattr(settings, "era_guard_mode", "report")
    return settings


def test_switch_defaults_to_off():
    field = type(settings).model_fields["era_guard_enabled"]
    assert field.default is False


def test_disabled_when_off(monkeypatch):
    monkeypatch.setattr(settings, "era_guard_enabled", False)
    project = _project(genre="玄幻")
    text = "他拿起电脑查看账册。"
    out, meta = apply_to_text(text, project, provider=None, segment_no=1)
    assert out == text
    assert meta == {"enabled": False, "status": "disabled"}

    block, meta2 = constraint_for(project)
    assert block == ""
    assert meta2 == {"enabled": False, "status": "disabled", "chars": 0}


def test_no_declaration_does_not_inject(era_on):
    """没有 genre、也没有世界设定：开关开着也不注入。"""
    project = _project()
    block, meta = constraint_for(project)
    assert block == ""
    assert meta["status"] == "baseline_unknown"
    assert meta["chars"] == 0

    text = "他拿起电脑查看账册。"
    out, scan = apply_to_text(text, project, provider=None, segment_no=1)
    assert out == text
    assert scan["status"] == "baseline_unknown"
    assert scan["counts"] == {}


def test_placeholder_genre_counts_as_no_declaration(era_on):
    project = _project(genre="未指定")
    block, meta = constraint_for(project)
    assert block == ""
    assert meta["status"] == "baseline_unknown"


def test_unrecognized_baseline_does_not_fall_back_to_dynasty(era_on):
    project = _project(genre="克苏鲁神话")
    assert world_baseline(project) == ("", "unrecognized:project.genre")

    block, meta = constraint_for(project)
    assert block == ""
    assert meta["status"] == "baseline_unknown"
    assert meta["baseline_source"] == "unrecognized:project.genre"


def test_explicit_baseline_activates_constraint(era_on):
    project = _project(world={"baseline": "王朝"})
    block, meta = constraint_for(project)
    assert block
    assert meta["status"] == "active"
    assert meta["baseline"] == "王朝"
    assert meta["chars"] == len(block)


def test_baseline_read_from_world_setting():
    project = _project(world={"baseline": "近现代都市"})
    assert world_baseline(project) == ("近现代", "plot_bible.world.baseline")
    assert "近现代" in project_profile(project)["allowed_tech_levels"]


def test_genre_is_an_explicit_declaration(era_on):
    project = _project(genre="都市")
    assert world_baseline(project) == ("现代", "project.genre")
    _, meta = constraint_for(project)
    assert meta["status"] == "active"
    assert meta["baseline"] == "现代"


def test_report_mode_scans_but_does_not_touch_text(era_on):
    project = _project(world={"baseline": "王朝"})
    text = "他拿起电脑查看账册。"
    out, meta = apply_to_text(text, project, provider=None, segment_no=1)
    assert out == text
    assert meta["status"] == "active"
    assert meta["mode"] == "report"
    assert meta["counts"].get("hard_ban") == 1
    assert meta["baseline"] == "王朝"


def test_default_report_never_calls_provider(era_on):
    class Boom:
        def chat(self, system, user):  # pragma: no cover - 被调用即失败
            raise AssertionError("report 模式不该调用 provider")

    project = _project(world={"baseline": "王朝"})
    text = "他拿起电脑查看账册。"
    out, meta = apply_to_text(text, project, Boom(), segment_no=1)
    assert out == text
    assert meta["mode"] == "report"


def test_rewrite_mode_requires_explicit_switch(era_on, monkeypatch):
    monkeypatch.setattr(settings, "era_guard_mode", "rewrite")

    class Provider:
        def chat(self, system, user):
            return "他拿起账册查看。"

    project = _project(world={"baseline": "王朝"})
    out, meta = apply_to_text(
        "他拿起电脑查看账册。", project, Provider(), segment_no=1
    )
    assert meta["status"] == "active"
    assert "电脑" not in out
    assert meta["rewrite_changed"] is True
    assert meta["counts"] == {}


def test_rewrite_mode_keeps_sentence_when_provider_is_fallback(era_on, monkeypatch):
    monkeypatch.setattr(settings, "era_guard_mode", "rewrite")
    provider = type("LocalFallbackLLMProvider", (), {"chat": lambda *a, **k: "x"})()
    project = _project(world={"baseline": "王朝"})
    text = "他拿起电脑查看账册。"
    out, meta = apply_to_text(text, project, provider, segment_no=1)
    assert out == text
    assert meta["rewrite_skipped"] == "provider-unavailable"


def test_system_prompt_injection_is_additive():
    base = build_system_prompt("测试书", {}, None, None, None, None)
    assert "ERA-X" not in base
    assert "POWER-X" not in base

    injected = build_system_prompt(
        "测试书", {}, None, None, None, None,
        era_constraint="ERA-X",
        power_constraint="POWER-X",
    )
    assert "ERA-X" in injected and "POWER-X" in injected
    assert injected.index("ERA-X") < injected.index("【风格】")
