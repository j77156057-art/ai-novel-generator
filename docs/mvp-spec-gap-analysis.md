# 任务书对照：MVP Spec v2 逐条现状（2026-11-02）

> 任务书 = `C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator-plan.md`
> （2026-10-12 的 wayfinder 关图产物：`/wayfinder` → to-spec → **MVP Spec v2**）。
> ⚠️ **发布版仓库按规矩不含 PRD**（任务书、语料、含原文的验收产物都不进仓库），
> 所以这份对照表放在这里，作为「任务书要求 ↔ 现状」的**唯一落点**。

## 0. 任务书要求的 MVP 是什么

| 项 | 任务书原文 |
|---|---|
| 范围 | 短篇（几千字）+ 番茄单源 + 快速模式 + 最小图谱（角色/坑/关系）+ 完整 Web（单用户占位登录） |
| 验证主链路 | **分段生成 → 图谱回写 → 总结注入** 能否防崩 |
| 技术栈 | Python(FastAPI) + **PostgreSQL(pgvector + JSONB)** + **React + Vite** + 统一云端 LLM 接口 |
| 里程碑 | M1 数据 → M2 检索 → M3 图谱 → M4 生成 → M5 前端 → M6 验收 |
| Fog of war（明确延后） | 云端供应商、本地模型混合、长篇伸缩、多平台、专家模式采访、版权风控、部署形态、epub/编码探测、live 爬番茄 |

## 1. 逐条对照

### 已做（且多数超出 MVP）

| 任务书条目 | 发布版现状 |
|---|---|
| M1 数据：种子语料 + 上传解析 + 入库 + 叙事切分 | ✓ `app/ingestion/store.py`（ingest_text / 自动分章 / 编码探测）；直传 `/api/novels/upload` 已**主动关闭**（409），强制走助理 agent 的「清洗 → 抽检 → 蒸馏 → 审查」全流程 |
| M2 检索：pgvector + BM25 混合召回 | ✓ `app/retrieval/search.py::hybrid_search`（schema 里同时给 `bm25` 与 `vector` 两路分） |
| M3 图谱：节点/边、坑生命周期、规则+LLM 双校验 | ✓ `kg_nodes` / `kg_edges` / `kg_v3` 路由 / 提醒接口；远超 MVP（角色卡、声纹、事实绑章号） |
| M4 生成：分段环 + 段级落库 + 续写 + 总结注入 | ✓ `app/generation/engine.py`（生成前注入：系统提示词 + 混合召回 + 图谱快照 + 上轮总结 + 本段目标；生成后：抽事实回写图谱 + 更新 `projects.summary`） |
| 风格维度化（视角/节奏/套路/篇幅） | ✓ `projects.style_dims` + 面板；后来扩展成「文风档案」维度（句长/标点/对白/形态…） |
| M5 前端：项目列表 / 新建 / 进度 / 预览 + 下载 / 历史 | ✓ 8 个页面（`/projects`、`/projects/:id`、`/corpus`、`/projects/:id/kg`、`/projects/:id/assistant`、`/assistant`、`/mine`、`/settings`） |
| M6 验收：端到端 + 自动一致性检查 + 人工抽检 | ✓ `backend/acceptance_lib.py` + `verify_acceptance_real.py` + 盲评 v2 + 深度攻击 + 真实浏览器端到端 |

### 明确的缺口（任务书写了、发布版没做）

| # | 缺口 | 任务书出处 | 现状 | 补齐成本 |
|---|---|---|---|---|
| **G1** | **导出只给正文**：没有「正文 + 大纲 + 人物表 + 坑清单」的一次性导出 | §2.6 生成环末步 | `/api/projects/{id}/export-txt` 只拼正文 + 一行元信息；大纲（`projects.outline` + 规划接口）、人物表（`/character-cards`）、坑清单（KG 伏笔 + `/reminders`）**数据都在，缺的是拼装与入口** | 小：一个导出接口 + 一个按钮 |
| **G2** | **没有登录占位**：第一屏不是登录 | §2.7 前端 | 路由 `/` 直接 redirect 到 `/assistant`；无 `/login` | 小：单用户占位页（任务书说「不做真多用户隔离」） |
| **G3** | **数据库默认不是 PostgreSQL** | §2.2 | 默认 SQLite；PG 是**可选**（`docker compose` + `requirements-postgres.txt`，README 有写）；且 `kg_nodes.props` 等 JSONB 在 SQLite 下是 JSON 文本列 | 中：默认值切换 + 迁移；对外说明「零配置起步，PG 可选」 |
| **G4** | **前端框架不是 React** | §2.2 / §1.5 | **Vue 3 + Vite**（8 个页面全是 .vue） | 不可改（已沉淀大量代码）⇒ 只能在 README 写清「为什么换」 |

### Fog of war 对照（任务书自己列为延后 ⇒ 现在仍延后是合规的）

live 爬番茄 / epub 与编码探测 / 多平台 / 本地模型混合 / 部署形态 / 版权风控 —— **都还没做，符合任务书**。
其中「**专家模式**」其实已实现（`projects.mode = expert`，前端标为「wayfinder 追问构思」）。

### 任务书没写、但发布版后来长出来的（超出 MVP 的加分项）

助理 agent 全家桶（wayfinder 式：附件暂存 → 清洗 → 抽检 → 蒸馏 → 审查 → 行动提案 + 审计链）、
角色卡与声纹、人物不千篇一律（persona/锚点）、全书摘要面板、盲评 v2（E1）、
深度攻击与真实浏览器端到端、发布卫生（空仓机自检、CI、文档数字被机器守着）。

## 2. 所以「还差什么」的诚实答案

**按任务书逐条，MVP 只差两件小事（G1 导出三件套、G2 登录占位）+ 两处技术栈偏离（G3 PG 默认、G4 React）**。

**但真正该做的大项不在任务书里**：任务书 10-12 定的目标是「防崩」，而发布版早把那条主线做完了；
现在项目的真问题是**「引擎到底有没有在向参考书学文风」** —— 这件事正在用跨书对照 v2 实测
（见 `ENGINEERING.md`「更正一则：判决 4」与 `docs/ruler-migration-table.md`）。

⇒ 建议的优先级：**先等 v2 的判决 1/3/4 出来，再决定补 G1/G2 还是先修出带**；
G1、G2 是「补任务书的账」，任何时候都能做（一两天内），但它们**不改变项目的核心结论**。
