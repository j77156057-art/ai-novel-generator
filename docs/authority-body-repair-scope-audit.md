# 真实正文修复范围与 apply 闸门审计（只读，未实施）

> 本文提到的 `scratch/…` 是本地工作目录下的脚本与中间产物，**不随仓库分发**。

## 0. 本文件的性质

只读审计，未调用模型、未写数据库、未改代码。数据来自
`scratch/authority-body-repair-real-v1/isolated.db`（只读打开，URI `mode=ro`）中
run79 的诊断报告与 generation 10 的正文。脚本与产物在
`scratch/authority-body-repair-scope-audit/`（`scope_audit.py`、`dependency_map.py`、
`body-10.txt`、`dependency-map.json`）。

本文档给出三件事：真实正文的违例依赖链、上一轮确认卡的实际缺陷、以及**当前 apply 闸门在结构上
不可达**的证据。

## 1. 正文与提及账本的真实形状

- generation 10 正文长度 **2848 字符**（含换行），低于阶段三要求的每段 3000 汉字。
- 提及账本共 96 条 source：**83 个正文段落**（G1P001…G1P083）+ **13 条契约来源**（C1S084…C1S096）。
- run79 的 scope：`{'segment_nos':[1], 'inventory_kinds':['object'], 'limit':5, 'cursor':0}`，
  skill `closed_inventory` v4，acceptance = `explicit_closure` + `full_scope_coverage`。
- run79 的 inventory：`coverage_complete=False`、`global_quality_verified=False`、
  `summary={'partial':True,'success':False,'pending_entities':179,'adjudicated_entities':16}`、
  `counts={'mentions':212,'authorized_entities':17,'pending_entities':195}`。
- **`verdict_scope.source_ids = ['G1P001','G1P005','G1P006','G1P009']`，`complete=false`**
  —— 83 个正文段落里只有 4 段被裁决过，其余 **79 段没有任何裁决**。
- `reviewed_dimensions=['object']`，`unreviewed_dimensions=['person','place']`
  —— 人物与地点维度**从未审过**。
- 账本 `sources` 里 C1S096 是权威起始物品清单来源（起始物品完整清单 + 三节点 + 证据目标）。

### 1.1 run79 的三项非 supported 检查

| check | 状态 | 证据 | 说明 |
| --- | --- | --- | --- |
| `object_N011` | contradicted | C1S096 + G1P005 [216,273) | 小凳子未在权威起始物品清单中 |
| `object_N013` | contradicted | C1S096 + G1P006 [275,349) | 棉质手套／棉套未授权（**已有候选，proposal 1 pending**） |
| `object_N017` | unknown | C1S096 + G1P006 [275,349) | 候选词「加固纸」与引文所指实体不符，**不是已确认违例** |
| `object_N021` | contradicted | C1S096 + G1P009 [403,475) | 手机未授权 |

其余 16 项为 supported。

## 2. 手机违例的真实依赖链（5 段，不是 1 段）

上一轮交接说明里写「手机需要处理照片、计时器及后文照片使用的依赖链，不能只删首次手机提及」。
从真实正文逐字定位后，这条链的精确形状是：

| source | 区间 | 正文 | 依赖 |
| --- | --- | --- | --- |
| G1P008 | [391,401) | 「要不要先拍张照？」 | 拍照提议 |
| G1P009 | [403,475) | 「……陈舟从桌面下方拿出手机，关掉闪光灯，对着封面拍正面和侧面各一张……直接把手机放回桌面。」 | 手机本体（唯一被裁决 contradicted 的段） |
| G1P010 | [477,516) | 「林夏俯身看了两张照片，确认角度里能看见翘边和书脊……」 | 使用拍成的照片 |
| G1P041 | [1298,1406) | 「核对继续往后走，桌上的手机计时器已经过了三十多分钟……」 | 手机计时器 |
| G1P060 | [1824,1864) | 「林夏没有追问第二遍，她把图册保持原样，低头查看登记本空白处，又取出那张封面照片。」 | 后文再次使用照片 |

关键几何事实：这 5 段分布在正文的 [391,1864)，**跨度 1473 字符**，中间夹着 40 个与手机无关的
段落（G1P011…G1P040、G1P042…G1P059）。

⇒ **不能用一个连续区间覆盖这条链。** 最小连续覆盖会把 1473 字符、约 40 个无关段落拖进修订范围，
直接违反「范围外正文保持逐字不变」。这条链只能拆成 5 次定点修订。

### 2.1 生产修复契约只有一个连续区间

`body_repair._request` / `authority_body_repair.validate_authority_request` 都要求
`0 <= start < end <= len(text)` 的**单个**区间，且：
- `conflict_source_id` 必须指向被裁决 contradicted 的检查所引用的正文 source；
- `preserve_source_ids` 必须是该 source 中区间之外的段落（否则 `repair_scope_denied`）。

