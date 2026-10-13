"""模型设置路由：前端「模型设置」页驱动——选渠道 / 填 key / 选模型 / 测连通。

- 配置持久化在 app_settings 表（键：llm.provider / llm.model / llm.models / llm.<渠道>_api_key）；
- PUT 后热生效（覆盖 settings + 重置 provider 缓存），下次 LLM 调用即用新配置；
- GET 永不返回明文 key（脱敏）；测试端点用临时配置发真实小请求，不落库。
"""

import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.llm.provider import apply_runtime_config, build_provider, get_llm_provider
from app.models import AppSetting
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 渠道元数据：供前端下拉与说明（key 获取地址 / 免费政策 / 默认候选模型）
PROVIDER_META: dict[str, dict] = {
    "zhipu": {
        "label": "智谱 GLM",
        "key_hint": "open.bigmodel.cn → 头像 → API Keys（形如 xxxxx.xxxxx）",
        "free_note": "GLM-4-Flash 永久免费不限 token；注册送 2000 万 token",
        "default_models": ["glm-4-flash", "glm-z1-flash"],
    },
    "siliconflow": {
        "label": "硅基流动",
        "key_hint": "cloud.siliconflow.cn → API Keys（形如 sk-...）",
        "free_note": "新用户送 2000 万 token；Qwen2.5-7B / GLM-4-9b 等轻量模型免费",
        "default_models": ["Qwen/Qwen2.5-7B-Instruct", "THUDM/glm-4-9b-chat"],
    },
    "minimax": {
        "label": "MiniMax",
        "key_hint": "platform.minimaxi.com → 账号管理 → 接口 Keys（形如 sk-cp-...，仅国内站可用）",
        "free_note": "实测唯一干净可用：M3.1-Flash-Preview（推理独立字段，正文不混思维链）；M3/M2.x 会把思维链混进正文",
        "default_models": ["MiniMax-M3.1-Flash-Preview"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_hint": "openrouter.ai → Keys（形如 sk-or-v1-...）",
        "free_note": "免费档账号级 50 次/日限额（所有 :free 模型共享）",
        "default_models": [
            "apodex/apodex-1.1-mini:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ],
    },
    "dashscope": {
        "label": "阿里百炼 DashScope",
        "key_hint": "bailian.console.aliyun.com → API-KEY（形如 sk-...）",
        "free_note": "新用户各模型 100 万 token 免费额度",
        "default_models": ["qwen-plus"],
    },
    "local": {
        "label": "本地兜底（无需 key）",
        "key_hint": "",
        "free_note": "确定性模板生成，离线可跑，非模型质量",
        "default_models": ["local-template"],
    },
}

# 渠道 → settings 里的 key 字段名 / model 字段名 / models(链) 字段名
_KEY_FIELD = {
    "openrouter": "openrouter_api_key",
    "zhipu": "zhipu_api_key",
    "siliconflow": "siliconflow_api_key",
    "minimax": "minimax_api_key",
    "dashscope": "dashscope_api_key",
}
_MODEL_FIELD = {
    "openrouter": "openrouter_model",
    "zhipu": "zhipu_model",
    "siliconflow": "siliconflow_model",
    "minimax": "minimax_model",
    "dashscope": "llm_model",
}
_MODELS_FIELD = {
    "openrouter": "openrouter_models",
    "zhipu": "zhipu_models",
    "siliconflow": "siliconflow_models",
    "minimax": "minimax_models",
}

_DB_KEY_PROVIDER = "llm.provider"
_DB_KEY_MODEL = "llm.model"
_DB_KEY_MODELS = "llm.models"


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}***{key[-4:]}"


def _current(db: Session) -> dict:
    """当前生效配置 + 各渠道 key 脱敏状态（GET 用）。"""
    keys_status: dict[str, dict] = {}
    for provider, field in _KEY_FIELD.items():
        env_val = getattr(settings, field, "") or ""
        db_row = db.get(AppSetting, f"llm.{field}")
        db_val = (db_row.value if db_row else "") or ""
        val = db_val or env_val
        keys_status[provider] = {"has": bool(val), "masked": _mask(val) if val else None}

    provider = settings.llm_provider or "local"
    model_field = _MODEL_FIELD.get(provider)
    model = getattr(settings, model_field, "") if model_field else "local-template"

    # 当前实际生效的 provider 单例标签
    active = get_llm_provider()
    effective = {
        "provider": provider,
        "model": model,
        "label": getattr(active, "label", "Local"),
        "is_fallback": type(active).__name__ == "LocalFallbackLLMProvider",
    }
    return {
        "provider": provider,
        "model": model,
        "keys": keys_status,
        "providers": PROVIDER_META,
        "effective": effective,
    }


class LlmConfigIn(StrictRequest):
    provider: str
    api_key: str | None = None  # 空 = 不改该渠道 key
    model: str | None = None
    models: list[str] | None = None  # 候选链（限流切换用）


@router.get("/llm")
def get_llm_config(db: Session = Depends(get_db)) -> dict:
    return _current(db)


@router.put("/llm")
def put_llm_config(body: LlmConfigIn, db: Session = Depends(get_db)) -> dict:
    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=f"未知渠道：{provider}")

    def _set(key: str, value: str) -> None:
        row = db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=value)
            db.add(row)
        else:
            row.value = value

    # 1) 持久化（只写传入的字段；api_key 空则保留原值）
    _set(_DB_KEY_PROVIDER, provider)
    overrides: dict[str, str] = {"llm_provider": provider}
    if body.model:
        _set(_DB_KEY_MODEL, body.model.strip())
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = body.model.strip()
    if body.models is not None:
        chain = [m.strip() for m in body.models if m.strip()]
        _set(_DB_KEY_MODELS, ",".join(chain))
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = ",".join(chain)
    key_field = _KEY_FIELD.get(provider)
    if key_field and body.api_key:
        _set(f"llm.{key_field}", body.api_key.strip())
        overrides[key_field] = body.api_key.strip()
    db.commit()

    # 2) 热生效：覆盖 settings + 重置 provider 单例缓存
    apply_runtime_config(overrides)
    return _current(db)


