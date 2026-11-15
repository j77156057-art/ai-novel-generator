"""正文证据基座：段落引用（服务端生成、带偏移）+ 可见输出净化 + 文本哈希。"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.body_evidence import (  # noqa: E402
    paragraph_sources,
    safe_response,
    text_hash,
)


def test_text_hash_is_sha256_and_changes_with_any_char():
    assert text_hash("abc") == text_hash("abc")
    assert len(text_hash("abc")) == 64
    assert text_hash("abc") != text_hash("abd")
    # 空文本也有稳定哈希（不能返回 None，否则指纹会静默失配）
    assert text_hash("") == text_hash("")


def test_paragraph_sources_numbers_non_empty_lines_only():
    text = "第一段\n\n第二段\n   \n第三段"
    sources = paragraph_sources(text, "generation:7", prefix="G7P")
    # 编号取**物理行号**（空行不产出来源，但占掉一个号）：2 号是「第二段」，
    # 4 号是「第三段」。这是 main 的既有口径，搬过来不改。
    assert list(sources) == ["G7P001", "G7P002", "G7P004"]
    first = sources["G7P001"]
    assert first["source_id"] == "generation:7"
    assert first["quote"] == "第一段"
    # 偏移必须是原文里的绝对位置（前端据此高亮）
    assert text[first["start"]:first["end"]] == "第一段"
    assert sources["G7P002"]["start"] > first["end"]


def test_paragraph_sources_default_prefix_and_empty_text():
    assert list(paragraph_sources("甲\n乙", "generation:1")) == ["P001", "P002"]
    assert paragraph_sources("", "generation:1") == {}


def test_safe_response_redacts_thinking_blocks():
    raw = "先说结论<think secret=\"x\">内部推理</think>然后呢"
    cleaned = safe_response(raw)
    assert "内部推理" not in cleaned
    assert "[REDACTED]" in cleaned


def test_safe_response_redacts_secrets():
    raw = (
        'Authorization: Bearer sk-abc123DEF\n'
        '{"api_key": "sk-live-9999", "password": "p@ss"}\n'
        'api_key=sk-plain-777'
    )
    cleaned = safe_response(raw)
    for secret in ("sk-abc123DEF", "sk-live-9999", "p@ss", "sk-plain-777"):
        assert secret not in cleaned, secret
    assert "auth" not in cleaned.lower() or "[REDACTED]" in cleaned


def test_safe_response_keeps_normal_text():
    raw = "第 3 段：主角在雨夜登场。"
    assert safe_response(raw) == raw


@pytest.mark.parametrize("raw", ["<analysis>思考</analysis>", "普通文本"])
def test_safe_response_is_idempotent(raw):
    once = safe_response(raw)
    assert safe_response(once) == once
