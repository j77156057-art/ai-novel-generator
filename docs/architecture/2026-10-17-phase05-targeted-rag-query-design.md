# Phase 0.5 详细设计：生成前单轮定向取证（RAG 查询词对齐本段目标）

- 日期：2026-10-17
- 分支：`feat/resume-push-2026-10`（HEAD `c1809a8`）
- 定位：Phase 0（生成后重写取证，已交付 `ea85d6b`）与 Phase 1（生成中工具循环，见评估文档）之间的**最小增量**
- 状态：**设计稿，未写功能代码**
- 姊妹文档：`docs/architecture/2026-10-17-phase1-writer-tool-calling-assessment.md`
- 证据口径：标 `文件:行号` 者均已由主理人独立复读确认。

---

## 0. 一页结论

**要解决的问题**：写第 N 段时，检索参考语料用的查询词是**第 N-1 段的摘要**——它描述的是「刚写完什么」，而不是「接下来要写什么」。于是检索回来的素材与本节目标系统性错位；写第一段时尤其明显（`prior` 为空，查询词退化成「书名 + 大纲第一条」）。

**方案**：查询词从「纯衔接侧」改为「**目标侧 + 衔接侧**」组合——目标侧取**已经算好、零额外 IO** 的 `reminders`（未填伏笔 + 角色约束，`engine.py:245` 已加载）；衔接侧保留 `prior[-1]` 但**只取尾部窗口**。无目标信号时**逐字节退回现状**。

**为什么它比 Phase 1 划算**：不改 provider 层、不引入工具协议、不加 SSE 事件、**不改变 `hybrid_search` 的调用次数**（仍每本语料 1 次）。确定性与成本两个最贵的风险面都不动。

**规模**：查询词构造 + 一个可观测字段 + 测试，预计 1 人日量级。

---

## 1. 现状取证

### 1.1 错位的本体

```python
def _rag_query(prior: list[str], project: Project) -> str:   # engine.py:103
    if prior:
        return prior[-1]                                     # :105 ← 用「上一段摘要」当检索词
    parts = [project.title or ""]
    outline = project.outline
    if isinstance(outline, (list, tuple)) and outline:
        parts.append(str(outline[0]))
    return " ".join(p for p in parts if p)                   # :110
```

- `prior` 来自 `_prior_summaries(project)`（`engine.py:73-85`）＝ L1 摘要链，按段号升序；`prior[-1]` 即**最新一段的摘要**。
- 调用链：`build_context`（`engine.py:237`）→ `query = _rag_query(prior, project)`（`:260`）→ `for nid in novel_ids: hybrid_search(db, nid, query, k=4)`（`:261-264`）。
- 首段特例：`prior` 为空 → 查询词 = 「书名 + 大纲第一项」——与"本段要写什么"同样无关（大纲是整篇的，见 §1.3）。

### 1.2 `build_context` 的调用方（改动面清单）

| 调用点 | 用途 | 是否受本次影响 |
|---|---|---|
| `engine.py:719` `generate_next_segment` | 非流式续写 | 受影响（查询词变化） |
| `engine.py:806` `generate_next_segment_stream` | 流式续写（**前端主路径**） | 受影响 |
| `engine.py:960` `regenerate_segment_stream` | 重生成 | 只读 `["constraints"]`（见 §8.1，本次不动） |
| `routers/generation.py:206` | `generate-plan` 预览端点 | 受影响（**也是最好的 A/B 观测点**） |
| `tests/`、`backend/test_*.py`、`backend/verify_*.py` 若干 | 回归脚本 | 需核不回归 |

对外入口已导出（`generation/__init__.py:7,14`），签名变更属**对外可见**改动 → 必须走"新增可选入参"而非改必填。

### 1.3 「本段目标」信号的真实可用性（关键事实）

