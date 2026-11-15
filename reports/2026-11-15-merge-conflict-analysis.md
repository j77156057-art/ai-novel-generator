# 2026-11-15 合流冲突分析（只读评估，未提交）

## 0. 基线与方法

| 项 | 值 |
|---|---|
| 对比基准 A | `origin/main` = `cb05a45`（2026-11-02，134 个提交，根提交 `a31a704`） |
| 对比基准 B | `origin/feat/resume-push-2026-10` = `989abce`（2026-11-15，264 个提交，根提交 `817a6df`） |
| 共同祖先 | **无**（`merge-base --is-ancestor` 两侧都为真，即互不为祖先；`merge-base` 为空） |
| 操作 | 临时 worktree 检出 `origin/main`，执行 `git merge --no-commit --no-ff --allow-unrelated-histories feat/resume-push-2026-10`，只收结果不提交 |
| 冲突文件 | **125** |
| 善后 | 评估完 `git merge --abort`，`git worktree remove --force`，主工作区未改动 |

分桶口径（按用户定义）：

- **A 两代实现**：同一功能两边各写一套，只能选一边。
- **B 必须并存**：互补功能，需手工合并。
- **C 机械合并**：锁文件、格式化、配置项增减，可按规则合成。
- **D 形式差异**：改名、移动、纯样式，可自动。

> 说明：下文"两侧差异"列只列**代表符号**（每个文件取 main 侧第一个独有符号、feat 侧第一个独有符号），完整差异以同目录 CSV 为准；"建议取舍"里的"取main/取feat"指整份取该侧（`git checkout --ours/--theirs`），不需要逐 hunk 编辑。

## 1. 总量

| 项 | 数量 |
|---|---|
| 冲突文件 | 125 |
| A 两代实现 | 67 |
| B 必须并存 | 35 |
| C 机械合并 | 9 |
| D 形式差异 | 14 |
| 风险高 | 25 |
| 风险中 | 54 |
| 风险低 | 46 |

## 2. 逐文件清单（125 行，按桶分组、桶内高风险在前）

