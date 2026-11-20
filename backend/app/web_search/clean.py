"""搜索结果清洗：进入缓存 / 提示词之前先去掉低质条目。

Provider 只能保证「这是搜索 API 返回的 JSON」，不能保证内容有用。真实结果里
常见几类垃圾：

- 登录墙 / 验证码 / 404 / 内容已删除页；
- 站内导航、版权声明、「转载请注明」这类样板文字；
- SEO 导流文案（加微信、扫码下载、免费领取……）；
- 同一页面的多条镜像（URL 不同、标题几乎一样）；
- 一个站点霸占前几条的站内聚合页。

这里只做**确定性的文本清洗与规则剔除**：不调模型、不改写事实、不臆造结论。
剔掉的条数由 `filter_results` 返回，调用方写进 `SearchBundle.dropped`，
日志和界面就能解释「为什么只剩 N 条」。
"""
from __future__ import annotations

import html
import re
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.web_search.base import SearchResult

_TAG_RE = re.compile(r"<[^>]{1,200}>")
_SPACE_RE = re.compile(r"\s+")
_NAV_PREFIX_RE = re.compile(r"^(?:首页|主页|当前位置|您的位置)\s*[>›»/|｜]\s*")
#: 只清「明显是站点样板」的句子，不碰正文里的正常内容。
_BOILERPLATE_RE = re.compile(
    r"(?:本文(?:来自|转自|由)[^。！？\n]{0,40}[。！？]?|"
    r"原文链接[：:]\s*[^\s，。]{0,80}|"
    r"转载请注明[^。！？\n]{0,30}[。！？]?|"
    r"责任编辑[：:]\s*[^\s。]{1,16}|"
    r"免责声明[：:][^。！？\n]{0,80}[。！？]?|"
    r"更多(?:精彩|内容|资讯)请[^。！？\n]{0,30}[。！？]?)"
)
#: 标题尾部的站点名（「xxx_百度百科」「xxx - 知乎」），去掉后更容易识别重复。
_TRAILING_SITE_RE = re.compile(
    r"\s*[_\-|｜–—·]\s*(?:百度百科|百度知道|百度经验|知乎(?:专栏)?|简书|掘金|"
    r"CSDN(?:博客)?|哔哩哔哩|bilibili|搜狐(?:网)?|网易(?:号)?|新浪(?:新闻)?|"
    r"腾讯(?:新闻|网)|澎湃新闻|今日头条|豆瓣|小红书|微博|博客园|51CTO|InfoQ|少数派)\s*$",
    re.IGNORECASE,
)
#: 一眼可判的垃圾页：登录墙 / 验证 / 报错 / 导流。
_HARD_JUNK_RE = re.compile(
    r"(?:请先登录|请登录后|登录后(?:查看|阅读|下载|评论)|会员专享|付费(?:阅读|查看)|"
    r"滑动验证|安全验证|验证码|页面不存在|404\s*not\s*found|内容已(?:被)?删除|"
    r"该页面(?:无法|不存在)|访问的页面不存在|加微信|微信号[：:]|"
    r"扫码(?:关注|下载|添加)|免费领取|点击下载|一键安装|优惠券|返利|下载APP)",
    re.IGNORECASE,
)
#: 弱信号：单出现不判垃圾，两个及以上同时出现才丢（避免误伤正常文章）。
_SPAM_HINT_RE = re.compile(
    r"(?:广告|推广|招商|加盟|代购|刷单|博彩|赌场|贷款|办证|开票|私聊|加群)"
)
#: 「安全搜索」开启时额外剔除的成人/高风险内容。
#: 不用裸词「成人」（会误伤「成人礼」），只收明确指向色情的说法。
_UNSAFE_RE = re.compile(
    r"(?:色情|情色|成人视频|成人网站|成人片|裸聊|约炮|招嫖|嫖娼|av女优|"
    r"porn|xxx|nsfw|18禁|18\+)",
    re.IGNORECASE,
)
_UNSAFE_HOST_LABELS = {"porn", "xxx", "sex", "adult", "hentai", "nsfw", "18+"}
#: 结果本身就是「搜索结果页 / 导航页」时，没有引用价值。
_SEARCH_PAGE_HOSTS = {
    "google.com",
    "baidu.com",
    "bing.com",
    "sogou.com",
    "so.com",
}
_SEARCH_PAGE_PATH = re.compile(r"^/(?:search|s|web)(?:/|$)", re.IGNORECASE)
_TRACKING_PARAM_PREFIX = ("utm_", "spm", "from", "share", "ref")