| 候选信号 | 现状 | 可用性 |
|---|---|---|
| `reminders`（未填伏笔 + 角色约束） | `build_context:245` **已加载**；`get_reminders`（`kg/queries.py:102`）返回 `plotholes` + `characters`，有 `TOP_K` 封顶 | ✅ **零额外 IO**，且语义就是"接下来该收的线" |
| `project.outline` | `models.py:41` 是自由 JSON；`prompts.py:232-237` 渲染成**整篇 bullet 列表**，**不按段对齐** | ⚠️ 只能当"全局目标"，无法定位本段 |
| 段号 `segment_no` | `build_context` 当前**不接收** | ⚠️ 需要新增可选入参才有意义；但 outline 不对齐，收了也用不上 |
| `project.premise` | 静态前提 | ⚠️ 恒定，无区分度 |
| `prior[-1]` | 当前唯一来源 | ✅ 保留，但降级为"衔接侧" |

**结论**：feat 分支上唯一现成的"本段目标"信号是 `reminders`。（注：master 线上的 `generation/{timeline,rhythm}.py` 若将来并入，本设计可自然扩展为按节拍取目标——见 §6 边界。）

### 1.4 成本事实（决定"不会变贵"的论证基础）

- `hybrid_search` 每次调用**加载该 novel 的全部 `NovelChunk` 行**（`retrieval/search.py:46-48`）再在 Python 内算 BM25（`:52`）+ 逐 chunk 余弦（`:58-61`）→ **代价与 `k`、与查询词长度都无关**，只与语料规模有关。
- 因此**改查询词不增加任何检索成本**；这是本方案"成本中性"的核心依据。

### 1.5 测试钉点（回归风险面）

- `backend/test_m4_api.py:239`：断言「未选参考语料时 `plan2["rag_context"] == []`」→ **必须保持**。
- `backend/test_m4.py:112-114`：断言 `rag_context` 是 `list` → **必须保持**。
- `backend/test_m4_api.py:100,233`：打印 `len(plan["rag_context"])`。
- **未发现**任何测试钉住查询词字符串本身 → 改动半径小，但正因如此**必须新增**针对查询词的测试（否则洞会静默复发，与本次修的 `style_narrative` 漏传同型）。

---

## 2. 缺口定义（三选一，本设计只打第一条）

| # | 缺口 | 本设计是否覆盖 |
|---|---|---|
| ① | RAG 查询词与「本段要写什么」错位（写到一半想查查不了） | ✅ **本设计就打这条** |
| ② | 自检后重写不给原文对照 | ❌ 已由 Phase 0 覆盖 |
| ③ | 工具层与 Writer 未打通（无协议、无循环） | ❌ 属 Phase 1，明确不做 |

**为什么值得单独立项**：①是**既有代码的事实缺陷**（不是新需求）；修复不改成本、不改确定性；而②③都要动更底层。

---

## 3. 方案设计

### 3.1 查询词构造（新 `_rag_query_targeted`）

**输入**（全部来自 `build_context` 内已算好的对象，不新增 IO）：
- `reminders`（`:245`）：未填伏笔标题 + 角色名
- `prior`（`:246`）：L1 摘要链
- `project`：`title` / `outline`（兜底用）

**构造规则**（优先级从高到低拼接，逐段截断）：

1. **目标侧（新增）**：未填伏笔（至多 `N_HOLES` 条，每条截 `C_ITEM` 字符）+ 角色约束名（至多 `N_CHARS` 个）
2. **衔接侧（保留，但收窄）**：`prior[-1]` 的**尾部窗口** `C_TAIL` 字符（而非整条摘要）
3. **兜底（现状）**：若上面两者皆空 → **逐字节返回** `_rag_query(prior, project)` 的结果

**上界**：查询词总长 ≤ `C_QUERY`。

