# 阶段三真实判官重审（额度恢复后）＋ 挖出来的两个真缺口

> 这一篇（悬疑·旧北站夜班值班室，接力版 5 段 876 段）在 MiniMax-M3 **真实通道**上
> 把「封闭清单审计 → 整段验收 → 解锁」整条链路走完。结论：**审计 0 违例、
> 整段验收 accepted**。但把过程走通的过程里又挖出两个真缺口。

## 1. 之前卡在哪：不是「模型没答对」，是调用根本没成功

上一次跑到整段验收时停住，报告里写的是：

```text
acceptance_status = blocked
unresolved        = ["acceptance_failed"]
model_participated = False          ← 模型根本没参与
model_error_category = RuntimeError  ← 但只有类型名，没有消息
```

`model_participated=False` 说明**模型一次都没被调用成功** —— 是通道侧失败
（当时是 429 额度用尽），不是「模型判了不通过」。这两件事的处置完全不同：
前者要等额度，后者要改口径，而报告把它们写成了同一个词。

**缺口一：验收失败不留错误消息。**
`body_acceptance.run_body_acceptance` 的 except 分支只记 `type(exc).__name__`，
把 `str(exc)` 丢了。于是「额度用尽 / 限流 / 输出被截断 / 模型答得不合格」
四种完全不同的失败，在报告里全都显示成 `RuntimeError` 四个字。
「报告说不清为什么」和「报告说错了」一样坏。

**修法**：`report['model_error_message'] = str(exc)[:500]`，并加测试守着
（`tests/test_body_acceptance.py::test_acceptance_failure_records_the_error_message`）。

顺带记录一条排查经验：这次失败最初被读成「模型判了不通过」，是因为
**控制台输出是乱码**，乱码被当成了「还没跑过文风蒸馏」那样的中文。
真值是「提示词体检 blocked，2 个必须先处理的问题」。
**看中文结论要看写进 UTF-8 文件的那份，不要看控制台。**

## 2. 缺口二：工序链的「文风蒸馏」只认一种产物来源

工序链的「文风蒸馏」判据只看 `distill_runs` 表。而文风档案有**两条**来路：

1. 项目自己跑一次蒸馏；
2. 从**蒸馏库**（`style_distills`）把已蒸馏好的档案**应用**到本项目。

只认第 1 条，就会把第 2 条的项目判成「还没跑过文风蒸馏」——
而同一时刻「档案审查」却是 done（档案确实在），链条自相矛盾。
实测：北荒刀歌真书档案的两个项目（`断刀试写` / `续写验证`）
`distill_runs=0`、`style_narrative` 各 331 KB / 364 KB，全中。

**修法**（`app/agents/workflow.py::_distill`）：判据改成看**产物**在不在位。

- 项目上有生效的 `style_narrative` → done（说清来源：跑过 / 从蒸馏库应用）；
- 蒸馏库里有同一份语料的 active 档案（还没应用）→ pending，
  理由直接告诉用户**去应用**（「蒸馏库里已有这份语料的文风档案（42 条规则），
  应用到本项目后这一步才算完」），而不是含糊的「还没跑过文风蒸馏」；
- 复用键用 `source_ids_hash` —— **蒸馏库自己那套归一化**
  （`app.style.narrative._source_ids_hash`），另算一套哈希会在语料顺序不同时对不上。

修完在真书项目上的实际效果：

```text
corpus_ready     done     参考语料已切分 5323 片。
clean_check      done     抽检通过（5 条提示不阻塞）。
distill          done     文风档案已就位（从蒸馏库应用）。   ← 修前是 pending
review_narrative done     蒸馏产物审查通过（15 条提示不阻塞）。
review_prompt    blocked  提示词体检有 2 个必须先处理的问题。
write_body       done     已写出 5 段。
review_body      pending  还没做整段验收（要先跑闭环审计）。
repair           done     没有待批的写回建议。
converged        blocked  卡在「提示词体检」。
```

## 3. 重审结果（真实 MiniMax-M3）

工件：`%LOCALAPPDATA%\hermes\cache\scratch\stage3\real-audit-relay9\`
（`report.json` / `acceptance.json` / `isolated.db`）。

| 环节 | 结果 |
|------|------|
| 封闭清单审计 | run_id=94，**4380 个候选全部裁决**，覆盖完整（`coverage_complete` / `verdict_scope.complete`），**违例 0**，`unknown_checks=28`，`rejected_mentions=19`，**新增模型调用 0**（复用 434 个检查点离线复算） |
| 整段验收 | **accepted**，`unresolved=[]`；4 项检查（时间/地点/参与者/物品状态）全部 `supported`；coverage 逐段回抄 **876 段**、`complete=true` |
| 开放项 | 28 条**可见但不阻塞**（4 处自相矛盾降级项 + 2 处地点存疑 + 22 条 `inventory_unlocated`） |
| 解锁 | refused / `nothing_to_unlock` —— 正文未被修订 → 没有守卫 → 无可解锁（不是失败） |
| 源库 | `source_unchanged = true`（受保护源库只读，未被碰） |

验收的引文是**逐条验过位置**的（`location_verified=true`），例如：

```text
acceptance_time  supported
  G1P001  「二十二点整，值班室墙上的电子钟跳了一格。」
  C1S887  「2026年11月3日22:00至23:00」（权威契约节点）
  G5P225  「陆宁在当夜记录的末尾添上："本次核对于二十四点前结束，后续事项保留待查。"」
