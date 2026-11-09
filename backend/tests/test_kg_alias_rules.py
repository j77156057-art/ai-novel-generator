"""化名判据回归测试：典故/一次性玩笑不得进称呼层。

用例全部取自《大奉打更人》原文，判据不是「听起来像不像人名」，
而是「是否跨章、是否被当事人否认、是否是典故引用」。
"""
from __future__ import annotations

from app.kg.alias_rules import judge_alias


def test_allusion_rejected_even_if_cross_chapter():
    # 「六号是个苦大仇深的鲁智深」：7 章出现，但全是比喻，不能折成恒远的别名
    v = judge_alias(
        "鲁智深",
        [83, 93, 114, 126, 288, 289, 306],
        ["“九号是金莲那个老银币，六号是个苦大仇深的鲁智深，其他几个网友，总该有漂亮妹子吧。",
         "身材魁梧的“鲁智深”站在院子里，沉默了许久。"],
        canonical="恒远",
    )
    assert v["ok"] is False
    assert v["verdict"] == "allusion"


def test_one_off_joke_alias_rejected():
    # 「在下陈近南，请多指教」是当场玩笑化名；ch83 原文直接否认过
    v = judge_alias(
        "陈近南",
        [77, 78, 83],
        ["有点尴尬...许七安重新输入信息：“诸位，在下陈近南，请多指教。",
         "他们都没喊三号的名字，大概心里都清楚，陈近南什么的，根本不是三号的真名。"],
        canonical="许七安",
    )
    assert v["ok"] is False
    assert v["verdict"] in {"allusion", "mock"}


def test_mock_without_allusion_list_still_rejected():
    # 不在典故库里，但两个语境都在否认 → mock
    v = judge_alias(
        "张三丰",
        [10, 11],
        ["他笑道：“化名张三丰。”", "张三丰什么的，完全不是真名。"],
    )
    assert v["ok"] is False
    assert v["verdict"] == "mock"


def test_single_chapter_rejected():
    v = judge_alias("恒清大师", [125], ["恒清大师不说话。"], canonical="恒清")
    assert v["ok"] is False
    assert v["verdict"] == "single_chapter"


def test_cross_chapter_alias_accepted():
    # 许大郎 75 次 / 45 章，是许七安的常态称呼，不是一次性玩笑
    v = judge_alias(
        "许大郎",
        [21, 29, 30, 34, 35, 49, 53, 55, 57, 58, 89, 103, 108, 116, 131],
        ["许二叔和许大郎脸色同时一黑。", "许二郎俊美的脸蛋挨了一拳，许大郎顺势骑上去。"],
        canonical="许七安",
    )
    assert v["ok"] is True
    assert v["verdict"] == "alias"


def test_identity_and_too_short():
    assert judge_alias("许七安", [1, 2], canonical="许七安")["verdict"] == "identity"
    assert judge_alias("许", [1, 2])["verdict"] == "too_short"
