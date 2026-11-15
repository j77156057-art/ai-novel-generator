# 首字等待能不能压：可执行判断

> 2026-11-15 · 只出方案与代价，**不改生产、不 commit、不动配置**。
>
> 输入：MiniMax 官方文档 + 本地 9 次参数探针 + 第 32–34 章 `quality_json` + 第 23–32 章耗时报告。
> 目标：回答「关掉/限制/降档思考能不能压首字等待」，以及「压了值不值」。

---

## 0. 结论先行

| 问题 | 结论 | 依据 |
|---|---|---|
| 能不能**关掉**思考 | **M3.1-Flash 不行**。`thinking.type=disabled` 与 `reasoning_effort=none` 都返回 400 | 官方文档 + 本地实测错误码 `2013` |
| 能不能**限流/限制思考量** | 没有 token 上限类参数；只有 `reasoning_effort` 五档 | 官方文档只列了五档，没有 `budget_tokens` |
| 能不能**降档** | **能**。`reasoning_effort = low / medium / high / xhigh / max`，默认 `max` | 官方文档 + 探针 200 |
| 生产现在用的是哪档 | **默认 max**。仓库里没有任何生产代码把 `reasoning_effort` 传给 MiniMax | `provider.py` 只给老模型注入 `thinking=disabled` |
| 有没有优先通道 | **没有**。`service_tier=priority` 不支持 M3.1-Flash，只接受 `standard` | 官方文档 |
| 降档值不值得 | **现在不能判**。没有任何一章 low 档的正文样本；先做 10 章 A/B | 见第 3 节 |
| 补完轮要不要现在改下限 | **不要**，等第 35–38 章 | 第 33 章在提示下限 3304 的情况下仍只写 2710 |

一句话：**「关掉思考」这条路是死的；「从 max 降到 low」这条路是通的，但生产没接线，而且降了值不值，必须用现有闸门指标实测，不能拍脑袋。**

---

## 1. 先钉住生产实际在跑什么（否则实验全废）

这一条必须在动手前解决，因为仓库里有三套口径：

1. 代码默认：`config.py` 的 `minimax_model = "MiniMax-M3.1-Flash-Preview"`。
2. 仓库 `.env`：`LLM_PROVIDER=zhipu`、`MINIMAX_MODEL=MiniMax-M3`。
3. 你与既有报告的口径：`MiniMax-M3.1-Flash`。

`.env` 不一定是运行进程的配置：环境变量的优先级高于 `.env`，而 2026-11-07 已经有同类事故——全局环境变量把 `MINIMAX_BASE_URL` 指到 opencode.ai，请求打到了错误端点（见 `experiments/2026-11-06-kg-audit/minimax-vol1/reports/sample_report.md`）。

**第 0 步（1 次短请求，5 分钟内）**：在实验脚本里打印并断言：

- `type(provider).__name__`
- `provider.model`、`provider.base_url`
- 响应体 `body["model"]` 的回显
- 是否出现 `reasoning_content`

回显模型必须是目标模型，否则中止，不跑实验。**不要凭 `.env` 或 shell 环境推断。**

---

## 2. 可用旋钮：查到的与查不到的

### 2.1 MiniMax 官方文档（OpenAI 兼容接口）

| 参数 | 支持情况 | 说明 |
|---|---|---|
| `reasoning_effort` | 仅 `MiniMax-M3.1-Flash` | 取值 `low` / `medium` / `high` / `xhigh` / `max`，默认 `max`；传 `none` 返回 400 |
| `thinking.type=disabled` | **M3.1-Flash 不支持** | 返回 400：`requires adaptive thinking; thinking.type="disabled" (including reasoning.effort=none) is not allowed (2013)` |
| `thinking.type=disabled` | M3 支持 | M3 可关闭思考；M2.x 不能关 |
| `reasoning_split` | M3.1-Flash 不允许为 false | 不是速度旋钮；设 false 会 400 |
| `service_tier` | 只接受 `standard` | `priority` 仅 M3 支持；M3.1-Flash 传 priority 返回 400 |
| `temperature` / `top_p` | M3.1-Flash 不接受调参 | 固定 `temperature=1`、`top_p=0.95`——这也意味着同一提示每次输出都会有波动，实验必须多点取中位数 |
| `max_tokens` / `max_completion_tokens` | 支持 | 是输出预算，**不是思考上限**；调小只会更容易截断，压不了思考时长 |
| `stream_options.include_usage` | 支持 | 仅用于测量：流式最后一条事件带 `usage` 与 `reasoning_tokens`；当前生产没开，所以流式 usage 一直是空 |

