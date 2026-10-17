"""M4 生成引擎：上下文组装 → 续写 → 段级总结 → 入库（支持段级续写环）。

漂移防护机制：M3.3 的未填坑 + 角色约束进入 system 提示并全程锁定；
前情注入改走**层级摘要树**（L1 段 / L2 卷 / L3 主线骨架，见 summary_tree.py），
把无界的段级摘要链压成有界前情块——实测全量拼接时 N=300 会膨胀到单段正文的 656%。
"""

import copy

from sqlalchemy import select

from app.generation.prompts import (
    build_system_prompt,
    build_user_prompt,
    merged_constraint_items,
)
from app.generation.timeline import (
    TimelineState,
    advance,
    build_node_prompt,
    force_complete_current,
    judge_node_completion,
    plan_nodes_with_llm,
    plan_timeline,
    state_from_dict,
    state_to_dict,
    timeline_progress,
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

    v2.6：节点粒度根治——如果 stop token 漏过（OpenAI 兼容 stop 通常不会
    返回 stop 字符串本身，但保险起见），剥离正文末尾的【本节完】marker 行。
    """
    if " <think>" in text or " </think>" in text:
        end = text.rfind(" </think>")
        if end != -1:
            text = text[end + len(" </think>"):].strip()
        # 只有开标签没有闭标签：从开标签后全部丢弃不可靠，退化为原样返回
    # v2.6：剥离【本节完】marker 行（如果存在）。通常 stop token 已停在此之前，
    # 但模型偶尔可能已输出 marker 才被 stop——保险剥离避免污染落库。
    # 模式：行首可有 0+ 空白（包含换行/空格/制表），然后是 marker，
    # marker 后到行尾可能有空白——$ 用 \Z 而不是 $（不受多行模式影响）
    import re
    text = re.sub(r"(?:[^\S\n]*\n)*[^\S\n]*【本节完】[ \t]*\Z", "", text).rstrip()
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

    两步共用**同一次抽取**：
    1. 图谱（KgNode/KgEdge）——下一次生成的 reminders 能拿到本段新角色/新伏笔；
    2. 事实账本（Fact）+ 别名归一 —— 把「宁折 / 宁折枝」这类同一人的不同写法
       并到规范名，否则 get_reminders 会把同一人当成两个锁定角色注入提示词。

    任何一步失败都绝不能阻断生成主流程，因此这里吞掉异常只回传状态。

    返回形状刻意**保持扁平兼容**：图谱统计（merged/new_characters/…）仍在顶层，
    账本与别名归一挂在 `ledger` / `aliases` 两个新键下。嵌套化会破坏
    `kg_update` 的既有消费方（前端与 API 都在读顶层 new_characters）。
    """
    try:
        from app.kg.build import extract_from_text, merge_result_into_graph

        result, _ = extract_from_text(text, segment_no)
        stats = dict(
            merge_result_into_graph(
                db, project_id, result, source="generated", segment_no=segment_no
            )
        )
    except Exception as exc:  # noqa: BLE001
        return {"merged": False, "reason": f"图谱合并失败：{str(exc)[:100]}"}

    try:
        from app.kg.ledger import facts_from_extraction, resolve_aliases, upsert_facts

        stats["ledger"] = upsert_facts(
            db, project_id, facts_from_extraction(result, segment_no),
            source="generated",
        )
        # 别名归一紧随其后：本段若引入了简称/全称两种写法，下一段就要用规范名
        aliases = resolve_aliases(db, project_id, source="generated")
        stats["aliases"] = {
            "auto_merged": len(aliases["auto"]),
            "nodes_removed": aliases["nodes_removed"],
            "review_count": aliases["review_count"],
            "review": aliases["review"],
        }
    except Exception as exc:  # noqa: BLE001
        stats["ledger"] = {"error": str(exc)[:100]}

    # 规则哨兵：零 LLM 的硬冲突检查。**只标红，不改正文。**
    # 放在账本之后：名字表与已认定状态都来自账本，账本先更新，哨兵这一段的判定才准。
    # 失败一样不阻断生成 —— 哨兵是增强，不是生成的前提。
    try:
        from app.kg.sentinel import audit_segment

        proj = db.get(Project, project_id)
        stats["sentinel"] = audit_segment(db, proj, text, segment_no)
    except Exception as exc:  # noqa: BLE001
        stats["sentinel"] = {"error": str(exc)[:100], "findings": []}

    # v3.0 独立 Judge Agent：只读 v3.0 KG 表，覆盖哨兵没做的瞬移/预知/秘密外泄/
    # 时间穿越/伏笔超期/语言一致性。与哨兵并列（都只标红不改正文），失败同样不阻断。
    try:
        from app.kg.judge import judge_segment, save_judge_findings

        judge_res = judge_segment(db, project_id, text, segment_no)
        # findings 必须落库（与哨兵同纪律）：只放单次响应里的告警，用户没盯着
        # 就永远丢了，事后无法复核。落库失败不阻断生成。
        try:
            save_judge_findings(db, project_id, judge_res.get("findings", []),
                                segment_no=segment_no)
        except Exception:  # noqa: BLE001
            pass
        stats["judge"] = judge_res
    except Exception as exc:  # noqa: BLE001
        stats["judge"] = {"error": str(exc)[:100], "findings": []}

    return stats


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
        # v3.0 KG 切片：按当前剧情点动态取的近期实体/状态/伏笔/情报/事件。
        # 空库（未抽过）时为空串，build_user_prompt 跳过该块。
        "kg_slice": _kg_slice_text(db, project),
    }


