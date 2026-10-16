# KG v3.0 阶段 2 接手文档

> 生成时间：2026-10-15。当前 HEAD：`e31a2a9`（worktrees/master 分支）。
> 直接复制粘贴到新会话即可继续。**先读本文档第 0 节（并发状态），再动手。**

---

## 0. 接手第一件事：处理并发 agent 的未提交改动（重要）

工作树里有一名**并发 agent** 的未提交改动，与我的 commit 叠加在同一棵树上：

```
 M backend/app/agents/log_audit.py          (+6)
 M backend/app/kg/batch_worker.py           (+255)  ← 可观测性增强
 M backend/app/routers/assistant.py         (±3)
 M backend/app/routers/kg_v3.py             (+36)
 M frontend/src/api/client.ts               (+18)
 M frontend/src/api/types.ts                (+40)
 M frontend/src/views/ProjectWorkspace.vue  (+246)  ← 含 bible 编辑 UI 等
?? docs/kg-batch-observability-design.md
?? docs/kg-cascade-protocol-design.md
```

这些改动**不是垃圾**：batch_worker 可观测性增强 + bible 级联前端 + 设计文档。
我（上一个窗口）基于干净 HEAD 的 commit 全部干净叠加在它们之上，测试全绿
（见第 4 节），所以**当前工作树 = 我的 HEAD + ta 的未提交改动 = 可工作状态**。

接手三选一（**先问用户**，不要自己决定）：

1. **保留并帮 ta 提交**（推荐）：`git add -A && git commit`，message 说明来源。
   之后工作树干净，所有后续任务不再有并发噪声。
2. **只 stash 不丢**：`git stash push -u -m "concurrent agent wip"`，工作树回到
   干净 HEAD；后续要做时 `git stash pop`。
3. **reset 丢弃**：`git checkout -- . && git clean -fd docs/kg-batch-observability-design.md docs/kg-cascade-protocol-design.md`。
   **仅在用户确认这些改动不要了才做**——batch_worker 的可观测性增强是 ta 的
   实际工作成果。

## 1. 环境（重要，别踩坑）

- 项目路径：`C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator`
- 你的 worktree：`worktrees/master`（HEAD = `e31a2a9`）
- 用户名含单引号 → bash 路径会炸，命令写成 .py 脚本放 scratch 目录执行，不要 python -c
- 后端 venv：`C:\Users\h'h'h\AppData\Local\Programs\Python\Python313\python.exe`（有 sqlalchemy 2.0.35）
- 跑测试前必须 `env.pop("PYTHONPATH") + env.pop("VIRTUAL_ENV")`（在 .py 脚本里），否则 venv 被 Hermes 环境覆盖
- git commit 用 `git commit -F msg文件`（**不要**在 commit message 里用反引号）
- 不要碰 `worktrees/timeline-pacing` 和 `worktrees/style-distill`（其他 agent 的）
- taskkill 在 MSYS bash 里要用单斜杠 `/F /PID`，`//F` 不识别
- 前端 `npm run dev` 需 `API_TARGET` 环境变量指向后端；Popen 里 npm 要写 `npm.cmd`
- uvicorn 端口避开 8020/8100/8848/8011/8021（8021 被冒烟占用可能残留）

## 2. 本次会话完成的工作（6 个 commit，全部基于干净 HEAD）

```
e31a2a9 docs: 划掉大纲级联候选
71b8678 大纲级联协议阶段 2-3 收尾：merged_constraint_items 的「bible 删除权威」（+7 断言）
75819ba 大纲级联协议阶段 2-3 后端：plot_bible 版本化 + KG 冲突扫描（并发 agent 提交）
88b0f7f docs: worker 可观测性完成说明 + 并发警告
a2181c3 批抽 Worker 可观测性：审计发现后端已完整实现，本次只补前端入口
5ce1737 docs: 前端集成设计文档（方案 A，已实施）
2084913 前端冒烟修复：slice/prompt 参数对齐 + tab 惰性加载
a6ea0b6 前端集成：KG 工作台 5 tabs
98e024d 前端集成后端部分：Judge findings 落库 + 4 读侧端点
581e813 known_kind=secret/event 消费路径（前一窗口）
```

