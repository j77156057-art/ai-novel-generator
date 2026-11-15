# 五级目录 · 本轮实现注记

> 日期：2026-11-13
> 范围：按 2026-11-13 八条裁决落地「建卡 → 骨架注入 → 作者/助理工具」。
> 本文件只记录本轮实现与实测数字；设计口径以 `docs/five-level-catalog-design.md`
> 为准（该文件由另一会话维护，本轮**未改动**）。

---

## 一、落地内容

| 文件 | 本轮改动 |
| --- | --- |
| `backend/app/retrieval/build_cards.py` | 建卡 v2：撤销 metric 硬过滤豁免；高频场景模板统一 `deprecated`；路径口径 `paths-v2`；统计补充 deprecated / active 数字 |
| `backend/app/retrieval/catalog_paths.py` | 路径词汇与拼装；新增 `classify_path()`，按层级词表认领 L1–L4，不再按字符串位置取层 |
| `backend/app/retrieval/catalog_store.py` | 卡表 schema `v2`；索引改用卡片结构化字段（`l1/l2/l3/l4`）；新增 `active_cards`；`filter_cards` 默认排除 deprecated，`find()` 仍可查到 deprecated |
| `backend/app/retrieval/skeleton.py` | 骨架只渲染 `active_cards`；缓存键 `distill_id + catalog_version`；结构节「Top 3 展开 + 其余压行」 |
| `backend/app/retrieval/trace.py` | `_l2()` 改走 `classify_path()`，修复 `style/dialogue` 把 L4 当 L2 的跨场景软档误判 |
| `backend/app/retrieval/tools.py` | `browse/search/similar` 默认只用 active 卡；`rule` 保留 deprecated 回查并返回 `deprecated / deprecated_reason` |
| `backend/app/agents/registry.py` | 作者 / 助理 agent 共用五个只读工具；每次调用写一行 `retrieval_logs` |
| `backend/app/generation/prompts.py` | 系统提示注入目录骨架块（`catalog_skeleton`） |
| `backend/app/generation/engine.py` | 生成 / 流式生成 / 计划预览均注入骨架 |
| `backend/app/routers/generation.py` | 计划预览传 `catalog_skeleton` |

未改动：`backend/app/retrieval/anchor.py`、`backend/app/style/narrative.py`、蒸馏侧代码、
`docs/five-level-catalog-design.md`。本轮无 Git 提交、无分支切换。

---

## 二、两套数字的差异来源

设计稿 §5.12.1 / §5.12.6 的样例与本轮现行档案不是同一代数据，**不做硬凑**，以当前档案为准。

| 指标 | 设计稿样例 | 本轮现行（distill_id=4） |
| --- | --- | --- |
| 规则总数 | 未单列 | 29,013 |
| 建卡总数 | 17,482 | **27,015** |
| L1 分布 | style 16,068 / world 1,414 | style 25,244 / world 1,496 / voice 275 |
| 硬过滤丢弃 | 未单列 | 1,997 |
| 骨架字数 | 995 | **1,937** |

差异来源：

1. **档案代次不同**：设计稿数字取自更早一代蒸馏档案（卡片量级约 1.7 万），
   本轮 `style_distills id=4` 现有 29,013 条规则，规则基数本身更大。
2. **硬过滤条件不同**：本轮撤销了「文笔量化审计」的硬过滤豁免，metric 族
   635 条全部不进卡库；同时 `voice` 族进入卡库（275 条），设计稿样例没有该族。
   高频场景模板 3,395 条保留在卡表中但标 `deprecated`，仍计入建卡总数，
   不计入 active 卡与骨架。
3. 骨架字数随路径数与计数增长（2,200 字上限内未截断），但三节结构与设计稿一致。

---

## 三、八条裁决的落地与实测

### 1) 总数差异：以当前档案为准

- 现行建卡：**27,015** 张；硬过滤丢弃 **1,997** 条；跳过 `文风漂移分析` 1 条。
- 档案规则 29,013 = 建卡 27,015 + 丢弃 1,997 + 跳过 1，账能对上。
- 两套数字差异来源见上一节，不反向修改设计稿。

### 2) 场景命中率 536 / 15,243：维持现状

- `SCENE_MIN_SCORE` 未提高；章级兜底未关闭；有证据支撑的归属优先于笼统「跨场景」，
  跨场景查询走 `search`。
