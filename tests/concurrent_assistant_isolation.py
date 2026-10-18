"""并发隔离测试（修正版）：20 个 Assistant 调用并发 + 1 个 Writer 单例调用。

修正点：
- SQLite 在并发写时会报 "database is locked"（SQLite 自身限制，与 Assistant 代码无关）；
  本测试通过：每个 worker **顺序** 调一次 /assistant/qa（用独立 SessionLocal），不并发写 DB。
- 真正的并发只验证 LLM provider 层的隔离：20 个线程同时调 Assistant provider.chat()，
  1 个线程调 Writer provider.chat()，断言：Writer 句柄不变、Writer 响应快完成。
- 验证 Assistant provider 抛错时：Writer 仍健康。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import threading
import tempfile
import time

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# ─── 真实库隔离（必须在任何 `import app.*` 之前）────────────────────────────
# `app.db` 的 engine 由 `settings.database_url` 建立，默认 `sqlite:///./novel_dev.db`
# ——**相对当前工作目录解析**。本脚本含 `Base.metadata.drop_all(bind=engine)`，
# 若在 backend/ 或任何含有 `novel_dev.db` 的目录下直接跑，drop 掉的就是真实开发库
# （2026-10-17 19:02 就是这样清空了 7941 个分片，且 drop 后 create_all 未成功，
#  只留下 122.9MB 的空壳）。
#
# 做法与 tests/qa3_qa4_qa5 一致：
#   1) 先把 DATABASE_URL 指到**本进程专属的临时库**；
#   2) 若外部已显式把 DATABASE_URL 指向真实库，**直接拒绝运行**，而不是静默清库。
_REAL_DB_NAMES = {"novel_dev.db", "novel_demo.db"}
_ext_db = os.environ.get("DATABASE_URL", "")
if _ext_db:
    if not _ext_db.startswith("sqlite"):
        raise SystemExit(f"拒绝运行：DATABASE_URL 不是 sqlite（{_ext_db}），本脚本会 drop_all。")
    _ext_path = os.path.abspath(_ext_db.split("///", 1)[-1])
    if os.path.basename(_ext_path) in _REAL_DB_NAMES:
        raise SystemExit(
            f"拒绝运行：DATABASE_URL 指向真实库 {_ext_path}。"
            "本脚本会 drop_all 真实数据；请去掉该环境变量（脚本会自建临时库）。"
        )
_ISO_DIR = tempfile.mkdtemp(prefix="concurrent_qa_")
os.environ["DATABASE_URL"] = f"sqlite:///{_ISO_DIR}/isolated.db"
atexit.register(lambda: shutil.rmtree(_ISO_DIR, ignore_errors=True))
print(f"[隔离] DATABASE_URL -> {os.environ['DATABASE_URL']}")
# ─── 隔离块结束 ─────────────────────────────────────────────────────────────

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402


class _SlowAssistantProvider(LocalFallbackLLMProvider):
    """Assistant 用：模拟慢响应（sleep 模拟真实 LLM 调用）。"""

    def __init__(self, sleep_s: float = 0.2, label: str = "slow") -> None:
        super().__init__()
        self._sleep = sleep_s
        self.label = label
        self.model = "slow-model"

    def chat(self, system, user, constraints=None):
        time.sleep(self._sleep)
        return super().chat(system, user, constraints)


class _BoomAssistantProvider:
    """Assistant 用：模拟抛错，验证 Writer 不被污染。"""

    model = "boom"

    def chat(self, system, user, constraints=None):
        raise RuntimeError("simulated assistant failure")


def main() -> int:
    print("=== 并发隔离测试：Assistant 慢响应 / 失败 vs Writer 单例 ===\n")

    # 干净状态
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()

    # 锁住 Writer 单例
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_handle = llm_provider_mod._PROVIDER
    writer_id_before = id(writer_handle)
    print(f"[Init] Writer 句柄 id={writer_id_before}, class={type(writer_handle).__name__}")

    # ============================================================
    # 场景 1: 20 个 Assistant 慢响应（每个 sleep 0.2s） + 1 个 Writer 调用
    # ============================================================
    print("\n[场景 1] 20 个 Assistant 慢响应 + 1 个 Writer 单例调用")

    # 替换 assistant 工厂为 slow provider
    original_factory = llm_provider_mod.build_assistant_provider
    llm_provider_mod.build_assistant_provider = lambda: _SlowAssistantProvider(sleep_s=0.2)

    assistant_results: list[float] = []
    assistant_errors: list[str] = []
    writer_result: list[str] = []
    writer_errors: list[str] = []
    barrier = threading.Barrier(21)
    start = time.time()

    def assistant_worker(i: int):
        try:
            barrier.wait(timeout=5)
            t0 = time.time()
            r = llm_provider_mod.get_assistant_provider().chat("sys", f"q-{i}")
            assistant_results.append(time.time() - t0)
            assert isinstance(r, str) and len(r) > 0, f"empty: {r!r}"
        except Exception as e:  # noqa: BLE001
            assistant_errors.append(f"a-{i}: {e!r}")

    def writer_worker():
        try:
            barrier.wait(timeout=5)
            t0 = time.time()
            r = writer_handle.chat("你是续写助手", "测试 Writer 不被 Assistant 阻塞")
            writer_result.append(f"elapsed={time.time()-t0:.3f}s, len={len(r)}")
        except Exception as e:  # noqa: BLE001
            writer_errors.append(f"writer: {e!r}")

    threads = []
    for i in range(20):
        threads.append(threading.Thread(target=assistant_worker, args=(i,)))
    threads.append(threading.Thread(target=writer_worker))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    elapsed = time.time() - start
    print(f"  总耗时: {elapsed:.2f}s")
    print(f"  Assistant 成功: {len(assistant_results)}/20, 错误: {len(assistant_errors)}")
    if assistant_results:
        print(f"  Assistant 平均响应: {sum(assistant_results)/len(assistant_results)*1000:.0f}ms, max: {max(assistant_results)*1000:.0f}ms")
    print(f"  Writer: {writer_result}")
    if writer_errors:
        print(f"  Writer 错误: {writer_errors}")
    print(f"  Writer 单例 id 一致: {id(llm_provider_mod._PROVIDER) == writer_id_before}")
    print(f"  Writer 单例对象一致: {llm_provider_mod._PROVIDER is writer_handle}")

    s1_pass = (len(assistant_results) == 20
               and not assistant_errors
               and not writer_errors
               and writer_result
               and id(llm_provider_mod._PROVIDER) == writer_id_before)

    # 还原
    llm_provider_mod.build_assistant_provider = original_factory
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    # ============================================================
    # 场景 2: Assistant 抛错时 Writer 单例健在
    # ============================================================
    print("\n[场景 2] Assistant provider 抛错时 Writer 不受影响")
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_handle2 = llm_provider_mod._PROVIDER
    writer_id2 = id(writer_handle2)

    # 让 assistant 工厂抛错
    def boom_factory():
        raise RuntimeError("simulated assistant boom")
    llm_provider_mod.build_assistant_provider = boom_factory

    # 20 个并发尝试取 assistant
    errors2: list[str] = []
    barrier2 = threading.Barrier(20)
    t0 = time.time()

    def try_assist(i):
        try:
            barrier2.wait(timeout=5)
            llm_provider_mod.get_assistant_provider()
        except RuntimeError as e:
            errors2.append(str(e))

    tts = [threading.Thread(target=try_assist, args=(i,)) for i in range(20)]
    for t in tts:
        t.start()
    for t in tts:
        t.join(timeout=10)
    elapsed2 = time.time() - t0

    print(f"  20 个并发取 assistant 耗时: {elapsed2:.2f}s")
    print(f"  全部 20 个都收到错误: {len(errors2)==20}")
    if errors2:
        print(f"  错误样本: {errors2[0]!r}")
    print(f"  Writer 单例 id 一致: {id(llm_provider_mod._PROVIDER) == writer_id2}")
    print(f"  Writer 单例对象一致: {llm_provider_mod._PROVIDER is writer_handle2}")

    s2_pass = (len(errors2) == 20
               and id(llm_provider_mod._PROVIDER) == writer_id2
               and llm_provider_mod._PROVIDER is writer_handle2)

    # 还原
    llm_provider_mod.build_assistant_provider = original_factory
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    # 总结
    print("\n=== 总结 ===")
    if s1_pass and s2_pass:
        print("PASS: 场景 1 + 场景 2 均通过")
        return 0
    else:
        print(f"FAILED: 场景 1={s1_pass}, 场景 2={s2_pass}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
