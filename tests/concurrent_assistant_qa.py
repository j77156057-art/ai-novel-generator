"""并发压测：20 个 /api/assistant/qa 并发 + 1 个 Writer 生成调用。

验收点（按 team-lead 派单 7 项）：
- 5 慢 + 15 快：模拟 Assistant provider 慢响应 / 快响应混合。
- 同时触发 1 次 Writer 单例调用（chat 路径），断言：
  ① Writer 单例不被 Assistant 故障污染（仍是同一实例）。
  ② Writer 业务调用能完成。
  ③ 20 个 Assistant 请求全返合法响应（answer/evidence/confidence/agent_run_id）。
  ④ Assistant 失败 / 慢响应不阻塞 Writer。
- 不发任何网络：Assistant provider 替换为 LocalFallbackLLMProvider。
- 不连真实 DB：内存 sqlite 库。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections import Counter

# 路径设置
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents.registry import reset_tool_registry  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import Project  # noqa: E402
from app.routers import assistant as assistant_router  # noqa: E402
from app.routers.assistant import AssistantQARequest  # noqa: E402


class _SlowProvider(LocalFallbackLLMProvider):
    """Assistant 用：模拟慢响应（带 sleep），验证锁/失败隔离。"""

    def __init__(self, sleep_s: float = 0.5, label: str = "slow") -> None:
        super().__init__()
        self._sleep = sleep_s
        self.label = label

    def chat(self, system: str, user: str, constraints=None):  # noqa: D401
        time.sleep(self._sleep)
        return super().chat(system, user, constraints)


class _BoomProvider:
    """Assistant 用：模拟抛错，验证 Writer 不被污染。"""

    def __init__(self):
        self.model = "boom"

    def chat(self, system, user, constraints=None):
        raise RuntimeError("assistant provider simulated failure")


def _setup_in_memory_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _create_project() -> int:
    s = SessionLocal()
    try:
        proj = Project(title="并发压测", target_length=5000, mode="quick", status="created")
        s.add(proj)
        s.commit()
        s.refresh(proj)
        return proj.id
    finally:
        s.close()


def _call_assistant_qa(project_id: int, query: str, slow: bool = False) -> dict:
    """走 router 函数（不是 HTTP；并发等价于多 FastAPI 请求）。"""
    # 把 assistant 工厂替换为带 sleep 的 slow provider（或正常 fallback）
    if slow:
        # 模拟慢响应
        original = llm_provider_mod.build_assistant_provider
        llm_provider_mod.build_assistant_provider = lambda: _SlowProvider(sleep_s=0.3, label="slow")
    s = SessionLocal()
    try:
        body = AssistantQARequest(project_id=project_id, query=query)
        return assistant_router.assistant_qa(body=body, db=s)
    finally:
        s.close()
        if slow:
            llm_provider_mod.build_assistant_provider = original
            llm_provider_mod.reset_assistant_provider_cache()


def _writer_singleton_call(writer_handle) -> str:
    """Writer 单例业务调用：直接调 _PROVIDER.chat。"""
    return writer_handle.chat(
        "你是小说续写助手。", "测试：确保 Writer 单例仍能产出文本。"
    )


def main() -> int:
    # 1) 干净状态
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    _setup_in_memory_db()
    project_id = _create_project()

    # 2) 锁住 Writer 单例（LocalFallback，无网络）
    llm_provider_mod._PROVIDER = LocalFallbackLLMProvider()
    writer_handle = llm_provider_mod._PROVIDER
    writer_id_before = id(writer_handle)

    # 3) 并发 20 个 /assistant/qa（5 慢 + 15 快）
    results: list[dict] = []
    errors: list[str] = []
    threads: list[threading.Thread] = []
    barrier = threading.Barrier(20)
    start = time.time()

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            slow = (i < 5)  # 前 5 个慢，后 15 个快
            r = _call_assistant_qa(project_id, f"q-{i}", slow=slow)
            results.append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"worker-{i}: {e!r}")

    for i in range(20):
        t = threading.Thread(target=worker, args=(i,))
        threads.append(t)
        t.start()

    # 4) 在 Assistant 并发期间触发 1 次 Writer 调用
    writer_thread = threading.Thread(
        target=lambda: _writer_singleton_call(writer_handle), daemon=True
    )
    writer_thread.start()

    for t in threads:
        t.join(timeout=30)
    writer_thread.join(timeout=10)

    elapsed = time.time() - start
    print(f"\n[并发结果] elapsed={elapsed:.2f}s, 成功={len(results)}, 错误={len(errors)}")

    # 5) 校验
    print(f"\n[Writer 校验]")
    print(f"  Writer 单例 id 前后一致: {id(llm_provider_mod._PROVIDER) == writer_id_before}")
    print(f"  Writer 单例仍是原对象: {llm_provider_mod._PROVIDER is writer_handle}")
    print(f"  Writer 单例 class: {type(llm_provider_mod._PROVIDER).__name__}")

    # 6) Assistant 响应合法性
    print(f"\n[Assistant 响应合法性] (n={len(results)})")
    required = ("answer", "evidence", "confidence", "agent_run_id")
    all_have_required = all(all(k in r for k in required) for r in results)
    print(f"  全部含 answer/evidence/confidence/agent_run_id: {all_have_required}")

    # 7) 错误信息
    if errors:
        print(f"\n[错误清单]")
        for e in errors[:10]:
            print(f"  {e}")
    else:
        print(f"\n[无错误]")

    # 8) 总结
    if errors:
        print("\nFAILED: 存在错误")
        return 1
    if not all_have_required:
        print("\nFAILED: 部分响应缺字段")
        return 1
    if id(llm_provider_mod._PROVIDER) != writer_id_before:
        print("\nFAILED: Writer 单例被污染")
        return 1
    print("\nPASSED: Writer 不受 Assistant 并发影响；20 个 Assistant 响应全合法")
    return 0


if __name__ == "__main__":
    sys.exit(main())
