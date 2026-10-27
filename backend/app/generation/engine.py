"""M4 生成引擎：上下文组装 → 续写 → 段级总结 → 入库（支持段级续写环）。

漂移防护机制：M3.3 的未填坑 + 角色约束进入 system 提示并全程锁定；
前情注入改走**层级摘要树**（L1 段 / L2 卷 / L3 主线骨架，见 summary_tree.py），
把无界的段级摘要链压成有界前情块——实测全量拼接时 N=300 会膨胀到单段正文的 656%。
"""

import logging
import copy
import re

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
    with_authority,
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
from app.generation.profiles import DEFAULT_PROFILE, STYLE_REFERENCE_PATHS, rules_for
from app.retrieval.strategies import (
    render_retrieval_blocks,
    retrieve_for_character,
    retrieve_for_character_auto,
    retrieve_for_continuation,
)
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


def _reference_chapter_hint(db, project: Project, novel_id: int, written: int) -> int | None:
    """Compatibility helper: generated counts and legacy starts grant no visibility."""
    from app.generation.visibility import VisibilityPolicy
    return VisibilityPolicy(project, project.reference_novel_ids or []).cutoffs.get(novel_id)


def _roleplay_character_name(project: Project, reminders: dict | None = None) -> str | None:
    """角色聊天时，被扮演的是哪个角色。

    取值顺序（**每一步都是真实存在的来源**）：
    1. `plot_bible.roleplay_character` —— 用户在界面上选定的那个；
    2. `plot_bible.characters` 的第一个 —— 用户自己写进剧情圣经的角色；
    3. **图谱抽出来的角色**（`reminders["character_constraints"]`）的第一个。

    ## 为什么必须有第 3 步

    之前只查前两步 ⇒ 真跑（北荒刀歌续写验证项目）返回 **None** ⇒
    **角色定向检索根本没跑**，`rag_items` 0 条 ⇒ 角色「什么都不知道」，
    只能编。
    而那个项目的角色（陆长风 / 陆崇 / 老周）明明在界面上的
    【角色人设约束】里 —— 它们来自**图谱抽取**，不在 plot_bible 里。
    ⇒ 只查 plot_bible 就等于查不到「界面显示存在的角色」。

    取不到返回 None（此时不检索）。
    """
    bible = project.plot_bible or {}
    if isinstance(bible, dict):
        explicit = bible.get("roleplay_character")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        chars = bible.get("characters") or []
        if isinstance(chars, (list, tuple)) and chars:
            first = chars[0]
            if isinstance(first, dict):
                nm = first.get("name")
                if isinstance(nm, str) and nm.strip():
                    return nm.strip()
            elif isinstance(first, str) and first.strip():
                return first.strip()
    # 兜底：图谱抽出来的角色（与 system 提示的【角色人设约束】同一个来源）
    for c in ((reminders or {}).get("character_constraints") or []):
        nm = str((c.get("name") if isinstance(c, dict) else c) or "").strip()
        if nm:
            return nm
    return None


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


# 段内自我重复的告警阈值：删掉超过这个比例说明模型已经陷入复读循环
_REPETITION_WARN_RATIO = 0.15


def _dedup_before_commit(text: str) -> tuple[str, dict]:
    """落库前清掉段内几乎逐字重复的段落，返回 (清理后文本, 统计)。

    v2.272 五章实测：第 5 段 1402 字里约 700 字是复读——同一句对白、同一段心理
    描写原样出现两次，7 处成对重复，用户读到的就是复制粘贴。

    提示词里已经写了「不要重复已写过的内容」，模型照旧复读，说明这不是指令层面
    能解决的问题，必须在产出后作确定性处理（也不该再调一次 LLM 重写，多一次调用
    多一次波动）。

    判据见 app/generation/dedup.py：只删「几乎逐字相同」的段落，保留首次出现，
    不改写任何正文。删段后摘要需要调用方重算（摘要基于正文）。
    """
    import logging

    from app.generation.dedup import strip_self_repetition

    cleaned, stats = strip_self_repetition(text)
    if stats["removed_paragraphs"]:
        log = logging.getLogger(__name__)
        if stats["ratio"] >= _REPETITION_WARN_RATIO:
            log.warning(
                "段内自我重复严重：删 %d 段 / %d 字（占 %.1f%%），模型可能陷入复读循环",
                stats["removed_paragraphs"], stats["removed_chars"], stats["ratio"] * 100,
            )
        else:
            log.info(
                "段内去重：删 %d 段 / %d 字（%.1f%%）",
                stats["removed_paragraphs"], stats["removed_chars"], stats["ratio"] * 100,
            )
    return cleaned, stats


#: 触发文风改写的判据用哪几个特征。
#:
#: 只挑**改写修得动**的：改写只做一件事 —— 把「一句一动作」的碎句合成有呼吸的
#: 复合句（见 `generation/rewrite.py`）。所以判据只能是「碎」类特征。
#: 对白形态（纯台词段占比）虽然也是真实差距，但**改写修不了它**
#: （改写提示词明确要求「对白必须保留引号，不要把对话并进叙述句里」），
#: 拿它当触发条件只会白花一次模型调用。
REWRITE_GATE_FEATURES = ("句长均值", "句号密度")


def _strip_ws(text: str) -> str:
    """去掉所有空白 —— 用来验证「只改了段落边界（和那一处标点形态）、内容一个字节没动」。"""
    return re.sub(r"\s+", "", text or "")

#: 闸门判定的**最小文本长度**。太短的文本（试写、占位）统计噪声大，
#: 句长均值会被长度归一化系数压低 → 误判成「碎」→ 白花一次改写调用。
#: 与诊断规则同口径（那边是 100；这里要更严，因为代价是一次模型调用）。
REWRITE_GATE_MIN_CHARS = 500


def _rewrite_gate(text: str, profile: dict | None) -> tuple[bool, list[dict]]:
    """这段正文离参考书有多远？偏离到「严重」才值得动一次改写。

    ## 尺子 = 项目已落库的文风档案

    就是**真正注入提示词的那一份**（`_style_profile(db, project)`），不是另算一遍。
    没有档案就没有「像不像」的基准 → 不改写（档案是增强，不是生成的前提）。

    ## 阈值 = 参考书自己的分布，不拍脑袋

    - 「句长均值 < p10」= 比 **90% 的人类样本**还碎；
    - 「句号密度 > p90」= 比 **90% 的人类样本**还碎。

    分位数来自参考书自己的分布：语料换了尺子也跟着换，不用维护第二套常量。

    ## 返回

    `(severe, reasons)` —— reasons 里每条带特征名、实测值、档位边界，
    便于在日志/接口里说清「为什么这次动了改写」。
    """
    if not profile or not text or not text.strip():
        return False, []
    if len(text.strip()) < REWRITE_GATE_MIN_CHARS:
        return False, []
    try:
        from app.style.stylometry import band_check

        checks = band_check(text, profile, REWRITE_GATE_FEATURES)
    except Exception:  # noqa: BLE001 —— 算不出来就不改写，绝不能阻断生成
        return False, []
    reasons: list[dict] = []
    for name in REWRITE_GATE_FEATURES:
        c = checks.get(name)
        if not c:
            continue
        value = float(c["value"])
        band = c["band"]
        if name == "句长均值":
            bound = float(band.get("p10") or 0.0)
            if bound and value < bound:
                reasons.append({"feature": name, "value": round(value, 1),
                                "bound": round(bound, 1), "direction": "below",
                                "detail": f"句长 {value:.1f} 字 < 档案 p10 {bound:.1f} 字"})
        else:
            bound = float(band.get("p90") or 0.0)
            if bound and value > bound:
                reasons.append({"feature": name, "value": round(value, 1),
                                "bound": round(bound, 1), "direction": "above",
                                "detail": f"句号密度 {value:.1f}/千字 > 档案 p90 {bound:.1f}"})
    return bool(reasons), reasons


def _rewrite_decision(project: Project, override: bool | None = None) -> str:
    """这次要不要做文风改写：``always`` / ``never`` / ``gate``。

    优先级（从高到低）：

    1. **单次请求的显式指定**（`override`，来自请求体 ``style_rewrite``）——
       「这一段别改写」是用户能当场做的决定；
    2. **项目显式设过**（``projects.style_rewrite = 1``）→ ``always``；
    3. 全局总开关（``settings.style_rewrite``）关 → ``never``；
    4. 全局闸门（``settings.style_rewrite_gate``）开 → ``gate``，否则 ``always``。

    ## 为什么要分「闸门」这一档

    改写要**多一次模型调用**（M3 关思考实测 19s/900 字；换成 M3.1-Flash 更慢），
    而它**只修得动「碎」**。对一段本来就够长的正文动它，纯属白花一次调用
    （还可能压缩 15-25% 字数）。所以默认改成「偏离严重才动」。

    ## 关于「项目显式关」

    ``projects.style_rewrite`` 是 ``NOT NULL DEFAULT 0``，库里分不出
    「从没设过」与「明确关掉」——所以 0 = 跟随全局，不是 never。
    要单次关掉用请求体（override）。
    """
    if override is not None:
        return "always" if override else "never"
    if getattr(project, "style_rewrite", 0):
        return "always"
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "style_rewrite", True)):
            return "never"
        return "gate" if bool(getattr(_st, "style_rewrite_gate", True)) else "always"
    except Exception:  # noqa: BLE001
        return "never"


def _rewrite_provider(writer_provider):
    """改写这一步用哪个 provider（默认与写手同一个）。

    ## 为什么要能换

    改写要的只是「把碎句合成长句」，**不需要写作模型的推理能力**。实测
    （真书续写的真实正文，MiniMax-M3.1-Flash 做写手）：

    | 改写模型 | 用时 | 成功率 | 句长 | 纯台词段 |
    |---|---|---|---|---|
    | MiniMax-M3 | 6~9s | 3/3 | 21.8 → 36.8 | 42% → 35% |
    | MiniMax-M3.1-Flash | 159s | 1/3（另两次 603s 超时） | 21.8 → **54.4** | 42% → **6%** |

    Flash 改写效果更好但慢且不稳 —— 把它留给「写」这一步更合适。

    ## 配了但用不了怎么办

    渠道不认这个模型、没有 key、或构建抛错 → **回落到写手的 provider**
    （记一条 warning）。改写失败绝不能影响已经生成好的正文。
    """
    try:
        from app.config import settings as _st
        model = str(getattr(_st, "style_rewrite_model", "") or "").strip()
    except Exception:  # noqa: BLE001
        model = ""
    if not model:
        return writer_provider
    try:
        from app.config import settings as _st
        from app.llm.provider import build_provider

        channel = (str(getattr(_st, "writer_provider", "") or "").strip()
                   or str(getattr(_st, "llm_provider", "") or "").strip())
        if not channel:
            return writer_provider
        api_key = (str(getattr(_st, "writer_api_key", "") or "").strip()
                   or str(getattr(_st, f"{channel}_api_key", "") or "").strip())
        base_url = (str(getattr(_st, "writer_base_url", "") or "").strip()
                    or str(getattr(_st, f"{channel}_base_url", "") or "").strip())
        if not api_key:
            logging.getLogger(__name__).info(
                "改写模型 %s 不可用（没有 %s 的 key），沿用写手 provider", model, channel
            )
            return writer_provider
        return build_provider(
            channel, api_key, model, base_url=base_url,
            max_tokens=int(getattr(_st, "writer_max_tokens", 0) or 0) or None,
            timeout=int(getattr(_st, "writer_timeout", 0) or 0) or 120,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "改写 provider 构建失败，沿用写手 provider: %s", exc
        )
        return writer_provider


