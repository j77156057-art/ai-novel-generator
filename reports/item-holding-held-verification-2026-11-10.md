# 物品持有注入：真实库 held 行验证（2026-11-10）

> 范围：只做前半（真实库新建项目 → 两段生成 → 取证）。**未改任何守卫、注入代码或产品逻辑**，未手工写 `kg_mentions`。
> 结论先行：**held 行没有出现，块是 `no_record`；但卡点不是 mentions，而是物品分组按 `project_id` 隔离。**

## 0. 结论摘要

| 问题 | 结论 |
|---|---|
| seg1 是否把物品名带进 mentions | **是**。p7 抽到 5 条 mention，其中 `kind='item'` 的是「镇国剑」（entity 68，mention_kind=llm） |
| seg2 prompt 是否带块 | **是**。主调用与重写调用都带，块原文见 §4 |
| 块里是否有真实 held 行 | **没有**。`镇国剑：无持有记录（no_record…）` |
| 卡在哪 | `kg_item_group` 53 行**全部属于 project_id=5**；新项目 7 的注册表为空。`kg_item_holding` 的名字兜底只对 `group_id IS NULL` 的行生效，而《大奉》的行都挂了 group → 查不到 |
| 放开项目隔离就够吗 | **不够**。`project_id=None` 时「镇国剑」命中两个同名实体，直接变成 ambiguous；地书才能稳定读到「天地会」 |
| 正文是否照着写 | 块是 no_record，没有约束可依；正文引入「自称开国皇帝的男子、剑认主认出了我」，与库内真实持有（99–106 镇北王、106– 元景帝）**都不符**。若按真值注入，这段属于写反 |
| 重写路径是否再丢块 | **没有**。seg2 主调用与重写调用的块逐字一致；落库正文 = 重写输出剥 marker |

## 1. 验证对象与环境

- 项目：`id=7`，标题 `【验证·勿删】物品持有-held-2-20261110`；`reference_novel_ids=[5,6]`，`narrative_tier='fast'`。
- 项目 `id=6`（`…-held-20261110`）是第一次尝试，因 harness 缺陷无效，保留作证据（见 §2）。
- 两次都真实调用 LLM（Zhipu `glm-4-flash-250414` 链），真实落库。
- 一个已记录的环境现象：项目要求 `tier=fast`，但生成路径 6 次打印「期望 tier=fast，蒸馏库命中 tier=balanced」。`_require_distill_ready` 通过，生成照常；与本次注入验证无关，但值得单独查。

**harness 说明（只影响验证脚本，不改产品代码）**：

1. 第 2 次生成传了 `novel_ids=[5]`（项目仍引用 `[5,6]`）。原因：默认 `[5,6]` 会把两本语料混检，第 1 次的 seg2 直接写成《豪门宠婚》剧情（`南宫爵/安小落` 在 user prompt 里出现 19/29 次）。只挂《大奉》后才能判断物品句。
2. 第 2 次把 `batch_worker.ensure_worker` 临时置为 no-op，让 `flush_project` 同步跑完批抽。第 1 次没这么做，`_enqueue_v3_extraction` 自己起了后台线程，详见 §2。

## 2. 第一次尝试为什么无效

| 现象 | 原因 |
|---|---|
| `flush_project` 耗时 0.0s，mentions=0 | `_enqueue_v3_extraction` 内部调 `ensure_worker()`，后台 daemon 线程先把队列取走了 |
| 进程退出后 `agent_runs` 77/78 仍是 `running` | 批抽线程被进程退出杀掉，任务没有收尾 |
| seg2 没有块（`found=False`） | 上一条的直接后果：seg1 没有 mention，`_active_item_entries` 为空 |
| seg2 写成《豪门宠婚》 | 默认 RAG 同时检索 [5,6]，`user` 里豪门人物出现 19–29 次，模型顺着它续写 |

另外确认：**premise 不进生成提示**。第 1 次的时间线规划 prompt 里是「【故事梗概】（无）」，即便 premise 写了镇国剑/地书。真正能引导物品名的是 `outline`。

## 3. 第二次流程与耗时

`outline` 改成直接点名物品的三句，规划输出随即包含「镇国剑/地书」，主生成 prompt 里 `镇国剑` 出现 39 次。

