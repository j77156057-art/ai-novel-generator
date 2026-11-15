# 2026-11-15 后端细节比对（只读，未提交）

> 范围：上一份报告 A 桶（67 个两代实现）与 B 桶（35 个必须并存）中属于后端的 **87 个文件**：`backend/app` 全部、`backend/` 下脚本、`tests/`，以及根级配置（`backend/requirements.txt`、`backend/config/providers.yaml`、`pytest.ini`）。
> 前端与独有功能盘点由另一路负责，本报告不重复；`package.json` 只在 §4 记一句说明。
> 产出方式：临时 worktree 检出 `origin/main`（`cb05a45`），用 `git show origin/feat/resume-push-2026-10:<path>` 取另一侧；未提交、未推送、未动 `main`。比对完成后 worktree 已清理。

---

## 0 基线与判定纪律

### 0.1 一个必须先说清的事实：两条线不是「同一功能的两个版本」

从模块清单看，两边**目录名相同、模块集大面积不相交**：

- `generation/`：main 有 `profiles / candidate / verbatim / dedup / visibility / ctx_profile / scene_contract / planning / character_card* / power_system* / cultivation / persona / tiers / description_elements / digest_context`；feat 有 `repetition / scope_guard / structure_check / progress_ledger / power_prompt / formulaic / patch / author_research`。**两边各自 import 的模块，另一边多数不存在。**
- `style/`：main 26 个文件（`spec / form / beat / attribution / scope / character_template / chapter_stats / digest / entities / …`），feat 8 个（`consistency / exemplar / jobs / narrative / pipeline / stylometry / warning_store / __init__`）。
- `retrieval/`：main 5 个（`bm25 / search / strategies / views`），feat 20 个（`brief/* / catalog_* / skeleton / l4_lexicon / adoption / anchor / budget / build_cards / tools / trace`）。

所以 87 个文件里，真正「同名函数逐个比细节」的只占一部分；另一部分是**同名文件里的不同产品线**。判定不能落在「谁更完善」，只能落在四个判据：

1. **行为正确性**：是否有明显 bug / 把已有缺陷钉死 / 伪造数据；
2. **对现有四十多章数据的兼容性**：现有库是 feat 形状（详见 §2.5），读写能不能对得上；
3. **可观测性**：失败、截断、降级是否有留痕；
4. **是否被现有测试锁住**：取另一边会不会让现有测试直接红。

### 0.2 五个判定档

| 判定 | 含义 |
|---|---|
| 取 feat | 以 feat 整份为准；main 侧对应内容不保留（丢失点单独列出） |
| 取 main | 以 main 整份为准；feat 的删改属于退步 |
| 手工合并 | 两边各有必须保留的部分；下面写明**骨架取哪边**、**从另一边搬哪几个具体函数/分支** |
| 两者等价 | 结构或断言等价，差异是注释/措辞/测试夹具，不改变行为 |
| 无法判定 | 缺另一侧信息（本报告只有 1 个：`routers/wizard.py`，取决于前端是否仍调大纲端点） |

「取 feat」不等于「feat 更好」，多数是因为数据形状与调用链已经绑死在 feat 上（引擎签名 ↔ prompts 参数 ↔ 落库列 ↔ 测试）。这一点在 §5 的「取 main 更好」清单里会反过来看到。

---

## 1 判定总表（87 个）

### 1.1 agents / corpus（8）

| # | 文件 | 桶/风险 | 判定 | 一句话依据 |
|---|---|---|---|---|
| 1 | `agents/audit.py` | A/中 | 手工合并 | feat 多 3 个 bible 人名核对函数；main 的 `rule_corpus_foreign_blocks`/`run_narrative_audit` 是语料杂质审计，feat 整条删除 |
| 2 | `agents/diagnosis.py` | A/中 | 手工合并 | feat 29 条规则；main 多 5 条，其中 2 条是「相对参考书分位」的风格规则 |
| 3 | `agents/log_audit.py` | A/低 | 手工合并 | feat 删 `_react_failure_reason` 是因为改读 `retrieval_logs`，但 run 列表从此没有 ReAct 收尾原因 |
| 4 | `agents/pending_actions.py` | A/中 | **取 main** | feat 三个 action-card 构造器是彻底删除（全仓无同名替代），assistant 无法再挂「文本修改」待办 |
| 5 | `agents/prompt_overrides.py` | B/中 | 取 feat | 覆盖解析入口换了 API（`PromptKey`/`resolve_prompt_block`），feat 的 `test_prompt_override_wiring` 18 条全新用例锁住 |
| 6 | `agents/proposals.py` | A/中 | 手工合并 | main 的 `expected_hash` 漂移守卫与 `merge_pending_acceptance` 是正确性修复，对应测试被 feat 删掉 |
| 7 | `agents/registry.py` | A/高 | 手工合并 | feat 有 QA 工具循环与 trace；main 的分页读段（`start_char/max_chars`）与 4 个审计工具被删 |
| 8 | `corpus_health.py` | A/中 | **取 main** | feat 删掉 `find_foreign_blocks`/`strip_foreign_blocks`，跨书正文混入不再能被检出/剥离 |

### 1.2 核心运行时（26）

| # | 文件 | 桶/风险 | 判定 | 一句话依据 |
|---|---|---|---|---|
| 9 | `db.py` | B/高 | 手工合并 | feat 多 3 个「真实库禁止 DROP」保护；补列清单两边各一半，需取并集 |
| 10 | `generation/branch.py` | A/中 | 取 feat | 加 `chapter_based` 分支推进，与 feat 的章调度一致 |
| 11 | `generation/engine.py` | A/高 | 取 feat | 重写轮/补写轮/截断重生成/质量落库都在 feat；main 的能力依赖 6 个 main-only 模块 |
| 12 | `generation/power_scale.py` | A/中 | 取 feat | 多 `scale_seq` / `detailed_scale` / `_load_15` 与 15 级数据文件 |
| 13 | `generation/power_system_type.py` | A/低 | 取 feat | 多 `system_type_from_bible` |
| 14 | `generation/prompts.py` | A/高 | 取 feat | 参数是与引擎签名一一对应的，不能单边取 |
| 15 | `generation/timeline.py` | A/中 | 手工合并 | feat 有章级调度；main 的节点判定带逐字证据与 audit，feat 退化为 bool |
| 16 | `ingestion/chunk.py` | A/中 | 取 feat | main 的 217 行（含 `chunk_quality`、句边界均衡切分）在 feat 侧无任何调用方 |
| 17 | `ingestion/clean.py` | A/中 | 手工合并 | feat 多重复段/跨书段剥离；main 的站点标记行清洗三项被整段删除 |
| 18 | `ingestion/encoding.py` | A/低 | 两者等价 | 174/174 行，函数与异常完全一致 |
| 19 | `ingestion/parse_txt.py` | A/高 | 取 feat | 109→454 行：卷标题、错误页、标题可信度、分卷切分 |
| 20 | `ingestion/seed.py` | A/中 | 取 feat | 多 `resolve_seed_dir` 并在缺失时抛 `FileNotFoundError` |
| 21 | `ingestion/store.py` | A/中 | 手工合并 | feat 删了 `drop_foreign_blocks` 与 `HTTPException/RuntimeError` 语义，与 #8 的判定冲突 |
| 22 | `kg/batch_worker.py` | A/高 | 取 feat | 1005→3331 行：DB 认领、版本归属、隔离区、重捞、心跳、零事件告警 |
| 23 | `kg/bible.py` | A/中 | 取 feat | 多性别/代词/首次出场章与 bible 键校验 |
| 24 | `kg/build.py` | A/高 | 取 feat | 多项目级 KG 锁、`merge_segment_into_kg`、`rebuild_generated_kg` |
| 25 | `kg/extractor.py` | A/中 | 取 feat | 被删的 4 个函数只服务 v2 路径，活跃路径是 v3 |
| 26 | `kg/extractor_v3.py` | A/高 | 取 feat | 定向重抽、事件归段、持有关系核对、错误随结果返回 |
| 27 | `kg/judge.py` | A/中 | 取 feat | 4 个检查全部加 `novel_id` 维度过滤 |
| 28 | `kg/ledger.py` | A/低 | 取 feat | 去掉 `commit` 参数由调用方提交；`rebuild_facts` 被 `build.rebuild_generated_kg` 取代 |
| 29 | `kg/queries.py` | A/低 | 取 feat | 删掉伪造的 `appearances=list(range(first,last+1))`，是缺陷修复 |
| 30 | `kg/retrieval_v3.py` | A/中 | 手工合并 | feat 有 novel 维度隔离；main 的 `max_foreshadows` 安全阀被删，提示词会线性膨胀 |
| 31 | `kg/sentinel.py` | A/高 | 取 feat | 多跨体系境界碰撞规则，`system_type` 贯穿，内置境界表改从项目读 |
| 32 | `llm/provider.py` | B/高 | 取 feat | 三层超时 + 速率判据 + `read1` + 工具不支持模型名单，main 是单层 120s |
| 33 | `llm/reasoning.py` | A/中 | 取 feat | 多 `dropped_chars`，能区分「推理被过滤多少」 |
| 34 | `main.py` | B/高 | 手工合并 | feat 多 `healthz`；router 集合 16 vs 13，需按最终存活模块裁剪 |

### 1.3 routers / schemas / models（13）