| # | 文件 | 桶 | 两侧差异 | 建议取舍 | 风险 |
|---|---|---|---|---|---|
| 1 | `backend/app/agents/registry.py` | A | main 独有 `_impl_audit_text_quality` 等 16 个；feat 独有 `_empty_parameter_schema` 等 15 个；31 处冲突 | 取feat | 高 |
| 2 | `backend/app/generation/engine.py` | A | main 独有 `_reference_chapter_hint` 等 46 个；feat 独有 `_rewrite_growth_allowance` 等 99 个；75 处冲突，3924 → 6054 行 | 取feat | 高 |
| 3 | `backend/app/generation/prompts.py` | A | main 独有 `_override_block` 等 8 个；feat 独有 `render_item_holding_section` 等 23 个；28 处冲突 | 取feat | 高 |
| 4 | `backend/app/ingestion/parse_txt.py` | A | feat 侧独有 `_error_page_reason` 等 11 个；6 处冲突（main 109 行 / feat 454 行） | 取feat | 高 |
| 5 | `backend/app/kg/batch_worker.py` | A | main 独有 `_session_factory` 等 7 个；feat 独有 `queued_segments` 等 65 个；45 处冲突，1005 → 3331 行 | 取feat | 高 |
| 6 | `backend/app/kg/build.py` | A | main 独有 `_segment_pairs` 等 2 个；feat 独有 `_kg_lock_for` 等 6 个；12 处冲突 | 取feat | 高 |
| 7 | `backend/app/kg/extractor_v3.py` | A | main 独有 `known_foreshadow_block`；feat 独有 `_mark_error` 等 9 个；20 处冲突 | 取feat | 高 |
| 8 | `backend/app/kg/sentinel.py` | A | feat 侧独有 `_rule_realm_jump` 等 2 个；25 处冲突（main 1089 行 / feat 1204 行） | 取feat | 高 |
| 9 | `backend/app/routers/assistant.py` | A | main 独有 `_excerpt` 等 4 个；feat 独有 `_clip_with_mark` 等 13 个；11 处冲突 | 取feat | 高 |
| 10 | `backend/app/routers/assistant_generation.py` | A | feat 侧独有 `_pattern_examples` 等 13 个；17 处冲突（main 322 行 / feat 1174 行） | 取feat | 高 |
| 11 | `backend/app/routers/generation.py` | A | main 侧独有 `_generate_guarded` 等 5 个；21 处冲突（main 543 行 / feat 385 行） | 取feat（与 engine 配对） | 高 |
| 12 | `backend/app/routers/wizard.py` | A | main 侧独有 `_parse` 等 14 个；11 处冲突（main 755 行 / feat 322 行） | 取feat（main 版依赖 `generation/planning.py`） | 高 |
| 13 | `backend/app/style/narrative.py` | A | main 独有 `_chapters_per_block` 等 34 个；feat 独有 `tier_with_sampling` 等 126 个；156 处冲突，5850 → 7788 行 | 取feat | 高 |
| 14 | `backend/app/style/stylometry.py` | A | main 独有 `paragraph_spans` 等 18 个；feat 独有 `split_sentences` 等 12 个；35 处冲突，1603 → 1136 行 | 取feat | 高 |
| 15 | `frontend/src/views/AssistantWorkspace.vue` | A | 两侧各自改写，53 处冲突，1813 → 2051 行 | 取feat + 回移 main 的执行历史/回滚面板 | 高 |
| 16 | `frontend/src/views/ProjectWorkspace.vue` | A | 两侧各自改写，94 处冲突，5749 → 4963 行 | 取feat | 高 |
| 17 | `backend/app/agents/audit.py` | A | main 独有 `rule_corpus_foreign_blocks` 等 5 个；feat 独有 `_bible_name_surface_forms` 等 3 个；12 处冲突 | 取feat | 中 |
| 18 | `backend/app/agents/diagnosis.py` | A | main 侧独有 `rule_style_sentence_length_deviation` 等 5 个；4 处冲突（main 1256 行 / feat 829 行） | 取main（严格超集） | 中 |
| 19 | `backend/app/agents/pending_actions.py` | A | main 侧独有 `collect_text_edit_cards` 等 3 个；7 处冲突（main 476 行 / feat 345 行） | 取main（超集） | 中 |
| 20 | `backend/app/agents/proposals.py` | A | main 侧独有 `repair_write_allowed` 等 2 个；12 处冲突（main 1496 行 / feat 1353 行） | 取main（超集） | 中 |
| 21 | `backend/app/corpus_health.py` | A | main 侧独有 `_foreign_chapter_spans` 等 4 个；26 处冲突（main 1036 行 / feat 669 行） | 取main（超集） | 中 |
| 22 | `backend/app/generation/branch.py` | A | 两侧各自改写，6 处冲突，349 → 367 行 | 取feat | 中 |
| 23 | `backend/app/generation/power_scale.py` | A | feat 侧独有 `_load_15` 等 3 个；3 处冲突（main 255 行 / feat 323 行） | 取feat | 中 |
| 24 | `backend/app/generation/timeline.py` | A | main 独有 `_authority_block` 等 2 个；feat 独有 `_balanced_chapter_ranges` 等 6 个；28 处冲突 | 取feat | 中 |
| 25 | `backend/app/ingestion/chunk.py` | A | main 侧独有 `_is_balanced` 等 4 个；2 处冲突（main 217 行 / feat 20 行） | 取main（超集） | 中 |
| 26 | `backend/app/ingestion/clean.py` | A | main 独有 `is_site_marker_line` 等 3 个；feat 独有 `_cjk_len` 等 7 个；9 处冲突 | 取feat | 中 |
| 27 | `backend/app/ingestion/seed.py` | A | feat 侧独有 `resolve_seed_dir`；2 处冲突（main 30 行 / feat 49 行） | 取feat | 中 |
| 28 | `backend/app/ingestion/store.py` | A | 两侧各自改写，8 处冲突，172 → 163 行 | 取feat | 中 |
| 29 | `backend/app/kg/bible.py` | A | feat 侧独有 `_first_text` 等 11 个；9 处冲突（main 261 行 / feat 580 行） | 取feat | 中 |
| 30 | `backend/app/kg/extractor.py` | A | main 侧独有 `_relaxed_surname_lead` 等 4 个；8 处冲突（main 1031 行 / feat 981 行） | 取main（超集） | 中 |
| 31 | `backend/app/kg/judge.py` | A | main 独有 `_payoff_of`；feat 独有 `_book_cond`；16 处冲突 | 取feat | 中 |
| 32 | `backend/app/kg/ledger.py` | A | main 侧独有 `rebuild_facts`；11 处冲突（main 679 行 / feat 594 行） | 取main（超集） | 中 |
| 33 | `backend/app/kg/retrieval_v3.py` | A | main 独有 `_payoff_of` 等 5 个；feat 独有 `_in_scope` 等 2 个；24 处冲突 | 取feat | 中 |
| 34 | `backend/app/llm/reasoning.py` | A | feat 侧独有 `dropped_chars`；5 处冲突（main 134 行 / feat 146 行） | 取feat | 中 |
| 35 | `backend/app/retrieval/search.py` | A | main 侧独有 `weighted_search`；4 处冲突（main 162 行 / feat 88 行） | 取feat（main 版依赖 feat 没有的 `retrieval/views.py`） | 中 |
| 36 | `backend/app/routers/assistant_proposals.py` | A | main 侧独有 `BodyRepairRequest` 等 6 个；4 处冲突（main 774 行 / feat 671 行） | 取main（超集） | 中 |
| 37 | `backend/app/routers/kg_v3.py` | A | feat 侧独有 `/worldview`、`/timeline` 等 12 个端点/函数；10 处冲突（main 373 行 / feat 484 行） | 取feat | 中 |
| 38 | `backend/app/routers/novels.py` | A | main 侧独有 `_upload_legacy`；3 处冲突（main 100 行 / feat 69 行） | 取main（超集） | 中 |
| 39 | `backend/app/routers/settings.py` | A | main 侧独有 `thinking_control_for`、`/generation-tiers` 等 7 个；11 处冲突（main 964 行 / feat 778 行） | 取main（超集） | 中 |
| 40 | `backend/app/style/exemplar.py` | A | main 侧独有 `_mature_pool` 等 3 个；13 处冲突（main 371 行 / feat 172 行） | 取main（超集） | 中 |
| 41 | `backend/app/style/jobs.py` | A | main 独有 `find_active_book_run` 等 3 个；feat 独有 `_aware` 等 11 个；20 处冲突 | 取feat | 中 |
| 42 | `backend/app/style/pipeline.py` | A | 两侧各自改写，9 处冲突，298 → 237 行 | 取feat | 中 |
| 43 | `backend/app/tokenize.py` | A | main 侧独有 `_chinese_tokens`；2 处冲突（main 30 行 / feat 23 行） | 取main（超集） | 中 |
| 44 | `frontend/src/components/RoleModelPanel.vue` | A | 两侧各自改写，9 处冲突，644 → 545 行 | 取main | 中 |
| 45 | `frontend/src/utils/assistantDisplay.ts` | A | 两侧各自改写，3 处冲突，222 → 93 行 | 取feat | 中 |
| 46 | `frontend/src/views/CorpusView.vue` | A | 两侧各自改写，8 处冲突，617 → 780 行 | 取feat | 中 |
| 47 | `frontend/src/views/KgWorkspace.vue` | A | 两侧各自改写，10 处冲突，1090 → 1074 行 | 取feat | 中 |
| 48 | `frontend/src/views/LlmSettings.vue` | A | 两侧各自改写，7 处冲突，260 → 177 行 | 取main | 中 |
| 49 | `frontend/src/views/ProjectsView.vue` | A | 两侧各自改写，6 处冲突，199 → 293 行 | 取feat | 中 |
| 50 | `tests/test_assistant_agent_tools.py` | A | feat 侧独有 `_ScriptedProvider` 等 17 个；3 处冲突（main 686 行 / feat 954 行） | 取feat | 中 |
| 51 | `tests/test_assistant_audit.py` | A | main 侧独有 `test_audit_corpus_does_not_report_a_corpus_a_project_already_fitted`；6 处冲突（main 1482 行 / feat 1460 行） | 取feat | 中 |
| 52 | `tests/test_assistant_proposals.py` | A | main 独有 `test_restore_expected_hash_guards_drift`；feat 独有 `_iter_route_paths`；4 处冲突 | 取main | 中 |
| 53 | `tests/test_settings_roles.py` | A | 两侧各自改写，3 处冲突，820 → 812 行 | 取main（settings 超集） | 中 |
| 54 | `tests/test_style_tier.py` | A | main 侧独有 `async_chat`；20 处冲突（main 904 行 / feat 865 行） | 取feat | 中 |
| 55 | `backend/app/agents/log_audit.py` | A | main 侧独有 `_react_failure_reason`；6 处冲突（main 606 行 / feat 568 行） | 取main（超集） | 低 |
| 56 | `backend/app/generation/power_system_type.py` | A | feat 侧独有 `system_type_from_bible`；1 处冲突（main 125 行 / feat 166 行） | 取feat | 低 |
| 57 | `backend/app/ingestion/encoding.py` | A | 两侧各自改写，1 处冲突，174 → 174 行 | 取feat | 低 |
| 58 | `backend/app/kg/queries.py` | A | 两侧各自改写，2 处冲突，148 → 154 行 | 取feat（等价） | 低 |
| 59 | `backend/app/retrieval/bm25.py` | A | 两侧各自改写，1 处冲突，41 → 41 行 | 取feat（等价） | 低 |
| 60 | `frontend/src/components/AssistantIssueList.vue` | A | 两侧各自改写，5 处冲突，251 → 242 行 | 取feat | 低 |
| 61 | `frontend/src/components/DistillProgress.vue` | A | 两侧各自改写，8 处冲突，317 → 301 行 | 取main | 低 |
| 62 | `frontend/src/views/AssistantResolver.vue` | A | 两侧各自改写，4 处冲突，60 → 94 行 | 取feat | 低 |
| 63 | `frontend/src/views/MyNovels.vue` | A | 两侧各自改写，2 处冲突，134 → 239 行 | 取feat | 低 |
| 64 | `tests/test_assistant_corpus_clean.py` | A | 两侧各自改写，2 处冲突，658 → 658 行 | 取feat | 低 |
| 65 | `tests/test_assistant_diagnose.py` | A | main 独有 `test_all_rules_are_registered`；feat 独有 `test_all_four_rules_are_registered`；5 处冲突 | 取main（规则超集） | 低 |
| 66 | `tests/test_assistant_readonly.py` | A | 两侧各自改写，2 处冲突，391 → 363 行 | 取feat | 低 |
| 67 | `tests/test_ingestion_encoding.py` | A | 两侧各自改写，2 处冲突，120 → 120 行 | 取feat | 低 |
| 68 | `backend/app/db.py` | B | main 独有 `_backfill_product_keys`（14 项补列）；feat 独有 `_sqlite_basename`、`is_protected_db` 等 3 个（79 项补列）；11 处冲突 | 先 models 后 db；补列清单取并集 | 高 |
| 69 | `backend/app/llm/provider.py` | B | main 独有 `variant`、`_thinking_kwargs`；feat 独有 `_RateGuard`、`open`、`feed`；49 处冲突，1441 → 1529 行 | 手工合成：variant + RateGuard | 高 |
| 70 | `backend/app/main.py` | B | feat 侧独有 `healthz`；6 处冲突（main 136 行 / feat 141 行） | 并集 + 按链门控导入 | 高 |
| 71 | `backend/app/models.py` | B | main 独有 `WorkflowRun`、`ActionProposal`、`BookDigest` 等 5 张表；feat 独有 `KgIngestQuarantine`、`GenerationSnapshot` 等 14 张表；23 处冲突 | 并集；同名表只留一份 | 高 |
| 72 | `backend/app/routers/projects.py` | B | main 独有 `DistillScopeSpec`、`PlanExtensionRequest` 等 88 个；feat 独有 `/distill/resume` 等 16 个；30 处冲突，2199 → 906 行 | 手工合并：main 主体 + feat resume | 高 |
| 73 | `backend/app/schemas.py` | B | 两侧各自改写，10 处冲突，187 → 165 行 | 并集 | 高 |
| 74 | `frontend/src/api/client.ts` | B | 两侧各自改写，25 处冲突，1275 → 880 行 | 并集（按端点分组） | 高 |
| 75 | `frontend/src/api/types.ts` | B | 两侧各自改写，36 处冲突，2109 → 2016 行 | 并集 | 高 |
| 76 | `tests/conftest.py` | B | main 独有 `experimental_consent` 夹具；feat 独有 `_db_logical_fingerprint` 夹具；8 处冲突 | 并集（两套夹具） | 高 |
| 77 | `backend/app/agents/prompt_overrides.py` | B | main 侧独有 `resolve_overrides_for_generation`（纯追加）；1 处冲突（main 276 行 / feat 241 行） | 并集：feat 主体 + main 批量函数 | 中 |
| 78 | `backend/test_m4_api.py` | B | 两侧各自改写，3 处冲突，327 → 367 行 | 跟随实现 | 中 |
| 79 | `backend/test_p0_regression.py` | B | feat 侧独有 `complete_message` 等 2 个；9 处冲突（main 482 行 / feat 528 行） | 跟随实现 | 中 |
| 80 | `backend/verify_acceptance.py` | B | feat 侧独有 `_clean`、`grams`、`jaccard` 等 6 个；11 处冲突（main 614 行 / feat 697 行） | 跟随实现 | 中 |
| 81 | `backend/verify_corpus_health.py` | B | 两侧各自改写，15 处冲突，406 → 406 行 | 跟随实现 | 中 |
| 82 | `backend/verify_sentinel.py` | B | 两侧各自改写，25 处冲突，919 → 951 行 | 跟随实现 | 中 |
| 83 | `frontend/src/router/index.ts` | B | 两侧各自改写，3 处冲突，88 → 67 行 | 并集 | 中 |
| 84 | `tests/test_db_isolation.py` | B | main 独有 `test_child_process_does_not_reuse_this_sessions_db`；feat 独有 `test_logical_fingerprint_ignores_only_heartbeat_rows`；5 处冲突 | 并集 | 中 |
| 85 | `tests/test_distill_jobs.py` | B | feat 侧独有 2 个用例；14 处冲突（main 609 行 / feat 718 行） | 跟随 distill 实现 | 中 |
| 86 | `tests/test_prompt_override_wiring.py` | B | main 独有 `_sha` 等 16 个；feat 独有 `_disable_guards`、`RecordingProvider` 等 16 个；3 处冲突 | 并集（两套夹具） | 中 |
| 87 | `tests/test_schema_migration_completeness.py` | B | 两侧各自改写，3 处冲突，121 → 124 行 | 并集（必须覆盖并集补列清单） | 中 |
| 88 | `backend/load_corpus.py` | B | 两侧各自改写，6 处冲突，227 → 210 行 | 并集 | 低 |
| 89 | `backend/test_extractor_v2.py` | B | 两侧各自改写，1 处冲突，61 → 61 行 | 跟随实现 | 低 |
| 90 | `backend/test_m3.py` | B | 两侧各自改写，1 处冲突，146 → 152 行 | 跟随实现 | 低 |
| 91 | `backend/test_m3_api.py` | B | 两侧各自改写，1 处冲突，114 → 120 行 | 跟随实现 | 低 |
| 92 | `backend/test_summary_tree.py` | B | 两侧各自改写，2 处冲突，649 → 649 行 | 跟随实现 | 低 |
| 93 | `backend/verify_style_pipeline.py` | B | 两侧各自改写，4 处冲突，270 → 266 行 | 跟随实现 | 低 |
| 94 | `backend/verify_stylometry.py` | B | main 侧独有 `_pad`；4 处冲突（main 336 行 / feat 296 行） | 跟随实现 | 低 |
| 95 | `tests/concurrent_assistant_isolation.py` | B | 两侧各自改写，2 处冲突，189 → 219 行 | 跟随实现 | 低 |
| 96 | `tests/concurrent_assistant_qa.py` | B | 两侧各自改写，2 处冲突，185 → 215 行 | 跟随实现 | 低 |
| 97 | `tests/concurrent_router_e2e.py` | B | 两侧各自改写，2 处冲突，169 → 199 行 | 跟随实现 | 低 |
| 98 | `tests/test_assistant_provider_config.py` | B | main 独有 `fake_chat`；feat 独有 `fake_complete_message`；6 处冲突 | 跟随 provider 实现 | 低 |
| 99 | `tests/test_finish_reason_truncation.py` | B | feat 侧独有 `read1`；3 处冲突（main 170 行 / feat 176 行） | 跟随 provider | 低 |
| 100 | `tests/test_provider_timeouts.py` | B | feat 侧独有 `read1`；8 处冲突（main 123 行 / feat 175 行） | 跟随 provider | 低 |
| 101 | `tests/test_reasoning_wiring.py` | B | feat 侧独有 `read1`；2 处冲突（main 150 行 / feat 155 行） | 跟随 reasoning | 低 |
| 102 | `tests/test_watermark_detection.py` | B | 两侧各自改写，7 处冲突，124 → 124 行 | 跟随实现 | 低 |
| 103 | `backend/app/config.py` | C | main 侧独有 `opencode_go_model_list`；14 处冲突（main 513 行 / feat 573 行）；键集 76 vs 110，交集 53，**无同名不同义冲突** | 并集（键集无交集） | 中 |
| 104 | `backend/config/providers.yaml` | C | 两侧各自改写，4 处冲突，73 → 100 行 | 并集 | 中 |
| 105 | `backend/requirements.txt` | C | 两侧各自改写，1 处冲突，22 → 21 行 | 并集 | 中 |
| 106 | `frontend/package.json` | C | 两侧各自改写，2 处冲突，30 → 24 行 | 并集 | 中 |
| 107 | `frontend/package-lock.json` | C | 两侧各自改写，41 处冲突，2892 → 1601 行 | 删掉锁文件后按合并后的 `package.json` 重生成 | 中 |
| 108 | `.gitignore` | C | 两侧各自改写，3 处冲突，47 → 111 行 | 并集 | 低 |
| 109 | `backend/.env.example` | C | 两侧各自改写，3 处冲突，67 → 52 行 | 并集 | 低 |
| 110 | `frontend/tsconfig.json` | C | 两侧各自改写，1 处冲突，20 → 20 行 | 取并集 | 低 |
| 111 | `pytest.ini` | C | 两侧各自改写，1 处冲突，19 → 13 行 | 并集 | 低 |
| 112 | `backend/ingest_web_novels.py` | D | 两侧各自改写，2 处冲突，97 → 96 行 | 取并集 | 低 |
| 113 | `data/seed/README-投放真实语料.md` | D | 说明文档，两侧只有排版差异（该文件路径含中文，hunk 统计未采到，记 0） | 取并集 | 低 |
| 114 | `docs/accept_percentile_as_ui_meter.md` | D | 两侧各自改写，1 处冲突，116 → 117 行 | 取并集 | 低 |
| 115 | `docs/architecture/agent-expansion-architecture.md` | D | 两侧各自改写，1 处冲突，62 → 68 行 | 取并集 | 低 |
| 116 | `docs/kg-e2e-journey-design.md` | D | 两侧各自改写，3 处冲突，148 → 146 行 | 取并集 | 低 |
| 117 | `docs/kg-judge-era-dimension-design.md` | D | 两侧各自改写，1 处冲突，174 → 174 行 | 取并集 | 低 |
| 118 | `docs/kg-nemotron-compare-redirect.md` | D | 两侧各自改写，1 处冲突，93 → 93 行 | 取并集 | 低 |
| 119 | `docs/percentile_root_cause_analysis.md` | D | 两侧各自改写，1 处冲突，46 → 42 行 | 取并集 | 低 |
| 120 | `docs/style_log_buckets_design.md` | D | 两侧各自改写，2 处冲突，261 → 255 行 | 取并集 | 低 |
| 121 | `docs/style-distillation-tiers.md` | D | 两侧各自改写，4 处冲突，146 → 146 行 | 取并集 | 低 |
| 122 | `frontend/src/App.vue` | D | 两侧各自改写，5 处冲突，74 → 52 行 | 取并集 | 低 |
| 123 | `frontend/src/style/base.css` | D | 两侧各自改写，23 处冲突，498 → 605 行 | 按最终设计稿定 | 低 |
| 124 | `frontend/src/style/tokens.css` | D | 两侧各自改写，5 处冲突，171 → 190 行 | 按最终设计稿定 | 低 |
| 125 | `README.md` | D | 两侧各自改写，2 处冲突，363 → 212 行 | 取并集 | 低 |

