# 项目 6/7 清理与 `kg_item_group.novel_id` 只读评估

> 说明：下文出现的 `<temp>` 路径是当时本机临时脚本 / 副本 / 日志的位置，**这些脚本未入库**，仅作取证记录保留，不可直接复跑。

日期：2026-11-11  
范围：`backend/novel_dev.db`（应用库，已改）、`backend/data/generated.db`（生成库，未改）  
相关：`reports/item-residual-surface-audit-2026-11-10.md` §14（`许七安的佩刀` 维持摘除）

---

## A. 删除验证项目 6/7

### A.1 判定：两个都是验证残留，不是真实写作内容

| 项目 | 标题 | 建库时间 | 引用书 | 生成段落 | 用户写作版本 |
|---|---|---|---:|---:|---:|
| 6 | `【验证·勿删】物品持有-held-20261110` | 2026-11-10 14:26:13 | [5, 6] | 2 段（743 / 1284 字符） | `versions` 0 行 |
| 7 | `【验证·勿删】物品持有-held-2-20261110` | 2026-11-10 14:30:12 | [5, 6] | 2 段（687 / 772 字符） | `versions` 0 行 |

- 标题自带「验证·勿删」；创建时间相隔 4 分钟；两段正文是 AI 生成的验证稿（项目 6 seg1 写李妙真/楚州、seg2 写夏诺/小落，等于把两本参考书混着跑；项目 7 两段都在写「许七安在梁上盯着镇国剑」）。
- `versions` 全表 0 行、`edit_proposals` 0 行 → 没有任何人工修订内容。
- 结论：**可以删**，未触发「停下先报」。

**项目 6 那 2 条卡 `running` 的抽取行**（`agent_runs`）：

| id | project | role | status | started_at | finished_at | note |
|---:|---:|---|---|---|---|---|
| 77 | 6 | kg_batch_extract | running | 2026-11-10 14:27:36 | 空 | `{"segment_no":1,"chapter":1}` |
| 78 | 6 | kg_batch_extract | running | 2026-11-10 14:28:06 | 空 | `{"segment_no":2,"chapter":2}` |
| 80 | 7 | kg_batch_extract | running | 2026-11-10 14:34:02 | 空 | `{"segment_no":2,"chapter":2}` |

都是 2026-11-10 下午那次验证跑批中途断掉留下的（`model` 为空、无 `finished_at`），项目 7 另有一条 79 是 `success`（`entities_upserted=5`）。

### A.2 备份

| 项 | 值 |
|---|---|
| 文件 | `backend/backups/novel_dev-full-20261111-001537.db` |
| 方式 | 先 `PRAGMA wal_checkpoint(TRUNCATE)`，再用 sqlite3 `backup()` API 整库拷贝 |
| 生成库 | 未备份——`generated.db` 里项目 6/7 行数为 0，本次不动 |

### A.3 删除范围与行数

只删 `project_id IN (6,7)` 的行，共 **62 行**：

| 表 | 行数 |
|---|---:|
| `agent_runs`（含上面 3 条 running） | 4 |
| `facts` | 16 |
| `kg_nodes` | 16 |
| `kg_events` | 6 |
| `kg_entities` | 5 |
| `kg_mentions` | 5 |
| `kg_relations` | 4 |
| `generations` | 4 |
| `projects` | 2 |

未动：`raw_novels`、`novel_chunks`（两本书是项目 5 也在用的共享语料）、`app_settings`（不引用 6/7）。

### A.4 孤儿检查

| 检查 | 结果 |
|---|---|
| 各带 `project_id` 表指向不存在项目 | 0 |
| `kg_events.subject_id/object_id/location_id/time_anchor_id` → `kg_entities` | 0 |
| `kg_mentions.entity_id` → `kg_entities` | 0 |
| `kg_edges.from_id/to_id` → `kg_nodes` | 0（删除前也确认过：没有任何 edge 引用 6/7 节点） |
| `evidence.run_id` → `agent_runs` | 0（删除前已确认） |

**孤儿合计 0。**

### A.5 复核

| 项 | 结果 |
|---|---|
| 剩余项目 | 1、2、3、4、5（标题分别是 Assistant 测试项目、调用链验收项目 ×3、`4`；1–4 也都是空壳测试项目，但不在本次范围，未动） |
| `镇国剑` 实体 | 只剩 `(id=26, project_id=5)`；**项目 7 的实体 68 已消失** |
| 读通道（项目 5） | `镇国剑` → `held`、gid=100、`via=surface` |
| 生成库 | `kg_item_group` 53、`kg_item_surface` 134、`kg_item_holding` 60、`kg_holding_story_segment` 4 —— 删除前后完全一致 |
| p1 seg1 / seg2 | `d056037b…` / `a2e9207c…` 不变 |
| p5 | `6c15c704…`、1018 字符不变 |
| p7 seg1 | `3ea63136…` → **空串 md5 `d41d8cd…`**，因为项目 7 整条已删，这条旧守卫随项目一起退役；镇国剑的守卫现在由项目 5 承担（见上） |

关于「133 写法」：`kg_item_surface` 现在是 **134 行**，等于既有 133 条 + 本次会话新增的 `太平刀` 1 条；删除项目 6/7 没有增删任何写法。

`project_id=None` 那条路径：代码里仍然显式拒绝（抛「必须指定项目」），**行为不变**；但它当初存在的理由——项目 5 的实体 26 与项目 7 的实体 68 同名「镇国剑」——已经随实体 68 一起消失。现在全库同名实体只剩一个，将来是否放宽这条守卫是独立决定，本次不动。

---

## B. `许七安的佩刀` 维持摘除

见 `reports/item-residual-surface-audit-2026-11-10.md` §14：不动库，`surface id=137` 保持 `rejected`；它只在 ch335、ch423 出现，两章分别有 `黑金长刀`、`太平刀` 兜底，摘不摘都是 125 章覆盖，恢复无收益。

---

## C. `kg_item_group.novel_id`：只读评估（未改 schema）

### C.1 现状：只有一个项目挂了不止一本书

| 项目 | 标题 | 引用书数 | 组数 |
|---|---|---:|---:|
| 1 | Assistant 测试项目 | 0 | 0 |
| 2 | 调用链验收项目 | 0 | 0 |
| 3 | 调用链验收项目 | 0 | 0 |
| 4 | 调用链验收项目 | 0 | 0 |
| 5 | `4` | **2**（《大奉打更人》925 章、《豪门宠婚，爵少你别闹》603 章） | **53** |

全库只有项目 5 有物品组，且它引用了两本书。**没有一条组是因为跨书而落错的**——53 组全部落在《大奉打更人》。所以这是**预防性改造，不是修 bug**；但要承认潜在风险是真的：两本书的章号都从 1 开始，1–603 完全重叠，`kg_item_surface.evidence_chapter` 只存章号不存书，ch93 这种小章号在两本书里都能对上。

### C.2 回填：53 组能全部唯一归书

按「写法/规范名在每本书 `novel_chunks` 里的独立章命中数」推（只读跑过一遍）：

| 归属 | 组数 | 说明 |
|---|---:|---|
| 唯一落书 5 | 47 | 只在《大奉打更人》出现 |
| 双命中但 5 压倒性 | 4 | `玉扳指` 7:1、`锦囊` 38:1、`刀` 571:60、`法宝` 88:1——书 6 那 1 章是同名的另一件东西（如别的「锦囊」），不影响判定 |
| 两本书都 0 命中 | 2 | `洛玉衡祖传神剑`、`佛门金身黄金神剑`——正文里没有字面命中，但两组的 note 都写明是从「神剑」组拆出的子项，来源链 → 书 5 |

- 8 个 `status='generic'` 的组（`神剑`/`玉石小镜`/`长刀`/`神兵`/`铜剑`/`刀`/`法器`/`法宝`）建议 **`novel_id` 留 NULL**，语义写死为「项目级/泛称组，不做书过滤」——它们是类名，本来就不属于某一本书。
- 也就是说回填可以做到 **53/53 有明确归属（45 → 5，8 → NULL）**，没有需要人工仲裁的残余。

### C.3 牵连：改动点清单

| 文件 | 现状 | 要改什么 |
|---|---|---|
| `app/kg/item_holding_read.py` `_load_groups()` | `SELECT ... FROM kg_item_group WHERE project_id IN (...)`；`by_surface`、`name_to_group`、`group_by_canonical` 全部**以名字为唯一键** | SELECT 加 `g.novel_id`；三张字典改成按 `(novel_id, name)` 分槽（或调用方传 `novel_id` 时先过滤）；`conflicts`（同名跨组）要按书作用域判，否则跨书同名会全被报成冲突 |
| `app/kg/item_holding_read.py` `_resolve_entities()` | 实体按 `project_id` 取，没有 novel 维度 | 组划到书之后，实体仍是项目级：同名实体在项目内仍会合并。要做到真书级隔离，`kg_entities` 也得加 `novel_id`；否则这里保持不动，接受「组按书、实体按项目」 |
| `app/kg/generated_store.py` `resolve_group_id()` | **没有 project_id、也没有 novel 过滤**，`ORDER BY pri, gid LIMIT 1` 取第一条 | 这是写侧最危险的一处：一旦第二本书有了组，写持有行可能挂到另一本书的组。必须加 `novel_id` 过滤（或至少加 project 过滤） |
| `app/kg/batch_worker.py` | 不建组；`_item_names(db, project_id)` 取项目实体名 | 组加 novel 不需要改它；只有实体也加 novel 时才要动 |
| `app/kg/canon_source.py` `fold_item_aliases()` | 全局 alias 折名，纯字符串，无项目/书维度 | 两本书同名写法会被折到同一个规范名。若做书级隔离，这里要加 novel 白名单；不做则保持（它只影响写法选择，不直接改组） |
| `migrations/2026-11-11-item-group-v1/apply_item_group.py` | 建组/建写法，带 `UNIQUE(project_id, kind, canonical_name)` | 唯一键要扩成含 `novel_id`；SQLite 不能 DROP 表级 UNIQUE，需 12-step 重建表（53 行，成本 <1s） |
| `kg_item_surface` | `UNIQUE(project_id, surface)`；读侧 `by_surface` 以 surface 为键 | 组按书拆后，同一写法（如「玉佩」）在两本书里无法各挂一个组。要支持就得给 surface 也加 `novel_id` 并改唯一键；不改就等于「一个写法全项目只能属于一个组」 |
| `kg_item_holding` / `kg_item_instance` | **既无 project_id 也无 novel_id**；`ux_item_holding_open` = `(item_id,item_name,hold_type,instance_id) WHERE valid_to IS NULL` | 两本书都有「镇国剑」的开放持有时会撞唯一键。真要跨书，这两张表也得加列——比 group 加列大得多 |

`UNIQUE(project,kind,canonical_name)` 的结论：**要改**。否则两本书都有「玉佩」时第二条组插不进去。

### C.4 回滚与灰度

- 加列用 `ALTER TABLE kg_item_group ADD COLUMN novel_id INTEGER NULL`，秒级、不重写表、老代码无感。
- 读侧先不动：所有查询继续忽略该列，行为零变化；回填只写 53 行。
- 观察一轮后再切换，切换面就两处：`_load_groups` 与 `resolve_group_id`，都带开关（`novel_scope=True/False`）。
- 回滚：当前 SQLite 3.49.1 支持 `ALTER TABLE ... DROP COLUMN`；即便不删列，NULL 语义「不做书过滤」也能让旧行为继续成立。快照 + 单步回滚沿用现有 `apply_cleanup.py` 的模式。
- 灰度期要写死一条：**NULL = 项目级/泛称，不做书过滤**，避免「NULL 与 5 混用」时出现两种语义。

### C.5 建议：先做一半

| 选项 | 理由 |
|---|---|
| **先做一半（推荐）**：加可空列 + 回填 53 行 + 不读 | 现在没有一条组落错书，属预防；但列和回填是一次性小成本，先落地可以让将来「第二本书真的产生组」时只改读侧两处，不用把「拆表重建唯一约束 + surface/holding 加列」捆在一起做 |
| 现在全做 | 要重建 `kg_item_group` 表、给 `surface`/`holding`/`instance` 加列并重建两个唯一索引，改动面覆盖生成库四张表；而当前收益是 0 条纠错，风险收益不划算 |
| 不做 | 账面上现在确实没有问题；但一旦项目 5 开始为《豪门宠婚》建物品组，`resolve_group_id` 的「取第一条」会静默挂错书，属于会留下脏数据的那类问题 |

## D. `kg_item_group.novel_id`：加列 + 回填（执行记录，读侧未切换）

执行脚本：[apply_novel_id.py](backend/migrations/2026-11-13-item-group-novel-id-v1/apply_novel_id.py)（`dry-run` 默认 / `--apply` / `--rollback`）。
快照：[group_novel_id_20261111-002928.json](backend/migrations/2026-11-13-item-group-novel-id-v1/snapshots/group_novel_id_20261111-002928.json)。
备份：`backend/backups/generated-full-novel-id-20261111-002928.db`、`backend/backups/novel_dev-full-novel-id-20261111-002928.db`（先 `PRAGMA wal_checkpoint(TRUNCATE)` 再 `sqlite3.backup()` 整库拷贝）。

### D.1 为什么只做一半

| 判断 | 依据 |
|---|---|
| 现在没有一条组落错书 | 53 组全部只在《大奉打更人》有压倒性命中；按项目 5 的两本书（5 vs 6）看，只有 4 组双命中且都是 7:1 以上的碾压 |
| 但风险是真的 | 两本书章号 1–603 完全重叠，`kg_item_surface.evidence_chapter` 只存章号；`resolve_group_id()` 既无 project 也无 novel 过滤、`ORDER BY pri, gid LIMIT 1`，一旦第二本书建组就会静默挂错 |
| 所以先落「标注」 | 加列 + 回填是一次性小成本（秒级、不重写表、老代码无感），把「事实」先固化；读侧一改就要同时动三处名字字典、`conflicts`、`resolve_group_id` 和两三个唯一索引，捆在一起做回归面太大 |
| 先把语义写死 | `NULL` = 项目级/泛称，不做书过滤；非 NULL = 指向 `novel_dev.raw_novels.id` 的标注。避免将来「NULL 与 5 混用」出现两套语义 |

本批**只加列 + 回填**：`item_holding_read.py`、`batch_worker.py`、`canon_source.py` 与生成层代码一行未改，`novel_id` 不参与任何 `WHERE`。

### D.2 目标表与加列方式

- 指向：`novel_id` → `novel_dev.db` 的 `raw_novels.id`（`INTEGER PRIMARY KEY`，SQLAlchemy `Project`/`Novel` 侧为自增整数）。
- **没有 FK**：`kg_item_group` 在 `generated.db`，`raw_novels` 在 `novel_dev.db`，两库物理隔离、不 `ATTACH`，SQLite 无法跨库建外键；且 `ALTER TABLE ... ADD COLUMN` 本身也不支持带 FK/UNIQUE。做法 = 只声明逻辑引用，回填时由脚本校验取值必须存在于 `raw_novels`。
- 加列语句：`ALTER TABLE kg_item_group ADD COLUMN novel_id INTEGER`（可空、无默认值、无索引）。

### D.3 回填规则与逐条依据

规则（按优先级）：`status='generic'` → NULL；否则按「规范名 + 全部 approved 写法」在每本书 `novel_chunks` 的独立章命中数取最大值 → 命中最多且唯一领先的书；零命中但有来源链（note 记「从「神剑」组拆出」）→ 5。

**结果：53 行 = 45 行 → novel 5，8 行 → NULL。** 完整 53 行的 `id / 规范名 / status / novel_id / 各书命中数 / 依据` 都在快照 `rows` 与 `evidence_detail` 里。

4 个双命中（按项目 5 的两本书 5 vs 6）：

| gid | 规范名 | status | 回填 | novel 5（章号区间） | novel 6（章号区间） | 比例 | 依据 |
|---|---|---|---:|---:|---|---:|---|
| 22 | 玉扳指 | active | 5 | 7 章（190–448） | 1 章（290） | 7:1 | 主书内连续出现；书 6 仅 1 章同名，是另一件东西 |
| 34 | 锦囊 | active | 5 | 38 章（152–838） | 1 章（104） | 38:1 | 另有《红楼梦》1 章（ch28）、《豆棚闲话》2 章（13–24），均非项目 5 的书 |
| 97 | 刀 | generic | **NULL** | 571 章（1–926） | 60 章（37–601） | — | 泛称组按状态规则留 NULL；纯器型词，跨书都有，本来就不指单件 |
| 99 | 法宝 | generic | **NULL** | 88 章（72–926） | 1 章（518） | 88:1 | 同上，纯类别词，留 NULL |

2 个零命中、按来源链归 5：

| gid | 规范名 | status | 回填 | 正文命中 | 依据 |
|---|---|---|---:|---|---|
| 101 | 洛玉衡祖传神剑 | active | 5 | 5 本书全 0 | note：「从「神剑」组拆出的独立一件（洛玉衡祖传），当前无持有段。」母组 `神剑`(gid24) 在《大奉打更人》命中 25 章、且只被项目 5 引用 → 子项随母组归 5 |
| 102 | 佛门金身黄金神剑 | active | 5 | 5 本书全 0 | note：「从「神剑」组拆出的独立一件（佛门金身黄金神剑），当前无持有段。」同上 |

另 5 个 active 组在**别的项目的书**里有 1–2 章偶然同名命中，不影响判定（这些书不属于项目 5）：`腰玉` 8:1（红楼梦）、`酒壶` 28:1（红楼梦）、`金钗` 5:2（红楼梦 2 章）、`银枪` 18:1（豆棚闲话）、`长剑` 27:1（戏中戏）——仍全部归 5。

8 个 `generic` 组留 NULL（`24 神剑`、`46 玉石小镜`、`47 长刀`、`95 神兵`、`96 铜剑`、`97 刀`、`98 法器`、`99 法宝`）：

- 它们都是类名/纯器型词（note 统一写「泛称：类名，不指单件」），不属于某一本书。
- 实测这 8 组名下 **0 条 `kg_item_holding`、0 条 `kg_holding_story_segment`**——是纯类档，没有「某本书的持有事实」挂在上面。
- 将来读侧怎么用：`NULL` 视为**项目级通配**——做书级过滤时，非 NULL 组只在对应书命中，NULL 组对任意书都有效（因为泛称词在两本书里都会出现）；判定「跨书同名冲突」时**不把 NULL 组算作冲突方**，因为它不代表某一本具体的书。这条语义现在就写死，切换时不需要再讨论。

### D.4 自检

| 项 | 结果 |
|---|---|
| `SELECT COUNT(*) WHERE novel_id IS NULL` | **8**（正好是 8 个 generic） |
| `SELECT DISTINCT novel_id WHERE novel_id IS NOT NULL` | `[5]` |
| 不在 `raw_novels` 的坏值 | `[]` |
| 按书计数 | novel 5 = 45，NULL = 8，合计 53 |

### D.5 验收（逐条给数）

| 项 | 改前 | 改后 | 结论 |
|---|---|---|---|
| 表结构 | 8 列 + `sqlite_autoindex_kg_item_group_1` | 9 列（仅多 `novel_id INTEGER`，`notnull=0`、无默认值）+ 同一个 `sqlite_autoindex_kg_item_group_1`（`unique=1, origin=u`） | 只新增一列 |
| 表 sql | — | 在改后 sql 文本里删掉 `, novel_id INTEGER` 后与改前**逐字一致**；`UNIQUE (project_id, kind, canonical_name)` 原样保留 | 约束零变化 |
| 其余 12 张生成表行数 | — | `kg_item_surface` 134 / `kg_item_holding` 60 / `kg_holding_story_segment` 4 / `kg_item_instance` 1 / … 全部不变 | 零变化 |
| gid45 覆盖章数 | 125 | **125** | 不变 |
| p1 两段 md5 | `d056037b0a0a56cee379646ca54e8e18` / `a2e9207ca2e028a05778b0dd8e884561` | 同左 | 不变 |
| p5 md5 | `6c15c704a8b78a04adfed097af76bf50` | 同左 | 不变 |
| 6 个探针 | `刀胚`/`太平刀`/`暗金色长刀`/`黑金刀`/`黑金长刀` → `held`/gid45/`via=surface`/lines=2（lines_md5 `617c601c…`）；`镇国剑` → `held`/gid100/`via=surface`/lines=2（lines_md5 `24e7c486…`） | 与改前**逐字一致**（整份探针 JSON 相等） | 读通道行为零变化 |
| 4 组 11 条持有行 `after_resolvable` | 全 true，`holding_bad=[]` | 全 true，`holding_bad=[]` | 红线通过 |
| 测试 | — | pytest 兼容集 **218 passed**；本批相关的 `test_item_generic`/`test_kg_item_holding_read`/`test_info_carrier` **37 passed**；脚本式 `test_entity_resolve` 42/42、`test_kg_name_resolution_kind` 13/13、`test_kg_cascade_protocol` 49/49、`test_batch_worker_observability` 37/37、`test_name_normalize_probe` 全过 | 全过 |

测试口径说明：仓库里 37 个测试文件是「脚本式」（模块顶层 `sys.exit()`），`pytest tests` 会在**收集阶段**就 `INTERNALERROR`（`test_batch_worker_observability.py`、`tests/stress/test_v30_stress.py`），这与本批改动无关（本批只动 `generated.db` 一列）；stress 模块另有一个既有问题 `no such column: kg_relations.status`（它自己的临时库版本漂移）。为确认没有污染，已比对 `novel_dev.db` 30 张表行数与 `00:29:28` 备份：**全部相同**。

### D.6 回滚实测（库副本）

副本 `<temp>\rb_copy.db`（200704 bytes）→ `ALTER TABLE kg_item_group DROP COLUMN novel_id`：

| 项 | 结果 |
|---|---|
| 列 | 9 → 8，与改前列名/顺序/定义完全一致 |
| 表 sql | **逐字一致**（SQLite 3.49.1 会精确还原被删列文本） |
| 索引 | `sqlite_autoindex_kg_item_group_1` 不变 |
| 53 行旧列 | 与改前逐字一致 |
| 13 张表逐行 | 与改前备份**全行一致**（不只计数） |
| 探针 / md5 / 覆盖 / 持有行 / 列清单 | 与改前全部一致（含 `coverage_gid45=125`、`d056037b…`/`a2e9207c…`/`6c15c704…`） |

结论：`DROP COLUMN` 能干净退回；正式库未执行回滚（回滚脚本 `--rollback` 会先自动备份再删列，同样可用）。

### D.7 留给下次的接口：读侧什么时候该切换

**触发条件（满足任一）**：

1. 项目 5 第一次出现 `novel_id=6` 的物品组（即真的开始为《豪门宠婚》建组）；
2. 同一个写法/规范名在两本书里各有一个组，`by_surface`/`name_to_group` 发生互相覆盖；
3. 出现「`resolve_group_id` 取第一条、把持有行挂到另一本书的组」的脏数据证据。

**切换面（按依赖顺序）**：

1. 先补约束（D.8 的索引/唯一键）——这是前置，否则第二本书同名组插不进去；
2. `item_holding_read._load_groups()`：SELECT 带出 `g.novel_id`，`by_surface` / `name_to_group` / `group_by_canonical` 三张字典从「名字 → 唯一组」改成按 `(novel_id, name)` 分槽；`NULL` 组作为通配同时参与；
3. `conflicts`（同名跨组）改成**书内作用域**，否则两本书的同名组会全被报成 `ambiguous`；
4. `generated_store.resolve_group_id()` 加 novel 过滤（写侧最危险的一处，目前完全没有项目/书过滤）；
5. 加 `novel_scope` 开关灰度，`NULL` 始终匹配任意书，默认关闭时行为与今天完全一致。

**不建议现在切**：当前没有任何一条组落错书，切换没有纠错收益，只会把三处名字键 + `conflicts` + `resolve_group_id` 的回归面一次性打开。

### D.8 切换时还要动哪几处唯一索引

| 约束/索引 | 现状 | 切换时要变成 | 代价 |
|---|---|---|---|
| `kg_item_group` 表级 `UNIQUE(project_id, kind, canonical_name)`（`sqlite_autoindex_kg_item_group_1`） | 同一项目同名只能一组 | `UNIQUE(project_id, novel_id, kind, canonical_name)`；若要让 NULL 泛称也唯一，需写成表达式唯一索引 `UNIQUE(project_id, COALESCE(novel_id,-1), kind, canonical_name)`（NULL 在普通 UNIQUE 里互不相等） | SQLite 改不了表级 UNIQUE → 12-step 重建表（新建 + copy 53 行 + drop + rename + 重建索引），实测成本 <1s |
| `kg_item_surface` 表级 `UNIQUE(project_id, surface)` | 一个写法全项目只能挂一个组 | `UNIQUE(project_id, novel_id, surface)` 或 `(project_id, COALESCE(novel_id,-1), surface)` | 同样要重建表（134 行） |
| `kg_item_holding` 的部分唯一索引 `ux_item_holding_open (COALESCE(item_id,-1), item_name, hold_type, COALESCE(instance_id,-1)) WHERE valid_to IS NULL` | 键里没有项目/书 | 两本书都有「镇国剑」的开放持有会撞键 → 要么加 `novel_id` 列并进键，要么明确接受「跨书同物合并」 | 加列 + 重建部分索引；影响 60 行 |
| `kg_holding_story_segment` 的 `ux_story_segment_branch` / `ux_story_segment_holding` | 同样无书维度 | 段要分书时一并加 `novel_id` | 4 行，成本低 |
| `kg_item_instance` 表级 `UNIQUE(item_name, instance_label)` | 同名不同书实例会撞 | 加 `novel_id` 后重建 | 1 行 |

