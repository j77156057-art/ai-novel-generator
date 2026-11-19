"""v3.0 character 名字后置过滤测试。

LLM 批抽会把描述性短语、量词短语、复数集合、带数字/引号/ASCII 的称谓
写成 character 的 canonical_name（实测项目 6 约 300/1939）。本模块验证
normalize_extraction 的过滤与「亲属描述必须保留」的安全网。

跑法：pytest tests/test_v30_character_name_filter.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg.extractor_v3 import _character_name_reject_reason, normalize_extraction


def _norm(names: list[str]) -> dict:
    return normalize_extraction(
        {"entities": [{"kind": "character", "canonical_name": n} for n in names]}
    )


# —— 拒绝：明确的非人名形态 ——

def test_reject_descriptive_phrase() -> None:
    assert _character_name_reject_reason("一脸不爽的男子") == "descriptive"
    assert _character_name_reject_reason("三个微胖的文员") == "descriptive"
    assert _character_name_reject_reason("穿黑衣的少女") == "descriptive"
    assert _character_name_reject_reason("七修尊者的仆人") == "descriptive"


def test_reject_quantifier_prefix() -> None:
    assert _character_name_reject_reason("一名弟子") == "quantifier_prefix"
    assert _character_name_reject_reason("两人") == "quantifier_prefix"


def test_reject_plural() -> None:
    assert _character_name_reject_reason("修士们") == "plural"
    assert _character_name_reject_reason("乘客等") == "plural"


def test_reject_digit_ascii_quote() -> None:
    assert _character_name_reject_reason("66只金属圣猿") == "digit"
    assert _character_name_reject_reason("3号护卫") == "digit"
    assert _character_name_reject_reason("Alice") == "ascii"
    assert _character_name_reject_reason("‘先生’") == "quote"


def test_reject_generic_role_and_too_long() -> None:
    assert _character_name_reject_reason("文员") == "generic_role"
    assert _character_name_reject_reason("侍卫") == "generic_role"
    assert _character_name_reject_reason("一二三四五六七八九十十一") == "too_long"


def test_reject_generic_roles_and_objects() -> None:
    for name in ("医生", "护士", "警察", "病人", "记者", "队长", "出租车司机"):
        assert _character_name_reject_reason(name) == "generic_role", name
    for name in ("银票", "手机", "快递车", "飞剑"):
        assert _character_name_reject_reason(name) == "generic_object", name


def test_keep_surname_plus_role() -> None:
    """姓+身份是合法指代，不能按后缀误杀。"""
    for name in ("王侍卫", "陈府尹", "林掌柜", "王医生", "陈经理"):
        assert _character_name_reject_reason(name) is None, name


def test_keep_names_starting_with_quantifier_like_surname() -> None:
    """万/全/那 也是姓氏，不能只按首字判量词短语。"""
    for name in ("万天明", "全冠清", "那英"):
        assert _character_name_reject_reason(name) is None, name


# —— 保留：真实人名与亲属描述 ——

def test_keep_real_names_and_kinship() -> None:
    for name in ("林悠然", "顾辰逸", "宋书航", "陈小满",
                 "林悠然的父", "林悠然的父亲", "顾辰逸的母"):
        assert _character_name_reject_reason(name) is None, name


def test_keep_non_character_kinds() -> None:
    """过滤只针对 character，其它 kind 不受影响。"""
    out = normalize_extraction({
        "entities": [
            {"kind": "location", "canonical_name": "一脸不爽的男子"},
            {"kind": "item", "canonical_name": "66只金属圣猿"},
        ]
    })
    assert [e["canonical_name"] for e in out["entities"]] == ["一脸不爽的男子", "66只金属圣猿"]
    assert out["rejected"] == []


# —— normalize_extraction 集成 ——

def test_normalize_drops_and_records_rejected() -> None:
    out = _norm(["林悠然", "一脸不爽的男子", "修士们", "66只金属圣猿"])
    assert [e["canonical_name"] for e in out["entities"]] == ["林悠然"]
    reasons = sorted(r["reason"] for r in out["rejected"])
    assert reasons == [
        "character_name_descriptive",
        "character_name_digit",
        "character_name_plural",
    ]
    assert all(r["stage"] == "entity" and r["raw"] for r in out["rejected"])


def test_normalize_keeps_kinship_entities() -> None:
    out = _norm(["林悠然的父", "林悠然的父亲"])
    assert [e["canonical_name"] for e in out["entities"]] == ["林悠然的父", "林悠然的父亲"]
    assert out["rejected"] == []
