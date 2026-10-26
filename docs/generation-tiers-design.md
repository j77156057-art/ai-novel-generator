# 生成档位设计（花费 / 速度 视角）

> 状态：设计待审｜依据：本会话实测矩阵

## 0. 一个被推翻的前提

设计动机原本是「强弱模型差距这么大」。实测推翻了这个说法：

| 模型 | 一次生成 800 字 | 改写已有 800 字 | 提升 |
|---|---|---|---|
| 智谱 glm-4-flash | 句长 **22.4** | 句长 **47.8** | +114% |
| MiniMax-M3（关思考）| 句长 **18.1** | 句长 **35.6** | +97% |

**在「一次性生成」这个任务形态上，强弱模型的句长没有差距，弱模型甚至略高。**

真正的差距在别处：

| 维度 | 智谱 glm-4-flash | MiniMax-M3 |
|---|---|---|
| 生成速度 | 8s / 800 字 | 9s / 800 字 |
| 改写速度 | 12s | **4s** |
| 中文细节质量 | 一般 | **明显更好** |
| 失控风险 | 未见 | M2.1 出现过（74157 字复读）|
| 推理开销 | 无 | 关思考后无 |
| 成本 | 免费 | 按 token 计费 |

所以档位**不能按「模型强弱」定义**，只能按**花费与速度**定义——这也正是用户选定的方向。

## 1. 三档定义

| 档位 | 模型 | 文风改写 | 目标场景 | 实测成本/耗时（3000 字章） |
|---|---|---|---|---|
| **极速** | 智谱 glm-4-flash | 关 | 批量试跑、看情节走向 | 免费 / 约 1 分钟 |
| **平衡** | 智谱 glm-4-flash | 开 | 日常写作、字数优先 | 免费 / 约 1.5 分钟 |
| **高质量** | MiniMax-M3 | 开 | 定稿、值得反复读的章节 | 按 token / 约 5 分钟 |

关键说明：

- **极速与平衡用同一个模型**，只差改写开关。实测智谱改写后句长 47.8，比 M3 的 35.6 还高——所以「弱模型 + 改写」不是妥协，是真的可用。
- **高质量档的收益不在句长，在表达质量**：具体细节、动作层次、对白自然度。这需要人工读才能判断，指标测不出来。
- M3 的改写更快（4s vs 12s），但生成慢（思考不能全关时的开销）且有失控风险（同系列的 M2.1 出现过）。

## 2. 档位落地形态

### 2.1 不做「运行期自动切换模型」

理由：`build_provider` 是按 settings 里的 api_key/base_url/model 构造的，运行期切换模型需要重建 provider 实例，而 writer provider 是单例（`_PROVIDER`），重建会打断正在进行的长文生成。

改成：**档位=配置预设**。用户选档 → 写入 settings（provider/model/max_tokens/style_rewrite 四个字段）→ 下次构建 provider 时生效。这是现有 `settings` 机制的自然延伸，零架构改动。

### 2.2 数据结构

```python
# app/generation/tiers.py

GenerationTier = Literal["turbo", "balanced", "premium"]

TIER_REGISTRY = {
    "turbo": {
        "key": "turbo", "name": "极速",
        "description": "免费模型，不做文风改写。批量试跑、快速看情节走向。",
        "provider": "zhipu",
        "model": "glm-4-flash-250414",
        "max_tokens": 8192,
        "style_rewrite": False,
    },
    "balanced": {
        "key": "balanced", "name": "平衡",
        "description": "免费模型 + 文风改写（实测句长 22.4 → 47.8）。日常写作首选。",
        "provider": "zhipu",
        "model": "glm-4-flash-250414",
        "max_tokens": 8192,
        "style_rewrite": True,
    },
    "premium": {
        "key": "premium", "name": "高质量",
        "description": "MiniMax-M3 关思考 + 文风改写。表达细节明显更好，烧 token。",
        "provider": "minimax",
        "model": "MiniMax-M3",
        "max_tokens": 16000,
        "style_rewrite": True,
    },
}
```

### 2.3 生效方式

`GET /api/settings/generation-tiers` → 列出三档（前端选档用）
`POST /api/settings/generation-tier` `{"tier": "balanced"}` → 把该档的四个字段写进 settings

写入后由现有的 provider 重建机制生效（`reset_provider_cache()`）。

**不改 `generate-loop` 的请求体**——档位是"设置"，不是"每次请求参数"。这样避免用户在不知道会烧多少 token 的情况下随手传 premium。

## 3. 每档必须知道的真实代价

写进档位说明里，前端也要显示：

| 档位 | 每章文字成本（3000 字）| 每章耗时 | 质量特征 |
|---|---|---|---|
| 极速 | 免费 | 1 min | 情节完整，句子偏碎，AI 腔风险中 |
| 平衡 | 免费 | 1.5 min | 句子有节奏，但细节一般 |
| 高质量 | 约 3000 输出 token + 思考（实测 88% 额外）| 5 min | 细节丰富、动作有层次 |

## 4. 不做的事

- **不做自动降级**：某档失败就换档，会让用户不知道最终用的是哪个模型。失败就直接报错。
- **不做按段落选档**：中途换模型会破坏前后文风一致性。
- **不做档位影响蒸馏**：蒸馏（`narrative_tier` full/balanced/fast）是另一套档位，
  它按采样量分，与本档位正交。两者不要混在同一个选择器里。

## 5. 验收

| 项 | 标准 |
|---|---|
| 三档写入 settings | 选档后 `settings.llm_provider/model/writer_max_tokens` 与档位定义一致 |
| 极速档 | style_rewrite=0，生成 3000 字/章，约 1 min |
| 平衡档 | style_rewrite=1，句长 ≥ 40 |
| 高质量档 | provider=minimax，model=MiniMax-M3，思考关闭，句长 ≥ 40 |
| 未知档位 | 报错并列出可选值（不静默退回默认） |

## 6. 待确认

1. 档位名用「极速 / 平衡 / 高质量」还是「经济 / 标准 / 旗舰」？
2. 档位存在 `app_settings` 全局，还是允许每个 project 独立选档？
   （建议全局——档位反映用户的成本意愿，不是单本书的属性）
