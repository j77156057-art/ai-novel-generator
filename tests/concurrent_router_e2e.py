"""端到端并发压测：20 个 /api/assistant/qa 顺序触发 + 1 个 Writer 单例调用。

设计：
- 不并发写 DB（SQLite 限制）；改为：20 个 worker 串行调 router 函数（各起独立 SessionLocal），
  Writer 线程与 Assistant 串行调用同时进行（用 LLM provider 层互不阻塞来验证隔离）。
- 关键校验：
  ① Writer 单例在 Assistant 20 轮调用过程中不被替换；
  ② Assistant provider 抛错时：router 仍返回 200（带降级 answer），Writer 仍健康；
  ③ Writer 业务调用耗时正常（不被 Assistant 阻塞）。
"""
from __future__ import annotations

import os
import sys
import threading
import time

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents.registry import reset_tool_registry  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import Project  # noqa: E402
from app.routers.assistant import AssistantQARequest  # noqa: E402


class _BoomProvider:
    """Assistant 抛错 provider：用来验证 router 降级。"""

    model = "boom"

    def chat(self, system, user, constraints=None):
        raise RuntimeError("simulated assistant failure in router")


def _setup_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_project() -> int:
    s = SessionLocal()
    try:
        proj = Project(title="e2e 压测", target_length=5000, mode="quick", status="created")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj.id
    finally:
        s.close()


def _call_qa(project_id: int, query: str) -> dict:
    from app.routers.assistant import assistant_qa
    s = SessionLocal()
    try:
        body = AssistantQARequest(project_id=project_id, query=query)
        return assistant_qa(body=body, db=s)
    finally:
        s.close()


def main() -> int:
    print("=== 端到端压测：20 个 /api/assistant/qa 串行 + 1 个 Writer 单例 ===\n")

    # 干净状态
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    _setup_db()
    project_id = _create_project()

    # 锁住 Writer
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_handle = llm_provider_mod._PROVIDER
    writer_id = id(writer_handle)

    # 替换 assistant 工厂为 boom
    original_factory = llm_provider_mod.build_assistant_provider
    llm_provider_mod.build_assistant_provider = lambda: _BoomProvider()

    # 启动 1 个 Writer 线程（持续 0.5s，模拟业务调用）
    writer_done = threading.Event()
    writer_error: list[str] = []
    writer_timing: list[float] = []

    def writer_runner():
        try:
            t0 = time.time()
            r = writer_handle.chat("你是续写助手", "测试 Writer 不被 Assistant 阻塞")
            writer_timing.append(time.time() - t0)
            assert isinstance(r, str) and len(r) > 0
        except Exception as e:  # noqa: BLE001
            writer_error.append(repr(e))
        finally:
            writer_done.set()

    writer_thread = threading.Thread(target=writer_runner)
    writer_thread.start()

    # 20 个 Assistant qa 调用（串行，因为 SQLite 不支持并发写）
    results: list[dict] = []
    errors: list[str] = []
    start = time.time()
    for i in range(20):
        try:
            r = _call_qa(project_id, f"q-{i}")
            results.append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"q-{i}: {e!r}")
    elapsed = time.time() - start

    writer_thread.join(timeout=10)
    writer_done.wait(timeout=5)

    # 校验
    print(f"[Assistant 端] elapsed={elapsed:.2f}s, 成功={len(results)}, 错误={len(errors)}")

    # 检查响应合法性
    all_valid = all(
        all(k in r for k in ("answer", "evidence", "confidence", "agent_run_id"))
        for r in results
    )
    # 检查 answer 是降级文案（因为 assistant provider 抛错）
    has_degraded = all("Assistant provider 不可用" in r["answer"] for r in results)

    print(f"  全部含必需字段: {all_valid}")
    print(f"  全部返回降级文案（说明 provider 抛错被捕获）: {has_degraded}")
    if errors:
        print(f"  错误样本: {errors[:3]}")

    # Writer 校验
    print(f"\n[Writer 端]")
    print(f"  Writer 业务调用耗时: {writer_timing}")
    print(f"  Writer 错误: {writer_error}")
    print(f"  Writer 单例 id 一致: {id(llm_provider_mod._PROVIDER) == writer_id}")
    print(f"  Writer 单例对象一致: {llm_provider_mod._PROVIDER is writer_handle}")

    # 总结
    s_pass = (len(results) == 20
              and not errors
              and all_valid
              and has_degraded
              and not writer_error
              and writer_timing
              and id(llm_provider_mod._PROVIDER) == writer_id
              and llm_provider_mod._PROVIDER is writer_handle)

    # 还原
    llm_provider_mod.build_assistant_provider = original_factory
    llm_provider_mod.reset_assistant_provider_cache()
    reset_provider_registry()

    print()
    if s_pass:
        print("PASS: 20 个 Assistant 端到端 + 1 个 Writer 调用；Writer 单例未受影响")
        return 0
    else:
        print("FAILED: 端到端并发隔离未通过")
        return 1


if __name__ == "__main__":
    sys.exit(main())
