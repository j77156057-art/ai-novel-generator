# -*- coding: utf-8 -*-
"""`/healthz` 的新旧代码判据。"""
from fastapi.testclient import TestClient

from app import health as health_mod
from app.main import app


def test_latest_source_mtime_skips_excluded_and_other_suffixes(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    (app_dir / "pkg" / "venv").mkdir(parents=True)
    (app_dir / "pkg" / "__pycache__").mkdir()
    stale = app_dir / "stale.py"
    fresh = app_dir / "pkg" / "fresh.py"
    stale.write_text("x = 1\n", encoding="utf-8")
    fresh.write_text("y = 2\n", encoding="utf-8")
    (app_dir / "pkg" / "venv" / "ignored.py").write_text("z = 3\n", encoding="utf-8")
    (app_dir / "pkg" / "__pycache__" / "ignored.py").write_text("w = 4\n", encoding="utf-8")
    (app_dir / "notes.txt").write_text("not source\n", encoding="utf-8")

    latest = fresh.stat().st_mtime + 10
    import os
    os.utime(fresh, (latest, latest))

    monkeypatch.setattr(health_mod, "SOURCE_ROOTS", (app_dir,))
    monkeypatch.setattr(health_mod, "EXTRA_FILES", ())

    mtime, path = health_mod.latest_source_mtime()
    assert mtime == latest
    assert path == "pkg/fresh.py"


def test_snapshot_fresh_flag(monkeypatch):
    import time

    now = time.time()
    monkeypatch.setattr(health_mod, "latest_source_mtime",
                        lambda: (now - 10.0, "app/a.py"))
    monkeypatch.setattr(health_mod, "git_info", lambda: ("abc1234", True))

    fresh = health_mod.snapshot(started_at=now)
    assert fresh["fresh"] is True and fresh["ok"] is True
    assert fresh["git_sha"] == "abc1234" and fresh["dirty"] is True

    stale = health_mod.snapshot(started_at=now - 20.0)
    assert stale["fresh"] is False and stale["ok"] is False

    same = health_mod.snapshot(started_at=now - 10.0)
    assert same["fresh"] is True


def test_healthz_endpoint_shape(monkeypatch):
    payload = {
        "ok": True, "fresh": True, "pid": 4321, "git_sha": "abc1234", "dirty": False,
        "started_at": "2026-11-11T13:00:00.000+08:00", "started_at_epoch": 200.0,
        "build_time": "2026-11-11T12:00:00.000+08:00", "build_time_epoch": 100.0,
        "build_time_file": "app/health.py",
        "scope": {"roots": [], "excluded_dirs": []},
    }
    monkeypatch.setattr(health_mod, "snapshot", lambda: payload)

    resp = TestClient(app).get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    for key in ("git_sha", "dirty", "build_time", "started_at", "pid", "fresh"):
        assert key in body
    assert body["started_at_epoch"] >= body["build_time_epoch"]
