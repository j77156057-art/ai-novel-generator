"""两把「对白形态」的尺：旧尺 vs 新尺（A 类）。

## 为什么要有这个测试

「纯台词段占比」（档案里那个）把「说话人+提示语」（「陆崇问道：」）也算成了
叙述 ⇒ 它测的是「**这一段有多少叙述**」，
不是「这段是不是只有台词」。

实测（15 本真书 / 191905 个含引号段）：两把尺在 **13/15 本书上差 16~71 个
百分点，且方向不一致**（北荒刀歌 旧 22% / 新 9%；斩神 旧 8% / 新 80%）
⇒ **跨书比较只能用同一把尺**。

但它们**各有其用**，不能相互替换：
- **旧尺**：书级特征（进档案、进判别力验证、进提示基线）—— 判别力更好；
- **新尺**（A 类）：段级形态判断（「读者看到的是不是光秃秃一句台词」）。

## 本测试锁住的事实

1. 两把尺在**同一段**上会给出不同答案（提示语多的段落差得最明显）；
2. 新尺**剥掉**提示语（"陆崇问道：" 不算动作）；
3. 旧尺**算进去**（它测叙述多少）；
4. `plain_dialogue_ratio` 与 `beat.is_plain_dialogue` **同一口径**（唯一实现）。
"""
import os
import tempfile
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
SCRATCH = pathlib.Path((os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())) / "novel-scratch"
sys.path.insert(0, str(BACKEND))

from app.style.beat import is_plain_dialogue  # noqa: E402
from app.style.stylometry import (  # noqa: E402
    dialogue_segment_form,
    plain_dialogue_ratio,
    paragraphs,
)


# ── 1. 两把尺的分界不同 ────────────────────────────────────────────────
def test_two_measures_use_different_boundaries():
    """两把尺的**分界线不同** —— 不是谁对谁错。

    旧尺：引号外剩 ≤6 字 ⇒ 纯台词段。
    新尺：剥掉「说话人+提示语」后还剩不剩**动作** ⇒ A 类。

    「陆崇问道：」引号外 4 字 ⇒ **两把尺都**判它是纯台词段/A 类
    （旧尺：4 字 ≈ 没叙述；新尺：剥掉提示语只剩人名）。

    分歧出现在**中间地带**：「陆长风点了点头道：」引号外 7 字 ——
    旧尺（≤6? 否）⇒ 不算纯台词段；新尺（剥掉提示语剩「陆长风点了点头」= 6 字有动作）⇒ 不算 A 类。
    而「陆长风点头道：」引号外 5 字 ⇒ 旧尺算纯台词段；
    新尺剥掉提示语剩「陆长风点头」= 4 字 ⇒ 算 A 类（人名+动作，但没有环境叙述）。

    ⇒ 两尺在**同一段上可以给出不同答案**，这正是「跨书不可比」的来源。
    """
    # 分歧样本：引号外 7 字（超过旧尺的 6 字阈值）
    ps7 = ["陆长风点了点头道：“你说什么？”"]
    _a, old7 = dialogue_segment_form(ps7)
    new7 = plain_dialogue_ratio(ps7)
    assert old7 == pytest.approx(0.0), "7 字 > 旧尺阈值 ⇒ 不算纯台词段"
    assert new7 == pytest.approx(0.0), "剥掉提示语剩动作 ⇒ 不算 A 类"

    # 分歧样本：引号外 5 字，但含「点头」这个动作
    ps5 = ["陆长风点头道：“你说什么？”"]
    _a, old5 = dialogue_segment_form(ps5)
    new5 = plain_dialogue_ratio(ps5)
    assert old5 == pytest.approx(1.0), "5 字 ≤ 旧尺阈值 ⇒ 算纯台词段"
    # 新尺剥掉提示语后剩「陆长风点头」= 4 字 ⇒ 有动作 ⇒ 不算 A 类
    assert new5 == pytest.approx(0.0)

    # 真正两尺一致判成 A 类的：光台词
    ps0 = ["“不曾想还真是位公子哥。”"]
    _a, old0 = dialogue_segment_form(ps0)
    new0 = plain_dialogue_ratio(ps0)
    assert old0 == pytest.approx(1.0)
    assert new0 == pytest.approx(1.0)


