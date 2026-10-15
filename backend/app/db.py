from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import event

from app.config import settings

Base = declarative_base()

DATABASE_URL = settings.database_url
IS_SQLITE = DATABASE_URL.startswith("sqlite")

connect_args = {"check_same_thread": False} if IS_SQLITE else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


if not IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _enable_pgvector(dbapi_conn, *args):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:  # pragma: no cover - 扩展已存在时跳过
            pass
        finally:
            cur.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """服务启动时的表结构初始化（建缺失表 + 补缺失列）。"""
    ensure_schema()


def ensure_schema(target_engine=None) -> None:  # noqa: ANN001  (SQLAlchemy Engine | None)
    """建缺失表 + 对已存在的 sqlite 库补齐新增列（幂等）。

    抽出成公共函数供**两个入口**复用，避免离线脚本绕过迁移：

    - 服务启动 `init_db()` —— 用模块级默认 engine；
    - 离线脚本 `load_corpus.py` —— 用它自己的**绝对路径** engine。

    为什么要给离线脚本复用：`load_corpus.py` 直接 `create_engine()` 建连接
    （不经 `app.db`），若不走这一段，已存在的生产库里就没有第 8 步新增的
    `raw_novels.clean_report` 列，导入时会 `no such column: clean_report` 失败。

    Args:
        target_engine: 目标引擎。缺省用本模块的 `engine`。
    """
    import app.models  # noqa: F401  (确保模型注册到 Base.metadata)

    eng = target_engine if target_engine is not None else engine
    Base.metadata.create_all(bind=eng)

    # —— 增量迁移：对已存在的 sqlite 库补齐新增列，避免清库重灌 ——
    if str(eng.url).startswith("sqlite"):
        _migrate_sqlite_columns(eng)


