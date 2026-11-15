# 文风档案作用域核查（只读）

日期：2026-11-11　范围：`backend/app` 提示词组装代码 + `novel_dev.db`（只读查询）
未改代码、未重启服务、未跑生成。

## 一、结论速览

1. **角色对话没有接这套档案。** 它只渲染「知识图谱角色卡 + 情境 + 最近 8 轮对话」，system prompt 全文由 `_role_system_prompt` 写死 4 条规则，不含 `style_profile` / `style_narrative`，也不读 `reference_novel_ids`。
2. **原创与续写在文风档案上完全同一条路径、同一份内容。** 4 个 prompt 组装点全部无条件调用 `_style_profile` / `_style_narrative`，没有一处按 `continuation_novel_id` 分支。
3. **续写项目按向导路径甚至拿不到档案**：续写分支只写 `generation_length` 和 `continuation_novel_id`，从不写 `reference_novel_ids`；而档案的数据源就是 `reference_novel_ids`。
4. `active_novel_id` / `continuation_novel_id` 在提示词侧只通过 `book_for_project()` 影响 **KG 事实块**（物品持有、KG 切片），不影响文风档案。

## 二、角色对话路径：实际渲染了什么

入口与组装：

- [assistant_wizard.py](backend/app/routers/assistant_wizard.py#L1168-L1217) `role_chat()`：`_character_card()` → `_role_system_prompt()` → `get_assistant_provider().chat(system, user)`；落库 `kind="role_chat"`。
- [assistant_wizard.py](backend/app/routers/assistant_wizard.py#L1220-L1309) `_character_card()`：只查本项目 KG —— `KgEntity(kind="character")` 的 `canonical_name`/`aliases`，取 `attrs` 的 `identity / faction / power_level / current_goal / arc / secrets`（各截 300 字）、最近 8 条 `KgCharacterState`、最多 10 条 `KgRelation`；查不到时退回 `KgNode` 的「姓名 + 提及次数」，或空串。
- [assistant_wizard.py](backend/app/routers/assistant_wizard.py#L1312-L1326) `_role_system_prompt()`：system 全文＝「你在扮演「X」+ 当前情境 + 人物卡 + 4 条规则（第一人称 / 只用卡里事实 / 200 字内 / 不输出括号旁白）」。
- user prompt：`body.history[-8:]` 拼成「用户：/角色：」对话记录 + `用户说：{message}`。

没有 `style_profile` / `style_narrative` / `style_dims` 的任何读取。角色卡查询也只按 `project_id`，**没有 `book_for_project()` 收窄**——合并图谱里同名角色会命中哪一本，代码不区分。

## 三、原创 vs 续写：同一条路径

4 个组装点，参数顺序一致：

| 位置 | 用途 | 传入风格档案 |
|---|---|---|
| [engine.py#L1560-L1567](backend/app/generation/engine.py#L1560-L1567) | 同步生成 | `_style_profile()` + `_style_narrative()` |
| [engine.py#L1745-L1752](backend/app/generation/engine.py#L1745-L1752) | 流式生成 | 同上 |
| [engine.py#L1929-L1937](backend/app/generation/engine.py#L1929-L1937) | 重生成 | 同上 |
| [generation.py#L208-L211](backend/app/routers/generation.py#L208-L211) | 生成前预检 | 同上 |

渲染位置：[prompts.py#L282-L318](backend/app/generation/prompts.py#L282-L318)（`_profile_block` → `【文风档案】`，`_narrative_block` → `【叙事档案】`/`【反向禁忌】`）。

- 四处都是 `build_system_prompt(project.title, ...)`，开头固定为「你是一位长篇小说续写助手」——原创项目也用这个词。
- 全仓库没有任何 `if continuation_novel_id:` 分支影响 prompt 内容（`grep active_novel_id|continuation_novel_id` 的全部命中见第五节）。
- 档案数据源：[pipeline.py#L173](backend/app/style/pipeline.py#L173) 与 [narrative.py#L4131](backend/app/style/narrative.py#L4131) 都只读 `project.reference_novel_ids`。
- 向导写侧：[assistant_wizard.py#L958](backend/app/routers/assistant_wizard.py#L958) 原创分支写 `reference_novel_ids`；[#L989-L1003](backend/app/routers/assistant_wizard.py#L989-L1003) 续写分支**只写 `generation_length` 与 `continuation_novel_id`**。

## 四、维度对照（数据取项目 5，`source_ids=[5,6]`）

项目 5 档案：`style_profile` JSON 30,712 字节（渲染 254 字）＋ `style_narrative` JSON 126,214 字节（渲染 6,022 字），两块的 96% 是叙事档案。参考语料＝《大奉打更人.txt》（5,659,842 字）＋《豪门宠婚，爵少你别闹.txt》（1,745,727 字），蒸馏采样 60 章、`tier=balanced`。

| 维度 | 原创项目是否对症 | 续写（续《大奉》）是否对症 | 依据（规则里的证据句 → 原文位置） |
|---|---|---|---|
| 角色声音词典（39 条） | 错配 | 部分对症 | 23 条是《大奉》角色（许七安/怀庆/魏渊/临安/元景帝…），**16 条是《豪门宠婚》角色**（宫爵/安小落/莫泽修/何萱萱/郑楚桥…）；「宫爵」命中 novel 6 第 1 章。另有 ≥9 条是截断/职务词而非人名（许银锣、许二郎、楚元、云州、司天、慕南、公主、王妃、武夫），`voice_name_audit.suspicious=["临安","楚元"]` |
| 剧情编排模式（11 条） | 错配 | 部分对症 | 证据句「许七安看了眼吕青，怒道…」→ 《大奉》第 130 章；「魏渊不忘给他灌输知识」 |
| 伏笔与悬念设计（14 条） | 错配 | 部分对症 | 证据句「许七安？！数千名巫师齐刷刷扭头…」「楚元缜投出飞剑，目标不是佛陀，而是神殊」——均为《大奉》人物关系 |
| 人物描写手法（11 条） | 错配 | 部分对症 | 「许七安低眉顺眼」「怀庆玉雕般的脸庞…」 |
| 对白风格（9 条） | 错配（方法可迁移） | 部分对症 | 「许七安摆摆手：这几天辛苦各位了，本官从不会亏待同僚」「李妙真大怒：你才是天宗败类」 |
| 潜台词模式（25 条） | 错配 | 部分对症 | 「戚广伯…为帅者既要明白慈不掌兵…」→ 《大奉》第 665 章；「许七安…与佛陀战于雷州边界」 |
| 高频场景模板（34 条） | 错配 | 部分对症 | 「战场：城头火炮轰鸣…戚广伯手持单筒望远镜」「宴会：…李妙真打量巡抚」——《大奉》设定；同时混入豪门晚宴/董事会类模板 |
| 节拍序列（35 条） | 错配 | 弱对症 | 「敲门声吵醒，柴杏儿的声音透着恼怒…」→ 《大奉》第 545–546 章具体桥段；这是已写章节的节拍，不是可复用模式 |
| 世界观呈现（9 条） | 错配 | 对症 | 「魏渊…任何一部法相图，都是价值连城的」「七种蛊术…天蛊老的培养」——《大奉》专有体系 |
| 场景构建（10 条） | 错配 | 对症 | 「元景帝赏赐的黄金和绸缎」「大理寺丞…城墙阵纹」「柴府…祠堂」 |
| 情绪表达（8 条） | 弱对症 | 对症 | 规则本身抽象（动作/对白承载情绪），但证据句全来自《大奉》 |
| 节奏控制（9 条） | 弱对症 | 对症 | 「轰轰轰！」「许七安看了眼吕青…」——短句推进，方法可迁移 |
| 文笔量化审计（6 条） | 对症 | 对症 | 纯数字（句长 31.2±20.7、短句 3.3%、对白 21.1%…），不绑人名 |
| 文风漂移分析（1 条） | 弱对症 | 弱对症 | 描述作者前后期变化；续写时参考价值取决于续写点落在哪个时期 |
| 【文风档案】统计块（254 字） | 对症 | 对症 | 句长 21.3±3、对白 28%、四字格 8.9/千字等；但它是两本书合并后的统计 |

## 五、`active_novel_id` / `continuation_novel_id` 在提示词侧的读取点

唯一取值点：[book_scope.py#L16-L31](backend/app/kg/book_scope.py#L16-L31)（`continuation_novel_id` → `active_novel_id` → `None`）。真正影响提示词的调用：

| 位置 | 影响哪块 prompt |
|---|---|
| [engine.py#L734](backend/app/generation/engine.py#L734) | `_active_item_entries`：`kg_mentions` 按书过滤 → 物品清单 |
| [engine.py#L773](backend/app/generation/engine.py#L773) | 物品「现在在谁手上」事实块 |
| [retrieval_v3.py#L60](backend/app/kg/retrieval_v3.py#L60) | KG 切片（实体/关系/状态/伏笔/事件） |

非提示词读取：`batch_worker.py:348`（后台抽图）、`assistant_wizard.py:958/968/969/993`（向导写侧）、`projects.py:475-492`（PATCH 校验）、`schemas.py:101-102/131-132`、前端 `ProjectWorkspace.vue:465-468/818-819`。

**结论**：这两列从未参与「文风档案要不要注入 / 注入什么」的决策。

## 六、能确证 / 不能只靠读代码判断

**能确证（代码或库内数据直接可见）**

- 角色对话不注入任何文风档案，只有 KG 角色卡 + 情境 + 8 轮历史；且不按书过滤角色。
- 原创/续写共用 4 个组装点，同一份档案、同一段固定文案（「你是一位长篇小说续写助手」）。
- 档案数据源是 `reference_novel_ids`；续写向导分支不写这个字段。
- 现实库里没有任何项目设了 `continuation_novel_id`（5 个项目全为 NULL；项目 5 是 `active_novel_id=5`、原创＋双参考书）。
- 项目 5 的角色声音词典 39 条中 16 条来自另一本书，且多条是截断/职务词。

**不能靠读代码判断**

- 模型实际是否遵循这些规则、遵循到什么程度（需同一前提的 A/B 生成）。
- 续写项目在真实使用中会不会被手工设上参考语料（决定档案到底出不出场）。
- 若把档案接进角色对话，是否真能更「像本人」（需对照实验；当前无任何数据）。
- 每条规则的实际来源书：蒸馏输入是两本，规则本身不标来源；本报告只对能反查到原文的 4 句做了核对。
