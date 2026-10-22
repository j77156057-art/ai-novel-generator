"""Assistant 审核规则引擎：语料 / 知识图谱 / 生成物（第 7 步交付 3）。

## 这个模块回答什么问题

助手从「只能问答和诊断」升级为「能审核语料、知识图谱、生成物」。三类
审核全部是**只读**的确定性规则跑批，产出与 `app.agents.diagnosis` 完全
同构的结构化 issue（九字段 schema），可被转成提案（`from_issue`）。

## 规则清单

### 语料（`run_corpus_audit`，全局扫描 raw_novels + novel_chunks）

| rule | severity | 判据 |
|---|---|---|
| `empty_or_short_chunks` | warn | 分片文本为空或短于 `MIN_CHUNK_CHARS`（切分/解析事故） |
| `duplicate_chunks` | warn | 文本完全相同的分片（重复入库/重复切分） |
| `suspected_injection_strings` | warn | 复用 `app.corpus_health` 的水印判据（句末位置分布） |
| `missing_style_profile` | info | 语料未拟合文体档案（不影响可用性，影响文风注入） |
| `chunks_without_embedding` | warn | 分片未建 embedding（混合检索的向量路失效） |

### 知识图谱（`run_kg_audit`，按项目，generated 图）

| rule | severity | 判据 |
|---|---|---|
| `orphan_kg_nodes` | info / warn | 无任何边连接的节点 |
| `name_conflict` | warn / error | **直接复用** `diagnosis.rule_name_conflict`（账本别名 × KG 节点交叉，保证与诊断口径逐字节一致） |
| `dead_character_open_holes` | warn | 哨兵状态表已死角色仍挂在未填坑里 |
| `cooccurrence_without_edge` | info | 两个角色在同段共现但彼此没有任何边（关系漏建） |

### 生成物（`run_generation_audit`，按项目）

| rule | severity | 判据 |
|---|---|---|
| `no_generations` | info | 项目没有任何已生成段 |
| `empty_segment_text` | error | 段落正文为空（脏数据） |
| `segment_length_outliers` | warn | 段长远超/远低于 `project.generation_length` |
| `ai_flavor_score` | warn | AI 腔量化打分（有文风档案 → z 分数离群；无档案 → 套话密度启发式） |
| `bible_character_absent` | warn | plot_bible 里的角色在全部生成段中零出现 |
| `generation_character_not_in_bible` | info | 高频出场角色不在 plot_bible 名单里（可能是新角色，交人确认） |

## 纪律

- **纯只读**：不写任何业务表。审核本身要被审计，由 router 另开 AgentRun。
- **复用优先**：name_conflict 直接调 diagnosis 的规则函数；注入串检测直接
  调 `corpus_health.find_repeated_watermarks`；AI 腔复用 `style.stylometry`。
  不复制判据——复制出来的第二套判据一定会和第一套漂移。
- **规则先行、模型后置**：与 diagnose 同一原则（见 routers/assistant.py）。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select

from app.agents.diagnosis import (
    DiagnosticIssue,
    DiagnosticReport,
    IssueEvidence,
    make_issue_id,
)
from app.models import Fact, Generation, KgEdge, KgNode, NovelChunk, Project, RawNovel

# ------------------------------------------------------------------
# 阈值常量（显式值；调整必须带理由，见各规则的 docstring）
# ------------------------------------------------------------------

#: 分片短于该字数 → 报（正常切分 chunk_size=1200，几十字的分片是事故）。
MIN_CHUNK_CHARS = 50

#: 同一文本重复出现达到该次数 → 报（1 次不报：单条不是「重复」）。
DUPLICATE_MIN_COPIES = 2

#: 水印扫描的单本体量上限（字符）。判据要在全篇上数位置分布，体量过大
#: 时内存与 CPU 都不可控——超过即跳过并在 tool_results 里说明，
#: **不静默**（静默跳过会让「没报=没问题」变成误读）。
WATERMARK_MAX_CHARS = 2_000_000

#: 孤儿节点数达到该值 → 从 info 升为 warn。
ORPHAN_WARN_COUNT = 5

#: 共现判据的最小段数：两个角色只在同一段出现一次，多半是巧合；
#: 稳定共现才说明「确实该有关系但图里没有」。
MIN_COOCURRENCE_SEGMENTS = 2

#: AI 腔打分（无文风档案时的启发式）：每百字命中的套话Pattern 数。
#: 不是分类器，是「密度离群就给人看一眼」的粗筛——阈值取 2.0：
#: 实测人类网文的比喻/情绪套话密度远低于此（corpus_health 同款语料
#: 上 `仿佛/宛如` 类Pattern 每百字 < 0.5 次）。
AI_CLICHE_DENSITY_WARN = 2.0

#: AI 腔打分（有文风档案时）：特征 z 分数平均绝对值超过该值 → 离群。
#: 与 stylometry 的 holdout p90 判据同量纲（p90 ≈ 1.3 为正常离散）。
AI_FLAVOR_MEAN_ABS_Z_WARN = 2.0

#: 「高频出场角色」的 mentions 门槛（KG 节点 props.mentions）。
PROMINENT_CHARACTER_MENTIONS = 5

#: 每条 issue 最多挂几条证据（防爆响应体；完整清单进 claim）。
MAX_EVIDENCE_PER_ISSUE = 5

_MAX_SNIPPET = 200

#: AI 腔套话 pattern（无档案路径的启发式判据）。
#: 选样标准：LLM 生成中文小说的高频「氛围/情绪」套话，人类作者也会用，
#: 但密度显著更低——判的是**密度**，不是出现与否。
_AI_CLICHE_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p)
    for p in (
        r"眼神里", r"瞳孔", r"眸中", r"闪过一丝", r"涌上心头",
        r"嘴角勾起", r"不禁", r"仿佛", r"宛如", r"犹如", r"好似",
        r"不只是", r"更是", r"终于明白", r"很多年后", r"他不知道",
        r"这一切", r"空气仿佛", r"时间仿佛", r"宛若",
    )
)


# ------------------------------------------------------------------
# 公共小工具
# ------------------------------------------------------------------


def _truncate(text: str, limit: int = _MAX_SNIPPET) -> str:
    return (text or "").strip()[:limit]


def _safe_float(value: Any) -> float:
    """Evidence.score 在 SQLite 上是 String 列（见 models.py 注释）——
    读回时统一转 float，转不了按 0.0。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num:  # NaN
        return 0.0
    return num


