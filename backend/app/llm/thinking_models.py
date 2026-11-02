"""「模型 → 思考行为」实测表：**一处回答三个问题**。

## 为什么要一张表（2026-11-02 拍板：B1）

在这之前，「这个模型是不是思考型的」「能不能把思考关掉」「预算地板给多少」
三件事分别写在三个地方：

| 事实 | 原先在哪 |
|---|---|
| 关不掉，只能调 `reasoning_effort`；预算地板分两档 | `llm/provider.py` 的 `MiniMaxChatProvider.__init__` |
| 界面给哪些档位、哪些模型有开关 | `routers/settings.py` 的 `THINKING_CONTROL` / `thinking_control_for()` |
| 写手默认 `max_tokens=32768` / `timeout=300` | `config/providers.yaml` + `config.py` |

后果：新加一个渠道时只能靠人记得**三处都改**。漏一处的表现是静默的 ——
要么界面上有一个按了没反应的开关，要么正文被思考吃光预算而变成空串。

⇒ 现在把它们收成这张表；上面三处都**读表**，不再各写一份判断。

## 表里放什么、不放什么

放：**实测过**的结论（每条都带依据与日期）。不放：厂商文档的推测。

实测过的才允许写「关得掉 / 关不掉」；**没实测过的一律 `UNVERIFIED`**，
界面上明说「这个模型没有思考开关（未验证）」，而不是把选项藏起来让人猜。

## 为什么「未验证」要显式存在，而不是干脆不给

不给开关（`mode="none"`）与「给了开关但没反应」之间还有一个坑：
**用户不知道是「模型不支持」还是「平台忘了做」**。`UNVERIFIED` 这一档
把这件事说出来 —— 界面上看得见、语义明确、并且**不会真的发字段**。
"""

from __future__ import annotations

from typing import NamedTuple

#: 思考行为分档（表里每一行的 `thinking` 字段取这些值之一）。
#:
#: - `THINK_OFF_CAPABLE`：能关掉（发 `thinking={"type":"disabled"}`）
#: - `THINK_TUNABLE`：关不掉，只能调档（发 `reasoning_effort`）
#: - `THINK_FORCED`：强制思考，连档位都调不动（只能靠加大预算）
#: - `NO_THINKING`：本来就不思考
#: - `UNVERIFIED`：**没实测过** —— 不给开关，但要在界面上说清楚
THINK_OFF_CAPABLE = "off_capable"
THINK_TUNABLE = "tunable"
THINK_FORCED = "forced"
NO_THINKING = "no_thinking"
UNVERIFIED = "unverified"

#: 预算地板：思考型模型要留够输出预算，否则正文会被思考挤成空串。
#:
#: 两组数字都来自真跑，不是拍的：
#: - `thinks_less`（思考被压下来的档位）：8192 / 180s。
#:   实测 effort=low 写 2400 字正文：36.0 秒、completion 2605、正文完整 ⇒ 绰绰有余。
#: - `full`（思考没被压下来）：32768 / 300s。
#:   实测 xhigh + 8192 ⇒ reasoning 8190、completion 8192 打满、finish=length、**正文 0 字**；
#:   省略档位（=max）+ 32768 ⇒ 超过 300 秒仍未返回。
FLOOR_THINKS_LESS: tuple[int, int] = (8192, 180)
FLOOR_FULL: tuple[int, int] = (32768, 300)


class ModelThinking(NamedTuple):
    """表里的一行。"""

    #: 渠道名（与 `PROVIDER_META` 的键同一套写法）。
    provider: str
    #: 模型名前缀（按 `startswith` 匹配；空串 = 该渠道的兜底行）。
    model_prefix: str
    #: 思考行为分档，取值见上面的常量。
    thinking: str
    #: ⚠️ **另一个轴**：思考 token 是否与正文共用同一份 `max_tokens` 预算。
    #:
    #: 为什么必须与 `thinking` 分开：这两件事的实测证据可以不同。
    #: 典型例子是 `deepseek-v4.1-flash` ——
    #: 「它是不是思考型 / 能不能关」**未实测**，但「共预算」有硬证据
    #: （max_tokens=64 ⇒ 正文 0 字、finish_reason=length）。
    #: 合成一档的话，要么给它错误的预算地板，要么把「共预算」这条实测丢掉。
    #:
    #: 反过来也成立：不知道它思不思考的模型（智谱、GPT）**不能**因此拿到
    #: 32768 的地板 —— 那会把所有既有请求的预算抬到 4 倍。
    budget_shared_with_thinking: bool
    #: 这个模型在**已经把思考压下来**时，预算地板够不够用。
    #: True ⇒ 地板可以降到 `FLOOR_THINKS_LESS`。
    thinks_less_when_configured: bool
    #: 界面可给的档位（不含「不改变」）。
    levels: tuple[str, ...]
    #: 给人看的说明（会直接显示在界面上）。
    note: str
    #: 实测依据：日期 + 一句话。**没有实测依据的必须写「未验证」**。
    evidence: str


