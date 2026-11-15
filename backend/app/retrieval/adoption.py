"""段末「规则是否被采用」的五档判定（供作者 agent 与监察复用）。

旧口径只认字面参数：模型把规则改写成自己的句子（换人称、拆句、换同义物
件）就算「未采用」。项目 6 实测 32 次调用判出 adopted=0，人工复核至少 3 条
被改写式采用——判据本身失真，比漏判更糟。

## 五档口径

| verdict | 含义 | 典型证据 |
| --- | --- | --- |
| `adopted_literal` | 规则里的引号短语 / 数字标记原样出现 | 「接通但沉默」、30% |
| `adopted_rewrite` | 无字面重合，但规则的高区分度词/分句在正文里成簇出现 | 接通、身份 |
| `not_detected` | 规则里有**可核对标记**（引号短语/数字+单位）但正文里没出现 | 数字/引号全不在 |
| `unjudgeable` | **判不了**：没有可核对标记，词面信号也不足以判采用或未采用 | 纯抽象规则、零星弱重合 |
| `nav` | browse/similar 等导航调用，不参与采纳统计 | — |

`unjudgeable` 单独一档，不并进 `not_detected`：把「判不了」说成「没用」会
让 agent 反复重查同一场景，也会让反哺统计把好规则误删。

## 判定输入只取规则本身（2026-11-13 修正）

卡表的 `text` 形如「规则正文（证据：参考书原句）」，`rule` 工具还会再带回
`evidence` 字段。实测第 3 章：判定输入把参考书的证据引文也算进了规则词集，
高 IDF 词全落成参考书的人名/物件（`歪啊`、`户只`、`神喃`），与本项目正文的
覆盖率只有 0 / 0.077 / 0.123，真实改写被压成 `not_detected`。现在先用
`rule_body()` 剥掉「（证据：……）」与单独回传的证据行，只用**规则本身**与
正文比对；锚点调用改读卡表里的规则正文（锚点返回的是参考书正文，不能当判据）。

## 阈值实测（2026-11-13，project 6 共 45 次 rule/anchor 调用，12 段）

剥掉证据后规则正文的覆盖率分布是 0–0.21，只有 3 次 ≥0.15，且都命中 ≥2 个
高 IDF 词（`接通`+`身份`、`电话`+`身份` 这类真实改写）。另有 1 次靠单字
`问号` 蹭到 0.158，属于巧合。因此保留 `REWRITE_MIN_COVERAGE=0.15`，并新增
`MATCHED_TERMS_MIN=2`：阈值不放水，但拒绝「一个热词就判采用」。

## 为什么不写死本书词表

改写式判定需要「哪些词足够特别」。这里不维护实体词表，而是从**卡表自身**
统计二元组 IDF：`TermWeights.from_cards(catalog.cards)`。换一本书重建卡表，
权重自动跟着换；规则文本里出现的高 IDF 词才是证据。纯抽象规则没有高 IDF
词，自然落到 `unjudgeable`，不会硬判。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RetrievalLog
from app.retrieval.catalog_store import load_catalog
from app.retrieval.trace import fill_segment_adoption

VERDICT_LITERAL = "adopted_literal"
VERDICT_REWRITE = "adopted_rewrite"
VERDICT_NOT_DETECTED = "not_detected"
VERDICT_UNJUDGEABLE = "unjudgeable"
VERDICT_NAV = "nav"
VERDICTS = (
    VERDICT_LITERAL, VERDICT_REWRITE, VERDICT_NOT_DETECTED,
    VERDICT_UNJUDGEABLE, VERDICT_NAV,
)
ADOPTED_VERDICTS = (VERDICT_LITERAL, VERDICT_REWRITE)
NAV_TOOLS = ("browse", "similar", "search")

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|次|字|秒|分钟|句|段|行|人|步|天|年|倍|个)")
_QUOTED = re.compile(r"[「『“\"]([^」』”\"]{2,20})[」』”\"]")
_CLAUSE_SPLIT = re.compile(r"[，。；：、！？!?;,\n]")
#: 只由功能词组成的二元组不算内容词。
_STOP_CHARS = set(
    "的了着是在有和与或就不都也很这那什么一个我你他她它们地得而之其所以"
    "因为如果但是然后把被给对从到为以于里上下来去说会能要把可还又更最"
)
#: 出现在二元组首/尾时基本是跨词碎片（「话里」「每句」「句都」），
#: 不当证据；否则「对话里」会被切出一个高 IDF 的「话里」造成误判。
_EDGE_CHARS = set(
    "的了着是在有和与或就不都也很这那什么一个我你他她它们地得而之其"
    "里每句个把被给从到为以于上下来去会能要把可还又更最则即且并"
)
#: 覆盖率只看权重最高的前若干个词：规则长时尾部低权重词会把分母拉大。
FOCUS_TERMS = 6
#: 位置线索：规则说「章末/结尾」时，命中词必须落在正文尾部才算数。
_END_CUES = ("章末", "结尾", "末尾", "最后", "收尾")
_OPEN_CUES = ("开篇", "开头", "起笔", "开场")

#: 二元组 IDF 门槛：低于它的词太常见，当证据会误判。
MIN_IDF = 3.5
#: 改写式采纳所需的高 IDF 词总权重。
REWRITE_MIN_WEIGHT = 4.0
#: 高 IDF 词覆盖率门槛（避免一条规则只蹭到一个热词就判采用）。
REWRITE_MIN_COVERAGE = 0.15
#: 至少命中几个高 IDF 词才算「成簇」。实测单字`问号`能单独蹭到 0.158，
#: 一个词的重合无法排除巧合；真实改写（电话+身份）至少两个词。
MATCHED_TERMS_MIN = 2
#: 规则正文里的「（证据：……）」。卡表把参考书原句附在规则后面，判定时必须
#: 剥掉，否则高 IDF 词会被参考书的人名/物件占满。
_EVIDENCE_BLOCK = re.compile(r"[（(]\s*证据\s*[:：]\s*(.*?)[）)]", re.S)


def rule_body(text: str) -> str:
    """只保留规则正文：去掉「（证据：……）」与单独回传的证据行。

    卡表 `text` 与 `rule` 工具的返回都可能是「规则（证据：参考书原句）」，
    后面还可能再拼一行 `evidence`。证据是参考书的场景原句，与本项目正文天然
    低重合，混进判定会让真实改写全部掉到阈值以下。
    """
    raw = text or ""
    evidence = [m.strip() for m in _EVIDENCE_BLOCK.findall(raw)]
    body = _EVIDENCE_BLOCK.sub("", raw)
    if evidence:
        kept = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # 单独回传的 evidence 行通常就是括号里那一段（可能截断），去掉。
            if any(ev and (stripped in ev or ev.startswith(stripped[:12]))
                   for ev in evidence):
                continue
            kept.append(line)
        body = "\n".join(kept)
    return body.strip()


def _bigrams(text: str) -> set[str]:
    out: set[str] = set()
    for run in _CJK_RUN.findall(text or ""):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg[0] in _STOP_CHARS and bg[1] in _STOP_CHARS:
                continue
            if bg[0] in _EDGE_CHARS or bg[1] in _EDGE_CHARS:
                continue
            out.add(bg)
    return out


def _compact(text: str) -> str:
    """去掉空白与常见标点，只用于字面短语匹配。"""
    return re.sub(r"[\s，。；：、！？!?;,\"'“”‘’（）()【】\[\]—…·]", "", text or "")


class TermWeights:
    """卡表二元组 IDF。`from_cards` 是唯一入口，保证换书自动换权重。"""

    def __init__(self, idf: Mapping[str, float], size: int) -> None:
        self.idf = dict(idf)
        self.size = int(size)

    @classmethod
    def from_cards(cls, cards: Iterable[dict]) -> "TermWeights":
        docs: list[set[str]] = []
        df: Counter = Counter()
        for card in cards:
            terms = _bigrams(
                f"{card.get('text') or ''} {card.get('summary') or ''}"
            )
            docs.append(terms)
            for term in terms:
                df[term] += 1
        total = max(1, len(docs))
        return cls(
            {term: math.log((total + 1) / (n + 1)) for term, n in df.items()},
            total,
        )

    def weight(self, term: str) -> float:
        return float(self.idf.get(term, 1.0))


def _markers(rule_text: str) -> list[str]:
    marks = [m.strip() for m in _QUOTED.findall(rule_text or "") if m.strip()]
    marks += [m.strip() for m in _UNIT.findall(rule_text or "")]
    return list(dict.fromkeys(marks))


def _structural_evidence(rule_text: str, generated_text: str,
                         terms: Iterable[str]) -> dict[str, Any] | None:
    """位置线索：规则要求「章末/开篇」时，命中词必须落在对应区域。

    这不是本书特有名词，而是写作规则的通用位置词；换书同样成立。
    """
    terms = [t for t in terms if t]
    if not terms:
        return None
    text = generated_text or ""
    size = len(text)
    if size < 40:
        return None
    tail = text[int(size * 0.8):]
    head = text[: max(1, int(size * 0.2))]
    if any(cue in rule_text for cue in _END_CUES):
        hit = [t for t in terms if t in tail]
        if hit:
            return {"zone": "ending", "terms": hit}
    if any(cue in rule_text for cue in _OPEN_CUES):
        hit = [t for t in terms if t in head]
        if hit:
            return {"zone": "opening", "terms": hit}
    return None


def judge_call(
    rule_text: str,
    generated_text: str,
    *,
    tool: str = "rule",
    weights: TermWeights | None = None,
) -> dict[str, Any]:
    """判一条规则是否被这段正文采用。返回 `{verdict, reason, evidence}`。

    `rule_text` 里若带「（证据：……）」或单独一行的参考书证据，会先由
    `rule_body` 剥掉——判定只比较规则本身与正文，不把参考书原句当规则词集。
    """
    if tool in NAV_TOOLS:
        return {"verdict": VERDICT_NAV, "reason": "导航调用，不参与采纳统计",
                "evidence": {}}
    if tool == "anchor":
        # 锚点返回的是参考书正文，不是规则；用它的词集和正文比只会得到噪声。
        # 批量判定在 `judge_calls` 里已换成卡表规则正文（tool 传 rule）。
        return {"verdict": VERDICT_UNJUDGEABLE,
                "reason": "锚点返回的是参考书正文，缺少规则原文，不能当判据",
                "evidence": {}}
    rule_text = rule_body(rule_text)
    generated_text = generated_text or ""
    if not rule_text:
        return {"verdict": VERDICT_UNJUDGEABLE, "reason": "缺少规则正文",
                "evidence": {}}

    markers = _markers(rule_text)
    gen_compact = _compact(generated_text)
    literal_hits = [m for m in markers if _compact(m) in gen_compact]
    if literal_hits:
        return {
            "verdict": VERDICT_LITERAL,
            "reason": "规则标记原样出现在正文",
            "evidence": {"markers": markers, "literal_hits": literal_hits},
        }

    rule_terms = _bigrams(rule_text)
    gen_terms = _bigrams(generated_text)
    weights = weights or TermWeights({}, 1)
    distinct = sorted(
        (t for t in rule_terms if weights.weight(t) >= MIN_IDF),
        key=lambda t: -weights.weight(t),
    )
    hits = [t for t in distinct if t in gen_terms]
    focus = distinct[:FOCUS_TERMS]
    total_w = sum(weights.weight(t) for t in focus)
    hit_w = sum(weights.weight(t) for t in hits)
    coverage = min(1.0, hit_w / total_w) if total_w else 0.0

    # 分句级强重合：一条具体分句里 >=3 个二元组、一半以上在正文出现。
    clause_hits: list[dict[str, Any]] = []
    for clause in _CLAUSE_SPLIT.split(rule_text):
        cb = _bigrams(clause)
        if len(cb) < 3:
            continue
        overlap = cb & gen_terms
        if len(overlap) >= 2 and len(overlap) / len(cb) >= 0.5:
            clause_hits.append({
                "clause": clause.strip(),
                "overlap": sorted(overlap),
                "ratio": round(len(overlap) / len(cb), 2),
            })

    evidence = {
        "markers": markers,
        "literal_hits": [],
        "distinct_terms": len(distinct),
        "matched_terms": [{"term": t, "idf": round(weights.weight(t), 2)} for t in hits],
        "coverage": round(coverage, 3),
        "weighted_hits": round(hit_w, 2),
        "clause_hits": clause_hits,
        "structural": _structural_evidence(rule_text, generated_text, hits),
    }

    if (len(hits) >= MATCHED_TERMS_MIN and hit_w >= REWRITE_MIN_WEIGHT
            and coverage >= REWRITE_MIN_COVERAGE):
        return {"verdict": VERDICT_REWRITE,
                "reason": "规则的高区分度词在正文里成簇出现（改写式采用）",
                "evidence": evidence}
    if clause_hits:
        return {"verdict": VERDICT_REWRITE,
                "reason": "规则的具体分句在正文里有一半以上词面重合",
                "evidence": evidence}
    if evidence["structural"] and hit_w >= REWRITE_MIN_WEIGHT:
        return {"verdict": VERDICT_REWRITE,
                "reason": "规则点名了章末/开篇位置，命中词正好落在该区域",
                "evidence": evidence}
    if markers:
        return {"verdict": VERDICT_NOT_DETECTED,
                "reason": "规则含可核对标记，但正文里没有出现",
                "evidence": evidence}
    # 零星弱重合不等于「没采用」：规则是抽象写法时，正文本来就不会复用它的
    # 元语言（`长复句`、`问号`）。只有可核对标记缺失时才算 not_detected。
    reason = ("仅有零星词面重合，既不足以判采用，也无法据此判未采用"
              if hits else
              "无可核对标记且无词面信号，无法区分同义改写与未采用")
    return {"verdict": VERDICT_UNJUDGEABLE, "reason": reason, "evidence": evidence}


def judge_calls(
    calls: Iterable[Mapping[str, Any]],
    generated_text: str,
    *,
    card_texts: Mapping[str, str] | None = None,
    weights: TermWeights | None = None,
) -> dict[str, Any]:
    """批量判定。`calls` 每项至少有 `tool`，规则调用还需 `rule_id` 或 `raw_text`。

    规则正文优先取 `call["rule_text"]`，其次取卡表的 `card_texts[rule_id]`
    （卡表正文只含规则，不含参考书证据），最后才退回 `raw_text`。
    `anchor` 的 `raw_text` 是参考书正文，不能当规则；没有卡表时直接判不可判。
    """
    card_texts = card_texts or {}
    details: list[dict[str, Any]] = []
    for call in calls:
        tool = str(call.get("tool") or call.get("tool_name") or "rule")
        rule_id = str(call.get("rule_id") or call.get("card_id") or "")
        raw = ""
        source = "nav" if tool in NAV_TOOLS else "missing"
        if tool not in NAV_TOOLS:
            explicit = str(call.get("rule_text") or "").strip()
            card_text = str(card_texts.get(rule_id) or "").strip()
            if explicit:
                raw, source = explicit, "rule_text"
            elif card_text:
                raw, source = card_text, "card"
            elif tool == "anchor":
                details.append({
                    "seq": call.get("seq"),
                    "tool": tool,
                    "rule_id": rule_id or None,
                    "path": call.get("path"),
                    "verdict": VERDICT_UNJUDGEABLE,
                    "reason": "锚点返回的是参考书正文，缺少卡表规则原文，不能当判据",
                    "evidence": {},
                    "rule_source": "missing",
                })
                continue
            else:
                raw = str(call.get("raw_text") or "")
                source = "raw" if raw else "missing"
        # 已经拿到卡表规则正文时按 rule 判；anchor 的原始返回是参考书正文，
        # 不能让 judge_call 当规则用。
        judge_tool = "rule" if source in ("card", "rule_text") else tool
        verdict = judge_call(raw, generated_text, tool=judge_tool, weights=weights)
        details.append({
            "seq": call.get("seq"),
            "tool": tool,
            "rule_id": rule_id or None,
            "path": call.get("path"),
            "rule_source": source,
            **verdict,
        })

    distribution = {v: 0 for v in VERDICTS}
    for item in details:
        distribution[item["verdict"]] += 1
    adopted_ids = list(dict.fromkeys(
        d["rule_id"] for d in details
        if d["verdict"] in ADOPTED_VERDICTS and d["rule_id"]
    ))
    judged = [d for d in details if d["verdict"] not in (VERDICT_NAV,)]
    shares = {
        v: (round(distribution[v] / len(judged), 3) if judged else 0.0)
        for v in VERDICTS if v != VERDICT_NAV
    }
    sources = Counter(d.get("rule_source") or "missing" for d in details)
    return {
        "calls": len(details),
        "judged_calls": len(judged),
        "adopted_rule_ids": adopted_ids,
        "distribution": distribution,
        "details": details,
        # 判定输入来源：card=卡表规则正文，raw=轨迹 raw_text，missing=缺规则原文。
        "rule_sources": dict(sources),
        # 兼容生成侧旧字段名，接线时可以直接替换 check_adoption 的返回。
        "counts": distribution,
        "shares": shares,
        "per_call": details,
        "note": (
            "unjudgeable 表示判不了，不是未采用；不要把两者合并统计"
        ),
    }


def calls_from_trace(db: Session, session_id: str,
                     segment_no: int | None = None) -> list[dict[str, Any]]:
    rows = db.execute(
        select(RetrievalLog).where(RetrievalLog.session_id == session_id)
        .order_by(RetrievalLog.seq)
    ).scalars().all()
    if segment_no is not None:
        rows = [r for r in rows if r.segment_no == segment_no]
    return [
        {
            "seq": r.seq,
            "tool": r.tool_name,
            "rule_id": r.rule_id or r.card_id,
            "path": r.path,
        }
        for r in rows
    ]


#: 卡表正文 + IDF 权重按 `(distill_id, catalog_version)` 缓存：27k 卡逐段重建
#: `TermWeights` 太贵，换书后 catalog_version 变化会自动失效。
_CARD_BUNDLE_CACHE: dict[tuple[int, str], tuple[TermWeights, dict[str, str]]] = {}


def _card_bundle(distill_id: int) -> tuple[TermWeights, dict[str, str]]:
    """返回该档案的 `(IDF 权重, {card_id: 规则正文})`。

    `card_texts` 只给规则正文（卡表 `text`），判定时仍会剥掉其中的证据块；
    这样锚点/规则调用都不必依赖轨迹里的 `raw_text`（那里混着参考书原句）。
    """
    catalog = load_catalog(int(distill_id))
    key = (catalog.distill_id, catalog.catalog_version)
    cached = _CARD_BUNDLE_CACHE.get(key)
    if cached is not None:
        return cached
    texts = {str(c["id"]): str(c.get("text") or "") for c in catalog.cards}
    bundle = (TermWeights.from_cards(catalog.cards), texts)
    _CARD_BUNDLE_CACHE[key] = bundle
    return bundle


def _session_card_texts(
    db: Session, session_id: str, segment_no: int
) -> tuple[dict[str, str], TermWeights | None]:
    """从轨迹行反查本段的 distill_id，再取卡表正文与权重；失败返回空。"""
    distill_ids = db.execute(
        select(RetrievalLog.distill_id)
        .where(
            RetrievalLog.session_id == session_id,
            RetrievalLog.segment_no == segment_no,
            RetrievalLog.distill_id.is_not(None),
        )
        .distinct()
    ).scalars().all()
    texts: dict[str, str] = {}
    weights: TermWeights | None = None
    for distill_id in distill_ids:
        try:
            bundle_weights, bundle_texts = _card_bundle(int(distill_id))
        except Exception:  # noqa: BLE001 —— 卡表缺失时退回 raw_text 判定
            continue
        texts.update(bundle_texts)
        if weights is None:
            weights = bundle_weights
    return texts, weights


def backfill_adoption(
    db: Session,
    session_id: str,
    segment_no: int,
    generated_text: str,
    *,
    calls: Iterable[Mapping[str, Any]] | None = None,
    card_texts: Mapping[str, str] | None = None,
    weights: TermWeights | None = None,
) -> dict[str, Any]:
    """段末回填：判定 → 写 `adopted_rule_ids` → 返回分布与逐条明细。

    写回仍走 `trace.fill_segment_adoption`（唯一写入点），保证「查了被拒但
    最终没用」的行也一起更新。

    `card_texts` / `weights` 缺省时自动按本段轨迹的 `distill_id` 从卡表取：
    判定输入因此稳定落在**规则正文**上，不会把 `rule` 带回的参考书证据算进去。
    """
    calls = list(calls) if calls is not None else calls_from_trace(
        db, session_id, segment_no
    )
    if card_texts is None or weights is None:
        bundle_texts, bundle_weights = _session_card_texts(db, session_id, segment_no)
        if card_texts is None:
            card_texts = bundle_texts
        if weights is None:
            weights = bundle_weights
    summary = judge_calls(
        calls, generated_text, card_texts=card_texts, weights=weights
    )
    updated = fill_segment_adoption(
        db, session_id, segment_no, summary["adopted_rule_ids"]
    )
    summary["updated_rows"] = updated
    summary["segment_no"] = segment_no
    return summary
