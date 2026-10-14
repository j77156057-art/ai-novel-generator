# AI 小说生成器

M1–M6 按 wayfinder 规划推进。当前进度：**M1 数据层 ✅ → M2 混合检索 ✅ → M3 知识图谱 ✅（规则基线）→ M4 分段生成环 ✅ → M5 前端 ✅ → M6 验收 ✅**。

## 里程碑
- **M1 数据层**（✅）：语料上传/种子 → UTF-8 解析分章 → 叙事切分 → PG/SQLite 入库。
- **M2 混合检索**（✅）：入库填 `novel_chunks.embedding` + BM25（关键词）× 向量（语义）→ RRF 融合召回。
- **M3 知识图谱**（✅ 规则基线）：角色/坑/关系抽取 + 漂移防护（生成前提醒）。LLM 抽取接口已留，待后续接入。
- **M4 分段生成环**（✅）：LLM 续写 + 段级总结链 + RAG 上下文 + 漂移防护（M3.3 reminders 注入）+ quick/expert 模式 + 生成前 plan 预览。LLM provider 可选 **dashscope（默认）/ openrouter（免费 1M 上下文模型）/ local（离线兜底）**。
- **M5 前端**（✅）：Vue3 + Vite + TS 工作台（语料库 / 创作项目 / 项目工作台三视图），全端点接线；视觉走语义 token，锁定 **editorial** 主题（暖纸/墨/朱红，WCAG AA）。
- **M6 验收**（✅）：前端 `vue-tsc` + `vite build` 双闸通过；后端 uvicorn 真实 HTTP 冒烟全链路通过；设计方向经三版方向稿（editorial / brutalist / soft）拍板 editorial。

## 目录
```
backend/
  app/
    config.py          配置(DATABASE_URL/切分/embedding)
    db.py              SQLAlchemy 引擎 + 建表 + pgvector 扩展
    models.py          projects/raw_novels/novel_chunks/kg_nodes/kg_edges/generations
    schemas.py         Pydantic 响应模型
    tokenize.py        极简中文分词(MVP 占位，正式换 jieba)
    embeddings/
      provider.py      EmbeddingProvider + 通义千问 v3(DashScope) + 本地离线兜底
    llm/
      provider.py      LLMProvider + 通义千问对话(DashScope /chat/completions) + 本地离线兜底(确定性回显)
    generation/
      prompts.py       系统提示(角色/未填坑/风格) + 用户提示(分层前情/RAG/大纲/指令)
      summary_tree.py  层级摘要树：L1 段 / L2 卷(8段) / L3 主线骨架 + 有界前情拼装
      engine.py        上下文组装 → 续写 → 段级总结 → 落库 → 链式续写
    ingestion/
      parse_txt.py     UTF-8 解析 + 自动分章(第X章/Chapter N；丢弃首章前版权页)
      chunk.py         字符窗口 + 重叠 叙事切分
      store.py         解析→切分→填 embedding→写库
      seed.py          种子语料加载(幂等)
    retrieval/
      bm25.py          纯 Python BM25
      search.py        BM25 × 向量 → RRF 融合
    kg/
      extractor.py     规则抽取器(RuleBasedExtractor) + 坑结算；LLM 抽取接口预留
      build.py         从分片抽图 → 写 kg_nodes/kg_edges（按 project_id 幂等重建）
      queries.py       角色出场/坑状态/生成前提醒(M3.3 约束供给)
    routers/novels.py  上传/种子/列表/分片查看/检索 API
    routers/projects.py 项目 + 知识图谱 API（建项目/建图谱/查角色/查坑/提醒）
    routers/generation.py  M4 生成环 API（预览/续写单段/多段/查看）
    main.py            FastAPI 入口(lifespan)
data/seed/             占位种子语料(可替换为你的 .txt)
docker-compose.yml     PG + pgvector
```

后端目录下另有可离线跑的回归与验证脚本（`cd backend` 后直接执行）：