| # | 文件 | 桶/风险 | 判定 | 一句话依据 |
|---|---|---|---|---|
| 35 | `models.py` | B/高 | 手工合并 | 25 张同名表、12 张列不同；现有库是 feat 形状，迁移只能朝 feat 方向 |
| 36 | `retrieval/bm25.py` | A/低 | 两者等价 | 41/41 行完全一致 |
| 37 | `retrieval/search.py` | A/中 | 取 feat | main 的 `weighted_search` 只服务 main-only 的 `retrieval/strategies` |
| 38 | `routers/assistant.py` | A/高 | 取 feat | feat 多 13 个 `_qa_*`：工具循环、墙钟预算、确定性降级 |
| 39 | `routers/assistant_generation.py` | A/高 | 取 feat | 322→1174：AI 味反馈、澄清、快照、懒加载 provider |
| 40 | `routers/assistant_proposals.py` | A/中 | 手工合并 | main 的 3 条修复/回滚端点（含 `InvalidTransition` 处理）被删 |
| 41 | `routers/generation.py` | A/高 | 取 feat | 端点与 body 一致；被删的 5 个守卫都依赖 main-only 模块 |
| 42 | `routers/kg_v3.py` | A/中 | 手工合并 | feat 多 6 个观测/运维端点；`max_foreshadows` 参数丢失需与 #30 同口径 |
| 43 | `routers/novels.py` | A/中 | 手工合并 | feat 直接返回 ORM `list[RawNovel]`，main 用 `NovelOut` 收口响应字段 |
| 44 | `routers/projects.py` | B/高 | 手工合并 | 2199→906 行；共享端点基本一致，main 独有 63 个功能端点 |
| 45 | `routers/settings.py` | A/中 | 取 feat | 同名 `test_llm_config` 是两套实现，feat 的走新 provider 超时契约 |
| 46 | `routers/wizard.py` | A/高 | **无法判定** | main 有 4 个大纲端点 + `planning.py`，feat 无；是否算退步取决于前端是否仍调 |
| 47 | `schemas.py` | B/高 | 手工合并 | 12 个类名/顺序完全相同，字段差 6 组，按保留功能取舍 |

### 1.4 style（6）

| # | 文件 | 桶/风险 | 判定 | 一句话依据 |
|---|---|---|---|---|
| 48 | `style/exemplar.py` | A/中 | 手工合并 | main 多 `exclude_chunk_ids`（同章不重复取样例）与单选角色过滤 |
| 49 | `style/jobs.py` | A/中 | 手工合并 | feat 多心跳/预告/续跑；main 的 book_digest 作业与多 kind run 被删 |
| 50 | `style/narrative.py` | A/高 | 手工合并 | feat 是现网档案来源（续跑/审计/证据）；main 多成熟度权重与场景分类 |
| 51 | `style/pipeline.py` | A/中 | **取 main** | `chapter_lengths` 是章级分位可重算的唯一入口；feat 的章级阈值是硬编码表 |
| 52 | `style/stylometry.py` | A/高 | 手工合并 | feat 有统一切句尺与对白口径；main 的 `band_check` 家族是「按参考书分布判带」唯一入口 |
| 53 | `tokenize.py` | A/中 | 取 feat | 23 行版本仍用 jieba，删掉的是自维护 `_chinese_tokens` |

### 1.5 根级脚本与 tests（34）

| # | 文件 | 桶/风险 | 判定 | 一句话依据 |
|---|---|---|---|---|
| 54 | `backend/load_corpus.py` | B/低 | 手工合并 | 参数 `drop_foreign_blocks` 与 #8/#21 绑定 |
| 55 | `backend/test_extractor_v2.py` | B/低 | 两者等价 | 仅 1 行注释措辞 |
| 56 | `backend/test_m3.py` | B/低 | 取 feat | 断言从伪造 `appearances` 改为真实 `first/last_chapter` |
| 57 | `backend/test_m3_api.py` | B/低 | 取 feat | 同上，并反向确认字段已移除 |
| 58 | `backend/test_m4_api.py` | B/中 | 取 feat | 断言随 `prior_blocks`/时间线分级投喂改写，旧断言已不成立 |
| 59 | `backend/test_p0_regression.py` | B/中 | 取 feat | FakeProvider 适配新 provider（`complete_message`/`read1`） |
| 60 | `backend/test_summary_tree.py` | B/低 | 取 feat | 同上，`chat/stream_chat` 收 `**kwargs` |
| 61 | `backend/verify_acceptance.py` | B/中 | 取 feat | 多 6 个度量函数（分位、Jaccard、n-gram、比喻密度） |
| 62 | `backend/verify_corpus_health.py` | B/中 | 两者等价 | 406/406 行，函数与退出码一致 |
| 63 | `backend/verify_sentinel.py` | B/中 | 取 feat | 生产境界表删除后，脚本改用本地夹具阶梯 |
| 64 | `backend/verify_style_pipeline.py` | B/低 | 两者等价 | 仅注释与污染样本名更换，断言等价 |
| 65 | `backend/verify_stylometry.py` | B/低 | 两者等价 | feat 删 42 行「换尺前后基线」说明，断言未变 |
| 66 | `tests/concurrent_assistant_isolation.py` | B/低 | 两者等价 | 仅多捕 `SystemExit`，断言相同 |
| 67 | `tests/concurrent_assistant_qa.py` | B/低 | 两者等价 | 同上 |
| 68 | `tests/concurrent_router_e2e.py` | B/低 | 两者等价 | 同上 |
| 69 | `tests/conftest.py` | B/高 | 手工合并 | feat 多 `_db_logical_fingerprint`；main 多 `experimental_consent` 夹具 |
| 70 | `tests/test_assistant_agent_tools.py` | A/中 | 取 feat | 多 16 条 QA 工具循环/白名单/预算用例 |
| 71 | `tests/test_assistant_audit.py` | A/中 | 手工合并 | feat 删掉的 1 条正是 main 语料审计用例 |
| 72 | `tests/test_assistant_corpus_clean.py` | A/低 | 取 feat | 43 条用例全同，仅 provider 假件签名 |
| 73 | `tests/test_assistant_diagnose.py` | A/低 | 手工合并 | 用例名从「四条规则」改「四条」，main 是五条 |
| 74 | `tests/test_assistant_proposals.py` | A/中 | 手工合并 | main 有漂移守卫用例，feat 多路由遍历工具 |
| 75 | `tests/test_assistant_provider_config.py` | B/低 | 取 feat | 假件从 `fake_chat` 改 `fake_complete_message` |
| 76 | `tests/test_assistant_readonly.py` | A/低 | 两者等价 | 仅删 29 行说明性 docstring |
| 77 | `tests/test_db_isolation.py` | B/中 | 手工合并 | feat 用指纹用例替掉 main 的子进程隔离用例，两者防的不是同一件事 |
| 78 | `tests/test_distill_jobs.py` | B/中 | 取 feat | 多 2 条：角色隔离、按 tier 复用 active run |
| 79 | `tests/test_finish_reason_truncation.py` | B/低 | 取 feat | `_Resp.read(n=-1)` 适配 `read1` |
| 80 | `tests/test_ingestion_encoding.py` | A/低 | 两者等价 | 120/120 行完全一致 |
| 81 | `tests/test_prompt_override_wiring.py` | B/中 | 取 feat | 18 条用例整组重写，锁的是 feat 的 resolver 与引擎接线 |
| 82 | `tests/test_provider_timeouts.py` | B/低 | 取 feat | 同上，`read(n=-1)` |
| 83 | `tests/test_reasoning_wiring.py` | B/低 | 取 feat | 同上 |
| 84 | `tests/test_schema_migration_completeness.py` | B/中 | 手工合并 | 期望清单需取「保留列」的并集 |
| 85 | `tests/test_settings_roles.py` | A/中 | 手工合并 | main 不硬编码超时、对 yaml 断言；feat 硬编码 180，另有模型清单一致性检查 |
| 86 | `tests/test_style_tier.py` | A/中 | 取 feat | 删 `async_chat`、`chars_per_chapter`，随 feat 采样签名 |
| 87 | `tests/test_watermark_detection.py` | B/低 | 两者等价 | 124/124 行完全一致 |

---

## 2 六个决定整条链路的区域（逐条比细节）

### 2.1 `generation/engine.py` + `generation/prompts.py`

**规模与结构**

| 指标 | main | feat |
|---|---|---|
| engine.py 行数 | 3924 | 6054 |
| engine 顶层 def | 81 | 134 |
| prompts.py 行数 | 927 | 1321 |
| prompts 顶层 def | 16 | 31 |

**engine：feat 的重写轮调度（具体参数）**

- `settings.rewrite_max_rounds = 2`（`_rewrite_max_rounds()` 读取，缺省 2）。
- `settings.rewrite_stop_without_gain = True`：某轮没有改善就停，不再跑满 2 轮。
- 双阈值：`rewrite_growth_allowance = 0.08`（允许增字 8%）、`rewrite_growth_reject_ratio = 0.16`（超过 16% 判「跑飞」回退首轮稿）。
- `_rewrite_improved()` 用「未过项数量是否下降」判定改善，不看字数。
- `_rewrite_rounds` 把每轮的 `{round, items, before, after, improved, failed}` 写进 `quality["rewrite_rounds"]`；任一轮抛异常 → `warnings.append("重写轮失败，终稿降级为首轮稿…")`，不再静默 break。
- `_gather_rewrite_evidence()` / `_rewrite_with_evidence()`：重写提示词带逐条证据与 `item_holding` 约束。

**engine：feat 的补写轮与截断处理**

- `_draft_prompt_min_chars()` 先看草稿是否短于 `min_chars`；短则 `_complete_short_draft()` 用**独立续写调用**补，`settings.draft_completion_max_rounds = 2`。
- `_regenerate_if_truncated(provider, text, finish_reason, ...)`：`settings.truncation_regen_attempts = 1`（默认重生成 1 次），配 `rewrite_growth_allowance` 防止「越重生成越长」。
- `_stream_with_retry` / `chat_with_retry` / `_is_transient_api_error`：瞬时错误（连接重置、5xx）在引擎层再重试，不只是 provider 层。
- 落库留痕：`_quality_check()` → `quality_json()` → `_commit_segment(..., quality=...)` 写 `generations.quality_json`（feat 独有列）。

**engine：main 独有的能力（feat 完全删掉）**