- `style/dialogue` 的 2,078 条场景分布（按卡上 `scene_tags`）：

| 场景 | 条数 | 场景 | 条数 |
| --- | ---: | --- | ---: |
| 无场景标签 | 651 | 赶路移动 travel | 55 |
| 群聊社交 groupchat | 307 | 救援脱困 rescue | 39 |
| 揭示真相 reveal | 296 | 宴会聚餐 banquet | 20 |
| 战斗对峙 combat | 210 | 误会冲突 misunderstand | 14 |
| 日常起居 daily | 197 | 亲密情感 intimacy | 12 |
| 修炼突破 cultivate | 169 | 夜谈独处 night | 12 |
| 交易买卖 trade | 81 | 审讯逼问 interrogate | 9 |
| | | 离别送行 farewell | 6 |

- 1,427 条恰有一个场景标签，0 条多标签；Top 5 = 1,179 条，占已标场景的 **82.6%**。
- 最集中的 groupchat 仅占已标场景 **21.5%**，没有集中在少数一两个场景。

### 3) technique 无场景路径：保留 `style/<l4>`

- 实现不变：有场景 `style/<场景>/<技法>`；structural 中间再插 `<节拍>`；
  无场景退 `style/<技法>`；L4 在 structural / technique / global 三种形状下都挂。
- 文档口径已写进 `catalog_paths.py` 模块注释；取 L2 必须走 `classify_path()`，
  不能按字符串第 2 段硬取（原因见第四节）。

### 4) 高频场景模板：保留卡片，统一 deprecated

- 3,395 条全部 `deprecated=true`，`deprecated_reason=设计稿 §8.2：逐章剧情摘要，
  检索默认不返回，rule 可回查`。
- `browse / search / similar / skeleton` 默认只用 active 卡；`rule` 按 id 仍可拿到全文。
- 建卡后 active 卡 **23,620** 张。
- `search` 命中数变化（k=5）：

| 查询 | 标记前 | 标记后 | 标记后命中里 deprecated 数 |
| --- | --- | --- | ---: |
| 群聊 新人 欢迎 | 5 条（含 2 条模板卡） | 5 条 | 0 |
| 战斗 招式 对峙 | 5 条（含 1 条） | 5 条 | 0 |
| 交易 讨价还价 | 5 条（含 1 条） | 5 条 | 0 |
| 修炼 突破 灵气 | 5 条（含 3 条） | 5 条 | 0 |

  `k=5` 仍返回 5 条、`fallback_count=0`；候选池从 27,649 降到 **23,620**，
  变化体现在「命中来源」而不是「命中条数」。
- deprecated 回查验证：`status=ok`、`deprecated=true`、正文 159 字、原因字段正常。

### 5) metric 族：撤销硬过滤豁免

- `build_cards.py` 删除 `METRIC_EXEMPT_DIM` 与 `metric_exempt` 逻辑，metric 与
  style 走同一套硬过滤。
- 重建结果：**建卡 27,015 / 硬过滤 1,997 / `by_l1` 中 metric 消失**。
  metric 族 635 条全部进入 `hard_filtered_by_dim["文笔量化审计"]`。
- `_render_audit_block` 未动，量化审计仍按固定块注入。
- 备注：撤销豁免前的上一版建卡为 27,649；本次净减 634，其中 635 条为 metric 卡，
  另有 1 条 `人物描写手法` 卡因档案/打分侧变化新增（本轮未改 `narrative.py`）。

### 6) L4 只认 8 类：其余进「未细分」

- L4 词表仍为 8 类：dialogue / action / psych / environment / pacing / pov /
  infocontrol / rhetoric；未扩表。
- 「未细分」共 **3,952** 条：technique 1,151 / global 1,687 / structural 1,114。
- 其余未分配：L2「未挂场景」656 条（technique 536 / structural 120）；
  L3「未分节拍」9 条。

### 7) 骨架缓存：`skeleton_4.json` + `distill_id + catalog_version`

- 缓存文件：`backend/data/catalog/skeleton_4.json`；文件内带
  `distill_id / catalog_version / built_at / text / stats`。
- 读缓存时校验 `catalog_version`，卡表版本变化自动失效重建。
- **待办：后续把骨架缓存迁到 DB 的 `style_narrative['catalog_skeleton']`**，
  文件缓存只作为当前过渡方案。

