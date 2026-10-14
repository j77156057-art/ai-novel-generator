"""模型设置路由：前端「模型设置」页驱动——选渠道 / 填 key / 选模型 / 测连通。

## 双角色视图（第 7 步新增）

`GET /api/settings/llm` 在保留全部原有顶层字段（`provider` / `model` /
`keys` / `providers` / `effective`——旧前端不能坏）之外，新增 `writer` 与
`assistant` 两个角色对象，各含：

    {provider, model, api_key_masked, has_key, base_url,
     max_tokens, timeout, is_fallback, label}

这是前端「模型选择窗口」的数据基础：Writer 与 Assistant 各选各的渠道 /
key / 模型，互不覆盖。

## PUT 的两种模式

- **legacy 模式**（请求体不带 `role`）：行为与第 6 步**完全一致**——
  写 `llm.*` 键、覆盖 settings、`apply_runtime_config` 热生效。
- **角色模式**（`role="writer" | "assistant"`）：写 `writer.*` /
  `assistant.*` 键（key 形如 `writer.provider` / `assistant.api_key`），
  覆盖 settings 的 `writer_*` / `assistant_*` 字段，并**只重置该角色**的
  provider 缓存（`ProviderRegistry.invalidate(role)`）——改 Assistant
  配置不会连带重建 Writer 单例，反之亦然。

`role="writer"` 会额外把配置**镜像**到 legacy 字段（`llm.provider` /
渠道 `*_api_key` / `llm.model` …）：Writer 单例（b58c763 路径）只读
legacy 字段，不镜像则角色配置落而不生效——那会是「UI 显示已改、
生成链路照旧」的静默失效。镜像写入与读取都经过同一套
`_MODEL_FIELD` / `_KEY_FIELD` 映射，不新造平行逻辑。

Writer 的 `max_tokens` / `timeout` **不接受** PUT：b58c763 硬红线固定
chat timeout=120 / stream_timeout=180，`llm/*` 行为不变；GET 的
`writer` 对象仍如实展示单例上的真实值。

## 配置优先级（读侧，四档从高到低）

1. 显式 role 配置（`writer_*` / `assistant_*` settings 字段，由角色 PUT
   或启动加载写入）；
2. `config/providers.yaml` 默认值（settings 类字段默认值）；
3. 渠道 legacy 字段回退（`config._legacy_channel_field`，dc39bbb 实现，
   Assistant 显式留空时按渠道名借 `*_api_key` / `*_base_url`）；
4. 都没有 → LocalFallback（离线可跑，非模型质量）。

`config.Settings.get_provider_config('assistant')` 已实现 1→3 的链条，
本模块不重复实现，只负责把持久化值写进 settings（= 第 1 档）。
"""

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.llm.provider import apply_runtime_config, build_provider, get_llm_provider
from app.llm.registry import ASSISTANT_ROLE, WRITER_ROLE, get_provider_registry
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

# ------------------------------------------------------------------
# 角色（role）配置：settings 字段名与 AppSetting 键的映射
# ------------------------------------------------------------------

#: 角色配置的六个字段（与 `Settings.get_provider_config` 的返回契约一致）。
_ROLE_FIELDS: tuple[str, ...] = (
    "provider", "api_key", "base_url", "model", "max_tokens", "timeout",
)
#: 其中取整数的字段（AppSetting 里按字符串存，读回时转 int）。
_ROLE_INT_FIELDS: frozenset[str] = frozenset({"max_tokens", "timeout"})

#: 合法 role 集合（与 `app.llm.registry._VALID_ROLES` 同源，避免两处漂移）。
_ROLES: tuple[str, ...] = (WRITER_ROLE, ASSISTANT_ROLE)


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}***{key[-4:]}"


