# 真书全流程验证（北荒刀歌语料 → 九道工序全绿）

> 一本**真书**（北荒刀歌，990 章 / 5323 片）当参考语料，走助理 agent 编排的
> **九道工序**，全程真实 MiniMax-M3 通道。结论：**从交稿到收敛全部走通**，
> 中间挖出一个**结构性缺口**（没有大纲就没有段契约，正文审查无从判起）。

## 1. 这一轮跑的是哪条链

```text
【提示词轨】① 语料已就位 → ② 清洗抽检 → ③ 文风蒸馏 → ④ 档案审查 → ⑤ 提示词体检
【正文轨】  ⑥ 写正文 → ⑦ 正文审查（封闭清单审计 + 整段验收）→ ⑧ 修复待批 → ⑨ 收敛
```

**真实素材**（不是造的数据）：

| 项 | 值 |
|----|-----|
| 参考语料 | `raw_novels` novel#1 = 北荒刀歌，**990 章 / 5323 片** |
| 文风档案 | 蒸馏库 #4：full 档、`narrative_version=8`、**3159 条规则**（297 章采样） |
| 档案用法 | **从蒸馏库应用**（不重跑蒸馏 —— 真档案已经在了，省掉几十分钟 LLM） |
| 可见边界 | `reference_cutoffs=[{novel_id:1, chapter:990}]`（不设它，参考内容会被可见性策略全挡掉） |
| 写作模式 | `original`：**自己的故事 + 参考书的文风**（最严的那条边界） |
| 项目 | 「真书全流程验证：临江渡口」：premise + plot_bible + 三段大纲 |

## 2. 工序链逐道状态（全部真实数据）

```text
corpus_ready     done     已完成      参考语料已切分 5323 片。
clean_check      done     已完成      抽检通过（5 条提示不阻塞）。
distill          done     已完成      文风档案已就位（从蒸馏库应用）。
review_narrative done     已完成      蒸馏产物审查通过（15 条提示不阻塞）。
review_prompt    done     已完成      提示词体检通过（0 条提示不阻塞）。
write_body       done     已完成      已写出 2 段。
review_body      done     已完成      整段验收已通过，规划覆盖率没有缺口。
repair           done     没有待批项  没有待批的写回建议。
converged        done     已收敛      这一轮全部做完了。
当前步: None    gates: []    待处理改动: 0
```

其中「③ 文风蒸馏 done」与「⑤ 提示词体检 0 问题」**是这一轮修完误报之后才有的**——
见 `docs/stage3-real-rerun.md` §4：修之前，真书档案会被判成「原创模式泄漏参考书角色声音」
和「禁用词自相矛盾」，两个 error 级，直接把生成闸门关死（409），真书项目**一个字都写不出来**。

## 3. 正文审查（真实判官，70 次调用）

| 环节 | 结果 |
|------|------|
| 写正文 | 真实 writer，2 段：**4801 + 3579 = 8380 字** |
| 段契约 | LLM 规划出 **3 个节点**（雾晨摆渡 / 异常登船 / 河心破绽），各带入态出态，落库；2 段分别绑到 n1、n2 |
| 封闭清单审计 | run_id=96，**70 次真实调用**；291 段、**1833 提及**、1747 独立实体；`pending_entities=0`（**全部裁决**）、覆盖完整、**违例 0**、`unknown_checks=10`、`rejected_mentions=10` |
| 整段验收 | **accepted**，`unresolved=[]`；coverage 把 **291 个段落 id + 2 个契约节点 id** 全回抄，`complete=true` |
| 规划覆盖率 | `blocking=[]`；`visible` 1 条：「规划里还有 1 个节点没有任何正文覆盖：n3（故事还没写到那里，不阻塞本版验收）」 |
| 开放项 | 10 条 `inventory_unlocated`（模型把提及的词改写了，逐字定位不到）——**可见但不阻塞** |
| 解锁 | refused / `nothing_to_unlock`（正文未被修订 → 没有守卫） |

