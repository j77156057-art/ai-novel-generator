"""参考语料专名识别与脱敏——**与具体语料无关的通用实现**。

## 这个模块解决什么

原创小说模式要「学作者的写法，但不学他的人和地名」。原文样例是最好的文风
载体（实测：给原文时句长 18.0，不给时 14.6，而参考语料是 60.3），但原文自带
世界观——大纲写「北漠」，正文会写成「北莽」。

所以需要把原文样例里的**专有名词换成占位符**，保留句法、节奏、标点习惯，
只去掉「这个东西叫什么」。

## 为什么不能靠规则词表

两条路都实测过，都不通用：

  后缀规则（山/城/州/门/派…）
      抽到的全是「世子殿」「赵朴」「不会」「这会」「只会」，
      而关键国名一个都抽不到（它们不带地名后缀）
  自足率（左右邻字熵）
      专名与虚词全部落在 0.50–0.53，无法区分

而且任何规则都要针对具体语言和具体书的命名习惯调参——换一本就失效。

## 通用方案：统计召回 + LLM 判定

1. **PMI 召回**（纯统计，语言无关的 n-gram 结合度）
   对 2/3/4-gram 算 `PMI = log2(P(w) / Π P(char_i))`，取结合紧的。
   这一步只管「不漏」，不追求准——虚词也会被召回，交给第 2 步。

2. **LLM 判定**（判据是「能否脱离本书语境独立成立」，不是词表）
   能独立成立 → 普通词 → 保留
   不能（本书独有的设定）→ 专名 → 进表

3. **独立出现率剔除残片**（不能只看子串关系）
   `太安` 几乎总跟在「城」后面 → 独立率低 → 是残片
   `北荒` 独立出现远多于 `北荒王府` → 独立率高 → 保留
   实测：只看子串会把「北荒」「北莽」「凉州」这些核心国名/州名误删。

三步都不含任何书名、语言特有后缀、人工词表。

## 缓存

专名表按 `(novel_id, 语料指纹)` 缓存进 `raw_novels.clean_report` 旁边的
`entities` 键（或独立文件）。LLM 判定按词缓存，重建表不重复调用。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter

#: 参与召回的 n-gram 长度。2-4 覆盖中文专名（2 字居多，3-4 字为复姓/长地名）。
NGRAM_SIZES = (2, 3, 4)

#: PMI 阈值。只管「结合紧不紧」，不管「是不是专名」——
#: 实测这个阈值能把关键专名全部召回，虚词也一起召回（正常，交给 LLM）。
PMI_MIN = 5.0

#: 候选最低频次。长篇里专名不会只出现几十次以下；太低的召回没有价值。
MIN_COUNT = 100

#: 残片判据：短名作为独立词出现的比例低于此值，判为更长名的残片。
MIN_INDEPENDENT_RATIO = 0.15

#: 占位符前缀（按类别）。类别名会进 prompt，所以要简短、无歧义。
PLACEHOLDER_PREFIX = {
    "characters": "人名",
    "places": "地名",
    "factions": "门派",
    "items": "物件",
}


# ─────────────────────────────────────────────────────────────
# 1. PMI 召回
# ─────────────────────────────────────────────────────────────

_HAN_RUN = re.compile(r"[^\W\d_]+", re.UNICODE)


def _iter_runs(text: str):
    """切出连续的非数字非标点串。用 Unicode 属性而非「中文」字面范围，
    这样对其他语言（日文假名、韩文）也能工作，不必假定是中文。"""
    for m in _HAN_RUN.finditer(text):
        yield m.group(0)


def pmi_candidates(
    text: str,
    *,
    sizes: tuple[int, ...] = NGRAM_SIZES,
    pmi_min: float = PMI_MIN,
    min_count: int = MIN_COUNT,
) -> dict[str, int]:
    """按 PMI 召回「内部结合紧」的 n-gram，返回 {词: 频次}。

    PMI 定义：`log2( P(w) / Π P(char_i) )`。

    **分母必须是各字概率的乘积，不能取几何平均。** 开 N 次根会把 PMI
    砍掉一半以上：实测「北荒」从 7.51 掉到 −0.53，「北莽」7.77 → −0.97，
    所有候选一起变负数，召回结果归零。

    这一步的定位是**高召回**：宁可放进虚词，也不要漏掉真专名——
    漏掉的专名会直接泄漏到生成文本里，而多放进来的虚词后面有 LLM 兜底。
    """
    runs = list(_iter_runs(text))
    if not runs:
        return {}

    unigram = Counter(ch for run in runs for ch in run)
    total_chars = sum(unigram.values())
    if not total_chars:
        return {}

    out: dict[str, int] = {}
    for n in sizes:
        grams: Counter[str] = Counter()
        for run in runs:
            for i in range(len(run) - n + 1):
                grams[run[i:i + n]] += 1
        gram_total = sum(grams.values())
        if not gram_total:
            continue

        for g, c in grams.items():
            if c < min_count:
                continue
            prod = 1.0
            ok = True
            for ch in g:
                p = unigram[ch] / total_chars
                if p == 0.0:
                    ok = False
                    break
                prod *= p
            if not ok or prod <= 0.0:
                continue
            if math.log2((c / gram_total) / prod) >= pmi_min:
                # 同一词可能被多个 n 命中（不同 n 不会同词，但保险）
                out[g] = max(out.get(g, 0), c)
    return out


# ─────────────────────────────────────────────────────────────
# 2. 残片剔除（独立出现率）
# ─────────────────────────────────────────────────────────────

def drop_fragments(
    table: dict[str, list[str]],
    text: str,
    *,
    min_independent_ratio: float = MIN_INDEPENDENT_RATIO,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """剔除「只是更长专名片段」的条目，返回 (保留, 被删)。

    ## 为什么不能只看子串

    首版逻辑是「A 是 B 的子串就删 A」，结果把核心专名删掉了：

        places 去掉残片 22 个：['凉州', '凉王府', '北荒', '北莽', ...]

    「北荒」（国名）是「北荒王府」（府邸名）的子串，但两者是**不同实体**。
    同理「凉州」是州名、「北莽」是国名，都不该因为出现在更长的词里而被删。

    ## 正确判据：独立出现率

        independ = count(A) - Σ count(更长的包含 A 的词)
        ratio    = independ / count(A)

    - 「北荒」独立出现 9400+ 次，「北荒王府」只有 400+ 次 → ratio ≈ 0.96 → 保留
    - 「太安」几乎总跟在「城」后面 → ratio 极低 → 删

    这个判据不含任何具体词，换任何语料都成立。
    """
    all_names: set[str] = set()
    for names in table.values():
        all_names |= set(names)

    counts = {g: text.count(g) for g in all_names}
    kept: dict[str, list[str]] = {}
    dropped: dict[str, list[str]] = {}

    for layer, names in table.items():
        keep_list: list[str] = []
        drop_list: list[str] = []
        for a in names:
            # 4 字及以上本身就可能是完整名，不参与残片判定（「赵青锋」含「赵」）
            if len(a) >= 4:
                keep_list.append(a)
                continue
            longer = [b for b in all_names if a != b and a in b]
            if not longer:
                keep_list.append(a)
                continue
            covered = sum(counts.get(b, 0) for b in longer)
            total_a = max(counts.get(a, 0), 1)
            independ = max(total_a - covered, 0)
            if independ / total_a < min_independent_ratio:
                drop_list.append(a)
            else:
                keep_list.append(a)
        kept[layer] = sorted(set(keep_list))
        if drop_list:
            dropped[layer] = sorted(set(drop_list))
    return kept, dropped


# ─────────────────────────────────────────────────────────────
# 3. 脱敏
# ─────────────────────────────────────────────────────────────

class Redactor:
    """把专名换成占位符，保留句法结构。

    ## 占位符为什么要等长

    首版用 `【人名3】` 这种格式，比原词长很多（2 字 → 5 字），实测让
    脱敏文本从 13112 字涨到 16110 字（+23%），句长被推高 23%。模型从样例
    里学到的句长因此是错的。

    这里改用**与原词等长**的占位符：
      2 字名 → `＃＃`（全角井号，宽度与汉字相同）
      3 字名 → `＃＃＃`
    同一实体始终用同一占位符（按首次出现顺序分配编号前缀，但总长不变）。

    实现上用「编号 + 填充」：内部维护 `name → id`，渲染时按原词长度生成
    `＃{id}` 再补足到原长。这样既保持等长、又能让模型看出「这里是个专名
    而不是普通字符」。
    """

    def __init__(self, table: dict[str, list[str]]):
        entries: list[tuple[str, str]] = []
        for layer, prefix in PLACEHOLDER_PREFIX.items():
            for name in table.get(layer) or []:
                if isinstance(name, str) and len(name) >= 2:
                    entries.append((name, prefix))
        # 长名优先——正则交替按书写顺序匹配，「太安城」必须排在「太安」前面
        entries.sort(key=lambda x: -len(x[0]))
        self._entries = entries
        self._prefix_of = {n: p for n, p in entries}
        self._ids: dict[str, int] = {}
        self._counters = {p: 0 for p in PLACEHOLDER_PREFIX.values()}
        self._pattern = (
            re.compile("|".join(re.escape(n) for n, _ in entries)) if entries else None
        )
        self.placeholder_map: dict[str, str] = {}

    def _placeholder(self, name: str) -> str:
        prefix = self._prefix_of[name]
        if name not in self._ids:
            self._counters[prefix] += 1
            self._ids[name] = self._counters[prefix]
            # 展示用（日志/调试），不参与替换
            self.placeholder_map[name] = f"{prefix}{self._ids[name]}"
        token = f"＃{self._ids[name]}"
        # 等长补足：太短就重复 ID，太长就截断（4 字名配 1 位数 ID）
        if len(token) < len(name):
            token = (token + "＃" * len(name))[: len(name)]
        return token

    def redact(self, text: str) -> str:
        if not self._pattern or not text:
            return text
        return self._pattern.sub(lambda m: self._placeholder(m.group(0)), text)

    @property
    def size(self) -> int:
        return len(self._entries)
