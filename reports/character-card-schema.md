# 角色卡字段规范（Character Card Schema）

> 适用系统：AI 长篇小说生成系统 · 中文仙侠 / 武侠向
> 版本：v0.1（调研稿，2026-11-08）
> 用途：作为「角色卡」补全的工程对照清单，可直接转 DDL / Pydantic / 抽取 prompt
> 现状基准：`kg_entities` / `kg_relations` / `kg_events` / `kg_character_state` / `kg_entity_knowledge`

---

## 0. 结论摘要

1. **现有系统的骨架是对的，缺的是两个"对象型"模块**：已有的人物实体、关系、事件、按章状态可以承载「基本信息 / 身份社会关系 / 经历时间线」；真正缺的是 **能力体系（功法、武技、技能、效果）** 与 **装备道具（武器、法器、防具、消耗品）**。这两块不能用 `attrs` 塞字符串解决，必须建独立表。
2. **四类外部规范各自只覆盖一部分，不能照搬任何一家**：
   - TRPG / D&D 给的是「属性—技能—专长—装备—法术位」的**数值化骨架**，强在"可校验"，弱在中文仙侠的品阶语义；
   - 游戏角色面板给的是「属性 / 技能树 / 装备栏槽位 / 天赋」的**槽位化结构**，强在 UI 与库存建模；
   - SillyTavern v2/v3 给的是「人设文本 + 世界书 + 版本信封」的**叙事化骨架**，强在 prompt 注入与版本兼容；
   - 中文人物设定卡 / 人物小传给的是「表里目标、弧光、底线、称呼、秘密」的**解释型字段**，强在写得像人，弱在不可校验。
3. **必须区分"事实型"和"解释型"两类字段**，并在库里物理分开：事实型进 `canon` 层，只读；解释型进 `inferred` 层，允许随剧情重算。混在一张表里，后续 judge / conflict_scan 会同时误报和漏报。
4. **功法/技能必须有独立的 `effect` 子结构**。只写一句"威力极大"没有工程价值；至少要能拆成 `效果类型 / 目标范围 / 量级 / 持续 / 触发条件 / 反噬`，否则生成时无法判断"这一招能不能解决当前局面"。
5. **推荐落地为 5 张新表**：`technique`、`technique_effect`、`character_technique`、`item`、`character_item`。它们都通过 `entity_id` 挂到现有 `kg_entities` 上，不破坏现有六类 kind 与 `PROTECTED_ATTRS` 机制。

---

## 1. 调研来源与取值口径

| 来源 | 提供什么 | 本规范采用的点 |
|---|---|---|
| D&D 5e 角色卡（D&D Beyond 官方规则与角色卡分区） | 六项属性与调整值、豁免、技能熟练、专长、装备、护甲等级、生命值、生命骰、法术位 / 已知法术 / 准备法术 | 属性—技能—装备—法术位的**分层**；「熟练 / 专精」两级掌握度；法术的**环位 / 消耗**概念 |
| 游戏角色面板（MMORPG 角色信息窗口） | 等级、职业、HP/MP、基础属性、装备槽位（头/身/手/脚/主手/副手/披风/饰品）、技能点、天赋树 | **装备槽位枚举**；「等级作为学习前置」；天赋作为独立于技能的被动加成 |
| SillyTavern 角色卡 v1 / v2 | `name` `description` `personality` `scenario` `first_mes` `mes_example`，v2 增加 `creator_notes` `system_prompt` `post_history_instructions` `alternate_greetings` `character_book` `tags` `creator` `character_version` `extensions` | **规范名 + 别名/称呼**、**开场状态**、**示例台词**、**世界书条目（键触发）**、**版本化信封** |
| SillyTavern 角色卡 v3（chara_card_v3） | v2 全部字段 + `nickname`、`assets`（立绘/音频）、`group_only_greetings`、`creator_notes_multilingual`、`source`、创建/修改时间、`use_regex` / `decorators` 等世界书扩展 | **多语言/多称呼**、**资产引用**、**字段级版本与来源** |
| 中文人物设定卡 / 人物设定表（24 栏版） | 姓名别名、年龄性别、身高体格、所属、发/眼/肤、穿着倾向、辨识点、性格三词、价值观底线、长处、短处、害怕的事、表/里目标、口头禅、出身、家庭成员、决定性过去、秘密（谁不知道）、主要关系、敌对心结、故事功能、开场→结尾状态、一句话概括 | **辨识点**、**长处即短处**、**表/里双层目标**、**称呼矩阵**、**故事功能**、**秘密的知情范围** |
| 英文写作向 Character Profile / Character Arc 模板 | want vs need、flaw、wound、belief、arc 的起点信念→终点信念→转折点→牺牲 | **人物弧光的结构化三元组**；**want/need 分离** |
| 仙侠 / 修真设定资料 | 境界阶梯（练气→筑基→金丹→元婴→化神…）、每境四小阶（初/中/后/巅峰）、功法品阶（天/地/玄/黄 × 高/中/初；或 凡诀/灵诀/宝诀/法诀/道典/圣典）、丹药九阶 × 品质（废/劣/次/正/精/无暇）、丹器符阵"修仙四艺"、灵根/血脉 | **境界 = 体系 + 大境界 + 小境界**三段式；**品阶必须是枚举而非自由文本**；**丹药区分阶与品质**；**副职业技艺单列** |
| 武侠设定通例 | 内功 / 外功 / 轻功 / 招式 / 兵器 / 暗器 / 点穴 / 毒 | 功法按「内功 / 外功 / 身法 / 招式」分型；武器按「长兵 / 短兵 / 软兵 / 暗器 / 弓弩」分型 |

> 说明：中文网文各家的品阶命名并不统一（有的用"天地玄黄"，有的用"凡灵宝法道圣"），因此**品阶字段必须是 `enum` + `grade_system_id` 双字段**，绑定到某个体系，不能只存一个字符串。

---

## 2. 类型约定

本清单中的数据类型统一按下列写法：

| 类型 | 含义 | 落库建议 |
|---|---|---|
| `text` | 自由文本 | `TEXT` |
| `enum(...)` | 封闭枚举，非法值必须进 `rejected` 而不是静默丢弃 | `VARCHAR(32)` + 应用层校验 |
| `int` / `float` | 数值 | `INTEGER` / `REAL` |
| `bool` | 布尔 | `BOOLEAN` |
| `ref(kind)` | 外键指向 `kg_entities.id`，`kind` 为其类型 | `INTEGER FK` |
| `list<T>` | 数组 | `JSON` |
| `map<K,V>` | 键值对象 | `JSON` |
| `versioned<T>` | **版本化标量**：随章节变化的值 | 独立历史表，见下 |
| `derived` | 由其它字段计算得出，不落库 | 视图 / 查询时算 |

