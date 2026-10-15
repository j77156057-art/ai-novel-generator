"""角色名归一化：伪角色过滤 + 分词截断/过度切分的语料自证归并。

覆盖：
1. _is_fake_character：占位符（角色A/具体角色名）与高频虚词（相信/时间）被剔除
2. _resolve_name_artifacts：包含关系判据（短名⊂长名 / 长名⊃短名）
3. _extend_name_candidates：后缀扩展判据（莫泽→莫泽修）
4. _build_character_name_index / _normalize_character_name / _dedup_character_voices

合成语料刻意复刻真实结构：名字后有标点/「的」（不能当成交接字符）、
名字出现在另一名字内部（小落⊂安小落）、名字+言说词（夏诺柔声道）。
"""
import sys
sys.path.insert(0, ".")

from app.style.narrative import (
    _build_character_name_index,
    _dedup_character_voices,
    _extend_name_candidates,
    _is_fake_character,
    _normalize_character_name,
    _resolve_name_artifacts,
)

FAILS = []


def check(desc, got, want):
    ok = got == want
    if not ok:
        FAILS.append(desc)
    print(f"{'PASS' if ok else 'FAIL'}  {desc}: got={got!r} want={want!r}")


# 合成语料：名字后接标点/功能字，避免拼接产生假后继
CORPUS = (
    "安小落，" * 100 + "安小落的" * 20      # 安小落=120；后继只有「，」「的」（都在停用集）
    + "南宫爵，" * 90                       # 南宫爵=90
    + "小落道" * 6                          # 小落=6+120(在安小落内)=126，独立 4.8%
    + "冷月，" * 60 + "冷月清，" * 2         # 冷月=62；冷月清=2（仅由冷月支撑）
    + "夏诺，" * 40 + "夏诺柔声道" * 1        # 夏诺=41；夏诺柔=1（仅由夏诺支撑）
    + "莫雨道" * 30 + "莫雨蝶，" * 20         # 莫雨=50；莫雨蝶=20 → 莫雨是真简称，不归并
    + "莫泽修说" * 40 + "莫泽，" * 2          # 莫泽=42，其中 40 次是「莫泽修」→ 后缀扩展
    + "何萱萱，" * 30 + "何萱，" * 1           # 何萱=31 → 何萱萱
)

# ── 1. 伪角色判定 ──
for bad in ["角色A", "角色B", "具体角色名", "具体角色名：安小落", "相信",
            "喜欢", "冷声", "时间", "任何", "简直", "充满", "", "安"]:
    check(f"{bad!r} 是伪角色", _is_fake_character(bad), True)
for good in ["安小落", "南宫爵", "小落", "钟姨", "莫雨蝶"]:
    check(f"{good!r} 是角色", _is_fake_character(good), False)

# ── 2. 包含关系判据 ──
PAIRS = [(n, 0) for n in (
    "安小落", "小落", "南宫爵", "宫爵", "冷月", "冷月清",
    "夏诺", "夏诺柔", "莫雨", "莫雨蝶",
)]
artifacts = _resolve_name_artifacts(PAIRS, CORPUS)
check("宫爵 → 南宫爵（只在长名内出现）", artifacts.get("宫爵"), "南宫爵")
check("小落 → 安小落（独立出现 4.8%）", artifacts.get("小落"), "安小落")
check("冷月清 → 冷月（仅由短名支撑）", artifacts.get("冷月清"), "冷月")
check("夏诺柔 → 夏诺（仅由短名支撑）", artifacts.get("夏诺柔"), "夏诺")
check("莫雨 不被归并（独立出现 60%，是真简称）", "莫雨" in artifacts, False)
check("莫雨蝶 不被归并", "莫雨蝶" in artifacts, False)
check("安小落 保持规范名", "安小落" in artifacts, False)
check("无语料 → 空映射",
      _resolve_name_artifacts([("小落", 0), ("安小落", 0)], ""), {})

# ── 3. 后缀扩展判据 ──
ext = _extend_name_candidates(
    ["安小落", "南宫爵", "莫泽", "何萱", "夏诺", "冷月", "莫雨"], CORPUS
)
check("莫泽 → 莫泽修（95.2% 的后继是「修」）",
      ext.get("莫泽"), "莫泽修")
check("何萱 → 何萱萱", ext.get("何萱"), "何萱萱")
check("安小落 不被扩展（后继是「，」「的」）", "安小落" in ext, False)
check("南宫爵 不被扩展", "南宫爵" in ext, False)
check("夏诺 不被扩展", "夏诺" in ext, False)
check("冷月 不被扩展", "冷月" in ext, False)
check("莫雨 不被扩展（独立出现 60%）", "莫雨" in ext, False)

