"""M4 生成引擎：上下文组装 → 续写 → 段级总结 → 入库（支持段级续写环）。

漂移防护机制：M3.3 的未填坑 + 角色约束进入 system 提示并全程锁定；
前情注入改走**层级摘要树**（L1 段 / L2 卷 / L3 主线骨架，见 summary_tree.py），
把无界的段级摘要链压成有界前情块——实测全量拼接时 N=300 会膨胀到单段正文的 656%。
"""

import copy
import logging

from sqlalchemy import select

from app.generation.prompts import (
    build_system_prompt,
    build_user_prompt,
    merged_constraint_items,
)
from app.generation.summary_tree import (
    budget_for_generation,
    compose_prior,
    empty_tree,
    l1_chain,
    record_l1,
    rollup_volumes,
    tree_before,
    tree_from_flat_summaries,
)
from app.kg.queries import get_reminders
from app.models import Generation, Project
from app.retrieval.search import hybrid_search
from app.style.exemplar import sample_style_exemplars


def _strip_thinking(text: str) -> str:
    """剥掉 MiniMax M2 等模型的  思维链，只留正文。

    双保险：provider 已显式传 thinking disabled，但 API 若忽略该参数，
    落库/注入前情的文本仍必须是干净的正文。
    """
    if " <think>" in text or " </think>" in text:
        end = text.rfind(" </think>")
        if end != -1:
            return text[end + len(" </think>"):].strip()
        # 只有开标签没有闭标签：从开标签后全部丢弃不可靠，退化为原样返回
    return text


def _next_segment_no(db, project_id: int) -> int:
    count = db.scalars(
        select(Generation.id).where(Generation.project_id == project_id)
    ).all()
    return len(count) + 1


def _summary_tree(project: Project) -> dict:
    """项目摘要树（**深拷贝**，可安全原地改）。

    两个必须这么做的理由：

    1. **旧项目引导**：步骤 2 之前落库的项目只有扁平 `summary`，没有树。直接从
       扁平链引导出一棵（L1 = 老摘要，段号按位置推定），已写内容一条不丢；
       L2/L3 的洞由下一次 rollup 按「整卷齐备」自动补上。
    2. **JSON 列不追踪原地改动**：SQLAlchemy 对普通 JSON 列只在**整体赋值**时
       判脏，原地 `tree["l1"][k] = v` 会被静默丢弃。故一律拷贝出、改完整体赋回，
       从写法上杜绝「测试里好使、落库后没保存」这类故障。
    """
    tree = project.summary_tree
    if isinstance(tree, dict) and tree:
        return copy.deepcopy(tree)
    return tree_from_flat_summaries(project.summary)


def _prior_summaries(project: Project) -> list[str]:
    """段级摘要链（L1 全文，按段号升序）。

    仍保留这个读法：RAG 查询词用它，且它是 `project.summary` 的等价视图。
    但**注入提示词的已不是它**——注入走 compose_prior 拼出的有界前情块。
    """
    chain = l1_chain(_summary_tree(project))
    if chain:
        return chain
    raw = project.summary
    if isinstance(raw, list):
        return [str(s) for s in raw if s]
    return []


def _rollup_fn(provider):
    """滚上函数：provider 自带 summarize_rollup 就用它，否则退回 LLMProvider 的确定性兜底。

    用 getattr 探测，而不是「先调用、抛错再吞」：测试替身/精简 provider 可能只有
    chat + summarize，靠异常兜底会把「没有滚上能力」和「滚上真的失败了」混成同一种
    信号——真出故障时反而查不出是哪一类。
    """
    fn = getattr(provider, "summarize_rollup", None)
    if fn is None:
        from app.llm.provider import LLMProvider

        fn = LLMProvider.summarize_rollup.__get__(provider, type(provider))
    return lambda texts, level: fn(texts, level)


def _rag_query(prior: list[str], project: Project) -> str:
    if prior:
        return prior[-1]
    parts = [project.title or ""]
    outline = project.outline
    if isinstance(outline, (list, tuple)) and outline:
        parts.append(str(outline[0]))
    return " ".join(p for p in parts if p)


def _merge_segment_into_graph(db, project_id: int, text: str, segment_no: int) -> dict:
    """生成后把本段事实增量合并进 generated 图谱与事实账本（闭环）。

    2026-10-17：四步编排（图谱 + 账本 + 别名归一 + 哨兵）已上移至
    `app.kg.build.merge_segment_into_kg`——此前 build.py 的公开入口
    `merge_graph_from_text` 只做图谱一步，Fact 账本不同步（live 验证
    实测复现：KgNode resolved 而 Fact 仍 open）。现在生成链路与公开入口
    走同一条四步路径，不存在"只更一半"的合并。本函数保留原名，
    调用点（generate_next_segment）不动。
    """
    from app.kg.build import merge_segment_into_kg

    return merge_segment_into_kg(db, project_id, text, segment_no)


