# KG 抽取 / LLM 渠道 待办台账（2026-11-16）

> 状态：条目 1 已实施（2026-11-16，待重启后端生效）；条目 2、条目 3 仅记录，**未实现、未排期**。
> 触发背景：项目 7 第 5 段（`Generation id=72`，1458 字）入队后，`agent_runs #205` 连续 3 次
> `MiniMax-M3.1-Flash` 读超时，于 2026-11-15T17:06:17Z 落 `failed`（`retry_exhausted`，48m09s）。

---

## 条目 1｜KG 批抽单次调用超时过长（**已实施 2026-11-16，待重启后端生效**）

- **现状（已查实，改造前）**
  - KG 批抽复用 writer 单例：`batch_worker._get_provider()` → `get_llm_provider()`
    （`backend/app/kg/batch_worker.py:1724-1728`），抽取调用 `provider.chat(...)`
    （`backend/app/kg/extractor_v3.py:828`）。
  - 整次 `complete_message` 硬截止 `writer_total_timeout = 960s`
    （`backend/app/config.py:306`；`provider.py:434-437` 未显式传参时取该默认值）。
  - 首字节前 `urlopen(timeout=max(1, remaining))` 直接用总预算（`provider.py:543-556`），
    所以 `writer_timeout=180s`（`config.py:287`）**不约束首字节阶段**；首字节后才用
    `min(180, remaining, 30)`（`provider.py:671`）。
  - 外层 `RETRY_LIMIT=2` → 3 次尝试，每次重新获得 960s（`batch_worker.py:54,652-657`），
    实测 3×960=2880s ≈ 实际 2888.7s。provider 内重试共享同一 deadline，不叠加。

- **已实施（2026-11-16）**
  1. **独立预算块**：`config/providers.yaml` 新增 `kg_extract.total_timeout: 300`、
     `kg_extract.first_byte_timeout: 120`；`app/config.py` 新增
     `kg_extract_total_timeout` / `kg_extract_first_byte_timeout`（可被同名 env 覆盖，
     `.env.example` 已补 `KG_EXTRACT_TOTAL_TIMEOUT` / `KG_EXTRACT_FIRST_BYTE_TIMEOUT`）。
  2. **抽取专用单例**：`get_kg_extract_provider()`（`provider.py`）——渠道/模型链与
     writer 完全相同，只显式传入上面两个预算；`reset_provider_cache()` 同步清它。
     `batch_worker._get_provider()` 改用它。
  3. **首字节闸门**：`complete_message` 的 `urlopen` 与 `_read_body` 首字节分支改为
     `min(剩余总预算, first_byte_timeout)`；**None = 原行为**，writer 不传，全链路不变。
  4. **note 留痕（顺带，低成本）**：`agent_runs.note.llm = {provider, models, model,
     total_timeout, first_byte_timeout}`，成功/失败/降级三条路径都写；`agent_runs.model`
     从恒为 `None` 改为记录实际应答模型（新增 `provider.last_model`，仅诊断用途，无迁移）。
  5. 注释写明最坏时长：`(RETRY_LIMIT+1) × 300s ≈ 15 分钟`（`batch_worker.py` 顶部）。

- **改前 / 改后数字**
  | 指标 | 改前 | 改后 |
  | --- | --- | --- |
  | 首字节永不到达的暴露时间（单次） | 960s（≈16 分钟） | 120s（2 分钟） |
  | 最坏放弃时间（整批 3 次尝试） | 3×960s ≈ 48 分钟 | 3×300s ≈ 15 分钟（首字节挂起时实际更早） |
  | writer / 正文生成 | 960s，首字节无额外上限 | **完全不变**（默认路径不传任何预算） |

- **验证**：`backend/tests/test_kg_extract_timeout.py` 20/20 通过（含 writer 960/None
  防回归断言、KG 300/120、首字节 urlopen/socket 实测 120、reset 双清）；
  相关子集 `test_llm_fallback_signature`(5) / `test_provider_opencode`(14) /
  `test_batch_worker_observability`(40) / `test_e2e_journey_pipeline`(19) 全通过，
  `test_batch_worker_claim` / `test_kg_silent_failures` / `test_registry_invoke_whitelist` 退出码 0。
