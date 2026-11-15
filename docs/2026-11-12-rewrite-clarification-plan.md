# 局部改写：改写提示词去编号 + 含糊指令先澄清

## Context

两条真实运行暴露的问题，按优先级处理：

1. **（先做）改写环节仍在给模型看编号**：Tier A 提示词把整段按句编号、用 `>>` 标目标句，等于请模型「别抄编号」，事后剥离只是兜底。要从根上改成：改写提示词不出现任何编号，只把待改句原样放进分隔块。
2. **（随后做）含糊指令由模型定位置**：Tier B 让模型同一次调用既选区间又写正文，实测选出 11/85 句（13%），等于整段换说法。改成先给 2–4 个候选区间让用户选，选定后走 Tier A。

约束不变：只动 p5 段 1；不改 `length_drop` / `scope_guard` 阈值；不 distill、不改 refs；守卫指标保持。

---

## Phase 1（优先）：改写提示词彻底去编号

### 提示词

改 `backend/app/generation/patch.py::build_patch_user_prompt`：

- 删除整段编号列表、`>>` 标记、`第 N 句到第 M 句`、`共 N 句` 等所有编号脚手架；
- 只保留一段明确分隔块：

```
下面是一段已定稿正文中的几句话。请按【本次要求】改写，并且只返回改写后的文本。

【本次要求】
{instruction}

【待改写的句子】
【待改写片段开始】
{目标句原文，逐句换行，不带任何序号}
【待改写片段结束】

【输出要求】
- 逐句对应改写，保持句子数量不变；
- 只返回改写后的文本，不要解释、不要代码块、不要添加任何列表标记或前后缀；
- 不要输出待改写片段之外的任何句子。
```

- 程序侧 `resolve_scope` 与按编号取原句、拼接逻辑**完全不动**；
- 提示词不出现「编号」二字，避免反向提示。

### 句数约束

在 `_validate_replacement` 增加：

- `span_sentences >= 2` 时，`len(split_sentences(replacement))` 必须等于 `span_sentences`，否则抛新错误码 `replacement_sentence_mismatch` 失败关闭。
- 理由：落库仍按字符区间整段替换，机械上不存在还原歧义；这条约束的作用是**锁住局部边界**——不允许模型把 2 句悄悄扩成 3–4 句，也不允许合并成 1 句后用户失去逐句对应关系。句数用 `split_sentences` 判定，连续句末标点算一句，避免 `？！` 被误数。
- 单句跨度保持现有规则（≥3 句才拒绝），减少对单句改写的误拒。
- 真实数据风险：模型被明确要求「保持句子数量不变」后通常能遵守；若 p5 实测出现合理合并被拒，如实记录，不擅自放宽。

### 兜底剥离保留

- `_strip_leading_numbering` 保留；
- `_tier_a_replacement` 改为返回 `(text, numbering_stripped)`，`numbering_stripped=True` 表示模型原始返回里确实出现行首编号、被兜底剥除；
- `parse_patch_reply` meta 增加 `numbering_stripped`，供探针与验收判断「模型本来就没写编号」还是「写了被剥掉」。

### 测试

`tests/test_local_patch.py`：

- `test_patch_prompt_contains_no_numbering_scaffolding`：提示词不含 `>>`、不含行首 `1.`、不含 `from_index`、不含「编号」；非目标句不出现在提示词里；
- `test_patch_prompt_contains_only_target_sentences`：多句跨度时，目标块 = 原文那句区间逐字；
- `test_multi_sentence_replacement_keeps_sentence_count`：2 句目标返回 1 句/3 句均 `replacement_sentence_mismatch`；
- `test_multi_sentence_replacement_restores_exactly`：2 句目标返回 2 句，重建后区间外逐字不变；
- `test_plain_text_is_not_stripped` 与小数不误剥保留；
- 现有 Tier A / 数量词 / 整段回归保持通过。

### 真跑（每次之间回滚到 `f218398bde8b618e518252fc3768b239`）

- p5 段 1「最后两句」跑一次；
- p5 段 1「仅调整最后一句」跑一次；
- 每次给：实际发出的改写提示词片段（探针包一层 `provider.chat` 捕获）、模型原始返回前 200 字、`numbering_stripped`、最终正文是否含行首编号、长度比例、区间外是否逐字不变、`scope_mismatch`/`length_drop` 两个信号；
- 若模型仍写出编号，如实报告，不以「剥掉了」当通过。

---

## Phase 2：含糊指令先澄清候选

