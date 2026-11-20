"""M4 生成引擎：上下文组装 → 续写 → 段级总结 → 入库（支持段级续写环）。

漂移防护机制：M3.3 的未填坑 + 角色约束进入 system 提示并全程锁定；
前情注入改走**层级摘要树**（L1 段 / L2 卷 / L3 主线骨架，见 summary_tree.py），
把无界的段级摘要链压成有界前情块——实测全量拼接时 N=300 会膨胀到单段正文的 656%。
"""

import copy
import hashlib
import json
import logging
import math
import re
import time

from sqlalchemy import func, or_, select

from app.agents.prompt_overrides import PromptKey, resolve_prompt_block
from app.config import settings
from app.generation.chapter_labels import commit_meta as _chapter_commit_meta
from app.generation.chapter_labels import require_label as _require_chapter_label
from app.generation.length_guard import LengthBlocked
from app.generation.length_guard import length_block_evidence as _length_block_evidence
from app.generation.prompts import (
    PROMPT_REVISION,
    build_system_prompt,
    build_user_prompt,
    chapter_outline_entry,
    filter_available_characters,
    merged_constraint_items,
    render_item_holding_section,
)
from app.generation.repetition import (
    analyze_length,
    analyze_repetition,
    analyze_summary_style,
    quality_payload,
)
from app.generation.scope_guard import (
    declared_local,
    detect_length_drop,
    detect_scope_mismatch,
)
from app.generation.story_cards import render_for_project as _render_story_cards
from app.generation.structure_check import StructureBlocked, check_structure
from app.generation.timeline import (
    TimelineState,
    advance,
    assign_chapter_ranges,
    build_node_prompt,
    force_complete_current,
    has_chapter_ranges,
    judge_node_completion_with_evidence,
    node_for_chapter,
    plan_nodes_with_llm,
    plan_timeline,
    state_from_dict,
    state_to_dict,
    sync_node_for_chapter,
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
from app.kg.bible import (
    character_first_chapter,
    character_gender,
    set_character_first_chapter,
    set_character_gender,
)
from app.kg.book_scope import book_for_project as _book_for_project
from app.kg.book_scope import prompt_scope_book_ids, scope_sql_values
from app.kg.queries import get_reminders
from app.llm.provider import render_constraints
from app.models import (
    Generation,
    KgCharacterState,
    KgEntity,
    KgEvent,
    KgForeshadow,
    KgRelation,
    NovelChunk,
    Project,
)
from app.retrieval.search import hybrid_search
from app.style import stylometry
from app.style.consistency import (
    ARCHIVE_BLOCK_PERCENTILES,
    ARCHIVE_CHAPTER_PERCENTILES,
    GEN_GATE_LIMITS,
    SENTENCE_SCOPE_FROM_CHAPTER,
    SHORT_SENTENCE_MAX_LEN,
    ULTRA_SHORT_DIALOGUE_MAX_HANZI,
    VOLUME_CHAPTERS,
    active_full_limits,
    active_gate_limits,
    char_count,
    gate_limits_from_percentiles,
    generation_gate,
    hanzi_count,
    length_limits,
    merge_ultra_short_dialogue,
    paragraph_floor_limits,
    percentile_row,
    set_dialogue_mix_reference,
    set_dialogue_reference,
    set_gate_limits,
    set_rhythm_reference,
    set_sentence_reference,
    volume_block_means,
    volume_gate,
    volume_reference,
)
from app.style.exemplar import sample_style_exemplars


#: 重写允许的字数上浮比例：**配置项** `settings.rewrite_growth_allowance`。
#:
#: 默认 8% 是**手写值，不是档案推导**：来源是第 4 章实测——修碎段唯一有效的
#: 手段，是把短对白并入场景时补上说话人的神态/动作/旁人反应，这必然增字；
#: 旧口径「不得多于当前字数」把这个手段禁掉了，模型只能靠删字把段均比值做上去
#: （第 4 章实测 2,987→2,868 汉字，段均只从 23.2 到 24.8）。
#: 它只防借补写无限膨胀，不是篇幅预算——篇幅上限仍由 `length_limits` 决定。
#: 跑飞回退阈值是独立配置 `rewrite_growth_reject_ratio`（默认 16% = 2 倍），
#: 同样是手写值。
def _rewrite_growth_allowance() -> float:
    value = getattr(settings, "rewrite_growth_allowance", None)
    return 0.08 if value is None else float(value)


def _rewrite_growth_reject_ratio() -> float:
    value = getattr(settings, "rewrite_growth_reject_ratio", None)
    return 0.16 if value is None else float(value)


#: ---- 重写/补完的**调度**开关（2026-11-15）---------------------------------
#: 这一组只决定「还要不要再调一次模型」，**不参与任何判定**：闸门判据、阈值、
#: 违规项定义全部原样留给 `generation_gate` / `check_structure` / `length_limits`。
#: 判定「有没有变好」也只读这些函数已有的输出（passed + 违规项列表）。
def _rewrite_max_rounds() -> int:
    """重写轮数上限；默认 2 = 旧行为，0 表示不跑重写轮（超长仍走压缩兜底）。"""
    value = getattr(settings, "rewrite_max_rounds", None)
    try:
        rounds = int(value) if value is not None else 2
    except (TypeError, ValueError):
        rounds = 2
    return max(0, rounds)


def _rewrite_stop_without_gain() -> bool:
    """一轮重写没有可测改善时是否提前停；默认 True，False 可回退旧调度。"""
    value = getattr(settings, "rewrite_stop_without_gain", None)
    return True if value is None else bool(value)


def _rewrite_skip_oversize() -> bool:
    """初稿已超上限时是否跳过整章重写、直接压缩；默认 True。"""
    value = getattr(settings, "rewrite_skip_oversize", None)
    return True if value is None else bool(value)


def _draft_completion_max_rounds() -> int:
    """初稿补完轮数上限；默认取常量 2，0 表示关闭补写。"""
    value = getattr(settings, "draft_completion_max_rounds", None)
    try:
        rounds = int(value) if value is not None else _DRAFT_COMPLETION_MAX_ROUNDS
    except (TypeError, ValueError):
        rounds = _DRAFT_COMPLETION_MAX_ROUNDS
    return max(0, rounds)


#: 流式初稿留痕的字段集（顺序固定，缺一项就写 null）。
_DRAFT_STREAM_FIELDS = (
    "model",
    "attempt",
    "request_ms",
    "first_frame_ms",
    "first_reasoning_ms",
    "first_content_ms",
    "reasoning_chars_before_content",
    "reasoning_chars",
    "content_chars",
    "reasoning_source",
    "usage",
)


def _draft_stream_quality(stats: dict | None) -> dict:
    """流式初稿的 reasoning/正文增量留痕（只记，不参与任何判定）。

    口径（时间都从「请求发出前」起算，单位毫秒）：
    - `request_ms`：请求发出 → 收到响应头（连接 + 供应商排队）；
    - `first_frame_ms`：请求发出 → 第一个非空增量（reasoning 或正文）；
    - `first_content_ms`：请求发出 → 第一个**可见正文字符**（用户看到的首字）；
    - `reasoning_chars_before_content`：首字到达前累计的思考字符数——
      回答「这一次的首字等待里，多少是思考、多少是正文/排队」；
    - `reasoning_chars` / `content_chars`：整条流的累计字符数；
    - `reasoning_source`：`reasoning_content` / `inline_think` / `both` / None。

    **拿不到就是 null，不写 0**：MiniMax 流式不回 usage、也不保证回
    reasoning 字段，0 会被误读成「确实没有思考」。
    """
    out = {key: None for key in _DRAFT_STREAM_FIELDS}
    if isinstance(stats, dict):
        for key in _DRAFT_STREAM_FIELDS:
            out[key] = stats.get(key)
    return out


def _draft_prompt_min_chars(max_chars: int | None, rhythm: dict) -> int:
    """初稿提示里的字数下限（**只影响提示措辞**，不改闸门与补完判据）。

    默认取 `rhythm["completion_min_ratio"]`——与 `_complete_short_draft` 的
    「明显偏短」触发线**同一个值**，避免出现「提示允许 3200、调度却从 3304 起判」
    的自相矛盾。`settings.draft_prompt_min_ratio` 可覆盖（0<r<1 生效）；
    设 0.8 回到旧的 `max_chars × 0.8`。
    """
    target = int(length_limits(max_chars)["target"])
    raw = getattr(settings, "draft_prompt_min_ratio", None)
    try:
        ratio = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        ratio = 0.0
    if not 0 < ratio < 1:
        ratio = float(
            rhythm.get("completion_min_ratio") or _DRAFT_COMPLETION_MIN_RATIO
        )
    return max(1, math.ceil(target * ratio))


def _rewrite_reason_count(reasons) -> int:
    """剩余问题的**条数**（去重后计）。只数个数，不解释、不改判据。"""
    return len(dict.fromkeys(reasons or []))


def _rewrite_improved(
    passed_before: bool, passed_after: bool, reasons_before, reasons_after
) -> bool:
    """这一轮候选是否带来可测改善。

    只有两种算改善：从「不过」变成「通过」，或剩余问题条数严格减少。
    条数不变或变多、以及候选整轮被丢弃（文本回退、reasons_after 不变）都算没变好。
    """
    if passed_after and not passed_before:
        return True
    return _rewrite_reason_count(reasons_after) < _rewrite_reason_count(reasons_before)


#: 初稿补写（2026-11-14）：汉字数低于目标这个比例就判「明显偏短」，先补写再进闸门。
#: 第 4 章实测首轮 2,700 / 4,000（缺 32.5%）直接进闸门，段均与占比类指标被
#: 短篇幅机械放大，重写轮又只做分布修正，篇幅缺口永远补不上。
#:
#: 这是**档案缺数据时的退回值**；正常走 `_archive_rhythm()` 从参考语料推：
#:   min_ratio  = clamp(P10(章长) / P50(章长), 0.70, 0.92)
#:   stop_ratio = clamp(P25(章长) / P50(章长), min_ratio + 0.03, 0.98)
_DRAFT_COMPLETION_MIN_RATIO = 0.85
#: 补到目标的这个比例就停（不追求逐字命中，留给闸门与重写轮）。
_DRAFT_COMPLETION_STOP_RATIO = 0.95
#: 最多补几轮：每轮都是一次完整生成成本，两轮足够覆盖 15%–30% 的缺口。
_DRAFT_COMPLETION_MAX_ROUNDS = 2
#: 补写 prompt 只带正文末尾这么多字符做衔接（太长会让模型复述前文）。
_COMPLETION_PROMPT_TAIL = 800

#: 节奏口径的样本下限与缓存（进程内，按「参考书 + 切块数」失效）。
_RHYTHM_MIN_CHAPTERS = 10
_RHYTHM_MIN_PURE_PARAS = 30
_RHYTHM_SAMPLE_CHUNKS = 200
#: 章长短于这个字符数的视为残章/标题块，不参与分位（否则 P10 会被拉到几十字）。
_RHYTHM_MIN_CHAPTER_CHARS = 200
_RHYTHM_CACHE: dict[tuple, dict] = {}


def _reference_novel_ids(project: Project) -> list[int]:
    """项目参考书 id：优先 `reference_novel_ids`，否则按当前书取单本。"""
    raw = getattr(project, "reference_novel_ids", None)
    ids: list[int] = []
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                v = int(x)
            except (TypeError, ValueError):
                continue
            if v > 0:
                ids.append(v)
    if not ids:
        book = _book_for_project(project)
        if book:
            ids.append(int(book))
    return sorted(set(ids))


def _archive_rhythm(db, project: Project) -> dict:
    """从参考档案推生成侧节奏口径（只读；任何失败都退回手拍常量）。

    两项口径与公式：

    1. `ultra_dialogue_max` = **纯对白段汉字数 P10**，夹在 [2, 12]。
       比参考书自己最短那一档还短的纯对白段，才算「档案里基本不出现」。
       纯对白段样本 <30 段 → 退回 `ULTRA_SHORT_DIALOGUE_MAX_HANZI`（4）。
    2. `completion_min_ratio` = **P10(章长) / P50(章长)**，夹 [0.70, 0.92]；
       `completion_stop_ratio` = **P25(章长) / P50(章长)**，夹
       [min_ratio + 0.03, 0.98]。有效章 <10 或 P50=0 → 退回 0.85 / 0.95。

    章长口径 = 该章全部 `novel_chunks.text` 的字符数（含空白，只用于比值）；
    <200 字符的残章剔除。纯对白段按 id 等距抽最多 200 个切块算，避免首次
    生成扫全书。结果按（项目、参考书、切块数）缓存。
    """
    fallback = {
        "ultra_dialogue_max": ULTRA_SHORT_DIALOGUE_MAX_HANZI,
        "completion_min_ratio": _DRAFT_COMPLETION_MIN_RATIO,
        "completion_stop_ratio": _DRAFT_COMPLETION_STOP_RATIO,
        "source": "fallback",
    }
    ids = _reference_novel_ids(project)
    if not ids:
        return fallback
    try:
        n_chunks = int(db.scalar(
            select(func.count()).select_from(NovelChunk).where(NovelChunk.novel_id.in_(ids))
        ) or 0)
        key = (tuple(ids), n_chunks)
        cached = _RHYTHM_CACHE.get(key)
        if cached is not None:
            return cached

        rows = db.execute(
            select(
                NovelChunk.novel_id,
                NovelChunk.chapter_no,
                func.sum(func.length(NovelChunk.text)),
            )
            .where(NovelChunk.novel_id.in_(ids), NovelChunk.chapter_no.isnot(None))
            .group_by(NovelChunk.novel_id, NovelChunk.chapter_no)
        ).all()
        lens = sorted(
            int(chars) for _nid, _ch, chars in rows
            if chars and int(chars) >= _RHYTHM_MIN_CHAPTER_CHARS
        )

        chunk_ids = list(db.scalars(
            select(NovelChunk.id)
            .where(NovelChunk.novel_id.in_(ids))
            .order_by(NovelChunk.id)
        ))
        step = max(1, len(chunk_ids) // _RHYTHM_SAMPLE_CHUNKS)
        sample = chunk_ids[::step][:_RHYTHM_SAMPLE_CHUNKS]
        pure: list[int] = []
        if sample:
            for text in db.scalars(
                select(NovelChunk.text).where(NovelChunk.id.in_(sample))
            ):
                for para in stylometry.paragraphs(text or ""):
                    n = stylometry._pure_dialogue_paragraph(para)
                    if n is not None:
                        pure.append(int(n))

        rhythm = dict(fallback)
        rhythm["source"] = "archive"
        rhythm["chapters"] = len(lens)
        rhythm["pure_paras"] = len(pure)
        if len(pure) >= _RHYTHM_MIN_PURE_PARAS:
            p10 = stylometry._quantile(sorted(pure), 0.10)
            rhythm["ultra_dialogue_max"] = int(min(12, max(2, round(p10))))
        if len(lens) >= _RHYTHM_MIN_CHAPTERS:
            p10, p25, p50 = (
                stylometry._quantile(lens, q) for q in (0.10, 0.25, 0.50)
            )
            if p50 > 0:
                min_ratio = min(0.92, max(0.70, p10 / p50))
                stop_ratio = min(0.98, max(min_ratio + 0.03, p25 / p50))
                rhythm["completion_min_ratio"] = round(min_ratio, 3)
                rhythm["completion_stop_ratio"] = round(stop_ratio, 3)
        _RHYTHM_CACHE[key] = rhythm
        return rhythm
    except Exception:  # noqa: BLE001 —— 口径推导失败不阻断生成，退回手拍值
        logging.getLogger(__name__).warning(
            "档案节奏口径推导失败（project %s），退回默认值", getattr(project, "id", None),
            exc_info=True,
        )
        return fallback


#: 章级/卷级分位抽样的章数上限（每本按连续块铺开，不扫全书）。
#:
#: 2026-11-14 由 240 提到 560：卷参照必须按卷长（`volume_chapters`）连续块取，
#: 240 章只够 17 卷，p10/p90 太粗；560 章 = 40 卷，单本约 3s（结果进程内缓存）。
_CHAPTER_PCT_SAMPLE = 560
#: 有效章少于此数不推分位（退回常量表），避免小语料把带算歪。
_CHAPTER_PCT_MIN = 120
_GATE_LIMIT_CACHE: dict[tuple, dict] = {}

#: 大纲里的卷标记：`第N卷` / `第N卷 ...`（N 可为阿拉伯或中文数字）。
_VOLUME_MARK_RE = re.compile(r"^\s*(第[一二三四五六七八九十百零〇两0-9]+卷)")


def _outline_volume_length(project: Project) -> int | None:
    """从项目大纲解析「每卷章数」；解析不出返回 None。

    支持两种大纲形态：
    - 条目是 dict，带 `volume` / `卷` 字段：按该字段分组，计每卷条目数；
    - 条目是字符串：以「第N卷」开头的**章条目**才计入该卷（卷头本身若是独立
      条目、且不含「第N章」，不计数）。

    只有卷数 ≥2 且各卷章数一致时才返回该章数——卷长不等时无法用单一连续块
    做卷对照，此时不臆造，交给配置退回。本项目的 40 章大纲无卷标记，返回 None。
    """
    outline = list(getattr(project, "outline", None) or [])
    groups: dict[str, int] = {}
    for entry in outline:
        if isinstance(entry, dict):
            key = entry.get("volume") or entry.get("卷")
            if key in (None, ""):
                continue
            if not entry.get("chapter") and not entry.get("章") and not entry.get("title"):
                continue
        elif isinstance(entry, str):
            m = _VOLUME_MARK_RE.match(entry)
            if not m or "第" not in entry.replace(m.group(0), "", 1):
                continue
            key = m.group(1)
        else:
            continue
        groups[str(key)] = groups.get(str(key), 0) + 1
    if len(groups) < 2 or len(set(groups.values())) != 1:
        return None
    return next(iter(groups.values()))


def _resolve_volume_chapters(project: Project) -> tuple[int, str]:
    """卷长及其来源：`outline`（大纲卷结构）/ `config`（配置退回）。"""
    from_outline = _outline_volume_length(project)
    if from_outline:
        return int(from_outline), "outline"
    fallback = int(getattr(settings, "volume_chapters", VOLUME_CHAPTERS) or VOLUME_CHAPTERS)
    return max(1, fallback), "config"


def _dialogue_reference_fallback(project: Project) -> dict:
    """对白配比参考线的退回值（v12）：配置优先，其次常量表；调用方负责留痕。"""
    configured = getattr(settings, "dialogue_ratio_reference", None)
    if configured and len(configured) == 3:
        p25, p50, p75 = (float(x) for x in configured)
        return {"p25": p25, "p50": p50, "p75": p75, "source": "config"}
    static = ARCHIVE_BLOCK_PERCENTILES["对白字占比"]
    return {
        "p25": float(static["p25"]),
        "p50": float(static["p50"]),
        "p75": float(static["p75"]),
        "source": "constant",
    }


def _sentence_reference_fallback(project: Project) -> dict:
    """`句长均值` 参考线的退回值（v13）：配置优先，其次常量表；调用方负责留痕。"""
    configured = getattr(settings, "sentence_length_reference", None)
    if configured and len(configured) == 3:
        p10, p50, p90 = (float(x) for x in configured)
        return {"p10": p10, "p50": p50, "p90": p90, "source": "config"}
    static = ARCHIVE_BLOCK_PERCENTILES["句长均值"]
    return {
        "p10": float(static["p10"]),
        "p50": float(static["p50"]),
        "p90": float(static["p90"]),
        "source": "constant",
    }


def _rhythm_reference_fallback(project: Project) -> dict:
    """短句/长句/句长节奏参考的退回值（v14）：配置优先，其次常量表。

    配置项（`settings.short_sentence_ratio_reference` / `long_...`）各是
    [p10, p50, p90]；`句长均值` 复用 `settings.sentence_length_reference`
    （p50 用于把目标字数折算成总句数）。
    """
    def _triple(name: str, static: dict) -> dict:
        configured = getattr(settings, name, None)
        if configured and len(configured) == 3:
            p10, p50, p90 = (float(x) for x in configured)
            return {"p10": p10, "p50": p50, "p90": p90, "source": "config"}
        return {
            "p10": float(static["p10"]),
            "p50": float(static["p50"]),
            "p90": float(static["p90"]),
            "source": "constant",
        }

    short = _triple("short_sentence_ratio_reference", ARCHIVE_BLOCK_PERCENTILES["短句占比"])
    long = _triple("long_sentence_ratio_reference", ARCHIVE_BLOCK_PERCENTILES["长句占比"])
    sent = _sentence_reference_fallback(project)
    source = "config" if all(
        x.get("source") == "config" for x in (short, long, sent)
    ) else "constant"
    return {
        "短句占比": {k: short[k] for k in ("p10", "p50", "p90")},
        "长句占比": {k: long[k] for k in ("p10", "p50", "p90")},
        "句长均值": {k: sent[k] for k in ("p10", "p50", "p90")},
        "source": source,
    }


def _first_positive(row: dict) -> float:
    """分位行里第一个大于 0 的分位（p10 常为 0 的指标用它当有效下沿）。"""
    for key in ("p10", "p25", "p50", "p75", "p90"):
        v = row.get(key)
        if isinstance(v, (int, float)) and float(v) > 0:
            return float(v)
    return 0.0


def _dialogue_mix_fallback(project: Project) -> dict:
    """对白句类配比的退回值（v14）：配置优先，其次参考书**章级**常量表。

    配置项 `settings.dialogue_mix_reference` 依序是
    [陈述句 p50, 问句 p25, 问句 p90, 感叹句首个非零分位, 叹词起头首个非零分位]。
    """
    configured = getattr(settings, "dialogue_mix_reference", None)
    if configured and len(configured) == 5:
        stmt, q25, q90, exc, tan = (float(x) for x in configured)
        return {
            "陈述p50": stmt, "问句p25": q25, "问句p90": q90,
            "感叹p25": exc, "叹词p50": tan, "source": "config",
        }

    stmt_row = ARCHIVE_CHAPTER_PERCENTILES["对白陈述句占比"]
    q_row = ARCHIVE_CHAPTER_PERCENTILES["对白问句占比"]
    return {
        "陈述p50": float(stmt_row["p50"]),
        "问句p25": float(q_row["p25"]),
        "问句p90": float(q_row["p90"]),
        "感叹p25": _first_positive(ARCHIVE_CHAPTER_PERCENTILES["对白感叹句占比"]),
        "叹词p50": _first_positive(ARCHIVE_CHAPTER_PERCENTILES["对白叹词起头占比"]),
        "source": "constant",
    }


def _gate_fallback(project: Project, fallback: dict, reason: str) -> dict:
    """闸门带退回常量表：写入原因并 WARN，不许静默（2026-11-14）。"""
    fallback["fallback_used"] = True
    fallback["fallback_reason"] = reason
    fallback["reason"] = reason
    # v12：对白配比参考线也必须跟着退回，否则提示会拿到空参考线。
    fallback.setdefault("dialogue_reference", _dialogue_reference_fallback(project))
    # v13：句长均值参考线同理。
    fallback.setdefault("sentence_reference", _sentence_reference_fallback(project))
    # v14：短句/长句节奏参考与对白句类配比同理。
    fallback.setdefault("rhythm_reference", _rhythm_reference_fallback(project))
    fallback.setdefault("dialogue_mix_reference", _dialogue_mix_fallback(project))
    # v19：句长形状参考推不出来时**不臆造**——两项判据只报 no_reference，
    # 不判、不拦；宁可缺一条观测，也不拿别本书的数字当门槛。
    fallback.setdefault("sentence_shape", {})
    fallback["sentence_scope"] = stylometry.SENTENCE_METRIC_SCOPE
    # 仅记录、无判罚效力：不参与任何闸门分支，换章号来源也不会误判。
    fallback["sentence_scope_from_chapter"] = SENTENCE_SCOPE_FROM_CHAPTER
    fallback["short_sentence_max_len"] = SHORT_SENTENCE_MAX_LEN
    logging.getLogger(__name__).warning(
        "闸门带退回常量表：%s（project %s，参考书 %s，有效章 %s）",
        reason, getattr(project, "id", None),
        fallback.get("reference_novels"), fallback.get("chapters"),
    )
    return fallback


def _archive_gate_limits(db, project: Project) -> dict:
    """从项目参考语料推**章级**分位与**卷级**（连续 `volume_chapters` 章块均值）分位。

    为什么必须重推：`ARCHIVE_BLOCK_PERCENTILES` 是 1,200 字块级样本，而生成单元
    是整章。块级「短句占比 p10＝0.023」与章级 p10＝0.020 不是同一分布——章级下限
    只能用章级分位。同理，「极短纯对白段占比」的上限不再写死 0，而是取参考语料
    **章级 p90**：参考书 93% 的章为 0 时它自然为 0，换一本语料（如中位 2.6% 的书）
    会自动变宽，不会误判。

    卷参照用**连续 `volume_chapters` 章**为一卷（卷长优先读项目大纲卷结构，读不到
    退回 `settings.volume_chapters`），逐块取 13 项指纹均值，再取这些卷均值的分位：
    p50 是「参考卷均值」，p10–p90 是参考语料自己卷与卷之间的正常摆动范围——趋势
    判据的容差就来自它，不手拍百分比。

    结果按（参考书 id、抽样上限、卷长）缓存；项目换参考书/换卷长 → key 变 → 自动
    重推。失败/样本不足退回常量表，并写 `fallback_used`/`fallback_reason` + WARN。
    """
    vc, vc_source = _resolve_volume_chapters(project)
    fallback = {
        "source": "fallback", "reason": "insufficient_archive",
        "chapter_limits": None, "volume_reference": None,
        "chapters": 0, "volumes": 0, "reference_novels": [],
        "volume_chapters": vc, "volume_chapters_source": vc_source,
        "fallback_used": True, "fallback_reason": "insufficient_archive",
    }
    ids = _reference_novel_ids(project)
    if not ids:
        return _gate_fallback(project, fallback, "no_reference")
    key = (tuple(ids), _CHAPTER_PCT_SAMPLE, vc)
    if key in _GATE_LIMIT_CACHE:
        return _GATE_LIMIT_CACHE[key]
    try:
        pairs = db.execute(
            select(NovelChunk.novel_id, NovelChunk.chapter_no)
            .where(NovelChunk.novel_id.in_(ids), NovelChunk.chapter_no.isnot(None))
            .distinct()
        ).all()
        by_book: dict[int, list[int]] = {}
        for nid, ch in pairs:
            by_book.setdefault(int(nid), []).append(int(ch))

        per_book = max(1, _CHAPTER_PCT_SAMPLE // max(1, len(by_book)))
        metrics_by_book: dict[int, list[list[dict]]] = {}
        # v14：章级「句数 / 汉字数」密度样本。提示里的绝对句数要按目标字数折算，
        # 旧实现用「字符/汉字 = 1.3」这个手写换算；这里改成直接用参考书自己
        # 每章的（切句数 ÷ 汉字数）分位，任何手写换算都不再参与推导。
        # v19：分子改用 `sentence_lens`（只计含汉字的句子），与句长类指标同尺。
        density_samples: list[float] = []
        # v19：中段拥挤度的窄带由参考书**池化** p25–p75 现推（不写死 9–29），
        # 逐章留一份句长样本，等池化带算出来后再回算每章的拥挤度。
        chapter_lens: list[list[int]] = []
        for nid, chs in by_book.items():
            chs.sort()
            # 连续块采样：每块 `vc` 章，块起点等距铺开。卷参照必须是连续章——
            # 等距抽 14 章会横跨几百章，把卷内摆动平均掉，参照带变窄。
            n_blocks = max(1, per_book // vc)
            if len(chs) >= vc:
                span = len(chs) - vc
                starts = sorted({span * i // max(1, n_blocks) for i in range(n_blocks)}) \
                    if n_blocks > 1 else [0]
                blocks = [chs[s:s + vc] for s in starts]
            else:
                blocks = [chs]
            picked = [c for b in blocks for c in b]
            parts: dict[int, list[str]] = {}
            for cno, txt in db.execute(
                select(NovelChunk.chapter_no, NovelChunk.text)
                .where(NovelChunk.novel_id == nid, NovelChunk.chapter_no.in_(picked))
                .order_by(NovelChunk.chapter_no, NovelChunk.id)
            ):
                parts.setdefault(int(cno), []).append(txt or "")
            book_blocks: list[list[dict]] = []
            for block in blocks:
                rows = []
                for cno in block:
                    body = "\n".join(parts.get(cno, [])).strip()
                    if len(body) < _RHYTHM_MIN_CHAPTER_CHARS:
                        continue
                    metrics = generation_gate(body)["metrics"]
                    rows.append(metrics)
                    hanzi = float(metrics.get("汉字数") or 0.0)
                    lens = stylometry.sentence_lens(body)
                    if hanzi > 0 and lens:
                        density_samples.append(len(lens) / hanzi)
                        chapter_lens.append(lens)
                if rows:
                    book_blocks.append(rows)
            if book_blocks:
                metrics_by_book[nid] = book_blocks

        all_blocks = [b for blocks in metrics_by_book.values() for b in blocks]
        all_rows = [m for block in all_blocks for m in block]
        if len(all_rows) < _CHAPTER_PCT_MIN:
            fallback["chapters"] = len(all_rows)
            fallback["reference_novels"] = ids
            return _gate_fallback(project, fallback, "insufficient_archive")

        volume_rows = volume_block_means(all_blocks, window=vc)
        chapter_pct = {
            name: percentile_row([r.get(name) for r in all_rows])
            for name in set(GEN_GATE_LIMITS) | {
                "段数",
                # v14：对白句类配比需要章级分位（提示按「每 10 句」折算）。
                "对白陈述句占比", "对白问句占比", "对白感叹句占比", "对白叹词起头占比",
            }
        }
        volume_pct = volume_reference(volume_rows)
        # v14：参考书每章「句数 ÷ 汉字数」的 p50（句/汉字）。提示按
        # `目标汉字数 × 句密度 p50` 折算总句数，不经过任何手写的字符/汉字换算。
        density_pct = percentile_row(density_samples)
        density_p50 = (density_pct or {}).get("p50")

        # v19：句长**形状**参考（非判罚）。两项都由参考书现推，不写死数字：
        # - 中段拥挤度 = 落在「池化 p25–p75」窄带内的句子占比，上限取该指标
        #   自己的章级 p90；
        # - 上尾下限 = `长句占比`（≥40 字）的章级 p10。
        # 窄带用**池化**分位而不是章级分位的中位：章级 p25 的中位会把带压窄，
        # 池化 p25–p75 才是「参考书全体的中间一半」。
        sentence_shape: dict[str, Any] = {}
        pooled = sorted(x for lens in chapter_lens for x in lens)
        if len(pooled) >= 200:
            b25 = float(stylometry._quantile(pooled, 0.25))
            b75 = float(stylometry._quantile(pooled, 0.75))
            crowd_rows = [
                sum(1 for x in lens if b25 <= x <= b75) / len(lens)
                for lens in chapter_lens if lens
            ]
            crowd_pct = percentile_row(crowd_rows)
            long_row = dict(chapter_pct.get("长句占比") or {})
            sentence_shape = {
                "band": {"p25": round(b25, 2), "p75": round(b75, 2)},
                "crowd": crowd_pct,
                "crowd_max": crowd_pct.get("p90"),
                "long_ratio_floor": long_row.get("p10"),
                "long_ratio_pct": long_row,
                "chapters": len(crowd_rows),
                "scope": stylometry.SENTENCE_METRIC_SCOPE,
                "source": "archive_pooled",
            }
        else:
            fallback_reasons.append("sentence_shape_insufficient")

        # v10：`极短纯对白段占比` 上限改用**非零章 p90**。
        # 全章 p90 在参考书自己多数章为 0 时恒等于 0（书里 6.57% 的章非零），
        # 拿 0 当上限等于每章必挂一项，没有判别力；非零章 p90 才是「参考书自己
        # 也会正常出现的量级」。换参考书自动重推；非零章样本为空时退回配置值
        # （未配置则退回常量表），并沿用 fallback_used / fallback_reason 留痕。
        short_all = [float(r.get("极短纯对白段占比") or 0.0) for r in all_rows]
        short_nonzero = [v for v in short_all if v > 0.0]
        nonzero_row = percentile_row(short_nonzero)
        ultra_p90 = (nonzero_row or {}).get("p90")
        full_p90 = (chapter_pct.get("极短纯对白段占比") or {}).get("p90")
        fallback_reasons: list[str] = []
        if ultra_p90 is not None:
            ultra_max = float(ultra_p90)
            ultra_source = "archive_nonzero_p90"
        else:
            configured = getattr(settings, "ultra_short_dialogue_max_ratio", None)
            if configured is not None:
                ultra_max = float(configured)
                ultra_source = "config"
            else:
                ultra_max = float(
                    (GEN_GATE_LIMITS.get("极短纯对白段占比") or {}).get("max") or 0.0
                )
                ultra_source = "constant"
            fallback_reasons.append("ultra_short_p90_empty")
            logging.getLogger(__name__).warning(
                "极短纯对白段上限退回：参考书 %s 有效章 %s 个、非零章 %s 个，"
                "推不出非零章 p90，改用 %s（project %s）",
                ids, len(short_all), len(short_nonzero), ultra_source,
                getattr(project, "id", None),
            )

        judge, full = gate_limits_from_percentiles(chapter_pct, ultra_short_max=ultra_max)
        # v12：对白配比参考线（非判罚）。与「本书最近一卷实测」同尺度，
        # 优先取**卷级**分位（参考书连续 `vc` 章块均值的 p25/p50/p75）；
        # 卷样本不足时退回章级分位，再退配置/常量表。
        dlg_vol = (volume_pct or {}).get("对白字占比") or {}
        dlg_ch = chapter_pct.get("对白字占比") or {}
        vol_ok = all(dlg_vol.get(k) is not None for k in ("p25", "p50", "p75"))
        ch_ok = all(dlg_ch.get(k) is not None for k in ("p25", "p50", "p75"))
        if vol_ok:
            dialogue_reference = {
                **{k: dlg_vol[k] for k in ("p25", "p50", "p75")},
                "source": "archive_volume", "volume_chapters": vc,
            }
        elif ch_ok:
            dialogue_reference = {
                **{k: dlg_ch[k] for k in ("p25", "p50", "p75")},
                "source": "archive_chapter",
            }
        else:
            dialogue_reference = _dialogue_reference_fallback(project)
            fallback_reasons.append("dialogue_reference_fallback")
            logging.getLogger(__name__).warning(
                "对白配比参考线退回：参考书 %s 的卷级/章级分位都推不出 p25/p50/p75，"
                "改用 %s（project %s）",
                ids, dialogue_reference.get("source"), getattr(project, "id", None),
            )
        # v13：主因指标参考线（非判罚）。段长缺口拆成 `段均句数 × 句长均值` 后，
        # 句长均值偏离参考带最远，故用它的卷口径 p10/p50/p90 做参考线。
        # 只补参考线，不把 `段长均值` 本身变成目标。
        sent_rows = volume_block_means(all_blocks, metrics=("句长均值",), window=vc)
        # 直接对「连续 vc 章块的句长均值」取分位——`volume_reference` 只覆盖
        # `FINGERPRINT_METRICS` 这 13 项（不含句长均值），不能拿它取这一项。
        sent_pct = percentile_row([r.get("句长均值") for r in sent_rows])
        if all(sent_pct.get(k) is not None for k in ("p10", "p50", "p90")):
            sentence_reference = {
                **{k: sent_pct[k] for k in ("p10", "p50", "p90")},
                "source": "archive_volume", "volume_chapters": vc,
            }
        else:
            sentence_reference = _sentence_reference_fallback(project)
            fallback_reasons.append("sentence_reference_fallback")
            logging.getLogger(__name__).warning(
                "句长均值参考线退回：参考书 %s 的卷级分位推不出 p10/p50/p90，"
                "改用 %s（project %s）",
                ids, sentence_reference.get("source"), getattr(project, "id", None),
            )

        # v14：短句/长句节奏参考（非判罚）。提示里的「至少 N 句短句/长句」
        # 由它现推：总句数 = 目标汉字 × 字符/汉字换算 ÷ 参考书卷口径句长 p50，
        # 再乘参考书卷口径短句/长句占比 p10（与「本书最近一卷」同尺度）。
        short_vol = (volume_pct or {}).get("短句占比") or {}
        long_vol = (volume_pct or {}).get("长句占比") or {}
        rhythm_ok = (
            all(short_vol.get(k) is not None for k in ("p10", "p50", "p90"))
            and all(long_vol.get(k) is not None for k in ("p10", "p50", "p90"))
            and all(sent_pct.get(k) is not None for k in ("p10", "p50", "p90"))
        )
        if rhythm_ok:
            rhythm_reference = {
                "短句占比": {k: short_vol[k] for k in ("p10", "p50", "p90")},
                "长句占比": {k: long_vol[k] for k in ("p10", "p50", "p90")},
                "句长均值": {k: sent_pct[k] for k in ("p10", "p50", "p90")},
                "source": "archive_volume", "volume_chapters": vc,
            }
            if density_p50 is not None and float(density_p50) > 0:
                rhythm_reference["句密度"] = {
                    "p50": round(float(density_p50), 5),
                    "chapters": len(density_samples),
                    "note": "参考书章级「切句数 ÷ 汉字数」p50（句/汉字）",
                }
            else:
                fallback_reasons.append("sentence_density_empty")
                logging.getLogger(__name__).warning(
                    "句密度退回：参考书 %s 的章级句数/汉字数样本为空，提示将用"
                    "字符/汉字退回换算（project %s）",
                    ids, getattr(project, "id", None),
                )
        else:
            rhythm_reference = _rhythm_reference_fallback(project)
            fallback_reasons.append("rhythm_reference_fallback")
            logging.getLogger(__name__).warning(
                "短句/长句节奏参考退回：参考书 %s 的卷级分位推不出短句/长句/句长"
                "p10/p50/p90，改用 %s（project %s）",
                ids, rhythm_reference.get("source"), getattr(project, "id", None),
            )

        # v14：对白句类配比（章级分位，非判罚）。把参考书的陈述/问句/感叹/叹词
        # 分位落到「每 10 句」的句数，替代手写的 5/2/2/2。
        stmt = (chapter_pct.get("对白陈述句占比") or {}).get("p50")
        q25 = (chapter_pct.get("对白问句占比") or {}).get("p25")
        q90 = (chapter_pct.get("对白问句占比") or {}).get("p90")
        exc = _first_positive(chapter_pct.get("对白感叹句占比") or {})
        tan = _first_positive(chapter_pct.get("对白叹词起头占比") or {})
        if stmt is not None and q25 is not None and q90 is not None and exc > 0 and tan > 0:
            dialogue_mix_reference = {
                "陈述p50": float(stmt), "问句p25": float(q25),
                "问句p90": float(q90), "感叹p25": float(exc),
                "叹词p50": float(tan), "source": "archive_chapter",
            }
        else:
            dialogue_mix_reference = _dialogue_mix_fallback(project)
            fallback_reasons.append("dialogue_mix_fallback")
            logging.getLogger(__name__).warning(
                "对白句类配比退回：参考书 %s 的章级分位不完整，改用 %s（project %s）",
                ids, dialogue_mix_reference.get("source"), getattr(project, "id", None),
            )
        meta = {
            "source": "archive", "reason": "",
            "chapter_limits": judge, "full_limits": full,
            "dialogue_reference": dialogue_reference,
            "sentence_reference": sentence_reference,
            "rhythm_reference": rhythm_reference,
            "dialogue_mix_reference": dialogue_mix_reference,
            "chapter_percentiles": chapter_pct,
            "sentence_shape": sentence_shape,
            "sentence_scope": stylometry.SENTENCE_METRIC_SCOPE,
            # `sentence_scope_from_chapter` 仅记录、无判罚效力：全仓没有任何分支
            # 读取它，换章号来源也不会误判——它不是闸门开关，别当闸用。
            "sentence_scope_from_chapter": SENTENCE_SCOPE_FROM_CHAPTER,
            "short_sentence_max_len": SHORT_SENTENCE_MAX_LEN,
            "volume_reference": volume_pct,
            "chapters": len(all_rows), "volumes": len(volume_rows),
            "volume_chapters": vc, "volume_chapters_source": vc_source,
            "fallback_used": False, "fallback_reason": "",
            "reference_novels": ids,
            "ultra_short_limit": {
                "max": round(ultra_max, 4),
                "source": ultra_source,
                "nonzero_p90": None if ultra_p90 is None else round(float(ultra_p90), 4),
                "full_chapter_p90": full_p90,
                "nonzero_chapters": len(short_nonzero),
                "chapters": len(short_all),
                "nonzero_share": (
                    round(len(short_nonzero) / len(short_all), 4) if short_all else 0.0
                ),
                "note": (
                    "全章 p90 恒为 0（参考书自己多数章非零），拿 0 当上限每章必挂、"
                    "无判别力；故取非零章 p90"
                ),
            },
        }
        if len(volume_rows) < 2:
            # 参考书有效章不足两卷（或短于一卷）：卷均值只有 0–1 个样本，
            # `percentile_row` 返回空，卷末对照会静默不判。显式留痕。
            fallback_reasons.append("volume_reference_empty")
            logging.getLogger(__name__).warning(
                "卷参照为空：参考书有效章 %s / 卷长 %s，卷均值样本仅 %s 个（<2；"
                "project %s，参考书 %s），卷末不做趋势判定",
                len(all_rows), vc, len(volume_rows),
                getattr(project, "id", None), ids,
            )
        meta["fallback_used"] = bool(fallback_reasons)
        meta["fallback_reason"] = "+".join(fallback_reasons)
        _GATE_LIMIT_CACHE[key] = meta
        return meta
    except Exception:  # noqa: BLE001 —— 推不出来就退回常量表，不阻断生成
        logging.getLogger(__name__).warning(
            "章级闸门带推导失败（project %s），退回常量表",
            getattr(project, "id", None), exc_info=True,
        )
        return _gate_fallback(project, fallback, "derive_error")


def _apply_gate_limits(db, project: Project, seg_no: int | None = None) -> dict:
    """把参考语料推出的结构判罚带写进当前 context，返回元信息供留痕。

    `seg_no` 用于取本书最近一卷的实测值（对白配比参考线 v12、句长均值参考线 v13）；
    不传时只写参考书分位，不写本书实测。
    """
    meta = dict(_archive_gate_limits(db, project))
    # v22：段数下限随目标字数线性折算（`paragraph_floor_limits`）。参考书 p10 是
    # 4,000 字基准下的绝对段数，4,000 字目标折算后仍等于 p10（项目 6 = 70）；
    # 短目标按 17.5 段/千字下降（1,200 → 21），无参考书时从常量带 70 反推同系数。
    # 折算结果与来源一并写进 meta，随章落库（quality.gate_limits["段数"]）可复核。
    target_chars = int(
        length_limits(getattr(project, "generation_length", None))["target"]
    )
    judge = paragraph_floor_limits(meta.get("chapter_limits"), target_chars)
    full = paragraph_floor_limits(meta.get("full_limits"), target_chars)
    meta["chapter_limits"] = judge
    meta["full_limits"] = full
    if judge:
        set_gate_limits(
            judge=judge,
            full=full,
        )
    # 实际生效的段数下限：有档案用折算后的判罚带，无档案用常量带回退折算。
    effective = judge or paragraph_floor_limits(active_gate_limits(), target_chars)
    meta["段数"] = (effective or {}).get("段数")
    window = int(meta.get("volume_chapters") or VOLUME_CHAPTERS)
    rows = _recent_gate_metrics(db, project, seg_no, window) if seg_no is not None else []

    ref = dict(meta.get("dialogue_reference") or _dialogue_reference_fallback(project))
    if ref and seg_no is not None:
        if ref.get("source") == "archive_volume":
            ref["window"] = window
        vals = [
            float(m["对白字占比"]) for m in rows
            if isinstance(m.get("对白字占比"), (int, float))
        ]
        if vals:
            ref["book_recent_ratio"] = round(sum(vals) / len(vals), 4)
            ref["book_recent_chapters"] = len(vals)
    meta["dialogue_reference"] = ref
    set_dialogue_reference(ref or None)

    sref = dict(meta.get("sentence_reference") or _sentence_reference_fallback(project))
    if sref and seg_no is not None:
        if sref.get("source") == "archive_volume":
            sref["window"] = window
        svals = [
            float(m["句长均值"]) for m in rows
            if isinstance(m.get("句长均值"), (int, float))
        ]
        if svals:
            sref["book_recent_mean"] = round(sum(svals) / len(svals), 3)
            sref["book_recent_chapters"] = len(svals)
    meta["sentence_reference"] = sref
    set_sentence_reference(sref or None)

    rref = dict(meta.get("rhythm_reference") or _rhythm_reference_fallback(project))
    if rref and seg_no is not None:
        if rref.get("source") == "archive_volume":
            rref["window"] = window
        for metric, field in (("短句占比", "book_recent_short"),
                              ("长句占比", "book_recent_long")):
            vals = [
                float(m[metric]) for m in rows
                if isinstance(m.get(metric), (int, float))
            ]
            if vals:
                rref[field] = round(sum(vals) / len(vals), 4)
                rref["book_recent_chapters"] = len(vals)
    meta["rhythm_reference"] = rref
    set_rhythm_reference(rref or None)

    mref = dict(meta.get("dialogue_mix_reference") or _dialogue_mix_fallback(project))
    meta["dialogue_mix_reference"] = mref
    set_dialogue_mix_reference(mref or None)
    return meta


def _merge_spc_cap(gate_limits_meta: dict) -> float | None:
    """确定性并段的「段均句数」上限：参考语料章级 p90（推不出用档案块级 p90）。

    并段是流水线里**唯一会主动把单句段压下去**的环节（段数 -1、句数不变），
    所以上限必须来自参考语料自己的分布：超过 p90 就停手，宁可「段长均值」
    不达标（记录项），也不许把结构推过参考上沿（第 16 章实测 2.13 > p90 2.10）。
    """
    row = ((gate_limits_meta.get("full_limits") or {}).get("段均句数")
           or active_full_limits().get("段均句数") or {})
    cap = row.get("max")
    return float(cap) if cap else None


def _merge_touched(info: dict) -> bool:
    """并段是否「有话说」：真并了、为保单句段放弃、或撞到段均句数上限。"""
    return bool(
        info.get("merged")
        or info.get("skipped_single_neighbor")
        or info.get("stopped_by_spc_cap")
    )


def _spc_of(text: str) -> float:
    """段均句数（与闸门同口径）：剥段首尾引号后切句，按自然段取均值。"""
    paras = stylometry.paragraphs(text or "")
    if not paras:
        return 0.0
    return sum(
        len(stylometry.split_sentences(stylometry.strip_edge_quotes(p))) for p in paras
    ) / len(paras)


def _recent_gate_metrics(db, project: Project, seg_no: int, n: int) -> list[dict]:
    """取本章之前最近 n 章的闸门实测值（时间正序），用于卷均值对照。"""
    rows = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id, Generation.segment_no < seg_no)
        .order_by(Generation.segment_no.desc())
        .limit(n)
    ).all()
    out: list[dict] = []
    for gen in reversed(rows):
        try:
            quality = json.loads(gen.quality_json or "{}")
        except (TypeError, ValueError):
            quality = {}
        metrics = (quality.get("style_gate") or {}).get("metrics") or {}
        if metrics:
            out.append(metrics)
    return out


def _previous_volume_rel(db, project: Project, seg_no: int,
                         window: int = VOLUME_CHAPTERS) -> dict[str, float]:
    """上一卷落库的相对差（`volume_gate.metrics[*].rel_diff`），做趋势基准。

    扫描窗口放大到 3 卷：重写/换版本会在同一章留下多行，按「最近 `window` 行」找
    可能漏掉上一卷的卷末行。
    """
    rows = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id, Generation.segment_no < seg_no)
        .order_by(Generation.segment_no.desc())
        .limit(window * 3)
    ).all()
    for gen in rows:
        try:
            quality = json.loads(gen.quality_json or "{}")
        except (TypeError, ValueError):
            continue
        prev = quality.get("volume_gate") or {}
        if prev.get("status") in ("baseline", "ok", "drift"):
            return {
                k: float(v.get("rel_diff"))
                for k, v in (prev.get("metrics") or {}).items()
                if isinstance(v.get("rel_diff"), (int, float))
            }
    return {}


def _attach_volume_gate(db, project: Project, quality: dict, seg_no: int,
                        chapter_no: int, metrics: dict,
                        gate_limits_meta: dict) -> dict:
    """卷末（每 `volume_chapters` 章或全书最后一章）算一次卷对照。

    卷长来自 `gate_limits_meta["volume_chapters"]`（优先大纲卷结构，读不到用
    `settings.volume_chapters`）。非卷末不写 `volume_gate`；卷样本不足该章数时
    记 `incomplete_volume`，不拿短卷凑。
    """
    window = int(gate_limits_meta.get("volume_chapters") or _resolve_volume_chapters(project)[0])
    total = len(getattr(project, "outline", None) or [])
    is_end = (chapter_no % window == 0) or (total and chapter_no == total)
    if not is_end:
        return {}
    history = _recent_gate_metrics(db, project, seg_no, window - 1)
    payload = volume_gate(
        history + [metrics],
        gate_limits_meta.get("volume_reference") or {},
        previous_rel=_previous_volume_rel(db, project, seg_no, window) or None,
        window=window,
    )
    payload["volume_no"] = (chapter_no - 1) // window + 1
    payload["chapter_range"] = [max(1, chapter_no - window + 1), chapter_no]
    quality["volume_gate"] = payload
    return payload


def _echoes_existing(continuation: str, existing: str, window: int = 24) -> bool:
    """补写是否在复述已有正文：任意 24 字窗口原样出现过即判注水。"""
    cont = "".join(continuation.split())
    base = "".join(existing.split())
    if len(cont) < window or len(base) < window:
        return False
    grams = {base[i:i + window] for i in range(len(base) - window + 1)}
    return any(cont[i:i + window] in grams for i in range(len(cont) - window + 1))


def _complete_short_draft(
    provider, text: str, max_chars: int | None = None,
    min_ratio: float | None = None, stop_ratio: float | None = None,
    constraints: dict | None = None,
) -> tuple[str, dict]:
    """初稿明显偏短时，**先补写、再进闸门**。

    篇幅缺口不交给重写轮：重写的目标是修分布（段均、对白占比），它既不保证
    增字，还会被 `rewrite_growth_allowance` 挡住。这里用独立的续写调用把
    缺口补上，补写有两条确定性护栏：

    - `_echoes_existing`：续写只要复述已有正文的任意 24 字窗口就整轮丢弃；
    - `analyze_repetition`：补完的全文若被判复读退化，整轮丢弃。

    `min_ratio` / `stop_ratio` 由 `_archive_rhythm()` 按参考语料章长分位推出
    （见该函数）；传 None 或非法值时退回 `_DRAFT_COMPLETION_MIN_RATIO` /
    `_DRAFT_COMPLETION_STOP_RATIO`。

    不新增情节/人物靠 prompt 约束（补写只准推进当前场景）。`constraints` 必须由
    调用方传入与初稿**同一份**锁定名单（2026-11-15 修复）：补写轮生成的是正文，
    角色清册与未填伏笔对它同样生效；此前这里不传 constraints，导致本章最后一次
    正文调用拿不到 bible 层约束（p0 回归里表现为 provider 实收名单为空）。
    """
    limit = length_limits(max_chars)
    target = limit["target"]
    # 与闸门下限同一个数：`char_count`（含标点与换行）。补写比例
    # （`_archive_rhythm` 的 min_ratio）本来就是按字符章长算的，此前这里
    # 却量汉字，同一个 3000 字预算下两把尺差约 20%。
    before = char_count(text)
    mr = (
        float(min_ratio)
        if isinstance(min_ratio, (int, float)) and 0 < float(min_ratio) < 1
        else _DRAFT_COMPLETION_MIN_RATIO
    )
    sr = (
        float(stop_ratio)
        if isinstance(stop_ratio, (int, float)) and mr < float(stop_ratio) <= 1
        else _DRAFT_COMPLETION_STOP_RATIO
    )
    max_rounds = _draft_completion_max_rounds()
    info: dict = {
        "triggered": False,
        "target": target,
        "before_chars": before,
        "after_chars": before,
        "min_ratio": round(mr, 3),
        "stop_ratio": round(sr, 3),
        "max_rounds": max_rounds,
        "rounds": [],
    }
    if not text or before >= target * mr:
        return text, info

    info["triggered"] = True
    system = (
        "你是中文长篇小说的续写引擎。用户给出的正文在中途停笔，你必须**接着断点"
        "继续写**，把本章剩余部分写完。硬性要求：\n"
        "1. 严格承接上一句的场景、时间与人物位置，不要重述、不要概括已经写过的内容；\n"
        "2. 不新增人物、不新增支线、不开启新场景，只推进当前这一幕；\n"
        "3. 不得注水：不许复述、不许空转、不许用排比堆字，每句都要有新信息；\n"
        "4. 只输出续写正文，不要任何解释、标题、序号或「续写」字样。"
    )
    for round_no in range(1, max_rounds + 1):
        cur = char_count(text)
        gap = max(0, target - cur)
        if cur >= target * sr or gap <= 0:
            break
        user = (
            f"【已有正文末尾（仅供衔接）】\n{text[-_COMPLETION_PROMPT_TAIL:]}\n\n"
            f"【本章目标】约 {target} 个字符（含标点）；目前已写 {cur} 个，还差 {gap} 个。"
            f"请从断点处接着写约 {gap} 个字符，写完自然收束，不要重复已有内容。"
        )
        try:
            cont = _chat_with_retry(provider, system, user, constraints=constraints)
        except Exception as exc:  # noqa: BLE001 —— 补写失败不阻断，保留初稿
            info["rounds"].append({
                "round": round_no,
                "failed": True,
                "error": f"{type(exc).__name__}: {exc}",
            })
            break
        cont = _strip_thinking(cont or "").strip()
        added = char_count(cont)
        if added < 50:
            info["rounds"].append({"round": round_no, "rejected": "too_short", "chars": added})
            break
        if _echoes_existing(cont, text):
            info["rounds"].append({"round": round_no, "rejected": "echo", "chars": added})
            break
        candidate = text.rstrip() + "\n\n" + cont
        rep = analyze_repetition(candidate)
        if getattr(rep, "verdict", "") == "degenerate":
            info["rounds"].append({
                "round": round_no,
                "rejected": "degenerate",
                "chars": added,
                "reasons": list(getattr(rep, "reasons", ()) or ())[:2],
            })
            break
        text = candidate
        info["rounds"].append({
            "round": round_no,
            "chars_before": cur,
            "chars_after": char_count(text),
        })
    info["after_chars"] = char_count(text)
    return text, info


#: 正文末尾的「本节完」类 marker —— 见 `_strip_thinking` 里的变体清单。
#: 方括号/圆括号全半角都认；主体允许 本节/本章/全文/全篇 + 完/结束/终（含只写【完】）；
#: 也认无括号的「本节完」；尾部可带一个句号/叹号。
_SECTION_END_MARKER = re.compile(
    r"(?:\s*\n)*[ \t\u3000]*"
    r"(?:[【\[［〔(（]\s*(?:(?:本节|本章|全文|全篇)?(?:完|结束|终))\s*[】\]］〕)）]"
    r"|(?:本节|本章)(?:完|结束))"
    r"[。．.！!]?[ \t\u3000]*\Z"
)


def _strip_thinking(text: str) -> str:
    """剥掉 MiniMax M2 等模型的 思维链，只留正文。

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
    # 2026-11-10：有的模型还会把 constraints 回显到正文末尾（【本段已锁定角色】/
    # 【尚未兑现的伏笔】两行）。它们排在 marker 之后，先把这段回显整行剥掉，
    # 再交给 marker 正则，否则 marker 不在末尾、正则匹配不到（实测 p1 落库段落
    # 末三行就是 marker + 两行锁定名单）。
    # 回显行与 marker 可能交替出现（marker 在前、回显在后，或反过来），
    # 单趟剥不干净；反复「剥回显行 → 剥 marker」，直到不再变化。
    while True:
        before = text
        lines = text.rstrip().splitlines()
        while lines:
            last = lines[-1].strip()
            if not last:
                lines.pop()
                continue
            # 回显行也可能用半角方括号（[本段已锁定角色…），前缀统一剥掉再比。
            if last.lstrip("【[［〔(（").startswith(("本段已锁定角色", "尚未兑现的伏笔")):
                lines.pop()
                continue
            break
        text = _SECTION_END_MARKER.sub("", "\n".join(lines)).rstrip()
        if text == before:
            break
    # 2026-11-14：marker 不止【本节完】一种写法。第 16 章模型写的是**半角**
    # `[本节完]`，旧的单一半角不匹配，marker 行留进正文，`ends_complete` 看到
    # `]` 就把完整的一章判成截断。上面 `_SECTION_END_MARKER` 一次覆盖：
    #   括号： 【】 [ ] ［］ 〔〕 （） ( )（混用不处理）
    #   主体： 本节/本章/全文/全篇 + 完/结束/终；也允许只写【完】
    #   无括号：本节完 / 本章完（只在末尾出现时才剥）
    #   尾部： 可带句号/叹号（【本节完】。）
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


def _foreshadow_ledger_block(
    db, project_id: int
) -> tuple[str | None, list[str] | None]:
    """作者侧伏笔台账 → (提示词块, constraints 短句)。

    项目还没有任何台账行时返回 `(None, None)`：调用方据此保留旧
    `plot_bible.foreshadowing` 的回退行为（老项目零迁移）。
    台账存在但全部结案时返回 `("", [])` —— 显式空列表压掉旧字段，避免双权威。
    """
    from app.foreshadow_ledger import entries_of, open_lines, render_open_block

    entries = entries_of(db, project_id)
    if not entries:
        return None, None
    return render_open_block(entries), open_lines(entries)


def _era_guard_block(project: Project) -> tuple[str, dict]:
    """算时代越界的静态约束块。

    时代与国力是两条互不依赖的特性，各自独立部署：本函数**只**在自身作用域
    里 import 时代模块，模块缺失（单侧批次）时降级为「不生效」，不向调用方
    抛 `ModuleNotFoundError`，国力侧照常工作。
    """
    try:
        from app.kg.era_check import constraint_for
    except ImportError:
        return "", {
            "enabled": False,
            "status": "disabled",
            "reason": "era_guard_not_installed",
            "chars": 0,
        }
    return constraint_for(project)


def _power_guard_block(project: Project) -> tuple[str, dict]:
    """算量级体系的静态约束块（与时代侧对称，各自独立 import 与降级）。"""
    try:
        from app.generation.power_prompt import build_power_constraint
    except ImportError:
        return "", {
            "enabled": False,
            "status": "disabled",
            "reason": "power_guard_not_installed",
            "chars": 0,
        }
    power_block = build_power_constraint(project)
    return power_block, {"enabled": bool(power_block), "chars": len(power_block)}


def _guard_blocks(project: Project) -> tuple[str, dict, str, dict]:
    """生成前算好两段静态约束：时代越界 + 量级体系。

    返回 `(era_block, era_meta, power_block, power_meta)`。两个开关各自独立：
    任何一个关闭（或对应模块未部署），该侧 block 为空串，system 提示逐字节
    回到旧版；两侧互不牵连。
    """
    era_block, era_meta = _era_guard_block(project)
    power_block, power_meta = _power_guard_block(project)
    return era_block, era_meta, power_block, power_meta


def _attach_power_meta(db, gen: Generation, power_meta: dict,
                       kg_stats: dict | None) -> dict:
    """把哨兵的 `power` 摘要并进本段 meta（粗放型的称谓记录也在此落库）。

    哨兵在 commit 之后才跑（它要读刚落库的正文），所以这里补一次 summary 更新。
    失败只影响 meta，不影响正文——吞掉异常并保留原有 power 摘要。
    """
    power = dict(power_meta or {})
    sentinel = (kg_stats or {}).get("sentinel") or {}
    if sentinel.get("power"):
        power["audit"] = sentinel["power"]
    try:
        summary = dict(gen.summary or {})
        summary["power"] = power
        gen.summary = summary
        db.commit()
    except Exception as exc:  # noqa: BLE001 —— meta 落库失败不影响正文
        logging.getLogger(__name__).warning("power meta 落库失败: %s", exc)
    return power


def _apply_era_guard(text: str, project: Project, provider, segment_no: int,
                     reminders: dict) -> tuple[str, dict]:
    """生成后时代自检；默认 report（只扫描，不改正文）。

    时代模块未部署（只装国力的单侧批次）时直接返回原文，meta 标 disabled，
    不改正文、不抛 import 错。
    """
    try:
        from app.kg.era_check import apply_to_text
    except ImportError:
        return text, {
            "enabled": False,
            "status": "disabled",
            "reason": "era_guard_not_installed",
        }

    return apply_to_text(
        text, project, provider, segment_no=segment_no, reminders=reminders
    )


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

        # 事件表有书维度：项目挂两本书时只审当前书的事件，避免别书事件串进来。
        proj = db.get(Project, project_id)
        judge_res = judge_segment(db, project_id, text, segment_no,
                                  novel_id=(proj.active_novel_id if proj else None))
        # findings 必须落库（与哨兵同纪律）：只放单次响应里的告警，用户没盯着
        # 就永远丢了，事后无法复核。落库失败不阻断生成。
        try:
            save_judge_findings(db, project_id, judge_res.get("findings", []),
                                segment_no=segment_no,
                                novel_id=(proj.active_novel_id if proj else None))
        except Exception:  # noqa: BLE001
            pass
        stats["judge"] = judge_res
    except Exception as exc:  # noqa: BLE001
        stats["judge"] = {"error": str(exc)[:100], "findings": []}

    return stats


def _require_distill_ready(db, project) -> None:
    """生成前档案预检（2026-10-17，修 live 验证复现的挂起洞）。

    项目有参考语料、但叙事档案既不在项目缓存也不在文风档案库时，**明确 raise**
    而不是让生成链路走到 `_style_narrative → project_narrative → extract`：
    那条路会**同步**触发全量提炼（分钟级），HTTP 请求 240s 必超时，
    用户看到的是无限转圈、无从判断是卡死还是在跑（live 实测复现）。

    判据**不在本函数里**：直接问 `reusable_narrative`——与取档路径同一份规则。
    2026-11-12 修：此前这里自己写了一份「指纹必须完全相等」，而取档侧已放宽
    到「档案来源覆盖当前参考书即可复用」（refs 收窄后渲染侧会过滤掉多出的书）。
    预检先 raise，放宽的代码永远走不到，项目 5 收窄到 [5] 后真实生成 500。

    无参考语料的自由生成不受影响（project_narrative 对它返回 None，
    本来就不提炼）。

    Raises:
        ValueError: 需要先提炼。消息带明确可操作的下一步（端点路径）。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    if not ids:
        return
    from app.style.narrative import reusable_narrative, resolve_tier

    cfg = resolve_tier(getattr(project, "narrative_tier", None))
    if reusable_narrative(db, project, ids, cfg) is not None:
        return  # 项目缓存（含来源超集）或文风档案库命中；生成链路会直接复用
    raise ValueError(
        f"项目尚未提炼文风档案（当前档位：{cfg.name}）。请先调用 "
        f"POST /api/projects/{project.id}/distill 完成提炼后再生成——"
        "直接生成会同步触发分钟级提炼，请求必然超时。"
    )


def _quality_check(
    text: str,
    *,
    budget: int | None = None,
    is_rewrite: bool = False,
) -> tuple[dict | None, dict | None, list[str]]:
    """算质量报告：返回 (落库 payload, 响应 repetition, warnings)。

    三个字段各自独立：`repetition`（复读）、`length`（正文 vs 字数预算）、
    `summary_style`（梗概式推进）。只标记、不拦截。

    - `budget`：项目 `generation_length`。整段生成传它；缺失时篇幅判据为
      `undetected`，不会假装达标。
    - `is_rewrite`：改写既有段落的路径传 True，篇幅判据返回
      `not_applicable`（跌幅由 `detect_length_drop` 单独报告）。

    检测异常一律吞掉并返回 `(None, None, [])`——闸门绝不能拖垮生成；
    `None` 在列里就是 `NULL`（读取侧显示「未检测」），不是「正常」。

    `truncated` 与 verdict 独立：短文本（`skipped_short`）同样会因
    `ends_complete=False` 被标记为截断。
    """
    try:
        report = analyze_repetition(text or "")
        length = analyze_length(text or "", budget, is_rewrite=is_rewrite)
        style = analyze_summary_style(text or "")
        payload = quality_payload(
            report, length=length, summary_style=style)
    except Exception:  # noqa: BLE001 —— 判据层故障不影响正文落库
        logging.getLogger(__name__).warning(
            "质量检测失败，本段按未检测落库", exc_info=True)
        return None, None, []

    rep = payload["repetition"]
    warnings: list[str] = []
    if rep["verdict"] == "degenerate":
        warnings.append("疑似复读，建议重生成：" + rep["reason_text"])
    if payload["length"]["verdict"] == "short":
        warnings.append(payload["length"]["reason_text"])
    if payload["summary_style"]["verdict"] == "summary_style":
        warnings.append(
            "疑似梗概式推进，建议展开场景与对白："
            + payload["summary_style"]["reason_text"])
    if payload["truncated"]:
        warnings.append("结尾停在半句，可能是截断")
    return payload, rep, warnings


def _quality_json(payload: dict | None) -> str | None:
    """payload → `generations.quality_json` 的 TEXT；None 保持 NULL。"""
    return json.dumps(payload, ensure_ascii=False) if payload else None


def _is_degenerate(payload: dict | None) -> bool:
    return bool(
        payload
        and (payload.get("repetition") or {}).get("verdict") == "degenerate"
    )


def _coherence_report(db, project: Project, text: str,
                      chapter_range: str | None = None) -> dict | None:
    """跑通用一致性层，返回可写进 `quality_json["coherence"]` 的纯数据。

    与文风闸门并排、互不影响：本函数不读也不改 `passed`。开关与模式：

    - `coherence_guard_enabled=False` 或 `coherence_guard_mode="off"`：直接跳过；
    - `"report"`（默认）：只算、只记录；
    - `"rewrite"`：definite 级问题计入 `_rewrite_rounds` 的重写理由。

    判定输入全部取自项目自身：`outline`（逐章大纲）、`plot_bible`（角色与称谓）、
    本章之前的 `generations.text`、以及本项目自己产出的 `kg_entities`
    （`novel_id IS NULL` 的那批；参考书摄入的实体不属于本书设定，必须排除）。

    任何异常都吞掉返回 None——一致性层绝不能拖垮生成主流程。
    """
    mode = str(getattr(settings, "coherence_guard_mode", "report") or "report").lower()
    if not getattr(settings, "coherence_guard_enabled", True) or mode == "off":
        return None
    try:
        from app.coherence import chapter_index, run_coherence

        chapter = chapter_index(chapter_range)
        prior: list[dict] = []
        if chapter:
            rows = db.scalars(
                select(Generation)
                .where(Generation.project_id == project.id)
                .order_by(Generation.id.desc())
                .limit(4)
            ).all()
            for g in rows:
                idx = chapter_index(g.chapter_range)
                if idx is None or idx >= chapter:
                    continue
                prior.append({"chapter": idx, "text": g.text})
            prior.sort(key=lambda c: c["chapter"])
        entities = [
            str(name) for name in db.scalars(
                select(KgEntity.canonical_name).where(
                    KgEntity.project_id == project.id,
                    KgEntity.novel_id.is_(None),
                )
            ).all()
            if name
        ]
        report = run_coherence(
            text,
            plot_bible=project.plot_bible,
            outline=project.outline,
            chapter=chapter,
            prior_chapters=prior,
            entity_names=entities,
        )
        # 第三刀：权威越界 K1–K5。默认只报告（authority_rewrite_enabled=False），
        # 结果并入同一份 quality_json["coherence"]，同时在 authority 键留原始摘要。
        if getattr(settings, "authority_guard_enabled", True):
            from app.coherence_authority import run_authority_checks

            authority = run_authority_checks(db, project, text, chapter)
            report["findings"] = list(report.get("findings") or []) + authority["findings"]
            counts = report.setdefault("counts", {})
            counts["definite"] = int(counts.get("definite") or 0) + authority["counts"]["definite"]
            counts["suspected"] = int(counts.get("suspected") or 0) + authority["counts"]["suspected"]
            report["notes"] = list(report.get("notes") or []) + authority["notes"]
            report["authority"] = authority
        return report
    except Exception:  # noqa: BLE001 —— 一致性层故障不影响正文落库
        logging.getLogger(__name__).warning("coherence check skipped", exc_info=True)
        return None


def _coherence_reasons(db, project: Project, text: str,
                       chapter_range: str | None) -> list[str]:
    """`coherence_guard_mode="rewrite"` 时，把 definite 级问题翻成重写理由。

    默认模式返回空列表——即「只报不拦」，不改变既有重写判定。
    """
    mode = str(getattr(settings, "coherence_guard_mode", "report") or "report").lower()
    if mode != "rewrite":
        return []
    report = _coherence_report(db, project, text, chapter_range)
    if not report:
        return []
    # 权威类命中要先过 90% 精度标注闸：默认不进重写，即使 mode=rewrite。
    allow_authority = bool(getattr(settings, "authority_rewrite_enabled", False))
    return [
        f"一致性（{f['check']}，第{f['paragraph']}段）：{f['message']}"
        for f in report.get("findings") or []
        if f.get("level") == "definite"
        and (allow_authority or f.get("category") != "authority")
    ]


def _run_structure_check(project: Project, text: str, chapter: int | None,
                          ctx: dict) -> dict:
    """跑三项结构检测（情节点/越章/性别），结果只写 `quality_json`、不改正文。

    检测本身故障绝不能拖垮生成：任何异常都收敛成 `status=error`，
    `blocking=False`（宁可漏报一次，也不要因为检测器崩了让整章作废）。
    """
    try:
        cards, _ = merged_constraint_items(
            ctx.get("reminders") or {}, project.plot_bible)
        return check_structure(
            text,
            outline=project.outline,
            chapter=chapter,
            character_cards=[c for c in cards if isinstance(c, dict)],
            beat_min_coverage=float(getattr(
                settings, "structure_beat_min_coverage", 0.34)),
            beat_min_units=int(getattr(
                settings, "structure_beat_min_units", 3)),
            premature_lookahead=int(getattr(
                settings, "structure_premature_lookahead", 3)),
            premature_max_hits=int(getattr(
                settings, "structure_premature_max_hits", 3)),
            gender_max_mismatches=int(getattr(
                settings, "structure_gender_max_mismatches", 0)),
            gender_window=int(getattr(settings, "structure_gender_window", 12)),
            mode=str(getattr(settings, "structure_guard_mode", "report")),
            block_reasons=getattr(settings, "structure_block_reasons", None),
        )
    except Exception:  # noqa: BLE001 —— 检测器故障不影响生成
        logging.getLogger(__name__).warning(
            "structure check skipped（project %s）", getattr(project, "id", None),
            exc_info=True)
        return {"status": "error", "blocking": False, "enforced": False,
                "blocking_reasons": []}


def _ledger_prior_rows(db, project: Project,
                       chapter: int | None) -> list[dict]:
    """逐章推进台账用：当前章之前**已落库**的章节（含已落库的 structure_check）。

    章号口径与 `_chapter_rows` 一致（`_chapter_ordinal` 解析 `chapter_range`），
    只取 `< chapter` 的章节：当前章正文还没入库，由调用方直接传入。
    同章多段时后段覆盖前段，与「以最新落库为准」一致。
    """
    if db is None or chapter is None:
        return []
    try:
        rows = db.execute(
            select(Generation.chapter_range, Generation.text,
                   Generation.quality_json)
            .where(Generation.project_id == project.id)
            .order_by(Generation.segment_no)
        ).all()
    except Exception:  # noqa: BLE001 —— 台账读不到历史不能拖垮生成
        logging.getLogger(__name__).warning(
            "progress ledger history skipped（project %s）",
            getattr(project, "id", None), exc_info=True)
        return []
    by_chapter: dict[int, dict] = {}
    for label, text, quality in rows:
        idx = _chapter_ordinal(label)
        if idx is None or idx >= chapter or not str(text or "").strip():
            continue
        stored: dict | None = None
        if quality:
            try:
                parsed = json.loads(quality)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                stored = parsed.get("structure_check") or None
        by_chapter[idx] = {"chapter": idx, "text": text,
                           "structure_check": stored}
    return [by_chapter[idx] for idx in sorted(by_chapter)]


def _run_progress_ledger(db, project: Project, text: str,
                         chapter: int | None,
                         structure_check: dict | None) -> dict:
    """章末跑逐章推进台账，结果只写 `quality_json.progress_ledger`。

    **只判不拦**：`mode` 恒为 `report`，不改 `passed`、不进 `violations`、
    不改正文。台账按最近 `progress_ledger_window_chapters` 章（含本章）回看，
    历史章已落库的 `structure_check` 直接引用，当前章用刚算出的那一份。
    任何异常收敛成 `status=error`，不影响本章落库。
    """
    version = "progress-ledger-v1"
    if not bool(getattr(settings, "progress_ledger_enabled", True)):
        return {"version": version, "mode": "report", "status": "disabled"}
    if chapter is None:
        return {"version": version, "mode": "report", "status": "skipped",
                "reason": "非分章生成，无法对齐章号"}
    try:
        from app.generation.progress_ledger import (
            build_progress_ledger,
            chapter_view,
            resolve_config,
        )

        cfg = resolve_config()
        window = max(2, int(cfg.get("window_chapters") or 8))
        prior = _ledger_prior_rows(db, project, chapter)[-(window - 1):]
        rows = [{"chapter": r["chapter"], "text": r["text"]} for r in prior]
        rows.append({"chapter": chapter, "text": text})
        stored = {r["chapter"]: r["structure_check"] for r in prior
                  if r.get("structure_check")}
        if structure_check:
            stored[chapter] = structure_check
        ledger = build_progress_ledger(
            rows,
            plot_bible=project.plot_bible,
            outline=project.outline,
            structure_checks=stored,
        )
        return chapter_view(ledger, chapter)
    except Exception:  # noqa: BLE001 —— 台账故障不影响生成
        logging.getLogger(__name__).warning(
            "progress ledger skipped（project %s, chapter %s）",
            getattr(project, "id", None), chapter, exc_info=True)
        return {"version": version, "mode": "report", "status": "error",
                "chapter": chapter}


def _advance_block(db, project: Project, chapter: int | None) -> str:
    """本章提示里的「推进要求」：每条都对应台账一个可核字段；失败给空串。

    与章末台账同一套口径（角色卡名单 / 境界阶梯 / 通用动词），不写死任何
    名字或章号。没拿到历史时只写「属性」「衔接」两条，阵容要求宁可不写。
    """
    if chapter is None:
        return ""
    if not bool(getattr(settings, "progress_ledger_enabled", True)):
        return ""
    try:
        from app.generation.progress_ledger import (
            advance_requirements,
            resolve_config,
        )

        cfg = resolve_config()
        # 回看长度与「阵容停滞」共享同一个配置：提示要说的正是这条判据。
        history = max(2, int(cfg.get("cast_stagnant_chapters") or 20))
        prior = _ledger_prior_rows(db, project, chapter)[-history:]
        return advance_requirements(
            chapter=chapter,
            plot_bible=project.plot_bible,
            prior_chapters=[{"chapter": r["chapter"], "text": r["text"]}
                            for r in prior],
            config=cfg,
        )
    except Exception:  # noqa: BLE001 —— 提示增强失败不影响生成
        logging.getLogger(__name__).warning(
            "advance requirements skipped（project %s, chapter %s）",
            getattr(project, "id", None), chapter, exc_info=True)
        return ""


def _short_sentence_check(metrics: dict, gate_limits_meta: dict) -> dict:
    """v17「短句占比偏低」：**只判不拦**，结果写 `quality_json.short_sentence_check`。

    **口径层级 = 章级**（写成章级就必须整条链都是章级）：

    - 实测量：`generation_gate(text)["metrics"]["短句占比"]`，即**整章正文**切句后
      「≤4 字短句数 ÷ 总句数」（`stylometry` 的短句定义），与参考书同一把尺。
    - 参考线：`gate_limits.chapter_percentiles["短句占比"]`。它来自
      `_archive_gate_limits` 对参考书**每一整章**文本跑同一函数后取分位，
      是**章级**分布；**不是**块级 `ARCHIVE_BLOCK_PERCENTILES`（1,200 字块），
      **也不是**卷级 `rhythm_reference["短句占比"]`（连续 `volume_chapters` 章的块均值）。
      三者不可混用：章级实测量只能对章级分布，p10 的定义就是「参考书约 10% 的章
      低于它」，命中率基线因此是 10% 而不是 0；拿块级/卷级 p10 套单章会把命中率
      系统性抬高，结论会错。
    - 退回链（只在档案不可用时触发，`source` 逐条写明）：章级分位
      （`archive_chapter`）→ `settings.short_sentence_ratio_reference`（`config`）
      → 块级常量表（`constant`）。最后一级是块级口径，只能当粗兜底，
      不要拿它的命中率与章级基线对照。

    本判定只记录：不改 `passed`、不进 `violations`、不触发重写、不中断生成。
    """
    row = (gate_limits_meta.get("chapter_percentiles") or {}).get("短句占比") or {}
    p10, p50, p90 = row.get("p10"), row.get("p50"), row.get("p90")
    source = "archive_chapter"
    if not all(isinstance(v, (int, float)) for v in (p10, p50, p90)):
        configured = getattr(settings, "short_sentence_ratio_reference", None)
        if configured and len(configured) == 3:
            p10, p50, p90 = (float(x) for x in configured)
            source = "config"
        else:
            static = ARCHIVE_BLOCK_PERCENTILES["短句占比"]
            p10, p50, p90 = (
                float(static["p10"]), float(static["p50"]), float(static["p90"]))
            source = "constant"
    raw = metrics.get("短句占比")
    value = float(raw) if isinstance(raw, (int, float)) else None
    return {
        "mode": "report",
        "level": "chapter",
        "metric": "短句占比",
        "value": None if value is None else round(value, 4),
        "p10": round(float(p10), 4),
        "p50": round(float(p50), 4),
        "p90": round(float(p90), 4),
        "source": source,
        "chapters": (
            gate_limits_meta.get("chapters") if source == "archive_chapter" else None
        ),
        "below_p10": None if value is None else bool(value < float(p10)),
        "note": "只判不拦：不影响 passed，不进 violations，不触发重写",
    }


def _sentence_shape_check(text: str, metrics: dict, gate_limits_meta: dict) -> dict:
    """v19「句长形状」：**只判不拦**，写 `quality_json.sentence_shape_check`。

    量的是**分布形状**，不是某一端的占比：

    - `crowd`（中段拥挤度）：落在参考书**池化 p25–p75** 窄带内的句子占比。
      上限 `gate_limits.sentence_shape.crowd_max` = 该指标的参考书章级 p90。
      为什么用它：句长均值/长句占比双双"正常"、短句占比却塌，说明句子被挤到
      中段——拥挤度直接量这件事，且不受篇幅与切句口径影响。
    - `long_ratio`（上尾）：≥40 字句占比，下限 = 参考书章级 p10。
      两个候选（p95 / ≥40 字占比）里只取这一个，避免一次上三条判据后
      出问题分不清是哪条。

    `CV` 下限本次不上（用户 2026-11-15 指定）。两项都只记录：不改 `passed`、
    不进 `violations`、不触发重写。参考带推不出来时 `source="no_reference"`，
    不判也不臆造。

    v21（2026-11-15）：新增 `verdict`——把两项**联判**。上尾没进带时
    `crowd_conclusion` 记 `voided`（`voided_by="long_ratio_below_floor"`），
    不再单独给「不拥挤」；`crowded` 仍照旧成立（中段堆压与上尾长度是两件事）。
    """
    shape = gate_limits_meta.get("sentence_shape") or {}
    band = shape.get("band") or {}
    lo, hi = band.get("p25"), band.get("p75")
    lens = stylometry.sentence_lens(text or "")
    crowd = None
    if lens and isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        crowd = sum(1 for x in lens if lo <= x <= hi) / len(lens)
    crowd_max = shape.get("crowd_max")
    long_ratio = metrics.get("长句占比")
    long_floor = shape.get("long_ratio_floor")
    source = shape.get("source") or "no_reference"
    # v21：拥挤度与上尾**联判**。上尾（≥40 字占比）没进带时，「不拥挤」不能单独成立
    # ——句子整体被压短会让中段占比机械地降下来，那不是分布均衡。
    # 只加结论字段：`crowded` / `not_crowded` / `voided`；仍是只判不拦。
    if crowd is None:
        crowd_conclusion = None
    elif isinstance(crowd_max, (int, float)) and crowd > float(crowd_max):
        crowd_conclusion = "crowded"
    elif (
        isinstance(long_floor, (int, float))
        and isinstance(long_ratio, (int, float))
        and float(long_ratio) < float(long_floor)
    ):
        crowd_conclusion = "voided"
    else:
        crowd_conclusion = "not_crowded"
    return {
        "mode": "report",
        "level": "chapter",
        "scope": stylometry.SENTENCE_METRIC_SCOPE,
        "band": {"p25": lo, "p75": hi},
        "sentences": len(lens),
        "crowd": {
            "value": None if crowd is None else round(crowd, 4),
            "max": crowd_max,
            "exceeded": (
                None if (crowd is None or not isinstance(crowd_max, (int, float)))
                else bool(crowd > float(crowd_max))
            ),
        },
        "long_ratio": {
            "value": (
                round(float(long_ratio), 4)
                if isinstance(long_ratio, (int, float)) else None
            ),
            "min": long_floor,
            "below": (
                None
                if not isinstance(long_ratio, (int, float))
                or not isinstance(long_floor, (int, float))
                else bool(float(long_ratio) < float(long_floor))
            ),
        },
        "source": source,
        "chapters": shape.get("chapters"),
        "verdict": {
            "crowd_conclusion": crowd_conclusion,
            "voided_by": (
                "long_ratio_below_floor" if crowd_conclusion == "voided" else None
            ),
            "rule": "上尾低于下限时，crowd_conclusion 不记 not_crowded",
        },
        "note": "只判不拦：不影响 passed，不进 violations，不触发重写",
    }


def _dialogue_ratio_check(project: Project, metrics: dict, gate_limits_meta: dict) -> dict:
    """v17「对白配比」：**只判不拦**，写 `quality_json.dialogue_ratio_check`。

    **口径层级 = 章级**：实测量是整章正文的「对白字 ÷ 汉字」；参考线取
    `gate_limits.chapter_percentiles["对白字占比"]` 的 p25/p50/p75——由
    `_archive_gate_limits` 对参考书**每一整章**实测后取分位。**不是**卷级
    `dialogue_reference`（连续卷块的均值，提示线用它），**也不是**块级常量表。
    p75 的定义是「参考书约 25% 的章高于它」，所以命中率基线是 25% 而非 0；
    拿卷级/块级 p75 套单章会让命中率偏移，无法与 25% 对照。

    退回链（只在档案不可用时触发，`source` 逐条写明）：章级分位
    （`archive_chapter`）→ `settings.dialogue_ratio_reference`（`config`）
    → 块级常量（`constant`）。后两级只作粗兜底，不参与 25% 基线判读。

    本判定只记录：不改 `passed`、不进 `violations`、不触发重写、不中断生成。
    """
    row = (gate_limits_meta.get("chapter_percentiles") or {}).get("对白字占比") or {}
    p25, p50, p75 = row.get("p25"), row.get("p50"), row.get("p75")
    source = "archive_chapter"
    chapters = gate_limits_meta.get("chapters")
    if not all(isinstance(v, (int, float)) for v in (p25, p50, p75)):
        ref = _dialogue_reference_fallback(project)
        p25, p50, p75 = ref["p25"], ref["p50"], ref["p75"]
        source = ref["source"]
        chapters = None
    raw = metrics.get("对白字占比")
    value = float(raw) if isinstance(raw, (int, float)) else None
    return {
        "mode": "report",
        "level": "chapter",
        "metric": "对白字占比",
        "value": None if value is None else round(value, 4),
        "p25": round(float(p25), 4),
        "p50": round(float(p50), 4),
        "p75": round(float(p75), 4),
        "source": source,
        "chapters": chapters,
        "above_p75": None if value is None else bool(value > float(p75)),
        "note": "只判不拦：不影响 passed，不进 violations，不触发重写",
    }


def _era_channel_abuse(db, project, era_meta: dict) -> dict:
    """通道滥用聚合（v18，只判不拦）：单章阈值 + 连续章。

    历史取自**已落库**的相邻章节（`quality_json.era_check.record_counts`），
    本章尚未落库，当前计数直接取本次扫描的 meta。阈值全部走配置。
    """
    from app.kg.era_check import channel_abuse

    lookback = int(getattr(settings, "era_channel_abuse_lookback", 8) or 0)
    history: list[dict] = []
    if lookback > 0 and db is not None and getattr(project, "id", None):
        rows = (
            db.query(Generation)
            .filter(Generation.project_id == project.id)
            .order_by(Generation.segment_no.desc())
            .limit(lookback)
            .all()
        )
        for row in reversed(rows):
            try:
                payload = json.loads(row.quality_json or "{}")
            except (TypeError, ValueError):
                continue
            history.append((payload.get("era_check") or {}).get("record_counts") or {})
    return channel_abuse(
        era_meta.get("record_counts") or {},
        history,
        threshold=int(getattr(settings, "era_channel_abuse_threshold", 3) or 0),
        streak=int(getattr(settings, "era_channel_abuse_streak", 2) or 0),
    )


def _era_check_quality(era_meta: dict, prompt_meta: dict, abuse: dict | None = None) -> dict:
    """v17/v18：时代越界扫描留痕（`mode=report`，只判不拦）。

    扫描本身由 `era_guard` 完成（`_apply_era_guard`），这里只把结果并进
    `quality_json.era_check`。`status`：`active` / `baseline_unknown`（判不出基线）/
    `disabled`（总开关关）/ `scan_disabled`（扫描开关关）。

    `prompt_injected` 来自**约束注入开关**的 meta，回答「本章 system 里到底加没加
    那段时代约束（`prompt_chars`）」——两个开关拆开后，命中率变化才能归因。
    `records` 是通道命中但缺引出人的降级记录（`channel_hit_no_introducer`），
    只留痕、不进 `violation_counts`。

    v18 新增两项：`violation_notes` 把 `needs_authorization` 按来源拆开
    （前现代档位 `period_mismatch` / 通道未登记 `unregistered_origin`）；
    `channel_abuse` 是通道留痕的单章/连续章聚合告警。

    只记录：不改正文、不影响 `passed`、不进 `violations`。
    """
    return {
        "mode": "report",
        "enabled": bool(era_meta.get("enabled")),
        "status": era_meta.get("status"),
        "baseline": era_meta.get("baseline"),
        "baseline_source": era_meta.get("baseline_source"),
        "mentions": era_meta.get("mentions"),
        "violation_counts": era_meta.get("counts") or {},
        "violation_notes": era_meta.get("note_counts") or {},
        "samples": list(era_meta.get("violations") or [])[:5],
        "records": list(era_meta.get("records") or [])[:5],
        "record_counts": era_meta.get("record_counts") or {},
        "channel_abuse": abuse or {},
        "prompt_injected": bool(prompt_meta.get("prompt_injected")),
        "prompt_chars": int(prompt_meta.get("chars") or 0),
        "prompt_status": prompt_meta.get("status"),
        "note": "只判不拦：不改正文、不影响 passed、不进 violations",
    }


_SNAPSHOT_VERSION = 1
#: 单段提示词快照上限（字符）。超长截断并标 `truncated`，但 sha256 仍按全文算，
#: 所以「当时用的是不是同一份提示词」仍可比对。
_SNAPSHOT_MAX_CHARS = 400_000


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prompt_snapshot(system: str, user: str) -> str:
    """初稿实际提示词快照（JSON 文本），写进 `generations.prompt_json`。"""
    full = system + "\n\n" + user
    truncated = len(full) > _SNAPSHOT_MAX_CHARS
    return json.dumps(
        {
            "version": _SNAPSHOT_VERSION,
            "system": system if not truncated else system[:_SNAPSHOT_MAX_CHARS],
            "user": user if not truncated else "",
            "chars": len(full),
            "truncated": truncated,
            "sha256": _sha_text(full),
            "captured_at": time.time(),
        },
        ensure_ascii=False,
    )


def _authority_snapshot(project: Project, ctx: dict, chapter_range: str | None) -> str:
    """当次权威输入快照（JSON 文本），写进 `generations.authority_json`。

    存「能回答当时看到哪一版权威」的最小集合：plot_bible 全文（体量小）、
    outline/premise/叙事档案的哈希与长度、以及进入上下文的角色名单。
    不复制 outline 全文——它在 user 提示词快照里已有摘录，重复存会撑大库。
    """
    narrative = project.style_narrative if isinstance(project.style_narrative, dict) else {}
    outline = project.outline or []
    constraints = ctx.get("constraints") or {}
    characters = narrative.get("characters") or []
    return json.dumps(
        {
            "version": _SNAPSHOT_VERSION,
            "chapter_range": chapter_range,
            "plot_bible": project.plot_bible,
            "premise": project.premise,
            "premise_sha256": _sha_text(project.premise or ""),
            "outline_len": len(outline),
            "outline_sha256": _sha_text(json.dumps(outline, ensure_ascii=False)),
            "narrative": {
                "version": narrative.get("version"),
                "sha256": _sha_text(
                    json.dumps(narrative, ensure_ascii=False, sort_keys=True)
                ),
                "character_names": [
                    c.get("name")
                    for c in characters
                    if isinstance(c, dict) and c.get("name")
                ][:40],
            },
            "style_source_ids": sorted(_style_source_ids(project) or []),
            "constraint_keys": sorted(constraints.keys()),
            "characters_in_context": list(constraints.get("characters") or [])[:40],
            "captured_at": time.time(),
        },
        ensure_ascii=False,
    )


def _record_segment_contract(
    project: Project,
    segment_no: int,
    label: str,
    prompt_json: str | None,
    authority_json: str | None,
    generation_id: int,
) -> dict | None:
    """把本段「当时的节点 + 权威」冻结成段契约，写进 `plot_timeline.planning`。

    与 `generations.prompt_json / authority_json` 的分工：

    - 那两列存**完整输入**（用于回放「当时模型看到了什么」）；
    - 这里只存验收/重判需要的轻量指针：节点快照、权威 premise、prompt 哈希、
      `generation_id`。不复制 prompt 全文，避免 `plot_timeline` 随章节线性膨胀。

    冻结的是**落库这一刻**的节点字段（title/entry_condition/exit_condition/
    beats…），之后重规划不会改写到历史段契约——这正是
    `body_consistency` / `derived_rebuild` 读取 `segment_contracts` 的前提。

    没有时间线（未规划、旧项目）时返回 None，不阻断生成。
    """
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not saved:
        return None
    st = state_from_dict(
        saved,
        total_segments=_total_segments(project),
        total_chapters=len(_outline_entries(project)) or None,
    )
    if st is None:
        return None
    chapter = _chapter_ordinal(label)
    node_id = (node_for_chapter(st, chapter) if chapter else None) or st.current_node
    if not node_id or node_id not in st.nodes:
        return None

    frozen = state_to_dict(st)["nodes"][node_id]
    contract = {
        "node_id": node_id,
        "generation_id": generation_id,
        "chapter_range": label,
        # 完整 prompt 在 generations.prompt_json；这里只留可校验的指纹。
        "prompt_sha256": _sha_text(prompt_json) if prompt_json else None,
        "authority_sha256": _sha_text(authority_json) if authority_json else None,
        "authority": {
            "premise": project.premise,
            "causal_node": frozen,
        },
        "captured_at": time.time(),
    }
    contracts = dict(st.segment_contracts)
    contracts[str(segment_no)] = contract
    st.segment_contracts = contracts
    project.plot_timeline = state_to_dict(st)
    return contract


def _commit_segment(db, project: Project, text: str, summary: str,
                    meta: dict | None = None,
                    quality: dict | None = None,
                    chapter_range: str | None = None,
                    prompt_json: str | None = None,
                    authority_json: str | None = None) -> tuple[Generation, int]:
    """落库一段：写 Generation + 追加 L1 到摘要树 + 同步扁平归档。

    `project.summary` **继续维护**，但它已从「注入源」退为「L1 无损归档」：
    前端/旧接口仍读它，而注入提示词的是 compose_prior 拼的有界前情块。
    两份都写不构成双写风险——它们由同一次 record_l1 派生，且树才是权威。

    `meta` 是纯新增的旁路键（如 `era` / `power` 自检摘要），写进
    `Generation.summary` 的兄弟键，不改 `{"text": ...}` 的既有读法。

    章身份：`chapter_range` 非空时必须是 `chapter_labels.resolve()` 产出的
    合法标签，否则在写库前抛 `ChapterIdentityError`；`None` 表示调用方明确
    走段模式（写 `段N`），**不是**「解析失败后用段号顶替」。
    """
    segment_no = _next_segment_no(db, project.id)
    if chapter_range is None:
        label = f"段{segment_no}"
    else:
        _require_chapter_label(
            chapter_range, segment_no=segment_no, where="_commit_segment")
        label = chapter_range
    # 章身份旁路：只有 `第N章` 标签才写 `chapter_meta`（段标签返回 None）。
    # 这是「outline 取不到标题」唯一的可见化出口：`title_source="missing_outline"`
    # 说明该章在 project.outline 里没条目，而不是被悄悄写成无标题章。
    chapter_meta = _chapter_commit_meta(project, label)
    if chapter_meta is not None:
        quality = {**(quality or {}), "chapter_meta": chapter_meta}
    # 一致性层：与文风闸门并排，写进 quality_json 的独立键。
    # 只加键、不改既有键的语义，老数据缺 `coherence` 时读取侧判 None 即可。
    if quality is not None:
        coherence = _coherence_report(db, project, text, label)
        if coherence is not None:
            quality = {**quality, "coherence": coherence}
    # v15：结构阻断只在这一处生效。`report` 模式只留痕照常落库；
    # `block` 模式在写库前抛出，本章不落库（fail-fast，由调用方决定重试/换章）。
    structure = (quality or {}).get("structure_check") or {}
    if structure.get("enforced"):
        raise StructureBlocked(
            "结构检测阻断落库：" + "；".join(
                structure.get("enforced_reasons")
                or structure.get("blocking_reasons")
                or []
            ))
    # 篇幅硬上限：压缩轮（`_rewrite_rounds` 末尾）跑完仍超 120% ⇒ 不落正式正文。
    # 下限（<80%）仍按既有口径只留痕，不在这里拦——本次只补上限这一侧。
    length_evidence = _length_block_evidence(project, text, quality)
    if length_evidence is not None:
        raise LengthBlocked(
            segment_no=segment_no, label=label, evidence=length_evidence)
    gen = Generation(
        project_id=project.id,
        segment_no=segment_no,
        chapter_range=label,
        text=text,
        summary={"text": summary, **(meta or {})},
        quality_json=_quality_json(quality),
        prompt_json=prompt_json,
        authority_json=authority_json,
    )
    db.add(gen)
    # flush 拿 generation_id 写段契约；契约失败不能拖垮已完成的正文落库。
    db.flush()
    try:
        _record_segment_contract(
            project, segment_no, label, prompt_json, authority_json, gen.id
        )
    except Exception:  # noqa: BLE001 —— 契约是旁路，生成主流程不能被它拖垮
        logging.getLogger(__name__).warning("segment contract skipped", exc_info=True)

    tree = _summary_tree(project)
    record_l1(tree, segment_no, summary)
    project.summary_tree = tree

    running = list(project.summary) if isinstance(project.summary, list) else []
    running.append(summary)
    project.summary = running
    project.status = "generating"
    db.commit()
    db.refresh(gen)

    # 文风一致性校验 · 第一阶段（L1 影子记录）：正文已落库之后才写一条记录。
    # 只记录、不判定、不改正文；record_segment_check 内部吞掉所有异常，
    # 这里再包一层是防 import 失败——校验器绝不能拖垮生成主流程。
    try:
        from app.style.consistency import record_segment_check

        record_segment_check(db, project, gen)
    except Exception:  # noqa: BLE001 —— 校验器故障不影响生成
        logging.getLogger(__name__).warning(
            "style shadow check skipped", exc_info=True
        )
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


def build_context(db, project: Project, novel_ids: list[int] | None = None,
                  fresh_start: bool = False) -> dict:
    """组装生成所需的全部上下文（不调用 LLM，供 plan 预览与续写共用）。

    `fresh_start=True`：正式开篇。忽略已写段落的摘要链、生成侧图谱提醒与
    KG 切片，只保留设定（plot_bible/premise）、风格与参考语料检索——用于
    「13 段试验正文只作文风参考、不承接情节」之后的第一章。

    novel_ids 缺省时取 project.reference_novel_ids（用户选定的参考语料）。
    只检索「选定」的语料，绝不混入整库其他语料——这是防混库的第一道闸。
    多本语料各自独立 RAG，结果合并进 rag_context（纯文本检索，不污染图谱隔离）。

    ## 正文修订闸门（批 9 接线）

    正文被改过（提案写入 `generations.text`）⇒ 摘要链、时间线完成证据、
    派生 KG / 事实**全部失效**。在 `agents/derived_rebuild.rebuild_derived()`
    重建并复核完之前，这里必须挡住生成（409），否则新正文会和旧派生状态
    一起喂进提示词——那是「越改越乱」的来源。

    闸门**只在 `body_revision_requires_review` 存在时生效**：
    没有改过正文的项目行为与之前逐字节一致（fail open 到旧路径）。
    """
    if (project.plot_timeline or {}).get("body_revision_requires_review"):
        from fastapi import HTTPException

        raise HTTPException(409, detail={
            "status": "needs_review",
            "message": "正文已修订，摘要与时间线完成证据已失效，须复核后再生成",
            "revision": project.plot_timeline["body_revision_requires_review"],
        })

    # 只取「用户自己小说」的图谱（generated），杜绝参考语料角色混入事实约束
    if fresh_start:
        reminders = {"unresolved_plotholes": [], "character_constraints": []}
        prior = []
        tree = empty_tree()
    else:
        reminders = get_reminders(db, project.id, source="generated")
        prior = _prior_summaries(project)
        tree = _summary_tree(project)

    # 作者侧伏笔台账：存在即接管旧 foreshadowing（口径见 prompts.merged_constraint_items）。
    foreshadow_block, ledger_lines = _foreshadow_ledger_block(db, project.id)
    if ledger_lines is not None:
        reminders["foreshadow_ledger"] = ledger_lines

    # 前情块**有界**：分层 + 预算裁剪，且带覆盖证明（report["uncovered"] 恒空）。
    # 预算随单段正文长度走——正文越长，前情允许多占一点，但比例固定，不随段数漂移。
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
    if _next_segment_no(db, project.id) == 1:
        # 首段：system 里刚藏起伏笔清单，尾部近因提醒也不能原样再列一遍，
        # 否则等于「一边藏一边漏」。角色锁定保留（首段仍需人物一致）。
        # 用「有没有已写段落」判定，而不是「摘要链是否为空」：老项目可能缺摘要，
        # 但已写过正文就不该再按首段处理 constraints。
        constraints["plotholes"] = []

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
        # 作者侧伏笔块：None = 项目还没有台账（保留旧 foreshadowing 回退）。
        "foreshadow_block": foreshadow_block,
        "novel_ids": novel_ids,
        "constraints": constraints,
        # v3.0 KG 切片：按当前剧情点动态取的近期实体/状态/伏笔/情报/事件。
        # 空库（未抽过）时为空串，build_user_prompt 跳过该块。
        "kg_slice": "" if fresh_start else _kg_slice_text(db, project),
        # 物品持有事实：本章涉及物品「现在在谁手上」（只读 generated.db）。
        # 关掉 settings.kg_item_holding_inject 或取不到时为空串，prompt 与旧版一致。
        "item_holding_block": "" if fresh_start else _item_holding_text(
            db, project, _next_segment_no(db, project.id) - 1
        ),
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


def _enqueue_v3_extraction(
    db, project: Project, segment_no: int, text: str,
    *, chapter: int | None = None, rebuild: bool = False,
) -> dict:
    """段 commit 后入队 v3.0 异步批抽（extractor_v3 落 v3.0 表）。

    与 v2.6 同步合并（_merge_segment_into_graph）并存：同步合并喂 reminders（本段
    立即可见），v3.0 批抽落 9 张表（供 Judge/检索切片/读侧 API）。
    零 LLM、零阻塞：只落一行 pending 入队行，抽取由常驻 worker 认领。

    ``chapter``：本段所属**故事章号**，由调用方从 generation.chapter_range 派生
    （``_chapter_ordinal``）。派生不到（段标签）时**不再回退段号**：段号不是章号，
    用它入队会把 `段20` 写成第 20 章，污染事件/状态/伏笔的按章归属。此时按
    ``chapter=None`` 入队（列可空），只抽不分章；章标签补齐后用 ``rebuild`` 重放。

    这里**不启动 worker、也不做超时重捞**：写章进程是短命的（每章一个 CLI
    进程，落库后立刻退出），在这里起 worker 只会「认领一行、还没抽完就随进程
    死掉」，在这里重捞则会把行认领给一个马上要死的 owner。抽取与重捞都由
    常驻服务（uvicorn）里的 worker 负责——它扫 pending 行认领，周期扫描捞
    stuck 行（见 ``batch_worker._discover_pending`` / ``_sweep_stuck``）。

    失败**不阻断生成主链，但必须报出去**：入队行写不进库时 `enqueue_segment`
    抛 `KgEnqueueError`，这里转成 `{"enqueued": False, "error": ...}` 返回给调用方
    （随响应下发），并记 error 日志——不再只 warning 一句就吞掉。
    """
    if chapter is None:
        # 段模式没有章号：按空章号入队（只抽不分章），绝不把段号当章号。
        logging.getLogger(__name__).warning(
            "v3 批抽章号缺失（project %s seg %s）：按 chapter=None 入队，"
            "不用段号冒充章号", project.id, segment_no)
    out: dict = {"enqueued": False}
    try:
        from app.kg import batch_worker as bw

        bw.enqueue_segment(db, project.id, segment_no, text, chapter=chapter,
                           rebuild=rebuild)
        out = {"enqueued": True, "pending": bw._queue.pending_count(project.id)}
    except Exception as exc:  # noqa: BLE001 —— 批抽是增强，失败不阻断
        logging.getLogger(__name__).error(
            "v3 批抽入队失败（project %s seg %s）：%s", project.id, segment_no, exc,
            exc_info=True)
        out = {"enqueued": False, "error": str(exc)[:200]}
    return out


def _shadow_ingest(project: Project, segment_no: int, text: str) -> dict:
    """KG 分层影子抽取（默认关闭）。

    与 v3 批抽、v2.6 合并完全并行：写独立 sqlite，不注入 prompt，不改任何
    现有返回值；未启用时零开销直接返回，异常静默兜底。
    """
    try:
        from app.kg import shadow

        return shadow.submit(project.id, text, segment_no)
    except Exception as exc:  # noqa: BLE001 —— 影子抽取失败不阻断生成
        return {"queued": False, "error": str(exc)[:100]}


def _active_character_names(db, project: Project, current_seg: int) -> list[str]:
    """本段涉及的角色名（mention 反查）。空则用 plot_bible 角色名兜底。

    按 **prompt 书白名单** 收窄，与 retrieval_v3 同口径：原创项目只认项目级
    提及（``novel_id IS NULL``），参考书语料在段号上的同号提及不得混进来。
    参考书章节号与生成段号共用同一列，不收窄会把别书人物当成「本章角色」。
    """
    try:
        from app.models import KgMention, KgEntity
        from sqlalchemy import select

        scope = scope_sql_values(prompt_scope_book_ids(project))
        ent_ids = {
            m.entity_id for m in db.scalars(
                select(KgMention).where(
                    KgMention.project_id == project.id,
                    KgMention.segment_no == current_seg,
                    # 同章旧稿的提及只留档，不进 prompt（否则重写章新旧两版同时出场）
                    KgMention.is_current.is_(True),
                    func.coalesce(KgMention.novel_id, -1).in_(scope),
                )
            )
        }
        names = []
        for eid in ent_ids:
            ent = db.get(KgEntity, eid)
            # 第二道门看**实体自身**的书：老提及行的 novel_id 可能是 NULL（写书维度
            # 之前落的），实体却属于参考书——只按提及过滤会把别书人物当本章角色。
            if (ent is not None and ent.kind == "character"
                    and (ent.novel_id if ent.novel_id is not None else -1) in scope):
                names.append(ent.canonical_name)
        if names:
            return names
        # 兜底：plot_bible 里的角色名
        bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
        return [c.get("name") for c in (bible.get("characters") or [])
                if isinstance(c, dict) and c.get("name")]
    except Exception:  # noqa: BLE001
        return []


def _active_item_entries(db, project: Project, current_seg: int) -> list[dict]:
    """本段提及的物品（``{"name", "item_id"}``）。一次批量查询，不逐条 get。

    书维度落在 ``kg_mentions.novel_id``：一律按 **prompt 书白名单** 取
    「本书的提及 + ``novel_id IS NULL`` 的项目级通配提及」；原创项目白名单只有
    ``NULL``，参考书语料在同段号上的提及不参与（与 retrieval_v3 同口径）。
    提及是**段级**事实，段属于哪本书由写侧落库时写死，这里不反推。
    """
    try:
        from app.models import KgEntity, KgMention

        scope = scope_sql_values(prompt_scope_book_ids(project))
        stmt = select(KgMention).where(
            KgMention.project_id == project.id,
            KgMention.segment_no == current_seg,
            # 同章旧稿的提及只留档，不进 prompt
            KgMention.is_current.is_(True),
            func.coalesce(KgMention.novel_id, -1).in_(scope),
        )
        ent_ids = sorted({m.entity_id for m in db.scalars(stmt)})
        if not ent_ids:
            return []
        rows = db.execute(
            select(KgEntity.id, KgEntity.canonical_name).where(
                KgEntity.id.in_(ent_ids),
                KgEntity.kind == "item",
                # 实体自身书维度：老提及行 novel_id 可能缺失，不能只信提及行
                func.coalesce(KgEntity.novel_id, -1).in_(scope),
            )
        ).all()
        return [{"name": name, "item_id": ent_id}
                for ent_id, name in rows if name]
    except Exception:  # noqa: BLE001 - 物品清单取不到就不注入，不阻断生成
        return []


def _item_holding_text(db, project: Project, current_seg: int) -> str:
    """本章物品「现在在谁手上」的 prompt 文本；失败/关闭时返回空串。"""
    if not settings.kg_item_holding_inject or current_seg < 1:
        return ""
    items = _active_item_entries(db, project, current_seg)
    if not items:
        return ""
    try:
        from app.kg.item_holding_read import (
            current_item_holdings,
            format_holding_facts,
        )

        facts = current_item_holdings(
            items, db=db, project_id=project.id,
            generated_db=settings.kg_generated_db,
            novel_id=_book_for_project(project),
        )
        return format_holding_facts(facts)
    except Exception:  # noqa: BLE001 - 读通道是增强，取不到不阻断生成
        logging.getLogger(__name__).warning(
            "物品持有事实读取失败（project %s seg %s）", project.id, current_seg,
            exc_info=True)
        return ""


def _style_info(project: Project) -> dict | None:
    """把向导选的 类型/风格平台 收成 style_dims 形态，喂给系统提示的【风格】块。"""
    info: dict = {}
    if project.genre:
        info["类型"] = project.genre
    if project.style:
        info["风格平台"] = project.style
    return info or None


def _catalog_skeleton(db, project: Project) -> str | None:
    """静态目录骨架（§5.12.1）：按项目参考书找到档案 → 已缓存的骨架文本。

    骨架是导航增强，不是生成前提：未建卡 / 无参考书 / 读缓存失败都返回 None，
    由提示词层整块跳过，绝不阻断正文生成。
    """
    from app.retrieval.skeleton import skeleton_for_project

    return skeleton_for_project(db, project)


def _roleplay_character_name(project: Project, reminders: dict | None = None) -> str | None:
    """角色聊天时，被扮演的是哪个角色。

    取值顺序（**每一步都是真实存在的来源**）：
    1. `plot_bible.roleplay_character` —— 用户在界面上选定的那个；
    2. `plot_bible.characters` 的第一个 —— 用户自己写进剧情圣经的角色；
    3. **图谱抽出来的角色**（`reminders["character_constraints"]`）的第一个。

    ## 为什么必须有第 3 步

    之前只查前两步 ⇒ 真跑（北荒刀歌续写验证项目）返回 **None** ⇒
    **角色定向检索根本没跑**，`rag_items` 0 条 ⇒ 角色「什么都不知道」，
    只能编。而那个项目的角色（陆长风 / 陆崇 / 老周）明明在界面上的
    【角色人设约束】里 —— 它们来**图谱抽取**，不在 plot_bible 里。
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

    零模型调用；按 (novel_id, 档案指纹) 进程内缓存。
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

    `persist_library=False`（P1-1 修复）：生成链路每次请求会调本函数 3 次
    （build_context + generate_next_segment/stream 各 2 次），命中缓存时
    只读返回即可——不算 corpus_content_hash（全量加载语料+sha256）、
    不写文风档案库。落库是提炼提交路径（POST /distill）的职责。
    """
    from app.style.narrative import attach_rule_weights, project_narrative_safe

    profile = project_narrative_safe(db, project, persist_library=False)
    # 第四层反馈权重：挂到副本上（不改项目缓存），渲染选条时乘进总分。
    return attach_rule_weights(db, profile, getattr(project, "id", None))


#: 关系类型优先级（R36-3，2026-11-12 用户定）：亲属/同门 > 敌对 > 隶属 > 位置/所有。
#: 按子串匹配，先命中先用；认不出的类型排在「隶属」之后、「位置/所有」之前。
_REL_PRIORITY_GROUPS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (0, ("父", "母", "子", "女", "兄", "弟", "姐", "妹", "夫", "妻", "亲", "同门", "师", "徒")),
    (1, ("敌", "仇", "对立", "敌对")),
    (2, ("隶属", "属下", "手下", "臣属", "门下")),
    (3, ("位置", "位于", "located_at", "owns", "拥有", "所有")),
)


def _relation_priority(rel_type: str | None) -> int:
    text = (rel_type or "").lower()
    for rank, keywords in _REL_PRIORITY_GROUPS:
        if any(k.lower() in text for k in keywords):
            return rank
    return 3 if not text else 2


def _narrative_timeline(db, project: Project, *,
                        current_chapter: int | None = None) -> dict | None:
    """KG 侧剧情时间线（续写/角色对话的第二来源）。

    文风档案里的节拍/剧情编排/伏笔/漂移回答「这类故事怎么推进」；这里回答
    「这本书究竟发生过什么、走到第 N 章是什么状态」。两者在渲染层合并——
    调用方传 None 时 `describe_narrative` 会直接报错，不允许静默降级成只有文风；
    本函数在 KG 空项目下返回**最小空时间线**（见下），而不是 None。

    取数全部来自 KG 业务表（零 LLM）：
      - `kg_events`：按 sequence 排序的事件序列（chapter 为章号）
      - `kg_foreshadows`：未回收伏笔（planted/developing）
      - `kg_character_state`：每个角色每类状态的最新一条
      - `kg_relations`：active 关系（角色对话用）

    返回**最小空时间线**（`events=[]` 等）表示该项目还没有任何 KG 事件：
    续写/重生成可以不中断地继续（渲染层写明「尚无已抽取剧情」），不再返回
    None 让 `describe_narrative` 抛 ValueError、把接口打成 500。
    """
    events = db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project.id,
            KgEvent.is_current.is_(True),  # 同章旧稿事件只留档，不进时间线
        ).order_by(KgEvent.sequence)
    ).all()
    if not events:
        return {
            "current_chapter": int(current_chapter) if current_chapter else 1,
            "events": [],
            "open_plot_holes": [],
            "character_states": [],
            "relations": [],
        }

    if current_chapter is None:
        current_chapter = max((e.chapter or 0) for e in events) or None

    event_items = []
    for e in events:
        ch = e.chapter if e.chapter is not None else e.segment_no
        if ch is None:
            continue
        event_items.append({"chapter_no": int(ch), "summary": e.summary or ""})

    holes = db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project.id,
            KgForeshadow.status.in_(("planted", "developing")),
        )
    ).all()
    hole_items = [f"{h.name}：{h.description}" if h.description else h.name for h in holes]

    states = db.scalars(
        select(KgCharacterState)
        .where(
            KgCharacterState.project_id == project.id,
            KgCharacterState.is_current.is_(True),  # 同章旧稿状态只留档
        )
        .order_by(KgCharacterState.chapter, KgCharacterState.id)
    ).all()
    # 每个角色每类状态只留最新一条，避免「第 1 章受伤 + 第 5 章痊愈」两条并排。
    latest: dict[tuple[int, str], KgCharacterState] = {}
    for s in states:
        latest[(s.character_id, s.state_type)] = s
    ent_names = {
        e.id: e.canonical_name
        for e in db.scalars(
            select(KgEntity).where(KgEntity.id.in_(list({k[0] for k in latest})))
        ).all()
    }
    by_char: dict[int, list[str]] = {}
    for (cid, stype), s in latest.items():
        if not s.new_value:
            continue
        by_char.setdefault(cid, []).append(f"{stype}={s.new_value}")
    state_items = [
        {"name": ent_names.get(cid, f"角色{cid}"), "state": "；".join(vals)}
        for cid, vals in by_char.items()
        if cid in ent_names
    ]

    rel_items = []
    rels = db.scalars(
        select(KgRelation).where(
            KgRelation.project_id == project.id, KgRelation.status == "active",
            KgRelation.is_current.is_(True),  # 同章旧稿关系只留档
        )
    ).all()
    if rels:
        ids = {r.from_id for r in rels} | {r.to_id for r in rels}
        names = {
            e.id: e.canonical_name
            for e in db.scalars(select(KgEntity).where(KgEntity.id.in_(list(ids)))).all()
        }
        scored = []
        for r in rels:
            if r.description:
                scored.append((
                    _relation_priority(r.rel_type),
                    f"{names.get(r.from_id, r.from_id)}—{names.get(r.to_id, r.to_id)}"
                    f"（{r.rel_type}）：{r.description}",
                ))
        # R36-3：亲属/同门 > 敌对 > 隶属 > 位置/所有。渲染层只取前 10 条，
        # 不排序就会让 `located_at` 这类弱关系挤掉「师妹/宿敌」。
        scored.sort(key=lambda pair: pair[0])
        rel_items = [text for _, text in scored]

    return {
        "current_chapter": current_chapter or 1,
        "events": event_items,
        "open_plot_holes": hole_items,
        "character_states": state_items,
        "relations": rel_items,
    }