# ── 4. 归一化（alias 优先，其次子串匹配）──
CANON = [("安小落", 620), ("南宫爵", 580), ("莫泽修", 210), ("冷月", 80)]
ALIAS = {"小落": "安小落", "宫爵": "南宫爵", "莫泽": "莫泽修", "夏诺柔": "夏诺"}
check("小落 → 安小落", _normalize_character_name("小落", CANON, ALIAS), "安小落")
check("宫爵 → 南宫爵", _normalize_character_name("宫爵", CANON, ALIAS), "南宫爵")
check("莫泽 → 莫泽修（alias）", _normalize_character_name("莫泽", CANON, ALIAS), "莫泽修")
check("夏诺柔 → 夏诺（alias，假名归回真名）",
      _normalize_character_name("夏诺柔", CANON, ALIAS), "夏诺")
check("莫泽修 保持（无 alias 命中时的子串兜底不越界）",
      _normalize_character_name("莫泽修", CANON, ALIAS), "莫泽修")
check("冷月 保持冷月（不越界升级）",
      _normalize_character_name("冷月", CANON, ALIAS), "冷月")
check("角色A → None", _normalize_character_name("角色A", CANON, ALIAS), None)
check("时间 → None", _normalize_character_name("时间", CANON, ALIAS), None)
check("无 canonical 时 新角色 原样",
      _normalize_character_name("新角色", None, None), "新角色")

# ── 5. 去重合并：批量路径的简称 + 针对性路径的全名收敛为一条 ──
voices = [
    {"rule": "小落：自称「我」 语气撒娇式 代表台词：「我不管」",
     "novel_id": 1, "chapter_no": 3},
    {"rule": "安小落：自称「我，人家」 口头禅「哎呀」 句长混合 语气撒娇式，"
             "紧张式 关系动态：对南宫爵撒娇 代表台词：「你不知道，喜欢一个人好累」",
     "novel_id": 1, "chapter_no": 12},
    {"rule": "宫爵：自称「本座」 语气命令式 代表台词：「就算我死了」",
     "novel_id": 1, "chapter_no": 8},
    {"rule": "南宫爵：自称「我」 语气命令式/傲慢式 节奏抢话 "
             "关系动态：对安小落占有欲强 代表台词：「我太太是我见过的最漂亮的人」",
     "novel_id": 1, "chapter_no": 20},
    {"rule": "角色A：说话习惯/口头禅/自称（证据：原文）",
     "novel_id": 1, "chapter_no": 1},
    {"rule": "具体角色名：安小落：说话直接（证据：原文）",
     "novel_id": 1, "chapter_no": 2},
    {"rule": "相信：自称「我」 语气试探式", "novel_id": 1, "chapter_no": 5},
]
merged = _dedup_character_voices(voices, CANON, ALIAS)
names = sorted(v["rule"].split("：")[0].strip() for v in merged)
print(f"\n去重后角色: {names}")
check("收敛为 2 个角色", len(merged), 2)
check("名字是规范全名", set(names), {"安小落", "南宫爵"})
for v in merged:
    if v["rule"].startswith("安小落："):
        check("安小落 保留信息更丰富的一条", "人家" in v["rule"], True)
    if v["rule"].startswith("南宫爵："):
        check("南宫爵 保留信息更丰富的一条", "我太太" in v["rule"], True)

# ── 6. 索引构建：speaking 优先 + 伪角色剔除 + 截断/扩展归并 ──
class _Row:
    def __init__(self, name, mentions, speaking):
        self.props = {"name": name, "mentions": mentions, "speaking": speaking}


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self, *_a, **_k):
        return _FakeResult(self._rows)


class _FakeProject:
    id = 1


fake_rows = [
    _Row("安小落", 22542, True),
    _Row("小落", 15508, True),
    _Row("安小", 14016, True),
    _Row("南宫爵", 17934, True),
    _Row("宫爵", 11583, True),
    _Row("时间", 921, True),      # speaking 但为虚词 → 剔除
    _Row("相信", 616, False),     # 高提及但为虚词 → 剔除
    _Row("冷月", 1112, True),
    _Row("莫泽", 3236, True),
    _Row("秦岚", 14, True),
]
kept, alias2 = _build_character_name_index(
    _FakeDB(fake_rows), _FakeProject(), 1, CORPUS
)
kept_names = [n for n, _ in kept]
print(f"\n索引规范名: {kept_names}\nalias: {alias2}")
check("虚词被剔除", any(n in kept_names for n in ("时间", "相信")), False)
check("截断名被剔除", any(n in kept_names for n in ("小落", "安小", "宫爵")), False)
check("全名保留",
      {"安小落", "南宫爵", "冷月", "莫泽修"}.issubset(set(kept_names)), True)
check("截断别名指向全名",
      alias2.get("小落") == "安小落" and alias2.get("宫爵") == "南宫爵", True)
check("扩展别名指向全名", alias2.get("莫泽") == "莫泽修", True)

print(f"\n结果：{'全部通过' if not FAILS else f'{len(FAILS)} 项失败 -> {FAILS}'}")
sys.exit(1 if FAILS else 0)
