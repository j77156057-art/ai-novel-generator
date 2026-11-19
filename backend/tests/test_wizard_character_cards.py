"""引导补角色卡的测试。

覆盖：
- `_attach_character_cards`：批量生成并写回 characters[].card
- 已有声纹卡不重复生成；force=True 才覆盖
- 生成失败只返回 reason，不抛异常（不阻断引导）
- `_char_line` 把分组角色卡（身份/能力/声纹）渲染进角色行
- `character_gender` 兼容平铺 / 分组 / 圣经嵌套三种落点

跑法：pytest tests/test_wizard_character_cards.py -q
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException

from app.generation.prompts import _char_line
from app.generation.character_card import strict_voice_collisions
from app.generation.character_card_gen import generate_cards, style_reference, tone_problem
from app.kg.bible import character_gender
from app.routers.assistant_wizard import (
    _attach_character_cards,
    _commit_message,
    remove_character,
)


class FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


class FakeProvider:
    def __init__(self, payload: dict | None = None, *, boom: bool = False,
                 payloads: list[dict] | None = None) -> None:
        self.payload = payload or {}
        self.boom = boom
        self.calls = 0
        self.prompts: list[tuple[str, str]] = []
        #: 队列：按调用顺序依次返回；空了再回落到 payload。
        self.payloads = list(payloads or [])

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        self.prompts.append((system, user))
        if self.boom:
            raise RuntimeError("channel down")
        if self.payloads:
            return json.dumps(self.payloads.pop(0), ensure_ascii=False)
        return json.dumps(self.payload, ensure_ascii=False)

    @property
    def last_user(self) -> str:
        return self.prompts[-1][1] if self.prompts else ""

    @property
    def first_user(self) -> str:
        return self.prompts[0][1] if self.prompts else ""


def _project() -> SimpleNamespace:
    return SimpleNamespace(
        title="雾港观测者", genre="科幻", style="豆瓣阅读",
        premise="近未来雾港，观测员追查旧案。", plot_bible=None,
    )


def _card(name: str, *, band: str, ending: str, tic: str) -> dict:
    return {
        "name": name,
        "身份": {"性别": "女", "年龄": "27", "外貌": "旧防风外套"},
        "声纹": {
            "自称": "我", "句长": band, "句末": ending,
            "口头禅": [tic], "语气底色": "克制",
        },
    }


def _bible() -> dict:
    return {
        "characters": [
            {"name": "林汐", "role": "夜班观测员", "motivation": "查清姐姐死因"},
            {"name": "林满", "role": "实习技术员", "motivation": "摆脱姐姐保护"},
        ]
    }


def test_attach_cards_writes_back() -> None:
    bible = _bible()
    project = _project()
    db = FakeDB()
    provider = FakeProvider({"characters": [
        _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
        _card("林满", band="中", ending="常用「呢」「吧」", tic="真的假的"),
    ]})

    stats = _attach_character_cards(db, project, bible, provider=provider)

    assert stats["generated"] == 2
    assert set(stats["names"]) == {"林汐", "林满"}
    assert bible["characters"][0]["card"]["声纹"]["口头禅"] == ["记录不会骗人"]
    assert project.plot_bible is bible
    assert db.commits == 1


def test_existing_cards_are_not_regenerated() -> None:
    bible = _bible()
    bible["characters"][0]["card"] = {"声纹": {"自称": "我"}}
    bible["characters"][1]["card"] = {"声纹": {"自称": "我"}}
    project = _project()
    provider = FakeProvider({})

    stats = _attach_character_cards(FakeDB(), project, bible, provider=provider)

    assert stats == {"generated": 0, "names": [], "missing": [], "collisions": 0,
                     "reason": "already_have_cards"}
    assert provider.calls == 0


def test_force_regenerates_existing_card() -> None:
    bible = _bible()
    bible["characters"][0]["card"] = {"声纹": {"自称": "我"}}
    project = _project()
    provider = FakeProvider({"characters": [
        _card("林汐", band="长", ending="常以反问收尾", tic="记录不会骗人"),
        _card("林满", band="中", ending="常用「呢」「吧」", tic="真的假的"),
    ]})

    stats = _attach_character_cards(
        FakeDB(), project, bible, only_names=["林汐"], force=True,
        provider=provider,
    )

    assert stats["generated"] == 1
    assert stats["names"] == ["林汐"]
    assert bible["characters"][0]["card"]["声纹"]["句长"] == "长"
    # 未点名的角色保持原样
    assert "card" not in bible["characters"][1]


def test_provider_failure_is_reported_not_raised() -> None:
    stats = _attach_character_cards(
        FakeDB(), _project(), _bible(),
        provider=FakeProvider(boom=True),
    )
    assert stats["generated"] == 0
    assert stats["reason"].startswith("generate_failed")


def test_missing_characters_are_reported() -> None:
    provider = FakeProvider({"characters": [
        _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
    ]})
    stats = _attach_character_cards(FakeDB(), _project(), _bible(), provider=provider)
    assert stats["generated"] == 1
    assert stats["missing"] == ["林满"]


def _retry_card(name: str, band: str, tic: str) -> dict:
    """语气底色是行为描述、且不带禁用词，避免收尾阶段额外聚焦调用。"""
    return {
        "name": name,
        "身份": {"性别": "女", "年龄": "27", "身份职业": "观测员"},
        "声纹": {
            "自称": "我", "句长": band, "句末": "直陈收尾",
            "口头禅": [tic], "语气底色": "被追问时先复述时间地点，再给结论",
        },
    }


def test_attach_retries_missing_cards_once() -> None:
    """首轮漏掉的角色会再补一轮，成功后 missing 清空。"""
    provider = FakeProvider(payloads=[
        {"characters": [_retry_card("林汐", "短", "记录不会骗人")]},
        {"characters": [_retry_card("林满", "长", "真的假的")]},
    ])
    stats = _attach_character_cards(FakeDB(), _project(), _bible(), provider=provider)

    assert stats["generated"] == 2
    assert stats["missing"] == []
    assert stats["retried"] == ["林满"]
    assert provider.calls == 2


def test_attach_retry_failure_keeps_missing() -> None:
    provider = FakeProvider(payloads=[
        {"characters": [_retry_card("林汐", "短", "记录不会骗人")]},
        {"characters": []},
    ])
    stats = _attach_character_cards(FakeDB(), _project(), _bible(), provider=provider)

    assert stats["generated"] == 1
    assert stats["missing"] == ["林满"]
    assert stats["retried"] == []


def test_user_card_survives_bulk_regeneration() -> None:
    """用户手写的卡是权威：批量重生成不能覆盖。"""
    bible = _bible()
    bible["characters"][0]["card"] = {
        "来源": "user", "声纹": {"自称": "我", "句长": "短"},
    }
    provider = FakeProvider({"characters": [
        _card("林汐", band="长", ending="常以反问收尾", tic="换一句"),
        _card("林满", band="中", ending="常用「呢」「吧」", tic="真的假的"),
    ]})
    stats = _attach_character_cards(
        FakeDB(), _project(), bible, force=True, provider=provider)

    # 只补了林满；林汐的用户卡原样保留
    assert stats["generated"] == 1
    assert bible["characters"][0]["card"]["声纹"]["句长"] == "短"


def test_user_card_can_be_regenerated_by_name() -> None:
    bible = _bible()
    bible["characters"][0]["card"] = {
        "来源": "user", "声纹": {"自称": "我", "句长": "短"},
    }
    provider = FakeProvider({"characters": [
        _card("林汐", band="长", ending="常以反问收尾", tic="换一句"),
    ]})
    stats = _attach_character_cards(
        FakeDB(), _project(), bible, only_names=["林汐"], force=True,
        provider=provider)

    assert stats["generated"] == 1
    assert bible["characters"][0]["card"]["声纹"]["句长"] == "长"


def test_unknown_names_are_reported() -> None:
    stats = _attach_character_cards(
        FakeDB(), _project(), _bible(), only_names=["不存在的人"],
        force=True, provider=FakeProvider({}),
    )
    assert stats["generated"] == 0
    assert stats["reason"] == "unknown_names"


def test_char_line_renders_grouped_card() -> None:
    line = _char_line({
        "name": "林汐",
        "role": "夜班观测员",
        "motivation": "查清姐姐死因",
        "card": _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
    })
    assert "夜班观测员" in line
    assert "自称「我」" in line
    assert "口头禅" in line
    assert "记录不会骗人" in line
    # 性别只由角色行的「性别：女（用她指代）」渲染一次，卡里不重复。
    assert line.count("性别") == 1


def test_char_line_unchanged_without_card() -> None:
    line = _char_line({"name": "林汐", "role": "夜班观测员", "motivation": "查真相"})
    assert line == "  · 林汐（夜班观测员；动机：查真相）"


def test_gender_reads_grouped_and_nested_card() -> None:
    assert character_gender({"性别": "女"}) == "女"
    assert character_gender({"身份": {"性别": "男"}}) == "男"
    assert character_gender({"card": {"身份": {"性别": "女"}}}) == "女"
    assert character_gender({"性别": "unknown"}) == ""


def test_commit_message_reports_card_count_and_collisions() -> None:
    msg = _commit_message("original", None, "outline",
                          {"generated": 7, "collisions": 1, "names": []})
    assert "7 个角色生成角色卡" in msg
    assert "1 处声纹撞车" in msg
    assert "大纲已经生成" in msg


def test_commit_message_silent_when_no_characters() -> None:
    msg = _commit_message("original", None, "outline",
                          {"generated": 0, "collisions": 0, "reason": "no_characters"})
    assert "角色卡" not in msg


class FakeDBWithProject(FakeDB):
    """只实现 `_require_project` 用到的那一个方法。"""

    def __init__(self, project: SimpleNamespace) -> None:
        super().__init__()
        self.project = project

    def get(self, model: object, key: int) -> SimpleNamespace:
        return self.project


def test_remove_character_drops_relationships_and_collisions() -> None:
    bible = _bible()
    bible["relationships"] = [
        {"a": "林汐", "b": "林满", "relation": "姐妹"},
        {"a": "林满", "b": "陈铎", "relation": "同事"},
    ]
    bible["card_collisions"] = [
        {"人物": ["林汐", "林满"], "字段": "句末"},
        {"人物": ["林满", "陈铎"], "字段": "自称"},
    ]
    project = SimpleNamespace(plot_bible=bible)
    out = remove_character(8, "林汐", FakeDBWithProject(project))

    assert out["ok"] is True
    assert out["removed_relationships"] == 1
    assert "移除「林汐」" in out["message"]
    assert [c["name"] for c in project.plot_bible["characters"]] == ["林满"]
    assert project.plot_bible["relationships"] == [
        {"a": "林满", "b": "陈铎", "relation": "同事"}
    ]
    assert project.plot_bible["card_collisions"] == [
        {"人物": ["林满", "陈铎"], "字段": "自称"}
    ]


def test_remove_character_404_when_absent() -> None:
    project = SimpleNamespace(plot_bible=_bible())
    try:
        remove_character(8, "不存在的人", FakeDBWithProject(project))
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("应当 404")


def test_remove_character_keeps_empty_lists() -> None:
    bible = {"characters": [{"name": "林汐"}], "relationships": []}
    project = SimpleNamespace(plot_bible=bible)
    out = remove_character(8, "林汐", FakeDBWithProject(project))

    assert out["removed_relationships"] == 0
    assert project.plot_bible["characters"] == []
    assert project.plot_bible["relationships"] == []
    assert "card_collisions" not in project.plot_bible


def test_remove_character_logs_success(caplog) -> None:
    caplog.set_level(logging.INFO, logger="app.routers.assistant_wizard")
    project = SimpleNamespace(plot_bible=_bible())
    remove_character(8, "林满", FakeDBWithProject(project))

    assert "角色已从剧情圣经移除" in caplog.text
    assert "project_id=8" in caplog.text
    assert "name=林满" in caplog.text
    assert "remaining_characters=1" in caplog.text


def test_remove_character_logs_rejection(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.routers.assistant_wizard")
    project = SimpleNamespace(plot_bible=_bible())
    try:
        remove_character(8, "不存在的人", FakeDBWithProject(project))
    except HTTPException:
        pass
    else:  # pragma: no cover
        raise AssertionError("应当 404")
    assert "角色移除被拒" in caplog.text
    assert "name=不存在的人" in caplog.text


#: 旧格式（rules 平铺）的最小蒸馏档案：项目 5/6 的真实结构。
PROFILE = {
    "rules": {
        "对白风格": [{"rule": "短句为主，话后接动作（证据：“谁加我？”宋书航喃喃道。）"}],
        "角色声音词典": [{"rule": "北河散人：爱用感叹号，自称“我”，称对方“小友”"}],
    },
    "forbidden": {
        "禁用情绪直陈": ["眼中闪过一丝", "心中充满了"],
        "禁用段尾拔高": ["这一刻，他仿佛听到了命运的声音"],
    },
}


def test_style_reference_keeps_dialogue_voice_and_forbidden() -> None:
    ref = style_reference(PROFILE)
    assert "短句为主" in ref
    assert "北河散人" in ref
    assert "眼中闪过一丝" in ref
    assert "禁用情绪直陈" in ref


def test_style_reference_empty_profile_is_empty() -> None:
    assert style_reference(None) == ""
    assert style_reference({}) == ""


def test_style_reference_caps_length() -> None:
    ref = style_reference({"rules": {"对白风格": [{"rule": "很长" * 200}]}},
                          max_chars=100)
    assert len(ref) <= 101


def test_generate_cards_includes_style_reference() -> None:
    provider = FakeProvider({"characters": [
        _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
    ]})
    generate_cards(provider, {"title": "雾港观测者"}, ["林汐"],
                   style_ref="【对白风格】\n- 短句为主，话后接动作")
    assert "参考语料" in provider.first_user
    assert "短句为主" in provider.first_user


def test_attach_cards_uses_project_style_narrative() -> None:
    project = _project()
    project.style_narrative = PROFILE
    provider = FakeProvider({"characters": [
        _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
    ]})
    _attach_character_cards(FakeDB(), project, _bible(), provider=provider)
    assert "北河散人" in provider.first_user
    assert "眼中闪过一丝" in provider.first_user


def test_strict_collisions_detect_duplicate_self_reference() -> None:
    cards = {
        "甲": {"声纹": {"自称": "本座", "句长": "短", "句末": "直陈收尾",
                        "语气底色": "压着声音说"}},
        "乙": {"声纹": {"自称": "本座", "句长": "中", "句末": "常用「呢」「吧」",
                        "语气底色": "先反问再回答"}},
    }
    hits = strict_voice_collisions(cards)
    assert any("自称" in "".join(h["相同"]) for h in hits)


def test_strict_collisions_detect_identical_forbidden_words() -> None:
    cards = {
        "甲": {"声纹": {"自称": "我", "句长": "短", "句末": "直陈收尾",
                        "语气底色": "压着声音说", "禁用词": ["但是", "然后"]}},
        "乙": {"声纹": {"自称": "老苏", "句长": "长", "句末": "常以反问收尾",
                        "语气底色": "先反问再回答", "禁用词": ["但是", "然后"]}},
    }
    hits = strict_voice_collisions(cards)
    assert any("禁用词" in "".join(h["相同"]) for h in hits)


def test_strict_collisions_ignore_generic_self_reference() -> None:
    cards = {
        "甲": {"声纹": {"自称": "我", "句长": "短", "句末": "直陈收尾",
                        "语气底色": "压着声音说"}},
        "乙": {"声纹": {"自称": "我", "句长": "长", "句末": "常以反问收尾",
                        "语气底色": "先反问再回答"}},
    }
    assert strict_voice_collisions(cards) == []


def test_tone_problem_flags_adjectives() -> None:
    assert tone_problem("随和，有点迷糊，喜欢开玩笑")
    assert tone_problem("冷漠")
    assert tone_problem("克制")
    assert tone_problem("")


def test_tone_problem_accepts_behavior() -> None:
    assert tone_problem("压着声音说，先反问再回答") == ""
    assert tone_problem("说话直接，避免拖沓，遇事先观察") == ""


def test_generate_cards_retries_on_tone_label() -> None:
    """语气底色只写「克制」时应触发重写（假 provider 每次都返回同样的卡）。"""
    provider = FakeProvider({"characters": [
        _card("林汐", band="短", ending="直陈收尾", tic="记录不会骗人"),
        _card("林满", band="长", ending="常以反问收尾", tic="真的假的"),
    ]})
    res = generate_cards(provider, {"title": "雾港观测者"}, ["林汐", "林满"])
    assert provider.calls > 1
    assert res["语气问题"]
    assert len(res["角色"]) == 2


def test_generate_cards_clears_identical_forbidden_words() -> None:
    """同批禁用词整套一样时不能靠重写（模型不改），收尾直接清掉。"""
    payload = {"characters": [
        {"name": "甲", "身份": {"性别": "男", "年龄": "30", "身份职业": "专员"},
         "声纹": {"自称": "本座", "句长": "短", "句末": "直陈收尾",
                  "语气底色": "压着声音说", "禁用词": ["但是", "然后"]}},
        {"name": "乙", "身份": {"性别": "男", "年龄": "50", "身份职业": "摊主"},
         "声纹": {"自称": "老苏", "句长": "长", "句末": "常以反问收尾",
                  "语气底色": "先反问再回答", "禁用词": ["但是", "然后"]}},
    ]}
    provider = FakeProvider(payload)
    res = generate_cards(provider, {"title": "本群禁止飞升"}, ["甲", "乙"])

    assert set(res["已清理禁用词"]) == {"甲", "乙"}
    assert res["补写禁用词"] == {}  # 假 provider 第二次仍回整卡，补写拿不到表
    assert res["冲突"] == []
    for card in res["角色"].values():
        assert "禁用词" not in (card.get("声纹") or {})


def test_generate_cards_fills_forbidden_words_via_focused_call() -> None:
    """雷同清单被清掉后，用一次聚焦小调用补回，且过滤规则句/跨角色重复。"""
    first = {"characters": [
        {"name": "甲", "身份": {"性别": "男", "年龄": "30", "身份职业": "专员"},
         "声纹": {"自称": "本座", "句长": "短", "句末": "直陈收尾",
                  "语气底色": "压着声音说", "禁用词": ["亲爱的", "宝贝"]}},
        {"name": "乙", "身份": {"性别": "男", "年龄": "50", "身份职业": "摊主"},
         "声纹": {"自称": "老苏", "句长": "长", "句末": "常以反问收尾",
                  "语气底色": "先反问再回答", "禁用词": ["亲爱的", "宝贝"]}},
    ]}
    second = {"禁用词": {
        "甲": ["亲爱的", "说自己是主角"],
        "乙": ["你说得对", "亲爱的"],
    }}
    provider = FakeProvider(first, payloads=[first, second])
    res = generate_cards(provider, {"title": "本群禁止飞升"}, ["甲", "乙"])

    assert provider.calls == 2
    assert res["补写禁用词"]["甲"] == ["亲爱的"]  # 规则句被过滤
    assert res["补写禁用词"]["乙"] == ["你说得对"]  # 与甲重复的「亲爱的」被过滤
    assert res["角色"]["甲"]["声纹"]["禁用词"] == ["亲爱的"]
    assert res["角色"]["乙"]["声纹"]["禁用词"] == ["你说得对"]


def test_complete_card_applies_finalize_cleanup() -> None:
    """单卡补全也走收尾：套话口头禅删掉、规则句禁用词过滤掉。"""
    from app.generation.character_card_gen import complete_card

    provider = FakeProvider({"characters": [{
        "name": "林汐",
        "身份": {"性别": "女", "年龄": "27", "身份职业": "观测员"},
        "声纹": {"自称": "我", "句长": "短", "句末": "直陈收尾",
                 "语气底色": "压着声音说",
                 "口头禅": ["加油", "把原始波形再调出来"],
                 "禁用词": ["不说爱字", "亲爱的"]},
    }]})
    res = complete_card(provider, {"name": "林汐"})
    voice = res["补充"]["声纹"]

    assert voice["口头禅"] == ["把原始波形再调出来"]
    assert voice["禁用词"] == ["亲爱的"]


def test_finalize_drops_placeholder_tic_and_conflicting_forbidden() -> None:
    """占位口头禅「…」删掉；和口头禅冲突的禁用词也删掉。"""
    from app.generation.character_card_gen import finalize_cards

    cards = {"甲": {"声纹": {
        "自称": "我", "句长": "短", "句末": "直陈收尾",
        "语气底色": "压着声音说",
        "口头禅": ["…", "还钱"], "禁用词": ["还钱", "亲爱的"],
    }}}
    fin = finalize_cards(cards)
    voice = cards["甲"]["声纹"]

    assert voice["口头禅"] == ["还钱"]
    assert voice["禁用词"] == ["亲爱的"]
    assert any(x.get("原因") == "与口头禅冲突" for x in fin["已过滤禁用词"])


def test_finalize_rewrites_label_tone_via_focused_call() -> None:
    """语气底色贴标签时，用一次聚焦调用重写成行为描述。"""
    from app.generation.character_card_gen import finalize_cards

    cards = {"甲": {"声纹": {
        "自称": "我", "句长": "短", "句末": "直陈收尾",
        "语气底色": "温柔，依赖",
        "口头禅": ["先看看"], "禁用词": ["亲爱的"],
    }}}
    provider = FakeProvider({"语气底色": {"甲": "先反问再回答"}})
    fin = finalize_cards(cards, provider=provider, story={"title": "本群禁止飞升"})

    assert cards["甲"]["声纹"]["语气底色"] == "先反问再回答"
    assert fin["语气重写"] == {"甲": "先反问再回答"}


def test_system_prompt_requires_word_style_forbidden_words() -> None:
    """禁用词必须要求写「词本身」，不能写「说自己是主角」这类规则句。"""
    from app.generation.character_card_gen import _SYSTEM

    assert "写成规则说明" in _SYSTEM
    assert "说自己是主角" in _SYSTEM
    assert '"禁用词": ["亲爱的"]' in _SYSTEM


def test_generate_cards_cleans_cliches_duplicate_tic_and_self() -> None:
    """套话口头禅、跨卡重复口头禅、自称撞车都在收尾确定性地处理。"""
    payload = {"characters": [
        {"name": "甲", "身份": {"性别": "男", "年龄": "30", "身份职业": "专员"},
         "声纹": {"自称": "本座", "句长": "短", "句末": "直陈收尾",
                  "语气底色": "压着声音说",
                  "口头禅": ["一切都会好起来的", "先看看"]}},
        {"name": "乙", "身份": {"性别": "男", "年龄": "50", "身份职业": "摊主"},
         "声纹": {"自称": "本座", "句长": "长", "句末": "常以反问收尾",
                  "语气底色": "先反问再回答",
                  "口头禅": ["先看看", "我哪知道"]}},
    ]}
    res = generate_cards(FakeProvider(payload), {"title": "本群禁止飞升"}, ["甲", "乙"])
    cards = res["角色"]

    assert res["自称回退"] == ["乙"]
    assert cards["甲"]["声纹"]["自称"] == "本座"
    assert cards["乙"]["声纹"]["自称"] == "我"
    assert cards["甲"]["声纹"]["口头禅"] == ["先看看"]
    assert cards["乙"]["声纹"]["口头禅"] == ["我哪知道"]
    assert res["冲突"] == []
    assert res["已清理口头禅"]
    assert res["已去重口头禅"]