**查不到的**：官方没有给出各档 `reasoning_effort` 对**创作类长文**质量的影响数据，也没有公开的「low 档首字等待能省多少」基准。只能实测，不能引用。

### 2.2 本地探针（9 次短请求，2026-11-15）

模型输出只有「收到」两个字，属于**参数接受度**探针，不代表章节效果：

| 模型 id | 控制组 | `reasoning_effort=low` | `thinking=disabled` |
|---|---|---|---|
| `MiniMax-M3.1-Flash` | 200；reasoning 0 字 | 200；reasoning 0 字 | **400（2013）** |
| `MiniMax-M3.1-Flash-Preview`（回显为 M3.1-Flash） | 200；reasoning 91 字 / 20 tokens | 200；reasoning 45 字 / 10 tokens | **400（2013）** |
| `MiniMax-M3` | 200；reasoning 17 tokens | 200；reasoning 14 tokens | 200；reasoning 0 tokens，正文 2 字 |

两点必须说清：

1. **Preview 与正式版是同一个 id**，服务端回显都是 `MiniMax-M3.1-Flash`；关闭思考都是 400。
2. Preview 这一次 low 把 reasoning 从 91 字降到 45 字，只能证明**参数生效**，不能证明章节级能省一半——提示太短，且同一模型控制组本身就有波动（正式版这次 0 字、Preview 这次 91 字）。**不要拿这组数字当效果量。**

### 2.3 生产代码现状：旋钮存在，但没接线

`MiniMaxChatProvider.__init__` 只做了一件事：老模型（M3/M2.x）注入 `thinking={"type":"disabled"}`；M3.1-Flash 走 `extra = {}`。全仓库检索 `reasoning_effort`，只有 `OpenCodeGoChatProvider` 的注释提到它，**没有任何生产代码把它下发给 MiniMax**。

所以：

- 当前 MiniMax 线实际跑的是**默认 `max`**。
- 既有报告里写的「reasoning low」与代码不一致；那条 `low` 是 opencode go / deepseek 线的参数，不是 MiniMax 的。
- 实验可以在**不改生产代码**的前提下做：构造 provider 后设置 `provider.extra_body["reasoning_effort"] = "low"`。上线才需要加配置项。

---

## 3. 小样本实验：max vs low，N=5，10 章，1.5–2 小时，不占生产库

### 3.1 设计

| 项 | 取值 | 理由 |
|---|---|---|
| 对照臂 A | `reasoning_effort=max`（不传，即生产现状） | 复现当前基线 |
| 实验臂 B | `reasoning_effort=low` | 官方允许的最低档；若运行模型是 M3，则 B 改为 `thinking=disabled` |
| 每臂样本 | **N=5** | 同一章同参数已出现 144.4s vs 315.6s（2.2 倍）波动；3 个点的中位数不稳，5 个点是「能看方向」的最低成本 |
| 总样本 | 10 章 + 1 次预热（丢弃） | 预热避免提示缓存/排队冷启动把第一跑算进某一臂 |
| 顺序 | 交错 ABBA/随机，不并行 | 并行会互相抢账号并发，污染首字等待；交错把供应商排队波动摊到两臂 |
| 提示 | 冻结第 35 章的 system + user（含 brief 素材）为 `prompt.json` | 10 次请求字节完全一致，排除提示漂移 |
| 素材/数据库 | 复制一份 `novel_dev.db` 到实验目录，只读打开 | 不占生产库、不写 Generation、不触发 KG/分支/滚上；跑坏可重来 |
| 正文处理 | **只测初稿**，不跑补完轮、不跑重写轮 | 补完/重写会掩盖思考档位的影响，并额外引入 90s 级变量 |
| 质量评估 | 现有 `generation_gate` + `_sentence_shape_check` + `_short_sentence_check` + `_dialogue_ratio_check`，同一份 `gate_limits_meta` | 不另造评分 |
| 结果落盘 | `experiments/2026-11-15-thinking-effort/runs.jsonl` + 汇总 md | 可复查每一跑 |

