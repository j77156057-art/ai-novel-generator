"""无头浏览器后端：用 Playwright 驱动本地 Chromium，渲染结果页后从 DOM 取结果。

为什么单列一个实现：百度 HTTP 直连会被 302 到验证码页，静态 HTML 里拿不到结果。
渲染后结果节点才是真实存在的，解析也不再依赖对原始 HTML 做正则切分。

实测结论（2026-11-15）：百度对无头渲染同样返回验证码页（headless-shell 与完整
Chromium 一致），**百度这条路已收掉**，无头模式改走 `cn.bing.com`；3 个真实业务
查询均拿到可用摘要。不追加第三家源，也不做任何绕验证码/指纹伪装。

边界
----
- 是 `FreeSearchBackend` 的另一种实现，`SearchBackend` 协议与调用方都不变；
- 浏览器实例懒启动、跨查询复用，**不每次查询重启**；单线程，沿用进程级最小间隔；
- 被拦、超时、解析为空一律降级（`SearchError`，由 `BriefTool` 记 blocked/error 并返回空串），
  不向模型抛错；
- 不碰代理、登录态、验证码：抓不到就如实报抓不到。

依赖：`playwright` + Chromium，均为本地可选依赖，**不在 requirements.txt 里**。
未安装时 `search()` 落 `error` 并降级，不影响其余功能。
"""
from __future__ import annotations

import time
from typing import Callable
from urllib.parse import quote

from .backend import (
    _GLOBAL_THROTTLE,
    DEFAULT_USER_AGENT,
    FreeSearchBackend,
    SearchError,
    SearchResult,
    _blocked_reason,
)
from .render import clean_text

#: Bing 结果页 DOM（2026-11-15 实测）：`#b_results > li.b_algo`，标题在 `h2 a`。
#: 注意：渲染刚完成时 CSS 可能尚未生效，`inner_text()` 会返回空；必须退回 `text_content()`。
BING_RESULT_SELECTOR = "#b_results > li.b_algo"
BING_SNIPPET_SELECTOR = ".b_caption p, .b_lineclamp2, .b_lineclamp3, .b_algoSlug"
BING_BLOCK_MARKERS = ("captcha", "unusual traffic", "Challenge Validation", "验证码")


def _text(node) -> str:
    if node is None:
        return ""
    try:
        return (node.inner_text() or "").strip() or (node.text_content() or "").strip()
    except Exception:  # noqa: BLE001 - 节点失效时按空文本处理
        return ""


def _attr(node, name: str) -> str:
    if node is None:
        return ""
    try:
        return node.get_attribute(name) or ""
    except Exception:  # noqa: BLE001
        return ""


def extract_bing_dom(page, limit: int) -> list[SearchResult]:
    """从渲染后的 Bing 结果页取标题/摘要/链接；结构变了就返回空。"""
    out: list[SearchResult] = []
    try:
        blocks = page.query_selector_all(BING_RESULT_SELECTOR)
    except Exception:  # noqa: BLE001
        return out
    for block in blocks:
        anchor = block.query_selector("h2 a")
        if anchor is None:
            continue
        title = clean_text(_text(anchor))
        if not title:
            continue
        snippet = clean_text(_text(block.query_selector(BING_SNIPPET_SELECTOR)))
        out.append(
            SearchResult(title=title, snippet=snippet, url=_attr(anchor, "href"))
        )
        if len(out) >= limit:
            break
    return out


class BrowserSearchBackend(FreeSearchBackend):
    """无头模式：渲染 Bing 结果页，DOM 取结果。

    只替换「取页面」这一层，复用父类的 UA、最小间隔与拦截判定。
    百度在无头渲染下同样被验证码拦死，已从本后端移除，不再做无谓重试。
    """

    BING_URL = "https://cn.bing.com/search?q={q}&count={n}"

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_s: float = 3.0,
        min_interval_s: float = 1.5,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_fallback: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(
            timeout_s=timeout_s,
            min_interval_s=min_interval_s,
            user_agent=user_agent,
            allow_fallback=allow_fallback,
            clock=clock,
            sleep=sleep,
        )
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None

    # ------------------------------------------------------------------ 页面
    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """只走 Bing；失败就是失败，不再追加第二/第三家源。"""
        q = (query or "").strip()
        if not q:
            raise SearchError("查询为空", retryable=False)
        limit = max(1, int(max_results))
        return self._fetch(
            self.BING_URL.format(q=quote(q), n=limit),
            site="Bing",
            markers=BING_BLOCK_MARKERS,
            extract=extract_bing_dom,
            limit=limit,
        )

    def _fetch(
        self,
        url: str,
        *,
        site: str,
        markers: tuple[str, ...],
        extract: Callable[[object, int], list[SearchResult]],
        limit: int,
    ) -> list[SearchResult]:
        """渲染一个结果页并提取条目；失败一律抛 `SearchError`，由调用方降级。"""
        _GLOBAL_THROTTLE.wait(self.min_interval_s, self._clock, self._sleep)
        page = self._new_page()
        try:
            page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=max(1, int(self.timeout_s * 1000)),
            )
            reason = _blocked_reason(page.content() or "", page.url or "", markers)
            if reason:
                raise SearchError(f"{site}验证/拦截: {reason}", blocked=True)
            results = extract(page, limit)
            if not results:
                raise SearchError(f"{site}空结果页（DOM 解析为空）", blocked=True)
            return results
        except SearchError:
            raise
        except Exception as exc:  # noqa: BLE001 - 超时/浏览器异常统一降级
            raise SearchError(f"{site}渲染失败: {exc}", retryable=True) from exc
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def _new_page(self):
        if self._browser is None or not self._browser.is_connected():
            self._start()
        try:
            return self._context.new_page()
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise SearchError(f"浏览器页面创建失败: {exc}", retryable=True) from exc

    def _start(self) -> None:
        """懒启动：一个 Chromium 实例跨查询复用，不每次查询重启。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - 依赖缺失时明确降级
            raise SearchError(
                f"playwright 不可用（未安装则跳过无头模式）: {exc}", retryable=False
            ) from exc

        self.close()
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=self.headless)
            context = browser.new_context(
                user_agent=self.user_agent,
                locale="zh-CN",
                viewport={"width": 1280, "height": 900},
            )
        except Exception as exc:  # noqa: BLE001 - 浏览器二进制缺失也走这里
            try:
                pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise SearchError(f"Chromium 启动失败: {exc}", retryable=False) from exc
        self._pw, self._browser, self._context = pw, browser, context

    def close(self) -> None:
        """释放浏览器与驱动；进程退出前调用与否都不影响正确性。"""
        for obj in (self._context, self._browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:  # noqa: BLE001
                    pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._context = self._browser = self._pw = None
