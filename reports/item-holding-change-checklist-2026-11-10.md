# 物品治理：要改哪几行、会撞什么（只读核对清单）

> 说明：下文出现的 `<temp>` 路径是当时本机临时脚本 / 副本 / 日志的位置，**这些脚本未入库**，仅作取证记录保留，不可直接复跑。

- 日期：2026-11-10
- 方式：**全程只读**。所有连接均以 `file:...?mode=ro&uri=true` 打开，未执行任何 INSERT/UPDATE/DELETE，未生成备份（因为没有写入）。
- 库：`backend/data/generated.db`（组/写法/持有段）+ `backend/novel_dev.db`（实体/提及）
- 相关脚本（临时，未入库）：`<temp>\audit_schema.py`、`audit_dump.py`、`audit_targeted.py`、`audit_sidebar.py`、`audit_gaps.py`、`audit_detail.py`、`audit_ddl.py`、`audit_scan.py`、`audit_ent.py`、`audit_read.py`、`audit_spear.py`、`audit_evidence.py`、`audit_mention2.py`

## 0. 现状基线（先给坐标，后面所有行号都落在这张表上）

| 对象 | 现状 |
|---|---|
| `kg_item_group` | 52 组：45 active + 7 generic（含 `玉石小镜`=46、`神兵`=95、`铜剑`=96、`刀`=97、`法器`=98、`法宝`=99） |
| `kg_item_surface` | 139 行：138 approved + 1 pending（pending 只有 `腰牌`=109） |
| `kg_item_holding` | 61 行，id 1–65，缺 13/14/56/57（见 §2.3） |
| `kg_holding_story_segment` | 4 行（神剑 3 行 + 朴刀 1 行） |
| `kg_item_instance` | 1 行（`地书` / `三号`） |
| `kg_entities`（project 5, item） | 45 行 |
| `kg_mentions` | **0 行**（schema 无 chapter 列：`id/project_id/entity_id/generation_id/segment_no/context_window/mention_kind`） |

**一句话结论**：`镇国剑` 不是独立组，是 `神剑` 组（id=24）下的一条 approved 写法；`地书` 的 7 条持有段里 6 条已经带 `instance_id=1`（三号），真正要改的是「组级 own」与「碎片 custody」在同一个组里混着返回；`铜锣/法器铜锣/长枪` 三个名字现在**已经在读通道里返回 ambiguous**，出表不会让"本来能查到的"变成查不到，反而会修好。

---

## 1. 镇国剑是不是独立组

### 1.1 事实

**不是。** `镇国剑` 是 `kg_item_surface.id=95`，`group_id=24`，`status='approved'`，`source='migration'`，`note='实体 aliases 迁移带入'`。同组还有 11 条写法（见 §2.1）。组 24 的规范名是 `神剑`。

读通道实测（`current_item_holdings(["镇国剑"], project_id=5)`）：

```
- 神剑：权属（own）归元景帝
```

即：查 `镇国剑` 返回的是 `神剑` 组，`via='surface'`，`item_id=26`（实体 26 的规范名是 `神剑`）。

### 1.2 三种做法与代价

| 方案 | 动几张表 / 几行 | 语义结果 | 主要风险 |
|---|---|---|---|
| **A. 拆出独立组「镇国剑」** | `kg_item_group` INSERT 1 行；`kg_item_surface` UPDATE 11 行（86,87,88,89,90,91,94,95,96,97,98）；`kg_item_holding` UPDATE 2 行（59,60）；`kg_holding_story_segment` UPDATE 3 行（1,2,3）。**合计 17 行** | 正确：`镇国剑` 独立成组，`神剑` 组只剩写法 24 | ① 组 24 只剩 `神剑` 一条写法，它对应 21 章里 3 个不同物品，必须同时决定它是 pending / generic / 仍 approved（不决定就会继续误挂）；② 实体 26 的 aliases 仍含 `镇国剑`，实体侧与组侧双轨不一致（见 §9.1） |
| **B. 只把组 24 改名「镇国剑」** | `kg_item_group` UPDATE **1 行**（id=24） | 组名对了，但写法 `神剑`（id=24）仍 approved 指向该组 | ① `神剑` 精确 21 章里有 12 章是洛玉衡祖传剑/佛门黄金剑，会被全部算成镇国剑；② 实体 26 规范名仍是 `神剑`，读通道输出 `镇国剑` 而别名表说 `神剑`，`kg_mentions` 一旦有数据两边会对不上 |
| **C. 维持现状** | **0 行** | 继续把 `镇国剑/镇国宝剑/镇国神剑/黄铜剑` 都报成 `神剑` | 已知错误归属保留；且读通道只显示「元景帝 own 106–∞」一条，看不到 `镇北王 99–106`（见 §9.6） |

