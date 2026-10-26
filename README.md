# AI 小说生成器 · AI Novel Generator

> **把「写一部长篇网文」拆成一套可工程化、可验证的流水线,并交给助理 agent 编排。**
> A full-stack AI novel-generation engine: hybrid RAG retrieval, a knowledge-graph drift
> guard, **bounded hierarchical context**, a **globally-shared style-distillation library**,
> and an **assistant agent that may only propose, never write** — with a verification culture
> built on reproducible evidence, not vibes.

<p align="center">
  <b>FastAPI</b> · <b>Vue 3 + Vite + TypeScript</b> · <b>SQLAlchemy / PostgreSQL+pgvector / SQLite</b> ·
  <b>RRF Hybrid Retrieval</b> · <b>Knowledge Graph</b> · <b>Agent Workflow</b>
</p>

---

## 这个系统 / What this is

长篇网文写作的难点不在「调一次 LLM」,而在**几百段之后还能不能守住同一本书的写法、不忘伏笔、不自相矛盾、前情还不把 token 撑爆**。这个系统把这些问题拆成可机检的部件:

- **文体是可蒸馏、可复用的资产** —— 一本书的写法蒸馏成一份档案,任何项目都能用;
- **前情是有界的** —— 层级摘要树保证「段 1..N 全被覆盖,没有洞」;
- **漂移是可拦截的** —— 知识图谱在生成前把角色/伏笔/进度提醒给模型;
- **助手里手不能乱写** —— agent 只提卡,任何落库都要人批。

---

## ✨ 能力 / Capabilities

### 📖 文体与叙事档案:一本书的写法,蒸馏一次,全局复用

- **16 维叙事档案** —— 人物描写手法 / 对白风格 / 段落编排 / 场景构建 / 情绪表达 / 节奏控制 /
  世界观呈现 / 剧情编排模式 / 角色声音词典 / 伏笔与悬念设计 / 文笔量化审计 / 节拍序列 /
  高频场景模板 / 潜台词模式 / 战斗描写 / 语序编排。
- **21 个可机检文体特征** —— 句长均值 / 句长 CV / 句长 p90 / 短句占比 / 长句占比 / 段长 CV /
  段均句数 / 对白字占比 / 对白段占比 / 纯台词段占比 / 逗号密度 / 句号密度 / 感叹号密度 /
  问号密度 / 省略号密度 / 比喻密度 / 情绪直陈密度 / 段尾升华率 / 四字格密度 / 短串占比 /
  虚词密度。档案里每一个数都能复算。
- **三档蒸馏**(完整 / 平衡 / 快速),落在**全局共享、幂等**的库里 —— 不按项目重复蒸馏。
- **注入取法是设计过的**:只在**成熟期**章节里挑、均匀取**不同的章**、每章取最早追加的那条。
  早期章节文风没稳定,拿开篇代表全书是错的。

### 🧠 有界前情:层级摘要树 + 覆盖不变量

- 三层结构:**段 → 卷 → 主线**,`compose_prior` 按预算裁剪。
- 它**永远报告 `uncovered`**(应恒为 `[]`):前情块必须连续覆盖段 `1..N` 且无洞。
- **覆盖声明写在文本里**,不只存在报告里 —— 外部核对器只读 prompt 文本就能算出有没有洞,
  不必相信模块自报的结果。

### 🔎 检索与知识图谱防漂移

- **混合检索**:纯 Python BM25 × 向量 embedding,**RRF** 融合。
- **知识图谱 v3**:实体 / 事件 / 关系 / 关系演化 / 伏笔 / 角色状态 / 知识条目 / 时间线因果链。
- **生成前提醒**(reminders)把「这个角色是谁」「这条伏笔还没收」「剧情走到哪」喂给模型,
  再叠加**用户自己写的 plot-bible**(故事权威),两边在**同一处合并**后使用两次:
  静态 system 层 + 靠近末尾的 user 提醒。

### 🎭 角色对话:让书里的人按自己的记忆开口

