# 物品持有注入 · 机制层验收（p1 真实生成）

- 日期：2026-11-10
- 项目：p1（`Assistant 测试项目`，id=1），既有数据，未新建项目、未造数据
- 入口：`app.generation.engine.generate_next_segment`（非干跑，真实调用模型）
- Provider：`ZhipuChatProvider` / `glm-4-flash-250414`
- 生成前备份：`backend/backups/novel_dev-pre-inject-accept-20261110-214717.db`

## 0. 结论先行

1. **注入块确实出现在真实生成的 prompt 里**：一次 `chat` 调用的 user prompt 中，第 1282 字符处出现完整的【物品持有事实】块，位置在【世界观事实（已发生…）】整段之后、【续写指令】之前。
2. **块的内容是 7 条 `no_record`**，不是"剑归谁"这类正向持有事实。原因不是链路没接上，而是 `backend/data/generated.db` 的 `kg_item_holding` 里没有 p1 这 7 个物品的任何持有段（60 行全是《大奉》物品、且名称无交集）。
3. **"块出现"成立，"约束被遵守"无法成立也无法证伪**：7 条全是 `no_record`，本层不下发任何正向持有约束；正文既没有写"无人持有 / 已确认无主"，也没有任何归属表述。
4. **一个比"块出现"更值得注意的机制事实**：最终落库的 seg3 并不是带块那次调用的产物。带块的首次生成（1338 字）触发了 AI 味重写，重写的 prompt（system 245 / user 1397）**不包含物品持有块**，最终落库的是重写结果（1262 字，与重写调用输出长度逐字相等）。也就是说：**注入只作用于被丢弃的初稿，最终正文来自一个没有注入块的调用。**

