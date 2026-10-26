"""语料体检：找出被爬取源注入的重复水印串。

## 为什么需要

实测事故：`novel_dev.db` 里的《焚天录》语料中，**「风动乾坤」（另一本书的书名）
出现 3,750 次**，几乎每一处都被粘在任意句子的末尾：

    '...与周围的世界，有些格格不入风动乾坤。'
    '...脸庞之上的喜意几乎难以掩饰风动乾坤。'

这是盗版站注入的 SEO 水印。后果不止「读起来脏」：
- **文体档案失真**：水印把每 100 字掺进 4 个字，句长、虚词频率、标点密度全被拖偏；
  且各章水印密度不均 → 语料内部离散度虚高，档案变「松」，判别力下降。
- **RAG 检索污染**：检索命中的片段里混着无关书名。
- **图谱抽取污染**：`风动乾坤` 会被当成正文内容参与角色/关系抽取。

## 判据（高精度、零语言学假设）

**注意：不能用「前为汉字、后为标点」当判据。** 我第一版就是这么写的，结果假阳一片 ——
中文没有词间空格，`点点头，表示`、`的说道，`、`了一声。` 全都满足那个条件，它们是正常短语。
（当时「正常词最高 40%」的读数还是**分子分母计数口径不一致**造成的假象，不是真的分离。）

真正有区分度的是**位置分布**：正常短语在句中句末都会出现，而水印是被程序按固定位置
插进去的 —— 它只出现在句末、且几乎总是紧跟同一个句末标点。实测：

| 串 | 次数 | 走到段尾比例 | 紧跟「。」比例 |
|---|---|---|---|
| `风动乾坤`（《焚天录》，水印） | 3,750 | **100%** | **99.9%** |
| 三本语料里全部正常高频短语 | — | 9%–43% | ≤36% |

间隔足够宽（100% vs 43%），所以 `min_final_ratio=0.9` 有充分余量。
判据仍可能出错，所以每条结论都附**原始上下文样本**供人直接复核。

## 实测影响（单变量实验，`_exp_watermark_effect.py`）

只清洗焚天录（另两本 0 命中，天然对照组），其余参数不动：

| 读数 | 清洗前 | 清洗后 |
|---|---|---|
| 焚天录自身留出集 p90 | 1.364 | **1.083**（−21%） |
| 跨语料最坏重叠率 | 43% | **7%** |
| 分位分离成立 | 4/6 | **6/6** |
| 最小跨语料/同语料比 | 1.56× | **1.76×** |

对照组两本的留出 p90 变化 **Δ = +0.0000**（逐位相同），所以上面的位移只能归因于清洗。

**机制（这里要精确，别写成"水印内容污染了档案"）**：水印贡献 **0 个标点、
0 个虚词、0 个句末标点**（清洗前后句数 8071 → 8071 不变），它只是一段 4 字的
"哑"字符。所以一切「每字密度」类特征的主效应是**纯分母稀释**：
`len(清洗前)/len(清洗后) = 1.04010`，六个密度指标的实测残差全为 `+0.0000`。

真正的结构失真在第二层：水印插在句末标点**之前**，等于给每个中签句子加 4 字
（句长均值 33.66 → 31.97），且各段中签密度不均 → 段长 CV 虚高（0.526 → 0.556）。
20 个特征里 7 个位移 ≥0.25σ，最大的三个是虚词密度 0.60σ / 四字格密度 0.57σ /
逗号密度 0.49σ —— 全部由上述两条机制解释。

## 去嵌套（很重要）

定长 n-gram 枚举会在注入串周围造出一整套**阶梯**：`动乾坤` ⊂ `风动乾坤` ⊂
`道风动乾坤` ⊂ `笑道风动乾坤`，共 13 个窗口全部满足判据（它们指向同一批出现位置）。
必须合并，否则 `strip_watermarks` 会照 `笑道风动乾坤` 去替换，把正文里的「笑道」
一起删掉 —— 那是**静默的正文损失**。合并规则见 `_dedupe_nested`。

注意：**不能**按「计数降序 + `in` 判被包含」去嵌套。短串计数可能**高于**长串
（实测 `动乾坤` 3756 > `风动乾坤` 3750；合成用例里 420 > 400），那样判的是
`长串 in 短串`，恒为假，等于没去嵌套。

## P0 修复：判据实现必须与标定口径一致（刻度标点 + 被粘连）

上面表格标定的是「紧跟**『。』**的比例」：水印 99.9% vs 正常短语 ≤36%。
但修复前 `find_repeated_watermarks` 的实现把 `after` 取成了「**任意**句末标点
（`。！？`）的并集」再求和 —— 正常**问句**以『？』结尾，会被并集计入分子。
现代网文充斥对白问句，于是正常短语越过了阈值被当成水印删掉。实测：

| 串 | 次数 | 旧口径（任意句末） | 『。』比例 | 结果 |
|---|---|---|---|---|
| `风动乾坤`（焚天录，水印） | 40,500 | 1.000 | **1.000** | 应删 |
| `怎么办` | 142 | 0.901 | **0.000** | **误删 128 处** |
| `没事吧` | 107 | 0.972 | **0.000** | **误删 104 处** |
| `怎么了` | 86 | 0.930 | 0.012 | **误删 80 处** |

共 936 字正常对白被删（`'接下来该怎么办？'` / `'炎儿，没事吧？'` / `'薰儿，怎么了？'`）。
这正是本文档开头警告过的「不能用『前为汉字、后为标点』当判据」的同一类错误。

**修复（两处）**：

1. **刻度标点**：比例只看 `DEFAULT_TERMINATOR`（默认『。』）**自己的计数**除以
   `cnt`，不做句末标点并集。这样 `没事吧` 的『。』比例 ≈ 0 < 0.9 → 被拒。
2. **被粘连（必要条件）**：真水印被程序粘在**任意**前导字上，`nesting_breadth`
   = 不同前导字的**种类数**很大；用于挡掉纯巧合的子窗口（如阶梯里 `动乾坤`
   的 breadth=1）。

**关于 breadth 阈值的诚实说明**：breadth **不是**一个能单独区分「自然短语 vs
水印」的信号 —— 实测自然高频短语也能粘在很多前导字上（`缓缓`=363、`顿时`=296、
`的目光`=277；焚天录语料）。真正的判别力来自**刻度标点比例**（自然短语『。』比例
实测 0.0–0.375，全部 < 0.9）。因此 `DEFAULT_MIN_NESTING_BREADTH` 刻意取**低**
（见常量注释）：它只负责把「只跟在极少数字后」的退化窗口排除，宁可漏报也不误删。
"""
from __future__ import annotations

