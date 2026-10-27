"""「按需触发」的回归测试：补到哪、补几处、什么时候根本不补（2026-10-27 加）。

## 为什么单开一个文件

`test_lead_in.py` 守的是**判据**（七道拦得住），这个文件守的是**成本**：
在同样的正文上，本次改动把「每段 11.7 次模型调用」压到 **5.6 次**，
而进带率不变。压法有三处（补到哪 / 留多少余量 / 什么时候不补），
外加「不够就再来一轮」这层自适应 —— 每一处都得有测试钉住，否则以后有人
"顺手"改回去（比如把 target 从 p90 改回 mean）成本会悄悄翻倍，
而且**没有任何一条会红**。

## 三处「按需」

1. **只在裸台词多于 p90 时触发** —— 低于 p10 时补引导语只会让比值更小、离带更远；
2. **补到 p90 就停**（不追均值）；
3. **再多给 `LEAD_IN_SAFETY_SLOTS` 处余量**（采纳率不是 100%）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.style.lead_in import (  # noqa: E402
    LEAD_IN_SAFETY_SLOTS,
    insert_lead_ins,
    pick_lead_in_slots,
)

BARE = "\u201c\u90fd\u9000\u540e\u3002\u201d"           # “都退后。”
NARR = ("\u9646\u5d07\u7ad9\u5728\u95e8\u53e3\uff0c\u624b\u6309\u5728\u5200\u67c4\u4e0a\uff0c"
        "\u542c\u89c1\u8fd9\u53e5\uff0c\u7709\u5934\u6162\u6162\u62e7\u8d77\u3002")  # 陆崇站在门口…


def _paras(bare, narr):
    """构造 bare 段裸台词 + narr 段叙述（不含引号，不参与分母）。"""
    out = []
    for _ in range(narr):
        out.append(NARR)
    for _ in range(bare):
        out.append(BARE)
    return out


# ── 1) 只补到上沿 ────────────────────────────────────────────────────
def test_fill_to_p90_costs_less_than_fill_to_mean():
    """同一段正文：补到上沿的槽位数**严格少于**补到均值。

    这是本次省下 52% 调用的直接来源 —— 若有人把目标改回均值，这条会红。
    """
    paras = _paras(bare=28, narr=4)       # 含引号段 28、裸 28 ⇒ 比值 100%
    q = 28
    mean, p90 = 0.162, 0.335
    slots_mean = pick_lead_in_slots(paras, mean)
    slots_p90 = pick_lead_in_slots(paras, p90)
    assert len(slots_p90) < len(slots_mean), (len(slots_p90), len(slots_mean))
    # 数量可直接对账：(b - floor(fill*q))
    assert len(slots_mean) == q - int(mean * q)
    assert len(slots_p90) == q - int(p90 * q)


def test_allowed_bare_uses_floor_not_round():
    """允许的裸台词段数必须**向下取整** —— 否则会卡在带外半段。

    真跑抓到的：q=68、上沿 0.3346 ⇒ `round(0.3346*68)=23` 段，
    允许的比值 23/68 = **0.3382 > 上沿**；而下一轮会认为
    「已经在目标附近」不再补 ⇒ 这段永远进不了带（实测差 0.003）。
    floor(0.3346*68) = 22 ⇒ 22/68 = 0.3235 ≤ 上沿 ✓
    """
    q = 68
    p90 = 0.3346
    paras = _paras(bare=30, narr=2)          # 含引号 30 段、裸 30 段
    slots = pick_lead_in_slots(paras, p90)
    allowed_after = 30 - len(slots)
    assert allowed_after <= p90 * q, (allowed_after, p90 * q)
    assert allowed_after / q <= p90, allowed_after / q


def test_filling_to_p90_never_lands_above_p90():
    """补完之后的比值必须**≤ 上沿**（100% 采纳的理想情况）。"""
    from app.style.form import measure_form

    q = 68
    p90 = 0.3346
    paras = _paras(bare=30, narr=2)
    slots = pick_lead_in_slots(paras, p90)
    assert slots, "应当要补"
    # 模拟「全部采纳」：把选中的段都变成混合段
    merged = list(paras)
    for i in slots:
        merged[i] = "\u4ed6\u4fa7\u8fc7\u8138\uff0c\u76ee\u5149\u843d\u5411\u8fdc\u5904\uff1a" + merged[i]
    ratio = measure_form("\n".join(merged))["bare_ratio"]
    assert ratio <= p90, (ratio, p90)


def test_safety_slots_added_only_when_something_is_needed():
    """余量只在「确实要补」时加 —— 已经在带内时一处都不补，别凭空多花钱。

    ⚠️ 注意这里的目标值怎么取：28 段全裸 ⇒ 比值 = **1.0**，
    所以「已经在带内」的目标是 1.0（而不是 0.99 —— 0.99 时它**确实在带外**，该补）。
    """
    paras = _paras(bare=28, narr=4)
    assert pick_lead_in_slots(paras, 1.0, safety_slots=2) == []
    base = pick_lead_in_slots(paras, 0.335)
    with_safety = pick_lead_in_slots(paras, 0.335, safety_slots=2)
    assert len(with_safety) == len(base) + 2, (len(base), len(with_safety))
    # 目标 0.99 时确实在带外（比值 1.0）⇒ 该补，且余量也加上
    near = pick_lead_in_slots(paras, 0.99)
    near_safety = pick_lead_in_slots(paras, 0.99, safety_slots=2)
    assert len(near) == 1, near          # floor(0.99*28)=27 ⇒ 差 1 段
    assert len(near_safety) == 3, near_safety


def test_safety_slots_cannot_exceed_bare_paragraphs():
    """余量不能把槽位数顶到超过裸台词段数（否则会重复挑同一段）。"""
    paras = _paras(bare=5, narr=2)
    slots = pick_lead_in_slots(paras, 0.0, safety_slots=99)
    assert len(slots) == 5
    assert len(set(slots)) == 5


def test_safety_slots_respects_max_slots():
    """余量也不能突破 MAX_LEAD_INS 那条上限（成本闸门优先）。"""
    paras = _paras(bare=40, narr=2)
    slots = pick_lead_in_slots(paras, 0.0, max_slots=6, safety_slots=10)
    assert len(slots) == 6


def test_safety_slot_default_is_zero_for_backward_compat():
    """不传 `safety_slots` 时行为与改动前**逐字相同**（老调用点不受影响）。"""
    paras = _paras(bare=20, narr=3)
    assert pick_lead_in_slots(paras, 0.5) == pick_lead_in_slots(paras, 0.5, safety_slots=0)


# ── 2) 诊断里能看见「按什么补的」 ──────────────────────────────────
class _Provider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, system, user, constraints=None, temperature=None):
        self.calls += 1
        return self.reply


def test_diagnostics_record_fill_policy():
    """诊断要写明「补到哪个比例、留了几处余量」—— 否则线上无从解释花了几次调用。"""
    paras = [NARR, BARE, NARR, BARE]
    out, info = insert_lead_ins(
        paras, _Provider("\u4ed6\u4fa7\u8fc7\u8138\uff0c\u76ee\u5149\u843d\u5411\u8fdc\u5904\u3002"),
        target_ratio=0.335, safety_slots=LEAD_IN_SAFETY_SLOTS,
    )
    assert info["fill_ratio"] == 0.335, info
    assert info["safety_slots"] == LEAD_IN_SAFETY_SLOTS, info
    assert info["slots"] >= 1, info


def test_safety_constant_is_documented_value():
    """余量就是个具体数字，改了要连带改文档 —— 这里钉住 2（实测出来的）。"""
    assert LEAD_IN_SAFETY_SLOTS == 2


# ── 3) 触发方向：只在「裸台词太多」时才该补 ────────────────────────
def test_only_upper_side_triggers():
    """低于下沿**不许**触发：补引导语只会把比值压得更低、离带更远（白花钱）。

    以前这道闸写成「不在带内」⇒ 两个方向都进：低于 p10 的段会白付一笔调用，
    再被末端闸门丢掉。这条钉方向。
    """
    from app.style.lead_in import should_fill_lead_ins

    assert should_fill_lead_ins(0.50, p10=0.0, p90=0.335) is True     # 高于上沿 ⇒ 补
    assert should_fill_lead_ins(0.335, p10=0.0, p90=0.335) is False   # 正好在上沿 ⇒ 不补
    assert should_fill_lead_ins(0.10, p10=0.05, p90=0.335) is False   # 带内 ⇒ 不补
    assert should_fill_lead_ins(0.01, p10=0.05, p90=0.335) is False   # 低于下沿 ⇒ 不补


def test_engine_uses_the_direction_guard():
    """引擎那道闸**必须**走 `should_fill_lead_ins`，不能再自己写一遍比较。

    自己写一份的后果：改方向时只改一处，另一边悄悄留着旧语义
    （「不在带内」，两个方向都进）—— 这类漂移以前在本项目出过（三条生成路径
    两把尺）。这里用源码级断言把它钉住。
    """
    import inspect

    from app.generation import engine as eng

    src = inspect.getsource(eng._maybe_correct_form)
    assert "should_fill_lead_ins" in src, "引擎没走触发方向的那道闸"
    assert "ratio_now > target[" not in src, "引擎里还留着一份自己写的比较"


# ── 4) 自适应轮数（2026-10-28 真跑逼出来的） ────────────────────────
def test_fill_rounds_twice_when_still_out_of_band():
    """一轮没进带 ⇒ **再来一轮**（这是真跑发现的：余量 2 处有时不够）。

    固定余量救不了「只差 1 个槽位」的段 —— 采纳率是**模型**决定的常量，
    换个模型就从 94% 掉到 67%。所以必须能补第二轮。
    """
    from app.style.lead_in import fill_lead_ins_until_in_band

    class _Flaky:
        """前 3 处写得出、后面写不出 —— 模拟采纳率不足。"""

        def __init__(self):
            self.calls = 0

        def chat(self, system, user, constraints=None, temperature=None):
            self.calls += 1
            if self.calls > 3:
                return "\u4ed6\u62ac\u5934\u3002"     # 太短 ⇒ 判据 1 拒
            return "\u4ed6\u4fa7\u8fc7\u8138\uff0c\u76ee\u5149\u843d\u5411\u8fdc\u5904\u3002"

    paras = [NARR, BARE, NARR, BARE, NARR, BARE, NARR, BARE]
    provider = _Flaky()
    out, info = fill_lead_ins_until_in_band(
        paras, provider, p10=0.0, p90=0.335, safety_slots=2, max_rounds=3,
    )
    assert info["rounds"] >= 2, info
    assert info["rounds_detail"][0]["inserted"] >= 1, info


def test_fill_stops_immediately_when_already_in_band():
    """已经在带内 ⇒ **一次都不调**（按需的第一条：不花钱）。"""
    from app.style.lead_in import fill_lead_ins_until_in_band

    class _Never:
        def __init__(self):
            self.calls = 0

        def chat(self, *a, **k):
            self.calls += 1
            return "\u4ed6\u4fa7\u8fc7\u8138\uff0c\u76ee\u5149\u843d\u5411\u8fdc\u5904\u3002"

    paras = [NARR, BARE]           # 含引号 1 段、裸 1 段 ⇒ 比值 1.0
    provider = _Never()
    out, info = fill_lead_ins_until_in_band(
        paras, provider, p10=0.0, p90=1.0, max_rounds=3,
    )
    assert provider.calls == 0, info
    assert info["rounds"] == 0, info
    assert info["in_band"] is True, info


def test_fill_stops_when_a_round_inserts_nothing():
    """一轮**一处都没成** ⇒ 停（再来也是白花钱）。"""
    from app.style.lead_in import fill_lead_ins_until_in_band

    class _Bad:
        def __init__(self):
            self.calls = 0

        def chat(self, *a, **k):
            self.calls += 1
            return "\u4ed6\u62ac\u5934\u3002"          # 太短 ⇒ 全被拒

    paras = [NARR, BARE, NARR, BARE]
    provider = _Bad()
    _out, info = fill_lead_ins_until_in_band(
        paras, provider, p10=0.0, p90=0.335, safety_slots=2, max_rounds=3,
    )
    assert info["rounds"] == 1, info
    assert info["inserted"] == 0, info
    assert provider.calls <= 4, info     # 一轮就收手


def test_fill_respects_max_rounds():
    """轮数上限必须真的生效（不能变成无限循环）。"""
    from app.style.lead_in import LEAD_IN_MAX_ROUNDS, fill_lead_ins_until_in_band

    class _Good:
        def __init__(self):
            self.calls = 0

        def chat(self, *a, **k):
            self.calls += 1
            return "\u4ed6\u4fa7\u8fc7\u8138\uff0c\u76ee\u5149\u843d\u5411\u8fdc\u5904\u3002"

    paras = [NARR, BARE] * 10
    provider = _Good()
    _out, info = fill_lead_ins_until_in_band(
        paras, provider, p10=0.0, p90=0.0, safety_slots=1, max_rounds=LEAD_IN_MAX_ROUNDS,
    )
    assert info["rounds"] <= LEAD_IN_MAX_ROUNDS, info


def test_fill_reports_not_in_band_honestly():
    """补了仍没进带 ⇒ **如实说**（不许假装成功）。"""
    from app.style.lead_in import fill_lead_ins_until_in_band

    class _Bad:
        def chat(self, *a, **k):
            return "\u4ed6\u62ac\u5934\u3002"

    paras = [NARR, BARE, NARR, BARE]
    _out, info = fill_lead_ins_until_in_band(
        paras, _Bad(), p10=0.0, p90=0.335, safety_slots=2, max_rounds=2,
    )
    assert info["in_band"] is False, info
    assert "未进带" in (info.get("reason") or ""), info