- **每段证据带来源标注** ——「他亲历」还是「他在场听到」由服务端判,认不出他开口就判「在场听到」
  (宁可漏判不要误判:误判会让角色知道不该知道的秘密)。
- **只知道他知道到此刻为止的事** —— 与续写共用同一条 fail-closed 边界,章节边界之外的内容不进入提示词。
- **没有依据就直说不知道** —— 检索不到支撑时不允许编,回答里带不上的东西就明说。
- **角色档案来自蒸馏结果** —— 生平时间线 + 说话方式 + 他依据的原文,三块拼成这个角色的 system 层。

### 🧪 提示词细化:把「像一点」变成可核对的数字

- **基线给区间而不是单点** —— 与 `band_check()` 同源(p10~p90);比例型/稀疏型指标**只给均值**
  (长尾分布上给区间等于没给)。
- **要求带例句** —— 例句过**两道闸**:脱敏(专名表拦截)+ PMI 候选兜底(表外漏网),
  候选集按书缓存进专名表;实测 28 条例句**零泄漏**。
- **提示词标签化** —— 每一块注入都带标签,模型知道哪块是约束、哪块是材料。

### 📇 角色档案:让「这个角色是谁」真的进到提示词里

- **证据回填** —— 字段级事实核验:从「有他出场的行」里定位 n-gram 命中,给老档案补上合规证据
  (实测 32/32 命中),不用放宽门禁。
- **生平时间线** —— 完整句 / 引号配对 / 名字不在引号里,还原「他经历过什么」。
- **限长 400 字/角色**,原文摘录插在「说话」块之后(放最后会被限长截掉)。

### 🛡 三条写作路径与可见性边界

| 路径 | 参考书怎么用 | 边界 |
|---|---|---|
| **只学写法**(原创) | 整本可作**写法材料** | 用户显式设的原文边界永远优先 |
| **接着这本书写**(续写) | 只有边界以内的章节 | **fail closed**:没设边界 ⇒ 整本挡掉(防剧透) |
| **角色聊天** | 角色声音词典 | 不读参考书原文 |

界面上这是**一个二选一**,不是一个可选输入框 —— 两套可见性规则,猜错了就是剧透或白白挡住素材。
「读到第 N 章」的边界直接写在参考语料下面。

### ✍️ 写作工序链

`生成 → 段长闸门 → 场景契约审计 → 去重 → AI 味重写 → 文风改写 → 形态校正 → 摘要/KG → 落库`

- **段长闸门**:长度不再是提示里的一句「软希望」。量的是**汉字**,不够就**回灌让模型扩写**
  (有界),仍不到下限就照写但**如实标注未达标**。
- **形态校正**:把「一行一句台词」掰成「叙述 + 台词同段」。**机械版**默认开 ——
  零模型调用、100% 可复现、**一个字都不新增**;**模型改写版**默认关(实测不可靠)。
- **候选池**(Best-of-N / 阈值式拒绝采样,默认关):同一提示采多份,按**参考书自己的分布**择优。
- **局部补写**(beat,默认关):在连续对白段开头并进一句动作叙述,逐段验收。

### 🤖 助理 agent:只提卡,不落库

这是本项目的**硬边界**:agent 的工具箱里,所有会改数据的能力都是**提案**,不是写入。

- **25 个工具**,其中写操作只有三种:`propose_action` / `propose_text_edit` / `propose_text_fix`。
- **提案 → 批准 → 落库 / 回滚**;每次落库留**版本快照**,可 diff、可恢复。
- **11 步工作流**:语料就位 → 清洗抽检 → 文风蒸馏 → 档案审查 → 提示词体检 → 写正文 →
  正文审查 → 修复待批 → 派生重建 → 解锁放行 → 收敛。
- **确定性诊断规则**(零模型调用):句长偏离、对白形态偏离、标点事故、外来拉丁字母、
  角色名冲突、伏笔堆积、死角色复现…… 每条 issue 带**可复核的证据**(原文 + 定位)。
  agent 看到 issue 后用 `from_issue` 起草改写,**证据由服务端带进提案**,模型不抄。