### 2.1 A 桶中"取feat"会丢掉什么（被放弃那版的独有能力）

按用户要求逐条说明，只列**会被丢弃且另一版没有**的代表项：

| 文件 | 取feat 会丢弃的 main 侧能力 |
|---|---|
| `agents/registry.py` | 16 个 main 独有工具：文本质量审计/修复、工作流状态、提案工单、角色模板、附件读取等 |
| `generation/engine.py` | `_reference_chapter_hint`、`_roleplay_character_name`、`_dedup_before_commit` 等 46 个（含 roleplay 与去重） |
| `generation/prompts.py` | `_override_block`、`_character_constraint_line`、`_description_guides` 等 8 个提示词块 |
| `kg/batch_worker.py` | 单进程批处理 `_session_factory`、`_mark_batch_failed`、`reap_stale_runs`（feat 改成了队列/断点/限流） |
| `kg/build.py` | `_segment_pairs`、`rebuild_generated_graph`（feat 用 `merge_segment_into_kg` + 锁） |
| `kg/extractor_v3.py` | `known_foreshadow_block`（伏笔块识别） |
| `routers/assistant.py` | `_excerpt`、`_evidence_rows_from_tool_results`、附件/探针相关入口 |
| `routers/generation.py` | `_generate_guarded`、`_verbatim_conflict`、`_require_workflow_ready`（工作流闸门） |
| `routers/wizard.py` | 14 个入口（含 `_parse_bible`、`_fill_missing_personas`）；但 main 版 import `generation/planning.py`，feat 没有该文件，**不能取** |
| `style/narrative.py` | `_chapters_per_block`、`_clip`、`_build_word_order_prompt` 等 34 个 |
| `style/stylometry.py` | `paragraph_spans`、`dialogue_cjk_count`、`_strip_quoted` 等 18 个（段落级统计口径） |
| `routers/wizard/generation/assistant` 前端 | 执行历史/版本回滚面板（`AssistantWorkspace`）、roleplay 面板、登录与导出（`1383f48`） |

