"""步骤 5 证据：验收指标 · 30 段可复现运行。

==============================================================================
先把「这份跑能证明什么、不能证明什么」写在最前面 —— 免得数字被过度解读。
==============================================================================

本跑用**离线确定性 provider**（`LocalFallbackLLMProvider`，无模型、无网络）。
它验的是**闭环与记忆系统**，不是模型文笔：

  能证（可机检、可复现）：
    · 30 段跑得通、每段都落库、失败不炸
    · 前情注入**有界**、覆盖**不留洞**（不随段数漂移）
    · 账本事实在长跑后仍在注入里（不遗忘）
    · 规则哨兵零误报（不崩）
    · 与上段的重复率、比喻密度、句长离散度（对照真实散文基线）
    · 账本增长 → 提示词膨胀的真实斜率（扩展性问题）

  不能证（本跑一律标「未测」，不编数字）：
    · 生成文笔是否像人写 —— 离线正文是固定模板，测文笔等于测模板
    · 是否漂移（跑题 / 丢主线）—— 需要真模型产出才有意义
    这两项的办法在 `docs/acceptance-metrics.md`「人工盲评」一节，由人来做。

阈值不是拍的：A 段先在**三本真实网文**上测出人类散文的分布，
阈值取该分布的分位数（每一项都打印来源）。

跑法：python verify_acceptance.py [--segments 30]
"""

import argparse
import collections
import os
import re
import sqlite3
import statistics
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_acceptance.db")
for _s in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(_DB + _s):
        try:
            os.remove(_DB + _s)
        except OSError:
            pass

os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401
from app.models import KgNode, Project  # noqa: E402
from app.generation import build_context, generate_next_segment  # noqa: E402
from app.generation.prompts import build_system_prompt  # noqa: E402
from app.llm.provider import LocalFallbackLLMProvider  # noqa: E402
from app.kg.queries import REMINDER_TOP_K  # noqa: E402

NOVEL_DB = os.path.join(HERE, "novel_dev.db")

PASS = 0
FAIL = 0
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "", hint: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
        if detail:
            print(f"         {detail}")
    else:
        FAIL += 1
        _FAILED.append(name)
        print(f"  [FAIL] {name}  ← {detail}")
        if hint:
            print(f"         {hint}")
    return ok


def section(title: str) -> None:
    print()
    print("#" * 78)
    print(f"# {title}")
    print("#" * 78)


# --------------------------------------------------------------------------- #
# 指标实现（纯函数）
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """去掉空白与换行：中文正文统计不该被排版空行影响。"""
    return _WS.sub("", text or "")


def grams(text: str, n: int = 3) -> set[str]:
    t = _clean(text)
    if len(t) < n:
        return set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 比喻是 AI 腔最稳的硬特征之一：真人写得再花，也不会每百字挂一个「像…」
_METAPHOR = re.compile(r"(像[^，。！？；：]{1,12}|如同|宛如|仿佛|彷佛)")


def metaphor_per100(text: str) -> float:
    t = _clean(text)
    if not t:
        return 0.0
    return 100.0 * len(_METAPHOR.findall(t)) / len(t)


_SENT_SPLIT = re.compile(r"[。！？…]+")


def sent_cv(text: str) -> float | None:
    """句长变异系数：AI 腔句子长度往往过于整齐，真人散文起伏更大。

    方向是「越大越像人」——所以阈值是**下界**（不得低于人类 p10）。
    句子太少时返回 None（不参与判定），而不是硬算一个噪音值。
    """
    sents = [s for s in _SENT_SPLIT.split(_clean(text)) if len(s) >= 4]
    if len(sents) < 5:
        return None
    m = statistics.mean(len(s) for s in sents)
    if m <= 0:
        return None
    return statistics.pstdev(len(s) for s in sents) / m