> 第 4 条是缺陷，已在同一分支修复并复验，见 [§8](#8-修复与复验2026-11-10)。本报告 §1–§7 保留修复前的原始验收记录，不作改动。


## 1. 数据前提（未改动）

p1 的 mention 现状（`kg_mentions`，真实回填所得，本次未写入任何一行）：

| 段 | 总数 | character | location | item |
|---|---|---|---|---|
| seg1 | 7 | 1（李浩） | 4（出租屋/公司/会议室） | 3（文件/策划书/笔） |
| seg2 | 12 | 1（李浩） | 4（公司/会议室/办公室/停车场） | 7（文件/策划书/笔/咖啡/手机/电脑/邮件） |

- item mention 合计 10 条，与任务描述一致。
- 当前段 = seg2（`_next_segment_no - 1 = 2`），`_active_item_entries` 取到的正是 seg2 的 7 个 item，块里 7 行与之一一对应。
- `generated.db` 侧：`kg_item_holding` 共 60 行，`group_id` 全非空、`item_name` 为《大奉》物品（地书、镇国剑、朴刀、护心镜等）；对这 7 个名字逐一查询均为 0 行。因此读通道按设计返回 `no_record`。

## 2. 真实生成记录

| 指标 | 值 |
|---|---|
| 总耗时（wall clock） | **67.29 s** |
| LLM 请求数 | **4 次**（2 次 `chat` + 2 次 `summarize`） |
| 落库段 | seg3，id=5，**1262 字** |

调用明细（按发生顺序）：

| # | 类型 | system | user | 输出 | 耗时 | 说明 |
|---|---|---|---|---|---|---|
| 1 | `chat` | 1167 | 1868 | 1338 | 29.12 s | 主生成，**含物品持有块** |
| 2 | `summarize` | — | 输入 1338 | 29 | 2.72 s | 初稿摘要（随后被覆盖） |
| 3 | `chat` | 245 | 1397 | 1262 | 33.18 s | AI 味重写，**不含物品持有块** |
| 4 | `summarize` | — | 输入 1262 | 35 | 1.96 s | 重写后摘要（最终落库） |

补充：
- `summarize` 是真实请求（`OpenAICompatibleChatProvider.summarize` 走 `_complete`），不是本地截断。
- 无 `summarize_rollup` 调用：p1 摘要树无需滚上。
- 无时间线判定调用：p1 无 outline，`_judge_and_advance` 直接 skipped。
- 未捕获 token 用量（provider 的 `last_usage` 只保留最后一次且未在脚本中留存），本报告只报耗时与请求次数。

## 3. 渲染后的物品持有块（一字未改）

### 3.1 原文

```text
【物品持有事实（已写内容最新一段；续写须与此一致）】
- 文件：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 策划书：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 笔：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 咖啡：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 手机：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 电脑：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
- 邮件：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
（本块只补「物品在谁手上」，不改动也不覆盖【剧情圣经】与【世界观事实】的既有约束。三者冲突时按「作者设定 > 世界观事实 > 本块」取信，行文中回避正面矛盾，不要静默改写物品归属。）
```

### 3.2 位置

主调用 user prompt 共 1868 字符，章节顺序与偏移：

| 偏移 | 标题 |
|---|---|
| 0 | 【前情提要】 |
| 81 | 【剧情圣经·动态指引】 |
| 112 | 【检索上下文（参考风格素材，可化用不可照抄）】 |
| **154** | **【世界观事实（已发生，续写须保持一致，不得矛盾）】** |
| 180 | 【近期出场实体】（世界观事实的子块） |
| 1140 | 【近期事件】（世界观事实的子块） |
| **1282** | **【物品持有事实（已写内容最新一段；续写须与此一致）】** |
| 1710 | 【续写指令】 |
| 1807 | 【风格】（在续写指令正文内） |
| 1812 | 【文风要求】（在续写指令正文内） |

- 块占据 `[1282, 1708]`，**紧接在【世界观事实】整段结束之后**（前置邻接是【近期事件】列表最后一行 `- 李浩在会议室中汇报策划书。` + 空行）；
- 块之后**紧接** `\n【续写指令】`，中间没有其他内容。
- 结论：**是**，块位于【世界观事实】之后、【续写指令】之前，与 `prompts.build_user_prompt` 的拼接顺序一致。
- 注意：扫描标题时会在 1638 / 1645 看到【剧情圣经】【世界观事实】，那是块内优先级说明里对既有约束的**引用**，不是新的章节标题。

### 3.3 长度与占比

| 组成 | 字符数 |
|---|---|
| 块标题行 | 26 |
| 7 条事实条目（`format_holding_facts` 输出） | 307 |
| 换行 + 优先级说明 | 93 |
| **块合计** | **427** |

- 占 system + user 总 prompt（3035）：**14.07%**
- 占 user prompt（1868）：**22.86%**

也就是说，在 p1 这个 prompt 里，一个全部是 `no_record` 的块占了近四分之一的 user 上下文。

## 4. 正文有没有用到持有事实

### 4.1 事实：没有正向持有事实可遵守

7 条全部是 `no_record`。按读通道语义，`no_record` 表示"该物品在持有表里一条段都没有"，**不下发持有约束**，只要求不要写成「无人持有 / 已确认无主」。因此这次验收里不存在"某物在某人手上"这类可被遵守或违背的事实。

### 4.2 落库正文的对应片段（seg3，1262 字）

出现的物品（仅作为道具自然出现，无任何归属表述）：

```text
电话铃声打破了宁静，李浩拿起手机，是项目组的负责人。
他拿起手机，给母亲打了一个电话。
他打开电脑，开始研究新的方案。
他坐在电脑前，一遍又一遍地修改着方案。
他终于完成了方案。他看着电脑屏幕上的内容，心中充满了满足。
就在他快要放弃的时候，他突然想到了一个点子。他兴奋地抓起笔，开始在纸上画了起来。
```

| 物品 | 正文命中次数 | 是否涉及持有/归属 |
|---|---|---|
| 手机 | 2 | 否 |
| 电脑 | 3 | 否 |
| 笔 | 1 | 否 |
| 文件 | 0 | — |
| 策划书 | 0 | — |
| 咖啡 | 0 | — |
| 邮件 | 0 | — |

正文中「无主 / 无人 / 持有 / 丢 / 遗失 / 落在 / 归属 / 谁的」出现次数均为 **0**。

### 4.3 判断

- **没有违背 `no_record` 语义**：正文没有把任何物品写成"无人持有 / 已确认无主"，也没有编造归属。
- **但也不能说"遵守了"**：没有正向约束可遵守；上述道具只是沿用前文场景自然出现，不能归因为"读了持有块才这么写"。
- **更关键的是最终正文根本没经过带块的调用**（见 §5.1），所以即便有正向约束，也无法在落库文本上验证遵守情况。

## 5. 单独指出的问题

### 5.1 最终落库正文来自"不带注入块"的重写调用（比"块出现"更重要）

证据链：

1. 第 1 次 `chat` 的 user（1868）含完整块，输出 1338 字；
2. 第 3 次 `chat` 的 system=245 / user=1397，符合 `_rewrite_prompt` 的"问题清单 + 待改正文"形状，**没有任何物品持有块，也没有 constraints**，输出 1262 字；
3. 落库 seg3 长度 1262，与第 3 次输出长度完全相等；`_rewrite_with_evidence` 在重写长度 > 原文 50% 时直接替换正文。

含义：当前链路里，物品持有块只作用于**初稿**；一旦触发 AI 味重写，重写调用看不到块，最终正文与注入内容脱钩。这次 p1 因为全是 `no_record`，看不出后果；如果块里是「镇国剑在镇北王手上」这类硬约束，重写路径就有机会把它丢掉。这是重写路径的覆盖缺口，不是本次要修的范围，但应当记录。

> 已修：重写调用现在携带与初稿**同一份**块与 constraints，复验见 [§8](#8-修复与复验2026-11-10)。


### 5.2 「文件」「邮件」被当作持有物条目

块把「文件」「邮件」与「笔」「咖啡」并列成持有物。这两个词来自抽取器给出的 `kg_entities.kind='item'`，`_active_item_entries` 只按 `kind='item'` 过滤，于是它们原样进入持有块。

- 当前**不构成错误持有事实**：两者都是 `no_record`，块没有断言任何归属。
- 但它说明 item 类目本身有噪声（信息载体/通信内容被当成可持有实体）。一旦将来持有表里出现同名行，这条噪声会直接变成一条错归属注入。
- 另外，「文件」「邮件」作为"物品"被列出来，本身已经让块的一部分内容失去意义——模型读到的是 7 条一模一样的"无记录"。

### 5.3 p1 为什么没有 p5 的档位/指纹问题

p1 的 `reference_novel_ids = None`、`narrative_tier = None`。`_require_distill_ready` 的第一行就是：

```python
ids = [int(i) for i in (project.reference_novel_ids or [])]
if not ids:
    return
```

没有参考语料 → 直接返回，不做档位解析、不算指纹、不查蒸馏库；`_style_narrative` 对无语料项目返回 None。本次 4 次调用里也没有任何蒸馏/滚上请求。

所以 p1 **不是"绕过了"预检，而是本来就不触发预检**。这与 p5 的 balanced/fast 指纹不匹配是两类情况，不能把 p1 的结果当作 p5 链路已验证。

## 6. 约束遵守声明

- 未改任何守卫代码。
- 未向 `kg_mentions` 手工插入任何行（生成前后 p1 仍为 seg1=7、seg2=12，无 seg3 mention）。
- 未新建项目、未造数据。
- 唯一的写操作是这次真实生成本身：新增 `generations` 行（seg3, id=5）、更新 p1 摘要树/扁平摘要、以及生成链路的既有副作用（图谱合并、v3 入队行）。
- v3 批抽 worker 未启动（脚本未调用 `ensure_worker`），没有后台 LLM 抽取混入本次调用统计。
- 生成前完整备份：`backend/backups/novel_dev-pre-inject-accept-20261110-214717.db`。

## 7. 复现材料

- 执行脚本：`D:\Temp\run_inject_acceptance.py`（provider 薄代理，只记录不改请求）
- 捕获产物：`D:\Temp\inject_acceptance_capture.json`（含完整 user prompt、块原文、偏移、逐次调用耗时）
- 关键代码位置：
  - 块生成：`backend/app/generation/engine.py` `_item_holding_text` / `_active_item_entries`
  - 块拼接：`backend/app/generation/prompts.py` `build_user_prompt`
  - 重写旁路：`backend/app/generation/engine.py` `_rewrite_prompt` / `_rewrite_with_evidence`

---

# 8. 修复与复验（2026-11-10）

## 8.1 缺陷一：重写路径丢块（已修）

### 8.1.1 结构原因

块和 constraints 原先是在 `generate_next_segment` / `generate_next_segment_stream` 的**主调用**现场拼的：块由 `build_user_prompt(item_holding_block=...)` 渲染，constraints 由 `provider.chat(..., constraints=...)` 追加。`_rewrite_prompt` 是另一套完全独立的 prompt 拼装，它根本没有这两个入参——所以只要触发重写，正文就被换成一个「没看过块」的调用的产物。

### 8.1.2 所有会产出最终正文的调用路径（逐条）

| # | 路径 | 是否产最终正文 | 修复前是否带块 | 本次改动 | 结论 |
|---|---|---|---|---|---|
| 1 | `generate_next_segment`（同步 / chat） | 是（主调用） | 是 | 仅重写调用点补 `item_holding_block` + `constraints`（engine.py:1576-1580） | 主调用无改动 |
| 2 | `generate_next_segment_stream`（SSE） | 是（主调用） | 是 | 同上，重写调用点（engine.py:1765-1769） | 主调用无改动 |
| 3 | `_rewrite_with_evidence`（两入口共用的 AI 味重写） | **是**（替换正文） | **否** | 新增 keyword-only `item_holding_block` / `constraints`；透传给 `_rewrite_prompt`；`provider.chat()` **不再**传 `constraints`；`rewrite_info` 新增 `item_holding_injected` / `constraints_injected` 两个观测键 | **核心修复** |
| 4 | `_rewrite_prompt`（重写 prompt 拼装） | —（产 prompt） | **否** | 块走 `render_item_holding_section`（与生成同一个渲染函数），有块时 system 增第 8 条「不得改动物品归属」；constraints 经 `render_constraints` 放进 **system** | 块与生成路径共用一份渲染，杜绝两处措辞漂移 |
| 5 | `regenerate_segment_stream`（重生成单段） | 是 | **是** | 无 | 它经 `_build_generation_prompts` → `build_user_prompt(item_holding_block=_item_holding_text(...))` 自带块，constraints 走 `_constraints_from` + `stream_chat(constraints=)`；且**不做**自动 AI 味重写（只置 `rewrite_info.alert`），不存在"替换后丢块" |
| 6 | `_apply_era_guard` → `era_check.apply_to_text` | 可能（`ERA_GUARD_MODE=rewrite`） | 不适用 | 无 | 默认 `report` 只扫描不改正文；`rewrite` 模式是**句级**替换（逐句改写 + `_rewrite_acceptable` 长度/术语校验），不重组全文、不重发整段 prompt，因此不存在"新 prompt 没带块"的路径 |
| 7 | `generate_plan`（预检/预览 API，routers/generation.py:220） | 否（不落正文） | **否** | 补 `item_holding_block=ctx.get("item_holding_block")` | 不产正文，但提示词必须与真实生成同源，否则人工审阅看到的不是实际下发的那份 |
| 8 | `_judge_and_advance` / `_merge_segment_into_graph` / `_enqueue_v3_extraction` | 否 | — | 无 | 只读正文或写簿记，不改正文 |

附带的正文清洗（`_strip_thinking`）：模型仍可能把 constraints 回显到正文尾部，因此先整行剥掉以 `【本段已锁定角色` / `【尚未兑现的伏笔` 开头的尾行，再走原 `【本节完】` 正则。只作用于**末尾连续行**，正文中间的同名内容不受影响。

### 8.1.3 constraints 为什么放 system 而不是 user 末尾

修复初版是把 `constraints=` 继续交给 provider 走 `_apply_constraints`（追加 user 末尾），实测**污染正文**：落库段末出现

```text
【本节完】
【本段已锁定角色】李浩、项目
【尚未兑现的伏笔】汇报怎么样？
```

根因：重写的 user 末尾是「待改正文」，锁定名单排在正文之后，模型把它当成正文的一部分原样输出。改为渲染进 system 后复验，落库正文三处标记均为 `False`。

### 8.1.4 生效证明（同一次真实生成）

复验方式：把 p1 回滚到 2 段（`novel_dev-pre-inject-accept-20261110-214717.db`），用 provider 薄代理（只记录、不改请求）真实调 `generate_next_segment`，触发重写后落库 seg3。

| 证据 | 值 |
|---|---|
| 主调用 `chat#0` | system 1167 / user 1623 / prompt 2790；块位于 user 偏移 **1282**，块长 **182**，占 prompt **6.52%** |
| 重写调用 `chat#2` | system 399 / user 1431 / prompt 1830；块位于 user 偏移 **49**，块长 **181**，占 prompt **9.89%** |
| 两处块是否同一份 | `rstrip("\n")` 后**完全相等**（182 vs 181 只差拼接处的换行） |
| 重写 prompt 可重建性 | 用捕获到的输入调 `_rewrite_prompt(...)`：system **399 == 399**、user **1431 == 1431**，且 user **逐字节相同**。说明捕获到的重写 prompt 就是「带块 + 带 constraints」构造出来的那一份 |
| 落库正文来源 | `_strip_thinking(chat#2 输出) == 落库 seg3` 为 **True**（原始 1116 字含 `【本节完】`，剥离后 1109 字） |
| `rewrite_info` | `original_chars=1193`、`rewritten_chars=1109`、`item_holding_injected=True`、`constraints_injected=True`，reasons 为「AI 腔模式命中 8 次」 |
| 落库正文污染检查 | `【本节完】` / `【本段已锁定角色` / `【尚未兑现的伏笔` 三者均 **False** |

结论：**同一次生成里，重写前（主调用）与重写后（重写调用）的 prompt 都拿到了同一份块；且落库正文来自带块的重写调用。** §5.1 的缺陷不复现。

复现材料：执行脚本 `D:\Temp\run_inject_acceptance2.py`，捕获产物 `D:\Temp\inject_acceptance_capture2.json`（完整 system/user、块原文与偏移、逐次耗时、`rewrite_info`、落库正文），重建校验脚本 `D:\Temp\recheck_rewrite_prompt3.py`。

### 8.1.5 测试

| 套件 | 结果 |
|---|---|
| `pytest tests/test_v26_engine_integration.py tests/test_kg_item_holding_read.py -q` | exit 0，收集到的 14 个用例全通过 |
| `python tests/test_v26_stop.py` | 24 通过 / 0 失败 |

新增用例 `test_rewrite_carries_item_block_and_constraints` 覆盖：有块时 system 含第 8 条、user 含块且在「请改写」之前；无块时 system 不加第 8 条、user 不含块；假 provider 断言 constraints 不再走 user 末尾、system 含锁定名单与伏笔；静态断言两个调用点都传了 `item_holding_block=ctx.get(...)`。

## 8.2 缺陷二：全 `no_record` 压缩为一行（已修）

改动在 `backend/app/kg/item_holding_read.py` `format_holding_facts`：当**所有**条目都是"空记录"（`no_record` 或没有明细行）**且没有 note** 时，合并成一行；只要有一条是 held / sealed / unowned / unknown、或带 note，就逐条列明细（原样不变）。

压缩后（p1 实测，7 个物品）：

```text
- 文件、策划书、笔、咖啡、手机、电脑、邮件：均无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
```

语义保住：仍然是「本层不提供持有约束」，并保留「不要写成已确认无主」这一条禁令。

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 事实条目区字符 | 307（7 行） | **62**（1 行） |
| 渲染块合计（含标题与优先级说明） | 427 | **182** |
| 占 user prompt | 22.86%（427/1868） | **11.21%**（182/1623） |
| 占 system+user 总 prompt | 14.07%（427/3035） | **6.52%**（182/2790） |

混合状态（held + no_record）、单条带 note 两种情形各有单测覆盖，均**不压缩**。

## 8.3 「文件」「邮件」类目噪声：评估结论（未改代码）

**结论：治理点应在 `kind='item'` 的判定侧，不应在注入侧加词表过滤。**

理由：

1. **注入侧过滤只是止血**。`_active_item_entries` 只按 `kind='item'` 取实体；如果在注入侧按"抽象名词黑名单"剔除，只保护了持有块这一个消费者，`kg_entities`、检索切片、图谱视图仍会把「文件/邮件」当物品，后续每接一个消费方都要再过滤一次。
2. **注入侧过滤会误伤**。真正可持有的「信件」「合同」「档案袋」与「邮件」在字面上高度接近，黑名单无法区分"信息载体"与"信息内容"；一旦误杀，持有表里真有同名行时反而漏注入——从"错归属"变成"漏归属"，两者都是缺陷。
3. **正确的闸门是"可被持有 / 可被交接"**。抽取器给 `kind='item'` 时应要求实体是一个**可被别人拿在手上、可丢失、可转交的物理对象**；「文件」「邮件」「策划书」「资料」「消息」「内容」属于信息载体/通信内容，应归到独立类目（如 `kind='info'`）或不入 `item`。
4. **成本在存量**。改判定会影响既有项目的 entity/mention 与图谱统计，需要一次回填/迁移，且会改变已有项目的块内容——所以不适合跟着本次缺陷修复一起做。

**结论：建议做 (3)，把抽象名词/信息载体从 `item` 判定里排除（或在抽取 prompt 与归一表里显式排除）；在 (3) 落地前，可先只在持有读取侧对"抽象词 + 持有表无同名行"的条目降级为 `no_record` 作为临时防线。本次未动任何抽取判定、未加任何过滤词表。**

## 8.4 p1 是否重跑、新增数据与回滚

**是否需要再跑一次对照：不需要。** 本轮修复后的复验**已经**是一次真实生成，并且同一次生成里同时捕获了主调用与重写调用两条 prompt（§8.1.4），对照数据已经拿到。再跑一次只会新增一组同类数据，不会增加结论强度。如果要更严的证据，更有价值的是等「持有表里真有 held 行」的项目（不是 p1）上验一次遵守情况，那属于另一个验收目标。

**本次新增的数据**（相对 `novel_dev-pre-inject-accept-20261110-214717.db`）：

| 表 | 变化 |
|---|---|
| `generations` | +1：seg3，id=5，1109 字 |
| `projects`（p1） | `summary` 311→471、`summary_tree` 410→575 字符 |
| `facts` | +3 |
| `kg_nodes` | +3 |
| `agent_runs` | +1：id=76，`role='kg_batch_extract'`，`status='running'`（v3 批抽入队行；worker 未启动，不会自动执行） |
| `backend/data/generated.db` | **无变化**（mtime 仍为 21:06:04，早于本次生成） |

**回滚方式**：停掉 v3 批抽 worker 后，用 `backend/backups/novel_dev-pre-inject-accept-20261110-214717.db` 覆盖 `backend/novel_dev.db` 即可回到 2 段状态（seg1=7 / seg2=12 mentions）。`generated.db` 无需回滚。

**不要**用 `backend/backups/novel_dev-pre-rewrite-fix-20261110-223500.db` 当回滚点：它是"修复后重跑之前"的快照，里面带着缺陷版的 seg3（1262 字，正文来自不带块的重写调用），正是本次要消除的状态。

