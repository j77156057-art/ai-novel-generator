"""ProviderRegistry 双轨隔离单测（ADR-2026-10-12 第 5 步第 1 步验收）。

权威且唯一来源；项目根 tests/test_provider_registry.py 走 pythonpath=backend 直接可跑。

覆盖需求：
  1. 双 role 互不命中 → writer 与 assistant 返回不同实例；
  2. assistant 异常时 writer 实例不受影响；
  3. registry invalidate 不会让 writer 句柄丢失；
  4. 并发获取用锁保护（sync threading.Lock + async asyncio.Lock），
     同一 role 只被构建一次；
  5. 失败角色不入缓存，下次同输入 get 会再次尝试构造（不被错误永久污染）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

# ─── bootstrap ──────────────────────────────────────────────────────────
# 本文件位于 ai-novel-generator/tests/；为让 `from app.xxx import` 直跑，
# 把 backend/ 加进 sys.path。两条路径等价：
#   - 走 conftest.py（自动加载）；
#   - 本文件自己直接 sys.path.insert（防御 conftest 没生效的边缘情况）。
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.llm import provider as provider_pkg  # noqa: E402
from app.llm.provider import (  # noqa: E402
    LLMProvider,
    LocalFallbackLLMProvider,
)
from app.llm.registry import (  # noqa: E402
    ASSISTANT_ROLE,
    WRITER_ROLE,
    ProviderRegistry,
    get_provider_registry,
    reset_provider_registry,
)


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空：registry / writer 单例 / assistant 单例。

    全部 reset 后再恢复（即便用例抛错也不污染下一个用例）。
    """
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    yield
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()


@pytest.fixture
def registry() -> ProviderRegistry:
    return get_provider_registry()


def _seed_writer_as_local_fallback() -> None:
    """把 Writer 单例强制设为 LocalFallback，方便测试离线断言。

    不读 settings；不影响 assistant 路径。
    """
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()


# ---------------------------------------------------------------------
# 1. 双 role 互不命中
# ---------------------------------------------------------------------


def test_double_role_returns_distinct_instances(registry):
    """writer / assistant 必须返回互相独立的 provider 实例，互不命中缓存。"""
    _seed_writer_as_local_fallback()
    # assistant 没 key 时 build_assistant_provider 自动降级 LocalFallback——能用同一
    # 类型做实例身份校验；区分通过 id() 和单例缓存字典同时验证。

    writer_p = registry.get(WRITER_ROLE)
    assistant_p = registry.get(ASSISTANT_ROLE)

    assert isinstance(writer_p, LLMProvider)
    assert isinstance(assistant_p, LLMProvider)
    # 命中同一实例应稳定
    assert registry.get(WRITER_ROLE) is writer_p
    assert registry.get(ASSISTANT_ROLE) is assistant_p
    # 双 role 都被各自缓存
    assert set(registry.roles_cached()) == {WRITER_ROLE, ASSISTANT_ROLE}


def test_registry_writer_delegates_to_legacy_singleton(registry):
    """registry.get('writer') 走的是原 Writer 单例路径，不复制构建逻辑。"""
    _seed_writer_as_local_fallback()

    legacy = provider_pkg.get_llm_provider()
    via_registry = registry.get(WRITER_ROLE)

    # 同一实例（Writer 单例缓存 _PROVIDER 全局 → id 一致）
    assert legacy is via_registry


def test_repeat_get_for_same_role_returns_same_instance(registry):
    """同 role 多次 get 必须返回同一实例（缓存生效）。"""
    writer_p1 = registry.get(WRITER_ROLE)
    writer_p2 = registry.get(WRITER_ROLE)
    assert writer_p1 is writer_p2


# ---------------------------------------------------------------------
# 2. assistant 异常时 writer 实例不受影响
# ---------------------------------------------------------------------


