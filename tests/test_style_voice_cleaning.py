"""角色声音词典清污 + 来源隔离（2026-11-11）。

## 背景

项目 5 的 39 条声音词典里，23 条来自《大奉打更人》、16 条来自《豪门宠婚》；
还有「公主」「武夫」「云州」「司天」「董事长」「南总」这类职务词/地名被当成
角色，模型会照着这些「角色」编口吻。规则条目自带 `novel_id`，所以来源隔离
可以放在渲染侧做，不需要重新蒸馏。

## 判据（全部可解释，见 narrative._voice_name_reject_reason）

  1. 名字必须过现有的 `_is_valid_voice_char_name`（2–4 汉字、纯中文、非泛称）；
  2. 整名等于职务/称谓词；
  3. 2–3 字且以地名后缀结尾；
  4. 2–3 字且以职务后缀结尾（南总、李董）；
  5. 与同一份词典里更长的名字构成前缀 → 截断（楚元→楚元缜）；
     后缀同形不判截断（仙子/六修仙子是独立称谓）。

条目形态：40–400 字、引号成对、至少覆盖两个声音字段；否则视为半成品。
A′（2026-11-14）：字段从门槛降为加分项——字段不足的条目看「定向对白证据」：
证据里既有成对引号对白、又点名该角色本人（泛称除外），即可放行。
这是 targeted 提取配角的合法形态（柴杏儿、李灵素）。

## 已知局限（测试里显式钉住，避免以后误以为「已彻底清干净」）

词典里没有全名条目时，截断名（李妙/慕南/小落/何萱）无法被前缀规则抓到——
那需要蒸馏阶段把别名映射落库。本文件不硬编人名映射。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.style.narrative import (  # noqa: E402
    audit_voice_items,
    clean_voice_items,
    describe_narrative,
    filter_rules_by_source,
)


def _voice(name: str, novel_id: int = 1, extra: str = "") -> dict:
    return {
        "novel_id": novel_id,
        "rule": (
            f"{name}：自称「我」，口头禅「罢了」，句长中短，语气克制，"
            f"节奏偏快，关系动态：与同伴互相信任，代表台词：「走吧。」{extra}"
        ),
    }


def test_role_and_place_words_are_dropped():
    items = [_voice("许七安"), _voice("公主"), _voice("武夫"), _voice("国师"), _voice("云州")]
    kept = [it["rule"].split("：")[0] for it in clean_voice_items(items)]
    assert kept == ["许七安"], f"职务词/地名仍在声音词典里：{kept}"


def test_office_suffix_and_whole_word_titles_are_dropped():
    items = [_voice("南总"), _voice("董事长"), _voice("钟姨")]
    kept = [it["rule"].split("：")[0] for it in clean_voice_items(items)]
    assert "南总" not in kept and "董事长" not in kept
    assert "钟姨" in kept, "「姓+称谓」的具体指代被误删，会把真角色一起清掉"


def test_truncated_name_is_dropped_when_full_name_exists():
    items = [_voice("楚元"), _voice("楚元缜")]
    kept = [it["rule"].split("：")[0] for it in clean_voice_items(items)]
    assert kept == ["楚元缜"], "截断名没被识别出来"


def test_suffix_title_is_not_treated_as_truncation():
    """「仙子」与「六修仙子」是独立指代；后缀同形不能当截断删掉。"""
    items = [_voice("仙子"), _voice("六修仙子")]
    kept = [it["rule"].split("：")[0] for it in clean_voice_items(items)]
    assert "仙子" in kept, f"真实称谓被截断规则误删：{kept}"
    assert "六修仙子" in kept


def test_voice_mapping_renders_before_dictionary_and_promotes_source():
    profile = {
        "rules": {
            "角色声音词典": [
                _voice("许七安"), _voice("宫爵"), _voice("周氏阿"), _voice("老板"),
            ],
        },
        "voice_stats": {
            "1|许七安": {"mentions": 100},
            "1|宫爵": {"mentions": 80},
            "1|周氏阿": {"mentions": 10},
            "1|老板": {"mentions": 5},
        },
        "voice_mapping": [{
            "character": "林夜",
            "source_character": "周氏阿",
            "source_novel_id": 1,
            "borrow_point": "借短句与平静威胁",
        }],
        "forbidden": {},
    }
    text = describe_narrative(profile, allowed_novel_ids={1}, mode="original")
    assert "林夜 ← 周氏阿" in text
    assert text.index("【本项目角色声音映射") < text.index("【角色声音词典】")
    dictionary = text.split("【角色声音词典】", 1)[1]
    assert "周氏阿" in dictionary, "映射源没被提升进最终声音词典"


def test_truncated_name_without_full_name_is_kept_and_reported():
    """已知局限：没有全名可比对时不猜（不硬编映射）。"""
    items = [_voice("李妙")]
    assert len(clean_voice_items(items)) == 1
    assert audit_voice_items(items)["kept"] == 1


def test_entry_shape_bounds():
    short = {"novel_id": 1, "rule": "李灵素：语气果断（证据：她走了。）"}
    unbalanced = {
        "novel_id": 1,
        "rule": (
            "许七安：自称「我，口头禅「罢了」，句长中短，语气克制，"
            "节奏偏快，关系动态：与同伴互相信任，代表台词：走吧。"
        ),
    }
    thin = {
        "novel_id": 1,
        "rule": (
            "许七安：语气克制，人物一向冷静，遇事先看四周再开口，"
            "从不把话说满，也从不把情绪写在脸上，习惯用短句把场面压住。"
        ),
    }
    report = audit_voice_items([short, unbalanced, thin])
    assert report["kept"] == 0
    assert report["rejected"].get("长度越界") == 1
    assert report["rejected"].get("引号未闭合") == 1
    assert report["rejected"].get("字段不足") == 1


def test_evidence_backed_thin_entries_are_kept():
    """配角只提得出「一句原文对白」——字段不足也是合法形态，不是半成品。

    原判据（≥40 字 + ≥2 字段）把大奉的柴杏儿、李灵素误删。A′（2026-11-14）
    把 `_VOICE_FIELDS` 降为加分项：只要证据里既有成对引号对白、又点名了
    这个角色本人，就按证据形态放行。
    """
    items = [
        {
            "novel_id": 5,
            "rule": (
                "柴杏儿：私下称李灵素“李郎”，被质疑时迅速转为强硬反问"
                "（证据：“净心大师此言何意？”柴杏儿柳眉轻蹙：“难不成，"
                "你怀疑是我冤枉他，是柴府上下冤枉他？”）"
            ),
        },
        {
            "novel_id": 5,
            "rule": "李灵素：语气平静，内心坚定（证据：李灵素要的就是这句话：“好！”）",
        },
    ]
    kept = [it["rule"].split("：")[0] for it in clean_voice_items(items)]
    assert kept == ["柴杏儿", "李灵素"], f"证据型配角条目被误删：{kept}"


def test_generic_names_are_rejected_even_with_targeted_dialogue():
    """A′ 的泛称排除：证据点名 + 引号对白也不能把职务/群像词放进来。"""
    items = [{
        "novel_id": 5,
        "rule": "狱卒：手里拎着棍子，见谁都要呵斥两句，从不给人好脸色"
                "（证据：狱卒敲了敲栅栏，喝道：“老实点！”）",
    }]
    report = audit_voice_items(items)
    assert report["kept"] == 0, report
    assert report["rejected"].get("泛称") == 1, report


def test_evidence_placeholder_and_short_body_still_rejected():
    """证据太短或正文不足 30 字仍算占位，不能借「证据」蒙混过关。"""
    placeholder = {"novel_id": 5, "rule": "李灵素：语气果断（证据：她走了。）"}
    tiny = {"novel_id": 5, "rule": "李灵素：语气平静（证据：好！）"}
    report = audit_voice_items([placeholder, tiny])
    assert report["kept"] == 0, report
    assert report["rejected"].get("长度越界") == 2, report


def test_titles_and_places_still_dropped_with_evidence_shape():
    """放宽薄条目后，职务词/地名拦截不退化。"""
    def body(name):
        return {
            "novel_id": 5,
            "rule": f"{name}：语气傲慢，句长短句（证据：本官只是一介女流，担不起此等大礼。）",
        }
    items = [body(n) for n in ("公主", "王妃", "国师", "武夫", "司天", "董事长", "南总", "云州")]
    report = audit_voice_items(items)
    assert report["kept"] == 0, report
    assert report["rejected"].get("职务/称谓") == 7, report
    assert report["rejected"].get("地名") == 1, report


def test_source_filter_keeps_only_allowed_books():
    profile = {
        "rules": {
            "角色声音词典": [_voice("许七安", 5), _voice("宫爵", 6)],
            "人物描写手法": [
                {"novel_id": 5, "rule": "先写环境再写人"},
                {"novel_id": 6, "rule": "先写奢侈品再写人"},
                {"rule": "无来源的旧规则"},
            ],
        },
        "forbidden": {"人物描写手法": ["禁止直白说教"]},
    }
    only_five = filter_rules_by_source(profile, {5})
    voice_names = [it["rule"].split("：")[0] for it in only_five["rules"]["角色声音词典"]]
    assert voice_names == ["许七安"]
    bodies = [it["rule"] for it in only_five["rules"]["人物描写手法"]]
    assert bodies == ["先写环境再写人", "无来源的旧规则"], (
        "无来源字段的条目应保守保留（否则旧档案会整块消失）"
    )
    assert only_five["forbidden"] == profile["forbidden"]


def test_describe_narrative_applies_filter_and_cleaning():
    profile = {
        "rules": {
            "角色声音词典": [_voice("许七安", 5), _voice("宫爵", 6), _voice("武夫", 5)],
            "人物描写手法": [{"novel_id": 6, "rule": "先写奢侈品再写人"}],
        },
        "forbidden": {},
    }
    text = describe_narrative(profile, allowed_novel_ids={5})
    assert "许七安" in text
    assert "宫爵" not in text
    assert "武夫" not in text, "职务词仍然被渲染进提示词"
    assert "先写奢侈品" not in text
    # 不传来源 = 保持旧行为（不过滤），但仍清污
    legacy = describe_narrative(profile)
    assert "宫爵" in legacy and "武夫" not in legacy
