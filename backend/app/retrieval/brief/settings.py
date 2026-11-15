"""检索工具的配置项与默认值。

所有上限、超时、预算、字数都在这里集中声明，调用点不得写死。
默认关闭，与 `author_research_enabled` 同风格：关掉时整条链路静默。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .backend import DEFAULT_USER_AGENT

_ENV_PREFIX = "BRIEF_TOOL_"
#: 后端类型白名单：auto = 配了 search_url 走 JSON，否则免费；free/json 为强制指定。
_BACKEND_CHOICES = ("auto", "free", "json")
#: `.env` 位置：backend/.env（与 app.config 读的是同一个文件）。
_DOTENV_PATH = Path(__file__).resolve().parents[3] / ".env"


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_choice(raw: str | None, default: str, allowed: tuple[str, ...]) -> str:
    value = (raw or "").strip().lower()
    return value if value in allowed else default


def _read_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 `.env` 读取，只取 `BRIEF_TOOL_` 前缀；缺失/不可读时返回空。

    不引入 python-dotenv：这里只需要 `KEY=VALUE`，注释、引号、`export` 前缀
    三件小事就够覆盖 .env 的实际写法。读不到不报错，由调用方回落到默认值。
    """
    target = path or _DOTENV_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith(_ENV_PREFIX):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class BriefSettings:
    """检索工具的开关与边界。

    `cache_dir` 相对后端进程工作目录解析；换项目可复用（键里不含项目/书籍 id）。
    """

    # 总开关：默认关。关掉时 call() 直接返回空串，不触发网络/缓存/留痕。
    # 依据：与 `author_research_enabled` 同风格，2026-11-15 用户拍板默认关闭。
    enabled: bool = False

    # 简报版本：参与缓存键；改返回格式或清洗规则时递增，旧缓存自然失效。
    # 依据：初始版本号，无历史包袱。
    brief_version: str = "v1"

    # 缓存目录（JSON 文件 + 索引），默认落在已被 .gitignore 排除的 backend/data 下。
    # 依据：相对后端进程 cwd；从 backend 启动即 backend/data/brief_cache。
    cache_dir: str = "data/brief_cache"

    # 检索后端：POST {search_url}，body={"query","count"}；空 URL 视为后端不可用。
    # 依据：未配置即不联网，保证「默认不联网」不依赖调用方自觉。
    search_url: str = ""
    search_api_key: str = ""

    # 后端类型：auto（有 search_url 用 JSON，否则免费）/ free（百度 + DDG）/ json。
    # 依据：2026-11-15 用户改走免费来源，同时保留 json 通道给以后的付费供应商。
    backend: str = "auto"
    # 免费后端全局最小请求间隔（秒）。后端实例里再兜底 max(1.0, ...)，
    # 配置写得再小也不会比 1 秒更密。
    # 待验证：1.5 秒是保守起点，未按百度的实际容忍度实测。
    min_interval_s: float = 1.5
    # 免费后端 UA。用默认 Python UA 更容易被结果页拒，先与桌面浏览器一致。
    # 待验证：具体 UA 未做 A/B 对比。
    user_agent: str = DEFAULT_USER_AGENT
    # 百度失败/被拦时是否回退 DuckDuckGo。
    # 依据：用户明确要求「失败回退」，默认开；关掉只用于对比排查。
    allow_fallback: bool = True
    # 免费后端是否改用无头浏览器（Playwright + 本地 Chromium）。
    # 依据：百度 HTTP 直连被 302 到验证码页，需要渲染后再取 DOM；默认关，
    # 因为它是可选依赖（需另装 playwright 与 Chromium 二进制），不能拖累默认路径。
    headless: bool = False

    # 每章可调用次数上限。
    # 待验证：取 3 是与目录检索软档「单段 anchor > 6 告警」同量级的保守起点，
    # 不是实测值；等真实轨迹（retrieval_logs）出来再校准。
    max_calls_per_chapter: int = 3
    # 单次检索超时（秒）。
    # 待验证：与 provider 单次请求超时同数量级，先保证检索不拖住正文生成。
    timeout_s: float = 3.0
    # 本章检索总预算（秒），跨多次调用累计。
    # 待验证：3 次 × 3s + 少量退避推出的上界，不是实测值。
    chapter_budget_s: float = 10.0
    # 单次最多返回条数（模型可在上限内请求更少）。
    # 待验证：按注入字数上限反推（3 条 × 约 130 字 ≈ 400 字），留余量。
    max_results: int = 3
    # 查询词长度上限（超出截断），防止超长 query 打爆缓存键与后端。
    # 待验证：中文检索词超过 80 字后收益递减，属经验值。
    max_query_chars: int = 80
    # 注入上下文的总字数上限。
    # 待验证：约为单段正文的 20%，避免素材块喧宾夺主。
    max_block_chars: int = 1200
    # 摘要最短字数：低于此值的条目只剩标题/栏目名，没有可用细节，直接丢弃。
    # 待验证：16 字约等于一句可用的结论；未做标注集校准。
    min_snippet_chars: int = 16
    # 查询词覆盖率下限：查询按空白/标点切词后，有多少比例的词出现在标题+摘要里。
    # 默认 0 = 一个查询词都不沾边才丢；调高会连「措辞不同但相关」的页一起丢。
    # 依据：2026-11-15 实测 cn.bing.com 把「旧城改造 征收 流程」拆成单字匹配，
    # 返回「旧」字词条与房产广告；留着会直接污染素材库与正文提示词。
    # 待验证：只按 4 条真实查询定过「0 能挡住错配、又不误伤『宋朝』这类相关页」。
    query_coverage_min: float = 0.0

    # 素材库目录；留空则取 cache_dir 的同级 `brief_library`。
    # 依据：与缓存同放 backend/data 下，已被 .gitignore 排除。
    library_dir: str = ""
    # 未使用素材的存活期（秒）。0 表示不按时间淘汰，只受条数上限约束。
    # 依据：外部检索结果的时效性没有实测依据，先按 60 天；已使用过的条目不按 TTL 淘汰。
    library_ttl_s: float = 60 * 24 * 3600.0
    # 素材库条数上限；超限按「最久未用 → 入库最旧」淘汰。
    # 待验证：200 条按每章 2~3 条估算约可覆盖 60 余章，未按真实库规模测过。
    library_max_items: int = 200
    # 每次检索后作为候选交给模型的条数上限（候选只列概要，选中的才进 prompt）。
    # 待验证：5 条是「一次能读完的候选清单」的经验值，未做标注校准。
    library_candidates: int = 5
    # 判定「正文采纳」的词面重合阈值：素材二元组被正文覆盖的比例。
    # 待验证：0.2 只做粗判；大幅改写时会漏判，宁可低估不虚报。
    adopt_overlap: float = 0.2

    # 单次检索的重试次数与退避（与 provider 的指数退避同一思路）。
    # 依据：沿用 provider「1 次原请求 + 2 次重试」的形态（_is_retryable/_backoff）。
    retries_per_call: int = 2
    # 待验证：退避基数与封顶沿用 provider 的形态，具体数值未做压测。
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 8.0

    # 近义查询判定：字符二元组 Jaccard 阈值；命中则复用缓存，不重复联网。
    # 待验证：0.9 是能容忍空白/标点差异、又不把不同问题并成一条的经验值。
    cache_similarity: float = 0.9
    # 近义匹配最多扫描的索引条数，避免缓存变大后每次全表比对。
    # 待验证：200 是性能上限，未按真实缓存规模测过。
    cache_scan_limit: int = 200
    # 缓存有效期（秒）。0 表示永不过期；版本号变更仍是硬失效手段。
    # 待验证：外部检索结果的时效性没有实测依据，先按 30 天失效。
    cache_ttl_s: float = 30 * 24 * 3600.0

    # 无素材时的返回文本（默认空串，不占上下文）。
    empty_text: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BriefSettings":
        if env is None:
            # 进程环境优先，其次 backend/.env；都没有就用默认值，不报错。
            # .env 由 app.config 加载给 pydantic，但不会写进 os.environ，
            # 所以这里自己读一遍，只取 BRIEF_TOOL_ 前缀键。
            src: Mapping[str, str] = {
                **_read_dotenv(),
                **{k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIX)},
            }
        else:
            src = env

        def get(name: str) -> str | None:
            return src.get(_ENV_PREFIX + name)

        return cls(
            enabled=_as_bool(get("ENABLED"), cls.enabled),
            brief_version=get("BRIEF_VERSION") or cls.brief_version,
            cache_dir=get("CACHE_DIR") or cls.cache_dir,
            search_url=get("SEARCH_URL") or cls.search_url,
            search_api_key=get("SEARCH_API_KEY") or cls.search_api_key,
            backend=_as_choice(get("BACKEND"), cls.backend, _BACKEND_CHOICES),
            min_interval_s=_as_float(get("MIN_INTERVAL_S"), cls.min_interval_s),
            user_agent=get("USER_AGENT") or cls.user_agent,
            allow_fallback=_as_bool(get("ALLOW_FALLBACK"), cls.allow_fallback),
            headless=_as_bool(get("HEADLESS"), cls.headless),
            max_calls_per_chapter=_as_int(get("MAX_CALLS"), cls.max_calls_per_chapter),
            timeout_s=_as_float(get("TIMEOUT_S"), cls.timeout_s),
            chapter_budget_s=_as_float(get("CHAPTER_BUDGET_S"), cls.chapter_budget_s),
            max_results=_as_int(get("MAX_RESULTS"), cls.max_results),
            max_query_chars=_as_int(get("MAX_QUERY_CHARS"), cls.max_query_chars),
            max_block_chars=_as_int(get("MAX_BLOCK_CHARS"), cls.max_block_chars),
            min_snippet_chars=_as_int(get("MIN_SNIPPET_CHARS"), cls.min_snippet_chars),
            query_coverage_min=_as_float(
                get("QUERY_COVERAGE_MIN"), cls.query_coverage_min),
            library_dir=get("LIBRARY_DIR") or cls.library_dir,
            library_ttl_s=_as_float(get("LIBRARY_TTL_S"), cls.library_ttl_s),
            library_max_items=_as_int(get("LIBRARY_MAX_ITEMS"), cls.library_max_items),
            library_candidates=_as_int(get("LIBRARY_CANDIDATES"), cls.library_candidates),
            adopt_overlap=_as_float(get("ADOPT_OVERLAP"), cls.adopt_overlap),
            retries_per_call=_as_int(get("RETRIES"), cls.retries_per_call),
            backoff_base_s=_as_float(get("BACKOFF_BASE_S"), cls.backoff_base_s),
            backoff_cap_s=_as_float(get("BACKOFF_CAP_S"), cls.backoff_cap_s),
            cache_similarity=_as_float(get("CACHE_SIMILARITY"), cls.cache_similarity),
            cache_scan_limit=_as_int(get("CACHE_SCAN_LIMIT"), cls.cache_scan_limit),
            cache_ttl_s=_as_float(get("CACHE_TTL_S"), cls.cache_ttl_s),
            empty_text=get("EMPTY_TEXT") if get("EMPTY_TEXT") is not None else cls.empty_text,
        )