class LlmTestIn(StrictRequest):
    provider: str
    api_key: str | None = None
    model: str | None = None


@router.post("/llm/test")
def test_llm_config(body: LlmTestIn, db: Session = Depends(get_db)) -> dict:
    """用给定（或已保存/默认）配置发一个真实小请求，验证 key 与模型可用。不落库。"""
    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        return {"ok": False, "error": f"未知渠道：{provider}"}

    # 解析 key：请求体 > DB 已存 > .env
    key = (body.api_key or "").strip()
    if not key:
        key_field = _KEY_FIELD.get(provider)
        if key_field:
            row = db.get(AppSetting, f"llm.{key_field}")
            key = (row.value if row else "") or (getattr(settings, key_field, "") or "")
    model = (body.model or "").strip()
    if not model:
        model_field = _MODEL_FIELD.get(provider)
        model = (getattr(settings, model_field, "") if model_field else "") or \
            PROVIDER_META[provider]["default_models"][0]

    if provider == "local":
        p = build_provider("local", "", model)
        t0 = time.time()
        reply = p.chat("test", "test")
        return {"ok": True, "model": model, "latency_ms": int((time.time() - t0) * 1000),
                "reply": reply[:60]}
    if not key:
        return {"ok": False, "error": "尚未填写 API key"}

    t0 = time.time()
    try:
        p = build_provider(provider, key, model)
        reply = p.chat("你是连通性测试助手，只做简短回复。", "请只回复两个字：正常")
        return {
            "ok": True,
            "model": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "")[:60],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": model, "error": str(exc)[:300]}


def load_persisted_llm_config(db: Session) -> None:
    """启动时把 app_settings 里的 LLM 配置覆盖到 settings（热切换的持久化半边）。"""
    rows = db.execute(select(AppSetting)).scalars().all()
    stored = {r.key: r.value for r in rows}
    if not stored:
        return
    overrides: dict[str, str] = {}
    if stored.get(_DB_KEY_PROVIDER):
        overrides["llm_provider"] = stored[_DB_KEY_PROVIDER]
    if stored.get(_DB_KEY_MODEL):
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = stored[_DB_KEY_MODEL]
    if stored.get(_DB_KEY_MODELS):
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = stored[_DB_KEY_MODELS]
    for field in _KEY_FIELD.values():
        v = stored.get(f"llm.{field}")
        if v:
            overrides[field] = v
    if overrides:
        apply_runtime_config(overrides)
