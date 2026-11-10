from datetime import datetime, timezone

from sqlalchemy import (
    Index,
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
    # 采样章数覆盖（None = 按档位默认口径；0 = 全本；N > 0 = 最多 N 章）。
    # 助理在对话里说「只取 200 章」「整本都灌进去」时写这里，并进蒸馏指纹，
    # 保证改完不会命中旧档案（静默返回旧结果是本项目反复踩过的坑）。
    narrative_sample_chapters = Column(Integer, nullable=True)
    # 时间线状态存档（§11.3）：替代进程内「已写段数重放 advance」恢复。
    # JSON：{version, nodes, done, current_node, current_beat, branch_used, seg_counts, current_main_idx}
    # 节点内容（title/entry/exit/beats/emotion）首次 §7.3 调 LLM 填实后落库，
    # 之后段间推进只更新 done/current_*/seg_counts，不再调 LLM。
    # 空表示「未启用时间线」或「尚未首次规划」——首次生成时回退到重放恢复+落库。
    # version 用于将来 schema 演进时旧数据可识别并降级。
    plot_timeline = Column(JSON, nullable=True)
    # 节奏参数档（v2.4 高潮密度蒸馏）：
    # JSON：{version, source_ids, chapter_emotions, avg_node_segments, branch_ratio, distilled_at}
    # avg_node_segments 从语料章节情绪蒸馏：高潮章占比反推"平均每几段一个主干节点"，
    # 取代首轮默认 5。branch_ratio 已落到 style_narrative，重复存是为了 plot_timeline
    # 首次规划时不用去 style_narrative 找。
    # 缓存指纹只有 source_ids + version（与 style_narrative 同模式）。
    # 空表示「未蒸馏」——首次生成时回退到默认 5。
    plot_rhythm = Column(JSON, nullable=True)


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


# ====================================================================
# KG v3.0：工业界对齐的 9 张表（docs/kg-v3-design-final.md）
#
# 设计原则（吸收 The Narrative Graph / Novelium / leenbj skill 经验）：
# - 领域分表（character/location/faction/item/worldrule/power_system 共用 kg_entities），
#   软 schema 用 attrs JSON 承载，运行时校验不锁 DDL
# - 关系带强度（strength）+ 独立演变表（kg_relation_evolution）——记过程不记快照
# - 事件用 SVO triplet + location 必填 + in_story_date（物理时间校验的输入）
# - 伏笔是独立表 + 5 态状态机（planted/developing/paid_off/dropped/expired），
#   不用 event_link 间接表达
# - 人物情报 kg_entity_knowledge 支撑「角色此刻知道什么」反查
# - kg_mentions 是实体-段反向索引（检索最近 N 段涉及的实体，不 dump 全表）
#
# 与 v2.6 旧表（kg_nodes/kg_edges/facts）关系：旧表保留只读，新链路双写过渡。
# ====================================================================


class KgEntity(Base):
    """v3.0 实体表：6 种 kind 共用一张表 + attrs 软 schema。

    canonical_name + aliases 处理姓名分裂（宁折/宁折枝）；first/last_chapter
    是实体活跃区间；mention_count 供节奏分析。attrs 按 kind 承载
    arc/current_goal/secrets/power_level/region/leader_id 等软字段。
    """

    __tablename__ = "kg_entities"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    kind = Column(String(16), nullable=False)  # character|location|faction|item|worldrule|power_system
    canonical_name = Column(String(128), nullable=False)
    aliases = Column(JSON, nullable=True, default=list)  # 已归并到 canonical_name 的其它书写形态
    attrs = Column(JSON, nullable=True, default=dict)  # 软 schema：arc/current_goal/secrets/power_level/region/leader_id
    first_chapter = Column(Integer, nullable=True)
    last_chapter = Column(Integer, nullable=True)
    mention_count = Column(Integer, nullable=False, default=0)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "kind", "canonical_name", name="uq_kg_entity"),
        # 检索切片/批抽扫描：WHERE project_id=? + 归一 by canonical_name
        Index("ix_kg_entity_project_kind", "project_id", "kind"),
        Index("ix_kg_entity_project_canonical", "project_id", "canonical_name"),
    )


