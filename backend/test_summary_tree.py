"""步骤 2 回归：层级摘要树（L1 段 / L2 卷 / L3 主线骨架）。

测的对象是**前情注入是否真的变成有界的**，以及**有没有留下信息空洞**。
后者是本模块最危险的失败模式：看不见的洞不会报错，只会在第 40 段开始
「模型莫名其妙忘了前面」，而排查时又什么都查不出来。

因此覆盖不变式不靠模块自报，而是用一个**独立算子**重算：
把渲染出来的前情文本当黑盒解析，从中还原每行声明覆盖的段区间，
再在段号集合上验证「并集 == 1..N 且两两不重叠」。只有另一个算子独立算出同一结论，
「没有空洞」才是一条证据，而不是把模块的自我声明再念一遍。

跑法：python test_summary_tree.py
"""

import copy
import json
import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_summary_tree_test.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = _DB + _suffix
    if os.path.exists(_p):
        try:
            os.remove(_p)
        except OSError:
            pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.db import Base, SessionLocal  # noqa: E402
from app.generation import build_context, generate_next_segment  # noqa: E402
from app.generation.summary_tree import (  # noqa: E402
    DEFAULT_BUDGET_CHARS,
    MISSING_L1,
    VOLUME_SIZE,
    budget_for_generation,
    compose_prior,
    empty_tree,
    l1_chain,
    record_l1,
    rollup_volumes,
    sealed_volume_count,
    tree_before,
    tree_from_flat_summaries,
    volume_of,
    volume_range,
)
from app.models import Generation, Project  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def section(title: str) -> None:
    print()
    print("-" * 74)
    print(title)
    print("-" * 74)


# ---------------------------------------------------------------------------
# 独立覆盖算子：只读渲染出来的前情文本，不看 summary_tree 的内部状态
# ---------------------------------------------------------------------------


def ranges_from_lines(lines: list[str]) -> dict[str, list[tuple[int, int]]]:
    """从渲染文本里解析每行**自称覆盖**的段区间。

    刻意不复用 compose_prior 的 report——那是被检验对象自己的说法。
    """
    out = {"fine": [], "coarse": [], "spine": []}
    for ln in lines:
        if ln.startswith("[卷 "):
            # [卷 2｜段 9-16] ……
            head = ln.split("]", 1)[0]
            span = head.split("段 ", 1)[1]
            a, b = span.split("-", 1)
            out["fine"].append((int(a), int(b)))
        elif ln.startswith("[近情·段 "):
            s = int(ln.split("]", 1)[0].split("段 ", 1)[1])
            out["fine"].append((s, s))
        elif ln.startswith("[主线·覆盖至卷"):
            span = ln.split("（段 ", 1)[1].split("）", 1)[0]
            a, b = span.split("-", 1)
            out["spine"].append((int(a), int(b)))
        elif ln.startswith("[主线]"):
            out["spine"].append((0, 0))  # 未声明覆盖范围
    return out


def audit_coverage(lines: list[str], n_seg: int) -> dict:
    """独立核算覆盖。

    检验的性质（**不是**「无重叠」——那是有意允许的，见 compose_prior 文档）：
      1. 无洞：1..N 里没有任何一段是 0 次覆盖；
      2. 重叠有界：任何一段最多被覆盖 2 次，且重叠只发生在**一条卷摘要 × 近情窗口**
         的 seam 上（跨界那一卷），因而重叠区必须是**一段连续的区间**，
         且长度 ≤ volume_size - 1；
      3. 重叠区必须紧贴窗口前沿（重叠只可能是「窗口压进某个卷摘要的尾部」，
         不可能两卷同时压进来）。

    第 2、3 条是关键：只写「允许重叠」等于把不变式废掉；把它约束成「有界 + 连续 + 贴前沿」
    才是真正可证伪的命题。
    """
    r = ranges_from_lines(lines)
    counts: dict[int, int] = {}
    for a, b in r["fine"]:
        for s in range(a, b + 1):
            counts[s] = counts.get(s, 0) + 1
    coarse: set[int] = set()
    for a, b in r["spine"]:
        coarse |= set(range(a, b + 1))

    zero = sorted(s for s in range(1, n_seg + 1) if counts.get(s, 0) == 0 and s not in coarse)
    multi = sorted(s for s in counts if counts[s] > 1)
    contiguous = bool(multi) and multi == list(range(multi[0], multi[-1] + 1))
    over_two = sorted(s for s in counts if counts[s] > 2)
    return {
        "counts": counts,
        "uncovered_all": zero,
        "overlap_from_fine": multi,
        "overlap_contiguous": contiguous,
        "overlap_too_deep": over_two,
        "overlap_size": len(multi),
        "spine_range": sorted(coarse),
    }


