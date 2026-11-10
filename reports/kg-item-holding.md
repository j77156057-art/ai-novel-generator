# 物品持有史 kg_item_holding

状态：表结构与读写入口已落地，**未接抽取管线、未处理存量、未接 reset_chapter**。

- 表位置：生成层 `backend/data/generated.db`（与 `gen_edges` 同库）
- 建表方式：`generated_store.SCHEMA` + `ensure_schema()`，全 `CREATE ... IF NOT EXISTS`，重复执行幂等
- 代码：`backend/app/kg/generated_store.py`
- 测试：`backend/tests/test_kg_item_holding.py`

## 1. 表结构

```sql
CREATE TABLE IF NOT EXISTS kg_item_holding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),
    owner_id INTEGER,
    owner_name TEXT,
    owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    transfer_event_id INTEGER,
    reason_code TEXT NOT NULL DEFAULT '',
    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR
        (owner_state IN ('unowned', 'sealed', 'unknown')
         AND owner_name IS NULL)
    ),
    UNIQUE (item_id, hold_type, valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_item_holding_open
    ON kg_item_holding (COALESCE(item_id, -1), item_name, hold_type)
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_item_holding_item
    ON kg_item_holding (item_id, valid_from);
CREATE INDEX IF NOT EXISTS ix_item_holding_owner
    ON kg_item_holding (owner_id, valid_to, hold_type);
```

`owner_kind` 取实体 kind 的实际值（`kg_entities.kind`，生成层镜像为 `gen_nodes.kind`），不另造 `creature` 这类分类；本表不对它做枚举约束。

## 2. 语义

**hold_type**

- `own`：权属（这东西是谁的）。
- `custody`：保管（这东西在谁手上）。
- 两者是**两条独立时间线**，同一物品可以同时有一条 own 和一条 custody，互不关闭。

**owner_state**

| 值 | 含义 | owner_id | owner_name |
|---|---|---|---|
| `held` | 有主 | 可空 | 可空，但两者至少一个非空 |
| `unowned` | 明确无主 | 空 | **必须为空** |
| `unknown` | 归属未知 | 空 | **必须为空** |
| `sealed` | 封印中，无普通持有者 | 空 | **必须为空** |

第三态 = `owner_state='held'` + `owner_id IS NULL` + `owner_name` 非空：有主，但主人还没实体化。读接口只看 `owner_id IS NULL` 时**不能**把它判成无主；按名字仍能查到它。

`unowned / unknown / sealed` 不允许写 `'未知'`、`'桑泊封印'` 这类哨兵名，避免造型假持有者。

## 3. 写入规则

唯一入口：

```python
record_holding(con, item_name, hold_type, owner_name, chapter, *,
               item_id=None, owner_id=None, owner_kind=None,
               owner_state="held", transfer_event_id=None,
               event_object=None, reason_code="", evidence="",
               confidence=0.5) -> dict
```

返回 `{"action": ..., "id": ..., "closed": ..., "reason": ...}`，`action` 取值：

| action | 何时 |
|---|---|
| `inserted` | 首段，或先关闭旧段再插新段 |
| `confirmed` | 同持有者同状态复述：不重开段 |
| `replaced` | 同章竞争且有事件对象，事件对象胜出：撤掉零长度段后重写 |
| `candidate` | 同章竞争无法判定：两条都进 `gen_candidates`，持有表不留行 |
| `rejected` | 倒序（`non_forward`）或零长度（`zero_length`） |

**规则一：先关后插。** 同一 `(item_id, item_name, hold_type)` 已有开放段时，先 `_close_open_segment` 把它的 `valid_to` 置为本章，再插入新的开放段。同一持有者、同一状态的复述只返回 `confirmed`。

**规则二：同章竞争不写史。** 本章已开过一段，又来一个不同持有者的主张：

- 有 `transfer_event_id` 且 `event_object` 能对上其中一方 → 该方胜出；另一方若尚未落库则只进候选，若已落库（零长度）则撤销后重写。
- 没有可用事件对象 → 先写的零长度段删除，两条主张都写进 `gen_candidates`（`rel_type=hold_type`，`reason='same_chapter_clash'`）。

