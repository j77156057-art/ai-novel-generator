# 前端接线盘点 + 冗余入口收敛（2026-11-14）

## 0. 口径与方法

| 项 | 来源 | 数量 |
| --- | --- | --- |
| 后端已注册端点 | `backend/app/routers/*.py` 中 `@router.(get/post/put/patch/delete)` 装饰器逐条计数 | 105（13 个 router 文件） |
| 前端 API 封装 | `frontend/src/api/client.ts` 的 `export` 方法 | 75 |
| 已接（有封装） | 105 − 39 | 66 |
| 后端有、前端没接 | 见 §1 A | 39 |
| 前端有、后端没有 | 全部封装的路径均能在 router 里找到 | 0 |
| 定义后无人调用（死封装） | 在 `frontend/src` 全文检索函数名，仅命中 `client.ts` 定义行 | 24 |

判定规则：

- **前端入口数** = 真实调用点（`views/`、`components/` 里的 `api.xxx(`）+ 渲染该数据的面板数；只有 `client.ts` 定义、没有调用点的不算入口。
- **破坏性** = 删除 / 批量 / 回滚 / 改配置 / 改密钥 / 重建整库。
- 端点路径本身就是常量来源（router 装饰器），本文不另抄一份到配置里。

---

## 1. 盘点

### A. 后端有、前端没接（39）

#### A1. 助手域（25）

| # | 端点 | 能力 | 后端就绪 | 前端入口数 | 读写 | 破坏性 | 分级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `POST /api/assistant/diagnose` | 项目诊断（规则先行） | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 2 | `POST /api/assistant/audit-corpus` | 语料审核 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 3 | `POST /api/assistant/audit-kg` | 图谱审核 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 4 | `POST /api/assistant/audit-generation` | 生成物审核 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 5 | `POST /api/assistant/analyze-logs` | 日志分析 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 6 | `POST /api/assistant/clean-corpus` | 语料清洗 | 是 | 0 | 写 | **是（批量）** | 前端显式 + 二次确认；当前缺失，需补入口或明确不开放 |
| 7 | `GET /api/assistant/runs` | 助手运行记录 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 8 | `GET /api/assistant/runs/{run_id}` | 单次运行详情 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 9 | `POST /api/assistant/proposals` | 创建编辑提案 | 是 | 0（死封装） | 写 | 否 | 收进助手（用户确认后创建） |
| 10 | `GET /api/assistant/proposals` | 提案列表 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 11 | `GET /api/assistant/proposals/{id}` | 提案详情 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 12 | `POST /api/assistant/proposals/{id}/approve` | 提案通过 | 是 | 0（死封装） | 写 | 否 | 收进助手 + 确认 |
| 13 | `POST /api/assistant/proposals/{id}/reject` | 提案驳回 | 是 | 0（死封装） | 写 | 否 | 收进助手 + 确认 |
| 14 | `POST /api/assistant/proposals/{id}/apply` | 应用提案 | 是 | 0（死封装） | 写 | **是（改正文）** | 前端显式 + 二次确认 |
| 15 | `POST /api/assistant/proposals/apply-selected` | 批量应用提案 | 是 | 0 | 写 | **是（批量）** | 前端显式 + 二次确认 |
| 16 | `GET /api/assistant/versions` | 版本列表 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 17 | `GET /api/assistant/versions/diff` | 版本 diff | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 18 | `POST /api/assistant/versions/{id}/restore` | 版本回滚 | 是 | 0（死封装） | 写 | **是（回滚）** | 前端显式 + 二次确认 |
| 19 | `GET /api/assistant/prompts/active` | 当前提示词 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 20 | `GET /api/assistant/prompts/versions` | 提示词版本 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |
| 21 | `GET /api/assistant/wizard/{pid}/actions` | 向导可执行动作 | 是 | 0 | 只读 | 否 | 收进助手（动作发现的同源参考） |
| 22 | `GET /api/assistant/snapshots` | 快照列表 | 是 | 0 | 只读 | 否 | 收进助手 |
| 23 | `GET /api/assistant/snapshots/groups` | 快照分组 | 是 | 0 | 只读 | 否 | 收进助手 |
| 24 | `POST /api/assistant/snapshots/rollback` | 单快照回滚 | 是 | 0 | 写 | **是（回滚）** | 前端显式 + 二次确认 |
| 25 | `POST /api/assistant/snapshots/rollback-group` | 分组回滚 | 是 | 0 | 写 | **是（批量回滚）** | 前端显式 + 二次确认 |

