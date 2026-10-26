# 规划缺失设定与正文状态传递设计

## 目标

规划节点发现缺少时间、地点、参与者、前后状态或其他必需前提时，返回 `clarification_required`。响应必须给出问题、必需原因、依赖节点、已有证据和是否允许用户明确授权虚构；默认 `authorized_fabrication=false`。未知保留为 unknown，不能进入 verified，也不能 apply 到项目正文规划。

草案和诊断都绑定当前规划输入的 SHA-256 fingerprint。apply 重新计算 fingerprint，旧 candidate 返回 409，避免旧诊断覆盖新计划。通过后才写入 outline/timeline；写入后回读由现有 API 完成。

## 状态传递

同一结构化节点字段由 `timeline_for` 生成 entry/exit：时间、地点、参与者、premise、motivation、state_before 进入 entry；action、result、cost、state_after 进入 exit。已有 `build_node_prompt` 消费 entry/exit，因此同步、流式、重生成和预览共用该节点状态边界，不新增另一套 prompt 规则。

## 验收

- unknown 产生 `clarification_required`、`verified=false`、逐字段 clarification。
- current fingerprint 不匹配时拒绝 apply。
- unknown candidate 不落库；已确认 candidate 才能落 outline/timeline。
- 节点 prompt 可见时间、地点、参与者和 entry/exit 状态。
- `candidate_only` 诊断提案不能被当作已验证修订。
