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
    target_total_chars = Column(Integer, nullable=True)  # 整本目标字数；None 表示不强求规划
    # 层级摘要树（L1 段 / L2 卷 / L3 主线骨架）。
    # summary 仍是**无损的 L1 归档**，但注入提示词的已是本字段拼出的有界前情
    # （summary_tree.compose_prior）——summary 从「注入源」退为「存档」。
    summary_tree = Column(JSON, nullable=True)
    # 文体档案缓存：从本项目选定的参考语料实测拟合（`style.build_reference_profile`）。
    # 存下来是为了**稳定**——档案一旦拟合就不再随请求变化，`describe()` 的文本
    # 得以整轮恒定地待在 system 提示里，不击穿提示词缓存。
    # 空表示「自由生成」（未选参考语料），此时不注入文风档案块。
    style_profile = Column(JSON, nullable=True)


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