def _novel_source(novel_id: int) -> str:
    """novel 级证据的 source 标签（与 diagnosis 的 `novel:{id}` 口径一致）。"""
    return f"novel:{novel_id}"


def _kg_source() -> str:
    return "generated"


# ==================================================================
# 语料审核
# ==================================================================


def rule_corpus_empty_or_short_chunks(
    chunks_by_novel: dict[int, list[NovelChunk]], taken: set[str]
) -> list[DiagnosticIssue]:
    """空分片 / 超短分片。"""
    issues: list[DiagnosticIssue] = []
    for novel_id in sorted(chunks_by_novel):
        bad = [
            c
            for c in chunks_by_novel[novel_id]
            if not (c.text or "").strip() or len((c.text or "").strip()) < MIN_CHUNK_CHARS
        ]
        if not bad:
            continue
        evidence = tuple(
            IssueEvidence(
                kind="chunk",
                source=_novel_source(novel_id),
                ref_id=str(c.id),
                snippet=_truncate(c.text or "") or "(空)",
                score=0.5,
            )
            for c in bad[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id("empty_or_short_chunks", f"novel{novel_id}", taken),
                type="corpus_quality",
                severity="warn",
                title=f"语料 {novel_id} 有 {len(bad)} 个空/超短分片",
                claim=(
                    f"novel_id={novel_id} 的分片里有 {len(bad)} 个文本为空或不足 "
                    f"{MIN_CHUNK_CHARS} 字（正常切分约 1200 字/片）。"
                    f"分片 id：{[c.id for c in bad[:10]]}。"
                ),
                suggestion=(
                    "检查该语料的解析与切分：空分片多见于章节解析产出空段、"
                    "或切分重叠参数把短章切碎。建议重新入库该语料。"
                ),
                rule="empty_or_short_chunks",
                evidence=evidence,
            )
        )
    return issues


def rule_corpus_duplicate_chunks(
    chunks_by_novel: dict[int, list[NovelChunk]], taken: set[str]
) -> list[DiagnosticIssue]:
    """重复分片：文本完全相同的分片（按 sha256 分组，跨语料也查）。"""
    by_hash: dict[str, list[NovelChunk]] = {}
    for chunks in chunks_by_novel.values():
        for c in chunks:
            text = (c.text or "").strip()
            if not text:
                continue
            by_hash.setdefault(hashlib.sha256(text.encode("utf-8")).hexdigest(), []).append(c)

    issues: list[DiagnosticIssue] = []
    for digest in sorted(by_hash):
        group = by_hash[digest]
        if len(group) < DUPLICATE_MIN_COPIES:
            continue
        first = group[0]
        novel_ids = sorted({c.novel_id for c in group})
        evidence = tuple(
            IssueEvidence(
                kind="chunk",
                source=_novel_source(c.novel_id),
                ref_id=str(c.id),
                snippet=_truncate(c.text),
                score=0.5,
            )
            for c in group[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id(
                    "duplicate_chunks", f"n{len(group)}:{first.novel_id}", taken
                ),
                type="corpus_quality",
                severity="warn",
                title=f"{len(group)} 个分片文本完全相同",
                claim=(
                    f"有 {len(group)} 个分片的文本逐字节相同（涉及语料 {novel_ids}）。"
                    f"分片 id：{[c.id for c in group[:10]]}。"
                    f"预览：「{_truncate(first.text, 60)}」。"
                ),
                suggestion=(
                    "重复分片会让检索反复命中同一段原文、文体统计重复计数。"
                    "常见原因是同一本书被导入多次或切分重叠过大；"
                    "确认后清理重复语料。"
                ),
                rule="duplicate_chunks",
                evidence=evidence,
            )
        )
    return issues


def rule_corpus_injection_strings(
    novels: dict[int, RawNovel],
    chunks_by_novel: dict[int, list[NovelChunk]],
    taken: set[str],
) -> tuple[list[DiagnosticIssue], dict[str, Any]]:
    """疑似注入串：复用 `app.corpus_health.find_repeated_watermarks`。

    判据是盗版站 SEO 水印的**位置分布**（几乎只出现在句末标点前），
    详见 corpus_health 模块文档——那是在 116 万字真实语料上校准过的，
    这里不重写。单本体量超过 `WATERMARK_MAX_CHARS` 时跳过并**显式**
    记进返回的 notes（不静默）。
    """
    from app.corpus_health import find_repeated_watermarks

    issues: list[DiagnosticIssue] = []
    notes: dict[str, Any] = {}
    for novel_id in sorted(novels):
        chunks = sorted(chunks_by_novel.get(novel_id) or [], key=lambda c: (c.idx or 0, c.id or 0))
        text = "\n".join((c.text or "") for c in chunks)
        if not text.strip():
            continue
        if len(text) > WATERMARK_MAX_CHARS:
            notes[f"novel:{novel_id}"] = {
                "skipped": "watermark_scan",
                "reason": f"体量 {len(text)} 字符超过上限 {WATERMARK_MAX_CHARS}",
            }
            continue
        marks = find_repeated_watermarks(text)
        notes[f"novel:{novel_id}"] = {"watermark_scan": "done", "marks": len(marks)}
        if not marks:
            continue
        evidence = tuple(
            IssueEvidence(
                kind="chunk",
                source=_novel_source(novel_id),
                ref_id=f"watermark:{m['text']}",
                snippet=f"{m['text']} × {m['count']}（紧跟『。』率 {m['final_ratio']:.0%}）",
                score=0.6,
            )
            for m in marks[:MAX_EVIDENCE_PER_ISSUE]
        )
        top = marks[0]
        issues.append(
            DiagnosticIssue(
                id=make_issue_id("suspected_injection_strings", f"novel{novel_id}", taken),
                type="corpus_quality",
                severity="warn",
                title=f"语料 {novel_id} 检出 {len(marks)} 个疑似注入串",
                claim=(
                    f"novel_id={novel_id}（《{novels[novel_id].title or '未命名'}》）检出 "
                    f"{len(marks)} 个高频句末串，最典型：「{top['text']}」出现 "
                    f"{top['count']} 次、{top['final_ratio']:.0%} 紧跟句末句号『。』"
                    f"（口径见 corpus_health：只看『。』，不用任意句末标点并集）。"
                    f"样本：{top.get('samples', [])[:1]}。"
                ),
                suggestion=(
                    "这是盗版站注入的 SEO 水印特征（位置分布判据）。它会稀释"
                    "文体档案、污染检索与图谱抽取；可用 corpus_health."
                    "strip_watermarks 清洗后重新拟合该语料的 style_profile。"
                ),
                rule="suspected_injection_strings",
                evidence=evidence,
            )
        )
    return issues, notes


