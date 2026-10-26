# 阶段2：剧情权威与参考可见范围

输入：Project.story_authority 为 generated_branch（默认）或 reference_anchor；reference_cutoffs 为 [{novel_id, chapter}]，章末 inclusive，每本参考书最多一个边界。章号必须在对应已选语料中真实存在。null 清空 cutoffs，authority null 非法。旧 continuation_start_chapter 仅保留兼容，不授予原文可见性、不随生成段数推进。

reference_anchor 只在第一本选定参考书有 cutoff 时提供位置前文；无边界显示待定位。已有生成正文时自动以生成分支为当前权威，原文位置降为参考。generated_branch 可以在显式 cutoff 内取参考，但不得声明必须衔接原著。所有参考 RAG、few-shot、L2 共用只读 VisibilityPolicy。无边界不注入原文实体内容；保留具体排除原因和来源诊断。原创脱敏样例也遵守显式 cutoff，保守减少样例。

L2 仅接受 novel_id + 非空 source_chapters（已有蒸馏格式）；全部来源章均可见才允许 whole-entry。缺来源/错书/混合未来章整条排除，记录 warning。粗粒度来源不证明事实发生时间，此处只保证消费来源边界，不宣称恢复作者叙事意图。当前角色清单不从任何参考 RAG 反推。

共享 build_context 负责边界；同步、流式、预览、重生成使用其中裁剪后的 narrative。预览禁止 provider 和 db 传入规划读取函数，增加 rag_blocks/kg_slice 与真实组装参数一致性；不在本阶段重构整个时间线 assembler。预览与生成的自动规划/摘要滚上差异仍需单独标明。

验收：先真实内存数据库 build_context→system/user/constraints 的 RED；四段生成无 anchor 不出现原著必须衔接、cutoff5 的 chapter6 哨兵在 RAG/fewshot/L2 皆不可见；API null、错书、缺章、重复边界、非法authority；预览无规划调用或落库。专项、父14项、受影响独立脚本；真实库只读 backup 到 scratch，迁移副本并组装最终 prompt，禁止 LLM/重新蒸馏。输出机器报告和独立提交，不提交父未跟踪设计文档。

## 界面入口与路径推导（2026-10-25 补）

这一段记的是上面那条设计**怎么落到界面上**，以及补上的一处断点。

界面（向导步骤①「参考语料」下方）给的是二选一：**只学写法** / **接着这本书写**。选后者时逐本填「故事进行到第几章」，提交为 `story_authority='reference_anchor'` + `reference_cutoffs=[{novel_id, chapter}]`；选前者则提交 `story_authority='generated_branch'` + `reference_cutoffs=null`（显式清空：旧边界会**优先于**写法参考路径的整本可见，白白限制素材）。章节上界来自 `GET /api/novels` 的 `max_chapter_no`（按 `novel_chunks` 的 max(chapter_no) 算），没有它输入框就没有上界。

补上的断点：`story_authority` 以前只存不用，`profile` 又是请求级参数 —— 项目记着「锚定在参考书上」而生成时没显式传 `profile=continuation`，就按原创路径跑，边界形同虚设。现在 `app/generation/profiles.py:resolve_profile(project, profile)`：显式写了路径就尊重显式值；没写就按锚定方式推导（`reference_anchor` → 续写路径，否则 → 原创路径）。路由层（`routers/generation.py:_resolve_profile`，预检要用）与引擎层（`build_context` / `generate_next_segment` / `generate_next_segment_stream`）各调一次，函数幂等。

两个刻意保留的行为：`reference_anchor` 但**没给**边界时仍然 fail closed（整本不可见，不悄悄放开）；`reference_cutoffs=null` 仍然合法（清空后可以再设）。续写路径的重生成（`regenerate_segment*`）维持它原有的「按重写原书章节处理」口径，不受这条推导影响。

诊断（`visibility_diagnostics.excluded`）按 (kind, reason) **归并**：一条记录带 `count` 与最多 5 个定位例子，例子只留 `novel_id/chunk_id/chapter_no/idx`。原因很实际：样例采样会扫过全书，被边界挡掉的候选动辄几千段，逐条记账（且每条带正文）会让预检响应膨胀到几百 KB（真书实测 470 KB → 归并后 64 KB）。归并不损失可核对性 —— 要逐条明细就去语料里查那一章那一段。