def _maybe_correct_form(
    db,
    project: Project,
    text: str,
    provider,
    *,
    profile: dict | None,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """形态校正：把正文的对白组织方式掰到**参考书自己的形态**。

    ## 为什么要有（实测）

    两个模型的先验都偏「比参考书更裸台词」（《北荒刀歌》：目标逐单元均值 16.2%，
    M3 写 58.1%、Flash 写 27.7%）。**提示层挪不动**（四轮实验在噪声内），
    **事后改写能改**（Flash 改写把纯台词段 42% → 6%）—— 所以这是一次
    **独立的改写**，不是改提示词。

    ## 闸门

    尺子 = 参考书**自己**的逐单元分布（p10~p90）：这一段的对白形态比 90% 的
    人类样本还极端，才值得多花一次模型调用去掰。落在带子里 → 不动。

    ## 目标与样例

    目标 = 分布中心（均值）；样例 = **同一本书**里形态正确的原文段落 ——
    给「形态的正确实例」比给统计数字有用。

    ## 失败策略

    `style/form.correct_form` 内部有四道判据（字数、对白内容、形态是否靠近、
    是否过冲），任何一道不过就返回原文。这里再兜一层异常。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "style_form_correction", True)):
            return text, {"applied": False, "reason": "形态校正开关关闭"}
        mode = str(getattr(_st, "style_form_correction_mode", "merge") or "merge")
    except Exception:  # noqa: BLE001
        mode = "merge"
    try:
        from app.style.form import (
            correct_form,
            form_target,
            measure_form,
            pick_form_examples,
        )
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("形态校正模块不可用: %s", exc)
        return text, {"applied": False, "reason": "模块不可用"}

    target = form_target(profile or {})
    if not target:
        return text, {"applied": False, "reason": "档案里没有形态目标"}

    measured = measure_form(text)
    # 闸门用 `band_check`（**同一把尺**）：它把文本侧的长度归一化因子**除掉**，
    # 再与档案的分布带比。直接用 `measure_form()["unit_ratio"]` 会带着长度因子 ——
    # 一段 2500 字的正文被缩到 0.95，一段 1800 字的被缩到 0.86 ——
    # 于是「形态刚好出带」的段会被判成带内（实测：2547 字那段 32.5%→实为 34.4%，
    # p90 是 33.5%，本来就该校正）。
    # 档案的带是在 unit 长度上拟的（因子 ≈ 1），所以两边要同一把尺。
    ratio = measured["unit_ratio"]
    try:
        from app.style.stylometry import band_check as _band_check

        _got = (_band_check(text, profile, ["纯台词段占比"]) or {}).get("纯台词段占比")
        if _got:
            ratio = float(_got.get("value") or ratio)
    except Exception:  # noqa: BLE001 —— 量不出来就用原值，闸门放宽总比误判成带内强
        pass
    # 落在参考书自己的分布带里 → 不动
    if target["p10"] <= ratio <= target["p90"]:
        return text, {"applied": False, "reason": "形态在参考书分布带内",
                      "measured": measured, "target": target}

    if mode == "llm":
        # 样例：同一本书里形态正确的段落
        examples: list[str] = []
        try:
            from app.models import NovelChunk
            from app.style.stylometry import paragraphs

            ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
            if ids:
                rows = db.query(NovelChunk).filter(NovelChunk.novel_id.in_(ids)).limit(1200).all()
                pool: list[str] = []
                for r in rows:
                    pool.extend(paragraphs(r.text or ""))
                examples = pick_form_examples(pool, target["target"])
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("形态样例取不到: %s", exc)

        try:
            return correct_form(text, provider, target_ratio=target["target"], examples=examples)
        except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
            logging.getLogger(__name__).warning("形态校正异常，保留原文: %s", exc)
            return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}

    # —— 默认：机械校正（只改段落边界；唯一的内容改动是把并接处的句号改成冒号）——
    try:
        from app.style.form_merge import merge_bare_dialogue
        from app.style.stylometry import paragraphs as _paras

        base_paras = _paras(text)
        work_paras, minfo = merge_bare_dialogue(base_paras, target["target"])
        sep = "\n\n" if "\n\n" in text else "\n"
        if minfo.get("merged"):
            # 守恒检查：**只允许改段落边界 + 把并接处的「。」改成「：」**。
            # 「把冒号改回去」之后必须与原文逐字相同 —— 这比原来还严：
            # 它证明除了那一处标点形态修正之外，一个字都没动。
            revert = minfo.get("revert_paragraphs") or work_paras
            if _strip_ws(sep.join(revert)) != _strip_ws(text):
                return text, {"applied": False, "reason": "机械校正改动了内容，拒绝",
                              "measured": measured, "target": target}
            after_merge = measure_form(sep.join(work_paras))
            if abs(after_merge["bare_ratio"] - target["target"]) >= abs(
                    measured["bare_ratio"] - target["target"]):
                work_paras = base_paras
                after_merge = measured
                minfo = {"merged": 0}
        else:
            after_merge = measured

        # —— 第二步：机械路线到顶了 ⇒ 用模型补引导语 ——
        #
        # 机械合并只改段落边界，能把裸台词占比拉下来但**到不了目标**
        # （实测 9 段真实产物：71.5% → 42.2%，参考书 22.7%）。
        # 剩下的差距只能靠**新增内容**补 —— 而这一步**会新增文字**，
        # 所以它跟机械合并不是一回事：**闸门相同、判据不同**。
        # 只有「机械合并之后仍在带外」才付这次模型调用的钱。
        lead_info = {"applied": False}
        ratio_now = after_merge["bare_ratio"]
        if not (target["p10"] <= ratio_now <= target["p90"]):
            try:
                from app.config import settings as _st2
                lead_on = bool(getattr(_st2, "style_lead_in", False))
            except Exception:  # noqa: BLE001
                lead_on = False
            if lead_on:
                try:
                    from app.style.lead_in import insert_lead_ins

                    work_paras, lead_info = insert_lead_ins(
                        work_paras, provider, target_ratio=target["target"],
                        proper_nouns=_reference_proper_nouns(db, project, novel_ids),
                    )
                except Exception as exc:  # noqa: BLE001 —— 补写失败绝不能影响正文
                    logging.getLogger(__name__).warning("引导语补写异常，保留机械结果: %s", exc)
                    lead_info = {"applied": False, "reason": f"异常 {type(exc).__name__}"}

        final_text = sep.join(work_paras)
        after = measure_form(final_text)
        if abs(after["bare_ratio"] - target["target"]) >= abs(measured["bare_ratio"] - target["target"]):
            return text, {"applied": False, "reason": "形态校正没逼近目标",
                          "measured": measured, "target": target}
        logging.getLogger(__name__).info(
            "形态校正：纯台词段 %.0f%% → %.0f%%（目标 %.0f%%；机械并 %d 段"
            "、改了 %d 处标点形态、补引导语 %d 处）",
            measured["bare_ratio"] * 100, after["bare_ratio"] * 100,
            target["target"] * 100, minfo.get("merged", 0),
            int(minfo.get("colon_fixed") or 0), int(lead_info.get("inserted") or 0),
        )
        return final_text, {"applied": True, "mode": "merge",
                            "before": measured, "after": after,
                            "target": target, "merged": minfo.get("merged", 0),
                            "colon_fixed": int(minfo.get("colon_fixed") or 0),
                            "lead_in": lead_info}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("机械形态校正异常，保留原文: %s", exc)
        return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}


def _candidate_pool_size() -> int:
    """候选池大小（1 = 关闭）。

    默认 **1（关闭）**：多采样 = 多花模型时间与额度（一次真跑 4~9 分钟/段），
    而实测收益集中在「默认写手之外的模型」上 ——
    Flash 经机械校正后 4/4 候选已进带，候选池只是让离分布中心更近；
    M3 上机械校正后仍有 2/3 不在带内，那才是候选池的兜底场景。
    所以默认关，要用得显式开。
    """
    try:
        from app.config import settings as _st
        from app.generation.candidate import normalize_pool_size

        return normalize_pool_size(int(getattr(_st, "style_candidate_pool", 1) or 1))
    except Exception:  # noqa: BLE001 —— 配置读不到就当没开
        return 1


def _generate_with_pool(
    provider,
    system: str,
    user: str,
    *,
    constraints=None,
    stop_sequences=None,
    pool_size: int = 1,
    profile: dict | None = None,
    target_chars: int | None = None,
) -> tuple[str, dict]:
    """同一提示采 N 份，按「参考书自己的分布」择优，返回 (正文, 诊断)。

    ## 这是什么

    文献里对付「输出不达标但目标可测」的标准解法（Best-of-N / 拒绝采样）：
    同一提示采样多份，用一个**外部打分器**选最好的那份
    （[Best-of-N](https://api.emergentmind.com/topics/best-of-n-bon)、
    [拒绝采样](https://yijia.ws/llm/2026-08-26-057)）。

    ## 为什么在我们这里特别顺

    Best-of-N 的头号风险是 **reward hacking** —— 打分器有偏时，
    选择压力会放大它的偏好，挑出「分高但人读着差」的稿子。
    我们的打分器**不是学出来的偏好，是参考书自己拟合出来的分布**，
    它不会为了讨好指标而牺牲别的东西。

    ## 形态怎么打分：先机械校正，再量

    打分针对的是**校正后**的形态（机械校正能把「一行一句台词」掰成参考书形态），
    所以择优时用「预览变换」先并一遍再量 —— 但**返回的是原文**，
    真正的校正仍由流水线自己的那道工序做（它还有闸门与模式开关）。

    ## 成本

    N 次生成调用（一次真跑 4~9 分钟/段）。所以：
    - `pool_size = 1` 时**一次调用都不多**（直接返回第一次的输出）；
    - 要开得显式开（`settings.style_candidate_pool`）。

    ## 失败策略

    任何一份采样失败都跳过；一份都没成功 → 抛错让上层按老路处理。
    """
    if pool_size <= 1:
        text = provider.chat(system, user, constraints=constraints,
                             stop_sequences=stop_sequences)
        return _strip_thinking(text), {"applied": False, "reason": "候选池关闭"}

    from app.generation.candidate import score_candidate

    def _preview(t: str) -> str:
        """形态预览：机械合并一遍（不改原文，只为打分）。

        候选池要按**最终形态**判合格 —— 机械校正是流水线后面那道工序，
        所以这里先把它「预演」一遍。
        """
        try:
            from app.style.form import form_target
            from app.style.form_merge import merge_bare_dialogue
            from app.style.stylometry import paragraphs

            tgt = form_target(profile or {})
            if not tgt:
                return t
            new_paras, _info = merge_bare_dialogue(paragraphs(t), tgt["target"])
            return "\n".join(new_paras)
        except Exception:  # noqa: BLE001
            return t

    # —— 阈值式拒绝采样：边采边判，合格即停 ——
    # 固定 N 的问题是「白花两份的钱」：实测 Flash 经机械校正后 4/4 候选已在带内，
    # 多数情况第一份就合格。改成边采边判 ⇒ 多数情况成本 = 1 份。
    cands: list[str] = []
    scored: list = []
    last_exc: Exception | None = None
    stopped_early = False
    for _i in range(pool_size):
        try:
            raw = provider.chat(system, user, constraints=constraints,
                                stop_sequences=stop_sequences)
        except Exception as exc:  # noqa: BLE001 —— 一份失败不该毁掉整批
            logging.getLogger(__name__).warning("候选池采样失败: %s", exc)
            last_exc = exc
            continue
        t = _strip_thinking(raw)
        if not (t and t.strip()):
            continue
        cands.append(t)
        # 守恒判据的基准 = **外部的字数目标**（段长闸门用的那个），
        # **不是**同伴中位数 —— 候选之间长度天然不同（形态差的稿子往往更短），
        # 拿同伴当基准会把正常的候选判成「膨胀」或「缩水」。
        scored.append(score_candidate(_preview(t), profile=profile,
                                      baseline_chars=target_chars))
        if scored[-1].ok:
            # 合格即停 —— 不再采下一份（这一轮改动省时间的地方）
            stopped_early = True
            break

    if not cands:
        # 全部失败 → **把真实的异常抛出去**。
        # 不能在这里再调一次 provider（那只会再失败一次，把错误藏起来）。
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("候选池全部采样失败且没有异常信息")

    qualified_idx = [i for i, c in enumerate(scored) if c.ok]
    pool_idx = qualified_idx if qualified_idx else list(range(len(scored)))
    best_idx = min(pool_idx, key=lambda i: (scored[i].score, i))
    picked = cands[best_idx]
    best_score = scored[best_idx].score
    info = {
        "applied": True,
        "n_candidates": len(cands),
        "n_qualified": len(qualified_idx),
        "picked": best_idx,
        "pool_size": pool_size,
        "stopped_early": stopped_early,
        "score": (None if best_score == float("inf") else round(best_score, 4)),
        "reject": scored[best_idx].reject,
        "candidates": [
            {
                "index": i,
                "chars": _cjk_count(c),
                "score": (None if s.score == float("inf") else round(s.score, 4)),
                "reject": s.reject,
            }
            for i, (c, s) in enumerate(zip(cands, scored))
        ],
    }
    if not qualified_idx:
        info["reason"] = "无合格候选，落库离目标最近的一份并标注偏离"
    logging.getLogger(__name__).info(
        "候选池择优：采 %d 份%s，选第 %s 份（合格 %s/%s，离目标 %.1f%%）",
        len(cands), "（提前合格即停）" if stopped_early else "",
        info.get("picked"), info.get("n_qualified"), info.get("n_candidates"),
        (info.get("score") or 0.0) * 100,
    )
    return picked, info


def _reference_plain_dialogue_ratio(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    *,
    max_chunks: int = 200,
) -> float | None:
    """参考书自己的「光台词段」占比（A 类），即 beat 插入的目标。

    取不到（没有参考语料 / 出错）→ None，调用方用默认值。

    ## 为什么目标必须来自参考书

    「纯台词段占比」是**书级特征**，跨书差 4.2 倍：
    《北荒刀歌》 6% / 豪门夜宴 33% / 《长夜司》 54% / 镜中局 76%（实测，15 本真书）。
    ⇒ 写死一个常数会把**对话密集型书**改成叙事密集型 —— 那是把书改坏。
    """
    try:
        from app.models import NovelChunk
        from app.style.beat import is_plain_dialogue
        from app.style.stylometry import paragraphs as _paras

        ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
        if not ids:
            return None
        rows = (
            db.query(NovelChunk)
            .filter(NovelChunk.novel_id.in_(ids))
            .order_by(NovelChunk.novel_id, NovelChunk.idx)
            .limit(max_chunks)
            .all()
        )
        quote = 0
        plain = 0
        for r in rows:
            for p in _paras(r.text or ""):
                if "“" not in p:
                    continue
                quote += 1
                if is_plain_dialogue(p):
                    plain += 1
        if quote < 20:
            return None
        return plain / quote
    except Exception:  # noqa: BLE001 —— 取不到就返回 None
        return None


def _reference_corpus_text(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    *,
    max_chunks: int = 60,
    max_chars: int = 60000,
) -> str:
    """参考书原文（供 beat 判据 3 的语汇白名单用）。

    取不到就返回空串 —— 调用方会退化成「上下文词组」白名单（并在诊断里标明降级）。
    """
    try:
        from app.models import NovelChunk

        ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
        if not ids:
            return ""
        rows = (
            db.query(NovelChunk)
            .filter(NovelChunk.novel_id.in_(ids))
            .order_by(NovelChunk.novel_id, NovelChunk.idx)
            .limit(max_chunks)
            .all()
        )
        parts: list[str] = []
        total = 0
        for r in rows:
            t = r.text or ""
            if not t:
                continue
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "".join(parts)[:max_chars]
    except Exception:  # noqa: BLE001 —— 取不到就退化成上下文白名单
        return ""


def _reference_proper_nouns(db, project: Project, novel_ids: list[int] | None = None) -> set[str]:
    """参考书的专名表（判据「不能造新事实」用）。

    取不到就返回空集 —— 调用方据此**降级**（判据改成只查模型自报的专名）。
    """
    proper_nouns: set[str] = set()
    try:
        from app.style.entity_store import ensure_table, entity_entries

        ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
        for nid in ids[:2]:
            try:
                proper_nouns |= set(entity_entries(ensure_table(db, nid)))
            except Exception:  # noqa: BLE001 —— 一本书的表取不到就跳过
                continue
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("专名表取不到，判据降级: %s", exc)
    return proper_nouns


def _maybe_insert_beats(
    db,
    project: Project,
    text: str,
    provider,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """在连续对白段的开头**插入一句动作/神态叙述**（beat）。

    ## ⚠️ 默认关闭（`settings.style_beat_insert = False`）

    **这条路的参照物曾经是错的，整条方向已被真跑推翻**（清单 AD）：

    最早的判据拿**《北荒刀歌》**（A 类 7%~12%）当标准，得出「模型写的对白戏
    比参考书密 ⇒ 要加叙述」。但 15 本真书的 A 类占比是
    **min 7%（北荒刀歌）· p25 64% · 中位 72% · p75 81% · max 88%**
    ⇒ **北荒刀歌是特例**；我们生成的是 **61%**，**已在真书区间下沿**，
    不是「差了一倍」。

    真跑的量-效曲线（插 6/12/24/48 处）显示：动作密度升到真书 **5 倍**
    （=「网球法」），A 类只降到 35%，**采纳率还随量下降**。

    ⇒ 代码与判据**保留**（① 目标比例取自**参考书自己**，对动作密集的书
    仍有意义；② 它是「局部插入」这条路的实现与判据范例），但**默认不开**。

    ## 机制本身是可靠的

    局部插入（beat）比整段形态改写可靠得多：真跑**采纳 5/6（83%）**，
    而整段形态改写是 **0/20** —— 任务小、上下文现成、逐段可验收。

    ## 目标比例

    用**参考书自己的** A 类占比（从语料现算），不是写死的常数 ——
    不同书差别很大（北荒刀歌 7% / 镜中局 76% / 大靖 54%）。

    ## 失败策略

    插入失败绝不能影响正文（整段失败就整段放弃）。详见 `style/beat_insert.py`。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "style_beat_insert", False)):
            return text, {"applied": False, "reason": "beat 插入开关关闭"}
    except Exception:  # noqa: BLE001
        return text, {"applied": False, "reason": "配置读取失败"}
    try:
        from app.style.beat import DEFAULT_TARGET_A_RATIO
        from app.style.beat_insert import _insert_beats
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("beat 模块不可用: %s", exc)
        return text, {"applied": False, "reason": "模块不可用"}

    # 目标比例 = **参考书自己的** A 类占比（不是写死的常数）。
    # 不同书差别很大（北荒刀歌 6% / 镜中局 76% / 大靖 54%）⇒ 目标必须随书变。
    target = DEFAULT_TARGET_A_RATIO
    ref_ratio = _reference_plain_dialogue_ratio(db, project, novel_ids)
    if ref_ratio is not None:
        target = ref_ratio

    # 专名表（判据 3 用）：优先用参考书的专名表
    proper_nouns = _reference_proper_nouns(db, project, novel_ids)

    try:
        return _insert_beats(text, provider, target_ratio=target,
                             proper_nouns=proper_nouns or None,
                             corpus_text=_reference_corpus_text(db, project, novel_ids) or None)
    except Exception as exc:  # noqa: BLE001 —— 插入失败绝不能影响正文
        logging.getLogger(__name__).warning("beat 插入异常，保留原文: %s", exc)
        return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}


