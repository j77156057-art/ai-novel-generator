"""联网搜索配置（多渠道）。

只做配置读写与连通性测试；真正的搜索编排在 `app/web_search/service.py`。
key 永不回显明文，只返回 has_key + 掩码。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppSetting
from app.schemas import StrictRequest
from app.web_search import service

router = APIRouter(prefix="/api/settings", tags=["web-search"])

#: 设置页渠道目录。`needs_key` / `default_base_url` 都在这里下发，
#: 前端不硬编码「哪家需要 key、默认地址是什么」。
_PROVIDERS = [
    {
        "value": "tavily",
        "label": "Tavily（LLM 摘要搜索）",
        "needs_key": True,
        "key_hint": "tvly-... · tavily.com 注册后每月有免费额度",
        "default_base_url": "https://api.tavily.com",
    },
    {
        "value": "brave",
        "label": "Brave Search",
        "needs_key": True,
        "key_hint": "X-Subscription-Token · brave.com/search/api",
        "default_base_url": "https://api.search.brave.com",
    },
    {
        "value": "serper",
        "label": "Serper（Google 结果）",
        "needs_key": True,
        "key_hint": "serper.dev 的 API Key（按次计费）",
        "default_base_url": "https://google.serper.dev",
    },
    {
        "value": "bocha",
        "label": "博查 Bocha（中文）",
        "needs_key": True,
        "key_hint": "bochaai.com 的 API Key（中文时效性好）",
        "default_base_url": "https://api.bochaai.com",
    },
    {
        "value": "exa",
        "label": "Exa（语义检索）",
        "needs_key": True,
        "key_hint": "exa.ai 的 API Key",
        "default_base_url": "https://api.exa.ai",
    },
    {
        "value": "searxng",
        "label": "SearXNG（自建聚合）",
        "needs_key": False,
        "key_hint": (
            "不需要 key；填自建实例地址，如 http://127.0.0.1:8080。"
            "注意：实例 settings.yml 的 search.formats 必须包含 json，否则 JSON API 会返回 403"
        ),
        "default_base_url": "",
    },
]
_PROVIDER_VALUES = {p["value"] for p in _PROVIDERS}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _set(db: Session, key: str, value: str) -> None:
    db.flush()
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def _payload(db: Session) -> dict:
    cfg = service.load_config(db)
    raw_key = cfg.api_key
    return {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "has_key": bool(raw_key),
        "api_key_masked": _mask(raw_key) if raw_key else None,
        "base_url": cfg.base_url,
        "max_results": cfg.max_results,
        "timeout": cfg.timeout,
        "safe_search": cfg.safe_search,
        "providers": _PROVIDERS,
    }


class WebSearchConfigIn(StrictRequest):
    enabled: bool | None = None
    provider: str | None = None
    #: 空/None = 保留原 key。
    api_key: str | None = None
    base_url: str | None = None
    max_results: int | None = None
    timeout: int | None = None
    safe_search: bool | None = None


class WebSearchTestIn(StrictRequest):
    query: str = "联网搜索测试"


@router.get("/web-search")
def get_web_search_config(db: Session = Depends(get_db)) -> dict:
    return _payload(db)


@router.put("/web-search")
def put_web_search_config(
    body: WebSearchConfigIn, db: Session = Depends(get_db)
) -> dict:
    if body.provider is not None:
        provider = body.provider.strip().lower()
        if provider not in _PROVIDER_VALUES | {"", "none"}:
            raise HTTPException(status_code=400, detail=f"暂不支持搜索渠道：{body.provider}")
        previous = service.load_config(db).provider
        _set(db, service.K_PROVIDER, "" if provider in {"", "none"} else provider)
        # 换渠道但没显式给端点：清掉旧渠道的地址，避免拿 Tavily 的 URL
        # 去请求 SearXNG（401/403）或拿 Brave 的地址去请求博查。
        if provider != previous and body.base_url is None:
            _set(db, service.K_BASE_URL, "")
    if body.api_key is not None and body.api_key.strip():
        _set(db, service.K_API_KEY, body.api_key.strip())
    if body.base_url is not None:
        _set(db, service.K_BASE_URL, body.base_url.strip())
    if body.max_results is not None:
        if not 1 <= body.max_results <= 30:
            raise HTTPException(status_code=400, detail="max_results 必须在 1..30 之间")
        _set(db, service.K_MAX_RESULTS, str(body.max_results))
    if body.timeout is not None:
        if not 3 <= body.timeout <= 20:
            raise HTTPException(status_code=400, detail="timeout 必须在 3..20 秒之间")
        _set(db, service.K_TIMEOUT, str(body.timeout))
    if body.safe_search is not None:
        _set(db, service.K_SAFE, "on" if body.safe_search else "off")
    if body.enabled is not None:
        _set(db, service.K_ENABLED, "on" if body.enabled else "off")
    db.commit()
    service.clear_cache()
    return _payload(db)


@router.post("/web-search/test")
def test_web_search(
    body: WebSearchTestIn, db: Session = Depends(get_db)
) -> dict:
    result = service.test_connection(db, body.query.strip() or "联网搜索测试")
    return result
