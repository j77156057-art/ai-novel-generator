"""M4 生成引擎：上下文组装 → 续写 → 段级总结 → 入库（支持段级续写环）。

漂移防护机制：M3.3 的未填坑 + 角色约束进入 system 提示并全程锁定；
前情注入改走**层级摘要树**（L1 段 / L2 卷 / L3 主线骨架，见 summary_tree.py），
把无界的段级摘要链压成有界前情块——实测全量拼接时 N=300 会膨胀到单段正文的 656%。
"""

import copy
import logging

from sqlalchemy import select

from app.agents.prompt_overrides import PromptKey, resolve_prompt_block
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


# ─── Phase 0.5：定向检索查询词（2026-10-17）────────────────────────────────
#
# 缺陷：`_rag_query` 拿 `prior[-1]` 当检索词——那是**刚写完那段**的摘要，描述的不是
# 「接下来要写什么」；首段则退化成「书名 + 大纲首项」（大纲是整篇的，不按段对齐）。
# 修法：查询词 = 「目标侧（未填伏笔 + 角色名，build_context 已加载，零额外 IO）」
#       + 「衔接侧（prior[-1] 尾部窗口）」；两者皆空则**逐字节**回到旧行为。
#
# 三个常数**先实测后定值**，实测口径：只读打开 backend/novel_dev.db
# （sqlite uri mode=ro，不进测试、不入库的临时脚本）。

#: 开关：置 False 即逐字节恢复改动前行为（设计文档 §3.2 / §9 回滚手段）。
_RAG_QUERY_TARGETED = True

#: 目标侧**单项**截断上限（字符）。
#: 实测：`source='generated'` 且 `props.status='open'` 的伏笔 question 长度
#: 6 / 8 / 8 / 11 / 14 —— 中位 8、最长 14；角色名 2~3 字符。
#: 取 40 = 实测最长值的 2.86 倍（真实数据永不触及），同时等于设计文档 §3.1
#: 「上界情景」用的「每条伏笔 40 字符」口径：单条病态长句不能吃光整个查询词。
_C_ITEM = 40

#: 衔接侧（`prior[-1]` 尾部窗口）保留的字符数。
#: 实测（只读 backend/novel_dev.db）：`_prior_summaries` **实际返回的串长**为
#: 24 / 29 / 38 / 44 / 46 字符（n=5，中位 38、最长 46；走扁平 `projects.summary`
#: 兜底的旧项目，其项串与 summary_tree 的 L1 项同源同长度）。
#: 故 200 在本库**永不触发**——它的作用不是「压真实分布」，而是对超出 L1 惯例
#: 长度的**病态摘要**兜底，并给「目标侧 + 衔接侧」的总预算划一条可算的上界。
#:
#: 订正（QA round 2）：本注释与 e43830e 的提交正文原写「旧查询词实测 141~278，
#: 中位 230、最长 278」「200 收窄 28%」——那是**误量**：量的对象是
#: `generations.summary` 的 **JSON 列字符串长度**（CJK 被 `\uXXXX` 转义成每字
#: 6 字符，外加 `{"text": ...}` 信封），不是 `_prior_summaries` 返回的串长度。
#: 真实分布是上面的 24~46，据此的「收窄 28%」不成立，已删除该论断。
_C_TAIL = 200

#: 查询词总长上限（字符）。
#: 预算算式（逐项写出，便于核对）：伏笔 3×_C_ITEM = 120；项间分隔 2；
#: ⇒ target = 122；target 与 tail 之间 1 个空格；tail = _C_TAIL = 200；
#: ⇒ 122 + 1 + 200 = 323 ≤ 360，余 37 字符，可再容纳约 12 个 2~3 字角色名
#: （每个占「名 + 1 个空格」）。
#: 另一侧依据：把最坏情景压到 360。该情景的**精确口径**是
#: 「50 条 40 字伏笔 + 20 个 6 字角色名」⇒ target = 50*40 + 20*6 + (50+20-1) = 2189；
#: 再加 1 个分隔符与**已按 `_C_TAIL` 收窄到 200** 的尾巴 ⇒ 2189 + 1 + 200 = 2390，
#: 即「已收窄、尚未做总长截断」时约 2390 字符（不是把 2000 字原始前情整条算进去的
#: 4190——那是不截断、也不收窄尾巴的另一种口径）。
#: `app/tokenize.py:19` 对 CJK **逐字切分**，故查询词长度 ≈ BM25 查询项数。
#: 与旧行为的关系：旧实现**无封顶**（实测 `_prior_summaries` 最长 46 字符），
#: 所以本上限的价值不是「更省」，而是「确定」——把长度从「随摘要长度漂移」
#: 变成可算的常数。订正：原记「不超过旧最坏值 278 的 1.3 倍」的基准 278 是
#: JSON 信封长度，错误，该论断已删除。
_C_QUERY = 360