#### A2. 项目域（5）

| # | 端点 | 能力 | 后端就绪 | 前端入口数 | 读写 | 破坏性 | 分级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 26 | `POST /api/projects/{pid}/build-narrative` | 重建叙事层 | 是 | 0 | 写 | **是（批量重建）** | 前端显式 + 二次确认 |
| 27 | `POST /api/projects/{pid}/build-kg` | 旧版建图 | 是 | 0（死封装） | 写 | **是（批量重建）** | 已被 `build-kg-from-generations` 取代，旧封装移除 |
| 28 | `GET /api/projects/{pid}/kg/character/{name}` | 单角色图谱 | 是 | 0（死封装） | 只读 | 否 | 收进助手（已有同名工具 `get_character_profile`） |
| 29 | `GET /api/projects/{pid}/reminders` | 未填坑 + 角色约束 | 是 | 0（死封装） | 只读 | 否 | 收进助手（已有同名工具 `get_reminders`） |
| 30 | `GET /api/style-distills/{id}` | 单份蒸馏详情 | 是 | 0（死封装） | 只读 | 否 | 收进助手 |

#### A3. 图谱 v3 域（6）

| # | 端点 | 能力 | 后端就绪 | 前端入口数 | 读写 | 破坏性 | 分级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 31 | `GET /api/projects/{pid}/kg-v3/worldview` | 世界观 | 是 | 0 | 只读 | 否 | 收进助手 |
| 32 | `GET /api/projects/{pid}/kg-v3/timeline` | 时间线 | 是 | 0 | 只读 | 否 | 收进助手 |
| 33 | `GET /api/projects/{pid}/kg-v3/story-context` | 故事上下文 | 是 | 0 | 只读 | 否 | 收进助手 |
| 34 | `GET /api/projects/{pid}/kg-v3/text-gate/no-generation` | 无生成章检查 | 是 | 0 | 只读 | 否 | 收进助手 |
| 35 | `POST /api/projects/{pid}/kg-v3/worker/recover` | 批抽恢复 | 是 | 0 | 写 | **是（批量）** | 前端显式 + 二次确认 |
| 36 | `POST /api/projects/{pid}/kg-v3/worker/backfill` | 批抽回填 | 是 | 0 | 写 | **是（批量）** | 前端显式 + 二次确认 |

#### A4. 生成 / 语料域（3）

| # | 端点 | 能力 | 后端就绪 | 前端入口数 | 读写 | 破坏性 | 分级 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 37 | `POST /api/generation/generate` | 非流式生成 | 是 | 0（死封装） | 写 | 否（有成本） | 保留封装与否待定；生成链路本轮不动 |
| 38 | `POST /api/generation/generate-loop` | 多段循环生成 | 是 | 0（死封装） | 写 | **是（批量）** | 生成链路本轮不动，标记为「前端无入口」 |
| 39 | `POST /api/novels/seed` | 演示语料播种 | 是 | 0 | 写 | **是（造数据）** | 不开放给助手；仅开发用途 |

### B. 前端有、但冗余 / 薄封装 / 重复（9 组）