### 8) 结构节压缩：Top 3 展开 + 其余压行

- structural 共 15 组：Top 3 展开 L3（战斗对峙 993 / 修炼突破 688 / 群聊社交 472）。
- **被压行的 12 组**：赶路移动 256、交易买卖 198、未挂场景 120、日常起居 83、
  揭示真相 75、离别送行 44、误会冲突 37、亲密情感 32、救援脱困 18、夜谈独处 17、
  审讯逼问 16、宴会聚餐 14。
- 压行后结构节 **880 字**（正文 873 字，34 行）。
- 骨架整体：**1,937 字**，未触发 2,200 字上限、无截断；
  分节字数：通用 196 / 结构 880 / 技法 805。
- 注入系统提示净增 **1,984 字**（1,937 + 47 字包裹）。

---

## 四、`trace._l2()` 修复

**问题**：旧实现按 path 字符串第 2 段取 L2。`style/dialogue` 的第 2 段是 L4 技法，
会被误当成 L2 场景；`global` 卡只有两段，同样会被误判，导致跨场景软档判错。

**修复**：`trace._l2()` 改为调用 `catalog_paths.classify_path(path)`，
每个 token 只在对应的 L1/L2/L3/L4 词表里认领，认不出就留空；层级不再依赖段位置。

验证：

| path | `_l2()` |
| --- | --- |
| `style/dialogue` | `None` |
| `style/combat/climax/action` | `combat` |
| `style/combat/action` | `combat` |
| `world/entry` | `None` |
| `style/pacing` | `None` |

同类修正也用于卡表索引与深度统计：`catalog_store` 用卡片结构化字段
`l1/l2/l3/l4` 建索引，`build_cards` 的 `depth_distribution` 按实际挂到的层级算，
不再按 path 段数算。

---

## 五、当前卡库核对数字

- catalog_version：`b2748cd6f478`；`path_schema_version=paths-v2`；`score_version=3`。
- 建卡耗时 258.55 秒；落盘约 54.8 MB；单卡最长 266 字，无卡面截断。
- `by_shape`：technique 15,244 / global 6,937 / none 1,771 / structural 3,063。
- `by_dim`：潜台词模式 3,750、高频场景模板 3,395、节拍序列 3,063、人物描写手法 2,680、
  对白风格 2,596、剧情编排模式 1,758、伏笔与悬念设计 1,754、段落编排 1,629、
  场景构建 1,569、节奏控制 1,558、世界观呈现 1,496、情绪表达 1,492、角色声音词典 275。
- `depth_distribution`：L4 21,163 / L2 1,111 / L1 3,627 / L3 1,114。
- 路径数 485；最大路径 `style/dialogue` 2,078；空路径 0。
- 场景来源：context 19,083 / chapter 5,255 / none 2,657 / rule_text 20。
- 硬过滤原因：三本书通用套话 1,028、模糊词 888、无证据 655、量化对账不通过 448、
  非人角色/无说话证据 81、无章号 12、规则过长 1、截断名/伪角色 1。

## 六、验证记录

- `python -m pytest tests/test_registry_invoke_whitelist.py -q`：2 passed。
- `python tests/test_v30_retrieval.py`：39 通过 / 0 失败。
- `browse(level=1)`：style 21,849 / world 1,496 / voice 275（均 active）。
- `browse(level=2, path="style")`：14 个场景域，另 `未挂场景` 562、`not_applicable` 6,937。
- `similar`：`style/dialogue` 种子返回 5 条非 deprecated、同 L4 命中；
  `by_l4["dialogue"]=5,324`，`by_l2["dialogue"]=0`（索引按结构化字段，未把 L4 当 L2）。

## 七、待办 / 边界

1. **骨架缓存迁 DB**：`backend/data/catalog/skeleton_4.json` → `style_narrative['catalog_skeleton']`。
2. L4 未细分 3,952 条暂不扩表；是否新增技法类目由设计侧决定。
3. 高频场景模板 3,395 条保持 deprecated；若后续要恢复，只需从 `DEPRECATED_DIMS` 移除并重建。
4. 设计稿 `docs/five-level-catalog-design.md` 未改，等待另一会话合并本注记中的口径。
