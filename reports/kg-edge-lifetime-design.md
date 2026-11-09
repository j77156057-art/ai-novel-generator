# 关系边生命周期设计：建边一次、跨章复用（只设计，未改代码）

## 0. 要解决的问题

现行抽取是「**每章重触发**」：第 50 章要复用 ch10 建立的关系，仍得在该章正文里再找到
「同僚/并肩/麾下」。实测 918 章：有 canon 边的 3996 个章内共现事件里，98.1% 因为
当章没有触发词被挡，只剩 76 个事件、61 行输出（门开后 20 行）。
这不是精度问题，是**作用域错了**——关系的「建立」和「复述」被混为一谈。

目标：**建立时用触发词 + 主宾门确认一次，之后按生命周期跨章复用；复用不再要求触发词。**
密度回到 canon 边量级，同时每条边都带建边原句证据。

## 1. 数据模型

一条关系边 = 一个带生命周期的实例：

| 字段 | 含义 |
|---|---|
| `from` / `to` / `rel` | 端点与关系类型 |
| `direction_basis` | `directed` / `symmetric`（并列主语按 `SYMMETRIC_RELS` 存一条） |
| `established_chapter` | 触发词 + 主宾门确认成立的章号 |
| `established_evidence` | 建边原句（必须逐字保留，供审计与 prompt 引用） |
| `confirm_method` | `role_verb` / `postposition` / `copula` / `coordinated` / `mutual` / `noun` / `canon` |
| `trigger` | 命中的触发词 |
| `confidence` | 建边置信度（过去时态已下调，沿用现有 `PAST_MARKERS`） |
| `valid_from` | 生效章号（= `established_chapter`） |
| `valid_to` | 失效章号；`NULL` 表示仍然有效 |
| `status` | `active` / `historical` / `ended` / `superseded` |
| `polarity_state` | 复用现有：`stable` / `conflicting`（`canon_source.POLARITY_CONFLICTING`） |
| `polarity_kind` | `SAME_CHAPTER_CLASH` / `CROSS_CHAPTER_REVERSAL` |
| `polarity_spans` | `{ally: [ch..], enemy: [ch..]}`，复用现有结构 |
| `last_confirmed_chapter` | 最近一次明确复述（可选，仅用于置信度刷新） |
| `source` | `canon` / `generated` / `legacy` / `cleaned` |

**渲染判定**（给定当前章 `C`）：
- `current`：`valid_from <= C` 且（`valid_to is NULL or C <= valid_to`）且端点在 `C` 时在世；
- `historical`：`C > valid_to`，或端点已死亡/退场；
- 其余不入邻域。

## 2. 三个失效问题

### 2.1 关系变了（反目 / 决裂 / 背叛）

- **失效依据从哪来**：一张小的「关系变更触发词」表，和建边触发词分开：
  - 平级：`反目|决裂|背叛|翻脸|断交|分道扬镳|恩断义绝|反戈|倒戈|投敌|兵戎相见`
  - 层级：`罢官|革职|逐出|除名|脱离|叛出|自立门户|卸任|调离`
  - 亲属：`和离|休书|断绝关系|逐出家门`
- **谁触发重判**：每章抽取仍跑两遍扫描，但职责分开：
  1. **建边扫描**：触发词 + 主宾门 → 新边（只在没有现存边时）；
  2. **失效扫描**：变更触发词 + 同一个主宾门 → 关闭旧边（`valid_to = 章号`、`status = ended`），
     需要时再开一条新边。
  明确复述（再次出现「同僚/麾下」）**不触发重判**，只刷新 `last_confirmed_chapter`。
- **边界**：变更触发词也必须过主宾门，否则会重蹈「挂错对象」；解不出主宾时就记事件、不动边。

### 2.2 角色退场或死亡

- **死亡来源**：canon `attrs.arc=死亡`（已有）+ 生成层 `death_claim` 事件（`conflict_scan` 已产）。
  取最早可确认的章号为 `death_chapter`。
- **下游怎么区分历史/当前**：靠 `status` + `valid_to` + `endpoint_alive(C)`：
  - 若 `C > death_chapter` 或 `C > valid_to`：渲染进「**历史关系（仅可作「曾…」表述）**」段，
    不进当前邻域；
  - 现行 `build_context` 已有一段「原著层·关系（极性相反，仅可作「曾…」表述）」，
    把它泛化成「历史/失效关系」段即可，不新增渲染通道。
- **退场但未死**（离开势力/地域）：由 `脱离|叛出|调离|自立门户` 触发 `status=ended`，同上。

### 2.3 极性冲突（先 ally 后 enemy）

- **选择：先建后冲 + 双写 `polarity_spans`**（沿用现有 `conflicting` 层，不新开表）。
  - 首次建边：`polarity_spans[rel] = [ch]`，`polarity_state = stable`。
  - 之后出现相反极性且过主宾门：
    - 若在**更晚**的章：判为 `CROSS_CHAPTER_REVERSAL`，旧边 `valid_to = 新章-1`、
      `status = superseded`，新边 `valid_from = 新章`；同时 `polarity_spans` 保留两段历史。
    - 若在**同一章**：判为 `SAME_CHAPTER_CLASH`，`polarity_state = conflicting`，
      两边都只进「极性相反」段，不作当前关系使用。
- **和现有 `conflicting` 层对接**：`build_context` 现在就会把
  `polarity_state == POLARITY_CONFLICTING` 的边剔出 1 跳邻域、单独渲染成
  「仅可作曾…表述」。本设计**不改这个契约**，只负责在抽取侧正确写入
  `polarity_state / polarity_kind / polarity_spans`。
