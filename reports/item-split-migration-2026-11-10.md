# 物品治理落库报告（2026-11-10）

对应清单：[item-holding-change-checklist-2026-11-10.md](item-holding-change-checklist-2026-11-10.md)
执行脚本：[apply_item_split.py](../backend/migrations/2026-11-10-item-split-v1/apply_item_split.py)

五条一次做完，已落真实库。全程走迁移目录：写前备份、逐行 JSON 快照、删组前孤儿检查、可一键回滚。

---

## 1. 改前 / 改后总量

| 表 | 改前 | 改后 | 说明 |
|---|---:|---:|---|
| `kg_item_group` | 52 | 53 | 拆出 3 组、删 2 组（铜锣 32、长枪 36） |
| `kg_item_surface` | 139 | 133 | 删 8 条铜锣写法、补 2 条新组规范名（-8+2） |
| `kg_item_holding` | 61 | 60 | 只删铜锣 43；`id=59` **保留** |
| `kg_holding_story_segment` | 4 | 4 | 只改挂，不删 |
| `kg_entities`（kind=item） | 45 | 43 | 删铜锣 34、长枪 38 |

孤儿检查（改后）：`kg_item_holding` 与 `kg_holding_story_segment` 都没有 `group_id`
指向不存在组的行；`kg_item_surface` 也没有悬空组。**0 孤儿。**

---

## 2. 五条变更明细

### 2.1 神剑组拆成三组

`kg_item_group`

- 新建 `镇国剑`（id=100，active）
- 新建 `洛玉衡祖传神剑`（id=101，active）
- 新建 `佛门金身黄金神剑`（id=102，active）
- 组 24 `神剑` 保留 `canonical_name`，`status` 改为 `generic`，
  `generic_members=[100,101,102]`，note 写明「至少指三件不同的剑，不指向单件」

`kg_item_surface`：86 / 87 / 88 / 89 / 90 / 91 / 94 / 95 / 96 / 97 / 98
共 11 条迁到 `镇国剑`（100）。写法 24 `神剑` 留在组 24。
另外给 101 / 102 各补一条同名 approved 写法（否则这两个组无法被名字解析到）。

`kg_item_holding`：59、60 改挂 `group_id=100`、`item_name='镇国剑'`。

- **`id=59 镇北王 own 99–106` 原样保留**，只改归属，未删未改区间
  （`valid_from=99, valid_to=106, reason_code=gift`）。

`kg_holding_story_segment`：1 / 2 / 3 改挂 `group_id=100`、`item_name='镇国剑'`。

`kg_entities`（应用库）：**实体 26 规范名 `神剑` → `镇国剑`**，并从 aliases 去掉
`镇国剑`（它已是规范名）。这是清单未明写、但必须做的联动：读通道在
「写法指向的组」与「实体规范名指向的组」不一致时会判 `ambiguous`，
不改这里，查「镇国剑」反而会坏。

`app/kg/canon_source.py`：`ITEM_ALIASES` 的 11 个折叠目标从 `"神剑"` 改为 `"镇国剑"`，
与库内实体保持一致。

### 2.2 玉石小镜三条漏口降级

`kg_item_surface`：65 `玉石碎片`、66 `枕头底下的玉石小镜`、67 `那面玉石小镜`
从组 9（地书）移到组 46（玉石小镜，generic）。三条仍为 approved。

效果：这三个名字现在走 generic 分支，返回「玉石小镜是类通称 + 候选」，不再冒充地书本体。

### 2.3 铜锣 / 法器铜锣 / 长枪 出表（17 行）

铜锣（组 32）：

- 删写法 32 / 114 / 115 / 116 / 117 / 118 / 119 / 120（8 行）
- 删持有段 43（`铜锣 own 许七安 189–∞`，证据是职位等级，不是物品）
- 孤儿检查后删组 32
- 删实体 34（`铜锣`）
- 最后从实体 19（`法器铜锣`）aliases 去掉 `铜锣`——放最后是为了避免中间态
  「铜锣」唯一落到 19 上被当成可解析

长枪（组 36）：

- **先** `valid_to=228`（holding 48 关闭），**再**改名 `银枪` + 挂组 33
- 写法 36 `长枪` 迁组 33
- 孤儿检查后删组 36
- 删实体 38（`长枪`）
- 实体 35（`银枪`）aliases 去掉 `长枪`

顺序未颠倒：若先改名，会与银枪 45（`杨砚 own 228–∞`，开放段）撞
`ux_item_holding_open`。

### 2.4 腰牌删 1 条 alias

实体 31（`金牌`）aliases 去掉 `腰牌`。surface 109 `腰牌` 仍是 pending（未动），
不合并、不降泛称。查「腰牌」现在返回 `no_record`，并保留「待审映射」的 note。

### 2.5 读通道分层 + 完整时间线（缺陷修复）

`backend/app/kg/item_holding_read.py`：

- `_facts_from_rows`：对有 `group_id` 的单位分层
  - 无 `instance_label`：只返回组级段（`instance_id IS NULL`）
  - 有 `instance_label`：只返回该碎片的段（不再夹带组级 `own`）
- `lines` 改为该层**全部**段（按 `order_key` 升序），`status` 仍按每个
  `hold_type` 最新一段聚合——历史可见，但不覆盖当前
- 渲染：同一 `hold_type` 有多段时才补章号区间 `（起–止）` / `（起–∞）`；
  单行输出保持原样，不影响既有断言

---

## 3. 改前 / 改后查同名返回什么

「改前」= 旧库 + 旧读通道（只取每个 `hold_type` 最新一行）；「改后」= 新库 + 新读通道。

