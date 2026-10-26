"""beat 插入的选点与判据（`app/style/beat.py`）

## 验的是什么

1. **A/C 判定**：引号前只有「说话人+提示语」（「徐骁问道：」）不算有动作；
   有真动作（「徐骁也不恼怒，乐呵呵道：」）才算。
   —— 这是「同一把尺」的前提，否则两侧不可比。
2. **选点**：
   - 只选 A 类段，C 类（有动作）不动；
   - 目标是把 A 类占比降到参考书自己的比例（雪中 6%）；
   - 选点**分散**（第 1 位优先，但不会全挤在第 1 位）；
   - 已经达标 → 一个都不选。
3. **插入**只改段落边界与那一段的内容，不动别的段。

## 数据

用**真书原文**（雪中悍刀行）与**真实生成正文**当输入 ——
不是人造的最小例子。
"""
import os
import tempfile
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
SCRATCH = pathlib.Path((os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())) / "novel-scratch"
sys.path.insert(0, str(BACKEND))

from app.style.beat import (  # noqa: E402
    BEAT_MAX_CHARS,
    BEAT_MIN_CHARS,
    DEFAULT_TARGET_A_RATIO,
    action_narration_len,
    find_slots,
    has_action,
    is_plain_dialogue,
    merge_beat,
)


# ── 1. A/C 判定 ──────────────────────────────────────────────────────────
def test_speech_tag_only_is_not_action():
    """「徐骁问道：」是提示语，**不是动作** —— 读者看不到任何反应。"""
    p = "徐骁问道：“看得懂？”"
    assert action_narration_len(p) < 3
    assert is_plain_dialogue(p)


def test_real_action_counts():
    """「徐骁也不恼怒，乐呵呵道：」有动作 ⇒ 不是光台词。"""
    p = "徐骁也不恼怒，乐呵呵道：“好好好，爹等着就是。”"
    assert action_narration_len(p) >= 3
    assert not is_plain_dialogue(p)


def test_narration_before_quote_counts():
    p = "徐凤年低头继续擦马镫，过了片刻才道：“那得看是按军马算。”"
    assert not is_plain_dialogue(p)


def test_pure_quote_is_plain():
    assert is_plain_dialogue("“世子想听实话？”")


def test_no_quote_is_not_plain():
    assert not is_plain_dialogue("他站在门口，没有说话。")


# ── 2. 选点 ──────────────────────────────────────────────────────────────
def _ps(*items):
    return list(items)


def test_finds_slots_only_on_plain_dialogue():
    ps = _ps(
        "老黄蹲在马厩里擦马镫。",
        "“世子想听实话？”",
        "“你跟着我这么多年。”",
        "“原来不在他身上。”",
    )
    slots = find_slots(ps, target_ratio=0.0)
    assert slots, "应该找到插入点"
    # 三段都是 A 类 ⇒ 三个都是候选
    assert len(slots) == 3
    assert all(is_plain_dialogue(ps[s.index]) for s in slots)


def test_skips_paragraphs_that_already_have_action():
    ps = _ps(
        "“这可造不了反。”",
        "徐凤年轻笑道：“苦惯了，只要有半口饭吃，就没人乐意揭竿而起。”",
        "“老许，你再说些风土人情。”",
    )
    slots = find_slots(ps, target_ratio=0.0)
    indices = [s.index for s in slots]
    assert 1 not in indices, "已有动作的段不该再插"


def test_no_slots_when_already_at_target():
    """A 类占比已达标 → 一个都不选。"""
    ps = _ps(
        "老黄低头继续擦马镫，过了片刻才道：“那得看是按军马算。”",
        "“还有这种分别？”",
        "徐凤年坐直了些：“还有这种分别？”",
    )
    assert find_slots(ps, target_ratio=0.5) == []


def test_slots_are_dispersed_not_all_first_rank():
    """选点要**分散**：不能全挤在连排第 1 位。

    依据：真书 A 类位次分布是第1位 38% / 第2位 21% / 第3位 14% ——
    一律插第 1 位会让 A 类全跑到第 2 位，形态会漂。
    """
    ps = _ps(
        "“第一句。”",
        "“第二句。”",
        "“第三句。”",
        "“第四句。”",
        "“第五句。”",
        "“第六句。”",
    )
    slots = find_slots(ps, target_ratio=0.0)
    assert len(slots) >= 3
    ranks = [s.rank for s in slots]
    assert len(set(ranks)) > 1, f"选点应分散在不同位次，实得 {ranks}"


