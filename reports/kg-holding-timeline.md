# 持有表第二条轴：故事先后（模型、DDL、小样）

状态：**只排先后，不记绝对时间**。真库 `generated.db` 未写，58 行未动；老表 DDL 在副本上逐字未变。

- DDL：`backend/migrations/2026-11-10-item-holding-v3/002_story_time_axis.sql`
- 代码：`backend/app/kg/generated_store.py`（`kg_holding_story_segment` / `record_story_segment` / `current_holder`）
- 小样脚本：`backend/migrations/2026-11-10-item-holding-v3/verify_story_axis.py`

---

## 0. 口径改小后的结论

| 项 | 结论 |
|---|---|
| 轴的性质 | 只排先后，不记距离、不记绝对时间 |
| 字段档位 | 两档：`order_basis='explicit'`（正文看得出先后）/ `'chapter'`（看不出，退章节序） |
| 排序键 | 同一物品内单调的整数，越大越新；下落章节序，上插显式先后 |
| 砍掉的字段 | `story_group` / `story_from` / `story_to` / `precision` / `distance_known` / `anchor_ref_chapter` |
| 保留的 | `order_key` / `order_basis` / `anchor_label`（原文依据）/ `narration`（回溯标记） |
| 老表 | 一个字不改，58 行不动 |
| 当前持有者 | 每类持有线取「最新先后键」那一段，不按置信度 |
| 冲突 | `story_order_conflict` 只留证据，不作裁决依据 |

“距离”这件事彻底不做：没有年份、没有区间长度、没有跨纪元换算。年号只作为 `anchor_label` 的原文证据存在，缺了不补、不推断。

---

## 1. 两档字段

| 档 | 判定 | 落法 | 读侧 |
|---|---|---|---|
| 有相对序 | 正文能看出「在……之前 / 之后 / 之初 / 当年」 | `order_basis='explicit'`、`order_key=<整数>` | 直接按 `order_key` 排 |
| 只有叙述序 | 正文什么都没说，只能记「在第 N 章被讲到」 | `order_basis='chapter'`、`order_key=NULL` | 退到 `evidence_chapter * 100` |

原来那三档（绝对 / 相对 / 叙述）压成两档，因为「绝对」只是「相对序」里一个更精确的来源：年号给出的是坐标，而我们现在只需要顺序。保留第三档只会多两个永远为空的字段。

`narration`（`forward` / `retro` / `prefigure`）保留：它不是时间字段，是**叙述关系**字段，用来标记「这段不在章节主线上」，写入口据此禁止它触发关段。

---

## 2. 排序键怎么定

### 2.1 刻度

```
章节序键 = chapter * 100            （显式先后也用同一刻度，插空）
例：ch99 → 9900；「开国之初」→ 0；介于 ch50 与 ch51 之间 → 5050
```

为什么不是事件 id：本语料 `kg_events` 是 0 行，v3 事件只活在抽取缓存里，重抽会换号。

为什么不是年份：全书 229 章只有 10 章出现年号，且多数是追述；拿它当主键会把 90% 的段挤到同一个值上。

为什么 `*100`：给同一章内的多个位置留插槽（slot 0–99）。这个键**只表示先后**，两个键相减没有时间含义——`distance_known` 这类字段因此被砍掉。

### 2.2 排序规则

```
order_effective = COALESCE(order_key, evidence_chapter * 100)
按 order_effective 升序 = 从早到晚；当前持有者取最大的那一段
```

### 2.3 「先后已知、距离未知」

同一条线上放两个递增的 `order_key` 就行，读侧只排序。不需要额外字段表达「距离未知」，因为**默认就不算距离**。

---

## 3. 归哪张表（结论未变）

单建旁注表 `kg_holding_story_segment`，不给老表加列。原因不变：回溯段与老表三条不变量直接冲突，加列解决不了。

1. `CHECK (valid_to > valid_from)`：同章内的回溯段只能是零长度，被拒。
2. `ux_item_holding_open`：同一物品同一 `hold_type` 只能一条开放段，回溯段开放就撞。
3. `UNIQUE (item_id, hold_type, valid_from)`：同章已有主线段时直接撞唯一键。

代价照旧：时间线要两段查询再拼；旁注与持有行有 owner 冗余（写入口唯一，测试里有对账）。

---

