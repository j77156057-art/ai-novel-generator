"""文体档案（stylometry）：把「风格」从形容词变成可测的数字。

## 为什么需要它

生成侧此前只有 `project.style = "起点"` 这种**平台标签**塞进系统提示。对模型来说
「起点」几乎不含信息——它既不是句长、也不是对白比例。于是「风格像不像参考语料」
这件事既无法量化、也无法验收，只能靠人肉读。本模块把文体拆成一批**纯统计特征**，
在参考语料上拟合出档案（profile），再让每个生成段算一个到档案的**距离**。

## 三条设计纪律

1. **零 LLM、纯离线**：全部是可机检的计数与比值，没有一处调用模型。
   于是档案可复现、可回归，验收不依赖「谁读着像」。
2. **阈值必须被标定，不能拍脑袋**。做法是把语料切成**与生成段同粒度**的单元，
   一部分当训练集拟合各特征的均值/标准差，另一部分当**留出集**算出「人写的单元
   离档案有多远」的经验分布。判定生成段时，报的是它在这个分布里的**分位**，
   而不是「超过某个我随手定的阈值」。
3. **特征必须有判别力才配留下**。一个特征若连「斗破苍穹 vs 红楼梦」都分不开，
   就不可能对「AI 腔 vs 人写」有判别力。`verify_stylometry.py` 用
   「同语料留出集距离 ≪ 跨语料距离」这条可证伪的命题把无用特征筛掉。

## 粒度为什么必须对齐

标点密度、对白占比这类是**比率**（尺度无关），但它们的**估计方差**随单元长度衰减。
拿 380 万字的整本小说估出的方差，去判一个 3,000 字的生成段，会把正常波动误判成漂移。
所以 `fit_profile(unit_chars=...)` 默认取生成长度，档案里也如实记下 `unit_chars`，
`style_distance` 会检查调用方给的文本长度是否落在可比区间内并在结果里标注。

## 与「风格平台」的关系

平台标签继续留在系统提示里（它毕竟承载了用户的显式选择），本模块产出的是
**静态文体基线块**：一组具体数字。两者不冲突——前者是意图，后者是可验收的目标。
静态块的文本由 `describe()` 生成且**不随段号变化**，因此不破坏系统提示的缓存友好性。
"""
from __future__ import annotations

import math
import re
import statistics
from typing import Iterable, Sequence

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PROFILE_VERSION = 2

#: 句子终结符（含中文全角）。省略号按整串处理，避免「……」被算成 2 个句子。
SENT_END = "。！？!?…"
#: 会在重切时被吃掉的成对符号
_ELLIPSIS_RUN = re.compile(r"[…]{2,}|\.{3,}")
#: 连续汉字串。用一次 findall 而不是逐字符 match —— 百万字语料上后者慢两个数量级。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

#: 引号内的对白（中文网文以 “” 为主，兼容「」『』）
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"))

_METAPHOR = re.compile(r"(像|如同|仿佛|彷佛|宛如|好似|犹如)")
#: 情绪直陈（贴标签式情绪描写，AI 腔典型）
_EMOTION = re.compile(r"(眼神里|瞳孔|眸中|闪过一丝|涌上心头|皆是|满是|无比|不禁|忍不住)")
#: 段尾拔高/升华（AI 腔典型结尾）
_ELEVATE = re.compile(r"(不只是|不仅|更是|原来|终于明白|从此|意味着|他不知道|很多年后|这一切)")

#: 需要计频的标点
_PUNCT = {
    "comma": "，",
    "period": "。",
    "excl": "！",
    "quest": "？",
    "ellipsis": "…",
    "pause": "、",
    "colon": "：",
    "semicolon": "；",
}

#: 中文作者归属研究里最经典的指纹 = 高频虚词频率。这里只取最稳的几个，
#: 不铺开成十几个（会稀释其余特征）。「的」与「了」是最强的两个。
_FUNC_WORDS = "的了着过是在不就都也又很这那"

#: 特征顺序即 `FEATURES`；保持固定顺序，档案与 z 向量才对得上。
FEATURES: tuple[str, ...] = (
    "句长均值",
    "句长CV",
    "句长p90",
    "短句占比",
    "长句占比",
    "段长CV",
    "段均句数",
    "对白字占比",
    "对白段占比",
    "逗号密度",
    "句号密度",
    "感叹号密度",
    "问号密度",
    "省略号密度",
    "比喻密度",
    "情绪直陈密度",
    "段尾升华率",
    "四字格密度",
    "短串占比",
    "虚词密度",
)