def _style_profile(db, project: Project) -> dict | None:
    """项目级文体档案（参考语料实测），按指纹缓存，整轮恒定。

    粒度取 `unit_chars = generation_length`：档案必须与生成段的长度可比，
    否则统计粒度不对齐会系统性偏移（`verify_stylometry` B 段有闸门）。
    取不到就返回 None —— 档案是增强，不是生成的前提。
    """
    from app.style import project_profile_safe

    return project_profile_safe(
        db, project, unit_chars=int(project.generation_length or 4000)
    )


def _style_source_ids(project: Project) -> set[int] | None:
    """叙事档案的来源范围：续写项目只看续写书，原创项目看参考书集合。

    续写时「参考书」就是源文本，别的参考书的角色口吻/桥段是噪音；原创项目
    没有「本书」，只能按当前参考书集合取。返回 None = 不过滤（保持旧行为）。
    """
    if project.continuation_novel_id:
        return {int(project.continuation_novel_id)}
    ids = {int(x) for x in (project.reference_novel_ids or [])}
    return ids or None


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
    narrative_tier 的本义是「提炼档位」（full/balanced/fast），与「是否开启生成期
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


#: 闸门指标 → 重写块里的中文标签 / 单位 / 数值格式。
#: 阈值本身不在这里写死，从 `GEN_GATE_LIMITS` 取——改闸门常量，重写提示同步变，
#: 避免「闸门判 0.60、重写提示还写 0.58」这种两套口径。
_GATE_FMT: dict[str, tuple[str, str, str]] = {
    "句长均值": ("句长平均", " 字", "{:.2f}"),
    "句长CV": ("句长 CV", "", "{:.3f}"),
    "短句占比": ("≤4 字短句", "", "{:.1%}"),
    "长句占比": ("≥40 字长句", "", "{:.1%}"),
    "长句31_39占比": ("31–39 字句", "", "{:.1%}"),
    "段长均值": ("段均", " 字", "{:.1f}"),
    # 结构项（2026-11-14）：段数是排版地板，进单章硬判。
    "段数": ("自然段数", " 段", "{:.0f}"),
    "叙述单句段占比": ("叙述单句段", "", "{:.1%}"),
    "对白字占比": ("对白字占比", "", "{:.1%}"),
    "问号每千字": ("问号", " 个/千字", "{:.2f}"),
    # 对白校准（gen-gate-v5）：纯对白段/纯消息段总占比已降为软档，不再在这里出现。
    "极短纯对白段占比": ("极短纯对白段", "", "{:.1%}"),
    "消息段字数中位": ("消息段字数中位", " 字", "{:.1f}"),
    "对白≥20占比": ("≥20 字对白句", "", "{:.1%}"),
    "对白感叹句占比": ("对白感叹句", "", "{:.1%}"),
    "对白叹词起头占比": ("叹词起头", "", "{:.1%}"),
    # 长度硬闸（v5）：正文字符数上限，值在 violations/limit 里按项目目标算。
    "正文字数": ("正文总字数", " 字", "{:.0f}"),
    # 汉字数仍渲染（文风指标），但不参与篇幅判界。
    "汉字数": ("正文汉字数", " 字", "{:.0f}"),
}


