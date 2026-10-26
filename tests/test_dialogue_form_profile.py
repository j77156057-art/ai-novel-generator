"""对白形态：把「对白写得碎」变成两个可验收的数字。

## 为什么要有这两个数

真书实测：含对白的段落**典型 47~61 字**，其中「只有台词、前面没写动作/神态」
的**纯台词段只占 21%~25%**；模型写的是典型 9~26 字、纯台词段 **52%~87%**。

这是「生成比参考书碎」的主要来源，而**已有的句长、标点密度特征量不出来** ——
一段 20 字的纯台词在句长分布上完全正常。于是把它变成两个数字：
`纯台词段占比`（进特征向量，可算 z、可判偏离）+ `dialogue_form`（样本级汇总，
进提示词基线块当目标）。

## 要守住的

1. **纯台词段的判定**要准：去掉引号内容后只剩标点/空白的才算纯台词段；
   「动作/神态 + 台词」不算。
2. **长度用中位数，不用平均数**：逐单元算再平均 = 均值的均值，会被超长对白段
   拉爆（同一份语料：逐单元均值 142 字 vs 汇总中位 59 字）。
3. **老档案不能显示 0**：没有 `dialogue_form` 的老档案不渲染这一行。
4. **指纹要失效老档案**：特征集变了，老档案里的缺失项会被算成 z=0
   （= 「从不偏离」），那是假的。
"""
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.style.stylometry import (  # noqa: E402
    FEATURES,
    MEASUREMENT_VERSION,
    describe,
    dialogue_segment_form,
    features,
    fit_profile_v2,
    pooled_dialogue_form,
)

# 三种对白段形态：
# 1) 纯台词段：只有引号内容
# 2) 动作/神态 + 台词（参考书 77% 的形态）
# 3) 叙述 + 台词
BARE = "“都退后。”"
WITH_ACTION = "徐龙象握住刀柄，往前半步：“爹，让我进去。”"
NARRATIVE = "院子里静了下来，只有风声。老人抬起头，看着门口的年轻人：“你来了。”"


def test_bare_segment_detection():
    """纯台词段判定：只有引号内容、前面没动作/神态的才算。"""
    mean_len, bare = dialogue_segment_form([BARE, WITH_ACTION, NARRATIVE])
    # 3 段里 1 段是纯台词段
    assert bare == 1 / 3, bare
    # 逐单元口径给的是**平均**长度（样本级中位数见 pooled_dialogue_form）
    expected = statistics.fmean([len(BARE), len(WITH_ACTION), len(NARRATIVE)])
    assert mean_len == expected, (mean_len, expected)


def test_action_prefixed_segment_is_not_bare():
    """「动作/神态 + 台词」不能被判成纯台词段 —— 那是参考书的主力形态。"""
    _, bare = dialogue_segment_form([WITH_ACTION])
    assert bare == 0.0, bare


def test_no_dialogue_paragraphs():
    """没有对白时不能除零。"""
    med, bare = dialogue_segment_form(["院子里静了下来。", "风穿过长廊。"])
    assert med == 0.0
    assert bare == 0.0


def test_feature_vector_has_bare_ratio():
    """纯台词段占比要进特征向量（可算 z、可判偏离）。

    文本要凑到 3000 字上下 —— 非密度特征会按长度归一化（方案 C），
    短文本上算出来的值是缩放过的，不能直接跟裸比例比。
    """
    assert "纯台词段占比" in FEATURES
    unit = "\n".join([BARE, WITH_ACTION, NARRATIVE])
    text = "\n".join([unit] * 40)  # ≈ 3000 字
    vec = features(text)
    # 归一化系数按长度算（方案 C），所以是「接近」而不是精确等于
    assert abs(vec["纯台词段占比"] - 1 / 3) < 0.03, vec["纯台词段占比"]


def test_pooled_form_prefers_median_over_mean():
    """样本级汇总用中位数：一个超长对白段不能把「典型长度」拉爆。"""
    units = [
        "\n".join([BARE, WITH_ACTION, NARRATIVE]),
        "\n".join([BARE, WITH_ACTION, NARRATIVE]),
        "\n".join([BARE, WITH_ACTION, "长" * 2000 + "“尾巴。”"]),
    ]
    pooled = pooled_dialogue_form(units)
    assert pooled["n_segments"] == 9, pooled
    # 中位被三个短段撑住，平均被那个 2000 字的段拉高 —— 这就是要用中位的原因
    assert pooled["seg_len_median"] < pooled["seg_len_mean"], pooled
    assert pooled["bare_ratio"] == 3 / 9, pooled


