# 两分支功能盘点与前端细节比对（2026-11-15）

- 仓库：`ai-novel-generator`
- 分支 A：`feat/resume-push-2026-10`（当前分支，生成/图谱/检索路线）——下文简称 **feat**
- 分支 B：`origin/main`（审核/修复路线）——下文简称 **main**
- 合并基：`git merge-base feat/resume-push-2026-10 origin/main` **无输出**（无共同祖先，两棵树是平行历史，不能 rebase/merge，只能按功能挑选）
- 规模：feat 264 commits / main 134 commits
- 本次为**只读盘点**：未提交、未推送、未改动 main、未切换主工作区分支。main 侧检视通过临时 worktree（`%TEMP%\mwt-main-20261115`，detached HEAD）完成，结束后清理。

## 0. 口径与证据方法

1. **"是否被实际调用"的判据只用 import/字符串引用**，不用"文件名像个功能"来推测。扫描规则：在目标分支树内 grep `from|import ... <module>`、`"app....<module>"`，排除 `__pycache__`、`venv`、`tests/`、`node_modules`、`experiments/` 之外的噪声；每命中至少记录 1 个引用方。
2. **"等价能力"判据**：另一边是否存在解决同一问题、且同样被路由/入口调用的模块；"同一目录下有个同名文件"不算等价。
3. **前端"哪份更好"判据**只用三条：行为正确性、与后端接口的匹配度、可维护性。**没有用"哪个更新"或提交数量**。
4. 判不出来的条目显式标注「无法判定，需要实跑对比」。

---

## 一、独有功能盘点

### 1.1 规模核对（先纠一处数字）

| 项 | 用户说法 | 实测 | 备注 |
|---|---|---|---|
| main 独有后端模块 | 约 100 个 | **100 个** | agents 42 + generation 21 + style 20 + roleplay 5 + routers 5 + kg 3 + llm 2 + retrieval 2 |
| `agents/skills/` 提示词版本文件 | 22 个 | **18 个** | 6 个 skill、18 个版本文件：`body_consistency/v1-v2`、`body_repair/v3-v4`、`closed_inventory/v1-v8`、`foundation_input_health/v1`、`planning_review/v1-v2`、`prompt_health_audit/v1`、`text_quality_audit/v1`、`text_quality_repair/v1`（实为 8 组 skill / 18 个文件，组数也不是 6） |
| feat 独有后端模块 | — | **76 个**（backend/app 内） | 另有 20 个 migration、100+ 个 test 文件不计入 |

两分支共有但内容不同的表结构差异（决定移植成本）：main 独有 5 张表 `WorkflowRun`、`ActionProposal`、`BookDigest`、`CharacterTemplate`、`RoleplayMessage`；feat 独有 14 张表（含 `GenerationSnapshot`、`AiwizardNotice` 等）。**任何"保留 main 侧模块"的结论，都要连带这 5 张表的 migration。**

### 1.2 main 侧独有功能表