**`versioned<T>` 的标准展开**（这是整套规范的关键，所有"会变"的字段都用它）：

```jsonc
[
  {
    "value": "筑基中期",
    "from_chapter": 41,
    "to_chapter": 88,          // null = 至今有效
    "source": "canon",          // canon | generated | inferred
    "confidence": 0.95,
    "evidence": { "chapter": 41, "quote": "他已筑基中期，只差一步便可结丹" }
  }
]
```

**性质标记**：

- `F` = 事实型，可校验。原文能直接找到依据，冲突时以 `canon` 为准，`generated` 不得覆盖。
- `I` = 解释型，会随剧情演进。由模型归纳或作者设定，允许重算，必须带 `confidence` 且不得反写 `canon`。

**来源标记**（对应第 6 节）：

- `KG` = 现有知识图谱可直接抽取 / 已有字段
- `TBL` = 需要新建独立设定表
- `LLM` = 需要模型归纳生成

---

## 3. 分类字段清单

### 3.1 基本信息

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `character_id` | 内部主键，指向 `kg_entities.id` | `int` | F | KG |
| `canonical_name` | 规范名（全书最常用称呼） | `text` | F | KG |
| `aliases` | 别名、道号、绰号、化名、曾用名 | `list<text>` | F | KG |
| `alias_usage` | 谁在什么场合怎么称呼他（含敬称/贬称/私称） | `list<{term, by_id, register, since_chapter}>` | F | KG + TBL |
| `gender` | 性别 | `enum(male, female, unknown)` | F | KG |
| `species` | 族属：人 / 妖 / 魔 / 仙 / 鬼 / 灵 / 半妖 | `enum` | F | KG + TBL |
| `age` | 实际年龄 | `versioned<int>` | F | TBL |
| `apparent_age` | 外貌年龄（修仙者常见"看着二十，实际三百"） | `versioned<int>` | F | TBL |
| `lifespan_limit` | 当前境界对应的寿元上限（年） | `versioned<int>` | F | TBL |
| `birth_place` | 出生地 / 出身地 | `ref(location)` | F | TBL |
| `birth_time` | 出生或入世时间（故事内纪年） | `text` | F | TBL |
| `alive_status` | 生死状态 | `versioned<enum(alive, dead, missing, sealed, reincarnated, ascended)>` | F | KG |
| `narrative_role` | 故事功能：主角 / 对立者 / 引路人 / 盟友 / 镜子 / 催化剂 / 调剂 | `enum` | I | LLM |
| `one_liner` | 一句话概括（书腰体） | `text` | I | LLM |
| `first_chapter` | 初登场章 | `int` | F | KG |
| `last_chapter` | 最后出场章 | `int` | F | KG |
| `mention_count` | 提及次数（热度权重） | `int` | F | KG |

### 3.2 身体与外貌

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `appearance_summary` | 外貌总述（可写进 prompt 的一段话） | `versioned<text>` | I | LLM |
| `height_cm` | 身高 | `versioned<int>` | F | KG |
| `build` | 体格：瘦削 / 匀称 / 魁梧 / 佝偻 | `enum` | F | KG |
| `hair` | 发色、发型、发质 | `versioned<text>` | F | KG |
| `eyes` | 瞳色、瞳形、异象（金瞳/重瞳/竖瞳） | `versioned<text>` | F | KG |
| `skin` | 肤色、肤质、异象 | `versioned<text>` | F | KG |
| `distinguishing_marks` | 辨识点：疤、痣、胎记、断指、纹身、残缺 | `list<{desc, body_part, cause_event_id, since_chapter}>` | F | KG |
| `attire_style` | 穿着倾向（"挑衣服先顾什么"，而非具体衣服名） | `text` | I | LLM |
| `appearance_changes` | 外貌突变：毁容、断臂、白发、妖化、返老还童 | `list<{change, chapter, cause_event_id}>` | F | KG |
| `body_condition` | 身体状态：伤、毒、咒、残、经脉受损、走火入魔 | `list<{type, desc, severity, since_chapter, recovered_chapter}>` | F | KG（复用 `kg_character_state.injury`） |
| `constitution` | 体质 / 根骨：凡体、灵体、道体、剑体、魔体、混沌体 | `enum` | F | TBL |
| `spiritual_root` | 灵根：属性、品级、纯度、觉醒章节 | `list<{element, grade, purity, awakened_chapter}>` | F | TBL |
| `bloodline` | 血脉 / 传承：名称、品级、来源、觉醒章节 | `list<{name, grade, origin_ref, awakened_chapter}>` | F | TBL |
| `special_senses` | 特殊感官：神识、听声辨位、望气、夜视 | `list<text>` | F | KG |
| `stats` | 基础属性表（力量/敏捷/根骨/悟性/神识/气运），可空 | `map<stat, {value, scale, evidence}>` | F / I | TBL |

> 说明：`stats` 对小说系统是**可选项**。原文大多不给数值，只有"他力气极大"这类相对描述；此时应存 `derived_rank`（如"同辈前五"），而不是编造数值。

### 3.3 身份与社会关系

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `current_identity` | 当前身份与官职（含 `office_type`：职官/散衔/泛称） | `versioned<{office, office_type, org_id}>` | F | KG（现有 `attrs.office` / `attrs.office_type`） |
| `identity_tags` | 身份标签：皇子、刑部郎中、外门弟子、散修 | `list<text>` | F | KG |
| `faction_id` | 当前所属势力 | `versioned<ref(faction)>` | F | KG |
| `rank_title` | 势力内位阶 / 职位 / 辈分 | `versioned<text>` | F | KG |
| `social_class` | 社会阶层：皇室 / 官宦 / 世家 / 寒门 / 江湖 / 散修 | `enum` | F | TBL |
| `affiliation_history` | 归属变更史：入派、叛门、逐出、自立 | `list<{org_id, role, from_chapter, to_chapter, exit_reason}>` | F | TBL |
| `reputation` | 外界名声 / 名号 / 通缉等级 | `versioned<text>` | I | LLM |
| `relations` | 关系边集合 | `list<ref(kg_relations)>` | F | KG |
| `relation_matrix` | 称呼矩阵：谁怎么叫他，公开/私下两种 | `list<{from_id, to_id, term, register, public, since_chapter}>` | F | KG + TBL |
| `lineage` | 血缘家族与亲属 | `list<{character_id, kinship, alive_status}>` | F | KG |
| `master_disciple` | 师承：师父、弟子、门派、拜师章 | `list<{master_id, disciple_ids, sect_id, since_chapter}>` | F | KG + TBL |
| `known_secrets` | 他已知的秘密（防"预知外挂"） | `list<ref(kg_entity_knowledge)>` | F | KG |
| `public_persona` | 对外人设（表） | `text` | I | LLM |
| `true_identity` | 真实身份 / 隐藏身世（里） | `text` | I | LLM |
| `leverage` | 别人能拿捏他的把柄 / 软肋 | `list<text>` | I | LLM |

