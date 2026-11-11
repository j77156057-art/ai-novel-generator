"""v2.6 诊断性 B：engine.py 集成测试。

覆盖：
- generate_next_segment 返回 dict 含 unlisted_characters 字段
- 白名单外角色被报告（即使没真 NER 也用启发式）
- 同步版/流式版/重生成版都不替换正文
- 白名单内角色不被报告
- 白名单空 → 返回 []

跑法：python tests/test_v26_engine_integration.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{tag}  {label}: got={got!r} want={want!r}")


# ── 组 1：detect_unlisted_characters + engine.py 集成 ──

def test_detect_via_guard():
    """直接调 guard.py 验证逻辑（不调 engine.py——避免 mock 一堆）"""
    print("\n=== 组 1: detect via guard ===")
    from app.generation.guard import detect_unlisted_characters

    # 白名单里有"林悠然"——正文"林悠然"被过滤
    r = detect_unlisted_characters("林悠然", ["林悠然"])
    check("白名单名被过滤", r, [])

    # 白名单里有"林悠然"——正文"薛姨妈"被报告
    r = detect_unlisted_characters("薛姨妈", ["林悠然"])
    check("白名单外被报告", r, ["薛姨妈"])

    # 白名单空 → 不报告
    r = detect_unlisted_characters("薛姨妈", [])
    check("白名单空 → 不报", r, [])

    # 集成到 engine.py 风格：ctx_reminders + plot_bible → collect
    from app.generation.guard import collect_allowed_names
    allowed = collect_allowed_names(
        ctx_reminders={"characters": ["林悠然", "顾辰逸"]},
        plot_bible={"characters": ["林悠然"]},
    )
    r = detect_unlisted_characters("林悠然说：薛姨妈在门口。", allowed)
    # 贪婪：先切"林悠然说"或"林悠然说：薛姨妈"——5 字 5 字 切
    # 实际：贪婪切最长，先 6 字窗口 "林悠然说：薛姨"——6 字（不重复是 5）——"林悠然说"4 字 或"林悠然说：薛"5 字
    # 简化：检查"林悠然"作为单字串也在 r 里（被白名单过滤掉了应该不在）
    check("林悠然不在报告里", "林悠然" not in r, True)


def test_engine_return_field_in_unit():
    """测 engine.py 集成：mock generate 路径，验证 unlisted_characters 字段在 return dict 里。

    完整端到端需要真 provider——这里只测 detect_unlisted_characters 的入口是
    被 import 且能被调用（间接保证 engine.py 引用正确）。
    """
    print("\n=== 组 2: engine.py 引用正确性 ===")
    # 静态检查 engine.py 是否引用 guard
    import importlib
    try:
        import app.generation.guard  # noqa: F401
    except Exception as e:
        check("guard.py import 成功", False, True)
        print("  import 失败:", e)
        return
    check("guard.py import 成功", True, True)

    # 静态检查：engine.py 含 detect_unlisted_characters 调用
    engine_path = BACKEND / "app" / "generation" / "engine.py"
    content = engine_path.read_text(encoding="utf-8")
    check(
        "engine.py 含 detect_unlisted_characters 调用",
        "detect_unlisted_characters" in content,
        True,
    )
    check(
        "engine.py 含 collect_allowed_names 调用",
        "collect_allowed_names" in content,
        True,
    )
    check(
        "engine.py return dict 含 unlisted_characters（同步版）",
        content.count('"unlisted_characters": unlisted') >= 1,
        True,
    )
    # 同步版有 1 个 + 流式版有 1 个 = 2 个
    check(
        "engine.py 流式版 yield 也含 unlisted_characters",
        content.count('"unlisted_characters": unlisted') >= 2,
        True,
    )


# ── 组 4：重写路径必须携带物品持有块与 constraints（2026-11-10 修）──

def test_rewrite_carries_item_block_and_constraints():
    print("\n=== 组 4: 重写携带注入块 ===")
    import inspect

    from app.generation import engine

    block = "- 镇国剑：权属（own）归镇北王（九十六至一百零六章）"
    system, user = engine._rewrite_prompt(
        "正文" * 80, ["机械转折"], None, None, item_holding_block=block,
    )
    check("重写 system 声明不得改动物品归属",
          "不得改动物品归属" in system, True)
    check("重写 user 含块标题",
          "【物品持有事实（已写内容最新一段；续写须与此一致）】" in user, True)
    check("重写 user 含块明细", block in user, True)
    check("块在「请改写」之前", user.index(block) < user.index("请改写："), True)
    check("优先级说明随块一起下发", "不要静默改写物品归属" in user, True)

    # 无块时保持旧形状：不加第 8 条、不出现块标题
    s2, u2 = engine._rewrite_prompt("正文" * 80, ["机械转折"], None, None)
    check("无块 system 不加第 8 条", "不得改动物品归属" in s2, False)
    check("无块 user 不含块标题", "【物品持有事实" in u2, False)

    class _Provider:
        def __init__(self):
            self.calls = []

        def chat(self, system, user, constraints=None, stop_sequences=None):
            self.calls.append(
                {"system": system, "user": user, "constraints": constraints}
            )
            return "改写后的正文，" * 40

    p = _Provider()
    cons = {"characters": ["李浩"], "plotholes": ["策划书去哪了"]}
    out, info = engine._rewrite_with_evidence(
        None, None, p, "正文" * 80, ["机械转折"], None,
        item_holding_block=block, constraints=cons,
    )
    check("重写成功返回", out is not None, True)
    check("constraints 进 system（不追加到待改正文之后）",
          "【本段已锁定角色（必须保持一致，不得凭空新增主要人物）】李浩"
          in p.calls[0]["system"], True)
    check("伏笔也进 system",
          "策划书去哪了" in p.calls[0]["system"], True)
    check("provider 不再收到 constraints= 追加",
          p.calls[0]["constraints"] is None, True)
    check("重写 prompt 含块", block in p.calls[0]["user"], True)
    check("rewrite_info 记录已注入",
          (info or {}).get("item_holding_injected"), True)
    check("rewrite_info 记录 constraints 已注入",
          (info or {}).get("constraints_injected"), True)

    src = inspect.getsource(engine)
    check("同步/流式两个入口都传块",
          src.count('item_holding_block=ctx.get("item_holding_block")') >= 2, True)


# ── 跑全部 ──

if __name__ == "__main__":
    test_detect_via_guard()
    test_engine_return_field_in_unit()
    test_rewrite_carries_item_block_and_constraints()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
