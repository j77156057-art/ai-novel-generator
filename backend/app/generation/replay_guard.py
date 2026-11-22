"""章节复述闸门：防止新章重演上一章已经写过的场景。

## 为什么需要（2026-11-22 五章真机回归）

第 3 章开篇把第 2 章的「子时电梯 + 搬箱子 + 冰袋」整场戏又演了一遍：
同一晚、同一部电梯、同一段对白，只是换了说法。前情摘要里明明写了这些事，
模型仍然「接着演」——因为它把「上一章结尾」当成了本章的起点，而不是
「上一章已经写完，本章从下一件事写起」。

字面重复检测抓不到它：两章之间 20 字以上的整句重复为 0，属于**场景级复述**。

## 两道防线

1. **写前**（`build_replay_guard_block`）：把最近已落库章节的标签、摘要、
   大纲列成「禁止重写」清单，明确要求本章从新时刻起笔；
2. **写后**（`scene_replay_check`）：拿本章**开头**与上一章正文做 8 字片段
   比对。复述场景会在开头集中共享大量片段，新场景则几乎不共享。

## 阈值怎么定的（本项目的真机数据，不是拍的）

五章实测：第 2→3 章开头 1200 字共享 35 个 8 字片段（覆盖率 2.9%），
12 字片段 7 个；其余相邻章共享 0–8 个（覆盖率 ≤0.4%），12 字片段 0 个。
⇒ 判据取「8 字共享 ≥10 且覆盖率 ≥1.5%」或「12 字共享 ≥3」，
离两类样本都留了明显余量。

## 失败策略

检测只读；改写不达标（复述仍在 / 字数越界 / 空输出 / 调用异常）一律**保留原稿**。
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: 复述检测的片段长度（汉字）。8 字能覆盖「路上颠的，冰袋化了」这类短句，
#: 又不至于把「合同上写着呢」这种角色口头禅当成复述。
GRAM = 8
#: 强证据片段长度：连续 12 字相同，基本只可能来自同一场景的重写。
STRONG_GRAM = 12

#: 只看本章开头这么多字。复述发生在开篇，看全文会把正常的伏笔回收误判。
OPENING_CHARS = 1200

#: 判为复述的最少共享片段数（8 字）与最低覆盖率。
MIN_HITS = 10
MIN_COVERAGE = 0.015
#: 强证据片段达到这个数也直接判复述（不必满足覆盖率）。
MIN_STRONG_HITS = 3

#: 正文太短不做判断（不足一个开头窗口，比较没有意义）。
MIN_TEXT_CHARS = 400

#: 提示里最多列几条重复片段。
SAMPLE_LIMIT = 5

#: 改写接受的字数区间（相对原文）。
REWRITE_MIN_RATIO = 0.85
REWRITE_MAX_RATIO = 1.15


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _strip_think_tags(text: str) -> str:
    """剥掉模型偶发回显的思维链标签（只留标签之后的正文）。"""
    if " </think>" in text:
        return text.rsplit(" </think>", 1)[-1].strip()
    return (text or "").strip()


def _grams(text: str, n: int) -> set[str]:
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def _shared_runs(head: str, prior: str, n: int = STRONG_GRAM,
                 limit: int = SAMPLE_LIMIT) -> list[str]:
    """从 `head` 里挑出与 `prior` 相同的连续片段（合并相邻窗口，供提示词举证）。"""
    prior_grams = _grams(prior, n)
    spans: list[tuple[int, int]] = []
    for i in range(max(0, len(head) - n + 1)):
        if head[i:i + n] in prior_grams:
            if spans and i <= spans[-1][1]:
                spans[-1] = (spans[-1][0], i + n)
            else:
                spans.append((i, i + n))
    return [head[a:b] for a, b in spans[:limit]]


def scene_replay_check(
    new_text: str,
    prior_texts,
    *,
    opening_chars: int = OPENING_CHARS,
    min_hits: int = MIN_HITS,
    min_coverage: float = MIN_COVERAGE,
    min_strong_hits: int = MIN_STRONG_HITS,
) -> dict:
    """量本章开头与前文的重叠，判断是否在复述已写场景。

    `prior_texts`：最近已落库章节正文（按时间正序；取最后一章作主对照，
    但也允许传多章——每章都算，取重叠最高的一章作为结论）。

    返回 `{"checked", "replay", "hits", "coverage", "strong_hits",
    "against", "samples", ...}`；`replay=True` 表示需要定向重写。
    """
    text = _norm(new_text)
    priors = [_norm(t) for t in (prior_texts or []) if _norm(t)]
    result = {
        "version": "replay-guard-v1",
        "checked": False,
        "replay": False,
        "opening_chars": opening_chars,
        "hits": 0,
        "coverage": 0.0,
        "strong_hits": 0,
        "against": None,
        "samples": [],
    }
    if len(text) < MIN_TEXT_CHARS or not priors:
        return result
    head = text[:opening_chars]
    head_grams = _grams(head, GRAM)
    if not head_grams:
        return result
    result["checked"] = True

    best: dict | None = None
    for idx, prior in enumerate(priors):
        shared = head_grams & _grams(prior, GRAM)
        strong = _grams(head, STRONG_GRAM) & _grams(prior, STRONG_GRAM)
        coverage = len(shared) / len(head_grams)
        if best is None or len(shared) > best["hits"]:
            best = {
                "index": idx,
                "hits": len(shared),
                "coverage": round(coverage, 4),
                "strong_hits": len(strong),
                "prior": prior,
            }
    assert best is not None
    replay = (
        (best["hits"] >= min_hits and best["coverage"] >= min_coverage)
        or best["strong_hits"] >= min_strong_hits
    )
    result.update({
        "replay": replay,
        "hits": best["hits"],
        "coverage": best["coverage"],
        "strong_hits": best["strong_hits"],
        "against": best["index"],
        "samples": _shared_runs(head, best["prior"]) if replay else [],
    })
    return result


def build_replay_guard_block(prior: list[dict], limit: int = 2) -> str:
    """写前提示块：列出最近已落库章节，明确禁止复述。

    `prior`：按章节正序的 `[{chapter, label, summary, outline}]`。
    只取最近 `limit` 章，避免把整本书塞进提示。
    """
    rows = [r for r in (prior or []) if r.get("chapter")]
    if not rows:
        return ""
    lines = ["【不得复述已写场景（硬要求）】"]
    for r in rows[-limit:]:
        label = str(r.get("label") or f"第{r['chapter']}章").strip()
        detail = str(r.get("summary") or r.get("outline") or "").strip()
        lines.append(f"- 已写完并落库：{label}。" + (detail or ""))
    lines.append(
        "本章正文必须从上述章节**结束之后**的新时刻、新场景写起；"
        "不得把上面任何一场戏再演一遍——同一晚的行动、同一段对话、"
        "同一个道具的搬动都不要重写，也不要用「第二天/当晚」把同一场戏换个说法重来。"
        "需要衔接时最多用一两句话交代时间与状态，然后立即进入本章大纲的新事件；"
        "本章大纲与上一章结尾重叠的部分，只写**尚未发生**的内容。"
    )
    return "\n".join(lines)


_SYSTEM = "你是中文小说编辑，只负责删掉正文开头对上一章场景的复述，不改后续情节。"


def replay_rewrite_once(
    provider,
    text: str,
    check: dict,
    prior: list[dict],
    max_chars: int | None,
) -> tuple[str, dict]:
    """对开头复述做一次定向重写（只动复述段，其余逐字保留）。

    接受条件（全部满足才换稿）：
    1. 重写后复述判据不再命中；
    2. 字数落在原文 85%–115%；
    3. 不超过篇幅硬上限。
    任何异常/失败都保留原文。
    """
    if not check.get("replay"):
        return text, {"applied": False, "reason": "未检测到复述"}
    if not text.strip():
        return text, {"applied": False, "reason": "正文为空"}

    from app.style.consistency import length_limits

    limit = length_limits(max_chars)
    lo = max(1, int(len(text) * REWRITE_MIN_RATIO))
    hi = min(int(limit["max"]), max(lo + 1, int(len(text) * REWRITE_MAX_RATIO)))

    prior_lines = []
    for r in (prior or [])[-2:]:
        label = str(r.get("label") or f"第{r.get('chapter')}章").strip()
        detail = str(r.get("summary") or r.get("outline") or "").strip()
        prior_lines.append(f"- {label}：{detail}")
    samples = check.get("samples") or []
    sample_block = "\n".join(f"- ……{s}……" for s in samples) or "（见上一章正文）"

    user = f"""下面这章正文的**开头部分**复述了前文已经写过的场景。以下片段与上一章重复：

