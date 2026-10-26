"""故事内时间解析：中文年号纪年 → 公元年。

给 Judge 的 impossible_sequences 用（judge.py 只读本模块，不反向依赖）。

核心问题：`in_story_date` 字段的输入有两大类——
  1) 公元年：'2024年3月'、'2020年'（re 一行就够）
  2) 年号纪年：'贞观三年秋'、'康熙元年'、'元丰五年'（需年号表 + 中文数字解析）

误报是本模块的主要风险：红楼梦全文用宽松正则扫出 15 处命中**全是假阳性**——
正则不检查左右边界，会把「老太太太和二奶奶」切成「太和二」、
「金玉如意一柄」切成「如意一」、「宋元丰五年四月眉山苏轼见于秘府」整句吃进来。
所以本模块不靠语义黑名单（太脆），靠三道 fail-closed 边界：
  左边界（年号前只能句首/标点/朝代号）+ 右边界（数字后必须季节词/标点/句尾）
  + 长度（字段值 <=10，即「时间短语」而非叙述句）。

年号表来源：台湾教育部《重编国语辞典》历代年号表，449 个年号 / 520 条记录
（56 个年号在多个朝代重复使用，如「建元」= 西汉/东晋/南齐 三处）。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# --------------------------------------------------------------------------- #
# 年号表
# --------------------------------------------------------------------------- #

_DATA_FILE = Path(__file__).resolve().parent / "data" / "era_names.json"


@lru_cache(maxsize=1)
def _load_eras() -> tuple[tuple[str, tuple[tuple[int, int, str], ...]], ...]:
    """读年号表 → ((年号, ((start, end, 朝代), ...)), ...)。

    同名年号保留全部记录（消歧靠区间命中，见 era_year）。
    """
    raw: dict[str, list[dict]] = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    out = []
    for era, records in raw.items():
        recs = tuple(
            (int(r["start"]), int(r["end"]), str(r.get("dynasty", "")))
            for r in records
            if isinstance(r.get("start"), int) and isinstance(r.get("end"), int)
        )
        if recs:
            out.append((era, recs))
    # 长年号优先（「天册万岁」必须先于「天册」匹配）
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return tuple(out)


_ERA_RE = re.compile(
    "(" + "|".join(re.escape(e) for e, _ in _load_eras()) + r")"
    r"([一二三四五六七八九十百零〇0-9元正]{1,4})年?"
)

# --------------------------------------------------------------------------- #
# 中文数字
# --------------------------------------------------------------------------- #

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}

# 右边界：年号纪年后只接受季节/时序修饰、标点、句尾。
# 量词（般/柄/线/个/只/条…）一律拒绝——「得了凤凰一般」「如意一柄」
_SEASON_TAIL = "春夏秋冬初末间中"
_PUNCT_TAIL = "，。、；：！？）」』\"' \t　.,;:!?)"
# 左边界：年号前只接受朝代词（宋元丰五年）或句首/标点。
_DYN_HEAD = (
    "两汉魏晋南北隋唐五代宋辽金元明清夏周秦"
    "西汉东汉南朝北朝北魏东魏西魏北齐北周武周"
    "后梁后唐后晋后汉后周北宋南宋西夏大明大清"
)
#: 字段最大长度。超出视为「从叙述句里抠出来的片段」而非时间短语
MAX_TIME_LEN = 10


def cn2int(s: str) -> int | None:
    """中文数字串 → int。'三'=3，'十一'=11，'二十三'=23，'一百零八'=108。

    无法解析返回 None（含非数字字符）。单元测试用，勿删。
    """
    if not s:
        return None
    num = 0
    section = 0
    for ch in s:
        if ch in _CN_DIGIT:
            section = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            num += (section or 1) * _CN_UNIT[ch]
            section = 0
        else:
            return None
    num += section
    return num if num > 0 else None


def _num_of(num: str) -> int | None:
    """纪年位 → 序号。元/正=1；阿拉伯数字 <=2 位；否则中文数字解析。"""
    if num in ("元", "正"):
        return 1
    if num.isdigit():
        return int(num) if len(num) <= 2 else None
    return cn2int(num)


def era_year(era: str, n: int) -> int | None:
    """年号+纪年 → 公元年。按区间消歧同名年号。

    「建元」在 3 个朝代复用：建元 6 年只有西汉区间(-140..-135)能容 → -135。
    多个记录都能容（极少）→ 取最小公元年；都不能容 → None（不猜）。
    """
    for era_name, records in _load_eras():
        if era_name != era:
            continue
        hits = [start + n - 1 for start, end, _ in records
                if start <= start + n - 1 <= end]
        if hits:
            return min(hits)
        return None
    return None


def extract_era_name(text: str) -> str | None:
    """取字段值里的年号名（用于相邻同 era 比较的 era 键）。

    公元年形态返回 None——它们与年号事件不构成同一量纲的比较链。
    """
    if not text:
        return None
    m = _ERA_RE.search(text)
    return m.group(1) if m else None


def _left_boundary_ok(s: str, start: int) -> bool:
    """左边界：年号前只能是句首/标点/朝代号（挡「老太太太和二奶奶」）。"""
    head = s[:start]
    if not head:
        return True
    h = head.rstrip(_PUNCT_TAIL)
    if not h:                     # 剥完标点见句首 → 合法
        return True
    # 头部必须落在某个朝代词上（宋元丰五年 / 唐贞观三年）
    return any(h.endswith(d) for d in _DYN_HEAD)


def _right_boundary_ok(s: str, end: int) -> bool:
    """右边界：纪年后必须季节词/标点/句尾（挡「凤凰一般」「如意一柄」）。"""
    tail = s[end:]
    if not tail:
        return True
    return tail[0] in _SEASON_TAIL or tail[0] in _PUNCT_TAIL


def parse_story_time(text: str | None) -> int | None:
    """'贞观三年秋' → 629；'2020年3月' → 2020；无法解析 → None。

    只认年份级精度（月/季节丢弃）。任何歧义/越界/超长一律 None——
    调用方（judge）据此断开比较链，宁漏勿错。
    """
    if not text:
        return None
    text = text.strip()
    # 长度防线：字段值应是「时间短语」而非叙述句
    if len(text) > MAX_TIME_LEN:
        return None
    m = re.search(r"(19|20)\d{2}", text)
    if m:
        return int(m.group(0))
    for m in _ERA_RE.finditer(text):
        if not _left_boundary_ok(text, m.start()):
            continue
        if not _right_boundary_ok(text, m.end()):
            continue
        era, num = m.group(1), m.group(2)
        n = _num_of(num)
        if n is None or n > 1000:
            continue
        year = era_year(era, n)
        if year is not None:
            return year
    return None
