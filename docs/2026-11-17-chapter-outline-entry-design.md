# 章纲写入与编辑入口 · 设计稿（待确认）

- 目标：补齐「章纲生产端」，让第 44 章及以后有章纲可依，已有 1–43 章可编辑/可显式标记缺失。
- 边界：本阶段**不跑生成、不写生产库、不碰 KG 表结构**；测试全部用内存/临时 SQLite。
- 状态：**设计，未落任何业务代码**。

---

## 0. 先说三条事实校正（影响方案选型）

1. **今天没有章纲编辑入口，但不是完全没有写入点。**
   - 创建项目时写一次：[projects.py](backend/app/routers/projects.py#L59-L65) `outline=body.outline`。
   - `PATCH /projects/{id}` **不能**改 outline：[schemas.py](backend/app/schemas.py#L90-L111) 的 `ProjectPatch` 没有 `outline` 字段；`patch_project` 只按 `model_dump(exclude_unset=True)` 的已知字段 `setattr`（[projects.py](backend/app/routers/projects.py#L605-L662)）。`outline` 出现在 `ProjectOut`（[schemas.py](backend/app/schemas.py#L114-L121)），是**只读返回**。
   - 结论：除创建时外，当前没有任何受校验的章纲写入路径。

2. **前端没有章纲/大纲 UI。** `ProjectWorkspace.vue` 全文件对 `outline|大纲|章纲` 零命中；`views/` 下也没有独立大纲页。

3. **章区间不是手工字段，而是按 outline 长度均分出来的。**
   - `state_from_dict(..., total_chapters=len(outline))` 每次恢复都会 `assign_chapter_ranges`（[timeline.py](backend/app/generation/timeline.py#L514-L577)）。
   - `_balanced_chapter_ranges` 按大纲条数均分（[timeline.py](backend/app/generation/timeline.py#L360-L398)）。
   - 因此**改 outline 长度 = 改所有节点的章区间**；这一点必须在保存回执和界面上显式告知，不能静默。

---

## 1. 数据结构：复用 `project.outline`，不新建表

### 1.1 决策

| 方案 | 结论 | 原因 |
|---|---|---|
| 复用 `project.outline`（`list[str]`，下标=章号-1） | **采用** | 零 schema 迁移；`chapter_outline_entry`、`_outline_entries`、`plan_nodes_with_llm` 已经按这个形态读；第 44 章以后追加即可生效 |
| 新表 `chapter_outlines` | 不采用（本阶段） | 需要建表/迁移；且要让生成链路读到，仍得同步回 `project.outline` 或改 `prompts.py`/`engine.py`（共享文件） |
| 把条目改成 `list[dict]`（带 title/status） | 不采用 | `chapter_outline_entry` 对每条做 `str(x)`（[prompts.py](backend/app/generation/prompts.py#L795-L813)），dict 会被渲染成字面量，必须改共享文件 |
| 复用 `CausalNode`（`generation/planning.py`） | 不采用为章纲载体 | `CausalNode` 是 3–12 个**场景级**因果节点，不是 40+ 条章级提纲；它适合以后做「规划扩展」，不适合一章一条 |

### 1.2 存储约定（关键细节）

- `project.outline` 统一为 `list[str]`，第 N 章 = `outline[N-1]`。
- **缺失章存空字符串 `""`，绝不存 JSON `null`。**
  - 原因：`chapter_outline_entry` 会 `str(x).strip()`；`str(None)` 得到 `"None"`，会把「无章纲」当成一条内容注入提示。
  - `""` 经 `entry or None` 返回 `None`，与「没有章纲」语义一致。
- 对外 API 可以把缺失显示成 `entry: null`、`status: "missing"`，但落库仍是 `""`。
- 不在 `ProjectPatch` 上补 `outline`：那会是一条**绕过全部校验**的写入路径，与「保存时必须校验」冲突。

### 1.3 草稿态怎么落

**采用无状态草稿 + 指纹**（与 main 的 `outline/apply` 同思路，复用 feat 已有的 `planning.source_fingerprint`）：

1. `POST .../draft` 返回候选条目 + `source_fingerprint`，**不写库**。
2. `POST .../apply` 必须带同一个 `source_fingerprint` 且 `confirm: true`，校验通过才写。
3. 期间源数据（premise/bible/outline/plot_timeline）有任何变化 → 指纹不一致 → 409 `stale`，拒绝应用。

好处：不新增表、不新增列；AI 永远不可能「直接生效」。
代价：关页面草稿丢失（可接受；后续若需要持久草稿，再加表，不影响本设计）。

---

## 2. 已有 43 章历史章纲怎么办

现状（依 `reports/2026-11-15-platform-gap.md`）：43 章正文、约 40 条 outline。

**结论：允许缺省，但必须显式标注；不自动补齐。**

- 第 1–40 章：`status="confirmed"`，可编辑。
- 第 41–43 章：`status="missing"`，在 GET 返回 `missing_chapters: [41,42,43]`，界面顶部固定横幅列出；生成时仍走现有节点提示，但需要（S4）在预检/生成回执里打 `outline_status="missing"`。
- 第 44 章起：可通过单章写入或区间 apply 新增；写入第 44 章时，41–43 继续保持 `""`，并在回执里明确 `auto_filled_missing: [41,42,43]`（不是静默补空）。
- **不自动用正文反推章纲**：那会让 AI 生成的摘要直接变成「历史事实」。另提供独立入口（S5）`draft-from-text`：只对**已定稿**章节生成草稿，仍要人工确认才写。

为什么不做「一键补齐 43 章」：旧章正文已经存在，章纲的作用只剩「重写/续写时参考」。自动补齐会把模型对旧正文的概括写回权威层，等于用二手摘要覆盖一手事实，且无法验证。显式缺省 + 按需补写更诚实。

---

## 3. 端点清单

全部放在**新文件** `backend/app/routers/chapter_outline.py`，前缀 `/api/projects`，路由注册只改 `main.py`（见 §7 共享文件清单）。

### 3.1 按区间批量看

`GET /api/projects/{project_id}/chapter-outline?from_chapter=1&to_chapter=60`

返回：

```json
{
  "project_id": 1,
  "outline_len": 40,
  "written_chapters": 43,
  "missing_chapters": [41, 42, 43],
  "first_missing_chapter": 41,
  "source_fingerprint": "…",
  "entries": [
    {"chapter": 1, "entry": "第1章《…》：…", "status": "confirmed",
     "written": true, "node_id": "n1", "node_range": [1, 5]},
    {"chapter": 41, "entry": null, "status": "missing",
     "written": true, "node_id": "n8", "node_range": [41, 44]}
  ],
  "checks": [
    {"rule": "outline_coverage", "status": "failed",
     "detail": "第41–43章无章纲（已显式标记）"},
    {"rule": "node_mapping", "status": "checked"}
  ],
  "skipped_checks": [],
  "warnings": []
}
```

- 区间外/超出 outline 长度的章也返回，`status="missing"`（不省略、不静默截断）。
- `node_id/node_range` 只从存档 `plot_timeline` 读；没有存档时置 `null` 并加 `warnings: [{"code":"no_timeline"}]`。
- 不调用 LLM、不写库。

### 3.2 编辑单章

`PUT /api/projects/{project_id}/chapter-outline/{chapter}`

```json
{"entry": "第44章《…》：…", "expected_fingerprint": "…", "confirm": true}
```

- 校验后写 `outline[chapter-1]`；章号超过当前长度时补齐中间章为 `""`，回执 `auto_filled_missing`。
- 成功返回单章结果 + `validation` + `timeline_impact`（见 §5）。

`DELETE /api/projects/{project_id}/chapter-outline/{chapter}`：显式把该章标记为缺失（写 `""`），返回 `status="missing"`。这是「允许缺省跳过」的唯一入口，不靠空字符串隐式表达。

### 3.3 区间保存 / 应用草稿

`POST /api/projects/{project_id}/chapter-outline/apply`

```json
{
  "from_chapter": 44, "to_chapter": 50,
  "entries": [
    {"chapter": 44, "entry": "第44章《…》：…"},
    {"chapter": 45, "entry": null}
  ],
  "expected_fingerprint": "…",
  "confirm": true,
  "reset_timeline": false
}
```

- `entries` 必须**恰好覆盖** `[from, to]` 每一章；`entry: null` = 显式缺失，缺项 = 错误。
- `confirm` 必须为 `true`；否则 409 `confirm_required`。
- `reset_timeline=true` 才清空 `project.plot_timeline`（会丢 `done/early_done/seg_counts`）；默认 false，回执说明节点内容可能陈旧。

### 3.4 AI 起草（草稿态，不生效）

`POST /api/projects/{project_id}/chapter-outline/draft`

```json
{"from_chapter": 44, "to_chapter": 50, "direction": "可选：这几章想往哪个方向走"}
```

- 上下文：`premise` + 已确认 bible（角色名/关系/伏笔）+ 第 1..from-1 条 outline + 最近已定稿章摘要（可选）。
- 复用 `generation/planning.py` 的 `model_json`（限次重试、不回显模型原文），不另造调用层。
- 返回 `{"status":"draft", "entries":[...], "source_fingerprint":"…", "validation":{...}, "warnings":[...]}`，**不写库**。
- provider 取不到 → 503；渠道失败 → 502（复用 `projects._provider_or_none/_model_call` 的语义）。

### 3.5（可选，独立子块）从已写正文起草

`POST /api/projects/{project_id}/chapter-outline/draft-from-text`

```json
{"from_chapter": 41, "to_chapter": 43}
```

只读 `Generation.text`，为每章生成一条摘要草稿，仍走 apply 人工确认。用于补 41–43，不默认执行。

---

## 4. 校验：每条规则都必须有 checked / skipped 记录

统一回执结构（`PUT` / `apply` / `draft` 共用）：

```json
{
  "ok": false,
  "errors": [
    {"code": "chapter_gap", "chapters": [42, 43], "message": "第42、43章缺少条目；若要缺省请显式传 entry: null"}
  ],
  "warnings": [
    {"code": "written_chapter_edit", "chapter": 30,
     "message": "第30章已定稿；改章纲只影响重写，不会修改已落库正文"}
  ],
  "checks": [
    {"rule": "chapter_continuity", "status": "checked"},
    {"rule": "chapter_prefix", "status": "checked"},
    {"rule": "time_window", "status": "skipped",
     "reason": "no_absolute_time_tokens_in_range"},
    {"rule": "authority_items", "status": "checked",
     "sources": ["premise", "bible", "kg_entity:24"]}
  ],
  "skipped_checks": ["time_window:no_absolute_time_tokens_in_range"]
}
```

- `errors` 非空 → 422 `{"error":"outline_invalid","report":...}`，**整批不落库**（与 `patch_project` 的「校验在 setattr 之前」一致）。
- `skipped_checks` 非空 → 仍然可以保存（除非该规则被配置为必需），但**界面必须展示**，返回里必须出现；不允许「查不了就当通过」。
- 每条规则无论结果如何都写 `checks`；这是这次「不许静默」的硬约束。

### 4.1 章号连续性（硬错误）

- `from_chapter >= 1`、`from_chapter <= to_chapter`、`to_chapter - from_chapter + 1 === entries.length`。
- `chapter` 必须是整数、不重复；区间内每一章都必须出现。
- 缺项 → `chapter_gap`（列出具体章号），不允许用「跳过」表达缺省；缺省必须显式 `entry: null`。
- 条目为空串 → `empty_entry`；要缺省请用 `null` / DELETE。
- 条目以 `第N章/回/节` 开头且 N ≠ 下标 → **warning** `chapter_prefix_mismatch`（兼容「第X回」和标题式章纲，不做硬拦）。

### 4.2 时间窗冲突（能算就算，算不了必须标 skipped）

解析口径（只认字面，不做语义推断）：

- 绝对时间：`凌晨/早上/上午/中午/下午/傍晚/晚上/夜里/半夜` + `H:MM` 或 `H点[MM分]`。
- 相对推进：`次日/翌日/第二天/隔天/当天/同日/与此同时/N分钟|小时|天后`。
- 与已有节点对照：从 `plot_timeline.nodes[*].entry_condition/exit_condition/title` 里抽同样的时间标记。

规则：

1. 相邻两章都有绝对时间、后一章没有「次日/翌日/第二天…」且 `time(N+1) < time(N)` → **error** `time_window_backwards`。
2. 时间相同且无 `与此同时/同日/当天` → **warning** `time_window_same_without_marker`。
3. 候选章时间与其节点区间内的时间相矛盾（且无推进词） → **warning** `time_window_node_mismatch`。
4. 区间内没有绝对时间标记 → `status="skipped", reason="no_absolute_time_tokens_in_range"`；节点侧也没有 → 继续记 `no_node_time_tokens`。

明确局限：这是**字面规则**，不是时间线求解器；跨章日期推进、倒叙、多线并行都可能误报，所以默认只对「时间倒退」硬拦，其余给 warning。查不到一律 `skipped`，不冒充通过。

### 4.3 权威外物件（字面检查，分级处理）

权威来源（按现有生成链路实际使用的）：

- `project.premise`
- `project.plot_bible`（`characters` 姓名、`foreshadowing`、`locations` 等，整体 `json.dumps` 文本做子串匹配）
- `KgEntity`（本项目、`canonical_name` + `aliases`，不限 kind）

候选抽取：

- **引号内名词**：`「…」`、`“…”`、`《…》`。命中权威文本 → 通过；未命中 → **error** `authority_quoted_term_unknown`（引号是作者明确强调的专名，误报率低）。
- **物件后缀词**：`函/册/卷/刀/剑/玉佩/钥匙/信封/文件/档案/药/针/钟/盒/箱/镜/牌/令/符…`。未命中 → **warning** `authority_item_suspect`（中文名词识别不可靠，不硬拦）。
- 权威文本为空或过短（< 20 字）→ `status="skipped", reason="authority_empty_or_too_thin"`。
- 不在这里调用 `closed_inventory_v2.inspect_inventory(protocol=5)`：它是多阶段 LLM 协议，适合异步诊断/规划扩展，不适合同步保存；回执里明确写「本次为字面检查，不是完整清单协议」，避免把弱检查说成强检查。

---

## 5. 保存后的时间线影响（必须回执 + 界面提示）

`apply` / 单章写入返回：

```json
{
  "outline_len_before": 40,
  "outline_len_after": 44,
  "chapter_ranges_recomputed": true,
  "node_content_stale": true,
  "reset_timeline": false,
  "note": "节点章区间会按新大纲长度重算；节点标题/entry/exit 仍来自旧章纲。需要重规划请显式 reset_timeline=true（会清空已完成进度）。"
}
```

- 长度变化 → `assign_chapter_ranges` 下次恢复时自动重算（已有逻辑，不改）。
- 内容变化但长度不变 → 区间不变，但节点内容可能陈旧，同样标 `node_content_stale=true`。
- **默认不自动重建节点**：43 章项目的 `done/early_done/seg_counts` 是真进度，不能因为改一条章纲就清空。
- 界面必须显示这段说明；`reset_timeline` 是勾选框，不是默认行为。

---

## 6. 与生成链路的衔接（不静默的落点）

- **消费端已经存在**：`chapter_outline_entry(project.outline, chapter)` 在生成提示里取本章条目（[prompts.py](backend/app/generation/prompts.py#L795-L813)）。所以只要把 44 章写进 `outline`，下一次生成就能读到，**本阶段不需要改生成引擎**。
- **但「缺章纲」目前在生成侧是静默的**：`chapter_outline_entry` 返回 `None` 后，提示里就没有「本章大纲」块，调用方不记录。要满足「界面可见」，需要 S4：
  - `routers/generation.py` 的 `/generate-plan` 响应加 `outline_status`（`confirmed|missing|beyond_end` + `missing_chapters`）；
  - `generate_next_segment` / 流式回执的 meta 同步带同一字段；
  - 可选：`engine.current_node_prompt` 返回结构化结果（现在返回 `str|None`），这是共享文件改动，S4 单独报批。
- **S4 之前**，新页面自身保证：GET/保存回执都带 `missing_chapters` 与 `skipped_checks`，不会静默。

---

## 7. 前端入口

### 7.1 位置

新增独立页面：`/projects/:id/outline` → `frontend/src/views/ChapterOutlineView.vue`。

不在 `ProjectWorkspace.vue` 里加 Tab：另一会话正在做批 10 前端接线，`ProjectWorkspace.vue` 是高风险共享文件；独立页只碰路由/导航/API 三个薄文件。

### 7.2 页面结构

1. **顶部状态条**：`已确认 1–40 / 缺失 41–43 / 已写 43 章`；缺失章号与 `skipped_checks` 固定展示。
2. **区间查看**：`from/to` 选择器 → GET 批量读；表格列：章号 / 状态 chip / 章纲 / 是否已写 / 所属节点与区间 / 操作。
3. **单章编辑**：行内编辑，保存走 PUT；错误逐条显示 `errors[].message`。
4. **AI 起草**：选区间 + 方向 → 生成草稿面板（每章 textarea，默认未确认）；点「确认生效」才 apply；草稿期间显示指纹，源变化时提示重新起草。
5. **时间线影响提示**：保存回执里的 `timeline_impact.note` 必须展示；`reset_timeline` 勾选框带二次确认。
6. **缺失章显式标记**：`missing` 行不能是空白行，必须有「无章纲」chip + 「补写」按钮。

### 7.3 共享前端文件（需先报备）

- `frontend/src/router/index.ts`：新增一条路由。
- `frontend/src/App.vue`：项目内导航加入口（若不做导航，可先只靠直接 URL，避免碰 App.vue）。
- `frontend/src/api/client.ts`：新增 5 个函数（list/put/delete/apply/draft）。
- `frontend/src/api/types.ts`：新增请求/响应类型。

新增文件：`frontend/src/views/ChapterOutlineView.vue`。

---

## 8. 子块拆分与提交计划

| 子块 | 内容 | 新增文件 | 共享文件改动 | 测试 |
|---|---|---|---|---|
| **S1** | 纯逻辑模块：outline 归一化、缺失标记、章号连续性、时间窗、权威字面检查、指纹 | `backend/app/generation/chapter_outline.py`；`backend/tests/test_chapter_outline_entry.py` | **无** | 内存库/纯函数，`python tests/test_chapter_outline_entry.py`；报 PASS/FAIL 数 |
| **S2** | 新路由：GET/PUT/DELETE/apply/draft | `backend/app/routers/chapter_outline.py`；`backend/tests/test_chapter_outline_api.py` | `backend/app/main.py`（import + include_router，2–3 行） | 直接调路由函数 + 临时 SQLite；假 provider 测 draft；报 PASS/FAIL 数 |
| **S3** | 前端独立页 + API | `frontend/src/views/ChapterOutlineView.vue` | `router/index.ts`、`api/client.ts`、`api/types.ts`（`App.vue` 可选） | `npm run build` / `vue-tsc`；报结果 |
| **S4** | 生成侧可见标记 | 无 | `routers/generation.py`（响应字段）；可能 `engine.py`/`schemas.py` | 定向测试；改前单独报文件清单 |
| **S5** | 从已写正文起草（可选） | 路由内新增端点或独立模块 | 无（除 S2 文件） | 假 provider + 临时库 |

执行顺序：S1 → S2 → S3 →（S4 报批）→（S5 可选）。每块独立提交，提交信息写清子块，报 hash + 定向测试数字。

---

## 9. 需要你拍板的 4 个点

1. **历史缺失**：同意「41–43 显式 missing、不自动补齐」吗？还是要我在 S2 同时做「从正文起草 41–43」的入口？
2. **时间线影响**：改章纲默认**不重建节点**、只提示 `node_content_stale`，可以吗？还是改完就重规划（会丢 done/early_done）？
3. **权威物件检查**：按上面的「引号内硬拦 + 后缀词 warning」分级，够不够？是否要接 `closed_inventory_v2` 做成异步强检查（成本高，不在同步保存里）。
4. **前端入口**：独立页 `/projects/:id/outline` 是否可接受？如果必须嵌进 `ProjectWorkspace.vue`，需要和批 10 会话协调该文件。

确认后我从 S1 开始，先只提交新增文件，不碰共享文件。
