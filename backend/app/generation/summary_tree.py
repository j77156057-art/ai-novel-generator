"""层级摘要树：把**无界**的段级摘要链压成**有界**的前情块（步骤 2）。

## 为什么要做

步骤 2 前置测量（`verify_summary_chain.py`，可复跑）给出的实测曲线：

    段数 N |  摘要60字档   占比（前情块 / 整条 user prompt）
      30 |      1,940     88%
     300 |     19,691     99%
    与正文比：N=300 时前情块 19,691 字 vs 单次生成 3,000 字 = 656%

也就是说：**当前实现是全量拼接、无上界**，越写前情越挤，最后 prompt 的绝大部分
都是「前情」而不是「指令 + 素材」。

同时要分清两件常被混为一谈的事（测量脚本已把结论钉住）：

- **尺寸问题**：前情块线性无界 → 由本模块解决（层级 + 预算）。
- **留存问题**：第 1 段埋的事实到第 N 段还在不在 → **不由摘要链解决**，由事实账本
  （`kg/ledger.py`，无损可累积）承担。摘要链是**有损**的，越滚越糊，指望它「别忘事」
  是方向的错误。

所以本模块的目标是**控预算 + 保叙事连贯**，不是「让摘要链别忘事实」。

## 结构（三层）

    L1 段摘要   每段 1 条（provider.summarize，~60 字）—— 已有的那层
    L2 卷摘要   每 VOLUME_SIZE(8) 段 1 条 —— 由该卷的 L1 滚上来
    L3 主线骨架 全篇 1 条               —— 由已出的 L2 滚上来

滚上（rollup）是**惰性**的：只有整卷 L1 齐备（「封卷」）才生成该卷 L2；
只有 L2 有新增才推进 L3。每 8 段各多 2 次短摘要调用，成本相对单段 3000 字可忽略。

## 覆盖不变式（可算术验证，不依赖 LLM）

拼前情时**分层切分点必须落在卷边界**上，从而保证：

    已封卷区间 [1, Vs*8]  ∪  近情窗口 [Vs*8+1, N]  ==  [1, N]

**无空洞、无重叠**——这是纯整数运算，`compose_prior` 每次都会回报 `uncovered`（恒空），
可被测试直接断言。L2 因预算被丢**不算空洞**，因为那些段落仍由 L3 覆盖（只是粗一档）；
因此「允许丢」的前提是该卷已被 L3 覆盖，否则**覆盖优先于预算**（宁可超预算也不留洞）。

## 边界纪律

- 纯逻辑、不碰 DB、不碰 LLM：滚上函数由调用方注入（`rollup_fn`），因此可纯单测。
- 所有段号键**一律用字符串**：`Project.summary_tree` 是 JSON 列，过一趟 DB 后
  int 键会变成 str 键。混用会在「内存里跑得好、落库后失效」处炸，故统一从入口归一。
"""

from __future__ import annotations

import hashlib
from typing import Callable


def _sig(text: str) -> str:
    """卷摘要内容指纹（8 位够用：只做自比对，不抗碰撞攻击）。"""
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8]

# 每卷段数。取 8 的依据：与「近情窗口」量级同阶（默认 3）+ 单卷 L2 输入可控
# （8 条 × 60 字 = 480 字，一次短摘要调用够），且 300 段 = 37 卷，L2 总量 ~7k 字。
VOLUME_SIZE = 8

# 前情块预算（字符）。取 1200 的依据：单段正文默认 3000 字，前情:正文 ≈ 0.4；
# 实测 N=30 全量拼接时前情块 1940 字已占整条 user prompt 的 88%，1200 是把
# 「前情」重新压回它该有的配角位置。
DEFAULT_BUDGET_CHARS = 1200

# 至少保留多少条**最近段摘要全文**。近情是连贯性最关键的一层，不能只靠卷摘要。
DEFAULT_RECENT_MIN = 3

# 前情预算相对单段正文长度的比例。0.4 的来历见 DEFAULT_BUDGET_CHARS。
PRIOR_BUDGET_RATIO = 0.4
PRIOR_BUDGET_MIN = 600
PRIOR_BUDGET_MAX = 2400