> **关于常数**：本设计**不发明新数字**。`N_HOLES`/`N_CHARS` 直接复用 `get_reminders` 既有的 `REMINDER_TOP_K`（`kg/queries.py:102`）封顶口径；`C_ITEM`/`C_TAIL`/`C_QUERY` 三个新常数**必须先实测再定值**（见 §7 T4），禁止拍脑袋——这是本项目既有纪律（`docs/验收指标.md` §6 提示词膨胀红线）。

### 3.2 开关与可观测（不做迁移）

- **开关**：新增模块级常量（如 `_RAG_QUERY_TARGETED = True`）作为唯一判据点。
  **不**复用 `project.narrative_tier == "full"`——Phase 0 那次借用已被 QA 标注「语义不等价」，不应再叠一层借用。
- **可观测**：在 `generate-plan` 预览端点（`routers/generation.py:206`）的返回里**新增 `rag_query` 字段**。理由：①零成本（查询词本来就在内存里）；②让 A/B 对比不必跑生成；③给测试一个稳定的断言锚点。
- **不引入项目字段、不做 SQLite 迁移**（若将来要按项目开关，再单独立项）。

### 3.3 回退路径（必须逐字节一致）

无 `reminders`、且无 `prior` 时 → 返回 `_rag_query(prior, project)` 的原结果。
这条是**防过度的硬保证**：任何"自由生成 / 无参考语料 / 首段无前情"的项目，行为与改动前**完全相同**（含字符级）。

---

## 4. 成本上界（逐条证明不变）

| 项 | 改动前 | 改动后 | 论证 |
|---|---|---|---|
| `hybrid_search` 调用次数 | 每 `novel_id` 1 次 | **每 `novel_id` 1 次** | 只改传给它的 `query` 字符串，循环结构（`:261-264`）不动 |
| 单次检索耗时 | 与语料规模成正比 | **同** | `search.py:46-61` 的加载与打分不依赖查询词长度 |
| 新增 LLM 调用 | — | **0** | 查询词由**已有内存对象**拼接而成，不调模型 |
| 新增 DB 查询 | — | **0** | `reminders` 已在 `:245` 加载；不额外 `select` |
| 新增网络 IO | — | **0** | 同上 |
| 提示词长度 | — | **不变** | 查询词**不进提示词**，只用于检索 |

**唯一新增开销**：字符串拼接（O(常数) 字符）。

---

## 5. 不做什么（明确边界）

1. **不做工具循环**、不加任何新 SSE 事件、不改 `LLMProvider` 接口 —— 全部属 Phase 1。
2. **不碰 `engine.py:913`** 的 `[]  # 重生成不带 RAG 素材` 排除 —— 那是作者有意为之，有注释为证。
3. **不改 `_build_generation_prompts` 的第 2 参口径**（`get_reminders(...)` vs `ctx["reminders"]`）—— 若核对发现确有差异，**单独立项**。
4. **不引入项目字段、不做迁移**（见 §3.2）。
5. **不动 `reminders` 的 `TOP_K` 封顶逻辑**（`docs/验收指标.md` §6 红线）。
6. **不追求"多次检索"** —— 检索次数保持 1 次/本；本设计只提高这一次的质量。

---

## 6. 与 Phase 1 的边界（将来怎么扩展）

若将来 master 线的 `generation/{timeline,rhythm}.py` 并入 feat，`outline` 升级为**按段/按节拍对齐**的结构，则本设计的 §3.1 规则 1 可直接扩展为"本段节拍目标 + 未填伏笔"，**不需要改架构**——因为查询词构造已被收敛到单一函数。

反之，Phase 1 若要做，本设计的 `rag_query` 观测字段正好是它的**基线对照**（工具循环是否真的比"更聪明的查询词"更好，需要这个对照才说得清）。

---

## 7. 验收标准与测试计划

