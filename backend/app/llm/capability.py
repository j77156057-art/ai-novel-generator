"""模型能力探测：能否关闭思考链、能承受多少并发。

## 为什么需要运行时探测

不同厂商关闭思考链的字段完全不同，猜错的表现是**空 content**（内容全在
`reasoning_content` 里）或 HTTP 400。并发上限同理——代码里写死一个数字必然
有一边不合适。

实测教训（本项目，2026-10-20）：

    智谱 glm-4-flash   短 prompt 8/8 全成功，1500 token prompt 并发 3 就 8/12 触发 429
    智谱 glm-4-flash   并发 1: 0.13 req/s  →  并发 2: 0.24 req/s（上限）
    MiniMax-M3         并发 15: 0.86 req/s（100% 成功）

**约束是 TPM 不是 RPM**，所以必须用接近生产负载的 prompt 长度去测。
几十 token 的短 prompt 永远测不出限流。

## 为什么做成只读工具给 agent

用户问「这个模型能不能关思考」「为什么蒸馏这么慢」时，agent 需要**实测**
而不是背文档。文档写的「并发 30」通常是并发数口径，长 prompt 下差一个数量级。

## 不消耗业务数据的保证

本模块只发 HTTP 请求到 LLM 端点，**不读写任何业务表**。它进 Assistant 白名单
是安全的：唯一副作用是消耗模型额度。
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# ── 构造「真实长度」的 prompt ──────────────────────────────────────────
# 内容与 token 数无关（限流按 token 计），但**必须够长**才能触发 TPM 限流。
_FILLER = (
    "他站在城墙上往下看，风把沙粒吹到脸上，生疼。城下那条河已经干了三年，"
    "河床上铺满白色的卵石，像一排排没埋好的骨头。远处有烟，不知道是炊烟还是"
    "烧房子。他把手按在刀柄上，指节发白，又松开。这已经是他今天第七次做这个"
    "动作了，每次都以为会看见什么，其实什么也没有。"
)


def build_probe_prompt(in_tokens: int = 1500) -> str:
    """构造约 `in_tokens` 个 token 的探测 prompt（中文约 1 字 ≈ 0.7-1 token）。"""
    target_chars = max(int(in_tokens * 1.4), 100)
    reps = max(1, target_chars // len(_FILLER) + 1)
    body = (_FILLER * reps)[:target_chars]
    return (
        "分析下面这段小说，提取写作手法。输出 JSON，"
        "键为「人物描写」「对白风格」「节奏控制」，每个键给 2 条抽象规则。\n\n"
        f"【正文】\n{body}\n\n只输出 JSON。"
    )


#: 关闭思考链的候选参数，按实测有效顺序排列。
#: 每个元素是 (可读名, 请求体附加字段)。
THINK_OFF_STRATEGIES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("thinking=disabled", {"thinking": {"type": "disabled"}}),
    ("reasoning_effort=none", {"reasoning_effort": "none"}),
    ("reasoning_effort=minimal", {"reasoning_effort": "minimal"}),
    ("chat_template_kwargs.enable_thinking=false",
     {"chat_template_kwargs": {"enable_thinking": False}}),
    ("enable_thinking=false", {"enable_thinking": False}),
    ("thinking=disabled + reasoning_effort=none",
     {"thinking": {"type": "disabled"}, "reasoning_effort": "none"}),
)


def _post(base_url: str, key: str, payload: dict, timeout: int = 400) -> dict:
    """发一次 chat/completions，返回 {ok, data} 或 {ok:False, code/err}。"""
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "data": json.loads(r.read().decode("utf-8", "replace"))}
    except urllib.error.HTTPError as e:
        return {"ok": False, "code": e.code,
                "body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def _extract(resp: dict) -> tuple[str, str]:
    """从响应里取 (正文, 推理内容)。"""
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    # 有的厂商把推理塞在正文的 <think> 标签里
    if not reasoning and "<think>" in content:
        m = re.search(r"<think>(.*?)</think>", content, re.S)
        if m:
            reasoning = m.group(1).strip()
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return content, reasoning


# ====================================================================
# 能力一：探测能否关闭思考链
# ====================================================================

def probe_thinking(
    base_url: str, api_key: str, model: str,
    in_tokens: int = 1500, out_tokens: int = 500, timeout: int = 400,
) -> dict:
    """逐个尝试关闭思考链的策略，返回实测结论。

    返回结构（agent 直接读 `verdict` / `recommended`）：

        {
          "verdict": "supported" | "unsupported" | "baseline_failed",
          "baseline": {"latency", "content_len", "reason_len", "thinks"},
          "strategies": [{"name", "ok", "latency", "reason_len", "speedup", "error"}],
          "recommended": {"name", "params", "latency", "speedup"} | None,
          "note": "人话解释"
        }
    """
    prompt = build_probe_prompt(in_tokens)

    def call(extra: dict) -> dict:
        payload = {"model": model,
                   "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": out_tokens, "temperature": 0.3, **extra}
        t0 = time.time()
        r = _post(base_url, api_key, payload, timeout)
        dt = time.time() - t0
        if not r["ok"]:
            return {"ok": False, "latency": round(dt, 1),
                    "error": r.get("err") or f"HTTP {r.get('code')}",
                    "code": r.get("code")}
        content, reasoning = _extract(r["data"])
        u = r["data"].get("usage") or {}
        return {"ok": True, "latency": round(dt, 1), "content_len": len(content),
                "reason_len": len(reasoning), "out_tokens": u.get("completion_tokens") or 0,
                "preview": content[:60]}

    base = call({})
    if not base["ok"]:
        return {
            "verdict": "baseline_failed",
            "baseline": base,
            "strategies": [],
            "recommended": None,
            "note": f"基线调用就失败了（{base.get('error')}），无法探测。"
                    "先确认 base_url / api_key / model 是否正确。",
        }

    # 基线是否真的在思考：有推理内容，或正文为空但输出 token 不少
    thinks = base["reason_len"] > 0 or (
        base["content_len"] == 0 and base.get("out_tokens", 0) > 50)
    base["thinks"] = thinks

    rows: list[dict] = []
    winners: list[dict] = []
    for name, extra in THINK_OFF_STRATEGIES:
        r = call(extra)
        row = {"name": name, **{k: v for k, v in r.items() if k != "preview"}}
        if r["ok"]:
            row["speedup"] = round(base["latency"] / r["latency"], 2) if r["latency"] else 0
            # 判定成功：不再返回推理内容，且正文非空
            if r["reason_len"] == 0 and r["content_len"] > 0:
                row["effective"] = True
                winners.append({"name": name, "params": extra,
                                "latency": r["latency"], "speedup": row["speedup"]})
            else:
                row["effective"] = False
        else:
            row["effective"] = False
        rows.append(row)

    if winners:
        winners.sort(key=lambda w: w["latency"])
        best = winners[0]
        return {
            "verdict": "supported",
            "baseline": base,
            "strategies": rows,
            "recommended": best,
            "note": (f"基线 {base['latency']}s（推理 {base['reason_len']} 字）→ "
                     f"关闭后 {best['latency']}s，提速 {best['speedup']}x。"
                     f"推荐参数 {json.dumps(best['params'], ensure_ascii=False)}"),
        }

    return {
        "verdict": "unsupported",
        "baseline": base,
        "strategies": rows,
        "recommended": None,
        "note": ("所有候选策略都无效（仍返回推理内容或正文为空）。"
                 "对策：把 max_tokens 提到 3-4 倍（推理会消耗输出额度），或换模型。"
                 if thinks else
                 "基线本身就没观察到思考内容，这个模型可能不支持思考模式，"
                 "无需关闭。"),
    }


# ====================================================================
# 能力二：探测并发上限
# ====================================================================

def probe_concurrency(
    base_url: str, api_key: str, model: str,
    levels: list[int] | None = None, requests_per_level: int = 12,
    in_tokens: int = 1500, out_tokens: int = 500,
    think_off: dict | None = None, timeout: int = 400,
    gap_seconds: float = 5.0,
) -> dict:
    """逐档加压，返回并发上限。

    **取「成功率 100% 档位里吞吐最高的」**，不是取吞吐最高那档：
    过载点的吞吐数字看着漂亮，但那只是「成功请求数 / 总耗时」的假象，
    实际有一半请求在 429 重试。

    返回：

        {
          "verdict": "ok" | "no_full_success",
          "rows": [{"concurrency", "success", "total", "throughput", "tpm", "p50", "errors"}],
          "recommended": {"concurrency", "success", "throughput", "tpm"} | None,
          "note": "人话解释"
        }
    """
    levels = levels or [1, 2, 3, 5, 10, 15]
    prompt = build_probe_prompt(in_tokens)
    extra = think_off or {}

    def call(_i: int) -> dict:
        payload = {"model": model,
                   "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": out_tokens, "temperature": 0.3, **extra}
        t0 = time.time()
        r = _post(base_url, api_key, payload, timeout)
        dt = time.time() - t0
        if not r["ok"]:
            return {"ok": False, "latency": dt, "code": r.get("code"),
                    "error": r.get("err") or f"HTTP {r.get('code')}"}
        content, _ = _extract(r["data"])
        u = r["data"].get("usage") or {}
        return {"ok": bool(content), "latency": dt,
                "in": u.get("prompt_tokens") or 0,
                "out": u.get("completion_tokens") or 0}

    def run_level(conc: int) -> dict:
        lock = threading.Lock()
        res: list = [None] * requests_per_level

        def worker(i: int) -> None:
            r = call(i)
            with lock:
                res[i] = r

        t0 = time.time()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            list(ex.map(worker, range(requests_per_level)))
        total = time.time() - t0
        ok = [r for r in res if r and r["ok"]]
        tokens = sum((r.get("in", 0) + r.get("out", 0)) for r in ok)
        errs = [r.get("code") or r.get("error") for r in res if r and not r["ok"]]
        return {
            "concurrency": conc,
            "success": len(ok),
            "total": requests_per_level,
            "seconds": round(total, 1),
            "throughput": round(len(ok) / total, 2) if total else 0,
            "tpm": int(tokens / total * 60) if total else 0,
            "p50": round(statistics.median([r["latency"] for r in ok]), 1) if ok else 0,
            "errors": [e for e in errs[:3] if e],
        }

    rows: list[dict] = []
    for i, conc in enumerate(levels):
        rows.append(run_level(conc))
        if i < len(levels) - 1 and gap_seconds > 0:
            time.sleep(gap_seconds)

    full = [r for r in rows if r["success"] == r["total"]]
    pool = full or rows
    if not pool:
        return {"verdict": "no_full_success", "rows": rows, "recommended": None,
                "note": "全部档位都失败，端点不可用。"}
    best = max(pool, key=lambda r: (r["success"] / max(r["total"], 1), r["throughput"]))

    note = (f"并发 {best['concurrency']} 时 {best['success']}/{best['total']} 成功，"
            f"吞吐 {best['throughput']} req/s，约 {best['tpm']} TPM。")
    if not full:
        note += " 注意：没有任何一档 100% 成功，这是成功率最高者，不建议直接采用。"
    else:
        note += f" 建议写入 provider.max_concurrent_hint = {best['concurrency']}。"

    return {
        "verdict": "ok" if full else "no_full_success",
        "rows": rows,
        "recommended": best,
        "note": note,
    }