**只需要动前两项**就能让「组按书」成立；后三项是「持有行/实例也按书」才需要的。如果将来确认项目 5 永远只在一个项目里写一本书，这一节可以整节不做。

## E. 读侧切换：按书解析 + 扩唯一键（执行记录）

### E.0 前置问题：「当前书」到底从哪里取（结论先行）

**结论：现有代码里没有可靠的「当前书」来源。** 逐调用点核对如下（`grep` 全 `backend`，2026-11-11）：

| 调用点 | 位置 | 手上的上下文 | 有没有书 |
|---|---|---|---|
| `current_item_holdings` | [item_holding_read.py](backend/app/kg/item_holding_read.py#L585-L591) | `project_id: int`、`generated_db` | **没有**（只有项目） |
| `_load_registry` | [item_holding_read.py](backend/app/kg/item_holding_read.py#L287) | `project_ids` | **没有** |
| `_fetch_holding_rows` | [item_holding_read.py](backend/app/kg/item_holding_read.py#L495) | `group_ids` + 名字/id | **没有**（按组取，组上有书，够用） |
| `engine._item_holding_text`（读侧唯一产线调用方） | [engine.py](backend/app/generation/engine.py#L737-L759) | `project` 对象 | **有对象但取不出单本**：`project.reference_novel_ids` 实测是 `[5, 6]`（两本参考语料） |
| `record_holding` / `record_story_segment`（写侧） | [generated_store.py](backend/app/kg/generated_store.py#L587) / [#L867](backend/app/kg/generated_store.py#L867) | `con`、`item_name`、`chapter` | **没有**（无 project、无 novel） |
| `resolve_group_id` | [generated_store.py](backend/app/kg/generated_store.py#L510) | `con`、`item_name` | **没有**，且 `ORDER BY pri, gid LIMIT 1` 抽第一条 |
| 批量抽取 / `canon_source.fold_item_aliases` | [canon_source.py](backend/app/kg/canon_source.py#L826) | 全局名字折叠 | **没有**（不写组，只折名） |

`batch_worker.py` 不写 `kg_item_group`/`kg_item_surface`（全 `backend/app` 内没有任何写入点，只有迁移脚本写），所以「批量抽取时手上有书」这个假设不成立。

**`project.reference_novel_ids` 不是「当前书」**：项目 5 实测是 `[5, 6]`，而 45 个组全部回填为 novel 5——说明建组时看的是其中一本，参考语料集合本身无法回答「这次读的是哪本」。用它直接当过滤条件会**同时读两本**，属于假过滤。

**因此按最保守形态接**：显式参数 `novel_id: int | None = None`，`None` = 不按书过滤，现有行为逐字不变。调用方暂时都不传（见 §E.7 缺口）。

### E.1 备份与迁移脚本

| 什么 | 位置 |
|---|---|
| 动手前整库备份（generated） | `backend/backups/generated-full-generated-novel-scope-20261111-004723.db` |
| 动手前整库备份（novel_dev） | `backend/backups/novel_dev-full-novel_dev-novel-scope-20261111-004723.db` |
| 迁移自带备份 | `backend/backups/generated-full-novel-scope-20261111-004913.db` |
| 迁移快照 | `backend/migrations/2026-11-14-item-group-surface-novel-scope-v1/snapshots/novel_scope_20261111-004913.json` |
| 迁移脚本 | `backend/migrations/2026-11-14-item-group-surface-novel-scope-v1/apply_novel_scope.py` |

脚本默认 dry-run，`--apply` / `--rollback`，支持 `--db` / `--backup-dir` 指到副本上实测。SQLite 3.49.1 改不了表级 UNIQUE，两张表都走 12-step 重建（新建 → 拷贝 → 丢旧 → 改名 → 重建索引），重建期 `foreign_keys=OFF`、`legacy_alter_table=ON`，`sqlite_sequence` 显式搬回（`kg_item_group=102`、`kg_item_surface=281`，与改前一致）。

### E.2 改动了哪两张表、唯一键的新形态

**行数一行没变**：组 53 行（8 个 `novel_id IS NULL`）、写法 134 行（12 行属于通配组，回填为 `NULL`）。

| 表 | 改前 | 改后 |
|---|---|---|
| `kg_item_group` | 表级 `UNIQUE(project_id, kind, canonical_name)`（`sqlite_autoindex_kg_item_group_1`） | 去掉表级唯一；增唯一索引 `ux_item_group_project_novel_name (project_id, kind, COALESCE(novel_id,-1), canonical_name)`；列与列序不变 |
| `kg_item_surface` | `UNIQUE(project_id, surface)`；无书列 | 新增可空 `novel_id INTEGER`（回填所属组的书）；去掉表级唯一；增 `ux_item_surface_project_novel_surface (project_id, COALESCE(novel_id,-1), surface)`；`ix_item_surface_group` 原样重建 |

两个新唯一键的形态就是「项目 + 书 + 名字」：

```
kg_item_group    (project_id, kind, COALESCE(novel_id, -1), canonical_name)
kg_item_surface  (project_id, COALESCE(novel_id, -1), surface)
```

三个设计取舍：

- **`COALESCE(novel_id,-1)` 不是装饰**：SQLite 里 `NULL` 在普通 UNIQUE 中互不相等，直接放 `novel_id` 会让同项目两条 NULL 泛称同名也能插进来。`-1` 不是合法 `raw_novels.id`（自增从 1 起）。
- **不加 FK**：`novel_id` 指向 `novel_dev.raw_novels.id`，但组表在 `generated.db`，两库物理隔离、不 ATTACH；`NULL` 的语义是「项目级通配」而非空外键。
- **持有行/实例/story segment 一律没动**（守边界）。代价见 §E.7 第 3 条。

### E.3 「当前书」的传递路径

```
调用方（暂缺） ──novel_id──▶ current_item_holdings(..., novel_id=None)
                                └─▶ _load_registry(path, project_id, novel_id)
                                      ├─ SQL: g.novel_id IS NULL OR g.novel_id = ?
                                      └─ 候选裁决：真书组 > NULL 通配组；横跨两本 → 歧义

调用方（暂缺） ──novel_id──▶ record_holding(..., novel_id=None)
                                └─▶ resolve_group_id(con, name, novel_id=None)
                                      ├─ 候选 = 这本书的组 + NULL 通配组
                                      └─ 没传书而候选跨两本 → 返回 None（不猜）
```

- `novel_id=None` 是**默认且唯一**的当前形态：不按书过滤，与加参数前逐字一致。
- 旧的 `resolve_group_id` 是 `ORDER BY pri, gid LIMIT 1`，同名跨书时会抽到另一本；现在没传书遇到跨书同名**返回 `None`**（宁可让读侧按名兜底/报歧义，也不写错组）。
- 读侧 `_load_registry` 的名字裁决：先按组去重（同一组的「写法 + 规范名」不是歧义），真书组恰好一个就取它，没有真书组时通配组单独命中，否则报冲突。`conflicts` 只在候选横跨多本时才产生，**NULL 通配组不算冲突方**。
- 按书查询时 `_fetch_holding_rows` **不做名字兜底**：持有行没有书维度，未挂组的老行无法证明归属，宁可不给也不跨书返回。

### E.4 NULL 语义（测试锁死）

`novel_id IS NULL` = 项目级通配，三条口径都落成了断言（`tests/test_item_group_cross_novel.py`）：

1. 任一本书下都**加载得到**通配组（`status=generic`、`group_id=300`），不被书过滤掉；
2. 同名时**真书组优先**，通配组不当冲突方；
3. 通配组的成员组仍按书过滤：book6 下泛称组在，但不会把 book5 成员组的持有行带出来。

### E.5 跨书回归场景（临时库，真实库一行没动）

`backend/tests/test_item_group_cross_novel.py` 在 `tmp_path` 里造项目 5 的两本书：

- gid 100 / novel 5 / `镇国剑`，持有行「镇北王 own 99–」（item_id 501）
- gid 200 / novel 6 / `镇国剑`，持有行「元景帝 own 106–」（item_id 601）
- gid 300 / novel NULL / `玉石小镜`（generic，成员 [100]）

七条断言：

| 测试 | 锁什么 |
|---|---|
| `test_resolve_group_id_hits_its_own_book` | `novel_id=5→100`、`=6→200`、不传书 `→None`（旧实现会抽 100） |
| `test_unique_key_allows_cross_book_but_blocks_same_book` | 新唯一键：跨书同名可共存，同书同名被 `IntegrityError` 拒 |
| `test_read_book5_does_not_return_book6_rows` | 按书 5 读只拿到 100 / 镇北王 |
| `test_read_book6_does_not_return_book5_rows` | 按书 6 读只拿到 200 / 元景帝 |
| `test_unscoped_read_reports_ambiguity_instead_of_picking` | 不传书 → `ambiguous`、`group_id=None`、`lines=[]` |
| `test_null_generic_group_visible_under_every_book` | NULL 泛称在两本书下都可见、不算跨书冲突、成员行仍按书过滤 |
| `test_single_book_result_identical_with_or_without_novel` | 单书场景「传书 == 不传书」逐字相等 |

### E.6 回滚实测（库副本，不是嘴上说）

副本 = 迁移后的 `generated.db`，跑 `--rollback` 后与迁移自带备份逐项对照：

| 项 | 结果 |
|---|---|
| 表结构（空白归一后逐字） | 一致：表级 `UNIQUE(project_id, kind, canonical_name)` / `UNIQUE(project_id, surface)` 回来，`ux_*` 两个索引消失，`ix_item_surface_group` 在 |
| 行数 / md5 | 组 53 / 134 行、组 md5、写法 md5 全一致 |
| 53 行 novel_id 逐行 | 一致（含 8 行 NULL） |
| 探针（19 个物品的整块 facts，按书=5） | 回滚副本与生产库 **md5 相同**：覆盖 125、六探针 group_id/via/lines/lines_md5 全一致 |
| 序号 | `kg_item_group=102`、`kg_item_surface=281` 不变 |
| 守卫 | 副本里插一条 book6 同名组后再回滚 → **明确拒绝**（`已有跨书同名数据，旧唯一键装不下`），库保持原样（54 行、唯一索引仍在） |

也就是说：**有了跨书同名数据之后不能回滚**，这是脚本主动拒绝的，不是静默半途而废。要回滚必须先删掉第二本书的同名组。

### E.7 验收数字、测试与仍然留着的缺口

改后逐项复核（`<temp>\d3_verify.py`，cwd=backend）：

- `coverage_gid45 = 125`；
- 六个探针：`暗金色长刀`/`太平刀`/`黑金长刀`/`刀胚`/`黑金刀` → `held / gid45 / via=surface / lines=2 / md5 617c601c…`，`镇国剑` → `held / gid100 / via=surface / lines=2 / md5 24e7c486…`，与改前逐字一致；
- p1 两段 `d056037b0a0a56cee379646ca54e8e18` / `a2e9207ca2e028a05778b0dd8e884561`、p5 `6c15c704a8b78a04adfed097af76bf50` 不变；
- 4 组（gid 20/45/33/5）11 条持有行 `after_resolvable` 全 true，`holding_bad=[]`；
- pytest：`350 passed`。脚本式文件按「模块顶层 `sys.exit(`」判定跳过 15 个 + `tests/stress`（这类文件收集期就 INTERNALERROR），其余把 `sys.exit` 放在 `__main__` 分支的脚本式文件照常收集；**37 个文件一个没改**。跑完核对两库行数：`novel_dev.db` 31 表、`generated.db` 13 表，与动手前备份**逐表一致**（无测试污染）。

仍然留着的缺口（下次接口）：

1. **调用方还没传书**。`engine._item_holding_text(db, project, current_seg)` 只有 `project`，`record_holding` 的生成流水线调用方也没有书上下文；两边都走默认 `None`，所以现在仍是「不按书过滤」。这是真缺口，不是假过滤：默认 `None` 下跨书同名会如实报 `ambiguous`，不会悄悄串。
2. **「当前书」仍是未定义业务概念**。`project.reference_novel_ids = [5, 6]` 是参考语料集合，不等于本次写/读的那一本。需要生成会话或批量任务里带 `novel_id`（例如 Generation 行或 batch job 上加一列），才能把参数真正接上。
3. **持有行/实例/story segment 没有书维度**。`kg_item_holding`、`kg_holding_story_segment`、`kg_item_instance` 未改：跨书同名物品若沿用同一 `item_id` 且 `hold_type` 相同，开放唯一键 `ux_item_holding_open` 会拒写第二条；按书查询时未挂组（`group_id IS NULL`）的历史行不参与。要彻底按书，下一批得给这三张表加 `novel_id` 并重建 `ux_item_holding_open` / `ux_story_segment_branch`。
4. **`surface.novel_id` 与 `group.novel_id` 没有库级一致性约束**（SQLite 不能跨表 CHECK）。当前没有应用代码写这两张表，迁移里按组回填；将来加写入路径时要自己保证一致。
5. **回滚有前提**：出现跨书同名后必须先清理数据才能回滚（脚本会拒绝，不会破坏库）。

## F. 「当前书」存哪儿：调研、候选与落地（本轮）

### F.1 调研结论（只读，先回答五问）

**① `projects.reference_novel_ids` 是「参考语料集合」，不是「当前书」。**

- 类型 `JSON`（[models.py](backend/app/models.py#L54)），注释就写着「选中的参考语料 novel_id 列表（空=自由生成）」。实测：项目 1–4 为 `NULL`，项目 5 为 `[5, 6]`（两本同时选）。
- 读它的地方（全 `backend` 扫过，共 19 处）：蒸馏/文风档案（`style/jobs.py:391/452/722`、`style/narrative.py:4131`、`style/pipeline.py:173`）、生成入口的蒸馏前置检查（`generation/engine.py:447`）、自检的语料检索（`engine.py:1054`，**遍历全部书**，`_SELFCHECK_CORPUS_CALLS` 限次）、引导会话（`routers/assistant_wizard.py:203/872/890/901`、`assistant_clean.py:137`）、项目序列化（`routers/projects.py:66/169`、`routers/generation.py:35/72/280`）、公式化生成（`generation/formulaic.py:839-846`）、物品读通道的「同语料项目」枚举（`item_holding_read.py:124/158`）。
- **只有一处把它当「一本书」用**：[agents/registry.py](backend/app/agents/registry.py#L268-L285) 的 `_impl_search_novel_chunks` —— `novel_id` 不传时取 `refs[0]`。这是「取第一本兜底」，不是语义上的当前书。
- 结论：它回答「用了哪些书」，回答不了「此刻按哪一本」。拿它当过滤条件会同时读两本，属于假过滤。

**② 写某一章时，代码手上没有书标识，也没有书级的章节号。**

- `generations` 实际列：`id, project_id, segment_no, chapter_range, text, summary` —— **没有 `novel_id`，也没有章号列**。
- 段号来源 [engine.py `_next_segment_no`](backend/app/generation/engine.py#L88-L92)：`本项目 generations 行数 + 1`，纯项目内线性计数；`chapter_range` 落库的是 `f"段{segment_no}"`（[engine.py:487](backend/app/generation/engine.py#L483-L487)）。
- `engine` 生成入口与 prompt 组装全程只有 `Project` 对象；`_item_holding_text(db, project, current_seg)` 也是 `project` + 段号（[engine.py:737-753](backend/app/generation/engine.py#L737-L753)）。
- 结论：段与书之间**没有任何绑定字段**，章节号也不来自任何参考书。

**③ `raw_novels` / `novel_chunks` 只在 chunk 级关联，没有段落表。**

- `novel_chunks`: `id, novel_id, chapter_no, idx, text, embedding`，`novel_id` 是 `ForeignKey("raw_novels.id")`、`nullable=False`。
- 全库没有「段落表」；`nodes/paragraphs` 之类不存在。粒度就是 chunk（正文按 `chapter_no` + `idx` 切片）。实测每本：书 2→26 段、3→919、4→121、5→5094、6→1781。
- 另有一条与书相关但常被忽略的：`distill_chunks` 有 `project_id + novel_id + scope + chapter_start/end`，它是蒸馏产物缓存，带书维度。

**④ 项目 5 的历史生成**：不是从两本中任何一本续写的。

| 证据 | 值 | 说明 |
|---|---|---|
| `generations`（项目 5） | 段1「小杨站在废弃的工厂前…失踪者A」、段2「小杨站在废弃工厂的角落…」 | 现代悬疑原创，人物/场景与两本书均无重合 |
| `projects.premise` / `genre` / `style` | 「刚毕业的记者…失踪人口」/ 悬疑 / 知乎盐选 | 原创设定 |
| `generations.chapter_range` | `段1` / `段2` | 没有章号，谈不上「从第 N 章续」 |
| `distill_chunks` | 书 5：5 块（章 130–891，含 `aux_voice_5`/`aux_drift_5`）；书 6：5 块（章 163–594，含 `aux_voice_6`/`aux_drift_6`） | **两本都蒸过**，蒸馏本身判不出「当前书」 |
| `style_distills` | 2 行（fast/balanced），`source_ids = [5, 6]` | 文风档案也是两本合并 |
| `kg_item_group` | 书 5 = 45 组，书 6 = 0 组，NULL = 8 组 | **书级物品事实只来自书 5** |
| `kg_item_surface` | 书 5 = 122 行，书 6 = 0 行，NULL = 12 行 | 同上 |

一句话：项目 5 在**写一个原创悬疑故事**，同时把两本书当**参考语料**（文风/检索），而**书级物品事实只有书 5**。

**⑤ 前端没有「当前书」的持久化入口，只有两处「书」相关 UI。**

- 第 2 步「参考语料」是**多选**：[ProjectWorkspace.vue `toggleRef`/`saveRefs`](frontend/src/views/ProjectWorkspace.vue#L592-L605) → `PATCH { reference_novel_ids: [...] }`，多本可同时勾。
- 第 7 步「图谱来源」是**单次查询参数**：[buildSourceOptions](frontend/src/views/ProjectWorkspace.vue#L1236-L1248) 选项为「我的小说（生成后）」或「参考语料·<书名>」，走 `POST /projects/{id}/build-kg?novel_id=`（后端 [projects.py:565-574](backend/app/routers/projects.py#L565-L574)）。**不落库**，刷新即回到默认。
- 结论：「选一本书」目前只存在于**一次请求**里，没有任何地方存过「这个项目现在写哪本」。

**⑥ 关于「一个项目同时写两本书」——没有这种场景，但有一件事必须写清楚。**

- `generations` 是**项目内单链**（`segment_no = 行数+1`），一个项目只有一条写作线；不存在两条并行的书级正文。
- 项目 5 同时**引用**两本书（蒸馏 `source_ids=[5,6]`、检索遍历两本）是**参考语料**语义，已由 `reference_novel_ids` 多值承载，不需要「当前书」再表达一次。
- 所以单值字段没有选错，**不触发「停下先报」**。但有一条边界要记死：`active_novel_id` 的语义是「书级事实（物品组/写法/持有）按哪一本收窄」，**不是**「在续写哪一本」。项目 5 的 `active_novel_id=5` 不表示它在续写《大奉打更人》。
- 真出现「一个项目要同时按两本书维护两条书级事实线」时，单值就装不下了，得换 F.2 的方案 B（关联表），这一条写进 F.6 缺口。

### F.2 候选方案与代价

| | A. `projects.active_novel_id`（可空 INTEGER） | B. 项目-书关联表 + `is_active` | C. 派生/运行时参数，不入库 |
|---|---|---|---|
| 改哪些表 | `projects` 加 1 列 | 新表 `project_novels(project_id, novel_id, is_active)` + 部分唯一索引 `UNIQUE(project_id) WHERE is_active=1` | 不改表 |
| 回填可行性 | 数据驱动：`refs` 非空且组只落一本 → 该书；否则 NULL。项目 5 → 5，1–4 → NULL | 回填 1 行 `(5, 5, 1)` | 不适用 |
| 读侧怎么取 | ORM `project.active_novel_id`（已有 `project` 对象的地方都能直接读）；SQL 同表取 | 每次多一次 JOIN/子查询；或启动时载入 | 调用方逐次传，**但谁来定这个值没有答案**（现状 19 个调用点只有 `project`） |
| 将来「一个项目多本待写」 | 要重构（加表并迁移语义） | 天生支持（多行 + 一激活） | 天生支持 |
| 代价 | 只能表示「当前一本」；与 `reference_novel_ids` 容易混（已用注释/命名区分：复数=语料集合，单数=当前书） | 多一张表、要保证「激活位唯一」；当前没有第二本书待写的需求，属于为未发生的需求付结构成本 | 把问题推给上层；且无持久化 → 同一项目两次生成可能按不同的书，不可复现 |
| 可逆性 | 高（改回 NULL 即等于「不指定」；删列一条 DDL） | 中（删表要撤索引与数据） | 最高（不入库） |

**推荐 A**，理由：现实里「当前书」确实是单值（`generations` 单链），且 A 满足用户给的三条边界——可空、可逆、`NULL` 时行为与现在完全一致。B 的唯一优势（多本待写）目前没有现实需求；C 无法回答「值从哪来」，等于没解决。

### F.3 落地了什么

| 项 | 内容 |
|---|---|
| 列 | `projects.active_novel_id INTEGER`，可空，无默认值，无索引，**排在列序末尾** |
| 模型 | [models.py](backend/app/models.py#L55-L60) `active_novel_id = Column(Integer, nullable=True)` |
| 补列迁移（防老库炸） | [db.py](backend/app/db.py#L142-L147) `("projects", "active_novel_id", "INTEGER")` —— 不加这一项，已存在的库 `SELECT projects.*` 会 `no such column` |
| 加列 + 回填脚本 | [apply_active_novel.py](backend/migrations/2026-11-15-project-active-novel-v1/apply_active_novel.py)（dry-run 默认 / `--apply` / `--rollback`，支持 `--db`/`--gen-db`/`--backup-dir` 指到副本） |
| 快照 | `backend/migrations/2026-11-15-project-active-novel-v1/snapshots/active_novel_20261111-010208.json` |
| 动手前整库备份 | `backend/backups/novel_dev-full-active-novel-20261111-010055.db`、`backend/backups/generated-full-active-novel-20261111-010055.db` |
| 脚本自带备份 | `backend/backups/novel_dev-full-active-novel-20261111-010207.db` |
| 生成链路 | **一行没接**：`item_holding_read.py`、`generated_store.py`、`engine.py` 全部未改；读侧继续走默认 `novel_id=None` |

回填规则（数据驱动，不写死项目号，写进脚本 `decide()`）：

1. `reference_novel_ids` 为空/非列表 → `NULL`；
2. 有语料 → 看 `generated.db.kg_item_group` 按 `novel_id` 的组数，**恰好一本书 >0** → 取该书；多本都有组 → `NULL`（单值装不下，不猜）；
3. 候选 id 必须存在于 `raw_novels`。

### F.4 回填表（逐项给依据）

| 项目 | 标题 | `reference_novel_ids` | `active_novel_id` | 依据 |
|---:|---|---|---:|---|
| 1 | Assistant 测试项目 | `NULL` | **NULL** | 没有参考语料，没有任何书级事实 |
| 2 | 调用链验收项目 | `NULL` | **NULL** | 同上（`status=created`，0 段正文） |
| 3 | 调用链验收项目 | `NULL` | **NULL** | 同上 |
| 4 | 调用链验收项目 | `NULL` | **NULL** | 同上 |
| 5 | `4` | `[5, 6]` | **5** | 组 45/45 归书 5、写法 122/122 归书 5、书 6 零组零写法；「参考语料里只有书 5 有书级物品事实」是唯一可判定信号 |

**项目 5 判 5 的反向信号（必须一起读）**：`generations` 是原创悬疑，两本书都蒸过（各 5 块）。所以 5 的含义是「书级物品事实来自大奉」，**不是「在续写大奉」**。若将来产品把「当前书」定义为「续写哪本」，项目 5 应改成 `NULL`——一条 `UPDATE` 即可，脚本 `--rollback` 也能整体复位。

### F.5 验收

**结构 diff（改前快照 vs 改后，两库逐对象比对）**

| 库 | 新增对象 | 删除对象 | DDL 变化 | 列变化 | 表数 |
|---|---|---|---|---|---:|
| `novel_dev.db` | 0 | 0 | 仅 `projects` | 仅 `projects`：末尾多 `active_novel_id INTEGER`（23 → 24 列） | 31 → 31 |
| `generated.db` | 0 | 0 | **无** | **无** | 13 → 13 |

**现有行为不变**（同一支 `d3_verify.py`，改前/改后各跑一次，逐字段比对）

| 项 | 改前 | 改后 |
|---|---|---|
| `coverage_gid45` | 125 | 125 |
| 六探针（`暗金色长刀`/`太平刀`/`黑金长刀`/`刀胚`/`黑金刀`/`镇国剑`） | status/group_id/via/lines/lines_md5 | **完全相同** |
| p1 段1 / 段2、p5 整块 facts 的 md5 | 3 个 md5 | **完全相同** |
| 4 组（gid 20/45/33/5）持有行 | 11 条，`holding_bad=[]` | 11 条，`holding_bad=[]`，逐条相同 |

**两库逐表行数**：与改前备份比对 `count_diff = {}`（novel_dev 31 表、generated 13 表，一张不差；测试跑完再比一次仍为空，无测试污染）。

**测试**：`350 passed`。跳过方式与上一轮一致——`--ignore=tests/stress` + 15 个「模块顶层 `sys.exit(`」的脚本式文件（`test_batch_worker_observability`、`test_entity_resolve`、`test_kg_cascade_protocol`、`test_kg_name_resolution_kind`、`test_kg_v3_frontend_api`、`test_knowledge_consumption`、`test_name_expand`、`test_name_normalize_probe`、`test_sample_chars_floor`、`test_timeline`、`test_v30_extractor`、`test_v30_integration`、`test_v30_judge`、`test_v30_retrieval`、`test_v30_schema`）。**没有改任何测试文件。**

**ORM 与幂等**：`Project` 查询 5 行正常，`active_novel_id` 读出 `None/None/None/None/5`；`ensure_schema()` 二次调用不报错（列已存在则跳过）。

**回滚实测（库副本）**：`Copy-Item novel_dev.db → <temp>\copy_active.db` → `--apply`（23→24 列、5 行不变）→ `--rollback` → 与生产原库逐项比对：**DDL 归一后逐字一致、索引一致、23 列一致、5 行数据一致**。即回滚能把库还原到加列前，不是「只把值清空」。

### F.6 仍然留着的缺口

1. **没接生成链路**（按边界刻意不做）。读侧 `current_item_holdings(..., novel_id=None)` 的默认值仍是唯一形态；接线时要改的是 `engine._item_holding_text` 与 `record_holding` 的调用方，一处一行。**传哪一列见 §G.6**（续写书优先，为空再退回本列）。
2. **列上没有 FK**。`active_novel_id → raw_novels.id` 是同库逻辑引用，但 SQLite 的 `ALTER TABLE ADD COLUMN` 加不了外键；脚本回填时校验了取值存在，**删书不会级联清空**。要硬约束只能重建 `projects` 表（23 列、5 行，成本可控但属于另一件事）。
3. **没有写入入口**。API 的 `ProjectOut`/`ProjectPatch` 都没暴露这一列（`schemas.py` 是显式字段，不会自动漏），前端也没有切换 UI。目前只有迁移脚本能写。
4. **语义只覆盖「书级事实的作用域」**，不覆盖「续写哪本」——**该缺口已在 §G 补上**（新增 `continuation_novel_id`，两者并存、可同时为空）。
5. **单值上限**：一个项目同时维护两条书级事实线时（F.1⑥ 描述的现实反例），单值装不下，要换关联表；那时 `projects.active_novel_id` 可作为「默认激活」保留或迁走。

## G. 「续写书」：与「事实来源书」并存的第二个概念

### G.1 两个概念各回答什么（人话）

- **`active_novel_id`（事实来源书）**：这个项目的书级事实——物品组、写法、持有段——是从哪本书里抽出来的。可空；`NULL` = 没指定，读侧不按书过滤。
- **`continuation_novel_id`（续写书）**：这个项目的正文是从哪本书往后写的。可空；`NULL` = 原创（不从任何书续写）。

一句话区别：**前者说「事实出自哪本」，后者说「正文接着哪本写」**。原创项目只可能有前者（借某本书的图谱当素材、正文自己写），不可能有后者；纯粹续写且事实也建在续写书上的项目，两列会是同一个 id。

### G.2 两列不一致（事实来自 A、却在续写 B）——**允许，不报错，只告警**

**判断：允许写入，生成时打一条 warn 日志，不中断。**

理由：

1. 两列描述的是**不同维度**，不是同一个值的两种写法，因此「不相等」本身不是矛盾。现实里有合法组合——比如续写 B，但物品事实刻意沿用 A（同一世界观、A 是设定集）；也可能项目先建了 A 的图谱、后来决定续写 B。
2. 这是**元数据问题**，不是运行时错误。升级成异常会直接阻断整条生成链，用户拿不到正文——代价远大于收益。
3. 但它是**高概率的录错信号**（大多数时候意味着某一列填错了），所以要留痕：生成入口发现两列非空且不等时，记一条 warning，带 `project_id / active / continuation` 三个值，便于事后排查。
4. 真正会「出错」的不是这个组合本身，而是拿 A 的持有事实去约束 B 的正文——这一点由 G.6 的取值优先级解决，而不是靠禁止写入。

**不做的**：不在库里加 `CHECK (active_novel_id = continuation_novel_id)` 之类的约束，也不在 `PATCH` 层互相校验。约束会把合法组合一并挡掉。

### G.3 命名与形态：选「独立可空列」，不建关联表

| | 独立可空列 `continuation_novel_id`（**已选**） | `project_novel` 关联表带 `role` 枚举 |
|---|---|---|
| 改动面 | `projects` 加 1 列 | 新表 + 把 `reference_novel_ids`、`active_novel_id` 一起迁进去 + 双写过渡 |
| 可逆 | 删一列即回到现在 | 迁完再想退，要把数据搬回 JSON 列，风险高 |
| 「同一本书既是事实来源又是续写书」 | 两列填同一个 id 即可 | 同一 `(project, novel)` 两行不同 `role`，或一行多 role |
| 需要存「额外属性」（如从第几章续、续写权重） | 存不了，要再加列 | 有地方放 |
| 语义直白度 | 高（列名即含义） | 中（要读懂 role 枚举） |

**为什么没触发「先报再动」**：用户给的例外条件是「同一本书可能既是事实来源又是续写书、**还得存额外属性**」。前一半独立两列天然支持（填同一个 id）；后一半目前**没有任何现实需求**——续写的起点章号现在根本不存在于库里（`generations.chapter_range` 是「段N」），没有可存的属性。所以不建表。

顺带确认一个容易混淆的点：`reference_novel_ids` 是**多值 JSON 集合**（用了哪些书），`active_novel_id` / `continuation_novel_id` 是**单值**（那一本）。三者可以同时存在、互不推导——不要用 `refs[0]` 去填这两列，那是 F.1① 说的假绑定。

### G.4 落地

| 项 | 内容 |
|---|---|
| 列 | `projects.continuation_novel_id INTEGER`，可空、无默认、无索引、无 FK，**物理列序末尾**（紧跟 `active_novel_id`） |
| 模型 | [models.py](backend/app/models.py#L61-L65) `continuation_novel_id = Column(Integer, nullable=True)` |
| 补列迁移（防老库炸） | [db.py](backend/app/db.py#L148-L150) `("projects", "continuation_novel_id", "INTEGER")` |
| 加列 + 回填脚本 | [apply_continuation_novel.py](backend/migrations/2026-11-16-project-continuation-novel-v1/apply_continuation_novel.py)（dry-run 默认 / `--apply` / `--rollback` / `--db` / `--backup-dir`） |
| 快照 | `backend/migrations/2026-11-16-project-continuation-novel-v1/snapshots/continuation_novel_20261111-010923.json` |
| 动手前整库备份 | `backend/backups/novel_dev-full-continuation-novel-20261111-010738.db`、`backend/backups/generated-full-continuation-novel-20261111-010738.db` |
| 脚本自带备份 | `backend/backups/novel_dev-full-continuation-novel-20261111-010923.db` |
| **没接的地方** | 生成链路、读侧（`item_holding_read.py`）、前端、API（`ProjectOut`/`ProjectPatch`）**一处没动**；这一步只定义存储位置 |

**对 `active_novel_id` 的影响：零。** 列名、已回填的值（项目 5 = 5）、列位置都没动；本轮结构 diff 里 `active_novel_id` 不属于变化项。

### G.5 回填表

回填规则写死在脚本 `decide()` 里，依据全部来自库内可查的事实：

1. `reference_novel_ids` 为空 → `NULL`（没有书可选）；
2. 有语料且 0 段正文 → `NULL`（没有可判定的续写）；
3. 有语料且有正文 → 看 `chapter_range` 是否含章号；当前全是「段N」，没有章号，且库内没有「书 ↔ 章节」的绑定字段 → `NULL`。

| 项目 | `reference_novel_ids` | `active_novel_id`（不动） | `continuation_novel_id` | 依据 |
|---:|---|---:|---:|---|
| 1 | `NULL` | NULL | **NULL** | 无参考语料 |
| 2 | `NULL` | NULL | **NULL** | 无参考语料 |
| 3 | `NULL` | NULL | **NULL** | 无参考语料 |
| 4 | `NULL` | NULL | **NULL** | 无参考语料 |
| 5 | `[5, 6]` | 5 | **NULL** | 原创悬疑（记者小杨/失踪者A）；`chapter_range = ['段1','段2']` 无章号；蒸馏覆盖 `[5,6]` 两本 → 与任何一本都没有章节续接关系 |

**没有哪个项目能判出在续写某本。** 佐证：`generations` 只有 `project_id / segment_no / chapter_range`，段号是 `本项目行数+1`，落库的 `chapter_range` 是 `f"段{n}"`——库里根本不存在「当前写到原著第几章」这个事实。所以全 `NULL` 不是偷懒，是数据里确实没有这个信息。

### G.6 将来读侧该传哪一列（本轮最该定的一件事）

**结论：物品组按书过滤时，优先传 `continuation_novel_id`，为空再传 `active_novel_id`，两者都空则传 `None`（不按书过滤）。**

| 顺序 | 传入值 | 含义 |
|---:|---|---|
| 1 | `project.continuation_novel_id` | 正在从这本书往后写 → 用这本书的物品事实约束正文 |
| 2 | `project.active_novel_id` | 原创但借了某本书的图谱 → 用这本书的事实 |
| 3 | `None` | 两者都没有 → 不按书过滤（**现有行为，逐字不变**） |

**为什么不是「事实来源优先」**：`current_item_holdings` 的输出不是「数据血缘报告」，而是**注入正文的硬约束**（「镇北王 own 99–106」「镇北王已死」这类）。约束必须对**正在写的那本**成立。如果事实来自 A、正文在写 B，传 A 就会把 A 的持有关系塞进 B 的正文——这是**直接写错内容**，比「少给几条事实」严重得多。所以正文归属（续写书）优先。

`active_novel_id` 仍然有用，两个位置：① 没有续写书时的回退（项目 5 正是这种：原创 + 借大奉图谱，`continuation=NULL` → 回退 `active=5`，与现在完全一致）；② 数据血缘/展示（这堆事实是哪本书抽的）。

**这条规则的反例（必须承认）**：

1. **续写 B，但 B 没有任何物品组，而 A 有**。按规则传 B → 读通道拿到空注册表 → 物品注入为空，A 的事实被完全丢掉。**这是规则的真实代价**。判断：这是**正确但不好受**的行为——A 的持有事实本来就不该约束 B 的正文；要恢复约束，正确做法是给 B 建图谱，而不是偷用 A 的。
2. **A 与 B 同世界观，A 的物品状态在 B 里依然成立**（设定集型语料）。按规则仍会丢掉 A 的事实。缓解：这种情况下应把 A 的组按 B 重建/复制，或由调用方显式传 A（运行时覆盖优先于列）。这属于「数据建模没做对」，不是取值规则错。
3. **两列都空但有同名物品跨项目**：传 `None` → 读通道如实报 `ambiguous`，不猜。与 F.2 的 NULL 语义一致。
4. **两列非空且相等**：传哪个都一样；规则不退化成「两本都读」。

**落点**：这条优先级只在一个地方实现——`engine._item_holding_text`（以及未来批量任务的同类入口），写成一个小函数（如 `_book_for_project(project)`），不要在 19 个调用点各写一遍。本轮**不实现**，只把规则定死。

### G.7 验收

| 项 | 结果 |
|---|---|
| 结构 diff（`novel_dev`） | 对象新增 0 / 删除 0 / 变化仅 `projects`；列变化仅 `projects`：**24 → 25**，末尾 `continuation_novel_id INTEGER`；表数 31 → 31 |
| 结构 diff（`generated.db`） | **零变化**（对象、列、行数全一致） |
| 覆盖 | `coverage_gid45` = **125**（改前后相同） |
| 六探针 | `暗金色长刀`/`太平刀`/`黑金长刀`/`刀胚`/`黑金刀`/`镇国剑`：status / group_id / via / lines / lines_md5 **逐字相同** |
| 三个 md5 | p1 段1、p1 段2、p5 整块 → **全部相同** |
| 持有行 | 4 组（gid 20/45/33/5）**11 条**，`holding_bad = []`，逐条相同 |
| 两库逐表行数 | 与改前备份 `count_diff = {}`（novel_dev 31 表、generated 13 表）；**测试跑完复比仍为空** |
| 测试 | **350 tests / 0 failures / 0 errors / 0 skipped**（junit 计数）；按既有方式跳过 `tests/stress` + 15 个顶层 `sys.exit(` 的脚本式文件，**未改任何测试文件** |
| ORM / 幂等 | `Project` 5 行读出 `active=5` 与 `cont=None`；`ensure_schema()` 二次调用不报错 |
| 回滚实测（副本） | `copy → apply`（24→25 列）`→ rollback` → 与生产原库比：**列数 24 一致、DDL 归一后一致、索引一致、5 行数据一致** |
| `active_novel_id` 未受影响 | 列名、值（项目 5 = 5）、物理位置均未变 |

### G.8 缺口

1. **只存位置，不接任何东西**：生成链路、读侧、前端、API 全部未接；`_book_for_project()` 的优先级规则只写在 G.6。（**已在本轮 §H 补上**）
2. **无 FK**：`continuation_novel_id → raw_novels.id` 是同库逻辑引用，`ALTER ADD COLUMN` 加不了外键；删书不会级联清空。
3. **无写入入口**：`ProjectOut`/`ProjectPatch` 未暴露，前端没有「从哪本续写」的控件。目前只有迁移脚本能写。（**已在本轮 §H.5/H.6 补上**）
4. **「从第几章续」无处安放**：如果将来要精确续写起点，需要 `continuation_start_chapter` 之类的新字段，或改用带属性的关联表——那才是 G.3 里关联表真正划算的时候。
5. **不一致只告警的实现还没写**：G.2 的 warn 日志要等生成入口接线时一起加，现在库里不存在不一致的样本（全 NULL），所以没有可验证的路径。
6. **G.6 优先级是默认值，不是强制**：调用方（如批量任务）若明确知道「就是写 B」，应显式传 B，覆盖列值；规则不阻止运行时覆盖。

## H. 接线：取值函数、读通道、API、前端（本轮）

### H.0 一句话结论

`_book_for_project` 落在 [engine.py](backend/app/generation/engine.py#L710-L726)；读侧 **1 处**真正传值（两个调用点共用这一个函数），写侧 **0 处**（全仓无生产调用方，保持默认 `None`）；API/前端入口已做。项目 5 传进去的值从 `None` 变成 `5`，**实测输出逐字未变**（理由不是「理论上一致」，而是 H.2 的三组数）。**没有触发红线**。

### H.1 取值函数的落点

| 项 | 落点 |
|---|---|
| 函数 | `engine._book_for_project(project)`，[engine.py L710-726](backend/app/generation/engine.py#L710-L726) |
| 顺序 | `continuation_novel_id` → `active_novel_id` → `None`；两列都缺（老对象/老库）也是 `None`，不猜 |
| 真正传值处 | [engine.py L774-778](backend/app/generation/engine.py#L774-L778) `current_item_holdings(..., novel_id=_book_for_project(project))` |
| 调用点 | `build_context`（[L621](backend/app/generation/engine.py#L621-L623)）与单段重生成（[L1951](backend/app/generation/engine.py#L1951-L1954)）都只调 `_item_holding_text`，不在调用点重写顺序 |

`_active_item_entries` **没有接**，这是有意的：`kg_entities` / `kg_mentions` 都没有 `novel_id` 列（[models.py](backend/app/models.py#L219-L247)），加参数也只能原样透传、形同虚设。按书收窄只发生在下游 `current_item_holdings`，函数 docstring 里写明了「假过滤比不过滤更危险」。代码里因此没有任何「看起来接了、其实没生效」的参数。

### H.2 决定性实测：传 `None` 与传 `5` 逐字对比（改代码之前跑）

项目 5 现状 `continuation=NULL → active=5`。在**本段没有 generation 混淆**的前提下，用同一批物品分别按 `novel_id=None` 与 `5` 调读通道：

| 项 | `novel_id=None` | `novel_id=5` | 是否相同 |
|---|---|---|---|
| 登记表加载的组数 | 53（书 5 的 45 + `novel_id IS NULL` 的 8） | 53（45 + 8） | 相同 |
| 15 个物品的 facts | — | — | **完全相同** |
| 渲染文本长度 / md5 | 1018 / `6c15c704a8b78a04adfed097af76bf50` | 1018 / `6c15c704a8b78a04adfed097af76bf50` | 相同 |
| 结果里出现的 group_id 集合 | `{9,18,24,29,33,45,46,100,101}` | 同左 | 相同 |
| 状态分布 | `held 9 / generic 2 / no_record 5` | 同左 | 相同 |
| 六探针（status/group_id/via/lines/lines_md5） | — | — | **6/6 逐字相同** |
| `coverage_gid45` | 125 | 125 | 相同 |

**关键风险点是 `_fetch_holding_rows` 的 `fallback = novel_id is None`**：传了书就会跳过 `group_id IS NULL` 名字/id 兜底。实测这份库里 **`kg_item_holding` 的 60 行中 `group_id IS NULL` 的行 = 0 行**，兜底路径没有任何行可丢——所以「传书会让注入事实变少」的风险在本数据上不存在，不是靠推理压下去的。

另外两点如实记录：

1. 项目 5 的段 1/2 在 `kg_mentions` 里**没有物品提及**，`_active_item_entries` 返回空 → `_item_holding_text` 在这两个段上本来就返回空串。也就是说接线对项目 5 的**现有正文生成**是零影响（结构上就不可能变），p5 的 md5 是 15 物品整块探针，不是某段生成结果。
2. 项目 1 三个段的 `_item_holding_text`（书 = `None`，两列都空）接线前后 md5 不变；段 1/2 仍是 `d056037b…` / `a2e9207c…`。

**结论：没有出现「传书导致正文生成结果变化」，不需要停下上报，也不需要调参。**

### H.3 读写两侧各接了几处

| 侧 | 接了几处 | 具体 |
|---|---:|---|
| 读 | **1** | `engine._item_holding_text` → `current_item_holdings(..., novel_id=...)`；两个生成调用点共用 |
| 写 | **0** | 见 H.4；保持既有默认 `novel_id=None` |
| API | 1 | `PATCH /api/projects/{id}` 校验两个字段 |
| 前端 | 1 | 项目工作台第 1 步「项目设置 · 书级事实」区块 |

### H.4 写侧卡在哪（为什么是 0 处，不是漏做）

1. `generated_store.resolve_group_id` / `record_holding` / `record_story_segment` 上一轮已支持 `novel_id`，但**全仓没有生产调用方**——只有 `backend/migrations/*` 脚本和测试在调（`grep` 证据：`app/` 下除 `generated_store.py` 自身，无其它命中）。
2. 批量抽取 `batch_worker._persist_extraction(db, project_id, ...)` 手上**有** `project_id`，但它只落 v3.0 的 9 张表（entities / relations / mentions / events / character_state / foreshadow …），**从不写 `generated.db`**。队列项 `_QueueItem` 也只有 `project_id / segment_no / text / chapter`，没有「书」这个字段。
3. 也就是说：写侧链路里根本没有「这一批属于哪本书」的来源；硬塞一个 `novel_id` 参数下去，没有任何调用方会给它赋值，等于给未来埋一个永远是 `None` 的假接口。

按约定**没有造一个猜出来的书 id**，两个写函数保持 `novel_id: int | None = None` 原样。待办见 H.7 第 1 条。

### H.5 API：设置 / 清空 / 拒绝

字段加在 `ProjectPatch` 与 `ProjectOut`（[schemas.py](backend/app/schemas.py)），校验在 [projects.py `_validate_book_fields`](backend/app/routers/projects.py#L487-L518)，且放在 `setattr` **之前**（非法时整条 patch 不落库）。

| 规则 | 行为 |
|---|---|
| `null` | 允许，清空回 NULL（原创 / 不按书过滤的合法状态） |
| 书不存在 | 400「事实来源书指定的书 #9999 不存在」 |
| 不在可写范围内 | 400「续写书指定的书 #2 不在本项目的可写范围内（参考语料=[5, 6]）」 |
| 可写范围 | `project.reference_novel_ids`；同一次 PATCH 里改了 refs 就按**新值**判断；refs 为空 = 没有可写的书，指定任何书都拒 |
| 独立性 | 两个字段各查各的，只设一个不会动另一个 |

对**真实服务**（重启后的 `:8000`）实测：

```
same-values: 200 {'active_novel_id': 5, 'continuation_novel_id': None}
bad-id    : 400 事实来源书指定的书 #9999 不存在
out-scope : 400 续写书指定的书 #2 不在本项目的可写范围内（参考语料=[5, 6]）
```

设值 / 清空的完整往返在 `tests/test_book_scope_wiring.py` 的隔离库上跑（不碰生产库，避免测完留下脏值）：独立设置、一起清空、非法 id、越界书、无参考语料——5 个 API 用例。

### H.6 前端：位置与截图

**在哪个页面、什么位置**：项目工作台（`/projects/5`）→ 第 1 步「参考语料」卡片内，参考语料 chips 与「蒸馏档位」之间，新增一块「项目设置 · 书级事实」：两个下拉框（事实来源书 / 续写书）+「保存书设置」按钮。续写书首项是「原创 / 不续写」，事实来源书首项是「不限（不按书过滤）」，两者都可留空；选项只列本项目已选的参考语料（与服务端可写范围一致）。

![项目设置 · 书级事实](assets/h1-project-settings-book-scope.png)

截图里项目 5 显示「事实来源书 = 大奉打更人.txt #5、续写书 = 原创 / 不续写」——与库里的 `active=5 / continuation=NULL` 一致。浏览器里实点了一次「保存书设置」：出现「书级事实已保存。」提示，下拉框保持原值（等于把原值再 PATCH 一遍，不改变状态）。

### H.7 现在还有哪些链路没传书

1. **写侧全部**：`record_holding` / `record_story_segment` / `resolve_group_id` 在生产链路上没有调用方，自然也没有书可传。要接，先得有「这一批属于哪本书」的来源（批量抽取的入队项或项目上推导），再在落库时传下去——本轮明确留作待办。
2. **按 `KgEntity` / `KgMention` 取物的链路**（`_active_item_entries`、`_kg_slice_text` 等）：这两张表没有 `novel_id`，**不是忘传，是没有维度**。要按书收窄，必须先给实体/提及加书列并回填，属于另一次表结构变更。
3. **建图链路**：`narrative.py` 的自检走 `build_graph(db, project.id, novel_id=nid)`，入参是**本次蒸馏的语料 id**，不是项目书，不属于本链路，未改。
4. **`current_item_holdings` 的其它调用方**：全仓扫描只有 `engine._item_holding_text` 一处（grep 结果 7 行，其余都在 `item_holding_read.py` 内部）。
5. **`§G.2` 的两列不一致只告警**：仍未实现（当前库里不存在不一致样本，没有可验证路径）。

### H.8 验收

| 项 | 结果 |
|---|---|
| 覆盖 | `coverage_gid45` = **125**（接线前后相同） |
| 六探针 | `太平刀`/`暗金色长刀`/`黑金长刀`/`刀胚`/`黑金刀`/`镇国剑`：status / group_id / via / lines / lines_md5 **逐字相同**（`镇国剑` 仍是 gid 100） |
| 三个 md5 | p1 段1 `d056037b0a0a56cee379646ca54e8e18`、p1 段2 `a2e9207ca2e028a05778b0dd8e884561`、p5 `6c15c704a8b78a04adfed097af76bf50`，**全部相同** |
| 持有行 | 4 组（gid 20/45/33/5）**11 条**，`holding_bad = []`，逐条 true |
| 传值变化 | 项目 5：`None → 5`；项目 1：仍是 `None`（两列都空） |
| 登记表可见性 | 传 `5` 时加载 **53** 组 = 书 5 的 45 + `novel_id IS NULL` 的 8；与传 `None` 相同 |
| 测试 | **362 tests / 0 failures / 0 errors / 0 skipped**（junit 计数；比上轮 350 多 12 条新用例）；按既有方式跳过 `tests/stress` + 15 个脚本式文件，**未改任何既有测试** |
| 新增测试 | `tests/test_book_scope_wiring.py`：三档优先级 4 条、读通道传值 3 条、API 5 条 |
| 结构 | 本轮 **0 个 schema 变更**：无新表、无新列、无新索引；只改 Python / TS / Vue |
| 红线 | 未触发（H.2 实测无变化） |

## I. 实体层 / 提及层的书维度（本轮）

### I.0 一句话结论

给**活着的** `kg_entities`（64 行）与 `kg_mentions`（34 行）加了可空 `novel_id`：
实体唯一键换成含书维度的表达式索引，提及跟随所属实体的书。回填 64 → 42 条归书 5、
22 条留 `NULL`；批量抽取链路已真正落书；`generated_store` 三个写函数只加了签名
（它们仍无生产调用方）。单书行为与 md5 逐字不变。

### I.1 认定过程：哪张表是活的，`kg_mentions` 到底动不动

| 表 | 行数 | 最近写入 | 写入方 | 判定 |
|---|---:|---|---|---|
| `kg_entities` | 64 | 2026-11-10 | `batch_worker._upsert_entity` | **活，加列** |
| `kg_mentions` | 34 | 2026-11-10 | `batch_worker`（LLM / alias 两条路径） | **活，加列** |
| `kg_relations` | 23 | 2026-11-10 | `batch_worker` | 活，但本轮不加（见 I.9） |
| `kg_events` / `kg_event_links` | 28 / 5 | 2026-11-10 | `batch_worker` | 活，不加 |
| `generations` / `raw_novels` / `novel_chunks` | 5 / 5 / 7941 | 2026-11-10 | 生成 / 导入链路 | 活，不加 |
| `kg_character_state` / `kg_entity_knowledge` / `kg_foreshadows` / `kg_relation_evolution` | 0 | 无 | 无 | **死表，不动** |

`kg_mentions` **不是死表**（34 行、全 `mention_kind='llm'`、全有 `generation_id`），所以没有跳过它。
但"提及层能不能按 chunk 归书"的答案是不能：

- 提及的上游是 `generations`（本项目自己写的正文），**表里没有 `chunk_id`**；
- `novel_chunks → chunk → mention` 这条链在库里根本不存在（`novel_chunks` 只服务参考语料检索与蒸馏）。

因此提及只能**跟随所属实体**归书：`mention.novel_id = entity.novel_id`。语义上也成立——
一段正文讲的是哪本书的事，取决于它提到的实体属于哪本书。

### I.2 分槽规则

| 层 | 唯一键 / 归属规则 | 同名跨书 |
|---|---|---|
| 实体 | `(project_id, kind, canonical_name, COALESCE(novel_id, -1))` | **各成一行，不共享** |
| 提及 | 唯一键不动：`(project_id, entity_id, generation_id)`；`novel_id` 只作过滤列 | 挂在各自书的实体上，天然不串 |

- `NULL` = **项目级通配**：读侧 `novel_id IS NULL OR novel_id = 当前书` 都可见，与 `kg_item_group`
  的 `COALESCE(novel_id,-1)` 同一口径。
- 为什么是表达式索引而不是 `UniqueConstraint`：SQLite/PG 的唯一约束里 `NULL` 互不相等，
  两条同名的 `NULL` 实体挡不住；折成 `-1` 才锁得住"通配只有一条"。
- 写侧命中顺序：**真书行 → `NULL` 通配行 → 新建（带书）**。已有 NULL 行时带书写入复用 NULL 行，
  不新建重复——这是单书场景"行为不变"的前提；反过来，书 5 的写绝不会并到书 6 的同名实体。

**与组表会不会形成两套矛盾的分槽规则**（停止条件）：迁移脚本在写库**之前**逐条比对了
「实体归书」与 `kg_item_group.novel_id` 的归书，`conflicts = []`。唯一两处跨两本参考语料的
名字（`玉扳指` / `锦囊`）在实体层留了 `NULL`，没有硬判成某本书，所以也不构成矛盾。

### I.3 表结构改动与方式

| 表 | 改动 | 方式 |
|---|---|---|
| `kg_entities` | 加 `novel_id INTEGER NULL`；删表级 `UNIQUE(project_id, kind, canonical_name)`；建唯一表达式索引 `uq_kg_entity` | **12-step 重建**（保留 `id`，`kg_relations`/`kg_mentions` 外键不断）；表级 UNIQUE 在 SQLite 里改不了 |
| `kg_mentions` | 加 `novel_id INTEGER NULL` | `ALTER TABLE ADD COLUMN`（唯一键不动） |

迁移脚本：[apply_kg_book.py](backend/migrations/2026-11-17-kg-entity-mention-novel-v1/apply_kg_book.py)
（`--dry-run` 默认 / `--apply` / `--rollback` / `--db` / `--generated-db` / `--backup-dir`；
先自动全库备份，再写快照）。老库启动时 [db.py](backend/app/db.py) 的补列清单会加 `novel_id`，
并补建同名唯一索引；但**去不掉旧的表级 UNIQUE**，真正换键必须跑上面的迁移。

### I.4 回填：覆盖多少行、多少归不出

规则：`canonical_name` + `aliases` 在项目 `reference_novel_ids` 里**唯一命中一本书**才归书，
否则 `NULL`。

| 对象 | 总数 | 归书 5 | `NULL` |
|---|---:|---:|---:|
| 实体 | 64 | **42** | **22** |
| 提及 | 34 | **2**（老李，段 1/2） | 32 |

22 条归不出的实体，逐条原因：

| 原因 | 条数 | 例子 |
|---|---:|---|
| 语料里没有这个名字（原创内容） | 7 | `李浩`、`出租屋`、`策划书`、`邮件`（项目 1）；`小杨`、`失踪者A`、`失踪者B`（项目 5） |
| 只命中非参考语料（该项目的语料里没有它） | 13 | 项目 1 的 `公司`、`会议室`、`京城`、`笔` 等 |
| 命中多本参考语料（跨书同名，无法唯一判定） | 2 | `玉扳指`、`锦囊`（书 5 / 书 6 都有） |

- 项目 1（`Assistant 测试项目`）`reference_novel_ids = null`，17 条实体全 `NULL`——这不是归书失败，
  是"这个项目没有书"。
- 提及只有 2 条归到书 5：项目 5 的 8 条提及里，6 条挂的实体（`失踪者A/B`、`小杨`）本身就归不出书。
- 快照：[kg_book_20261111-013854.json](backend/migrations/2026-11-17-kg-entity-mention-novel-v1/snapshots/kg_book_20261111-013854.json)
  （含逐条归书依据、`before`/`after` 结构、备份路径）。

### I.5 写侧：改的是签名，还是真的接了链路

**接了链路的一处**（批量抽取，真在写）：

- [batch_worker.py `_run_batches_locked`](backend/app/kg/batch_worker.py#L271-L276)
  每批算一次 `book = book_for_project(db.get(Project, project_id))`，传给
  `_persist_extraction(..., book=book)` 与两条 `_index_mentions_only(..., book=book)`；
- `_persist_extraction` 里：新实体带 `novel_id=book`，`_lookup_entity_id(..., novel_id=book)`
  跨书不串，mention 插入带 `novel_id=book`；
- `_upsert_entity`：查重限定「本书 + NULL」，真书行优先；
- 书来源与读侧**同一个** `book_for_project`（[book_scope.py](backend/app/kg/book_scope.py)），
  `engine._book_for_project` 已改为它的别名，不存在"读侧续写优先、写侧来源优先"的分叉。

**只改了签名的一处**（没有生产调用方，不假装接线）：

| 函数 | 新参数 | 生产调用方 | 说明 |
|---|---|---|---|
| `resolve_group_id` | `project_id=None` | **0** | `generated.db` 的组行是多项目共存的；传了才 `AND g.project_id=?` |
| `record_holding` | `project_id=None` | **0** | 透传给 `resolve_group_id` |
| `record_story_segment` | `project_id=None` | **0** | 同上 |

它们是 [generated_store.py](backend/app/kg/generated_store.py#L510-L575) 里的库函数，只有迁移脚本与测试在调。
按约定**没有造假调用方**，用 `tests/test_kg_entity_book_scope.py` 的两条集成测试证明
「按 `project_id` 解析到正确的组」——**改的是签名，不是链路**。

**现在谁还在传 `None`**：

1. `_persist_extraction` / `_index_mentions_only` 的 `book` 默认 `None`：只有不经过
   `_run_batches_locked` 的直接调用（脚本、单测）会拿到 `None`，落 `NULL` 通配；
2. `resolve_group_id` / `record_holding` / `record_story_segment` 的 `project_id` 默认 `None`：
   因为调用方是 0，目前**全部**是 `None`；
3. 迁移脚本回填时按规则主动留 `NULL` 的 22 条实体（见 I.4）；
4. **没重启的 `:8000` 进程**仍在跑旧代码：它的写入不会带 `novel_id`（落 `NULL`，语义仍是通配），
   重启后新写入才落书。

### I.6 测试

新增 [tests/test_kg_entity_book_scope.py](backend/tests/test_kg_entity_book_scope.py) 10 条：

| 锁什么 | 用例 |
|---|---|
| 分槽：同名不同书两行 / 同书幂等 / 已有 NULL 行被复用 | 3 |
| 唯一键：两条同名 NULL 被 `COALESCE(-1)` 挡住 | 1 |
| 写侧反查：`_lookup_entity_id` 不跨书 | 1 |
| 写侧 mention：降级路径按书收窄候选、新行落当前书 | 1 |
| 读侧提及：传书只看到「本书 + NULL」 / 不传书全可见 | 2 |
| 组解析签名：`resolve_group_id` / `record_holding` 按 `project_id` 收窄 | 2 |

全量：**372 tests / 0 failures / 0 errors / 0 skipped**（junit 计数；上轮 362 + 本轮 10）。
跳过方式不变：`tests/stress` + 15 个脚本式文件，**未改任何既有测试**。

### I.7 验收数字（真实库，迁移后）

| 项 | 结果 |
|---|---|
| 覆盖 | `coverage_gid45` = **125** |
| 六探针 | `太平刀`/`暗金色长刀`/`黑金长刀`/`刀胚`/`黑金刀`/`镇国剑`：status / group_id / via / lines / lines_md5 **逐字相同**（`镇国剑` 仍 gid 100） |
| 三个 md5 | p1 段1 `d056037b0a0a56cee379646ca54e8e18`、p1 段2 `a2e9207ca2e028a05778b0dd8e884561`、p5 `6c15c704a8b78a04adfed097af76bf50`，**全部相同** |
| 持有行 | 11 条，`holding_bad = []` |
| 跨书回归 | 临时库：同名实体书 5/6 各一行、`_active_item_entries` 传 6 只见书 6 + NULL；不传书按旧规则全可见（I.6） |
| 库体检 | 实体 64 / 提及 34 行数不变；新键无重复；`PRAGMA foreign_key_check` 空 |

### I.8 回滚实测（库副本，不是嘴上说）

在 `<temp>\kgbk` 的 `novel_dev.db` / `generated.db` 副本上先 `--apply` 再 `--rollback`：

- 结构：`kg_entities` 回到原来的表级 `UNIQUE(project_id, kind, canonical_name)`（`sqlite_master`
  逐条比对），`kg_mentions.novel_id` 删除；
- 行数：实体 64 → 64、提及 34 → 34；
- 值：去 `novel_id` 后实体/提及行内容的 md5 与迁移前**相同**（`dce2…` / `fbed…`）；
- 副本这次产生的快照已删除，仓库里只留真实库那一份。

### I.9 停止条件：都没触发

| 停止条件 | 实测 |
|---|---|
| 新唯一键撞现有数据 | **不撞**：旧键 `(project,kind,name)` 比新键更严，现有数据在新键下必然唯一；迁移脚本另做了重复检查，结果 0 行 |
| 实体归书与 `kg_item_group.novel_id` 矛盾 | **0 条**（I.2）；脚本把它做成写库前的硬校验，触发即退出 |

### I.10 仍然留着的缺口

1. **`kg_relations` / `kg_events` 没有书维度**：跨书同名实体拆开后，关系和事件的端点仍按 id 绑定，
   不会串行；但"这条关系属于哪本书"无法直接查，只能经端点反推。本轮不加列，避免一次改四张表。
2. **写侧仍以"项目当前书"为准，不是"段文本实际所属书"**：同一项目在书 5 写完切到书 6 后，
   再补写旧段也会落书 6。要彻底解决，需要在 `_QueueItem` 里冻结入队时的书（改动会碰队列与
   `AgentRun.note` 的 payload，留作待办）。
3. **p1 这类没有参考语料的项目**：实体/提及全 `NULL`，语义是"项目级通配"，与加书维度前一致。
4. **`_active_item_entries` 之外的 `KgEntity` 读点**（`bible.py`、`entity_lookup` 的若干调用）
   仍按 project 取全量；跨书同名时会靠 `resolve_entity_by_name` 的确定性排序取一行，
   尚未逐个加书过滤。

## J. 写侧 / 提及层流程断点（只读诊断）

> 本轮**不改代码、不改数据**。所有写操作都发生在 `<temp>\diag\run\` 的库副本上；
> 真实库两个文件的 mtime 停在 `generated.db 00:49:13` / `novel_dev.db 01:38:54`，
> 本次诊断跑在 `01:45–01:52`，可以据此证明没被污染。

### J.0 一句话结论

「正文写出来 → 物品持有事实能按书读出来」这条链路，**运行时不存在任何一段会写 `generated.db`**。
v3.0 抽取链路只写 `novel_dev.db` 的九张表；库里 60 条持有行、134 条写法、53 个组**全部**由
2026-11-10 的迁移/回填脚本一次性造出，之后再无新增。这不是「某个函数没人调用」，
而是**两张库之间根本没有桥**。另有四个静默问题：线上进程跑的是旧代码（写侧书维度未生效）、
事件的 `segment_no` 一律标成批内第一段、项目 5 的物品提及为 0（持有块永远为空）、
多 worker 会重复重捞。

### J.1 链路逐跳结论

| 跳 | 位置 | 结论 |
|---|---|---|
| 正文落库 | `_commit_segment` → `generations` | **通**（实测 5 → 6 行） |
| 入队 | [engine.py `_enqueue_v3_extraction`](backend/app/generation/engine.py#L644-L665) → `enqueue_segment` | **通**（`AgentRun` 77 落库，pending=1；入队行自己 commit，不依赖调用方） |
| 起 worker | `ensure_worker()`（engine L657、main.py L57） | **通**（进程内 daemon 线程，幂等） |
| 启动恢复 | [main.py L54-L66](backend/app/main.py#L54-L66) → `recover_running` | 单进程**可用**；多 worker 会重复（J-5） |
| 批抽 | `_run_batches_locked` → `extract_batch` | **通**（实测 success，`entities_upserted=6`） |
| 落 v3.0 表 | `_persist_extraction` | **通**（实测实体 +2、提及 +4） |
| **v3.0 表 → `generated.db`** | 无 | **断**：实测三个阶段全部 0 增量（J-1） |
| `generated.db` → 读通道 | `current_item_holdings` | **通**：绕过 mention 直接查 43 个物品名 → 43 条命中 |
| 提及 → 读通道 | `_active_item_entries` | **断**：p5 段 1/2 物品提及 0 条 → 返回 `[]`（J-2） |
| 读通道 → 提示词 | `_item_holding_text` | 有 items 才渲染；实测段 3/4 有块、段 1/2 空串 |

### J.2 断点表

| # | 断点位置 | 现象（证据） | 断的是 | 影响面（会不会静默出错） | 修法 | 代价 | 优先级 |
|---|---|---|---|---|---|---|---|
| J-1 | v3.0 表 → `generated.db` | `record_holding` / `record_story_segment` / `resolve_group_id` 在 `app/` 内**0 个调用点**（只有 `generated_store` 内部、`migrations/`、`tests/`）；实测：真实 provider 抽取 success（实体 6、提及 +4）后 `generated.db` 全表零增量；喂一份「完美抽取」（实体 +2 / 关系 +1 / 事件 +1）直接打 `_persist_extraction`，仍是零增量 | **数据流** | **静默**：`AgentRun` 记 `success`、前端显示抽取成功，持有层永远不增长 | 在 `_persist_extraction` 末尾加「持有投影」：从本批 item 实体 + `owns`/`custody` 关系 + 事件里生成 `record_holding(...)` / `record_story_segment(...)`；幂等键用 (project, book, item, hold_type, chapter) | 中：要先定义持有三元组的抽取字段（`hold_type`/`owner_state`/`reason_code`）与幂等口径 | **P0** |
| J-2 | 提及层 → `_active_item_entries` | p5 的 8 条提及**全部指向 character**（entity 63/64/65/66），item 提及 **0** 条 → `_active_item_entries(seg1/2)` 返回 `[]`，`_item_holding_text` 在 [engine.py L749](backend/app/generation/engine.py#L744-L767) 直接返回 `""`。副本上写了两段带物品名的正文后，段 3/4 立刻有 4–6 个物品、持有块非空 → **读通道本身是好的**，卡在「没有物品提及」 | **数据流**（不是代码 bug，是覆盖率断） | **静默**：提示词里少一块，模型照写，无人报错 | ① 抽取时对 item 实体也做一次文本命中索引（现在只给「LLM 抽到的实体」建 mention）；② 或 `_item_holding_text` 允许「用本段正文命中物品名」直接查持有层，不依赖历史 mention | 小：① 复用 `canon_source.text_mention` + `_first_mentioned_form`；② 改一处取数 | **P0**（与 J-1 同组） |
| J-3 | 部署：运行中的 `:8000` 进程 | `PID 37656 = python -m uvicorn app.main:app --port 8000`（**无 `--reload`**），`CreationDate 2026/11/11 1:20:50`；而 `batch_worker.py` mtime `1:37:59`、`models.py 1:32:10`、`book_scope.py 1:32:23` → 线上进程比书维度代码**早 12–17 分钟** | **数据流**（写侧书维度线上等于没上线） | **静默**：没有版本探针，接口行为与改前一致，落库 `novel_id` 全是 `NULL`；书 6 的抽取还会并进书 5 的同名实体（旧 `_upsert_entity` 不带书过滤） | 重启；启动脚本统一走 `start-dev.bat`（它带 `--reload`）；加 `/healthz` 返回 git commit + 启动时间 | 极小 | **P0** |
| J-4 | 事件 / 状态变化的段号 | [batch_worker.py L862](backend/app/kg/batch_worker.py#L858-L872) `segment_no=batch[0].segment_no`、[L959](backend/app/kg/batch_worker.py#L952-L962) 同；而批大小是 5 → 后 4 段的事件全挂在第 1 段。读取方按段过滤：[judge.py L117](backend/app/kg/judge.py#L117)、[retrieval_v3.py L151](backend/app/kg/retrieval_v3.py#L151-L153) | **数据流**（写错，不是漏写） | **静默**：judge 判段 2 看不到段 2 的事件；检索窗口最多偏 4 段 | 抽取结果里带段号（或让 LLM 回 `segment`），拿不到时再用 `batch[0]` 兜底并打标 | 中：要动 `extractor_v3` 的输出契约 | **P1** |
| J-5 | 多 worker 下的启动恢复 | [batch_worker.py L544-L546](backend/app/kg/batch_worker.py#L532-L546) 自带 `TODO(多进程)`：`--workers>1` 时每个进程都会 `recover_running` 一遍；`_queue` 是模块级内存 `deque`，`_run_gate` 只是进程内 `RLock` | **能力缺口**（当前单进程，不触发） | 静默重复写：实体 upsert 幂等，但 mention 的「查重 → 插入」非原子、relation/event 是追加型 → 会出现重复事件 | 上多进程前：`recover_running` 改成「条件 UPDATE 抢占」或只让一个进程恢复；或换外部队列 | 中 | **P2** |
| J-6 | 队列溢出静默丢段 | [batch_worker.py L85](backend/app/kg/batch_worker.py#L80-L90) `while len(q) > MAX_QUEUE: q.popleft()`，被丢的段 `AgentRun` 行仍停在 `running` | 能力缺口 | 静默延迟：单进程长期不重启就一直挂着（重启会被 `recover_running` 捞回） | 溢出时把被丢段的 `AgentRun` 标 `failed` + `error=queue_overflow`，或加告警 | 小 | **P2** |
| J-7 | `NULL` 实体的跨书可见性 | 22 条 `NULL` = 项目级通配，读侧三处（`_active_item_entries`、`_lookup_entity_id`、`resolve_entry_by_name`）都是 `novel_id IS NULL OR = book`。p5 的 5 条是模板垃圾：`小杨`/`失踪者A`/`失踪者B`/`玉扳指`/`锦囊` | 能力缺口（口径本来如此） | 静默可见：p5 一旦有续写书 6，这 5 条在书 6 里也能被解析到 | 人工判定这 5 条归属（`玉扳指`/`锦囊` 是书 5/6 同名，本就不该硬判）；模板垃圾实体建议清掉 | 小 | **P2** |
| J-8 | `kg_mentions` 没有 `chunk_id` | 列：`id/project_id/entity_id/generation_id/segment_no/context_window/mention_kind/novel_id`；读者（engine L690/L722、judge L127/L176/L251、retrieval_v3 L53/L332）全部按 `segment_no` / `generation_id` 读，**没有按 chunk 的读法** | 能力缺口 | 不静默（当前无需） | 若将来要「章节内第几段证据」再加；`context_window` 已存命中片段，够定位 | 小 | **P3** |
| J-9 | 抽取失败只落 `failed`，不自动重来 | 毒批 `failed`，只能靠 `POST /kg-v3/worker/requeue` 或重启；生成主链完全不报错 | 能力缺口 | 静默：不看 `worker/status` 就不知道 | 前端加失败徽标 / 自动重排队；`AgentRun.note.error` 已有 | 小 | **P3** |

### J.3 真断点（现在就是错的或漏的）

1. **J-1 两张库之间没有桥**。抽取 `success` 是个假信号——它只证明「v3.0 九张表写完了」，
   不证明「持有事实进库了」。这一条决定了整个物品持有能力在运行时**只读不写**。
2. **J-2 提示词里那块持有事实为空的直接原因**：p5 的历史段落是模板文本（「小杨站在废弃的工厂前」），
   里面没有任何物品名 → 物品提及 0 条。这不是代码错，是**覆盖率断**，但它和 J-1 组合起来的结果是：
   「60 条持有事实在库里、读通道也能读、p5 已有的两段却一次都没渲染进提示词」。
3. **J-3 线上进程比代码旧 12–17 分钟**，书维度写侧代码实际没跑。这条最危险——
   它没有任何可观测信号，接口返回、行数、md5 全都正常，只有新写入的 `novel_id` 是 `NULL`。
4. **J-4 事件段号一律 `batch[0]`**。批大小 5，等于每 5 段里有 4 段的事件被挂到第 1 段上。
   这是**写错的数据**，不是缺数据，且读侧（judge / retrieval）都按段过滤，错位会一直传下去。

### J.4 能力缺口（不是错，只是没建）

- **J-5 多进程恢复**：当前单进程（`--port 8000` 无 `--workers`）不触发；上多 worker 前必须改。
- **J-6 队列溢出丢段**：行还在，只是停在 `running`，重启能捞回。
- **J-7 `NULL` 实体通配可见**：口径如此；问题是这 5 条 `NULL` 是模板垃圾/跨书同名，值不值得留。
- **J-8 提及无 `chunk_id`**：读者不需要它，现在加是过度设计。
- **J-9 毒批不自动重试**：设计选择（避免打爆配额），缺的是可见性。

### J.5 单答一：60 条持有行 / 134 条写法 / 53 个组，到底是谁写的

**全部来自迁移/回填脚本，运行时一条都没写过。** 证据是 `created_at` 只有几个同秒批次，
且都落在 2026-11-10：

| 表 | 行数 | `created_at` 批次 | 产生者 |
|---|---:|---|---|
| `kg_item_holding` | **60** | `06:44:23Z` × 53、`07:55:05Z` × 5、`09:38:16Z` × 2 | [migrate_v3_item_holding.py](backend/migrations/2026-11-10-item-holding-v3/migrate_v3_item_holding.py)（`holdings_v3.json` → `record_holding`，只灌 `verdict="转持有"`）、[apply_gap_fill.py](backend/migrations/2026-11-10-item-holding-v3/apply_gap_fill.py)、`apply_segment_precision.py` / `apply_item_merge.py` |
| `kg_item_group` | **53** | `2026-11-10 09:59:48` × 45、`18:35:04` × 2、`18:45:52/53` × 3、`21:06:04+08:00` × 3 | [apply_item_group.py](backend/migrations/2026-11-11-item-group-v1/apply_item_group.py) + [apply_group_backfill.py](backend/migrations/2026-11-11-item-group-backfill-v1/apply_group_backfill.py) + 21:06 的分槽补丁（同刻有 `backups/generated-pre-split-20261110-210604.db` 佐证） |
| `kg_item_surface` | **134** | `09:59:48` × 130、`10:05:22` × 1、`21:06:04+08:00` × 2、`23:57:43` × 1 | 同上（组脚本连写法一起写）；状态分布：approved 114（书 5 `102` + `NULL` `12`）、rejected 19、pending 1 |
| `kg_item_instance` | 1 | `08:50:02Z` | `verify_instances.py`（地书「三号」实例） |
| `kg_holding_story_segment` | 4 | `07:55:05Z` × 4 | `apply_gap_fill.py` 的故事轴补丁（镇国剑 / 朴刀） |

对照运行时：`kg_batch_extract` 的 `AgentRun` 只有 **5 行 success**（2026-11-10 13:29–17:22），
`generations` 只有 5 行，`gen_nodes` / `gen_edges` **0 行**（影子抽取默认关闭）。
**结论：运行时既没写持有层，也没写影子层。所谓「v3.0 抽取链路」实际产出只有 `novel_dev.db`
的实体/提及/关系/事件四张表，持有层完全靠脚本。**

### J.6 单答二：现在写一章正文，跑完抽取后 `generated.db` 会多出什么（实测）

环境：`novel_dev.db`（131 MB）+ `data/generated.db`（246 KB）复制到 `<temp>\diag\run\`，
`DATABASE_URL` / `KG_GENERATED_DB` 指向副本；project 5（`active_novel_id=5`）。
三个阶段分别跑「真实 provider 全链」「provider 不可用降级」「完美抽取直接打落库层」：

| 阶段 | 怎么跑的 | `novel_dev.db` 增量 | `generated.db` 增量 |
|---|---|---|---|
| P1 | `insert Generation(seg=3)` → `_merge_segment_into_graph` → `_enqueue_v3_extraction` → `flush_project(5)`；provider=`ZhipuChatProvider`，`AgentRun 77 = success`，`entities_upserted=6` | `generations +1`、`agent_runs +1`、`kg_entities +2`、`kg_mentions +4` | **无（0 行）** |
| P2 | 同上，但把 `_get_provider` 强制为 `None`（走 alias 提及降级）；`AgentRun 78 = failed(alias_only)`，`mentions_added=6` | `generations +1`、`agent_runs +1`、`kg_mentions +6` | **无（0 行）** |
| P3 | 手工构造满分 `ExtractionResult`（item + character + `owns` 关系 + `incident` 事件），直接调 `_persist_extraction(..., book=5)` | `entities +2`、`relations +1`、`events +1`（`stats` 逐项对得上） | **无（0 行）** |

**会多出**：`generations` 一行正文、`agent_runs` 一行批抽凭据、`kg_entities` 新实体、
`kg_mentions` 若干（LLM 路径或 alias 路径）、以及理想情况下的 `kg_relations` / `kg_events`。

**不会多出**：`kg_item_group`、`kg_item_surface`、`kg_item_holding`、`kg_holding_story_segment`、
`kg_item_instance`、`gen_nodes`、`gen_edges`、`gen_events` —— 一张都不动。

顺带的读侧对照（同一副本）：

| 段 | `_active_item_entries` | `_item_holding_text` |
|---|---|---|
| 段 1 / 2（真实历史段） | `[]` | `''` |
| 段 3 / 4（本次实测写的段） | 4 / 6 个物品 | **非空**（七星剑→女子国师、军弩→许七安 47–106→长公主 106–109→许七安 109–∞ …） |
| 绕过 mention，直接拿书 5 的 43 个物品名问持有层 | 43 / 43 命中 | — |

这张对照说明了断点的准确位置：**读通道和持有数据都是好的，缺的是「写持有事实」和
「物品提及」这两段**。

### J.7 修复清单（排序，含必须一起改的组合）

| 顺序 | 项 | 为什么必须在这一步 | 单独改会不会生效 |
|---|---|---|---|
| 1 | **J-3 重启 `:8000`**（顺带统一启动方式 + 版本探针） | 代码在磁盘上、不在进程里，后面所有改动都等于没上线 | 单独改：能生效，但只是把「旧代码」换成「当前代码」，持有层仍然不写 |
| 2 | **J-1 + J-2（必须一起改）** | J-1 让持有层开始增长；J-2 让新段落的物品真的被提到。只改 J-1 → p5 提示词仍然空；只改 J-2 → 只能反复读旧 60 行，新事实永远进不了库 | **不生效**（缺任一条都是半条链） |
| 3 | J-4（事件段号） | 持有投影要消费事件；段号错位会把「某章谁拿到什么」挂到错段，回头修数据成本更高 | 单独改：生效，且应先于 J-1 落数据 |
| 4 | J-5 + J-6（上多进程前置） | 单进程下不触发；一旦 `--workers>1`，重复抽取与静默丢段会同时出现 | 单独改任一条都不够：抢占 + 溢出留痕要一起 |
| 5 | J-7（5 条 `NULL` 实体归属 / 清理模板垃圾） | 影响跨书可见性，属于数据口径清理，可独立做 | 单独改：生效（数据层） |
| 6 | J-8 / J-9 | 纯增强与可见性 | 单独改：生效 |

**一句话的落地顺序**：先重启（J-3），再一起做「持有投影 + 物品提及」（J-1/J-2），
然后修事件段号（J-4），最后才是多进程（J-5/J-6）与数据清理（J-7）。

## K. 执行 J-3 / J-1+J-2 / J-4：重启、持有投影、物品提及、事件段号

本节只做 J.7 排序里的前 3 项，串行完成；J-5/J-6（多进程）与 J-7（NULL 清理）未动。

### K.1 重启前后进程对照

| 时间点 | PID | 进程启动时间 | cmdline / 端口 | 结果 |
|---|---:|---|---|---|
| 开工前（陈旧进程） | 37656 | 2026-11-11 01:20:50 | `python -m uvicorn app.main:app --port 8000` / 8000 | `batch_worker.py` mtime = 01:37:59，**晚于进程启动**，进程里跑的是旧代码 |
| 第一次重启（J-3） | 38368 | 02:05:28 | 同参数 / 8000 | `/openapi.json` 200，111,873 B |
| 第二次重启（第一轮真跑验收后） | 2512 | 02:56:29 | 同参数 / 8000 | 200，111,873 B；随后为回滚真跑写入而停掉 |
| **最终重启（本轮代码定稿后）** | **2812** | **03:22:23** | 同参数 / 8000 | **晚于全部改动 mtime（`extractor_v3.py` 03:01:28 / `holding_projection.py` 03:05:55 / `generated_store.py` 03:06:02 / `batch_worker.py` 02:43:43）**；`/openapi.json` 200，111,873 B |

重启方式与原来一致：同一解释器、同一 cwd（`backend/`）、同一参数、同一端口，直接 `-m uvicorn app.main:app --port 8000`。

**「代码更新了进程还是旧的」怎么避免（结论，不改部署）**：

1. 判断标准只有一个：**进程启动时间 ≥ 被改文件的最大 mtime**。不是看端口通不通，也不是看 `openapi.json` 有没有响应。
2. 开发机：`uvicorn --reload` 只解决开发场景，不能当生产兜底（多进程/守护进程下 reload 不可靠）。
3. 部署侧的正解是「发布即新进程」：构建产物带版本号（git sha + build time），启动时写入进程环境；`/healthz` 返回 `{git_sha, build_time, started_at}`，值班和验收脚本直接比对 `started_at ≥ build_time`，不满足就判定发布失败。
4. 本项目的落地动作（后续做）：`backend/start-dev.bat` 或部署脚本在启动后自检一次 mtime > started_at 就打印 `STALE` 并拒绝标绿。

### K.2 持有投影：落到哪、去重键、怎么复用

入口：[holding_projection.py](backend/app/kg/holding_projection.py) 的 `project_holdings()`，由 [batch_worker.py](backend/app/kg/batch_worker.py) 在 `_persist_extraction` 之后调用（`_project_holdings`）。

- **写侧书维度**：`project_id` 来自入队行；`novel_id = book_for_project(Project)`（书 5 → 5）。组/写法/持有都按这个二元组落 `generated.db`。
- **数据来源**：`item.attrs.held_by`（confidence 0.6）、`owns` 关系边、事件动词白名单（0.5）。同一 `(item, owner)` 多来源只合并、不重复建行。
- **组去重键**：`ux_item_group_project_novel_name = UNIQUE(project_id, kind, COALESCE(novel_id,-1), canonical_name)`。`ensure_item_group` 先按 `(novel_id IS ? OR novel_id IS NULL)`、`ORDER BY (novel_id IS NULL), id` 查：**先复用真书组，再退 NULL 通配组**；都没有才 `INSERT OR IGNORE` 建组。所以同名组永远不会建出第二个。
- **写法去重键**：`ux_item_surface_project_novel_surface = UNIQUE(project_id, surface, COALESCE(novel_id,-1))`。`INSERT OR IGNORE`；如果同名写法已被**别的组**占着（真实数据里 `佩刀` 挂在 `黑金长刀`(45) 下且被标 `rejected`），不覆盖人工结论，改为在投影统计里报 `surface_conflict`。此时新组仍可按 `canonical_name` 被 `resolve_group_id` 解析。
- **持有去重键**：open 持有唯一（`ux_item_holding_open`）。`record_holding` 返回 `confirmed / replaced / extended_back / rejected`；重跑只会 `confirmed`，不会新增行。
- **故事段**：`record_story_segment` 按 `(holding, segment)` 幂等，重跑 `story_segments=0`。
- **失败可见**：投影抛错 → `stats.projection = {"errors": [...]}`；批状态直接标 `failed`，`AgentRun.note.error = "projection_failed: ..."`，同时打 warning。不会出现「返回 success 但持有层一行没写」。单测 `test_worker_marks_failed_when_projection_fails` 钉住这条。

**复用实证**：同一章回放第二次时 `groups_created=0 / surfaces_created=0`，组 103/104 被原样复用；`kg_item_group` 里没有同名重复组（`group by canonical_name, COALESCE(novel_id,-1) having count(*)>1` 为空）。

### K.3 物品提及为 0 的根因

**不是 prompt，也不是 schema（不需要动表）。** 根因在落库分支：

- v3.0 的 `kg_mentions` 只有在**本批 LLM 抽到的实体**上才会建；
- 库里已有的 41 条书 5 物品实体（迁移/人工灌入）只在「provider 不可用」的降级路径 `_index_mentions_only` 里被扫描；
- 所以正常成功路径下，物品一条提及都不会有；读侧 `_active_item_entries` 靠 mention 反查 → p5 永远是空。

修复：成功路径在 LLM mention 落库后，追加 `_alias_mentions_into(...)`（用库里已有实体的规范名 + aliases 做别名匹配），再刷新 mention 计数。`_alias_mentions_into` 开头补 `db.flush()`，因为 `SessionLocal(autoflush=False)` 下刚 add 的 mention 还没进 SELECT，会撞 `kg_mentions(project_id, entity_id, generation_id)` 唯一键。

**结果**：两次真跑里 item 提及都是 5 条（副本 20 条提及中 5 条、真实库 27 条中 5 条），不再是 0。

### K.4 库副本真跑一章：新增明细

副本：`backend/novel_dev.db` + `backend/data/generated.db` 先回到跑前快照（`<temp>\d4\copy_backup`），`DATABASE_URL` / `KG_GENERATED_DB` 指向副本；project 5、novel 5 的前 5 个 chunk（第 1–2 章，约 5.7k 字）；真实 provider；`enqueue_segment ×5 → flush_project(5)`。

| 表 | 跑前 | 跑后 | 新增 |
|---|---:|---:|---:|
| `kg_entities` | 64 | 70 | **6** |
| `kg_mentions` | 34 | 54 | **20**（llm 18 + alias 2） |
| `kg_events` | 28 | 33 | **5** |
| `kg_relations` | 23 | 31 | **8** |
| `kg_item_group` | 53 | 54 | **1** |
| `kg_item_surface` | 134 | 135 | **1** |
| `kg_item_holding` | 60 | 61 | **1** |
| `kg_holding_story_segment` | 4 | 5 | **1** |

逐条：

- 新增实体：`大奉京兆府`(location)、`监牢`(location)、`许七安`、`许平志`、`许新年`(character)、`税银`(item)，全部 `novel_id=5`。
- 提及 20 条：character 10 / **item 5** / location 3 / 库里已有实体 2；按段 1:5、2:3、3:3、4:4、5:5。
- 事件 5 条：段号 **1,2,3,4,5 各 1 条**（`穿越` / `得知` / `提供帮助` / `询问` / `口述`）。
- 持有投影：组 `103 税银`（novel 5, active）、写法 `282 税银 → 103 approved`、持有 `66 税银 → 许平志`（`owner_state=held, valid_from=2, reason_code=attr_held_by, confidence=0.6`）、故事旁注 `5 → holding 66 / forward / chapter 2`。
- 5 行 `AgentRun` 全部 `success`；投影统计 `{"pairs":1,"groups_created":1,"surfaces_created":1,"holdings":{"inserted":1},"story_segments":1,"surface_conflicts":[],"errors":[]}`。

**真实库同验证（跑前已备份到 `<temp>\d4\real_backup`）**：

| 表 | 跑前 | 跑后 | 新增 |
|---|---:|---:|---:|
| `kg_entities` | 64 | 77 | **13** |
| `kg_mentions` | 34 | 61 | **27**（llm 25 + alias 2） |
| `kg_events` | 28 | 33 | **5** |
| `kg_relations` | 23 | 30 | **7** |
| `kg_item_group` | 53 | 55 | **2** |
| `kg_item_surface` | 134 | 136 | **2** |
| `kg_item_holding` | 60 | 62 | **2** |
| `kg_holding_story_segment` | 4 | 6 | **2** |

- 新增实体 13 个（id 70–82）：location 7（大奉京兆府、长乐县、京兆府大牢、广南街、河、京兆府后堂、京城）、character 3（许七安、许平志、许新年）、item 2（税银、镇北王刀）、worldrule 1（大奉律法）。
- 提及 27 条：character 10 / location 9 / **item 5** / worldrule 1 / 已有实体 2；按段 1:6、2:4、3:4、4:6、5:7。
- 事件 29–33：段号 **1,2,3,4,5 各 1 条**（`醒来` / `回忆` / `得知` / `告诉` / `得知`）。
- 组 `103 镇北王刀`、`104 税银`；写法 `282 镇北王刀 → 103`、`283 税银 → 104`（均 approved）；持有 `66 镇北王刀 → 镇北王`（valid_from 1）、`67 税银 → 许平志`（valid_from 2），理由均 `attr_held_by`、confidence 0.6；故事旁注 5、6。
- 5 行 `AgentRun` 全部 `success`，无 `projection_failed`。

> 副本与真实库的实体/提及条数不同，是因为两次都是真调 LLM，模型输出不完全一致；**两边的结构性结论一致**：事件按段落、item 提及非 0、持有层开始由运行时写入。

### K.5 重跑幂等证据

同一份抽取结果回放（不是第二次调 LLM，输入完全相同，更严格）：

| 库 | 实体 | 提及 | 事件 | 关系 | 组 | 写法 | 持有 | 故事段 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 副本 delta | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 真实库 delta | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

- 事件：`events_added=0`、`events_skipped=5`（去重键 = `project_id + segment_no + event_type + subject_id + object_id + verb + summary`）。
- 投影：`groups_created=0 / surfaces_created=0 / story_segments=0 / holdings={"confirmed":N}`（副本 1、真实库 2）——命中已有 open 持有，只确认，不插新行。
- 提及：走 `kg_mentions(project_id, entity_id, generation_id)` 唯一键，delta 0。

### K.6 事件段号：一批 5 段的落库分布

两轮真跑的落库段号分布都是 `[(1,1),(2,1),(3,1),(4,1),(5,1)]`（段号 → 条数），即 **5 条事件分别落在 1/2/3/4/5 段**，按段过滤不再全挤在第 1 段。

实现分两层：

1. [extractor_v3.py](backend/app/kg/extractor_v3.py) 的 `attribute_event_segments`：模型给了**两个以上不同段号**时以模型为准（仅在「声称的段里连主体/客体/地点都没出现」时用正文纠正）；模型整批都写第 1 段（LLM 的典型退化输出）视为没标——先按正文命中猜段（`_best_segment`，并列或最高分不到次高分 1.5 倍视为分不清），分不清就按事件顺序在段间均摊。
2. [batch_worker.py](backend/app/kg/batch_worker.py) 的 `_persist_extraction`：`segment_no` 取**每条事件自己的 `segment`**（不在本批段号集合内才退 `batch[0]`）。

本地真实 LLM 第一轮就复现了「5 条全标第 1 段」的退化输出（这正是旧行为最容易踩的坑）；修完后同一批落到 1–5 各一条。

### K.7 单书探针、跨书回归、测试

**单书探针（真跑后重测，与跑前逐项比对）**：

- `gid45`（黑金长刀）覆盖：**125** 条，未变。
- 六个探针（刀胚 / 太平刀 / 暗金色长刀 / 镇国剑 / 黑金刀 / 黑金长刀）结果集合与跑前**完全相同**。
- `p1` 段 1、段 2 与 `p5` 的 `_item_holding_text` md5 与跑前**完全相同**。
- 11 条持有行的 `after_resolvable` 全为 `true`，`holding_bad=[]`。
- （附带）项目 7 的段 1 文本相对更早的 b5 快照为空；p7 不在本次验收清单，且本轮改动只涉及写侧投影与抽取落库，不改 `_item_holding_text` 读路径。

**跨书回归 / NULL 泛称**：`test_item_group_cross_novel.py`、`test_item_generic.py`、`test_kg_entity_book_scope.py`、`test_book_scope_wiring.py` 共 **49 passed**；`NULL` 泛称组仍可被书内真数组覆盖，语义未变。

**测试**：

- 投影 / worker 可见性 / 提及别名 / 组读侧 / 书维度：**45 passed**（含新增 `test_holding_projection.py` 的 8 个用例：真写组/写法/持有段、重跑幂等、复用人工组、复用 NULL 通配组、段号归属 `[1,2,3,4,4,5]`、整批标第 1 段的纠正 `[1,2,3,4,5]`、成功路径补 item 提及、事件重跑去重）。
- 脚本式老测试（[test_legacy_script_suite.py](tests/test_legacy_script_suite.py)）：脚本一行未改；补了 `PYTHONPATH=backend`（脚本按路径执行时 `sys.path[0]` 是 `backend/tests`，4 个 pytest 风格文件因此 `ModuleNotFoundError`）。门禁新增 1 个 known-broken：`test_v30_integration.py`（rhythm 平票排序 + `events 数 == 2` 依赖真实 LLM 是否产出事件；两条都在本轮改动前就存在，HEAD 实现同样失败）。
- 全量 `pytest`：**787 项 → 781 passed / 2 failed / 2 xfailed / 2 skipped**（主套件 702 项 + 脚本式门禁 85 项）。两个 failed 都是既有、与 KG 无关、文件未改动：
  - `tests/test_distill_fixes.py::test_generation_path_extraction_still_commits_project_cache`：`app/style/narrative.py` 已改为「生成路径拒绝同步蒸馏」，测试仍期望返回档案；
  - `tests/test_distill_jobs.py::test_api_shape_matches_frontend_distill_job_contract`：`distill_run_to_dict` 已多出 `health / health_note / progress_at / elapsed_minutes / expected_minutes`，测试的期望键集没更新。
  （`git status` 确认 `backend/app/style`、这两个测试文件均未改动，属既有问题，未顺手修。）

### K.8 停止条件核对

- **唯一键**：无需改。`kg_item_group` 的 `novel_id` 槽位与两条 `ux_item_*` 唯一索引在库中已存在（本轮沿用并写进新建表 DDL），投影没有「先改键才能落地」的情况。
- **53 组冲突**：未撞出重复组——组按 `canonical_name` 复用，`group by canonical_name, COALESCE(novel_id,-1) having count(*)>1` 为空。
- **物品提及 schema**：根因不在 schema，未动表结构。

## L. 执行第四项 / 第六项：多进程认领、溢出留痕、chunk_id 判定、失败可见性

本节只做这两项，串行完成；**第五项（22 条 NULL 归属）一行未碰**。改动面只有两处：[batch_worker.py](backend/app/kg/batch_worker.py) 与新增的 [test_batch_worker_claim.py](backend/tests/test_batch_worker_claim.py)（5 个用例）；未执行迁移、未改表结构。真实库与生成库的 `LastWriteTime` 都停在 **2026-11-11 03:11:38**（§K 真跑那一刻），本节所有验收都跑在临时库上。

### L.1 抢占：库内按行原子认领 + 租约（不引入外部依赖）

| 方案 | 取舍 |
|---|---|
| redis / 分布式锁 | 要新增外部服务，命中「必须引入外部依赖 → 先报」；单机 SQLite 应用没有收益 |
| 进程间文件锁 | Windows 与 POSIX 语义不一致，且拿不到「哪一行被谁领」的可查痕迹 |
| **库内认领（采用）** | 复用既有 `agent_runs` 表与 SQLite 写事务：零迁移、可查询、可人工接管 |

认领信息写在 `agent_runs.note` 的 JSON 里（不改表）：`claim_owner`（`host#pid#nonce`，唯一到进程）、`claim_at`、`claim_until`（ISO-8601 UTC）。认领是一条带条件的 UPDATE：

```sql
UPDATE agent_runs SET note = json_set(note,
         '$.claim_owner', :me, '$.claim_at', :now, '$.claim_until', :until)
 WHERE id = :id AND role = 'kg_batch_extract' AND status = 'running'
   AND (json_extract(note, '$.claim_owner') IS NULL
        OR json_extract(note, '$.claim_owner') = :me
        OR json_extract(note, '$.claim_until') IS NULL
        OR json_extract(note, '$.claim_until') < :now)
```

`rowcount == 1` 才算领到，否则返回 `claim_lost`。

**为什么多进程不会双领**：两个进程可以同时 SELECT 到同一批 running 行（WAL 下读不阻塞），但归属不由读决定。SQLite 的写事务是串行的，同一行的条件 UPDATE 只可能有一个命中——赢家 `rowcount=1`，输家要么 `rowcount=0`（`claim_lost`），要么在 WHERE 里看到「owner 非空 + 租约未过期 + 认领者还活着」被 `claimed_by:<owner>` 提前挡掉。赢家不是「先读到的人」，而是「UPDATE 真的改了这一行的人」。

**租约与接管**：`CLAIM_LEASE_MINUTES=15`。租约过期后即使 owner 还活着也允许接管（防长尾批永久占行）；接管前先把旧租约写成 `1970-01-01T00:00:00+00:00` 作废，再走同一条 CAS，因此两个接管者之间也只有一个是赢家。判活：同机按 `host#pid#nonce` 取 pid，Windows 用 `OpenProcess` + `WaitForSingleObject`（`os.kill(pid, 0)` 在 Windows 会真的杀进程，不能用），跨机读 `app_settings` 的 `kg.worker.<owner>` 心跳（15 s 续、45 s 过期）。`force=True`（人工超时重捞）无视存活直接接管。

**入队即认领**：`enqueue_segment` 写完 running 行就把 `claim_owner` 写成自己。否则另一个进程的 `recover_running` 会把本进程内存队列里正在排队的段当成「上一进程遗留」抢走。

**同进程**：`owner == WORKER_ID` 时条件直接成立，单进程下 `recover_running` 的对外行为与旧版一致，只是 note 里多了三个字段。

**写锁冲突**：`_claim_execute` 遇 `SQLITE_BUSY` 短重试 3 × 20 ms，**不 rollback**（同一事务里前面已认领成功的行会被一起回滚掉），3 次仍忙就跳过该行（`claim_busy`），交给赢家进程。

### L.2 多进程实证（两个真实 OS 进程，同栅栏放行）

两个 `subprocess.Popen` 子进程各自把 `DATABASE_URL` 指向同一个临时库（5 条 running 的批抽行），import 完成各写 `a.ready`/`b.ready`，父进程写 `go` 同时放行，各自执行 `recover_running(project_id=1)`：

```
[a] worker_id=DESKTOP-TC52OS8#27008#f57a75
     requeued=5 missing=[] overflow=[] pending=5
     skipped=[]
[b] worker_id=DESKTOP-TC52OS8#27260#523320
     requeued=0 missing=[] overflow=[] pending=0
     skipped=[{'run_id': 1, 'reason': 'claimed_by:DESKTOP-TC52OS8#27008#f57a75'},
              {'run_id': 2, 'reason': 'claimed_by:DESKTOP-TC52OS8#27008#f57a75'},
              {'run_id': 3, 'reason': 'claimed_by:DESKTOP-TC52OS8#27008#f57a75'},
              {'run_id': 4, 'reason': 'claimed_by:DESKTOP-TC52OS8#27008#f57a75'},
              {'run_id': 5, 'reason': 'claimed_by:DESKTOP-TC52OS8#27008#f57a75'}]
=== 库里的最终状态 ===
    run 1..5: status=running claim_owner=DESKTOP-TC52OS8#27008#f57a75
=== 判据 ===
    两个进程合计 requeued = 5（每行只被领一次）
    每行都有唯一 claim_owner = True
    两个进程都成功退出 = True
```

「谁领到」= a（`#27008#f57a75`）；「另一个人看到什么」= b 对 5 行全部返回 `claimed_by:a`，自己 `requeued=0`、`pending=0`，没往队列里放任何一段。同一批只被领一次。

### L.3 溢出留痕：字段与查询方式

旧实现是 `put()` 到上限后静默 `popleft`，事后只剩一行日志。现在 `put()` 返回被丢的段，`enqueue_segment` / `_requeue_row` 调 `_mark_overflow` 把每一段写回它自己的 `AgentRun` 行（不新增表）：

| note / 行字段 | 值 |
|---|---|
| `status` | `failed` |
| `finished_at` | 丢弃时刻 |
| `error` / `error_kind` | `queue_overflow` |
| `abandoned` | `true`（不再自动重试，需 `POST /kg-v3/worker/requeue` 手动捞） |
| `attempts` / `max_attempts` | `0` / `3`（`RETRY_LIMIT+1`） |
| `queue_overflow.reason` | `enqueue_overflow` / `requeue_overflow` |
| `queue_overflow.count` | 同一行被丢次数（重复入队又被挤掉会累加） |
| `queue_overflow.dropped_at` / `max_queue` / `project_id` / `segment_no` | 丢弃时刻 / 当时队列上限 / 项目 / 段号 |

查询方式（两条都写进了 docstring）：

- API：`GET /kg-v3/worker/status` → `summary.overflow`（计数）+ `recent_runs[].queue_overflow`（逐段明细）；`summary.abandoned` 同时可见。
- SQL：`SELECT id, note FROM agent_runs WHERE json_extract(note,'$.error_kind')='queue_overflow'`。

实证（临时库 `MAX_QUEUE=2`，连续入队 4 段）：

```
run 1 段 1: status=failed error_kind=queue_overflow
            queue_overflow={'reason':'enqueue_overflow','count':1,'dropped_at':...,'max_queue':2,'project_id':1,'segment_no':1}
run 2 段 2: 同上
run 3/4: status=running（仍在队列里）
worker_status.summary = {'success':0,'failed':2,'running':2,'stuck':0,'empty':0,'abandoned':2,'overflow':2,'claimed':2}
SQL 命中行 = [1, 2]
```

边界：`_QueueItem.run_id is None` 的历史调用（不经 `enqueue_segment`）仍然只有日志——库里没有可写的行；正常链路（`enqueue_segment` / `requeue_failed`）都带 `run_id`。

### L.4 chunk_id：判定 = 保留现状（命中停止条件，未改表）

只读取证：

| 检查 | 结果 |
|---|---|
| 两库含 chunk 的列 | `novel_dev` 只有 `distill_chunks.chunk_key`（蒸馏检索，与本链路无关）；`generated` 一张表都没有 |
| `kg_mentions` 列 | `id, project_id, entity_id, generation_id, segment_no, context_window, mention_kind, novel_id` —— 无 `chunk_id` |
| `novel_chunks` 结构 | `id, novel_id, chapter_no, idx, text, embedding`；novel 5 = **5094 块 / 925 章**，章均 5.5 块（第 1–3 章 3/3/4 块） |
| `generations` 定位信息 | `chapter_range` 的值是 `段1` / `段2` / `段3`，不是章号；表上也没有 chunk 外键 |

结论：`novel_chunks → chunk → mention` 这条链**现在不存在，也补不出唯一映射**：

1. `novel_chunks` 是参考语料的检索切块，一章 3–4 块；一个生成段可能对多章、多块，`chunk_id` 不是「一个提及一个值」。
2. `generations` 连章号都没记（`chapter_range` 是「段N」文本），无法 join 到 `novel_chunks`。
3. 要补，就必须 `ALTER TABLE kg_mentions ADD COLUMN chunk_id`（改表），或新建 mention × chunk 关联表并重建 61 条提及（改表 + 重建）。

按停止条件：**先报，停下**。没有 ALTER，没有重建；提及仍是 **61 条**（项目 1：26 / 项目 5：35；`novel_id` NULL 32 条），34→61 的回填原样保留。将来若要做，正解是「`generations` 先记章号 + mention→chunk 多对多」，属独立任务。

### L.5 失败可见性：状态值与重试策略

状态机不动，仍只有 `success / failed / running`；分类与计数全在 note：

| `error_kind` | 触发 | status | attempts | abandoned |
|---|---|---|---|---|
| `retry_exhausted` | 同一批连续抛错，用满 3 次 | `failed` | `3/3` | `true` |
| `provider_unavailable` | provider 未配置 / 不可用（不重试） | `failed` | `0/3` | `true` |
| `projection_failed` | 抽取成功但持有投影抛错 | `failed` | `1~3/3` | `true` |
| `queue_overflow` | 队列满被丢（见 L.3） | `failed` | `0/3` | `true` |
| `duplicate_running_row` | 同一段存在多条 running 行 | `failed` | — | — |

- 成功路径也写 `attempts / max_attempts / abandoned`，投影失败时 `abandoned=true`——「抽取成功但投影错」不会被当成全绿。
- 重试上限：`RETRY_LIMIT=2` → 总尝试 3 次（首次 + 2 次重试），每轮 `attempts_made += 1`。
- 放弃后状态：`status=failed` + `abandoned=true` + `error_kind=retry_exhausted`，不再自动重试；人工 `POST /kg-v3/worker/requeue` 把它重置为 `running`、`attempts=0`、`abandoned=false`，旧错误留在 `last_error`。
- 可查：`GET /kg-v3/worker/status` 的 `recent_runs[]`（`error / error_kind / attempts / max_attempts / abandoned`）与 `summary` 的 `failed / abandoned / overflow / claimed`。

实证：

```
run 5: status=failed error_kind=retry_exhausted attempts=3/3 abandoned=True error='upstream 500'
worker_status.summary = {'success':0,'failed':3,'running':2,'stuck':0,'empty':0,'abandoned':3,'overflow':2,'claimed':2}
手动重捞后：1 行 requeued → status=running attempts=0 abandoned=False last_error='upstream 500'
```

### L.6 单进程行为不变

| 项 | 复核 |
|---|---|
| 5 个 run | §K 真跑写入的 5 行（id 82–86，段 1–5）仍全部 `success`，`batch_stats.segments=5` |
| 投影链路 | 生成库四表 55 / 136 / 62 / 6，与 §K.7 完全一致（本节未执行任何投影） |
| 幂等重跑 | §K.5 的回放结论不受影响：`holding_projection.py` / `generated_store.py` 一行未改 |
| 真实库写入 | `novel_dev.db` 与 `data/generated.db` 的 mtime 都停在 03:11:38；本节验收全部使用临时库 |

### L.7 守卫数字（只读复核）

| 守卫 | 结果 |
|---|---|
| gid45 覆盖 | **125**（黑金长刀 47 / 太平刀 76 / 暗金色长刀 4 / 黑金刀 2 / 刀胚 1，去重章并集） |
| 六探针 | 6 个名字全部 `status=held`、`gid=45`、`via=surface`、`lines=2`（前 5 个别名都解析到规范名「黑金长刀」，`lines_md5` 同为 `dddd45b4`）；`镇国剑` → `gid=100`、`lines_md5=12fce7d7` |
| 持有行 | 4 个组共 11 条持有段，`current_item_holdings` 逐条可解析 **11/11 ok**（`bad=[]`） |
| p1/p5 指纹 | 与 `backend/backups/novel_dev-full-novel-id-20261111-002928.db` 逐字一致：p1seg1 `c892a57c…`、p1seg2 `df488a3b…`、p5seg1 `e3e00c9e…`（md5 覆盖 `chapter_range + \0 + text`） |
| 提及 | 61 条（项目 1：26 / 项目 5：35），`novel_id` NULL 32 条未动（第五项） |
| 生成库四表 | 55 / 136 / 62 / 6 |

> 口径说明：`kg_item_surface.evidence_chapter` 的 `DISTINCT` 只有 1，不是覆盖率口径；125 是「gid45 的 approved 写法在 novel 5 正文里的去重章并集」，与 §K.7 同口径。

### L.8 测试

| 范围 | 命令 | 结果 |
|---|---|---|
| 项目根全量 | `pytest -q`（`testpaths = tests`） | **788 项：782 passed / 2 failed / 2 xfailed / 2 skipped**，1028.5 s |
| 本轮新增 | `pytest tests/test_batch_worker_claim.py` | **5 passed** |
| KG / 持有 / 跨书 / 书维度回归 | 8 个文件 | **77 passed**（13.59 s） |
| NULL 泛称 + 组读侧 | `test_item_generic / test_item_group_read / test_item_group_cross_novel` | **39 passed**（4.62 s） |

2 个 failed 与 §K.7 是同一对，且与本节改动无交集（两个测试文件都不在 `git status` 的修改列表里，被测代码属 style / distill 路径；本节只改 `app/kg/batch_worker.py` 并新增一个测试文件）：

- `tests/test_distill_fixes.py::test_generation_path_extraction_still_commits_project_cache`（`assert (None)`）
- `tests/test_distill_jobs.py::test_api_shape_matches_frontend_distill_job_contract`（多出 `health / health_note / progress_at / elapsed_minutes / expected_minutes` 五个键）

脚本式文件仍按既有方式走 [test_legacy_script_suite.py](tests/test_legacy_script_suite.py) 子进程门禁（86 项，2 xfail），未改任何脚本；新增的 `test_batch_worker_claim.py` 被门禁当作脚本收进 1 项（脚本方式下无断言、exit 0），它的 5 个真用例在 backend 里单独跑。`backend/tests` 不能整体 `pytest tests`：目录里既有脚本式文件在收集期 `sys.exit()`，会 `INTERNALERROR SystemExit: 0`（pytest.ini 注释已写明老式脚本不进 pytest 收集），所以新式用例按组跑。

### L.9 停止条件核对与未做的事

- 第四项：**无外部依赖**。只用了 stdlib（`os / secrets / socket / threading / time / ctypes`）+ 既有 SQLAlchemy / SQLite，没有 redis、没有文件锁。
- 第六项 chunk_id：**命中停止条件**（必须 `ALTER TABLE` 才能加列，且加了也无唯一值可填）→ 已停，未改表、未重建提及。
- 第五项（22 条 NULL 归属）：**未碰**。
- `:8000` 仍是 PID 2812（启动 03:22:23），而 `batch_worker.py` mtime = 10:19:29 → **进程里是旧代码**。本节刻意没有重启：启动会执行 `recover_running` 并向真实库写 `kg.worker.<id>` 心跳，属真实库写入；按要求「写数据前先备份」，且第五项要等代码定稿后单独处理，重启留作独立动作（备份 → 同参数同端口重启 → 校验 `started_at ≥ mtime` 且 `/openapi.json` 200）。
- 临时验收脚本（`backend/.tmp_accept/`）与 pytest 中间产物（`.pytest_full_k4_k6.xml` 等）已删除。


## M. 提及 generation_id / segment_no 定位审计（停止条件命中，未修）

### M.1 结论先行

- **你给的两个错配例子不成立**。当前库里 `generations.id=2` 的 `segment_no=1`、`chapter_range='段1'`，它就是 p1 段 1 自己的生成行；`id=5` 是 **p1 段 3**（不是「项目 5 的段 2」）。真实的 generations 映射是：

| generation id | project | segment_no | chapter_range | 字数 |
|---|---|---|---|---|
| 1 | 5 | 1 | 段1 | 1413 |
| 2 | 1 | 1 | 段1 | 1036 |
| 3 | 1 | 2 | 段2 | 1341 |
| 4 | 5 | 2 | 段2 | 2273 |
| 5 | 1 | 3 | 段3 | 1109 |

  id 是全库自增、按写入时间交错，不按项目连续；「id 2 = 段 2」的读法把 id 当成了项目内序号。
- **61 条定位结果：正确 34 / 需改 0 / 定位失败 27（44.3%）**。按你的停止条件（定位失败 > 三成）**已停，未改代码、未写库**。
- 27 条定位失败**全部在项目 5**，且 `context_window` 是《大奉打更人》原文（许七安 / 税银 / 京兆府），在**任何一条** `generations.text` 里都不存在（全文与去空白两种口径都试过）。其中：
  - **10 条**（id 35–44）`generation_id` 指向 gen1 / gen4——那是「同项目、同 segment_no」的 generation，但正文不是同一份文本；
  - **17 条**（id 45–61）`generation_id IS NULL`，落在段 3/4/5，而 p5 从来只有 2 条 generation。
- 因为这 27 条**没有一条存在唯一的正确 generation 目标**，按「定位失败保持原样」的规则，**本次没有可执行的数据修复（需改=0）**；17 条 NULL 同时属于第五项，按约定不碰。
- 证据链：`kg_entities` id 70–82（大奉京兆府 / 许七安 / 许平志 / 许新年 / 税银 …）全部 `novel_id=5`，对应 §K.5 的验收记录「新增实体 id 70–82、事件段号 1–5 各 1 条」——那轮验收是把大奉语料按 p5 的段号 1–5 入队跑的，这批 mention 就是那次跑出来的。

### M.2 逐条定位表 · 项目 1（26 条，全部正确）

| mention | 原 gen/seg | 定位 gen/seg | 依据（命中位置） | 结论 |
|---|---|---|---|---|
| 1 | 2 / 1 | 2 / 1 | gen2 text[66:128] 完整命中（62 字） | 正确 |
| 2 | 2 / 1 | 2 / 1 | gen2 text[22:85] 完整命中（63 字） | 正确 |
| 3 | 2 / 1 | 2 / 1 | gen2 text[108:170] 完整命中（62 字） | 正确 |
| 4 | 2 / 1 | 2 / 1 | gen2 text[562:625] 完整命中（63 字） | 正确 |
| 5 | 2 / 1 | 2 / 1 | gen2 text[56:118] 完整命中（62 字） | 正确 |
| 6 | 2 / 1 | 2 / 1 | gen2 text[180:243] 完整命中（63 字） | 正确 |
| 7 | 2 / 1 | 2 / 1 | gen2 text[322:383] 完整命中（61 字） | 正确 |
| 8 | 3 / 2 | 3 / 2 | gen3 text[0:32] 完整命中（32 字） | 正确 |
| 9 | 3 / 2 | 3 / 2 | gen3 text[308:370] 完整命中（62 字） | 正确 |
| 10 | 3 / 2 | 3 / 2 | gen3 text[0:36] 完整命中（36 字） | 正确 |
| 11 | 3 / 2 | 3 / 2 | gen3 text[727:790] 完整命中（63 字） | 正确 |
| 12 | 3 / 2 | 3 / 2 | gen3 text[1004:1067] 完整命中（63 字） | 正确 |
| 13 | 3 / 2 | 3 / 2 | gen3 text[78:140] 完整命中（62 字） | 正确 |
| 14 | 3 / 2 | 3 / 2 | gen3 text[20:83] 完整命中（63 字） | 正确 |
| 15 | 3 / 2 | 3 / 2 | gen3 text[549:610] 完整命中（61 字） | 正确 |
| 16 | 3 / 2 | 3 / 2 | gen3 text[91:153] 完整命中（62 字） | 正确 |
| 17 | 3 / 2 | 3 / 2 | gen3 text[897:959] 完整命中（62 字） | 正确 |
| 18 | 3 / 2 | 3 / 2 | gen3 text[282:344] 完整命中（62 字） | 正确 |
| 19 | 3 / 2 | 3 / 2 | gen3 text[304:366] 完整命中（62 字） | 正确 |
| 28 | 5 / 3 | 5 / 3 | gen5 text[0:44] 完整命中（44 字） | 正确 |
| 29 | 5 / 3 | 5 / 3 | gen5 text[27:90] 完整命中（63 字） | 正确 |
| 30 | 5 / 3 | 5 / 3 | gen5 text[342:404] 完整命中（62 字） | 正确 |
| 31 | 5 / 3 | 5 / 3 | gen5 text[100:162] 完整命中（62 字） | 正确 |
| 32 | 5 / 3 | 5 / 3 | gen5 text[216:279] 完整命中（63 字） | 正确 |
| 33 | 5 / 3 | 5 / 3 | gen5 text[505:568] 完整命中（63 字） | 正确 |
| 34 | 5 / 3 | 5 / 3 | gen5 text[1028:1090] 完整命中（62 字） | 正确 |

### M.3 逐条定位表 · 项目 5（35 条：8 正确 / 27 定位失败）

| mention | 原 gen/seg | 定位 gen/seg | 依据（命中位置 / 无命中说明） | 结论 |
|---|---|---|---|---|
| 20 | 1 / 1 | 1 / 1 | gen1 text[0:32] 完整命中（32 字） | 正确 |
| 21 | 1 / 1 | 1 / 1 | gen1 text[5:69] 完整命中（64 字） | 正确 |
| 22 | 1 / 1 | 1 / 1 | gen1 text[563:627] 完整命中（64 字） | 正确 |
| 23 | 1 / 1 | 1 / 1 | gen1 text[687:749] 完整命中（62 字） | 正确 |
| 24 | 4 / 2 | 4 / 2 | gen4 text[0:32] 完整命中（32 字） | 正确 |
| 25 | 4 / 2 | 4 / 2 | gen4 text[31:95] 完整命中（64 字） | 正确 |
| 26 | 4 / 2 | 4 / 2 | gen4 text[36:100] 完整命中（64 字） | 正确 |
| 27 | 4 / 2 | 4 / 2 | gen4 text[251:313] 完整命中（62 字） | 正确 |
| 35 | 1 / 1 | — / — | 5 条 generations 全文/去空白均无命中；窗口开头「大奉京兆府，监牢。」 | 定位失败 |
| 36 | 1 / 1 | — / — | 同上；窗口开头「大奉京兆府，监牢。」 | 定位失败 |
| 37 | 1 / 1 | — / — | 同上；窗口开头「五万两白银。朝野震动…」 | 定位失败 |
| 38 | 1 / 1 | — / — | 同上；窗口开头「三天前，那位在御刀卫当差…」 | 定位失败 |
| 39 | 1 / 1 | — / — | 同上；窗口开头「并快速流动。许七安，字宁宴…」 | 定位失败 |
| 40 | 1 / 1 | — / — | 同上；窗口开头「作为许平志的亲侄儿…」 | 定位失败 |
| 41 | 4 / 2 | — / — | 同上；窗口开头「中度过下半辈子。『开局就是地狱模式啊....』」（`....` 是原文，不是窗口截断） | 定位失败 |
| 42 | 4 / 2 | — / — | 同上；窗口开头「许家并非大族，族人分散各地…」 | 定位失败 |
| 43 | 4 / 2 | — / — | 同上；窗口开头「职务，打入京兆府大牢。两天！」 | 定位失败 |
| 44 | 4 / 2 | — / — | 同上；窗口开头「五万两的税银被劫…」（`....` 是原文） | 定位失败 |
| 45 | NULL / 3 | — / — | 同上；窗口开头「脚步声。一名狱卒领着…」 | 定位失败 |
| 46 | NULL / 3 | — / — | 同上；窗口开头「许七安脑海里浮现此人的相关记忆…」 | 定位失败 |
| 47 | NULL / 3 | — / — | 同上；窗口开头「许新年顿住，沉默的看着他。」 | 定位失败 |
| 48 | NULL / 3 | — / — | 同上；窗口开头「坊司，受到凌辱。」 | 定位失败 |
| 49 | NULL / 4 | — / — | 同上；窗口开头「年皱了皱眉：『你要这个干嘛。』」（`....` 是原文） | 定位失败 |
| 50 | NULL / 4 | — / — | 同上；窗口开头「过是这样的：【三天前的卯时二刻…」 | 定位失败 |
| 51 | NULL / 4 | — / — | 同上；窗口开头「许新年皱了皱眉：『你要这个干嘛。』」（`....` 是原文） | 定位失败 |
| 52 | NULL / 4 | — / — | 同上；窗口开头「人敢出手帮助，求告无门的无奈…」 | 定位失败 |
| 53 | NULL / 4 | — / — | 同上；窗口开头「时二刻（早晨六点半），许平志押运…」 | 定位失败 |
| 54 | NULL / 4 | — / — | 同上；窗口开头「行至广南街，刚过桥，忽然掀起…」 | 定位失败 |
| 55 | NULL / 5 | — / — | 同上；窗口开头「人供词、参与押送士卒的供词。」 | 定位失败 |
| 56 | NULL / 5 | — / — | 同上；窗口开头「过是这样的：【三天前的卯时二刻…」 | 定位失败 |
| 57 | NULL / 5 | — / — | 同上；窗口开头「道。税银失踪案的经过是这样的…」 | 定位失败 |
| 58 | NULL / 5 | — / — | 同上；窗口开头「时二刻（早晨六点半），许平志押运…」 | 定位失败 |
| 59 | NULL / 5 | — / — | 同上；窗口开头「行至广南街，刚过桥，忽然掀起…」 | 定位失败 |
| 60 | NULL / 5 | — / — | 同上；窗口开头「她手里握着一根甘蔗，腰间挂着…」 | 定位失败 |
| 61 | NULL / 5 | — / — | 同上；窗口开头「如凝脂，顾盼生辉。她手里握着…」 | 定位失败 |

### M.4 截断 / 省略号情况

- `context_window` 由 `_window_around`（[batch_worker.py:1862](backend/app/kg/batch_worker.py#L1862-L1869)）取命中词前后各 30 字，**只做首尾裁剪、不加省略号**。短窗口（32–44 字）都是因为命中点在段首被裁了前半，属正常窗口，不影响整段子串定位。
- 含 `....` 的 **4 条**（id 41、44、49、51）是原文自带的省略号（「开局就是地狱模式啊....」「我要破案....」），不是我方截断；且它们不是定位失败的原因——失败原因是整段文本在 generations 里不存在。
- 没有发现「窗口被中间截断」或「加了 `…` 导致定位不可靠」的条目。

### M.5 写侧根因（只读结论，未改代码）

**一句话**：`_seg_to_gen` 只按 `(project_id, segment_no)` 查 generation，写侧从不校验「入队正文是否就是该 generation 的正文」——段号撞上就绑定，撞不上就 NULL。

- 正常生成链是安全的：`_write_generation` 先 `Generation(segment_no=N, text=text)`（[engine.py:484-494](backend/app/generation/engine.py#L484-L494)），随后用**同一个 `text` 和同一个 `segment_no`** 调 `_enqueue_v3_extraction`（[engine.py:1617-1622](backend/app/generation/engine.py#L1617-L1622)），`enqueue_segment` 的 commit 会把这条 generation 一起落库。
- 写侧映射 [batch_worker.py:1841-1849](backend/app/kg/batch_worker.py#L1841-L1849) 只做 `{g.segment_no: g.id}`；两处提及写入 `:1475` / `:1625` 都只读这个映射。既没有正文一致性校验，也没有「同项目同 segment_no 多行 generation」的冲突检测（dict 后写覆盖）。
- 所以这 10 条**不是**「批次里取了别的段的 generation」，也**不是**「没传、拿了默认值」——是按段号显式查到了同项目同段号的那条 generation，但入队正文根本不是该段正文。历史上 §K 验收把大奉语料按 p5 段号 1–5 入队，就是这个入口。
- 备份交叉验证：从 `novel_dev-20261110-163128.db` 到 `novel_dev-full-active-novel-20261111-010055.db`，p5 的 generation 一直是 1413/2273 字的小杨正文，**从未**出现大奉文本；这 27 条窗口在任何备份的任何 generation 里都 0 命中。结论是「提及的正文从来不是 generation 正文」，不是「正文被改过导致失配」。

### M.6 未执行项与守卫

| 项 | 状态 |
|---|---|
| 历史数据修复 | **未做**。需改=0；定位失败按规则保持原样（其中 17 条 `generation_id IS NULL` 属第五项，未碰） |
| 写侧修改 | **未做**。命中停止条件，按「先报，别修」停下 |
| 新抽取实测 | **未做**。会向真实库写入 AgentRun / mention，且依赖 LLM；应在你批准退出停止状态后再跑 |
| schema / chunk_id / :8000 | 均未动；`:8000` 仍是 PID 2812 跑旧代码 |
| 真实库写入 | **零写入**：`novel_dev.db` mtime 仍为 `2026-11-11 03:11:38`；`kg_mentions` 61 行、`generations` 5 行未变 |
| p1/p5 指纹 | 与 `backend/backups/novel_dev-full-novel-id-20261111-002928.db` 逐字一致：p1seg1 `c892a57c…`、p1seg2 `df488a3b…`、p1seg3 `66d2c025…`、p5seg1 `e3e00c9e…`、p5seg2 `2c4f86d2…` |
| 测试 | 未改代码，未重跑；两个既有 distill 失败同 §L.8，与本节无交集 |

### M.7 需要你决定

1. **接受现状结案**（推荐）：0 条可修，27 条定位失败已如实列出；p1 的「错配」前提本身不成立。
2. **清掉这 27 条验收污染**：它们是大奉语料按 p5 段号入队的产物，可以删除或把非 NULL 的 10 条 `generation_id` 置 NULL——但这属于第五项/数据清理，需要你单独批准，且要先备份。
3. **只补写侧防线**（不改 schema、不动历史数据）：在 `_seg_to_gen` 里要求 `generation.text == item.text` 且同段唯一，否则 `generation_id=None` + 计数日志，保证「以后新抽取绝不挂到错的 generation 上」。这条需要你批准后我再改并实测一段。


## N. 提及绑定防线 + 历史污染清理（3 + 2）

### N.1 写侧防线

改动只在 [batch_worker.py](backend/app/kg/batch_worker.py)，不动 schema：

| 位置 | 改动 |
|---|---|
| [`_seg_to_gen` L1854](backend/app/kg/batch_worker.py#L1854-L1920) | 返回 `(mapping, report)`；绑定前逐字比对 `item.text == generation.text`；同段多条 generation 不绑；三类原因分别计数 |
| [`_persist_extraction` L1233](backend/app/kg/batch_worker.py#L1233) | `stats.mention_binding = report`，成功路径随 `batch_stats` 进 `AgentRun.note` |
| [降级路径 L367-380](backend/app/kg/batch_worker.py#L367-L380)、[失败路径 L463-477](backend/app/kg/batch_worker.py#L463-L477) | `note.mention_binding` 与既有 `error_kind / attempts / max_attempts / abandoned` 并列 |

绑定报告结构（可查，无需新表）：

```json
{"bound": 1,
 "unbound": {"text_mismatch": 0, "multi_candidate": 0, "no_generation": 0},
 "segments": [{"segment_no": 1, "generation_id": 2, "status": "bound"}]}
```

规则：正文逐字相同且同段唯一 → `bound`；无 generation → `no_generation`；同段多条 → `multi_candidate`；唯一但正文不同 → `text_mismatch`。后三种一律 `generation_id=NULL`，不会挂到任何 generation 上。

### N.2 副本实测（真实库副本，非生产）

用 `sqlite3.backup()` 把生产库复制到 `backend/.tmp_verify/verify.db`（临时副本，验证后已删除），`DATABASE_URL` 指向副本后跑：

| 场景 | 输入 | 结果 |
|---|---|---|
| A 正常路径 | p1 段 1，正文 = `generations.id=2` 全文 | 新提及 `generation_id=2`、`segment_no=1`；`report.bound=1` |
| B 段号撞车 | p5 段 1，正文「许七安握着神剑…」（与 gen1 正文不同） | 提及照落，`generation_id=NULL`；`report.unbound.text_mismatch=1`，`generation_ids=[1]` |
| C 留痕 | p1 段 1 入队一段无关正文，`provider=None` 走降级路径 | `AgentRun id=87`（副本）`status=failed`；`note.mention_binding` = `{"bound":0,"unbound":{"text_mismatch":1,...},"segments":[{"segment_no":1,"status":"unbound","reason":"text_mismatch","generation_ids":[2]}]}` |

结论：正常生成链（同一份 `text` 同段号）仍然绑得上；段号撞车但正文不同的一律留 NULL 且计数 +1。

### N.3 历史污染清理

**引用检查**（31 张表逐表扫列名）：除 `kg_mentions` 自身外，唯一含 `mention` 的列是 `kg_entities.mention_count`——它是派生计数，不是 id 引用。`kg_relations.from_id/to_id` 指向实体、`kg_events.subject_id/object_id/location_id` 指向实体、`evidence_segment_ids` 为 `"[]"`、`evidence.ref_id` 是 run/chunk/summary 级别；没有任何行按 id 引用这 27 条提及。

| 项 | 内容 |
|---|---|
| 整库备份 | `backend/backups/novel_dev-pre-mention-cleanup-20261111-122635.db`（`wal_checkpoint(TRUNCATE)` 后 `backup()`） |
| 快照 | `backend/backups/kg_mentions_cleanup_27_20261111-122635.json`（27 行、全部列，支持回滚插入） |
| 删除清单 | id **35–44**（10 条，`text_mismatch`：挂在 gen1/gen4 但正文不同）+ id **45–61**（17 条，`no_generation`） |
| 删除前 | `kg_mentions=61`（项目 1：26 / 项目 5：35） |
| 删除后 | `kg_mentions=34`（项目 1：26 / 项目 5：8，剩余 id 20–27） |
| 逐表行数变化 | 只有 `kg_mentions 61→34`；`kg_entities` 仍 77 行（值变、行数不变）；其余表行数不变 |
| p1 完好性 | 删除前后 26 条 digest 均为 `4ffc10f88130a0a3df3a36a24733bffc`（`id\0context_window\0` 串联），`context_window` 逐字一致 |
| 派生计数 | 受影响 13 个实体 `mention_count` 同事务重算：70/71/72/73/74/76/77/78/79/81/82 分别 1/5/3/2/5/1/2/2/2/1/1 → 全部 0；41/45 保持 0 |
| 遗留 NULL | `novel_id IS NULL` 仍 32 条（26 p1 + 6 p5），第五项未碰；`generation_id IS NULL` 现为 0 |

> `mention_count` 是唯一被这次删除影响的非目标字段：它不是 id 引用，但不重算就会停在旧值（许七安 5 条提及实际已 0）。因此与删除放同一事务重算，并在此明示。

### N.4 守卫

| 守卫 | 结果 |
|---|---|
| gid45 覆盖 | **125**（黑金长刀 47 / 太平刀 76 / 暗金色长刀 4 / 黑金刀 2 / 刀胚 1，去重章并集） |
| 六探针 | 刀胚/太平刀/暗金色长刀/黑金刀/黑金长刀 → `held / gid45 / via=surface / lines=2 / md5 617c601c`；镇国剑 → `held / gid100 / via=surface / lines=2 / md5 24e7c486`。清理前备份 vs 清理后生产**逐组相同** |
| 持有行 | 4 组（gid 5/20/33/45）共 **11 条**，`after_resolvable` **11/11 true**，`bad=[]` |
| p1/p5 指纹 | 与 002928 备份逐字一致：p1seg1 `c892a57c`、p1seg2 `df488a3b`、p1seg3 `66d2c025`、p5seg1 `e3e00c9e`、p5seg2 `2c4f86d2` |
| 生成库 | `data/generated.db` mtime 仍 `2026-11-11 03:11:38`，sha256 `0a6b0025…`（本轮零写入） |

> md5 口径说明：§L 记的 `dddd45b4`/`12fce7d7` 与本节 `617c601c`/`24e7c486` 是同一组 lines 的不同序列化算法（§L 当次脚本已删）。不变量（status/gid/via/lines 数）完全一致，且本节用同一算法对比了清理前备份与清理后生产，**逐组相同**；`617c601c`/`24e7c486` 也与 §K.7 记录一致。

### N.5 回滚实测（副本）

- 在清理后生产库的临时副本（`backend/.tmp_verify/rollback2.db`，验证后已删除）执行 `rollback --db … --snapshot …`：插入 **27** 行，总数回到 **61**，27 个 id 全部回来。
- 逐列对比快照：**27/27 完全一致**，`bad=[]`；受影响实体 `mention_count` 也恢复到 1/5/3/2/5/1/2/2/2/1/1（41/45 恢复为 1）。
- 生产库未执行回滚，快照与备份保留在 `backend/backups/`。

### N.6 测试

- 全量（项目根 `pytest -q`）：**788 项 → 786 passed / 2 failed / 2 xfailed / 2 skipped**（exit 1）。
- 2 个 failed 是既有 distill 失败，与本次改动无交集：
  - `tests/test_distill_fixes.py::test_generation_path_extraction_still_commits_project_cache`（`assert (None)`）
  - `tests/test_distill_jobs.py::test_api_shape_matches_frontend_distill_job_contract`（多出 `health / health_note / progress_at / elapsed_minutes / expected_minutes` 五个键）
- 本轮新增 `backend/tests/test_kg_mentions_alias.py` 6 个用例全过；`backend/tests/test_book_scope_wiring.py` **12 passed**。

### N.7 范围与未做

- 未改 schema；未碰 `chunk_id`；第五项 `novel_id IS NULL` 未碰。
- 3+2 交付完成时 `:8000` 仍是旧进程（PID 2812），用户批准后的重启与验收见 §N.8，误停事故见 §N.9。
- 本轮真实库写入仅两处：删除 27 条 `kg_mentions`、重算 13 个 `kg_entities.mention_count`；写入前已整库备份。

### N.8 重启 `:8000`（用户批准后执行）

启动方式：`backend\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`，cwd = `backend/`，不加 `--reload`（与 `start-dev.bat` 同一解释器：Python 3.13.14 / uvicorn 0.30.6）。

| 项 | 值 |
|---|---|
| 改动文件最大 mtime | `app/routers/assistant_wizard.py` = **2026-11-11 12:26:46.902**（其次 `wizard_flow.py` 12:26:09.708、`tests/test_kg_mentions_alias.py` 12:21:57、`app/kg/batch_worker.py` 12:21:37.049） |
| 进程启动时间 | **2026-11-11 12:50:01.347**（PID 29976） |
| 判据 | 启动时间晚于最大 mtime **3 分 14 秒** → 进程加载的是当前盘上代码 |
| 监听 | 8000 只有 PID 29976 一个监听者；PID 6664 是 venv 启动壳，父子关系 `6664 → 29976`，不是第二个服务 |
| 接口 | `/openapi.json` **200**（111,909 B）；`/healthz` 返回 **404**——本项目没有这个端点，健康检查以 openapi 为准 |

启动副作用（`recover_running` / `reap_stale_runs`）：

- 启动前 `agent_runs` 中 `role='kg_batch_extract' AND status='running'` = **0 行** → 重入队 0、正文缺失 0、重复段 0、认领冲突 0（没有行可认领）；启动日志也没有「启动恢复 KG 批抽」告警行。
- `agent_runs` 总数 86 → 86（无新增/改写）；蒸馏收割 0 条。
- 写前备份仍在：`backend/backups/novel_dev-pre-mention-cleanup-20261111-122635.db` 与同名快照 JSON。

重启后基线（与清理后逐项一致）：

| 守卫 | 结果 |
|---|---|
| `kg_mentions` | **34**（p1 26 / p5 8；`generation_id IS NULL` 0；`novel_id IS NULL` 32） |
| gid45 覆盖 | **125**（黑金长刀 47 / 太平刀 76 / 暗金色长刀 4 / 黑金刀 2 / 刀胚 1） |
| 六探针 | 五个刀名 `held / gid45 / via=surface / lines=2 / md5 617c601c`；镇国剑 `held / gid100 / lines=2 / md5 24e7c486` |
| 11 条持有行 | **11/11** `after_resolvable=True`，`bad=[]` |
| p1/p5 指纹 | `c892a57c` / `df488a3b` / `66d2c025` / `e3e00c9e` / `2c4f86d2`，与 002928 备份一致 |
| 生成库 | `data/generated.db` mtime `2026-11-11 03:11:38`、sha256 `0a6b0025…`，重启未写 |

两条新代码的生效证据：

- **提及绑定防线**：进程启动晚于 `batch_worker.py` 12:21:37；盘上 [`_seg_to_gen` L1854-L1918](backend/app/kg/batch_worker.py#L1854-L1918) 已是「逐字比对 + 三类未绑定计数 + 返回 `(mapping, report)`」，`_persist_extraction` 把 report 写进 `stats.mention_binding`。下一次真实抽取即可在 `AgentRun.note.batch_stats.mention_binding` 看到计数字段（副本实测已在 `AgentRun 87` 的 note 里看到同结构，见 §N.2 场景 C）。本轮不触发真实抽取，避免向生产库写入。
- **引导新步骤 `book_scope`**：同 venv、同盘代码调用 `wizard_flow.step_order`：
  - 0 本 → 无 `book_scope`
  - 1 本 → 无 `book_scope`
  - 2 本 → `['branch','corpus','book_scope','distill','genre','style','length','chapters','outline','confirm']` → **在且仅在两本以上语料时出现**

### N.9 事故：本轮误停 `:8000`（如实记录）

| 项 | 内容 |
|---|---|
| 谁停的 | 我在本轮收尾时停的（不是用户、不是系统） |
| 对象 | 后台命令 `484f24b358ae4e79f153f75ab31e3717`（shell PID 41080，03:22:21 启动；其子进程即 uvicorn PID 2812，占用 8000） |
| 当时的判断依据 | 该后台命令长期无可见输出、处于 idle，我把它当成更早那次全量 pytest 的残留进程，用 `StopCommand` 清理。实际全量 pytest 是另一条命令 `b5b324d3a21bc8a8e6e7342cc6626e88`，`484f24b…` 才是服务 |
| 停机时长 | `execution.json.finishedAtMs` = 2026-11-11 **12:29:54.626** → 重启 **12:50:01.347**，**20 分 07 秒** |
| 影响面 | 旧进程 `output.log` 最后一条请求是 09:28:54，停机前近 3 小时无访问；停机期间没有请求被拒的记录 |
| 数据损失 | **无**。停前 0 条 running 批抽行、停后 `agent_runs` 86→86；内存队列为空；蒸馏无在跑任务；两个库文件均未受损（生成库 mtime/sha256 未变）。27 条清理（12:26:35–12:27:51）发生在停机之前且已提交完成 |
| 处置 | 按「不重启 `:8000`」的约束未擅自拉起，先报告并请用户决定；用户批准后用原解释器、原参数、原端口于 12:50:01 恢复 |
| 防再犯 | 停后台命令前先核 cwd 与命令行；不以「无输出」推断进程身份；长驻服务单独登记，不与测试命令混在同一批后台任务 |

---

## O. `/healthz`：让「进程是不是旧代码」自动可判

目标：把 §N.8 那种「手工比 mtime」固化成端点 + 一条命令的检查脚本，退出码可直接进发布流程。

### O.1 端点

`GET /healthz` → 200，实现在 [health.py](backend/app/health.py)，由 [main.py](backend/app/main.py#L113-L116) 注册。

| 字段 | 含义 |
|---|---|
| `ok` / `fresh` | `started_at >= build_time`；两者同值，`fresh` 是显式语义 |
| `pid` | 当前进程 PID，用来和端口监听者对号 |
| `git_sha` | `git rev-parse --short HEAD`；git 不可用时 `null` |
| `dirty` | `git status --porcelain` 非空 → `true`（含未跟踪文件） |
| `started_at` / `started_at_epoch` | 进程 import `app.health` 的时刻（uvicorn 启动即加载 `app.main`） |
| `build_time` / `build_time_epoch` | **请求到达时**实时扫描到的源码/配置最大 mtime |
| `build_time_file` | 命中最新的那个文件（相对 `backend/`） |
| `scope.roots` / `scope.excluded_dirs` | 扫描范围与排除项，随响应返回，避免口径只存在于文档 |

**为什么 `build_time` 在请求时扫、而不固化在启动那一刻**：固化值永远等于启动时的旧盘面，「起服务之后又改了代码」就永远判不出来——验收要求的 touch 往返也会失效。

扫描范围：`backend/app/**/*.py`、`backend/config/**/*.{yaml,yml,json}`、`backend/.env`。
排除：`__pycache__`、`venv`、`node_modules`、`backups`、`.tmp_verify`、`.pytest_cache`、`data`、`migrations`、`worktrees`。不排这些，一次测试写入、备份或迁移脚本就会把 `build_time` 永远顶到最新，检查恒为陈旧。

### O.2 检查脚本

[check_healthz.py](backend/check_healthz.py)，仅标准库：

```
python backend/check_healthz.py
python backend/check_healthz.py --url http://127.0.0.1:8000/healthz
```

退出码：`0` = 进程不旧于代码；`1` = 陈旧（代码比进程新）；`2` = 连不上或缺字段（无法判定，按失败处理）。

往返实测（同一个运行中的进程，PID 28984，2026-11-11 16:58:03 启动）：

| 步骤 | `build_time` | 最新文件 | 退出码 |
|---|---|---|---|
| 正常 | 16:29:11.055 | `app/routers/assistant_generation.py` | **0**（余量 1732.768s） |
| 新建 `app/__healthz_touch_probe__.py` | 16:58:33.469 | `app/__healthz_touch_probe__.py` | **1**（STALE 29.647s） |
| 删除探针 | 16:29:11.055 | `app/routers/assistant_generation.py` | **0**（余量 1732.768s） |

服务未启动时跑同一脚本：`FAIL 无法读取 http://127.0.0.1:8000/healthz: <urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>`，退出码 **2**（无法判定，按失败处理）。

13:04 那轮在 PID 12156 上做过同样的 0 → 1 → 0 往返（13:04:14 → 13:05:28 → 13:04:14），结论一致；本轮是重启后的复测。

### O.3 重启与监听者对照

| 时点 | 监听 8000 的 PID | 说明 |
|---|---|---|
| §N.8 验收后 | 29976 | 12:50:01 启动；13:03:32 最后一条日志后随宿主终端被回收退出 |
| 13:04 那轮 | 12156 | 13:04:31 启动（父会话按「好的，启动服务」拉起），已含新端点；13:05 完成脚本往返后停掉跑全量 |
| 跑全量测试期间 | 无 | 13:35–14:10 为让隔离守卫通过而停服，`listeners=0`、库文件稳定 |
| **本轮重启** | **28984** | 16:58:03 启动；重启前 `netstat` 确认 `listeners=0`；启动晚于源码最大 mtime 16:29:11 |

- 端口：重启后 `netstat -ano | findstr :8000` 只有一条 `LISTENING`，`OwningProcess = 28984`，与 `/healthz` 的 `pid` 一致；没有第二个监听者，也没有旧进程残留。
- `/healthz` 原始响应（HTTP 200，逐字）：

```json
{"ok":true,"fresh":true,"pid":28984,"git_sha":"7af5a80","dirty":true,"started_at":"2026-11-11T16:58:03.822+08:00","started_at_epoch":1794387483.8227396,"build_time":"2026-11-11T16:29:11.055+08:00","build_time_epoch":1794385751.0550406,"build_time_file":"app/routers/assistant_generation.py","scope":{"roots":["backend/app/**/*.py","backend/config/**/*.{yaml,yml,json}","backend/.env"],"excluded_dirs":[".pytest_cache",".tmp_verify","__pycache__","backups","data","migrations","node_modules","venv","worktrees"]}}
```

- `recover_running`：启动日志只有 `Started server process [28984] / Waiting for application startup / Application startup complete / Uvicorn running`，没有「启动恢复 KG 批抽」告警；启动后 `agent_runs` 中 `status='running'` 按角色分组为空（`kg_batch_extract + running` = **0 行**）→ 捞到 **0 行**、重入队 0、正文缺失 0。
- 另一条线在 16:16–16:29 改的 5 个文件（`wizard_chat.py` 16:16:38、`generation/engine.py` 16:18:09、`generation/prompts.py` 16:18:20、`routers/assistant_generation.py` 16:29:11；`routers/assistant_wizard.py` 13:41:27）全部早于 `started_at` 16:58:03，进程加载的就是盘上这一版：
  - 段数步不再吞改写指令：[wizard_chat.py](backend/app/wizard_chat.py#L134-L136) 的改写动词清单（`改写|重写|重新写|修改|…`）+ [control_intent](backend/app/wizard_chat.py#L284-L300)。
  - 重写指令注入：[engine.py](backend/app/generation/engine.py#L1101-L1113) 的 `_rewrite_prompt`（注释即「注入只作用于被丢弃的初稿」那处修复）。
  - 只核对了盘上代码状态与进程启动时间的先后，没有真跑生成（按本轮要求）。

### O.4 边界：mtime 判据不是通用的

`build_time` 依赖**本机文件系统 mtime**，以下场景会失效，发布流程不能直接照搬：

- **多机部署**：每台机器的代码 mtime 不同，`build_time` 只反映被请求的那台机器；
- **镜像构建**：容器内文件的 mtime 是**构建时间**，不是部署时间，`started_at` 永远晚于它 → 恒判新鲜，属于假阴性；
- **CI 用 `git archive`/浅克隆**：可能不带原始 mtime，扫描结果没有意义。

这类场景应改成：构建时写入 build id / 镜像 digest，进程启动时读取并在 `/healthz` 返回，再与「部署目标版本」比对；mtime 方案只适用于「本机开发/单机部署，改了文件就要重启」的场景。

### O.5 守卫（停服跑测前 / 16:58 重启后各一次，逐项相同）

| 守卫 | 结果 |
|---|---|
| `kg_mentions` | **34**（p1 26 / p5 8；`generation_id IS NULL` 0；`novel_id IS NULL` 32） |
| gid45 覆盖 | **125**（黑金长刀 47 / 太平刀 76 / 暗金色长刀 4 / 黑金刀 2 / 刀胚 1） |
| 六探针 | 五个刀名 `held / gid45 / via=surface / lines=2 / md5 617c601c`；镇国剑 `held / gid100 / lines=2 / md5 24e7c486` |
| 11 条持有行 | **11/11** `after_resolvable=True`，`bad=[]` |
| p1/p5 指纹 | `c892a57c` / `df488a3b` / `66d2c025` / `e3e00c9e` / `2c4f86d2`，与 002928 备份一致 |
| 生成库 | `data/generated.db` mtime `2026-11-11 03:11:38`、sha256 `0a6b0025…`，未写 |

重启后（17:00 左右）用同一脚本复跑：`kg_mentions` 34（1→26 / 5→8）、p5 剩余 id `[20…27]` 且 27 条污染 0 残留、gid45 去重覆盖 125、六探针 md5 `617c601c` / `24e7c486`、11 条持有行 `after_resolvable=True`、`bad=[]`、五组 p1/p5 指纹逐条「一致」、生成库 mtime 与 sha256 未变。服务在线期间的轮询没有改动这些表（写的是 `app_settings`，见 O.6）。

### O.6 测试

全量（服务停止，项目根 `pytest -q`，`testpaths = tests`）：**789 项 → 783 passed / 2 failed / 0 errors / 4 skipped**，866.7 s（13:56:03 → 14:10:30，`backend/.tmp_verify/junit_full_4.xml`，exit 1）。

| 项 | 结果 |
|---|---|
| 跳过 4 | 2 xfail（`test_legacy_script_suite` 的 `test_v30_integration`、`concurrent_assistant_qa`）+ 2 skip（`test_reasoning_wiring` 库里没有 project 5） |
| 失败 2 | `test_distill_fixes.py::test_generation_path_extraction_still_commits_project_cache`（`assert (None)`，仍未修，与本批无关）；`test_distill_jobs.py::test_api_shape_matches_frontend_distill_job_contract`（契约锁过期，**本轮已修绿**，见 O.7） |
| 本批新增 | `backend/tests/test_healthz.py` **3 passed**（按显式路径跑：`pytest backend/tests/test_healthz.py`） |

**为什么 junit 里只有 47 个文件、而 `backend/tests/` 有 75 个**：不是收集缺失。根 [pytest.ini](pytest.ini) 的 `testpaths = tests` 只收**根 `tests/`**（45 个 `test_*.py`）；junit 的 47 个 `classname` = 45 个文件 + `test_reasoning_strip` 拆出的 3 个类。`backend/tests/` 是另一套（脚本式断言 + 新加的单测），只在显式给路径时运行——本轮新增的 healthz 用例也在这一套里，所以不进这 789 项。

**时间线要说明**：这轮全量覆盖的是 13:56–14:10 的代码树；另一条线随后在 16:16–16:29 又改了 5 个文件（见 O.3），不在这次全量范围内（按要求不重跑第三遍）。本批改动的 `health.py`（13:04）、`main.py`（13:03）、`check_healthz.py`（13:03）、`test_healthz.py`（13:04）都在 13:56 之前，已被这轮覆盖。

**已知事实（已实测更正）：服务在线时会写真实库，但写者不是 notices 接口，而是 KG 批抽 worker 的心跳。**

- 写入点：`ensure_worker()` 在启动时起守护线程，每 `HEARTBEAT_INTERVAL_SECONDS = 15.0` 秒调一次 `_heartbeat()`，往 `app_settings` upsert 一条 `kg.worker.<host>#<pid>#<随机>` = UTC ISO 时间（[batch_worker.py](backend/app/kg/batch_worker.py#L87-L90)，`_heartbeat` 在 L575-L621，`ensure_worker` 里首次写入并起线程）。
- 现场证据（17:00:57 → 17:01:11，12 秒窗口，逐表比对）：主库 sha256 变了，唯一变化的表是 **`app_settings`**（14 行不变、内容摘要变）；`wizard_notices`、`distill_runs`、`agent_runs`、`assistant_wizard_sessions`、`projects` 全部不变。当前 pid 的心跳行是 `kg.worker.DESKTOP-TC52OS8#28984#568860`。
- 前端每 3 秒轮询 `GET /api/assistant/wizard/5/notices`（16:58–17:01 共 192 次，日志里只有这一个业务路径）；该接口是读接口，只在「有 ACTIVE 蒸馏任务且被判为卡住」时经 `_materialize_stalled` → `emit_notice` 写 `wizard_notices`，并按 `(job_id, kind)` 去重——本轮窗口内 0 写入。
- 后果与结论：只要后端在线，`tests/test_db_isolation.py::test_real_dev_db_untouched_by_test_session` 与 `test_legacy_script_suite` 的真实库指纹守卫就**必然**报「真实库被改动」，写者是服务自身（每 15 秒一次写事务），不是测试隔离失效，也不是本批引入。真实部署下这个写就是会发生的；发布流程要在服务在线时跑全量，应给这两条守卫加「服务在线豁免」或改跑停服窗口，而不是靠「把服务停着不重启」规避。本次不改这两条测试。

此前一次**服务在线**跑的全量（13:37 启动，`junit_full.xml`）正是被这个心跳写入打中：

- `tests/test_db_isolation.py::test_real_dev_db_untouched_by_test_session` 报「跑测期间真实库 sha256 变了」；
- `tests/test_legacy_script_suite.py::test_legacy_script_passes[tests\concurrent_router_e2e.py]` 报「真实库被测试脚本改动了：['novel_dev.db']」。

停服后重跑（本轮 789 项）这两条恢复通过，剩下的只有 `test_distill_fixes` 那条既有失败（契约那条已修，见 O.7）。这两条守卫对「测试期间有服务在跑」没有豁免机制，属于既有设计，本次不改。

### O.7 前后端契约：`DistillRun` 多出的 5 个字段（先判后修）

结论：**后端不该删，前端也没缺，错的是这条测试的契约锁。** 它把 14 个键写死；`health / health_note / progress_at / expected_minutes / elapsed_minutes` 是 99814bb「蒸馏进度心跳与看护」加的字段，`git show HEAD:` 里前后端都已包含，只有这条断言没跟上。

| 字段 | 谁写 | 谁读 |
|---|---|---|
| `health` | `distill_run_to_dict` 每次出参时调 `distill_health()` 派生（不落库，[jobs.py](backend/app/style/jobs.py#L145-L230)） | 前端 [AssistantWorkspace.vue](frontend/src/views/AssistantWorkspace.vue) 的 tag 文案与 `stalled` 样式；后端 `_materialize_stalled` 也调它决定是否发通知 |
| `health_note` | 同上（派生） | 同一页面进度条旁那句人话；通知回执里复用 |
| `elapsed_minutes` | `distill_run_to_dict` 由 `started_at → finished_at/now` 算；`_materialize_stalled` 取它写通知正文 | 前端 `DistillJob` / `WizardNoticePayload` 已声明；UI 通过 `health_note` 间接展示 |
| `expected_minutes` | 落库列：`create_distill_run` 用 `wizard_facts.estimate_minutes` 按参考语料字数外推，无语料为 NULL | `distill_health` 判超时；通知回执；前端类型声明 |
| `progress_at` | 落库列：进度心跳每次刷新 | `distill_health` 判「多久没更新」；前端类型声明（可直接判断百分比是否刚刷新） |

值本身没有 bug（按要求先查再动手）：`expected_minutes = max(1, ceil(chars × MINUTES_PER_CHAR))`，随书字数变化、无参考语料时为 NULL，不是常数；`distill_health` 四态只读实测（`backend/.tmp_verify/health_sanity.py`，伪造时间、不写库）——排队 20 分钟 → `stalled`（阈值 5）、正常 running → `running`、20 分钟无进度 → `stalled`（阈值 15）、40 分钟 vs 预计 3 分钟 → `stalled`（阈值 `max(2.5×3, 3+30)=33`）、success → `success`。

修法：后端响应不动、前端不动，只把 [test_distill_jobs.py](tests/test_distill_jobs.py#L481-L520) 的键集补齐，并把 pending 行的语义断言加严（`health == "queued"`、`health_note` 含「已排队」、`progress_at / expected_minutes / elapsed_minutes` 全 null）——不是放宽，是让契约锁回到真实契约。

结果：`pytest tests/test_distill_jobs.py` **20 passed / 0 failed**（22.1 s，`backend/.tmp_verify/junit_distill_jobs.xml`），目标用例真绿、非 skip/xfail。因为没改后端响应与前端，不需要重启（服务仍是 PID 28984、`/healthz` `fresh=true`），也不需要前端截图。守卫复跑同 O.5：`kg_mentions` 34、gid45 覆盖 125、六探针 `617c601c` / `24e7c486`、11 条持有行全 true、p1/p5 五组指纹一致、`generated.db` 未写。

## §P `reset_chapter` 接上持有轴（重跑不再叠段/留孤儿）

### P.1 谱系与改前缺口

`reset_chapter` 原来只清生成轴的 `gen_*`（`gen_edges / gen_events / gen_candidates / gen_rejected_subjects / gen_edge_invalidations / gen_node_mentions / gen_nodes`），持有轴三张表完全没接。实际写侧谱系（读代码确认，不是推测）：

| 表 | 唯一写入口 | 本章归属字段 | 与其它表的关系 |
|---|---|---|---|
| `kg_item_holding` | [record_holding](backend/app/kg/generated_store.py#L754) | `valid_from` + `evidence_chapter`（`_extend_back` 只前移 `valid_from`，`evidence_chapter` 仍记当章） | 主键被旁注引用 |
| `kg_holding_story_segment` | [record_story_segment](backend/app/kg/generated_store.py#L947) | 主线旁注看 `holding_id`；回溯/预叙分支 `holding_id IS NULL`，只能按 `evidence_chapter` | `holding_id → kg_item_holding.id` |
| `kg_item_instance` | [record_item_instance](backend/app/kg/generated_store.py#L726) | `evidence_chapter` | 被持有段/旁注的 `instance_id` 引用 |

`kg_item_group` / `kg_item_surface` 是身份登记表，不按章派生（`ensure_item_group` 自身幂等），不进 reset。`:8000` 的服务路径不调用 `reset_chapter`；它只被 [ingest_chapter](backend/app/kg/layer_pipeline.py#L37) 调用，所以这次改动对在线服务不是热路径，但必须让盘上代码与进程一致（见 P.5）。

### P.2 修法（顺序有硬约束）

[_reset_holdings](backend/app/kg/generated_store.py#L1274) 在 [reset_chapter](backend/app/kg/generated_store.py#L1350) 最前面执行，同一事务、不单独提交（与 `ingest_chapter` 的提交边界一致）：

1. 先取本章 `valid_from = ? OR evidence_chapter = ?` 的持有 id；
2. **先删旁注**（`holding_id IN ids`），再删持有主键——没有 FK，顺序错了不会报错，只会悄悄留孤儿；
3. 删本章分支旁注（`holding_id IS NULL AND evidence_chapter = ?`）；
4. 回滚本章做过的关闭：`UPDATE ... SET valid_to = NULL WHERE valid_to = ?`，并带 `NOT EXISTS` 同键开放段守卫，避免撞 `ux_item_holding_open`（本章新段还开着时直接重开会违反部分唯一索引）；
5. 全库扫孤儿旁注（`holding_id` 已不存在）——`record_holding` 的同章竞争会用 `DELETE` 撤掉零长度段（`_resolve_same_chapter`），它的旁注正是孤儿来源；
6. 删本章登记且已无人引用的 `kg_item_instance`；仍被别的章引用则保留。

没改 schema，也没改 `record_holding / record_story_segment` 的写入语义。

### P.3 改前 / 改后对照（同一脚本、同一场景）

`backend/.tmp_verify/repro_reset_holdings.py`（`--old` 回放改前实现，全在临时库上跑）。场景：同一章 3 连续重写，持有者 A→B→A→B→A，每轮先 reset 再跑投影。

| 轮次 | 持有者 | 改前 持有/故事段/孤儿 | 改后 持有/故事段/孤儿 |
|---|---|---|---|
| 1 | 许七安 | 1 / 1 / 0 | 1 / 1 / 0 |
| 2 | 许新年 | 0 / 1 / **1** | 1 / 1 / 0 |
| 3 | 许七安 | 1 / **2** / **1** | 1 / 1 / 0 |
| 4 | 许新年 | 0 / **2** / **2** | 1 / 1 / 0 |
| 5 | 许七安 | 1 / **3** / **2** | 1 / 1 / 0 |

如实说明：**持有行本身没有翻倍**。同章同持有者重跑时 `record_holding` 走 `confirmed`；同章换持有者时 `_resolve_same_chapter` 会删掉零长度行、只写候选（所以持有行是 1→0→1 抖动）。真正累积的是**故事段（1→2→3）与孤儿旁注（0→1→2）**，以及「本章关闭的上一段没有回滚」造成的幻影空洞。改后三者全部归零增量、无孤儿，段链闭合（`许七安 [3,4)` + `许新年 [4,∞)`）。

提及轴另测：`ingest_chapter` 同章连跑两遍（自带 reset，canon 给三个名字）——`gen_nodes 2 / gen_node_mentions 2 / gen_edges 0 / gen_events 0 / mention_sum 4`，第二遍与第一遍完全一致，零增量（`backend/.tmp_verify/repro_ingest_twice.py`）。

如果线上看到的确实是持有行数翻倍，那更可能是 `item_name` 在两次运行间不稳定（短名被并进长名/写法变化）导致换了唯一键，这一类不在「按章 reset」能覆盖的范围，需要另立一条排查（不属于本次改动）。

### P.4 测试

新增 3 个用例（未改任何既有断言）：

- [test_reset_chapter_clears_holdings_and_story_segments](backend/tests/test_kg_item_holding.py#L441)：同章 A→B→A，逐轮断言 `持有/故事段/开放段/孤儿/实例 = 1/1/1/0/0`；
- [test_reset_chapter_reopens_segment_closed_by_that_chapter](backend/tests/test_kg_item_holding.py#L465)：重跑后一章时本章关闭的上一段要重开，连续两次重跑保持 `2/2/1/0/0` 且段链连续；
- [test_reset_chapter_keeps_instance_referenced_by_other_chapter](backend/tests/test_kg_item_holding.py#L491)：本章登记但被别的章引用的实例保留，只删无人引用的那个。

结果：`test_kg_item_holding.py` **18 passed**；`test_kg_item_holding.py + test_holding_projection.py + test_kg_layers.py` **40 passed / 0 failed**。

根目录全量：**3 failed + 1 error**，全部有归属，无一与本次改动相关：

- `test_db_isolation::test_real_dev_db_untouched_by_test_session` 与 `legacy_script_suite[concurrent_router_e2e]`：服务在线时 KG worker 每 15s 写 `app_settings`（已知事实，见 §O.6 的更正）；
- `test_distill_fixes::test_generation_path_extraction_still_commits_project_cache`：既有失败，未动；
- `legacy_script_suite[test_kg_v3_frontend_api]`：跑测时另一条线正在改 [projects.py](backend/app/routers/projects.py)（瞬时 `SyntaxError: invalid character '（'`），文件写完后 `compileall` 通过、`import app.main` 通过；同一用例复跑只剩上面那条「真实库被在线服务写」的已知门禁。

### P.5 重启与守卫

- 改前监听者：PID **28984**（16:58:03 启动），`/healthz` 已 `fresh=false`（`build_time` 17:47:47 来自另一条线的 `character_rename.py`）；
- 停旧进程后 `netstat` 无 `:8000` 监听，同参数（cwd=`backend`，`.\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000`）拉起；
- 改后监听者：PID **33084**，`netstat` 只有一条 `LISTENING 33084`，与 `/healthz.pid` 一致；启动日志 `uvicorn_restart_20261111_1751.log`；
- `/healthz` 原文（HTTP 200）：

```json
{"ok":true,"fresh":true,"pid":33084,"git_sha":"7af5a80","dirty":true,"started_at":"2026-11-11T17:57:10.545+08:00","started_at_epoch":1794391030.5456462,"build_time":"2026-11-11T17:47:47.000+08:00","build_time_epoch":1794390467.0004296,"build_time_file":"app/kg/character_rename.py","scope":{"roots":["backend/app/**/*.py","backend/config/**/*.{yaml,yml,json}","backend/.env"],"excluded_dirs":[".pytest_cache",".tmp_verify","__pycache__","backups","data","migrations","node_modules","venv","worktrees"]}}
```

- `recover_running`：启动后 `agent_runs` 中 `status='running'` 共 **0 行**（`kg_batch_extract` 0），无可恢复任务；
- 守卫（`backend/.tmp_verify/post_guards.py`，日志 `post_guards_after_reset_fix.log`）：`kg_mentions` **34**（项目 1=26 / 5=8）、gid45 覆盖 **125**（47+4+1+2+76 去重并集）、六探针 `617c601c` / `24e7c486`、11 条持有行全 `true`、p1/p5 五组指纹与 002928 一致、`generated.db` sha256 仍 `0a6b0025…`（未写）。

## §Q `地书碎片` / `地书聊天群`：现状核实与「堵源头」停点

### Q.1 现状（只读）

| 层 | 状态 |
|---|---|
| `kg_item_group` | 只有 id=9 `地书`（active）。**没有**名为 `地书碎片` / `地书聊天群` 的独立组 |
| `kg_item_surface` | id=57 `地书碎片`、id=58 `地书聊天群`：都在 group 9，`status=approved`、`confidence=0.6`、`source=migration`、note「实体 aliases 迁移带入」——是**写法**，不是独立规范名 |
| 持有轴 | `kg_item_holding` / `kg_holding_story_segment` / `kg_item_instance` 命中 `地书*` 的 7 行全是 `地书`（含 gid 9），两个派生词 **0 持有行** |
| `kg_entities` | 项目 5 只有 id=9 `地书`；aliases 已包含这两个词及 `三号碎片 / 地书碎片·三号 / 叁号镜子 / 地宗法宝 / 玉石碎片 / 枕头底下的玉石小镜 / 那面玉石小镜` |
| 提及/关系/事件 | `kg_mentions` / `kg_relations` / `kg_events` 对这两个词 **0 行**（`kg_mentions` 总数仍 34） |
| 生成层 | `gen_nodes` 无这两个词 |

### Q.2 重跑会翻回来的证据（临时副本演练）

`backend/.tmp_verify/probe_derived_sync.py`：

- `normalize_extraction({"entities":[{"kind":"item","canonical_name":"地书碎片"}]})` → canonical 仍是 `地书碎片`，`ITEM_ALIASES` 不折叠；
- `ensure_item_group(con, "地书碎片")` → **新建 group 105**；`ensure_item_group(con, "地书聊天群")` → **新建 group 106**，两次的 surface 插入都被既有 57/58 的唯一键挡下并报 `surface_conflict`；
- `_upsert_entity` 只按 `canonical_name` 与 `should_merge` 匹配，`地书碎片` 不命中 `地书` → 会新建实体。

即：组和实体都会在下次同步/重跑时被重新插出来，之前的清理会被翻回来。

### Q.3 为什么「只加剔除表」不够（停点）

- `canon_source.ITEM_ALIAS_BLOCKLIST` 只在 `fold_item_aliases` 内部剔除**别名**，不参与实体/组创建；
- `canon_source.NON_ENTITY_NAMES` 只用于 legacy faction 节点过滤，抽取同步不读；
- `GENERIC_ALIASES` 只存在于迁移脚本 [apply_item_merge.py](backend/migrations/2026-11-10-item-holding-v3/apply_item_merge.py)，运行时不读；
- 反过来把它们塞进 `ITEM_ALIASES` 折成 `地书` 也不对：[canon_source.py](backend/app/kg/canon_source.py#L803-L807) 已写明「玉石小镜 / 玉石镜 不在这里」——九件套的类通称不能并成一件，`地书碎片` 是同一类集合体，`地书聊天群` 更是通讯渠道而非物品。

结论：**堵源头需要新增一个实体级剔除钩子（同步逻辑）或改抽取提示词**，正好命中你设的停点，所以先停在这里报告，没有动代码、没有动库。

### Q.4 待你拍板的三个选项（附影响面）

| 选项 | 组侧 | 实体侧 | 影响 |
|---|---|---|---|
| **A. 通称组（纯数据）** | 建 `status='generic'` 的 `地书碎片`（`generic_members='[9]'`）与 `地书聊天群`（成员空、note=通讯渠道），把 57/58 挪过去。`ensure_item_group` 按名复用任意状态的组（已核实无 status 过滤）→ 重跑不再建组 | 仍会建实体 | 读侧 `地书碎片` 解析为 `None`（与 `玉石小镜` 现状一致，generic 组不参与 `resolve_group_id`）；`地书碎片·三号` 的个体时间线不受影响；持有/提及 0 行变化 |
| **B. 实体级剔除（同步逻辑）** | — | 新增派生词集合，在 `normalize_extraction` 入口丢弃这两个 item 实体；引用它们的边端点解析失败即跳过 | 立即数据影响为 0（当前本来就没有这些行）；这是唯一不改提示词就能拦住新实体的办法；需要新增「派生词不再建新实体」用例 |
| **C. 抽取提示词** | — | 提示词显式禁止 | 不推荐：提示词是通用模板，且另一条线正在改 `generation/prompts.py`，容易冲突 |

另有一个组侧替代方案 **D**：让 `ensure_item_group` 在新建组前先查 `kg_item_surface`（approved 写法 → 复用其 group）。这是通用修复（任何已登记写法都不会再长出同名新组），且 `地书碎片` 仍解析到 group 9；代价是改一处存储层逻辑。

我的建议是 **D + B**（组侧通用复用、实体侧剔除），保留 57/58 作为 group 9 的写法，读侧行为不变。

### Q.5 数据处置（未执行）

按要求，第二项动库必须备份 + 快照 + `--rollback`，并在副本上演练回滚。目前只做了只读核实与临时副本演练（`probe_dishu.py` / `probe_derived_sync.py`），**真实库 `generated.db` / `novel_dev.db` 未写**，也没有生成迁移脚本——等你选定 A/D + B 之后再做。

## §R D + B 实现（2026-11-11 18:2x）

按批准执行 **D（存储层复用已登记写法）+ B（同步层丢弃派生实体）**，保留 `地书碎片` / `地书聊天群` 作为 group 9 的写法。**本次零数据变更**：D/B 都是代码路径，真实库没有需要清理的行。

### R.1 D 的前置核查：真实数据里没有「一写法多行」

`backend/.tmp_verify/check_surface_multi.py`（只读）：

| 项 | 结果 |
|---|---|
| `kg_item_surface` 总行数 / 不同 surface 字符串 | **136 / 136** |
| 同一 surface 字符串出现 >1 行 | **0** |
| 同一 surface 字符串有 >1 条 approved 行 | **0** |
| approved 行跨多个 `group_id` | **0** |

即：真实库里不存在「命中多行写法、无法判定」的情况，不需要停下来报冲突。多行判定规则仍写进代码并有用例锁住（见 R.2），未来真出现时行为是确定的。

### R.2 D：`ensure_item_group` 复用 approved 写法所属组

[generated_store.py](backend/app/kg/generated_store.py#L619-L684) 新增 `_approved_surface_hit`，`ensure_item_group` 在「名字不是任何组的规范名」时调用它：

- **只认 approved**：rejected 写法（如 `佩刀` → 组 45 但被拒）不参与复用，避免把被否定的身份重新挂回来；
- **通用**：不针对这两个词，任何已登记写法都走同一条路；
- **槽位语义**：命中的组是什么就是什么——组 9（book 5 真书组）就返回 9；`status='generic'` 的泛称组按泛称复用，返回 `group_status='generic'`，`resolve_group_id` 仍不解析它，不会把通称塞进真书组；
- **跨书判定（按书优先）**：传了书 → 先取本书显式写法（`novel_id=书`），没有再退到项目级通配写法（`novel_id IS NULL`）；**别书的写法绝不复用**。没传书 → 全部行都是候选。
- **命中多行（已定义）**：候选过滤后若仍指向 ≥2 个不同组 → **报歧义、不猜**，抛 `RuntimeError` 并列出 `surface#id→group#id(novel=…)`；`project_holdings` 会把它收进 `stats.errors` → `agent_runs.note`。理由：把 A 物品的持有史挂到 B 组是不可逆的语义污染，而「没复用」最坏只是多一个可见、可修的组。真实数据当前 0 例（见 R.1）。
- 返回新增 `reused_surface_id` / `group_status`，复用时不写重复写法（`surface_created=False`）。

### R.3 B：`DERIVED_ITEM_NAMES` + `drop_derived_items`

[canon_source.py](backend/app/kg/canon_source.py#L824-L847) 新增独立表 `DERIVED_ITEM_NAMES = {"地书碎片", "地书聊天群"}`，在 `normalize_extraction` **入口**（折叠之前）调 `drop_derived_items`（[L902](backend/app/kg/canon_source.py#L902-L958)）：

- 只丢 `kind='item'` 的这两个名字；
- 引用一并处理：关系 / 状态变化 / 情报里指向它们的行整条丢；事件保留剧情，只把 `subject` / `object` / `location` 中的派生词清空；
- **可见性**：命中记一条 `warning`，计数写回 result（`derived_items_dropped` / `derived_refs_dropped`），`batch_worker._persist_extraction` 转进 `ExtractionStats.derived_items_dropped` / `derived_refs_dropped`，最终落在 `agent_runs.note.batch_stats` 可查。

与三张既有表的分工（注释里逐条写明）：`NON_ENTITY_NAMES` 是建词表时剔抽象词；`ITEM_ALIAS_BLOCKLIST` 是折叠后剔别名（不删实体、剔通名）；`ITEM_ALIASES` 是异写折叠（把 `地书碎片` 折成 `地书` 会把碎片粒度并进本体，与「九件套通称不折」冲突）；`GENERIC_ALIASES` 只在迁移脚本、运行时不读。所以必须单列一张「同步层丢弃表」。

**已知事实**：这两个词当前 0 持有 / 0 提及 / 0 关系 / 0 事件，本次**不清理任何历史数据**，只拦「以后重跑/新批再抽出来」。

### R.4 副本演练（真实库临时副本，未写真库）

`backend/.tmp_verify/drill_d_b.py`：

| 调用 | 改前（旧代码逻辑 / Q.2 实测） | 改后 |
|---|---|---|
| `ensure_item_group("地书碎片", project=5, novel=5)` | 新建 group 105 | **group_id=9**、`group_created=False`、`surface_created=False`、`reused_surface_id=57` |
| `ensure_item_group("地书聊天群", …)` | 新建 group 106 | **group_id=9**、`reused_surface_id=58` |
| `ensure_item_group("镇国剑", …)` | 复用 group 100（按规范名） | **不变**：group 100，无写法复用 |
| `ensure_item_group("太平刀", …)` | 建新组（当时无同名组） | **复用 group 45**（surface 281 已是 approved 写法）——D 的通用效果，不是特例 |
| `ensure_item_group("演练新物品锤", …)` | 建组 | **仍建组**（group 105/写法学 137） |
| 组 / 写法总数 | 55 / 136 | 56 / 137（只多了演练新物品那一组一写法） |

`normalize_extraction` 喂含两词的抽取：entities 剩 `['地书','许七安']`，计数 `{'地书碎片':1,'地书聊天群':1}`、refs=2，关系被清、事件 `object=None`，并打出一条 warning。

### R.5 用例

- [test_holding_projection.py](backend/tests/test_holding_projection.py#L232-L341) 新增 4 个：已登记写法复用组且新物品仍建组、generic 组按泛称语义复用、别书写法不跨书复用、多行歧义抛错且传书后可按书判定；
- [test_kg_canon_source.py](backend/tests/test_kg_canon_source.py#L675-L722) 新增 2 个：派生实体丢弃 + 计数/日志/引用清理、`镇国剑`/`太平刀` 不受影响；
- 既有测试一行未改。相关套件：`test_holding_projection` + `test_kg_canon_source` + `test_kg_item_holding` + `test_kg_mentions_alias` + `test_kg_silent_failures` **89 passed**；新增用例单跑 4 + 2 passed。

### R.6 守卫与真实库

`backend/.tmp_verify/post_guards.py`（日志 `post_guards_after_d_b.log`）：

| 守卫 | 结果 |
|---|---|
| `kg_mentions` | **34**（项目 1=26 / 5=8；`p5` 剩余 id `[20…27]`，27 条污染 0 残留） |
| gid45 覆盖 | **125**（47+4+1+2+76 去重并集） |
| 六探针 | 五个刀名 `held / gid45 / via=surface / lines=2 / md5 617c601c`；`镇国剑` `held / gid100 / md5 24e7c486` |
| 11 条持有行 | 全部 `after_resolvable=True`，`bad=[]` |
| p1/p5 指纹 | `c892a57c` / `df488a3b` / `66d2c025` / `e3e00c9e` / `2c4f86d2`，与 002928 备份逐条一致 |
| `generated.db` | sha256 仍 `0a6b0025…`、mtime 仍 `2026-11-11 03:11:38`——**未写** |

真实库无需动数据，因此**没有**备份/快照/回滚动作；副本演练只在临时目录进行，真库零写入。

### R.7 重启

- 改前进程 PID 33084（17:57:10 启动），`check_healthz.py` 判 STALE（代码 18:26:46 新于进程）；
- 第一次重启 PID 38448 后，另一条线仍在改 `power_prompt.py` / `bible.py` / `character_rename.py` / `projects.py`（18:30–18:31），`build_time` 被顶新，`fresh=false`；等文件稳定（`compileall app` 通过）后第二次重启；
- 当前监听者 **PID 23288**，`netstat` 只有一条 `LISTENING 23288`，与 `/healthz.pid` 一致；启动日志 `uvicorn_restart_20261111_1833.log`；
- `/healthz` 原文（HTTP 200）：

```json
{"ok":true,"fresh":true,"pid":23288,"git_sha":"7af5a80","dirty":true,
 "started_at":"2026-11-11T18:32:25.279+08:00",
 "build_time":"2026-11-11T18:31:15.984+08:00",
 "build_time_file":"app/routers/projects.py"}
```

`check_healthz.py`：`OK 进程不旧于代码（余量 69.295s）`。



