"""文本解析 + 自动分章（编码不再硬编码 utf-8，改由 `encoding.decode_text` 探测）。

从 `path` 读取时走编码探测，能正确读入 GBK / GB18030 的国内网文 txt；
调用方若已持有 `content`（字符串），则直接用，不再做编码处理。

## 分章规则（v2）

支持的标题形态：

1. **显式标记**：行首「第X章 / 回 / 節 / 节 / 卷 / 部 / 篇」或 `Chapter N`。
2. **纯数字序号**：`1、标题`、`1. 标题`、`1．标题`、`1 标题`、`01 标题`、
   `(1) 标题`、`（一）标题`。
3. **站点导出式**：`正文_001 标题` / `正文 正文_001标题`。

第 2、3 类没有「第…章」这种显式标记，正文里以数字开头的行很多，单看「行首是数字」
必然误切，因此加了一组**互相独立的判据**（全部满足才认，任一不满足即当正文）：

- **段落隔离**：该行前后都是空行。国内网文 txt 的正文段落之间普遍有空行，
  而正文里的编号列表（如「1、甲方应…」）通常嵌在段落内部，前后不留空行。
- **标题形态**：整行不超过 40 字符、汉字不超过 30 个；不以句末/句中标点收尾
  （`。！？，、；：`）；不以引号开头（排除对白行）。
- **序号严格递增**：从 1（或 2、3）起，逐个 +1；允许 1~5 的小跳号（ txt 缺章），
  以及「从 1 重新开始且上一章 ≥10」的**按卷重置**。
- **链长下限**：最终成链的候选 ≥5 条才启用数字模式；且数量必须显著多于显式
  标题（>1.5 倍），避免把正文里零散出现的「第N章」引用误当成主分章方式。

## 卷 / 章混排

同时存在「第X卷」与「第X章」时，「卷」只作卷边界（切段、丢弃卷前导言），
不单独成章；章号在输出里**连续编号**（1..N），不受各卷重置影响。
只有「卷」没有任何「章」的书（如公版译本按卷分），才把卷当章。
"""

import re
from typing import Optional

from app.ingestion.encoding import read_text_file

_CN = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
# 十/百/千 累加到「当前节」；万/亿 结算整节并进位到 total
_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}

_NUM_CHARS = "0-9零一二三四五六七八九十百千万两"

