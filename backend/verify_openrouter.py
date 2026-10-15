"""真实调用验证：OpenRouter 免费模型接入（M4.1）。

直接用项目里的 OpenRouterChatProvider 发真实请求，验证：
1) key 是否有效  2) :free 档是否真的供这个 model  3) 返回质量  4) reasoning 关闭是否生效。

用法：OPENROUTER_API_KEY=sk-or-... python verify_openrouter.py
key 只从环境变量读，不落盘。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.llm.provider import OpenRouterChatProvider  # noqa: E402

KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")

if not KEY:
    print("需要 OPENROUTER_API_KEY 环境变量")
    sys.exit(2)


def probe(provider: OpenRouterChatProvider, system: str, user: str, label: str) -> dict:
    t0 = time.time()
    try:
        out = provider.chat(system, user)
        dt = time.time() - t0
        print(f"\n=== {label} ===  ({dt:.1f}s)")
        print(out[:600])
        return {"ok": True, "len": len(out), "secs": round(dt, 1)}
    except Exception as exc:  # noqa: BLE001
        dt = time.time() - t0
        print(f"\n=== {label} ===  FAILED ({dt:.1f}s)")
        print(f"{type(exc).__name__}: {exc}")
        return {"ok": False, "err": str(exc)[:400]}


def main() -> int:
    provider = OpenRouterChatProvider(
        KEY, "https://openrouter.ai/api/v1", MODEL, reasoning=False
    )
    print(f"model = {provider.model}")
    print(f"endpoint = {provider.base_url}/chat/completions")
    print(f"extra_body = {provider.extra_body}")

    # 1) 连通性 + 身份探针
    r1 = probe(provider, "你是一个助手。", "用一句话回答：你是什么模型？", "1) 连通性")

    # 2) 真实小说续写探针（贴近 M4 实际用法：带角色/悬念约束的短续写）
    r2 = probe(
        provider,
        "你是长篇小说续写助手。角色：林夜。悬念：那光是什么。只输出正文，200字内。",
        "请续写开场：林夜推开旧宅的门，屋里有一束来历不明的光。请直接给正文。",
        "2) 小说续写质量",
    )

    print("\n--- 汇总 ---")
    print(json.dumps({"connectivity": r1, "continuation": r2}, ensure_ascii=False, indent=2))

    ok = r1.get("ok") and r2.get("ok")
    print("\n✅ 真实调用通过" if ok else "\n❌ 存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
