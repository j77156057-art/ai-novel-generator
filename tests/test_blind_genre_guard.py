"""盲评包的**题材指纹**闸门（2026-10-31 补）。

==============================================================================
起因
==============================================================================

18 题的盲评包出完后，主人只读了一两题就说：

> 「一个厉鬼的，另外一个不知道什么题材，那么只需要判断一两个，
> 根据题材大概都知道哪个是人写的」

⇒ 泄露的不是**名字**（名字已经全遮了），是**题材指纹**：
人类侧那本满篇「厉鬼 / 复苏 / 规则」这套灵异术语，我们侧是都市市井悬疑
—— 评者不看文风就能分出哪边是出版物。

⚠️ **年代闸门没抓到**：它只分了「古风 / 现代」两桶，
而这次泄露是**同代不同题材**（都是现代）。

==============================================================================
两道东西
==============================================================================

1. **题材判定**（`acceptance_lib.judge_genres`）：让模型读正文判题材。
   ⚠️ 词表口径实测太弱（18 本语料里「剑/刀/江湖/系统/掌柜」这类词在过半的
   书里都出现 ⇒ 现代都市小说被误判成古风武侠，9/18 判错），
   所以**权威口径是模型**，词表只作粗筛。
2. **多题材臂出包**（`verify_acceptance_real.build_multi_arm_packet`）：
   每一题两侧**同题材**，题与题之间题材乱着来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from acceptance_lib import (  # noqa: E402
    GENRE_CLASSES,
    GENRE_UNKNOWN,
    genre_dominant,
    genre_distance,
    genre_mismatch,
    genre_profile,
    judge_genres,
)
from test_blind_packet_guard import _StubJudge  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. 题材指纹词表（粗筛口径）：认得出「这是哪一类故事」
# --------------------------------------------------------------------------- #

def test_题材指纹认得出灵异术语():
    """阳性对照：尺必须会响。"""
    text = ("厉鬼复苏，红纸遮住鬼眼，驭鬼者在收容所里值夜。"
            "规则写着不能回头看，活人只能走完最后一程。" * 3)
    p = genre_profile(text)
    assert p["灵异克苏鲁"] > 0, p
    assert genre_dominant(p) in ("灵异克苏鲁", "规则怪谈"), p


def test_题材指纹认得出都市市井():
    text = ("巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上。"
            "他拎着粉笔在墙上划了一道，楼道里的声控灯亮了一下。" * 4)
    p = genre_profile(text)
    assert p["都市市井"] > 0, p
    assert genre_dominant(p) == "都市市井", p


def test_题材指纹认得出古风():
    text = ("他以内力震开剑锋，门派的长老在旁冷哼。"
            "江湖上谁都知道那柄刀的来历，镖局的镖师已经退到墙根。" * 3)
    assert genre_dominant(genre_profile(text)) == "古风武侠"


def test_词表不再把现代小说误判成古风():
    """回归：单字标记（「刀」「剑」「江湖」）会把现代都市小说染成古风。

    实测 2026-10-31：18 本语料里有 9 本被旧词表判成「古风武侠」。
    ⇒ 词表里只留区分度高的词。
    """
    modern = ("他掏出手机叫了外卖，监控和卷宗都调出来，"
              "地铁末班车上他还在刷视频。巷口的灯把积水照成一片。" * 4)
    p = genre_profile(modern)
    assert genre_dominant(p) != "古风武侠", p


# --------------------------------------------------------------------------- #
# 2. 题材距离：同题材 = 0，明确不同 = 1
# --------------------------------------------------------------------------- #

def test_同题材的两侧距离为零():
    a = "巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上，手里拎着粉笔。"
    b = "他走过楼道，街口的灯亮着，铺子里没人，摊子收了，筒子楼在身后。"
    assert genre_distance(a, b) == 0.0, genre_distance(a, b)


def test_两侧都没露题材指纹时距离是零():
    """回归：原来这一档算成 1.0（"毫无共同点"），与事实相反。

    后果很实：都市臂的人类侧整本没有题材标记，于是每一题都被算成
    "最大不同"，一道题都出不来。
    """
    a = "他把手里的塑料袋换了只手，窗外的雨还在下。"
    b = "楼道里有人在打电话，声音从门缝里漏出来。"
    assert genre_distance(a, b) == 0.0, genre_distance(a, b)


def test_题材明确不同的两侧判为不一致():
    """这正是主人读一两题就分辨出来的那一类。"""
    city = ("巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上，"
            "他拎着粉笔在墙上划了一道。" * 3)
    old = ("他以内力震开剑锋，门派的长老在旁冷哼，"
          "江湖上谁都知道那柄刀的来历，镖局的镖师退到墙根。" * 3)
    bad, ev = genre_mismatch(city, old)
    assert bad, ev
    assert ev["dominant"]["A"] != ev["dominant"]["B"], ev


def test_年代一致但题材不同的两侧也会被判不一致():
    """同代不同题材 —— 年代闸门抓不到，题材闸门要抓得到。"""
    city = ("他掏出手机叫了外卖，监控和卷宗都调出来，地铁末班车上他还在刷视频。"
            "巷口的灯把积水照成一片。" * 3)
    ghost = ("厉鬼复苏，红纸遮住鬼眼，驭鬼者在收容所里值夜，"
             "规则写着不能回头看，手机在口袋里震了一下。" * 3)
    bad, ev = genre_mismatch(city, ghost)
    assert bad, ev


# --------------------------------------------------------------------------- #
# 3. 模型判题材（权威口径）
# --------------------------------------------------------------------------- #

def test_模型判题材_只认闭集里的类别():
    """闭集之外（模型自由发挥）一律不认 —— 不认 = 没判出来。"""
    class _Bad(_StubJudge):
        def chat(self, system, user):
            if "题材分类员" in system:
                return json.dumps([{"id": 1, "genre": "赛博朋克"},
                                   {"id": 2, "genre": GENRE_CLASSES[3]}],
                                  ensure_ascii=False)
            return super().chat(system, user)

    got = judge_genres([("a", "甲"), ("b", "乙")], _Bad(set()))
    assert "a" not in got, got          # 闭集外 ⇒ 当作没判出来
    assert got["b"] == GENRE_CLASSES[3], got


def test_模型判题材_结果按文本缓存(tmp_path):
    """同一段文本第二次判 ⇒ 不再调模型。"""
    stub = _StubJudge(set())
    cache = tmp_path / "genres.json"
    judge_genres([("a", "甲段")], stub, cache_path=cache)
    n = stub.calls
    judge_genres([("a", "甲段")], stub, cache_path=cache)
    assert stub.calls == n, stub.calls


def test_题材没判出来不许当一致():
    """fail closed：一侧没判出来 ⇒ 判为不一致（不能默认放过）。"""
    bad, ev = genre_mismatch("甲", "乙", labels=("", GENRE_CLASSES[3]))
    assert bad, ev
    assert "没判出来" in ev.get("reason", ""), ev


def test_两侧都判不出来不算题材不一致():
    """都没露题材指纹 ⇒ 没有题材线索可用，不算泄露。"""
    bad, ev = genre_mismatch("甲", "乙", labels=(GENRE_UNKNOWN, GENRE_UNKNOWN))
    assert not bad, ev


def test_一侧看不出来另一侧题材明确_不能算一致():
    """一侧满篇题材术语、另一侧看不出题材 ⇒ 评者照样能靠题材分辨出来。

    ⇒ 保守起见当不一致（换窗口/换段），不默认放过。
    """
    bad, ev = genre_mismatch("甲", "乙", labels=(GENRE_UNKNOWN, GENRE_CLASSES[5]))
    assert bad, ev
    assert "无法确认同题材" in ev.get("reason", ""), ev


# --------------------------------------------------------------------------- #
# 4. 多题材臂出包：每题两侧同题材，题与题之间题材乱着来
# --------------------------------------------------------------------------- #

def _arm(name: str, era: str, city: bool) -> dict:
    body = ("巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上，"
            "他拎着粉笔在墙上划了一道，楼道里的声控灯亮了一下。" * 12
            if city else
            "他以内力震开剑锋，门派的长老在旁冷哼，"
            "镖局的镖师退到墙根，剑锋上还带着血。" * 12)
    return {
        "name": name, "era": era,
        "rows": [{"seg": i + 1, "text": body} for i in range(6)],
        # 人类侧要够长，才能挑出**互不相同**的窗口（短文本只有一个窗口，
        # 会被「同一窗口只用一次」的去重规则吃掉）
        "corpus": [(name + "·人类侧", (body + "\n" + body[::-1]) * 6)],
    }


def test_多臂出包_每题两侧同题材且题序打散(tmp_path):
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_multi_arm_packet

    arms = [_arm("都市", "现代", True), _arm("古风", "古风", False)]
    info = build_multi_arm_packet(arms, tmp_path, provider=_StubJudge(set()),
                                  n_snips=4)
    assert info["emitted"] is True, info
    key = json.loads((tmp_path / "blind_key.json").read_text(encoding="utf-8"))
    assert key["blind"] is True, key["blind_reasons"]
    assert len(key["items"]) == 4
    # 两个臂都有题（题与题之间题材乱着来，不是整块堆在一起）
    arms_used = {it["arm"] for it in key["items"]}
    assert arms_used == {"都市", "古风"}, arms_used
    # 每一题的两侧题材一致
    assert all(it["genre_ok"] for it in key["items"]), key["items"]
    assert all(it["era_ok"] for it in key["items"]), key["items"]
    # 题面里不出现题材名（题材名是内部信息）
    sheet = (tmp_path / "blind_samples.md").read_text(encoding="utf-8")
    assert "都市" not in sheet and "古风" not in sheet


def test_多臂出包_题材对不上就不发题面(tmp_path):
    """某一臂的人类侧与我们的题材完全不符 ⇒ 这一题不合格 ⇒ 不发题面。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_multi_arm_packet

    bad_arm = {
        "name": "错配", "era": "现代",
        "rows": [{"seg": i + 1, "text": "巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上。" * 24}
                 for i in range(4)],
        "corpus": [("灵异书", "厉鬼复苏，红纸遮住鬼眼，驭鬼者在收容所里值夜。" * 24)],
    }
    judge = _StubJudge(set(), genre_rules={"厉鬼": GENRE_CLASSES[5]})
    info = build_multi_arm_packet([bad_arm], tmp_path, provider=judge,
                                  n_snips=2)
    assert info["emitted"] is False, info
    assert not (tmp_path / "blind_samples.md").exists(), "题面不该发出去"
    assert any("题材" in r for r in info["reasons"]), info["reasons"]


def test_多臂出包_题材判不出来就不发题面(tmp_path):
    """题材没判出来 ⇒ 不能确认两侧同题材 ⇒ fail closed，不发题面。"""
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_multi_arm_packet

    class _NoGenre(_StubJudge):
        def chat(self, system, user):
            if "题材分类员" in system:
                return ""            # 模拟题材判定批次失败
            return super().chat(system, user)

    info = build_multi_arm_packet([_arm("都市", "现代", True)], tmp_path,
                                  provider=_NoGenre(set()), n_snips=2)
    assert info["emitted"] is False, info
    assert not (tmp_path / "blind_samples.md").exists(), "题面不该发出去"
    assert any("题材" in r for r in info["reasons"]), info["reasons"]
