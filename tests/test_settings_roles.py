"""Settings 双角色（writer / assistant）分离测试（第 7 步交付 1 验收）。

覆盖 team-lead 派单列出的验收项：

  (a) GET /api/settings/llm 新增 `writer` / `assistant` 对象，各含九字段
      （provider/model/api_key_masked/has_key/base_url/max_tokens/timeout/
      is_fallback/label），且**原有顶层字段逐字保留**（旧前端不坏）；
  (b) PUT 带 `role` 时持久化到 `writer.*` / `assistant.*` 键、热生效
      **只重置该角色**的 provider 缓存（另一个角色的单例原样不动）；
  (c) PUT 不带 `role` 时 legacy 行为逐字不变（写 `llm.*` 键 +
      apply_runtime_config），且不接受角色模式新字段（400）；
  (d) 优先级链：显式 role 配置 > providers.yaml 默认 > 渠道 legacy 回退
      （dc39bbb 的 `_legacy_channel_field` 不破）；
  (e) 启动加载 `load_persisted_llm_config`：角色键优先、legacy 键兜底、
      writer 键镜像到 legacy 字段（Writer 单例真正热生效）；
  (f) 红线：Writer 的 max_tokens / timeout 不接受 PUT；assistant 不接受
      models 候选链；assistant 配置**绝不**镜像 legacy 字段。

不依赖真实网络：provider 工厂在空 key 下降级 LocalFallback，测试只断言
配置落点与缓存失效语义，不断言模型回复。
"""

from __future__ import annotations

import os
import sys

# ─── bootstrap ──────────────────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import (  # noqa: E402
    ASSISTANT_ROLE,
    WRITER_ROLE,
    get_provider_registry,
    reset_provider_registry,
)
from app.models import AppSetting  # noqa: E402
from app.routers import settings as settings_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

# 本文件会改动的全部 settings 字段（快照/还原，测试必须与环境无关）：
# - 角色字段（writer_* / assistant_*）
# - legacy Writer 字段（llm_provider / llm_model / llm_base_url）
# - 五渠道 key / base_url / model / models（llm_models 不是 Settings 字段——
#   候选链只按渠道存，见 config.py；快照它会让 fixture 自己 500）
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "writer_provider", "writer_api_key", "writer_base_url", "writer_model",
    "writer_max_tokens", "writer_timeout",
    "assistant_provider", "assistant_api_key", "assistant_base_url",
    "assistant_model", "assistant_max_tokens", "assistant_timeout",
    "llm_provider", "llm_model", "llm_base_url",
    "zhipu_api_key", "zhipu_base_url", "zhipu_model", "zhipu_models",
    "openrouter_api_key", "openrouter_base_url", "openrouter_model", "openrouter_models",
    "siliconflow_api_key", "siliconflow_base_url", "siliconflow_model", "siliconflow_models",
    "minimax_api_key", "minimax_base_url", "minimax_model", "minimax_models",
    "dashscope_api_key", "dashscope_base_url",
)


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例：清库 + 清双轨单例 + 清角色 settings；结束后原样还原。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    snap = {f: getattr(settings, f) for f in _SNAPSHOT_FIELDS}
    for f in _SNAPSHOT_FIELDS:
        if f.endswith(("_api_key", "_base_url")):
            setattr(settings, f, "")
    yield
    for f, v in snap.items():
        setattr(settings, f, v)
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _put(db, **kwargs) -> dict:
    """直接调 PUT router（与既有测试的调用方式一致）。"""
    body = settings_router.LlmConfigIn(**kwargs)
    return settings_router.put_llm_config(body=body, db=db)


def _get(db) -> dict:
    return settings_router.get_llm_config(db=db)


# ====================================================================
# (a) GET：双角色视图 + 旧字段逐字保留
# ====================================================================


def test_get_returns_writer_and_assistant_blocks_with_nine_fields(db_session):
    """GET 出参含 writer / assistant 两个角色对象，九字段齐全。"""
    out = _get(db_session)
    for role in ("writer", "assistant"):
        assert role in out, f"GET 出参缺 {role} 角色对象"
        block = out[role]
        for field in ("provider", "model", "api_key_masked", "has_key",
                      "base_url", "max_tokens", "timeout", "is_fallback", "label"):
            assert field in block, f"{role} 对象缺字段 {field}"
        assert isinstance(block["has_key"], bool)
        assert isinstance(block["is_fallback"], bool)


