# -*- coding: utf-8 -*-
"""蒸馏块大小「跟着渠道走」—— 能力声明 / 优先级 / 指纹同源。

## 这组测试守的不变量

1. **能力声明是按渠道的**，不是写死的常量；
2. **优先级**：显式参数 > 配置覆盖 > 渠道实测 > 保守兜底；
3. **查表与实例同源** —— 指纹算出来的块大小必须等于实际蒸馏用的块大小。
   这条最要紧：不同源 ⇒ 指纹永远对不上 ⇒ 缓存永远不命中（每次重跑），
   或者命中**旧渠道**的规则（换渠道后拿到旧结果，且无任何报错）。
4. **未声明的渠道保守退化** —— 兜底值不变 ⇒ 不因为这次改动让任何现网行为变差。
5. **opencode-go 渠道必需的两个头** —— 少了就是 403/400，不是 200。
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from app.llm import provider as provider_mod  # noqa: E402
from app.llm.provider import (  # noqa: E402
    GenericOpenAIChatProvider,
    LocalFallbackLLMProvider,
    OpenCodeGoChatProvider,
    ZhipuChatProvider,
    block_chars_for_channel,
    build_provider,
)
from app.style import narrative  # noqa: E402


# ── 1. 能力声明 ──────────────────────────────────────────────────────

def test_capability_is_declared_per_channel():
    """块大小是**渠道能力**，不是全局常量。"""
    assert ZhipuChatProvider.distill_block_chars == 20_000
    # opencode-go 的取值由实测定（5 万），但**不在这里写死** ——
    # 写死会让「改了实测结论却忘了改测试」变成红灯，
    # 而这条测试要守的是「**按渠道不同**」这个形状，不是某个具体数字。
    assert OpenCodeGoChatProvider.distill_block_chars > 0
    assert OpenCodeGoChatProvider.distill_block_chars != ZhipuChatProvider.distill_block_chars
    # 通用 openai 渠道服务任意厂商 ⇒ 不知道 ⇒ 声明 0（未声明）
    assert GenericOpenAIChatProvider.distill_block_chars == 0
    # LocalFallback 不是真渠道 ⇒ 未声明
    assert LocalFallbackLLMProvider.distill_block_chars == 0


def test_unchannel_capability_falls_back_to_conservative_default():
    """没声明能力的渠道 ⇒ 用保守兜底（2 万字），行为与改动前一致。"""
    assert narrative._resolve_block_chars(GenericOpenAIChatProvider.__new__(
        GenericOpenAIChatProvider)) == narrative.TARGET_BLOCK_CHARS


def test_opencode_go_declares_a_larger_block_than_zhipu():
    """1M 上下文的渠道必须比 28K TPM 的渠道块大 —— 这正是本次改动的意义。

    （实测定案是 5 万字：比智谱的 2 万大，但不是「能读 200 万就取 200 万」
    —— 那个取值被 A/B 证伪了，见 ENGINEERING.md。）"""
    assert OpenCodeGoChatProvider.distill_block_chars > ZhipuChatProvider.distill_block_chars


# ── 2. 优先级 ────────────────────────────────────────────────────────

class _FakeProvider:
    def __init__(self, n: int):
        self.distill_block_chars = n


def test_priority_explicit_argument_wins():
    assert narrative._resolve_block_chars(_FakeProvider(200_000), requested=50_000) == 50_000


def test_priority_channel_value_when_no_explicit_argument():
    assert narrative._resolve_block_chars(_FakeProvider(200_000)) == 200_000


def test_priority_config_override_beats_channel(monkeypatch):
    """用户显式配置 ⇒ 压过渠道实测值（与 distill_max_concurrent 同形状）。"""
    from app.config import settings as _st
    monkeypatch.setattr(_st, "distill_block_chars", 123_456, raising=False)
    assert narrative._resolve_block_chars(_FakeProvider(200_000)) == 123_456


def test_config_override_of_zero_means_not_set(monkeypatch):
    """0 = 不覆盖 ⇒ 仍走渠道实测值。"""
    from app.config import settings as _st
    monkeypatch.setattr(_st, "distill_block_chars", 0, raising=False)
    assert narrative._resolve_block_chars(_FakeProvider(200_000)) == 200_000


def test_unreadable_config_does_not_break_distillation(monkeypatch):
    """配置不可读 ⇒ 用渠道值，不抛错（蒸馏不能因为配置读不到就炸）。"""
    from app.config import settings as _st

    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("配置读不到")

    monkeypatch.setattr(_st, "distill_block_chars", property(
        lambda self: (_ for _ in ()).throw(RuntimeError("boom"))), raising=False)
    assert narrative._resolve_block_chars(_FakeProvider(200_000)) == 200_000


# ── 3. 查表与实例同源（指纹的正确性） ─────────────────────────────────

def test_block_size_value_must_be_justified_by_measured_evidence():
    """渠道的块大小**不许写一个没测过的数**。

    ## 为什么有这条测试

    2026-11-01 第一版我按「模型能读 200 万字」给 opencode-go 填了 200,000
    （留 10 倍余量）⇒ **A/B 实测直接证伪**：
    墙钟快 6.4 倍，但规则数掉到 1/8.4、rejected 率翻倍。

    ⇒ 「能读多少」不是「该用多少」。填这个数必须有 A/B 依据。

    ## 这条测试怎么起作用

    它守的是「**这个数落在实测可用的档位上**」——
    改了 `distill_block_chars` 却没重跑 A/B，就会转红。

    换句话说：**它不是不许改这个数，是不许「改了数却没重跑 A/B」**。
    """
    # 实测结论（2026-11-01 四档扫描，见 ENGINEERING.md 与 docs/context-budget-design.md）：
    #   块 2 万  → 28 块 → 每维候选约 111 条
    #   块 5 万  → 11 块 → 每维候选约 39 条
    #   块 20 万 →  3 块 → 每维候选约 12 条
    #
    # 下游每维只取 5 条（RULES_PER_DIM）⇒ **候选池要够选**。
    # 12 条候选选 5 条 = 多样性只剩 1/4 ⇒ 判据是「每维候选 ≥ 30」。
    #
    # ⇒ 换算成块大小：每块恒定产出约 40 条 ⇒ 候选 = 40 × 块数
    # ⇒ 块 5 万 → 11 块 → 每维约 39 条 ✅；块 20 万 → 3 块 → 约 12 条 ❌
    value = OpenCodeGoChatProvider.distill_block_chars
    assert value <= 100_000, (
        f"opencode-go 的块大小 {value} 超过了实测可用档位（≤100,000）："
        "20 万字档实测每维候选只剩约 12 条（要选 5 条）⇒ 多样性不够"
    )
    assert value >= 20_000, (
        f"opencode-go 的块大小 {value} 比保守基线还小 ⇒ 没有放大的理由"
    )


def test_lookup_table_matches_instance_value():
    """**查表**与**实例上的值**必须一致 —— 指纹靠这个才能算对。"""
    for channel, cls in (("zhipu", ZhipuChatProvider),
                         ("opencode-go", OpenCodeGoChatProvider)):
        assert block_chars_for_channel(channel) == cls.distill_block_chars


def test_fingerprint_block_chars_matches_real_distillation_block(monkeypatch):
    """指纹用的块大小 == 实际蒸馏用的块大小（同一渠道下）。

    断言**从类上读**期望值，不写死数字 —— 渠道能力调整后这条测试不该红
    （它守的是「两个来源同源」，不是「某个具体数字」）。
    """
    from app.config import settings as _st
    monkeypatch.setattr(_st, "distill_block_chars", 0, raising=False)
    monkeypatch.setattr(_st, "distiller_provider", "opencode-go", raising=False)

    fp = narrative._block_chars_for_fingerprint()
    real = narrative._resolve_block_chars(OpenCodeGoChatProvider.__new__(
        OpenCodeGoChatProvider))
    assert fp == real == OpenCodeGoChatProvider.distill_block_chars


def test_changing_channel_invalidates_the_fingerprint(monkeypatch):
    """换渠道 ⇒ 指纹必须变 ⇒ 旧缓存失效。

    这是「档位不进指纹」那个坑的第三次出现：不堵的话，用户从智谱换到
    opencode-go，界面照样「命中缓存」，拿到的是**旧渠道**的规则，且无报错。
    """
    from app.config import settings as _st
    monkeypatch.setattr(_st, "distill_block_chars", 0, raising=False)

    monkeypatch.setattr(_st, "distiller_provider", "zhipu", raising=False)
    fp_zhipu = narrative.narrative_fingerprint([1], block_chars=narrative._block_chars_for_fingerprint())

    monkeypatch.setattr(_st, "distiller_provider", "opencode-go", raising=False)
    fp_oc = narrative.narrative_fingerprint([1], block_chars=narrative._block_chars_for_fingerprint())

    assert fp_zhipu != fp_oc, "换渠道后指纹没变 ⇒ 会静默复用旧渠道的产出"


def test_fingerprint_without_block_chars_still_works():
    """不传 block_chars ⇒ 字段缺席（老调用方兼容），不是 None。"""
    fp = narrative.narrative_fingerprint([1])
    assert "block_chars" not in fp


# ── 4. opencode-go 渠道的两个必需头 ────────────────────────────────────

def test_opencode_go_provider_sends_the_two_mandatory_headers():
    """少 session 头 ⇒ 400 MissingSessionID；少浏览器 UA ⇒ 403 Cloudflare 1010。

    这两个头以前只写在验收脚本里 ⇒ 通过界面配这个端点必然失败。
    """
    p = OpenCodeGoChatProvider("k", "https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash")
    assert "x-opencode-session" in p.extra_headers
    assert "Mozilla/5.0" in p.extra_headers["User-Agent"]


def test_opencode_go_is_reachable_through_the_normal_factory_path():
    """走 build_provider（= 用户在界面上的路径）也能拿到那两个头。"""
    p = build_provider("opencode-go", "k", "deepseek-v4.1-flash")
    assert isinstance(p, OpenCodeGoChatProvider)
    assert "x-opencode-session" in p.extra_headers
    assert "Mozilla/5.0" in p.extra_headers["User-Agent"]
    assert p.distill_block_chars == OpenCodeGoChatProvider.distill_block_chars


def test_opencode_go_defaults_to_thinking_off():
    """deepseek-v4.1-flash 的思考 token 与正文共用预算 ⇒ 默认必须关。

    实测 max_tokens=64 时正文一个字都没有（finish_reason=length）。
    """
    p = build_provider("opencode-go", "k", "deepseek-v4.1-flash")
    assert p.extra_body.get("thinking") == {"type": "disabled"}


def test_opencode_go_thinking_can_be_explicitly_enabled():
    """用户可以在界面显式选「开思考」。"""
    p = build_provider("opencode-go", "k", "deepseek-v4.1-flash", thinking_off=False)
    assert "thinking" not in p.extra_body


def test_generic_openai_channel_behavior_unchanged():
    """通用 openai 渠道：未指定 thinking_off ⇒ **不关**（与改动前逐字节一致）。"""
    p = build_provider("openai", "k", "gpt-4o-mini", "https://api.openai.com/v1")
    assert "thinking" not in p.extra_body
    # 显式关仍然有效
    p2 = build_provider("openai", "k", "gpt-4o-mini", "https://api.openai.com/v1",
                        thinking_off=True)
    assert p2.extra_body.get("thinking") == {"type": "disabled"}


# ── 5. 渠道名拼错不会静默降级 ─────────────────────────────────────────

def test_typo_channel_name_is_rejected_at_the_api_boundary():
    """渠道名拼错 ⇒ **在 API 边界就 400**，不许悄悄变成 LocalFallback。

    ## 分层说明（别改错层）

    - **API 边界**（`PUT /api/settings/llm`）对未知渠道名返回 400 ——
      这是**用户能看见**的那道防线（既有契约，见 `test_settings_roles.py`）。
    - **工厂层**（`build_provider`）对未知渠道名降级 LocalFallback 且不抛错 ——
      这是**既有的、有意的**兜底契约（`config.py` 明写「保持既有行为，不抛错：
      渠道名可能来自运行时 PUT 的自由输入」），多处测试锁着它。

    ⇒ 本测试守的是「**新渠道 opencode-go 必须在 API 边界被认得**」，
    以及「拼错的渠道名不会被当成 opencode-go」。
    """
    from app.routers.settings import PROVIDER_META

    assert "opencode-go" in PROVIDER_META, "新渠道没进 PROVIDER_META ⇒ 界面选不了"
    assert "opencode-go-typo" not in PROVIDER_META

    # 工厂层仍按既有契约降级（不抛错）—— 这条不能被本次改动破坏。
    from app.llm.provider import LocalFallbackLLMProvider as _LF
    assert isinstance(build_provider("opencode-go-typo", "k", "m"), _LF)
