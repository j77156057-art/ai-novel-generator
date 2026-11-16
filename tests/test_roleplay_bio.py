"""角色生平：证据回填 + 生平时间线。

为什么这两块都要：
- 档案的 characters 层**只抽前 N 个主要角色**（北荒刀歌 8 个：陆长风/陆崇/赵磐石/
  楚惊蛰/陆北枝/小宁/裴长卿/赵青锋）—— **老周不在里面** ⇒ 用户在角色对话里
  选了他，他有**声音**却**没有档案**（身份/行为倾向/底线/关键经历全没有）。
- 而老档案即使有，也**过不了门禁**（`evidence_status` 缺失 ⇒ 整块渲染成空串）。

⇒ 档案在 ⇒ 证据回填后能用；档案不在 ⇒ 生平时间线兜底（机械层，零模型调用）。
"""
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.roleplay.bio import _event_candidate, render_timeline  # noqa: E402
from app.style.bio_evidence import (  # noqa: E402
    backfill_profile,
    build_corpus,
    verify_claims,
)


# ── 生平时间线：事件必须是「完整的一句话」 ──────────────────────────────
def test_event_is_a_whole_sentence():
    """裁半句 ⇒ 事件是残的（真跑踩过：「恶奴就更没有了，」）。"""
    line = "两人咧着嘴笑，一个流着口水，一个缺了门牙，都挺傻。老周也跟着乐，还拿手比划着。"
    got = _event_candidate(line, "老周")
    assert got is not None
    assert got.endswith(("。", "！", "？", "…")), got


def test_event_skips_broken_dialogue():
    """对白被切在引号里 ⇒ 不算完整句。"""
    line = "轻声道：“小宁，有机会，你应该出去看一看。"
    assert _event_candidate(line, "小宁") is None


def test_event_skips_colon_ending():
    """以冒号结尾 ⇒ 不是完整句（「…嬉笑道：」）。"""
    line = "酒劲上了头，陆长风醉眼朦胧指了指小宁，再点了点鱼幼薇，嬉笑道："
    assert _event_candidate(line, "小宁") is None


def test_event_skips_name_inside_quotes():
    """名字在引号内 ⇒ 那是**别人说他的**，不是他做的事。"""
    line = "陆长风笑道：“老周，你说楚惊蛰这小子成天就想着练剑。”"
    assert _event_candidate(line, "老周") is None


def test_event_prefers_name_early():
    """名字出现得越早，越像「他做的事」。"""
    line = (
        "老周伸手比划了一下与陆长风身高差不多的高度，大概意思就是我是这样高的高手。"
        "后来老周又说了些别的。"
    )
    got = _event_candidate(line, "老周")
    assert got is not None
    assert got.startswith("老周"), got


def test_render_timeline():
    tl = [{"chapter_no": 13, "event": "老周咧嘴一笑。"},
          {"chapter_no": 14, "event": "老周嚼着小草。"}]
    out = render_timeline(tl)
    assert "第13章" in out and "第14章" in out
    assert render_timeline([]) == ""


# ── 证据回填 ────────────────────────────────────────────────────────────
def _corpus(lines):
    corpus, starts = build_corpus(lines)
    return corpus, starts


def test_verify_claims_locates_event_phrase():
    lines = [
        (1, "裴长卿九岁奉召入内廷，以对弈生死命题获得西楚皇帝赐名。"),
        (2, "陆长风啃着黄瓜，没说话。"),
    ]
    corpus, starts = _corpus(lines)
    profile = {"name": "裴长卿", "关键经历": ["九岁奉召入内廷，得赐名曹家小得意"]}
    accepted, rejected = verify_claims(profile, corpus, starts, lines)
    assert len(accepted) == 1, (accepted, rejected)
    assert accepted[0].phrase.startswith("九岁")
    assert accepted[0].chapter_no == 1
    assert rejected == []


def test_verify_claims_drops_unsupported():
    """书里没有的事件 ⇒ 丢掉（宁可少给，不可给错）。"""
    lines = [(1, "老周蹲在马厩里擦马镫。"), (2, "陆长风啃着黄瓜。")]
    corpus, starts = _corpus(lines)
    profile = {"name": "老周", "关键经历": ["他当年在北荒边城连败三大高手"]}
    accepted, rejected = verify_claims(profile, corpus, starts, lines)
    assert accepted == []
    assert len(rejected) == 1


def test_backfill_makes_legacy_profile_renderable():
    """老档案没有 claim 账本 ⇒ 门禁挡住 ⇒ 整块渲染成空串。回填后要能渲染。"""
    from app.style.character_profile import render_character_profile

    lines = [
        (1, "老周咧嘴傻笑，没门牙的老周真是可爱。"),
        (2, "老周把剑匣垫在屁股底下坐着。"),
    ]
    corpus, starts = _corpus(lines)
    legacy = {
        "name": "老周",
        "身份": "北荒王府的老仆",
        "关键经历": ["老周咧嘴傻笑"],
    }
    assert render_character_profile(legacy) == "", "老档案本该被门禁挡住"

    filled = backfill_profile(legacy, corpus, starts, lines)
    assert filled["evidence_status"] == "located_extracts_not_semantic_proof"
    rendered = render_character_profile(filled)
    assert "老周" in rendered, rendered
    assert "原文" in rendered, rendered
