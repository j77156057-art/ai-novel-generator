# 关系极性审计（2026-11-10）

## 1. 做法

- **`REL_WHITELIST` 不动**：仍是 ally / enemy / subordinate / superior / mentor /
  student / family / sibling / parent / child / located_at / member_of 等 13 种。
  极性不是新的关系类型，而是 ally / enemy 这对关系的状态。
- 新增三态 `polarity_state`，在**渲染与审计层**过滤，不改词表、不删边：
  原著层的边原样保留，只是不再把「上一章还是敌人」的边当现状喂给下一章。
- 冲突边**不进邻域扩展**（adjacency），单独渲染成
  `【原著层·关系（极性相反，仅可作「曾…」表述）】`，最多展开 4 对，其余兜底
  「另有 N 对未展开」。

## 2. 三态契约

| `polarity_state` | 含义 | 下游行为 |
|---|---|---|
| 键不存在 | 不是 ally/enemy，或该对实体只有一个极性方向（无对照） | 按普通关系使用 |
| `stable` | 同一对实体只有一个极性方向（如只有 ally） | 按普通关系渲染；返回的 `edges` 带 `polarity_state="stable"` |
| `conflicting` | 同一对实体同时存在 ally 与 enemy | 不进邻域；只进「极性相反」提示段，禁止当现状 |

## 3. 两类冲突的口径（写死在代码里）

**不要再用「章节区间重叠」混算**——区间重叠会把「ch1–200 为敌、ch150 为友」
这种真实反转误判成同章矛盾。判据是**章号集合**，不是最早/最晚章：

- **`same_chapter_clash`：正反两条边的章号集合有交集。**
  同一章里既写盟友又写敌人，多半是关系抽取把不同对象/不同事件混到一对实体上，
  优先级最高，先报给人工。
- **`cross_chapter_flip`：章号集合无交集。**
  先敌后友或先友后敌，属剧情反转，可以作为「曾…」表述使用，但不能当现状。

两者互斥，有交集即判 `same_chapter_clash`。

## 4. 实现落点

| 位置 | 职责 |
|---|---|
| `app/kg/canon_source.py::chapter_list` | `chapters` 字段解析：JSON 数组 `[2,4,8]` 与区间 `"1-30"` 都容忍 |
| `app/kg/canon_source.py::annotate_polarity` | 给每条 ally/enemy 边打三态 + `polarity_kind` + `polarity_spans`；`load_legacy` 与 `_edges_from` 两个出口都调用（重新标注会先清旧键，过滤后不会残留） |
| `app/kg/conflict_scan.py::scan_polarity` | canon 内部审计，返回 `same_chapter_clash` / `cross_chapter_flip` / `counts` |
| `app/kg/layer_context.py::build_context` | 冲突边不进 adjacency；正常关系段不出现；渲染「极性相反」段；返回 `canon_conflicts` 与 `layers.canon_conflicts` |

## 5. 实测（并入 53 个实体后）

| 口径 | 冲突对 | 同章并存 | 跨章反转 |
|---|---|---|---|
| mixed 词表（本次复核） | **29** | **5** | **24** |
| 全量 legacy 词表（对照） | 44 | 13 | 31 |
| mixed 并入前（对照） | 16 | 2 | 14 |

mixed 中 ally/enemy 边共 211 条：145 条 `stable`、66 条 `conflicting`
（一个冲突对含一正一反两条边）。

### 5.1 同章并存（5 对，优先复核）

| 实体对 | 交集章 |
|---|---|
| 周旻 / 许七安 | ch195 |
| 朱阳 / 许七安 | ch123 |
| 杨珍珍 / 许七安 | ch14 |
| 苏苏 / 许七安 | ch204 |
| 许七安 / 魏渊 | ch134、ch137 |

### 5.2 跨章反转（24 对，节选）

褚采薇–许七安（敌 ch6 / 盟 ch30–229）、临安–许七安（敌 ch121–127 / 盟 ch123–128）、
元景帝–魏渊（盟 ch126–159 / 敌 ch138、163）、恒慧–恒远（盟 ch148 / 敌 ch146）、
浮香–许七安（盟 ch101–143 / 敌 ch146）、神殊–许七安（敌 ch152 / 盟 ch227）。

## 6. 已知局限

- `same_chapter_clash` 优先判为**数据噪声**，不判剧情；本次复核的 5 对里除许七安–魏渊
  外都只有 1 个交集章，基本是一跳共现或关系方向抽错。
- `cross_chapter_flip` 只说明「变过」，不判断哪一端是当前立场——这需要事件层
  的时间点，当前词表没有，所以渲染成「曾…」而不是二选一。
- 冲突只覆盖 ally / enemy。subordinate / superior 等关系的翻转（如背叛）目前
  不进审计，留待势力聚合层处理。