class KgRelation(Base):
    """v3.0 关系表：带强度（0-1）+ since_chapter。

    rel_type 取值域：ally / enemy / mentor / subordinate / romantic / belongs_to /
    located_at / owns，以及亲属类 father / mother / elder_brother / younger_brother /
    elder_sister / younger_sister / son / daughter / kin（2026-11-08 扩容）；
    通用词 sister / brother / sibling / parent / child 亦合法，其中 sister/brother
    落库时归一为 sibling（不猜长幼）。
    strength 变化不覆盖本行——每次变化追加 kg_relation_evolution 一行，本行存最新。
    """

    __tablename__ = "kg_relations"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    from_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False)
    to_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False)
    rel_type = Column(String(32), nullable=False)
    strength = Column(Float, nullable=False, default=0.5)
    since_chapter = Column(Integer, nullable=True)
    description = Column(String(200), nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "from_id", "to_id", "rel_type", name="uq_kg_relation"),
        Index("ix_kg_relation_project", "project_id"),
    )


class KgRelationEvolution(Base):
    """关系演变史：每次 strength 变化追加一行（不清历史）。

    例：太子×魏征 ch5=0.3「初次合作」→ ch15=0.8「共渡危机」→ ch40=0.5「政见分歧」。
    """

    __tablename__ = "kg_relation_evolution"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    edge_id = Column(Integer, ForeignKey("kg_relations.id"), nullable=False)
    chapter = Column(Integer, nullable=True)
    strength = Column(Float, nullable=False)
    note = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        # 关系曲线展开：按 edge_id 拉所有 evolution
        Index("ix_kg_relation_evolution_edge", "edge_id"),
    )


class KgEvent(Base):
    """v3.0 事件表：SVO triplet + location 必填 + 故事内时间。

    in_story_date（如「贞观三年秋」）是物理时间校验的关键输入；
    subject/object/location 存 entity canonical_name（抽取层名字，落库时解析为 id
    失败时保留字符串在 extra JSON，不阻塞——judge 用 id，检索用字符串兜底）。
    """

    __tablename__ = "kg_events"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    sequence = Column(Integer, nullable=False)  # 项目内连续序号（全局事件顺序）
    chapter = Column(Integer, nullable=True)
    segment_no = Column(Integer, nullable=True)  # 生成段号（generations.segment_no）
    event_type = Column(String(32), nullable=False)  # incident|revelation|foreshadow_plant|foreshadow_payoff|death|romance_milestone|battle|decision|time_marker
    summary = Column(String(500), nullable=True)
    subject_id = Column(Integer, nullable=True)  # SVO 主体 -> kg_entities.id
    verb = Column(String(64), nullable=True)  # SVO 动作
    object_id = Column(Integer, nullable=True)  # SVO 客体 -> kg_entities.id
    location_id = Column(Integer, nullable=True)  # 物理时间校验需要
    time_anchor_id = Column(Integer, ForeignKey("kg_events.id"), nullable=True)
    in_story_date = Column(String(64), nullable=True)  # 「贞观三年秋」式故事内时间
    extra_actors = Column(JSON, nullable=True, default=list)  # 参与角色超过 2 个时存名字列表
    confidence = Column(Float, nullable=False, default=1.0)
    evidence_segment_ids = Column(JSON, nullable=True, default=list)
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "sequence", name="uq_kg_event_seq"),
        # 切片取最近 N 段事件 ORDER BY sequence DESC；批抽取 max(sequence)
        Index("ix_kg_event_project_sequence", "project_id", "sequence"),
        Index("ix_kg_event_project_segment", "project_id", "segment_no"),
    )