#: 特征的通俗解释（面板/报告里用，避免读者对着「CV」发愣）
FEATURE_NOTES: dict[str, str] = {
    "句长均值": "一句话平均多少字",
    "句长CV": "句长参差程度（越小越整齐，AI 腔偏小）",
    "句长p90": "长句那一端有多长（分布形状，不只是均值）",
    "短句占比": "≤4 字的句子比例（节奏顿挫感）",
    "长句占比": "≥40 字的句子比例（绵长句偏好）",
    "段长CV": "段落长短参差程度（越小越像 AI 写等长段）",
    "段均句数": "一个自然段平均几句话",
    "对白字占比": "引号内的字数占正文比例",
    "对白段占比": "含对白的段落比例",
    "逗号密度": "每千字逗号数",
    "句号密度": "每千字句号数",
    "感叹号密度": "每千字感叹号数",
    "问号密度": "每千字问号数",
    "省略号密度": "每千字省略号数",
    "比喻密度": "每千字「像/如同/仿佛…」（AI 腔偏高）",
    "情绪直陈密度": "每千字贴标签式情绪词（AI 腔偏高）",
    "段尾升华率": "以总结/拔高收尾的段落比例（AI 腔偏高）",
    "四字格密度": "每千字被标点界定的四字串（成语代理指标）",
    "短串占比": "被标点界定的 ≤3 字短串比例（用词粒度代理指标）",
    "虚词密度": "每千字高频虚词（的/了/着/过/是/在…）——中文作者归属的经典指纹",
}

#: 代理指标——不是直接测量，只是结构性近似，判读时必须知道这一点
PROXY_FEATURES = frozenset({"四字格密度", "短串占比"})


# --------------------------------------------------------------------------- #
# 切分与计数
# --------------------------------------------------------------------------- #

def paragraphs(text: str) -> list[str]:
    """按换行切自然段，丢弃空白段。"""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def sentences(para: str) -> list[str]:
    """把一段切成句子。终结符保留在句尾（它是文体信号本身，不能丢）。

    省略号先整体归一成一个终结符，否则「……」会切出 2~6 个空句。
    """
    if not para:
        return []
    s = _ELLIPSIS_RUN.sub("…", para)
    out: list[str] = []
    buf: list[str] = []
    for ch in s:
        buf.append(ch)
        if ch in SENT_END:
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def dialogue_char_count(text: str) -> int:
    """引号内的字符总数（对白篇幅）。跨段引号按段内配对处理。"""
    n = 0
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            a = text.find(open_q, i)
            if a < 0:
                break
            b = text.find(close_q, a + 1)
            if b < 0:
                break
            n += b - a - 1
            i = b + 1
    return n


def _runs(text: str) -> list[int]:
    """被非汉字字符界定的连续汉字串，返回各自长度。

    这是「四字格 / 短串」两个代理指标的底座：不用成语词典，只看**结构性**的
    汉字串长度分布。好处是零依赖、可复现；代价是它无法区分成语与恰好四字的
    普通词（如「我不知道你」），所以本模块**只把它当作分布信号**，不做单点判读。
    """
    return [len(m) for m in _CJK_RUN.findall(text)]


def _cv(values: Sequence[float]) -> float:
    """变异系数 = 标准差 / 均值。均值为 0 时返回 0（而不是 inf）。"""
    vals = [v for v in values]
    if len(vals) < 2:
        return 0.0
    m = statistics.fmean(vals)
    if m <= 0:
        return 0.0
    return statistics.pstdev(vals) / m


# --------------------------------------------------------------------------- #
# 特征向量
# --------------------------------------------------------------------------- #

