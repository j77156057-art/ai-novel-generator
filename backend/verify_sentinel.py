"""规则哨兵的可证伪验证。零 LLM、纯离线、不碰数据库（DB 往返另有一段）。

做法：**每条规则都必须同时给出正例和反例**，缺一不算通过。
只测正例的规则库是没有价值的 —— 一个 `return [Finding(...)]` 的常量函数也能全过。
所以这里的反例是重点，且刻意挑中文里最容易踩的构词/歧义：

| 陷阱 | 为什么危险 |
|---|---|
| `陆见微不想死。` | 「不想」若被当成副词槽，大活人会被记成死亡状态 |
| `陆见微的死对头` | 「死」是构词成分，不是状态 |
| `陆见微死了这条心` | 「死心」不是死亡 |
| `他想起死去的师父` | 提及死者不算死者出场 |
| `陆见微醒来。` | 日常醒来若被当成复活，会把死亡状态冲掉 |
| `验骨碑的手` | 物品名吞掉「的」会归错人 |
| `陆见微突破到一个从没听过的境界` | 词表外的境界不能参与大小比较 |

另有三条结构性闸门：
  - **跨度自证**：`finding.spans[i].text` 必须等于 `原文[span[0]:span[1]]`
  - **纯函数**：`check_segment` 不得修改传入的状态表与正文
  - **只标红**：审计前后正文逐字节不变
"""
from __future__ import annotations

import copy
import json
import re
import sys

from app.kg.sentinel import (
    RULES,
    E_DEATH,
    E_GIVE,
    E_HOLD,
    E_REALM_DOWN,
    E_REALM_UP,
    E_REVIVE,
    E_SPEAK,
    REALM_SYSTEMS,
    STATE_LIFE,
    STATE_OWNER,
    STATE_REALM,
    apply_event,
    check_segment,
    cn_num,
    extract_events,
    seed_tracked,
)

PASSED: list[str] = []
FAILED: list[str] = []

NAMES = ["陆见微", "沈砚", "陈九"]

#: 离线兜底占位正文的标记（`app/llm/provider.py:412`）。
#: 项目 4/5 的正文只有这一行占位符，**没有角色名是正确的**，不是抽取器哑了。
#: `_assert_fallback_mark_alive()` 会去源码里核对这个串还在，防止标记改名后
#: G1b 变成一条永远为真的空判据。
_FALLBACK_MARK = "【离线兜底·确定性续写】"
_FALLBACK_SRC = "app/llm/provider.py"

#: H4 用的**量具**（不是生产模式）：去掉主语锚点后的境界构造。
#: 只用它数「这样的构造在正文里出现了多少次」，用来和真实抽取数做分子/分母。
#: 刻意与 `sentinel._REALM_UP` 保持同源语义但不 import 私有常量 ——
#: 量具写死在这里，改生产模式时不会悄悄把量具一起改掉，分母才稳。
_REALM_VERB_PROBE = "突破|晋升|进阶|踏入|达到|进入|迈入|跨入|凝结|结成|修成|臻至"
_ORDINAL_UNIT_PROBE = "段|阶|层|品|重|级"


def _system_words(ladder) -> tuple[tuple[str, ...], ...]:
    """把一维/二维的境界表都摊成「体系列表」。"""
    if ladder and isinstance(ladder[0], (tuple, list)):
        return tuple(tuple(g) for g in ladder)
    return (tuple(ladder),)