def clean_text(text: str, limit: int = 600) -> str:
    """去标签/实体/导航前缀/样板句，折叠空白。

    只动「排版噪声」，不改写句子：宁可留一句多余的话，也不替作者重写摘要。
    """
    value = html.unescape(str(text or ""))
    value = _TAG_RE.sub(" ", value)
    value = value.replace("\u200b", "").replace("\ufeff", "")
    value = _NAV_PREFIX_RE.sub("", value)
    value = _BOILERPLATE_RE.sub("", value)
    value = _SPACE_RE.sub(" ", value).strip()
    return value[:limit]


def clean_title(title: str) -> str:
    value = clean_text(title, limit=200).strip(" \t\"'“”‘’《》")
    value = _TRAILING_SITE_RE.sub("", value).strip()
    return value or "（无标题）"


def normalize_url(url: str) -> str:
    """规范化 URL：去 www、去 utm/分享参数、去 fragment、统一尾斜杠。

    只用于判重，不拿它去请求；原 URL 仍按搜索结果原样返回。
    """
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PARAM_PREFIX)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme or "https", host, path, urlencode(query), ""))


def is_search_page(url: str) -> bool:
    """搜索引擎自己的结果页 / 聚合导航页：不能作为引用来源。"""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host in _SEARCH_PAGE_HOSTS and bool(_SEARCH_PAGE_PATH.match(parts.path or ""))


def _title_key(title: str) -> str:
    """近似标题判重：只留字母/数字/汉字，忽略标点与站点后缀。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())[:80]


def _is_unsafe(url: str, text: str) -> bool:
    """安全搜索的本地判据：域名标签命中，或标题/摘要出现明确的成人关键词。

    Tavily 没有 safe-search 参数，这个开关必须在本地生效，否则设置页
    只是一个不产生作用的复选框。
    """
    host = urlsplit(url).netloc.lower()
    labels = {part for part in re.split(r"[.\-]", host) if part}
    if labels & _UNSAFE_HOST_LABELS:
        return True
    return bool(_UNSAFE_RE.search(text))


def filter_results(
    items: list[SearchResult],
    *,
    max_results: int,
    per_domain: int = 2,
    safe: bool = True,
) -> tuple[list[SearchResult], int]:
    """清洗并筛掉低质结果，返回 `(保留列表, 剔除条数)`。

    - 空摘要、搜索页、登录墙/报错页、SEO 导流 → 丢；
    - URL 规范化后重复、标题近似重复 → 丢，只留第一条；
    - 同一站点最多 `per_domain` 条，避免站内聚合页霸屏；
    - `safe=True` 时额外剔成人/高风险内容（本地规则，不依赖渠道参数）；
    - 按相关性分数降序，保留前 `max_results` 条。
    """
    kept: list[SearchResult] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    domain_counts: dict[str, int] = {}
    dropped = 0

    for item in items:
        url = (item.url or "").strip()
        title = clean_title(item.title)
        snippet = clean_text(item.snippet)
        haystack = f"{title} {snippet}"

        if not url.startswith(("http://", "https://")) or is_search_page(url):
            dropped += 1
            continue
        # 没有摘要 = 模型拿不到任何可引用的内容，留 URL 只会诱发编造。
        if not snippet:
            dropped += 1
            continue
        if _HARD_JUNK_RE.search(haystack):
            dropped += 1
            continue
        if safe and _is_unsafe(url, haystack):
            dropped += 1
            continue
        if len(set(_SPAM_HINT_RE.findall(haystack))) >= 2:
            dropped += 1
            continue

        url_key = normalize_url(url)
        title_key = _title_key(title)
        if url_key in seen_urls:
            dropped += 1
            continue
        if title_key and title_key in seen_titles:
            dropped += 1
            continue

        domain = urlsplit(url_key).netloc
        if domain_counts.get(domain, 0) >= per_domain:
            dropped += 1
            continue

        seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        kept.append(replace(item, title=title, url=url, snippet=snippet))

    # 分数高的优先；没有分数（None）的排最后，同分保持原始顺序。
    kept.sort(key=lambda r: (r.score is None, -(r.score or 0.0)))
    return kept[: max(1, int(max_results))], dropped