**约束核对（A/B 都适用）**

- `kg_item_group`：`UNIQUE(project_id, kind, canonical_name)`。组名 `镇国剑` 当前**未被占用**（52 组里没有同名），A 的 INSERT 不会撞；B 的改名也不会撞。
- `kg_item_surface`：`UNIQUE(project_id, surface)`。A 只改 `group_id`，不改 surface 文本，不会撞。
- `kg_item_holding`：`ux_item_holding_open` 唯一键是 `(COALESCE(item_id,-1), item_name, hold_type, COALESCE(instance_id,-1)) WHERE valid_to IS NULL`。A 把 59/60 的 `item_name` 改成 `镇国剑` 后，开区间行只剩 60 一条 → 不撞；**但若先插一条 `镇国剑` 开区间行再改 60，就会撞**。
- `kg_holding_story_segment`：`ux_story_segment_holding` 唯一键是 `holding_id`，跟随 59/60 一起改组即可；`ux_story_segment_branch` 只作用于 `holding_id IS NULL` 的行（当前只有 id=1 的神剑回溯行），A 把它的 `item_name` 改成 `镇国剑` 后键值变化，不撞；**若同时再补一条 `镇国剑` 回溯行且 `order_key=0`，会与 id=1 撞**。

### 1.3 「镇国剑 82 章」重算要动多少行

**0 行可写，因为现有 schema 没有章级容器。** 依据：

- `kg_mentions` 0 行且无 `chapter` 列（按 `segment_no` 存），无法表达「82 章」；
- 全书 82 章这个数是**出现章数**，不是持有段数；库里对应的持有叙事只有 `kg_item_holding` 2 行（59、60）+ `kg_holding_story_segment` 3 行；
- 真要落章级，需要新增表/列（例如「章号 + 写法 + 指称对象」），这是 schema 变更，不在本次清单的只读范围。

所以：「重算 82 章」在当前结构下的量级是 **2 + 3 = 5 行**；如果要求逐章留痕，则是 **新增 schema + 82 行**。

---

## 2. 「神剑」组（id=24）下面挂了什么

### 2.1 全部写法（12 条，全部 approved，全部 `source='migration'`）

| surface id | 写法 | 备注 |
|---|---|---|
| 24 | 神剑 | 规范名；21 章里 9 章镇国剑 / 9 章洛玉衡 / 3 章佛门 |
| 86 | 大奉镇国宝剑 | |
| 87 | 庙里供奉着的神剑 | ch97/98 |
| 88 | 开国帝君当年征战沙场使用的佩剑 | |
| 89 | 开国皇帝当年使用过的佩剑 | ch97 |
| 90 | 斩天灭仙的神剑 | ch95 |
| 91 | 玄武神剑 | |
| 94 | 镇压气运的神剑 | ch122 |
| 95 | 镇国剑 | 82 章，规范名候选 |
| 96 | 镇国宝剑 | |
| 97 | 镇国神剑 | |
| 98 | 黄铜剑 | 16 章；ch98 是「触摸」不是持有 |

无 pending、无 rejected。

### 2.2 全部持有段（2 条现存 + 1 条已删）

| id | item_name | hold_type | owner | 区间 | reason_code | 现状 |
|---|---|---|---|---|---|---|
| 59 | 神剑 | own | 镇北王 | 99–106 | gift | **保留**（按用户口径算持有） |
| 60 | 神剑 | own | 元景帝 | 106–∞ | transfer | 现存；读通道当前只显示这一条 |
| 56 | 黄铜剑 | own | 元景帝 | 98–∞ | explicit_hold | **已 DELETE**，快照在 `backend/migrations/2026-11-13-item-holding-void-v1/holding_56_snapshot.json`（理由：ch98 只是触摸） |

补充：`kg_holding_story_segment` 里 3 行都挂组 24（id=1 回溯「大奉开国皇帝」ch95、id=2 挂 holding 59、id=3 挂 holding 60）。

### 2.3 `kg_item_holding` 缺号说明（避免下次误判为"丢数据"）

