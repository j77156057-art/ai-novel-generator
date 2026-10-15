"""v3.0 Judge 时间锚量纲转换测试（era_convert + check_impossible_sequences 口径 C）。

覆盖：
- parse_story_time：公元年 / 年号纪年 / 元正 / 超区间 / 边界假阳性
- 三类假阳性防线：跨词切碎（左边界）/ 量词误食（右边界）/ 著录长句（长度）
- 同名年号消歧（建元 = 西汉/东晋/南齐）
- 口径 C 判定：同 era 倒挂报 err / 递增不报 / 跨 era 换年号不报 /
  混排两处倒挂全检出 / 解析失败断链 / 旧 4 位公元年行为保留
- 红楼梦全量回归：0 误报

跑法：python tests/test_era_dimension.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.era_convert import cn2int, era_year, extract_era_name, parse_story_time
from app.kg.judge import check_impossible_sequences
from app.models import KgEvent

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


# ====================================================================
# 1. cn2int 中文数字
# ====================================================================
check("cn2int 三", cn2int("三"), 3)
check("cn2int 十", cn2int("十"), 10)
check("cn2int 十一", cn2int("十一"), 11)
check("cn2int 二十三", cn2int("二十三"), 23)
check("cn2int 一百零八", cn2int("一百零八"), 108)
check("cn2int 空串", cn2int(""), None)
check("cn2int 非数字", cn2int("三年"), None)

# ====================================================================
# 2. parse_story_time 真阳性
# ====================================================================
check("公元年 2020年3月", parse_story_time("2020年3月"), 2020)
check("公元年 裸 2020", parse_story_time("2020"), 2020)
check("贞观三年秋", parse_story_time("贞观三年秋"), 629)   # 627 + 3 - 1
check("贞观三年", parse_story_time("贞观三年"), 629)
check("康熙元年", parse_story_time("康熙元年"), 1662)       # 1662 + 1 - 1
check("大观四年", parse_story_time("大观四年"), 1110)       # 1107 + 4 - 1
check("元丰五年", parse_story_time("元丰五年"), 1082)       # 1078 + 5 - 1
check("乾隆六十年", parse_story_time("乾隆六十年"), 1795)   # 1736 + 60 - 1
check("嘉靖四十五年", parse_story_time("嘉靖四十五年"), 1566)
check("政和二年春", parse_story_time("政和二年春"), 1112)
check("贞观十八年", parse_story_time("贞观十八年"), 644)   # 627 + 18 - 1

# ====================================================================
# 3. 假阳性防线（红楼梦实测成因）
# ====================================================================
# 3a 左边界：跨词切碎
check("跨词 老太太和二奶奶", parse_story_time("老太太和二奶奶办的"), None)
check("跨词 亲家太太和三妹妹", parse_story_time("亲家太太和三妹妹"), None)
check("跨词 总理元始三一教门", parse_story_time("总理元始三一教门"), None)
# 3b 右边界：量词误食
check("量词 凤凰一般", parse_story_time("得了凤凰一般"), None)
check("量词 如意一柄", parse_story_time("金玉如意一柄"), None)
check("量词 黄龙一线", parse_story_time("剑斩黄龙一线儿差"), None)
# 3c 长度防线：著录长句
check("著录长句", parse_story_time("宋元丰五年四月眉山苏轼见于秘府"), None)
check("长度防线 11 字真超限", parse_story_time("唐贞观三年秋月春风花落"), None)   # 11 字符
check("长度边界 10 字仍解析", parse_story_time("唐贞观三年秋月春"), 629)         # 8 字符，合法
# 3d 园林/场景
check("大观园试才题对额", parse_story_time("大观园试才题对额"), None)
check("只管普通一请", parse_story_time("只管普通一请"), None)
check("形容词 似非大观", parse_story_time("似非大观"), None)
# 3e 空/无匹配
check("空串", parse_story_time(""), None)
check("None", parse_story_time(None), None)
check("不详", parse_story_time("不详"), None)
# 3f 超区间
check("超区间 嘉靖六十年", parse_story_time("嘉靖六十年"), None)  # 嘉靖最长 45

# ====================================================================
# 4. 同名年号消歧
# ====================================================================
# 建元在 3 个朝代复用：西汉(-140..-135) / 东晋(343..344) / 南齐(479..482)。
# 「建元六年」只有西汉区间能容 → -135（唯一解）
check("建元六年（西汉唯一解）", era_year("建元", 6), -135)
# 「建元三年」西汉(-140+3-1=-138) 与东晋(343+3-1=345) 都能容 → 按设计取最小公元年
check("建元三年（西汉+东晋都能容→取最小）", era_year("建元", 3), -138)
# 「建元四年」西汉(-137) 与东晋(346) 都能容 → 同样取最小
check("建元四年（都能容→最小）", era_year("建元", 4), -137)
# 超出全部区间 → None
check("建元十年超所有区间", era_year("建元", 10), None)
# 东晋独有区间才能验东晋记录真的在表里：建元 343..344 → 元年/二年
check("建元二年（西汉+东晋都能容→最小）", era_year("建元", 2), -139)
check("贞观三年", era_year("贞观", 3), 629)
check("贞观二十三年", era_year("贞观", 23), 649)           # 唐贞观末

# ====================================================================
# 5. extract_era_name
# ====================================================================
check("extract 贞观三年秋", extract_era_name("贞观三年秋"), "贞观")
check("extract 康熙元", extract_era_name("康熙元"), "康熙")
check("extract 公元年", extract_era_name("2024年3月"), None)
check("extract 空", extract_era_name(""), None)

# ====================================================================
# 6. check_impossible_sequences 口径 C
# ====================================================================
engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()


def add_events(pairs: list[tuple[int, str, str]]) -> None:
    """pairs: [(sequence, in_story_date, summary)]"""
    for seq, date, summary in pairs:
        db.add(KgEvent(project_id=1, sequence=seq, event_type="incident",
                       summary=summary, in_story_date=date))
    db.commit()


# —— 6a 同 era 倒挂 → err（核心增量：原实现完全不判年号）——
add_events([
    (10, "贞观三年秋", "甲"),
    (11, "贞观元年春", "乙"),
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("同 era 倒挂报 err", len(f), 1)
check_true("同 era 倒挂 rule 正确", f and f[0].rule == "impossible_sequences")
check_true("同 era 倒挂 severity err", f and f[0].severity == "err")

# —— 6b 同 era 递增 → 不报 ——
db.query(KgEvent).delete()
db.commit()
add_events([
    (20, "贞观元年春", "丙"),
    (21, "贞观三年秋", "丁"),
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("同 era 递增不报", len(f), 0)

# —— 6c 跨 era 换年号 → 不报（贞观末→永徽元是正常历史推进）——
db.query(KgEvent).delete()
db.commit()
add_events([
    (30, "贞观二十三年", "戊"),
    (31, "永徽元年", "己"),
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("跨 era 换年号不报", len(f), 0)

# —— 6d 混排贞观/永徽/贞观 两处真倒挂全检出（口径 A 会漏第 2 处）——
db.query(KgEvent).delete()
db.commit()
add_events([
    (40, "贞观三年秋", "庚"),     # 629
    (41, "贞观元年春", "辛"),     # 627  ← 倒挂 1
    (42, "永徽四年", "壬"),       # 653
    (43, "永徽二年", "癸"),       # 651  ← 倒挂 2
    (44, "贞观五年", "子"),       # 631（跨 era，不比）
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("混排两处倒挂全检出", len(f), 2)

# —— 6e 解析失败断链：中间插不可解析事件 → 前后不跨接比较 ——
db.query(KgEvent).delete()
db.commit()
add_events([
    (50, "贞观三年秋", "丑"),     # 629
    (51, "时间不明", "寅"),        # None → 断链
    (52, "贞观元年春", "卯"),     # 627
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("解析失败断链不报", len(f), 0)

# —— 6f 旧行为保留：4 位公元年递减/递增 ——
db.query(KgEvent).delete()
db.commit()
add_events([
    (60, "2020年3月", "辰"),
    (61, "2018年5月", "巳"),
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("公元年递减报 err", len(f), 1)

db.query(KgEvent).delete()
db.commit()
add_events([
    (70, "2021年", "午"),
    (71, "2022年", "未"),
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("公元年递增不报", len(f), 0)

# —— 6g 空 in_story_date 跳过 ——
db.query(KgEvent).delete()
db.commit()
add_events([
    (80, "", "申"),
    (81, "贞观三年秋", "酉"),
    (82, "贞观元年春", "戌"),   # 这一对应该报
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("空日期跳过 + 后续对仍判", len(f), 1)

# —— 6h 乱序输入（sequence 非升序落库）——judge 必须自排序 ——
db.query(KgEvent).delete()
db.commit()
add_events([
    (92, "贞观元年春", "亥"),   # 627（后落库但 sequence 大）
    (91, "贞观三年秋", "甲"),   # 629
])
f = check_impossible_sequences(db, 1, "", segment_no=1)
check("乱序落库按 sequence 排序后判", len(f), 1)

# ====================================================================
# 7. 红楼梦全量回归（防假阳性）
# ====================================================================
_seed = BACKEND.parent / "data" / "seed" / "红楼梦.txt"
if _seed.exists():
    with open(_seed, encoding="utf-8", errors="replace") as fh:
        _txt = fh.read()
    # 逐 ±4 字窗口模拟「从正文抠出的候选字段值」
    import re as _re
    _hits = 0
    for _m in _re.finditer(r"(?:贞观|康熙|乾隆|大观|元丰|建元|永乐|洪武|嘉靖|政和|普通|凤凰|黄龙|如意|甘露|太和)", _txt):
        _w = _txt[max(0, _m.start() - 4):_m.end() + 4]
        if parse_story_time(_w) is not None:
            _hits += 1
            print(f"    误报: {_w!r} -> {parse_story_time(_w)}")
    check("红楼梦全量 0 误报", _hits, 0)
else:
    print(f"SKIP  红楼梦种子不存在: {_seed}")

# ====================================================================
print()
if FAILED:
    print(f"结果：{PASS} 通过, {FAIL} 失败 -> {FAILED}")
    sys.exit(1)
print(f"结果：全部通过（{PASS} 断言）")
