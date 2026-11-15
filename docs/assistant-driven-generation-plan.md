# 对话助手主导整篇生成：意见 → 理解 → 反问 → 执行 → 回报（方案）

> 本轮只出方案，不改业务代码。文中每个「助手可以做到」都指到现存代码；没有代码支撑的一律标 **需新建** 或 **做不到**。
> 约束：不动 `app/style/**`（另一条线在改文风渲染）；不新增写旁路，执行只走既有端点/服务。
> 证据口径：`file:line` 均为本仓库当前代码；标注「实测」的结论来自本会话前几轮的隔离环境实测。

---

## 0. 结论（先看这 8 条）

1. **能真正改正文的现成通道只有一条**：`POST /api/assistant/invoke-generation` 的 `action=regenerate_segment` + `instruction`，一次只改一段（[assistant_generation.py:580](backend/app/routers/assistant_generation.py#L580)、[engine.py:1945](backend/app/generation/engine.py#L1945)）。其余能力都是**名单/配置级**改动，不会动已写正文。
2. **不能把写操作交给 LLM 自由调用**。Agent 工具白名单是只读的，注册与调用双重门都会拒绝非只读工具（[registry.py:71-88](backend/app/agents/registry.py#L71-L88)、[registry.py:163-167](backend/app/agents/registry.py#L163-L167)、[registry.py:215-219](backend/app/agents/registry.py#L215-L219)）。所以正确形态是「**LLM 负责理解/规划/措辞，确定性执行器负责动手**」。
3. **反问是安全阀，不是默认动作**。只有两种情况必问：目标段无法确定、局部改 vs 全局设定无法区分。其余用「默认解释 + 回报可纠偏」吸收，每轮最多问一个问题。
4. **范围遵循最小作用域**：1 段 > 最近 3 段 > 全篇。**全篇永不默认**，必须用户明确说「全篇/整本/一直」，且先给分批计划。
5. **单段重写目前不可回滚**：`regenerate_segment_stream` 直接覆盖目标段，引擎里没有任何版本/快照写入（[engine.py:1945-2030](backend/app/generation/engine.py#L1945-L2030) 无 proposal/version/snapshot 分支）。要支持「直接做」必须先补前置快照（**需新建**）。
6. **重写不会级联改后段**：只替换第 N 段并重建摘要树，后段正文保持原样（[engine.py:1954-1958](backend/app/generation/engine.py#L1954-L1958)）。回报必须主动说明「第 N+1 段可能还在承接旧情绪」。
7. **回报必须是结构化 receipt 渲染出来的人话**：改了哪几段、按什么理解、改前改后开头对照、副作用、能否回滚。只说「已重写」不合格。
8. **明确做不到**：改文风回改旧文（要逐段重写）、全篇一键重写、章节级操作（无章节实体）、功法/性别字段、KG 实体手动编辑、已生成正文里的角色名。见第 7 节。

---

## 1. 现状底座（只列与本方案相关的）

| 入口 | 能做什么 | 不能做什么 | 证据 |
|---|---|---|---|
| `POST /api/assistant/qa` | 单轮问答；只读工具取证据；提出待确认卡，**不执行** | 无会话、无 clarification 字段、无多轮状态 | [assistant.py:265-484](backend/app/routers/assistant.py#L265-L484)、请求体仅 `project_id/query/scope/version_id` [assistant.py:107-119](backend/app/routers/assistant.py#L107-L119)、工具三元组硬编码 [assistant.py:316-320](backend/app/routers/assistant.py#L316-L320) |
| `POST /api/assistant/invoke-generation` | `generate_next` / `regenerate_segment`（带 `instruction`），写完落库 | 一次只处理一段；`instruction` 必填；不暴露 `novel_ids` | 动作枚举 [assistant_generation.py:95](backend/app/routers/assistant_generation.py#L95)、校验 [L130-144](backend/app/routers/assistant_generation.py#L130-L144)、重写分支 [L680-750](backend/app/routers/assistant_generation.py#L680-L750) |
| `POST /api/generations/regenerate-stream` | 流式重写单段 | **无 `instruction` 参数**（只传段号/novel_ids/max_chars） | [generation.py:146-161](backend/app/routers/generation.py#L146-L161) |
| 待确认卡 | 把「重写第 N 段 / 继续写」变成一张卡，确认后前端调端点 | 只提出、不执行；不解析模糊范围 | [pending_actions.py:261-345](backend/app/agents/pending_actions.py#L261-L345)、卡种 [L58](backend/app/agents/pending_actions.py#L58) |
| 角色增删改境界 | 加角色、删角色、改境界（含 `realm_order` 校验） | 不改已写正文 | [character_edit.py:66](backend/app/kg/character_edit.py#L66)、[L115](backend/app/kg/character_edit.py#L115)、[L182](backend/app/kg/character_edit.py#L182)；端点 [projects.py:685](backend/app/routers/projects.py#L685)、[L703](backend/app/routers/projects.py#L703)、[L718](backend/app/routers/projects.py#L718) |
| 角色改名 | 按影响面放行/拒绝；只改名单并登记别名 | 不改旧正文；影响大时拒绝 | 端点 [projects.py:662](backend/app/routers/projects.py#L662)、阈值 `MAX_LAST_SEGMENT_HITS=3` [character_rename.py:36](backend/app/kg/character_rename.py#L36)、判定 [L140](backend/app/kg/character_rename.py#L140) |
| bible 整体改写 | 写 `plot_bible` + revision + 冲突扫描 | 不改已写正文；时间线模式下部分键不渲染 | [projects.py:637](backend/app/routers/projects.py#L637) → [bible.py:410](backend/app/kg/bible.py#L410) |
| 文风档位/蒸馏 | 改 `narrative_tier`、跑蒸馏、应用蒸馏档案 | 只影响后续生成，不回改旧文 | 档位字段 [schemas.py:96-98](backend/app/schemas.py#L96-L98)、蒸馏 [projects.py:119](backend/app/routers/projects.py#L119)、应用 [projects.py:423-462](backend/app/routers/projects.py#L423-L462) |
| 提案 + 版本回滚 | `generation_text` 提案可改 `generations.text`，**应用时写 Version 快照**，可 restore | 需要先有完整 `after_text`；三次状态机；非生成通道 | [assistant_proposals.py:277](backend/app/routers/assistant_proposals.py#L277)、[L458](backend/app/routers/assistant_proposals.py#L458)、[L605](backend/app/routers/assistant_proposals.py#L605)；写入+快照 [proposals.py:892-977](backend/app/agents/proposals.py#L892-L977) |
| 诊断/审计 | 语料、KG、生成三套规则审计 | 不自动在重写后跑；不是「节奏分析」 | [assistant_audit.py:287](backend/app/routers/assistant_audit.py#L287)、[L339](backend/app/routers/assistant_audit.py#L339)、[L383](backend/app/routers/assistant_audit.py#L383) |
| 引导多轮状态机 | 真正的多轮：作答/追问/没听懂/回退；`/say` 有 clarify 分支 | 只在引导阶段；commit 后 409 | [assistant_wizard.py:711-864](backend/app/routers/assistant_wizard.py#L711-L864)、[wizard_chat.py:345-391](backend/app/wizard_chat.py#L345-L391) |
| 段落重置 | 仓库内只有内部函数 | **HTTP 层无端点** | [generated_store.py:1447](backend/app/kg/generated_store.py#L1447) |

**关键结论**：目前的对话助手是「问答 + 提卡」，不是「规划 + 执行 + 回报」。本方案补的就是中间那层编排。

---

## 2. 意图识别：用户会说哪些话

分类沿用前几轮实测的分类（改角色名/境界/功法/背景/增删）与引导侧测出的说法（改某段/改主线/否定上一段），合并扩展为 12 类。

| # | 类型 | 典型说法 | 判定信号 | 落点能力 | 现状 |
|---|---|---|---|---|---|
| I1 | 重写某段（明确段号） | 「第 3 段重写」「把第 5 段改成他输」 | 段号正则 + 改写动词 | `invoke-generation(regenerate_segment)` | **可复用**（[pending_actions.py:296-323](backend/app/agents/pending_actions.py#L296-L323)） |
| I2 | 重写最近内容（无段号） | 「这段不行，重写」「刚才那段太水」 | 指代词 + 改写动词 | 同上，段号由上下文解析 | **缺一环**：无上下文解析（需新建 scope resolver） |
| I3 | 局部精修 | 「删掉重复」「压到 800 字」「结尾加个钩子」 | 编辑动词 + 目标段 | 同上，`instruction` 承载要求 | **可复用** |
| I4 | 否定上一段 | 「这段写得不好」「不行，重来」 | 否定词 + 指代 | 同上 | **缺一环**：否定词不在 `_REWRITE_MARKERS`（[pending_actions.py:169-187](backend/app/agents/pending_actions.py#L169-L187) 只收「重写/改写」类） |
| I5 | 重写但保留某物 | 「重写第 3 段，保留打斗」 | 改写动词 + 保留从句 | 同上，保留项进 `instruction` | **可复用** |
| I6 | 改整体节奏 | 「节奏太慢」「太拖了」「水」 | 评价词，无具体段 | 多段重写 / 先改几段 | **新建**（无批量重写） |
| I7 | 改文风 | 「太 AI 腔」「更冷硬」「少形容词」 | 风格词 | 后续生成：档位/蒸馏；旧文：逐段重写 | **部分可复用**（见第 5 节） |
| I8 | 改人物（设定） | 「主角不该这么软」「加个妹妹」「他该是金丹」 | 角色名 + 设定谓词 | 性格/动机 → bible apply；增删/境界 → character_edit；名字 → rename | **可复用**（见第 5 节） |
| I9 | 改情节走向 | 「别让他赢」「换个结局」「加个反转」 | 情节动词 + 角色/事件 | 已写正文 → 重写段；后续 → bible `direction/conflicts` | **部分可复用**（后续走向在时间线模式不渲染，实测见审计文档 §2.4） |
| I10 | 续写/多写 | 「继续写」「再写三段」 | 续写标记 | `generate_next` / `/generate-loop` | **可复用**（[pending_actions.py:325-343](backend/app/agents/pending_actions.py#L325-L343)、[generation.py:179](backend/app/routers/generation.py#L179)） |
| I11 | 查询/诊断 | 「为什么角色名对不上」「哪段 AI 腔最重」 | 疑问句 | `/qa`、`/audit-generation` | **可复用** |
| I12 | 回滚/撤销 | 「回到改之前」「撤销刚才」 | 撤销词 | `versions/restore` | **缺一环**：重写无快照，仅提案通道有版本 |

补充两类容易漏的：

| # | 类型 | 典型说法 | 处置 |
|---|---|---|---|
| I13 | 全局偏好 | 「以后每段短点」「以后别写这么长」 | 改 `project.generation_length`（[schemas.py:80](backend/app/schemas.py#L80)）；**只影响后续**，必须说明 |
| I14 | 删除段落 | 「这段删掉」 | **做不到**：无 HTTP reset、无章节实体；只能重写成空/极短或用提案改文本，且会破坏摘要链语义 → 诚实拒绝 + 给替代 |

---

## 3. 什么时候反问，什么时候直接做

### 3.1 槽位模型

每条意见抽 4 个槽：

| 槽 | 含义 | 例子 |
|---|---|---|
| `target` | 改哪里 | 第 3 段 / 最近三段 / 全篇 / 角色「林砚」 / 项目级 |
| `operation` | 做什么 | 重写 / 删减 / 扩写 / 改名 / 改境界 / 加人 / 删人 / 改档位 / 续写 |
| `direction` | 改成什么 | 「他输」「更冷硬」「保留打斗」 |
| `scope_intent` | 局部还是全局 | 这一段的行为 vs 整本设定 |

判定只依赖前三项是否**可确定或可安全默认**。

### 3.2 判定规则（按优先级）

| 规则 | 内容 | 违反时 |
|---|---|---|
| R1 | **目标必须唯一确定**：能解析到段落集合，或能从会话上下文唯一指代 | 问「哪几段」，给编号选项 |
| R2 | **局部/全局必须可区分**：一句话同时能解释为「改这段」和「改整本设定」时，不许猜 | 问「改这段写法，还是改整本设定」 |
| R3 | **不可逆且影响 > 1 段**：先给执行计划（段号、指令、预计改动），等一句「开始」 | 先计划，不直接跑 |
| R4 | **能力不存在**：直接说做不到 + 给替代路径 | 拒绝，不假装 |
| R5 | **每轮最多 1 个问题**，且这个问题必须改变执行计划；可默认的槽位不许问 | 合并成 1 句 |
| R6 | **最小作用域**：能 1 段解决不动 3 段；能 3 段解决不动全篇 | 降级范围 |
| R7 | **沉默执行必须声明理解**：默认解释要写在回报最前面，便于用户一句话纠偏 | 补声明 |

判定结果分 5 档：

- **L1 直接做**：目标 + 改法都明确。
- **L2 声明后做**：目标明确、改法缺失 → 用默认解释，先声明再执行，不阻塞。
- **L3 问一句再做**：目标不确定，或局部/全局有歧义。
- **L4 先给计划**：多段 / 全篇 / 不可逆影响面大。
- **L5 拒绝 + 替代**：能力不存在。

### 3.3 信息充分性判断表

| 用户原话 | target | operation | direction | 判定 | 助手动作 |
|---|---|---|---|---|---|
| 第 3 段重写 | 明确 | 明确 | 缺 | **L2** | 直接做，默认「保留原情节与人物关系，改掉重复表达/AI 腔」，回报里声明 |
| 第 3 段重写，保留打斗 | 明确 | 明确 | 明确 | **L1** | 直接做，`instruction` 带「保留打斗」 |
| 这段太拖沓（刚在看第 12 段） | 上下文唯一 | 评价 → 默认删减 | 缺 | **L2** | 直接做第 12 段，声明「删重复描写、压铺垫、保留信息点」 |
| 这段太拖沓（无上下文） | 不明确 | 默认 | 缺 | **L3** | 只问范围：「第 12 段，还是最近三段？」 |
| 主角不该这么软 | 不明确 | 不明确 | 缺 | **L3** | 只问局部/全局：「改第 12 段的行为，还是改整本设定？」 |
| 节奏太慢（无范围词） | 默认最近 3 段 | 默认删减 | 缺 | **L2** | 按最近 3 段做，声明「如果指全篇，我再分批」 |
| 整本节奏都慢 | 全篇 | 默认删减 | 缺 | **L4** | 先给计划：N 段、每批 3 段、先改一批看效果 |
| 太 AI 腔了 | 不明确 | 改文风 | 缺 | **L3** | 问「只改后续生成，还是回改已有段」 |
| 把主角改名叫沈砚 | 角色明确 | 明确 | 明确 | **L1/L5** | 先算影响面；超阈值拒绝并给替代（[character_rename.py:140](backend/app/kg/character_rename.py#L140)） |
| 给主角加个妹妹 | 角色明确 | 明确 | 缺 | **L2** | 直接加（`add_character`），声明「只进生成名单，不进 KG」 |
| 让他输掉这场 | 需解析「这场」 | 明确 | 明确 | **L1/L3** | 能定位到段就做；不能就问段 |
| 改一下主线 | 项目级 | 不明确 | 缺 | **L3** | 问「改后续走向（bible），还是改已写正文」 |
| 以后每段短点 | 项目级 | 明确 | 明确 | **L1** | 改 `generation_length`，声明只影响后续 |
| 这段删掉 | 段明确 | 删除 | — | **L5** | 做不到，给「重写成极短过渡段」的替代 |

### 3.4 三个必答例子

**「这段太拖沓」该问什么**

只问**范围**，不问「是节奏还是句子长度」。理由：两种理解在同一段内的改法高度重叠（删重复心理描写、压缩环境铺垫、保留信息点与冲突），差异不足以改变执行对象；而改错段的代价是覆盖正文。语义差异用回报里的「我的理解」吸收。

> 先确认范围：你说的「这段」是**第 12 段**，还是**最近三段（10–12）**？我按「删重复描写、压铺垫、保留信息点和结尾钩子」来改；如果你要的是加快事件推进，改完不满意我再按那个方向来一轮。

**「主角不该这么软」该问什么**

必须问**局部还是全局**，因为两条路的能力和代价完全不同：

> 这句有两种改法：A. 只改第 12 段——把他这次的退让写成「有代价的隐忍」；B. 改整本设定——把性格/动机改成「隐忍」，影响之后所有生成（**已写的正文不会自动变**）。你要 A 还是 B？

**「第 3 段重写」够不够直接做**

够。目标确定、操作确定，缺的只是方向，用默认解释即可，不需要问。若第 3 段不存在，直接告知现有段范围，不猜。

### 3.5 只问一句：问题选择算法

伪代码（**需新建**，建议放 `app/agents/dialog/clarify.py`）：

```python
def pick_question(missing: list[str], intent: str, ctx: dict) -> dict:
    # 优先级固定：目标段 > 局部/全局 > 方向 > 范围大小
    order = ["target", "scope_intent", "direction", "scope_size"]
    slot = next(s for s in order if s in missing)
    return QUESTION_TEMPLATES[(intent, slot)].render(ctx)  # 每模板最多 2 个选项 + 1 个默认
```

硬约束：

- 同一话题连续追问 ≤ 2 轮；第 3 轮改为「先做最小范围 + 给选项」。
- 问句里必须带**具体候选**（第 12 段 / 最近三段），不能问「你想改哪里」。
- 不能把已经在上下文里的信息再问一遍。

---

## 4. 改写范围怎么定

### 4.1 映射规则

| 用户说法 | 解析结果 | 说明 |
|---|---|---|
| 第 N 段 / 第 N–M 段 | `{N}` / `{N..M}` | 段不存在 → 告知现有范围；复用段号解析 [pending_actions.py:243-258](backend/app/agents/pending_actions.py#L243-L258) |
| 这段 / 刚才那段 | 会话上下文 `current_segment_no`，否则最近一次操作段 | 无上下文 → L3 问 |
| 最后一段 / 最新一段 | `max(segment_no)` | 复用「最后一段」标记表 [assistant.py:224-236](backend/app/routers/assistant.py#L224-L236) |
| 最近几段 | 最后 K 段，K 默认 3 | 回报里写明 K |
| 开头 / 前三段 | `1..3` | |
| 从第 N 段开始 | `N..max` | |
| 整体 / 全篇 / 一直 / 从头到尾 | 全部段 | **永不默认**；必须 L4 分批 |
| 后面 / 接下来 | 不重写 | 转续写 `generate_next` |
| 「林砚那几段」 | 含该角色名的段集合 | **需新建**：按 `Generation.text` 计数（可复用改名统计口径 [character_rename.py](backend/app/kg/character_rename.py)） |
| 没有范围词的评价型（节奏/水/文风） | 最近 3 段 | 最小作用域 + 声明 |
| 没有范围词的指令型（重写/改） | 最近 1 段 | 最小作用域 + 声明 |

### 4.2 默认值的设计理由

- **评价型默认 3 段**：单段往往看不出节奏问题，3 段能覆盖一个完整小场景；同时避免默认全篇带来的长时间覆盖。
- **指令型默认 1 段**：用户说「重写」时通常心里有一段（刚看的/刚提的），默认多改等于多破坏。
- **全篇永不默认**：全篇意味着几十到上百次 LLM 调用、不可回滚、耗时以小时计，必须显式授权。

### 4.3 映射不确定时的反问话术

- 范围歧义：
  > 你说的「节奏慢」我先按**最近 3 段（10–12）**处理。如果是从开头就慢，回我「全篇」，我先给你分批计划，不会一次覆盖全书。
- 段号不存在：
  > 目前只有第 1–12 段，没有第 15 段。你是想改第 12 段，还是等写到第 15 段？
- 指代无上下文：
  > 我不确定「这段」指哪一段：是第 12 段（最新），还是第 10 段（你上次让我改的）？

### 4.4 多段/全篇的前置条件

1. 先算范围：`segments = [...]`、总字数、预计调用次数。
2. 分批：每批 ≤ 3 段（与现有批量上限精神一致，[proposals.py:210](backend/app/agents/proposals.py#L210) 的 `MAX_BULK_APPLY=50` 是提案上限，不是重写上限）。
3. 批间必须回一次头：给本批的 before/after 开头 + 副作用，再问「继续下一批 / 调方向 / 停」。
4. 每段执行前写快照（**需新建**，见 5.3）。

---

## 5. 已有能力盘点

### 5.1 可直接复用（不改）

| 能力 | 复用点 | 证据 |
|---|---|---|
| 单段重写 + 指令注入 | `regenerate_segment_stream(..., instruction=...)`；只注入 user prompt 的重写要求区；空指令时提示词逐字节不变 | [engine.py:1945-2005](backend/app/generation/engine.py#L1945-L2005) |
| 重写只替换目标段 + 重建摘要树 | 改完不会级联改后段 | [engine.py:1954-1958](backend/app/generation/engine.py#L1954-L1958) |
| 指令是否真进了提示词 | `instruction_injected` 回报 | [assistant_generation.py:699](backend/app/routers/assistant_generation.py#L699)、[L746](backend/app/routers/assistant_generation.py#L746) |
| 重写后 AI 腔反馈 | 生成后自动跑 | [assistant_generation.py:424-572](backend/app/routers/assistant_generation.py#L424-L572) |
| 续写 | `generate_next` | [assistant_generation.py:605](backend/app/routers/assistant_generation.py#L605) |
| 角色增删改境界 | `add_character` / `remove_character` / `set_character_realm` | [character_edit.py:66](backend/app/kg/character_edit.py#L66)、[L115](backend/app/kg/character_edit.py#L115)、[L182](backend/app/kg/character_edit.py#L182) |
| 改名前影响面统计 + 阈值拒绝 | 逐段计数，超 3 次拒绝并给回执 | [character_rename.py:36](backend/app/kg/character_rename.py#L36)、[L140](backend/app/kg/character_rename.py#L140) |
| bible 改写 + revision + 冲突扫描 | `apply_bible_revision` | [bible.py:410](backend/app/kg/bible.py#L410) |
| 文风档位 / 蒸馏 / 应用 | 改配置、跑蒸馏、套档案 | [projects.py:119](backend/app/routers/projects.py#L119)、[L423](backend/app/routers/projects.py#L423) |
| 版本快照 + 回滚 | 提案 apply 时写 Version；`versions/restore` | [proposals.py:892-977](backend/app/agents/proposals.py#L892-L977)、[assistant_proposals.py:605](backend/app/routers/assistant_proposals.py#L605) |
| 审计规则 | AI 腔、空段、段长离群、角色名单双向不一致 | [assistant_audit.py:383](backend/app/routers/assistant_audit.py#L383)、规则实现 `app/agents/audit.py` |
| 多轮 clarify 骨架 | 引导侧 `/say` 的 clarify / explain / action 分支 | [assistant_wizard.py:711-864](backend/app/routers/assistant_wizard.py#L711-L864) |
| 对话记录落库 | `record_message`，`AssistantMessage.session_id` 可承载会话 | [assistant.py:465-473](backend/app/routers/assistant.py#L465-L473)、[models.py:1116](backend/app/models.py#L1116)、[L1156](backend/app/models.py#L1156) |

### 5.2 有零件但没接线（缺一环）

| 缺口 | 已有零件 | 缺什么 | 处置 |
|---|---|---|---|
| 多轮状态 | `AssistantMessage.session_id`、wizard 状态机 | `/qa` 无 session、无 pending clarification | **接线 + 新建轻量状态** |
| 待确认卡 → 自动执行 | `pending_actions` 能提卡 | 用户希望「助手自己动手」，不再点按钮 | 新增「执行器」直接调服务层；卡片降级为危险动作的可选确认 |
| 重写后副作用检查 | `audit_generation`、改名计数、AI 腔反馈 | 重写完不会自动跑；没有编排 | **接线**：重写后串一个 `post_rewrite_audit` |
| 回滚 | Version 快照通道 | 重写路径不写快照 | **新建**：重写前落快照，或改走提案 apply |
| 文风回改旧文 | 单段重写带 instruction | 没有批量、没有「文风指令模板」 | 新建批量 + 指令模板 |
| 角色相关段定位 | 改名计数逻辑 | 没有「按角色名找段」的查询 | **新建**小函数 |

### 5.3 必须新建

| # | 新建件 | 职责 | 建议位置 |
|---|---|---|---|
| N1 | `intent` 分类 + 槽位抽取 | 规则优先（正则/词典），LLM 兜底；输出第 3 节的槽 | `app/agents/dialog/intent.py` |
| N2 | `clarify` 问题选择 | 按 3.5 的固定优先级出 1 个问题 | `app/agents/dialog/clarify.py` |
| N3 | `scope` 解析器 | 模糊说法 → 段集合；含「按角色找段」 | `app/agents/dialog/scope.py` |
| N4 | `orchestrator` 执行器 | 计划 → 调用既有服务 → receipt | `app/agents/dialog/orchestrator.py` |
| N5 | `receipt` 构造 | 结构化回报 + before/after 截取 | `app/agents/dialog/receipt.py` |
| N6 | 重写前置快照 | 覆盖前把旧 `Generation.text` 写 Version（复用提案快照通道或新建小表） | `app/agents/dialog/snapshot.py` |
| N7 | 重写后副作用审计 | 旧名残留、bible 冲突、持有物冲突、AI 腔、后段衔接提示 | `app/agents/dialog/post_audit.py` |
| N8 | 批量重写编排 | 逐段调用 `regenerate_segment_stream`，批间回报 | `app/agents/dialog/batch.py` |
| N9 | 多轮会话端点 | `POST /api/assistant/turn`（或扩展 `/qa`）带 `session_id`、返回 `mode/question/receipt` | `app/routers/assistant_dialog.py` |
| N10 | 全局偏好落点 | 「以后都短点」→ `generation_length`；无跨项目记忆 | 复用 `PATCH /projects/{id}` |

接口草图（**仅示意，不落地**）：

```python
class AssistantTurnRequest(StrictRequest):
    project_id: int
    text: str
    session_id: int | None = None
    context: dict | None = None      # current_segment_no / selection / last_segment_no

class AssistantTurnResponse(BaseModel):
    mode: Literal["clarify", "preview", "executed", "refused"]
    message: str                     # 人话
    understood: dict                 # 槽位 + 默认解释
    question: dict | None = None     # mode=clarify 时：{slot, text, options}
    plan: dict | None = None         # mode=preview 时：段号/指令/成本
    receipt: dict | None = None      # mode=executed 时：见 6.2
```

执行器调用关系（全部走现有服务，不新增写旁路）：

```python
def execute(plan, db, project):
    results = []
    for seg in plan.segments:
        snapshot(project, seg)                      # N6 需新建
        for ev in regenerate_segment_stream(
            db, project, seg, instruction=plan.instruction_for(seg)
        ):
            ...
        results.append(audit_after(project, seg))   # N7 需新建
    return build_receipt(plan, results)              # N5 需新建
```

---

## 6. 执行完怎么回报

### 6.1 回报的五个必答项

1. **改了哪几段**（段号集合，明确「只动了这些」）。
2. **按什么理解改的**（默认解释，放最前面）。
3. **改前改后的关键差别**（字数 + 开头 1–2 句对照）。
4. **副作用检查**（旧名残留、bible 冲突、持有物冲突、后段衔接、AI 腔）。
5. **能不能回滚**（有快照给版本号；没有就明说）。

### 6.2 receipt schema（建议）

```json
{
  "understood": {
    "intent": "rewrite",
    "target": {"segments": [12], "resolved_by": "context"},
    "direction": "删重复心理描写与环境铺垫，保留信息点与结尾钩子",
    "assumptions": ["把「太拖沓」理解为删减，而不是加快事件推进"]
  },
  "executed": [
    {"segment_no": 12, "action": "regenerate_segment", "ok": true,
     "chars_before": 1120, "chars_after": 862, "instruction_injected": true}
  ],
  "before_after": [
    {"segment_no": 12,
     "before_head": "……前 80 字……",
     "after_head": "……前 80 字……"}
  ],
  "side_effects": {
    "old_names": [], "bible_conflicts": [], "held_item_conflicts": [],
    "ai_flavor": {"before": 3, "after": 1},
    "downstream": "第 13 段及以后未改；摘要链已重建",
    "rollback": {"available": true, "version_id": 88}
  },
  "not_done": ["未改第 10、11 段；未改 bible"]
}
```

### 6.3 副作用检查清单

| 检查 | 做法 | 现状 |
|---|---|---|
| 旧名残留 | 对 `Generation.text` 逐段 `count`（复用改名统计口径） | 有零件，**需接线** |
| bible 冲突 | 若本次改了 bible，读 `apply_bible_revision` 返回的 `conflicts` | **可复用** |
| 持有物冲突 | 用 `get_fact_ledger` 证据 + 文本比对，检查新正文是否与持有事实矛盾 | **需新建** |
| AI 腔 | 复用 `_run_ai_flavor_feedback` | **可复用** |
| 后段衔接 | 不级联改后段是既有语义；必须文案提示 | **可复用事实 + 需措辞** |
| 回滚可用性 | 有快照给 `version_id`，无快照明确「不可一键撤销」 | **需新建快照** |

### 6.4 回报文案模板（示例数据，非真实生成）

> 改完了，**只动了第 12 段**。
>
> **我的理解**：你说的「太拖沓」= 重复的心理描写 + 两处环境铺垫。我按「删重复、压铺垫、保留信息点和结尾钩子」改，情节和人物关系没动。
>
> **改了什么**
> - 第 12 段：1120 字 → 862 字（-23%）
> - 重写指令确实进了提示词（`instruction_injected=true`）
>
> **开头对照**
> - 改前：「夜色像一层化不开的墨，林砚站在山门前，心里反复想着白天师父说的那句话，想着自己是不是真的做错了，想着……」
> - 改后：「山门前的灯灭了一盏。林砚站着没动，白天那句话还在耳边——他是不是真的做错了。」
>
> **副作用**
> - 角色名：无旧名残留（林夜 0 次，林砚 2 次）
> - 设定：未改 bible，无冲突
> - 持有物：新正文未与「青锋剑 / 半枚玉佩」冲突
> - 后段：第 13 段及以后未改；摘要链已按新第 12 段重建，但第 13 段开头可能仍承接旧情绪
> - AI 腔：3 处 → 1 处
> - 回滚：本次有快照，版本号 88，说「回滚第 12 段」即可
>
> **没做**：没动第 10、11 段，没改设定。
>
> 如果这个力度合适，我可以照同样标准处理第 10、11 段；不合适就说「再短一点」或「保留那段环境描写」。

### 6.5 回滚说明（诚实版）

- 走本方案的重写：**先快照再改**，回报给 `version_id`（**需新建 N6**）。
- 现状直接调 `invoke-generation`：**没有快照**，覆盖后只能靠数据库备份/git 恢复。方案落地前，助手必须把这一句写进回报。

---

## 7. 能力的诚实边界

| 做不到的事 | 原因 / 证据 | 替代路径 |
|---|---|---|
| 改文风后自动重写全部旧文 | 文风档位/蒸馏只改后续生成的配置（[projects.py:423-462](backend/app/routers/projects.py#L423-L462)） | 逐段重写 + 文风指令；批量需新建 |
| 全篇一键重写 | 无批量端点，`regenerate_segment` 一次一段（[assistant_generation.py:95](backend/app/routers/assistant_generation.py#L95)） | 分批 3 段，批间确认 |
| 章节级操作（「改第 3 章」） | 无章节实体，只有 `chapter_range="段N"`（[engine.py:488](backend/app/generation/engine.py#L488)、[models.py:479-489](backend/app/models.py#L479-L489)） | 把「章」翻译成段范围并让用户确认 |
| 删除某段 | 无 HTTP reset（仅内部 [generated_store.py:1447](backend/app/kg/generated_store.py#L1447)） | 重写成极短过渡段，或人工处理 |
| 改功法 | 底层无「功法」概念，bible 写 `功法` 零渲染 | 写进 `role/motivation` 或 `power_constraint`（效果有限） |
| 改性别 | 无字段 | 写进 `role` 描述，但不会结构化 |
| 手动建/合并 KG 实体 | `kg_v3` 只读，写入口只有豁免 | 只能靠生成后自动抽取 |
| 改已生成正文里的角色名 | 改名只改名单；旧正文不动 | 改名单 + 逐段重写受影响的段 |
| 全篇节奏分析 | 审计只有空段/段长离群/AI 腔/名单不一致（[assistant_audit.py:383](backend/app/routers/assistant_audit.py#L383)），不是节奏模型 | 助手逐段读 + 主观判断，或先改 3 段看效果 |
| 后台长任务自主跑完 | 生成是同步/流式请求 | 分批、每批等用户确认 |
| 跨项目记住偏好 | 无用户级偏好存储 | 每个项目单独设 `generation_length` / `narrative_tier` |

---

## 8. 落地顺序（建议）

| 阶段 | 范围 | 验收 |
|---|---|---|
| P0 | N1 意图（规则版）+ N2 反问 + N3 范围（明确段号/最近段/最后段）+ N9 多轮端点 + N5 receipt + N6 快照 | 附录 A 的对话能逐字跑通；单段重写有 before/after、有回滚 |
| P1 | N7 副作用审计 + N8 批量（≤3 段/批）+ 角色相关段定位 | 「节奏太慢」能按最近 3 段执行并逐段回报 |
| P2 | 全篇分批 + 文风批量 + 全局偏好映射 + 更细的意图（LLM 兜底） | 「整本都慢」能给出计划、分批执行、批间可停 |

P0 的验收不依赖 `app/style/**`，也不改现有端点契约。

---

## 附录 A：对话示例（验收标准）

> 场景：项目已有 12 段，用户刚在页面上看完第 12 段。以下每句都应能在 P0 落地后原样复现。
> 方括号是系统内部动作，不直接展示给用户。

### A.1 第一轮

**用户**：这段太拖沓了

**[意图]** `rewrite`；**[槽位]** `target=这段`（上下文 → 第 12 段）、`operation=删减`、`direction=缺`、`scope_intent=局部`。
**[判定]** target 可由上下文唯一确定 → 不因目标发问；direction 缺失但可默认 → **L2**。
**[动作]** 不阻塞，先声明理解，再执行。若上下文里没有「刚看的段」，则降为 **L3**，只问一句范围。

**助手**：

> 我按**第 12 段**来改（你刚看的那段），理解是「删重复心理描写、压缩环境铺垫、保留信息点和结尾钩子」，情节和人物关系不动。
> 现在开始，改完给你对照。

**[执行]**
1. 快照第 12 段（N6）。
2. `POST /api/assistant/invoke-generation`
   `{project_id, action:"regenerate_segment", segment_no:12, instruction:"删减重复心理描写与环境铺垫，保留信息点与结尾钩子；不改变情节与人物关系"}`
3. 读回 `instruction_injected`、新旧字数、旧名计数、AI 腔反馈（N7）。

### A.2 第二轮：回报

**助手**（即 6.4 的模板，含：只动第 12 段 / 理解 / 1120→862 / 开头对照 / 副作用 / 可回滚版本号 / 没做什么）。

### A.3 第三轮：用户纠偏

**用户**：还是太慢，我要的是事件推进快一点，不是句子变短

**[意图]** 同 I3；**[槽位]** `target=第 12 段`（沿用上轮）、`direction=加快事件推进`。
**[判定]** 目标明确、方向明确 → **L1**。
**[执行]** 再次快照 → 重写，`instruction="压缩场景内的等待与铺垫，把关键事件提前到本段前 1/3；保留原有信息点"`。
**[回报]** 同上模板，额外说明「这次改的是事件顺序密度，不是句长」。

### A.4 验收断言（可勾选）

- [ ] 第一轮没有问「你是指节奏还是句子长度」——只声明理解，不阻塞。
- [ ] 第一轮明确说出「第 12 段」，且没有改动其他段。
- [ ] `instruction` 真的进入提示词，回报里有 `instruction_injected=true`。
- [ ] 回报含改前/改后开头对照，不只是字数。
- [ ] 回报含副作用四项：旧名、bible、持有物、后段衔接。
- [ ] 回报明确「只动了第 12 段」，并列出「没做什么」。
- [ ] 回报给得出回滚方式（有版本号或明确说没有）。
- [ ] 第三轮能接住「不是句子变短，是事件推进」，并换指令重做。
- [ ] 若上下文缺失，第一轮只问一个问题，且问题带具体候选（第 12 段 / 最近三段）。

### A.5 变体：全局歧义必须问

**用户**：主角不该这么软

**助手**：

> 这句有两种改法，成本差很多：
> A. 只改第 12 段——把这次的退让写成「有代价的隐忍」；
> B. 改整本设定——把性格/动机改成「隐忍」，影响之后所有生成，**已写的正文不会自动变**。
> 你要 A 还是 B？

**用户**：B

**[执行]** `POST /projects/{id}/bible/apply` 改 `characters[].motivation`（写 revision + 冲突扫描）。
**[回报]** 「已改设定：林砚的动机从『避免冲突』改为『有代价的隐忍』。只影响后续生成；已写的 12 段没有变。要不要我按新设定重写最近 3 段？」

---

## 附录 B：交付前自查（断言 → 证据）

| 方案里的断言 | 证据 | 状态 |
|---|---|---|
| 能重写单段并注入用户要求 | [engine.py:1945-2005](backend/app/generation/engine.py#L1945-L2005) | 现有 |
| 能回报指令是否注入 | [assistant_generation.py:699](backend/app/routers/assistant_generation.py#L699)、[L746](backend/app/routers/assistant_generation.py#L746) | 现有 |
| 重写不级联改后段 | [engine.py:1954-1958](backend/app/generation/engine.py#L1954-L1958) | 现有 |
| 能加/删角色、改境界 | [character_edit.py:66/115/182](backend/app/kg/character_edit.py#L66) | 现有 |
| 改名有影响面门槛 | [character_rename.py:36/140](backend/app/kg/character_rename.py#L36) | 现有 |
| bible 改写带 revision/冲突 | [bible.py:410](backend/app/kg/bible.py#L410) | 现有 |
| 文风档位/蒸馏只影响后续 | [projects.py:119/423](backend/app/routers/projects.py#L119) | 现有 |
| 提案改正文会写 Version 快照 | [proposals.py:892-977](backend/app/agents/proposals.py#L892-L977) | 现有 |
| 重写本身没有快照 | [engine.py:1945-2030](backend/app/generation/engine.py#L1945-L2030) 无版本写入 | 事实（缺口） |
| `/qa` 单轮、无 clarification | [assistant.py:107-119/265-484](backend/app/routers/assistant.py#L107-L119) | 事实（缺口） |
| 写工具不能进 agent 白名单 | [registry.py:71-88/163-167/215-219](backend/app/agents/registry.py#L71-L88) | 现有（设计约束） |
| 无章节实体 | [engine.py:488](backend/app/generation/engine.py#L488)、[models.py:479-489](backend/app/models.py#L479-L489) | 事实（缺口） |
| 无 HTTP reset_chapter | 全仓库仅 [generated_store.py:1447](backend/app/kg/generated_store.py#L1447) | 事实（缺口） |
| 多轮 clarify 骨架可借鉴 | [assistant_wizard.py:711-864](backend/app/routers/assistant_wizard.py#L711-L864) | 现有 |
| 会话字段可承载多轮 | [models.py:1116/1156](backend/app/models.py#L1116) | 现有（需接线） |
| 意图分类/槽位/范围解析 | 无对应模块 | **需新建** |
| 批量重写 | 无端点 | **需新建** |
| 重写后自动副作用审计 | 有规则，无编排 | **需新建接线** |
| 全局偏好跨项目记忆 | 无存储 | **做不到** |
