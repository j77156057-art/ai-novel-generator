"""test_character_candidate_pool.py —— v2.229 候选池判据

4 断言：
A. mentions 达标的真角色全部进池（不因 speaking=False 被排除）
B. speaking=True 的节点排在前面（确定有台词的优先）
C. _FAKE_CHARS 黑名单仍生效（虚词不进池）
D. min_mentions 下限仍生效

回归背景：旧判据 (speaking or m>=200) and m>=min_mentions 在斗破 30 章
只产出 5 个候选（2 个是伪角色），角色声音维度只出 2 条。
"""
import sys
sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import _build_character_name_index


class FakeNode:
    def __init__(self, name, mentions, speaking=False, source=None):
        self.props = {"name": name, "mentions": mentions, "speaking": speaking}
        self.type = "character"
        self.source = source


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, stmt):
        return FakeScalars(self._rows)


class FakeProject:
    id = 1


def build(rows, corpus_text="", cleaning="full", min_mentions=10):
    db = FakeDB([FakeNode(n, m, s, source="novel:1") for n, m, s in rows])
    return _build_character_name_index(
        db, FakeProject(), 1, corpus_text=corpus_text,
        min_mentions=min_mentions, cleaning=cleaning,
    )


def test_a():
    # 真角色 mentions 达标但 speaking=False（斗破实测情形）
    rows = [
        ("萧炎", 1265, True),
        ("萧战", 159, False),
        ("纳兰", 139, False),
        ("萧宁", 68, False),
        ("萧媚", 51, False),
        ("萧薰儿", 15, False),
    ]
    canonical, _ = build(rows)
    names = {n for n, _ in canonical}
    expect = {"萧炎", "萧战", "纳兰", "萧宁", "萧媚", "萧薰儿"}
    missing = expect - names
    assert not missing, f"A failed: 漏真角色 {missing}（池={names}）"
    print(f"[A] speaking=False 的真角色全进池 ✓ ({len(names)} 个)")


def test_b():
    # speaking=True 不再影响排序（候选池按 mentions 排序，speaking 只用于
    # 旧版布尔判据，v2.229 已废弃）。这里验证「进池不受 speaking 影响」。
    rows = [
        ("萧炎", 1265, True),   # speaking
        ("萧战", 159, False),   # 高提及但非 speaking
        ("路人甲", 30, True),   # 低提及但 speaking
    ]
    canonical, _ = build(rows)
    order = [n for n, _ in canonical]
    assert set(order) == {"萧炎", "萧战", "路人甲"}, f"B failed: 池={order}"
    # 按 mentions 降序
    assert order == ["萧炎", "萧战", "路人甲"], f"B failed: 未按 mentions 排序 {order}"
    print(f"[B] 进池不受 speaking 影响、按 mentions 排序 ✓ (order={order})")


def test_c():
    rows = [
        ("萧炎", 100, True),
        ("家族", 500, False),   # 黑名单
        ("闻言", 300, False),   # 黑名单
        ("时间", 400, False),   # 黑名单
    ]
    canonical, _ = build(rows)
    names = {n for n, _ in canonical}
    for fake in ("家族", "闻言", "时间"):
        assert fake not in names, f"C failed: 伪角色 {fake} 进池 names={names}"
    print(f"[C] 黑名单伪角色被拦 ✓ (池={names})")


def test_d():
    rows = [
        ("萧炎", 100, True),
        ("龙套", 5, False),   # 低于 min_mentions=10
    ]
    canonical, _ = build(rows)
    names = {n for n, _ in canonical}
    assert "龙套" not in names, f"D failed: 低提及角色进池 {names}"
    print(f"[D] min_mentions 下限生效 ✓ (池={names})")


test_a()
test_b()
test_c()
test_d()
print("\n=== 4/4 passed ===")