def _require_distill_ready(db, project) -> None:
    """生成前档案预检（2026-10-17，修 live 验证复现的挂起洞）。

    项目有参考语料、但叙事档案既不在项目缓存也不在蒸馏库时，**明确 raise**
    而不是让生成链路走到 `_style_narrative → project_narrative → extract`：
    那条路会**同步**触发全量蒸馏（分钟级），HTTP 请求 240s 必超时，
    用户看到的是无限转圈、无从判断是卡死还是在跑（live 实测复现）。

    无参考语料的自由生成不受影响（project_narrative 对它返回 None，
    本来就不蒸馏）。

    Raises:
        ValueError: 需要先蒸馏。消息带明确可操作的下一步（端点路径）。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    if not ids:
        return
    from app.style.narrative import (
        corpus_content_hash,
        find_style_distill,
        narrative_fingerprint,
        resolve_tier,
    )

    cfg = resolve_tier(getattr(project, "narrative_tier", None))
    want = narrative_fingerprint(ids, cfg)
    cached = project.style_narrative or {}
    if isinstance(cached, dict) and cached.get("fingerprint") == want:
        return  # 项目缓存命中
    content_hash = corpus_content_hash(ids, db)
    if find_style_distill(db, ids, cfg.key, want["tier_signature"], content_hash) is not None:
        return  # 蒸馏库命中（生成链路会直接复用）
    raise ValueError(
        f"项目尚未蒸馏文风档案（当前档位：{cfg.name}）。请先调用 "
        f"POST /api/projects/{project.id}/distill 完成蒸馏后再生成——"
        "直接生成会同步触发分钟级蒸馏，请求必然超时。"
    )


def _commit_segment(db, project: Project, text: str, summary: str) -> tuple[Generation, int]:
    """落库一段：写 Generation + 追加 L1 到摘要树 + 同步扁平归档。

    `project.summary` **继续维护**，但它已从「注入源」退为「L1 无损归档」：
    前端/旧接口仍读它，而注入提示词的是 compose_prior 拼的有界前情块。
    两份都写不构成双写风险——它们由同一次 record_l1 派生，且树才是权威。
    """
    segment_no = _next_segment_no(db, project.id)
    gen = Generation(
        project_id=project.id,
        segment_no=segment_no,
        chapter_range=f"段{segment_no}",
        text=text,
        summary={"text": summary},
    )
    db.add(gen)

    tree = _summary_tree(project)
    record_l1(tree, segment_no, summary)
    project.summary_tree = tree

    running = list(project.summary) if isinstance(project.summary, list) else []
    running.append(summary)
    project.summary = running
    project.status = "generating"
    db.commit()
    db.refresh(gen)
    return gen, segment_no


def _rollup_summary_tree(db, project: Project, provider) -> dict:
    """惰性滚上 L2/L3；每段都调，但**只有封卷时才会真的发 LLM 请求**。

    与图谱合并同一条纪律：失败绝不能阻断生成主流程（段已落库，滚上只是优化），
    故吞掉异常回传状态。幂等由 rollup_volumes 保证，重复调用不会重复计费。
    """
    try:
        tree = _summary_tree(project)
        stats = rollup_volumes(tree, _rollup_fn(provider))
        project.summary_tree = tree
        db.commit()
        return stats
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return {"error": str(exc)[:100], "l2_created": [], "spine_updated": False}


def rebuild_summary_tree(db, project: Project, provider) -> dict:
    """按库里各段摘要**整体重建**摘要树（重生成路径专用）。

    为什么不能走增量：重生成会改动**任意一段**，受影响的卷摘要与主线骨架都基于旧内容，
    续滚只会把错的内容带着往下传。所以 L1 重排、L2/L3 从零重算。
    `record_l1` 对空摘要落占位，因此缺段不会把封卷永久卡死。
    """
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no)
    ).all()
    tree = empty_tree()
    for g in gens:
        record_l1(tree, g.segment_no, (g.summary or {}).get("text") or "")
    stats = rollup_volumes(tree, _rollup_fn(provider))
    project.summary_tree = tree
    project.summary = l1_chain(tree)
    project.status = "generating"
    db.commit()
    return stats


def build_context(db, project: Project, novel_ids: list[int] | None = None) -> dict:
    """组装生成所需的全部上下文（不调用 LLM，供 plan 预览与续写共用）。

    novel_ids 缺省时取 project.reference_novel_ids（用户选定的参考语料）。
    只检索「选定」的语料，绝不混入整库其他语料——这是防混库的第一道闸。
    多本语料各自独立 RAG，结果合并进 rag_context（纯文本检索，不污染图谱隔离）。
    """
    # 只取「用户自己小说」的图谱（generated），杜绝参考语料角色混入事实约束
    reminders = get_reminders(db, project.id, source="generated")
    prior = _prior_summaries(project)

    # 前情块**有界**：分层 + 预算裁剪，且带覆盖证明（report["uncovered"] 恒空）。
    # 预算随单段正文长度走——正文越长，前情允许多占一点，但比例固定，不随段数漂移。
    tree = _summary_tree(project)
    prior_blocks, prior_report = compose_prior(
        tree, budget_chars=budget_for_generation(project.generation_length)
    )

    if novel_ids is None:
        novel_ids = list(project.reference_novel_ids or [])

    rag_context: list[str] = []
    if novel_ids:
        query = _rag_query(prior, project)
        for nid in novel_ids:
            try:
                hits = hybrid_search(db, nid, query, k=4)
                rag_context.extend(h["text"] for h in hits)
            except Exception:  # noqa: BLE001 - 单本检索失败不阻断其余语料续写
                continue

    # RAG 结果可能含反向禁忌词（原文确实用了这些词，但反向禁忌禁止生成时使用）。
    # 不删除 RAG 结果（它们是相关剧情素材），但在注入 prompt 时标注清楚。
    narrative = _style_narrative(db, project)
    forbidden_words = []
    for items in ((narrative or {}).get("forbidden") or {}).values():
        forbidden_words.extend(items)
    rag_has_forbidden = bool(forbidden_words) and any(
        w in t for w in forbidden_words for t in rag_context
    )

    # 风格样例：从参考语料采样原文段落作为 few-shot，教模型「怎么写」。
    # 排除已被 RAG 命中的 chunk——同一段原文同时作为「相关剧情」和「风格样例」
    # 出现两次会让模型困惑。样例进 user prompt 不进 system（每次生成现采样，
    # 内容随段号变，进 system 会击穿提示词缓存）。
    style_exemplars = sample_style_exemplars(
        db, project, novel_ids, exclude_texts=set(rag_context)
    ) if novel_ids else []

    # constraints 是**真正下发进 prompt** 的那份（provider 会把它追加到 user 末尾做
    # 近因提醒，见 llm/provider._apply_constraints），因此它必须与 system 提示里的
    # 【角色人设约束】/【未填伏笔】用**同一个合并口径**，否则会出现「system 里列了、
    # 尾部自称完整的锁定名单却没列」的自相矛盾提示词，而漏掉的恰是最高权威层
    # plot_bible（用户构思）。口径统一在 prompts.merged_constraint_items。
    chars, fores = merged_constraint_items(reminders, project.plot_bible)
    constraints = {
        "characters": [
            c.get("name") for c in chars if isinstance(c, dict) and c.get("name")
        ],
        "plotholes": [f for f in fores if f],
    }

    return {
        "reminders": reminders,
        # 兼容读法：扁平 L1 链（RAG 查询词、旧接口、按段号对齐的断言都在用）
        "prior_summaries": prior,
        # 真正注入提示词的那份：有界、分层
        "prior_blocks": prior_blocks,
        "prior_report": prior_report,
        "rag_context": rag_context,
        "style_exemplars": style_exemplars,
        "rag_has_forbidden": rag_has_forbidden,
        "novel_ids": novel_ids,
        "constraints": constraints,
    }


def _style_info(project: Project) -> dict | None:
    """把向导选的 类型/风格平台 收成 style_dims 形态，喂给系统提示的【风格】块。"""
    info: dict = {}
    if project.genre:
        info["类型"] = project.genre
    if project.style:
        info["风格平台"] = project.style
    return info or None


def _style_narrative(db, project: Project) -> dict | None:
    """项目级叙事档案（LLM 提炼），按指纹缓存，整轮恒定。

    与 `_style_profile` 同理：叙事档案进的是静态 system 块，每次生成都变会击穿缓存。
    取不到就返回 None —— 叙事档案是增强，不是生成的前提。

    `persist_library=False`（P1-1 修复）：生成链路每次请求会调本函数 3 次
    （build_context + generate_next_segment/stream 各 2 次），命中缓存时
    只读返回即可——不算 corpus_content_hash（全量加载语料+sha256）、
    不写蒸馏库。落库是蒸馏提交路径（POST /distill）的职责。
    """
    from app.style.narrative import project_narrative_safe

    return project_narrative_safe(db, project, persist_library=False)


def _style_profile(db, project: Project) -> dict | None:
    """项目级文体档案（参考语料实测），按指纹缓存，整轮恒定。

    粒度取 `unit_chars = generation_length`：档案必须与生成段的长度可比，
    否则统计粒度不对齐会系统性偏移（`verify_stylometry` B 段有闸门）。
    取不到就返回 None —— 档案是增强，不是生成的前提。
    """
    from app.style import project_profile_safe

    return project_profile_safe(
        db, project, unit_chars=int(project.generation_length or 3000)
    )


#: 生成后文风告警的高分位阈值：percentile 高于此值报"文风偏离档案"。
#:
#: **与 verify_stylometry.py 的 97.5 方向一致**：
#: percentile 的语义是"有多大比例的留出单元距离 ≤ 本段"，distance 越大 percentile 越大。
#: 所以 percentile 高 = 比大多数人类样本都更偏离，percentile 低 = 更像正常文体。
#: 这里用高分位（97.5）判"异常偏离"——与离线验收同一条线，不搞两套口径。
#: 具体值由 `verify_stylometry.py` 在真实语料上扫 `unit_mean_abs_z` 分布后定，不拍脑袋。
STYLE_WARN_PERCENTILE = 97.5

#: AI 腔模式库：常见的中文 AI 写作套路。生成后检测这些模式，
#: 命中则触发针对性重写。与 system prompt 的【文风要求：忌 AI 腔】互补——
#: 提示词是预防，这里是补救。
AI_FLAVOR_PATTERNS = [
    "就在这时",
    "突然之间",
    "心中充满了",
    "眼中闪过一丝",
    "嘴角勾起",
    "嘴角微勾",
    "深吸一口气",
    "他知道，",
    "她明白，",
    "不禁",
    "忍不住",
    "仿佛",
    "宛如",
    "犹如",
    "像是",
    "似乎",
    "显然",
    "毫无疑问",
    "毫无疑问地",
    "毋庸置疑",
    "众所周知",
    "总的来说",
    "综上所述",
    "值得一提的是",
    "不难发现",
    "由此可见",
    "不仅如此",
    "更是",
    "原来",
    "终于",
    "从此",
    "意味着",
]


def _detect_ai_flavor(text: str) -> dict:
    """检测文本中的 AI 腔模式，返回 {pattern: count} 和总数。"""
    hits = {}
    total = 0
    for p in AI_FLAVOR_PATTERNS:
        c = text.count(p)
        if c > 0:
            hits[p] = c
            total += c
    return {"hits": hits, "total": total}


def _should_rewrite(text: str, forbidden: dict | None) -> tuple[bool, list[str]]:
    """判断是否需要重写。命中 AI 腔模式或反向禁忌时返回 True。

    阈值：AI 腔模式命中 ≥ 5 次，或反向禁忌命中 ≥ 3 次。
    """
    reasons = []
    flavor = _detect_ai_flavor(text)
    if flavor["total"] >= 5:
        top = sorted(flavor["hits"].items(), key=lambda x: -x[1])[:3]
        reasons.append(f"AI 腔模式命中 {flavor['total']} 次（{'、'.join(f'{k}×{v}' for k, v in top)}）")
    if forbidden:
        for dim, items in forbidden.items():
            for item in items:
                c = text.count(item)
                if c >= 2:
                    reasons.append(f"反向禁忌「{item}」命中 {c} 次")
    return (len(reasons) > 0, reasons)


# ── Phase 0「生成自检 · 原文对照」参数 ──────────────────────────────────
# 只影响完整档的重写环节；快档与未命中重写路径零新增调用。
_SELFCHECK_FINDING_LIMIT = 5      # 最多带几条历史告警的证据
_SELFCHECK_EVIDENCE_CHARS = 200   # 单条原文片段截断长度
_SELFCHECK_CORPUS_CALLS = 1       # 参考语料检索的**全局**调用上限
_SELFCHECK_CORPUS_K = 3           # 单次检索最多取几条片段


def _selfcheck_enabled(project) -> bool:
    """生成自检开关（Phase 0 原型）。

    **这里的判据是暂借 `project.narrative_tier`，语义上并不等价**：
    narrative_tier 的本义是「蒸馏档位」（full/balanced/fast），与「是否开启生成期
    自检」是两件事。之所以暂借，是为了 Phase 0 不新增字段、不做 schema 迁移：
    完整档才开自检，其余档位（含 narrative_tier 为 None）行为与现在逐字节一致。
    独立化为独立开关（新增列或项目设置项）留待后续——届时只需改本函数的判据，
    调用方与 prompt 形状都不用动。
    """
    return getattr(project, "narrative_tier", None) == "full"


def _truncate_snippet(s: str, limit: int) -> str:
    """截断原文片段：超长补省略号，防止撑爆 prompt。"""
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + "…"


def _evidence_query(text: str, reasons: list[str], forbidden: dict | None) -> str:
    """构造参考语料检索 query。

    思路：反向禁忌词是作者明令回避的表达，拿它去语料里检索，能找到「作者遇到
    同类场景时怎么写」；再补一段正文开头做场景锚点。取前若干个词拼成短 query。
    """
    parts: list[str] = []
    for _dim, items in (forbidden or {}).items():
        for it in items or []:
            if it and it in text and it not in parts:
                parts.append(it)
    head = (text or "").strip().split("\n")[0][:40].strip()
    if head:
        parts.append(head)
    return " ".join(parts[:6])[:200]


def _gather_rewrite_evidence(db, project, text: str, reasons: list[str],
                             forbidden: dict | None) -> tuple[list[dict], dict]:
    """收集重写的「原文对照」证据（Phase 0），返回 (evidence, stats)。

    A 路（零成本、纯本地读库）：读哨兵历史告警 `load_findings`——每条告警自带
      `evidence`（更早段的反证原文片段）。例如「死人说话」告警的 evidence 就是
      该角色死亡那段的原文。这是系统里现成的原料，不新增任何模型调用。

    B 路（限量检索）：对参考语料做 `hybrid_search`。注意该函数每次会**加载该
      novel 全部 chunks** 做 BM25 + 向量，大语料（实测 833 章）代价不小，所以
      必须限量：全局最多 1 次调用、最多 3 条片段。

    任一步失败 → 吞异常，返回**已收集到的部分**，绝不阻断重写（重写是补救，
    不是生成的前提）。检索失败在 stats.retrieval_tripped 里标记，供测试断言
    与线上观测。
    """
    stats = {
        "findings_used": 0,
        "retrieval_called": False,
        "retrieval_hits": 0,
        "retrieval_tripped": False,
    }
    evidence: list[dict] = []

    # ---- A 路：历史告警（零成本）----
    try:
        from app.kg.sentinel import load_findings

        recent = load_findings(db, project.id, source="generated")[:_SELFCHECK_FINDING_LIMIT]
    except Exception:  # noqa: BLE001 - 告警读不出也要能重写
        recent = []
    # severity 优先 err（Python 排序稳定 → 同 severity 内仍按段号倒序，即最近的先看）
    for f in sorted(recent, key=lambda d: 0 if (d.get("severity") or "") == "err" else 1):
        note = ""
        entity = (f.get("entity") or "").strip()
        message = (f.get("message") or "").strip()
        if entity or message:
            note = f"{entity}：{message}" if entity else message
        # 每条告警最多取 1 段片段：单条告警的 evidence 可能有多个 span，
        # 全塞进来会让一条告警吃掉整个证据预算。
        got = False
        for ev in (f.get("evidence") or [])[:1]:
            snippet = _truncate_snippet(str(ev.get("text") or ""), _SELFCHECK_EVIDENCE_CHARS)
            if not snippet:
                continue
            seg = ev.get("segment_no")
            src = f"前文第{seg}段" if seg is not None else "前文"
            # note 与片段一起给：只给片段模型不知道这段为什么重要
            body = f"{note}\n片段：{snippet}" if note else snippet
            evidence.append({"source": src, "kind": "finding", "text": body})
            got = True
        if not got and note:
            # 告警本身没有原文片段时，至少把「哪里不对」告诉模型
            evidence.append({"source": "前文", "kind": "finding", "text": note})
    stats["findings_used"] = sum(1 for e in evidence if e["kind"] == "finding")

    # ---- B 路：参考语料检索（限量，全局最多 _SELFCHECK_CORPUS_CALLS 次）----
    ids = [int(i) for i in (project.reference_novel_ids or []) if str(i).strip().isdigit()]
    if ids and _SELFCHECK_CORPUS_CALLS > 0:
        query = _evidence_query(text, reasons, forbidden)
        if query:
            tried = 0
            for nid in ids:
                if tried >= _SELFCHECK_CORPUS_CALLS:
                    break
                tried += 1
                stats["retrieval_called"] = True
                try:
                    from app.retrieval.search import hybrid_search

                    hits = hybrid_search(db, nid, query, k=_SELFCHECK_CORPUS_K)
                except Exception:  # noqa: BLE001 - 检索熔断：吞掉，重写继续
                    stats["retrieval_tripped"] = True
                    continue
                # 书名要给模型做来源标注，否则它分不清「前文事实」与「参考语料」
                title = f"语料{nid}"
                try:
                    from app.models import RawNovel

                    novel = db.get(RawNovel, nid)
                    if novel is not None and novel.title:
                        title = f"《{novel.title}》"
                except Exception:  # noqa: BLE001 - 书名取不到不影响片段本身
                    pass
                for h in hits or []:
                    snippet = _truncate_snippet(str(h.get("text") or ""), _SELFCHECK_EVIDENCE_CHARS)
                    if not snippet:
                        continue
                    ch = h.get("chapter_no")
                    src = f"参考语料{title}第{ch}章" if ch is not None else f"参考语料{title}"
                    evidence.append({"source": src, "kind": "corpus", "text": snippet})
            stats["retrieval_hits"] = sum(1 for e in evidence if e["kind"] == "corpus")

    return evidence, stats


def _rewrite_prompt(text: str, reasons: list[str], forbidden: dict | None,
                    evidence: list[dict] | None = None) -> tuple[str, str]:
    """构造针对性重写的 system/user prompt。

    evidence：`_gather_rewrite_evidence` 产出的「原文对照」证据。
    **为 None/空列表时 user 回退到旧形状**（问题清单 + 待改正文），
    保证快档与无证据场景逐字节不变。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是将一段 AI 生成的文本改写得更像真人写的网文。\n\n"
        "改写要求：\n"
        "1. 保持剧情、人物、对话内容不变，只改表达方式\n"
        "2. 去掉所有 AI 腔套路（机械转折、情绪直陈、段尾拔高、解释性对白）\n"
        "3. 用动作、姿态、语气、沉默来承载情绪，不要贴标签\n"
        "4. 对白要像真人说话，可有停顿、改口、答非所问\n"
        "5. 长短句交错，不要每段同样长度\n"
        "6. 允许留白，段落可以以一个动作或物件收住\n"
        "7. 具体优于形容，少堆形容词\n"
    )
    if forbidden:
        for dim, items in forbidden.items():
            if items:
                system += f"\n【{dim}】禁止使用：{'、'.join(items[:8])}"
    system += "\n\n只输出改写后的正文，不要输出任何解释或标记。"
    user = (
        f"以下文本存在以下 AI 腔问题：\n{'；'.join(reasons)}\n\n"
        f"请改写：\n\n{text}"
    )
    if evidence:
        # 【原文对照】块：每条证据都带**来源标注**（前文第 N 段 / 参考语料《X》第 M 章），
        # 让模型分清哪段是「你自己更早写下的事实」、哪段是「别人写的同类场景」。
        # 「不要照抄内容」必须写明：参考语料是别人的作品，照抄会直接污染正文
        # 并引入版权/一致性问题；前文片段照抄则会让本段变成重复叙述。
        blocks = [
            f"- 来源：{e.get('source') or '未知'}（{e.get('kind') or '未知'}）\n{e.get('text') or ''}"
            for e in evidence
        ]
        user = (
            f"以下文本存在以下 AI 腔问题：\n{'；'.join(reasons)}\n\n"
            "【原文对照】以下片段仅供你对照调性与前文事实：参考语料是同类场景的写法参考，"
            "前文片段是你自己更早写下的事实。**只学表达方式、只保证与前文不矛盾，"
            "不要照抄内容，也不要把参考语料的情节搬进本段。**\n"
            + "\n\n".join(blocks)
            + "\n\n"
            f"请改写：\n\n{text}"
        )
    return system, user