- **生效条件**：需重启后端（`reset_provider_cache()` 后按新 settings 重建）；本轮未重启。

---

## 条目 2｜跨渠道 provider fallback（另立一项，本期不做）

- **状态**：待办 / 未排期 / 未实现。
- **目标**：MiniMax 整条模型链不可用时，能自动切换到智谱等其他渠道继续完成调用，
  而不是像 #205 那样在单一渠道内耗尽 48 分钟后整段失败。
- **为什么不是配置问题**
  - 现有 `models` 链只在**同一个 provider 实例内部**换 model id：`_model_chain` 由
    `[model] + [m for m in models if m != model]` 拼成（`backend/app/llm/provider.py:439`），
    所有候选共用同一份 `base_url` / `api_key`（`provider.py:426-470` 构造期固定）。
  - `get_llm_provider()` 只实例化一个渠道类（`provider.py:1197-1245`），失败即抛
    `RuntimeError`（`provider.py:626-630`），**没有任何跨类切换分支**。
  - 结论：把 `models` 配成 2–3 个也仍然是「MiniMax 内换 id」，解决不了整渠道不可用。
- **已知约束**
  - `app_settings` 里存在一条**从未生效**的备用链：`llm.zhipu_models = glm-4-flash,glm-z1-flash`
    （2 个模型）。因 `llm.provider = minimax`，该键不会被读取。
  - 同渠道内可用 id 有限：M3 / M2.x 在类注释里被明确排除（「不作默认，也不入切换链」，
    `provider.py:1024-1025`）；`MiniMax-Text-01` 曾被 404/400 打回（`provider.py:623-625`）。
  - 生效配置只有单值：`llm.minimax_models = MiniMax-M3.1-Flash`（DB），去重后链长为 1。
  - assistant / distiller 角色显式禁止自带 `models`（`routers/settings.py:476-483`）；
    `providers.yaml` 的 writer 块也不含 `models` 键，yaml 配不出链。
- **需要新增（仅描述）**
  1. **provider 级 fallback 层**：在 `get_llm_provider()` 之上（或 `complete_message` 外层）
     加一层「渠道列表 + 逐个尝试」的调度器；现有 `_model_chain` 保持为渠道内细粒度。
  2. **失败判定口径**：哪些错误触发切换（连接失败 / 首字节超时 / 5xx / 429 / 内容不可解析），
     哪些不触发（例如 400 参数错、鉴权错需直接暴露）；以及每渠道的重试上限与总预算分配。
  3. **切换策略**：顺序 / 权重 / 冷却期；一次任务内是否允许回切；切换后如何写回
     `agent_runs.note`（当前 note 不记录 model/provider，见 #203，需补可观测字段）。
  4. **额度与成本口径**：跨渠道计价不同（MiniMax 计费 vs 智谱免费档限流），需要明确
     预算归属、限流退避与配额耗尽后的行为。
  5. **验收**：构造「主渠道全链失败」用例，验证自动转副渠道且 `failures/attempts` 留痕；
     并验证副渠道也失败时的终态仍是 `failed`（可被 `worker/requeue` 重捞）。
- **不做的原因**：用户裁决「跨渠道 provider fallback 另立一项，本期不做」。

---

## 条目 3｜skills 诊断体系接线（18 份 playbook + `diagnostic_skills` 加载链）

- **状态**：只记账，**未接线、未排期**（2026-11-16 只读复核；决定 A）。
- **决策**：现在不接。成本是 3 个缺失模块 + registry 三处 + 14 份测试 + 4 个 fixture；
  且技能体系依赖「生成 → 落库 → KG」主线先稳定。18 份 JSON 已在仓库里作为数据资产与
  测试夹具，不接线不会丢能力。

### 3.1 现状结论

