from app.retrieval.bm25 import bm25_scores
from app.tokenize import tokenize


def test_chinese_words_are_not_split_into_single_characters():
    tokens = tokenize("这本书的人物关系复杂")
    assert "人物" in tokens
    assert "关系" in tokens
    assert "人" not in tokens
    assert "物" not in tokens


def test_mixed_english_and_code_identifiers_are_preserved():
    tokens = tokenize("Python 调试 tokenize_text 与 value.trim，检查 schema_version")
    assert "python" in tokens
    assert "调试" in tokens
    assert "tokenize_text" in tokens
    assert "value.trim" in tokens
    assert "schema_version" in tokens


def test_empty_and_punctuation_only_input():
    assert tokenize("") == []
    assert tokenize("，。！？") == []


def test_bm25_rejects_same_character_but_different_word_interference():
    docs = [
        "他独自走过人山人海，手里提着一件旧物。",
        "这本书的人物关系复杂，每个角色都有自己的秘密。",
        "他合上书，与同事一同离开。",
        "双方签署合同后立即生效。",
    ]
    assert max(range(len(docs)), key=lambda i: bm25_scores("人物", docs)[i]) == 1
    assert max(range(len(docs)), key=lambda i: bm25_scores("合同", docs)[i]) == 3
