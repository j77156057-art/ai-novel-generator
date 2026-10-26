"""真机验证：闸门修复后生成一段，检查是否还有金句复述/填坑过急。"""
import json
import re
import urllib.request

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:8000" + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(op.open(req, timeout=180).read().decode())


res = post("/api/generate", {"project_id": 1, "novel_id": 1, "max_chars": 700})
text = res["text"]
print(f"segment_no={res['segment_no']}  字数={len(text)}")
print("=" * 60)
print(text)
print("=" * 60)

# AI 味硬指标：金句式复述（与已拦掉的假坑原句高度重合）
GOLDEN = ["不会被遗忘", "最贵的代价"]
print("\n=== AI 味指标 ===")
for g in GOLDEN:
    hit = g in text
    print(f"  {'FAIL仍复述' if hit else 'PASS 未复述'}  金句「{g}」")

#段尾升华 heuristic：末段以总结/抒情收尾
paras = [p for p in text.split("\n") if p.strip()]
last = paras[-1][-40:] if paras else ""
print(f"  末段结尾: ...{last}")

# 情绪直陈
emo = re.findall(r"(眼神里|瞳孔|眸中|闪过一丝|涌上|皆是|满是)", text)
print(f"  {'FAIL 情绪直陈' if emo else 'PASS 无情绪直陈标签'}  命中={emo}")

with open("gate_result.json", "w", encoding="utf-8") as f:
    json.dump({"segment_no": res["segment_no"], "text": text,
               "golden_hits": [g for g in GOLDEN if g in text]}, f,
              ensure_ascii=False, indent=2)