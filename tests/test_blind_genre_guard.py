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
    FLAVOR_CLASSES,
    GENRE_CLASSES,
    GENRE_STRONG,
    GENRE_UNKNOWN,
    flavor_asymmetry,
    flavor_hits,
    flavor_mean,
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


def test_一侧看不出来另一侧是强题材_判为不一致():
    """一侧满篇强题材术语、另一侧看不出题材 ⇒ 评者能靠题材分辨出来。

    ⇒ 强题材（灵异/规则怪谈/年代/言情豪门）判为不一致（换窗口）。
    """
    bad, ev = genre_mismatch("甲", "乙", labels=(GENRE_UNKNOWN, GENRE_STRONG[0]))
    assert bad, ev
    assert "强题材" in ev.get("reason", ""), ev


def test_一侧看不出来另一侧是弱题材_只记弱标记():
    """弱题材（都市市井/都市异能）的词汇是日常生活词，
    一侧多一点少一点，评者很难据此判断哪边是出版物 ⇒ 不判为不一致，
    但要记进证据（供人复核）。"""
    weak = "现代都市（市井生活流）"
    assert weak not in GENRE_STRONG
    bad, ev = genre_mismatch("甲", "乙", labels=(GENRE_UNKNOWN, weak))
    assert not bad, ev
    assert ev.get("weak") is True, ev


def test_古风武侠算强题材():
    """回归：古风武侠原先被算成弱题材，是错的。

    「内力/真气/镖局/朝廷」是**类型标志**，不是日常词 ——
    一侧满篇武侠、另一侧是市井，评者一眼就分得出来。
    """
    wuxia = "古风武侠（江湖门派/内力招式）"
    assert wuxia in GENRE_STRONG
    bad, ev = genre_mismatch("甲", "乙", labels=(GENRE_UNKNOWN, wuxia))
    assert bad, ev
    assert "强题材" in ev.get("reason", ""), ev


def test_武侠与玄幻算兼容_评者分不出是哪种古风():
    """同一种世界观下的两个子类型 ⇒ 兼容。

    泄露的本质是**非对称**（一侧比另一侧更扎眼 ⇒ 评者能定位），
    两侧同样扎眼（哪怕题材不同）并不泄露。
    """
    wuxia = "古风武侠（江湖门派/内力招式）"
    xianxia = "东方玄幻（修真/仙侠/御剑）"
    bad, ev = genre_mismatch("甲", "乙", labels=(wuxia, xianxia))
    assert not bad, ev
    assert ev.get("weak") is True, ev
    # 方向反过来也一样
    bad2, ev2 = genre_mismatch("甲", "乙", labels=(xianxia, wuxia))
    assert not bad2, ev2


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
    # 回归（2026-10-31 真 bug）：挂回时用循环变量 `name` 当字典键，
    # 内层解包会覆盖它 ⇒ 各臂的人类侧文本被**整体对调**
    # （都市臂拿到古风臂的窗口）⇒ 每题的人类侧必须来自**本臂自己的书**
    assert all(it["human_source"].startswith(it["arm"]) for it in key["items"]), \
        [(it["arm"], it["human_source"]) for it in key["items"]]
    # 题面里不出现题材名（题材名是内部信息）
    sheet = (tmp_path / "blind_samples.md").read_text(encoding="utf-8")
    assert "都市" not in sheet and "古风" not in sheet