| 脚本 | 作用 |
|---|---|
| `test_p0_regression.py` | 评审 B 的 4 个 P0 生产路径缺陷回归（55 断言） |
| `test_ledger.py` | 事实账本 + 别名归一（40 断言） |
| `test_summary_tree.py` | 层级摘要树 + 有界前情 + **独立算子覆盖审计**（73 断言） |
| `test_m3*.py` / `test_m4*.py` | M3 图谱 / M4 生成环（含 API 层端到端，需 httpx） |
| `verify_summary_chain.py` | 前情分层的 before/after 证据 + 卷层门槛 N=8..40 扫描 |
| `verify_determinism.py` | 抽取链确定性（`sha256` 逐位比对，改动未触及其它模块的证据） |
| `verify_sentinel.py` | 规则哨兵：抽事件→判硬冲突（97 断言 + 116 万字真实语料零误报） |
| `verify_acceptance.py` | **验收指标**：人类散文基线 → 30/80 段闭环 → 四轴判定（`--segments N`） |
| `_seed_prior_demo.py` | 造前情预检面板的演示项目（19 段，走 production 代码路径） |
| `_verify_prior_panel.js` | 前情预检面板真浏览器端到端（27 断言；需先起前后端） |
| `_seed_ui_e2e.py` | 造「硬冲突面板」浏览器断言用的种子库（4 段，含 dead_speaks + realm_regression） |
| `_verify_workspace_alerts.mjs` | 工作台硬冲突面板真浏览器断言（18 断言；需先起种子后端 + 前端，见文件头注释） |

> 指标定义、阈值来源、人工盲评流程与失败处理矩阵见 **`docs/验收指标.md`**。

```
frontend/              M5 前端（Vue3 + Vite + TS）
  src/
    main.ts / App.vue  入口 + 应用外壳（侧栏导航）
    theme.ts           主题选择器（改一个常量全站换肤）
    style/
      tokens.css       语义设计 token（editorial 默认，另备 brutalist/soft）
      base.css         基础重置 + 共享组件类（全走 token）
    api/
      types.ts         镜像后端 schemas 的 TS 类型
      client.ts        全端点 fetch 封装 + 错误解析
    router/index.ts    路由（语料/项目/项目工作台）
    views/             CorpusView / ProjectsView / ProjectWorkspace（三 tab：图谱/提醒/生成）
    composables/useAsync.ts
  concepts/            三版设计方向稿（editorial / brutalist / soft，自包含 HTML）
```

## 跑起来

### 1. 数据库（二选一）
- 本地快速开发：用 SQLite，无需起服务
  ```
  DATABASE_URL=sqlite:///./novel_dev.db
  ```
- 接 PG + pgvector：
  ```
  docker compose up -d
  DATABASE_URL=postgresql+psycopg2://novel:novel@localhost:5432/novel
  ```

### 2. 安装依赖 & 启动
```
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### 3. Embedding 配置
`.env.example` 已给模板（复制为 `.env`）：
```
EMBEDDING_PROVIDER=dashscope        # dashscope | local
EMBEDDING_DIM=1536                  # 通义 text-embedding-v3，匹配 Vector(1536) 列
EMBEDDING_MODEL=text-embedding-v3
DASHSCOPE_API_KEY=                  # 留空 → 自动降级本地离线兜底(仅开发/测试)
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# M4 分段生成环：复用同一个 DashScope key；llm_base_url 留空则复用上面的 BASE_URL
LLM_PROVIDER=dashscope               # dashscope | local（local=确定性模板，离线可测、回显约束）
LLM_MODEL=qwen-plus                  # 通义千问对话模型
```
> 国内供应商选**通义千问 text-embedding-v3 @1536** 是为了不动现有 `Vector(1536)` 列。
> 未填 key 时走 `LocalFallbackProvider`（哈希词袋向量），保证离线可跑、可测；**非生产质量**。
> M4 同理：`LLM_PROVIDER=local` 或不填 `DASHSCOPE_API_KEY` 时，续写走 `LocalFallbackLLMProvider`（确定性模板，会把注入的角色/未填坑回显进正文，便于离线验证约束确实进入生成环）。

### 3b. OpenRouter 免费模型（可选 provider）
除通义千问外，还可切到 OpenRouter 的免费模型：
```
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=apodex/apodex-1.1-mini:free
```
> 走 OpenAI 兼容 `/chat/completions`，与 DashScope 共用同一基类；默认**关闭 reasoning**（续写要吞吐）。
> **实测（2026-10）OpenRouter 全部 15 款 `:free` 模型跑中文续写，5 款可用**：

| 模型 | 速度 | 中文质量 |
|---|---|---|
| `apodex/apodex-1.1-mini:free`（默认） | ~2.5s | 好，干净无杂质 |
| `nvidia/nemotron-3-super-120b-a12b:free` | ~5s | 好 |
| `nvidia/nemotron-3.5-lightning:free` | ~6s | 快但夹英文、结尾失控 |
| `poolside/laguna-s-2.1:free` | ~7s | **中英混杂，不建议中文** |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | ~22s | **最好**（多段+结尾收得住），1M 上下文，长文用 |

> 实测失败：`inkling-small/inkling`（403 无权限）、`gemma-4-31b/26b`（429 限流）、`lfm-2.5-2.6b`（400）、north-mini-code（代码专用）、content-safety（分类器）。
> 免费档限流普遍，遇 429/403 属常态，换模型或重试即可。
> 想自己复现这份榜单：`python list_free_models.py`（列出全部免费）+ `python bench_free_models.py`（批量实测速度与质量）。

### 4. 灌种子 + 上传 + 检索
```
curl -X POST http://localhost:8000/api/novels/seed
curl -F "file=@你的小说.txt" http://localhost:8000/api/novels/upload
curl http://localhost:8000/api/novels