def pctl(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


# --------------------------------------------------------------------------- #
# 种子：一个够长的短篇设定，逐段续写
# --------------------------------------------------------------------------- #

SEED_CHARS = [
    {"name": "林夜", "role": "主角", "trait": "手指会发光；记不清自己是怎么倒下的"},
    {"name": "苏晚", "role": "接线人", "trait": "冷静，话短，先给选择再给解释"},
    {"name": "陆见微", "role": "对手", "trait": "总是笑着说话，目的不明"},
]
SEED_FORES = ["那道金光到底是什么", "陆见微为何认得这枚铜钥匙"]
SEED_REALMS = ["炼气", "筑基", "金丹", "元婴"]

PLOT_BIBLE = {
    "synopsis": "林夜在巷口醒来，霓虹的余光还黏在视网膜上。他记不清自己是怎么倒下的，"
                "只记得胸口那道凉意。他的手指正在发光，像没调好的灯管。",
    "direction": "三卷：巷口 → 接线人 → 锚。第一卷弄清金光来历，第二卷被迫站队。",
    "characters": SEED_CHARS,
    "foreshadowing": SEED_FORES,
    "conflicts": ["林夜要弄清金光的来历", "陆见微在利用他，而他还没察觉"],
    "relationships": [{"from": "林夜", "to": "苏晚", "type": "同路人"}],
    "realm_order": SEED_REALMS,
}


# --------------------------------------------------------------------------- #
# A. 人类散文基线 —— 阈值的来源
# --------------------------------------------------------------------------- #

def human_baseline(chunk_chars: int = 3000) -> dict:
    """在三本真实网文上算同样的三个指标，作为阈值来源。

    切法与 `verify_sentinel.py` H 段一致（3000 字一段），避免两处口径不同
    导致「基线」和「被测对象」其实不是一回事。
    """
    con = sqlite3.connect(NOVEL_DB)
    rows = con.execute(
        "select novel_id, text from novel_chunks order by novel_id, chapter_no, idx"
    ).fetchall()
    con.close()

    by_novel: dict[int, list[str]] = {}
    for nid, t in rows:
        by_novel.setdefault(nid, []).append(t)

    reps: list[float] = []
    metas: list[float] = []
    cvs: list[float] = []
    n_chars = 0
    n_segs = 0
    for parts in by_novel.values():
        text = "".join(parts)
        segs = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]
        n_chars += len(text)
        n_segs += len(segs)
        gs = [grams(s) for s in segs]
        reps.extend(jaccard(a, b) for a, b in zip(gs, gs[1:]))
        for s in segs:
            metas.append(metaphor_per100(s))
            c = sent_cv(s)
            if c is not None:
                cvs.append(c)
    return {
        "novels": len(by_novel), "chars": n_chars, "segs": n_segs,
        "rep": reps, "meta": metas, "cv": cvs,
    }


def section_a(base: dict | None) -> dict | None:
    section("A. 人类散文基线（阈值的来源，不是拍出来的）")
    if base is None:
        print("  开发库里没有 novel_chunks，跳过基线（相关阈值改为不确定）")
        return None

    print(f"  语料：{base['novels']} 本真实网文 / {base['chars']:,} 字 / "
          f"{base['segs']} 段（每段 3000 字，切法与 verify_sentinel H 段一致）")
    print(f"  相邻段 3-gram Jaccard：中位 {pctl(base['rep'], 0.5):.4f}  "
          f"p90 {pctl(base['rep'], 0.9):.4f}  (n={len(base['rep'])})")
    print(f"  比喻密度 / 百字：中位 {pctl(base['meta'], 0.5):.4f}  "
          f"p90 {pctl(base['meta'], 0.9):.4f}  (n={len(base['meta'])})")
    print(f"  句长变异系数：中位 {pctl(base['cv'], 0.5):.4f}  "
          f"p10 {pctl(base['cv'], 0.1):.4f}  (n={len(base['cv'])})")

    thr = {
        "rep_max": pctl(base["rep"], 0.9),
        "meta_max": pctl(base["meta"], 0.9),
        "cv_min": pctl(base["cv"], 0.1),
    }
    print(f"  → 阈值：重复率 ≤ {thr['rep_max']:.4f}（人类 p90）｜"
          f"比喻密度 ≤ {thr['meta_max']:.4f}（人类 p90）｜"
          f"句长CV ≥ {thr['cv_min']:.4f}（人类 p10）")
    check("A1 人类基线可用（三项指标都有足够样本）",
          len(base["rep"]) >= 100 and len(base["meta"]) >= 100 and len(base["cv"]) >= 100,
          f"rep={len(base['rep'])} meta={len(base['meta'])} cv={len(base['cv'])}")
    check("A2 基线自身合理：真人相邻段重复率远低于 1（否则说明切分口径错了）",
          pctl(base["rep"], 0.5) < 0.05,
          f"中位 {pctl(base['rep'], 0.5):.4f}")
    return thr