## 3. 三问专答

### 3.1 起不来的点：两套实现同时注册会怎样

**结论：不会"路由重复导致报错"，但会"启动即 ImportError"；ORM 若两份同名表并存会直接抛错。**

**(a) 路由重复 —— 不会**
- `main.py` main 侧 `include_router` 16 次、feat 侧 13 次，其中 11 个模块同名（`novels`、`projects`、`generation`、`kg_v3`、`wizard`、`settings`、`assistant`、`assistant_proposals`、`assistant_audit`、`assistant_generation`、`assistant_clean`）。
- 同名模块在文件树里只能留一份，所以"两套同模块同时注册"在物理上不成立。
- 对两侧全部 router 做 `(method, path)` 归一化后：**跨不同模块文件的重复路径 = 0**（共 234 条路由声明，76 组重复全部是"同一模块的 main 版 vs feat 版"）。也就是说，只要不人为把同名模块改名后同时注册，就不会出现路由遮蔽。

**(b) ORM 表名冲突 —— 会**
- `models.py` 两侧各有一份完整模型：main 30 张表、feat 39 张表、**同名表 25 张**。
- 同名表里有 **12 张列不同**：`projects`、`distill_runs`、`generations`、`kg_character_state`、`kg_entities`、`kg_events`、`kg_mentions`、`kg_relations`、`novel_chunks`、`raw_novels`、`style_distills`、`agent_runs`。
- 其余 13 张列完全一致。
- 具体差异举例：`projects` main 多 `continuation_start_chapter`、`story_authority`、`reference_cutoffs`、`narrative_scope`、`character_template_id`、`style_rewrite`；feat 多 `active_novel_id`、`continuation_novel_id`、`narrative_sample_chapters`。
- 如果把两版同名 class 都留在同一个 `Base` 下，SQLAlchemy 在构建 metadata 时会抛 `InvalidRequestError: Table 'projects' is already defined for this MetaData instance`。**必须每个表名只留一份，列取并集。**
- 注意 `Base.metadata.create_all` 只补**缺失的表**，不补列。所以列取并集后，`db.py` 的补列清单也必须并集，否则老库永远缺列（两侧各自都写了这个注释）。