- 13、14：`2026-11-10-item-holding-v3/apply_item_merge.py` 删除（合并用）
- 56：`2026-11-13-item-holding-void-v1/void_touch_holding.py` 删除（黄铜剑触摸）
- 57：`2026-11-10-item-holding-v3/apply_segment_precision.py` 删除（黑金刀与黑金长刀重复，`[94,∞)` 被覆盖）

### 2.4 实体侧

`kg_entities.id=26`，canonical `神剑`，mention_count=13，ch95–122，aliases 11 条：`大奉镇国宝剑 / 庙里供奉着的神剑 / 开国帝君当年征战沙场使用的佩剑 / 开国皇帝当年使用过的佩剑 / 斩天灭仙的神剑 / 玄武神剑 / 镇压气运的神剑 / 镇国剑 / 镇国宝剑 / 镇国神剑 / 黄铜剑`。

→ 只要这 11 条 alias 不动，任何按 alias 的匹配（`canon_source.text_mention`、`_index_mentions_only`）都会把 `镇国剑` 归到 `神剑`，与组侧拆分脱节。

---

## 3. 「地书」降成组级

### 3.1 现有持有段逐行（7 行，全部 group_id=9）

| id | hold_type | owner | owner_kind | 区间 | instance_id | 处置建议 |
|---|---|---|---|---|---|---|
| 15 | own | 天地会 | faction | 74–∞ | NULL | **留在组级**。这是唯一一条"本体/组织权属"行，语义正确 |
| 12 | custody | 金莲师兄 | character | 0–51 | 1 | 改挂「三号」：**已经是 instance=1，无需改行**，只需在规范名/注释上明确它是碎片 |
| 32 | custody | 许七安 | character | 51–74 | 1 | 同上 |
| 64 | custody | 魏渊 | character | 74–76 | 1 | 同上 |
| 65 | custody | 许七安 | character | 76–105 | 1 | 同上 |
| 33 | custody | 许新年 | character | 105–108 | 1 | 同上 |
| 34 | custody | 许七安 | character | 108–∞ | 1 | 同上，开区间 |

**结论：7 行里没有一行需要"作废重登"。** 6 条 custody 早已带 `instance_id=1`，组级 own 也只有 1 条。真正的问题是**读通道把组级和碎片级一起返回**（见 3.3），不是数据错挂。

### 3.2 「三号地书碎片」有没有自己的组

- **没有独立组。** 现有机制是：`kg_item_instance.id=1`（`item_name='地书'`, `instance_label='三号'`）+ 组 9 下 3 条带 `instance_label='三号'` 的写法：`61 三号碎片`、`62 地书碎片·三号`、`63 叁号镜子`（均 approved）。
- **建议不新建组。** 理由：`instance_id` 已经能做寻址与拆分渲染（读通道输出 `地书·三号` 独立一条），新建组会要求把 6 条 custody 行、3 条写法、1 条 instance 全部迁移，且与 `kg_item_instance` 语义重复。
- 若坚持新建组：需 UPDATE 3 surface（61/62/63）+ 6 holding（12/32/33/34/64/65）共 **9 行**，并把 instance 的 `item_id`/归属一起改；同时 `ux_item_holding_open` 会因为 `item_name` 变化而重新计算键，需确认不与组 9 的开区间行冲突。

### 3.3 「地书碎片」「地书聊天群」现在各挂了什么

两者**都不是独立规范名，也没有独立组/实体**：

| 名字 | 实际挂载 | 后果 |
|---|---|---|
| 地书碎片 | `kg_item_surface.id=57`（group 9, approved）+ 实体 9 的 alias | 查询"地书碎片"返回**组 9 全部段**，包括组级 `own 天地会` |
| 地书聊天群 | `kg_item_surface.id=58`（group 9, approved）+ 实体 9 的 alias | 同上；且它根本不是物品，是通讯群 |

读通道实测（`地书碎片` / `三号碎片` / `地书` 三个输入返回完全一致）：

```
- 地书：权属（own）归天地会
- 地书·三号：实物（custody）在许七安手上
```

→ 这就是"降成组级"要解的点：查碎片时不该出现"天地会 own"。最小改法是让 `地书碎片` 不走 surface（转 generic 或 rejected），并把组级 own 的渲染与 instance 行分开；不需要动那 7 行数据。

---

## 4. 玉石小镜归类确认

### 4.1 现状：**已经在通称侧，但有三处漏口**

