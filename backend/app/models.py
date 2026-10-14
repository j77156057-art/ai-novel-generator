from datetime import datetime, timezone

from sqlalchemy import (
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
    2. **patch 只能描述三类目标的修改**：`generations.text` /
       `projects.plot_bible` / 提示词运行时覆写（`prompt_overrides` 表）。
       明确**不**支持删除 Project / 删除 Generation / 改 AppSetting（LLM key）——
       校验在 `app.agents.proposals.validate_patch`，越界直接 ValueError。
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
    # 提案类型：text_edit | prompt_edit | fact_resolution（与 target_kind 一一对应）。
    type = Column(String(32), nullable=True)
    # 修改目标类别：generation_text | project_plot_bible | prompt_override。
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
    - `kind`：`text | prompt | bible | mixed`，与 EditProposal.type 一致。
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
