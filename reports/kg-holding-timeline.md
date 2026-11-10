# 持有表第二条轴：故事时间（模型与 DDL）

状态：**模型与 DDL 已定稿，未在真库执行**；`kg_item_holding` 的列、约束、索引一行未改，58 行未动；写入代码未改。

- DDL 文件：`backend/migrations/2026-11-10-item-holding-v3/002_story_time_axis.sql`
- 现有表与入口：`backend/app/kg/generated_store.py`（`kg_item_holding` / `record_holding`）
- 只在 `:memory:` 里跑过 DDL 与约束用例，真库 `backend/data/generated.db` 未打开写入

---

## 0. 结论先行

1. **不给人 `kg_item_holding` 加列，单建一张旁注表 `kg_holding_story_segment`。** 不是因为「加列不方便」，是因为回溯段和老表的三个不变量**直接冲突**，加列解决不了任何一条（见 §2）。
2. 三档时间点**能**用同一套字段表达：差别只在 `story_group / precision / distance_known` 三列的取值，不需要三张表或三组列。
3. 排序键是**同一 `story_group` 内单调递增的整数**，不是事件 id，也不是自由文本年代标签。
4. 回溯段进旁注表，**不进章节轴**，所以 `_close_open_segment` 不该也不能被它触发——这是表结构保证的，不是约定。
5. 赠予句：持有段取**受赠方**，给出方落 `transfer_from_id / transfer_from_name`；`transfer_event_id` 继续留空（v3 没有整数事件 id）。
6. 本书只有**粗粒度**的绝对标尺：229 章里 10 章出现年号，其中只有 2 章标定「当前故事年」。所以结论是**只能做到「绝对锚点 + 相对序 + 叙述序」三层并存**，不给每章硬编序号。

---

## 1. 时间点是什么：三档方案与代价

| 档位 | 正文依据（本书实例） | 落到哪几列 | 跨段可比性 | 距离 |
|---|---|---|---|---|
| 绝对 | 「元景三十六年」ch7、「元景六年」ch202、「贞德二十九年」ch332 | `story_group='dafeng'`、`story_from=<纪元编码>`、`precision='year'/'month'/'day'` | 同一纪元内可比；跨纪元只可比先后 | 同纪元内可算；跨纪元不可算 |
| 相对 | 「开国之初」ch95、「十九年前」ch1、「三天前」ch2 | `story_group='dafeng'`（纪元级）或 `'ch:95'`（章内）、`precision='era'/'order'`、`distance_known=0` | 只在同一 `story_group` 内可比 | 不可算 |
| 只有叙述序 | 神剑 ch122「用镇压气运的神剑，辅以法阵封印」——只说「这里讲到」，没给先后 | `story_group=NULL`、`story_from=NULL`、`precision='unknown'` | 不可比 | 不可算；读侧退回 `evidence_chapter` |

### 1.1 一套字段够不够

够。三档的差别全部落在三个字段的组合上，不需要为每档开一组列：

- `story_group`：标尺身份。`NULL` = 没有标尺。
- `story_from / story_to`：该标尺内的整数坐标；`GROUP` 不同则**禁止比较**，这是读侧纪律，不靠字段保证。
- `precision`：`era / year / month / day / moment / order / unknown`。
- `distance_known`：`1` 表示两个坐标的差值有意义（同纪元、同章内），`0` 表示只知先后。
- `anchor_label`：原文写法，保留可审计性（「元景三十六年」「开国之初」）。
- `anchor_ref_chapter`：相对序的参照章（「三天前」相对第 2 章）；绝对序为 `NULL`。

### 1.2 排序键取什么

取**存储下来的整数**，由文本坐标按固定映射算出来，映射常量版本化。理由：

- **不能用事件 id**：本语料 `kg_events` 是 0 行（`SELECT COUNT(*) FROM kg_events` = 0）；v3 事件只活在抽取缓存里，重抽会换号，做键会一重抽就错位。
- **不能用自由文本年代**：`'元景三十六年'` 与 `'元景36年'` 是同一坐标的两种写法，直接字符串比较会把 36 排到 6 前面。
- **大奉历编码**（写进 DDL 注释，代码里只允许一处实现）：

  ```
  era_base: 开国 0 / 贞德 1_000_000 / 元景 2_000_000
  key = era_base + year*10000 + month*100 + day
  ```

  例：开国之初 = `0`；贞德二十九年 = `1_290_000`；元景六年 = `2_060_000`；元景三十六年 = `2_360_000`。

  纪元的先后有正文直证（ch332「贞德29年的进士，元景14年，不知因何被贬」、ch202「元景六年，独孤老将军逝世」），但**两纪元之间隔多久正文没说**，所以跨纪元 `distance_known=0`。这就是为什么坐标差不能直接当距离用。