### 3.2 每跑记录什么

延迟与思考：

- `request_ms`：请求发出 → 响应头（连接 + 供应商排队）
- `first_frame_ms`：请求发出 → 第一个非空增量
- `first_content_ms`：请求发出 → 第一个可见正文汉字（**主指标**，对应你说的首字等待）
- `reasoning_chars_before_content`：首字到达前累计的思考字符数（**归因主指标**）
- `reasoning_chars` / `content_chars` / 总流时长 / `finish_reason`
- `usage`（实验里加 `stream_options.include_usage`，拿 `reasoning_tokens`；生产当前没开）

正文：

- 汉字数、`generation_gate` 的 `passed` / `reasons` / `metrics`
- `sentence_shape_check`、`short_sentence_check`、`dialogue_ratio_check`
- 是否低于 3304（= 会不会触发补完轮）

### 3.3 时间与成本

- 单章按第 23–32 章均值 513s 估，10 跑 ≈ 85 分钟；加预热与提示快照 ≈ **1.5–2 小时**。
- 若 low 真能砍掉一半首字等待，实际约 **60–75 分钟**。
- 早停：先跑 3 跑/臂（6 跑，约 50 分钟）。如果 low 的 `first_content_ms` 中位数**没有比 max 低 10%**，直接停，结论写「无信号」，不烧剩下的 4 跑。
- token 成本：仓库里没有 M3.1-Flash 单价，无法先算钱；实验开 `include_usage` 后按实际 `reasoning_tokens` 结算。按既有注释的 4.3–7.3 万 reasoning 字符/章估算，10 章与第 23–32 章那 10 章同一量级，不是额外一个数量级。

### 3.4 预注册判据（先写死，避免看完数字再找理由）

1. **主判据**：low 的 `first_content_ms` 中位数比 max 低 **≥20%** 才继续；低于 20% 直接判「不划算」。
   - 依据：首字等待占单章 59%–82%，砍 20% 只换来总时长 12%–16%，与已经拿到的重写优化（13%）同量级，不值得再承担质量风险。
2. **硬闸门**：low 的 `generation_gate.passed` 比例不得低于 max，且不得新增违规项。
3. **质量否决**：以下四项里，只要有 **2 项以上**在 low 臂出现「≥4/5 跑都比 max 臂中位数更差」，判「不划算」：
   - `sentence_shape_check` 的中段拥挤度超上限
   - `sentence_shape_check` 的长句占比低于下限
   - `short_sentence_check` 的短句占比低于 p10
   - `dialogue_ratio_check` 的对白配比落在 p25–p75 之外
4. **补完税**：如果 low 的一稿汉字中位数 <3304，按第 33 章实测的 91.8s 给 low 加一笔「补完轮成本」；若加上后有效总时长仍不优于 max，判「不划算」。
5. **不放行**：即使 1–4 全过，也只算「进入确认轮」，再加 5 跑/臂（N=10）复核；**不在本轮直接改生产**。
6. **不打分**：不造综合分、不用主观「读起来怎么样」，只报上述已有指标的逐跑值与中位数。

---

## 4. 代价判断：为什么基线已经没有多少余量

第 34 章（v19 新尺的第一章，可与后续直接比较）的实测：