**规则三：拒绝零长度与倒序。** `chapter < 开放段.valid_from` → `rejected/non_forward`；`chapter == valid_from` 且同主换态 → `rejected/zero_length`。

**公共关闭函数**：`_close_open_segment(con, table, key, chapter)`，`gen_edges` 的 `invalidate_edge` 与持有表共用，不存在第三套关闭逻辑。只更新 `valid_to IS NULL` 的行，键比较用 `IS`（支持 `NULL` 键）。

## 4. 时间边界

- 段一律是**半开区间** `[valid_from, valid_to)`；`valid_to IS NULL` 表示仍然有效。
- 章号是整数章序，不是时间戳；同一章发生的变化用「同章竞争」处理，不允许产出零长度段。
- 相邻段首尾相接：前一段 `valid_to == 后一段 valid_from`，无重叠、无空洞。
- `valid_to > valid_from` 由 CHECK 保证；**段间不重叠/相接只能由写函数保证**（见第 6 节）。

## 5. 读接口

```python
item_holding_timeline(con, item_name, *, item_id=None) -> list[dict]
```

某物品的完整持有时间线，`own` / `custody` 各自成线，按 `valid_from` 排序，含开放段。

```python
holder_current_holdings(con, owner_name=None, *, owner_id=None,
                        chapter=None) -> list[dict]
```

某持有者当前持有什么。`chapter=None` 只取开放段；`chapter=N` 取覆盖 N 的段（`valid_from <= N < valid_to` 或 `valid_to IS NULL`）。只按 id / name 命中，不加 `owner_id IS NULL` 过滤，第三态因此可读。

## 6. SQLite 上表达不了 / 打折扣的约束

1. `UNIQUE (item_id, hold_type, valid_from)`：`item_id` 为 `NULL` 时 SQLite 认为各行互不相等，**对未实体化物品失效**。开放段唯一性用表达式索引 `COALESCE(item_id, -1) + item_name` 补偿，但表级 UNIQUE 本身补不了。
2. 段间「不重叠、首尾相接」无法用 CHECK / UNIQUE 表达，只能靠 `record_holding` 与测试保证。
3. `owner_kind` 必须落在 `kg_entities.kind` 实际值上：跨库跨表，未加枚举 CHECK、未加触发器，写错值不报错。
4. `owner_id` 与 `owner_name` 的同一性（同一个 id 永远同一个名字）无法约束。
5. `owner_state='unowned'` ⇒ `owner_id IS NULL` 未加 CHECK（设计未要求，没有自行补规则）。
6. `transfer_event_id` 未声明指向事件的 FK；SQLite 默认也不强制 FK。
7. `valid_from / valid_to` 只校验正长度，无法校验章号范围，也无法校验它与 `evidence_chapter` 的关系。
8. `reason_code` 是自由文本，无枚举约束。

## 7. 与最初设计的偏差

1. **表放在 `generated.db`，不是 `novel_dev.db`。** 依据是「`_close_open_segment` 与 `gen_edges` 共用」。代价见第 8 节。
2. 开放段唯一索引用**表达式索引**（`COALESCE(item_id, -1), item_name, hold_type`）而不是裸 `(item_id, hold_type)`，否则 `item_id` 为空时唯一性失效。
3. 同章竞争的「不写史」用 **DELETE 撤掉刚插入的零长度段**实现，没有加 `status` 列。
4. 候选**复用 `gen_candidates`**（`from_name=物品名`、`to_name=持有者名`、`rel_type=hold_type`、`reason='same_chapter_clash'`），没有新建候选表。
5. `owner_name` 从 NOT NULL 改为可空，并加「非 held 态必须为空」的 CHECK；`held` 态只要求 `owner_id` / `owner_name` 至少一个非空。
6. `item_name` 必填、`item_id` 可空：物品本身也可能是未实体化的名字。

