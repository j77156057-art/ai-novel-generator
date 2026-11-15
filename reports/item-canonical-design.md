# 物品「同物多规范名」：成因、量化与根治方案

状态：**只读调查**。未改任何表、未改 `ITEM_ALIASES`、未重抽、未跑同步脚本。
数据源：`backend/data/generated.db`（`kg_item_holding` 62 行 / `kg_item_instance` 1 行 /
`kg_holding_story_segment` 4 行）、`backend/novel_dev.db`（`kg_entities` 45 行 item、
`novel_chunks` 7941 行）、抽取缓存 `experiments/2026-11-06-kg-audit/minimax-vol1/cache_minimax`
（229 个 `ch*.json`）。

## 0. 结论

**三者叠加，但主因在归一环节**：唯一带跨章知识的一环是 `canon_source.ITEM_ALIASES`
这个硬编码 dict，它把「判定同一物」外包给了人；抽取环节逐章独立、没有跨章记忆，
存储环节的唯一约束只能防「一名多物」、防不住「一物多名」，读通道只按字面名查、不做判定。

- 「同物多写」估算 **70–130 个名字（9%–18%）**，中位约 90；置信度中。
- 「同名不同物」**通名 16 个（2.2%）**，且 **45 个现有实体里 7 个已被污染（15.6%）**。
- 因此**不能按名字合并**；推荐「同物组」模型（一个组对多个 surface form，持有段挂组），
  配外部化 surface 表 + 待审候选 + 结构闸。

## 1. 成因链：六环里只有一环在做判定，且判据是人