def _legacy_effective_config() -> dict[str, Any]:
    """legacy Writer 路径当前生效的配置块（writer_provider 为空时用）。

    只读 settings 的 legacy 字段（`llm_provider` + 渠道 `*_api_key` /
    `*_model` / `*_base_url`）——与 `get_llm_provider()` 的构建口径一致，
    不复制构建逻辑，只做「展示用」的取值。
    """
    provider = (settings.llm_provider or "local").strip() or "local"
    key_field = _KEY_FIELD.get(provider)
    model_field = _MODEL_FIELD.get(provider)
    base_url = (settings.llm_base_url or "").strip()
    if not base_url and provider != "local":
        base_url = str(getattr(settings, f"{provider}_base_url", "") or "").strip()
    return {
        "provider": provider,
        "api_key": str(getattr(settings, key_field, "") or "").strip() if key_field else "",
        "base_url": base_url,
        "model": str(getattr(settings, model_field, "") or "").strip() if model_field else "local-template",
        "max_tokens": settings.writer_max_tokens,
        "timeout": settings.writer_timeout,
    }


def _resolve_role_config(role: str) -> dict[str, Any]:
    """按优先级链解析某个角色的当前配置（GET / 测试端点共用）。

    优先级：显式 role 配置 > providers.yaml 默认 > 渠道 legacy 回退。

    - `assistant`：直接委托 `settings.get_provider_config('assistant')`——
      第 2/3 档（含 dc39bbb 的渠道回退）已在那个函数里实现，此处不重复。
    - `writer`：`writer_provider` 非空 → 显式角色配置（第 1 档）；
      为空 → legacy 生效配置（Writer 单例的实际口径，见
      `_legacy_effective_config`）。
    """
    if role == ASSISTANT_ROLE:
        return dict(settings.get_provider_config(ASSISTANT_ROLE))
    if role == WRITER_ROLE:
        if (settings.writer_provider or "").strip():
            return {
                "provider": settings.writer_provider,
                "api_key": settings.writer_api_key or "",
                "base_url": settings.writer_base_url or "",
                "model": settings.writer_model or "",
                "max_tokens": settings.writer_max_tokens,
                "timeout": settings.writer_timeout,
            }
        return _legacy_effective_config()
    raise ValueError(f"未知 role: {role!r}（应为 {_ROLES} 之一）")


def _active_role_provider(role: str) -> Any:
    """取该角色**当前生效**的 provider 单例（权威事实，不是配置推断）。

    writer → `get_llm_provider()`（b58c763 原单例，生成链路实际用的就是它）；
    assistant → `ProviderRegistry.get('assistant')`（与 Writer 物理隔离）。
    两个工厂按设计都不抛错（缺 key 降级 LocalFallback），但仍兜一层：
    单例万一取到，GET 也不能 500——降级为「配置视图 + is_fallback 未知」。
    """
    if role == WRITER_ROLE:
        return get_llm_provider()
    return get_provider_registry().get(ASSISTANT_ROLE)


def _role_block(role: str) -> dict[str, Any]:
    """单个角色的配置视图（GET 出参的 `writer` / `assistant` 对象）。

    取值口径：
    - `provider` 来自配置解析（角色单例不记录渠道名，只有 label）；
    - `model` / `base_url` / `api_key` / `max_tokens` / `timeout` /
      `is_fallback` / `label` **优先取生效单例上的真实值**——单例才是
      真正在跑的那一个，配置字段可能因 mixed 状态（env 手改）而与
      单例不一致，那种时候「实际在用什么」比「配置写了什么」重要。
    """
    cfg = _resolve_role_config(role)
    try:
        active = _active_role_provider(role)
    except Exception:  # noqa: BLE001 - 单例取到不影响 GET 的其余部分
        active = None

    provider = (cfg.get("provider") or "local").strip() or "local"
    if active is not None:
        api_key = str(getattr(active, "api_key", "") or "")
        model = str(getattr(active, "model", "") or cfg.get("model") or "")
        base_url = str(getattr(active, "base_url", "") or cfg.get("base_url") or "")
        max_tokens = getattr(active, "max_tokens", None)
        if max_tokens is None:
            max_tokens = cfg.get("max_tokens")
        timeout = getattr(active, "timeout", None)
        if timeout is None:
            timeout = cfg.get("timeout")
        label = getattr(active, "label", "Local")
        is_fallback = type(active).__name__ == "LocalFallbackLLMProvider"
    else:
        api_key = str(cfg.get("api_key") or "")
        model = str(cfg.get("model") or "")
        base_url = str(cfg.get("base_url") or "")
        max_tokens = cfg.get("max_tokens")
        timeout = cfg.get("timeout")
        label = "(unavailable)"
        is_fallback = True

    return {
        "provider": provider,
        "model": model,
        "api_key_masked": _mask(api_key),
        "has_key": bool(api_key),
        "base_url": base_url,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "is_fallback": is_fallback,
        "label": label,
    }