### 1.3 「先后已知、距离未知」怎么存

同一 `story_group`、递增的 `story_from`、`distance_known=0`。读侧只允许 `ORDER BY story_from`，**禁止做减法**。例：开国（0）与元景六年（2_060_000）数值差 206 万，实际差几代人都不知道，`distance_known=0` 就是把「别拿这个数算时间」写进数据。

---

## 2. 归哪张表

### 2.1 两个选项

| | A. 给 `kg_item_holding` 加列 | B. 单建旁注表（选这个） |
|---|---|---|
| 加约束 | `ALTER TABLE ADD COLUMN` **加不了 CHECK，也加不了表级 UNIQUE** | 新表可以写全套 CHECK + 两个部分唯一索引 |
| 动 58 行 | 加列不改值；但想要约束就必须重建表，12 步复制 58 行、重建 4 个索引、搬 `AUTOINCREMENT` 序列 | 不动老表，58 行零影响 |
| 回溯段 | 装不下（见 2.2） | 装得下 |
| 读路径 | 一条查询 | 两条查询 / `UNION`，靠 `holding_id` 拼 |
| 冗余 | 无 | 旁注行与持有行有 owner 字段冗余 |

### 2.2 决定性理由：回溯段与老表的三条不变量直接冲突

把 ch95「大奉开国皇帝得了神剑」写成 `valid_from=95` 的行，会同时撞三堵墙：

1. `CHECK (valid_to IS NULL OR valid_to > valid_from)`：同一章内结束的回溯段只能是零长度段，被 CHECK 拒。
2. `ux_item_holding_open (COALESCE(item_id,-1), item_name, hold_type) WHERE valid_to IS NULL`：同一物品同一 `hold_type` 只能有一条开放段。回溯段要开放就撞索引；要关闭就得给 `valid_to`，又回到第 1 条。
3. `UNIQUE (item_id, hold_type, valid_from)`：第 95 章若已有主线段的 `valid_from=95`，回溯段直接撞唯一键。

**加列一条都解决不了**——冲突在结构上，不在字段上。所以只能另开一张表。

### 2.3 选 B 的代价（要认）

- 时间线要两次查询（章节轴 + 故事轴）再在应用层拼；好在两表同库同连接，可以用 `LEFT JOIN ... ON s.holding_id = h.id` 一次取完。
- 旁注行与持有行有 `item_name / owner_* / hold_type` 冗余。写入口只有 `record_holding`，没有第二条更新路径，漂移风险低；测试里要加一条对账查询（同 `holding_id` 的两行这些字段必须相等）。
- id 不共享。跨轴拼接的唯一键是 `holding_id`，不是 `id`。

### 2.4 与 v3 事件表的关系

- `kg_events` 在 `novel_dev.db` 里**是空表**（0 行），`in_story_date` / `time_anchor_id` 两列从来没有数据。所以本轮**不建 FK、不共享主键**。
- 抽取缓存里 5907 条事件、1409 条有 `in_story_date`，但带年号的只有 **12 条**（7 章）。它只能当**抽取线索**，不能当排序键。
- 将来若真灌 `kg_events`，交叉校验用 `(chapter, sequence)`，不要用自增 id。

### 2.5 跨库读

两张表都在 `generated.db`，同库同连接，`JOIN` 没有障碍。只有要拿 `novel_dev.db` 的正文/实体时才有跨库问题：在生成层连接上 `ATTACH DATABASE 'novel_dev.db' AS kg`，代价与限制沿用 `reports/kg-item-holding.md` §8（连接生命周期、无外键、快照可见性、相对路径）。本轮不新增跨库依赖。

---

## 3. 最终 DDL

完整文件：`backend/migrations/2026-11-10-item-holding-v3/002_story_time_axis.sql`。核心部分：

