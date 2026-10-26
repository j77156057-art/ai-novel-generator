# 受控修复恢复路径补全（待审阅，未实施）

> **v2 修订（2026-10-23）**：新增 `docs/authority-body-repair-kg-fact-audit.md`，只读审计已查清
> KG/Fact 入口，并发现本方案第 6 条有两处前提不成立——生成守卫全仓无清除路径、Fact 账本无重建入口。
> 第 6 条已按审计结论拆为四个可执行子项，并新增第 7 条（生成解锁过渡）。
> v2 仍为**待审阅、未实施**。

## 当前结论

阶段二仍失败。协议修复后真实Assistant run81确认手套依赖消除，但时间、地点、参与者以局部范围未覆盖为由返回unknown。该结果不能解释为三维存在矛盾，也不能改写成supported。原始pending提案不升级。

离线证据见authority-body-repair-remaining-audit.json；完整输入与响应来自scratch/authority-body-repair-review-v2，不新增模型调用。

现有实现缺少三个必要转移：候选完整语义复验、复验通过后对同一pending提案的显式审核更新、KG/Fact受控重建与生成解锁。现有局部复验固定global_quality_verified=false并继承unresolved；重复调用不能解除这些缺口。

只读审计（`docs/authority-body-repair-kg-fact-audit.md`）进一步把第三项拆清：KG全量重建入口**已存在**（`build_graph_from_text`），但Fact账本**没有重建入口**（`upsert_facts`只增量累加，`_clear_graph_by_source`只清KgNode/KgEdge），且`body_revision_requires_review`**全仓无清除路径**——即使前两者补齐，生成仍会一直409。因此第三项实际需要三段实现，不是一段。

## 最小方案

复用EditProposal、AgentRun、Evidence、Version及既有API/诊断能力，不加表，不更改ReAct核心。

1. 增加明确的候选复验操作。输入只有project_id、proposal_id、base_content_hash和显式确认；服务端读取原诊断、冻结契约、候选与之前复验，不接受客户端验收结论。重复成功请求复用结果；失败重试必须显式且保留每次审计。
2. 将目标修复判断与整段契约判断分别记录。目标判断可展示“手套依赖消除已有证据”；只要其他维度unknown或原违例未解决，整体及apply仍false。全段检查使用完整候选正文和冻结权威，避免要求局部文字重新声明全部时间地点。每个结论仍需服务端来源定位。
3. 在调用模型前返回确定的剩余阻塞。小凳子、手机仍未修复，不发无意义的全段验收请求。关联依赖先由确认卡明确修复范围；手机的照片/计时器/后文照片使用属于同一依赖链，不能只删首次手机提及。范围外正文保持逐字不变，不能自动扩写整章。
4. **（v2新增）解除apply结构死锁。** 审计证明当前闸门不可达：`semantic_accepted`依赖`unresolved`为空，而`unresolved`中的`inventory_partial_coverage`与`full_scope_acceptance_unverified`恒定读自**原诊断**run79的inventory（`coverage_complete=False`/`global_quality_verified=False`），正文修订不改变它——因此即使所有违例都修好，approve/apply仍409。需要新增一条受控转移：**局部修复的准入与全局验收解耦**——允许「目标违例已解决+范围外逐字不变+保留事实未被改动」的局部修复被apply，同时把全局验收义务**重新绑定到新正文哈希**（不删除、不缩小、不把未审等同allowed）。全局验收仍由最后一步整段复验关闭；在此之前`body_revision_requires_review`持续拦截生成。现有`assess_local_repair`已能算出`local_repair_verification='accepted'`，但`apply_allowed`恒false且apply闸门只看`semantic_accepted`——改的正是这个接缝。**这属于受控写回准入条件变更，必须先审阅。**
5. **（v2新增）支持「依赖链」范围。** 手机链跨5段（G1P008/G1P009/G1P010/G1P041/G1P060），跨度1473字符、中间夹40个无关段落，单个连续区间会破坏「范围外逐字不变」。且其中4段在run79里没有对应的contradicted检查（它们只是手机链的下游，未被单独裁决），现状下**没有任何一张合法确认卡能覆盖它们**。需要二选一：①修复契约支持多区间（共享同一违例根因、范围外逐字保留）；②先对这4段产出独立诊断证据再逐段出卡。同时约束：替代物必须落在权威起始物品清单内（图册/无酸纸/浆糊/软刷/压板/登记本/铅笔），不得由Writer自行发明替代道具。
6. **（v2新增）object_N017单独裁决。** 「加固纸」是unknown而非confirmed violation，不得当违例删除。需一次独立判断：正文G1P002「右上角的加固纸已经翘开」指旧图册自带的封面加固纸（则非新增物件，应判allowed），还是权威清单里的无酸纸（则在第一节点使用即违反「无酸纸未使用」）。该判断必须由独立复验给出，服务端不补判。
7. 原coverage缺口保留为缺口；不得通过删除unresolved、缩小旧acceptance或把未审等同allowed解除。沿用原提及账本及来源映射，候选改动导致失效的项重新定位；需要全范围验收的证据必须有有界完整覆盖，不能以179项未经裁决宣告全部正确。若无法完成，返回具体未覆盖范围，停止应用。
8. 仅当新独立复验严格满足同一候选的全部验收要求时，将新run绑定pending提案；旧失败run不改，版本仍只在正式apply产生。指纹同时绑定当前正文、候选、冻结契约、原诊断及验收范围，任何漂移409。
9. 正式apply后先精确GET/SQL回读正文与唯一Version，再恢复派生状态。摘要/completion沿用现有实现；KG用既有全量重建入口`build_graph_from_text(..., source='generated')`（不用增量合并，避免修订前节点残留）；Fact按审计结论新增「按当前生成分支重建」原语，语义照抄既有`save_judge_findings`同段先清后写或`write_state`快照替换，且必须排除`type='exemption'`的作者豁免行。重建绑定当前正文指纹且隔离原著事实。任一重建失败保持stale。
10. 新增显式生成解锁过渡。审计确认：`body_revision_requires_review`在`proposals.py:591`设置、在`engine.py:424`拦截生成，但**全仓没有任何清除它的代码**，`rebuild_derived`只读它且刻意保留。因此即使KG/Fact/摘要全部重建成功，生成仍然409。解锁必须是独立可执行动作：仅当KG重建、Fact重建、completion重判三者全部成功且回读校验通过后，才清除该守卫并把`derived_state`从`pending_rebuild`改为已重建；任一步失败保持守卫与stale。不允许用改状态标签代替。
11. 解锁以`completion`/`done`重判结果为准，不能只凭「摘要已重建」判定恢复完成。
12. 修复必须串行：每次apply改变正文哈希，其余提案的`base_content_hash`立即失效（stale）。一次apply后必须回读新正文哈希，再用新哈希重算下一张确认卡；手机链5段同理。每次apply都会重置`body_revision_requires_review`并把`derived_state`置回`pending_rebuild`，中间态不可生成是预期行为，不得绕过。

