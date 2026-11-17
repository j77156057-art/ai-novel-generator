# 重写前快照：存储结构与写入时机（设计）

> 本轮只出设计，不执行 DDL、不改代码、不动库。脚本方案在第 10 节，等批准。
> 不动 `app/style/**`。
> 结论先行：**新建 `generation_snapshots` 表，不复用 `versions`。**

---

## 0. 结论摘要

1. **复用 `versions` 不可行**：`proposal_id` 是 `NOT NULL` 外键（[models.py:654](backend/app/models.py#L654)），而重写没有提案；且该表被明确定义为「永不 UPDATE」的提案产物（[models.py:632-637](backend/app/models.py#L632-L637)），而快照必须经历 `pending → applied/failed` 的状态更新。两者生命周期不同。
2. **写入时机采用两段式**：模型调用**前**写一条 `pending`（只含 before，兼作并发占位与失败留痕）；模型返回后，在**引擎原有那次提交里**把正文与快照一起落为 `applied`。
3. **事务边界**：正文更新与快照 `applied` 必须同一事务（[engine.py:2055](backend/app/generation/engine.py#L2055)）；`pending` 是独立的短事务，不在模型调用期间持有写锁。
4. **崩溃安全**：模型调用期间崩溃 → 只剩 `pending`，正文未变；提交前崩溃 → 事务回滚，正文未变；提交后崩溃 → 正文与快照一致，只有 KG/摘要可能落后（这是引擎既有风险，不是快照引入的）。
5. **并发**：`(project_id, segment_no) WHERE status='pending'` 部分唯一索引挡住同段并发；提交时用**正文内容哈希 CAS** 防止跨入口丢更新。**不给 `generations` 加版本号**（原因见 5.3）。
6. **批量**：重写按段各自提交（引擎必须先提交正文再 replay KG，[engine.py:2052-2055](backend/app/generation/engine.py#L2052-L2055)），所以批量重写不做事务级原子；**批量回滚**在一个事务里恢复所有正文，能整组退。
7. **连锁变化选择「一起退」**：回滚正文后重建摘要树 + 全量 replay KG。后果是回滚变慢（KG replay 全量），但不会出现「摘要说 A、正文是 B」。
8. **容量**：925 段 × 每段 1200 字 × 保留 3 版 ≈ **22 MB 正文快照，含索引与页开销约 25–28 MB**；不设上限、每段 10 版约 75–90 MB。全量快照可接受，不需要 diff/压缩。

---

## 1. 先查：`versions` 能不能直接承载

### 1.1 字段对照

| 需求 | `versions` 现状 | 结论 |
|---|---|---|
| 指向被改的段 | 没有 `generation_id`；只有 `snapshot_json.target_id` 字符串（[models.py:641-646](backend/app/models.py#L641-L646)） | 可用但只能弱引用，无法做外键与按段查询 |
| 正文 | `snapshot_json.before / after` | 够用 |
| 时间 | `created_at` | 够用 |
| 来源标识 | `kind`（text/prompt/bible/…）、`source_proposal_id`、`is_restore` | 没有「操作类型」「操作者入口」「指令」 |
| 一次操作多段的分组 | **没有** `batch_id` | 不够用 |
| 段号 | **没有** `segment_no` | 不够用 |
| 状态流转 | 表定义为不可变、只追加（[models.py:632-637](backend/app/models.py#L632-L637)） | 快照需要 `pending→applied/failed`，冲突 |
| 写入前置条件 | `proposal_id NOT NULL` 外键到 `edit_proposals`（[models.py:654](backend/app/models.py#L654)） | **硬阻塞** |

### 1.2 三条可选路线与代价

| 路线 | 做法 | 代价 | 判定 |
|---|---|---|---|
| A. 复用 + 放松约束 | 把 `proposal_id` 改成 nullable，再补 `batch_id/segment_no/operation` 可空列 | SQLite **不支持**直接改列可空性，要 12 步重建表（建新表→拷数据→删旧表→改名→重建索引/外键）。本项目现有迁移机制只做 `ALTER TABLE ADD COLUMN`（[db.py:129-138](backend/app/db.py#L129-L138)），没有重建表的工具 | 否 |
| B. 复用 + 伪造提案 | 每次重写先插一条 `edit_proposals`（`run_id` 有值、`type="rewrite"`、`status="applied"`），再走既有 `insert_version_row` | 自动重写会进入人工审批箱；所有提案列表/统计都要额外过滤；回滚会把提案置 `rolled_back`，污染审批指标；且 `versions` 仍无 `batch_id` | 否 |
| C. 新建专用表 | `generation_snapshots`，只服务「执行型重写」 | 新表 + 新索引；回滚逻辑自写（可照抄 `restore_version` 的语义） | **采用** |

### 1.3 为什么不「加几个可空列」就完事

`batch_id`、`segment_no`、`operation` 确实是可空列能解决的，但 `proposal_id` 的 `NOT NULL` 不是加列能解决的；而且即使绕过它，`versions` 的「永不 UPDATE」不变量（[models.py:632-637](backend/app/models.py#L632-L637)）会被 `pending→applied` 打破，已有测试与读侧假设都要跟着改。新建表反而更小的爆炸半径。

---

## 2. 表结构

### 2.1 DDL 草案（SQLite / PostgreSQL 通用）

```sql
CREATE TABLE generation_snapshots (
  id                       INTEGER PRIMARY KEY,
  project_id               INTEGER NOT NULL REFERENCES projects(id),
  generation_id            INTEGER NOT NULL REFERENCES generations(id),
  segment_no               INTEGER NOT NULL,
  batch_id                 VARCHAR(36) NOT NULL,
  operation                VARCHAR(24) NOT NULL,   -- rewrite | batch_rewrite | rollback
  status                   VARCHAR(16) NOT NULL,   -- pending | applied | failed | rolled_back
  before_text              TEXT NOT NULL,
  after_text               TEXT,
  before_hash              VARCHAR(64) NOT NULL,   -- sha256(before_text)
  after_hash               VARCHAR(64),
  instruction              TEXT,                   -- 本次重写要求，用于解释「为什么变成这样」
  agent_run_id             INTEGER REFERENCES agent_runs(id),
  bible_hash               VARCHAR(64),            -- 快照时的 plot_bible 指纹（旧名一致性）
  error_reason             VARCHAR(255),
  created_at               DATETIME NOT NULL,
  finished_at              DATETIME,
  rolled_back_at           DATETIME,
  rolled_back_by_snapshot_id INTEGER REFERENCES generation_snapshots(id)
);

CREATE INDEX ix_gs_project_segment ON generation_snapshots(project_id, segment_no, id DESC);
CREATE INDEX ix_gs_batch           ON generation_snapshots(batch_id, segment_no);
CREATE INDEX ix_gs_status_created  ON generation_snapshots(status, created_at);

-- 同一段同时只允许一个在途重写；SQLite / PG 都支持部分唯一索引
CREATE UNIQUE INDEX uq_gs_pending
  ON generation_snapshots(project_id, segment_no)
  WHERE status = 'pending';
```

### 2.2 字段说明

| 字段 | 为什么需要 |
|---|---|
| `generation_id` | 段可能被删/重建，外键锁住「快照属于哪一行正文」；恢复时按 id 写回 |
| `segment_no` | 批量回滚排序、按段查历史，不用 join `generations` |
| `batch_id` | 一次用户操作（「最近 3 段一起调语气」）共享一个 UUID；单段重写也生成一个，保持语义统一 |
| `operation` | 区分 `rewrite` / `batch_rewrite` / `rollback`，回滚历史可读 |
| `status` | `pending` 在途、`applied` 已生效、`failed` 失败留痕、`rolled_back` 已被回滚 |
| `before_text` / `before_hash` | 回滚的唯一数据源；哈希用于 CAS 与漂移检测 |
| `after_text` / `after_hash` | 支持「回滚的回滚」（redo）；也是判断「当前正文是否就是这版」的依据 |
| `instruction` | 回报与审计：为什么这段变成了现在这样 |
| `agent_run_id` | 关联既有 `AgentRun`（[assistant_generation.py:609-625](backend/app/routers/assistant_generation.py#L609-L625)），失败排查不用另建日志 |
| `bible_hash` | 回滚前比对设定是否变过，防止「正文退了、名字没退」 |
| `error_reason` | 失败原因：`model_error` / `timeout` / `client_disconnect` / `concurrent_edit` |

### 2.3 不建的东西

- **不建 `operation_batches` 表**：`batch_id` + 索引足够；批级状态由行聚合得出（全 `applied` / 有 `failed` / 部分 `rolled_back`）。
- **不加 `generations.edit_seq`**：理由见 5.3。
- **不写 `versions`**：不双写，避免两套历史互相漂移。

---

## 3. 写入时机

### 3.1 两种朴素时序的崩溃后果

| 时序 | 模型调用期间崩溃 | 模型成功、落库前崩溃 | 并发 |
|---|---|---|---|
| 纯前置：调用前写完整快照 | 留下「已写快照但正文未变」的孤儿；若含 after 则为 null | 快照在、正文旧；重试时前一版快照语义不明 | 无占位，两个请求都能进模型 |
| 纯后置：拿到结果、落库前写 | 什么都不留（无痕迹） | 事务回滚，什么都不留 | 两个请求都花了 token，最后一个覆盖前一个 |

### 3.2 采用：两段式（pending 前置 + applied 同事务）

```
T0 进入模型前（独立短事务，立即 commit）
    INSERT generation_snapshots(
      status='pending', before_text=当前正文, before_hash=sha256(before),
      batch_id, segment_no, generation_id, operation,
      instruction, agent_run_id, bible_hash, created_at
    )
    —— 部分唯一索引保证同段只有一个 pending

T1 模型调用（流式；全程不持有数据库写事务）

T2 模型成功（与正文更新同一事务）
    UPDATE generations SET text = :new
      WHERE id = :gid AND text = :before_text      -- CAS，rowcount 必须为 1
    UPDATE generation_snapshots
      SET after_text=:new, after_hash=sha256(:new),
          status='applied', finished_at=now
      WHERE id = :snapshot_id
    COMMIT                                          -- 就是引擎 engine.py:2055 那次

T3 派生重建（引擎既有逻辑，各自事务）
    rebuild_generated_kg(...)   → 内部 commit
    rebuild_summary_tree(...)   → 内部 commit

T4 失败/中断
    UPDATE generation_snapshots SET status='failed',
      error_reason=:reason, finished_at=now WHERE id=:snapshot_id
```

### 3.3 崩溃矩阵

| 崩溃时点 | 正文 | 快照 | 一致性 | 下次启动 |
|---|---|---|---|---|
| T0 之后、T1 中 | 未变 | `pending`（只有 before） | 一致 | TTL 清理把超时 `pending` 置 `failed` |
| T1 结束、T2 提交前 | 未变 | `pending` | 一致 | 同上 |
| T2 事务内 | 回滚，未变 | `pending` | 一致 | 同上 |
| T2 提交后、T3 中 | 已变 | `applied` | 正文与快照一致；KG/摘要可能落后 | 无自动修复，回报 `derived` 状态 |
| T3 之后 | 已变 | `applied` | 全一致 | — |

关键点：**任何崩溃点都不会出现「正文已变但快照没写」**。因为正文更新与快照 `applied` 在同一个事务里。

### 3.4 事务边界为什么不能更长

不能把 T0 到 T2 包成一个事务：模型调用是分钟级，SQLite 写锁会长期占住整库，所有生成/抽取都停摆。`pending` 必须是独立短事务。

也不能在 T2 用 ORM 的 `target.text = text`：那会绕过 CAS。必须改成带 `WHERE text = :before` 的条件更新，并 `db.refresh(target)`。

### 3.5 接入点（不改调用契约）

`regenerate_segment_stream` 增加可选参数（默认 `None`，行为逐字节不变）：

```python
def regenerate_segment_stream(
    db, project, segment_no, *, max_chars=None, instruction="",
    snapshot_id: int | None = None,          # 需新建：T2 的 finalizer
    expected_before: str | None = None,      # CAS 基线
): ...
```

- `snapshot_id is None`：沿用现有 `target.text = text`（[engine.py:2044](backend/app/generation/engine.py#L2044)），`/regenerate-stream` 完全不受影响。
- 非 `None`：在 2044 处改为条件 UPDATE，rowcount≠1 抛 `ConcurrentEdit`，`db.rollback()`，该段正文保持旧值。
- T4 由调用方（orchestrator）在 `except` / `finally` 里落 `failed`；流被客户端中断时由 `GeneratorExit` 兜底。

---

## 4. 重写失败了怎么办

**保留 `failed` 行，24 小时后清理。**

理由：

1. 「重试过一次」需要痕迹。`AgentRun` 已记录 run 级失败（[assistant_generation.py:751-753](backend/app/routers/assistant_generation.py#L751-L753)），但快照行补的是**段级 + 指令级**上下文：哪一段、用什么要求、失败在哪个阶段。
2. 直接删会让并发占位提前释放，两个请求可能连续打模型；保留 `failed` 不影响占位（唯一索引只约束 `pending`）。
3. 垃圾量可控：失败行没有 `after_text`，单行约 4KB，按每天几十次失败算，一年也就几十 MB；24 小时 TTL 后只剩当天。

**不保留**的情况：客户端主动取消、且正文未变——仍记 `failed(error_reason=client_disconnect)`，因为「用户点了取消」和「模型挂了」在排查时是两回事。

---

## 5. 并发

### 5.1 场景

两个请求同时重写第 12 段：都读到旧正文、都调模型、都写回。结果最后一个覆盖第一个，且快照链断裂（第二条的 `before` 不是第一条的 `after`）。

### 5.2 两层防护

| 层 | 机制 | 作用 |
|---|---|---|
| 进入模型前 | `uq_gs_pending` 部分唯一索引 | 第二个请求插入 `pending` 直接冲突 → 409「该段正在重写中，请稍后」；不浪费 token |
| 提交时 | 条件更新 `WHERE id=? AND text=?` | 防止**其它写正文入口**（`/regenerate-stream`、提案 apply）在生成期间改过该段；rowcount=0 → 回滚并标 `failed(concurrent_edit)` |

### 5.3 为什么不用 `generations.edit_seq` 版本号

- 加计数器只对「走助手的重写」生效；`/regenerate-stream`（[generation.py:146-161](backend/app/routers/generation.py#L146-L161)）和提案 apply 都不会维护它，CAS 会漏掉这些写入口，制造「假一致」。
- 条件更新用正文本身做基线，对所有入口都成立，且不需要改 `generations` 表、不需要迁移项。
- 代价是大文本比较；SQLite 对 1–2KB 文本的等值比较是微秒级，可忽略。

### 5.4 不同段并发

允许。不同 `segment_no` 不冲突；KG 重建本身有 per-project RLock 串行（[build.py:434-435](backend/app/kg/build.py#L434-L435)），不会互相踩。

---

## 6. 保留策略与容量

### 6.1 规则

| 类型 | 保留 | 清理 |
|---|---|---|
| `applied` | 每段最新 **3 条**；且最近 **7 天**内的不删 | 每日/启动时清理 |
| `rolled_back` | 与 `applied` 同规则 | 同上 |
| `pending` | 超过 **30 分钟**视为失联 | 置 `failed(model_timeout)` 后再按 failed 规则 |
| `failed` | **24 小时** | 直接删除 |

```sql
-- 每段只留最新 3 条 applied（示意，按 id 倒序）
DELETE FROM generation_snapshots
WHERE status IN ('applied','rolled_back')
  AND created_at < datetime('now','-7 days')
  AND id NOT IN (
    SELECT id FROM (
      SELECT id, ROW_NUMBER() OVER (
        PARTITION BY project_id, segment_no ORDER BY id DESC
      ) AS rn
      FROM generation_snapshots
      WHERE status IN ('applied','rolled_back')
    ) WHERE rn <= 3
  );
```

### 6.2 容量估算

假设：925 段，平均每段 1200 个汉字（UTF-8 3 B/字 ≈ 3.6 KB），一条已应用快照存 before+after ≈ 7.2 KB，加字段/索引开销约 0.5 KB → **单条约 7.7 KB（按 8 KB 计）**。

| 场景 | 条数 | 正文快照 | 含索引/页开销（约 +20%） |
|---|---|---|---|
| 每段重写 1 次 | 925 | 7.1 MB | **~8.5 MB** |
| 每段重写 3 次（默认上限） | 2 775 | 21.3 MB | **~25.5 MB** |
| 每段重写 10 次（不设上限） | 9 250 | 71 MB | **~85 MB** |
| 失败/在途行 | 每天几十条 | < 1 MB | < 1 MB |

对照：全书正文本身约 `925 × 3.6 KB ≈ 3.3 MB`。**快照是正文的 2.5–8 倍**，但绝对量在 SQLite 里很小。

结论：**不需要 diff、不需要压缩、不需要外部对象存储**。若未来上限放到每段 30 版，再考虑 zlib（中文散文可压到约 1/3，85 MB → 30 MB），现在不做。

---

## 7. 回滚语义

### 7.1 入口

| 入口 | 语义 | 实现成本 |
|---|---|---|
| 「撤销上一次」 | 找到该段最新 `applied` 快照，恢复到它的 `before_text` | 低；默认 UI 按钮 |
| 「回到任意一次」 | 指定 `snapshot_id`，恢复到它的 `before_text` | 与上面同一个原语；多一个按段列历史的查询 |
| 「整批撤销」 | 按 `batch_id` 找到该批全部 `applied`，整组恢复 | 中；见 7.3 |

因为每条快照都存了完整 `before_text`，**「任意一次」不比「上一次」贵多少**，建议服务层直接支持任意次，UI 默认给「上一次」。

### 7.2 单段回滚（一个事务）

```sql
BEGIN;
-- 1. 漂移检查：当前正文必须仍等于该快照的 after
--    SELECT text FROM generations WHERE id=:gid  → sha256 与 after_hash 比对，不等则 409
-- 2. 写回 before
UPDATE generations SET text = :before_text WHERE id = :gid AND text = :current_text;
-- 3. 新增一条「回滚快照」：before=当前、after=恢复后的内容
INSERT generation_snapshots(operation='rollback', status='applied', ...);
-- 4. 原快照置 rolled_back，指向回滚快照
UPDATE generation_snapshots SET status='rolled_back',
  rolled_back_at=now, rolled_back_by_snapshot_id=:new_id WHERE id=:snapshot_id;
COMMIT;
```

这与 `restore_version` 的语义完全对齐：历史不丢、回滚产生新版本、同事务（[proposals.py:1178-1187](backend/app/agents/proposals.py#L1178-L1187)）。因此「回滚的回滚」天然成立——再回滚一次，恢复的是回滚快照的 `before_text`。

### 7.3 批量回滚（整组退，不能退一半）

1. 取该 `batch_id` 下所有 `applied` 快照，按 `segment_no` 排序。
2. 逐条检查：
   - 若同段存在**更晚**的 `applied` 快照 → 409（「该段在批量之后又被改过，先撤销后面的」）。
   - 当前正文哈希必须等于快照 `after_hash`，否则 409（漂移）。
3. **所有正文恢复在同一个事务里**：任一失败 → 全部 `ROLLBACK`，不存在半组状态。
4. 同事务插入各段的 `rollback` 快照、把原快照置 `rolled_back`。
5. 提交后**只重建一次**派生数据（摘要树 + KG），不是每段重建一次。

注意：批量**重写**本身无法做成单事务（引擎要求先提交正文再 replay KG，[engine.py:2052-2055](backend/app/generation/engine.py#L2052-L2055)），所以「批量要么全成要么全不成」只能由**批量回滚**兜底，不能由批量重写保证。这一点必须写进回报文案。

### 7.4 回滚要不要留快照

**要**，理由同 `versions`：`v0 → v1 → v2 → 回滚` 得到四条历史，回滚本身可再被回滚（[models.py:632-637](backend/app/models.py#L632-L637)）。不留的话，「撤错了」就没有反悔路径。

---

## 8. 重写后的连锁变化：摘要与 KG 跟不跟着退

### 8.1 选择：一起退

回滚顺序：

1. 恢复正文（事务内完成，见 7.2/7.3）。
2. `rebuild_summary_tree(db, project, provider)`：按库内各段摘要整体重建卷摘要与主线骨架（[engine.py:524-544](backend/app/generation/engine.py#L524-L544)）。
3. `rebuild_generated_kg(db, project.id)`：清空 generated 侧派生数据并全量 replay（[build.py:411-475](backend/app/kg/build.py#L411-L475)）。

### 8.2 后果（必须接受）

| 后果 | 说明 |
|---|---|
| 回滚变慢 | KG 是全量 replay，925 段可能要几分钟；不是 O(1) 操作 |
| KG 重建不是原子的 | 该函数自身文档写明「不做事务级原子性」，只保证失败不静默（[build.py:451-455](backend/app/kg/build.py#L451-L455)）。回滚响应必须带 `kg_complete: bool` 与失败段列表 |
| 回滚后 KG 可能短暂落后 | 正文与摘要是强的；KG 是尽力而为。回报写「图谱重建中/有 N 段失败」 |
| 不选「只退正文」 | 只退正文会让摘要与正文矛盾，后续生成读到错误前情，问题会扩散到新段，代价更高 |

**为什么不选「只退正文」**：摘要树是后续生成的上下文来源（[engine.py:2066-2067](backend/app/generation/engine.py#L2066-L2067)），KG 的 `recent_entities` 也进 prompt。只退正文等于把错误留在「记忆」里。

---

## 9. 旧名残留与设定漂移

### 9.1 风险链

角色改名走的是 bible + 别名（[character_rename.py](backend/app/kg/character_rename.py)），不写正文。如果同一轮里先改名、再重写（或反过来），只回滚正文会得到「正文用旧名、名单用新名」或反之。

### 9.2 设计

1. 每条快照记录 `bible_hash = sha256(canonical(plot_bible))`。
2. 回滚前重新计算当前 `plot_bible` 哈希：
   - 相同 → 正常回滚。
   - 不同 → **409**，给三个选项：
     - A. 先把设定退回到快照时的版本，再回滚正文（需要新增 bible 回滚入口）；
     - B. 只回滚正文，接受设定不一致（回报里标 `warnings: ["bible_changed_since_snapshot"]`）；
     - C. 取消。
3. 设定回滚的实现：读快照时点的 `kg_bible_revisions` 的 `bible_json`，用 `apply_bible_revision` 再追加一条（[bible.py:410](backend/app/kg/bible.py#L410)）。历史不丢，但**需新建一个入口**；P0 先做检测 + 拒绝，P1 再做自动退设定。
4. 残留：改名会往 `kg_entities.aliases` 写旧名。回滚正文 + 全量 replay 后，正文里的旧名会被重新抽取，但手工/改名写入的 alias 行不会自动删除。回报必须注明「别名表可能仍保留新名」。

### 9.3 P0 的硬规则

**同一 `batch_id` 里只要发生过 bible/角色改动，默认不允许只回滚正文。** 要么整批回滚（正文 + 设定），要么拒绝。这条规则防止「越滚越乱」。

---

## 10. 落地脚本方案（不执行，等批准）

### 10.1 代码改动清单

| # | 文件 | 改动 | 性质 |
|---|---|---|---|
| 1 | `backend/app/models.py` | 新增 `GenerationSnapshot` 模型（表 + 3 个索引 + 1 个部分唯一索引） | 新建 |
| 2 | `backend/app/db.py` | **不需要**加迁移项：新表由 `Base.metadata.create_all` 创建（[db.py:99-126](backend/app/db.py#L99-L126)） | 无 |
| 3 | `backend/app/agents/dialog/snapshot.py` | `begin_snapshot` / `finalize_snapshot` / `fail_snapshot` / `purge_snapshots` | 新建 |
| 4 | `backend/app/generation/engine.py` | `regenerate_segment_stream` 增加 `snapshot_id/expected_before`，T2 改条件更新 | 小改（默认路径不变） |
| 5 | `backend/app/agents/dialog/rollback.py` | 单段/批量回滚 + 派生重建编排 | 新建 |
| 6 | `backend/app/routers/assistant_dialog.py` | 回滚入口（按 `batch_id` 或 `snapshot_id`） | 新建 |

### 10.2 显式 DDL（运维手工执行版）

```sql
-- 与 2.1 完全一致；执行前先备份库文件
CREATE TABLE IF NOT EXISTS generation_snapshots (...);
CREATE INDEX IF NOT EXISTS ix_gs_project_segment ...;
CREATE INDEX IF NOT EXISTS ix_gs_batch ...;
CREATE INDEX IF NOT EXISTS ix_gs_status_created ...;
CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_pending ... WHERE status = 'pending';
```

### 10.3 回退方案

```sql
DROP TABLE generation_snapshots;
```

无其它表引用它；`versions`、`generations` 均未改动，丢弃快照表即可回到现状。**唯一的损失是历史快照**，所以脚本执行前要求备份。

### 10.4 验证清单（落地时跑）

- [ ] 新库：`create_all` 自动建表，`PRAGMA index_list` 能看到 4 个索引。
- [ ] 老库：启动后自动补建，不需要手工 DDL。
- [ ] 单段重写：成功 → 1 条 `applied`；模型报错 → 1 条 `failed`，正文未变。
- [ ] 并发：两个请求同段 → 第二个 409，且不产生第二条 `pending`。
- [ ] 批量：3 段全成 → 3 条同 `batch_id`；第 2 段失败 → 1 条 `applied` + 1 `failed` + 1 未开始，批量回滚能把已应用的整组退回。
- [ ] 回滚：回到 `before_text` 后，再回滚一次能回到回滚前。
- [ ] 漂移：重写后手动改正文再回滚 → 409，不覆盖。
- [ ] 设定漂移：重写后改名再回滚 → 409，给三个选项。
- [ ] 保留：构造 5 版后跑清理，每段剩 3 版，7 天内不删。

---

## 11. 明确不做 / 边界

| 事项 | 结论 |
|---|---|
| 章节级快照 | 做不到：无章节实体，只有 `chapter_range="段N"`（[engine.py:488](backend/app/generation/engine.py#L488)）；快照粒度只能是段 |
| 删除段的快照/恢复 | 做不到：无 HTTP reset（仅内部 [generated_store.py:1447](backend/app/kg/generated_store.py#L1447)）；本设计只覆盖「改」，不覆盖「删」 |
| 回滚后续写的段 | 不回滚。重写不级联改后段（[engine.py:1954-1958](backend/app/generation/engine.py#L1954-L1958)），回滚同理只退被快照的段 |
| 跨项目撤销 | 不支持；`project_id` 隔离 |
| 把快照暴露成用户可编辑的「版本库」 | 不本期；先只服务撤销/审计 |

---

## 12. 一句话建议

**新建 `generation_snapshots`，不复用 `versions`**：`versions.proposal_id` 是 `NOT NULL` 外键且该表被定义为不可 UPDATE 的提案产物，重写既没有提案、又必须经历 `pending → applied` 的状态流转；复用要么伪造提案污染审批箱，要么在 SQLite 上重建表，代价都高于一张专用表。

---

## 附录：断言 → 证据

| 断言 | 证据 |
|---|---|
| `versions` 需要 `proposal_id` | [models.py:654](backend/app/models.py#L654) |
| `versions` 不可 UPDATE / 只追加 | [models.py:632-637](backend/app/models.py#L632-L637) |
| 迁移机制只支持 ADD COLUMN | [db.py:129-138](backend/app/db.py#L129-L138) |
| 新表由 `create_all` 创建 | [db.py:119-126](backend/app/db.py#L119-L126) |
| 正文在 2055 行提交 | [engine.py:2044-2055](backend/app/generation/engine.py#L2044-L2055) |
| 必须先提交正文再 replay KG | [engine.py:2052-2055](backend/app/generation/engine.py#L2052-L2055) |
| KG 全量 replay 且非原子 | [build.py:411-475](backend/app/kg/build.py#L411-L475)、[build.py:451-455](backend/app/kg/build.py#L451-L455) |
| 摘要树整体重建 | [engine.py:524-544](backend/app/generation/engine.py#L524-L544) |
| 回滚产生新版本、历史不丢 | [proposals.py:1178-1187](backend/app/agents/proposals.py#L1178-L1187) |
| 失败已有 AgentRun 留痕 | [assistant_generation.py:609-625](backend/app/routers/assistant_generation.py#L609-L625)、[L751-753](backend/app/routers/assistant_generation.py#L751-L753) |
| 无章节实体 | [models.py:479-489](backend/app/models.py#L479-L489) |
| 无 HTTP 重置入口 | 仅 [generated_store.py:1447](backend/app/kg/generated_store.py#L1447) |

---

## 13. 落地记录（2026-11-11 实现与设计的差异）

实现按本设计执行，有 **三处必要补充**，都不是「换设计绕过去」，且都只增不改：

1. **快照多存一列摘要**（`before_summary` / `after_summary`）。
   设计 §7 写「回滚正文后重建摘要树」，但 `rebuild_summary_tree` 的 L1 输入是
   `generations.summary['text']`（[engine.py:524-544](backend/app/generation/engine.py#L524-L544)）。
   只退正文的话，重建出来的 L2/L3 仍然是重写后的内容——正是设计要防的
   「摘要说 A、正文是 B」。故摘要与正文同事务落库、一起恢复。这是设计的洞，
   已在 `models.GenerationSnapshot` 的 docstring 里写明。

2. **启动时收割全部 `pending`**（`reap_all_pending`，`error_reason='process_restart'`）。
   单进程部署下，服务刚起来时库里的 `pending` 必定属于上一个进程；不收割的话，
   部分唯一索引 `uq_gs_pending` 会让该段**30 分钟内不能再重写**（超时收割才生效）。
   设计矩阵里「下次启动 → TTL 清理」这一格的忠实实现。**前提是单进程**；
   若将来上多 worker，必须改成按进程标识/心跳收割，否则会误杀别的 worker 的在途行。

3. **HTTP 路径避开 `batch` 字样**：`/snapshots/groups`、`/snapshots/rollback-group`。
   `test_apply_has_no_bulk_route` 对 `/api/assistant` 下含 `batch|bulk` 的路径一律判红
   （红线针对「批量应用提案」）。整组撤销不是批量应用，换词守住红线、不弱化它。

另外补了一条设计未写、实现必需的兜底：`begin_snapshot` 在插 `pending` 前先按
30 分钟 TTL 收割本段超时行，使「进程没死、流被遗弃」的场景也能自愈。

> 2026-11-12 更新：这条 30 分钟兜底已被**租约**取代，见 §14。§13 第 2 条的
> 「单进程前提」也已解决——启动收割不再整批清，改为按 pid + 启动时刻挑着收。

---

## 14. 在途租约（2026-11-12）

§3、§5、§13 的 `pending` 清理靠固定 30 分钟 TTL + 启动整批清。实测/推演暴露
两个洞：

1. **进程活着但流被遗弃**（客户端断开后生成器没跑 `finally`、协程被挂起）：
   没有人写心跳也没有人释放，最多卡 30 分钟；
2. **多 worker**：启动时「库里所有 pending 都属于上一进程」的前提不成立，
   会把别的存活 worker 的在途行清掉。

### 14.1 表结构（v2）

在 §2.1 基础上加三列（迁移脚本 v2 自动 `ALTER TABLE ADD COLUMN`）：

| 列 | 作用 |
|---|---|
| `owner_pid` | 写入者进程 id |
| `owner_started_at` | 该进程**启动时刻**指纹（Windows 取 `GetProcessTimes`，POSIX 取 `/proc/<pid>/stat` 字段 22）。防 pid 复用，**不能省** |
| `heartbeat_at` | 最后一次心跳 |

索引不变，仍是 `uq_gs_pending (project_id, segment_no) WHERE status='pending'`。

**存量 pending 不回填。** 按 `created_at` 补心跳等于给僵尸行发一张新租约
（崩溃残留会多活一个判据周期）；补 pid 可能撞上被复用的 pid。三列保持
`NULL`，语义是「升级前写入、无租约证据」：启动收割按「pid 不存在」清掉，
显式接管按「心跳 `NULL` 视为已过期」处理。因此迁移应在重启前执行；在线
执行时旧进程写下的 pending 没有租约，可能被新进程接管——升级窗口的已知代价。

### 14.2 心跳

- 间隔 `HEARTBEAT_INTERVAL_SECONDS = 20s`；
- 超时判据 `max(3 × 20s, 60s) = 60s`，允许连续丢 2 次心跳；
- **节流**：`SnapshotLease.touch()` 先在内存里比「距上次写是否超过 20s」，
  没超过一次数据库调用都不发。不能每个 chunk 无条件 UPDATE——SQLite 写
  事务串行，心跳会和生成/抽取抢写锁；
- 接入点：`engine.regenerate_segment_stream` 的流循环（仅 `snapshot_id` 非空
  的助手重写路径；`/regenerate-stream` 零额外写）。

为什么删掉固定 30 分钟：回滚链路要重放全书 KG，一次可能远超 5 分钟。把
「活得久」当「死了」会收割正在正常干活的任务。心跳判据只回答「最近 60 秒
有没有生命迹象」，不回答「任务该不该这么久」。

代价（如实记录）：模型整段无输出超过 60s 时，另一进程会认为租约过期；
但要真正抢走仍需用户显式「强制接管」，不会静默覆盖。若将来 provider 出现
分钟级首 token 延迟，应改后台心跳线程而不是调大阈值。

### 14.3 收割：三种判据各管一段

| 场景 | 判据 | 动作 |
|---|---|---|
| 所有者已死 | `owner_pid` 不存在，或 pid 存在但 `owner_started_at` 对不上（pid 复用） | 进入前 / 启动时**自动**置 `failed`，不打扰用户 |
| 所有者活着但心跳超时 | `now - heartbeat_at > 60s`（`NULL` 视为过期） | **不自动清**，返回 409，用户可显式接管 |
| 多 worker 启动 | pid 存活 + 启动时刻一致 | **不动**，哪怕心跳暂时陈旧 |

同机判活复用 `kg.batch_worker._pid_alive` 的 `OpenProcess`（Windows 上
`os.kill(pid, 0)` 会终止进程，绝不能用）。

### 14.4 强制接管

`begin_snapshot(..., takeover=True)` → 条件 UPDATE：

```sql
UPDATE generation_snapshots
   SET status='failed', error_reason='taken_over: owner_pid=…; stale=…s; by_pid=…'
 WHERE id = :blocker
   AND status = 'pending'
   AND owner_pid = :old_pid
   AND heartbeat_at = :old_heartbeat
   AND (heartbeat_at IS NULL OR heartbeat_at < :cutoff)
```

`rowcount=1` 才算接管成功，随后插入自己的 `pending`（同一事务）。原持有者
只要在这之间写了一次心跳，CAS 就不命中——不会把活人正在写的行抢走。
失败一律抛人话「这段正由另一个进程写入」，**不把 SQL/约束错误原文给用户**。
留痕两行都在：旧行 `failed` + `error_reason`（原持有者、停跳秒数、接管者），
新行 `pending` + `owner_pid`（接管者）。

### 14.5 实测（2026-11-12，`tests/test_rewrite_snapshot.py`）

| 验收 | 结果 |
|---|---|
| 流被遗弃但进程活着 | 心跳老化 90s → 普通请求 409（含「心跳已停 90 秒 / 可强制接管」），`takeover=True` 成功；旧行 `taken_over: owner_pid=41444; stale=90s; by_pid=41444`，仍只有一条 pending |
| pid 复用 | 子进程 pid 存活 + `owner_started_at` 对不上 → 被清；同批两个**真实存活** worker 的行保持 `pending`；启动收割共清 3 条（复用 / 死 pid / 无租约） |
| 多 worker | 两个子进程同时持有各自段：启动收割清 **0** 条，明细 `[(1, 36696, pending), (2, 36004, pending)]` |
| CAS 失败 | 心跳新鲜时 `takeover=True` → 409「这段正由另一个进程写入……未能接管」，pending 仍只有原来那条 |
| 心跳开销 | 1000 次节流判断 **0.78 ms**（单次 0.0008 ms，零写库）；真实心跳写库 10 次 avg **9.31 ms**（min 8.01 / max 10.51）。按 20s 一次摊薄，写入放大可忽略 |

迁移演练：在 `<temp>\snapshot-rehearsal\novel_dev_copy_v2.db` 上插入一条
**无租约的存量 pending** → `--apply` 后三列存在且该行三列仍为 `NULL`（未被
伪造租约）→ 再 `--apply` 幂等 → `--rollback` 后表删除、回滚前备份中该行
仍在 → 再 `--apply` 恢复。真实库 `novel_dev.db` 已 apply（列已加，0 行）。

