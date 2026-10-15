"""v2.6 诊断性 B：detect_unlisted_characters 测试（只告警不替换）。

设计：detect_unlisted_characters 返回"白名单外的 2-6 字中文字符串"列表。
无 NER → 接受高假阳性：所有 2-6 字中文连续串都报（不是人名也报）。
**优点**：高召回率，宁误报不漏报。**缺点**：把"咖啡馆见""电话 12345"也报。
调用方（engine.py）负责 logger.warning + 把列表塞进 return dict。
**正文不变**（v2.6 这条只观测，不纠错）。

跑法：python tests/test_v26_half_b.py
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


# ── 组 1：detect_unlisted_characters 基础 ──

def test_detect_basic():
    print("\n=== 组 1: detect 基础（白名单里的被过滤）===")
    from app.generation.guard import detect_unlisted_characters

    # 白名单全在 → 白名单名被过滤掉
    r = detect_unlisted_characters(
        "林悠然和顾辰逸",
        ["林悠然", "顾辰逸"],
    )
    # "林悠然和顾辰" 6 字串（含"和"）不在白名单 → 报告
    check("白名单名被过滤，但含'和'的串仍报", r, ["林悠然和顾辰"])

    # 薛姨妈 → 报告（白名单外）
    r = detect_unlisted_characters(
        "薛姨妈",
        ["林悠然"],
    )
    check("白名单外「薛姨妈」被报告", r, ["薛姨妈"])

    # 多个白名单外
    r = detect_unlisted_characters(
        "薛姨妈王夫人",
        ["林悠然"],
    )
    # 贪婪模式不切单个——5 字作为一个候选
    check("贪婪切 1 个候选", r, ["薛姨妈王夫人"])


def test_detect_edge_cases():
    print("\n=== 组 2: detect 边界 ===")
    from app.generation.guard import detect_unlisted_characters

    # 白名单空 → 不报（无"白名单"概念）
    r = detect_unlisted_characters(
        "林悠然和薛姨妈。",
        [],
    )
    check("白名单空 → 不报", r, [])

    # 白名单只有单字 → 单字不进白名单 → 不报
    r = detect_unlisted_characters(
        "林悠然和薛姨妈。",
        ["林"],
    )
    check("白名单只有单字 → 不报", r, [])

    # 空文本
    r = detect_unlisted_characters("", ["林悠然"])
    check("空文本", r, [])

    # 短文本（2 字起）能被检测
    r = detect_unlisted_characters(
        "咖啡",
        ["林悠然"],
    )
    check("2 字也报", r, ["咖啡"])


def test_detect_diagnostic_semantics():
    print("\n=== 组 3: 诊断性语义（接受假阳性）===")
    from app.generation.guard import detect_unlisted_characters

    # 普通词组也被报（接受假阳性）
    r = detect_unlisted_characters(
        "咖啡馆见面。",
        ["林悠然"],
    )
    # 贪婪切 1 个候选
    check("「咖啡馆见面」会报（假阳性，可接受）", "咖啡馆见面" in r, True)

    # 数字旁边的"电话"也报
    r = detect_unlisted_characters(
        "电话 12345。",
        ["林悠然"],
    )
    check("「电话」会报（假阳性）", "电话" in r, True)

    # 纯英文不报
    r = detect_unlisted_characters(
        "Hello World.",
        ["林悠然"],
    )
    check("纯英文不报", r, [])

    # 去重：同一名字多次出现只报一次
    r = detect_unlisted_characters(
        "薛姨妈说：你好。薛姨妈走了。",
        ["林悠然"],
    )
    # 贪婪切 3 个候选：「薛姨妈说」「你好」「薛姨妈走了」
    # 单字"薛姨妈"不单独切（被贪婪吸收到更长的串里）
    check("「薛姨妈说」被报告", "薛姨妈说" in r, True)
    check("「薛姨妈走了」被报告", "薛姨妈走了" in r, True)
    check("「你好」被报告（假阳性）", "你好" in r, True)


# ── 组 4：collect_allowed_names ──

def test_collect_allowed_names():
    print("\n=== 组 4: collect_allowed_names ===")
    from app.generation.guard import collect_allowed_names

    names = collect_allowed_names()
    check("全空 → []", names, [])

    names = collect_allowed_names(
        ctx_reminders={"characters": ["林悠然", "顾辰逸"]},
    )
    check("ctx_reminders list[str]", set(names), {"林悠然", "顾辰逸"})

    names = collect_allowed_names(
        ctx_reminders={"characters": [
            {"name": "林悠然", "mentions": 100},
            {"canonical": "顾辰逸", "alias": ["小顾"]},
        ]},
    )
    check("ctx_reminders dict name", "林悠然" in names, True)
    check("ctx_reminders dict canonical", "顾辰逸" in names, True)

    names = collect_allowed_names(
        plot_bible={"characters": ["角色A", {"name": "角色B"}]},
    )
    check("plot_bible list[str]", "角色A" in names, True)
    check("plot_bible dict", "角色B" in names, True)

    names = collect_allowed_names(
        ctx_reminders={"characters": ["林悠然"]},
        plot_bible={"characters": ["林悠然", "顾辰逸"]},
    )
    check("去重：林悠然只一次", names.count("林悠然"), 1)
    check("顾辰逸在", "顾辰逸" in names, True)

    names = collect_allowed_names(
        ctx_reminders={"characters": ["林", "林悠然"]},
    )
    check("单字 '林' 过滤", "林" in names, False)
    check("'林悠然' 保留", "林悠然" in names, True)


# ── 跑全部 ──

if __name__ == "__main__":
    test_detect_basic()
    test_detect_edge_cases()
    test_detect_diagnostic_semantics()
    test_collect_allowed_names()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
