# 设定到正文受控修复（experimental）

仅消费数据库内同项目、同 Generation、已完成且模型参与的 closed_inventory 原始报告。重新读取完整正文和冻结契约，对 observation、正文、契约内容指纹及短 ID 逐项核验；仅 violated/contradicted 且显式 closed 的目标可以生成确认卡。客户端不能提交诊断或验收标记。

确认后复用既有 pending/version 通道，Writer 固定范围一次，Assistant 独立语义检查一次，输入完整冻结权威和候选正文，不重抽提及账本。局部检查要求引用权威端和改动端；保留原报告未覆盖项、其他违例与现有 acceptance。它们未解决则 semantic_accepted=false，审批和应用维持阻塞。

派生恢复为显式 POST，绑定项目所有当前 Generation 的 ID/正文哈希及冻结契约。摘要从当前正文重新生成；completion 通过现有带逐字证据的 judge 重新判定，不累计段数、不改冻结契约。模型失败不写成功状态，暂停标记保留。项目 KG/Fact 不删除且不重用旧状态，明确 pending_rebuild 并继续阻断生成。全部恢复完成以前不得解除暂停；无新增表或 schema。

验收：先 RED→GREEN；复用真实 run79 手套与手机证据，只读备份副本，记录源哈希/stat。一次真实候选及一次独立复验后按完整门槛判定，失败不重试、不推进阶段3。fixture 的 apply/重复409/stale/错项目/精确回读不能代替真实作品验收。
