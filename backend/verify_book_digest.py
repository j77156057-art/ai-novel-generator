"""全书摘要（BookDigest）的可证伪验证。**全部离线**，不调任何模型。

要证的四件事（每件都可能被证伪，所以有 FAIL 闸门）：

  A. **证据强制真的强制**：引文不在原文里 ⇒ 整条被丢。
     这条最要紧：摘要最容易出的事故不是「压得不够狠」，
     而是**看起来很像摘要的编造**（时间线顺、角色像那么回事，
     但那些事件原文里根本没有）。

  B. **三项验收能自动判，而且判得过**：
     压缩比（输出 ≤ 输入 / 目标比）、主线覆盖、角色覆盖。
     关键是「判得过」—— 一个永远 FAIL 的闸门等于没有闸门。

  C. **阳性对照**：一份**故意做坏**的摘要（压得不够 + 漏角色 + 证据全假）
     必须被判 FAIL。
     没有阳性对照的闸门，说明不了它有判别力 ——
     它可能只是「对什么都判 FAIL」。

  D. **档位选择会说明理由**：整本投喂 vs 切块抽，
     以及「渠道没声明上限 ⇒ 不敢整本投喂」这条保守规则。

跑法（仓库根目录）：

    cd backend && python verify_book_digest.py

退出码 0 = 全过；1 = 有 FAIL（**门禁脚本，不是装饰**）。
"""
from __future__ import annotations

import json
import sys

from app.style.digest import (
    MODE_RETRIEVAL,
    MODE_WHOLE,
    build_digest_prompt,
    choose_digest_mode,
    digest_caps,
    enforce_evidence,
    parse_digest,
    render_digest_text,
    verify_digest,
)

PASSED: list[str] = []
FAILED: list[str] = []

#: 构造语料：两章，每章一句「像一件事」的话 + 一句叙述。
CORPUS: dict[int, str] = {
    1: (
        "沈砚第一次杀人是在矿脉塌方那天，他握着镐没敢松手。"
        "老陶把矿灯递给他，说了句走吧，然后自己走在前面。"
        "风从矿道口灌进来，吹得灯焰晃了两下。"
    ),
    2: (
        "沈砚把矿灯还给老陶，转身走进了更深的矿道。"
        "老陶在后面喊了一声，他没有回头。"
        "石头上的水痕一路向下，看不出有多深。"
    ),
    3: (
        "三天后沈砚带着矿灯又下了矿道，这次他没有带镐。"
        "他在岔口停了很久，最后选了左边那条路。"
        "矿道里很安静，只有滴水的声音。"
    ),
    4: (
        "沈砚在坑底看见了那具矿工的尸体，认出了他的工牌。"
        "他把工牌收进口袋，沿着来路慢慢走了回去。"
        "外面的天已经黑了。"
    ),
}

#: 真实存在、可以当引文用的句子。
#: ⚠️ 每一句都注明它**在哪一章** —— 引文挂在错的章上会被证据强制丢掉
#: （这正是这套闸门要抓的事，所以 fixture 本身必须先是对的）。
REAL_QUOTES = [
    "沈砚第一次杀人是在矿脉塌方那天",      # 第 1 章
    "老陶把矿灯递给他",                    # 第 1 章
    "沈砚把矿灯还给老陶",                  # 第 2 章
    "老陶在后面喊了一声",                  # 第 2 章
    "三天后沈砚带着矿灯又下了矿道",        # 第 3 章
    "沈砚在坑底看见了那具矿工的尸体",      # 第 4 章
]

#: 一份**全都做好**的摘要（引文逐字来自原文）。
GOOD_DIGEST = {
    "digest_version": 1,
    "timeline": [
        {
            "seq": 1, "chapter_no": 1, "in_story_time": "开篇",
            "event": "沈砚在矿脉塌方里第一次杀人", "actors": ["沈砚"],
            "evidence": [{"chapter_no": 1, "quote": REAL_QUOTES[0]}],
        },
        {
            "seq": 2, "chapter_no": 2, "in_story_time": "开篇",
            "event": "老陶在矿道口喊了沈砚一声", "actors": ["老陶", "沈砚"],
            "evidence": [{"chapter_no": 2, "quote": REAL_QUOTES[3]}],
        },
        {
            "seq": 3, "chapter_no": 3, "in_story_time": "三天后",
            "event": "沈砚独自又下了矿道", "actors": ["沈砚"],
            "evidence": [{"chapter_no": 3, "quote": REAL_QUOTES[4]}],
        },
    ],
    "characters": [
        {
            "name": "沈砚", "role": "矿工", "arc": "从矿工到独自下矿道的人",
            "key_events": [1, 3],
            "relations": [{"name": "老陶", "kind": "师徒", "changed_at": 3}],
            "voice": {"avg_len": 6, "habits": ["话少"]},
            "evidence": [{"chapter_no": 1, "quote": REAL_QUOTES[0]}],
        },
        {
            "name": "老陶", "role": "老矿工", "arc": "把矿灯交给沈砚后自己走前面",
            "key_events": [1, 2],
            "relations": [{"name": "沈砚", "kind": "师徒", "changed_at": 2}],
            "voice": {"avg_len": 8, "habits": ["话短"]},
            "evidence": [{"chapter_no": 1, "quote": REAL_QUOTES[1]}],
        },
    ],
    "reactions": [
        {
            "character": "沈砚", "trigger": "被叫住",
            "says": "不辩解，只回一句", "does": "转身走开",
            "evidence": [{"chapter_no": 2, "quote": REAL_QUOTES[2]}],
        },
    ],
}

