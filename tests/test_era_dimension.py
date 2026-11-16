"""v3.0 Judge 时间锚量纲转换测试（era_convert + check_impossible_sequences 口径 C）。

覆盖：
- parse_story_time：公元年 / 年号纪年 / 元正 / 超区间 / 边界假阳性
- 三类假阳性防线：跨词切碎（左边界）/ 量词误食（右边界）/ 著录长句（长度）
- 同名年号消歧（建元 = 西汉/东晋/南齐）
- 口径 C 判定：同 era 倒挂报 err / 递增不报 / 跨 era 换年号不报 /
  混排两处倒挂全检出 / 解析失败断链 / 旧 4 位公元年行为保留
- 古典长篇语料全量回归：0 误报（本地投放了语料才跑，没投放就跳过）

（这份原本是「脚本式断言」—— check() 只记数不抛，跑挂了也会显示通过。
 已改写成 pytest 用例：失败即抛，断言消息带标签订位。）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.era_convert import cn2int, era_year, extract_era_name, parse_story_time
from app.kg.judge import check_impossible_sequences
from app.models import KgEvent


# ====================================================================
# 1. cn2int 中文数字
# ====================================================================
def test_cn2int():
    assert cn2int("三") == 3
    assert cn2int("十") == 10
    assert cn2int("十一") == 11
    assert cn2int("二十三") == 23
    assert cn2int("一百零八") == 108
    assert cn2int("") is None
    assert cn2int("三年") is None


# ====================================================================
# 2. parse_story_time 真阳性
# ====================================================================
def test_parse_story_time_true_positives():
    assert parse_story_time("2020年3月") == 2020
    assert parse_story_time("2020") == 2020
    assert parse_story_time("贞观三年秋") == 629   # 627 + 3 - 1
    assert parse_story_time("贞观三年") == 629
    assert parse_story_time("康熙元年") == 1662     # 1662 + 1 - 1
    assert parse_story_time("大观四年") == 1110     # 1107 + 4 - 1
    assert parse_story_time("元丰五年") == 1082     # 1078 + 5 - 1
    assert parse_story_time("乾隆六十年") == 1795   # 1736 + 60 - 1
    assert parse_story_time("嘉靖四十五年") == 1566
    assert parse_story_time("政和二年春") == 1112
    assert parse_story_time("贞观十八年") == 644   # 627 + 18 - 1


# ====================================================================
# 3. 假阳性防线（古典长篇语料实测成因）
# ====================================================================
def test_parse_story_time_false_positive_guards():
    # 3a 左边界：跨词切碎
    assert parse_story_time("老太太和二奶奶办的") is None
    assert parse_story_time("亲家太太和三妹妹") is None
    assert parse_story_time("总理元始三一教门") is None
    # 3b 右边界：量词误食
    assert parse_story_time("得了凤凰一般") is None
    assert parse_story_time("金玉如意一柄") is None
    assert parse_story_time("剑斩黄龙一线儿差") is None
    # 3c 长度防线：著录长句
    assert parse_story_time("宋元丰五年四月眉山苏轼见于秘府") is None
    assert parse_story_time("唐贞观三年秋月春风花落") is None   # 11 字符
    assert parse_story_time("唐贞观三年秋月春") == 629           # 8 字符，合法
    # 3d 园林/场景
    assert parse_story_time("大观园试才题对额") is None
    assert parse_story_time("只管普通一请") is None
    assert parse_story_time("似非大观") is None
    # 3e 空/无匹配
    assert parse_story_time("") is None
    assert parse_story_time(None) is None
    assert parse_story_time("不详") is None
    # 3f 超区间
    assert parse_story_time("嘉靖六十年") is None  # 嘉靖最长 45


# ====================================================================
# 4. 同名年号消歧
# ====================================================================
def test_era_year_disambiguation():
    # 建元在 3 个朝代复用：西汉(-140..-135) / 东晋(343..344) / 南齐(479..482)。
    # 「建元六年」只有西汉区间能容 → -135（唯一解）
    assert era_year("建元", 6) == -135
    # 「建元三年」西汉(-138) 与东晋(345) 都能容 → 按设计取最小公元年
    assert era_year("建元", 3) == -138
    assert era_year("建元", 4) == -137
    # 超出全部区间 → None
    assert era_year("建元", 10) is None
    assert era_year("建元", 2) == -139
    assert era_year("贞观", 3) == 629
    assert era_year("贞观", 23) == 649           # 唐贞观末


# ====================================================================
# 5. extract_era_name
# ====================================================================
def test_extract_era_name():
    assert extract_era_name("贞观三年秋") == "贞观"
    assert extract_era_name("康熙元") == "康熙"
    assert extract_era_name("2024年3月") is None
    assert extract_era_name("") is None


# ====================================================================
# 6. check_impossible_sequences 口径 C
# ====================================================================
def _fresh_db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    return Session()


def _add_events(db, pairs: list[tuple[int, str, str]]) -> None:
    """pairs: [(sequence, in_story_date, summary)]"""
    for seq, date, summary in pairs:
        db.add(KgEvent(project_id=1, sequence=seq, event_type="incident",
                       summary=summary, in_story_date=date))
    db.commit()


def _reset(db):
    db.query(KgEvent).delete()
    db.commit()


def test_same_era_backwards_in_time_is_reported():
    db = _fresh_db()
    _add_events(db, [(10, "贞观三年秋", "甲"), (11, "贞观元年春", "乙")])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 1, f"同 era 倒挂报 err: got={len(f)}"
    assert f[0].rule == "impossible_sequences", "同 era 倒挂 rule 正确"
    assert f[0].severity == "err", "同 era 倒挂 severity err"
    db.close()


def test_same_era_forward_in_time_is_not_reported():
    db = _fresh_db()
    _add_events(db, [(20, "贞观元年春", "丙"), (21, "贞观三年秋", "丁")])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 0, f"同 era 递增不报: got={len(f)}"
    db.close()


def test_era_switch_is_not_reported():
    db = _fresh_db()
    _add_events(db, [(30, "贞观二十三年", "戊"), (31, "永徽元年", "己")])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 0, f"跨 era 换年号不报: got={len(f)}"
    db.close()


def test_mixed_eras_detect_every_backwards_pair():
    db = _fresh_db()
    _add_events(db, [
        (40, "贞观三年秋", "庚"),     # 629
        (41, "贞观元年春", "辛"),     # 627  ← 倒挂 1
        (42, "永徽四年", "壬"),       # 653
        (43, "永徽二年", "癸"),       # 651  ← 倒挂 2
        (44, "贞观五年", "子"),       # 631（跨 era，不比）
    ])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 2, f"混排两处倒挂全检出: got={len(f)}"
    db.close()


def test_unparsable_date_breaks_the_chain():
    db = _fresh_db()
    _add_events(db, [
        (50, "贞观三年秋", "丑"),     # 629
        (51, "时间不明", "寅"),       # None → 断链
        (52, "贞观元年春", "卯"),     # 627
    ])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 0, f"解析失败断链不报: got={len(f)}"
    db.close()


def test_plain_ad_years_keep_old_behaviour():
    db = _fresh_db()
    _add_events(db, [(60, "2020年3月", "辰"), (61, "2018年5月", "巳")])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 1, f"公元年递减报 err: got={len(f)}"

    _reset(db)
    _add_events(db, [(70, "2021年", "午"), (71, "2022年", "未")])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 0, f"公元年递增不报: got={len(f)}"
    db.close()


def test_empty_date_is_skipped_but_later_pair_still_checked():
    db = _fresh_db()
    _add_events(db, [
        (80, "", "申"),
        (81, "贞观三年秋", "酉"),
        (82, "贞观元年春", "戌"),   # 这一对应该报
    ])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 1, f"空日期跳过 + 后续对仍判: got={len(f)}"
    db.close()


def test_judge_sorts_by_sequence_itself():
    db = _fresh_db()
    _add_events(db, [
        (92, "贞观元年春", "亥"),   # 627（后落库但 sequence 大）
        (91, "贞观三年秋", "甲"),   # 629
    ])
    f = check_impossible_sequences(db, 1, "", segment_no=1)
    assert len(f) == 1, f"乱序落库按 sequence 排序后判: got={len(f)}"
    db.close()


# ====================================================================
# 7. 古典长篇语料全量回归（防假阳性）
# ====================================================================
def test_no_false_positive_on_local_corpus():
    """本地投放了古典长篇语料才跑；没投放就跳过（版权正文不进仓库）。"""
    import pytest

    _seed = BACKEND.parent / "data" / "seed" / "古典长篇语料.txt"
    if not _seed.exists():
        pytest.skip(f"本地未投放古典长篇语料：{_seed}")
    with open(_seed, encoding="utf-8", errors="replace") as fh:
        _txt = fh.read()
    # 逐 ±4 字窗口模拟「从正文抠出的候选字段值」
    import re as _re
    _hits = []
    for _m in _re.finditer(r"(?:贞观|康熙|乾隆|大观|元丰|建元|永乐|洪武|嘉靖|政和|普通|凤凰|黄龙|如意|甘露|太和)", _txt):
        _w = _txt[max(0, _m.start() - 4):_m.end() + 4]
        if parse_story_time(_w) is not None:
            _hits.append((_w, parse_story_time(_w)))
    assert not _hits, f"古典长篇语料全量 0 误报：{_hits[:5]}"