def test_get_keeps_all_legacy_top_level_fields(db_session):
    """旧前端的四个顶层字段（provider/model/keys/providers/effective）必须原样在。"""
    out = _get(db_session)
    for key in ("provider", "model", "keys", "providers", "effective"):
        assert key in out, f"GET 出参缺原有顶层字段 {key}（旧前端会坏）"
    # keys 只覆盖需要填 key 的五个渠道（local 无需 key，只在 providers 元数据里）
    # ——这是第 6 步的既有口径，逐字保留
    assert set(out["keys"]) == set(settings_router._KEY_FIELD)
    assert set(out["providers"]) == set(settings_router.PROVIDER_META)
    assert "local" in out["providers"] and "local" not in out["keys"]
    assert "label" in out["effective"] and "is_fallback" in out["effective"]


def test_get_role_block_masks_key_and_reports_has_key(db_session):
    """key 脱敏：has_key 为真时 api_key_masked 不得含完整 key。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "abcd1234efgh5678"
    settings.assistant_model = "glm-4-flash"
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    block = _get(db_session)["assistant"]
    assert block["has_key"] is True
    assert "abcd1234efgh5678" not in block["api_key_masked"]
    assert block["api_key_masked"].startswith("abcd") and block["api_key_masked"].endswith("5678")


def test_get_assistant_block_reflects_live_singleton(db_session):
    """角色对象的 model/label 取**生效单例**上的真实值，不是配置推断。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "live-singleton-key"
    settings.assistant_model = "glm-4-flash"
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    block = _get(db_session)["assistant"]
    assert block["provider"] == "zhipu"
    assert block["model"] == "glm-4-flash"
    assert block["is_fallback"] is False
    assert block["label"]  # 单例标签非空


# ====================================================================
# (b) PUT role=assistant：持久化 + 只失效 assistant 缓存
# ====================================================================


def test_put_assistant_role_persists_assistant_keys(db_session):
    """role=assistant → 写 `assistant.*` 键（不是 llm.* 单槽）。"""
    _put(db_session, provider="zhipu", role="assistant",
         api_key="assistant-key-1", model="glm-4-flash")

    assert db_session.get(AppSetting, "assistant.provider").value == "zhipu"
    assert db_session.get(AppSetting, "assistant.api_key").value == "assistant-key-1"
    assert db_session.get(AppSetting, "assistant.model").value == "glm-4-flash"
    # 双轨隔离：Assistant 的配置**绝不**写进 legacy 键
    assert db_session.get(AppSetting, "llm.provider") is None
    assert db_session.get(AppSetting, "llm.zhipu_api_key") is None
    # settings 字段同步（读取侧第 1 档 = 显式角色配置）
    assert settings.assistant_provider == "zhipu"
    assert settings.assistant_api_key == "assistant-key-1"
    assert settings.assistant_model == "glm-4-flash"


def test_put_assistant_role_invalidates_only_assistant_cache(db_session):
    """改 Assistant 只重置 assistant 单例；Writer 单例原样不动。"""
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "writer-channel-key"
    settings.zhipu_model = "glm-4-flash"
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "assistant-old-key"
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    writer_before = llm_provider_mod.get_llm_provider()
    assistant_before = llm_provider_mod.get_assistant_provider()
    assert writer_before is not assistant_before

    _put(db_session, provider="zhipu", role="assistant", api_key="assistant-new-key")

    # Writer 单例：对象不变（没被重建）
    assert llm_provider_mod._PROVIDER is writer_before
    # Assistant 单例：被 invalidate 后按**新配置**重建（不是旧对象复用，
    # 也不是停在 None——PUT 出参要回显角色视图，取单例时自然重建）
    rebuilt = llm_provider_mod.get_assistant_provider()
    assert rebuilt is not assistant_before
    assert rebuilt.api_key == "assistant-new-key"


def test_put_assistant_accepts_max_tokens_and_timeout(db_session):
    """Assistant 的 max_tokens / timeout 可配（与 Writer 红线相反，见下）。"""
    _put(db_session, provider="zhipu", role="assistant",
         api_key="k", max_tokens=2048, timeout=90)
    assert db_session.get(AppSetting, "assistant.max_tokens").value == "2048"
    assert db_session.get(AppSetting, "assistant.timeout").value == "90"
    assert settings.assistant_max_tokens == 2048
    assert settings.assistant_timeout == 90