| # | 模块 / 模块组 | 解决什么问题 | feat 侧是否有等价能力 | 是否被实际路由/调用（代码证据） | 结论 | 理由 |
|---|---|---|---|---|---|---|
| M1 | `agents/closed_inventory.py`、`closed_inventory_v2.py` + `skills/closed_inventory/v1-v8.json`（闭集盘点） | 把"这一章里出现了哪些该被盘点的东西（实体/物件/能力）"做成**闭集清单**，再逐项核对正文是否交代，解决"写了但没盘点、盘点但没交代"的漏项 | **无**。feat 侧最接近的是 `kg/generated_extract.py`（生成层机械抽取）与 `kg/quality_report.py`，前者只记录"写了什么"、不产出"应写清单"，后者是统计报告，都不是闭集盘点 | `closed_inventory` ← `agents/diagnostic.py`、`agents/authority_body_repair.py`；`closed_inventory_v2` ← `agents/diagnostic.py`、`agents/plan_extension.py`。skills json 由 `agents/diagnostic_skills.load_skill(skill_id, version)` 按目录 + manifest 校验加载（`diagnostic_skills.py:110-140`），非 import，故 grep 显示"无引用"属正常 | **保留（高）** | `closed_inventory/v8.json` 的 `failure_boundaries` 逐条写明了"物件 vs 实体 vs 势力"的判定边界，与本项目已知的物件/持有/实体分裂问题直接对应；且它有 8 个迭代版本可追溯，是 main 侧唯一"带版本演进"的盘点逻辑 |
| M2 | `agents/body_repair.py`、`body_consistency.py`、`body_evidence.py`、`body_acceptance.py`、`authority_body_repair.py`（正文修复与体检） | 体检生成正文与"权威设定"的一致性，产出**带证据句**的修复提案（不是直接改稿）；`body_acceptance` 做接受判定，`authority_body_repair` 用权威层数据修 | **部分**。feat 有 `generation/repetition.py`（复读检测）、`generation/structure_check.py`（结构约束）、`kg/conflict_scan.py`（生成文本 vs 原著层三类冲突），但都是**检测**，没有"带证据句的修复提案 + 接受判定"闭环 | `body_repair` ← `agents/registry.py`、`routers/assistant.py`、`routers/assistant_proposals.py`；`body_consistency` ← `body_repair.py`、`diagnostic.py`、`closed_inventory.py`；`body_evidence` ← 10 处（含 `generation/timeline.py`）；`body_acceptance` ← `generation/engine.py`、`routers/assistant_proposals.py` | **保留（高）** | 这是 main 侧唯一"检测→证据→提案→接受"四段齐全的链路，且被 `generation/engine.py` 直接调用，不是侧挂 |
| M3 | `agents/decision_points.py`、`react_loop.py` | 决策点标注（哪里需要人拍板）+ ReAct 循环（工具调用-观察-再决策） | **无**。feat 的 `agents/action_catalog.py` 是"可执行操作目录（单一真源）"，只声明有什么操作，没有决策点/循环 | `decision_points` ← `agents/registry.py`、`agents/workflow.py`；`react_loop` ← `routers/assistant.py` | **保留（中）** | 都有真实入口。但 `react_loop` 只在 assistant 路由被调用一次，作用面窄；建议先保留 `decision_points`（与 M4 的 workflow 强耦合），`react_loop` 单独评估 |
| M4 | `agents/workflow.py`、`claim_guard.py`、`consent.py`、`action_proposals.py`、`derived_rebuild.py` + `routers/assistant_action_proposals.py` + 表 `WorkflowRun`/`ActionProposal` | 多步动作的编排、声明守卫（防止模型越权断言）、用户同意闸门、动作提案落库与派生重建 | **无**。feat 有 `pending_actions`（提案待确认卡片）与 `generation/patch.py`（局部补丁），但没有"多步 workflow 状态机 + 声明守卫" | `workflow` ← `action_proposals.py`、`registry.py`、`routers/assistant.py`、`routers/generation.py`、`routers/projects.py`（5 处）；`claim_guard` ← `routers/assistant.py`；`consent` ← `diagnostic.py`、`routers/assistant.py`；`action_proposals` ← `registry.py` + 2 个 router | **保留（中高）** | 被 5 处调用，是 main 侧助手链路的骨架。`claim_guard`/`consent` 是"模型不能瞎说/不能擅自改"的护栏，与本项目"禁止把 reported 断言当 value"的既有规则同源 |
| M5 | `agents/diagnostic.py`、`diagnostic_skills.py` + `skills/*.json`（技能提示词版本管理） | 把"体检/修复"的提示词做成**带 manifest、可指定版本、可校验依赖**的闭集 playbook，而不是散在代码里的字符串 | **无**。feat 的提示词都在 `generation/prompts.py` 与 `style/narrative.py` 里硬编码 | `diagnostic_skills` ← `diagnostic.py`、`registry.py`；`diagnostic` ← 7 处（`body_repair`、`closed_inventory`、`derived_rebuild` 等） | **保留（高）** | 这是**提示词版本管理与回滚能力**，`load_skill(skill_id, version)` 强制校验 `id/version/status/applicable_tasks/agent_roles/required_capabilities/deps(min_version)`（`diagnostic_skills.py:63-93`），可复现、可审计；对"规则版本来回改导致结果不可复现"这个既有痛点是对症药 |
| M6 | `agents/audit_chunk.py`、`audit_narrative.py` | 分块审计 / 叙事审计 | **部分**。feat 有 `kg/quality_report.py`（KG 质检报告）与 `style/warning_store.py`，但不对正文做分块/叙事两级审计 | `audit_chunk` ← `agents/audit.py`、`registry.py`；`audit_narrative` ← `agents/audit.py`、`workflow.py` | 保留（中） | 走 `agents/audit.py` 统一入口，属 M2/M4 链路的下游 |
| M7 | `agents/attachments.py`、`attachment_chat.py`、`ingest_clean.py` + `routers/assistant_attachments.py` | 附件（长文/文件）入库清洗与"对着附件聊" | **部分**。feat 有 `routers/assistant_clean.py`（`POST /api/assistant/clean-corpus`，语料清洗）与 `routers/novels.py` 上传，但没有"附件会话" | `attachments` ← `attachment_chat.py`、`registry.py`、`routers/assistant_attachments.py`；`attachment_chat` ← `routers/assistant.py`；`ingest_clean` ← `registry.py`、`ingestion/store.py`、`routers/assistant_attachments.py` | 待定 | 能力真实存在且已路由，但与本项目当前"本地语料库 + 蒸馏"主流程重叠度高、且 `attachments` 依赖前端单独面板；**建议排在 M1/M2/M5 之后** |
| M8 | `agents/plan_extension.py` + `routers/projects.py` 的 `POST /plan-extension` | 按"外扩 N 章"续写计划，且复用闭集盘点做前置校验 | **无**。feat 的续写靠 `retrieval/brief/*` + `generation/progress_ledger.py`，没有"计划外扩"接口 | `plan_extension` ← `action_proposals.py`、`routers/projects.py` | 保留（中） | 有独立端点；但依赖 M1/M4，单独移植价值低 |
| M9 | `generation/` 21 个模块：`background`、`candidate`、`character_card(_gen)`、`ctx_profile`、`cultivation`、`dedup`、`description_elements`、`digest_context`、`persona`、`planning`、`plot_structure`、`power_scale_gen`、`power_system(_gen)`、`profiles`、`rewrite`、`scene_contract`、`tiers`、`verbatim`、`visibility` | 设定层：世界背景硬规则、角色卡/个性/修炼档案、战力体系与量级、生成模式上下文策略、候选打分、逐字照搬检测、可见性裁剪 | **多数无**。feat 的 `generation/` 只有闸门与台账（repetition/scope_guard/structure_check/author_research/progress_ledger/patch/power_prompt），**整套"设定编辑"后端不存在** | 全部有引用：`engine.py`（ctx_profile/rewrite/verbatim/visibility/candidate/dedup）、`routers/projects.py`（character_card/gen、persona、power_system/gen、cultivation、scale、background）、`routers/wizard.py`（background/persona/plot_structure/planning）、`routers/settings.py`（tiers） | **保留（高，但需拆分）** | 这是 main 侧最大的可用资产，对应 main 前端 ProjectWorkspace 的"设定编辑台"。**逐模块取舍**：`verbatim`（照搬闸门）、`visibility`（缺来源即排除）、`dedup`、`candidate` 应优先；`character_card*`/`power_system*`/`scale`/`cultivation` 属产品功能，按产品意愿定 |
| M10 | `style/` 20 个模块：`attribution(_fix)`、`beat(_insert)`、`bio_evidence`、`chapter_stats`、`character_profile`、`character_template`、`digest`、`ellipsis_fix`、`entities`、`entity_judge`、`entity_store`、`forbidden_usage`、`form`、`form_merge`、`layers`、`lead_in`、`scope`、`spec` | 文风层：对白归属归正、节拍插入、引导语补写、省略号/形态校正、专名识别与判定、禁用词语法角色判定、文风规格书（闭集目标）、章长统计、角色档案蒸馏 | **部分**。feat 有 `style/consistency.py`（文风一致性）、`style/stylometry.py`、`style/exemplar.py`、`style/pipeline.py`、`style/narrative.py`（巨型文件），**无** attribution/beat/lead_in/ellipsis_fix/form/entity_judge/spec | 全部有引用，核心落点：`generation/engine.py`（attribution_fix、beat_insert、ellipsis_fix、form、form_merge、lead_in、bio_evidence、spec）、`generation/prompts.py`（attribution、spec、character_profile）、`style/narrative.py`（layers、scope、entity_store） | **保留（中高，挑 5 个）** | 与 feat 的文风路线是**互补**而非重复。优先移植：`spec.py`（把档案统计变成机器可验的闭集目标——正好补 feat"指标系统性偏离靶值"的教训）、`entity_judge`/`entity_store`、`forbidden_usage`、`attribution`。`beat_insert.py` 文件头自己写着"默认关闭——参照物曾经是错的"，**不建议移植** |
| M11 | `roleplay/` 5 个 + `routers/roleplay.py` + 表 `RoleplayMessage` | 与角色对话（roleplay），带 bio/roster/evidence | **无** | `roleplay` 全部 ← `routers/roleplay.py`（`main.py` 挂载）；`roster` ← 只有 router；`bio`/`evidence` ← `roleplay/chat.py` | 待定 | 是一个完整但**孤岛**的产品功能，与生成/KG 主链无耦合。移植成本低（自带 router + 1 张表），但产品价值需用户拍板 |
| M12 | `routers/digest.py`、`style/digest.py`、`generation/digest_context.py`、`style/character_profile.py` + 表 `BookDigest` | 把整本书压成结构化摘要（时间线/角色生平/情境反应），并在生成时按纪律裁剪后进上下文 | **无**。feat 的上下文靠 `retrieval/brief/*` + `kg/layer_context.py`（按章 1-2 跳邻域），没有整书摘要层 | `routers/digest` ← `main.py`、`verify_book_digest.py`、`closed_inventory*`、`body_consistency`、`derived_rebuild`、`generation/digest_context.py`、`style/jobs.py`（**9 处**）；`digest_context` ← `engine.py`、`roleplay/chat.py` | **保留（高）** | 被 9 处引用，是 main 侧"长上下文"的地基；feat 目前靠逐章邻域，跨 200+ 章时会有上下文窗口压力 |
| M13 | `llm/capability.py`、`llm/thinking_models.py` + `routers/assistant_probe.py` + `LlmRole.thinking` | 每个模型/通道的"思考档位"能力登记 + 型号下限表 + 思考探针；用户可按角色设置思考档位 | **无**。feat 的 `llm/provider.py`/`stream.py` 只是**硬编码**一份"老模型一律关思考"的名单，用户不可调、无探针 | `capability` ← `agents/registry.py`、`routers/assistant_probe.py`；`thinking_models` ← `llm/provider.py`、`routers/settings.py` | **保留（高）** | 与本项目已记录的"思考占满 max_tokens 导致正文 0 字"问题直接相关：feat 只是绕过去了，main 给了可控旋钮 + 探针。**注意 `thinking` 是 `LlmRole` 的字段，需核对 feat 侧该表是否需要加列**（本次未逐列核对，标为移植时必查项） |
| M14 | `generation/tiers.py` + `POST /settings/generation-tier` | 按"花费/速度"分三档的生成配置预设 | **无** | `tiers` ← `routers/settings.py`（唯一入口） | 保留（中） | 单入口、实现小；与 M9 的 `profiles.py` 同批移植更顺 |
| M15 | `kg/foreshadow_cleanup.py`、`foreshadow_quality.py`、`chapter_evidence.py` | 伏笔清理与质量判定、章级证据 | **部分**。feat 有 `kg/conflict_scan.py`、`kg/quality_report.py`，但无伏笔专项 | `foreshadow_cleanup` ← `main.py`、`kg/judge.py`、`kg/retrieval_v3.py`；`foreshadow_quality` ← 6 处（`extractor_v3.py`、`batch_worker.py`、`generation/prompts.py` 等） | 保留（中） | 已接进 feat 同名的 `kg/judge.py`/`retrieval_v3.py` 流程，移植面窄 |
| M16 | `retrieval/strategies.py`、`retrieval/views.py` | 检索策略注册 + 视图（不同生成模式用不同检索面） | **部分**。feat 的 `retrieval/` 是五级目录 + brief 外部检索，机制完全不同 | `strategies` ← `generation/engine.py`、`roleplay/evidence.py`；`views` ← `engine.py`、`retrieval/search.py`、`style/exemplar.py`、`style/narrative.py` | 待定 | 与 feat 的检索架构**互斥**，移植等于两套检索并存；建议只抽 `views` 的"按模式裁剪"思想，不搬实现 |
| M17 | `routers/assistant_attachments.py`、`assistant_action_proposals.py`、`assistant_probe.py` | 附件/动作提案/思考探针的 HTTP 面 | 无 | 三者都 ← `backend/app/main.py`（挂载即路由） | 随 M7/M4/M13 一起 | 自身无逻辑，是宿主 router |
| M18 | 表 `WorkflowRun`、`ActionProposal`、`BookDigest`、`CharacterTemplate`、`RoleplayMessage` | M4/M12/M9/M11 的持久化 | 无 | `models.py` 定义 | 随宿主模块 | 单独存在无意义，**但必须随宿主一起进 migration，否则上述模块全部跑不起来** |

