# `provider is None` 只读审计（2026-11-17）

> 只读产出，未改任何代码，未提交。范围：`backend/app` 内所有 `provider is None` / `_provider_or_none()` 判断点。
> 共 26 处：本章纲起草端点（`routers/chapter_outline.py:391`）已在 `d1ced3f` 修复，剩余 25 处在本清单内。

## 0. 判定前提（为什么不能只看 `is None`）

- `get_llm_provider()` / `get_distiller_provider()` 在「渠道不匹配 / 没填 key」时**不返回 None**，兜底返回 `LocalFallbackLLMProvider`（`app/llm/provider.py:1450`、`:1769`、`:2104`、`:2144-2157`）。所以只判 `provider is None` 的防御是**形式上的**，真正决定后果的是「LocalFallback 的输出会不会被当成真实结果消费」。
- `LocalFallbackLLMProvider` 只覆写 `chat()`，输出固定自我标记：`【离线兜底·确定性续写】已锁定角色：…待解悬念：…（续写正文占位：…）`（`provider.py:1479-1483`）。
- `summarize_rollup()` 走基类 `LLMProvider.summarize_rollup`（`provider.py:375-386`），是**对已有真实摘要的确定性截断**，不是模板正文。
- 因此「输出能否骗过消费方」取决于三件事：是否有**严格 JSON 解析**、是否有**证据/闸门校验**、输出是否**自我标记**。

## 1. 逐处清单

### `routers/projects.py`（7 处，全 C）

| 行 | 分支后本来要做什么 | 输出流向 | 兜底生效时用户看到什么 | 类 | 改的话一句 |
|---|---|---|---|---|---|
| 1277 `generate_character_card` | `_model_call(complete_card)`，解析失败 → `if not result` 回 `ok:false` | 返回前端（候选卡，不落库） | 「AI 没给出可用结果」，无假字段 | C | 前面加 `is_local_fallback` → 503（行为等价，只改文案） |
| 1325 `generate_character_cards_batch` | `generate_cards(raise_errors=True)` | 返回前端（候选，不落库） | 502，批量生成失败 | C | 同上 |
| 1571 `generate_power_systems` | `parse_systems` 空 → `ok:false` / 502 | 返回前端（候选体系） | 失败提示，不落库 | C | 同上 |
| 1687 `generate_background` | `normalize_background` + 闸门（一句话世界 + 硬规则） | 返回前端（候选，不落库） | 「AI 回了内容但不像一组世界背景」+ 原文 2000 字 | C | 同上 |
| 1741 `generate_tiers` | 档位 JSON 解析，空 → `ok:false` | 返回前端（候选档位） | 失败提示 | C | 同上 |
| 1808 `plan_cultivation_route` | 规划 JSON 解析，空 → `ok:false` | 返回前端（候选路线） | 失败提示 | C | 同上 |
| 1931 `generate_power_scale` | `parse_scale` 空 → `ok:false` | 返回前端（候选量表） | 失败提示 | C | 同上 |

共同结论：模板文本过不了 JSON 解析/闸门，**不会作为「规划/分析结论」出现在界面上**，也没写库，归 C。

### `generation/engine.py`（3 处，全 B）

| 行 | 分支后本来要做什么 | 输出流向 | 兜底生效时用户看到什么 | 类 | 改的话一句 |
|---|---|---|---|---|---|
| 4839 `generate_next_segment` | 组装提示词 → 生成正文 → 写 `generations` | **写库** + 返回/SSE；后续进前情 | 段落里明写「【离线兜底·确定性续写】…占位」 | B | 保留；如需更严，在写库前拦 `is_local_fallback` |
| 5326 流式生成 | 同上，逐字吐 | SSE 实时显示 + 写库 | 同上，逐字可见 | B | 同上 |
| 5875 重写/regenerate | 重写选定段 → 覆盖写库 | 写库 + 返回 | 同上 | B | 同上 |

说明：B 的依据是「正文生成兜底出字、质量差可感知」——模板自我标记清晰，且离线回归/演示依赖这条路。

### `kg/corpus_ingest.py`（4 处，全 C，但见第 2 节待确认项）

| 行 | 分支后本来要做什么 | 输出流向 | 兜底生效时用户看到什么 | 类 | 改的话一句 |
|---|---|---|---|---|---|
| 606 `ingest_novel` | `extract_batch` 抽 KG → 写 `kg_events/entities` | 写库（仅在解析成功时） | 窗口 `status=failed`，实体 0 条 | C | 无需改；若要更早提示，在入口拦 |
| 708 `retry_failed_split` | 单章重跑失败窗口 | 写库（成功才写） | 重跑仍失败的窗口数 | C | 同上 |
| 799 `fill_empty_chapters`（补洞） | 单章补抽空章 | 写库（成功才写） | 补洞失败章号 | C | 同上 |
| 907 `extract_targeted` | 窄 prompt 补势力/体系 | 写库（成功才写） | 目标类实体仍为 0 | C | 同上 |

`extract_batch` 解析失败返回带 `ERROR_FIELD` 的空骨架，调用方据此判 failed（`kg/extractor_v3.py:805` 注释），模板文本不会写进 KG。

### `agents/`（4 处 C 已正确 + 1 处 C）