| # | 环节 | 位置 | 负责什么 | 现在的判据 | 为什么挡不住一物多名 |
| --- | --- | --- | --- | --- | --- |
| 1 | 抽取 | `extractor_v3.extract_segment`（[extractor_v3.py](backend/app/kg/extractor_v3.py#L628-L640)）；提示词见 [L560](backend/app/kg/extractor_v3.py#L558-L560) | 产出 item 实体与 `canonical_name`/`aliases` | 提示词要求「canonical_name 用**全书**最常用的规范称呼」，但模型每次只看一章一段 | **模型看不到全书**，每章各取各的「最常用」：ch94 叫「黑金刀」、ch102 叫「黑金长刀」、ch227 叫「监正出品的长刀」。不判定、只命名 |
| 2 | 折叠 | `canon_source.fold_item_aliases`（[canon_source.py L753-L803](backend/app/kg/canon_source.py#L752-L803)） | **唯一真正判定「这两个名字是同一物」的地方**（`normalize_extraction` L806 第一步就调它） | `ITEM_ALIASES`（[L720-L741](backend/app/kg/canon_source.py#L720-L741)）**精确字符串匹配**，16 条；另有 `ITEM_ALIAS_BLOCKLIST`（[L746-L750](backend/app/kg/canon_source.py#L746-L750)）手工剔通名 | 判据是**人写死的常量**：dict 里没有的名字原样通过。加一个物品＝人工读原文、确认、加一行 |
| 3 | canon 词表 | `canon_source` 两层词表（`layers` 的 entities/aliases） | 人物/势力/地点的跨章归一 | 与 item 无关：`kg_nodes` 3315 行里 item 为 **0**，item 层根本没有词表 | item 在这一环被跳过 |
| 4 | 实体存储 | [kg_entities DDL](backend/app/models.py) `UNIQUE(project_id, kind, canonical_name)`；`aliases` 是 JSON 数组 | 存规范名与别名 | 只保证「同一个规范名不重复」；别名没有唯一约束、没有反向索引 | **只能防一名多物，防不住一物多名**。实测已有 3 处别名撞规范名（`铜锣↔法器铜锣`、`银枪→长枪`） |
| 5 | 读通道 | `item_holding_read._resolve_entities`（[item_holding_read.py L78-L140](backend/app/kg/item_holding_read.py#L78-L140)） | 把调用方给的名字/ id 解析到实体 | `canonical_name IN (...)` **或** `aliases LIKE '%"名"%'`，再 `setdefault` 先到先得 | 不判定同物；两行别名重合时**静默取先到的一行**，不报冲突 |
| 6 | 同步脚本 | `sync_item_entities.load_cache_entities` / `build_plan` | 把 229 章缓存灌进 `kg_entities` | 折叠后按 `(project, kind, canonical_name)` 幂等 upsert | 新名字**直接 insert 成新规范名**；没有任何「这个名字是否已是别物的写法」检查 |

### 1.1 为什么每加一个物品就要人肉补一条

因为**跨章同一物的知识只存在于第 2 环的 dict 里**，而 dict 的唯一写入方式是人工编辑源码：

1. 抽取（第 1 环）逐章独立 → 同一物每章可以换一个「最常用」；
2. canon 词表（第 3 环）没有 item → 没有任何自动通道把两个写法并起来；
3. 存储（第 4 环）唯一约束按名字判 → 两个名字就是两个合法实体，不会报错；
4. 读通道（第 5 环）按名查 → 谁也不会发现少了一半持有段；
5. 同步脚本（第 6 环）把缓存原样灌入 → 每跑一次，未登记的名字就固化成一条新实体。

所以**不是某个人忘了加**，而是链路上没有任何一处能自动产生「这两个名字同一物」的判断。
人肉补 dict 是当前唯一供给，补一条才能折一条，这是结构性的，不是操作疏漏。

### 1.2 归因结论

- **抽取环节**：问题是「不判定」，不是「判错」。逐章独立是设计使然，单个模型调用不可能知道全书用词。
- **归一环节**：问题是「判据不成体系」——把判定权交给一个 16 条的硬编码 dict，没有状态、没有证据、没有候选。
- **存储环节**：问题是「约束方向错了」——唯一约束压在 `canonical_name` 上，只能阻止重名，不能阻止同物多名；别名侧完全没有约束。
- **主因排序**：归一环节 > 存储环节 > 抽取环节。抽取只是被动地产出异写；只要有可审的 surface→组 映射，抽取层不需要改。

## 2. 量化

### 2.1 基数先说清

「736」对不上任何一档，最接近的两档是：

| 口径 | 数量 |
| --- | --- |
| 229 章缓存里的未折叠 item 规范名（raw canonical） | **742** |
| 经 `fold_item_aliases` 折叠后的规范名 | **733**（折叠只并掉了 9 个名） |
| 别名 surface（去重） | 524 |
| 全部 surface（规范名 ∪ 别名） | 1157 |
| 实体提及次数 | 1396 |

下文分母用 **742**（折叠前，最能代表「抽取产出了多少个名字」）。

### 2.2 同物多写：70–130 个名字（9%–18%），中位约 90

**方法（四步，可复算）**

1. **生成候选对**。三条字面规则合取，去重后 **210 对**：
   - R1 别名撞上另一规范名：209 对（例：`十五万两税银` 的别名里含 `税银`）；
   - R2 `X的Y` 且 `Y` 是规范名：31 对（例：`许七安的佩刀` → `佩刀`）；
   - R3 修饰语＋通名：73 对（例：`黑金长刀` → `长刀`）。
2. **分层随机抽样 30 对**（`random.seed(42)`，覆盖三条规则）。
3. **人工判定**（逐对读两名的章分布、别名、描述，必要时回原文）：**9 对确为同一物 = 30%**；
   Wilson 95% 区间 **17%–47%**。
4. **折算**：210 对 × 30% ≈ **63 对真同物**（区间 36–99）。三条字面规则对「同物异写」的召回率
   按经验取 0.6–0.8（`玉石小镜→地书` 这类无语义重合的写法抓不到），得 **45–165 对**，
   取中位约 90 对；每对消掉 1 个多余规范名 → **70–130 个名字**。

**抽样判为「真同物」的 9 对**（可复核）

| 对 | 依据 |
| --- | --- |
| 十五万两大奉税银 × 税银 | 全书唯一一笔 15 万两失银，ch1–ch10 同一批 |
| 制服 × 打更人制服 | ch104/ch107 与 ch72 同一套差服 |
| 石碑 × 钱钟生平石碑 | ch42–45 同一块碑 |
| 无核枇杷 × 枇杷 | ch199–200 同一品种 |
| 密信 × 给杨川南的信 | ch162–212 同一封信的两种叫法 |
| 佩刀 × 许七安的佩刀 | `佩刀` 的抽取描述即「许七安佩戴的刀」 |
| 户籍 × 许七安的户籍 | ch64–88 同一份文书 |
| 枷锁 × 枷锁和镣铐 | ch1–214 同一套刑具 |
| 金子 × 黄金 | 同一种贵金属（非唯一实物） |

**上界（不要用）**：机械并查集并组得 **64 组、281 个名字（37.9%）**。这个数被通名污染——
`铜锣` 一组 33 个名字、`佩刀/刀` 一组 25 个、`镜子` 一组 15 个，明显过并，只能当上界。
组大小分布：41 组是 2 个名字，6 组 3 个，其余 17 组都是被通名拉大的。

**下界（可证）**：现有 `ITEM_ALIASES` 16 条 = 13 个多余名（神剑 11 个写法、地书 2 个、黑金长刀 2 个）。

**置信度：中**。样本 30、单人判定、规则召回率是经验值；但「远大于 16 条硬编码」这个结论
在区间下沿（45 对）也成立。

### 2.3 同名不同物：16 个通名 + 7/45 个实体已污染

**A. 通名/品级名 16 个（2.2%）**：`佩刀 刀 剑 手串 法器 玉佩 玉扳指 符箓 账册 账簿 酒壶 铜锣 银票 镜子 长刀 长剑`。
逐个可证，最硬的三条：

- `铜锣`：**427 章**出现；ch62 原文「其次是**铜锣**，是正经的打更人，至少是练气境，月俸五两银子二石米」——
  它是**品级**，同时指戴铜锣的人、也指那面锣。
- `长刀`：**127 章**出现；ch47「腰悬长刀和军弩」、ch48「御刀卫的制式长刀」、ch102「黑金长刀」——
  是类别，不是某一物。
- `玉石小镜`：**111 章**（ch51–ch926）；ch596「李妙真取出地书碎片……**跌出一件同款玉石小镜**。
  李灵素探出手掌接过，从指间逼出一滴鲜血，让地书重新认主」——**九块碎片是九面同款镜子**，
  是「一类多实例」，不是「一物多名」。

**B. 现有 45 个 item 实体里 7 个已被污染（15.6%）**

| 实体 id | 名字 | 污染 |
| --- | --- | --- |
| 34 | 铜锣 | 8 个别名含「俊朗不凡的铜锣」「新来的铜锣」「昨日新换的铜锣」——**这些是打更人（人），不是锣**；实体 kind 错了 |
| 19 | 法器铜锣 | 别名 `铜锣`，与 id=34 双向撞名 |
| 31 | 金牌 | 别名含 `腰牌`，但 ch73–107 的腰牌是打更人凭证、ch108+ 的金牌是御赐，两物 |
| 35 | 银枪 | 别名含 `长枪`，而 `长枪` 是另一条独立规范名 |
| 23 / 30 / 37 | 玉扳指 / 酒壶 / 长剑 | 纯通名，任何同类物都能叫 |

**C. 抽样里的错并风险：4/30 = 13%（区间 5%–30%）**

- `腰牌 × 金牌`：打更人腰牌 vs 陛下御赐金牌；
- `佩刀 × 朱广孝的佩刀`：`佩刀` 的抽取描述是「许七安佩戴的刀」，与朱广孝那把不是一把；
- `凭书 × 出入皇城的凭书`：打更人衙门内部凭书 vs 皇城凭书；
- `鸡精 × 简陋版鸡精`：两个批次/品级。

**D. 结论**：**不能按名字合并**。按名合并会同时踩「一物多名」（漏并）和「一名多物」（错并）；
而且是双向的——现存的 `玉石小镜→地书` 已经踩了后者（九面镜子并成一件法宝）。

## 3. 方案候选与代价

| 方案 | 能解决 | 解决不了 | 人力 | 误伤 | 评 |
| --- | --- | --- | --- | --- | --- |
| **A 硬编码别名表继续扩**（现状） | 已确认的单条异写；改一次立刻生效 | 规模：每物 1 条人肉；无证据、无状态、无候选；**别名撞名不可见**（现有 3 处）；同步重跑会把旧名重新插回来 | O(物品数)，且随章数线性增长 | 中：blocklist 靠人手挡，`黑金刀` 已踩过一次 | 只适合当兜底，不能当主线 |
| **B 别名外部化成表 + 审核状态 + 证据，抽取时读表折叠** | 可审计、可回滚、可批量；`status` 区分 pending/approved；抽取/同步时按表折叠，不再改源码发版 | 仍靠人供给；不加重名约束的话，新名字照样能写成新规范名 | 每条 1 次审核（约 1–2 分钟）；一次性回填 45 实体 + 16 条 | 低（未审核不生效） | **必做**，是 A 的替代形态 |
| **C 自动候选发现**（共现、`X的Y`、上下文相似） | 把「人肉想」变成「人肉审」；已实测 210 对候选，30 对抽 9 对为真 | 精度只有 ~30%，反向风险 13%；不能自动入库 | 每批审 N 条；首轮约 210 条 | 中：**必须 pending 态**，自动入库会把 `佩刀` 折成 `许七安的佩刀` | **必做**，是供给线 |
| **D 结构约束**：`(project, kind, canonical_name)` 唯一 + 别名全局唯一 | 别名全局唯一：直接消灭「一个别名属于两个实体」和抢注 | **完全解决不了同物多写**——`黑金刀`/`黑金长刀` 是两个合法规范名，唯一约束管不着；别名唯一还会禁止「同一写法属于两个组」的合法多义（`铜锣` 既是品级又是锣） | 一次性清 3 处撞名 | 高：现有 3 处撞名会直接写入失败；对合法多义是误伤 | **不能单独用**；改造成「surface 表内唯一」才安全 |
| **E「同物组」模型**：一个组对多个 surface form，持有段挂组 | 同时表达「一物多名」（组多 surface）、「一名多物」（同一 surface 只能属一个组）、「一类多实例」（instance 挂组）；改名不动持有段 | 不自动判定；读侧要改；要迁移 | 一次性建表 + backfill + 读侧改造；之后每条候选 1 次审核 | 低：段挂组，名字退化为 surface | **推荐骨架** |

**关键判断**：D 常被误认为「上了唯一约束就根治」，其实方向反了——唯一约束能防**一名多物**（两个人抢同一个名字），
防不住**一物多名**（同一个东西有两个名字）。本项目的主要痛点是后者，所以单靠 D 无效。

## 4. 推荐方案与迁移路径

### 4.1 推荐：E 作骨架 + B 作审核面 + C 作供给 + D 作闸（D 落在 surface 表上）

四者不是四个方案，是一套的四层：

- **E 骨架**：`kg_item_group`（组）+ `kg_item_surface`（写法→组）+ `kg_item_holding.group_id`。
- **B 审核面**：surface 表自带 `status`（pending / approved / rejected）、`evidence_chapter`、`evidence`、
  `source`、`reviewer`、`reviewed_at`。
- **C 供给**：候选发现只往 surface 表写 `pending`，人工批准才生效。
- **D 闸**：`UNIQUE(project_id, surface)` 压在外化表上——同一写法不能属于两个组；
  而不是压在 JSON 别名里（那会误伤合法多义）。

### 4.2 零破坏迁移（62 行持有段、45 个实体一个不删不改名）

全部是**加表、加可空列、回填**，没有 DELETE、没有 UPDATE 规范名：

1. **建表**（新迁移 `2026-11-1x-item-group-v1`）
   ```sql
   CREATE TABLE kg_item_group (
     id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'item',
     canonical_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
     created_at TEXT NOT NULL, UNIQUE(project_id, kind, canonical_name));
   CREATE TABLE kg_item_surface (
     id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
     surface TEXT NOT NULL, group_id INTEGER NOT NULL REFERENCES kg_item_group(id),
     status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
     source TEXT NOT NULL DEFAULT '', evidence_chapter INTEGER, evidence TEXT NOT NULL DEFAULT '',
     reviewer TEXT, reviewed_at TEXT, created_at TEXT NOT NULL,
     UNIQUE(project_id, surface));
   ```
2. **回填组**：45 行 `kg_entities(kind='item')` → 45 个组，1:1，规范名不动。
3. **回填 surface**：每个组写入 1 条 `approved` 的规范名 surface + 其 `aliases` 逐条写入
   （状态 `approved`）；其中 `铜锣`/`金牌`/`银枪` 的撞名别名先写 `pending`，不自动合并。
4. **加列**：`ALTER TABLE kg_item_holding ADD COLUMN group_id INTEGER;`
   `ALTER TABLE kg_item_instance ADD COLUMN group_id INTEGER;`（可空，不动旧列）
5. **回填 group_id**：按 `item_name` 精确匹配 surface → group；匹配不到的写 `NULL` 并列清单
   （实测 46 个持有名全部能被现有实体解析到，预期 0 漏）。
6. **验证**（任一不过就停）：
   - `kg_item_holding` 仍是 62 行、`kg_entities` 仍是 45 行；
   - `SELECT COUNT(*) FROM kg_item_holding WHERE group_id IS NULL` = 0；
   - 每个 `(item_name)` 只映射到 1 个 group；
   - 读通道跑 8 个用例（地书/黑金长刀/黑金刀/许七安/神剑/铜锣/金牌/银枪），结果与迁移前一致。

**为什么零破坏**：旧列旧行一格不动，读通道在 group 解析失败时回退到现有的按名解析；
回滚只需 `DROP` 两张新表并忽略新列（SQLite 可空列不写即可）。

### 4.3 防复发的那道闸加在哪

两个写入口，缺一不可：

1. **持有段写入**：`generated_store.record_holding`（[generated_store.py L552](backend/app/kg/generated_store.py#L552-L600)）
   ——它是全库唯一持有写入口（`_insert_holding` L505 只被它调用）。加：
   `surface → group` 查不到 → **不新建组、不写段**，写一条 `pending` 候选并返回
   `action='pending_surface'`。
2. **实体同步**：`sync_item_entities.build_plan`（[sync_item_entities.py](backend/migrations/2026-11-10-item-holding-v3/sync_item_entities.py)）
   ——当前会把未知名字直接 insert。加：
   未在 surface 表登记的规范名 → 进 `pending` 候选，`--allow-new-surface` 才插；
   默认 dry-run 列出「本次会新增的规范名」。

这就是「每加一个物品不再需要人肉补一行」的地方：**新物品进来先落候选，人只做批准**；
批准是数据操作，不是改源码发版。

### 4.4 人工要审的那条记录长什么样

一条 `pending` 候选（表行，字段对齐 `kg_item_surface` + 信号列）：

```json
{
  "id": 211,
  "project_id": 5,
  "surface": "监正出品的长刀",
  "group_id": 48,
  "group_canonical": "黑金长刀",
  "status": "pending",
  "source": "rule:R3+cooccur",
  "signals": {
    "alias_collide": false,
    "possessive_head": true,
    "same_chapter": [227],
    "other_surfaces_in_chapter": ["黑金长刀"],
    "chapter_distance_to_group": 125
  },
  "evidence_chapter": 227,
  "evidence": "监正送他的黑金长刀",
  "reviewer": null,
  "reviewed_at": null
}
```

审核界面只需要三个动作：**批准**（→ approved，持有段自动归组）、**拒绝**（→ rejected，不再提示）、
**新建组**（确实是一把新刀）。每条 1–2 分钟，且有原文可核。

### 4.5 落地后「新物品进来」的流程

| 情形 | 步数 | 流程 |
| --- | --- | --- |
| 名字已登记（surface 命中） | **1 步** | 抽取产出名字 → 闸查表命中 → 直接写段/实体。人 0 次 |
| 名字未登记、是已有物的新写法 | **2 步 + 1 次审核** | 落 `pending` 候选 → 人点「批准」 |
| 名字未登记、确实是新物 | **2 步 + 1 次审核** | 落 `pending` → 人点「新建组」 |

对比现在：每个新物品都要「读原文 → 判断 → 改 `canon_source.py` → 发版 → 重跑同步」，
且旧名会在下一次同步时重新长出来（`ITEM_ALIASES` 没改就会复现，`黑金刀` 就是这个坑）。

## 5. 边界与可执行的下一步

**本轮不做**：不改 `ITEM_ALIASES`、不写任何表、不重跑同步、不动 62 行持有段。

**下一步（按顺序，每步可独立验收）**

1. **写建表迁移**（只建 `kg_item_group` / `kg_item_surface` + 加可空 `group_id`），
   `--dry-run` 输出 45 组、46 个持有名的回填计划与 3 处撞名清单；不提交数据变更。
2. **生成首批候选**：把第 2 节的 210 对 + 45 实体的撞名别名导入 `pending`，
   人工过一遍，产出 `approved` 基线（预计 60–100 条）。
3. **读通道加 group 解析**（`_resolve_entities` 优先按 surface→group，失败回退按名），
   用第 4.2 节 6 条验证守住零破坏。
4. **上闸**：先在 `record_holding` 与 `build_plan` 里跑 `warn` 模式一周，比对「本该 pending」的
   实际写入量；再切 `block`。
5. **回填历史**：按 4.2 第 5 步补齐 `group_id`，之后 `kg_item_holding.item_name` 只作展示，
   判重一律走 `group_id`。

**验收口径**：迁移后 `kg_item_holding` 62 行、`kg_entities` 45 行不变；新增物品不再出现
「同名两条开放段」；人工从「改代码」降为「点批准」。
