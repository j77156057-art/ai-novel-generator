"""说话归因召回修复的回归测试（ADR：先红后绿）。

缺陷（2026-10 QA 实测复现）：`KGExtractor` 的说话归属 pattern 口径过窄——
`_SPEAK_ADV_PATTERN` 只认单字副词、`_SPEAK_VERB_PATTERN` 不认复合动词，
导致现代网文主句式整句漏抽：

    宁折枝低声说道："好。"   → characters == {}（多字副词「低声」）
    宁折枝开口道："好。"     → characters == {}（复合动词「开口」）
    宁折枝沉声道："好。"     → characters == {}（多字副词「沉声」）

后果：KG 簿记的「清空 + 全量 replay」只忠实 replay extractor 的产出，
这些句式被漏抽后，重生成时图谱看不出变化。

本文件三组钉子：
- T1–T3：QA 复现例句必须抽到角色（修复前红 → 修复后绿）；
- T4–T8：「不新增伪角色」护栏——历史上为治伪角色专门把 pattern 收窄
  （实测「贾母又」78 次、「贾母便」102 次、「宝玉听」388 次），
  扩召回**不得**把这些粘连形态放回来；无主语的裸副词句也不得造出新角色；
- T9–T11：时间词停用护栏（round 2 回归修复）——62609a5 的 2 字副词槽让
  「许久后低声」占满 {0,2}，时间词「许久」被误标 speaking=True（属性级回归，
  角色总数口径看不见）；护栏只挡时间词整体，不误伤真许姓角色。

纪律：本文件先写、先在未修复的代码上跑出**红**，修完再跑出绿。
"""
from __future__ import annotations

import os
import sys

# ─── bootstrap：与仓库其它测试一致，把 backend/ 加进 sys.path ────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.kg.build import extract_from_text  # noqa: E402


def _characters(text: str) -> set[str]:
    """走 QA 复现时用的同一入口（extract_from_text），返回抽到的角色集合。"""
    result, _ = extract_from_text(text, segment_no=1)
    return set(result.characters)


def _speaking(text: str) -> set[str]:
    result, _ = extract_from_text(text, segment_no=1)
    return {n for n, c in result.characters.items() if c.speaking}


# ---------------------------------------------------------------------------
# T1–T3：QA 复现例句（修复前 characters == {} → 红）
# ---------------------------------------------------------------------------


def test_multichar_adverb_speech_attribution():
    """「宁折枝低声说道」必须抽出宁折枝（2 字方式副词进副词槽）。

    修复前：副词槽只认单字，「低声」整体匹配不上 → 整句漏抽 → 红。
    """
    chars = _characters('宁折枝低声说道："好。"')
    assert "宁折枝" in chars, f"低声说道 应归因到宁折枝，实际 {chars}"
    assert "宁折枝" in _speaking('宁折枝低声说道："好。"'), (
        "宁折枝应带 speaking 证据（入图通路之一）"
    )


def test_compound_verb_speech_attribution():
    """「宁折枝开口道」必须抽出宁折枝（复合动词进动词族）。

    修复前：动词族只有 听说/[一-鿿]道/道/说/问/答/笑，「开口」匹配不上 → 红。
    """
    chars = _characters('宁折枝开口道："好。"')
    assert "宁折枝" in chars, f"开口道 应归因到宁折枝，实际 {chars}"
    assert "宁折枝" in _speaking('宁折枝开口道："好。"')


def test_multichar_adverb_shengsheng():
    """「宁折枝沉声道」必须抽出宁折枝（沉声既是方式副词又可接单字道）。"""
    chars = _characters('宁折枝沉声道："好。"')
    assert "宁折枝" in chars, f"沉声道 应归因到宁折枝，实际 {chars}"


def test_mixed_adverb_count_within_bound():
    """副词槽 {0,2} 上界内，1 字 + 2 字副词可混用（「才缓缓开口」）。

    这是副词槽设计的边界钉：计数上界仍是 2，只是单个副词可长至 2 字。
    """
    chars = _characters('何母才缓缓开口，"萱萱，妈妈知道。"')
    assert "何母" in chars, f"才缓缓开口 应归因到何母，实际 {chars}"
    chars2 = _characters('南宫爵低声缓缓问道："你确定？"')
    assert "南宫爵" in chars2, f"两个 2 字副词（低声+缓缓）应仍归因到南宫爵，实际 {chars2}"


# ---------------------------------------------------------------------------
# T4–T8：不新增伪角色（历史教训的钉子；新旧代码下都必须绿）
# ---------------------------------------------------------------------------


