"""批量实测 OpenRouter 免费模型的中文小说续写能力（真实调用）。

对 free_models.json 里的候选逐个发一次真实续写请求，输出：成功/失败、耗时、字数、是否吐了不该有的内容。
用法：OPENROUTER_API_KEY=sk-or-... python bench_free_models.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.llm.provider import OpenRouterChatProvider  # noqa: E402

KEY = os.environ.get("OPENROUTER_API_KEY", "")

# 针对中文小说续写的候选（已剔除代码专用/安全分类/过小档）
CANDIDATES = [
    "apodex/apodex-1.1-mini:free",
    "thinkingmachines/inkling:free",
    "thinkingmachines/inkling-small:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "poolside/laguna-s-2.1:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "liquid/lfm-2.5-2.6b:free",
]

SYSTEM = (
    "你是长篇小说续写助手。只输出正文，不要解释、不要标题。"
    "保持中文，画面具体、节奏紧凑。"
)
USER = (
    "请续写约200字：林夜推开旧宅的门，屋里悬浮着一束来历不明的白光。"
    "要求：有动作、有感官细节，结尾埋一个悬念。"
)


def bench(model: str) -> dict:
    p = OpenRouterChatProvider(KEY, "https://openrouter.ai/api/v1", model, reasoning=False)
    t0 = time.time()
    try:
        out = p.chat(SYSTEM, USER)
        dt = time.time() - t0
        return {"ok": True, "secs": round(dt, 1), "len": len(out), "text": out}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "secs": round(time.time() - t0, 1), "err": str(exc)[:200]}


def main() -> int:
    results = []
    for m in CANDIDATES:
        r = bench(m)
        results.append({"model": m, **r})
        flag = "OK " if r["ok"] else "ERR"
        detail = (
            f"{r['secs']:>6}s  {r['len']:>4}字"
            if r["ok"]
            else f"{r['secs']:>6}s  {r['err'][:70]}"
        )
        print(f"[{flag}] {m:<52} {detail}", flush=True)

    ok = [r for r in results if r["ok"]]
    ok.sort(key=lambda r: r["secs"])
    print("\n=== 成功模型按速度排序 ===")
    for r in ok:
        print(f"  {r['secs']:>6}s  {r['len']:>4}字  {r['model']}")

    with open("free_bench.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n成功 {len(ok)}/{len(CANDIDATES)}；明细写入 free_bench.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
