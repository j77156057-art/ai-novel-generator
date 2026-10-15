"""护栏测试：跑测试**绝不允许**修改真实开发库（第 8 步交付 3）。

背景：用户的三本网文就是被「测试清库」毁掉的 —— 测试进程共享 `app.db` 的
全局 engine，而该 engine 由 `settings.database_url`（默认相对 CWD 的
`sqlite:///./novel_dev.db`）建立，一次 `drop_all()` 就把真实库清空。

`tests/conftest.py` 已把测试库强制隔离到独立临时文件。本文件是**验证那层隔离
真的生效**的护栏：

  (a) 本会话使用的测试库 URL 必须指向临时文件，且**不是**任何一个真实库；
  (b) 会话开始前对真实库打的 mtime+size+sha256 快照，到本测试运行时**逐项不变**。
"""

from __future__ import annotations

import hashlib
import os

# 复用 conftest 的常量与函数（pytest 会把 conftest 所在目录放进导入路径）。
from conftest import REAL_DB_FILES, _fingerprint  # noqa: E402


def test_test_db_is_isolated_to_temp_file(test_database_url):
    """本会话测试库必须落在临时目录，且与真实库文件无关。"""
    assert test_database_url.startswith("sqlite")
    # 不是内存库（内存库在并发/多连接下不可靠；要求独立**文件**库）
    assert ":memory:" not in test_database_url

    # 归一化成绝对路径，与真实库清单比对
    rest = test_database_url[len("sqlite:"):].lstrip("/")
    used = os.path.abspath(rest)
    for real in REAL_DB_FILES:
        assert used != os.path.abspath(real), (
            f"测试库落到了真实库 {real!r}！隔离失效。"
        )
    # 真实库文件名不应出现在测试库路径里（额外的一道粗筛）
    assert "novel_dev.db" not in test_database_url
    assert "novel_demo.db" not in test_database_url


def test_real_dev_db_untouched_by_test_session(real_db_snapshot):
    """真实库文件指纹（mtime+size+sha256）自会话开始起**没有被改动**。"""
    checked = 0
    for path, before in real_db_snapshot.items():
        if before is None:
            # 真实库不存在（干净检出）——没得比，跳过
            continue
        after = _fingerprint(path)
        assert after is not None, f"真实库 {path} 在跑测期间消失了！"
        assert after["sha256"] == before["sha256"], (
            f"真实库 {path} 的内容在跑测期间被改动（sha256 变了）——测试隔离失效！\n"
            f"  before sha256={before['sha256']}\n  after  sha256={after['sha256']}"
        )
        assert after["size"] == before["size"], f"真实库 {path} 大小变了"
        assert after["mtime"] == before["mtime"], f"真实库 {path} mtime 变了"
        checked += 1
    # 至少校验到一个真实库（否则这护栏形同虚设）
    assert checked >= 1, "未找到任何可校验的真实库；护栏未生效"


def test_fingerprint_detects_change(tmp_path):
    """自检：指纹函数对内容改动敏感（否则上面的断言可能假通过）。"""
    p = tmp_path / "x.db"
    p.write_bytes(b"hello")
    fp1 = _fingerprint(str(p))
    p.write_bytes(b"hello world")
    fp2 = _fingerprint(str(p))
    assert fp1 is not None and fp2 is not None
    assert fp1["sha256"] != fp2["sha256"]
    assert fp1["size"] != fp2["size"]


def test_hashing_is_stable():
    """sha256 口径自检（防止 fingerprint 用了不稳定的序列化）。"""
    assert hashlib.sha256(b"abc").hexdigest() == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