def budget_for_generation(generation_length: int | None) -> int:
    """单段正文长度 → 前情块预算。策略集中在这一处，避免各调用点各写一套。"""
    try:
        n = int(generation_length or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return DEFAULT_BUDGET_CHARS
    return max(PRIOR_BUDGET_MIN, min(PRIOR_BUDGET_MAX, int(n * PRIOR_BUDGET_RATIO)))

# L1 缺失时的占位。刻意**显式记一条**而不是跳过：跳过会让该卷永远封不了卷
# （sealed 要求整卷齐备），一个空摘要就能把整条 L2/L3 链路永久卡死；
# 记占位则链路继续推进，且缺失量在 report 里可见，不静默。
MISSING_L1 = "（本段摘要缺失）"


# ———————————————————————————— 基础访问 ————————————————————————————


def seg_key(segment_no: int | str) -> str:
    """段号 → 树内键。**唯一归一入口**：JSON 列过 DB 后键必为 str。"""
    return str(int(segment_no))


def empty_tree(volume_size: int = VOLUME_SIZE) -> dict:
    return {
        "l1": {},  # 段号(str) -> 段摘要
        "l2": {},  # 卷号(str) -> {"range": [a, b], "text": ...}
        "l3": {"text": "", "through_volume": 0},  # 主线骨架 + 已覆盖到第几卷
        "params": {"volume_size": int(volume_size)},
    }


def volume_size_of(tree: dict) -> int:
    params = tree.get("params") or {}
    try:
        v = int(params.get("volume_size") or VOLUME_SIZE)
    except (TypeError, ValueError):
        v = VOLUME_SIZE
    return max(1, v)


def volume_range(volume_no: int, volume_size: int) -> tuple[int, int]:
    """卷号（1 起）→ 闭区间 [起段, 止段]。"""
    start = (volume_no - 1) * volume_size + 1
    return start, start + volume_size - 1


def volume_of(segment_no: int, volume_size: int) -> int:
    return (int(segment_no) - 1) // volume_size + 1


def l1_map(tree: dict) -> dict[str, str]:
    return {str(k): str(v) for k, v in (tree.get("l1") or {}).items() if v}


def l1_segment_nos(tree: dict) -> list[int]:
    """已记录的段号，**数值升序**（树里是 str 键，字典序 '10' < '9' 会排错）。"""
    return sorted(int(k) for k in (tree.get("l1") or {}) if str(k).isdigit())


def l1_chain(tree: dict) -> list[str]:
    """L1 链（按段号升序）。用于 RAG 查询词与旧接口的兼容读法。"""
    l1 = tree.get("l1") or {}
    return [str(l1[seg_key(s)]) for s in l1_segment_nos(tree) if l1.get(seg_key(s))]


def record_l1(tree: dict, segment_no: int | str, text: str) -> None:
    """登记一段的 L1。空摘要落 `MISSING_L1` 占位，保证封卷不被一个空串卡死。"""
    tree.setdefault("l1", {})[seg_key(segment_no)] = (str(text).strip() or MISSING_L1)


def tree_from_flat_summaries(
    summaries, volume_size: int = VOLUME_SIZE
) -> dict:
    """把旧的扁平摘要链**就地引导**成树（老项目迁移用）。

    段号按位置推定为 1..N——这正是旧实现写入 `Project.summary` 的顺序。
    故意不生成 L2/L3：滚上要调 LLM，引导必须是无副作用的纯函数；
    生成的洞由下一次 `rollup_volumes` 补上（它按 L1 齐备度自动封卷）。
    """
    tree = empty_tree(volume_size)
    if isinstance(summaries, (list, tuple)):
        for i, s in enumerate(summaries, start=1):
            if s:
                record_l1(tree, i, s)
    return tree


# ———————————————————————————— 封卷 / 滚上 ————————————————————————————


def sealed_volume_count(tree: dict) -> int:
    """已封卷数：只有**整卷 L1 齐备**（每段都有一条）才算封。

    拿半卷去生成卷摘要会得到一个「其实不止讲了这些」的伪摘要，且下次补段后
    还要重算——不如等它齐。因为 `record_l1` 对空摘要落占位，这个条件总能满足。
    """
    l1 = tree.get("l1") or {}
    vsize = volume_size_of(tree)
    v = 0
    while True:
        start, end = volume_range(v + 1, vsize)
        if all(seg_key(s) in l1 for s in range(start, end + 1)):
            v += 1
        else:
            return v


def invalidate_from(tree: dict, segment_no: int) -> dict:
    """废弃受某段影响的 L2/L3（重生成某段后必须调，否则注入的是**旧内容**的摘要）。

    只丢该段所在卷及之后的卷摘要；L3 清空并让 `through_volume=0`，由
    `rollup_volumes` 用幸存下来的 L2 从头重建——**不能**在旧骨架上续滚：
    旧骨架里已经混入了被替换掉的剧情，续滚只会把错的内容带着传下去。
    """
    vsize = volume_size_of(tree)
    vol = volume_of(segment_no, vsize)
    dropped = [k for k in (tree.get("l2") or {}) if int(k) >= vol]
    for k in dropped:
        tree["l2"].pop(k, None)
    tree["l3"] = {"text": "", "through_volume": 0, "sigs": {}}
    return {"dropped_volumes": sorted(int(k) for k in dropped), "from_volume": vol}


def tree_before(tree: dict, segment_no: int) -> dict:
    """截出「只到某段之前」的一棵等价树（重生成某段时组装前情用）。

    重生成的语义是「只看目标段之前的内容」，所以：

    - L1 只留 `段号 < segment_no` 的；
    - L2 只留**整卷都在该段之前**的（跨界那一卷的一半内容属「之后」，不能代表）；
    - L3 若已覆盖到被截掉的卷，说明骨架里混了「之后」的剧情 → 丢弃，
      交给 `rollup_volumes` 用幸存 L2 重建（截断后若出现空卷它也会补）。

    纯函数，不发 LLM；调用方随后自行决定要不要 `rollup_volumes` 补齐。
    """
    vsize = volume_size_of(tree)
    out = empty_tree(vsize)
    for s in l1_segment_nos(tree):
        if s < segment_no:
            record_l1(out, s, (tree.get("l1") or {})[seg_key(s)])
    for k, rec in (tree.get("l2") or {}).items():
        rng = (rec or {}).get("range") or list(volume_range(int(k), vsize))
        if int(rng[1]) < segment_no:
            out["l2"][seg_key(int(k))] = dict(rec)
    # 幸存卷里，能连续覆盖到第几卷 —— 决定 L3 是否还成立
    reach = 0
    while seg_key(reach + 1) in out["l2"]:
        reach += 1
    try:
        through = int(((tree.get("l3") or {}).get("through_volume")) or 0)
    except (TypeError, ValueError):
        through = 0
    if through and through <= reach:
        # 骨架与指纹一并带过：只带文本不带指纹，rollup 会判定漂移并白重建一次
        sigs = {
            str(k): str(v)
            for k, v in ((tree.get("l3") or {}).get("sigs") or {}).items()
        }
        out["l3"] = {
            "text": str((tree.get("l3") or {}).get("text") or ""),
            "through_volume": through,
            "sigs": {k: v for k, v in sigs.items() if int(k) <= through},
        }
    return out


def rollup_volumes(
    tree: dict, rollup_fn: Callable[[list[str], str], str]
) -> dict:
    """把树推进到一致状态：补齐全封卷的 L2，并把 L3 对齐到 L2 条数。

    `rollup_fn(texts, level) -> str`，level ∈ {"volume", "spine"}。由调用方注入
    （生产传 provider 的滚上摘要，测试传纯函数），因此本函数可在无 LLM 下测试。

    **幂等**：已经一致时一次生成调用都不发。返回本次新建的键，供观测与断言。
    收敛规则（唯一一条，避免多套状态机互相打架）：

      - 骨架记的**已覆盖卷指纹**与当前卷摘要对不上（含少卷/多卷/某卷内容被重算）
        → 丢弃骨架、从头重建；
      - 指纹一致且 `through_volume < L2 条数` → 正常增补，在旧骨架上叠新卷。

    为什么用指纹而不是只比条数：条数一样但**内容变了**是最隐蔽的一种陈旧——
    重生成第 3 段后卷 1 摘要被重算，卷数仍是 1，若只比条数就会认为骨架有效，
    于是 prompt 里的主线仍是**改稿前**的剧情。这类洞不会报错，只会让模型
    在后文里悄悄写出已经删掉的情节。指纹把「记得手动失效」变成「不可能漏」。
    """
    created_l2: list[int] = []
    spine_updated = False
    vsize = volume_size_of(tree)
    tree.setdefault("l2", {})
    l2 = tree["l2"]

    for v in range(1, sealed_volume_count(tree) + 1):
        if seg_key(v) in l2:
            continue
        start, end = volume_range(v, vsize)
        l1 = tree.get("l1") or {}
        texts = [str(l1[seg_key(s)]) for s in range(start, end + 1) if l1.get(seg_key(s))]
        if not texts:
            continue
        text = str(rollup_fn(texts, "volume")).strip()
        l2[seg_key(v)] = {"range": [start, end], "text": text, "sig": _sig(text)}
        created_l2.append(v)

    # 补指纹：老树（步骤 2 之前落的树 / 手写的测试树）没有 sig，就地补上
    sigs: dict[str, str] = {}
    for k, rec in l2.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("sig") != _sig(rec.get("text") or ""):
            rec["sig"] = _sig(rec.get("text") or "")

    # —— L3 对齐 ——
    l3 = tree.setdefault("l3", {"text": "", "through_volume": 0})
    # 取**连续前缀**长度而非 max：中间万一缺卷，骨架绝不能跨过空洞去滚，
    # 否则 L3 会声称覆盖了一段它其实没见过内容的区间。
    n_l2 = 0
    while l2.get(seg_key(n_l2 + 1)):
        sigs[seg_key(n_l2 + 1)] = l2[seg_key(n_l2 + 1)].get("sig") or ""
        n_l2 += 1

    try:
        through = int((l3 or {}).get("through_volume") or 0)
    except (TypeError, ValueError):
        through = 0

    prev_sigs = {str(k): str(v) for k, v in ((l3 or {}).get("sigs") or {}).items()}
    spine_text = str((l3 or {}).get("text") or "")
    # 「漂移」= 骨架**自称已覆盖**的那些卷里，有谁的内容变了或没了。
    # 注意只能按 prev_sigs 的键去比，不能反过来遍历当前卷：
    # 比 prev_sigs 多出来的卷是「新封的卷」，那是**增补**而不是漂移——
    # 反过来写会把每次正常封卷都判成漂移，导致骨架每次从头重算（增量白做）。
    # 另外 `text 有但 sigs 空`（升级前落的树）来源不可考，一律当漂移重建一次。
    drift = bool(spine_text) and (
        not prev_sigs or any(sigs.get(k) != v for k, v in prev_sigs.items())
    )

    if spine_text and not drift:
        base, start_from = [spine_text], through + 1
    else:
        base, start_from = [], 1

    new_texts = [
        str((l2.get(seg_key(k)) or {}).get("text") or "")
        for k in range(start_from, n_l2 + 1)
    ]
    new_texts = [t for t in new_texts if t]

    if new_texts:
        merged = str(rollup_fn(base + new_texts, "spine")).strip()
        tree["l3"] = {"text": merged, "through_volume": n_l2, "sigs": dict(sigs)}
        spine_updated = True
    elif n_l2 == 0 and spine_text:
        # 卷全没了 → 骨架必须跟着清空，否则它会继续宣称覆盖一段已不存在的区间
        tree["l3"] = {"text": "", "through_volume": 0, "sigs": {}}
        spine_updated = True

    return {"l2_created": created_l2, "spine_updated": spine_updated, "l2_total": n_l2}


# ———————————————————————————— 拼前情（有界） ————————————————————————————


def _spine_line(tree: dict) -> str | None:
    """骨架行。刻意把「覆盖到哪」写进文本本身（而不是只留在 report 里）：

    这样前情块自带可审计的覆盖声明，外部核对器只读 prompt 文本就能算出有没有洞，
    不必相信本模块自报的 `uncovered`。
    """
    l3 = tree.get("l3") or {}
    text = str(l3.get("text") or "").strip()
    if not text:
        return None
    try:
        through = int(l3.get("through_volume") or 0)
    except (TypeError, ValueError):
        through = 0
    if through > 0:
        return f"[主线·覆盖至卷 {through}（段 1-{through * volume_size_of(tree)}）] {text}"
    return f"[主线] {text}"


def _volume_line(tree: dict, volume_no: int) -> str | None:
    rec = (tree.get("l2") or {}).get(seg_key(volume_no)) or {}
    text = str(rec.get("text") or "").strip()
    if not text:
        return None
    rng = rec.get("range") or list(volume_range(volume_no, volume_size_of(tree)))
    return f"[卷 {volume_no}｜段 {int(rng[0])}-{int(rng[1])}] {text}"


def compose_prior(
    tree: dict,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    recent_min: int = DEFAULT_RECENT_MIN,
) -> tuple[list[str], dict]:
    """按预算拼出有界的前情块，超预算时**按优先级降级**。

    优先级（高 → 低）：主线骨架 L3 ＞ 近情 L1 全文 ＞ 卷情 L2（新卷优先保留）。

    **覆盖不变式（唯一必须成立的）**：`已封卷区间 [1, V*vsize] ∪ 近情窗口 [ws, N]` 必须
    连续覆盖 `1..N`，**不允许有洞**。允许两者在 seam 上有一段**有界重叠**（见下）。

    为什么允许重叠：早期版本要求窗口必须落在卷边界上（`ws = vs*vsize + 1`），
    结果窗口长度被卷粒度绑架——实测 N=18 时标称 `recent_min=3`、实际窗口 **10 段**，
    且长度随 `N mod 8` 在 3↔10 之间振荡（同一套参数，不同段数下行为差 3 倍）。
    更糟的是会出现「同一段历史，分层反而比全量逐段更贵」：N=8/9/10 贵 53%/48%/44%。

    现在改为：窗口前沿取 `min(近情下限, 覆盖下限)`，让**跨界那一卷的摘要与窗口重叠**
    （重叠区 = `[ws, V*vsize]`，大小 ≤ recent_min-1 段，且只多花一条卷摘要 ~110 字）。
    代价是几行文本被两个分辨率各讲一次（读起来就是「卷情概述 + 最近细节」，不矛盾），
    换来的是：`recent_min` 名副其实、消除 N mod 8 振荡、每个 N 都省预算。

    实测（`verify_summary_chain.py` D 段）：卷层门槛从 11 段降到 **8 段**，
    且 N=18 从 −2% 变成 −44%。

    **降级链（越靠后越不省预算，但绝不丢信息）**：
        有界分层  →  粗层缺失时退化全量    （report["degraded"]）
    「全量」那一档只在老项目首次生成（有 L1 却没滚出 L2/L3）或某卷 L2 为空时触发，
    下一次 rollup 补齐后自动回到有界档。宁可吃预算也不留洞——洞是看不见的失败。

    返回 (lines, report)。report["uncovered"] **恒为空列表**：这是本函数的对外承诺，
    由「卷边界切分 + 粗层缺失退全量」两条机制共同保证，可被外部算子独立复核。
    """
    l1 = tree.get("l1") or {}
    segs = l1_segment_nos(tree)
    report: dict = {
        "n_segments": 0, "sealed_volumes": 0, "window": None,
        "l2_rendered": [], "l2_dropped": [], "uncovered": [],
        "chars": 0, "budget_chars": int(budget_chars), "over_budget": False,
        "missing_l1": 0, "spine": False, "degraded": False, "flat_segments": 0,
    }
    if not segs:
        return [], report

    n_seg = segs[-1]
    vsize = volume_size_of(tree)
    sealed = sealed_volume_count(tree)
    recent_min = max(1, int(recent_min))

    # —— 窗口前沿 ——
    # 目标：窗口**恰好约等于 recent_min 段**（这个旋钮必须名副其实），同时不给覆盖留洞。
    #   ws ≤ n_seg - recent_min + 1   ← 近情下限（不能再往新处退，否则不足 recent_min 段）
    #   ws ≤ sealed * vsize + 1       ← 覆盖下限（再往新处退，[已封卷尾, ws-1] 就没人覆盖了）
    # 两者取小即是唯一解。
    window_start = max(1, min(n_seg - recent_min + 1, sealed * vsize + 1))
    # 卷摘要渲染到「窗口起点所在的那一卷」（含）。允许它与窗口**有一段已知重叠**：
    # 那一卷的摘要覆盖 [.., V*vsize]，窗口从 ws ≥ (V-1)*vsize+1 开始，两者在 [ws, V*vsize] 上重合。
    vs = max(0, min(sealed, volume_of(window_start, vsize)))
    sealed_end = vs * vsize

    report.update(
        n_segments=n_seg,
        sealed_volumes=sealed,
        missing_l1=sum(1 for s in segs if l1.get(seg_key(s)) == MISSING_L1),
    )

    lines: list[str] = []
    used = 0

    # 骨架要**有东西可概括**才有意义：只覆盖 1 卷时它与那条卷摘要是同一件事，
    # 白挂 ~110 字纯开销（实测 N=8 时全量逐段 511 字 vs 挂骨架 780 字，贵 53%）。
    # 不渲染时 spine_covers 归 0 → 后续「可因预算丢弃的卷」集合为空，覆盖仍然安全。
    try:
        through = int(((tree.get("l3") or {}).get("through_volume")) or 0)
    except (TypeError, ValueError):
        through = 0
    spine = _spine_line(tree) if through >= 2 else None
    if spine:                                       # 最高优先：永远保留
        lines.append(spine)
        used += len(spine) + 1
        report["spine"] = True

    # L3 覆盖到第几卷 —— 决定某个 L2 能不能因预算被丢（丢了不留洞的前提）
    spine_covers = through if spine else 0

    # —— L2：先给**必留层**（L3 + 近情）留够预算，剩下的才分给卷摘要 ——
    # 顺序不能反：近情是覆盖下限、不会被丢，若先让卷摘要把预算吃满，最后再无条件
    # 追加近情，总量必然超预算（实测 200 段时会到 1574 字 vs 预算 1200）。
    # 所以这里是「预留」而不是「事后补救」。
    recent_lines = [
        f"[近情·段 {s}] {l1[seg_key(s)]}"
        for s in range(window_start, n_seg + 1)
        if l1.get(seg_key(s))
    ]
    reserved = sum(len(x) + 1 for x in recent_lines)

    # 选取「新卷优先」，但**渲染按时间序**：预算不够时该丢的是最老的卷（离当前最远、
    # 且已被 L3 粗覆盖），而读者视角下卷情必须从早到晚读，否则叙事顺序错乱。
    chosen: list[int] = []
    for v in range(vs, 0, -1):
        # 整卷都落在窗口里 → 这条卷摘要是纯冗余（窗口已逐字覆盖它），跳过。
        # 不做这步时 N=8、recent_min=8 会出现「窗口 [1,8] + 卷1摘要 [1,8]」——
        # 两处把同一卷各讲一遍，白花 ~110 字，重叠也顶到整卷。
        if (v - 1) * vsize + 1 >= window_start:
            continue
        line = _volume_line(tree, v)
        if line is None:
            continue
        cost = len(line) + 1
        if used + reserved + cost <= budget_chars:
            chosen.append(v)
            used += cost
        elif v > spine_covers:
            # 该卷没有被 L3 覆盖 → 丢了就是真空洞 → 覆盖优先于预算，照留并标记
            chosen.append(v)
            used += cost
            report["over_budget"] = True
        else:
            report["l2_dropped"].append(v)
    chosen.sort()

    # —— 覆盖兜底：粗层缺失时，宁可退化成「全量拼接」也**绝不留洞** ——
    # 触发场景：步骤 2 之前建的老项目（有 30 段 L1，却从未滚出 L2/L3）；
    # 或某卷的 L2 因故为空文本。此时按预算裁出来的窗口会让段 1..24 无人覆盖，
    # prompt 里就是一段真空——模型不会报错，只会悄悄忘掉。
    # 旧的全量拼接虽然吃预算，但至少不丢信息，所以退化方向是「有界 → 全量」，
    # 并把窗口前沿推到最早的那个洞，且如实标记 degraded（不静默）。
    chosen_set = set(chosen)
    first_hole: int | None = None
    for v in range(spine_covers + 1, vs + 1):
        if v not in chosen_set:
            first_hole = (v - 1) * vsize + 1
            break
    if first_hole is not None and first_hole < window_start:
        window_start = first_hole
        recent_lines = [
            f"[近情·段 {s}] {l1[seg_key(s)]}"
            for s in range(window_start, n_seg + 1)
            if l1.get(seg_key(s))
        ]
        reserved = sum(len(x) + 1 for x in recent_lines)
        report["degraded"] = True
        report["flat_segments"] = n_seg - window_start + 1

    report["window"] = [window_start, n_seg]
    report["l2_rendered"] = chosen
    for v in chosen:
        line = _volume_line(tree, v)
        if line:
            lines.append(line)

    lines.extend(recent_lines)
    used += reserved

    covered = set(chosen)
    report["uncovered"] = [
        s for s in range(1, sealed_end + 1)
        if volume_of(s, vsize) not in covered and volume_of(s, vsize) > spine_covers
        and s < window_start
    ]
    report["chars"] = used
    if used > budget_chars:
        report["over_budget"] = True
    return lines, report
