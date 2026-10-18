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

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
_PY = _BACKEND / "venv" / "Scripts" / "python.exe"
_TIMEOUT = 180
#: 临时库目录。**每个脚本一个独立文件**——否则某个脚本 drop_all/create_all 会串味到下一个。
_TMP_DIR = Path("D:/Temp/legacy_gate")

#: 脚本可能打开的真实库（相对 backend/）。跑前跑后必须原样。
_REAL_DBS = ("novel_demo.db", "novel_dev.db")


def _discover() -> list[str]:
    """收集待跑脚本（相对 backend/ 的 posix 路径），顺序稳定。"""
    found: list[str] = []
    found += [p.relative_to(_BACKEND).as_posix()
              for p in sorted((_BACKEND / "tests").glob("test_*.py"))]
    found += [p.relative_to(_BACKEND).as_posix()
              for p in sorted(_BACKEND.glob("test_*.py"))]
    return found


SCRIPTS = _discover()


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


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda s: s.replace("/", "·"))
def test_legacy_script_passes(script: str, real_db_untouched):
    """该脚本必须以退出码 0 结束（其内部 check() 计数决定 0/1）。"""
    assert _PY.exists(), f"找不到解释器：{_PY}"
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    db_file = _TMP_DIR / (script.replace("/", "_").replace(".py", "") + ".db")
    if db_file.exists():
        db_file.unlink()
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [str(_PY), script], cwd=str(_BACKEND), env=env,
            capture_output=True, timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{script} 超时（>{_TIMEOUT}s）——可能死锁或等待外部服务")

    if r.returncode != 0:
        out = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        err = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        tail = "\n".join((out[-12:] + err[-12:]))
        pytest.fail(f"{script} 退出码 {r.returncode}\n--- 输出尾部 ---\n{tail}")