import re
from collections import Counter

_CJK = r"[\u4e00-\u9fff]"
_CJK_RUN = re.compile(_CJK + "+")
#: 句末标点（用于统计与删除位置判定）
_SENT_FINAL = "。！？"

#: 候选串长度。太短会撞上大量常用词，太长会漏掉 3 字水印。
DEFAULT_LENGTHS = (3, 4, 5, 6)
DEFAULT_MIN_COUNT = 60
DEFAULT_MIN_FINAL_RATIO = 0.9

#: 判据的**刻度标点**：比例只看紧跟这一个标点的情况（不做「任意句末标点」并集）。
#: 标定表测的就是「紧跟『。』的比例」——水印 99.9% vs 正常短语 ≤36%。
#: 用并集会把问句（以『？』收尾）计入分子 → 正常对白被误删（见模块文档 P0 节）。
DEFAULT_TERMINATOR = "。"

#: 「被粘连」必要条件的阈值：不同前导字的**种类数**下限。
#:
#: 取值依据（焚天录实测，见模块文档 P0 节）：
#: - 真水印 `风动乾坤` breadth = **1781**（被粘在任意前导字上）；
#: - 阶梯里纯巧合的子窗口 `动乾坤` breadth = **1**（永远跟在 `武` 后）—— 要挡的就是这类。
#: 取 8：远低于真水印（1781），只排除「仅跟在极少数字后」的退化窗口。
#: **刻意取低**：breadth 不是判别主力（自然短语也有高 breadth，`缓缓`=363），
#: 主判据是刻度标点比例；这里守「宁可漏报，不可误删」。
DEFAULT_MIN_NESTING_BREADTH = 8

