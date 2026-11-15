# 用户问题地图（对话式创建）

> 范围：`backend/app/wizard_story.py` / `wizard_flow.py` / `wizard_chat.py` / `routers/assistant_wizard.py`
> 方法：用临时探针脚本对 `chat.parse()` 逐条实测 32 种说法（不连 DB、不跑生成），
> 结论以实测返回的 `kind` 为准；代码位置按 2026-11-14 工作副本行号标注。

## 图例

| 标记 | 含义 |
|---|---|
| ✅ 答对 | 按用户意图接住，且落到正确的步骤/字段 |
| 💬 转解释 | 不推进流程，交给 `explain()`；有固定知识就答对，没有就由助手模型泛答 |
| ⚠️ 答偏 | 有响应，但把话塞进了错误字段（污染 `premise` / 主角 / 世界观） |
| ❌ 卡住 | 落到兜底话术，重复问同一题，用户会以为没听懂 |

## 1. 定位类（想写什么）

| 用户说法 | 当前位置 | 实际行为 | 判定 | 代码 |
|---|---|---|---|---|
| 我要写一篇小说 | branch | `answer: original`，下一题 `genre` | ✅ | [wizard_chat.py L532-L544](backend/app/wizard_chat.py#L532-L544) |
| 写个科幻悬疑 | branch | 无「小说」字样也按原创接住，并抽出题材多选 | ✅ | [wizard_chat.py L540-L543](backend/app/wizard_chat.py#L540-L543) |
| 帮我写个爽文 | branch | `clarify`：方向还没定 | ❌ | 模式只认「写+小说/故事/书」：[wizard_chat.py L206-L219](backend/app/wizard_chat.py#L206-L219)；「爽文」只在 [wizard_story.py L70-L74](backend/app/wizard_story.py#L70-L74) 当节奏词 |
| 现在什么题材好卖 | branch / genre | `question` → `_market_answer()`：引用有效行情卡给 2–3 个方向，每条带来源/采集日/置信度/时间窗；卡过期则退回原解释 | ✅ | [wizard_chat.py L1005-L1031](backend/app/wizard_chat.py#L1005-L1031) |
| 什么主角比较讨喜（在 protagonist） | protagonist | `question` → `_market_answer()`：列该题材的人设方向，并明确「编辑口径，不是读者统计」 | ✅ | 同上 |
| 想写悬疑还是言情？ | genre | `question` → 解释，不落题材 | 💬 | [wizard_chat.py L778-L779](backend/app/wizard_chat.py#L778-L779) |

## 2. 约束类（写作边界）

| 用户说法 | 当前位置 | 实际行为 | 判定 | 代码 |
|---|---|---|---|---|
| 3000 字 / 短一点 | length | `answer: 3000 / 1500` | ✅ | [wizard_chat.py L631-L639](backend/app/wizard_chat.py#L631-L639) |
| 30 万字，日更 | 任意故事步 | 抽成 `book_length`/`cadence`，提交写 `target_length` | ✅ | [wizard_story.py L267-L315](backend/app/wizard_story.py#L267-L315) |
| 我只有 30 万字预算（在 genre） | genre | `clarify`；信号有 `book_length` 但当前步无值，且路由只在 answer/bypass 才预填 | ❌ | [wizard_chat.py L744-L754](backend/app/wizard_chat.py#L744-L754)、[assistant_wizard.py L998-L1010](backend/app/routers/assistant_wizard.py#L998-L1010) |
| 日更（在 length 步） | length | `clarify`：只说明每段字数 | ❌ | [wizard_chat.py L631-L639](backend/app/wizard_chat.py#L631-L639) |
| 番茄还是起点？ | style | `question` → 解释两站差异 | 💬 | [wizard_chat.py L611-L629](backend/app/wizard_chat.py#L611-L629) |
| 能不能带感情线 | genre | `answer: 言情` | ⚠️ | 同义词把「感情」映射成言情：[wizard_chat.py L587-L604](backend/app/wizard_chat.py#L587-L604) |
| 不要虐 / 别开后宫 | 任意故事步 | 无「雷点」字段；落在自由文本步会被整句当答案 | ⚠️ | 自由文本直返：[wizard_chat.py L607-L609](backend/app/wizard_chat.py#L607-L609) |

## 3. 内容类（设定与写法）

| 用户说法 | 当前位置 | 实际行为 | 判定 | 代码 |
|---|---|---|---|---|
| 主角叫林深，28 岁，前刑警 | protagonist | `林深：28 岁，前刑警` | ✅ | [wizard_story.py L196-L238](backend/app/wizard_story.py#L196-L238) |
| 帮我起个书名（在 premise） | premise | 整句被当核心冲突 | ⚠️ | 自由文本直返：[wizard_chat.py L607-L609](backend/app/wizard_chat.py#L607-L609) |
| 帮我起个名字（在 protagonist） | protagonist | 整句被当主角名 | ⚠️ | 同上 |
| 金手指能不能改成系统 | power | `answer: 能不能改成系统`（截掉了标签，但这是修改意图） | ⚠️ | [wizard_story.py L354-L360](backend/app/wizard_story.py#L354-L360) |
| 开篇怎么写出钩子 / 卡文怎么办 | 任意 | `question` → FAQ 无此项 → 助手模型泛答 | 💬 | [wizard_chat.py L1034-L1102](backend/app/wizard_chat.py#L1034-L1102) |
| 这段好不好看（在 world / premise） | world / premise | 整句被当世界观 / 核心冲突 | ⚠️ | 同上自由文本直返 |
| 这段好不好看（在 outline / confirm） | outline / confirm | `clarify` 兜底话术 | ❌ | [wizard_chat.py L649-L654](backend/app/wizard_chat.py#L649-L654)、[L680-L683](backend/app/wizard_chat.py#L680-L683) |

## 4. 返工类（写完之后不满意）

| 用户说法 | 当前位置 | 实际行为 | 判定 | 代码 |
|---|---|---|---|---|
| 把第 3 段重写，太水了 | chapters / length / 任意带改写动词 | 明确说明「引导不改正文」，指到工作台助手并先弹确认 | ✅ | [wizard_chat.py L394-L440](backend/app/wizard_chat.py#L394-L440) |
| 太水了，重写（不带段号，在 premise） | premise | 整句被当核心冲突 | ⚠️ | 自由文本直返：[wizard_chat.py L607-L609](backend/app/wizard_chat.py#L607-L609) |
| 主角太蠢了，换一个（在 protagonist） | protagonist | 整句被当主角名 | ⚠️ | 同上 |
| 配角抢戏 / 节奏太慢 | 任意故事步 | 无对应意图，落到自由文本 | ⚠️ | 同上 |

## 5. 元问题类（关于工具本身）

| 用户说法 | 当前位置 | 实际行为 | 判定 | 代码 |
|---|---|---|---|---|
| 你能写多少字 | confirm | `question` → `_FAQ_LENGTH`（讲的是单段字数语义） | 💬 部分答偏 | [wizard_chat.py L983-L984](backend/app/wizard_chat.py#L983-L984) |
| 上一章写过什么，你还记得吗 | confirm | `question` → FAQ 无历史正文事实 | ⚠️ 可能泛答 | [wizard_chat.py L1034-L1102](backend/app/wizard_chat.py#L1034-L1102) |
| 会不会抄别人的书 | confirm | `clarify` 兜底；`refusal()` 只拦「要求抄」 | ❌ | [wizard_chat.py L443-L449](backend/app/wizard_chat.py#L443-L449) |
| 版权归谁 | confirm | `clarify` 兜底 | ❌ | [wizard_chat.py L680-L683](backend/app/wizard_chat.py#L680-L683) |
| 人设会不会前后不一致 | confirm | `clarify` 兜底 | ❌ | 同上 |

## 6. 难缠说法专项

| 说法 | 当前行为 | 判定 | 关键实现 |
|---|---|---|---|
| 随便 / 你看着办（故事步） | `clarify`：给 2–3 个方向，不落默认、不推进 | ✅ | [wizard_chat.py L729-L740](backend/app/wizard_chat.py#L729-L740) |
| 随便（字数/段数） | 取默认（3000），并说明「按默认走了」 | ✅ | [wizard_chat.py L756-L776](backend/app/wizard_chat.py#L756-L776) |
| 一口气塞多个信息 | 当前步作答 + 其余步预填 + `_auto` 跳过，回执列出「已记下 / 还差」 | ✅ | [assistant_wizard.py L129-L171](backend/app/routers/assistant_wizard.py#L129-L171)、[L998-L1010](backend/app/routers/assistant_wizard.py#L998-L1010) |
| 说到一半改主意（「算了改成悬疑」，在 world） | 整句被当世界观；`actions.plan` 只在句中出现「题材/类型」时识别改题材 | ⚠️ | [wizard_actions.py L260-L263](backend/app/wizard_actions.py#L260-L263) |
| 先给我写一段看看（在 branch / genre） | `clarify`：重复问方向/题材 | ❌ | [wizard_chat.py L299-L323](backend/app/wizard_chat.py#L299-L323)（只认「写+小说/故事/书」） |
| 先给我写一段看看（在 chapters） | 被当「1 段」，结果碰巧一致 | ⚠️ | [wizard_chat.py L641-L647](backend/app/wizard_chat.py#L641-L647) |
| 按默认来 / 别问了 | 后续步骤全部按默认补齐，直达确认页 | ✅ | [assistant_wizard.py L916-L935](backend/app/routers/assistant_wizard.py#L916-L935) |

## 7. 由本图得出的改造边界

1. **已补「定位类的行情依据」**：题材/主角两步能引用独立行情卡（有来源、日期、置信度、时间窗），
   解决「现在什么题材好卖」「什么主角讨喜」只有模型泛答的问题；
   静态 `GENRE_HINTS` 保留为兜底，行情卡过期或题材不匹配时自动退回。
   实现：`backend/app/wizard_market.py` + `backend/data/market_cards/*.json`。
2. **明确不改的已知缺口（记录在案，后续单独处理）**：
   - 故事自由文本步没有「指令/问题」闸门，`帮我起个名字`、`这段好不好看`、`太水了` 会被当答案；
   - `我只有 30 万字预算` 这类「当前步无解、但其他步有答案」的句子在 clarify 路径不预填；
   - `算了改成悬疑` 不带「题材/类型」二字时不会触发改题材；
   - `先给我写一段看看` 没有「想直接开工」的意图识别；
   - 元问题（版权/抄袭/一致性）在 confirm 步落到兜底话术。