def rule_corpus_missing_style_profile(
    novels: dict[int, RawNovel], chunks_by_novel: dict[int, list[NovelChunk]], taken: set[str]
) -> list[DiagnosticIssue]:
    """缺 style_profile 的语料。"""
    missing = [
        nid
        for nid in sorted(novels)
        if not (novels[nid].style_profile or {})
    ]
    if not missing:
        return []
    evidence = tuple(
        IssueEvidence(
            kind="novel",
            source=_novel_source(nid),
            ref_id=f"novel:{nid}",
            snippet=(
                f"《{novels[nid].title or '未命名'}》 {len(chunks_by_novel.get(nid) or [])} 个分片，"
                f"style_profile 为空"
            ),
            score=0.4,
        )
        for nid in missing[:MAX_EVIDENCE_PER_ISSUE]
    )
    return [
        DiagnosticIssue(
            id=make_issue_id("missing_style_profile", f"n{len(missing)}", taken),
            type="corpus_quality",
            severity="info",
            title=f"{len(missing)} 个语料未拟合文体档案",
            claim=(
                f"以下语料的 style_profile 为空：{missing}。未拟合档案的语料"
                f"被项目选为参考时，不会注入文风约束（自由生成）。"
            ),
            suggestion=(
                "在项目里把这些语料选为参考语料并触发一次档案拟合"
                "（style.pipeline.build_reference_profile）；语料体量过小"
                "（分片数不足）时拟合会失败，属预期。"
            ),
            rule="missing_style_profile",
            evidence=evidence,
        )
    ]


def rule_corpus_chunks_without_embedding(
    chunks_by_novel: dict[int, list[NovelChunk]], taken: set[str]
) -> list[DiagnosticIssue]:
    """未建 embedding 的分片（混合检索的向量路失效，只剩 BM25）。"""
    issues: list[DiagnosticIssue] = []
    for novel_id in sorted(chunks_by_novel):
        chunks = chunks_by_novel[novel_id]
        if not chunks:
            continue
        missing = [c for c in chunks if c.embedding is None or str(c.embedding).strip() in ("", "None")]
        if not missing:
            continue
        ratio = len(missing) / len(chunks)
        evidence = tuple(
            IssueEvidence(
                kind="chunk",
                source=_novel_source(novel_id),
                ref_id=str(c.id),
                snippet=_truncate(c.text or "") or "(空)",
                score=0.5,
            )
            for c in missing[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id(
                    "chunks_without_embedding", f"novel{novel_id}:{len(missing)}", taken
                ),
                type="corpus_quality",
                severity="warn",
                title=f"语料 {novel_id} 有 {len(missing)}/{len(chunks)} 个分片未建 embedding",
                claim=(
                    f"novel_id={novel_id} 的分片 {ratio:.0%} 没有向量（embedding 为空），"
                    f"混合检索对这些分片退化为纯 BM25。分片 id："
                    f"{[c.id for c in missing[:10]]}。"
                ),
                suggestion=(
                    "embedding 缺失通常是入库时向量服务不可用（见 ingestion/store.py "
                    "的降级路径）。补跑一次 embedding 填充即可恢复向量检索。"
                ),
                rule="chunks_without_embedding",
                evidence=evidence,
            )
        )
    return issues


def run_corpus_audit(
    db: Any,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[DiagnosticReport, dict[str, Any]]:
    """跑全量语料审核规则。

    `novel_ids` 缺省 → 全部语料；给定 → 只审这些（前端可先审项目引用的）。

    返回 `(报告, 概要统计)`：统计里有 novel 总数 / 分片总数 / 各规则
    跳过说明，router 原样放进响应——「审了多少」必须和「报了什么」
    对得上。
    """
    q = select(RawNovel)
    # `is not None` 而非裸 `if novel_ids:`：空列表必须与 None 分开。
    # `[]` 是 falsy，旧写法会把「一本都没选」执行成「审全库」（静默范围放大）。
    # 路由层现在已对 `[]` 报 422，这里是第二道闸门——库函数被直接调用时
    # 也不该广播。空列表走到 `in_([])` 会得到空集，语义即「审 0 本」。
    if novel_ids is not None:
        q = q.where(RawNovel.id.in_(novel_ids))
    novels = {n.id: n for n in db.scalars(q).all()}

    chunks_by_novel: dict[int, list[NovelChunk]] = {nid: [] for nid in novels}
    if novels:
        chunk_q = select(NovelChunk).where(NovelChunk.novel_id.in_(novels.keys()))
        for c in db.scalars(chunk_q).all():
            chunks_by_novel.setdefault(c.novel_id, []).append(c)

    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []
    rule_errors: dict[str, Any] = {}
    watermark_notes: dict[str, Any] = {}

    def _run(name: str, func) -> None:
        try:
            func()
        except Exception as exc:  # noqa: BLE001 - 单规则失败不拖垮整次审核
            rule_errors[f"rule:{name}"] = {"error": str(exc)[:200]}

    _run(
        "empty_or_short_chunks",
        lambda: issues.extend(rule_corpus_empty_or_short_chunks(chunks_by_novel, taken)),
    )
    _run(
        "duplicate_chunks",
        lambda: issues.extend(rule_corpus_duplicate_chunks(chunks_by_novel, taken)),
    )

    def _injection() -> None:
        nonlocal watermark_notes
        found, notes = rule_corpus_injection_strings(novels, chunks_by_novel, taken)
        issues.extend(found)
        watermark_notes = notes

    _run("suspected_injection_strings", _injection)
    _run(
        "missing_style_profile",
        lambda: issues.extend(rule_corpus_missing_style_profile(novels, chunks_by_novel, taken)),
    )
    _run(
        "chunks_without_embedding",
        lambda: issues.extend(rule_corpus_chunks_without_embedding(chunks_by_novel, taken)),
    )

    stats = {
        "novels_audited": len(novels),
        "chunks_audited": sum(len(v) for v in chunks_by_novel.values()),
        "watermark_scan": watermark_notes,
        "rule_errors": rule_errors,
    }
    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=[
            "empty_or_short_chunks",
            "duplicate_chunks",
            "suspected_injection_strings",
            "missing_style_profile",
            "chunks_without_embedding",
        ],
    )
    return report, stats