## 4. 最终 DDL（简化版）

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

    order_key INTEGER,               -- 同物品内可比，越大越新；NULL = 退章节序
    order_basis TEXT NOT NULL DEFAULT 'chapter'
        CHECK (order_basis IN ('explicit', 'chapter')),
    anchor_label TEXT NOT NULL DEFAULT '',   -- 原文依据：「开国之初」「元景三十六年」
    narration TEXT NOT NULL DEFAULT 'forward'
        CHECK (narration IN ('forward', 'retro', 'prefigure')),

    transfer_from_id INTEGER,
    transfer_from_name TEXT,

    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,

    CHECK ((order_basis = 'explicit') = (order_key IS NOT NULL)),
    CHECK (order_key IS NULL OR order_key >= 0),
    CHECK (narration = 'forward' OR holding_id IS NULL),
    CHECK (   -- 与老表同口径
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR (owner_state IN ('unowned','sealed','unknown') AND owner_name IS NULL)
    ),
    CHECK (transfer_from_name IS NULL OR length(trim(transfer_from_name)) > 0)
);

CREATE UNIQUE INDEX ux_story_segment_holding
    ON kg_holding_story_segment (holding_id) WHERE holding_id IS NOT NULL;
CREATE UNIQUE INDEX ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id, -1), item_name, hold_type, COALESCE(order_key, -1))
    WHERE holding_id IS NULL;
CREATE INDEX ix_story_segment_item
    ON kg_holding_story_segment (item_id, order_key);
CREATE INDEX ix_story_segment_owner
    ON kg_holding_story_segment (owner_id, order_key);
```

列数从 24 降到 18；约束从 7 条降到 5 条，但没有一条是「宽松掉的」——砍掉的三条（区间自洽、标尺-精度配对、标尺-精度白名单）随字段一起消失，剩下的仍然全在。

---

## 5. SQLite 硬约束复核（原 8 条）

| # | 原结论 | 现在 |
|---|---|---|
| 1 | `UNIQUE(item_id, hold_type, valid_from)` 对 `item_id IS NULL` 失效 | 不变；新表分支索引用 `COALESCE(item_id,-1)` 补上 |
| 2 | 段间不重叠 / 首尾相接无法用 CHECK 表达 | 不变；新表只有单键，不涉及区间 |
| 3 | `owner_kind` 跨库，无枚举 | 不变 |
| 4 | `owner_id` / `owner_name` 同一性无法约束 | 不变 |
| 5 | `unowned ⇒ owner_id IS NULL` 未加 CHECK | 与老表同口径，没有偷偷加严 |
| 6 | `transfer_event_id` 无 FK | 新表 `holding_id` 也不加 FK（连接没开 `PRAGMA foreign_keys`） |
| 7 | `valid_from` 无法校验章号范围 | 新表不存章区间，只存章节序兜底值 |
| 8 | `reason_code` 自由文本 | 新表改用 `order_basis` / `narration` 两个枚举 |

`ALTER TABLE ADD COLUMN` 的取舍照旧：加不了 CHECK / 表级 UNIQUE，要约束就得 12 步重建（复制 58 行、重建 4 个索引、搬 `sqlite_sequence`），而且重建完回溯段仍然装不进去。**不做重建。**

---

## 6. 写入规则

`record_story_segment` 是 `kg_holding_story_segment` 的**唯一写入口**；老表仍只走 `record_holding`。

```python
record_story_segment(con, item_name, hold_type, owner_name, chapter, *,
                     item_id=None, owner_id=None, owner_kind=None,
                     owner_state="held", holding_id=None, narration="forward",
                     order_key=None, order_basis=None, anchor_label="",
                     transfer_from_id=None, transfer_from_name=None,
                     evidence="", confidence=0.5)
```

| 规则 | 行为 |
|---|---|
| 主线旁注 | `narration='forward'` 必须给 `holding_id`；物品 / 持有者 / 状态必须与持有行一致，否则 `ValueError` |
| 回溯 / 预叙 | `holding_id` 必须为空；不碰 `kg_item_holding`、不调用 `_close_open_segment` |
| 两档一致性 | `explicit` ⇔ `order_key` 非空；`chapter` ⇔ `order_key` 为空；由 CHECK 兜底 |
| 冲突 | 回溯键不早于主线最新键时，写一条 `gen_candidates(reason='story_order_conflict')`，**照常插入** |
| 幂等 | 同一 `holding_id` 重复写 → `confirmed`；同一分支坐标重复写 → `confirmed` |
| 赠予 | 持有段取受赠方；给出方写 `transfer_from_name`；`transfer_event_id` 继续留空 |

`_close_open_segment` 不动：回溯分支在另一张表，物理上触发不到它。

---

## 7. 小样结果（临时副本，不推广）

副本：`<temp>\kg_story_axis_copy.db`（真实库只读复制，**未入库**）。

### 7.1 神剑

写入前：章节轴 0 行。

| 写入 | 结果 |
|---|---|
| ch95 开国皇帝得剑（追述） | `kg_holding_story_segment` id=1，`holding_id=NULL`，`order_key=0`，`order_basis='explicit'`，`anchor_label='开国之初'`，`narration='retro'` |
| ch99 元景帝赠予镇北王 | 章节轴 id=59：`owner=镇北王`、`valid_from=99`、`valid_to=NULL`；旁注 id=2：`holding_id=59`、`order_basis='chapter'`（键 NULL）、`transfer_from_name='元景帝'` |
| ch97 庙供奉 | **不写行**（`located_at` ≠ 持有），神剑章节轴仍为 1 行 |

实际行：

```
章节轴:
{id:59, item_name:神剑, hold_type:own, owner_name:镇北王, owner_state:held,
 valid_from:99, valid_to:None, transfer_event_id:None}

