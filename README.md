# AI 小说生成器 · AI Novel Generator

> **把"写一部长篇网文"拆成一套可工程化、可验证、可防漂移的全栈生成系统。**
> A full-stack AI novel-generation engine: hybrid RAG retrieval, a knowledge-graph
> drift guard, **bounded hierarchical context**, a **globally-shared style-distillation
> library**, and a **dual-model Writer / Assistant agent architecture** — with a
> verification culture built on byte-level evidence, not vibes.

<p align="center">
  <b>FastAPI</b> · <b>Vue 3 + Vite + TypeScript</b> · <b>SQLAlchemy / SQLite（默认）/ PostgreSQL+pgvector（可选）</b> ·
  <b>RRF Hybrid Retrieval</b> · <b>Knowledge Graph</b> · <b>Agent Workflow</b>
</p>

---

## ✨ Why this belongs on a résumé

This is **not** a "call an LLM in a loop" demo. It is a small production-shaped system
where the hard problems are the ones senior engineers actually care about:

- **Context that doesn't blow up.** Long novels need prior context, but naive full-history
  concatenation explodes token budget. I built a **bounded hierarchical summary tree**
  (segment → volume → main-spine) with a *provable coverage invariant* — the prior-context
  block must continuously cover every segment `1..N` with **no holes**. At 300 segments the
  prior block dropped to **5.4% of the old naive size** (`uncovered == []`), verified
  offline and independently re-audited by a separate operator.
- **A style library you distill once and reuse forever.** Upload a corpus (e.g. *Dream of the
  Red Chamber*), distill its voice into a reusable profile at one of **three tiers**
  (完整 / 平衡 / 快速), and **any project** can apply it. Distillation lands in a
  **globally-shared, idempotent** library — no re-distilling per project.
- **Drift guard via knowledge graph.** Deterministic rule extraction of characters / plot-holes
  / relations feeds *generation-time reminders* so the model doesn't forget open threads or
  contradict established characters.
- **Dual-model agent architecture.** Writer and Assistant roles run on **separate model
  configs** (`ProviderRegistry` two-track), with concurrent isolation, autonomous diagnosis,
  and a **proposal → approval → commit/rollback** workflow for edits.
- **Verification as a first-class deliverable.** Every meaningful change ships with an
  offline-verifiable test, a byte-level `sha256` proof that unrelated modules were untouched,
  and an adversarial "try to break it" harness.

---

## 🧭 Architecture

```mermaid
flowchart TD
    Corpus[语料 / Corpus · txt] -->|UTF-8 解析 + 自动分章| Chunks[叙事切分 Chunks]
    Chunks -->|BM25 + 向量 embedding| Store[(SQLite 默认 / PG+pgvector 可选)]
    Chunks -->|LLM 软 schema 抽取（规则兜底）| KG[(知识图谱 KG)]
    KG -->|生成前提醒 reminders| Gen[分段生成环 Generation Loop]
    Store -->|RRF 融合召回| Gen
    Gen -->|段级总结| Tree[层级摘要树 Summary Tree]
    Tree -->|有界前情 compose_prior| Gen
    Gen -->|Writer 续写| LLM1[Writer Model]
    Gen -->|Assistant 诊断/提案| LLM2[Assistant Model]
    Corpus -->|文风蒸馏| Lib[(全局蒸馏库 StyleDistill · 三档)]
    Lib -->|apply 复用| Gen
```

**Generation data flow:** `KG reminders` + user **plot-bible** → merged constraints (one
authoritative merge, used twice: static system layer + near-end user reminder) →
`summary_tree.compose_prior` assembles **bounded layered prior** (main-spine / recent / volume)
→ RAG context (M2) → LLM writes → segment summary → append to `generations` + roll into the
summary tree (L1→ lazily L2/L3) → next segment reads the tree.

---

## 🛠 Tech stack