def _gate_hard_block(gate_info: dict | None) -> str:
    """把闸门结果渲染成重写 system 的硬指标块；通过/跳过时返回空串。

    只渲染**本轮仍未过的项**：第二轮重写时 `gate_info` 是第一轮后的复测结果，
    这里按 `violations` 过滤，已经达标的项不再出现在提示里，避免模型把整篇
    重新写一遍（那样会把上一轮刚修好的分布又改坏）。
    """
    if not gate_info or gate_info.get("passed") or gate_info.get("skipped"):
        return ""
    metrics = gate_info.get("metrics") or {}
    viol_limits = {
        v.get("metric"): (v.get("limit") or {})
        for v in (gate_info.get("violations") or [])
    }
    bad = set(viol_limits)
    lines = []
    for key, (label, unit, fmt) in _GATE_FMT.items():
        if key not in bad:
            continue
        if key in ("汉字数", "正文字数"):
            # 长度上限不在 GEN_GATE_LIMITS 里（随项目目标字数变），单独取。
            cap = int((gate_info.get("length_limit") or {}).get("max") or 0)
            cur = int(metrics.get(key) or 0)
            lines.append(f"- {label} ≤{cap} 字（当前 {cur} 字）")
            continue
        # 阈值取「判这条违规的那份带」优先（违规项自带 limit，判罚带随项目目标字数
        # 折算过），再退全量参考带。两处都没有上下限时只留 violations、不渲染：
        # 没有阈值就没有可执行的修复目标，绝不能把 None 送进 format()
        # （无参考书时 `段数` 在 full 带里根本不存在，旧代码在此抛 TypeError，
        # 导致重写轮整轮崩掉、正文仍旧稿）。
        limit = viol_limits.get(key) or active_full_limits().get(key) or {}
        lo, hi = limit.get("min"), limit.get("max")
        if lo is None and hi is None:
            continue
        if lo is not None and hi is not None:
            target = f"{fmt.format(lo)}–{fmt.format(hi)}{unit}"
        elif lo is not None:
            target = f"≥{fmt.format(lo)}{unit}"
        else:
            target = f"≤{fmt.format(hi)}{unit}"
        cur = float(metrics.get(key) or 0.0)
        lines.append(f"- {label} {target}（当前 {fmt.format(cur)}{unit}）")

    def _band(key: str) -> str:
        """按闸门常量渲染区间，重写块里不再手写第二套数字。"""
        limit = active_full_limits().get(key) or {}
        lo, hi = limit.get("min"), limit.get("max")
        if lo is not None and hi is not None:
            return f"{lo:.1%}–{hi:.1%}"
        if lo is not None:
            return f"≥{lo:.1%}"
        return f"≤{hi:.1%}" if hi is not None else "按档案分位"

    def _pct_min(key: str) -> float:
        """取硬闸下限（感叹句/叹词起头共用），不在重写块里另写一套数。"""
        return float((active_full_limits().get(key) or {}).get("min") or 0.0)

    long_mid = float(metrics.get("长句31_39占比") or 0.0)
    long_40 = float(metrics.get("长句占比") or 0.0)
    single_p = float(metrics.get("叙述单句段占比") or 0.0)
    dlg_p = float(metrics.get("对白字占比") or 0.0)
    dlg_target = _band("对白字占比")
    actions: list[str] = []
    if bad & {"句长均值", "句长CV", "短句占比", "叙述单句段占比"}:
        # 不再写「短句每千字 6–10 处」这类手拍配额：短句占比本身已在上面的
        # 违规行里按档案带渲染，多写一套数字只会和闸门打架（2026-11-14）。
        actions.append(
            "① 短句：用动作、停顿、单字应答把长句拆开，短句占比按上面的档案带。"
            "**≤4 字的对白不得单独成段**：必须与说话人的神态/动作同段，"
            "或补一句旁人反应；叙述单句段是参考书的正常结构（参考 "
            + _band("叙述单句段占比")
            + f"，当前 {single_p:.1%}），**不要为了凑段均去并它**；"
            "只把语义上被硬拆开的叙述句接回去，不要把短对白单独留成一行。"
        )
    if bad & {"长句占比", "长句31_39占比"}:
        actions.append(
            f"② 长句：≥40 字句 {_band('长句占比')}（当前 {long_40:.1%}）；"
            f"31–39 字句 {_band('长句31_39占比')}（当前 {long_mid:.1%}）。"
            "长句要展开成分句递进，不是加逗号凑长度；长短句必须在同一段内交替。"
        )
    if bad & {"对白字占比", "问号每千字"}:
        actions.append(
            "③ 对白：每 10 句对白中，信息推进 ≤5 句、抖机灵 ≥2 句、"
            f"套近乎或答非所问 ≥2 句；连续追问 ≤2 轮。对白字占比 {dlg_target}（当前 {dlg_p:.1%}）——"
            "少问号不等于少对白，把追问换成陈述、动作或答非所问，不要删对白。"
        )
    # 「段长均值」自 v8 起不再进 violations，这里不再有「④ 段长」动作块。
    if bad & {"极短纯对白段占比", "消息段字数中位"}:
        from app.style.consistency import ARCHIVE_CHAPTER_PERCENTILES as _cp
        actions.append(
            "⑤ 对白碎：极短纯对白段（整段只有引号、且 ≤4 字）"
            f"{_band('极短纯对白段占比')}"
            f"（当前 {float(metrics.get('极短纯对白段占比') or 0):.1%}）；"
            f"消息/对白段字数中位 ≥{_cp['消息段字数中位']['p10']:g} 字"
            f"（当前 {float(metrics.get('消息段字数中位') or 0):g} 字）。"
            "总占比不作为硬判（一条 40 字的消息独立成段不算碎），但每条消息都要带"
            "「谁说的、什么神态、旁人怎么反应」；≤4 字对白不得单独成段；"
            "群聊里两三条消息之间必须有群体反应或人物评价；"
            "电话/语音段同规则：两三条消息之间要有停顿、动作、环境声或对方反应，"
            "不得一条一句孤立成行。"
        )
    if bad & {"对白≥20占比", "对白感叹句占比", "对白叹词起头占比"}:
        from app.style.consistency import ARCHIVE_CHAPTER_PERCENTILES as _cp
        actions.append(
            f"⑥ 对白平/短：≥20 字对白句 ≥{_cp['对白≥20占比']['p10']:.1%}"
            f"（当前 {float(metrics.get('对白≥20占比') or 0):.1%}，目标 "
            f"{_cp['对白≥20占比']['p50']:.1%}）。把几条干巴巴的短陈述展开成"
            "带称呼、反问、追问或感叹的完整说法；同一场对话连续 3 条平铺陈述"
            "算不合格。"
            f"感叹句 ≥{_pct_min('对白感叹句占比'):.1%}"
            f"（当前 {float(metrics.get('对白感叹句占比') or 0):.1%}）；"
            f"叹词起头（哎/嘿/哟/诶/嗯）≥{_pct_min('对白叹词起头占比'):.1%}"
            f"（当前 {float(metrics.get('对白叹词起头占比') or 0):.1%}）。"
            "叹词与感叹句必须真的落在句首/句末，不能只在别处补一个「啊」；"
            f"语气词密度 ≥{_cp['语气词密度千字']['p25']:g}/千字"
            f"（当前 {float(metrics.get('语气词密度千字') or 0):g}），"
            f"含助词对白句 ≥{_cp['含助词对白句占比']['p25']:.1%}"
            f"（当前 {float(metrics.get('含助词对白句占比') or 0):.1%}）。"
        )
    if "正文字数" in bad:
        lim = gate_info.get("length_limit") or {}
        cur_h = int(metrics.get("正文字数") or 0)
        actions.append(
            f"⑦ 字数：当前 {cur_h} 个字符（含标点），上限 {int(lim.get('max') or 0)} 字"
            f"（目标 {int(lim.get('target') or 0)} 字）。只许压缩与改写，不许扩写；"
            "删重复、合并同类场景、去掉解释性句子，保留情节与对白，不要靠删情节凑数。"
        )
    return (
        "\n\n【文风硬指标（本次重写必须同时满足，与档案同一统计口径）】\n"
        + "\n".join(lines)
        + "\n只改上面列出的项，已达标的部分保持原样；每一项都可事后统计核对：\n"
        + "\n".join(actions) + "\n"
    )


