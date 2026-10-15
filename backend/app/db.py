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
    eng = target_engine if target_engine is not None else engine
    with eng.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
