"""候选打分：给「同一提示采样 N 份」提供**一把尺子**。

## 这个模块负责什么 / 不负责什么

- **负责**：给一份文本打分 —— 守恒（字数不缩水/不膨胀）+ 形态（落在参考书
  自己的分布带里），并给出「离分布中心多远」这个可比较的分数。
- **不负责**：**选哪一份**。选择发生在**整条链跑完之后**
  （`engine._run_pool_selection` 的末端择优）——
  因为落库的不是「一次采样的原稿」，而是它被后面几道工序改过之后的东西。
  ⇒ 量与选**分开**：本模块是纯函数，好测；选择在引擎里，因为它要看成品。

## 为什么有这个东西

对白形态的差距是**模型先验**（提示层挪不动，见 AGENTS.md）。但模型输出**不是
确定值** —— 同一提示重复采样，散布很大（实测 Flash 的纯台词段在 12.4%~50.6% 之间，
极差 38 个百分点）。

既然散布这么大，就有了**选择空间**：采 N 份，按一把尺子选最好的那份。
这就是文献里的 **Best-of-N / 拒绝采样**
（[Best-of-N](https://api.emergentmind.com/topics/best-of-n-bon)、
[拒绝采样](https://yijia.ws/llm/2026-08-26-057)、
[两者对比](https://yijia.ws/interviews/2026-04-05-046)）。

## 为什么我们做这条特别顺

Best-of-N 的头号风险是 **reward hacking**：打分器有偏时，选择压力会放大它的
偏好，挑出「分高但人读着差」的稿子（[Best-of-N Sampling](https://d-central.tech/mining-glossary/best-of-n-sampling)）。

**我们的打分器不是学出来的偏好，是参考书自己拟合出来的分布** —— 它不可能为了
讨好指标而牺牲别的东西。所以这条路的风险面比文献里描述的小得多。

## 打分器的三条硬约束（都从文献的已知陷阱反推）

1. **打分必须含「守恒」判据**，不能只看形态。
   否则就是 reward hacking 的温和版：挑到「形态对但内容空」的稿子。
   所以每一份候选都要过**字数不缩水/不膨胀 + 对白不丢**。
   （例外：**末端**那次判定只判形态 —— 长度已由段长闸门管过，
   而文风改写会压缩 15%~25%，用守恒下限去裁决链后的文本会系统性误杀。
   见 `engine._judge_final_text`。）
2. **N 有界且默认小**：收益随 log(N) 增长、成本线性增长
   （[拒绝采样实践要点](https://yijia.ws/llm/2026-08-26-057)：N=16~32 性价比最高，
   但那是「生成很便宜」的假设；我们一次真跑 4~9 分钟，N 必须小）。
3. **必须有下限兜底**：N 份都不达标时，落库**最好的那份**并如实标注偏离 ——
   不能因为「没达标」就不落库（拒绝采样的原则：预算不确定，但保证最低质量）。

## 口径：只用一把尺 —— `stylometry.band_check`

「拿参考书自己的分布当尺子」在项目里**只有一个入口**：`style/stylometry.band_check()`
（诊断规则、改写闸门共用）。本模块必须用它，不能自己拿 `features()` 的值去比。

**为什么不能用 `form.measure_form()["unit_ratio"]`**（第一版就是这么写的，
被单元测试当场抓住）：那个值**带着长度归一化因子**
（`features()` 对非密度特征乘 `exp(-|log(len/unit)|*0.3)`）。一段 80 字的短文本
因子 ≈ 0.31 —— 于是「纯台词段占比 100%」被量成 **29%**，看起来像合格。
`band_check()` 把这个因子**除掉**，两边才是同一把尺。

参考书的带是在 **unit 长度**上拟的（因子 ≈ 1），而一段生成正文 ≈ 一个单元
⇒ 实测值与档案带可比。

"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

#: 候选池的默认上限。
#:
#: 为什么这么小：收益随 log(N) 增长而成本**线性**增长；我们一次真跑 4~9 分钟/段，
#: N=3 是「多花一倍时间换一个明显更稳的结果」的档位。
#: 文档里 N=16~32 性价比最高，那是在「生成几乎免费」的假设下（大批量短任务）。
DEFAULT_POOL_SIZE = 3

#: 候选池的绝对上限（防止有人把 N 调到几十）。
MAX_POOL_SIZE = 8

#: 守恒判据：字数允许**缩水的下限**（与 `generation/rewrite.py` 的 MIN_LEN_RATIO 同值）。
MIN_LEN_RATIO = 0.60

#: 守恒判据：字数允许**超出目标**的上限（绝对余量）。
#:
#: **为什么这个上限比 `rewrite.py` 宽松得多**（真跑 N=3 实测出来的）：
#: 候选之间是**独立采样**（同一提示重复生成），不是「从某个草稿改写出来」——
#: 模型这一次多写几百字是**正常的采样差异**，不是「凭空造内容」。
#: 而 `rewrite.py` 的膨胀上限（15%）是为**改写**设的：那里的输入是一份确定的
#: 草稿，输出多出 45% 必然是模型编的新内容（新对白、新情节），没有事实依据。
#:
#: 真跑证据（N=3，Flash，目标 2400 字）：候选 2357 / 2811 / 2873 字。
#: 用 rewrite 的 15% 上限（2760）会把 **2811 与 2873 那两份全判成「膨胀」** ——
#: 它们恰恰是**独立写出来的正常稿子**，不是谁改写出来的。
#: ⇒ 候选池只需要防「**短得离谱**」（内容没写够）与「**长得离谱**」（失控），
#: 上限放到目标 + 60%。
MAX_LEN_OVERFLOW_ABS = 45
MAX_LEN_OVERFLOW_RATIO = 0.60

#: 形态评分用的特征名（档案里的原名）。
FORM_FEATURE = "纯台词段占比"


@dataclass
class Candidate:
    """一份候选正文 + 它的评分。"""

    text: str
    #: 形态实测：`{"value": 实测值, "bucket": 桶名}`
    form: dict = field(default_factory=dict)
    #: 评分：越小越好
    score: float = 0.0
    #: 为什么不合格（合格时为空）
    reject: str = ""
    #: 档案给的形态目标（None = 没有档案，无法评分）
    target: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.reject


def _cjk_len(text: str) -> int:
    return sum(1 for ch in (text or "") if "一" <= ch <= "鿿")


def _form_measure(text: str, profile: dict | None) -> tuple[float | None, dict | None]:
    """量这段正文在档案分布里的位置。返回 (实测值, 档案带)。

    走 `stylometry.band_check()` —— 项目里「拿参考书自己的分布当尺子」的**唯一入口**
    （诊断规则与改写闸门共用它）。

    **为什么不能直接用 `form.measure_form()["unit_ratio"]`**（第一版就是这么写的，
    被单元测试当场抓住）：那个值**带着长度归一化因子**
    （`features()` 对非密度特征乘 `exp(-|log(len/unit)|*0.3)`）。一段 80 字的短文本
    因子 ≈ 0.31 —— 于是「纯台词段占比 100%」被量成 **29%**，看起来像合格。
    `band_check()` 把这个因子**除掉**，两边才是同一把尺。

    参考书的带是在 **unit 长度**上拟的（因子 ≈ 1），而一段生成正文 ≈ 一个单元
    ⇒ 实测值与档案带可比。
    """
    if not profile:
        return None, None
    from app.style.stylometry import band_check

    got = band_check(text, profile, [FORM_FEATURE]) or {}
    item = got.get(FORM_FEATURE)
    if not item:
        return None, None
    value = float(item.get("value") or 0.0)
    band = item.get("band") or {}
    target = {
        "target": float(band.get("mean") or 0.0),
        "p10": float(band.get("p10") or 0.0),
        "p90": float(band.get("p90") or 1.0),
        "bucket": item.get("bucket"),
    }
    return value, target


def score_candidate(
    text: str,
    *,
    profile: dict | None,
    baseline_chars: int | None = None,
) -> Candidate:
    """给一份候选打分（越小越好），并判它是否合格。

    `profile` = 项目已落库的文风档案（就是真正注入提示词的那一份）。
    没有档案 → **判不出**（不是判不合格）—— 档案是增强，不是生成的前提。

    `baseline_chars` = 字数基准（通常是**本次生成要求的目标字数**）。

    ## 为什么基准不能是「同伴中位数」（第一版踩过）

    第一版拿同批候选的字数中位数当基准，判据是「不能比同伴短太多」。
    结果**误杀了形态最差的那份**：一份「一行一句台词」的正文天然比
    「叙述+台词同段」的短一半（实测 18 字 vs 60 字），于是它先被字数判据毙掉，
    根本轮不到形态判据 —— 择优变成了「谁长谁赢」。

    候选之间字数本就会有差别（形态差的稿子往往更短），所以基准必须是
    **外部的、可验收的字数目标**（段长闸门用的那个目标），不是同伴。
    """
    cand = Candidate(text=text or "")
    if not (text or "").strip():
        cand.reject = "空文本"
        cand.score = float("inf")
        return cand

    # —— 守恒判据 1：相对**字数目标**不能缩水太多 ——
    if baseline_chars:
        ratio = _cjk_len(text) / max(1, baseline_chars)
        if ratio < MIN_LEN_RATIO:
            cand.reject = f"字数不足目标（{_cjk_len(text)}/{baseline_chars} = {ratio:.0%}）"
            cand.score = float("inf")
            return cand

    # —— 守恒判据 2：不能失控地长（独立采样多写一点正常，长太多不正常）——
    if baseline_chars:
        growth = max(MAX_LEN_OVERFLOW_ABS, int(baseline_chars * MAX_LEN_OVERFLOW_RATIO))
        if _cjk_len(text) > baseline_chars + growth:
            cand.reject = f"字数超出目标过多（{_cjk_len(text)} > {baseline_chars}+{growth}）"
            cand.score = float("inf")
            return cand

    # —— 形态分：与机械校正闸门同一把尺 ——
    try:
        value, target = _form_measure(text, profile)
        if value is None or target is None:
            # 没有档案（或档案里没这项特征）⇒ 判不出形态，但内容守恒已过 ⇒ 合格
            cand.score = 0.0
            return cand
        cand.target = target
        cand.form = {"value": value, "bucket": target["bucket"]}
        dist = abs(value - target["target"])
        if not (target["p10"] <= value <= target["p90"]):
            cand.reject = (
                f"形态不在参考书分布带内（{value:.0%}，带 "
                f"{target['p10']:.0%}~{target['p90']:.0%}）"
            )
        cand.score = dist
        return cand
    except Exception as exc:  # noqa: BLE001 —— 量不出来就不判不合格
        cand.reject = ""
        cand.score = 0.0
        cand.form = {"error": f"{type(exc).__name__}: {exc}"}
        return cand


def normalize_pool_size(value: int | None) -> int:
    """把配置的 N 收敛到 [1, MAX_POOL_SIZE]。1 = 关闭候选池。"""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_POOL_SIZE))