- 候选池择优：`_candidate_pool_size` / `_produce_with_pool` / `_run_pool_selection` / `_pick_gate` / `_pick` / `_score`。
- 文风闸门（整段报告）：`_style_gate`，用 `stylometry.band_check` 的 p10/p90 判 in/out；改写成本闸门 `_rewrite_gate`，只看 `REWRITE_GATE_FEATURES = ("句长均值","句号密度")` 且要求「严重偏离」，短于 `REWRITE_GATE_MIN_CHARS = 500` 不判。
- 逐字闸门：`_maybe_fix_verbatim` / `_verbatim_system_prompt` / `VerbatimViolation`；`judge_final_text`。
- 场景合同：`_audit_and_repair_scene` / `_scene_contract_for_generation`；归属校正 `_maybe_correct_attribution`；形态/省略号机械修正；提交前去重 `_dedup_before_commit`；角色语料卡刷新 `_refresh_character_personas` / `backfill_character_evidence`。
- 这些依赖 `generation/candidate|verbatim|scene_contract|dedup|profiles|visibility` 与 `style/attribution_fix`——**feat 树里都不存在**。

**两边相同的常量（说明阈值口径没变）**

`_REPETITION_WARN_RATIO = 0.15`、`MIN_GAP_TO_ELLIPSIS = 0.05`、`REWRITE_GATE_MIN_CHARS = 500`、`STYLE_WARN_PERCENTILE = 99.5`、`STYLE_WARN_ABS_Z = 3.0`、`AUTO_AUDIT_INTERVAL = 10`。

**签名差别（调用链的硬约束）**

| 函数 | main | feat |
|---|---|---|
| `generate_next_segment` | `(..., profile=None, style_rewrite=None)` | `(..., chapter_range=None, fresh_start=False)` |
| `generate_next_segment_stream` | 同上 | `(..., chapter_range=None)` |
| `_commit_segment` | `(..., pipeline=None)` | `(..., meta=None, quality=None, chapter_range=None)` |
| `_judge_and_advance` | `(..., written_segments)` | `(..., written_segments, *, progress_before=None, chapter_based=False)` |
| `_build_generation_prompts` | 7 参 | 14 参（`reminders/instruction/rewrite_before_chars/chapter/chapter_context/appeared_names/chapter_outline`） |
| `_rewrite_prompt` | 3 参 | 9 参（`evidence/item_holding_block/constraints/gate_info/research_notes/max_chars`） |
| `regenerate_segment_stream` | 6 参 | 10 参（`instruction/snapshot_id/expected_before/selected_scope`） |

**prompts：两侧的块集合**

- feat 独有块：`_catalog_skeleton_block`、`_chapter_quota_block`、`_gate_block`、`_sentence_reference_block`、`_dialogue_reference_block`、`_length_block`、`_opening_premise`/`_is_opening_spoiler`、`render_item_holding_section`、`chapter_outline_entry`、`_override_section`。
- main 独有块：`_character_template_block`、`_character_constraint_line`、`_description_guides`、`_voice_character_for`、`_character_profile_block`、`_scale_contrast_block`、`_psych_scene_boundary`、`_override_block`。
- `_narrative_block` 从「`include_voice/dims/voice_character/entity_table`」换成「`allowed_novel_ids/opening/mode/timeline`」——不是加参数，是换了叙事投影模型。
- `build_system_prompt` feat 净增 13 参、去掉 4 参（`entity_table/override_style/override_constraint/book_digest`）；`build_user_prompt` 净增 8 参、去掉 4 参（`prev_scene/scene_contract/override_directive/rag_blocks`）。

**判定：engine 取 feat、prompts 取 feat**

依据不是「新」，而是：

1. `generations.chapter_range`、`generations.quality_json`、`generation_snapshots` 表只在 feat；反向取 main 会让 40 多章的 `quality_json` 直接断读。
2. feat 的 `_quality_check` 是**唯一**把重复度/篇幅/梗概式推进落库的路径；main 的质量信息散在 `_judge_final_text` 与响应体里，不落库、事后无法定位。
3. main 的 6 组能力依赖 6 个 feat 不存在的模块，搬进 feat 等于把引擎重写一遍，且其阈值口径（`band_check`）与 feat 的 `consistency` 分位表是两把尺。

**丢失清单（必须由产品拍板，不是技术判断）**：候选池择优、逐字对照闸门（照搬参考书段落拦截）、场景合同审计/修复、归属校正、机械形态修正、提交前去重、角色语料卡刷新、main 的整段文风闸门报告。若这些必须保留，正确做法不是「合并引擎」，而是**两套生成入口并存**（feat 续写入口 + main 创作入口），工作量与风险都比混改低。

### 2.2 `llm/provider.py`

**三层超时的契约**

| 层 | main | feat |
|---|---|---|
| 签名默认值 | `timeout: int = 120`、`stream_timeout: int = 180` | `timeout=None`、`stream_timeout=None`、`total_timeout=None` |
| 实际取值 | 硬编码 120/180 | `settings.writer_timeout=180`、`writer_stream_timeout=180`、`writer_total_timeout=960` |
| 首字节前 | 受 `timeout` 限制 | **只受 `total_timeout` 硬顶**（生成期可长达数分钟，180s 空闲超时会误杀） |
| 首字节后 | 每次 recv 空闲超时 | `_RateGuard` 速率窗口（低于 `_RATE_FLOOR_BPS` 立即判挂起） |
| 整链上限 | 无（模型链 × `_RETRIES_PER_MODEL=2` 相乘） | `deadline = now + total_timeout`，整条链共享 |

feat 的注释里带着两条实测：第 18 章一次重写 4 个尝试各卡 180s、总耗时 896.8s 仍零产出；第 16 章一次重写静默挂 19 分钟。这不是「更新」，是 main 的两层超时**结构上兜不住**。

**流式与非流式的差别（feat）**

- 非流式：`_read_body()` 用 `read1(65536)` 分块读，每块 `guard.feed()`；`complete_message()` 是薄封装，`_note_usage()` 记 token。
- 流式：`_iter_stream_lines()` 每读一个 **chunk**（不是按行）`feed()`——按行会漏掉「只滴字节、很久不出换行」的假活。第 19 章日志 `[stream] 113.0s chars=8` 就是这种形态。
- 留痕：`_stream_ms` 统一毫秒口径，记 `request_ms / first_frame_ms / first_reasoning_ms / first_content_ms`，以及 `reasoning_chars_before_content`；`_reasoning_source()` 区分字段与内联两种来源（都没有回 `None` 而不是 0）。
- 工具调用：`tools_unsupported_models()` + `ToolsUnsupportedError`，避免 `finish_reason=tool_calls` 但 `tool_calls=[]` 的假信号。

**重试与限速**

- 两边相同：`_RETRIES_PER_MODEL = 2`、`_RETRYABLE_HTTP = {429,500,502,503,529}`、`_SWITCH_HTTP = {429,403}`、`_FALLBACK_SUMMARY_CHARS = 80`。
- feat 新增 `_is_retryable` 的显式分类、`_backoff`、`_is_account_daily_limit`（账号日限额与普通 429 分开）、`_RateGuard.check/open/feed`。
- main 独有：`variant()`（浅拷贝只换思考开关与输出预算）、`_thinking_kwargs()`（角色 thinking → `reasoning_effort`/`thinking_off`）、`block_chars_for_channel()`（按渠道查蒸馏块能力，供指纹用）、`chat(..., temperature=, max_tokens=)` 逐调用参数、MiniMax `reasoning_effort` 构造参数。

**判定：取 feat**

依据：可观测性从「6 条 log」变 13 条，且多了 4 个时间锚点；总时长硬顶是唯一能覆盖「换模型 × 重试 × 慢速滴包」的机制；`read1` 修的是真实挂起形态。丢失点：角色思考档位与逐调用 `temperature/max_tokens`。若设置页仍暴露思考档位，则 `variant()`/`_thinking_kwargs()` 需手工搬回（两函数自包含，约 60 行），但不要搬 main 的 120s 默认值。

### 2.3 `kg/` 系列

**`batch_worker.py`（1005 → 3331，def 35 → 92）**

- main：内存 `_BatchQueue`（`BATCH_SIZE=5`、`MAX_QUEUE=200`、`WORKER_THREADS=1`、`RETRY_LIMIT=2`、`STUCK_MINUTES=30`）+ 一行 `AgentRun(status=running/failed)`。批级失败走 `_mark_batch_failed`；重启遗留靠 `reap_stale_runs`。
- feat：队列落库并**按行原子认领**（`_claim_row` 条件 UPDATE、`_claim_orphaned`、`_claim_expired`、`_claim_execute`、`_owner`/`_owner_same_host`/`_pid_alive`）；版本归属 `_max_version` + `_supersede_previous_versions`（同章旧事件/状态/关系/提及全部 `is_current=0`，不物理删）；失败留痕 `_quarantine`（转存 `kg_ingest_quarantine`）、`requeue_failed`（按 run_id 重捞，正文从 `Generation` 回读）；运维面 `_sweep_stuck`/`_heartbeat`/`_ensure_heartbeat`/`worker_status`/`backfill_project`/`rebuild_chapter`；质量面 `_annotate_no_generation`/`text_gate_no_generation_summary`/`_scan_no_generation_history`；输出预算 `_OUTPUT_BUDGET_NOTE`（模型被 4096 token 截断时压条数，不压抽取口径）；`_discover_pending` 会扫历史段补抽。
- 差异：`_BatchQueue.put` main 返回 `None`，feat 返回 `list[_QueueItem]`（溢出丢弃要有回执）；章号一律从 `chapter_range` 标签派生（`_chapter_of`/`_chapter_of_label`），解析不出就不参与，**不拿段号顶章号**（feat 注释解释了旧段 1 撞新第 1 章的后果）。
- 判定：**取 feat**。版本归属靠 `kg_*` 三列（`novel_id/is_current/version`），main 根本没有这三列；取 main 等于让检索/切片/时间线继续读「全部历史版本混在一起」的数据。

