# 智谱双版本抽取质量对比（候选 #5 转向版）

> 2026-10-16。目标：原候选 #5 是「nemotron 复跑对比智谱弱模型抽取质量」，
> nemotron 3 款全超时 + 7 款 OpenRouter free 模型全 404 后，转向「同 provider
> 不同版对比」——glm-4-flash（默认）vs glm-4-flash-250414（更新版）。
> 完整 raw 数据归档：`docs/journey_glm_versions_compare.json`

## 一、为什么转向 nemotron 路径

skill 沉淀的 4 款 nemotron 在 2026-10-16 实测：

| 模型 | skill 沉淀速度 | 实测 | 结果 |
|---|---|---|---|
| nvidia/nemotron-3-super-120b-a12b:free | ~5s | 60s 超时 | NoneType（HTTP 200 但 choices 空） |
| nvidia/nemotron-3-ultra-550b-a55b:free | ~22s | 120s 超时 | read timed out |
| nvidia/nemotron-3.5-lightning:free | ~6s | 60s 超时 | read timed out |
| apodex/apodex-1.1-mini:free | ~2.5s | — | 404 No endpoints found |

加上 7 款备选 OpenRouter free 模型（qwen 2.5 72b / gemini flash / llama 3.3 /
mistral small / deepseek v3 / gpt-oss 20b / hermes 405b）——**全部 404**。
OpenRouter 2026-10 几乎下架了 free 档，nemotron 3 款自身在 60-120s 全超时。
nemotron 路径客观条件已无法完成。

**智谱双版对比**仍能达成原候选目的（质量差异观察），且都 free 档无成本。

## 二、对比设计

- **同一段 1200 字测试文本**（林悠然 + 顾辰逸 + 公司 + 秘书 + 父亲是董事长秘密）
  → 与真链路 journey 用的样本同结构
- **同一抽取 prompt**（_BATCH_TEMPLATE，4 类输出 schema 完整版）
- **N=4 段**（同 prompt 跑 4 段以减弱随机性）
- **每段同时调 2 个模型**：记录 raw 输出、tokens、dt
- **宽松 JSON 解析**（先试直接 → 剥 ```json fence → 找首个 { 配对）

## 三、实测结果（N=4）

| 指标 | glm-4-flash（旧） | glm-4-flash-250414（新） | 差 |
|---|---|---|---|
| entities | 23 | 14 | -9（少而精） |
| relations | 16 | 8 | -8 |
| events | 15 | 9 | -6 |
| event_type 分布 | 15 全 incident | 2 incident / 4 revelation / 2 decision / 1 battle | **全类型** |
| event_links | 0 | 5 | +5 |
| entity_knowledge | 0 | 8 (6 secret + 2 event) | +8 |
| state_changes | 0 | 6 | +6 |
| foreshadows | 0 | 4 | +4 |
| in_story_date 填充率 | 6.7% | 100% | **+93%** |
| avg dt | 29.1s | 18.3s | **-37% 速度** |
| parse_fail | 0 | 0 | 0 |

## 四、关键发现

### 4.1 弱模型「猛堆 entity/relation」是低质信号

旧版数字看起来多（23 entities vs 14）但全是「无脑识别」——所有 event 标
incident、所有 in_story_date 空、所有 7 类只填 3 类。新版数字更少但**把
7 类都填了，且类型分布合理**（revelation/battle/decision 都出现）。
这是「**召回率 vs 结构化深度**」的典型 tradeoff。

### 4.2 in_story_date 100% 填充 = 候选 #7 工作前提

候选 #7（口径 C）要求 `in_story_date` 不为空才能用年号纪年做量纲比较。
旧版 6.7% 填充率意味着 **6/7 段的口径 C 形同虚设**。
新版 100% 填充意味着 **口径 C 真正生效**——之前接 #7 以为有戏，结果发现
主链数据根本不来。

### 4.3 knowledge/state/foreshadows 全空 = 规则失效主因

旧版 knowledge=0 → unauthorized_knowledge 规则不报（因为没东西可比）
→ 用户在 Judge 面板永远看不到「预知外挂」告警。
state_changes=0 → teleportation 规则只查 character_state 库，但库也是 0
→ 瞬移告警也失效。
新版全填 8/6/4 → **3 条 Judge 规则实际触发起来**。

### 4.4 速度 +37% 是免费红利

新版 18.3s vs 旧 29.1s——同样内容同样 schema，**2 路并发批量时 8 段
总耗时从 232s 降到 146s**。所有 v3.0 后续工作的延迟都受益。

## 五、建议（独立议题，本轮不实施）

把 `backend/.env` 的 `ZHIPU_MODEL` 改为 `glm-4-flash-250414`（或加入
`zhipu_models` 链首）——零成本升级，KG 抽取质量飞跃。但这是 settings 改动，
要重启服务 + 验证回退路径——**新一阶段任务**。

## 六、抛出的反直觉点

**「旧版数字大看着像质量高」是错判**。KG 抽取质量的真实指标是：
- 7 类 schema 的覆盖度（不是 entities 数量）
- 字段填充率（in_story_date / known_kind / event_type）
- 类型多样性（不是 1 个 type 100%）

这些指标**只在 7 类完整 + 字段全填**后才显出优势。