- **需要模型判的那类**(如正文内的事实矛盾)走**同一个 issue 契约** —— 证据照样由服务端给,
  agent 的改写草稿照样要过提案闸门。

### 🔌 多渠道模型与角色隔离

- `ProviderRegistry` **两轨**:Writer 与 Assistant 各自独立的模型配置,并发隔离。
- **timeout 真正生效**(此前六个渠道分支都只传 `max_tokens`,`timeout` 一直用基类默认的 120 秒)。
- 写手模型、辅助模型、嵌入模型各自可配;空 key ⇒ 本地确定性兜底。

---

## 🧭 架构 / Architecture

```mermaid
flowchart TD
    Corpus[语料 Corpus · txt] -->|UTF-8 解析 + 自动分章| Chunks[叙事切分 Chunks]
    Chunks -->|BM25 + 向量 embedding| Store[(PG + pgvector · SQLite)]
    Chunks -->|规则抽取 角色/坑/关系| KG[(知识图谱 KG)]
    Corpus -->|文风蒸馏 三档| Lib[(全局蒸馏库 StyleDistill)]
    KG -->|生成前提醒 reminders| Gen[分段生成环 Generation Loop]
    Store -->|RRF 融合召回 + 原文边界| Gen
    Lib -->|档案 + 规则 + 写法样例| Gen
    Gen -->|段级总结| Tree[层级摘要树 Summary Tree]
    Tree -->|有界前情 compose_prior + 覆盖证明| Gen
    Gen -->|Writer 续写| LLM1[Writer Model]
    Gen -->|诊断 / 提卡| AG[助理 Agent · 25 工具]
    AG -->|提案 Proposal| Gate[提案闸门 人批]
    Gate -->|批准| Gen
    Gate -->|版本快照| Ver[(Versions · diff · restore)]
```

**生成数据流**:`KG reminders` + 用户 **plot-bible** → 合并约束(唯一权威口径,用两次:
静态 system 层 + 近因 user 提醒)→ `summary_tree.compose_prior` 组装**有界前情**
(主线 / 近情 / 卷)→ RAG 上下文(含原文边界裁剪)→ LLM 写 → 段级摘要 → 写进 `generations`
并卷进摘要树(L1 → 按需 L2/L3)→ 下一段读树。

---

## 🛠 技术栈 / Tech stack

| Layer | Choice |
|---|---|
| Backend | **FastAPI** (Python 3.13), SQLAlchemy 2, Pydantic v2 (Strict) |
| Storage | PostgreSQL + **pgvector** (prod) · SQLite (dev/test) |
| Retrieval | Pure-Python **BM25 × dense-vector RRF fusion** |
| Knowledge graph | Deterministic rule extractor(LLM slot reserved on the same interface) |
| Generation | Bounded-prior engine + segment summary chain + 工序链 |
| Style | **Globally-shared distillation library**, 3 tiers, idempotent upsert |
| Frontend | **Vue 3 + Vite + TypeScript**, semantic design tokens (WCAG AA) |
| Agents | Writer/Assistant role isolation, `ProviderRegistry` two-track, proposal→approval→commit/rollback |
| LLM providers | DashScope · OpenRouter · MiniMax · OpenAI-compatible · Local deterministic fallback |

---

## 🚀 快速开始 / Quick start

```bash
# 0) 数据库：默认 SQLite —— 零配置起步，复制 .env 就能跑
#    要 PostgreSQL + pgvector（生产 / 大数据量）：
#        docker compose up -d
#        pip install -r requirements-postgres.txt
#        DATABASE_URL=postgresql+psycopg2://novel:novel@localhost:5432/novel

# 1) backend
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
uvicorn app.main:app --reload --port 8000

# 2) embedding / llm keys  (在 backend/.env 里；留空 => 本地确定性兜底)
#    EMBEDDING_PROVIDER=dashscope  EMBEDDING_MODEL=text-embedding-v3
#    LLM_PROVIDER=dashscope        LLM_MODEL=qwen-plus

# 3) frontend
cd frontend
npm install
npm run dev            # http://localhost:5173 , /api proxies to :8000
```

