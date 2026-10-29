"""回归测试：2026-10-30 真模型验收跑抓出来的三个「跑到一半才炸」的洞。

==============================================================================
三个洞的共同点：**都不在常规路径上**，所以类型检查、单测、CI 全绿也没抓到。
==============================================================================

1. `generate_next_segment` 每第 10 段必炸
   —— `engine.py` 里写的是 `_schedule_async_audit_kg(db, project_id)`，
   而那个作用域里只有 `project`，没有 `project_id` ⇒ NameError。
   异常发生在 commit **之后** ⇒ 段落已落库，调用方却收到 500。
   （真模型验收跑第 10 段当场炸；离线验收跑同样第 10 段炸。）

2. `plan_route(..., raise_errors=True)` 必炸
   —— docstring 承诺了这个参数，调用方也照着传，但签名里没有 ⇒ TypeError；
   函数体里的 `if raise_errors:` 又是个未定义名 ⇒ 走到异常分支再 NameError。
   （修炼路线规划端点 100% 失败。）

3. `describe_narrative` 渲染「心理描写手法」维度必炸
   —— `psych_ratio` 在渲染函数里**从没取过**，只在用到时才被解释器去找 ⇒ NameError。
   而且只在档案里有这一维时才炸 ⇒ 别的维度都测不出来。

为什么这三处值得单独守：它们都是**「路径走到某一步才触发」**的类型。
断言必须真的把那条路径走一遍，光看源码或断言字符串在不在都不算数。
"""

from pathlib import Path
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.db import Base
from app.models import Project, Generation
from app.generation import generate_next_segment
from app.llm.provider import LocalFallbackLLMProvider
from app.generation.cultivation import plan_route
from app.style.narrative import describe_narrative


def _fixture():
    db = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(db.bind)
    p = Project(title='回归项目', target_length=3000, generation_length=1000,
                genre='都市异能', premise='一个普通人在雨夜醒过来。')
    db.add(p)
    db.commit()
    db.refresh(p)
    return db, p


# --------------------------------------------------------------------------- #
# 1. 每第 10 段
# --------------------------------------------------------------------------- #

def test_生成到第10段不炸_且10段都落库():
    """跑到 AUTO_AUDIT_INTERVAL（10）那一次，异步审计的触发点。

    修之前：第 10 段抛 NameError（`project_id` 未定义）⇒ 段落已落库但调用方收到 500。
    """
    from app.generation.engine import AUTO_AUDIT_INTERVAL
    db, p = _fixture()
    provider = LocalFallbackLLMProvider()
    for i in range(1, AUTO_AUDIT_INTERVAL + 1):
        out = generate_next_segment(db, p, provider=provider, max_chars=600)
        assert out["segment_no"] == i, f'第 {i} 段段号不对'
    n = db.query(Generation).filter(Generation.project_id == p.id).count()
    assert n == AUTO_AUDIT_INTERVAL, f'落库段数 {n} != {AUTO_AUDIT_INTERVAL}'


# --------------------------------------------------------------------------- #
# 2. plan_route 的 raise_errors
# --------------------------------------------------------------------------- #

class _Boom:
    def chat(self, system, user, **kwargs):
        raise RuntimeError('渠道不通')


def test_plan_route_接受_raise_errors_且渠道异常按约定上抛():
    """端点靠这个参数区分「渠道不通」和「模型没给路线」。"""
    profile = {"境界": "炼气"}
    system = {"name": "体系", "levels": []}
    with pytest.raises(RuntimeError, match='渠道不通'):
        plan_route(_Boom(), profile, system, raise_errors=True)


def test_plan_route_默认不抛_规划失败返回空():
    profile = {"境界": "炼气"}
    system = {"name": "体系", "levels": []}
    assert plan_route(_Boom(), profile, system) == {}


# --------------------------------------------------------------------------- #
# 3. 心理写法占比
# --------------------------------------------------------------------------- #

def test_describe_narrative_带心理维度不炸且占比进文本():
    profile = {
        "rules": {"心理描写手法": [{"rule": "用动作外化情绪，不直接写心理"}]},
        "psych_ratio": "直陈心理为主",
    }
    out = describe_narrative(profile)
    assert out
    assert "直陈心理为主" in out, '心理写法占比没渲染进档案文字'


def test_describe_narrative_没有占比字段也不炸():
    """档案里没有 psych_ratio（老档案 / 没抽到）时也不能炸。"""
    profile = {"rules": {"心理描写手法": [{"rule": "用动作外化情绪"}]}}
    assert describe_narrative(profile)


# --------------------------------------------------------------------------- #
# 5. 空正文不能落库
# --------------------------------------------------------------------------- #

class _Empty:
    """永远返回空串的 provider（模拟「思考吃光预算 ⇒ content 为空」）。"""

    def __init__(self):
        self.calls = 0

    def chat(self, system, user, **kwargs):
        self.calls += 1
        return ""

    def summarize(self, text):
        return ""


def test_模型连续返回空正文时不落库且报错():
    """真模型验收跑实测到过：一次调用 8 秒返回空串，空段落照样落库。

    修之前：空正文被当正常段落提交（小说多一页空白 + 摘要链被污染）。
    修之后：重试一次；仍为空 ⇒ 抛 502 且**不写入**。
    """
    from fastapi import HTTPException
    db, p = _fixture()
    provider = _Empty()
    with pytest.raises(HTTPException) as ei:
        generate_next_segment(db, p, provider=provider, max_chars=600)
    assert ei.value.status_code == 502
    assert ei.value.detail["status"] == "empty_segment"
    assert provider.calls >= 2, '空正文没有重试'
    n = db.query(Generation).filter(Generation.project_id == p.id).count()
    assert n == 0, f'空段落仍然落了库（{n} 段）'


def test_流式路径空正文同样不落库():
    """界面主流程走的是**流式**那条路 ⇒ 闸门必须两边都有。

    口径不一致是最阴的一种 bug：同步接口守住了，界面那条路没守住，
    用户照样会看到空白页。
    """
    from app.generation.engine import generate_next_segment_stream
    db, p = _fixture()
    provider = _Empty()
    events = list(generate_next_segment_stream(db, p, provider=provider, max_chars=600))
    errs = [e for e in events if e.get("type") == "error"]
    assert errs, f'流式路径没有报空正文错误：{events}'
    assert errs[0]["status"] == "empty_segment"
    n = db.query(Generation).filter(Generation.project_id == p.id).count()
    assert n == 0, f'空段落仍然落了库（{n} 段）'


# --------------------------------------------------------------------------- #
# 6. _plan_exhausted 里 json 的导入
# --------------------------------------------------------------------------- #

def test_plan_exhausted_遇到字符串时间线不炸():
    """plot_timeline 存成 JSON 字符串时，解析要用 json —— 模块里必须 import 过。

    修之前：`json.loads` 抛 NameError（不是 ValueError）⇒ except 抓不住 ⇒ 直接崩。
    """
    from app.generation.engine import _plan_exhausted
    db, p = _fixture()
    p.plot_timeline = json.dumps({"planning": {"nodes": {}, "segment_contracts": {}}},
                                 ensure_ascii=False)
    db.commit()
    assert _plan_exhausted(p) is False