```

**为什么审计能 0 调用复算**：正文没变 → 候选与判定口径没变 → 检查点键命中；
这是「续跑负缓存 + 检查点」那套机制的直接效果（协议版本戳不参与寻址）。

## 4. 顺带在真书档案上跑出来的两个**误报**（提示词体检）

工序链的「提示词体检」在北荒刀歌真书档案上判出 **2 个 error 级**问题：

1. 「原创模式把参考书的角色声音词典带进了提示词」
   （`prompt_voice_leak_in_original`）
2. 「档案的禁用词与档案正文自相矛盾」（`prompt_forbidden_self_conflict`）

**两条都是误报**，而且是同一个毛病：**判据查错了对象**。

### 误报一：查的是「档案里有没有」，不是「提示词里有没有」

原创模式的 `PROFILE_RULES['original']['include_voice'] = False`，
`narrative_dims` 也不含「角色声音词典」——**原创模式的渲染块里根本没有它**。
但检查查的是 `method_rules(narrative)`，也就是**档案**里有没有这个维度，
于是任何含真书档案的项目在原创模式都被判成泄漏。

证据有两处：

- 档案里留着「角色声音词典」本身是**对的** —— 续写模式写的就是原书角色，
  角色聊天模式也要它。按老口径的建议「把该维度从档案里去掉（重新蒸馏时排除）」
  去改，会**打断续写**。
- 它给的另一条建议「或修 `profiles.PROFILE_RULES['original']['include_voice']`」
  让人去修一件**早就修好**的事 —— 判据查错了对象的典型信号。

**修法**：判据改成比**渲染结果** —— 渲染块与「不含声音词典」的版本不同
才算泄漏。这样它守的是**不变量本身**（原创模式的提示词不得含参考书声音），
而不是一个代理指标。防护没有跟着消失：有测试把 `include_voice` 改成 `True`
（模拟真的漏了），断言检查照样报出来。

### 误报二：拿**渲染块**去比，而渲染块里就含着禁用清单本身

`describe_narrative` 会把反向禁忌段渲染进提示词：

```text
【反向禁忌：参考作者从不使用以下表达，严禁出现在生成文本中】
【禁用转折词】禁止使用：然而、但是、不过、与此同时、紧接着、下一刻、旋即、随即
```

检查的写法是 `word in narrative_block` —— **禁用清单里的每个词都必然在里面**，
所以只要档案有禁用清单，这条检查**恒真**（真书档案有 42 个禁用词 → 必报）。

**修法**：比的对象是**规则正文**（把渲染块在 `【反向禁忌` 处切开），
真矛盾长这样：「规则在教模型用『与此同时』，而清单同时禁『与此同时』」。
禁词只出现在清单自己里 → 不是矛盾。

### 修完之后的真书档案

```text
profile=original      0 个问题
profile=continuation  0 个问题
```

真书项目的工序链随之自洽（`gates` 为空）：

```text
corpus_ready     done     参考语料已切分 5323 片。
clean_check      done     抽检通过（5 条提示不阻塞）。
distill          done     文风档案已就位（从蒸馏库应用）。
review_narrative done     蒸馏产物审查通过（15 条提示不阻塞）。
review_prompt    done     提示词体检通过（0 条提示不阻塞）。
write_body       done     已写出 5 段。
review_body      pending  还没做整段验收（要先跑闭环审计）。
repair           done     没有待批的写回建议。
converged        pending  还没到「正文审查」。
```

**这两条误报的实际代价**：生成闸门是「提示词体检有 error 就 409」，
所以在修之前，**真书项目连正文都写不出来**（闸门把门关死了）。
误报不是「报告不好看」，是**把真活堵住了**。

## 5. 复现

```powershell
# 审计 + 验收 + 解锁（复用检查点 → 审计 0 调用）
$env:PROJECT_ID='7'
$env:CLOSED_INVENTORY_CONCURRENCY='4'
python tools/run_closed_inventory_full_audit.py `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\real-audit-relay9" `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\real-audit-relay-ckpt" `
  "$env:LOCALAPPDATA\hermes\cache\scratch\stage3\cross-relay\work-1\cross-relay.db"
```

## 6. 这一轮做了什么、没做什么

**做了**：

- 阶段三那篇在真实 MiniMax-M3 上把「审计 → 整段验收 → 解锁」整条链路走完，
  结论是**审计 0 违例、整段验收 accepted**（§3）；
- 挖出并修掉两个真缺口：验收失败不留错误消息（§1）、工序链的「文风蒸馏」
  只认一种产物来源（§2）；
- 挖出并修掉提示词体检的两个**误报**（§4）—— 误报把真书项目的生成闸门关死了。

**没做**：

- **派生重建 / 解锁**没跑成「解锁」：正文未被修订 → 没有守卫 →
  `nothing_to_unlock`。这不是失败，但也**不算**「解锁路径已验证」。
- **真书全流程验证**（新项目从交稿起步，走完九道工序）还没做：本次是拿
  **已有正文**验审计与验收两段。真书项目的工序链现在停在「正文审查」
  （待跑整段验收），前七道工序都已用真实数据走通。
