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
    density_leak,
    era_mismatch,
    flavor_asymmetry,
    flavor_distance,
    flavor_hits,
    flavor_mean,
    flavor_profile,
    flavor_signature,
    grams,
    genre_distance,
    genre_mismatch,
    genre_profile,
    human_baseline,
    jaccard,
    judge_genres,
    mask_density,
    mask_names,
    mask_side,
    metaphor_per100,
    normalize_for_blind,
    pctl,
    profile_distance,
    profile_mean,
    residual_proper_nouns,
    residual_proper_nouns_per_item,
    sent_cv,
    shape_candidates,
    attribution_discriminability,
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
#:
#: ⚠️ **为什么是 7 轮，不是 3 轮**（2026-10-31 实测）：
#:
#: ① **模型是非确定性的**：同一段文字问两次，答案可能不同。
#:    实测 s06 的「麻子」第 1 轮没报、第 3 轮才报出来 ⇒
#:    每一轮都得把**全部题目重新问一遍**（只问「文本变了的那些题」
#:    等于把漏掉的答案永远锁死）。
#: ② **遮名字会生出残片**：遮掉「五塘站」之后，「五塘站B口」剩下「B口」——
#:    它长得像个专名，下一轮被报出来 ⇒ 再遮一轮。
#:
#: ⇒ 两件事叠加，一轮清不干净。实测：3 轮后还剩 3 个（假阴性 + 残片各占一半）。
#: ⇒ 轮数不够就**整包作废**，宁可不出包也不带着真名出包。
#:
#: ⚠️ 循环遇到**第一个干净的轮次就停** ⇒ 轮数只是上限，不是固定开销；
#: 实测收敛在第 4~5 轮。
#:
#: ⚠️ **2026-10-31 第二次实测：7 轮不够**（包被 fail-closed 扣下）。
#: 那一轮每轮新认出 1~4 个名字（第 5 轮甚至认出「猫科动物信息素的分离和提取」
#: 这种**整句短语**），第 7 轮才认出「苏醒」⇒ 轮数用光 ⇒ 整包作废。
#:
#: ⇒ 上限提到 **12**：新名字的出现率是**递减**的（实测每轮 6/5/4/3/1/3/1 个题），
#: 12 轮足够收敛；代价只是最坏情况多花 5 轮（每轮 ≈ 17 次判定调用）。
#:
#: ⚠️ 这一层的失败模式是「**宁可作废**」—— 宁可不出包，也不带着真名出包。
RESIDUAL_ROUNDS = 12

#: 补遮掉的专名字数占正文的比例上限。
#:
#: 超过它 ⇒ 模型多半把普通词当成了专名（这时候再补遮只会把题面涂花）。
#: 这种包宁可作废，也不能交出去一个被涂花的题面。
RESIDUAL_MAX_RATIO = 0.05

#: 每题挑人类侧窗口时试几个（先按题材指纹粗筛，再交给模型判题材）。
#:
#: ⚠️ 别调大：这些窗口**每一个都要过一遍模型判题材**（12 段一批），
#: 候选一多就 hours 级别。实测 10 个/臂够用（粗筛先排掉了一大半明显不像的）。
WINDOW_TRIES = 6

#: 人类侧**候选池**大小（每个臂、每本书）：先脱敏、再判题材、再配对。
#:
#: 每个臂的人类侧候选窗口数量（**粗筛后**保留的个数）。
#:
#: 为什么不是更多：每个候选都要过一遍模型判题材，而题材判定是整条链上
#: 最贵的一步（实测一次调用 3~10 分钟）。而且候选窗口**越长**，
#: 要遮的专名候选越多（实测现代都市文里 4000 字片段能召出 1000+ 个候选）。
#:
#: ⚠️ 但也不能太少：脱敏痕迹密度**只有脱敏之后才知道**
#: （实测两种密度代理 —— 形状候选数、PMI 真名命中数 —— 在真实书上的
#: 区分度都不够：实际井号密度 4.9~11.3，代理值只有 41~44 / 95~136，
#: 几乎不相关 ⇒ 只能「多取几个窗口，脱敏后自己挑」）。
#: ⇒ 候选数**直接决定这个臂最多能出几题**（每个窗口只能用一次）：
#: 20 个候选 ⇒ 最多 20 题，实测能凑出 8 题（受我们侧段数限制）。
POOL_TRIES = 20

#: 每个我们侧的段取几个窗口（同一段的不同起点）。
#:
#: 为什么：密度是**逐窗口**属性，而我们侧的段之间密度差得也多
#: （实测 1.53~4.55 井号/百字）⇒ 一个段只取一个窗口 ⇒ 密度档位不够，
#: 配不出密度接近的组合。
#: ⇒ 每段取几个起点不同的窗口，**配对时每段只能用一次**（同一段出现两次
#: 评者会看出来）。
WINDOW_PER_SEG = 2

#: 每个臂**多备**几段（候选段数 = 每臂题数 + 这个数）。
#:
#: 为什么：有些段的两个窗口都配不上（密度比或题材不合）⇒ 只备够题数的段
#: 的话，这一臂就少出题（实测都市臂 5/6）⇒ 备几段多余的，配对时挑够就停。
EXTRA_SEGS = 3

#: 候选窗口之间至少隔这么多字（否则同一个场景会被切出好几个「窗口」，
#: 评者会发现几题其实是同一段）。
POOL_MIN_GAP = 1500

#: 一道题两侧的**脱敏痕迹密度**比上限（max/min）。
#:
#: 为什么要有：整包密度差 2.5 倍就判泄露，但那是**整包**口径；
#: 逐题配对时收紧到 1.6 —— 一道题里一侧全是井号、另一侧几乎没有，
#: 评者一眼就能看出哪边是「被处理过的」。
DENSITY_PAIR_MAX_RATIO = 1.6

#: 配对调试开关（打印每一条被拒的候选）。
_DEBUG_PAIR = bool(os.environ.get("BLIND_DEBUG_PAIR"))

