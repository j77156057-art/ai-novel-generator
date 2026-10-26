"""列出 OpenRouter 当前所有免费模型（id 以 :free 结尾），按上下文长度排序。

用法：OPENROUTER_API_KEY=sk-or-... python list_free_models.py
key 只读环境变量，不落盘；也支持匿名拉取（部分接口不强制 key）。
"""

import json
import os
import sys
import urllib.request

URL = "https://openrouter.ai/api/v1/models"
KEY = os.environ.get("OPENROUTER_API_KEY", "")


def fetch() -> list[dict]:
    req = urllib.request.Request(URL)
    if KEY:
        req.add_header("Authorization", f"Bearer {KEY}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"拉取失败: {type(exc).__name__}: {exc}")
        sys.exit(1)
    return data.get("data", [])


def main() -> int:
    models = fetch()
    free = [m for m in models if str(m.get("id", "")).endswith(":free")]
    # 去重（同系列可能有多个变体），按上下文长度降序
    free.sort(key=lambda m: -(m.get("context_length") or 0))

    print(f"总模型数 {len(models)}，其中免费 {len(free)} 款\n")
    print(f"{'MODEL ID':<52} {'CONTEXT':>10}  MODIFIERS")
    print("-" * 100)
    for m in free:
        mid = m.get("id", "")
        ctx = m.get("context_length") or 0
        ctx_s = f"{ctx/1e6:.1f}M" if ctx >= 1_000_000 else f"{ctx//1000}K"
        arch = (m.get("architecture") or {})
        mods = []
        if arch.get("modality") == "text+image->text":
            mods.append("vision")
        pr = m.get("pricing") or {}
        # 输出价也为 0 的确认是真免费
        if (pr.get("completion") or "0") == "0":
            mods.append("free-out")
        print(f"{mid:<52} {ctx_s:>10}  {','.join(mods)}")

    # 导出精简列表供挑选
    out = [{"id": m.get("id"), "context": m.get("context_length")} for m in free]
    with open("free_models.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n已写入 free_models.json（{len(out)} 款）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
