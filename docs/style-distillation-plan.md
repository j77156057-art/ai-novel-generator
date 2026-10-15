# 风格蒸馏分支：风格增强三步走（P0→P1→P2）

> 分支：`feat/style-distillation`（worktree: `worktrees/style-distill`）
> 基线：master `9ccabab`
> 范围：风格层增强，与主线（事实层/校验层/助手能力）正交，互不触碰文件。

## 背景：当前状态与调研结论的落差

**已实现（本项目已有的风格能力）**
- `style/stylometry.py`：21 个纯统计特征 + 留出集分位验收（`fit_profile` / `style_distance` / `describe`），整轮生成恒定进静态 system 块
- `style/pipeline.py`：语料体检（SEO 水印检测剔除）→ 档案落库 → 按 `source_ids + unit_chars + version + schema` 指纹缓存
- `generation/prompts.py`：【文风档案】块进 system prompt（确定性、静态、不破坏缓存）
- `agents/audit.py`：`rule_generation_ai_flavor`（有档案时用 `style_distance` 的 `mean_abs_z`，无档案时用套话密度）
- `docs/验收指标.md`：人类基线实测分位（3 本真实网文 1,155,276 字）

**调研结论（为什么现有实现不够）**
- 数字是**验收工具**，不是**生成指导**。模型照着数字写，会得到"数字全对、读起来不对"的结果 [调研结论]
- 写作蒸馏器（writing-dna-skill）与小说风格蒸馏器（writer-novel-style-distiller）反复强调：**原文样例的 in-context 校准不可替代**——它传递的是"句子怎么呼吸、段落怎么接、什么时候突然用一个短句"这类文档描述不出来的东西 [10]
- 小说特有的 L2/L3 维度（人物描写手法、对白习惯、爽点节奏、主角依恋机制、断章惯例）当前**一项未覆盖**，全部特征都是 L1 表层 [11]

## 三步走

### P0：original-sample 注入（few-shot 原文样例）

**问题**：`build_context` 现在只产出 `rag_context`（按查询词语义检索的相关片段），没有"风格样例"这一路。风格样例的目标是"让模型照着这个语气、节奏、段落编排写"，不是"找和当前剧情相关的原文"。

**做法**：
- `build_context` 新增 `style_exemplars` 字段：从每个参考语料均匀采样 2~3 个自然段（每段 300~500 字，**与生成段同粒度**，与 `unit_chars` 对齐），按原文段落边界切，保证是完整段落而非截断
- 采样策略：**按结构位置均匀采样 + 体裁接近优先**，不是按语义查询——两者目标不同，不共用 RAG 那一路。优先取与当前 project 的 `genre` 匹配的章节，在全书范围内均匀分布，每本取 1~2 段，总量 2~3 段
- **排除已被 RAG 命中的 chunk**：`build_context` 里 RAG 已经用 `hybrid_search` 检索了 4 个 chunk，样例采样必须排除这些 chunk，否则同一段原文会同时作为"相关剧情"和"风格样例"出现两次，模型会困惑
- `build_user_prompt` 新增【风格样例】块：放在 RAG 上下文之后、续写指令之前，明确标注"以下是参考语料的原文段落，请学习其语气、节奏、段落编排与人物口吻，不要复用其内容"
- **样例块进 user prompt 不进 system**：样例每次生成从语料里现采样，内容随段号变化，进 system 块会击穿提示词缓存；system 块只放整轮恒定的内容（统计档案、叙事档案）
- 落库：样例是每次生成现采样，不进 `style_profile`（档案是统计量，样例是原文，两者分离）

**验收**：
- `test_style_exemplar.py`：采样数量、段落边界完整、不与 RAG 重复、`unit_chars` 对齐
- 手测：真实续写一段，肉眼比对样例段与生成段的节奏差异

**改动文件**：
- `backend/app/style/exemplar.py`（新）：采样函数
- `backend/app/generation/engine.py`：`build_context` 加 `style_exemplars`
- `backend/app/generation/prompts.py`：`build_user_prompt` 加【风格样例】块
- `backend/app/routers/generation.py`：`generate-plan` 预览里带上样例块（与 `generate` 口径一致）

---

### P1：生成后 style_distance 告警

**问题**：`style_distance` 建好了、`verify_stylometry.py` 会跑，但生成主链路（`generate_next_segment_stream`）不调用它。模型写了段 AI 腔爆表的段落，会照单全收落库，事后靠人点"审核"才发现。

**做法**：
- `generate_next_segment_stream` 落库后调一次 `style_distance(text, profile)`，`percentile < P_WARN`（暂定 20，见下方讨论）时在 `done` 事件里带 `style_warning` 字段
- 前端工作台生成结果处：若 `style_warning` 存在，段落旁加黄色标记"文风偏离档案，建议重写"
- **不做自动重写**：自动重写会触发额外 LLM 调用，且改出来的未必更好；给信号让人决定就够了

