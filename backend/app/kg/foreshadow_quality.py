"""伏笔条目的**质量判据**：哪些「计划回收章」不该拿来判超期。

## 为什么要有这个

`planned_payoff_chapter = 0`（第 0 章不存在）比任何 current_chapter 都小
⇒ 直接命中「已超期」。实测老账本里这样的行有 **84 条**，
报出来的是「计划第 0 章回收，已超期 9 章仍未揭晓」——**纯噪音**。

`50/30/12` 这类值另有来路：抽取提示的 JSON 示例里写着 `"planned_payoff_chapter": 50`，
模型照抄示例（实测 18 条全填 50）。

⇒ 告警前先问「这个回收章**说得通吗**」：数值不变式 + 正文证据。
说不通的 ⇒ **当未知，不报**。

## 本模块当前只搬了「回收章判据」

main 侧同模块还有「对话碎片检测」（`looks_like_dialogue_fragment`）与
「近似变体」（`name_similarity` / `nearest_existing_name`），它们的消费方
（渲染层过滤、账本去重）在后续批次；等消费方一起搬，避免搬进来没人读。
"""

from __future__ import annotations

#: 章号最小合法值（章从 1 开始）。
_MIN_CHAPTER = 1


def plausible_payoff_chapter(planned, planted=None, text=None, *,
                             closed: bool = False) -> bool:
    """这个「计划回收章」说得通吗？

    判据是**与剧情无关的通用不变式**，不猜内容：

    1. **章号必须 ≥ 1** —— `0` 不存在（实测 97 条 active 里 **72 条是 0**）；
    2. **回收章必须晚于埋下章** —— 同一章回收等于没埋；`planted` 未知时只判第 1 条；
    3. **正文证据** —— 这个章号得**在正文里真的出现过**（「第 N 章」/「第 N 节」）。

    ## `closed=True`：已收尾的行只判第 1 条

    `paid_off` / `dropped` 的回收章记的是**当时的计划**，是历史记录：
    `paid_off` 的「同章埋了又收」合法（实测 #91 `pf=1/planted=1`）；
    `dropped` 的线索从未回收，没有「同章回收」这层语义。
    ⇒ 对 `closed` 行**只判「章号 ≥ 1」**，不拿现在的正文去重新裁决历史。

    ## ⚠️ 为什么**没有**「回收章不能超过已写章数」这条上界

    `planned_payoff_chapter` 是「**计划**回收章」：埋@3 → 计划第 60 章是合法且
    有用的路线图。用「已写章数」当上界会把这条功能直接杀掉
    （用户才写 9 章就想规划第 60 章回收，也会被判成非法）。
    ⇒ 上界改用**证据**判：这个章号正文里说过没有。
    """
    if not isinstance(planned, int) or isinstance(planned, bool):
        return False
    if planned < _MIN_CHAPTER:
        return False
    if closed:
        # 历史记录：不拿现在的正文/埋下章去重新裁决它。
        return True
    if isinstance(planted, int) and not isinstance(planted, bool):
        if planned <= planted:
            return False
    if text is not None:
        # 正文证据：章号没在正文里出现过 ⇒ 不采信。
        # ⚠️ `text=""`（正文取不到）与 `text=None`（不做证据判）不同：
        # 空串 ⇒ 证据判**不放行**任何章号（宁可置空，也不让凭空来的值进账）。
        from app.kg.chapter_evidence import payoff_chapter_in_text

        if not payoff_chapter_in_text(planned, text):
            return False
    return True


def sanitize_payoff_chapter(planned, planted=None, text=None, *, closed: bool = False):
    """说得通就返回原值，说不通返回 None（**当作未知**，不猜一个数补上）。"""
    return planned if plausible_payoff_chapter(planned, planted, text, closed=closed) else None