#: 去嵌套阈值：同组内计数低于「组内最高计数 × 该比例」的成员，判为**偶然的前缀延伸**。
#:
#: 取值依据（实测焚天录那一段阶梯）：3,756 → 3,750 → 584 → 196 → 166 → …。
#: 真正属于注入串的窗口，其计数必然与核心同级（前缀恒定就是 100%，前缀有变化就按比例衰减，
#: 但它们共享同一批出现位置）；而偶然延伸是「核心串碰巧被某个词收尾」，量级断崖下跌。
#: `3750/3756 = 0.998`，`584/3756 = 0.156`，0.5 正落在断崖中间 —— 两侧余量都很大。
NEST_KEEP_RATIO = 0.5


def scan_watermarks(
    text: str,
    *,
    lengths: tuple[int, ...] = DEFAULT_LENGTHS,
    min_count: int = DEFAULT_MIN_COUNT,
    min_final_ratio: float = DEFAULT_MIN_FINAL_RATIO,
    min_nesting_breadth: int = DEFAULT_MIN_NESTING_BREADTH,
    terminator: str = DEFAULT_TERMINATOR,
) -> dict:
    """水印体检的**完整**结果：``{"flagged", "rejected", "thresholds"}``。

    与 `find_repeated_watermarks` 同一套实现，只是额外返回
    **被拒候选 + 拒绝原因** —— 有损清洗必须能看出「检测器考虑过什么、为什么没删」，
    而不是只给一个最终名单。`rejected` 只记**真正有歧义的**候选：
    「（旧口径）任意句末标点比例达标，但刻度标点比例不足」这类**误删风险**候选
    （如 `没事吧`），以及「刻度标点比例达标但未被粘连」的退化窗口。
    这样数量很小、可人工复核，而不是把全部高频短语都倒出来。

    判据（与模块文档标定一致）：
      - `final` = 紧跟**刻度标点 `terminator`（默认『。』）** 的次数；
      - `final_ratio` = final / count，须 >= `min_final_ratio`；
      - `nesting_breadth` = 不同前导字的种类数，须 >= `min_nesting_breadth`。
    """
    thresholds = {
        "lengths": list(lengths),
        "min_count": min_count,
        "min_final_ratio": min_final_ratio,
        "min_nesting_breadth": min_nesting_breadth,
        "terminator": terminator,
    }
    if not text or len(text) < min_count:
        return {"flagged": [], "rejected": [], "thresholds": thresholds}

    # 候选：只在**连续汉字串内部**枚举定长 n-gram。跨标点的 n-gram 不可能是全汉字，
    # 所以这样枚举与原意等价，但对 38 万字语料快一到两个数量级。
    cand: Counter = Counter()
    for rm in _CJK_RUN.finditer(text):
        s = rm.group(0)
        for L in lengths:
            for i in range(len(s) - L + 1):
                cand[s[i:i + L]] += 1

    n_text = len(text)
    flagged: list[dict] = []
    rejected: list[dict] = []
    for s, approx in cand.items():
        if approx < min_count:
            continue
        occ = [(m.start(), m.end()) for m in re.finditer(re.escape(s), text)]
        cnt = len(occ)
        if cnt < min_count:
            continue

        after = Counter(
            text[e] for _, e in occ if e < n_text and text[e] in _SENT_FINAL
        )
        if not after:
            continue
        # 标定口径：只看**刻度标点**自己的计数，不做「任意句末标点」并集。
        final = after.get(terminator, 0)
        ratio = final / cnt
        # 旧口径（任意句末标点并集）只用于诊断「被拒原因」，不参与判定。
        dominant, dominant_n = after.most_common(1)[0]
        pooled_ratio = dominant_n / cnt
        # 「被粘连」广度：不同前导字的种类数。
        leads = {text[st - 1] for st, _ in occ if st > 0}
        breadth = len(leads)
        samples = [text[max(0, st - 16): en + 6].replace("\n", "\\n")
                   for st, en in occ[:3]]

        base = {
            "text": s,
            "count": cnt,
            "final": final,
            "final_ratio": round(ratio, 4),
            "pooled_ratio": round(pooled_ratio, 4),
            "final_char": dominant,
            "terminator": terminator,
            "nesting_breadth": breadth,
            "samples": samples,
        }

        if ratio < min_final_ratio:
            # 只有「以其它句末标点（如问号）结尾」时才够阈值 → 典型误删风险候选。
            if pooled_ratio >= min_final_ratio:
                rejected.append({**base, "reason": (
                    f"紧跟『{terminator}』比例 {ratio:.3f} < {min_final_ratio}；"
                    f"仅以『{dominant}』结尾时达 {pooled_ratio:.3f}"
                    f"（问句误判风险，拒删）"
                )})
            continue
        if breadth < min_nesting_breadth:
            rejected.append({**base, "reason": (
                f"前导字广度 {breadth} < {min_nesting_breadth}"
                f"（未表现为被粘到任意前导字）"
            )})
            continue

        # 段尾比例（走到段末/文末的比例）：仅作**佐证**，不参与筛选 ——
        # 换行格式一变它就不稳，把它当判据会让检测器对语料格式过敏。
        para_end = 0
        for _, e in occ:
            j = e
            while j < n_text and (text[j] in _SENT_FINAL or text[j] in "，、：；…—\"”"):
                j += 1
            while j < n_text and text[j] in " \t":
                j += 1
            if j >= n_text or text[j] == "\n":
                para_end += 1

        flagged.append({**base, "para_end_ratio": round(para_end / cnt, 4)})

    # 去嵌套：见 `_dedupe_nested` 的说明。
    flagged = _dedupe_nested(flagged)
    rejected = _dedupe_nested(rejected)
    rejected.sort(key=lambda d: -d["count"])
    return {"flagged": flagged, "rejected": rejected, "thresholds": thresholds}


