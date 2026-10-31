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


def _slug(name: str) -> str:
    """与 `verify_acceptance_real._slug` 同款（缓存文件名要对得上）。"""
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in (name or ""))
    return (out or "x")[:40]


def _verdicts(cache_path: Path) -> dict[str, str]:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    verdicts = data.get("verdicts", data) if isinstance(data, dict) else {}
    return {w: v for w, v in verdicts.items() if v} if isinstance(verdicts, dict) else {}


def check_judged_residue(packet: Path, samples: str) -> list[str]:
    """口径 1：判定成专名的词，必须一个都不在题面里。

    ## 为什么必须**按书核对**（2026-10-31 修）

    原来是把**每个缓存文件**里的专名拿到**整份题面**里搜 ——
    跨书误报很严重：同一个词在 A 书里是专名（「云深」在《回声年代》里判成
    character）、在 B 书里不是（《道途魔典》里判成不是专名），
    只要它出现在题面的**任何一侧**就被报成残留。

    ⇒ 现在按题逐题核对：**这一题的人类侧片段**只用**它那本书**的缓存查，
    **我们侧片段**只用该臂的缓存查 ⇒ 不再有跨书误报。
    """
    key_path = packet / "blind_key.json"
    if not key_path.exists():
        print("[1] 跳过（包里没有 blind_key.json，没法按书核对）")
        return []
    try:
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print("[1] 跳过（blind_key.json 读不出来）")
        return []
    meta = {it.get("sample_id"): it for it in key.get("items", [])}
    pairs = parse_items(samples)
    if not meta or not pairs:
        print("[1] 跳过（题面或密封文件里没解析出题目）")
        return []

    leaked: list[str] = []
    for sid, a_text, b_text in pairs:
        it = meta.get(sid)
        if not it:
            continue
        human_side = a_text if it.get("slot_A") == "human" else b_text
        our_side = b_text if it.get("slot_A") == "human" else a_text
        for cache_path in (
            packet / f"blind_names_human_{_slug(it.get('human_source', ''))}.json",
            packet / f"blind_names_ours_{_slug(it.get('arm', ''))}.json",
        ):
            if not cache_path.exists():
                continue
            names = _verdicts(cache_path)
            scope = human_side if cache_path.name.startswith("blind_names_human") else our_side
            hits = [w for w in names if w in scope]
            if hits:
                print(f"[1] {sid}　{cache_path.name}：仍出现在这一题里 {len(hits)} {hits[:10]}")
                leaked += hits
    if not leaked:
        print("[1] 按书核对：判定成专名的词，没有一个还留在题面里")
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
                # ⚠️ 这里必须用 **side**，不能写死 "A" ——
                # 换题时 buf 里装的是**上一题的 B 段**（遇到 "**B**" 时 buf 被清空过），
                # 写死 "A" 会把 B 段记成 A 段 ⇒ 重组时缺 "B" ⇒ **一题都解析不出来**
                # （实测：这个 bug 让口径 4 一直在「跳过」）。
                items.append((cur_id, side, "\n".join(buf)))
            cur_id, side, buf = "", "", []
            head = line.strip()
            # ⚠️ 题面里的题号用的是**全角括号**「（s01）」——
            # 只认 ASCII 括号会永远提取不到题号 ⇒ 一题都解析不出来
            # （实测：这个 bug 让口径 1 与口径 4 一直在「跳过」）。
            if ("(" in head and head.endswith(")")) or \
                    ("（" in head and head.endswith("）")):
                idx = max(head.rfind("("), head.rfind("（"))
                cur_id = head[idx + 1:-1]
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
        elif da and db:
            # 两侧都有主导题材**且相同** ⇒ 词表口径下也是同一题材 ⇒ 不用看
            continue
        elif da or db:
            print(f"[4] {sid}：只有一侧露题材指纹（A={da or '无'}　B={db or '无'}）"
                  "⇒ 一侧看不出题材（要看一眼）")
            bad.append(sid)
    print(f"[4] 逐题题材核对：{len(pairs)} 题，{len(bad)} 题需要人眼过一遍")
    return bad


def check_flavor_asymmetry(packet: Path, backend: str = "") -> list[str]:
    """口径 5：**世界观级子题材指纹对称**（完全不用 LLM）。

    口径 4 用的是粗题材词表（分不清军旅/宅斗/系统流这些**闭集外**的子题材），
    这一遍用 `acceptance_lib.flavor_asymmetry` 单独查一遍：
    某一类子题材指纹**只在一侧**出现 ⇒ 评者能靠题材定位 ⇒ 要看一眼。

    ⚠️ 词表口径会有误判（这套词表本来就是**粗筛**）⇒ 命中是「要看一眼」，
    不是「一定泄露」；但**出包时这一条是硬门槛**（不对称就换窗口）。
    """
    _backend_on_path(backend)
    from acceptance_lib import flavor_asymmetry  # noqa: E402

    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    pairs = parse_items(samples)
    if not pairs:
        print("[5] 跳过（题面里没解析出题目）")
        return []
    bad: list[str] = []
    for sid, a, b in pairs:
        asym = flavor_asymmetry(a, b)
        if asym:
            detail = "、".join(
                f"{x['class']}（只在{x['only_in']}侧，{x['hits_a']} vs {x['hits_b']}）"
                for x in asym)
            print(f"[5] {sid}：子题材指纹不对称 {detail} ⇒ 要看一眼")
            bad.append(sid)
    print(f"[5] 子题材指纹对称：{len(pairs)} 题，{len(bad)} 题需要人眼过一遍")
    return bad


