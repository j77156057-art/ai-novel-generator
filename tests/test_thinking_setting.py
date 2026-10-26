"""思考档位（thinking）的可配置化：接口 ↔ 渠道能力必须对得上。

## 为什么要有这个文件

三家渠道对「思考」的控制**互不相同**，给错字段的后果是**按了没反应**
（服务端静默忽略不认识的字段），比没有这个开关更糟。所以这里钉住三件事：

1. **渠道能力表**：谁能调档、谁能关、谁不支持 —— 与实测/官方文档一致；
2. **界面上拿到的档位 = 后端真实能发的档位**（不给出按了没反应的选项）；
3. **档位真的变成了请求字段**（MiniMax → reasoning_effort；DeepSeek → thinking.disabled），
   且 **不配档位时一个字段都不多发**（行为与加开关之前逐字节一致）。

实测依据（2026-10-26）：
- MiniMax M3.1 关不掉（disabled → 400），只能调档；省略 = max（最慢最贵）。
  2400 字续写：默认 66.7s / completion 8000 打满 / 正文为空；
  low → 36.0s / 2605 / 正文完整。
- MiniMax 老 M3 **不吃** effort（传 low 反而更慢 22.8s vs 19.9s），只能关。
- DeepSeek（通用兼容渠道）：吃 thinking.disabled（v4-pro 27.8s → 8.2s），不吃 effort。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.llm.provider import (  # noqa: E402
    GenericOpenAIChatProvider,
    MiniMaxChatProvider,
    _thinking_kwargs,
    build_provider,
)
from app.routers.settings import (  # noqa: E402
    PROVIDER_META,
    _ROLE_FIELDS,
    thinking_control_for,
)


# ── 1. 渠道能力表 ─────────────────────────────────────────────────
def test_role_fields_include_thinking():
    assert "thinking" in _ROLE_FIELDS
    assert "max_tokens" in _ROLE_FIELDS  # 没把老字段挤掉


def test_minimax_m31_can_only_tune_effort():
    c = thinking_control_for("minimax", "MiniMax-M3.1-Flash")
    assert c["mode"] == "effort"
    assert c["levels"] == ("low", "medium", "high", "xhigh", "max")


def test_minimax_legacy_m3_cannot_tune_effort():
    """老 M3 不认 effort（实测传了更慢）—— 只能关。"""
    c = thinking_control_for("minimax", "MiniMax-M3")
    assert c["mode"] == "disable"
    assert "low" not in c["levels"]


def test_deepseek_via_generic_channel_can_disable():
    c = thinking_control_for("openai", "deepseek-v4-pro")
    assert c["mode"] == "disable"
    assert "off" in c["levels"]


def test_unknown_channel_offers_nothing():
    """没验证过的渠道**不给开关** —— 按了没反应比没有更糟。"""
    for prov in ("zhipu", "siliconflow", "dashscope", "openrouter"):
        c = thinking_control_for(prov, "")
        assert c["mode"] == "none", prov
        assert c["levels"] == (), prov


def test_every_provider_in_meta_has_an_answer():
    """PROVIDER_META 里每个渠道都能拿到一个答案（哪怕是不支持）。"""
    for name, meta in PROVIDER_META.items():
        c = thinking_control_for(name, (meta.get("default_models") or [""])[0])
        assert "mode" in c and "levels" in c


# ── 2. 档位 → 请求字段 ────────────────────────────────────────────
def test_m31_effort_becomes_reasoning_effort():
    kw = _thinking_kwargs("minimax", "MiniMax-M3.1-Flash", "low")
    assert kw == {"reasoning_effort": "low"}


def test_m31_off_degrades_to_lowest_effort():
    """M3.1 关不掉 —— 要「关」就如实给最低档，而不是悄悄忽略。"""
    kw = _thinking_kwargs("minimax", "MiniMax-M3.1-Flash", "off")
    assert kw == {"reasoning_effort": "low"}


def test_legacy_m3_off_becomes_thinking_disabled():
    assert _thinking_kwargs("minimax", "MiniMax-M3", "off") == {"thinking_off": True}


def test_deepseek_off_becomes_thinking_disabled():
    assert _thinking_kwargs("openai", "deepseek-v4-pro", "off") == {"thinking_off": True}


def test_empty_thinking_sends_nothing():
    """不配 ⇒ 一个字段都不多发（与加开关之前逐字节一致）。"""
    assert _thinking_kwargs("minimax", "MiniMax-M3.1-Flash", "") == {}
    assert _thinking_kwargs("openai", "deepseek-v4-pro", "") == {}


def test_illegal_value_is_ignored():
    assert _thinking_kwargs("minimax", "MiniMax-M3.1-Flash", "minimal") == {}
    assert _thinking_kwargs("openai", "deepseek-v4-pro", "high") == {}


# ── 3. 到请求体为止 ───────────────────────────────────────────────
def test_generic_provider_sends_thinking_disabled():
    p = build_provider("openai", "k", "deepseek-v4-pro",
                       "https://api.deepseek.com/v1", thinking_off=True)
    assert isinstance(p, GenericOpenAIChatProvider)
    assert p.extra_body.get("thinking") == {"type": "disabled"}


def test_generic_provider_default_sends_no_thinking():
    p = build_provider("openai", "k", "deepseek-v4-pro", "https://api.deepseek.com/v1")
    assert "thinking" not in p.extra_body


def test_minimax_effort_lands_in_request_body():
    p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1",
                            "MiniMax-M3.1-Flash", **_thinking_kwargs(
                                "minimax", "MiniMax-M3.1-Flash", "low"))
    assert p.extra_body.get("reasoning_effort") == "low"


def test_minimax_without_thinking_is_unchanged():
    """不配档位时 MiniMax 的请求体不含 reasoning_effort（行为不变）。"""
    p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")
    assert "reasoning_effort" not in p.extra_body
