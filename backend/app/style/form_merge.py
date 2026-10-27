"""形态校正（机械版）：只改段落边界，不新增一个字。

## 为什么可能是它

LLM 改写的实测采纳率太低（同一段输入重复 4 次 0/4；正式测量 0/20；
三次成功都是偶然）。但目标形态本身是**结构性**的：

    参考书：叙述 + 台词   写在同一段
    生成：  台词         单独一段

要从前者到后者，**把旁边那段叙述并到台词段**就够了 —— 这是纯粹的**段落边界
调整**，不新增内容、不改对白、不改情节，**100% 可复现、零模型调用**。

## 选配对：最大匹配，不是贪心（2026-10-27 实测）

一段纯叙述只能救回**一段**裸台词 ⇒ 这是**路径上的最大匹配**问题（DP 可解）。
早先的实现是「从后往前贪心地找**前面**那段叙述」，**「叙述在台词后面」的形状
一个都救不到**。实测 9 段真实产物：贪心并 112 段（裸台词占比 51.1%），
最大匹配并 162 段（**42.2%**）—— 纯机械、零模型调用，多拿 9 个点。

## 怎么并

1. 找出「纯台词段」（去掉引号内容后只剩标点）；
2. 找它**旁边**那段纯叙述（同场景的邻段，没有引号、引号外字数 ≥ 7）：
   - 叙述在**前面** → `叙述：“台词”`（冒号形态，参考书主流）
   - 叙述在**后面** → `“台词”叙述`（对白在前，参考书也有）
   两种都**不改文字顺序**，只是把段落边界去掉；
3. 配对按**最大匹配**选（见上），不是贪心。

并多少段由**目标比例**定：要达到目标裸台词占比 t，需并 k = n(1-t) 段
（n = 纯台词段数）。

## 唯一一处内容改动：并接处的「。」→「：」

参考书的混合段几乎都用**冒号形态**（`…道：“…”`）：实测前 40 万字，「：+引号」2318 处、
「。」+引号」**仅 3 处**。机械合并天然产出「叙述。“台词”」⇒ **与参考书对不上**。

⇒ 并接时把叙述末尾的「。」改成「：」。**这是全流程唯一改动内容的地方**，
所以它必须能被**独立验证**：`merge_bare_dialogue` 同时返回 `revert_paragraphs`
（把冒号改回去的版本），调用方拿它跟原文逐字比 —— 相同就证明
「除了那一处标点形态修正之外，一个字都没动」。

## 反方向（目标偏高：对白密集型书）

把「叙述 + 台词」拆成两段：叙述段收尾的「：」改成「。」，台词另起一段。
这一步会动一个标点（：→。），所以单独标注。
"""
from __future__ import annotations

import re
from typing import Sequence

#: 纯台词段判定：去掉引号内容后剩下的非标点字符不超过这个数
#: （与 style/stylometry.py 的 `_BARE_SEG_TAIL_MAX` 同一口径）。
BARE_TAIL_MAX = 6

_OPENERS = "“「『"
_CLOSERS = "”」』"

#: 中文句号 / 冒号（写成转义，免得源码里出现全角符号被编辑器改坏）。
_PERIOD = "。"
_COLON = "："


def _join_narration(narration: str, dialogue: str) -> tuple[str, str]:
    """把叙述并到台词前面。返回 **(合并后的段, 把冒号修正改回去的段)**。

    ## 为什么要有第二个返回值

    「。」→「：」是全流程**唯一**改动内容的操作。返回一个「改回去」的版本，
    调用方就能独立验证：把它跟原文逐字比，相同 ⇒ 除了那一处标点形态修正，
    **一个字都没动**。这比「原文 == 新文」更严，也比「放宽成差不多就行」更严。

    ## 只处理「。」

    实测所有可并候选的叙述尾标点 **100% 是「。」**，所以「。」这一种就够用。
    末尾已经是「：」的保持原样（不用再加一个）；「！」「？」等不动
    （改它们会连着改两个字符，形态收益不值那个代价）。
    """
    n = (narration or "").strip()
    d = (dialogue or "").strip()
    if n.endswith(_PERIOD):
        return n[:-1] + _COLON + d, n + d
    return n + d, n + d