# --------------------------------------------------------------------------- #
# B. 30 段闭环
# --------------------------------------------------------------------------- #

def section_b(segments: int, thr: dict | None) -> dict:
    section(f"B. {segments} 段闭环（离线确定性 provider）")

    Base.metadata.create_all(engine)
    db = SessionLocal()
    proj = Project(
        title="验收项目", target_length=3000, generation_length=1200,
        genre="都市异能", premise=PLOT_BIBLE["synopsis"], plot_bible=PLOT_BIBLE,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)

    provider = LocalFallbackLLMProvider()
    rows: list[dict] = []
    prev_g: set = set()
    err = ""
    print(f"  项目 id={proj.id}　库={_DB}")
    print()
    print(f"  {'段':>3} | {'正文字':>6} | {'前情/预算':>12} | {'覆盖':>9} | "
          f"{'角色':>4} | {'哨兵':>4} | {'重复':>6} | {'比喻':>6} | {'句CV':>5}")
    print("  " + "-" * 84)

    try:
        for i in range(1, segments + 1):
            # 测「本段真正拿到的那份上下文」：必须在生成**之前**取
            ctx = build_context(db, proj)
            rep = ctx["prior_report"]
            cons = ctx["constraints"]

            res = generate_next_segment(db, proj, provider=provider, max_chars=1200)
            txt = res["text"] or ""
            g = grams(txt)
            findings = (res.get("kg_update", {}) or {}).get("sentinel", {}) or {}
            n_err = sum(1 for f in findings.get("findings", [])
                        if f.get("severity") == "err")

            cv = sent_cv(txt)
            row = {
                "seg": i, "chars": len(_clean(txt)),
                "prior": rep.get("chars", 0), "budget": rep.get("budget_chars", 0),
                "over_flag": bool(rep.get("over_budget")),
                "uncovered": len(rep.get("uncovered") or []),
                "n_segments": rep.get("n_segments", 0),
                "n_chars": len(cons.get("characters") or []),
                "sent_err": n_err,
                "sent_all": len(findings.get("findings", [])),
                "rep": jaccard(prev_g, g) if prev_g else 0.0,
                "meta": metaphor_per100(txt),
                "cv": cv,
                "text_head": _clean(txt)[:40],
            }
            rows.append(row)
            prev_g = g
            show = f"{cv:.3f}" if cv is not None else "  —  "
            print(f"  {i:>3} | {row['chars']:>6} | {row['prior']:>5}/{row['budget']:<6} | "
                  f"{'1-' + str(row['n_segments']):>9} | {row['n_chars']:>4} | "
                  f"{n_err:>4} | {row['rep']:>6.3f} | {row['meta']:>6.2f} | {show:>5}")
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:160]}"
        print(f"  第 {len(rows) + 1} 段炸了：{err}")

    print("  " + "-" * 84)

    ok_all = len(rows) == segments and not err
    check(f"B1 {segments} 段全部跑完且落库", ok_all,
          err or f"完成 {len(rows)}/{segments} 段",
          "闭环跑不通 = 后面所有指标都无从谈起")

    # 落库核对：段数必须对得上（跑完 ≠ 存下来）
    with SessionLocal() as db2:
        n_gen = db2.query(models.Generation).filter(
            models.Generation.project_id == proj.id).count()
    check("B2 落库段数与生成段数一致", n_gen == len(rows),
          f"生成 {len(rows)} / 落库 {n_gen}")

    if not rows:
        db.close()
        return {"rows": rows, "err": err, "project_id": proj.id}

    # —— 前情注入：预算是**软目标**，核心不变量是「不随段数漂移」——
    #
    # 契约先说清：`compose_prior` 明确写着「覆盖优先于预算，照留并标记」——
    # 丢一段就会在上下文里留一个永久的洞（模型不报错，只会悄悄忘掉），
    # 所以它宁可超出预算并置 `over_budget=True`。
    # 因此「从不超预算」是**一条错断言**：它要求一个系统刻意不提供的保证。
    # （这条断言初版就是这么写的，实测红了才看清 —— 保留这段说明，免得后人再写一遍。）
    #
    # 真正该守的两件事：
    #   1. **不漂移**：峰值必须饱和，不能随段数一路涨（engine.build_context 的原话）；
    #   2. **信号诚实**：超预算这件事必须被如实上报，不能静默。
    vsize = 8  # = summary_tree.VOLUME_SIZE，卷是压缩的基本单位
    peaks = [max(r["prior"] for r in rows[i:i + vsize])
             for i in range(0, len(rows), vsize)]
    warm = 2  # 预热：前两卷还在建 L2/L3，本来就会涨，不算漂移
    steady = peaks[warm:]
    drift = [(i + warm, a, b) for i, (a, b) in enumerate(zip(steady, steady[1:])) if b > a]
    budget = rows[-1]["budget"]
    peak_max = max(peaks) if peaks else 0
    over_n = sum(1 for r in rows if r["budget"] and r["prior"] > r["budget"])

    check("B3a 前情峰值**不随段数漂移**（预热 2 卷后必须饱和）", not drift,
          f"逐 8 段窗口峰值 {peaks}；最大 {peak_max}（预算 {budget}）；"
          f"稳态比较 {max(0, len(steady) - 1)} 次",
          f"递增的窗口：{drift[:5]} —— 涨而不停就是 review 说的『越写越挤』")
    check("B3b 前情峰值有硬上界（≤ 2× 预算，超出即说明真的失控）",
          peak_max <= 2 * budget if budget else True,
          f"实测 {peak_max / budget:.2f}× 预算" if budget else "无预算",
          "软预算允许超支，但**不允许无界**：这条是防止将来某次改动让它开始漂移")
    bad_flag = [r["seg"] for r in rows
                if r["budget"] and r["over_flag"] != (r["prior"] > r["budget"])]
    check("B3c `over_budget` 信号诚实（超了必须报，没超不许报）", not bad_flag,
          f"信号与实际不符的段：{bad_flag[:6]}",
          "信号说谎比超支本身更糟：用户会以为预算没被突破")
    if over_n:
        print(f"         〔成本〕{over_n}/{len(rows)} 段软超预算，"
              f"峰值 {peak_max / budget:.2f}× —— 这是「不留洞」的既定代价，"
              f"已在前端超标记处可见，此处只作计量不作失败")

    # —— 前情覆盖不留洞 ——
    holes = [r for r in rows if r["uncovered"]]
    covered = [r["n_segments"] for r in rows]
    check("B4 前情覆盖不留洞（uncovered 恒空）", not holes,
          f"uncovered 非空的段：{[r['seg'] for r in holes[:5]]}",
          "留洞 = 某几段的剧情永远不会进入后续上下文，等于被遗忘")
    check("B5 覆盖区间随段数单调推进（1..N-1 全在）",
          covered == sorted(covered) and covered[-1] == segments - 1,
          f"覆盖末值 {covered[-1] if covered else None}（应为 {segments - 1}）")

    # —— 不崩：哨兵零误报 ——
    total_err = sum(r["sent_err"] for r in rows)
    total_all = sum(r["sent_all"] for r in rows)
    check("B6 规则哨兵零误报（离线模板正文不该触发硬冲突）", total_err == 0,
          f"err {total_err} / 全部告警 {total_all}",
          f"触发的段：{[r['seg'] for r in rows if r['sent_err']][:8]}")

    # —— 账本不丢种子事实 ——
    last = rows[-1]
    check("B7 长跑后种子事实仍在注入里（角色）",
          last["n_chars"] >= len(SEED_CHARS),
          f"末段注入角色数 {last['n_chars']}（种子 {len(SEED_CHARS)}）",
          "角色从注入里消失 = 模型看不到它，等于人设丢失")

    return {"rows": rows, "err": err, "project_id": proj.id,
            "peaks": peaks, "peak_max": peak_max, "budget": budget,
            "drift": drift, "over_n": over_n}


