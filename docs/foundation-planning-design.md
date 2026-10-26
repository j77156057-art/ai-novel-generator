# 剧情规划入口（experimental）

输入：现有项目 premise、已由用户确认的 plot_bible 与显式 goals；Writer 角色配置。新增 POST /api/projects/{id}/generate-outline，node_count 3–12，confirm_bible 必须 true；preview 默认 true，只返回候选。preview=false 显式应用，禁止覆盖已有生成正文的项目。

输出：兼容 list[str] outline，结构化因果节点（前提、动机、行动、结果、代价、时间、地点、参与者、状态前后），来源标记 model_participated/degraded/status/error。可未知的推断标 unknown，代价允许 NA，不强迫戏剧冲突；缺少字段属于 needs_planning。结构完整只是 draft，不能证明剧情质量。节点应用进入既有 plot_timeline，保留额外 planning 元数据并支持现有 timeline 读取。

build-bible 使用真实 Writer provider，不再依据旧 settings 白名单；无模型、请求失败、解析失败均显式 needs_planning，不写模板、不覆盖旧圣经/计划、不把异常字符串（可能含 key）返回用户。成功是 bible_draft，需用户确认；支持 preview 不写。

PATCH outline 接受严格 list[非空字符串]；省略不变，null 清空；变更清除旧 timeline（避免继续复用旧节点），status=needs_planning；相同值保留执行状态。保持原有 create 兼容。

验收：先 RED 再实现；HTTP schema/bad payload/null/回读、同值复用、预览零写、失败保留旧计划、Writer 轨道、结构化状态保存恢复。真实 sqlite backup 上先短 probe，再正式 bible + outline，保存产物和回读；人工检查时间、地点、人物、动机及因果可解释性。既有 409 needs_review 不放宽。不修改诊断和 ReAct，不隐式蒸馏。

补充：POST /api/projects/{id}/outline/apply 接收 generate-outline 的 candidate，校验输入指纹及完整节点后保存，避免预览后为了保存重复调用模型。指纹包含 premise、bible、原 outline 与 timeline，输入变更返回 409；已有正文项目拒绝覆盖。GET project 返回 plot_timeline，供用户及诊断复核实际草案。planning 元数据随 timeline 序列化、推进和支线切换保留。unknown/NA 是诚实标记，不是质量通过。
