from datetime import datetime, timezone

from sqlalchemy import (
    Index,
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.db import Base, IS_SQLITE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 向量列类型：PG 用 pgvector.Vector，sqlite 开发模式退化为 Text
if IS_SQLITE:
    _EmbeddingCol = (Text,)  # type: ignore[assignment]
else:
    from pgvector.sqlalchemy import Vector

    _EmbeddingCol = (Vector(1536),)


class Project(Base):
    """用户的创作项目。"""

    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=True)  # 占位：当前单用户，预留多用户隔离
    title = Column(String(255), nullable=False)
    target_length = Column(Integer, nullable=False, default=5000)
    style_dims = Column(JSON, nullable=True)  # 节奏/视角/套路/篇幅 维度化预设（旧字段，保留兼容）
    mode = Column(String(16), nullable=False, default="quick")  # quick | expert
    outline = Column(JSON, nullable=True)
    summary = Column(JSON, nullable=True)  # 各段总结，续写时注入
    status = Column(String(16), nullable=False, default="created")
    created_at = Column(DateTime, default=_utcnow)

    # —— 新版创作向导字段（M5 重设计）——
    genre = Column(String(32), nullable=True)  # 类型：科幻/都市/悬疑/言情/奇幻/历史/仙侠…
    style = Column(String(32), nullable=True)  # 风格平台调性：番茄/起点/晋江/豆瓣阅读/知乎盐选…
    premise = Column(Text, nullable=True)  # 小说大概内容（用户填写，可空）
    reference_novel_ids = Column(JSON, nullable=True)  # 选中的参考语料 novel_id 列表（空=自由生成）
    plot_bible = Column(JSON, nullable=True)  # 动态剧情圣经：角色关系/后续走向/剧情冲突/伏笔
    generation_length = Column(Integer, nullable=False, default=3000)  # 单次生成字数（长文，避免多段压缩）
    # 层级摘要树（L1 段 / L2 卷 / L3 主线骨架）。
    # summary 仍是**无损的 L1 归档**，但注入提示词的已是本字段拼出的有界前情
    # （summary_tree.compose_prior）——summary 从「注入源」退为「存档」。
    summary_tree = Column(JSON, nullable=True)
    # 文体档案缓存：从本项目选定的参考语料实测拟合（`style.build_reference_profile`）。
    # 存下来是为了**稳定**——档案一旦拟合就不再随请求变化，`describe()` 的文本
    # 得以整轮恒定地待在 system 提示里，不击穿提示词缓存。
    # 空表示「自由生成」（未选参考语料），此时不注入文风档案块。
    style_profile = Column(JSON, nullable=True)
    # 叙事档案缓存：LLM 从参考语料提炼的 L2/L3 认知层规则（人物描写手法/对白风格/
    # 段落编排/场景构建/情绪表达/节奏控制/世界观呈现），附证据账本防幻觉伪规则。
    # 独立于 style_profile：后者是纯统计零 LLM，前者是 LLM 提炼，两者生成方式、
    # 失效逻辑、验收方法都不同。独立缓存键：source_ids + narrative_version。
    # 空表示「自由生成」（未选参考语料）或尚未提炼。
    style_narrative = Column(JSON, nullable=True)
    # 叙事蒸馏档位（full / balanced / fast，见 app.style.narrative.TIER_REGISTRY）。
    # 项目级偏好：用户选「快速档试跑」后落在这里，后续 build-narrative 与生成链路
    # 都按它来，不必每次重新选。空 = 未选 = 完整档（默认档）。
    # **独立于 style_narrative**：后者是产出缓存（本档算出来的东西），本字段是
    # 「用哪一档算」的意图。产出里已经记了 tier，但那是结果不是偏好——用户改档
    # 之后要靠本字段才知道下次该按哪档重算。
    narrative_tier = Column(String(16), nullable=True)


