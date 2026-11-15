# -*- coding: utf-8 -*-
"""一致性层（app.coherence）单测。

所有用例都用**合成数据**：人名、年份、器物、章号与任何真实作品无关。
这就是本层的验收标准——换一本书，只要提供大纲 / plot_bible / 前文正文 /
实体名，同一套规则仍然成立。
"""
from __future__ import annotations

from app.coherence import build_roster, chapter_index, run_coherence, split_paragraphs


def _findings(res: dict, check: str | None = None) -> list[dict]:
    items = res["findings"]
    return [f for f in items if check is None or f["check"] == check]


def _run(text: str, **kw) -> dict:
    kw.setdefault("plot_bible", {"characters": []})
    kw.setdefault("outline", [])
    return run_coherence(text, **kw)


# ─── 说话人归属 ────────────────────────────────────────────────────────────────

def test_speaker_merged_permission_qa_is_definite():
    """同一对引号里「请示 + 规则式回答」= 两组对话被并进一对引号。"""
    text = (
        "他站在窗口前，把材料递过去。\n\n"
        "「我明天能不能补办？五点以后不行，只能今天。」\n\n"
        "窗口里的人把材料推了回来。"
    )
    res = _run(text)
    hits = _findings(res, "quote_two_turns")
    assert len(hits) == 1
    assert hits[0]["level"] == "definite"
    assert hits[0]["paragraph"] == 2


def test_speaker_self_rhetorical_is_not_flagged():
    """自问自答不是两个人，不能报。"""
    text = "「我总得知道什么时候能走吧？总不能一直在这儿等着。」他说。"
    res = _run(text)
    assert _findings(res, "quote_two_turns") == []


def test_speaker_two_quotes_without_cue_is_suspected():
    """同一段两组引号、人称对调、中间没有提示语 → 疑似切换。"""
    text = "「你先别走。」「我马上回来。」"
    res = _run(text)
    hits = _findings(res, "two_quotes_no_cue")
    assert len(hits) == 1
    assert hits[0]["level"] == "suspected"


def test_speaker_two_quotes_with_name_in_between_is_not_flagged():
    """中间点了人名 = 有归属线索，不报。"""
    bible = {"characters": [{"name": "林昭"}]}
    text = "「你先别走。」林昭把门带上，「我马上回来。」"
    res = _run(text, plot_bible=bible)
    assert _findings(res, "two_quotes_no_cue") == []


# ─── 时间线 ───────────────────────────────────────────────────────────────────

def test_timeline_cross_chapter_relative_vs_absolute():
    """前一章给了绝对年、本章给了同一器物的相对年代，没有锚点时只报疑似，
    并且两处原文都要出现在证据里。"""
    prior = [{"chapter": 1, "text": "那张报纸的日期印着 2003 年。"}]
    text = "他说这张报纸是二十多年前的东西。"
    res = _run(text, chapter=2, prior_chapters=prior)
    hits = _findings(res, "cross_chapter_year_clash")
    assert len(hits) == 1
    assert hits[0]["level"] == "suspected"
    joined = " ".join(
        e["text"] if isinstance(e, dict) else str(e) for e in hits[0]["evidence"])
    assert "2003" in joined and "二十多年前" in joined


def test_timeline_anchor_from_plot_bible_makes_it_definite():
    """plot_bible 里给了故事年份锚点，就能判到 definite。"""
    prior = [{"chapter": 1, "text": "那张报纸的日期印着 2003 年。"}]
    text = "他说这张报纸是二十多年前的东西。"
    bible = {"characters": [], "synopsis": "故事发生在 2003 年。"}
    res = _run(text, chapter=2, prior_chapters=prior, plot_bible=bible)
    hits = _findings(res, "cross_chapter_year_clash")
    assert len(hits) == 1
    assert hits[0]["level"] == "definite"
    assert res["anchors"]["year"] == 2003


