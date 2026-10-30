r"""盲评包泄露扫描（**不依赖出包流程自己的判定**）。

用法：
    python scripts/leak_scan.py --packet <盲评包目录> [--corpus <人类侧语料目录>]

包里要有 `blind_samples.md`（题面）与 `blind_names_*.json`（判定缓存）。

跑四条口径：

1. **判定名残留** —— 把判定缓存里「判成专名」的词逐个回查题面。
   出现次数必须为 **0**（这查的是「遮了没有」）。
2. **排他 + 人名形状**（**完全不用 LLM**） —— 对人类侧每本书取
   「本书高频、别的书里几乎没有」的词，再过一遍人名形状
   （`app.kg.extractor._valid_name`），剩下的就是这本书特有的人名形状词；
   回查题面里有没有 ⇒ 这条链路的判别力接近人眼。
   ⚠️ 命中**不等于**泄露：普通词也会命中，必须逐个看上下文。
3. **题面级复查**（可选，需要模型）—— 把整份题面交给模型「只报还是专名的词」。
   这一遍与出包时用的判定链路不同（输入/提示词/缓存都不同），能抓住独立漏网的。
4. **逐题题材指纹核对**（**完全不用 LLM**）—— 出包时题材是**模型**判的，
   这一遍用**词表**判，两把尺互相独立。命中 ⇒ 两侧题材词汇分布不同，
   **要人眼过一遍**（词表已知会误判）。

退出码：1 = 判定名还有残留（**这个包不能发出去**）；0 = 通过。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _backend_on_path(backend: str = "") -> Path:
    if backend:
        cand = Path(backend)
        if cand.exists():
            sys.path.insert(0, str(cand))
            return cand
        raise SystemExit(f"--backend 指向的目录不存在：{backend}")
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "backend"
        if cand.exists():
            sys.path.insert(0, str(cand))
            return cand
    raise SystemExit("找不到 backend 目录（用 --backend 指到仓库的 backend/，"
                     "或把脚本放在仓库的 skills/<name>/scripts/ 下）")


def check_judged_residue(packet: Path, samples: str) -> list[str]:
    """口径 1：判定成专名的词，必须一个都不在题面里。"""
    leaked: list[str] = []
    for cache_path in sorted(packet.glob("blind_names_*.json")):
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        verdicts = data.get("verdicts", data) if isinstance(data, dict) else {}
        names = [w for w, v in verdicts.items() if v]
        hits = [w for w in names if w in samples]
        print(f"[1] {cache_path.name}：判成专名 {len(names)} 个，"
              f"仍出现在题面里的 {len(hits)} {hits[:10]}")
        leaked += hits
    return leaked


def check_exclusive_shape(packet: Path, corpus: Path | None,
                          backend: str = "") -> list[str]:
    """口径 2：排他 + 人名形状（不用 LLM）。"""
    if not corpus or not corpus.exists():
        print("[2] 跳过（没给 --corpus）")
        return []
    _backend_on_path(backend)
    from app.kg.extractor import _valid_name  # noqa: E402
    from app.style.entities import pmi_candidates  # noqa: E402

    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    books: dict[str, dict] = {}
    for p in sorted(corpus.glob("*.txt")):
        books[p.name] = pmi_candidates(
            p.read_text(encoding="utf-8", errors="ignore"), min_count=20)

    hits: list[str] = []
    for name, cand in books.items():
        others = [c for n, c in books.items() if n != name]
        excl = [(w, c) for w, c in cand.items()
                if all(o.get(w, 0) <= 2 for o in others)]
        shaped = [(w, c) for w, c in excl if _valid_name(w)]
        found = [(w, c) for w, c in shaped if w in samples]
        print(f"[2] {name}：排他候选 {len(excl)} → 过人名形状 {len(shaped)} → "
              f"出现在题面里 {len(found)}")
        for w, c in sorted(found, key=lambda x: -x[1])[:30]:
            print(f"      {w}（本书 {c} 次）")
        hits += [w for w, _ in found]
    return hits


def check_text_level(packet: Path, provider=None, backend: str = "") -> list[str]:
    """口径 3：题面级复查（需要模型）。没给 provider 就跳过。"""
    if provider is None:
        print("[3] 跳过（没给 provider）")
        return []
    _backend_on_path(backend)
    from acceptance_lib import residual_proper_nouns  # noqa: E402

    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    left = residual_proper_nouns(samples, provider)
    print(f"[3] 题面级复查：残留专名 {len(left)} {left[:12]}")
    return left


def parse_items(samples: str) -> list[tuple[str, str, str]]:
    """把题面拆成 ``[(题号, A 文本, B 文本)]``。"""
    items: list[tuple[str, str, str]] = []
    cur_id, side, buf = "", "", []
    for line in samples.splitlines():
        if line.startswith("## 第"):
            if cur_id and side:
                items.append((cur_id, "A", "\n".join(buf)))
            cur_id, side, buf = "", "", []
            head = line.strip()
            if "(" in head and head.endswith(")"):
                cur_id = head[head.rfind("(") + 1:-1]
            continue
        if line.strip() in ("**A**", "**B**"):
            if cur_id and side:
                items.append((cur_id, side, "\n".join(buf)))
                buf = []
            side = line.strip().strip("*")
            continue
        if side and line.startswith("> "):
            buf.append(line[2:])
    if cur_id and side:
        items.append((cur_id, side, "\n".join(buf)))
    # 题面里 A、B 是交替出现的：重组成 (题号, A, B)
    paired: list[tuple[str, str, str]] = []
    pending: dict[str, dict[str, str]] = {}
    for sid, s, text in items:
        pending.setdefault(sid, {})[s] = text
    for sid, d in pending.items():
        if "A" in d and "B" in d:
            paired.append((sid, d["A"], d["B"]))
    return paired


def check_genre_mismatch(packet: Path, backend: str = "") -> list[str]:
    """口径 4：**逐题题材指纹**独立核对（**完全不用 LLM**）。

    出包时用的是**模型**判题材；这一遍是**词表**口径 —— 两把尺互相独立，
    词表弱（已知会误判），所以这里的命中**要人眼过一遍**：
    命中说明两侧的题材词汇分布不同，是**需要看一眼**的地方，
    不是「一定泄露」。
    """
    _backend_on_path(backend)
    from acceptance_lib import genre_dominant, genre_profile  # noqa: E402

    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    pairs = parse_items(samples)
    if not pairs:
        print("[4] 跳过（题面里没解析出题目）")
        return []
    bad: list[str] = []
    for sid, a, b in pairs:
        da = genre_dominant(genre_profile(a))
        db = genre_dominant(genre_profile(b))
        if da and db and da != db:
            print(f"[4] {sid}：A 主导={da}　B 主导={db} ⇒ 两侧题材不同（要看一眼）")
            bad.append(sid)
        elif da or db:
            print(f"[4] {sid}：只有一侧露题材指纹（A={da or '无'}　B={db or '无'}）"
                  "⇒ 一侧看不出题材（要看一眼）")
            bad.append(sid)
    print(f"[4] 逐题题材核对：{len(pairs)} 题，{len(bad)} 题需要人眼过一遍")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--packet", required=True, help="盲评包目录（含 blind_samples.md）")
    ap.add_argument("--corpus", default="", help="人类侧语料目录（*.txt）")
    ap.add_argument("--backend", default="",
                    help="仓库的 backend/ 目录（脚本不在仓库里时必填）")
    ap.add_argument("--judge-provider", default="",
                    help="跑口径 3 用的 provider（自己装好就传进来）")
    args = ap.parse_args()

    packet = Path(args.packet)
    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    print(f"题面 {len(samples)} 字")

    leaked = check_judged_residue(packet, samples)
    check_exclusive_shape(packet, Path(args.corpus) if args.corpus else None,
                          args.backend)
    if args.judge_provider:
        check_text_level(packet, args.judge_provider, args.backend)
    check_genre_mismatch(packet, args.backend)

    print("=" * 60)
    if leaked:
        print(f"⛔ 判定过的专名仍留在题面里 {len(leaked)} 处 ⇒ 这个包不能发出去")
        return 1
    print("✅ 判定名残留 0 处（口径 2 / 口径 4 的命中要人眼过一遍："
          "它们可能是普通词、也可能是题材差异）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