```sql
CREATE TABLE IF NOT EXISTS kg_holding_story_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER,              -- 主线旁注 → kg_item_holding.id；回溯分支为 NULL
    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),
    owner_id INTEGER, owner_name TEXT, owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),
    story_group TEXT,                -- 'dafeng' | 'ch:95' | NULL
    story_from INTEGER,
    story_to INTEGER,
    precision TEXT NOT NULL DEFAULT 'unknown'
        CHECK (precision IN ('era','year','month','day','moment','order','unknown')),
    distance_known INTEGER NOT NULL DEFAULT 0 CHECK (distance_known IN (0,1)),
    anchor_label TEXT NOT NULL DEFAULT '',
    anchor_ref_chapter INTEGER,
    narration TEXT NOT NULL DEFAULT 'forward'
        CHECK (narration IN ('forward','retro','prefigure')),
    transfer_from_id INTEGER,
    transfer_from_name TEXT,
    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,

    CHECK ((story_group IS NULL) = (story_from IS NULL)),
    CHECK (story_to IS NULL OR story_to > story_from),
    CHECK ((precision = 'unknown') = (story_from IS NULL)),
    CHECK (COALESCE(story_group,'') <> 'dafeng'
           OR precision IN ('era','year','month','day','moment')),
    CHECK (COALESCE(story_group,'') NOT LIKE 'ch:%'
           OR precision IN ('moment','order')),
    CHECK (narration = 'forward' OR holding_id IS NULL),
    CHECK (
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR (owner_state IN ('unowned','sealed','unknown') AND owner_name IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_holding
    ON kg_holding_story_segment (holding_id) WHERE holding_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id,-1), item_name, hold_type,
        COALESCE(story_group,''), COALESCE(story_from,-1), COALESCE(story_to,-1))
    WHERE holding_id IS NULL;

CREATE INDEX IF NOT EXISTS ix_story_segment_item
    ON kg_holding_story_segment (item_id, story_group, story_from);
CREATE INDEX IF NOT EXISTS ix_story_segment_owner
    ON kg_holding_story_segment (owner_id, story_group, story_to);
```

内存库验证结果（`:memory:`，不碰真库）：

- DDL 连续执行两次通过（幂等）。
- 8 类插入用例：正常行通过；重复分支被 `ux_story_segment_branch` 拒；回溯挂主线被 `CHECK` 拒；有精度无标尺被拒；零长度被拒。

---

## 4. SQLite 硬约束复核（原 8 条逐条）

| # | 原结论 | 新轴下的状态 | 处理 |
|---|---|---|---|
| 1 | `UNIQUE(item_id, hold_type, valid_from)` 对 `item_id IS NULL` 失效 | 不变 | 新表用 `COALESCE(item_id,-1)` 补；老表不动 |
| 2 | 段间不重叠 / 首尾相接无法用 CHECK 表达 | 不变，新表同样表达不了 | 只能靠 `record_holding` + 校验 SQL；单行内 `story_to > story_from` 可落 CHECK |
| 3 | `owner_kind` 必须落在 `kg_entities.kind`，跨库，无枚举 | 不变 | 保持不约束 |
| 4 | `owner_id` 与 `owner_name` 的同一性无法约束 | 不变 | 保持不约束 |
| 5 | `unowned ⇒ owner_id IS NULL` 未加 CHECK | 不变 | 新表与老表**同口径**，不偷偷加严 |
| 6 | `transfer_event_id` 无 FK | 新表 `holding_id` 也无 FK | 连接没开 `PRAGMA foreign_keys`，加 FK 只会造成假安全感 |
| 7 | `valid_from/valid_to` 无法校验章号范围、与 `evidence_chapter` 的关系 | 不变 | 新增的 `story_to > story_from` 是唯一能落的那条 |
| 8 | `reason_code` 自由文本 | **改善** | 新表不用自由文本，改用 `precision` / `narration` 两个枚举 |

### 4.1 `ALTER TABLE ADD COLUMN` 的取舍

**不做重建。** 原因：

- 加列本身能加，但**加不了 CHECK 和表级 UNIQUE**；要约束就得走 SQLite 的 12 步重建：建新表 → 复制 58 行 → 删旧表 → 改名 → 重建 4 个索引 → 搬 `sqlite_sequence`。任一步失败都要靠备份回滚。
- 重建的收益是零：即使重建完，回溯段仍然装不进去（§2.2）。
- 现有连接池/其他读句柄在 WAL 下持有旧表句柄，重建期间的行为要额外验证，风险和收益不成比例。