**(c) 启动时导入失败 —— 会，且有多条硬链**
按"`main.py` 取并集"直接跑，以下 import 在模块顶层执行，任一条缺失即 `ImportError`：

| 链 | 缺失符号 | 存在于 |
|---|---|---|
| `app.main` → `routers.assistant_action_proposals` | `app.models.ActionProposal` | 仅 main |
| `app.main` → `routers.roleplay` | `app.models.RoleplayMessage` | 仅 main |
| `app.main` → `routers.assistant_probe` | `app.llm.capability`、`agents.registry._resolve_probe_target` | 仅 main |
| `app.main` → `routers.assistant_attachments` | `app.agents.attachments` | 仅 main |
| `app.main` → `routers.assistant_wizard` | `app.assistant_store` | 仅 feat |
| `app.main` → `routers.assistant_snapshot` | `app.agents.dialog.snapshot` / `dialog.rollback` | 仅 feat |
| `app.generation.engine`（feat 版） | `app.agents.dialog.snapshot.SnapshotLease` / `fail_snapshot` | 仅 feat |
| `app.agents`（包 `__init__`）→ `agents.registry`（main 版） | 11 个 main 独有 agent 模块 + `llm.capability` | 仅 main |
| `routers.generation`（feat 版）→ `engine._catalog_skeleton` | `_catalog_skeleton` 在 main engine 出现 0 次 | 仅 feat |
| `routers.generation`（main 版）→ `engine._l2_character_names` | `_l2_character_names` 在 feat engine 出现 0 次 | 仅 main |
| `routers.wizard`（main 版） | `generation.planning`（`OutlineRequest` 等） | 仅 main |
| `agents.plan_extension`（main 独有） | `generation.planning` | 仅 main |
| `agents.prompt_audit`（main 独有） | `generation.profiles` | 仅 main |

