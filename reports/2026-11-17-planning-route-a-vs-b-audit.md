# 规划/生成路线 A vs B 只读架构审计

- 审计对象：`feat/resume-push-2026-10` @ `13c3691`（下称 **A**）与 `origin/main` @ `cb05a45`（下称 **B**）
- 审计方式：只读 `git show` / `git grep` / 文件阅读 + 仓内报告引用；未启动服务、未写库、未提交、未修改现有文件。
- 标注约定：
  - **【事实】** 读代码确认（含当前 HEAD 与 `origin/main` 的差异）。
  - **【实测】** 直接引用 `reports/` 里已有的实测数字。
  - **【推断】** 基于代码与数据做的判断，没有直接实验证据。
  - **【查不到】** 仓内没有可支撑的代码或数据。
- 行号说明：当前分支文件可直接点开；`origin/main:...` 指向未检出的 main 版本，只能按代码内容核对。

---

## 0. 结论先行

**推荐：保持 A 的章级生成/调度作为唯一生成主线，同时把 B 的「权威约束 + 段契约 + 证据化判定 + 规划扩展/大纲生成」作为子系统移植进来；不替换整条引擎，也不维持两套并行引擎。**

这不是“各有优劣”的和稀泥，理由是可验证的三条：

1. **B 不是章级产品**：B 的生成标签是 `段N`，没有章号、章纲、章级质量回执；B 的规划是 3–12 个因果节点，不是 40 条章纲。把 B 当主线会直接丢掉 A 已经做出来的“按章写、按章回执、按章重写”的产品形态。【事实】
2. **A 的长期主线目前缺的是“规划生产端”，不是“生成端”**：A 的章纲 `project.outline` 在 feat 侧除创建时 `projects.py:65` 外没有第二个写入方；4 个大纲端点与前端大纲面板在 main，`generation/planning.py` 虽已移植到 feat 但**没有任何路由/引擎调用方**。A 的节点区间只是把章纲**均分**，没有追加/重规划入口。也就是说，A 的生成端已经比 B 复杂得多，但它的“上游计划”在 feat 里是断供的。【事实 + 报告】
3. **B 的完整移植代价已被仓内报告量化**：125 个冲突文件、67 个“两代实现”、25 个高风险；`engine.py` 75 处冲突；即便只恢复大纲端点，也要补齐 4 个端点 + 前端面板 + 调用接线（`generation/planning.py` 已在 feat，但没有调用方），不能只搬端点。而旧项目已有 43 章、40 条大纲，B 的 `outline/apply` 明确拒绝“已有正文”的项目（409）。【实测 + 事实】

**会改主意的条件**（写死在这里，避免以后模糊）：

- 产品明确放弃“固定章节”，改为“按段/按场景连载”；此时 B 的段式引擎才具备主线资格，但仍需补 KG/文风/章节回执。
- 或者：A 侧在 3 个里程碑内仍无法补上章纲生产者与章号入参，导致第 44 章以后无法同源续写；且团队接受把现有 43 章视为只读遗留、新项目重新起盘。
- 或者：做一次同章同模型的 A/B，B 的契约+权威在“越界实体数、越章材料字数、验收一次通过率”三项上都显著优于 A 的 `structure_guard_mode=block`+移植后的 authority；目前仓内**没有**这项实验，所以不能先验地判 B 更强。

---

## 1. 两套机制的真实结构

### 1.1 A：章级节点 + 章纲 + 章号同步

#### 数据结构