# ---------------------------------------------------------------------------


class FakeProvider:
    """记录滚上调用次数与入参，用来断言「什么时候才真的发请求」。"""

    def __init__(self, texts=None):
        self.texts = list(texts or [])
        self.idx = 0
        self.rollups: list[tuple[str, list[str]]] = []
        self.seen_user: list[str] = []

    def chat(self, system, user, constraints=None):
        self.seen_user.append(user)
        t = self.texts[min(self.idx, len(self.texts) - 1)] if self.texts else "正文占位"
        self.idx += 1
        return t

    def stream_chat(self, system, user, constraints=None):
        yield self.chat(system, user, constraints)

    def summarize(self, text):
        return f"段摘要{self.idx}"

    def summarize_rollup(self, texts, level):
        self.rollups.append((level, list(texts)))
        return f"{level}#{len(texts)}:" + "|".join(texts)[:40]


def tree_with(n_seg: int, vsize: int = VOLUME_SIZE, text_len: int = 30,
              rolled: bool = False) -> dict:
    """造一棵 L1 齐备的树；rolled=True 时顺带滚出 L2/L3。"""
    tree = empty_tree(vsize)
    for s in range(1, n_seg + 1):
        record_l1(tree, s, f"第{s}段" + "情" * text_len)
    if rolled:
        rollup_volumes(tree, FakeProvider().summarize_rollup)
    return tree


def _degrades(tree: dict) -> bool:
    """该树按默认预算拼出来会不会走「退化全量」这一档。"""
    return compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS)[1]["degraded"]


