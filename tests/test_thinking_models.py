"""「模型 → 思考行为」实测表：能力、预算地板、以及**未验证**这一档的语义。

## 为什么要有这个文件

2026-11-02 之前，「哪个模型关得掉思考」「预算地板给多少」「界面给哪些档位」
三件事分别写在 `llm/provider.py`、`routers/settings.py`、`config/providers.yaml`。
新加渠道漏一处是**静默失效**：要么界面上有个按了没反应的开关，
要么正文被思考吃光预算变成空串（实测 max_tokens=64 时正文 0 字）。

⇒ 收成一张表（`app/llm/thinking_models.py`），本文件钉住四条：

1. **两件事是两个轴**：「能不能关思考」与「思考是否与正文共用预算」可以证据不同。
   `deepseek-v4.1-flash` 就是这种：能不能关**未实测**，但共预算**实测过**。
   合成一档必然错一边 —— 要么给它错误的预算，要么把实测证据丢掉。
2. **没实测过就不给开关，但要说出来**（`verification="unverified"`），
   而不是悄悄消失让人以为平台忘了做。
3. **不知道它思不思考的模型不能拿预算地板** —— 给它套 32768 等于拿别人的渠道付代价。
4. **前缀匹配必须最长优先**：`MiniMax-M3.1` 以 `MiniMax-M3` 开头，
   按顺序匹配会把 M3.1 判成老 M3（这两种模型的规则完全相反）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.llm.provider import (  # noqa: E402
    MiniMaxChatProvider,
    OpenCodeGoChatProvider,
    build_provider,
)
from app.llm.thinking_models import (  # noqa: E402
    DEFAULT_UNVERIFIED,
    FLOOR_FULL,
    FLOOR_THINKS_LESS,
    TABLE,
    UNVERIFIED,
    budget_floor,
    lookup,
    thinking_levels,
    thinking_mode,
)
from app.routers.settings import thinking_control_for  # noqa: E402


# ── 1. 前缀匹配：最长优先 ─────────────────────────────────────────
def test_longest_prefix_wins_m31_is_not_mistaken_for_legacy_m3():
    """`MiniMax-M3.1-Flash` **以** `MiniMax-M3` 开头 —— 不能因此判成老 M3。

    两者规则完全相反：M3.1 关不掉（只能调档）；老 M3 不认 effort（只能关）。
    判错的表现是「界面给了 low/medium/high，但传下去毫无反应」。
    """
    assert lookup("minimax", "MiniMax-M3.1-Flash").model_prefix == "MiniMax-M3.1"
    assert lookup("minimax", "MiniMax-M3.1-Flash-Preview").model_prefix == "MiniMax-M3.1"
    assert lookup("minimax", "MiniMax-M3").model_prefix == "MiniMax-M3"
    # 两种模型的档位不是同一套
    assert "low" in thinking_levels("minimax", "MiniMax-M3.1-Flash")
    assert "low" not in thinking_levels("minimax", "MiniMax-M3")


def test_channel_filter_comes_first():
    """渠道先过滤：同一个模型名在不同渠道下可以有不同的行。"""
    # deepseek 系只在 openai 渠道有行；换个渠道就是未验证
    assert lookup("openai", "deepseek-flash").thinking != UNVERIFIED
    assert lookup("zhipu", "deepseek-flash").thinking == UNVERIFIED


# ── 2. 「未验证」这一档的语义 ─────────────────────────────────────
def test_opencode_default_model_offers_no_switch_but_says_unverified():
    """`deepseek-v4.1-flash`：不给开关，但**必须说清是「未实测」**。

    2026-11-02 拍板：「先按『未验证 ⇒ 不给开关』处理」。
    这条钉住两件事 —— 不给开关（levels 为空），且 verification 明确写成 unverified
    （而不是让它看起来像「这个模型本来就不思考」）。
    """
    c = thinking_control_for("opencode-go", "deepseek-v4.1-flash")
    assert c["mode"] == "none"
    assert c["levels"] == ()
    assert c["verification"] == "unverified", (
        "未实测的模型必须标成 unverified —— 否则界面上分不清"
        "「模型本来不思考」与「平台没做完」"
    )
    # 但要说明「已知它共预算」这件事（用户据此知道预算为什么要给足）
    assert "共用" in c["note"], c["note"]


def test_unverified_offers_zero_levels_and_sends_nothing():
    """未验证 ⇒ 一个字段都不发（不是「发了个没人认的字段」）。"""
    assert thinking_levels("opencode-go", "deepseek-v4.1-flash") == ()
    assert thinking_mode("opencode-go", "deepseek-v4.1-flash") == "none"
    assert lookup("opencode-go", "deepseek-v4.1-flash").thinking == UNVERIFIED


def test_verified_models_do_offer_levels():
    """对照：实测过的模型照常给档位（证明上一条不是「全都不给」）。"""
    assert thinking_levels("openai", "deepseek-flash") == ("default", "off")
    assert thinking_control_for("openai", "deepseek-flash")["verification"] == "verified"


def test_unknown_channel_is_unverified_not_no_thinking():
    """表里没有的渠道：**不知道**它思不思考，不能断言「它不思考」。"""
    row = lookup("siliconflow", "some-model")
    assert row is DEFAULT_UNVERIFIED
    assert row.thinking == UNVERIFIED


# ── 3. 预算地板：只给「知道共预算」的模型 ─────────────────────────
def test_no_floor_for_models_we_know_nothing_about():
    """智谱 / GPT / 表里没有的模型 **不给地板**。

    这一条最要紧：给一个「可能是思考型」的模型套 32768，
    会把所有既有请求的预算抬到 4 倍 —— 那是拿别人的渠道去付代价。
    """
    for prov, model in (("zhipu", "glm-4-flash-250414"),
                        ("openai", "gpt-4o-mini"),
                        ("siliconflow", "qwen-max"),
                        ("minimax", "")):
        assert budget_floor(prov, model) is None, (prov, model)


def test_floor_applies_where_budget_is_known_to_be_shared():
    """实测共预算的模型：该给地板。"""
    # 未验证但共预算 ⇒ 最坏情况
    assert budget_floor("opencode-go", "deepseek-v4.1-flash") == FLOOR_FULL
    # 能关但没关 ⇒ 实测够用那一档
    assert budget_floor("openai", "deepseek-flash") == FLOOR_THINKS_LESS


def test_explicitly_turned_off_needs_no_floor():
    """显式关掉了 ⇒ 思考不占预算 ⇒ 不需要地板。"""
    for prov, model in (("opencode-go", "deepseek-v4.1-flash"),
                        ("minimax", "MiniMax-M3.1-Flash"),
                        ("openai", "deepseek-v4-pro")):
        assert budget_floor(prov, model, thinking_off=True) is None, (prov, model)


def test_effort_only_lowers_the_floor_when_it_actually_reduces_thinking():
    """判据是「这个档位把思考压下来了吗」，不是「有没有发 effort」。

    2026-10-26 补测推翻过一版：xhigh **也发 effort**，思考却照旧吃预算
    （8192 下正文 0 字）。⇒ 只有 low/medium/high 才降地板。
    """
    for ok in ("low", "medium", "high"):
        assert budget_floor("minimax", "MiniMax-M3.1-Flash", effort=ok) == FLOOR_THINKS_LESS, ok
    for bad in ("xhigh", "max", "", None):
        assert budget_floor("minimax", "MiniMax-M3.1-Flash", effort=bad) == FLOOR_FULL, bad


# ── 4. 到 provider 实例为止（不是只算得对，得真的生效）───────────
def test_m31_floor_lands_on_the_instance():
    """M3.1 + low ⇒ 实例上是 8192/180（这条与既有 test_minimax_reasoning_effort 同源）。"""
    p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash",
                            reasoning_effort="low")
    assert p.max_tokens == FLOOR_THINKS_LESS[0]
    assert p.timeout == FLOOR_THINKS_LESS[1]


def test_opencode_floor_only_when_thinking_is_on():
    """opencode-go：默认关思考 ⇒ 不加预算（行为不变）；显式开思考 ⇒ 抬到最坏情况。

    前者守住「不改变默认路径」；后者是新加的保护 ——
    否则用户一开思考就会拿到「正文 0 字」（实测症状）。
    """
    default = build_provider("opencode-go", "k", "deepseek-v4.1-flash")
    assert default.extra_body.get("thinking") == {"type": "disabled"}
    assert default.max_tokens is None, "默认关思考时不该动预算（既有行为）"

    on = build_provider("opencode-go", "k", "deepseek-v4.1-flash", thinking_off=False)
    assert "thinking" not in on.extra_body
    assert on.max_tokens == FLOOR_FULL[0]
    assert on.timeout == FLOOR_FULL[1]


def test_opencode_is_still_the_same_class():
    """表只是「知识」，不改变渠道实现 —— 别把 factory 改出另一个类。"""
    assert isinstance(build_provider("opencode-go", "k", "deepseek-v4.1-flash"),
                      OpenCodeGoChatProvider)


# ── 5. 表本身的自洽（防止加行时写漏或写矛盾）──────────────────────
def test_every_row_states_its_evidence():
    """每一行都要带**日期 + 依据** —— 没有依据就不该写成「已验证」。

    判据是「有一行可追溯的记录」，不是「含某个词」：
    写成「实测」两个字却没日期，等于没依据。
    """
    import re
    for row in TABLE:
        assert row.evidence.strip(), f"{row.provider}/{row.model_prefix} 缺实测依据"
        assert re.search(r"20\d{2}-\d{2}-\d{2}", row.evidence), (
            f"{row.provider}/{row.model_prefix} 的依据没写日期：{row.evidence}"
        )


def test_rows_with_levels_declare_a_control_mode():
    """给了档位就必须有一个模式；没档位就不能有模式。"""
    for row in TABLE:
        mode = thinking_mode(row.provider, row.model_prefix)
        if row.levels:
            assert mode in ("effort", "disable"), (row.provider, row.model_prefix, mode)
        else:
            assert mode == "none", (row.provider, row.model_prefix, mode)