def _is_transient_api_error(exc: Exception) -> bool:
    """上游偶发 HTTP 错误（第 9 段实测 422 重跑即成功）。

    只认明确可重试的 4xx/5xx；认证/参数错误照旧直接抛，避免把真故障藏起来。
    """
    cause = getattr(exc, "__cause__", None)
    code = getattr(cause, "code", None)
    retryable = (422, 429, 500, 502, 503, 504)
    if code in retryable:
        return True
    msg = str(exc)
    return any(f"HTTP {c}" in msg for c in retryable)


def _chat_with_retry(provider, system: str, user: str, **kwargs) -> str:
    """生成/重写调用只重试一次，**复用同一 prompt**（含已完成的清查笔记）。

    2026-11-13：首跑 422 把 33 轮自查 token 白扔。重试发生在同一函数内，
    笔记、门禁块、证据块都是已经拼好的字符串，不需要重查、也不会重新计费。
    """
    try:
        return provider.chat(system, user, **kwargs)
    except Exception as exc:  # noqa: BLE001
        if not _is_transient_api_error(exc):
            raise
        logging.getLogger(__name__).warning(
            "生成调用命中可重试 HTTP 错误，1 次重试（复用同一 prompt）: %s", exc)
        time.sleep(1.5)
        return provider.chat(system, user, **kwargs)


def _brief_settings():
    """读取外部素材工具的配置（全部走 `BriefSettings`，调用点不写死数值）。"""
    from app.retrieval.brief.settings import BriefSettings

    return BriefSettings.from_env()


def _brief_backend_ready(cfg) -> tuple[bool, str]:
    """是否有可用检索后端；返回 `(可用, 不可用原因)`。

    免费后端（含无头模式）本来就不需要 `search_url`，之前拿 `search_url` 非空当
    可见条件，会把免费路径整体藏掉、链路根本跑不起来。JSON 后端仍要求地址；
    无头模式额外要求 playwright 已安装——否则给了工具也只会每轮报错。
    """
    kind = str(cfg.backend or "auto").strip().lower()
    if kind == "json":
        if str(cfg.search_url or "").strip():
            return True, ""
        return False, "no_search_url"
    if getattr(cfg, "headless", False):
        try:
            import importlib.util

            if importlib.util.find_spec("playwright") is None:
                return False, "no_playwright"
        except Exception:  # noqa: BLE001 —— 探测失败按不可用处理，保守
            return False, "no_playwright"
    return True, ""


def _brief_visibility(cfg) -> str:
    """工具可见性三态：`disabled` / `no_backend` / `visible`。

    `no_backend` 涵盖 JSON 后端缺地址、无头模式缺 playwright、以及配置/初始化
    异常——都是「开关开了但没有能干活的后端」，与「开关没开」必须分开看。
    """
    if not cfg.enabled:
        return "disabled"
    return "visible" if _brief_backend_ready(cfg)[0] else "no_backend"


def _brief_tool_visible(cfg) -> bool:
    """工具是否进初稿的 tools 列表：开关打开 + 有可用后端。

    只把工具藏起来、而不是「先给出去再永远返回空串」：后者会让模型反复调用
    一个死工具，每轮都烧 prompt 与回合，还把上下文塞满「无结果」。
    """
    return _brief_visibility(cfg) == "visible"


def _start_brief_tool(db, project: Project, segment_no: int):
    """每章开始时建工具并重置计数/秒数预算；不可见时返回 `(None, 留痕)`。

    `state` 固定三种：`disabled`（开关没开）/ `no_backend`（开了但没有可用后端）/
    `visible`（已给出工具）。`reason` 只做诊断细分，不参与判断。
    """
    meta: dict = {"enabled": False, "visible": False, "state": "disabled",
                  "reason": "disabled"}
    try:
        cfg = _brief_settings()
    except Exception:  # noqa: BLE001 —— 配置读不到就不给工具，不阻断生成
        logging.getLogger(__name__).warning(
            "读取外部素材工具配置失败，本章不提供该工具", exc_info=True)
        return None, {**meta, "state": "no_backend", "reason": "settings_error"}
    meta["enabled"] = bool(cfg.enabled)
    if not cfg.enabled:
        return None, meta
    ready, reason = _brief_backend_ready(cfg)
    if not ready:
        return None, {**meta, "state": "no_backend", "reason": reason}
    try:
        from app.retrieval.brief.tool import BriefTool

        tool = BriefTool(cfg, db=db)
        tool.start_chapter(
            session_id=f"gen-{project.id}-{segment_no}",
            project_id=project.id,
            novel_id=project.active_novel_id or None,
            segment_no=segment_no,
        )
    except Exception:  # noqa: BLE001 —— 初始化失败只降级为「无工具」
        logging.getLogger(__name__).warning(
            "外部素材工具初始化失败，本章不提供该工具", exc_info=True)
        return None, {**meta, "state": "no_backend", "reason": "init_error"}
    return tool, {**meta, "visible": True, "state": "visible", "reason": None}


def _brief_tool_quality(tool, meta: dict) -> dict:
    """章末留痕：问了几次、问了什么、拿回多少字、缓存命中几次。

    字段全部沿用 `BriefTool.chapter_stats()` 的键名，不另造一套；未启用时
    只落「为什么没给工具」，便于事后区分「没开」与「开了但模型没问」。
    """
    if tool is None:
        return dict(meta)
    return {
        **meta,
        **tool.chapter_stats(),
        "max_calls": tool.settings.max_calls_per_chapter,
        "budget_s": tool.settings.chapter_budget_s,
    }


def _parse_tool_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: 素材轮的系统后缀：这一轮只判断「要不要查素材 + 取哪几条」，不写正文。
#: 末轮成文走正常的 chat / stream_chat（流式），不把整章退化成一次性下发。
#: 预算是硬上限：素材只是增强项，不能让「等待首字」被它拖到几十秒。
_BRIEF_ROUND_BUDGET_S = 15.0


def _complete_with_budget(provider, messages, *, tools, tool_choice, budget):
    """给素材轮的结构化补全加预算；provider 不认这个参数时退回原签名。"""
    try:
        return provider.complete_message(
            messages, tools=tools, tool_choice=tool_choice,
            budget_seconds=budget,
        )
    except TypeError:
        return provider.complete_message(
            messages, tools=tools, tool_choice=tool_choice,
        )

_BRIEF_DECISION_SUFFIX = (
    "\n\n【本轮只做素材判断】\n"
    "先不要写正文。若本章需要补充具体细节（环境、器物、职业流程、专业信息等），"
    "调用 search_external_material；它返回的是素材库候选清单（含 id、入库天数、"
    "来源查询、标题与 ≤40 字摘要，不含正文）。随后调用 select_external_material，用方括号内的 id "
    "挑选本章真正要用的条目；候选里没有合适的、或本章不需要素材，就直接回复「无」。"
    "没被选中的条目会留在素材库里给后面的章节用。拿到素材后会在下一轮再请你写正文。"
)

#: 候选已就绪但模型没点名时的补充指令：只做选择，不再检索。
_BRIEF_SELECT_SUFFIX = (
    "\n\n【选素材】以上是素材库候选清单。"
    "调用 select_external_material，用方括号内的 id 指定本章要用的条目；"
    "若一条都用不上，直接回复「无」，不要硬选。"
)


def _material_suffix(blocks: list[str]) -> str:
    """把工具拿回的素材块拼到初稿 user prompt 末尾；没有素材时返回空串。

    没有素材时**一个字符都不加**，保证「模型不调用工具」的提示与接线前逐字相同。
    块头声明沿用检索侧的统一口径：只作素材，不改变人物、设定与章纲。
    """
    chunks = [b.strip() for b in blocks if b and b.strip()]
    if not chunks:
        return ""
    return (
        "\n\n【外部资料 · 仅作素材】以下内容来自外部检索，未经核实："
        "只能用于补充细节，不得改变人物、设定与章纲。\n\n"
        + "\n\n".join(chunks)
    )


def _gather_brief_materials(provider, brief_tool, system: str, user: str, *,
                            constraints: dict | None) -> list[str]:
    """两阶段素材轮：先检索入库、再由模型点名取哪几条。

    第一阶段 `search_external_material` 只把候选清单（id/入库时间/标题）回给模型；
    第二阶段 `select_external_material` 选中的条目才作为素材块返回给调用方拼进初稿。
    未选中的条目留在素材库，后续章节可再取；模型一次都不调用时，不联网、不写轨迹。

    只在初稿阶段调用：重写轮与压缩轮不碰这里，避免同一章重复联网/计费。
    任何异常都降级为「没有素材」，不阻断正文生成。
    """
    if brief_tool is None:
        return []
    from app.llm.provider import ToolsUnsupportedError, _apply_constraints
    from app.retrieval.brief.tool import SELECT_TOOL_NAME

    messages = [
        {"role": "system", "content": system + _BRIEF_DECISION_SUFFIX},
        {"role": "user", "content": _apply_constraints(user, constraints)},
    ]
    specs = [brief_tool.spec(), brief_tool.select_spec()]
    blocks: list[str] = []
    # 轮数 = 检索次数上限 + 选择轮 + 「不需要素材」的收尾判断。
    rounds = max(1, int(brief_tool.settings.max_calls_per_chapter)) + 2
    # 整段素材轮共享一份时长预算：每轮只能用「剩余额度」，
    # 避免 rounds 轮各自吃满预算把首字拖到几十秒（实测单轮曾耗时 49s）。
    deadline = time.monotonic() + _BRIEF_ROUND_BUDGET_S
    try:
        for _ in range(rounds):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            message = _complete_with_budget(
                provider, messages, tools=specs, tool_choice="auto",
                budget=remaining,
            )
            calls = list(message.get("tool_calls") or [])
            if not calls:
                break
            messages.append({
                "role": "assistant",
                "content": str(message.get("content") or ""),
                "tool_calls": calls,
            })
            for call in calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                result = brief_tool.dispatch(
                    _parse_tool_arguments(fn.get("arguments")),
                    name=name,
                )
                # 只有「选中」阶段返回的才是可注入正文的素材；检索阶段返回候选清单。
                if result and name == SELECT_TOOL_NAME:
                    blocks.append(result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": result,
                })
        # 模型常拿到候选后直接回复「无」；不补这一轮，两阶段会退化成「只查不选」。
        if not blocks and brief_tool.chapter_stats().get("candidates", 0) > 0:
            blocks = _ask_brief_selection(
                provider, brief_tool, messages,
                budget=deadline - time.monotonic(),
            )
    except ToolsUnsupportedError:
        logging.getLogger(__name__).warning(
            "当前模型链不支持 function calling，本章退化为无素材初稿")
        return []
    except Exception:  # noqa: BLE001 —— 素材是增强项，失败不阻断正文
        logging.getLogger(__name__).warning(
            "外部素材轮失败，本章按无素材继续", exc_info=True)
        return []
    return blocks


