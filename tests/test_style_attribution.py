"""对话归属位置（第九道闸门 + 引擎侧的归属口径）。

===============================================================================
为什么这个文件的第一批用例是「阳性对照」
===============================================================================

第一版尺的动词集合漏了单独的「说」⇒ 我们侧写的「才说：“…”」全部漏检
⇒ 得出「我们侧**从不**写前置归属（0 次）」的**错误结论**
⇒ 一整套基于它的判断（逐题剔除怎么剔、根因怎么写）全部建在坏尺上。

⇒ **尺必须先用阳性对照自证**。这批用例就是那把尺的「阳性对照」：
每一条都是**真实文本里出现过的句子**，不是编的。

后面几组用例覆盖：惯例判定、口径生成、提示词接线、整包判别力检验。
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from acceptance_lib import attribution_discriminability  # noqa: E402
from app.style.attribution import (  # noqa: E402
    attribution_convention,
    attribution_directive,
    attribution_profile,
)

#: 真实文本里出现过的**阳性对照**（必须命中前置归属）。
#:
#: ⚠️ 第一条与第五条就是**坏过的那条尺漏掉的东西**（「说」字派）。
POSITIVE_FRONT = [
    "他低声质问道：“这里是什么地方？！”",
    "一名少年问道：“公子，何事？”",
    "李牧终于 Associates说道：“一只猫叼走了记忆晶体？”".replace(" Associates", ""),
    "颜柔说道：“先生，那接下来该怎么办？我们失去了联系。”",
    "裴远没接，盯着那饼看了两息，才说：“内务房不管饭。”",
    "秦昭想了想，说：“这牌子被人刮过。”",
]

#: **阴性对照**：不该命中前置归属。
NEGATIVE_FRONT = [
    "“木头。”裴远低声说。",
    "“你手里拿的什么。”",
    "“先吃。”",
    "秦昭压低声音。",
    "他点了点头，转身走进雨里，脚步声很快没了。",
]


def test_尺认得出前置归属():
    """阳性对照：每一句都要命中。"""
    for s in POSITIVE_FRONT:
        p = attribution_profile(s)
        assert p["front"] >= 1, f"漏检了前置归属：{s!r}"


def test_尺不误伤裸台词和后置归属():
    """阴性对照：裸台词、纯叙述都不该命中前置。"""
    for s in NEGATIVE_FRONT:
        p = attribution_profile(s)
        assert p["front"] == 0, f"误伤了：{s!r}"


def test_尺认得出后置归属():
    """后置：「引语 + 说话人 + 动词」是另一条惯例，也要认。"""
    p = attribution_profile("“木头。”裴远低声说。")
    assert p["back"] == 1, p
    assert p["front"] == 0, p


def test_后置尺不吃叙述里的说():
    """回归：第二版尺的假阳性。

    「…不用说，肯定是他舅舅」「…实在不好意思说，中毒太深」——
    这里的「说」是叙述的一部分，不是归属标记。
    第一版尺把它们算成后置（参考书全书 31 次里大部分是这类）。
    """
    for s in ("“我？抱歉，你说的是我吗？”实在不好意思说，中毒太深。",
              "“你这取名字的方式跟谁学的？！”不用说，肯定是他舅舅。"):
        p = attribution_profile(s)
        assert p["back"] == 0, f"把叙述里的「说」当成了归属：{s!r} → {p}"


def test_插话式单独算不混进后置():
    """插话式（`“…，”他道，“…”`）是第三种写法，必须单列。

    混进「后置」会把尺的口径搅浑：参考书几乎没有插话式，我们侧很常见 ——
    这是另一条非对称线索，不能和真后置混为一谈。
    """
    p = attribution_profile("“不认识，”他道，“古字，我一个学剑的哪里认得。”")
    assert p["mid"] == 1, p
    assert p["back"] == 0, p


def test_尺分得清道字派和说字派():
    """「道」与「说」是两个派别 —— 盲评里正是靠这个定位的。"""
    dao = attribution_profile("秦昭道：“放肆。”")
    shuo = attribution_profile("秦昭说：“别动。”")
    assert dao["front_dao"] == 1 and dao["front_shuo"] == 0, dao
    assert shuo["front_shuo"] == 1 and shuo["front_dao"] == 0, shuo


def test_坏过的尺不能复发():
    """回归：第一版尺漏了单独的「说」⇒ 这里钉死。"""
    p = attribution_profile("秦昭想了想，说：“这牌子被人刮过。”")
    assert p["front"] == 1, "「说」字派又漏了 —— 尺退回第一版了"
    assert p["front_shuo"] == 1, p


def test_门槛卡边的那本书也要触发():
    """回归：门槛把《回声年代》挡在外面 ⇒ 都市臂没拿到口径。

    实测：那本书**样例**里的前置密度只有 0.080/百字（命中 3 次），
    第一版门槛定 0.10 ⇒ 判定「没有明显惯例」⇒ **不给口径**
    ⇒ 都市臂产出仍是「后置 7 次 / 前置 0 次」。

    ⇒ 门槛必须同时看**命中数**（≥3，绝对证据）与**密度**（≥0.05，比率）。
    """
    # 3 次「道」字派前置归属，散在约 3700 字里 ⇒ 密度 ≈ 0.081/百字
    filler = "他站在廊下，看着天色一点点暗下去，远处传来更鼓的声响。" * 60
    samples = [filler + "秦昭道：“放肆。”", filler + "他道：“你且慢。”",
               filler + "一名少年问道：“公子，何事？”"]
    d = attribution_directive(samples)
    assert d, "卡边的那本书被门槛挡掉了 ⇒ 口径没下达"
    assert "道" in d, d


def test_一次两次命中不算惯例():
    """光有 1~2 次命中 ⇒ 不给口径（宁可少说，也不要教错）。"""
    filler = "他站在廊下，看着天色一点点暗下去。" * 200
    samples = [filler + "秦昭道：“放肆。”"]
    assert attribution_directive(samples) == ""


def test_能判出这本书用道字派前置():
    text = "\n".join([
        "秦昭道：“放肆。”",
        "裴远道：“兄长。”",
        "一名少年问道：“公子，何事？”",
    ])
    conv = attribution_convention(text)
    assert conv["convention"] == "front_dao", conv


def test_没有明显惯例就不乱判():
    """全是裸台词的文本没有归属惯例 ⇒ none。"""
    text = "\n".join([
        "“别动。”",
        "“放手。”",
        "“我不走。”",
    ])
    assert attribution_convention(text)["convention"] == "none"


def test_口径从样例里量出来且带正确实例():
    """口径必须**自带一个正确实例** —— 形态要靠实例教，不能只给数字。"""
    samples = [
        "秦昭道：“放肆。”\n众人皆惊，一时无人敢应。\n他负手而立，衣袍无风自动。",
        "裴远道：“兄长。”\n那声音清冷，听不出喜怒。\n云梦江氏的人互看一眼，谁也没有先动。",
        "一名少年问道：“公子，何事？”\n少年立在阶下，不敢抬头。\n风从廊下穿过，卷起几片落叶。",
    ]
    d = attribution_directive(samples)
    assert d, "该给口径却没给"
    assert "道" in d, d
    assert "例如：" in d, d
    assert "“" in d, "口径里必须有原文实例"


def test_没有明显惯例时不给口径():
    """宁可少说，也不要给一条没有正确实例的空口径。"""
    samples = ["“别动。”\n“放手。”\n“我不走。”"]
    assert attribution_directive(samples) == ""


def test_空样例不给口径():
    assert attribution_directive([]) == ""
    assert attribution_directive(["", "   "]) == ""


def test_提示词里带上了归属口径():
    """接线：样例有明确惯例 ⇒ 提示词的句法样板块里要有那句口径。"""
    from app.generation.prompts import build_user_prompt

    exs = [{"novel_id": 1, "novel_title": "样例书", "text": (
        "秦昭道：“放肆。”\n众人皆惊，一时无人敢应。\n他负手而立，衣袍无风自动。\n"
        "裴远道：“兄长。”\n那声音清冷，听不出喜怒。\n云梦江氏的人互看一眼，谁也没有先动。\n"
        "一名少年问道：“公子，何事？”\n少年立在阶下，不敢抬头。\n风从廊下穿过，卷起几片落叶。\n"
    )}]
    prompt = build_user_prompt([], [], None, 1200, style_exemplars=exs)
    assert "【句法样板（照着写）】" in prompt
    assert "例如：" in prompt, "归属口径没进提示词"


def test_没有样例时提示词不变():
    """没有样例 ⇒ 不注入口径（老规矩：样例是增强，不是前提）。"""
    from app.generation.prompts import build_user_prompt

    prompt = build_user_prompt([], [], None, 1200, style_exemplars=None)
    assert "【句法样板（照着写）】" not in prompt


def test_整包判别力检验抓得出不盲的包():
    """第九道闸门：只用写法就能定位 ⇒ 这个包在写法上不盲。"""
    ours = ["他开口：“别动。”\n他皱眉：“我不走。”\n他说：“放手。”"] * 6
    human = ["秦昭道：“放肆。”\n裴远道：“兄长。”\n一名少年问道：“公子，何事？”"] * 6
    res = attribution_discriminability(ours, human)
    assert res["leaky"] is True, res
    assert res["rule"], res


def test_整包判别力检验对对称的包不报警():
    """两侧写法一样 ⇒ 不该拦。"""
    same = "秦昭道：“放肆。”\n裴远道：“兄长。”\n少年问道：“何事？”"
    res = attribution_discriminability([same] * 8, [same] * 8)
    assert res["leaky"] is False, res


def test_判别力检验样本不够时不下结论():
    """可判对数太少 ⇒ 不下结论（宁可放过，不可乱拦）。"""
    ours = ["他开口：“别动。”"]
    human = ["秦昭道：“放肆。”"]
    res = attribution_discriminability(ours, human)
    assert res["leaky"] is False, res


# --------------------------------------------------------------------------- #
# 归属校正（形态校正层的第二维）
# --------------------------------------------------------------------------- #

def test_归属率的分母是对白句数不是百字():
    """分母错了 ⇒ 诊断和目标都会错。"""
    from app.style.attribution_fix import attribution_rate

    text = "秦昭道：“你怎么在这儿？”\n“我在这儿。”裴远道。\n他没有回答。"
    r = attribution_rate(text)
    assert r["quotes"] == 2, r
    assert r["hits"] >= 2, r
    assert 0.0 < r["rate"] <= 1.0, r


def test_能从参考书段落里量出归属目标():
    from app.style.attribution_fix import attribution_target

    one = "秦昭道：“放肆。”\n众人皆惊，一时无人敢应。\n他负手而立，衣袍无风自动。"
    two = "裴远道：“兄长。”\n那声音清冷，听不出喜怒。\n云梦江氏的人互看一眼。"
    three = "一名少年问道：“公子，何事？”\n少年立在阶下，不敢抬头。\n风卷起几片落叶。"
    t = attribution_target([one, two, three] * 8)
    assert t, "量不出目标"
    assert t["convention"] == "front_dao", t
    assert t["examples"], t
    assert t["rate"] > 0, t


def test_参考书没有归属惯例时量不出目标():
    from app.style.attribution_fix import attribution_target

    paras = ["“别动。”\n“放手。”\n“我不走。”"] * 40
    assert attribution_target(paras) is None


class _FakeProvider:
    """假 provider：按脚本返回一段文本。"""

    def __init__(self, out):
        self.out = out
        self.calls = 0

    def chat(self, system, user, temperature=None):
        self.calls += 1
        return self.out


#: 一段「对白密集」的正文（够长，能过 200 字的门槛）
_NARR = ("他站在廊下，没有再往前一步。风从堂前穿过去，把案上的纸吹起一角，"
         "纸角翻了两下，又落回原处。屋里没有人说话。")
_BODY = ("“别动。”\n“放手。”\n“你到底想怎样。”\n" + _NARR + "\n"
         "“让开。”\n“我不让。”\n" + _NARR + "\n"
         "“你听见没有？”\n“听见了。”\n" + _NARR + "\n"
         "他等了片刻，才又开口。\n")

#: 同一段台词，**只**多加了说话人归属（对白内容逐字相同）
_FIXED = ("秦昭道：“别动。”\n裴远道：“放手。”\n秦昭道：“你到底想怎样。”\n"
          + _NARR + "\n"
          "裴远道：“让开。”\n秦昭道：“我不让。”\n" + _NARR + "\n"
          "秦昭道：“你听见没有？”\n裴远道：“听见了。”\n" + _NARR + "\n"
          "他等了片刻，才又开口。\n")


def test_归属校正确实能把归属率往上拉():
    from app.style.attribution_fix import correct_attribution

    prov = _FakeProvider(_FIXED)
    text, info = correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("applied"), info
    assert info["after"]["rate"] > info["before"]["rate"], info


def test_归属校正拒绝改动对白内容():
    """对白内容被改 ⇒ 拒绝（保留原文）。这一步是**内容守恒**的兜底。"""
    from app.style.attribution_fix import correct_attribution

    tampered = _FIXED.replace("别动。", "你给我站住别动。")
    prov = _FakeProvider(tampered)
    text, info = correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert not info.get("applied"), info
    assert "守恒" in info.get("reason", ""), info
    assert text == _BODY


def test_归属校正拒绝不靠近目标的改写():
    from app.style.attribution_fix import correct_attribution

    prov = _FakeProvider(_BODY)  # 原样返回 ⇒ 归属率没动
    text, info = correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert not info.get("applied"), info
    assert "没靠近" in info.get("reason", ""), info


def test_归属校正过冲会被配平到目标():
    """矫枉过正 ⇒ 不是原样保留，而是**配平到目标**。

    2026-10-31 改：过冲不再是「拒绝」，而是先机械配平再判。
    （拒绝会让整段留在「0 归属 / 90% 归属」这种极端状态 —— 那正是泄露面。）
    """
    from app.style.attribution_fix import correct_attribution

    # 7 句对白里给 5 句加归属 ⇒ 归属率 71%；目标 40%
    partial = ("秦昭道：“别动。”\n裴远道：“放手。”\n秦昭道：“你到底想怎样。”\n"
               + _NARR + "\n"
               "裴远道：“让开。”\n“我不让。”\n" + _NARR + "\n"
               "秦昭道：“你听见没有？”\n“听见了。”\n" + _NARR + "\n"
               "他等了片刻，才又开口。\n")
    prov = _FakeProvider(partial)
    text, info = correct_attribution(
        _BODY, prov, target_rate=0.4, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("applied"), info
    assert abs(info["after"]["rate"] - 0.4) <= 0.15, info


def test_切片不会把对白切断():
    """切片按**段落**切 —— 切断一个对白会让模型看到半句话。"""
    from app.style.attribution_fix import _split_chunks

    text = "\n".join(["他说了一句很长的话。" * 8, "“这是一句对白。”"] * 40)
    chunks = _split_chunks(text)
    assert len(chunks) > 1, "这么长的文本应该被切开"
    joined = "\n".join(chunks)
    assert joined.count("“这是一句对白。”") == 40, "切片把对白切断了"
    for c in chunks:
        assert c.count("“") == c.count("”"), f"切片里有半个引号：{c[:40]}"


class _ChunkProvider:
    """按段返回：给「要改写的那段」改写；命中标记的那段返回空（模拟失败）。"""

    def __init__(self, bad_marker=None):
        self.bad_marker = bad_marker
        self.calls = 0

    def chat(self, system, user, temperature=None):
        self.calls += 1
        body = user.split("【原文】")[-1].strip()
        if self.bad_marker and self.bad_marker in body:
            return ""
        return body.replace("“", "秦昭道：“")


def test_某一段拿不到就保留那段原文():
    """改写是增强，不是前提 —— 一段失败不该把整段判死。"""
    from app.style.attribution_fix import correct_attribution

    para = "他站在廊下，看着天色暗下去。"
    text = "\n".join(["“别动。”", para] * 100)
    # 只让**含标记的那一段**失败
    text = text.replace("他站在廊下，看着天色暗下去。",
                        "他站在廊下，看着天色暗下去。（这段会失败）", 1)
    prov = _ChunkProvider(bad_marker="会失败")
    out, info = correct_attribution(
        text, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("chunks", 0) > 1, info
    assert info.get("chunks_failed", 0) == 1, info
    assert info.get("applied") is True, info
    # 失败的那一段保持原样
    assert "（这段会失败）" in out, out[:200]


# --------------------------------------------------------------------------- #
# 两遍改写（2026-10-31 实测后加的）
# --------------------------------------------------------------------------- #

#: 「只给部分对白加归属」的正文：7 句对白里 2 句带归属 ⇒ 29%。
_PARTIAL = ("秦昭道：“别动。”\n“放手。”\n“你到底想怎样。”\n" + _NARR + "\n"
            "“让开。”\n“我不让。”\n" + _NARR + "\n"
            "“你听见没有？”\n秦昭道：“听见了。”\n" + _NARR + "\n"
            "他等了片刻，才又开口。\n")


class _TwoPassProvider:
    """按提示词内容分两路返回：加归属 / 去冗余。

    记录每次拿到的提示词 —— 用来断言「第二遍作用在**第一遍的结果**上」。
    """

    def __init__(self, added, trimmed):
        self.added = added
        self.trimmed = trimmed
        self.prompts: list[str] = []

    def chat(self, system, user, temperature=None):
        self.prompts.append(user)
        if "去掉多余" in user:
            return self.trimmed
        return self.added


def test_过冲会被机械配平到目标():
    """模型改写过冲 ⇒ 数量由本地**机械配平**（模型数不准，见 `_trim_attribution_to_count`）。"""
    from app.style.attribution_fix import correct_attribution

    prov = _TwoPassProvider(_FIXED, _FIXED)   # 第一遍 100%
    out, info = correct_attribution(
        _BODY, prov, target_rate=0.3, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("applied"), info
    assert abs(info["after"]["rate"] - 0.3) <= 0.15, info
    assert info.get("trimmed"), f"应该发生过机械配平：{info}"


def test_机械配平后引语逐字不变():
    """配平只去「人名+道：」前缀 ⇒ 台词内容守恒。"""
    from app.style.attribution_fix import _trim_attribution_to_count

    out, dropped = _trim_attribution_to_count(_FIXED, 2)
    assert dropped == 5, dropped
    assert out.count("“") == _FIXED.count("“")
    for line in _FIXED.split("\n"):
        if "“" in line:
            core = line.split("“", 1)[1]
            assert core in out, f"引语内容被改了：{line}"


def test_模型没补够时会再调几遍():
    """模型一次没补够 ⇒ 迭代调整（每遍按「现在 N 处 / 目标 M 处」下指令）。"""
    from app.style.attribution_fix import correct_attribution

    prov = _TwoPassProvider(_PARTIAL, _PARTIAL)   # 第一遍只补到 29%
    out, info = correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert len(prov.prompts) >= 2, f"应该调不止一遍：{len(prov.prompts)}"
    assert info.get("applied"), info


def test_第二遍作用在第一遍的结果上():
    """回归：调整遍的输入必须是**第一遍的输出**，不能是原文。"""
    from app.style.attribution_fix import correct_attribution

    prov = _TwoPassProvider(_PARTIAL, _PARTIAL)   # 29% ⇒ 离目标 80% 还远 ⇒ 会调
    correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert len(prov.prompts) >= 2, prov.prompts
    second = prov.prompts[1]
    body = second.split("【原文】")[-1]
    assert "秦昭道" in body, "调整遍拿到的应是第一遍改写后的文本"


def test_校正走关思考的provider变体():
    """校正必须用**关思考 + 独立预算**的变体（空输出的根因）。"""
    from app.style.attribution_fix import correct_attribution

    class _VariantProvider:
        def __init__(self):
            self.variant_kwargs = None

        def variant(self, **kwargs):
            self.variant_kwargs = kwargs
            return self

        def chat(self, system, user, temperature=None, max_tokens=None):
            return _FIXED

    prov = _VariantProvider()
    correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert prov.variant_kwargs is not None, "没走 variant()"
    assert prov.variant_kwargs.get("thinking_off") is True, prov.variant_kwargs
    assert prov.variant_kwargs.get("max_tokens"), prov.variant_kwargs


def test_provider没有variant也能跑():
    """老 provider / 本地兜底没有 variant() ⇒ 不能炸。"""
    from app.style.attribution_fix import correct_attribution

    prov = _FakeProvider(_FIXED)
    out, info = correct_attribution(
        _BODY, prov, target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("applied"), info


def test_两遍都空输出就保留原文():
    """空输出 ⇒ 原样返回，绝不把正文改坏。"""
    from app.style.attribution_fix import correct_attribution

    class _EmptyProvider:
        def chat(self, system, user, temperature=None, max_tokens=None):
            return ""

    out, info = correct_attribution(
        _BODY, _EmptyProvider(), target_rate=0.8, convention="front_dao",
        examples=["秦昭道：“放肆。”"],
    )
    assert info.get("applied") is False, info
    assert info.get("reason") == "空输出", info
    assert out == _BODY