def _kg_slice_text(db, project: Project) -> str:
    """取 v3.0 KG 切片并渲染成 prompt 文本。失败返回空串（不阻断）。"""
    try:
        from app.kg.retrieval_v3 import slice_for_prompt, format_slice_for_prompt

        current_seg = _next_segment_no(db, project.id) - 1
        if current_seg < 1:
            return ""
        # active_characters：本段已写正文里的角色。首段无正文，用 plot_bible 角色名兜底。
        active = _active_character_names(db, project, current_seg)
        sl = slice_for_prompt(db, project.id, current_seg, active_characters=active)
        return format_slice_for_prompt(sl)
    except Exception:  # noqa: BLE001 —— KG 切片是增强，取不到不阻断生成
        return ""


def _enqueue_v3_extraction(db, project: Project, segment_no: int, text: str) -> dict:
    """段 commit 后入队 v3.0 异步批抽（extractor_v3 落 v3.0 表）。

    与 v2.6 同步合并（_merge_segment_into_graph）并存：同步合并喂 reminders（本段
    立即可见），v3.0 批抽落 9 张表（供 Judge/检索切片/读侧 API）。
    零 LLM、零阻塞：enqueue 只进内存队列，worker 线程后台抽。失败静默兜底。
    """
    try:
        from app.kg import batch_worker as bw

        bw.ensure_worker()
        chapter = _next_segment_no(db, project.id) - 1  # 本段 commit 后 = segment_no
        bw.enqueue_segment(db, project.id, segment_no, text, chapter=chapter)
        return {"enqueued": True, "pending": bw._queue.pending_count(project.id)}
    except Exception as exc:  # noqa: BLE001 —— 批抽是增强，失败不阻断
        return {"enqueued": False, "error": str(exc)[:100]}