- 18 份 JSON 全部 `status=experimental`：
  `body_consistency` v1-v2、`body_repair` v3-v4、`closed_inventory` v1-v8、
  `foundation_input_health` v1、`planning_review` v1-v2、`prompt_health_audit` v1、
  `text_quality_audit` v1、`text_quality_repair` v1（role=`editor`，其余 `diagnostic`）。
- **生产零消费点**：`load_skill()` 的唯一生产调用点是
  `backend/app/agents/diagnostic.py:265`，它属于 `diagnostic_tool()`
  （`diagnostic.py:530-553`）；`diagnostic_tool` 全仓无调用方。`discover_skills()` 只被测试调用。
  `body_repair.create_body_repair()`（`body_repair.py:532`）会用 `previous['skill']` 做复验，
  但它自己也没有上游调用方（`prepare_body_repair` / `create_body_repair` 在 `backend/app` 内零调用）。
- 实际身份：**数据资产 + 测试夹具**（`tests/test_diagnostic_skills_manifest.py`、
  `tests/test_skill_packaging.py`），不服务任何请求。
- 加载器本身可用且会真报错。`load_skill()`（`diagnostic_skills.py:110-140`）按
  `ROOT/<skill_id>/v<version>.json` 路径读取；`validate_manifest()`（L61-107）会拦住：
  `invalid_manifest_fields`（含额外字段）、`invalid_manifest_id`、`invalid_manifest_version`、
  `invalid_manifest_status`、`invalid_manifest_list:*`、`invalid_manifest_contract`、
  `unsupported_action`、`validated_requires_evidence`；`load_skill` 另拦
  `invalid_skill_identifier`、`untrusted_skill`、`unknown_skill`、`skill_version_not_found`、
  `manifest_identity_mismatch`、`skill_status_not_allowed`；只有 `OSError`/`JSONDecodeError`
  被包装成 `invalid_manifest`。唯一静默点是 `discover_skills()` 的
  `except ValueError: continue`（L151-152），坏清单被跳过。

### 3.2 接线要动的确切位置（`backend/app/agents/registry.py`）

1. `_MODEL_TOOL_PARAMETERS`（L970 起的 dict）：补 `run_diagnosis` 与
   `list_diagnostic_skills` 的参数定义。main 侧对应 `_DIAGNOSTIC_PARAMETERS`
   （`origin/main:backend/app/agents/registry.py:1887-1945`），含 `skill_id` / `skill_version` /
   `allow_experimental`。
2. `_DEFAULT_SPECS`（L1035 起的 tuple）：补两个 `ToolSpec`——`run_diagnosis` 指向
   `diagnostic_tool`，`list_diagnostic_skills` 指向 `discover_skills`；main 的注册在
   `registry.py:1842-1852` 与 `1930-1945`。
3. `_register_default_tools`（L1225）：确保两个 spec 进入默认注册表，从而被
   `backend/app/agents/diagnosis.py:251-254` 的白名单调用链执行。

**为什么不动它就「没有任何请求能触达」**：模型工具层是唯一能把 `skill_id` + `skill_version`
传进 `run_diagnostic_task()` 的地方。没有这两个工具，模型既看不到 `discover_skills()`
的目录，也无法发起 `diagnostic_tool()`，18 份 JSON 不会被任何用户请求读到。

### 3.3 必须先补的三个模块

| 缺失模块 | 不补的后果 |
|---|---|
| `backend/app/agents/consent.py` | `diagnostic_tool` 在 `diagnostic.py:543-544` 导入 `experimental_consent_granted`；**第一次调用就 ImportError**。且 18 份 skill 全为 `experimental`，没有授权闸门只能一直 `skill_status_not_allowed` |
| `backend/app/agents/prompt_audit.py` | `read_capability` 的 `prompt_health` 分支在 `diagnostic.py:159-161` 导入 `run_prompt_audit`；`prompt_health_audit v1` 一调用就 ImportError |
| `backend/app/style/scope.py` | `read_capability` 的 `binding` 分支在 `diagnostic.py:120-121` 导入 `resolve_project_scope`；`inspect_narrative_binding` 失败，`foundation_input_health v1`（默认技能）跑不完 |

