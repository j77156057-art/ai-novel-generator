"""Provider 双轨隔离注册表（ADR-2026-10-12 第 2 步 / 第 5 步第 1 步）。

- 单例；按 role（writer / assistant / distiller）维度缓存 LLMProvider。
- 并发安全：async 路径走 asyncio.Lock（FastAPI 端点专用）；
  sync 路径走 threading.Lock（pytest / 同步工具用）。
  两锁各自维护，互不感知；任何时刻只有一个 role 在被构造或重建。
- 失败隔离：单个 role 的 provider 构造抛错时**绝不**写入缓存，也**绝不**复用
  另一个 role 已缓存的 provider 兜底——失败信号原样冒泡给上层。
- 失效：invalidate(role) 单 role；invalidate() 清空全部。
  Writer 失效**不会**清掉 _PROVIDER 单例本身（Writer 单例由
  app.llm.provider.reset_provider_cache() 管理，此处只清自己的角色缓存）；
  同理 Assistant 失效不动 _ASSISTANT_PROVIDER、Distiller 失效不动
  _DISTILLER_PROVIDER。

用法：
    from app.llm.registry import get_provider_registry
    writer_p   = get_provider_registry().get("writer")
    assistant  = await get_provider_registry().aget("assistant")
    distiller  = get_provider_registry().get("distiller")
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

# 注意：不直接 `from app.llm.provider import get_llm_provider`——
# 那样 registry 拿到的是写死的函数对象，测试 monkeypatch 改不到 registry 内部
# 的局部名字。让 _build 在调用时**动态查** provider 模块的属性，
# 这样测试 patch 单例工厂函数 / 失效函数都能在 registry 里即时生效。
from app.llm import provider as _provider_mod

WRITER_ROLE = "writer"
ASSISTANT_ROLE = "assistant"
#: 蒸馏模型：文风档案抽取专用角色。与 NarrativeTier 的「蒸馏档位」
#: （full / balanced / fast，采样与计算档位）不是一回事——那个在 app/style/narrative.py。
DISTILLER_ROLE = "distiller"

_VALID_ROLES = frozenset({WRITER_ROLE, ASSISTANT_ROLE, DISTILLER_ROLE})

#: 报错文案里列举的合法角色（三处 raise 共用一处定义，避免漏改）。
_ROLE_HINT = f"{WRITER_ROLE!r} / {ASSISTANT_ROLE!r} / {DISTILLER_ROLE!r}"


class ProviderRegistry:
    """单例：role → LLMProvider。

    本类被设计为「进程内全局唯一」，外部通过 get_provider_registry() 拿实例；
    不要直接 __init__()（会破坏单例不变量）。
    """

    def __init__(self) -> None:
        self._sync_lock = threading.Lock()
        # asyncio.Lock 必须首次 await 时绑定 event loop；先占位 None
        self._async_lock: asyncio.Lock | None = None
        self._cache: dict[str, Any] = {}

    # --------------------------------------------------------------
    # 内部构造器：按 role 调对应工厂；失败绝不写入缓存
    # --------------------------------------------------------------
    def _build(self, role: str) -> Any:
        # 动态查属性——pytest monkeypatch 改 provider.* 的属性时，这里跟着看到。
        if role == WRITER_ROLE:
            # Writer 单例原厂路径（不复制构建逻辑；不动 b58c763 行为）。
            return _provider_mod.get_llm_provider()
        if role == ASSISTANT_ROLE:
            # 助理侧单例入口（与 _PROVIDER 互不影响，由 reset_assistant_provider_cache 控制）。
            # 单例语义写在 app.llm.provider 里，避免在 Registry 中复制构造逻辑。
            return _provider_mod.get_assistant_provider()
        if role == DISTILLER_ROLE:
            # 蒸馏侧单例入口（独立第三轨，由 reset_distiller_provider_cache 控制）。
            return _provider_mod.get_distiller_provider()
        raise ValueError(f"未知 role: {role!r}（应为 {_ROLE_HINT}）")

    # --------------------------------------------------------------
    # sync 接口
    # --------------------------------------------------------------
    def get(self, role: str) -> Any:
        """同步获取 role 对应 provider；命中缓存则直接返回。

        失败语义：构造抛错时**不**写入缓存、**不**复用另一个 role 实例，
        异常原样往上冒。后续 get(role) 同输入会再次尝试构造（不会一直报错）。
        """
        if role not in _VALID_ROLES:
            raise ValueError(f"未知 role: {role!r}（应为 {_ROLE_HINT}）")
        with self._sync_lock:
            cached = self._cache.get(role)
            if cached is not None:
                return cached
            # 注意：构造抛错时此处同步 raise，缓存保持空——下次 get 会重试。
            provider = self._build(role)
            self._cache[role] = provider
            return provider

    def invalidate(self, role: str | None = None) -> None:
        """失效指定 role（或全部）。

        语义：
        - role=None      ：清空所有 cache + 触发三个角色双侧单例 reset
                           （让下次 get(role) 真正重建底层连接/句柄）；
        - role='writer'   ：清掉 role 自己的缓存 + 调 reset_provider_cache()
                           让 _PROVIDER 单例回到 None；
        - role='assistant'：清掉 role 自己的缓存 + 调 reset_assistant_provider_cache()
                           让 _ASSISTANT_PROVIDER 单例回到 None；
        - role='distiller'：清掉 role 自己的缓存 + 调 reset_distiller_provider_cache()
                           让 _DISTILLER_PROVIDER 单例回到 None。
        """
        with self._sync_lock:
            if role is None:
                self._cache.clear()
                # 动态查属性：测试 monkeypatch 这几个 reset 函数也能在此处即时生效
                _provider_mod.reset_provider_cache()
                _provider_mod.reset_assistant_provider_cache()
                _provider_mod.reset_distiller_provider_cache()
                return
            if role not in _VALID_ROLES:
                raise ValueError(f"未知 role: {role!r}（应为 {_ROLE_HINT}）")
            self._cache.pop(role, None)
            if role == WRITER_ROLE:
                _provider_mod.reset_provider_cache()
            elif role == ASSISTANT_ROLE:
                _provider_mod.reset_assistant_provider_cache()
            elif role == DISTILLER_ROLE:
                _provider_mod.reset_distiller_provider_cache()

    def roles_cached(self) -> list[str]:
        """返回当前已缓存的 role 列表（测试 / 状态面板用；不暴露实例句柄）。"""
        with self._sync_lock:
            return list(self._cache.keys())

    # --------------------------------------------------------------
    # async 接口（FastAPI 路由专用）
    # --------------------------------------------------------------
    async def aget(self, role: str) -> Any:
        """异步获取 role 对应 provider。

        锁在 async 层独立维护；与 sync get() 用的是同一份 _cache，但锁不同——
        因此只要锁内只做 dict.get + _build() 这一段（构造本身是同步实例化，
        不发 HTTP），并发路径下不会让两个 writer 同时被构造两次。
        """
        if role not in _VALID_ROLES:
            raise ValueError(f"未知 role: {role!r}（应为 {_ROLE_HINT}）")
        if self._async_lock is None:
            self._async_lock = asyncio.Lock()
        async with self._async_lock:
            cached = self._cache.get(role)
            if cached is not None:
                return cached
            # asyncio.Lock 持有期间不调用阻塞 I/O；构造逻辑是同步的实例化，
            # 仅在 OS 内存里 new 对象或访问 settings（不联网），符合 lock 约束。
            provider = self._build(role)
            self._cache[role] = provider
            return provider


# ====================================================================
# 模块级单例
# ====================================================================

_registry: ProviderRegistry | None = None


def get_provider_registry() -> ProviderRegistry:
    """拿全局单例。模块首次调用时构造，后续复用。

    测试代码如需隔离环境，请用 reset_provider_registry() 显式丢弃
    ——pytest fixtures 之间天然隔离，靠 autouse fixture 兜底。
    """
    global _registry
    if _registry is None:
        _registry = ProviderRegistry()
    return _registry


def reset_provider_registry() -> None:
    """丢弃全局 registry 单例；下次 get_provider_registry() 重建。

    不会触碰 _PROVIDER / _ASSISTANT_PROVIDER / _DISTILLER_PROVIDER
    （这三个由自己的 reset_*_cache 管）。
    想要全清：先 reset_provider_registry()，再 reset_provider_cache()、
    reset_assistant_provider_cache() 与 reset_distiller_provider_cache()。
    """
    global _registry
    _registry = None
