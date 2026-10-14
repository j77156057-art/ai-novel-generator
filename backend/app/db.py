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


def init_db():
    import app.models  # noqa: F401  (确保模型注册到 Base.metadata)

    Base.metadata.create_all(bind=engine)

    # —— 增量迁移：对已存在的 sqlite 库补齐新增列，避免清库重灌 ——
    if IS_SQLITE:
        _migrate_sqlite_columns()


def _migrate_sqlite_columns() -> None:
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
        ("raw_novels", "style_profile", "JSON"),
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
    with engine.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