先后轴:
{id:1, holding_id:None, owner_name:大奉开国皇帝, order_key:0,  order_basis:explicit,
 anchor_label:开国之初, narration:retro,   transfer_from_name:None,   evidence_chapter:95}
{id:2, holding_id:59,   owner_name:镇北王,       order_key:None, order_basis:chapter,
 anchor_label:'',       narration:forward, transfer_from_name:元景帝, evidence_chapter:99}
```

### 7.2 当前持有者

查询（等价于 `store.current_holder`）：

```sql
WITH seg AS (
  SELECT h.item_name, h.hold_type, h.owner_name, h.owner_state,
         h.valid_from AS chapter,
         COALESCE(s.order_key, h.valid_from * 100) AS ord
    FROM kg_item_holding h
    LEFT JOIN kg_holding_story_segment s ON s.holding_id = h.id
   WHERE h.item_name = :item
  UNION ALL
  SELECT s.item_name, s.hold_type, s.owner_name, s.owner_state,
         s.evidence_chapter,
         COALESCE(s.order_key, s.evidence_chapter * 100)
    FROM kg_holding_story_segment s
   WHERE s.holding_id IS NULL AND s.item_name = :item
)
SELECT item_name, hold_type, owner_name, owner_state, chapter, ord
  FROM (SELECT seg.*, ROW_NUMBER() OVER (
              PARTITION BY hold_type ORDER BY ord DESC, chapter DESC) AS rn
          FROM seg)
 WHERE rn = 1;
```

实际返回：

```
神剑 → [{hold_type:'own', owner_name:'镇北王', chapter:99, ord:9900, narration:'forward'}]
地书 → [{hold_type:'custody', owner_name:'许七安', chapter:74, ord:7400},
        {hold_type:'own',     owner_name:'天地会', chapter:74, ord:7400}]
```

- 神剑：**能给出唯一答案**——`own` 一条，镇北王。开国皇帝那条回溯分支键为 0，排在前面，不影响「最新」。
- 地书：两条线各自唯一——权属天地会、保管许七安。

注意：神剑的答案「唯一」只针对**表内已有的段**。ch106「元景帝持剑」、ch108 等尚未进表，所以这里的唯一不等于「全书正确」。这是数据覆盖问题，不是排序口径问题。

### 7.3 地书回归

4 行（id 12–15）写入前后 `SELECT *` 逐字段一致：`True`。`own`：地宗 `[73,74)` → 天地会 `[74,∞)`；`custody`：金莲师兄 `[73,74)` → 许七安 `[74,∞)`。

### 7.4 真实库

| | 写入前 | 写入后 |
|---|---|---|
| `kg_item_holding` 行数 | 58 | 58 |
| 内容哈希（全 58 行 JSON） | `c3f2a6ad71450bc9` | `c3f2a6ad71450bc9` |
| `kg_*` 表 | `['kg_item_holding']` | `['kg_item_holding']` |

老表 DDL 与真实库**逐字一致**（`True`）；真实库仍无旁注表。

---

## 8. 覆盖估计：为什么不做绝对时间

| 口径 | 数量 |
|---|---|
| v3 覆盖章 | 229 |
| 1–229 章出现年号的章 | **10**（4.4%），其中标定「当前故事年」的只有 ch7、ch192 |
| 缓存事件 5907 条里带年号的 | **12**（0.2%） |
| 有 `in_story_date` 的事件 | 1409（23.9%），多为章内相对时刻 |

所以绝对时间只能当锚点，不能当每章落点。现在的两档口径对**全部 229 章**都有效：有先后就用先后，没有就退章节序。绝对时间以后要加，也只是给 `anchor_label` 补证据，不改结构。

---

## 9. 未做

- 没有在真实 `generated.db` 执行 DDL；58 行未动（哈希证据见 7.4）。
- 没有改 `record_holding`，没有碰 `kg_relations`，没有重抽。
- 没有把 ch97 的 `located_at` 转成持有，也没有补 ch106/108 的缺失段。
- 小样结论只覆盖神剑三章与地书两线，不推广到全书。
