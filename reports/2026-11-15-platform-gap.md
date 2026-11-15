# 平台自跑通性缺口清单（项目 6 收尾 / 2026-11-15）

> 只读审计。没有改代码、没有真跑生成、没有写库。
> 参照系：第 35–43 章是用 `D:\Temp` 下临时脚本直接调引擎写出来的；本清单要回答的是
> **平台自己的那条路（uvicorn + 路由 + 前端）能不能复现同一条路径**。

## 0. 结论摘要

- **断点在第 2 步：章节身份。** 平台四个生成入口都不传 `chapter_range`，落库一律 `段N`；
  一本从零开始的书在平台上永远停在段模式，章号/章纲/结构检测/节点对齐/跨章锚点全部关闭。
- **「不补就跑不通」：1 条**（章节身份，见 A1）。
- **「不补能跑但结果不对」：7 条**（B1–B7）。
- **需要实跑验证：2 条**（C1–C2）。
- 新项目第 1 章本身**能跑通**，没有硬崩溃：`premise` 为空也能过，`_require_distill_ready`
  在无参考书时直接放行。

## 1. 平台链路（实测代码位置）

| 步 | 位置 | 做了什么 |
|---|---|---|
| 1 | `frontend/src/views/ProjectWorkspace.vue:1511` | 主按钮（文案是「生成 N 段」，不是「下一章」）调 `api.generateStream({project_id, max_chars})` |
| 2 | `frontend/src/api/client.ts:411` | `POST /api/generate-stream`，请求体只有 `project_id` + `max_chars` |
| 3 | `backend/app/routers/generation.py:117-140` | `generate_stream()` 调 `generate_next_segment_stream(db, proj, novel_ids, max_chars=max_chars)` —— **没有 `chapter_range` 参数** |
| 4 | `backend/app/generation/engine.py:5123` | `generate_next_segment_stream` 内部：`chapter = _chapter_ordinal(chapter_range)`；`chapter_based` 由 `_timeline_progress()` 决定（`engine.py:4218`，取库里 `第N章` 序数最大值，>0 才算章模式） |
| 5 | `engine.py:2168` / `engine.py:5940` | 落库：`chapter_range` 为空就写 `f"段{segment_no}"` |

其它同源入口（都不传章号）：

- `POST /api/generate` → `generate_next_segment`（`engine.py:4653`）
- `POST /api/generate-loop` → `generate_loop`（`engine.py:6062`），向导 `/run` 也走这条
- `POST /api/regenerate-stream` → 重写指定 `segment_no`，不产新章

**临时脚本的唯一分叉点**就是第 3 步：脚本直接调 `generate_next_segment_stream(..., chapter_range="第N章《标题》")`，
平台没有这个字段的入口（`GenerateBody` 是 `StrictRequest`，多传即 422）。

## 2. 临时脚本替平台做的事 × 平台侧等价实现

| 脚本做的事 | 平台等价实现 | 结论 |
|---|---|---|
| 校验上一章已存在、不重复落库 | 无。段号自增，重复点会继续追加 | 缺口（B5） |
| 传 `chapter_range="第N章《标题》"` | 无端口暴露；引擎也不自己生成章名（全仓没有 `f"第{n}章"` 的生成点） | **硬缺口（A1）** |
| 内存注入卷四章节大纲条目（`chapter_outline_entry`） | `project.outline` 只在 `POST /projects` 创建时写（`projects.py:58/65`）；`ProjectPatch` 没有 `outline` 字段（`schemas.py:83-102`），没有追加/编辑端点 | 缺口（B1） |
| 内存注入卷四时间线节点（`current_node_prompt`） | 只有「首次生成时自动规划」一条路（`engine.py:4417/4445`，且要求 `project.outline` 非空）；没有追加节点、重分配章区间、强制重规划的端点 | 缺口（B2） |
| 给结构检测传扩展 outline（`_OutlineProxy`） | 无。`_run_structure_check`（`engine.py:1745`）直接用 `project.outline`，查不到条目就三项 `skipped`（`structure_check.py:296-320`） | 缺口（B3） |
| `apply_runtime_config` 换模型 | 有：`routers/settings.py` 已接 | 等价 |
| 写库/版本核对 | 共用落库逻辑 | 等价 |