def features(text: str) -> dict[str, float]:
    """一段文本的文体特征向量。全部是可机检的计数与比值。"""
    text = text or ""
    n = len(text)
    paras = paragraphs(text)
    if n == 0 or not paras:
        return {k: 0.0 for k in FEATURES}

    per1k = 1000.0 / n

    sents: list[str] = []
    for p in paras:
        sents.extend(sentences(p))
    sent_lens = [len(s) for s in sents] or [0]

    para_lens = [len(p) for p in paras]
    sents_per_para = [len(sentences(p)) for p in paras]

    dlg_chars = dialogue_char_count(text)
    dlg_paras = sum(1 for p in paras if any(q in p for q, _ in _QUOTE_PAIRS))

    elevate = 0
    for p in paras:
        tail = p[-45:]
        if _ELEVATE.search(tail):
            elevate += 1

    runs = _runs(text)
    n_runs = max(len(runs), 1)

    short_sent = sum(1 for L in sent_lens if L <= 4)
    long_sent = sum(1 for L in sent_lens if L >= 40)

    vec = {
        "句长均值": statistics.fmean(sent_lens),
        "句长CV": _cv(sent_lens),
        "句长p90": _quantile(sorted(sent_lens), 0.90),
        "短句占比": short_sent / len(sent_lens),
        "长句占比": long_sent / len(sent_lens),
        "段长CV": _cv(para_lens),
        "段均句数": statistics.fmean(sents_per_para),
        "对白字占比": dlg_chars / n,
        "对白段占比": dlg_paras / len(paras),
        "逗号密度": text.count(_PUNCT["comma"]) * per1k,
        "句号密度": text.count(_PUNCT["period"]) * per1k,
        "感叹号密度": text.count(_PUNCT["excl"]) * per1k,
        "问号密度": text.count(_PUNCT["quest"]) * per1k,
        "省略号密度": text.count(_PUNCT["ellipsis"]) * per1k,
        "比喻密度": len(_METAPHOR.findall(text)) * per1k,
        "情绪直陈密度": len(_EMOTION.findall(text)) * per1k,
        "段尾升华率": elevate / len(paras),
        "四字格密度": sum(1 for r in runs if r == 4) * per1k,
        "短串占比": sum(1 for r in runs if r <= 3) / n_runs,
        "虚词密度": sum(text.count(w) for w in _FUNC_WORDS) * per1k,
    }
    return {k: float(vec[k]) for k in FEATURES}


# --------------------------------------------------------------------------- #
# 档案拟合（train / holdout）
# --------------------------------------------------------------------------- #

