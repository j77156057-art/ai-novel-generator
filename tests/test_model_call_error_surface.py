"""模型渠道失败要在界面上说得清（2026-10-29）。

## 这条测试是**真机点出来**的

在浏览器里点「生成体系」，界面只回一句：

```
Internal Server Error
```

后端栈是：

```
RuntimeError: MiniMax chat 调用失败: HTTP Error 429: Too Many Requests
```

也就是说 **渠道限流被当成了本站 500**。人看到「Internal Server Error」能做的
只有干瞪眼：不知道该等一会儿、该去充值，还是该报 bug。

单元测试、类型检查、`npm run build`、CI 全绿都发现不了这类问题 —— 因为
**代码路径本身是通的**，只是失败时的出口没写。

## 钉住的口径

- 渠道故障 → **502**（不是 500：500 的含义是「本站写错了」）；
- `detail` 里要有**能照着做**的判断（限流 / 额度 / 鉴权 / 超时）+ 渠道原话；
- 原话截到 200 字（整段栈放界面没人看得懂，但也不能吞掉线索）；
- **端点内部自己抛的 HTTPException 不许被这层改写**（400 说「还没建体系」，
  不能变成 502）；
- 结构守门：`projects.py` 里任何 `provider.chat(` 都必须裹在 `_model_call(...)`
  里 —— 以后新加端点忘了裹，这条测试就红。
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
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
from app.routers.projects import (  # noqa: E402
    _model_call, _model_failure_detail,
)


# ======================================================================
# 1. 文案：翻成能照着做的话
# ======================================================================

def test_限流要说清是限流():
    detail = _model_failure_detail(
        "生成修炼体系", RuntimeError("MiniMax chat 调用失败: HTTP Error 429: Too Many Requests"))
    assert detail.startswith("生成修炼体系没做成：")
    assert "限流" in detail or "额度" in detail
    # 渠道原话要留着 —— 「重试还是充值」只能靠它判断
    assert "429" in detail


def test_额度用尽要说额度():
    detail = _model_failure_detail("生成档位分层", RuntimeError("FreeUsageLimitError"))
    assert "额度" in detail


def test_鉴权失败要说鉴权():
    detail = _model_failure_detail("补全角色卡", RuntimeError("HTTP Error 401: Unauthorized"))
    assert "鉴权" in detail


def test_超时要说超时():
    detail = _model_failure_detail("规划修炼路线", TimeoutError("timed out"))
    assert "超时" in detail


def test_认不出的异常也不吞():
    detail = _model_failure_detail("给角色定量级", RuntimeError("socket closed by peer"))
    assert "模型渠道调用失败" in detail
    assert "socket closed by peer" in detail


def test_原话截断到两百字():
    detail = _model_failure_detail("生成修炼体系", RuntimeError("x" * 500))
    assert "…" in detail
    assert len(detail) < 300


def test_换行被压平():
    """渠道报错常带多行，塞进一行红条里会撑坏布局。"""
    detail = _model_failure_detail("生成修炼体系", RuntimeError("line1\nline2\nline3"))
    assert "\n" not in detail
    assert "line1 line2 line3" in detail


# ======================================================================
# 2. `_model_call`：成功照旧、失败换 502、内部 4xx 不许被改写
# ======================================================================

def test_成功时原样返回():
    assert _model_call("随便", lambda: {"ok": 1}) == {"ok": 1}


def test_失败时是502不是500():
    def boom():
        raise RuntimeError("HTTP Error 429: Too Many Requests")

    with pytest.raises(HTTPException) as ei:
        _model_call("生成修炼体系", boom)
    assert ei.value.status_code == 502
    assert "429" in str(ei.value.detail)


def test_端点自己的业务报错不被改写():
    """「还没建体系」是 400 的业务判断，不是渠道故障。"""

    def boom():
        raise HTTPException(status_code=400, detail="还没有修炼体系，先建体系")

    with pytest.raises(HTTPException) as ei:
        _model_call("生成档位分层", boom)
    assert ei.value.status_code == 400
    assert ei.value.detail == "还没有修炼体系，先建体系"


# ======================================================================
# 3. 端点级：真打一次请求，看回的是不是人话
# ======================================================================

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


class _Boom:
    """任何一次 chat 都像渠道限流那样炸。"""

    def __init__(self, exc):
        self.exc = exc

    def chat(self, *a, **kw):
        raise self.exc


def test_生成体系_渠道429回可读的502(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, _Boom(
            RuntimeError("MiniMax chat 调用失败: HTTP Error 429: Too Many Requests")))
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        assert r.status_code == 502, r.text
        detail = r.json()["detail"]
        assert detail != "Internal Server Error"
        assert "生成修炼体系" in detail
        assert "429" in detail


def test_补全角色卡_超时回可读的502(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, _Boom(TimeoutError("timed out")))
        r = client.post(f"/api/projects/{p.id}/character-cards/generate",
                        json={"name": "沈砚"})
        assert r.status_code == 502, r.text
        assert "超时" in r.json()["detail"]


def test_生成体系_成功路径没被改坏(monkeypatch):
    """包了一层之后，正常返回必须原样是 200 + 解析出的体系。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = _project(db)

        class Good:
            def chat(self, system, user, **kw):
                return json.dumps({"体系": [{
                    "名称": "守夜阶",
                    "说明": "雾港的提灯体系",
                    "类型": "序列型",
                    "境界": [
                        {"名称": "守夜人", "说明": "提灯能照见一里内的暗", "档位": "初入"},
                        {"名称": "执灯人", "说明": "灯光能压住一条街", "档位": "小成"},
                        {"名称": "点灯人", "说明": "一人可封半座港", "档位": "一方"},
                    ],
                }]}, ensure_ascii=False)

        client = _client(db, monkeypatch, Good())
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["体系"], body


# ======================================================================
# 4. 结构守门：以后新加的端点不许再裸调 provider.chat
# ======================================================================

def test_projects里的模型调用都必须裹在model_call里():
    """裸调 = 渠道一抖就变裸 500。

    判断法：`provider.chat(` 那一行，往上两行内（含本行）必须出现 `_model_call(`。
    """
    src = (projects.__file__ or "")
    assert src, "拿不到 projects.py 的路径"
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    naked = []
    for i, line in enumerate(lines):
        if "provider.chat(" not in line:
            continue
        window = lines[max(0, i - 2): i + 1]
        if not any("_model_call(" in w for w in window):
            naked.append((i + 1, line.strip()))
    assert not naked, f"这些调用没裹 _model_call：{naked}"
