"""接线测试：证明 provider 真的在剥推理块，而不只是 helper 写对了。

`test_reasoning_strip.py` 只验证 `app/llm/reasoning.py` 本身。这里 mock 掉
`urlopen`，让**假响应里带推理块**，再走真实的 `chat()` / `stream_chat()`，
断言输出干净——防止出现"helper 写得对但忘接上"这种最常见的假绿。

沿用 `test_provider_timeouts.py` 的 mock 方式（按 payload 里的
`"stream": true` 区分 chat / stream 响应）。
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from app.llm.provider import ZhipuChatProvider

REAL_THINK = (
    "<think>\n好的，我需要先分析用户的要求，确认每一个细节，然后逐条对照原文…"
    "这些都要检查一遍，不能漏。\n</think>\n"
)
BODY = "小杨的指甲在生锈的铁皮上划出刺耳声响。"


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


class _FakeStreamResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for line in self._payload.splitlines(keepends=True):
            yield line


def _make_urlopen(*, think: bool):
    def _fake(req, timeout=None, **kwargs):
        body = req.data or b""
        if b'"stream": true' in body:
            content = (REAL_THINK + BODY) if think else BODY
            # 逐字符切开，模拟真实 token 边界（最容易把开标签撕成两半）
            frames = b"".join(
                b'data: {"choices": [{"delta": {"content": '
                + json.dumps(ch, ensure_ascii=False).encode("utf-8")
                + b"}}]}\n\n"
                for ch in content
            ) + b"data: [DONE]\n\n"
            return _FakeStreamResp(frames)
        content = (REAL_THINK + BODY) if think else BODY
        payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        return _FakeResp(payload)

    return _fake


def _provider():
    return ZhipuChatProvider(api_key="test-key", base_url="https://example.invalid/v4",
                             model="glm-test", models=["glm-test"])


def test_chat_strips_reasoning(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=True))
    out = _provider().chat("sys", "user")
    assert "<think>" not in out and "</think>" not in out
    assert "逐条对照原文" not in out, "推理内容没剥掉"
    assert out == BODY


def test_chat_without_reasoning_unchanged(monkeypatch):
    """无推理块时**逐字不动**（别把正常文本也吃掉）。"""
    monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=False))
    assert _provider().chat("sys", "user") == BODY


def test_stream_chat_strips_reasoning(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=True))
    pieces = list(_provider().stream_chat("sys", "user"))
    joined = "".join(pieces)
    assert "<think>" not in joined and "</think>" not in joined
    assert "逐条对照原文" not in joined, "流式路径漏剥了"
    assert joined == BODY


def test_stream_chat_without_reasoning_unchanged(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=False))
    joined = "".join(_provider().stream_chat("sys", "user"))
    assert joined == BODY


def test_summarize_paths_also_strip(monkeypatch):
    """summarize / summarize_rollup 也走 _complete，应同样受益。"""
    monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=True))
    p = _provider()
    assert "<think>" not in p.summarize(BODY)
    assert "<think>" not in p.summarize_rollup([BODY, "第二段"], "L1")


@pytest.mark.parametrize("think", [True, False])
def test_generation_engine_never_persists_reasoning(monkeypatch, think):
    """端到端：走生成引擎，断言**落库的正文**里没有推理块。

    这是最贴近用户影响的一层——用户看到的是数据库里那段正文。
    """
    from app.db import SessionLocal, init_db
    from app.generation.engine import generate_next_segment
    from app.models import Generation, Project

    init_db()
    db = SessionLocal()
    try:
        proj = db.query(Project).filter(Project.id == 5).first()
        if proj is None:
            pytest.skip("库里没有 project 5，跳过端到端")
        before = db.query(Generation).filter(Generation.project_id == 5).count()
        monkeypatch.setattr(urllib.request, "urlopen", _make_urlopen(think=think))
        # 引擎在函数体内 `from app.llm.provider import get_llm_provider`，
        # 是**调用时**取属性，所以打这个补丁能生效。不打的话测试环境没有
        # api_key，会拿到 LocalFallbackLLMProvider（离线兜底），mock 根本不被调用。
        import app.llm.provider as prov_mod
        monkeypatch.setattr(prov_mod, "get_llm_provider", lambda *a, **k: _provider())
        generate_next_segment(db, proj, novel_ids=[])
        seg = (db.query(Generation).filter(Generation.project_id == 5)
               .order_by(Generation.segment_no.desc()).first())
        assert seg is not None
        assert db.query(Generation).filter(Generation.project_id == 5).count() == before + 1
        assert "<think>" not in (seg.text or "")
        assert "</think>" not in (seg.text or "")
        assert (seg.text or "").strip() == BODY
        db.rollback()
    finally:
        db.close()