def _current(db: Session) -> dict:
    """当前生效配置 + 各渠道 key 脱敏状态（GET 用）。

    原有顶层字段（provider / model / keys / providers / effective）**逐字
    保留**——旧前端与既有测试都按这个形状读；新增的 `writer` /
    `assistant` 两个角色对象只做加法，不改变任何既有键的语义。
    """
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
        # 第 7 步：双角色视图（前端模型选择窗口的数据基础）
        "writer": _role_block(WRITER_ROLE),
        "assistant": _role_block(ASSISTANT_ROLE),
    }


class LlmConfigIn(StrictRequest):
    """PUT /api/settings/llm 请求体。

    `role` 缺省 → legacy 模式（与第 6 步完全一致：`provider` 必填，
    只写传入的字段，`api_key` 空则保留原值）。
    `role` 给定 → 角色模式：写该角色的六个字段（持久化到
    `writer.*` / `assistant.*` 键），只重置该角色的 provider 缓存。

    `base_url` / `max_tokens` / `timeout` 是角色模式的新字段；legacy
    模式下携带它们 → 400（legacy 路径从不接受这三个字段，静默忽略
    会造成「以为生效了」的假象，故 fail loud）。
    """

    provider: str
    api_key: str | None = None  # 空 = 不改该角色 key
    model: str | None = None
    models: list[str] | None = None  # 候选链（限流切换用；assistant 角色不接受）
    base_url: str | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    role: Literal["writer", "assistant"] | None = None


@router.get("/llm")
def get_llm_config(db: Session = Depends(get_db)) -> dict:
    return _current(db)


def _set(db: Session, key: str, value: str) -> None:
    """写入一个 AppSetting 键（upsert）。

    先 `flush()` 再 `get()`：同一事务里先前 `add` 的行还没发 SQL，直接
    `get` 会查不到而走 INSERT——同一个 key 两条 INSERT 撞 UNIQUE 直接 500。
    flush 不发 COMMIT，仍在同一事务内，安全。
    """
    db.flush()
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value


def _put_legacy(body: LlmConfigIn, provider: str, db: Session) -> dict:
    """legacy 模式：第 6 步行为的逐字保留（写 llm.* 键 + 全量热生效）。

    角色模式专属字段（base_url / max_tokens / timeout）在此出现即 400：
    这三个字段在第 6 步的协议里不存在（会 422），静默接受会让调用方
    以为配置生效了——而 legacy 路径的 Writer 单例根本不读它们。
    """
    if body.base_url is not None or body.max_tokens is not None or body.timeout is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "base_url / max_tokens / timeout 仅在 role 模式下有效；"
                "legacy 模式（不带 role）不接受这三个字段"
            ),
        )

    def _set_key(key: str, value: str) -> None:
        _set(db, key, value)

    # 1) 持久化（只写传入的字段；api_key 空则保留原值）
    _set_key(_DB_KEY_PROVIDER, provider)
    overrides: dict[str, str] = {"llm_provider": provider}
    if body.model:
        _set_key(_DB_KEY_MODEL, body.model.strip())
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = body.model.strip()
    if body.models is not None:
        chain = [m.strip() for m in body.models if m.strip()]
        _set_key(_DB_KEY_MODELS, ",".join(chain))
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = ",".join(chain)
    key_field = _KEY_FIELD.get(provider)
    if key_field and body.api_key:
        _set_key(f"llm.{key_field}", body.api_key.strip())
        overrides[key_field] = body.api_key.strip()
    db.commit()

    # 2) 热生效：覆盖 settings + 重置 provider 单例缓存
    apply_runtime_config(overrides)
    return _current(db)


