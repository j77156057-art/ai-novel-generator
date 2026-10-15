"""`finish_reason` 截断检测的回归测试。

## 要防的静默故障

实测（2026-10-14）：`max_tokens=400` 时模型返回 `finish_reason="length"`，
输出 619 字且**停在半句**。而修复前 `provider.py` 里 `finish_reason` 出现
**0 次** —— 系统不知情，半截正文当完整段落落库，故事从断句处继续。

## 关键区分（别把两件事混为一谈）

- **段长不足**：模型自己 `finish_reason=stop` 提前收手（实测 2048 上限时
  只用了 1038 token）——**不是截断，不该告警**
- **被截断**：`finish_reason=length` 撞上 max_tokens —— **必须告警**

所以下面既要有"length 会告警"的用例，也要有"stop 不告警"的反证，否则
一个无脑 WARNING 的实现也能通过测试。
"""
from __future__ import annotations

import json
import logging
import urllib.request

from app.llm.provider import ZhipuChatProvider


class _Resp:
    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._p


class _StreamResp:
    def __init__(self, payload: bytes):
        self._p = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for line in self._p.splitlines(keepends=True):
            yield line


def _provider(max_tokens=2048):
    return ZhipuChatProvider(api_key="k", base_url="https://example.invalid/v4",
                            model="glm-test", models=["glm-test"], max_tokens=max_tokens)


def _mock(finish_reason: str):
    """假响应：正文被拦腰截断 + finish_reason 由参数决定。"""
    def _fake(req, timeout=None, **kwargs):
        body = req.data or b""
        if b'"stream": true' in body:
            lines = [
                'data: {"choices": [{"delta": {"content": "\u6b63\u6587\u5f00\u5934"}}]}',
                'data: {"choices": [{"delta": {"content": "\uff0c\u7136\u540e\u534a\u53e5\u65ad\u4e86"},'
                ' "finish_reason": "%s"}]}' % finish_reason,
                "data: [DONE]",
            ]
            return _StreamResp(("\n\n".join(lines) + "\n\n").encode("utf-8"))
        payload = json.dumps({
            "choices": [{"message": {"content": "\u6b63\u6587\u5f00\u5934\uff0c\u7136\u540e\u534a\u53e5\u65ad\u4e86"},
                         "finish_reason": finish_reason}]
        }).encode("utf-8")
        return _Resp(payload)
    return _fake


# ---------------- 非流式 ----------------

def test_chat_warns_when_truncated(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", _mock("length"))
    p = _provider(max_tokens=2048)
    with caplog.at_level(logging.WARNING, logger="llm.provider"):
        p.chat("sys", "user")
    assert p.last_finish_reason == "length"
    assert any("拦腰截断" in r.getMessage() for r in caplog.records), "截断了却没有告警"


def test_chat_no_warn_when_normal_stop(monkeypatch, caplog):
    """反证：正常收尾**不告警**。否则一个无脑 WARNING 也能通过上面的测试。"""
    monkeypatch.setattr(urllib.request, "urlopen", _mock("stop"))
    p = _provider()
    with caplog.at_level(logging.WARNING, logger="llm.provider"):
        out = p.chat("sys", "user")
    assert p.last_finish_reason == "stop"
    assert out == "正文开头，然后半句断了"
    assert not [r for r in caplog.records if "拦腰截断" in r.getMessage()]


# ---------------- 流式 ----------------

def test_stream_warns_when_truncated(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", _mock("length"))
    p = _provider()
    with caplog.at_level(logging.WARNING, logger="llm.provider"):
        pieces = list(p.stream_chat("sys", "user"))
    joined = "".join(pieces)
    assert p.last_finish_reason == "length"
    assert joined == "正文开头，然后半句断了"  # 截断内容照常返回，不改行为
    assert any("拦腰截断" in r.getMessage() for r in caplog.records), "流式截断了却没有告警"


def test_stream_no_warn_when_normal_stop(monkeypatch, caplog):
    monkeypatch.setattr(urllib.request, "urlopen", _mock("stop"))
    p = _provider()
    with caplog.at_level(logging.WARNING, logger="llm.provider"):
        list(p.stream_chat("sys", "user"))
    assert p.last_finish_reason == "stop"
    assert not [r for r in caplog.records if "拦腰截断" in r.getMessage()]


def test_truncation_keeps_reasoning_strip(monkeypatch, caplog):
    """两条防护叠加：被截断 + 带推理块，两者都不许进正文。"""

    def _fake(req, timeout=None, **kwargs):
        body = req.data or b""
        content = "<think>思考被打断</think>正文开头，然后半句断了"
        if b'"stream": true' in body:
            frames = b"".join(
                b'data: {"choices": [{"delta": {"content": '
                + json.dumps(c, ensure_ascii=False).encode("utf-8")
                + b"}}]}\n\n" for c in content
            ) + b'data: {"choices": [{"delta": {}, "finish_reason": "length"}]}\n\n'
            frames += b"data: [DONE]\n\n"
            return _StreamResp(frames)
        return _Resp(json.dumps({"choices": [{
            "message": {"content": content}, "finish_reason": "length"}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _fake)
    p = _provider()
    with caplog.at_level(logging.WARNING, logger="llm.provider"):
        out = p.chat("sys", "user")
    assert out == "正文开头，然后半句断了"
    assert "<think>" not in out and "思考" not in out


# ---------------- 配置 ----------------

def test_writer_max_tokens_leaves_headroom():
    """writer.max_tokens 必须给「3000 字」目标留够余量。

    实测 glm-4-flash 中文约 1.67 字/token → 3000 字约需 1800 token。
    原值 2048 只剩 14% 余量，撞上就被拦腰截断。
    """
    import os
    import re
    here = os.path.dirname(os.path.abspath(__file__))
    yml = os.path.join(os.path.dirname(here), "backend", "config", "providers.yaml")
    src = open(yml, encoding="utf-8").read()
    writer_block = src.split("writer:")[1].split("\nassistant:")[0]
    m = re.search(r"max_tokens:\s*(\d+)", writer_block)
    assert m, "writer 块里没有 max_tokens"
    value = int(m.group(1))
    assert value >= 3000, (
        f"writer.max_tokens={value} 对 3000 字（≈1800 token）余量不足 "
        f"（需要 ≥ 3000 才有 1.6 倍以上余量）"
    )