### 1.3 feat 侧独有功能表

| # | 模块 / 模块组 | 解决什么问题 | main 侧是否有等价能力 | 是否被实际调用（代码证据） | 结论 | 理由 |
|---|---|---|---|---|---|---|
| F1 | `agents/action_catalog.py`、`agents/inspection.py` | 助手可执行操作目录（单一真源）+ 作者 agent 轨迹的只读监察 | **无** | `action_catalog` ← `registry.py`、`routers/assistant.py`；`inspection` ← `registry.py` | **保留（高）** | 是 feat 助手链路的路由表；main 的 `workflow.py` 是另一种编排范式，**不能互相替代**（合并时要显式选一种，否则两套并存） |
| F2 | `agents/dialog/snapshot.py`、`rollback.py` + `routers/assistant_snapshot.py` | 改写前后的快照与回滚（版本化，不物理删） | **部分**。main 有 `routers/assistant.py` 的 `/assistant/versions*`（内容版本 + diff + restore），语义相近但粒度不同 | `dialog/snapshot` ← `main.py`、`engine.py`、`routers/assistant_snapshot.py`、`routers/assistant_generation.py`；`rollback` ← `routers/assistant_snapshot.py` | **保留（高）** | 这是 main 缺失的整块能力；两套版本机制需要合并成一套（见前端表 AssistantWorkspace） |
| F3 | `wizard_flow/chat/actions/facts/story/market` 6 个 + `routers/assistant_wizard.py` + `assistant_store.py` | 对话式"一步步生成"的状态机、自由输入解析、受控写操作、可核查事实层、题材行情卡 | **无** | 全部 ← `routers/assistant_wizard.py`（`main.py` 挂载）；`wizard_market`/`wizard_story` ← `wizard_chat.py`/`wizard_flow.py` | **保留（高）** | feat 独有且自成体系；`wizard_market` 注释明确"不进文风规则库、不进蒸馏档案"，边界设计清晰 |
| F4 | `kg/layers.py`、`canon_source.py`、`book_scope.py`、`generated_store.py`、`generated_extract.py`、`layer_context.py`、`layer_pipeline.py`、`holding_projection.py` | **KG 双层**（原著层 / 生成层）隔离、canon 词表唯一读取点、按书作用域、生成层独立存储与持有投影 | **无**。main 的 `kg/` 只有 3 个模块，没有分层概念 | `canon_source` ← `main.py`、`batch_worker.py`、`conflict_scan.py`、`generated_extract.py`、`layer_context.py` + 全部 migration；`layer_context` ← `layer_pipeline.py`；`holding_projection` ← `batch_worker.py` | **保留（高）** | 这是 feat 路线的**核心架构**，且直接落地了本项目"旧图只读、新图独立"的硬约束 |
| F5 | `kg/item_holding_read.py`、`item_generic.py`、`item_category.py`、`info_carrier.py` | 物件/持有/信息载体的读取与归类 | **无** | `item_holding_read` ← `generation/engine.py`（**进生成链路**）+ 3 个 migration verify；`item_generic` ← `canon_source.py` + 2 migration；`item_category` ← migration | **保留（高）** | 已被 `generation/engine.py` 消费，是 KG 真正进入生成的地方（对应项目记忆里"KG 净增 +4 条真事实"的那条路） |
| F6 | `kg/character_edit.py`、`character_rename.py`、`conflict_scan.py` | 角色增删改境界、改名影响面评估、生成文本 vs 原著层冲突检测 | **无**（main 的角色编辑走 `generation/character_card*.py`，是另一套数据源） | `character_edit`/`character_rename` ← `routers/projects.py`（`/bible/characters/*`）；`conflict_scan` ← `layer_pipeline.py` | **保留（高）** | 有独立端点 + 前端模态，是 feat 前端 `ProjectWorkspace` 三个独有模态的后端 |
| F7 | `kg/era_check.py`、`era_guard.py`、`era_prompt.py` | 时代越界护栏：提示块 + 生成后自检 | **无** | `era_check` ← `generation/engine.py`；`era_guard` ← `era_check.py`、`era_prompt.py`；`era_prompt` ← `era_check.py` | 保留（中） | 已进 `engine.py`；main 侧无对应概念 |
| F8 | `kg/story_context.py`、`shadow.py`、`quality_report.py`、`entity_lookup.py`、`alias_rules.py` | 三类消费视图、影子库、质检、实体查表、化名判据 | **部分**（main 的 `style/entity_store.py`、`entity_judge.py` 做专名判定，但不在 kg 层） | `story_context` ← `routers/kg_v3.py`、`export_story.py`；`shadow` ← `main.py`、`engine.py`；`quality_report` ← `batch_worker.py` | 保留（中） | `alias_rules` 只被 `experiments/` 引用，**属半个死代码**，需要确认是否已内联 |
| F9 | `retrieval/catalog_config/paths/store.py`、`skeleton.py`、`tools.py`、`trace.py`、`budget.py`、`adoption.py`、`anchor.py`、`l4_lexicon.py`（五级目录检索） | L1–L5 路径化检索、静态骨架、五个只读工具、调用轨迹、预算软着陆、规则采用判定 | **无** | `skeleton` ← `engine.py`；`tools` ← `registry.py`、`author_research.py`；`trace` ← `registry.py`、`tools.py`、`adoption.py`；`catalog_store` ← 4 处 | **保留（高）** | feat 检索路线的骨架，`tools` 同时服务作者 agent 与助理 agent（共用实现） |
| F10 | `retrieval/brief/*` 8 个 | 外部素材检索（可替换后端、无头浏览器、缓存、素材库、渲染、设置、工具） | **无** | `brief` ← `engine.py`；`tool` ← `engine.py`；`library`/`cache`/`render`/`backend`/`browser` 互相引用 | 待定 | 注释首行写"独立模块，**默认关闭**"。是能力但默认不启用，且引入 Playwright 依赖。**建议保留但维持默认关闭** |
| F11 | `generation/repetition.py`、`scope_guard.py`、`structure_check.py`、`author_research.py`、`progress_ledger.py`、`patch.py`、`power_prompt.py` | 生成闸门（复读/越界/结构）、写后自查回路、逐章推进台账、局部补丁、量级提示块 | **部分**。main 的 `style/dedup`→feat 无（main 的 `generation/dedup.py` 才是对应物）、`generation/verbatim.py`（照搬）feat 无 | `repetition` ← `engine.py`、`routers/generation.py`、`assistant_generation.py`、`dialog/rollback.py`；`structure_check` ← `engine.py`、`progress_ledger.py`；`patch` ← `engine.py`、`routers/assistant_generation.py` | **保留（高）** | 全部进 `engine.py`，是 feat 生成质量的实际控制点 |
| F12 | `coherence.py`、`health.py` | 通用一致性检查（coherence-v1）、健康检查 | 无 | `coherence` ← `engine.py`、`progress_ledger.py`、`kg/batch_worker.py`、`verify_progress_ledger.py`；`health` ← `main.py` | 保留（中） | 轻量、已被调用 |
| F13 | `style/consistency.py` | 文风一致性 | **无**（main 用 `style/spec.py` + `pipeline.py` 走另一套） | ← `engine.py`、`generation/prompts.py`、`style/narrative.py` | 保留（中） | 与 main 的 `spec.py` 功能有重叠，合并时需二选一或串起来 |
| F14 | `agents/ai_flavor_memory.py` | AI 腔反馈闭环的持久化底座 | **无** | ← `registry.py`、`routers/assistant_generation.py` | 保留（中） | 对应本项目"去 AI 味"的长期诉求 |
| F15 | `kg/corpus_ingest.py`、`export_story.py`、`rebuild_chapter.py`、`relabel_misplaced.py`、`relation_backfill_rollback.py`、`retrieval/build_cards.py`、`generation/formulaic.py` | — | — | **7 个模块在 feat 树内零 import/零字符串引用**（`build_cards` 只被自身目录内引用；`rebuild_chapter` 等为一次性修复 CLI） | 舍弃（可归档） | 纯死代码或一次性脚本。`rebuild_chapter`/`relabel_misplaced` 类如仍需要，应移入 `migrations/` 而不是留在 `app/` |

