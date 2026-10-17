# Phase 1「生成中受控工具调用」——方案与风险盘点

- 日期：2026-10-17
- 分支：`feat/resume-push-2026-10`（本地 HEAD `63119b6`；远程已推送锚点 `4eaddf0`）
- 状态：**评估项已全部落地并经独立验证；③ 重生成 AI 腔检测+告警（方案 b）已落地（见 §4.2）**
- 前提基线：Phase 0 已交付（`ea85d6b`，重写环节加原文对照，仅完整档）
- 证据口径：本文标注 `文件:行号` 的结论，凡标【亲验】者由主理人独立复跑确认，非采信单一来源。

---

## 0. 结论先说：Phase 1 的真实代价比原描述高一个量级

**这个代码库里根本没有「模型驱动的工具调用」这回事。**

- 全仓（排除 `worktrees/`）grep `tool_calls|function_call|tool_choice|tools=|tool_call_id` → **零命中**【亲验】。
- provider 接口是 `LLMProvider.chat(self, system, user, constraints=None)`（`backend/app/llm/provider.py:100`）、`stream_chat`（`:111`）——**无 tools 参数**。
- 所谓「Assistant 的工具循环」，实为 `backend/app/routers/assistant.py:188-192` 一个**写死的三元组 `for`**：

  ```python
  for tool_name, kwargs in (
      ("read_generation_segments", {"limit": 5}),
      ("search_novel_chunks", {"query": body.query, "k": 5}),
      ("get_reminders", {}),
  ):
  ```

  终止条件 = 元组耗尽；**没有 max iterations、没有轮数上限、没有超时**。循环之后才是单次 `provider.chat`（`assistant.py:252`）。【亲验】

**推论**：Phase 1 不是「把 Writer 接进既有循环」，而是**必须先造一套工具调用协议**（要么改 provider 层，要么在 prompt 里定文本协议）。上一轮「两套没打通、接上即可」的表述是错的，现予订正。

---

## 1. 已核实的事实基线

### 1.1 Writer 生成链路的真实形状

| 入口 | 行号 | 真实 LLM 调用 |
|---|---|---|
| `generate_next_segment`（非流式） | `engine.py:689` | `chat`@`:736`、`summarize`@`:737`、重写 `chat`@`:643`（经 `:746`）、`summarize`@`:749` |
| `generate_next_segment_stream` | `engine.py:780` | `stream_chat`@`:824`、`summarize`@`:829`、重写@`:838`、`summarize`@`:841` |
| `regenerate_segment_stream` | `engine.py:923` | `stream_chat`@`:963`、`summarize`@`:968`（**无重写步骤**） |
| `generate_loop`（批量） | `engine.py:1005` | 外层 `for`@`:1019`，每段调 `generate_next_segment` |

- **单段的现有 LLM 预算已达「2 次 chat + 2 次 summarize」**（重写命中时）。再加工具循环即在现有预算上叠乘。
- 生产真实主路径的逐段循环**在前端**：`frontend/src/views/ProjectWorkspace.vue:738-739`——一次点击 = 前端串行 N 次 SSE 请求。**这意味着任何"每段多几次 LLM 往返"的改动都会被放大 N 倍感知为"变慢"。**

### 1.2 Phase 0 留下的钩子

- `_selfcheck_enabled` 定义 `engine.py:441`，判据体 `:451`（`narrative_tier == "full"`）；**全仓唯一读取点 `engine.py:639`**【亲验】——单点开关，独立化的改动面很小。
- `_rewrite_with_evidence(db, project, provider, text, reasons, forbidden) -> (str|None, dict|None)` @`engine.py:624`；调用点 `:746` / `:838`。
- `_gather_rewrite_evidence` @`engine.py:479`；常量 `_SELFCHECK_FINDING_LIMIT/EVIDENCE_CHARS/CORPUS_CALLS/CORPUS_K` @`engine.py:435-438`。
- 哨兵取证 `load_findings(db, project_id, source="generated")` @`app/kg/sentinel.py:824`（engine 调用点 `:507`）。
- 检索 `hybrid_search(db, novel_id, query, k=10)` @`app/retrieval/search.py:44`。

