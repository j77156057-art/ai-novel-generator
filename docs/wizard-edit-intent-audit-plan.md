# 「改主线 / 改某段」对话编辑能力调研计划（只调研，不改代码）

## Context

用户要确认：在对话助手里说「我想改一下主线」「想修改某几段」时，系统**实际**会发生什么。
需要分引导阶段（正文未生成）与正文生成后两段，逐类给出：实际分支（file:line）、用户看到的话、
是否入库、落到哪张表/字段、对已生成正文与后续生成的影响、是否死路。最终给一张表 + 一句结论，
用于决定要不要做「对话式编辑」。

约束：不改代码、不改表结构、不写/不碰 `kg_*` 数据；不重启 `:8000`；实测只用一次性项目，
用完清干净，不碰项目 5（`active=5 / continuation=NULL`）。

## 已由静态阅读确认的事实（待实测复核）

### A. 引导阶段（`POST /api/assistant/wizard/{id}/say`）

1. `/say` 的处理顺序（[assistant_wizard.py](backend/app/routers/assistant_wizard.py)）：
   `refusal` → 长文本写 `project.premise`（仅 `_PREMISE_STEPS` 且 ≥40 字）→ `actions.plan`
   → `chat.parse`（作答/追问/没听懂/回退）。
2. `chat.parse`（[wizard_chat.py](backend/app/wizard_chat.py)）里
   `control_intent` 只在 `character` / `scene` 两步生效；其余步骤「改设定」类说法没有识别位。
3. `actions.plan`（[wizard_actions.py](backend/app/wizard_actions.py)）的动作表里
   **没有「主线 / plot_timeline」**，也没有「改第 N 段」。`大纲` 只能开关（`set_outline`）。
4. 疑似真 bug：「第三段写紧一点」在 `chapters` 步会被 `first_number` 解析成 `3` 并当成
   「写 3 段」作答推进；在 `length` 步会被解析成 `3 字`（随后被 validate 拒或误收）。
   → 需要用真实 `/say` 复核。
5. 「刚才那个设定不算了，换成 XX」没有对应分支；唯一官方替代是「上一步」(`/say` back)
   或 `/goto` 回某步重答，重答会覆盖 `answers[step]`；`project.premise` 只追加不覆盖。
6. 引导里 `control_message` 对「改人物/世界观」给出的替代路径是「去 /kg 工作台角色卡改」；
   但 `kg_v3` 路由自述「全部只读（豁免是唯一写入）」，且全仓库无 `gender/性别` 字段。
   → 这条替代路径疑似是**假指路**，需在报告里点明。

### B. 正文生成之后

1. 引导 commit 后无 active session，`/say` 直接 409；用户实际只能走工作台助手 `/api/assistant/qa`
   或项目工作台按钮。
2. `/qa` 只读：只读工具白名单 + LLM 回答；写操作只以「待确认卡片」形式提出
   （[assistant.py](backend/app/routers/assistant.py) 的 `_collect_pending_actions`）。
3. 「改主线」→ `_REWRITE_MARKERS` 与段号都不命中 → 不弹卡，只得到一段 LLM 文字；无落点。
4. 「把第 2 段重写，别让主角赢」→ 命中 `_REWRITE_MARKERS` + 段号且该段存在 → 弹
   `invoke_generation` 危险卡 → 确认后走 `POST /api/assistant/invoke-generation`。
   **但 `instruction` 只进 AgentRun 与响应文案，没有传进引擎**（`regenerate_segment_stream`
   签名无 instruction），「别让主角赢」不会进入提示词；响应用户看到的却是「已按你的指令重写」。
   → 疑似真 bug（约束丢失 + 误导性回执）。
   重写本身会带物品持有块与 constraints，但不带 RAG。
5. 人物性别：无字段、无写入通道；`edit_proposals` 的 `target_kind` 白名单不含 KG 实体。
   唯一相近的是 `POST /projects/{id}/bible/apply`（写 `kg_bible_revisions` + 覆盖
   `projects.plot_bible`，**明确不自动改 KG**），对已生成正文无影响、后续生成会读新 bible。
6. `edit_proposals` / `versions`：后端有完整状态机与读写端点、有测试；前端 `client.ts` 有方法，
   但**没有任何 view 调用** → 无 UI。助手也不会自动写提案（只提出卡片，且 proposal 卡不由 `/qa` 自动提）。

## 待执行（研究/验证，不写业务代码）

1. 用 Grep 补齐 `wizard_actions.plan`、`control_message`、`proposals.apply_proposal` 的精确行号；
   确认 `apply` 到 `generation_text` 后是否重建摘要树（影响后续生成的口径）。
2. 若 `:8000` 可用且是只读查询，则直接只读验证；否则按上一轮方式起隔离 8010 + 一次性 DB 副本：
   - 建 `__after_` 前缀一次性项目，`/start` 后真实 `POST /say` 测三类说法：
     「我想改一下主线」「刚才那个设定不算了，换成 XX」「第三段写紧一点」（分别在 branch /
     chapters / length / confirm 步），记录 `mode/message/session.step_key/answers` 与落库；
   - 在隔离副本里造一条 Generation 行，测 `/qa` 对「改主线」与「把第 2 段重写，别让主角赢」
     的返回（是否弹卡、卡片 body 里 `instruction` 是否被引擎消费——静态已确认不消费）；
   - 只读查询 `edit_proposals` / `versions` 行数与前端引用，确认「后端活、UI 死」。
3. 清理：按前缀删测试项目的 assistant_messages / sessions / notices / action_audits /
   generations / projects；停隔离端口；不动项目 5。

## 交付

- 报告：分 A/B 两段的证据表（说法 → 实际分支 file:line → 用户看到的话 → 入库位置 →
  对已生成正文/后续生成影响 → 是否死路）。
- 结论一句：现在「改主线」「改某段」各自能走通哪条路；走不通的替代路径。
