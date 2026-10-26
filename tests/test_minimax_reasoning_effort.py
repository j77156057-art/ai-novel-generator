"""MiniMax 思考深度（reasoning_effort）：关不掉，但**可以调低**。

2026-10-26 真跑（api.minimaxi.com，OpenAI 兼容端点）得到的结论，钉在测试里：

- 官方 effort 档位：low / medium / high / xhigh / max，**省略 = max**（最慢最贵）；
  思考**关不掉**（thinking={"type":"disabled"} / effort="none" → 400）。
- 2400 字续写（max_tokens=8000）：
  省略 effort(=max) → 66.7s、completion 8000 打满、finish=length、**正文为空**；
  effort=low → 36.0s、completion 2605、finish=stop、正文完整 4079 字。
- **M3（老）不吃 effort**（传 low 反而更慢）—— 它是老式 adaptive，思考混在 content 里。

所以这里的断言全是「行为」而不是「数字」：发了合法的档位就带上、非法值不带、
老模型不受影响、预算地板按「有没有真的调低」给。
"""
from __future__ import annotations

import os
import pathlib
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.llm.provider import MiniMaxChatProvider  # noqa: E402


def _mk(model: str, **kw) -> MiniMaxChatProvider:
    return MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", model, **kw)


# ── 1. M3.1 系：发出去的请求体带 reasoning_effort ─────────────────
def test_m31_sends_reasoning_effort():
    p = _mk("MiniMax-M3.1-Flash", reasoning_effort="low")
    assert p.extra_body.get("reasoning_effort") == "low"


def test_m31_preview_sends_reasoning_effort():
    p = _mk("MiniMax-M3.1-Flash-Preview", reasoning_effort="medium")
    assert p.extra_body.get("reasoning_effort") == "medium"


def test_illegal_effort_level_is_not_sent():
    """非法档位**不发** —— 发了会被服务端忽略，白白让人以为调了档。

    （"minimal" 不是合法值：官方只有 low/medium/high/xhigh/max。）
    """
    p = _mk("MiniMax-M3.1-Flash", reasoning_effort="minimal")
    assert "reasoning_effort" not in p.extra_body


def test_empty_effort_is_not_sent():
    """没配 effort ⇒ 不发 ⇒ 用官方默认（max），行为与调低前逐字节一致。"""
    p = _mk("MiniMax-M3.1-Flash")
    assert "reasoning_effort" not in p.extra_body


# ── 2. M3（老）：不受影响，仍然是 thinking=disabled + 剥离兜底 ──────
def test_legacy_m3_ignores_effort_and_keeps_thinking_disabled():
    p = _mk("MiniMax-M3", reasoning_effort="low")
    assert "reasoning_effort" not in p.extra_body
    assert p.extra_body.get("thinking") == {"type": "disabled"}


# ── 3. 预算地板：按「有没有真的把思考调低」给 ────────────────────
def test_budget_floor_is_lower_when_effort_was_sent():
    """调低思考后思考几乎不占预算 ⇒ 地板降一档（实测 2605 token / 36 秒够用）。

    没调低时地板保持最坏情况 —— 否则思考会吃满预算，**正文被静默截断**
    （2026-10-25 真跑：111.8 秒撞 120 秒超时线，max_tokens 8192 不够）。
    """
    tuned = _mk("MiniMax-M3.1-Flash", reasoning_effort="low",
                max_tokens=4096, timeout=60)
    default = _mk("MiniMax-M3.1-Flash", max_tokens=4096, timeout=60)

    assert tuned.max_tokens < default.max_tokens
    assert tuned.timeout < default.timeout
    # 用户显式给的值更大时，以用户的为准（地板只是下限）
    big = _mk("MiniMax-M3.1-Flash", reasoning_effort="low",
              max_tokens=32768, timeout=300)
    assert big.max_tokens == 32768
    assert big.timeout == 300


# ── 4. 写手通道用配置里的档位；蒸馏/助理角色不传（保持 max）───────
def test_writer_path_uses_configured_effort():
    """写手要吞吐 ⇒ 用配置里的档位（默认 low）。"""
    from app.config import settings

    assert settings.minimax_reasoning_effort == "low", (
        "写手默认档位变了 —— 改前先看 ENGINEERING.md 里的真跑数据"
    )


def test_distiller_and_assistant_keep_default_effort():
    """蒸馏/助理要「判断对错」⇒ 思考有实际价值 ⇒ **不传** effort（官方默认 max）。

    这两个角色走 `build_role_provider`，不经过写手那条路径。
    """
    import inspect

    from app.llm import provider as prov

    src = inspect.getsource(prov.build_role_provider)
    assert "reasoning_effort" not in src, (
        "蒸馏/助理角色不应被塞 effort —— 它们的思考有实际价值"
    )