class KgEventLink(Base):
    """事件因果链：caused_by|causes|follows|resolves|reverses|parallel。"""

    __tablename__ = "kg_event_links"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    from_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=False)
    to_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=False)
    link_type = Column(String(16), nullable=False)
    confidence = Column(Float, nullable=False, default=1.0)

    __table_args__ = (
        UniqueConstraint("project_id", "from_event_id", "to_event_id", "link_type", name="uq_kg_event_link"),
        Index("ix_kg_event_link_from", "from_event_id"),
        Index("ix_kg_event_link_to", "to_event_id"),
    )


class KgCharacterState(Base):
    """角色状态变化表：每章追踪 location/injury/mood/goal/inventory/alive 变化。

    Novelium Character Dashboard 的数据结构；judge 查瞬移（location 与事件地点矛盾）、
    死人说话（alive=false 后又出场）都从这里取基线。
    """

    __tablename__ = "kg_character_state"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    character_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False)
    chapter = Column(Integer, nullable=True)
    segment_no = Column(Integer, nullable=True)
    state_type = Column(String(16), nullable=False)  # location|injury|mood|goal|inventory|alive
    new_value = Column(String(500), nullable=True)  # 变化后的值
    cause_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=True)
    created_at = Column(DateTime, default=_utcnow)

    __table_args__ = (
        # 切片按角色取最新状态（slice_for_prompt 查每角色每 state_type 最新一条）
        Index("ix_kg_char_state_project_char", "project_id", "character_id"),
        Index("ix_kg_char_state_project_segment", "project_id", "segment_no"),
    )


class KgForeshadow(Base):
    """伏笔状态机表：独立不混入 kg_events。

    5 态：planted|developing|paid_off|dropped|expired。
    expired = 超过 planned_payoff_chapter 仍未回收（Novelium 伏笔超期检测的输入）。
    """

    __tablename__ = "kg_foreshadows"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    name = Column(String(200), nullable=False)
    planted_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=True)
    payoff_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=True)
    status = Column(String(16), nullable=False, default="planted")
    planted_chapter = Column(Integer, nullable=True)
    planned_payoff_chapter = Column(Integer, nullable=True)
    actual_payoff_chapter = Column(Integer, nullable=True)
    description = Column(String(500), nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_kg_foreshadow"),
        # 切片 active 过滤（status IN ('planted','developing')）+ Judge 超期检查
        Index("ix_kg_foreshadow_project_status", "project_id", "status"),
    )


class KgEntityKnowledge(Base):
    """人物情报追踪：entity 在哪个事件学到了什么（known_kind=entity|event|secret）。

    Novelium 卖点——生成时反查「林悠然此刻知道什么」，喂进 prompt 防预知外挂。
    """

    __tablename__ = "kg_entity_knowledge"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False)
    known_entity_id = Column(Integer, nullable=True)  # 指向 entity/event/fact 之一（按 known_kind 解释）
    known_kind = Column(String(16), nullable=True)  # entity|event|secret
    # secret/event 类的事实文本（known_entity_id 为 NULL 时的内容载体）：
    # - secret：秘密内容短句（"父亲的死因另有隐情"）——没有对应实体，只有文本
    # - event：事件简述（解析失败时的降级载体；解析成功则 known_entity_id 指向 kg_events.id）
    known_text = Column(String(200), nullable=True)
    learned_at_event_id = Column(Integer, ForeignKey("kg_events.id"), nullable=True)
    learned_at_chapter = Column(Integer, nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)

    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", "known_entity_id", "learned_at_event_id", name="uq_kg_entity_knowledge"),
        Index("ix_kg_entity_knowledge_project_entity", "project_id", "entity_id"),
    )