## 8. 跨库读的实际代价（结论：现在不能直接 JOIN）

- 持有表在 `backend/data/generated.db`；`list_relations` / `retrieval_v3` 读的是 `backend/novel_dev.db`（`settings.database_url = sqlite:///./novel_dev.db`）。
- 两条读路径用 SQLAlchemy session，持有表用裸 `sqlite3` 连接。**不同连接、不同事务，无法直接 JOIN**，ORM 里也没有这张表的映射。
- 想直接 JOIN，只有一条路：在生成层 sqlite3 连接上 `ATTACH DATABASE 'novel_dev.db' AS kg`，再写 `JOIN kg.kg_entities`。代价是：
  - ATTACH 必须绑定在该连接的生命周期上，连接池/多线程下要自己保证；
  - 跨库 JOIN 没有外键与一致性保证，读到的只是 `novel_dev.db` 已提交的快照，看不到生成侧同事务的未提交数据；
  - 两个库的 schema 演进要一起看，迁移顺序变成隐式依赖；
  - 路径是相对的（`./novel_dev.db`），进程工作目录变了就挂错库。
- 不 ATTACH 的替代方案：应用层两次查询再按 id/名字拼（N+1 或批量 IN），或把表搬到 `novel_dev.db`。
- **本轮不搬**，只记录结论。

## 9. 已知缺口

- `reset_chapter` 未接：重跑某章不会清 `kg_item_holding`，也不会回滚已关闭的段。
- 写入入口尚未接抽取管线，没有任何生产调用方。
- 存量 24 条物品端点 `belongs_to` 未迁移。

## 10. 地书 / 玉石小镜 合并（2026-11-10）

### 10.1 ch51–73 核证

| 章 | 原句 | 结论 |
| --- | --- | --- |
| 51 | 老道「指着最顶端的菩提手串和玉石小镜……公子，二选一」；「许七安接过钱囊，又从老道那里取走玉石镜……随意把巴掌大的玉石镜揣进怀里」 | 投壶赢后二选一，不是抽中；菩提珠被马车买走。**当时不知道是地书**（老道自称「不知」） |
| 56 | 「钱袋里只剩下一面不足巴掌大的玲珑玉质小镜」「玉质镜面上……若隐若现的几张银票」 | 发现储物功能，仍不知是地书 |
| 57/60/69 | 「他握着玉石小镜，用力甩了甩，做倾倒状」「镜面透出军弩、铜镜、朴刀虚影」 | 一直在身上，当储物法宝用 |
| 71 | 「藏在枕头底下的玉石小镜」「洁净的镜面缓缓浮现一行小字」「【玖：金莲师兄在哪……你怎么得到这面镜子的」 | 第一条来讯，只有玖号 |
| 72 | 「【玖：贫道紫莲，那位赠予你镜子的道长是贫道的师兄」「这法宝又叫做地书」「【叁：……那位金莲道长……以黄金五百两的价格兜售于我」 | 身份对上：他是**叁（三号）** |
| 73 | 「地书是上古至宝……滴血认主」「抹在玉石小镜的表面」「八个光点……加上我，正好九面镜子」「滴血认主后……产生了某种奇妙的联系」 | 不是第一次拿到，是拿到 22 章后**补建立联系** |

- **51–73 一直在许七安身上**，中间没给过别人。老道只在 ch51 出现；魏渊出现在 55/62/64/66 但没碰镜子（ch74 才第一次看到）；金莲师兄在 51–73 只由玖号口述，ch77 才真人现身。
- **71–72 他不在群里**：ch74 明说「叁号碎片被地宗的人封禁了，断绝了与我们之间的传书能力，且地宗的人可以通过玖号碎片锁定叁号」——玖号能来讯是地宗锁定叁号，不是群成员身份。八个光点是 ch73 滴血之后才出现。
- **金莲持有到 73 没有正文支持**。只支持「金莲在 ch51 把三号碎片交给许七安」（51 摊位、73 玖号转述、74 陆号「舍弃碎片，转赠给你」、77 金莲「放心把地书托付与你」、100「您将三号碎片赠予我时」）。原行 `[73,74)` 把玖号「到京城后失联」误当成持有终点。
- **判定：起点 51，不是 73**（置信度 0.95）。补 `[0,51)` 金莲 custody；金莲那行实体没错、区间错。