## 验收与边界

先用保存真实响应离线RED/GREEN：局部非覆盖不能伪装全段通过；已知未修复项使模型调用数为0；客户端伪造、过期正文/契约/候选、错项目、重复批准应用均被拒绝。

按`docs/authority-body-repair-kg-fact-audit.md`补三条离线RED/GREEN：
①正文修订后重跑KG全量重建，修订前正文产生的节点（如被登记成角色的「手机」）必须消失——用增量合并会残留，测试必须能区分；
②Fact账本重建后，修订前正文产生的`source='generated'`事实行必须被清除，同时`type='exemption'`的作者豁免行必须保留；
③生成解锁过渡：重建未全部成功时生成仍返回409；全部成功并回读后`body_revision_requires_review`被清除、生成不再409——这是当前全仓不存在的路径，必须由新代码与新测试同时提供。

按`docs/authority-body-repair-scope-audit.md`再补三条离线RED/GREEN：
④解除死锁：在一个`unresolved`含`inventory_partial_coverage`+`full_scope_acceptance_unverified`的fixture上，局部修复（目标违例已解决、范围外逐字不变、保留事实未变）应可被apply，且全局验收义务**仍绑定在新正文哈希上**、不得被删除或缩小；不得出现「改`semantic_accepted`为true即可放行」的路径；
⑤依赖链范围：手机链5段的多区间修订必须只改这5段，跨度1473字符内的40个无关段落逐字不变；替代物必须落在权威起始物品清单内；
⑥串行漂移：前一次apply改变正文后，后一张卡的`base_content_hash`必须被判stale并要求重新出卡。

实际验收复用现有正文、候选和提及账本，不重蒸馏、不重新生成、不重扫实体。新模型调用必须对应已明确可推进的阶段，次数有界，成功阶段不重跑。真实完整通过前不启动阶段三，不部署为已验收能力。

该方案改变受控审核转移及恢复流程，按用户设计先审阅要求，本文件待审阅后实施。它不是已完成实现或质量通过证明。