### 1.4 特别判断：用户点名的四处是否值得移植

| 议题 | 判断 | 依据 |
|---|---|---|
| main 的**闭集盘点**（`closed_inventory` 系列） | **值得移植，优先级最高** | ① 被 `diagnostic.py`、`plan_extension.py`、`authority_body_repair.py` 三处调用，非侧挂；② feat 侧无等价物（`generated_extract` 只记录不盘点）；③ 8 个版本文件带 `failure_boundaries`，正好覆盖本项目已知的"物件 vs 实体 vs 势力"混淆；④ 移植代价 = 2 个 py + 8 个 json + 依赖 M5 的 loader |
| main 的**正文修复与体检**（`body_*`） | **值得移植**，但只搬 `body_consistency` + `body_evidence` + `body_repair` + `body_acceptance` 四件；`authority_body_repair` 视权威层数据是否可对齐再定 | ① 四件均被 `registry.py` / `routers/assistant_proposals.py` / `generation/engine.py` 调用；② 与 feat 的检测类模块（repetition/structure_check/conflict_scan）互补，不冲突；③ 需要 M18 的 `ActionProposal` 表 |
| main 的**决策点与 ReAct 循环** | **`decision_points` 值得，`react_loop` 待定** | `decision_points` 是 `workflow.py` 的输入层，`workflow` 被 5 处调用；`react_loop` 只有 `routers/assistant.py` 一处入口，且和 feat 的 `action_catalog` + `wizard_actions` 在"谁来决定下一步动作"上**语义重叠**，两套并存会打架 |
| main 的**技能提示词版本管理**（`agents/skills/`） | **值得移植，且建议优先于上面三者** | 它是 M1/M2/M5 的**承载体**：不先有 `diagnostic_skills.load_skill` 的 manifest 校验，搬过来的 json 只是一堆没人读的文件。而且它解决的是"规则改了无法复现"这个贯穿全项目的元问题 |
| feat 侧被 main 缺失的能力（反向） | **F4 双层 KG、F5 物件持有读取、F2 快照回滚、F3 向导、F9 五级目录检索** 五项均为 main 完全不存在 | 见 1.3 表；这五项应视为"合并后不可丢失"的核心资产，任何以 main 为骨架的重构都必须先确认它们仍在链路上 |