> **依赖只装得上一半的问题已经堵掉**:`requirements.txt` 里全是纯 wheel(不需要 C 编译器、
> 不需要 PostgreSQL 开发环境),PG 驱动单独放在 `requirements-postgres.txt` ——
> 以前 `psycopg2-binary==2.9.9` 没有 Python 3.13 的预编译轮子,pip 会退回源码编译然后
> 要本机装 `pg_config`,空白机器上必然卡死。

**跑测试**(可选,面试官最常做的第一件事):

```bash
cd backend
pip install -r requirements-dev.txt     # 额外的只有 pytest
cd ..
python -m pytest tests/ -q              # 1,300 项用例
```

主要端点(完整清单在 `docs/`):

```bash
curl -X POST http://localhost:8000/api/novels/upload -F "file=@小说.txt"   # 导入语料
curl "http://localhost:8000/api/novels/{id}/search?q=林夜&k=5"              # 混合检索
curl -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"我的小说","mode":"quick","target_length":5000}'             # 建项目
curl -X POST "http://localhost:8000/api/projects/{pid}/build-kg?novel_id={id}"   # 知识图谱
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1],"max_chars":2400}'                    # 写一段
curl -X POST http://localhost:8000/api/generate-plan                        # 规划与预检
curl -X POST http://localhost:8000/api/assistant/diagnose                   # 确定性诊断
curl -X POST http://localhost:8000/api/assistant/proposals                  # 提案闸门
curl -X POST "http://localhost:8000/api/projects/{pid}/distill" \
  -H 'Content-Type: application/json' -d '{"tier":"balanced"}'              # 文风蒸馏
```

---

## 🧪 测试档案 / Sample output

仓库**不带语料**（版权正文不进 git），但两端各附一段：左边是系统的**输入**
（自写示例语料，用作风格参考），右边是同一套流水线生成的**输出**。

**输入 · 自写示例语料**（节选，原创 demo，非真实出版物）：

> 沈舟记得很清楚，他是在第十七次被人踩进泥里那天，才真正看见那把剑的。
>
> 不是什么名剑。锈得厉害，剑脊上爬满暗红的纹路，像一道道没愈合的疤。它就插在宗门后山的乱石堆里，剑柄朝外，仿佛在等一个够穷、也够倔的人来拔。
>
> 所有人都说那是废铁。连教他打杂的李管事都笑：「就你，也配碰剑？」

**输出 · 系统生成的一节正文**（悬疑向，节选）：

> 雨从傍晚下起，到九点多，旧桥值班室的玻璃窗已蒙得看不清外头，桌边的搪瓷杯隔着闷响一下一下往外挪，沈砚摊开签收簿，从最后一页往前核，笔尖顺势在纸上拖出一道短横。
>
> 他到旧桥值了三个月，桥不长，临河那侧封了半边，另一边留给早班公交和进城的货车，一落雨，路灯便映进积水，光影碎在水面上。
>
> 值班室统共一张桌、两把椅子，墙角堆着旧护栏，门后挂着塑料雨衣，鞋柜里常年一股潮橡胶味。

**想跑自己的**（三步）：

1. 找一本你有权使用的 `.txt`（UTF-8；带 `第X章` 分章最好），放进 `data/seed/`，
   或用 `NOVEL_DIR` 指向别处；
2. `cd backend && python load_corpus.py` 入库（自动切分 + 抽知识图谱）；
3. 界面上建项目 → 选参考书 → 走文风蒸馏 → 生成。

详见 `data/seed/README-投放真实语料.md`。

---

## 📦 仓库里有什么 / What's in the box