`PlotNode` 与 `TimelineState` 定义在 [timeline.py](backend/app/generation/timeline.py#L33-L74)：

- `PlotNode.id / kind("main"|"branch") / title / emotion`
- `entry_condition`（起点状态，注入提示）
- `exit_condition`（本段/本章写到什么程度算完成）
- `beats[]`（高潮内部节拍）
- `opens[] / closes[]`（钩子）
- `segment_budget`（本节点分到几段）
- `parent / children`（支线挂主干）
- **`chapter_start / chapter_end`（本节点覆盖的大纲章节闭区间）**
- `TimelineState.nodes / order / done / current_node / current_beat / branch_budget / branch_used / seg_counts / early_done / branch_current`

持久化版本是 `TIMELINE_SCHEMA_VERSION = 1`，落在 `projects.plot_timeline` JSON 列里，节点字典显式保存 `chapter_start/chapter_end`（`timeline.py:330-357`）。【事实】

#### 谁产出

1. `plan_timeline(total_segments)` 先搭骨架：按总段数反推 3–12 个主干节点 + 支线额度（`timeline.py:77-118`）。
2. `plan_nodes_with_llm(provider, outline, plot_bible, n_main)` **调一次 LLM**，把整条 outline 切成 `n_main` 个剧情点（`timeline.py:600-668`）。
3. `_balanced_chapter_ranges(n_nodes, total_chapters)` 在调用 LLM **之前**就把章区间按条数均分好（`timeline.py:360-397`）；LLM 只负责填 title/emotion/entry/exit/beats，**不能决定章节边界**。
4. `_timeline_state_for(project, written_segments, provider, db, chapter_based)` 负责按需创建/持久化；章模式下先 `sync_node_for_chapter` 把 `current_node/current_beat/done` 对齐到当前章号（`engine.py:4569`、`timeline.py:442-477`）。
5. 规划失败时 `plan_nodes_with_llm` 返回 `None`，调用方回退到**占位节点**（空 entry/exit、无 beats），生成不阻断（`timeline.py:608`、`engine.py` 的 `_timeline_state_for`）。【事实】

#### 谁消费、在哪一步生效

生成一章前：

- 由 `_timeline_progress(db, project)` 推当前章号，再用 `current_node_prompt(project, written, chapter=..., chapter_based=True)` 选节点（`engine.py:4776-4810`）。
- `chapter_outline_entry(project.outline, chapter)` 取**本章那一条**章纲；提示词里同时放“本章大纲”和“当前剧情进度”，并明确“本章大纲的具体事件优先，剧情进度只约束节奏与停笔点”（[prompts.py](backend/app/generation/prompts.py#L1291-L1301)）。
- `build_node_prompt` 注入：覆盖章节区间、“只写这个区间内的事件，不得提前用后面章节的材料”、entry/exit、当前 beat、主线进度、三条硬约束（不提前用后续节点、不引入章纲/entry/exit/可用角色表之外的角色、不写结局）。【事实】
- `filter_available_characters` 按“已出场 + 本章大纲 + 角色卡首次出场章”过滤角色；`_advance_block` 把台账里的推进要求挂进提示（`engine.py:1888`）。【事实】

一个容易漏掉的诊断缺口：`/api/generate-plan` 的注释说“预检页必须与真实生成同源”，但它调用 `current_node_prompt(proj, written, provider, db=db)` 时**没有传 `chapter=`**（[generation.py](backend/app/routers/generation.py#L301-L303)）。因此章模式下预检拿到的节点/章纲可能与实际生成不是同一份；同时它传了 `provider` 和 `db`，预检本身可能触发建图并持久化。这是“预检所见 ≠ 实际下发”的具体来源。【事实 + 推断】

生成一章后：

- `_judge_and_advance(..., chapter_based=True)`：章号不变时直接 `skipped_reason="rewrite_same_chapter"`；章号前进才 `sync_node_for_chapter`，并用 `judge_node_completion` 判 exit（只返回 `true/false`，最多看最近 1200 字），`true` 只记 `early_done`，不切走 `current_node`（`engine.py:4719-4747`）。
- `_run_structure_check`：检查情节点覆盖、越章材料（后续章关键词命中）、人物性别一致；`structure_guard_mode` 默认 `report`，可配 `block`（`engine.py:1765-1795`、`5069`、`5525`）。覆盖判定有一个自带的退化条件：本章大纲切出的单元太少（典型是卷纲一句一章）时，`discrimination="insufficient"`，覆盖率不参与判据，只输出兜底值、不拦截（[structure_check.py](backend/app/generation/structure_check.py#L8-L32)）。【事实】
- `_run_progress_ledger` 与 `_coherence_report` 生成质量回执，默认也是报告/可选重写，不是硬闸。【事实】

#### A 的关键设计判断

A 在 11-13 之后做过一次明确修复：**节点推进不再按 Generation 行数，而是按章号**。`sync_node_for_chapter` 的注释写明：重跑同一章不会把大纲往后推，支线不能顶替当前节点，`early_done` 不能被下一次同步抹掉（`timeline.py:442-457`）。这直接针对早期“第 1 章的 5 个版本吃掉 n3 的 6 段预算，第 3 章落到 n4”的事故。【事实 + 实测】

但 A 的章区间本质是**按大纲条数均分**：

```
14 章 / 4 点 → 1–4、5–8、9–11、12–14
```

它不是按剧情因果切分，节点内可能横跨 10 章（事故里 n4 覆盖第 5–14 章）。【事实】

---

### 1.2 B：段级因果节点 + 保存权威 + 段契约 + 证据判定

#### 数据结构

B 的 `PlotNode` 与 A 同源，但**没有** `chapter_start/chapter_end`；`TimelineState` 多了三个字段（`origin/main:backend/app/generation/timeline.py:50-64`）：

- `completion: dict[node_id, {...}]`：节点完成的**正文证据**
- `last_judged_segment: int`
- `planning: dict`：结构化规划状态，核心是：
  - `planning.nodes`：`CausalNode[]`
  - `planning.segment_contracts`：**段号 → 契约**
  - `planning.budget_revisions[]`
  - `planning.verified / source_fingerprint`

`CausalNode` 定义在 `origin/main:backend/app/generation/planning.py:104-115`，字段是：

```
title, premise, motivation, action, result, cost,
time, location, participants[], state_before, state_after
```

它不是“章节摘要”，而是一个**可判定的因果场景**：谁、为何、在哪、什么时间、做了什么、结果/代价、前后状态。`UNKNOWN_VALUES` 里的 `unknown/待定/未定` 会被收集成 `clarifications`，结构化不完整时 `apply` 返回 409 `clarification_required`，不允许含糊草案落库（`planning.py:147-203`、`origin/main:backend/app/routers/wizard.py:656-680`）。【事实】

`segment_contracts[seg_no]` 的内容（`origin/main:backend/app/generation/engine.py:3118-3133`）：

```python
{
  "node_id": cur_id,
  "prompt": build_node_prompt(cur_node, beat, progress,
                              authority=project.premise,
                              continuation=...),
  "authority": {
      "premise": project.premise,
      "causal_node": deepcopy(causal_nodes[causal_index])
  }
}
```

#### 谁产出

1. `POST /api/projects/{id}/generate-outline`：根据 `premise`、已确认 `plot_bible`、作者 goals，调模型产出 3–12 个 `CausalNode`（`origin/main:backend/app/routers/wizard.py`、`generation/planning.py:117-141`）。
2. `POST /outline/clarify`：对 `unknown` 字段逐条提问，不替作者决定。
3. `POST /outline/apply`：校验 fingerprint、结构完整性；**已有正文的项目直接 409**；通过后 `timeline_for(nodes, metadata, total_segments)` 写 `planning.nodes`，并把 `project.outline` 覆盖成**节点标题列表**（`wizard.py:656-680`）。【事实】
4. `POST /plan-extension`：当前段预算用尽、但故事未讲完时，追加新的 `CausalNode`；先把新节点序列化，再用 `closed_inventory_v2.inspect_inventory(protocol=5)` 对照 `project.premise` 做“是否越出作者权威”的检查，通过后**预写**新节点的 `segment_contracts`，并设 `current_node` 为第一个新节点（`origin/main:backend/app/agents/plan_extension.py:200-420`）。一个硬限制：`_authority_text` 只从**已冻结的契约**里取权威，项目还没有任何契约时返回 `no_saved_authority`，扩展直接不可用；所以它救不了“从零开始、前一章都没有”的项目。【事实】
5. `POST /outline/revise-budget`：只允许修订“当前未完成节点”，新预算必须大于原预算与已用段数，并记录 `budget_revisions` 与 `authorized_by="explicit_user_confirmation"`（`wizard.py:628-653`）。【事实】
6. 每提交一段，`_judge_and_advance` 为该段写契约（段号 = 提交前段数 + 1），并冻结当次 prompt 与 authority（`engine.py:3118-3133`）。**契约主要是“写完即冻结”，不是“写前先签合同”**；写前能硬拦的是 `needs_planning/needs_review/plan_exhausted` 三道 409 与提示里的 authority。【事实】

#### 谁消费、在哪一步生效

写前：

- `current_node_prompt` 在 `db` 存在且状态为 `needs_planning` / `needs_review` 时直接 409：前者是“节点缺少有效计划”，后者是“节点预算用尽且退出条件尚未证实”。这两条与 `_plan_exhausted` 的 409 一起，构成 B 的三道写前硬闸（`origin/main:backend/app/generation/engine.py`）。
- `_plan_exhausted(project)` 在规划耗尽时返回 409，不让你硬编（`engine.py:2933`）。
- `build_node_prompt(node, beat, progress, authority, continuation)` 把**权威块**和**接力上下文**挂进提示（`timeline.py:240-292`、`261-292`）。
  - 权威块：`AUTHORITY_MARK + 说明 + 权威文本 + 规则`；首次来自 `project.premise`，已有契约时优先用契约里冻结的 authority（`_current_authority` / `_contract_authority`）。
  - 接力上下文：同一节点跨多段时，把上一段收尾状态/结尾引文给模型，避免第三段重演“进门”。
- 提示里硬约束：只写当前节点、写到 exit 即停、不引入未提及角色/物件/地点、物件必须能在权威里找到依据。【事实】

写后：

- `_judge_and_advance` 的 `judge_node_completion(provider, node, recent_text, ...)` 是三态判定（`supported/contradicted/unknown`），要求模型返回 `source_ids`（从给定段落 ID 中选）与 `reason`，并明确禁止自由输出 quote/坐标；同时记录 `text_hash` 做审计。`supported` 必须有 `sources`，否则降级为 `unknown`；完成证据落入 `state.completion[node_id]`。注意：`unknown` 不代表“不推进”，预算仍按段推进，只是不把它记为完成（`origin/main:backend/app/generation/timeline.py:180, 616-642`）。【事实】
- 验收层 `body_acceptance` 读取段契约做“计划节点缺口”检查：只拦截**正文明确与计划矛盾**的项；未写到的项只展示、不误判为违例（`origin/main:backend/app/agents/body_acceptance.py:191-255`）。【事实】
- `body_consistency.read_body` 依据 `planning.segment_contracts` 取该段**当时冻结的 prompt/authority**，而不是当前 premise；契约缺失会标 `missing_saved_contract`（`contract_complete=False`），不允许拿今天的设定去审昨天的正文（`origin/main:backend/app/agents/body_consistency.py:223-236`）。【事实】
- `closed_inventory_v2` 是一个多阶段、带 checkpoint 和调用预算的清单协议；但它在 main 里**只被 `plan_extension`（规划节点）与 `diagnostic`（独立诊断）调用，不在逐章生成主链上**。生成主链的约束来自 prompt 里的 `authority` 和验收层对契约的读取。【事实】

#### B 的关键设计判断

B 的注释自己记录了两次实测事故，值得原样保留：

- “实测一次真实生成凭空造出 80 处清单外物件（镊子、钟、材料盒…），整段验收把它们全判成违例” → 因此 authority 必须与 prompt 同处一份（`timeline.py:244-258`）。
- “实测第三段重新写了一遍‘进门’，或者在第三段里断言‘登记页仍然是空白的’” → 因此需要 `continuation` 接力上下文（`timeline.py:275-281`）。

这两条说明 B 的 authority 不是纸面设计，而是被 B 自己的失败逼出来的；但**它们不是 A 的 43 章语料上的实测**，不能直接当作“B 在 A 上更好”的证据。【事实 + 推断】

---

### 1.3 对任务描述中三处前提的校正

这三处会直接影响“B 到底强在哪”的判断，必须先说清：

1. **“引擎有 6 处写 `segment_contracts`”不成立。**
   - 【事实】main `engine.py` 里 `segment_contracts` 出现 5 次：`2954` 读、`3114` 读、`3133` **唯一一次写**、`3238` 读、`3826` 读；另有 `plan_extension.py:396` 一次预写。合计 2 个写入点，不是 6 个。
2. **“用 `inspect_inventory(protocol=5)` 按权威时间窗/物件清单做越界检查”只对“规划节点”成立，不对“生成正文”成立。**
   - 【事实】`inspect_inventory` 在 main 只被 `plan_extension` 与 `diagnostic` 调用；生成链没有调用。正文越界主要靠 prompt 里的 authority + 验收层读契约，不是逐段跑 protocol 5。
3. **“`build_node_prompt(node, beat, progress, authority=None, continuation=None)`”在 B 成立，A 不是这个签名。**
   - 【事实】A 的 `build_node_prompt(node, beat, main_pct)` 只有 3 个参数，没有 authority/continuation（当前分支 `timeline.py`；B 的签名见 `origin/main:backend/app/generation/timeline.py:261`）。
4. 附带校正：任务说 A 的 `timeline.py` 约 734 行；当前 HEAD 实际 **857 行**，期间已有增量（章区间/early_done 等）。【事实】

---

## 2. 能力逐项对比

| 维度 | A（feat） | B（main） | 判定 |
|---|---|---|---|
| **规划粒度** | 章级：40 条章纲，每章一条；节点覆盖多章（均分区间） | 段级：3–12 个因果节点，每节点多段；无章概念 | A 更贴“章”产品；B 更贴“场景因果” |
| **计划字段** | title/emotion/entry/exit/beats/hooks；无时间、地点、参与者、前后状态 | 因果节点 11 字段：含 time/location/participants/state_before/after | B 可判定的信息更多 |
| **写前约束** | 章纲 + 节点区间 + entry/exit + 可用角色表 + 三条硬约束；无 authority；规划失败不拦 | 节点 + authority + continuation + entry/exit；`needs_planning/needs_review/plan_exhausted` 三道 409 | B 多了“权威、接力、硬闸”；A 多了“本章大纲” |
| **写后校验** | structure_check（覆盖/越章/性别）+ ledger + coherence；默认 report-only，可配 block | 契约 + 证据化 judge + body_acceptance 计划缺口；契约缺 prompt 时不可审旧文 | B 的证据链更硬；A 的检查项更贴章 |
| **越界检测：时间窗** | 无时间窗概念 | 权威文本里用 `HH:MM至HH:MM` 正则抽时间窗；抽不到就没有 headroom | B 有，但**依赖 premise 里写了钟点** |
| **越界检测：人物/物件清单** | 角色表按“已出场+本章大纲”过滤；无物件封闭清单 | `project.premise` 作为封闭权威，plan_extension 用 protocol 5 查节点；正文靠 prompt+验收 | B 强；A 无物件清单 |
| **封闭措辞** | “不得提前用后面章节的材料”“不引入未提及角色” | “未在权威中出现的人物/物件/地点不得新增”“写到 exit 即停” | 大体等价，B 多物件/地点 |
| **规划不足时扩展** | 无端点；outline 不可追加；节点无追加入口；只能在下次规划时重算（且需要章纲） | `plan-extension` 追加因果节点并预写契约；`revise-budget` 修订当前节点预算 | B 有明确扩展；A 基本没有 |
| **与 KG/时间线一致性** | 强：章号同步、KG 角色卡回填与按章可用过滤、进度台账、coherence 用 KG 实体 | 中：`kg_slice` 注入“已发生事实”，每段 `_merge_segment_into_graph` 增量合并喂 reminders；但无章级角色卡过滤、无 ledger/coherence | A 强，B 有基础 KG 闭环 |
| **增量修改/局部重写** | `rewrite_same_chapter` 不推进节点；`scope_guard` 防重写越界；patch 支持局部 | 契约按段冻结；`body_repair`/`authority_body_repair` 可按权威修；重写与段预算/契约的关系需另行核对 | A 的章级重写更成熟；B 的权威修复更精确 |
| **出错可诊断性** | 质量回执 `quality_json`；结构检查三项；judge 只回 bool；**不存当次 prompt** | 契约保存当次 prompt+authority；judge 回三态 + `source_ids` + `reason` + `text_hash`；验收按段给证据 | B 强一档，尤其“提示词可回放” |
| **规划失败处理** | 静默回退占位节点，不阻断，也不告警 | `clarification_required` 409，unknown 字段逐条列出 | B 强；A 的静默降级是风险 |
| **产品完整度** | 章标签、风格闸、provider、队列、续写、KG、前端 | 段标签、无章纲/章回执；前端大纲面板完整 | A 是产品主线，B 是能力库 |
| **测试覆盖** | 1123/1126 通过（报告口径） | 大纲/契约测试只存在于 main；feat 无 | A 的回归更完整；B 的目标能力在 feat 无测试 |

【事实】除“1123/1126”来自 `reports/2026-11-15-merge-conflict-analysis.md` 外，其余为代码对照结论。

---

## 3. 实证：A 的已暴露问题，逐条对上 B 的哪一层

> 说明：`reports/` 里的问题多数发生在 11-13 的代码状态；其中“章纲整段不投”“性别无信号”等已在当前 HEAD 修掉。表里同时标注“现在是否已修”，避免拿旧账判现在的 A。

### 3.1 问题清单与对应关系

| # | A 的实际问题（报告原文/数字） | 现在的 A 是否已修 | B 的哪一层本可以拦 | 对应判定 |
|---|---|---|---|---|
| 1 | 生成到第 43 章，`outline` 只有 40 条；`chapter=44` 时 `chapter_outline_entry=None`，`node_for_chapter(44)=None`，`check_structure` 三项全 `skipped`；平台“看起来成功”但无章纲、无节点、无结构检测（`2026-11-15-platform-gap.md:64-76`） | 未修；当前仍无章纲写入方 | B 的 `plan-extension` 只能扩“因果节点/段预算”，**没有章纲概念**；`generate-outline` 会在已有正文时 409，且会把 outline 覆盖成节点标题 | **对应不上（章级需求）**；只在“节点/预算不足”层面部分对应 |
| 2 | 新章落库标签是 `段61`，与 43 章的 `第N章` 体系不一致；下次续写仍按最大章号 43 算（`platform-gap.md:68-72`） | 未修；`routers/generation.py` 三个入口仍无章号入参 | B 完全没有章身份，只会写 `段N` | **对应不上**；B 更弱，不能拿 B 修 |
| 3 | `project.outline` 无追加/编辑；`ProjectPatch` 无 `outline`；全仓除创建时 `projects.py:65` 外无第二写入方（`platform-gap.md:92-95`、`merge-plan-29.md:16`） | 未修 | B 的 4 个 outline 端点 + 前端面板 + `generation/planning.py`，是唯一现成的大纲生产端 | **可对应**；但这是“搬生产端”，不是“换引擎” |
| 4 | 时间线节点与章区间无追加入口；`assign_chapter_ranges` 无路由调用（`platform-gap.md:97-100`） | 未修 | B 的 `plan-extension` 追加因果节点、`revise-budget` 调预算；但都不生成章区间 | **部分对应**：能补节点，不能补“第 41–47 章区间” |
| 5 | 第 3 章把第 4–5 章材料提前烧掉：旧稿 4,460 字里 98 段（64%）写的是第 4 章骨架；`孩子/走失/投票` 在旧第 3 章出现 0 次，`编外跑腿` 身份也未出现（`2026-11-13-p6-ch03-old-vs-new.md:27, 44, 96-107`、`p6-ch03-rootcause.md:163-179`） | **已修**：当前章模式注入本章大纲，节点区间按章同步；结构检查可报“越章材料” | B 的 authority 只能拦**有名有姓的实体/物件**，拦不住“同一批人物演了下一章的剧情”；B 也无章边界 | **部分对应**：B 能拦物件/人物越界，拦不住“剧情事件提前” |
| 6 | 第 3 章顾青梧“她→他”：全链路没有性别信号；角色卡无 gender，KG 无性别（`p6-ch03-rootcause.md:11-30`） | **已修**：当前 KG 回填会补 `gender/first_chapter`，提示注入性别；结构检查有 gender 项 | B 的 CausalNode 没有 gender 字段，authority 是 premise 文本；不解决 | **对应不上**；这是 A 已补的能力 |
| 7 | 第 1 章的 5 个版本吃掉 n3 的 6 段预算，第 3 章指针走到 n4（`p6-ch03-rootcause.md:151-161`） | **已修**：`sync_node_for_chapter` 按章号推进，`rewrite_same_chapter` 不推进 | B 仍按段推进；若重写也生成新段行，同样会消耗预算；B 的 `last_judged_segment` 只防重复判定，不防重写吃预算 | **对应不上**；A 的章同步更对 |
| 8 | 单章平均 513.3 秒，首字等待占 47.5%，重写/压缩尾段占 35.8%，其中 26.2% 可证明无效；第 31 章 reasoning+正文撞满 65536 被截断（`2026-11-15-generation-bottleneck-ch23-32.md:12-19`） | 部分修（超长/重试/串行等另有变更，需逐项核） | B 不解决性能；契约/验收反而增加调用 | **对应不上**；不要拿 B 当性能方案 |
| 9 | 结构检测/一致性/台账默认只报告，不阻断；平台第 44 章三项静默 skipped（`platform-gap.md`；`structure_guard_mode` 默认 report） | 机制已在，默认仍 report | B 的验收对“与计划矛盾”会阻断，但只覆盖计划节点缺口；不是全量结构闸 | **部分对应**：B 的“矛盾项硬拦”可借鉴，不能覆盖 A 的章级检查 |
| 10 | A 不保存当次 prompt；历史章只能靠 `quality_json` 和当前代码反推，无法回答“当时模型到底看到了什么” | 未修（`Generation` 只有 text/summary/quality_json） | B 的契约冻结当次 `prompt` + `authority`，历史诊断不拿今天的 premise 替代 | **强对应**；这是 B 最值得搬的一层 |
| 11 | 规划失败静默回退占位节点，生成继续，事后才知道节点是空的（`plan_nodes_with_llm` 返回 None） | 未修 | B 的 `clarification_required` / 409 把“计划不完整”变成显式状态 | **强对应** |
| 12 | 平台无“上一章存在/不重复”校验，连点会重复追加（`platform-gap.md:107-110`） | 未修 | B 也没有这个幂等保护（至少本次核对范围内没有） | **对应不上** |
| 13 | 11-13 时有“自查零收益”：笔记只进重写轮、与“只许压缩”冲突（`p6-ch03-rootcause.md:14`） | 报告口径，当前需另核 | B 的验收证据/修复分层更清楚，但不直接等于解决 | **弱对应**；不硬凑 |

### 3.2 不能对应、但必须澄清的两组数据

- **“80 处清单外物件”不是 A 的实测。**
  - 【事实】它出自 B 的 `timeline.py:244-258` 注释，是 B 在**未注入 authority**时的一次真实生成，之后才加了 authority。把它算成 A 的问题，或算成 B 已经解决的证据，都是错的。
- **B 的“第三段重演进门”也不是 A 的章级模式问题。**
  - 【事实】B 是多段共用一个节点，才会出现同一节点内第 1/2/3 段重复开场；A 的章模式一章一 prompt，但会因“多版本”出现跨章预算错位（问题 7）。两者不是同一个病。

### 3.3 实证部分的总结

- B 真正能对上 A 的，只有四类：**大纲生产端（问题 3）、规划耗尽/不完整显式化（问题 11）、当次 prompt+authority 冻结（问题 10）、以及物件/人物越界的权威约束（问题 5 的一半）**。
- B 对不上 A 的，是：**章身份、章纲、章区间、章级结构检查、章级角色卡/台账/coherence、性别、重写不推进、性能**。B 只有 `kg_slice` 与段后增量合并，不能替代 A 的按章可用角色过滤与进度台账。【事实 + 推断】
- 因此“B 是本可以拦住 A 的问题”这个命题，只在上述四类成立；其余硬凑会把 B 的能力边界吹大。【推断】

---

## 4. 代价：完整移植 B 要动什么

### 4.1 模块清单（按依赖顺序）

| 层 | 要动的文件/能力 | 现状 | 代价要点 |
|---|---|---|---|
| 规划 schema | `backend/app/generation/planning.py`（CausalNode/OutlineRequest/apply 校验） | **feat 已有（234 行），但无路由/引擎调用方** | 主要是接线与测试，不是重写 |
| 规划扩展 | `backend/app/agents/plan_extension.py` | **feat 缺失**；但 `action_proposals.py` 已经 import 它并映射 `/plan-extension` | 不补就运行时 ImportError；补了还要加路由 |
| 时间线 | `timeline.py` 的 `with_authority/_authority_block/build_node_prompt(authority, continuation)/judge 证据/completion` | feat 是 v1，**无 authority/continuation/completion** | 与 A 的 chapter 字段合并；两份 `_node_to_dict` 要兼容 v1/v2 |
| 引擎 | `engine.py` 的 `_plan_exhausted`、契约写入、`_continuation_context`、`_contract_authority`、验收重绑 | feat 无 | `engine.py` 已是 7,384 行；main 版仅 3,416 行，直接 merge 的 75 处冲突集中在生成主链 |
| 大纲端点 | `wizard.py` 的 `generate-outline / outline/clarify / outline/apply / outline/revise-budget` | feat 零命中 | 4 个端点 + 请求模型 + 错误码 |
| 项目端点 | `projects.py` 的 `/plan-extension` | feat 零命中 | 与 action proposal 的 `/assistant/...` 不同，需单独路由 |
| 生成端点 | `generation.py` 的 `plan_exhausted` 409、章号入参 | feat 无章号入参 | 与 A 的章模式叠加时，要决定“段耗尽”和“章纲耗尽”谁先报 |
| 提示词 | `prompts.py` 的 authority 块、continuation 块 | feat 无 | 与 A 的“本章大纲优先”顺序要重新定；两者都抢“具体事件”的解释权 |
| 验收 | `body_acceptance.py / body_consistency.py / derived_rebuild.py / diagnostic.py` | feat **文件已在**，但契约来源没有 | 不是搬文件，是让引擎产出契约；否则 `missing_saved_contract` |
| 前端 | `client.ts` 4 个函数、`ProjectWorkspace.vue` 大纲面板/澄清/应用/预算修订 | feat 零调用 | `2026-11-15-dropped-audit.md` 明确：不能只搬端点 |
| 测试 | `test_copy_gate.py`、`test_foundation_planning.py` | 只在 main | 需随端点/规划一起搬，否则回归空白 |

【事实】以上“feat 缺失/已有但未接线”来自 `git ls-tree`、`git grep` 与 `reports/2026-11-15-dropped-audit.md`、`reports/2026-11-15-merge-plan-29.md`。

### 4.2 数据迁移

- **DB schema：基本不需要新增列。**
  - 【事实】当前 `models.py` 已经有 main 的 `story_authority`、`continuation_start_chapter`、`reference_cutoffs`、`narrative_scope`（`models.py:122-137`）；`plot_timeline` 是 JSON 列。
  - 【事实】11-15 的冲突报告说 feat 缺这些列；当前 HEAD 已并入，说明模型层至少已做过一次并集。以当前代码为准。
- **JSON 语义：需要迁移。**
  - A 的 `plot_timeline` 是 `version=1`、节点带 `chapter_start/end`。
  - B 的 `state_from_dict` 接受 version 1/2，但只读 `completion/last_judged_segment/planning`，**不读 chapter 字段**；加载 A 的旧状态后，`planning={}`、`completion={}`、契约全空。【事实】
  - 因此旧 43 章在 B 的验收层会全部是“无契约/无保存权威”，不能做同口径审计。可以补一份“合成契约”，但那是伪造当时 prompt，不如明确标 legacy。【推断】
- **旧 43 章 + 40 条大纲怎么处理：**
  1. **原项目冻结为 legacy**：只读保留，不再用新规划器覆盖；新项目走合并后的链路。代价最小，且不违反“旧数据只读”的项目约束。
  2. **原地迁移**：不可行。`outline/apply` 只要发现任何 `Generation` 就 409；`project.outline` 会被节点标题覆盖；A 的 40 条章纲与 B 的 3–12 个节点没有一一映射。
  3. **重生成 43 章**：等于放弃已完成的正文与已校准文风，成本最高；只有在“43 章可弃”时成立。
- **结论**：**没有无损迁移路径**；最现实的是“旧项目只读 + 新项目新链路”，这也是项目记忆里“旧图谱只读、新图谱独立”的同一原则。【推断】

### 4.3 两套并存的维护成本

- **不能两套引擎同时跑**：路由同名、ORM 同名表、`engine.py` 同名函数；`merge-conflict-analysis.md:189-222` 已列出 10 余条“启动即 ImportError”的硬链。所谓“并存”只能是**一套引擎 + 两套数据/提示策略**，不能是两份 `engine.py`。【事实】
- **每改一次生成主链，要过两套判定语义**：A 的 judge 是 bool + `segment_budget`；B 的 judge 是 `source_ids/reason` 证据 + 契约。两者的“完成”定义不同，测试矩阵至少翻倍。【推断】
- **两套时间线 schema**：v1（章区间）与 v2（planning/contracts）要长期兼容；每次加字段都要考虑旧项目降级。B 的 `state_from_dict` 已经用 `version in (1,2)` 做兼容，但 A 的 `_node_from_dict` 不认 v2 字段，反向不兼容。【事实】
- **两套前端规划入口**：A 目前没有大纲面板；B 有。若保留 A 的章级产品，最终仍需一套“章纲编辑器”，这套东西不能直接复用 B 的“因果节点编辑器”。【推断】
- **报告已经给出合并代价**：125 个冲突文件、67 个“两代实现”、25 个高风险；`engine.py` 75 处、`prompts.py` 28 处、`wizard.py` 11 处、`projects.py` 30 处、`models.py` 23 处（`merge-conflict-analysis.md:23-34` 与逐文件表）。这不是“搬几个文件”的量级。【实测】

---

## 5. 结论

### 5.1 明确推荐

**选项对应：并存（A 为主链路，B 为规划/验收子系统），不是“替换”，也不是“保持 A 现状”。**

**保持 A 的章级生成/调度主线；B 只作为“规划与验收子系统”并入，不整体替换。**

具体落法（按优先级）：

1. **P0：补 A 的规划生产端，而不是补 B 的引擎。**
   - 把 B 的 `generate-outline / outline/clarify / outline/apply / revise-budget` 接到 feat，复用已在 feat 的 `generation/planning.py`，但 `outline/apply` 的产物要**映射成章纲**，而不是覆盖成节点标题。
   - 这是当前唯一能解决第 44 章“无章纲”问题的路径；只加 `ProjectPatch.outline` 只能手填，不能自动生成。
2. **P0：补章号入参与章节身份。**
   - 生成入口接受 `chapter_range`，或引擎在 `chapter_based` 时按 outline 自动生成 `第N章《标题》`；否则新章一旦以 `段N` 落库，章模式就失去章号来源，只会继续写段号。
3. **P1：移植 B 的 authority + 契约冻结 + 证据化 judge。**
   - `with_authority`、`build_node_prompt(authority, continuation)`、`segment_contracts` 的 `prompt/authority` 冻结、judge 的 `source_ids/reason/text_hash` 规则。
   - 这一层直接解决 A 的“历史 prompt 不可回放”“越界无权威依据”“判完成只给 bool”三个问题，且不需要引入 B 的段式调度。
4. **P1：把 A 的 `structure_guard_mode` 从默认 `report` 改成可配置的 `block`（至少对“本章大纲缺失/越章材料/角色提前出场”三类），并补“章纲缺失”显式告警。**
   - 平台第 44 章三项静默 skipped，是当前最容易被误判为“成功”的失败模式。
5. **P2：不要移植 B 的段式 `planning.nodes` 作为 A 的章节计划。**
   - 两者粒度不同；强行合并会让“章纲优先”和“因果节点优先”在同一个 prompt 里互相打架。

### 5.2 为什么不选“替换”

- B 没有章号、章纲、章级质量回执、KG 角色卡、进度台账、coherence；替换会把这些一起丢掉。【事实】
- B 的 `outline/apply` 对已有正文项目 409；现有 43 章无法原地迁移，只能冻结或重生成。【事实】
- 仓内 29 项合流计划对 `wizard.py` 等模块的骨架判定也是“取 feat”；这与“A 为产品主线、B 为能力来源”的判断一致（`2026-11-15-merge-plan-29.md:14-18`）。【实测】
- B 的完整移植是 125 冲突文件级别；而 A 的生成链已经在 43 章上跑过，回归风险远低于换引擎。【实测 + 推断】

### 5.3 为什么不选“保持 A 现状”

- A 的章纲没有生产者/编辑器；节点区间是均分；规划失败静默降级；不存当次 prompt；结构检查默认不拦；11 条 risk 项在迁移中被静默丢弃（含 authority、照搬门禁、story seed 校验、提交前去重、验收义务重绑）。【事实 + 实测】
- 这些不是“以后优化”，而是第 44 章以后继续写就会踩的坑；纯保持 A 等于把已知缺口留到下一章爆发。

### 5.4 改主意的量化门槛

如果出现以下任一情况，应重新评估“B 为主线”：

1. **产品转向**：明确不再按“第 N 章”交付，改为按段/场景连载；此时 A 的章级优势消失，B 的段式契约更贴近目标。
2. **A 的规划端连续两个里程碑仍补不上**：第 44 章仍无章纲、仍写 `段N`；此时 A 的章模式只是脚本遗产，不是平台能力。
3. **受控 A/B 结果反向**：同一章、同一模型、同一参数下，B 的“越界实体数/越章材料字数/验收一次通过率”三项均优于 A 的 `block` 模式 + 移植 authority；且优势不是靠“B 没有章”换来的。
4. **维护成本超过收益**：两套 schema/判定语义的回归成本连续两个版本高于重写成本，且团队接受弃用 43 章。

在以上条件未出现前，**A 是长期主线，B 是必须吸收的规划/验收能力库**。这不是折中，而是由“B 无章、A 有章但缺计划生产端”这两个硬事实共同决定的。

---

## 附录 A：证据索引

### A.1 当前分支（可点开）

- [timeline.py](backend/app/generation/timeline.py#L33-L74)：PlotNode/TimelineState；`chapter_start/end`
- [timeline.py](backend/app/generation/timeline.py#L330-L397)：schema v1、节点序列化、均分章区间
- [timeline.py](backend/app/generation/timeline.py#L410-L477)：`node_for_chapter`、`sync_node_for_chapter`
- [timeline.py](backend/app/generation/timeline.py#L600-L733)：`plan_nodes_with_llm`、bool judge
- [engine.py](backend/app/generation/engine.py#L1765-L1795)：`_run_structure_check` 与默认 report
- [engine.py](backend/app/generation/engine.py#L4569-L4640)：`_timeline_state_for` 的章同步与建图
- [engine.py](backend/app/generation/engine.py#L4719-L4747)：章模式 judge/`rewrite_same_chapter`
- [engine.py](backend/app/generation/engine.py#L4776-L4815)：`current_node_prompt`
- [prompts.py](backend/app/generation/prompts.py#L1291-L1306)：章纲与节点块同时注入
- [generation.py](backend/app/routers/generation.py#L277-L304)：`/api/generate-plan`（预检不带 chapter）
- [models.py](backend/app/models.py#L122-L137)：main 的 4 个权威列已在当前模型
- [models.py](backend/app/models.py#L609-L623)：`Generation` 无 prompt 字段

### A.2 main 分支（未检出，按 `origin/main:` 引用）

- `origin/main:backend/app/generation/timeline.py:50-64`：`completion/last_judged_segment/planning`
- `origin/main:backend/app/generation/timeline.py:240-258`：authority 块与幂等
- `origin/main:backend/app/generation/timeline.py:261-292`：`build_node_prompt(authority, continuation)`
- `origin/main:backend/app/generation/timeline.py:452-497`：v1/v2 状态序列化
- `origin/main:backend/app/generation/timeline.py:627-700`：证据化 `judge_node_completion`
- `origin/main:backend/app/generation/planning.py:104-203`：CausalNode、clarification、apply 校验
- `origin/main:backend/app/generation/engine.py:2933-2954`：`_plan_exhausted`
- `origin/main:backend/app/generation/engine.py:3118-3133`：契约写入（唯一一次）
- `origin/main:backend/app/generation/engine.py:3208-3220`：`_contract_authority`
- `origin/main:backend/app/agents/plan_extension.py:200-420`：protocol 5、预写契约、追加节点
- `origin/main:backend/app/routers/wizard.py:628-680`：修订预算、apply 409
- `origin/main:backend/app/agents/body_acceptance.py:191-255`：计划缺口验收
- `origin/main:backend/app/agents/body_consistency.py:214-260`：读契约、`missing_saved_contract`
- `origin/main:backend/app/agents/closed_inventory_v2.py`：多阶段清单协议（生成链不直接调用）

### A.3 报告实证

- `reports/2026-11-15-platform-gap.md:64-110`：第 44 章无章纲/无节点/结构 skipped；A1/B1–B5
- `reports/2026-11-15-generation-bottleneck-ch23-32.md:12-19`：513.3 秒/章、首字 47.5%、重写 35.8%（26.2% 无效）
- `reports/2026-11-13-p6-ch03-old-vs-new.md:94-107`：64% 篇幅提前使用第 4 章材料
- `reports/2026-11-13-p6-ch03-rootcause.md:11-14, 151-179`：节点 n4 跨 5–14 章、多版本吃预算、第 3 章要素 0 次
- `reports/2026-11-15-merge-conflict-analysis.md:23-34, 189-222`：125 冲突、25 高风险、启动 ImportError 链
- `reports/2026-11-15-merge-plan-29.md:16, 130-160`：outline 无写入方、4 端点+前端+接线不可拆（planning.py 现已在 feat）
- `reports/2026-11-15-dropped-audit.md:19-46, 107-114`：11 条 risk 被静默 dropped；大纲端点 feat 零消费方

### A.4 明确查不到的

- **没有**在 A 的 43 章语料上跑过 B 的 authority/contract/acceptance 的对照实验；无法量化“B 能多拦多少”。
- **没有** A 的逐章 prompt 存档；无法复原 43 章当时实际看到的节点块（只能从报告里的存档节点重建）。
- **没有** B 的作品在“章级产品”上的输出样本；B 的段式结果能否拼成合格章节，未验证。
- **没有**“第 44 章用 A 的章模式 + 新章纲”实跑成功的记录；平台缺口报告只做到诊断。