### 1.5 合并时的结构性冲突（必须先拍板）

1. **两套"下一步做什么"机制**：main `workflow.py` + `decision_points.py` vs feat `action_catalog.py` + `wizard_actions.py`。二者都想当助手侧的唯一编排真源，**不能并存**。
2. **两套版本/回滚**：main `/assistant/versions*` vs feat `dialog/snapshot.py` + `routers/assistant_snapshot.py`。
3. **两套检索**：main `retrieval/strategies.py` + `views.py` vs feat 五级目录 + brief。
4. **两套文风规格**：main `style/spec.py` vs feat `style/consistency.py`。

---

## 二、前端 18 个冲突文件

两分支 `frontend/src` 共 23 个共同文件，其中 **18 个内容不同**，5 个完全相同（`composables/useAsync.ts`、`composables/useLlmConfig.ts`、`main.ts`、`theme.ts`、`utils/llmRoles.ts`）。

> 结论列只用四档：取 feat / 取 main / 手工合并 / 等价。

| # | 文件 | 功能是否相同 | 细节差在哪（证据） | 结论 | 依据（行为正确性 / 接口匹配 / 可维护性） | 移植依赖 |
|---|---|---|---|---|---|---|
| 1 | `App.vue` | 基本相同（同一侧栏 + topbar 骨架） | 差异**只有登录块**：main 有 `login-shell` 分支、`user-area`（会话标记 + 头像 + 退出）、`crumb` 默认值"工作台"、侧栏 `sidebar-note`；feat 无登录、`crumb` 默认 `""` 回落到"概览"、多 `topbar-note`、设置图标换成新路径 | **取 feat** | 行为正确性：main 的登录是 `utils/auth.ts` 里 26 行的 localStorage 用户名（无密码、无后端、无鉴权），只增加全局闸门；可维护性：feat 的图标与 `topbar-note` 是当前设计系统产物。若产品确需"谁在用"，再手工加回登录块（代价见 §3.1） | 无（不移植登录则零依赖） |
| 2 | `api/client.ts` | 不同（main 方法更多） | main 119 个方法 / feat 78 个；**main 独有 65 个**，全部指向 main 独有端点（background/persona/characterCard/powerSystem/scale/cultivation/workflow/proposal/actionProposal/roleplay/attachments/digest/assistantRuns/thinking-probe/styleNarrative/generationTier 等）；**feat 独有 24 个**（`renameBibleCharacter`/`addBibleCharacter`/`removeBibleCharacter`/`setBibleCharacterRealm`、`resumeDistill`、`cleanCorpus`、快照 4 个、`applySelectedProposals`、wizard 系列 13 个）。**共有方法里未发现路径漂移**，只有 `buildKg`（`/build-kg`）与 `getReminders` 被 feat 移除 | **手工合并**（以 feat 为底，按需追加） | 接口匹配：逐条核对过，main 独有方法在 feat 后端**大部分不存在**（如 `/character-cards`、`/workflow`、`/style-narrative/*`、`/digests`），直接搬 client 方法会产生 404 死接口；feat 独有方法在 main 后端也不存在。反向确认：`/assistant/runs`、`/assistant/versions`、`/reminders`、`/export-txt` 这几个 main 前端用到的方法，**feat 后端已有端点**，只是 client 缺方法 | 见 §3 |
| 3 | `api/types.ts` | 不同 | main 独有类型集中在：角色卡/个性/战力体系/量级/修炼、`DigestOut`/`DistillScope`、`WorkflowState`、`Roleplay*`、`AssistantRunDetailResponse`、`CorpusCleanReport`/`ForeignBlocksReport`、`CharacterTemplate`；feat 独有：`Wizard*`、`AssistantSnapshot*`/`Rollback*`、`KgCharacterEditResult`/`RenameResult`、`KgRebuildResult`、`AssistantCleanCorpus*`、`active_novel_id`/`continuation_novel_id`。**同名类型字段不同**：`NovelOut`（main 多 `max_chapter_no`/`clean_report`/`story_authority`/`reference_cutoffs`）、`ProjectOut`、`ProjectCreate`/`ProjectPatch` | **手工合并** | 接口匹配：类型必须跟各自后端的 schema 走，main 的 `clean_report`/`story_authority` 字段在 feat 的 `schemas.py` 里不存在，直接合并会让前端读到 `undefined`；可维护性：以 feat 为底、按"后端已移植"逐组追加，是唯一不会产生幻影字段的顺序 | 见 §3 |
| 4 | `router/index.ts` | 不同 | main 多 `/login` 路由 + 全局前置守卫（未登录 `redirect` 到 login）；feat 无守卫，路由表基本一致 | **取 feat** | 行为正确性：main 的守卫把整个应用挡在一个纯前端用户名后面，无安全收益却引入"清 localStorage 就被踢出"的运维问题；接口匹配：无后端鉴权端点可对接 | 无 |
| 5 | `views/ProjectWorkspace.vue` | **不同（两套不同定位）** | 两边共用同一套"七步向导"，但：**main 多**——创作板块/角色板块切换、世界背景与硬规则、大纲生成-澄清-应用（`generateOutline`/`clarifyOutline`/`applyOutline`）、角色卡编辑（人写优先、AI 只补空）、量级编辑、修炼档案、个性编辑、蒸馏范围（`DistillScope`）、文风档案检查、纯量读数、导出对话框；**feat 多**——`plot_bible` 级联协议编辑器（`applyBible`）、角色增删改名改境界四个模态（`/bible/characters/*`）、重写快照与分组回滚（`listSnapshots`/`rollbackSnapshot`/`rollbackSnapshotGroup`）、待应用提案面板（`listProposals`/`approveProposal`/`applySelectedProposals`）、流式手稿台（`generate-stream`） | **手工合并**（两个方向都可行，需拍板） | 接口匹配：main 那 27 个设置类调用对应的 `/projects/{id}/background`、`/character-cards*`、`/power-system*`、`/cultivation*`、`/scale*`、`/style-narrative/*`、`/workflow`、`/plan-extension` 在 **feat 后端全部不存在**（`projects.py` main 48 个端点 vs feat 25 个），所以"直接取 main"必须有后端先行；反之 feat 独有的四个模态与提案面板只依赖 feat 已有端点。行为正确性：两边的"生成控制台"细节（问题清单/预算条/分层构成/独立算子/段落分页/单段重生成）**未逐项实跑对比**，谁更正确 → 无法判定，需要实跑对比 | 取 main 为骨 → 需移植 §3.2 的 27 个端点 + M9；取 feat 为骨 → 需补大纲生成/背景/角色卡/战力/修炼五组 UI，同样依赖 M9 |
| 6 | `views/ProjectsView.vue` | 基本相同（都是"建新书 + 书稿列表"） | feat 多：搜索框、状态筛选（all/active/completed）、`errorText()` 分类错误、骨架屏、空态/筛选空态分离、`createProject({mode:"quick", target_length:5000})`；main 多：列表行内"助手""图谱"两个直达按钮（`router.push` 到 `/projects/{id}/assistant`、`/kg`）、`createProject` 传完整参数 | **手工合并**（取 feat 为骨，补 2 个按钮） | 行为正确性：feat 的筛选/空态/错误分类是净增益；main 的两个直达按钮是真实便利性。接口匹配：两者都只调 `listProjects`/`createProject`，feat 后端两者都在 | 补 2 个按钮 = 0 后端依赖 |
| 7 | `views/MyNovels.vue` | **相同**（同样的 `refresh`/`openProject`/`goCreate` 三个函数、同样只调 `listProjects`） | 纯视觉差：main 用 `.novel-item` 三列网格 + `section-index` 编号 + `finished-row` 列表；feat 用全局 `.card`/`.state-panel`/骨架屏 | **等价**（选 feat） | 可维护性：feat 那份依赖全局 token 与公共状态组件，与其余 17 个文件同一套设计系统；取 main 会把旧视觉体系带回来 | 无 |
| 8 | `views/AssistantWorkspace.vue` | **不同（两套助手）** | main：只读**历史面板**（运行记录 `assistantRuns`/`assistantRunDetail` + 内容版本 `listVersions`/`diffVersions`/`restoreVersion`，含两版对比、恢复前哈希漂移保护）、`ReactNotice` 块、提案草稿就地编辑（`draft`/`draftField`）；feat：**向导**（`startWizard`/`answerWizard`/`sayWizard`/`backWizard`/`doneWizard`/`runWizard`/`undoWizard`/`wizardRoleChat`、`WizardStepCard`）、工具轨迹（`toolTrace`/`toolResults`/`toolFallbackReason`）、通知（`listWizardNotices`/`ackWizardNotice`） | **手工合并**（取 feat 为骨 + 补历史面板） | 接口匹配（关键）：`/api/assistant/runs`、`/api/assistant/runs/{id}`、`/api/assistant/analyze-logs`、`/api/assistant/versions`、`/versions/diff`、`/versions/{id}/restore` **在 feat 后端全部已存在**（`routers/assistant.py` 与另一 router 共同提供），所以历史面板是**纯前端移植**；可维护性：feat 的 wizard 与 toolTrace 是独有能力，不可丢 | 0 个新后端端点；需在 `client.ts` 补 4 个方法、`types.ts` 补 `AssistantRun` 系列 |
| 9 | `views/AssistantResolver.vue` | 相同（都是"拿不到 id 就跳默认助手页"） | main 用 `onMounted` 一次性同步解析、失败无 UI；feat 抽出 `async resolveAssistant()`，带 `loading` 骨架 + `error` 状态面板 + "重新加载"重试 | **取 feat** | 行为正确性：feat 有失败可恢复路径，main 失败即白屏；可维护性：feat 的三态（加载/错误/就绪）是其余视图的统一写法 | 无 |
| 10 | `views/CorpusView.vue` | **不同（各有一半）** | main：语料库列表 + **入库体检报告**（`clean_report`、外来块 `ForeignBlocksNotice`）+ **全书摘要**（`startDigest`/`getDigest`）+ 蒸馏任务进度 + 跳助手；feat：语料库列表 + **上传**（`uploadNovel`）+ **语料清洗**（`cleanCorpus` + 二次确认 `cleanConfirmOpen`）+ 搜索 | **手工合并** | 接口匹配：feat 后端有 `/api/assistant/clean-corpus`（`routers/assistant_clean.py`）与 `/novels/upload`，但**没有** `/api/digest` 与 `clean_report` 字段；`assistant_clean.py` 是两分支共有文件，所以 main 后端也有 clean-corpus 端点——main 的差异只在**前端没用它**。即"清洗"是 feat 前端独有、"体检+全书摘要"是 main 前后端成套独有 | feat 清洗块 = 0 依赖；main 的体检+摘要 = 需移植 M12（digest router + `BookDigest` 表 + `clean_report` 字段） |
| 11 | `views/KgWorkspace.vue` | 相同（同 7 个 tab、同 `kgV3StyleWarnings` 等 15 个 API、同惰性加载） | 差异极小：feat 的 Style 审计 tab 标题带 `v2.19 bucket chip`、`v2.20 max_tokens` 列；main 的 worker 状态文案多一个 `interrupted`（"已中断"）分支。**注意**：feat 后端 `kg_v3.py` 是否返回 `interrupted` 字段未逐字段核对 | **取 feat** | 接口匹配：两边调的 API 集合完全相同，feat 的新列为增量展示；行为正确性：唯一疑点是 `interrupted` 文案，属可补的一行 | 若补 `interrupted`，需先确认 `kg_v3` worker-status 返回该字段（未核对，标为待查） |
| 12 | `views/LlmSettings.vue` | **不同** | main 多"生成档位"切换（`getGenerationTiers`/`setGenerationTier`，按花费/速度三档）；feat 无 | **取 main**（补这块） | 接口匹配：`/settings/generation-tier(s)` 在 feat 后端**不存在**（feat `settings.py` 3 个端点，main 5 个）；行为正确性：main 那份的其余部分（分角色 `saveLlmConfig`）两边一致 | 需移植 M14（`generation/tiers.py`）+ 2 个 settings 端点 |
| 13 | `components/AssistantIssueList.vue` | 相同（同 props、同 emit） | main 有 `issueTypeLabel()`，把内部 `issue.type` 枚举映射成中文；feat 删掉该函数，直接渲染 `issue.type` 原值 | **取 main** | 行为正确性：`type` 是后端内部枚举（如 `closed_inventory_gap`），直出对用户不可读，是展示层退化；可维护性：标签映射收敛在组件内，无副作用 | 0（纯前端函数） |
| 14 | `components/DistillProgress.vue` | **不同（进度语义不同）** | main 用"阶段化"进度（`stage` + 当前/总阶段），并接受 `title` prop 以便被"书稿蒸馏"和"全书摘要"共用；feat 用"块"进度（`progress_current/progress_total` = 块数），标题硬编码"文风蒸馏"，用 `<progress>` 元素 | **取 feat** | 接口匹配（决定性）：feat 的 `style/jobs.py` 把 `progress_total` 写成**块数**，main 的写成**阶段数**；在 feat 后端上渲染 main 那份会显示错误分母。可维护性：feat 无第二个消费者（没有 digest），`title` prop 是为 main 的 digest 服务的 | 若移植 M12 的 digest，再补 `title` prop |
| 15 | `components/RoleModelPanel.vue` | **不同** | main 有 per-role **思考档位**控件（`payload.thinking`，取自后端 `providers[channel].thinking`）；feat 删除该控件 | **取 main** | 接口匹配：`thinking` 需要 feat 后端 `POST /settings/llm-config` 接受该字段，以及 `llm/capability.py`/`thinking_models.py` 提供的可选值；行为正确性：feat 只是"老模型一律关思考"的硬编码名单，无法按角色调 | 需移植 M13（2 个 llm 模块 + `assistant_probe` router + settings 的 thinking 字段）。**移植时必查**：`thinking` 在 main 是 `LlmRole` 的字段还是配置 JSON 字段（本次未逐列核对） |
| 16 | `style/base.css` | 不同（两套视觉体系） | feat 是 token 化的设计系统：侧栏 232px + sticky + `--bg-sunken`/`--accent-soft`/`--accent-line`、`.link-btn` 已**上提为全局**（`base.css` 内定义）、`.chip`/骨架屏/`.state-panel`；main 保留硬编码深色侧栏 244px、`.link-btn` 仍散在组件里、独有 `.login-shell`/`.brand-copy`/`.user-area`/`.user-chip`/`.session-state`/`.logout-btn`/`.sidebar-note` | **取 feat**（如需登录再从 main 补 7 个类） | 可维护性（决定性）：`.link-btn` 上提后，各组件内的重复定义才能删；取 main 会让其余 17 个文件的 token 依赖全部落空。行为正确性：无差异 | 若移植登录 → 补 7 个类 |
| 17 | `style/tokens.css` | 不同（feat 是超集） | feat 多 `--ink`/`--ink-line`/`--paper`/`--paper-muted`/`--font-display`/`--ease-out-quart`/`--gap-xs|sm|md`，并保留 main 的 `--gap`/`--gap-lg` | **取 feat** | 可维护性：纯超集，无破坏性；行为正确性：无差异 | 无 |
| 18 | `utils/assistantDisplay.ts` | 不同（feat 少两类映射） | feat 的 `pendingActionKindLabel` 只处理 2 种 kind（`proposal`、`invoke_generation`），`pendingActionRiskMeta` 只有默认分支"中风险 · 追加新段落"；main 处理 6 种（`insert_segment`/`body_patch`/`body_repair`/`ingest_attachment`/`action_proposal`/`proposal`）并有 `issueTypeLabel` | **取 main**（拣需要的分支） | 行为正确性（已定位到具体缺陷）：feat 的 `AssistantWorkspace.vue` 对**所有**卡片调用 `pendingActionRiskMeta(card.action)`，而后端会下发 `kind="proposal"`，于是改写提案卡片被渲染成"中风险 · **追加新段落**"——文案与实际动作不符，是真实缺陷。main 侧的 `body_repair`/`ingest_attachment` 等分支依赖 main 独有 kind，需按实际保留的 kind 裁剪 | 0（纯前端）；仅保留 feat 后端真实会下发的 kind 分支 |

