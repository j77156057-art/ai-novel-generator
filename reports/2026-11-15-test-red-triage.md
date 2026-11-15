# 测试红灯分诊（2026-11-15，只读）

> 范围：项目根 `pytest`（`testpaths = tests`，共收集 **1126** 项，三段执行合计 1127 项次，含参数化）。
> 约束：不改被测代码、不改配置、不 commit。本文只做归因与动作建议。

## 0. 结论先看

| 类别 | 条数 | 含义 |
|---|---:|---|
| 在制品红 | **25** | 别的会话正在改的模块引起，现在修会撞车 |
| 真回归 | **4** | 不是别人在动的区域，改动外溢弄坏的 |
| 断言过期 | **5** | 行为是有意改的，断言该跟着更新 |
| 既有失败（11-14 起就在） | **4** | `test_stat_evidence` / `test_name_audit` / `test_evidence_quotes` / `test_extract_split` |
| 环境噪声（并发写主库） | **1** | `concurrent_router_e2e` 的真实库守卫 |
| 合计 | **39**（38 FAILED + 1 ERROR） | |

**与你记录的 47 FAILED + 1 ERROR 差 8 条**，原因两条，都不是「分诊漏了」：

1. **代码在我跑测期间仍在改**。`backend/app/generation/engine.py` 于 **06:38**、`backend/app/config.py` 于 **06:38** 仍在被写入（我 06:00–06:25 跑测），是移动靶。
2. **我分三段跑**（见 §1 方法）。每段是独立 pytest 会话：模块级状态、临时库、真实库快照都会重置，**跨文件污染型**失败在分段跑里可能不复现。差出来的 8 条最可能是这类 + 下面 §4 的两条主库守卫（时序依赖）。

§4 给出了在单会话整轮里复测这 8 条的具体做法。

## 1. 方法与环境

- 命令：`python -m pytest <files> -q --tb=line -p no:cacheprovider --junitxml=...`，按收集顺序（= 文件名字典序）切成 3 段跑完 62 个文件 / 1126 项。
  - G0（14 文件 / 385 项）：**全绿**，413 s
  - G1（20 文件 / 372 项）：19 F + 1 E，327 s
  - G2（28 文件 / 370 项）：19 F + 2 xfail，501 s
- 解释器：系统 Python 3.13；门禁子进程用 `backend/venv/Scripts/python.exe`（与 `test_legacy_script_suite` 一致）。
- **并发干扰已排查**：
  - 我起跑时有一个 A/B 实验进程 `exp_run.py --seq W,A1,B1,A2,B2,A3,B3`（首字等待实验）在跑，**它自己在脚本里 `os.environ.pop("DATABASE_URL")` 且声明「不写任何库」**，只打 MiniMax 接口；06:0x 已结束。它不是任何失败的直接原因，但它与打点进程共用 CPU/网络，**长耗时用例的耗时数字不可信**（结论不受影响）。
  - 常驻 `uvicorn app.main:app --port 8000`（PID 33868，04:22 起）全程在跑，是 §5 主库写入的嫌疑源。
  - 出网可用（`open.bigmodel.cn` 200），所以「网络被墙导致 provider 不可用」这一类推测可以排除。

## 2. 分诊表

### A. 在制品红（25）