def _active_character_names(db, project: Project, current_seg: int) -> list[str]:
    """本段涉及的角色名（mention 反查）。空则用 plot_bible 角色名兜底。"""
    try:
        from app.models import KgMention, KgEntity
        from sqlalchemy import select

        ent_ids = {
            m.entity_id for m in db.scalars(
                select(KgMention).where(
                    KgMention.project_id == project.id,
                    KgMention.segment_no == current_seg,
                )
            )
        }
        names = []
        for eid in ent_ids:
            ent = db.get(KgEntity, eid)
            if ent is not None and ent.kind == "character":
                names.append(ent.canonical_name)
        if names:
            return names
        # 兜底：plot_bible 里的角色名
        bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
        return [c.get("name") for c in (bible.get("characters") or [])
                if isinstance(c, dict) and c.get("name")]
    except Exception:  # noqa: BLE001
        return []


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
    """
    from app.style.narrative import project_narrative_safe

    return project_narrative_safe(db, project)


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
#: 每 N 段自动异步触发 audit_kg（防主链 LLM 拖慢，fire-and-forget 模式）
AUTO_AUDIT_INTERVAL = 10

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


def _rewrite_prompt(text: str, reasons: list[str], forbidden: dict | None) -> tuple[str, str]:
    """构造针对性重写的 system/user prompt。"""
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
    return system, user


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
        # 方案 B「对数粒度 profile」：把 bucket / scale_ratio / chars 一起给前端
        # Style 审计 subtab 可显式展示「本段走 long bucket / scale_ratio 2.0x」
        "bucket": d.get("bucket"),
        "scale_ratio": d.get("scale_ratio"),
        "chars": d.get("chars"),
    }




def _schedule_async_audit_kg(db, project_id: int) -> None:
    """每 N 段异步触发 audit_kg agent（fire-and-forget，不阻塞 generate）。

    audit_kg 是 LLM 密集工具，**不能**在主链同步等结果。
    用 daemon thread 启动，结果落 AgentRun 表 + 助手页可见。
    """
    import logging
    import threading
    from app.db import SessionLocal
    from app.agents.registry import _impl_audit_kg  # 内部入口，避免走 LLM 路由

    def _run():
        try:
            s = SessionLocal()
            try:
                _impl_audit_kg(db=s, project_id=project_id)
            finally:
                s.close()
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "auto audit_kg 失败 (project %s)", project_id, exc_info=True)

    t = threading.Thread(target=_run, daemon=True, name=f"audit-kg-{project_id}")
    t.start()

def _timeline_state_for(
    project: Project, written_segments: int, provider=None, db=None
) -> TimelineState | None:
    """为项目取/建时间线状态（§11.3：DB 落库版）。

    优先级：
    1. 存档（project.plot_timeline）有效 → 反序列化恢复（不再调 LLM，省一次 plan）
       ——若 written_segments > 存档中已写段数：快进补齐到当前；
       若 < ：回退到重放（保守，advance 单向不可逆，宁可重算也不丢推进）
    2. 无存档 → plan + §7.3 LLM 填实节点；填实后立即落库（若有 db）
    3. 全部失败/无 outline → 返回 None 走原逻辑

    `db` 不传时只读不写（用于 generate-plan 预检）。
    """
    if not project.outline:
        return None
    total_chars = project.target_length or 5000
    per_seg = project.generation_length or 3000
    total_segments = max(total_chars // max(per_seg, 1), 1)

    # 路径 1：尝试从存档恢复
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    st = state_from_dict(saved, total_segments=total_segments) if saved else None

    if st is None:
        # 路径 2：首次规划
        # v2.4 高潮密度蒸馏：plot_rhythm.avg_node_segments 取代首轮默认 5。
        # plot_rhythm 由 compute_plot_rhythm 算，存到 project 列；调用方（生成
        # 主流程）负责在首次 generate 前确保已蒸馏。
        # 合法范围：v >= 1（avg 太小会触发 plan_timeline 的 div-by-zero 保护；
        # 太大 plan_timeline 会 clamp 主干节点数到 [3,12]，所以这里只做下限
        # 校验——>12 的值仍可用，只是主干节点被 clamp 到 12）。
        avg_node_segments = 5  # DEFAULT_AVG_NODE_SEGMENTS
        if isinstance(project.plot_rhythm, dict):
            v = project.plot_rhythm.get("avg_node_segments")
            if isinstance(v, int) and v >= 1:
                avg_node_segments = v
        st = plan_timeline(total_segments=total_segments, avg_node_segments=avg_node_segments)
        if provider is not None:
            outline_list = project.outline
            if isinstance(outline_list, str):
                import json as _json
                try:
                    outline_list = _json.loads(outline_list)
                except Exception:  # noqa: BLE001
                    outline_list = [outline_list]
            filled = plan_nodes_with_llm(provider, outline_list, project.plot_bible, st.total_main)
            if filled:
                for node in filled:
                    if node.id in st.nodes:
                        node.segment_budget = st.nodes[node.id].segment_budget
                        st.nodes[node.id] = node
    else:
        # 存档存在：若 written_segments 比存档进度多（应该不会发生但兜底），
        # 不额外 advance——存档即权威。少的话走 advance 补齐。
        archived_written = sum(st.seg_counts.values())
        if written_segments > archived_written:
            for _ in range(written_segments - archived_written):
                st = advance(st)

    # 落库（db 给定 + state 有效 + 还没存过/或被改过）
    if db is not None and st is not None:
        _persist_timeline_state(db, project, st)

    return st


def get_section_stop_sequences() -> list[str]:
    """v2.6：节点粒度根治——返回节末 stop token 列表。

    stop 列表：
    - 【本节完】 — model 在节末单独一行写这个 marker，后端停在 marker 处
    - 一些副 marker 防止 model 试图"写下一节"或"写小标题"时跑远

    调用方在 chat(stream_stop_sequences=...) / stream_chat(stop_sequences=...) 传这个 list。
    用 per-request 参数而不是 provider.set_stop_sequences() —— 避免单例被跨请求污染。
    """
    return [
        "【本节完】",
        "## 第",
        "\n# ",
        "\n## ",
    ]


def _persist_timeline_state(db, project: Project, st: TimelineState) -> None:
    """把 state 序列化写回 project.plot_timeline。失败也不抛（generate 主流程不挂）。"""
    try:
        project.plot_timeline = state_to_dict(st)
        db.add(project)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _load_timeline_state_for_branch(db, project: Project, written_segments: int):
    """v2.5 helper：给 roll_branch 读存档 state——不调 LLM、不触发 advance。

    与 _timeline_state_for 的差别：
    - 不传 provider（不调 plan_nodes_with_llm）
    - 不落库（只读，不写）
    - 不调 advance 补齐（roll_branch 自己处理节流逻辑）
    - 不做 LLM 节点规划（仅恢复存档）

    失败 → 返回 None（roll_branch 走 no_state 路径，不切支线）
    """
    if not project.outline:
        return None
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not saved:
        return None
    total_chars = project.target_length or 5000
    per_seg = project.generation_length or 3000
    total_segments = max(total_chars // max(per_seg, 1), 1)
    # 不传 provider 给 state_from_dict（用最新 total_segments 复算骨架）
    return state_from_dict(saved, total_segments=total_segments)


def _judge_and_advance(
    db, project: Project, provider, recent_text: str, written_segments: int
) -> dict:
    """v2.1：commit 段后调一次 LLM 判定当前节点 exit_condition 是否达成。

    - 判定 True：取最新 state → force_complete_current → 落库
    - 判定 False：只跑正常 advance（段数 +1，可能满 budget 自然 done）→ 落库
    - 判定 None（失败/不确定/没 exit）：只跑正常 advance → 落库
    - 没 outline / state 不可用：返回 skipped=True，不动 state

    返回 dict 供 API 透传给前端（诊断/进度展示用）。失败兜底完全静默——
    段数预算是兜底，LLM 加速只是「锦上添花」。
    """
    out = {"called": False, "judge": None, "forced": False, "advanced": False, "skipped": True}
    if not project.outline:
        return out
    st = _timeline_state_for(project, written_segments, provider=None, db=None)
    if st is None or not st.current_node or st.current_node not in st.nodes:
        return out
    cur_id = st.current_node
    cur_node = st.nodes[cur_id]

    # 1) 跑正常 advance（不管 LLM 怎么判，段数 +1）
    archived_written = sum(st.seg_counts.values())
    # _timeline_state_for 已做 archived 兜底：但传入 db=None 没重算
    # 这里取出的 st 可能比 written_segments 落后一格（commit 前算的）
    # 保险：手工 advance 一次（即使 archived ≥ written 也是幂等无害，next/beat 不变）
    st = advance(st)
    out["advanced"] = True
    out["called"] = True
    out["skipped"] = False

    # 2) 调 LLM 判定——但只在「advance 之后该节点还未 done」时调
    # （如果 advance 已让它 done，就没必要再让 LLM 重复判定）
    if cur_id not in st.done and cur_node.exit_condition:
        out["judge"] = judge_node_completion(provider, cur_node, recent_text)
        if out["judge"] is True:
            st = force_complete_current(st)
            out["forced"] = True

    # 3) 落库（用新 state 覆盖）
    _persist_timeline_state(db, project, st)
    out["current_node"] = st.current_node
    out["done"] = sorted(st.done)
    return out


def current_node_prompt(
    project: Project, written_segments: int, provider=None, db=None
) -> str | None:
    """算出当前剧情点的提示块（时间线启用时）；无 outline 返回 None。

    供 generate_next_segment 与 generate-plan 共用——两条路径必须下发同一份提示，
    否则预检看到的不算数。written_segments = 已写段数（_next_segment_no - 1）。
    `db` 用于持久化；预检/读侧可不传（仅恢复，不写）。
    """
    st = _timeline_state_for(project, written_segments, provider, db=db)
    if st is None or not st.current_node or st.current_node not in st.nodes:
        return None
    return build_node_prompt(
        st.nodes[st.current_node], st.current_beat, timeline_progress(st)
    )


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
    # —— 时间线：算出当前剧情点提示（有 outline 才启用；无则 None 走原逻辑）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
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
        node_prompt=node_prompt,
        kg_slice=ctx.get("kg_slice") or None,
    )

    # v2.6：节点粒度根治——per-request 传 stop token（不用单例 setter，避免跨请求污染）
    # 重写/重生成是 AI 味修复，不传 stop
    text = provider.chat(system, user, constraints=ctx["constraints"],
                         stop_sequences=get_section_stop_sequences())
    summary = provider.summarize(text)

    # AI 味检测 + 针对性重写
    narrative = _style_narrative(db, project)
    forbidden = (narrative or {}).get("forbidden") or {}
    rewrite_info = None
    need_rewrite, reasons = _should_rewrite(text, forbidden)
    if need_rewrite:
        try:
            rw_system, rw_user = _rewrite_prompt(text, reasons, forbidden)
            rewritten = provider.chat(rw_system, rw_user)
            rewritten = _strip_thinking(rewritten)
            if rewritten and len(rewritten) > len(text) * 0.5:
                rewrite_info = {"reasons": reasons, "original_chars": len(text), "rewritten_chars": len(rewritten)}
                text = rewritten
                summary = provider.summarize(text)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("AI 味重写失败，保留原文: %s", exc)

    gen, segment_no = _commit_segment(db, project, text, summary)

    # —— v2.1：exit_condition LLM 判定 → 必要时提前切节点（§13 待定项落地）——
    # 此时本段刚 commit 完，state 还没 advance（advance 是下次预检时触发的）。
    # 在「commit 之前」的 current_node + 刚生成的正文上判定。
    # 失败/不确定/节点没 exit → 走段数预算兜底，逻辑照旧。
    node_judge = _judge_and_advance(db, project, provider, text, written)

    # —— 图谱进入生成闭环 ——
    # 每段生成后增量合并事实到 generated 图（不清空、upsert），
    # 使下一次生成的 reminders 能拿到本段新出现的角色 / 新埋的伏笔。
    kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)

    # v3.0 异步批抽：入队 extractor_v3 落 9 张表（Judge/检索切片/读侧 API 的数据源）。
    # 零 LLM 零阻塞，失败静默兜底。状态透传给前端（v3_extraction 键）。
    v3_extraction = _enqueue_v3_extraction(db, project, segment_no, text)

    # —— v2.5：支线滚动触发 ——
    # 在 KG 合并后跑——这样能查到本段新产生的角色/钩子。
    # roll_branch 读存档 state、判断 signal、切支线、落库——失败静默兜底。
    # last_branch_written: 上次切支线时的已写段数——state 里没有存这个，
    # 简化方案：从当前存档 plot_timeline 里读"分支段已写"（粗略：state.current_node
    # 是否为分支 kind），如果当前是分支段已写过 MIN_GAP_SEGMENTS 才允许再切。
    # 第一版简化：last_branch_written=None 总是允许切（最宽松）。
    # 后续可优化：把 last_branch_written 存到 plot_timeline 顶层。
    # 注：roll_branch 返回 (new_state, info_dict)——新 state 用于 state 推进，
    # info_dict 透传给前端。这里 _new_state 不需要（roll_branch 内部已落库），
    # 取 info_dict 当作 branch_roll。
    from app.generation.branch import roll_branch as _roll_branch
    _post_commit_written = _next_segment_no(db, project.id) - 1  # = written + 1
    _new_state, branch_info = _roll_branch(
        db, project, _load_timeline_state_for_branch(db, project, written),
        provider, _post_commit_written, last_branch_written=None,
    )

    # 摘要在**段落库之后**才滚上：滚上失败不会牵连已写成的段
    tree_stats = _rollup_summary_tree(db, project, provider)

    # 文风告警：percentile 低于阈值时提示用户，不阻断、不自动重写
    style_warning = _style_warning(text, _style_profile(db, project))
    # v2.1：style_warning 落库（段级历史，>500 截断）
    if style_warning:
        try:
            from app.style import append_style_warning
            append_style_warning(db, project, segment_no, style_warning)
        except Exception:  # noqa: BLE001 —— 落库失败绝不能阻断生成主流程
            import logging
            logging.getLogger(__name__).warning(
                "style_warning 落库失败 (seg %s): %s", segment_no, style_warning, exc_info=True)
    # v2.1：每 AUTO_AUDIT_INTERVAL 段异步触发 audit_kg（不阻塞 generate）
    if segment_no and segment_no % AUTO_AUDIT_INTERVAL == 0:
        _schedule_async_audit_kg(db, project_id)

    # v2.6 诊断性 B：检测白名单外"疑似角色"——只告警，不替换（旁路观测）
    # 启发式会有假阳性（普通词组也报），调用方拿到列表后可聚合展示
    from app.generation.guard import detect_unlisted_characters, collect_allowed_names
    _allowed = collect_allowed_names(
        ctx_reminders=ctx.get("reminders"),
        plot_bible=project.plot_bible,
    )
    unlisted = detect_unlisted_characters(text, _allowed)
    if unlisted:
        import logging
        logging.getLogger(__name__).warning(
            "v2.6 诊断性 B：本段检测到 %d 个白名单外疑似角色 %s",
            len(unlisted), unlisted[:10],  # 最多打 10 个，避免日志爆
        )

    return {
        "kg_update": kg_stats,
        "v3_extraction": v3_extraction,
        "summary_tree": tree_stats,
        "node_judge": node_judge,
        "branch_roll": branch_info,
        "segment_no": segment_no,
        "chapter_range": gen.chapter_range,
        "text": text,
        "summary": summary,
        "system_prompt": system,
        "user_prompt": user,
        "constraints": ctx["constraints"],
        "rag_context": ctx["rag_context"],
        "style_warning": style_warning,
        "unlisted_characters": unlisted,
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

    v2.3：传 `node_prompt` 给 build_user_prompt——流式版也走时间线模式，
    剔除 plot_bible 全字段 + premise。commit 段后调 `_judge_and_advance`
    （与同步版同口径）—— yield 一个 `node_judge` 事件。
    """
    from app.llm.provider import get_llm_provider

    if provider is None:
        provider = get_llm_provider()
    if max_chars is None:
        max_chars = project.generation_length or 3000

    # 同 generate_next_segment：先补齐粗层，再拼有界前情（幂等，常态零开销）
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids)
    # —— 时间线：算出当前剧情点提示（与同步版同口径，db 用于落库）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
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
        node_prompt=node_prompt,
        kg_slice=ctx.get("kg_slice") or None,
    )

    # v2.6：节点粒度根治——流式版同步传 per-request stop token
    pieces: list[str] = []
    for piece in provider.stream_chat(system, user, constraints=ctx["constraints"],
                                       stop_sequences=get_section_stop_sequences()):
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
        try:
            rw_system, rw_user = _rewrite_prompt(text, reasons, forbidden)
            rewritten = provider.chat(rw_system, rw_user)
            rewritten = _strip_thinking(rewritten)
            if rewritten and len(rewritten) > len(text) * 0.5:
                rewrite_info = {"reasons": reasons, "original_chars": len(text), "rewritten_chars": len(rewritten)}
                text = rewritten
                summary = provider.summarize(text)
        except Exception as exc:  # noqa: BLE001 —— 重写失败不阻断，保留原文
            logging.getLogger(__name__).warning("AI 味重写失败，保留原文: %s", exc)

    gen, segment_no = _commit_segment(db, project, text, summary)
    # v2.3：v2.1 集成到流式版——commit 段后调 judge_and_advance（与同步版同口径）
    node_judge = _judge_and_advance(db, project, provider, text, written)

    # v2.5 阶段 2：流式版补 v2.5 集成——与同步版同口径
    # 顺序：commit → judge → KG 合并 → roll_branch（KG 必须在 roll_branch 前）
    kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
    # v3.0 异步批抽：与同步版同口径入队
    v3_extraction = _enqueue_v3_extraction(db, project, segment_no, text)
    _post_commit_written = _next_segment_no(db, project.id) - 1  # = written + 1
    from app.generation.branch import roll_branch as _roll_branch
    _new_state, branch_info = _roll_branch(
        db, project, _load_timeline_state_for_branch(db, project, written),
        provider, _post_commit_written, last_branch_written=None,
    )

    tree_stats = _rollup_summary_tree(db, project, provider)

    # 文风告警：percentile 低于阈值时提示用户，不阻断、不自动重写
    style_warning = _style_warning(text, _style_profile(db, project))

    # v2.6 诊断性 B：检测白名单外"疑似角色"——只告警，不替换
    from app.generation.guard import detect_unlisted_characters, collect_allowed_names
    _allowed = collect_allowed_names(
        ctx_reminders=ctx.get("reminders"),
        plot_bible=project.plot_bible,
    )
    unlisted = detect_unlisted_characters(text, _allowed)
    if unlisted:
        import logging
        logging.getLogger(__name__).warning(
            "v2.6 诊断性 B：本段检测到 %d 个白名单外疑似角色 %s",
            len(unlisted), unlisted[:10],
        )

    yield {
        "type": "done",
        "segment_no": segment_no,
        "chars": len(text),
        "summary": summary,
        "summary_tree": tree_stats,
        "id": gen.id,
        "style_warning": style_warning,
        "unlisted_characters": unlisted,
        "rewrite_info": rewrite_info,
        "node_judge": node_judge,
        "kg_update": kg_stats,
        "v3_extraction": v3_extraction,
        "branch_roll": branch_info,
    }