| # | 入口 | 前端入口数 | 重复对象 | 读写 | 破坏性 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| B1 | 助手历史页签残留封装（diagnose / audit×3 / analyze-logs / runs×2 / proposals×6 / versions×3 / prompts×2 = 18 个） | 0（死代码） | 助手已改为单一对话流 + 工具调度 | 只读为主 | 否 | **第一批删除** |
| B2 | 旧图谱 / 旧生成封装（`buildKg`、`getCharacter`、`getReminders`、`getStyleDistill`、`generate`、`generateLoop` = 6 个） | 0（死代码） | 现役实现或助手工具 | 混合 | 否 | **第一批删除** |
| B3 | 步骤⑦「角色」列表 | 1 | `KgWorkspace` 角色列表；助手 `get_character_profile` | 只读 | 否 | **第一批降级为指向 KG v3 的一行提示** |
| B4 | 步骤⑦「未填坑」列表 | 1 | 助手 `get_reminders` | 只读 | 否 | 保留（生成前就地查看的使用习惯），助手可查 |
| B5 | 文风蒸馏续跑 | 2（工作区按钮 + 助手完成通知卡） | 同一端点 `/distill/resume` | 写 | 否（有成本） | 保留两处，均需显式点击；不入助手自动执行 |
| B6 | 生成 | 4（整段流式 / 单段重生成 / 单段自然语言改写 / 向导 run） | 粒度不同，非纯重复 | 写 | 部分（改写覆盖单段） | **本轮不动**（多章生成链路在跑） |
| B7 | 图谱「查看哪张图」下拉 + 刷新 | 1 | KG v3 工作台数据面板 | 只读 | 否 | 保留（用于新旧图对照） |
| B8 | 设置页 LLM 表单 | 1 | 无 | 写 | **是（配置/密钥）** | 保留唯一显式入口 + 输入框掩码 |
| B9 | `buildKgFromGenerations` 与已死的 `buildKg` | 2（其一为死代码） | 新旧两条建图路径 | 写 | **是（批量重建）** | 活路径保留；死封装删除（并入 B2） |

### C. 分级结论

**档 1：只读、低风险、纯展示 → 直接收进助手，前端入口移除或降级**

- 诊断 / 语料审核 / 图谱审核 / 生成物审核 / 日志分析 / 运行记录（A1 #1–5、#7–8）：后端已有只读工具或只读端点，前端 0 入口，只差「助手能发现」。
- 角色与关系（A2 #28、B3）：助手工具 `get_character_profile` 已存在；步骤⑦ 列表降级为链接。
- 未填坑与提醒（A2 #29、B4）：助手工具 `get_reminders` 已存在。
- 图谱世界观 / 时间线 / 故事上下文 / 无生成章检查（A3 #31–34）：只读，纳入动作目录供助手发现。
- 提案 / 版本 / 提示词的**读**（A1 #10、11、16、17、19、20）：只读，纳入动作目录。

**档 2：写操作 → 收进助手，保留必要的前端确认**

- 单段自然语言改写（既有 `invoke-generation` 待确认卡片）。
- 提案创建 / 通过 / 驳回（A1 #9、12、13）：助手提出，卡片由用户点确认后才发请求。
- 图谱豁免新增（`POST /kg-v3/exemptions`，已接前端）：维持前端表单，助手不代填实体名。

**档 3：破坏性 / 批量 / 配置 / 密钥 → 前端必须保留显式入口 + 二次确认，不靠自然语言**

- 回滚：快照回滚、分组回滚、版本 restore（A1 #18、24、25）。
- 批量写：提案 apply / apply-selected、语料清洗、重建图谱/叙事层、批抽 recover/backfill/requeue。
- 改配置 / 密钥：LLM 设置保存、蒸馏启动与应用、角色删除。
- 生成循环与语料播种（A4 #38、39）：不开放给助手。

---

## 2. 收敛方案

### 第一批（本次落地）

1. **后端：动作目录成为唯一真源**
   - 新增 `backend/app/agents/action_catalog.py`：声明每个可执行操作的 key / 类别 / 策略 / 是否需确认 / 批量 / 配置 / 前端入口 / 触发词。分级规则写进 `ActionSpec.__post_init__`，档 3 操作若被标成可自动执行会直接抛错。
   - 注册只读工具 `list_actions`（白名单 + 默认 spec 同步加），助手可按 query/category **发现**操作；提示词里不写死操作清单。
   - `/api/assistant/qa` 在检索后按用户问题匹配目录，把命中项作为「可执行操作」拼进上下文，并在响应新增 `available_actions`（前端可忽略）。
2. **前端：删除死入口、降级重复面板**
   - `client.ts` 删除 B1+B2 共 24 个无人调用的封装；保留 `types.ts` 类型（后端契约仍在）。
   - 步骤⑦「角色」列表替换为指向 KG v3 工作台的一行提示（角色数仍显示，`characters` 计算属性不变成死变量）；「未填坑」与「建/重建图谱」按钮保留。