def cut_units(text: str, unit_chars: int, *, max_units: int | None = None) -> list[str]:
    """把长文切成 `unit_chars` 上下的单元，**尽量落在段落边界**。

    落在段落边界是必须的：硬切会把一句话拦腰截断，句长与段长统计随即失真。
    """
    paras = paragraphs(text)
    units: list[str] = []
    buf: list[str] = []
    size = 0
    truncated = False
    for p in paras:
        if size and size + len(p) + 1 > unit_chars:
            units.append("\n".join(buf))
            buf, size = [], 0
            if max_units and len(units) >= max_units:
                truncated = True
                break
        buf.append(p)
        size += len(p) + 1
    if buf and not truncated:
        units.append("\n".join(buf))
    # 过短的尾巴（< 半个单元）会让 CV、对白占比这类统计严重偏斜，单独剔掉。
    # 这一步必须走完——提前 return 会让截断路径漏掉过滤，静默混进脏样本。
    floor = max(1, unit_chars // 2)
    return [u for u in units if len(u) >= floor]


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarize(values: list[float]) -> dict[str, float]:
    vals = sorted(values)
    m = statistics.fmean(vals) if vals else 0.0
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return {
        "mean": float(m),
        "std": float(sd),
        "p10": float(_quantile(vals, 0.10)),
        "p50": float(_quantile(vals, 0.50)),
        "p90": float(_quantile(vals, 0.90)),
        "min": float(vals[0]) if vals else 0.0,
        "max": float(vals[-1]) if vals else 0.0,
    }


def fit_profile(
    texts: Iterable[str],
    *,
    unit_chars: int = 3000,
    train_ratio: float = 0.7,
    max_units_per_text: int | None = 400,
    label: str = "",
) -> dict:
    """在参考语料上拟合文体档案。

    返回结构（可直接 JSON 落库）::

        {
          "version": 1,
          "unit_chars": 3000,
          "n_train": 62, "n_holdout": 27,
          "feat": { 特征名: {mean,std,p10,p50,p90,min,max}, ... },
          "holdout": {
             "mean_abs_z": {mean,std,p50,p90,max},   # 留出集到档案的距离分布
             "p90_abs_z":   {...},
             "unit_mean_abs_z": [ ... 每个留出单元的值 ... ],
          },
        }

    `holdout` 是最关键的一块：它给出「**人写的**单元离档案有多远」的经验分布。
    没有它，判定阈值只能拍脑袋；有了它，生成段的距离可以报成**分位**。
    """
    units: list[tuple[str, str]] = []  # (来源标签, 文本)
    for i, t in enumerate(texts):
        for u in cut_units(t, unit_chars, max_units=max_units_per_text):
            units.append((f"src{i}", u))

    if len(units) < 4:
        raise ValueError(
            f"语料太少无法拟合（切出 {len(units)} 个单元，至少需要 4 个）——"
            f"unit_chars={unit_chars} 是不是比语料还长？"
        )

    # 按**单元序号**取模切分，而不是按文本切分：一本小说只有一两个单元时，
    # 按文本切会让 holdout 为空或只剩某本书，训练/留出就不可比了。
    train = [u for i, (_, u) in enumerate(units) if i % 10 < int(train_ratio * 10)]
    holdout = [u for i, (_, u) in enumerate(units) if i % 10 >= int(train_ratio * 10)]
    if not train:
        train, holdout = units, []
    if not holdout and len(train) > 2:
        train, holdout = train[:-1], train[-1:]

    train_vecs = [features(u) for u in train]
    feat: dict[str, dict[str, float]] = {}
    for k in FEATURES:
        feat[k] = _summarize([v[k] for v in train_vecs])

    profile: dict = {
        "version": PROFILE_VERSION,
        "label": label,
        "unit_chars": int(unit_chars),
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_units": len(units),
        "feat": feat,
        "holdout": {},
    }

    if holdout:
        d = [metric_mean_abs_z(features(u), profile) for u in holdout]
        profile["holdout"] = {
            "mean_abs_z": _summarize(d),
            "unit_mean_abs_z": [round(x, 4) for x in d],
        }
        # 每个特征各自的留出波动 —— 用来判断「某特征这个 z 值算不算大」
        feat_holdout: dict[str, dict[str, float]] = {}
        hvecs = [features(u) for u in holdout]
        for k in FEATURES:
            zs = [
                abs((v[k] - feat[k]["mean"]) / feat[k]["std"]) if feat[k]["std"] > 0 else 0.0
                for v in hvecs
            ]
            feat_holdout[k] = {
                "mean": float(statistics.fmean(zs)) if zs else 0.0,
                "p90": float(_quantile(sorted(zs), 0.90)),
                "max": float(max(zs)) if zs else 0.0,
            }
        profile["holdout"]["feat_abs_z"] = feat_holdout
    return profile


# --------------------------------------------------------------------------- #
# 距离度量
# --------------------------------------------------------------------------- #

def z_scores(vec: dict[str, float], profile: dict) -> dict[str, float]:
    """逐特征 z 值。std 为 0 时给 0（该特征在语料里恒定，不构成偏离依据）。"""
    out: dict[str, float] = {}
    feat = profile.get("feat") or {}
    for k in FEATURES:
        st = feat.get(k) or {}
        sd = float(st.get("std") or 0.0)
        mean = float(st.get("mean") or 0.0)
        out[k] = (float(vec.get(k, 0.0)) - mean) / sd if sd > 0 else 0.0
    return out


def metric_mean_abs_z(vec: dict[str, float], profile: dict) -> float:
    """距离 = 各特征 |z| 的均值。

    取均值而不是平方和（欧氏）：欧氏会被单个离群特征主导（标点密度偶尔飙一次
    就把总分拉爆），而我们关心的是「整体像不像」，不是「有没有一项特别离谱」。
    单项离谱由 `worst` 单独报出来，不混进总分。
    """
    z = z_scores(vec, profile)
    return statistics.fmean([abs(v) for v in z.values()]) if z else 0.0


def style_distance(text: str, profile: dict) -> dict:
    """一段文本相对档案的距离，并给出**对照留出集的分位**。

    `percentile` 的含义：在参考语料**人写的**留出单元里，有多大比例的距离
    ≤ 本段。distance 越大 percentile 越大：100% = 比所有人类样本都更偏离；
    0% = 比所有人类样本都更接近。**这个分位才是可判读的结论**，
    `mean_abs_z` 本身没有绝对含义。

    PROFILE_VERSION=2 时（方案 B「对数粒度 profile」）—— 按 text 长度
    选最匹配的 bucket 算 distance，解决「粒度不对齐时距离系统性偏大」的
    老 bug。详见 docs/style_log_buckets_design.md。
    """
    vec = features(text)
    base_unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )
    # 方案 B 路径：v2 profile 有 buckets 字段 → 选 bucket
    if profile.get("version") == 2 and profile.get("buckets"):
        bucket_name = pick_bucket(len(text or ""), base_unit)
        bucket = profile["buckets"].get(bucket_name) or profile["buckets"].get("medium")
        if bucket is None:
            # 极端：v2 但 buckets 都空 —— 退回 medium bucket 结构
            bucket = {"unit_chars": base_unit, "feat": {}, "holdout": {}}
    else:
        # v1 路径：直接用 profile.feat / profile.holdout
        bucket_name = "v1"
        bucket = profile

    z = z_scores(vec, bucket)
    abs_z = {k: abs(v) for k, v in z.items()}
    total = statistics.fmean(list(abs_z.values())) if abs_z else 0.0

    ranked = sorted(abs_z.items(), key=lambda kv: kv[1], reverse=True)
    holdout = bucket.get("holdout") or {}
    hold = holdout.get("mean_abs_z") or {}
    # `unit_mean_abs_z` 是 `holdout` 的平级字段（见 `fit_profile`），不在 `mean_abs_z` 里面
    dist = holdout.get("unit_mean_abs_z") or []
    percentile: float | None = None
    if dist:
        below = sum(1 for d in dist if d <= total)
        percentile = 100.0 * below / len(dist)

    unit_chars = int(bucket.get("unit_chars") or 0)
    return {
        "chars": len(text or ""),
        "unit_chars": unit_chars,
        "bucket": bucket_name,  # v2 才有意义（"short"/"medium"/"long"）
        # 粒度不对齐时距离会系统性偏大或偏小，必须如实标注而不是静默给个数字
        "scale_ratio": round(len(text or "") / base_unit, 2) if base_unit else None,
        "mean_abs_z": round(total, 3),
        "max_abs_z": round(max(abs_z.values()), 3) if abs_z else 0.0,
        "percentile": None if percentile is None else round(percentile, 1),
        "z": {k: round(v, 3) for k, v in z.items()},
        "worst": [(k, round(v, 2)) for k, v in ranked[:5]],
        "holdout_ref": {
            "p50": round(float(hold.get("p50") or 0.0), 3),
            "p90": round(float(hold.get("p90") or 0.0), 3),
            "max": round(float(hold.get("max") or 0.0), 3),
        } if dist else None,
        "features": {k: round(float(vec.get(k, 0.0)), 3) for k in FEATURES},
    }