def test_多臂出包_人类侧候选窗口覆盖全书(tmp_path):
    """回归：候选窗口不能全挤在书的最前面。

    实测 2026-10-31：粗筛分数大面积相同（词表口径弱）⇒ 「全局取前 N 个」
    等于取书的最前面 N 个 ⇒ 候选窗口全来自前 10K 字，那几章的题材
    未必代表全书 ⇒ 分组取样（每段取粗筛最优的一个）。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_multi_arm_packet

    body = ("巷子很窄，后厨的冰柜嗡嗡响，掌柜蹲在灶眼边上，"
            "他拎着粉笔在墙上划了一道，楼道里的声控灯亮了一下。")
    long_text = (body + "\n") * 900          # 一本书 ≈ 9 万字
    arm = {
        "name": "都市", "era": "现代",
        "rows": [{"seg": i + 1, "text": body * 12} for i in range(6)],
        "corpus": [("都市·人类侧", long_text)],
    }
    info = build_multi_arm_packet([arm], tmp_path, provider=_StubJudge(set()),
                                  n_snips=2)
    assert info["emitted"] is True, info
    key = json.loads((tmp_path / "blind_key.json").read_text(encoding="utf-8"))
    # 候选池本身要**覆盖全书**（证据里留了偏移，供人复查）
    offs = key["evidence"]["pool_offsets"]["都市"]
    assert len(offs) >= 4, offs
    assert max(offs) > len(long_text) // 2, offs
    assert min(offs) < len(long_text) // 4, offs


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


# --------------------------------------------------------------------------- #
# 5. 第八道闸门：世界观级子题材指纹**对称**（模型闭集之外的子题材）
# --------------------------------------------------------------------------- #
#
# 为什么要有这道闸门（2026-10-31 实测）：
# 模型判题材的闭集只有 9 类，「军旅训练营」「古风宅斗言情」「耽美荤话」
# 「职场公文」「系统流」**都不在闭集里** ⇒ 模型只能判「看不出来」
# ⇒ 题材闸门按「两边都看不出来」放行 ⇒ 但评者一眼就看得出两段不是一个题材。
# 实测 13 题里 5 题栽在这上面（s01 宅斗 vs 修真、s10 押送囚犯 vs 修车铺、
# s11 荤话 vs 查案、s12 军旅训练 vs 职场悬疑、s04 超能力 vs 市井写实）。
#
# ⚠️ 词表**只用来剔除，不用来放行**：判不准时只会少几道题，不会放进坏题。


def test_子题材指纹_认出模型闭集之外的子题材():
    """阳性对照：尺必须会响。

    这几类都不在模型的 9 类闭集里（模型只能判「看不出来」），
    但评者一眼就能分出题材不同。
    """
    assert flavor_hits("兵王在据点分队，军车押送囚犯，看守所的灯亮着")["军旅军事"] >= 3
    assert flavor_hits("他掀了盖头，新郎成亲，侯爷在王府里点了妾")["古风宅斗言情"] >= 2
    assert flavor_hits("他射进来，唇瓣含住，一声喘息")["耽美成人"] >= 3
    assert flavor_hits("面板亮起，宿主升级，积分抽奖")["都市系统流"] >= 3
    assert flavor_hits("他办了离职，工牌留在前台，签字的部门是IT部")["职场公文"] >= 3


def test_子题材指纹_一侧有另一侧没有_判为不对称():
    """用户点破的那件事：一边有题材指纹、另一边没有 ⇒ 评者能靠它定位。"""
    bad = flavor_asymmetry("下午一点，所有人在据点集合，分队信息在大屏上",
                           "他在修车铺里拧螺丝，机油味混着巷口的油烟")
    assert bad, bad
    assert any(b["class"] == "军旅军事" for b in bad), bad


def test_子题材指纹_两侧不同子题材_也算不对称():
    """一边古风宅斗、一边修真宗门 ⇒ 两侧不是同一个题材（实测 s01 就栽在这）。"""
    bad = flavor_asymmetry("当年成亲，侯爷在王府里抬妾",
                           "外门弟子握着半枚玉牌，巡夜的师兄在池边清点")
    assert bad, bad
    assert {b["class"] for b in bad} == {"古风宅斗言情", "修真仙侠"}, bad


def test_子题材指纹_两侧同样有_判为对称():
    """两侧都露同一类指纹 ⇒ 评者两边都定位不了 ⇒ 不算泄露。"""
    assert not flavor_asymmetry("他在宗门领了灵石，闭关出来已是筑基",
                                "那本《宗门录》记着灵石出入，他闭关三年")
    assert not flavor_asymmetry("面板上只剩三次委托", "他把面板关掉，委托还没做完")


def test_子题材指纹_两侧都没有_判为对称():
    """两边都没有子题材指纹 ⇒ 没有题材线索可用 ⇒ 不算泄露。"""
    assert not flavor_asymmetry("他把碗筷收了，窗外的雨停了", "她把灯关掉，门轴响了一声")


def test_子题材指纹_只用于剔除_词表没有的类别不会硬凑():
    """词表没收录的类别不会被硬判成泄露（否则误伤一片好题）。"""
    assert not flavor_asymmetry("他们讨论了一下午的排班表", "他修完车，擦了擦手")
    # 指纹表是**有限**的：不在表里的类别不参与判定
    assert "科幻" not in FLAVOR_CLASSES


def test_子题材指纹均值_当锚点用():
    m = flavor_mean(["面板升级", "面板抽奖", "巷子里的雨"])
    assert m["都市系统流"] > 0, m
    assert "都市系统流" not in flavor_mean(["巷子里的雨"])


def test_多臂出包_子题材不对称就不发题面(tmp_path):
    """人类侧与我们的子题材不同 ⇒ 这一题不合格 ⇒ 不发题面。

    实测 2026-10-31：s12 一侧是军旅训练营、另一侧是职场悬疑 ——
    模型两边都判「看不出来」（闭集里没有「军旅」）⇒ 题材闸门放行，
    是这道闸门把它抓住的。
    """
    sys.path.insert(0, str(BACKEND))
    from verify_acceptance_real import build_multi_arm_packet

    arm = {
        "name": "都市", "era": "现代",
        "rows": [{"seg": i + 1, "text": "他在巷口的修车铺里拧螺丝，机油味混着油烟，"
                                          "手机屏幕亮了一下，他没看。" * 24}
                 for i in range(4)],
        "corpus": [("军旅书", "下午一点所有人在据点集合，分队信息投在大屏上，"
                              "兵王在队伍里喊了一声，班长带队出列。" * 24)],
    }
    info = build_multi_arm_packet([arm], tmp_path, provider=_StubJudge(set()),
                                  n_snips=2)
    assert info["emitted"] is False, info
    assert not (tmp_path / "blind_samples.md").exists(), "题面不该发出去"
    assert any("题材" in r for r in info["reasons"]), info["reasons"]


def test_多臂出包_候选池铺开密度档位(tmp_path):
    """候选池不能只给**一个密度档位**。

    闸门按「密度比 ≤1.6」挑窗口；池子若全落在同一档，
    就等于「只允许那一档的我们侧窗口出题」
    （实测 2026-10-31：都市臂只出题 4/10 —— 池子全在形状候选数 39~45，
    我们侧 20~30 的低密度窗口一个都配不上）。
    ⇒ 池子要按形状**排名分档**，每档取一个。
    """
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import shape_candidates
    from verify_acceptance_real import build_multi_arm_packet

    plain = "他蹲在墙根抽烟，风把烟灰吹散在水泥地上，远处有人在喊搬家。" * 40
    dense = "赵长安、钱北山、孙三更、李四海、周无咎、吴长风、郑小满、王九龄。" * 40
    long_text = (dense + "\n" + plain + "\n") * 8      # 一半高密度、一半低密度
    arm = {
        "name": "都市", "era": "现代",
        "rows": [{"seg": i + 1, "text": plain * 12} for i in range(6)],
        "corpus": [("都市·人类侧", long_text)],
    }
    info = build_multi_arm_packet([arm], tmp_path, provider=_StubJudge(set()),
                                  n_snips=2)
    key = json.loads((tmp_path / "blind_key.json").read_text(encoding="utf-8"))
    offs = key["evidence"]["pool_offsets"]["都市"]
    shapes = [len(shape_candidates(long_text[o:o + 400])) for o in offs]
    assert len(shapes) >= 4, shapes
    # 池子必须**同时**含高密度与低密度窗口（档位铺开）
    assert max(shapes) >= 2 * min(shapes), shapes


# --------------------------------------------------------------------------- #
# 5. 第八道闸门的第二次整类漏网（2026-10-31 逐题肉眼复核后补）
# --------------------------------------------------------------------------- #
#
# 起因：出了 17 题包后**逐题肉眼看**题面，发现三道题的**一侧是另一个子题材**：
#   · s04 人类侧 = 深山野外寻人（景区/深山/老林/失踪），我们侧 = 都市系统流
#   · s06 人类侧 = 军事谍战（信号基站/撤离/接应），我们侧 = 都市系统流
#   · s16 人类侧 = 古风情爱纠纷（风流谈资/洞底/颠倒黑白），我们侧 = 修真
#
# ⇒ 这三类在旧词表里**一个词都没收** ⇒ 模型闭集里也没有 ⇒ 两道闸门同时放行。
# ⇒ 教训：**闭集分类器 + 有限词表都只会在闭集内正确**，
#   闭集外的一整类题材，两道闸门会**同时失灵**。
#
# ⚠️ 这些用例里的文本是**从最终包的真实题面摘出来的**（不是编的），
# 所以它们锁住的是「真实坏题长什么样」。

def test_子题材指纹_识得出深山野外寻人():
    """s04 的真实坏题：人类侧是深山寻人，我们侧是都市系统流。"""
    wild = ("篝火在石堆边烧着，景区工作人员说这里是深山老林，"
            "进山采药的人失踪了，篝火旁边还留着帐篷。"
            "男人问：那姑娘失联几天了。老林里没有信号。")
    ours = ("江望把手机翻过来扣在柜台上，灰白面板还亮着。"
            "他在修车铺里拧螺丝，机油味混着巷口的油烟。")
    bad = flavor_asymmetry(wild, ours)
    assert bad, "深山寻人 vs 都市系统流 应当判为不对称"
    assert any(b["class"] == "都市野外寻人" for b in bad), bad


def test_子题材指纹_识得出军事谍战():
    """s06 的真实坏题：人类侧是军事谍战，我们侧是都市系统流。"""
    mil = ("他让对方在岛上建立临时信号基站，方便第一时间发送情报，"
           "但必须单独行动，才能把目标降到最小。"
           "任务顺利完成，我们也撤离了小岛。")
    ours = ("江望把手机翻过来扣在柜台上，灰白面板还亮着。"
            "老陶在修车铺里拧螺丝，机油味混着巷口的油烟。")
    bad = flavor_asymmetry(mil, ours)
    assert bad, "军事谍战 vs 都市系统流 应当判为不对称"
    assert any(b["class"] == "军旅谍战" for b in bad), bad


def test_子题材指纹_识得出古风情爱纠纷():
    """s16 的真实坏题：人类侧是宅斗情爱纠纷，我们侧是修真宗门。"""
    love = ("当年洞底救美一事充当过一段时间的风流谈资，"
            "那女子涨红了脸，说他强词夺理、颠倒黑白，死心塌地。")
    ours = ("外门弟子握着半枚玉牌，巡夜的师兄在池边清点，宗门规矩不能破。")
    bad = flavor_asymmetry(love, ours)
    assert bad, "情爱纠纷 vs 修真 应当判为不对称"
    assert any(b["class"] == "古风宅斗言情" for b in bad), bad


def test_子题材指纹_新补的三类不会误伤同题材的好题():
    """新增词表**只能剔不能放** ⇒ 同一题材的两侧不该被判不对称。"""
    # 两侧都是现代都市 ⇒ 都不该有军旅/野外/宅斗指纹
    assert not flavor_asymmetry(
        "他把手机揣回兜里，出了便利店，雨水顺着伞骨往下淌。",
        "修车铺的卷帘门拉到一半，机油味混着巷口早点摊的油条味。")
    # 两侧都是深山野外 ⇒ 两侧都该有「都市野外寻人」⇒ 对称
    both_wild = ("篝火在石堆边烧着，景区工作人员说这里是深山老林，"
                 "进山采药的人失踪了。")
    assert not flavor_asymmetry(both_wild, both_wild)


def test_子题材指纹_泛词不进新类():
    """「情报」「任务」这类泛词**不进**军事/系统流新类：
    收进来会把好题误判成不对称（实测会误伤 s10/s11/s14 三道好题）。"""
    for word in ("情报", "任务", "支援", "部队", "潜入"):
        assert word not in FLAVOR_CLASSES["军旅谍战"], word
    # 这类词只能待在「都市系统流」里（它本来就是系统流用语）
    assert "任务" in FLAVOR_CLASSES["都市系统流"]


# --------------------------------------------------------------------------- #
# 6. 第六道闸门的假阴性（2026-10-31 实测根因）
# --------------------------------------------------------------------------- #
#
# 实测：整包 17 题（14422 字）一次丢给模型问「还有哪些专名」，
# 模型返回**空数组** —— 读不完那么长的一段就放弃了 ⇒ 闸门是假阴性，
# 包被判「题面干净」而实际漏了《道途魔典》的「聚金阁」和我们侧的
# 「落雁坳」「慧照峰」。逐题问（每题约 800 字）立刻报出来。
#
# ⚠️ **长文本让模型沉默而不是报错** —— 沉默被当成了「没有」⇒ 最危险。

def test_题面复查_必须逐题问():
    """逐题复查：每题独立一次调用，返回每题各自的残留专名。"""
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns_per_item

    class _Stub:
        def __init__(self):
            self.calls = []

        def chat(self, system, user):
            self.calls.append(user)
            if "聚金阁" in user:
                return '["聚金阁"]'
            if "落雁坳" in user:
                return '["落雁坳"]'
            return "[]"

    p = _Stub()
    got = residual_proper_nouns_per_item(
        [("s01", "他推门进去，牌匾上写着聚金阁。"),
         ("s02", "落雁坳在册籍上记着三个人。"),
         ("s03", "他把碗筷收了。")],
        p)
    assert got["s01"] == ["聚金阁"], got
    assert got["s02"] == ["落雁坳"], got
    assert got["s03"] == [], got
    # ⚠️ 逐题 = 调用次数 = 题数（不是 1 次整包）
    assert len(p.calls) == 3, p.calls
    # ⚠️ 每次调用的输入都只是**一题**的正文（不能是整包）
    assert all(len(c) < 200 for c in p.calls), [len(c) for c in p.calls]


def test_题面复查_长文本不能让模型沉默():
    """**阳性对照**：尺必须会响。

    模拟「模型对长文本直接放弃」这个实测行为：输入超过阈值就返回空。
    ⇒ 逐题问（短输入）仍能抓到名字；整包问（长输入）抓不到。
    这正是 2026-10-31 把假阴性钉死的那个对照。
    """
    sys.path.insert(0, str(BACKEND))
    from acceptance_lib import residual_proper_nouns_per_item

    class _Stub:
        """长文本就放弃（返回空），短文本才认真看。"""

        def chat(self, system, user):
            return '["聚金阁"]' if len(user) < 200 else "[]"

    p = _Stub()
    one = "他推门进去，牌匾上写着聚金阁。"
    assert residual_proper_nouns_per_item([("s01", one)], p)["s01"] == ["聚金阁"]
    # 整包（长）问 ⇒ 同样的模型会沉默 ⇒ 这就是假阴性的来源
    assert p.chat("校对", one * 200) == "[]"