def _put_role(body: LlmConfigIn, provider: str, role: str, db: Session) -> dict:
    """角色模式：写 `writer.*` / `assistant.*` 键 + 只重置该角色缓存。

    与 legacy 模式的三个关键差异（都是有意的）：

    1. **持久化键按角色命名**（`writer.provider` / `assistant.api_key`）——
       两个角色各存各的，不再共享 `llm.*` 单槽；
    2. **热生效只重置该角色**：`ProviderRegistry.invalidate(role)` 内部
       对 writer 调 `reset_provider_cache()`、对 assistant 调
       `reset_assistant_provider_cache()`，另一个角色的单例原样不动；
    3. **writer 额外镜像 legacy 字段**：Writer 单例（b58c763 路径）只读
       `llm_provider` / 渠道 `*_api_key` 等 legacy 字段，不镜像则角色
       配置「落而不生效」——UI 显示已改、生成链路照旧的静默失效。
       Assistant 绝不镜像 legacy（双轨隔离的底线：Assistant 的配置变化
       不得以任何形式改写 Writer 的字段）。
    """
    if body.models is not None and role != WRITER_ROLE:
        raise HTTPException(
            status_code=400,
            detail="assistant 角色是单模型配置，不接受 models 候选链（限流切换链仅 Writer 路径有）",
        )
    if role == WRITER_ROLE and (body.max_tokens is not None or body.timeout is not None):
        raise HTTPException(
            status_code=400,
            detail=(
                "Writer 的 max_tokens / timeout 由 b58c763 基线固定"
                "（chat timeout=120 / stream_timeout=180），本端点不提供改写入口；"
                "GET 的 writer 对象仍会如实展示单例上的真实值"
            ),
        )
    if body.max_tokens is not None and body.max_tokens <= 0:
        raise HTTPException(status_code=400, detail="max_tokens 必须为正整数")
    if body.timeout is not None and body.timeout <= 0:
        raise HTTPException(status_code=400, detail="timeout 必须为正整数")

    # ---- 1) 持久化角色键（只写传入的字段；api_key 空则保留原值）----
    _set(db, f"{role}.provider", provider)
    overrides: dict[str, Any] = {f"{role}_provider": provider}

    if body.model:
        model = body.model.strip()
        _set(db, f"{role}.model", model)
        overrides[f"{role}_model"] = model
    if body.api_key:
        api_key = body.api_key.strip()
        _set(db, f"{role}.api_key", api_key)
        overrides[f"{role}_api_key"] = api_key
    # base_url：Assistant 允许显式清空（"" = 回渠道默认，build_provider 对空
    # base_url 会取 settings 的渠道默认值，安全）；Writer 只覆盖非空——清空
    # 渠道 base_url 会让 Writer 单例拿到空 URL（legacy 路径没有「回默认」
    # 的读取逻辑，只有 dashscope 有 `llm_base_url or dashscope_base_url`
    # 一层回退）。
    #
    # 这里**只写一次** `{role}.base_url`：清空分支若另起一次 `_set`，同一事务
    # 里会出现两个同 key 的 pending AppSetting——`db.get` 看不到未 flush 的
    # 行，于是第二条走 INSERT，撞 UNIQUE 直接 500（实测踩过）。
    if body.base_url is not None:
        if role == ASSISTANT_ROLE or body.base_url.strip():
            base_url = body.base_url.strip()
            _set(db, f"{role}.base_url", base_url)
            overrides[f"{role}_base_url"] = base_url

    if role == ASSISTANT_ROLE:
        if body.max_tokens is not None:
            _set(db, "assistant.max_tokens", str(body.max_tokens))
            overrides["assistant_max_tokens"] = body.max_tokens
        if body.timeout is not None:
            _set(db, "assistant.timeout", str(body.timeout))
            overrides["assistant_timeout"] = body.timeout
    else:
        # ---- Writer：镜像到 legacy 字段，让 b58c763 单例真正热生效 ----
        _set(db, _DB_KEY_PROVIDER, provider)
        overrides["llm_provider"] = provider
        if body.model:
            model_field = _MODEL_FIELD.get(provider)
            if model_field:
                _set(db, _DB_KEY_MODEL, body.model.strip())
                overrides[model_field] = body.model.strip()
        if body.api_key:
            key_field = _KEY_FIELD.get(provider)
            if key_field:
                _set(db, f"llm.{key_field}", body.api_key.strip())
                overrides[key_field] = body.api_key.strip()
        if body.base_url and body.base_url.strip():
            # 只覆盖、不清空：清空渠道 base_url 会让 Writer 单例拿到空 URL
            # （legacy 路径没有「回默认」的读取逻辑，只有 dashscope 有
            # `llm_base_url or dashscope_base_url` 一层回退）。
            url = body.base_url.strip()
            if provider != "local":
                base_field = f"{provider}_base_url"
                if hasattr(settings, base_field):
                    _set(db, f"llm.{base_field}", url)
                    overrides[base_field] = url
                if provider == "dashscope":
                    _set(db, "llm.llm_base_url", url)
                    overrides["llm_base_url"] = url
        if body.models is not None:
            chain = [m.strip() for m in body.models if m.strip()]
            _set(db, _DB_KEY_MODELS, ",".join(chain))
            overrides["llm_models"] = ",".join(chain)
            if provider in _MODELS_FIELD:
                _set(db, f"llm.{_MODELS_FIELD[provider]}", ",".join(chain))
                overrides[_MODELS_FIELD[provider]] = ",".join(chain)

    db.commit()

    # ---- 2) 热生效：覆盖 settings + **只**重置该角色的 provider 缓存 ----
    # 刻意不走 apply_runtime_config：那个函数无条件 reset_provider_cache()
    # （Writer 单例），角色模式下改 Assistant 不该顺带重建 Writer。
    for field, value in overrides.items():
        if value is None:
            continue
        if hasattr(settings, field):
            setattr(settings, field, value)
    get_provider_registry().invalidate(role)
    return _current(db)