class RawNovel(Base):
    """采集/上传入库的源小说（MVP：种子语料 + 用户上传）。"""

    __tablename__ = "raw_novels"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=True)
    author = Column(String(255), nullable=True)
    platform = Column(String(64), nullable=True)
    genre = Column(String(64), nullable=True)
    heat = Column(Integer, nullable=True)
    tags = Column(JSON, nullable=True)
    source_url = Column(String(512), nullable=True)
    status = Column(String(16), default="imported")
    # 该语料自身的文体档案（含语料体检结论）。按语料缓存，避免每次生成重算。
    style_profile = Column(JSON, nullable=True)
    # 导入时的语料清洗报告（第 8 步）：检出串 + 次数 + 删字数 + 原文样本 +
    # 清洗前后字数。有损清洗的**留痕**，供人复核「洗掉了什么」。经 API 可读。
    # 未检出任何水印时仍写入一份 cleaned=False 的报告（区分「天然干净」与「漏检」）。
    clean_report = Column(JSON, nullable=True)


class NovelChunk(Base):
    """源小说的叙事切分片段，M2 接入向量/BM25 混合检索。"""

    __tablename__ = "novel_chunks"

    id = Column(Integer, primary_key=True)
    novel_id = Column(Integer, ForeignKey("raw_novels.id"), nullable=False)
    chapter_no = Column(Integer, nullable=True)
    idx = Column(Integer, nullable=False, default=0)
    text = Column(Text, nullable=False)
    embedding = Column(*_EmbeddingCol, nullable=True)  # M2 填充


class KgNode(Base):
    """知识图谱节点：角色/坑/关系/章节/世界观物/事件。"""

    __tablename__ = "kg_nodes"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    type = Column(String(32), nullable=False)  # character|plothole|relationship|chapter|worlditem|event
    props = Column(JSON, nullable=True)
    first_chapter = Column(Integer, nullable=True)
    last_chapter = Column(Integer, nullable=True)
    source = Column(String(64), nullable=True)  # 图谱来源标记：novel:123（参考语料分图）/ generated（生成后图谱）


class KgEdge(Base):
    """知识图谱边：人设/关系/坑状态等。"""

    __tablename__ = "kg_edges"

    id = Column(Integer, primary_key=True)
    from_id = Column(Integer, ForeignKey("kg_nodes.id"), nullable=False)
    to_id = Column(Integer, ForeignKey("kg_nodes.id"), nullable=False)
    type = Column(String(32), nullable=True)
    props = Column(JSON, nullable=True)


class Fact(Base):
    """事实账本：一张**带别名的扁平表**，刻意不是图。

    为什么不是图（外部评审 A 的 Q3 结论，已采纳）：这里实际要回答的问题是
    「本段相关的那 ~20 条事实是什么」——是过滤/检索问题，不是多跳遍历问题。
    为此建图遍历设施只增加维护成本，查询侧一次 WHERE 就够。

    与 KgNode 的分工：
    - KgNode 是「从文本抽出来的实体与关系」，`novel:{id}` / `generated` 各自成图；
    - Fact 是「这个项目当前认定的事实 + 名字表」，是**裁决层**：
      `aliases` 解决「宁折 / 宁折枝」这类姓名分裂，`source` 决定权威档位
      （plot_bible/用户手改 > 高置信抽取 > 低置信抽取，见评审 A 的 Q6）。
    名字表的权威在 Fact，图谱里的名字由 ledger 按它改写后保持一致。
    """

    __tablename__ = "facts"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    type = Column(String(32), nullable=False)  # character | plothole | relationship | …（可扩展）
    key = Column(String(160), nullable=False)  # 规范名（canonical）：注入提示词只用它
    props = Column(JSON, nullable=True)  # 一行当前状态：mentions/speaking/status/…
    aliases = Column(JSON, nullable=True)  # 已归并到本 key 的其它书写形态
    source = Column(String(64), nullable=False, default="generated")  # 权威档位来源
    confidence = Column(String(16), nullable=False, default="high")  # high | low（Q6 分层裁决）
    first_segment = Column(Integer, nullable=True)
    last_segment = Column(Integer, nullable=True)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class Generation(Base):
    """分段生成产物：每轮落库，支持段级续写。"""

    __tablename__ = "generations"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    segment_no = Column(Integer, nullable=False)
    chapter_range = Column(String(32), nullable=True)
    text = Column(Text, nullable=True)
    summary = Column(JSON, nullable=True)


class AppSetting(Base):
    """运行时配置键值对：LLM 渠道 / API key / 模型选择等前端可改项的持久化。

    key 形如 llm.provider / llm.zhipu_api_key / llm.zhipu_model；
    启动时由 main.lifespan 加载并覆盖 settings，PUT /api/settings/llm 即时热生效。
    """

    __tablename__ = "app_settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=False, default="")