def find_repeated_watermarks(
    text: str,
    *,
    lengths: tuple[int, ...] = DEFAULT_LENGTHS,
    min_count: int = DEFAULT_MIN_COUNT,
    min_final_ratio: float = DEFAULT_MIN_FINAL_RATIO,
    min_nesting_breadth: int = DEFAULT_MIN_NESTING_BREADTH,
) -> list[dict]:
    """返回可疑水印串，按出现次数降序（`scan_watermarks` 的 flagged 部分）。

    每一项形如::

        {"text": "风动乾坤", "count": 40500, "final": 40498,
         "final_ratio": 0.99995, "pooled_ratio": 1.0, "final_char": "。",
         "terminator": "。", "nesting_breadth": 1781,
         "para_end_ratio": 1.0, "samples": ["...格格不入风动乾坤。", ...]}

    `final` / `final_ratio` 是**紧跟刻度标点（『。』）**的次数与比例（不是任意句末
    标点的并集）—— 与模块文档的标定口径一致。`samples` 是**原始上下文**：判据可能
    出错，所以每条结论都必须能被人直接复核，而不是只给一个分数。

    需要「被拒候选 + 原因」时用 `scan_watermarks`。
    """
    return scan_watermarks(
        text,
        lengths=lengths,
        min_count=min_count,
        min_final_ratio=min_final_ratio,
        min_nesting_breadth=min_nesting_breadth,
    )["flagged"]


