"""独立验证脚手架（QA4，攻击用）。

**不是** pytest 测试（文件名不以 test_ 开头，pytest 不收集）。
单独跑：`python tests/qa4_verify_proposal_write_path.py`

## 为什么自带bootstrap

1. `DATABASE_URL` 必须在 `import app.db` **之前**改掉 —— `app/config.py` 在import
   期就 `settings = Settings()` 并new 出 `app.db.engine`。若晚一步，`engine` 已经
   指向 `novel_dev.db`，而项目里既有的 pytest fixture 会 `drop_all()`，会**真的
   删掉开发库**。本脚本只允许自己造库。
2. `app.db.engine` 是模块级单例，无法事后换绑 —— 所以只能靠 import 前的 env。

## 指纹算法（按列名稳定序列化，**不用 repr(ORM)**）

`repr(ORM对象)` 里带 `<app.models.Generation object at 0x7f...>` 内存地址，
每次 import 都不同，拿它做哈希会得到假阳性。这里改成：

    读表列名 → 按主键排序取行 → 每行 {列名: 规范化值} → json.dumps(sort_keys=True)
    → 整表拼成一个大字符串 → sha256

规范化：datetime → isoformat(None 也统一成 None)、bytes → hex、其它 → str。
这样「只有 Generation.text 变了」会被如实检出，而对象地址变化不会。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import date, datetime
from typing import Any

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BACKEND = os.path.join(_REPO, "backend")
for _p in (_BACKEND,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 关键：先换库，再 import app ───────────────────────────────────────────
QA4_TMPDIR = tempfile.mkdtemp(prefix="qa4_proposals_")
QA4_DB = os.path.join(QA4_TMPDIR, "qa4_verify.db")
os.environ["DATABASE_URL"] = f"sqlite:///{QA4_DB}"
# 顺带保证任何 LLM 端点都不会真的联网
os.environ.setdefault("WRITER_PROVIDER", "local")
os.environ.setdefault("ASSISTANT_PROVIDER", "local")

from sqlalchemy import inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
import app.models as models  # noqa: E402,F401
from app.models import (  # noqa: E402
    AgentRun,
    AppSetting,
    EditProposal,
    Fact,
    Generation,
    KgEdge,
    KgNode,
    Project,
    PromptOverride,
    Version,
)
from app.main import app as fastapi_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 需求指定的 8 张表。业务表= 前5 张 + prompt_overrides；审计表= 后3 张。
BUSINESS_TABLES: tuple[str, ...] = (
    "generations",
    "projects",
    "facts",
    "kg_nodes",
    "kg_edges",
    "prompt_overrides",
)
AUDIT_TABLES: tuple[str, ...] = ("edit_proposals", "agent_runs", "versions")
ALL_TABLES: tuple[str, ...] = BUSINESS_TABLES + AUDIT_TABLES


# ====================================================================
# 指纹
# ====================================================================

def _norm(value: Any) -> Any:
    """把任意列值规范化成 JSON 可序列化且**跨进程稳定**的形式。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    return str(value)


def table_fingerprint(db: Session, table: str) -> dict[str, Any]:
    """单表指纹：{行数, sha256}。按列名排序序列化，主键升序取行。"""
    inspector = inspect(db.bind)
    if table not in inspector.get_table_names():
        return {"rows": -1, "sha256": "TABLE_MISSING"}
    cols = [c["name"] for c in inspector.get_columns(table)]
    pk_cols = list(inspector.get_pk_constraint(table)["constrained_columns"]) or None
    order = ", ".join(pk_cols) if pk_cols else ", ".join(cols)
    raw = db.execute(text(f'SELECT * FROM "{table}" ORDER BY {order}')).fetchall()
    payload = [
        {c: _norm(r[idx]) for idx, c in enumerate(cols)}
        for r in raw
    ]
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return {
        "rows": len(raw),
        "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
        "_cols": cols,
    }


def snapshot_all(db: Session) -> dict[str, dict[str, Any]]:
    """8 张表的全量指纹。返回的 dict 不含 `_cols`（避免误入比较）。"""
    out: dict[str, dict[str, Any]] = {}
    for t in ALL_TABLES:
        fp = table_fingerprint(db, t)
        out[t] = {"rows": fp["rows"], "sha256": fp["sha256"]}
    return out


