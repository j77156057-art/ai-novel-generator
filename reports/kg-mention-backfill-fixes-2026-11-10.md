# 三处缺陷修复与回填验证（2026-11-10）

## 0. 结论摘要

| 缺陷 | 修复 | 验证 |
|---|---|---|
| 1. mentions 回填入口 | 启动恢复 + 超时 running 重捞 + 无入队行段主动回填 | p1/p5 四段全部补建入队行并跑完（4/4 success） |
| 2. tier/指纹不匹配静默蒸馏 | 生成路径只降级或拒绝，不再同步蒸馏 | 副本模拟不匹配：0.004s 降级 + WARN，0 次蒸馏 |
| 3. RRF O(n²) | 两路 RRF 提到推导式外 | 6875 chunk：15.38s → 0.0037s，结果逐项相同 |

回填结果一句话：**p1 补回了正文里真实出现的普通物品 mention；p5 的 43 个《大奉》物品一条也没补回，因为四段正文里一个物品名都没出现——这是正确结果，不是失败。**

## 1. 缺陷 1：mentions 的回填入口

### 1.1 改了什么

`backend/app/kg/batch_worker.py`

- `recover_running(db, project_id=None, *, stuck_only=False)`
  - 启动时用 `stuck_only=False`：把库里**所有** `role=kg_batch_extract` 且
    `status=running` 的行按 `Generation.text` 重新入队。进程刚启动时内存队列一定
    为空，所以 running 行等价于「上一进程留下的、永远没人处理的段」，不必等
    30 分钟超时。
  - 手动 API 用 `stuck_only=True`：只重捞超过 `STUCK_MINUTES` 的行。
  - 同一段多条 running 行只入队一次，其余标 failed（`duplicate_running_row`）。
- `requeue_failed`：候选从「仅 failed」扩到「failed 或超时 running」。
- `_requeue_row`：正文缺失时把行标 `failed` 并写 `error=<reason>_text_missing`，
  同时 WARN——不静默丢。
- `backfill_project(db, project_id)`：逐段按 `request_hash` 检查是否已有
  AgentRun 行；**没有行才补建并入队**。已有行（success/failed/running）一律跳过，
  reason 带现有 status；正文为空的段跳过并 WARN。幂等。
- `_index_mentions_only`：保留原来的 `_first_mentioned_form` 正文命中守卫，
  **没有放宽任何匹配条件**；新增一条汇总日志，写明命中段、未命中段、空正文段。

`backend/app/main.py`

- lifespan 里 `init_db()` 之后调用 `ensure_worker()` + `recover_running(db)`。
  失败只 WARN，不拖垮启动。

`backend/app/routers/kg_v3.py`

- `POST /kg-v3/worker/recover?project_id=&stuck_only=`
- `POST /kg-v3/worker/backfill?project_id=`
- `/worker/requeue` 文档更新（现在也收 stuck running）。

`backend/migrations/2026-11-10-kg-backfill-v1/apply_backfill.py`

- `--dry-run`：逐段报告有没有入队行、物品名在正文里能否命中（不写库）。
- 默认：先备份库 → 补建入队行 → `flush_project` 同步跑完 → 写快照 JSON。
- `--no-process`：只入队，交给服务里的 worker。
- `--rollback <snapshot.json>`：删除该次新建的 AgentRun 与 kg_mentions 行，
  并重算 `mention_count`。

### 1.2 dry-run 实测（真实库）

| 项目 | 段 | 正文长度 | 物品实体 | 物品名命中 | 已有入队行 |
|---|---|---|---|---|---|
| p1 | seg1 | 1036 字 | 0 | 0 | 无 |
| p1 | seg2 | 1341 字 | 0 | 0 | 无 |
| p5 | seg1 | 1413 字 | 43 | 0 | 无 |
| p5 | seg2 | 2273 字 | 43 | 0 | 无 |

回填前：`agent_runs(role=kg_batch_extract)` 0 行，`kg_mentions` 0 行。
四段都命中「压根没有行」这一类，正是 `requeue_failed` 永远够不到的场景。

### 1.3 真实回填结果

执行：

```bash
python backend/migrations/2026-11-10-kg-backfill-v1/apply_backfill.py --project 1 --project 5
```

- 4 段全部补建入队行：AgentRun `72/73`（p1）、`74/75`（p5），状态全部 `success`。
- p1：19 条 mention（character 2、location 7、item 10），新建 14 个实体
  （character 1、location 6、item 7：文件/策划书/笔/咖啡/手机/电脑/邮件）。
- p5：8 条 mention（全部 character：小杨/失踪者A/失踪者B/老李），**item mention 0**；
  43 个物品实体没有新增、也没有被误命中。
- 批级 stats 正常落 note（p1 关系 12/事件 11，p5 关系 6/事件 7）。

### 1.4 补得回什么、补不回什么、为什么

**补得回**

- 四段全部从「零行」变成有账可查、可重捞的入队行，并真实跑完。
- p1 正文里出现的普通物品（文件、策划书、笔、咖啡、手机、电脑、邮件）被 LLM
  抽成实体并落了 item mention。这些物品确实在正文里出现，不是硬造。

**补不回**

- p5 的 43 个《大奉》物品：0 条 item mention。
- 原因：p5 四段正文都是都市悬疑模板（「小杨站在废弃的工厂前……」），43 个物品名
  及其别名写法在正文里一次都没出现。别名降级路径的 `_first_mentioned_form` 要求
  正文独立命中，命中 0 就不落 mention。
