# -*- coding: utf-8 -*-
"""引号口径归一化 + 空稿防线。

真机回归（2026-11-22 五章）：
- 第 3 章整章用「」，其余四章用“”，声纹检查只认“”导致全角色被判「没台词」；
- 模型偶发「只有 reasoning、没有 content」，空段被落库后只能手工删库。
"""
from __future__ import annotations

import pathlib
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.models import Generation, NovelChunk, Project  # noqa: E402
from app.style.quotes import (  # noqa: E402
    detect_quote_style,
    normalize_quotes,
    normalize_to_reference,
)


def _session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng, sessionmaker(bind=eng, autoflush=False)()


def test_detect_quote_style():
    assert detect_quote_style("他说：“走。”她说：“好。”") == "curly"
    assert detect_quote_style("他说：「走。」她说：「好。」") == "corner"
    assert detect_quote_style("没有对白。") is None
    # 混用取多数
    assert detect_quote_style("「一」「二」「三」「四」“五”") == "corner"


def test_normalize_quotes_between_paired_styles():
    got, n = normalize_quotes("他说：「走。」又说：『好』。", "curly")
    assert got == "他说：“走。”又说：‘好’。"
    assert n == 4
    back, n2 = normalize_quotes(got, "corner")
    assert back == "他说：「走。」又说：『好』。"
    assert n2 == 4
    # 半角形态不转
    same, n3 = normalize_quotes('他说："走。"', "curly")
    assert same == '他说："走。"'
    assert n3 == 0


def test_normalize_to_reference_uses_reference_style():
    eng, s = _session()
    s.add(NovelChunk(novel_id=99, chapter_no=1, idx=0,
                     text="他说：“走。”" * 30))
    s.commit()

    got, info = normalize_to_reference(
        s, Project(title="t"), "他说：「走。」", novel_ids=[99])
    assert info["reference_style"] == "curly"
    assert info["applied"] is True
    assert got == "他说：“走。”"
    s.close()
    eng.dispose()


def test_normalize_without_reference_keeps_own_majority():
    eng, s = _session()
    got, info = normalize_to_reference(
        s, Project(title="t"), "「一」「二」「三」「四」“五”", novel_ids=[])
    assert info["reference_style"] is None
    assert info["target_style"] == "corner"
    assert got == "「一」「二」「三」「四」「五」"
    s.close()
    eng.dispose()


def test_voice_check_recognizes_corner_quotes():
    from app.generation.engine import _dialogue_voice_check

    eng, s = _session()
    p = Project(title="voice", plot_bible={"characters": [{
        "name": "许照",
        "card": {"声纹": {"自称": "我", "口头禅": ["得嘞"]}},
    }]})
    s.add(p)
    s.commit()
    check = _dialogue_voice_check(p, "许照说：「今天收租。」")
    assert check["characters"]["许照"]["lines"] == 1
    s.close()
    eng.dispose()


def test_commit_segment_rejects_empty_text():
    from app.generation.engine import _commit_segment

    eng, s = _session()
    p = Project(title="empty")
    s.add(p)
    s.flush()
    try:
        _commit_segment(s, p, "   \n  ", "summary")
    except ValueError as exc:
        assert "为空" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空正文必须被拒绝")
    assert s.query(Generation).filter_by(project_id=p.id).count() == 0
    s.close()
    eng.dispose()


class _ChatProvider:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def variant(self, **_kwargs):
        return self

    def chat(self, _system, _user, **_kwargs):
        self.calls += 1
        return self.reply


def test_empty_draft_retry_recovers_and_gives_up():
    from app.generation.engine import _retry_empty_draft

    got = _retry_empty_draft(_ChatProvider("补出来的正文"), "s", "u")
    assert got == "补出来的正文"
    assert _retry_empty_draft(_ChatProvider(""), "s", "u") == ""
