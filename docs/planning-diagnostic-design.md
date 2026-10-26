# 实验规划诊断

输入为项目当前 premise、plot_bible、outline、plot_timeline.planning。能力 inspect_planning_review 只读取当前项目，沿用项目 reference scope 授权，不读取任意 SQL 或附件。服务端按确定顺序展开叶值，赋予 S001 等短 ID，保留真实 JSON pointer path、完整文本及本字段字符坐标。ID 只在该计划内容指纹下有效，模型不能猜测路径。cursor 选择单个节点；不发送完整 outline 或其他节点，coverage 标明未覆盖节点。默认仅诊断 evidence_responsibility，其他维度未验证。

planning_review 实验 skill 描述四类检查，当前执行器仅选择证据责任机制这一类。模型返回精简 findings：assessment/reason/source_ids，枚举选择短 ID；服务端附加真实 path/quote/start/end 并核验，未知 ID 失败，不映射错误路径。JSON 截断保存 raw 且维持 failed。定位通过只是定位，不代替语义判断；completed 仅表示本批执行与解析成功，不表示整计划或质量通过。未知保留。

修复为正式 Writer generate-outline preview 的新候选。候选保存在独立工件；仅在隔离副本显式 apply 并回读后，同 scope repair_verification 一次。旧计划及两次诊断均保存，不自动覆盖现用库，不生成正文。诊断仅可写 AgentRun/Evidence，计划及圣经变更纳入指纹。skill 保持 experimental。

验收：缺能力/非法 skill 拒绝、scope 越权拒绝、预览纯读、null schema 保守、逐条证据定位、输入变化复验、真实角色模型与审计回读、原库指纹不变。
