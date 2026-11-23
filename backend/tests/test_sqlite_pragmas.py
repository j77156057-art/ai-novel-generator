# -*- coding: utf-8 -*-
"""SQLite 并发基线：主 engine 的每个连接都要带 WAL + busy_timeout。

背景：请求线程、SSE 生成线程、素材 worker、KG worker 会同时写同一个库。
默认 rollback journal 下读会挡写，busy_timeout 又是 0，偶发并发写直接
`database is locked`。这里把驱动层设置钉住，防止以后换 engine 写法时丢掉。
"""
from __future__ import annotations

import pytest

from app.db import IS_SQLITE, engine


def test_sqlite_connection_uses_wal_and_busy_timeout():
    if not IS_SQLITE:
        pytest.skip("当前不是 sqlite 库")
    url = str(engine.url)
    with engine.connect() as conn:
        mode = str(conn.exec_driver_sql("PRAGMA journal_mode").scalar() or "").lower()
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    if ":memory:" in url:
        # 内存库没有 WAL 文件，journal_mode 会回 memory；只校验 busy_timeout。
        assert mode in ("memory", "wal")
    else:
        assert mode == "wal", f"文件库应使用 WAL，实际 {mode!r}（{url}）"
    assert int(busy) == 5000
