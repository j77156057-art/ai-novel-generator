# 物品持有注入块：真实数据验收报告

- 日期：2026-11-10
- 范围：`settings.kg_item_holding_inject` → `_item_holding_text` → `build_user_prompt` 的整条注入链
- 数据源：`backend/novel_dev.db`（应用库，只读）+ `backend/data/generated.db`（生成层，只读）
- 结论口径：**链路代码已接，但在这份库上永久不触发；且缺回填入口，历史段无法自救。** 见 §5、§6。

---

## 1. 结论先行

用户的怀疑成立，且比怀疑更严重一层：

1. **卡点确认在 `_active_item_entries` ← `kg_mentions` 为空。** 实测 p1/p5 的 `_active_item_entries` 均返回 `[]`，`_item_holding_text` 均返回 `''`；全库 `kg_mentions = 0`。
2. **不是渲染器坏了。** 在读通道直查和副本补 mention 两条独立路径上，块都能正常渲染出原文（§3.5、§3.6）。缺的只有 mentions 这一环。
3. **但只补 mentions 也救不了这 4 段。** 4 段正文对 132 个 approved 写法裸子串命中 **0**，连「刀」「剑」「法宝」「法器」「镜」都是 0 次。即使强跑抽取，item mention 仍是 0。
4. **真正的问题是「没有回填入口 + 触发依赖正文含物品名」。** 生成侧入队代码是接上的（两条生成路径都无条件调用），但：
   - 全库 `agent_runs` 中 `role='kg_batch_extract'` 为 **0 行**，说明这 4 段从未进入过批抽；
   - `POST /kg-v3/worker/requeue` 只接受 `status='failed'` 的既有行，对这 4 段（无行）无效；
   - 入队队列是**进程内存队列**，进程重启后 `running` 行只会被统计成 `stuck`，**没有任何启动恢复或自动重捞**；
   - 因此：**凡是「生成发生在 e255530 之前」或「入队后进程退出」的段，其 mentions 永远补不回来**——注入块对这些段是永久空串，且没有任何报错或信号。

这不是「需要补充数据」可以概括的事：补数据没有官方入口，只能写一次性脚本或复制库，属于链路缺口。

---

## 2. 验收方法

- 纯只读：`sqlite3` 以 `mode=ro` 打开，SQLAlchemy 只做 `select`；`_active_item_entries`/`_item_holding_text` 直接调用生产函数。
- 未执行任何生成，未修改任何库。
- 临时脚本（不入库）：`D:\Temp\verify_chain.py`、`D:\Temp\verify_narrative.py`、`D:\Temp\verify_fingerprint.py`、`D:\Temp\verify_timeline.py`、`D:\Temp\bench_rrf.py`、`D:\Temp\bench_hybrid.py`。
- 机制验证副本：`D:\Temp\diag_inject_copy.py`（复制 `novel_dev.db` 后补 1 条 mention，**仅证明渲染器可用，明确不作为生产证据**）。

---

## 3. 注入链逐环实测

### 3.1 开关与路径

| 项 | 实测值 |
|---|---|
| `settings.kg_item_holding_inject` | `True` |
| `settings.kg_generated_db` | `...\backend\data\generated.db`（存在） |
| `settings.llm_provider` | `zhipu`（key 已配置，不复述） |
| `settings.embedding_provider` | `dashscope`（key **未**配置 → 实际走 `LocalFallbackProvider`） |

开关这一环没问题。

### 3.2 项目配置（narrative 档案 / 设定 / 参考语料）

