# AI 小说生成器 · AI Novel Generator

> **把"写一部长篇网文"拆成一套可工程化、可验证、可防漂移的全栈生成系统。**
> A full-stack AI novel-generation engine: hybrid RAG retrieval, a knowledge-graph
> drift guard, **bounded hierarchical context**, a **globally-shared style-distillation
> library**, and a **dual-model Writer / Assistant agent architecture** — with a
> verification culture built on byte-level evidence, not vibes.

<p align="center">
  <b>FastAPI</b> · <b>Vue 3 + Vite + TypeScript</b> · <b>SQLAlchemy / PostgreSQL+pgvector / SQLite</b> ·
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
    Chunks -->|BM25 + 向量 embedding| Store[(PG+pgvector / SQLite)]
    Chunks -->|规则抽取 角色/坑/关系| KG[(知识图谱 KG)]
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
| Backend | **FastAPI** (Python 3.13), SQLAlchemy 2, Pydantic v2 (Strict) |
| Storage | PostgreSQL + **pgvector** (prod) · SQLite (dev/test) |
| Retrieval | Pure-Python **BM25 × dense-vector RRF fusion** |
| Knowledge graph | Deterministic rule extractor (LLM slot reserved on same interface) |
| Generation | Layered-prior engine + segment summary chain |
| Style | **Globally-shared distillation library**, 3 tiers, idempotent upsert |
| Frontend | **Vue 3 + Vite + TypeScript**, semantic design tokens (WCAG AA) |
| Agents | Writer/Assistant role isolation, `ProviderRegistry` two-track, proposal/approval workflow |
| LLM providers | DashScope (default) · OpenRouter (free 1M-ctx) · Local deterministic fallback |

---

## 🚀 Quick start

```bash
# 0) choose a database
#    dev (SQLite, zero setup):  DATABASE_URL=sqlite:///./novel_dev.db
#    prod (PG+pgvector):        docker compose up -d
#                              DATABASE_URL=postgresql+psycopg2://novel:novel@localhost:5432/novel

# 1) backend
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 2) embedding / llm keys  (copy .env.example -> .env; empty key => offline fallback)
#    EMBEDDING_PROVIDER=dashscope  EMBEDDING_MODEL=text-embedding-v3
#    LLM_PROVIDER=dashscope        LLM_MODEL=qwen-plus

# 3) frontend
cd frontend
npm install
npm run dev            # http://localhost:5173 , /api proxies to :8000
```

Endpoints (full list in `docs/`):

```bash
curl -X POST http://localhost:8000/api/novels/seed                 # seed corpus
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

**Planned (see `docs/architecture/` & `docs/product/`):** LLM-based KG extraction (same
`KGExtractor` interface), component-level `vitest` suite, and the full LangGraph
approval subgraph for the agent edit-proposal lifecycle.

> The "AI-tell" detector noted in planning is **design-stage**, not yet wired into the
> generation loop — tracked in the roadmap, not claimed as done.

---

<sub>Internal verification scripts (`_exp_*` / `_probe_*` / `_verify_*`) and local dev
databases are git-ignored and excluded from this repository.</sub>