补充证据：`agents/prompt_overrides.py` 两侧都有 `resolve_prompt_block` / `PromptKey`；main 额外有 `resolve_overrides_for_generation`，调用方是 main 独有的 `agents/prompt_audit.py` 与 main 版 `engine.py` / `routers/generation.py`。feat 侧不调用它，因此它**可以安全丢弃**，但若要挂 main 的 prompt_audit，就必须保留。

### 3.2 两套 generation 与 agents 的关系

**结论：是双向依赖，不是单向；两条链都汇入 `generation/engine.py::generate_next_segment`，但上游入口不同。**

**(a) feat 的 `generation/` → `agents/`（正向）**
- `generation/engine.py:17`：`from app.agents.prompt_overrides import PromptKey, resolve_prompt_block`
- `generation/engine.py:4129`：`from app.agents.registry import _impl_audit_kg`（KG 注入，绕过 LLM 路由）
- `generation/engine.py:5739+`：`from app.agents.dialog.snapshot import SnapshotLease / fail_snapshot`（多处分段调用）
- 即：**feat 的生成链路无法脱离 agents 包独立运行**。

**(b) main 的 `agents/` → `generation/`（反向）**
- `agents/action_proposals.py:415`：`from app.generation.engine import generate_next_segment`
- `agents/plan_extension.py:23-24`：`from app.generation.planning import CausalNode, model_json`；`from app.generation.timeline import PlotNode, build_node_prompt`
- `agents/prompt_audit.py:57-58`：`from app.generation.profiles import normalize_profile, rules_for`；`from app.generation.prompts import _narrative_block, build_system_prompt`；`from app.generation.engine import _style_profile`
- `agents/derived_rebuild.py:87-88`：`generation.summary_tree`、`generation.timeline`
- `agents/diagnostic.py:102`：`generation.timeline`
- 即：**main 的助理工单链也无法脱离 generation 独立运行**。