def _rewrite_with_evidence(db, project, provider, text: str, reasons: list[str],
                           forbidden: dict | None) -> tuple[str | None, dict | None]:
    """执行一次针对性重写，返回 (rewritten_text_or_None, rewrite_info)。

    抽出成公共函数是为了让 `generate_next_segment` 与 `generate_next_segment_stream`
    **两个入口的重写语义完全一致**——原先两处各写一遍，任何一边漏改都会造成
    「流式与非流式重写结果不同」这种极难排查的不一致。

    完整档（_selfcheck_enabled）才收集原文对照证据；其余档位 evidence 为空，
    prompt 回退旧形状，零新增调用。

    重写失败一律吞掉返回 (None, None) 记日志——重写是补救，不是生成的前提。
    """
    evidence: list[dict] = []
    selfcheck: dict = {}
    if _selfcheck_enabled(project):
        evidence, selfcheck = _gather_rewrite_evidence(db, project, text, reasons, forbidden)
    try:
        rw_system, rw_user = _rewrite_prompt(text, reasons, forbidden, evidence)
        rewritten = provider.chat(rw_system, rw_user)
        rewritten = _strip_thinking(rewritten)
        if rewritten and len(rewritten) > len(text) * 0.5:
            info = {
                "reasons": reasons,
                "original_chars": len(text),
                "rewritten_chars": len(rewritten),
            }
            # selfcheck 是纯新增键 —— 既有字段语义一个字都不改
            if selfcheck:
                info["selfcheck"] = selfcheck
            return rewritten, info
    except Exception as exc:  # noqa: BLE001 —— 重写失败不阻断，保留原文
        logging.getLogger(__name__).warning("AI 味重写失败，保留原文: %s", exc)
    return None, None


