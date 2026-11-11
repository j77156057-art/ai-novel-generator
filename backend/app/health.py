# -*- coding: utf-8 -*-
"""`/healthz`：判断「端口上跑的进程是不是当前代码」。

口径（与报告 §O 一致）：

- ``started_at``：进程 import 本模块的时刻（uvicorn 启动即加载 ``app.main``）。
- ``build_time``：**请求到达时**实时扫描的源码最大 mtime。之所以不固化在启动
  那一刻，是因为要能发现「服务起来之后又改了代码」——固化值永远等于启动时的
  旧盘面，就判不出陈旧。
- 扫描范围：``backend/app/**/*.py``、``backend/config/**/*.{yaml,yml,json}``、
  ``backend/.env``。运行时会加载的代码与配置只在这些位置。
- 排除：``__pycache__``、``venv``、``node_modules``、``backups``、``.tmp_verify``、
  ``.pytest_cache``、``data``、``migrations``、``worktrees``。不排这些，随便一次
  测试/备份/迁移写入都会把 ``build_time`` 顶到「永远比进程新」。
- ``fresh = started_at >= build_time``：前者不早于后者，进程里才是盘上的代码。

已知边界：mtime 是**本机文件系统**的元数据。多机部署、镜像构建（容器内文件
mtime 是构建时间而非部署时间）、或 CI 用 ``git archive`` 之类不带原始 mtime 的
方式产出时，这个判据不可用；那种场景应改成构建产物里写 build id，由进程读取
并在 ``/healthz`` 返回，再与部署一致版本比对。
"""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent
REPO_ROOT = BACKEND_DIR.parent

#: 进程启动时间（import 时刻，uvicorn 启动即加载本模块）。
STARTED_AT: float = time.time()

SOURCE_ROOTS = (APP_DIR, BACKEND_DIR / "config")
SOURCE_SUFFIXES = frozenset({".py", ".yaml", ".yml", ".json"})
EXTRA_FILES = (BACKEND_DIR / ".env",)
EXCLUDED_DIRS = frozenset({
    "__pycache__", "venv", "node_modules", "backups", ".tmp_verify",
    ".pytest_cache", "data", "migrations", "worktrees",
})


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def _rel(path: Path, root: Path | None = None) -> str:
    bases = (BACKEND_DIR,) if root is None else (BACKEND_DIR, root)
    for base in bases:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def latest_source_mtime() -> tuple[float, str | None]:
    """扫描源码/配置，返回 ``(最大 mtime 的 epoch, 对应文件相对路径)``。"""
    latest, latest_file = 0.0, None
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix not in SOURCE_SUFFIXES:
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime > latest:
                    latest, latest_file = mtime, _rel(path, root)
    for path in EXTRA_FILES:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > latest:
            latest, latest_file = mtime, _rel(path)
    return latest, latest_file


def git_info() -> tuple[str | None, bool | None]:
    """返回 ``(HEAD 短 sha, 工作区是否有未提交改动)``；git 不可用时为 ``(None, None)``。"""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if sha.returncode != 0:
            return None, None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
        dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
        return (sha.stdout.strip() or None), dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def snapshot(started_at: float | None = None) -> dict:
    """当前健康快照；``started_at`` 仅供测试注入。"""
    started = STARTED_AT if started_at is None else started_at
    build_time, build_file = latest_source_mtime()
    sha, dirty = git_info()
    return {
        "ok": build_time <= started,
        "fresh": build_time <= started,
        "pid": os.getpid(),
        "git_sha": sha,
        "dirty": dirty,
        "started_at": _iso(started),
        "started_at_epoch": started,
        "build_time": _iso(build_time) if build_time else None,
        "build_time_epoch": build_time,
        "build_time_file": build_file,
        "scope": {
            "roots": ["backend/app/**/*.py", "backend/config/**/*.{yaml,yml,json}",
                      "backend/.env"],
            "excluded_dirs": sorted(EXCLUDED_DIRS),
        },
    }
