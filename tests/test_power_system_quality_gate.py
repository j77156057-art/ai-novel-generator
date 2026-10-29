"""体系解析的**结果闸门**：解析成功 ≠ 解析正确（2026-10-29，App 自己的 MiniMax 通道）。

## 这条测试是**真机跑出来**的

走 App 自己的后端（`POST /api/projects/{id}/power-system/generate` → App 配的 MiniMax 渠道）
跑三类型体系，三类都 HTTP 200、类型也对、境界也解析出来了 —— 但有两类**内容是错的**：

1. **细分型那一版**：29 个境界，名字是「初期 / 中期 / 大圆满」**反复出现**。
   模型把层级摊平成了行：每个大境界的三段小境界各占一行，
   而真正的大境界名（炼体境、引气境…）整个丢了。
2. **序列型那一版**：境界名是「列：见隙」「列：识烬」—— 前缀「列」是**表头**，
   不是境界的一部分。

这两种结果都会直接落库并显示在界面上，**比解析失败危险得多**
（解析失败至少还有重试这道闸）。所以闸门要拦的不是「有没有返回」，
而是「返回的东西像不像一套体系」。

## 钉住的口径

- 同名境界重复出现 ⇒ 不合格（层级被摊平）；
- 境界名带表头前缀（「列：」「序列：」）⇒ 不合格（列名认错了）；
- 闸门要返回**人能读懂的原因**，好把它回灌给模型让它自己改；
- 重试提示里必须带**上一版的具体错处**，不能只说「格式不对」；
- 端点不许再用「格式认不出」这种没信息量的说法糊弄人。
"""
from __future__ import annotations

import json
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.generation.power_system_gen import (  # noqa: E402
    plausible_systems, retry_instructions, system_quality_issues,
)
from app.models import Project  # noqa: E402
from app.routers import projects  # noqa: E402


# ======================================================================
# 1. 闸门本身
# ======================================================================

def test_同名境界重复出现算不合格():
    """真机那版细分型：29 个境界，名字是「初期/中期/大圆满」反复出现。"""
    systems = [{
        "名称": "玄冥道宗·境界体系",
        "类型": "细分型",
        "境界": [
            {"名称": "初期", "说明": "1. 第一层：淬皮"},
            {"名称": "中期", "说明": "2. 第四层：淬骨"},
            {"名称": "大圆满", "说明": "3. 第七层：炼血"},
            {"名称": "初期", "说明": "1. 第一层：引气"},
        ],
    }]
    issues = system_quality_issues(systems)
    assert issues, "重名境界必须被拦下"
    assert any("重复" in i for i in issues), issues
    assert plausible_systems(systems) is False


def test_境界名带表头前缀算不合格():
    """真机那版序列型：名字是「列：见隙」「列：识烬」。"""
    systems = [{
        "名称": "无明律",
        "类型": "序列型",
        "境界": [{"名称": "列：见隙"}, {"名称": "列：识烬"}],
    }]
    issues = system_quality_issues(systems)
    assert issues, "表头前缀必须被拦下"
    assert plausible_systems(systems) is False


def test_正常体系还是合格():
    systems = [{
        "名称": "守夜阶",
        "类型": "序列型",
        "境界": [
            {"名称": "守夜人", "说明": "提灯能照见一里内的暗"},
            {"名称": "执灯人", "说明": "灯光能压住一条街"},
            {"名称": "掌灯人", "说明": "一盏灯定一城夜"},
        ],
    }]
    assert system_quality_issues(systems) == []
    assert plausible_systems(systems) is True


def test_名字里带冒号但不是表头词不算不合格():
    """「开脉：初成」这种带冒号的名字不是表头残留。"""
    systems = [{"境界": [{"名称": "开脉：初成"}, {"名称": "开脉：贯通"}]}]
    assert plausible_systems(systems) is True


# ======================================================================
# 2. 重试提示要把原因回灌
# ======================================================================

def test_重试提示带上一版的具体错处():
    issues = system_quality_issues([
        {"境界": [{"名称": "初期"}, {"名称": "中期"}, {"名称": "初期"}]},
    ])
    hint = retry_instructions(issues)
    assert "初期" in hint, hint
    assert "重复" in hint, hint
    assert "只输出一个 JSON 对象" in hint, hint


def test_没有错处时也给硬要求():
    hint = retry_instructions([])
    assert "只输出一个 JSON 对象" in hint, hint
    assert "境界名不能重名" in hint, hint


# ======================================================================
# 3. 端点行为：拦 → 带原因重试 → 成了
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


FLAT = json.dumps({"体系": [{
    "名称": "玄冥道宗",
    "类型": "细分型",
    "境界": [
        {"名称": "初期", "说明": "1. 第一层：淬皮"},
        {"名称": "中期", "说明": "2. 第四层：淬骨"},
        {"名称": "初期", "说明": "3. 第一层：引气"},
    ],
}]}, ensure_ascii=False)


GOOD = json.dumps({"体系": [{
    "名称": "守夜阶",
    "类型": "序列型",
    "境界": [
        {"名称": "守夜人", "说明": "提灯能照见一里内的暗"},
        {"名称": "执灯人", "说明": "灯光能压住一条街"},
        {"名称": "掌灯人", "说明": "一盏灯定一城夜"},
    ],
}]})