# --------------------------------------------------------------------------- #
# C. 账本增长 → 提示词膨胀（扩展性）
# --------------------------------------------------------------------------- #

def section_c() -> dict:
    """把「账本无界增长会怎样」量成真实斜率，而不是推断。

    做法：拿一本真实网文逐段跑抽取，**累计去重**的角色名就是真实长跑后账本里的角色数；
    把这些名字按检查点灌进一个探针项目，量出每次 system 提示的真实长度。
    """
    section("C. 账本增长 → 提示词膨胀（真实语料上的累积实测）")

    try:
        from app.kg.build import extract_from_text
        con = sqlite3.connect(NOVEL_DB)
        rows = con.execute(
            "select nc.novel_id, rn.title, nc.text from novel_chunks nc "
            "join raw_novels rn on rn.id = nc.novel_id "
            "order by nc.novel_id, nc.chapter_no, nc.idx"
        ).fetchall()
        con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过：{exc}")
        return {}

    if not rows:
        print("  章节库为空，跳过（不作 PASS/FAIL 计）")
        return {}

    first_novel = rows[0][0]
    book = [r for r in rows if r[0] == first_novel]
    title = book[0][1]
    text = "".join(r[2] for r in book)
    segs = [text[i:i + 3000] for i in range(0, len(text), 3000)]

    # 一趟抽完，同时得到「累计曲线」与「按首次出现排序的名字表」——
    # 抽两遍只是把同一份计算做两次，没有第二个信息量。
    cumulative: set[str] = set()
    names: list[str] = []
    curve: list[tuple[int, int]] = []
    for i, s in enumerate(segs, 1):
        try:
            result, _ = extract_from_text(s, segment_no=i)
            for n in result.characters or []:
                if n not in cumulative:
                    cumulative.add(n)
                    names.append(n)
        except Exception:  # noqa: BLE001 - 单段抽取失败不该毁掉整条曲线
            pass
        if i % 20 == 0:
            curve.append((i, len(cumulative)))

    print(f"  语料：《{title}》{len(text):,} 字 / {len(segs)} 段")
    print("  累计去重角色名：")
    for i, n in curve:
        print(f"    第 {i:>3} 段 → {n:>4} 个")
    if not curve:
        return {}

    last_seg, last_n = curve[-1]
    slope_names = last_n / last_seg  # 个/段

    # 用真实抽到的名字灌进探针项目，量 system 提示
    Base.metadata.create_all(engine)
    db = SessionLocal()
    probe = Project(title="膨胀探针", target_length=3000, generation_length=1200,
                    genre="仙侠", premise="探针。")
    db.add(probe)
    db.commit()
    db.refresh(probe)

    made = 0
    points: list[tuple[int, int, int]] = []  # (角色数, system 字数, 段数当量)
    last_ctx = None
    for target in (10, 30, 60, 100):
        while made < target and made < len(names):
            db.add(KgNode(project_id=probe.id, type="character", source="generated",
                          props={"name": names[made], "mentions": 1},
                          first_chapter=1, last_chapter=1))
            made += 1
        db.commit()
        ctx = build_context(db, probe)
        system = build_system_prompt(probe.title, ctx["reminders"], None,
                                     probe.plot_bible, None)
        points.append((made, len(system), int(made / slope_names) if slope_names else 0))
        last_ctx = ctx

    print()
    print(f"  {'账本角色数':>10} | {'system 提示字数':>14} | {'约等于写到第几段':>16}")
    print("  " + "-" * 50)
    for n, sys_len, seg_eq in points:
        print(f"  {n:>10} | {sys_len:>14,} | {seg_eq:>16}")

    # 封顶前斜率（首两段，均 < K）：清单内部仍线性——但封顶后即安全。
    per_char_within = 0.0
    if len(points) >= 2:
        (n0, s0, _), (n1, s1, _) = points[0], points[1]
        per_char_within = (s1 - s0) / (n1 - n0) if n1 != n0 else 0.0
    # 全段斜率（首→尾）仅作报告：修复后应明显变小（末段已封顶）。
    per_char_full = (points[-1][1] - points[0][1]) / (points[-1][0] - points[0][0]) \
        if len(points) >= 2 and points[-1][0] != points[0][0] else 0.0

    capped_len = len(last_ctx["reminders"]["character_constraints"]) if last_ctx else 0
    print("  " + "-" * 50)
    print(f"  封顶前斜率（{points[0][0]}→{points[1][0]} 个角色，均<K）："
          f"{per_char_within:.1f} 字/角色")
    print(f"  全段斜率（首→尾）：{per_char_full:.1f} 字/角色（封顶后应显著回落）")
    print(f"  末检查点（{points[-1][0]} 个角色）清单实体数 = {capped_len} / K={REMINDER_TOP_K}")

    # 修复必须证「饱和」：账本规模超过 K 后，相邻两个检查点的 system 提示长度增量≈0。
    sat_points = [p for p in points if p[0] > REMINDER_TOP_K]
    sat_delta = sat_points[-1][1] - sat_points[-2][1] if len(sat_points) >= 2 else None
    if sat_delta is not None:
        print(f"  封顶后增量（{sat_points[-2][0]}→{sat_points[-1][0]} 个角色）："
              f"{sat_delta:+,} 字（应≈0）")

    check("C1 账本角色数随段数单调增长（真实语料实测）",
          len(curve) >= 2 and curve[-1][1] > curve[0][1],
          f"{curve[0] if curve else None} → {curve[-1] if curve else None}")

    # 旧断言「提示词随账本线性增长（无界）」已被证伪：现在是「有界 + 饱和」。
    # 拆成两条硬契约：清单被硬封顶；超过 K 后提示词不再上升（前情预算裁剪只管
    # prior_blocks，reminders 清单本身的无界增长已在此封死）。
    capped_ok = last_ctx is not None and capped_len <= REMINDER_TOP_K
    check("C2a 角色清单被硬性封顶（实体数 ≤ REMINDER_TOP_K）",
          capped_ok,
          f"末检查点实体数 = {capped_len} / K={REMINDER_TOP_K}")
    if sat_delta is not None:
        check("C2b 账本超过 K 后提示词**饱和**（增量≈0，不再线性增长）",
              sat_delta <= 5,
              f"封顶后增量 = {sat_delta} 字（K={REMINDER_TOP_K}）",
              "前情预算裁剪只管 prior_blocks；reminders 清单本身的无界增长已在此封死")
    else:
        print("  [跳过] C2b：语料角色数不足以跨过 K 证饱和（不计入 PASS/FAIL）")
    db.close()
    return {"per_char_within": per_char_within, "per_char_full": per_char_full,
            "slope_names": slope_names, "points": points, "capped_len": capped_len}