#: 一份**故意做坏**的摘要：引文全是编的、漏了角色、事件与原文无关。
BAD_DIGEST = {
    "digest_version": 1,
    "timeline": [
        {
            "seq": 1, "chapter_no": 1, "in_story_time": "开篇",
            "event": "沈砚屠了半个矿场", "actors": ["沈砚", "老陶"],
            "evidence": [{"chapter_no": 1, "quote": "这句话原文里从来没有出现过"}],
        },
    ],
    "characters": [],
    "reactions": [],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    """记一条结论。FAIL 要写清**为什么**，不然没法复查。"""
    if ok:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


class _Provider:
    """假 provider：只为测档位选择与提示词，不产生内容。"""

    def __init__(self, max_context_chars: int = 0) -> None:
        self.max_context_chars = max_context_chars


def _bloated_report(digest: dict, chapters: dict[int, str]) -> dict:
    """把摘要的条目复制到撑破压缩上限，用它验证压缩比闸门**有判别力**。

    没有这一步的话，「压缩比达标」可能只是一句永远为真的客套话。
    """
    bloated = {
        "timeline": (digest.get("timeline") or []) * 8,
        "characters": (digest.get("characters") or []) * 8,
        "reactions": (digest.get("reactions") or []) * 8,
    }
    return verify_digest(bloated, chapters, chars_in=1200)


def main() -> int:
    section("A. 证据强制")
    kept, stats = enforce_evidence(GOOD_DIGEST, CORPUS)
    check(
        "真实引文的条目全部留下",
        len(kept["timeline"]) == 3 and len(kept["characters"]) == 2
        and len(kept["reactions"]) == 1,
        f"kept={stats['kept']} dropped={stats['dropped']}",
    )
    kept_bad, stats_bad = enforce_evidence(BAD_DIGEST, CORPUS)
    check(
        "编造的引文被整条丢弃",
        kept_bad["timeline"] == [] and stats_bad["dropped"] == 1,
        f"dropped={stats_bad['dropped']}",
    )
    check(
        "证据缺失率如实统计",
        stats_bad["dropped_ratio"] == 1.0,
        f"dropped_ratio={stats_bad['dropped_ratio']}",
    )

    section("B. 三项验收（好摘要要判得过）")
    names = ["沈砚", "老陶"]
    candidates = [
        "沈砚第一次杀人是在矿脉塌方那天，他握着镐没敢松手。",
        "老陶把矿灯递给他，说了句走吧，然后自己走在前面。",
    ]
    good_report = verify_digest(
        kept, CORPUS, chars_in=1200,
        mainline_candidates=candidates, character_names=names,
        evidence_stats=stats,
    )
    checks = good_report["checks"]
    check(
        "压缩比达标（输出 ≤ 输入/10）",
        checks["compression"]["ok"],
        f"ratio={checks['compression']['ratio']}",
    )
    check(
        "主线覆盖达标（≥0.8）",
        checks["mainline_coverage"]["ok"],
        f"value={checks['mainline_coverage']['value']}",
    )
    check(
        "角色覆盖达标（≥0.9）",
        checks["character_coverage"]["ok"],
        f"value={checks['character_coverage']['value']}（候选 {names}）",
    )
    check(
        "好摘要整体通过",
        good_report["passed"] is True,
    )

    section("C. 阳性对照（坏摘要必须 FAIL）")
    kept_bad2, stats_bad2 = enforce_evidence(BAD_DIGEST, CORPUS)
    bad_report = verify_digest(
        kept_bad2, CORPUS, chars_in=1200,
        mainline_candidates=candidates, character_names=names,
        evidence_stats=stats_bad2,
    )
    bad_checks = bad_report["checks"]
    check(
        "坏摘要整体 FAIL",
        bad_report["passed"] is False,
    )
    check(
        "角色覆盖为 0（角色被整块丢掉）",
        bad_checks["character_coverage"]["value"] == 0.0,
        f"value={bad_checks['character_coverage']['value']}",
    )
    check(
        "证据缺失率超阈值 ⇒ FAIL",
        bad_checks["evidence"]["ok"] is False,
        f"dropped_ratio={bad_checks['evidence']['dropped_ratio']}",
    )
    check(
        "压缩比闸门有判别力（撑大的摘要必须判不达标）",
        _bloated_report(kept, CORPUS)["checks"]["compression"]["ok"] is False,
        "把好摘要的条目复制到超出上限 ⇒ compression.ok 必须变 False",
    )
    # 主线覆盖闸门的判别力：**只留开头几条**的摘要（真实产品里最常见的失败模式：
    # 模型只读了开头就交差）必须被挡住。
    truncated = {
        "timeline": (kept.get("timeline") or [])[:1],
        "characters": [],
        "reactions": [],
    }
    trunc_report = verify_digest(
        truncated, CORPUS, chars_in=1200,
        mainline_candidates=candidates, character_names=names,
    )
    trunc_checks = trunc_report["checks"]
    check(
        "主线覆盖闸门有判别力（只覆盖开头的摘要必须判不达标）",
        trunc_checks["mainline_coverage"]["ok"] is False,
        f"value={trunc_checks['mainline_coverage']['value']}",
    )
    check(
        "主线覆盖闸门对好摘要判达标",
        good_report["checks"]["mainline_coverage"]["ok"] is True,
        f"value={good_report['checks']['mainline_coverage']['value']}",
    )

    section("D. 档位选择")
    mode, reason = choose_digest_mode(1200, provider=_Provider())
    check(
        "渠道没声明上限 ⇒ 不敢整本投喂",
        mode == MODE_RETRIEVAL and "未声明" in reason,
        reason,
    )
    mode2, reason2 = choose_digest_mode(1200, provider=_Provider(max_context_chars=1_500_000))
    check(
        "书不大 + 渠道能吃下 ⇒ 整本一次投喂",
        mode2 == MODE_WHOLE,
        reason2,
    )
    # 真书量级（66 万字）+ 声明 150 万字的渠道 ⇒ 整本。
    # 门槛原来写死 30 万字，而本机最小的真书也有 43.6 万字 ⇒ 整本档在真书上
    # 永远不可达 ⇒ 门槛改为跟随渠道声明（见 digest.choose_digest_mode）。
    mode3, reason3 = choose_digest_mode(663_125, provider=_Provider(max_context_chars=1_500_000))
    check(
        "真书量级（66 万字）+ 渠道能吃下 ⇒ 整本一次投喂",
        mode3 == MODE_WHOLE,
        reason3,
    )
    mode4, reason4 = choose_digest_mode(1_600_000, provider=_Provider(max_context_chars=1_500_000))
    check(
        "书超过渠道声明能读的字数 ⇒ 退回切块抽",
        mode4 == MODE_RETRIEVAL,
        reason4,
    )
    mode5, reason5 = choose_digest_mode(3_000_000, provider=_Provider(max_context_chars=50_000_000))
    check(
        "渠道声明得离谱 ⇒ 绝对天花板兜住",
        mode5 == MODE_RETRIEVAL and "绝对天花板" in reason5,
        reason5,
    )
    mode6, _ = choose_digest_mode(900_000, provider=_Provider(), requested=MODE_WHOLE)
    check("用户显式指定就照办", mode6 == MODE_WHOLE)

    section("E. 提示词与解析")
    caps = digest_caps(1200)
    system, user = build_digest_prompt(
        "[第1章]\n" + CORPUS[1], caps=caps, whole=True
    )
    check(
        "提示词要求逐字引文",
        "逐字" in system and "evidence" in system,
    )
    check(
        "提示词写明条数上限",
        f"timeline ≤ {caps.timeline}" in system,
    )
    check(
        "正文进了 user",
        "沈砚第一次杀人" in user,
    )
    parsed = parse_digest(
        '```json\n{"timeline": [], "characters": [], "reactions": []}\n```'
    )
    check(
        "模型输出的 markdown 围栏能剥掉",
        parsed["timeline"] == [],
    )
    text = render_digest_text(kept)
    check(
        "渲染文本含三块",
        all(k in text for k in ("【故事时间线】", "【主要角色】", "【情境反应】")),
    )

    print(f"\n{'=' * 60}")
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(json.dumps({"passed": len(PASSED), "failed": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
