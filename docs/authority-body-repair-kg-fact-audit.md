# 受控修复恢复：KG/Fact 入口与生成守卫离线审计（只读，未实施）

## 0. 本文件的性质

只读代码审计结果，不含任何模型调用、数据库写入或代码修改。所有结论均可在
`release/foundation-delivery` 分支 HEAD `d700597` 上按给出的文件行号复核。

用途：补齐 `docs/authority-body-repair-recovery-design.md` 第 6 条（「KG/Fact 必须查清项目已有
生成分支事实重建入口」）所要求的前置调查，并指出该设计第 6 条的**两处前提不成立**。

## 1. 结论摘要

| 项 | 结论 |
| --- | --- |
| 生成守卫 | **无法解除**。全仓不存在清除 `body_revision_requires_review` 的代码路径 |
| KG 重建入口 | **存在且可复用**：`build_graph_from_text(source='generated')` 全量重建 |
| Fact 重建入口 | **不存在**。`upsert_facts` 只增量累加，全仓无按 source 批量清除 Fact 的路径 |
| 建议 | 复用 KG 全量重建；Fact 需新增「按段替换」原语（可照抄既有哨兵/Judge 先清后写语义）；生成解锁需新增显式过渡 |

## 2. 生成守卫：没有任何代码能解除它

关键键 `project.plot_timeline['body_revision_requires_review']`，全仓仅 5 处代码命中：

- `backend/app/agents/proposals.py:591` —— 正文 apply 时**设置**（同时清 summary/summary_tree/completion，
  并写 `derived_state={'kg':'pending_rebuild','facts':'pending_rebuild'}`）。
- `backend/app/agents/derived_rebuild.py:26` —— 重建入口**要求该键存在**，否则 `rebuild_requires_body_revision`。
- `backend/app/generation/engine.py:424-428` —— 生成时**拦截**：`body_revision_requires_review` 存在即
  `HTTPException(409, '正文已修订，摘要与时间线完成证据已失效，须复核后再生成')`。

全仓检索（含前端与测试）确认：**没有任何一处 pop / 置空 / 改写该键**。
`rebuild_derived` 成功时只 `timeline.update(completion=..., done=..., last_judged_segment=...)`，
`body_revision_requires_review` 原封不动留在 timeline 里。

因此：**即使摘要、summary_tree、completion 全部重建成功，生成仍然 409。**
`rebuild_derived` 自己的设计也印证这点——它把结果标成 `status='needs_review'`、
`limitation='KG/Fact pending rebuild; existing review guard retained'`，即「保留守卫」是刻意的，
不是遗漏。

设计文档第 6 条写的「全部成功并回读后才解除生成守卫」缺少**可执行对象**：现在既没有守卫解除函数，
也没有「解除」这个动作的任何残留实现。这一条必须新增为独立过渡，不能靠改状态标签。

## 3. KG 重建：入口存在，但必须用全量而非增量

- `backend/app/kg/build.py:164-179` `build_graph_from_text(db, project_id, text, source='generated')`
  —— 语义是**手动全量重建**：先 `_clear_graph_by_source` 清掉该 source 的旧节点与相关边，
  再对拼接正文重新抽取并写回。
- `_clear_graph_by_source`（`build.py:17-28`）只清 `KgNode` / `KgEdge`，按 `source` 隔离，
  不触碰参考语料侧 `source='novel:{id}'` 的图。
- 已有 HTTP 入口：`backend/app/routers/wizard.py:428-440`
  `POST /api/wizard/{project_id}/build-kg-from-generations` —— 取该项目全部 `Generation.text`
  拼接后 `build_graph_from_text(..., source='generated')`。
- 生成时自动增量用的是 `merge_graph_from_text` / `merge_result_into_graph`
  （`build.py:195-206`、`engine.py:187-191`），语义是**增量 upsert，不清空**。

**修复恢复必须用全量重建，不能用增量。** 正文修订后，修订前正文产生的角色/坑/关系节点
（例如「手机」被登记成角色、「手套/棉套」进了角色 props）仍留在图里；增量合并只会再累加一遍，
旧事实永不消失。全量重建按当前正文重新抽取，是唯一能清掉陈旧节点的既有路径。

## 4. Fact 账本：没有任何重建入口

- `backend/app/kg/ledger.py:129-195` `upsert_facts(...)` —— 明确写着「增量 upsert。语义：同名累加
  计数/扩段范围，**不清空、不重建**」。同一 `(type, key)` 命中即累加 `mentions`/`speaking`、
  扩 `first_segment`/`last_segment`，没有任何删除分支。
- `build_graph_from_text` 走的是 `_clear_graph_by_source`，**只清 KgNode/KgEdge，不清 `facts` 表**。
- 全仓 Fact 删除点只有两处，且都不是重建：
  - `ledger.py:518/534/536` `apply_merges` —— 别名合并时删掉被吸收的重复行。
  - `sentinel.py:1001-1005` `write_state` —— 快照里不存在的 state 键被丢弃。
- **不存在**任何按 `source` 批量清除 Fact 的函数，也不存在「按段重建 Fact」的函数。

