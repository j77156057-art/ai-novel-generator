"""beat 插入的四道判据（`app/style/beat_insert.py`）

## 验的是什么

插入的输出是**模型写的内容** —— 它会进落库正文。所以四道判据必须真的能挡：

1. **长度** 10~30 字（真书 C 类引号前叙述 p25~p75）；
2. **不含引号**（不能写台词 —— 那是改写不是插入）；
3. **不含新专名**（不能凭空造人名/地名/物件 —— 没有事实依据）；
4. **像叙述**（含动作/神态动词）。

以及：
- 一个位置失败**不影响**别的位置；
- 全部失败 → **原文返回**（不落库半成品）。

## 用假 provider
"""
import os
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.style.beat_insert import _insert_beats, _looks_like_narration, _clean_beat  # noqa: E402


class _Provider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.temps = []

    def chat(self, system, user, **kw):
        self.calls += 1
        self.temps.append(kw.get("temperature"))
        r = self.replies[self.calls - 1] if self.calls - 1 < len(self.replies) else self.replies[-1]
        if isinstance(r, Exception):
            raise r
        return r


BODY = (
    "老周蹲在马厩里擦马镫。\n\n"
    "“世子想听实话？”\n\n"
    "“你跟着我这么多年，还不知道我从不问自己会心疼的账？”\n\n"
    "“原来不在他身上。”"
)