### 3.4 能力体系

能力体系分四层：**境界（刻度）→ 功法（体系）→ 技能/招式（动作）→ 效果（结果）**。前两层描述"他有多强"，后两层描述"他能做什么"。

#### 3.4.1 境界与天赋

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `realm_system_id` | 所属境界体系（修仙 / 武道 / 炼气 / 妖修），指向 `power_system` | `ref(power_system)` | F | KG |
| `realm` | 当前大境界 | `versioned<text>` | F | KG（现有 `attrs.realm`） |
| `realm_stage` | 小境界：初期 / 中期 / 后期 / 巅峰 / 圆满 | `versioned<enum>` | F | TBL |
| `realm_progress` | 突破进度描述（"只差一线"） | `versioned<text>` | I | LLM |
| `power_rank` | 战力档位（同辈前列 / 越阶而战 / 一方巨擘） | `versioned<enum>` | I | LLM |
| `talents` | 天赋 / 被动特性：名称、品级、效果、来源 | `list<{name, grade, effect, source_chapter}>` | F | TBL |
| `combat_style` | 战斗风格：刚猛、诡谲、消耗、守御、以势压人 | `text` | I | LLM |
| `signature_move` | 成名绝技 | `ref(technique)` | F | KG |
| `combat_weakness` | 战斗层面弱点：怕近身、耗不起、神识弱、旧伤 | `list<{type, desc, exploit}>` | I | LLM |

#### 3.4.2 角色持有的功法 / 技能

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `techniques` | 已持有功法/技能引用 | `list<ref(technique)>` | F | TBL |
| `technique_mastery` | 每部功法的掌握度与起始章 | `list<{technique_id, level, since_chapter}>` | F | TBL |
| `skills` | 非修炼类技能：医术、追踪、骑术、易容、毒理、机关 | `list<{name, level, source_chapter}>` | F | TBL |
| `professions` | 副职业技艺：炼丹 / 炼器 / 阵法 / 符箓 / 御兽 / 医术（"修仙四艺"） | `list<{craft, level, grade}>` | F | TBL |
| `technique_conflicts` | 功法冲突 / 相克 / 不可兼修 | `list<{a_id, b_id, reason}>` | F | TBL |
| `forbidden_arts` | 禁术 / 魔功（标记为不可在正文随意使用） | `list<ref(technique)>` | F | TBL |

#### 3.4.3 功法 / 技能主表（必须的结构化子结构）

见第 4 节完整定义。最小必备字段如下（对应需求中列出的 10 项）：

| 必备字段 | 含义 | 类型 | 性质 |
|---|---|---|---|
| `name` | 功法/技能名称 | `text` | F |
| `type` | 类型枚举 | `enum` | F |
| `grade` | 品阶 / 等级 | `versioned<enum>` | F |
| `source_type` | 来源：门派传承 / 家族 / 师授 / 自创 / 奇遇 / 遗迹 / 禁术 / 交易 | `enum` | F |
| `effect` | 效果（结构化 + 文本双写） | `list<effect>` + `text` | F（机制部分）/ I（描述部分） |
| `activation` | 发动条件 | `list<text>` | F |
| `cost` | 消耗 | `list<{resource, amount, unit}>` | F |
| `limitations` | 限制 / 门槛 | `list<text>` | F |
| `backlash` | 反噬 / 代价 | `list<{effect, trigger, severity}>` | F |
| `mastery` | 掌握程度 | `versioned<enum>` | F |
| `first_chapter` | 首次出现章节 | `int` | F |
| `holders` | 持有者及各自掌握度 | `list<{character_id, mastery, since_chapter}>` | F |

### 3.5 装备与道具

#### 3.5.1 物品主表（`item`）

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `item_id` | 主键，指向 `kg_entities.id`（kind=`item`） | `int` | F | KG |
| `name` / `aliases` | 名称与别名（本名 / 俗称 / 器灵称呼） | `text` / `list<text>` | F | KG |
| `category` | 大类：武器 / 法器 / 防具 / 饰品 / 丹药 / 符箓 / 阵材 / 消耗品 / 秘籍 / 信物 / 坐骑 / 储物 / 货币 | `enum` | F | TBL |
| `subcategory` | 细类：刀 / 剑 / 枪 / 鞭 / 弓 / 扇 / 钟 / 印 / 鼎 / 塔 / 幡 / 珠 / 镜 / 铃 / 甲 / 袍 / 靴 | `enum` | F | TBL |
| `grade` | 品阶（法宝：凡器/灵器/宝器/道器/仙器；丹药：九阶） | `versioned<enum>` | F | TBL |
| `grade_system_id` | 品阶所属体系（不同流派刻度不同） | `ref(power_system)` | F | TBL |
| `quality` | 品质细分（丹药：废/劣/次/正/精/无暇） | `enum` | F | TBL |
| `material` | 材质 / 炼制主料 | `text` | F | TBL |
| `crafter_id` | 炼制者 | `ref(character)` | F | TBL |
| `origin_id` | 出处（遗迹 / 门派 / 秘境） | `ref(location / faction)` | F | TBL |
| `effects` | 效果列表（复用技能 effect 结构） | `list<effect>` | F / I | TBL |
| `requirements` | 使用条件：境界门槛、血脉、认主、灵力注入 | `list<text>` | F | TBL |
| `durability` | 耐久 / 剩余威能 | `versioned<int \| enum>` | F | TBL |
| `item_state` | 状态：完好 / 受损 / 损毁 / 封印 / 沉睡 / 进化中 | `versioned<enum>` | F | KG + TBL |
| `bound_to` | 认主绑定者 | `versioned<ref(character)>` | F | TBL |
| `current_location` | 当前所在（随身 / 某地 / 某人之手） | `versioned<ref(location) \| ref(character)>` | F | KG |
| `ownership_history` | 流转史：谁在第几章如何得到 / 失去 | `list<{owner_id, from_chapter, to_chapter, how, cause_event_id}>` | F | TBL |
| `significance` | 剧情意义（伏笔物 / 身份凭证 / 钥匙） | `text` | I | LLM |
| `first_chapter` / `last_chapter` | 首末出现章 | `int` | F | KG |

