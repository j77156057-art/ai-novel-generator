"""真实库防误删防线（2026-10-17 清库事故的结构性根治）的测试。

## 这道防线解决什么

那次事故是某脚本对 `<app.db 的模块级 engine>` 调 `Base.metadata.drop_all()`，
而该 engine 的 URL 来自 `settings.database_url`（默认 `sqlite:///./novel_dev.db`，
**相对 CWD 解析**）——一句 drop_all 就把真实开发库清空，7941 个分片全丢。

只靠「改默认值」或「每个脚本自己设 DATABASE_URL」都堵不住：
- `config.Settings` 用 `env_file=".env"`（相对 CWD 自动加载），在 backend/ 下跑的
  任何进程都读到同一份 .env，默认值并不能把它和真实库分开；
- 「靠纪律」挡不住未来新写的脚本。

所以防线放在**驱动层**：`app/db.py` 监听 `Engine` 类的 `before_cursor_execute`，
在真实库上拒绝 `DROP TABLE`。与入口无关（pytest / 手工脚本 / `python -c` 都一样）。

## 本文件要证明什么

1. 文件名判定正确（真实库名 / 一次性 scratch / 内存库）；
2. 真实库上的 `drop_all` **真的会抛错**，且表**没有**被删掉；
3. 显式 `ALLOW_DESTRUCTIVE_DB=1` 时放行（需要重置真库的人有出口）；
4. 一次性 scratch 库上的 `drop_all` 不受影响（别把正常测试打死）；
5. 本测试自己**没有碰**真实库。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

import app.models  # noqa: F401  确保 metadata 里注册了表
from app.db import Base, is_protected_db

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
_REAL = (_BACKEND / "novel_dev.db", _BACKEND / "novel_demo.db")


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<absent>"


def _real_snapshot() -> dict[str, str]:
    return {p.name: _sha256(p) for p in _REAL}


def _engine(path: Path):
    return create_engine(f"sqlite:///{path.as_posix()}", future=True)


def _table_count(p: Path) -> int:
    con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(
            "select count(*) from sqlite_master where type='table'"
        ).fetchone()[0]
    finally:
        con.close()


# ── 1. 文件名判定 ────────────────────────────────────────────────────────
def test_is_protected_db_detects_real_names(tmp_path):
    assert is_protected_db(f"sqlite:///{tmp_path.as_posix()}/novel_dev.db")
    assert is_protected_db(f"sqlite:///{tmp_path.as_posix()}/novel_demo.db")
    # 一次性沙箱名不该被当成真实库
    assert not is_protected_db(f"sqlite:///{tmp_path.as_posix()}/_scratch.db")
    assert not is_protected_db(f"sqlite:///{tmp_path.as_posix()}/ai_novel_pytest_123.db")
    # 内存库与非 sqlite 都不在保护范围
    assert not is_protected_db("sqlite:///:memory:")
    assert not is_protected_db("postgresql+psycopg://u:p@localhost/db")


# ── 2. 真实库上 drop_all 被拒，且表没丢 ──────────────────────────────────
def test_drop_all_refused_on_protected_db_name(tmp_path):
    """核心断言：防线真的会拦，且拦下后数据仍在。"""
    db = tmp_path / "novel_dev.db"          # **临时目录**里的同名文件，不是真实库
    eng = _engine(db)
    Base.metadata.create_all(bind=eng)
    assert _table_count(db) > 0, "前置：先建出表，才能证明 drop 真的没发生"

    with pytest.raises(RuntimeError, match="拒绝在真实库上执行 DROP TABLE"):
        Base.metadata.drop_all(bind=eng)

    assert _table_count(db) > 0, "被拒后表必须还在（否则这防线等于没拦）"


# ── 3. 显式 opt-in 放行 ──────────────────────────────────────────────────
def test_drop_all_allowed_with_explicit_optin(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_DESTRUCTIVE_DB", "1")
    db = tmp_path / "novel_dev.db"
    eng = _engine(db)
    Base.metadata.create_all(bind=eng)
    Base.metadata.drop_all(bind=eng)        # 有显式 opt-in → 放行
    assert _table_count(db) == 0


# ── 4. 一次性沙箱库不受影响 ──────────────────────────────────────────────
def test_drop_all_allowed_on_scratch_db(tmp_path):
    db = tmp_path / "_scratch.db"
    eng = _engine(db)
    Base.metadata.create_all(bind=eng)
    Base.metadata.drop_all(bind=eng)        # 非真实库 → 不该被拦
    assert _table_count(db) == 0


# ── 5. 本测试自己没碰真实库 ──────────────────────────────────────────────
def test_this_suite_never_touched_real_db():
    """护栏：整个模块跑下来，真实库必须一字节未变。"""
    # 会话级快照由 conftest 建立；这里只做「当前仍存在且非空」的最低断言，
    # 真正的字节级比对在 tests/test_db_isolation.py。
    for p in _REAL:
        if p.is_file():
            assert p.stat().st_size > 0, f"{p} 变成空文件了——防线失效"


def test_opt_in_is_not_set_by_default():
    """防线默认是开的：环境变量不该被测试进程遗留地打开。"""
    assert os.environ.get("ALLOW_DESTRUCTIVE_DB") != "1"