def _strip_quoted(text: str) -> str:
    """去掉成对引号及其内容（与 stylometry 同实现）。"""
    out = text
    for o, c in (("“", "”"), ("「", "」"), ("『", "』")):
        while o in out and c in out:
            a = out.index(o)
            b = out.find(c, a)
            if b < 0:
                out = out.replace(o, "")
            else:
                out = out[:a] + out[b + 1:]
    return out


def is_bare_paragraph(p: str) -> bool:
    """这一段是不是「光秃秃一句台词」。"""
    if not any(q in p for q in _OPENERS):
        return False
    rest = _strip_quoted(p).strip("：:，。！？…、；; \t　")
    return len(rest) <= BARE_TAIL_MAX


def _has_quote(p: str) -> bool:
    return any(q in p for q in _OPENERS)


def _outside_chars(p: str) -> int:
    """引号外的字数 —— **与 `is_bare_paragraph` 同一个口径**。

    ⚠️ 这里必须复用尺子自己的算法，不能另写一套：判定「是不是纯台词段」
    看的就是这个数（≤ 6 即为纯台词段），而并段的收益也完全取决于它 ——
    并进来的叙述若只贡献 ≤ 6 字，并完之后那一段**仍然是纯台词段**，
    指标一动不动（白并）。所以门槛必须从这条边界推出来。
    """
    return len(_strip_quoted(p).strip("：:，。！？…、；; \t　"))


def _classify(paras: Sequence[str], gate: int) -> list[str]:
    """把段落分成四类。

    - `N` = 纯叙述段，**且**引号外字数 ≥ gate（可以并进别的段）
    - `n` = 纯叙述段但太短（引号外 < gate）—— 它**并不了**任何段，
      而且它会**挡住**左右的段（不能跨过它合并）
    - `B` = 纯台词段（有引号，引号外 ≤ BARE_TAIL_MAX）
    - `M` = 混合段（有引号，引号外 > BARE_TAIL_MAX）
    """
    out: list[str] = []
    for p in paras:
        if not _has_quote(p):
            out.append("N" if _outside_chars(p) >= gate else "n")
        elif is_bare_paragraph(p):
            out.append("B")
        else:
            out.append("M")
    return out


def _match_pairs(kinds: Sequence[str], direction: str = "both") -> list[tuple[int, int, str]]:
    """在段落链上找「纯叙述段 ↔ 裸台词段」的**最大匹配**。

    ## 为什么是最大匹配而不是贪心

    一段纯叙述只能救回**一段**裸台词（用了就没了），一段裸台词也只配被救一次。
    所以这是一个**路径上的最大匹配**问题：图是若干条路径的并
    （混合段与太短的叙述都隔断链），DP 就能解：`dp[i] = max(dp[i+1], 1 + dp[i+2])`。

    早先的实现是「从后往前贪心地找**前面**那段叙述」，只能并到前面的叙述。
    实测 9 段真实产物：贪心并 112 段（裸台词占比 51.1%），最大匹配能并 162 段（42.0%）
    —— **差 9 个点，纯机械、零模型调用**。差的那 50 段全是
    「叙述在台词**后面**」的形状，贪心根本看不到。

    ## 两种并法（都不改文字顺序）

    - `N B` → `叙述：“台词”`（向前并，**冒号形态** = 参考书主流形态）
    - `B N` → `“台词”叙述`（向后并，对白在前、动作在后；参考书里也有）

    匹配数相同时**优先向前**（冒号形态更贴参考书）。
    """
    if direction not in ("both", "forward"):
        raise ValueError(f"direction 只能是 both / forward，收到 {direction!r}")

    # 切段：只有 N/B 参与，n 与 M 都隔断链
    runs: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    for i, k in enumerate(kinds):
        if k in ("N", "B"):
            cur.append((i, k))
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    pairs: list[tuple[int, int, str]] = []
    for run in runs:
        m = len(run)
        # dp[i] = (匹配数, 向前边数, 配对列表) —— 对 run[i:] 而言
        dp: list[tuple[int, int, list]] = [(0, 0, [])] * (m + 1)
        for i in range(m - 1, -1, -1):
            skip = dp[i + 1]
            best = skip
            if i + 1 < m and run[i][1] != run[i + 1][1]:
                nxt = dp[i + 2]
                fwd = 1 if run[i][1] == "N" else 0
                cand = (1 + nxt[0], fwd + nxt[1],
                        [(run[i][0], run[i + 1][0], "F" if fwd else "B")] + nxt[2])
                if (cand[0], cand[1]) > (skip[0], skip[1]):
                    best = cand
            dp[i] = best
        for a, b, d in dp[0][2]:
            if d == "B" and direction == "forward":
                continue
            pairs.append((a, b, d))
    return pairs