def test_no_glued_fake_name_jiamu_you():
    """「贾母又问道」只抽贾母，不得抽出粘连体「贾母又」。

    历史实测：贪婪版/无副词槽时「贾母又」出现 78 次。扩召回不得放它回来。
    """
    chars = _characters('贾母又问道："你可知错？"')
    assert "贾母" in chars, f"贾母又问道 应归因到贾母，实际 {chars}"
    assert "贾母又" not in chars, f"不得抽出粘连体「贾母又」，实际 {chars}"


def test_no_glued_fake_name_fengjie_bian():
    """「凤姐便道」只抽凤姐，不得抽出「凤姐便」（历史实测 50 次）。"""
    chars = _characters('凤姐便道："还不快退下。"')
    assert "凤姐" in chars, f"凤姐便道 应归因到凤姐，实际 {chars}"
    assert "凤姐便" not in chars, f"不得抽出粘连体「凤姐便」，实际 {chars}"


def test_no_glued_fake_name_baoyu_ting():
    """「宝玉听说」只抽宝玉，不得抽出「宝玉听」（历史实测 388 次）。"""
    chars = _characters("宝玉听说，忙赶了过来。")
    assert "宝玉" in chars, f"宝玉听说 应归因到宝玉，实际 {chars}"
    assert "宝玉听" not in chars, f"不得抽出粘连体「宝玉听」，实际 {chars}"


def test_bare_adverb_speech_creates_no_character():
    """无主语的裸方式副词句（「低声说道：“好。”」）不得造出新角色。

    枚举的 2 字副词首字都不是姓氏、末字都不是名用字——裸出时必须过不了
    _valid_name。这条钉子挡住未来有人把副词槽放宽成贪婪形式。
    """
    chars = _characters('低声说道："好。"')
    assert chars == set(), f"无主语裸副词句不得抽出任何角色，实际 {chars}"
    chars2 = _characters('沉声道："此事需从长计议。"')
    assert chars2 == set(), f"无主语沉声道不得抽出任何角色，实际 {chars2}"


def test_control_single_char_patterns_unchanged():
    """对照：原有单字/「X道」句式行为不变（防止修复顺手改坏老路径）。"""
    assert _characters('林夜说道："走吧。"') == {"林夜"}
    assert _characters('林夜问："去哪？"') == {"林夜"}
    assert _characters('林夜笑道："走了。"') == {"林夜"}


# ---------------------------------------------------------------------------
# T9–T11：时间词停用护栏（62609a5 引入的属性级回归，QA 复验抓到）
# ---------------------------------------------------------------------------


def test_time_word_not_attributed_as_speaker():
    """「许久后低声道」是「时间词 + 方式副词」，许久不得被归因为说话人。

    62609a5（未加护栏）：副词槽 2 字化后「后 + 低声」恰好占满 {0,2}、句末
    「道」收尾 → 整句归因给「许久」（许久首字「许」是姓氏，2 字名过
    _valid_name）→ speaking=True → 红。加 _TIME_WORD_STOP 护栏后转绿。

    这是**属性级回归**：角色总数不变（许久本就经主语 pass 在图里），
    只有 speaking 被升级——总数口径的对照表看不见它。
    """
    chars = _characters('许久后低声道："好。"')
    assert chars == set(), f"时间词「许久」不得被归因，实际 {chars}"
    # 变体：另一个 2 字副词 + 单字副词混排
    assert _characters('许久才沉声道："此事再议。"') == set(), (
        "「才 + 沉声」混排同样不得归因给时间词"
    )


def test_time_word_guard_does_not_block_real_xu_surname():
    """护栏只挡时间词整体，**不误伤真许姓角色**（QA 明确要求的证明）。

    停用表拦的是「许久」这个词，不是「许」这个姓——许七安/许墨必须照常
    抽到（含 2 字副词句式）。
    """
    assert _characters('许七安说道："好。"') == {"许七安"}
    chars = _characters('许墨低声问："你确定？"')
    assert "许墨" in chars, f"许墨低声问 应归因到许墨，实际 {chars}"
    assert "许墨" in _speaking('许墨低声问："你确定？"'), (
        "许墨应带 speaking 证据（护栏不得连真角色的归因一起挡掉）"
    )


def test_time_word_guard_keeps_round1_fixes():
    """护栏不加戏：round 1 的正确归因（低声说道/开口/混合副词）不受影响。"""
    assert _characters('宁折枝低声说道："好。"') == {"宁折枝"}
    assert _characters('宁折枝开口道："好。"') == {"宁折枝"}
    assert _characters('何母才缓缓开口，"萱萱，妈妈知道。"') == {"何母"}
    # 伪角色钉子也仍成立
    assert _characters('贾母又问道："你可知错？"') == {"贾母"}
