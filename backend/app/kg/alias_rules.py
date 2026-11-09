"""化名判据：区分「全篇使用的别名」与「一次性玩笑/典故引用」。

称呼层只收**全篇使用**的别名。下面三类一律不进称呼层，最多作为该章的
文本事实记录（``judge_alias`` 返回 ``ok=False``，调用方按 ``verdict`` 落账）：

- ``allusion``：典故人物名。原文是比喻或引用，不是该角色的别名。
  例：「六号是个苦大仇深的鲁智深」——鲁智深是水浒人物，用来形容恒远；
  「在下陈近南，请多指教」——陈近南是武侠人物，许七安临时借来当化名。
- ``mock``：显式否定/玩笑语境（「根本不是三号的真名」「开玩笑的」）。
- ``single_chapter``：只在一章出现，无法证明「全篇使用」。

判据只做否决，不做自动收录：``ok=True`` 仅表示「可以进称呼层」，
最终仍要人工确认。这样误收的代价是漏一个别名，而不是把比喻折成永久别名。
"""
from __future__ import annotations

import re

#: 典故人物库：古典小说/民间传说里的人物名，几乎不会被用来给角色起名。
#: 命中即拒——原文出现这些名字时，绝大多数是比喻、绰号或玩笑化名。
ALLUSION_NAMES = frozenset({
    # 水浒
    "鲁智深", "武松", "林冲", "宋江", "李逵", "吴用", "卢俊义", "燕青",
    # 三国
    "陈近南", "诸葛亮", "关羽", "张飞", "赵云", "曹操", "刘备", "吕布",
    # 西游
    "孙悟空", "猪八戒", "沙僧", "唐僧",
    # 红楼
    "贾宝玉", "林黛玉", "薛宝钗", "王熙凤",
})

#: 显式否定/玩笑：出现这些说法时，该写法是被当事人否认过的，不能当别名。
_DENIAL = re.compile(
    r"(?:根本|压根|完全)?不是[^。！？]{0,10}(?:真名|本名|名字)"
    r"|(?:化名|假名|冒名|借用.{0,4}名字)"
    r"|(?:开玩笑|戏言|闹着玩|胡说八道)"
)

#: 判据只处理 2 字以上写法；单字无法与普通词区分。
MIN_FORM_LEN = 2


def judge_alias(form: str, chapters, contexts=None,
                canonical: str | None = None) -> dict:
    """判断一个写法能否作为 ``canonical`` 的别名进称呼层。

    参数
    ----
    form:      候选写法（原文里的词）。
    chapters:  该写法出现的章号集合/列表。
    contexts:  该写法出现的句子（用于否决判据）；缺省时跳过语境判断。
    canonical: 规范名；与 form 相同时直接判为 ``identity``。

    返回 ``{"ok": bool, "verdict": str, "reason": str}``。
    """
    form = (form or "").strip()
    canonical = (canonical or "").strip()

    if len(form) < MIN_FORM_LEN:
        return {"ok": False, "verdict": "too_short", "reason": "写法短于 2 字，无法与普通词区分"}
    if canonical and form == canonical:
        return {"ok": False, "verdict": "identity", "reason": "就是规范名本身，不是变体"}

    chs = sorted({int(ch) for ch in (chapters or [])})
    sentences = [str(s) for s in (contexts or []) if s]

    if form in ALLUSION_NAMES:
        sample = next((s for s in sentences if form in s), "")
        return {
            "ok": False,
            "verdict": "allusion",
            "reason": (f"典故人物名，原文是比喻/玩笑而非别名：{sample[:60]}"
                       if sample else "典故人物名，不能作为角色别名"),
        }

    if sentences:
        denied = [s for s in sentences if _DENIAL.search(s)]
        # 半数以上语境都在否认/开玩笑，才判 mock；偶尔一句玩笑不影响全篇别名。
        if len(denied) * 2 >= len(sentences):
            return {
                "ok": False,
                "verdict": "mock",
                "reason": f"否定/玩笑语境占多数：{denied[0][:60]}",
            }

    if len(chs) <= 1:
        return {
            "ok": False,
            "verdict": "single_chapter",
            "reason": f"仅在 {chs[0] if chs else '?'} 章出现，不足以证明全篇使用",
        }

    return {
        "ok": True,
        "verdict": "alias",
        "reason": f"跨 {len(chs)} 章使用（{chs[0]}–{chs[-1]}）",
    }