**(c) 合流后一次生成走哪条**
- 直接生成入口：`POST /api/generate`、`/api/generate-stream`、`/api/generate-plan` → `routers/generation.py` → `engine.generate_next_segment`。
- 助理驱动入口：`POST /api/assistant/invoke-generation` → `routers/assistant_generation.py` → （main 侧）`agents/action_proposals.py` → 同一个 `generate_next_segment`。
- **两条链最终都落到同一个函数**，不存在"两套并行的运行时"。合流后走哪条取决于保留哪份 `routers/generation.py` 与哪份 `engine.py`——这是编译期（选文件时）决定的，不是运行时开关。
- 若按"feat 为主链路"合流：直接生成走 feat 的 `routers/generation.py`；助理驱动走 feat 的 `routers/assistant_generation.py`（1174 行、13 个 feat 独有符号）。main 的 `agents/action_proposals.py` 整份不会进树，"助理→工单→生成"这条 main 链默认是**死的**。
- 若要让 main 那条链同时活着，必须额外提供 `generation/planning.py` 与 `generation/profiles.py`（feat 都没有），并把 main 的 11 个 agent 模块 + `llm/capability.py` 挂上，然后把 main 版 `agents/registry.py` 的工具表合并进 feat 的 registry。工作量远大于"取一边"。

### 3.3 最小可运行合流（≤10 个文件）

前提：**feat 为主链路**（理由：`989abce` 是刚推送的开源线，264 个提交、11-15 最新，1123/1126 测试通过；main 停在 11-02）。其余 67 个 A 文件按第 2 节整份取一侧（48 个 `--theirs`、19 个 `--ours`），**不算"改"**。

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `backend/app/main.py` | 取 feat 的 13 个 `include_router`；把 main 独有的 5 个 router（`digest`/`roleplay`/`assistant_action_proposals`/`assistant_probe`/`assistant_attachments`）放进 `if settings.enable_legacy_routers:` 的**函数内 import**，避免启动即 ImportError |
| 2 | `backend/app/models.py` | feat 的 39 张表原样保留；**只追加** main 独有的 5 张（`workflow_runs`、`action_proposals`、`book_digests`、`character_templates`、`roleplay_messages`）；25 张同名表**不重复定义**，列取并集 |
| 3 | `backend/app/db.py` | 以 feat 的 `_migrate_sqlite_columns`（79 项）为底，追加 main 独有的 14 项补列：`projects.continuation_start_chapter/story_authority/reference_cutoffs/character_template_id/narrative_scope/style_rewrite`、`distill_runs.kind/scope_json/novel_id`、`style_distills.product_key`、`novel_chunks.raw_text`、`raw_novels.source_text/source_chapters`、`agent_runs.trace_json` |
| 4 | `backend/app/config.py` | 键并集：main 独有 23 个 + feat 独有 57 个，交集 53 个且**无同名不同义**，直接合并即可 |
| 5 | `backend/app/schemas.py` | 两侧 `StrictRequest` 同名同义，其余按类名并集；`UploadResult` 两侧都有 |
| 6 | `backend/app/agents/prompt_overrides.py` | 保留 feat 的 `resolve_prompt_block`/`PromptKey`，追加 main 的 `resolve_overrides_for_generation`（纯函数，无冲突） |
| 7 | `backend/app/routers/projects.py` | 以 main 版为主体（88 个独有入口），补回 feat 的 `POST /{project_id}/distill/resume` 与 `_normalize_novel_ids`；若决定不挂 main 的可选模块，则改为取 feat 版 |
| 8 | `frontend/src/api/types.ts` | 两侧类型并集，按领域分段（generation / kg / assistant / wizard / roleplay / digest） |
| 9 | `frontend/src/api/client.ts` | 方法并集，与 types 一一对应；删除指向未挂路由的方法 |
| 10 | `tests/conftest.py` | 夹具并集：保留 main 的 `experimental_consent` 与 feat 的 `_db_logical_fingerprint`，并保留 feat 的 DB 隔离夹具 |