- 这不是「没跑」：batch 行是 success，日志写明「落 0 条 mention」，回填报告逐段
  列出 `item_hits: []`。

**守卫专项验证**

在**回填前**的库备份副本上强制 `provider=None`，只走别名降级路径跑 p5：

```
kg v3 批抽降级为 alias mention（项目 5，2 段）：provider 不可用，落 0 条 mention
p5 item mentions: 0    all mentions: 0
```

43 个物品实体、2 段正文、0 条 mention。正文命中守卫没有被放宽。

### 1.5 可观测性

- 启动恢复：重入队/正文缺失都打 WARN，并返回 `{requeued, missing, skipped, segments}`。
- 重捞：正文缺失的行标 `failed + error`，WARN 带 project/run/segment。
- 回填：返回逐段 `enqueued` / `skipped`（reason 带现有 status），并打日志。
- 别名降级：批次 WARN 写明落了几条；`_index_mentions_only` 汇总日志写明哪些段
  未命中、哪些段空正文。

## 2. 缺陷 2：tier 与档案指纹不匹配

### 2.1 改了什么

`backend/app/style/narrative.py`

- 新增 `_degraded_narrative`：只在 `persist_library=False`（生成路径）使用。
  顺序：
  1. 项目缓存可用且 `source_ids` 相同 → 直接复用（哪怕档位/版本不同），WARN 写明
     期望档位与实际档位。
  2. 蒸馏库里同语料集合的 active 行：先轻查，**没有候选就不算正文 hash**；有候选
     才 `corpus_content_hash` 校验，hash 一致才复用，WARN 写明实际档位。
  3. 都没有 → 返回 `None`，WARN 明确拒绝同步蒸馏。
- `project_narrative` 在 `persist_library=False` 且指纹不匹配时直接返回上述结果，
  **不再进入 `extract_narrative_profile`**。后台蒸馏路径（`persist_library=True`）
  行为不变。

### 2.2 实测

先纠正一个前提：**当前库里 p5 的项目缓存指纹是 `balanced / 8aaceb261c43`，与期望
一致**，所以这次直接命中缓存（0.14s），没有复现原来的 120 秒超时。蒸馏库里有
两条同源同正文行：`fast / 8f041a76c181` 与 `balanced / 8aaceb261c43`。

在临时副本上把 p5 缓存指纹改成 `fast` 模拟不匹配：

```
A mismatch elapsed=0.004s tier=fast usable=True distill_delta=0
A log: 生成路径叙事档案降级：项目 5 期望 tier=balanced（指纹 8aaceb261c43），
       实际使用项目缓存 tier=fast（指纹 8f041a76c181）；未触发同步蒸馏。
```

再模拟「项目缓存为空且语料集合没有可用库档」：

```
B no-archive elapsed=0.005s returned=None db_still_none=True distill_delta=0
B log: 生成路径拒绝同步蒸馏：项目 5 期望 tier=balanced（指纹 8aaceb261c43），
       项目缓存与蒸馏库都没有可用档案。请先调用 POST /api/projects/5/distill 完成蒸馏后再生成。
```

两条路径都不写库、不新增 `style_distills` 行。

## 3. 缺陷 3：RRF O(n²)

`backend/app/retrieval/search.py`：把 `_rrf(bm25_rank)` / `_rrf(vec_rank)` 提到
推导式外，各算一次。

6875 chunk 实测（随机分数，同一口径）：

```
n=6875 old=15.38s new=0.0037s identical=True
```

## 4. 回归测试

| 测试 | 结果 |
|---|---|
| `tests/test_batch_worker_observability.py` | 37/37 通过 |
| `tests/test_kg_silent_failures.py` + `tests/test_kg_mentions_alias.py` | 14/14 通过 |
| `tests/test_narrative_extract.py` | 34/34 通过 |
| `tests/test_v30_retrieval.py` | 39/39 通过 |
| 改动模块 `compileall` | 通过 |

## 5. 快照与回滚

### 5.1 代码（三个独立 commit）

| 缺陷 | commit | 回滚 |
|---|---|---|
| 1 | `13fbd44` | `git revert --no-edit 13fbd44` |
| 2 | `0df8847` | `git revert --no-edit 0df8847` |
| 3 | `7af5a80` | `git revert --no-edit 7af5a80` |

仓库里其它未提交的改动（item split 等）没有纳入这三个 commit。

### 5.2 数据（本次真实回填）

- 回填前 DB 备份：`backend/backups/novel_dev-pre-kg-backfill-20261110-212942.db`
  （完整恢复用这个最稳）。
- 快照 JSON：`backend/migrations/2026-11-10-kg-backfill-v1/snapshots/backfill_20261110-213503.json`
  （记录本次新建的 AgentRun id、mention id、touched entity id）。
- 定向回滚：

```bash
python backend/migrations/2026-11-10-kg-backfill-v1/apply_backfill.py \
  --rollback backend/migrations/2026-11-10-kg-backfill-v1/snapshots/backfill_20261110-213503.json
```

注意：定向回滚只删本次新建的 AgentRun 与 kg_mentions，并重算 `mention_count`；
**不删** LLM 抽取顺带新建的实体/关系/事件。要完整回到回填前状态，用 DB 备份。

## 6. 没做的

- 注入效果验收（物品持有块是否真的出现在生成正文里）按约定另行验收，本次只验证
  回填入口本身。
- 没有为了凑结果修改 p1/p5 的模板正文，也没有放宽正文命中检查。