| 目录 | 内容 |
|---|---|
| `backend/app/` | 后端全部实现：混合检索 / 知识图谱 / 生成环 / 文体蒸馏 / 助理 agent |
| `frontend/` | Vue 3 + Vite 前端：创作三板块、助理工作台、模型设置 |
| `tests/` | **1,300** 项 pytest 用例（单源真源，`pytest tests/`） |
| `docs/` | 48 份设计文档与指南：架构怎么定的、判据为什么这么定 |
| `ENGINEERING.md` | 工程纪律：每一条规矩背后都是一次真实踩坑 |
| `data/seed/` | 语料**投放区**（空目录 + 投放说明） |

**刻意不放进仓库的东西**（有意为之，不是遗漏）：

- **参考书原文与语料** —— 版权正文不进 git，只在本机投放；
- **模型跑出来的验收产物** —— 含原文片段的中间 JSON 不随仓库分发；
- **内部的计划书 / 任务书 / 交接单** —— 过程管理材料留在本地工作区；
- **本机绝对路径** —— 全部改成相对路径或环境变量，换台机器 clone 就能跑。

---


这个项目把**证据**当交付物:

- **可复算的量** —— 档案里的每个数都能用同一套尺子重算;口径变更会作废旧档案并重新拟合,
  而不是让两套数并存。
- **覆盖不变量自带证明** —— 覆盖声明写进前情文本本身,外部核对器只读 prompt 就能算出有没有洞,
  不必相信模块自报的 `uncovered`。
- **边界有测试钉着** —— `tests/test_assistant_readonly.py`(助理只读)、
  `tests/test_reference_boundary.py`(原文边界)、`tests/test_action_proposals.py`(提案闸门)、
  `tests/test_workflow_steps.py`(工作流)。
- **对抗性验证** —— `tests/qa4_verify_deep_attacks.py`(深度攻击)、
  `tests/qa4_verify_proposal_write_path.py`(提案写路径)、
  `tests/qa6_assistant_browser_smoke.py`(真实浏览器端到端)。
- **构建闸门** —— 前端要 `vue-tsc --noEmit` + `vite build` 都过;后端有真实 `uvicorn` HTTP 冒烟。
- **空白机自检** —— 仓库在一台**干净机器**上跑过完整一遍:新建 venv → 装依赖 → 照 README 复制
  `.env` → 空 SQLite 起服务 → 打真实 HTTP → 前端 `npm install && npm run build`。
  19 项全绿(零 key、零配置、零手工干预)。
- **规模** —— 后端 **1,300** 项测试(`pytest tests`),前端 7 个纯逻辑测试文件。
  当前基线有 **8 项既有失败**(`settings_roles` ×1、`style_tier` ×7),与最近改动无关。
- **工程纪律** —— 见 `ENGINEERING.md`:每条规矩都对应一次真实踩坑,不是口号。

---

## 🗺 状态 / Status

已交付:语料导入与清洗 · 混合检索 · 知识图谱 v3 · 分段生成环 · 层级摘要树 · 文风蒸馏三档与全局库 ·
叙事档案与写法样例 · 形态校正 · 助理 agent 与提案闸门 · 工作流 · Vue 3 前端 ·
角色对话(来源标注 / 生平时间线 / 不知道就直说) · 提示词细化(基线区间 / 例句两道闸) · 角色档案证据回填。

明确的边界(不声称已完成):

- `prompt_override` 提案**未接线**(B2):三个只增不减的追加槽改不了维度级行为。
- 基于 LLM 的知识图谱抽取:**接口已留**,当前走确定性规则抽取。
- 前端组件级 `vitest`:目前只有纯逻辑测试文件,组件级套件未铺开。
- **候选池择优的是段长闸门扩写前的稿子** —— 择优形态 ≠ 落库形态,这一处口径差还没接上。

<sub>本地开发数据库与内部验证脚本被 git 忽略,不随本仓库分发;
参考书原文与模型跑出来的验收产物同理 —— 版权正文不进 git。</sub>