# ==================================================================
# 知识图谱审核
# ==================================================================


def rule_kg_orphan_nodes(db: Any, project_id: int, taken: set[str]) -> list[DiagnosticIssue]:
    """孤儿节点：无任何边（from/to 都不出现）的 KG 节点。"""
    nodes = list(
        db.scalars(
            select(KgNode).where(
                KgNode.project_id == project_id, KgNode.source == "generated"
            )
        ).all()
    )
    if not nodes:
        return []
    node_ids = [n.id for n in nodes]
    connected: set[int] = set()
    for from_id, to_id in db.execute(
        select(KgEdge.from_id, KgEdge.to_id).where(
            (KgEdge.from_id.in_(node_ids)) | (KgEdge.to_id.in_(node_ids))
        )
    ).all():
        connected.add(int(from_id))
        connected.add(int(to_id))

    orphans = [n for n in nodes if n.id not in connected]
    if not orphans:
        return []
    severity = "warn" if len(orphans) >= ORPHAN_WARN_COUNT else "info"

    def _node_label(n: KgNode) -> str:
        props = n.props or {}
        name = props.get("name") or props.get("question") or ""
        return f"{n.type}:{name}" if name else n.type

    evidence = tuple(
        IssueEvidence(
            kind="kg",
            source=_kg_source(),
            ref_id=str(n.id),
            snippet=_truncate(_node_label(n)),
            score=0.4,
        )
        for n in orphans[:MAX_EVIDENCE_PER_ISSUE]
    )
    return [
        DiagnosticIssue(
            id=make_issue_id("orphan_kg_nodes", f"n{len(orphans)}", taken),
            type="kg_quality",
            severity=severity,
            title=f"{len(orphans)} 个图谱节点没有任何边",
            claim=(
                f"generated 图谱里有 {len(orphans)}/{len(nodes)} 个节点不与任何"
                f"其它节点相连。节点 id：{[n.id for n in orphans[:10]]}。"
            ),
            suggestion=(
                "孤儿节点不参与关系推理，也不会进 reminders 的关系清单。"
                "若是抽取遗漏的关系，可在下次生成时自然补上；"
                "若是噪声实体（碎片名/误识别的物），建议走提案清理。"
            ),
            rule="orphan_kg_nodes",
            evidence=evidence,
        )
    ]


def rule_kg_name_conflict(db: Any, project: Any, taken: set[str]) -> list[DiagnosticIssue]:
    """角色名冲突：**直接复用** diagnosis 的规则函数。

    不复制判据是硬要求：diagnosis.name_conflict 的口径（账本 aliases ×
    KG 角色节点交叉）是评审过的，复制一份必然漂移。这里构造同一个
    `DiagnosticSnapshot` 调同一个函数，保证「诊断说的冲突」与「审核说的
    冲突」永远是一回事。
    """
    from app.agents.diagnosis import DiagnosticSnapshot, rule_name_conflict

    snap = DiagnosticSnapshot(db=db, project=project, scope="all")
    return rule_name_conflict(snap, taken)


def rule_kg_dead_character_open_holes(
    db: Any, project_id: int, taken: set[str]
) -> list[DiagnosticIssue]:
    """已死角色仍挂未填坑。

    判据与 diagnosis.dead_character_reappearance 的「坑仍挂着」分支
    同源（哨兵状态表 × 未填坑），但这里**只看坑**——审核图谱时
    「正文又出现」属于生成物审核的领地，两边都报会让同一条事实
    在两个端点重复出现，用户以为有两处问题。
    """
    from app.kg.sentinel import STATE_LIFE, load_state
    from app.kg.queries import get_reminders

    try:
        state = load_state(db, project_id, source="generated")
    except Exception:  # noqa: BLE001 - 状态表读不到 → 本条不判（不拖垮审核）
        return []
    dead = sorted(
        entity
        for (kind, entity), st in state.items()
        if kind == STATE_LIFE and (st.get("value") == "dead")
    )
    if not dead:
        return []

    reminders = get_reminders(db, project_id, source="generated", top_k=None)
    holes = reminders.get("unresolved_plotholes") or []
    issues: list[DiagnosticIssue] = []
    for entity in dead:
        hooked = [h for h in holes if entity in (h.get("question") or "")]
        if not hooked:
            continue
        evidence = tuple(
            IssueEvidence(
                kind="reminder",
                source=_kg_source(),
                ref_id=f"plothole:seg{h.get('first_chapter')}:{(h.get('question') or '')[:24]}",
                snippet=_truncate(h.get("question") or ""),
                score=0.5,
            )
            for h in hooked[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id("dead_character_open_holes", entity, taken),
                type="character_state",
                severity="warn",
                title=f"已死角色「{entity}」仍挂在 {len(hooked)} 条未填坑里",
                claim=(
                    f"哨兵状态表记录「{entity}」已死亡，但仍有 {len(hooked)} 条"
                    f"open 伏笔以它为主角——剧情上等于要求一个已死角色回来收尾。"
                ),
                suggestion=(
                    f"确认这些伏笔的归属：改由他人生效、或在正文补明确的复活"
                    f"描写后再回收。已死角色的伏笔会与角色锁定清单冲突。"
                ),
                rule="dead_character_open_holes",
                evidence=evidence,
            )
        )
    return issues


def _match_present_names(text: str, names_sorted: Sequence[str]) -> list[str]:
    """长名优先匹配，**命中即挖空**，避免短名搭长名的便车。

    旧写法是 `[n for n in names_sorted if n in text]`：虽然先按长度降序排过，
    但对每个名字都独立做子串判断、不挖掉已匹配区间，于是「冷轩月」出现时
    「冷轩」同样算命中——注释声称的「长名优先」并未生效，会报出实际没出现
    过的角色对（实测：节点 冷轩 + 冷轩月、段里只有「冷轩月」→ 误报
    `pair:冷轩月|冷轩`，而「冷轩」从未独立出现）。

    这里给每个字符加一个 consumed 标记：按长度降序扫，某名字的命中区间若
    与已吃掉区间有重叠就跳过，否则整段标记为已消费。这样只有真正独立出现
    的名字才算命中，同时保持返回顺序为长度降序（与旧实现的顺序语义一致，
    便于 pair 键稳定）。
    """
    consumed = bytearray(len(text))
    present: list[str] = []
    for name in names_sorted:  # 长名在前
        hit = False
        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            end = i + len(name)
            if not any(consumed[i:end]):
                consumed[i:end] = b"\x01" * len(name)
                hit = True
            start = i + 1
        if hit:
            present.append(name)
    return present