def _build_generation_prompts(
    db,
    project: Project,
    before_segment: int | None = None,
    novel_ids: list[int] | None = None,
    max_chars: int | None = None,
    provider=None,
    node_prompt: str | None = None,
):
    """组装提示词；`before_segment` 给定时的前情**只取该段之前**（重生成用）。

    实现方式不是「把整条链换掉」，而是从当前摘要树里截出等价的一棵
    （`summary_tree.tree_before`），再交给同一套 compose_prior——
    这样重生成与正常续写的**前情口径完全一致**（同样的分层、同样的预算），
    避免出现「正常续写有主线骨架、重生成没有」这种隐性不一致。

    v2.3：新增 `node_prompt` 参数（v2.2 节点粒度强化后，stream 版走
    原逻辑模式会注入整条 outline + plot_bible + premise，模型拿到就
    演完整条故事——这正是"第一章就完结"bug 的变体）。传 node_prompt
    后 build_user_prompt 走时间线模式，剔除全故事信息。
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
        node_prompt=node_prompt,
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

    v2.3：传 `node_prompt` 给 `_build_generation_prompts`——重生成用时间线模式
    剔除全故事信息。**不**调 `_judge_and_advance`——重生成替换第 N 段语义
    上不改 `_next_segment_no`（已存在），不应触发 state advance。
    """
    from app.llm.provider import get_llm_provider

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

    # v2.3：算当前剧情点提示（重生成第 N 段时该段已存在，written_segments = N-1，
    # 与 generate_next_segment 同步版同口径）。
    written = segment_no - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)

    # 前情 = 只取该段之前的树（截断时若 L3 混入了被替换内容，会自动丢弃并重建）
    system, user, max_chars = _build_generation_prompts(
        db, project, before_segment=segment_no, novel_ids=novel_ids,
        max_chars=max_chars, provider=provider, node_prompt=node_prompt,
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