def _style_warning(text: str, profile: dict | None) -> dict | None:
    """对生成段落算一次风格距离，percentile 高于阈值时返回告警，否则 None。

    告警只提示不阻断——自动重写会触发额外 LLM 调用，且改出来的未必更好。
    无档案时静默跳过（档案是增强，不是生成的前提）。
    """
    if not profile or not text:
        return None
    try:
        from app.style import style_distance
        d = style_distance(text, profile)
    except Exception:  # noqa: BLE001 —— 距离计算失败绝不能阻断生成主流程
        return None
    pct = d.get("percentile")
    if pct is None or pct < STYLE_WARN_PERCENTILE:
        return None
    return {
        "percentile": pct,
        "mean_abs_z": d.get("mean_abs_z"),
        "worst": d.get("worst", []),
        "holdout_ref": d.get("holdout_ref"),
        "message": (
            f"本段文风偏离档案（percentile {pct}，高于阈值 {STYLE_WARN_PERCENTILE}）。"
            f"最偏的特征：{'、'.join(k for k, _ in (d.get('worst') or [])[:3]) or '未知'}。"
            "建议重写或手动调整。"
        ),
    }


def generate_next_segment(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
) -> dict:
    """续写下一段（长文单段）：组装上下文 → 调用 LLM → 段级总结 → 落库 → 更新主线摘要链。

    长文策略：默认一次生成 project.generation_length 字（前端可改），而非多段短循环。
    多段短循环会把前情/大纲反复塞进上下文，既压低缓存命中率又易触发会话压缩；
    改为「用户每点一次生成 = 一次长段」，续写环靠段级总结链（短摘要）衔接，不重复搬运正文。
    """
    from app.llm.provider import get_llm_provider

    # 档案预检：无缓存档案时明确失败，而不是同步跑分钟级蒸馏把请求挂死
    _require_distill_ready(db, project)

    if provider is None:
        provider = get_llm_provider()

    # 字数：前端/项目配置优先；未配则回退长文默认 3000，彻底告别 800/1200 的碎段
    if max_chars is None:
        max_chars = project.generation_length or 3000

    # 先把摘要树补齐再组装前情：老项目（步骤 2 之前建的）只有 L1、没有 L2/L3，
    # 直接 compose 会因粗层缺失而退化成全量（虽不留洞，但白吃预算）。
    # 这一步幂等——树已一致时零 LLM 调用，所以常态下没有额外开销。
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
    )
    user = build_user_prompt(
        ctx["prior_summaries"],
        ctx["rag_context"],
        project.outline,
        max_chars,
        project.plot_bible,
        project.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
    )

    text = provider.chat(system, user, constraints=ctx["constraints"])
    summary = provider.summarize(text)

    # AI 味检测 + 针对性重写
    narrative = _style_narrative(db, project)
    forbidden = (narrative or {}).get("forbidden") or {}
    rewrite_info = None
    need_rewrite, reasons = _should_rewrite(text, forbidden)
    if need_rewrite:
        # 重写（完整档会带原文对照证据）——流式与非流式共用同一实现，语义一致
        rewritten, info = _rewrite_with_evidence(db, project, provider, text, reasons, forbidden)
        if rewritten:
            text = rewritten
            summary = provider.summarize(text)
            rewrite_info = info

    gen, segment_no = _commit_segment(db, project, text, summary)

    # —— 图谱进入生成闭环 ——
    # 每段生成后增量合并事实到 generated 图（不清空、upsert），
    # 使下一次生成的 reminders 能拿到本段新出现的角色 / 新埋的伏笔。
    kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
    # 摘要在**段落库之后**才滚上：滚上失败不会牵连已写成的段
    tree_stats = _rollup_summary_tree(db, project, provider)

    # 文风告警：percentile 低于阈值时提示用户，不阻断、不自动重写
    style_warning = _style_warning(text, _style_profile(db, project))

    return {
        "kg_update": kg_stats,
        "summary_tree": tree_stats,
        "segment_no": segment_no,
        "chapter_range": gen.chapter_range,
        "text": text,
        "summary": summary,
        "system_prompt": system,
        "user_prompt": user,
        "constraints": ctx["constraints"],
        "rag_context": ctx["rag_context"],
        "style_warning": style_warning,
        "rewrite_info": rewrite_info,
    }