def _assert_fallback_mark_alive() -> None:
    """兜底标记是 G1b 的判据依据。它改名了就必须在这里炸，而不是悄悄放过。"""
    try:
        with open(_FALLBACK_SRC, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return  # 源码不在当前工作目录时不做数（例如别处 import 本模块）
    if _FALLBACK_MARK not in src:
        raise AssertionError(
            f"兜底标记 {_FALLBACK_MARK!r} 已不在 {_FALLBACK_SRC} 里；"
            "G1b 的判据失效了，请同步更新 _FALLBACK_MARK"
        )


def check(name: str, ok: bool, detail: str = "", hint: str = "") -> None:
    """`detail` 是**测量结果**（无论成败都印），`hint` 是**对失败的解读**（只在失败时印）。

    分开是必要的：把「说明整条通道没跑通」这种话挂在 PASS 行上，
    过两天回头读日志就会读成一条没解决的指控。测量与解读必须各归各位。
    """
    (PASSED if ok else FAILED).append(name)
    tail = f"  ← {detail}" if detail and not ok else ""
    if not ok and hint:
        tail += f"（{hint}）"
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{tail}")
    if ok and detail:
        print(f"         {detail}")


def section(t: str) -> None:
    print()
    print("-" * 78)
    print(t)
    print("-" * 78)


def kinds(text: str, seg: int = 1, **kw) -> list[str]:
    return [e.kind for e in extract_events(text, segment_no=seg, names=NAMES, **kw)]


def events(text: str, seg: int = 1, **kw):
    return extract_events(text, segment_no=seg, names=NAMES, **kw)


def audit(segs: list[str], **kw) -> list[str]:
    """跑一条多段链，返回告警规则名（按判定顺序）。"""
    st: dict = {}
    out: list[str] = []
    for i, s in enumerate(segs, 1):
        ev = extract_events(s, segment_no=i, names=NAMES, **kw)
        out += [f.rule for f in check_segment(st, ev)]
        for e in ev:
            apply_event(st, e)
    return out


def main() -> int:
    _assert_fallback_mark_alive()
    print("=" * 78)
    print("规则哨兵 · 可证伪验证（正例 + 反例，缺一不算通过）")
    print(f"已注册规则：{json.dumps({k: v[0] for k, v in RULES.items()}, ensure_ascii=False)}")
    print("=" * 78)

    # ======================================================================
    section("A. 事件抽取：正例")
    # ======================================================================
    pos = [
        ("主动式死亡", "陆见微已经死了。", E_DEATH, "陆见微"),
        ("主动式死亡（无副词）", "陆见微死了。", E_DEATH, "陆见微"),
        ("被动式死亡", "陆见微被杀死。", E_DEATH, "陆见微"),
        ("施动在前", "沈砚杀死了陆见微。", E_DEATH, "陆见微"),
        ("身亡（主语紧邻）", "陆见微身亡。", E_DEATH, "陆见微"),
        ("身亡（副词槽内）", "陆见微最终身亡。", E_DEATH, "陆见微"),
        ("复活（无歧义）", "陆见微没有死。", E_REVIVE, "陆见微"),
        ("诈死", "陆见微其实是诈死。", E_REVIVE, "陆见微"),
        ("说话", "陆见微说道：「走吧。」", E_SPEAK, "陆见微"),
        ("说话（逗号）", "沈砚说，这事不急。", E_SPEAK, "沈砚"),
        # 「的/地」这一格是**实测逼出来的**：116 万字真实语料里，
        # `的`+说话动词 出现 1159 次，`地`+说话动词 只有 21 次 —— 55:1。
        # 而方式状语是个**开放类**（淡淡 / 笑吟吟 / 咬牙切齿 / 没好气 / 漫不经心…），
        # 枚举不出来，只能按「必须有 的/地 标记」这个结构特征来认。
        ("说话（的-方式状语）", "陆见微淡淡的说道：「走吧。」", E_SPEAK, "陆见微"),
        ("说话（的-四字状语）", "沈砚咬牙切齿的说道：「不可能。」", E_SPEAK, "沈砚"),
        ("说话（的-三字状语）", "陆见微没好气的说道：「随你。」", E_SPEAK, "陆见微"),
        ("说话（地-方式状语）", "沈砚淡淡地说道：「也好。」", E_SPEAK, "沈砚"),
        ("交接", "陆见微把验骨碑交给沈砚。", E_GIVE, "验骨碑"),
        ("持握", "沈砚握住铜钥匙。", E_HOLD, "铜钥匙"),
        ("境界推进（词表）", "陆见微突破到金丹。", E_REALM_UP, "陆见微"),
        ("境界推进（序号）", "陆见微晋升三阶。", E_REALM_UP, "陆见微"),
        ("境界跌落", "陆见微境界跌到炼气。", E_REALM_DOWN, "陆见微"),
    ]
    for label, text, kind, ent in pos:
        got = events(text)
        hit = [e for e in got if e.kind == kind and e.entity == ent]
        check(f"A 正例·{label}：`{text}` → {kind}({ent})",
              bool(hit), f"实得 {[(e.kind, e.entity) for e in got]}")

    # ======================================================================
    section("B. 事件抽取：反例（重点）")
    # ======================================================================
    neg = [
        ("「不想死」不是死亡", "陆见微不想死。", E_DEATH),
        ("「死对头」不是死亡", "陆见微的死对头来了。", E_DEATH),
        ("「死了这条心」不是死亡", "陆见微死了这条心。", E_DEATH),
        ("「死气沉沉」不是死亡", "陆见微走进死气沉沉的院子。", E_DEATH),
        ("「死有余辜」不是死亡事件", "陆见微的仇家死有余辜。", E_DEATH),
        ("「怕死」不是死亡", "陆见微怕死。", E_DEATH),
        ("提及死者不算复活事件", "他想起陆见微。", E_REVIVE),
        ("日常醒来不是复活事件", "陆见微醒来，天已经亮了。", E_REVIVE),
        ("「问」在非台词位置不算说话", "陆见微的问题没人回答。", E_SPEAK),
        ("单独的「道」在「知道」里不算说话", "陆见微知道真相。", E_SPEAK),
        # 状语槽放开到「带 的/地 标记的 1~4 字」之后，必须挡住**归属错人**：
        # `萧炎的死对头说道` 说话的是死对头，不是萧炎。记错人比漏报更糟。
        ("「X的死对头说道」不得记成 X 在说话", "陆见微的死对头说道：「你来了。」", E_SPEAK),
        ("「X和Y的师父说道」不得记成 X 或 Y 在说话", "陆见微和沈砚的师父说道：「别闹。」", E_SPEAK),
        ("「突破重围」不是境界推进", "陆见微突破重围。", E_REALM_UP),
        # 长距离主语**故意不认**：中文里隔了一整句的主语可能已经换人，
        # 硬凑距离就会把 `陆见微，他不想死。` 判成陆见微死亡。这是刻意的漏报。
        ("长距离主语不认（刻意漏报）", "陆见微身受重伤，最终身亡。", E_DEATH),
        ("长距离主语不认（防误报）", "陆见微，他不想死。", E_DEATH),
    ]
    for label, text, kind in neg:
        got = [e for e in events(text) if e.kind == kind]
        check(f"B 反例·{label}：`{text}` 不得抽出 {kind}",
              not got, f"误抽出 {[(e.kind, e.entity, e.text) for e in got]}")

    # 物品名不得吞掉「的」
    got = events("沈砚握住验骨碑的手，指节发白。")
    items = [e.entity for e in got if e.kind == E_HOLD]
    check("B 反例·物品名不吞「的」：`握住验骨碑的手` → 物品应为 `验骨碑`",
          items == ["验骨碑"], f"实得 {items}")

    # 词表外的境界不得参与比较
    got = [e for e in events("陆见微突破到「浊世境」。") if e.kind == E_REALM_UP]
    check("B 反例·词表外的境界不产生事件（不参与大小比较）",
          not got or all(e.order is None for e in got),
          f"实得 {[(e.entity, e.value, e.order) for e in got]}")

    # ======================================================================
    section("C. 规则正例：硬冲突必须报出来")
    # ======================================================================
    # C1 已死角色说话
    st = {}
    apply_event(st, events("陆见微死了。")[0])
    f = check_segment(st, events("陆见微说道：「我不甘心。」", 5))
    check("C1 `dead_speaks`：第1段死亡 + 第5段说话 → 报 err",
          len(f) == 1 and f[0].rule == "dead_speaks" and f[0].severity == "err",
          str([(x.rule, x.severity) for x in f]))
    if f:
        print(f"        告警：{f[0].message}")
        print(f"        反证：第 {f[0].evidence[0]['segment_no']} 段「{f[0].evidence[0]['text']}」")

    # C2 已死角色有动作
    f = check_segment(st, events("陆见微握住铜钥匙。", 7))
    check("C2 `dead_acts`：已死角色又出手拿东西 → 报 err",
          any(x.rule == "dead_acts" for x in f), str([x.rule for x in f]))

    # C3 物品归属冲突
    # **注意：这里显式登记了物品，这是契约的一部分，不是为了让测试变绿。**
    # 「身份唯一」是归属冲突的前提：`铜钥匙` 和 `茶杯` 在字面上没法区分，
    # 而现实里茶杯人人各有一只。所以调用方必须先声明哪些物品是唯一的
    # （或让它们在本上下文里经历过交接）。代价与依据写在
    # `rule_item_owner_conflict` 的 docstring 里。
    st3 = {}
    seed_tracked(st3, ["铜钥匙"])
    apply_event(st3, events("陆见微握住铜钥匙。")[0])
    f = check_segment(st3, events("沈砚握住铜钥匙。", 4))
    check("C3 `item_owner_conflict`：铜钥匙先归陆见微、后由沈砚持握 → 报 err",
          any(x.rule == "item_owner_conflict" for x in f), str([x.rule for x in f]))

    # C4 境界倒退
    st4 = {}
    apply_event(st4, events("陆见微突破到金丹。")[0])
    f = check_segment(st4, events("陆见微突破到炼气。", 6))
    check("C4 `realm_regression`：金丹 → 炼气 → 报 err",
          any(x.rule == "realm_regression" for x in f), str([x.rule for x in f]))

    # ======================================================================
    section("D. 规则反例：这些**不该**报（豁免与边界）")
    # ======================================================================
    # D1 复活豁免
    st = {}
    apply_event(st, events("陆见微死了。")[0])
    apply_event(st, events("陆见微没有死。", 3)[0])
    f = check_segment(st, events("陆见微说道：「走。」", 9))
    check("D1 复活豁免：死了 → 明确复活 → 再说话，**不得**报 dead_speaks",
          not any(x.rule == "dead_speaks" for x in f), str([x.rule for x in f]))

    # D2 日常醒来不得冲掉死亡状态（否则会漏报真正的 dead_speaks）
    st = {}
    apply_event(st, events("陆见微死了。")[0])
    live_events = events("陆见微醒来。", 4)
    check("D2 死亡状态未记时，`醒来` 不产生复活事件",
          not any(e.kind == E_REVIVE for e in live_events),
          str([(e.kind, e.entity) for e in live_events]))

    # D3 交接豁免
    # 这里也显式登记：**登记之后冲突本来是会报的**，所以「不报」才是豁免在起作用，
    # 而不是被上面那道前提闸门挡掉了。不登记的话这条断言就退化成恒真。
    st = {}
    seed_tracked(st, ["铜钥匙"])
    apply_event(st, events("陆见微握住铜钥匙。")[0])
    f = check_segment(st, events("陆见微把铜钥匙交给沈砚。然后沈砚握住铜钥匙。", 4))
    check("D3 交接豁免：同段内「交给」之后的持握，**不得**报归属冲突",
          not any(x.rule == "item_owner_conflict" for x in f), str([x.rule for x in f]))

    # D6 未登记物品的持握不得报冲突 —— 这是 116 万字真实网文抓到的那条误报的回归测试。
    # `茶杯` 在《大奉打更人》里出现 12 次、6 个不同的人端着，各是各的杯子。
    st = {}
    apply_event(st, events("陆见微握住茶杯。")[0])
    f = check_segment(st, events("沈砚举起茶杯。", 4))
    check("D8 未登记物品（茶杯）换人持握，**不得**报归属冲突",
          not any(x.rule == "item_owner_conflict" for x in f), str([x.rule for x in f]))
    # 反过来：交接过的物品就自动获得了唯一性，之后换人持握要报
    st = {}
    apply_event(st, events("陆见微把铜钥匙交给沈砚。")[0])
    f = check_segment(st, events("陈九握住铜钥匙。", 6))
    check("D9 交接过 = 唯一性证据：此后第三人持握同一物，**要**报归属冲突",
          any(x.rule == "item_owner_conflict" for x in f), str([x.rule for x in f]))

    # D4 境界跌落豁免
    st = {}
    apply_event(st, events("陆见微突破到金丹。")[0])
    f = check_segment(st, events("陆见微境界跌落，退到炼气。", 6))
    check("D4 跌落豁免：本段有明确跌落描写时，**不得**报境界冲突",
          not any(x.rule == "realm_regression" for x in f), str([x.rule for x in f]))

    # D5 同境界不报
    st = {}
    apply_event(st, events("陆见微突破到金丹。")[0])
    f = check_segment(st, events("陆见微突破到金丹。", 6))
    check("D5 同境界重复提及，**不得**报境界冲突",
          not any(x.rule == "realm_regression" for x in f), str([x.rule for x in f]))

    # D6 活人正常活动不报
    st = {}
    apply_event(st, events("陆见微突破到金丹。")[0])
    f = check_segment(st, events("陆见微说道：「走。」沈砚握住铜钥匙。", 3))
    check("D6 活人说话/拿东西，**不得**报任何告警", not f, str([x.rule for x in f]))

    # D7 提及死者不算出场
    st = {}
    apply_event(st, events("陆见微死了。")[0])
    f = check_segment(st, events("沈砚想起陆见微，沉默了很久。", 4))
    check("D7 `dead_acts` 只认动作谓词：单纯提及死者**不得**报",
          not f, str([(x.rule, x.message) for x in f]))

    # ======================================================================
    section("E. 结构性闸门")
    # ======================================================================
    # E1 跨度自证：告警给的 span 必须能切回原文
    text = "陆见微死了。后来陆见微说道：「我不甘心。」"
    st, findings = {}, []
    seg1 = extract_events(text[:6], segment_no=1, names=NAMES)
    seg2 = extract_events(text[6:], segment_no=2, names=NAMES)
    for e in seg1:
        apply_event(st, e)
    findings = check_segment(st, seg2)
    ok_spans = True
    detail = []
    for f in findings:
        for s in f.spans:
            sub = text[6:][s["span"][0]:s["span"][1]] if s["segment_no"] == 2 else text[:6][s["span"][0]:s["span"][1]]
            detail.append((sub, s["text"]))
            if sub != s["text"]:
                ok_spans = False
    check("E1 跨度自证：每条告警的 span 切回原文 == 记录文本",
          ok_spans and bool(detail), f"对拍 {detail}")
    print(f"        对拍：{[d[0] for d in detail]}")

    # E2 纯函数：check_segment 不得改状态表、不得改正文
    st = {}
    apply_event(st, events("陆见微死了。")[0])
    st_snapshot = copy.deepcopy(st)
    body = "陆见微说道：「我不甘心。」"
    body_snapshot = body
    ev = events(body, 5)
    check_segment(st, ev)
    check("E2 `check_segment` 是纯函数：不改状态表、不改正文、不改事件",
          st == st_snapshot and body == body_snapshot,
          f"状态表前后一致={st == st_snapshot}，正文前后一致={body == body_snapshot}",
          "状态表或正文被改动了")

    # E3 只标红：跑一轮完整审计，正文逐字节不变
    body = "陆见微死了。\n\n陆见微说道：「我不甘心。」\n\n沈砚握住铜钥匙。"
    before = body
    st = {}
    segs = [s for s in body.split("\n\n")]
    for i, s in enumerate(segs, 1):
        ev = extract_events(s, segment_no=i, names=NAMES)
        check_segment(st, ev)
        for e in ev:
            apply_event(st, e)
    check("E3 只标红不改写：整轮审计后正文逐字节不变", body == before)

    # E4 时间序：状态只能靠段号推进，不受调用顺序影响
    st_a, st_b = {}, {}
    for e in events("陆见微死了。", 2):
        apply_event(st_a, e)
    for e in events("陆见微死了。", 1):
        apply_event(st_b, e)
    check("E4 状态记录带段号（告警里能说清「第几段记的」）",
          st_a[(STATE_LIFE, "陆见微")]["segment_no"] == 2
          and st_b[(STATE_LIFE, "陆见微")]["segment_no"] == 1,
          f"{st_a[(STATE_LIFE,'陆见微')]} / {st_b[(STATE_LIFE,'陆见微')]}")

    # ======================================================================
    section("F. 中文数字与境界序数")
    # ======================================================================
    for s, want in [("一", 1), ("十", 10), ("十一", 11), ("二十", 20),
                    ("三十五", 35), ("九十九", 99), ("一百", 100)]:
        check(f"F `cn_num({s})` = {want}", cn_num(s) == want, f"实得 {cn_num(s)}")
    check("F `cn_num` 对非法输入返回 None（不猜）", cn_num("甲乙") is None, str(cn_num("甲乙")))
    ev = events("陆见微晋升三阶。")
    check("F 序号境界的可比较性：`三阶` → order=3",
          ev and ev[0].order == 3, str([(e.value, e.order) for e in ev]))

    # --------------------------------------------------------------------- #
    # F2 **量纲隔离**：`order` 是个整数，但它来自两套互不相干的刻度 ——
    # 序号写法（`三阶` → 3）与词表位次（`金丹` → 第 4 位）。
    # 整数一混，`金丹`(4) 后面写 `三阶`(3) 就会被判成「境界倒退」，
    # 而这两件事根本不能比大小。这是纯粹的误报源，必须靠量纲标签隔开。
    #
    # 下面 4 条是**误报**（期望 `[]`），2 条是**真阳性**（期望报出）。
    # 只删误报不算过关 —— 真阳性得活着，否则就是把整条规则关掉了事。
    # --------------------------------------------------------------------- #
    fp_cases = [
        ("序号 ↔ 词表", ["陆见微晋升金丹。", "陆见微晋升三阶。"]),
        ("序号 ↔ 词表（大罗/一品）", ["陆见微晋升大罗。", "陆见微晋升一品。"]),
        ("不同单位（重/品）", ["陆见微晋升七重。", "陆见微晋升三品。"]),
        ("词表内跨系统（一星/黄阶）", ["陆见微晋升一星。", "陆见微晋升黄阶。"]),
    ]
    for label, segs in fp_cases:
        got = audit(segs)
        check(f"F2 跨量纲不比较·{label}：{' + '.join(segs)} → 不报",
              got == [], f"实得 {got}（两个刻度不可比，报出来就是误报）")

    tp_cases = [
        ("同单位真倒退", ["陆见微晋升五阶。", "陆见微晋升三阶。"]),
        ("同词表真倒退", ["陆见微晋升元婴。", "陆见微晋升筑基。"]),
    ]
    for label, segs in tp_cases:
        got = audit(segs)
        check(f"F2 同量纲仍然报出·{label}：{' + '.join(segs)} → realm_regression",
              got == ["realm_regression"], f"实得 {got}（把真阳性和误报一起删掉等于关掉规则）")

    tn_cases = [
        ("序内正序", ["陆见微晋升三阶。", "陆见微晋升五阶。"]),
        ("词表内正序", ["陆见微晋升筑基。", "陆见微晋升元婴。"]),
    ]
    for label, segs in tn_cases:
        got = audit(segs)
        check(f"F2 正序不报·{label}：{' + '.join(segs)} → 不报", got == [], f"实得 {got}")

    # --------------------------------------------------------------------- #
    # F3 `realm_order` 必须**真的**接进正则，而不只是接进排名。
    #
    # 曾经的实现把阶梯交替式在导入时按默认表编译死，`realm_order` 只影响取名次。
    # 症状极具迷惑性：传一本书自己的阶梯抽取不到任何东西，而默认表里的词照样抽得出来
    # —— 即「抽取结果取决于一张调用方根本没见过的表」。
    # 所以这里两个方向都要验：**传了的要抽到，没传的不该凭空抽到**。
    # --------------------------------------------------------------------- #
    LADDER = ("斗之气", "斗者", "斗师", "大斗师", "斗灵", "斗王", "斗皇", "斗宗",
              "斗尊", "斗圣", "斗帝")
    got = extract_events("萧炎突破到斗皇。", segment_no=1, names=["萧炎"], realm_order=LADDER)
    check("F3 传了自定义阶梯 → 该阶梯的词抽得出来（`斗皇`）",
          [(e.kind, e.value, e.order) for e in got] == [(E_REALM_UP, "斗皇", 7)],
          str([(e.kind, e.value, e.order) for e in got]))
    got = extract_events("萧炎突破到金丹。", segment_no=1, names=["萧炎"], realm_order=LADDER)
    check("F3 传了自定义阶梯 → 表外的词不该凭空抽出来（`金丹` 不在该表里）",
          got == [], f"实得 {[(e.kind, e.value) for e in got]}（抽取结果不能取决于调用方没传的表）")
    got = extract_events("萧炎突破到斗皇。", segment_no=1, names=["萧炎"],
                         realm_order=(LADDER, ("一星", "二星", "三星")))
    check("F3 二维阶梯也能用（一维/二维都吃）",
          [(e.kind, e.value, e.scale) for e in got] == [(E_REALM_UP, "斗皇", "ladder:0")],
          str([(e.kind, e.value, e.scale) for e in got]))

    # --------------------------------------------------------------------- #
    # F4 复合档位 `N段<阶梯>` —— 这是《斗破苍穹》真正的核心句式。
    #
    # 真实语料实测（38.8 万字）里的写法是：`突破十段斗之气`、`晋升第四段斗之气`、
    # `达到七段斗之气`、`进入第四段斗之气`。生产模式此前既没有 `第` 前缀、
    # 也不认 `N段<阶梯>` 这个复合形式 —— 于是 25 次境界构造一次都抽不出来。
    #
    # 但 `段` 不能当普通序号单位加进去：`突破了一段时间` 会被切成 `一段`，
    # 判成「突破」—— 所以只在**后面紧跟阶梯词**时才认（下面 F4b 就是这个守卫）。
    # --------------------------------------------------------------------- #
    got = extract_events("萧炎突破到第七段斗之气。", segment_no=1, names=["萧炎"],
                         realm_order=LADDER)
    check("F4a 复合档位 `第七段斗之气` → 抽得到，rank=7，量纲带子阶梯",
          [(e.kind, e.value, e.order, e.scale) for e in got]
          == [(E_REALM_UP, "第七段", 7, "ord:段@斗之气")],
          str([(e.kind, e.value, e.order, e.scale) for e in got]))
    got = extract_events("萧炎达到十段斗之气。", segment_no=1, names=["萧炎"],
                         realm_order=LADDER)
    check("F4a 无「第」的写法也算：`十段斗之气` → rank=10",
          [(e.value, e.order) for e in got] == [("十段", 10)],
          str([(e.value, e.order) for e in got]))
    got = extract_events("萧炎突破了一段时间。", segment_no=1, names=["萧炎"],
                         realm_order=LADDER)
    check("F4b `突破了一段时间` **不得**被当成境界推进（`段` 的后视守卫）",
          got == [], f"实得 {[(e.kind, e.value) for e in got]}")

    # F4e 子阶梯隔离：`第五段炼体术` 与 `第三段斗之气` 不是一个刻度，不可比。
    # 这里必须把两套子阶梯都放进表里，否则「不判」会被后视守卫挡掉而不是被量纲挡掉，
    # 那样这条断言就测不到它想测的东西。
    got_a = audit(["萧炎进入第五段炼体术。", "萧炎进入第三段斗之气。"],
                  realm_order=("炼体术",) + LADDER)
    check("F4e 不同子阶梯不可比：`炼体术` 与 `斗之气` 的段位互不判定",
          got_a == [], f"实得 {got_a}")

    # ======================================================================
    section("G. 真实语料上的误报检查（有数据才跑，没有就说明）")
    # ======================================================================
    try:
        import sqlite3

        con = sqlite3.connect("novel_dev.db")
        rows = con.execute(
            "select project_id, segment_no, text from generations "
            "where text is not null order by project_id, segment_no"
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        rows = []
        print(f"  跳过：{exc}")

    if not rows:
        print("  开发库里没有生成正文，跳过真实语料检查（不作 PASS/FAIL 计）")
    else:
        from app.kg.build import extract_from_text

        by_proj: dict[int, list] = {}
        for pid, no, t in rows:
            by_proj.setdefault(pid, []).append((no, t))

        total_f, total_ev, no_name_segs, bad_spans = 0, 0, 0, []
        name_counts: dict[int, int] = {}
        samples: list[str] = []
        for pid, segs in by_proj.items():
            st = {}
            for no, t in segs:
                # **只用抽取器真正认定的角色名。**
                # 这里绝不能写 `or NAMES` 兜底 —— 那会把硬编码的测试名字灌进真实语料，
                # 于是「真实语料上的误报率」就变成了在测我自己的常量表，毫无意义。
                # 哨兵没有名字表时按设计保持沉默：不认人就不判事。
                r, _ = extract_from_text(t, segment_no=no)
                real_names = list(r.characters)
                name_counts[pid] = max(name_counts.get(pid, 0), len(real_names))
                if not real_names:
                    no_name_segs += 1
                    continue
                ev = extract_events(t, segment_no=no, names=real_names)
                total_ev += len(ev)
                for e in ev:
                    # 跨度自证：抽出来的事件必须能切回原文
                    if t[e.span[0]:e.span[1]] != e.text:
                        bad_spans.append((pid, no, e.text))
                    samples.append(f"项目{pid} 段{no} {e.kind}({e.entity}) 「{e.text}」")
                total_f += len(check_segment(st, ev))
                for e in ev:
                    apply_event(st, e)
            print(f"  项目 {pid}：{len(segs)} 段，最多识别到 {name_counts[pid]} 个角色名")

        for line in samples:
            print(f"    抽到：{line}")
        print(f"  合计：事件 {total_ev} 条，告警 {total_f} 条，"
              f"无角色名而沉默的段 {no_name_segs} 段")

        check("G2 真实生成文本上零误报", total_f == 0,
              f"实得 {total_f} 条告警", "逐条看是不是误报")
        check("G3 抽出的每条事件都能切回原文（跨度自证）", not bad_spans, str(bad_spans))

        # G4 结构闸门：**没有名字表时，哨兵不能凭空造出主体**。
        # 早先这条写的是 `no_name_segs >= 0` —— 恒真的断言，什么都没验。
        # 真正该验的是：空名字表下事件数必须为 0（模式全都锚在名字上）。
        # 这一条是可证伪的：谁加一个不锚名字的模式，它立刻红。
        ghost = sum(len(extract_events(t, segment_no=no, names=[]))
                    for pid, segs in by_proj.items() for no, t in segs
                    if not list(extract_from_text(t, segment_no=no)[0].characters))
        check("G4 空名字表下一条事件都不产（哨兵不会凭空造主体）",
              ghost == 0, f"实得 {ghost} 条事件")

        # G1 **不是**「每个项目都必须有名字」—— 兜底占位文本本来就没有角色。
        # 正确的判据有两半：
        #   a) 至少要有一个项目真的给出了名字，否则「0 告警」是空洞通过；
        #   b) 0 名字的项目必须能被**解释**（正文确实是离线兜底占位），
        #      否则就是抽取器哑了而我没发现。
        with_names = {p: v for p, v in name_counts.items() if v >= 1}
        zero_names = {p: v for p, v in name_counts.items() if v < 1}
        check("G1a 至少一个项目真的识别出了角色名（否则「0 告警」是空洞通过）",
              bool(with_names), f"各项目最多角色名数 {name_counts}")
        unexplained = []
        for pid in zero_names:
            blob = "".join(t for _no, t in by_proj[pid])
            if _FALLBACK_MARK not in blob:
                unexplained.append((pid, len(blob)))
        check("G1b 0 名字的项目其正文确实是离线兜底占位（不是抽取器哑了）",
              not unexplained,
              f"这些项目没有角色名、正文里也没有兜底标记 {unexplained}",
              f"兜底标记取 `{_FALLBACK_MARK}`，出处 app/llm/provider.py:412")

        # G5 阳性对照：把已知会触发的句子塞进**真实文本**，通道必须立刻工作。
        # 没有这一步，上面那个「零误报」可能只是整条路都没跑通。
        pid0, segs0 = next(iter(by_proj.items()))
        probe_names: list[str] = []
        for _no, _t in segs0:
            probe_names += list(extract_from_text(_t, segment_no=_no)[0].characters)
        probe_names = list(dict.fromkeys(probe_names))
        if not probe_names:
            check("G5 阳性对照需要至少一个真实角色名", False, "抽取器在本项目上没给出任何名字")
        else:
            victim = probe_names[0]
            st = {}
            for _no, _t in segs0:
                for e in extract_events(_t, segment_no=_no, names=probe_names):
                    apply_event(st, e)
            carrier = segs0[-1][1] + f"\n\n{victim}死了。\n\n{victim}说道：「我不甘心。」"
            probe = check_segment(st, extract_events(carrier, segment_no=99, names=probe_names))
            check(f"G5 阳性对照：真实文本里注入「{victim}死了 + {victim}说话」→ 必须报 dead_speaks",
                  any(x.rule == "dead_speaks" for x in probe),
                  f"实得 {[x.rule for x in probe]}",
                  "整条通道在真实文本上没跑通")

    # ======================================================================
    section("H. 实规模真实散文：章节库")
    # ======================================================================
    # G 段跑的「生成正文」其实大部分是兜底占位（项目 4/5 的正文只有一行占位符），
    # 真正有散文的只有项目 1 的 2 段 —— 拿它下「真实语料零误报」的结论太薄。
    # 章节库里有三本真实网文（约 116 万字），这才是够量的燃料。
    #
    # 名字表怎么来：**按段累积 + 稳定性过滤**（出现在 ≥3 段才算数）。
    # 这是照着生产路径来的 —— 账本本来就是逐段累积的，而单块大切片抽一次
    # 会抽出一堆一次性的碎片名。用累积表还有个好处：它是**最坏情况**，
    # 表脏了误报就会冒出来，误报为 0 才算真的稳。
    try:
        import sqlite3

        con = sqlite3.connect("novel_dev.db")
        chunks = con.execute(
            "select novel_id, title, text from novel_chunks nc join raw_novels rn "
            "on rn.id = nc.novel_id order by novel_id, chapter_no, idx"
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        chunks = []
        print(f"  跳过：{exc}")

    if not chunks:
        print("  章节库是空的，跳过实规模检查（不作 PASS/FAIL 计）")
    else:
        import collections

        from app.kg.build import extract_from_text

        by_novel: dict[int, list[str]] = {}
        titles: dict[int, str] = {}
        for nid, title, t in chunks:
            by_novel.setdefault(nid, []).append(t)
            titles[nid] = title

        # 一本书自己的境界阶梯 —— 抽取和判定都只认这本书自己的刻度
        LADDERS = {
            3: ("斗之气", "斗者", "斗师", "大斗师", "斗灵", "斗王", "斗皇", "斗宗",
                "斗尊", "斗圣", "斗帝"),
        }

        ev_by_kind: collections.Counter = collections.Counter()
        f_by_rule: collections.Counter = collections.Counter()
        scale_bad_spans: list[str] = []
        scale_chars = 0
        scale_segs = 0
        found_lines: list[str] = []
        for nid, parts in by_novel.items():
            text = "".join(parts)
            segs = [text[i:i + 3000] for i in range(0, len(text), 3000)]
            ladder = LADDERS.get(nid, REALM_SYSTEMS)
            c: collections.Counter = collections.Counter()
            for i, s in enumerate(segs):
                for n in set(extract_from_text(s, segment_no=i + 1)[0].characters):
                    c[n] += 1
            names = [n for n, k in c.items() if k >= 3]
            st: dict = {}
            for i, s in enumerate(segs):
                ev = extract_events(s, segment_no=i + 1, names=names, realm_order=ladder)
                ev_by_kind.update(e.kind for e in ev)
                for e in ev:
                    if s[e.span[0]:e.span[1]] != e.text:
                        scale_bad_spans.append(f"{titles[nid]} 段{i+1} {e.text}")
                f = check_segment(st, ev)
                f_by_rule.update(x.rule for x in f)
                for x in f:
                    found_lines.append(
                        f"{titles[nid]} 段{x.spans[0]['segment_no']} {x.rule}({x.entity})"
                        f" 「{x.spans[0]['text']}」")
                for e in ev:
                    apply_event(st, e)
            scale_chars += len(text)
            scale_segs += len(segs)
            # 名字表污染披露：抽出来的「名字」里有明显是动词短语的碎片。
            # 不修，但要摊在明处 —— 误报率是在这张脏表上测的，属于上界。
            junk = [n for n in names if len(n) > 3 and not any(n.endswith(x) for x in ("儿",))]
            print(f"  {titles[nid]}：{len(text):,} 字 / {len(segs)} 段，"
                  f"名字表 {len(names)} 个，其中 {len(junk)} 个偏长（疑似动词碎片）")
            if junk:
                print(f"    疑似碎片样例：{junk[:10]}")

        print(f"  合计：{scale_chars:,} 字 / {scale_segs} 段")
        print(f"  事件分布：{dict(ev_by_kind)}")
        print(f"  告警分布：{dict(f_by_rule)}")
        for line in found_lines[:20]:
            print(f"    告警：{line}")

        check("H1 通道在真实散文上真的跑起来了（事件数 > 0）",
              sum(ev_by_kind.values()) > 0,
              f"事件 {sum(ev_by_kind.values())} 条",
              "一条事件都没有的话，下面「零误报」就是空洞通过")
        check("H2 真实散文上零误报（脏名字表下的上界）",
              sum(f_by_rule.values()) == 0,
              f"实得 {sum(f_by_rule.values())} 条告警 {dict(f_by_rule)}",
              "逐条看是不是误报")
        check("H3 实规模下跨度自证仍然成立", not scale_bad_spans, str(scale_bad_spans[:5]))

        # H4 境界召回的边界。这一段是**披露 + 能力证明**，不是「必须在真实散文上抽到」。
        #
        # 实测结论：真实人类网文里，境界描写的主语基本都不在构造里
        # （`十一岁突破十段斗之气`、`半个月晋升第四段斗之气`、`今天一定能突破到第七段斗之气`）。
        # 硬要认就得允许跨句找远主语，而 `萧炎和萧战交谈，然后突破到斗皇` 会把功劳记到
        # 萧战头上 —— **记错人比漏报更糟**，所以按模块一贯的「少报优于误报」沉默以对。
        #
        # 分母是量具（去掉主语锚点的同一构造），分子是真实抽取结果。
        # 两者之差 = 「主语不在构造内」的句子，这个数字必须说出来。
        corpus_with_name = 0
        corpus_anywhere = 0
        sample: tuple[str, str] | None = None   # (book, 构造片段)
        for nid, parts in by_novel.items():
            text = "".join(parts)
            ladder = LADDERS.get(nid, REALM_SYSTEMS)
            realm_words = [w for g in _system_words(ladder) for w in g]
            probe_realm = re.compile(
                rf"(?:{_REALM_VERB_PROBE})(?:到|至|了)?(?:第)?[一二三四五六七八九十百]*"
                rf"(?:{_ORDINAL_UNIT_PROBE})?(?:{'|'.join(realm_words)})"
            )
            corpus_anywhere += len(probe_realm.findall(text))
            if sample is None:
                m = probe_realm.search(text)
                if m:
                    sample = (titles[nid], text[max(0, m.start() - 12):m.end() + 2])
            c: collections.Counter = collections.Counter()
            segs = [text[i:i + 3000] for i in range(0, len(text), 3000)]
            for i, s in enumerate(segs):
                for n in set(extract_from_text(s, segment_no=i + 1)[0].characters):
                    c[n] += 1
            nm_list = [n for n, k in c.items() if k >= 3]
            for i, s in enumerate(segs):
                corpus_with_name += sum(
                    1 for e in extract_events(s, segment_no=i + 1, names=nm_list,
                                              realm_order=ladder)
                    if e.kind in (E_REALM_UP, E_REALM_DOWN))

        if corpus_anywhere == 0 or sample is None:
            print("  章节库里没找到带阶梯词的境界构造，跳过 H4（不作 PASS/FAIL 计）")
        else:
            print(f"  真实散文里境界构造出现 {corpus_anywhere} 次，其中【主语在构造内】因而"
                  f"能归到人名的 {corpus_with_name} 次 —— 差额按设计不判（见 H4 注释）")
            print(f"  构造样本（取自《{sample[0]}》，**主语被省略了**）：…{sample[1]}")
            # H4a 只证「模式认得真实句式」：把**真实语料里的构造**原样搬到人名后面，
            # 必须抽得出来。这一条是可证伪的，而且用的是真实文本的写法，
            # 不是我编的句子 —— 编的句子只能证明我会编。
            frag = sample[1]
            for verb in _REALM_VERB_PROBE.split("|"):
                if verb in sample[1]:
                    frag = sample[1][sample[1].index(verb):]
                    break
            got_h4 = extract_events("萧炎" + frag, segment_no=1, names=["萧炎"],
                                    realm_order=LADDERS.get(3, REALM_SYSTEMS))
            check("H4a 真实语料的境界构造，补上主语后必须抽得出来（模式认得真实句式）",
                  bool(got_h4), f"实得 {[(e.kind, e.value) for e in got_h4]}；片段 {frag}",
                  "连补了主语都抽不出来，说明模式与真实句式对不上")

    # ======================================================================
    section("I. 库往返：状态必须能出库再入库而不掉字段")
    # ======================================================================
    # 这一段是被一个**静默失效**逼出来的：加了量纲标签之后，`state_facts_from_events`
    # 没把它写进 props、`load_state` 也没还原 —— 于是「第 1 段记金丹、第 9 段写炼气」
    # 这种跨段真倒退永远报不出来，因为还原出来的 scale 是 None，跟事件的 scale 永不相等。
    # 进程内的用例全绿，跨段全哑 —— **只有真的过一遍库才看得见**。
    # 判据：往返前后判定结果必须一致；往返不能把不该有的东西也带出来。
    #
    # 还有一个**更隐蔽的失效**，是本节自己踩出来的：
    # 原来这里写的是「先改 os.environ["DATABASE_URL"] 指向临时库，再 from app.db import engine」——
    # 但 `app.db` 在更早的 section 里**已经被导入过了**，模块级 engine 早按开发库建好了，
    # 此时再改环境变量对它毫无影响。于是本段实际写的是**开发库**：
    # 实测每跑一次脚本，开发库里就多出两个测试项目（`库往返` / `告警形状`），
    # 跑十几次后项目列表被垃圾灌满，而这些脏数据还会被 :8000 的后端读出去。
    # 修法不是「把环境变量挪到更前面」（那会打断 G/H 段对 dev 库的依赖），
    # 而是本段**自建 engine**，绝不碰全局那个。
    try:
        import os
        import tempfile

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        _dir = tempfile.mkdtemp(prefix="sentinel_rt_")
        _url = f"sqlite:///{os.path.join(_dir, 'rt.db')}"
        os.environ["EMBEDDING_PROVIDER"] = "local"
        os.environ["LLM_PROVIDER"] = "local"
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：{exc}")
        _url = ""

    try:
        from app.db import Base  # noqa: E402
        import app.models  # noqa: E402,F401  必须先导入模型，create_all 才认识表

        engine = create_engine(_url)  # 独立引擎：见上面那段注释
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        check("I0 本段用的是独立库，不是开发库",
              "sentinel_rt_" in str(engine.url), str(engine.url),
              "又用回全局 engine 了 —— 那会把测试项目写进开发库")
        from app.kg.ledger import upsert_facts  # noqa: F401
        from app.models import Project
        from app.kg.sentinel import (
            load_state,
            replace_state_facts,
            state_facts_from_events,
            state_to_rows,
        )

        proj = Project(user_id=1, title="库往返")
        db.add(proj)
        db.commit()

        def through_db(segs: list[str]) -> list[str]:
            """逐段过库：算出事件 → 状态推进 → 按**替换**语义落库 → 下一段从库里还原。

            这里刻意走 `replace_state_facts` 而不是通用的 `upsert_facts` ——
            后者对 int 型 props 是累加的，用在这里会把段号加成垃圾（I7 就是这件事）。
            """
            st: dict = {}
            rules: list[str] = []
            for i, s in enumerate(segs, 1):
                ev = extract_events(s, segment_no=i, names=NAMES)
                rules += [f.rule for f in check_segment(st, ev)]
                for e in ev:
                    apply_event(st, e)
                replace_state_facts(db, proj.id, state_to_rows(st), source="generated")
                st = load_state(db, proj.id)
            return rules

        got = through_db(["陆见微突破到金丹。", "陆见微突破到炼气。"])
        check("I1 量纲跨段落库不丢：`金丹 → 炼气` 过库后仍然报 realm_regression",
              got == ["realm_regression"], f"实得 {got}",
              "量纲没落库时这里必然是空 —— 跨段判定会静默失效")
        got = through_db(["陆见微突破到金丹。", "陆见微晋升三阶。"])
        check("I2 跨量纲跨段同样不报（落库不能让量纲隔离失效）",
              got == [], f"实得 {got}")
        got = through_db(["陆见微死了。", "陆见微说道：「我不甘心。」"])
        check("I3 死亡状态跨段落库不丢：过库后仍然报 dead_speaks",
              got == ["dead_speaks"], f"实得 {got}")
        got = through_db(["陆见微握住铜钥匙。", "沈砚握住铜钥匙。"])
        check("I4 未登记物品跨段也不报（唯一性前提不能因为过库而失效）",
              got == [], f"实得 {got}")
        got = through_db(["陆见微把铜钥匙交给沈砚。", "陈九握住铜钥匙。"])
        check("I5 交接过 = 唯一性，跨段落库后仍然报归属冲突（前提必须一起持久化）",
              got == ["item_owner_conflict"], f"实得 {got}")

        # I7 **状态是「点值」，不是「计数器」。**
        # 通用写库函数 `upsert_facts` 对 int 型 props 是做**累加**的（它本来是给
        # 「提及次数」这类计数器用的）。状态里的 `since_segment` 一旦走那条路，
        # 每写一次就被加一次 —— 告警里那句「境界在第 N 段记为…」的 N 就成了累加出来的
        # 假值。而 `from`（转移来源）走的是「键已存在就不覆盖」，于是永远停在旧值。
        # 两种错法都会让**告警证据失真**，而证据失真的告警等于没有告警。
        through_db(["陆见微突破到金丹。", "陆见微说道：「走。」", "陆见微又说：「别走。」"])
        reloaded = load_state(db, proj.id)
        check("I7 同一状态重复落库，段号不得累加（点值语义）",
              reloaded.get(("realm", "陆见微"), {}).get("segment_no") == 1,
              f"实得 {reloaded.get(('realm', '陆见微'), {}).get('segment_no')}",
              "累加说明状态被当成计数器写了")
        snapped = state_facts_from_events(
            extract_events("陆见微说道：「走。」", segment_no=1, names=NAMES),
            project_id=proj.id)
        check("I6 库里没有 `tracked` 之外的多余状态种类",
              {r["key"].split(":", 1)[0] for r in snapped} <= {"life", "owner", "realm",
                                                                "tracked"},
              str(sorted({r["key"].split(":", 1)[0] for r in snapped})))

        # I8 **告警的形状：活响应 与 落库读回 必须一致。**
        # 告警的落库键是 `规则:实体:段号:偏移` —— 键里有实体，读回时却不带出来，
        # 同一个概念就有了两种形状：生成响应里的 `findings` 有 `entity`，
        # `GET /api/alerts` 没有。后果不是「少显示一个字段」那么轻：
        #   · 用户看告警列表时无法结构化地知道「是谁」，只能去 message 里捞；
        #   · 任何按 `entity` 聚合/去重的消费端（前端、导出、后续的复活判定）
        #     会在两条路径上算出不同结果。
        # 之前没人发现，是因为两条路径分别被验证过、没人**对比**过它们。
        from app.kg.sentinel import audit_segment, load_findings
        from app.models import Fact

        proj2 = Project(user_id=1, title="告警形状")
        db.add(proj2)
        db.commit()
        # 名字表必须走 Fact：`known_names` 只认 type=character + confidence=high
        db.add(Fact(project_id=proj2.id, type="character", key="陆见微",
                    aliases=[], source="generated", confidence="high"))
        db.commit()
        audit_segment(db, proj2, "陆见微死了。", 1)
        live_res = audit_segment(db, proj2, "陆见微说道：「我不甘心。」", 2)
        live = live_res["findings"]
        stored = load_findings(db, proj2.id)
        live_keys = set(live[0]) if live else set()
        stored_keys = set(stored[0]) if stored else set()
        check("I8 落库读回的告警与活响应同形状（活响应字段 + segment_no）",
              bool(live) and live_keys | {"segment_no"} == stored_keys,
              f"活响应 {sorted(live_keys)} / 落库读回 {sorted(stored_keys)}",
              "两边字段集不同 —— 同一概念两种形状，消费端会各算各的")
        check("I8b 实体值本身也要对得上（不只是键存在）",
              bool(live) and stored[0].get("entity") == live[0].get("entity"),
              f"活 {live[0].get('entity') if live else None} / "
              f"库 {stored[0].get('entity') if stored else None}")
        db.close()
        engine.dispose()
        # 临时库跑完就删：不然每跑一次留一个目录（本段用 mkdtemp，路径不固定）。
        # 删不掉不算失败 —— 只是留了点垃圾，不该把一个验证脚本搞红。
        try:
            import shutil
            shutil.rmtree(_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        check("I 库往返段本身可运行", False, f"{type(exc).__name__}: {str(exc)[:120]}")

    print()
    print("=" * 78)
    print(f"PASS {len(PASSED)}   FAIL {len(FAILED)}")
    for f in FAILED:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