def _maybe_rewrite_style(
    project: Project,
    text: str,
    provider,
    *,
    profile: dict | None = None,
    override: bool | None = None,
) -> tuple[str, dict]:
    """按「偏离严重才动」的口径决定要不要做两段式文风改写。

    ## 为什么必须放在落库之前、去重之后

    顺序有讲究：
      AI 味重写 → 去重 → **文风改写** → 落库
    去重在改写之前，是因为复读段落改写后可能「看起来不同」而逃过去重；
    改写放最后，是因为它要与「最终要落库的文本」一一对应。

    ## 为什么要可选开关而不是默认行为

    改写是**风格增强**，不是正确性修复：
      - 它压缩 15-25% 字数（实测 3216 → 约 2700）
      - 它要额外一次调用（M3 关思考实测 19s/900 字）
      - 没选参考语料时没有「参考断句节奏」可对齐，开了纯浪费
    所以默认按**闸门**触发：偏离参考书越远的那一段才值得改。

    ## 失败策略

    `rewrite_style` 内部已做三道校验（字数缩水、对白丢失、句长未改善），
    任何一道不过就返回原文。这里再兜一层异常——改写挂了绝不能影响已经
    生成好的正文。
    """
    decision = _rewrite_decision(project, override)
    if decision == "never":
        return text, {"applied": False, "reason": "开关关闭"}
    if decision == "gate":
        severe, reasons = _rewrite_gate(text, profile)
        if not severe:
            return text, {"applied": False, "reason": "偏离未达闸门", "gates": reasons}
    try:
        from app.generation.rewrite import rewrite_style
        new_text, info = rewrite_style(text, _rewrite_provider(provider))
        if info.get("applied"):
            b, a = info.get("before"), info.get("after")
            logging.getLogger(__name__).info(
                "文风改写：句长 %.1f → %.1f，引号 %d → %d，字数 %d → %d",
                b[0] if b else 0, a[0] if a else 0,
                b[2] if b else 0, a[2] if a else 0,
                info.get("chars", (0, 0))[0], info.get("chars", (0, 0))[1],
            )
        else:
            logging.getLogger(__name__).info("文风改写未采纳：%s", info.get("reason"))
        return new_text, info
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("文风改写异常，保留原文: %s", exc)
        return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}


#: 照搬回灌改写后，允许的最短比例（低于它 = 改坏了，不采纳）。
#: 与 `generation/rewrite.py` 的 `MIN_LEN_RATIO` 同一口径：
#: 改写掉内容比改不掉更糟 —— 宁可这一段不落库，也不能悄悄丢情节。
_VERBATIM_MIN_LEN_RATIO = 0.60