### 前端结论汇总

- **取 feat（7 个）**：`App.vue`、`router/index.ts`、`views/AssistantResolver.vue`、`views/KgWorkspace.vue`、`views/MyNovels.vue`（等价）、`style/base.css`、`style/tokens.css`
- **取 main（4 个）**：`views/LlmSettings.vue`（补生成档位）、`components/AssistantIssueList.vue`、`components/RoleModelPanel.vue`（补思考档位）、`utils/assistantDisplay.ts`
- **取 feat + 小补丁**：`components/DistillProgress.vue`（保持块进度语义）
- **手工合并（5 个）**：`api/client.ts`、`api/types.ts`、`views/ProjectWorkspace.vue`、`views/ProjectsView.vue`、`views/AssistantWorkspace.vue`、`views/CorpusView.vue`（实为 6 个，其中 CorpusView 属"各有一半"）

> 即 **"取 main 更好" 的文件是 4 个**（LlmSettings、AssistantIssueList、RoleModelPanel、assistantDisplay），另有 1 个（DistillProgress）明确**不能**取 main。

---

## 三、main 独有三块能力的移植工作量

### 3.1 登录占位

- 实现：`utils/auth.ts`（26 行，localStorage 存用户名，无密码/无请求）+ `views/LoginView.vue` + `router/index.ts` 全局前置守卫 + `App.vue` 登录分支与用户区 + `base.css` 7 个类。
- **后端依赖：无。** 不需要任何接口，也不产生鉴权。
- 工作量：小（5 个前端文件）。但**它不提供任何安全能力**，只是在本地把整个应用锁在一个可随意填入的名字后面。建议不移植；若确实需要"谁在用"的标识，应改为"昵称展示"而非"路由闸门"。

