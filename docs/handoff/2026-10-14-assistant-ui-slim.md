# 助手页瘦身规格：只留问答，其余能力交给 agent 自主调度

## 产品决策（用户明确要求）

> 智能助手只提供问答框，其他表层的能力都应该是 agent 自身可以调动的能力，
> 不应该放在前端让用户去操控。
>
> 提案审批：对话时加一个提示窗口。调动生成：加个确认窗口。
> 就算没做好也可以 git 回滚。

## 目标形态

**前端只有一个对话流。** agent 在回答里附上「结论 + 证据」，遇到需要写操作时
在对话流里插一张**待确认卡片**，用户点确认才执行。

| 能力 | 归属 | 触发方式 |
|---|---|---|
| 问答（检索/角色/剧情） | 对话流 | 用户提问 |
| 诊断 | **agent 自主** | agent 自己判断需要时调用 |
| 日志分析 | **agent 自主** | 同上 |
| 三类审核（语料/KG/生成物） | **agent 自主** | 同上 |
| 提案审批 | 对话流内**确认卡片** | agent 提草案 → 用户点确认 |
| 调动生成 | 对话流内**确认卡片** | 同上 |

## 关键约束

1. **写操作必须经用户确认**。agent 不得自行 apply 提案或触发生成。
2. **git 可回滚是兜底，不是设计**。确认卡片是设计，回滚是兜底。
3. 不删后端端点，只改前端入口 —— 端点仍是 agent 和确认卡片的调用对象。

---

# 任务 A：后端（软件工程师）

## 目标

让 `POST /api/assistant/qa` 能自主调用诊断/日志/审核三类能力，并在需要写操作时
返回结构化的待确认卡片数据。

## 现状（已核实）

- `backend/app/routers/assistant.py:180` 已有工具调用循环 `invoke_tool(...)`
- `backend/app/agents/registry.py` 注册表：
  - `FORBIDDEN_PREFIXES`（36 行）：write_/commit_/update_/delete_/patch_/rollback_/apply_proposal
  - `ALLOWED_READ_ONLY_TOOLS`（49 行）：7 个现有只读工具
  - `register()`（105 行）：拒绝非 read_only、拒绝黑名单前缀
  - `invoke()`（159 行）：第二道闸门
- 已有只读工具实现：`_impl_search_novel_chunks`（213）等

## 需要新增的只读工具（4 个）

注册到 `ALLOWED_READ_ONLY_TOOLS` 并实现 `_impl_*`，**全部 `read_only=True`**，
名字**不得**以 `FORBIDDEN_PREFIXES` 任何前缀开头：

| 工具名 | 复用已有实现 | 说明 |
|---|---|---|
| `audit_corpus` | `app.agents.audit` 的语料 5 条规则 | 需支持可选 `novel_ids` 过滤 |
| `audit_kg` | 同上，图谱 4 条规则 | 无需参数 |
| `audit_generation` | 同上，生成物 6 条规则 | 无需参数 |
| `analyze_logs` | `app.agents.log_audit.run_log_audit_rules` | 需支持 `status` / `limit`，**必须复用 `_validate_run_status` 与 `_validate_runs_limit`** |

**这三个审核 + 日志分析在业务数据上是只读的**（只写自己的 AgentRun / Evidence 表），
所以可以安全地交给 agent 自主调用。不要修改 `app/agents/audit.py` 与
`app/agents/log_audit.py` 的现有规则行为——那些已被独立验证过。

`analyze_logs` 必须复用 `app.routers.assistant._validate_run_status` /
`_validate_runs_limit`（`assistant.py:429` / `:445`），否则会出现同一个参数在
`/runs` 报 400、在工具里却 200 的不一致。

## 待确认卡片（写操作）

在 `AssistantQARequest` 继承的响应里新增顶层字段 `pending_actions: list[dict]`，
每项形如：

```python
{
  "kind": "proposal" | "invoke_generation",
  "title": "一句话说明要做什么",
  "detail": {...},          # 前端渲染所需的全部字段（前端不自己拼请求）
  "endpoint": "/api/assistant/proposals",   # 用户确认后前端实际调用的端点
  "method": "POST",
  "body": {...},            # 完整的请求体，前端原样回传
}
```

**服务端负责把请求体拼好**，前端不参与构造——避免前后端各拼一半导致漂移。
本次只让 agent 能「**提出**」，不实现自动执行；执行由用户点确认后前端调端点。

## 验收

- `pytest tests/test_assistant_readonly.py tests/test_assistant_diagnose.py`
  必须全绿（现有回归）
- 新增测试：4 个工具能被 invoke_tool 调用、返回结构符合 `ALLOWED_READ_ONLY_TOOLS`
  与黑名单；`analyze_logs` 用非法 status / limit=-1 时**报错**（不是静默通过）
- `pending_actions` 缺省为 `[]`，不破坏既有响应契约

## 禁止

- 不要改 Writer 生成路径（`app/generation/engine.py`）
- 不要放宽 `FORBIDDEN_PREFIXES` 或 `read_only=True` 的强制
- 不要删任何现有后端端点

完成后提交，回报 commit hash 与测试结果。

---

# 任务 B：前端（软件工程师）

## 目标

`frontend/src/views/AssistantWorkspace.vue` 从 2101 行 / 6 页签瘦身为**只有对话流**。

## 现状（已核实）

- `AssistantWorkspace.vue`：2101 行，页签定义在 73–78 行：
  `qa` / `diagnose` / `proposal` / `version` / `logs` / `invoke`
- `frontend/src/components/AssistantIssueList.vue`：257 行（issue 列表渲染）
- `frontend/src/utils/assistantDisplay.ts`：40 行（severity / source 的文字+色标映射）

## 要做的

1. **删除** 页签 `diagnose` / `version` / `logs`（这三个变成 agent 自主能力）。
2. **删除** 页签 `proposal` / `invoke` 的**表单界面**，改为在对话流里渲染
   `pending_actions` 卡片：**确认 / 取消**两个按钮。
   - 确认 → 前端用卡片里服务端给好的 `endpoint` + `method` + `body` 原样发请求
   - 取消 → 从列表移除，不发请求
   - **前端不得自己拼请求体**
3. **保留并复用** `AssistantIssueList.vue`：agent 自主跑出的 issues 应该在回答里
   展示（`utils/assistantDisplay.ts` 的双编码映射正好用在这里）。
4. `pending_actions` 为空时完全不渲染该区域（不要留"暂无待确认"之类的空态）。

## 交互要求

- 确认卡片的措辞要让用户**一眼看出 agent 要干什么**（改哪一段 / 生成什么）
- 危险动作（覆盖已有段落的 regenerate）视觉上要与「新建提案」区分
- 不要用颜色单打独斗传达危险等级，沿用 `assistantDisplay.ts` 的文字+色标双编码

## 验收

- `npm run typecheck` EXIT=0
- `npm run build` EXIT=0
- 页面渲染后**只有一个输入框和一条对话流**，无任何其它操作面板
- 提交后回报 commit hash

## 禁止

- 不要引入新框架或 UI 库（沿用仓库现有 Vue 3 + Vite + TS）
- 不要删除后端调用的 client 方法（`api/client.ts` 里的仍被确认卡片使用）