| 位置 | 内容 |
|---|---|
| `kg_item_group.id=46` | canonical `玉石小镜`，`status='generic'`，`generic_members='[9]'`，note 明确写「九块碎片是九面同款镜子（ch596），是类通称，不对应单件」 |
| group 46 写法 | `138 玉石小镜`、`278 玉石镜`，均 approved |
| 实体 9 aliases | 含 `玉石碎片`、`枕头底下的玉石小镜`、`那面玉石小镜` |

读通道实测（正确）：

```
- 玉石小镜：九块碎片是九面同款镜子…是类通称，不对应单件；候选：地书（权属归天地会；实物在三号…许七安）
```

### 4.2 三处漏口（会被当成单件地书）

| surface id | 写法 | 所在组 | 问题 |
|---|---|---|---|
| 65 | 玉石碎片 | 9（单件组，approved） | 类名被登记成单件写法 |
| 66 | 枕头底下的玉石小镜 | 9（approved） | 带修饰的镜类通称，绕过 generic 判定 |
| 67 | 那面玉石小镜 | 9（approved） | 同上 |

这三条**不是**指向"地书碎片"的专名，是泛指"那面镜子"。查询它们会直接进组 9，返回 `地书` 的持有事实，绕过 group 46 的通称保护。

另：实体 9 的 3 条 alias（`玉石碎片`/`枕头底下的玉石小镜`/`那面玉石小镜`）与上述写法重复，属"双轨"，改一处不改另一处会出现两条路径答案不同。

---

## 5. 铜锣 / 法器铜锣 / 长枪 出表影响面

### 5.1 「品级不算物品」的判定依据

- `kg_item_holding.id=43`（`铜锣` own 许七安 189–∞）的证据原文是：**「吏员道：'铜锣许七安随行'」** —— 这是职务称谓，不是持有动作。
- 检索日志 `evidence` 里 3 条「铜锣」全是等级/标志：`胸口绣着的银锣、铜锣标志`、`这位铜锣`、`铜锣许七安随行`。
- 组 32 的 8 条写法里 6 条是人物修饰语（`俊朗不凡的铜锣`、`新来的铜锣`…），本身就是按"人"写的。

### 5.2 逐条移除清单

| 组 | 表 | 行 | 动作 | 依赖 |
|---|---|---|---|---|
| 铜锣（32） | `kg_item_surface` | 32, 114, 115, 116, 117, 118, 119, 120（**8 行**） | DELETE | 先于删组 |
| 铜锣（32） | `kg_item_holding` | 43（**1 行**） | DELETE（建议先按 §8 快照留档） | 先于删组 |
| 铜锣（32） | `kg_item_group` | id=32（**1 行**） | DELETE | 最后 |
| 铜锣（32） | `kg_entities` | id=34（**1 行**） | DELETE（或保留为非 item 的别名容器，但 item 化是错的） | 与组删除无先后，但要在 mentions 写入前 |
| 法器铜锣（18） | `kg_entities` | id=19 的 aliases 去掉 `铜锣`（**1 行**） | UPDATE | 必须在删实体 34 之后（否则 `铜锣` 会落到 19 上反而变唯一） |
| 长枪（36） | `kg_item_surface` | 36 → 改挂 group 33（**1 行**） | UPDATE group_id | 先于删组 36 |
| 长枪（36） | `kg_item_holding` | 48 → `item_name='银枪'`, `group_id=33`, **`valid_to=228`** | UPDATE | **必须先设 valid_to**，见 5.3 |
| 长枪（36） | `kg_item_group` | id=36（**1 行**） | DELETE | 最后 |
| 长枪（36） | `kg_entities` | id=38（**1 行**） | DELETE | — |
| 银枪（33） | `kg_entities` | id=35 的 aliases 去掉 `长枪`（**1 行**） | UPDATE | 在删实体 38 之前或之后均可；建议在之后，避免中间态双命中 |

合计：**17 行**（surface 9 + holding 2 + group 2 + entity 3 + alias 1）。

### 5.3 撞约束风险（这是本清单里唯一会直接报错的地方）

`ux_item_holding_open`：
```
UNIQUE (COALESCE(item_id,-1), item_name, hold_type, COALESCE(instance_id,-1)) WHERE valid_to IS NULL
```
- 现存 `id=45`：`银枪 / own / 228–∞ / instance NULL`（开区间）。
- 若把 `id=48` 直接改成 `item_name='银枪'` 且保持 `valid_to=NULL` → **唯一键冲突，UPDATE 直接失败**。
- 必须先 `valid_to=228`（48 是 227 的重复登记，228 已交接给杨砚），再改名字与组。
- 同理：`长枪` 组删除前若先删组、后改 48，会因为 `ON DELETE` 无外键约束而**静默留孤儿**（见 §9.5）。

