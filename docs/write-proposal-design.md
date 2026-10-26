# 写工序提案化：让 agent 能「提」，不能「做」

> 这一片解决真书全流程（`docs/realbook-full-flow.md`）暴露的最后一个结构缺口：
> **工序链的写工序没有写路径**。agent 看得见「写正文 pending」，
> 却没法提一个「先规划大纲」的提案 —— 于是人只能自己想到那一步，
> 或者干脆卡死在链上。
>
> 状态：**设计稿，待审阅**。实现顺序在 §8，每片都能独立验证。

## 1. 为什么要（实测证据，不是设想）

真书全流程第一次跑失败：项目给了 premise + plot_bible，**没给大纲**。

```text
工序链：write_body pending「还没有正文 —— 按你定的章数与字数写。」
实际：  写了正文也过不了正文审查 ——
        没有 outline → 没有时间线 → 没有段契约 →
        封闭清单审计零候选（missing_saved_authority）→ 覆盖不完整 →
        整段验收 no_pending_acceptance → 链走不到收敛。
```

**这里有两个问题，第二个更隐蔽**：

1. agent 没有提「先规划大纲」的能力（写工序 `write_proposal=None`）；
2. **工序链的判据在把人引向错误的动作**。`write_body` 只判「有没有正文」，
   不判「有没有段契约」——于是它说「去写正文」，而写了也白写。

第 2 条是本设计里**先改**的部分（§6），因为它不需要新机制，
却能立刻让链条说对话。

## 2. 提案分两类：**文本提案**与**动作提案**

现在只有一类提案（`EditProposal`），它天生是「**改哪些字**」的：

```text
EditProposal: type ↔ target_kind 一一对应
  text_edit        → generation_text
  prompt_edit      → prompt_override
  fact_resolution  → project_plot_bible
  corpus_text_edit → novel_chunk_text
```

它有 `before_text` / `after_text` / `scope_json.base_content_hash`（漂移基线），
`write_target()` 只认这四种内容。**启动蒸馏、规划大纲、调动生成都不是「改字」**：
没有 before/after，漂移语义也不同。

硬塞进 EditProposal 会让「这个提案到底改哪儿」变成需要读代码才能回答的问题 ——
那正是 `proposals.py` 里那条注释要防的事。

**所以分两类，共用同一套审批闸门**：

| | 文本提案（现有） | 动作提案（新增） |
|---|---|---|
| 回答的问题 | 改哪些字 | 做什么事 |
| 落点 | `EditProposal` | `ActionProposal`（新表，轻量） |
| 漂移语义 | 目标内容变了（`base_content_hash` 不匹配） | **前提**变了（语料指纹/正文状态/规划额度） |
| 例子 | 改第 3 段的一处措辞 | 启动蒸馏、规划大纲、扩展规划、调动生成 |

## 3. 动作提案表（设计）

```text
ActionProposal
  id, project_id, run_id          # run_id = 提它的 agent 那一轮（审计链）
  kind                           # start_distill | plan_outline | extend_plan | invoke_generation
  title                          # 一句话说清要做什么（人靠它判断要不要批）
  detail_json                    # 给人看的补充（档位、将写进哪、依据）
  endpoint, method, body_json    # 批准后原样发的请求（服务端拼好，前端不参与）
  precondition_fingerprint       # 「前提」的指纹 —— 漂移检测用
  precondition_note              # 人话：这条提案的前提是什么
  risk_level                     # low | medium | high
  status                         # pending | approved | rejected | applied | stale
  created_at, approved_at, applied_at, applied_result_json
```

**状态机**（与 EditProposal 同构，但只到 applied）：

```text
pending ──approve──> approved ──apply──> applied
   │                     │
   └──reject──> rejected └──apply 时前提不符──> stale（前提变了，要重新提）
```

**漂移语义**是动作提案的核心。举例：

| kind | 前提指纹算的是什么 | 什么算「前提变了」 |
|------|-------------------|-------------------|
| `start_distill` | 语料指纹（语料 novel id + 章节集合 + 清洗版本） | 语料在等待期间被清洗过/换过 → 蒸馏结果会与现状不符 |
| `plan_outline` | 项目还没有 outline（且 premise/plot_bible 未变） | 期间已经有人填了大纲 → 这条提案过时 |
| `extend_plan` | 规划用尽的状态（已用节点数 + 已有正文段数） | 已经写完了/又推进了 → 追加的节点接不上 |
| `invoke_generation` | 当前已写段数 + 规划状态 | 期间又写了段 → 这条「续写一段」的落点变了 |

**不匹配 → 置 `stale` + 409**，和 EditProposal 一样：**不静默重算、不覆盖**。
「提案过期」这个事实本身要留痕（单独 commit，不随 apply 失败回滚）。