def diff_snapshots(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """两份快照的差异表（只列出真正变化的表）。"""
    changed: dict[str, Any] = {}
    for t in ALL_TABLES:
        b, a = before.get(t, {}), after.get(t, {})
        if b.get("sha256") != a.get("sha256") or b.get("rows") != a.get("rows"):
            changed[t] = {"before": b, "after": a}
    return changed


def business_unchanged(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> tuple[bool, list[str]]:
    """业务表（6 张，含 prompt_overrides）是否逐字节未变。"""
    changed = [
        t for t in BUSINESS_TABLES
        if before.get(t, {}).get("sha256") != after.get(t, {}).get("sha256")
    ]
    return (not changed), changed


# ====================================================================
# 结果累积
# ====================================================================

class Report:
    """收集每一条断言的通过/失败，最后统一打印。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._sec = "-"

    def section(self, title: str) -> None:
        self._sec = title
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")

    def check(self, name: str, ok: bool, evidence: str = "") -> bool:
        self.rows.append(
            {"section": self._sec, "name": name, "ok": bool(ok), "evidence": evidence}
        )
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if evidence:
            for line in str(evidence).splitlines():
                print(f"         {line}")
        return bool(ok)

    def note(self, text: str) -> None:
        print(f"  ---- {text}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if not r["ok"]]

    def summary(self) -> str:
        total = len(self.rows)
        bad = len(self.failed)
        lines = [
            "=" * 78,
            f"总计{total} 项断言 | 通过 {total - bad} | 失败 {bad}",
            "=" * 78,
        ]
        if bad:
            lines.append("失败明细：")
            for r in self.failed:
                lines.append(f"  - [{r['section']}] {r['name']}")
                if r["evidence"]:
                    lines.append(f"      {r['evidence']}")
        return "\n".join(lines)


# ====================================================================
# 夹具
# ====================================================================

def reset_db() -> None:
    """清空并重建全部表（只动本脚本的临时库）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def make_client() -> TestClient:
    return TestClient(fastapi_app)


def seed_project(db: Session, title: str = "QA4项目", plot_bible: dict | None = None) -> Project:
    proj = Project(
        title=title,
        target_length=5000,
        mode="quick",
        status="created",
        plot_bible=plot_bible if plot_bible is not None else {
            "characters": [{"name": "张三", "role": "主角"}],
            "foreshadowing": ["铜钥匙"],
        },
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def seed_generation(
    db: Session, project_id: int, text: str = "原始正文：铜钥匙藏在门后。", segment_no: int = 1
) -> Generation:
    gen = Generation(
        project_id=project_id,
        segment_no=segment_no,
        chapter_range=f"{segment_no}-{segment_no}",
        text=text,
        summary={"short": f"第 {segment_no} 段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def new_proposal(
    client: TestClient,
    project_id: int,
    gen_id: int,
    after_text: str = "改写后的正文：铜钥匙被张三取出。",
    **extra: Any,
) -> dict:
    payload: dict[str, Any] = {
        "project_id": project_id,
        "type": "text_edit",
        "target_kind": "generation_text",
        "target_id": str(gen_id),
        "after_text": after_text,
        "reason": "QA4 修正描述",
    }
    payload.update(extra)
    resp = client.post("/api/assistant/proposals", json=payload)
    assert resp.status_code == 200, f"创建提案失败 {resp.status_code}: {resp.text}"
    return resp.json()


def approve(client: TestClient, pid: int, actor: str = "qa4") -> dict:
    resp = client.post(
        f"/api/assistant/proposals/{pid}/approve", json={"action": "approve", "actor": actor}
    )
    assert resp.status_code == 200, f"approve 失败 {resp.status_code}: {resp.text}"
    return resp.json()


def try_apply(client: TestClient, pid: int, actor: str = "qa4") -> tuple[int, Any]:
    resp = client.post(
        f"/api/assistant/proposals/{pid}/apply", json={"action": "apply", "actor": actor}
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


def try_restore(client: TestClient, vid: int, actor: str = "qa4", body: dict | None = None) -> tuple[int, Any]:
    payload = {"action": "restore", "actor": actor}
    if body is not None:
        payload = {**payload, **body}
    resp = client.post(f"/api/assistant/versions/{vid}/restore", json=payload)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, resp.text


def get_text(db: Session, gen_id: int) -> str | None:
    """**用裸 SQL** 读generations.text。

    刻意不写 `db.get(Generation, gid).text`：`SessionLocal` 是
    `expire_on_commit=False`（见 app/db.py），同一个 Session 里
    `db.get()` 会直接返回 identity-map 里的**旧对象**，而 HTTP 请求
    是在**另一个** Session 里提交的 —— 于是断言会读到过期的缓存值，
    把「apply 成功」误报成「apply 没写库」（QA4 第一轮就踩了这个坑）。
    裸 SQL 无缓存，是这里唯一可靠的读法。
    """
    row = db.execute(
        text("SELECT text FROM generations WHERE id = :i"), {"i": gen_id}
    ).fetchone()
    return None if row is None else row[0]


def get_proposal_row(db: Session, pid: int):
    """同样绕开 identity-map 地读一条EditProposal（重新查库）。"""
    return db.execute(
        text(
            "SELECT id, status, version_id, approved_by, applied_at, project_id,"
            " target_kind, target_id, type FROM edit_proposals WHERE id = :i"
        ),
        {"i": pid},
    ).fetchone()


def proposal_status(db: Session, pid: int) -> str | None:
    row = get_proposal_row(db, pid)
    return None if row is None else row[1]


def count(db: Session, model: Any) -> int:
    return len(db.scalars(select(model)).all())