- **顺序不可知时**（同章、或多章但无法排序）：一律降为 `conflicting`，宁可当「曾…」也不当现状。

## 3. 预计边数

| 口径 | 数量 | 说明 |
|---|---|---|
| 当前输出（门开，逐章行） | 20 行 | 去重后只有 **14 个唯一人物对** |
| 触发词命中的唯一人物对（门关） | 36 对 | 918 章全量 |
| 改造后预计（建边一次，过门） | **约 25–32 条唯一人物—人物边** | 下限 14（实测去重），上限 36 |

**关键不是条数，是覆盖**：同一条边在「两端共同出场的所有章」都可用，而不再只在那 1 个
碰巧写了触发词的章可用。举例：`许七安-临安` 现在只在 ch127/826/855 出现，改造后从建边章起
到失效前全程可用。

## 4. 跨章复用的有效期

**建议：默认无限期（`valid_to = NULL`），直到被显式推翻。**

理由：
- 「最近 N 章」会误杀长期稳定关系（如许七安-魏渊的隶属横跨数百章），且 N 的选择没有依据；
- 无限期 + 显式失效事件（2.1/2.2/2.3）语义清晰，可审计：每条失效都有原句依据；
- 与现有极性机制天然一致（反转就是一次显式失效）。

约束：
- 端点死亡/退场 → 立即 `ended`；
- 变更触发词过门 → 立即 `ended`/`superseded`；
- 极性相反 → 按 2.3 处理；
- 以上都没有时，允许边一直有效；**不引入「定期重确认」**，否则又把密度问题带回来。

## 5. 与现有代码的接口（落地时）

- `generated_extract._collect_edges` 拆成两步：`_establish_edges`（现逻辑 + 门）与
  `_detect_invalidations`（新增变更触发词扫描）。
- `generated_store` 的 `gen_edges` 补列：`established_chapter / established_evidence /
  confirm_method / valid_from / valid_to / status`；`POLARITY_*` 列已有则复用。
- `layer_context.build_context` 的边筛选从「本章触发」改为「本章在有效期内且端点在世」；
  历史/失效边走「仅可作曾…」段。
- `conflict_scan` 不用改：它读 canon 事实 + 本章文本，与边生命周期解耦。

## 6. 验收口径（下一轮）

1. 唯一人物—人物边 ≥ 25，且每条都有 `established_evidence`；
2. 建边原句审计精度沿用主宾门口径（目标 ≥ 85%，先把「关系名词·唯一指代」分支收紧）；
3. 注入 3 类失效场景各 ≥2 例（反目、死亡、极性反转），断言旧边 `ended`、下游不再当现状；
4. 不新增「放宽触发词」造成的无证据边：复用边的证据一律指向建边原句。

## 7. 落地状态（三处收紧已实现）

设计批准后按「收紧三处」实现，代码在
`backend/app/kg/generated_extract.py`（`detect_invalidations` / `detect_deaths` /
`_assertion_rejected`）、`generated_store.py`（`invalidate_edge` /
`close_edges_by_endpoint` / `load_invalidations`）、`layer_pipeline.py`（写库）、
`layer_context.py`（失效边不再当现状渲染）。测试：`backend/tests/test_kg_edge_lifetime.py`。

**收紧一：失效触发词走同一套硬拒。** `detect_invalidations` 复用建边的
question / hypothetical / reported / negated 四类判定（`_assertion_rejected`），
并补了一条建边侧没有的 **未遂/否定词表** `_UNREALIZED`：
`差点|差一点|险些|几乎|几近|眼看要|就要|正要|未遂|九死一生|并未|尚未|从未|从不|
绝不|不曾|未曾|没能|没有|并非|不是|不再|无法|不能|不会|不肯`。
原因是建边侧的 `_NEGATED` 只认 `未曾/未能/未免`，「并未背叛」会漏过去。
三条断言（`test_unrealized_conflict_does_not_end_edge` 等）都是「边仍在」。
顺带把 `_HYPOTHETICAL` 补上 `若不是|若非|要不是`——原表只有 `若是`，而中文里
「若不是…早就翻脸」是更常见的反事实句式。

**收紧二：死亡只认既成事实。** `DEATH_MARKERS` 补 `战死|殒命|遇害`；
`detect_deaths` 从 claim 里取 death 断言，再过同一个 `_assertion_rejected`。
「差点死了」「险些丧命」「九死一生」都不切 `historical`（三条断言在
`test_near_death_does_not_mark_historical`），只有「战死沙场」这类才切
（`test_confirmed_death_marks_edges_historical`）。

**收紧三：推翻不动旧证据。** `upsert_edge` 只写一次 `established_chapter` /
`established_evidence`（后续复述用 `COALESCE` 不覆盖）；失效走
`invalidate_edge` → 追加一行 `gen_edge_invalidations`（含 `chapter / reason /
trigger / evidence`）并把边置 `ended`/`historical`、写 `valid_to`。
`test_confirmed_conflict_ends_edge_and_keeps_both_evidence` 断言
`established_evidence` 仍是建边原句（含「结盟」）、失效记录里是「反目」那句，两条同时存在。

**额外做的一处**：`reset_chapter` 会删掉本章的失效记录、并把 `valid_to == chapter`
的边复位成 `active`，否则重跑同一章会把边永久关死（计数不可复算）。