#: 出包的随机种子。
#:
#: 为什么必须固定：① 我们侧窗口的起点是随机的 ⇒ 不固定的话每次出的题面
#: 都不一样，**判定缓存永远命不中**（题材判定按文本 sha256 缓存）
#: ⇒ 每次都要重跑全部模型调用，几小时下不来；
#: ② 出包要**可复现**：同一个种子 + 同一批输入 ⇒ 同一个包，出问题能复查。
#: 换种子 = 换一批题（`BLIND_SEED=1234 python ...`）。
BLIND_SEED = int(os.environ.get("BLIND_SEED", "20261031"))

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


def spread_positions(n_segments: int, k: int) -> list[int]:
    """取 **恰好 k** 个位置，跨整条长跑均匀铺开（首尾必留）。

    为什么不能直接用 `sample_positions`：它在 ``k <= 6`` 时返回**固定的 6 个位置**
    （首 2、中 2、尾 2），跟 k 无关 —— 多臂出包每个臂的题数是 ``n_snips / 臂数``，
    用它会取到超出配额的段（实测 k=2 却取到 5 个位置）。
    """
    if n_segments <= 0 or k <= 0:
        return []
    if k >= n_segments:
        return list(range(1, n_segments + 1))
    step = (n_segments - 1) / (k - 1) if k > 1 else 0
    want = [1 + int(round(i * step)) for i in range(k)]
    out: list[int] = []
    for p in want:
        p = max(1, min(n_segments, p))
        if p not in out:
            out.append(p)
    return out


def _round_robin_order(chosen: list[dict]) -> list[int]:
    """题序**轮转**打散：返回 `chosen` 的下标顺序。

    ## 为什么不用纯随机

    主人 2026-10-31 的原话：「要么就直接打乱，每个都是从乱七八糟的什么题材都有，
    这样就没办法靠题材分辨了」—— 要的是**每题两侧同题材、题与题之间题材乱着来**。

    纯随机打散的问题是**可能连续两题同题材**（实测出现过都市×3 连排）：
    评者看到第 1、2 题都是都市，就知道第 3 题大概也是都市
    ⇒ 题材又能当定位线索。

    轮转取题 ⇒ **相邻两题必定来自不同臂**，题材在题序上交替出现。
    每轮按「桶里还剩多少」降序取，避免某个臂早早用完、后面连着同题材。
    """
    buckets: dict[str, list[int]] = {}
    for i, c in enumerate(chosen):
        buckets.setdefault(c.get("arm", ""), []).append(i)
    order: list[int] = []
    while buckets:
        for arm in sorted(buckets, key=lambda a: (-len(buckets[a]), a)):
            if buckets[arm]:
                order.append(buckets[arm].pop(0))
        buckets = {a: v for a, v in buckets.items() if v}
    return order


#: **人工剔除清单**的文件名（放在出包目录里）。
#:
#: ## 为什么需要它
#:
#: 有些「题材不同」是**词表与模型都判不出来**的（同一个大类里的子题材）：
#: 实测《回声年代》里混着**军旅/谍战**与**科幻悬疑**的情节，
#: 而我们这侧是**市井/系统流**主线 ⇒ 模型把两侧都判成「现代都市（市井生活流）」
#: ⇒ 闸门全绿，但**评者能靠题材分辨**（一边是爆炸与功勋，一边是饺子与冰柜）。
#:
#: ⇒ 这类题只能靠**人眼**剔。而「剔掉哪几个窗口」必须**可复现**：
#: 清单里记的是「臂 + 人类侧窗口偏移」，不是「第几题」
#: （题序会随题数变化而变，偏移不会）。
#:
#: 格式：``{"都市异能": [408000, 280000]}``（偏移，人类侧原文字符下标）。
BLIND_EXCLUDE_FILE = "blind_exclude.json"


def _load_exclude(out_dir: Path) -> dict[str, set[int]]:
    """读人工剔除清单（没有就当空）。"""
    path = out_dir / BLIND_EXCLUDE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, set[int]] = {}
    for arm, offs in data.items():
        try:
            out[str(arm)] = {int(o) for o in (offs or [])}
        except Exception:  # noqa: BLE001
            continue
    return out