后果：正文修订后重跑 KG 全量重建，**`facts` 表仍是修订前正文的事实**。生成侧消费路径
`get_reminders(db, project_id, source='generated')`（`kg/queries.py:96`）读的是 KG 节点，
但 `agents/diagnosis.py`、`agents/audit.py`、角色状态判定读的是 `facts` 表
（如 `Fact(type='state', key='life:{角色}')`）。所以只重建 KG 会造成
**图谱已更新、事实账本仍是旧版本**的分裂状态，比不重建更危险。

### 4.1 可复用的既有先例（不需要加表）

仓库里已经存在两种「先清后写」语义，都在同一张扁平 `facts` 表上，都有 `source` /
`first_segment` / `last_segment` 列可用来做段级定位：

1. **同段替换**：`kg/judge.py:458-496` `save_judge_findings` —— 删除 `last_segment == segment_no`
   的旧告警行，再写新的本段告警。注释写明理由：「段落改好了、旧告警还挂着」比没有告警更糟。
2. **快照替换 + 丢弃缺席键**：`kg/sentinel.py:975-1008` `write_state` —— live 集合外的键直接删。

Fact 账本按段重建可以照抄第 1 种语义（按 `source='generated'` 且段级定位先清后写），
或对全项目重建照抄第 2 种（快照语义）。两者都不需要新增表或改 schema。

### 4.2 一条必须守住的边界

`Fact(type='exemption')` 是**作者的豁免决定**（`routers/kg_v3.py:163-176` 写入，
`kg/judge.py:437-445` 读取）。重建派生事实时不得把豁免行一起清掉，否则会静默抹掉作者判断。
按 source 批量清理时必须排除该类型。

## 5. apply 闸门：单一服务端计算位

- `POST /api/assistant/proposals/{id}/approve` → `assistant_proposals.py:451`
  `if scope['independent_verification_required'] and scope['semantic_accepted'] is not True: 409`。
- `POST /api/assistant/proposals/{id}/apply` → `proposals.py:948` 同一条件、同一 409。
- `semantic_accepted` 只在 `body_repair.py:201-203` 计算一次：
  `verification['status']=='completed' and checks 非空 and all(supported) and not verification['unresolved']`。
- `verification['unresolved']` 在 `authority_body_repair.py` 装配：
  - `:86` 起始即 `list(previous.get('unresolved',[]))` —— **原样继承**；
  - `:109-113` 协议类缺口；
  - `:118` 每个非 supported 的原检查追加 `original_check:<id>`；
  - `:120` 覆盖不全时追加 `inventory_partial_coverage`；
  - `:121` 旧 inventory 未全局通过时**无条件**追加 `full_scope_acceptance_unverified`。
- `scope_json` 里的 `semantic_accepted` / `verification_run_id` / `verification_acceptance` /
  `repair_result` / `confirmation_hash` 均在 `proposals.py:158-169` 的
  `SERVER_COMPUTED_SCOPE_KEYS` 内，客户端无法伪造。

推论（内部审计给出的 `no_global_recovery_transition` 结论）：
在 `scope='local_only'` 下 `unresolved` 永不为空 ⇒ 闸门永不开。要合规打开，只能靠一次
**整段范围**的复验产生满足完整验收要求的 `unresolved` 为空的结果，再通过**显式操作**把新 run
绑定到既有 pending 提案。直接改 `semantic_accepted` 是绕过闸门，禁止。

另注：`body_repair.py:157-165` 对相同 `confirmation_hash` 直接复用已存 `repair_result`，
这是防止重复调模型的保护。新增的复验操作必须是**另一个显式入口**，不能靠重复提交同一确认请求
隐式覆盖（那等于既绕过重复调用保护，又绕过审核）。

## 6. 对恢复设计第 6 条的修订建议

原设计第 6 条：「KG/Fact 必须查清项目已有生成分支事实重建入口，重建绑定当前正文指纹且隔离原著事实。
任一重建失败保持 stale，全部成功并回读前不解除生成守卫。」

按本次审计，该条需要拆成四个可执行子项，其中后两项在原设计里没有对应实现：

1. **KG**：`build_graph_from_text(db, project_id, 拼接正文, source='generated')`，全量重建，
   不用增量合并；重建前后核对 `KgNode/KgEdge` 数量与 source 隔离。
2. **Fact**：新增「按当前生成分支重建」原语，语义照抄 `save_judge_findings`（同段先清后写）
   或 `write_state`（快照替换）；排除 `type='exemption'`；重建绑定当前正文指纹。
3. **生成解锁**：新增显式过渡，在 KG 与 Fact 重建全部成功、且回读校验通过后，
   才清除 `body_revision_requires_review` 并把 `derived_state` 置为已重建；
   任何一步失败则保持 409 守卫与 `pending_rebuild` 状态。
4. **完成度重判**：`completion` / `done` 沿用现有 `judge_node_completion` 实现不动，
   但解锁必须以它为准，不能只凭「摘要已重建」判定恢复完成。

## 7. 未验证 / 不在本轮范围

- 未运行任何真实模型脚本，未调用 Writer/Assistant/Distiller。
- 未连接任何数据库做读回；以上结论全部来自代码静态阅读，行号对应 HEAD `d700597`。
- 未验证 `build_graph_from_text` 在本项目正文上的实际抽取结果与耗时。
- 未验证 Fact 段级替换对 `type='state'`（角色生死/持有物）语义的影响，需要在实现阶段单测固化。