### 1.3 工具层现状（可复用的真实边界）

- `AssistantToolRegistry` @`app/agents/registry.py:112`；`register`@`:123-145`（拒 `read_only is False`、拒 `FORBIDDEN_PREFIXES`、拒重名）；`invoke`@`:166-189`（黑名单前缀 → 未注册 KeyError → `read_only is False` → 最终 `spec.callable(db=db, project_id=project_id, **kwargs)`）。
- 已注册 11 个工具（`_DEFAULT_SPECS`@`:549-634`）全部 `read_only=True`：`search_novel_chunks` / `read_generation_segments` / `get_character_profile` / `get_plot_timeline` / `get_fact_ledger` / `get_reminders` / `summarize_bible` / `audit_corpus` / `audit_kg` / `audit_generation` / `analyze_logs`。
- **它们确实是纯读**【亲验】：`app/agents/` 下 grep `db.(add|commit|flush|delete|merge)` 仅命中 `prompt_overrides.py` 与 `proposals.py`——两者都**不在**只读白名单内。
- ⚠️ **`ALLOWED_READ_ONLY_TOOLS`（`registry.py:63`）在运行时从未被读取**【亲验 grep】。白名单是「事实上封闭的注册集」，不是运行时校验集；`register` 只校验 `read_only` 标志与前缀。**这意味着"注册即信任"，Phase 1 若新增工具，护栏并不自动生效。**
- ⚠️ `registry.py:17-22` 与 `:54-62` 的 docstring 称 audit 类工具「只写助手自己的 AgentRun + Evidence 两张审计表」——与实现不符：这些写入在**路由层**（`routers/assistant.py:171-181`、`:262`、`routers/assistant_shared.py:161-186`），工具 callable 自身零写入。属文档口径差，复用到 Writer 时该行为会**消失**（Writer 路径不写 AgentRun/Evidence）。

### 1.4 成本真相（决定 Phase 1 上限怎么定）

- **`hybrid_search` 每次调用都会加载该 novel 的全部 `NovelChunk` 行**（`search.py:46-48` `select(NovelChunk).where(novel_id == ...)`），再在 Python 内对全量算 BM25（`:52`）+ 逐 chunk 余弦（`:58-61`）。**代价与 `k` 无关，只与语料规模有关。**【亲验】
- 仓库唯一的「833 章 / 42 分钟」计时是**文风蒸馏**，不是检索（`docs/style-distillation-tiers.md:78-81`）。**检索本身没有精确计时记录**——Phase 1 定上限前必须先补一个基准。

### 1.5 SSE 面与前端中间态

- 引擎只产出两类事件：`{"type":"chunk"}`（`engine.py:826` / `:965`）与 `{"type":"done"}`（续写 `:865-877` / 重生成 `:992-1002`，**两形状字段不同**）。`{"type":"error"}` 由**路由包装层**在异常时下发（`routers/generation.py:131` / `:164`）。
- 前端解析：`api.generateStream`@`frontend/src/api/client.ts:338-386`、`api.regenerateStream`@`:389-439`；渲染 `ProjectWorkspace.vue:1926-1942`。
- **中间态 UI 已存在，但只表达「收到多少字」**：`ProjectWorkspace.vue:1929-1931`「正在生成… 已 N 字（第 x/y 段）」。**现有事件集合里没有任何一种能表达「模型正在调工具 / 等工具返回」**——这就是「卡住几秒」的根因，且不是纯前端问题。
- ⚠️ `frontend/src/api/types.ts:560-580` 的 done 类型**未声明**后端实际下发的 `style_warning` / `rewrite_info` / `summary_tree`——类型与运行时 payload 早已不对齐，Phase 1 加新事件时会被迫一并处理（否则又会出现「改了形状前端没跟」的老坑）。

### 1.6 确定性风险面（会碎的资产）

没有共享 `FakeProvider` fixture（`tests/conftest.py` 只做数据库隔离）；各测试自建 stub，**只有部分支持「按调用序号返回不同响应」**。断言了调用次数 / prompt 内容 / SSE 形状的测试，是最先碎的一批：

