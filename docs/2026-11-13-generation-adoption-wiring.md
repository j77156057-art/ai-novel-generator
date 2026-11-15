# 生成侧接入检索侧新工具（六处）

## Context

检索侧（`retrieval/`）已完成四件事：卡表 IDF 采纳判据、`judge_calls` 批量判定、
段末回填改为更新该段所有行、五级目录工具对中文标签归一、作者轨迹监察工具
`inspect_retrieval_trace`。生成侧仍在用旧实现：

- `author_research._judge_call` 用硬编码的 `_ENTITY_WORDS`（本书实体词）判改写，
  换书即失效，且项目 6 实测 adopted=0、人工复核至少 3 条被改写式采用；
- `author_research.check_adoption` / `backfill_adoption` 自己判定、只写采纳行，
  与 `retrieval.trace.fill_segment_adoption`（更新该段所有行，含被拒）口径不一致；
- 作者提示词仍警告「中文标签会空转」，但工具已能归一；
- 提示词用「最多再读 2 条 / 最多 3 轮」这类轮次口径，与工具内预算池（接近 soft
  收窄、hard 只给最小结果）不一致；
- 助理侧工具清单里 `inspect_retrieval_trace` 只在注册表 `_DEFAULT_SPECS` 有，
  仓库内另有一处手维护的工具清单注释未同步。

目标：生成侧只做委托与提示词对齐，不改 `retrieval/` 实现；用项目 6 实跑验证。

## 一、`backend/app/generation/author_research.py`（第 1–3、6 条）

### 1. 删除旧判据，改为委托
- 删除 `_UNIT_RE` / `_QUOTED_RE` / `_ENTITY_WORDS` / `_CN_STOP` / `_CJK_RE` /
  `_markers` / `_bigrams` / `_rewrite_evidence`，以及旧的 `_judge_call`、
  `check_adoption`、`backfill_adoption` 实现体；同时删掉仅它们使用的
  `import re`、`select`、`RetrievalLog`。
- 新增 import：
  `from app.retrieval.adoption import TermWeights, judge_call as retrieval_judge_call, judge_calls as retrieval_judge_calls, backfill_adoption as retrieval_backfill_adoption`。

### 2. `TermWeights` 从卡表构建
- 新增 `_adoption_weights(distill_id)`：`load_catalog(int(distill_id)).cards`
  → `TermWeights.from_cards(...)`；按 `distill_id` 做模块级 memo（卡表 54MB，
  避免每段重建）；`distill_id` 为空或卡表未构建时返回 `None`。
- `distill_id` 来自 `ToolContext.distill_id`（工具首次 `_catalog` 时回填）。
  在 `run_author_research` 的 `finally` 里写入
  `out["distill_id"] = tool_ctx.distill_id`、`out["segment_no"] = segment_no`，
  让 `backfill_adoption` 不必改 engine 调用签名。

### 3. `_judge_call` 委托
```python
def _judge_call(call: dict, text: str, weights=None) -> dict:
    return retrieval_judge_call(
        str(call.get("raw_text") or ""), text,
        tool=str(call.get("tool") or "rule"), weights=weights,
    )
```

### 4. `check_adoption` 委托并保持字段兼容
- 调用 `retrieval_judge_calls(calls, text, weights=weights)`，直接返回
  `counts/shares/per_call`（新实现已兼容这三个键）。
- 为不丢旧 `quality_json` 里的字段，按 `seq` 把原 call 的 `args`、`gap_metric`
  回填进 `per_call` 对应项（`setdefault`，不覆盖新字段）。

### 5. `backfill_adoption` 委托
```python
def backfill_adoption(db, research, text, *, weights=None):
    calls = research.get("calls") or []
    sid, seg = research.get("session_id"), research.get("segment_no")
    weights = weights or _adoption_weights(research.get("distill_id"))
    if not sid or seg is None:
        return check_adoption(calls, text, weights=weights)
    try:
        return retrieval_backfill_adoption(db, sid, int(seg), text,
                                           calls=calls, weights=weights)
    except Exception:
        db.rollback()
        return check_adoption(calls, text, weights=weights)
```
- 注意新实现会更新该段**所有**行（含被拒），回填后要核对库里行数与分布。
- engine 两处 `backfill_adoption(db, research, text)`（`engine.py` L2325、
  L2583）签名不变，无需改 engine。

### 6. 让 `judge_calls` 能取到规则 id
`_record` 里补两个字段（`retrieval.judge_calls` 只认 `call.rule_id/card_id`）：
- `"rule_id": ids[0] if ids else None`（`_ids` 对 rule/anchor 返回 rule_id/card_id；
  search/browse/similar 是 nav，不参与采纳统计）；
