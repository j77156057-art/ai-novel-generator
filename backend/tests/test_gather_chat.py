"""test_gather_chat.py —— 验证 narrative._gather_chat 真并发

3 断言：
A. 空列表返回 []
B. 5 个 0.3s 串行 = 1.5s, _gather_chat 应 < 0.5s（5x 提速）
C. 失败/异常透传为 Exception 实例
"""
import asyncio
import time
import sys
sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.llm.provider import LLMProvider
from app.style.narrative import _gather_chat, _DEFAULT_MAX_CONCURRENT


class FakeProvider(LLMProvider):
    def __init__(self, delay=0.3, fail_user_substr=None):
        self.delay = delay
        self.fail_user_substr = fail_user_substr
        self.call_count = 0

    def chat(self, system, user, constraints=None):
        time.sleep(self.delay)
        self.call_count += 1
        if self.fail_user_substr and self.fail_user_substr in user:
            raise RuntimeError(f"fail: {user}")
        return f"resp[{user[:20]}]"


# A. 空列表
def test_a():
    p = FakeProvider()
    rs = _gather_chat(p, [])
    assert rs == [], f"A failed: {rs}"
    print(f"[A] empty prompts returns [] ✓")


# B. 5 个 0.3s 真并发（max_concurrent=5 → 1 批 = 0.3s）
def test_b():
    p = FakeProvider(delay=0.3)
    prompts = [("sys", f"user-{i}", None) for i in range(5)]
    t0 = time.time()
    rs = _gather_chat(p, prompts, max_concurrent=5)
    dt = time.time() - t0
    assert len(rs) == 5, f"B failed: {len(rs)}"
    assert dt < 0.6, f"B failed: 串行 1.5s 应并发 0.3-0.5s 实测 {dt:.2f}s"
    print(f"[B] 5 concurrent calls in {dt:.2f}s ✓ (串行 1.5s, 并发 0.3-0.6s)")


# C. 失败透传为 Exception 实例
def test_c():
    p = FakeProvider(delay=0.1, fail_user_substr="user-2")
    prompts = [("sys", f"user-{i}", None) for i in range(5)]
    rs = _gather_chat(p, prompts, max_concurrent=5)
    assert len(rs) == 5
    # user-2 应是 Exception，其他是 str
    assert isinstance(rs[2], RuntimeError), f"C failed: rs[2]={type(rs[2])}"
    assert isinstance(rs[0], str), f"C failed: rs[0]={type(rs[0])}"
    print(f"[C] failure passed through as Exception ✓ (rs[2]={type(rs[2]).__name__}, rs[0]={type(rs[0]).__name__})")


# 跑
test_a()
test_b()
test_c()
print("\n=== 3/3 passed ===")
