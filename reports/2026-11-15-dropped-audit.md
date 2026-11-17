# 弃项审计与大纲端点仲裁（2026-11-15）

> 说明：下文出现的 `<temp>` 路径是当时本机临时脚本 / 副本 / 日志的位置，**这些脚本未入库**，仅作取证记录保留，不可直接复跑。

- 基准：feat = `feat/resume-push-2026-10`（当前工作区）；main = `freeze/main-cb05a45`（`cb05a45`，只读 worktree `<temp>\mwt-main3`）
- 方法：`git grep` 全仓检索 + `ast` 符号级 diff（main-only 符号 / 同名实现差异）+ 逐个查 feat 等价物；不按文件名推断。
- 关联：`reports/2026-11-15-main-coverage-ledger.md` / `.csv`

## 一、`wizard.py` 4 个大纲端点：调用方仲裁

### 1. 前端的真实链条

**main 侧（有活消费方）**

| 层 | 证据 |
|---|---|
| API 定义 | `frontend/src/api/client.ts:654` `generateOutline`、`:657` `clarifyOutline`、`:660` `applyOutline`、`:663` `reviseBudget` |
| 唯一调用方 | `frontend/src/views/ProjectWorkspace.vue:156 runGenerateOutline` → `:166 api.generateOutline(...)`；`:189 runClarifyOutline` → `:201 api.clarifyOutline(...)`；`:213 runApplyOutline` → `:218 api.applyOutline(...)`；`:2289 runReviseBudget` → `:2304 api.reviseBudget(...)` |
| UI 入口 | `ProjectWorkspace.vue:3495`（生成大纲按钮）、`:3527`（澄清）、`:3535`（应用）、`:3701`（确认修订预算） |

**feat 侧（零消费方）**
- `git grep -nE "generateOutline|clarifyOutline|applyOutline|reviseBudget|generate-outline|outline/apply|outline/clarify|outline/revise-budget"`（排除 `reports/` 与 `*.md`）**无命中（exit 1）**。
- feat 的 `client.ts` 里没有任何这 4 个函数；同名行段是另一套接口：`:794-872` 的 `/assistant/wizard/{id}`、`/start`、`/answer`、`/say`、`/goto`、`/commit`、`/run` 等 12 个端点。
- feat 的 `ProjectWorkspace.vue` 对 `wizard` 零命中；它走的是 assistant-wizard 状态机，不是 main 的大纲向导。

> 更正我上一轮的说法：`client.ts:655,661` 是 **main 树**的行号，且真实定义行是 `654/657/660/663`；feat 树里这些行属于 `/assistant/wizard/*`。之前把 main 的调用链误当成 feat 的，现在收回。

### 2. 测试打的是哪个路径、断言什么

两个测试文件**只存在于 main 树**（feat 树无此文件，`git ls-tree` 已核）。

| 测试 | 路径 | 断言 | 性质 |
|---|---|---|---|
| `tests/test_copy_gate.py:79` | `POST /api/projects/{id}/generate-outline` | `status == "outline_draft"`；`structure_copy_check.verdict == "clean"`；第一次照搬后模型被重试（`len(seen)==2`）；重试提示必须含“纠正照搬问题”并把命中片段回灌；改不掉时 `preview=False` 直接拒绝且不落库 | **真实业务流**：照搬门禁的“检测→回灌→重试→拒绝保存”闭环，不是端点存在性 |
| `tests/test_copy_gate.py:103`、`:144` | 同上 | 同一闭环的持久照搬（`:91 test_persistent_copy_is_refused_and_nothing_is_saved`）与“模型没见过的句子不算照搬”（`:119 test_a_phrase_the_model_never_saw_is_not_treated_as_copying`） | 真实业务流 |
| `tests/test_foundation_planning.py:77,83,105,108,116,123,132` | `POST .../generate-outline`、`POST .../outline/apply` | `confirm_bible=False` → 422；apply 后 `GET /projects/{id}` 能读回 outline；重复 apply → 409；已存在生成 → 409；失败分类（`error.code`）与重试次数 | **真实业务流**：写库、读回、幂等/冲突、错误分类 |

### 3. 结论

**这 4 个端点在 feat 里没有活的前端消费方，也没有测试。** 处置改回 `dropped`。