{sample_block}

请只重写**开头到复述结束为止**，其余正文保持逐字不变：
1. 删掉对已发生场景的重演：同一晚的行动、同一段对话、同一个动作的搬动都不要重写；
2. 用不超过 200 字交代时间/状态，直接接到本章真正的新事件；
3. 删掉复述后腾出的篇幅，用于展开本章大纲里**尚未发生**的新剧情，
   不得提前写后面章节的材料；
4. 整章字数控制在 {lo}–{hi} 字；
5. 人物说话方式与叙事口吻保持不变；
6. 输出完整正文，不要解释，不要加标题。

【已发生、不得复述的上一章】
{chr(10).join(prior_lines) or '（见上一章正文）'}

【正文】
{text}"""
    try:
        try:
            prov = provider.variant(thinking_off=True, max_tokens=16000)
        except Exception:  # noqa: BLE001
            prov = provider
        try:
            got = (prov.chat(_SYSTEM, user, temperature=0.3) or "").strip()
        except TypeError:
            got = (prov.chat(_SYSTEM, user) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return text, {"applied": False, "reason": f"调用失败 {type(exc).__name__}"}

    got = _strip_think_tags(got)
    if not got:
        return text, {"applied": False, "reason": "空输出"}
    if len(got) < lo or len(got) > hi:
        return text, {"applied": False, "reason": f"字数越界 {len(got)}（允许 {lo}–{hi}）"}

    after = scene_replay_check(got, [p.get("text") or "" for p in (prior or [])])
    if after.get("replay"):
        return text, {"applied": False, "reason": "复述仍在", "after": after}

    logger.info(
        "复述闸门改写采纳：hits %s → %s（project %s）",
        check.get("hits"), after.get("hits"), None,
    )
    return got, {"applied": True, "before": check, "after": after}