3. **不动的部分**
   - `generate` / `generate-stream` / `regenerate-stream` / `assistant/invoke-generation` / 蒸馏全部不改；避免影响正在跑的多章生成链路。

### 后续批次（不在本次范围）

- 为 worldview / timeline / story-context / text-gate / runs / proposals 读路径注册只读工具，让助手直接答，而不是只给出「在哪个页面」。
- 提案读路径补前端视图（或确认永久由助手承担）。
- 破坏性入口的二次确认统一组件化（当前散落在各面板）。

---

## 3. 回退方式

- 后端：新增文件 + 白名单/spec 两行 + `/qa` 一段拼接；回退即删除 `action_catalog.py`、撤掉 `list_actions` 两处注册与 `/qa` 调用。
- 前端：仅 `client.ts` 删函数、`ProjectWorkspace.vue` 一个模板块替换；`git revert` 对应提交即可。
- 全部改动不触碰数据库、不写业务表、不改生成参数。

---

## 4. 第一批落地结果（2026-11-14）

### 4.1 删死封装前的复验（对应收紧要求 1）

对 24 个候选逐个核了四种「看不见的调用」：

| 复验项 | 命令/方式 | 结果 |
| --- | --- | --- |
| 动态下标取方法 | 检索 `api[` / `client[` | 0 处 |
| 整表遍历分发 | 检索 `Object.keys(api)` / `Object.entries(api)` / `...api` | 0 处 |
| 动态 import / 异步组件 | 检索 `import(` / `defineAsyncComponent` / `component: () =>` | 0 处指向这些方法名 |
| 路由 / 菜单配置 | 读 `router/index.ts` 与 `App.vue` | 只静态 import 视图组件，不引用 API 方法名 |
| 全量名字检索 | 在 `frontend/src` 全部文件检索 24 个方法名 | 仅命中 `client.ts` 定义行 |

结论：24 个全部可删，**没有**留下 `unused?` 待定项。删除后 `vue-tsc --noEmit` 与 `vite build` 均通过。

### 4.2 步骤⑦角色列表降级前的导航检查（对应收紧要求 2）

- 被替换的「角色」块是纯展示：无 `@click`、无 `router-link`、不参与 `goStep`。
- 该卡片真正承载导航的是标题栏已有的 `KG v3 工作台 →`（`/projects/:id/kg`），本次**未改**。
- `characters` 计算属性仍被新提示里的「当前图谱 N 个角色节点」使用，不产生死变量。
- 全文件检索 `characters`：除 `bible.characters` / `plan.constraints.characters` 两处同名字段外，图谱侧只剩该提示。

### 4.3 档 3 登记（对应收紧要求 3）

`action_catalog.py` 共登记 43 项，其中：

- `auto_read` 12 项（助手可直接调只读工具）；
- `propose_write` 2 项（助手提出、用户点确认卡片）；
- `frontend_only` 18 项（破坏性/批量/配置/密钥，全部 `requires_confirmation=true`，并写明前端入口）；
- `unwired_read` 11 项（只读、后端就绪、两边未接）。**2026-11-21：已全部绑定白名单只读工具，现为 0 项**；
  当前分布为 `auto_read 26 / propose_write 2 / frontend_only 18 / unwired_read 0`。

档 3 **全部在目录里**，`list_actions` 能返回；助手看到的是「只能由用户在前端页面操作，不得代执行」，不会被当成可代劳。约束由 `ActionSpec.__post_init__` 强制：破坏性操作若标成非 `frontend_only`、`frontend_only` 若没写入口或没标确认，模块导入即报错。

### 4.4 改动清单

| 文件 | 改动 |
| --- | --- |
| `backend/app/agents/action_catalog.py` | 新增：动作目录 + 四档策略 + 匹配/渲染 |
| `backend/app/agents/registry.py` | 白名单与 `_DEFAULT_SPECS` 各加 `list_actions`，新增只读实现 |
| `backend/app/routers/assistant.py` | `/qa` 匹配目录并注入上下文；响应新增 `available_actions`；系统提示补「按策略回答」一条 |
| `tests/test_action_catalog.py` | 新增：档 3 必须前端确认、档 3 必须登记、匹配确定性 |
| `tests/test_registry_whitelist.py` | 写死的 12 改为与白名单长度对齐（原断言在本次改动前已是红的） |
| `frontend/src/api/client.ts` | 删除 24 个无人调用的封装，保留类型定义（后端契约仍在） |
| `frontend/src/views/ProjectWorkspace.vue` | 步骤⑦角色列表降级为指向 KG v3 的一行提示；未填坑与建图按钮不动 |