所以：**老表一个字不改；新约束全部落在新表上。**

### 4.2 现有 `UNIQUE` 与 `ux_item_holding_open` 还成不成立

- 都成立，且**继续有效**。新轴不往 `kg_item_holding` 写行，所以不存在「新轴把它撞坏」的情况。
- 「同一物品同一章有两条不同故事时间的段」：
  - 章节轴上仍然**不允许**——同章不同持有者继续走「同章竞争 → 候选」，语义不变。
  - 故事轴上**允许**，靠 `ux_story_segment_branch` 按 `(物品, hold_type, 标尺, from, to)` 去重，同一坐标不许写两条。
- 一条章节轴段最多一条旁注（`ux_story_segment_holding`）。要两个故事位置就写两条段，而不是给同一段挂两条旁注。

### 4.3 哪些能落约束、哪些只能靠写函数

能落约束：

- 单段内 `story_to > story_from`
- `story_group` 与 `story_from` 同生同灭
- `precision` 与 `story_group` 配对
- 回溯 / 预叙不得挂主线（`holding_id` 必须为空）
- 持有者三态口径
- 分支行去重、一条主线一条旁注

只能靠写函数（和测试）：

- 跨段的单调、不重叠、首尾相接
- `distance_known` 的真实性（正文有没有说距离，数据库看不出来）
- 旁注行与持有行的 owner 一致
- `transfer_from_*` 只在确有转手动作时写
- 同一 `story_group` 的坐标必须来自同一套映射（没有全局注册表）

---

## 5. 写入规则（落在 `record_holding` 里）

`record_holding` 增加一组 keyword-only 参数，全部有默认值，老调用行为**逐字段不变**：

```python
record_holding(con, item_name, hold_type, owner_name, chapter, *,
               # …原有参数不动…
               story_group=None, story_from=None, story_to=None,
               precision="unknown", distance_known=0, anchor_label="",
               anchor_ref_chapter=None, narration="forward",
               transfer_from_id=None, transfer_from_name=None)
```

| 规则 | 条件 | 行为 |
|---|---|---|
| R1 | `narration='forward'` 且无故事坐标 | 完全走现有逻辑，不碰旁注表（58 行与今天一致） |
| R2 | `narration='forward'` 且有故事坐标 | 走现有逻辑写章节轴，成功后写一条 `holding_id=<新行 id>` 的旁注 |
| R3 | `narration in ('retro','prefigure')` | **只写旁注表**，`holding_id=NULL`，**不调用** `_close_open_segment`，不碰 `kg_item_holding`；返回 `{"action":"inserted","axis":"story","closed":0,"reason":"retro_branch"}` |
| R4 | 回溯段与主线在故事序上冲突 | 不拒写、不改 `valid_to`；写一条 `gen_candidates(reason='story_order_conflict')` 留证据 |
| R5 | `transfer_from_*` 有值 | 仅在该段是「一次转手的落点」时写；`transfer_event_id` 继续留空 |
| R6 | 旁注行 owner 字段 | 与对应持有行同值写入；测试里有对账断言 |

### 5.1 `_close_open_segment` 该不该关段

**不该，也不能。** 它只接受 `(table, key, chapter)`，对 `kg_item_holding` 生效；回溯段在另一张表，物理上到不了它。这就是选独立表的第二个收益：不需要给关闭函数加 `if narration=='retro'` 这类旁路判断——那种判断迟早会漏。

### 5.2 两个悬着口径的结论

**（1）回溯叙述算不算持有：算，但与按章连续性分开。**

- 进 `kg_holding_story_segment`，`narration='retro'`，`holding_id=NULL`，带 `story_group/story_from`。
- 不参与章节轴连续性判断（不在老表里，天然不参与）。
- **不许覆盖主线段的 `valid_to`**：因为不调用关闭函数，结构上做不到。
- 若回溯坐标与主线旁注矛盾，只记候选 `story_order_conflict`，不裁决。
- 落得下，没有卡点。卡点只会在「硬塞进老表」时出现（§2.2）。

**（2）赠予句的持有者取谁：取受赠方。**