## 3. 新项目从零 → 第一章

| 步 | 依赖 | 平台能否过 | 依据 |
|---|---|---|---|
| 1 建项目 | `premise` 可空、`outline` 可空 | 能 | `projects.py:58` |
| 2 出 bible | 向导 `bible` 步 + `POST /bible/apply` | 能 | `projects.py:637` |
| 3 蒸馏 | 无参考书 → 直接放行 | 能 | `_require_distill_ready`，无参考书直接过 |
| 4 点生成 | `build_user_prompt` 无 raise；`premise` 空也能出开篇 | 能 | `prompts.py` 里无 `raise ValueError` |
| 5 落库 | `chapter_range` 未传 → `段1` | 能，但**从此锁死在段模式** | `engine.py:2168` |
| 6 第 2 章起 | `_timeline_progress` 取不到章号 → `chapter=None` | 能跑，但章纲/结构检测/节点全空 | `engine.py:4218` |

即：**「从零跑到一章」技术上可行；「从零跑到一本有章纲的书」不补就结构性断。**
真正的断点不是第 1 章，而是第 1 章写完之后：没有任何一步能把 `段1` 升级成 `第1章《标题》`。

## 4. 项目 6 现状：在平台点续写会怎样

| 环节 | 实际取值 | 后果 |
|---|---|---|
| `_timeline_progress` | 库里最大章号 43 → `chapter = 44` | 章模式仍是开的 |
| `chapter_outline_entry(outline, 44)` | `None`（`outline` 只有 40 条） | 提示里**没有本章大纲** |
| `current_node_prompt(..., chapter=44)` | 已存时间线区间只到 40；`node_for_chapter(44)` 返回 `None`（`timeline.py:442-477` 不猜节点） | 是否注入旧节点取决于 `current_node` 现值，需实跑确认（C2） |
| `check_structure(chapter=44)` | 查不到条目 → `beat/premature/gender` 全 `skipped` | 平台会「看起来成功」但结构检测空转 |
| 落库标签 | `段61` | 与 43 章的 `第N章` 体系不一致；下次点续写仍算 44（最大章号还是 43） |
| provider | 由 settings/DB 决定，不是脚本写死的 `minimax / MiniMax-M3.1-Flash` | 同章不同源（B7） |
| KG / bible / 台账 | 都在库里 | 这部分同源 |

结论：**能跑，但和 43 章不同源**——无章纲、无节点、无结构检测、无章号。

## 5. 缺口清单

### A. 不补就跑不通

**A1 章节身份没有生产者**
- 缺什么：把「第 N 段」提升为「第 N 章《标题》」的代码路径，以及生成入口的章号入参。
- 在哪：`engine.py:2168`、`engine.py:5940`（只兜底写 `段N`）；`routers/generation.py:117/89/187`（三个入口均无章号）；
  `schemas.py` 的 `GenerateBody` 无 `chapter_range`。
- 要补什么：要么把 `chapter_range` 加进入参并由调用方按 `outline` 条数计算；
  要么让引擎在 `chapter_based` 时按 `outline` 自动生成 `第N章《标题》`（更符合"平台自己会写书"的目标）。
- 补完能验证：库里新 Generation 的 `chapter_range` 形如 `第44章《…》`；`structure_check` 的 `mode` 从 `report` 变为有非 skipped 项。

### B. 不补能跑但结果不对

**B1 `outline` 无法追加/编辑**
- 在哪：`schemas.py:83-102`（`ProjectPatch` 无 `outline`）；`projects.py:525`（PATCH 落库字段来自该 schema）。
- 要补什么：`ProjectPatch.outline` + 追加/校验（条数与章号连续性）。
- 补完能验证：PATCH 后第 44 条条目存在，`chapter_outline_entry(project.outline, 44)` 非空。

