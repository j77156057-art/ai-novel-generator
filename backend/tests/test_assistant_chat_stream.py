"""助手「剧情讨论」流式链路的上下文与协议测试。

覆盖：
- `_build_chat_context` 把剧情圣经/章纲/最近正文/提醒拼进资料；
- 问「第 N 段」时定向取该段，而不是随便取最近几段；
- 长正文截断时带显式标记（模型不会顺着截断处续写）；
- SSE 帧格式与 generation 流式协议一致。

跑法：pytest tests/test_assistant_chat_stream.py -q
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Generation, Project
from app.routers.assistant import (
    _build_chat_context,
    _sse_event,
    _thinking_meta,
    _web_search_hint,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _seed(db) -> Project:
    project = Project(
        title="雾港观测者",
        genre="科幻",
        style="豆瓣阅读",
        premise="近未来雾港，观测员追查姐姐的旧案。",
        generation_length=3000,
        target_length=200000,
        outline=["浮标事故", "旧案浮出"],
        plot_bible={
            "synopsis": "观测员在雾港追查姐姐的死因。",
            "characters": [
                {
                    "name": "林汐",
                    "role": "夜班观测员",
                    "motivation": "查清姐姐死因",
                    "card": {
                        "身份": {"性别": "女", "年龄": "27", "身份职业": "观测员"},
                        "声纹": {"自称": "我", "句长": "短", "口头禅": ["记录不会骗人"]},
                    },
                },
                {"name": "林满", "role": "实习技术员", "motivation": "摆脱姐姐保护"},
            ],
            "relationships": [
                {"a": "林汐", "b": "林满", "relation": "姐妹"},
            ],
            "conflicts": ["真相与亲情的冲突"],
            "foreshadowing": ["旧观测站的录音带"],
            "direction": "沿着录音带追到当年的值班表。",
        },
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    db.add(Generation(
        project_id=project.id, segment_no=1, chapter_range="第1章",
        text="第1段正文：雨夜的观测站。", summary={"short": "雨夜事故"},
    ))
    db.add(Generation(
        project_id=project.id, segment_no=2, chapter_range="第2章",
        text="第2段正文" * 400, summary={"short": "旧录音带浮现"},
    ))
    db.commit()
    return project


def test_context_contains_story_material(db):
    project = _seed(db)
    context, evidence, steps = _build_chat_context(db, project, "接下来剧情怎么走？", 1)

    assert "【剧情圣经】" in context
    assert "林汐" in context and "自称「我」" in context
    assert "关系：林汐—林满：姐妹" in context
    assert "核心冲突" in context and "伏笔" in context
    assert "【章纲】" in context and "第2章：旧案浮出" in context
    assert "【已写正文（只读）】" in context
    assert "第2段（第2章）" in context
    assert evidence, "讨论上下文应附带可落库的证据行"
    assert [s["name"] for s in steps] == [
        "读取剧情圣经",
        "读取最近正文",
    ]
    assert "角色" in steps[0]["meta"]
    assert steps[1]["meta"].startswith("第1段、第2段")


def test_long_segment_is_clipped_with_marker(db):
    project = _seed(db)
    context, _, _ = _build_chat_context(db, project, "最近写的这段有什么问题？", 1)
    assert "以下截断，原文未提供" in context


def test_target_segment_is_read_by_number(db):
    project = _seed(db)
    context, _, steps = _build_chat_context(db, project, "第1段写了什么？", 1)
    assert "第1段（第1章）" in context
    assert "第2段（第2章）" not in context
    assert steps[-1]["meta"].startswith("第1段")


def test_sse_event_is_valid_data_frame():
    frame = _sse_event({"type": "delta", "text": "你好"})
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    payload = json.loads(frame[len("data: "):].strip())
    assert payload == {"type": "delta", "text": "你好"}


def test_web_search_hint_detects_meta_request():
    hint = _web_search_hint("你可以去网络搜索一下最近这个题材的流行写法吗？")
    assert "能力边界" in hint
    assert "没有联网能力" in hint


def test_web_search_hint_ignores_novel_topic():
    assert _web_search_hint("主角能不能上网搜索资料？") == ""
    assert _web_search_hint("把网络搜索写进小说里合理吗？") == ""


class _FakeProvider:
    """只带 last_stream_stats 的替身：测「推理留痕」的换算，不碰网络。"""

    def __init__(self, stats):
        self.last_stream_stats = stats


def test_thinking_meta_reports_duration_and_chars():
    provider = _FakeProvider({
        "reasoning_chars": 320,
        "first_reasoning_ms": 200,
        "first_content_ms": 3200,
    })
    assert _thinking_meta(provider) == {"chars": 320, "duration_ms": 3000}


def test_thinking_meta_is_none_without_reasoning():
    assert _thinking_meta(_FakeProvider({"reasoning_chars": None})) is None
    assert _thinking_meta(_FakeProvider({})) is None
    # 没有 provider 统计（本地兜底 / 旧渠道）也不能凭空编一个耗时。
    assert _thinking_meta(object()) is None


def test_thinking_meta_keeps_chars_when_duration_unknown():
    provider = _FakeProvider({"reasoning_chars": 88, "first_content_ms": 900})
    assert _thinking_meta(provider) == {"chars": 88, "duration_ms": None}
