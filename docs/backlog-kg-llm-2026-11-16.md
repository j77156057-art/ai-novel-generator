# KG 抽取 / LLM 渠道 待办台账（2026-11-16）

> 状态：条目 1 已实施（2026-11-16，待重启后端生效）；条目 2 仅记录，**未实现、未排期**。
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

## 附：本轮已确认但**不属于**上述两项的事实

- `#205` 失败期间心跳每 15s 照写、`failures` 累计为 0、写锁可获取（窗口内 2 次 + 独立 3 次
  `BEGIN IMMEDIATE` 均成功）。
- `/worker/status` 的 `claim_expired` 对 `pending` 行恒为 `false`（`batch_worker.py:1189-1190`
  的 `r.status == "running"` 门禁），属显示口径问题，非时区 bug；不影响接管
  （接管按 `claim_until` 独立判断，`batch_worker.py:994-1004`）。