def _dedupe_nested(
    flagged: list[dict], keep_ratio: float = NEST_KEEP_RATIO
) -> list[dict]:
    """把「同一段注入串的不同观察窗口」合并成一条。

    定长 n-gram 枚举会在注入串周围产生一整套**阶梯**。以 `风动乾坤` 为例，
    长度 3~6 的窗口里 `动乾坤`、`风动乾坤`、`道风动乾坤`、`笑道风动乾坤` …
    共 13 个全部满足判据 —— 因为它们指向的是同一批出现位置，并不是 13 个水印。

    必须合并，否则 `strip_watermarks` 会照 `笑道风动乾坤` 去替换，把正文里的
    「笑道」一起删掉：那是**静默的正文损失**，比漏删水印严重得多。

    规则：按字符串包含关系做传递闭包分组；组内取最高计数 `top`，
    返回计数 `>= keep_ratio * top` 里**最长**的那一串。

    - 前缀恒定 → 延伸串与核心串计数相同 → 返回延伸串（前缀确实属于注入内容）
    - 前缀有变化 → 延伸串计数断崖下跌 → 返回核心串（实测焚天录返回 `风动乾坤`）

    注意不能按「计数降序、短的先进、用 `in` 判被包含」来做 —— 短串计数可能**高于**
    长串（`动乾坤` 3756 > `风动乾坤` 3750），那样判的是 `长串 in 短串`，恒为假，
    等于没去嵌套。这里改成与顺序无关的组内比较。
    """
    n = len(flagged)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = flagged[i]["text"], flagged[j]["text"]
            if a in b or b in a:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[dict]] = {}
    for i, d in enumerate(flagged):
        groups.setdefault(find(i), []).append(d)

    kept: list[dict] = []
    for group in groups.values():
        top = max(d["count"] for d in group)
        core = max(
            (d for d in group if d["count"] >= keep_ratio * top),
            key=lambda d: len(d["text"]),
        )
        core["nested_variants"] = sorted(
            (d["text"] for d in group if d is not core), key=len
        )
        kept.append(core)
    kept.sort(key=lambda d: (-d["count"], -len(d["text"])))
    return kept


def strip_watermarks(text: str, marks: list[dict]) -> str:
    """把检出的水印串从正文里删掉（供清洗后的档案重新拟合用）。

    **只删被判定为水印的那种出现**（紧跟句末标点的那一处），不做无差别全文替换：
    判据既然建立在位置分布上，删除就该遵守同一判据。否则正文里偶然出现的同名短语
    会被静默删除 —— 这类损失在语料清洗里是不可见的，事后无法审计。
    """
    if not marks:
        return text
    verbs = sorted({m["text"] for m in marks}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in verbs))
    n_text = len(text)
    out: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        e = m.end()
        if e < n_text and text[e] in _SENT_FINAL:
            out.append(text[pos:m.start()])
            pos = e
    out.append(text[pos:])
    return "".join(out)


def report(texts: dict[str, str], **kw) -> dict[str, list[dict]]:
    """对一批语料逐个体检，返回 {书名: [可疑串 ...]}。"""
    return {name: find_repeated_watermarks(t, **kw) for name, t in texts.items()}


# --------------------------------------------------------------------------- #
# 完本感言 / 跨书简介清洗
# --------------------------------------------------------------------------- #

#: 完本标志：正文结束后作者写的话。「大结局」「完结感言」等。
#: 这些内容不是小说正文，混入语料会污染文风档案和 RAG 检索。
_FINALE_PATTERNS = [
    re.compile(r"^[—\-*=#\s]*大结局[—\-*=#\s]*$", re.MULTILINE),
    re.compile(r"^[—\-*=#\s]*(?:全文完|全书完|正文完|完结)[—\-*=#\s]*$", re.MULTILINE),
]

#: 作者话 / 完本感言的起始标志。从这里开始到文末都是非正文内容。
#: 匹配「各位...亲们」「感谢...支持」「新书...连载」等典型完本话术。
_AFTERWORD_PATTERNS = [
    re.compile(
        r"(?:各位(?:可爱|亲爱|亲|书友|读者|小伙伴)|衷心感谢|非常感谢|无比痛心)"
        r"[^\n]{0,200}?(?:宣布|感谢|支持|陪伴|再见|连载|完结)",
        re.MULTILINE,
    ),
    re.compile(r"(?:新书|下一本|另外一本书)[《\"\"][^》\"]{2,30}[》\"\"][^\n]{0,100}(?:连载|发布|上线|问世)"),
]