### 10.2 四项改动

1. **hold_type**：玉石小镜三段 `own → custody`；`own` 只剩「地书 → 天地会」一条（`instance_id` 为空，挂整体）。删掉 `地书 → 地宗` 的 `own` 段。
2. **合并**：规范名取「地书」，`玉石小镜` 落别名；单块用 `kg_item_instance` 的 `三号` 区分（证据 ch72「叁代表的是我这面镜子的序号吗」）。
3. **段冲突**：删并行开放段 `[74,∞)`，链改成 `[0,51) 金莲 → [51,105) 许七安 → [105,107) 许新年 → [107,∞) 许七安`，首尾相接、无重叠。
4. **别名收敛**：`地书` 剔掉 `镜子`；`玉石小镜`/`暗金色长刀` 不再作规范名；`暗金色长刀`/`刀胚` 折叠到 `黑金长刀`（ch93 炼刀、ch227「监正送他的黑金长刀」）。

### 10.3 改前后 diff

```
kg_item_holding: 63 → 61
~ id=12 地书: valid_from 73→0, valid_to 74→51, evidence_chapter 73→51, instance_id None→1
- 删除 id=13: 地书/custody/许七安[74,∞)
- 删除 id=14: 地书/own/地宗[73,74)
~ id=23 暗金色长刀→黑金长刀, valid_to ∞→102
~ id=32/33/34 玉石小镜→地书, own→custody, instance_id None→1
kg_item_instance: 新增 (地书, None, 三号, ch72)
kg_entities(item): 48 → 46
~ id=9 地书 aliases: 去掉「镜子」，并入 三号碎片/叁号镜子/地宗法宝/玉石碎片/枕头底下的玉石小镜/那面玉石小镜
- 删除 id=17 暗金色长刀
- 删除 id=24 玉石小镜
~ id=48 黑金长刀 aliases: 去 刀/刀子/长刀，收 暗金色长刀/刀胚
```

### 10.4 改后校验

- 「地书」「玉石小镜」「三号碎片」「地书碎片·三号」四个名字分别查当前持有者，结果逐字节一致：
  `地书：权属（own）归天地会` + `地书·三号：实物（custody）在许七安手上`。
- 段清单：`地书/custody/三号 [0,51) 金莲师兄 → [51,105) 许七安 → [105,107) 许新年 → [107,∞) 许七安`；`地书/own/整体 [74,∞) 天地会`。无并行开放段。
- 读通道补了一处：`_resolve_entities` 原来只按 `canonical_name`/`id` 取行，别名不在 SQL 条件里，只给「玉石小镜」会解析不到。改为在 OR 条件里加 JSON 子串预筛（仍只打一次库），回归测试见 `test_alias_input_resolves_to_canonical`。
- 备份：`backend/backups/generated-merge-20261110-165002.db`、`backend/backups/novel_dev-merge-20261110-165002.db`。
- 回滚：停服后把备份覆盖回 `backend/data/generated.db` 与 `backend/novel_dev.db`（`generated.db` 还要一并删 `-wal`/`-shm`）。

### 10.5 未处理

- `黑金刀`（id=47，持有 `[94,∞)`）仍是独立规范名，和 `黑金长刀` 形成并行开放段；本轮只按批准折叠 `暗金色长刀/刀胚`，未动它。**（2026-11-10 段精度批次已并段：删 id=57、实体 id=47 收作别名，见 `reports/dishu-evidence-chain.md` 第 8 节。）**
- 抽取缓存里 `地书碎片` / `地书聊天群` 仍是独立规范名，未来重跑同步会把它们插成新实体；若要彻底收敛，需要再加两条 `ITEM_ALIASES`（`地书聊天群` 语义上是群、不是物品，建议单独处理）。