### 3.2 导出三件套

- 实现：`utils/projectExports.ts`（143 行，纯前端把数据拼成 Markdown + `Blob` 下载）+ `ProjectWorkspace.vue` 的「导出作品」对话框。
- 三件 = **大纲 / 人物表 / 坑清单**（不含正文 txt；正文导出 feat 已有 `api.exportTxt` → `GET /projects/{id}/export-txt`）。
- 后端依赖（按件拆）：

| 件 | 数据来源 | feat 后端是否支持 | 需动的接口 |
|---|---|---|---|
| 大纲 | `project.outline`（`GET /projects/{id}`） | **已支持** | 无 |
| 坑清单 | `Reminders`（`GET /projects/{id}/reminders`） | **端点已存在**，但 feat 的 `client.ts` 把 `getReminders` 删了 | 只需在 `client.ts` 补 1 个方法 + `types.ts` 补 `Reminders` |
| 人物表 | `CharacterCardItem[]`（`GET /projects/{id}/character-cards`） | **不支持**（main 独有端点 + `CharacterCard`/`CharacterTemplate` 表） | 要么移植 main 的 `character-cards` 端点族（`GET/POST /character-cards`、`/character-cards/generate`、`/character-cards/batch` + `generation/character_card.py`、`character_card_gen.py`、`persona.py` + `CharacterTemplate` 表），要么改数据源为 feat 的 `plot_bible.characters`（属改造而非移植） |

