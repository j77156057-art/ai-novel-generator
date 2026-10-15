"""文本解析 + 自动分章（编码不再硬编码 utf-8，改由 `encoding.decode_text` 探测）。

从 `path` 读取时走编码探测，能正确读入 GBK / GB18030 的国内网文 txt；
调用方若已持有 `content`（字符串），则直接用，不再做编码处理。
"""

import re
from typing import Optional

from app.ingestion.encoding import read_text_file

# 章/回/節/节/卷/部/篇 都作章节标记。
# 早期只认「章」，导致公版书（红楼梦、聊斋等「第X回」）一章都切不出来，
# 整篇被兜底成单章，M3 的首现章/末现章与角色章节归属随之退化。
CHAPTER_RE = re.compile(
    r"^\s*第\s*([0-9零一二三四五六七八九十百千两]+)\s*[章回節节卷部篇]"
    r"|^\s*Chapter\s+(\d+)",
    re.IGNORECASE,
)

_CN = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
# 十/百/千 累加到「当前节」；万/亿 结算整节并进位到 total
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def cn_to_int(s: str) -> Optional[int]:
    """把「十二」「一百零五」「两千零一」「一万」「一亿两千万」写成整数；无法识别返回 None。"""
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, section, cur = 0, 0, 0
    for ch in s:
        if ch in _CN:
            cur = _CN[ch]
        elif ch in _UNITS:
            unit = _UNITS[ch]
            if unit >= 10000:  # 万 / 亿：结算当前节（含未带单位的个位）并进位
                total += (section + cur) * unit
                section, cur = 0, 0
            else:  # 十 / 百 / 千：累加到当前节
                section += (cur if cur else 1) * unit
                cur = 0
        else:
            return None
    return total + section + cur


def parse_txt(path: str | None = None, content: str | None = None) -> list[dict]:
    """返回 [{"chapter_no","title","body"}, ...]。

    - 命中「第X章/回/節/节/卷/部/篇」或「Chapter N」则按章切分；
    - 首个章节标记之前的内容（版权页 / 前言）直接丢弃，不进语料；
    - 完全没命中则整篇作为单章（标题取文件名）。
    """
    if content is None:
        if path is None:
            raise ValueError("parse_txt 需要 path 或 content")
        # 编码探测：gb18030 兜底，覆盖 GBK/GB2312 等国内网文常见编码。
        content, _detected = read_text_file(path)

    chapters: list[dict] = []
    cur_no = 0
    cur_title: str | None = None
    buf: list[str] = []
    started = False
    found_any = False

    for line in content.splitlines():
        m = CHAPTER_RE.match(line)
        if m:
            found_any = True
            if started and buf:
                chapters.append(
                    {"chapter_no": cur_no, "title": cur_title, "body": "\n".join(buf).strip()}
                )
            started = True
            cur_no += 1
            cur_title = line.strip()
            buf = []
        elif started:
            buf.append(line)

    if started and buf:
        chapters.append(
            {"chapter_no": cur_no, "title": cur_title, "body": "\n".join(buf).strip()}
        )

    if not found_any:
        name = (path or "未命名").rsplit("/", 1)[-1]
        chapters = [{"chapter_no": 1, "title": name, "body": content.strip()}]

    # 入库前清洗：完本感言 + 跨书简介
    # 不清洗的话，这些非正文内容会进入 novel_chunks 表，被 RAG 检索到，
    # 污染生成上下文（模型可能学到「各位亲亲们...」这类作者话）
    from app.corpus_health import strip_finale_and_cross_novel
    for ch in chapters:
        ch["body"], _ = strip_finale_and_cross_novel(ch["body"])
    # 清洗后为空的章节丢弃
    chapters = [ch for ch in chapters if ch["body"].strip()]

    return chapters