# ====================================================================
# ADR-2026-10-12 第 4 节：Assistant Agent 4 张最小表（P0）。
#
# 设计原则：
# - 不变量继承 b58c763：角色锁定 / 节拍约束 / KG 透传 / prompt 限长全部保持；
#   4 张表只承载 Assistant 的「审计 + 提案 + 版本」三件事。
# - 写入受 `EditProposal.commit` 单点收敛：Assistant 端**不**提供写入端点；
#   任何对 chapters / characters / beats / plot_bible / Fact / KgNode 的写
#   必须由用户在后续 ADR 引入的审批流触发（见 ADR §5 第 4 步）。
# - 每张表带 project_id 或 run_id 外键，便于审计回放与回滚。
# ====================================================================


class AgentRun(Base):
    """一次 Assistant 调用的执行记录。

    role 硬约束为 'assistant'（与 Writer 单例隔离；Writer 不写本表）。
    request_hash 用于「同请求重放」的关联；model 字段记录实际命中的 provider
    实例的 model / label，便于事后查 Assistant provider 漂移。

    `note`（第 7 步新增，nullable）：这次 run 的**性质说明**。既有约定把
    语义标记写进 `model` 字段（`(proposal_authoring)` / `(local_fallback)` /
    `(blocked_by_registry)`），那些是「模型侧发生了什么」；而
    「这次生成是助手调用的」属于**调用来源**，与模型名不是一回事——
    混进 model 会让「writer 实际用了什么模型」这件事还要先做字符串剥切。
    故单开一列，只由 invoke-generation 等「显式指令触发」的端点书写。
    """

    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    role = Column(String(16), nullable=False, default="assistant")  # assistant（保留 writer 扩展位）
    status = Column(String(16), nullable=False, default="running")  # running | success | failed
    started_at = Column(DateTime, default=_utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    request_hash = Column(String(64), nullable=False)  # sha256
    model = Column(String(128), nullable=True, default="")
    note = Column(Text, nullable=True)  # 第 7 步：调用性质说明（如「助手调用生成」）


class Evidence(Base):
    """Assistant 一次调用产生的只读证据（kind ∈ generation/chunk/reminder/fact/kg/run/novel）。

    第 7 步增加 `run` 与 `novel`：日志分析类调用引用的证据是 AgentRun
    审计行本身；语料审核的 novel 级结论（缺 style_profile 等）证据是
    整本书（见 `app.agents.diagnosis.ALLOWED_EVIDENCE_KINDS` 的同款说明）。

    snippet 限制 1000 字以内（router 已裁；这里是二次防御）。
    score ∈ [0, 1]：检索相似度 / 启发式置信度。
    """

    __tablename__ = "evidence"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False)
    kind = Column(String(32), nullable=False)  # generation | chunk | reminder | fact | kg
    source = Column(String(64), nullable=False)  # generation / novel / fact / kg
    ref_id = Column(String(64), nullable=False)  # 引用目标 id（chunk_id / generation_id / fact.key）
    snippet = Column(Text, nullable=True)
    score = Column(String(16), nullable=True)  # SQLite 上 FLOAT 没有；用 str 存、解析端做 float()