> 不需要改但必须一起进树的新增文件：`agents/dialog/snapshot.py`、`agents/dialog/rollback.py`、`assistant_store.py`、`retrieval/tools.py`、`retrieval/trace.py`、`agents/action_catalog.py`、`agents/inspection.py`、`agents/ai_flavor_memory.py`（均为 feat 独有、无冲突）。
>
> 若第 1 步把 `enable_legacy_routers` 打开，则必须额外提供 `generation/planning.py`、`generation/profiles.py`、`llm/capability.py` 与 main 的 11 个 agent 模块，否则仍会 ImportError。

## 4. 摘要

### 4.1 四桶计数

| 桶 | 数量 | 含义 |
|---|---|---|
| A 两代实现 | 67 | 只能整份取一边；建议 **48 个取 feat**，**19 个取 main**（main 是严格超集且 import 可独立）：`agents/diagnosis`、`agents/log_audit`、`agents/pending_actions`、`agents/proposals`、`corpus_health`、`ingestion/chunk`、`kg/extractor`、`kg/ledger`、`routers/assistant_proposals`、`routers/novels`、`routers/settings`、`style/exemplar`、`tokenize`、`components/DistillProgress.vue`、`components/RoleModelPanel.vue`、`views/LlmSettings.vue`、`tests/test_assistant_diagnose.py`、`tests/test_assistant_proposals.py`、`tests/test_settings_roles.py` |
| B 必须并存 | 35 | 手工合并，主战场是 `models.py`/`db.py`/`main.py`/`schemas.py`/`provider.py`/`projects.py` + 前端 API 层 + 26 个测试/验证脚本 |
| C 机械合并 | 9 | 配置、依赖、锁文件、ignore、pytest 配置 |
| D 形式差异 | 14 | 文档、样式、说明文件 |

### 4.2 风险高的 25 个

后端核心与接口 7：`models.py`、`db.py`、`main.py`、`schemas.py`、`generation/engine.py`、`llm/provider.py`、`routers/projects.py`
后端其它 11：`agents/registry.py`、`generation/prompts.py`、`ingestion/parse_txt.py`、`kg/batch_worker.py`、`kg/build.py`、`kg/extractor_v3.py`、`kg/sentinel.py`、`routers/assistant.py`、`routers/assistant_generation.py`、`routers/generation.py`、`routers/wizard.py`
后端文风 2：`style/narrative.py`、`style/stylometry.py`
前端 4：`views/ProjectWorkspace.vue`、`views/AssistantWorkspace.vue`、`api/types.ts`、`api/client.ts`
测试 1：`tests/conftest.py`

### 4.3 必须先拍板的决策点（按依赖顺序）

1. **主链路选 feat 还是 main。** 决定 67 个 A 文件取哪边，并决定 25 张同名表的"列并集"以哪边为基线。建议 feat。
2. **main 独有的 5 张表 / 5 个 router 是否保留。** 保留则 `models.py` 追加 5 类、`main.py` 增加 5 个门控 router；不保留则 5 个 router 与 5 张表一起删，`main.py` 只留 feat 的 13 个。
3. **若保留 main 的可选模块，是否接受额外 4 个依赖文件。** `generation/planning.py`、`generation/profiles.py`、`llm/capability.py` + main 的 11 个 agent 模块；否则 main 的"助理→工单→生成"链不成立。
4. **`models.py` → `db.py` 的并集基线。** 先定 12 张列不同的同名表怎么并列，再定补列清单（feat 79 + main 14）与 `test_schema_migration_completeness.py` 的期望值。
5. **`main.py` 的导入策略。** 并集导入会直接 ImportError；必须改成"主链路顶层导入 + 可选模块函数内导入"。
6. **`provider.py` 的重试/限流语义。** main 的 `variant` 与 feat 的 `_RateGuard` 是两套超时/重试，需定一套为准，另一边降级为可选参数。
7. **`routers/projects.py` 以哪边为底。** main 88 个独有入口 vs feat 的 `distill/resume`；若第 1 步选 feat，则此处要么改取 feat、要么单独手并。
8. **前端 API 层顺序。** 先 `types.ts` → 再 `client.ts` → 再 `router/index.ts` → 最后各 view；否则视图层无法通过类型检查。
9. **测试口径。** `conftest.py` 两套夹具并存；被放弃一侧的测试要么删、要么改成针对保留实现的断言，不能直接留。

---

*本报告为只读评估产物，未提交、未推送，未改动 `main` 与主工作区。*