**B2 时间线节点与章区间无追加入口**
- 在哪：`engine.py:4403-4460`（只在生成时规划）；`timeline.py` 的 `assign_chapter_ranges` 无路由调用。
- 要补什么：节点追加/重规划端点（或在追加 outline 时自动 `assign_chapter_ranges`）。
- 补完能验证：`plot_timeline` 里出现 41–47 区间，`node_for_chapter(44)` 非 None。

**B3 结构检测越界静默 skipped**
- 在哪：`structure_check.py:296-320`（输入缺失即 skipped，不报错）。
- 要补什么：章节条目缺失时给出显式告警（不是判罚），至少让前端显示「本章无大纲」。
- 补完能验证：第 44 章质量回执中出现「missing outline entry」类标记，而不是三项静默 skipped。

**B4 平台无「上一章存在 / 不重复」校验**
- 在哪：`routers/generation.py:117-140`；脚本侧对应逻辑在 `D:\Temp\p6_ch38_run.py`。
- 要补什么：重复点击的幂等保护（同一进度不重复追加）。
- 补完能验证：连点两次只多 1 段。

**B5 大纲本身没有生成器**
- 在哪：`wizard_actions.py:533`（`answers["outline"]` 只是开关）；`assistant_wizard.py:1191`（只是路由到 `stage="outline"`）；
  全仓无写 `project.outline` 的第二处（除 `projects.py:65`）。
- 要补什么：从 premise/bible 生成 N 条章纲的服务 + 「确认后写库」的端点。
- 补完能验证：新项目在向导里能拿到可编辑的章纲列表，且确认后 `project.outline` 非空。

**B6 `plot_rhythm` 从未计算**
- 在哪：`rhythm.py:165` 定义 `compute_plot_rhythm`，全仓无调用点。
- 要补什么：建项目/换参考书时算一次并写 `project.plot_rhythm`。
- 补完能验证：`project.plot_rhythm` 非空，`plan_timeline` 不再用默认 `avg_node_segments=5`。

**B7 provider 与脚本不同源**
- 在哪：`routers/settings.py`（`apply_runtime_config`）；脚本侧直接写死 `minimax / MiniMax-M3.1-Flash`。
- 要补什么：不需要改代码，需要在平台侧把「正文生成模型」固定成与验收样本一致的配置（属运维/配置项）。
- 补完能验证：平台新章的 `draft_stream.model` 与 43 章相同。

## 6. 需要实跑验证（不烧额度）

**C1 空 outline 时 `current_node_prompt` 的真实返回值**
- 静态判断：`engine.py:4417/4513/4550` 都 `if not project.outline: return None`，应返回 `None`。
- 验证方式：`tests/test_v25_stage23.py` 那套 fake provider harness 里建一个无 outline 项目，
  调 `generate_next_segment`，断言提示里没有 `【当前剧情点】`。

**C2 项目 6 chapter=44 越界时 `current_node` 的实际值**
- 静态判断：`sync_node_for_chapter`（`timeline.py:442-477`）在区间外**不猜节点**，保留现值；
  若 40 章写完后已清空则为 `None`，否则会注入**最后一个旧节点**的提示。
- 验证方式：读库里 `project.plot_timeline` 的 `current_node` 字段即可判断，不需要跑模型。

## 7. 建议的最小验证（一次不烧额度的端到端）

1. 复用 `backend/tests` 里已有的 fake provider 集成测试（`test_v26_engine_integration.py`、`test_v25_stage23.py`），
   加一条：`POST /projects`（空 outline）→ 调生成一次 → 断言 `chapter_range == "段1"`、`structure_check` 三项 skipped。
   这条能把 A1/B3 变成可回归的红灯。
2. 再补一条：`POST /projects`（带 3 条 outline）→ 生成一次 → 断言 `chapter_range` 仍是 `段1`。
   若期望是 `第1章`，这就是 A1 的验收用例。
