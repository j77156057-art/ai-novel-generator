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

    # (表, 列, 类型) —— 仅当列不存在时才 ALTER ADD
    migrations = [
        ("projects", "genre", "VARCHAR(32)"),
        ("projects", "style", "VARCHAR(32)"),
        ("projects", "premise", "TEXT"),
        ("projects", "reference_novel_ids", "JSON"),
        ("projects", "plot_bible", "JSON"),
        ("projects", "generation_length", "INTEGER NOT NULL DEFAULT 3000"),
        ("projects", "target_total_chars", "INTEGER"),
        ("projects", "summary_tree", "JSON"),
        ("projects", "style_profile", "JSON"),
        ("raw_novels", "style_profile", "JSON"),
        ("kg_nodes", "source", "VARCHAR(64)"),
    ]
    with engine.begin() as conn:
        for table, col, coltype in migrations:
            existing = {
                r[1]
                for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