def _maybe_fix_verbatim(
    db,
    project: Project,
    text: str,
    provider,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """照搬门禁：正文里若**逐字搬了**参考语料的原文，改掉；改不掉就不落库。

    ## 为什么放在所有正文改写之后、落库之前

    落库的是最终文本，门禁必须量最终文本 —— 任何在它之前的改写
    （去重 / 文风改写 / 形态校正 / beat 插入）都可能重新引入原文片段。

    ## 命中之后：回灌自改 → 改不掉就不落库

    与 `plot_structure.CopyViolation` 同一口径：把命中的片段指给模型，
    让它用自己的措辞重写，比直接报错让用户重做一次改对的概率高得多。
    仍改不掉 ⇒ 抛 `VerbatimViolation`（可重试失败），由路由层转成
    明确可读的错误，**这一段不落库**。

    ## 降级一律放行

    没有语料、索引没建好、扫描出错 —— 一律放行并在报告里标注「未执行」。
    门禁是加分项，不是主链路：索引坏了不能连正文都写不出来。
    """
    from app.config import settings
    from app.generation.verbatim import (
        VerbatimViolation, load_index, scan_verbatim, warm_index_async,
    )

    if not settings.style_verbatim_gate:
        return text, {"enabled": False, "verdict": "disabled"}
    if not text:
        return text, {"enabled": True, "verdict": "skipped", "reason": "empty_text"}

    ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
    if not ids:
        return text, {"enabled": True, "verdict": "skipped", "reason": "no_reference_corpus"}

    threshold = int(settings.style_verbatim_threshold or 25)
    index = load_index(db, ids)
    if index is None:
        # 索引还没建（建它要扫全语料，不能卡在请求里）→ 后台建，这一段先放行。
        warm_index_async(db, ids)
        return text, {"enabled": True, "verdict": "skipped", "reason": "index_not_ready"}

    report = scan_verbatim(text, index, threshold=threshold)
    report["enabled"] = True
    if report["verdict"] != "possible_copy":
        return text, report

    # —— 回灌自改（有界）——
    max_rewrites = max(0, int(settings.style_verbatim_max_rewrites or 0))
    attempts = 0
    for attempt in range(1, max_rewrites + 1):
        attempts = attempt
        hint = VerbatimViolation._build_hint(report["spans"], threshold)
        try:
            rewritten = provider.chat(_verbatim_system_prompt(threshold), text + hint)
        except Exception as exc:  # noqa: BLE001 —— 改写挂了不能连累正文以外的东西
            logging.getLogger(__name__).warning("照搬回灌失败，保留原文: %s", exc)
            break
        rewritten = _strip_thinking(rewritten or "")
        if not rewritten:
            break
        # 改写把内容削掉太多 ⇒ 判为「改坏了」，不采纳（照抄 MIN_LEN_RATIO 的口径：
        # 改写掉内容比改不掉更糟 —— 宁可这一段不落库，也不能悄悄丢情节）。
        if len(rewritten) < len(text) * _VERBATIM_MIN_LEN_RATIO:
            logging.getLogger(__name__).info(
                "照搬回灌：改写后字数 %d < 原文 %d 的 %.0f%%，判为改坏了，不采纳",
                len(rewritten), len(text), _VERBATIM_MIN_LEN_RATIO * 100)
            break
        again = scan_verbatim(rewritten, index, threshold=threshold)
        report["last_attempt"] = {"attempt": attempt, "verdict": again["verdict"],
                                  "match_count": again["match_count"]}
        if again["verdict"] != "possible_copy":
            report.update(again)
            report["rewritten"] = True
            report["attempts"] = attempts
            return rewritten, report
        report = again
    report["rewritten"] = False
    report["attempts"] = attempts
    # 改不掉 ⇒ 这一段不落库（抛可重试失败，让调用方/用户重试）。
    raise VerbatimViolation(report)


def _verbatim_system_prompt(threshold: int = 25) -> str:
    """回灌改写用的 system prompt。

    刻意只给一条要求：**换掉与参考原文逐字相同的那几处**。
    不让它「重写整段」—— 那是文风改写的活，两件事混在一起会把情节改丢。

    阈值写进提示里（而不是写死 25）—— 阈值是配置项，提示必须跟它一致。
    """
    return (
        "你是一位中文小说编辑。你的任务只有一件：把下面这段文字里"
        "**与参考原文逐字相同**的地方改写成你自己的措辞。\n\n"
        "要求：\n"
        "1. 情节、人物、对白内容、信息量一律不变；\n"
        "2. 只改**被指出来的那几处**的句子与用词，其余原样保留；\n"
        f"3. 改完之后，**不得**再出现任何连续 {threshold} 个字以上与参考原文相同的片段；\n"
        "4. 长度与原文差不多（上下浮动 10% 以内）。\n\n"
        "只输出改写后的正文，不要解释。"
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


def build_context(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    profile: str | None = None,
) -> dict:
    """组装生成所需的全部上下文（不调用 LLM，供 plan 预览与续写共用）。

    novel_ids 缺省时取 project.reference_novel_ids（用户选定的参考语料）。
    只检索「选定」的语料，绝不混入整库其他语料——这是防混库的第一道闸。
    多本语料各自独立 RAG，结果合并进 rag_context（纯文本检索，不污染图谱隔离）。

    `profile` 决定「这次该看见什么」，见 app/generation/profiles.py。
    缺省走原创（可见范围最小）。裁剪发生在函数末尾，只做减法。

    缺省（调用方没写 profile）时按项目的**锚定方式**推导：
    项目锚定在参考书上（`story_authority='reference_anchor'`）→ 续写路径，
    否则 → 原创。见 `profiles.resolve_profile`。
    """
    from app.generation.profiles import resolve_profile
    profile = resolve_profile(project, profile)
    if (project.plot_timeline or {}).get('body_revision_requires_review'):
        from fastapi import HTTPException
        raise HTTPException(409, detail={'status':'needs_review',
            'message':'正文已修订，摘要与时间线完成证据已失效，须复核后再生成',
            'revision':project.plot_timeline['body_revision_requires_review']})
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

    # —— 检索（v2.293：按用途分策略，不再一条 hybrid_search 打天下）——
    #
    # 旧实现：`hybrid_search(db, nid, _rag_query(prior, project), k=4)`。
    # 实测两个问题：
    #
    # 1. **分数无区分度**：RRF 只看排名（1/(60+rank)），BM25 的 24.7 与 17.6
    #    融合后只差 6%。用自造人名当 query（「沈砚跟周郎中走出破庙」）时，
    #    命中第 554/545/529 这类无关章节，分数 0.023-0.027 全靠噪声排序。
    # 2. **没有位置概念**：续写要的是「紧接着当前剧情位置的前文」，
    #    这不需要相似度，直接按章节号取最准。
    #
    # 现在分三种用途（见 app/retrieval/strategies.py）：
    #   续写      → 位置检索为主 + 语义检索补闲笔
    #   角色聊天  → 按人名定向检索（角色自己的原话与经历）
    #   原创      → 不检索（rag=False，检索会漏参考书剧情）
    rag_items: list[dict] = []          # 带 role 结构，供分块渲染
    rag_context: list[str] = []          # 兼容旧的纯文本消费者
    _rules = rules_for(profile) if profile else rules_for(DEFAULT_PROFILE)
    _rag_mode = _rules.get("rag")
    from app.generation.visibility import VisibilityPolicy
    # 「写法参考」路径（原创）下参考书整本可见：不存在「故事后续」要防。
    # 续写/角色聊天不在此列 —— 那两条路径下参考书就是用户正在读的那本。
    from app.generation.profiles import normalize_profile as _normalize_profile
    _unbounded = frozenset({_normalize_profile(profile)}) & STYLE_REFERENCE_PATHS
    visibility = VisibilityPolicy(project, novel_ids, unbounded_paths=_unbounded)
    has_generated = bool(prior) or db.scalar(select(Generation.id).where(Generation.project_id == project.id).limit(1)) is not None
    if has_generated:
        visibility.diagnostics["authority"] = "generated_branch"
    if novel_ids and _rag_mode:
        _query = _rag_query(prior, project)
        # Each reference book has its own explicit, fixed boundary.
        for nid in novel_ids:
            try:
                if _rag_mode == "character":
                    # 只处理一次（多本语料时角色检索会重复，下面 break）
                    _who = _roleplay_character_name(project, reminders)
                    hits = (
                        retrieve_for_character_auto(db, project, novel_ids, _who, k=6)
                        if _who else []
                    )
                else:
                    hits = retrieve_for_continuation(
                        db, nid, chapter_hint=visibility.cutoffs.get(nid), query=_query,
                        k_pos=3, k_sem=2,
                    )
                rag_items.extend(hits)
            except Exception as exc:  # noqa: BLE001 - 单本检索失败不阻断其余语料续写
                visibility.diagnostics["warnings"].append(f"novel_id={nid}: retrieval_failed:{type(exc).__name__}")
                continue
        from app.retrieval.views import prepare_hits
        rag_items = visibility.filter_hits(prepare_hits(rag_items), "rag")
        if visibility.diagnostics["authority"] != "reference_anchor" or prior:
            rag_items = [{**h, "role": "ambience"} if h.get("role") == "position" else h for h in rag_items]
        # rag_context 是**旧字段**，语义 = 「剧情检索上下文」。
        # 角色聊天填的是角色定向检索结果，不是剧情检索，所以不写进这里——
        # 否则 apply_profile 会把它当剧情上下文，且下游按「参考素材」渲染
        # 会把角色原话当成剧情参考塞进正文。
        if _rag_mode != "character":
            rag_context = [h["text"] for h in rag_items if h.get("text")]

    # RAG 结果可能含反向禁忌词（原文确实用了这些词，但反向禁忌禁止生成时使用）。
    # 不删除 RAG 结果（它们是相关剧情素材），但在注入 prompt 时标注清楚。
    narrative = visibility.narrative(_style_narrative(db, project))
    # —— 角色档案的证据回填（老档案没有 claim 账本 ⇒ 整块渲染成空串）——
    # 回填放在 visibility 之后：它要改的是 characters 层，而 visibility 已按路径
    # 裁剪过（原创路径不给 characters 层 ⇒ 这里也就没有可回填的东西）。
    narrative = _backfill_character_evidence(db, project, narrative)
    # —— 人物塑造模板（独立产物，按档位存）——
    #
    # 只有当模板的档位与**这份档案的档位**一致时才注入：拿 A 档的方法论
    # 配 B 档的档案是静默的不一致，比没有模板更糟。
    if isinstance(narrative, dict):
        try:
            from app.style.character_template import applied_template_rules
            _tier = str(narrative.get("tier")
                        or getattr(project, "narrative_tier", "") or "")
            _rules = applied_template_rules(db, project, _tier)
            if _rules:
                narrative = {**narrative, "character_template": {"rules": _rules}}
        except Exception:  # noqa: BLE001 - 模板是增强，不该拖垮生成
            pass
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
        db, project, novel_ids, exclude_texts=set(rag_context), visibility=visibility,
        exclude_chunk_ids={src["chunk_id"] for h in rag_items for src in h.get("sources", [h])
                           if src.get("chunk_id") is not None}
    ) if novel_ids else []

    style_exemplars = visibility.filter_hits(style_exemplars, "exemplar")

    # 角色聊天：写法样板只留**被扮演角色**说过的话。
    # 这一步以前**从来没有过** —— `ctx_profile` 按 `character_filtered` 过滤，
    # 而那个标记上游没设过 ⇒ 角色聊天的写法样板恒为空（实测 0 条）。
    #
    # 顺序很要紧：**先按角色检索定位他出场的块，再从这些块里取他的台词**。
    # 不能「随机采样 + 按名字筛」—— 实测采出的 3 块里一块都没有他
    # （中文里他常被「缺胳膊的老马夫」代称，名字都不出现）。
    _prof_norm = _normalize_profile(profile)
    if _prof_norm == "roleplay":
        try:
            from app.style.exemplar import exemplars_from_character_hits

            _who = _roleplay_character_name(project, reminders)
            _char_ex = exemplars_from_character_hits(rag_items, _who or "")
            if _char_ex:
                style_exemplars = _char_ex
        except Exception as exc:  # noqa: BLE001 —— 过滤失败不该阻断生成
            logging.getLogger(__name__).warning("角色样例构建失败: %s", exc)

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

    # v2.277：原创模式需要「专名表」来脱敏风格样例——原文样例是句长唯一
    # 有效的约束（实测：不给样例 14.6 字/句，给原文 18.0，而参考是 60.3），
    # 但原文自带世界观（大纲写「北漠」正文写「北莽」）。
    # 只有需要脱敏时才加载，续写/角色聊天不付这个成本。
    from app.generation.ctx_profile import apply_profile
    from app.generation.profiles import normalize_profile  # rules_for 已在模块级导入
    _prof = normalize_profile(profile)
    _entity_table: dict = {}
    if rules_for(_prof).get("style_exemplars") == "redacted" and novel_ids and style_exemplars:
        try:
            from app.style.entity_store import ensure_table
            # 缺表/表过期时**在这里建**（PMI 召回 + LLM 判定）——只发生一次，之后零成本。
            # 以前只 load_table：建表器没有调用点，专名表永远是空的 → 脱敏是空操作 →
            # 参考原文（带专名）直接进提示词。
            _entity_table = ensure_table(db, int(novel_ids[0]))
        except Exception as exc:  # noqa: BLE001 - 建表失败不该阻断生成
            import logging as _lg
            _lg.getLogger(__name__).warning("专名表构建失败，样例将不脱敏: %s", exc)

    _warnings: list[str] = []
    _ctx = apply_profile({
        "reminders": reminders,
        "prior_summaries": prior,
        "prior_blocks": prior_blocks,
        "prior_report": prior_report,
        "rag_context": rag_context,
        # v2.293：带 role 的检索结果，供 prompt 分块渲染。
        # 与 rag_context 并存：raw_text 消费者（旧接口、前端预览）读前者的
        # 字符串列表，prompt 组装读后者的结构化块。
        "rag_items": rag_items,
        "rag_blocks": render_retrieval_blocks(rag_items) if rag_items else [],
        "rag_has_forbidden": rag_has_forbidden,
        "style_exemplars": style_exemplars,
        "novel_ids": novel_ids,
        "constraints": constraints,
        "kg_slice": _kg_slice_text(db, project),
        "entity_table": _entity_table,
    }, profile, warnings=_warnings)
    _ctx["style_narrative"] = narrative
    _ctx["visibility_diagnostics"] = visibility.diagnostics
    _ctx["profile_warnings"] = _warnings + visibility.diagnostics["warnings"]
    return _ctx


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


def _prev_scene_hint(db, project: Project) -> str | None:
    """上一节的主体场景类型，用于本节的「必须换场景」约束。

    ## 为什么是整节而不是尾部

    粘滞的判断要看整节：实测第 3/4/5 章整章都在船上，
    只取尾部 600 字会抓到「刀锋」「弓手」这类收尾打斗的物件，
    反而看不出「整章都在水里」。

    ## 为什么用场景类型而不是关键词

    抽关键词试过两条路都失败（字符 n-gram 没有分词，产出「一声」「出一」
    这类跨词碎片；按首字聚合更糟）。场景分类器是密度判据 + 六类专属
    空间物件名词，实测能把三章稳定判成同一类「对峙/冲突」——
    这个「同类」本身就是粘滞信号，直接告诉模型「换一类」比给一串
    猜出来的名词更可用。

    零 LLM。返回 None 表示没有上一节（首段）或判不出类型。
    """
    from app.style.narrative import dominant_scene

    last = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no.desc())
        .limit(1)
    ).first()
    if not last or not (last.text or "").strip():
        return None
    return dominant_scene(last.text or "")