#### 3.5.2 装备栏与库存（`character_item`）

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `slot` | 装备槽位 | `enum(main_hand, off_hand, head, body, waist, feet, cloak, accessory_1, accessory_2, bonded_artifact, storage, mount)` | F | TBL |
| `equipped_item_id` | 当前装备 | `versioned<ref(item)>` | F | TBL |
| `equipped_since` | 装备起始章 | `int` | F | TBL |
| `inventory` | 随身物品清单 | `list<{item_id \| item_name, qty, unit, obtained_chapter, consumed_chapter}>` | F | TBL（替代现有 `inventory` 文本） |
| `currency` | 货币 / 灵石 / 贡献点 | `versioned<map<currency, amount>>` | F | TBL |
| `consumables_used` | 已消耗丹药 / 符箓记录（防"无限回血"） | `list<{item_id, chapter, effect_applied}>` | F | TBL |
| `recipes` | 掌握的丹方 / 器方 / 阵图 | `list<ref(item)>` | F | TBL |
| `lost_items` | 遗失 / 被夺物品及章号 | `list<{item_id, chapter, how, recovered_chapter}>` | F | TBL |

> 注意：`kg_character_state.inventory` 目前是 `String(500)` 文本，只能当"快照摘要"用。要支撑"第 37 章丢了本命剑、第 52 章夺回"这种连续性，必须换成上面的结构化表。

### 3.6 目标与动机

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `surface_goal` | 表层目标（他自己会说出口的） | `versioned<text>` | I | LLM |
| `hidden_goal` | 里层目标（他自己都未必承认的） | `text` | I | LLM |
| `need` | 真正需要（与 want 相对，弧光的核心） | `text` | I | LLM |
| `motivation` | 动机权重：复仇 / 求生 / 守护 / 求道 / 权力 / 情爱 / 赎罪 / 好奇 | `list<{type, weight}>` | I | LLM |
| `values` | 价值观与底线（"恩要还，债不欠"） | `list<text>` | I | LLM |
| `fears` | 恐惧（越不愿承认越好用） | `list<text>` | I | LLM |
| `flaw` | 缺陷 / 执念（最好是他长处的反面） | `text` | I | LLM |
| `wound` | 创伤性前史（"没有这件事就不是现在这样"） | `text` | I | LLM |
| `arc` | 人物弧光：起点信念 → 转折点 → 终点信念 → 牺牲 | `{start_belief, turning_points:[event_id], end_belief, sacrifice}` | I | LLM |
| `current_goal_state` | 当前目标进度 | `list<ref(kg_character_state)>`（`state_type=goal`） | F / I | KG |
| `inner_conflict` | 内在矛盾（两难选择的两端） | `text` | I | LLM |

### 3.7 经历时间线

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `timeline` | 按章经历：章号、事件、状态变化、地点 | `list<{chapter, segment_no, event_id, summary, state_delta, location_id, in_story_date}>` | F | KG（`kg_events` + `kg_mentions`） |
| `key_events` | 关键事件（战斗 / 死亡 / 揭示 / 抉择 / 感情里程碑） | `list<ref(kg_events)>` | F | KG |
| `state_snapshots` | 按章状态快照：位置 / 伤 / 情绪 / 目标 / 库存 / 生死 | `list<ref(kg_character_state)>` | F | KG |
| `knowledge_timeline` | 认知时间线：第几章知道了什么 | `list<ref(kg_entity_knowledge)>` | F | KG |
| `status_changes` | 身份 / 势力 / 境界的变更节点 | `list<{field, old, new, chapter, cause_event_id}>` | F | TBL |
| `offscreen_periods` | 未出场区间（再登场时需交代去向） | `list<{from_chapter, to_chapter, stated_reason}>` | F | TBL |
| `story_position` | 开场状态 → 结尾状态 | `{start, end}` | I | LLM |

### 3.8 语言风格与行为习惯

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `speech_style` | 说话风格：句长、语速、敬语、结论先行 | `text` | I | LLM |
| `register` | 语域：文雅 / 市井 / 江湖 / 官腔 / 稚气 / 古奥 | `enum` | I | LLM |
| `verbal_tics` | 口头禅、语气词、习惯性自称 | `list<{phrase, frequency, context}>` | I | LLM |
| `address_terms` | 称呼习惯（他怎么叫别人） | `list<{target_id, term, register}>` | F | KG + TBL |
| `sample_dialogues` | 台词样例（直接进 prompt 的 few-shot） | `list<{quote, chapter, scene}>` | F | KG |
| `habits` | 行为习惯与小动作 | `list<text>` | I | LLM |
| `decision_pattern` | 决策模式：先算代价 / 先护人 / 先试探 | `text` | I | LLM |
| `moral_boundary` | 道德边界：绝不做什么 | `list<text>` | I | LLM |
| `fighting_habit` | 战斗习惯：起手式、撤退条件、是否留手 | `text` | I | LLM |
| `tell` | 情绪外显特征（生气时更安静 / 手指会敲桌） | `text` | I | LLM |

### 3.9 其他

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `secrets` | 秘密清单：内容、知情者、揭露章 | `list<{content, known_by:[character_id], reveal_chapter}>` | F | KG + LLM |
| `weakness` | 非战斗弱点：软肋、执念、身份把柄 | `list<{type, desc}>` | I | LLM |
| `do_not_write` | 禁止模型改写的 canon 设定（保护名单） | `list<text>` | F | TBL |
| `open_questions` | 尚未确认的设定（待作者拍板） | `list<text>` | I | LLM |
| `schema_version` | 角色卡结构版本 | `text` | F | TBL |
| `source_layer` | 数据层：canon / generated / inferred | `enum` | F | TBL |
| `confidence` | 整体置信度 | `float` | F | TBL |
| `evidence` | 证据句集合：章号 + 原文引文 | `list<{chapter, quote}>` | F | TBL |
| `created_at` / `updated_at` | 创建 / 更新时间 | `datetime` | F | TBL |

---

## 4. 「功法 / 技能」结构化子结构

### 4.1 类型划分（先定 `type`，再定字段）