**`extractor_v3.py`（513 → 905，def 10 → 18）**

- feat 新增：`extract_targeted`（定向重抽）、`attribute_event_segments`（事件归段）、`reconcile_owns`（「持有」关系核对）、`_mark_error`、`validate_kinship_terms`、`_has_possession_sentence`、`_valid_label`、`_term_in_text`、`_best_segment`。
- 签名：`extract_segment(..., known_foreshadows=)` → `(..., note=)`；`extract_batch(..., known_foreshadows=)` → `(..., note=)`；`normalize_extraction(raw)` → `(raw, *, error=None)`——解析错误随规范化结果一起返回，调用方才能把行标 failed 并写原因。
- main 独有：`known_foreshadow_block()`（把已埋伏笔名塞进提示词）。
- 判定：**取 feat**。`note` 通道被 `_OUTPUT_BUDGET_NOTE` 占用。丢失点：不再提示已知伏笔名，模型可能重复埋同名伏笔；但落库按 `name` upsert 会合并，**不会产生重复行**，只损失 token 与抽取质量。

**`build.py`（388 → 683）**

- feat 加 `_project_kg_lock`/`_kg_lock_for`/`_merge_segment_into_kg_locked`（同项目串行）、`merge_segment_into_kg`（对外入口）、`rebuild_generated_kg`、`_hole_detection_key`。
- main 有 `rebuild_generated_graph`、`_segment_pairs`；`merge_result_into_graph(..., *, commit=True)` → feat `(..., text="")`：函数不再自己 commit，由锁包装层统一提交。
- 判定：**取 feat**。改的是事务边界，取 main 会在并发批抽下丢锁。

**`sentinel.py`（1089 → 1204）**

- feat 加 `_rule_realm_jump`、`_realm_collision_findings`（跨体系境界不可比）；`build_extractors(names, realm_order=REALM_SYSTEMS)` → `(names, realm_order=None)`；`check_segment/audit_segment/extract_events` 增 `system_type`。
- 生产侧内置 `REALM_SYSTEMS` 表在 feat 删除，改从项目 `power_system_type` 读；`verify_sentinel.py` 相应改为本地夹具阶梯。
- 判定：**取 feat**。`realm_order=None` 看似更危险，但 feat 在 `system_type_from_bible` 缺失时**不判定**而不是拿错表判，行为更保守。

**`queries.py`（148 → 154）**

- 唯一实质差异：`get_character()` 删掉 `appearances=list(range(first_chapter, last_chapter+1))`。原字段把「首末章」编造成「连续出场列表」——角色只在第 3、50 章出场也会返回 48 章。
- feat 同时改了 `test_m3.py`/`test_m3_api.py` 的断言（钉真实 `first/last_chapter`，并反向确认字段已移除）。
- 判定：**取 feat**。这是缺陷修复，且 main 的旧断言把缺陷钉死了。

**`retrieval_v3.py`（539 → 464）**

- feat 加 `_in_scope`/`_label_in_scope`（`novel_id` 维度隔离）、`max_relations`；删 `max_foreshadows`（默认 60，超了留最早埋下的）、`_payoff_of`、`_squash/_order/_trim_leading_clause/_compact_context`（提示词压缩）。
- 判定：**手工合并**。骨架 feat（novel 隔离必须保留）；从 main 搬 `max_foreshadows` 参数 + `_payoff_of` + 裁剪分支（三处代码，`slice_for_prompt` 内），否则未回收伏笔会随章数线性进提示词——main 文档 §6 记录过这个膨胀。

**`judge.py` / `ledger.py` / `bible.py` / `extractor.py`**

- `judge.py`：4 个检查全加 `novel_id` 过滤 → 取 feat（多书隔离）。
- `ledger.py`：去 `commit=True` 三处、`rebuild_facts` 被 `rebuild_generated_kg` 取代 → 取 feat。
- `bible.py`：加 `character_gender`/`character_pronoun`/`character_first_chapter`/`validate_bible_keys`/`_world_container_ok` → 取 feat。
- `extractor.py`：main 的 `trim_cut_artifact`/`looks_like_cut_artifact`/`name_shape_candidates`/`_relaxed_surname_lead` 是 v2 姓名/残句修正；活跃路径是 v3（`batch_worker` 只 import `extractor_v3`）→ 取 feat。

### 2.4 `style/`

**`narrative.py`（5850 → 7788，def 101 → 190）**

- feat 新增 125 个函数，可归五组：
  1. 规则证据与审计：`_score_rule`、`_rule_evidence`、`select_rules_with_audit`、`_verify_rule`、`_render_audit_block`、`audit_voice_items`、`_parse_audit_metrics`、`_format_audit_median`；
  2. 断点续跑：`_load_done_chunks`、`_save_done_chunk`、`count_done_chunks`、`distill_resume_scope`、`_clear_stale_scope_chunks`、`reusable_narrative`；
  3. 节拍骨架：`build_beat_skeletons`、`validate_beat_skeleton`、`skeletonize_beat_rule`、`_beat_nodes`、`build_beat_terms`；
  4. 声音映射：`_voice_*` 20 余个（`_voice_imitation_score`、`_voice_address_score`、`audit_voice_items`、`_promote_mapped_voices`、`clean_voice_items`…）；
  5. 权重与降级：`load_rule_weights`、`attach_rule_weights`、`bump_rule_weight`、`ensure_rule_scores`、`_degraded_narrative`、`_classify_chunk_failure`、`_describe_provider`。
- main 独有：成熟度权重（`_attach_maturity_weights`/`maturity_weight`/`_mature_chapters`）、场景分类（`classify_scene`/`dominant_scene`/`scene_corpus_stats`）、战斗/心理/词序三套提示（`_build_combat_prompt`/`_build_psych_prompt`/`_build_word_order_prompt`）、角色模板（`extract_character_template`/`_extract_character_profiles`）、成本与切块（`estimate_distill_cost`/`_distill_cutoffs`/`_block_chars_for_fingerprint`/`_configured_block_chars`）。
- 两边相同的常量：`NARRATIVE_VERSION = 10`、`WEIGHTING_VERSION = 1`、`SAMPLE_RATIO = 0.30`、`MIN_SAMPLE_CHARS = 300_000`、`MIN_CHAPTERS_PER_CHUNK = 5`、`TARGET_BLOCK_CHARS = 20_000`、`RULES_PER_DIM = 5`、`MAX_VOICE_CHARACTERS = 30`、`VOICE_MAIN_CHARACTERS = 8`、`VOICE_BATCH_SIZE = 7`、`_DEFAULT_MAX_CONCURRENT = 5`、`_DISTILL_TOTAL_PHASES = 6`。
- 签名要点：feat 的 `_tier_signature(tier)` 不带 scope，main 的带 scope；main 的 `_persist_style_distill(..., tier_signature=)` 多一个参数。**档案签名口径不同 → 混用会让 `style_distills` 全部 miss。**
- 判定：**手工合并**。骨架 feat（现网 40 多章的档案由 feat 的续跑/审计产出，`content_hash`/`tier_signature` 与 feat 的 `jobs` 对齐）；从 main 搬成熟度权重与场景分类（`maturity_weight`/`_attach_maturity_weights`/`classify_scene`/`dominant_scene` 共 6 个函数），但必须同时给 `_persist_style_distill` 加回 `tier_signature` 并迁移旧档案，否则新档案与旧档案互相不认。

**`stylometry.py`（1603 → 1136，def 39 → 33）**

- feat 新增 12 个：`split_sentences`、`sentence_lens`（**唯一尺**：蒸馏与生成共用，按 `。！？…` 断开、保留句末标点、不按段切）、`dialogue_sentences`、`dialogue_spans`、`dialogue_ranges`、`dialogue_profile`、`_pure_dialogue_paragraph`、`paragraph_sentence_counts`、`strip_edge_quotes`、`_share`、`_med`、`_round`。
- main 独有 18 个：`band_check`、`band`、`profile_band`、`measure_summary`、`cjk_len`、`pooled_dialogue_form`、`raw_counts`、`_strip_quoted`、`plain_dialogue_ratio`、`dialogue_segment_form`、`_mean_run_length`、`paragraph_spans`、`_is_speech_tag_only`、`_punctuation_and_paragraph_features`、`dialogue_cjk_count`、`_strip_speech_tag`、`_first_quote_pos`、`mean_only`。
- 相同常量：`PROFILE_VERSION = 2`、`MEASUREMENT_VERSION = 4`、`_MIN_HOLDOUT_FOR_PERCENTILE = 20`、`BUCKET_THRESHOLDS = (0.6, 1.7)`、`SENT_END = "。！？!?…"`、`_LEAD_DIALOGUE_HEAD = 12`、`_BARE_SEG_TAIL_MAX = 6`。分位样本不足 20 个留出单元时两边都返回 `None` 而不是 100%。
- 判定：**手工合并**。骨架 feat（`split_sentences`/`sentence_lens` 是 v2 口径唯一入口，换尺会让历史档案与新的 `style_check` 不可比）；从 main 搬 `band_check` + `profile_band` + `band`（纯计算，约 150 行），作为「按**本项目参考书**分布判带」的入口，供 #2 的两条风格规则与 main 的 `_rewrite_gate` 使用。**不搬** `measure_summary`/`cjk_len`（feat 的 `consistency`/engine 已有替代）。
- 注意：feat 的章级阈值来自 `consistency.ARCHIVE_BLOCK_PERCENTILES`/`ARCHIVE_CHAPTER_PERCENTILES` **硬编码表**；main 的 `band_check` 是「每次按本项目档案算分位」。硬编码表无法随新参考书重算——这是 §5 里 `pipeline.py` 判 main 的同一个理由。

**`pipeline.py`（298 → 237）**