class EditProposal(Base):
    """Assistant 提出的待审批写回建议（ADR-2026-10-12 第 5 步第 4 步开放审批流）。

    ## 状态机（唯一合法流转，其余组合一律 409）

        pending ──approve──> approved ──apply──> applied ──restore──> rolled_back
           │                    │
           │                    └──目标漂移──> stale（终态，需重新生成提案）
           └──reject──> rejected（终态）

    `status` 取值域：`pending | approved | rejected | applied | stale | rolled_back`。

    ## 三条硬不变量

    1. **Assistant 不写**：本表的写入只发生在「用户调POST /api/assistant/proposals」
       与诊断/审批端点里；`app.agents.registry` 的白名单不含任何写工具，
       写工具名命中 `FORBIDDEN_PREFIXES` 一律 PermissionError。
    2. **patch 只能描述四类目标的修改**：`generations.text` /
       `projects.plot_bible` / 提示词运行时覆写（`prompt_overrides` 表）/
       参考语料分片正文（`novel_chunks.text`，第 10 步的 `corpus_text_edit`）。
       明确**不**支持删除 Project / 删除 Generation / 删除 NovelChunk /
       改 AppSetting（LLM key）—— 校验在 `app.agents.proposals.validate_patch`，
       越界直接 ValueError。
    3. **目标漂移即 stale**：`scope_json.base_content_hash` 记录提案创建那一刻
       目标的 sha256；apply 前重算，不一致 → 置 `stale` 并 409，绝不静默重算或覆盖。

    `target_table` 是 `target_kind` 的**旧字段镜像**（NOT NULL 保留以兼容既有行），
    新代码一律读 `target_kind`。
    """

    __tablename__ = "edit_proposals"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False)
    # —— 第 4 步新增：归属项目（既有行可能为空，故nullable）——
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    # 提案类型：text_edit | prompt_edit | fact_resolution | corpus_text_edit
    #（与 target_kind 一一对应）。
    type = Column(String(32), nullable=True)
    # 修改目标类别：generation_text | project_plot_bible | prompt_override | novel_chunk_text。
    target_kind = Column(String(32), nullable=True)
    scope_json = Column(JSON, nullable=True)  # 影响范围 + 目标基线哈希 base_content_hash
    # —— 既有字段（保留）——
    target_table = Column(String(64), nullable=False)  # 旧镜像：generations | projects | prompt_overrides
    target_id = Column(String(64), nullable=True)  # 受影响行的 id（可空表示新增）
    patch_json = Column(JSON, nullable=True)  # 受 schema 约束的 patch（白名单键）
    risk_level = Column(String(16), nullable=False, default="low")  # low | medium | high
    status = Column(String(16), nullable=False, default="pending")
    # —— 第 4 步新增：diff / 理由 / 证据 / 版本指针 ——
    before_text = Column(Text, nullable=True)  # 修改前原文（用于 diff 与回滚基线）
    after_text = Column(Text, nullable=True)  # 修改后文本（apply 时写入业务表）
    reason = Column(Text, nullable=True)  # 为什么改（人可读）
    evidence_json = Column(JSON, nullable=True)  # 关联证据（issue/Evidence 的结构化拷贝）
    version_id = Column(Integer, nullable=True)  # apply成功后产生的 Version.id
    created_by = Column(String(64), nullable=True)  # 创建人（当前单用户可为 None）
    created_at = Column(DateTime, default=_utcnow, nullable=True)
    approved_by = Column(String(64), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)


class Version(Base):
    """提案落地后的不可变版本快照（ADR-2026-10-12 第 5 步第 4 步）。

    ## 不可变 + 只追加

    Version 行**永不 UPDATE、永不 DELETE**：回滚不是「删掉新版本」，
    而是「再写一条 Version 记录旧内容」。于是 `v0 → v1 → v2 → 回滚` 得到的是
    v0 / v1 / v2 / v3 四条历史，v3 与 v1 内容相同但来源不同（`kind`/`source_proposal_id`
    可区分），验收标准 6 的「历史可读」天然成立。

    ## 字段分工

    - `snapshot_json`：`{target_kind, target_id, before, after, ...}`，
      `after` 即该版本生效后的目标内容；restore 读的就是它。
    - `content_hash`：`after` 的 sha256（`hashlib.sha256` over canonical JSON）——
      既做幂等去重，也做 apply 前的漂移比对。
    - `kind`：`text | prompt | bible | mixed | corpus_text`，与 EditProposal.target_kind
      的落点一致（`corpus_text` = 参考语料分片正文，与生成正文 `text` 分开）。
    - `proposal_id`：旧字段（NOT NULL），指向直接产生本版本的提案；
      `source_proposal_id` 是它的语义别名，回滚版本指向被恢复的那条提案。
    """

    __tablename__ = "versions"

    id = Column(Integer, primary_key=True)
    proposal_id = Column(Integer, ForeignKey("edit_proposals.id"), nullable=False)
    snapshot_json = Column(JSON, nullable=True)
    created_by = Column(String(64), nullable=True)  # 审批人 user_id（预留；当前单用户可为 None）
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    # —— 第 4 步新增 ——
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    kind = Column(String(16), nullable=True)  # text | prompt | bible | mixed
    content_hash = Column(String(64), nullable=True)  # sha256(after 的 canonical JSON)
    source_proposal_id = Column(Integer, ForeignKey("edit_proposals.id"), nullable=True)
    # 回滚产生的版本为True；用它区分「apply 产生」与「restore 产生」。
    is_restore = Column(Boolean, nullable=False, default=False)