| `type` | 中文 | 定义 | 典型字段侧重 |
|---|---|---|---|
| `cultivation_method` | 功法 | 修炼总纲，决定能修到哪个境界、提升哪些属性，长期生效 | `grade`、`realm_ceiling`、`attribute_gain` |
| `mental_method` | 心法 | 内功运行路线，决定灵力性质与走火入魔风险 | `meridian_path`、`qi_nature`、`backlash` |
| `martial_art` | 武技 / 招式 | 具体战斗动作，有起手、判定、消耗 | `activation`、`cost`、`range` |
| `spell` | 术法 | 施法型能力，需结印 / 口诀 / 媒介 | `components`、`cast_time`、`range` |
| `divine_ability` | 神通 | 高阶天赋能力，通常有严格前置 | `requirements`、`backlash` |
| `movement` | 身法 / 轻功 | 位移、闪避、追击 | `distance`、`duration` |
| `craft` | 技艺（副职业） | 炼丹 / 炼器 / 阵法 / 符箓 / 御兽 / 医术 | `craft_level`、`success_rate`、`materials` |
| `forbidden` | 禁术 / 魔功 | 威力大、代价重，需单独标记 | `backlash`、`moral_cost` |

### 4.2 `technique` 主表字段

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `technique_id` | 主键 | `int` | F | TBL |
| `name` | 名称 | `text` | F | KG |
| `aliases` | 别名 / 旧称 / 俗称 | `list<text>` | F | KG |
| `type` | 类型（见 4.1） | `enum` | F | TBL + LLM |
| `category` | 战斗定位：攻击 / 防御 / 辅助 / 治疗 / 控制 / 位移 / 探查 / 修炼 | `enum` | F | LLM |
| `grade` | 品阶 | `versioned<enum>` | F | TBL |
| `grade_system_id` | 品阶体系（天地玄黄 / 凡灵宝法道圣 / 上中下三品） | `ref(power_system)` | F | TBL |
| `tier` | 阶数（1–9，用于跨体系比较） | `int` | F | TBL |
| `element` | 属性：金木水火土 / 雷 / 冰 / 风 / 阴 / 阳 / 无 | `enum` | F | TBL |
| `realm_ceiling` | 该功法最高能修到的境界 | `text` | F | TBL |
| `source_type` | 来源：门派传承 / 家族 / 师授 / 自创 / 奇遇 / 遗迹 / 禁术 / 交易 | `enum` | F | TBL |
| `source_entity_id` | 来源实体（门派 / 师父 / 遗迹） | `ref(faction \| character \| location)` | F | KG |
| `acquisition_chapter` | 获得章节 | `int` | F | TBL |
| `effect_text` | 效果描述（叙事版，可进 prompt） | `text` | I | LLM |
| `effects` | 效果列表（结构化，见 4.3） | `list<effect>` | F / I | TBL |
| `activation` | 发动条件：结印 / 口诀 / 兵器 / 姿势 / 蓄力轮次 | `list<text>` | F | TBL |
| `cost` | 消耗：灵力 / 真气 / 气血 / 寿元 / 精神力 / 材料 / 法宝耐久 | `list<{resource, amount, unit}>` | F | TBL |
| `cast_time` | 施展时长 / 蓄力时间 | `text` | F | TBL |
| `cooldown` | 冷却或使用间隔 | `text` | F | TBL |
| `range` | 有效距离 / 范围 | `text` | F | TBL |
| `target` | 目标类型：自身 / 单体 / 小范围 / 大范围 / 领域 | `enum` | F | TBL |
| `limitations` | 限制与门槛：境界要求、性别、体质、单一目标、需持续维持 | `list<text>` | F | TBL |
| `backlash` | 反噬：触发条件、表现、严重度 | `list<{effect, trigger, severity}>` | F | TBL |
| `counters` | 克制 / 被克制 | `list<{technique_id, direction}>` | F | TBL |
| `mastery_level` | 掌握程度（见 4.4） | `versioned<enum>` | F | TBL |
| `proficiency` | 熟练度数值（0–1），用于排序 | `versioned<float>` | I | LLM |
| `holders` | 持有者及各自掌握度 | `list<{character_id, mastery_level, since_chapter, learned_from}>` | F | TBL |
| `is_secret` | 是否为不传之秘 | `bool` | F | TBL |
| `first_chapter` / `last_chapter` | 首末出现章 | `int` | F | KG |
| `evidence` | 证据句 | `list<{chapter, quote}>` | F | TBL |
| `canon_status` | canon / generated / inferred | `enum` | F | TBL |

### 4.3 `technique_effect` 效果子结构（"技能有什么作用"的答案）

| 字段名 | 含义 | 类型 | 性质 | 来源 |
|---|---|---|---|---|
| `effect_id` | 主键 | `int` | F | TBL |
| `owner_type` | 效果归属：technique / item / talent / bloodline | `enum` | F | TBL |
| `owner_id` | 归属对象 ID | `int` | F | TBL |
| `effect_type` | 效果类型：damage / heal / buff / debuff / control / shield / mobility / detection / summon / seal / purify / revive | `enum` | F | TBL + LLM |
| `damage_type` | 伤害属性：物理 / 灵力 / 神魂 / 真元 / 毒性 / 因果 | `enum` | F | TBL |
| `target_scope` | 作用范围：self / single / line / cone / area / domain | `enum` | F | TBL |
| `magnitude` | 量级（数值或相对档："越阶"、"同境无敌"） | `text` | F / I | LLM |
| `magnitude_unit` | 量级单位 / 口径 | `text` | F | TBL |
| `duration` | 持续时间 / 生效轮次 | `text` | F | TBL |
| `trigger` | 触发条件（命中 / 破防 / 濒死 / 特定时辰） | `text` | F | TBL |
| `stackable` | 是否可叠加 | `bool` | F | TBL |
| `counter_effect` | 被什么化解 / 免疫 | `text` | F | TBL |
| `narrative` | 叙事化描述（写正文用） | `text` | I | LLM |
| `chapter` | 首次体现该效果的章节 | `int` | F | TBL |

> **关键约束**：`effect_type` + `target_scope` 是**事实型**（原文能看出"这是范围攻击"），`magnitude` 是**解释型**（"威力极大"到底是多大，模型只能给相对判断）。不要把两者混成一个 `power` 字段。

### 4.4 掌握程度枚举

| 值 | 含义 | 判定依据 |
|---|---|---|
| `unknown` | 未提及 | 原文无信息 |
| `not_learned` | 未习得（知其名而不会） | 只听说过 |
| `beginner` | 初学 / 入门 | 原文"刚学会""勉强使出" |
| `small_success` | 小成 | 能稳定施展，无额外描写 |
| `great_success` | 大成 | 原文"已臻大成" |
| `perfection` | 圆满 / 化境 | 原文"圆满""随心所欲" |
| `mastered` | 已超越原功法（自创变式） | 原文明确改写 |
| `crippled` | 已废 / 被破功 | 原文明确失效 |