# --------------------------------------------------------------------------- #
# 确定性文字描述（进系统提示的静态块）
# --------------------------------------------------------------------------- #

def _n(x: float, digits: int = 1) -> str:
    """紧凑数字：去掉无意义的小数尾巴，避免 3.0 这种噪音。"""
    s = f"{x:.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def describe(profile: dict, *, max_features: int | None = None) -> str:
    """把档案翻成一段**确定性**文字，用于系统提示的静态文体基线块。

    刻意只写数字与目标区间，不写「生动」「细腻」这类形容词——形容词无法验收，
    而数字可以（步骤 5 的验收指标直接量同一批特征）。

    确定性意味着：同一份档案永远产出同一段文本，不随段号/时间变化，
    因此放进静态 system 块不会破坏 prompt 缓存。
    """
    # 方案 B 兼容：v2 profile 是 buckets 嵌套，主档用 medium bucket.feat
    feat = profile.get("feat") or {}
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
    if not feat:
        return ""
    unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )

    def st(k: str) -> dict:
        return feat.get(k) or {}

    lines: list[str] = []
    lines.append(
        f"句长 {_n(st('句长均值')['mean'])}±{_n(st('句长均值')['std'])} 字"
        f"（参差度 CV {_n(st('句长CV')['mean'], 2)}）"
        f" · 每段 {_n(st('段均句数')['mean'])} 句"
        f" · 段长 CV {_n(st('段长CV')['mean'], 2)}"
    )
    lines.append(
        f"对白占正文 {_n(st('对白字占比')['mean'] * 100)}%"
        f"（{_n(st('对白段占比')['mean'] * 100)}% 的段落含对白）"
    )
    lines.append(
        "每千字标点："
        f"，{_n(st('逗号密度')['mean'])} · 。{_n(st('句号密度')['mean'])}"
        f" · ！{_n(st('感叹号密度')['mean'])} · ？{_n(st('问号密度')['mean'])}"
        f" · …{_n(st('省略号密度')['mean'])}"
    )
    lines.append(
        f"上限（超出即偏 AI 腔）：比喻 ≤{_n(st('比喻密度')['p90'])}/千字"
        f" · 情绪直陈 ≤{_n(st('情绪直陈密度')['p90'])}/千字"
        f" · 段尾拔高 ≤{_n(st('段尾升华率')['p90'] * 100)}% 的段落"
    )
    lines.append(
        f"用词粒度：四字格 {_n(st('四字格密度')['mean'])}/千字"
        f" · ≤3 字短串占 {_n(st('短串占比')['mean'] * 100)}%"
    )
    head = (
        f"【文体基线】以下数字来自参考语料的统计（每单元约 {unit} 字），"
        f"不是形容词，请照着写而不是照着想象："
    )
    return head + "\n" + "\n".join(lines)