class PromptOverride(Base):
    """提示词的**运行时覆写**（ADR-2026-10-12 第 5 步第 4 步）。

    ## 为什么必须独立成表，而不是改 prompts.py

    硬约束：改提示词只能走运行时覆写，**不许改`app/generation/prompts.py` 源文件**
    （Writer 生成链路的 b58c763 基线不可动）。所以覆写以「键→ 文本」的形式落库，
    由 `app.agents.prompt_overrides.resolve_prompt_block()` 在运行时拼接；
    代码默认值始终留在 prompts.py，覆写表为空即等价于原行为。

    ## 一项目一键一条生效

    `(project_id, prompt_key)` 上有唯一约束 `uq_prompt_override_active`：
    同一项目的同一提示词键**同时最多一条 active 覆写**。新覆写落库时旧行被
    降级为 `active=False`（**不删除**——历史要能查、能回滚）。
    """

    __tablename__ = "prompt_overrides"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    # 覆写目标键，见 app.agents.prompt_overrides.PROMPT_KEYS。
    prompt_key = Column(String(64), nullable=False)
    override_text = Column(Text, nullable=False)  # 覆写后的完整文本（非 diff 片段）
    active = Column(Boolean, nullable=False, default=True)
    source_proposal_id = Column(Integer, ForeignKey("edit_proposals.id"), nullable=True)
    created_by = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    # 刻意**不**加 UNIQUE 约束：`(project_id, prompt_key, active)` 在
    # SQLite 与 PG 上对 NULL / False 的处理不一致，且历史行天然会撞（同一键
    # 多次降级后有多条 active=False）。唯一性由应用层在写事务里保证 ——
    # 见 prompt_overrides.set_active_override()：先降旧、再插新，同一事务内
    # 「至多一条 active」恒成立（读-改-写全程持有该行的写锁）。


class DistillRun(Base):
    """一次「文风蒸馏」的后台任务行（进度条的单一事实来源）。

    ## 为什么必须有这张表

    蒸馏是**分钟级**长任务（833 章语料跑完整档实测 42 分钟，见
    `app.style.narrative` 的档位注释）。HTTP 请求挂不了这么久，所以蒸馏被拆成
    「登记任务 → 后台跑 → 前端轮询」三步，而**进度必须落在 DB 里**：

    - 进程内的全局变量在 uvicorn reload / 多 worker 下会丢；
    - 用户离开页面再回来（甚至换个标签页）仍要能看到「还在跑、跑到哪了」。

    ## 状态机（唯一合法流转）

        pending ──后台线程取走──> running ──成功──> success
                                     └──任何异常──> failed

    `pending` 是**登记态**：`POST /distill` 只写这一行就返回，前端拿到的 job_id
    立刻可轮询。后台线程（`app.style.jobs.run_distill_job`）把它置 running 后开工。

    **绝不允许任务停在 running**：任何异常都落到 `failed` + `error`，否则前端
    进度条会永远转下去——比直接报错更难排查（见 jobs.run_distill_job 的 finally）。

    ## 进度语义

    `progress_current / progress_total` 的单位是**块（chunk）**，即蒸馏时切给
    LLM 的语料分片数（一本语料切成若干块，每块一次 provider.chat）。
    `progress_total == 0` 表示「总数还没探明」——前端此时渲染不确定态，
    绝不拿 0 当分母算出 0% 或 NaN。

    `stage` 是**人话阶段名**（如「语料 2/3：提炼第 5/12 块」），直接显示给用户。

    ## 摘要字段为什么在这里冗余一份

    `n_chapters / rule_count / rejected` 是从产出的 `style_narrative` 里抄出来的
    摘要。任务结束后前端只轮询这一行就能给出结论，不必再去解析整份档案 JSON
    （那份 JSON 有几万字符，不该每次轮询都传）。
    """

    __tablename__ = "distill_runs"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    # 本次蒸馏用的档位（full / balanced / fast）。**落库而非留空**：
    # 项目上的 narrative_tier 是「下次该用哪档」的偏好，会被用户改掉；
    # 这一行记的是「这次实际按哪档算的」，历史任务不该随偏好变化而变化。
    tier = Column(String(16), nullable=True)
    # pending | running | success | failed
    status = Column(String(16), nullable=False, default="pending")
    # 人话阶段名（如「语料 2/3：提炼第 5/12 块」），直接显示给用户
    stage = Column(String(255), nullable=True)
    progress_current = Column(Integer, nullable=False, default=0)
    progress_total = Column(Integer, nullable=False, default=0)  # 0 = 总数未探明
    # —— 成功后回填的摘要（失败时保持 NULL，不伪造）——
    n_chapters = Column(Integer, nullable=True)  # 实际采样章节数
    rule_count = Column(Integer, nullable=True)  # 提炼出的规则条数
    rejected = Column(Integer, nullable=True)  # 因证据不通过被拒的规则数
    error = Column(Text, nullable=True)  # failed 时的原因（type: message）
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