### 4.5 示例 JSON

```jsonc
{
  "name": "太虚剑典",
  "aliases": ["太虚剑经"],
  "type": "cultivation_method",
  "category": "attack",
  "grade": [
    { "value": "天阶上品", "from_chapter": 12, "to_chapter": null,
      "source": "canon", "confidence": 0.9,
      "evidence": { "chapter": 12, "quote": "此乃天阶上品功法，非宗主不可修" } }
  ],
  "grade_system_id": 88,
  "tier": 8,
  "element": "无",
  "realm_ceiling": "化神",
  "source_type": "sect",
  "source_entity_id": 41,
  "acquisition_chapter": 12,
  "effect_text": "以剑气化域，域内万物皆可为剑；修至大成可斩神魂。",
  "effects": [
    { "effect_type": "damage", "damage_type": "spirit", "target_scope": "area",
      "magnitude": "同境压制", "duration": "持续三息", "trigger": "剑域展开",
      "counter_effect": "被佛门金刚罩克制", "chapter": 57 },
    { "effect_type": "buff", "target_scope": "self",
      "magnitude": "剑意+1档", "duration": "一炷香", "trigger": "静心凝神",
      "chapter": 33 }
  ],
  "activation": ["需以本命剑为引", "口诵剑诀七字"],
  "cost": [{ "resource": "灵力", "amount": "三成", "unit": "成" },
           { "resource": "寿元", "amount": "一年", "unit": "年" }],
  "cast_time": "三息",
  "cooldown": "一日一次",
  "range": "百丈",
  "target": "area",
  "limitations": ["未至金丹不可修", "剑心不稳者易反噬"],
  "backlash": [{ "effect": "经脉寸断", "trigger": "强行越阶施展", "severity": "high" }],
  "counters": [{ "technique_id": 205, "direction": "countered_by" }],
  "mastery_level": [
    { "value": "small_success", "from_chapter": 33, "to_chapter": 88,
      "source": "canon", "confidence": 0.8, "evidence": { "chapter": 33, "quote": "剑典小成" } },
    { "value": "great_success", "from_chapter": 89, "to_chapter": null,
      "source": "canon", "confidence": 0.85, "evidence": { "chapter": 89, "quote": "剑典大成" } }
  ],
  "holders": [{ "character_id": 7, "mastery_level": "great_success", "since_chapter": 12 }],
  "is_secret": true,
  "first_chapter": 12,
  "last_chapter": 120,
  "canon_status": "canon"
}
```

---

## 5. 数据来源三分法

| 分类 | 判定标准 | 典型字段 | 落地方式 |
|---|---|---|---|
| **A. 可从现有知识图谱抽取** | `kg_entities.props/attrs`、`kg_relations`、`kg_events`、`kg_character_state` 里已有，或只需扩展抽取 prompt | 规范名、别名、性别、生死、首次出场章、身份/官职、所属势力、关系边、师承、按章状态、事件时间线、已知秘密、境界（粗粒度） | 直接读表 / 扩 `extractor_v3.py` 的 attrs 约定 |
| **B. 需新建独立设定表** | 存在一对多、需要版本化历史、需要跨角色持有关系 | 功法主表、效果子表、角色-功法掌握度、物品主表、装备栏、库存、持有/流转史、品阶体系 | 新建 5 张表，`entity_id` 挂到 `kg_entities` |
| **C. 需模型归纳生成** | 原文没有直接陈述，属于评价、动机、风格、弧光 | 效果量级、战力档位、动机权重、人物弧光、性格与语域、口头禅、剧情意义、战斗弱点 | 由 LLM 生成，标 `source=inferred` + `confidence`，**不得写入 canon** |

**不能靠模型归纳的项**（必须人工或原文）：品阶枚举、境界名称、功法名称、持有者、获得章节、消耗数值、生死状态、所属势力。这些一旦让模型"补"，就会出现设定漂移。

---

## 6. 事实型 vs 解释型

| 维度 | 事实型（F，可校验） | 解释型（I，随剧情演进） |
|---|---|---|
| 判定标准 | 原文能逐字找到依据；两个来源冲突时必有一个错 | 原文没有直接陈述；不同读者/模型可以给出不同答案 |
| 冲突处理 | `canon` 覆盖 `generated`，冲突进 `conflict_scan` 标红 | 允许重算，保留历史版本即可 |
| 是否进 prompt | 是，作为硬约束 | 是，但标注"参考设定，可调整" |
| 是否可被生成层改写 | 否（受 `PROTECTED_ATTRS` 保护） | 是 |
| 代表字段 | 名称、别名、性别、境界、品阶、持有者、获得章、消耗、限制、生死、位置、关系类型 | 效果量级、战力档位、动机、弧光、性格、语域、口头禅、剧情意义、熟练度数值 |
| 校验手段 | 正则 / 枚举校验 / 跨章一致性 / 与原文比对 | 只做"是否与事实矛盾"的反向检查，不做真值校验 |

**混合型字段的处理**：`effects`、`stats`、`power_rank` 这类"结构是事实、数值是解释"的字段，必须拆成两列——`effect_type`（F）+ `magnitude`（I），不要合并。

---

## 7. 与现有 schema 的对照与缺口

| 现有结构 | 位置 | 能承载的角色卡内容 | 缺口 |
|---|---|---|---|
| `kg_entities`（kind: character / location / faction / item / worldrule / power_system） | `backend/app/models.py:204-225` | 基本信息、身份标签、境界、势力 | `attrs` 是软 schema，无类型约束；`item` 无任何约定字段 |
| `kg_entities.attrs` 已用键 | `models.py:219`、`kg/extractor_v3.py:348,370` | `identity / identity_tags / rank / realm / faction / affiliation / arc / current_goal / secrets / power_level / region / leader_id / office / office_type / status / system / tier / belongs_to` | 无功法、无装备、无效果、无掌握度 |
| `kg_relations` + `kg_relation_evolution` | `models.py:235-276` | 社会关系、关系强度随时间变化 | 无"持有 / 装备 / 习得"这类物与能力的边 |
| `kg_events` | `models.py:284-310` | 经历时间线、关键事件、获得/失去节点 | 无"能力首次施展"的事件类型 |
| `kg_character_state`（state_type: location / injury / mood / goal / inventory / alive） | `models.py:339-356` | 按章状态快照 | `inventory` 是 500 字文本，无结构；无境界/掌握度状态 |
| `kg_entity_knowledge` | `models.py:395-419` | 认知边界、秘密知情 | 无"谁知道某部功法/某件法宝" |
| `PROTECTED_ATTRS` | `backend/app/kg/layers.py:18-22` | canon 保护名单 | 需扩充：`technique_id`、`item_id`、`grade`、`holders` 等 |
| `VALID_ENTITY_KINDS` | `backend/app/kg/extractor_v3.py:84` | 六类 kind 固定 | **不要新增 kind**；功法与物品分别用 `power_system` / `item` 挂载，靠独立表补充 |