| 项目 | 标题 | genre | reference_novel_ids | plot_bible | style_narrative | generations |
|---|---|---|---|---|---|---|
| p1 | Assistant 测试项目 | 空 | `[]` | 空 | 空 | seg 1, 2 |
| p2 | 调用链验收项目 | 空 | `[]` | 空 | 空 | 无 |
| p3 | 调用链验收项目 | 空 | `[]` | 空 | 空 | 无 |
| p4 | 调用链验收项目 | 空 | `[]` | 空 | 空 | 无 |
| p5 | `4` | 悬疑 | `[5, 6]` | characters/relationships/direction/conflicts/foreshadowing/synopsis | **有**（tier=fast, source_ids=[5,6], narration_version=7） | seg 1, 2 |

- 参考语料实际内容（`novel_chunks`）：novel 5 = 《大奉打更人》5094 chunk；novel 6 = 《豪门宠婚》1781 chunk。
- **只有一个项目（p5）挂着《大奉》语料，且它的 premise 是都市悬疑**（「我是一名刚毕业的记者，接到一个关于失踪人口的线索」）。不存在「贴着《大奉》语料的生成项目」——这张表里没有可用的第 2 步载体。

### 3.3 `_active_item_entries` 的取值来源

代码位置：[engine.py](backend/app/generation/engine.py#L693-L717)

```
KgMention.project_id == project.id
KgMention.segment_no == current_seg        # current_seg = _next_segment_no() - 1
→ KgEntity.id IN (...) 且 KgEntity.kind == 'item'
→ [{"name": canonical_name, "item_id": id}]
```

即：**只认「上一段」的 item mention**。三个前置条件缺一不可——上一段生成过、上一段有 item mention、该 mention 指向 `kind='item'` 的实体。

### 3.4 真实库里的实际值

| 项目 | `_next_segment_no` | `current_seg` | kg_mentions(该段) | `_active_item_entries` | `_item_holding_text` |
|---|---|---|---|---|---|
| p1 | 3 | 2 | 0 | `[]` | `''` |
| p2 | 1 | 0（<1 直接返回） | — | `[]` | `''` |
| p3 | 1 | 0 | — | `[]` | `''` |
| p4 | 1 | 0 | — | `[]` | `''` |
| p5 | 3 | 2 | 0 | `[]` | `''` |

配套计数：

| 指标 | 实测 |
|---|---|
| `kg_mentions` 全库 | **0** |
| `kg_entities` p1/p2/p3/p4 | 0 行（任何 kind） |
| `kg_entities` p5 | 仅 `item` 43 行（无 character） |
| `agent_runs` 全库 | 71 行，全部 `('assistant','success')` |
| `agent_runs role='kg_batch_extract'` | **0** |

`generations` 表只有 `id/project_id/segment_no/chapter_range/text/summary`，**没有时间戳列**，无法直接给 4 段定日期。旁证：4 段的 `summary` 只有 `{'text': ...}` 一个键，而当前 `_commit_segment` 无条件写入 `era`/`power` 元数据（[engine.py](backend/app/generation/engine.py#L471-L473)），说明这 4 段由更早版本的代码写入。批抽挂主链的提交 `e255530` 是 2026-10-15 18:14，物品块并 prompt 的 `08acfcd` 是 2026-11-10 16:12。

### 3.5 读通道本身：正常（真实 `generated.db`，不经 mentions）

| 查询名 | 返回 |
|---|---|
| 镇国剑 | 权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞） |
| 神剑 | 通称/歧义名：正文里至少指三件不同的剑…候选：镇国剑 / 洛玉衡祖传神剑 / 佛门金身黄金神剑 |
| 地书 | 权属（own）归天地会 |
| 玉石小镜 | 类通称，不对应单件；候选：地书（权属（own）归天地会） |
| 三号碎片 | 六段完整 custody 时间线（金莲师兄 0–51 → 许七安 51–74 → 魏渊 74–76 → 许七安 76–105 → 许新年 105–108 → 许七安 108–∞） |
| 长枪 | 银枪：银甲女子 156–228；李妙真 227–228；杨砚 228–∞ |
| 铜锣 | 无持有记录（no_record） |
| 腰牌 | 无持有记录（no_record）；「腰牌」→金牌 是待审映射，本轮不作数 |

读通道（前一段刚修的分层与全量时间线）在真实库上工作正常。

### 3.6 渲染器：可用（副本机制验证，**非生产证据**）

在 `novel_dev.db` 的副本上补 1 条 `kg_mentions(project=5, entity_id=26, segment_no=2, mention_kind='alias')` 后：

- `_active_item_entries` → `[{'name': '镇国剑', 'item_id': 26}]`
- `_item_holding_text` →

```
- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）
```

- `build_user_prompt` 中出现（[prompts.py](backend/app/generation/prompts.py#L432-L443)）：

```
【物品持有事实（已写内容最新一段；续写须与此一致）】
- 镇国剑：权属（own）归镇北王（99–106）；权属（own）归元景帝（106–∞）
（本块只补「物品在谁手上」，不改动也不覆盖【剧情圣经】与【世界观事实】的既有约束。…）
```

这条只证明「mention 有了 → 块能渲染」，**不是生产证据**：mention 是人造的，真实库一行未写。

---

## 4. 为什么这 4 段注定 0：正文里没有物品名

对 4 段正文逐条比对 `kg_item_surface` 里 132 个 `approved` 写法（裸子串）：

| 段 | 长度 | 命中写法 |
|---|---|---|
| p1 seg1 | 1036 | 无 |
| p1 seg2 | 1341 | 无 |
| p5 seg1 | 1413 | 无 |
| p5 seg2 | 2273 | 无 |

单词核对：`刀` 0/4、`剑` 0/4、`法宝` 0/4、`法器` 0/4、`镜` 0/4。

所以：

- 走 LLM 抽取 → 文本里没有物品 → 抽不出 item mention；
- 走 `_index_mentions_only` 别名降级（[batch_worker.py](backend/app/kg/batch_worker.py#L793-L842)）→ 候选写法在文本里不命中 → 同样 0 条。

**注：即便这 4 段补了 mention，也等于凭空造证据，不能做。** 这也是本轮不跑「再生成一次」的原因——p1/p5 再生成仍然是悬疑文本。

---

## 5. 归因：三个独立缺口

### 缺口 A（主因）：`kg_mentions` 从未被写入，且没有回填入口

- 代码侧确实接上了：同步与流式两条生成路径在 `_commit_segment` 之后都**无条件**调用 `_enqueue_v3_extraction`（[engine.py L1558](backend/app/generation/engine.py#L1556-L1558)、[L1747](backend/app/generation/engine.py#L1746-L1747)），入队失败也会把 `{"enqueued": False}` 回传前端。
- 但**这份库上从未发生过**：`kg_batch_extract` 0 行。
- 恢复手段只有 `requeue_failed`（[batch_worker.py L405-L447](backend/app/kg/batch_worker.py#L405-L447)），它要求行已存在且 `status='failed'`；**对「根本没有行」的段和「stuck 的 running 行」都无效**。
- `main.py` 里没有任何启动期扫描/重灌（grep `batch_worker|ensure_worker` 在 `app/main.py` 无命中）；`stuck` 只被统计（[L374-L377](backend/app/kg/batch_worker.py#L374-L377)），不会被重捞。

**判定：缺陷。** 表现是静默的——注入块直接为空串，prompt 与旧版无异，没有任何告警。

### 缺口 B：触发依赖「上一段正文命中物品名」这一数据前置

`_active_item_entries` 只看上一段。这带来两个后果：

1. 首段永远拿不到块（`current_seg=0` 直接返回空）——设计上合理；
2. 上一段没提物品 → 下一段也拿不到块。对「刚开篇、器物尚未出场」的项目，块会静默缺位。这不是错误，但意味着**注入是否生效取决于正文内容，而非配置**，无法从「开关=True + 有档案」推断出「一定会注入」。

### 缺口 C：p5 的叙事档案指纹不命中 → `build_context` 会触发全量蒸馏

`build_context` 在 [L565](backend/app/generation/engine.py#L565) 调用 `_style_narrative` → `project_narrative`：

| 项 | 值 |
|---|---|
| `project.narrative_tier` | `balanced` |
| 缓存档案 fingerprint | `{source_ids:[5,6], narrative_version:7, tier:'fast', tier_signature:'8f041a76c181'}` |
| 期望 fingerprint（按项目字段） | `{..., tier:'balanced', tier_signature:'8aaceb261c43'}` |
| 是否命中缓存 | **否** |

缓存不命中就会走 `corpus_content_hash`（全量 6875 chunk 加载 + sha256）→ 蒸馏库查找 → **真实提取（按章采样调 LLM）**。实测 `build_context(p5)` 超过 120s 未返回（已中止），与这条路径吻合。也就是说：**p5 现在连「组装上下文」这一步都过不去**，更谈不上跑真实生成。

（若把 `narrative_tier` 改回 `fast`，或复用已存在的 `style_distills` 行 —— `source_ids=[5,6] + tier=fast + narrative_version=7`，`n=60 章 / 127 条规则`，`created_by_project_id=5` —— 缓存即可命中，这一步会变成纯读。）

### 附带发现：`hybrid_search` 的 RRF 融合是 O(n²)

[search.py L64-L71](backend/app/retrieval/search.py#L64-L71) 把 `_rrf(bm25_rank)` / `_rrf(vec_rank)` 写在了 dict 推导式**内部**，对每个 chunk 重算一次全量排名表。实测（真实库、真实语料、只读）：

| 语料 | chunks | 现有写法 | 提出循环后 |
|---|---|---|---|
| n=1000 | 0.25s | 0.4ms | 652x |
| n=2000 | 1.04s | 0.8ms | 1298x |
| n=6875 | **12.58s** | 2.9ms | 4380x |

`hybrid_search` 整体：novel 5（5094 chunk）11.94s、novel 6（1781 chunk）2.29s。每次 `build_context` 都要付这笔钱（p5 两本语料 ≈ 14s）。这不是本次验收的卡点，但它会拖住任何真实生成，且修法只有两行（把两次 `_rrf` 提到推导式外）。

---

## 6. 影响范围：只在这几个项目不生效，还是普遍接不上？

分三种情况，结论不同：

| 情况 | 注入块是否出现 | 依据 |
|---|---|---|
| 新代码（≥ e255530）上**新生成**、正文含已登记物品名、worker 存活并完成抽取 | **会** | 副本验证渲染器可用；写入路径 `_index_mentions_only` / `_persist_extraction` 均在 |
| 生成发生在 e255530 之前的段（本库 4 段全部属于此类） | **永久不会** | 无 kg 行 → requeue 不接受 → 无回填脚本/接口 |
| 入队后进程退出（`running` 残留） | **永久不会** | 队列是内存态；`stuck` 只统计不重捞；`requeue` 只收 `failed` |
| 新项目首段抽取失败、且库内**无任何实体** | 后续也补不上 | `_mention_candidates` 只读库内已有实体，空库时降级路径返回 0；且不回溯历史段 |
| 有语料但 premise/正文不含物品（p5 现状） | **永久不会** | 正文 0 命中（§4） |

所以准确表述是：

- **不是「所有项目都接不上」**：代码路径完整，健康路径（新生成 + 正文含物品 + worker 存活）能出块。
- **也不是「只是这几个空项目缺数据」**：缺的是**回填能力**。历史段、崩溃残留段在任何配置下都无法进入抽取，且失败完全静默。这是链路的可靠性缺陷。
- 本库的 4 段属于「双重不可达」：既没有 kg 行（缺口 A），正文也不含物品名（缺口 B）。

---

## 7. 最小可行触发方式

### 7.1 机制验证（已做，不算生产证据）

在库副本上补 1 条 `kg_mentions` → 块渲染成功（§3.6）。仅用于排除「渲染器坏了」这一可能。

### 7.2 真实证据路径（需要新增一个项目，成本约 2 次生成 + 1~2 次抽取）

现有 5 个项目没有一个可作载体（p1–p4 无语料；p5 语料是《大奉》但 premise 是都市悬疑）。最小可行路径：

1. **建项目**：`title=大奉验收`，`genre=仙侠/古装权谋`，`premise` 写成大奉语境（明确提到「镇国剑」「地书」等已登记物品），`reference_novel_ids=[5, 6]`，`narrative_tier='fast'`。
2. **关键省钱点**：`tier=fast + ids=[5,6]` 会命中已存在的 `style_distills` 行（`source_ids=[5,6], tier=fast, narrative_version=7`），**不会触发全量蒸馏**，绕开缺口 C。
3. **生成 seg1**：正文里必然出现物品名（premise 已点名）。
4. **落 mentions**：同进程内 `ensure_worker()` 后调 `batch_worker.flush_project(project_id)`（或等后台 worker 自然处理）；检查 `kg_mentions` 里出现 `kind='item'` 的行。若 LLM 抽取失败，别名降级路径也会落 mention（前提是第 3 步正文确实含规范名/别名）。
5. **生成 seg2**：`_active_item_entries` 取 seg1 的 item mention → 块应出现。**截两样东西**：
   - 渲染后的 user prompt 里【物品持有事实】整段原文（可在 `build_user_prompt` 前后落临时文件，或直接调 `build_context(...)["item_holding_block"]` 打印）；
   - 生成正文中与该物品归属/状态对应的句子，逐条比对「谁手上、什么状态」。
6. 若要判断「正文是否真的遵守约束」，比对点：prompt 里写「镇北王 99–106 / 元景帝 106–∞」这类区间，正文是否写成当前时间点对应的人；出现相反归属即为未遵守。

**副作用**：会在真实库新增 1 个项目、2 段正文、若干实体/mention 行。是否需要我在真实库执行、还是先在 `novel_dev.db` 副本上跑（副本需要运行时替换 `app.db.SessionLocal` 才能让 worker 指向副本），请拍板。

### 7.3 若要真正修链路（不是验收）

最小修复集合（按价值排序）：

1. 给 `kg_mentions` 补一个**回填入口**：按 `project_id` + 已有 `generations` 逐段调 `_index_mentions_only`（零 LLM，可反复跑，幂等已由 `(project, entity, segment)` 查询保证）。这是缺口 A 的正解。
2. `requeue_failed` 放开对 `stuck running` 行的支持（或启动时把超时 `running` 行重置为 `failed`）。
3. `build_context` 在叙事档案指纹不命中时**不应同步蒸馏**：要么按缓存 tier 兜底、要么异步蒸馏 + 本次跳过该块。这是缺口 C。
4. `hybrid_search` 把 `_rrf` 提出推导式。

---

## 8. 本轮没有做的事

- 没有跑任何生成（p1/p5 再生成仍是悬疑文本，结果必然 0，不构成证据）。
- 没有修改那 4 段模板文本。
- 没有向真实库写入任何 mention / 实体 / 项目。
- 没有把副本上的人造 mention 当生产证据引用（§3.6 已显式标注）。

## 9. 复现命令

```powershell
# 只读：注入链逐环实测
python D:\Temp\verify_chain.py

# 只读：项目叙事档案 / 指纹
python D:\Temp\verify_narrative.py
python D:\Temp\verify_fingerprint.py

# 只读：时间线旁证
python D:\Temp\verify_timeline.py

# 纯计算：RRF 复杂度
python D:\Temp\bench_rrf.py

# 真实语料上的混合检索耗时
python D:\Temp\bench_hybrid.py

# 机制验证（写副本，不写真实库）
python D:\Temp\diag_inject_copy.py
```