def test_slot_context_is_available_for_prompt():
    ps = _ps(
        "老黄蹲在马厩里擦马镫。",
        "“世子想听实话？”",
        "“你跟着我这么多年。”",
    )
    slots = find_slots(ps, target_ratio=0.0)
    s = slots[0]
    assert s.before  # 上一段（给模型看上下文）
    assert s.after   # 下一段（要插在它前面）


def test_need_is_limited_by_target():
    """降到目标比例即可 —— 不是「能插就插」。"""
    ps = _ps(*[f"“第{i}句。”" for i in range(10)])
    # 10 段全 A 类；目标 50% ⇒ 约 5 段要变 C 类
    slots = find_slots(ps, target_ratio=0.5)
    assert 3 <= len(slots) <= 7, f"实际 {len(slots)}"


# ── 3. 插入 ──────────────────────────────────────────────────────────────
def test_merge_beat_merges_into_the_paragraph():
    """beat 要**并进**那一段，不能另起一段。

    真跑暴露的：第一版另起一段 ⇒ **A 类占比一点没降**（61% → 61%）。
    因为 A 类的定义是「整段只有引号」，插在它**前面**不改变它是不是 A 类。
    ⇒ beat 必须并到**引号前面**（变成「动作 + 台词」），整段才从 A 变 C。
    """
    ps = ["老黄蹲在马厩里擦马镫。", "“世子想听实话？”"]
    out = merge_beat(ps, 1, "老黄没有抬头。")
    assert len(out) == 2, "不能多出一段"
    assert out[1] == "老黄没有抬头。“世子想听实话？”"
    # 合并后那段不再是「光台词」
    assert has_action(out[1])
    assert not is_plain_dialogue(out[1])


def test_merge_beat_ignores_empty():
    ps = ["a", "b"]
    assert merge_beat(ps, 1, "  ") == ps


def test_merge_beat_out_of_range_is_safe():
    ps = ["a"]
    assert merge_beat(ps, 5, "x") == ps


# ── 4. 常量与真书一致 ────────────────────────────────────────────────────
def test_beat_length_range_matches_reference():
    """真书 C 类引号前叙述：p25 7 · p50 14 · p75 27 字。"""
    assert BEAT_MIN_CHARS <= 14 <= BEAT_MAX_CHARS
    assert BEAT_MIN_CHARS >= 5


def test_default_target_matches_reference():
    """兜底目标 = **15 本真书的中位**（72%），不是雪中（7%）。

    雪中悍刀行是**特例**（动作叙述极密）；拿它当标准会让系统把多数书
    硬掰成网球法（实测：动作密度升到真书 5 倍）。
    """
    assert DEFAULT_TARGET_A_RATIO == pytest.approx(0.72, abs=0.01)


def test_default_target_is_not_the_outlier_book():
    """兜底值**不能**是雪中那个特例。"""
    assert DEFAULT_TARGET_A_RATIO > 0.5


# ── 5. 真实语料上的验收（需要语料库）────────────────────────────────────
@pytest.mark.skipif(
    not (SCRATCH / "corpus-check/novel_dev.db").exists(),
    reason="需要验证语料库",
)
def test_on_real_reference_book_slots_are_few():
    """在真书上跑：选点数应该**很少**（真书自己已经达标）。"""
    from sqlalchemy import create_engine, text as sa_text

    from app.style.stylometry import paragraphs

    eng = create_engine(f"sqlite:///{(SCRATCH / 'corpus-check/novel_dev.db').as_posix()}")
    with eng.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT n.title, c.text FROM novel_chunks c "
            "JOIN raw_novels n ON n.id = c.novel_id ORDER BY n.title, c.idx")).fetchall()
    book = None
    by_novel: dict[str, list[str]] = {}
    for t, txt in rows:
        by_novel.setdefault(t, []).append(txt or "")
    for t in by_novel:
        if t.startswith("雪中"):
            book = t
            break
    assert book

    ps = [p for p in paragraphs("\n\n".join(by_novel[book])) if p.strip()]
    quote = [p for p in ps if "“" in p]
    a_ratio = sum(1 for p in quote if is_plain_dialogue(p)) / len(quote)
    # 真书的 A 类占比应当接近 6%
    assert 0.03 <= a_ratio <= 0.12, f"实测 A 类占比 {a_ratio:.1%}，与真书基准 6% 差太远"
    # 目标是 6% ⇒ 真书上几乎不需要插入
    assert len(find_slots(ps, target_ratio=DEFAULT_TARGET_A_RATIO)) <= 20
