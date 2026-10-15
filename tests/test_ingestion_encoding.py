"""编码探测测试（第 8 步交付 1）。

覆盖：utf-8-sig（带 BOM）/ utf-8 / gbk / gb18030 的正确识别，
错误编码的可读性校验（不能「解得出但全是乱码」就接受），
以及全失败时**报错而非静默返回空串**。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.ingestion.encoding import (  # noqa: E402
    EncodingDetectionError,
    analyze_text,
    decode_text,
)

_CN = "《大奉打更人》\r\n第一章 牢狱之灾\r\n他说：这是一段中文正文，包含标点、数字 123 与英文 abc。\r\n"


# ====================================================================
# 正确解码
# ====================================================================


def test_utf8_sig_with_bom():
    raw = _CN.encode("utf-8-sig")
    text, enc = decode_text(raw, filename="bom.txt")
    assert enc == "utf-8-sig"
    assert text.lstrip("\ufeff").startswith("《大奉打更人》")
    assert "\ufeff" not in text  # BOM 已被剥离


def test_plain_utf8():
    raw = _CN.encode("utf-8")
    text, enc = decode_text(raw)
    assert enc == "utf-8"
    assert text == _CN


def test_gbk_falls_back_to_gb18030():
    """GBK 字节流：utf-8 解不出 → gb18030 命中（gb18030 是 GBK 超集）。"""
    raw = _CN.encode("gbk")
    text, enc = decode_text(raw)
    assert enc == "gb18030"
    assert text == _CN


def test_gb18030_with_char_outside_gbk():
    """含 GBK 之外、GB18030 才有的字符（如 '𠮷' U+20BB7）也能解出。"""
    text_in = "测试𠮷字：仅 GB18030 可表示的扩展汉字。\n" * 5
    raw = text_in.encode("gb18030")
    text, enc = decode_text(raw)
    assert enc == "gb18030"
    assert text == text_in
    assert "𠮷" in text


def test_empty_bytes_returns_empty_text():
    text, enc = decode_text(b"   \r\n  ")
    assert text == ""
    assert enc == "utf-8"


# ====================================================================
# 可读性校验：错误编码不得「解得出就当成功」
# ====================================================================


def test_nul_heavy_bytes_are_rejected():
    """UTF-16 风格 / 含大量 NUL 的字节流：utf-8 虽能「解码」但不可读 → 必须报错。"""
    raw = b"\x00\x01\x02\x03" * 500
    with pytest.raises(EncodingDetectionError):
        decode_text(raw, filename="utf16-ish.txt")


def test_random_latin_mojibake_rejected():
    """大量 C1/DEL 控制符（乱码特征）→ 判为不可读。"""
    raw = bytes(range(0x00, 0x20)) * 40  # 全 C0 控制符
    with pytest.raises(EncodingDetectionError):
        decode_text(raw)


def test_error_message_names_file_and_reasons():
    raw = b"\x00\x01\x02\x03" * 500
    with pytest.raises(EncodingDetectionError) as exc:
        decode_text(raw, filename="坏文件.txt")
    msg = str(exc.value)
    assert "坏文件.txt" in msg          # 带文件名
    assert "gb18030" in msg             # 带各候选
    assert "可读性" in msg or "解码失败" in msg  # 带原因


def test_analyze_text_readable_for_chinese():
    v = analyze_text(_CN)
    assert v["readable"] is True
    assert v["cjk_ratio"] > 0.25


def test_analyze_text_unreadable_for_control_heavy():
    v = analyze_text("\x00\x01\x02\x03" * 100)
    assert v["readable"] is False


def test_read_text_file_detects_gbk(tmp_path):
    """read_text_file 走同一套探测（供 load_corpus / parse_txt / seed 复用）。"""
    from app.ingestion.encoding import read_text_file

    p = tmp_path / "novel.txt"
    p.write_bytes(_CN.encode("gbk"))
    text, enc = read_text_file(str(p))
    assert enc == "gb18030"
    assert text == _CN