def _l2_character_names(project: Project, ctx: dict, profile: str | None) -> list[str]:
    """取本次 prompt 可见的角色名，作为 L2 档案的严格白名单。"""
    from app.generation.profiles import normalize_profile
    mode = normalize_profile(profile)
    if mode == "original":
        return []
    entries = (ctx.get("style_narrative") or getattr(project, "style_narrative", None) or {}).get("characters") or []
    if mode == "roleplay":
        name = _roleplay_character_name(project, ctx.get("reminders"))
        return [name] if name else []
    names: list[str] = []
    bible = project.plot_bible or {}
    for item in (bible.get("characters") or []):
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    reminders = ctx.get("reminders") or {}
    for item in (reminders.get("character_constraints") or []):
        name = item.get("name") if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    if mode == "continuation":
        # 续写还应看见参考书角色：只选本节上下文实际提及者，不把整层人物表
        # 倒进 prompt。这样参考主角即便不在用户原创 bible 里也能接入档案。
        context_text = "\\n".join(str(x) for x in (
            ctx.get("prior_blocks") or "",
            *(ctx.get("prior_summaries") or []),
            project.outline or "",
        ))
        for entry in entries if isinstance(entries, list) else []:
            name = str(entry.get("name") or "").strip() if isinstance(entry, dict) else ""
            if name and name in context_text:
                names.append(name)
    known_l2 = {
        str(entry.get("name") or "").strip()
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict) and str(entry.get("name") or "").strip()
    }
    if mode == "continuation":
        names = [name for name in names if name in known_l2]
    from app.style.narrative import _is_fake_character
    return list(dict.fromkeys(
        name for name in names if not _is_fake_character(name)
    ))


def _scene_contract_for_generation(project: Project, ctx: dict, profile: str | None, written: int) -> str | None:
    """Build an optional, current-scene contract without an extra LLM call."""
    from app.generation.profiles import normalize_profile
    import os
    if normalize_profile(profile) != "continuation" or os.environ.get("SCENE_CONTRACT_ENABLED") != "1":
        return None
    from app.generation.scene_contract import build_scene_contract
    scene_type = ctx.get("prev_scene") or "未判定场景"
    outline = project.outline or []
    goal = outline[written] if isinstance(outline, list) and written < len(outline) else None
    names = _l2_character_names(project, ctx, profile)
    return build_scene_contract(
        scene_type=scene_type, current_goal=goal,
        previous_scene=ctx.get("prev_scene"), allowed_characters=names,
        outline=str(goal or ""),
    )


def _backfill_character_evidence(db, project: Project, narrative: dict | None) -> dict | None:
    """给老角色档案回填证据，让它**真的能进提示词**。

    ## 为什么需要

    `render_character_profile` 的门禁要求
    `evidence_status == "located_extracts_not_semantic_proof"` ——
    这个字段只有**带 claim 账本的新档案**才有。老档案缺这个键 ⇒
    默认值 `unverified_legacy` ⇒ **整个角色档案渲染成空串**。

    实测（《北荒刀歌》，8 个角色）：回填前 `render_character_profile` 返回
    **空字符串**；回填后是 **400 字/角色**（限长后），身份、说话方式、
    行为倾向、底线、关键经历**全在**，还附上了**原文摘录**（= 配合检索）。

    ## 为什么不是「直接放行」

    门禁的原意是对的：没核过的声明不该进提示词。老档案的字段值是**改写过的总结**，
    claim 级那道判据（value 必须逐字出现在摘录里）核不了。
    ⇒ 回填走**字段级事实核验**：字段值里的事件短语能不能在**有他出场的行**里
    定位到（详见 `style/bio_evidence.py`）。核不上的字段**丢掉**。

    零模型调用；按 (novel_id, 档案指纹) 进程内缓存（实测北荒刀歌 2.6s，缓存后 0）。
    """
    if not isinstance(narrative, dict):
        return narrative
    chars = narrative.get("characters") or []
    if not chars:
        return narrative
    # 新档案自带账本 ⇒ 不需要回填
    if all(c.get("evidence_status") == "located_extracts_not_semantic_proof"
           for c in chars if isinstance(c, dict)):
        return narrative
    novel_ids = list(getattr(project, "reference_novel_ids", None) or [])
    if not novel_ids:
        return narrative
    try:
        from app.style.bio_evidence import backfill_profiles

        for nid in novel_ids:
            filled = backfill_profiles(db, nid, chars)
            if filled:
                narrative = dict(narrative)
                narrative["characters"] = filled
                break
    except Exception as exc:  # noqa: BLE001 —— 回填失败不该阻断生成
        logging.getLogger(__name__).warning("角色档案证据回填失败: %s", exc)
    return narrative


def _style_narrative(db, project: Project) -> dict | None:
    """项目级叙事档案（LLM 提炼），按指纹缓存，整轮恒定。

    与 `_style_profile` 同理：叙事档案进的是静态 system 块，每次生成都变会击穿缓存。
    取不到就返回 None —— 叙事档案是增强，不是生成的前提。
    """
    # Generation consumes an explicitly prepared cache only; reads never distill.
    cached = getattr(project, "style_narrative", None)
    return copy.deepcopy(cached) if isinstance(cached, dict) else None


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
#: bucket 留出分布 max 仍小于生成段 distance（30 章大靖 long bucket 留出 5
#: 单元 max ~ 1.5，段 2 distance 6.461 远超）。阈值从 97.5 提到 99.5：
#: (1) 与 verify_stylometry 离线验收保持同向，但放更宽——bucket 留出样本
#: 数少是工程边界，不应让 100% 触发常态化；(2) 高于 99.5% 才是真异常
#: （参考语料历史样本里都没有这种程度的距离）。
#: 真要回到 97.5 阈值，需参考 50+ 章 30 万字让 long bucket 留出 20+ 单元
#: —— 留出分布 max 自然扩大。详见 docs/style_log_buckets_design.md 风险评估节。
STYLE_WARN_PERCENTILE = 99.5

#: 留出样本不足时的绝对距离阈值（v2.231）。
#:
#: `style_distance` 在留出单元 < 20 时报 percentile=None（分位被粗化到无意义）。
#: 此时改用 mean_abs_z 绝对值判定，否则等于把告警关掉——AI 腔文本也会放过。
#:
#: 阈值 3.0 的依据（豆棚闲话 11.2 万字 / 9 个留出单元的实测分离度）：
#: - 同源 37 个片段：min 0.467 / p50 0.719 / p90 0.865 / max 1.087
#: - AI 腔样本：等长段+升华 6.05 / 短句刷屏 12.07 / 情绪直陈 14.33 / 比喻+感叹号 36.91
#: 两者相差 5.6 倍，3.0 取在同源 max 的 2.8 倍处——留足正常波动余量，
#: 同时能抓住最弱的一类 AI 腔（等长段+升华 6.05）。
STYLE_WARN_ABS_Z = 3.0
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


def _audit_and_repair_scene(text: str, provider, *, reference_texts: list[str] | None = None, enabled: bool | None = None) -> tuple[str, dict]:
    """Audit and locally repair one generated scene; never rewrite the whole text."""
    import os
    if enabled is None:
        enabled = os.environ.get("SCENE_CONTRACT_ENABLED") == "1"
    if not enabled or not text:
        return text, {"metrics": {}, "violations": [], "repair_count": 0}
    from app.generation.scene_contract import audit_scene_text, build_local_repair_prompt, apply_repaired_spans, derive_scene_budgets
    rag_samples = list(reference_texts or [])
    budgets = derive_scene_budgets(rag_samples, target_chars=len(text))
    audit = audit_scene_text(text, budgets=budgets)
    audit["mystery_measure"] = "reveal_cue_proxy_not_semantic_fact_count"
    spans = audit.get("repair_spans") or []
    if not spans:
        audit["repair_count"] = 0
        return text, audit
    import re
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    repairs = []
    for span in spans[:1]:
        local = "\n\n".join(paragraphs[span["start"]:span["end"]])
        system, user = build_local_repair_prompt(
            local, audit, immutable_facts=[], preserve_ending=""
        )
        try:
            repaired = provider.chat(system, user)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("局部文风修复失败，保留初稿: %s", exc)
            continue
        if (repaired and max(20, len(local) * 0.7) <= len(repaired) <= len(local) * 1.5
                and paragraphs[-1] not in repaired
                and not any(tag in repaired for tag in ("```", "【本节完】", "以下是"))):
            repairs.append({"text": repaired})
    if not repairs:
        audit["repair_count"] = 0
        return text, audit
    repaired_text = apply_repaired_spans(text, repairs, spans[:len(repairs)])
    # Never accept a local edit merely because the repair model returned text.
    # The repair must reduce the measured violation score; otherwise keep the
    # original draft (the model may trade fewer questions for more dialogue or
    # add new mystery cues).
    after = audit_scene_text(repaired_text, budgets=budgets)
    def _score(result: dict) -> tuple[int, float]:
        metrics = result.get("metrics") or {}
        excess = 0.0
        for key, limit in budgets.items():
            value = float(metrics.get(key, 0))
            excess += max(0.0, value - float(limit)) / max(float(limit), 0.01)
        return (len(result.get("violations") or []), excess)
    if _score(after) >= _score(audit):
        audit["repair_count"] = 0
        audit["repair_rejected"] = True
        return text, audit
    audit["repair_count"] = len(repairs)
    audit["repaired"] = repaired_text != text
    audit["after_metrics"] = after.get("metrics")
    audit["after_violations"] = after.get("violations")
    return repaired_text, audit