class KgMention(Base):
    """实体-段反向索引：context_window 保留原文片段（喂 LLM 用有血有肉的事实）。

    slice_for_prompt 按 segment_no 倒序取最近 N 段涉及的实体，不 dump 全表。
    """

    __tablename__ = "kg_mentions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("kg_entities.id"), nullable=False)
    generation_id = Column(Integer, ForeignKey("generations.id"), nullable=True)
    segment_no = Column(Integer, nullable=True)
    context_window = Column(String(500), nullable=True)  # 提及处原文片段
    mention_kind = Column(String(16), nullable=True)  # llm|alias|reference

    __table_args__ = (
        UniqueConstraint("project_id", "entity_id", "generation_id", name="uq_kg_mention"),
        Index("ix_kg_mention_project_segment", "project_id", "segment_no"),
        Index("ix_kg_mention_project_entity", "project_id", "entity_id"),
    )


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
    # —— 看护用的两个字段（只服务「疑似卡住」判断，不参与蒸馏逻辑）——
    #
    # progress_at：最后一次进度**心跳**（每次 progress 回调、以及进入 running 时写）。
    #   为什么不能拿 started_at 代替：进度可能已经写了很多块，只是最后一块慢；
    #   只有「最后一次进度时间」才能算出「多久没有动静」。
    # 为 NULL 的老行按 started_at / created_at 兜底（见 jobs.distill_health）。
    progress_at = Column(DateTime, nullable=True)
    # expected_minutes：提交时按语料字数外推的预计耗时（分钟）。**纯估算**，
    #   界面必须标注「非实测」；NULL 表示算不出来（如语料未统计），此时只做
    #   「无进度时长」判断，不做「超预计倍数」判断——宁可少报，不许编。
    expected_minutes = Column(Integer, nullable=True)