- `"path": str(args.get("path") or "")`。

### 7. 提示词去轮次化（第 6 条）
- `_system_prompt` 末行由
  `内容够了就 {"tool":"done"}。最多再读 2 条，不要重复读同一条。`
  改为
  `内容够了就 {"tool":"done"}。预算接近上限时工具会自动收窄返回（条数变少、窗口变窄），按需要继续读即可；不要重复读同一条。`
- 模块 docstring 第 3 步同步改为「预算接近时收窄」，不再写「最多补挑 2 条」。
- 代码里的 `_MAX_MODEL_ROUNDS/_MAX_MODEL_RULES/_MAX_ROUNDS_SAFETY` 保留为内部
  安全阀，不写进提示词（本次只改提示词口径，不改循环行为）。

## 二、`backend/app/generation/prompts.py`（第 5 条）

- `_catalog_path_keys()` 的模型可见标题
  `【路径键（工具只认英文键，别用中文标签拼 path）】`
  → `【路径键（优先用英文键；中文标签也会被工具归一，可直接使用）】`。
- 同步修正 `_catalog_skeleton_block()`（L205-208）与 `_catalog_path_keys()`（L221）
  的 docstring：删掉「前 6 次调用全部空转」的旧结论，改为「工具已归一，英文键
  优先用于精确对照」。

## 三、`backend/app/agents/registry.py`（第 4 条）

现状核查：`ALLOWED_READ_ONLY_TOOLS`（L73-99）与 `_DEFAULT_SPECS`（L1006-1017）
**都已包含** `inspect_retrieval_trace`，其 description 已写明用途与返回三档。
仓库里没有独立的「助理 system prompt 工具清单」；模型可见的说明就是
`_DEFAULT_SPECS.description`。因此本条只做一处同步：

- 更新模块顶部手维护的「默认白名单」注释（L61-63），改为指向
  `ALLOWED_READ_ONLY_TOOLS` 单一来源，并补上五级目录工具与
  `inspect_retrieval_trace`，避免注释与实际白名单再次漂移。

> 若你所说的「提示词侧」另有所指（例如 `routers/assistant.py` 的 QA system
> prompt），那不在本次「只改 generation/ 与 agents/」的约束内，报告里会单列。

## 验证（第 4 项 + 实跑）

### 静态
- `python -m py_compile` 三个改动文件；导入 `author_research` 与
  `app.agents.registry` 确认无循环导入。
- 旧函数无引用残留：`rg "_ENTITY_WORDS|_rewrite_evidence|_bigrams" backend/app/generation`。

### 项目 6 实跑（脚本放 `D:\Temp`，不进仓库）
1. 进程级临时开启：启动脚本前设 `$env:AUTHOR_RESEARCH_ENABLED="true"`（不改
   `.env`；新进程默认仍为 `False`，结束后自然恢复）。
2. 取项目 6 最新一段正文（当前为段 19，5404 字）与 `generation_gate(text, len(text))`；
   若闸门通过，则按实测指标构造一个确定失败项（例如把「短句占比」下限抬到实测值
   的 2 倍），报告里标明这是为了确定性触发自查。
3. `run_author_research(db, project, get_llm_provider(), {}, segment_no=19, gate=...)`
   → `backfill_adoption(db, research, text)`。
4. 核对并记录：
   - `retrieval_logs` 里该 `session_id` 的行数 == `len(research["calls"])`，`seq`
     无缺口/重复；
   - `adopted_rule_ids` 非空行数 > 0，`counts/shares` 与 `SELECT` 结果一致
     （新实现会写该段所有行）；
   - 中文路径探针：用同一条 query，分别以英文 L1 键与对应中文标签调 `search`，
     两边的 `status` 均为 `ok` 且 `hits` 非空；
   - `inspection.inspect_session(db, session_id=...)` 的 `verdict ∈ {healthy, watch}`。
5. 恢复核对：另起进程确认 `settings.author_research_enabled is False`。

## 报告

新增 `reports/2026-11-13-generation-adoption-wiring.md`：
- 六处改前/改后对照表（旧实现 → 委托目标 / 旧提示词 → 新提示词）；
- 项目 6 实跑证据（session_id、调用数 vs 轨迹行数、采纳分布、中文路径探针、
  监察 verdict、耗时/ token）；
- 明确声明：`author_research_enabled` 默认关闭；本次为临时进程级开启，未改
  `.env`，已恢复默认；
- 遗留说明：第 4 条仓库内无独立提示词清单的核查结论。

## 不做

- 不改 `retrieval/` 下任何实现；
- 不改闸门判定与数值口径；
- 不重构 author_research 的检索流程（只换判据委托与提示词）。
