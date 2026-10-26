# 基础时间线：预算与可核验证据

范围：隔离基础分支，合并已提交清洗修复；不涉及助手调度、文风契约或全量蒸馏。原指定 narrative-foundation-design.md 不存在，以 narrative-foundation-audit.md 为审计依据。

输入：已规划节点、当前生成正文和 generation 段号。输出：节点段数预算状态、三态退出条件判定和可定位引文，持久化在 plot_timeline v2。entry/exit/title 缺失、占位或规划节点数量不符时 needs_planning，不自动视为完成。

完成规则：advance 只计数/节拍，预算耗尽置 budget_exhausted；done 仅由 supported 且引文程序核验通过的结果触发。判定覆盖整段正文，不截末1200字。模型输出 verdict=supported/contradicted/unknown、reason、sources=[{source_id,quote,start,end}]；source_id 使用 generation 的段号标识，start/end 是该正文 Unicode 字符偏移。程序校验 source_id、整数边界和 text[start:end]==quote；模型省略偏移时仅对唯一逐字引文计算偏移，重复引文或显式错偏移拒绝。无引文、裸 bool、错误偏移、异常都降为 unknown。引文证明模型结论可定位，不等于程序能证明任意自然语言语义。无需强造代价。

预算耗尽且未证实时，状态 needs_review 暂停下一次生成，返回可解释的 409（保留当前节点和判定）。用户需调整计划/预算或确认正文，再继续；不无限自动重写，不悄悄跳过节点。缺计划返回 needs_planning。预览只读取状态并显示诊断，不调用模型和落库。

迁移：v1 的 done 保留为历史完成并标 legacy_unverified，不伪造 sources；v2 保存 completion 证据。已有存档只读恢复时不重算/补写段数；提交后每段只计一次，正文段号用于幂等。旧状态没有可核验来源时不回填 supported。

验收：先 RED 预算末段钥匙仍未找到也执行判定且不 done；然后逐项覆盖裸 true、前1200字证据、伪造引文/offset/source、矛盾、异常、空规划与节点数量不足、v1 历史、证据往返、预算用尽生成暂停、只读预览；跑专项及受影响同步/流式/清洗/权威测试。真实验证只用现用 SQLite 只读 backup 副本，确认实际 Writer role，真实正文样本通过同一 judge 消费路径，报告真实模型和质量边界；不触碰原库/服务。