**重大工作模式**：4 个候选任务里 3 个是「审计发现后端已实现，本次只补前端/收尾」——
接手文档的候选表严重过时，**新任务先审计再动手**（文件在哪 → 测试绿不绿 →
缺口在哪），别按文档描述直接开干。

## 3. 各任务完成细节

### 3.1 known_kind=secret/event 消费路径（581e813，已完成）

- 写侧 `batch_worker` §3.6 三类分流：entity→实体 id；event→按 summary 保守匹配
  （`knowledge_match.event_summary_match`：全等/双向包含/剥标点包含，刻意不做模糊相似），
  失败降级 `known_text`；secret→`known_entity_id=None`+`known_text`
- 读侧 `retrieval_v3`：known_facts 渲染 `事件：`/`秘密：` 前缀标签；`can_character_know` 三形态
- Judge 新规则 `secret_leak`（warn，RULES 5→6）：holder 自述豁免/已学到/短秘密/无 secret 行不报
- 迁移 `kg_entity_knowledge.known_text VARCHAR(200)` 老库补列
- **顺手修存量 bug**：`db.py` 164 行三元短路使 `init_db()` 默认路径从不补建 v3.0 索引

### 3.2 KG 前端工作台（98e024d/a6ea0b6/2084913/5ce1737，已完成）

- 新路由 `/projects/:id/kg`（KgWorkspace.vue 零新依赖）：6 tabs = 切片预览/人物情报/
  事件时间线/Judge 面板/关系与节奏/Worker 状态
- 后端新增：Judge findings 落库（Fact type=kg_judge，engine 判定后自动存，先清后写同段）、
  DELETE /exemptions、GET /judge/findings、/characters、/relations
- 冒烟抓到 2 bug：① `/slice/prompt` 不接 characters 参数→渲染文本永远缺状态/情报块；
  ② switchTab 不触发数据加载
- 设计决策：关系图 = **环形静态 SVG**（零物理引擎）；力导向图明确不做

### 3.3 Worker 可观测性（a2181c3，审计模式）

- **审计发现**后端完整实现已存在（commit 6e6f406 之后）：enqueue_segment 写
  AgentRun + _run_batches 翻 success/failed/empty（RETRY_LIMIT=2）+ worker_status
  汇总 + requeue_failed 手动重捞 + stuck 判定（STUCK_MINUTES=30）
- 唯一缺口：前端零入口 → 补 Tab 6「Worker 状态」（线程 alive/dead、内存积压、
  5 项 summary、recent_runs、failed 行右侧重捞 checkbox + 批量重捞）
- 顺带：test_batch_worker_observability.py 36 断言文件**漏 git add**，补提交；
  修 summary success 期望值（2→3）

### 3.4 大纲级联协议阶段 2-3（75819ba + 71b8678，审计模式）

- 设计文档：`docs/kg-cascade-protocol-design.md`（并发 agent 写的，决策 A：只存修改后+diff）
- 已有（75819ba，并发 agent 提交）：`app/kg/bible.py` diff_bible/detect_conflicts/
  apply_bible_revision/list_bible_revisions + KgBibleRevision 模型 +
  POST `/bible/apply` + GET `/bible/revisions`；前端编辑模态/历史侧栏/conflicts toast
- 本次补唯一空缺（71b8678）：`prompts.merged_constraint_items` 的 **bible 删除权威**——
  bible.characters 显式列名时，未列入的 KG character_constraints 整条剔除
  （bible 里删了「顾辰逸」后旧 KG 事实不再把他当锁定角色注入 prompt）。
  四条保守边界不过滤：无 characters 键/空列表/bible=None/无 name 键行。只做角色侧。

## 4. 测试约定与当前绿数

