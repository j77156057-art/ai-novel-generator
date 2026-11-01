"""计量分层：纯量层 / 新分母 / 标点与分段用法特征（2026-11-01）。

## 为什么要这组测试

这一轮把密度类特征的**分母**从「全字符数」换成「纯汉字数」，
并新增了 12 项「标点用法 / 分段用法」特征。

坏处是：**分母与分母混着的东西**（标点、空白）恰恰是**被测的写法**
—— 空白占比就是「多久换一次段、段间留多少空行」。
混进分母 ⇒ 跨书比较有系统性偏置（实测 26%）。

⇒ 这组测试把两条不变量钉住：

1. **偏置归零**：同一段文本**只改排版**（空行多少），密度类读数必须**不变**；
2. **尺只有一把**：汉字口径三处（stylometry / engine / candidate）必须一致，
   否则长度守恒、候选打分、档案密度各用各的数。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.style import stylometry as S  # noqa: E402

BODY = (
    "他站在门口，看着手里那张皱掉的纸，没有说话。\n\n"
    "“为什么不走？”老陶在院子里问，锤子敲在铁皮上，响了两声。\n\n"
    "“走不了。”\n\n"
    "雨停了。巷子里的灯一盏一盏亮起来，把他的影子拉得很长。\n\n"
    "她把伞收起来，抖了抖水，才低声说：“再等一会儿。”\n\n"
    "“我等得起。”\n\n"
    "“那就再等等。”\n\n"
    "他终于转过身，往巷子深处走去，脚步声一下一下地远去。\n"
)

#: 排版量：改排版时**允许**变的两项。
LAYOUT_FEATURES = {"空白占比", "段间空行密度"}


def _flat(text: str) -> str:
    """把段间空行合并（同一段文字，只改排版）。"""
    return text.replace("\n\n", "\n")


def _spaced(text: str) -> str:
    """把段间空行加多（同一段文字，只改排版）。"""
    return text.replace("\n\n", "\n\n\n")


# --------------------------------------------------------------------------- #
# 1. 纯量层
# --------------------------------------------------------------------------- #

def test_raw_counts_separates_text_punctuation_and_whitespace():
    """纯量层把三样东西分开报：汉字 / 标点 / 空白。"""
    c = S.raw_counts(BODY)
    assert c["汉字数"] == len(S._CJK_CHAR.findall(BODY))
    assert c["字符数"] == len(BODY)
    assert c["空白数"] == sum(1 for ch in BODY if ch.isspace())
    assert c["标点数"] > 0
    assert c["段数"] == len(S.paragraphs(BODY))
    assert c["句数"] > 0
    # 三块加起来应当等于全部字符（汉字 + 标点 + 空白 + 少量其它）
    assert c["汉字数"] < c["字符数"]


def test_cjk_count_is_the_single_ruler():
    """三处汉字计数必须同一口径（stylometry / engine / candidate）。"""
    from app.generation.candidate import _cjk_len
    from app.generation.engine import _cjk_count

    n = S.cjk_len(BODY)
    assert n == _cjk_count(BODY) == _cjk_len(BODY)
    assert n == 120, n  # 这段固定文本的汉字数（防口径漂移的锚点）


# --------------------------------------------------------------------------- #
# 2. 偏置归零检验
# --------------------------------------------------------------------------- #

def test_layout_change_does_not_move_density_readings():
    """**只改排版**（空行多少）⇒ 除排版量外，所有特征读数必须不变。

    这是这次改造的核心不变量：分母里不许再混进空白与标点。
    改之前：密度类特征的分母是 `len(text)`（含空白）⇒ 这条会红。
    """
    base = S.features(BODY)
    for variant, label in ((_flat(BODY), "合并空行"), (_spaced(BODY), "加多空行")):
        got = S.features(variant)
        for k in S.FEATURES:
            if k in LAYOUT_FEATURES:
                continue
            assert abs(got[k] - base[k]) < 1e-9, (
                f"{label}后「{k}」变了：{base[k]} → {got[k]}"
            )


def test_density_denominator_is_cjk_not_raw_chars():
    """密度类的分母是**纯汉字**：加标点不该把密度读数按比例压低。

    做法：同一段文字，后面追加几个**不参与任何密度计数**的符号
    （「」（））⇒ 汉字数不变 ⇒ 「每千汉字多少个句号」必须**不变**
    （旧口径会变，因为分母是全字符数）。

    ⚠️ 不能拿「。」来试：句号本身就是要计数的特征，加它读数当然会变。
    """
    tail = "「」（）"
    a = S.features(BODY)
    b = S.features(BODY + tail)
    for k in ("句号密度", "逗号密度", "虚词密度", "顿号密度"):
        assert abs(a[k] - b[k]) < 1e-6, (k, a[k], b[k])


def test_measurement_version_bumped_so_old_profiles_die():
    """换分母 + 加特征 ⇒ 测量口径版本必须 +1（老档案自动失效重拟）。"""
    assert S.MEASUREMENT_VERSION >= 4, S.MEASUREMENT_VERSION
    from app.style.pipeline import profile_fingerprint

    fp = profile_fingerprint([1, 2], 1200)
    assert fp["measurement"] == S.MEASUREMENT_VERSION


# --------------------------------------------------------------------------- #
# 3. 新特征：标点用法
# --------------------------------------------------------------------------- #

def test_new_punctuation_features_exist_and_behave():
    """新增的标点/分段特征必须真的在算（不是恒为 0 的占位）。"""
    # unit_chars 设成本文的**汉字数** ⇒ 长度归一化系数 = 1 ⇒ 比的是裸值
    # （「叙述段连长度」这类计数型特征会按长度缩放，那是另一件事）。
    vec = S.features(BODY, unit_chars=S.cjk_len(BODY))
    for k in ("顿号密度", "分号密度", "冒号密度", "破折号密度",
              "逗号句内位置", "引号内句号占比", "段均汉字数",
              "叙述段连长度", "对白段连长度", "段首对白率",
              "空白占比", "段间空行密度"):
        assert k in S.FEATURES, k
        assert k in S.FEATURE_NOTES, k
        assert k in vec, k
    # 这段文本里：没有顿号/分号/破折号；有冒号吗？（没有）⇒ 稀疏项应为 0
    assert vec["顿号密度"] == 0
    assert vec["分号密度"] == 0
    assert vec["破折号密度"] == 0
    # 逗号位置：逗号平均落在句子前段（这段的逗号都在短句里）
    assert 0.0 < vec["逗号句内位置"] < 1.0
    # 引号内句号：这段的台词多是「“走不了。”」这种 ⇒ 句号收在引号内
    assert vec["引号内句号占比"] > 0
    # 分段：叙述段连长度 ≥ 1（有两段连续叙述）
    assert vec["叙述段连长度"] >= 1
    assert vec["对白段连长度"] >= 1
    assert 0 < vec["段首对白率"] < 1
    assert vec["空白占比"] > 0


def test_pause_semicolon_colon_dash_are_measured():
    """顿号/分号/冒号/破折号：之前定义了却没有任何特征在用。"""
    text = "甲、乙、丙、丁。\n\n他说：走。\n\n但是没有。\n\n——走了。"
    vec = S.features(text, unit_chars=S.cjk_len(text))
    assert vec["顿号密度"] > 0
    assert vec["冒号密度"] > 0
    assert vec["破折号密度"] > 0
    assert vec["分号密度"] == 0  # 这段没有分号


def test_comma_position_moves_with_the_comma():
    """逗号位置要真的跟着逗号走（不是常量）。"""
    early = "他走了，走了，走了。"
    late = "他走了很久很久很久很久，走的时候没有回头。"
    a = S.features(early, unit_chars=S.cjk_len(early))["逗号句内位置"]
    b = S.features(late, unit_chars=S.cjk_len(late))["逗号句内位置"]
    assert a < b, (a, b)


# --------------------------------------------------------------------------- #
# 4. 档案与测量同一把尺
# --------------------------------------------------------------------------- #

def test_profile_and_runtime_use_the_same_ruler():
    """档案基线（拟合）≈ 运行时实测（同一把尺）。

    做法：用 `unit_chars = 该单元的汉字数` ⇒ 长度归一化系数 = 1，
    于是拟合值与裸实测值应当一致。
    """
    units = S.cut_units(BODY * 40, 1200)
    assert units, "切不出单元"
    unit = units[0]
    fitted = S.features(unit, unit_chars=S.cjk_len(unit))["句长均值"]
    lens = [len(s) for p in S.paragraphs(unit) for s in S.sentences(p)]
    measured = sum(lens) / len(lens)
    assert abs(fitted - measured) / measured < 0.05, (fitted, measured)


# --------------------------------------------------------------------------- #
# 5. 前后端共用同一份口径 fixture
# --------------------------------------------------------------------------- #

def test_shared_measure_fixture_matches_backend():
    """**前后端对同一份 fixture 断言** ⇒ 两边的口径不许漂。

    界面要显示「汉字数 / 标点占比 / 空白占比」，就必须在浏览器里自己数一遍；
    数出来的数必须与后端段长闸门量的那个数**是同一个数**。
    两边各写各的，早晚漂一处 —— 漂了之后的症状是
    「界面说 1050 字、闸门说 1200 字」，没人查得清是哪边错了。

    前端那份在 `frontend/tests/measure.test.ts`，读的是同一个文件。
    """
    import json

    fixture = Path(__file__).resolve().parent / "fixtures" / "measure_cases.json"
    cases = json.loads(fixture.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 10, "用例太少，挡不住口径漂移"
    for case in cases:
        text = case["text"]
        m = S.measure_summary(text)
        assert m["chars"] == case["chars"], (case["name"], "字符数", m["chars"])
        assert m["cjk"] == case["cjk"], (case["name"], "汉字数", m["cjk"])
        assert m["punct"] == case["punct"], (case["name"], "标点数", m["punct"])
        assert m["blank"] == case["blank"], (case["name"], "空白数", m["blank"])
        # 占比以字符数为分母（与前端 measure.ts 同）
        if m["chars"]:
            assert abs(m["punct_pct"] - case["punct"] / case["chars"] * 100) < 0.01
            assert abs(m["blank_pct"] - case["blank"] / case["chars"] * 100) < 0.01


def test_measure_summary_is_display_facing_but_measure_only():
    """展示层读数只回答「有多少」：不含任何风格判断（不含 z 分、不含带内判定）。"""
    m = S.measure_summary(BODY)
    assert set(m) == {"chars", "cjk", "punct", "blank", "paras", "sents",
                      "punct_pct", "blank_pct"}, sorted(m)
    assert m["cjk"] == S.cjk_len(BODY)
    assert m["chars"] == len(BODY)


# --------------------------------------------------------------------------- #
# 6. 比值类特征：分子分母必须同一单位
# --------------------------------------------------------------------------- #

#: 「分子 ÷ 分母」型特征（读数必须落在 [0,1]）。
#:
#: 刻意**显式列出**而不是「非密度特征全算」——
#: 非密度特征里还有句长均值、段均汉字数、段连长度这些**本来就可以 > 1** 的量。
RATIO_FEATURES = (
    "短句占比", "长句占比", "对白字占比", "对白段占比", "纯台词段占比",
    "段尾升华率", "短串占比", "逗号句内位置", "引号内句号占比",
    "段首对白率", "空白占比",
)


def test_ratio_features_are_unit_consistent():
    """**比值的分子分母必须同一单位**，否则读数会越界或系统性偏大。

    踩过的坑：换分母时把 `对白字占比` 的分母换成了纯汉字，
    分子 `dialogue_char_count` 却还按**字符**算（引号里的句号也算进去）
    ⇒ `“你来了。”他说。` 读成 **0.8**，而真实占比是 3/5 = **0.6**。

    这类错**不会被**「偏置归零检验」抓到 —— 那一检验只看密度类。
    """
    text = "“你来了。”他说。"
    # unit_chars = 该文本的汉字数 ⇒ 长度归一化系数 = 1 ⇒ 量的是**裸比值**
    # （否则「方案 C」的长度因子会把短文本整体压低，读数不是这个比值本身）。
    vec = S.features(text, unit_chars=S.cjk_len(text))
    assert abs(vec["对白字占比"] - 0.6) < 1e-6, vec["对白字占比"]
    for k in RATIO_FEATURES:
        assert k in vec, k
        assert -1e-9 <= vec[k] <= 1 + 1e-9, (k, vec[k])


def test_ratio_features_stay_in_range_on_real_text():
    """同一组不变量在**真实正文**上也成立（短文本里最容易被越界）。"""
    vec = S.features(BODY)
    for k in RATIO_FEATURES:
        assert -1e-9 <= vec[k] <= 1 + 1e-9, (k, vec[k])