#: 跨书简介：另一本书的简介或预告被粘到语料里。
#: 典型格式：「文中男主：...」「文中女主：...」「这是一本...小说」。
#: 也包含「新书《...》已经更新/连载」这类作者在章节末尾的推书。
_CROSS_NOVEL_PATTERNS = [
    re.compile(r"^\s*文中(?:男主|女主|男配|女配)[：:].*$", re.MULTILINE),
    re.compile(r"^\s*这是一本[^\n]{5,80}(?:小说|文|故事)[^\n]{0,40}$", re.MULTILINE),
    re.compile(r"^\s*新书[《\"\"][^》\"]{2,40}[》\"\"][^\n]{0,60}(?:已经|正在|即将)?(?:更新|连载|发布|上线|问世).*$", re.MULTILINE),
]


def _find_finale_start(text: str) -> int | None:
    """找到完本感言的起始位置，返回字符偏移量。找不到返回 None。

    只匹配文末区域内（短文本 50%，长文本 20%）的完本标志——
    正文中间也可能出现「大结局」字样（如章节标题），但那不是全文终结。
    """
    # 短文本（章节级 body）用更低阈值：章节里出现「大结局」就是完本，
    # 不需要等它落到 80% 尾部区域。全书级别才用 80%。
    if len(text) < 5000:
        tail_ratio = 0.3
    else:
        tail_ratio = 0.8
    tail_start = int(len(text) * tail_ratio)
    tail = text[tail_start:]
    earliest = None
    for pat in _FINALE_PATTERNS:
        m = pat.search(tail)
        if m:
            pos = tail_start + m.start()
            if earliest is None or pos < earliest:
                earliest = pos
    # 作者话模式：从匹配位置往前找到段落开头
    for pat in _AFTERWORD_PATTERNS:
        m = pat.search(tail)
        if m:
            pos = tail_start + m.start()
            # 往前找到段落开头
            line_start = text.rfind("\n", 0, pos)
            if line_start == -1:
                line_start = 0
            else:
                line_start += 1
            if earliest is None or line_start < earliest:
                earliest = line_start
    return earliest


def strip_finale_and_cross_novel(text: str) -> tuple[str, dict]:
    """清洗完本感言和跨书简介，返回 (清洗后文本, 清洗报告)。

    两类脏内容：
    1. **完本感言**：大结局后的作者话、感谢词、新书预告——从完本标志到文末全部删除
    2. **跨书简介**：另一本书的简介片段（「文中男主：...」「这是一本...小说」）——逐行删除

    返回的清洗报告含删除的字符数和命中的模式，供人复核。
    """
    removed = 0
    hits: list[str] = []

    # 1. 完本感言：从完本标志到文末
    finale_pos = _find_finale_start(text)
    if finale_pos is not None and finale_pos < len(text):
        removed += len(text) - finale_pos
        hits.append(f"完本感言（删除文末 {len(text) - finale_pos} 字）")
        text = text[:finale_pos].rstrip()

    # 2. 跨书简介：逐行删除
    for pat in _CROSS_NOVEL_PATTERNS:
        matches = pat.findall(text)
        if matches:
            for m in matches:
                hits.append(f"跨书简介：{m[:40]}")
            removed += sum(len(m) for m in matches)
            text = pat.sub("", text)

    # 清理多余空行（只在有删除时才做，正常文本原样返回）
    if removed > 0:
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, {"removed": removed, "hits": hits}


# ────────────────────────────────────────────────────────────
# v2.240：通用「重复段」广告识别
# ────────────────────────────────────────────────────────────

#: 段级广告排除的「叙事词」。含人称代词、典型动作/心理词、连接词。
#:
#: 与 `_FAKE_CHARS` 的区别：那是判定一个名字是不是「人名」的，这里是判定
#: 一段文字是不是「叙事句子」——任何一段包含这些词的，**几乎不可能**是广告。
#:
#: 为什么是这批词？v7 probe 跨 3 本书（《北荒刀歌》/《焚天录》/《长夜司》）
#: 验证：包含人称/动作/心理/连接词的段全部是合法叙事，**没有任何例外**。
_NARRATIVE_BLOCKLIST = (
    "说道笑道喊道叱道喝道低声道沉声道轻声道缓声",
    "看到看着望去瞥见",
    "想着想心中心里头心里面",
    "听到听见听得",
    "他她我你我们他们它们自己",
    "为是在有不都了着与而则把让使从到和或但也",
    "如同甚仿佛似忽骤然忽然",
    "只是才",
    # v2.241 补：主语+短动作的常见模式（《北荒刀歌》误伤案例）
    # 「点了点头」「嗯了一声」「笑了一下」「默不作声」「一言不发」
    "点头", "嗯了", "摇了", "笑了笑", "眨了眨", "不答", "默不",
    "脱口", "笑道", "叹了口气", "笑了一笑", "置之", "没说话",
)

