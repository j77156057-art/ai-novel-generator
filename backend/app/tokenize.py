"""极简中文分词（MVP 占位）：CJK 拆字 + 英文/数字词。

正式环境应替换为 jieba 等分词器以获得可用的 BM25/词袋质量；
当前实现保证离线可跑、可复现，足以验证混合检索管线。
"""


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    toks: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            buf.append(ch)
        else:
            if buf:
                toks.append("".join(buf))
                buf = []
            if "一" <= ch <= "鿿":  # CJK 统一表意区
                toks.append(ch)
    if buf:
        toks.append("".join(buf))
    return toks
