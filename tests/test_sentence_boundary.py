"""句子边界的口径：闭合引号必须跟着它的那一句。

## 为什么要单独钉一条

中文里句末标点常在引号**内**（`…不可匹敌。”`）。只按 `。！？…` 切，
闭合引号会被甩成下一个「句」—— 真书 990 章实测：**24% 的「句」是孤立的 `”`**
（均长 2.2 字）。

后果不是「多算一句」，而是**句数虚增三成、平均句长压低三成**：
档案基线、诊断判定、文体距离全部跟着失真（老档案 56.0 vs 同口径实测 39.2，差 40%）。

所以这里钉三件事：
  1. 闭合引号/括号不产生独立「句」；
  2. 测量口径（`stylometry.sentences`）与切块口径（`ingestion.chunk`）**认同一个句**；
  3. 档案基线与运行时测量**用同一把尺**（拟合出来的句长均值 ≈ 实测的句长均值）。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.ingestion.chunk import _split_sentences as chunk_split
from app.style.stylometry import sentences

CLOSERS = "」』”’）)】》〕"


def test_closing_quote_stays_with_its_sentence():
    assert sentences("“有。”") == ["“有。”"]
    assert sentences("他笑了笑：“好。”然后转身走了。") == ["他笑了笑：“好。”", "然后转身走了。"]


def test_no_orphan_closer_sentences():
    """对白密集的段落里，不该出现只剩一个闭合引号的「句」。"""
    text = (
        "曹长卿平静道：“前辈何止第八？世人只知李剑神两袖青蛇不可匹敌。”\n"
        "“有。”\n"
        "“嗯？”\n"
        "“冷。”\n"
        "老剑神忽然大笑起来，笑声震动屋檐，灰尘簌簌落下。\n"
    )
    orphans = [s for s in sentences(text) if s and s[0] in CLOSERS]
    assert not orphans, f"仍有孤立闭合引号：{orphans}"


def test_measurement_and_chunking_share_the_same_sentence_boundary():
    """同一个语料，切块与测量必须认同一个「句」。

    两边口径不一致时，档案（按块文本拟合）与运行时（按生成段测量）就不是同一把尺。
    """
    text = "曹长卿平静道：“前辈何止第八？世人只知李剑神两袖青蛇不可匹敌。”然后他走了。"
    assert sentences(text) == chunk_split(text)


def test_ellipsis_is_one_terminator():
    """省略号整串算一个终结符（否则「……」会切出 2~6 个空句）。

    注意：整串会被**归一成一个** `…`（`_ELLIPSIS_RUN`），所以句长比原文少 1 字 ——
    这是既有行为，且对参考与生成两侧影响一致，不影响比值。
    """
    out = sentences("他愣住了……然后笑了。")
    assert len(out) == 2, f"省略号不该切出多句：{out}"
    assert out[1] == "然后笑了。"


def test_profile_baseline_matches_runtime_measurement():
    """档案基线必须与运行时测量同一口径：拟合出的句长均值 ≈ 实测句长均值。

    这是「档案与测量不是同一把尺」的自检：口径漂了（比如分句器把闭合引号
    甩成独立句），拟合值与实测值会差出三成。
    """
    from app.style.stylometry import cut_units, features

    # 对白密集的语料（闭合引号问题最容易暴露的那种）
    body = (
        "曹长卿平静道：“前辈何止第八？世人只知李剑神两袖青蛇不可匹敌。”\n"
        "“有。”\n"
        "“嗯？”\n"
        "“冷。”\n"
        "老剑神忽然大笑起来，笑声震动屋檐，灰尘簌簌落下，桌上酒碗微微颤动。\n"
    )
    text = body * 40
    units = cut_units(text, 3000, max_units=20)
    assert units, "切不出单元"
    fitted = features(units[0])["句长均值"]

    # 同一段文字的实测句长均值（同一把尺）
    lens = [len(s) for p in text.split("\n") if p.strip() for s in sentences(p)]
    measured = sum(lens) / len(lens)
    assert abs(fitted - measured) / measured < 0.05, (
        f"档案口径({fitted:.1f})与实测({measured:.1f})对不上：不是同一把尺"
    )


def test_measurement_version_is_in_the_fingerprint():
    """测量口径进指纹：口径一变，老档案自动失效（不用人记得清缓存）。"""
    from app.style.pipeline import profile_fingerprint
    from app.style.stylometry import MEASUREMENT_VERSION

    fp = profile_fingerprint([1], unit_chars=3000)
    assert "measurement" in fp
    assert fp["measurement"] == MEASUREMENT_VERSION


def test_old_fingerprint_no_longer_matches():
    """老档案（指纹里没有 measurement 键）必须判为失效。"""
    from app.style.pipeline import profile_fingerprint

    fp = profile_fingerprint([1], unit_chars=3000)
    old = {k: v for k, v in fp.items() if k != "measurement"}
    assert old != fp, "去掉 measurement 键后仍与新指纹相等 → 老档案不会被判失效"