def check_style_discriminability(packet: Path, backend: str = "") -> list[str]:
    """口径 6：**逐题的写法不对称**（完全不用 LLM）。

    前五条口径查的都是**内容**（专名 / 题材 / 痕迹）。
    这一条查的是**写法** —— 实测（2026-10-31 盲评第一次真跑），
    八道闸门全绿、`blind=true`，但规则「出现『X道：“…”』的那侧 = 人类侧」
    在 10 对可判题里**判对 10**（精确率 100%）⇒ 评者能靠写法定位。

    ## 为什么这里**只能提示、不能硬拦**

    判别力（哪条规则能定位哪边是「我们」）**需要密封文件** ——
    题面里 A/B 是**逐题随机**的，没有 `blind_key.json` 就不知道哪边是谁。
    ⇒ 本脚本没有密封文件，**只能测「逐题两侧的写法不对���」**，
    那是**必要条件**，不是充分条件。

    ⚠️ 而逐题不对称**单靠本脚本也判不了「泄露」**：
    题面里 A/B 逐题随机 ⇒ 「道」字落在 A 侧还是 B 侧也是随机的
    ⇒ 从外面看是均衡的 ⇒ **看不出方向**。
    ⇒ 所以这一条只能当「**要看一眼**」（与口径 4 / 5 同级），
    **硬判定在出包器里**（`backend/verify_acceptance_real.py` 有密封文件，
    能算精确率）。

    ## 逐题不对称**为什么仍然值得看

    评者不需要知道哪边是「我们」—— 他只要学会「**挑带『X道：“…”』的那一侧**」。
    人类侧 11/18 题有、我们侧 1/18 题有 ⇒ 他这么挑就能拿 10/18。
    ⇒ 逐题不对称的题数，就是他能这么挑对的次数。
    """
    _backend_on_path(backend)
    from acceptance_lib import attribution_discriminability  # noqa: E402
    from app.style.attribution import attribution_profile  # noqa: E402

    samples = (packet / "blind_samples.md").read_text(encoding="utf-8")
    pairs = parse_items(samples)
    if not pairs:
        print("[6] 跳过（题面里没解析出题目）")
        return []

    # —— 逐题：两侧的「道」字派归属是否一边有、一边没有 ——
    one_sided = 0
    for sid, a, b in pairs:
        pa = attribution_profile(a)
        pb = attribution_profile(b)
        if (pa["front_dao"] > 0) != (pb["front_dao"] > 0):
            one_sided += 1
    print(f"[6] 逐题「道」字派归属一边有一边无：{one_sided}/{len(pairs)} 题"
          "（评者只要学会「挑有『X道：“…”』的那一侧」就能用）")

    res = attribution_discriminability([a for _s, a, _b in pairs],
                                       [b for _s, _a, b in pairs])
    for r in res.get("rules", []):
        print(f"[6]    {r['rule']}：可判 {r['decidable']}　判对 {r['correct']}"
              f"　精确率 {r['precision']:.0%}"
              "（⚠️ 没有密封文件 ⇒ A/B 逐题随机 ⇒ 这个精确率**没有意义**）")
    bad = [sid for sid, a, b in pairs
           if (attribution_profile(a)["front_dao"] > 0)
           != (attribution_profile(b)["front_dao"] > 0)]
    print(f"[6] 写法不对称：{len(pairs)} 题，{len(bad)} 题需要人眼过一遍"
          "（**硬判定要密封文件**，出包器里才有）")
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
    check_flavor_asymmetry(packet, args.backend)
    style_bad = check_style_discriminability(packet, args.backend)

    print("=" * 60)
    if leaked:
        print(f"⛔ 判定过的专名仍留在题面里 {len(leaked)} 处 ⇒ 这个包不能发出去")
        return 1
    if style_bad:
        # ⚠️ 写法这一条**只能提示**：没有密封文件就不知道哪边是「我们」，
        # 判别力测不了 ⇒ 硬判定在出包器里（它有密封文件）。
        print(f"⚠️ 写法上逐题不对称 {len(style_bad)} 题 ⇒ 要人眼过一遍"
              "（**硬判定要有密封文件**，出包器里才有）")
    print("✅ 判定名残留 0 处（口径 2 / 口径 4 的命中要人眼过一遍："
          "它们可能是普通词、也可能是题材差异）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
