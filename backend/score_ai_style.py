"""AI 味量化对照：三段生成（demo 语料时代→ 现在真实语料）逐项打分。

指标来源（都是可机检的硬特征，不靠主观判断）：
1.金句复述：是否复述了已拦掉的假坑原文 / demo 语料金句
2. 段尾升华：末段是否以总结/拔高/抒情收尾
3. 情绪直陈：贴标签式情绪描写
4. 对话讲设定：人物台词里是否出现设定名词直陈
5. 比喻密度：每百字的「像/如同」数（AI 腔典型高比喻）
6. 段长规整度：各段长度的变异系数（AI 腔段落等长）
"""
import json
import re
import sqlite3
import statistics

con = sqlite3.connect("novel_demo.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT segment_no, LENGTH(text) n, text FROM generations "
    "WHERE project_id=1 ORDER BY id"
).fetchall()

GOLDEN = ["不会被遗忘", "最贵的代价"]
EMO = r"(眼神里|瞳孔|眸中|闪过一丝|涌上心头|皆是|满是|无比)"
SUMMARY_END = r"(不只是|不仅|更是|原来|终于明白|从此|意味着|他不知道|很多年后)"
METAPHOR = r"(像[^，。！？]{1,12}[，。！？]|如同|彷佛|宛如)"


def score(text):
    n = max(len(text), 1)
    paras = [p for p in text.split("\n") if p.strip()]
    lens = [len(p) for p in paras] or [1]
    tail = paras[-1] if paras else ""
    return {
        "字数": n,
        "金句复述": [g for g in GOLDEN if g in text],
        "段尾升华": bool(re.search(SUMMARY_END, tail[-45:])),
        "情绪直陈": len(re.findall(EMO, text)),
        "比喻密度": round(len(re.findall(METAPHOR, text)) / n * 100, 2),
        "段长变异": round(statistics.pstdev(lens) / max(statistics.mean(lens), 1), 2),
        "对话占比": round(
            sum(1 for p in paras if p.strip().startswith(('"', "“")))
            / max(len(paras), 1), 2),
    }


print(f"{'段':>3} {'字数':>5} {'金句':>5} {'升华':>4} {'情绪':>4} "
      f"{'比喻%':>6} {'段长CV':>6} {'对话%':>5}")
for r in rows:
    s = score(r["text"])
    print(f"{r['segment_no']:>3} {s['字数']:>5} "
          f"{('有' if s['金句复述'] else '无'):>4} "
          f"{('有' if s['段尾升华'] else '无'):>4} "
          f"{s['情绪直陈']:>4} {s['比喻密度']:>6} {s['段长变异']:>6} {s['对话占比']:>5}"
          + (f"   ← {'/'.join(s['金句复述'])}" if s["金句复述"] else ""))

print("\n判读：")
print("  金句=无 / 升华=无 / 情绪=0 → 反 AI 腔硬指标全过")
print("  比喻密度越低越好（<1.0自然，>2.0 偏 AI 腔）")
print("  段长变异越大越好（<0.35 说明段落等长，AI 腔特征）")
con.close()