def _ask_brief_selection(provider, brief_tool, messages: list[dict], *, budget: float) -> list[str]:
    """候选已就绪但模型没点名时的补选轮：只给选择工具，不给检索工具。

    单独留一个函数而不是塞进主循环，是为了让「是否真的走到选择」在代码上可读：
    主循环里的选择调用与这里是同一个 `dispatch` 入口，留痕落在同一处。
    """
    from app.retrieval.brief.tool import SELECT_TOOL_NAME

    if budget <= 1:
        return []
    convo = list(messages) + [{"role": "user", "content": _BRIEF_SELECT_SUFFIX}]
    blocks: list[str] = []
    message = _complete_with_budget(
        provider, convo, tools=[brief_tool.select_spec()], tool_choice="auto",
        budget=budget,
    )
    for call in list(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        if str(fn.get("name") or "") != SELECT_TOOL_NAME:
            continue
        result = brief_tool.dispatch(
            _parse_tool_arguments(fn.get("arguments")), name=SELECT_TOOL_NAME,
        )
        if result:
            blocks.append(result)
    return blocks


def _truncation_regen_attempts() -> int:
    """截断后优先重生成的次数预算（配置项，退默认 1）。

    为什么不是「无限重试」：一次 4,000 字级生成的成本与一章相同，截断往往是
    供给侧的 `max_tokens` 硬顶，重生成未必能规避；一次足以区分「偶发截断」
    与「稳定撞顶」，再多次只是烧预算。设 0 即退回「不重生成、直接降级压缩」。
    """
    raw = getattr(settings, "truncation_regen_attempts", None)
    try:
        return 1 if raw is None else max(0, int(raw))
    except (TypeError, ValueError):
        return 1


def _regenerate_if_truncated(provider, text: str, finish_reason: str | None,
                             regenerate) -> tuple[str, dict]:
    """初稿被 `finish_reason=length` 截断时，优先重生成；预算不足才降级。

    `regenerate` 是**无副作用的重生成回调**（复用同一 prompt）。之所以不在
    截断稿上直接压缩：压缩只会把断头稿按比例缩短，断在半路的剧情不会自己接上，
    却被当成正常章落库（第 31 章实测 5,926 → 4,084 即此路径）。

    返回 `(采用的正文, 留痕 dict)`。留痕字段：
    - `draft_finish_reason` / `draft_truncated_by_tokens`：初稿是否撞 token 上限；
    - `regenerated` / `regen_finish_reason`：是否重生成、重生成结果；
    - `accepted_regenerated`：是否采用了重生成稿；
    - `degrade_reason`：未采用重生成的原因（预算/失败/再次截断）。
    """
    info: dict = {
        "draft_finish_reason": finish_reason,
        "draft_truncated_by_tokens": finish_reason == "length",
        "regenerated": False,
        "regen_finish_reason": None,
        "accepted_regenerated": False,
        "degrade_reason": None,
    }
    if not info["draft_truncated_by_tokens"]:
        return text, info
    attempts = _truncation_regen_attempts()
    if attempts <= 0:
        info["degrade_reason"] = "regeneration_budget_exhausted"
        return text, info
    for _ in range(attempts):
        try:
            candidate = regenerate()
        except Exception:  # noqa: BLE001 —— 重生成失败不得吞掉已经拿到的初稿
            logging.getLogger(__name__).warning(
                "截断稿重生成失败，降级为压缩断头稿", exc_info=True)
            info["degrade_reason"] = "regeneration_failed"
            break
        info["regenerated"] = True
        info["regen_finish_reason"] = getattr(provider, "last_finish_reason", None)
        if info["regen_finish_reason"] != "length":
            info["accepted_regenerated"] = True
            return candidate, info
    if not info["degrade_reason"]:
        info["degrade_reason"] = "regenerated_but_still_truncated"
    return text, info


def _stream_with_retry(provider, system: str, user: str, **kwargs):
    """流式版的可重试包装：只在**还没吐出任何 chunk** 时重试一次。

    已经发过 chunk 再重试会让前端的正文重复；那种情况照旧抛给上层。
    """
    emitted = False
    for attempt in (1, 2):
        try:
            for piece in provider.stream_chat(system, user, **kwargs):
                emitted = True
                yield piece
            return
        except Exception as exc:  # noqa: BLE001
            if emitted or attempt == 2 or not _is_transient_api_error(exc):
                raise
            logging.getLogger(__name__).warning(
                "流式生成命中可重试 HTTP 错误，1 次重试（复用同一 prompt）: %s", exc)
            time.sleep(1.5)


def _stream_events_with_retry(provider, system: str, user: str, **kwargs):
    """`_stream_with_retry` 的事件版：额外把 reasoning 增量透传出来。

    重试判定只认「已经吐出正文」；只收到 reasoning 时重试不会污染正文。
    """
    emitted = False
    for attempt in (1, 2):
        try:
            fn = getattr(provider, "stream_chat_events", None)
            source = (
                fn(system, user, **kwargs)
                if callable(fn)
                else (("content", p) for p in provider.stream_chat(system, user, **kwargs))
            )
            for kind, piece in source:
                if kind == "content":
                    emitted = True
                yield kind, piece
            return
        except Exception as exc:  # noqa: BLE001
            if emitted or attempt == 2 or not _is_transient_api_error(exc):
                raise
            logging.getLogger(__name__).warning(
                "流式生成命中可重试 HTTP 错误，1 次重试（复用同一 prompt）: %s", exc)
            time.sleep(1.5)


def _sentence_quota_block(gate_info: dict | None, text: str,
                          max_chars: int | None = None) -> str:
    """把句长类**比值**缺口换算成「全篇至少几句」的显式配额。

    重写轮此前只把 `句长CV 0.519 低于下限 0.576` 这类比值写进原因清单，模型
    无法自查，第 5–7 章连续三章卡在句长 CV / 短句占比 / 长句占比。这里沿用
    `_chapter_quota_block` 的原则——只给可数的绝对量：全篇 N 句里短句至少 M 句。

    阈值来自**当前参考书重推值** `active_full_limits()`（档案分位推导；仅在
    档案缺数据时退回 `GEN_GATE_LIMITS` 常量表），当前值来自闸门实测
    `gate_info["metrics"]`，全篇句数来自 `stylometry.split_sentences`（与闸门
    同一把尺）。缺任一数据就不出这一块，不用手拍值补。

    2026-11-14（v11）：全篇句数是**中心值不是下限**——句数配额只用来拆/并句式，
    不能当成「必须写满 N 句」的指标去堆句子。句子一多，总字数会顶破 `max_chars`
    硬上限，所以块尾显式声明这条边界（此前只有句数、没有上限约束，实测第 24 章
    初稿 419 句 / 6,370 汉字，句数没被任何上限拦住）。
    """
    metrics = (gate_info or {}).get("metrics") or {}
    total = len(stylometry.split_sentences(text or ""))
    if total < 2:
        return ""
    full = active_full_limits()
    lines: list[str] = []
    for key, label in (("短句占比", "≤4 字短句"), ("长句占比", "≥40 字长句")):
        lo = float((full.get(key) or {}).get("min") or 0.0)
        cur = metrics.get(key)
        if not lo or not isinstance(cur, (int, float)):
            continue
        need = int(-(-(lo * total) // 1))  # ceil
        have = int(round(float(cur) * total))
        if have < need:
            lines.append(
                f"- {label}：目前约 {have} 句（全篇 {total} 句，{float(cur):.1%}），"
                f"本轮至少 {need} 句（{lo:.1%}），还差约 {need - have} 句"
            )
    cv_lo = float((active_full_limits().get("句长CV") or {}).get("min") or 0.0)
    cv_cur = metrics.get("句长CV")
    if cv_lo and isinstance(cv_cur, (int, float)) and float(cv_cur) < cv_lo:
        lines.append(f"- 句长CV：目前 {float(cv_cur):.3f}，需 ≥{cv_lo:.3f}")
    if not lines:
        return ""
    lim = length_limits(max_chars)
    return (
        "\n\n【句子分布配额（本轮必须做到）】\n"
        + "\n".join(lines)
        + "\n做法：短句用动作、停顿、单字应答把长句拆开；长句把同一场景里的连续动作"
        "并成一句。只改句式，不得新增或删减情节。\n"
        + f"注意：全篇约 {total} 句是**中心值不是下限**，不是「必须写满 {total} 句」；"
        f"句数不得多到让正文总字数突破 {lim['max']} 字硬上限，超了同样判不合格。"
    )


def _rewrite_prompt(text: str, reasons: list[str], forbidden: dict | None,
                    evidence: list[dict] | None = None,
                    item_holding_block: str | None = None,
                    constraints: dict | None = None,
                    gate_info: dict | None = None,
                    research_notes: str | None = None,
                    max_chars: int | None = None) -> tuple[str, str]:
    """构造针对性重写的 system/user prompt。

    evidence：`_gather_rewrite_evidence` 产出的「原文对照」证据。
    **为 None/空列表时 user 回退到旧形状**（问题清单 + 待改正文），
    保证快档与无证据场景逐字节不变。

    item_holding_block：与初稿同一份物品持有块（`build_context` 的产物）。
    重写是"会替换正文"的路径，必须携带同一份已发生事实与 constraints——
    否则注入只作用于被丢弃的初稿（2026-11-10 实测：p1 落库正文来自重写，
    重写 prompt 里没有物品持有块）。

    constraints：与初稿同一份锁定名单。**必须放 system、不能追加在 user 末尾**：
    重写的 user 末尾是「待改正文」，把锁定名单排在正文之后，模型会把它当成
    正文内容原样吐出来（实测落库段落末三行 = 【本节完】+ 锁定角色 + 未填伏笔）。
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
    if item_holding_block:
        # 只在有块时追加：无块项目的重写 prompt 逐字节不变。
        system += "8. 不得改动物品归属；与【物品持有事实】冲突时以事实为准\n"
    if forbidden:
        for dim, items in forbidden.items():
            if items:
                system += f"\n【{dim}】禁止使用：{'、'.join(items[:8])}"
    constraint_text = render_constraints(constraints)
    if constraint_text:
        system += (
            "\n\n【本段锁定信息（只用于约束，禁止把这些条目原样写进正文）】\n"
            + constraint_text
        )
    lim = length_limits(max_chars)
    cur_hanzi = hanzi_count(text)
    # C（2026-11-13）：取消「不得多于当前字数」。碎段要修好，唯一有效手段是
    # 把短对白并入场景时补神态/动作/旁人反应——那必然增字。改为「段数必须降、
    # 汉字允许小幅上浮」，并把上限一并写清，防止借补写无限膨胀。
    from app.style.stylometry import paragraphs as _paragraphs
    para_texts = _paragraphs(text)
    cur_paras = max(1, len(para_texts))
    char_total = len("".join((text or "").split()))
    mean_now = char_total / cur_paras if cur_paras else 0.0
    allowance = _rewrite_growth_allowance()
    # 2026-11-14（v11）：可长到值必须与硬上限**取小**。旧写法只算 `cur × 1.08`，
    # 6,378 字时给出「最多到 6,888 字」，同一块里又写「硬上限仍是 4,800 字」——
    # 两条指令互相打架，模型自然照着大的那个写。
    grow_cap = min(int(cur_hanzi * (1 + allowance)), int(lim["max"]))
    # 2026-11-14：**不再给段结构方向性指令**。`段长均值` 已退出单章判罚（v8），
    # 重写提示若仍写「段均不足就压到 ≤N 段 / 只并多句叙述段 / 达标后不得增减段数」，
    # 就是判据松了而指令没松——模型会继续照旧合并段落（第 21 章叙述单句段仅 7.1%，
    # 正是压段指令压出来的）。这里只留事实与边界：当前结构、允许补写换增字、
    # 全章汉字硬上限；段怎么切由模型按场景自行判断。
    # v11：禁令按篇幅分情形——**未超限**时保留「不得靠删内容抬段均」，
    # **已超限**时压缩是第一目标，明确允许删（篇幅要求，跟段均无关）。
    over_limit_now = cur_hanzi > int(lim["max"])
    if over_limit_now:
        length_rule = (
            f"当前正文已超过全文汉字硬上限 {lim['max']} 字，**本轮第一目标是压到 "
            f"{lim['max']} 字以内**：允许删冗句、删对白、删可割舍情节，这是篇幅要求，"
            f"跟段均无关；不允许新增内容，也不得比现在还长。"
        )
    else:
        length_rule = (
            f"**给短对白补神态、动作或旁人反应是明确允许的改法**，"
            f"汉字数可在当前基础上小幅上浮，最多到 {grow_cap} 字"
            f"（约 +{allowance:.0%}；与硬上限 {lim['max']} 字取小）；"
            f"但**不得靠删短句、删对白、删情节来抬段均**。"
        )
    system += (
        f"\n\n【段结构与字数】当前 {cur_paras} 段 / {cur_hanzi} 个汉字 / "
        f"段均 {mean_now:.1f} 字。段落切分按场景、动作与说话人切换自行判断，"
        f"不要求向任何段均目标靠拢。"
        + length_rule
        + f"全文汉字数硬上限仍是 {lim['max']} 字。"
    )
    system += _sentence_quota_block(gate_info, text, max_chars)
    gate_block = _gate_hard_block(gate_info)
    system += gate_block
    system += "\n\n只输出改写后的正文，不要输出任何解释或标记。"
    if gate_block:
        head = f"以下文本存在以下问题（AI 腔 / 文风硬指标）：\n{'；'.join(reasons)}"
    else:
        head = f"以下文本存在以下 AI 腔问题：\n{'；'.join(reasons)}"
    parts = [head]
    if evidence:
        # 【原文对照】块：每条证据都带**来源标注**（前文第 N 段 / 参考语料《X》第 M 章），
        # 让模型分清哪段是「你自己更早写下的事实」、哪段是「别人写的同类场景」。
        # 「不要照抄内容」必须写明：参考语料是别人的作品，照抄会直接污染正文
        # 并引入版权/一致性问题；前文片段照抄则会让本段变成重复叙述。
        blocks = [
            f"- 来源：{e.get('source') or '未知'}（{e.get('kind') or '未知'}）\n{e.get('text') or ''}"
            for e in evidence
        ]
        parts.append(
            "【原文对照】以下片段仅供你对照调性与前文事实：参考语料是同类场景的写法参考，"
            "前文片段是你自己更早写下的事实。**只学表达方式、只保证与前文不矛盾，"
            "不要照抄内容，也不要把参考语料的情节搬进本段。**\n"
            + "\n\n".join(blocks)
        )
    section = render_item_holding_section(item_holding_block)
    if section:
        parts.append(section)
    if research_notes:
        # 缺口驱动的定向自查笔记：只在重写轮出现，初稿 prompt 不背这段。
        parts.append(research_notes.strip())
    parts.append(f"请改写：\n\n{text}")
    return system, "\n\n".join(parts)


def _rewrite_with_evidence(db, project, provider, text: str, reasons: list[str],
                           forbidden: dict | None, *,
                           item_holding_block: str | None = None,
                           constraints: dict | None = None,
                           gate_info: dict | None = None,
                           research_notes: str | None = None,
                           max_chars: int | None = None) -> tuple[str | None, dict | None]:
    """执行一次针对性重写，返回 (rewritten_text_or_None, rewrite_info)。

    抽出成公共函数是为了让 `generate_next_segment` 与 `generate_next_segment_stream`
    **两个入口的重写语义完全一致**——原先两处各写一遍，任何一边漏改都会造成
    「流式与非流式重写结果不同」这种极难排查的不一致。

    **必须携带与初稿同一份注入**（2026-11-10 修）：
    `item_holding_block` 原样进入重写 user prompt，`constraints` 进入重写 system
    （**不能**交给 provider 追加到 user 末尾——重写 user 末尾是待改正文，追加会
    被模型当正文回显）。重写会替换正文，如果这里不带，注入就只作用于被丢弃的初稿。

    完整档（_selfcheck_enabled）才收集原文对照证据；其余档位 evidence 为空，
    prompt 回退旧形状，零新增调用。

    重写失败一律吞掉返回 (None, None) 记日志——重写是补救，不是生成的前提。
    """
    evidence: list[dict] = []
    selfcheck: dict = {}
    if _selfcheck_enabled(project):
        evidence, selfcheck = _gather_rewrite_evidence(db, project, text, reasons, forbidden)
    try:
        rw_system, rw_user = _rewrite_prompt(
            text, reasons, forbidden, evidence,
            item_holding_block=item_holding_block,
            constraints=constraints,
            gate_info=gate_info,
            research_notes=research_notes,
            max_chars=max_chars,
        )
        # constraints 已进 system；不能再走 provider 的"追加到 user 末尾"通道——
        # 重写的 user 末尾是待改正文，追加会把锁定名单挤进正文被模型回显。
        #
        # 2026-11-14：**重写也走流式**。此前走非流式 `chat()`，整套可靠性都压在
        # 「首字节前硬顶」上：第 17、18 两章重写轮连续整轮失败（MiniMax 生成期
        # 几分钟不吐字节，180s recv 空闲超时先把它掐了）。流式与初稿共用一条
        # 已验证路径，慢只表现为慢；字节一到达就由 provider 的吞吐窗口兜住假活。
        # 这里只换传输层：增量拼接后**原样**交给下面的长度校验与闸门。
        #
        # 重试仍用 `_stream_with_retry`（只在还没吐任何 chunk 前重试一次），
        # 不再套超时重试——避免「provider 重试 × 外层重试」把预算翻倍。
        # 不传 `constraints`：`stream_chat` 会把它追加到 user 末尾，而重写的
        # user 末尾是待改正文，锁定名单已经在 system 里。
        rewritten = _strip_thinking(
            "".join(_stream_with_retry(provider, rw_system, rw_user)))
        if rewritten and len(rewritten) > len(text) * 0.5:
            info = {
                "reasons": reasons,
                "original_chars": len(text),
                "rewritten_chars": len(rewritten),
                # 观测位：重写是否真的携带了注入块 / 锁定名单（而不是又走回老路）
                "item_holding_injected": bool(item_holding_block),
                "constraints_injected": bool(render_constraints(constraints)),
            }
            # 闸门结果只作为观测键追加：旧字段语义不变
            if gate_info:
                info["style_gate"] = gate_info
            # selfcheck 是纯新增键 —— 既有字段语义一个字都不改
            if selfcheck:
                info["selfcheck"] = selfcheck
            return rewritten, info
    except Exception as exc:  # noqa: BLE001 —— 重写失败不阻断，保留原文
        # C（2026-11-13）：失败不能「break 了当没发生」。把异常带回 `_rewrite_rounds`，
        # 由它写进本轮 rounds（最终随 quality_json 落库），事后可查是哪一轮、什么错。
        err = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning("AI 味重写失败，保留原文: %s", err)
        payload = {"failed": True, "error": err}
        return None, payload
    return None, None


def _compression_prompt(text: str, max_chars: int | None) -> tuple[str, str]:
    """超长压缩轮的提示：只做一件事——压进汉字硬上限。

    与普通重写不同，这里**允许删句、删段、删可割舍的情节**（篇幅问题，
    不是「禁止删字抬段均」那个问题），但不允许新增内容、不允许改写成新情节。
    """
    lim = length_limits(max_chars)
    cur = char_count(text)
    target = int(lim["max"] * 0.95)
    system = (
        "你是长篇小说的压缩编辑。任务只有一个：把正文压到字数上限以内。\n"
        f"当前 {cur} 个字符（含标点），硬上限 {lim['max']} 个字符，目标约 {target} 个。\n"
        "1. 允许删句、删段、删掉可以割舍的过场与次要情节；这是篇幅压缩，不是改写。\n"
        "2. 不允许新增任何内容，不允许扩写、补写、改写成新情节。\n"
        "3. 优先删：重复叙述、无信息量的动作与环境描写、重复的情绪描写、"
        "可以合并的对话来回；保留：关键对白、转折、伏笔、本章必须发生的事件。\n"
        "4. 只输出压缩后的正文，不要输出任何解释或标记。"
    )
    return system, f"请压缩以下正文：\n\n{text}"


def _compression_round(provider, text: str, max_chars: int | None) -> tuple[str, dict]:
    """两轮重写仍超限时追加的一次压缩轮；返回 (候选正文或原文, 留痕记录)。

    接受条件只有一条：**字符数比原文少**。不受 `grew_too_much` 与段均/段数
    相关规则约束——这一轮的目的就是删，行文结构让位于篇幅。
    """
    limit = length_limits(max_chars)
    before = char_count(text)
    record: dict = {
        "round": "compression", "kind": "compression",
        "chars_before": before, "limit": limit["max"],
    }
    try:
        system, user = _compression_prompt(text, max_chars)
        rewritten = _strip_thinking(
            "".join(_stream_with_retry(provider, system, user)))
    except Exception as exc:  # noqa: BLE001 —— 压缩失败不阻断，保留原文
        record.update({
            "failed": True, "error": f"{type(exc).__name__}: {exc}",
            "accepted": False, "chars_after": before,
            "still_over_limit": before > limit["max"],
        })
        return text, record
    if not rewritten or len(rewritten) < len(text) * 0.3:
        record.update({
            "failed": True, "error": "empty_or_too_short",
            "accepted": False, "chars_after": before,
            "still_over_limit": before > limit["max"],
        })
        return text, record
    after = char_count(rewritten)
    accepted = after < before
    record.update({
        "candidate_chars": after,
        "accepted": accepted,
        "discarded": not accepted,
        "discard_reasons": [] if accepted else ["no_reduction"],
        "chars_after": after if accepted else before,
        "still_over_limit": (after if accepted else before) > limit["max"],
    })
    if accepted:
        return rewritten, record
    return text, record


def _rewrite_rounds(
    db, project, ctx: dict, provider, text: str, summary: str,
    forbidden: dict, gate: dict,
    research_notes: str | None = None,
    max_chars: int | None = None,
    chapter_range: str | None = None,
) -> tuple[str, str, dict | None, list[dict], dict]:
    """最多 `settings.rewrite_max_rounds` 轮重写（默认 2），每轮只针对「当前仍未过」的项。

    第二轮不是全篇重写：`gate` 每轮重算后传回 `_gate_hard_block`，提示里只列
    剩余缺口（已达标项不再出现），避免模型把第一轮刚修好的分布又改坏。

    调度侧另有「无改善即停」（`settings.rewrite_stop_without_gain`，默认开）：
    一轮候选没让闸门从不过变通过、也没让剩余问题条数严格减少，就不再调下一轮。
    轮数用尽仍不过就如实返回，不再重试——每轮都是一次完整生成成本，且继续磨
    只会让文本更平。返回 (正文, 摘要, 最后一轮 info, 每轮摘要)。

    初稿已超字数硬上限时（`settings.rewrite_skip_oversize`，默认开）直接跳过整章
    重写，交给末尾的压缩轮：报告实测超长章的重写轮净收益为负（第 24 章两轮
    净 −4 字仍超限，第 23 章两轮全丢），压缩才是有效手段。

    一致性层只有在 `coherence_guard_mode="rewrite"` 时才贡献重写理由；
    默认 report 模式下 `_coherence_reasons` 恒返回空，既有重写判定不变。
    """
    rounds: list[dict] = []
    info_last = None
    limit = length_limits(max_chars)
    max_rounds = _rewrite_max_rounds()
    stop_without_gain = _rewrite_stop_without_gain()
    skip_oversize = _rewrite_skip_oversize()
    oversize_at_entry = char_count(text) > limit["max"]
    need, reasons = _should_rewrite(text, forbidden)
    if not gate["passed"]:
        need = True
        reasons = reasons + gate["reasons"]
    coh = _coherence_reasons(db, project, text, chapter_range)
    if coh:
        need = True
        reasons = reasons + coh
    rewrite_skipped = "oversize" if (oversize_at_entry and skip_oversize) else None
    if rewrite_skipped:
        # 只改调度，不改闸门：压缩轮会重新判长度，仍超限则如实返回给落库侧。
        need = False
        reasons = []
    for round_no in range(1, max_rounds + 1):
        if not need:
            break
        gate_before, reasons_in = gate, list(reasons)
        length_before = char_count(text)
        rewritten, info = _rewrite_with_evidence(
            db, project, provider, text, reasons, forbidden,
            item_holding_block=ctx.get("item_holding_block") or None,
            constraints=ctx.get("constraints"),
            gate_info=gate_before,
            research_notes=research_notes,
            max_chars=max_chars,
        )
        if not rewritten:
            # 失败也留痕：记录第几轮、错误类型、失败前的原因清单。
            # 模型返回空串时 `_rewrite_with_evidence` 给的是 (None, None)，
            # 这里必须补一条 failed 记录，否则 rewrite_info 仍是 null、失败又被吞掉。
            fail_info = info or {"failed": True, "error": "empty_rewrite"}
            info_last = fail_info
            rounds.append({
                "round": round_no,
                "failed": True,
                "error": fail_info.get("error") or "empty_rewrite",
                "reasons_in": reasons_in,
                "metrics_before": gate_before.get("metrics"),
            })
            break
        length_after = char_count(rewritten)
        # 长度硬闸配套（2026-11-14 收紧）：已超限时，候选必须**严格低于上限且
        # 低于原文**才接受；只「没变长」不再算通过。旧写法只判「是否继续加长」，
        # 第 24 章 R2 候选 6366（仍 > 4800）因此被放行，终稿 6378 超限落库。
        # 2026-11-17：改量与下限同一个数——`char_count`（含标点与换行）。
        over_cap_before = length_before > limit["max"]
        over_limit = over_cap_before and not (
            length_after < limit["max"] and length_after < length_before
        )
        # 允许小幅增字（补神态/动作），但不给无限膨胀的口子。上浮比例与跑飞阈值
        # 都是配置项（默认 8% / 16%，手写值、非档案推导）。
        grew_too_much = length_after > length_before * (
            1 + _rewrite_growth_reject_ratio()
        )
        # C（2026-11-13）→ 2026-11-14 收窄：struct guard 回到「纯防跑飞 + 防删内容」。
        # 旧口径以 `mean_before < 28.8` 为开关、要求「段数必须下降」：本书段均长期
        # 28.0–28.2，等于每轮重写都在被要求并段——这正是单句段被压到 7.1% 的机制，
        # 与刚删掉的提示词压段指令是同一件事。段结构已不是判罚项，guard 不再对
        # 段结构下方向：不设 28.8 开关，非超长时也不看段数是否下降。
        from app.style.stylometry import paragraphs as _paragraphs
        char_before = len("".join((text or "").split()))
        paras_before = max(1, len(_paragraphs(text)))
        mean_before = char_before / paras_before
        paras_after = max(1, len(_paragraphs(rewritten)))
        char_after = len("".join((rewritten or "").split()))
        mean_after = char_after / paras_after
        # 段均句数 cap 只约束「并段」这一步，不是整轮候选的拒绝理由（2026-11-14）。
        # cap 的本职是：并段不许把段均句数堆过参考 p90（该判定在
        # `merge_ultra_short_dialogue` 里，合并前预测、超了就停并记
        # `stopped_by_spc_cap`）。重写轮的候选是模型整篇改写，spc 超 cap 只说明
        # 结果落在参考上沿之外，属于**观测项**——它不能反过来否决候选，
        # 否则等于让「并段手段的上限」决定「整章产出」这个目标。
        # 第 20 章实测：round1 候选 1.965 对 cap 1.961（只超 0.004）被整轮丢弃，
        # 而该候选的段数/段均/汉字都优于初稿，白丢一次完整生成。
        spc_cap = float((active_full_limits().get("段均句数") or {}).get("max") or 0.0)
        spc_before = _spc_of(text)
        spc_after = _spc_of(rewritten)
        spc_over = bool(spc_cap and spc_before <= spc_cap and spc_after > spc_cap)
        struct_reject = False
        # 已超长度硬上限时，「字数变少」是必须做的压缩，不能再被防删内容护栏否掉：
        # 旧口径下 oversize + 碎段同时出现时，任何重写都因 length_after<length_before
        # 被整轮回退（第 6/7 章实测两轮全废），两条规则互相封死。超长分支不再用
        # 段结构做否决项——候选是否合格由上方 `over_limit` 的长度条件决定。
        over_cap = length_before > limit["max"]
        if over_cap:
            # 超长分支只看长度（上方 over_limit）：删句/删段/并段都是压缩手段，
            # 结构规则不再反过来否决候选。
            struct_reject = False
        else:
            # 纯防作弊：不许靠删字、删内容抬指标。不再要求段数下降，
            # 也不以段均是否低于 28.8 作为开关——guard 不对段结构下方向。
            struct_reject = length_after < length_before
        # 超长时长度硬闸优先：压缩本身要靠降段数完成，不能再用段均句数把它否掉。
        # `spc_over` 不参与整轮回退：候选照旧交给闸门与长度校验，
        # 只有闸门判不过才会在下一轮继续修。
        rejected = over_limit or grew_too_much or struct_reject
        # 整轮丢弃留痕（2026-11-14）：只加计数，不放宽任何阈值。
        # 第 10 章首轮被 lengthening+growth 同时拒掉、静默白花一次完整生成，
        # 而原记录里 hanzi_after 已被回写成 before，看不出「丢掉的是哪一版」。
        # 这里显式记拒因与候选版规模，供每 5 章统计丢弃频率与拒因分布。
        discard_reasons: list[str] = []
        if rejected:
            if over_limit:
                discard_reasons.append("over_limit")
            if grew_too_much:
                discard_reasons.append("growth")
            if struct_reject:
                discard_reasons.append("structure")
            # 不再有 "spc_cap"：段均句数 cap 只约束并段，不是整轮拒因。
        cand_chars, cand_paras = length_after, paras_after
        if rejected:
            rewritten = text
            length_after = length_before
        if rewritten != text:
            summary = provider.summarize(rewritten)
        text = rewritten
        info_last = info
        gate = generation_gate(text, max_chars)
        need, reasons = _should_rewrite(text, forbidden)
        if not gate["passed"]:
            need = True
            reasons = reasons + gate["reasons"]
        coh = _coherence_reasons(db, project, text, chapter_range)
        if coh:
            need = True
            reasons = reasons + coh
        improved = _rewrite_improved(
            bool(gate_before.get("passed")),
            bool(gate.get("passed")),
            reasons_in,
            reasons,
        )
        rounds.append({
            "round": round_no,
            "chars": len(rewritten),
            "chars_before": length_before,
            "chars_after": length_after,
            "rejected_lengthening": rejected,
            "rejected_growth": grew_too_much,
            "rejected_structure": struct_reject,
            "discarded": rejected,
            "discard_reasons": discard_reasons,
            "discarded_chars": (cand_chars - length_before) if rejected else 0,
            "discarded_paras": (cand_paras - paras_before) if rejected else 0,
            "paras_before": paras_before,
            "paras_after": paras_after,
            "mean_before": round(mean_before, 1),
            "spc_before": round(spc_before, 3),
            "spc_after": round(spc_after, 3),
            "spc_cap": round(spc_cap, 3) or None,
            # 仅观测：候选自身段均句数是否越过 cap。不进 discard_reasons，
            # `rejected_spc_cap` 这个名字留给「并段被 cap 截停」——
            # 那一计数在并段侧的 `stopped_by_spc_cap` 里。
            "spc_over_cap": spc_over,
            "reasons_in": reasons_in,
            "metrics_before": gate_before.get("metrics"),
            "metrics_after": gate.get("metrics"),
            "reasons_after": gate.get("reasons") or [],
            "coherence_after": coh,
            "passed_after": not need,
            # 调度留痕（2026-11-15）：只记录这一轮有没有可测改善，以及是否因此提前停。
            "improved": improved,
            "max_rounds": max_rounds,
            "stop_without_gain": stop_without_gain,
        })
        # 没有可测改善就不再花第二次调用：候选被整轮丢弃（正文一字未变），
        # 或候选被接受但闸门仍未通过且剩余问题条数没减少。只决定调不调下一轮，
        # 不改 any 判定/阈值；`rewrite_stop_without_gain=False` 可回退旧调度。
        if stop_without_gain and (rejected or not improved):
            rounds[-1]["stopped_early"] = (
                "candidate_discarded" if rejected else "no_gain"
            )
            break
    # 重写轮结束仍超限：追加一次压缩轮（目标只有一个——压进硬上限）。
    # 压缩轮不受 grew_too_much / 段均规则约束；仍超限则如实返回，由落库侧判 failed。
    path = "direct" if not rounds else "rewrite"
    if char_count(text) > limit["max"]:
        path = "compression"
        compressed, comp_record = _compression_round(provider, text, max_chars)
        rounds.append(comp_record)
        if comp_record.get("accepted"):
            text = compressed
            summary = provider.summarize(text)
    final_chars = char_count(text)
    length_path = {
        "path": path,
        "limit": limit["max"],
        "final_chars": final_chars,
        "final_hanzi": hanzi_count(text),
        "still_over_limit": final_chars > limit["max"],
        "rewrite_skipped": rewrite_skipped,
        "compression_accepted": any(
            r.get("kind") == "compression" and r.get("accepted") for r in rounds
        ),
    }
    return text, summary, info_last, rounds, length_path


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

#: 时间线最少段数。`target_length // generation_length` 在目标字数小于单段预算时
#: 会算出 1，时间线退化为「全书一个节点」——模型只有一次演出机会，必然把整条
#: 弧光一次写完。保底 2 段，保证至少存在一次节点推进。
_MIN_TOTAL_SEGMENTS = 2


def _total_segments(project: Project) -> int:
    """全书预计段数：按字数预算算，但**不低于 2 段**。

    为什么选「至少 2 段」而不是「按场景节点数」：节点数本身是由
    `plan_timeline(total_segments=...)` 反推出来的，用节点数再回算段数会形成
    循环依赖；outline 的条目也只是剧情概括，不是可计数的场景预算。
    2 是最小的、能消除「全书 1 段」退化的口径，改动面最小。
    """
    total_chars = project.target_length or 5000
    per_seg = project.generation_length or 4000
    return max(total_chars // max(per_seg, 1), _MIN_TOTAL_SEGMENTS)


def _chapter_ordinal(label: str | None) -> int | None:
    """从章节标签取章号（复用一致性层的解析口径，不另写一套）。

    `第44–46章` 这类区间标签取**起章** 44：一个 Generation 行覆盖多章时，
    它仍然属于 44 这一章（节点选择、台账归属），推进量另用 `_chapter_last_ordinal`。
    """
    if not label:
        return None
    try:
        from app.coherence import chapter_index
        from app.generation.chapter_labels import label_span

        span = label_span(label)
        if span is not None:
            return span[0]
        return chapter_index(label)
    except Exception:  # noqa: BLE001
        return None


def _chapter_last_ordinal(label: str | None) -> int | None:
    """区间标签取**末章**（`第44–46章` → 46）；推进量必须按末章算，否则少算两章。"""
    if not label:
        return None
    try:
        from app.generation.chapter_labels import label_span

        span = label_span(label)
        return span[1] if span is not None else _chapter_ordinal(label)
    except Exception:  # noqa: BLE001
        return None


def _outline_entries(project: Project) -> list:
    """project.outline 归一成 list（list/tuple 原样，JSON 字符串尝试解析，换行串按行切）。"""
    outline = project.outline
    if isinstance(outline, (list, tuple)):
        return list(outline)
    if isinstance(outline, str):
        try:
            parsed = json.loads(outline)
            if isinstance(parsed, list):
                return parsed
        except Exception:  # noqa: BLE001
            pass
        return [line.strip() for line in outline.splitlines() if line.strip()]
    return []


def _timeline_progress(db, project: Project) -> tuple[int, bool]:
    """时间线推进量：`(已写章数 / 已写段数, 是否按章推进)`。

    修复项 2 的核心口径：
    - 只要历史 Generation 里存在可解析的章节标签（第N章/回/节/卷），
      推进量 = **最大章号**。重写同一章（多个版本）章号不变 → 推进量不变；
    - 一个章节标签都没有（老项目/纯段标签）→ 回退到行数，保持旧行为。
    """
    if db is None:
        return 0, False
    try:
        rows = db.execute(
            select(Generation.chapter_range)
            .where(Generation.project_id == project.id)
            .order_by(Generation.segment_no)
        ).scalars().all()
    except Exception:  # noqa: BLE001
        return 0, False
    ordinals = [o for o in (_chapter_last_ordinal(r) for r in rows) if o]
    if ordinals:
        return max(ordinals), True
    return len(rows), False


def _chapter_rows(
    db, project: Project, up_to_chapter: int | None
) -> list[tuple[int | None, str | None, str]]:
    """取前序章节的 (章号, 标签, 正文)。

    `up_to_chapter` 给定时只取**章号 < 它的已定稿章节**——当前章的旧版本
    （重写前那一稿）不能反过来给自己提供「已出场 / 性别」证据。
    """
    if db is None:
        return []
    try:
        rows = db.execute(
            select(Generation.chapter_range, Generation.text)
            .where(Generation.project_id == project.id)
            .order_by(Generation.segment_no)
        ).all()
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int | None, str | None, str]] = []
    for label, text in rows:
        idx = _chapter_ordinal(label)
        if up_to_chapter is not None and (idx is None or idx >= up_to_chapter):
            continue
        out.append((idx, label, text or ""))
    return out


def _infer_gender_from_texts(name: str, texts: list[str]) -> str:
    """从前序正文里「名字所在句结束后，下一句的主语代词」推断性别。

    只在**主语延续**位置取证据：`甲……。她……`。名字后窗口里的她/他 会把
    宾语（"甲看着他"）和别人的代词算进来，实测同一段会出现 3:3 的死锁；
    改成只看下一句句首代词后，同一批正文能得到 1:0 / 0:24 这类干净证据。

    判定：一方有证据且另一方为零 → 直接采用；双方都有 → 需要 3 条以上
    且一方 ≥ 2 倍另一方、占比 ≥ 70%。否则返回空串（不猜）。
    """
    if not name:
        return ""
    female = male = 0
    sentence_end = "。！？…"
    for text in texts:
        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            j = i + len(name)
            # 跳过名字所在句的剩余部分（到句末或换行）
            while j < len(text) and text[j] not in sentence_end and text[j] != "\n":
                j += 1
            k = j
            while k < len(text) and (text[k] in sentence_end or text[k].isspace()):
                k += 1
            if text.startswith("她", k):
                female += 1
            elif text.startswith("他", k):
                male += 1
            start = i + len(name)
    if female and not male:
        return "女"
    if male and not female:
        return "男"
    total = female + male
    if total >= 3 and female >= 3 and female >= 2 * male and female / total >= 0.7:
        return "女"
    if total >= 3 and male >= 3 and male >= 2 * female and male / total >= 0.7:
        return "男"
    return ""


def _backfill_character_cards(db, project: Project, up_to_chapter: int | None) -> None:
    """从已定稿正文回写角色性别与首次出场章节（修复项 3/4）。

    - 只补空字段，绝不覆盖作者显式写下的 gender / first_chapter；
    - 性别按「名字后 80 字窗口」里的她/他计数，证据不足不写；
    - 首次出场章节取最早出现该名字的**已定稿章节**；当前章旧稿不参与。
    """
    bible = project.plot_bible
    if not isinstance(bible, dict):
        return
    chars = bible.get("characters")
    if not isinstance(chars, list):
        return
    need = [
        c for c in chars
        if isinstance(c, dict) and str(c.get("name") or "").strip()
        and (not character_gender(c) or character_first_chapter(c) is None)
    ]
    if not need:
        return
    rows = _chapter_rows(db, project, up_to_chapter)
    if not rows:
        return
    texts = [t for _, _, t in rows]
    new_bible = copy.deepcopy(bible)
    changed = False
    for card in new_bible.get("characters") or []:
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        if not name:
            continue
        if not character_gender(card):
            gender = _infer_gender_from_texts(name, texts)
            if gender:
                set_character_gender(card, gender)
                changed = True
        if character_first_chapter(card) is None:
            for idx, _label, text in rows:
                if idx and name in text:
                    set_character_first_chapter(card, idx)
                    changed = True
                    break
    if not changed:
        return
    try:
        project.plot_bible = new_bible
        db.add(project)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _appeared_character_names(
    db, project: Project, reminders: dict | None, up_to_chapter: int | None
) -> set[str]:
    """前序章节已出现过的人名集合（只算章号 < 当前章的已定稿章节）。"""
    chars, _ = merged_constraint_items(reminders or {}, project.plot_bible)
    names = [str(c.get("name") or "").strip() for c in chars if isinstance(c, dict)]
    names = [n for n in names if n]
    if not names:
        return set()
    rows = _chapter_rows(db, project, up_to_chapter)
    if not rows:
        return set()
    blob = "\n".join(text for _, _, text in rows)
    return {name for name in names if name in blob}


def _merge_gender_into_reminders(reminders: dict | None, plot_bible: dict | None) -> None:
    """把 bible 里的性别补进 KG reminders 的同名角色卡（bible 卡已渲染时只是兜底）。"""
    if not isinstance(reminders, dict) or not isinstance(plot_bible, dict):
        return
    by_name: dict[str, str] = {}
    for card in plot_bible.get("characters") or []:
        if isinstance(card, dict):
            name = str(card.get("name") or "").strip()
            gender = character_gender(card)
            if name and gender and name not in by_name:
                by_name[name] = gender
    if not by_name:
        return
    for card in reminders.get("character_constraints") or []:
        if not isinstance(card, dict) or character_gender(card):
            continue
        name = str(card.get("name") or "").strip()
        if name in by_name:
            card["gender"] = by_name[name]


def _timeline_state_for(
    project: Project, written_segments: int, provider=None, db=None, *, chapter_based: bool = False
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
    total_segments = _total_segments(project)
    outline_list = _outline_entries(project)
    total_chapters = len(outline_list) or None

    # 路径 1：尝试从存档恢复
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    st = (
        state_from_dict(saved, total_segments=total_segments, total_chapters=total_chapters)
        if saved else None
    )

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
            filled = plan_nodes_with_llm(provider, outline_list, project.plot_bible, st.total_main)
            if filled:
                for node in filled:
                    if node.id in st.nodes:
                        node.segment_budget = st.nodes[node.id].segment_budget
                        st.nodes[node.id] = node
                st.planning_status = "llm"
            else:
                # 规划不完整要显式记录：占位节点照用（不阻断生成），但状态
                # 必须能让调用方/端点一眼看出「这不是模型规划的结果」。
                st.planning_status = "placeholder"
                st.planning_reason = (
                    "模型规划失败（超时或返回的节点解析不出），已回退占位节点；"
                    "可调用 POST /api/projects/{id}/timeline/plan 重试"
                )
        else:
            st.planning_status = "placeholder"
            st.planning_reason = (
                "未配置可用模型渠道，使用占位节点（标题/节拍为空）；"
                "配置渠道后可调用 POST /api/projects/{id}/timeline/plan 重规划"
            )
        # 无 LLM / 规划失败时，占位节点也要有章节区间，保证「章 → 点」映射存在。
        if total_chapters:
            assign_chapter_ranges(st, total_chapters)
    else:
        # 存档存在：
        # - 章号驱动模式（历史里有第N章标签）：直接按当前章同步节点，
        #   不再用 Generation 行数 advance——重写/换版本不会把大纲往后推；
        # - 旧模式（没有章号标签）：保持原 advance 补齐行为。
        if chapter_based and has_chapter_ranges(st):
            st = sync_node_for_chapter(st, written_segments + 1)
        else:
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
    total_segments = _total_segments(project)
    # 章区间必须一起复算：章模式靠它把 current_node 对齐章号；不传的话老存档
    # 的 chapter_start/end 全是 0，支线挂载会拿不到主干锚点。
    outline_list = _outline_entries(project)
    # 不传 provider 给 state_from_dict（用最新 total_segments 复算骨架）
    return state_from_dict(
        saved, total_segments=total_segments, total_chapters=len(outline_list) or None
    )


#: 证据化 judge 只看正文末尾这么多字符：退出条件通常在后半段落实，
#: 全章塞进去会让一次判定多出数千字符的段落坐标表；超出部分不影响结论。
_JUDGE_TEXT_CHARS = 2000


def _judge_verdict(judge: dict) -> bool | None:
    """三态证据判定 → 旧布尔契约。

    `supported` 才算完成；`contradicted` 明确未完；`unknown`（含解析失败、
    证据不足）保持 None，继续走段数预算兜底。
    """
    return {"supported": True, "contradicted": False}.get(judge.get("verdict"))


def _judge_evidence(judge: dict) -> dict:
    """给接口/质量回执的证据摘要；只留指纹与来源 ID，不复制正文。"""
    audit = judge.get("audit") or {}
    return {
        "verdict": judge.get("verdict"),
        "reason": judge.get("reason"),
        "source_ids": [
            s.get("paragraph_id") for s in (judge.get("sources") or [])
            if s.get("paragraph_id")
        ],
        "text_hash": audit.get("text_hash"),
        "plan_hash": audit.get("plan_hash"),
        "error_category": audit.get("error_category"),
    }


def _judge_and_advance(
    db,
    project: Project,
    provider,
    recent_text: str,
    written_segments: int,
    *,
    progress_before: int | None = None,
    chapter_based: bool = False,
) -> dict:
    """v2.1：commit 段后调一次 LLM 判定当前节点 exit_condition 是否达成。

    - 判定 True：取最新 state → force_complete_current → 落库
    - 判定 False：推进到当前章/段对应节点 → 落库
    - 判定 None（失败/不确定/没 exit）：同上
    - 没 outline / state 不可用：返回 skipped=True，不动 state

    修复项 2：`chapter_based=True` 时推进量取自**章号**（`_timeline_progress`）。
    如果本次 commit 没有让最大章号变大（重写、换版本、重跑同一章），直接
    `skipped_reason="rewrite_same_chapter"`，不推进会消耗大纲段位。
    """
    out = {"called": False, "judge": None, "forced": False, "advanced": False, "skipped": True}
    if not project.outline:
        return out
    judge_text = (
        recent_text[-_JUDGE_TEXT_CHARS:]
        if len(recent_text) > _JUDGE_TEXT_CHARS else recent_text
    )

    if chapter_based:
        progress_after, _ = _timeline_progress(db, project)
        if progress_before is not None and progress_after <= progress_before:
            out["skipped_reason"] = "rewrite_same_chapter"
            return out
        # 按章同步：written_segments 传「刚写完章号 - 1」，_timeline_state_for 会同步到该章。
        st = _timeline_state_for(
            project, max(progress_after - 1, 0), provider=None, db=None, chapter_based=True
        )
        if st is None or not st.current_node or st.current_node not in st.nodes:
            return out
        st = sync_node_for_chapter(st, progress_after)
        out["advanced"] = True
        out["called"] = True
        out["skipped"] = False
        cur_id = st.current_node
        cur_node = st.nodes[cur_id]
        if cur_node.exit_condition:
            judge = judge_node_completion_with_evidence(
                provider, cur_node, judge_text,
                source_id=f"segment:{progress_after}",
            )
            st.completion[cur_id] = judge
            st.last_judged_segment = progress_after
            out["judge"] = _judge_verdict(judge)
            out["evidence"] = _judge_evidence(judge)
            if out["judge"] is True:
                # 章模式：提前完成只记成显式状态（done + early_done），
                # current_node 不提前切走——注入点必须继续与章号对齐；
                # 下一次 sync 重算 done 时会并上 early_done，不会被回滚。
                st = force_complete_current(st, keep_current=chapter_based)
                out["forced"] = True
        _persist_timeline_state(db, project, st)
        out["current_node"] = st.current_node
        out["done"] = sorted(st.done)
        return out

    st = _timeline_state_for(project, written_segments, provider=None, db=None)
    if st is None or not st.current_node or st.current_node not in st.nodes:
        return out
    cur_id = st.current_node
    cur_node = st.nodes[cur_id]

    # 1) 跑正常 advance（不管 LLM 怎么判，段数 +1）
    st = advance(st)
    out["advanced"] = True
    out["called"] = True
    out["skipped"] = False

    # 2) 调 LLM 判定——但只在「advance 之后该节点还未 done」时调
    # （如果 advance 已让它 done，就没必要再让 LLM 重复判定）
    if cur_id not in st.done and cur_node.exit_condition:
        judged_segment = max(1, written_segments + 1)
        judge = judge_node_completion_with_evidence(
            provider, cur_node, judge_text, source_id=f"segment:{judged_segment}",
        )
        st.completion[cur_id] = judge
        st.last_judged_segment = judged_segment
        out["judge"] = _judge_verdict(judge)
        out["evidence"] = _judge_evidence(judge)
        if out["judge"] is True:
            st = force_complete_current(st)
            out["forced"] = True

    # 3) 落库（用新 state 覆盖）
    _persist_timeline_state(db, project, st)
    out["current_node"] = st.current_node
    out["done"] = sorted(st.done)
    return out


def current_node_prompt(
    project: Project,
    written_segments: int,
    provider=None,
    db=None,
    *,
    chapter: int | None = None,
    chapter_based: bool = False,
) -> str | None:
    """算出当前剧情点的提示块（时间线启用时）；无 outline 返回 None。

    供 generate_next_segment 与 generate-plan 共用——两条路径必须下发同一份提示，
    否则预检看到的不算数。written_segments = 已写段数 / 已写章数（chapter_based）。

    修复项 2：传了 `chapter`（或 chapter_based 时用 written+1）就按**章号**
    选节点，而不是按历史行数；重跑同一章只会命中同一个节点。
    `db` 用于持久化；预检/读侧可不传（仅恢复，不写）。
    """
    st = _timeline_state_for(
        project, written_segments, provider, db=db, chapter_based=chapter_based
    )
    if st is None:
        return None
    if chapter is None and chapter_based:
        chapter = written_segments + 1
    if chapter is not None and has_chapter_ranges(st):
        st = sync_node_for_chapter(st, chapter)
    if not st.current_node or st.current_node not in st.nodes:
        return None
    prompt = build_node_prompt(
        st.nodes[st.current_node], st.current_beat, timeline_progress(st)
    )
    # 章模式：支线与主线并行——注入点仍是章号对齐的主干节点（上面那块），
    # 支线单独给一小块「本段可穿插推进」的说明；绝不把支线当当前节点。
    if chapter_based and st.branch_current:
        bnode = st.nodes.get(st.branch_current)
        if bnode is not None and bnode.kind == "branch":
            tip = (bnode.entry_condition or bnode.title or "").strip()
            if tip:
                prompt += f"\n【并行支线】{bnode.title}：{tip}（可穿插推进，不计入主线节点推进）"
    return prompt


def generate_next_segment(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
    chapter_range: str | None = None,
    fresh_start: bool = False,
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
        max_chars = project.generation_length or 4000

    # 先把摘要树补齐再组装前情：老项目（步骤 2 之前建的）只有 L1、没有 L2/L3，
    # 直接 compose 会因粗层缺失而退化成全量（虽不留洞，但白吃预算）。
    # 这一步幂等——树已一致时零 LLM 调用，所以常态下没有额外开销。
    if fresh_start:
        # 正式开篇：清空**活动**摘要链，让第 1 章从零开始。既有 Generation 行
        # （试验段）不删，它们的 summary 仍在各自行里，只是不再进前情。
        project.summary = []
        project.summary_tree = empty_tree()
        db.commit()
    else:
        _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids, fresh_start=fresh_start)
    overrides = _prompt_overrides(db, project.id)
    # 首段判定与 build_user_prompt 同源：没有前情就是开篇。system 侧据此遮蔽
    # 角色底牌、降权压缩趋势，必须与 user 侧同口径，否则两边各说一套。
    is_opening = not ctx["prior_blocks"] and not ctx["prior_summaries"]
    # —— 时间线：算出当前剧情点提示（有 outline 才启用；无则 None 走原逻辑）——
    # written = Generation 行数（段号用）；timeline_written = 时间线推进量。
    # 修复项 2：有章节标签时时间线按「已写章数」推进，重写/换版本不再占大纲段位。
    written = 0 if fresh_start else _next_segment_no(db, project.id) - 1
    timeline_written, chapter_based = (
        (0, False) if fresh_start else _timeline_progress(db, project)
    )
    chapter = _chapter_ordinal(chapter_range)
    if chapter is None and chapter_based:
        chapter = timeline_written + 1
    node_prompt = None if fresh_start else current_node_prompt(
        project, timeline_written, provider, db=db,
        chapter=chapter, chapter_based=chapter_based,
    )
    # 修复项 3/4：从已定稿正文回写性别/首次出场章节；再算「前序已出现的人名」。
    # 章号驱动项目只取当前章之前的稿（避免重写时用上一稿给自己作证）；
    # 无章节标签的老项目取全部历史，保持旧行为。
    evidence_up_to = chapter if chapter_based else None
    if chapter is not None:
        _backfill_character_cards(db, project, up_to_chapter=evidence_up_to)
    _merge_gender_into_reminders(ctx["reminders"], project.plot_bible)
    appeared_names = (
        _appeared_character_names(db, project, ctx["reminders"], up_to_chapter=evidence_up_to)
        if chapter is not None else None
    )
    chapter_outline = chapter_outline_entry(project.outline, chapter)
    chapter_context = "\n".join(x for x in (chapter_outline, node_prompt) if x)
    # user 尾部的锁定名单也要按同一口径收窄，否则 system 说「未列出不要出场」、
    # 尾部 constraints 又把全量名字念一遍，两条约束继续打架。
    if chapter is not None:
        all_chars, _ = merged_constraint_items(ctx["reminders"], project.plot_bible)
        eligible_chars, _ = filter_available_characters(
            all_chars,
            chapter=chapter,
            chapter_context=chapter_context or None,
            appeared_names=appeared_names,
        )
        ctx["constraints"]["characters"] = [
            c.get("name") for c in eligible_chars if c.get("name")
        ]
    era_block, era_prompt_meta, power_block, power_meta = _guard_blocks(project)
    # v12：闸门带（含对白配比参考线）必须在拼提示**之前**写入当前 context——
    # 提示里的极短纯对白段上限与对白参考线都读它；并段仍在其后，硬上限不变。
    gate_limits_meta = _apply_gate_limits(db, project, seg_no=written + 1)
    # 初稿提示的字数下限与补完触发线同源（见 `_draft_prompt_min_chars`）；
    # 节奏口径缓存命中时零成本，提前到这里只为拼提示时能取到同一个值。
    rhythm = _archive_rhythm(db, project)
    prompt_min_chars = _draft_prompt_min_chars(max_chars, rhythm)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
        era_constraint=era_block,
        power_constraint=power_block,
        style_source_ids=_style_source_ids(project),
        is_opening=is_opening,
        narrative_mode="original",
        catalog_skeleton=_catalog_skeleton(db, project),
        max_chars=max_chars,
        chapter=chapter,
        chapter_context=chapter_context or None,
        appeared_names=appeared_names,
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
        item_holding_block=ctx.get("item_holding_block") or None,
        chapter_outline=chapter_outline,
        min_chars=prompt_min_chars,
        advance_block=_advance_block(db, project, chapter),
        story_cards_block=_render_story_cards(db, project.id),
        foreshadow_block=ctx.get("foreshadow_block"),
    )

    from app.generation.author_research import (
        backfill_adoption,
        public_summary,
        run_author_research,
    )

    # 自查默认关闭（settings.author_research_enabled）。关闭时只落一个
    # `status="disabled"` 的占位，不跑工具、不调模型、不写 retrieval_logs。
    research: dict = {
        "enabled": False, "status": "disabled", "calls": [], "notes_block": "",
    }
    # v2.6：节点粒度根治——per-request 传 stop token（不用单例 setter，避免跨请求污染）
    # 重写/重生成是 AI 味修复，不传 stop
    # v16：外部素材工具只在初稿的**素材轮**可见（结构化返回）；重写轮/压缩轮不碰。
    brief_tool, brief_meta = _start_brief_tool(db, project, written + 1)
    # 素材轮非流式（要结构化 tool_calls）；末轮成文仍走原来的 chat / stream_chat。
    brief_blocks = _gather_brief_materials(
        provider, brief_tool, system, user, constraints=ctx["constraints"])
    draft_user = user + _material_suffix(brief_blocks)
    text = _chat_with_retry(
        provider, system, draft_user,
        constraints=ctx["constraints"],
        stop_sequences=get_section_stop_sequences(),
    )
    # v15：初稿撞 `max_tokens`（finish_reason=length）时**优先重生成**，
    # 不在断头稿上直接压缩——压缩只会按比例缩短，断在半路的剧情不会自己接上。
    draft_finish_reason = getattr(provider, "last_finish_reason", None)
    text, truncation_info = _regenerate_if_truncated(
        provider, text, draft_finish_reason,
        lambda: _chat_with_retry(
            provider, system, draft_user, constraints=ctx["constraints"],
            stop_sequences=get_section_stop_sequences(),
        ),
    )
    # 截断判据两条独立：`finish_reason=length`（供给侧硬顶）与 `ends_complete`
    # （末字符不是句末标点）。后者在补写/并段/重写之前对**初稿**取一次。
    draft_ends_complete = bool(
        analyze_repetition(text).metrics.ends_complete if text.strip() else False
    )
    # 1) 篇幅缺口先补写，再进闸门（重写轮不负责补篇幅，见 _complete_short_draft）。
    #    补写门槛与极短对白上限都从参考档案的分位推（_archive_rhythm），换书自动适配。
    #    rhythm 已在拼提示前取过（缓存命中），此处直接复用。
    # 并段的硬上限是参考语料章级「段均句数」p90（闸门带已在拼提示前写入）。
    merge_cap = _merge_spc_cap(gate_limits_meta)
    text, completion_info = _complete_short_draft(
        provider, text, max_chars,
        min_ratio=rhythm.get("completion_min_ratio"),
        stop_ratio=rhythm.get("completion_stop_ratio"),
        constraints=ctx["constraints"],
    )
    # 2) 极短纯对白段：生成后确定性并段，不再只写在 prompt 里指望模型遵守。
    #    只并进「非单句叙述段」/纯对白段，且不得把段均句数推过参考 p90。
    text, merge_info = merge_ultra_short_dialogue(
        text, names=list(appeared_names or []),
        max_hanzi=rhythm.get("ultra_dialogue_max"),
        spc_cap=merge_cap,
    )
    summary = provider.summarize(text)

    # AI 味检测 + 针对性重写
    narrative = _style_narrative(db, project)
    forbidden = (narrative or {}).get("forbidden") or {}
    rewrite_info = None
    # 生成侧硬闸门：与 AI 腔判据并列，命中任一条就重写；最多两轮，第二轮只补剩余缺口。
    # 判罚带按项目参考语料的章级分位现推（换参考书自动重推；推不出退回常量表）。
    gate = generation_gate(text, max_chars)

    # —— 写后自查（2026-11-13 v2：缺口驱动；默认关闭）——
    # 只有开关打开且初稿没过闸门才查；查询词由失败项反推。3–8 轮工具调用，
    # 笔记只进重写轮。默认 `author_research_enabled=False`：段 11 实测 1/58 的
    # 自查 token 只换来与段 9 持平的闸门表现，且段落类缺口方向相反，故不默认跑。
    if settings.author_research_enabled and not gate.get("passed"):
        research = run_author_research(
            db, project, provider, ctx,
            segment_no=written + 1,
            gate=gate,
            max_chars=max_chars,
            node_prompt=node_prompt,
            skeleton=_catalog_skeleton(db, project),
        )

    text, summary, rewrite_info, rewrite_rounds, length_path = _rewrite_rounds(
        db, project, ctx, provider, text, summary, forbidden, gate,
        research_notes=research.get("notes_block") or None,
        max_chars=max_chars,
        chapter_range=chapter_range,
    )

    # 重写轮会重新排版，可能新造「整段只有引号、≤4 汉字」的对白段（第 5 章实测
    # 终稿 0.092，而首轮本来是 0）。确定性并段必须在重写之后再跑一次，否则它
    # 在重写轮被绕过——那是唯一能保证该指标恒为 0 的位置。
    text, merge_after = merge_ultra_short_dialogue(
        text, names=list(appeared_names or []), spc_cap=merge_cap
    )

    # 批 6：文风形态校正链（改写 → 形态 → 归属 → 省略号 → beat → 照搬）。
    # 放在并段之后、时代自检之前：它量的是「即将落库的正文」。
    text, style_fix = _apply_style_corrections(
        db, project, provider, text, novel_ids=novel_ids)
    if any((style_fix.get(k) or {}).get("applied") for k in (
            "rewrite_info", "form_info", "attr_info", "ell_info", "beat_info")):
        summary = provider.summarize(text)

    # 时代越界自检：默认 report（只扫描、落 meta，不改正文）。
    # 显式 rewrite 模式下才可能改 text；改过就重算摘要，避免摘要与正文脱节。
    era_before = text
    text, era_meta = _apply_era_guard(
        text, project, provider, written + 1, ctx["reminders"]
    )
    if text != era_before:
        summary = provider.summarize(text)

    # 复读闸门（P1 · I2）：落库前算好，与正文同一次 INSERT 落 quality_json。
    # 篇幅判据用整段预算；P1 是新段生成，不是改写。
    quality, repetition, warnings = _quality_check(
        text, budget=project.generation_length)
    # 落库前对**最终正文**再量一次闸门（重写后可能已修好，也可能仍不达标）。
    # 这是观测与告警口径，不改正文；是否重写在上面已经决定过了。
    gate_final = generation_gate(text, max_chars)
    quality["style_gate"] = gate_final
    # 批 6：校正链的逐道留痕（跑没跑、为什么没跑、改了哪些指标）。
    quality["style_corrections"] = style_fix
    # v15：结构检测（情节点覆盖 / 越章材料 / 人物性别一致），只写留痕、不改正文；
    # 是否阻断由 `structure_guard_mode` 在 `_commit_segment` 决定。
    quality["structure_check"] = _run_structure_check(project, text, chapter, ctx)
    # 逐章推进台账（v1）：角色阵容 / 属性推进 / 章间衔接，只判不拦——
    # 只写留痕，`mode=report`，不改 `passed`、不进 `violations`。
    quality["progress_ledger"] = _run_progress_ledger(
        db, project, text, chapter, quality.get("structure_check"))
    # v17：短句占比偏低——只判不拦（章级口径），写留痕，不改 passed / violations。
    quality["short_sentence_check"] = _short_sentence_check(
        gate_final.get("metrics") or {}, gate_limits_meta)
    # v17：对白配比（只判不拦，章级 p25/p50/p75）。
    quality["dialogue_ratio_check"] = _dialogue_ratio_check(
        project, gate_final.get("metrics") or {}, gate_limits_meta)
    # v19：句长形状（中段拥挤度 / 上尾占比，只判不拦）。
    quality["sentence_shape_check"] = _sentence_shape_check(
        text, gate_final.get("metrics") or {}, gate_limits_meta)
    # v17：时代越界扫描留痕（只判不拦；扫描由上面的 `_apply_era_guard` 完成）。
    quality["era_check"] = _era_check_quality(
        era_meta, era_prompt_meta, _era_channel_abuse(db, project, era_meta))
    # 长度路径留痕：走的是 direct（未重写）/ rewrite（重写轮内压回）/ compression
    # （两轮重写后追加压缩轮）。终稿字数与是否仍超限按**最终正文**重算（并段可能加字）。
    # 2026-11-17：字数 = `char_count`（字符，含标点），与下限同一个数。
    _final_chars = char_count(text)
    quality["length_path"] = {
        "path": length_path["path"],
        "limit": length_path["limit"],
        "final_chars": _final_chars,
        "final_hanzi": gate_final.get("hanzi"),
        "still_over_limit": _final_chars > length_path["limit"],
        "compression_accepted": length_path["compression_accepted"],
    }
    # v16：外部素材工具留痕（问了几次/问了什么/拿回多少字/缓存命中几次）。
    # 未启用时只落「为什么没给工具」，与「开了但模型一次没问」可区分。
    # 采纳粗判必须用**最终正文**：只记注入条数看不出素材有没有帮上忙。
    if brief_tool is not None:
        brief_tool.note_chapter_text(text)
    quality["brief_tool"] = _brief_tool_quality(brief_tool, brief_meta)
    # v15：截断留痕。两条判据独立：供给侧的 `finish_reason=length`，与正文末字符
    # 是否收束（`ends_complete`）。`final_from_truncated_draft` 为真 = 终稿源自
    # 被截断的初稿（典型是压缩轮在断头稿上收口）；此时 `passed` 一律压为 False，
    # 被截断的稿子不能冒充合格章。留痕随正文同一次 INSERT 落 `quality_json`。
    final_ends_complete = quality.get("truncated") is False
    truncated_source = bool(
        (truncation_info.get("draft_truncated_by_tokens")
         or not draft_ends_complete)
        and not truncation_info.get("accepted_regenerated")
    )
    quality["truncation"] = {
        **truncation_info,
        "draft_ends_complete": draft_ends_complete,
        "final_ends_complete": final_ends_complete,
        "final_from_truncated_draft": truncated_source,
        "final_from_truncated_draft_via_compression": bool(
            truncated_source and length_path.get("compression_accepted")),
        "degrade_note": (
            "终稿来自截断稿：初稿被 max_tokens 截断，压缩轮在断头稿上收口"
            if truncated_source and length_path.get("compression_accepted")
            else ("终稿正文本身未收束" if not final_ends_complete else None)
        ),
    }
    quality["passed"] = bool(
        gate_final.get("passed") and final_ends_complete and not truncated_source
    )
    if truncated_source:
        warnings.append("初稿被截断，终稿来自截断稿，本章按不合格章记录")
    # 提示版本留痕：这一章是在哪套提示下生成的（与闸门 v11 口径对齐）。
    quality["prompt_revision"] = PROMPT_REVISION
    # 单章只判结构合规；指纹 13 项单章只记录，卷末再与参考卷均值对照。
    quality["gate_limits"] = {
        k: gate_limits_meta.get(k)
        for k in ("source", "chapters", "volumes", "volume_chapters",
                  "volume_chapters_source", "reference_novels", "chapter_limits",
                  "volume_reference", "ultra_short_limit", "dialogue_reference",
                  "sentence_reference", "rhythm_reference",
                  "dialogue_mix_reference",
                  # v20：句子口径与形状带随章落库（同步版与流式版同款）。
                  # `sentence_scope_from_chapter` 仅记录、无判罚效力（无任何分支读取）。
                  "sentence_scope", "sentence_scope_from_chapter",
                  "short_sentence_max_len", "sentence_shape",
                  # v22：段数下限的折算结果与来源（reference_p10_per_1k /
                  # constant_per_1k），用于复核「这个数是怎么来的」。
                  "段数",
                  "fallback_used", "fallback_reason")
    }
    _attach_volume_gate(db, project, quality, written + 1, chapter or written + 1,
                        gate_final["metrics"], gate_limits_meta)
    # 软档观测：偏离档案四分位带但硬闸门判不出来的项（如对白 16.2% vs 中位 22.6%）。
    # 只记录，不影响 passed、不触发重写。
    quality["soft_deviations"] = gate_final.get("soft_deviations") or []
    # C（2026-11-13）：重写轮次随正文落库。此前只进接口返回值，事后查不到
    # 「跑了几轮、哪轮失败、为什么没修好」——第 4 章第 2 轮 HTTP 400 就是这样丢的。
    quality["rewrite_rounds"] = rewrite_rounds
    # 补写与并段都是「生成后确定性处理」，必须随正文留痕：补了多少、并了几段、
    # 有没有因为复述/复读被丢弃，否则事后无法区分「模型写够的」和「补出来的」。
    if completion_info.get("triggered"):
        quality["draft_completion"] = completion_info
    # 留痕条件含 `skipped_single_neighbor`（为保住单句段峰而主动放弃）与
    # `stopped_by_spc_cap`（段均句数已到参考 p90）：否则事后只看到「并了 0 段」，
    # 分不清是模型没造碎段、还是护栏挡住的。
    if _merge_touched(merge_info):
        quality["dialogue_merge"] = merge_info
    if _merge_touched(merge_after):
        quality["dialogue_merge_after_rewrite"] = merge_after
    if rewrite_info:
        quality["rewrite_info"] = rewrite_info
    if any(r.get("failed") for r in rewrite_rounds):
        warnings.append("重写轮失败，终稿降级为首轮稿（详见 quality.rewrite_rounds）")
    if not gate_final["passed"]:
        warnings.append("文风硬指标未达标：" + "；".join(gate_final["reasons"]))

    # 段末回填「查了有没有用」：能程序判的判（规则参数是否出现在正文、anchor 原文
    # 是否有字面重合），判不了的显式记 unjudgeable；同时把判定的 adopted_rule_ids
    # 写回 retrieval_logs（未回填 = 未统计，不能当成「查了不用」）。
    author_research = public_summary(
        research, backfill_adoption(db, research, text)
    )
    quality["author_research"] = author_research

    gen, segment_no = _commit_segment(
        db, project, text, summary,
        meta={"era": {**era_prompt_meta, **era_meta}, "power": power_meta},
        quality=quality,
        chapter_range=chapter_range,
        prompt_json=_prompt_snapshot(system, draft_user),
        authority_json=_authority_snapshot(project, ctx, chapter_range),
    )

    # —— v2.1：exit_condition LLM 判定 → 必要时提前切节点（§13 待定项落地）——
    # 此时本段刚 commit 完，state 还没 advance（advance 是下次预检时触发的）。
    # 在「commit 之前」的 current_node + 刚生成的正文上判定。
    # 失败/不确定/节点没 exit → 走段数预算兜底，逻辑照旧。
    # 正式开篇（fresh_start）没有时间线状态，跳过节点判定。
    node_judge = None if fresh_start else _judge_and_advance(
        db, project, provider, text, written,
        progress_before=timeline_written,
        chapter_based=chapter_based,
    )

    # —— 图谱进入生成闭环 ——
    # 每段生成后增量合并事实到 generated 图（不清空、upsert），
    # 使下一次生成的 reminders 能拿到本段新出现的角色 / 新埋的伏笔。
    # 复读退化段**跳过**（标记而非拒绝）：循环对白一旦并入 facts 会污染后续
    # reminders，且重写路径本来就不合并 KG，无法靠重写修回来。
    if _is_degenerate(quality):
        kg_stats = {
            "merged": False,
            "skipped": "repetition_degenerate",
            "reason": "本段疑似复读，已跳过 KG 抽取",
        }
    else:
        kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
    power_meta = _attach_power_meta(db, gen, power_meta, kg_stats)

    # v3.0 异步批抽：入队 extractor_v3 落 9 张表（Judge/检索切片/读侧 API 的数据源）。
    # 零 LLM 零阻塞，失败静默兜底。状态透传给前端（v3_extraction 键）。
    v3_extraction = _enqueue_v3_extraction(
        db, project, segment_no, text, chapter=_chapter_ordinal(chapter_range))
    # KG 分层影子抽取（默认关闭）：独立库、异步、不参与注入，返回值不外露。
    _shadow_ingest(project, segment_no, text)

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
    if fresh_start:
        # 开篇没有时间线 state（judge 已跳过），支线滚动无状态可读。
        branch_info = None
    else:
        _new_state, branch_info = _roll_branch(
            db, project, _load_timeline_state_for_branch(db, project, written),
            provider, _post_commit_written, last_branch_written=None,
            chapter_based=chapter_based,
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
        "rewrite_rounds": rewrite_rounds,
        "author_research": author_research,
        "repetition": repetition,
        "length": (quality or {}).get("length"),
        "summary_style": (quality or {}).get("summary_style"),
        # 结构检查（beat / premature / gender）连同每项的 skipped 原因一起回传，
        # 前端不再需要自己猜「为什么没检查」。
        "structure_check": (quality or {}).get("structure_check"),
        "warnings": warnings,
    }


def generate_next_segment_stream(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
    chapter_range: str | None = None,
):
    """流式续写生成器：逐 delta 产出 {type:chunk} 事件，完成后落库并返回 {type:done}。

    与 generate_next_segment 语义一致（组装→续写→总结→落库→更新主线摘要链），
    区别在续写过程逐块 yield，供 SSE 转发——长文首字即显、不干等。

    v2.3：传 `node_prompt` 给 build_user_prompt——流式版也走时间线模式，
    剔除 plot_bible 全字段 + premise。commit 段后调 `_judge_and_advance`
    （与同步版同口径）—— yield 一个 `node_judge` 事件。
    """
    from app.llm.provider import get_llm_provider

    yield {"type": "stage", "stage": "prepare", "label": "装配上下文"}

    # 档案预检：与 generate_next_segment 同——无缓存档案时明确失败，
    # 不把请求挂死在同步蒸馏上（流式下作为 SSE error 事件下发）。
    _require_distill_ready(db, project)

    if provider is None:
        provider = get_llm_provider()
    if max_chars is None:
        max_chars = project.generation_length or 4000

    # 同 generate_next_segment：先补齐粗层，再拼有界前情（幂等，常态零开销）
    _rollup_summary_tree(db, project, provider)

    ctx = build_context(db, project, novel_ids)
    overrides = _prompt_overrides(db, project.id)
    is_opening = not ctx["prior_blocks"] and not ctx["prior_summaries"]
    # —— 时间线：算出当前剧情点提示（与同步版同口径，db 用于落库）——
    written = _next_segment_no(db, project.id) - 1
    timeline_written, chapter_based = _timeline_progress(db, project)
    chapter = _chapter_ordinal(chapter_range)
    if chapter is None and chapter_based:
        chapter = timeline_written + 1
    node_prompt = current_node_prompt(
        project, timeline_written, provider, db=db,
        chapter=chapter, chapter_based=chapter_based,
    )
    evidence_up_to = chapter if chapter_based else None
    if chapter is not None:
        _backfill_character_cards(db, project, up_to_chapter=evidence_up_to)
    _merge_gender_into_reminders(ctx["reminders"], project.plot_bible)
    appeared_names = (
        _appeared_character_names(db, project, ctx["reminders"], up_to_chapter=evidence_up_to)
        if chapter is not None else None
    )
    chapter_outline = chapter_outline_entry(project.outline, chapter)
    chapter_context = "\n".join(x for x in (chapter_outline, node_prompt) if x)
    if chapter is not None:
        all_chars, _ = merged_constraint_items(ctx["reminders"], project.plot_bible)
        eligible_chars, _ = filter_available_characters(
            all_chars,
            chapter=chapter,
            chapter_context=chapter_context or None,
            appeared_names=appeared_names,
        )
        ctx["constraints"]["characters"] = [
            c.get("name") for c in eligible_chars if c.get("name")
        ]
    era_block, era_prompt_meta, power_block, power_meta = _guard_blocks(project)
    # v12：闸门带（含对白配比参考线）必须在拼提示**之前**写入当前 context——
    # 提示里的极短纯对白段上限与对白参考线都读它；并段仍在其后，硬上限不变。
    gate_limits_meta = _apply_gate_limits(db, project, seg_no=written + 1)
    # 初稿提示的字数下限与补完触发线同源（见 `_draft_prompt_min_chars`）；
    # 节奏口径缓存命中时零成本，提前到这里只为拼提示时能取到同一个值。
    rhythm = _archive_rhythm(db, project)
    prompt_min_chars = _draft_prompt_min_chars(max_chars, rhythm)
    system = build_system_prompt(
        project.title, ctx["reminders"], _style_info(project), project.plot_bible,
        _style_profile(db, project), _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
        era_constraint=era_block,
        power_constraint=power_block,
        style_source_ids=_style_source_ids(project),
        is_opening=is_opening,
        narrative_mode="original",
        catalog_skeleton=_catalog_skeleton(db, project),
        max_chars=max_chars,
        chapter=chapter,
        chapter_context=chapter_context or None,
        appeared_names=appeared_names,
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
        item_holding_block=ctx.get("item_holding_block") or None,
        chapter_outline=chapter_outline,
        min_chars=prompt_min_chars,
        advance_block=_advance_block(db, project, chapter),
        story_cards_block=_render_story_cards(db, project.id),
        foreshadow_block=ctx.get("foreshadow_block"),
    )

    # —— 写后自查（与同步版同口径：缺口驱动，只查不过的项）——
    from app.generation.author_research import (
        backfill_adoption,
        public_summary,
        run_author_research,
    )

    research: dict = {
        "enabled": False, "status": "disabled", "calls": [], "notes_block": "",
    }

    # v2.6：节点粒度根治——流式版同步传 per-request stop token
    # v16：工具可见时，先用非流式素材轮取素材，末轮成文仍走 stream_chat 逐块下发。
    brief_tool, brief_meta = _start_brief_tool(db, project, written + 1)
    # 素材轮非流式（要结构化 tool_calls）；末轮成文仍走 stream_chat 逐块下发。
    brief_blocks = _gather_brief_materials(
        provider, brief_tool, system, user, constraints=ctx["constraints"])
    draft_user = user + _material_suffix(brief_blocks)
    pieces: list[str] = []
    reasoning_chars = 0
    yield {"type": "stage", "stage": "model", "label": "等待模型首字"}
    for kind, piece in _stream_events_with_retry(
        provider, system, draft_user, constraints=ctx["constraints"],
        stop_sequences=get_section_stop_sequences(),
    ):
        if kind == "reasoning":
            if reasoning_chars == 0:
                yield {"type": "stage", "stage": "thinking", "label": "模型思考中"}
            reasoning_chars += len(piece)
            yield {"type": "reasoning", "chars": reasoning_chars}
            continue
        if not pieces:
            yield {"type": "stage", "stage": "writing", "label": "流式撰写中"}
        pieces.append(piece)
        yield {"type": "chunk", "text": piece}
    yield {"type": "stage", "stage": "post", "label": "重算摘要与图谱"}
    # 初稿流刚结束就取：后续补完/重写走非流式 chat，不会覆盖这份留痕。
    # 取不到（非 OpenAI 兼容 provider / 测试桩）就是 None → 落库写 null。
    draft_stream_stats = getattr(provider, "last_stream_stats", None)
    text = _strip_thinking("".join(pieces))
    # v15：与同步版同口径。流式路径的重生成改用**非流式**调用（复用同一 prompt），
    # 否则前端会看到两份正文；终稿仍然只有一份（落库以重生成稿为准）。
    draft_finish_reason = getattr(provider, "last_finish_reason", None)
    text, truncation_info = _regenerate_if_truncated(
        provider, text, draft_finish_reason,
        lambda: _chat_with_retry(
            provider, system, draft_user, constraints=ctx["constraints"],
            stop_sequences=get_section_stop_sequences(),
        ),
    )
    draft_ends_complete = bool(
        analyze_repetition(text).metrics.ends_complete if text.strip() else False
    )
    # 与同步版同口径：先补篇幅缺口，再确定性合并极短纯对白段，最后进闸门。
    # 两个门槛都取参考档案分位（_archive_rhythm），换书自动适配；rhythm 已在拼提示前取过。
    # 并段上限用参考语料章级「段均句数」p90（闸门带已在拼提示前写入）。
    merge_cap = _merge_spc_cap(gate_limits_meta)
    text, completion_info = _complete_short_draft(
        provider, text, max_chars,
        min_ratio=rhythm.get("completion_min_ratio"),
        stop_ratio=rhythm.get("completion_stop_ratio"),
        constraints=ctx["constraints"],
    )
    text, merge_info = merge_ultra_short_dialogue(
        text, names=list(appeared_names or []),
        max_hanzi=rhythm.get("ultra_dialogue_max"),
        spc_cap=merge_cap,
    )
    summary = provider.summarize(text)

    # AI 味检测 + 针对性重写（补救措施，与 system prompt 的预防互补）
    narrative = _style_narrative(db, project)
    forbidden = (narrative or {}).get("forbidden") or {}
    rewrite_info = None
    # 与同步版同口径：同一函数、同一阈值、同样最多两轮。
    # 判罚带同样按参考语料章级分位现推。
    gate = generation_gate(text, max_chars)

    # 缺口驱动自查：默认关闭（settings.author_research_enabled），关闭时不产生工具调用。
    if settings.author_research_enabled and not gate.get("passed"):
        research = run_author_research(
            db, project, provider, ctx,
            segment_no=written + 1,
            gate=gate,
            max_chars=max_chars,
            node_prompt=node_prompt,
            skeleton=_catalog_skeleton(db, project),
        )

    text, summary, rewrite_info, rewrite_rounds, length_path = _rewrite_rounds(
        db, project, ctx, provider, text, summary, forbidden, gate,
        research_notes=research.get("notes_block") or None,
        max_chars=max_chars,
        chapter_range=chapter_range,
    )

    # 与同步版同口径：重写后再跑一次确定性并段（重写会新造极短纯对白段）。
    text, merge_after = merge_ultra_short_dialogue(
        text, names=list(appeared_names or []), spc_cap=merge_cap
    )

    # 批 6：文风形态校正链，与同步版同口径（放在并段之后、时代自检之前）。
    text, style_fix = _apply_style_corrections(
        db, project, provider, text, novel_ids=novel_ids)
    if any((style_fix.get(k) or {}).get("applied") for k in (
            "rewrite_info", "form_info", "attr_info", "ell_info", "beat_info")):
        summary = provider.summarize(text)

    # 时代越界自检：与同步版同口径；默认 report，不改正文。
    era_before = text
    text, era_meta = _apply_era_guard(
        text, project, provider, written + 1, ctx["reminders"]
    )
    if text != era_before:
        summary = provider.summarize(text)

    # 复读闸门（P2 · I2）：与同步版同口径，落库前算好、随正文同一次 INSERT 落。
    quality, repetition, warnings = _quality_check(
        text, budget=project.generation_length)
    # 落库前对**最终正文**再量一次闸门（重写后可能已修好，也可能仍不达标）。
    # 这是观测与告警口径，不改正文；是否重写在上面已经决定过了。
    gate_final = generation_gate(text, max_chars)
    quality["style_gate"] = gate_final
    # 批 6：校正链逐道留痕，与同步版同口径。
    quality["style_corrections"] = style_fix
    # 与同步版同口径：结构检测留痕（是否阻断由 `structure_guard_mode` 决定）。
    quality["structure_check"] = _run_structure_check(project, text, chapter, ctx)
    # 与同步版同口径：逐章推进台账（只判不拦，`mode=report`）。
    quality["progress_ledger"] = _run_progress_ledger(
        db, project, text, chapter, quality.get("structure_check"))
    # 与同步版同口径：短句占比偏低只判不拦（章级口径）。
    quality["short_sentence_check"] = _short_sentence_check(
        gate_final.get("metrics") or {}, gate_limits_meta)
    # 与同步版同口径：对白配比 + 时代越界扫描，均只判不拦。
    quality["dialogue_ratio_check"] = _dialogue_ratio_check(
        project, gate_final.get("metrics") or {}, gate_limits_meta)
    # v19：句长形状（中段拥挤度 / 上尾占比，只判不拦），与同步版同口径。
    quality["sentence_shape_check"] = _sentence_shape_check(
        text, gate_final.get("metrics") or {}, gate_limits_meta)
    quality["era_check"] = _era_check_quality(
        era_meta, era_prompt_meta, _era_channel_abuse(db, project, era_meta))
    # 与同步版同口径：长度路径留痕 + 终稿字数/是否仍超限按最终正文重算
    # （字数 = `char_count`，字符含标点，与下限同一个数）。
    _final_chars = char_count(text)
    quality["length_path"] = {
        "path": length_path["path"],
        "limit": length_path["limit"],
        "final_chars": _final_chars,
        "final_hanzi": gate_final.get("hanzi"),
        "still_over_limit": _final_chars > length_path["limit"],
        "compression_accepted": length_path["compression_accepted"],
    }
    # 与同步版同口径：外部素材工具留痕（键名沿用 `chapter_stats()`）。
    # 采纳统计用**最终正文**，与同步版落在同一个调用点。
    if brief_tool is not None:
        brief_tool.note_chapter_text(text)
    quality["brief_tool"] = _brief_tool_quality(brief_tool, brief_meta)
    # 与同步版同口径：截断留痕 + 截断稿不得冒充合格章。
    final_ends_complete = quality.get("truncated") is False
    truncated_source = bool(
        (truncation_info.get("draft_truncated_by_tokens")
         or not draft_ends_complete)
        and not truncation_info.get("accepted_regenerated")
    )
    quality["truncation"] = {
        **truncation_info,
        "draft_ends_complete": draft_ends_complete,
        "final_ends_complete": final_ends_complete,
        "final_from_truncated_draft": truncated_source,
        "final_from_truncated_draft_via_compression": bool(
            truncated_source and length_path.get("compression_accepted")),
        "degrade_note": (
            "终稿来自截断稿：初稿被 max_tokens 截断，压缩轮在断头稿上收口"
            if truncated_source and length_path.get("compression_accepted")
            else ("终稿正文本身未收束" if not final_ends_complete else None)
        ),
    }
    quality["passed"] = bool(
        gate_final.get("passed") and final_ends_complete and not truncated_source
    )
    if truncated_source:
        warnings.append("初稿被截断，终稿来自截断稿，本章按不合格章记录")
    # 提示版本留痕：这一章是在哪套提示下生成的（与闸门 v11 口径对齐）。
    quality["prompt_revision"] = PROMPT_REVISION
    # 单章只判结构合规；指纹 13 项单章只记录，卷末再与参考卷均值对照。
    quality["gate_limits"] = {
        k: gate_limits_meta.get(k)
        for k in ("source", "chapters", "volumes", "volume_chapters",
                  "volume_chapters_source", "reference_novels", "chapter_limits",
                  "volume_reference", "ultra_short_limit", "dialogue_reference",
                  "sentence_reference", "rhythm_reference",
                  "dialogue_mix_reference",
                  # v20：句子口径与形状带随章落库，否则跨章无法按 scope 分组比较
                  # （第 35 章之前这四项在库里全是 null，只有内存里有值）。
                  # `sentence_scope_from_chapter` 仅记录、无判罚效力（无任何分支读取）。
                  "sentence_scope", "sentence_scope_from_chapter",
                  "short_sentence_max_len", "sentence_shape",
                  # v22：段数下限的折算结果与来源（同步版同款）。
                  "段数",
                  "fallback_used", "fallback_reason")
    }
    _attach_volume_gate(db, project, quality, written + 1, chapter or written + 1,
                        gate_final["metrics"], gate_limits_meta)
    # 软档观测（与同步版同口径）：只记录，不影响 passed、不触发重写。
    quality["soft_deviations"] = gate_final.get("soft_deviations") or []
    # C（2026-11-13）：与同步版同口径，重写轮次随正文落库，失败轮也要留痕。
    quality["rewrite_rounds"] = rewrite_rounds
    # 流式初稿的 reasoning/正文增量留痕：首字等待到底是供应商排队、思考还是
    # 网络抖动。拿不到（不回 reasoning 字段）就是 null，不会被读成「没思考」。
    quality["draft_stream"] = _draft_stream_quality(draft_stream_stats)
    # 与同步版同口径：补写/并段留痕 + 重写失败时显式告警，不再静默降级。
    if completion_info.get("triggered"):
        quality["draft_completion"] = completion_info
    # 留痕条件含 `skipped_single_neighbor`（为保住单句段峰而主动放弃）与
    # `stopped_by_spc_cap`（段均句数已到参考 p90）：否则事后只看到「并了 0 段」，
    # 分不清是模型没造碎段、还是护栏挡住的。
    if _merge_touched(merge_info):
        quality["dialogue_merge"] = merge_info
    if _merge_touched(merge_after):
        quality["dialogue_merge_after_rewrite"] = merge_after
    if rewrite_info:
        quality["rewrite_info"] = rewrite_info
    if any(r.get("failed") for r in rewrite_rounds):
        warnings.append("重写轮失败，终稿降级为首轮稿（详见 quality.rewrite_rounds）")
    if not gate_final["passed"]:
        warnings.append("文风硬指标未达标：" + "；".join(gate_final["reasons"]))

    # 段末回填采纳判定（与同步版同口径、同一函数）。
    author_research = public_summary(
        research, backfill_adoption(db, research, text)
    )
    quality["author_research"] = author_research

    gen, segment_no = _commit_segment(
        db, project, text, summary,
        meta={"era": {**era_prompt_meta, **era_meta}, "power": power_meta},
        quality=quality,
        chapter_range=chapter_range,
        prompt_json=_prompt_snapshot(system, draft_user),
        authority_json=_authority_snapshot(project, ctx, chapter_range),
    )
    # v2.3：v2.1 集成到流式版——commit 段后调 judge_and_advance（与同步版同口径）
    node_judge = _judge_and_advance(
        db, project, provider, text, written,
        progress_before=timeline_written,
        chapter_based=chapter_based,
    )

    # —— 图谱进入生成闭环（与 generate_next_segment 顺序对齐）——
    # 必须放在 _commit_segment 之后、_rollup_summary_tree 之前：正文已落库才归档，
    # 且摘要滚上失败不该牵连簿记（滚上后置，失败只影响它自己）。
    # 复用 _merge_segment_into_graph（它已委托到 app.kg.build.merge_segment_into_kg
    # 做图谱+账本+别名归一+哨兵四步）——**不另写一套实现**：7416114 才把四步编排
    # 收归唯一入口，重写的结果就是又出现"只更一半"的路径。
    # 保留 try/except：簿记异常绝不打断 SSE（正文已落库，done 必须照样发）。
    kg_stats = None
    if _is_degenerate(quality):
        # 复读退化段跳过 KG（标记而非拒绝）：循环对白不并入 facts。
        kg_stats = {
            "merged": False,
            "skipped": "repetition_degenerate",
            "reason": "本段疑似复读，已跳过 KG 抽取",
        }
    else:
        try:
            kg_stats = _merge_segment_into_graph(db, project.id, text, segment_no)
        except Exception as exc:  # noqa: BLE001 —— 簿记异常绝不打断 SSE：正文已落库，
            # done 事件必须照样发出，否则前端表现为"生成卡住且无结果"。
            logging.getLogger(__name__).warning("流式 KG 簿记失败，段落已落库: %s", exc)
            kg_stats = {"merged": False, "reason": f"KG 簿记失败：{str(exc)[:100]}"}
    power_meta = _attach_power_meta(db, gen, power_meta, kg_stats)

    # v3.0 异步批抽：与同步版同口径入队
    v3_extraction = _enqueue_v3_extraction(
        db, project, segment_no, text, chapter=_chapter_ordinal(chapter_range))
    # KG 分层影子抽取（默认关闭）：与同步版同口径，独立库、不参与注入。
    _shadow_ingest(project, segment_no, text)
    _post_commit_written = _next_segment_no(db, project.id) - 1  # = written + 1
    from app.generation.branch import roll_branch as _roll_branch
    _new_state, branch_info = _roll_branch(
        db, project, _load_timeline_state_for_branch(db, project, written),
        provider, _post_commit_written, last_branch_written=None,
        chapter_based=chapter_based,
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
        "rewrite_rounds": rewrite_rounds,
        "author_research": author_research,
        # 两条护栏的自检摘要（era 默认 report；power 为体系类型 + unknown 计数）。
        "era": {**era_prompt_meta, **era_meta},
        "power": power_meta,
        # 纯新增键：done 的既有字段语义一个都不改。挂簿记统计是为了让
        # 流式路径也能被观测（此前流式完全不做归档，出了问题无从判断）。
        "kg": kg_stats,
        # master 侧把流式键统一为 kg_update（与同步路径同键）。两个都发：
        # 老前端读 kg、新消费方读 kg_update，谁都不会静默拿不到。
        "kg_update": kg_stats,
        "node_judge": node_judge,
        "v3_extraction": v3_extraction,
        "branch_roll": branch_info,
        # 复读闸门（D7）：与 200 响应同形的 verdict / 触发线 / 原因 + 提示。
        "repetition": repetition,
        "length": (quality or {}).get("length"),
        "summary_style": (quality or {}).get("summary_style"),
        "structure_check": (quality or {}).get("structure_check"),
        "warnings": warnings,
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
    instruction: str | None = None,
    rewrite_before_chars: int | None = None,
    chapter: int | None = None,
    chapter_context: str | None = None,
    appeared_names: set[str] | None = None,
    chapter_outline: str | None = None,
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
        max_chars = project.generation_length or 4000
    # 与正常续写同源：提示里的字数下限取补完触发线（重写分支不使用它）。
    prompt_min_chars = _draft_prompt_min_chars(max_chars, _archive_rhythm(db, project))

    if reminders is None:
        reminders = get_reminders(db, project.id, source="generated")

    # 与续写同口径：台账存在即接管旧 foreshadowing（重生成也不该漏掉未回收线）。
    foreshadow_block, ledger_lines = _foreshadow_ledger_block(db, project.id)
    if ledger_lines is not None:
        reminders["foreshadow_ledger"] = ledger_lines

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
    # 重生成同样按「有没有前情」判定首段；段 1 重写/重生成走开篇口径。
    is_opening = not blocks and not l1_chain(tree)
    era_block, _, power_block, _ = _guard_blocks(project)
    system = build_system_prompt(
        project.title, reminders,
        _style_info(project), project.plot_bible, _style_profile(db, project),
        _style_narrative(db, project),
        style_suffix=overrides[PromptKey.STYLE],
        constraint_note=overrides[PromptKey.CONSTRAINT],
        era_constraint=era_block,
        power_constraint=power_block,
        style_source_ids=_style_source_ids(project),
        is_opening=is_opening,
        # 重写/重生成 = 续写：文风 + 人物 + 时间线并重；KG 时间线必填。
        narrative_mode="continuation",
        narrative_timeline=_narrative_timeline(db, project),
        max_chars=max_chars,
        chapter=chapter,
        chapter_context=chapter_context,
        appeared_names=appeared_names,
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
        item_holding_block=_item_holding_text(
            db, project,
            before_segment if before_segment is not None
            else (_next_segment_no(db, project.id) - 1),
        ) or None,
        instruction_suffix=instruction,
        # 重写：用原文长度替换续写预算（80%–120%），避免「续写 ≥max_chars*0.8」
        # 与「重写要贴近原文篇幅」两条预算互相打架。
        rewrite_before_chars=rewrite_before_chars,
        chapter_outline=chapter_outline,
        min_chars=prompt_min_chars,
        story_cards_block=_render_story_cards(db, project.id),
        foreshadow_block=foreshadow_block,
    )
    return system, user, max_chars


def regenerate_segment_stream(
    db,
    project: Project,
    segment_no: int,
    novel_ids: list[int] | None = None,
    provider=None,
    max_chars: int | None = None,
    instruction: str | None = None,
    snapshot_id: int | None = None,
    expected_before: str | None = None,
    selected_scope: dict | None = None,
    chapter_label: str | None = None,
):
    """重生成单段（流式）：删除目标段旧内容、依前文摘要链重生成插回、重建摘要链。

    语义：只替换指定 segment_no 的 text/summary，不改动其他段的 text；
    后续段的「上下文」严格说应基于新摘要，但为不引发级联重写，仅重建摘要树
    （L1 重排 + L2/L3 整体重算），保证「未来」续写连贯。已落库的后段正文保留。

    `novel_ids` 目前**未被使用**——重生成有意不带 RAG 素材（见
    `_build_generation_prompts` 的 `[]` 实参），保留参数以维持调用方契约。
    `instruction`（2026-11-11）：对话助手重写卡带的用户要求。只注入
    user prompt 的【本次重写要求】指令区（不进待改正文/物品块/constraints）；
    为空时提示词与旧版逐字节相同。done 事件用 `instruction_injected` 回报
    是否真的出现在发出的 prompt 里，供 API/测试核对。
    `snapshot_id` / `expected_before`（2026-11-11 快照）：助手重写走两段式写入——
    调用方在调模型前已写好 `pending` 快照，这里用 CAS（`WHERE text = expected_before`）
    写正文并与快照 `applied` 同事务提交。两者都为 None 时走原有赋值路径，
    `/regenerate-stream` 的行为逐字节不变。
    `selected_scope`（2026-11-12 澄清）：用户在候选清单里选定的区间。含糊指令
    必须由路由层先澄清；这里拿到 `selected_scope` 就直接走 Tier A 的改写提示词，
    不再让模型定位。未给 `selected_scope` 且 `resolve_scope` 为 None 时抛
    `clarification_required` 失败关闭（安全网）。
    v2.3：传 `node_prompt` 给 `_build_generation_prompts`——重生成用时间线模式
    剔除全故事信息。**不**调 `_judge_and_advance`——重生成替换第 N 段语义
    上不改 `_next_segment_no`（已存在），不应触发 state advance。
    `chapter_label`（2026-11-15）：调用方已解析好的章标签，只**改标签**不改段号。
    为 None 时保留目标段原标签（原本是段标签则维持 `段N`，不擅自换章号）；
    非 None 时用它覆盖，并据此选节点/算证据窗口。
    """
    from app.llm.provider import get_llm_provider

    yield {"type": "stage", "stage": "prepare", "label": "装配上下文"}

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
    # 改前正文（B 的范围校验基线）。在旧路径里 target.text 随后会被直接赋值，
    # 快照路径则由 SQL UPDATE 改库，故必须在这里先留一份内存副本。
    before_text = target.text or ""

    # 提醒包**只加载一次**，同一个对象同时喂 system 提示与尾部 constraints：
    # 除了省一次查询，更是防止两次查询之间发生并发写入，导致「system 列了一套
    # 锁定名单、尾部 constraints 列了另一套」的自相矛盾提示词。
    reminders = get_reminders(db, project.id, source="generated")
    # 修复项 2：重生成按目标段的**章号**选节点；重写同一章不推进时间线。
    # 章标签：入参优先，其次目标段原标签——重写不改章号身份，避免同一段被
    # 悄悄推到未来的章号上。显式传入的标签必须先过唯一生产者校验；保留的
    # 历史标签（可能是 `段N`）不回溯改写。
    if chapter_label is not None:
        _require_chapter_label(
            chapter_label, segment_no=segment_no, where="regenerate")
    label = chapter_label or target.chapter_range
    timeline_written, chapter_based = _timeline_progress(db, project)
    chapter = _chapter_ordinal(label)
    if chapter is None and chapter_based:
        chapter = timeline_written
    node_prompt = current_node_prompt(
        project, timeline_written, provider, db=db,
        chapter=chapter, chapter_based=chapter_based,
    )
    evidence_up_to = chapter if chapter_based else None
    if chapter is not None:
        _backfill_character_cards(db, project, up_to_chapter=evidence_up_to)
    _merge_gender_into_reminders(reminders, project.plot_bible)
    appeared_names = (
        _appeared_character_names(db, project, reminders, up_to_chapter=evidence_up_to)
        if chapter is not None else None
    )
    chapter_outline = chapter_outline_entry(project.outline, chapter)
    chapter_context = "\n".join(x for x in (chapter_outline, node_prompt) if x)
    if chapter is not None:
        all_chars, _ = merged_constraint_items(reminders, project.plot_bible)
        eligible_chars, _ = filter_available_characters(
            all_chars,
            chapter=chapter,
            chapter_context=chapter_context or None,
            appeared_names=appeared_names,
        )
        reminders.setdefault("character_constraints", [])
        allowed = {c.get("name") for c in eligible_chars if c.get("name")}
        reminders["character_constraints"] = [
            c for c in reminders["character_constraints"]
            if not c.get("name") or c.get("name") in allowed
        ]

    # 前情 = 只取该段之前的树（截断时若 L3 混入了被替换内容，会自动丢弃并重建）
    system, user, max_chars = _build_generation_prompts(
        db, project, before_segment=segment_no, novel_ids=novel_ids,
        max_chars=max_chars, provider=provider, reminders=reminders,
        node_prompt=node_prompt, instruction=instruction,
        # 原文长度已知（before_text 在上面捕获）：重写提示词据此给出
        # 80%–120% 的篇幅区间，而不是续写的「不少于 max_chars*0.8」。
        rewrite_before_chars=len(before_text) or None,
        chapter=chapter,
        chapter_context=chapter_context or None,
        appeared_names=appeared_names,
        chapter_outline=chapter_outline,
    )
    # 指令是否**真的**出现在发出的 user prompt 里（只看渲染后的文本，不看入参）。
    # 空/纯空白指令一律 False，与改动前「不注入」行为一致。
    _instruction = (instruction or "").strip()
    instruction_injected = bool(_instruction) and _instruction in user
    # constraints 与正常续写同源（角色/伏笔的锁定名单不能因重生成而变）。
    # 只组装这一个 dict，**不跑 build_context**：那条路会为 RAG 与风格样例各加载
    # 一遍全部 NovelChunk，而重生成的 user prompt 不带 RAG 素材，结果全部丢弃。
    constraints = _constraints_from(reminders, project)
    # 重生成第 1 段 = 首段（此前没有任何已写段落）：与正常生成同口径，
    # 伏笔不进尾部近因提醒，否则 system 里刚藏起来的清单会在 user 末尾漏出。
    if segment_no == 1:
        constraints["plotholes"] = []

    pieces: list[str] = []
    # —— 在途租约心跳（仅助手重写路径）——
    # 每 20 秒最多写一次 ``heartbeat_at``（snapshot.SnapshotLease 在内存里
    # 节流）；``snapshot_id is None`` 的 /regenerate-stream 完全不产生额外写。
    # 心跳与生成/抽取抢写锁是真实成本，所以不做「每 chunk 一次无条件 UPDATE」。
    lease = None
    if snapshot_id is not None:
        from app.agents.dialog.snapshot import SnapshotLease

        lease = SnapshotLease(snapshot_id)
        lease.touch(db)  # 开流前刷新一次（begin 之后可能已过一段时间）

    # —— 意图分流：局部指令走句子索引补丁（2026-11-12）——
    # 判定复用 scope_guard.declared_local，不另造关键词表（两套口径必漂）。
    # Tier A：位置词（段尾/最后一句/段首/第 N 句…）由程序解析成编号，
    # 改写提示词只给目标句原文、不给任何编号，模型只写替换文本。
    # 含糊指令（某处/此处/这一句）必须由路由层先澄清候选区间，再把用户选定
    # 的 selected_scope 传进来；没有选定区间就进入改写属于调用错误，失败关闭。
    # 两条路都只把替换文本拼回原文，其余文字逐字不动，压缩没有发生的空间。
    local_patch = bool(_instruction) and declared_local(_instruction)
    patch_meta = None
    if local_patch:
        from app.generation.patch import (
            PatchError,
            build_patch_user_prompt,
            parse_patch_reply,
            resolve_scope,
        )

        def _chat(user_prompt: str) -> str:
            try:
                if lease is not None:
                    lease.touch(db)
                return provider.chat(system, user_prompt, constraints=constraints)
            except BaseException as exc:  # noqa: BLE001 —— 含客户端断开
                if snapshot_id is not None:
                    from app.agents.dialog.snapshot import fail_snapshot

                    reason = ("client_disconnect" if isinstance(exc, GeneratorExit)
                              else type(exc).__name__)
                    fail_snapshot(db, snapshot_id, reason)
                raise

        def _fail_patch(exc: PatchError, reply_text: str, stage: str) -> None:
            # 留痕：快照 error_reason 只存 code；模型原始返回截一段进日志，
            # 否则事后无法解释「为什么这句补丁失败了」。
            logging.getLogger("generation.engine").warning(
                "局部补丁失败（%s）code=%s；模型返回前 400 字：%r",
                stage, exc.code, (reply_text or "")[:400],
            )
            if snapshot_id is not None:
                from app.agents.dialog.snapshot import fail_snapshot

                fail_snapshot(db, snapshot_id, exc.code)

        # Tier A：范围词（段尾/最后一句/段首/第 N 句…）先由程序解析成编号。
        # 用户已从候选清单选定区间时直接使用，不再解析指令、也不再问模型。
        if selected_scope is not None:
            scope = selected_scope
        else:
            try:
                scope = resolve_scope(_instruction, before_text)
            except PatchError as exc:
                logging.getLogger("generation.engine").warning(
                    "局部补丁范围解析失败 code=%s；指令：%r", exc.code, _instruction
                )
                if snapshot_id is not None:
                    from app.agents.dialog.snapshot import fail_snapshot

                    fail_snapshot(db, snapshot_id, exc.code)
                raise

        if scope is None:
            # 安全网：含糊指令必须先澄清。路由层会在 begin_snapshot 之前拦截，
            # 走到这里说明调用方漏了澄清步骤，不能退回「让模型自己选位置」。
            exc = PatchError(
                "clarification_required",
                "含糊指令必须先澄清候选区间，不能直接改写。",
            )
            _fail_patch(exc, "", "定位")
            raise exc

        # Tier A：目标句由程序按编号取出，只把原句放进分隔块；模型只写替换文本。
        patch_user = build_patch_user_prompt(before_text, _instruction, scope)
        # 指令确实进了本次发出的 prompt（补丁 prompt 里也有【本次要求】区）。
        instruction_injected = _instruction in patch_user
        reply = _chat(patch_user)
        try:
            text, patch_meta = parse_patch_reply(reply, before_text, scope)
        except PatchError as exc:
            # 直接失败，**不回退整段重写**：回退会重新打开压缩的口子，而且
            # 「成功的整段重写」与「局部改写」在正文上不可区分。失败保证
            # 「局部指令要么精确补丁，要么正文一行不动」。
            _fail_patch(exc, reply, "写替换")
            raise
        # 验收留痕：本次实际发出的改写提示词与模型原始返回前 300 字。
        # 只进 done 响应，不落快照；用于确认「提示词本来就没有编号」以及
        # 模型是否仍然写了编号（numbering_stripped 为 True 即触发兜底剥离）。
        patch_meta["rewrite_prompt"] = patch_user
        patch_meta["raw_reply_preview"] = (reply or "")[:300]
        # 补丁完成后一次性把完整正文发给客户端：UI 拿到的仍是「整段新正文」，
        # 但模型从未收到「重写整段」的请求。
        yield {"type": "chunk", "text": text}
    else:
        try:
            reasoning_chars = 0
            yield {"type": "stage", "stage": "model", "label": "等待模型首字"}
            fn = getattr(provider, "stream_chat_events", None)
            source = (
                fn(system, user, constraints=constraints)
                if callable(fn)
                else (("content", p) for p in provider.stream_chat(system, user, constraints=constraints))
            )
            for kind, piece in source:
                if kind == "reasoning":
                    if reasoning_chars == 0:
                        yield {"type": "stage", "stage": "thinking", "label": "模型思考中"}
                    reasoning_chars += len(piece)
                    yield {"type": "reasoning", "chars": reasoning_chars}
                    continue
                if not pieces:
                    yield {"type": "stage", "stage": "writing", "label": "流式撰写中"}
                if lease is not None:
                    lease.touch(db)
                pieces.append(piece)
                yield {"type": "chunk", "text": piece}
            yield {"type": "stage", "stage": "post", "label": "重算摘要与图谱"}
        except BaseException as exc:  # noqa: BLE001 —— 含客户端断开（GeneratorExit）
            # 快照留痕：模型报错/超时/客户端中断都置 failed，正文一行没动。
            # 不做这两行的话，残留的 pending 会变成一条没有心跳的租约，该段要等
            # 心跳判据超时后由用户显式接管才能重写。
            if snapshot_id is not None:
                from app.agents.dialog.snapshot import fail_snapshot

                reason = ("client_disconnect" if isinstance(exc, GeneratorExit)
                          else type(exc).__name__)
                fail_snapshot(db, snapshot_id, reason)
            raise

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
    # 重生成是用户显式改稿，不自动再重写；但闸门结果必须随告警一起返回，
    # 否则用户看到的“AI 腔告警”会漏掉句长/段长/问号这三类硬指标。
    _regen_gate = generation_gate(text, max_chars)
    if not _regen_gate["passed"]:
        _need_rewrite = True
        _reasons = _reasons + _regen_gate["reasons"]
    if _need_rewrite:
        rewrite_info = {
            "alert": True,
            "reasons": _reasons,
            "style_gate": _regen_gate,
            "original_chars": len(text),
            "rewritten_chars": None,
        }

    # 时代自检在重生成路径同样生效：默认 report，只在显式 rewrite 时改 text。
    _, era_prompt_meta, _, power_meta = _guard_blocks(project)
    era_before = text
    text, era_meta = _apply_era_guard(
        text, project, provider, segment_no, reminders
    )
    if text != era_before:
        summary = provider.summarize(text)

    # 复读闸门（P3 · I3）：落库前算好，两条子路径都必须带同一份报告。
    # 重写路径不合并 KG，所以这里没有「跳过 KG」的动作（详见方案稿 §3.2）。
    # 篇幅判据：这是改写既有段落，跌幅归 detect_length_drop，本字段
    # 返回 not_applicable，不报「篇幅不足」。
    quality, repetition, warnings = _quality_check(text, is_rewrite=True)
    quality["style_gate"] = generation_gate(text, max_chars)

    new_summary = {
        "text": summary,
        "era": {**era_prompt_meta, **era_meta},
        "power": power_meta,
    }

    # 局部指令 vs 整段重写：只留痕（响应 / done / 快照行），不阻断、不重试。
    # 用户可能真的想整段重写，所以这里绝不据此拒绝落库。
    scope_mismatch = detect_scope_mismatch(instruction, before_text, text)
    # 篇幅跌幅单独一条（不与 scope_mismatch 混账）：首尾保留情况与「整段被压短」
    # 是两类不同的模型不服从。用户明确要求精简/压缩时本函数返回 None。
    length_drop = detect_length_drop(instruction, before_text, text)

    if snapshot_id is None:
        # —— 旧路径（/regenerate-stream、测试）：逐字节不变 ——
        target.text = text
        target.summary = new_summary
        target.chapter_range = label or f"段{segment_no}"
        # 章身份旁路：只有 `第N章` 标签才写（段标签返回 None），与 _commit_segment 同口径。
        regen_meta = _chapter_commit_meta(project, target.chapter_range)
        if regen_meta is not None:
            quality = {**quality, "chapter_meta": regen_meta}
        # 与正文同一次 commit：正文与标记不可能分叉。
        target.quality_json = _quality_json(quality)

        # —— 重生成的 KG 簿记（清空 + 全量 replay）——
        # ⚠️ 顺序陷阱：rebuild_generated_kg 从 Generation 表读各段正文，新文本必须先
        # 落库，否则 replay 读到的是**旧**文本 —— 等于没修。故此 commit 必须在 replay 前。
        db.commit()
    else:
        # —— 两段式写入（助手重写）：正文 CAS + 快照 applied 同一事务 ——
        from app.agents.dialog.snapshot import (
            ConcurrentEdit,
            fail_snapshot,
            finalize_snapshot,
        )

        try:
            finalize_snapshot(
                db,
                snapshot_id=snapshot_id,
                generation_id=target.id,
                segment_no=segment_no,
                expected_before=expected_before,
                new_text=text,
                new_summary=new_summary,
                # 与 applied 同一事务落快照行（只留痕，不参与任何拦截）。
                scope_mismatch=scope_mismatch,
                length_drop=length_drop,
                # 复读标记进同一条 SQL UPDATE（防「正文换了、标记没跟上」）。
                quality_json=_quality_json(quality),
            )
            # 与上面旧路径同一个提交点：正文与快照一起落，中间崩了都不会出现
            # 「正文变了但快照还是 pending」。
            db.commit()
            # ⚠️ 必须立刻刷新：finalize_snapshot 是 SQL 级 UPDATE
            # （synchronize_session=False），而 SessionLocal 是
            # expire_on_commit=False——commit 后 identity map 里的 target 仍是
            # 旧正文。下面的 rebuild_generated_kg / rebuild_summary_tree 都用
            # select(Generation) 读段，不刷新就会把**旧正文/旧摘要**重新灌回
            # KG 与摘要树（实测复现：replay 后 facts 仍只剩旧正文独有句）。
            db.refresh(target)
        except ConcurrentEdit:
            db.rollback()
            fail_snapshot(db, snapshot_id, "concurrent_edit")
            raise
        except BaseException as exc:  # noqa: BLE001 —— 含中断
            db.rollback()
            fail_snapshot(db, snapshot_id, type(exc).__name__)
            raise

    kg_stats = None
    try:
        from app.kg.build import rebuild_generated_kg

        kg_stats = rebuild_generated_kg(db, project.id)
    except Exception as exc:  # noqa: BLE001 —— 簿记失败绝不打断 SSE：正文已落库，
        # done 事件必须照样发出，否则前端表现为"重生成卡住且无结果"。
        logging.getLogger(__name__).warning("重生成 KG replay 失败，正文已落库: %s", exc)
        kg_stats = {"rebuilt": False, "reason": f"{str(exc)[:100]}"}

    # —— 重写后的 v3 重新批抽（按章版本化，不删旧行）——
    # 旧路径只做 v2.6 replay，v3 表（事件/关系/状态/提及）永远停留在旧稿，
    # 于是「正文已改、KG 仍是旧事实」——这正是改写后新旧两版事实能同时进
    # prompt 的根因。这里以 rebuild=True 重新入队本段：
    #   · 批抽落库时把同章旧版本行置 is_current=0（保留可回溯），新行为当前；
    #   · 章号仍由章节标签派生，派生不到才回退段号（与新建同口径）。
    # 失败不阻断重写主链——正文与 v2.6 已落库，v3 靠后续入队/周期扫描补齐。
    _enqueue_v3_extraction(
        db, project, segment_no, text,
        chapter=_chapter_ordinal(target.chapter_range), rebuild=True,
    )

    # 重建整棵摘要树：该段所在的卷摘要与主线骨架都基于旧内容，不能续滚
    tree_stats = rebuild_summary_tree(db, project, provider)
    db.refresh(target)

    # 文风一致性校验 · L1 影子记录（重写路径）：与 _commit_segment 同口径。
    # 必须在 refresh 之后调用，否则采到的是 identity map 里的旧正文。
    # record_segment_check 内部吞异常，这里再包一层防 import 失败——校验器
    # 任何故障都不得影响已经成功的重写。
    try:
        from app.style.consistency import record_segment_check

        record_segment_check(db, project, target)
    except Exception:  # noqa: BLE001 —— 影子采样失败不影响重写
        logging.getLogger(__name__).warning(
            "style shadow check skipped", exc_info=True
        )

    yield {
        "type": "done",
        "segment_no": segment_no,
        "chars": len(text),
        "summary": summary,
        "summary_tree": tree_stats,
        "rewrite_info": rewrite_info,
        # 本次重写要求是否真的进了发出的 prompt（供 API/测试事后核对；
        # 空指令恒为 False）。正文已落库，此标记只描述提示词，不改文本。
        "instruction_injected": instruction_injected,
        # 局部指令 vs 整段重写：仅提示（含前后缀长度/字数变化），不阻断。
        "scope_mismatch": scope_mismatch,
        # 篇幅跌幅 >20% 的单独提示（用户要求精简时不出现），同样不阻断。
        "length_drop": length_drop,
        # 句子级补丁的落地明细（仅局部指令路径）：锚点/替换长度、未涉及部分
        # 由构造保证逐字不变。整段重写路径为 None。
        "patch": patch_meta,
        "era": {**era_prompt_meta, **era_meta},
        "power": power_meta,
        "id": target.id,
        # 快照 id（助手重写才有）：撤销入口据此定位「上一次」。
        "snapshot_id": snapshot_id,
        # 与流式续写路径（generate_next_segment_stream）的 done 键对齐，
        # 让重生成也带上簿记统计，可被前端/API 观测。
        "kg": kg_stats,
        # 复读闸门（D7）：verdict / triggers / reason_text + 提示。
        "repetition": repetition,
        "length": (quality or {}).get("length"),
        "summary_style": (quality or {}).get("summary_style"),
        "warnings": warnings,
    }


def generate_loop(
    db,
    project: Project,
    novel_ids: list[int] | None = None,
    provider=None,
    segments: int = 1,
    max_chars: int | None = None,
    chapter_labels: list[str] | None = None,
) -> list[dict]:
    """连续续写（默认单段）。需批量时才传 segments>1；逐段落库，后段读前段总结作前情。

    `chapter_labels`：调用方（路由）已解析好的逐段章标签，长度应与 segments 一致。
    这里**不推导章号**（推导只在 `app.generation.chapter_labels` 一处），传 None 即段模式。

    注意：segments 过大会在单次请求里反复调用 LLM，前情摘要链虽短，但仍有会话压缩风险；
    生产建议「用户逐次点生成」而非一次性 loop 多段。
    """
    labels = list(chapter_labels or [])
    if labels and len(labels) != segments:
        raise ValueError(
            f"chapter_labels 长度 {len(labels)} 与 segments {segments} 不一致")
    results: list[dict] = []
    for idx in range(segments):
        results.append(generate_next_segment(
            db, project, novel_ids, provider, max_chars,
            chapter_range=(labels[idx] if labels else None),
        ))
    project.status = "loop_done" if project.status == "generating" else project.status
    db.commit()
    return results


# =========================================================================== #
# 文风形态校正链（批 6：从 freeze/main-cb05a45 的 engine 搬入）
# =========================================================================== #
# 这一段是本分支此前完全缺失的「落库前改文本工序」。顺序不可调（main 的
# `_run_text_chain` 有逐条理由）：文风改写 → 形态校正 → 归属校正 → 省略号
# 校正 → beat 插入 → 照搬门禁。本分支的主链在 `generate_next_segment`
# / `..._stream` 里内联，接入点是重写轮与并段之后、质量判定之前
# （见 `_apply_style_corrections`）。
#
# 纪律：只做文本变换 + 只读数据库，不落库（照搬门禁抛 `VerbatimViolation`
# 由调用方决定，不能静默吞掉）。
MIN_GAP_TO_ELLIPSIS = 0.05


REWRITE_GATE_FEATURES = ("句长均值", "句号密度")


def _style_gate(text: str, profile: dict | None) -> dict:
    """段级文风判定的**唯一出口**（仲裁点）。

    ## 返回什么

    ::

        {
          "available": bool,       # 有没有规格书可判
          "items": {key: {...}},   # 逐项落位（value/lo/hi/in_band/deviation）
          "in_band": [key, ...],   # 在带内的项
          "out_of_band": [key, ...],  # **区间型**出带 ⇒ 可回灌
          "warn_only": [key, ...],    # **上限型**出带 ⇒ 只告警
          "deviations": [str, ...],   # 可回灌的偏差句（给改写流程）
        }

    ## 三条判定规则（2026-11-01 拍板）

    1. **区间型出带 ⇒ 回灌**：附上偏差量，让执行方自己改。
    2. **上限型出带 ⇒ 只告警**：比喻/情绪直陈/段尾拔高这类「越少越好」，
       超出不该回灌（回灌等于让模型去凑指标），也不阻断。
    3. **没进提示词的项 ⇒ 只记录不判定**：模型看不见它，回灌没有意义。

    ## 为什么不用 `band_check` 单独判

    `band_check` 是「拿档案的带当尺」，`spec_check` 是「拿规格书当尺」，
    而规格书**由档案导出** ⇒ 同一把尺的两个视图。
    用规格书当尺的好处是：段级判定与提示词里给模型的区间**逐字同源**，
    模型收到的指令与验收它的判据不可能对不上。

    ## 降级

    没有档案 / 档案里没有规格书（老档案）⇒ `available=False`，什么都不判。
    档案是增强，不是生成的前提 —— 判不出来就放行，绝不阻断。
    """
    if not profile or not text or not text.strip():
        return {"available": False, "items": {}, "in_band": [],
                "out_of_band": [], "warn_only": [], "deviations": []}
    try:
        from app.style.spec import spec_check, spec_deviation_lines

        spec = profile.get("spec") or {}
        if not spec or not spec.get("items"):
            return {"available": False, "items": {}, "in_band": [],
                    "out_of_band": [], "warn_only": [], "deviations": []}
        check = spec_check(text, spec)
    except Exception:  # noqa: BLE001 —— 判不出来就不判，绝不能阻断生成
        return {"available": False, "items": {}, "in_band": [],
                "out_of_band": [], "warn_only": [], "deviations": []}

    items = check.get("items") or {}
    in_band = [k for k, v in items.items() if v.get("checked") and v.get("in_band")]
    return {
        "available": True,
        "items": items,
        "in_band": in_band,
        "out_of_band": list(check.get("out_of_band") or []),
        "warn_only": list(check.get("warn_only") or []),
        # 回灌句只给**区间型**出带项（上限型只告警，见规则 2）
        "deviations": spec_deviation_lines(check),
        "recorded": list(check.get("recorded") or []),
    }


def _strip_ws(text: str) -> str:
    """去掉所有空白 —— 用来验证「只改了段落边界（和那一处标点形态）、内容一个字节没动」。"""
    return re.sub(r"\s+", "", text or "")


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

    ## 与仲裁点 `_style_gate` 的关系（2026-11-01）

    两者的**尺是同一把**：这里的 `band_check` 取的 p10/p90，
    正是规格书里那两项的 `lo`/`hi`（规格书由档案导出 ⇒ 同源）。

    但**分工不同**，不能合并：
    - `_style_gate` 判的是「这一段整体像不像（逐项 in/out）」⇒ **报告用**；
    - `_rewrite_gate` 判的是「值不值得多花一次模型调用改写」⇒ **成本闸门**，
      只看改写**修得动**的那两项（`REWRITE_GATE_FEATURES`），且要「严重偏离」。

    合并的后果很具体：规格书有 34 项，逐项出带在跨书对照里能出 13 项 ——
    若「有出带就改写」，每一段都会白花一次模型调用，而改写只修得动断句。

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
        #
        # 三处「按需」（2026-10-27 量着定的，别改成凭感觉）：
        # 1. **只在裸台词太多时才付这笔钱** —— 低于下沿 p10 时补引导语只会让
        #    比值更小、离带更远（这道闸以前写成「不在带内」，两个方向都进）；
        # 2. **补到带的上沿就够，不追均值** —— 目的是「进带」，不是「打到平均」；
        # 3. **余量 2 处 + 不够就再来一轮**（`fill_lead_ins_until_in_band`）。
        #    余量是按某个采纳率估的，而**采纳率是模型决定的**：实测换模型后
        #    采纳率只有 67%~82%，固定余量会漏掉「只差 1 个槽位」的段。
        #    实测代价：11.7 → 5.6 次模型调用/段（-52%），出带的 5 段全部进带。
        lead_info = {"applied": False}
        ratio_now = after_merge["bare_ratio"]
        try:
            from app.style.lead_in import should_fill_lead_ins

            want_lead = should_fill_lead_ins(
                ratio_now, p10=target["p10"], p90=target["p90"],
            )
        except Exception:  # noqa: BLE001 —— 判不出来就别花钱
            want_lead = False
        if want_lead:
            try:
                from app.config import settings as _st2
                lead_on = bool(getattr(_st2, "style_lead_in", False))
            except Exception:  # noqa: BLE001
                lead_on = False
            if lead_on:
                try:
                    from app.style.lead_in import fill_lead_ins_until_in_band

                    work_paras, lead_info = fill_lead_ins_until_in_band(
                        work_paras, provider,
                        p10=target["p10"], p90=target["p90"],
                        proper_nouns=_reference_proper_nouns(db, project, novel_ids),
                        corpus_text=_reference_corpus_text(db, project, novel_ids) or None,
                        character_names=_project_character_names(db, project),
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


def _maybe_correct_attribution(
    db,
    project: Project,
    text: str,
    provider,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """归属校正：把「说话人归属」的写法掰到**参考书自己的写法**。

    ## 为什么要有（实测）

    参考书《道途魔典》**76.6%** 的对白带说话人归属（`X道：“…”`），
    我们的产出只有 **3.2%**（提示词加了一句口径后 14.2%）⇒ 差 5 倍。
    而盲评实测：「出现『X道：“…”』的那侧 = 人类侧」这条规则**精确率 100%**
    ⇒ 这是泄露面，不修就出不了能用的盲评包。

    ## 闸门（为什么要多花一次模型调用）

    只在**离目标够远**时才校正：差得不多就别动（改写本身有风险）。
    目标与样例都来自**参考书自己** ⇒ 换一本书自动跟着换。

    ## 失败策略

    `style/attribution_fix.correct_attribution` 内部有四道判据
    （对白内容守恒 / 字数 / 归属率是否靠近 / 不许过冲），
    任何一道不过就返回原文。这里再兜一层异常。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "style_attribution_correction", True)):
            return text, {"applied": False, "reason": "归属校正开关关闭"}
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.style.attribution_fix import (
            attribution_rate,
            attribution_target,
            correct_attribution,
        )
        from app.style.stylometry import paragraphs as _paras
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("归属校正模块不可用: %s", exc)
        return text, {"applied": False, "reason": "模块不可用"}

    before = attribution_rate(text)
    if before["quotes"] < 3:
        return text, {"applied": False, "reason": "对白太少", "before": before}

    # —— 目标与样例：从参考书的段落里量 ——
    try:
        ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
        target = None
        if ids:
            from app.models import NovelChunk
            rows = db.query(NovelChunk).filter(
                NovelChunk.novel_id.in_(ids)).limit(1200).all()
            pool: list[str] = []
            for r in rows:
                pool.extend(_paras(r.text or ""))
            target = attribution_target(pool)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("归属目标取不到: %s", exc)
        target = None
    if not target:
        return text, {"applied": False, "reason": "量不出归属目标", "before": before}

    # —— 闸门：离目标够远才值得花一次调用 ——
    from app.style.attribution_fix import MIN_GAP_TO_CORRECT, MIN_RATE_TO_CORRECT
    gap = abs(before["rate"] - target["rate"])
    if gap < MIN_GAP_TO_CORRECT:
        return text, {"applied": False, "reason": f"离目标够近（差 {gap:.0%}）",
                      "before": before, "target": target}
    if target["rate"] < MIN_RATE_TO_CORRECT:
        return text, {"applied": False, "reason": "参考书自己就几乎没有归属",
                      "before": before, "target": target}

    try:
        return correct_attribution(
            text, provider,
            target_rate=target["rate"],
            convention=target["convention"],
            examples=target["examples"],
        )
    except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
        logging.getLogger(__name__).warning("归属校正异常，保留原文: %s", exc)
        return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}