# --------------------------------------------------------------------------- #
# D. 四轴判定
# --------------------------------------------------------------------------- #

def section_d(b: dict, thr: dict | None, c: dict) -> None:
    section("D. 四轴判定（不崩 / 不漂移 / 不遗忘 / 像人写）")

    rows = b.get("rows") or []
    verdicts: list[tuple[str, str, str]] = []

    # —— 不崩 ——
    hard = sum(r["sent_err"] for r in rows)
    verdicts.append((
        "不崩",
        "PASS" if hard == 0 else "FAIL",
        f"哨兵错误级告警 {hard} 条（{len(rows)} 段）；离线正文是模板，本项只证「通道不误报」",
    ))

    # —— 不遗忘 ——
    # 判定口径：**留洞**是不可接受的（信息永久缺失）；**软超预算**不是 ——
    # 它是「覆盖优先」这个刻意取舍的代价，且已被 over_budget 如实上报、前端可见。
    # 把两者混成一条 FAIL 会让「不遗忘」这个轴失去意义（成本问题不该冒充记忆问题）。
    holes = sum(1 for r in rows if r["uncovered"])
    drifted = bool(b.get("drift"))
    forget_ok = bool(rows) and holes == 0 and not drifted
    verdicts.append((
        "不遗忘",
        "PASS" if forget_ok else "FAIL",
        f"前情覆盖留洞 {holes} 段 / 峰值漂移 {'有' if drifted else '无'}"
        f"（峰值 {b.get('peak_max')} 稳定于 {b.get('budget')} 预算之上）；"
        f"账本事实留存见 B7",
    ))

    # —— 成本（单列，不与四轴混）——
    if b.get("over_n"):
        verdicts.append((
            "成本",
            "需人工",
            f"{b['over_n']}/{len(rows)} 段前情软超预算，峰值 "
            f"{b['peak_max'] / b['budget']:.2f}× —— 既定取舍不是 bug，"
            f"但要不要收紧预算（或改压缩层级）是产品决定，不该由脚本替你拍",
        ))

    # —— 不漂移 ——
    verdicts.append((
        "不漂移",
        "需人工",
        "机器只能证「注入侧没退化」（覆盖/预算/约束在不在）；"
        "跑题与丢主线必须看正文 → 见 docs/acceptance-metrics.md 人工盲评",
    ))

    # —— 像人写 ——
    if rows:
        reps = [r["rep"] for r in rows if r["rep"] > 0]
        metas = [r["meta"] for r in rows]
        cvs = [r["cv"] for r in rows if r["cv"] is not None]
        detail = (f"离线模板正文：重复率中位 {pctl(reps, 0.5):.3f}"
                  f"（人类 p90={thr['rep_max']:.4f}）、"
                  f"比喻密度中位 {pctl(metas, 0.5):.2f}、"
                  f"句长CV 中位 {pctl(cvs, 0.5):.3f}"
                  if thr else "无基线")
        verdicts.append((
            "像人写",
            "未测",
            "离线正文是固定模板，测文笔等于测模板。指标已就位（阈值来自人类散文），"
            "但必须喂真模型产出才有意义。" + "　实测参考：" + detail,
        ))
    else:
        verdicts.append(("像人写", "未测", "无产出"))

    for axis, v, why in verdicts:
        mark = {"PASS": "PASS", "FAIL": "FAIL", "未测": "未测", "需人工": "需人工"}[v]
        print(f"  [{mark:>5}] {axis}　{why}")


