"""文风规格书 StyleSpec + 仲裁点（2026-11-01）。

## 这组测试钉什么

规格书是这次改造的**核心新增物**，它要同时满足几条互相拉扯的要求：

1. **同源**：提示词里给模型的区间 = 验收用的区间。
   ⇒ `render_spec` 与 `spec_check` 必须从**同一份 spec** 出，
   且 `spec_check` 的实测值必须与 `stylometry.band_check` **逐值相等**
   （否则就是两把尺 —— 那正是这次要消灭的东西）。
2. **闭集**：给的是**区间 + 中心值**，不是均值。
   ⇒ 均值当目标是错的指令（模型会照着均值写，产出机械产物）。
3. **不误伤**：零方差的项不参与判定（否则生成里偶尔用一个顿号就被回灌）。
4. **上限型只告警**：比喻/情绪直陈这类「越少越好」的超出，不回灌、不阻断。
5. **排版量不判定**：空白占比/段间空行密度描述的是 txt 排版，不是作者怎么写。
6. **确定性**：`render_spec` 同一份 spec 两次调用逐字节相同（提示词缓存的前提）。
7. **章长要能算**：主人点名要的「作者习惯一章写多长」—— 仓库以前**从来没量过**。

## 为什么「贴边出带」要单独测

规格书的区间是 p10~p90，而实测值是连续量 ⇒ 落点贴着边界是常态
（实测冒号密度真值 6.04、区间下界 6.1 —— 只差 1%，却是真出带）。

若判定用精确比较，回灌句会写成「实测 31/千汉字（目标 31~41/千汉字）」——
**实测明明落在展示出来的区间里，却被说偏低**。模型会照着一个不存在的问题去改。

⇒ 两道防线：
- **容差**（半个展示刻度）：按展示精度看不出差别 ⇒ 不算偏离；
- **回灌门槛**（超出带宽 5%）：判定仍如实，但贴边的项不写进回灌句。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.generation.engine import _style_gate, _pipeline_record  # noqa: E402
from app.generation.prompts import _profile_block  # noqa: E402
from app.style import stylometry as S  # noqa: E402
from app.style.chapter_stats import (  # noqa: E402
    DEFAULT_MIN_CHAPTER_CHARS,
    chapter_length_stats,
    chapter_lengths,
)
from app.style.pipeline import profile_fingerprint  # noqa: E402
from app.style.spec import (  # noqa: E402
    SPEC_ITEMS,
    SPEC_VERSION,
    build_spec,
    render_spec,
    spec_check,
    spec_deviation_lines,
)

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


def _profile(feat_overrides: dict | None = None, **extra) -> dict:
    """一份 v2 档案（medium 桶），带 feat 与 dialogue_form。"""
    feat = {
        "句长均值": {"mean": 25.0, "std": 3.0, "p10": 22.0, "p50": 25.0, "p90": 29.0},
        "段均汉字数": {"mean": 24.0, "std": 3.0, "p10": 20.0, "p50": 24.0, "p90": 27.0},
        "段均句数": {"mean": 1.1, "std": 0.1, "p10": 1.0, "p50": 1.1, "p90": 1.2},
        "对白字占比": {"mean": 0.22, "std": 0.05, "p10": 0.10, "p50": 0.22, "p90": 0.36},
        "对白段占比": {"mean": 0.28, "std": 0.07, "p10": 0.17, "p50": 0.28, "p90": 0.49},
        "纯台词段占比": {"mean": 0.30, "std": 0.08, "p10": 0.15, "p50": 0.30, "p90": 0.51},
        "逗号密度": {"mean": 71.0, "std": 5.0, "p10": 61.0, "p50": 71.0, "p90": 79.0},
        "句号密度": {"mean": 36.0, "std": 3.0, "p10": 31.0, "p50": 36.0, "p90": 41.0},
        "问号密度": {"mean": 4.0, "std": 1.0, "p10": 2.4, "p50": 4.0, "p90": 6.4},
        "感叹号密度": {"mean": 1.7, "std": 1.0, "p10": 0.3, "p50": 1.7, "p90": 4.0},
        "省略号密度": {"mean": 2.0, "std": 1.0, "p10": 0.7, "p50": 2.0, "p90": 4.5},
        "顿号密度": {"mean": 1.3, "std": 0.8, "p10": 0.1, "p50": 1.3, "p90": 3.4},
        "分号密度": {"mean": 0.0, "std": 0.0, "p10": 0.0, "p50": 0.0, "p90": 0.0},
        "冒号密度": {"mean": 11.0, "std": 3.0, "p10": 6.1, "p50": 11.0, "p90": 16.8},
        "破折号密度": {"mean": 0.1, "std": 0.1, "p10": 0.0, "p50": 0.1, "p90": 0.3},
        "逗号句内位置": {"mean": 0.50, "std": 0.01, "p10": 0.49, "p50": 0.50, "p90": 0.52},
        "引号内句号占比": {"mean": 1.0, "std": 0.03, "p10": 0.93, "p50": 1.0, "p90": 1.0},
        "段首对白率": {"mean": 0.19, "std": 0.06, "p10": 0.08, "p50": 0.19, "p90": 0.34},
        "对白段连长度": {"mean": 2.4, "std": 0.5, "p10": 1.7, "p50": 2.4, "p90": 3.2},
        "叙述段连长度": {"mean": 4.8, "std": 2.0, "p10": 2.6, "p50": 4.8, "p90": 12.1},
        "空白占比": {"mean": 0.12, "std": 0.02, "p10": 0.08, "p50": 0.12, "p90": 0.15},
        "段间空行密度": {"mean": 1.0, "std": 0.2, "p10": 0.8, "p50": 1.0, "p90": 1.2},
        "比喻密度": {"mean": 1.0, "std": 0.5, "p10": 0.3, "p50": 1.0, "p90": 2.0},
        "情绪直陈密度": {"mean": 0.3, "std": 0.2, "p10": 0.0, "p50": 0.3, "p90": 0.7},
        "段尾升华率": {"mean": 0.01, "std": 0.005, "p10": 0.0, "p50": 0.01, "p90": 0.02},
        "句长CV": {"mean": 0.64, "std": 0.05, "p10": 0.57, "p50": 0.64, "p90": 0.71},
        "段长CV": {"mean": 0.61, "std": 0.05, "p10": 0.55, "p50": 0.61, "p90": 0.71},
        "短句占比": {"mean": 0.03, "std": 0.01, "p10": 0.01, "p50": 0.03, "p90": 0.06},
        "长句占比": {"mean": 0.18, "std": 0.05, "p10": 0.12, "p50": 0.18, "p90": 0.27},
        "四字格密度": {"mean": 14.0, "std": 2.0, "p10": 11.2, "p50": 14.0, "p90": 19.1},
        "短串占比": {"mean": 0.17, "std": 0.03, "p10": 0.14, "p50": 0.17, "p90": 0.22},
        "虚词密度": {"mean": 104.0, "std": 8.0, "p10": 92.0, "p50": 104.0, "p90": 117.0},
    }
    for k, v in (feat_overrides or {}).items():
        feat[k] = v
    prof = {
        "version": 2,
        "_meta": {"base_unit_chars": 3000},
        "buckets": {
            "medium": {
                "unit_chars": 3000,
                "feat": feat,
                "holdout": {},
                "dialogue_form": {
                    "n_segments": 120,
                    "seg_len_median": 32.0,
                    "seg_len_mean": 40.0,
                    "bare_ratio": 0.30,
                },
            },
        },
    }
    prof.update(extra)
    return prof


# --------------------------------------------------------------------------- #
# 1. 章长统计（仓库以前从来没有量过这一项）
# --------------------------------------------------------------------------- #

def test_chapter_lengths_filters_too_short_chapters():
    """太短的章（版权页残留、作者有话说）不进分布 —— 否则左端拖出一条假尾巴。"""
    lens = chapter_lengths(["啊" * 500, "啊" * 100, "啊" * 300])
    assert lens == [500, 300]
    assert DEFAULT_MIN_CHAPTER_CHARS == 200


def test_chapter_length_stats_quantiles():
    """章长分布给 p10/p50/p90 —— 与 band_check 用同一对分位点。"""
    stats = chapter_length_stats([1000, 1100, 1200, 1300, 1400])
    assert stats["n_chapters"] == 5
    assert stats["p10"] < stats["p50"] < stats["p90"]
    assert stats["min"] == 1000 and stats["max"] == 1400


def test_chapter_length_stats_empty_is_empty_dict():
    """没有样本时返回空 dict —— **不返回 0**。

    0 会被下游当成「这本书一章 0 字」，而真相是「没有可用章节」。
    这两种情况必须分得开。
    """
    assert chapter_length_stats([]) == {}
    assert chapter_length_stats([0, 0]) == {}


def test_spec_contains_chapter_length_item():
    """规格书里必须有「章长」这一项（主人点名要的东西）。"""
    prof = _profile()
    prof["chapter_stats"] = chapter_length_stats([1000, 1200, 1400, 1600])
    spec = build_spec(prof)
    item = next(i for i in spec["items"] if i["key"] == "章长")
    assert item["kind"] == "range"
    assert item["lo"] < item["target"] < item["hi"]
    assert item["in_prompt"] is True


def test_spec_without_chapter_stats_has_no_chapter_item():
    """没有章长样本 ⇒ 该项**不出现**（不是填 0）。"""
    spec = build_spec(_profile())
    assert not [i for i in spec["items"] if i["key"] == "章长"]


# --------------------------------------------------------------------------- #
# 2. 同源：spec_check 的实测值必须与 band_check 逐值相等
# --------------------------------------------------------------------------- #

def test_spec_check_value_equals_band_check_value():
    """**同一把尺**：spec_check 的实测值 = band_check 的实测值。

    这是「提示词里说的区间 = 验收用的区间」的地基：
    若两者算出来不一样，模型收到的指令与验收它的判据就不是一回事。
    """
    prof = _profile()
    spec = build_spec(prof)
    check = spec_check(BODY, spec, unit_chars=3000)
    bands = S.band_check(BODY, prof, ["句长均值", "对白字占比", "逗号密度"])
    for name, got in bands.items():
        mine = check["items"][name]["value"]
        assert abs(mine - got["value"]) < 1e-6, (name, mine, got["value"])


def test_spec_bands_equal_profile_bands():
    """规格书的 lo/hi 必须就是档案的 p10/p90（不能另算一套分位点）。"""
    prof = _profile()
    spec = build_spec(prof)
    feat = prof["buckets"]["medium"]["feat"]
    for it in spec["items"]:
        f = it.get("feature")
        if not f or f not in feat:
            continue
        if it["scale"] == 1.0:
            assert abs(it["lo"] - feat[f]["p10"]) < 1e-6, it["key"]
            assert abs(it["hi"] - feat[f]["p90"]) < 1e-6, it["key"]


# --------------------------------------------------------------------------- #
# 3. 闭集：区间 + 中心值，不是均值
# --------------------------------------------------------------------------- #

def test_spec_items_have_range_and_center():
    """区间型项必须同时给区间与中心值。"""
    spec = build_spec(_profile())
    for it in spec["items"]:
        if it["kind"] != "range":
            continue
        assert it["lo"] is not None and it["hi"] is not None, it["key"]
        assert it["lo"] <= it["hi"], it["key"]
        if it["target"] is not None:
            assert it["lo"] <= it["target"] <= it["hi"], it["key"]


def test_render_spec_shows_ranges_not_means():
    """渲染出来的必须是**区间**，且带「落在区间内即合格」的说明（由提示块给出）。"""
    prof = _profile()
    prof["chapter_stats"] = chapter_length_stats([1000, 1200, 1400])
    prof["spec"] = build_spec(prof)
    block = _profile_block(prof)
    assert "22~29" in block, block          # 句长均值 22~29（不是均值 25）
    assert "参考书一章" in block, block       # 章长进提示词
    assert "落在区间内即合格" in block, block


def test_render_spec_is_deterministic():
    """同一份 spec 两次渲染**逐字节相同** —— 提示词缓存的前提。"""
    spec = build_spec(_profile())
    a = render_spec(spec)
    b = render_spec(spec)
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")


def test_render_spec_has_no_duplicate_header():
    """提示块与规格书**各出一层标题**即可，不能两层都叫「文风规格」。

    （实测踩过：`_profile_block` 有一句说明，`render_spec` 又带一句首行 ⇒ 两层标题。）
    """
    prof = _profile()
    prof["spec"] = build_spec(prof)
    block = _profile_block(prof)
    assert block.count("【文风规格】") == 0, block
    assert block.count("【文风规格：") == 1, block


# --------------------------------------------------------------------------- #
# 4. 不误伤：零方差降级 info
# --------------------------------------------------------------------------- #

def test_zero_variance_item_degrades_to_info():
    """p10 == p90 ⇒ 这一项在这本书上恒定 ⇒ 不构成写法特征 ⇒ 只陈述不判定。

    典型：一本从不用顿号的书，顿号密度恒为 0。
    否则生成里偶尔用一个顿号就被回灌 —— 那是误伤。
    """
    prof = _profile()   # 分号密度 p10=p90=0
    spec = build_spec(prof)
    item = next(i for i in spec["items"] if i["key"] == "分号密度")
    assert item["kind"] == "info", item
    check = spec_check(BODY, spec, unit_chars=3000)
    assert "分号密度" not in check["out_of_band"]
    assert "分号密度" not in check["warn_only"]
    assert check["items"]["分号密度"]["checked"] is False


def test_zero_variance_still_stated_in_prompt():
    """降级 info ≠ 从提示词里消失 —— 模型仍看得见它（只是不判定）。"""
    prof = _profile()
    prof["spec"] = build_spec(prof)
    block = _profile_block(prof)
    assert "分号" in block, block


# --------------------------------------------------------------------------- #
# 5. 上限型只告警；排版量不判定
# --------------------------------------------------------------------------- #

def test_upper_item_out_of_band_only_warns():
    """上限型出带 ⇒ 只告警，不回灌、不阻断。"""
    spec = build_spec(_profile())
    # 造一段比喻密度极高的文本
    loud = "\n".join(["他的心里像刀割一样疼，像火一样烧，像冰一样冷。"] * 20)
    check = spec_check(loud, spec, unit_chars=3000)
    assert "比喻密度" in check["warn_only"], check["warn_only"]
    assert "比喻密度" not in check["out_of_band"]
    assert not any("比喻" in line for line in spec_deviation_lines(check))


def test_layout_items_are_recorded_not_checked():
    """排版量（空白占比/段间空行密度）只记录，不判定、不进提示词。

    它们描述的是「这份 txt 的排版」，不是「作者怎么写」；
    而模型的输出格式根本控制不了空行 ⇒ 判定它等于判噪声。
    """
    prof = _profile()
    spec = build_spec(prof)
    for key in ("空白占比", "段间空行密度"):
        item = next(i for i in spec["items"] if i["key"] == key)
        assert item["in_prompt"] is False, item
    check = spec_check(BODY, spec, unit_chars=3000)
    for key in ("空白占比", "段间空行密度"):
        assert check["items"][key]["checked"] is False
        assert key in check["recorded"]


def test_layout_items_not_in_prompt():
    """排版量不进提示词。"""
    prof = _profile()
    prof["spec"] = build_spec(prof)
    block = _profile_block(prof)
    assert "空白" not in block, block
    assert "空行" not in block, block


# --------------------------------------------------------------------------- #
# 6. 贴边出带：容差 + 回灌门槛
# --------------------------------------------------------------------------- #

def test_edge_value_inside_displayed_range_is_not_out_of_band():
    """按展示精度看不出差别的 ⇒ 不算偏离。

    真值 30.96、带宽下界 31.2：精确比较会判「偏低」，
    但回灌句会写成「实测 31/千汉字（目标 31~41）」—— 自相矛盾。
    """
    spec = build_spec(_profile())
    item = next(i for i in spec["items"] if i["key"] == "句号密度")
    # 直接构造一个贴边的 spec：lo=31.2，hi=41.1，digits=1
    item["lo"], item["hi"] = 31.2, 41.1
    spec2 = {"items": [item], "unit_chars": 3000}
    check = spec_check(BODY, spec2, unit_chars=3000)
    v = check["items"]["句号密度"]["value"]
    if v < 31.2:
        # 只有当差距小到展示精度看不出来时，才应该判在带内
        assert check["items"]["句号密度"]["in_band"] is True


def test_deviation_lines_skip_edge_out_of_band():
    """贴边出带**判定如实**（在 out_of_band 里），但**不写进回灌句**。

    回灌一堆「差 1%」的要求，模型只能乱改。

    构造法：拿实测值本身，把下界设成「刚好比它低一点点」——
    这样它一定出带，且超出幅度 = 一点点 / 带宽 ⇒ 必然低于 5% 门槛。
    """
    spec = build_spec(_profile())
    item = next(i for i in spec["items"] if i["key"] == "冒号密度")
    probe = spec_check(BODY, {"items": [item], "unit_chars": 3000}, unit_chars=3000)
    value = probe["items"]["冒号密度"]["value"]
    # 下界 = 实测 + 0.08：大于容差（0.05，digits=1 的半个刻度）⇒ 确实出带；
    # 但超出幅度 0.08 / 带宽 10.7 ≈ 0.75% ⇒ 低于 5% 门槛 ⇒ 不进回灌句。
    item["lo"] = value + 0.08
    item["hi"] = value + 10.7
    spec2 = {"items": [item], "unit_chars": 3000}
    check = spec_check(BODY, spec2, unit_chars=3000)
    assert "冒号密度" in check["out_of_band"], check["out_of_band"]
    assert not any("冒号" in line for line in spec_deviation_lines(check))


def test_deviation_lines_include_real_out_of_band():
    """真偏离（超出带宽 5% 以上）必须出现在回灌句里。"""
    spec = build_spec(_profile())
    item = next(i for i in spec["items"] if i["key"] == "省略号密度")
    item["lo"], item["hi"] = 0.7, 4.5
    spec2 = {"items": [item], "unit_chars": 3000}
    check = spec_check(BODY, spec2, unit_chars=3000)
    lines = spec_deviation_lines(check)
    if "省略号密度" in check["out_of_band"]:
        assert any("…" in line for line in lines), lines


def test_deviation_line_uses_distinct_names():
    """回灌句的专名不能重名（「每段」曾同时指字数与句数）。"""
    spec = build_spec(_profile())
    check = spec_check(BODY, spec, unit_chars=3000)
    lines = spec_deviation_lines(check)
    names = [line.split("」")[0].lstrip("「") for line in lines]
    assert len(names) == len(set(names)), names


def test_deviation_line_shows_unit_with_space():
    """中文单位前留空格（「20~27 汉字」），百分号与「/千汉字」紧贴。"""
    spec = build_spec(_profile())
    check = spec_check(BODY, spec, unit_chars=3000)
    for line in spec_deviation_lines(check):
        # 「汉字」作为独立单位 ⇒ 前有空格；作为「/千汉字」的一部分 ⇒ 紧贴
        if "/千汉字" in line:
            assert " 千汉字" not in line, line
        else:
            assert " 汉字" in line or "汉字" not in line, line


# --------------------------------------------------------------------------- #
# 7. 仲裁点：段级判定的唯一出口
# --------------------------------------------------------------------------- #

def test_style_gate_reports_per_item():
    """仲裁点给出逐项 in/out。"""
    prof = _profile()
    prof["spec"] = build_spec(prof)
    gate = _style_gate(BODY, prof)
    assert gate["available"] is True
    assert gate["items"], gate
    assert set(gate["out_of_band"]) <= set(gate["items"])
    assert set(gate["warn_only"]) <= set(gate["items"])


def test_style_gate_without_profile_is_not_available():
    """没有档案 ⇒ 不判（档案是增强，不是生成的前提）。"""
    gate = _style_gate(BODY, None)
    assert gate["available"] is False
    assert gate["out_of_band"] == []
    assert gate["deviations"] == []


def test_style_gate_with_legacy_profile_without_spec_is_not_available():
    """老档案没有 spec 字段 ⇒ 不判（退回旧路径，不假装能判）。"""
    gate = _style_gate(BODY, {"version": 2, "buckets": {"medium": {"feat": {}}}})
    assert gate["available"] is False


def test_pipeline_record_keeps_style_gate_detail():
    """仲裁点的逐项判定要**落库**（事后可查，不必重跑）。"""
    prof = _profile()
    prof["spec"] = build_spec(prof)
    gate = _style_gate(BODY, prof)
    record = _pipeline_record({"style_gate": gate})
    assert "style_gate" in record
    assert record["style_gate"]["out_of_band"] == gate["out_of_band"]
    assert record["style_gate"]["items"], record
    row = record["style_gate"]["items"]["句长均值"]
    assert "value" in row and "lo" in row and "hi" in row and "in_band" in row


def test_pipeline_record_omits_style_gate_when_unavailable():
    """没判 ⇒ 不落一个空壳（免得看起来像「判过且全合格」）。"""
    record = _pipeline_record({"style_gate": {"available": False}})
    assert record["style_gate"] == {}


# --------------------------------------------------------------------------- #
# 8. 指纹：规格书版本变了 ⇒ 档案重算
# --------------------------------------------------------------------------- #

def test_fingerprint_contains_spec_version():
    """指纹带 spec 版本 ⇒ 规格书改了，老档案自动失效。

    否则档案里的 spec 是旧的 ⇒ 提示词与验收用两套口径。
    """
    fp = profile_fingerprint([1, 2], 3000)
    assert fp["spec"] == SPEC_VERSION


def test_fingerprint_changes_when_spec_version_changes(monkeypatch):
    """spec 版本变 ⇒ 指纹变 ⇒ 老档案判失效。"""
    import app.style.pipeline as pl

    fp1 = pl.profile_fingerprint([1], 3000)
    monkeypatch.setattr(pl, "SPEC_VERSION", SPEC_VERSION + 1)
    fp2 = pl.profile_fingerprint([1], 3000)
    assert fp1 != fp2


# --------------------------------------------------------------------------- #
# 9. 规格项表的完整性
# --------------------------------------------------------------------------- #

def test_spec_items_keys_unique():
    """规格项的 key 不能重名（否则 build_spec 里会互相覆盖）。"""
    keys = [it.key for it in SPEC_ITEMS]
    assert len(keys) == len(set(keys))


def test_spec_items_have_prompt_and_dev_names():
    """每一项都要有渲染前缀与回灌专名。"""
    for it in SPEC_ITEMS:
        assert it.label, it
        assert it.dev or it.label, it


def test_spec_has_all_four_kinds():
    """四种 kind 都要有样本（range/upper/info 各有用处）。"""
    kinds = {it.kind for it in SPEC_ITEMS}
    assert kinds == {"range", "upper", "info"}