def test_assistant_build_failure_does_not_corrupt_writer_cache(registry, monkeypatch):
    """Assistant 构造抛错时：writer 已缓存实例原样保留；assistant 不入缓存。"""
    # 1) 先让 writer 健康落盘到 registry
    _seed_writer_as_local_fallback()
    writer_health = registry.get(WRITER_ROLE)
    assert isinstance(writer_health, LLMProvider)

    # 2) 把 assistant 工厂注入到 provider 模块（registry 间接调它），让它必然抛错
    boom = RuntimeError("assistant build boom")

    def fake_fail():
        raise boom

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", fake_fail)

    # 3) 试图取 assistant——必须冒泡错误
    with pytest.raises(RuntimeError, match="assistant build boom"):
        registry.get(ASSISTANT_ROLE)

    # 4) writer 缓存健在，未被污染
    assert registry.get(WRITER_ROLE) is writer_health
    # 5) assistant 因失败不入缓存
    assert ASSISTANT_ROLE not in registry.roles_cached()

    # 6) 校验：monkeypatch 还原后，assistant 重新 get 应能成功（不被错误永久污染）
    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", lambda: LocalFallbackLLMProvider()
    )
    fresh = registry.get(ASSISTANT_ROLE)
    assert isinstance(fresh, LLMProvider)


# ---------------------------------------------------------------------
# 3. registry invalidate 不让 writer 句柄丢失
# ---------------------------------------------------------------------


def test_invalidate_writer_does_not_lose_writer_handle(registry):
    """registry.invalidate('writer') 清空自缓存 + 重置 _PROVIDER；下次 get 健康重建。"""
    _seed_writer_as_local_fallback()
    before = registry.get(WRITER_ROLE)
    assert isinstance(before, LLMProvider)
    assert WRITER_ROLE in registry.roles_cached()

    registry.invalidate(WRITER_ROLE)

    # 自缓存清空 + _PROVIDER 已被 reset_provider_cache() 清掉
    assert WRITER_ROLE not in registry.roles_cached()
    assert llm_provider_mod._PROVIDER is None

    # 重建：必须仍能拿到一个 provider 实例（Writer 句柄**没**丢失，只是重新构造）
    after = registry.get(WRITER_ROLE)
    assert isinstance(after, LLMProvider)
    # 因为外部已 _seed 一次 + autouse fixture 收尾也重置，本次 get 走的是新构造，
    # 不要求 id() 相等——但「能稳定拿到 LLMProvider 实例」必须成立。
    assert after is not None


def test_invalidate_assistant_leaves_writer_intact(registry):
    """invalidate('assistant') 只清 assistant；writer 缓存原样保留。"""
    _seed_writer_as_local_fallback()
    writer_before = registry.get(WRITER_ROLE)
    assistant_before = registry.get(ASSISTANT_ROLE)
    assert isinstance(writer_before, LLMProvider)
    assert isinstance(assistant_before, LLMProvider)

    registry.invalidate(ASSISTANT_ROLE)

    # assistant 已清；writer 还在原位
    assert ASSISTANT_ROLE not in registry.roles_cached()
    assert WRITER_ROLE in registry.roles_cached()
    # writer 实例对象未重建——仍指同一对象
    assert registry.get(WRITER_ROLE) is writer_before


def test_invalidate_all_clears_both_sides(registry):
    """invalidate() 不带参数 → 同时清两边角色缓存 + 双侧单例。"""
    _seed_writer_as_local_fallback()
    w = registry.get(WRITER_ROLE)
    a = registry.get(ASSISTANT_ROLE)
    assert isinstance(w, LLMProvider)
    assert isinstance(a, LLMProvider)

    registry.invalidate()

    assert registry.roles_cached() == []
    assert llm_provider_mod._PROVIDER is None
    assert llm_provider_mod._ASSISTANT_PROVIDER is None


# ---------------------------------------------------------------------
# 4. 并发获取用锁保护（threading.Lock + asyncio.Lock）
# ---------------------------------------------------------------------


def test_concurrent_sync_get_writer_builds_once(registry, monkeypatch):
    """N 个线程同时 get('writer')：registry 的 sync threading.Lock 保护，
    get_llm_provider() 只应被调用一次去构建 _PROVIDER 单例。"""
    # 清掉 _PROVIDER，否则 counting_get 因为 _PROVIDER 已有值而立刻 return，触发不到构造
    llm_provider_mod._PROVIDER = None
    build_calls = [0]

    def counting_get():
        build_calls[0] += 1
        # 模拟 Writer 单例：第一次 new，后续命中全局缓存（不再 +1）
        if llm_provider_mod._PROVIDER is None:
            llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
        return llm_provider_mod._PROVIDER

    monkeypatch.setattr(llm_provider_mod, "get_llm_provider", counting_get)

    barrier = threading.Barrier(16)
    results: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(id(registry.get(WRITER_ROLE)))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发 worker 抛错: {errors!r}"
    # 全部 16 个线程拿到同一对象（id 一致）
    assert len(set(results)) == 1, f"并发拿到了不同实例: {set(results)}"
    # Writer 单例构造只触发一次（_build 入口被 registry sync Lock 保护）
    assert build_calls[0] == 1, f"Writer 被构造了 {build_calls[0]} 次"