- 独立脚本：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`
- 断言打印 PASS/FAIL + 总数，退出码 0/1；新功能必须配测试
- 冒烟脚本位置：`C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\run_tests.py`
  （python 脚本包 venv，`python run_tests.py tests/test_x.py` 可批量）
- 种子脚本：`seed_kg_smoke.py`（KG 五表数据）、`seed_worker.py`（AgentRun 3 行）在 scratch
- **核心 5 套件当前全绿**：cascade_protocol 49 / batch_worker_observability 36 /
  kg_v3_frontend_api 41 / v30_integration 33 / v30_judge 29。上次全量 15+ 套件不破
- **接手后第一件事：跑一遍核心 5 套件确认环境通**（并发 agent 的未提交改动叠加后）

## 5. 下一步候选（仅剩 3 项）

| # | 项 | 说明 | 难度 |
|---|---|---|---|
| 5 | **真环境 nemotron 复跑对比** | OpenRouter 配额重置后，用 nemotron-3-ultra 对比智谱弱模型的抽取质量（foreshadows/knowledge 智谱全 0 行）。**先查当天剩余额度**（免费档 50 次/日），耗尽会全 429 | 半天 |
| 6 | **e2e user journey 测试** | 建项目→生成→批抽→Judge→切片→豁免 全链路"用户视角"测试。两层：mock 回归套件 19 断言 + 真 LLM 冒烟脚本挂后台。冒烟首次跑暴露真 bug（LocalFallbackLLMProvider 缺 stop_sequences 签名），记录独立议题待修 | 1 天 |
| 7 | **Judge 时间锚量纲转换** | impossible_sequences 只判 4 位年份，"贞观三年秋"式非年份时间不比（需朝代年号→公元年的常识库，已有 `data/seed/` 红楼梦可试水） | 2 天 |

另有打磨项（非候选）：前端关系图升级力导向 / 移动端适配 / KG 工作台数据刷新按钮
统一（现在每 tab 各自刷新，切 tab 惰性加载只在首次）。

## 6. 用户偏好

- 中文交流；设计文档先评审再实现；里程碑完成就 commit
- 要求彻底完整实现，会质疑"就这点吗"；百万字~千万字规模考虑
- 真实长篇语料端到端验证（红楼梦种子 data/seed/红楼梦.txt，1047687 字）
- "精巧"优先于"大而全"——不要一味堆表/堆功能，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree（**并发 agent 违反了这条**，见第 0 节）
- 增量交付+端到端真验证，但拒绝粗糙

## 7. 已知坑（多窗口踩过）

- **内存库多连接**：batch_worker 后台线程自建 SessionLocal，测试必须 `app.db.SessionLocal = 内存 Session`（_appdb.SessionLocal 赋值）+ StaticPool，否则 :memory: 每连接独立
- **后台进程 stdout 断**：长跑脚本的 `w()` 必须 try 包 print（Windows OSError Errno 22 崩进程）
- **seed 幂等返回空**：续跑模式 run_seed 按 source_url 去重返回 []，必须从 RawNovel 库查
- **worktree 种子在根级** `data/seed/` 不在 backend/ 下——cwd=backend 时用绝对路径
- **uvicorn 端口占用**：起新服务前 `netstat -ano | grep ":端口"`，taskkill 用单斜杠 `/F /PID`
- **真调 LLM 智谱 60-72s/段稳定**；OpenRouter nemotron 中文质量好但免费档 50 次/日
- **测试期望值 vs 实现的裁断**：跑出失败先分清"实现错"还是"期望值写错"——
  本会话 3 次失败全是后者（summary success 3≠2、合并去重后名单长度、清册过滤条数），
  用 scratch 里的小 probe 脚本查真实值再定，别急改实现

## 8. API key

- 智谱 glm-4-flash 有效 key 在 `backend/.env` 的 ZHIPU_API_KEY（2026-10-15 验证可用）
- OpenRouter key 在 `backend/.env` 的 OPENROUTER_API_KEY（nemotron-3-ultra-550b-a55b:free）
- 启动后必须真调一次 LLM 验通，不能只看 uvicorn 200

## 9. 核心设计文档清单

- `docs/kg-v3-design-final.md`：v3.0 最终设计（9 表 + Judge + 8 功能）
- `docs/kg-v3-handoff.md`：上一版接手文档（历史）
- `docs/kg-v3-frontend-design.md`：前端集成设计（方案 A，已实施）
- `docs/kg-cascade-protocol-design.md`：级联协议设计（未跟踪文件，并发 agent 写）
- `docs/kg-batch-observability-design.md`：批抽可观测性设计（未跟踪文件，并发 agent 写）
- skill `ai-novel-generator` 已沉淀全部踩坑
