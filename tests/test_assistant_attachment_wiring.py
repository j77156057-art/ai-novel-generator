"""附件入口接线（批 10e）：上传/投放卡/规划器/复核的端到端口径。

主来源：freeze/main 的 `tests/test_assistant_clean_ingest.py`。剔除三类与本项目
main 刻意分歧或 feat 尚未实现的断言：
1. 「直传关闭」——feat 已按用户拍板恢复直传（`e5eaadf`）；
2. `clean_report.readback` / 按 novel_id 复核——feat 的入库报告没有
   `chunks_fingerprint`，`check_ingest_clean` 明确拒绝而不是给假结论；
3. 广告模板 `strip_repeated_ads(templates=...)`——feat 该函数尚无模板参数。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.models import RawNovel, Project
from app.agents import attachment_chat
from app.agents import attachments as store
from app.routers import assistant_attachments as ar


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _client(router, db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_staging_scope_and_ingest_only_by_card(db, monkeypatch, tmp_path):
    """上传只暂存；**点卡确认前不许写库**；重试幂等。"""
    monkeypatch.setattr(store, "ROOT", tmp_path)
    a, b = Project(title="A"), Project(title="B")
    db.add_all([a, b])
    db.commit()
    with _client(ar.router, db) as client:
        assert client.post(
            "/api/assistant/attachments",
            data={"project_id": a.id},
            files={"file": ("../x.txt", b"hello")},
        ).status_code == 422
        assert client.post(
            "/api/assistant/attachments",
            data={"project_id": a.id},
            files={"file": ("x.exe", b"hello")},
        ).status_code == 415
        up = client.post(
            "/api/assistant/attachments",
            data={"project_id": a.id},
            files={"file": ("x.txt", "第一章 起\n风来了。".encode())},
        )
        assert up.status_code == 200, up.text
        aid = up.json()["attachment_id"]
        assert {"filename", "size", "encoding", "status", "preview"} <= set(up.json())
        # 清洗预览是只读端点：展开时才看，不写库
        pv = client.get(
            f"/api/assistant/attachments/{aid}/preview", params={"project_id": a.id}
        )
        assert pv.status_code == 200, pv.text
        assert pv.json()["attachment_id"] == aid and pv.json()["untrusted"] is True
        # 跨项目不可见
        assert client.get(
            f"/api/assistant/attachments/{aid}", params={"project_id": b.id}
        ).status_code == 404
        card = store.propose_ingest(
            db=db, project_id=a.id, attachment_id=aid, instruction="把附件投放入库"
        )
        assert card["endpoint"] == "/api/assistant/attachments/ingest"
        assert not list(db.scalars(select(RawNovel))), "提卡阶段不许写库"
        out = client.post(card["endpoint"], json=card["body"])
        assert out.status_code == 200, out.text
        body = out.json()
        assert body["novel_id"] and body["check_status"] in (
            "sample_reviewed",
            "review_required",
        )
        meta = client.get(
            f"/api/assistant/attachments/{aid}", params={"project_id": a.id}
        ).json()
        assert meta["status"] == "ingested"
        assert meta["novel_id"] == body["novel_id"]
        # 重复点确认不会重复导入
        again = client.post(card["endpoint"], json=card["body"])
        assert again.status_code == 200, again.text
        assert again.json()["novel_id"] == body["novel_id"]
    assert len(list(db.scalars(select(RawNovel)))) == 1


def test_planner_sees_metadata_not_preview_text(db, monkeypatch, tmp_path):
    """规划器只拿公开元数据；`preview`（清洗前原文）不得进提示词。"""
    monkeypatch.setattr(store, "ROOT", tmp_path)
    project = Project(title="planner")
    db.add(project)
    db.commit()
    meta = store.stage(
        db=db, project_id=project.id, filename="x.txt", data="风来了。".encode()
    )
    seen: list[tuple[str, str]] = []

    class Provider:
        model = "test"

        def chat(self, system, user):
            seen.append((system, user))
            return (
                '{"tool_calls":[{"name":"read_attachment","arguments":'
                '{"attachment_id":"' + meta["attachment_id"] + '","limit":100}}]}'
            )

    monkeypatch.setattr(attachment_chat, "get_planner_provider", lambda: Provider())
    plan = attachment_chat.run_attachment_tools(
        db=db, project_id=project.id, query="读附件",
        attachment_ids=[meta["attachment_id"]],
    )
    assert plan["planner_model_participated"] is True
    assert plan["tool_results"]["read_attachment"]["text"] == "风来了。"
    assert '"parameters"' in seen[0][1] and "不可信" in seen[0][0]
    # 规划器看到元数据，但看不到未请求的全文 / preview 字段
    assert "风来了。" not in seen[0][1]
    assert '"preview":' not in seen[0][1]
    assert all("preview" not in item for item in plan["metadata"])


def test_malformed_planner_falls_back_to_deterministic_read(db, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    project = Project(title="scope")
    db.add(project)
    db.commit()
    meta = store.stage(
        db=db, project_id=project.id, filename="x.txt", data=b"hello"
    )

    class Bad:
        def chat(self, *args):
            return '{"tool_calls":[null]}'

    monkeypatch.setattr(attachment_chat, "get_planner_provider", lambda: Bad())
    plan = attachment_chat.run_attachment_tools(
        db=db, project_id=project.id, query="读附件",
        attachment_ids=[meta["attachment_id"]],
    )
    assert plan["planner_model_participated"] is False
    assert plan["tool_results"]["read_attachment"]["text"] == "hello"


def test_ingest_tool_only_proposes_card(db, monkeypatch, tmp_path):
    """registry 的投放工具只产出待确认卡，不写库。"""
    from app.agents.registry import get_tool_registry, invoke_tool

    monkeypatch.setattr(store, "ROOT", tmp_path)
    project = Project(title="card")
    db.add(project)
    db.commit()
    meta = store.stage(
        db=db, project_id=project.id, filename="x.txt", data=b"hello"
    )
    spec = get_tool_registry().resolve("ingest_attachment")
    assert spec.read_only is True
    result = invoke_tool(
        "ingest_attachment", db=db, project_id=project.id,
        attachment_id=meta["attachment_id"], instruction="把附件投放入库",
    )
    assert result["proposed"] is True
    assert len(result["pending_actions"]) == 1
    assert not list(db.scalars(select(RawNovel)))
    # 没有明确投放意图 → 不给卡
    quiet = invoke_tool(
        "ingest_attachment", db=db, project_id=project.id,
        attachment_id=meta["attachment_id"], instruction="读一下这个附件",
    )
    assert quiet["proposed"] is False and quiet["pending_actions"] == []


def test_high_confidence_residual_blocks(monkeypatch):
    from app.agents import ingest_clean as agent

    prepared = agent.preview_ingest_clean(text="正文。")
    prepared["chapters"][0]["body"] += "【无限大冒险全文阅读】"
    monkeypatch.setattr(
        agent, "review_samples",
        lambda sample: {"status": "sample_reviewed", "model_participated": True},
    )
    assert agent.check_ingest_clean(prepared=prepared)["status"] == "blocked"


def test_provider_failure_never_passes(monkeypatch):
    from app.agents.ingest_clean import review_samples
    from app.routers import assistant_shared

    class Bad:
        def chat(self, *args):
            raise RuntimeError("network unavailable")

    monkeypatch.setattr(assistant_shared, "_ensure_assistant_provider", lambda: Bad())
    result = review_samples([{"text": "正文"}])
    assert result["model_participated"] is False
    assert result["status"] == "review_required"