def test_put_assistant_rejects_models_chain(db_session):
    """assistant 是单模型配置，不接受 models 候选链（400，不静默忽略）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        _put(db_session, provider="zhipu", role="assistant",
             api_key="k", models=["glm-4-flash", "glm-z1-flash"])
    assert exc.value.status_code == 400


def test_put_assistant_allows_clearing_base_url(db_session):
    """Assistant 的 base_url 可显式清空（"" = 回渠道默认，build_provider 安全）。"""
    settings.assistant_base_url = "https://assistant.example.com/v4"
    _put(db_session, provider="zhipu", role="assistant",
         api_key="k", base_url="   ")
    assert db_session.get(AppSetting, "assistant.base_url").value == ""
    assert settings.assistant_base_url == ""


# ====================================================================
# (b') PUT role=writer：持久化 + 镜像 legacy + 只失效 writer 缓存
# ====================================================================


def test_put_writer_role_persists_and_mirrors_legacy(db_session):
    """writer 角色键落库，并镜像到 llm.*（Writer 单例只读 legacy 字段）。"""
    _put(db_session, provider="zhipu", role="writer",
         api_key="writer-role-key", model="glm-4-plus")

    assert db_session.get(AppSetting, "writer.provider").value == "zhipu"
    assert db_session.get(AppSetting, "writer.api_key").value == "writer-role-key"
    assert db_session.get(AppSetting, "writer.model").value == "glm-4-plus"
    # 镜像：没有这几条，UI 显示已改而生成链路照旧（静默失效）
    assert db_session.get(AppSetting, "llm.provider").value == "zhipu"
    assert db_session.get(AppSetting, "llm.zhipu_api_key").value == "writer-role-key"
    assert db_session.get(AppSetting, "llm.model").value == "glm-4-plus"
    assert settings.llm_provider == "zhipu"
    assert settings.zhipu_api_key == "writer-role-key"
    assert settings.zhipu_model == "glm-4-plus"


def test_put_writer_role_invalidates_only_writer_cache(db_session):
    """改 Writer 只重置 writer 单例；Assistant 单例原样不动（对称隔离）。"""
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "writer-old-key"
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "assistant-keep-key"
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    writer_before = llm_provider_mod.get_llm_provider()
    assistant_before = llm_provider_mod.get_assistant_provider()

    _put(db_session, provider="zhipu", role="writer", api_key="writer-new-key")

    # Writer 单例：按新 key 重建（invalidate → 出参回显时按新 legacy 字段重建）
    rebuilt_writer = llm_provider_mod.get_llm_provider()
    assert rebuilt_writer is not writer_before
    assert rebuilt_writer.api_key == "writer-new-key"
    # Assistant 单例：原样不动（改 Writer 不得顺带重建 Assistant）
    assert llm_provider_mod.get_assistant_provider() is assistant_before


def test_put_writer_role_rejects_max_tokens_and_timeout(db_session):
    """b58c763 红线：chat timeout=120 / stream_timeout=180 不提供改写入口。"""
    from fastapi import HTTPException

    for bad in ({"max_tokens": 4096}, {"timeout": 300}):
        with pytest.raises(HTTPException) as exc:
            _put(db_session, provider="zhipu", role="writer",
                 api_key="k", **bad)
        assert exc.value.status_code == 400
        assert "b58c763" in str(exc.value.detail)


def test_put_writer_role_still_reports_singleton_values_on_get(db_session):
    """红线另一面：PUT 不接受，但 GET 的 writer 对象如实展示单例真实值。"""
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "writer-channel-key"
    llm_provider_mod.reset_provider_cache()
    reset_provider_registry()

    block = _get(db_session)["writer"]
    assert block["timeout"] == 120
    assert block["max_tokens"] == 2048  # Writer 默认（yaml 未覆写时）


# ====================================================================
# (c) legacy 模式：第 6 步行为逐字不变
# ====================================================================


def test_put_legacy_mode_writes_llm_keys_and_hot_reloads(db_session):
    """不带 role → 写 llm.* 键 + apply_runtime_config（第 6 步行为）。"""
    out = _put(db_session, provider="zhipu", api_key="legacy-key", model="glm-4-flash")

    assert db_session.get(AppSetting, "llm.provider").value == "zhipu"
    assert db_session.get(AppSetting, "llm.zhipu_api_key").value == "legacy-key"
    assert db_session.get(AppSetting, "llm.model").value == "glm-4-flash"
    assert settings.llm_provider == "zhipu"
    assert settings.zhipu_api_key == "legacy-key"
    # 不写角色键
    assert db_session.get(AppSetting, "writer.provider") is None
    assert db_session.get(AppSetting, "assistant.provider") is None
    # 出参仍是完整配置视图（旧前端照常读）
    assert out["provider"] == "zhipu"
    assert "writer" in out and "assistant" in out


def test_put_legacy_mode_rejects_role_only_fields(db_session):
    """legacy 模式携带 base_url / max_tokens / timeout → 400（不静默忽略）。"""
    from fastapi import HTTPException

    for bad in ({"base_url": "https://x.example.com"}, {"max_tokens": 100},
                {"timeout": 30}):
        with pytest.raises(HTTPException) as exc:
            _put(db_session, provider="zhipu", api_key="k", **bad)
        assert exc.value.status_code == 400
        assert "role" in str(exc.value.detail)


def test_put_legacy_mode_empty_api_key_keeps_previous(db_session):
    """legacy 口径：api_key 空 = 不改该 key（不写成空把自己锁死）。"""
    _put(db_session, provider="zhipu", api_key="first-key")
    _put(db_session, provider="zhipu", model="glm-4-plus")
    assert db_session.get(AppSetting, "llm.zhipu_api_key").value == "first-key"


def test_put_rejects_unknown_provider(db_session):
    """未知渠道 → 400（两种模式都一样）。"""
    from fastapi import HTTPException

    for kwargs in ({"provider": "not-a-channel"}, {"provider": "not-a-channel", "role": "writer"}):
        with pytest.raises(HTTPException) as exc:
            _put(db_session, api_key="k", **kwargs)
        assert exc.value.status_code == 400


# ====================================================================
# (d) 优先级链：显式 role > yaml 默认 > 渠道 legacy 回退
# ====================================================================


def test_explicit_assistant_config_beats_legacy_channel_key(db_session):
    """assistant_api_key 显式配置 → 优先于渠道 legacy key（dc39bbb 回退不被误用）。"""
    settings.zhipu_api_key = "legacy-channel-key"
    settings.zhipu_base_url = "https://legacy.example.com/v4"
    _put(db_session, provider="zhipu", role="assistant",
         api_key="explicit-assistant-key", model="glm-4-flash")

    cfg = settings.get_provider_config("assistant")
    assert cfg["api_key"] == "explicit-assistant-key"
    block = _get(db_session)["assistant"]
    assert block["has_key"] is True


def test_assistant_legacy_fallback_still_works_after_role_put(db_session):
    """角色显式留空 key 时仍走渠道回退（_legacy_channel_field 行为不破）。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""
    settings.zhipu_api_key = "legacy-channel-key"
    settings.zhipu_model = "glm-4-flash"
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    cfg = settings.get_provider_config("assistant")
    assert cfg["api_key"] == "legacy-channel-key"
    provider = llm_provider_mod.build_assistant_provider()
    assert not isinstance(provider, LocalFallbackLLMProvider), (
        "渠道回退失效 → Assistant 又降级本地兜底（模型从来没被调用）"
    )