def rule_kg_cooccurrence_without_edge(
    db: Any, project_id: int, taken: set[str]
) -> list[DiagnosticIssue]:
    """同段共现但无边：两个角色在同一生成段出现 ≥ `MIN_COOCURRENCE_SEGMENTS`
    次，但图谱里彼此没有任何边。

    这是「关系漏建」的粗筛：抽取器漏掉的关系不会自己出现，而稳定共现
    是最弱的正信号（同居/同队/对话都算）。severity 恒为 info——共现
    不等于有关系（两个角色在同场景但无交互极常见），报出来是让人
    「看一眼要不要补」，不是断言漏建。
    """
    chars = list(
        db.scalars(
            select(KgNode).where(
                KgNode.project_id == project_id,
                KgNode.source == "generated",
                KgNode.type == "character",
            )
        ).all()
    )
    name_to_id: dict[str, int] = {}
    for n in chars:
        name = (n.props or {}).get("name")
        if name:
            name_to_id.setdefault(name, n.id)
    if len(name_to_id) < 2:
        return []

    gens = list(
        db.scalars(
            select(Generation)
            .where(Generation.project_id == project_id)
            .order_by(Generation.segment_no)
        ).all()
    )
    if not gens:
        return []

    # 共现计数：长名优先匹配，命中即挖空（见 _match_present_names）。
    # 不能写成 `[n for n in names_sorted if n in text]`——那样短名会搭长名的便车，
    # 报出实际没出现过的角色对。
    names_sorted = sorted(name_to_id, key=len, reverse=True)
    pair_segments: dict[tuple[str, str], list[int]] = {}
    for g in gens:
        text = g.text or ""
        if not text:
            continue
        present = _match_present_names(text, names_sorted)
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                pair = (present[i], present[j])
                pair_segments.setdefault(pair, []).append(g.segment_no)

    # 已有边（任意方向、任意类型）的节点对
    node_ids = list(name_to_id.values())
    linked: set[tuple[int, int]] = set()
    for from_id, to_id in db.execute(
        select(KgEdge.from_id, KgEdge.to_id).where(
            (KgEdge.from_id.in_(node_ids)) | (KgEdge.to_id.in_(node_ids))
        )
    ).all():
        linked.add((int(from_id), int(to_id)))
        linked.add((int(to_id), int(from_id)))

    missing: list[tuple[tuple[str, str], list[int]]] = []
    for pair, segs in sorted(pair_segments.items()):
        if len(segs) < MIN_COOCURRENCE_SEGMENTS:
            continue
        a_id, b_id = name_to_id.get(pair[0]), name_to_id.get(pair[1])
        if a_id is None or b_id is None:
            continue
        if (a_id, b_id) in linked:
            continue
        missing.append((pair, sorted(segs)))

    if not missing:
        return []
    # 按共现段数降序，最多的最可能是真关系
    missing.sort(key=lambda item: (-len(item[1]), item[0]))
    evidence: list[IssueEvidence] = []
    for pair, segs in missing[:MAX_EVIDENCE_PER_ISSUE]:
        evidence.append(
            IssueEvidence(
                kind="kg",
                source=_kg_source(),
                ref_id=f"pair:{pair[0]}|{pair[1]}",
                snippet=f"{pair[0]} × {pair[1]} 共现于段 {segs[:8]}，其间无边",
                score=0.4,
            )
        )
    return [
        DiagnosticIssue(
            id=make_issue_id("cooccurrence_without_edge", f"n{len(missing)}", taken),
            type="kg_quality",
            severity="info",
            title=f"{len(missing)} 对角色稳定共现但没有关系边",
            claim=(
                f"{len(missing)} 对角色在 ≥{MIN_COOCURRENCE_SEGMENTS} 个相同段落"
                f"出现、图谱里却没有连接它们的边。前几对："
                + "；".join(f"{a}×{b}（{len(s)} 段）" for (a, b), s in missing[:5])
                + "。"
            ),
            suggestion=(
                "共现不等于有关系（同场景无交互极常见），但稳定共现且无边，"
                "值得确认抽取是否漏掉了这段关系；确有其事再走提案补边。"
            ),
            rule="cooccurrence_without_edge",
            evidence=tuple(evidence),
        )
    ]


def run_kg_audit(db: Any, project: Any) -> tuple[DiagnosticReport, dict[str, Any]]:
    """跑全量 KG 审核规则（按项目，只审 generated 图——参考语料图
    `novel:{id}` 的卫生由语料审核负责，两边不重叠）。"""
    project_id = int(project.id)
    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []
    rule_errors: dict[str, Any] = {}

    def _run(name: str, func) -> None:
        try:
            issues.extend(func())
        except Exception as exc:  # noqa: BLE001 - 单规则失败不拖垮整次审核
            rule_errors[f"rule:{name}"] = {"error": str(exc)[:200]}

    _run("orphan_kg_nodes", lambda: rule_kg_orphan_nodes(db, project_id, taken))
    _run("name_conflict", lambda: rule_kg_name_conflict(db, project, taken))
    _run(
        "dead_character_open_holes",
        lambda: rule_kg_dead_character_open_holes(db, project_id, taken),
    )
    _run(
        "cooccurrence_without_edge",
        lambda: rule_kg_cooccurrence_without_edge(db, project_id, taken),
    )

    stats = {"project_id": project_id, "rule_errors": rule_errors}
    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=[
            "orphan_kg_nodes",
            "name_conflict",
            "dead_character_open_holes",
            "cooccurrence_without_edge",
        ],
    )
    return report, stats


# ==================================================================
# 生成物审核
# ==================================================================


def rule_generation_no_segments(
    gens: list[Generation], project: Any, taken: set[str]
) -> list[DiagnosticIssue]:
    """项目没有任何已生成段。"""
    if gens:
        return []
    return [
        DiagnosticIssue(
            id=make_issue_id("no_generations", f"project{project.id}", taken),
            type="generation_quality",
            severity="info",
            title="项目尚未生成任何段落",
            claim=(
                f"项目 {project.id}（《{project.title or '未命名'}》）的 generations "
                f"表里没有行。审核的其余规则（段长/AI 腔/角色一致性）没有输入。"
            ),
            suggestion="先通过生成端点产出至少一段，再跑生成物审核。",
            rule="no_generations",
            evidence=(),
        )
    ]


