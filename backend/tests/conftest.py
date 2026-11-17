"""`backend/tests/` 的收集配置。

两件事：

1. **库守卫（硬闸，事前拒绝）**：本目录下的用例会直接使用 `app.db` 的全局 engine。
   该 engine 由 `settings.database_url` 决定，默认 `sqlite:///./novel_dev.db`（相对
   CWD 解析）。若在 `backend/` 或仓库根裸跑，就会连到真实开发库 `backend/novel_dev.db`
   —— 历史上正是这样把语料清空的。所以在**导入期**检查实际生效的 DATABASE_URL，
   只要不是临时目录下的 sqlite，就打印当前值并让整个 pytest 拒绝启动。
   覆盖两种入口：裸跑 `pytest backend/tests`；从仓库根跑但 `tests/conftest.py`
   （它负责改写 DATABASE_URL）没有先执行。

2. **收集排除**：本目录里有三类文件不能进 pytest 收集——
   模块级 `sys.exit()` 脚本（导入即 `INTERNALERROR`，整个门禁崩在收集阶段）、
   可以导入但 0 条用例的空壳脚本、以及 `stress/` 下同样模块级执行的压力脚本。
   前两类由 `tests/test_legacy_script_suite.py` 用子进程逐个执行（断言退出码 0）；
   `stress/` 那个在 legacy 里是显式 `skip`（原因见下），不进 pytest 收集。
   新增同类文件时记得与 legacy 的排除名单同步。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

#: 真实库文件名：无论落在哪个目录，一律拒绝。
_REAL_DB_NAMES = {"novel_dev.db", "novel_demo.db", "live_dev.db"}


def _allowed_roots() -> list[Path]:
    """允许写库的目录：系统临时目录 + 显式指定的测试库目录。"""
    roots = [Path(tempfile.gettempdir())]
    extra = os.environ.get("AI_NOVEL_TEST_DB_DIR", "").strip()
    if extra:
        roots.append(Path(extra))
    return [p.resolve() for p in roots]


def _effective_urls() -> list[tuple[str, str]]:
    """实际生效的库地址：(来源, URL)。没配就是 settings 里的默认值。"""
    out: list[tuple[str, str]] = [("DATABASE_URL", os.environ.get("DATABASE_URL", "<未设置>"))]
    try:  # 只读取 settings，不建 engine
        from app.config import settings

        out.append(("settings.database_url", str(settings.database_url)))
    except Exception as exc:  # noqa: BLE001 — 连配置都读不出来时要让人看到原因
        out.append(("settings.database_url", f"<读取失败: {exc}>"))
    return out


def _reject_reason(source: str, url: str) -> str | None:
    """返回拒绝原因；None 表示这条路安全。"""
    raw = url.strip()
    if raw in ("", "<未设置>"):
        return f"{source} 未设置，会回落到默认的 './novel_dev.db'（相对 CWD）"
    if raw.startswith("sqlite:///:memory:") or raw in (":memory:", "sqlite://"):
        return None  # 内存库，最安全
    if not raw.startswith("sqlite"):
        return f"{source}={raw} 不是临时 sqlite，可能是真实库"
    path_part = raw.split("sqlite:///", 1)[-1]
    if not path_part or path_part == ":memory:":
        return None
    candidate = Path(path_part)
    if not candidate.is_absolute():
        return f"{source}={raw} 是相对路径，按 CWD({Path.cwd()}) 解析后可能落到仓库里的真实库"
    resolved = candidate.resolve()
    if resolved.name in _REAL_DB_NAMES:
        return f"{source}={raw} 指向真实库 {resolved}"
    for root in _allowed_roots():
        if root == resolved.parent or root in resolved.parents:
            return None
    return f"{source}={raw} 不在临时目录 {[str(r) for r in _allowed_roots()]} 下"


def _guard_database() -> None:
    problems = [
        reason
        for source, url in _effective_urls()
        if (reason := _reject_reason(source, url)) is not None
    ]
    if not problems:
        return
    detail = "\n".join(f"  - {p}" for p in problems)
    values = "\n".join(f"  {s} = {u}" for s, u in _effective_urls())
    pytest.exit(
        "backend/tests 的库守卫拒绝启动：当前配置会连到真实开发库。\n"
        f"当前值：\n{values}\nCWD = {Path.cwd()}\n原因：\n{detail}\n"
        "请显式把 DATABASE_URL 指到临时目录（例如 "
        "sqlite:///<临时目录>/xx.db），或先经 tests/conftest.py 的隔离入口跑。",
        returncode=2,
    )


_guard_database()


#: 模块级 sys.exit() 脚本：pytest 收进来会 INTERNALERROR。
_SYS_EXIT_SCRIPTS = [
    "test_chapter_identity.py",
    "test_entity_resolve.py",
    "test_kg_book_partition.py",
    "test_kg_cascade_protocol.py",
    "test_kg_name_resolution_kind.py",
    "test_kg_v3_frontend_api.py",
    "test_knowledge_consumption.py",
    "test_name_expand.py",
    "test_name_normalize_probe.py",
    "test_sample_chars_floor.py",
    "test_timeline.py",
    "test_v30_extractor.py",
    "test_v30_integration.py",
    "test_v30_judge.py",
    "test_v30_retrieval.py",
    "test_v30_schema.py",
]

#: 可导入但 0 条用例的空壳脚本：收进来只会白跑一次 import。
_EMPTY_SHELLS = [
    "test_async_chat.py",
    "test_batch_worker_observability.py",
    "test_era_dimension.py",
    "test_finale_clean.py",
    "test_narrative_extract.py",
    "test_style_exemplar.py",
    "test_style_warning.py",
    "test_style_warning_persistence.py",
]

collect_ignore = _SYS_EXIT_SCRIPTS + _EMPTY_SHELLS + [
    # stress/ 下的压力脚本：同样是「模块级执行 + sys.exit」，跑 100/1000/3000 三级。
    # 它 100 段就先红：写侧文本归属门把脚本的合成正文判成 text_mismatch/unbound，
    # 实体不落库（`100段 entity 有值` 失败）。修好要按绑定契约重做合成语料，不是
    # 加一列的修补；所以显式排除，并在 legacy 里以 skip 显式记录（不静默漏跑）。
    "stress/test_v30_stress.py",
]