**阈值讨论**：
- `percentile` 语义：在参考语料人写的留出单元里，有多大比例的距离 ≤ 本段。100% = 比所有人类样本都更像本语料的正常文体
- **两条线方向不同，不冲突**：`verify_stylometry.py` 的离线验收用**高分位**（97.5）判"异常偏离"；生成链路的实时告警用**低分位**（20）判"明显不像"。高分位是"超过大多数人类样本的偏离程度"，低分位是"比大多数人类样本都更不像"，两者衡量的是分布的两端，不矛盾
- 候选低分位值 10 / 20 / 30。20 比较平衡：低于它基本是"明显不像"，但不至于正常波动就误报
- 具体值由 `verify_stylometry.py` 在真实语料上扫一遍 `unit_mean_abs_z` 分布后定，不拍脑袋

**验收**：
- `test_style_warning.py`：正常段不触发、AI 腔明显段触发、无档案时静默跳过（档案是增强不是前提）
- 手测：续写一段，观察 `done` 事件是否带告警

**改动文件**：
- `backend/app/generation/engine.py`：`generate_next_segment_stream` 落库后调 `style_distance`
- `backend/app/schemas.py`：`done` 事件加 `style_warning` 字段
- `frontend/src/views/ProjectWorkspace.vue`：生成结果处加告警标记

---

### P2：L2/L3 认知层提炼（LLM 分块深读 + 证据账本）

**问题**：当前 21 个特征全是 L1 表层。小说的 L2 结构层（开头钩子形态、段落编排模式、章末收束惯例）和 L3 认知层（人物描写手法、对白习惯、爽点节奏、主角依恋机制、剧情编排模式）一项都没有。

**做法**（参考 `writer-novel-style-distiller` 的维度设计 [11]）：
- 新增 `app/style/narrative.py`：用 LLM 对参考语料分块深读，提炼 L2/L3 规则
  - 分块：按章节或 5 万字一卷，每块单独提炼
  - 每块产出：人物描写手法（外貌/动作/心理各怎么处理）、对白怎么写、世界观呈现方式、爽点节奏、结局收束习惯
  - **强制附证据**：每条规则必须引用原文段落，防止模型编造"这个作者其实不这么写"的伪规则（`writer-novel-style-distiller` 的证据账本机制 [11]）
- 提炼结果合并去重后，落库到 **新列 `Project.style_narrative`**（JSON），独立于 `style_profile`
  - **为什么不塞进 `style_profile`**：`style_profile` 是纯统计零 LLM，P2 是 LLM 提炼，两者生成方式、失效逻辑、验收方法都不同，混在一个字段里会让 `profile_fingerprint` 越来越复杂
  - **独立缓存键**：`source_ids + narrative_version`，不依赖 `style_profile` 的指纹逻辑（`source_ids + unit_chars + version + schema`）。改了 P2 的提炼逻辑只需把 `narrative_version` +1
- 进 prompt：`_profile_block` 之后加【叙事档案】块，同样是确定性静态块（提炼一次存下来，不随段号变），进 `build_system_prompt` 的 system 块
- **不做全文覆盖**：L3 深读只覆盖代表性章节（20~30 章成熟期章节），剩下的是重复模式。同时避开早期文风漂移问题 [11]

**验收**：
- `test_narrative_extract.py`：规则带证据、证据段落真实存在于语料、去重合并正确
- 手测：续写一段，肉眼比对叙事档案里的规则是否体现在生成段

**改动文件**：
- `backend/app/style/narrative.py`（新）：LLM 分块提炼 + 证据账本
- `backend/app/models.py`：`Project` 加 `style_narrative` 列（JSON，nullable）
- `backend/app/generation/prompts.py`：`build_system_prompt` 加【叙事档案】块（system 块，静态恒定）
- `backend/app/routers/settings.py` 或 `projects.py`：加一个"提炼叙事档案"的触发端点（幂等，按 `source_ids + narrative_version` 缓存）

---

## 不做的事（避免过度设计）

- **不做 L6 视觉风格**：本项目是文字生成器，不涉及配图排版
- **不做早中晚期漂移分析的自动化**：只在 P2 采样时手动指定"取后 1/3 章节"即可，不值得做成独立功能
- **不做风格迁移/Prompt-and-Rerank**：本项目是生成不是改写，TST 那套方法不适用
- **不做 GRPO/RL 训练**：算力成本高，且调研显示 8B 模型 GRPO 后叙事完整性仍是挑战 [1]，不划算

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| P2 的 LLM 提炼幻觉伪规则 | 强制附证据 + 删除测试（删掉某条规则后对照原文验证是否成立）[11] |
| P0 样例注入挤占生成空间 | 样例总量控制在 800~1200 字（2~3 段），与 `unit_chars` 对齐 |
| P1 阈值误报 | 先在真实语料上扫分布再定，不拍脑袋；告警只提示不阻断 |
| P2 提炼成本高 | 只覆盖 20~30 章代表性章节，分块并行 |
