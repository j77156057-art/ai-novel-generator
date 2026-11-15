"""时代越界护栏接入产品链路的回归测试。

盯八件事：
1. 默认打开（总开关 + 约束注入 + 扫描留痕三个默认值都是 True，模式 report）；
2. 两个子开关互不牵连：关注入不影响扫描，关扫描不影响注入；
3. 基线必须由项目显式声明；没有声明 → 不注入、不扫描，meta 记 `baseline_unknown`；
4. 显式声明但认不出 → 同样不注入，绝不回落王朝；
5. meta 状态可区分：`disabled` / `constraint_disabled` / `scan_disabled` /
   `baseline_unknown` / `active`；
6. 默认 report：扫描但不改正文；
7. 改写必须显式开关（report 下 provider 一次都不许被调）；
8. 例外通道命中但同句缺引出人 → 降级为 `record_only`，不进 counts/violations。
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
    """确保总开关与两个子开关都开，模式 report（默认即如此，这里显式钉住）。"""
    monkeypatch.setattr(settings, "era_guard_enabled", True)
    monkeypatch.setattr(settings, "era_guard_prompt_enabled", True)
    monkeypatch.setattr(settings, "era_guard_scan_enabled", True)
    monkeypatch.setattr(settings, "era_guard_mode", "report")
    return settings


def test_switches_default_to_on():
    fields = type(settings).model_fields
    for name in (
        "era_guard_enabled",
        "era_guard_prompt_enabled",
        "era_guard_scan_enabled",
    ):
        assert fields[name].default is True


def test_prompt_switch_off_still_scans(era_on, monkeypatch):
    """关掉约束注入不影响扫描：本组就是「约束未注入」的纯观测口径。"""
    monkeypatch.setattr(settings, "era_guard_prompt_enabled", False)
    monkeypatch.setattr(settings, "era_guard_scan_enabled", True)
    project = _project(world={"baseline": "王朝"})
    block, meta = constraint_for(project)
    assert block == ""
    assert meta["status"] == "constraint_disabled"
    assert meta["prompt_injected"] is False

    text = "他拿起电脑查看账册。"
    out, scan = apply_to_text(text, project, provider=None, segment_no=1)
    assert out == text
    assert scan["status"] == "active"
    assert scan["counts"].get("hard_ban") == 1


def test_scan_switch_off_still_injects(era_on, monkeypatch):
    """关掉扫描不影响约束注入：两开关互不牵连。"""
    monkeypatch.setattr(settings, "era_guard_prompt_enabled", True)
    monkeypatch.setattr(settings, "era_guard_scan_enabled", False)
    project = _project(world={"baseline": "王朝"})
    block, meta = constraint_for(project)
    assert block
    assert meta["prompt_injected"] is True

    _, scan = apply_to_text("他拿起电脑查看账册。", project, provider=None, segment_no=1)
    assert scan["status"] == "scan_disabled"
    assert scan["counts"] == {}
    assert scan["records"] == []


def test_channel_hit_without_introducer_is_record_only(era_on):
    """通道命中但同句没引出人：记录不判，不进 counts/violations。"""
    project = _project(world={"baseline": "王朝"}, characters=[{"name": "张三"}])
    text = "系统提示先弹出来：缺少原持有人印章。"
    out, meta = apply_to_text(text, project, provider=None, segment_no=1)
    assert out == text
    assert meta["status"] == "active"
    assert meta["counts"] == {}
    assert meta["record_counts"] == {"channel_hit_no_introducer": 1}
    assert meta["records"][0]["violation"] == "record_only"


def test_period_lexicon_only_loaded_for_modern_baseline(era_on):
    """前现代档位只对声明的基线装载；王朝书零影响。"""
    from app.kg.era_check import project_profile
    from app.kg.era_guard import EraGuard

    modern = EraGuard(profile=project_profile(_project(genre="都市")))
    assert modern.lexicon["衙门"]["level"] == "前现代"
    assert modern.lexicon["衙门"]["allowed"] is False
    # 与原生档重名的词被移出原生、挂到前现代（对现代基线才是错位）
    assert modern.lexicon["铜钱"]["level"] == "前现代"

    dynasty = EraGuard(profile=project_profile(_project(world={"baseline": "王朝"})))
    assert "衙门" not in dynasty.lexicon
    assert dynasty.lexicon["铜钱"]["level"] == "原生"
    assert dynasty.lexicon["铜钱"]["allowed"] is True


def test_period_lexicon_excludes_cultivation_terms(era_on):
    """题材词不进时代表：宗门/掌门/长老/弟子/灵石。"""
    from app.kg.era_check import project_profile
    from app.kg.era_guard import EraGuard

    guard = EraGuard(profile=project_profile(_project(genre="都市")))
    for word in ("宗门", "掌门", "长老", "弟子", "灵石"):
        assert word not in guard.lexicon


def test_period_term_flags_in_modern_book(era_on):
    """都市书里的前现代制度词：判 needs_authorization，来源记 period_mismatch。"""
    project = _project(genre="都市")
    _, meta = apply_to_text("知府大人让人把卷宗抬进衙门。", project, None, segment_no=1)
    assert meta["status"] == "active"
    assert meta["note_counts"].get("period_mismatch", 0) >= 1
    assert meta["counts"].get("needs_authorization", 0) >= 1
    levels = {item["tech_level"] for item in meta["violations"]}
    assert levels == {"前现代"}


def test_period_term_not_loaded_for_dynasty_book(era_on):
    """王朝书里「银子」不装表，不产生任何 mention。"""
    project = _project(world={"baseline": "王朝"})
    _, meta = apply_to_text("他掏出银子放在桌上。", project, None, segment_no=1)
    assert meta["mentions"] == 0
    assert meta["counts"] == {}


def test_period_legit_context_is_record_only(era_on):
    """道具/剧组语境：只留痕不判罚，不进 counts。"""
    project = _project(genre="都市")
    _, meta = apply_to_text("剧组道具，展柜里摆着一面衙门旧匾。", project, None, segment_no=1)
    assert meta["counts"] == {}
    assert meta["record_counts"].get("period_legit") == 1
    assert meta["violations"] == []


def test_channel_abuse_aggregates_threshold_and_streak():
    from app.kg.era_check import CHANNEL_ABUSE_KEY, channel_abuse

    hit = {CHANNEL_ABUSE_KEY: 3}
    out = channel_abuse(hit, [{}, {}], threshold=3, streak=2)
    assert out["flagged"] is True
    assert "chapter_threshold" in out["flags"]

    one = {CHANNEL_ABUSE_KEY: 1}
    out2 = channel_abuse(one, [one], threshold=3, streak=2)
    assert out2["consecutive"] == 2
    assert "consecutive_chapters" in out2["flags"]

    out3 = channel_abuse({}, [one], threshold=3, streak=2)
    assert out3["flagged"] is False
    assert out3["consecutive"] == 0


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
