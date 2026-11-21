"""pytest 全局配置：把 backend/ 加入 sys.path + **强制测试用独立临时库**。

## 为什么必须在这里做库隔离（这是本文件存在的首要理由）

此前测试进程共享 `app.db` 的**全局 engine**，而该 engine 由
`settings.database_url` 建立（默认 `sqlite:///./novel_dev.db`，**相对 CWD 解析**）。
于是任何人在 `backend/` 目录下跑一次 pytest，形如下面这些测试的 `drop_all()`
就会把**真实开发库** `backend/novel_dev.db` 清空：

    Base.metadata.drop_all(bind=engine)   # test_settings_roles / test_assistant_readonly …

用户的 1066 个分片语料（外加项目数据）就是这样没的。

## 本文件的做法

1. 在**任何**测试模块 import `app.db` 之前，把 `DATABASE_URL` 环境变量改写成
   pytest 的**独立临时文件库**，并把 `settings.database_url` 一并覆盖；
2. 只在 `tmp` 类目录里建库，**绝不**读 CWD、**绝不**复用 `settings.database_url`
   里可能的相对路径；
3. 若外部显式传入的 `DATABASE_URL` 指向**真实开发库**，**拒绝并报错**（不是静默覆盖，
   而是要让人知道自己差点清库）；
4. 会话结束先 `engine.dispose()` 再删掉临时库文件与专属临时目录。

配合 `tests/test_db_isolation.py` 的护栏测试（比对跑测前后真实库的
mtime+size+sha256 不变），形成「机制 + 验证」双重保障。

## ⚠️ 为什么**不能**沿用「继承来的 DATABASE_URL」

原来 (b) 分支是「显式传入的库若落在已存在的目录里 → 沿用」。
看起来是「尊重调用方」，但**子进程会继承父进程的 `DATABASE_URL`** ⇒

    父进程 pytest（库 A）
      └─ spawn 子进程 pytest（继承 DATABASE_URL=库 A）
           └─ 子进程 conftest 沿用库 A → 子进程退出时 atexit **删掉库 A**

⇒ 父进程后续所有用共享 engine 的用例全部炸，报
`sqlite3.OperationalError: attempt to write a readonly database`
（Linux 上 POSIX 允许 unlink 打开中的文件；**Windows 上删不掉**被打开的文件，
PermissionError 被 `except OSError: pass` 吞掉 ⇒ **本机全绿，只有 Linux CI 红**）。

⇒ 现在：**每个进程一个专属临时目录**（`tempfile.mkdtemp()`），
继承来的 `DATABASE_URL` **只当安全检查**（指向真实开发库 ⇒ 拒绝），不再当位置来源。
想指定目录用 `AI_NOVEL_TEST_DB_DIR`。护栏见
`tests/test_db_isolation.py::test_child_process_does_not_reuse_this_sessions_db`。
"""

from __future__ import annotations

import atexit
import hashlib
import os
import sqlite3
import sys
import tempfile

import pytest

# ─── 1) sys.path：让 `from app.xxx import` 在测试里可用 ────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ─── 2) 真实库文件清单（绝对路径，**不看 CWD**）────────────────────────────
_REPO_ROOT = os.path.abspath(os.path.join(_BACKEND, ".."))
_REAL_DB_FILES = {
    os.path.abspath(os.path.join(_BACKEND, "novel_dev.db")),
    os.path.abspath(os.path.join(_BACKEND, "novel_demo.db")),
    os.path.abspath(os.path.join(_REPO_ROOT, "novel_dev.db")),
}


def _fingerprint(path: str) -> dict | None:
    """文件的 mtime + size + sha256 指纹；文件不存在返回 None。"""
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"size": st.st_size, "mtime": st.st_mtime, "sha256": h.hexdigest()}


#: KG worker 心跳键前缀。别的会话在跑生成/回填时，worker 每 15s 往 `app_settings`
#: 写一次 `kg.worker.<id>`，整库字节必然变化——这属正常活动，不是「测试写坏真实库」。
HEARTBEAT_KEY_PREFIX = "kg.worker."