- main 的 `build_reference_profile(..., chapter_lengths: list[int] | None)` 会调用 main-only 的 `style/chapter_stats.py`（`chapter_lengths()`/`load_chapter_lengths()`），产出档案里的 `chapter_stats`（每章汉字分布）——**章级闸门阈值的唯一可重算来源**。
- feat 无 `chapter_stats.py`，章级阈值是硬编码。
- 判定：**取 main**，并一并迁移 `style/chapter_stats.py`。依据是方法论正确性：换一本参考书，feat 的章级阈值不会变，main 的会重算。

**`exemplar.py`（371 → 172）**

- main：`visibility`（`VisibilityPolicy` 过滤可见书）、`exclude_chunk_ids`（同章已用过的块不再取）、`_mature_pool`、`filter_single_character`（避免样例全是同一个角色的对白）、`exemplars_from_character_hits`。
- feat：只剩 `novel_ids` + `exclude_texts`。
- 判定：**手工合并**。骨架 feat；搬 `exclude_chunk_ids` 过滤与 `filter_single_character`（纯函数、零依赖，防「同一段反复当范例」与「对白全是同一人」）。`visibility` 依赖 main-only 模块，不搬——feat 在调用侧用 `book_scope`/`scope_sql_values` 限定 `novel_ids`，功能等价。

**`jobs.py`（498 → 814）**

- feat 加：`_make_heartbeat_writer`/`_beat`/`_aware`（心跳）、`emit_notice`/`_terminal_payload`（状态通知）、`_estimate_expected_minutes`/`format_duration`（进度预告）、`resume_progress`/`distill_health`；`run_distill_job(..., content_hash=None, resume=True)`。
- main 有：`run_book_digest_job`/`find_active_book_run`/`_digest_entry_count`（book_digest 作业）、`create_distill_run(kind, scope, novel_id)`、`find_active_run(kind)`——**main 的 run 支持多 kind，feat 只有 narrative**。
- 判定：**手工合并**。骨架 feat；若保留 book_digest 功能，搬 `run_book_digest_job`/`find_active_book_run` 与 `distill_runs.kind/scope_json/novel_id` 三列（main 侧列）；不保留则取 feat 即可。

### 2.5 `models.py` / `db.py` / `schemas.py`

**25 张同名表，12 张列不同**

| 表 | main 独有列 | feat 独有列 |
|---|---|---|
| `agent_runs` | `trace_json` | — |
| `distill_runs` | `kind`, `scope_json`, `novel_id` | `progress_at`, `expected_minutes` |
| `generations` | — | `quality_json` |
| `kg_character_state` | — | `is_current`, `version`, `note` |
| `kg_entities` | — | `novel_id` |
| `kg_events` | — | `novel_id`, `is_current`, `version`, `note` |
| `kg_mentions` | — | `novel_id`, `is_current`, `version` |
| `kg_relations` | — | `status`, `is_current`, `version` |
| `novel_chunks` | `raw_text` | — |
| `projects` | `continuation_start_chapter`, `story_authority`, `reference_cutoffs`, `narrative_scope`, `character_template_id`, `style_rewrite` | `active_novel_id`, `continuation_novel_id`, `narrative_sample_chapters` |
| `raw_novels` | `source_text`, `source_chapters` | — |
| `style_distills` | `product_key` | — |

- main 独有 5 张表：`action_proposals`(ActionProposal)、`book_digests`(BookDigest)、`character_templates`(CharacterTemplate)、`roleplay_messages`(RoleplayMessage)、`workflow_runs`(WorkflowRun)。
- feat 独有 14 张表：`ai_flavor_memories`、`assistant_messages`、`assistant_wizard_sessions`、`distill_chunks`、`generation_snapshots`、`kg_corpus_windows`、`kg_ingest_quarantine`、`narrative_rule_weights`、`retrieval_logs`、`rule_quality_stats`、`style_baselines`、`style_checks`、`wizard_action_audits`、`wizard_notices`。

**迁移方向（只能朝 feat）**

现有 40 多章的库是 feat 形状。反向（取 main 的 models）需要 DROP 上面 feat 独有列并丢版本数据；正向（取 feat）只需给 main 侧功能补列：

- 若要保留 main 的续写/权威书/参考截断：`projects` 补 6 列；
- 若要保留 main 的导入原文快照：`raw_novels` 补 2 列、`novel_chunks` 补 `raw_text`；
- 若要保留 main 的 book_digest：补 `book_digests` 表 + `distill_runs` 3 列 + `style_distills.product_key`；
- 若要保留 main 的 ReAct 追踪：`agent_runs` 补 `trace_json`（注意 feat 已把工具轨迹写进 `retrieval_logs`，两套并存会重复）。

feat 列回填口径（如需把 feat 库给 main 侧代码读）：`kg_*.novel_id` 按 `book_scope` 回填，`is_current=1`、`version=1`；`generations.quality_json=NULL` 一律读成「未检测」，不能当「正常」。

**`db.py`（239 → 381）**

- feat 加 `_block_drop_table_on_real_db`/`is_protected_db`/`_sqlite_basename`：在非测试库上禁止 `drop_all`，这是安全修复。
- main 有 `_backfill_product_keys`（`style_distills.product_key`，main 独有列）。
- 补列清单：main 50 项、feat 79 项；main 独有 14 项、feat 独有 45 项。
- 判定：**手工合并**。骨架 feat（保护逻辑必留）；补列清单取「功能保留列」的并集，不盲目取并集（会加进没有消费方的列）。`test_schema_migration_completeness` 同步更新。

**`schemas.py`（187 → 165）**

- 12 个类名与顺序完全相同，`StrictRequest`（`extra="forbid"`）两边一致。
- 字段差：main 有 `NovelOut.max_chapter_no`、`UploadResult.check_status`、`ProjectCreate/ProjectPatch.continuation_start_chapter/story_authority/reference_cutoffs`、`ProjectPatch.outline`；feat 有 `ProjectPatch.active_novel_id/continuation_novel_id`。
- 判定：**手工合并**。骨架 feat；仅保留仍在用的 main 字段（`outline` 取决于 #46 向导大纲是否保留，`check_status` 取决于 #16 上传质检是否保留）。删字段要同时删 router 里对它的赋值，否则 `StrictRequest` 会在出参校验时报错。

### 2.6 `routers/`

**`projects.py`（2199 → 906，def 63 → 30）**

同名端点逐个比：

| 端点 | main | feat |
|---|---|---|
| `create_project` | `_validate_visibility(db, body.model_dump())`；写入 `story_authority`、`reference_cutoffs` | 无可见性校验；不写这两列 |
| `patch_project` | `background_tasks: BackgroundTasks`，在后台重建参考图谱 | `response: Response`，写版本响应头；不触发后台重建 |
| `get_project` / `build_kg` / `reminders` / `export_txt` / `narrative_tiers` | 逐字相同 | 逐字相同 |

- main 独有 63 个端点集中在：角色卡/角色模板（`generate_character_card`、`list_character_templates`、`apply_character_template`）、人设（`complete_persona`、`save_persona`、`list_personas`）、境界体系（`generate_power_system`、`generate_tiers`、`save_scale`、`plan_cultivation_route`）、背景（`generate_background`、`get_background`）、工作流（`get_workflow`、`log_workflow`、`extend_plan`）。
- feat 独有 30 个集中在：多书作用域（`_normalize_novel_ids`、`_validate_book_fields`、`resume_distill`）、蒸馏详情（`_sd_summary`/`_sd_detail`）、bible 修订（`apply_bible_revision`、`get_bible_revisions`、`rename_bible_character`）。
- 判定：**手工合并**。共享端点取 feat（与 feat 的 `Project` 列和 `ProjectCreate/ProjectPatch` 对齐）；main 独有端点按前端是否仍用决定是否保留，保留则需同时带 main-only 模块与表——**这不是文件级取舍，是功能级取舍**。

**`generation.py`（543 → 385，def 21 → 16）**

- 端点集合与 body 模型两边一致（`GenerateBody`/`RegenerateBody`/`GenerateLoopBody`/`GeneratePlanBody`）。
- feat 删 5 个守卫：`_generate_guarded`（只把 `VerbatimViolation` 转 409）、`_verbatim_conflict`（409 + 人话）、`_require_workflow_ready`、`_require_story_seed`、`_resolve_profile`。
- 后果：feat 下 `generate`/`generate-stream` 不再要求工作流就绪或故事种子；引擎抛的 `ValueError` 会冒成 500，而不是 main 的结构化 400/409。
- 判定：**取 feat**。被删的 5 个守卫全部依赖 main-only 模块（`verbatim`、`workflow_runs`、`story_authority`、`profiles`），且 feat 引擎不再抛 `VerbatimViolation`。**但**：若想减少 500，可只搬 `_generate_guarded` 里「`ValueError` → 400」那一条分支（3 行），不搬 `verbatim` 依赖。

**`kg_v3.py`（373 → 484）**

- feat 加 6 个端点：`get_worldview`、`get_timeline`、`get_story_context`、`get_text_gate_no_generation`、`recover_worker`、`backfill_worker`；`get_events` 加 `novel_id`。
- `get_slice`/`get_slice_prompt`：main 有 `max_foreshadows` 参数，feat 删除（与 #30 一致）。
- 判定：**手工合并**。骨架 feat（6 个观测/运维端点是 feat 批抽链路的操作面）；若按 #30 恢复 `max_foreshadows`，两个端点同步加回，保持 API 与实现一致。

**`assistant.py` / `assistant_generation.py` / `assistant_proposals.py`**