def generate_next_segment_stream(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
):
    """流式续写生成器：逐 delta 产出 {type:chunk} 事件，完成后落库并返回 {type:done}。

    与 generate_next_segment 语义一致（组装→续写→总结→落库→更新主线摘要链），
    区别在续写过程逐块 yield，供 SSE 转发——长文首字即显、不干等。
    """
    from app.llm.provider import get_llm_provider

    # 档案预检：与 generate_next_segment 同——无缓存档案时明确失败，
    # 不把请求挂死在同步蒸馏上（流式下作为 SSE error 事件下发）。
    _require_distill_ready(db, project)

    if provider is None:
        provider = get_llm_provider()
    if max_chars is None:
        max_chars = project.generation_length or 3000

    # 同 generate_next_segment：先补齐粗层，再拼有界前情（幂等，常态零开销）
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
    )
    user = build_user_prompt(
        ctx["prior_summaries"],
        ctx["rag_context"],
        project.outline,
        max_chars,
        project.plot_bible,
        project.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
    )

    pieces: list[str] = []
    for piece in provider.stream_chat(system, user, constraints=ctx["constraints"]):
        pieces.append(piece)
        yield {"type": "chunk", "text": piece}

    text = _strip_thinking("".join(pieces))
    summary = provider.summarize(text)

    # AI 味检测 + 针对性重写（补救措施，与 system prompt 的预防互补）
    narrative = _style_narrative(db, project)
    forbidden = (narrative or {}).get("forbidden") or {}
    rewrite_info = None
    need_rewrite, reasons = _should_rewrite(text, forbidden)
    if need_rewrite:
        # 与 generate_next_segment 共用同一实现：流式路径的重写语义必须完全一致
        rewritten, info = _rewrite_with_evidence(db, project, provider, text, reasons, forbidden)
        if rewritten:
            text = rewritten
            summary = provider.summarize(text)
            rewrite_info = info

    gen, segment_no = _commit_segment(db, project, text, summary)

    # —— 图谱进入生成闭环（与 generate_next_segment 顺序对齐）——
    # 必须放在 _commit_segment 之后、_rollup_summary_tree 之前：正文已落库才归档，
    # 且摘要滚上失败不该牵连簿记（滚上后置，失败只影响它自己）。
    # 复用 _merge_segment_into_graph（它已委托到 app.kg.build.merge_segment_into_kg
    # 做图谱+账本+别名归一+哨兵四步）——**不另写一套实现**：7416114 才把四步编排
    # 收归唯一入口，重写的结果就是又出现"只更一半"的路径。
    kg_stats = None
    try:
        kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
    except Exception as exc:  # noqa: BLE001 —— 簿记异常绝不打断 SSE：正文已落库，
        # done 事件必须照样发出，否则前端表现为"生成卡住且无结果"。
        logging.getLogger(__name__).warning("流式 KG 簿记失败，段落已落库: %s", exc)
        kg_stats = {"merged": False, "reason": f"KG 簿记失败：{str(exc)[:100]}"}

    tree_stats = _rollup_summary_tree(db, project, provider)

    # 文风告警：percentile 低于阈值时提示用户，不阻断、不自动重写
    style_warning = _style_warning(text, _style_profile(db, project))

    yield {
        "type": "done",
        "segment_no": segment_no,
        "chars": len(text),
        "summary": summary,
        "summary_tree": tree_stats,
        "id": gen.id,
        "style_warning": style_warning,
        "rewrite_info": rewrite_info,
        # 纯新增键：done 的既有字段语义一个都不改。挂簿记统计是为了让
        # 流式路径也能被观测（此前流式完全不做归档，出了问题无从判断）。
        "kg": kg_stats,
    }