class StyleDistill(Base):
    """**可复用的文风蒸馏档案库**（用户原话：「已经蒸馏完的应该可以保存到库里面，下次可以直接用」）。

    ## 它和 `distill_runs` 的关系

    `distill_runs` 是**一次蒸馏任务的执行记录**（进度条来源，跑完即归档，可能被
    `reap_stale_runs` 标 failed）。但任务本身的产物——那份几万字符的叙事档案——
    只存在发起项目的 `Project.style_narrative` 里，**换个项目就摸不着了**。本表就是
    把产物从「项目私有」提升为「全局复用」的媒介：第一次蒸馏成功就写一行，之后任何
    项目只要**语料集合 + 档位 + 语料正文**三者一致，就能直接命中这行、免掉一次 42 分钟的 LLM。

    ## 复用的判据（为什么是这四个字段）

    复用键 = `(source_ids_hash, tier, tier_signature, content_hash)`：

    - `source_ids_hash`：参考语料 novel_id 集合的稳定哈希（顺序归一）。
    - `tier` + `tier_signature`：档位 key + 档位配置哈希（维度白名单/采样章数/清洗强度）。
      换档必须重算，否则会静默拿到旧档结果（本项目已踩过两次的同类坑）。
    - `content_hash`：**语料实际正文**的 sha256。这一步是 defect-C 回归修复的关键——
      两个项目若只因 `source_ids` 相同就命中，那么「同一本语料被重新导入/清洗后正文变了」
      也会误命中，产出 stale 档案且不报错。content_hash 让库只在**正文真的没变**时才命中。

    这四列上加唯一约束 `(source_ids_hash, tier, tier_signature, content_hash, status)`，
    保证同一份输入在 `active` 态下至多一行——库里不会出现「两个一模一样的档案」让前端
    选择困难，也保证第二个项目复用时只会 FIND 到已有行、不会又写一份。

    ## 库 ≠ 项目缓存

    `Project.style_narrative` 仍是**生成链路**用的单槽缓存（每次生成直接读它，稳定不击穿
    提示词缓存）。本表是跨项目共享层；`project_narrative` 命中本表后，会把档案**复制**进
    该项目的 `style_narrative`，二者从此各自独立——改了项目缓存不会污染库，归档库行也不影响
    已应用的项目。

    ## status

    - `active`：可被复用（默认）。
    - `archived`：用户主动归档，不再出现在「可复用」列表里，但保留供历史查阅；
      归档后同输入再蒸馏会新建一行（而非悄悄覆盖）。
    """

    __tablename__ = "style_distills"

    id = Column(Integer, primary_key=True)
    # 参考语料 novel_id 列表（与 `Project.reference_novel_ids` 同口径，排序归一）。
    source_ids = Column(JSON, nullable=False)
    # source_ids 的稳定哈希（排序后 join）。既用于复用查找，也避免对 JSON 列建联合索引的麻烦。
    source_ids_hash = Column(String(64), nullable=False, index=True)
    # 档位 key（full / balanced / fast）。
    tier = Column(String(16), nullable=False, index=True)
    # 档位配置哈希（维度白名单/采样章数/清洗强度）。见 app.style.narrative._tier_signature。
    tier_signature = Column(String(64), nullable=False)
    # 语料正文 sha256（见 app.style.narrative.corpus_content_hash）。defect-C 回归修复点。
    content_hash = Column(String(64), nullable=False, index=True)
    # 提炼时的 NARRATIVE_VERSION，归档版本漂移时便于识别旧档。
    narrative_version = Column(Integer, nullable=False, default=1)
    # active | archived（见类注释）。
    status = Column(String(16), nullable=False, default="active", index=True)
    # 完整叙事档案 JSON（几万字符）。列表接口**不**回传它，只在详情/应用时回传。
    style_narrative = Column(JSON, nullable=True)
    # 摘要（与 distill_runs 同口径，列表即可见，不必解析整份 JSON）：
    n_chapters = Column(Integer, nullable=True)
    rule_count = Column(Integer, nullable=True)
    rejected = Column(Integer, nullable=True)
    # 首次蒸馏出这份档案的项目（来源追溯）。可空：极端情况下库也可由管理操作预置。
    created_by_project_id = Column(
        Integer, ForeignKey("projects.id"), nullable=True
    )
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    # 最近一次被应用到某个项目的时间（命中复用也算）。可空 = 仅入库未被用过。
    applied_at = Column(DateTime, nullable=True)


