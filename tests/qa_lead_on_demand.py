"""QA：引导语「按需触发」的真跑测量台（2026-10-28 加）。

## 这个脚本量什么

`tests/test_lead_in_on_demand.py` 用**假 provider** 守住了「补到哪、补几处、
什么时候不补」三条规则（纯机械、可复现）。但**采纳率**与**真跑耗时**只有真模型给得出来。

本脚本拿**真实正文 + 真实模型**跑一遍 `app.style.lead_in.insert_lead_ins`
（生产代码本身，不做简化），量三件事：

1. 每段实际花了几次模型调用（静态预测 ≈ 5.6，旧行为 ≈ 11.7）；
2. 补完之后**有没有真的进带**（`p10 <= ratio <= p90`）；
3. 每次调用**多贵**（耗时、思考量）—— 这一项决定了「关思考」值不值。

## 怎么跑

```bash
# 需要：一个带**新口径形态档案**的库（档案里要有「纯台词段占比」这一项）+ 一个可用的 OpenAI 兼容端点
export LEAD_API_KEY=sk-...            # 凭据只从环境变量读，不进仓库、不进日志
export LEAD_BASE_URL=https://…/v1
export LEAD_MODEL=qiniu/conatus-free  # 端点上**真的能调**的模型
export LEAD_THINKING=off              # off = 发 thinking:{"type":"disabled"}
python tests/qa_lead_on_demand.py <库路径> [<库路径> …]
```

输出：`tests/qa_lead_on_demand_run.json`（明细）+ 控制台汇总。

## 为什么脚本里没有「本机绝对路径」

库路径走命令行参数、backend 路径从 `__file__` 推导 —— 这个仓库**不携带任何机器路径**
（见 README「发布卫生」）。凭据同理：只从环境变量读，脚本里没有 key。

## 一条方法论（这脚本最有价值的部分）

**「模型没回话」和「判据拒了」在结果里长得一模一样。**

`insert_lead_ins` 遇到调用异常会记成 `{"reason": "调用失败 RuntimeError"}`，
和「长度不在 10~30 字」并列在同一个 `rejected` 列表里 ——
于是**限流**看起来像**判据太严**。

本脚本第一版就栽在这里：74 次调用全被拒，读起来是「判据全灭」，
真相是 `max_tokens=320` 让思考类模型每次都 `finish=length` 空返回（0.1 秒/次）。
⇒ 所以这里对**限流类失败做重试**，并把「调用失败」单独统计出来：
判断「判据好不好」之前，先确认**模型真的回话了**。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.config import settings  # noqa: E402
from app.llm.provider import GenericOpenAIChatProvider  # noqa: E402
from app.style.form import form_target, measure_form  # noqa: E402
from app.style.form_merge import merge_bare_dialogue  # noqa: E402
from app.style.lead_in import (  # noqa: E402
    LEAD_IN_SAFETY_SLOTS,
    fill_lead_ins_until_in_band,
    should_fill_lead_ins,
)
from app.style.stylometry import paragraphs  # noqa: E402

OUT = os.path.join(REPO, "tests", "qa_lead_on_demand_run.json")

#: 端点上「真的能调」的模型。生产那条 MiniMax 链
#: `models=[M3.1-Flash-Preview, M3, M3.1-Flash, Text-01]` 是 MiniMax **官方**端点专有的，
#: 拿到别的 OpenAI 兼容端点上是 403 —— 模型链必须跟着端点走。
LEAD_MODELS = ("qiniu/conatus-free", "moonshotai/kimi-k3-free")

#: kimi-k3 是思考类模型：`max_tokens=320` 时每次都 `finish=length` 空返回。
#: 引导语正文只用 ~30 字，需要的是「先想几个词再写完」的预算。
LEAD_MAX_TOKENS = int(os.environ.get("LEAD_MAX_TOKENS", "3000"))
LEAD_TIMEOUT = int(os.environ.get("LEAD_TIMEOUT", "240"))
#: `off` → 请求体带 `thinking:{"type":"disabled"}`（本项目 provider 已有的字段）；
#: `none` → 不发思考控制字段（模型按默认，最慢最贵）。
LEAD_THINKING = (os.environ.get("LEAD_THINKING") or "off").strip().lower()


def _provider():
    """提供方**只**从环境变量拿凭据 —— 不读库、不打印、不落盘。"""
    key = (os.environ.get("LEAD_API_KEY") or "").strip()
    if not key:
        raise SystemExit("no api key in env (LEAD_API_KEY)")
    base = (os.environ.get("LEAD_BASE_URL") or "").strip().rstrip("/")
    if not base:
        raise SystemExit("no base url in env (LEAD_BASE_URL)")
    model = (os.environ.get("LEAD_MODEL") or LEAD_MODELS[0]).strip()
    chain = [model] + [m for m in LEAD_MODELS if m != model]
    return GenericOpenAIChatProvider(
        api_key=key, base_url=base, model=model, models=chain,
        thinking_off=(LEAD_THINKING == "off"),
        max_tokens=LEAD_MAX_TOKENS, timeout=LEAD_TIMEOUT,
    )


class Counting:
    """统计调用次数 + 对**限流类失败**重试。

    为什么必须重试：免费档 429 是常态。不重试的话限流会被记成「判据拒绝」，
    看起来像判据太严，其实是**没打到模型**（见模块 docstring 末尾的方法论）。
    """

    def __init__(self, inner, retries: int = 4, wait: float = 5.0):
        self.inner = inner
        self.calls = 0
        self.retries = retries
        self.wait = wait
        self.failed = 0

    def chat(self, *a, **k):
        self.calls += 1
        last = None
        for attempt in range(self.retries):
            try:
                return self.inner.chat(*a, **k)
            except Exception as exc:  # noqa: BLE001
                last = exc
                msg = str(exc)
                transient = ("429" in msg or "Too Many" in msg
                             or "rate" in msg.lower() or "busy" in msg.lower()
                             or "temporarily unavailable" in msg.lower())
                if not transient:
                    break
                time.sleep(self.wait * (attempt + 1))
        self.failed += 1
        raise last


def load_segments(db: str):
    """取「有形态档案」的正文段。档案里没有「纯台词段占比」这一项 ⇒ 用不了。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute(
            "SELECT id, project_id, text FROM generations "
            "WHERE text IS NOT NULL AND length(text) > 500 ORDER BY id"):
        row = con.execute("SELECT style_profile FROM projects WHERE id=?",
                          (r["project_id"],)).fetchone()
        prof = json.loads(row[0]) if row and row[0] else {}
        target = form_target(prof)
        if not target:
            continue
        rosters = {rr[0] for rr in con.execute(
            "SELECT canonical_name FROM kg_entities WHERE kind='character' AND project_id=?",
            (r["project_id"],))}
        names = {rr[0] for rr in con.execute(
            "SELECT canonical_name FROM kg_entities WHERE project_id=?", (r["project_id"],))}
        out.append((r["id"], r["project_id"], r["text"], target, rosters, names))
    con.close()
    return out