## 4. 五种动作提案逐个说清

### 4.1 `start_distill` —— 启动文风蒸馏

- **谁提**：工序链走到「③ 文风蒸馏」且档案不在位时，agent 提。
- **前提**：语料指纹 + 目标档位。
- **apply**：`POST /api/projects/{id}/distill`（端点已存在，决策点 `DISTILL_TIER` 已在用）。
- **档位从哪来**：**人定**。这里不新增问法 —— 决策点注册表已经有
  `DISTILL_TIER`（完整/平衡/快速），agent 提卡片时把
  「档位待选」写进 detail，用户可以在同一张卡上选完再批。
  也就是说：**动作提案与选择框要能叠**（见 §7）。

### 4.2 `plan_outline` —— 规划大纲（**本片最关键的一个**）

- **谁提**：工序链走到「⑥ 写正文」且**没有段契约**时，agent 提。
- **前提**：项目没有 outline；premise/plot_bible 存在。
- **agent 起草什么**：从 premise + plot_bible 起草一份**剧情大纲**
  （若干条，每条一个剧情点：谁、在哪、做什么、结束状态）。
  草稿是**提案内容**，不是已生效的设定 —— 人批了才写进项目。
- **apply**：`PATCH /api/projects/{id}`（写 outline）→ 随后由引擎规划时间线
  （`engine._timeline_state_for` → `plan_nodes_with_llm`）产出**段契约**。
- **回灌**：人改了草稿再批 → 写进去的是**人改过的**版本，不是 agent 的原版。

### 4.3 `extend_plan` —— 扩展规划

- **谁提**：生成被拒（409 `plan_exhausted`）时，agent 提。
- **前提**：规划用尽状态。
- **apply**：`plan_extension`（`app/agents/plan_extension.py` 已有）——
  它已经守住了「只追加、不改已有契约、越界即失败」三条纪律。
- **为什么要提案化**：它会**新增契约节点**，等于给正文开新口子 —— 属于写动作，
  该走人批。

### 4.4 `prompt_override` —— 覆写系统提示词

- **能力已有**：`EditProposal` 的 `type='prompt_edit'` / `target_kind='prompt_override'`
  早就支持（`_coerce_after_content` 与 `write_target` 都认）。
- **缺的是**：agent 侧能提（现在只有 invoke_generation 会提卡）+ 工序链接线
  （「⑤ 提示词体检」那步的 `write_proposal='prompt_override'`）。
- **结论**：**不新建类型**，直接用文本提案通道。

### 4.5 `invoke_generation` —— 调动生成

- **能力已有**：`pending_actions.propose_invoke_generation`（重写某段/续写下一段）。
- **缺的是**：工序链「⑥ 写正文」的 `write_proposal=None` 没引用它。
- **结论**：同样**不新建**，接线即可。

## 5. agent 侧：只读工具给「能提什么」，写工具只提不执行

硬边界不变：**agent 只能提，不能执行**。所以工具分两半：

| 工具 | 读/写 | 作用 |
|------|-------|------|
| `get_workflow_state`（已有） | 只读 | 工序链状态；**每道写工序带上它能提的 kind 列表** |
| `propose_action`（新） | **只写提案表**，不动业务表 | 提一条动作提案，返回提案 id 与人话摘要 |
| `propose_edit`（新） | **只写提案表**，不动业务表 | 提一条文本提案（正文/提示词/设定/语料） |

**为什么不用现成的 `prepare_body_repair` 那套「只读构造卡片」模式**：
`prepare_body_repair` 是「只读构造待确认卡，不落库」；动作提案**要落库**
（人批的是库里那条记录，apply 时才知道改的是哪一版）。
所以两者形态不同：**卡片是临时的，提案是持久的**。

**执行仍然只有人点确认后由前端发请求**：
卡片契约 `{kind, title, detail, endpoint, method, body}` 原样复用，
前端多认一个 `kind='action_proposal'`（渲染方式与现有 proposal 卡片一致），
**不新增交互形态**。

## 6. 工序链的接线与判据修正

### 6.1 判据先修（不需要新机制，但必须先修）—— ✅ 已做

`write_body` 的判据要从「有没有正文」改成「有没有**可被审查的**正文」：

```text
现在：  没有正文 → pending「还没有正文 —— 按你定的章数与字数写。」   ← 把人引向错误的动作
应该是：没有段契约的来源（没有大纲）→ blocked「还没有剧情大纲 —— 写出来的正文过不了正文审查」
        有段契约、没有正文 → pending「还没有正文 —— 按你定的章数与字数写。」
        写了正文但某段没有冻结契约 → blocked「已写出 N 段，但第 X 段没有对应的段契约」
```