def rule_generation_empty_text(
    gens: list[Generation], taken: set[str]
) -> list[DiagnosticIssue]:
    """段落正文为空（脏数据）。"""
    empty = [g for g in gens if not (g.text or "").strip()]
    if not empty:
        return []
    evidence = tuple(
        IssueEvidence(
            kind="generation",
            source=_kg_source(),
            ref_id=str(g.id),
            snippet=f"段 {g.segment_no} 正文为空",
            score=0.5,
        )
        for g in empty[:MAX_EVIDENCE_PER_ISSUE]
    )
    return [
        DiagnosticIssue(
            id=make_issue_id("empty_segment_text", f"n{len(empty)}", taken),
            type="generation_quality",
            severity="error",
            title=f"{len(empty)} 个段落正文为空",
            claim=(
                f"段 {[g.segment_no for g in empty[:10]]} 的 text 为空或全空白——"
                f"落库了但没有内容（generation id：{[g.id for g in empty[:10]]}）。"
            ),
            suggestion=(
                "空段落会进入摘要链与后续前情，污染上下文。"
                "用重生成替换这些段，或走提案清理。"
            ),
            rule="empty_segment_text",
            evidence=evidence,
        )
    ]


def rule_generation_length_outliers(
    gens: list[Generation], project: Any, taken: set[str]
) -> list[DiagnosticIssue]:
    """段长离群：远超/远低于 project.generation_length。"""
    target = int(project.generation_length or 3000)
    if target <= 0:
        return []
    low, high = target * 0.5, target * 2.0
    outliers: list[tuple[Generation, float]] = []
    for g in gens:
        n = len((g.text or "").strip())
        if n == 0:
            continue  # 空段由 empty_segment_text 负责，不双报
        ratio = n / target
        if ratio < 0.5 or ratio > 2.0:
            outliers.append((g, ratio))
    if not outliers:
        return []
    evidence = tuple(
        IssueEvidence(
            kind="generation",
            source=_kg_source(),
            ref_id=str(g.id),
            snippet=f"段 {g.segment_no}：{len((g.text or '').strip())} 字（目标 {target}，{ratio:.0%}）",
            score=0.45,
        )
        for g, ratio in outliers[:MAX_EVIDENCE_PER_ISSUE]
    )
    return [
        DiagnosticIssue(
            id=make_issue_id("segment_length_outliers", f"n{len(outliers)}", taken),
            type="generation_quality",
            severity="warn",
            title=f"{len(outliers)} 个段落长度离群",
            claim=(
                f"以项目设定单段 {target} 字为基准，{len(outliers)} 个段落的长度"
                f"超出 [{low:.0f}, {high:.0f}] 区间："
                + "；".join(
                    f"段 {g.segment_no} {len((g.text or '').strip())} 字（{ratio:.0%}）"
                    for g, ratio in outliers[:10]
                )
                + "。"
            ),
            suggestion=(
                "段长偏离设定多半是 max_chars 被临时改过、或模型提前收尾/续写失控。"
                "确认这些段是否可接受；不可接受则重生成。"
            ),
            rule="segment_length_outliers",
            evidence=evidence,
        )
    ]


def _ai_flavor_with_profile(text: str, profile: dict) -> float | None:
    """有文风档案时的 AI 腔打分：特征相对档案的平均 |z|。

    复用 `style.stylometry` 的 z_scores / metric_mean_abs_z——档案是
    从参考语料（人类文本）拟合的，生成段相对它的离群程度就是「像不像
    人写」的可量化 proxy。取不到特征时返回 None（不猜）。
    """
    from app.style.stylometry import features, metric_mean_abs_z, z_scores

    try:
        vec = features(text)
        z = z_scores(vec, profile)
        return float(metric_mean_abs_z(vec, profile)) if z else None
    except Exception:  # noqa: BLE001 - 档案形状异常 → 本条降级不用
        return None


def _ai_flavor_without_profile(text: str) -> float:
    """无文风档案时的 AI 腔打分：套话密度（次/百字）。

    启发式：`_AI_CLICHE_PATTERNS` 的命中数 ÷ 字数 × 100。不是分类器，
    是粗筛——人类作者也用这些词，但密度显著更低（见常量注释）。
    """
    if not text:
        return 0.0
    hits = sum(len(p.findall(text)) for p in _AI_CLICHE_PATTERNS)
    return hits / max(1, len(text)) * 100.0


def rule_generation_ai_flavor(
    gens: list[Generation], project: Any, taken: set[str]
) -> list[DiagnosticIssue]:
    """AI 腔量化打分。

    两条路径（有项目文风档案 → z 分数离群；无档案 → 套话密度），
    都只标红不改稿——「像 AI」是风格判断，最终话语权在人。
    """
    profile = project.style_profile if isinstance(project.style_profile, dict) else None
    if not gens:
        return []

    scored: list[tuple[Generation, float, str]] = []
    for g in gens:
        text = (g.text or "").strip()
        if not text:
            continue
        if profile:
            value = _ai_flavor_with_profile(text, profile)
            if value is None:
                continue
            if value > AI_FLAVOR_MEAN_ABS_Z_WARN:
                scored.append((g, value, f"特征 |z| 均值 {value:.2f}（阈值 {AI_FLAVOR_MEAN_ABS_Z_WARN}）"))
        else:
            density = _ai_flavor_without_profile(text)
            if density > AI_CLICHE_DENSITY_WARN:
                scored.append((g, density, f"套话密度 {density:.1f} 次/百字（阈值 {AI_CLICHE_DENSITY_WARN}）"))
    if not scored:
        return []
    evidence = tuple(
        IssueEvidence(
            kind="generation",
            source=_kg_source(),
            ref_id=str(g.id),
            snippet=f"段 {g.segment_no}：{detail}",
            score=0.4,
        )
        for g, _, detail in scored[:MAX_EVIDENCE_PER_ISSUE]
    )
    mode = "文风档案 z 分数" if profile else "套话密度启发式（无档案）"
    return [
        DiagnosticIssue(
            id=make_issue_id("ai_flavor_score", f"n{len(scored)}", taken),
            type="generation_quality",
            severity="warn",
            title=f"{len(scored)} 个段落 AI 腔打分离群",
            claim=(
                f"打分方式：{mode}。以下段落超过阈值："
                + "；".join(f"段 {g.segment_no}（{detail}）" for g, _, detail in scored[:10])
                + "。"
            ),
            suggestion=(
                "AI 腔是风格问题不是事实问题：可对这些段做提示词层面的"
                "文风约束（prompt_override），或重生成。是否算问题由人判断——"
                "本规则只提供可量化的信号。"
            ),
            rule="ai_flavor_score",
            evidence=evidence,
        )
    ]