def _build_generation_prompts(
    db,
    project: Project,
    before_segment: int | None = None,
    novel_ids: list[int] | None = None,
    max_chars: int | None = None,
    provider=None,
):
    """组装提示词；`before_segment` 给定时的前情**只取该段之前**（重生成用）。

    实现方式不是「把整条链换掉」，而是从当前摘要树里截出等价的一棵
    （`summary_tree.tree_before`），再交给同一套 compose_prior——
    这样重生成与正常续写的**前情口径完全一致**（同样的分层、同样的预算），
    避免出现「正常续写有主线骨架、重生成没有」这种隐性不一致。
    """
    if max_chars is None:
        max_chars = project.generation_length or 3000

    tree = _summary_tree(project)
    if before_segment is not None:
        tree = tree_before(tree, before_segment)
        if provider is not None:
            # 截断可能让 L3 失效或产生未封卷的洞 → 就地补齐，重建成本换来口径一致
            rollup_volumes(tree, _rollup_fn(provider))
    blocks, _ = compose_prior(
        tree, budget_chars=budget_for_generation(project.generation_length)
    )
    system = build_system_prompt(
        project.title, get_reminders(db, project.id, source="generated"),
        _style_info(project), project.plot_bible, _style_profile(db, project),
    )
    user = build_user_prompt(
        l1_chain(tree),
        [],  # 重生成不带 RAG 素材：避免把「参考语料」的语感混进重写，保持与原段同源
        project.outline,
        max_chars,
        project.plot_bible,
        project.premise,
        prior_blocks=blocks,
    )
    return system, user, max_chars