而且 `validate_authority_request` 取 `targets[0]` —— 一次请求只服务一个 contradicted 检查。
**实测核对（`phone-chain-issuability.json`）**：这 5 段里只有 G1P009 挂了检查
（N020/N021/N022/N023/N024，其中仅 N021 为 contradicted）；G1P008、G1P010、G1P041、G1P060
在 run79 里**一条检查都没有**（既非 supported 也非 contradicted，只是没被裁决）。
因此现状下**没有任何一张合法的确认卡能覆盖它们**。

⇒ 恢复设计必须补一条：要么支持「依赖链」范围（多区间、共享同一违例根因），
要么先对这 4 段产出独立诊断证据。两者都超出当前契约。

## 3. 上一轮的「手机确认卡」是坏的

`scratch/authority-body-repair-real-v1/phone-confirmation-card.json` 与 `confirmation-card.json`
**字节数完全相同（2931）**，内容只差一处：`conflict_source_id` 与 `scope` 换成了 G1P009 的
[403,475)，**objective 仍然是「消除未授权手套和棉套的全部现实依赖」**。

也就是说：这张卡会把「删掉手套和棉套」的要求发给 Writer 去改**手机**那一段。
既不会删掉手机，又会让手机段落凭空丢掉手套描述——目标与范围错配。
按现状确认它，等于制造一个新的错误候选。

## 4. apply 闸门在结构上不可达（新增结论）

上一轮交接把阻塞归为「复验范围混合」「全局验收无恢复转移」「pending 提案无重新审核入口」
三项。逐行核对后，这三项可以合并成一个更硬的结论：

**当前实现下，任何修复提案都无法被 apply——不是「这次没通过」，而是「永远不可能通过」。**

证据链：

1. `assistant_proposals.py:451`（approve）与 `proposals.py:948`（apply）都要求
   `scope['semantic_accepted'] is True`，否则 409。`apply-selected` 复用同一核心，
   `versions/{id}/restore` 只能恢复既有 Version 快照（当前 Version=0，无快照可恢复），
   没有旁路。
2. `semantic_accepted` 只在 `body_repair.py:201-203` 计算：
   `completed` + checks 非空 + 全部 supported + **`not verification['unresolved']`**。
3. `verification['unresolved']` 在 `authority_body_repair.py` 装配：
   - `:86` 起始 = `previous['unresolved']` 原样继承；
   - `:118` 每个非 supported 原检查追加 `original_check:<id>`；
   - `:120` `if not previous['inventory']['coverage_complete']` → 追加 `inventory_partial_coverage`；
   - `:121` `if not previous['inventory']['global_quality_verified']` → 追加 `full_scope_acceptance_unverified`。

第 3 步的第 120、121 条读的是**原诊断 run79 的 inventory**，正文修订不会改变它。
run79 的 `coverage_complete=False`、`global_quality_verified=False`，因此这两条**恒定追加**。
⇒ 即使把 N011、N013、N017、N021 全部修好、全部 supported，`unresolved` 仍非空，
`semantic_accepted` 仍为 false，approve/apply 仍 409。

反过来说，当前闸门隐含要求：**必须先有一次覆盖完整、全局通过的验收**，才能落地任何一处修复。
而那一次验收又必须在「修好的正文」上做。正文修不好就无法验收，正文不修就无法验收——**死锁**。

### 4.1 解除死锁需要什么（两条路，都必须先审阅）

- **路线 A：解耦局部修复闸门与全局验收。** 允许「目标违例已解决 + 边界逐字保留 + 保留事实未被改动」
  的局部修复被 apply，同时把全局验收义务**重新绑定到新正文哈希**（不是删除、不是缩小）。
  全局验收仍在最后由整段复验关闭；在此之前生成守卫 `body_revision_requires_review` 持续拦截生成。
  现有代码里 `assess_local_repair` 已经算出 `local_repair_verification='accepted'`，
  但 `apply_allowed` 恒为 false、apply 闸门只看 `semantic_accepted`——**这个接缝就是要改的地方**。
- **路线 B：先做出一次完整覆盖的全局验收。** 在**未修订**的原正文上先跑一次有界、完整覆盖的
  封闭清单审计，得到 `coverage_complete=True` + `global_quality_verified=True` 的验收，
  再逐处修复、逐处把义务重新定位。
  实测成本口径：当前 83 个正文段落里只有 4 段被裁决，**79 段无裁决**；179 个待审实体；
  `person`/`place` 两个维度**从未审过**。要拿到 `full_scope_coverage` 的证据，
  需要一次覆盖全部段落的有界批次审计（仓库有 checkpoint 机制，可断点续跑），
  成本高且与「不为清零盲目重扫」的口径冲突，需要用户明确取舍。