| Layer | Choice |
|---|---|
| Backend | **FastAPI** · Python **3.11–3.13**（3.13 实测）· `psycopg2-binary==2.9.13`（含 cp313 wheel）· SQLAlchemy 2 · Pydantic v2 (Strict) |
| Storage | **SQLite（默认，零配置）** · PostgreSQL + **pgvector**（可选，生产规模向量检索） |
| Retrieval | Pure-Python **BM25 × dense-vector RRF fusion** |
| Knowledge graph | LLM soft-schema extractor (v3.0, 批量合并抽取) + deterministic rule extractor 作离线兜底 |
| Generation | Layered-prior engine + segment summary chain |
| Style | **Globally-shared distillation library**, 3 tiers, idempotent upsert |
| Frontend | **Vue 3 + Vite + TypeScript**, semantic design tokens (WCAG AA) |
| Agents | Writer/Assistant role isolation, `ProviderRegistry` two-track, proposal/approval workflow |
| LLM providers | DashScope · 智谱 GLM · OpenRouter · SiliconFlow · MiniMax · OpenAI 兼容 · 本地确定性兜底 |

---

## 🚀 Quick start

```bash
# 0) 环境要求
#    Python 3.11–3.13（3.13 实测）；requirements.txt 会装 psycopg2-binary==2.9.13（带 cp313 wheel）
#    Node 18 / 20 / 22+（Vite 6 的 engines 要求；本仓在 Node 24 实测构建通过）

# 1) 后端：默认 SQLite，零配置，首次启动自动建表
cd backend
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000     # 由此启动时 SQLite 落在 backend/novel_dev.db

# 2) key（可选：不配也能起服务，但能力会降级——见下方「离线能力边界」）
#    EMBEDDING_PROVIDER=dashscope  EMBEDDING_MODEL=text-embedding-v3
#    LLM_PROVIDER=dashscope        LLM_MODEL=qwen-plus

# 3) 前端：按 lock 精确复现安装（回到仓库根再进 frontend）
cd ../frontend
npm ci                 # 必须用 npm ci；npm install 会按 ^ 范围重新解析，装出不同版本
npm run dev            # http://localhost:5173 , /api proxies to :8000
```

**什么时候才需要 PostgreSQL。** 默认 SQLite 就能跑完整流程（开发、单机、几千段以内）。
只有确实需要 **pgvector 向量索引 / 生产规模并发** 时才切换：

```bash
docker compose up -d     # 在仓库根执行：起 ankane/pgvector:pg16，映射 5432
# 然后改 backend/.env：
#   注释掉 DATABASE_URL=sqlite:///./novel_dev.db
#   取消注释 DATABASE_URL=postgresql+psycopg2://novel:novel@localhost:5432/novel
```

**种子语料目录。** `/api/novels/seed` 读 `SEED_DIR`（默认 `data/seed`）。相对路径一律按
**仓库根**解析，与在哪个目录启动 uvicorn 无关；目录不存在或不是目录时接口返回 **400**
并回显解析后的绝对路径，不再返回看起来成功的 `200 []`。仓库自带 `data/seed/*.txt`
可直接导入，重复调用按 `source_url` 幂等。

Endpoints（完整列表见服务起来后的 Swagger：`http://localhost:8000/docs`）：

```bash
curl -X POST http://localhost:8000/api/novels/seed                 # 导入 data/seed/*.txt（目录缺失返回 400）
curl -F "file=@小说.txt" http://localhost:8000/api/novels/upload   # upload
curl "http://localhost:8000/api/novels/{id}/search?q=林夜&k=5"      # M2 hybrid search
curl -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"我的小说","mode":"quick","target_length":5000}'     # create project
curl -X POST "http://localhost:8000/api/projects/{pid}/build-kg?novel_id={id}"  # M3 KG
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1],"max_chars":800}'            # M4 write a segment
curl -X POST http://localhost:8000/api/projects/{pid}/distill \
  -H 'Content-Type: application/json' -d '{"tier":"balanced"}'     # distill style library
```

---

## 🔌 离线能力边界（哪些有兜底，哪些没有）

| 能力 | 缺 key / 缺参考语料时 | 说明 |
|---|---|---|
| Embedding / 混合检索 | ✅ 有兜底 | 自动降级 `LocalFallbackProvider`（确定性哈希词袋向量）+ 纯 Python BM25；链路能跑通，但**语义质量远低于真实模型，仅限开发/测试** |
| 自由生成（无参考语料） | ✅ 有兜底 | 降级 `LocalFallbackLLMProvider`，用确定性模板把角色 / 未填坑 / 前情回显成正文；**不是可发布的正文** |
| **文风蒸馏** | ❌ **没有可用兜底** | 模板只产出占位文本，不产出蒸馏规则。未配可用 key 时整次蒸馏必然失败（全部语料块无规则），不会静默产出一份空档案 |