def regenerate_segment_stream(
    db,
    project: Project,
    segment_no: int,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
):
    """重生成单段（流式）：删除目标段旧内容、依前文摘要链重生成插回、重建摘要链。

    语义：只替换指定 segment_no 的 text/summary，不改动其他段的 text；
    后续段的「上下文」严格说应基于新摘要，但为不引发级联重写，仅重建摘要树
    （L1 重排 + L2/L3 整体重算），保证「未来」续写连贯。已落库的后段正文保留。
    """
    from app.llm.provider import get_llm_provider

    # 档案预检：重生成同样不该把请求挂死在同步蒸馏上
    _require_distill_ready(db, project)

    if provider is None:
        provider = get_llm_provider()

    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no)
    ).all()
    target = next((g for g in gens if g.segment_no == segment_no), None)
    if target is None:
        raise ValueError(f"段 {segment_no} 不存在，无法重新生成")

    # 前情 = 只取该段之前的树（截断时若 L3 混入了被替换内容，会自动丢弃并重建）
    system, user, max_chars = _build_generation_prompts(
        db, project, before_segment=segment_no, novel_ids=novel_ids,
        max_chars=max_chars, provider=provider,
    )
    # constraints 与正常续写同源（角色/伏笔的锁定名单不能因重生成而变），只算一次
    constraints = build_context(db, project, novel_ids=novel_ids)["constraints"]

    pieces: list[str] = []
    for piece in provider.stream_chat(system, user, constraints=constraints):
        pieces.append(piece)
        yield {"type": "chunk", "text": piece}

    text = _strip_thinking("".join(pieces))
    summary = provider.summarize(text)

    target.text = text
    target.summary = {"text": summary}
    target.chapter_range = f"段{segment_no}"

    # —— 重生成的 KG 簿记（清空 + 全量 replay）——
    # ⚠️ 顺序陷阱：rebuild_generated_kg 从 Generation 表读各段正文，新文本必须先
    # 落库，否则 replay 读到的是**旧**文本 —— 等于没修。故此 commit 必须在 replay 前。
    db.commit()
    kg_stats = None
    try:
        from app.kg.build import rebuild_generated_kg

        kg_stats = rebuild_generated_kg(db, project.id)
    except Exception as exc:  # noqa: BLE001 —— 簿记失败绝不打断 SSE：正文已落库，
        # done 事件必须照样发出，否则前端表现为"重生成卡住且无结果"。
        logging.getLogger(__name__).warning("重生成 KG replay 失败，正文已落库: %s", exc)
        kg_stats = {"rebuilt": False, "reason": f"{str(exc)[:100]}"}

    # 重建整棵摘要树：该段所在的卷摘要与主线骨架都基于旧内容，不能续滚
    tree_stats = rebuild_summary_tree(db, project, provider)
    db.refresh(target)

    yield {
        "type": "done",
        "segment_no": segment_no,
        "chars": len(text),
        "summary": summary,
        "summary_tree": tree_stats,
        "id": target.id,
        # 与流式续写路径（generate_next_segment_stream）的 done 键对齐，
        # 让重生成也带上簿记统计，可被前端/API 观测。
        "kg": kg_stats,
    }


def generate_loop(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    segments: int = 1,
    max_chars: int | None = None,
) -> list[dict]:
    """连续续写（默认单段）。需批量时才传 segments>1；逐段落库，后段读前段总结作前情。

    注意：segments 过大会在单次请求里反复调用 LLM，前情摘要链虽短，但仍有会话压缩风险；
    生产建议「用户逐次点生成」而非一次性 loop 多段。
    """
    results: list[dict] = []
    for _ in range(segments):
        results.append(generate_next_segment(db, project, novel_ids, provider, max_chars))
    project.status = "loop_done" if project.status == "generating" else project.status
    db.commit()
    return results
