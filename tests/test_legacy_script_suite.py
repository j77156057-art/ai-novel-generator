"""把 `backend/` 下老式 `sys.exit()` 测试脚本纳入门禁。

## 为什么需要这一层

`backend/tests/` 与 `backend/test_*.py` 里的 44 个文件**不是 pytest 用例**：它们
在模块级跑断言、末尾 `sys.exit(0/1)`。pytest 直接收会 `INTERNALERROR: SystemExit`，
所以它们长期游离在门禁之外。

代价已经真实发生过：`provider.chat()` 在 v2.6 加了 `stop_sequences` 之后，其中
`test_p0_regression.py` / `test_summary_tree.py` **静默失效**——门禁一直绿，没人知道
它们已经断了。这正是本文件要堵的洞。

## 做法

每个脚本一个参数化用例，用子进程跑，断言退出码 0。脚本本身一行不改（它们自带
`check()` 计数与退出码语义），改了反而容易破坏原有断言。

## 安全措施

1. 每个子进程的 `DATABASE_URL` 指向临时库（尊重 settings 的脚本走临时库）；
2. `cwd` 固定为 `backend/`（脚本内部有相对路径假设）；
3. **真实库守卫**：跑前跑后核对 `novel_demo.db` / `novel_dev.db` 的大小与 mtime，
   任何一个变了就让本文件失败。防的是将来有人把某个脚本改成写真实库——
   那种改动在这里会被当场抓住，而不是默默污染数据。
"""
from __future__ import annotations
import tempfile

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
_REPO = _BACKEND.parent
_PY = _BACKEND / "venv" / "Scripts" / "python.exe"
_TIMEOUT = 180
#: 临时库目录。**每个脚本一个独立文件**——否则某个脚本 drop_all/create_all 会串味到下一个。
_TMP_DIR = Path(tempfile.gettempdir()) / "legacy_gate"

#: 脚本可能打开的真实库（相对 backend/）。跑前跑后必须原样。
_REAL_DBS = ("novel_demo.db", "novel_dev.db")

#: 已知有缺陷的老脚本：**仍然跑**（让真实库守卫覆盖到它），但不作为通过条件。
#: `concurrent_assistant_qa.py` 用 20 个线程对**同一个 sqlite 文件**并发写，必然撞
#: `database is locked`——这与库隔离无关（换任何文件库都一样），是脚本自身的设计问题。
#: 用 `xfail(strict=False)`：不阻塞门禁；若有人把它修好，会以 XPASS 显现。
_KNOWN_BROKEN = {
    "concurrent_assistant_qa.py":
        "20 线程并发写同一 sqlite 文件 → database is locked（脚本设计问题，与隔离无关）",
    # 该脚本混用 pytest 风格与 sys.exit 断言，且会触发一次**真实 LLM** 批抽：
    # - `rhythm top1` 在 2 个实体 mention 数平票时依赖不稳定的排序（HEAD 同样失败）；
    # - `events 数 == 2` 假设批抽不新增事件，配了真 key 时每批会多写 0~5 条。
    # 两条都与本套件的隔离无关，保持运行（XPASS 可见）但不阻塞门禁。
    "test_v30_integration.py":
        "rhythm 平票排序 + 'events 数==2' 依赖真实 LLM 是否产出事件（环境相关）",
}


def _discover() -> list[Path]:
    """收集待跑脚本（绝对路径）。

    三组：
      1. `backend/tests/test_*.py` —— 老式 sys.exit 脚本（v3.0 KG 等）
      2. `backend/test_*.py`       —— 更早的一批
      3. `tests/concurrent_*.py`   —— 并发压测脚本。**必须纳入**：它们曾因
         `from app.db import engine` + `drop_all` 且无 DATABASE_URL 覆盖，
         在 backend/ 下直接跑就能清掉真实库（与 2026-10-17 事故同类暴露面）。
    """
    found: list[Path] = []
    found += sorted((_BACKEND / "tests").glob("test_*.py"))
    found += sorted(_BACKEND.glob("test_*.py"))
    found += sorted((_REPO / "tests").glob("concurrent_*.py"))
    return found


def _params() -> list:
    out = []
    for p in _discover():
        rel = p.relative_to(_REPO).as_posix().replace("/", "·")
        reason = _KNOWN_BROKEN.get(p.name)
        if reason:
            out.append(pytest.param(
                p, marks=pytest.mark.xfail(reason=reason, strict=False), id=rel))
        else:
            out.append(pytest.param(p, id=rel))
    return out


SCRIPTS = _params()


def _db_fingerprint() -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for name in _REAL_DBS:
        p = _BACKEND / name
        if p.exists():
            st = p.stat()
            out[name] = (st.st_size, st.st_mtime)
    return out


@pytest.fixture(scope="module")
def real_db_untouched():
    """真实库守卫：本文件跑完后，真实库必须一字节未变。"""
    before = _db_fingerprint()
    yield
    after = _db_fingerprint()
    changed = [k for k in before if before[k] != after.get(k)]
    assert not changed, (
        f"真实库被测试脚本改动了：{changed}。\n"
        f"改动前 {before} / 改动后 {after}\n"
        "门禁绝不允许写真实库——请让脚本改用临时库（DATABASE_URL 或 :memory:）。"
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_legacy_script_passes(script: Path, real_db_untouched):
    """该脚本必须以退出码 0 结束（其内部 check() 计数决定 0/1）。"""
    assert _PY.exists(), f"找不到解释器：{_PY}"
    label = script.relative_to(_REPO).as_posix()
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_file = _TMP_DIR / (script.stem + ".db")
    if db_file.exists():
        db_file.unlink()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    env["PYTHONIOENCODING"] = "utf-8"
    # 脚本按路径执行时 sys.path[0] 是脚本所在目录（backend/tests），不是 backend/。
    # 自带 sys.path 插入的老脚本能 import app，pytest 风格的几个（test_kg_canon_source
    # 等）没有这一步 → ModuleNotFoundError。用 PYTHONPATH 补上，脚本一行不改。
    env["PYTHONPATH"] = str(_BACKEND) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        r = subprocess.run(
            [str(_PY), str(script)], cwd=str(_BACKEND), env=env,
            capture_output=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{label} 超时（>{_TIMEOUT}s）——可能死锁或等待外部服务")

    if r.returncode != 0:
        out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = "\n".join((out[-12:] + err[-12:]))
        pytest.fail(f"{label} 退出码 {r.returncode}\n--- 输出尾部 ---\n{tail}")