#: 已知合法的高频短段白名单（永远不当作广告）。
#:
#: 这些段虽然频次高、含数字（如「第 X 章」），但作者自加的更新说明或
#: 故事元素，不应被误删。手工维护，遇到误判时追加。
_REPEATED_ADS_WHITELIST = frozenset({
    # 作者更新说明
    "ps：错字先更后改。", "ps:错字先更后改。",
    "先更后改。", "ps：先更后改。",
    "（求收藏）", "求收藏", "求推荐", "求月票", "求订阅",
    "感谢打赏", "感谢订阅", "感谢月票", "感谢支持",
    # 章节结束标记
    "（未完待续）", "未完待续",
    "（本章完）", "本章完",
    "（全书完）", "全书完",
    # 高频音效 / 武功效果（《焚天录》类武侠、玄幻常见）
    "“噗嗤！”", "“阿弥陀佛！”", "“咳咳！”", "“唉！”", "“哈哈！”",
    "“哼！”", "“嗯！”", "“哦？”", "“呐？”",
    "“轰隆隆！”", "“轰轰轰！”", "“砰砰砰！”", "“嗡嗡！”",
    "“轰轰！”", "“嗤嗤！”", "“八极崩！”", "“砰砰！”",
    # v2.241：连续点（作者用「.........」作欲言又止）
    "..........", ".........", "........", ".......", "......",
    ".....", "....", "...",
    # v2.245：连续问号/问号+叹号组合（网文常见语气词）
    "“？？？”", "“？？”", "“？！”", "“！！！”", "“！？”",
    "“什么？！”", "“怎么？！”", "“你？！”",
})


#: 「段落」正则。匹配一段连续非空行（含内部续行）。
_PARAGRAPH_RE = re.compile(r"[^\n\r]+(?:(?:\r\n|\r|\n)[^\n\r]+)*")


def _is_narrative_paragraph(body: str) -> bool:
    """段内是否含强叙事信号（人称/动作/心理/连接词/标点/点串）。

    v2.243：白名单段也返回 True（是叙事），让 strip_repeated_ads 跳过它。
    之前版本返回 False 把白名单段当成广告候选，是逻辑反了。
    """
    # v2.244：连续 3+ 个点 / 中文省略号 = 省略号格式，合法内容
    # （北荒刀歌/大靖作者用「.........」「………」）
    if "..." in body or chr(0x2026) in body:
        return True
    if body in _REPEATED_ADS_WHITELIST:
        return True
    for w in _NARRATIVE_BLOCKLIST:
        if w in body:
            return True
    return False