def test_摊平的那一版会被拦下并带原因重试(monkeypatch):
    """第一次回的是摊平的（重名）→ 闸门拦住 → 带原因重试 → 这次对了。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)
    calls: list[str] = []

    class Two:
        def chat(self, system, user, **kw):
            calls.append(user)
            return FLAT if len(calls) == 1 else GOOD

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, Two())
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert len(calls) == 2, "应该重试一次"
        # 第二次的提示里必须带**具体错处**，不能只有「格式不对」
        assert "重复" in calls[1], calls[1][:500]
        assert "初期" in calls[1], calls[1][:500]


def test_一直不合格就如实说不行并给出原因(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)

    class AlwaysFlat:
        def chat(self, system, user, **kw):
            return FLAT

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, AlwaysFlat())
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        detail = body["detail"]
        assert "重复" in detail, detail
        assert "格式认不出" not in detail, "别再用没信息量的说法糊弄人"


def test_失败时把模型原文带出来(monkeypatch):
    """只说「认不出」等于让人干瞪眼 —— 原文要看得到。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)

    class AlwaysFlat:
        def chat(self, system, user, **kw):
            return FLAT

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, AlwaysFlat())
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        body = r.json()
        assert body["ok"] is False
        assert body.get("原文"), body
        assert "初期" in body["原文"], body["原文"][:200]


def test_类型与小境界对不上时要报出来():
    """模型标了「粗放型」却给了每境的细分 ⇒ 小境界按约定剥掉，但**冲突要报**。

    ⚠️ 这里**不改类型**：类型是「这套体系有多硬」的约定，剥掉多余的、
    保留模型给的类型，再把冲突回灌给模型让它自己改。
    """
    from app.generation.power_system_gen import parse_systems, system_quality_issues

    raw = json.dumps({"体系": [{
        "名称": "苍冥四阶",
        "类型": "粗放型",
        "境界": [
            {"名称": "凡人", "小境界": [{"名称": "初期"}, {"名称": "中期"}]},
            {"名称": "修行者", "小境界": [{"名称": "初期"}, {"名称": "中期"}]},
        ],
    }]})
    systems = parse_systems(raw)
    assert systems, "应该解析得出来"
    s = systems[0]
    assert s["类型"] == "粗放型", s["类型"]
    assert not s["境界"][0].get("小境界"), "非细分型的小境界按约定要剥掉"
    issues = system_quality_issues(systems)
    assert any("小境界" in i for i in issues), issues
    assert any("粗放型" in i for i in issues), issues


# ======================================================================
# 4. 提示词：写着的要求必须真的发出去
# ======================================================================

def test_端点必须发完整的_system_提示词(monkeypatch):
    """这条是**真机点出来的**。

    模块里写好了一份提示词（三型说明、硬要求、输出 JSON 的格式），
    但端点只发了一行「你在为一本中文小说设计修炼体系。」——
    **那份提示词定义了却从没发出去**。于是模型：
    - 不知道要输出 JSON（回表格、回标题）；
    - 不知道三型怎么选（类型全靠猜）；
    - 不知道档位怎么分层。

    ⇒ 守卫：端点发给 provider 的 system 段必须含那些要求。
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)
    seen: dict = {}

    class Spy:
        def chat(self, system, user, **kw):
            seen["system"] = system
            seen["user"] = user
            return GOOD

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, Spy())
        r = client.post(f"/api/projects/{p.id}/power-system/generate", json={})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True, r.json()
    assert "输出 JSON" in seen["system"], seen["system"][:200]
    assert "体系类型" in seen["system"], seen["system"][:200]
    assert "档位" in seen["system"], seen["system"][:200]


def test_作者指定类型_不符就带原因重试(monkeypatch):
    """要粗放型、模型给了细分型 ⇒ 记问题、带原因重试。"""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)
    calls: list[str] = []

    soft = json.dumps({"体系": [{
        "名称": "苍冥四阶", "类型": "粗放型",
        "境界": [
            {"名称": "凡人", "说明": "以血肉之身承受尘世"},
            {"名称": "修行者", "说明": "借天地之力夺一线长生"},
            {"名称": "圣人", "说明": "一念干涉天地"},
        ],
    }]}, ensure_ascii=False)

    class Two:
        def chat(self, system, user, **kw):
            calls.append(user)
            return GOOD if len(calls) == 1 else soft

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, Two())
        r = client.post(f"/api/projects/{p.id}/power-system/generate",
                        json={"system_type": "粗放型"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True, body
        assert len(calls) == 2, "类型不符应该重试一次"
        assert "粗放型" in calls[0], "user 段要写明作者要的类型"
        assert "粗放型" in calls[1], "重试时要再说一遍"
        assert "类型不对" in calls[1], calls[1][-600:]


def test_作者指定类型_一直不符就如实说不行(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    Base.metadata.create_all(engine)

    class AlwaysFine:
        def chat(self, system, user, **kw):
            return GOOD  # 类型是「序列型」，作者要的是「粗放型」

    with Session(engine) as db:
        p = _project(db)
        client = _client(db, monkeypatch, AlwaysFine())
        r = client.post(f"/api/projects/{p.id}/power-system/generate",
                        json={"system_type": "粗放型"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert "粗放型" in body["detail"], body["detail"]