需要写清的中间状态是分侧的：
- **main 侧**：有前端（4 个调用点）+ 有测试（`test_copy_gate.py` 3 个用例、`test_foundation_planning.py` 6 个用例），是“有业务调用且有测试”；
- **feat 侧**：前端零调用、测试零覆盖，是“无调用方且无测试”。

因此不是“有测试无业务调用”这个中间态，而是**两边都完整、只是整条能力留在 main**：调用方 `ProjectWorkspace.vue` 已随同名冲突文件整文件取 feat，测试只测这条被放弃的链路。若将来要恢复，必须同时恢复 4 个端点 + `generation/planning.py` + 前端面板，不能只搬端点。

---

## 二、49 条 `dropped` 逐条：放弃的到底是什么

> 判定口径：`risk` = 该符号在 feat 无等价物，且做**数据正确性**或**安全/合规**工作；`benign` = 写法、日志、注释、测试组织差异，或 feat 有等价实现（含改名实现）。
> `feat 侧等价物` 一律写实名；“无”表示全仓检索不到等价能力。

| # | 路径 | 放弃的 main 符号（实名） | 这个符号的作用 | feat 侧等价物（实名） | 判断 |
|---|---|---|---|---|---|
| 1 | `backend/app/agents/react_loop.py` | `run_react`、`build_tool_catalog`、`infer_parameters`、`validate_arguments`、`parse_decision`、`_normalize_arg_value`、`canonical_arguments`、`place_signature`、`result_fingerprint`、`render_stagnation_directive`、`strip_observation_echo`、`_neutralize`、`clip_arguments`、`oversized_argument`、`render_observations`、`execute_tool_call`、`render_user_message`、`render_repair_message`、`degraded_result`、`ReactResult`、`CatalogTool`、`STAGNATION_THRESHOLD`、`STAGNATION_REDIRECT_ROUNDS`、`DEFAULT_CONTEXT_BUDGET_CHARS`、`MAX_ARGUMENT_CHARS`（共 65 个顶层标识） | 多轮 ReAct 主循环：参数校验、停滞重定向、工具结果回显净化、上下文预算 | `_qa_answer_with_tools()`（`routers/assistant.py:477`，≤3 步、只读工具）；无停滞/回显净化/上下文预算 | **risk**：`strip_observation_echo`/`_neutralize` 是提示注入防线（工具结果来自用户语料），feat 无等价物；不是“更强”，是更简 |
| 2 | `backend/app/agents/registry.py` | `_impl_propose_text_edit`、`_impl_propose_text_fix`、`_impl_propose_action`、`_impl_record_workflow_step`、`_impl_get_workflow_state`、`_impl_audit_chunks`、`_impl_audit_narrative`、`_impl_audit_prompt`、`_impl_audit_text_quality`、`_impl_run_diagnosis`、`_impl_probe_thinking`、`_impl_probe_concurrency`、`_resolve_probe_target`、`list_diagnostic_skills`、`_TOOL_PARAMETERS`、`_params`、`TEXT_EDIT_MIN_RATIO` | 把提案、工序链、诊断、模型探针暴露成模型工具 | 12 个只读工具（`read_*`/`browse_*`/`search_*`/`describe_*`） | **risk**：`_impl_propose_text_edit` 强制 `after_text` 只能来自模型（服务端不得改写正文）是写入红线；且第 4/5 批模块搬入后无工具入口 |
| 3 | `backend/app/generation/engine.py` | `_reference_chapter_hint`、`_roleplay_character_name`、`_REPETITION_WARN_RATIO`、`MIN_GAP_TO_ELLIPSIS`、`_dedup_before_commit`、`REWRITE_GATE_FEATURES`、`_style_gate`、`_strip_ws`、`REWRITE_GATE_MIN_CHARS`、`_rewrite_gate`、`_rewrite_decision`、`_rewrite_provider`、`_maybe_correct_form`、`_maybe_correct_attribution`、`_maybe_correct_ellipsis`、`_candidate_pool_size`、`_judge_final_text`、`_run_pool_selection`、`_run_text_chain`、`_produce_with_pool`、`_reference_plain_dialogue_ratio`、`_reference_corpus_text`、`_reference_proper_nouns`、`_project_character_names`、`_maybe_insert_beats`、`_rewrite_targets`、`_maybe_rewrite_style`、`_VERBATIM_MIN_LEN_RATIO`、`_maybe_fix_verbatim`、`_verbatim_system_prompt`、`_pipeline_record`、`_refresh_character_personas`、`_prev_scene_hint`、`_l2_character_names`、`_scene_contract_for_generation`、`_backfill_character_evidence`、`_audit_and_repair_scene`、`_TIMELINE_NON_STATE_KEYS`、`_cjk_count`、`_measure_payload`、`_enforce_length`、`_plan_exhausted`、`_rebind_acceptance_after_generation`、`_prev_segment_state`、`_continuation_context`、`_contract_authority`、`_current_authority``（共 47 个） | 生成主链：照搬修正、提交前去重、权威边界、验收义务重绑、候选池择优、质量门 | `_should_rewrite()`（`:2866`）、`_rewrite_rounds()`（`:3846`）、`_compression_round()`（`:3801`）、`_regenerate_if_truncated()`（`:3461`）、`_coherence_report()`（`:1671`） | **risk**：`_maybe_fix_verbatim`（照搬修正）、`_dedup_before_commit`（提交前重复段）、`_contract_authority`/`_current_authority`（权威边界）、`_rebind_acceptance_after_generation`（验收义务）四项是正确性/合规；候选池/节拍/风格项 benign |
| 4 | `backend/app/generation/prompts.py` | `_override_block`、`_character_constraint_line`、`_description_guides`、`_psych_scene_boundary`、`_scale_contrast_block`、`_character_profile_block`、`_voice_character_for`、`_character_template_block`；改写 `_profile_block`、`_narrative_block`、`build_system_prompt`、`build_user_prompt` | 按生成模式/角色档案/量级对比拼装提示块 | `profile` 改为 `narrative_mode` + `catalog_skeleton_block` + `item_holding_block`；“禁止情绪直陈”在 `prompts.py:986` | **benign**：签名与拼装方式不同；L2 角色档案/角色模板缺口对应第 7/9 批 |
| 5 | `backend/app/ingestion/parse_txt.py` | `CHAPTER_RE` | 章标题识别的简化正则 | `_HEAD_RE` / `_ARABIC_HEAD_RE`（覆盖更多章头形态） | **benign**：feat 是超集 |
| 6 | `backend/app/kg/batch_worker.py` | `reap_stale_runs`、`_mark_batch_failed`、`_active_foreshadow_names`、`_is_suspect_foreshadow`、`_sanitize_payoff_chapter`、`_nearest_existing_name`、`_session_factory` | 重启后收割卡死批次、失败落账、把已有伏笔名喂给抽取器、回收章净化、近似名合并 | `_mark_batch_failed` 等价逻辑（`batch_worker.py:311` 标 failed）；`reap_stale_runs` 无等价（feat 只收割 style 任务） | **risk**：进程重启后的卡死批次不会再被收割；伏笔名无去重/近似合并 |
| 7 | `backend/app/kg/build.py` | `rebuild_generated_graph`、`_segment_pairs` | 从正文段重建生成图，并校验段对形状 | `rebuild_generated_kg()`（从 DB 重建） | **benign**：架构不同，重建能力等价 |
| 8 | `backend/app/kg/extractor_v3.py` | `known_foreshadow_block` | 把项目已有伏笔名写进抽取提示，防止同名伏笔被重复记账 | 无 | **risk**：同名伏笔会重复累积，伏笔账本越抽越脏 |
| 9 | `backend/app/kg/sentinel.py` | `REALM_SYSTEMS`、`REALM_ORDER`、`_REALM_ALT` | 内置境界阶梯表，用于境界跃迁事件抽取 | `build_extractors(names, realm_order)` 只接受显式阶梯；`sentinel.py:160` 注明“未配置=不注入、不判定” | **benign**：刻意改为纯数据驱动，不是漏搬 |
| 10 | `backend/app/routers/assistant.py` | `_FALLBACK_ANSWER_NO_PROVIDER`、`_FALLBACK_ANSWER_NOT_CONVERGED`、`_excerpt`、`_evidence_rows_from_tool_results`、`_evidence_rows_for_legacy_tool` | ReAct 失败兜底答案 + 工具结果转 Evidence 行 | `Evidence` 模型（`models.py:766`）+ `assistant.py:429/465` 直接构造 evidence_rows | **benign**：feat 有等价实现 |
| 11 | `backend/app/routers/assistant_generation.py` | `AssistantInvokeGenerationRequest._validate_action_fields`、`assistant_invoke_generation`、`_segment_out`（同名实现差异） | 助手触发一次生成并校验动作字段 | 同名端点保留，实现改写 | **benign**：无独有能力 |
| 12 | `backend/app/routers/generation.py` | `_generate_guarded`、`_verbatim_conflict`、`_require_workflow_ready`、`_require_story_seed`、`_resolve_profile` | 生成前照搬门禁（409）、工序就绪校验、story seed 校验、profile 解析 | 仅 `_require_project()`（`:70`）+ engine 异常映射 | **risk**：照搬门禁是合规底线；`_require_story_seed` 是生成前输入校验。`_require_workflow_ready` 属 main 工序链，随第 5 批 |
| 13 | `backend/app/style/narrative.py` | `WEIGHTING_VERSION`、`MATURITY_WEIGHT_EARLY`、`MATURITY_WEIGHT_MATURE`、`TARGET_BLOCK_CHARS`、`_chapters_per_block`、`DIM_LABELS`、`_DIM_MAX_CHARS`、`RULES_PER_DIM`、`_ARCHIVE_MAX_CHARS`、`_clip`、`FORBIDDEN_SECTION_MARKER`、`_build_word_order_prompt`、`_COMBAT_NOISE_PREFIX_RE`、`_PSYCH_NOISE_PREFIX_RE`、`_build_combat_prompt`、`_build_psych_prompt`、`_scene_scores`、`scene_corpus_stats`、`dominant_scene`、`classify_scene`、`_sample_chapters_spread`、`sample_chapters_for_scope`、`maturity_weight`、`_distill_cutoffs`、`_attach_maturity_weights`、`_COLLECTIVE_SUFFIXES`、`_COLLECTIVE_SUFFIX_MIN_LEN`、`_is_collective_entity`、`_DISTILL_TOTAL_PHASES`、`PH_PREPARE`、`PH_SAMPLE`、`PH_EXTRACT`、`PH_VOICE`、`PH_L2`、`PH_SUMMARY`、`_resolve_concurrency`、`_resolve_block_chars`、`_configured_block_chars`、`_block_chars_for_fingerprint`、`_l2_candidate_reason`、`_extract_character_profiles`、`extract_character_template`、`estimate_distill_cost`、`_EVIDENCE_TAIL_RE`、`_ACTION_CHAIN_RE`、`_VOICE_NAME_PREFIX_RE`、`_RELATION_FIELD_RE`、`_PLOT_BEARING_DIMS`、`_strip_evidence`、`_EVIDENCE_CAPTURE_RE`、`EXAMPLE_MAX_CHARS`、`_extract_example`、`_strip_forbidden_evidence`、`_prompt_form`、`_group_by_chapter`、`_mature_chapters`、`_rule_weight`、`_spread_pick`、`_select_dim_rules`、`_select_beat_steps``（共 60 个） | 成熟度加权选规则、场景分类采样、L2 角色档案/角色模板、证据剥离后进提示、蒸馏成本估算 | `select_rules_with_audit()`、`describe_narrative()`（含“清污：职务词/地名/截断名不进提示”）、`_validate_evidence()`（`:1317`）、`skeletonize_beat_rule()` | **benign**：质量能力缺口（成熟度/场景分类/角色模板/成本估算）；角色模板已排第 9 批，不在正确性范围 |
| 14 | `backend/app/style/stylometry.py` | `MEASUREMENT_VERSION`、`_SENT_END_RE`、`_CJK_CHAR`、`_PUNCT_ALL`、`paragraph_spans`、`dialogue_cjk_count`、`_BARE_SEG_TAIL_MAX`、`_strip_quoted`、`_SPEECH_VERBS2`、`_SPEECH_VERBS1`、`_first_quote_pos`、`_strip_speech_tag`、`_is_speech_tag_only`、`plain_dialogue_ratio`、`dialogue_segment_form`、`pooled_dialogue_form`、`cjk_len`、`raw_counts`、`measure_summary`、`_LEAD_DIALOGUE_HEAD`、`_QUOTE_END_INNER`、`_QUOTE_END_OUTER`、`_punctuation_and_paragraph_features`、`_mean_run_length`、`profile_band`、`band_check``（共 26 个） | 指标带判定、纯量读数、对白形态测量 | `features()`、`z_scores()`、`metric_mean_abs_z()`、`style_distance()`、`dialogue_profile()`、`pick_bucket()`、`PROFILE_VERSION=2` | **benign**：等价且更细 |
| 15 | `frontend/src/views/ProjectWorkspace.vue` | `runGenerateOutline`、`runClarifyOutline`、`runApplyOutline`、`runReviseBudget`、`saveBg`、`runBatchCards`、`savePowerSystem`、`generateTiers`、`saveCultivation`、`savePersona`、`checkStyleConflicts`、`repairStyle`、`runSelectedExport`、`runExtendPlan`、`saveRoleplayCutoffs`（共 223 个 main 独有标识） | 大纲向导、背景/角色卡/战力/修炼/个性面板、文风冲突检查与修复、导出、计划外扩、角色扮演截断边界 | 同名文件已改为 assistant-wizard 工作台（`/assistant/wizard/*`），无上述面板 | **benign**：整体能力缺失、非正确性；但不能当作“无独有符号”静默丢掉——建议按第 7/14 批拆面板回填 |
| 16 | `backend/app/agents/audit.py` | `rule_corpus_foreign_blocks`、`_fitted_novel_ids`、`run_narrative_audit` | 跨书污染审计、判断语料是否已被别的项目拟合、蒸馏叙事级审计 | 5 条语料规则（空/短、重复块、注入串、缺文风档案、缺向量）；`corpus_health` 在入库侧清洗；无 foreign 审计、无叙事审计 | **risk**：已入库的跨书污染不会再被审计发现；叙事审计随第 4 批 |
| 17 | `backend/app/generation/branch.py` | `roll_branch`、`attach_branch_to_state`（同名实现差异） | 分支掷骰与状态挂载 | 同名函数 + `chapter_based` 关键字 | **benign**：feat 超集 |
| 18 | `backend/app/generation/power_scale.py` | `SCALE_FIELDS`（同名实现差异） | 量级字段表 | 同名常量，feat +69 行 | **benign**：feat 超集 |
| 19 | `backend/app/generation/timeline.py` | `with_authority`、`_authority_block`、`AUTHORITY_MARK`、`_AUTHORITY_RULE`、`_AUTHORITY_NOTE` | 把“权威约束（不得违反）”边界块注入提示，禁止权威外的物件/地点/人物 | 无（`git grep -i authority` 仅 `kg/ledger.py` 的来源权重） | **risk**：生成忠实性边界丢失 |
| 20 | `backend/app/ingestion/clean.py` | `clean_consumption_text` | 消费前二次清洗：只删精确区间，保留其余字符与换行 | `clean_novel_text()`（入库时：水印→跨书→重复段→段级广告，`store.py` 调用） | **benign**：清洗前移到入库，覆盖更强 |
| 21 | `backend/app/ingestion/seed.py` | `run_seed`（同名实现差异） | 种子数据写入 | 同名函数，feat 改写 | **benign**：无独有能力 |
| 22 | `backend/app/ingestion/store.py` | `ingest_text(..., drop_foreign_blocks=False)`（`:49/:71`）+ `status="imported_review"` 分支（`:92`，经 `invoke_tool("preview_ingest_clean")`/`("check_ingest_clean")` 判定，`:85-86`） | 入库前检测并显式剔除跨书整章；经清洗复核决定是否置 `imported_review` 待人工确认 | feat 版只有 `clean_novel_text` + `parse_txt`，固定 `status="imported"` | **risk**：污染稿/HTML 错误页可直接入库，少了 422 拦截与 `imported_review` 待复核状态 |
| 23 | `backend/app/kg/bible.py` | `detect_conflicts`、`apply_bible_revision`（同名实现差异） | 设定冲突检测与修订应用 | feat 版同名函数，+328 行 | **benign**：feat 更强 |
| 24 | `backend/app/kg/judge.py` | `_payoff_of` + `check_foreshadow_expiry` 主版对 `planned_payoff_chapter` 的净化 | 回收章说不通（0 / 早于埋下章 / 正文无证据）就不报“已超期” | 直接用 `planned_payoff_chapter` 比较（无净化） | **risk**：会把计划第 0 章/无效章报成“超期”，main 实测这类噪音 84 条 |
| 25 | `backend/app/kg/retrieval_v3.py` | `_compact_context`、`_trim_leading_clause`、`_squash` 等 | 检索上下文压缩与截断 | feat 自己的上下文拼装（`retrieval/*`） | **benign**：实现差异 |
| 26 | `backend/app/llm/reasoning.py` | `ThinkTagStripper` 主版（无 `dropped_chars`） | 剥离 think 块 | feat 版增加 `dropped_chars` 计数 | **benign**：feat 超集 |
| 27 | `backend/app/routers/kg_v3.py` | `get_slice`、`get_slice_prompt`、`get_events`、`list_relations`、`requeue_worker`、`ExemptRequest`（同名实现差异） | KG 切片/事件/关系查询与重排 | feat 版同名端点改写 | **benign**：无独有能力 |
| 28 | `backend/app/style/jobs.py` | `find_active_book_run`、`run_book_digest_job`、`_digest_entry_count` | 整书摘要任务：并发去重、任务执行、条目计数 | 无 BookDigest（已排第 9 批） | **benign**：能力缺口非正确性；第 9 批搬 `BookDigest` 时必须连带此任务 |
| 29 | `backend/app/style/pipeline.py` | `profile_fingerprint`（去掉 `measurement`/`spec`）、`build_reference_profile`（去掉 `chapter_stats`/`spec`）；另 `PROFILE_MAX_JSON_BYTES`、`project_profile` 实现差异 | 档案指纹含测量尺/规格书版本；档案带章节统计与规格书 | `PROFILE_VERSION=2` + 仅特性版本 | **risk**：尺子（`MEASUREMENT_VERSION`）或规格书（`SPEC_VERSION`）变更后旧档案不会失效，判定会拿旧尺子量新文本（main 记录过 56.0 vs 39.2、40% 偏差） |
| 30 | `frontend/src/utils/assistantDisplay.ts` | `ReactNotice`、`reactNotice`、`bodyRepairVerificationText`、`issueTypeLabel` | 展示 ReAct 提示、正文修复验证结论、问题类型标签 | 无 | **benign**：随 `body_repair` 第 4 批一起回填 |
| 31 | `frontend/src/views/CorpusView.vue` | `reportOf`、`foreignOf`、`loadDigest`、`pollDigest`、`startDigest`、`checkLabel`、`stopDigestPolling` | 跨书污染报告展示、整书摘要任务入口与轮询 | feat 语料页无这两块 | **benign**：能力缺口，随第 4/9 批 |
| 32 | `frontend/src/views/KgWorkspace.vue` | `runStatusText` | 批抽运行状态文案 | 无 | **benign**：文案 |
| 33 | `frontend/src/views/ProjectsView.vue` | `segmentTotal`、`error`、`openProject` | 项目卡片段数统计与错误态 | feat 版重写项目卡 | **benign**：展示差异 |
| 34 | `tests/test_assistant_agent_tools.py` | `test_qa_over_http_pending_actions_present`、`test_card_fields_complete_and_endpoint_derived_from_kind`（同名用例改写） | 测 pending_actions 卡片与工具字段 | feat 同名测试改写 | **benign**：测试组织 |
| 35 | `tests/test_assistant_audit.py` | `test_audit_corpus_does_not_report_a_corpus_a_project_already_fitted` + 4 个“何时调模型”用例改写 | 测跨书拟合误报、审计模型调用条件 | feat 同名测试改写（无跨书拟合用例） | **benign**：测试；但跨书拟合断言随 `_fitted_novel_ids` 一起消失 |
| 36 | `tests/test_style_tier.py` | `CountingProvider.async_chat`、`_FILLER` + 11 个分层/采样用例改写 | 测三档蒸馏的采样轴、调用数、成本提示 | feat `settings` 三档端点（第 10 批） | **benign**：测试 |
| 37 | `backend/app/generation/power_system_type.py` | 无 main 独有符号（同名实现差异） | 体系类型判定 | feat +41 行超集 | **benign** |
| 38 | `backend/app/ingestion/encoding.py` | 仅文档示例书名（`《长夜司》/《焚天录》/《豪门夜宴》` → `《大奉打更人》/《斗破苍穹》/《豪门宠婚…》`） | 编码探测示例 | 同名实现 | **benign**：文档差异 |
| 39 | `backend/app/kg/queries.py` | `profile` 的 `appearances=list(range(first,last+1))` | 用首末章编造“连续出场列表” | feat 删除该字段并写明理由（`KgNode` 只存首末章） | **benign**：feat 更强（删掉伪造字段） |
| 40 | `backend/app/retrieval/bm25.py` | 仅 docstring 分词说明 | 关键词检索说明 | 同名实现 | **benign**：文档差异 |
| 41 | `frontend/src/components/AssistantIssueList.vue` | 无独有符号 | 问题清单展示 | feat 同名组件 | **benign** |
| 42 | `frontend/src/components/DistillProgress.vue` | `running`、`capped`（主版进度分母=阶段数） | 蒸馏进度条 | feat `style/jobs.py` 分母=块数 | **benign**：取 feat 正确（主版会把进度算错） |
| 43 | `frontend/src/views/AssistantResolver.vue` | 无独有符号 | 助手解析页 | feat 同名页 | **benign** |
| 44 | `frontend/src/views/MyNovels.vue` | 无独有符号 | 我的小说页 | feat 同名页 | **benign** |
| 45 | `tests/test_assistant_corpus_clean.py` | 仅 provider stub 差异（`_BoomProvider`、`_JsonProvider`） | 清洗复核测试 | feat 同名测试 | **benign**：测试组织 |
| 46 | `tests/test_assistant_readonly.py` | `test_agent_run_and_evidence_persisted` 中 `AgentRun.trace_json` 断言 | 断言工具轨迹持久化 | feat 用 `Evidence`/`RetrievalLog` | **benign**：测试；但少了“轨迹可回放”的回归 |
| 47 | `tests/test_ingestion_encoding.py` | `_CN`、`test_utf8_sig_with_bom` | 编码探测常量与 BOM 用例 | feat 同名测试 | **benign**：测试组织 |
| 48 | `AgentRun.trace_json`（列） | `trace_json` | 把 ReAct 工具轨迹存进 `agent_runs` | `Evidence`（`models.py:766`）+ `RetrievalLog` | **benign**：可观测性仍有等价载体；与 main 的 ReAct 循环同批放弃 |
| 49 | `tests/test_assistant_react_loop.py` | `run_react` 全链路用例（停滞、净化、参数裁剪、预算） | 锁 ReAct 循环鲁棒性 | 无 | **benign**：测试；风险已记在第 1 行 |

**汇总：`risk` 11 条** —— 第 1 行 `react_loop.py`、第 2 行 `registry.py`、第 3 行 `engine.py`、第 6 行 `batch_worker.py`、第 8 行 `extractor_v3.py`、第 12 行 `routers/generation.py`、第 16 行 `agents/audit.py`、第 19 行 `timeline.py`、第 22 行 `store.py`、第 24 行 `kg/judge.py`、第 29 行 `style/pipeline.py`。其余 38 条 `benign`。

## 三、据此要动的台账

1. `POST .../generate-outline`、`.../outline/apply`、`.../outline/clarify`、`.../outline/revise-budget`（台账 349–352）：`planned（第7批）` → `dropped`，理由“经验证无调用方且无测试（feat 前端零调用、feat 测试零覆盖；main 侧调用方 `ProjectWorkspace.vue:166/201/218/2304` 已随该文件取 feat）”。
2. `tests/test_copy_gate.py`（415）、`tests/test_planning_budget_revision.py`（468）、`tests/test_planning_clarification_api.py`（470）：只打这 4 个端点，随端点 `dropped`。
3. `tests/test_foundation_planning.py`（448）、`test_planning_retry.py`（473）、`test_plot_structure.py`（475）、`test_structure_copy_check.py`（501）：保留 `planned`，但要把 outline 用例摘掉（其余是 `build-bible`/库级单测）。
4. 11 条 `risk` 建议改判 `planned`（按依赖批次：registry/engine/audit 第 4–5 批，store 第 6 批，generation router 第 4/7 批，timeline 第 7 批，batch_worker/extractor/judge 第 12 批，pipeline 第 3/9 批，react_loop 第 4 批）。**不能按现状静默 dropped**。
