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
4. 会话结束删掉临时库文件。

配合 `tests/test_db_isolation.py` 的护栏测试（比对跑测前后真实库的
mtime+size+sha256 不变），形成「机制 + 验证」双重保障。
"""

from __future__ import annotations

import atexit
import hashlib
import os
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


#: 会话开始（import conftest 时）对真实库做的快照，供护栏测试比对。
REAL_DB_SNAPSHOT: dict[str, dict | None] = {
    p: _fingerprint(p) for p in sorted(_REAL_DB_FILES)
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


def _force_isolated_database() -> str:
    """把测试库强制指向独立临时文件，返回最终使用的 URL。

    Raises:
        RuntimeError: 显式传入的 `DATABASE_URL` 指向真实开发库（拒绝执行）。
    """
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

    # (b) 允许的临时目标选择：
    #     - 显式传入的库若落在已存在的目录里 → 沿用（尊重「用 D:/Temp 下的临时库」）；
    #     - 否则退到 AI_NOVEL_TEST_DB_DIR，再退到系统临时目录。
    if incoming_path and os.path.isdir(os.path.dirname(incoming_path)):
        target = incoming_path
    else:
        base_dir = os.environ.get("AI_NOVEL_TEST_DB_DIR", "") or tempfile.gettempdir()
        if not os.path.isdir(base_dir):
            base_dir = tempfile.gettempdir()
        target = os.path.join(base_dir, f"ai_novel_pytest_{os.getpid()}.db")

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
    """会话结束：删掉临时库文件（连同可能产生的 -wal/-shm）。"""
    path = _url_to_path(_TEMP_DB_URL)
    if not path:
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            if os.path.isfile(path + suffix):
                os.remove(path + suffix)
        except OSError:
            pass


atexit.register(_cleanup_temp_db)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """本会话实际使用的（临时）测试库 URL。"""
    return _TEMP_DB_URL


@pytest.fixture(scope="session")
def real_db_snapshot() -> dict[str, dict | None]:
    """会话开始时对真实库打的指纹快照（供护栏测试比对）。"""
    return REAL_DB_SNAPSHOT