**为什么这是「先修」**：不修的话，agent 提不出动作提案时，
链上显示的仍然是「去写正文」——人照着做，写完才发现过不了审查。
**判据说错话，agent 的派发建议也会错**（工序链是 agent 判断下一步的唯一依据）。

**在真书数据上实测**（写了 2 段正文、但没有大纲的项目）：

```text
修前： write_body pending「还没有正文 —— 按你定的章数与字数写。」   ← 事实上已经有 2 段了
修后： write_body  blocked「先补段契约」已写出 2 段，但第 1、2 段没有对应的段契约
                     —— 正文审查过不去（先补规划）。
      review_body blocked「先补段契约」正文审查没有判据基准
                     （封闭清单审计会判成 missing_saved_authority）。
```

**段契约的判定口径必须与正文审查同源**：
`project.plot_timeline['planning']['segment_contracts'][段号]`
（与 `app.agents.body_consistency.read_body` 一致）。
自己另算一套就会与审计判据对不上 —— 那正是「查错对象」的坑。

**「没有大纲就不许生成」那道闸门不在这一片**：先关门不给出路是死路 ——
要等片 4/5（agent 能提大纲提案）再上，那时才是「门槛 + 回灌让执行方自改」。

### 6.2 写工序的 `write_proposal` 改成「能提哪几类」

现在是一个字符串（`'start_distill'` / `'prompt_override'` / `None`），
一个工序可能要提好几种（写正文可能要提 `invoke_generation`，
也可能提 `plan_outline` / `extend_plan`）。改成注册表：

```text
STEP_WRITE_KINDS: dict[step_id, tuple[kind, ...]]
  clean_check     → ('ingest_clean',)
  distill         → ('start_distill',)
  review_narrative→ ('start_distill',)
  review_prompt   → ('prompt_override',)
  write_body      → ('plan_outline', 'extend_plan', 'invoke_generation')
  review_body     → ('close_acceptance',)
  repair          → ('apply_body_repair',)
```

`get_workflow_state` 的每道工序把这份列表返回给 agent，
agent 据此决定「这一步我该提什么」——**服务端说了算，agent 不自己发明**。

### 6.3 动作提案的 apply 要能被界面看到

面板的「等着你处理的改动」区现在只显示 `EditProposal`（文本提案）。
动作提案要**一起显示**，但措辞不同（动作提案不是「改哪儿」，是「做什么」）：
「要启动一次完整档蒸馏」「要写一份剧情大纲」。

**批准 → 应用仍是两步**（沿用面板已有的约定，不合并成「一键生效」）。

## 7. 动作提案与选择框怎么叠

`DISTILL_TIER` 那类决策点是**选择框**（kind='choice'），
动作提案是**待确认卡片**（kind='action_proposal'）。两者可能同时出现
（要启动蒸馏，但档位还没定）。

**建议**：**一张卡片里同时带选项**——`detail.options`（复用选择框已有的形状）
+ `body_patch`。用户点一个选项 → 前端把 `body_patch` 合并进 `body` → 点确认 → 执行。
**不弹两张卡**（两张卡要人来对应，顺序一错就批错动作）。

这要求选择框的 `Option.body_patch` 能作用到动作提案的 body 上 ——
现有实现里 choice 卡的 `body` 是**基体**、`body_patch` 是**补丁**，语义正好对得上。

## 8. 实施顺序（每片都能独立验证）

| 片 | 内容 | 验收方式 | 状态 |
|----|------|---------|------|
| 1 | **判据修正**：`write_body` 看段契约；正文审查说清「没有契约」 | 工序链在无大纲项目上说对话；真书项目复现两次 | ✅ 已做 |
| 2 | `ActionProposal` 表 + 状态机 + 漂移检测（不接 agent） | 单测：状态机合法性、漂移 → stale、approve/apply 分两步 | ✅ 已做 |
| 3 | `POST /api/assistant/action-proposals`（创建）+ `approve\|reject\|apply` | API 级测试 + 面板能显示动作提案 | ✅ 已做（后端；面板待接） |
| 4 | agent 工具 `propose_action`（只提不执行）+ 工序链 `STEP_WRITE_KINDS` | agent 走到「没有大纲」那步能提出 `plan_outline` | ✅ 已做 |
| 5 | `plan_outline` 的 apply（写 outline → 触发时间线规划 → 产契约） | 真书全流程**不填大纲**也能从卡上批出大纲并走完九道工序 | ✅ 已做（端到端见下） |
| 5b | 生成闸门加「没有大纲就 409」 | 有出路了再关门（片 4/5 之后） | ✅ 已做 |
| 6 | `start_distill` / `extend_plan` 接入；`prompt_override` 与 `invoke_generation` 接线 | 三类动作各走一遍「提 → 批 → 应用」 | ✅ 已做（执行器 + agent 侧；`prompt_override` 走文本提案通道，另议） |
| 7 | 选择框与动作提案合并成一张卡 | 一次点击完成「选档位 + 批启动」 | ✅ 已做 |
| 8 | 面板显示动作提案（与文本提案措辞不同） | 面板上能看到「要启动一次完整档蒸馏」 | ✅ 已做 |

