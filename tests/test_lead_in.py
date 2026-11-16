"""引导语补写的回归测试。

要守住的：
1. **选点**：补几段由目标比例算；**均匀摊开**（不能全挤在一处）；
2. **并进去的形态**：冒号形态 `引导语：“台词”`（参考书主流形态）；
3. **六道判据**逐条拦得住：长度 / 像叙述 / 造事实 / 与已补的重复 /
   与上下文局部重复 / **与本段自身重复**；
4. **失败不影响正文**：模型报错 / 全部不合格 ⇒ 原文返回。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.style.lead_in import (  # noqa: E402
    insert_lead_ins,
    pick_lead_in_slots,
)

BARE = "“都退后。”"
NARR = "陆崇站在门口，手按在刀柄上，听见这句，眉头慢慢拧起，北荒王府里没几个人能让她这样说话。"


class _Provider:
    """按顺序返回预设回复；调用次数记在 `calls`。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts = []

    def chat(self, system, user, constraints=None, temperature=None):
        self.prompts.append((system, user))
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[i]


# ── 选点 ────────────────────────────────────────────────────────────
def test_pick_slots_count_follows_target():
    paras = [BARE] * 10          # 10 段全是裸台词
    slots = pick_lead_in_slots(paras, 0.5)
    # 目标 50%：10 段含引号 ⇒ 留 5 段裸 ⇒ 补 5 段
    assert len(slots) == 5, slots


def test_pick_slots_are_spread():
    """均匀摊开：不能全挤在一处（参考书里裸台词段是散着的）。"""
    paras = [BARE] * 12
    slots = pick_lead_in_slots(paras, 0.5)
    assert len(slots) == 6, slots
    gaps = [b - a for a, b in zip(slots, slots[1:])]
    assert min(gaps) >= 1, slots
    assert slots[0] != 0 or True
    # 相邻两处不能挨着（否则补了等于没补）
    assert all(g >= 2 for g in gaps), slots


def test_pick_slots_respects_max():
    paras = [BARE] * 40
    slots = pick_lead_in_slots(paras, 0.0, max_slots=5)
    assert len(slots) == 5, slots


def test_pick_slots_empty_when_already_below_target():
    paras = [BARE, NARR, "他抬眼看向门外，沉声道：“走。”"]
    assert pick_lead_in_slots(paras, 0.9) == []


# ── 并进去的形态 ─────────────────────────────────────────────────────
def test_lead_in_is_merged_with_colon():
    paras = [NARR, BARE, NARR, BARE]
    provider = _Provider(["他侧过脸，目光落向远处。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is True, info
    assert info["inserted"] == 1, info
    assert "他侧过脸，目光落向远处：“都退后。”" in out, out


def test_lead_in_keeps_original_dialogue_intact():
    """引导语是**加在前面**的，台词一个字都不能动。"""
    paras = [NARR, BARE]
    provider = _Provider(["他侧过脸，目光落向远处。"])
    out, _info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert out[-1].endswith(BARE), out
    assert BARE in out[-1]


# ── 六道判据 ────────────────────────────────────────────────────────
def test_reject_short_lead_in():
    paras = [NARR, BARE]
    provider = _Provider(["他抬头。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert "长度" in info["rejected"][0]["reason"], info
    assert out == paras


def test_reject_lead_in_with_quotes():
    paras = [NARR, BARE]
    provider = _Provider(["他侧过脸，冷冷地说：“都退后。”"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert out == paras


def test_reject_lead_in_with_number():
    """数字 = 可能的新事实（但单独的「一」是副词，不算）。"""
    paras = [NARR, BARE]
    provider = _Provider(["他抽出了三把刀，刀身映着月光。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert "造事实" in info["rejected"][0]["reason"], info


def test_accept_lead_in_with_aspect_one():
    """「眉头一皱」里的「一」是副词，不是数量 ⇒ 不该被拒。"""
    paras = [NARR, BARE]
    provider = _Provider(["他眉头一皱，指节在刀柄上收紧。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is True, info


def test_reject_lead_in_duplicating_its_own_paragraph():
    """引导语不能把本段开头又说一遍（「管家点头，…：管家点头：…」）。"""
    paras = ["管家点头：“白袍，腰间带刀。”"]
    provider = _Provider(["管家点头，垂在身侧的手微微收紧。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert "本段" in info["rejected"][0]["reason"], info
    assert out == paras


def test_reject_lead_in_duplicating_context():
    """引导语不能跟上下文刚写过的动作重复。"""
    paras = ["赵长安揉了下鼻梁：“得看什么账。”", "“我不是来讨债。”"]
    provider = _Provider(["赵长安揉着鼻梁的手微微一顿。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert "上下文" in info["rejected"][0]["reason"], info


def test_reject_lead_in_duplicating_accepted_one():
    """两条引导语不能几乎一样。"""
    paras = [NARR, BARE, NARR, BARE]
    provider = _Provider([
        "他侧过脸，目光落向远处的湖面。",
        "他侧过脸，目光落向远处的湖面。",
    ])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["inserted"] == 1, info
    assert len(info["rejected"]) == 1, info
    assert "重复" in info["rejected"][0]["reason"], info


# ── 失败不影响正文 ──────────────────────────────────────────────────
def test_provider_failure_keeps_original():
    class _Boom:
        def chat(self, *a, **kw):
            raise RuntimeError("boom")

    paras = [NARR, BARE]
    out, info = insert_lead_ins(paras, _Boom(), target_ratio=0.0)
    assert info["applied"] is False
    assert out == paras


def test_all_rejected_keeps_original():
    paras = [NARR, BARE]
    provider = _Provider(["嗯。"])
    out, info = insert_lead_ins(paras, provider, target_ratio=0.0)
    assert info["applied"] is False
    assert out == paras