| 资产 | 位置 | 断的是什么 |
|---|---|---|
| `test_writer_selfcheck.py` | `:82,:86,:89`（`chat_calls` 按序号）、`:187-193`、`:219-225`、`:257`（快档检索调用==0）、`:334-339`、`:360-367` | Phase 0 核心，**最易碎** |
| `test_gen_pipeline_fixes.py` | `:66-69`、`:194-196`（preflight 前不调 LLM）、`:210-213` | 调用次数 |
| `test_stream_kg_bookkeeping.py` | `:126-192`、`:189`（含 `sentinel` 键）、`:216-220` | SSE done 形状 |
| `test_regenerate_kg_replay.py` | `:218,:263,:416` | SSE done 形状 |
| assistant 系列 | `test_assistant_diagnose.py:380,:404`、`test_assistant_audit.py:546,:577,:1041`、`test_assistant_provider_config.py:325` | `len(calls) == 1` |
| 旧式脚本 | `backend/test_m4_api.py:102-105,157-159,192-197,231-237,297-309`、`backend/test_p0_regression.py:63-78,137-141,337`、`backend/test_summary_tree.py:144-159,236-254,602-605` | prompt 内容与 rollup 计数 |

---

## 2. 三个方案选项

| 方案 | 实现路径 | 收益 | 代价 / 风险 |
|---|---|---|---|
| **A. 文本协议** | 在 user prompt 约定「需要时输出受控指令块 → 解析 → 执行 → 把结果追加进下一轮 user」 | 不改 provider 层；可用现有 `chat`/`stream_chat` | 污染正文（需强解析与剥离，现有 `llm/reasoning.py` 剥离推理块可参考）；解析脆弱、模型不听话即静默退化；**缓存击穿**（前缀被工具结果改写）；流式下的解析时机复杂 |
| **B. provider 层原生 tool 协议** | 扩 `LLMProvider` 接口 + 各 provider 适配 + 循环器 | 最干净、语义最正、未来可扩展 | **改动面最大**：provider 抽象是最底层，所有 stub/mock 都要跟着改；确定性测试资产大面积重写；仍需自行实现上限/超时/熔断 |
| **C. Phase 0.5：生成前单轮定向取证**（不引入循环） | 把 `build_context`（`engine.py:237`）里那条「RAG query 取 `prior[-1]`（上一段）」换成**按本段目标定向**的查询；仍只调用 1 次检索 | **零循环、零协议、零新增事件**；确定性与缓存都不受影响 | 拿不到「写到一半发现要改设定」的能力，但拿得到大部分查证收益 |

**已知的产品收益缺口（三条）**：①RAG query 与「本段要写什么」错位——写到一半想查查不了；②自检后重写不给原文对照（Phase 0 已修）；③工具层与 Writer 未打通。

→ 方案 C 精准命中 ①，且**不需要**任何新协议。方案 A/B 才有能力覆盖「生成中动态决策」。

---

## 3. 风险盘点

| # | 风险 | 触发条件 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | **成本爆炸** | 模型在一次生成里多次调 `search_novel_chunks` | 每次调用都全量加载该 novel 的 chunks（`search.py:46-48`）；N 次调用 = N× 全量 + 前端串行 N 段放大 | 全局调用上限必须钉成不变量；单段上限建议从 1 起；**先补检索基准计时再定数值，拒绝拍脑袋常数** |
| R2 | **确定性崩塌** | 任何「模型自行决定调 0~N 次」的设计 | §1.6 那批测试全部要重写 | 上限做成可注入常量并对测试暴露；stub 需支持按序号返回；把「上限」本身写成断言对象 |
| R3 | **提示词缓存击穿** | 工具结果插入上下文前缀 | 成本与首字延迟同时上涨 | 工具结果一律**追加在尾部**、不改前缀；工具轮次设硬上限 |
| R4 | **流式静默** | 工具调用期间无事件 | 用户看到「卡住几秒」 | 需新增事件（如 `tool`）+ 前端中间态；注意 §1.5 的类型未对齐问题 |
| R5 | **越权** | Phase 1 新增工具 / 复用注册表 | `ALLOWED_READ_ONLY_TOOLS` 运行时未被读取，护栏不自动生效 | 把白名单改成**运行时真校验**，或至少在 `register` 里加断言 |
| R6 | **异常语义污染** | 工具抛 `HTTPException`（`audit_corpus`@`registry.py:444-451`、`audit_kg`@`:479`、`audit_generation`@`:497`） | HTTP 语义进到引擎层无对应处理，可能整段失败 | 工具在引擎上下文内统一包成「失败即跳过 + 记录」，沿用 Phase 0 的熔断口径 |