| # | 测什么 | 怎么证 | 通过判据 |
|---|---|---|---|
| T1 | **定向生效** | 造项目：有 1 条未填伏笔（含特征词）+ 有 `prior` | 查询词**含**该伏笔特征词 |
| T2 | **回退逐字节一致** | 无 `reminders`、无 `prior` | 查询词与 `_rag_query(...)` 返回值**字符级相同** |
| T3 | **首段** | `prior` 为空但 `reminders` 非空 | 查询词非空且含伏笔词（不退化成"书名+大纲首项"） |
| T4 | **长度上界（先实测后定值）** | 用真实 9 段项目 + 真实语料，打印实际查询词长度分布 | 实测值 ⇒ 定 `C_QUERY`；断言 `≤ C_QUERY`。**先出实测数字再写常数** |
| T5 | **调用次数不变** | spy `hybrid_search`，2 本语料 | 恰好 2 次，且每次 `query` 相同 |
| T6 | **兼容不回归** | 复跑 `backend/test_m4_api.py:239`、`backend/test_m4.py:112-114` | 未选语料仍 `rag_context == []`；仍是 `list` |
| T7 | **可观测** | 调 `generate-plan` 端点 | 返回含 `rag_query` 字段，且与生成路径实际使用的一致 |
| T8 | **全量回归** | 仓库根、无路径参数、`--basetemp` 在仓库外、**不传 `-q`** | ≥ 基线 **476 passed / 2 skipped / 0 failed**，无新增 skip |

**独立 QA 必须攻击的三点**（不接受只重跑实现者的测试）：
1. 在**未修版本**上独立复现"查询词 = 上一段摘要"（不要复用实现者的探针）。
2. 证明 T2 的回退是**真的逐字节**（对含生僻字符/换行的摘要也要一致）。
3. 证明 T5 的调用次数**没有**因为新逻辑而增加（构造最坏输入：reminders 极多 + 语料 2 本）。

---

## 8. 顺带发现（本次不改，建议单独立项）

### 8.1 重生成路径白付一次全量检索

`engine.py:960`：

```python
constraints = build_context(db, project, novel_ids=novel_ids)["constraints"]
```

只为取一个键，却跑了 `build_context` 的全部工作：RAG 检索（每本语料 1 次**全量 chunks 加载**）+ 风格样例采样（`sample_style_exemplars`）+ 前情块组装——**结果全部丢弃**。而 `_build_generation_prompts` 自己已经明确"重生成不带 RAG 素材"（`:913`）。

→ 这是**纯浪费**（每次重生成多付一次全量检索），且与作者意图相悖。修法是把 `constraints` 的组装抽成独立小函数（内部只是 `merged_constraint_items(reminders, project.plot_bible)`，`engine.py:291`），或让 `build_context` 支持 `skip_rag=True`。**建议单独提一个小 commit**，不要混进 Phase 0.5。

### 8.2 `outline` 不接受按段对齐

`prompts.py:232-237` 把 `outline` 一律渲染成整篇 bullet 列表。若用户把大纲写成"第 N 段：…"的形式，目前没有解析。属产品面问题，记录待议。

---

## 9. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 新查询词反而比旧词差（检索质量不可控） | 常量开关一键回退；`generate-plan` 的 `rag_query` 字段支持人工 A/B 对照 |
| 常数定值拍脑袋 | T4 强制先实测再定值 |
| 隐式改变既有测试的隐含假设 | §1.5 的钉点清单逐条核；T6/T8 兜底 |
| 与外层"段间串行"叠加放大延迟 | 成本上界已证为 0 增量（§4） |

**回滚**：置常量 `False` 即恢复改动前行为（T2 保证逐字节一致），无需改数据结构、无需迁移。

---

## 10. 待裁决

- (i) 按本设计实现（走"先红后绿 + 独立 QA"），**并同时修 §8.1** 那个白付检索；
- (ii) 只按本设计实现，§8.1 单独立项；
- (iii) 先只落 §3.2 的 `rag_query` 观测字段，跑真实 A/B 数据后再决定是否改查询词；
- (iv) 搁置。