路线 A 改动的是**受控写回的准入条件**，属于「受控审核转移」，必须走设计审阅，不能顺手改。

## 5. 修复顺序与哈希漂移（执行层约束）

三个修复点彼此不相交：手套 G1P006 [275,349)、小凳子 G1P005 [216,273)、手机链 5 段。
但**每次 apply 都会改变正文哈希**，其余提案的 `base_content_hash` 立刻失效（stale）。
因此：

- 修复必须**串行**：一次 apply → 回读新正文哈希 → 用新哈希重算下一张卡的 `base_content_hash`；
- 手机链的 5 段同理，第 2 段的卡必须在第 1 段 apply 之后重新生成；
- 每次 apply 都会重置 `body_revision_requires_review`、清空摘要/completion，
  并把 `derived_state` 置回 `kg/facts = pending_rebuild`——**中间态不可生成，这是对的，不要绕过**。

## 6. 建议的修复顺序（待用户确认后执行）

1. **手套 G1P006 [275,349)** —— 已有候选（proposal 1 pending），已完成一次局部复验
   （run81：object_state supported，引用 C1S096 + G1P006；time/place/participants 为局部范围下的 unknown）。
   只需在闸门解耦后走 apply + 精确回读。
2. **小凳子 G1P005 [216,273)** —— 单段、独立 contradicted 检查（object_N011），
   现有契约即可出卡；objective 应为「去掉未授权的小凳子，保留本段其余对话与动作」。
3. **手机链 5 段** —— 依赖新的「依赖链范围」契约或 4 段独立诊断证据（见 §2.1）。
   objective 应统一为「删除手机及其派生的拍照／照片／计时器依赖，不得引入新物件、动作或结果」，
   并明确要求 5 段之间的引用关系（照片怎么替代、计时改用什么）保持自洽，
   **不得由 Writer 自行发明替代道具**——替代物必须同样落在权威起始物品清单内
   （图册、无酸纸、浆糊、软刷、压板、登记本、铅笔）。
4. **object_N017（加固纸，unknown）** —— 不作为违例删除。正文有 3 处提到「加固纸」：
   G1P002「右上角的加固纸已经翘开」、G1P007「看得到加固纸的内边发黄」、
   G1P082「翘起的加固纸仍旧悬在那里」。三处都在第一节点（09:00–10:00，图册未修复、
   无酸纸未使用），字面读都指向**旧图册自带的封面加固纸**（图册既有状态），而不是权威清单里
   尚未使用的无酸纸。但 run79 的裁决是 unknown（候选词「加固纸」与引文所指实体无法对应）。
   ⇒ 需要一次独立裁决给出结论；**本文件的读法不是裁决，不得据此把 N017 当 allowed 放行**。
   裁决问题已固化在 `scratch/.../N017-adjudication-question.json`。

## 6.1 本轮已备好的确认卡（只读产物，未提交、未调用模型）

位于 `scratch/authority-body-repair-scope-audit/`：

| 文件 | 范围 | conflict | 可否现在用 |
| --- | --- | --- | --- |
| `card-01-glove-G1P006.json` | [275,349) | G1P006 | 可（已有候选 proposal 1；本卡仅核对 objective 与范围一致） |
| `card-02-stool-G1P005.json` | [216,273) | G1P005 | 可（object_N011 contradicted） |
| `card-03-phone-G1P009.json` | [403,475) | G1P009 | 可（object_N021 contradicted）——**只覆盖手机本体那一段** |
| （缺）G1P008 / G1P010 / G1P041 / G1P060 | — | — | **不可**：无检查，且现行契约只支持单区间 |

三张卡的 `base_content_hash` 已用 `sha256(body)` 复算，与上一轮卡使用的
`e218e20d…f11c531` **完全一致**，确认取证口径没有漂移。
⚠ 三张卡同时可用**不表示可以并行**：每张卡的 `base_content_hash` 都绑定当前正文，
一次 apply 后其余两张立即 stale，必须回读新哈希重算（见 §5）。

## 7. 本轮未做 / 未验证

- 未生成任何候选，未调用 Writer/Assistant，未写任何数据库。
- 未确认修复方案是否被用户接受，因此未推进到「诊断证据 → 用户确认」之后的任何一步。
- 未验证路线 A / 路线 B 哪个可行——两者都需要先审阅设计。
- 未评估完成 179 项未裁决提及的实际模型成本与批次边界。
- 本文所有行号对应 `release/foundation-delivery` HEAD `d700597`。