- `assistant.py`（1046 → 1305）：feat 加 13 个 `_qa_*`（`_qa_answer_with_tools`、`_qa_parse_arguments`、`_qa_answer_deterministic`、`_qa_collect_deterministic_evidence`、`_qa_target_segment_no`、`_qa_asks_last_segment`…），main 有 `_excerpt`/`_evidence_rows_*`。判定 **取 feat**（工具循环有墙钟预算与确定性降级，`test_assistant_agent_tools` 16 条锁住）。
- `assistant_generation.py`（322 → 1174）：feat 加 AI 味反馈（`_run_ai_flavor_feedback`、`_summarize_flavor_hits`、`_pattern_examples`）、澄清（`_clarify_payload`、`_clarify_fingerprint`、`_run_clarification`）、快照与懒加载（`_lazy_llm`、`_segment_out`）。判定 **取 feat**。
- `assistant_proposals.py`（774 → 671）：main 多 `rebuild_project_derived`、`accept_current_body`、`create_local_body_repair`，并区分 `proposal_lib.InvalidTransition` → 409。判定 **手工合并**（feat 骨架，搬这 3 个端点与异常映射，与 #6 的 `expected_hash` 守卫配套）。

**`novels.py`（100 → 69）**

- main：`list_novels -> list[NovelOut]`，`NovelOut` 明确列出响应字段（含 `max_chapter_no`），并保留 `_upload_legacy`。
- feat：`list_novels -> list[RawNovel]`，直接把 ORM 交给 FastAPI 序列化。
- 风险：`RawNovel` 在 feat 上字段较少，但直接返回 ORM 会让「以后加的内部列」自动泄露到 API，且没有类型约束。
- 判定：**手工合并**。骨架 feat，用 `NovelOut` 收口响应；去掉 `max_chapter_no`（main 独有列，feat 无）。

**`settings.py`（964 → 778）**