| # | 测试 | 依据 | 建议动作 | 归属模块 / 会话 |
|---|---|---|---|---|
| 1-5 | `test_finish_reason_truncation::test_chat_warns_when_truncated` / `test_chat_no_warn_when_normal_stop` / `test_stream_warns_when_truncated` / `test_stream_no_warn_when_normal_stop` / `test_truncation_keeps_reasoning_strip` | 全部报 `_Resp.read() takes 1 positional argument but 2 were given` / `'_StreamResp' object has no attribute 'read'`。`provider._read_body()` 改成 `reader = getattr(resp,"read1",None) or resp.read` 后调 `reader(65536)`（[provider.py](backend/app/llm/provider.py#L632-L682)），流式改走 `_iter_stream_lines(resp, guard)`。测试里的假响应桩只实现了无参 `read()` / `__iter__` | 等 provider 侧停手后，把假桩补成「`read(n)` 接受长度 + `read1(n)` 可选 + 可迭代」，**不要**为迁就测试回退 provider | `app/llm/provider.py`（05:28 改）— provider 分块读/速率判据会话 |
| 6-10 | `test_reasoning_wiring::test_chat_strips_reasoning` / `test_chat_without_reasoning_unchanged` / `test_stream_chat_strips_reasoning` / `test_stream_chat_without_reasoning_unchanged` / `test_summarize_paths_also_strip` | 同上，`_FakeResp.read()` 参数不匹配 / `_FakeStreamResp` 无 `read` | 同 #1-5，与 truncation 那批一起改假桩 | 同上 |
| 11 | `test_legacy_script_suite[backend·test_p0_regression.py]` | 脚本尾部 `RuntimeError: OpenRouter chat 调用失败: main.<locals>._Resp.read() takes 1 positional argument but 2 were given` | 同 #1-5；该脚本自带桩，一起补 `read(n)` | 同上（脚本本身 10-14 未改） |
| 12-16 | `test_distill_fixes::test_all_chunks_failed_raises_instead_of_silent_success` / `test_all_chunks_unparseable_also_raises` / `test_partial_chunk_failure_recorded_not_raised` / `test_run_distill_job_partial_failure_visible_in_stage` / `test_generation_path_extraction_still_commits_project_cache` | 「Regex 不匹配」×2 = 全失败时的报错文案变了；「部分失败数没记上」= 返回字典里取不到 `failed_chunks`；「部分失败没写进 stage」= stage 文案变成「蒸馏完成（5 个语料块调用失败，已跳过）」后断言仍按旧串匹配；`test_generation_path_...` 拿到 `None`（生成路径不再走落库分支） | 等蒸馏留痕口径定稿（`failed_chunks` / `failed_reasons` / stage 文案），再一次性对断言 | `app/style/narrative.py`（11-15 01:44 改，且已 staged）+ `app/style/jobs.py`—蒸馏留痕会话 |
| 17-21 | `test_writer_selfcheck::test_full_tier_rewrite_prompt_includes_finding_evidence` / `test_fast_tier_does_not_gather_evidence` / `test_retrieval_failure_is_tripped_not_fatal` / `test_no_evidence_falls_back_to_legacy_prompt` / `test_stream_entry_also_gets_evidence` | 五条都报「未找到重写 prompt，共 1~2 次 chat」，打印出的两次 chat 是「初稿补完」与「断点续写」——**重写轮没被触发**。engine 新增 `rewrite_max_rounds` / `rewrite_stop_without_gain` 调度（[config.py](backend/app/config.py#L300-L310)） | 等 engine 调度定稿，确认这 5 条该断言的是「重写轮被调度」还是「新调度下不再重写」；**不要**先改 engine 去迁就测试 | `app/generation/engine.py`（06:38 仍在改，+3848 行）— 主链路会话 |
| 22 | `test_stream_kg_bookkeeping::test_bookkeeping_failure_does_not_break_stream` | 断言落库正文等于原文，实际每段之间多了空行（`\n` → `\n\n`）：正文落库前多了段落归一化 | 同上，等 engine 落库口径定稿后改断言或改调用 | `app/generation/engine.py` |
| 23 | `test_stream_kg_bookkeeping::test_rewrite_failure_does_not_crash_generation` | 「重写失败不应产出 rewrite_info」，实际 `generation.get("rewrite_info")` 仍有完整 dict（含 `reasons`）——`rewrite_info` 语义从「重写成功才写」变成「重写尝试就写」 | 同上；若语义是有意的，这条转「断言过期」 | `app/generation/engine.py` |
| 24 | `test_regenerate_no_wasted_context::test_regenerate_executes_no_sql_against_novel_chunks` | 捕获到 1 条 SQL：`SELECT count(*) ... FROM novel_chunks WHERE novel_id IN (?)`。来源是 [engine.py:317](backend/app/generation/engine.py#L317) 新增的语料规模统计 | 等 engine 定稿：若这条 count 是「语料量体检」就必须进白名单（测试允许 count，不允许读行），否则回退 | `app/generation/engine.py` |
| 25 | `test_qa_attack_ai_flavor_loop::test_e1_byte_identical_without_override_vs_pre_change` | 「除 master 有意新增的 2 条 AI 腔禁忌外，system 必须与 `4227c73^` 逐字节相同」——`prompts.py` 已迭代到 **v17**（v10–v17 连续改提示词），断言的金标准点还是旧的 | 等 prompts 序列收尾，按最终版本重算金标准哈希并在提交说明里写「为什么这次改是真的」 | `app/generation/prompts.py`（05:25 改）— 提示词金标准会话 |

### B. 真回归（4）

| # | 测试 | 依据 | 建议动作 | 归属模块 |
|---|---|---|---|---|
| 26 | `test_settings_roles::test_model_lists_are_consistent_across_three_sources` | `minimax: config 的 minimax_models 与 PROVIDER_META 不一致（['MiniMax-M3.1-Flash-Preview'] != ['MiniMax-M3.1-Flash-Preview','MiniMax-Text-01']）`。11-13 下线 Text-01 时只改了 `config.py`，[routers/settings.py:110](backend/app/routers/settings.py#L110) 的 `PROVIDER_META.default_models` 没跟着删。**三源一致性断言本身没过期**，是代码漏改 | 删 `PROVIDER_META` 里的 `MiniMax-Text-01`；顺手清 `provider.py:1046` 的 `_MODEL_MAX_TOKENS = {"MiniMax-Text-01": 40000}` 残留。最小复现：`pytest tests/test_settings_roles.py::test_model_lists_are_consistent_across_three_sources` | `app/routers/settings.py`（10-18 未动，无人改，安全） |
| 27 | `test_legacy_script_suite[backend·tests·test_e2e_journey_pipeline.py]` | 3 条失败：`unauthorized_knowledge 已落库（豁免前）`、`slice 提及 林悠然/顾辰逸（KG 事实回流）`。单跑可复现，与网络无关（脚本用 mock/本地库）。KG 侧 `models.py`（11-14 13:08）、`retrieval_v3.py`（11-14 06:07）是最近改动 | 先确认 `unauthorized_knowledge` 规则与 `slice_for_prompt.recent_entities` 是否被 `models.py` 的表/字段改动影响；最小复现即单跑该脚本 | `app/kg/*` + `app/models.py`（11-14 改动，今晚无人动） |
| 28 | `test_legacy_script_suite[backend·tests·test_v30_extractor.py]` | 单跑 1 条失败：`entity mention_count: got=0 want=2`（其余全过）。`extractor_v3.py` 的 diff 只加了 `note` 参数，`mention_count` 落库侧在 `holding_projection/models` | 顺 #27 一起查 mention 计数落库；若确认是 `models.py` 波及，与 #27 合并修 | 同上 |
| 29 | `test_legacy_script_suite[backend·test_m4_api.py]` | 2 条失败，都与 bible 伏笔有关：`bible 伏笔进入 system_prompt`、`bible 伏笔进入 constraints（实际：[]）`。`bible.py`（11-14 05:59）与 `prompts.py` v15/v17 都动过注入面 | **先不要动**：prompts 正在改，这条可能是注入面重构的正常中间态。等 prompts 收尾后若仍红，再按「bible `foreshadowing` 未合并进 system/constraints」查 [prompts.py:189-190](backend/app/generation/prompts.py#L189-L190) | `app/generation/prompts.py` + `app/kg/bible.py`（**有冲突风险**） |

### C. 断言过期（5）

| # | 测试 | 新契约（据代码注释） | 建议动作 | 归属 |
|---|---|---|---|---|
| 30 | `test_provider_timeouts::test_writer_default_chat_120s_and_stream_180s` | 「chat=120 / stream=180」不再是契约。现在两端都从 settings 读：`writer_timeout` 默认 **180**（11-14 由 120 提到 180，理由：4000 字级单章实测需 180s+），`writer_stream_timeout` 默认 180。Assistant 仍显式传 60 | 改断言为「默认 = `settings.writer_timeout` / `settings.writer_stream_timeout`，且两者同档；Assistant 显式 60 不变」。**不要**把默认值改回 120 | `app/llm/provider.py` + `app/config.py`（与 #1-11 同源但**不冲突**，可在 provider 收尾后改） |
| 31 | `test_settings_roles::test_put_writer_role_still_reports_singleton_values_on_get` | 同 #30：GET 的 writer 块如实展示单例真实值，现在就是 **180**（`assert 180 == 120`） | 断言改为与 `settings.writer_timeout` 比较，不写死 120 | 同上 |
| 32 | `test_regenerate_no_wasted_context::test_regenerate_prompt_bytes_unchanged` | 重生成的 (system,user) 金标准哈希变更是**有意的**（prompts v10–v17 连续迭代），但测试要求「必须重新论证而不是随手改常数」 | 等 prompts 收尾 → 逐条说明哪些指令变了、为什么 → 再重算哈希 | `app/generation/prompts.py`（**需等收尾**） |
| 33 | `test_regenerate_no_wasted_context::test_only_style_fit_touches_chunks_not_whole_rows` | 新契约：续写渲染**必须**提供 KG timeline，缺了就抛 `ValueError("续写渲染必须提供 KG 时间线…")`（[narrative.py:7445-7451](backend/app/style/narrative.py#L7445-L7451)）。测试仍按「render 可不传 timeline」调用 | 测试补 timeline 入参；同时确认该硬要求只作用于 `RENDER_MODE_CONTINUATION` | `app/style/narrative.py` |
| 34 | `test_legacy_script_suite[backend·tests·test_v25_branch.py]` | 唯一失败：`b-n1-1.children 含 b-n1-2`。`branch.py` 新增 `chapter_based` 语义，并把锚点归一化到「父主干」（`if cur.kind == "branch": anchor = cur.parent ...`），所以段模式下支线不再嵌套挂支线，改为同级挂主干 | **需 branch.py 作者确认**：段模式是否也有意改成同级。确认有意 → 改脚本断言；确认只该作用于章模式 → 属回归（把锚点归一化加 `chapter_based` 条件） | `app/generation/branch.py`（改动中） |

### D. 既有失败（4，11-14 旧日志里就在）

| # | 测试 | 说明 |
|---|---|---|
| 35 | `test_legacy_script_suite[backend·tests·test_stat_evidence.py]` | 既有，按你的记录单独列出，不计入本轮新增 |
| 36 | `test_legacy_script_suite[backend·tests·test_name_audit.py]` | 同上 |
| 37 | `test_legacy_script_suite[backend·tests·test_evidence_quotes.py]` | 同上 |
| 38 | `test_legacy_script_suite[backend·tests·test_extract_split.py]` | 同上 |

### E. 环境噪声（1）

| # | 测试 | 依据 |
|---|---|---|
| 39 | `test_legacy_script_suite[tests·concurrent_router_e2e.py]`（teardown error） | `真实库被测试脚本改动了：['novel_dev.db']`。**归因成立，但不是这个脚本干的**：我把该脚本单独按门禁口径跑了一遍（临时库 + PYTHONPATH），前后 `backend/novel_dev.db` 的 sha256 与 mtime **完全不变**，脚本自身 PASS。写入来自**外部进程**（常驻 uvicorn 上的 KG worker 心跳 / 后台任务）。详见 §5 |

## 3. 现在就能安全修的

按「改动面小 + 无人在动 + 不依赖别人定稿」排序：

1. **#26 模型清单三源不一致**：删 `routers/settings.py` `PROVIDER_META` 里的 `MiniMax-Text-01`（顺带清 `provider.py:1046` 残留）。`settings.py` 最后改动 10-18，今晚无人碰，零冲突。
2. **#27 / #28 KG 侧两条**（`e2e_journey_pipeline` 的 unauthorized/slice 回流、`v30_extractor` 的 mention_count=0）：涉及 `models.py`(11-14) / `holding_projection`，今晚无人在动。**建议先只做定位**（谁把 mention/unauthorized 写丢了），改动放最后，因为它和 KG 回填可能共用表。
3. **#39 守卫判法**（只改测试判法，不改业务）：见 §5 建议。`test_db_isolation` 与 `test_legacy_script_suite` 的 `real_db_untouched` 都该从「sha256/字节不变」改成「**只允许心跳键变更**」。
4. **#30 / #31 超时断言**：`provider.py` 停手后即可改（改测试，不动代码）。现在改会和 #1-11 的假桩改动撞同一个文件，**建议排在 provider 收尾后**。

## 4. 必须等别人收尾的

| 等谁 | 等什么 | 关联失败 | 判断「转绿」的依据 |
|---|---|---|---|
| provider 分块读会话（`provider.py` 05:28 起） | 假响应桩补 `read(n)` / `read1(n)` / 可迭代 | #1-11（11 条） | 假桩能跑通 `_read_body` 与 `_iter_stream_lines`，11 条自动转绿；若只改 provider 不改桩，**永远不会自动转绿**，这是需要连带处理的最容易漏的一块 |
| engine 主链路会话（`engine.py` 06:38 仍在改，+3848 行） | 重写轮调度、落库段落归一化、`rewrite_info` 语义、`novel_chunks` count 是否进白名单 | #17-24（8 条） | engine 停手后一次全量重跑；这 8 条里 24 号（count SQL）若被定为「语料体检」需改的是**测试白名单**而不是 engine |
| 蒸馏留痕会话（`narrative.py` 11-15 01:44） | `failed_chunks` / `failed_reasons` / stage 文案定稿 | #12-16（5 条） | 留痕字段写进档案 dict 后，5 条按新字段改断言即可转绿 |
| 提示词金标准会话（`prompts.py` 05:25，已迭代到 v17） | 提示词定稿 + 逐条变更说明 | #25、#32（2 条）、连带 #29 | 定稿后重算两处金标准哈希；#29 若仍红才是真问题 |
| branch.py 作者 | 段模式是否也有意改成「同级挂主干」 | #34 | 有意 → 改脚本；无意 → 加 `chapter_based` 条件后自动转绿 |

## 5. 两条主库守卫：归因确认与改法建议

**归因成立，但写入方不是测试。** 证据链：

1. `batch_worker._heartbeat()`（[batch_worker.py:841-887](backend/app/kg/batch_worker.py#L841-L887)）每 `HEARTBEAT_INTERVAL_SECONDS = 15.0` 用 `SessionLocal()` 往 `app_settings` 写 `kg.worker.<id>`，落的就是 `backend/novel_dev.db`。心跳线程只在 worker 启动后存在。
2. 我在 **06:39** 静默期（无 KG 任务）连续 20 s 采样 `backend/novel_dev.db` 的 mtime：**不变**；`pytest tests/test_db_isolation.py` 单跑 **4 passed**。说明一旦没有 worker 在跑，这两条守卫就是绿的。
3. 06:05–06:12 我的 G1 段跑完 44 个老脚本时，`concurrent_router_e2e` 的 teardown 抓到 `novel_dev.db` 变了；而该脚本**单独**按门禁口径跑，前后 sha256/mtime 完全一致 → 写入是**同一时间窗内的外部进程**（别的会话的生成/回填在跑时，uvicorn 上的 worker 心跳就开始写）。
4. 结论：`test_real_dev_db_untouched_by_test_session`（sha256）与 `real_db_untouched`（size+mtime）**都无法区分「测试写坏了库」与「别的进程在正常跑」**。你记录的 47 里那两条，就是这么来的。

**建议判法（只改测试，不改业务）**：

- 保留「测试库 URL 必须指向临时文件、不得等于任一真实库」这条硬断言（#`test_test_db_is_isolated_to_temp_file`），这是真护栏，别动。
- 把「整文件字节不变」改成「**除 `app_settings` 里 `kg.worker.%` 心跳键外的内容不变**」：
  - 做法 A（推荐，口径最准）：用 `sqlite3` 只读连接对比 `app_settings` 表里 `key NOT LIKE 'kg.worker.%'` 的 `(key, value)` 集合 + `alembic_version`；表结构/行数不一致才算失败。
  - 做法 B（最省事）：守卫改成「前后差异只能落在 `app_settings` 表，且差异行数 ≤ 心跳条数（worker 数）」，允许 mtime 变。
- 另外给守卫一个「**有并发写入者**」的显式出口：检测到 `kg.worker.*` 心跳在跑时，把 sha256 断言降级为 `xfail(strict=False)` 并打印「检测到外部 worker 心跳，主库字节断言不适用」。这样既不被噪声红，也不会真漏掉「测试清库」。

## 6. 未复现的 8 条怎么办

我这次只抓到 39（38F+1E）。差的 8 条最可能在两处：

1. **两条主库守卫**（`test_db_isolation::test_real_dev_db_untouched_by_test_session`、`test_legacy_script_suite[tests·concurrent_router_e2e.py]` 的 teardown）——时序依赖，只在「测试期间有 worker 在写」时红。这两条不是新增故障。
2. **跨文件污染型**：分段跑把会话边界切开了。复测办法（不改代码）：

```
# 停在 worker 空闲时，单会话整轮跑（约 16 min，建议放到不让别的会话生成的时间窗）
python -m pytest -q --tb=line -p no:cacheprovider --junitxml=D:/Temp/jfull.xml
```

跑完把 `D:/Temp/jfull.xml` 与本文 §2 对差：多出来的就是分段跑漏掉的，按「是否落在 §2 的同一模块」并入对应类别即可（大概率仍落在 A/C 两类的同一批模块上）。

## 7. 复现命令

```powershell
# 单条复现（示例）
python -m pytest "tests/test_reasoning_wiring.py::test_chat_strips_reasoning" -q --tb=short

# 老脚本单跑（与门禁同口径）
$env:DATABASE_URL="sqlite:///D:/Temp/x.db"; $env:PYTHONPATH="<repo>\backend"
& "<repo>\backend\venv\Scripts\python.exe" "<repo>\backend\test_m4_api.py"

# 主库写入归因（静默期应不变；有 worker 时每 15 s 变一次）
(Get-Item .\backend\novel_dev.db).LastWriteTime
```

## 8. 复测结果（2026-11-15 07:11–07:30，单会话整轮）

### 8.1 跑测状态（可比的快照）

| 项 | 值 |
|---|---|
| 命令 | `python -m pytest -q > D:\Temp\full_retest.log 2>&1`（单会话、根目录、门禁口径） |
| commit | `cd321ad`（2026-11-12 02:40:38 +0800，`docs(generation): 复读闸门接线方案稿（定稿）`） |
| 工作区 | **脏**：223 项（197 个 tracked 改动，其中 183 已 staged；26 个 untracked） |
| 日志 | `D:\Temp\full_retest.log`，228,717 B / 3,704 行，07:11:10 创建 → 07:30:26 最后写入，**19.3 min** |
| 开跑前关键模块已停手 | `engine.py` 06:38、`provider.py` 05:28、`prompts.py` 05:25、`narrative.py` 01:44、`branch.py` 11-14 06:11 —— 均早于 07:11，**本次结果是一个稳定快照**（§0 里「engine 还在改」的漂移风险已消失） |
| 跑完后又被改、**不在本次结果内** | `backend/app/retrieval/brief/{render,settings,tool}.py`（11:06–11:08）、`backend/app/kg/quality_report.py`（07:40） |

### 8.2 判定：全部用例执行完，但末尾计数行丢失

- 进度条到 **[100%]**（16 行），符号合计 **1128**：`.` 1101 + `F` 23 + `X` 2 + `s` 2。
- `short test summary info` 完整：23 条 FAILED、2 条 SKIPPED、2 条 XPASS，**0 ERROR**。
- 异常点：日志**没有** pytest 最后那行 `23 failed, 1101 passed, ... in ...s`，文件在最后一条 FAILED 之后直接就是 `EXIT=1`。按进度 100% + 汇总清单完整 + 退出码 1（有失败）判定为「跑完、有红」；上面的 1101/23/2/2 是按进度符号还原的。**归因更正（见 §8.6）**：不是收尾丢写，而是 pytest 8.3.4 在 `-q` 下不打印末尾 tally 行。
- 项次 1127 → **1128**（+1）：06:49 给 `test_db_isolation.py` 加了逻辑指纹自检用例，属收集漂移。
- 按你的要求**没有重跑**。

### 8.3 数字对差：39（38F+1E）→ 23（23F+0E），-16

**转绿（16）**

| 旧编号 | 测试 | 转绿原因 |
|---|---|---|
| #1-5 | `test_finish_reason_truncation`（5） | 假响应桩补齐 `read(n)/read1(n)` |
| #6-10 | `test_reasoning_wiring`（5） | 同上；其中 **2 条实际是 SKIP**（见 8.5），不是真绿 |
| #26 | `settings_roles::test_model_lists_are_consistent_across_three_sources` | 删掉 `PROVIDER_META` 的 `MiniMax-Text-01` |
| #31 | `settings_roles::test_put_writer_role...singleton_values_on_get` | 断言跟 settings（180） |
| #30 | `test_provider_timeouts::test_writer_default_chat_120s_and_stream_180s` | 按三层超时新契约重写（非放宽） |
| #27 | `legacy[backend·tests·test_e2e_journey_pipeline.py]` | KG `newest.get(ch) is None` 修复 |
| #28 | `legacy[backend·tests·test_v30_extractor.py]` | 同上（mention_count 恢复 2） |
| #39 | `legacy[tests·concurrent_router_e2e.py]` + `test_db_isolation` | 主库守卫改逻辑指纹（只忽略 `kg.worker.*`） |

**仍红（23，按文件归组）**

| 文件 | 条数 | 明细 | 类别 / 结论 |
|---|---:|---|---|
| `tests/test_distill_fixes.py` | 5 | 全失败文案、部分失败 `5 != 2`、stage 文案、`project_cache is None` | 在制品（蒸馏留痕）。**断言随 fixture 规模过期**：测试按 3 个语料块写死，实际跑 6 块，等 `narrative.py`/`jobs.py` 定稿后改断言 |
| `tests/test_writer_selfcheck.py` | 5 | 全部「未找到重写 prompt，共 1~2 次 chat」 | 在制品（engine 重写轮调度）。与 §2 完全一致，**未见好转** |
| `tests/test_stream_kg_bookkeeping.py` | 2 | 正文多了空行（`\n`→`\n\n`）；`rewrite_info` 在重写失败时仍有值 | 在制品（engine 落库归一化 + `rewrite_info` 语义），与 §2 一致 |
| `tests/test_regenerate_no_wasted_context.py` | 3 | `novel_chunks` count SQL；渲染缺 timeline 抛 `ValueError`；prompt 金标准哈希 `b6ff…` vs `7f55…` | 2 条断言过期（prompt v17、timeline 变硬要求）+1 条在制品（count 是否进白名单） |
| `tests/test_qa_attack_ai_flavor_loop.py` | 1 | system 与 `4227c73^` 不再逐字节相同（新增字数硬上限、伏笔措辞） | 提示词金标准，等 prompts 收尾后重算 |
| `tests/test_legacy_script_suite.py[backend·test_m4_api.py]` | 1 | `bible 伏笔进入 system_prompt` / `constraints` 为空 | prompts + bible 注入面，与 §2 #29 一致 |
| `tests/test_legacy_script_suite.py[backend·test_p0_regression.py]` | 1 | **原因变了**：dummy provider 已能跑（52/55），现在红在「constraints 含 bible 角色（沈砚）/伏笔（铜镜背面的裂纹）」 | **被桩错误掩盖的旧红浮出**，与 m4_api 同源（bible→constraints 注入） |
| `tests/test_legacy_script_suite.py[backend·tests·test_v25_branch.py]` | 1 | `b-n1-1.children 含 b-n1-2` | branch.py 段模式语义，仍需作者确认 |
| `tests/test_legacy_script_suite.py[backend·tests·{test_stat_evidence,test_name_audit,test_evidence_quotes,test_extract_split}.py]` | 4 | 既有失败 | 旧账，本轮未动 |

### 8.4 有没有新出现的红

**按 test id：没有新增红。** 23 条全部落在 §2 的旧清单里。

但有三个变化必须记下来：

1. `legacy[backend·test_p0_regression.py]` **红了换原因**：桩修好后它才真正跑到断言层，暴露的「bible 角色/伏笔没进 constraints」与 `test_m4_api.py` 完全同源——说明 bible→constraints 注入这条链在 `prompts.py` 迭代里被改坏了（或尚未接回），这是当前**唯一一条新暴露的功能性红**，优先级应提到 distill/selfcheck 之前。
2. 新增 **2 条 XPASS**（非 strict，不计失败）：`legacy[backend·tests·test_v30_integration.py]`、`legacy[tests·concurrent_assistant_qa.py]`。以后若加 `strict=True` 会变红。
3. 新增 **2 条 SKIP**：`test_reasoning_wiring.py:137`「库里没有 project 5，跳过端到端」——这 2 条是环境跳过，**不能算已修复**。

### 8.5 下一步（按优先级）

1. **bible→constraints 注入**（p0 + m4_api，2 条）：查 `prompts.py` v15/v17 与 `bible.py` 的合并点，确认伏笔/角色是否被新提示词结构删掉了注入；这条是功能性回归，不是断言过期。
2. **蒸馏留痕**（5 条）：等 `narrative.py`/`jobs.py` 定稿，把 3 块/2 块写死的断言改成按实际块数取。
3. **engine 重写轮**（7 条：selfcheck 5 + bookkeeping 2）：engine 已停手（06:38），可以直接对着最终代码判断「该断言重写轮被调度」还是「新调度下不再重写」。
4. **提示词金标准**（qa_attack 1 + regenerate 1）：prompts 最后写入 05:25，已停手，可以逐条写变更说明再重算哈希。
5. `branch.py` 段模式（1 条）：需 branch 作者一句确认。
6. 4 条既有失败：维持旧账，不在本轮范围。

### 8.6 重跑复测（2026-11-15 15:19–15:39，单会话整轮）

| 项 | 值 |
|---|---|
| 命令 | `python -m pytest -o addopts="-ra" --junitxml=D:\Temp\full_retest_rerun.xml 2>&1 \| Tee-Object -FilePath D:\Temp\full_retest2.log -Append` |
| commit | `989abce`（14:41 首轮跑测后，另一会话把 230 项改动提交了；首轮快照是 `563666b` + 230 项脏） |
| 工作区 | **干净**：DIRTY=0 / STAGED=0 / UNTRACKED=0（开跑时记录） |
| 日志 | `D:\Temp\full_retest2.log` 追加段，15:19:39 → 15:39:15，**19 min 32 s** |
| 收集 | **1130 items**（`testpaths=tests`，与首轮一致） |

**汇总行原文**

```text
= 3 failed, 1123 passed, 2 skipped, 2 xpassed, 4 warnings in 1172.64s (0:19:32) =
```

**FAILED 清单（3 条，全部既有失败）**

1. `tests/test_legacy_script_suite.py::test_legacy_script_passes[backend·tests·test_evidence_quotes.py]`
2. `tests/test_legacy_script_suite.py::test_legacy_script_passes[backend·tests·test_extract_split.py]`
3. `tests/test_legacy_script_suite.py::test_legacy_script_passes[backend·tests·test_stat_evidence.py]`

**与 §8.2 对差**：项次 1128 → 1130（+2）；failed **23 → 3（−20）**；passed 1101 → 1123（+22）；skipped 2 → 2；xpassed 2 → 2；error 0 → 0。**预期 3 failed 命中，无第 4 条红。**

**关于「末尾计数行」**：`pytest.ini` 的 `addopts = -ra -q`，pytest 8.3.4 在 `-q` 下不输出末尾 tally 行。已用 7 条的小样本复现：`-q` 无末行；`-o addopts=""` 输出 `7 passed in 10.50s`。因此本次重跑用 `-o addopts="-ra"`（测试集不变）让汇总行落盘；§8.2 的「收尾丢写」归因据此更正。

**兜底解析**：`D:\Temp\full_retest_rerun.xml` 已生成（日志中 `generated xml file:` 一行），可作为失败/通过数的机器可读兜底。

## 9. 修复落地（2026-11-15 11:xx–12:xx，未 commit）

只改测试文件（`git diff --stat` 仅 4 个 `tests/*.py`），未动 `backend/app`、未重启后端、未 commit/push/checkout。

| 组 | 修前 | 修后 | 依据（现行契约） |
|---|---|---|---|
| `test_distill_fixes.py` | 5 红 / 5 绿 | **0 红 / 10 绿** | 5 章/块 → 30 章=6 块（`MIN_CHAPTERS_PER_CHUNK`）；`failed_chunks=5`；生成路径无缓存时**拒绝同步蒸馏**返回 None 且不写缓存 |
| `test_writer_selfcheck.py` | 5 红 / 0 绿 | **0 红 / 5 绿** | 重写轮 2026-11-14 起走 `stream_chat`；假桩需同时在 chat/stream_chat 记录 prompt 并按 system 标记分流 |
| `test_stream_kg_bookkeeping.py` | 2 红 / 3 绿 | **0 红 / 5 绿** | 落库前段落统一空行分隔（`merge_ultra_short_dialogue`）；重写失败**必须**留痕 `rewrite_info.failed`（「失败也留痕」） |
| `test_regenerate_no_wasted_context.py` | 3 红 / 3 绿 | **1 红 / 5 绿** | 允许节奏统计的 `count/sum`（不读正文）；续写渲染硬要求 KG 时间线 → 夹具补最小事件；金标准哈希未动（待定） |
| `test_qa_attack_ai_flavor_loop.py` | 1 红 | 1 红 | 提示词金标准，待定 |
| `test_legacy_script_suite.py` | 7 红 | 5 红 | `p0` / `m4_api` 由**另一会话**改脚本后转绿（11:49 / 11:52），非本轮改动；其余见下 |

**仍红 7 条**：`regenerate` 提示词哈希、`qa_attack` 金标准、`legacy[v25_branch]`（段模式支线挂载语义），加 4 条既有失败（按约束不动）。

**结转事项（待决断）**

1. **提示词金标准（2 条）**：prompts v17 实际差异已定位——新增【字数硬上限（写完必须自查）】块、续写纪律改为「每轮不少于本节要求字数」。是否按 v17 重算两处 sha256，需确认这批提示词变更已定稿。
2. **`branch.py` 段模式**：锚点归一化把支线挂到父主干，段模式是否也应有此语义，需作者确认。
3. **KG 空项目的重生成**：`_narrative_timeline()` 无 KG 数据返回 None → 续写渲染 `ValueError` → 未抽取项目重生成会 500。是返回最小空时间线（可用）还是保持硬错（要求先抽取），属产品口径。

### 9.1 三条决断落地 + `max_names` 生产缺陷（2026-11-15 晚）

与 §9 不同，这一轮**动了生产代码**（3 个 `backend/app` 文件），仍遵守：不 commit / push / checkout、不重启后端、不动 182 个 staged 文件。

| 项 | 改动 | 验证（修前 → 修后） |
|---|---|---|
| ① 提示词定稿 | `test_regenerate_no_wasted_context.py` 的 `PROMPT_SHA256` 按当前 system+`\x00`+user 字节重算为 `b6ffd53d…`；`test_qa_attack…::test_e1` 增加 `v17_deltas` 三段精确回退（字数硬上限块、续写纪律「每轮不少于本节要求字数」、伏笔「本节最多推进一条」措辞）；两处 docstring 写明变更史 | 2 红 → **0 红**（regenerate 哈希 1 条 + qa_attack 全文件 24 条，合计 26 passed） |
| ② `branch.py` 只对章模式归一化 | [branch.py](backend/app/generation/branch.py) 锚点归一化加 `chapter_based` 条件：章模式把支线挂主干层，段模式保持**嵌套挂载**（保留「支线从哪一段长出来」的溯源） | `legacy[backend·tests·test_v25_branch.py]`：1 红 → **绿**（脚本 57/57；组 4 改成段/章两种模式分组断言并注明差异） |
| ③ KG 空项目重生成 | [engine.py](backend/app/generation/engine.py) `_narrative_timeline()` 零事件返回**最小空时间线**（`current_chapter=1` + 空列表），不再返回 None；[narrative.py](backend/app/style/narrative.py) `_render_kg_timeline()` 在零事件时写明「尚无已抽取剧情」 | 新增 `test_regenerate_works_with_zero_kg_events`（保留既有夹具事件）；regenerate 文件 **7 passed**，不再 500 |
| ④ `max_names` TypeError | [narrative.py](backend/app/style/narrative.py) `_audit_character_names(..., max_names=None)`：默认全量（2026-11-12 契约），显式传正整数才截断并置 `skipped=True`；`checked` 改为记送审数（失败批次不再假性归零），失败由 `failed_batches` 回报 | `legacy[backend·tests·test_name_audit.py]`：1 红（TypeError + D 断言）→ **绿**（脚本 12/12；新增 L「默认全量覆盖」覆盖） |

**本轮关联回归**：受影响 11 个测试文件合并跑 **136 passed**（style/narrative/prompts/regenerate 面），无新增红。

**剩余既有失败（按约束不动）**：`legacy[...test_stat_evidence.py]`、`legacy[...test_evidence_quotes.py]`、`legacy[...test_extract_split.py]` 3 条；原 4 条既有失败中 `test_name_audit` 已按决断修复。