### 4.5 发现效果（`list_actions` / `/qa` 匹配）

| 查询 | 命中 |
| --- | --- |
| `能不能回滚到快照` | `snapshot_rollback`（destructive / frontend_only / 需确认）+ `snapshot_list`（read / unwired_read） |
| `这个世界观有什么设定` | `kg_worldview`（read / unwired_read）+ `bible_summary`（read / auto_read） |
| `图谱审核有什么问题` | `audit_kg`（read / auto_read，可直接执行） |
| `帮我把这段重写一下` | `generation_review`（读取段落）+ `invoke_generation`（写 / propose_write / 需确认） |

`actions_prompt_block` 对 `frontend_only` 输出「只能由用户在前端页面操作，不得代执行」，对 `propose_write` 输出「只能提出，用户点确认卡片后才执行」。

### 4.6 验证记录

- `python -m pytest tests/test_action_catalog.py tests/test_assistant_readonly.py tests/test_assistant_agent_tools.py tests/test_registry_whitelist.py backend/tests/test_registry_invoke_whitelist.py -q` → 50 passed（含 `/qa` 走 HTTP 的端到端用例）。
- `npm run typecheck`（`vue-tsc --noEmit`）→ 通过。
- `npm run build`（`vue-tsc && vite build`）→ 通过。
- `python -m py_compile` 三个后端改动文件 → 通过。
- 生成链路（`generate` / `generate-stream` / `regenerate-stream` / `invoke-generation`）与蒸馏代码未改动。

### 4.7 已知缺口（第一批结束时）

1. ~~**TODO（下一步分水岭，未在本轮动）**：provider 目前只有文本 `chat`，**没有 function calling**，模型不能自主调用任何工具。~~ → **2026-11-21 已解决**：`/qa` 已走 provider 原生 `tool_calls`（确定性取证保留为回落腿），模型可自主调用白名单只读工具。
2. ~~`frontend_only` 里几项目前两端都没有入口~~ → 已在第二批补齐（语料清洗、快照回滚/整组回滚、批量应用提案），见 §5。
3. ~~`proposal_list` / `version_list` / `prompt_active` 等读能力仍是 `unwired_read`~~ → **2026-11-21 已接线**：连同 KG 世界观/时间线/故事上下文、提炼详情、无生成章检查、项目诊断、运行记录、快照列表一起绑定为只读工具。
4. ~~单条提案应用（`POST /proposals/{id}/apply`）仍只有后端~~ → 前端已在助手工作台、项目页回滚入口与工作流条使用单条 `applyProposal`。

## 5. 第二批：补齐三项档 3 的显式入口（2026-11-14）

三项目录里有、两端都没入口，档 3 的前提（前端保留显式入口）不成立，用户实际用不到。本轮按同一标准补齐：**入口可见 / 影响范围写进确认文案 / 二次确认不可绕过 / 执行后给可核对回执**；仍然不允许从对话框直接触发。

### 5.1 语料清洗（`CorpusView.vue`）

| 项 | 落点 |
| --- | --- |
| 入口 | 语料页「语料清洗（规则先行 · 模型复核）」卡片：选语料 + 单次扫描分片数（1–200，默认 20）+ `dry_run` |
| 影响范围文案 | ①扫描对象（哪本语料 / novel_id / 最多 N 片，按 idx 升序）②数据影响（**不写 `novel_chunks`、不改正文**；最多创建 min(N,50) 条 pending 提案，要生效须再批准+应用）③模型调用（八条判据先跑，只有命中可复核判据的分片才送模型；不可用则降级并标注）④留痕（一条 AgentRun）；勾 `dry_run` 时额外声明「本次不创建提案」 |
| 二次确认 | 模态；请求只在 `submitClean()` 内发出，函数首行校验 `cleanConfirmOpen`，关闭模态没有任何别的触发路径 |
| 执行后核对 | `scope.chunks_scanned/limit/novel_id`、`proposal_count` + 提案 id/chunk id、`findings_by_rule` 逐判据计数、`model_participated` + `model_note`、`agent_run_id`、每条建议修正的 before→after/来源/置信度/是否已落提案、`model_rejected` |