#: plot_bible 登记角色名时常带的身份 / 职业 / 描述性前缀。
#:
#: 形如「记者小杨」「神秘失踪者A」「上司老李」——bible 里写全了身份，
#: 正文里却只用核心名（实测 project 5：正文「小杨」54 次、「失踪者A」10 次，
#: 而全名「记者小杨」「神秘失踪者A」0 次）。拿全名做精确子串匹配必假阳。
#:
#: 收录原则：**只收几乎不会单独成名的前缀**（职业 / 职务 / 关系 / 描述性
#: 修饰），不收姓氏与单字通配——前缀一放宽，任何名字都能被剥成两三个字，
#: 匹配就退化成「随便命中」。
_IDENTITY_PREFIXES: tuple[str, ...] = (
    # 职业 / 职务
    "记者", "编辑", "作家", "医生", "律师", "老师", "教授", "警察", "刑警",
    "法医", "护士", "司机", "保姆", "保安", "秘书", "助手", "保镖", "侦探",
    "老板", "经理", "主任", "所长", "局长", "队长", "组长", "书记", "村长",
    "导演", "编剧", "演员", "歌手", "学生", "士兵", "军官", "将军",
    "工程师", "程序员", "会计师",
    # 关系 / 称谓
    "同事", "邻居", "同学", "同乡", "上司", "下属", "师父", "徒弟",
    "师兄", "师姐",
    # 描述性修饰（bible 常用特征标注还没起名的角色）
    "神秘", "年轻", "资深", "首席", "退休", "陌生", "可疑", "失踪",
)

#: 剥前缀后核心名的最小长度。
#:
#: 2 是硬底线：1 字核心（「记者小李」→「李」）在中文正文里几乎必然命中
#: 别人（姓李的一大把），匹配就失去了证据意义。剥完不足 2 字 → 不用核心名，
#: 只认全名。
_MIN_CORE_NAME_LEN = 2


#: 剥前缀后核心名的禁用头字。
#:
#: 这些字只在「X者」「X员」「X生」里当**后缀**——剥完它们跑到核心名开头，
#: 说明剥过头了（`失踪者B` 剥成 `者B`）。这种形态在正文里几乎必然匹配到
#: 别的东西（`幸存者B`、`目击者B` 都含 `者B`），留着它就是白放宽匹配。
_CORE_HEAD_DENY: tuple[str, ...] = ("者", "员", "生", "士")


def _bible_name_surface_forms(name: str) -> tuple[str, ...]:
    """一个 plot_bible 角色名的全部可匹配书写形态（全名 + 去前缀核心名）。

    例：`记者小杨` → `("记者小杨", "小杨")`；`神秘失踪者A` →
    `("神秘失踪者A", "失踪者A")`；`许七安` → `("许七安",)`（无前缀可剥）。

    只剥**一层**、且只剥**开头**命中前缀表的部分：
    - 只剥一层：剥两层（`神秘失踪者A` → `失踪者A` → `者A`）会把名字削成
      无意义的碎片，误命中率陡增；
    - 只剥开头：前缀的定义就是「写在名字前面的身份」，不是任意子串；
    - 剥完不足 `_MIN_CORE_NAME_LEN` 字 → 丢弃该形态。
    """
    full = (name or "").strip()
    if not full:
        return ()
    forms = [full]
    for prefix in _IDENTITY_PREFIXES:
        if not full.startswith(prefix):
            continue
        core = full[len(prefix):].strip()
        if (
            len(core) >= _MIN_CORE_NAME_LEN
            and core[0] not in _CORE_HEAD_DENY
        ):
            forms.append(core)
        break  # 一层即止；多个前缀命中时按表内顺序取第一个
    return tuple(dict.fromkeys(forms))


def _name_in_bible(name: str, bible_names: Sequence[str]) -> bool:
    """判定 `name` 是否就是 bible 名单里的某个角色（双向包含）。

    命中条件（任一）：
    - 与某个 bible 名的**任一书写形态**完全相同；
    - `name` 包含某个形态（正文里叫「记者小杨」，bible 登记「小杨」）；
    - `name` 被某个形态包含（bible 登记「记者小杨」，正文里叫「小杨」）。

    为什么允许双向包含而不是只做相等：bible 与正文的命名口径本来就不一致，
    只认相等就得等两边都改对齐——那是数据治理，不是审核规则能等到的。
    """
    target = (name or "").strip()
    if not target:
        return False
    for bible_name in bible_names:
        for form in _bible_name_surface_forms(bible_name):
            if not form:
                continue
            if target == form or form in target or target in form:
                return True
    return False


def _bible_name_appears(name: str, full_text: str) -> bool:
    """plot_bible 角色 `name` 是否在正文里出场（全名匹配不上时回退核心名）。

    ## 为什么需要回退（F3）
    原实现是 `name not in full_text`——拿 bible 的**带身份前缀全名**去正文
    做精确子串匹配，而正文只用短名 → 主角被报成「从未出场」（实测 project 5：
    「小杨」54 次、「失踪者A」10 次，全名 0 次）。

    ## 为什么不复用 `app.kg.entity_resolve`
    那个模块只处理「X的/之/者 + 单字亲属称谓」（`林悠然的父亲` → `父亲`），
    与本次的形状（身份前缀 + 核心名）不重叠：`神秘失踪者A` 经
    `parse_canonical` 原样返回。且它的核心是**单字**亲属称谓，放进本规则的
    正文匹配会退化成「正文出现『父亲』就算该角色出场」——那是把
    `林悠然的父亲` 和任何人的父亲混为一谈，误伤远大于收益。故不复用，
    判据边界写在本函数里。

    ## 误伤边界（镜像缺陷「短名搭长名便车」为什么不会重现）
    那条缺陷的形状是：**短名被当成多个不同长名的同一实体**，在 KG 里连出
    错边（`凤年--[对话]-->徐凤`）。本规则的形状不同，且副作用方向相反：
    - 本规则只回答「要不要给用户报一条 warn」，产出的是**告警**，不是图数据，
      放宽匹配不会写出任何边；
    - 放宽的方向是**少报**（把假阳抹掉），不是多报。残留风险是漏报——
      bible 记 `记者小杨`，正文里的 `小杨` 其实是另一个人（如 `陈小杨`）
      时不报。这个残留是刻意接受的：`从未出场` 是 warn 级**提示**，
      报错的代价（用户以为漏写了主角、去改剧情）大于漏报的代价
      （用户自己知道谁出场了）；
    - 核心名 ≥2 字 + 只剥一层 + 前缀表只收不成名的词，把漏报压在
      「同一个核心名被两个不同角色共用」这一种情形上。真出现这种情形时
      （bible 里同时有 `记者小杨` 与 `邻居小杨`），任一出现即视为出场，
      也不报——宁可不报警，也不指认错人。
    """
    if not name:
        return False
    for form in _bible_name_surface_forms(name):
        if form and form in full_text:
            return True
    return False