def build_multi_arm_packet(arms: list[dict], out_dir: Path, provider=None,
                            n_snips: int = 18, chars: int = 400,
                            judge_provider=None) -> dict:
    """多题材臂的盲评包：**每题两侧同题材，题与题之间题材乱着来**。

    ## 为什么要有它

    单题材臂的包会被**题材指纹**泄露（实测 2026-10-31）：人类侧那本满篇
    「厉鬼 / 复苏 / 规则」，我们侧是都市市井悬疑 ⇒ 评者读一两题就能分出
    哪边是出版物，**年代闸门全绿也没抓到**（年代只分了「古风 / 现代」两桶）。

    ⇒ 做法：**每一题的两侧都取自同一个题材臂**（同题材、同年代），
      而**不同题来自不同题材臂** —— 题材在题与题之间乱着来，
      评者没法用「哪边像人写的」定位，只能逐题读文风。

    ## 流程（顺序是有讲究的）

    1. **两侧都取 400 字窗口** —— 我们侧不能用整段（2000+ 字 vs 人类侧 400 字
       = 一眼就能看出来的非对称特征）；
    2. **先脱敏**，再判题材 —— 判的是**评者真正看到的那段文字**；
    3. **配对**：题材一致（模型判，闭集）+ **脱敏痕迹密度接近**
       （密度差一大截，数井号就认得出哪边是出版物）；
    4. 题序打散。

    ## arms 的结构

    ```python
    {"name": "古风",
     "rows":   [{"seg": 1, "text": "..."}],       # 我们侧该题材的产出
     "corpus": [("书名", "整本正文")],               # 人类侧该题材的语料
     "era": "古风"}
    ```
    """
    if not arms:
        return {}
    rng = random.Random(BLIND_SEED)
    out_dir.mkdir(parents=True, exist_ok=True)

    genre_provider = judge_provider or provider

    # —— 1. 每臂分多少题（多的往前排，余数分给前面的臂）——
    base, extra = divmod(n_snips, len(arms))
    quota = [base + (1 if i < extra else 0) for i in range(len(arms))]

    # —— 2. 每臂：我们侧的窗口 + 人类侧的候选池 ——
    exclude = _load_exclude(out_dir)
    #
    # ⚠️ 每个臂用**独立的随机源**（种子 = BLIND_SEED + 臂序号）：
    # ① 各臂互不干扰 ⇒ 可以**分臂预热**（一个臂一个臂跑，各自写同一份缓存），
    #    最后再跑一次全量 —— 缓存命中，几分钟出包（不然三臂连跑要两小时，
    #    撞上后台任务 1 小时上限）；
    # ② 窗口可复现 ⇒ 题材判定缓存（按文本 sha256）能命中。
    plan: list[dict] = []
    for arm_idx, (arm, k) in enumerate(zip(arms, quota)):
        rng_arm = random.Random(BLIND_SEED + 1000 * arm_idx)
        arm_name = arm.get("name", "")
        drop = exclude.get(arm_name, set())
        rows_all = [r for r in (arm.get("rows") or []) if r.get("text")]
        # ⚠️ **先剔掉比窗口还短的段**（生成被截断的），再分配位置 ——
        # 否则短段白占一个位置 ⇒ 这一臂少出一题（实测都市臂 9 段里有 1 段
        # 只有 282 字 ⇒ 6 个位置只凑出 5 道题）。
        rows = [r for r in rows_all
                if len(normalize_for_blind(r["text"])) >= chars]
        corpus = [(n, normalize_for_blind(t)) for n, t in (arm.get("corpus") or [])]
        if not rows or not corpus or k <= 0:
            continue
        ours: list[str] = []
        seg_no: list[object] = []
        # 多备几段：有些段两个窗口都配不上（密度/题材不合）⇒
        # 只备 quota 段的话，这一臂就少出题（实测都市臂 5/6）。
        # ⇒ 备 `quota + EXTRA_SEGS` 段，配对时挑够 quota 个就停。
        n_seg = min(len(rows), k + EXTRA_SEGS)
        for p in spread_positions(len(rows), n_seg):
            st = normalize_for_blind(rows[p - 1]["text"])
            # 段本身比窗口还短的（生成被截断的段）⇒ 跳过，不拿它凑数
            if len(st) < chars:
                continue
            # 每段取 WINDOW_PER_SEG 个**起点不同**的窗口：
            # 脱敏痕迹密度是逐窗口属性（实测我们侧各段 1.53~4.55 井号/百字），
            # 一个段只给一个窗口 ⇒ 密度档位不够，配不出密度接近的组合。
            # 配对时**每段只能用一次**（见 `used_segs`）。
            for _j in range(WINDOW_PER_SEG):
                start = rng_arm.randrange(0, len(st) - chars)
                ours.append(st[start:start + chars])
                seg_no.append(rows[p - 1].get("seg"))
        if not ours:
            continue
        # 人类侧候选池：**粗筛排序 → 取前 POOL_TRIES 个**。
        #
        # 为什么只取这么点（2026-10-31 改）：每个候选都要过一遍模型判题材
        # （题材闸门），而题材判定是最贵的一步（实测一次 3~10 分钟）。
        # 18 个候选/臂 × 3 臂 = 54 次 ⇒ 整包跑不完。
        #
        # 粗筛怎么排：用「该臂我们侧窗口的**平均**题材指纹」当锚点，
        # 把明显不同题材的窗口排到后面。⚠️ 词表口径只作**排序**用，
        # 它把 18 本语料里的 9 本判错过 ⇒ 绝不能当判据；
        # 真正的题材一致与否由模型判（`judge_genres`）。
        anchor = profile_mean(ours)
        # ⚠️ **第八道闸门**：细粒度（世界观级）子题材指纹。
        # 模型的题材闭集只有 9 类，「军旅训练营」「古风宅斗」「耽美荤话」
        # 「职场公文」都不在里面 ⇒ 模型只能判「看不出来」⇒ 闸门放行
        # ⇒ 但评者一眼就看得出题材不同（实测 13 题里 5 题栽在这）。
        # ⇒ 这里用**词表指纹**当第二层锚点，把「明显不同子题材」的窗口排后。
        flavor_anchor = flavor_mean(ours)
        # 脱敏痕迹密度是**逐窗口**属性，而密度是**最硬的泄露信号**
        # （评者数一数井号就知道哪边是出版物）。
        # 实测 2026-10-31：都市臂的人类侧《回声年代》是群像文，
        # 窗口里「疑似专名」17~45 个 ⇒ 脱敏后 3.9~11.7 井号/百字，
        # 而我们侧只有 1.5~3.6 ⇒ **密度比 2.4~7.4** ⇒ 逐题配对全部被拒。
        #
        # ⇒ 选候选窗口时就把密度当**第一排序键**：
        # 用「形状候选数」（本地算，不调模型）当脱敏密度的**代理** ——
        # 它与脱敏后的井号密度同向（被遮的真名必须先是候选）。
        ours_shape = sorted(len(shape_candidates(w)) for w in ours)
        target_shape = ours_shape[len(ours_shape) // 2]
        pool: list[tuple[str, int, str]] = []
        for name, src in corpus:
            # 采样步长 2000 > POOL_MIN_GAP(1500) ⇒ 任意两个候选的间隔都够大，
            # 「同一窗口只用一次」的去重规则不会把它们吃掉。
            offs = _candidate_offsets(src, chars, step=2000, limit=700)
            scored = []
            for off in offs:
                win = src[off:off + chars]
                shape_gap = abs(len(shape_candidates(win)) - target_shape)
                prof_gap = profile_distance(genre_profile(win), anchor)
                flav_gap = flavor_distance(flavor_profile(win), flavor_anchor)
                scored.append((off, prof_gap, shape_gap, flav_gap))
            # ⚠️ **按「子题材类集合」分组挑候选**（2026-10-31 新增，第八道闸门）。
            #
            # 为什么按类集合分：两侧**子题材类集合相同**是「指纹对称」的
            # **充要条件**（某一类只在一侧出现 ⇒ 评者能靠它定位）。
            # ⇒ 我们侧有哪几种类集合，人类侧就必须各准备同样类集合的窗口，
            #   否则那一类我们侧窗口**一道题都配不出来**。
            #
            # ⚠️ 2026-10-31 的一个**假结论**（已推翻，留着当反面教材）：
            # 第一版指纹词表只收「面板/宿主/积分/升级/委托」这类标准系统流词，
            # 量出来「《回声年代》全书 215 个窗口**一个系统流词都没有**」
            # ⇒ 一度打算把都市臂整个弃用（「这本就配不上这个题材」）。
            # 补上「系统/任务/技能/经验」后重测：人类侧本来就有
            # **17 个「任务」窗口、9 个「系统」窗口** ⇒ **是尺错了，不是语料错了**。
            # ⇒ 教训：量出「一个都没有」这种极端结论，先怀疑尺。
            by_sig: dict[tuple, list[tuple]] = {}
            for off, prof_gap, shape_gap, flav_gap in scored:
                sig = flavor_signature(src[off:off + chars])
                by_sig.setdefault(sig, []).append((off, prof_gap, shape_gap, flav_gap))
            # 我们侧用到哪些类集合、各要几个窗口
            need: dict[tuple, int] = {}
            for w in ours:
                sig = flavor_signature(w)
                need[sig] = need.get(sig, 0) + 1
            total_need = sum(need.values()) or 1
            picked: list[int] = []
            for sig, cnt in sorted(need.items(), key=lambda kv: -kv[1]):
                quota = max(1, int(round(POOL_TRIES * cnt / total_need)))
                group = sorted(by_sig.get(sig, []), key=lambda x: x[0])
                if not group:
                    # 这一类**没有对称窗口** ⇒ 这一类我们侧窗口配不出题
                    continue
                # 组内按**密度档位铺开**挑（形状候选数 = 脱敏密度的代理）。
                #
                # ⚠️ 为什么不能「每段取形状最接近中位数的那一个」（2026-10-31 实测）：
                # 人类侧《回声年代》是群像文，形状候选数 18~95（中位 55），
                # 我们侧主线叙事只有 20~56（中位 39）——
                # 若按「离我们中位数最近」挑，池子会**全落在 39~45 那一档**，
                # 于是**我们侧低密度窗口（20~30）配不上任何人类侧窗口**
                # （密度比 2~4.6 ⇒ 逐题被拒）⇒ 实测都市臂只出题 4/10。
                #
                # ⇒ 改成：按形状**排名分档**，每档取一个 ⇒ 池子**铺满整个密度区间**，
                #   每个我们侧窗口都能在池子里找到密度档位接近的那个。
                #   档内按 offset 取中位 ⇒ 顺带把窗口摊到全书（别都挤在一处）。
                by_shape = sorted(group, key=lambda x: (x[2], x[0]))
                per = max(1, len(by_shape) // quota)
                for i in range(quota):
                    band = by_shape[i * per:(i + 1) * per]
                    if not band:
                        continue
                    band_by_off = sorted(band, key=lambda x: x[0])
                    best = band_by_off[len(band_by_off) // 2]
                    if best[0] not in picked:
                        picked.append(best[0])
            if len(picked) < POOL_TRIES:
                # 不够就从**已有类集合**的组里再补（补的顺序按密度距离），
                # 绝不跨类集合补 —— 跨类的窗口对称不了，进了也是白进。
                rest = [x for x in scored if x[0] not in picked
                        and flavor_signature(src[x[0]:x[0] + chars]) in need]
                rest.sort(key=lambda x: (x[2], x[1]))
                for off, _p, _s, _f in rest:
                    if len(picked) >= POOL_TRIES:
                        break
                    picked.append(off)
            for off in picked:
                if off in drop:
                    continue          # 人工剔除清单里的窗口（题材对不上，人眼判的）
                pool.append((name, off, src[off:off + chars]))
        plan.append({"arm": arm.get("name", ""), "era": arm.get("era", ""),
                     "ours": ours, "segs": seg_no, "pool": pool,
                     "rows": rows, "quota": k})
    if not plan:
        return {}

    # —— 3. 脱敏：两侧同一个函数；我们侧按臂分批，人类侧按书分批 ——
    ours_full_by_arm: dict[str, str] = {}
    for arm in arms:
        ours_full_by_arm[arm.get("name", "")] = "\n".join(
            normalize_for_blind(r["text"]) for r in (arm.get("rows") or [])
            if r.get("text"))
    human_full_by_book: dict[str, str] = {}
    for arm in arms:
        for n, t in (arm.get("corpus") or []):
            human_full_by_book[n] = normalize_for_blind(t)

    mask_fail: list[str] = []
    ours_masked: dict[str, list[str]] = {}
    human_masked: dict[str, list[str]] = {}
    if provider is None:
        mask_fail.append("没有 provider（判定专名要用模型）")
    else:
        for p in plan:
            try:
                out, _hit = mask_side(
                    p["ours"], ours_full_by_arm.get(p["arm"], ""), provider,
                    cache_path=out_dir / f"blind_names_ours_{_slug(p['arm'])}.json",
                    log=lambda m, a=p["arm"]: print(f"     [我们侧 {a}] {m}"))
                ours_masked[p["arm"]] = out
            except Exception as exc:  # noqa: BLE001
                mask_fail.append(f"我们侧 {p['arm']} 脱敏失败（{exc}）")
        by_book: dict[str, list[str]] = {}
        for p in plan:
            for name, _off, text in p["pool"]:
                by_book.setdefault(name, []).append(text)
        for book, texts in by_book.items():
            try:
                out, _hit = mask_side(
                    texts, human_full_by_book.get(book, ""), provider,
                    cache_path=out_dir / f"blind_names_human_{_slug(book)}.json",
                    log=lambda m, b=book: print(f"     [人类侧 {b}] {m}"))
                human_masked[book] = out
            except Exception as exc:  # noqa: BLE001
                mask_fail.append(f"人类侧 {book} 脱敏失败（{exc}）")

    # 挂回：人类侧池按书顺序重建（与上面分批顺序一致）
    # ⚠️ 这里**不能用**循环变量 `name` 当字典键 —— 内层解包会把它覆盖掉，
    # 下一轮进循环时它还留着上一轮的旧值，于是各臂的人类侧文本被整体对调
    # （实测 2026-10-31：都市臂拿到古风臂的窗口，题材一致性全线崩）。
    # ⇒ 一律用**显式的书名**取脱敏结果，循环变量另起名。
    pool_masked: dict[tuple[str, int], str] = {}
    for p in plan:
        entries = p["pool"]
        if not entries:
            continue
        book = entries[0][0]                     # 该臂人类侧用的书
        masked_book = human_masked.get(book, [])
        if len(masked_book) != len(entries):
            mask_fail.append(
                f"人类侧 {book} 的脱敏结果条数对不上"
                f"（{len(masked_book)} ≠ {len(entries)}）")
            continue
        for (pname, off, _text), mtext in zip(entries, masked_book):
            pool_masked[(pname, off)] = mtext

    # —— 4. 判题材（判的是**脱敏后**的文本）——
    reasons: list[str] = []
    evidence: dict = {}
    # 候选池的偏移：供人复查「窗口是不是覆盖全书」（而不是全挤在开头）
    evidence["pool_offsets"] = {p["arm"]: [off for _n, off, _t in p["pool"]]
                                for p in plan}
    if genre_provider is None:
        mask_fail.append("没有 provider（判题材要用模型）")
    else:
        to_judge: list[tuple[str, str]] = []
        for p in plan:
            for i, t in enumerate(ours_masked.get(p["arm"], [])):
                to_judge.append((f"o:{p['arm']}:{i}", t))
            for name, off, _t in p["pool"]:
                to_judge.append((f"h:{name}:{off}", pool_masked.get((name, off), "")))
        try:
            labels = judge_genres(to_judge, genre_provider,
                                  cache_path=out_dir / "blind_genres.json",
                                  log=lambda m: print(f"     [题材判定] {m}"))
        except Exception as exc:  # noqa: BLE001
            mask_fail.append(f"题材判定失败：{exc}")
            labels = {}
        missing = [k for k, _ in to_judge if k not in labels]
        if missing:
            mask_fail.append(f"有 {len(missing)}/{len(to_judge)} 段题材没判出来")
        evidence["genre_labels"] = labels

    # —— 5. 配对：题材一致 + 密度接近 + 窗口不重复 ——
    chosen: list[dict] = []
    if not mask_fail:
        for p in plan:
            got = 0
            used_windows: set[tuple[str, int]] = set()
            # **每段只能用一次**（同一段的两个窗口都进包 ⇒ 评者看得出是同一段）
            used_segs: set = set()
            quota = p.get("quota") or len(ours_masked.get(p["arm"], []))
            # ⚠️ 候选窗口要**打散**再配对：
            # 按 pool 的原顺序（= 按 offset 升序）配，评者会拿到
            # **全书最前面**的那几个窗口（实测 2 题的 offset 是 2000/4000，
            # 而书有 4 万字）⇒ 几题其实是同一段书的前后脚，评者一眼能看出。
            pool_order = list(range(len(p["pool"])))
            rng.shuffle(pool_order)
            for i, otext in enumerate(ours_masked.get(p["arm"], [])):
                if got >= quota:
                    break
                seg = p["segs"][i] if i < len(p["segs"]) else None
                if seg in used_segs:
                    continue          # 这一段的另一个窗口已经用过了
                la = labels.get(f"o:{p['arm']}:{i}", "")
                odens = mask_density(otext)
                best = None
                for pi in pool_order:
                    name, off, _t = p["pool"][pi]
                    if (name, off) in used_windows:
                        continue
                    htext = pool_masked.get((name, off), "")
                    lb = labels.get(f"h:{name}:{off}", "")
                    bad, ev = genre_mismatch(otext, htext, labels=(la, lb))
                    if bad:
                        if _DEBUG_PAIR:
                            print(f"      [配对·拒] {p['arm']} 段{i} 窗口{off}: "
                                  f"genre {ev.get('reason')} "
                                  f"la={la!r} lb={lb!r}")
                        continue
                    if era_mismatch(otext, htext)[0]:
                        if _DEBUG_PAIR:
                            _ev = era_mismatch(otext, htext)[1]
                            print(f"      [配对·拒] {p['arm']} 段{i} 窗口{off}: era "
                                  f"{_ev['dominant']} {_ev['counts']} "
                                  f"ours={otext[:30]!r} human={htext[:30]!r}")
                        continue
                    # —— 第八道闸门：细粒度子题材指纹**不对称** ⇒ 评者能定位 ——
                    # 例：「一侧军旅训练营、另一侧职场悬疑」——模型闭集里没有
                    # 「军旅」这一类，模型判「看不出来」，闸门会放行，
                    # 但评者一眼看得出两段不是一个题材。
                    asym = flavor_asymmetry(otext, htext)
                    if asym:
                        if _DEBUG_PAIR:
                            print(f"      [配对·拒] {p['arm']} 段{i} 窗口{off}: "
                                  f"子题材指纹不对称 {asym}")
                        continue
                    # —— 第九道闸门：句法惯例对称性（**写法**）——
                    # ⚠️ 这一道**不逐题剔除**，只在**整包**上判（见整包自证处）。
                    # 实测（真包验算）：逐题剔除的**净效果是负的** ——
                    # 剔掉的 10 题里把我们判赢的 2 题也剔了，而我们判输的 8 题
                    # 一个没抓到 ⇒ 这次失败是**系统性**的，逐题治不了。
                    hd = mask_density(htext)
                    lo_d, hi_d = min(odens, hd), max(odens, hd)
                    if lo_d <= 0:
                        # 两侧都没有占位符 ⇒ 没有痕迹可比（比值无意义，按「相同」算）
                        ratio = 1.0 if hi_d <= 0 else 99.0
                    else:
                        ratio = hi_d / lo_d
                    if ratio > DENSITY_PAIR_MAX_RATIO:
                        if _DEBUG_PAIR:
                            print(f"      [配对·拒] {p['arm']} 段{i} 窗口{off}: "
                                  f"density {ratio:.2f}（我们 {odens:.2f} / "
                                  f"人类 {hd:.2f}）")
                        continue
                    if best is None or ratio < best[0]:
                        best = (ratio, name, off, htext, hd, ev)
                if best is None:
                    continue
                _ratio, name, off, htext, hd, ev = best
                used_windows.add((name, off))
                used_segs.add(seg)
                chosen.append({
                    "arm": p["arm"], "era": p["era"],
                    "seg": seg,
                    "ours": otext, "human": htext, "book": name,
                    "offset": off, "genre_ok": True, "genre_evidence": ev,
                    "era_ok_after": True,
                    "flavor": {"ours": flavor_hits(otext), "human": flavor_hits(htext)},
                    "len_ratio": round(min(len(otext), len(htext))
                                       / max(len(otext), len(htext), 1), 3),
                    "density": {"ours": odens, "human": hd},
                })
                got += 1
            print(f"  臂「{p['arm']}」出题 {got}/{quota}"
                  f"（我们侧候选窗口 {len(ours_masked.get(p['arm'], []))} 个，"
                  f"人类侧候选 {len(p['pool'])} 个）")
    if not chosen:
        print("  ⛔ 没有一道题配得上 ⇒ **不出题面**")
        return {"n": 0, "blind": False, "emitted": False,
                "reasons": reasons or ["没有一道题同时满足题材一致与脱敏密度接近"],
                "positions": []}

    ours_all = [c["ours"] for c in chosen]
    human_all = [c["human"] for c in chosen]
    dens_ours = mask_density("\n".join(ours_all))
    dens_human = mask_density("\n".join(human_all))
    evidence["mask_density"] = {"ours": dens_ours, "human": dens_human}
    if density_leak(dens_ours, dens_human):
        reasons.append(
            f"脱敏痕迹密度差得出来（我们 {dens_ours:.2f} vs 人类 {dens_human:.2f} "
            "个井号/百字）：数一数就能认出出版物那一侧")
    per_arm = {}
    for c in chosen:
        per_arm.setdefault(c["arm"], {"n": 0, "genre_bad": 0, "era_bad": 0})
        per_arm[c["arm"]]["n"] += 1
    evidence["per_arm"] = per_arm

    # —— 第八道闸门的整包自证：每一题两侧的子题材指纹都得对称 ——
    # 配对时已经逐题挡过；这里再查一遍整包（防止以后有人改配对逻辑漏掉）。
    asym_items = []
    for c in chosen:
        bad = flavor_asymmetry(c["ours"], c["human"])
        if bad:
            asym_items.append({"arm": c["arm"], "offset": c["offset"], "asym": bad})
    evidence["flavor_asymmetry"] = asym_items
    if asym_items:
        reasons.append(
            f"有 {len(asym_items)}/{len(chosen)} 题的两侧子题材指纹不对称"
            f"（例：{asym_items[0]['asym']}）⇒ 评者能靠题材定位")

    # —— 第九道闸门：**整包**写法判别力检验 ——
    # 问的是「**只用写法，能不能定位哪边是人类侧**」。
    # 能 ⇒ 这个包在写法上不盲 ⇒ 评者能靠写法定位 ⇒ **不该发出去**。
    #
    # ⚠️ 为什么是整包、不是逐题剔除：实测（真包验算）逐题剔除**净效果为负**
    # （把我们判赢的 2 题剔了、我们判输的 8 题一个没抓到）⇒ 这次失败是
    # **系统性**的（我们侧从不按参考书的惯例写对话归属），逐题治不了。
    #
    # 实测（2026-10-31 第一次真跑）：规则「『X道：“…”』出现的那侧 = 人类侧」
    # 在 10 对可判题里**判对 10**（精确率 100%）⇒ 这个包在写法上根本不盲。
    attr = attribution_discriminability(ours_all, human_all)
    evidence["style_discriminability"] = attr
    if attr.get("leaky"):
        reasons.append(
            f"**只用写法就能定位哪边是人类侧**（规则「{attr['rule']}」"
            f"在 {attr['best'].get('decidable')} 对可判题里判对 "
            f"{attr['best'].get('correct')}，精确率 "
            f"{attr['best'].get('precision', 0):.0%}）⇒ 这个包在写法上不盲")

    # —— 6. 题面级专名复查（第六道闸门，同单臂包）——
    #
    # ⚠️ **必须逐题问**（2026-10-31 实测）：整包 17 题（14422 字）一次丢给模型，
    # 它返回**空数组** —— 读不完那么长的一段就放弃了 ⇒ 闸门是**假阴性**，
    # 包被判「题面干净」而实际漏了《道途魔典》的「聚金阁」和我们侧的
    # 「落雁坳」「慧照峰」。逐题问（每题约 800 字）立刻报出来。
    # ⇒ 长文本让模型**沉默**而不是报错；沉默被当成了「没有」⇒ 最危险。
    residual_left: list[str] = []
    residual_found: list[str] = []
    if provider is not None and not mask_fail:
        judge = judge_provider or provider
        # ⚠️ **不传 cache_path**：安全闸门不缓存否定结论（模型非确定性 ⇒
        # 缓存会把一次走运的「干净」固化成每次重建都在白捡的假阴性）。
        for rnd in range(RESIDUAL_ROUNDS):
            items_now = [(f"s{n:02d}", f"{c['ours']}\n{c['human']}")
                         for n, c in enumerate(chosen, 1)]
            try:
                per_item = residual_proper_nouns_per_item(
                    items_now, judge,
                    log=lambda m: print(f"     [题面复查] {m}"))
            except Exception as exc:  # noqa: BLE001
                mask_fail.append(f"题面级专名复查失败：{exc}")
                break
            # 汇总：任一题报出残留专名 ⇒ 整包都要补遮
            # （同一个名字可能在多题出现；补遮是**两侧都补**，保持痕迹对称）
            left_all: list[str] = []
            for _sid, names in per_item.items():
                for n in names:
                    if n not in left_all:
                        left_all.append(n)
            if not left_all:
                break
            residual_found += left_all
            if rnd == RESIDUAL_ROUNDS - 1:
                residual_left = left_all
                break
            for c in chosen:
                c["ours"] = mask_names(c["ours"], left_all)[0]
                c["human"] = mask_names(c["human"], left_all)[0]
            print(f"     → 补遮 {len(left_all)} 个残留专名后重查")
        if residual_found:
            total = sum(len(c["ours"]) + len(c["human"]) for c in chosen) or 1
            ratio = sum(len(n) for n in set(residual_found)) / total
            print(f"  题面复查：补遮 {len(set(residual_found))} 个残留专名"
                  f"（占正文 {ratio:.1%}）")
            if ratio > RESIDUAL_MAX_RATIO:
                residual_left = residual_left or sorted(set(residual_found))
                mask_fail.append(
                    f"题面复查补遮比例过高（{ratio:.1%} > {RESIDUAL_MAX_RATIO:.0%}）："
                    "模型多半在把普通词当专名，这个包不可信")

    blind = not reasons and not mask_fail and not residual_left
    if mask_fail:
        reasons.append("脱敏没跑成，题面里留着没脱敏的原文：" + "；".join(mask_fail))
    if residual_left:
        reasons.append("题面里还残留专名（复查没清干净）：" + "、".join(residual_left[:10]))

    # —— 7. 组装题面：题序打散（题材在题与题之间乱着来）——
    #
    # 为什么用**轮转**而不是纯随机：纯随机可能连续两题同题材
    # （实测出现过都市×3 连着排），那样评者又能靠题材定位到哪边是出版物
    # ⇒ 轮转取题，**相邻两题必定不同题材**，且题与题之间题材乱着来。
    order = _round_robin_order(chosen)
    items, key = [], []
    for n, idx in enumerate(order, 1):
        c = chosen[idx]
        flip = rng.random() < 0.5
        slot_a = "human" if flip else "ours"
        slot_b = "ours" if flip else "human"
        sid = f"s{n:02d}"
        items.append({"id": sid, "A": slot_a, "B": slot_b})
        key.append({
            "sample_id": sid,
            "slot_A": slot_a, "slot_B": slot_b,
            "arm": c["arm"], "our_segment_no": c["seg"],
            "human_source": c["book"], "human_offset": c["offset"],
            "genre_ok": c["genre_ok"], "era_ok": c["era_ok_after"],
            "len_ratio": c.get("len_ratio"), "density": c.get("density"),
            "genre_evidence": c.get("genre_evidence", {}),
            "flavor": c.get("flavor", {}),
        })

    lines = ["# 盲评题面（**只有 A/B，没有标签**）", ""]
    if not blind:
        lines += [
            "> ⚠️ **本包不是盲评包**：两侧文本不满足盲评条件（见下）。",
            "> 评者不看内容就能分出哪边是哪边 ⇒ **不能用它做统计判定**。", ">",
        ]
        for reason in reasons:
            lines.append(f"> - {reason}")
        lines.append("")
    else:
        lines += [
            f"每题两段各约 {chars} 字，一段来自本项目的产出，一段来自人类作品。",
            "**两侧的专名都已替换成等长占位符（＃＃）** —— 处理痕迹两边一样。",
            "题目之间的题材**不一样**，但**每一题的两段是同一个题材**。",
            "请只凭文本判断，不要猜来源。**二选一**，不许「差不多」。", "",
        ]
    for n, idx in enumerate(order, 1):
        a_text = chosen[idx]["ours"] if items[n - 1]["A"] == "ours" else chosen[idx]["human"]
        b_text = chosen[idx]["ours"] if items[n - 1]["B"] == "ours" else chosen[idx]["human"]
        lines += [f"## 第 {n} 题（{items[n - 1]['id']}）", "", "**A**", "",
                  "> " + a_text.replace("\n", "\n> "), "",
                  "**B**", "", "> " + b_text.replace("\n", "\n> "), ""]

    (out_dir / "blind_key.json").write_text(json.dumps({
        "note": "密封文件：谁是我们生成的、人类语料是谁、两侧是否同题材。不要和题面一起给评者。",
        "blind": blind,
        "blind_reasons": reasons,
        "evidence": evidence,
        "residual_names": {"repaired": sorted(set(residual_found)), "left": residual_left},
        "items": key,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if mask_fail or residual_left or not chosen:
        why = ("脱敏没跑成" if mask_fail else
               "题面里还残留专名" if residual_left else "没有合格题目")
        print(f"  ⛔ {why} ⇒ **不出题面**")
        return {"n": len(chosen), "blind": False, "reasons": reasons, "emitted": False,
                "positions": [c["seg"] for c in chosen],
                "mask_density": {"ours": dens_ours, "human": dens_human},
                "per_arm": per_arm}

    (out_dir / "blind_samples.md").write_text("\n".join(lines), encoding="utf-8")
    (out_dir / "blind_sheet.md").write_text(build_blind_sheet(items), encoding="utf-8")
    return {"n": len(items), "blind": True, "reasons": reasons, "emitted": True,
            "positions": [c["seg"] for c in chosen],
            "mask_density": {"ours": dens_ours, "human": dens_human},
            "per_arm": per_arm}


def build_blind_sheet(items: list[dict]) -> str:
    """生成盲评**记录表**（给评者填的那张）。

    ## 为什么只有**两列**（2026-10-31 改）

    原来的表有四列：「更像人写」「主线更清」「有 AI 腔」「猜 AI」。
    主人当场点破：「猜了哪个更像人写，另外那个不就是 AI 了么」
    ⇒ **强制二选一下，「猜 AI」与「更像人写」必然取反，「有 AI 腔」同理**
    ⇒ 这两列的**信息量是零**，还让评者多填 36 次。

    ⇒ 现在只留两列：
    - 「更像人写」—— **判定用**（二项检验的主指标）；
    - 「主线更清」—— **独立维度**（叙事清晰度 ≠ 像不像人写的），
      判定脚本拿它出**交叉表**：两列指向不同侧的那些题 =
      「我们的产出是主线清楚但不像人写（语言层问题），
      还是像人写但主线乱（结构层问题）」。

    「备注」列留着：评者如果认出某一侧出自哪本书，写在这一列，
    判定脚本会把它单独拎出来（那一题不计入统计）。

    ## 为什么**没有「合计」栏**（2026-10-31 改）

    原来的表末尾有一段：
    `- 「更像人写」选我们那段的次数 k = ____` / `评者人数 = ____` / `判定 n = 评者人数 × 18`。

    三个问题，任意一个都足以删掉它：
    1. **评者填不了** —— A/B 与「哪边是我们的」是**逐题随机**的（密封文件里），
       评者只有逐题作答，没有办法从汇总数反推 k ⇒ 这一栏只会让人瞎填一个数。
    2. **k 根本不用人填** —— `verify_blind_eval.py` 直接从密封文件算 k 与 n
       （`slot_A` / `slot_B` 决定哪边是「我们」），人手填的那份只会和脚本对不上。
    3. **它把「哪边是谁」这层框子漏给了评者** —— 「选**我们**那段」暗示了
       「有人=我们的产出、有人=不是」，而这张表上评者只需要回答
       「哪一段更像人写」。多给一句提示，就多一条可以顺着猜的路。

    ⇒ 表上只留**评者填得到的东西**：题号 + 两列判断 + 备注。
    题数写在表头（「一共 N 题」），n = 评者人数 × 题数 由**汇总方**按行数算，
    不用在表上留空让人填。
    """
    lines = ["# 盲评记录表", "",
             f"一共 **{len(items)} 题**，每题两段（A 和 B）。两列都填 `A` 或 `B`；",
             "**备注**列可留空 —— 但如果你觉得某一侧出自某本具体的书，",
             "请写在这一列（那一题会被单独剔除，不计入判定）。", "",
             "| sample | 更像人写 | 主线更清 | 备注 |",
             "|---|---|---|---|"]
    for it in items:
        lines.append(f"| {it['id']} | A / B | A / B | |")
    return "\n".join(lines)


def _slug(name: str) -> str:
    """给书名/臂名做文件名安全的短标签（ASCII 优先，中文保留但去掉路径字符）。"""
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in (name or ""))
    return (out or "x")[:40]


def _candidate_offsets(text: str, chars: int, step: int = 2000, limit: int = 300) -> list[int]:
    """在长文本里取候选窗口起点（稀疏采样，够用又快）。"""
    n = len(text or "")
    if n <= chars:
        return [0]
    offs = list(range(0, n - chars + 1, step))
    if len(offs) > limit:
        offs = offs[:limit]
    if offs[-1] != n - chars:
        offs.append(n - chars)
    return offs


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
        # ⚠️ **不传 cache_path**：安全闸门不缓存否定结论（见 acceptance_lib 的说明）
        for rnd in range(RESIDUAL_ROUNDS):
            # ⚠️ **逐题问**（2026-10-31 实测）：整包一次问（14422 字），
            # 模型返回**空数组** —— 读不完那么长的一段就放弃了 ⇒ 闸门是**假阴性**，
            # 包被判「题面干净」而实际漏了「聚金阁」「落雁坳」这类专名。
            # ⇒ 逐题问（每题约 800 字）才问得动。
            items_now = [(f"s{n:02d}", f"{a}\n{h}")
                         for n, (a, h) in enumerate(zip(ours_anon, human_anon), 1)]
            try:
                per_item = residual_proper_nouns_per_item(
                    items_now, judge,
                    log=lambda m: print(f"     [题面复查] {m}"))
            except Exception as exc:  # noqa: BLE001 - 复查失败 ⇒ 不发题面，不能崩整轮验收
                mask_fail.append(f"题面级专名复查失败：{exc}")
                break
            left: list[str] = []
            for _sid, names in per_item.items():
                for n in names:
                    if n not in left:
                        left.append(n)
            if not left:
                break
            residual_found += left
            if rnd == RESIDUAL_ROUNDS - 1:
                residual_left = left
                break
            # 补遮：两侧都补（同一个词两侧都出现就都遮，保持痕迹对称）
            ours_anon = [mask_names(t, left)[0] for t in ours_anon]
            human_anon = [mask_names(t, left)[0] for t in human_anon]
            print(f"     → 补遮 {len(left)} 个残留专名后重查")
        if residual_found:
            total = sum(len(t) for t in ours_anon) + sum(len(t) for t in human_anon)
            total = max(total, 1)
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

    # —— 第九道闸门：**整包**写法判别力检验（单臂包也要查）——
    #
    # 为什么单臂包也要查：写法泄露与题材/专名**无关**。
    # 多臂包那次实测：题材、年代、专名、脱敏痕迹**全部对称**，
    # 但「只用写法就能定位哪边是人类侧」的规则精确率 **100%**。
    attr = attribution_discriminability(ours_anon, human_anon)
    verdict.setdefault("evidence", {})["style_discriminability"] = attr
    if attr.get("leaky"):
        verdict["reasons"] = list(verdict["reasons"]) + [
            f"**只用写法就能定位哪边是人类侧**（规则「{attr['rule']}」"
            f"在 {attr['best'].get('decidable')} 对可判题里判对 "
            f"{attr['best'].get('correct')}，精确率 "
            f"{attr['best'].get('precision', 0):.0%}）⇒ 这个包在写法上不盲"]
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
    (out_dir / "blind_sheet.md").write_text(build_blind_sheet(items), encoding="utf-8")
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
