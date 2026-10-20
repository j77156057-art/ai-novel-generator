"""test_ensure_reference_graphs.py —— v2.228 蒸馏前置图谱自检

5 断言：
A. 已有 character 节点 → 跳过不重建（stats = -1）
B. 无 character 节点 → 调 build_graph 补建
C. build_graph 抛异常 → 记 warning 不阻断（stats = 0）
D. 多本语料混合（一本有图一本没有）→ 只补缺的那本
E. build_graph 返回 0 characters → 记 0 + warning

用 fake db/build_graph，不碰真 SQLite。
"""
import sys
sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

import app.style.narrative as N


class FakeProject:
    id = 1


class FakeDB:
    """按 novel_id 返回预设的 character 节点数。"""

    def __init__(self, counts: dict[int, int]):
        self.counts = counts
        self.scalar_calls = []

    def scalar(self, stmt):
        # 从 stmt 里解不出 novel_id 时退化为 0，测试里靠 build_graph 的调用记录判断
        self.scalar_calls.append(stmt)
        # 按调用顺序消费 counts
        nid = self.scalar_calls.__len__()  # 第 1 次调用对应第 1 本
        return self.counts.get(nid, 0)


def test_a():
    db = FakeDB({1: 12})
    stats = N._ensure_reference_graphs(db, FakeProject(), [1])
    assert stats[1] == -1, f"A failed: {stats}"
    print(f"[A] 已有图跳过不重建 ✓ (stats={stats})")


def test_b():
    db = FakeDB({1: 0})
    calls = []
    orig = N.__dict__.get("_orig_build_graph")

    import app.kg.build as B
    orig_build = B.build_graph
    try:
        B.build_graph = lambda db_, pid, novel_id=None: (
            calls.append(novel_id) or {"characters": 19}
        )
        stats = N._ensure_reference_graphs(db, FakeProject(), [1])
    finally:
        B.build_graph = orig_build

    assert stats[1] == 19, f"B failed: {stats}"
    assert calls == [1], f"B failed: calls={calls}"
    print(f"[B] 缺图补建 ✓ (stats={stats}, build_graph calls={calls})")


def test_c():
    db = FakeDB({1: 0})
    import app.kg.build as B
    orig_build = B.build_graph

    def _boom(db_, pid, novel_id=None):
        raise RuntimeError("分片异常")

    try:
        B.build_graph = _boom
        stats = N._ensure_reference_graphs(db, FakeProject(), [1])
    finally:
        B.build_graph = orig_build

    assert stats[1] == 0, f"C failed: {stats}"
    print(f"[C] 建图异常不阻断 ✓ (stats={stats})")


def test_d():
    db = FakeDB({1: 8, 2: 0})
    calls = []
    import app.kg.build as B
    orig_build = B.build_graph
    try:
        B.build_graph = lambda db_, pid, novel_id=None: (
            calls.append(novel_id) or {"characters": 15}
        )
        stats = N._ensure_reference_graphs(db, FakeProject(), [1, 2])
    finally:
        B.build_graph = orig_build

    assert stats[1] == -1, f"D failed: novel1 应跳过 {stats}"
    assert stats[2] == 15, f"D failed: novel2 应补建 {stats}"
    assert calls == [2], f"D failed: 只该补建 novel2, calls={calls}"
    print(f"[D] 多本混合只补缺 ✓ (stats={stats}, calls={calls})")


def test_e():
    db = FakeDB({1: 0})
    import app.kg.build as B
    orig_build = B.build_graph
    try:
        B.build_graph = lambda db_, pid, novel_id=None: {"characters": 0}
        stats = N._ensure_reference_graphs(db, FakeProject(), [1])
    finally:
        B.build_graph = orig_build

    assert stats[1] == 0, f"E failed: {stats}"
    print(f"[E] 抽出 0 角色记 0 ✓ (stats={stats})")


test_a()
test_b()
test_c()
test_d()
test_e()
print("\n=== 5/5 passed ===")