# M2：混合检索（q=查询，k=返回条数）
curl "http://localhost:8000/api/novels/{novel_id}/search?q=林夜&k=5"
```

### 5. M3：知识图谱（先建项目，再建图谱）
```
# 建项目
curl -X POST http://localhost:8000/api/projects \
  -H 'Content-Type: application/json' \
  -d '{"title":"我的小说","mode":"quick","target_length":5000}'

# 建图谱：一本语料一张图，source=novel:{novel_id}；不传 novel_id 则取库中第一本
# （同 project_id 可幂等重建；加 ?all_corpora=true 才合并全部语料）
curl -X POST "http://localhost:8000/api/projects/{project_id}/build-kg?novel_id={novel_id}"

# 查图谱（type=character|plothole|all；source 不传＝全量供浏览）
curl "http://localhost:8000/api/projects/{project_id}/kg"
curl "http://localhost:8000/api/projects/{project_id}/kg/character/林夜"
curl "http://localhost:8000/api/projects/{project_id}/kg?source=novel:1"

# M3.3：生成前提醒（未填坑 + 角色人设约束，供 M4 注入提示词防漂移）
# source 默认 "generated"＝本项目自己写的图谱；要查参考语料侧得显式指定。
# 两侧刻意默认不同：提醒会约束「接下来写什么」，参考语料的角色/悬念与之无关
#（前端原文「各自成图，绝不混在一起」，见评审 B P0-1）。
curl "http://localhost:8000/api/projects/{project_id}/reminders"
curl "http://localhost:8000/api/projects/{project_id}/reminders?source=novel:1"
```

### 6. M4：分段生成环
```
# 生成前预览（只组装上下文与提示词，不调 LLM）—— 人工漂移预检
# novel_ids 为参考语料列表；不传则回退 project.reference_novel_ids
curl -X POST http://localhost:8000/api/generate-plan \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1]}'

# 续写单段（novel_ids 可选；不传则用项目已选的参考语料，未选则跳过 RAG）
curl -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1],"max_chars":800}'

# 连续续写多段（段级总结链自动注入前情）
curl -X POST http://localhost:8000/api/generate-loop \
  -H 'Content-Type: application/json' \
  -d '{"project_id":1,"novel_ids":[1],"segments":3}'