### 候选上界

`clarify_span_limit(total) = max(1, min(3, total // 10))`

- 3 句：一个候选最多一个完整叙事节拍；
- 全文 10%：防止短段里 3 句占比过大（85 句时 3 句 = 3.5%）；
- 向下取整、至少 1 句：短段仍可用。

越界、倒序、覆盖全文、超过上界、空区间、与已接受候选重叠 → 丢弃；全部丢弃 → `no_valid_candidate` 失败关闭；候选按模型顺序保留，重叠保留先出现者。

### 澄清留痕与幂等

- `generation_snapshots` 新增 `clarification` JSON 列（`_migrate_sqlite_columns` 补列）；
- 新状态 `needs_clarification`、operation `clarify`；不建 pending、不写 `generations.text`、不产生 applied；
- `clarification = {round, candidates, fingerprint, note}`；
- 幂等键 `sha256(project_id, segment_no, before_hash, instruction, round, note)`；同键已存在则复用，不再调模型；
- 选定并成功写入后置 `clarified`；再次使用同一 id → 409 `clarification_used`；正文已变化 → 409 `clarification_stale`。

### HTTP 契约

- `202 Accepted`：`{status:"needs_clarification", clarification_id, round, max_rounds:2, candidates[], before_hash}`；
- `200`：正常写入成功；
- `422`：候选全部无效 / 两轮用尽 / Tier A 校验失败；
- `409`：段落占用、澄清已使用、正文已变化。

202 属 2xx，现有 `request()` 会正常解析，不会走错误分支。

### 后端改动

- `patch.py`：`build_candidates_user_prompt`（此提示词**保留编号清单**，只选区间、不写正文）、`parse_candidates_reply`（程序按编号截取 `preview`，不采信模型复述；`reason` ≤60 字；去重/重叠过滤）、`scope_from_selection`；删除 Tier B 两段式的 `build_index_user_prompt` / `parse_index_reply`；
- `models.py` + `db.py`：`clarification` JSON 列；
- `snapshot.py`：`begin_clarification`、`find_clarification`、`mark_clarification_clarified`；
- `engine.py`：`regenerate_segment_stream(..., selected_scope=None)`，有选定区间直接走 Tier A；无区间且 `resolve_scope` 为 None 抛 `clarification_required` 安全网；
- `assistant_generation.py`：请求体加 `clarification_id` / `selected` / `clarify_reply`（互斥）；含糊指令前置拦截走澄清，不 `begin_snapshot`；选择路径校验候选与 `before_hash` 后走普通快照 + Tier A；澄清调用也落 AgentRun。

### 前端

- `AssistantWorkspace.vue` 的待确认卡片：收到 202 `needs_clarification` 时不标记 confirmed，渲染候选（`第 from–to 句` + 程序截取片段 + 理由）；点选后以原 body + `clarification_id` + `selected` 再调用；「都不对」展开输入并提交 `clarify_reply`，两轮后显示 `clarify_round_limit`；
- 候选预览只显示程序截取的原文片段，**不把编号清单渲染进正文区域**；
- `types.ts` / `client.ts` 补字段，复用现有 `assistantRunPendingAction`。

---

## 验证顺序

1. Phase 1 改动 + 测试 + `py_compile`；重启前后记 pid 与 `/healthz`；
2. Phase 1 两次真跑（先「最后两句」，再「仅调整最后一句」），每次回滚并核对 md5；
3. `post_guards.py` 复核：`kg_mentions` 34、gid45 并集 125、六探针、11 条持有行；
4. Phase 2 改动 + 测试；
5. Phase 2 两次真跑：「某处写得太平了，收拾一下」「这一句不太顺」各走澄清 → 选择 → 执行 → 回滚，确认澄清阶段 md5 不变；
6. 前端启动后端 + `npm run dev`，浏览器触发卡片 → 候选展示截图 → 点选执行截图；
7. 按逻辑拆 1–2 个提交接在 `feat/resume-push-2026-10` 后，不推远程。

## 关键文件

- `backend/app/generation/patch.py`
- `backend/app/generation/engine.py`
- `backend/app/routers/assistant_generation.py`
- `backend/app/agents/dialog/snapshot.py`
- `backend/app/models.py`、`backend/app/db.py`
- `tests/test_local_patch.py`
- `frontend/src/views/AssistantWorkspace.vue`
- `frontend/src/api/types.ts`、`frontend/src/api/client.ts`
- `backend/.tmp_verify/seg1_patch_probe.py`