class DistillChunk(Base):
    """蒸馏的**块级完成标记**（断点续跑的落盘点）。

    ## 为什么必须有这张表

    蒸馏 400+ 分钟里几乎全部时间花在「每块一次 LLM 调用」上，但每块解析出的
    规则原来只累加在 `extract_narrative_profile` 的内存累加器里，整份档案要等
    全部块跑完才落库。进程一没，已完成的部分就全部作废——这正是用户说的
    「中断一次等于全部白跑」。

    这块表把**每块的净产出**（通过证据校验的规则 / 禁忌 / 拒伪计数）在块处理
    完的当下就写进去，续跑时直接把已完成块回灌进累加器、跳过它们的 prompt
    构建与 LLM 调用。

    ## scope：续跑只在同一次输入内有效

    `scope` 是 `source_ids + 档位配置 + narrative_version + max_chapters` 的
    稳定哈希。换语料、换档位、改采样章数、提炼逻辑升版，scope 就变了，旧块
    自然查不到——不会拿旧档的半成品拼出新档的档案。

    ## 失败块不落库

    调用失败 / 解析不出预期维度的块**不写行**。这样续跑会重新尝试它，而不是
    把「上次超时」永久固化成「这块没有产出」。`failed_chunks` 因此每轮重算，
    不会跨轮累加。

    ## payload 的形状

    `{"rules": {维度: [{rule, novel_id, chapter_no}]}, "forbidden": {维度: [str]},
      "rejected": int}`。收尾阶段（角色声音 / 文风漂移）也用本表存：它们的
    `chunk_key` 是 `aux:voice:{nid}` / `aux:drift:{nid}` 这类合成键，续跑时
    同样可以跳过已完成的调用。
    """

    __tablename__ = "distill_chunks"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "scope", "chunk_key", name="uq_distill_chunk_scope_key"
        ),
        Index("ix_distill_chunks_lookup", "project_id", "scope"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    #: source_ids + 档位配置 + narrative_version + max_chapters 的短哈希
    scope = Column(String(32), nullable=False)
    novel_id = Column(Integer, nullable=False)
    #: 块内容哈希（跨轮稳定：采样确定性，同 scope 下块逐字节一致）
    chunk_key = Column(String(64), nullable=False)
    chapter_start = Column(Integer, nullable=True)
    chapter_end = Column(Integer, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class WizardNotice(Base):
    """引导/蒸馏的**持久化通知**（完成回执、疑似卡住提醒的投递队列）。

    ## 为什么不是「前端轮询到终态就直接画一条消息」

    那样确实能显示回执，但**只对当时开着页面的用户有效**：用户关掉页面去睡觉，
    服务重启后回到助手，那条 message 没有任何地方存着，就永远丢了。用户的原话是
    「蒸馏完成了会自己唤醒助手吧」——唤醒的消息必须能跨刷新、跨重启被再次捡起。

    所以通知落库、前端轮询、取走即 ack（`consumed_at`）。服务重启后：
    - 已完成但没被消费的行原样还在 → 下次进助手立刻补发；
    - 被 `reap_stale_runs` 收割成 failed 的任务 → 由收割逻辑补写一条
      `distill_failed`（interrupted=True），同样不会丢。

    ## 去重

    `(job_id, kind)` 唯一：同一个任务的成功回执/失败回执/卡住提醒各只有一条。
    前端 ack 之后即使再次轮询也不会重复弹。

    ## 为什么 payload 是 JSON 而不是若干列

    回执内容随任务类型变化（成功有章数/规则数/失败块，失败有错误原因，卡住有
    最后阶段和建议），拆成列会有一半永远是 NULL。payload 里存已渲染好的
    `message` + 结构化数字，前端直接展示，不需要再拼。
    """

    __tablename__ = "wizard_notices"
    __table_args__ = (
        UniqueConstraint("job_id", "kind", name="uq_wizard_notice_job_kind"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("distill_runs.id"), nullable=False)
    # distill_done | distill_failed | distill_stalled
    kind = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    # 前端取走并展示后回填；NULL = 还没被消费（重启后照样会被捡起来）
    consumed_at = Column(DateTime, nullable=True)


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
    """AI 腔反馈闭环的记忆行（Assistant 主导的全局/项目级「问题账本 + 方案库」）。

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

    ## 从「问题账本」升级为「可复用方案库」

    老版本只记**问题**（issue_key / hit_count / summary），不记**怎么修的**：
    每次同模式复发都要重新想一遍微调方法，LLM 成本与不确定性随复发次数线性
    增长。新增的 solution 系列字段把「当年沉淀的微调方法」也存下来——下次
    同模式命中时优先复用（零成本、确定性），只有方案被验证无效
    （flagged_ineffective）才重新规划。字段语义见各自行内注释。
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
    # —— 可复用方案库：问题账本只记「哪里犯了 AI 腔」，下面几个字段记「当时
    # 怎么修好的」，让下次同模式命中时先复用、后规划（见类注释末节）——
    # 当年沉淀的微调方法文案，即实际写进 constraint_note 的那段。渲染提示词时
    # 优先用它（那才是可执行的约束），没有它的老行才回落到 summary。
    # 空串 = 尚未规划过方案（老行 / 首见），由 resolve_tuning 的规划分支补上。
    solution = Column(Text, nullable=False, default="")
    # 方案来源：template（模板渲染，零成本）/ assistant_llm（冷门模式现调 LLM
    # 规划）/ fallback（LLM 不可用或调用失败时的通用兜底）。记来源是为了可
    # 观测：模板命中率、LLM 调用量、兜底占比都能从这一列统计，兜底占比升高
    # 说明 LLM 渠道出了问题，而不是「方案变简单了」。
    solution_origin = Column(String(16), nullable=False, default="")
    # 该方案被复用过多少次。>= 1 表示「这方案此前真的被应用过」——它是
    # recurrences 计数的前提：只统计「已生效方案」的复发，避免把从未被应用
    # 过的方案的复发误算成无效证据。
    times_reused = Column(Integer, nullable=False, default=0)
    # 最近一次复用（应用 / 重新规划）方案的时间。可空 = 从未被复用过。
    last_reused_at = Column(DateTime, nullable=True)
    # 方案生效后该问题又复发了多少次。复发说明方案可能没压住这个模式——
    # 单次复发可能是巧合，连续复发才是方案无效的信号。
    recurrences = Column(Integer, nullable=False, default=0)
    # 复发达到 INEFFECTIVE_RECURRENCES 后的「可能无效」标记。
    # 语义**不是删除、也不是停用**：历史行照常可查、可被 load；它改变的只是
    # 下次同模式命中时的决策——不盲目复用这段方案，改为重新规划一条新的，
    # 规划成功后本标记清零、计数重置，进入下一轮「应用 → 观察复发」循环。
    # 与 active=False 的分工：active 是人工的「这条教训整体不要了」；
    # 本标记是自动的「这个修法试试新的」。
    flagged_ineffective = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # 去重查找的加速索引；(scope, issue_key) 前缀即可覆盖 global 行的查找，
        # project 行的 project_id 等值条件由前缀过滤后在行级完成。
        Index("ix_ai_flavor_memory_key", "scope", "project_id", "issue_key"),
    )


class KgBibleRevision(Base):
    """plot_bible 的版本快照——只存修改后状态 + 顶层 diff。

    为什么是独立表（不复用 Fact）：
    - 8 类事实枚举（character/plothole/relationship/...）已固化；bible 修订是
      「元数据」不是事实，加 type=kg_bible_revision 会污染提醒/检索的过滤语义
    - bible_json 体量大（几 KB），与事实账本混存会让 list/详情序列化变慢
    - revision_no 需要严格单增，与 (project_id, revision_no) 联合唯一——Fact
      的 (type, key) 唯一不直接复用

    老项目无 revision 行属正常（迁移不补历史快照）。
    """

    __tablename__ = "kg_bible_revisions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    # 项目内 1,2,3,4 自增；同一项目下严格连续
    revision_no = Column(Integer, nullable=False)
    # 修改后的完整 bible dict（plot_bible 全字段，与 PATCH body 同形状）
    bible_json = Column(JSON, nullable=False)
    # 顶层 key 维度的 diff：{added: {...}, removed: {...}, changed: {key: {old, new}}}
    # 取自本次 PATCH body 的 plot_bible vs 落库前 proj.plot_bible
    diff_json = Column(JSON, nullable=False)
    # 本次 PATCH 触发的 KG 冲突清单（field 名 → 列表）
    conflicts_json = Column(JSON, nullable=True)
    # 来源标识：'user' = 人工改 / 'kg_lift' = KG 自动回流（暂未启用，结构预留）
    source = Column(String(16), nullable=False, default="user")
    # 备注（人工填的"为什么改"，可空）
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("project_id", "revision_no", name="uq_kg_bible_revision"),
    )


class AssistantWizardSession(Base):
    """助理对话里的「一步步生成」引导会话。

    为什么落库而不是放前端内存：用户问到一半关掉页面、切项目、刷新，
    回来必须停在同一题上；前端拼出来的状态一旦丢失就只能从头再问一遍。
    按 `project_id` 一条（同一项目同时只允许一个进行中的引导），
    `answers` 存已选项、`history` 存走过的步骤（用于「上一步」），
    `step_key` 指向当前问题。

    status: active | committed | aborted
      - active：引导进行中，GET 可直接恢复；
      - committed：已保存配置（可能已启动蒸馏/大纲），不再恢复为问题；
      - aborted：用户重新开始或放弃，保留行便于追溯。
    """

    __tablename__ = "assistant_wizard_sessions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    # original | continuation | role_chat；未选方向前为空。
    kind = Column(String(16), nullable=True)
    step_key = Column(String(32), nullable=False, default="branch")
    answers = Column(JSON, nullable=False, default=dict)
    history = Column(JSON, nullable=False, default=list)
    #: 本轮引导的流程改写（白名单约束，见 wizard_flow）：
    #: {"skip": ["outline"], "order": ["length", "style"], "custom": [{key,title,question,field,after}]}
    #: 与 answers/history 分开存：后两者的语义（已选项/回退栈）不能被流程编辑污染。
    overrides = Column(JSON, nullable=False, default=dict)
    #: 提交后的执行结果（stage: outline / ready / role / done，含蒸馏 job、大纲、
    #: 生成摘要）。刷新页面后据此恢复「已配置完」的状态，而不是重新问一遍。
    result = Column(JSON, nullable=True)
    status = Column(String(16), nullable=False, default="active", index=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class AssistantMessage(Base):
    """助理对话的**逐条记录**（跨退出存活的历史流）。

    用户原话：「agent 对话那里的记录也得保存下来，不能退出就没有了」。
    之前只有 `AssistantWizardSession` 存了状态机（当前第几步、已选项），
    前端 `turns` 是纯内存——刷新/关页/换标签页，前面说过的话就全没了。

    ## 为什么和 `AssistantWizardSession` 分开

    会话表是**状态机**（停在哪一题、能退到哪一步），本表是**对话流**
    （谁说了什么）。合并成一张表会让「上一步」逻辑和「历史回显」互相污染：
    历史里既有能被重新提交的卡片，也有只该只读回放的解释与通知。

    ## 归属

    按 `project_id` 归属（用户重进的是这个项目的助手，换一轮引导不该把旧对话
    藏起来），`session_id` 只标注这条消息属于哪一轮引导，可为空（普通问答不
    属于任何一轮引导）。

    ## kind

    plain（问答/解释文字）| wizard_question（引导出题卡片）|
    wizard_action（改配置回执）| wizard_result（提交后的执行结果）|
    notice（蒸馏回执/卡住提醒）| role_chat（角色对话）。

    `payload` 只存重建气泡所需的**结构化最小集**（题面、结果、通知 payload），
    不存 LLM 原始响应。
    """

    __tablename__ = "assistant_messages"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "dedupe_key", name="uq_assistant_message_dedupe"
        ),
        Index("ix_assistant_messages_project", "project_id", "id"),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    #: 所属引导会话；普通问答为 NULL
    session_id = Column(Integer, nullable=True)
    #: user | assistant
    role = Column(String(16), nullable=False)
    kind = Column(String(32), nullable=False, default="plain")
    text = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=True)
    #: 幂等键：同一条消息重复写入时靠它去重（如 notice:{id}）。NULL 不参与唯一约束。
    dedupe_key = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False)


class WizardActionAudit(Base):
    """助理在引导里执行的每一次写操作（审计 + 撤销依据）。

    产品约束：助理可以改**项目级配置与会话状态**（档位、采样章数、字数、
    题材、风格、大纲、本次引导流程），但不能改全局配置、代码、其他项目，
    也不能删除/覆盖已有正文。每次写操作都落一行，回复里必须能说出
    「改了什么、从什么改成什么、影响是什么」，并且可以撤销。

    ``undo_payload`` 是撤销时回写的旧值（结构因 target 而异）；
    ``trigger_distill`` 这类已经开始跑的任务标 ``undoable=False``——
    撤销只能恢复参数，不能假装任务没跑过。
    """

    __tablename__ = "wizard_action_audits"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    session_id = Column(Integer, nullable=True)
    # assistant | user（当前只由助理在对话里触发；保留字段便于以后区分）
    actor = Column(String(16), nullable=False, default="assistant")
    # 触发时所在步骤（branch / distill / genre…），便于按对话回看
    step_key = Column(String(32), nullable=True)
    # set_tier | set_sampling | trigger_distill | set_length | set_genre |
    # set_style | set_outline | skip_step | move_step | insert_question
    action = Column(String(32), nullable=False)
    # 被改的对象：project.narrative_tier / project.generation_length /
    # session.answers.outline / session.overrides …
    target = Column(String(64), nullable=False)
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    # 给人看的一句影响说明（耗时、生效时机、不可逆点）
    impact = Column(Text, nullable=True)
    undoable = Column(Boolean, nullable=False, default=True)
    undo_payload = Column(JSON, nullable=True)
    undone_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow, nullable=False, index=True)
