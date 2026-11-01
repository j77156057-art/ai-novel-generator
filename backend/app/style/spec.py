"""文风规格书（StyleSpec）：把档案统计变成「模型照着写、机器能验」的闭集目标。

## 这份东西解决什么

今天模型拿到的文体基线是 `stylometry.describe()` 的输出 —— 里面有区间，
但**一半是均值**（「对白占正文 18%」「每段约 15 汉字」「每段句数 1.2 句」），
而且**没有章长**（作者一章写多长，从来没量过）。

均值当目标是**错的指令**：让模型每段都写 15 个汉字、每段 1.2 句，
得到的是一套机械产物 —— 这正是「照抄数字」的失败模式。

⇒ 规格书给的是**闭集**：
- **区间**（p10~p90）= 「落在这一带算合格」（与 `band_check` 判带内是同一对分位点）；
- **中心值**（p50）= 「方向」（该往哪靠），**不是**必须命中的点；
- **上限项**（比喻/情绪直陈/段尾拔高/纯台词段）= 「超过即偏 AI 腔」，只告警不阻断。

## 三条纪律（都是这轮踩出来的）

1. **提示词里说的区间 = 验收用的区间**。
   `render_spec()` 与 `spec_check()` 都从**同一份 spec** 出 ⇒ 不可能两套口径。
   （有测试守着：spec_check 与 band_check 在同一段文本上给同一组结论。）
2. **零方差的项不参与判定**。
   某项在这本书上恒定（p10 == p90，例如一本从不用顿号的书，顿号密度恒为 0），
   说明它**不构成本书的写法特征** ⇒ 降级为 `info`（只陈述、不判定）。
   否则生成里偶尔用一个顿号就被回灌 —— 那是误伤。
3. **排版量不判定**（空白占比、段间空行密度）。
   它们描述的是「这份 txt 的排版」，不是「作者怎么写」；
   而且模型的输出格式根本控制不了空行 ⇒ 判定它等于判噪声。
   仍然**记录**在报告里（可见），但不进判定、不回灌。

## 判定与回灌的分工（2026-11-01 拍板）

- **区间型出带** ⇒ 回灌（附偏差量），这是「模型能改」的那一类。
- **上限型出带** ⇒ **只告警**，不阻断、不回灌。
- **不进提示词的项** ⇒ 只记录，**不判定**（模型看不见它，回灌没有意义）。
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable, NamedTuple, Sequence

from app.style import stylometry as S

#: 规格书版本。口径（`stylometry.MEASUREMENT_VERSION`）或本文件的项目集变了就要 +1。
SPEC_VERSION = 1


class SpecItem(NamedTuple):
    """一项规格。"""

    key: str
    #: 档案里的特征名；None = 来自章长统计（不是 33 项特征之一）。
    feature: str | None
    group: str
    #: `range`（闭集区间，判定+回灌）/ `upper`（上限，只告警）/ `info`（只陈述）。
    kind: str
    #: 换算到展示单位（占比类 ×100）。
    scale: float
    digits: int
    #: 渲染用前缀（「一句」「每段」…）。
    label: str
    unit: str
    #: 是否进提示词。不进 ⇒ 只记录不判定（模型看不见它，回灌没有意义）。
    in_prompt: bool = True
    #: **回灌用**的专名。默认同 `label`；只有渲染前缀会重名时才另给。
    #:
    #: 为什么需要单独一个：渲染时「每段 20~27 汉字」靠单位就能分辨，
    #: 而回灌句是「「每段」偏高：实测 32.6 汉字」—— 两项都叫「每段」时
    #: 人和模型都得回头看单位才知道说的是哪一个 ⇒ 给出专名。
    dev: str = ""


#: 规格项表。**顺序即渲染顺序**（确定性 ⇒ 不击穿提示词缓存）。
SPEC_ITEMS: tuple[SpecItem, ...] = (
    # —— 章长：参考书自己的分节长度（**不是**模型这一次的产出目标）——
    SpecItem("章长", None, "章长", "range", 1.0, 0, "参考书一章", "汉字", True),
    # —— 长度 ——
    SpecItem("句长均值", "句长均值", "长度", "range", 1.0, 1, "一句", "字", True),
    SpecItem("段均汉字数", "段均汉字数", "长度", "range", 1.0, 0, "每个自然段", "汉字", True, "自然段字数"),
    SpecItem("段均句数", "段均句数", "长度", "range", 1.0, 1, "每个自然段的句数", "句", True, "自然段句数"),
    # —— 对白 ——
    SpecItem("对白字占比", "对白字占比", "对白", "range", 100.0, 0, "对白占正文", "%", True),
    SpecItem("对白段占比", "对白段占比", "对白", "range", 100.0, 0, "含对白的段落", "%", True),
    SpecItem("纯台词段占比", "纯台词段占比", "对白", "upper", 100.0, 0, "纯台词段", "%", True),
    SpecItem("对白段典型字数", "__dialogue_median__", "对白", "info", 1.0, 0, "对白段典型", "字", True),
    # —— 标点密度（每千汉字）——
    SpecItem("逗号密度", "逗号密度", "标点密度", "range", 1.0, 0, "逗号", "/千汉字", True),
    SpecItem("句号密度", "句号密度", "标点密度", "range", 1.0, 0, "句号", "/千汉字", True),
    SpecItem("问号密度", "问号密度", "标点密度", "range", 1.0, 1, "？", "/千汉字", True),
    SpecItem("感叹号密度", "感叹号密度", "标点密度", "range", 1.0, 1, "！", "/千汉字", True),
    SpecItem("省略号密度", "省略号密度", "标点密度", "range", 1.0, 1, "…", "/千汉字", True),
    # —— 标点用法 ——
    SpecItem("顿号密度", "顿号密度", "标点用法", "range", 1.0, 1, "顿号", "/千汉字", True),
    SpecItem("分号密度", "分号密度", "标点用法", "range", 1.0, 1, "分号", "/千汉字", True),
    SpecItem("冒号密度", "冒号密度", "标点用法", "range", 1.0, 1, "冒号", "/千汉字", True),
    SpecItem("破折号密度", "破折号密度", "标点用法", "range", 1.0, 1, "破折号", "/千汉字", True),
    SpecItem("逗号句内位置", "逗号句内位置", "标点用法", "range", 100.0, 0, "逗号落在句子的", "%", True),
    SpecItem("引号内句号占比", "引号内句号占比", "标点用法", "range", 100.0, 0, "对白句号收在引号内", "%", True),
    # —— 分段 ——
    SpecItem("段首对白率", "段首对白率", "分段", "range", 100.0, 0, "段首即对白", "%", True),
    SpecItem("对白段连长度", "对白段连长度", "分段", "range", 1.0, 1, "连续对白", "段", True),
    SpecItem("叙述段连长度", "叙述段连长度", "分段", "range", 1.0, 1, "连续叙述", "段", True),
    # —— 排版量：只记录，**不进提示词**（描述的是这份 txt 的排版，不是作者怎么写）——
    SpecItem("空白占比", "空白占比", "排版", "info", 100.0, 1, "空白", "%", False),
    SpecItem("段间空行密度", "段间空行密度", "排版", "info", 1.0, 2, "段间空行", "行/段", False),
    # —— 上限（超出即偏 AI 腔；只告警，不回灌）——
    SpecItem("比喻密度", "比喻密度", "上限", "upper", 1.0, 1, "比喻", "/千汉字", True),
    SpecItem("情绪直陈密度", "情绪直陈密度", "上限", "upper", 1.0, 1, "情绪直陈", "/千汉字", True),
    SpecItem("段尾升华率", "段尾升华率", "上限", "upper", 100.0, 0, "段尾升华", "%", True),
    # —— 分布形状：进提示词（模型要看得见「参差」），但**不判定** ——
    SpecItem("句长CV", "句长CV", "分布形状", "info", 1.0, 2, "句长参差度", "", True),
    SpecItem("段长CV", "段长CV", "分布形状", "info", 1.0, 2, "段长参差度", "", True),
    SpecItem("短句占比", "短句占比", "分布形状", "info", 100.0, 0, "≤4 字短句", "%", True),
    SpecItem("长句占比", "长句占比", "分布形状", "info", 100.0, 0, "≥40 字长句", "%", True),
    SpecItem("四字格密度", "四字格密度", "分布形状", "info", 1.0, 1, "四字格", "/千汉字", True),
    SpecItem("短串占比", "短串占比", "分布形状", "info", 100.0, 0, "≤3 字短串", "%", True),
    SpecItem("虚词密度", "虚词密度", "分布形状", "info", 1.0, 0, "虚词", "/千汉字", True),
)

#: 渲染分组顺序（提示词里一行一组）。
SPEC_GROUPS: tuple[str, ...] = (
    "章长", "长度", "对白", "标点密度", "标点用法", "分段", "上限", "分布形状",
)

_ITEMS_BY_KEY = {it.key: it for it in SPEC_ITEMS}


def _n(value: float, digits: int = 1) -> str:
    """紧凑数字（与 `stylometry._n` 同口径）。"""
    s = f"{value:.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _unit_sep(unit: str) -> str:
    """单位前要不要空格。

    百分号与「/千汉字」紧贴（「10~36%」「61~79/千汉字」）；
    中文单位（「汉字」「字」「句」）留一个空格（「每段 20~27 汉字」）。

    渲染与回灌**共用**这一处 —— 两边各写一份就会哪天改了一边忘了另一边，
    于是「提示词里写的区间」和「回灌句里报的区间」格式不一致。
    """
    if unit in ("%", "") or unit.startswith("/"):
        return ""
    return " "


def _profile_feat(profile: dict | None) -> dict:
    """取档案里主档（medium bucket）的特征表。"""
    if not isinstance(profile, dict):
        return {}
    feat = profile.get("feat") or {}
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
    return feat or {}


def _profile_dialogue_form(profile: dict | None) -> dict:
    """取档案里样本级对白形态汇总（`pooled_dialogue_form` 的结果）。

    v1 路径在 `profile["dialogue_form"]`；v2 路径在 `profile["buckets"]["medium"]["dialogue_form"]`
    —— 与 `stylometry.describe()` 的取法一致（同一把尺）。
    """
    if not isinstance(profile, dict):
        return {}
    dform = profile.get("dialogue_form") or {}
    if not dform and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        dform = medium.get("dialogue_form") or {}
    return dform or {}


def _base_unit(profile: dict | None) -> int:
    if not isinstance(profile, dict):
        return 0
    return int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )


def build_spec(
    profile: dict | None,
    *,
    chapter_lengths: Sequence[int] | None = None,
) -> dict:
    """从档案（+章长统计）导出一份规格书。

    ## 区间从哪来

    - 档案特征：p10 / p50 / p90（**同一对分位点**，与 `band_check` 判带内一致）；
    - 章长：章节汉字数的 p10 / p50 / p90。

    ## 章长从哪取（两条路，优先级从上到下）

    1. `profile["chapter_stats"]` —— 档案里已经存好的**统计摘要**
       （`chapter_stats.chapter_length_stats()` 的产物）。这是**主路径**：
       档案是按指纹缓存的，章长跟着档案走 ⇒ 整轮生成恒定 ⇒ 不击穿提示词缓存。
    2. `chapter_lengths` 参数 —— 直接给每章的汉字数（现算）。
       只在「档案还没建好、单独要一份规格书」时用。

    ## 降级规则（模块 docstring 第 2 条纪律）

    - `p10 == p90`（零方差）⇒ `kind="info"`：这一项在这本书上没有变化，
      不构成「写法特征」，判定它只会误伤。
    - 缺 p10/p90（样本不足）⇒ 该项**不出现在规格书里**（不是填 0）。
    """
    feat = _profile_feat(profile)
    unit = _base_unit(profile)
    items: list[dict] = []

    chap_stats = _chapter_stats(profile, chapter_lengths)
    dform = _profile_dialogue_form(profile)
    for it in SPEC_ITEMS:
        if it.feature is None:  # 章长：来自章节统计
            if not chap_stats:
                continue
            lo, mid, hi = chap_stats["p10"], chap_stats["p50"], chap_stats["p90"]
            src = "chapter_stats"
        elif it.feature == "__dialogue_median__":
            # 对白段典型字数：档案的 dialogue_form（只有中位数，没有分布 ⇒ 只陈述）
            val = dform.get("seg_len_median")
            if val is None:
                continue
            lo = mid = hi = float(val)
            src = "dialogue_form"
        else:
            st = feat.get(it.feature) or {}
            lo, mid, hi = st.get("p10"), st.get("p50"), st.get("p90")
            src = it.feature
            if lo is None or hi is None:
                continue  # 样本不足 ⇒ 不给这一项（不填 0）
        lo_f, hi_f = float(lo) * it.scale, float(hi) * it.scale
        mid_f = float(mid) * it.scale if mid is not None else None
        kind = it.kind
        if kind == "range" and abs(hi_f - lo_f) < 1e-9:
            # 零方差 ⇒ 这本书里它恒定 ⇒ 不构成写法特征 ⇒ 只陈述
            kind = "info"
        items.append({
            "key": it.key,
            "feature": it.feature,
            "source": src,
            "group": it.group,
            "kind": kind,
            "lo": lo_f,
            "hi": hi_f,
            "target": mid_f,
            # 换算倍率也落进来：规格书要能**独立复算**（不回头找档案）。
            # 缺了它，外部拿到 spec 只能读到已换算的区间，无法还原原始特征值。
            "scale": it.scale,
            "unit": it.unit,
            "label": it.label,
            "dev": it.dev or it.label,
            "digits": it.digits,
            "in_prompt": bool(it.in_prompt),
        })

    return {
        "spec_version": SPEC_VERSION,
        "measurement": S.MEASUREMENT_VERSION,
        "unit_chars": unit,
        "n_chapters": chap_stats.get("n_chapters", 0),
        "items": items,
    }


def _chapter_stats(profile: dict | None, lengths: Sequence[int] | None) -> dict:
    """章长统计摘要（p10/p50/p90/章数…）。

    优先取档案里已经存好的 `chapter_stats`（**随档案恒定**），
    否则用传入的每章字数现算。都没有 ⇒ 空 dict（规格书里少「章长」这一项）。
    """
    stored = (profile or {}).get("chapter_stats") or {}
    if stored.get("p10") is not None:
        return stored
    if lengths:
        from app.style.chapter_stats import chapter_length_stats

        return chapter_length_stats(lengths)
    return {}


def spec_item(spec: dict, key: str) -> dict | None:
    """按 key 取一项规格。"""
    for it in (spec or {}).get("items") or []:
        if it.get("key") == key:
            return it
    return None


def render_spec(spec: dict | None) -> str:
    """把规格书渲染成**确定性**的一段文字（进 system 块）。

    确定性 = 同一份规格书永远产出同一段文本 ⇒ 不破坏提示词缓存
    （`backend/verify_stylometry.py` D 段有同样的闸门，规格书也要有）。

    ## 为什么不自带首行说明

    说明句（「落在区间内即合格，不必照抄中心值」）由**调用方**（提示块）统一给出
    —— 两边各写一遍就会出现两层标题（实测过一次），而且措辞会漂。
    这里只渲染**规格本身**，一行一组。
    """
    items = (spec or {}).get("items") or []
    if not items:
        return ""
    lines: list[str] = []
    for group in SPEC_GROUPS:
        cells: list[str] = []
        for it in items:
            if it.get("group") != group:
                continue
            if not it.get("in_prompt"):
                continue
            cells.append(_render_item(it))
        if cells:
            lines.append(f"· {group}：" + " · ".join(cells))
    return "\n".join(lines)


def _render_item(it: dict) -> str:
    digits = int(it.get("digits") or 0)
    lo = float(it.get("lo") or 0.0)
    hi = float(it.get("hi") or 0.0)
    target = it.get("target")
    unit = it.get("unit") or ""
    label = it.get("label") or ""
    sep = _unit_sep(unit)
    if it.get("kind") == "upper":
        return f"{label} ≤{_n(hi, digits)}{sep}{unit}"
    if abs(hi - lo) < 1e-9:
        return f"{label} {_n(lo, digits)}{sep}{unit}"
    out = f"{label} {_n(lo, digits)}~{_n(hi, digits)}{sep}{unit}"
    if target is not None:
        out += f"（中心 {_n(float(target), digits)}）"
    return out


def spec_check(
    text: str,
    spec: dict | None,
    *,
    unit_chars: int | None = None,
) -> dict:
    """量一段文本相对规格书的落位（**逐项**）。

    返回::

        {
          "items": {key: {value, lo, hi, target, kind, in_band, deviation, checked}},
          "out_of_band": [key, ...],     # 只含**可回灌**的（区间型且在提示词里）
          "warn_only": [key, ...],       # 上限型出带：只告警
          "recorded": [key, ...],        # 只记录不判定的
        }

    ## 实测值怎么算（与 `band_check` 同一把尺）

    密度类特征**不缩放**；非密度类要**除掉**长度归一化因子
    （档案是按 unit 粒度拟合的，不除就会把「本来句长就够」的长文本判成偏短）。
    ⇒ 与 `stylometry.band_check` 的处理完全一致，有测试对着对账。

    ## deviation 是什么

    相对「带中点」的归一化偏移：`-1` = 正好在下界，`+1` = 正好在上界，`0` = 正中。
    这样「偏了多少」能跨指标比较，也能直接回灌给模型。

    ## 容差（`_tolerance`）：为什么边界不能是精确比较

    规格项的区间与展示值都带**四舍五入**（例：句号密度带 31.2~41.1，展示成
    「31~41/千汉字」）。若判定用精确比较，一个真值 30.96 的实测会因
    `30.96 < 31.2` 被判「偏低」，回灌句却写成「实测 31/千汉字（目标 31~41）」——
    **实测明明落在展示出来的区间里，却被说偏低**。这种自相矛盾的回灌会让
    模型照着一个不存在的问题去改。

    ⇒ 容差取「展示精度的半个刻度」：`10^-digits / 2`，
    即「按展示精度看不出差别 ⇒ 不算偏离」。
    """
    items = (spec or {}).get("items") or []
    if not items:
        return {"items": {}, "out_of_band": [], "warn_only": [], "recorded": []}

    unit = int(unit_chars or (spec or {}).get("unit_chars") or 0)
    vec = S.features(text or "", unit_chars=unit or 3000)
    scale = S._length_scale_factor(S.cjk_len(text or ""), unit)
    # 对白段典型字数不是 33 项特征之一 —— 用**同一个** `pooled_dialogue_form` 量
    # （档案那侧也是它算出来的，取中位数不是均值）⇒ 两边同一把尺。
    dform_measured = S.pooled_dialogue_form([text or ""])

    out: dict[str, dict] = {}
    out_of_band: list[str] = []
    warn_only: list[str] = []
    recorded: list[str] = []

    for it in items:
        key = it["key"]
        feature = it.get("feature")
        if feature is None:
            # 章长：拿不到本章的章（生成段不是章）⇒ 只记录，不判定
            recorded.append(key)
            continue
        raw = float(vec.get(feature) or 0.0)
        if feature == "__dialogue_median__":
            value = float(dform_measured.get("seg_len_median") or 0.0)
        else:
            value = raw / scale if (feature in S.NON_DENSITY_FEATURES and scale) else raw
        lo, hi = it.get("lo"), it.get("hi")
        kind = it.get("kind")
        checked = bool(it.get("in_prompt")) and kind in ("range", "upper")
        in_band: bool | None = None
        if checked and lo is not None and hi is not None:
            tol = _tolerance(it)
            if kind == "upper":
                in_band = value <= float(hi) + tol
            else:
                in_band = float(lo) - tol <= value <= float(hi) + tol
        deviation = _deviation(value, lo, hi)
        out[key] = {
            "value": value,
            "lo": lo,
            "hi": hi,
            "target": it.get("target"),
            "kind": kind,
            "unit": it.get("unit"),
            "label": it.get("label"),
            "dev": it.get("dev") or it.get("label") or key,
            "digits": int(it.get("digits") or 0),
            "checked": checked,
            "in_band": in_band,
            "deviation": deviation,
        }
        if not checked:
            recorded.append(key)
        elif in_band is False:
            if kind == "upper":
                warn_only.append(key)
            else:
                out_of_band.append(key)

    return {
        "items": out,
        "out_of_band": out_of_band,
        "warn_only": warn_only,
        "recorded": recorded,
    }


#: **回灌门槛**：超出带宽的比例低于它 ⇒ 只报告，不回灌。
#:
#: 为什么需要这道门槛（2026-11-01 实测）：规格书的区间是 p10~p90，
#: 而实测值是**连续量** ⇒ 落点贴着边界是常态（实测：冒号密度真值 6.04，
#: 区间下界 6.1 —— 只差 1%，却是货真价实的「出带」）。
#:
#: 若把所有出带项都回灌，模型会收到一堆「差 1%」的要求 ⇒ 它只能乱改，
#: 反而把写好的句子改坏。而「差 1%」恰恰是**最不该改**的。
#:
#: ⇒ 判定仍如实（出带就是出带，报告里看得到），但**回灌**只给
#: 「超出带宽 5% 以上」的项 —— 那是真的值得改的幅度。
#:
#: 5% 的来历：冒号 6.04 vs 6.1（1%）不归；省略号 6.2 vs 4.5（超出带宽 45%）归。
#: 5% 落在两者之间，且远小于任何「肉眼可辨的写法差异」所需的幅度。
REWRITE_MIN_BAND_EXCEED = 0.05


def _band_exceed_ratio(value: float, lo: Any, hi: Any) -> float:
    """实测值超出区间的幅度（占带宽的比例）。

    - 区间内 ⇒ 0；
    - 贴边（差一点点）⇒ 很小（0.0~0.1）；
    - 远离 ⇒ 接近 1（甚至 >1）。

    带宽为 0（零方差项）时退化为「相对偏差」，避免除零。
    """
    if lo is None or hi is None:
        return 0.0
    lo_f, hi_f = float(lo), float(hi)
    if value < lo_f:
        excess = lo_f - value
    elif value > hi_f:
        excess = value - hi_f
    else:
        return 0.0
    width = hi_f - lo_f
    if width <= 1e-9:
        # 零方差：退化成一个「相对偏差」的量级（用 1 当参考尺度）
        return min(1.0, excess / max(abs(hi_f), 1.0))
    return excess / width


def _tolerance(it: dict) -> float:
    """判定容差 = 「展示精度的半个刻度」。

    为什么需要：区间与展示值都四舍五入（带 31.2~41.1 展示成「31~41/千汉字」）。
    精确比较时，一个真值 30.96 的实测会因 `30.96 < 31.2` 判「偏低」，
    回灌句却写成「实测 31/千汉字（目标 31~41）」—— **实测明明落在展示出来的
    区间里**。这种自相矛盾的回灌会让模型去修一个不存在的问题。

    ⇒ 判据：**按展示精度看不出差别 ⇒ 不算偏离**。
    """
    digits = int(it.get("digits") or 0)
    return 0.5 * (10.0 ** (-digits))


def _deviation(value: float, lo: Any, hi: Any) -> float | None:
    """实测值相对「带中点」的归一化偏移（-1 = 下界，+1 = 上界）。"""
    if lo is None or hi is None:
        return None
    lo_f, hi_f = float(lo), float(hi)
    mid = (lo_f + hi_f) / 2
    half = (hi_f - lo_f) / 2
    if half <= 1e-9:
        return 0.0 if abs(value - mid) <= 1e-9 else (1.0 if value > mid else -1.0)
    return (value - mid) / half


def spec_deviation_lines(check: dict, *, limit: int = 6) -> list[str]:
    """把出带项写成**可回灌**的句子（给改写流程用）。

    只给**区间型**出带项（上限型按拍板只告警，不回灌）。

    ## 为什么有门槛（`REWRITE_MIN_BAND_EXCEED`）

    贴边的出带（差 1%）不写进来 —— 回灌一堆「差 1%」的要求，
    模型只能乱改。**判定仍如实**（`out_of_band` 里全都在），
    这里只是**挑出值得改的那些**。

    ## 措辞为什么这样写

    - 专名用 `dev`（如「每段字数」），不是渲染前缀 —— 渲染时「每段 20~27 汉字」
      靠单位能分辨，回灌句脱离上下文后不行；
    - 数字用规格自带的 `digits`（与提示词里那一项**同精度**），
      不在这里重新猜 —— 猜出来的精度会与提示词对不上。
    """
    items = (check or {}).get("items") or {}
    lines: list[str] = []
    for key in (check or {}).get("out_of_band") or []:
        it = items.get(key) or {}
        dev = it.get("dev") or it.get("label") or key
        dev_val = it.get("deviation")
        unit = it.get("unit") or ""
        digits = int(it.get("digits") or 0)
        lo, hi = it.get("lo"), it.get("hi")
        value = float(it.get("value") or 0.0)
        # 贴边的出带不写进回灌句（见 REWRITE_MIN_BAND_EXCEED）
        if _band_exceed_ratio(value, lo, hi) < REWRITE_MIN_BAND_EXCEED:
            continue
        where = ""
        if lo is not None and hi is not None:
            where = (f"（目标 {_n(float(lo), digits)}~{_n(float(hi), digits)}"
                     f"{_unit_sep(unit)}{unit}）")
        direction = "偏低" if (dev_val is not None and dev_val < 0) else "偏高"
        lines.append(
            f"「{dev}」{direction}：实测 {_n(value, digits)}"
            f"{_unit_sep(unit)}{unit}{where}"
        )
        if len(lines) >= limit:
            break
    return lines