- 同名 `test_llm_config`/`load_persisted_llm_config` 是两套实现：feat 走新 provider 的 `total_timeout` 与 `tools_unsupported_models`。
- main 多：`list_generation_tiers`、`set_generation_tier`、`thinking_control_for`（角色思考档位）。
- 判定：**取 feat**。丢失点：生成档位与思考档位两个设置端点；若前端仍暴露，需随 §2.2 的 `variant()/`_thinking_kwargs` 一起搬回。

**`wizard.py`（755 → 322，def 24 → 14）**

- main 多 10 个：`_parse_nodes`/`_parse`/`_parse_bible`/`_fill_missing_background`/`_fill_missing_personas`/`revise_budget`/`generate_outline`/`clarify_outline`/`apply_outline`/`_planning_failure`，并 import `generation/planning.py`（feat 无此模块）。
- feat 的 `_style_sample` 返回 `str`，main 返回 `tuple[str, dict]`——两边叙事 API 不同。
- 判定：**无法判定，需要前端侧确认**。后端自洽性上 feat 没问题；但若前端向导仍调 `generate_outline`/`apply_outline`，取 feat 会让向导走到 404。要保留则是**手工合并**：feat 骨架 + 搬 4 个大纲端点 + `generation/planning.py`，并把 `_style_sample` 的返回值适配成 feat 的 `str`。

---

## 3 逐文件明细

> 格式：**两边做法** / **差异** / **判定**；「手工合并」额外写**骨架与搬运**。所有结论只基于代码结构与签名，不基于提交时间。

### 3.1 agents

**1. `agents/audit.py`（main 1444 / feat 1332，def 35→33）**
- 做法：两边都是「规则函数 → DiagnosticIssue」；feat 侧规则签名统一去掉 `fitted_novel_ids`。
- 差异：feat 加 `_bible_name_appears`/`_name_in_bible`/`_bible_name_surface_forms`（bible 人名在正文出现的核对）；main 有 `rule_corpus_foreign_blocks`、`run_narrative_audit`、`_foreign_blocks`、`_fitted_novel_ids`、`_chunk_boundary`。feat 的异常集合为空（main 有 `ValueError`）。
- 判定：**手工合并**。骨架 feat；搬 `rule_corpus_foreign_blocks`（依赖 #8 的 `find_foreign_blocks`）；`run_narrative_audit` 若要保留需连同 main 的叙事审计语料一并迁，否则不搬。

**2. `agents/diagnosis.py`（1256 / 829，def 34→29）**
- 做法：规则清单式诊断，每条规则产 issue。
- 差异：feat 29 条；main 多 5 条：`rule_style_plain_dialogue_ratio`、`rule_style_sentence_length_deviation` 及其支撑 `_plain_window_ratio`、`_zero_window_ratio`、`_reference_window_distribution`。后两条走 `stylometry.band_check`。
- 判定：**手工合并**。骨架 feat；搬这 5 个函数 + 在规则注册表登记，并把它们标注为「需要本项目风格档案」——没有档案时返回空而不是报错。

**3. `agents/log_audit.py`（606 / 568，def 15→14）**
- 做法：把 `AgentRun` 行整理成审计清单。
- 差异：feat 删 `_react_failure_reason`，因为 feat 的 ReAct 轨迹写进 `retrieval_logs`（`registry._record_invoke_trace`），不再读 `agent_runs.trace_json`（main 独有列）。代价：run 列表没有 `stop_reason`/`degraded_reason`。
- 判定：**手工合并**。骨架 feat；把 `_react_failure_reason` 改读 feat 的 `retrieval_logs`（按 `session_id` 关联）或 `AgentRun.note`，**不要**搬 main 的 `trace_json` 版本——那会强迫加一列没人写的字段。

**4. `agents/pending_actions.py`（476 / 345，def 7→4）**
- 做法：把「待确认动作」渲染成前端卡片。
- 差异：feat 删 `collect_text_edit_cards`、`build_text_edit_proposal_action`、`build_action_proposal_action`；全仓 grep 无同名替代。文件零外部依赖（只有 `re`/`typing`），两边可直接替换。
- 判定：**取 main**。丢失的是「assistant 提议文本修改」这条能力本身，不是实现细节；feat 侧没有等价物。

**5. `agents/prompt_overrides.py`（276 / 241，def 8→7）**
- 做法：提示词覆盖块的解析与渲染。
- 差异：feat 换成 `PromptKey` + `resolve_prompt_block`（引擎按 key 逐块取），删 `resolve_overrides_for_generation`；`test_prompt_override_wiring` 18 条用例整组重写。
- 判定：**取 feat**。接线与测试是一套的，取 main 会让 18 条新用例全红。

**6. `agents/proposals.py`（1496 / 1353，def 34→32）**
- 做法：提案 → 目标写入 → 版本回滚。
- 差异：main 有 `restore_version(..., expected_hash=)`（目标漂移则拒）、`write_target(..., repair_context=)`、`repair_write_allowed`、`merge_pending_acceptance`；feat 全删。main 的异常集合多 `ValueError`。
- 判定：**手工合并**。骨架 feat；搬 `expected_hash` 校验分支（`restore_version` 开头 10 行）与 `merge_pending_acceptance`，并恢复 `test_restore_expected_hash_guards_drift`。依据：没有漂移守卫时，用户在生成期间改了目标正文，回滚会把新内容覆盖掉——这是数据损坏级别的问题。

**7. `agents/registry.py`（1948 / 1232，def 42→41）**
- 做法：assistant 工具注册表。
- 差异：feat 加 15 个（`_impl_query_ai_flavor_memory`、`_impl_browse`、`_impl_similar`、`_impl_list_actions`、`_impl_anchor`、`_impl_inspect_retrieval_trace`、`_record_invoke_trace`、`_trace_status`、`describe_tools`…），`invoke(..., trace=)`；main 加 16 个（`_impl_probe_thinking`、`_impl_probe_concurrency`、`_impl_audit_prompt`、`_impl_audit_chunks`、`_impl_audit_text_quality`、`_impl_audit_narrative`、`_impl_run_diagnosis`、`_impl_propose_action`、`_impl_propose_text_edit`、`_impl_record_workflow_step`、`_impl_get_workflow_state`、`list_diagnostic_skills`、`_params`…）。
- 关键签名：main 的 `_impl_read_generation_segments` 有 `start_char`/`max_chars`，feat 没有——**长段只能读开头**。
- 判定：**手工合并**。骨架 feat；搬 `start_char`/`max_chars` 两个参数与切片分支（约 8 行）。审计类工具（`_impl_audit_*`/`_impl_run_diagnosis`）只有前端仍需要时才搬，且要连带 main 的 diagnosis 规则集。

**8. `corpus_health.py`（1036 / 669，def 15→10）**
- 做法：语料清洗与健康报告。
- 差异：feat 删 `find_foreign_blocks`、`strip_foreign_blocks`、`_foreign_chapter_spans`、`_foreign_top_grams`——跨书/外来正文的发现与剥离整条消失。`strip_repeated_ads` feat 少了 `preserve_layout`、`templates` 两个参数。
- 判定：**取 main**。这是唯一能检出「参考语料里混进别的书」的机制；feat 既没有替代，也没有在别处报这个信号。

### 3.2 核心运行时

**9. `db.py`（239 / 381，def 6→8）**
- 做法：建表、补列、保护。
- 差异：feat 加 `_block_drop_table_on_real_db`/`is_protected_db`/`_sqlite_basename`；main 有 `_backfill_product_keys`。补列 main 50 / feat 79，main-only 14、feat-only 45。
- 判定：**手工合并**。骨架 feat；补列清单取「保留功能」并集（不是全并集）；`_backfill_product_keys` 只有保留 `style_distills.product_key` 时才搬。

**10. `generation/branch.py`（349 / 367，def 6→6）**
- 差异：`roll_branch(..., *, chapter_based=False)`、`attach_branch_to_state(..., *, chapter_based=False)`。
- 判定：**取 feat**。与 feat 的章调度一致；main 无此参数。

**11. `generation/engine.py`** — 见 §2.1。判定 **取 feat**（丢失清单已列）。

**12. `generation/power_scale.py`（255 / 323，def 8→11）**
- 差异：feat 加 `scale_seq`、`detailed_scale`、`_load_15`，配 `generation/data/power_scale_15.json`（15 级阶梯）。
- 判定：**取 feat**。

**13. `generation/power_system_type.py`（125 / 166，def 5→6）**
- 差异：feat 加 `system_type_from_bible`。
- 判定：**取 feat**（sentinel 的 `system_type` 判定依赖它）。

**14. `generation/prompts.py`** — 见 §2.1。判定 **取 feat**。

**15. `generation/timeline.py`（692 / 734，def 15→19）**
- 做法：剧情节点推进 + 分支。
- 差异：feat 加 `assign_chapter_ranges`/`_balanced_chapter_ranges`/`node_for_chapter`/`sync_node_for_chapter`/`has_chapter_ranges`/`_clone_state`；删 `_authority_block`/`with_authority`。
- 关键：`judge_node_completion` main 返回 `dict{verdict, reason, sources, audit}`（`source_id`、`text_hash`、`plan_hash`、`error_category`，要求逐字段落证据），feat 返回 `bool | None`；`build_node_prompt` main 多 `authority`、`continuation` 两参；`force_complete_current` feat 多 `keep_current`；`state_from_dict` feat 多 `total_chapters`。
- 判定：**手工合并**。骨架 feat（章级调度必须留）；搬 main 的 `judge_node_completion` 证据判定与 `body_evidence.paragraph_sources/text_hash/safe_response`，调用点把 `dict["verdict"]` 映射成 feat 需要的三态。依据：feat 的 bool 版本一旦判错，事后只有「切了/没切」一条信息，无法复核。

**16. `ingestion/chunk.py`（217 / 20，def 5→0）**
- 差异：main 有 `chunk_quality`、`_is_balanced`、`_split_sentences`、`_units`（句/段边界优先）；feat 只剩 `chunk_text`（字符窗口 + 重叠）。
- 事实：两边 `backend/app` 内都**没有** `chunk_text` 的调用方（grep 只命中 `proposals.py` 的字段名说明）；main 的 `audit.py` 只是把 `"chunk_quality"` 当 note 的键。
- 判定：**取 feat**。被删的是死代码；若将来要做语料分块质检，再从 main 取回。

**17. `ingestion/clean.py`（259 / 388，def 6→10）**
- 差异：feat 加 `strip_duplicate_paragraphs`、`strip_cross_novel_paragraphs`、`_split_blocks`、`_join_blocks`、`_norm_para`、`_count_tokens`、`cjk_len`；删 `strip_site_marker_lines`、`is_site_marker_line`、`clean_consumption_text`（站点标记/消费文清洗）。`build_clean_report` feat 多 `duplicate_report`/`cross_novel_report`。
- 判定：**手工合并**。骨架 feat；搬 3 个站点标记清洗函数并在 `build_clean_report` 里恢复对应计数项。依据：重复段/跨书段是「内容层」清洗，站点标记是「噪声层」清洗，两者不互替。

**18. `ingestion/encoding.py`（174 / 174）** — 判定 **两者等价**。

**19. `ingestion/parse_txt.py`（109 / 454，def 2→13）**
- 差异：feat 加 `_split_by_headings_with_volumes`、`_split_by_headings`、`_plausible_title`、`_strict_title_ok`、`_plain_title`、`_numeric_candidates`、`_isolated`、`_error_page_reason`、`_build_chain`、`flush`、`cjk_len`；签名 `parse_txt(..., clean_tail=True)` → `(..., report=None)`（可回收解析报告）。
- 判定：**取 feat**。

**20. `ingestion/seed.py`（30 / 49，def 0→2）**
- 差异：feat 加 `resolve_seed_dir`，缺失时抛 `FileNotFoundError`。
- 判定：**取 feat**。

**21. `ingestion/store.py`（172 / 163，def 2→2）**
- 差异：`ingest_text(..., drop_foreign_blocks=False)` feat 删除；main 抛 `HTTPException`/`RuntimeError`，feat 不抛。
- 判定：**手工合并**。骨架 feat；恢复 `drop_foreign_blocks` 参数与调用 `strip_foreign_blocks`（配 #8），并恢复 `HTTPException` 语义（入库失败要 4xx/5xx，不是静默返回）。
- 说明：这一条与 #8、#54 必须一起改，否则参数存在但没人用。

**22–31. kg 系列** — 见 §2.3。`batch_worker`/`build`/`sentinel`/`queries`/`judge`/`bible`/`extractor` 取 feat；`extractor_v3` 取 feat；`ledger` 取 feat；`retrieval_v3` 手工合并。

**32. `llm/provider.py`** — 见 §2.2。判定 **取 feat**。

**33. `llm/reasoning.py`（134 / 146，def 6→7）**
- 差异：feat 加 `dropped_chars`（内联推理被过滤掉的字符数），供 `reasoning_chars_before_content` 归因。
- 判定：**取 feat**。

**34. `main.py`（136 / 141，def 2→3）**
- 差异：feat 加 `healthz`。router 集合：main 16 个（多 `digest_router`、`roleplay_router`、`assistant_action_proposals_router`、`assistant_probe_router`、`assistant_attachments`）；feat 13 个（多 `assistant_wizard_router`、`assistant_snapshot_router`）。同名 11 个。
- 判定：**手工合并**。骨架 feat（`healthz` 保留）；router 列表按最终存活的模块裁剪。**不能两边并集直接 include**：同名模块的两份 `APIRouter` 会造成重复路径，且 main-only router 依赖 main-only 表。

**35. `models.py`** — 见 §2.5。判定 **手工合并**。

**36. `retrieval/bm25.py`（41 / 41）** — 判定 **两者等价**。

**37. `retrieval/search.py`（162 / 88，def 5→4）**
- 差异：main 有 `weighted_search`；feat 删。feat 的 `hybrid_search` 仍被引擎使用。
- 判定：**取 feat**。`weighted_search` 的唯一调用方是 main-only 的 `retrieval/strategies.py`。

### 3.3 routers / schemas

**38–47** — 见 §2.6。判定：`assistant` 取 feat、`assistant_generation` 取 feat、`assistant_proposals` 手工合并、`generation` 取 feat、`kg_v3` 手工合并、`novels` 手工合并、`projects` 手工合并、`settings` 取 feat、`wizard` 无法判定、`schemas` 手工合并。

### 3.4 style

**48–52** — 见 §2.4。判定：`exemplar`/`jobs`/`narrative`/`stylometry` 手工合并，`pipeline` 取 main。

**53. `tokenize.py`（30 / 23，def 2→1）**
- 差异：feat 删 `_chinese_tokens`，仍用 jieba。
- 判定：**取 feat**。被删函数无调用方。

### 3.5 根级脚本

**54. `backend/load_corpus.py`（227 / 210）**
- 差异：`import_txts(..., drop_foreign_blocks=False)` feat 删除。
- 判定：**手工合并**（与 #8/#21 同一处开关，一起恢复）。

**55. `backend/test_extractor_v2.py`（61 / 61）** — 仅 1 行注释措辞 → **两者等价**。

**56. `backend/test_m3.py`（146 / 152）**
- 差异：断言从 `appearances == [1,2,3]` 改为 `first_chapter/last_chapter`，并在注释里说明旧断言「看似对的，实则钉死缺陷」。
- 判定：**取 feat**。

**57. `backend/test_m3_api.py`（114 / 120）** — 同上 → **取 feat**。

**58. `backend/test_m4_api.py`（327 / 367）**
- 差异：旧断言要求 `user_prompt` 含「前情提要/检索上下文/大纲」三个扁平标题；feat 改成守「续写指令结构」，因为前情改走 `prior_blocks`、时间线分级投喂时不再注入整条大纲。
- 判定：**取 feat**。

**59. `backend/test_p0_regression.py`（482 / 528）** — feat 加 `complete_message`/`read1` 假件、`chat(**kwargs)` → **取 feat**。

**60. `backend/test_summary_tree.py`（649 / 649）** — 同名 12 函数，`chat/stream_chat` 收 `**kwargs` → **取 feat**。

**61. `backend/verify_acceptance.py`（614 / 697）** — feat 加 `sent_cv`/`metaphor_per100`/`pctl`/`_clean`/`jaccard`/`grams` 6 个度量 → **取 feat**。

**62. `backend/verify_corpus_health.py`（406 / 406）** — **两者等价**。

**63. `backend/verify_sentinel.py`（919 / 951）**
- 差异：feat 把 `REALM_SYSTEMS` 从 import 改成脚本内夹具阶梯（生产内置表已删）。
- 判定：**取 feat**（与 #31 一致）。

**64. `backend/verify_style_pipeline.py`（270 / 266）** — 仅注释与污染样本命名 → **两者等价**。

**65. `backend/verify_stylometry.py`（336 / 296）** — feat 删 42 行「换尺前后基线」说明，断言未变 → **两者等价**。

### 3.6 tests

**66–68. `tests/concurrent_*.py`** — feat 只多捕 `SystemExit`，断言相同 → **两者等价**。

**69. `tests/conftest.py`（241 / 238，def 7→7）**
- 差异：feat 加 `_db_logical_fingerprint`（库指纹，防跨用例复用连接）；main 有 `experimental_consent` 夹具（实验功能开关）。
- 判定：**手工合并**。两者互不冲突，直接并存。

**70. `tests/test_assistant_agent_tools.py`（686 / 954，def 40→56）** — feat 加 16 条 QA 工具循环/白名单/墙钟预算/空 tool_calls 用例 → **取 feat**。

**71. `tests/test_assistant_audit.py`（1482 / 1460，def 90→89）**
- 差异：feat 删 `test_audit_corpus_does_not_report_a_corpus_a_project_already_fitted`（对应 main 的 `_fitted_novel_ids`）。
- 判定：**手工合并**（随 #1：保留语料审计就保留该用例）。

**72. `tests/test_assistant_corpus_clean.py`（658 / 658，def 43）** — 仅假件签名 → **取 feat**。

**73. `tests/test_assistant_diagnose.py`（761 / 756）**
- 差异：`test_all_rules_are_registered` → `test_all_four_rules_are_registered`（main 5 条、feat 4 条）。
- 判定：**手工合并**（随 #2 增加规则后，注册表用例要改成「按实际规则数」而不是硬编码 4）。

**74. `tests/test_assistant_proposals.py`（1527 / 1490，def 77）**
- 差异：main 有 `test_restore_expected_hash_guards_drift`；feat 加 `_iter_route_paths`。
- 判定：**手工合并**（两者都要）。

**75. `tests/test_assistant_provider_config.py`（409 / 399）** — `fake_chat` → `fake_complete_message` → **取 feat**。

**76. `tests/test_assistant_readonly.py`（391 / 363）** — 仅删 29 行 docstring → **两者等价**。

**77. `tests/test_db_isolation.py`（122 / 131）**
- 差异：main 有 `test_child_process_does_not_reuse_this_sessions_db`；feat 换成 `test_logical_fingerprint_ignores_only_heartbeat_rows`。
- 判定：**手工合并**。两者防的不是同一件事：一个是跨进程连接复用，一个是「心跳行不算数据变化」。

**78. `tests/test_distill_jobs.py`（609 / 718，def 31→33）** — feat 加「角色隔离」「按 tier 复用 active run」两条 → **取 feat**。

**79. `tests/test_finish_reason_truncation.py`（170 / 176）** — `read(n=-1)` 适配 `read1` → **取 feat**。

**80. `tests/test_ingestion_encoding.py`（120 / 120）** — **两者等价**。

**81. `tests/test_prompt_override_wiring.py`（293 / 325）** — 18 条用例整组换 → **取 feat**。

**82. `tests/test_provider_timeouts.py`（123 / 175）** — `read(n=-1)` → **取 feat**。

**83. `tests/test_reasoning_wiring.py`（150 / 155）** — 同上 → **取 feat**。

**84. `tests/test_schema_migration_completeness.py`（121 / 124）**
- 差异：期望补列清单不同（main 50 / feat 79）。
- 判定：**手工合并**。清单改成「§3.5 最终保留列的并集」，并在注释里写明「并集不是目的，覆盖每个有消费方的列才是」。

**85. `tests/test_settings_roles.py`（820 / 812，def 48）**
- 差异：main 对 `_PROVIDERS_YAML["writer"]["timeout"]` 断言（不硬编码数字）；feat 硬编码 `assert block["timeout"] == 180`，并另加「三处模型清单一致」的检查。
- 判定：**手工合并**。取 main 的「不硬编码、对 yaml 断言」写法 + feat 的模型清单一致性检查。依据：feat 的硬编码在下次调超时时必红，而它想证明的其实是「GET 如实展示单例值」。

**86. `tests/test_style_tier.py`（904 / 865，def 42→41）** — feat 删 `async_chat`、`chars_per_chapter` → **取 feat**。

**87. `tests/test_watermark_detection.py`（124 / 124）** — **两者等价**。

---

## 4 根级配置

| 文件 | 差异 | 判定 |
|---|---|---|
| `backend/requirements.txt` | main：核心依赖不含 `pgvector`/`psycopg2`（另拆 `requirements-postgres.txt`），无显式 `PyYAML`；feat：内联 `pgvector==0.3.1`、`psycopg2-binary==2.9.13`、`PyYAML==6.0.3` | **手工合并**：取 main 的「核心可装 / PostgreSQL 另装」结构；补 feat 的 `PyYAML`（`app/config.py` 直接 `import yaml`，不能靠间接依赖）。`pgvector` 两边 `models.py` 都是 try-import 降级，不是启动硬依赖 |
| `backend/config/providers.yaml` | `writer.model` main 钉 `MiniMax-M3.1-Flash`，feat 为空；`writer.max_tokens` 32768 → 131072；`writer.timeout` 300 → 180；feat 加 `assistant.tools_unsupported_models: glm-z1-flash`；feat 加 opencode go 渠道；`distiller.max_tokens` 8192 → 32768 | **取 feat**：数值是 40 多章实测调出来的（第 31 章 65536 撞满、单章 900s）；`model` 置空更适合开源默认。注意 `timeout: 180` 必须配 provider 的 `total_timeout: 960` 才有意义，yaml 不能单取 |
| `pytest.ini` | feat 只删 6 行注释，`addopts = -ra -q` 与 `testpaths` 完全一致 | **两者等价** |
| `package.json` | 前端依赖 | 不在本报告范围（另一路负责），不判 |

---

## 5 摘要

### 5.1 判定分档计数（87 个）

| 判定 | 个数 | 文件 |
|---|---|---|
| 取 feat | **42** | engine / prompts / branch / power_scale / power_system_type / parse_txt / seed / chunk / batch_worker / bible / build / extractor / extractor_v3 / judge / ledger / queries / sentinel / provider / reasoning / search / assistant / assistant_generation / generation(router) / settings(router) / tokenize / prompt_overrides / test_m3 / test_m3_api / test_m4_api / test_p0_regression / test_summary_tree / verify_acceptance / verify_sentinel / test_assistant_agent_tools / test_assistant_corpus_clean / test_assistant_provider_config / test_distill_jobs / test_finish_reason_truncation / test_prompt_override_wiring / test_provider_timeouts / test_reasoning_wiring / test_style_tier |
| 取 main | **3** | `agents/pending_actions.py`、`corpus_health.py`、`style/pipeline.py` |
| 手工合并 | **29** | audit / diagnosis / log_audit / proposals / registry / db / timeline / clean / store / retrieval_v3 / main.py / models / assistant_proposals / kg_v3 / novels / projects / schemas / exemplar / jobs / narrative / stylometry / load_corpus / conftest / test_assistant_audit / test_assistant_diagnose / test_assistant_proposals / test_db_isolation / test_schema_migration / test_settings_roles |
| 两者等价 | **12** | encoding / bm25 / test_extractor_v2 / verify_corpus_health / verify_style_pipeline / verify_stylometry / 3×concurrent_* / test_assistant_readonly / test_ingestion_encoding / test_watermark_detection |
| 无法判定 | **1** | `routers/wizard.py`（取决于前端是否仍调 4 个大纲端点） |

### 5.2 「取 main 更好」的三个（最反直觉）

1. **`agents/pending_actions.py` —— feat 少了 3 个函数，等于砍掉一条能力。**
   feat 删除 `collect_text_edit_cards` / `build_text_edit_proposal_action` / `build_action_proposal_action`，全仓无替代。这文件的删减**没有任何替代实现**，直接后果是 assistant 不能再把「文本修改」渲染成待确认卡片。它零外部依赖，整份取 main 无成本。

2. **`corpus_health.py` —— feat 删掉了唯一的跨书污染检测。**
   `find_foreign_blocks` / `strip_foreign_blocks` 被整段删除，`ingestion/store.py` 的 `drop_foreign_blocks` 参数也随之消失。结果是：参考语料里混进别的书，系统再也发现不了，而且会把它当风格样本蒸馏。**这是四个判据里「数据正确性」直接失守的一处**，比任何「新功能」都重要。

3. **`style/pipeline.py` —— feat 的章级阈值是硬编码，main 的可以按新参考书重算。**
   main 的 `chapter_lengths` 参数接 `style/chapter_stats.py`，产出档案里的每章汉字分布；feat 把章级阈值写成 `consistency.ARCHIVE_CHAPTER_PERCENTILES` 常量表。换一本参考书，feat 的阈值不会变——这不是「新旧」，是**方法上无法校准**。代价是要一并迁 `chapter_stats.py`（约 150 行，纯统计）。

### 5.3 main 侧明显更好、但必须手工搬的 6 处

这几处不足以整份取 main（会拖进 main-only 模块），但**不搬就是退步**：

| 文件 | 搬什么 | 不搬的后果 |
|---|---|---|
| `generation/timeline.py` | `judge_node_completion` 的证据判定 + `audit{source_id, text_hash, plan_hash, error_category}` | 节点切错后只剩「切了/没切」，无法复核 |
| `agents/proposals.py` | `restore_version(expected_hash=)` 漂移守卫 + `merge_pending_acceptance` | 生成期间目标被改，回滚会覆盖新正文 |
| `agents/registry.py` | `_impl_read_generation_segments` 的 `start_char`/`max_chars` | 长段只能读开头，assistant 诊断被截断 |
| `kg/retrieval_v3.py` | `max_foreshadows` + `_payoff_of` + 裁剪分支 | 未回收伏笔随章数线性进提示词 |
| `style/stylometry.py` | `band_check` / `profile_band` / `band` | 无法按「本项目参考书」判偏离，只剩全局硬编码阈值 |
| `style/exemplar.py` | `exclude_chunk_ids` + `filter_single_character` | 同一段反复当范例；样例对白全是同一角色 |

### 5.4 无法判定（1 个）

- `routers/wizard.py`：feat 只有 14 个端点，main 的 `generate_outline`/`clarify_outline`/`apply_outline`/`revise_budget` 与 `generation/planning.py` 在 feat 侧不存在。后端自洽性上 feat 无问题；**是否算退步，取决于前端向导是否仍调这 4 个端点**——需前端侧确认后才能定。若要保留，则按 §2.6 的搬运清单做，而不是整份取 main（main 的 `_style_sample` 返回 tuple，与 feat 叙事 API 不兼容）。

### 5.5 一句话结论

87 个后端文件里，**42 个只能取 feat**（数据形状、引擎签名、测试三重绑定），**3 个应整份取 main**（pending_actions / corpus_health / pipeline），**29 个必须手工合并**（其中 6 处是 main 侧的正确性/可观测性修复，不搬就是退步），**12 个等价**，**1 个待前端确认**。合流的主要风险不在「选哪边」，而在 **29 个手工合并里的跨文件配套**：`corpus_health + store + load_corpus` 是一组、`retrieval_v3 + kg_v3` 是一组、`timeline + 证据模块` 是一组、`stylometry + diagnosis` 是一组——任何一组只改一半，都会出现「参数在、没人用」或「阈值在、尺不同」的静默错配。
