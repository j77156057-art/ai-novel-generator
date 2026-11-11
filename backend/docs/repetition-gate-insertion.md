# 复读闸门插入点说明（方案稿，本轮不接线）

> 状态：**方案已定稿，仍未接线**——未改 `engine.py` / `provider.py`，未写库，未重启，未跑生成。
> 判据层已就绪：`backend/app/generation/repetition.py` + `tests/test_repetition_gate.py`（30 例全绿）。
> D1–D7 **全部定稿**（D3 = 新增 `generations.quality_json` 列，见 §5.1）。接线要等 `engine.py` 交还。

---

## 1. 现状：正文有四条写入路径，`_commit_segment` 只覆盖其中两条

| # | 路径 | 入口 | 实际写正文的位置 | 经 `_commit_segment` |
|---|---|---|---|---|
| P1 | 续写（非流式） | `generate_next_segment` [engine.py#L1535](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1535) | [`_commit_segment` L1628](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1628) → [L473](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L473) | 是 |
| P2 | 续写（SSE，前端实际走这条） | `generate_next_segment_stream` [L1721](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1721) | [`_commit_segment` L1817](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1817) | 是 |
| P3 | 重写某一段 | `regenerate_segment_stream` [L1976](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1976) | 旧路径 `target.text = text` [L2225](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L2225)（分支 [L2223](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L2223)）；快照路径 `finalize_snapshot` [L2242](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L2242)（SQL 级 UPDATE） | **否** |
| P4 | 回滚 | `rollback`（`app/agents/dialog/snapshot.py`） | 把 `before_text` 写回 | **否** |
| P5 | 助手端点 | `POST /api/assistant/invoke-generation` | 复用 P1 / P3 | 随 P1 / P3 |

**这决定了闸门不是「往 `_commit_segment` 里塞一行」就完事**：只改 `_commit_segment`
会漏掉重写路径（也就是诊断里退化最集中的那条路径——11-11 六次重写都走 P3）。

诊断结论里 gen4 是 P1/P2 写进去的（`agent_run_id` 为空），所以**首期只覆盖 P1+P2 是
有效的**；但 P3 不覆盖，就等于「闸门只管第一次生成，用户点重写可以绕过」。

---

## 2. 复读检测的插入点（三选一，或组合）

### I1 — `_commit_segment` 入口（L473，函数最前面）

- 覆盖：P1 + P2。
- 能拿到：最终正文（已过 AI 腔重写、时代自检）；`db`、`project`、`summary`。
- 能做：拒绝（raise）/ 标记（改 `meta`）/ 落库后再处理。
- 不能做：拿不到「生成耗时、finish_reason、重试次数」这类生成期信息（函数签名里没有）。
- 代价：最小改动面（一个函数、两条路径同时生效，且天然覆盖未来新增的
  `generate_*` 路径）；但 `_commit_segment` 的 docstring 明确它只做「落库三件事」，
  加检测会把「判」和「写」耦在一个函数里。

### I2 — 两个 generate 函数的 commit 前一步（L1627 / L1816）

- 覆盖：P1 + P2，**两处重复调用**（本项目历史上最忌讳的“两条路径各写一份”）。
- 能拿到：同 I1，另外能拿到 `provider`、`rewrite_info`、`written`（段号）。
- 能做：三档处置都能做，且**自动重生成**只能在这里做（`_commit_segment` 内部
  重入生成会打乱 `_next_segment_no`）。
- 代价：两处代码；将来新增生成路径容易漏。

### I3 — `regenerate_segment_stream` 落库分支前（L2214 之后、L2223 分支之前）

- 覆盖：P3（含局部补丁路径——补丁文本同样走这里）。
- 能拿到：`before_text`、`instruction`、`snapshot_id`。
- 能做：拒绝写入时**只需 `fail_snapshot(reason=...)`**，正文一行不动——比 P1/P2 的
  拒绝语义更干净（有现成的快照兜底）。
- 代价：第三处调用；快照行要能表达「被闸门拦下」这个新原因。

### I4 — `_commit_segment` 内部、`record_segment_check` 旁边（L508）

- 覆盖：P1 + P2，**只能在正文已经 `db.commit()` 之后**执行。
- 能做：**只能「落库后标记」**，做不了拒绝，也做不了自动重试（已经写出去了）。
- 代价：与 A 档处置互斥；好处是零风险（检测抛异常也不影响已落库正文）。

> **检测本身的成本**：单段 2273 字 ≈ **1.2 ms**，6800 字 ≈ 4.8 ms（纯 CPU、不发请求）。
> 与一次生成（数十秒）相比可忽略，因此“要不要为了省 CPU 而不检测”不是问题。

> **按已定决策的实际组合**：P1/P2 用 **I2**（必须在调用点决定“跳过 KG”，
> `_commit_segment` 内部拿不到这个决定），P3 用 **I3**（标记要写到 `target.quality_json`，
> 只有调用点拿得到 `target`）。I1 是“落库后只标记”的老方案，与“跳过 KG”冲突；
> I4 同理（正文与 KG 都已经写完）——本轮都不采用。

---

## 3. 已定处置：标记 + 跳过 KG 抽取（不拒绝落库）

**原则**：正文一律落库（拒绝会让用户丢掉刚生成的内容），污染从**下游**切断——
退化段不进 KG / facts，标记随接口响应返回，前端提示「疑似复读，建议重生成」。

### 3.1 标记的数据形状（落点：`generations.quality_json`，见 D3 / §5.1）

`quality_json` 是一个 TEXT 列，值是下面这段 JSON（退化段示例）：

```json
{
  "repetition": {
    "verdict": "degenerate",
    "triggers": ["top_sentence_repeat=18>=10 且 repeated_sentence_coverage=0.525>=0.2", "dup20=0.566>=0.3"],
    "reason_text": "全段 124 句只有 58 个不同句子（唯一率 0.468）；…；后 1338 字…为模板循环",
    "top_sentence_repeat": 18,
    "repeated_sentence_coverage": 0.525,
    "dup20": 0.566
  },
  "truncated": true,
  "checked_at": "2026-11-12T…Z"
}
```

只存「判定 + 命中的线 + 一句话原因 + 3 个关键数」，不存全量指标（列随行读取，越短越好）。

与早先「`summary` 兄弟键」方案的关键差别：**`verdict == "ok"` 与 `"skipped_short"` 也要写**
（值更短，如 `{"repetition":{"verdict":"ok"},"truncated":false,"checked_at":…}`）。
原因见 §3.4：列的 `NULL` 专门表示「未检测」，若 ok 也写 `NULL`，前端就只能把
「检测过且正常」误显示成「没测过」。三态必须可分。

### 3.2 逐路径插入位置与语义

| 路径 | 检测时机（commit 前） | 标记写到哪 | 跳过什么 | 失败语义 | 快照 | 摘要树 | 前端 |
|---|---|---|---|---|---|---|---|
| P1 同步续写 | era 自检之后、[`_commit_segment` L1628](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1628) 之前（≈L1627） | `_commit_segment(..., quality=…)` 建行时写 `quality_json`（同一次 INSERT） | [L1642](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1642) `_merge_segment_into_graph` 前按同一份报告跳过 | 检测异常一律吞掉 + warning，正文照常落库 | 无（此路径不建快照） | **照写** | 响应体 `repetition` + `warnings` |
| P2 流式续写 | 同 P1，[L1817](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1817) 之前（≈L1816） | 同上（同一次 INSERT） | [L1833](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L1833) 同 P1 | 同上 | 无 | 照写 | `done` 事件带 `repetition` / `warnings` |
| P3 重写 | `new_summary` 构造之后（L2210-2214）、落库分支 [L2223](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L2223) 之前 | 旧路径：`target.text = text`（[L2225](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/generation/engine.py#L2225)）之后紧接 `target.quality_json = …`，同一次 UPDATE / commit；快照路径：`finalize_snapshot(..., quality=…)` 写进同一条 SQL UPDATE（[snapshot.py L697-709](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/agents/dialog/snapshot.py#L697-L709)），与 applied 同一事务 | **无需跳过**：该路径本就不调 `_merge_segment_into_graph` | 同上 | 正文照写、`finalize_snapshot` 照常；**不** `fail_snapshot`（拒绝语义已排除） | 照写 | 响应体同上 |
| P4 回滚 | 不检测（只恢复既有文本） | 不写 | 不跳过 | **只 `logger.warning`**，不改行为、不让回滚失败 | 回滚自身事务不变 | 不变 | 不变 |

逐条说明：

- **标记与正文同事务落**：报告在 `db.commit()` 之前算好（P1/P2 在调用点，P3 在落库分支
  之前），值随正文一起落——旧路径挂在 `target` 上，快照路径作为 `finalize_snapshot` 的入参
  进同一条 SQL UPDATE。**不存在“先写正文、再补标记”的第二次写**，也就不会出现
  「正文已更新、标记丢了」的半态。
- **P1/P2 对 `_commit_segment` 的唯一签名改动**：新增可选参数（如 `quality: dict | None = None`），
  写进新列。默认 `None` 时既有字段输出与现在**逐字节相同**（`text` / `summary` / `chapter_range`
  不改，指纹 `md5(chapter_range + "\0" + text)` 不变）。
- **跳过 KG 必须放在调用点，不能改 `_merge_segment_into_graph` 内部**：该函数还有别的调用方
  （`app/kg/build.py`），改内部语义会波及它们。
- **P2 不能撤回已推送的 chunk**：用户看到整段之后才收到 `warnings`。这也正是“标记”而不是
  “拒绝”的技术原因之一。
- **P3 无需跳过 KG**：重写路径本来就**不合并 KG**（只有 P1/L1642 与 P2/L1833 调
  `_merge_segment_into_graph`），这里不存在“跳过”这个动作——别读成「重写被拦住了什么」。
  它暴露的是另一件事：**重写无法修复已被污染的 facts**（p5 的循环对白仍在 `facts` 里），
  要修只能走重建，是独立任务。
- **P3 的快照目前只有 succeeded / failed 两态**：标记不落快照表——写在 `target.quality_json`
  就能审计。**建议不加第三态**，避免迁移。
- **P4 只记日志**：回滚恢复的是本来就存在的文本，不是新产出；也不应因为“旧文本退化”让回滚失败。
  日志字段固定为 `project_id / segment_no / verdict / top_sentence_repeat / dup20`，便于事后 grep。

### 3.3 对既有数据与后续生成的影响

- **一次加列迁移**：`generations.quality_json`（TEXT，可空，**不回填**）。迁移只动 `novel_dev.db`，
  `generated.db` 一行不动，5 行指纹不变。详见 §5.1。
- **对后续生成的连锁**：退化段被跳过 KG → 后续段的 reminders（角色约束 / 未填坑）里**不含**
  该段抽出的事实；在重生成该段之前，这段“对后续不可见”。这是有意为之（宁可少约束，也不让
  循环对白变成设定），但前端提示里要说清楚，否则作者会觉得“系统忘了刚写的内容”。
- **对已有脏数据不追溯**：闸门只作用于新产出。p5 已污染的 facts / kg_mentions **不会**因此减少
  （守卫值 34 保持不变是预期结果，不是漏检）。

### 3.4 历史段回读：`NULL` 显示「未检测」，不是「正常」

- 迁移后现有 5 段（gen1/2/3/4/5）的 `quality_json` 全是 `NULL`。API 序列化为 `"quality": null`。
- 前端映射（**必须**照这个来，`null` 绝不能画成「正常」）：

| `quality` | 展示 | 含义 |
|---|---|---|
| `null` | **未检测** | 这段写作时还没有闸门；不代表内容正常 |
| `{"repetition":{"verdict":"ok"},…}` | 正常 | 检测过，未命中退化线 |
| `{"repetition":{"verdict":"degenerate"},…}` | 疑似复读（附 `reason_text`） | 命中退化线 |
| `{"repetition":{"verdict":"skipped_short"},…}` | 段落过短，未判定 | <200 字，判据不适用 |

- **不要**在读接口里现算再展示：读时检测会让同一段的历史结论随代码版本漂移，与「写入时判定、
  写入时跳过 KG」的口径不一致；判据也不该由前端或读路径承担。
- 若产品要求「历史段也能看到结论」，那是一次**独立的离线补测任务**（对 5 行跑一次
  `analyze_repetition` 写回），不在本次迁移里，且结果要标注「补测」与写入时判定区分。**本轮不做。**
- 因此 gen4 在迁移后仍显示「未检测」是**正确**的（当时确实没检测），不是漏标；要让它显示退化，
  得跑上面的补测任务。

---

## 4. 截断：`ends_complete` 是唯一跨路径信号（已定）

**三条代码事实**（已核对）：

1. 只有非流式 `chat()` 记录 `finish_reason`（[_note_finish_reason L326](file:///c:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/backend/app/llm/provider.py#L326)，调用点 L284）；
2. **`stream_chat()` 完全没有 `finish_reason` 解析**——而前端实际走 SSE（P2）；
3. `provider.last_finish_reason` 会被随后的 `provider.summarize()` 覆盖（`summarize` → `_complete`
   → 同一条 `chat` 链路），在 `_commit_segment` 里读它**读不到**。

因此统一用 `RepetitionMetrics.ends_complete`：

- `truncated = not metrics.ends_complete`，**独立于 verdict**：短文本（<200 字）不判退化，
  但半句结尾照样打 `truncated`——截断与长度无关；
- **只标记、不拒绝、不重试**（D5）；它只是代理信号，模型“半句自停”也会命中，
  而诊断里无法把两者区分开（缺 `finish_reason` 日志）；
- 落地形态：`quality_json` 里的 `truncated` 字段（与 `repetition` 同层）+ `warnings` 里一条
  「结尾停在半句，可能是截断」；对快照 / 摘要树 / KG 均无影响。

**为什么不能只用 truncated**：`smoke_v226c` 8280 字、结尾完整、却整段循环（dup20=0.715）；
gen4 是两者同时命中。两个信号正交，不能互相替代。

---

## 5. 决策记录

| # | 决策 | 结论 | 依据 |
|---|---|---|---|
| D1 | 覆盖范围 | **P1 / P2 / P3 各自挂；P4 只记日志、不判定** | 只挂 `_commit_segment` 会漏掉重写路径，而 11-11 六次重写都走 P3；回滚恢复的是既有文本，不是新产出 |
| D2 | 处置 | **标记 + 跳过 KG，不拒绝落库** | 拒绝会让用户丢掉刚生成的内容；真正的污染源是 KG / facts（p5 现状） |
| D3 | 标记持久化落点 | **新增 `generations.quality_json` 列**（TEXT、可空、不参与指纹；`NULL` = 未检测） | `summary` 会被重写与回滚覆盖（`before_summary`/`after_summary` 就在做这件事），标记跟着摘要走会丢；独立表要 join、还要处理一段多条结论；现有迁移机制只支持 `ADD COLUMN`，加列是最小改动。详见 §5.1 |
| D4 | 退化段是否进 KG | **不进**（P1/P2 在调用点跳过；P3 无需跳过——它本就不合并 KG） | 同 D2 |
| D5 | 截断信号 | **`ends_complete` → 打 `truncated` 标记，不拒绝** | `stream_chat` 无 finish_reason、`summarize` 会覆盖，只有文本信号跨路径可用 |
| D6 | 阈值 | **沿用 `repetition.py` 默认值**（200 / 10 / 0.20 / 0.30） | 敏感性平台已验（9 组组合命中同一批 7 段） |
| D7 | HTTP 语义 | **200 + 响应体带 `repetition` 与 `warnings`** | 不把成功写入伪装成失败 |

### 5.1 D3 决策记录：标记载体 = `generations.quality_json`

**结论**：新增一列 `generations.quality_json`。不用 `summary` 兄弟键，也不用独立表。

为什么否掉另两个：

- **`summary` 兄弟键会丢**：`summary` 不是只写一次——P3 重写会把 `after_summary` / `before_summary`
  写回同一行，P4 回滚也恢复旧摘要。标记挂在摘要里，会被重写/回滚整块覆盖，而「这段被检测过、
  结果是退化」恰恰是最不该随摘要回滚丢掉的信息。
- **独立表收益不抵成本**：要 join 才能回读；还要处理「一段被重写多次、留多条结论」的取舍
  （哪条算数？要不要清理？）；而闸门只关心「这一行**当前**正文的结论」，一条就够。

列的规格：

| 项 | 取值 | 说明 |
|---|---|---|
| 列名 | `quality_json` | 带 `_json` 表明内容形态；与 `summary` 解耦 |
| 类型 | `TEXT` | 存 JSON 文本（SQLite 无 JSONB；也不拆多列，字段还会演进） |
| 可空性 | **允许 `NULL`** | **`NULL` = 未检测**，不是「正常」。迁移后 5 行、以及接线前写入的所有行都是 `NULL` |
| 默认值 | 无（`NULL`） | 迁移**不回填**：回填成 `{"verdict":"ok"}` 等于伪造「已检测」 |
| 参与指纹 | **否** | 现有守卫指纹是 `md5(chapter_range + "\0" + text)`（`.tmp_verify/post_guards.py`），加列前后 5 行 digest 必须逐字一致；本列也不进任何去重 / 幂等判据 |
| 索引 | 不建 | 只有「按 generation 主键取一行」的读法；没有「按 verdict 扫全表」的需求 |
| ORM | `Generation.quality_json`（`Text`，`nullable=True`） | DB 里永远是字符串；序列化层解析成 `quality: object \| null` 返回 |

写入语义：

- 每个**新产出**的段都写：`verdict ∈ {ok, degenerate, skipped_short}` 都落一行 JSON，
  附 `truncated` 与 `checked_at`（形状见 §3.1）。
- 只有「没经过闸门」才是 `NULL`：历史行、接线前写入的行、P4 回滚恢复的行（回滚不判定）。
- 于是三态可分，前端不会把「没测过」说成「正常」（映射见 §3.4）。

迁移脚本形态（沿用 `2026-11-15-project-active-novel-v1/apply_active_novel.py` 的做法）：

新目录 `backend/migrations/2026-11-13-generation-quality-v1/`（日期按落地日），脚本
`apply_quality_column.py`：

```
python apply_quality_column.py                # dry-run（默认，只读）
python apply_quality_column.py --apply        # 加列
python apply_quality_column.py --rollback     # 清值 + 删列
python apply_quality_column.py --db <副本> --backup-dir <目录>
```

| 环节 | 做法（照抄模板） |
|---|---|
| dry-run | 默认只读：打印 `generations` 的列清单、`has_column`、行数；**已存在列则拒跑**（`assert not has_column`） |
| 前置守卫 | 迁移前记录 5 行指纹 `md5(chapter_range + "\0" + text)`；迁移后重算必须**完全一致** |
| 备份 | `_backup()`：`PRAGMA wal_checkpoint(TRUNCATE)` + `con.backup()` → `backups/novel_dev-full-quality-col-<ts>.db` |
| 加列 | `ALTER TABLE "generations" ADD COLUMN "quality_json" TEXT`；**不做任何 UPDATE**（不回填） |
| 快照 | `snapshots/quality_col_<ts>.json`，含 before/after 的 `columns` / `generations_rows` / `rows_md5` / 5 行指纹 |
| 前后断言 | 列数 +1、`generations` 行数不变（5）、新列是最后一列、5 行指纹不变、`generated.db` 未被触碰 |
| `--rollback` | 先 `UPDATE "generations" SET "quality_json"=NULL` 再 `ALTER TABLE ... DROP COLUMN`（与模板同序：即使 DROP 不支持，值也已复位）；同样先备份 |
| 回滚代价 | 删列会丢掉接线后写入的**所有**标记；回滚前必须确认没有要保留的检测结果 |
| 环境检查 | `DROP COLUMN` 需 SQLite ≥ 3.35：脚本显式检查 `sqlite3.sqlite_version_info`，不满足直接报错并提示改用「重建表」方案 |

**范围**：只动 `novel_dev.db` 的 `generations` 表；`generated.db` 一行不动。
**模型/接口改动**：只在接线时做（`Generation.quality_json` + 序列化 `quality`），本轮不改。

**响应形状（D7 已定，按这个做）**：

```json
{
  "…": "原有字段不变",
  "repetition": {"verdict": "degenerate", "triggers": ["…"], "reason_text": "…"},
  "warnings": ["疑似复读，建议重生成", "结尾停在半句，可能是截断"]
}
```

`verdict == "ok"` 时 `repetition` 也返回（前端可忽略），`warnings` 为空数组——
**不要**在正常时省略这两个键，前端分支会因此变脆。

---

## 6. 已知漏报与误报边界

| 类型 | 形态 | 行为 | 原因 / 数据 |
|---|---|---|---|
| **误标** | 550 字纯对白，同一对白标签重复 25 次（`rep_cov` 22%） | 判 `degenerate`，跳过 KG | 覆盖率按全文算，短段里一个短标签就能占 20%+；同密度在 2000 字段只有 6%，不触发。测试 `test_dialogue_tag_repeat_flags_only_in_short_text` 钉住 |
| **误标** | 模型“半句自停” | 打 `truncated` | 无 `finish_reason` 无法区分自停与截断，所以 truncated 只提示、不阻断 |
| **放过** | 单句高频但覆盖率低（正常文本实测单句最高 **100 次**、`rep_cov` <20%） | 判 `ok` | AND 结构压误报的自觉代价：不能因为「满篇『他说。』」就判退化 |
| **放过** | 短段（<200 字）整段循环 | `skipped_short`，只可能打 `truncated` | 短文本 n-gram 宇宙太小；显式取舍，不是漏实现 |
| **放过** | 跨段互抄（第 2 段抄第 1 段） | 不命中 | 归 `formulaic.py` 的 M1，两套判据不重叠 |
| **放过** | 回滚恢复的旧退化文本 | 不判定、不标记 | P4 只记日志（D1） |
| **放过** | P3 局部补丁 `anchor_ambiguous` 失败 | 与复读无关 | 别把它统计进“退化率”，否则指标失真 |

**已知但本轮不修**：断句把 `”` 留在下一句（`”那个身影问道。`）是诊断口径的一部分；
换成更“正确”的断句会让 5 段固定值全部变化、与诊断表对不上。这条口径写死是有意的。

---

## 7. 接线时预计要加的测试（预告，不实现）

- P1 / P2 / P3 各一条：gen4 文本 → 正文落库、`generations.quality_json` 带 `degenerate`、
  响应含 `repetition` / `warnings`、KG 未新增（`kg_mentions` 不变）。
- 正常文本（gen1）→ 正文与 `summary` 与现在**逐字节一致**（5 行指纹不变）；
  `quality_json` 为 `ok`（**不是** `NULL`），KG 照常合并。
- 同事务：模拟 commit 失败 → 正文与 `quality_json` 一起不落，不出现「正文更新了、标记没写」。
- P3（旧路径 `snapshot_id is None`）：退化重写 → `target.text` 与 `target.quality_json` 同步更新、
  快照 `succeeded`（**不是** `failed`）、`before_summary` / `after_summary` 行为不变。
- P3（快照路径）：退化重写 → `finalize_snapshot` 的同一条 UPDATE 同时改正文与 `quality_json`；
  快照 `applied`，`before_summary` / `after_summary` 行为不变。
- P4：回滚退化文本 → 只多一条 warning 日志；正文、`summary`、`quality_json` 都不变。
- 截断：半句文本 → `truncated=true`、`ends_complete=false`，但 `verdict` 仍可为 `ok`。
- 短文本：<200 字 → `verdict=skipped_short`，`quality_json` **仍写**（与 `NULL` 区分）。
- 历史段：迁移后 5 行 `quality_json IS NULL` → API 返回 `quality: null` → 前端映射「未检测」。
- 迁移脚本：dry-run 不写库；`--apply` 后列数 +1、`generations` 行数不变、5 行指纹不变；
  `--rollback` 后列消失、5 行指纹仍不变。
- 流式：`done` 事件同时含 `repetition` 与 `warnings`，且与 `error` 事件互斥。