### 蒸馏失败怎么读

失败报错按**真实原因**分桶，每个桶附一句可执行动作，照做即可定位：

| 桶 | 含义 | 首先检查 |
|---|---|---|
| `no_key` | 没有可用 key | `.env` 的 `DISTILLER_API_KEY` / `WRITER_API_KEY`，或「模型设置」里给蒸馏单独选模型 |
| `auth` | key 无效或无权限 | key 与 `base_url` 是否配套（换渠道要换 key）、key 是否被禁用 |
| `rate_limit` | 限流 / 配额用尽 | 稍后重试或换账号；同渠道换其它免费模型绕不开账号级日限额 |
| `model_not_found` | 模型名不存在 | `providers.yaml` / `.env` 里的模型名（含大小写与后缀） |
| `network` | 不可达 / 超时 | `base_url` 是否可达，代理 / 防火墙 / DNS |
| `truncated` | 输出被截断、JSON 不完整 | 调高 `distiller.max_tokens`，或换不把思维链写进 `content` 的模型 |
| `protocol` | 返回的不是约定 JSON | 换指令遵循更好的模型，确认推理过程没混进 `content` |
| `unknown` | 未归类 | 看后端日志里的原始异常 |

### `distiller_max_tokens = 32768` 为什么这么大

蒸馏一次要输出 11 个维度的规则 JSON，且部分模型把 **reasoning 也计入 completion**。
8192 时实测：30 章三路对照里 deepseek 有 16 章触顶、qwen 30/30 章触顶并有一章整章失败；
单章 completion 峰值 32,805 token、输出文本最长 13,782 字，reasoning 占 completion 的 71%。
调到 32768 后两路截断归零。它是**输出上限**而非每次实际消耗，只有长章或带 reasoning 的模型
才会用满；语料更短或模型更稳时可在 `providers.yaml` 或 `DISTILLER_MAX_TOKENS` 下调。

---

## 🔬 Engineering rigor (the part I'm proudest of)

This project treats **evidence** as a deliverable:

- **Bounded prior with a proof.** `compose_prior` always reports `uncovered` (always `[]`);
  a *separate* operator (`test_summary_tree.py`, 73 assertions) re-derives coverage from the
  rendered text and fails if it disagrees. No self-reported truth.
- **Byte-level non-regression.** Refactors ship with a `sha256` comparison proving untouched
  modules are bit-for-bit identical (`verify_determinism.py`).
- **Adversarial verification harnesses.** Sentinel rule (`verify_sentinel.py`, 97 assertions +
  1.16M chars of real corpus, zero false positives), deep-attack suites against the agent
  (`qa4_verify_deep_attacks.py`), and a real-browser end-to-end panel check
  (`_verify_prior_panel.js`, 27 assertions).
- **Build gates.** Frontend ships only when `vue-tsc --noEmit` + `vite build` both pass;
  backend gets a real `uvicorn` HTTP smoke test across the full chain.

---

## 🗺 Status & roadmap

**Shipped (M1–M6):** data layer → hybrid retrieval → knowledge-graph drift guard →
segmented generation loop → Vue3 frontend → acceptance. Plus the globally-shared style
distillation library and the dual-model agent workflow.

**Shipped after M6:** KG v3.0 LLM extraction（软 schema + schema 漂移归一，规则抽取保留为
离线兜底）与批量合并抽取 worker；蒸馏失败按因分桶报错；SQLite 默认 + PostgreSQL/pgvector 可选；
前端 lock 可复现安装（`npm ci`）。

**Planned (see `docs/architecture/` & `docs/product/`):** component-level `vitest` suite and the
full LangGraph approval subgraph for the agent edit-proposal lifecycle.

> The "AI-tell" detector noted in planning is **design-stage**, not yet wired into the
> generation loop — tracked in the roadmap, not claimed as done.

---

<sub>Scratch harnesses matching `_exp_*` / `_probe_*` / `_verify_*` and local dev databases are
git-ignored and excluded from this repository; the tracked verification scripts cited above live in
`backend/` and `tests/`.</sub>