#: 显式标题：第X章/回/節/节/卷/部/篇 | Chapter N
#: 允许「第X集/卷 + 卷名 + 第Y章」的合并标题（如「第二集 众生相 第一章 名叫红雨的姑娘」）
_STRICT_RE = re.compile(
    rf"^\s*(?:(?:第\s*[{_NUM_CHARS}]+\s*[集卷部篇])[^\n第]{{0,20}}?)?"
    rf"(?:第\s*(?P<num>[{_NUM_CHARS}]+)\s*(?P<unit>[章回節节卷部篇集])"
    rf"|Chapter\s+(?P<en>\d+))"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

#: 纯数字序号标题。分隔符**必填**，否则「2019年」「3个人」这类正文会被当成标题。
_NUMERIC_RE = re.compile(
    rf"^\s*(?:[（(]\s*(?P<bn>[{_NUM_CHARS}]+)\s*[）)]"
    rf"|(?P<n>[{_NUM_CHARS}]+)\s*(?P<sep>[、.．:：]|[ \u3000]))"
    rf"\s*(?P<title>\S.*)$"
)

#: 站点导出式：正文_001xxx / 正文 正文_001xxx
_NAMED_RE = re.compile(r"^\s*(?:正文\s*)?正文[_\s]*(\d{1,4})\s*(.*)$")

#: 标题不允许以这些标点收尾（正文/对白特征）
_TITLE_TAIL_BAD = "。！？!?，、；：…—"
#: 标题不允许以这些字符开头（对白/标注特征）。
#: 注意不含括号：`(1) 标题` 这种数字序号标题本身就带括号。
_TITLE_HEAD_BAD = "“”「」『』【】"

_MAX_TITLE_CHARS = 40
_MAX_TITLE_CJK = 30

#: 以句号收尾、但仍是章节标题的形态：**阿拉伯数字章号 + 空白分隔 + 短标题**。
#: 「第230章 曹施主，有人找你。」是标题；「第三节课，是大学英语。」（无空白）、
#: 「第2000章，说实话……」（逗号分隔的作者感言）都不是。
_ARABIC_HEAD_RE = re.compile(r"^\s*第\s*\d{1,4}\s*[章回節节]\s+\S.{0,37}$")

#: 错误页判据（收窄）：**必须以整篇 HTML 文档开头**才算错误页。
#: 正文里出现 `<`、`<div>`、代码片段或引用一个网页都不满足「整章以 <html 开头」，
#: 因此不会被误杀；只有抓取失败被当成一章正文存进来的 500/502 页面才会命中。
_HTML_DOC_START_RE = re.compile(
    r"^\s*(?:<!doctype\s+html|<html[\s>])", re.IGNORECASE
)
#: 服务器错误模板的强特征：只有错误页模板才有的整块标记。
_ERR_STRONG_RE = re.compile(
    r"whitelabel\s+error\s+page"
    r"|java\.lang\.[a-z.]*exception"
    r"|<\s*h1[^>]*>\s*(?:http\s+status|error|exception)"
    r"|\b(?:nginx|tomcat|apache)\s*/?\s*\d",
    re.IGNORECASE,
)
#: 弱特征：单看不够，需与强特征或 HTML 外壳同现。
_ERR_WEAK_RE = re.compile(
    r"http\s+status\s+\d{3}"
    r"|string\s+index\s+out\s+of\s+range"
    r"|(?:type\s+)?exception\s+report"
    r"|<\s*hr\s+size\s*=",
    re.IGNORECASE,
)


def _error_page_reason(body: str) -> Optional[str]:
    """判断章节正文是否其实是抓取失败留下的 HTML/服务器错误页。

    返回 reason 字符串（`html_error_page` / `server_error_page`），正常正文返回 None。
    判据刻意收窄，宁可漏判也不误杀正文：

    - 只有**整章以 `<!DOCTYPE html` 或 `<html` 开头**（抓取页面的形态）才进入判断；
      正文里随手出现的 `<html>`、`<div>`、`a < b`、代码段都不会命中。
    - 以 `<html` 开头且含 `</html>`/`<body`，视为整篇网页（正常小说不会这样）。
    - 以 `<html` 开头且命中错误模板特征（HTTP 状态码 / Tomcat 异常 / whitelabel 等）。
    - 没有 `<html` 外壳时，只对**短章**（≤400 字符）且同时含强特征与弱特征的情况定性，
      例如 `502 Bad Gateway nginx/1.18.0` 这类纯文本错误页。
    """
    s = body.strip()
    if not s:
        return None
    html_doc = bool(_HTML_DOC_START_RE.match(s))
    weak = bool(_ERR_WEAK_RE.search(s))
    strong = bool(_ERR_STRONG_RE.search(s))
    if html_doc:
        low = s.lower()
        if "</html>" in low or "<body" in low:
            return "html_error_page"
        if strong or weak:
            return "html_error_page"
    if len(s) <= 400 and strong and weak:
        return "server_error_page"
    return None


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


def _cjk_len(s: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", s))


def _plausible_title(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > _MAX_TITLE_CHARS:
        return False
    if _cjk_len(s) > _MAX_TITLE_CJK:
        return False
    if s[-1] in _TITLE_TAIL_BAD:
        return False
    if s[0] in _TITLE_HEAD_BAD:
        return False
    return True


def _isolated(lines: list[str], i: int) -> bool:
    """该行前后是否都是空行（首行/末行视作隔离）。"""
    prev_ok = i == 0 or not lines[i - 1].strip()
    next_ok = i + 1 >= len(lines) or not lines[i + 1].strip()
    return prev_ok and next_ok


def _strict_title_ok(line: str) -> bool:
    """显式标题的宽松判定：只排除「明显是正文」的行。

    章节标题允许以 `！？…，` 结尾（「第五章 要相信科学！」很常见），所以这里
    **不套用** `_plausible_title` 的句读限制；只挡三类：
    - 以句号收尾的整句（如「第一卷完，此卷名为：世界的真相。」），
      但**保留**「第230章 曹施主，有人找你。」这种标题 —— 区别在于它有
      「阿拉伯数字章号 + 空白」的显式标记；正文里的「第三节课，是大学英语。」
      没有空白分隔，作者感言「第2000章，说实话……」用逗号分隔，都不认；
    - 超过 60 字符的行（正文段落，不是标题）；
    - 以引号/书名号开头的行（对白或引文）。
    """
    s = line.strip()
    if not s or len(s) > 60:
        return False
    if s[0] in _TITLE_HEAD_BAD:
        return False
    if s.endswith("。"):
        return bool(_ARABIC_HEAD_RE.match(s))
    return True


def _numeric_candidates(lines: list[str]) -> list[tuple[int, int, str, bool]]:
    """数字/站点式标题候选：``[(行号, 序号, 原始行, 是否段落隔离)]``。

    这里只做**形态**过滤，隔离与否交给 `_build_chain` 分级使用：
    正文后半段常因导出问题丢了空行，一刀切要求隔离会把后半本整段漏掉。
    """
    out: list[tuple[int, int, str, bool]] = []
    for i, line in enumerate(lines):
        if not _plausible_title(line):
            continue
        iso = _isolated(lines, i)
        m = _NAMED_RE.match(line)
        if m:
            out.append((i, int(m.group(1)), line, iso))
            continue
        m = _NUMERIC_RE.match(line)
        if not m:
            continue
        raw = m.group("bn") or m.group("n")
        sep = m.group("sep")
        title = (m.group("title") or "").strip()
        if not title:
            continue
        # 「3.5 倍」这类小数、以及标题以数字开头（「1 2019年…」）一律当正文
        if sep == "." and title[:1].isdigit():
            continue
        num = cn_to_int(raw)
        if num is None or num <= 0 or num > 6000:
            continue
        out.append((i, num, line, iso))
    return out


def _plain_title(line: str) -> bool:
    """大跳号才用的更严形态：**去掉序号后**标题不超过 20 个汉字。

    正文里的编号列表项（如「1、微信—点右上角…—输入：公众号」）序号后往往是一整句
    说明，长度远超 20 字；真正的章节标题很短。
    """
    s = re.sub(
        rf"^\s*(?:正文[_\s]*\d{{1,4}}|[（(]\s*[{_NUM_CHARS}]{{1,6}}\s*[）)]"
        rf"|[{_NUM_CHARS}]{{1,6}}\s*[、.．:：\s])\s*",
        "",
        line.strip(),
    )
    return _cjk_len(s) <= 20


def _build_chain(
    cands: list[tuple[int, int, str, bool]], allow_reset: bool = False
) -> list[tuple[int, int, str]]:
    """挑出严格递增的标题链（允许小跳号、按卷重置、以及后段丢失空行）。

    隔离分级：
    - 前后有空行：允许 +1~+5 的小跳号（txt 缺章）；+6~+30 的大跳号要求标题
      形态更严（短、无标点），避免把「1、微信—点右上角…」这类推广列表接进来。
    - 没有空行：只允许 +1~+3，且必须已经成链——正文里「3 个人」这类行
      很难恰好落在上一章号 +1~+3 且满足标题形态，误切概率很低。
    - 重置回 1：只在本书存在「第X卷/部/篇」时启用。真正的按卷重置必有卷标题；
      没有卷标题却从 1 重新开始，基本都是文末推广/目录，不认。
    """
    chain: list[tuple[int, int, str]] = []
    prev: Optional[int] = None
    for i, n, line, iso in cands:
        if prev is None:
            if iso and n <= 3:
                chain.append((i, n, line))
                prev = n
            continue
        if iso:
            if n == prev + 1 or 1 < n - prev <= 5:
                chain.append((i, n, line))
                prev = n
            elif 5 < n - prev <= 30 and _plain_title(line):
                chain.append((i, n, line))
                prev = n
            elif allow_reset and n == 1 and prev >= 10:
                chain.append((i, n, line))
                prev = n
        elif 1 <= n - prev <= 3:  # 后段缺空行：只认真实连号
            chain.append((i, n, line))
            prev = n
    return chain


def _split_by_headings(lines: list[str], headings: list[tuple[int, str]]) -> list[dict]:
    """按 ``[(行号, 标题行)]`` 切章，返回 ``[{chapter_no,title,body}]``。

    首个标题之前的内容（版权页/简介/前言）直接丢弃。
    """
    chapters: list[dict] = []
    for idx, (pos, title) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        body = "\n".join(lines[pos + 1:end]).strip()
        if body:
            chapters.append(
                {"chapter_no": len(chapters) + 1, "title": title.strip(), "body": body}
            )
    return chapters


def parse_txt(
    path: str | None = None,
    content: str | None = None,
    report: dict | None = None,
) -> list[dict]:
    """返回 [{"chapter_no","title","body"}, ...]。

    标题识别规则见模块文档；完全没命中则整篇作为单章（标题取文件名）。

    report：可选的输出参数。传入 dict 时写入 `parse_failed`（被判为 HTML/服务器
    错误页而**丢弃**的章节清单）与 `parse_failed_count`。错误页不是正文：它们
    是抓取失败留下的 500/502 页面，入库会污染检索与蒸馏语料，必须在这里拦掉；
    丢弃后章号重新连续编号，不留空号。
    """
    if content is None:
        if path is None:
            raise ValueError("parse_txt 需要 path 或 content")
        # 编码探测：gb18030 兜底，覆盖 GBK/GB2312 等国内网文常见编码。
        content, _detected = read_text_file(path)

    lines = content.splitlines()
    strict = []  # (行号, 序号, 单位, 原始行)
    for i, line in enumerate(lines):
        m = _STRICT_RE.match(line)
        if not m:
            continue
        if not _strict_title_ok(line):
            # 以句号收尾的整句（卷末感言）、超长正文段落，不算标题
            continue
        num = cn_to_int(m.group("num") or m.group("en"))
        if num is None:
            continue
        strict.append((i, num, (m.group("unit") or "章"), line))

    chap_heads = [(i, line) for i, _n, u, line in strict if u in "章回節节篇"]
    vol_heads = [(i, line) for i, _n, u, line in strict if u in "卷部集"]
    chap_titles = chap_heads

    numeric_chain = _build_chain(_numeric_candidates(lines), allow_reset=bool(vol_heads))

    # 模式选择（按优先级）：
    # 1) 以「卷/集」为主、且卷数不少于章数 → 按卷切（公版译本常见）；
    # 2) 数字序号链显著多于显式标题 → 数字模式（「1、标题」这类导出）；
    # 3) 有「章」→ 按章切，卷只作边界；
    # 4) 只有「卷」→ 按卷切；5) 兜底用数字链。
    mode: Optional[str]
    n_chap, n_vol, n_num = len(chap_heads), len(vol_heads), len(numeric_chain)
    if n_vol >= 3 and n_vol >= n_chap:
        mode = "vol"
    elif n_num >= 5 and n_num > max(n_chap, n_vol) * 1.5:
        mode = "numeric"
    elif n_chap >= 3:
        mode = "chap"
    elif n_chap:
        mode = "chap"
    elif n_vol:
        mode = "vol"
    elif n_num:
        mode = "numeric"
    else:
        mode = None

    if mode == "chap":
        # 卷只作卷边界：切段后丢弃卷前导言，不单独成章。
        headings = sorted(chap_heads + vol_heads)
        chapters = _split_by_headings_with_volumes(lines, headings)
    elif mode == "vol":
        chapters = _split_by_headings(lines, sorted(vol_heads))
    elif mode == "numeric":
        # 数字序号为主，但允许尾部切换成「第N章」格式（不少 txt 是拼接来的）
        merged = {(i, line) for i, _n, line in numeric_chain}
        last_pos = numeric_chain[-1][0] if numeric_chain else -1
        merged |= {(i, line) for i, line in chap_titles if i > last_pos}
        chapters = _split_by_headings(lines, sorted(merged))
    else:
        name = (path or "未命名").rsplit("/", 1)[-1]
        chapters = [{"chapter_no": 1, "title": name, "body": content.strip()}]

    # 入库前清洗：完本感言 + 跨书简介
    # 不清洗的话，这些非正文内容会进入 novel_chunks 表，被 RAG 检索到，
    # 污染生成上下文（模型可能学到「各位亲亲们...」这类作者话）
    from app.corpus_health import strip_finale_and_cross_novel
    parse_failed: list[dict] = []
    kept: list[dict] = []
    for ch in chapters:
        ch["body"], _ = strip_finale_and_cross_novel(ch["body"])
        body = ch["body"].strip()
        if not body:
            continue
        reason = _error_page_reason(body)
        if reason:
            # 抓取失败留下的 HTML/服务器错误页：丢弃，不当作正文，也不占章号
            parse_failed.append(
                {
                    "title": ch.get("title", ""),
                    "reason": reason,
                    "chars": len(body),
                    "sample": body[:120],
                }
            )
            continue
        kept.append(ch)

    # 清洗/拦截后重新连续编号（不留空号）
    for no, ch in enumerate(kept, start=1):
        ch["chapter_no"] = no
    if report is not None:
        report["parse_failed"] = parse_failed
        report["parse_failed_count"] = len(parse_failed)
    return kept


def _split_by_headings_with_volumes(
    lines: list[str], headings: list[tuple[int, str]]
) -> list[dict]:
    """卷章混排切分：卷行只作为边界，不产出章节；章行产出章节。"""
    vol_re = _STRICT_RE
    chapters: list[dict] = []
    cur_title: Optional[str] = None
    buf: list[str] = []
    started = False

    def flush() -> None:
        if started and buf:
            body = "\n".join(buf).strip()
            if body:
                chapters.append(
                    {"chapter_no": len(chapters) + 1, "title": (cur_title or "").strip(),
                     "body": body}
                )

    heading_pos = {pos for pos, _line in headings}
    for i, line in enumerate(lines):
        if i in heading_pos:
            flush()
            buf = []
            m = vol_re.match(line)
            is_vol = bool(m and (m.group("unit") in "卷部集"))
            if is_vol:
                # 卷边界：丢弃卷标题与卷前导言，等待下一个「章」
                started = False
            else:
                started = True
                cur_title = line
        elif started:
            buf.append(line)
    flush()
    return chapters