def main() -> None:
    dbs = sys.argv[1:]
    if not dbs:
        raise SystemExit(__doc__ or "usage: qa_lead_on_demand.py <db> [<db> ...]")

    print("style_lead_in(default) =", bool(getattr(settings, "style_lead_in", None)))
    print("LEAD_IN_SAFETY_SLOTS  =", LEAD_IN_SAFETY_SLOTS)
    print("relay model/max_tokens =", (os.environ.get("LEAD_MODEL") or LEAD_MODELS[0]),
          LEAD_MAX_TOKENS, "thinking=" + LEAD_THINKING)
    print()

    segs: list = []
    for db in dbs:
        segs.extend(load_segments(db))
    # 多份库往往是同一批正文的不同快照 ⇒ **按正文去重**，
    # 否则同一段会被算两次（第一版就把 7 段报成 13 段）。
    uniq: dict = {}
    for s in segs:
        uniq.setdefault(hashlib.sha1(s[2].encode("utf-8")).hexdigest(), s)
    segs = list(uniq.values())
    print("segments with band:", len(segs), "(deduped by text)")
    if not segs:
        print("no usable segments")
        return

    results = []
    t_start = time.time()
    for gid, pid, text, target, rosters, names in segs:
        provider = Counting(_provider())

        paras = [p for p in paragraphs(text) if p.strip()]
        merged, _m = merge_bare_dialogue(paras, target["target"])
        ratio_now = measure_form("\n".join(merged))["bare_ratio"]
        trigger = should_fill_lead_ins(ratio_now, p10=target["p10"], p90=target["p90"])

        rec = {"gid": gid, "pid": pid,
               "mean": round(target["target"], 3), "p10": round(target["p10"], 3),
               "p90": round(target["p90"], 3),
               "ratio_after_merge": round(ratio_now, 3), "trigger": trigger}

        if trigger and bool(getattr(settings, "style_lead_in", None)):
            t0 = time.time()
            out, linfo = fill_lead_ins_until_in_band(
                merged, provider, p10=target["p10"], p90=target["p90"],
                proper_nouns=names, corpus_text=None, character_names=rosters,
            )
            dt = round(time.time() - t0, 1)
            final_ratio = measure_form("\n".join(out))["bare_ratio"]
            rejected = linfo.get("rejected") or []
            reasons = [str(x.get("reason")) for x in rejected]
            rec.update({
                "slots": linfo.get("slots", 0), "inserted": linfo.get("inserted", 0),
                "rounds": linfo.get("rounds", 0),
                "rounds_detail": linfo.get("rounds_detail", []),
                "rejected": len(rejected),
                # 「调用失败」单列：它不是判据拒绝，是**没打到模型**。
                "call_failures": sum(1 for x in reasons if "调用失败" in x),
                "calls": provider.calls, "provider_failures": provider.failed,
                "final_ratio": round(final_ratio, 3),
                "in_band": bool(target["p10"] <= final_ratio <= target["p90"]),
                "sec_lead": dt,
                "chars_before": len(text), "chars_after": sum(len(p) for p in out),
                "fill_ratio": linfo.get("fill_ratio"),
                "safety_slots": linfo.get("safety_slots"),
                "reject_reasons": reasons,
            })
        else:
            rec.update({"skipped": True, "calls": 0,
                        "final_ratio": rec["ratio_after_merge"],
                        "in_band": bool(target["p10"] <= ratio_now <= target["p90"])})
        results.append(rec)
        print("gid=%-4s trigger=%s ratio=%.3f -> %.3f calls=%s in_band=%s sec=%s" % (
            gid, rec["trigger"], rec["ratio_after_merge"], rec["final_ratio"],
            rec.get("calls"), rec["in_band"], rec.get("sec_lead", 0)))

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)

    ran = [r for r in results if not r.get("skipped")]
    calls = sum(r.get("calls", 0) for r in results)
    inserted = sum(r.get("inserted", 0) for r in ran)
    judged = calls - sum(r.get("call_failures", 0) for r in ran)
    print()
    print("== SUMMARY ==")
    print("segments            : %d" % len(results))
    print("triggered           : %d" % len([r for r in results if r["trigger"]]))
    print("total model calls   : %d" % calls)
    print("accepted            : %d / %d judged (%.0f%%)" % (
        inserted, judged, 100.0 * inserted / max(1, judged)))
    print("in_band after lead  : %d/%d" % (sum(1 for r in ran if r["in_band"]), len(ran)))
    print("call failures       : %d (限流，不是判据拒绝)" % sum(
        r.get("call_failures", 0) for r in ran))
    print("wall sec            : %.1f (%.1f/段)" % (
        time.time() - t_start, (time.time() - t_start) / max(1, len(ran))))


if __name__ == "__main__":
    main()
