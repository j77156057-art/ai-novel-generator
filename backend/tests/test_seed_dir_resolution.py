"""SEED_DIR 解析与「目录不存在」的失败语义。

覆盖：
- 默认 seed_dir 是绝对路径，且指向 <仓库根>/data/seed（与启动时的工作目录无关）
- 相对路径覆盖值按仓库根解析，而不是按进程 CWD
- 目录不存在时 resolve_seed_dir / run_seed 抛 FileNotFoundError，不再静默返回 []
- POST /api/novels/seed 在目录缺失时返回 400 + 具体路径

跑法：python tests/test_seed_dir_resolution.py  或 pytest tests/test_seed_dir_resolution.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import REPO_ROOT, settings
from app.db import Base, get_db
import app.models  # noqa: F401
from app.main import app
from app.ingestion.seed import resolve_seed_dir, run_seed


def _memory_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def test_default_seed_dir_is_absolute_under_repo_root():
    path = Path(settings.seed_dir)
    assert path.is_absolute(), f"默认 seed_dir 必须是绝对路径，实际：{settings.seed_dir!r}"
    assert path == REPO_ROOT / "data" / "seed"


def test_relative_seed_dir_resolves_from_repo_root_not_cwd(tmp_path, monkeypatch):
    # 把 CWD 挪到别处：旧实现会解析成 tmp/data/seed（不存在），新实现必须仍然
    # 落在仓库根的 data/seed 上。
    monkeypatch.chdir(tmp_path)
    assert resolve_seed_dir("data/seed") == REPO_ROOT / "data" / "seed"


def test_missing_seed_dir_raises_instead_of_returning_empty(tmp_path):
    missing = tmp_path / "no-such-seed"
    try:
        resolve_seed_dir(str(missing))
    except FileNotFoundError as exc:
        assert str(missing) in str(exc)
    else:
        raise AssertionError("目录不存在时必须抛 FileNotFoundError，不能静默返回空结果")


def test_run_seed_raises_when_dir_missing(tmp_path):
    Session = _memory_session()
    db = Session()
    try:
        run_seed(db, str(tmp_path / "no-such-seed"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("run_seed 在目录缺失时必须抛错，不能返回 []")
    finally:
        db.close()


def test_seed_endpoint_returns_400_when_dir_missing(tmp_path, monkeypatch):
    Session = _memory_session()

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    missing = tmp_path / "no-such-seed"
    monkeypatch.setattr(settings, "seed_dir", str(missing))
    try:
        resp = TestClient(app).post("/api/novels/seed")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}: {resp.text}"
    assert "SEED_DIR" in resp.json()["detail"]
    assert str(missing) in resp.json()["detail"]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