**片 1 和片 2 是独立的**，可以先做：片 1 立刻让链条说对话（已做），
片 2 是新表（不碰既有表的风险）。

## 6.5 片 5 的端到端验证（真通道 + 真书）

跑的是真接口（`TestClient` + lifespan，与 uvicorn 同一套路由）与真实 writer：

```text
【提提案之前】
  write_body  blocked / 还没有剧情大纲 —— 写出来的正文过不了正文审查
                            （正文审查要拿每段的段契约当判据基准）。
  当前步: write_body

【agent 构造的卡】kind=action_proposal endpoint=/api/assistant/action-proposals
  库里动作提案行数 = 0（只构造，没落库）✓
  提案已创建：id=1 status=pending
  前提：项目还没有剧情大纲，且设定与人物没变
  已批准 status=approved
  已执行：大纲已写入。下一步写正文时，引擎会据此规划剧情节点并冻结段契约。

【写正文】HTTP 200

【提案之后】
  write_body       done     已写出 2 段。
  review_body      pending  还没做整段验收（要先跑闭环审计）。
  当前步: review_body     gates: []

  时间线节点: ['n1', 'n2', 'n3']
  冻结段契约: ['1', '2']
```

**这条链现在闭合了**：链上说「缺大纲」→ agent 起草一份并提出来 → 人确认 →
大纲写进项目 → 写正文时引擎规划出 3 个节点并冻结 2 份段契约 → 正文审查有了
判据基准 → 链走到「正文审查」。

完整日志：`docs/action-proposal-flow-log.txt`。

## 6.4 实现时发现的一件事：agent 不能有写工具

Assistant 的工具注册表**硬拒** `read_only=False` 的工具（还有工具名前缀黑名单：
`write_` / `commit_` / `update_` / `delete_` / `patch_` / `rollback_` / `apply_proposal`）。
这不是疏忽，是那条硬边界在代码里的落点。

所以 `propose_action` **不能**直接写提案表 —— 它只能**构造待确认卡**
（与 `prepare_body_repair` 同一形态：只读构造，不落库）。落库发生在
**用户点确认**之后：前端把卡里的 body 原样发给 `POST /api/assistant/action-proposals`，
服务端才创建 pending 的提案行。

于是「提」与「做」是**三次点击**：

```text
agent 提（只构造卡，不落库）
   → 用户点「确认」   → 创建 pending 提案行
   → 用户点「批准」   → approved
   → 用户点「执行」   → applied（前提变了就 409 stale）
```

这比设计稿里写的「两次点击」多一次 —— 但它换来了「agent 永远碰不到写路径」
这条边界不被破。**宁可多一次点击，不动那条边界。**

## 6.6 片 7：选项与动作并成一张卡

「要启动蒸馏，但档位没定」是常态。弹两张卡（一张选择框 + 一张动作卡）要人来
对应，**顺序一错就批错动作**。所以选项并进同一张卡：

- 选项来自**决策点注册表**（`DISTILL_TIER`：完整/平衡/快速），不在动作侧另造；
- 选项的 `body_patch` 由服务端**嵌好**（作用在动作自己的请求体上），
  前端只做「合并 + 发送」，不参与构造；
- 补丁里放的是**完整**的动作体而不是只放改动的那个字段 ——
  前端做的是**浅合并**（`{...card.body, ...option.body_patch}`），
  只放增量会把动作体里其它字段一起换掉（比如把 `force: false` 弄丢）。

## 6.7 又挖出一个真问题：当前步取错了

链上有个「当前步」= 下一步该做哪件事。它原本取的是「**第一个被卡住的**工序」——
但被卡住的那步可能在**后面**：「写正文」缺段契约会被卡住，而前面还有没做的
工序（蒸馏）。于是链说「卡在写正文」，人就去解决写正文，其实应该先蒸馏。

**改成取「链上第一个还没做完的工序」**（按顺序），被卡住的后续工序在摘要里
以「另有 N 处待处理：…」带出来。判据说错话，派发建议也会错（工序链是 agent
判断下一步的唯一依据）。

## 9. 这一轮不做的

- **不放松「agent 只读」这条硬边界**（写一律走提案，人批）；
- **不改 EditProposal 的四类文本提案**（不塞动作进去）；
- **不引入第二套审批流**（动作提案复用同一套「批准 → 应用」两步与 409 stale 语义）；
- **不让 agent 自己批准自己的提案**（提出与批准必须不同人/不同次点击）。
