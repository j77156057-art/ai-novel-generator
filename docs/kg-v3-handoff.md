# KG v3.0 接手文档

> 生成时间：2026-10-15。当前 HEAD：`6e6f406`（worktrees/master 分支）。
> 直接复制粘贴到新会话即可继续。

---

## 【接手任务】ai-novel-generator —— KG v3.0 后续工作

## 环境（重要，别踩坑）
- 项目路径：`C:\Users\h'h'h\WorkBuddy\2026-10-12-11-59-30\ai-novel-generator`
- 你的 worktree：`worktrees/master`（HEAD = `6e6f406`）
- 用户名含单引号 → bash 路径会炸，命令写成 .py 脚本放 scratch 目录执行，不要 python -c
- 后端 venv：`C:\Users\h'h'h\AppData\Local\Programs\Python\Python313\python.exe`（有 sqlalchemy 2.0.35）
- 跑测试前必须 `env.pop("PYTHONPATH") + env.pop("VIRTUAL_ENV")`（在 .py 脚本里），否则 venv 被 Hermes 环境覆盖
- git commit 用 `git commit -F msg文件`（**不要**在 commit message 里用反引号）
- 不要碰 `worktrees/timeline-pacing` 和 `worktrees/style-distill`（其他 agent 的）

## 当前状态：v3.0 主体已完成（10 个 commit）

```
87317d1 v2.6.1（起点）
1f3e942 Step 2: 9 张表 schema + 模型（39 断言）
7642b31 Step 3: extractor + 异步批抽 worker（84 断言）
9bebd5b Step 4: 独立 Judge Agent（29 断言）
b44c7af Step 5: 检索切片 + 读侧 API（39 断言）
3ae16e8 Step 6: 压力测试 + 冒烟（7 断言）
9565a6c extractor 补全 event_links/entity_knowledge（+18 断言）
d5bea1e 集成：批抽挂主链 + 7 API 端点 + 作者豁免（33 断言）
ec6b2b3 实体归一：X的Y 型变体分裂（42 断言）
6e6f406 表索引：9 表 14 索引，老库幂等补建
<本次> known_kind=secret/event 消费路径：写侧三类落库 + 读侧渲染 + judge secret_leak + db.py 164 行三元短路修复
```

**23 套件全绿**（v30 5 件 + entity_resolve + e2e_v26 + v2.5/v2.6 全家 + knowledge_consumption 新套件 70 断言）。

## known_kind=secret/event 消费路径（2026-10-15 完成）

- **写侧** `batch_worker._persist_extraction` §3.6：entity→实体 id；event→按 `kg_events.summary` 保守匹配解析（`app/kg/knowledge_match.py::event_summary_match`：全等/双向包含/剥标点包含，刻意不做模糊相似——不同事件常只差一个动词），解析失败降级 `known_text` 存文本；secret→`known_entity_id=None` + `known_text`
- **读侧** `retrieval_v3`：known_facts 渲染 `事件：xxx`/`秘密：xxx` 前缀标签（单角色上限 8 条）；`can_character_know` 接受实体名/事件描述/秘密文本三种形态
- **Judge** 新规则 `secret_leak`（warn）：说出 known_kind=secret 的文本秘密且无学习行 → 报。豁免：持有人自述、已学到（规范化文本比对）、<6 字短秘密、项目无 secret 行。RULES 现 6 条（test_v30_judge 的 5→6 已同步）
- **迁移**：`kg_entity_knowledge.known_text VARCHAR(200)` 老库幂等补列（db.py migrations）

## 已修复的存量 bug（顺手）

- **db.py `_migrate_sqlite_columns` 164 行三元短路**：`conn.execute(...) if target_engine is not None else engine` 导致 `init_db()` 默认路径**从不补建** v3.0 索引（只有 load_corpus 离线路径建）。已修 + 有回归断言
- 同函数第二段完全重复的迁移循环已删
- test_v25_stage23 的 FakeStreamingProvider 补 `stop_sequences` 参数（0c9ae98 加的 engine 签名，测试假件没跟上——存量测试债）

## v3.0 架构一览（已跑通）
- 9 张表：`kg_entities / kg_relations / kg_relation_evolution / kg_events / kg_event_links / kg_character_state / kg_foreshadows / kg_entity_knowledge / kg_mentions`
- 核心模块：
  - `app/kg/extractor_v3.py`：LLM 软 schema 抽取（EXTRACTION_SCHEMA 7 类输出，含 sequence 引用）
  - `app/kg/batch_worker.py`：异步批抽 worker（入队→攒批 5 段→落 9 表），已挂 `engine._enqueue_v3_extraction` 主链
  - `app/kg/judge.py`：独立 Judge（瞬移/预知/时间穿越/伏笔超期/语言混入 5 类）
  - `app/kg/retrieval_v3.py`：slice_for_prompt + relation_curve/can_character_know/rhythm_analysis
  - `app/kg/entity_resolve.py`：实体归一（单字核心称谓 + 修饰词剥离，保守）
  - `app/routers/kg_v3.py`：7 个读侧 API 端点（slice/curve/knowledge/rhythm/events/exemptions）