def test_fit_profile_stores_dialogue_form():
    """档案里要存样本级汇总，否则提示词基线块没数据可渲染。"""
    text = "\n".join([BARE, WITH_ACTION, NARRATIVE] * 200)
    profile = fit_profile_v2([text], unit_chars=3000, label="t")
    bucket = profile["buckets"]["medium"]
    form = bucket["dialogue_form"]
    assert form["n_segments"] > 0, form
    assert 0.0 <= form["bare_ratio"] <= 1.0
    assert form["seg_len_median"] > 0


def test_describe_renders_dialogue_line():
    """有汇总数据时，基线块要出现对白形态那一行。

    格式随「基线块规范化」改过：现在是固定句式 `【标签】内容`
    （见 docs/style-prompt-refinement-design.md §2.2/§2.3）。
    """
    text = "\n".join([BARE, WITH_ACTION, NARRATIVE] * 200)
    profile = fit_profile_v2([text], unit_chars=3000, label="t")
    out = describe(profile)
    assert "【对白段】" in out, out
    assert "典型" in out, out
    assert "纯台词段" in out, out


def test_describe_skips_line_for_legacy_profile():
    """老档案没有 dialogue_form → 不渲染这行（不能显示 0）。"""
    profile = {
        "version": 2,
        "_meta": {"base_unit_chars": 3000},
        "buckets": {
            "medium": {
                "unit_chars": 3000,
                "feat": {
                    "句长均值": {"mean": 56.0, "std": 18.8, "p10": 34.0,
                                 "p50": 53.5, "p90": 84.2, "min": 24.9, "max": 109.7},
                    "句长CV": {"mean": 0.6, "std": 0.1, "p10": 0.4,
                               "p50": 0.6, "p90": 0.8, "min": 0.3, "max": 1.0},
                    "段均句数": {"mean": 1.8, "std": 0.5, "p10": 1.0,
                                 "p50": 1.7, "p90": 2.6, "min": 1.0, "max": 4.0},
                    "段长CV": {"mean": 0.97, "std": 0.2, "p10": 0.6,
                               "p50": 0.9, "p90": 1.3, "min": 0.4, "max": 1.8},
                    "对白字占比": {"mean": 0.175, "std": 0.122, "p10": 0.027,
                                   "p50": 0.155, "p90": 0.358, "min": 0.0, "max": 0.553},
                    "对白段占比": {"mean": 0.43, "std": 0.1, "p10": 0.2,
                                   "p50": 0.4, "p90": 0.7, "min": 0.0, "max": 1.0},
                    "逗号密度": {"mean": 67.0, "std": 5.0, "p10": 60.0,
                                 "p50": 66.0, "p90": 75.0, "min": 50.0, "max": 90.0},
                    "句号密度": {"mean": 12.1, "std": 2.0, "p10": 9.0,
                                 "p50": 12.0, "p90": 15.0, "min": 7.0, "max": 20.0},
                    "感叹号密度": {"mean": 0.8, "std": 0.5, "p10": 0.0,
                                   "p50": 0.7, "p90": 1.5, "min": 0.0, "max": 3.0},
                    "问号密度": {"mean": 2.5, "std": 1.0, "p10": 1.0,
                                 "p50": 2.3, "p90": 4.0, "min": 0.0, "max": 6.0},
                    "省略号密度": {"mean": 0.2, "std": 0.2, "p10": 0.0,
                                   "p50": 0.2, "p90": 0.5, "min": 0.0, "max": 1.0},
                    "比喻密度": {"mean": 2.0, "std": 1.0, "p10": 0.5,
                                 "p50": 1.8, "p90": 3.5, "min": 0.0, "max": 6.0},
                    "情绪直陈密度": {"mean": 0.4, "std": 0.3, "p10": 0.0,
                                     "p50": 0.3, "p90": 0.9, "min": 0.0, "max": 2.0},
                    "段尾升华率": {"mean": 0.03, "std": 0.03, "p10": 0.0,
                                   "p50": 0.02, "p90": 0.063, "min": 0.0, "max": 0.12},
                    "四字格密度": {"mean": 9.7, "std": 2.0, "p10": 6.0,
                                   "p50": 9.5, "p90": 13.0, "min": 4.0, "max": 18.0},
                    "短串占比": {"mean": 0.062, "std": 0.02, "p10": 0.03,
                                 "p50": 0.06, "p90": 0.09, "min": 0.02, "max": 0.15},
                },
                "holdout": {},
            },
        },
    }
    out = describe(profile)
    assert "对白段：典型" not in out, out


def test_measurement_version_bumped_for_new_features():
    """特征集变了 → 测量口径版本必须 +1（老档案要自动失效重拟）。"""
    assert MEASUREMENT_VERSION >= 3, MEASUREMENT_VERSION