def _explain_no_pairs(kinds: Sequence[str], counts: dict) -> str:
    """并 0 段时，**如实说出是哪一种成因**（诊断改进，不改任何行为）。

    ## 为什么原来那句是误导的

    原来统一报「找不到可并的叙述」。
    但「找不到」有**两种完全不同的成因**，处置方式也相反：

    | 成因 | 特征 | 该怎么办 |
    |---|---|---|
    | **真没有叙述** | `N = 0`（整段正文全是台词/混合段） | 没辙，形状本身就那样 |
    | **有叙述但不相邻** | `N > 0`，却一个配对都找不到 | **混合段把链隔断了** —— 这是**可以救**的 |

    第二种曾被误判成第一种：实测某轮真跑里 `N=15 / B=22 / M=32`，
    报的是「找不到可并的叙述」⇒ 让人以为「这形状天生如此、没办法」；
    而真相是 **M（混合段）占了 32 段、把 N 与 B 全隔开了**
    ⇒ 叙述**有**，只是没有一对是邻居。

    ⇒ 一句笼统的「找不到」会让人**顺着错的方向去修**。
    这里把成因、计数都写进 reason。
    """
    n_narration = counts.get("N", 0)
    n_bare = counts.get("B", 0)
    n_mixed = counts.get("M", 0)
    n_short = counts.get("n", 0)
    if n_narration == 0:
        return (f"没有可并的纯叙述段（裸台词 {n_bare} 段、混合 {n_mixed} 段、"
                f"太短叙述 {n_short} 段）—— 纯叙述一个都没有")
    return (f"有 {n_narration} 段纯叙述，但**没有一段与裸台词段相邻**"
            f"（裸台词 {n_bare} 段、混合 {n_mixed} 段、太短叙述 {n_short} 段）"
            f"—— 混合段/太短叙述把链隔断了，属可救的形状")