#: 实测表。新加渠道/模型时**只改这里**。
#:
#: 排序规则：更具体的（模型名前缀更长）先匹配 —— 见 `lookup()`。
TABLE: tuple[ModelThinking, ...] = (
    # ── MiniMax ────────────────────────────────────────────────────
    ModelThinking(
        provider="minimax",
        model_prefix="MiniMax-M3.1",
        thinking=THINK_TUNABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        # 只给**实测能写出正文**的三档：xhigh / max 在写手场景写不出正文
        # （xhigh：reasoning 8190 把 8192 预算吃满，finish=length，正文 0 字；
        #   max：>300s 未返回）⇒ 不提供。
        levels=("low", "medium", "high"),
        note="关不掉思考，只能调档。实测三档形态差不多；medium 快约 25%，low 最省思考 token。"
             "默认档（不选）= 最高档，写手场景会写不出正文，所以不提供那两档",
        evidence="2026-10-26 两轮共 15 次调用：low 7/7、medium 6/7、high 6/6；"
                 "xhigh 2/3（其中 1 次正文 0 字）、max 0/2（读超时）",
    ),
    ModelThinking(
        provider="minimax",
        model_prefix="MiniMax-M3",
        thinking=THINK_OFF_CAPABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        levels=("off",),
        note="老 M3 只能关思考（它不认 effort，默认就关着）",
        evidence="2026-10-26 实测：传 effort=low 反而更慢（22.8s vs 19.9s）；"
                 "关思考 15s 且句长更好（60.2 vs 49.2）",
    ),
    # ── opencode Go 订阅通道 ───────────────────────────────────────
    ModelThinking(
        provider="opencode-go",
        model_prefix="deepseek-v4.1-flash",
        # ⚠️ 关键一条：**没实测过它能不能关**。
        # 渠道层默认关思考（OpenCodeGoChatProvider 的 thinking_off 默认 True），
        # 那条默认值是**照上个模型的实测搬过来的**，不是这个模型自己的实测。
        thinking=UNVERIFIED,
        # 但「思考 token 与正文共用预算」这一条有实测（max_tokens=64 ⇒ 正文 0 字），
        # 所以预算地板必须按思考型给 —— 这一项不能因为「未验证」就放松。
        budget_shared_with_thinking=True,
        thinks_less_when_configured=False,
        levels=(),
        note="这个模型没有思考开关（未实测关不关得掉）。"
             "已知：它的思考 token 与正文共用同一份 max_tokens 预算，"
             "预算太小会让正文一个字都没有 —— 所以写手预算按思考型给足",
        evidence="2026-11-01 实测：max_tokens=64 时 finish_reason=length、正文 0 字；"
                 "「能不能显式关掉」**未实测**",
    ),
    # ── 通用 OpenAI 兼容渠道（按模型名认厂商）────────────────────
    ModelThinking(
        provider="openai",
        model_prefix="deepseek-v4-pro",
        thinking=THINK_OFF_CAPABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        levels=("default", "off"),
        note="建议保持默认：实测关掉几乎不省时间（64.6s→63.9s），"
             "而它每篇只有 1~13 段对白、平均 173s，最长 291s",
        evidence="2026-10-26 真跑（2400 字续写，两种模型各两组）+ 6 篇复测",
    ),
    ModelThinking(
        provider="openai",
        model_prefix="deepseek-chat",
        thinking=THINK_OFF_CAPABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        levels=("default", "off"),
        note="可关（与 deepseek-flash 同族，实测结论一致）",
        evidence="2026-10-26 实测：thinking.disabled 生效、reasoning 归零",
    ),
    ModelThinking(
        provider="openai",
        model_prefix="deepseek-reasoner",
        thinking=THINK_OFF_CAPABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        levels=("default", "off"),
        note="可关（与 deepseek-chat 同族，实测结论一致）",
        evidence="2026-10-26 实测：thinking.disabled 生效、reasoning 归零",
    ),
    ModelThinking(
        provider="openai",
        model_prefix="deepseek-flash",
        thinking=THINK_OFF_CAPABLE,
        budget_shared_with_thinking=True,
        thinks_less_when_configured=True,
        levels=("default", "off"),
        note="可关：实测快 28%（25.8s→18.6s），对白形态基本不变",
        evidence="2026-10-26 真跑（2400 字续写）：25.8s → 18.6s，reasoning 87 → 0",
    ),
)