- 工作量：**大纲+坑清单 = 纯前端小改动（0 新端点）；人物表 = 中到大（一整个角色卡子系统）**。建议先上"大纲 + 坑清单"两件。

### 3.3 执行历史面板

- 实现：`AssistantWorkspace.vue` 内的只读历史面板（运行记录 + 内容版本两栏，含两版对比与恢复），加上 `client.ts` 的 5 个方法。
- **后端依赖：全部已存在。** 已逐条核对 feat 后端：
  - `GET /api/assistant/runs`、`GET /api/assistant/runs/{run_id}`、`POST /api/assistant/analyze-logs` → feat `routers/assistant.py` 有；
  - `GET /api/assistant/versions`、`POST /api/assistant/versions/diff`、`POST /api/assistant/versions/{id}/restore`、`GET /api/assistant/prompts/versions` → feat 侧 router 有。
- 需动的接口：**0 个新端点**。只需：
  1. `client.ts` 补 `assistantRuns` / `assistantRunDetail` / `listVersions` / `diffVersions` / `restoreVersion`；
  2. `types.ts` 补 `AssistantRun`（feat 已有该类型）/ `AssistantRunDetailResponse` / 版本相关类型；
  3. `AssistantWorkspace.vue` 加历史面板区块。
- 需要产品侧先拍板的一点：feat 已有 `dialog/snapshot.py` + `routers/assistant_snapshot.py` 的可回滚快照，与 main 的 `/assistant/versions*` 在"回滚"语义上重叠。**建议历史面板只做"只读查看 + 两版对比"，回滚仍走 feat 的快照**，避免两套写路径并存。
- 工作量：**小（纯前端）**。

---

## 四、无法判定、需要实跑对比的项

1. `ProjectWorkspace.vue` 两边的**生成控制台细节**（问题清单、预算条、分层构成、独立算子、段落分页、单段重生成、流式手稿）哪个行为更正确——两套实现均未见测试覆盖，只能实跑同一章对比输出。
2. `KgWorkspace.vue` 的 `interrupted` 状态：未核对 `kg_v3` worker-status 响应是否含该字段。
3. `RoleModelPanel` 的 `thinking` 持久化位置（`LlmRole` 列 vs 配置 JSON）在 main 侧未逐列确认，直接影响 feat 是否需要加列 migration。
4. M10 中 `style/beat.py`/`beat_insert.py`/`lead_in.py` 的实际收益：`beat_insert.py` 文件头自称"默认关闭，参照物曾经是错的"，是否已修正需看运行记录。
5. F8 的 `kg/alias_rules.py` 是否已内联（当前只被 `experiments/` 引用），需确认后再决定是否按死代码处理。
6. `retrieval/brief/*` 默认关闭状态下是否有任何生产入口会打开它（本次只确认了 import，未追配置开关的运行时取值）。

---

## 五、摘要

**A. 独有功能里「值得保留」的（按价值排序）**

1. `agents/skills/` 技能提示词版本管理 + `diagnostic_skills.py` —— 提示词可按版本号固定、manifest 校验依赖，解决"规则改了无法复现"的元问题；它是下面几项能力的承载体，应先搬。
2. `agents/closed_inventory(_v2)` + `closed_inventory/v1-v8.json` —— 闭集盘点，feat 侧无任何等价物；`v8` 的 `failure_boundaries` 正好对准本项目"物件/实体/势力"混淆。
3. `agents/body_repair` / `body_consistency` / `body_evidence` / `body_acceptance` —— "检测→证据→提案→接受"四段闭环，被 `generation/engine.py` 直接调用；feat 只有检测，没有修复提案。
4. `routers/digest` + `style/digest` + `generation/digest_context` + `BookDigest` 表 —— 全书摘要层，被 9 处引用；feat 只有逐章邻域，长书会撞上下文墙。
5. `llm/capability.py` + `thinking_models.py` + `assistant_probe` —— 思考档位可控 + 探针；直接对应本项目"思考吃满 max_tokens 导致正文 0 字"的已知问题。
6. `style/spec.py`（文风规格书）—— 把档案统计变成机器可验的闭集目标，对症"文风指标系统性偏离靶值"。
7. `agents/workflow` + `claim_guard` + `consent` + `action_proposals` —— 被 5 处调用，是 main 助手链路骨架（与 feat 的 `action_catalog` 冲突，需二选一）。
8. `generation/verbatim` + `visibility` + `dedup` + `candidate` —— 照搬闸门、缺来源即排除、段内去重、候选打分，四个小而硬的闸门。

**B. 前端 18 个文件里「取 main 更好」的**

4 个：`views/LlmSettings.vue`（生成档位切换）、`components/AssistantIssueList.vue`（issue 类型中文化）、`components/RoleModelPanel.vue`（思考档位控件）、`utils/assistantDisplay.ts`（补 `proposal` 等 kind 的风险文案——**feat 现有版本会把改写提案标成"追加新段落"，是已定位的真实文案缺陷**）。

另有 1 个明确**不能取 main**：`components/DistillProgress.vue`（main 是阶段进度，feat 后端是块进度，取 main 会显示错误分母）。其余：取 feat 7 个、等价 1 个、手工合并 6 个。

**C. 移植登录 / 导出 / 执行历史各需动哪些后端接口**

| 能力 | 需要新增/改动的后端接口 | 结论 |
|---|---|---|
| 登录占位 | **无**（纯 localStorage，不产生鉴权） | 不建议移植 |
| 导出三件套 | 大纲：无；坑清单：无（`/projects/{id}/reminders` 已存在，只缺 client 方法）；人物表：需 `GET/POST /projects/{id}/character-cards`、`/character-cards/generate`、`/character-cards/batch` + `CharacterTemplate` 表，或改数据源为 `plot_bible.characters` | 先上两件，人物表另议 |
| 执行历史面板 | **0 个新端点**（`/assistant/runs`、`/assistant/runs/{id}`、`/assistant/analyze-logs`、`/assistant/versions`、`/versions/diff`、`/versions/{id}/restore` feat 全部已有） | 纯前端移植，最小成本、最高性价比 |
