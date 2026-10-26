"""形态校正的**内容守恒判据**：口径必须对「合并对白」免疫。

## 为什么要有这个文件

形态校正的活儿之一就是把**同一个人连续说的多段台词并成一段**
（"一段一句台词"的流水账 → 一段一口气说完）。

逐条计数口径（`_content_lost`）在这种改动上**必然失真**：
字一个字都没少，但"条数"少了 ⇒ 被算成"内容丢失"。
2026-10-26 真跑抓到过：一次内容**一字未丢**的好改写被判成"丢 15%"
（字符多重集口径是 0.0%）⇒ 超过 MAX_DIALOGUE_LOSS ⇒ **被自己的守门判据拒掉** ——
形态校正最该发挥的时候被误杀。

所以主判据改成**字符多重集**口径；逐条口径降级为提示（info），不作拦截。
本文件把三条边界钉住：

1. **合并对白**（字没少）⇒ 不许被当成丢失；
2. **真丢内容** ⇒ 仍然拒绝；
3. **凭空扩写 / 同义改写** ⇒ 仍然拒绝。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.style.form import (  # noqa: E402
    MAX_DIALOGUE_LOSS,
    _chars_lost,
    _content_lost,
    _dialogue_chars,
    _dialogue_contents,
    correct_form,
)

# 原始：一段一句台词的流水账（7 段全裸）
BEFORE = "\n".join([
    "“天黑，路滑。”",
    "“若有人追来，省得连累你。”",
    "“我只问那辆车。”",
    "“今夜之后，我不会再问。”",
    "“那囚犯姓沈，三年前判的流放，昨夜死在城南。”",
    "“押送的人没进牢房，直接去了东桥。”",
    "“他们在桥下等了很久，等到子时，才把车赶上桥。”",
])

# 改写（真实形态校正干的两件事）：
#   ① 同一个人连续说的多段台词**并成一段**（7 段 → 4 段）；
#   ② 给其中几段**补引导语**（叙述 + 台词同段，裸台词率下降）。
# 引号里的字**一个没少**（字符多重集完全相同）。
MERGED = "\n".join([
    "守卒把碗放下，碗底磕在木桌上：“天黑，路滑。若有人追来，省得连累你。”",
    "“我只问那辆车。今夜之后，我不会再问。”",
    "陌生人从袖中取出一块帕子，擦去脸上的水：“那囚犯姓沈，三年前判的流放，昨夜死在城南。”",
    "“押送的人没进牢房，直接去了东桥。他们在桥下等了很久，等到子时，才把车赶上桥。”",
])

# 真丢：删掉两句
LOST = "\n".join([
    "“天黑，路滑。”",
    "“我只问那辆车。”",
    "“那囚犯姓沈，三年前判的流放，昨夜死在城南。”",
])

# 凭空扩写：加了一大段新对白
ADDED = BEFORE + "\n" + "\n".join([
    "“还有件事我得告诉你，那晚桥上来过第二拨人，穿的是衙门皂衣，问的却不是车上的货。”",
    "“他们问的是人，不是货。你若听见了什么风声，便说与我听。”",
    "“我什么也没听见，只看见桥下有人提着灯，来来回回走了三趟。”",
    "“那灯是绿的。桥务衙门用的灯，从来都是黄的。”",
])

PROFILE = {
    "version": 2,
    "buckets": {
        "medium": {
            "unit_chars": 3000,
            "feat": {
                "纯台词段占比": {"mean": 0.16, "std": 0.15, "p10": 0.0, "p90": 0.33},
            },
        },
    },
}


class _Provider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, system, user, **kw):
        self.calls += 1
        return self.reply


# ── 1. 两种口径的差异：合并 ⇒ 逐条说"丢"，字符说"没丢" ────────────
def test_merged_dialogue_is_not_content_loss_by_chars():
    """合并对白：逐条口径失真，字符口径守恒。"""
    before_items = _dialogue_contents(BEFORE)
    after_items = _dialogue_contents(MERGED)
    item_loss = _content_lost(before_items, after_items)
    char_loss = _chars_lost(_dialogue_chars(BEFORE), _dialogue_chars(MERGED))

    assert item_loss > 0, "前提：逐条口径确实会失真（否则这条测试就没意义）"
    assert char_loss == 0.0, f"字符口径不该判丢失，实际 {char_loss:.1%}"


def test_chars_lost_detects_real_loss():
    """真丢内容 ⇒ 字符口径要抓到。"""
    lost = _chars_lost(_dialogue_chars(BEFORE), _dialogue_chars(LOST))
    assert lost > 0.10, f"丢了这么多却没判出来：{lost:.1%}"


def test_chars_lost_detects_invented_dialogue():
    """凭空扩写 ⇒ 字符口径的「多」要抓到。"""
    added = _chars_lost(_dialogue_chars(ADDED), _dialogue_chars(BEFORE))
    assert added > 0.10, f"扩写了这么多却没判出来：{added:.1%}"


# ── 2. 端到端：判据 1 拦得住真问题，放得过合并 ────────────────────
def test_correct_form_accepts_merged_dialogue():
    """合并对白的改写**要被采纳**（旧口径会把它误杀成"丢 15%"）。"""
    out, info = correct_form(
        BEFORE, _Provider(MERGED), target_ratio=0.16, examples=["“有人退后。”"]
    )
    assert info["applied"] is True, info.get("reason")
    assert out == MERGED
    assert info["dialogue_lost"] == 0.0


def test_correct_form_still_rejects_dropped_dialogue():
    out, info = correct_form(
        BEFORE, _Provider(LOST), target_ratio=0.16, examples=["“有人退后。”"]
    )
    assert info["applied"] is False
    assert "没守恒" in info["reason"], info["reason"]
    assert out == BEFORE


def test_correct_form_still_rejects_invented_dialogue():
    out, info = correct_form(
        BEFORE, _Provider(ADDED), target_ratio=0.16, examples=["“有人退后。”"]
    )
    assert info["applied"] is False
    assert "没守恒" in info["reason"], info["reason"]
    assert out == BEFORE


def test_rejection_reports_item_level_loss_for_humans():
    """条数口径仍然报出来（给人看），但不再拿它当判据。"""
    _out, info = correct_form(
        BEFORE, _Provider(LOST), target_ratio=0.16, examples=["“有人退后。”"]
    )
    assert "dialogue_lost" in info
    assert "dialogue_lost_items" in info, "条数口径的诊断值要留着（给人看）"
    assert info["dialogue_lost_items"] > 0


def test_max_dialogue_loss_is_a_ratio():
    assert 0 < MAX_DIALOGUE_LOSS < 1
