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

## ⚠️ 为什么**不能**沿用「继承来的 DATABASE_URL」（2026-10-27 踩过，只在 Linux 炸）

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
想指定目录用 `AI_NOVEL_TEST_DB_DIR`。
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

# 测试辅助模块（原本的一次性工具脚本，逻辑被测试覆盖）：
#   让 `from probe_closed_inventory import ...` 这类 import 直接可用。
_HELPERS = os.path.abspath(os.path.join(os.path.dirname(__file__), "helpers"))
if _HELPERS not in sys.path:
    sys.path.insert(0, _HELPERS)

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
    global _TEMP_DB_DIR
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
    """会话结束：删掉临时库文件（连同可能产生的 -wal/-shm）与专属临时目录。"""
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
    """「用户已授权实验性诊断」这个前提。

    公开工具 `run_diagnostic_task` 现在**自己**不放行实验技能：放行凭据是
    用户那条消息里的原话（`app.agents.consent`），模型伪造不了。走工具的
    测试要显式给出这个前提 —— 否则就等于默认「用户同意了」。
    """
    from app.agents.consent import reset_experimental_consent, set_experimental_consent
    token = set_experimental_consent(True)
    try:
        yield True
    finally:
        reset_experimental_consent(token)