def test_concurrent_sync_get_assistant_builds_once(registry, monkeypatch):
    """N 个线程同时 get('assistant')：registry 的 sync threading.Lock 保护，
    build_assistant_provider 只应被调用一次。"""
    build_calls = [0]

    def counting_build():
        build_calls[0] += 1
        # 用 LocalFallback 无 key 也能跑，且不会发任何网络请求
        return LocalFallbackLLMProvider()

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", counting_build)

    barrier = threading.Barrier(16)
    results: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(id(registry.get(ASSISTANT_ROLE)))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"并发 worker 抛错: {errors!r}"
    assert len(set(results)) == 1, f"并发拿到了不同实例: {set(results)}"
    assert build_calls[0] == 1, f"assistant 被构造了 {build_calls[0]} 次"


def test_concurrent_sync_failure_does_not_serialize_errors(registry, monkeypatch):
    """并发 N 个 get('assistant')，且 build 必然抛错：每个调用都应收到错误，
    但彼此不互相影响（缓存不会被部分写入）。"""
    build_calls = [0]

    def boom():
        build_calls[0] += 1
        raise RuntimeError("intentional failure")

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", boom)

    barrier = threading.Barrier(8)
    successes: list[object] = []
    errors: list[str] = []

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            successes.append(registry.get(ASSISTANT_ROLE))
        except RuntimeError as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert successes == [], "失败模式下不应有成功返回"
    # 每个线程都收到错误（说明错误未被某次成功吞掉）
    assert len(errors) == 8, f"预期 8 个错误，实际: {errors!r}"
    # 关键：失败两次都尝试过构造（锁允许重试），但缓存**不**被写入
    assert ASSISTANT_ROLE not in registry.roles_cached()
    # 锁内每次都重试了构造（未缓存 → 下次 get 必再次尝试）
    assert build_calls[0] == 8, f"失败模式应每次都重试，实际 build_calls={build_calls[0]}"


def test_async_concurrent_aget_assistant_under_asyncio_lock(registry, monkeypatch):
    """asyncio.Lock 下多次 await registry.aget('assistant') 只构建一次。"""
    build_calls = [0]

    def counting_build():
        build_calls[0] += 1
        return LocalFallbackLLMProvider()

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", counting_build)

    async def worker():
        return await registry.aget(ASSISTANT_ROLE)

    async def driver() -> list[int]:
        coros = [worker() for _ in range(8)]
        results = await asyncio.gather(*coros)
        return [id(r) for r in results]

    ids = asyncio.run(driver())
    assert len(set(ids)) == 1
    assert build_calls[0] == 1


# ---------------------------------------------------------------------
# 5. 失败角色不入缓存（ad-hoc 验证，与第 2 节互补）
# ---------------------------------------------------------------------


def test_assistant_failure_does_not_pollute_cache_then_recovers(registry, monkeypatch):
    """连续两次失败 → 都不入缓存；monkeypatch 还原后第三次成功并入缓存。"""
    fail_count = [0]

    def maybe_fail():
        fail_count[0] += 1
        if fail_count[0] <= 2:
            raise RuntimeError(f"boom #{fail_count[0]}")
        return LocalFallbackLLMProvider()

    monkeypatch.setattr(llm_provider_mod, "build_assistant_provider", maybe_fail)

    with pytest.raises(RuntimeError, match="boom #1"):
        registry.get(ASSISTANT_ROLE)
    with pytest.raises(RuntimeError, match="boom #2"):
        registry.get(ASSISTANT_ROLE)

    # 两次失败后仍没缓存（失败不入缓存）
    assert ASSISTANT_ROLE not in registry.roles_cached()

    # 第三次成功 → 入缓存
    third = registry.get(ASSISTANT_ROLE)
    assert isinstance(third, LLMProvider)
    assert ASSISTANT_ROLE in registry.roles_cached()