class AIFlavorMemory(Base):
    """AI 腔反馈闭环的记忆行（Assistant 主导的全局/项目级「问题账本」）。

    ## 它在闭环里的位置

    助理 agent（Assistant）是主 agent：调度 Writer（`app.generation.engine`）
    生成后，**由 Assistant 检查**产出、把本次 AI 腔问题总结成一句人话写进本表；
    下次生成前，编排层读回本表、渲染成提示词块注入给 Writer 的输入——微调的
    对象是「下次的输入」，不是重写本次文本（检查者与生成者必须分离）。

    ## 去重键：(scope, project_id, issue_key)

    同一个问题模式（如「仿佛」式机械比喻）无论在多少段里反复出现，都只累加
    一行 `hit_count`。表长由「问题种类」决定而非「写作轮数」决定——没有这条，
    一个 300 段的长篇能把表写爆，注入提示词的也会是同一句话的复读。

    `scope=global` 的行 `project_id` 为 NULL：那是跨项目的通用教训，刻意不挂
    到任何项目上（挂了就等于把全局记忆悄悄变成某个项目的私有记忆）。

    ## 为什么没有 UNIQUE 约束

    与 `PromptOverride` 同款判断（见其类注释）：`(project_id, issue_key)` 在
    SQLite 与 PG 上对 NULL / False 的处理不一致，且去重要求「查得到才累加」，
    本质是读-改-写，唯一性由应用层在写事务里保证（`app.agents.
    ai_flavor_memory.upsert_memory`）。这里只建普通索引加速这次查找。

    ## 有界膨胀

    `examples` 每条最多 3 条证据（新的挤掉最旧的），读取时再按条数/字符预算
    裁剪——两端同时设防，详见 ai_flavor_memory 模块注释。

    ## active 是软开关

    停用某条记忆（`active=False`）只让它从 load 结果里消失，历史仍可查；
    显式的人工停用决策不会被 upsert 悄悄复活。
    """

    __tablename__ = "ai_flavor_memories"

    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False, default="global")  # global | project
    # scope=global 时为 NULL（见类注释）；scope=project 时必填。
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)
    # 命中的 AI 腔模式 / 归一化问题类型（如「仿佛」「宛如」），由检测侧归一后传入。
    issue_key = Column(String(128), nullable=False)
    summary = Column(Text, nullable=False, default="")  # 一句人话总结
    hit_count = Column(Integer, nullable=False, default=0)  # 累计命中次数（去重累加）
    # 例句证据 [{segment_no, offset, snippet}]，上限 3 条（写入端强制）。
    examples = Column(JSON, nullable=True, default=list)
    last_segment_no = Column(Integer, nullable=True)  # 最近一次出现在哪段
    severity = Column(String(16), nullable=False, default="mid")  # high | mid | low（排序裁剪用）
    active = Column(Boolean, nullable=False, default=True)  # 软开关：停用不删历史
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        # 去重查找的加速索引；(scope, issue_key) 前缀即可覆盖 global 行的查找，
        # project 行的 project_id 等值条件由前缀过滤后在行级完成。
        Index("ix_ai_flavor_memory_key", "scope", "project_id", "issue_key"),
    )