| 行 | 分支后本来要做什么 | 输出流向 | 兜底生效时用户看到什么 | 类 | 改的话一句 |
|---|---|---|---|---|---|
| `authority_body_repair.py:125` | 正文权威修复 → 产修订 | 写库/返回修复稿 | 抛 `ValueError('local_fallback')`（151 行守卫） | C | 守卫已在，可选换成共用函数 |
| `body_acceptance.py:354` | 验收判定 | 验收回执 | 358 行显式降级，不产判定 | C | 同上 |
| `derived_rebuild.py:81` | 重建派生数据 | 写库 | 92 行抛 `local_fallback` | C | 同上 |
| `diagnostic.py:315` | 诊断/体检 | 诊断回执 | 319 行抛 `local_fallback` | C | 同上 |
| `dialog/rollback.py:188` | 回滚后重建摘要树（`rebuild_summary_tree`） | **写库** `summary_tree` | 摘要 = 真实 L1 摘要的确定性截断，不是模板正文 | C | 可加共用守卫，防未来 `summarize_rollup` 改实现 |

### 其余（4 处，全 C）

| 行 | 分支后本来要做什么 | 输出流向 | 兜底生效时用户看到什么 | 类 | 改的话一句 |
|---|---|---|---|---|---|
| `kg/batch_worker.py:819` | `_llm_meta` 记录模型名/耗时 | 仅回执/日志 | 模型名显示 `LocalFallback…`（`612` 行已用 `_provider_unavailable` 拦过前置） | C | 可复用 helper |
| `roleplay/chat.py:328` | 取 provider 回答角色问题 | 返回前端（对话记录） | `finalize_answer` 换成 `OFFLINE_NOTICE`「没配模型…」 | C | 已正确 |
| `routers/assistant_generation.py:454` | 惰性句柄取不到 provider → 抛 `RuntimeError` | 旁路闭环 | 闭环走纯模板降级，不影响正文 | C | 这是**活分支**（`_lazy_llm()` 设计上可返回 None），不是死判断 |
| `style/narrative.py:3817`、`:7907` | 文风蒸馏 / 模板构建 | 写蒸馏库、档案 | `resolve_distill_provider()` 已挡掉 LocalFallback（3241/3353），无 key 时直接报错 | C | 无需改 |
| `style/entity_store.py:181` | 构建专名表（LLM 判定） | 写缓存文件 + 关闭原文进提示词 | 判空 → `judge_failed`，提示词不用该表 | C | 无需改 |

## 2. 汇总

### 2.1 数量

- **A（必须改）：0 处**
- **B（可以保留）：3 处** —— `generation/engine.py:4839 / 5326 / 5875`
- **C（无实际影响）：22 处**

A 为空的原因是：所有「输出会写库/显示为结论」的分支，后面都有**严格 JSON 解析**（projects.py 7 处、corpus_ingest 4 处）、**证据/闸门校验**（narrative 2 处、entity_store 1 处），或者**已有 LocalFallback 显式守卫**（agents 4 处、roleplay 1 处、batch_worker 1 处）；剩下的是活分支（assistant_generation:454）与确定性截断（dialog/rollback:188）。

### 2.2 判断不了、需要读运行时才能确定

- **`kg/corpus_ingest.py` 的 4 处（尤其 606 `ingest_novel`）**：静态能确定「模板输出不会写进 KG」，但**失败窗口数是否在界面上可见**需要实跑确认。如果前端只显示「实体 0 条」而不显示 failed 窗口，用户可能把「抽取失败」当成「这本书没有实体」，就应从 C 升为 B/A。建议用临时库跑一次「无渠道 + 1 本书」看结果页文案。

### 2.3 能否把两处正确实现抽成共用函数

可以，且建议抽。两处现状：

- `kg/batch_worker.py:1824 _provider_unavailable(provider)`：`provider is None or isinstance(provider, LocalFallbackLLMProvider)`；
- `kg/era_check.py:497`：`provider is None or provider_name == "LocalFallbackLLMProvider"`（按类名，未 import 类型）。

建议在 `app/llm/provider.py` 增加单一事实来源：

```python
def is_local_fallback(provider) -> bool:
    return provider is None or isinstance(provider, LocalFallbackLLMProvider)

# 需要抛错时（agents 现有写法）：
def ensure_real_provider(provider, *, action: str): ...
```

复用面：

- 直接替换：`batch_worker.py:1824`、`era_check.py:497`、`chapter_outline.py:391`（已修，可回填）——3 处；
- 顺带统一现有 7 处内联 `isinstance` 守卫：`authority_body_repair.py:151`、`body_acceptance.py:358`、`body_repair.py:478`、`derived_rebuild.py:92`、`diagnostic.py:319`、`ingest_clean.py:192`、`attachment_chat.py:87`——7 处；
- **A 类中可直接复用的：0 处**（当前没有 A 类）。

合计约 10 个调用点可收敛到同一个 helper；`era_check` 按类名比较的写法应改为类型判断，避免重命名漂移。

## 3. 附：本次不计入的点

- `app/llm/provider.py:1668` `is_writer = provider is None`：函数参数默认值语义（None = 用默认 writer），不是「判断 LLM 是否可用」，不计入。
- `routers/chapter_outline.py:391`：已在 `d1ced3f` 修复，作为反例列在这里。