def feature_table(profile: dict) -> list[dict]:
    """特征清单（面板/报告用）：均值、区间、留出波动、是否代理指标。

    v2 兼容：bucket 嵌套时取 medium bucket 的 feat + holdout。
    """
    feat = profile.get("feat") or {}
    hold = ((profile.get("holdout") or {}).get("feat_abs_z") or {})
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
        hold = ((medium.get("holdout") or {}).get("feat_abs_z") or {})
    rows = []
    for k in FEATURES:
        st = feat.get(k) or {}
        h = hold.get(k) or {}
        rows.append({
            "name": k,
            "note": FEATURE_NOTES.get(k, ""),
            "mean": round(float(st.get("mean") or 0.0), 3),
            "std": round(float(st.get("std") or 0.0), 3),
            "p10": round(float(st.get("p10") or 0.0), 3),
            "p90": round(float(st.get("p90") or 0.0), 3),
            "holdout_p90_z": round(float(h.get("p90") or 0.0), 3) if h else None,
            "proxy": k in PROXY_FEATURES,
        })
    return rows


# --------------------------------------------------------------------------- #
# 方案 B「对数粒度 profile」(PROFILE_VERSION=2)
#
# 8 轮真链路冒烟 + 1 轮 probe + 并行 v2.12 实测确认：fit_profile(unit_chars=3000)
# 切 3000 字/单元统计 mean/std，但生成段 6264 字（2.09x）features 在不同长度
# 上算密度的 z 系统性偏大，触发 percentile 100%。
#
# 修法：fit 3 套 bucket（base/2 + base + base*2），style_distance 按 text
# 长度选最匹配 bucket。
# 设计：docs/style_log_buckets_design.md
# --------------------------------------------------------------------------- #

#: bucket 选档阈值（基于 text_chars / base_unit_chars 比值）。
#: < 0.6 → short，0.6-1.7 → medium，>= 1.7 → long。
#: 留 0.6-1.7 给 medium 覆盖绝大多数「与 unit_chars 粒度接近」的段。
BUCKET_THRESHOLDS = (0.6, 1.7)


def pick_bucket(text_chars: int, base_unit_chars: int) -> str:
    """按 text 长度选最匹配的 bucket 名。

    base_unit_chars 是 profile fit 时的「主档」unit_chars（即 generation_length）。
    阈值 BUCKET_THRESHOLDS = (0.6, 1.7)：
    - text_chars / base_unit_chars < 0.6 → "short"（短文）
    - 0.6 <= text_chars / base_unit_chars < 1.7 → "medium"（主档，对齐旧版）
    - text_chars / base_unit_chars >= 1.7 → "long"（长文）

    base_unit_chars <= 0 时降级返回 "medium"（最稳的兜底）。
    """
    if base_unit_chars <= 0:
        return "medium"
    ratio = text_chars / base_unit_chars
    if ratio < BUCKET_THRESHOLDS[0]:
        return "short"
    if ratio < BUCKET_THRESHOLDS[1]:
        return "medium"
    return "long"