**结论**：六类 kind 不用动，`attrs` 也不用大改。缺口全部通过"**独立表 + 外键挂 `kg_entities`**"补齐，这是改动面最小、且与现有 `canon/generated` 分层兼容的做法。

---

## 8. 最小落地建议（5 张新表）

```text
technique              功法/技能主表（4.2）
  └ technique_effect   效果子表（4.3），owner_type 可复用于 item / talent
character_technique    角色-功法：掌握度、起始章、师承来源（版本化）
item                   物品主表（3.5.1）
character_item         角色-物品：槽位、持有、消耗、流转史（版本化）
```

配套的三条约束：

1. **所有 `versioned` 字段落成"历史行 + `is_current` 标记"**，不要用 JSON 数组原地覆盖，否则无法回放"第 41 章的他是筑基中期"。
2. **`grade` 必须带 `grade_system_id`**。中文网文品阶体系互不兼容（天地玄黄 vs 凡灵宝法道圣 vs 上中下三品），`tier` 只用于同体系内比较；跨体系比较必须走 `power_rank`（解释型）。
3. **抽取顺序固定为**：先抽"名称 + 类型 + 持有者"（事实），再抽"效果类型 + 目标范围"（半事实），最后才让模型归纳"量级 + 叙事描述"（解释）。反过来做，模型会先编一个威力数值，再倒推设定。

---

## 9. 参考来源

**TRPG / D&D**