def test_writer_role_block_uses_legacy_chain_when_role_config_empty(db_session):
    """writer_provider 为空 → writer 视图回退 legacy 生效配置（不显示空）。"""
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "writer-channel-key"
    settings.zhipu_model = "glm-4-flash"
    llm_provider_mod.reset_provider_cache()
    reset_provider_registry()

    block = _get(db_session)["writer"]
    assert block["provider"] == "zhipu"
    assert block["model"] == "glm-4-flash"
    assert block["has_key"] is True


# ====================================================================
# (e) 启动加载：角色键优先 → legacy 键 → writer 镜像
# ====================================================================


def _seed_setting(db, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value


def test_load_persisted_config_applies_role_keys_first(db_session):
    """启动加载：assistant 角色键覆盖 settings（优先级最高）。"""
    _seed_setting(db_session, "assistant.provider", "minimax")
    _seed_setting(db_session, "assistant.api_key", "startup-assistant-key")
    _seed_setting(db_session, "assistant.model", "MiniMax-M3.1-Flash-Preview")
    _seed_setting(db_session, "assistant.timeout", "45")
    db_session.commit()

    settings_router.load_persisted_llm_config(db_session)

    assert settings.assistant_provider == "minimax"
    assert settings.assistant_api_key == "startup-assistant-key"
    assert settings.assistant_model == "MiniMax-M3.1-Flash-Preview"
    assert settings.assistant_timeout == 45


def test_load_persisted_config_mirrors_writer_keys_to_legacy(db_session):
    """启动加载：writer 角色键镜像到 legacy 字段（重启后仍然生效）。"""
    _seed_setting(db_session, "writer.provider", "zhipu")
    _seed_setting(db_session, "writer.api_key", "startup-writer-key")
    _seed_setting(db_session, "writer.model", "glm-4-plus")
    db_session.commit()

    settings_router.load_persisted_llm_config(db_session)

    assert settings.writer_provider == "zhipu"
    assert settings.writer_api_key == "startup-writer-key"
    # 镜像面（Writer 单例的实际口径）
    assert settings.llm_provider == "zhipu"
    assert settings.zhipu_api_key == "startup-writer-key"
    assert settings.zhipu_model == "glm-4-plus"


def test_load_persisted_config_keeps_legacy_keys_working(db_session):
    """启动加载：没有角色键时，legacy 键照旧生效（第 6 步行为不破）。"""
    _seed_setting(db_session, "llm.provider", "zhipu")
    _seed_setting(db_session, "llm.zhipu_api_key", "legacy-startup-key")
    _seed_setting(db_session, "llm.model", "glm-4-flash")
    db_session.commit()

    settings_router.load_persisted_llm_config(db_session)

    assert settings.llm_provider == "zhipu"
    assert settings.zhipu_api_key == "legacy-startup-key"
    assert settings.zhipu_model == "glm-4-flash"


def test_load_persisted_config_ignores_dirty_int_values(db_session):
    """脏数据（max_tokens 非数字）不当成配置，保持 yaml/env 默认。"""
    _seed_setting(db_session, "assistant.max_tokens", "not-a-number")
    db_session.commit()

    settings_router.load_persisted_llm_config(db_session)

    assert settings.assistant_max_tokens == 1024  # yaml 默认，未被脏数据污染


# ====================================================================
# (f) 测试端点：带 role 的连通性测试
# ====================================================================


def test_llm_test_endpoint_with_role_local(db_session):
    """POST /api/settings/llm/test 带 role=local → ok + role 回显，不联网。"""
    out = settings_router.test_llm_config(
        body=settings_router.LlmTestIn(provider="local", role="assistant"), db=db_session
    )
    assert out["ok"] is True
    assert out["role"] == "assistant"


def test_llm_test_endpoint_role_requires_matching_channel(db_session):
    """表单渠道 ≠ 角色已存渠道 → 不拿 A 渠道的 key 测 B 渠道（回退为空）。"""
    _put(db_session, provider="zhipu", role="assistant", api_key="zhipu-key")
    out = settings_router.test_llm_config(
        body=settings_router.LlmTestIn(provider="minimax", role="assistant"),
        db=db_session,
    )
    # 不报 ok：没有 minimax 的 key 可测（zhipu 的 key 不会被借去测 minimax）
    assert out["ok"] is False
    assert "key" in out["error"]


# ====================================================================
# HTTP 层：StrictRequest / Literal 校验
# ====================================================================


def test_put_rejects_unknown_role_over_http(db_session):
    """role=admin → 422（Literal 白名单，不静默按 legacy 处理）。"""
    with TestClient(fastapi_app) as client:
        resp = client.put("/api/settings/llm", json={
            "provider": "zhipu", "role": "admin", "api_key": "k",
        })
        assert resp.status_code == 422


def test_put_rejects_extra_fields_over_http(db_session):
    """未知字段 → 422（StrictRequest，与其它端点一致）。"""
    with TestClient(fastapi_app) as client:
        resp = client.put("/api/settings/llm", json={
            "provider": "zhipu", "role": "assistant", "api_key": "k",
            "write_segment": 3,
        })
        assert resp.status_code == 422


def test_get_llm_over_http_returns_both_roles(db_session):
    """端到端：GET /api/settings/llm 200 且含双角色视图。"""
    with TestClient(fastapi_app) as client:
        resp = client.get("/api/settings/llm")
        assert resp.status_code == 200
        body = resp.json()
        assert body["writer"]["provider"]
        assert body["assistant"]["provider"]
