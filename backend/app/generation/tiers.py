"""生成档位：按「花费 / 速度」划分的三档配置预设。

## 为什么按花费/速度分，而不是按「模型强弱」

实测（本会话，同一 800 字任务）：

    模型                一次生成    改写已有    提升
    智谱 glm-4-flash    22.4       47.8       +114%
    MiniMax-M3          18.1       35.6       +97%

**在「一次性生成」这个任务形态上，弱模型的句长反而更高。** 强弱差距不在句长，
在表达细节质量、改写速度、失控风险。

所以档位只能按花费与速度定义，否则会误导用户「选强模型句子就更长」。

## 三档

    turbo     极速    免费模型，不改写        批量试跑、快速看情节
    balanced  平衡    免费模型 + 改写         日常写作首选（实测 22.4 → 47.8）
    premium   高质量  M3.1-Flash + 改写       定稿；对白形态几乎贴住参考书

**极速与平衡用同一个模型，只差改写开关。** 这不是偷懒——实测智谱改写后句长 47.8，
比 M3 的 35.6 还高。弱模型 + 改写是真的可用。

**改写现在是「闸门」触发**（偏离参考书越远的那段才改），所以三档的改写开销
都随实际偏离浮动 —— 均衡偏离时改写这一段基本不发生。

## 为什么是「配置预设」而不是「运行期切换」

`build_provider` 按 settings 里的 api_key / base_url / model 构造实例，而 writer
provider 是单例（`_PROVIDER`）。运行期换模型要重建实例，会打断正在进行的长文生成。

所以档位的生效方式是：**选档 → 写入 settings 四个字段 → 下次构建 provider 时生效**。
零架构改动，是现有 settings 机制的自然延伸。

## 为什么档位不放进 generate-loop 请求体

它是「设置」不是「每次请求的参数」。放进请求体，用户会在不知道烧多少 token 的
情况下随手传 premium。档位变更应当是一次显式、可预期的操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: 档位 key
GenerationTierKey = Literal["turbo", "balanced", "premium"]

#: 默认档：平衡。免费 + 改写，是「能用」与「不花钱」的平衡点。
DEFAULT_GENERATION_TIER: str = "balanced"

TIER_TURBO = "turbo"
TIER_BALANCED = "balanced"
TIER_PREMIUM = "premium"


@dataclass(frozen=True)
class GenerationTier:
    """一个档位的完整定义。字段名与 settings 一一对应，便于直接写入。"""

    key: str
    name: str
    description: str
    #: 写入 settings 的四个字段
    provider: str
    model: str
    max_tokens: int
    style_rewrite: bool
    #: 展示用的代价说明（前端要显示，否则用户不知道选了什么）
    cost_hint: str
    speed_hint: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "style_rewrite": self.style_rewrite,
            "cost_hint": self.cost_hint,
            "speed_hint": self.speed_hint,
        }


TIER_REGISTRY: dict[str, GenerationTier] = {
    TIER_TURBO: GenerationTier(
        key=TIER_TURBO,
        name="极速",
        description="免费模型，不做文风改写。批量试跑、快速看情节走向。",
        provider="zhipu",
        model="glm-4-flash-250414",
        max_tokens=8192,
        style_rewrite=False,
        cost_hint="免费",
        speed_hint="约 1 分钟 / 3000 字章",
    ),
    TIER_BALANCED: GenerationTier(
        key=TIER_BALANCED,
        name="平衡",
        description=(
            "免费模型 + 文风改写（同一段实测句长 22.4 → 47.8）。"
            "句子有节奏，细节一般，日常写作首选。"
        ),
        provider="zhipu",
        model="glm-4-flash-250414",
        max_tokens=8192,
        style_rewrite=True,
        cost_hint="免费",
        speed_hint="约 1.5 分钟 / 3000 字章（偏离不大时不做改写，更快）",
    ),
    TIER_PREMIUM: GenerationTier(
        key=TIER_PREMIUM,
        name="高质量",
        description=(
            "MiniMax-M3.1-Flash + 文风改写。对白形态几乎贴住参考书"
            "（纯台词段 27.7% vs 参考 20.8%；M3 是 58.1%），"
            "细节也更足。代价是单段慢 4~5 倍（它的思考关不掉）。"
        ),
        provider="minimax",
        model="MiniMax-M3.1-Flash",
        # adaptive thinking：思考 token 算进 completion 预算 → 32K 起步
        # （实测写 2400 字正文用掉 111.8s；8192 会让正文被思考挤掉、被截断）
        max_tokens=32768,
        style_rewrite=True,
        cost_hint="按 token 计费（每章约 3000 输出 + 改写开销 + 思考 token）",
        speed_hint="约 2~3 分钟 / 3000 字章（M3 约 25 秒，慢 4~5 倍）",
    ),
}


def resolve_tier(key: str | None) -> GenerationTier:
    """把档位 key 解析成配置对象。

    key 为 None / 空串 → 默认档。
    **未知值抛错，不静默退回默认**——用户传错档位名却悄悄用免费模型，
    比直接报错更糟：他会以为自己在用高质量档。
    """
    if key is None or not str(key).strip():
        return TIER_REGISTRY[DEFAULT_GENERATION_TIER]
    k = str(key).strip().lower()
    if k not in TIER_REGISTRY:
        valid = "、".join(f"{t.key}（{t.name}）" for t in
                          (TIER_REGISTRY[TIER_TURBO], TIER_REGISTRY[TIER_BALANCED],
                           TIER_REGISTRY[TIER_PREMIUM]))
        raise ValueError(f"未知生成档位 {key!r}，可选：{valid}")
    return TIER_REGISTRY[k]


def list_tiers() -> list[dict[str, Any]]:
    """按「极速 → 平衡 → 高质量」的顺序列出全部档位（前端选档用）。"""
    order = [TIER_TURBO, TIER_BALANCED, TIER_PREMIUM]
    return [TIER_REGISTRY[k].to_public_dict() for k in order if k in TIER_REGISTRY]


def tier_settings_patch(tier: GenerationTier) -> dict[str, Any]:
    """把档位翻译成 settings 的字段补丁。

    `writer_max_tokens` 而不是 `max_tokens`：这是 settings 里 writer 路径的字段名，
    与 distiller / assistant 的 max_tokens 是分开的，不能混。
    """
    return {
        "llm_provider": tier.provider,
        "writer_max_tokens": tier.max_tokens,
        # 各渠道的模型名分别落到自己字段——settings 里渠道间模型名不共享
        f"{tier.provider}_model": tier.model,
        # 档位的核心差异之一：极速档不改写、平衡/高质量档改写。
        # 写全局默认（settings.style_rewrite）；项目显式设过 projects.style_rewrite
        # 时以项目为准，全局值只影响「没显式选过的项目」。
        "style_rewrite": tier.style_rewrite,
    }