def _bible_character_names(project: Any) -> list[str]:
    """plot_bible 里的角色名清单（缺表/坏形状 → 空列表，不抛）。"""
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    names: list[str] = []
    for c in bible.get("characters") or []:
        if isinstance(c, dict):
            name = (c.get("name") or "").strip()
        elif isinstance(c, str):
            name = c.strip()
        else:
            name = ""
        if name:
            names.append(name)
    return names


def rule_generation_bible_character_mismatch(
    db: Any, project: Any, gens: list[Generation], taken: set[str]
) -> list[DiagnosticIssue]:
    """与 plot_bible 角色名单不一致（双向，分开报）。"""
    bible_names = _bible_character_names(project)
    if not bible_names:
        return []
    full_text = "\n".join((g.text or "") for g in gens)
    issues: list[DiagnosticIssue] = []

    # 方向 ①：bible 角色在全部生成段中零出现（计划了却没写）
    # 匹配走 `_bible_name_appears`：全名 → 去身份前缀的核心名，两级都找不到
    # 才算「没出场」（见该函数的误伤边界说明）。
    absent = [n for n in bible_names if not _bible_name_appears(n, full_text)]
    if absent:
        evidence = tuple(
            IssueEvidence(
                kind="kg",
                source=_kg_source(),
                ref_id=f"bible_character:{n}",
                snippet=(
                    f"plot_bible 角色「{n}」在全部生成段中零出现"
                    f"（已按全名与去身份前缀的核心名各查一次）"
                ),
                score=0.5,
            )
            for n in absent[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id("bible_character_absent", f"n{len(absent)}", taken),
                type="consistency",
                severity="warn",
                title=f"{len(absent)} 个 plot_bible 角色从未出场",
                claim=(
                    f"plot_bible 登记了这些角色，但全部 {len(gens)} 个生成段里"
                    f"一次都没出现：{absent[:10]}。"
                ),
                suggestion=(
                    "确认是「还没写到」还是「已废案」：后者应从 plot_bible 撤下，"
                    "否则角色锁定清单会一直带着不出场的人名。"
                ),
                rule="bible_character_absent",
                evidence=evidence,
            )
        )

    # 方向 ②：高频出场角色不在 bible（可能是新角色，交人确认）
    kg_chars = list(
        db.scalars(
            select(KgNode).where(
                KgNode.project_id == project.id,
                KgNode.source == "generated",
                KgNode.type == "character",
            )
        ).all()
    )
    # 方向 ② 与方向 ① 必须用**同一套**匹配口径：一边说「记者小杨 == 小杨」、
    # 另一边说「小杨不在 bible 里」，两条规则会在同一次审核里自相矛盾。
    extra = []
    for n in kg_chars:
        name = (n.props or {}).get("name")
        if not name or _name_in_bible(name, bible_names):
            continue
        mentions = (n.props or {}).get("mentions") or 0
        if int(mentions) >= PROMINENT_CHARACTER_MENTIONS:
            extra.append((name, int(mentions)))
    if extra:
        extra.sort(key=lambda item: (-item[1], item[0]))
        evidence = tuple(
            IssueEvidence(
                kind="kg",
                source=_kg_source(),
                ref_id=f"kg_character:{name}",
                snippet=f"出场角色「{name}」mentions={mentions}，不在 plot_bible 名单",
                score=0.4,
            )
            for name, mentions in extra[:MAX_EVIDENCE_PER_ISSUE]
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id("generation_character_not_in_bible", f"n{len(extra)}", taken),
                type="consistency",
                severity="info",
                title=f"{len(extra)} 个高频出场角色不在 plot_bible",
                claim=(
                    f"这些角色出场 ≥{PROMINENT_CHARACTER_MENTIONS} 次但不在 "
                    f"plot_bible 名单："
                    + "、".join(f"{n}（{m} 次）" for n, m in extra[:10])
                    + "。"
                ),
                suggestion=(
                    "新角色随剧情出现是正常的；该进 bible 的（主要人物/关系方）"
                    "补进去，后续的角色锁定与伏笔约束才覆盖得到。"
                ),
                rule="generation_character_not_in_bible",
                evidence=evidence,
            )
        )
    return issues


def run_generation_audit(db: Any, project: Any) -> tuple[DiagnosticReport, dict[str, Any]]:
    """跑全量生成物审核规则（按项目）。"""
    gens = list(
        db.scalars(
            select(Generation)
            .where(Generation.project_id == project.id)
            .order_by(Generation.segment_no)
        ).all()
    )
    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []
    rule_errors: dict[str, Any] = {}

    def _run(name: str, func) -> None:
        try:
            issues.extend(func())
        except Exception as exc:  # noqa: BLE001 - 单规则失败不拖垮整次审核
            rule_errors[f"rule:{name}"] = {"error": str(exc)[:200]}

    _run("no_generations", lambda: rule_generation_no_segments(gens, project, taken))
    _run("empty_segment_text", lambda: rule_generation_empty_text(gens, taken))
    _run("segment_length_outliers", lambda: rule_generation_length_outliers(gens, project, taken))
    _run("ai_flavor_score", lambda: rule_generation_ai_flavor(gens, project, taken))
    _run(
        "plot_bible_character_mismatch",
        lambda: rule_generation_bible_character_mismatch(db, project, gens, taken),
    )

    stats = {
        "project_id": int(project.id),
        "segments_audited": len(gens),
        "generation_length": int(project.generation_length or 3000),
        "rule_errors": rule_errors,
    }
    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=[
            "no_generations",
            "empty_segment_text",
            "segment_length_outliers",
            "ai_flavor_score",
            "bible_character_absent",
            "generation_character_not_in_bible",
        ],
    )
    return report, stats
