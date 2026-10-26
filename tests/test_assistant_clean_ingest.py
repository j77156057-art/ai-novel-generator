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
from app.models import RawNovel, NovelChunk, Project
from app.routers import novels
from app.agents.registry import get_tool_registry

@pytest.fixture
def db():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    e.dispose()

def test_direct_upload_is_closed_and_points_to_the_assistant(db):
    """直传文本已关闭：它绕过整条流程（清洗、抽检、蒸馏、审查）。

    关掉不是「删了」，而是**明确报错并指向助理** ——
    静默 404 会被读成「上传功能坏了」。
    """
    app = FastAPI()
    app.include_router(novels.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        r = c.post("/api/novels/upload", files={"file": ("book.txt", "正文".encode(), "text/plain")})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["status"] == "text_submission_moved_to_assistant"
    assert detail["assistant_endpoint"] == "/api/assistant/attachments"
    # 关闭直传**不能**留下入库副作用
    assert not list(db.scalars(select(RawNovel)))


def test_cleaning_and_agent_review_still_run_on_the_assistant_path(db, monkeypatch, tmp_path):
    """清洗 + agent 抽样复查这套能力没丢：它现在跑在助理那条路上。"""
    import app.agents.ingest_clean as agent
    from app.routers import assistant_attachments as ar
    from app.agents import attachments as store
    monkeypatch.setattr(store, "ROOT", tmp_path)
    calls = []
    def review(samples):
        calls.append(samples)
        return {"model_participated": False, "status": "review_required", "reason": "provider_unavailable"}
    monkeypatch.setattr(agent, "review_samples", review)
    p = Project(title="清洗测试")
    db.add(p)
    db.commit()
    app = FastAPI()
    app.include_router(ar.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        up = c.post("/api/assistant/attachments", data={"project_id": p.id},
                    files={"file": ("book.txt", "第一章 风起\n风来。一秒记住【abc.info】，为您提供精彩小说阅读。\n风又来。".encode(), "text/plain")})
        assert up.status_code == 200, up.text
        aid = up.json()["attachment_id"]
        card = store.propose_ingest(db=db, project_id=p.id, attachment_id=aid, instruction="把附件投放入库")
        out = c.post(card["endpoint"], json=card["body"])
    assert out.status_code == 200, out.text
    assert calls, "助理投放也必须走 agent 抽样复查"
    body = out.json()
    assert body["check_status"] == "review_required"
    report = body["clean_report"]
    assert report["agent_check"]["model_participated"] is False
    assert report["readback"]["matches"] is True
    novel = db.get(RawNovel, body["novel_id"])
    assert "一秒记住" in novel.source_text
    chunks = db.scalars(select(NovelChunk).where(NovelChunk.novel_id == novel.id)).all()
    assert chunks and all("一秒记住" not in ch.text for ch in chunks)
    assert get_tool_registry().resolve("check_ingest_clean").read_only


def test_attachment_scope_and_pending_ingest(db, monkeypatch, tmp_path):
    from app.routers import assistant_attachments as ar
    from app.agents import attachments as store
    from app.routers.assistant import AssistantQARequest
    monkeypatch.setattr(store, "ROOT", tmp_path)
    a, b = Project(title="A"), Project(title="B")
    db.add_all([a, b]); db.commit()
    app = FastAPI(); app.include_router(ar.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        bad = c.post("/api/assistant/attachments", data={"project_id": a.id}, files={"file": ("../x.txt", b"hello")})
        assert bad.status_code == 422
        bad = c.post("/api/assistant/attachments", data={"project_id": a.id}, files={"file": ("x.exe", b"hello")})
        assert bad.status_code == 415
        r = c.post("/api/assistant/attachments", data={"project_id": a.id}, files={"file": ("x.txt", "风来。一秒记住【abc.info】，为您提供精彩小说阅读。".encode())})
        assert r.status_code == 200, r.text
        m = r.json(); aid = m["attachment_id"]
        assert set(("filename", "size", "encoding", "status", "preview")) <= set(m)
        assert c.get(f"/api/assistant/attachments/{aid}", params={"project_id": b.id}).status_code == 404
        assert c.get("/api/assistant/attachments/unknown", params={"project_id": a.id}).status_code == 404
        assert c.get(f"/api/assistant/attachments/{aid}", params={"project_id": a.id}).json()["status"] == "staged"
        card = store.propose_ingest(db=db, project_id=a.id, attachment_id=aid, instruction="把附件投放入库")
        assert card["endpoint"] == "/api/assistant/attachments/ingest"
        assert not list(db.scalars(select(RawNovel)))
        result = c.post(card["endpoint"], json=card["body"])
        assert result.status_code == 200, result.text
        assert c.get(f"/api/assistant/attachments/{aid}", params={"project_id": a.id}).json()["novel_id"] == result.json()["novel_id"]
        # Retry is idempotent, never duplicates an import.
        assert c.post(card["endpoint"], json=card["body"]).json()["novel_id"] == result.json()["novel_id"]
    assert AssistantQARequest(project_id=a.id, query="检查附件", attachment_ids=[aid]).attachment_ids == [aid]


def test_chat_attachment_planner_dispatches_schema_tools(db, monkeypatch, tmp_path):
    from app.agents import attachments as store
    from app.routers import assistant as qa
    from app.agents import attachment_chat
    monkeypatch.setattr(store, "ROOT", tmp_path)
    p = Project(title="planner"); db.add(p); db.commit()
    m = store.stage(db=db, project_id=p.id, filename="x.txt", data="风来了。".encode())
    seen = []
    class Provider:
        model = "test"
        def chat(self, system, user):
            seen.append((system, user))
            return '{"tool_calls":[{"name":"read_attachment","arguments":{"attachment_id":"' + m["attachment_id"] + '","offset":0,"limit":100}}]}'
    monkeypatch.setattr(attachment_chat, "get_planner_provider", lambda: Provider())
    plan = attachment_chat.run_attachment_tools(db=db, project_id=p.id, query="读附件", attachment_ids=[m["attachment_id"]])
    assert "read_attachment" in plan["tool_results"]
    assert plan["tool_results"]["read_attachment"]["text"] == "风来了。"
    assert '"parameters"' in seen[0][1] and "不可信" in seen[0][0]
    # The planner sees metadata, not unsolicited full source text.
    assert "风来了。" not in seen[0][1]
    with pytest.raises(Exception) as exc:
        qa.assistant_qa(qa.AssistantQARequest(project_id=p.id, query="读", attachment_ids=["unknown"]), db=db)
    assert exc.value.status_code == 404


def test_preparation_preserves_literature_and_meta(db, monkeypatch):
    from app.agents.ingest_clean import preview_ingest_clean
    text = "第一章 风\n他笑了笑。\n\n" + "他问怎么办？\n" * 80
    ordinary = preview_ingest_clean(text=text)
    from app.ingestion.parse_txt import parse_txt
    meta = parse_txt(content=text, clean_tail=False)
    explicit = preview_ingest_clean(text=text, chapter_meta=meta)
    assert ordinary["chapters"] == explicit["chapters"] == meta
    assert not ordinary["report"]["removed_spans"]
    source = "行首\n风来。一秒记住【abc.info】，为您提供精彩小说阅读。风去。\n\n末尾"
    out = preview_ingest_clean(text=source, chapter_meta=[{"chapter_no": 9, "title": "X", "body": source}])
    assert out["chapters"][0]["body"] == "行首\n风来。风去。\n\n末尾"
    assert all(out["report"]["checks"].values())


def test_readback_scope_and_stale(db, monkeypatch):
    from app.agents import ingest_clean as agent
    from app.ingestion.store import ingest_text
    from app.agents.registry import invoke_tool
    monkeypatch.setattr(agent, "review_samples", lambda s: {"status": "sample_reviewed", "model_participated": True})
    result = ingest_text(db, "正文。" * 30)
    a = Project(title="A", reference_novel_ids=[result["novel_id"]]); b = Project(title="B")
    db.add_all([a,b]); db.commit()
    def check(pid, nid):
        return invoke_tool("check_ingest_clean", db=db, project_id=pid, novel_id=nid)
    assert check(a.id, result["novel_id"])["stale"] is False
    with pytest.raises(Exception) as exc:
        check(b.id, result["novel_id"])
    assert exc.value.status_code == 403
    with pytest.raises(Exception) as exc:
        check(a.id, 999999)
    assert exc.value.status_code == 404
    row = db.scalars(select(NovelChunk)).first(); row.text += "改动"; db.commit()
    assert check(a.id, result["novel_id"])["status"] == "stale"
    row.text = "正文。" * 30; db.commit()
    novel = db.get(RawNovel, result["novel_id"]); novel.source_text += "原文改动"; db.commit()
    assert check(a.id, result["novel_id"])["status"] == "stale"


def test_high_confidence_residual_blocks(db, monkeypatch):
    from app.agents import ingest_clean as agent
    p = agent.preview_ingest_clean(text="正文。")
    p["chapters"][0]["body"] += "无限大冒险全文阅读"
    monkeypatch.setattr(agent, "review_samples", lambda s: {"status": "sample_reviewed", "model_participated": True})
    assert agent.check_ingest_clean(prepared=p)["status"] == "blocked"


def test_malformed_planner_output_isolated(db, monkeypatch, tmp_path):
    from app.agents import attachments as store, attachment_chat
    monkeypatch.setattr(store, "ROOT", tmp_path)
    p = Project(title="scope"); db.add(p); db.commit()
    m = store.stage(db=db, project_id=p.id, filename="x.txt", data=b"hello")
    class Bad:
        def chat(self, *args):
            return '{"tool_calls":[null]}'
    monkeypatch.setattr(attachment_chat, "get_planner_provider", lambda: Bad())
    result = attachment_chat.run_attachment_tools(db=db, project_id=p.id, query="读附件", attachment_ids=[m["attachment_id"]])
    assert result["planner_model_participated"] is False
    assert result["tool_results"]["read_attachment"]["text"] == "hello"


def test_attachment_backslash_name_rejected(db, monkeypatch, tmp_path):
    from app.agents import attachments as store
    monkeypatch.setattr(store, "ROOT", tmp_path)
    p = Project(title="scope"); db.add(p); db.commit()
    with pytest.raises(Exception) as exc:
        store.stage(db=db, project_id=p.id, filename=".." + chr(92) + "evil.txt", data=b"hello")
    assert exc.value.status_code == 422


def test_repeated_ad_header_never_deletes_following_story():
    from app.corpus_health import strip_repeated_ads
    text = "推广12345\n他走进院子。\n\n末尾\n"
    cleaned, _ = strip_repeated_ads(text, templates={"推广12345"}, preserve_layout=True)
    assert cleaned == "\n他走进院子。\n\n末尾\n"


def test_provider_failure_never_passes(monkeypatch):
    from app.agents.ingest_clean import review_samples
    from app.routers import assistant_shared
    class Bad:
        def chat(self, *a):
            raise RuntimeError("network unavailable")
    monkeypatch.setattr(assistant_shared, "_ensure_assistant_provider", lambda: Bad())
    result = review_samples([{"text": "正文"}])
    assert result["model_participated"] is False
    assert result["status"] == "review_required"