- ch99「元景帝请出神剑，赠予镇北王」→ 章节轴新段 `owner=镇北王`，`valid_from=99`；旁注行写 `transfer_from_id/name=元景帝`，`narration='forward'`。
- 不能像现在这样把赠予方写成持有者。判据是句子的**动作落点**：`赠予` 的宾语是剑，受益方是镇北王，元景帝是**给出方**。
- `transfer_event_id` 保持 `NULL`：v3 没有整数事件 id，`ch74#7` 这种 ref 不能塞进 INTEGER 列。

---

## 6. 覆盖率估计（证据）

| 口径 | 数量 | 说明 |
|---|---|---|
| v3 覆盖章 | 229（1–229） | 缓存 `ch001..ch229` |
| 1–229 章里有年号的章 | **10**（6,7,11,19,104,186,189,192,195,202） | 正则 `元景/贞德 + 数字 + 年` |
| 其中标定「当前故事年」的章 | **2**（ch7「现在是元景36年」、ch192「元景初年至元景三十六年」） | 其余 8 章是卷宗/履历/追述 |
| 缓存事件总数 | 5907 | 229 章 |
| 事件有 `in_story_date` | 1409（23.9%） | 多为章内相对时刻（卯时二刻、三天前） |
| 事件带年号 | **12**（0.2%），7 章 | 其余 1397 条只在章内可比 |
| 全书（925 章）含年号的 chunk | 55 | 占比同量级 |

结论：

- **绝对档覆盖 10/229 章 = 4.4%**，只能当锚点，不能当每章落点。
- **相对档覆盖约 24% 的事件**，但只在同一 `story_group` 内可比（同一章或同一纪元）。
- **叙述序兜底 100%**（`evidence_chapter`）。
- 不硬编序号。神剑那条线里，ch95 能落「开国」这个纪元锚，ch99 只能落叙述序 + 转手方，这是正文决定的，不是设计偷懒。

---

## 7. 小样验证（DDL 确认后执行，本轮未跑）

在 `generated.db` 的**临时副本**上执行（真库不开写）：

| 组 | 输入 | 期望 |
|---|---|---|
| 神剑 · 回溯 | ch95「玄武奉上神剑」「开国皇帝得了神剑」 | 旁注分支：`owner=大奉开国皇帝`、`story_group='dafeng'`、`story_from=0`、`precision='era'`、`distance_known=0`、`narration='retro'`、`holding_id=NULL`；章节轴 0 行新增 |
| 神剑 · 赠予 | ch99「元景帝请出神剑，赠予镇北王」 | 章节轴新段：`owner=镇北王`、`valid_from=99`；旁注：`transfer_from_name=元景帝`、`narration='forward'`、`holding_id=<新段 id>` |
| 神剑 · 地点 | ch97「庙里供奉着的神剑」 | 不写持有（`located_at` 不是持有者），章节轴与旁注表都不新增 |
| 地书 · 回归 | ch73/74 四条现有行（id 12–15） | 加轴前后 `SELECT *` **逐字段相等**；旁注表 0 行 |

验收查询：

```sql
-- 1. 老表 58 行逐字段不变（加轴前 dump 与加轴后比对）
SELECT * FROM kg_item_holding ORDER BY id;
-- 2. 神剑两轴
SELECT h.*, s.owner_name AS story_owner, s.story_group, s.story_from,
       s.precision, s.narration, s.transfer_from_name
  FROM kg_item_holding h
  LEFT JOIN kg_holding_story_segment s ON s.holding_id = h.id
 WHERE h.item_name = '神剑';
SELECT * FROM kg_holding_story_segment WHERE holding_id IS NULL AND item_name = '神剑';
-- 3. 旁注与持有行对账（必须 0 行）
SELECT s.id FROM kg_holding_story_segment s
  JOIN kg_item_holding h ON h.id = s.holding_id
 WHERE s.item_name <> h.item_name OR s.hold_type <> h.hold_type
    OR COALESCE(s.owner_name,'') <> COALESCE(h.owner_name,'');
```

---

## 8. 本轮没做

- 没有在 `generated.db` 执行任何 DDL；58 行未动。
- 没有改 `record_holding` / `_close_open_segment` 的代码（规则见 §5，等 DDL 确认后单独提交）。
- 没有重抽、没有补边、没有碰 `kg_relations`。
- 没有为「章内相对序」建标尺注册表：`ch:95` 这类链目前只约定命名，不预生成。