def merge_bare_dialogue(
    paragraphs: Sequence[str],
    target_ratio: float,
    *,
    min_narration_chars: int | None = None,
    colon_form: bool = True,
    direction: str = "both",
) -> tuple[list[str], dict]:
    """把「纯台词段」并进旁边那段纯叙述，逼近目标裸台词占比。

    返回 (新段落列表, 诊断)。诊断字段：
        merged     并了几段
        before/after  裸台词段占比（并之前/并之后）
        target     目标
        reason     为什么没并（没得并 / 已达目标 / 找不到可并的叙述
                   —— 最后这句会**区分两种成因**，见 `_explain_no_pairs`）
        counts     段落分类计数（N/n/B/M 各多少）—— 并 0 段时靠它定位成因
        colon_fixed  有几处把「。」改成了「：」
        backward   有几处是「向后并」（`“台词”叙述`）
        revert_paragraphs  **把冒号改回去**的段落列表 —— 调用方拿它跟原文
                     逐字比，相同就证明「除那一处标点形态修正外一个字没动」
    """
    paras = list(paragraphs)
    # 门槛 = **指标自己的边界**：叙述的引号外字数必须 > BARE_TAIL_MAX，
    # 并进来之后那一段才不再是「纯台词段」。
    # （早先用「原始长度 ≥ 20 字」是拍脑袋的数：它把 7~19 字的短叙述
    #   全挡在外面 —— 而那些段并进来照样能把指标拉下来。）
    gate = BARE_TAIL_MAX + 1 if min_narration_chars is None else min_narration_chars

    kinds = _classify(paras, gate)
    bare_idx = [i for i, k in enumerate(kinds) if k == "B"]
    n = len(bare_idx)
    quote_n = sum(1 for p in paras if _has_quote(p))
    before = (n / quote_n) if quote_n else 0.0
    # 分类计数随诊断一起返回：并 0 段时**光看 reason 猜不出原因**，
    # 必须带上 N/B/M/n 各多少（见下面 `_explain_no_pairs`）。
    counts = {
        "N": kinds.count("N"), "n": kinds.count("n"),
        "B": kinds.count("B"), "M": kinds.count("M"),
    }
    info = {"before": before, "after": before, "target": target_ratio, "merged": 0,
            "counts": counts}

    if not n:
        info["reason"] = "没有纯台词段"
        return paras, info
    if before <= target_ratio:
        info["reason"] = f"已经低于目标（{before:.0%} ≤ {target_ratio:.0%}）"
        return paras, info

    # 需要并多少段：并 k 段 → 裸台词段 n-k、含引号段仍 quote_n
    need = n - int(round(target_ratio * quote_n))
    need = max(0, min(need, n))
    if need == 0:
        info["reason"] = "已经贴近目标"
        return paras, info

    # 找配对：**向前**的优先（冒号形态更贴参考书），不够再补向后并的
    pairs = _match_pairs(kinds, direction)
    pairs.sort(key=lambda t: (t[2] != "F", -t[1]))
    pairs = pairs[:need]
    if not pairs:
        info["reason"] = _explain_no_pairs(kinds, counts)
        return paras, info

    # 从后往前并（前面的下标才不会被后面的并段影响）
    merged = 0
    colon_fixed = 0
    backward = 0
    drop: set[int] = set()
    revert: list[str] = list(paras)
    # 配对是 **(靠前下标, 靠后下标)**；方向决定谁是叙述：
    #   "F" = 靠前的是叙述 → `叙述：“台词”`（冒号形态，参考书主流）
    #   "B" = 靠后的是叙述 → `“台词”叙述`（对白在前，文字顺序不变）
    for i, j, d in sorted(pairs, key=lambda t: -t[1]):
        if i in drop or j in drop:
            continue
        if d == "F":
            narr, dialogue = paras[i], paras[j]
        else:
            narr, dialogue = paras[j], paras[i]
        if d == "F":
            # 叙述在前 → 冒号形态；合并段放在**靠前**的位置
            if colon_form:
                joined, reverted = _join_narration(narr, dialogue)
            else:
                joined = narr.strip() + dialogue.strip()
                reverted = joined
        else:
            # 台词在前、叙述在后 → 文字顺序不变，只是并成一段；
            # 合并段放在**靠前**的位置（不能放到后面 —— 那会调换顺序）
            joined = dialogue.strip() + narr.strip()
            reverted = joined
            backward += 1
        paras[i] = joined
        revert[i] = reverted
        if joined != reverted:
            colon_fixed += 1
        drop.add(j)
        merged += 1

    new_paras = [p for i, p in enumerate(paras) if i not in drop]
    revert_paras = [p for i, p in enumerate(revert) if i not in drop]
    n_after = sum(1 for p in new_paras if is_bare_paragraph(p))
    q_after = sum(1 for p in new_paras if _has_quote(p))
    info.update({
        "merged": merged,
        "colon_fixed": colon_fixed,
        "backward": backward,
        "revert_paragraphs": revert_paras,
        "after": (n_after / q_after) if q_after else 0.0,
        "reason": "已并" if merged else _explain_no_pairs(kinds, counts),
    })
    return new_paras, info