# ── 长度判据 ─────────────────────────────────────────────────────────────
def test_rejects_too_short():
    p = _Provider(["他抬头。"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 0
    assert any("长度" in r["reason"] for r in info["rejected"])


def test_rejects_too_long():
    p = _Provider(["老周抬起头看了看门外的天色，然后慢慢地把手里的旧布叠好"
                   "放回腰间，又低下头继续去擦那副已经被磨得发亮的马镫。"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 0
    assert any("长度" in r["reason"] for r in info["rejected"])


# ── 引号判据（不能写台词）────────────────────────────────────────────────
def test_rejects_quote_in_beat():
    p = _Provider(["老周说：“这是军马。”"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 0
    assert any("引号" in r["reason"] for r in info["rejected"])


# ── 新专名判据（不能凭空造事实）──────────────────────────────────────────
#: 参考书的专名表（真书项目里 `entity_store` 的表就是这个形状）。
PROPER_NOUNS = {"陆长风", "老周", "北荒", "王府", "陆崇", "北荒王"}


def test_rejects_declared_proper_noun_not_in_table():
    """模型**自报**了表里没有的专名 ⇒ 拒。

    判据的关键设计：让**模型自己声明**它用了什么专名，
    服务端只管**查表** —— 不用猜「什么算专名」（中文没词边界，猜不准）。
    """
    p = _Provider(["他转头望向京城的方向。\n专名：京城"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0, proper_nouns=PROPER_NOUNS)
    assert info["inserted"] == 0
    assert any("专名" in r["reason"] for r in info["rejected"])


def test_allows_declared_proper_noun_in_table():
    """自报的专名在表里 ⇒ 放行。

    （句子要够长 —— 低于 10 字会被长度判据先拒掉，那是对的。）
    """
    p = _Provider([
        "老周没有抬头，手里的旧布在马镫上来回蹭着。\n专名：老周",
        "陆长风把袖口拢了拢，指尖在布边停住。\n专名：陆长风",
        "陆崇将手按在木槽上，抬眼看去。\n专名：陆崇",
    ])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0, proper_nouns=PROPER_NOUNS)
    assert info["inserted"] == 3


def test_rejects_numbers():
    """beat 里出现**数字** ⇒ 拒（新事实的来源）。

    真书的 beat 几乎不报数；报数就是新事实（新的数量/物件）概率最高的地方。
    """
    p = _Provider(["他转头望向那边的十二匹青灰马。"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0, proper_nouns=PROPER_NOUNS)
    assert info["inserted"] == 0
    assert any("数字" in r["reason"] for r in info["rejected"])


def test_plain_action_is_accepted():
    """正常的动作/神态叙述 ⇒ 放行（不误杀）。"""
    p = _Provider([
        "老周没有抬头，手里的旧布在马镫上来回蹭着。",
        "陆长风把袖口拢了拢，指尖在布边停住。",
        "陆崇将手按在木槽上，抬眼看去。",
    ])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0, proper_nouns=PROPER_NOUNS)
    assert info["inserted"] == 3
    assert "proper_noun_source" in info


def test_declared_noun_line_is_stripped_from_beat():
    """「专名：」那行不能进正文。"""
    p = _Provider(["老周没有抬头。\n专名：老周"])
    text, info = _insert_beats(BODY, p, target_ratio=0.0, proper_nouns=PROPER_NOUNS)
    assert "专名：" not in text


# ── 像叙述吗 ─────────────────────────────────────────────────────────────
def test_rejects_non_narrative():
    p = _Provider(["因为军马吃草，所以私马也得吃草。"])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 0
    assert any("不像叙述" in r["reason"] for r in info["rejected"])


# ── 合格就插 ─────────────────────────────────────────────────────────────
def test_accepts_good_beat():
    # 三处候选给**三句不同**的话（判据 4 会拒重复，见下面那个测试）
    p = _Provider([
        "老周没有抬头，手里的旧布在马镫上来回蹭着。",
        "陆长风把袖口拢了拢，指尖在布边停住。",
        "门外的风把草料吹散了几根，陆崇弯腰拾起。",
    ])
    text, info = _insert_beats(BODY, p, target_ratio=0.0)
    # target_ratio=0.0 ⇒ 三段 A 类全是候选（3 处）⇒ 全部插入
    assert info["inserted"] == 3
    assert info["slots"] == 3
    assert "老周没有抬头" in text
    # 插入后那段变成 C 类（**并进**那一段，不是另起一段）
    from app.style.beat import has_action

    assert any(has_action(x) for x in text.split("\n\n"))
    # 段数不变（beat 是并进去的）
    assert len([x for x in text.split("\n\n") if x.strip()]) == len(
        [x for x in BODY.split("\n\n") if x.strip()]
    )


def test_uses_low_temperature():
    p = _Provider(["老周没有抬头。"])
    _insert_beats(BODY, p, target_ratio=0.0)
    assert p.temps and all(t is not None and t <= 0.3 for t in p.temps)


# ── 一个失败不影响别的 ───────────────────────────────────────────────────
def test_one_failure_does_not_block_others():
    p = _Provider([
        "太短。",                                     # 第 1 处：长度不过
        "老周没有抬头，手里的旧布在马镫上来回蹭着。",  # 第 2 处：合格
        "陆长风把袖口拢了拢，指尖在布边停住。",       # 第 3 处：合格
    ])
    text, info = _insert_beats(BODY, p, target_ratio=0.0)
    # 第 1 处长度不过 → 拒；其余的插入
    assert info["inserted"] == 2
    assert len(info["rejected"]) == 1


def test_all_failed_returns_original():
    p = _Provider(["太短。"] * 6)
    text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 0
    assert not info["applied"]
    assert text == BODY, "全部失败时必须原文返回"


def test_provider_exception_is_contained():
    p = _Provider([
        RuntimeError("boom"),
        "老周没有抬头，手里的旧布在马镫上来回蹭着。",
        "陆长风把袖口拢了拢，指尖在布边停住。",
    ])
    text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 2


def test_no_slots_returns_original():
    p = _Provider([])
    text, info = _insert_beats(BODY, p, target_ratio=0.99)
    assert p.calls == 0
    assert info["slots"] == 0
    assert text == BODY


# ── 清洗输出 ─────────────────────────────────────────────────────────────
def test_clean_beat_strips_quotes_and_extra_lines():
    assert _clean_beat("“老周没有抬头。”") == "老周没有抬头。"
    assert _clean_beat("老周没有抬头。\n解释：这是动作。") == "老周没有抬头。"
    assert _clean_beat("  ") == ""


def test_looks_like_narration():
    assert _looks_like_narration("老周抬起头。")
    assert not _looks_like_narration("因为军马吃草，所以私马也得吃草。")
    assert not _looks_like_narration("“带引号的”。")


# ── 判据 2 的误杀修复（真跑实测误杀率 33%）──────────────────────────────
def test_narration_judge_does_not_kill_valid_action_lines():
    """真跑（完整流程）里这两条**合格的 beat 被误杀**了。

    原判据要求「必须命中动作动词白名单」，白名单漏一个词就杀 ——
    实测 6 条候选里误杀 2 条（33%）。
    ⇒ 改成「默认是叙述，只在明确是说理时才拒」。
    """
    # 真跑原句：没有白名单里的动词，但都是标准的动作/神态叙述
    assert _looks_like_narration("拢着湿布的手微微收紧，指节泛白。")
    assert _looks_like_narration("陆长风眉梢微动，指尖搭在黄绢边角。")


def test_narration_judge_still_rejects_reasoning():
    """说理/议论仍然要拒 —— 它不是「动作或神态」。"""
    assert not _looks_like_narration("因为军马吃草，所以私马也得吃草。")
    assert not _looks_like_narration("显然，这笔账是要算清楚的。")


# ── 判据 4：不能跟已插入的重复（真跑实测 4 条里 2 条几乎一样）──────────
def test_duplicate_beats_are_rejected():
    """真跑原句：两条 beat 几乎一样（公共前缀 4 字），读起来像复读。"""
    p = _Provider([
        "他垂下眼，指尖在袖口边停了停。",
        "他垂下眼，手指在袖口上停了停。",
        "陆崇将手按在木槽上，抬眼看去。",
    ])
    _text, info = _insert_beats(BODY, p, target_ratio=0.0)
    assert info["inserted"] == 2, "第二条与第一条重复，应当被拒"
    assert any("重复" in r["reason"] for r in info["rejected"])


def test_find_duplicate_unit():
    from app.style.beat_insert import _find_duplicate

    a = "他垂下眼，指尖在袖口边停了停。"
    b = "他垂下眼，手指在袖口上停了停。"
    c = "陆崇将手按在木槽上，抬眼看去。"
    assert _find_duplicate(b, [a])
    assert not _find_duplicate(c, [a])
    assert not _find_duplicate(a, [])


# ── 判据 5②：不能跟插入点附近的原文重复 ────────────────────────────────
def test_beat_must_not_duplicate_adjacent_original():
    """真跑原句：beat 跟**紧邻的原文段**做了同一个动作。

    原文：「陆崇将手中的蜡书放在木槽上。」
    beat：「陆崇将手按在木槽上，抬眼看去。」
    ⇒ 相邻两段连着往木槽上放东西，读起来像卡带。
    """
    from app.style.beat_insert import _too_similar_to_context

    beat = "陆崇将手按在木槽上，抬眼看去。"
    ctx = "陆崇将手中的蜡书放在木槽上。"
    assert _too_similar_to_context(beat, [ctx])
    # 干净的 beat 不该被误杀
    assert not _too_similar_to_context("袖口忽然绷紧，又缓缓松开。", [ctx])
    assert not _too_similar_to_context("老周没有抬头，手里的旧布在马镫上来回蹭着。", [ctx])


def test_context_duplicate_is_rejected_in_insert():
    """走完整流程：上下文重复的那条要被拒，其余照插。"""
    body = (
        "陆崇将手中的蜡书放在木槽上。\n\n"
        "“京中来了密旨。”\n\n"
        "老周蹲在栏边，正给一匹枣红马刷去肩上的干泥。\n\n"
        "“世子想听实话？”"
    )
    p = _Provider([
        "陆崇将手按在木槽上，抬眼看去。",   # 与紧邻原文重复 ⇒ 拒
        "老周停下刷子，抬眼看向门口。",     # 干净 ⇒ 插
    ])
    _text, info = _insert_beats(body, p, target_ratio=0.0)
    assert info["inserted"] == 1, info
    assert any("上下文重复" in r["reason"] for r in info["rejected"])