# --------------------------------------------------------------------------- #
# E. 失败处理矩阵
# --------------------------------------------------------------------------- #

MATRIX = [
    ("不崩", "哨兵 err > 0", "先看 evidence 里的前段原文：是模型真矛盾还是抽取误报；"
                            "误报改规则并**加进 verify_sentinel 的负例**，真矛盾才回退该段重写"),
    ("不崩", "哨兵误报（人判断为误报）", "按「少报优于误报」收紧规则；"
                                        "每一类误报都要落成一条断言，否则下次还会回来"),
    ("不遗忘", "前情覆盖留洞", "**停发**，先修 compose_prior 的覆盖证明——"
                              "留洞=某些剧情永远不进上下文，写下去必然自相矛盾"),
    ("不遗忘", "前情超预算", "调 budget_for_generation 或提高压缩层级；"
                            "不得靠截断正文解决（截断=新的洞）"),
    ("不遗忘", "账本事实掉出注入", "已加界与排序（top-50 / 最近出场+提及数，见 §6）。"
                                  "残留风险＝长尾角色不进 reminders 清单；"
                                  "靠 RAG / KG 检索兜底，需监控是否因此丢主线"),
    ("不漂移", "主线相关度下降（人工盲评）", "先查大纲注入是否被前情挤掉；"
                                            "再查 RAG 检索是否退化成同质召回"),
    ("像人写", "AI 味指标越界", "对照人类基线定位是哪个特征越界（比喻/句长/升华结尾），"
                              "改提示词约束后再测，不做无对照的「感觉更好」"),
    ("性能", "提示词线性膨胀", "**已修**：get_reminders 按最近出场+提及数取 top-50 封顶"
                              "（REMINDER_TOP_K），清单贡献随账本规模饱和而非增长；"
                              "阈值由 C 段实测斜率定，不拍脑袋"),
]