def strip_repeated_ads(
    text: str,
    *,
    min_count: int = 15,
    min_len: int = 5,
    max_len: int = 80,
    notice_pat: str | None = None,
    templates: set[str] | None = None,
    preserve_layout: bool = False,
) -> tuple[str, dict]:
    """删除**重复段**形式的广告（v2.240 + v2.246 补强）。

    v2.246 补强：把「广告段 + 紧跟的 1 段公告 + 空行」视为 1 个「块」整体删除。
    否则 17 个「（红票/上传/更新）类公告段」会作为「合法但低频段」漏网。

    ## 为什么

    `scan_watermarks` 只识别「焊在句末 `。』」的水印（判据：final_ratio ≥ 阈值），
    《北荒刀歌》真链路 v2.238 暴露漏网：句首广告
    `一秒记住【..info】，为您提供精彩小说阅读。` 在 990 章里出现 604 次，
    final_ratio=0% 全部漏过，**导致 LLM 蒸馏证据校验 rejected 高达 1056**。

    ## 通用方法

    1. 块级：连续非空行视作 1 块（块内允许单 \r\n 续行）
    2. 块首段（首行 5-80 字 + 频次 >= ``min_count`` + 非叙词） = 广告
    3. 整块删除（含块内所有续行 + 块后空行）

    跨 3 本书回归（北荒刀歌 511 万 / 焚天录 603 万 / 大靖 510 万）：

    - 北荒刀歌：识别 `一秒记住【..info】…` 601 次 + 公告段 17 次（块内连删）
    - 焚天录：识别 `手机端阅读请登陆m.zhuaji.org` 89 次
    - 大靖：识别 `ps：先更后改。` 26 次

    **零误伤**。白名单段（武功效果、更新说明）永远跳过。

    ## 与 `scan_watermarks` 的关系

    互补关系：
    - `scan_watermarks` 抓「句末水印」（焚天录类植入式广告）
    - `strip_repeated_ads` 抓「段首/段中重复广告」（北荒刀歌类独立成行广告）
    """
    if not text:
        return text, {"removed": 0, "ads": []}

    # v2.249：放宽到「1 个或多个空行」也匹配。
    # 北荒刀歌用 \r\n\r\n（双换行），焚天录用 \r\r\n\r\r\n（双倍空行），
    # 通用正则：一次或多次「\r」+ 一次或多次「\n」，中间不夹杂内容。
    # 等价「段间至少 1 个空行」。v2.248 漏了焚天录的双倍空行。
    _SEP_RE = re.compile(r"(?:\r+\n+){2,}|\n{2,}")
    ends = [m.end() for m in _SEP_RE.finditer(text)]
    blocks: list[tuple[int, int]] = []
    pos = 0
    for end in ends:
        blocks.append((pos, end))
        pos = end
    if pos < len(text):
        blocks.append((pos, len(text)))

    if not blocks:
        return text, {"removed": 0, "ads": []}

    # 取每个块的首行做频次统计
    first_line_counts: dict[str, int] = {}
    for s, e in blocks:
        body = text[s:e]
        # 块内第一行（遇到 \r\n 截止）
        for sep in ("\r\n", "\n"):
            if sep in body:
                first = body.split(sep, 1)[0].strip()
                break
        else:
            first = body.strip()
        if not (min_len <= len(first) <= max_len):
            continue
        first_line_counts[first] = first_line_counts.get(first, 0) + 1

    # 选广告块：首行频次达标 + 非叙词
    drop_first: set[str] = set()
    ads: list[dict] = []
    for s, n in first_line_counts.items():
        if templates is not None:
            if s not in templates:
                continue
        elif n < min_count:
            continue
        if _is_narrative_paragraph(s):
            continue
        drop_first.add(s)
        ads.append({"text": s, "count": n, "sample_len": len(s)})

    if not drop_first:
        return text, {"removed": 0, "ads": []}

    # 重排文本：以广告首行打头的块整块扣掉
    parts: list[str] = []
    last = 0
    removed = 0
    for s, e in blocks:
        body = text[s:e]
        for sep in ("\r\n", "\n"):
            if sep in body:
                first = body.split(sep, 1)[0].strip()
                break
        else:
            first = body.strip()
        if first not in drop_first:
            continue
        parts.append(text[last:s])
        if preserve_layout:
            # Conservative ingestion mode: only the identified header is evidence.
            # A following narrative line must never disappear with the ad block.
            first_end = next((i for i, c in enumerate(body) if ord(c) in (10, 13)), len(body))
            parts.append(body[first_end:])
        last = e
        removed += e - s
    parts.append(text[last:])
    cleaned = "".join(parts)

    if removed > 0 and not preserve_layout:
        nl = chr(13) + chr(10)
        pattern = "(?:" + nl + "|" + chr(10) + "|" + chr(13) + "){3,}"
        cleaned = re.sub(pattern, lambda m: nl + nl, cleaned).strip()

    return cleaned, {
        "removed": removed,
        "ads": sorted(ads, key=lambda a: -a["count"]),
    }