# 查看已生成段落（按 segment_no 升序）
curl "http://localhost:8000/api/generations/{project_id}"
```

**生成环数据流**：`kg/queries.get_reminders(source="generated")` 取未填坑 + 角色约束 → `prompts.merged_constraint_items(reminders, plot_bible)` 与**用户构思的剧情圣经**按「bible 在前、图谱补充」合并（唯一口径，两处共用）→ 该结果一份进 `build_system_prompt` 的【角色人设约束】/【未填伏笔】块（静态层，保缓存），另一份作为 `constraints` 由 `llm/provider._apply_constraints` 追加到 **user 消息末尾**做近因提醒 → `generation/engine.build_context` 同时收集**分层前情块** + RAG 上下文(M2) + 大纲 → `generate_next_segment` 调 LLM 续写 → `provider.summarize` 出段级总结 → 写 `generations` 表 + 记进 `projects.summary_tree` 的 L1 → **封卷时**惰性滚出 L2 卷摘要 / L3 主线骨架 → 下一段自动读树拼前情。

> 为什么同一份约束要下发两次：静态 system 层负责「整轮不漂」，user 末尾负责「本次别忘」。
> 两处必须共用同一合并口径，否则尾部那份自称完整的锁定名单会漏掉最高权威层（bible）。

**前情注入改成有界分层（步骤 2）**：段级摘要链原本**全量拼接、无上界**，实测 N=300 时前情块
19,691 字 ≫ 单次生成 3,000 字（占 656%）。现改为 `summary_tree.compose_prior` 拼装：

| 层 | 内容 | 优先级 | 配色（预检面板） |
|---|---|---|---|
| L3 主线骨架 | 全篇因果链（≤320 字） | 最高，永不丢 | 朱红 |
| 近情 L1 | **未封卷**内的逐段摘要全文 | 覆盖下限，不因预算丢 | 蓝 |
| 卷情 L2 | 已封卷（8 段/卷）压成一条（≤200 字） | 预算不足时丢**最老**的卷 | 灰 |

**关键不变式**：`已封卷区间 ∪ 近情窗口` 必须**连续覆盖 `1..N`，不允许有洞**；允许两者在
seam 上有一段**有界重叠**（重叠区 = 跨界那一卷的尾部 `[ws, V×8]`，长度 ≤ `recent_min-1` 段，
且必须**连续、紧贴窗口前沿**）。`compose_prior` 每次都回报 `uncovered`（恒空）作为可断言证明，
并有独立算子（`test_summary_tree.py` 的 `audit_coverage`）只读渲染文本、不信自报值地重算一遍。
L2 被预算丢掉不算空洞（那些段仍由 L3 覆盖）；反之若某卷没被 L3 覆盖则**覆盖优先于预算**。
老项目（只有扁平 `summary`）会自动引导成树；粗层缺失时宁可**退化为全量**也不留洞
（`prior_report.degraded` 如实标记）。

> **为什么允许重叠**：早期版本要求窗口必须落在卷边界上，结果窗口长度被卷粒度绑架——
> 标称 `recent_min=3` 实测 N=18 时窗口 **10 段**，且随 `N mod 8` 在 3↔10 间振荡；
> N=8/9/10 时分层甚至比全量逐段**更贵**（贵 53%/48%/44%）。改为允许跨界卷摘要与窗口重叠后，
> 卷层门槛从 11 段回到结构性的 **8 段**，`N=8..40` 每个段数都省预算（+6%…+62%）。
> 代价只是几行文本被「卷情概述 + 最近细节」两个分辨率各讲一次，不矛盾。

实测（`python verify_summary_chain.py`，离线可复现）：N=300 时前情块 1,069 字，
**压到旧口径的 5.4%**，且 `uncovered == []`。回归见 `python test_summary_tree.py`（73 断言）。

**前情预检面板（工作台步骤⑥）**：前情现在是**有预算、分层、会丢东西**的构造物，
丢卷/超预算/退化全发生在用户看不见的地方——所以工作台把它摆到台面上。
面板**进工作台即自动预检**（折叠头就带字数、覆盖区间、健康档，不点开也看得见），
展开后给出：分层构成表（每层条数 / 覆盖段号 / 字数）、按层堆叠的预算条、
问题清单（有洞=红 / 退化·超预算·丢卷·摘要缺失=黄）、以及 prompts 原文三个页签。

面板里的「覆盖自证」是**前端从渲染文本独立重算**的，不是转述 `prior_report.uncovered`：
两边必须一致，不一致就报红。理由是后端若哪天回归，面板必须当哨兵而不是复读机。
实测见 `node _verify_prior_panel.js`（27 断言，真浏览器驱动，覆盖「三层齐备」与「超预算」两条分支）。

> 注意 `constraints` **不含**参考语料图（`source=novel:{id}`）——那是风格样本，见上文来源隔离。

### 7. M5：前端工作台
先后端（步骤 2 起 :8000），再起前端：
```
cd frontend
npm install
npm run dev          # http://localhost:5173 ，/api 代理到 :8000
```
- 三视图：**语料库**（上传/分片/M2 检索）· **创作项目**（建项目含大纲/风格/模式）· **项目工作台**（知识图谱 / 生成前提醒 / 生成控制台 三 tab）。
- 换肤：改 `frontend/src/theme.ts` 的 `ACTIVE_THEME`（`editorial` 已锁定）。
- 构建门禁：`npm run build`（内部 `vue-tsc --noEmit && vite build`）。
> 注：后端未提供「列出所有项目」端点，前端项目列表暂用 localStorage 缓存；换浏览器/清缓存会丢列表（数据本身在服务端）。
> 注：若 `npm install` 后 `vite build` 报缺 `@rollup/rollup-win32-x64-msvc`（个别环境 optional dep 被跳过），补装：`npm i -D @rollup/rollup-win32-x64-msvc`。


## 已落地的决策（来自 wayfinder 决策地图）
- 存储：PostgreSQL（pgvector 向量列）；MVP 开发退化为 SQLite。
- 上传解析：UTF-8 txt + 自动分章；**首个章节标记之前的内容（版权页/前言）不进语料**。
- 中文章节号解析支持到万/亿级（长篇 2000+ 章刚需）。
- 采集：MVP 种子语料 + 用户上传（live 爬番茄后置）。
- Embedding：通义千问 v3 @1536（国内供应商，匹配现有向量列）。

## M2 已知约束（非阻塞）
- BM25 用占位分词（CJK 拆字 + 英文词），正式环境应换 jieba 提升关键词质量。
- 向量相似度在 Python 内对单本分片计算；语料膨胀后应将向量检索下推到 PG `<=>` 算子。
- 本地兜底 embedding 仅用于离线测试，禁止生产使用。

## M3 已知约束（非阻塞 · 规则基线）
- **规则抽取为占位质量**：角色只用高精信号（显式命名 `叫X` / 分句主语 / 紧贴说话动词的真名），过滤常见词与 2 字重复名；「命名种子 或 说话归属 或（主语出现≥2 次且跨≥2 章）」才入图，再按精确名回扫补省略指代的章节归属。对短篇/规范叙事精度尚可，**长篇与复杂人名仍有漏抽/误抽风险**。
- **坑结算保守**：仅认强解决短语（真相是/原来是/终于明白/揭晓/答案是…）且在末现章之后同章命中该坑关键词才置 resolved；弱词（明白/原来/真相）误判率高，已刻意放过。
- **关系标签粗糙**：同分片共现 + 首个关系线索词即定标签，`说` 易压过 `给/和`，后续可加权关系动词。
- 以上三项均为**确定性、可离线验证**的基线，正式精度交给 LLM 抽取（同一 `KGExtractor` 接口后置）。

## M4 已知约束（非阻塞）
- **`Project` → `Generation` 没有 FK 级联删除**：删项目不会连带删段（`cascade` 未配，也无删除端点，所以**当前没有用户可达路径**）。风险在直接操作库时暴露：SQLite 会复用被释放的自增 id，于是**新项目会"继承"旧项目的孤儿段**，下一次生成的 `segment_no` 从旧的最大号继续（实测新项目第 1 段变成第 20 段），摘要树的卷边界随之全部错位、L2 永远滚不出来。脚本里清项目必须连段一起删，见 `_seed_prior_demo.py`。补一个删除端点时应同时补级联。
- **离线兜底不是真续写**：`LocalFallbackLLMProvider` 只确定性回显约束 + 占位正文，仅用于离线跑通与验证约束注入；真质量依赖 `LLM_PROVIDER=dashscope` + 真实 `DASHSCOPE_API_KEY`（通义千问 qwen-plus 等对话模型）。
- **RAG 检索依赖 M2 embedding**：若 `novel_chunks.embedding` 为空（未跑 embedding 填充），向量分退化为 0，实际只剩 BM25；生成环仍能跑，但检索上下文质量下降。
- **段级总结链在 `projects.summary`**：扁平、**无损归档**（每段一条，供 RAG 查询词与旧接口读）；真正注入提示词的已改为 `projects.summary_tree` 拼出的有界前情块。外部直接改库时两份都要维护，否则前情与归档会不一致。
- **卷层的启用门槛是 8 段**（= `volume_size`，封满一卷是卷摘要存在的前提）：在此之前近情窗口必然覆盖全部段、卷摘要不参与——只有几段时逐段全文本来信息更多，压缩此时是净亏。门槛是**结构性的**、不是可随便调的常量：`verify_summary_chain.py` 的 D 段扫描 N=8..40 断言「第 8 段起卷层启用且比全量逐段更省」，任何 N 上出现「分层反而更贵」的倒挂即判 FAIL。
- **滚上会多花 2 次短摘要调用/卷**（卷摘要 + 主线骨架），每 8 段一次；幂等，树一致时零调用。真实 provider 失败会被吞掉（不阻断生成），但该卷摘要就一直缺着，`prior_report.degraded` 会暴露它。
- **前情预算可能被"必留层"结构性撑破**：预算 = `clamp(generation_length × 0.4, 600, 2400)`，而必留层（主线骨架 + 近情窗口）不因预算被裁。当 `generation_length` 偏小时会出现「预算 600 装不下 618」的情形（实测 `generation_length=900`：主线 342 + 近情 3 段 276 = 618，两条卷摘要全被丢）。这是**有意的取舍**（覆盖完整优先于预算），不是 bug，`prior_report.over_budget` 如实标记、预检面板加红边并标「超出 N 字」。想压回去就调大单段字数或调小近情段数。
- **大纲为可选**：`ProjectCreate.outline` 已落库并通过 `generate-plan`/`generate` 注入；不填则用户提示无【大纲】区块。
- **漂移防护是「软约束」**：约束挂在系统提示里，由 LLM 自觉遵守；`LocalFallback` 用确定性回显模拟，真实模型仍需评测其遵循度（M6 验收项）。

## M5 已知约束（非阻塞）
- **项目列表本地缓存**：后端无「列项目」端点，前端用 localStorage 存项目列表；换设备/清缓存丢失（项目数据在服务端，重新建项目拿新 id 即可）。后续可加 `GET /api/projects`。
- **设计为 token 驱动**：换肤 = 改 `theme.ts` 一个常量；`brutalist` / `soft` 两套主题已预置但**未做 AA 精修**（默认 editorial 已达标），若要切需补对比度校准。
- **前端自动化测试只有一条**：前情预检面板有真浏览器端到端（`backend/_verify_prior_panel.js`，playwright 驱动，把面板渲染文本与后端 API 原值逐项比对，27 断言，覆盖「三层齐备」与「超预算」两条分支）。其余视图仍以 `vue-tsc` + `vite build` 双闸 + 手工验证为准；组件级单测（vitest）后置。

## M6 验收记录（✅）
**前端双闸**：`vue-tsc --noEmit` 0 错；`vite build` 成功（38 modules，按视图 code-split）。
**后端真实 HTTP 冒烟**（uvicorn :8000，SQLite + local embedding/llm，全 curl 200）：
| 步骤 | 结果 |
|---|---|
| `/health` | `{"ok":true}` 200 |
| 上传种子 `sample_novel.txt` | novel_id=1，3 章 / 3 分片 |
| 建项目（含 outline） | project id，outline 落库 |
| `build-kg` | 2 角色 / 4 坑 / 1 关系 |
| `reminders` | 含「那光是什么」等未填坑 |
| `generate-plan` | 提示注入：林夜×17 / 苏晚×13 / 那光是什么×5 |
| `generate` | 段1，正文回显锁定角色 |
| `generate-loop`(segments=2) | 段2、段3 |
| `generations/{id}` | 累计 3 段，segment_no [1,2,3] |
**设计拍板**：三版方向稿 editorial / brutalist / soft → 用户选 **editorial**；令牌按 WCAG AA 精修（`--accent-ink` 文字色 6.2:1、`--fg-faint` 5.3:1、`--ok`/`--warn` 加深），朱红填充保留鲜艳用于图形。
