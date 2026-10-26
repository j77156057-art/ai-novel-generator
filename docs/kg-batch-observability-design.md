# 批抽 worker 可观测性设计（kg-batch-observability）

> 2026-10-15。接手候选 #3：「后台线程失败只在内存日志，无持久化状态。
> 生产需要队列/失败重试/进度 API」。
> 关键事实：batch_worker.py 头部 docstring **早已声明**「队列用 AgentRun 表
> （已有 role/status 机）+ 内存 deque，不新建表（精巧优先）」「enqueue_segment
> 只写一行 AgentRun(role=kg_batch_extract)」——但实现时从未写入。本任务是
> **补完已声明的设计意图**，不是新设计。

## 一、现状（缺口清单）

| 缺口 | 现状 | 后果 |
|---|---|---|
| 持久化状态 | 队列纯内存 deque，AgentRun 一行不写 | 重启丢队列无痕；哪段抽过/失败/没抽无从知晓 |
| 失败处理 | 仅 Python logging（内存）；LLM 挂时 extract_batch 吞异常返回空骨架 | 失败被记成"0 产出的成功"，毒批无限重试或静默丢失 |
| 重试 | 无 | 一次网络抖动 = 5 段情报永久丢失 |
| 进度 API | 无 | 「批抽到哪了/积压多少」只能看日志 |
| worker 存活 | 线程引用不暴露 | 线程死了服务照跑，无人知道 |

## 二、方案

### 2.1 每段一行 AgentRun（role=kg_batch_extract）

- **enqueue_segment** 写一行：`status=running`，`started_at=now`，
  `request_hash=sha256("{project_id}:{segment_no}")`（稳定可关联），
  `note=JSON {"segment_no", "chapter"}`。行随 engine 的 generation 事务一起提交
  ——**入队即持久化**，重启后 running 行 = 丢失/卡住的段，一眼可见
- **批完成**：该批 5 行统一更新 `status=success`，`finished_at=now`，
  `note` 追加 `batch_stats`（entities/relations/events/links/knowledge/states/
  foreshadows/errors——ExtractionStats 是批级粒度，同一批各行 stats 相同，
  这是 LLM 一次抽 5 段的诚实反映）
- **批失败**：该批行 `status=failed`，note 记错误
- 体量：段-paced 生成（LLM 60s/段），千段小说 = 千行，SQLite 无压力

### 2.2 失败重试（进程内，有限次）

- `_run_batches` 捕获异常 → 同批**立即重试**（≤2 次）→ 仍失败：
  行标 failed（note 带错误），**不自动重入队**（毒批防护），走手动重捞
- 空骨架检测：整批 0 entities 且 0 events 且 0 relations → note 标
  `"empty": true`（真实小说文本几乎不可能，大概率是 LLM 挂了）。
  状态 API 汇总 empty 数

### 2.3 读侧 API（挂 /api/projects/{id}/kg-v3/worker）

| 端点 | 返回 |
|---|---|
| `GET /status` | `worker_alive`（线程引用 is_alive）、`pending`（内存队列积压段数）、`summary`（success/failed/running/stuck 计数，stuck=running 且 started_at 超 30 分钟——重启残留）、`recent_runs`（最近 50 行：segment_no/status/耗时/stats） |
| `POST /requeue` | body `{run_ids: [...]}`（限本项目的 failed 行）→ 段文本从 Generation 表回读（project_id+segment_no），重新入队，行重置 running。返回 `{requeued, missing}`（文本已被删的段报 missing） |

### 2.4 assistant 日志隔离（防污染，必做）

AgentRun 将变成多 role 表，但 assistant 的 `GET /runs` 与 `analyze-logs`
**不过滤 role**——worker 的 failed 会进助手失败率。修法：assistant 端点与
`fetch_run_history` 加 `role="assistant"` 默认过滤（今天只有 assistant 写该表，
行为保持不变；worker 端点过滤 role="kg_batch_extract"）。AgentRun docstring
的「role 硬约束 assistant」注释同步改为「assistant 专属端点按 role 过滤」。

### 2.5 前端：KG 工作台第 6 个 tab「批抽状态」

- 顶部：worker 存活徽标（绿/红）+ 积压数 + summary 四计数（stuck 标红）
- 最近批次表：段号/状态 tag/耗时/stats 摘要/empty 标记
- failed 行带「重捞」按钮（单个/全部），requeue 后刷新

## 三、明确不做

- **不做** DB 持久化队列（重启后 running 段靠 requeue API 手动重捞——
  千段级偶发重启，手动可接受；DB 队列是另一档工程）
- **不做** 重试退避调度（立即重试 2 次够覆盖网络抖动；持续挂是 key/配额问题，
  重试解决不了，暴露出来给人看才是对的）
- **不做** per-segment 精确 stats 归因（一次 LLM 抽 5 段，无法拆分——诚实记批级）

## 四、工作量

| 项 | 量 |
|---|---|
| batch_worker 改造（enqueue 落库/批完成更新/重试/空骨架标记/线程引用） | ~150 行 |
| 2 个 API + assistant role 隔离 | ~120 行 |
| 测试（落库/更新/重试/重捞/stuck/隔离） | ~45 断言 |
| 前端 tab | ~180 行 |
| **合计** | **~1 天** |

## 决策点

1. AgentRun 粒度：**每段一行**（推荐，精确知道哪段丢了）vs 每批一行
2. 重试策略：**立即重试 2 次 + 手动重捞 API**（推荐）vs 自动重入队
3. assistant 隔离：**role 过滤**（推荐）vs 不隔离
4. 前端：**加第 6 个 tab**（推荐）vs 只做 API