def _migrate_sqlite_columns(target_engine=None) -> None:  # noqa: ANN001
    """补齐 sqlite 库缺失的列。`target_engine` 缺省用模块级 `engine`。"""
    from sqlalchemy import text

    # (表, 列, 类型) —— 仅当列不存在时才 ALTER ADD。
    #
    # 幂等性：每项都先PRAGMA table_info 查列，缺才ALTER；重复调用 init_db()
    # 不会重复加列、不会报错。新表（prompt_overrides）不需要出现在这里——
    # Base.metadata.create_all 会建好；这里只处理「已存在的库补列」。
    migrations = [
        ("projects", "genre", "VARCHAR(32)"),
        ("projects", "style", "VARCHAR(32)"),
        ("projects", "premise", "TEXT"),
        ("projects", "reference_novel_ids", "JSON"),
        ("projects", "plot_bible", "JSON"),
        ("projects", "generation_length", "INTEGER NOT NULL DEFAULT 3000"),
        ("projects", "summary_tree", "JSON"),
        ("projects", "style_profile", "JSON"),
        # —— 第 10 步 P2 叙事档案：独立于 style_profile 的 JSON 列——
        # 漏这一项的后果：create_all 只给**新库**建该列，已存在的库永远补不上，
        # 任何 SELECT projects.* 都会 `no such column: projects.style_narrative`。
        # 详见 tests/test_schema_migration_completeness.py。
        ("projects", "style_narrative", "JSON"),
        # —— 文风蒸馏三档：项目级「用哪一档算」的偏好（full/balanced/fast）——
        # nullable：既有项目的 style_narrative 是按默认完整档算的，补 NULL 不破坏它们。
        # 漏这一项的后果与 style_narrative 相同：老库 SELECT projects.* 直接炸。
        ("projects", "narrative_tier", "VARCHAR(16)"),
        # —— §11.3 时间线状态存档：节点+进度一体化 JSON ——
        # 漏这一项的后果：create_all 只给**新库**建该列，已存在的库永远补不上，
        # 任何 SELECT projects.* 都会 `no such column: projects.plot_timeline`，
        # 时间线状态就退化回「每次重启重放」旧行为（功能不挂，但失去持久化收益）。
        ("projects", "plot_timeline", "JSON"),
        # —— v2.4 高潮密度蒸馏档：avg_node_segments 从语料章节情绪反推 ——
        # 漏这一项后果与 plot_timeline 相同：老库 SELECT projects.* 直接炸。
        # 该列 nullable：未蒸馏时为空，回退到首轮默认 avg_node_segments=5。
        ("projects", "plot_rhythm", "JSON"),
        ("raw_novels", "style_profile", "JSON"),
        # —— 第 8 步：语料清洗报告（导入时剔除水印的留痕）——
        ("raw_novels", "clean_report", "JSON"),
        ("kg_nodes", "source", "VARCHAR(64)"),
        # —— ADR-2026-10-12 第 5 步第 4 步：EditProposal 补齐提案生命周期字段——
        # 全部 nullable：既有行（第 2/3 步写入的）不该被 NOT NULL 破坏。
        ("edit_proposals", "project_id", "INTEGER"),
        ("edit_proposals", "type", "VARCHAR(32)"),
        ("edit_proposals", "target_kind", "VARCHAR(32)"),
        ("edit_proposals", "scope_json", "JSON"),
        ("edit_proposals", "before_text", "TEXT"),
        ("edit_proposals", "after_text", "TEXT"),
        ("edit_proposals", "reason", "TEXT"),
        ("edit_proposals", "evidence_json", "JSON"),
        ("edit_proposals", "version_id", "INTEGER"),
        ("edit_proposals", "created_by", "VARCHAR(64)"),
        ("edit_proposals", "created_at", "DATETIME"),
        ("edit_proposals", "approved_by", "VARCHAR(64)"),
        ("edit_proposals", "approved_at", "DATETIME"),
        ("edit_proposals", "applied_at", "DATETIME"),
        # —— 第 7 步：AgentRun 增加调用性质说明列（invoke-generation 用）——
        # nullable：既有审计行（第 2/3/4 步写入的）没有这项，补 NULL 不破坏它们。
        ("agent_runs", "note", "TEXT"),
        # —— Version 补齐快照指纹与归属——
        ("versions", "project_id", "INTEGER"),
        ("versions", "kind", "VARCHAR(16)"),
        ("versions", "content_hash", "VARCHAR(64)"),
        ("versions", "source_proposal_id", "INTEGER"),
        ("versions", "is_restore", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    # v3.0 KG 索引定义（ensure_schema 同步建，_migrate_sqlite_columns 老库补建）
    v30_indexes = [
        # (table, idx_name, cols_sql, note)
        ("kg_entities", "ix_kg_entity_project_kind", "project_id, kind", "切片/批抽扫描 + 归一"),
        ("kg_entities", "ix_kg_entity_project_canonical", "project_id, canonical_name", "归一 by canonical_name"),
        ("kg_relations", "ix_kg_relation_project", "project_id", "切片扫关系"),
        ("kg_relation_evolution", "ix_kg_relation_evolution_edge", "edge_id", "关系曲线展开"),
        ("kg_events", "ix_kg_event_project_sequence", "project_id, sequence", "切片取最近 N 段 + 批抽 max(sequence)"),
        ("kg_events", "ix_kg_event_project_segment", "project_id, segment_no", "按段号反查事件"),
        ("kg_event_links", "ix_kg_event_link_from", "from_event_id", "因果链正向"),
        ("kg_event_links", "ix_kg_event_link_to", "to_event_id", "因果链反向"),
        ("kg_character_state", "ix_kg_char_state_project_char", "project_id, character_id", "按角色取最新状态"),
        ("kg_character_state", "ix_kg_char_state_project_segment", "project_id, segment_no", "按段号查状态"),
        ("kg_foreshadows", "ix_kg_foreshadow_project_status", "project_id, status", "active 过滤 + Judge 超期"),
        ("kg_entity_knowledge", "ix_kg_entity_knowledge_project_entity", "project_id, entity_id", "Judge 预知 + 切片 known_facts"),
        ("kg_mentions", "ix_kg_mention_project_segment", "project_id, segment_no", "切片核心：按段号取最近 N 段"),
        ("kg_mentions", "ix_kg_mention_project_entity", "project_id, entity_id", "mention_count 聚合"),
    ]
    eng = target_engine if target_engine is not None else engine
    with eng.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
        # 老库补建 v3.0 索引（IF NOT EXISTS 幂等，新库 create_all 已建则跳过）
        for table, idx_name, cols_sql, _note in v30_indexes:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({cols_sql})")) if target_engine is not None else engine
    with eng.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