---

## 4. 顺带发现（不在 Phase 1 范围，建议单独立项）

### 4.1 🟢 已解决（commit `b1e13d1`）：重生成路径的 system 提示缺「叙事文风」块

> **✅ 已修复**：由提交 `b1e13d1`（「重生成路径纳入叙事档案」）落地——`_build_generation_prompts`（`engine.py:1056`）现与续写入口一致传入 `_style_narrative(db, project)`，经 `_narrative_block`（`prompts.py:177`）渲染。本文在祖先 `c1809a8` 撰写时的结论对当前 HEAD 已过期，保留作历史记录。

- `generate_next_segment`（`engine.py:720-723`）调 `build_system_prompt` 传 **6 个参数**，含 `_style_narrative(db, project)`；
- `_build_generation_prompts`（`engine.py:907-910`，供 `regenerate_segment_stream` 用）只传 **5 个参数**，**缺 `_style_narrative`**。

→ 用户「重新生成」得到的段落，system 提示里**没有叙事文风块**，与两条续写入口口径不一致。这是**小而可测**的口径差，建议先单独修（改动 1 行级 + 1 条测试），不要混进 Phase 1。

### 4.2 其他口径差

| 项 | 状态 | 落地提交 | 说明 |
|---|---|---|---|
| `ALLOWED_READ_ONLY_TOOLS`（`registry.py:63`）运行时未被读取（R5） | 🟢 已修 | `c67f2ac` | `register()` 新增 fail-closed 校验：不在白名单的工具抛 `PermissionError`（见 R5 与 `tests/test_registry_whitelist.py` T1/T2/T3） |
| `registry.py` docstring 称 audit 工具「写审计两表」与实现不符 | 🟢 已订正 | `c67f2ac` | `:54-62` 注释改为：audit 工具 callable 纯读，写入在路由层（`routers/assistant.py`） |
| `regenerate_segment_stream` 不经 AI 腔自动重写，但已加零成本 AI 腔检测+告警（方案 b，复用 `_should_rewrite` 纯计数） | 🟢 已落地 | `73719c9`（实现）+ `3360e73`（QA 对抗） | regen done 带 `rewrite_info` 告警信号（`alert:true`、不重写）；自动重写留作方案 c opt-in |
| `frontend/src/api/types.ts` done 类型字段少于后端实际下发 | 🟢 已对齐 | `63119b6` | `GenerateStreamDoneMeta` 补 `summary_tree`/`style_warning`/`rewrite_info`；`RegenerateStreamDoneMeta` 补 `summary_tree` |

**③ 重生成缺 AI 腔重写——已落地方案 b（零成本检测+告警）**：

- 现状：`_should_rewrite`（`engine.py:548`）是**纯启发式、零 LLM 调用**（`_detect_ai_flavor` + 反向禁忌计数）；真正重写的 `_rewrite_with_evidence` → `_gather_rewrite_evidence` 的 **B 路会调 `hybrid_search` 全量加载整本语料**（`engine.py:621-623`）——正是 §8.1 刚从 regenerate 砍掉的代价。故「镜像续写重写」会 undo §8.1，必须单独拍板。
- 决策（用户拍板选 b）：
  - **(a) 完整镜像续写重写**：否决——会 undo §8.1 的语料加载节省，得不偿失。
  - **(b) 只检测 + 告警（零成本）**：🟢 **已落地**（`73719c9` 实现 + `3360e73` 独立 QA 对抗验证）。regen done 携带 `rewrite_info` 告警信号（`alert:true`、`rewritten_chars:null`、附 `reasons`），提示「本段可能残留 AI 腔，未自动修」，让重生成「说实话」。复用 `63119b6` 已开通的前端 `RegenerateStreamDoneMeta.rewrite_info` 字段位。
  - **(c) 检测 + 轻量重写**：只走 A 路哨兵历史告警、不加载语料、多一次 chat。**仍作为后续 opt-in**，若用户要自动修再立。