def _pad(s: str, cols: int) -> str:
    """按**显示宽度**补空格：中文算 2 列，否则箭头对不齐。"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in s)
    return s + " " * max(0, cols - w)


def section_e() -> None:
    section("E. 失败处理矩阵（判定红了之后做什么）")
    aw = max(sum(2 if ord(c) > 0x2E7F else 1 for c in m[0]) for m in MATRIX)
    tw = max(sum(2 if ord(c) > 0x2E7F else 1 for c in m[1]) for m in MATRIX)
    for axis, trigger, action in MATRIX:
        print(f"  【{_pad(axis, aw)}】{_pad(trigger, tw)} → {action}")


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=int, default=30, help="连写段数（默认 30）")
    args = ap.parse_args()

    print("=" * 78)
    print("AI 小说生成器 · 验收指标 · 可复现运行")
    print(f"provider：离线确定性（LocalFallbackLLMProvider）　段数：{args.segments}")
    print("=" * 78)
    print("声明：本跑验**闭环与记忆系统**，不验模型文笔 —— 后者需要真模型 + 人工盲评。")

    base = None
    if os.path.exists(NOVEL_DB):
        try:
            base = human_baseline()
        except Exception as exc:  # noqa: BLE001
            print(f"  基线计算失败：{exc}")
    thr = section_a(base)
    b = section_b(args.segments, thr)
    c = section_c()
    section_d(b, thr, c)
    section_e()

    print()
    print("=" * 78)
    print(f"PASS {PASS}   FAIL {FAIL}")
    for f in _FAILED:
        print(f"  FAIL: {f}")
    print("=" * 78)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
