# 双 Agent 增量架构决策记录 (ADR-2026-10-12)

> 范围：仅记录 P0 双 Agent 最小可行形态的架构决策；落地为"文档先行 + 增量实施"。
> 关联基线：commit `b58c763` 的角色锁定、节拍约束、KG 透传、prompt 限长规则保持不变。

## 1. 总体定位与不变量

- **P0 范围**：仅引入"诊断型 Assistant"；**不迁移、不重写 Writer**。Writer 维持现状（继续走 LangGraph 大图上的纯生成节点）。
- **Assistant 角色**：只读检索 + 诊断 + 提案审批三件套；不直接生产正文，提案经人工/调度确认后才生效。
- **不变量继承 (b58c763)**：
  - 角色锁定：Writer 仅生成正文、Assistant 仅诊断/提案，禁止串权。
  - 节拍约束：生成节拍与剧情节拍 schema 不变，Assistant 写入只能走 EditProposal。
  - KG 透传：KnowledgeGraph 对外只读视图，新增诊断证据走 Evidence 表引用节点 id。
  - Prompt 限长：所有 LLM 调用仍受 max_tokens 与 system prompt 长度门禁控制，不因多 Agent 放宽。

## 2. Provider 双轨隔离

- **配置层**：在 `config/providers.yaml` 与 `.env` 引入 `writer.*` 与 `assistant.*` 两套独立段，分别持有 model / api_key / base_url / timeout / max_tokens。
- **运行时隔离**：`ProviderRegistry` 单例内部以 `role ∈ {writer, assistant}` 维度缓存与加锁，避免共享连接池导致 Writer 流量阻塞 Assistant 反向影响正文生成。
- **并发安全**：Assistant 端的检索/诊断接口全部声明 `asyncio` + 每请求独立 client；Writer 主链路不在该注册表中复用，零耦合。
- **失败隔离**：Assistant provider 异常只回落到"只读检索关闭、诊断走本地规则"，**不得**让 Assistant 异常重路由到 Writer provider。

## 3. 框架选型边界

- **LangGraph**：仅承载 Assistant 内部的"审批 / 诊断状态机"子图（states: `retrieve → diagnose → propose → await_approval → commit/rollback`），不接管 Writer 主图。
- **Deep Agents**：本轮**暂缓**；如未来需要多步工具编排，再以独立 ADR 评估，不在 P0 范围内。
- **FastAPI 工具层复用**：Assistant 直接复用既有 `app/api/v1/*` 检索/草稿查询/节拍查询端点，不再造平行 HTTP 层，降低回归成本。

## 4. 最小数据模型 (P0)

只新增 4 张表，所有写入受 `EditProposal.commit` 单点收敛：

| 表 | 作用 | 关键字段 |
|---|---|---|
| `agent_runs` | 一次 Assistant 调用的执行记录 | id, role='assistant', status, started_at, finished_at, request_hash |
| `evidence` | 诊断/检索得到的只读证据 | id, run_id, kind(beat/kg/snippet), ref_id, snippet, score |
| `edit_proposals` | 待审批的写回建议 | id, run_id, target_table, target_id, patch_json, risk_level, status |
| `versions` | 提案落地后的版本快照 | id, proposal_id, snapshot_json, created_by, created_at |

- 不新增 `chapters/characters/beats` 等业务表；提案 patch 必须通过既有 CRUD 校验。
- 每张表带 `run_id` 外键，便于审计回放与回滚。

## 5. 实施顺序 (强制)

按依赖最小化串行推进，每步可独立回滚：

1. **config / provider 隔离**：`providers.yaml` + `ProviderRegistry` 双轨 + 单测覆盖并发/失败回落。
2. **只读检索接入**：Assistant 端打通 FastAPI 既有检索/节拍/KG 视图，零写入。
3. **诊断流**：LangGraph 子图接入检索结果，产出 Evidence 与诊断报告。
4. **提案 + 审批**：写入 `edit_proposals`，由人在后台/UI 审批后调用既有 CRUD 落库并写 `versions`。

## 6. 已识别风险与缓解

- 风险：Assistant provider 故障误回落 Writer → **强制角色标签 + registry 单测断言隔离**。
- 风险：提案绕过审批直写 → **禁止在 AgentRun 上下文内直接持有 ORM session 写权限**。
- 风险：LangGraph 状态膨胀 → 子图仅 4 状态，超出即拆分新 ADR。

## 7. 未决项 (待 PRD 确认)

- EditProposal 审批人身份与 RBAC 粒度。
- Assistant 是否需要"建议性章节大纲"能力（在 P0 之外，需另开 ADR）。
- Evidence 保留周期与脱敏策略。