def fit_profile_v2(
    texts: Iterable[str],
    *,
    unit_chars: int = 3000,
    train_ratio: float = 0.7,
    max_units_per_text: int | None = 400,
    label: str = "",
) -> dict:
    """对数粒度 profile fit：3 套 unit_chars（base/2 + base + base*2）。

    返回 profile schema (PROFILE_VERSION=2)：
        {
            "version": 2,
            "label": "...",
            "_meta": {"base_unit_chars": 3000, "schema": "log_buckets_v1"},
            "buckets": {
                "short":  {unit_chars: 1500, n_train, n_holdout, n_units, feat, holdout},
                "medium": {unit_chars: 3000, ...},
                "long":   {unit_chars: 6000, ...},
            }
        }

    每个 bucket 单独跑 fit 的核心逻辑（cut_units + train/holdout + feat + holdout 分布）。
    短语料切不出某 bucket（< 4 单元）时跳过该 bucket，不报错。

    退化：若 medium 切不出（极端短），保留至少 medium 桶用 short 配置。
    """
    base = int(unit_chars)
    bucket_units: dict[str, int] = {
        "short": max(500, base // 2),
        "medium": base,
        "long": base * 2,
    }

    # 切 3 套单元
    bucket_texts: dict[str, list[str]] = {b: [] for b in bucket_units}
    for t in texts:
        for bname, uc in bucket_units.items():
            for u in cut_units(t, uc, max_units=max_units_per_text):
                bucket_texts[bname].append(u)

    profile: dict = {
        "version": PROFILE_VERSION,
        "label": label,
        "_meta": {
            "base_unit_chars": base,
            "schema": "log_buckets_v1",
        },
        "buckets": {},
    }

    for bname, units in bucket_texts.items():
        if len(units) < 4:
            # 短语料切不出该 bucket —— 跳过
            continue
        bucket_profile = _fit_single_bucket(
            units,
            unit_chars=bucket_units[bname],
            train_ratio=0.5,  # 50/50 切分：bucket 单元数少时留出也能保留足够样本
        )
        profile["buckets"][bname] = bucket_profile

    # 退化：medium 都没切出来，用 short 配置当 medium
    if "medium" not in profile["buckets"] and "short" in profile["buckets"]:
        profile["buckets"]["medium"] = dict(profile["buckets"]["short"])
        profile["buckets"]["medium"]["unit_chars"] = base

    return profile


def _fit_single_bucket(
    units: list[str],
    *,
    unit_chars: int,
    train_ratio: float,
) -> dict:
    """单个 bucket 的 fit（与 fit_profile v1 同款逻辑，返回扁平 dict）。"""
    train = [u for i, u in enumerate(units) if i % 10 < int(train_ratio * 10)]
    holdout = [u for i, u in enumerate(units) if i % 10 >= int(train_ratio * 10)]
    if not train:
        train, holdout = units, []
    if not holdout and len(train) > 2:
        train, holdout = train[:-1], train[-1:]

    train_vecs = [features(u) for u in train]
    feat: dict[str, dict[str, float]] = {}
    for k in FEATURES:
        feat[k] = _summarize([v[k] for v in train_vecs])

    bucket: dict = {
        "version": PROFILE_VERSION,
        "unit_chars": int(unit_chars),
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_units": len(units),
        "feat": feat,
        "holdout": {},
    }

    if holdout:
        d = [metric_mean_abs_z(features(u), bucket) for u in holdout]
        bucket["holdout"] = {
            "mean_abs_z": _summarize(d),
            "unit_mean_abs_z": [round(x, 4) for x in d],
        }
        feat_holdout: dict[str, dict[str, float]] = {}
        hvecs = [features(u) for u in holdout]
        for k in FEATURES:
            zs = [
                abs((v[k] - feat[k]["mean"]) / feat[k]["std"])
                if feat[k]["std"] > 0 else 0.0
                for v in hvecs
            ]
            feat_holdout[k] = {
                "mean": float(statistics.fmean(zs)) if zs else 0.0,
                "p90": float(_quantile(sorted(zs), 0.90)),
                "max": float(max(zs)) if zs else 0.0,
            }
        bucket["holdout"]["feat_abs_z"] = feat_holdout
    return bucket
