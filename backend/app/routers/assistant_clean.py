"""Assistant 语料清洗路由（第 10 步）：POST /api/assistant/clean-corpus。

## 这一步补的是什么

第 8 步的确定性清洗（`app.ingestion.clean`）只覆盖**固定模式**的脏（盗版站
SEO 水印）。但错字 / 断字 / 残句需要**语义判断**，固定模式覆盖不到。本端点
把两者接起来：

    确定性检测（`app.agents.text_clean`，纯规则、可离线）
        → 命中可疑片段连同上下文交 Assistant provider 复核
        → 产出「建议修正」(before / after / 理由 / 置信度)
        → 每条修正落成一条 **pending 提案**（`corpus_text_edit`）

## 三条硬约束（派单红线）

1. **规则先行、模型后置**：确定性检测**没命中的分片不调模型**；provider 抛错
   时降级为「只返回确定性检测结果 + 明确说明模型未参与」，**不假装复核过**
   （响应用 `model_participated=false` + `model_note` 显式表达）。
2. **绝不静默改语料正文**：本端点**不写 `novel_chunks`**。产物只是一批
   `pending` 提案（`type=corpus_text_edit`, `target_kind=novel_chunk_text`），
   要生效必须再走 `approve → apply`（复用既有唯一写入通道）。
3. **只读 + 留痕**：本端点自身只写助手审计表（AgentRun / Evidence）；每次调用
   落一条 `role='assistant'` 的 AgentRun。

## 为什么把「只提示、不自动改」单列

单字成段 / 连续超短段 / 低频生僻字都可能是作者风格或人名/专有名词，
`text_clean` 把它们划为**仅提示类**（`HINT_ONLY_RULES`）：只出 issue，
不出 `suggested_edit`、更不产提案。详见 `app.agents.text_clean` 的划线表。
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import proposals as proposal_lib
from app.agents import text_clean
from app.agents.diagnosis import (
    DiagnosticIssue,
    DiagnosticReport,
    IssueEvidence,
    make_issue_id,
)
from app.db import get_db
from app.models import AgentRun, NovelChunk, Project, RawNovel
from app.routers.assistant_shared import (
    _chat_with_assistant,
    _confidence_from_evidence,
    _now_utc,
    _request_hash,
    _require_project,
    evidence_rows_to_dicts,
    persist_evidence,
)
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant_clean")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# ====================================================================
# 常量（含取值依据）
# ====================================================================

#: 单次扫描的分片数默认值。**默认小值**是刻意的：一本 500 万字小说有上千分片，
#: 一次跑全库既慢又会产出海量提案。默认 20 ≈「一次复核一屏」。
DEFAULT_CLEAN_CHUNK_LIMIT = 20

#: 单次扫描分片数上限（防误传大数把请求打成全库扫描）。
MAX_CLEAN_CHUNK_LIMIT = 200

#: 单次最多创建的提案数（每个命中分片最多一条）。与 `MAX_BULK_APPLY` 对齐：
#: 清洗产出的批次可以直接喂给受控批量应用，规模统一。
MAX_CLEAN_PROPOSALS_PER_CALL = 50

#: 建议修正进入「可提案」的置信度门槛。低于门槛的仍作为建议返回（供人看），
#: 但不自动落提案 —— 避免把低把握的猜测堆进审批列表。
CONFIDENCE_FLOOR = 0.4

#: 模型修正的最小相似度：低于此值视为「模型大段改写了原文」，直接丢弃。
#: 这是「模型把正确文本改错」的第一道护栏（见派单「如何避免模型改错」）。
MODEL_MIN_SIMILARITY = 0.85

#: issue.type（语料正文级脏文本）。
ISSUE_TYPE = "corpus_text_quality"

#: Evidence 片段上下文左右扩增字数。
_CONTEXT_PAD = 20


# ====================================================================
# 请求体
# ====================================================================


class CleanCorpusRequest(StrictRequest):
    """POST /api/assistant/clean-corpus 请求体。

    `project_id` 或 `novel_id` 二选一（至少给一个）：语料是**全局**的，
    `project_id` 在清洗里只是 AgentRun 的审计锚点（`agent_runs.project_id`
    非空）；`novel_id` 限定要清洗的语料。显式给了不存在的 id → 404（fail loud，
    不静默清洗一个空集）。`limit` 默认小值防一次跑全库。`dry_run=true` 只检测
    与复核、不创建提案。
    """

    project_id: int | None = None
    novel_id: int | None = None
    chunk_ids: list[int] | None = None
    limit: int = DEFAULT_CLEAN_CHUNK_LIMIT
    dry_run: bool = False


# ====================================================================
# 取数 / 校验
# ====================================================================


def _resolve_anchor_project(db: Session, project_id: int | None,
                            novel_id: int | None) -> Project:
    """确定 AgentRun 的审计锚点项目。

    - 显式给了 `project_id` → 必须存在（404）；
    - 只给 `novel_id` → 优先选**引用了该语料**的项目，否则回退到最早的项目；
      一个项目都没有 → 409（`agent_runs.project_id` 非空，没有锚点就没法留痕）。
    """
    if project_id is not None:
        return _require_project(db, project_id)
    for proj in db.scalars(select(Project).order_by(Project.id.asc())).all():
        refs = list(proj.reference_novel_ids or [])
        if novel_id in refs:
            return proj
    first = db.scalars(select(Project).order_by(Project.id.asc()).limit(1)).first()
    if first is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "语料清洗需要至少一个项目作为审计锚点（agent_runs.project_id 非空）；"
                "请先创建项目，或在请求体里显式传 project_id"
            ),
        )
    return first


def _require_scope(db: Session, novel_id: int | None,
                   chunk_ids: list[int] | None) -> None:
    """显式给定的 novel_id / chunk_ids 必须存在 → 否则 404（不静默审空集）。"""
    if novel_id is not None and db.get(RawNovel, novel_id) is None:
        raise HTTPException(status_code=404, detail=f"语料 novel_id={novel_id} 不存在")
    if chunk_ids:
        missing = [cid for cid in dict.fromkeys(chunk_ids)
                   if db.get(NovelChunk, cid) is None]
        if missing:
            raise HTTPException(status_code=404, detail=f"分片不存在：{missing}")


def _select_chunks(db: Session, *, novel_id: int | None,
                   chunk_ids: list[int] | None, limit: int) -> list[NovelChunk]:
    """按确定顺序取分片（novel_id, idx, id 升序），并施加 limit。"""
    q = (
        select(NovelChunk)
        .order_by(NovelChunk.novel_id.asc(), NovelChunk.idx.asc(), NovelChunk.id.asc())
    )
    if chunk_ids:
        q = q.where(NovelChunk.id.in_(list(dict.fromkeys(chunk_ids))))
    if novel_id is not None:
        q = q.where(NovelChunk.novel_id == novel_id)
    return list(db.scalars(q.limit(limit)).all())


# ====================================================================
# findings → issues
# ====================================================================


def _context(text: str, start: int, end: int, pad: int = _CONTEXT_PAD) -> str:
    """取包含可疑片段的**原样子串**（命中处前后各扩 pad 字），供人直接复核。

    刻意不加省略号：证据的价值就在于「这就是原文那一段」。
    """
    if not text:
        return ""
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e].replace("\n", "\\n")


#: 各判据的处理建议（人可读；说明该不该自动改）。
_SUGGESTIONS: dict[str, str] = {
    text_clean.RULE_REPLACEMENT_CHAR:
        "删除替换符 U+FFFD（可走 corpus_text_edit 提案，before/after 已给）。",
    text_clean.RULE_ILLEGAL_CONTROL:
        "删除非法控制符；并核对导入时的编码探测是否把二进制当成了文本。",
    text_clean.RULE_ENCODING_RESIDUE:
        "这多半是编码转换事故：确认原文编码（GBK/GB18030）后重新导入该语料，"
        "或人工核对本处片段后修正。",
    text_clean.RULE_BROKEN_LINE:
        "核对是否句子被硬换行从中切断；是则把断行并回（需人确认，勿自动改）。",
    text_clean.RULE_SINGLE_CHAR_PARA:
        "单字成段可能只是作者的强调式风格 —— 仅提示，不建议自动改。",
    text_clean.RULE_ULTRA_SHORT_RUN:
        "连续超短段可能是切分事故，也可能是刻意排版 —— 仅提示，不建议自动改。",
    text_clean.RULE_DUP_ADJACENT:
        "合并相邻重复字（如「他他」→「他」）。",
    text_clean.RULE_RARE_CHAR:
        "低频生僻字可能是错字，也可能是人名/专有名词/异体字 —— 仅提示，不建议自动改。",
}


def _findings_to_issues(
    findings: list[text_clean.TextFinding],
    chunks_by_id: dict[int, NovelChunk],
    taken: set[str],
) -> list[DiagnosticIssue]:
    """把确定性 finding 转成 diagnosis 同款九字段 issue（带位置证据）。"""
    issues: list[DiagnosticIssue] = []
    for f in findings:
        chunk = chunks_by_id.get(f.chunk_id) if f.chunk_id is not None else None
        text = (chunk.text if chunk is not None else "") or ""
        ctx = _context(text, f.start, f.end)
        evidence = IssueEvidence(
            kind="chunk",
            source=f"novel:{f.novel_id}",
            ref_id=str(f.chunk_id),
            snippet=ctx or f.fragment,
            score=f.confidence,
        )
        issues.append(
            DiagnosticIssue(
                id=make_issue_id(f.rule, f"{f.chunk_id}:{f.start}", taken),
                type=ISSUE_TYPE,
                severity=f.severity,
                title=f"[{f.rule}] 分片 {f.chunk_id} 偏移 {f.start}",
                claim=f"{f.detail}；原文片段：「{ctx}」",
                suggestion=_SUGGESTIONS[f.rule],
                rule=f.rule,
                evidence=(evidence,),
            )
        )
    return issues


# ====================================================================
# 模型复核
# ====================================================================


def _parse_model_json(raw: str | None) -> list[dict] | None:
    """从模型输出里抽 JSON（容错：允许前后有解释性文字）。

    找不到合法 JSON / 形状不符 → `None`（调用方据此走「模型未参与」）。
    """
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        results = data.get("results")
        return results if isinstance(results, list) else None
    if isinstance(data, list):
        return data
    return None


def _guard_model_correction(original: str, corrected: str) -> tuple[bool, str]:
    """护栏：模型对整段的修正必须「小改」，否则丢弃。

    三条：
    1. 非空、与原文不同；
    2. 长度比在 [0.5, 1.5] 之间（防模型把整段删掉/灌水）；
    3. `difflib` 相似度 ≥ `MODEL_MIN_SIMILARITY`（防模型大段重写）。

    这是「避免模型把正确文本改错」的核心护栏：只有明确判据命中的分片才送模型，
    再叠一层「改动必须小」的硬约束，超界一律丢弃并记进 `model_rejected`。
    """
    if not isinstance(corrected, str) or not corrected.strip():
        return False, "空输出"
    if corrected == original:
        return False, "无改动"
    ratio = len(corrected) / max(1, len(original))
    if ratio < 0.5 or ratio > 1.5:
        return False, f"长度比 {ratio:.2f} 越界（允许 0.5–1.5）"
    sim = difflib.SequenceMatcher(None, original, corrected).ratio()
    if sim < MODEL_MIN_SIMILARITY:
        return False, f"相似度 {sim:.2f} < {MODEL_MIN_SIMILARITY}（疑似大段改写，已丢弃）"
    return True, ""


def _review_with_assistant(
    project: Project,
    eligible: list[NovelChunk],
    findings_by_chunk: dict[int, list[text_clean.TextFinding]],
) -> tuple[dict[int, dict], str, str]:
    """对「命中需要模型判断的判据」的分片做一次 Assistant 复核。

    返回 `(chunk_id -> {corrected, confidence, note}, model_label, model_note)`。

    - `eligible` 为空 → 不调模型，`model_note="no_eligible_chunks"`；
    - provider 抛错（`_chat_with_assistant` 返回空 label）→ `"provider_unavailable"`，
      上层据 `model_participated=False` 明确声明「模型未参与」；
    - 输出不可解析 → `"model_output_unparseable"`。
    """
    if not eligible:
        return {}, "", "no_eligible_chunks"

    blocks: list[str] = []
    for chunk in eligible:
        fs = findings_by_chunk.get(int(chunk.id), [])
        rules = sorted({f.rule for f in fs})
        blocks.append(f"### chunk_id={chunk.id} | 命中规则={rules}")
        blocks.append(chunk.text or "")
        blocks.append("")

    system = (
        "你是中文网文的语料清洗助手。用户给出若干段落及其**确定性命中**的疑似缺陷"
        "（乱码替换符 / 非法控制符 / 编码残留 / 断字 / 相邻重复字）。"
        "严格要求：① 只修复这些缺陷，其余文字必须**逐字保持不变**；"
        "② 不要改写文风、不要润色、不要调整标点、不要增删内容；"
        "③ 若某段无需修改，就原样返回该段；"
        "④ 不确定就**不要改**（宁可原样返回）。"
        "输出**严格 JSON**，不要任何 JSON 之外的文字，形状："
        '{"results":[{"chunk_id":<int>,"corrected":"<逐字修正后的整段文本>",'
        '"note":"<一句话说明改了什么>","confidence":<0.0-1.0>}]}'
    )
    user = (
        f"项目：{project.title or '未命名'}\n"
        f"待复核分片数：{len(eligible)}\n\n"
        + "\n".join(blocks)
    )
    fallback = "(Assistant provider 不可用，未做模型复核)"
    raw, label = _chat_with_assistant(system=system, user=user, fallback=fallback)
    if not label:
        return {}, "", "provider_unavailable"

    parsed = _parse_model_json(raw)
    if parsed is None:
        return {}, label, "model_output_unparseable"

    out: dict[int, dict] = {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            cid = int(item.get("chunk_id"))
        except (TypeError, ValueError):
            continue
        corrected = item.get("corrected")
        if not isinstance(corrected, str):
            continue
        try:
            conf = float(item.get("confidence"))
        except (TypeError, ValueError):
            conf = 0.5
        out[cid] = {
            "corrected": corrected,
            "confidence": round(min(1.0, max(0.0, conf)), 4),
            "note": str(item.get("note") or ""),
        }
    return out, label, ("ok" if out else "no_suggestions")


# ====================================================================
# 建议修正
# ====================================================================


def _build_suggested_edits(
    chunks_by_id: dict[int, NovelChunk],
    findings_by_chunk: dict[int, list[text_clean.TextFinding]],
    model_map: dict[int, dict],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """为每个有命中的分片合成**至多一条**建议修正（before/after 齐全）。

    优先级：模型复核结果（通过护栏）> 确定性修正。二者都没有 → 不出建议。
    每个分片只出一条 —— 同分片若出多条提案，第一条 apply 后其余会立刻 stale。
    """
    edits: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    for cid in chunks_by_id:
        chunk = chunks_by_id[cid]
        fs = findings_by_chunk.get(cid, [])
        if not fs:
            continue
        before = chunk.text or ""
        rules_hit = {f.rule for f in fs}
        after: str | None = None
        source: str | None = None
        conf = 0.0
        reason = ""

        mm = model_map.get(cid)
        if mm is not None:
            ok, why = _guard_model_correction(before, mm["corrected"])
            if ok:
                after = mm["corrected"]
                source = "model"
                conf = float(mm.get("confidence", 0.5))
                reason = "模型复核（仅修命中缺陷）：" + (mm.get("note") or "修正乱码/断字/重复字")
            else:
                rejects.append({"chunk_id": cid, "reason": why})

        if after is None:
            fixed = text_clean.deterministic_fix(before, rules_hit)
            if fixed is not None:
                after = fixed
                source = "rule"
                conf = text_clean.fix_confidence(rules_hit)
                fixed_rules = sorted(rules_hit & text_clean.RULE_FIXABLE)
                reason = "确定性修正：" + "、".join(fixed_rules)

        if after is None:
            continue

        edits.append({
            "chunk_id": cid,
            "novel_id": chunk.novel_id,
            "rules": sorted(rules_hit),
            "source": source,
            "before": before,
            "after": after,
            "reason": reason,
            "confidence": round(float(conf), 4),
            "proposed": False,
        })
    return edits, rejects


def _proposal_evidence(
    findings: list[text_clean.TextFinding], chunk: NovelChunk
) -> list[dict[str, Any]]:
    """提案 evidence_json：把该分片的确定性 finding 结构化附上（供审批人复核）。"""
    return [
        {
            "kind": "chunk",
            "source": f"novel:{f.novel_id}",
            "ref_id": str(f.chunk_id),
            "snippet": f.fragment,
            "score": f.confidence,
            "rule": f.rule,
            "detail": f.detail,
        }
        for f in findings
    ]


def _compose_answer(
    *,
    issue_count: int,
    edits: list[dict[str, Any]],
    proposal_count: int,
    chunks_scanned: int,
    model_participated: bool,
    model_note: str,
    dry_run: bool,
) -> str:
    """人可读结语；明确区分「模型参与 / 未参与」。"""
    if issue_count == 0:
        return (
            f"已扫描 {chunks_scanned} 个分片，八条确定性判据全部执行，未命中任何"
            f"可疑点；规则未命中，未调用 Assistant 模型。"
        )
    if model_participated:
        head = (
            f"确定性检测命中 {issue_count} 处可疑点，已把命中分片连同上下文交由 "
            f"Assistant 复核。"
        )
    elif model_note == "no_eligible_chunks":
        head = (
            f"确定性检测命中 {issue_count} 处可疑点，均属「仅提示」判据"
            f"（单字成段 / 连续短段 / 低频生僻字），**未经模型判断**，也未自动改。"
        )
    else:
        head = (
            f"确定性检测命中 {issue_count} 处可疑点；Assistant provider 未参与"
            f"（{model_note}），**未做任何模型复核**，以下仅为确定性结果。"
        )
    if dry_run:
        tail = f"dry_run=true：产出 {len(edits)} 条建议修正，未创建任何提案。"
    else:
        tail = (
            f"已创建 {proposal_count} 条 pending 提案（corpus_text_edit），"
            f"需人批准后 apply 才会写入 novel_chunks。"
        )
    return head + tail


# ====================================================================
# POST /api/assistant/clean-corpus
# ====================================================================


@router.post("/clean-corpus")
def assistant_clean_corpus(
    body: CleanCorpusRequest, db: Session = Depends(get_db)
) -> dict:
    """语料正文清洗：确定性检测 → 模型复核 → 产出待批提案（**不写正文**）。

    响应含：`answer` / `issues[]`（沿用 diagnosis 九字段）/ `suggested_edits[]`
    （每条带 before/after/reason/confidence/source）/ `proposals[]`（pending，
    `corpus_text_edit`）/ `confidence` / `agent_run_id` / `model_participated`。
    """
    if body.project_id is None and body.novel_id is None:
        raise HTTPException(
            status_code=422, detail="必须至少提供 project_id 或 novel_id 之一"
        )
    if body.limit < 1 or body.limit > MAX_CLEAN_CHUNK_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=f"limit 必须在 1..{MAX_CLEAN_CHUNK_LIMIT} 之间，收到 {body.limit}",
        )
    _require_scope(db, body.novel_id, body.chunk_ids)
    project = _resolve_anchor_project(db, body.project_id, body.novel_id)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=_now_utc(),
        finished_at=None,
        request_hash=_request_hash(body),
        model="",
        note="语料清洗（检测+复核）",
    )
    db.add(run)
    db.flush()
    run_id = int(run.id)

    # ---- 1) 取分片 + 确定性检测（纯规则、只读）----
    chunks = _select_chunks(
        db, novel_id=body.novel_id, chunk_ids=body.chunk_ids, limit=body.limit
    )
    chunks_by_id = {int(c.id): c for c in chunks}
    findings = text_clean.scan_corpus(
        [(int(c.id), int(c.novel_id), c.text or "") for c in chunks]
    )
    findings_by_chunk: dict[int, list[text_clean.TextFinding]] = {}
    for f in findings:
        if f.chunk_id is not None:
            findings_by_chunk.setdefault(int(f.chunk_id), []).append(f)

    by_rule: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1

    taken: set[str] = set()
    issues = _findings_to_issues(findings, chunks_by_id, taken)
    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results={"chunks_scanned": len(chunks)},
        rules_evaluated=list(text_clean.ALL_RULES),
    )
    evidence_rows = persist_evidence(db, run_id, report)

    # ---- 2) 模型后置：只对「命中需要模型判断的判据」的分片复核 ----
    eligible = [
        chunks_by_id[cid]
        for cid in sorted(chunks_by_id)
        if any(f.is_model_eligible for f in findings_by_chunk.get(cid, []))
    ]
    model_map, model_label, model_note = _review_with_assistant(
        project, eligible, findings_by_chunk
    )
    model_participated = model_note in ("ok", "no_suggestions")

    # ---- 3) 合成建议修正（每条一 chunk）----
    edits, rejects = _build_suggested_edits(chunks_by_id, findings_by_chunk, model_map)

    # ---- 4) 落 pending 提案（**不写 novel_chunks**）----
    created: list[Any] = []
    if not body.dry_run:
        for e in edits:
            if e["confidence"] < CONFIDENCE_FLOOR:
                e["skip_reason"] = (
                    f"置信度 {e['confidence']} < 门槛 {CONFIDENCE_FLOOR}（仅提示，不落提案）"
                )
                continue
            if len(created) >= MAX_CLEAN_PROPOSALS_PER_CALL:
                e["skip_reason"] = (
                    f"超过单次提案上限 {MAX_CLEAN_PROPOSALS_PER_CALL}（下次再触发剩余部分）"
                )
                continue
            chunk = chunks_by_id[int(e["chunk_id"])]
            try:
                proposal = proposal_lib.build_proposal(
                    db,
                    run_id=run_id,
                    project_id=int(project.id),
                    proposal_type="corpus_text_edit",
                    target_kind="novel_chunk_text",
                    target_id=str(e["chunk_id"]),
                    after_text=e["after"],
                    reason=e["reason"],
                    evidence=_proposal_evidence(
                        findings_by_chunk.get(int(e["chunk_id"]), []), chunk
                    ),
                    risk_level="medium",
                    created_by="clean-corpus",
                )
            except proposal_lib.ProposalError as exc:
                # 单条提案构造失败（越界 / 空变更 / 目标不存在…）不拖垮本次清洗：
                # build_proposal 在 add 之前失败，session 仍干净，记原因后继续。
                e["skip_reason"] = str(exc)[:200]
                continue
            except Exception as exc:  # noqa: BLE001 - 未预期错误：整批回滚并 fail loud
                db.rollback()
                run.status = "failed"
                run.finished_at = _now_utc()
                run.model = "(local_fallback)"
                db.commit()
                raise HTTPException(
                    status_code=500, detail=f"创建语料清洗提案失败：{str(exc)[:200]}"
                ) from exc
            e["proposed"] = True
            created.append(proposal)
        db.commit()

    # ---- 5) 收尾：run 记 success，model 回显（未过模型记 local_fallback）----
    run.status = "success"
    run.finished_at = _now_utc()
    run.model = model_label or "(local_fallback)"
    db.commit()

    answer = _compose_answer(
        issue_count=len(issues),
        edits=edits,
        proposal_count=len(created),
        chunks_scanned=len(chunks),
        model_participated=model_participated,
        model_note=model_note,
        dry_run=body.dry_run,
    )

    tool_results = {
        "chunks_scanned": len(chunks),
        "limit": body.limit,
        "novel_id": body.novel_id,
        "chunk_ids": body.chunk_ids,
        "findings_by_rule": dict(sorted(by_rule.items())),
        "findings_by_severity": dict(sorted(by_sev.items())),
        "model_note": model_note,
        "model_rejected": rejects,
        "hint_only_rules": sorted(text_clean.HINT_ONLY_RULES),
    }

    issue_dicts = [i.to_dict() for i in issues]
    return {
        "answer": answer,
        "issues": issue_dicts,
        # findings 兼容别名：与 diagnose / audit-* 响应同构，前端一份渲染逻辑复用
        "findings": issue_dicts,
        "suggested_edits": edits,
        "model_rejected": rejects,
        "proposals": [proposal_lib.proposal_to_dict(p) for p in created],
        "proposal_count": len(created),
        "dry_run": body.dry_run,
        "model_participated": model_participated,
        "model_note": model_note,
        "evidence": evidence_rows_to_dicts(evidence_rows),
        "confidence": _confidence_from_evidence(evidence_rows),
        "agent_run_id": run_id,
        "tool_results": tool_results,
        "rules_evaluated": list(text_clean.ALL_RULES),
        "scope": {
            "project_id": int(project.id),
            "novel_id": body.novel_id,
            "chunk_ids": body.chunk_ids,
            "limit": body.limit,
            "chunks_scanned": len(chunks),
        },
    }
