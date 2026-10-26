"""AI 味量化对照：逐段打分（默认 demo 库；可传参指向任意库/项目/段）。

指标来源（都是可机检的硬特征，不靠主观判断）：
1.金句复述：是否复述了已拦掉的假坑原文 / demo 语料金句
2. 段尾升华：末段是否以总结/拔高/抒情收尾
3. 情绪直陈：贴标签式情绪描写
4. 对话讲设定：人物台词里是否出现设定名词直陈
5. 比喻密度：每百字的「像/如同」数（AI 腔典型高比喻）
6. 段长规整度：各段长度的变异系数（AI 腔段落等长）

用法：
    python score_ai_style.py                       # 原默认：novel_demo.db / 项目 1 全部段
    python score_ai_style.py --db novel_dev.db --project 5 --segments 1 2
                                                   # 指定库/项目/段号做对照

注意：**指标定义（GOLDEN/EMO/SUMMARY_END/METAPHOR 与 score() 的算法）一字未改**，
本次只把「读哪个库、哪个项目、哪些段」参数化 —— 否则对照就失去可比性。
"""
import argparse
import re
import sqlite3
import statistics

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


def fetch_rows(db: str, project_id: int, segments: list[int] | None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT segment_no, LENGTH(text) n, text FROM generations "
            "WHERE project_id=?"
        )
        params: list = [project_id]
        if segments:
            placeholders = ",".join("?" for _ in segments)
            sql += f" AND segment_no IN ({placeholders})"
            params.extend(segments)
        sql += " ORDER BY id"
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="novel_demo.db", help="sqlite 库路径（默认 novel_demo.db）")
    ap.add_argument("--project", type=int, default=1, help="项目 id（默认 1）")
    ap.add_argument("--segments", type=int, nargs="*", default=None,
                    help="只统计这些段号（默认全部）")
    args = ap.parse_args()

    rows = fetch_rows(args.db, args.project, args.segments)
    print(f"库={args.db} 项目={args.project} "
          f"段={args.segments or '全部'}（{len(rows)} 段）")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
