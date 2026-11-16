"""「生成体系」解析不出时要自动重试一次（2026-10-29 真机两轮取证）。

## 为什么需要重试

同一个提示词，同一天，模型回的形态每次都不同：

- 第一轮：`#### 1. 开脉境` 标题 + 正文
- 第二轮：`1. **炼气境**` 有序列表

解析器已覆盖 JSON / 标题 / 有序列表 / 表格四种，但追不完。
⇒ 解析不出时**带一句「只输出 JSON」重试一次**，这是「门槛 + 把原因回灌给
执行方让它自己改」，比无限加形态支持稳。

重试仍失败时，界面文案必须说清是**格式**问题（「AI 回了内容但格式认不出」），
不能退回「AI 没给出可用体系」—— 那会让人去改要求，而问题在格式。
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.models import Project  # noqa: E402
from app.routers import projects  # noqa: E402

GOOD = {
    "体系": [{
        "名称": "守夜阶", "说明": "雾港的提灯体系", "类型": "序列型",
        "境界": [
            {"名称": "守夜人", "说明": "提灯能照见一里内的暗", "档位": "初入"},
            {"名称": "执灯人", "说明": "灯光能压住一条街", "档位": "小成"},
            {"名称": "点灯人", "说明": "一人可封半座港", "档位": "一方"},
        ],
    }]
}


class _Scripted:
    """按顺序吐预设内容，并记录每次收到的 user 提示词。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, system, user, **kw):
        self.seen.append(user)
        return self.replies.pop(0) if self.replies else ""


def _client(db, monkeypatch, provider):
    app = FastAPI()
    app.include_router(projects.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", lambda: provider)
    return TestClient(app)


def _project(db):
    p = Project(title="测试", premise="雾港守夜", outline=[], plot_timeline={"sentinel": True})
    db.add(p)
    db.commit()
    return p


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db


def test_格式认不出时重试一次并成功(db_session, monkeypatch):
    provider = _Scripted(["这是一套体系。总纲：共九境。", json.dumps(GOOD, ensure_ascii=False)])
    client = _client(db_session, monkeypatch, provider)
    p = _project(db_session)

    r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert len(provider.seen) == 2, f"应该调用两次，实际 {len(provider.seen)}"
    # 重试那次必须把「只输出 JSON」的硬要求回灌进去
    assert "只输出一个 JSON" in provider.seen[1], provider.seen[1][-200:]


def test_两次都认不出时说清是格式问题(db_session, monkeypatch):
    provider = _Scripted(["总纲：共九境。", "还是散文。", "第三次也不该被调用"])
    client = _client(db_session, monkeypatch, provider)
    p = _project(db_session)

    r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "格式" in body["detail"], body["detail"]
    assert len(provider.seen) == 2, "最多重试一次，别无限重试"


def test_一次就成时不多调(db_session, monkeypatch):
    provider = _Scripted([json.dumps(GOOD, ensure_ascii=False)])
    client = _client(db_session, monkeypatch, provider)
    p = _project(db_session)

    r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert len(provider.seen) == 1, "第一次就解析成功，不该再调"