def _rag_query_targeted(reminders: dict, prior: list[str], project: Project) -> str:
    """Phase 0.5：把检索词从「上一段摘要」改为「本段目标 + 衔接侧」。

    优先级（目标侧在前，因为「接下来该收的线」比「刚写完什么」更贴近本段）：

    1. 目标侧：`reminders.unresolved_plotholes[*].question`（未填伏笔）→
       `reminders.character_constraints[*].name`（角色约束），每项压空白后截 `_C_ITEM`；
    2. 衔接侧：`prior[-1]` 的尾部 `_C_TAIL` 字符；
    3. **回退**：目标侧与衔接侧**皆空** → 逐字节返回 `_rag_query(prior, project)`。
       这只覆盖「无目标信号**且**无前情」（自由生成 / 首段无前情）一种情形。
       反过来的情形——**目标侧为空但有前情**——走的是第 2 步：查询词 = `prior[-1]`
       的尾部窗口。那是设计内的**收窄**，与旧行为**不同**（旧的会原样返回整条
       `prior[-1]`），不要把它读成"没目标信号就和以前一样"。
    4. 总长压到 `_C_QUERY`。

    **预算悬崖（既定后果，不是 bug）**：实现是
    `query = (target + " " + tail).strip()[: _C_QUERY]`，故 `len(target)` 逼近
    `_C_QUERY` 时衔接侧会被整段截掉——`len(target) == _C_QUERY - 1` 时只剩 1 个
    分隔空格；`len(target) >= _C_QUERY` 时连空格都不剩。也就是说「衔接侧总在」
    **不成立**：目标侧优先，且目标侧可以吃掉整个预算（50 条实测 14 字伏笔
    = 50*14+49 = 749 > 360，必然触发）。改常数时请同时看
    `tests/test_rag_query_targeted.py::test_t4c_query_budget_cliff_drops_tail`。

    成本不变：`reminders` 与 `prior` 都已在 `build_context` 里算好，本函数只做
    字符串拼接；`hybrid_search` 仍每本语料 1 次（`retrieval/search.py` 的检索耗时
    与查询词长度无关，只与语料规模有关）。
    """
    if not _RAG_QUERY_TARGETED:
        return _rag_query(prior, project)

    box = reminders or {}
    items: list[str] = []
    for hole in box.get("unresolved_plotholes") or []:
        if isinstance(hole, dict):
            # 压空白：`question` 里的换行会让日志/预览按行断裂，且对逐字分词无意义
            text = " ".join(str(hole.get("question") or "").split())
            if text:
                items.append(text[:_C_ITEM])
    for char in box.get("character_constraints") or []:
        if isinstance(char, dict):
            text = " ".join(str(char.get("name") or "").split())
            if text:
                items.append(text[:_C_ITEM])
    target = " ".join(items)

    # `str[:n]` 在串本身短于 n 时原样返回，无需先比长度
    tail = prior[-1][-_C_TAIL:] if prior and prior[-1] else ""

    if not target and not tail:
        return _rag_query(prior, project)

    # 两者只有一个非空时，中间的单个空格由 strip 去掉，不做分支判断
    query = (target + " " + tail).strip()
    return query[:_C_QUERY]


