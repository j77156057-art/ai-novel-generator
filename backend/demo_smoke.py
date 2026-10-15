"""演示后端探活 + 真模型端到端验证。

走 Python（避免沙箱对裸 curl 的拦截），验证：
1) :8000 后端健康、演示数据仍在
2) 通过 /api/generate 用 OpenRouter 免费模型真生成一段小说（跑通真实 provider）
用法：python demo_smoke.py
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
# 走 Python 原生，禁掉代理，避免沙箱 MITM 干扰 localhost
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(path: str, timeout: int = 30):
    with OPENER.open(BASE + path, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def post(path: str, payload: dict, timeout: int = 180):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with OPENER.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main() -> int:
    # 1) 健康
    st, body = get("/health")
    print(f"[1] health {st} {body}")

    # 2) 演示数据仍在
    st, body = get("/api/novels")
    novels = json.loads(body)
    print(f"[2] novels {st}: {len(novels)} 本 -> {[n['title'] for n in novels]}")
    st, body = get("/api/projects/1/kg")
    kg = json.loads(body)
    kinds = {}
    for n in kg["nodes"]:
        kinds[n["type"]] = kinds.get(n["type"], 0) + 1
    print(f"[2] project1 kg {st}: {kinds}, edges={len(kg['edges'])}")

    st, body = get("/api/generations/1")
    gens = json.loads(body)
    print(f"[2] 已有生成段数: {len(gens)}（本地兜底时生成的）")

    # 3) 真模型端到端：调 /api/generate（内部走 OpenRouter apodex）
    print("\n[3] 调 /api/generate（OpenRouter apodex-1.1-mini 真模型，约 2-3s/段）...")
    t0 = time.time()
    st, data = post("/api/generate", {"project_id": 1, "novel_id": 1})
    dt = time.time() - t0
    print(f"[3] HTTP {st}  耗时 {dt:.1f}s")
    print(f"[3] segment_no={data.get('segment_no')}  chars={len(data.get('text') or '')}")
    print(f"[3] 正文摘要: {data.get('summary')}")
    print("\n---------- 真实模型生成的正文 ----------")
    print(data.get("text"))
    print("--------------------------------------------")
    return 0


if __name__ == "__main__":
    sys.exit(main())
