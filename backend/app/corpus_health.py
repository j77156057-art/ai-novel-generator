"""语料体检：找出被爬取源注入的重复水印串。

## 为什么需要

实测事故：`novel_dev.db` 里的《斗破苍穹》语料中，**「武动乾坤」（另一本书的书名）
出现 3,750 次**，几乎每一处都被粘在任意句子的末尾：

    '...与周围的世界，有些格格不入武动乾坤。'
    '...脸庞之上的喜意几乎难以掩饰武动乾坤。'

这是盗版站注入的 SEO 水印。后果不止「读起来脏」：
- **文体档案失真**：水印把每 100 字掺进 4 个字，句长、虚词频率、标点密度全被拖偏；
  且各章水印密度不均 → 语料内部离散度虚高，档案变「松」，判别力下降。
- **RAG 检索污染**：检索命中的片段里混着无关书名。
- **图谱抽取污染**：`武动乾坤` 会被当成正文内容参与角色/关系抽取。

## 判据（高精度、零语言学假设）

**注意：不能用「前为汉字、后为标点」当判据。** 我第一版就是这么写的，结果假阳一片 ——
中文没有词间空格，`点点头，表示`、`的说道，`、`了一声。` 全都满足那个条件，它们是正常短语。
（当时「正常词最高 40%」的读数还是**分子分母计数口径不一致**造成的假象，不是真的分离。）

真正有区分度的是**位置分布**：正常短语在句中句末都会出现，而水印是被程序按固定位置
插进去的 —— 它只出现在句末、且几乎总是紧跟同一个句末标点。实测：

| 串 | 次数 | 走到段尾比例 | 紧跟「。」比例 |
|---|---|---|---|
| `武动乾坤`（斗破苍穹，水印） | 3,750 | **100%** | **99.9%** |
| 三本语料里全部正常高频短语 | — | 9%–43% | ≤36% |

间隔足够宽（100% vs 43%），所以 `min_final_ratio=0.9` 有充分余量。
判据仍可能出错，所以每条结论都附**原始上下文样本**供人直接复核。

## 实测影响（单变量实验，`_exp_watermark_effect.py`）

只清洗斗破（另两本 0 命中，天然对照组），其余参数不动：

| 读数 | 清洗前 | 清洗后 |
|---|---|---|
| 斗破自身留出集 p90 | 1.364 | **1.083**（−21%） |
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

定长 n-gram 枚举会在注入串周围造出一整套**阶梯**：`动乾坤` ⊂ `武动乾坤` ⊂
`道武动乾坤` ⊂ `笑道武动乾坤`，共 13 个窗口全部满足判据（它们指向同一批出现位置）。
必须合并，否则 `strip_watermarks` 会照 `笑道武动乾坤` 去替换，把正文里的「笑道」
一起删掉 —— 那是**静默的正文损失**。合并规则见 `_dedupe_nested`。

注意：**不能**按「计数降序 + `in` 判被包含」去嵌套。短串计数可能**高于**长串
（实测 `动乾坤` 3756 > `武动乾坤` 3750；合成用例里 420 > 400），那样判的是
`长串 in 短串`，恒为假，等于没去嵌套。
"""
from __future__ import annotations

import re
from collections import Counter

_CJK = r"[\u4e00-\u9fff]"
_CJK_RUN = re.compile(_CJK + "+")
#: 句末标点（水印几乎只会被插在这些标点之前）
_SENT_FINAL = "。！？"

#: 候选串长度。太短会撞上大量常用词，太长会漏掉 3 字水印。
DEFAULT_LENGTHS = (3, 4, 5, 6)
DEFAULT_MIN_COUNT = 60
DEFAULT_MIN_FINAL_RATIO = 0.9

#: 去嵌套阈值：同组内计数低于「组内最高计数 × 该比例」的成员，判为**偶然的前缀延伸**。
#:
#: 取值依据（实测斗破那一段阶梯）：3,756 → 3,750 → 584 → 196 → 166 → …。
#: 真正属于注入串的窗口，其计数必然与核心同级（前缀恒定就是 100%，前缀有变化就按比例衰减，
#: 但它们共享同一批出现位置）；而偶然延伸是「核心串碰巧被某个词收尾」，量级断崖下跌。
#: `3750/3756 = 0.998`，`584/3756 = 0.156`，0.5 正落在断崖中间 —— 两侧余量都很大。
NEST_KEEP_RATIO = 0.5


def find_repeated_watermarks(
    text: str,
    *,
    lengths: tuple[int, ...] = DEFAULT_LENGTHS,
    min_count: int = DEFAULT_MIN_COUNT,
    min_final_ratio: float = DEFAULT_MIN_FINAL_RATIO,
) -> list[dict]:
    """返回可疑水印串，按出现次数降序。

    每一项形如::

        {"text": "武动乾坤", "count": 3750, "final": 3747,
         "final_ratio": 0.9992, "final_char": "。",
         "para_end_ratio": 1.0, "samples": ["...格格不入武动乾坤。", ...]}

    `samples` 是**原始上下文**：判据可能出错，所以每条结论都必须能被人直接复核，
    而不是只给一个分数。
    """
    if not text or len(text) < min_count:
        return []

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
        final = sum(after.values())
        ratio = final / cnt
        if ratio < min_final_ratio or not after:
            continue
        final_char, _ = after.most_common(1)[0]

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

        samples = [text[max(0, st - 16): en + 6].replace("\n", "\\n")
                   for st, en in occ[:3]]
        flagged.append({
            "text": s,
            "count": cnt,
            "final": final,
            "final_ratio": round(ratio, 4),
            "final_char": final_char,
            "para_end_ratio": round(para_end / cnt, 4),
            "samples": samples,
        })

    # 去嵌套：见 `_dedupe_nested` 的说明。
    return _dedupe_nested(flagged)


def _dedupe_nested(
    flagged: list[dict], keep_ratio: float = NEST_KEEP_RATIO
) -> list[dict]:
    """把「同一段注入串的不同观察窗口」合并成一条。

    定长 n-gram 枚举会在注入串周围产生一整套**阶梯**。以 `武动乾坤` 为例，
    长度 3~6 的窗口里 `动乾坤`、`武动乾坤`、`道武动乾坤`、`笑道武动乾坤` …
    共 13 个全部满足判据 —— 因为它们指向的是同一批出现位置，并不是 13 个水印。

    必须合并，否则 `strip_watermarks` 会照 `笑道武动乾坤` 去替换，把正文里的
    「笑道」一起删掉：那是**静默的正文损失**，比漏删水印严重得多。

    规则：按字符串包含关系做传递闭包分组；组内取最高计数 `top`，
    返回计数 `>= keep_ratio * top` 里**最长**的那一串。

    - 前缀恒定 → 延伸串与核心串计数相同 → 返回延伸串（前缀确实属于注入内容）
    - 前缀有变化 → 延伸串计数断崖下跌 → 返回核心串（实测斗破返回 `武动乾坤`）

    注意不能按「计数降序、短的先进、用 `in` 判被包含」来做 —— 短串计数可能**高于**
    长串（`动乾坤` 3756 > `武动乾坤` 3750），那样判的是 `长串 in 短串`，恒为假，
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