| 阶段 | LLM 调用 | 明细（耗时 / 输出） | 墙钟 |
|---|---|---|---|
| seg1 | 6 | 规划 8.77s/523 · 主生成 37.71s/900 · 摘要 1.34s/24 · **AI 味重写 21.11s/694** · 摘要 2.25s/38 · 节点判定 5.80s/223 | **115.31s** |
| seg1 批抽 | 2 | 连通性探针 4.49s/80 · 实体抽取 40.09s/2772 | **44.66s** |
| seg2 | 4 | 主生成 30.60s/771 · 摘要 2.35s/32 · **AI 味重写 26.65s/779** · 摘要 1.35s/31 | **69.53s** |
| 合计 | **12** | LLM 净耗时约 182.5s | **229.5s** |

落库：seg1 687 字（id=8），seg2 772 字（id=9）；两段都触发了 AI 味重写，落库均为重写后的正文。

## 4. seg2 prompt 里的块原文（一字不改）

主调用与重写调用的块都在 `user` 里：主调用偏移 **6699**，重写调用偏移 **50**（在「请改写」之前）；长度均为 **163** 字符，`rstrip` 后逐字相同：

```text
【物品持有事实（已写内容最新一段；续写须与此一致）】
- 镇国剑：无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）
（本块只补「物品在谁手上」，不改动也不覆盖【剧情圣经】与【世界观事实】的既有约束。三者冲突时按「作者设定 > 世界观事实 > 本块」取信，行文中回避正面矛盾，不要静默改写物品归属。）
```

说明：

- 全 `no_record` 压缩生效——只有一条也压成单行，没有展开成「标题 + 明细 + 优先级」三段之外的冗余。
- 块里**没有 held/sealed/unowned/unknown 任何一条**，这正是本次要解释的卡点。

## 5. 卡点论证：不是 mentions，是项目级分组隔离

只读调用 `current_item_holdings(["镇国剑","地书"], project_id=...)`：

```text
-- project_id=7（本次验证项目） --
- 镇国剑、地书：均无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）

-- project_id=5（《大奉》入库所在项目） --
- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）
- 地书：权属（own）归天地会

-- project_id=None（全局） --
- 镇国剑：名字有歧义，可能在指：镇国剑 / 镇国剑；不猜，需指定具体是哪一件
- 地书：权属（own）归天地会
```

对应的代码事实：

1. `_item_holding_text` 传的是 `project_id=project.id`；
2. `_load_registry(path, project_id)` 只加载该项目的 group —— 而 `select project_id, count(*) from kg_item_group group by project_id` 只有一行 `(5, 53)`，项目 7 是 0；
3. registry 空 → 实体解析不出 `group_id` → `_fetch_holding_rows` 落到名字兜底，而兜底条件是 `h.group_id IS NULL AND h.item_name IN (...)`；《大奉》的行都带 `group_id`，所以一条都匹配不上。

也就是说：**只要是在新项目里生成，哪怕 mentions 完全正确、物品名一字不差，块也必然是 `no_record`。** 本次 p7 的 mention（`镇国剑`、`mention_kind=llm`）恰好证明了这一点。

## 6. seg2 正文对照

块内事实：`镇国剑：no_record`（无归属约束）。库内真值：`镇北王 99–106` → `元景帝 106–∞`。

正文中含「镇国剑」的句子：

```text
· 许七安悄无声息地站在正堂的梁上，目光如炬，穿过层层瓦砾，凝视着墙上那把镇国剑。那剑，代表着大奉王朝的荣耀，开国帝王的荣耀……
· “我是谁，这不重要。”男子轻抚着镇国剑的剑柄，语气平淡，“重要的是这把剑。”
· “这把剑，本应属于我。”男子继续说道，“它见证了我的巅峰，也见证了我的陨落。”
· “这是镇国剑的力量。”男子说道，“它已经认主，认出了我。”
· 突然，镇国剑发出一声低鸣，剑身开始颤动……
```

判定：

| 维度 | 结果 |
|---|---|
| 是否照块写 | 不适用。块是 no_record，没有可照的归属 |
| 是否回避 | 场景上「剑在镇北王府正堂」与真实持有（镇北王）相容，但没有写出归属人 |
| 是否写反 | 相对**真值**是写反：正文让一名「自称开国皇帝」的男子说「剑本应属于我/认主认出了我」。开国皇帝既不是镇北王也不是元景帝。因为块没给事实，这属于「无约束时的自由发挥恰好写错」，而不是「违背注入」；一旦块按真值注入，这段就是正面冲突 |
| 附带质量 | seg2 几乎复述 seg1 的同一场景（梁上、华服男子、李灵素），没有推进到下一节点「地书传承」；「地书」两段正文都没出现，因此抽取也没有地书 mention |