def _style_warning(text: str, profile: dict | None) -> dict | None:
    """对生成段落算一次风格距离，percentile 高于阈值时返回告警，否则 None。

    告警只提示不阻断——自动重写会触发额外 LLM 调用，且改出来的未必更好。
    无档案时静默跳过（档案是增强，不是生成的前提）。

    ## 两条判定路径（v2.231）

    **主路径 percentile**：留出单元足够（≥20）时，分位是可判读的结论。

    **兜底路径绝对 z**：留出单元不足时 `style_distance` 报 percentile=None
    （分位被粗化，9 个样本只能给每档 11.1%，且超过最大值即 100%）。
    此时若不判定就等于关掉了告警功能——AI 腔文本也放过去。改用绝对阈值：

    实测（豆棚闲话 11.2 万字，9 个留出单元）：
    - 同源 37 个片段 mean_abs_z: min 0.467 / p50 0.719 / max **1.087**
    - AI 腔样本：等长段+升华 6.05 / 短句刷屏 12.07 / 情绪直陈 14.33 /
      比喻+感叹号 36.91
    分离度 5.6 倍，阈值取 3.0（同源 max 的 2.8 倍，最弱 AI 腔的 0.5 倍）。
    """
    if not profile or not text:
        return None
    try:
        from app.style import style_distance
        d = style_distance(text, profile)
    except Exception:  # noqa: BLE001 —— 距离计算失败绝不能阻断生成主流程
        return None
    pct = d.get("percentile")
    z = d.get("mean_abs_z")

    if pct is not None:
        if pct < STYLE_WARN_PERCENTILE:
            return None
        reason = f"percentile {pct}，高于阈值 {STYLE_WARN_PERCENTILE}"
        pct_for_payload = pct
    else:
        # 兜底：留出样本不足，分位不可用 → 绝对距离判
        if z is None or z < STYLE_WARN_ABS_Z:
            return None
        reason = f"mean_abs_z {z}，高于绝对阈值 {STYLE_WARN_ABS_Z}（留出样本不足，分位不可用）"
        pct_for_payload = None

    return {
        "percentile": pct_for_payload,
        "mean_abs_z": z,
        "worst": d.get("worst", []),
        "holdout_ref": d.get("holdout_ref"),
        "message": (
            f"本段文风偏离档案（{reason}）。"
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
        if provider is not None and db is not None:
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
    # 存档是权威；读取和预览不得用段号推测完成或重复补计段数。

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


#: timeline 顶层**不属于**时间线 state 的键 —— 它们由别的子系统记账，
#: 写回 state 时**不能连坐清除**：
#:   pending_acceptance          全局验收义务（换绑不删除，见 agents/proposals.py）
#:   derived_state / derived_rebuild  派生数据（KG/Fact/摘要）的重建状态
#:   body_revision_requires_review   正文修订守卫（只能由解锁清除）
#:   inventory_binding 等其它子系统的记账
_TIMELINE_NON_STATE_KEYS = (
    'pending_acceptance', 'derived_state', 'derived_rebuild',
    'body_revision_requires_review', 'inventory_binding',
)


def _persist_timeline_state(db, project: Project, st: TimelineState) -> None:
    """把 state 序列化写回 project.plot_timeline。失败也不抛（generate 主流程不挂）。

    **只覆盖 state 自己拥有的键**，别的子系统的记账原样留着。
    之前这里是整体覆盖（`project.plot_timeline = state_to_dict(st)`），
    于是正文一生成，验收义务与派生状态就被时间线**连坐清除** ——
    下一轮整段验收会以 `no_pending_acceptance` 拒绝，验收义务凭空消失。
    """
    try:
        state = state_to_dict(st)
        preserved = {k: v for k, v in (project.plot_timeline or {}).items()
                     if k in _TIMELINE_NON_STATE_KEYS and k not in state}
        project.plot_timeline = {**preserved, **state}
        # Some callers pass a detached Project while context loading has already
        # loaded that identity in this Session. Merge rather than silently losing
        # timeline evidence on an identity-conflict rollback.
        db.merge(project)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _cjk_count(text: str) -> int:
    """汉字数（标点、空格、换行不算）。段长要求的计量单位。"""
    return sum(1 for ch in (text or '') if '一' <= ch <= '鿿')


def _enforce_length(provider, system: str, user: str, text: str, *, target_chars: int,
                    constraints=None, stop_sequences=None, node_prompt: str | None = None,
                    max_attempts: int = 2):
    """段长闸门：不够长就**回灌让模型扩写**（有界），并如实报告实际长度。

    为什么要有这道闸门：长度以前只是提示里的一句「软希望」，实测同一段要求
    3000 汉字、模型交出 1900，系统照收不误 —— 验收口径与产出口径对不上。
    现在提示的单位已经是「汉字」，这里量的是同一个单位。

    目标 = 配置的段长（汉字）；下限 = 目标的 80%（既有标定）。
    达不到目标 → 回灌扩写（最多 max_attempts 次）；仍不到下限 → 照写，但如实标注
    「未达标」（这个剧情点本身可能就短，硬拒会卡死合理的收尾节点）。
    """
    floor = int(target_chars * 0.8)
    report = {'target_cjk': target_chars, 'floor_cjk': floor,
              'cjk': _cjk_count(text), 'attempts': 1, 'below_target': False}
    for attempt in range(1, max_attempts + 1):
        count = _cjk_count(text)
        report['cjk'] = count
        if count >= target_chars:
            break
        report['below_target'] = True
        if attempt > max_attempts:
            break
        hint = (
            f"\n\n—— 上一版长度不够（{count} 汉字，目标 {target_chars} 汉字）——\n"
            f"请把这一段写到 **{int(target_chars * 1.2)} 汉字以上**"
            "（同一剧情点范围内）：补足场景细节、动作过程、"
            "对白与人物反应、观察与判断的来回，而不是把剧情推进到下一个点。"
            "仍然只写这个剧情点、不要引入本剧情点之外的人物与物件。")
        try:
            grown = provider.chat(system, user + hint, constraints=constraints,
                                 stop_sequences=stop_sequences)
        except Exception:  # noqa: BLE001 - 扩写失败就保留原版，不阻断生成
            break
        grown = _strip_thinking(grown)
        if not grown or _cjk_count(grown) <= count:
            break                       # 没变长就别换了
        text = grown
        report['attempts'] = attempt + 1
    report['cjk'] = _cjk_count(text)
    report['below_target'] = report['cjk'] < target_chars
    report['below_floor'] = report['cjk'] < floor
    return text, report


def _plan_exhausted(project: Project) -> bool:
    """这个项目**有规划**，而且规划里的剧情点已经全部写完。

    没有规划的项目（自由续写）不算 —— 那种情况下继续写是正常的。

    口径注意：契约是按**段号**存的（segment_contracts 的键是 '1'、'2'…），
    而 `done` 存的是**节点 id**（'n1'、'n2'…）。两边不是同一个命名空间，
    必须先从契约里取出 node_id 再比。
    """
    timeline = project.plot_timeline or {}
    if isinstance(timeline, str):
        try:
            timeline = json.loads(timeline)
        except (TypeError, ValueError):
            return False
    planning = (timeline or {}).get('planning') or {}
    if isinstance(planning, str):
        try:
            planning = json.loads(planning)
        except (TypeError, ValueError):
            return False
    contracts = (planning or {}).get('segment_contracts') or {}
    nodes = (planning or {}).get('nodes') or {}
    if not contracts and not nodes:
        return False                      # 没有规划 → 自由续写，不拦

    planned_nodes: set[str] = set()
    # 计划里的**节点全集**（不是「已经被段绑定过的节点」）：
    # 只看契约会漏掉「规划里有、正文还没写到」的节点 —— 实测三段全绑在 n1，
    # 引擎因此以为「规划写完了」，而 n2/n3 根本没写过（正文永远走不到它们）。
    if isinstance(nodes, dict):
        planned_nodes |= {str(k) for k in nodes.keys()}
    elif isinstance(nodes, list):
        planned_nodes |= {str(n.get('id')) for n in nodes
                          if isinstance(n, dict) and n.get('id')}
    if not planned_nodes and isinstance(contracts, dict):
        for contract in contracts.values():
            if isinstance(contract, dict) and contract.get('node_id'):
                planned_nodes.add(str(contract['node_id']))
    elif isinstance(contracts, list):
        for contract in contracts:
            if isinstance(contract, dict) and (contract.get('node_id') or contract.get('id')):
                planned_nodes.add(str(contract.get('node_id') or contract.get('id')))
    if not planned_nodes:
        # 退化：state 序列化出来的 nodes 是 dict（键=节点 id）
        if isinstance(nodes, dict):
            planned_nodes = {str(k) for k in nodes.keys()}
        elif isinstance(nodes, list):
            planned_nodes = {str(n.get('id')) for n in nodes
                             if isinstance(n, dict) and n.get('id')}
    if not planned_nodes:
        return False
    done = {str(x) for x in (timeline.get('done') or [])}
    return planned_nodes <= done


def _rebind_acceptance_after_generation(db, project: Project) -> None:
    """生成了一段正文 → 全局验收义务要**换绑**到这一版（不删除）。

    正文一改，上一版的整段验收就不再覆盖它 —— 但义务还在：
      pending_acceptance.state         'accepted' → 'carried_forward'
      pending_acceptance.bound_body_hash  → 当前正文指纹
      derived_state                    作废（引擎只做增量 KG 合并，
                                        不是解锁要求的全量重建）
    """
    try:
        timeline = dict(project.plot_timeline or {})
        pending = timeline.get('pending_acceptance')
        if pending:
            from app.agents.body_acceptance import body_fingerprint
            from app.models import Generation
            rows = list(db.scalars(select(Generation).where(Generation.project_id == project.id)
                                   .order_by(Generation.segment_no, Generation.id)))
            pending = dict(pending)
            pending['state'] = 'carried_forward'
            pending['bound_body_hash'] = body_fingerprint(rows)
            timeline['pending_acceptance'] = pending
        derived = timeline.get('derived_state')
        if derived:
            timeline['derived_state'] = dict(derived, kg='pending_rebuild', facts='pending_rebuild')
        if pending or derived:
            project.plot_timeline = timeline
            db.commit()
    except Exception:  # noqa: BLE001 - 换绑失败不该让生成主流程挂
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


def _prev_segment_state(db, project: Project, segment_no: int, limit: int = 320) -> str:
    """上一段收尾时的现场（摘要 + 结尾引文）——多段接力提示用。

    为什么需要它：同一个节点跨多段时，每一段拿到的都是**节点入口状态**，
    模型于是从入口重演开场（实测第三段重新写了一遍「进门」），
    或者断言与上一段冲突的状态（第二段已写下登记，第三段说「仍然是空白」）。
    把上一段真实的收尾现场摆到模型面前，接力才接得上。
    """
    if db is None or segment_no <= 1:
        return ''
    from app.models import Generation
    row = (db.query(Generation)
             .filter_by(project_id=project.id, segment_no=segment_no - 1)
             .order_by(Generation.id.desc()).first())
    if row is None:
        return ''
    text = (row.text or '').strip()
    summary = row.summary
    if isinstance(summary, dict):
        summary = summary.get('text') or ''
    summary = (summary or '').strip()
    parts = []
    if summary:
        parts.append(f'上一段摘要：{summary}')
    if text:
        parts.append(f'上一段结尾：…{text[-limit:]}')
    return '\n'.join(parts)


def _continuation_context(db, project: Project, st, segment_no: int) -> dict | None:
    """当前节点已经写过段时，给出「接着上一段继续」的接力上下文。"""
    node_id = st.current_node
    if not node_id or node_id not in st.nodes:
        return None
    seg_count = int(st.seg_counts.get(node_id) or 0)
    if seg_count <= 0:
        return None
    prev_state = _prev_segment_state(db, project, segment_no)
    if not prev_state:
        return None
    return {'segment_index': seg_count + 1,
            'segment_budget': int(st.nodes[node_id].segment_budget or 0),
            'prev_state': prev_state}


def _judge_and_advance(
    db, project: Project, provider, recent_text: str, written_segments: int
) -> dict:
    """v2.1：commit 段后调一次 LLM 判定当前节点 exit_condition 是否达成。

    - supported 且证据有效：force_complete_current → 落库
    - 非 supported：仅计数，预算用尽进入 needs_review，不自动 done
    - 缺条件、异常、无证据：记录 unknown
    - 没 outline / state 不可用：返回 skipped=True，不动 state

    返回 dict 供 API 透传给前端（诊断/进度展示用）。
    预算与完成语义分离；引文来源和偏移经过程序核验。
    """
    out = {"called": False, "judge": None, "forced": False, "advanced": False, "skipped": True}
    if not project.outline:
        return out
    st = _timeline_state_for(project, written_segments, provider=None, db=None)
    if st is None or not st.current_node or st.current_node not in st.nodes:
        return out
    if st.last_judged_segment >= written_segments + 1:
        return dict(out, reason="already_judged")
    cur_id = st.current_node
    cur_node = st.nodes[cur_id]
    # Bind each committed segment to its actual contract before advancing.
    # Regeneration must not consume the next node's time/location/state.
    st.planning = dict(st.planning)
    contracts = dict(st.planning.get('segment_contracts') or {})
    from copy import deepcopy
    causal_nodes = st.planning.get('nodes') or []
    causal_index = st.order.index(cur_id) if cur_id in st.order else -1
    contracts[str(written_segments + 1)] = {
        'node_id': cur_id,
        # 契约里冻结的提示必须与**真正下发给模型的那份**一致 ——
        # 权威要跟着提示一起冻结（权威不在提示里，模型就看不见什么被允许：
        # 实测一次真实生成凭空造出 80 处清单外物件，整段验收全判成违例）。
        'prompt': build_node_prompt(cur_node, st.current_beat, timeline_progress(st),
                                     authority=project.premise,
                                     continuation=_continuation_context(
                                         db, project, st, written_segments + 1)),
        # Freeze author constraints that may not survive the outline projection.
        # Historical diagnostics must never substitute the current premise.
        'authority': {'premise': project.premise,
                      'causal_node': deepcopy(causal_nodes[causal_index])
                      if 0 <= causal_index < len(causal_nodes) else None},
    }
    st.planning['segment_contracts'] = contracts

    # 1) 跑正常 advance（不管 LLM 怎么判，段数 +1）
    # 只按本次已提交的段计数，禁止读取时补进度。
    # 状态恢复不补计段数，重复判定由 last_judged_segment 拦截。
    # written_segments 是提交前的段数。
    # 当前调用只为刚提交段增加一次计数。
    st = advance(st)
    out["advanced"] = True
    out["called"] = True
    out["skipped"] = False

    # 无论预算是否耗尽，都对本次正文执行证据判定。
    if cur_id not in st.done:
        out["judge"] = judge_node_completion(provider, cur_node, recent_text,
                                               source_id=f"generation:{written_segments + 1}")
        st.completion[cur_id] = out["judge"]
        st.last_judged_segment = written_segments + 1
        if out["judge"]["verdict"] == "supported":
            st = force_complete_current(st)
            out["forced"] = True
        else:
            # 预算用尽但退出条件未证实 → **记为待复核**（不自动跳节点：
            # 要不要放弃这个节点是作者的决定，代码不替他做）。
            # 原来这里什么都不做：除了 current_node 留在本节点、生成入口 409，
            # 这个缺口在**任何记录里都看不到** —— 正文与规划的冲突就这么消失了。
            # 现在把它记进 planning.node_needs_review：
            # 验收的「规划 vs 正文」门禁会把它报出来，作者能看见缺了什么。
            budget = int(cur_node.segment_budget or 0)
            if budget and int(st.seg_counts.get(cur_id) or 0) >= budget:
                st.planning = dict(st.planning)
                needs = list(st.planning.get('node_needs_review') or [])
                if cur_id not in needs:
                    needs.append(cur_id)
                st.planning['node_needs_review'] = needs
                out['needs_review_node'] = cur_id

    # 3) 落库（用新 state 覆盖）
    _persist_timeline_state(db, project, st)
    out["current_node"] = st.current_node
    out["done"] = sorted(st.done)
    out.update(timeline_progress(st))
    return out


def current_node_prompt(
    project: Project, written_segments: int, provider=None, db=None
) -> str | None:
    """算出当前剧情点的提示块（时间线启用时）；无 outline 返回 None。

    供 generate_next_segment 与 generate-plan 共用——两条路径必须下发同一份提示，
    否则预检看到的不算数。written_segments = 已写段数（_next_segment_no - 1）。
    `db` 用于持久化；预检/读侧可不传（仅恢复，不写）。
    """
    had_saved_plan = bool(project.plot_timeline)
    st = _timeline_state_for(project, written_segments, provider, db=db)
    if st is None or not st.current_node or st.current_node not in st.nodes:
        return None
    status = timeline_progress(st)["status"]
    if db is not None and provider is not None and had_saved_plan and status == "needs_planning" and not st.seg_counts:
        filled = plan_nodes_with_llm(provider, project.outline, project.plot_bible, st.total_main)
        if filled:
            for node in filled:
                node.segment_budget = st.nodes[node.id].segment_budget
                st.nodes[node.id] = node
            _persist_timeline_state(db, project, st)
            status = timeline_progress(st)["status"]
    if db is not None and status in {"needs_planning", "needs_review"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail={
            "status": status, "current_node": st.current_node,
            "message": "节点缺少有效计划，请完善起点和退出条件" if status == "needs_planning" else
                       "节点预算已用尽且退出条件尚未证实，请检查正文或调整计划/预算",
            "completion": st.completion.get(st.current_node),
        })
    return build_node_prompt(
        st.nodes[st.current_node], st.current_beat, timeline_progress(st),
        authority=_current_authority(project, st),
        continuation=_continuation_context(db, project, st, written_segments + 1),
    )


def _contract_authority(contract: dict | None, project: Project) -> str | None:
    """契约里冻结的**保存权威**（含封闭物品清单、人物、地点）。

    取值口径：契约冻结的权威优先，退回项目 premise。
    权威必须与提示同处一份 —— 只在契约里、不进提示，模型就看不见什么被允许。
    """
    if isinstance(contract, dict):
        authority = contract.get('authority') or {}
        if isinstance(authority, dict):
            premise = authority.get('premise')
            if premise:
                return str(premise)
    return project.premise or None


def _current_authority(project: Project, st) -> str | None:
    """当前剧情点对应的**保存权威**（含封闭物品清单/人物/地点）。

    必须进生成提示：模型看不到清单就会凭空造道具（实测一次真实生成造出
    80 处清单外物件，整段验收全判成违例）。
    取值口径：优先用该段的契约里冻结的权威，退回项目 premise。
    """
    written = int(st.last_judged_segment or 0)
    contracts = (getattr(st, 'planning', None) or {}).get('segment_contracts') or {}
    for key in (str(written + 1), str(written)):
        authority = _contract_authority(contracts.get(key), project)
        if authority:
            return authority
    return project.premise or None


def generate_next_segment(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
    profile: str | None = None,
    style_rewrite: bool | None = None,
) -> dict:
    """续写下一段（长文单段）：组装上下文 → 调用 LLM → 段级总结 → 落库 → 更新主线摘要链。

    长文策略：默认一次生成 project.generation_length 字（前端可改），而非多段短循环。
    多段短循环会把前情/大纲反复塞进上下文，既压低缓存命中率又易触发会话压缩；
    改为「用户每点一次生成 = 一次长段」，续写环靠段级总结链（短摘要）衔接，不重复搬运正文。

    `profile` 缺省时按项目的锚定方式推导（见 `profiles.resolve_profile`）：
    锚定在参考书上的项目走续写路径 —— 否则边界填了也没用，参考书原文会被整本挡掉。
    """
    from app.llm.provider import get_llm_provider
    from app.generation.profiles import resolve_profile

    profile = resolve_profile(project, profile)

    if provider is None:
        provider = get_llm_provider()

    # 字数：前端/项目配置优先；未配则回退长文默认 3000，彻底告别 800/1200 的碎段
    if max_chars is None:
        max_chars = project.generation_length or 3000

    # 先把摘要树补齐再组装前情：老项目（步骤 2 之前建的）只有 L1、没有 L2/L3，
    # 直接 compose 会因粗层缺失而退化成全量（虽不留洞，但白吃预算）。
    # 这一步幂等——树已一致时零 LLM 调用，所以常态下没有额外开销。
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids, profile=profile)
    # —— 时间线：算出当前剧情点提示（有 outline 才启用；无则 None 走原逻辑）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
    if node_prompt is None and _plan_exhausted(project):
        # 有规划、但规划已经写完 → **不再写正文**。
        # 规划是权威的分解：节点用完意味着故事走到了权威划定的边界
        # （例：权威写的是「上午09:00至12:00」），再写就既没有契约可依、
        # 也没有保存权威可核对（实测会写出一段「无规划」的正文并自己编造前情）。
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail={
            'status': 'plan_exhausted',
            'message': '规划已写完：这一版的剧情点都写过了。要继续写，请先扩展规划'
                       '（否则新写的正文没有保存权威可核对）。',
            'written_segments': written})
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), ctx.get("style_narrative"),
        profile=profile,
        character_names=_l2_character_names(project, ctx, profile),
        entity_table=ctx.get("entity_table"),
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
        # v2.290：本节段号 + 上节场景要素 → 大纲目标 + 场景推进硬约束。
        # 只在非时间线模式传（时间线模式的 node_prompt 已经精确到剧情点）。
        segment_no=None if node_prompt else (written + 1),
        prev_scene=None if node_prompt else _prev_scene_hint(db, project),
        # v2.293：分块检索上下文（位置块 / 语义块 / 角色块各带标签）
        rag_blocks=ctx.get("rag_blocks") or None,
        scene_contract=_scene_contract_for_generation(project, {**ctx, "prev_scene": _prev_scene_hint(db, project)}, profile, written),
    )

    # v2.6：节点粒度根治——per-request 传 stop token（不用单例 setter，避免跨请求污染）
    # 重写/重生成是 AI 味修复，不传 stop
    #
    # 候选池（Best-of-N / 拒绝采样）：同一提示采 N 份，按「参考书自己的分布」择优。
    # 默认 pool_size=1 → 一次调用都不多，行为与从前完全一致。
    text, pool_info = _generate_with_pool(
        provider, system, user,
        constraints=ctx["constraints"],
        stop_sequences=get_section_stop_sequences(),
        pool_size=_candidate_pool_size(),
        profile=_style_profile(db, project),
        target_chars=max_chars,
    )
    text = _strip_thinking(text)
    # 段长闸门：写完先量**汉字**数，不够就回灌让模型扩写（有界）。
    # 原来长度只是提示里的「软希望」——实测同一段要求 3000 汉字，模型交出 1900，
    # 系统照收不误。提示单位改成汉字之后，这道闸门才量得准。
    text, length_report = _enforce_length(
        provider, system, user, text, target_chars=max_chars,
        constraints=ctx["constraints"], stop_sequences=get_section_stop_sequences(),
        node_prompt=node_prompt)
    text, scene_audit = _audit_and_repair_scene(text, provider, reference_texts=[item.get("text", "") for item in (ctx.get("rag_items") or []) if isinstance(item, dict)])
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

    # v2.273：落库前清段内自我重复（长文复读退化的确定性兜底）
    text, dedup_stats = _dedup_before_commit(text)
    if dedup_stats["removed_paragraphs"]:
        summary = provider.summarize(text)

    # v2.278：两段式文风改写。默认按**闸门**触发 —— 偏离参考书越远的那段
    # 才值得多花一次模型调用（尺子 = 真正注入提示词的那份档案）。
    # 实测：一次性生成 3000 字句长只有 16，而改写已有文本能到 60。
    text, rewrite_info = _maybe_rewrite_style(
        project, text, provider,
        profile=_style_profile(db, project), override=style_rewrite,
    )
    # 形态校正：把对白组织方式掰到参考书自己的形态（闸门 = 参考书的逐单元分布带）
    text, form_info = _maybe_correct_form(
        db, project, text, provider,
        profile=_style_profile(db, project), novel_ids=novel_ids,
    )
    # 正文变了 → 段级摘要要重算（改写与形态校正都可能改了正文）
    # beat 插入：在连续对白段的开头补一句动作/神态叙述。
    # 放在形态校正**之后** —— 形态校正只重排已有的叙述，碰到「前面也是台词」
    # 就并无可并；这一步是**新增**叙述，填的正是那个缺口。
    text, beat_info = _maybe_insert_beats(
        db, project, text, provider, novel_ids=novel_ids,
    )
    if (rewrite_info.get("applied") or form_info.get("applied")
            or beat_info.get("applied")):
        summary = provider.summarize(text)

    # 照搬门禁：正文 vs 参考语料。放在**所有**正文改写之后、落库之前 ——
    # 落库的是最终文本，门禁必须量最终文本。
    # 改不掉就抛 VerbatimViolation（可重试失败），这一段不落库。
    text, verbatim_info = _maybe_fix_verbatim(
        db, project, text, provider, novel_ids=novel_ids,
    )
    if verbatim_info.get("rewritten"):
        summary = provider.summarize(text)

    gen, segment_no = _commit_segment(db, project, text, summary)
    # 正文变了 → 上一版的整段验收**不再覆盖这一版**：义务换绑（不删除），
    # 派生状态作废（引擎只做增量合并，不是解锁要求的全量重建）。
    _rebind_acceptance_after_generation(db, project)

    # —— v2.1：exit_condition LLM 判定 → 必要时提前切节点（§13 待定项落地）——
    # 此时本段刚 commit 完，state 还没 advance（advance 是下次预检时触发的）。
    # 在「commit 之前」的 current_node + 刚生成的正文上判定。
    # 失败/不确定/缺条件只记录 unknown，不由预算推断完成。
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
        "length": length_report,
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
        "form_info": form_info,
        "candidate_pool": pool_info,
        "beat_info": beat_info,
        "verbatim_info": verbatim_info,
    }