| 指标 | 第 34 章 | 参考线 | 状态 |
|---|---|---|---|
| 中段拥挤度 | 0.6068 | 上限 0.5741 | **超限** |
| 短句占比 | 0.015 | p10 0.0199 | **低于 p10** |
| 长句占比 | 0.078 | 下限 | 低于下限（软偏差） |
| 对白配比 | 0.406 | p25–p75 0.175–0.345 | **高于 p75** |

第 33 章的句长指标用的是第 34 章之前的老尺（`SENTENCE_SCOPE_FROM_CHAPTER=34`），**不能拿来和第 34 章比**。

结论：

- 硬闸门只判「段数 / 极短纯对白段占比 / 字数上限」，不判这些句长与对白配比；所以 low 档即使把句子形态弄差，也可能照样 `passed`。
- 但第 34 章已经在 3 项软指标上贴边/越线，**没有余量再让 low 档继续恶化**。
- 因此质量否决权必须给这四项软指标，而不是只看 `passed`。如果 low 省了首字等待却把中段拥挤度、短句占比、对白配比中的两项以上推得更差，这笔账**不值**——省下的是秒数，赔的是已经在修的文风。

反过来，如果 low 让这四项保持在同一水平（逐跑中位数差异在 ±10% 内），首字等待又真降 20% 以上，那就值得进入 N=10 确认轮，再谈加配置项和回退开关。

---

## 5. 补完轮：等第 35–38 章，现在不改下限

| 章 | prompt | 一稿汉字 | 是否触发补完 | 补后汉字 |
|---|---|---|---|---|
| 32 | v16 | 3020 | 是 | 3961 |
| 33 | v18 | 2710 | 是（91.8s） | 3687 |
| 34 | v18 | 3549 | 否 | — |

- 触发线 = `4000 × 0.826 = 3304`。第 33 章一稿 2710（目标的 67.8%），**在提示已经要求「不少于 3304 字」的情况下仍然少写 594 字**。
- 这说明「再降下限」不是有效杠杆：模型不是被下限卡住，而是没有按提示写满。把 3304 降到 3000，只会让 3000–3304 的稿子免于补完，**不会让模型多写**。
- 第 32 章也触发（3020→3961），但它是 `prompt-v16`，与 v18 不同源，不并入同一口径；它只说明「补完触发不是第 33 章独有」。
- 补完耗时目前只存在日志里（第 33 章 91.8s），`quality_json.draft_completion` 没记 `seconds`——这是下一个留痕缺口，但**本轮不改**。

**判断**：样本只有 2 章 v18（1 触发、1 不触发），不足以下调或上调下限。等第 35–38 章，收齐四个数再决定：

1. 触发率（≥2/4 才值得讨论）；
2. 每次补完的实际秒数（是否稳定在 90s 量级）；
3. 补完是否真的改善四项软指标，还是只加长度；
4. 触发章的 `draft_stream.reasoning_chars_before_content` 是否异常低（用来区分「模型自停」与「预算不足」）。

如果补完只是加长度、四项软指标一点没改善，才有理由讨论「接受更短的章」；如果它同时修了句长/对白形态，那 90s 是必要成本，不该砍。

---

## 6. 本轮明确不做

- 不改生产配置，不加 `reasoning_effort` 配置项，不 commit。
- 不对 M3.1-Flash 做「关闭思考」实验——接口层面就是 400，没有实验空间。
- 不把模型换成 M3 来关思考：M3 可关（本地实测 `reasoning_tokens=0`），但那是**换模型**，需要单独的质量 A/B，不能混进这次降档实验。
- 不调补完下限、不调闸门、不改判罚。

---

## 7. 还缺的两块数据

1. **运行进程的真实 provider/model**：`.env` 与代码默认不一致，必须先钉住（第 0 步）。
2. **M3.1-Flash 的单价**：仓库没有；实验开 `stream_options.include_usage` 后，用实际 `reasoning_tokens` 才能把「省时间」换算成「省钱」。