def _maybe_correct_ellipsis(
    db,
    project: Project,
    text: str,
    provider,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """省略号习惯校正：对白里「用省略号」的密度对齐**参考书自己**。

    ## 为什么要有（实测）

    2026-10-31 的盲评包里，「**有省略号的那侧 = 人类侧**」可判 7 对、
    判对 7 对、**精确率 100%** ⇒ 一条真泄露线。
    我们 0.068 个/百字 vs 参考书 0.684 个/百字（差 10 倍）。

    ## 闸门

    - 只在**离目标够远**时才动（差得不多就别碰）；
    - 目标与惯例都来自**参考书自己** ⇒ 换一本书自动跟着换。

    ## 失败策略

    `style/ellipsis_fix.apply_ellipsis_habit` 内部量完直接改（纯机械，
    不调模型）⇒ 失败极少；这里再兜一层异常。
    """
    if not text or not text.strip():
        return text, {"applied": False, "reason": "空文本"}
    try:
        from app.config import settings as _st
        if not bool(getattr(_st, "style_ellipsis_correction", True)):
            return text, {"applied": False, "reason": "省略号校正开关关闭"}
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.style.ellipsis_fix import (
            apply_ellipsis_habit,
            ellipsis_rate,
            ellipsis_target,
        )
        from app.style.stylometry import paragraphs as _paras
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("省略号校正模块不可用: %s", exc)
        return text, {"applied": False, "reason": "模块不可用"}

    before = ellipsis_rate(text)
    if before["quotes"] < 5:
        return text, {"applied": False, "reason": "对白太少", "before": before}

    # —— 目标：从参考书的段落里量 ——
    try:
        ids = list(novel_ids or (getattr(project, "reference_novel_ids", None) or []))
        target = None
        if ids:
            from app.models import NovelChunk
            rows = db.query(NovelChunk).filter(
                NovelChunk.novel_id.in_(ids)).limit(1200).all()
            pool: list[str] = []
            for r in rows:
                pool.extend(_paras(r.text or ""))
            target = ellipsis_target(pool)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("省略号目标取不到: %s", exc)
        target = None
    if not target:
        return text, {"applied": False, "reason": "量不出省略号目标", "before": before}

    gap = abs(before["rate"] - target["rate"])
    if gap < MIN_GAP_TO_ELLIPSIS:
        return text, {"applied": False, "reason": f"离目标够近（差 {gap:.0%}）",
                      "before": before, "target": target}

    try:
        return apply_ellipsis_habit(text, target["rate"])
    except Exception as exc:  # noqa: BLE001 —— 校正失败绝不能影响正文
        logging.getLogger(__name__).warning("省略号校正异常，保留原文: %s", exc)
        return text, {"applied": False, "reason": f"异常 {type(exc).__name__}"}


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


def _project_character_names(db, project: Project) -> set[str]:
    """本项目的**角色名表**（判据 7「说话人一致」用）。

    取两处：知识图谱里的 character 实体 + `plot_bible` 里的角色。
    取不到就返回空集 —— 调用方据此**降级**（判据 7 的第 3/4 条自动失效，
    只剩模型自报 vs 模型自报那两条）。
    """
    names: set[str] = set()
    try:
        from app.models import KgEntity
        from sqlalchemy import select

        rows = db.scalars(
            select(KgEntity).where(KgEntity.project_id == project.id)
        ).all()
        for e in rows:
            if getattr(e, "kind", "") == "character" and e.canonical_name:
                names.add(str(e.canonical_name).strip())
    except Exception as exc:  # noqa: BLE001 —— 取不到就降级，不阻断
        logging.getLogger(__name__).warning("角色表取不到，说话人判据降级: %s", exc)
    try:
        bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
        for item in (bible.get("characters") or []):
            name = item.get("name") if isinstance(item, dict) else item
            if name and str(name).strip():
                names.add(str(name).strip())
    except Exception:  # noqa: BLE001
        pass
    return {n for n in names if n}


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


def _rewrite_targets(text: str, profile: dict | None) -> dict:
    """改写要用的目标：**参考书自己的句长带**（p10~p90）。

    ## 为什么目标必须来自档案，不能写死

    改写提示词里原先写死「平均每句 45 到 70 个字」，而参考书实测的句长带
    只有 22~27 字 ⇒ 模型必然把句子拉过头（实测一句冲到 41.2 字）。
    又因为**句号密度 ≈ 1000 / 句长**（实测 39.1 vs 38.3、24.3 vs 24.5），
    句长冲过头 ⇒ 句号密度跟着一起出带 ——
    那正是「产出不像参考书」的头号短板（句号密度 94% 出带）。

    口径与 `band_check` 一致：按文本长度选桶，再取该桶的分布带。
    """
    if not profile:
        return {}
    try:
        from app.style.stylometry import pick_bucket

        base_unit = int((profile.get("_meta") or {}).get("base_unit_chars")
                        or profile.get("unit_chars") or 0)
        if profile.get("version") == 2 and profile.get("buckets"):
            bucket_name = pick_bucket(len(text), base_unit)
            bucket = (profile["buckets"].get(bucket_name)
                      or profile["buckets"].get("medium") or {})
        else:
            bucket = profile
        feat = bucket.get("feat") or profile.get("feat") or {}
        band = (feat.get("句长均值") or {})
        lo, hi = band.get("p10"), band.get("p90")
        if lo is None or hi is None:
            return {}
        return {"sentence_range": (float(lo), float(hi)), "bucket": bucket_name
                if profile.get("version") == 2 else "v1"}
    except Exception:  # noqa: BLE001 —— 取不到目标就按「没有目标」走，绝不能阻断生成
        return {}


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
    gate_info: dict = {"decision": decision}
    if decision == "gate":
        severe, reasons = _rewrite_gate(text, profile)
        gate_info["severe"] = bool(severe)
        gate_info["reasons"] = reasons
        if not severe:
            return text, {"applied": False, "reason": "偏离未达闸门",
                          "gates": reasons, "gate": gate_info}
    try:
        from app.generation.rewrite import rewrite_style
        targets = _rewrite_targets(text, profile)
        # 规格书偏差（2026-11-01）：让改写知道**现在差多少**，不只是**该到哪**。
        # 尺是同一把 —— 与注入提示词的那份 spec 同源。
        deviations = _style_gate(text, profile).get("deviations") or []
        new_text, info = rewrite_style(
            text, _rewrite_provider(provider),
            target_sentence_range=targets.get("sentence_range"),
            deviations=deviations,
        )
        info["gate"] = gate_info
        info["targets"] = ({"sentence_range": list(targets["sentence_range"])}
                           if targets.get("sentence_range") else None)
        if deviations:
            info["deviations"] = deviations
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

def _apply_style_corrections(
    db,
    project: Project,
    provider,
    text: str,
    *,
    novel_ids: list[int] | None = None,
) -> tuple[str, dict]:
    """落库前的文风改文本工序**唯一入口**（批 6）。

    顺序与 main 的 `_run_text_chain` 一致，逐条理由见 `_run_text_chain` 文档：
    文风改写 → 形态校正 → 归属校正 → 省略号校正 → beat 插入 → 照搬门禁。
    每一道都受对应 `style_*` 配置键控制（关掉就是空操作），因此本函数
    在默认配置下也会被调用，但只跑「该跑的那几道」。

    纪律：
    - **只做文本变换 + 只读库**，不落库（`_maybe_fix_verbatim` 例外：改不掉
      就抛 `VerbatimViolation`，由调用方决定这一段不落库——不能静默放过）；
    - 任何一道异常都已在各自函数内兜住（保留原文），不会阻断生成。

    返回 (正文, 诊断)。诊断键名与 main 的 `_run_text_chain` 对齐，
    只额外把规格书仲裁点叫 `spec_gate`——本分支 `quality["style_gate"]`
    已被 `generation_gate` 占用，不能撞名。
    """
    ids = list(novel_ids if novel_ids is not None
               else (project.reference_novel_ids or []))
    profile = _style_profile(db, project)
    diag: dict = {}

    text, diag["rewrite_info"] = _maybe_rewrite_style(
        project, text, provider, profile=profile)
    text, diag["form_info"] = _maybe_correct_form(
        db, project, text, provider, profile=profile, novel_ids=ids)
    text, diag["attr_info"] = _maybe_correct_attribution(
        db, project, text, provider, novel_ids=ids)
    text, diag["ell_info"] = _maybe_correct_ellipsis(
        db, project, text, provider, novel_ids=ids)
    text, diag["beat_info"] = _maybe_insert_beats(
        db, project, text, provider, novel_ids=ids)
    text, diag["verbatim_info"] = _maybe_fix_verbatim(
        db, project, text, provider, novel_ids=ids)
    diag["spec_gate"] = _style_gate(text, profile)
    return text, diag