def test_timeline_without_any_year_does_not_invent_one():
    """正文一个年份都没有时，不允许凭空造锚点。"""
    res = _run("他推门进去，屋里没有人。")
    assert res["anchors"]["year"] is None
    assert _findings(res, "cross_chapter_year_clash") == []


# ─── 指代 ─────────────────────────────────────────────────────────────────────

def test_reference_missing_antecedent_is_definite():
    text = "他忽然停住：「这东西跟那份契约有什么关系？」"
    res = _run(text)
    hits = _findings(res, "no_antecedent")
    assert len(hits) == 1
    assert hits[0]["level"] == "definite"
    assert "契约" in hits[0]["message"]


def test_reference_with_prior_antecedent_is_clean():
    prior = [{"chapter": 1, "text": "他把那份契约压在箱底。"}]
    text = "他忽然停住：「这东西跟那份契约有什么关系？」"
    res = _run(text, chapter=2, prior_chapters=prior)
    assert _findings(res, "no_antecedent") == []


# ─── 伏笔 / 剧透 ───────────────────────────────────────────────────────────────

def test_foreshadow_future_clue_leak():
    outline = [
        "第一章：他在门口捡到一张收据。",
        "第二章：他去取东西，发现箱子是空的。",
        "第三章：有人跟踪他。",
        "第四章：她交出「铜钥匙」。",
    ]
    text = "他把铜钥匙放在桌上，谁也没说话。"
    res = _run(text, chapter=2, outline=outline)
    hits = _findings(res, "future_clue_leak")
    assert len(hits) == 1
    assert hits[0]["level"] == "suspected"
    assert "铜钥匙" in hits[0]["message"]


def test_foreshadow_chapter_prohibition_is_definite():
    outline = [
        "第一章：他在门口捡到一张收据。",
        "第二章：他去取东西。不写铜钥匙，不提旧案。",
    ]
    text = "他用铜钥匙开了门。"
    res = _run(text, chapter=2, outline=outline)
    hits = _findings(res, "chapter_prohibition")
    assert len(hits) == 1
    assert hits[0]["level"] == "definite"


def test_foreshadow_character_name_is_not_a_clue():
    """大纲里引号括起来的人名不是线索，不能报提前泄露。"""
    outline = [
        "第一章：他出门。",
        "第二章：他回来。",
        "第三章：他遇到「林昭」。",
    ]
    text = "林昭站在门口，没有动。"
    bible = {"characters": [{"name": "林昭"}]}
    res = _run(text, chapter=2, outline=outline, plot_bible=bible)
    assert _findings(res, "future_clue_leak") == []


# ─── 数据驱动 ─────────────────────────────────────────────────────────────────

def test_roster_comes_from_plot_bible_address_values():
    """address 的**值**是「怎么称呼被称呼者」，要挂到被称呼者名下。"""
    bible = {"characters": [
        {"name": "林昭", "voice": {"address": {"沈砚": "沈先生/老沈"}}},
        {"name": "沈砚", "voice": {"address": {"林昭": "小昭", "外人": "您/师傅"}}},
    ]}
    roster = build_roster(bible)
    assert {"沈先生", "老沈"} <= roster["沈砚"]
    assert "小昭" in roster["林昭"]
    assert "您" not in roster["林昭"]
    assert "师傅" not in roster["沈砚"]


def test_chapter_index_reads_label_only():
    assert chapter_index("第2章 别开灯") == 2
    assert chapter_index("第十二章：收网") == 12
    assert chapter_index("段7") is None
    assert chapter_index(None) is None


def test_output_shape_is_stable_and_serializable():
    res = _run("他推门进去。\n\n「你来了。」她说。", chapter=1)
    assert res["version"].startswith("coherence-")
    assert set(res["counts"]) == {"definite", "suspected"}
    assert isinstance(res["findings"], list) and isinstance(res["notes"], list)
    assert set(res["anchors"]) == {"year", "source"}
    assert len(split_paragraphs("甲。\n\n乙。")) == 2
