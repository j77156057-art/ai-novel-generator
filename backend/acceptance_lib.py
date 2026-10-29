"""验收跑共享库：指标 + 种子 + 人类基线（离线跑与真模型跑共用）。

为什么单独抽一个库
------------------------------------------------------------------------------
两轮验收必须用**同一套指标、同一份阈值**，否则两轮的数不能比：

- `verify_acceptance.py`      —— 离线确定性 provider（无模型、无网络）
- `verify_acceptance_real.py`  —— 真模型端到端（本次新增）

阈值不是拍的：先在**真实语料**上量出人类散文的分布，阈值取该分布的分位数
（重复率 p90 / 比喻密度 p90 / 句长 CV p10）。基线语料从环境变量
`ACCEPTANCE_CORPUS` 指定，默认取仓库内 `backend/novel_dev.db`（开发库）。

⚠️ 基线语料一旦换了，**阈值必须重算** —— 不能沿用别的语料上的分位数
（人类基线只有 3 本网文这件事本身就是已知局限，见 docs/acceptance-metrics.md §8）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


# --------------------------------------------------------------------------- #
# 指标实现（纯函数）
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")


def _clean(text: str) -> str:
    """去掉空白与换行：中文正文统计不该被排版空行影响。"""
    return _WS.sub("", text or "")


def grams(text: str, n: int = 3) -> set[str]:
    t = _clean(text)
    if len(t) < n:
        return set()
    return {t[i:i + n] for i in range(len(t) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 比喻是 AI 腔最稳的硬特征之一：真人写得再花，也不会每百字挂一个「像…」
_METAPHOR = re.compile(r"(像[^，。！？；：]{1,12}|如同|宛如|仿佛|彷佛)")


def metaphor_per100(text: str) -> float:
    t = _clean(text)
    if not t:
        return 0.0
    return 100.0 * len(_METAPHOR.findall(t)) / len(t)


_SENT_SPLIT = re.compile(r"[。！？…]+")


def sent_cv(text: str) -> float | None:
    """句长变异系数：AI 腔句子长度往往过于整齐，真人散文起伏更大。

    方向是「越大越像人」——所以阈值是**下界**（不得低于人类 p10）。
    句子太少时返回 None（不参与判定），而不是硬算一个噪音值。
    """
    sents = [s for s in _SENT_SPLIT.split(_clean(text)) if len(s) >= 4]
    if len(sents) < 5:
        return None
    m = statistics.mean(len(s) for s in sents)
    if m <= 0:
        return None
    return statistics.pstdev(len(s) for s in sents) / m


def pctl(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


def text_metrics(text: str) -> dict:
    """一段正文的三项指标 + 字数（真模型跑与离线跑共用同一把尺）。"""
    cv = sent_cv(text)
    return {
        "chars": len(_clean(text)),
        "rep": 0.0,              # 相邻段重复率需要上下文，单独算
        "meta": metaphor_per100(text),
        "cv": cv,
        "grams": grams(text),
    }


# --------------------------------------------------------------------------- #
# 种子：一个够长的短篇设定，逐段续写
# --------------------------------------------------------------------------- #

SEED_CHARS = [
    {"name": "林夜", "role": "主角", "trait": "手指会发光；记不清自己是怎么倒下的"},
    {"name": "苏晚", "role": "接线人", "trait": "冷静，话短，先给选择再给解释"},
    {"name": "陆见微", "role": "对手", "trait": "总是笑着说话，目的不明"},
]
SEED_FORES = ["那道金光到底是什么", "陆见微为何认得这枚铜钥匙"]
SEED_REALMS = ["炼气", "筑基", "金丹", "元婴"]

PLOT_BIBLE = {
    "synopsis": "林夜在巷口醒来，霓虹的余光还黏在视网膜上。他记不清自己是怎么倒下的，"
                "只记得胸口那道凉意。他的手指正在发光，像没调好的灯管。",
    "direction": "三卷：巷口 → 接线人 → 锚。第一卷弄清金光来历，第二卷被迫站队。",
    "characters": SEED_CHARS,
    "foreshadowing": SEED_FORES,
    "conflicts": ["林夜要弄清金光的来历", "陆见微在利用他，而他还没察觉"],
    "relationships": [{"from": "林夜", "to": "苏晚", "type": "同路人"}],
    "realm_order": SEED_REALMS,
}


# --------------------------------------------------------------------------- #
# 人类散文基线 —— 阈值的来源
# --------------------------------------------------------------------------- #

def corpus_texts(spec: str | os.PathLike, max_chars: int | None = None) -> list[tuple[str, str]]:
    """读基线语料，返回 ``[(来源名, 全文)]``。

    支持三种来源：
    - ``.db``：sqlite，取 ``novel_chunks`` 的 ``text``（按 novel_id 聚成整本）
    - ``.txt``：单个文本文件
    - 目录：目录下所有 ``.txt``（忽略 README 等说明文件）

    读进来的文本会过一遍**项目自带的语料清洗**（`app.ingestion.clean`）——
    盗版站注入的 SEO 水印会稀释句长/比喻密度等指标（见 app/corpus_health.py）。

    `max_chars`：**先截断再清洗**（顺序反了会白洗几百万字 —— 清洗是最贵的一步）。
    """
    path = Path(spec)
    if not path.exists():
        raise FileNotFoundError(f"基线语料不存在：{path}")

    out: list[tuple[str, str]] = []
    if path.is_dir():
        for p in sorted(path.glob("*.txt")):
            if p.name.startswith("README"):
                continue
            out.append((p.stem, p.read_text(encoding="utf-8", errors="replace")))
    elif path.suffix.lower() == ".db":
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select novel_id, text from novel_chunks order by novel_id, chapter_no, idx"
            ).fetchall()
        finally:
            con.close()
        by_novel: dict[int, list[str]] = {}
        for nid, t in rows:
            by_novel.setdefault(nid, []).append(t)
        for nid, parts in by_novel.items():
            out.append((f"novel{nid}", "".join(parts)))
    else:
        out.append((path.stem, path.read_text(encoding="utf-8", errors="replace")))

    return [(name, _clean_corpus(text[:max_chars] if max_chars else text))
            for name, text in out]


def _clean_corpus(text: str) -> str:
    """清洗基线语料（复用项目自己的清洗实现，不另造一套）。

    大文本走 `clean_consumption_text`（正则精确删站点标记，快）；
    它解决不了的水印（句末植入式）由 `clean_novel_text` 的统计判据兜底。
    """
    try:
        from app.ingestion.clean import clean_consumption_text, clean_novel_text
    except Exception:  # noqa: BLE001 —— 清洗不可用时**原样返回**（并在报告里体现）
        return text
    try:
        cleaned, _ = clean_consumption_text(text)
    except Exception:  # noqa: BLE001
        cleaned = text
    try:
        cleaned2, _ = clean_novel_text(cleaned)
        return cleaned2
    except Exception:  # noqa: BLE001
        return cleaned


def human_baseline(texts: list[tuple[str, str]], chunk_chars: int = 3000,
                   max_chars: int | None = None) -> dict:
    """在真实语料上算同样的三个指标，作为阈值来源。

    切法与 `verify_sentinel.py` H 段一致（3000 字一段），避免两处口径不同
    导致「基线」和「被测对象」其实不是一回事。

    `max_chars`：每本源语料最多取多少字（默认全取）。用来把大语料截到
    与原基线同量级，避免基线被一本超长书主导。
    """
    reps: list[float] = []
    metas: list[float] = []
    cvs: list[float] = []
    n_chars = 0
    n_segs = 0
    books = []
    for name, text in texts:
        if max_chars:
            text = text[:max_chars]
        segs = [text[i:i + chunk_chars] for i in range(0, len(text), chunk_chars)]
        n_chars += len(text)
        n_segs += len(segs)
        books.append({"name": name, "chars": len(text), "segs": len(segs)})
        gs = [grams(s) for s in segs]
        reps.extend(jaccard(a, b) for a, b in zip(gs, gs[1:]))
        for s in segs:
            metas.append(metaphor_per100(s))
            c = sent_cv(s)
            if c is not None:
                cvs.append(c)
    return {
        "books": books, "novels": len(texts), "chars": n_chars, "segs": n_segs,
        "rep": reps, "meta": metas, "cv": cvs,
    }


def thresholds(base: dict) -> dict:
    """从基线导出三条阈值（重复率上界、比喻密度上界、句长 CV 下界）。"""
    return {
        "rep_max": pctl(base["rep"], 0.9),
        "meta_max": pctl(base["meta"], 0.9),
        "cv_min": pctl(base["cv"], 0.1),
    }


def baseline_corpus_spec() -> str:
    """基线语料来源：环境变量优先，否则用开发库。"""
    env = os.environ.get("ACCEPTANCE_CORPUS", "").strip()
    if env:
        return env
    return str(HERE / "novel_dev.db")
