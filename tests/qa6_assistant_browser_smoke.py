"""QA6 · 助手面板浏览器烟雾测试的「编排 driver」。

负责：起临时库 → 播种 → 拉起后端 uvicorn(:8010) 与前端 vite dev(:5183) →
调 tests/qa6_assistant_browser_probe.mjs 跑真 Chromium 断言 → 拆干净。

全程不碰 backend/novel_dev.db。
运行：python tests/qa6_assistant_browser_smoke.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"

TMP_DB = Path(tempfile.gettempdir()) / f"qa6_browser_{int(time.time())}.db"
DB_URL = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["DATABASE_URL"] = DB_URL
os.environ["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(BACKEND))

API_PORT = 8010
WEB_PORT = 5183


def seed() -> int:
    from app.db import SessionLocal, init_db
    from app.models import Fact, Generation, Project

    init_db()
    db = SessionLocal()
    try:
        proj = Project(
            title="QA6 浏览器烟雾测试项目",
            genre="悬疑",
            style="知乎盐选",
            target_length=5000,
            status="created",
            created_at=datetime.now(timezone.utc),
            plot_bible={"characters": [{"name": "陈默", "role": "主角"}]},
        )
        db.add(proj)
        db.flush()
        for i, text in enumerate(
            [
                "第一章。陈默推开旧宅的门，灰尘在光柱里浮动。他记得十年前这里也是这样。",
                "第二章。雨下了一夜。陈默发现抽屉里的照片被人动过，边缘多了一道折痕。",
                "第三章。他站在窗前，久久没有说话。",
            ],
            start=1,
        ):
            db.add(Generation(project_id=proj.id, segment_no=i,
                              chapter_range=f"第{i}章", text=text,
                              summary={"summary": f"第{i}段概要"}))
        # 8 条 low 置信事实 → 必然命中 low_confidence_facts_overflow，
        # 让「诊断真的出 issue」成为确定性条件，而不是碰运气。
        for i in range(8):
            db.add(Fact(project_id=proj.id, type="character", key=f"待裁决名字{i}",
                        props={"mentions": 1}, aliases=[f"别名{i}"],
                        source="generated", confidence="low", first_segment=1))
        db.commit()
        pid = int(proj.id)
        print(f"  seeded project_id={pid}（DB={TMP_DB}）")
        return pid
    finally:
        db.close()


def wait_http(url: str, timeout: float = 90.0) -> bool:
    import httpx

    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=3.0).status_code < 500:
                return True
        except Exception:
            time.sleep(0.6)
    return False


def _drain(proc: subprocess.Popen, out: Path, tag: str) -> None:
    """把子进程 stdout 往文件里搬。

    绝不能在主线程里 `proc.stdout.read()` —— 读不到 EOF 就永久阻塞，
    而 dev server 永远不 EOF（实测这里死锁过一次）。
    """
    def _run() -> None:
        try:
            with open(out, "w", encoding="utf-8", errors="replace") as f:
                for line in proc.stdout:  # type: ignore[union-attr]
                    f.write(line)
        except Exception as exc:  # noqa: BLE001
            print(f"  ({tag} log drain stopped: {exc})")

    import threading

    threading.Thread(target=_run, daemon=True).start()


def main() -> int:
    pid = seed()
    env = dict(os.environ)
    env["DATABASE_URL"] = DB_URL

    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(API_PORT)],
        cwd=str(BACKEND), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    LOG_API = Path(tempfile.gettempdir()) / "qa6_api.log"
    LOG_WEB = Path(tempfile.gettempdir()) / "qa6_vite.log"
    web = None
    try:
        _drain(api, LOG_API, "api")
        if not wait_http(f"http://127.0.0.1:{API_PORT}/health"):
            print(f"FAIL 后端 90s 未 ready（日志：{LOG_API}）")
            print(LOG_API.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 2
        print(f"  后端 ready :{API_PORT}")

        env_web = dict(os.environ)
        env_web["API_TARGET"] = f"http://127.0.0.1:{API_PORT}"
        # --host 127.0.0.1 是必须的：默认只监听 [::1]，而本机沙箱里 IPv4/IPv6
        # 的 localhost 并不互通，127.0.0.1 会连不上（实测踩过）。
        cmd = ["npm", "run", "dev", "--", "--host", "127.0.0.1",
               "--port", str(WEB_PORT), "--strictPort"]
        shell = sys.platform == "win32"
        web = subprocess.Popen(
            " ".join(cmd) if shell else cmd,
            cwd=str(FRONTEND), env=env_web, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            shell=shell,
        )
        _drain(web, LOG_WEB, "vite")
        if not wait_http(f"http://127.0.0.1:{WEB_PORT}/", timeout=120):
            print(f"FAIL 前端 dev server 120s 未 ready（日志：{LOG_WEB}）")
            print(LOG_WEB.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 2
        print(f"  前端 ready :{WEB_PORT} (API_TARGET={env_web['API_TARGET']})")

        probe = ROOT / "tests" / "qa6_assistant_browser_probe.mjs"
        node_env = dict(os.environ)
        node_env["E2E_BASE"] = f"http://127.0.0.1:{WEB_PORT}"
        node_env["E2E_PID"] = str(pid)
        r = subprocess.run(["node", str(probe)], env=node_env, cwd=str(ROOT))
        return int(r.returncode)
    finally:
        for p in (web, api):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        try:
            TMP_DB.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