### 5.4 读通道 / 别处引用会不会被打断

| 检查点 | 结果 |
|---|---|
| `kg_mentions` | **0 行**，无引用，不会打断 |
| `kg_nodes` / `kg_edges` / `facts` / `kg_relations` | 全库字符串扫描：**0 命中** |
| `evidence` 检索日志 | 5 条命中（3 铜锣、2 镇国剑），只是历史检索留痕，**不参与裁决** |
| 读通道当前行为 | 三个名字**现在全部返回 ambiguous（0 条 line）**：`铜锣 → [法器铜锣, 铜锣]`、`法器铜锣 → [法器铜锣, 铜锣]`、`长枪 → [银枪, 长枪]`。出表后分别变成：`铜锣` 无记录、`法器铜锣` 归许七安、`长枪` 归银枪（杨砚） |
| 反向影响 | **没有"本来能查到"的会变查不到**——因为它们现在都查不到。出表是净修复 |
| `resolve_group_id`（写入口） | 只认 `g.status='active'`，删组后新写入不会再挂到 32/36；建议长枪改走 33，而不是留空 |

---

## 6. 腰牌现状（只列影响面，不执行）

| 对象 | 现状 |
|---|---|
| `kg_item_surface.id=109` | `腰牌`，`group_id=29`（金牌），**`status='pending'`**（全库唯一 pending），note 已写明「与金牌不是同一件」 |
| `kg_entities.id=31` | canonical `金牌`，aliases **含 `腰牌`**，mention_count=12，ch108–158 |
| 持有段 | `kg_item_holding.id=40`：`金牌` own 许七安 108–∞（`reason_code='gift'`） |
| 读通道实测 | `腰牌 → 金牌：权属归许七安`，`via='entity'`（走实体别名，不走 surface） |

**"不合并"落地的影响面（3 行）**

1. `kg_entities.id=31` aliases 去掉 `腰牌` → **1 行 UPDATE**。之后 `腰牌` 在实体侧无归属。
2. surface 109 保持 `pending` 即可（pending 不参与 `name_to_group`），**0 行**。
3. 结果变化：`腰牌` 从「金牌归许七安」变成「无记录（no_record）」。这不等于"腰牌无主"，只是没登记。
4. 若之后决定"降成泛称"：需要新建或复用一个 generic 组，把 surface 109 移过去，`generic_members` 留空或指向 29 → **1 INSERT + 1 UPDATE**；`resolve_group_id` 会自动返回 None（它只认 active），写入侧不会误挂。

**撞约束风险**：无。`kg_item_surface` 唯一键是 surface 文本，移组不改文本；实体 aliases 是 JSON，无唯一约束；但去掉 alias 后要检查是否还有别的实体也挂 `腰牌`——扫描结果：**只有实体 31**。

---

## 7. 依赖顺序（如果未来要执行，按这个顺序）

1. **备份**：`generated.db` + `novel_dev.db`（沿用 `backend/backups/` 命名 `*-pre-<动作>-<时间戳>.db`）。
2. **快照要删的行**：每一条 DELETE 前把整行落 JSON（照 `holding_56_snapshot.json` 的格式），便于回滚。
3. **先改持有段、后删组**：`kg_item_holding.group_id` **没有外键**，SQLite 默认也不开 `foreign_keys`，删组不会报错，只会留孤儿。顺序必须是「改挂/删除持有段 → 改挂写法 → 删组」。
4. **长枪必须先关区间**：`id=48` 先 `valid_to=228`，再改名改组。
5. **别名最后动**：先删实体 34（铜锣）、实体 38（长枪），再清理实体 19 的 `铜锣` alias、实体 35 的 `长枪` alias，避免中间态出现"唯一命中"。
6. **拆镇国剑组**：先建组 → 移写法 → 改持有段 → 改 story_segment → 最后决定写法 24「神剑」的 status。
7. **每组操作完跑一次读通道**（`current_item_holdings`）比对输出，不要只看 SQL 生效。

## 8. 回滚