`visible` 那条正是「规划覆盖率门禁」设计的意图：**没写到的地方说得清，但不阻塞本版**，
也不会假装「都覆盖了」。

## 4. 挖出来的结构性缺口：**没有大纲，就没有段契约，正文审查无从判起**

第一次跑（项目只给了 premise + plot_bible，**没给大纲**）：

```text
审计：4 次尝试，全部 calls=0 / model_calls=0 / mentions=0
      unresolved = missing_saved_authority ×2 + inventory_partial_coverage
      coverage_complete=false   → blocker: audit_coverage_incomplete
验收：直接 no_pending_acceptance 抛错
```

根因在 `app/generation/engine.py::_timeline_state_for` 的**第一行**：

```python
if not project.outline:
    return None
```

没有 `outline` → 不规划时间线 → **没有段契约** → 封闭清单审计没有判据基准
（它要拿每段的 `contract:{段号}` 来源当权威）→ 审计零候选、零调用、覆盖不完整
→ 正文审查这步**永远过不去** → 链走不到收敛。

补上三段大纲后，同一条链一次走通（§2、§3）。

**这不是「少传一个参数」，是工序链缺一环**：
「写正文」这一步的 `read_tools` 只有 `get_plot_timeline`，**没有产出大纲/契约的写路径**
（`write_proposal=None`）。也就是说：**agent 自己没法把「没有大纲」这件事变成「可执行的下一步」**——
它只能看到「写正文 pending」，却没法提一个「先规划大纲」的提案。
这正是 `docs/agent-pipeline-design.md` 里 **片 2c（写工序提案化）** 与
**片 3（闭环工序接入扩展规划）** 要解决的事：

| 片 | 内容 | 状态 |
|----|------|------|
| 2c | 写工序提案化：`start_distill` 与 `prompt_override` 两类提案（含 apply 路径） | ⏳ 待做 |
| 3 | 闭环工序接入验收/修复/重建/解锁/**扩展规划**的提案类型 | ⏳ 待做 |

按现有机制，「大纲」由 `plan_extension`（`app/agents/plan_extension.py`）那条路负责 ——
它的设计说明里写着「规划用尽后生成被拒（409 plan_exhausted）是对的 —— 没有段契约就没有保存权威可核对」。
**要让 agent 自主推进，它得能「提一个扩展规划/补大纲的提案」**，而不是让作者手动填。

## 5. 这套审核**抓不到**什么（要说清楚）

正文里肉眼可见两处瑕疵：

1. 沈娘子一句台词里混进了英文词：「怕不是 **River** 里头翻了肚皮」；
2. 年龄前后不一致：叙述写「那年他**十四岁**」，沈娘子却叫「**十三岁**的娃娃」。

**这两条都不是这套审核的判据范围**：封闭清单审的是**越界**（正文里出现了契约之外的
物品/角色/地点），整段验收审的是**时间/地点/参与者/物品状态**与契约是否一致。
数字自相矛盾、混入英文词属于**语言质量与事实自洽**，当前没有一道工序在管。

所以「九道工序全绿」的准确含义是：**流程走通了、越界没发生、状态一致**，
不等于「文字没问题」。要覆盖后两者，需要新加审核维度（不是修现有判据）。

## 6. 复现

```powershell
# 1) 建项目 + 应用真书档案 + 写正文（走真接口）
python "%LOCALAPPDATA%\hermes\cache\scratch\run_realbook_flow.py"

# 2) 正文审查（封闭清单审计 + 整段验收），并发 4
$env:PROJECT_ID='8'; $env:CLOSED_INVENTORY_CONCURRENCY='4'
python tools/run_closed_inventory_full_audit.py `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\realbook-audit-2" `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\realbook-audit-ckpt2" `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\realbook-flow\work-2\realbook2.db"
```

工件：`scratch/stage3/realbook-flow/work-2/`（库 + flow.log）、
`scratch/stage3/realbook-audit-2/`（report.json / acceptance.json / isolated.db）、
`scratch/stage3/realbook-audit-ckpt2/`（69 个裁决检查点）。
