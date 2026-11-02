"""BM25 分词：中文使用 jieba，英文与代码标识符独立保留。"""

import re

import jieba

# 英文、数字、点号 / 下划线 / 连字符连接的标识符不交给 jieba 拆开，
# 避免 tokenize_text、value.trim、schema_version 等代码查询在中英混排时退化。
_ASCII_TOKEN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_CJK_CHAR = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _chinese_tokens(text: str) -> list[str]:
    return [
        token.strip()
        for token in jieba.cut(text, cut_all=False)
        if token.strip() and _CJK_CHAR.search(token)
    ]


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens: list[str] = []
    position = 0
    for match in _ASCII_TOKEN.finditer(text):
        tokens.extend(_chinese_tokens(text[position:match.start()]))
        tokens.append(match.group())
        position = match.end()
    tokens.extend(_chinese_tokens(text[position:]))
    return tokens