| 动作 | 回滚方式 |
|---|---|
| 删行（holding/surface/group/entity） | 按 JSON 快照逐字段 INSERT 回原表；`id` 显式写回（表是 `INTEGER PRIMARY KEY AUTOINCREMENT`，可显式指定 id） |
| 改 `group_id` | 快照里记原 `group_id`，UPDATE 回 |
| 改 `valid_to` | 快照里记原值（NULL），UPDATE 回 NULL |
| 拆组 | 删新组 → 写法/持有段/旁注 `group_id` 改回 24 → 删组 |
| 整库级 | `backend/backups/*-pre-*.db` 覆盖回 |

注意：`kg_item_holding` 的 `ux_item_holding_open` 会阻止"先插新行再回滚旧行"的操作，回滚也要按「先关/删新开区间行，再恢复旧行」的顺序。

---

## 9. 清单外的新问题（单列，不塞进正题）

### 9.1 实体 aliases 与组 surfaces 是两套真源，已经不一致

实体 26（`神剑`）11 条 alias 与组 24 的 12 条 surface 内容基本相同但**不是同一张表**。拆组时只改组、不改实体，会让：
- 按 surface 解析（读通道）得到 `镇国剑`；
- 按 alias 匹配（`canon_source`、`_index_mentions_only`）得到 `神剑`；
两边对同一段文本给出不同规范名。

### 9.2 `铜锣` ↔ `法器铜锣` 是**双向别名循环**

- 实体 19（`法器铜锣`）aliases = `["铜锣"]`
- 实体 34（`铜锣`）aliases 含 `法器铜锣`

结果：两个名字都命中 2 个实体 → 读通道对两者都返回 `ambiguous`。只删一边不能解，必须两边同时清（实体 34 整体删除，实体 19 去掉 `铜锣`）。

### 9.3 `长枪` 的 `id=48` 与 `银枪` 的 `id=45` 是"双开区间"

同一支枪（李妙真 227 握枪 → 228 交给杨砚），库里有两条开区间记录，分属两个组、两个 item_name，唯一索引不认为冲突（因为 `item_name` 不同）。这是唯一索引管不到的重复，只能靠人工合并。

### 9.4 `地书聊天群` 被登记成物品写法（surface 58，approved）

它是通讯渠道，不是物品。查询它会返回地书的持有事实。建议转 generic 或 rejected——但**注意**：`地书` 的 295 章子串里有 71 章是通讯类，若把通讯写法全剔掉，`地书` 的可检索面会从 295 掉到 89（已核过）。这是产品取舍，不是 bug。

### 9.5 `kg_item_holding.group_id` 没有外键

DDL 里只有 `kg_item_surface.group_id REFERENCES kg_item_group(id)`；持有表的 `group_id` 是裸 `INTEGER`。加上 SQLite 默认 `PRAGMA foreign_keys=OFF`，**删组不会级联、不会报错**，只会留下 `group_id` 指向不存在组的行，而读通道按组取段时这些行会永久不可见（不是"无记录"，是"消失"）。任何删组操作前必须跑一次孤儿检查：

```sql
SELECT h.id, h.item_name, h.group_id FROM kg_item_holding h
LEFT JOIN kg_item_group g ON g.id = h.group_id
WHERE h.group_id IS NOT NULL AND g.id IS NULL;
```

### 9.6 读通道只返回"每个 hold_type 的最新一段"，历史段不可见

`_facts_from_rows` 对同一 `(instance_id, hold_type)` 只保留 `order_key` 最大的一条。因此：
- `镇北王 own 99–106`（id=59）在任何查询里都不会出现，只显示 `元景帝 own 106–∞`（id=60）；
- 这意味着"保留 id=59"对当前输出**没有可见影响**，但它对将来做时间线是必要的；
- 反过来，如果误删 id=59，当前输出不会有任何变化 —— 容易被误判为"删了也没事"。

### 9.7 `kg_mentions` 全表为空，任何"按提及归属"的迁移都没有依据

当前 45 个 item 实体的 `mention_count` 是历史值（例如实体 26 写 13，而 `镇国剑` 文本 82 章），与 `kg_mentions` 的 0 行不一致。凡是用 `mention_count` 判断"这个名字有多少戏份"的推论，目前都不可靠。

### 9.8 `神剑` 组里混了三种"同名不同物"

组 24 的写法 24（`神剑`）同时覆盖：镇国剑（9 章）、洛玉衡祖传剑（9 章）、佛门金身黄金剑（3 章）。这三者没有任何交接关系。只要 surface 24 还是 approved 指向一个单件组，无论组名叫什么，都会把 12 章错误归属。**这是 §1 方案 A 必须同时解决的问题，也是方案 B 无法解决的问题。**