- 压力测试：3000 段=625 万字，检索 35ms，entity 饱和不爆炸
- 真环境验证：10 段跑通（智谱 glm-4-flash），主链 enqueue 9/9

## 下一步候选（按优先级，接手人自选或问用户）

| # | 项 | 说明 | 难度 |
|---|---|---|---|
| 1 | ~~known_kind=secret/event 消费路径~~ | **已完成**（写侧三类落库+读侧渲染+judge secret_leak，见上文专节） | - |
| 2 | **前端集成** | 9 表 + 7 API 端点 + Judge findings 后端就绪但前端不可见。需要 Vue3 页面（关系图/时间线/Judge 面板/豁免管理） | 3-5 天 |
| 3 | **批抽 worker 可观测性** | 后台线程失败只在内存日志，无持久化状态。生产需要队列/失败重试/进度 API | 1 天 |
| 4 | **大纲级联协议阶段 2-3** | final 文档 4 阶段协议的改纲自动级联、KG 回流大纲未实现 | 3-5 天 |
| 5 | **真环境 nemotron 复跑对比** | OpenRouter 配额重置后，用 nemotron-3-ultra 对比智谱弱模型的抽取质量（foreshadows/knowledge 智谱全 0 行） | 半天 |
| 6 | **e2e user journey 测试** | 建项目→生成→批抽→Judge→切片→豁免 全链路"用户视角"测试（目前只有单段级） | 1 天 |
| 7 | Judge 时间锚量纲转换 | impossible_sequences 只判 4 位年份，"贞观三年秋"式非年份时间不比（需历史常识库） | 2 天 |

## 用户偏好（重要）
- 中文交流
- 要求彻底完整实现，会质疑"就这点吗"
- 每个里程碑完成就 commit（master worktree 直接提交）
- 喜欢真实长篇语料端到端验证（红楼梦种子 data/seed/红楼梦.txt）
- 设计文档要先评审再实现
- "精巧"优先于"大而全"——不要一味堆表，要动态生长的设计
- 多 agent 并行时每个任务开独立 worktree
- 设计哲学：增量交付+端到端真验证，但拒绝粗糙。"再看看，不用一味增加，要精巧"

## 测试约定
- 测试是独立脚本：`python tests/test_xxx.py`（在 backend/ 下跑，不是 pytest）
- 断言打印 PASS/FAIL + 总数，退出码 0/1
- 新功能必须配测试
- 跑法：`cd backend && env -u PYTHONPATH -u VIRTUAL_ENV "<venv python>" tests/test_xxx.py`

## 已知坑（多窗口踩过，务必先读）
- **内存库多连接**：batch_worker 后台线程自建 SessionLocal，测试必须用 `StaticPool` 共享连接 + 覆盖 `app.db.SessionLocal`，否则 `:memory:` 每连接独立 → `no such table`
- **后台进程 stdout 断**：长跑脚本的 `w()` 必须 try 包 print（Windows OSError Errno 22 会崩进程）
- **seed 幂等返回空**：续跑模式下 `run_seed` 按 source_url 去重返回 []，`results[0]` IndexError——必须从 RawNovel 库查
- **worktree 种子在根级** `data/seed/`，不在 backend/ 下——cwd=backend 时用绝对路径
- **uvicorn 端口避开** 8020/8100/8848/8011
- **OpenRouter nemotron 免费档 50 次/天**：大批量前查剩余，耗尽会全 429；智谱 glm-4-flash 免费无限但抽取深度弱（foreshadows/knowledge 全 0 行）
- **真调 LLM 超长 pipeline 会超时**：真环境验证用后台进程 + 写文件，不要 HTTP client 等

## API key
- 智谱 glm-4-flash 有效 key 在 `backend/.env` 的 ZHIPU_API_KEY（2026-10-15 验证可用）
- OpenRouter key 在 `backend/.env` 的 OPENROUTER_API_KEY（nemotron-3-ultra-550b-a55b:free，配额已耗尽待重置）

## 核心设计文档
- `worktrees/master/docs/kg-v3-design-final.md`：v3.0 最终设计（9 表 + Judge + 8 功能，已实现主体）
- skill `ai-novel-generator` 已沉淀全部踩坑（hermes skills 目录）

## 接手后第一件事
1. 读 `docs/kg-v3-design-final.md`
2. 跑 `backend/tests/test_v30_schema.py` 确认环境通
3. 问用户下一步做上表哪个候选（或用户另有方向）