- [Basic Rules: Step-by-Step Characters — D&D Beyond](https://www.dndbeyond.com/sources/basic-rules/step-by-step-characters)
- [Sheet Sections — D&D Beyond Support](https://dndbeyond-support.wizards.com/hc/en-us/articles/7747193946388-Sheet-Sections)
- [Creating a Character (2024 Basic Rules) — D&D Beyond](https://www.dndbeyond.com/sources/dnd/br-2024/creating-a-character)

**游戏角色面板**

- [Black Desert — Character Profile（等级 / HP / MP / 负重 / 技能前置）](https://www.naeu.playblackdesert.com/es-eS/Wiki?wikiNo=8)
- [Ragnarok Online — 界面与装备槽位（头上/头中/头下/铠甲/主手/副手/披风）](http://guidesragnarok.free.fr/guides/interface.php)

**角色卡对话规格**

- [SillyTavern — Character Design 官方文档](https://docs.sillytavern.app/usage/core-concepts/characterdesign/)
- [SillyTavern — Characters 官方文档（内容字段与示例消息）](https://docs.sillytavern.app/usage/characters/)
- [Character Card V2 Specification（含 TypeScript 定义全文）](https://github.com/malfoyslastname/character-card-spec-v2/blob/main/spec_v2.md)
- [Character Card V3 Specification](https://github.com/kwaroran/character-card-spec-v3/blob/main/spec_v3.md)
- [Character Cards Explained: V1 / V2 / V3（v3 新增 nickname、assets、group_only_greetings、use_regex 等）](https://www.loreweaverai.com/guides/character-cards-explained)
- [Foreverse 术语表：角色卡 / chara_card_v3](https://foreverse.cn/zh/docs/glossary)

**中文小说人物设定卡 / 人物小传**

- [人物設定表範本（24 欄完整版 + 配角 6 欄 + 連載追加欄位）— LitMemo](https://litmemo.com/templates/character-sheet)
- [How to Create the Ultimate Character Profile — Reedsy](https://blog.reedsy.com/character-profile/)
- [Character Profile Template（want / need / wound / flaw / arc）— ScriptLix](https://scriptlix.com/templates/character-profile-template)
- [Character Outline Template（interior life / social web / arc mechanics 三簇）— Storyloft](https://storyloft.app/character-outline-template/)
- [写作突破：人物与人物小传的创作 — QQ阅读](https://m.xiaoshuo.qq.com/read/1053826260/8)

**仙侠 / 武侠题材设定**

- [修真百科 术语表（境界、阶段：初期/中期/后期/巅峰）](https://xianxia.dev/zh/glossary/)
- [《一个普通修仙者的故事》设定（练气→筑基→金丹→元婴→化神→练虚→合体→大乘；丹器符阵四艺；丹药九阶 × 品质）— 晋江](https://wap.jjwxc.net/book2/9568714/3)
- [《玄尘道途》功法品级（天/地/玄/黄 × 高/中/初，按境界、威力、瓶颈、经络划分）](https://m.baike.com/wiki/%E7%8E%84%E5%B0%98%E9%81%93%E9%80%94/19763487)
- [《北玄门》基本设定（先天→练气→筑基→紫府→金丹→元婴→元神→渡劫→天仙；丹阵符器九阶三品）— 创世中文网](https://m.chuangshi.qq.com/read/30340011/5)
- [《修仙：我能看穿万物》设定（丹药一阶下品至九阶极品；废/劣/次/正/精/无暇）— 起点](https://m.qidian.com/chapter/1047274004/874280079/)
- [《源起混沌》功法等级（凡诀/灵诀/宝诀/法诀/道典/圣典）— QQ阅读](https://ubook.reader.qq.com/book-detail/57364386)

**项目内部对照**

- `backend/app/models.py:204-225`（`KgEntity` 与 `attrs` 软 schema）
- `backend/app/models.py:235-276`（`KgRelation` / `KgRelationEvolution`）
- `backend/app/models.py:284-310`（`KgEvent`）
- `backend/app/models.py:339-356`（`KgCharacterState`）
- `backend/app/models.py:395-419`（`KgEntityKnowledge`）
- `backend/app/kg/extractor_v3.py:84`（`VALID_ENTITY_KINDS`）
- `backend/app/kg/extractor_v3.py:342-384`（六类 kind 边界与 `attrs` 约定）
- `backend/app/kg/layers.py:18-22`（`PROTECTED_ATTRS`）

---

## 10. 表结构定稿（已落盘）

> 定案：**用新表，不扩 `kg_character_state.state_type`**，原 6 个值保持不动。

产物目录：`experiments/2026-11-08-character-card/`

| 文件 | 作用 |
|---|---|
| `schema.sql` | 5 张表 + 1 张字段登记表 + 3 个视图 |
| `seed_examples.sql` | 真实样本数据 + 100 条字段来源登记 |
| `build.py` | 建库脚本，可重复执行（先删后建） |
| `character_card.db` | 独立库，98 KB；不连接、不修改 `kg_vol1_p1fix.db` |

### 10.1 五张表

| 表 | 行数（示例） | 承载 | 关键约束 |
|---|---|---|---|
| `technique` | 2 | 功法/武技/术法/技艺主表 | `type` 八值枚举；`grade` 与 `grade_system_id` 分离 |
| `technique_effect` | 1 | 效果子表，`owner_type` 可复用于 item/talent/bloodline | `effect_type`（事实）与 `magnitude`（解释）分列 |
| `character_technique` | 1 | 角色 × 功法，掌握度 | `UNIQUE(character, technique, since_chapter)` |
| `item` | 3 | 武器/法器/防具/丹药主表 | `UNIQUE` 不设；`category` 十四值枚举 |
| `character_item` | 4 | 角色 × 物品，**流转史** | `UNIQUE(item, character, segment_no)` |

辅助对象：

- `field_source_map`：100 行，逐字段登记 `fill_class`（A/B/C）与 `auto_now`（当前抽取器能否填）。
- `v_item_custody`：按 `acquired_chapter` 排出的持有时间线。
- `v_field_gap`：直接列出"当前填不出来"的字段。
- `v_technique_card`：给 prompt 用的技能卡扁平视图。

### 10.2 流转史的设计

一次「持有」= 一行（`segment_no` 从 1 开始）。「第 37 章丢剑、第 52 章夺回」= 两行：

```text
segment 1: acquired=37 ... lost=48, lost_reason='lost'
segment 2: acquired=52, acquired_reason='recovered', lost=NULL
```

全部 `lost_reason` 取值：`lost`（丢失）/ `seized`（被夺）/ `destroyed`（损毁）/ `gifted`（赠出）/ `sold`（售出）/ `confiscated`（收缴扣押）/ `stolen`（被盗）/ `stored`（封存）/ `unknown`。

非实体持有者（狱头、当铺、典当行）没有 `entity_id`，写 `holder_at_loss_name`，避免为临时持有者硬造实体。

### 10.3 示例数据（《大奉打更人》第一卷真实数据）

| 类型 | 样本 | 关键章 |
|---|---|---|
| 功法 | `天地一刀斩`（entity 1964，`martial_art`） | 第 80 章藏经阁挑刀法、第 104 章一刀重创炼神境银锣、第 153 章思考与刀法的契合度 |
| 术法 | `望气术`（entity 1985，`spell` / `detect`） | 第 2 章、第 30 章褚采薇解释原理 |
| 武器 | `黑金长刀`（entity 1462，`weapon/dao`，材质黑金，炼制者司天监） | 第 93 章炼金术造刀、第 94 章新佩刀、第 109 章起使用 |
| 武器 | `佩刀`（entity 811，打更人制式） | 第 81 章配发 |
| 法器 | `玉石小镜`（entity 1126，需滴血认主） | 第 73 章认主、第 105 章被堂弟换走 |
| **丢失** | `佩刀` 两段持有 | 第 81 章配发 → 第 102 章随腰牌摘下、收押期间由狱头保管（`confiscated`）→ 第 107 章取回（`recovered`） |
| 丢失（未闭合） | `玉石小镜` 一段持有 | 第 105 章被许新年 `seized`；**归还章节在图谱中无明确记载，故不建第二段** |

`v_item_custody` 实测输出：

```text
('佩刀', 1, 81, 102, 'gifted', 'confiscated', '狱头')
('佩刀', 2, 107, 220, 'recovered', None, None)
```

### 10.4 字段填充现状：100 个字段里 81 个当前填不出来

| 表 | 字段总数 | 当前可自动填 | 当前填不出 |
|---|---|---|---|
| `technique` | 29 | 6 | 23 |
| `technique_effect` | 17 | 1 | 16 |
| `character_technique` | 12 | 4 | 8 |
| `item` | 26 | 7 | 19 |
| `character_item` | 16 | 1 | 15 |
| **合计** | **100** | **19** | **81** |

当前能自动填的只有：实体名、别名、首末章、`entity_id`、`canon_status`，以及 `owns` 边的起止章。

**结构性缺口（不是"没抽到"，而是"抽不出"）**：

1. **`item` 没有任何 attrs 约定**。抽取器对 `item` 只产出名字与章节，`category`、`grade`、`material`、`crafter` 全部为空。这是最优先要补的——否则物品表永远是空壳。
2. **`power_system` 的 attrs 只有 `system/tier/belongs_to`**，没有 `grade`、`effect`、`cost`、`backlash`。`tier` 实测值为"未知"（`天地一刀斩`、`望气术` 都是），说明该字段目前无信息量。
3. **效果类字段全部依赖模型归纳**（`effect_type`、`target_scope`、`magnitude`）。现有 16 个效果字段只有 1 个能自动填。
4. **掌握度读不出来**。原文没有"小成/大成"这类词时，`mastery_level` 只能落 `unknown`，不能靠战绩反推。
5. **`evidence` 只有事件摘要，没有原文引文**。`kg_events.summary` 是抽取器写的概括句，不是逐字原文，已在 `evidence_json` 里用 `quote_type: "event_summary"` 标出，不能当作原文证据使用。

### 10.5 顺带发现的两个数据缺陷

1. **`天地一刀斩` 同时是 `power_system`(1964) 和 `item`(897)**。同一个词被拆进两个 kind，说明 kind 边界在"功法 vs 秘籍"上仍有歧义。新表用 `technique.entity_id` 单值引用，需要先定一个主实体，另一个作为别名。
2. **`黑金刀`(1461) 与 `黑金长刀`(1462) 是两个节点**。同一件武器在第 93 章叫"黑金刀"、第 109 章叫"黑金长刀"，未合并。`item.aliases_json` 可以吸收这种别名，但需要一次合并动作。

### 10.6 不动的部分

- `kg_character_state.state_type` 保持 `location / injury / mood / goal / inventory / alive` 六值不变。
- `kg_vol1_p1fix.db` 只以 `mode=ro` 只读方式打开，构建后 mtime 仍为 `2026/11/7 10:09:17`，未产生 `-wal` / `-shm`。
- 不新增 `VALID_ENTITY_KINDS`，六类 kind 原样保留。
