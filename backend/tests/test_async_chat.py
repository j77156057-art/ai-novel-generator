"""test_async_chat.py —— 验证 provider.async_chat 异步包装

3 断言：
A. async_chat 返回字符串（与 chat() 一致）
B. asyncio.gather 多个 async_chat 真正并发（3 个 fake 调 0.3s, 整并发耗时 < 0.5s）
C. async_chat 把 exceptions 透传（chat 抛错时 gather return_exceptions=True 能接住）
"""
import asyncio
import time
import sys
sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.llm.provider import LLMProvider


class FakeProvider(LLMProvider):
    def __init__(self, delay=0.5, fail=False):
        self.delay = delay
        self.fail = fail
        self.call_count = 0

    def chat(self, system, user, constraints=None):
        self.call_count += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("fake fail")
        return f"resp[{user[:20]}]"


async def main():
    # A. async_chat 返回字符串
    p = FakeProvider(delay=0.0)
    r = await p.async_chat("sys", "user-A")
    assert r == "resp[user-A]", f"A failed: {r!r}"
    print(f"[A] async_chat returns str ✓ (r={r!r})")

    # B. asyncio.gather 真正并发
    p = FakeProvider(delay=0.3)
    t0 = time.time()
    rs = await asyncio.gather(*[p.async_chat("sys", f"user-{i}") for i in range(3)])
    dt = time.time() - t0
    assert dt < 0.5, f"B failed: not concurrent, dt={dt:.2f}s"
    assert len(rs) == 3, f"B failed: {len(rs)} results"
    print(f"[B] 3 concurrent calls in {dt:.2f}s ✓ (串行 0.9s, 并发 < 0.5s)")

    # C. exceptions 透传
    p = FakeProvider(delay=0.0, fail=True)
    rs = await asyncio.gather(p.async_chat("sys", "user"), return_exceptions=True)
    assert len(rs) == 1
    assert isinstance(rs[0], RuntimeError), f"C failed: not exception {rs[0]!r}"
    print(f"[C] exception透传 ✓ ({rs[0]!r})")


asyncio.run(main())
print("\n=== 3/3 passed ===")