- 独立 QA 攻击验证（9 条，全通过）核心不变量：命中/干净/禁忌告警三场景均 `chat_call_count==0`（regen 不调 `chat`，排除方案 c 误触发）、落库文本与 stub 原始文本逐字节相等（无任何重写）、阈值边界 5 命中告警 / 4 不告警、反向禁忌 2 命中告警 / 1 不告警、`rewrite_info` 可 JSON 序列化。

---

## 5. 建议与待裁决

**建议顺序**（均已落地，留存为执行记录）：

1. 🟢 **§4.1 口径差已修**（`b1e13d1`）——重生成纳入叙事文风块。
2. 🟢 **方案 C（Phase 0.5）已落地**（`4592119`）——新增 `_rag_query_targeted`（`engine.py:165`）改造 `build_context`（`:401`），配套 `tests/test_rag_query_targeted.py`（11 条 + 变异取证 M1–M9），全量门禁已含。
3. 🟢 **§4.2 四项口径差已修三项**（`c67f2ac` R5 运行时白名单真校验 + docstring 订正；`63119b6` 前端 done 类型对齐）。
4. 🟢 **③ 重生成 AI 腔检测+告警（方案 b）已落地**：`73719c9` 实现 + `3360e73` 独立 QA 对抗验证；自动重写留作方案 c opt-in。

**待用户拍板**：无——③ 已按方案 b 落地，评估项全部结清。

---

## 6. 门禁核实留档（R5 验证过程，重要）

- 初版 R5 提交将回归测试落在 `backend/tests/test_registry_whitelist.py`，但 `pytest.ini` 的 `testpaths = ../tests` 只收集**根 `tests/`**——该目录（含同处此地的 `test_finale_clean.py` / `test_narrative_extract.py` / `test_style_warning.py` / `test_style_exemplar.py` / `test_name_normalize_probe.py` 共 5 个旧测试文件）**从未被门禁执行**，属「死测试」技术债。
- 后果：首轮「绿门禁」与独立 QA 的「SAFE TO MERGE」都建立在**未实际运行 T1/T2/T3** 之上，R5 当时**并未被门禁验证**。
- 修正：将 `test_registry_whitelist.py` 迁至根 `tests/`（与 `pytest.ini` 既定「单源真源在根 tests/」一致），并 fold 进 `c67f2ac`；重跑全量门禁确认该测试**真正被执行且通过**。
- 建议：另立项清理 `backend/tests/` 下其余死测试（或显式确认其退役），避免再次出现「绿门禁但测试没跑」的假阳性。

## 附：本文结论的独立复验清单

| 结论 | 复验方式 | 结果 |
|---|---|---|
| 全仓无 `tool_calls` 等原生工具协议 | `grep -rn 'tool_calls\|function_call\|tool_choice\|tools=' --include=*.py`（排除 worktrees） | 零命中【亲验】 |
| `_selfcheck_enabled` 只有一个读取点 | grep 全仓 | 仅 `engine.py:639`【亲验】 |
| 11 个只读工具 callable 自身零写入 | `grep 'db\.(add\|commit\|flush\|delete\|merge)' backend/app/agents/` | 仅命中 `prompt_overrides.py`/`proposals.py`（均不在白名单）【亲验】 |
| `hybrid_search` 每次全量加载 chunks | 读 `search.py:44-63` | 确认【亲验】 |
| Assistant 的「循环」是写死的三元组 | 读 `assistant.py:185-231` | 确认【亲验】 |
| 重生成 system 提示缺 `_style_narrative` | 对比读 `engine.py:714-735` 与 `:900-919` | 确认【亲验】 |
| `ALLOWED_READ_ONLY_TOOLS` 运行时未被读取 | grep 全仓引用点 | 原结论：仅 re-export 与测试引用【亲验】；**已在 `c67f2ac` 改为 `register()` 运行时真校验（fail-closed）** |