## 7. 新增数据

| 项目 | generations | facts | kg_nodes | kg_entities | kg_mentions | agent_runs |
|---|---|---|---|---|---|---|
| p6（无效尝试） | 2（743 / 1284 字，id=6/7） | 10 | 10 | 0 | 0 | 2（77/78 均 `running`） |
| p7（有效尝试） | 2（687 / 772 字，id=8/9） | 6 | 6 | 5 | 5 | 2（79 `success` / 80 `running`） |
| 合计 | 4 | 16 | 16 | 5 | 5 | 4 |

另：`kg_events` +6、`kg_relations` +4（都在 p6/p7 名下）；`projects` +2。
`backend/data/generated.db` **没有任何写入**（mtime 仍是 21:06:04，早于验证）。`style_distills` 未新增（仍 2 行）。

## 8. 重写路径复查（真实环境）

seg2 命中 AI 味重写（`似乎×3、仿佛×2、心中充满了×1`）：

- 主调用块与重写调用块 `rstrip` 后逐字一致（长度均 163）；
- 重写 system = 340 字符（含 `【本段锁定信息…】` 与第 8 条），user = 992 字符（块 + 待改正文）；
- `rewrite_info = {original_chars: 771, rewritten_chars: 772, item_holding_injected: True, constraints_injected: True}`；
- `_strip_thinking(重写输出) == 落库 seg2` 为 **True**。

结论：上一轮修的「重写丢块」没有复现，真实库路径下重写确实带着块。

## 9. 清理方式

保留是安全的：所有新增行都带 `project_id ∈ {6,7}`，检索/注入/统计都按项目过滤；`generated.db` 与全局 `kg_item_holding` 未被写。副作用只有：项目列表多两条；`agent_runs` 里三条 `running`（77/78/80）是进程退出留下的，**如果之后启动批抽 worker，`recover_running` 会把它们重新入队并补抽**（会给 p6/p7 补 mention，不改变 §0 的结论）。不想被补抽就把这三条置 `failed` 或随项目一起删。

要完整删除验证项目，按 `project_id` 逐表删即可（有行的 8 张表）：

```sql
DELETE FROM agent_runs   WHERE project_id IN (6,7);
DELETE FROM generations  WHERE project_id IN (6,7);
DELETE FROM facts        WHERE project_id IN (6,7);
DELETE FROM kg_nodes     WHERE project_id IN (6,7);
DELETE FROM kg_entities  WHERE project_id IN (6,7);
DELETE FROM kg_mentions  WHERE project_id IN (6,7);
DELETE FROM kg_events    WHERE project_id IN (6,7);
DELETE FROM kg_relations WHERE project_id IN (6,7);
DELETE FROM projects     WHERE id IN (6,7);
```

全量回滚（会一并删掉两个验证项目，回到验证前）：`backend/backups/novel_dev-pre-held-verify-20261110-222612.db`。`generated.db` 不需要回滚。

## 10. 建议（待决定，本轮未动）

1. ~~先消歧，再谈共享~~ **（2026-11-10 订正）**：`kg_item_group` 里「镇国剑」只有 1 行（id=100），不存在两个同名 group；`project_id=None` 的 ambiguous 来自跨项目同名**实体**（p5 的 26 与 p7 的 68）。详见 `item-holding-read-scope-2026-11-10.md`。
2. 三条可选治理路径：
   - a. 持有读取按「项目族 / `reference_novel_ids`」解析：项目 7 引用 [5,6] 时，允许读参考语料对应的 group；
   - b. 建项目时把参考语料的 group 复制/映射一份到新项目（数据冗余，但隔离最干净）；
   - c. 读取允许全局 fallback + 显式消歧（同名 group 先合并或按 instance 指定）。
3. 本次 p7 没有出现「文件/邮件」这类类目噪声（只抽到「镇国剑」），上一轮报告的结论不受影响。

## 11. 复现材料

- 第 2 次执行：`D:\Temp\run_held_verify2.py`，捕获 `D:\Temp\held_verify_capture2.json`（12 次调用全文、块偏移、`rewrite_info`、落库正文）
- 第 1 次（无效）：`D:\Temp\run_held_verify.py`，捕获 `D:\Temp\held_verify_capture.json`
- 对照/清理取证：`D:\Temp\collect_held_evidence.py`、`D:\Temp\probe_cleanup_tables.py`