def _constraints_from(reminders: dict, project: Project) -> dict:
    """组装**真正下发进 prompt** 的那份锁定名单（角色名 + 未填伏笔）。

    constraints 是真正下发进 prompt 的那份（provider 会把它追加到 user 末尾做近因
    提醒，见 `llm/provider._apply_constraints`），因此它必须与 system 提示里的
    【角色人设约束】/【未填伏笔】用**同一个合并口径**，否则会出现「system 里列了、
    尾部自称完整的锁定名单却没列」的自相矛盾提示词，而漏掉的恰是最高权威层
    plot_bible（用户构思）。口径统一在 `prompts.merged_constraint_items`。

    抽成本函数（2026-10-17）是为了让重生成路径**不必**为这一个键跑一整轮
    `build_context`：那条路会为 RAG 与风格样例各加载一遍全部 `NovelChunk`，
    而重生成的 user prompt 明确不带 RAG 素材（见 `_build_generation_prompts`），
    加载结果全部丢弃。

    Args:
        reminders: `get_reminders(db, project.id, source="generated")` 的返回值。
        project: 项目，只用它的 `plot_bible`（最高权威层）。

    Returns:
        `{"characters": [角色名...], "plotholes": [伏笔问题...]}`。
    """
    chars, fores = merged_constraint_items(reminders, project.plot_bible)
    return {
        "characters": [
            c.get("name") for c in chars if isinstance(c, dict) and c.get("name")
        ],
        "plotholes": [f for f in fores if f],
    }


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

    stats = merge_segment_into_kg(db, project_id, text, segment_no)

    # v3.0 独立 Judge Agent：只读 v3.0 KG 表，覆盖哨兵没做的瞬移/预知/秘密外泄/
    # 时间穿越/伏笔超期/语言一致性。与哨兵并列（都只标红不改正文），失败同样不阻断。
    # 合并说明：master 侧本函数是「内联四步 + judge」，HEAD 侧已把四步收归
    # `merge_segment_into_kg` 唯一入口（7416114）。这里保留单一入口，只把
    # master 的 judge 一步叠加在上——否则合并会把「只更一半」的旧路径带回来。
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
    # Phase 0.5：查询词由「本段目标 + 衔接侧」拼成（见 _rag_query_targeted）。
    # 未选语料时不检索、也不报非空查询词——可观测字段必须与真实检索一致。
    rag_query = ""
    if novel_ids:
        rag_query = _rag_query_targeted(reminders, prior, project)
        for nid in novel_ids:
            try:
                hits = hybrid_search(db, nid, rag_query, k=4)
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

    # constraints = 真正下发进 prompt 的锁定名单（口径与 system 提示同源，
    # 详见 _constraints_from 的 docstring）。
    constraints = _constraints_from(reminders, project)

    return {
        "reminders": reminders,
        # 兼容读法：扁平 L1 链（RAG 查询词、旧接口、按段号对齐的断言都在用）
        "prior_summaries": prior,
        # 真正注入提示词的那份：有界、分层
        "prior_blocks": prior_blocks,
        "prior_report": prior_report,
        "rag_context": rag_context,
        # Phase 0.5 可观测：这次到底拿什么词去检索（供 plan 预览与人工 A/B 对照）
        "rag_query": rag_query,
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
#: **与 verify_stylometry.py 的 97.5 方向一致**（同一分布同一口径）：
#: percentile 的语义是"有多大比例的留出单元距离 ≤ 本段"，distance 越大 percentile 越大。
#: 所以 percentile 高 = 比大多数人类样本都更偏离，percentile 低 = 更像正常文体。
#:
#: v2.14 方案 B「对数粒度 profile」实施后，distance 系统性偏大问题缓解了
#: （段 1 mean_abs_z 3.476 → 2.094，-40%），但 percentile 仍 100/100——
#: bucket 留出分布 max 仍小于生成段 distance（30 章大奉 long bucket 留出 5
#: 单元 max ~ 1.5，段 2 distance 6.461 远超）。阈值从 97.5 提到 99.5：
#: (1) 与 verify_stylometry 离线验收保持同向，但放更宽——bucket 留出样本
#: 数少是工程边界，不应让 100% 触发常态化；(2) 高于 99.5% 才是真异常
#: （参考语料历史样本里都没有这种程度的距离）。
#: 真要回到 97.5 阈值，需参考 50+ 章 30 万字让 long bucket 留出 20+ 单元
#: —— 留出分布 max 自然扩大。详见 docs/style_log_buckets_design.md 风险评估节。
STYLE_WARN_PERCENTILE = 99.5
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


#: 命中片段的上下文字数：命中处前后各保留这么多字原文，方便人/模型读懂
#: 「当时是怎么用的」——只有模式和次数时只能说「仿佛×3」，给不出例句。
_FLAVOR_SNIPPET_CONTEXT = 15


def _flavor_snippet(text: str, start: int, end: int) -> str:
    """取命中区间 `[start, end)` 前后各 `_FLAVOR_SNIPPET_CONTEXT` 字的原文片段。

    触到文本边界的那一侧不补省略号（原文确实就这么长），被截断的一侧补
    「…」，表明片段是窗口而非全文。
    """
    lo = max(0, start - _FLAVOR_SNIPPET_CONTEXT)
    hi = min(len(text), end + _FLAVOR_SNIPPET_CONTEXT)
    snippet = text[lo:hi]
    if lo > 0:
        snippet = "…" + snippet
    if hi < len(text):
        snippet = snippet + "…"
    return snippet


def _detect_ai_flavor(text: str) -> dict:
    """检测文本中的 AI 腔模式，返回命中明细。

    返回结构（`hits` / `total` 与旧版同语义，新增字段纯增量，`_should_rewrite`
    等既有调用方不受影响）：

    - `hits`：`{pattern: 次数}`；
    - `total`：全部命中次数之和；
    - `occurrences`：`{pattern: [{"offset": 命中起始下标, "snippet": 原文片段}]}`，
      每个命中的 pattern 各带一份位置清单，供「总结本次具体哪里犯了 AI 腔」取例句。

    **子串去重（长模式优先）**：`AI_FLAVOR_PATTERNS` 同时含「毫无疑问」与
    「毫无疑问地」，旧实现逐模式 `text.count()`，同一处「毫无疑问地」会被两个
    模式各计一次，`total` 虚高。现按长度降序扫描，每个命中区间占用对应字符，
    落在已占用区间内的后续命中不再计数——同一处文本只归最长的模式。
    """
    claimed: set[int] = set()  # 已被某次命中占用的字符下标
    hits: dict[str, int] = {}
    occurrences: dict[str, list[dict]] = {}
    total = 0
    # 长模式优先：更具体的模式（「毫无疑问地」）先占位，短模式（「毫无疑问」）
    # 落在已占区间内的部分自动跳过。sorted 稳定，等长模式保持库内原顺序。
    for pattern in sorted(AI_FLAVOR_PATTERNS, key=len, reverse=True):
        start = text.find(pattern)
        while start != -1:
            end = start + len(pattern)
            # 同一模式内部按「不重叠」计数（与 str.count 语义一致）：命中后按
            # 模式长度前进，不逐字滑动。
            if all(i not in claimed for i in range(start, end)):
                claimed.update(range(start, end))
                hits[pattern] = hits.get(pattern, 0) + 1
                total += 1
                occurrences.setdefault(pattern, []).append(
                    {"offset": start, "snippet": _flavor_snippet(text, start, end)}
                )
            start = text.find(pattern, start + len(pattern))
    return {"hits": hits, "total": total, "occurrences": occurrences}


def _should_rewrite(text: str, forbidden: dict | None) -> tuple[bool, list[str]]:
    """判断是否需要重写。命中 AI 腔模式或反向禁忌时返回 True。

    阈值：AI 腔模式命中 ≥ 5 次，或反向禁忌命中 ≥ 2 次。
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
        # 方案 B「对数粒度 profile」：把 bucket / scale_ratio / chars 一起给前端
        # Style 审计 subtab 可显式展示「本段走 long bucket / scale_ratio 2.0x」
        "bucket": d.get("bucket"),
        "scale_ratio": d.get("scale_ratio"),
        "chars": d.get("chars"),
    }


def _prompt_overrides(db, project_id: int) -> dict[str, str]:
    """读取该项目三个提示词覆写槽位的生效文本（未设置 = 空串）。

    三个槽位的语义以 `app.agents.prompt_overrides.PromptKey` 为准
    （style_suffix / constraint_note / directive_suffix），本函数只负责
    「读出来交给提示词组装」，不改写任何语义。

    读不到（库缺表、查询异常等）时按「未设置」处理并告警——覆写是增强，
    绝不能阻断生成主流程；老项目没有覆写行时走同一条空串路径，行为不变。
    """
    overrides: dict[str, str] = {}
    for key in (PromptKey.STYLE, PromptKey.CONSTRAINT, PromptKey.DIRECTIVE):
        try:
            text, _source = resolve_prompt_block(db, project_id, key)
        except Exception as exc:  # noqa: BLE001 —— 覆写读取失败降级为「未设置」
            logging.getLogger(__name__).warning(
                "提示词覆写读取失败，按未设置处理（project_id=%s, key=%s）: %s",
                project_id, key, exc,
            )
            text = ""
        overrides[key] = text or ""
    return overrides


def _schedule_async_audit_kg(db, project_id: int) -> None:
    """每 N 段异步触发 audit_kg agent（fire-and-forget，不阻塞 generate）。

    audit_kg 是 LLM 密集工具，**不能**在主链同步等结果。
    用 daemon thread 启动，结果落 AgentRun 表 + 助手页可见。
    """
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
    overrides = _prompt_overrides(db, project.id)
    # —— 时间线：算出当前剧情点提示（有 outline 才启用；无则 None 走原逻辑）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
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
        directive_suffix=overrides[PromptKey.DIRECTIVE],
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
        # 重写（完整档会带原文对照证据）——流式与非流式共用同一实现，语义一致
        rewritten, info = _rewrite_with_evidence(db, project, provider, text, reasons, forbidden)
        if rewritten:
            text = rewritten
            summary = provider.summarize(text)
            rewrite_info = info

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
            logging.getLogger(__name__).warning(
                "style_warning 落库失败 (seg %s): %s", segment_no, style_warning, exc_info=True)
    # v2.1：每 AUTO_AUDIT_INTERVAL 段异步触发 audit_kg（不阻塞 generate）
    if segment_no and segment_no % AUTO_AUDIT_INTERVAL == 0:
        _schedule_async_audit_kg(db, project.id)

    # v2.6 诊断性 B：检测白名单外"疑似角色"——只告警，不替换（旁路观测）
    # 启发式会有假阳性（普通词组也报），调用方拿到列表后可聚合展示
    from app.generation.guard import detect_unlisted_characters, collect_allowed_names
    _allowed = collect_allowed_names(
        ctx_reminders=ctx.get("reminders"),
        plot_bible=project.plot_bible,
    )
    unlisted = detect_unlisted_characters(text, _allowed)
    if unlisted:
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
    overrides = _prompt_overrides(db, project.id)
    # —— 时间线：算出当前剧情点提示（与同步版同口径，db 用于落库）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
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
        directive_suffix=overrides[PromptKey.DIRECTIVE],
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
        # 与 generate_next_segment 共用同一实现：流式路径的重写语义必须完全一致
        rewritten, info = _rewrite_with_evidence(db, project, provider, text, reasons, forbidden)
        if rewritten:
            text = rewritten
            summary = provider.summarize(text)
            rewrite_info = info

    gen, segment_no = _commit_segment(db, project, text, summary)
    # v2.3：v2.1 集成到流式版——commit 段后调 judge_and_advance（与同步版同口径）
    node_judge = _judge_and_advance(db, project, provider, text, written)

    # —— 图谱进入生成闭环（与 generate_next_segment 顺序对齐）——
    # 必须放在 _commit_segment 之后、_rollup_summary_tree 之前：正文已落库才归档，
    # 且摘要滚上失败不该牵连簿记（滚上后置，失败只影响它自己）。
    # 复用 _merge_segment_into_graph（它已委托到 app.kg.build.merge_segment_into_kg
    # 做图谱+账本+别名归一+哨兵四步）——**不另写一套实现**：7416114 才把四步编排
    # 收归唯一入口，重写的结果就是又出现"只更一半"的路径。
    # 保留 try/except：簿记异常绝不打断 SSE（正文已落库，done 必须照样发）。
    kg_stats = None
    try:
        kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
    except Exception as exc:  # noqa: BLE001 —— 簿记异常绝不打断 SSE：正文已落库，
        # done 事件必须照样发出，否则前端表现为"生成卡住且无结果"。
        logging.getLogger(__name__).warning("流式 KG 簿记失败，段落已落库: %s", exc)
        kg_stats = {"merged": False, "reason": f"KG 簿记失败：{str(exc)[:100]}"}

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
        # 纯新增键：done 的既有字段语义一个都不改。挂簿记统计是为了让
        # 流式路径也能被观测（此前流式完全不做归档，出了问题无从判断）。
        "kg": kg_stats,
        # master 侧把流式键统一为 kg_update（与同步路径同键）。两个都发：
        # 老前端读 kg、新消费方读 kg_update，谁都不会静默拿不到。
        "kg_update": kg_stats,
        "node_judge": node_judge,
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
    reminders: dict | None = None,
    node_prompt: str | None = None,
):
    """组装提示词；`before_segment` 给定时的前情**只取该段之前**（重生成用）。

    实现方式不是「把整条链换掉」，而是从当前摘要树里截出等价的一棵
    （`summary_tree.tree_before`），再交给同一套 compose_prior——
    这样重生成与正常续写的**前情口径完全一致**（同样的分层、同样的预算），
    避免出现「正常续写有主线骨架、重生成没有」这种隐性不一致。

    `reminders`（2026-10-17）：调用方已加载的提醒包。重生成路径用它同时喂
    system 提示与尾部的 constraints ——**同一个对象**，不只是省一次查询，更是
    防止两次查询之间发生并发写入，导致「system 列了一套锁定名单、尾部
    constraints 列了另一套」的自相矛盾提示词。为 None 时本函数自行加载。

    `novel_ids` 目前**未被使用**：重生成有意不带 RAG 素材（见下方 `[]` 实参），
    保留参数以维持调用方契约。
    v2.3：新增 `node_prompt` 参数（v2.2 节点粒度强化后，stream 版走
    原逻辑模式会注入整条 outline + plot_bible + premise，模型拿到就
    演完整条故事——这正是"第一章就完结"bug 的变体）。传 node_prompt
    后 build_user_prompt 走时间线模式，剔除全故事信息。
    """
    if max_chars is None:
        max_chars = project.generation_length or 3000

    if reminders is None:
        reminders = get_reminders(db, project.id, source="generated")

    overrides = _prompt_overrides(db, project.id)

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
        project.title, reminders,
        _style_info(project), project.plot_bible, _style_profile(db, project),
        _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
    )
    user = build_user_prompt(
        l1_chain(tree),
        [],  # 重生成不带 RAG 素材：避免把「参考语料」的语感混进重写，保持与原段同源
        project.outline,
        max_chars,
        project.plot_bible,
        project.premise,
        prior_blocks=blocks,
        directive_suffix=overrides[PromptKey.DIRECTIVE],
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

    `novel_ids` 目前**未被使用**——重生成有意不带 RAG 素材（见
    `_build_generation_prompts` 的 `[]` 实参），保留参数以维持调用方契约。
    v2.3：传 `node_prompt` 给 `_build_generation_prompts`——重生成用时间线模式
    剔除全故事信息。**不**调 `_judge_and_advance`——重生成替换第 N 段语义
    上不改 `_next_segment_no`（已存在），不应触发 state advance。
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

    # 提醒包**只加载一次**，同一个对象同时喂 system 提示与尾部 constraints：
    # 除了省一次查询，更是防止两次查询之间发生并发写入，导致「system 列了一套
    # 锁定名单、尾部 constraints 列了另一套」的自相矛盾提示词。
    reminders = get_reminders(db, project.id, source="generated")
    # v2.3：算当前剧情点提示（重生成第 N 段时该段已存在，written_segments = N-1，
    # 与 generate_next_segment 同步版同口径）。
    written = segment_no - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)

    # 前情 = 只取该段之前的树（截断时若 L3 混入了被替换内容，会自动丢弃并重建）
    system, user, max_chars = _build_generation_prompts(
        db, project, before_segment=segment_no, novel_ids=novel_ids,
        max_chars=max_chars, provider=provider, reminders=reminders,
        node_prompt=node_prompt,
    )
    # constraints 与正常续写同源（角色/伏笔的锁定名单不能因重生成而变）。
    # 只组装这一个 dict，**不跑 build_context**：那条路会为 RAG 与风格样例各加载
    # 一遍全部 NovelChunk，而重生成的 user prompt 不带 RAG 素材，结果全部丢弃。
    constraints = _constraints_from(reminders, project)

    pieces: list[str] = []
    for piece in provider.stream_chat(system, user, constraints=constraints):
        pieces.append(piece)
        yield {"type": "chunk", "text": piece}

    text = _strip_thinking("".join(pieces))
    summary = provider.summarize(text)

    # —— 零成本 AI 腔检测 + 告警（方案 b，见 docs/architecture/2026-10-17-phase1-writer-tool-calling-assessment.md §4.2 ③）——
    # 复用 _should_rewrite 的纯计数判据（与续写路径口径一致，零 LLM 调用、不加载语料）。
    # 命中即带 rewrite_info 告警信号，提示「本段可能残留 AI 腔，未自动修」。
    # 故意不做自动重写：重写 B 路会全量加载语料（undo §8.1 的节省），留作后续 opt-in（方案 c）。
    _regen_narrative = _style_narrative(db, project)
    _regen_forbidden = (_regen_narrative or {}).get("forbidden") or {}
    rewrite_info = None
    _need_rewrite, _reasons = _should_rewrite(text, _regen_forbidden)
    if _need_rewrite:
        rewrite_info = {
            "alert": True,
            "reasons": _reasons,
            "original_chars": len(text),
            "rewritten_chars": None,
        }

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
        "rewrite_info": rewrite_info,
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
