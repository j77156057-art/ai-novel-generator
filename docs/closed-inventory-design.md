# 显式封闭设定检查（experimental v1）

输入为限定 segment_nos 的完整正文和各段历史保存 authority；不使用当前 premise 回填。独立 capability read_closed_inventory 复用正文读取并为历史 authority 叶子生成服务端短 ID、坐标、契约哈希。保留 body_consistency v1/v2。

Assistant 任务拆为：契约约束提取（object/person/place 各自 open/closed，默认 open；closed 必须给逐字明确完整/仅有的源引文和授权集合）；完整正文按连续段落分批提取 mention ledger（规范实体、字面提及、source ID、actual/memory/hypothetical/metaphor/negated/quoted/unknown）；服务端与 closed 集合对比形成候选；独立 Assistant 判断 violated/allowed/unknown，引用契约与正文两端。无缺省封闭、无 fixture 物件硬编码。不把引用/隐喻/否定直接判成存在事实。

每个正文 source 必须在对应批次 covered_source_ids 中明确出现。批次不截取首尾。协议完整仅证明模型看过每段，不能证明实体召回完全；最终结论必须注明此限制。缺来源、缺覆盖或缺候选裁决严格失败/unknown，不能全局 supported。引文由服务端定位，模型不得伪造坐标。抽取与裁决原响应独立落审计；请求量按实际 chat 次数计数。

诊断只写 AgentRun/Evidence。evidence_ref 绑定数据库位置、task UUID、input fingerprint（含契约内容），run 回读持久化。controlled repair 本轮仅输出带 authority→body 两端证据的受控建议，execution_available=false；现有 prepare_body_repair 仅支持正文两端，不能宣称已支持契约保留或自动编辑。相关使用/依赖未选择完整范围前须澄清，不能自动删除剧情实体。默认不调用 Writer，不自动 apply。

验收：先离线 RED，跨题材开放集合/别名/比喻/否定/引用测试；旧专项回归；已有 full-contract-real-v2 正文做一次有界任务，只检查既有产物，不重新规划或生成。真实检出至少一处已知新增后停止；不追求全绿。库和工件隔离，原库 hash/stat 前后相同。一次真实实验不晋升 validated。