### 3.4 版本固定为什么只是名义上的

- `report['skill']` 只保留 `id/version/status`（`diagnostic.py:267`），随 `run.note` 落库
  （`diagnostic.py:453-459`）；复验只比这三个键（`diagnostic.py:282-283`）。
- `validate_manifest` 用 `set(value) != required` 做严格字段集校验
  （`diagnostic_skills.py:62-64`），**新增 `sha256` 字段会被直接判 `invalid_manifest_fields`**。
- 因此同版本号替换文件内容是静默的：给定版本号只能证明「当前磁盘这一份」，不能证明
  「当时那次运行用的那一份」。`origin/main` 也只有三键（main 的 `diagnostic.py:267`），
  这一步 main 也没做。
- 让哈希链路成立要改三处：
  1. 放开 `diagnostic_skills.py:62-64` 的字段集（或把哈希放进 sidecar 并在 L61-107 校验）；
  2. `load_skill()`（L110-140）计算并返回 `sha256(文件字节)`；
  3. `report['skill']`（`diagnostic.py:267`）写入该哈希，复验（L282-283）比对哈希而非仅版本号。

### 3.5 缺失的 14 个测试与 4 个 fixture

- 入口/发现：`test_diagnostic_discovery.py`、`test_diagnostic_subagent.py`、
  `test_diagnostic_tool_observation.py`。
- 闭集盘点：`test_closed_inventory_v3.py`、`test_closed_inventory_v4.py`、
  `test_closed_inventory_v4_tool.py`。
- 正文修复：`test_body_repair_decoupling.py`、`test_body_repair_delivery.py`、
  `test_body_repair_multirange.py`、`test_body_repair_scope.py`、
  `test_authority_repair_protocol.py`。
- 规划/合流/纯对话：`test_planning_diagnostic.py`、`test_merge_diagnosis.py`、
  `test_diagnosis_plain_dialogue.py`。
- fixture/helper：`tests/fixtures/closed_inventory_v4_real_verdict.json`、
  `tests/fixtures/planning_accepted_real_shape.json`、`tests/fixtures/body_repair_run79.json`、
  `tests/helpers/probe_closed_inventory.py`。
- **后果**：接线之后没有回归保护——「发现层静默跳过坏清单」「复验只比版本号」
  「多区间范围越界」这三类问题都不会红。本地已有的
  `test_diagnostic_skills_manifest.py`（18 文件、closed_inventory 8 版、body_consistency 2 版、
  body_repair 2 版）与 `test_skill_packaging.py`（3 条 strict xfail）只覆盖加载与身份，
  不覆盖接线行为。

### 3.6 触发条件

- 「生成 → 落库 → KG」主线稳定之后；或
- 出现具体诊断需求：正文一致性核对、闭集盘点、提示词体检、文字质量修复；或
- 需要「按版本复现当时提示词」的审计/合规要求。

### 3.7 捡起来的第一步

1. 先确认三个依赖模块（`consent.py`、`prompt_audit.py`、`style/scope.py`）能从 main 连带搬入
   且不牵出新依赖（`style/scope.py` 对应 `projects.narrative_scope` 列，属第 3 批）。
2. 在 `registry.py` 三处只加 `list_diagnostic_skills`，用 `discover_skills()` 验证目录可达。
3. 跑 `tests/test_diagnostic_skills_manifest.py`，确认加载链未被改动破坏。
4. 再搬 14 个测试与 4 个 fixture，最后才开 `run_diagnosis`。

---

## 附：本轮已确认但**不属于**上述条目的事实

- `#205` 失败期间心跳每 15s 照写、`failures` 累计为 0、写锁可获取（窗口内 2 次 + 独立 3 次
  `BEGIN IMMEDIATE` 均成功）。
- `/worker/status` 的 `claim_expired` 对 `pending` 行恒为 `false`（`batch_worker.py:1189-1190`
  的 `r.status == "running"` 门禁），属显示口径问题，非时区 bug；不影响接管
  （接管按 `claim_until` 独立判断，`batch_worker.py:994-1004`）。
