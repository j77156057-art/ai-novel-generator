# Skill 沉淀设计

## 要解决的问题

本项目里大量能力是**做出来的**：一次清洗、一次蒸馏、一次正文审查、一次文案修复，
背后都有确定性的规则和踩过的坑。这些能力如果只留在代码里，助理 agent 看不见、
用不上；下一次别的项目要用，还得重新推一遍。

所以要一个**沉淀单元**：把「一套做法 + 它的适用范围 + 它的失败边界 + 它的证据」
打包成一份 skill，存进仓库，让助理 agent 能发现它、选它、调用它。

## 三个角色

```
助理 agent（主 agent）
  ├── 发现：list_diagnostic_skills        → 目录（不注入步骤正文）
  ├── 选择：按用户诉求挑一份 skill
  └── 派发：run_diagnostic_task(skill_id, skill_version, …)
                    │
                    ▼
        子 agent（按 skill 声明的角色执行）
          diagnostic：只给「看到了什么」+ 局限
          editor：    只给规则算得出来的机械修法
                    │
                    ▼
        能力（capability，只读）
          inspect_text_quality / inspect_prompt_health / …
                    │
                    ▼
        业务数据（一个字都不写）
```

主 agent 不自己执行步骤，**子 agent 也不写业务数据**。要改仍然只有一条路：
子 agent 给出机械修法 → 助理 agent 用 `propose_text_fix` 提待确认卡 → 人点确认
→ 落库 → 批准 → 应用。

## 清单契约

一份 skill 是一份 JSON：`backend/app/agents/skills/<id>/v<n>.json`。

| 字段 | 作用 |
|---|---|
| `id` / `version` | 身份。版本递增，旧版本保留（可回滚） |
| `status` | `experimental` / `validated` / `deprecated` |
| `applicable_tasks` | 这个 skill 能接哪些任务类型（工具参数里的枚举） |
| `agent_roles` | **谁执行**：子 agent 角色，一个 skill 只声明一个 |
| `required_capabilities` | 依赖的能力与最低版本 —— 缺了就拒，不降级 |
| `steps` | 步骤：每步一个能力 + 依赖哪几步 + 观察到什么 |
| `acceptance` | 验收项，每项都要有证据支撑 |
| `source_evidence` | 实测证据（见下） |
| `failure_boundaries` | **明确说不能做什么**（这条最容易被忽略，也最要紧） |
| `allowed_actions` | 必须是空数组 —— skill 自己不许动手 |

`validate_manifest` 会逐项校验：字段不多不少、id 形状、版本号、状态、
角色必须是已注册口径、能力必须存在且版本够、步骤依赖必须无环、
验收项必须都要证据、`validated` 必须附齐 `source_evidence`。
**清单里的任何一项不合规，`load_skill` 直接抛错** —— 不会「差不多能用」。

### 角色即口径

`agent_roles` 不是装饰。不同角色拿到同一份观察，要给出**不同形状**的结论：

- `diagnostic`：区分主语、字段、时点、传言、比喻和设想；引文定位不等于语义成立。
- `editor`：只认规则算得出来的机械修法（半角标点换全角、连续重复标点收成一个）；
  措辞、节奏、文学性一律不判；不许把「发现问题」说成「改好了」。

口径写在 `ROLE_SYSTEM_PROMPTS` 里，**角色与口径一一对应**（少一条就掉回默认诊断口径，
有测试钉住）。

## 证据与状态

`status=validated` 要求每条 `source_evidence` 齐六个字段：

| 字段 | 含义 |
|---|---|
| `run_ref` | 那次真实跑（`agent_runs:<id>`） |
| `code_version` | 实现这份能力的提交 |
| `input_fingerprint` | 输入指纹（内容哈希，能对上同一份输入） |
| `scope` | 范围（哪个项目、哪几段） |
| `provider_role` | 谁在判（`assistant`） |
| `independent_verification` | **独立复验怎么做的、结果是什么** |

最后一条是分量最重的：不能是「跑过了」，要是「**用另一条路径重算，结果如何**」。

## 已沉淀的 skill

| skill | 角色 | 干什么 | 状态 |
|---|---|---|---|
| `text_quality_audit` | diagnostic | 正文文字质量审核：标点事故（error）/ 外来英文词（warn） | experimental |
| `text_quality_repair` | editor | 标点事故的**机械修复**：修法由规则算出，只构造待确认卡 | experimental（附一次完整证据链） |
| `prompt_health_audit` | diagnostic | 提示词体检：蒸馏产物有没有真进提示词、原创模式有没有漏进参考书的说话方式、禁用词与档案是否自相矛盾 | experimental |

`text_quality_repair` 的证据链（`source_evidence`）：

- `run_ref`：`agent_runs:96` —— 助理 agent 在真实通道上读正文、判出 6 处标点事故、提卡
- `code_version`：`02f5c81`
- `input_fingerprint`：`62270c65…`（该段正文的内容哈希）
- `scope`：`project:9/generation:20`
- `independent_verification`：提案 #4 应用后，**用同一套规则复算** →
  `punctuation_hits=0`、剩余可机械修 0 处、正文差异与规则改法逐字一致

它仍标 `experimental` 而不是 `validated`：证据链只覆盖**一个案例**，
「质量改善已证明」这句话还差几次独立样本。补齐样本后改状态是一行的事。

## 边界

- **只读**。skill 跑完只写自己的审计表（`agent_runs` / `evidence`），业务表一个字不动。
  有测试在 SQL 层守着这条线。
- **不静默**。查了哪层、没查哪层要在结果里看得见（`fact_checked=false`、
  `coverage.complete`、`unresolved`），不能把「没查」读成「没问题」。
- **实验授权**。`experimental` 的 skill 需要 `allow_experimental=true`。
  这条目前是**提示词层面的约定**，服务端没有闸门 —— 模型自己就能置 true。
  要收紧应做成「服务端核过的授权凭据 + 拒绝时把原因回灌给执行方」。
- **不自动应用**。`allowed_actions` 恒为空；`prepare_*` 能力只构造卡。

## 加一份新 skill 的流程

1. 先在项目里**做出**这套做法，并且**真跑通**（没有实测证据的不要沉淀）。
2. 写清单：id、版本、任务类型、角色、所需能力、步骤、验收项、失败边界。
3. 能力不够就注册新能力（`register_capability` + `read_capability` 分支），
   binding 是**受审计的本地实现 id**，不是从 JSON 里 import 的路径。
4. 写测试：能被发现、能被加载、角色口径对、**跑完不写业务数据**。
5. 附 `source_evidence`（含独立复验），把 `run_ref` 指向真实那次跑。
6. 放进 `backend/app/agents/skills/<id>/v<n>.json`，助理 agent 立刻能在目录里看到。