#: 表里没有的渠道/模型 ⇒ 这个（不给开关，但界面上会说明「未验证」）。
#:
#: 为什么不直接返回 `NO_THINKING`：那是在断言「它不思考」，而这件事我们并不知道。
#:
#: ⚠️ `budget_shared_with_thinking=False` 是关键：**不知道就不要加预算地板**。
#: 给一个「可能是思考型」的模型套 32768 地板，会把所有既有请求的预算抬到 4 倍 ——
#: 那不是保守，那是拿别人的渠道去付代价。
DEFAULT_UNVERIFIED = ModelThinking(
    provider="*",
    model_prefix="",
    thinking=UNVERIFIED,
    budget_shared_with_thinking=False,
    thinks_less_when_configured=False,
    levels=(),
    note="",
    evidence="",
)


def lookup(provider: str, model: str = "") -> ModelThinking:
    """查表：先按渠道过滤，再按**模型名前缀最长**优先匹配。

    为什么是「最长前缀」而不是「表里的顺序」：`MiniMax-M3.1` 与 `MiniMax-M3`
    是前缀包含关系（M3.1 以 M3 开头），按顺序匹配会把 M3.1 判成老 M3 ——
    那正是「同一渠道不同模型规则不同」最容易踩的那一脚。
    """
    prov = (provider or "").strip().lower()
    name = (model or "").strip()

    candidates = [r for r in TABLE if r.provider == prov]
    if not candidates:
        return DEFAULT_UNVERIFIED

    matched = [r for r in candidates
               if name.startswith(r.model_prefix) if r.model_prefix]
    if matched:
        # 最长前缀优先（M3.1 胜过 M3）
        return max(matched, key=lambda r: len(r.model_prefix))

    # 该渠道有表但模型没匹配上：看有没有「空前缀」的兜底行
    for r in candidates:
        if not r.model_prefix:
            return r
    return DEFAULT_UNVERIFIED


def thinking_levels(provider: str, model: str = "") -> tuple[str, ...]:
    """界面可给的档位。空 tuple = 这个模型不给思考开关。"""
    return lookup(provider, model).levels


def thinking_mode(provider: str, model: str = "") -> str:
    """档位控制方式，给界面的 `mode` 字段用。

    - `THINK_TUNABLE` ⇒ `"effort"`（只能调档）
    - `THINK_OFF_CAPABLE` ⇒ `"disable"`（能开关）
    - 其它 ⇒ `"none"`（不发任何字段）
    """
    t = lookup(provider, model).thinking
    if t == THINK_TUNABLE:
        return "effort"
    if t == THINK_OFF_CAPABLE:
        return "disable"
    return "none"


def budget_floor(provider: str, model: str = "", *,
                 thinking_off: bool | None = None,
                 effort: str | None = None) -> tuple[int, int] | None:
    """写手的预算地板 `(max_tokens, timeout)`；`None` = 不需要地板。

    ## 判据是「思考有没有在吃正文的预算」，不是「这个模型叫不叫思考型」

    按顺序判：

    1. **显式关掉了**（`thinking_off=True`）⇒ 不需要地板（思考不占预算）；
    2. **不知道它的预算与思考共不共用** ⇒ **不给地板**。
       表里 `budget_shared_with_thinking=False` 的（智谱、GPT、表里没有的模型）
       全走这一条 —— 给它们套 32768 会把既有请求的预算抬到 4 倍，
       那不是保守，那是拿别人的渠道去付代价；
    3. 共预算 + 思考被压下来的档位（`thinks_less_when_configured` 且
       effort 在 low/medium/high）⇒ `FLOOR_THINKS_LESS`；
    4. 其余共预算的情形 ⇒ `FLOOR_FULL`。

    第 4 条对 `deepseek-v4.1-flash` 尤其要紧：它「能不能关」未实测，
    但「思考与正文共用预算」是实测过的 ⇒ 按最坏情况给。

    ## 一条被推翻过的旧判据（留在这，别再走回去）

    2026-10-26 之前是「发了 effort ⇒ 地板降一档」。错在 xhigh 也发 effort，
    但思考照旧吃预算（8192 下正文 0 字）。⇒ 判据必须是
    「**这个档位把思考压下来了吗**」，不是「有没有发这个字段」。
    """
    row = lookup(provider, model)

    if thinking_off:
        return None
    if not row.budget_shared_with_thinking:
        return None
    if row.thinking == NO_THINKING:
        return None
    if row.thinking == THINK_TUNABLE:
        effort_value = (effort or "").strip().lower()
        # 档位把思考压下来了才算「思考少」
        if row.thinks_less_when_configured and effort_value in ("low", "medium", "high"):
            return FLOOR_THINKS_LESS
        return FLOOR_FULL
    if row.thinking == THINK_OFF_CAPABLE:
        # 能关但**没关** ⇒ 它还在思考，但实测预算够用（DeepSeek/老 M3 都测过）
        return FLOOR_THINKS_LESS
    # THINK_FORCED / UNVERIFIED：按最坏情况
    return FLOOR_FULL