def generate_next_segment_stream(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
    profile: str | None = None,
    style_rewrite: bool | None = None,
):
    """流式续写生成器：逐 delta 产出 {type:chunk} 事件，完成后落库并返回 {type:done}。

    与 generate_next_segment 语义一致（组装→续写→总结→落库→更新主线摘要链），
    区别在续写过程逐块 yield，供 SSE 转发——长文首字即显、不干等。

    v2.3：传 `node_prompt` 给 build_user_prompt——流式版也走时间线模式，
    剔除 plot_bible 全字段 + premise。commit 段后调 `_judge_and_advance`
    （与同步版同口径）—— yield 一个 `node_judge` 事件。

    `profile` 缺省时按项目的锚定方式推导（见 `profiles.resolve_profile`），
    与同步版同口径。
    """
    from app.llm.provider import get_llm_provider
    from app.generation.profiles import resolve_profile

    profile = resolve_profile(project, profile)

    if provider is None:
        provider = get_llm_provider()
    if max_chars is None:
        max_chars = project.generation_length or 3000

    # 同 generate_next_segment：先补齐粗层，再拼有界前情（幂等，常态零开销）
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids, profile=profile)
    # —— 时间线：算出当前剧情点提示（与同步版同口径，db 用于落库）——
    written = _next_segment_no(db, project.id) - 1
    node_prompt = current_node_prompt(project, written, provider, db=db)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), ctx.get("style_narrative"),
        profile=profile,
        character_names=_l2_character_names(project, ctx, profile),
        entity_table=ctx.get("entity_table"),
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
        # v2.293：流式版同样要分块——否则两条生成路径的 prompt 不一致，
        # 流式预览与最终结果会对不上。
        rag_blocks=ctx.get("rag_blocks") or None,
        scene_contract=_scene_contract_for_generation(project, {**ctx, "prev_scene": _prev_scene_hint(db, project)}, profile, written),
    )

    # v2.6：节点粒度根治——流式版同步传 per-request stop token
    #
    # 候选池（Best-of-N / 拒绝采样）：流式路径**不能**边生成边择优
    # （择优要看到全貌），所以这里先非流式采 N 份、择优，再把选中那份**分块吐出来**。
    # pool_size=1（默认）时走原来的流式路径，行为完全不变。
    _pool_size = _candidate_pool_size()
    if _pool_size > 1:
        _picked, _pool_info = _generate_with_pool(
            provider, system, user,
            constraints=ctx["constraints"],
            stop_sequences=get_section_stop_sequences(),
            pool_size=_pool_size,
            profile=_style_profile(db, project),
        )
        text = _strip_thinking(_picked)
        # 分块吐出去（前端按 chunk 增量渲染）
        for _i in range(0, len(text), 800):
            yield {"type": "chunk", "text": text[_i:_i + 800]}
    else:
        pieces: list[str] = []
        for piece in provider.stream_chat(system, user, constraints=ctx["constraints"],
                                           stop_sequences=get_section_stop_sequences()):
            pieces.append(piece)
            yield {"type": "chunk", "text": piece}
        text = _strip_thinking("".join(pieces))
        _pool_info = {"applied": False, "reason": "候选池关闭"}
    text, scene_audit = _audit_and_repair_scene(text, provider, reference_texts=[item.get("text", "") for item in (ctx.get("rag_items") or []) if isinstance(item, dict)])
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

    # v2.273：落库前清段内自我重复（长文复读退化的确定性兜底）
    text, dedup_stats = _dedup_before_commit(text)
    if dedup_stats["removed_paragraphs"]:
        summary = provider.summarize(text)

    # v2.278：两段式文风改写。默认按**闸门**触发 —— 偏离参考书越远的那段
    # 才值得多花一次模型调用（尺子 = 真正注入提示词的那份档案）。
    # 实测：一次性生成 3000 字句长只有 16，而改写已有文本能到 60。
    text, rewrite_info = _maybe_rewrite_style(
        project, text, provider,
        profile=_style_profile(db, project), override=style_rewrite,
    )
    # 形态校正：把对白组织方式掰到参考书自己的形态（闸门 = 参考书的逐单元分布带）
    text, form_info = _maybe_correct_form(
        db, project, text, provider,
        profile=_style_profile(db, project), novel_ids=novel_ids,
    )
    # 正文变了 → 段级摘要要重算（改写与形态校正都可能改了正文）
    # beat 插入：在连续对白段的开头补一句动作/神态叙述。
    # 放在形态校正**之后** —— 形态校正只重排已有的叙述，碰到「前面也是台词」
    # 就并无可并；这一步是**新增**叙述，填的正是那个缺口。
    text, beat_info = _maybe_insert_beats(
        db, project, text, provider, novel_ids=novel_ids,
    )
    if (rewrite_info.get("applied") or form_info.get("applied")
            or beat_info.get("applied")):
        summary = provider.summarize(text)

    # 照搬门禁：正文 vs 参考语料。放在**所有**正文改写之后、落库之前 ——
    # 落库的是最终文本，门禁必须量最终文本。
    # 改不掉就抛 VerbatimViolation（可重试失败），这一段不落库。
    text, verbatim_info = _maybe_fix_verbatim(
        db, project, text, provider, novel_ids=novel_ids,
    )
    if verbatim_info.get("rewritten"):
        summary = provider.summarize(text)

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
        "form_info": form_info,
        "candidate_pool": _pool_info,
        "beat_info": beat_info,
        "verbatim_info": verbatim_info,
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
    profile: str | None = None,
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
    ctx = build_context(db, project, novel_ids=novel_ids, profile=profile or "continuation")
    system = build_system_prompt(
        project.title, get_reminders(db, project.id, source="generated"),
        _style_info(project), project.plot_bible, _style_profile(db, project),
        # 重写第 N 章：属于续写模式（写的是原书章节），原书角色声音要保留
        ctx.get("style_narrative"), profile=profile or "continuation",
        character_names=_l2_character_names(project, {**ctx, "prior_summaries": l1_chain(tree), "prior_blocks": blocks}, profile or "continuation"),
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
    profile: str | None = None,
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
    contract = (((project.plot_timeline or {}).get('planning') or {})
                .get('segment_contracts') or {}).get(str(segment_no))
    if not project.outline:
        node_prompt = None
    elif not contract or not contract.get('prompt'):
        from fastapi import HTTPException
        raise HTTPException(409, detail={'status': 'needs_review',
            'message': '旧段缺少原始节点契约，请明确复核后重生成，不能使用当前节点替代'})
    else:
        # 契约提示在冻结时就可能带了权威（幂等函数会识别标记，不重复挂）。
        # 老契约（冻结时还没把权威写进提示）在这里补挂 —— 否则重生成会退回
        # 「模型看不见清单」的老口径，凭空造出清单外物件。
        node_prompt = with_authority(contract['prompt'],
                                     _contract_authority(contract, project))

    # 前情 = 只取该段之前的树（截断时若 L3 混入了被替换内容，会自动丢弃并重建）
    system, user, max_chars = _build_generation_prompts(
        db, project, before_segment=segment_no, novel_ids=novel_ids,
        max_chars=max_chars, provider=provider, node_prompt=node_prompt,
        profile=profile,
    )
    # constraints 与正常续写同源（角色/伏笔的锁定名单不能因重生成而变），只算一次
    constraints = build_context(db, project, novel_ids=novel_ids, profile=profile)["constraints"]

    pieces: list[str] = []
    for piece in provider.stream_chat(system, user, constraints=constraints):
        pieces.append(piece)
        yield {"type": "chunk", "text": piece}

    text = _strip_thinking("".join(pieces))
    # 段长闸门：重生成与正常续写**同一把尺**。原来只有续写有闸门，重生成写多短
    # 都照收 —— 验收口径（每段 ≥3000 汉字）与产出口径对不上。
    text, length_report = _enforce_length(
        provider, system, user, text, target_chars=max_chars,
        constraints=constraints, node_prompt=node_prompt)
    summary = provider.summarize(text)

    target.text = text
    target.summary = {"text": summary}
    target.chapter_range = f"段{segment_no}"

    # 重建整棵摘要树：该段所在的卷摘要与主线骨架都基于旧内容，不能续滚
    tree_stats = rebuild_summary_tree(db, project, provider)
    db.refresh(target)
    # 正文被换掉 → 上一版的整段验收不再覆盖它：义务换绑（不删除），派生状态作废。
    # 少了这一步，重生成之后验收会拿旧指纹去比，判成 stale_body_fingerprint。
    _rebind_acceptance_after_generation(db, project)

    yield {
        "type": "done",
        "segment_no": segment_no,
        "chars": len(text),
        "cjk": length_report.get("cjk"),
        "length_report": length_report,
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
    profile: str | None = None,
    style_rewrite: bool | None = None,
) -> list[dict]:
    """连续续写（默认单段）。需批量时才传 segments>1；逐段落库，后段读前段总结作前情。

    注意：segments 过大会在单次请求里反复调用 LLM，前情摘要链虽短，但仍有会话压缩风险；
    生产建议「用户逐次点生成」而非一次性 loop 多段。
    """
    results: list[dict] = []
    for _ in range(segments):
        results.append(generate_next_segment(
            db, project, novel_ids, provider, max_chars, profile=profile,
            style_rewrite=style_rewrite,
        ))
    project.status = "loop_done" if project.status == "generating" else project.status
    db.commit()
    return results