| 查询名 | 改前 | 改后 |
|---|---|---|
| 神剑 | `- 神剑：权属（own）归元景帝` | `- 神剑：通称/歧义名…；候选：镇国剑（权属归镇北王（99–106）；权属归元景帝（106–∞）） / 洛玉衡祖传神剑（无持有记录） / 佛门金身黄金神剑（无持有记录）` |
| 镇国剑 | `- 神剑：权属（own）归元景帝` | `- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）` |
| 镇国宝剑 / 大奉镇国宝剑 / 黄铜剑 / 玄武神剑 | 都返回 `- 神剑：权属归元景帝` | 都返回 `- 镇国剑：…镇北王（99–106）…元景帝（106–∞）` |
| 洛玉衡祖传神剑 | `no_record`（via=name，不认识） | `no_record`（via=surface，已挂独立组） |
| 佛门金身黄金神剑 | `no_record`（via=name） | `no_record`（via=surface，已挂独立组） |
| 地书 | `- 地书：权属（own）归天地会；实物（custody）在许七安手上` | `- 地书：权属（own）归天地会`（**不再吐三号**） |
| 地书碎片 | 同上（含三号 custody） | `- 地书：权属（own）归天地会` |
| 三号碎片 | 只显示最新一段 custody | `- 地书·三号： custody 金莲师兄（0–51）→ 许七安（51–74）→ 魏渊（74–76）→ 许七安（76–105）→ 许新年（105–108）→ 许七安（108–∞）` |
| 玉石小镜 | generic 候选：`地书（own 天地会；custody 许七安）` | generic 候选：`地书（own 天地会）` |
| 玉石碎片 | `- 地书：权属（own）归天地会；实物（custody）在许七安手上` | `- 玉石小镜：类通称…；候选：地书（权属（own）归天地会）` |
| 枕头底下的玉石小镜 / 那面玉石小镜 | 同玉石碎片（当成地书） | 都走 `玉石小镜` generic |
| 铜锣 | `ambiguous`（法器铜锣 / 铜锣） | `no_record`（出表，不猜） |
| 法器铜锣 | `ambiguous` | `- 法器铜锣：权属（own）归许七安` |
| 长枪 | `ambiguous`（银枪 / 长枪） | `- 银枪：权属归银甲女子（156–228）；权属归李妙真（227–228）；权属归杨砚（228–∞）` |
| 银枪 | `- 银枪：权属归银甲女子（156–228）；权属归杨砚（228–∞）` | 同上，多出李妙真 227–228 一段 |
| 腰牌 | `- 金牌：权属（own）归许七安`（via=entity） | `no_record`；note「『腰牌』→金牌 是待审映射，本轮不作数」 |
| 金牌 | `- 金牌：权属（own）归许七安` | 不变 |

---

## 4. 产物与回滚

- 迁移脚本：`backend/migrations/2026-11-10-item-split-v1/apply_item_split.py`
- 本次快照：`backend/migrations/2026-11-10-item-split-v1/snapshots/item_split_20261110-210604.json`
- 写前备份：
  - `backend/backups/generated-pre-split-20261110-210604.db`
  - `backend/backups/novel_dev-pre-split-20261110-210604.db`

回滚：

```powershell
python backend/migrations/2026-11-10-item-split-v1/apply_item_split.py --rollback backend/migrations/2026-11-10-item-split-v1/snapshots/item_split_20261110-210604.json
```

回滚在副本上验证过：`kg_item_group / kg_item_surface / kg_item_holding /
kg_holding_story_segment / kg_item_instance / kg_entities(item)` 全部与改前逐行一致。

---

## 5. 清单没预见、但执行时处理了的点

1. **实体 26 规范名必须同步改**（`神剑` → `镇国剑`）。不改会让读通道判
   「写法组 ≠ 实体组」歧义，查镇国剑反而坏掉。已改，aliases 同步去掉重复项。
2. **两个空组需要补写法行**。`洛玉衡祖传神剑` / `佛门金身黄金神剑` 只建组不建
   `kg_item_surface` 时，`_build_units` 按 `by_surface` 解析不到，等于查不到。
   已各补一条同名 approved 写法（source=migration）。
3. **快照去重**。长枪 48 被更新两次（先 `valid_to`，再改名），首版快照把第二次的
   中间态也记了，回滚会把 `valid_to` 留在 228。已改为「同一 id 只记第一次见到的
   原始行」，副本回滚验证通过。
4. **时间线修复只对已登记组的单位生效**。旧库无 `kg_item_group` 时行为不变，
   避免影响非登记表调用方。`test_kg_item_holding_read.py` 全绿。
5. **长枪时间线出现 227–228 重叠**（李妙真 227–228 与杨砚 228–∞ 在 228 处半开区间衔接，
   但与银甲女子 156–228 的结束点重合属原数据口径）。本轮不改数据，仅如实展示。

---

## 6. 测试

```
backend/tests/test_item_group_read.py            12 passed
backend/tests/test_item_generic.py
backend/tests/test_kg_item_holding_read.py
backend/tests/test_kg_item_holding.py
backend/tests/test_kg_item_endpoint.py
backend/tests/test_kg_mentions_alias.py
backend/tests/test_kg_canon_source.py
backend/tests/test_kg_alias_rules.py
backend/tests/test_kg_layers.py
合计 112 passed
```

新增回归：`test_full_timeline_keeps_closed_segment`（镇北王 99–106 必须可读）。
契约变更：组级查询只返回组级段 `[None]`；碎片级查询只返回该碎片 `["三号"]`；
generic 候选只给组级摘要。
