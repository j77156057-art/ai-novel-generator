"""真模型端到端验收跑（2026-10-30 新增）。

==============================================================================
先把「这份跑能证明什么、不能证明什么」写在最前面 —— 免得数字被过度解读。
==============================================================================

离线跑（`verify_acceptance.py`）用**离线确定性 provider**，正文是固定模板：
它能证「闭环与记忆系统」，但**证不了文笔**（测文笔等于测模板）。

本跑换**真模型**（默认 opencode Go 订阅通道的 deepseek-v4.1-flash），
把离线跑证不了的两轴补上：

  能证（机器可判）：
    · 真模型跑得通、每段都落库、失败不炸（真实故障率）
    · 正文**不是模板**（段长、非空、重复率、比喻密度、句长离散度）
    · 前情注入有界、不留洞（与离线跑同一把尺）
    · 账本事实长跑后仍在注入里（不遗忘）
    · 规则哨兵在**真正文**上的告警（⚠️ 真正文可能触发真冲突，
      所以这一项只列证据不直接判失败 —— 见 R6 的口径说明）

  仍不能证（必须人来做）：
    · 「像不像人写」的主观判断 → 机器指标只给三个可计算特征，
      主观盲评走 `verify_blind_eval.py`（本脚本会生成盲评包）
    · 「有没有跑题」→ 靠盲评的第 2、3 题与人工复核

跑法：
    python verify_acceptance_real.py --segments 30
    python verify_acceptance_real.py --segments 80 --channel minimax --model MiniMax-M3

产出（默认落在 --out 目录）：
    report.json        逐段指标 + 断言结果（机器可读）
    segments.json      逐段正文（供盲评取样与人工复核）
    blind_samples.md   盲评题面（**只有 A/B，没有标签**）
    blind_key.json     密封映射（谁是 AI 写的）—— **不要和题面一起给人**
    blind_sheet.md     记录表（照 docs/acceptance-metrics.md §5.5 的格式）
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# —— 数据库：单独一份，不与离线跑共用（否则两轮互相覆盖）——
_DB = os.path.join(
    os.environ.get("TEMP", os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp")),
    "ai_novel_acceptance_real.db",
)


def _drop_db() -> None:
    """删掉上一轮的验收库（默认行为；--resume 时保留，才能接着跑）。"""
    for _s in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(_DB + _s):
            try:
                os.remove(_DB + _s)
            except OSError:
                pass

os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"          # 不让 settings 里的渠道自己生效
os.environ.setdefault("ACCEPTANCE_CORPUS", "")

from acceptance_lib import (  # noqa: E402
    PLOT_BIBLE,
    SEED_CHARS,
    baseline_corpus_spec,
    blind_verdict,
    corpus_texts,
    grams,
    human_baseline,
    jaccard,
    mask_density,
    mask_names,
    mask_side,
    metaphor_per100,
    normalize_for_blind,
    pctl,
    residual_proper_nouns,
    sent_cv,
    thresholds,
)

from app.db import Base, SessionLocal, engine  # noqa: E402
from app import models  # noqa: E402,F401
from app.models import Project  # noqa: E402
from app.generation import build_context, generate_next_segment  # noqa: E402
from app.llm import provider as provider_mod  # noqa: E402
from app.llm.provider import GenericOpenAIChatProvider, MiniMaxChatProvider  # noqa: E402


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

#: 题面级专名复查最多补遮几轮。
#:
#: 每一轮都是「读题面 → 列出残留专名 → 补遮 → 再读」。
#: 3 轮还清不干净 ⇒ 说明漏的不是个别词，而是判定链路整体失灵 ⇒ 整包作废。
RESIDUAL_ROUNDS = 3

#: 补遮掉的专名字数占正文的比例上限。
#:
#: 超过它 ⇒ 模型多半把普通词当成了专名（这时候再补遮只会把题面涂花）。
#: 这种包宁可作废，也不能交出去一个被涂花的题面。
RESIDUAL_MAX_RATIO = 0.05

PASS = 0
FAIL = 0
_WARN = 0
_FAILED: list[str] = []
_NOTES: list[str] = []


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


def warn(name: str, detail: str = "", hint: str = "") -> None:
    """只记证据、不判失败的项（需要人来看的）。"""
    global _WARN
    _WARN += 1
    print(f"  [WARN] {name}")
    if detail:
        print(f"         {detail}")
    if hint:
        print(f"         {hint}")


def note(text: str) -> None:
    _NOTES.append(text)
    print(f"  · {text}")


def section(title: str) -> None:
    print()
    print("#" * 78)
    print(f"# {title}")
    print("#" * 78)


# --------------------------------------------------------------------------- #
# 通道装配
# --------------------------------------------------------------------------- #

def read_oc_key() -> str:
    """opencode Go 的 key：环境变量优先，其次用户级环境变量（不打印）。"""
    key = os.environ.get("OC_KEY", "").strip()
    if key:
        return key
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, "OC_KEY")[0]).strip()
    except Exception:  # noqa: BLE001
        return ""


def read_minimax_key() -> str:
    """MiniMax 的 key：环境变量优先，其次冒烟库的 app_settings（不打印）。"""
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if key:
        return key
    db = os.environ.get("SMOKE_DB", "out/smoke_run.db")
    if not os.path.exists(db):
        return ""
    try:
        import sqlite3
        con = sqlite3.connect(db)
        rows = dict(con.execute("select key, value from app_settings").fetchall())
        con.close()
    except Exception:  # noqa: BLE001
        return ""
    for k in ("writer.api_key", "minimax_api_key", "llm.minimax_api_key"):
        v = str(rows.get(k) or "").strip()
        if v:
            return v
    return ""


def build_provider(channel: str, model: str, max_tokens: int, timeout: int, session: str,
                   *, thinking_off: bool = False, install_singleton: bool = True):
    """按渠道名装配 provider，并装成 Writer 单例。

    ⚠️ 装成单例是刻意的：生成链路里有一批**内部**的 LLM 调用
    （角色个性反推、摘要滚上、节点判定、支线滚动、文风改写）走
    `get_llm_provider()`。只把 provider 传进 `generate_next_segment` 的话，
    那些调用会落到 LocalFallback —— 于是「真模型跑」里混着模板正文，
    结论就不成立了。

    ## `thinking_off`：判定专名那条链路要关思考

    逐词判定要回「每个词一个 JSON 对象」，**输出越长越容易被 max_tokens
    拦腰截断**；开了思考时推理块还要吃掉同一份预算 ⇒ 截断概率更高。
    而截断的后果不是「这段判定差一点」，而是**整批判定消失**
    （JSON 解析失败 ⇒ 旧实现把整批判成「不是专名」⇒ 真名留在题面里）。
    所以判定用的 provider 单独装一个、关掉思考。

    `install_singleton=False` ⇒ 不覆盖 Writer 单例（判定 provider 用）。
    """
    if channel == "opencode-go":
        key = read_oc_key()
        if not key:
            raise SystemExit("拿不到 opencode Go 的 key（环境变量 OC_KEY）")
        prov = GenericOpenAIChatProvider(
            key,
            "https://opencode.ai/zen/go/v1",
            model,
            extra_headers={
                "x-opencode-session": session,
                # ⚠️ 浏览器 UA 是必需的：实测裸 urllib 的 UA 会被 Cloudflare 拦成 403
                # （`error code: 1010`）。这不是 opencode 特有，但凡是走
                # Cloudflare 的 OpenAI 兼容端点都可能这样。
                "User-Agent": UA,
            },
            max_tokens=max_tokens,
            timeout=timeout,
            thinking_off=thinking_off,
        )
    elif channel == "minimax":
        key = read_minimax_key()
        if not key:
            raise SystemExit("拿不到 MiniMax 的 key（环境变量 MINIMAX_API_KEY 或冒烟库）")
        prov = MiniMaxChatProvider(
            key, os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1"),
            model, max_tokens=max_tokens, timeout=timeout,
        )
    else:
        raise SystemExit(f"未知渠道：{channel}")
    if install_singleton:
        provider_mod._PROVIDER = prov
    return prov


# --------------------------------------------------------------------------- #
# 基线
# --------------------------------------------------------------------------- #

def load_baseline(max_chars: int | None) -> tuple[dict | None, dict | None]:
    spec = baseline_corpus_spec()
    if not spec or not os.path.exists(spec):
        print(f"  基线语料不存在（{spec or '未设置'}）—— 本轮不设文风阈值")
        return None, None
    texts = corpus_texts(spec, max_chars=max_chars)
    if not texts:
        return None, None
    base = human_baseline(texts, chunk_chars=3000)
    thr = thresholds(base)
    return base, thr


# --------------------------------------------------------------------------- #
# 正文跑
# --------------------------------------------------------------------------- #

def run_segments(segments: int, provider, out_dir: Path, resume: bool) -> tuple[list[dict], int]:
    Base.metadata.create_all(engine)
    db = SessionLocal()
    proj = db.query(Project).order_by(Project.id).first()
    if proj is None:
        proj = Project(
            title="验收项目（真模型）", target_length=3000, generation_length=1200,
            genre="都市异能", premise=PLOT_BIBLE["synopsis"], plot_bible=PLOT_BIBLE,
        )
        db.add(proj)
        db.commit()
        db.refresh(proj)
    print(f"  项目 id={proj.id}　库={_DB}")

    seg_path = out_dir / "segments.json"
    rows: list[dict] = []
    if resume and seg_path.exists():
        try:
            rows = json.loads(seg_path.read_text(encoding="utf-8"))
            print(f"  断点续跑：已有 {len(rows)} 段")
        except Exception:  # noqa: BLE001
            rows = []

    print()
    print(f"  {'段':>3} | {'秒':>5} | {'正文字':>6} | {'前情/预算':>11} | {'覆盖':>8} | "
          f"{'角色':>4} | {'哨兵':>4} | {'重复':>6} | {'比喻':>6} | {'句CV':>5}")
    print("  " + "-" * 84)

    prev_g: set = set()
    for i in range(1, segments + 1):
        if i <= len(rows):
            prev = rows[i - 1]
            if not prev.get("error"):
                prev_g = set(prev.get("_grams") or [])
                continue
            # 上一轮**失败**的那段：重跑（而不是跳过）——
            # 失败段没有落库，故事里就缺这一段。
            #
            # ⚠️ 只**替换这一行**，不截断后面的行：后面那些行在上一轮已经
            #    落过库了（它们是故事的一部分），重跑一遍会让**同一段故事
            #    在库里出现两份**（实测踩过：截断式续跑把已提交的段又生成了一遍，
            #    库里多出重复段落，故事连续性被污染）。
            #    重跑出来的这一段会落在故事**末尾**（段号是按提交顺序给的），
            #    这点如实记在报告里，不假装它是「补在原位」。
            rerun_failed = True
        else:
            rerun_failed = False
        t0 = time.time()
        try:
            ctx = build_context(db, proj)
            rep = ctx["prior_report"]
            cons = ctx["constraints"]
            res = generate_next_segment(db, proj, provider=provider, max_chars=1200)
            txt = res.get("text") or ""
            findings = (res.get("kg_update", {}) or {}).get("sentinel", {}) or {}
            n_err = sum(1 for f in findings.get("findings", [])
                        if f.get("severity") == "err")
            g = grams(txt)
            cv = sent_cv(txt)
            row = {
                "seg": i,
                "segment_no": res.get("segment_no"),
                "seconds": round(time.time() - t0, 1),
                "chars": len(txt.replace(" ", "").replace("\n", "")),
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
                "text": txt,
                "_grams": sorted(g),
                "sent_findings": [f for f in findings.get("findings", []) if f.get("severity") == "err"],
                # —— 诊断：正文是在哪一步丢的 ——
                "diag": {
                    "finish_reason": getattr(provider, "last_finish_reason", None),
                    "length": res.get("length") or {},
                    "form": {k: v for k, v in (res.get("form_info") or {}).items()
                             if k in ("applied", "reason", "before", "after", "cjk_before", "cjk_after")},
                    "rewrite": {k: v for k, v in (res.get("rewrite_info") or {}).items()
                                if k in ("applied", "reason", "changed")},
                    "verbatim": {k: v for k, v in (res.get("verbatim_info") or {}).items()
                                 if k in ("applied", "reason", "hits")},
                },
            }
        except Exception as exc:  # noqa: BLE001
            row = {
                "seg": i, "seconds": round(time.time() - t0, 1), "error": f"{type(exc).__name__}: {exc}",
                "chars": 0, "prior": 0, "budget": 0, "over_flag": False, "uncovered": 0,
                "n_segments": 0, "n_chars": 0, "sent_err": 0, "sent_all": 0,
                "rep": 0.0, "meta": 0.0, "cv": None, "text": "", "_grams": [],
                "sent_findings": [],
            }
        if rerun_failed and len(rows) >= i:
            # 替换掉上一轮那条失败行（不追加 —— 追加会让同一段在报告里出现两次）
            rows[i - 1] = row
        else:
            rows.append(row)
        prev_g = set(row.get("_grams") or [])
        show = f"{row['cv']:.3f}" if row.get("cv") is not None else "  —  "
        err_txt = row.get("error", "")
        print(f"  {i:>3} | {row['seconds']:>5.1f} | {row['chars']:>6} | "
              f"{row['prior']:>5}/{row['budget']:<5} | {'1-' + str(row['n_segments']):>8} | "
              f"{row['n_chars']:>4} | {row['sent_err']:>4} | {row['rep']:>6.3f} | "
              f"{row['meta']:>6.2f} | {show:>5}"
              + (f"  !! {err_txt[:60]}" if err_txt else ""), flush=True)
        # 每段落盘：长跑中断也不丢前面的结果（可 --resume 续跑）
        seg_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    db.close()
    return rows, proj.id


# --------------------------------------------------------------------------- #
# 断言
# --------------------------------------------------------------------------- #

def judge(rows: list[dict], thr: dict | None, segments: int, proj_id: int) -> dict:
    section("判定（与离线跑同一把尺）")
    ok_rows = [r for r in rows if not r.get("error")]
    err_rows = [r for r in rows if r.get("error")]

    check(f"R1 {segments} 段全部跑完（无异常段）", len(err_rows) == 0,
          f"完成 {len(ok_rows)}/{segments}" + (f"；异常段：{[r['seg'] for r in err_rows][:6]}" if err_rows else ""),
          "真模型跑出异常 = 真实故障面，这一条比离线跑更值钱")

    with SessionLocal() as db2:
        n_gen = db2.query(models.Generation).filter(
            models.Generation.project_id == proj_id).count()
    check("R2 落库段数与生成段数一致", n_gen == len(ok_rows),
          f"落库 {n_gen} / 生成 {len(ok_rows)}")

    if not ok_rows:
        return {"rows": rows}

    # —— 前情注入：预算软，但峰值不许漂移 ——
    vsize = 8
    peaks = [max(r["prior"] for r in ok_rows[i:i + vsize])
             for i in range(0, len(ok_rows), vsize)]
    warm = 2
    steady = peaks[warm:]
    drift = [(i + warm, a, b) for i, (a, b) in enumerate(zip(steady, steady[1:])) if b > a]
    budget = ok_rows[-1]["budget"]
    peak_max = max(peaks) if peaks else 0
    over_n = sum(1 for r in ok_rows if r["budget"] and r["prior"] > r["budget"])
    check("R3a 前情峰值**不随段数漂移**（预热 2 卷后必须饱和）", not drift,
          f"逐 8 段窗口峰值 {peaks}；最大 {peak_max}（预算 {budget}）；"
          f"稳态比较 {max(0, len(steady) - 1)} 次",
          f"递增的窗口：{drift[:5]}")
    check("R3b 前情峰值有硬上界（≤ 2× 预算）", peak_max <= 2 * budget if budget else True,
          f"实测 {peak_max / budget:.2f}× 预算" if budget else "无预算")
    bad_flag = [r["seg"] for r in ok_rows
                if r["budget"] and r["over_flag"] != (r["prior"] > r["budget"])]
    check("R3c `over_budget` 信号诚实", not bad_flag,
          f"信号与实际不符的段：{bad_flag[:6]}")
    if over_n:
        note(f"〔成本〕{over_n}/{len(ok_rows)} 段软超预算，峰值 {peak_max / budget:.2f}×")

    holes = [r for r in ok_rows if r["uncovered"]]
    check("R4 前情覆盖不留洞（uncovered 恒空）", not holes,
          f"uncovered 非空的段：{[r['seg'] for r in holes[:5]]}")
    covered = [r["n_segments"] for r in ok_rows]
    check("R5 覆盖区间随段数单调推进", covered == sorted(covered) and covered[-1] == segments - 1,
          f"覆盖末值 {covered[-1] if covered else None}（应为 {segments - 1}）")

    last = ok_rows[-1]
    check("R7 长跑后种子事实仍在注入里（角色）", last["n_chars"] >= len(SEED_CHARS),
          f"末段注入角色数 {last['n_chars']}（种子 {len(SEED_CHARS)}）")

    # —— 真模型特有：正文是不是真的写出来了 ——
    empties = [r["seg"] for r in ok_rows if r["chars"] < 50]
    check("R8 没有空段（正文 ≥ 50 字）", not empties,
          f"过短的段：{empties[:8]}",
          "空段 = 模型没吐字或被闸门拦了，这在线下跑里永远不会出现")

    # —— 哨兵：真正文可能触发**真**冲突，所以只列证据，不直接判失败 ——
    err_segs = [r["seg"] for r in ok_rows if r["sent_err"]]
    if err_segs:
        warn("R6 规则哨兵在真正文上报了 err 级冲突",
             f"段：{err_segs[:10]}（共 {len(err_segs)} 段）",
             "真正文可能真的有矛盾，也可能是抽取误报 —— 必须人工看原文再定性。"
             "这一项**不判失败**，因为离线跑的「零误报」口径在这里不适用。")
        for r in ok_rows:
            if r["sent_err"]:
                for f in r.get("sent_findings", [])[:2]:
                    note(f"  seg{r['seg']} {f.get('type')}: {str(f.get('message') or '')[:100]}")
    else:
        check("R6 规则哨兵在真正文上零 err 级冲突", True, "无 err 级告警")

    # —— 文风三指标 vs 人类基线（机器辅助，不能替代盲评）——
    if thr:
        reps = [r["rep"] for r in ok_rows[1:]]
        metas = [r["meta"] for r in ok_rows]
        cvs = [r["cv"] for r in ok_rows if r["cv"] is not None]
        if reps:
            check("M3 相邻段重复率 ≤ 人类 p90", max(reps) <= thr["rep_max"],
                  f"实测最大 {max(reps):.4f} / 中位 {pctl(reps, 0.5):.4f}（人类 p90 {thr['rep_max']:.4f}）",
                  "超了说明模型在复读（同一段话反复写）")
        if metas:
            check("M4 比喻密度 ≤ 人类 p90", max(metas) <= thr["meta_max"],
                  f"实测最大 {max(metas):.2f} / 中位 {pctl(metas, 0.5):.2f}"
                  f"（人类 p90 {thr['meta_max']:.2f}）",
                  "比喻密度是 AI 腔最稳的硬特征之一")
        if cvs:
            check("M5 句长 CV ≥ 人类 p10", min(cvs) >= thr["cv_min"],
                  f"实测最小 {min(cvs):.4f} / 中位 {pctl(cvs, 0.5):.4f}"
                  f"（人类 p10 {thr['cv_min']:.4f}）",
                  "句长太齐 = 机器味")
    else:
        warn("M3~M5 文风三指标无基线可比", "没有基线语料，本轮不判文风指标")

    return {"rows": rows, "peaks": peaks, "peak_max": peak_max, "budget": budget,
            "drift": drift, "over_n": over_n}


# --------------------------------------------------------------------------- #
# 盲评包
# --------------------------------------------------------------------------- #

def sample_positions(n_segments: int, k: int = 6) -> list[int]:
    """跨位置取样。

    **默认 6 个位置**：首 2、中 2、尾 2（漂移与文风退化都发生在长跑里）。

    **k > 6 时在整条长跑上均匀铺开** —— 题数多了就必须分散取：
    否则十几道题都落在同一段区间里，评者其实在评同一批文本，
    样本量是假的（判断之间高度相关，符号检验会低估不确定性）。

    跨位置取样是**刻意的**：只取段首会漏掉长跑后的退化，
    只取尾部会漏掉开头的跑偏。
    """
    if k <= 6:
        want = [1, 3, max(1, n_segments // 2), max(2, n_segments // 2 + 1),
                max(1, n_segments - 1), n_segments]
    else:
        # 首尾必留（漂移的两个端点），中间按 k 等分铺开
        step = (n_segments - 1) / (k - 1) if k > 1 and n_segments > 1 else 1
        want = [1 + int(round(i * step)) for i in range(k)]
    out: list[int] = []
    for p in want:
        p = max(1, min(n_segments, p))
        if p not in out:
            out.append(p)
    return out


def build_blind_packet(rows: list[dict], corpus: list[tuple[str, str]], out_dir: Path,
                       provider=None, n_snips: int = 6, chars: int = 400,
                       judge_provider=None) -> dict:
    """生成盲评包。

    ⚠️ **这版有六条硬约束**（2026-10-30，主人一句话点破了第一版）：
    「光是看到主角叫啥我就知道是不是人写的了」。

    1. **两侧同等脱敏**：第一版的人类样本取自一部知名小说，人物名一出来
       就等于举牌 ⇒ 现在两侧的专名都换成**等长占位符**，
       两侧用**同一套**召回 + 判定 + 替换流程（`acceptance_lib.mask_side`），
       评者既认不出出处，两边的处理痕迹也一致。
    2. **取样口径对称**：第一版我们取「段首 400 字」、人类取「随机 400 字」，
       引号密度差 2.3 倍 —— **取样位置本身就是泄露线索**。现在两侧都随机取。
    3. **题材/年代自检（整包级别）**：一古一今两侧，评者不看内容就能分出
       哪边是哪边 ⇒ 那个包**不是盲评**。现在会检出这种情况，
       把密封文件里的 `blind` 置 false ⇒ **判定脚本拒绝给统计结论**。
    4. **脱敏痕迹密度对称**：两侧占位符密度差太远 ⇒ 数井号就能认出
       出版物那一侧，同样判为不是盲评。
    5. **脱敏没跑成就不发题面**：留没脱敏的原文 = 直接交出出处。
    6. **题面级专名复查**（2026-10-30 补）：逐词判定会漏（一次批次失败
       = 一百个真名被记成普通词），所以脱敏之后**再读一遍题面**，
       把还是专名的词挑出来补遮；补不干净 ⇒ 整个包作废。

    另外：**人类语料身份记在密封文件里**（不进题面），事后能追溯样本来源。
    """
    ok_rows = [r for r in rows if not r.get("error") and r.get("text")]
    if not ok_rows:
        return {}
    # —— 版式归一（**两侧同一个函数**）：盗版站文本带段首缩进和章节标记，
    #    我们这侧没有 ⇒ 缩进/章节号本身就是「非对称特征」。
    #    不归一的话，题面里会出现「第74章 报复」这种拼进正文的章节头。
    ok_rows = [{"seg": r["seg"], "text": normalize_for_blind(r["text"])} for r in ok_rows]
    corpus = [(name, normalize_for_blind(t)) for name, t in corpus]
    positions = sample_positions(len(ok_rows), n_snips)
    picked = [r for r in ok_rows if r["seg"] in positions][:n_snips]
    if len(picked) < n_snips:
        return {}

    # —— 取样：两侧都用**随机位置**，口径一致（段首 vs 随机本身就是线索）——
    rng = random.Random()
    ours: list[str] = []
    for r in picked:
        t = r["text"]
        if len(t) <= chars:
            ours.append(t)
            continue
        st = rng.randrange(0, len(t) - chars)
        ours.append(t[st:st + chars])

    pool = [(name, text) for name, text in corpus if len(text) >= chars * 3]
    human: list[str] = []
    human_src: list[str] = []
    if pool:
        # **轮转取书**（不是随机抽）：多本混采时保证每本出的题数一样 ——
        # 随机抽可能 5 题来自同一本，评者认出一本就等于认出大半。
        start = rng.randrange(len(pool))
        for i in range(n_snips):
            name, src = pool[(start + i) % len(pool)]
            off = rng.randrange(0, max(1, len(src) - chars))
            human.append(src[off:off + chars])
            human_src.append(name)
    else:
        human = ["（没有语料可取人类片段）"] * n_snips
        human_src = ["（无）"] * n_snips

    # —— 两侧同等脱敏（同一套流程：召回 → LLM 判定 → 残片剔除 → 等长占位符）——
    ours_anon, ours_masked = ours, []
    human_anon, human_masked = human, []
    mask_fail: list[str] = []
    if provider is None:
        mask_fail.append("没有 provider（判定专名要用模型）")
    else:
        try:
            ours_anon, ours_masked = mask_side(
                ours, "\n".join(r["text"] for r in ok_rows), provider,
                cache_path=out_dir / "blind_names_ours.json",
                log=lambda m: print(f"     [我们侧脱敏] {m}"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 我们侧脱敏失败（{exc}）⇒ 两侧痕迹不对称，包不作数")
            mask_fail.append(f"我们侧脱敏失败（{exc}）")
        try:
            human_anon, human_masked = mask_side(
                human, "\n".join(t for _n, t in corpus), provider,
                cache_path=out_dir / "blind_names_human.json",
                log=lambda m: print(f"     [人类侧脱敏] {m}"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 人类侧脱敏失败（{exc}）⇒ 两侧痕迹不对称，包不作数")
            mask_fail.append(f"人类侧脱敏失败（{exc}）")

    if ours_masked:
        print(f"  我们侧遮掉 {len(ours_masked)} 个专名：{ours_masked[:12]}")
    if human_masked:
        print(f"  人类侧遮掉 {len(human_masked)} 个专名：{human_masked[:12]}")

    # —— 第六道闸门：题面级专名复查（逐词判定会漏，这一遍**读正文**）——
    #
    # 为什么必须有：逐词判定漏一个 = 真名原样留在题面里 = 盲评当场作废。
    # 实测（2026-10-30）：一次批次撞上 max_tokens 被截断 ⇒ 整批 100 个候选
    # 被记成「不是专名」⇒ 人类侧 1896 个候选里 1880 个是 null，
    # 真名「张山」「李香玲」原样出现在第 1 题里。
    #
    # 这一遍把**成段正文**交给模型，让它自己挑出还是专名的词 ——
    # 输入、提示词、缓存都与逐词判定不同 ⇒ 一遍漏了另一遍能抓住。
    # 挑到了就补遮（等长占位符，长度守恒），再查一遍；
    # 补不干净 ⇒ 整个包作废，题面不发出去。
    residual_found: list[str] = []
    residual_left: list[str] = []
    if provider is not None and not mask_fail:
        judge = judge_provider or provider
        body = "\n".join(ours_anon) + "\n" + "\n".join(human_anon)
        for rnd in range(RESIDUAL_ROUNDS):
            try:
                left = residual_proper_nouns(
                    body, judge, cache_path=out_dir / "blind_residual.json",
                    log=lambda m: print(f"     [题面复查] {m}"))
            except Exception as exc:  # noqa: BLE001 - 复查失败 ⇒ 不发题面，不能崩整轮验收
                mask_fail.append(f"题面级专名复查失败：{exc}")
                break
            if not left:
                break
            residual_found += left
            if rnd == RESIDUAL_ROUNDS - 1:
                residual_left = left
                break
            # 补遮：两侧都补（同一个词两侧都出现就都遮，保持痕迹对称）
            ours_anon = [mask_names(t, left)[0] for t in ours_anon]
            human_anon = [mask_names(t, left)[0] for t in human_anon]
            body = "\n".join(ours_anon) + "\n" + "\n".join(human_anon)
            print(f"     → 补遮 {len(left)} 个残留专名后重查")
        if residual_found:
            total = max(len(body), 1)
            ratio = sum(len(n) for n in set(residual_found)) / total
            print(f"  题面复查：补遮 {len(set(residual_found))} 个残留专名"
                  f"（占正文 {ratio:.1%}）")
            # 补遮比例过高 ⇒ 模型多半把普通词当成了专名 ⇒ 别把包糟蹋了
            if ratio > RESIDUAL_MAX_RATIO:
                residual_left = residual_left or sorted(set(residual_found))
                mask_fail.append(
                    f"题面复查补遮比例过高（{ratio:.1%} > {RESIDUAL_MAX_RATIO:.0%}）："
                    "模型多半在把普通词当专名，这个包不可信")

    # —— 自检：题材/年代 + 脱敏痕迹密度（**整包级别**）——
    dens_ours = mask_density("\n".join(ours_anon))
    dens_human = mask_density("\n".join(human_anon))
    verdict = blind_verdict("\n".join(ours_anon), "\n".join(human_anon),
                            dens_ours, dens_human,
                            ours_full="\n".join(r["text"] for r in ok_rows),
                            human_full="\n".join(t for _n, t in corpus))
    blind = verdict["blind"]
    # ⚠️ 脱敏没跑成 ⇒ 题面里留着**没脱敏的原文**（专名直接暴露出处）。
    #    这种包绝不能被判成盲评 —— 哪怕年代与密度都看不出来。
    if mask_fail:
        verdict["reasons"] = list(verdict["reasons"]) + [
            "脱敏没跑成，题面里留着没脱敏的原文：" + "；".join(mask_fail)]
        verdict["blind"] = False
        blind = False
    if residual_left:
        verdict["reasons"] = list(verdict["reasons"]) + [
            "题面里还残留专名（复查没清干净）：" + "、".join(residual_left[:10])]
        verdict["blind"] = False
        blind = False
    if not blind:
        print("  ⚠️ 自检：这个包**不是盲评包**")
        for reason in verdict["reasons"]:
            print(f"     · {reason}")
        print("     ⇒ 题面会写明这一点，判定脚本也会拒绝给统计结论。")

    # 随机化：每一对用抛硬币决定谁是 A
    rng = random.Random()
    key = []
    items = []
    for i in range(n_snips):
        flip = rng.random() < 0.5
        slot_a = "human" if flip else "ours"
        slot_b = "ours" if flip else "human"
        items.append({"id": f"s{i + 1:02d}", "A": slot_a, "B": slot_b})
        key.append({"sample_id": f"s{i + 1:02d}", "slot_A": slot_a, "slot_B": slot_b,
                    "our_segment_no": picked[i]["seg"],
                    "human_source": human_src[i]})
    order = list(range(n_snips))
    rng.shuffle(order)

    lines = ["# 盲评题面（**只有 A/B，没有标签**）", ""]
    if not blind:
        lines += [
            "> ⚠️ **本包不是盲评包**：两侧文本不满足盲评条件"
            "（见下）。",
            "> 评者不看内容就能分出哪边是哪边 ⇒ **不能用它做统计判定**。",
            "> 它的用途是**上界对照**：请只当「我们这段与出版物的差距」来读。",
            ">",
        ]
        for reason in verdict["reasons"]:
            lines.append(f"> - {reason}")
        lines.append("")
    else:
        lines += [
            f"每题两段各约 {chars} 字，一段来自本项目的产出，一段来自人类作品。",
            "**两侧的专名都已替换成等长占位符（＃＃）** —— 处理痕迹两边一样。",
            "请只凭文本判断，不要猜来源。**二选一**，不许「差不多」。", "",
        ]
    for n, idx in enumerate(order, 1):
        a_text = ours_anon[idx] if items[idx]["A"] == "ours" else human_anon[idx]
        b_text = ours_anon[idx] if items[idx]["B"] == "ours" else human_anon[idx]
        lines += [f"## 第 {n} 题（{items[idx]['id']}）", "", "**A**", "", "> " + a_text.replace("\n", "\n> "), "",
                  "**B**", "", "> " + b_text.replace("\n", "\n> "), ""]

    (out_dir / "blind_key.json").write_text(
        json.dumps({"note": "密封文件：谁是我们生成的、人类语料是谁、两侧是否同题材。"
                             "不要和题面一起给评者。",
                    "blind": blind,
                    "blind_reasons": verdict["reasons"],
                    "evidence": verdict["evidence"],
                    "masked_names": {"ours": ours_masked, "human": human_masked},
                    "residual_names": {
                        "repaired": sorted(set(residual_found)),
                        "left": residual_left,
                    },
                    "human_corpus": [name for name, _t in corpus],
                    "items": key}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 脱敏没跑成 / 题面里还残留专名 ⇒ 题面里留着**没脱敏的原文**
    # （专名直接暴露出处）⇒ **不发题面**，只留密封文件作追溯用。
    if mask_fail or residual_left:
        why = "脱敏没跑成" if mask_fail else "题面里还残留专名"
        print(f"  ⛔ {why} ⇒ **不出题面**（blind_samples.md / blind_sheet.md 不生成）")
        return {"positions": [r["seg"] for r in picked], "n": n_snips, "blind": False,
                "reasons": verdict["reasons"], "emitted": False,
                "mask_density": {"ours": dens_ours, "human": dens_human}}

    (out_dir / "blind_samples.md").write_text("\n".join(lines), encoding="utf-8")

    sheet = ["# 盲评记录表", "",
             "| sample | 更像人写 | 主线更清 | 有 AI 腔 | 猜 AI | 备注 |",
             "|---|---|---|---|---|---|"]
    for it in items:
        sheet.append(f"| {it['id']} | A / B | A / B | A / B | A / B | |")
    sheet += ["", "## 合计", "", "- 「更像人写」选我们那段的次数 k = ____",
              "- 评者人数 = ____（题面每份独立作答，互不通气）",
              f"- 判定 n = 评者人数 × {len(items)}"]
    (out_dir / "blind_sheet.md").write_text("\n".join(sheet), encoding="utf-8")
    return {"positions": [r["seg"] for r in picked], "n": n_snips, "blind": blind,
            "reasons": verdict["reasons"], "emitted": True,
            "mask_density": {"ours": dens_ours, "human": dens_human}}


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=int, default=30)
    ap.add_argument("--channel", default="opencode-go", choices=["opencode-go", "minimax"])
    ap.add_argument("--model", default="")
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--timeout", type=int, default=300)
    # 输出目录用**相对路径**：写死成某台机器的绝对路径，换台机器 clone 就会
    # 生成一个名字里带盘符的目录（POSIX 上那根本不是目录，是一个普通文件名）。
    # 用正斜杠：Windows 的 Python 也认这个分隔符。
    ap.add_argument("--out", default="out/acceptance")
    ap.add_argument("--corpus", default="", help="基线语料（.db / .txt / 目录）")
    ap.add_argument("--human-corpus", default="",
                    help="盲评的**人类侧**语料（.db / .txt / 目录；"
                         "给目录 = 多本混采；缺省用基线语料）")
    ap.add_argument("--baseline-max-chars", type=int, default=1_200_000)
    ap.add_argument("--snips", type=int, default=6,
                    help="盲评包题数（默认 6）。题数 × 评者人数 = 判定数 n；"
                         "n < 18 时检验没有统计力量，所以要么多请人、要么把题数加到 18。")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--no-blind", action="store_true")
    args = ap.parse_args()

    model = args.model or ("deepseek-v4.1-flash" if args.channel == "opencode-go" else "MiniMax-M3")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.corpus:
        os.environ["ACCEPTANCE_CORPUS"] = args.corpus
    if not args.resume:
        _drop_db()

    print("=" * 78)
    print("AI 小说生成器 · 真模型端到端验收")
    print(f"渠道：{args.channel}　模型：{model}　段数：{args.segments}")
    print("=" * 78)
    print("声明：本跑验的是**真模型下的闭环与文风指标**；"
          "主观「像不像人写 / 有没有跑题」仍需人工盲评（本脚本会生成盲评包）。")

    section("0. 装配通道")
    provider = build_provider(args.channel, model, args.max_tokens, args.timeout,
                               session=f"acc{time.strftime('%m%d%H%M')}")
    print(f"  provider：{type(provider).__name__}（已装成 Writer 单例，内部调用同源）")
    # 判定专名**单独一个 provider**：关思考、不装成单例。
    # 理由：逐词判定的回包是「每个词一个 JSON 对象」，输出越长越容易被
    # max_tokens 拦腰截断，而截断 = 整批判定消失 = 真名留在题面里。
    judge_provider = build_provider(args.channel, model, args.max_tokens, args.timeout,
                                     session=f"judge{time.strftime('%m%d%H%M')}",
                                     thinking_off=True, install_singleton=False)
    print("  判定 provider：独立实例（关思考）—— 判定回包短，不易被截断")

    section("A. 人类散文基线（阈值的来源）")
    base, thr = load_baseline(args.baseline_max_chars)
    if base:
        books = "；".join(f"{b['name']} {b['chars']:,} 字/{b['segs']} 段" for b in base["books"])
        print(f"  语料：{books}")
        print(f"  相邻段 3-gram Jaccard：中位 {pctl(base['rep'], 0.5):.4f}  "
              f"p90 {pctl(base['rep'], 0.9):.4f}  (n={len(base['rep'])})")
        print(f"  比喻密度 / 百字：中位 {pctl(base['meta'], 0.5):.4f}  "
              f"p90 {pctl(base['meta'], 0.9):.4f}  (n={len(base['meta'])})")
        print(f"  句长变异系数：中位 {pctl(base['cv'], 0.5):.4f}  "
              f"p10 {pctl(base['cv'], 0.1):.4f}  (n={len(base['cv'])})")
        print(f"  → 阈值：重复率 ≤ {thr['rep_max']:.4f}｜比喻密度 ≤ {thr['meta_max']:.4f}｜"
              f"句长CV ≥ {thr['cv_min']:.4f}")
    else:
        thr = None

    section(f"B. {args.segments} 段闭环（真模型）")
    rows, proj_id = run_segments(args.segments, provider, out_dir, args.resume)
    result = judge(rows, thr, args.segments, proj_id)

    report = {
        "channel": args.channel, "model": model, "segments": args.segments,
        "segments_done": len(rows),
        "pass": PASS, "fail": FAIL, "warn": _WARN,
        "failed": _FAILED, "notes": _NOTES,
        "thresholds": thr,
        "baseline": ({k: base[k] for k in ("novels", "chars", "segs", "books")}
                     if base else None),
        "rows": [{k: v for k, v in r.items() if k != "_grams"} for r in rows],
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_blind:
        section("C. 盲评包")
        # 人类侧语料**可以跟基线语料不是同一批**：
        # 基线要「一份能代表当代网文的语料」，盲评人类侧要「同题材 + 混采几本」，
        # 两者诉求不同 ⇒ 分开指定（--human-corpus / BLIND_HUMAN_CORPUS）。
        human_spec = args.human_corpus or os.environ.get("BLIND_HUMAN_CORPUS", "") \
            or baseline_corpus_spec()
        try:
            corpus = corpus_texts(human_spec, max_chars=600_000) if os.path.exists(
                human_spec) else []
        except Exception:  # noqa: BLE001
            corpus = []
        if corpus:
            print(f"  人类侧语料：{human_spec}")
            print(f"  混采书目：{'、'.join(n for n, _t in corpus)}")
        info = build_blind_packet(rows, corpus, out_dir, provider=provider,
                                   n_snips=args.snips, judge_provider=judge_provider)
        if info:
            print(f"  取样段：{info['positions']}")
            print(f"  脱敏痕迹密度（井号/百字）：我们 {info['mask_density']['ours']:.2f} / "
                  f"人类 {info['mask_density']['human']:.2f}")
            print(f"  自检：{'是盲评包' if info['blind'] else '**不是盲评包**'}"
                  + ("" if info["blind"] else f" —— {info['reasons']}"))
            if info.get("emitted") is False:
                print("  ⛔ **没出题面**（脱敏没跑成）—— 只留了密封文件作追溯。")
            else:
                print("  已生成：blind_samples.md（题面）/ blind_key.json（密封）/ blind_sheet.md（记录表）")
                print("  ⚠️ 题面给评者时**不要**同时给 blind_key.json")
        else:
            warn("盲评包没生成", "可用段落不足")

    print()
    print("=" * 78)
    print(f"PASS {PASS}   FAIL {FAIL}   WARN {_WARN}")
    for f in _FAILED:
        print(f"  FAIL: {f}")
    print(f"产出目录：{out_dir}")
    print("=" * 78)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