### 5.2 快照回滚（`ProjectWorkspace.vue` 步骤 6）

| 项 | 落点 |
| --- | --- |
| 入口 | 「重写快照与回滚」卡片：按段列表（`GET /snapshots?status=applied`）+ 按整组列表（`GET /snapshots/groups`，`undoable=false` 时禁用） |
| 影响范围文案 | 单段：覆盖（当前正文约 N 字 → 快照 #id 的 before_text 共 M 字）、定位（before_hash 前 12 位 / 来源操作 / 快照时间）、不动（其它段与剧情圣经）、可再撤销（回滚生成新快照，回执给 id）、拒绝条件（正文在快照后被改过则整段不动）。整组：共 N 段（列段号）、applied/failed/已回滚计数、同成同败、每段生成新快照 |
| 二次确认 | 模态；请求只在 `submitRollback()` 内发出。设定漂移 409 时模态内追加后端 `options[]` 三选一（先退设定/只退正文/取消），未选前主按钮禁用 |
| 执行后核对 | 单段 `restored_snapshot_id`、`rollback_snapshot_id`、`rollback_batch_id`、`chars`、`text_hash` 前 12 位、`warnings`、`derived`；整组逐段「恢复快照 #x → 新快照 #y（字数）」；成功后刷新正文与快照列表 |

### 5.3 批量应用提案（`ProjectWorkspace.vue` 步骤 6）

| 项 | 落点 |
| --- | --- |
| 入口 | 「待应用提案」卡片：待批准（行内「批准」，只改状态）+ 已批准（多选，显示 target 人话描述与 `impact`） |
| 影响范围文案 | 覆盖范围（逐条 `#提案id · type · 第 N 段（正文 #id）/ 语料分片 #id / 剧情圣经 / 提示词覆写`）、同成同败（任一 stale/blocked → 整批零写入）、可回退点（每条写一条 Version，可逐条回滚）、上限（50，与 `MAX_BULK_APPLY` 对齐） |
| 二次确认 | 模态；请求只在 `submitApply()` 内发出，首行校验 `applyConfirmOpen`；超选 50 条时前端直接拦下 |
| 执行后核对 | 成功逐条 `proposal_id → version_id`、`kind/target_kind/target_id`、`content_hash` 前 12 位、`applied_at`；409 展示 `reason` + `stale[]`/`blocked[]` 逐条原因，并明确「整批已拒绝（零写入）」 |

> 为什么加「批准」：clean-corpus 产出的都是 `pending`，不批准就永远进不了批量应用，入口等于不可用。批准只改状态、不写业务表，所以只做行内提示。

### 5.4 改动清单（第二批）

| 文件 | 改动 |
| --- | --- |
| `frontend/src/api/types.ts` | 新增清洗/快照/回滚/批量应用类型；提案 type 补 `corpus_text_edit`、target_kind 补 `novel_chunk_text` |
| `frontend/src/api/client.ts` | 新增 `cleanCorpus` / `listSnapshots` / `listSnapshotGroups` / `rollbackSnapshot` / `rollbackSnapshotGroup` / `listProposals` / `approveProposal` / `applySelectedProposals` |
| `frontend/src/views/CorpusView.vue` | 新增语料清洗卡片 + 确认模态 + 回执 |
| `frontend/src/views/ProjectWorkspace.vue` | 步骤 6 新增快照回滚卡片与批量应用提案卡片 + 两个确认模态 + 回执 |
| `backend/app/agents/action_catalog.py` | 只改 4 条 `frontend_entry` 文案（policy / 数量 / 断言均未动） |

### 5.5 验证记录（第二批）

- `npm run typecheck`（`vue-tsc --noEmit`）→ 通过。
- `python -m pytest tests/test_action_catalog.py -q` → 通过（目录只改描述，不破断言）。
- 生成链路、蒸馏、既有端点与提案状态机未改动；三项入口全部是新增代码，回退 = 删除新增块与文档段落。