@router.put("/llm")
def put_llm_config(body: LlmConfigIn, db: Session = Depends(get_db)) -> dict:
    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        raise HTTPException(status_code=400, detail=f"未知渠道：{provider}")
    if body.role is None:
        return _put_legacy(body, provider, db)
    return _put_role(body, provider, body.role, db)


class LlmTestIn(StrictRequest):
    """POST /api/settings/llm/test 请求体。

    `role` 缺省 → legacy 行为（缺省 key/model 从 DB/.env 取）。
    `role` 给定 → 缺省 key/model/base_url 从**该角色当前生效配置**取
    （同一套优先级链）；表单渠道与角色已存渠道不一致时不回退——
    不拿 A 渠道的 key 去测 B 渠道。
    """

    provider: str
    api_key: str | None = None
    model: str | None = None
    role: Literal["writer", "assistant"] | None = None


def _local_test(model: str, role: str | None) -> dict:
    p = build_provider("local", "", model)
    t0 = time.time()
    reply = p.chat("test", "test")
    out = {
        "ok": True,
        "model": model,
        "latency_ms": int((time.time() - t0) * 1000),
        "reply": reply[:60],
    }
    if role:
        out["role"] = role
    return out


@router.post("/llm/test")
def test_llm_config(body: LlmTestIn, db: Session = Depends(get_db)) -> dict:
    """用给定（或已保存/默认）配置发一个真实小请求，验证 key 与模型可用。不落库。"""
    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        return {"ok": False, "error": f"未知渠道：{provider}"}

    if body.role is not None:
        # ---- 角色模式：缺省值从该角色当前生效配置取 ----
        cfg = _resolve_role_config(body.role)
        if (cfg.get("provider") or "") != provider:
            cfg = {}  # 表单渠道 ≠ 角色已存渠道 →不回退到别的渠道的 key
        key = (body.api_key or "").strip() or str(cfg.get("api_key") or "").strip()
        model = (
            (body.model or "").strip()
            or str(cfg.get("model") or "").strip()
            or PROVIDER_META[provider]["default_models"][0]
        )
        base_url = str(cfg.get("base_url") or "").strip()
        if provider == "local":
            return _local_test(model, body.role)
        if not key:
            return {"ok": False, "role": body.role, "error": "尚未填写 API key"}
        t0 = time.time()
        try:
            p = build_provider(provider, key, model, base_url)
            reply = p.chat("你是连通性测试助手，只做简短回复。", "请只回复两个字：正常")
            return {
                "ok": True,
                "role": body.role,
                "model": model,
                "latency_ms": int((time.time() - t0) * 1000),
                "reply": (reply or "")[:60],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "role": body.role,
                "model": model,
                "error": str(exc)[:300],
            }

    # ---- legacy 模式：第 6 步行为逐字保留 ----
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
        return _local_test(model, None)
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
    """启动时把 app_settings 里的 LLM 配置覆盖到 settings（热切换的持久化半边）。

    应用顺序（后者覆盖前者，优先级从高到低）：

    1. **角色键**（`writer.*` / `assistant.*`）——第 7 步角色 UI 写入的
       显式角色配置，是最新的用户意图；
    2. **legacy 键**（`llm.*`）——第 6 步及更早的写入，保持既有行为。

    `writer.*` 键会额外镜像到 legacy 字段（与 `_put_role` 同一套映射）：
    Writer 单例只读 legacy 字段，不镜像则重启后角色配置丢失生效。

    Assistant 键只写 `assistant_*` settings 字段，**绝不**碰 legacy——
    渠道回退由 `get_provider_config('assistant')` 在读取时完成
    （显式留空 → 按渠道名借 legacy key，dc39bbb 的行为不破）。
    """
    rows = db.execute(select(AppSetting)).scalars().all()
    stored = {r.key: r.value for r in rows}
    if not stored:
        return
    overrides: dict[str, Any] = {}

    # ---- 1) 角色键（显式角色配置，优先级最高）----
    for role in _ROLES:
        for field in _ROLE_FIELDS:
            raw = stored.get(f"{role}.{field}")
            if raw is None or raw == "":
                continue
            if field in _ROLE_INT_FIELDS:
                try:
                    overrides[f"{role}_{field}"] = int(raw)
                except ValueError:
                    continue  # 脏数据不当成配置，保持 yaml/env 默认
            else:
                overrides[f"{role}_{field}"] = raw

    # ---- 2) legacy 键（既有行为）----
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

    # ---- 3) writer 角色键镜像到 legacy（Writer 单例的实际口径）----
    writer_provider = (stored.get("writer.provider") or "").strip()
    if writer_provider:
        overrides["llm_provider"] = writer_provider
        writer_model = (stored.get("writer.model") or "").strip()
        if writer_model:
            model_field = _MODEL_FIELD.get(writer_provider)
            if model_field:
                overrides[model_field] = writer_model
        writer_key = (stored.get("writer.api_key") or "").strip()
        if writer_key:
            key_field = _KEY_FIELD.get(writer_provider)
            if key_field:
                overrides[key_field] = writer_key
        writer_base = (stored.get("writer.base_url") or "").strip()
        if writer_base and writer_provider != "local":
            base_field = f"{writer_provider}_base_url"
            if hasattr(settings, base_field):
                overrides[base_field] = writer_base
            if writer_provider == "dashscope":
                overrides["llm_base_url"] = writer_base
        writer_models = (stored.get("writer.models") or "").strip()
        if writer_models:
            overrides["llm_models"] = writer_models
            if writer_provider in _MODELS_FIELD:
                overrides[_MODELS_FIELD[writer_provider]] = writer_models

    if overrides:
        apply_runtime_config(overrides)