# ── 2. 新尺剥掉提示语 ────────────────────────────────────────────────────
def test_new_measure_strips_speech_tag():
    ps = ["陆崇问道：“看得懂？”"]
    assert plain_dialogue_ratio(ps) == pytest.approx(1.0)


def test_new_measure_keeps_real_action():
    ps = ["陆崇也不恼怒，乐呵呵道：“好好好。”"]
    assert plain_dialogue_ratio(ps) == pytest.approx(0.0)


# ── 3. 旧尺按「≤6 字」切 ────────────────────────────────────────────────
def test_old_measure_boundary_is_six_chars():
    """旧尺：引号外 ≤6 字就算纯台词段（含说话人+提示语）。"""
    # 「陆崇问道」= 4 字 ⇒ 旧尺算纯台词段
    _a, r1 = dialogue_segment_form(["陆崇问道：“看得懂？”"])
    assert r1 == pytest.approx(1.0)
    # 「老周低头继续擦马镫，过了片刻才道」= 16 字 ⇒ 不算
    _a, r2 = dialogue_segment_form(["老周低头继续擦马镫，过了片刻才道：“那得看。”"])
    assert r2 == pytest.approx(0.0)


# ── 4. 唯一实现：与 beat 的判定同口径 ────────────────────────────────────
def test_plain_dialogue_ratio_matches_beat_judgement():
    """`plain_dialogue_ratio` 与 `beat.is_plain_dialogue` 必须一致。

    否则会出现「同一个概念两个数」——本项目吃过这个亏
    （`stylometry` 的旧尺与 `beat` 的新尺曾经各说各话）。
    """
    samples = [
        "陆崇问道：“看得懂？”",
        "陆崇也不恼怒，乐呵呵道：“好好好。”",
        "“不曾想还真是位公子哥。”",
        "他站在门口，没有说话。",
        "老周低下头继续擦马镫，过了片刻才道：“那得看是按军马算。”",
    ]
    for s in samples:
        if "“" not in s:
            continue
        from_beat = 1.0 if is_plain_dialogue(s) else 0.0
        from_stylometry = plain_dialogue_ratio([s])
        assert from_beat == from_stylometry, s


# ── 5. 真书上的基准（需要语料库）────────────────────────────────────────
@pytest.mark.skipif(
    not (SCRATCH / "corpus-check/novel_dev.db").exists(),
    reason="需要验证语料库",
)
def test_new_measure_range_on_real_books():
    """新尺在真书上的分布：**中位 ~72%**，最低 ~7%（北荒刀歌），最高 ~88%。

    这组数是本项目**最重要的一次纠错**的根据 ——
    之前拿北荒刀歌的 A 类 7% 当标准，得出「我们差了一倍」；
    实测我们 61% 已在真书区间内。

    ⇒ 北荒刀歌**在新尺下**也是 7%（它引号外的动作叙述很长，剥掉提示语后
    剩下的仍是一大段动作 ⇒ 极少 A 类）。
    """
    from sqlalchemy import create_engine, text as sa_text

    eng = create_engine(f"sqlite:///{(SCRATCH / 'corpus-check/novel_dev.db').as_posix()}")
    with eng.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT n.title, c.text FROM novel_chunks c "
            "JOIN raw_novels n ON n.id = c.novel_id ORDER BY n.title, c.idx")).fetchall()
    by: dict[str, list[str]] = {}
    for t, x in rows:
        by.setdefault(t, []).append(x or "")

    ratios = {}
    for t, chunks in by.items():
        ps: list[str] = []
        for c in chunks:
            ps.extend(paragraphs(c))
        r = plain_dialogue_ratio(ps)
        if r:
            ratios[t] = r

    assert len(ratios) >= 10
    vals = sorted(ratios.values())
    median = vals[len(vals) // 2]
    # 中位应在 60%~85%
    assert 0.60 <= median <= 0.85, f"中位 {median:.1%}"
    # 最低的一本（北荒刀歌）远低于 10% —— 它的动作叙述极密
    assert min(vals) < 0.15, f"最低 {min(vals):.1%}"
    # 最高的一本（光台词为主）超过 85%
    assert max(vals) > 0.85