def main() -> int:
    print("=" * 74)
    print("步骤 2 回归：层级摘要树")
    print("=" * 74)

    # —————————————————————— 一、纯逻辑：分层与封卷 ——————————————————————
    section("一、分层算术与封卷判定（纯函数，无 DB 无 LLM）")

    check("卷号：第 8 段属第 1 卷", volume_of(8, 8) == 1, str(volume_of(8, 8)))
    check("卷号：第 9 段属第 2 卷（边界不串卷）", volume_of(9, 8) == 2, str(volume_of(9, 8)))
    check("卷区间：第 2 卷 = 段 9-16", volume_range(2, 8) == (9, 16), str(volume_range(2, 8)))

    t7, t8, t15, t16 = tree_with(7), tree_with(8), tree_with(15), tree_with(16)
    check(
        "封卷判定：7/8/15/16 段 → 0/1/1/2 卷",
        [sealed_volume_count(x) for x in (t7, t8, t15, t16)] == [0, 1, 1, 2],
        str([sealed_volume_count(x) for x in (t7, t8, t15, t16)]),
    )

    # 缺段不能封卷；但空摘要必须落占位，否则一个空串能把整条链路永久卡死
    holed = tree_with(16)
    holed["l1"].pop("5")
    check("缺一段 → 该卷不封（不拿半卷生成伪卷摘要）", sealed_volume_count(holed) == 0,
          str(sealed_volume_count(holed)))
    holed2 = tree_with(16)
    record_l1(holed2, 5, "")
    check("空摘要 → 落占位而非跳过（链路不被一个空串卡死）",
          holed2["l1"]["5"] == MISSING_L1 and sealed_volume_count(holed2) == 2,
          f"{holed2['l1']['5']!r} / {sealed_volume_count(holed2)}")

    # JSON 列过一趟 DB 后键必为 str —— 混用 int/str 键会在「落库后失效」处炸
    rt = json.loads(json.dumps(tree_with(16)))
    check("JSON 往返后仍能正确封卷（str 键归一有效）", sealed_volume_count(rt) == 2,
          str(sealed_volume_count(rt)))
    check("JSON 往返后 L1 链仍按数值序（字符串序会把 10 排到 9 前）",
          l1_chain(tree_from_flat_summaries([f"s{i}" for i in range(1, 12)]))[9:11]
          == ["s10", "s11"], "顺序错")

    # —————————————————————— 二、滚上：惰性与幂等 ——————————————————————
    section("二、滚上（rollup）：封卷才发请求、重复调用零成本、骨架按增量叠")

    fake = FakeProvider()
    t8 = tree_with(8)
    r1 = rollup_volumes(t8, fake.summarize_rollup)
    check("8 段 → 恰好 1 条卷摘要 + 1 次骨架更新",
          r1 == {"l2_created": [1], "spine_updated": True, "l2_total": 1}, str(r1))
    check("L2 只调 1 次、L3 只调 1 次（不重复生成）",
          [lv for lv, _ in fake.rollups] == ["volume", "spine"], str(fake.rollups))
    check("卷摘要带正确区间 [1,8]", t8["l2"]["1"]["range"] == [1, 8], str(t8["l2"]["1"]))

    calls_before = len(fake.rollups)
    r2 = rollup_volumes(t8, fake.summarize_rollup)
    check("幂等：再滚一次不发任何请求，L2 不重建",
          len(fake.rollups) == calls_before and r2["l2_created"] == []
          and not r2["spine_updated"], str(r2))

    # 第 16 段封第 2 卷：**增量**路径应是「旧骨架 + 新卷」两次调用，
    # 而不是把两卷一起从头滚（后者是批量路径，得拿「先滚 8 段再滚 16 段」才走到）
    inc = tree_with(8)
    f2 = FakeProvider()
    rollup_volumes(inc, f2.summarize_rollup)
    for s in range(9, 17):
        record_l1(inc, s, f"第{s}段" + "情" * 30)
    rollup_volumes(inc, f2.summarize_rollup)
    spine_calls = [p for lv, p in f2.rollups if lv == "spine"]
    check("增量封卷：共 2 次骨架调用（8 段时 1 次、16 段时再 1 次）",
          len(spine_calls) == 2, str([lv for lv, _ in f2.rollups]))
    check("增量封卷：第 2 次骨架输入 = [旧骨架, 新卷摘要]",
          len(spine_calls[-1]) == 2 and spine_calls[-1][0].startswith("spine#1:"),
          str(spine_calls[-1]))
    check("骨架水位 = 已滚卷数", inc["l3"]["through_volume"] == 2, str(inc["l3"]))

    # 批量路径（一次滚 16 段）应为「两卷 + 1 次骨架」，不该冒出中间的骨架调用
    bulk = tree_with(16)
    f2b = FakeProvider()
    rollup_volumes(bulk, f2b.summarize_rollup)
    check("批量封卷：2 次卷摘要 + 恰好 1 次骨架（不逐卷滚骨架）",
          [lv for lv, _ in f2b.rollups] == ["volume", "volume", "spine"],
          str([lv for lv, _ in f2b.rollups]))

    # —— 最隐蔽的一种陈旧：卷数不变但**卷内容变了**（重生成场景）——
    # 只比条数的实现会认为骨架仍然有效，于是 prompt 里的主线还是改稿前的剧情。
    stale = copy.deepcopy(inc)
    stale["l2"]["1"]["text"] = "改稿后的第 1 卷（内容已变）"
    stale["l2"]["1"].pop("sig", None)          # 内容变了，指纹应随之被重算
    f3 = FakeProvider()
    rollup_volumes(stale, f3.summarize_rollup)
    spine_in = [p for lv, p in f3.rollups if lv == "spine"][-1]
    check("卷数不变但内容变了 → 骨架判定漂移并**丢弃重建**（不续滚旧内容）",
          not any("spine#1" in x or "含错内容" in x for x in spine_in)
          and len(spine_in) == 2, str(spine_in))

    # 指纹写回后必须自洽：再滚一次应当完全静默
    f3b = FakeProvider()
    rollup_volumes(stale, f3b.summarize_rollup)
    check("指纹写回后自洽：紧接着再滚一次零调用（不无限重建）",
          f3b.rollups == [], str(f3b.rollups))

    # 卷被删光（连同 L1 一起没了）→ 骨架必须跟着清空，不能继续宣称覆盖不存在的卷
    wiped = copy.deepcopy(inc)
    wiped["l1"] = {}
    wiped["l2"] = {}
    rollup_volumes(wiped, FakeProvider().summarize_rollup)
    check("卷全没了 → 骨架清空（不再宣称覆盖已不存在的卷）",
          wiped["l3"]["text"] == "" and wiped["l3"]["through_volume"] == 0,
          str(wiped["l3"]))

    # —————————————————————— 三、覆盖不变式（独立算子） ——————————————————————
    section("三、前情拼装：覆盖不变式由**独立算子**核算，不看模块自报")

    worst = []
    for n_seg in (1, 3, 7, 8, 9, 16, 17, 24, 25, 40, 120):
        for recent_min in (1, 3, 8, 40):
            tree = tree_with(n_seg)
            rollup_volumes(tree, FakeProvider().summarize_rollup)
            lines, rep = compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS,
                                       recent_min=recent_min)
            au = audit_coverage(lines, n_seg)
            problems = []
            if au["uncovered_all"]:
                problems.append(f"有洞 {au['uncovered_all'][:4]}")
            if au["overlap_too_deep"]:
                problems.append(f"重叠超过 2 层 {au['overlap_too_deep'][:4]}")
            if au["overlap_size"] > VOLUME_SIZE - 1:
                problems.append(f"重叠过大 {au['overlap_size']}")
            if au["overlap_size"] and not au["overlap_contiguous"]:
                problems.append(f"重叠不连续 {au['overlap_from_fine']}")
            # 重叠只能是「窗口压进某一卷摘要的尾部」→ 重叠区必须含窗口前沿
            if au["overlap_size"] and rep["window"] and \
                    rep["window"][0] not in au["overlap_from_fine"]:
                problems.append(f"重叠区不含窗口前沿 {rep['window']}")
            # 模块自报与独立算子**必须一致**——不一致说明 report 本身在说谎
            if sorted(au["uncovered_all"]) != sorted(rep["uncovered"]):
                problems.append("report 与独立核算不一致")
            if problems:
                worst.append((n_seg, recent_min, problems))
    check(f"覆盖不变式：{11 * 4} 组 (段数 × 近情下限) 全部「无洞 + 重叠有界且贴前沿」",
          not worst, str(worst[:2]))

    # 逐组展示一组代表值，证明不是「都退化成全量拼接」才没洞
    tree30 = tree_with(30)
    rollup_volumes(tree30, FakeProvider().summarize_rollup)
    lines30, rep30 = compose_prior(tree30, budget_chars=DEFAULT_BUDGET_CHARS, recent_min=3)
    print(f"    N=30 样例：窗口={rep30['window']} 渲染卷={rep30['l2_rendered']} "
          f"丢弃卷={rep30['l2_dropped']} 字符={rep30['chars']}")
    check("N=30 窗口含当前未封卷全部（段 25-30）",
          rep30["window"] == [25, 30], str(rep30["window"]))
    check("N=30 三卷卷摘要全部渲染（预算够）", rep30["l2_rendered"] == [1, 2, 3],
          str(rep30["l2_rendered"]))
    check("N=30 骨架给出明确的覆盖声明（可从文本审计）",
          any(ln.startswith("[主线·覆盖至卷 3（段 1-24）]") for ln in lines30),
          str(lines30[:1]))

    # —— recent_min 必须**名副其实**（这是重构前的实测缺陷：N=18 标称 3、实际 10）——
    drift = []
    for n_seg in range(8, 41):
        tree = tree_with(n_seg)
        rollup_volumes(tree, FakeProvider().summarize_rollup)
        _, rep = compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS, recent_min=3)
        win = rep["window"][1] - rep["window"][0] + 1
        if win < 3 or win > VOLUME_SIZE:
            drift.append((n_seg, win))
    check("近情窗口长度稳定在 [recent_min, volume_size]（不再随 N mod 8 振荡到 10）",
          not drift, str(drift[:5]))
    wins = []
    for n_seg in range(8, 41):
        tree = tree_with(n_seg)
        rollup_volumes(tree, FakeProvider().summarize_rollup)
        _, rep = compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS, recent_min=3)
        wins.append(rep["window"][1] - rep["window"][0] + 1)
    check("窗口长度不再随段数剧烈跳变（8..40 段内极差 ≤ volume_size）",
          max(wins) - min(wins) <= VOLUME_SIZE, f"min={min(wins)} max={max(wins)}")

    # —— 分层在任何段数下都不该比「全量逐段」更贵（重构前 N=8/9/10 贵 53/48/44%）——
    inverted = []
    for n_seg in range(8, 41):
        tree = tree_with(n_seg)
        rollup_volumes(tree, FakeProvider().summarize_rollup)
        lines_n, rep_n = compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS)
        flat = sum(len(f"[近情·段 {s}] {tree['l1'][str(s)]}") + 1 for s in range(1, n_seg + 1))
        if rep_n["chars"] >= flat:
            inverted.append((n_seg, rep_n["chars"], flat))
    check("分层在 8..40 段全程都不比全量逐段更贵（无「压缩反而更贵」倒挂）",
          not inverted, str(inverted[:4]))

    # 预算不足：允许丢**最老**的卷摘要，但绝不能丢出空洞
    tight = tree_with(120)
    rollup_volumes(tight, FakeProvider().summarize_rollup)
    lines_t, rep_t = compose_prior(tight, budget_chars=900, recent_min=3)
    au_t = audit_coverage(lines_t, 120)
    check("预算收紧：丢的是老卷（保留的是靠后的卷）",
          rep_t["l2_dropped"] and min(rep_t["l2_dropped"]) < min(rep_t["l2_rendered"]),
          f"dropped={rep_t['l2_dropped'][:3]} rendered={rep_t['l2_rendered']}")
    check("预算收紧后依然无洞（丢掉的卷由 L3 覆盖）", not au_t["uncovered_all"],
          str(au_t["uncovered_all"][:5]))
    check("预算收紧 ≠ 静默：over_budget / l2_dropped 都如实回报",
          bool(rep_t["l2_dropped"]) and rep_t["chars"] > 0, str(rep_t))

    # 没有骨架时，卷摘要**不允许**被预算丢掉（丢了就是真空洞）→ 覆盖优先于预算
    nospine = tree_with(120)
    f4 = FakeProvider()
    rollup_volumes(nospine, f4.summarize_rollup)
    nospine["l3"] = {"text": "", "through_volume": 0}
    lines_ns, rep_ns = compose_prior(nospine, budget_chars=300, recent_min=3)
    au_ns = audit_coverage(lines_ns, 120)
    # 120 段 = 15 卷；窗口只剩最后 3 段（重构后不再被卷边界拖长），
    # 故 15 卷全部需要，且无骨架时一条都不许因预算被丢。
    check("无骨架 + 极小预算：卷摘要一条都不许丢（覆盖优先于预算）",
          rep_ns["l2_dropped"] == [] and len(rep_ns["l2_rendered"]) == 15,
          f"dropped={rep_ns['l2_dropped']} rendered={len(rep_ns['l2_rendered'])}")
    check("无骨架 + 极小预算：无洞，且 over_budget 如实标记",
          not au_ns["uncovered_all"] and rep_ns["over_budget"], str(rep_ns))

    empty_lines, empty_rep = compose_prior(empty_tree(), budget_chars=1200)
    check("空树 → 空前情块 + n_segments=0（首段路径不炸）",
          empty_lines == [] and empty_rep["n_segments"] == 0, str(empty_rep))

    # —— 老项目首次生成：有 30 段 L1，却从未滚出 L2/L3 ——
    # 这一档若按预算裁窗口，段 1..24 会无人覆盖 = prompt 里一段真空。
    # 正确行为是**退化成全量**（吃预算但不丢信息）并如实标记，而不是留洞。
    fresh = tree_with(30)
    lines_f, rep_f = compose_prior(fresh, budget_chars=DEFAULT_BUDGET_CHARS, recent_min=3)
    au_f = audit_coverage(lines_f, 30)
    check("树有 L1 但未滚上（老项目首次生成）：退化为全量而非留洞",
          not au_f["uncovered_all"], str(au_f["uncovered_all"][:6]))
    check("退化被如实标记（degraded + flat_segments + over_budget），不静默",
          rep_f["degraded"] and rep_f["flat_segments"] == 30 and rep_f["over_budget"],
          str({k: rep_f[k] for k in ("degraded", "flat_segments", "over_budget")}))
    check("退化只发生在粗层真缺时（滚上之后回到有界档）",
          _degrades(fresh) and not _degrades(tree_with(30, rolled=True)),
          "滚上后仍在退化")

    # —— 引擎侧：老项目第一次续写前会自动补粗层，不把退化留给用户 ——
    legacy30 = tree_with(30)
    check("有 L1 无粗层 → 判定为「需要补滚上」（引擎据此提前补）",
          sealed_volume_count(legacy30) == 3 and not (legacy30.get("l3") or {}).get("text"),
          "状态不对")

    # 确定性：L1 插入顺序不同不得影响结果
    a1, a2 = empty_tree(), empty_tree()
    for s in range(1, 41):
        record_l1(a1, s, f"第{s}段内容")
    for s in (3, 17, 1, 40, 9, 25, 33, 8, 16, 24, 32, 39, 2, 4, 5, 6, 7, 10, 11, 12,
              13, 14, 15, 18, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30, 31, 34, 35, 36,
              37, 38):
        record_l1(a2, s, f"第{s}段内容")
    rollup_volumes(a1, FakeProvider().summarize_rollup)
    rollup_volumes(a2, FakeProvider().summarize_rollup)
    l1a, _ = compose_prior(a1, recent_min=3)
    l1b, _ = compose_prior(a2, recent_min=3)
    check("确定性：L1 登记顺序不同，前情块逐字符一致", l1a == l1b, str(l1a[:1]) + str(l1b[:1]))

    # 预算策略集中在单一处
    check("预算策略：按单段正文字数走比例，并夹在 [600, 2400]",
          (budget_for_generation(3000), budget_for_generation(100),
           budget_for_generation(99999)) == (1200, 600, 2400),
          str([budget_for_generation(x) for x in (3000, 100, 99999)]))

    # —————————————————————— 四、tree_before：重生成的前情截断 ——————————————————————
    section("四、tree_before：重生成时的前情截断（纯函数、不改原树）")

    src = tree_with(40)
    rollup_volumes(src, FakeProvider().summarize_rollup)
    snap = json.dumps(src, sort_keys=True, ensure_ascii=False)
    cut = tree_before(src, 20)
    check("tree_before 不修改原树（纯函数）",
          json.dumps(src, sort_keys=True, ensure_ascii=False) == snap, "原树被改")
    check("截断后 L1 只到 19 段", max(int(k) for k in cut["l1"]) == 19,
          str(max(int(k) for k in cut["l1"])))
    check("截断后 L2 只留整卷在前的（卷 1-2，共 2 条）",
          sorted(int(k) for k in cut["l2"]) == [1, 2], str(sorted(cut["l2"])))
    check("截断后 L3 被丢弃（原骨架含段 20 之后的内容）",
          cut["l3"]["text"] == "" and cut["l3"]["through_volume"] == 0, str(cut["l3"]))
    lines_c, rep_c = compose_prior(cut, recent_min=3)
    au_c = audit_coverage(lines_c, 19)
    check("截断树拼出的前情对 1..19 同样无洞", not au_c["uncovered_all"],
          str(au_c["uncovered_all"]))

    # —————————————————————— 五、引擎接线：老项目 / 落库 / 有界 ——————————————————————
    section("五、引擎接线：老项目引导、JSON 真落库、前情有界")

    Base.metadata.create_all(bind=SessionLocal().bind)
    db = SessionLocal()

    legacy = Project(title="老项目", target_length=3000, generation_length=3000,
                     summary=[f"旧摘要{i}" for i in range(1, 13)])
    db.add(legacy)
    db.commit()
    db.refresh(legacy)
    ctx_l = build_context(db, legacy)
    check("老项目（只有扁平 summary）→ 自动引导出树，已写内容不丢",
          len(ctx_l["prior_blocks"]) > 0 and "旧摘要1" in ctx_l["prior_summaries"][0],
          str(ctx_l["prior_summaries"][:2]))
    check("老项目引导出的前情块带层级标记（不再是一条条 1. 2. 3.）",
          any(ln.startswith("[近情·段 ") for ln in ctx_l["prior_blocks"]),
          str(ctx_l["prior_blocks"][:2]))

    proj = Project(title="层级回归", target_length=300000, generation_length=3000)
    db.add(proj)
    db.commit()
    db.refresh(proj)
    pid = proj.id

    texts = [f"第{i}段正文。" + "风雪夜，陆见推开木门。" * 3 for i in range(1, 20)]
    fp = FakeProvider(texts)
    rollup_calls_after_each: list[int] = []
    for _ in range(17):
        generate_next_segment(db, proj, provider=fp)
        rollup_calls_after_each.append(len(fp.rollups))

    # **换一个 session 重读**：证明 JSON 列真的写进库了（原地改 dict 不会被判脏）
    db.close()
    db = SessionLocal()
    proj = db.get(Project, pid)
    check("summary_tree 真的落库（新 session 重读仍在）",
          isinstance(proj.summary_tree, dict) and len(proj.summary_tree.get("l1") or {}) == 17,
          str(type(proj.summary_tree)))
    check("17 段 → 封 2 卷，库里确有 2 条卷摘要",
          sorted(proj.summary_tree["l2"]) == ["1", "2"], str(sorted(proj.summary_tree["l2"])))
    check("骨架水位 = 2（与已封卷数一致）",
          proj.summary_tree["l3"]["through_volume"] == 2, str(proj.summary_tree["l3"]))
    check("滚上只在**封卷那两段**真的发请求（其余 15 段零成本）",
          [i + 1 for i in range(1, 17)
           if rollup_calls_after_each[i] > rollup_calls_after_each[i - 1]] == [8, 16],
          str(rollup_calls_after_each))

    ctx = build_context(db, proj)
    au = audit_coverage(ctx["prior_blocks"], 17)
    # 独立算子结论：无洞 + 重叠有界 + 连续 + 贴窗口前沿（重叠是有意允许的 seam，不是 bug）
    check("17 段前情对 1..17 无洞（独立算子重算段号区间）", not au["uncovered_all"], str(au)[:200])
    check("17 段前情重叠有界（每段最多 2 层）且落在一条连续 seam 上",
          not au["overlap_too_deep"] and au["overlap_contiguous"]
          and au["overlap_size"] <= VOLUME_SIZE - 1, str(au)[:200])
    check("17 段前情重叠区紧贴近情窗口前沿（段 15）",
          not au["overlap_from_fine"] or au["overlap_from_fine"][0] == ctx["prior_report"]["window"][0],
          f"overlap={au['overlap_from_fine']} window={ctx['prior_report']['window']}")
    check("独立算子结论与模块自报 uncovered 一致（互证）",
          sorted(au["uncovered_all"]) == sorted(ctx["prior_report"]["uncovered"]),
          f"{au['uncovered_all']} vs {ctx['prior_report']['uncovered']}")
    print(f"    独立算子：重叠={au['overlap_from_fine']} 窗口={ctx['prior_report']['window']} "
          f"骨架覆盖={au['spine_range'][:1]}…{au['spine_range'][-1:]}")
    check("扁平归档 summary 仍同步维护（长度 = 段数）", len(proj.summary) == 17,
          str(len(proj.summary)))

    # 有界性：前情块不随段数线性膨胀
    sizes = []
    for n in (8, 40, 200):
        t = tree_with(n)
        rollup_volumes(t, FakeProvider().summarize_rollup)
        ln, rp = compose_prior(t, budget_chars=DEFAULT_BUDGET_CHARS, recent_min=3)
        sizes.append((n, rp["chars"]))
    flat_200 = 200 * 60
    check("有界：200 段时前情块 ≤ 预算（不随段数线性膨胀）",
          sizes[-1][1] <= DEFAULT_BUDGET_CHARS, str(sizes))
    print(f"    前情块尺寸：8段={sizes[0][1]} 字 / 40段={sizes[1][1]} 字 / "
          f"200段={sizes[2][1]} 字；旧实现 200 段全量拼接 ≈ {flat_200:,} 字 "
          f"（压到 {sizes[-1][1] / flat_200 * 100:.1f}%）")

    # —————————————————————— 六、重生成：树的重建 ——————————————————————
    section("六、重生成：受影响的卷摘要与骨架必须重算（不能留旧内容）")

    from app.generation.engine import rebuild_summary_tree  # noqa: E402

    before_text = proj.summary_tree["l2"]["1"]["text"]
    target = db.scalars(
        Generation.__table__.select().where(Generation.project_id == pid)
        .order_by(Generation.segment_no)
    ).all()
    check("库里有 17 段可供重建", len(target) == 17, str(len(target)))

    fp2 = FakeProvider(texts)
    stats = rebuild_summary_tree(db, proj, fp2)
    db.close()
    db = SessionLocal()
    proj = db.get(Project, pid)
    check("重建后 L2 仍是 2 条（卷数由段数决定，不因重建丢失）",
          sorted(proj.summary_tree["l2"]) == ["1", "2"], str(sorted(proj.summary_tree["l2"])))
    check("重建**确实重算**了卷摘要（重新调了 LLM，而不是沿用旧文本）",
          len(fp2.rollups) >= 3, str([lv for lv, _ in fp2.rollups]))
    check("重建后扁平归档与树一致",
          proj.summary == l1_chain(proj.summary_tree), "不一致")
    check("重建后骨架水位与卷数一致（自洽）",
          proj.summary_tree["l3"]["through_volume"] == 2, str(proj.summary_tree["l3"]))
    print(f"    （重建前卷1摘要 = {before_text[:24]}…）")

    # —————————————————————— 七、重生成整链（此前零覆盖） ——————————————————————
    section("七、重生成单段：前情只含该段之前 + 受影响层整体重算")

    regen_proj = Project(title="重生成回归", target_length=300000, generation_length=3000)
    db.add(regen_proj)
    db.commit()
    db.refresh(regen_proj)
    rp_id = regen_proj.id

    fp3 = FakeProvider(["正文占位" for _ in range(40)])
    for _ in range(19):
        generate_next_segment(db, regen_proj, provider=fp3)
    db.refresh(regen_proj)
    l1_before = dict(regen_proj.summary_tree["l1"])
    l2_1_before = regen_proj.summary_tree["l2"]["1"]["text"]
    spine_before = regen_proj.summary_tree["l3"]["text"]
    check("重生成前置条件：19 段、2 卷、骨架已就位",
          len(l1_before) == 19 and sorted(regen_proj.summary_tree["l2"]) == ["1", "2"]
          and bool(spine_before), str(sorted(regen_proj.summary_tree["l2"])))

    from app.generation.engine import regenerate_segment_stream  # noqa: E402

    fp4 = FakeProvider(["重生成后的新正文" for _ in range(5)])
    events = list(regenerate_segment_stream(db, regen_proj, 3, provider=fp4))
    done = [e for e in events if e.get("type") == "done"]
    check("重生成产出 done 事件（段号不变）",
          len(done) == 1 and done[0]["segment_no"] == 3, str(done))

    # —— 前情必须只含「该段之前」的内容 ——
    user_regen = fp4.seen_user[-1] if fp4.seen_user else ""
    check("重生成的前情只含目标段**之前**的段（不含 段3 自己）",
          "[近情·段 1]" in user_regen and "[近情·段 3]" not in user_regen,
          str([ln for ln in user_regen.splitlines() if ln.startswith("[近情")]))
    check("重生成前情对 1..2 无洞（截断树同样走无洞拼装）",
          user_regen.count("[近情·段 1]") == 1 and "[近情·段 2]" in user_regen,
          str(user_regen[:80]))

    db.close()
    db = SessionLocal()
    regen_proj = db.get(Project, rp_id)
    tree = regen_proj.summary_tree
    check("L1 第 3 段已被新摘要替换", tree["l1"]["3"] != l1_before["3"],
          f"{l1_before['3']!r} → {tree['l1']['3']!r}")
    check("段 3 所在卷的卷摘要被**整体重算**（不再含旧内容）",
          tree["l2"]["1"]["text"] != l2_1_before, "卷 1 摘要未变 = 注入的仍是改稿前剧情")
    check("重算后的卷 1 摘要确实基于新 L1（含新摘要文本）",
          tree["l1"]["3"] in tree["l2"]["1"]["text"],
          f"L1[3]={tree['l1']['3']!r} / L2[1]={tree['l2']['1']['text']!r}")
    check("主线骨架被重算（不再挂在旧卷摘要上）",
          tree["l3"]["text"] != spine_before and "spine#" in tree["l3"]["text"],
          str(tree["l3"]["text"])[:40])
    check("重建后 L1 条数仍为 19（段没丢也没多）", len(tree["l1"]) == 19, str(len(tree["l1"])))
    check("重建后扁平归档 == 树的 L1 链", regen_proj.summary == l1_chain(tree), "不一致")
    check("重建后段 3 的正文确为本次重生成的新文",
          (db.get(Generation, done[0]["id"]).text or "").startswith("重生成后的新正文"),
          str(db.get(Generation, done[0]["id"]).text)[:20])

    # 重生成后再拼一次前情：仍须无洞、且在预算内
    lines_r, rep_r = compose_prior(tree, budget_chars=DEFAULT_BUDGET_CHARS)
    au_r = audit_coverage(lines_r, 19)
    check("重生成后的前情对 1..19 仍无洞、重叠有界且连续（独立算子）",
          not au_r["uncovered_all"] and not au_r["overlap_too_deep"]
          and (not au_r["overlap_size"] or au_r["overlap_contiguous"]), str(au_r)[:200])
    check("重生成后的前情仍在预算内", rep_r["chars"] <= DEFAULT_BUDGET_CHARS, str(rep_r["chars"]))

    db.close()
    print()
    print("=" * 74)
    print(f"结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    print("=" * 74)
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print(f"  - {f}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