#: 超过这个行数的表只比行数、不逐行摘要：414MB 的主库逐行摘要把门禁拖慢到不可接受，
#: 而「测试误写真实库」现实形态是清表/建表/批量插入，这些都会先被行数与 schema 抓住。
_MAX_INLINE_ROWS = 5000


def _db_logical_fingerprint(path: str) -> dict | None:
    """真实库的**逻辑内容**指纹：schema + 每表行数 + 小表逐行摘要（跳过心跳行）。

    与 `_fingerprint`（整文件字节）的分工：
      - 字节指纹最敏感，但会被 15s 一次的心跳噪声打红；
      - 逻辑指纹只跳过 `app_settings` 里的 `kg.worker.%` 行，其余照旧，既不会被
        心跳误伤，也仍能抓住「测试 drop_all / 清表 / 插数据」。
    文件不存在或打不开时返回 None（干净检出场景）。
    """
    if not os.path.isfile(path):
        return None
    uri = "file:" + path.replace("\\", "/") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.Error:
        return None
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        ]
        schema = hashlib.sha256()
        for name, sql in conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"):
            schema.update(f"{name}\x00{sql or ''}\n".encode())

        counts: dict[str, int] = {}
        content = hashlib.sha256()
        heartbeat_rows = 0
        for table in tables:
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            where = ""
            if table == "app_settings" and "key" in cols:
                where = f" WHERE key NOT LIKE '{HEARTBEAT_KEY_PREFIX}%'"
                heartbeat_rows = conn.execute(
                    f'SELECT count(*) FROM "{table}" '
                    f"WHERE key LIKE '{HEARTBEAT_KEY_PREFIX}%'"
                ).fetchone()[0]
            n = conn.execute(f'SELECT count(*) FROM "{table}"{where}').fetchone()[0]
            counts[table] = n
            if n <= _MAX_INLINE_ROWS:
                for row in conn.execute(f'SELECT * FROM "{table}"{where}'):
                    content.update((table + "\x00" + repr(tuple(row)) + "\n").encode())
        return {
            "schema": schema.hexdigest(),
            "counts": counts,
            "content": content.hexdigest(),
            "heartbeat_rows": heartbeat_rows,
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


#: 会话开始（import conftest 时）对真实库做的**逻辑**内容快照，供护栏测试比对。
#: 只跳过 `kg.worker.%` 心跳行，其余（schema/行数/小表内容）都要逐项不变。
REAL_DB_SNAPSHOT: dict[str, dict | None] = {
    p: _db_logical_fingerprint(p) for p in sorted(_REAL_DB_FILES)
}

REAL_DB_FILES: tuple[str, ...] = tuple(sorted(_REAL_DB_FILES))


def _url_to_path(url: str) -> str | None:
    """把 sqlite URL 转成绝对文件路径；非 sqlite 或内存库返回 None。"""
    if not url or not url.startswith("sqlite"):
        return None
    rest = url[len("sqlite:"):]
    if rest.startswith("////"):
        path = rest[3:]          # sqlite:////abs/path      -> /abs/path
    elif rest.startswith("///"):
        path = rest[3:]          # sqlite:///rel/path.db    -> rel/path.db
    elif rest.startswith("//"):
        path = rest[2:]
    else:
        path = rest
    if path in ("", "/", ":memory:") or path.startswith(":memory:"):
        return None
    return os.path.abspath(path)


#: 本进程专属的临时目录（库文件放在里面；退出时整个目录删掉）。
#:
#: ⚠️ 声明必须**在** `_force_isolated_database()` 调用之前 —— 否则模块加载到
#: 那行「注解 + 赋值 None」时会把函数里刚设好的路径**重置**掉，清理就不删目录了。
_TEMP_DB_DIR: str | None = None


def _force_isolated_database() -> str:
    """把测试库强制指向**本进程专属**的临时文件，返回最终使用的 URL。

    Raises:
        RuntimeError: 显式传入的 `DATABASE_URL` 指向真实开发库（拒绝执行）。
    """
    global _TEMP_DB_DIR
    incoming = os.environ.get("DATABASE_URL", "")
    incoming_path = _url_to_path(incoming) if incoming else None

    # (a) 显式指向真实库 → 拒绝，绝不静默覆盖（这是「差点清库」的高危信号）。
    if incoming_path and os.path.abspath(incoming_path) in _REAL_DB_FILES:
        raise RuntimeError(
            "拒绝用真实开发库跑测试：环境变量 DATABASE_URL 指向 "
            f"{os.path.abspath(incoming_path)!r}（真实库）。\n"
            "本测试套件已在 conftest 里强制隔离到临时库；请**取消**该环境变量，"
            "或把它指向一个临时目录，切勿指向 novel_dev.db / novel_demo.db。"
        )

    # (b) 库的位置：**只看**显式的 AI_NOVEL_TEST_DB_DIR，否则每个进程开一个
    #     **专属**临时目录。
    #
    #     ⚠️ 绝**不**沿用「继承来的 DATABASE_URL」—— 子进程会继承父进程的值，
    #     沿用它就等于让子进程去动父进程的库（它退出时会把那个文件删掉）。
    #     详见模块 docstring 里那节「为什么不能沿用继承来的 DATABASE_URL」。
    base_dir = os.environ.get("AI_NOVEL_TEST_DB_DIR", "").strip()
    if base_dir:
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            base_dir = ""
    if base_dir:
        target = os.path.join(base_dir, f"ai_novel_pytest_{os.getpid()}.db")
    else:
        _TEMP_DB_DIR = tempfile.mkdtemp(prefix="ai_novel_pytest_")
        target = os.path.join(_TEMP_DB_DIR, "test.db")

    url = "sqlite:///" + target.replace("\\", "/")
    os.environ["DATABASE_URL"] = url
    return url


_TEMP_DB_URL = _force_isolated_database()

# ─── 3) 在任何测试模块 import app.db 之前，锁定 settings + engine ──────────
import app.config as _app_config  # noqa: E402

# 双保险：即使 app.config 已被提前 import（例如被某个插件），也把它掰到临时库。
_app_config.settings.database_url = _TEMP_DB_URL

import app.db as _app_db  # noqa: E402  (engine 此刻绑定到临时库)

_bound = str(_app_db.engine.url)
if _url_to_path(_bound) is None or os.path.abspath(
    _url_to_path(_bound) or ""
) in _REAL_DB_FILES:
    raise RuntimeError(
        f"测试库隔离失败：app.db.engine 指向 {_bound!r}，疑似真实库。"
        "拒绝继续（防止清空真实开发库）。"
    )


def _cleanup_temp_db() -> None:
    """会话结束：删掉临时库文件（连同可能产生的 -wal/-shm）与专属临时目录。

    ⚠️ 必须先 `engine.dispose()`：**Windows 上「进程自己打开的文件」也删不掉**。
    连接池里挂着一个没关的 SQLite 连接，`os.remove` / `rmtree` 就会失败
    （前者被 `except OSError` 吞掉、后者靠 `ignore_errors=True` 静默跳过）
    ⇒ 每跑一次测试就在临时目录留一个 test.db，攒久了满地是库。
    """
    app_db = sys.modules.get("app.db")
    if app_db is not None:
        try:
            app_db.engine.dispose()
        except Exception:  # pragma: no cover - 解释器收尾时模块可能已被拆
            pass

    path = _url_to_path(_TEMP_DB_URL)
    if path:
        for suffix in ("", "-wal", "-shm"):
            try:
                if os.path.isfile(path + suffix):
                    os.remove(path + suffix)
            except OSError:
                pass
    if _TEMP_DB_DIR:
        import shutil

        shutil.rmtree(_TEMP_DB_DIR, ignore_errors=True)


atexit.register(_cleanup_temp_db)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """本会话实际使用的（临时）测试库 URL。"""
    return _TEMP_DB_URL


@pytest.fixture(scope="session")
def real_db_snapshot() -> dict[str, dict | None]:
    """会话开始时对真实库打的指纹快照（供护栏测试比对）。"""
    return REAL_DB_SNAPSHOT


@pytest.fixture
def experimental_consent():
    """在用例期间显式授予实验性诊断权限（对应 /qa 里按用户原话设置的上下文）。"""
    from app.agents.consent import (
        reset_experimental_consent,
        set_experimental_consent,
    )

    token = set_experimental_consent(True)
    try:
        yield
    finally:
        reset_experimental_consent(token)
