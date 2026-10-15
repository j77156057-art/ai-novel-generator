"""节奏参数蒸馏（v2.4）—— 高潮密度从语料章节情绪反推。

首轮（§7.1）：avg_node_segments 用默认 5，每 4-6 段一个主干节点。
v2.4：补章节级情绪标注 → 统计"高潮章占比" → 反推平均每几段一个主干节点。

数据流：
1. label_chapters_in_novel: 扫一个 novel_id 的所有 NovelChunk，按
   chapter_no 分组 → 每章调一次 LLM classify_chapter_emotion → 标一个情绪
2. compute_plot_rhythm: 多本合并所有 chapter_emotions，调
   distill_avg_node_segments + distill_branch_ratio（已有，移到本文件
   共享）→ 写 plot_rhythm JSON

蒸馏纯函数（distill_avg_node_segments）：
- 输入：list[str]，每章一个情绪标签
- 输出：int，平均每几段一个主干节点
- 设计：高潮章占总章比例 = p_non_branch，
  avg_node_segments = round(1 / p_non_branch)（最简反推）。
  clamp 在 [3, 12]，与 plan_timeline.MIN/MAX_MAIN_NODES 一致。

设计取舍：
- 不调 LLM 训分类模型——单章情绪标注用 LLM（已经能识别"爽/虐/甜/怒/
  悬疑/日常/过渡"），保证情绪标签质量
- 失败兜底：LLM 异常 / 章节无文本 → 默认情绪"日常"（不计入高潮）
- 缓存：plot_rhythm 存 source_ids + version 指纹（与 style_narrative
  同模式），source 变了或代码改了（version +1）自动重算
"""
from __future__ import annotations

import json as _json
import re as _re
from collections import Counter

#: 蒸馏档 schema 版本。改蒸馏逻辑 +1，老数据降级回退到默认。
RHYTHM_VERSION = 1

#: 章节情绪标签候选（与 classify_chapter_emotion 的 system prompt 对齐）
_CHAPTER_EMOTIONS = ("爽", "虐", "甜", "怒", "悬疑", "日常", "过渡")

#: 非高潮标签——与 timeline.distill_branch_ratio 的 _BRANCH_EMOTIONS 对齐
_NON_CLIMAX_EMOTIONS = frozenset({"日常", "过渡", "支线", "闲笔"})

#: 蒸馏值边界——与 timeline.MIN/MAX_MAIN_NODES 一致
_MIN_AVG = 3
_MAX_AVG = 12


def classify_chapter_emotion(provider, chapter_text: str) -> str | None:
    """v2.4：单章情绪标注。

    返回值语义：
    - 情绪字符串（"爽"/"虐"/.../"过渡"）：成功
    - None：失败/超时/解析不出/无文本——回退到"日常"（不计入高潮）

    容错同 §7.3 plan_nodes_with_llm：```json 包裹、找 {...} 块、缺键
    返回 None。
    """
    if not chapter_text or not chapter_text.strip():
        return None

    # 节流：取前 1500 字——LLM 判情绪不需要全章
    excerpt = chapter_text[:1500] if len(chapter_text) > 1500 else chapter_text
    system = (
        "你是网文编辑。任务：判断下面这一章的整体情绪基调。\n"
        "可选标签：爽 | 虐 | 甜 | 怒 | 悬疑 | 日常 | 过渡\n"
        "要求：\n"
        "1. 选最贴近的一标签。\n"
        "2. 严格按 JSON 输出，不要解释。\n"
        '格式：{"emotion": "爽"} 或 {"emotion": "日常"} 等。\n'
        "只输出 emotion 字段。"
    )
    user = f"【本章正文】\n{excerpt}\n\n判断：这一章的情绪基调是？"
    try:
        raw = provider.chat(system, user)
    except Exception:  # noqa: BLE001
        return None
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    emo = data.get("emotion")
    if not isinstance(emo, str):
        return None
    # 校验是否在候选集
    if emo not in _CHAPTER_EMOTIONS:
        return None
    return emo


def _gather_chapter_texts(db, novel_id: int) -> list[tuple[int, str]]:
    """取一个 novel 的所有 chapter，按 chapter_no 合并 chunks 文本。

    返回 [(chapter_no, concatenated_text), ...] 按 chapter_no 升序。
    """
    from app.models import NovelChunk
    from sqlalchemy import select

    rows = db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
    ).all()
    chapters: dict[int, list[str]] = {}
    for r in rows:
        if r.chapter_no is None:
            continue
        chapters.setdefault(r.chapter_no, []).append(r.text or "")
    return sorted(
        ((cn, "\n".join(parts)) for cn, parts in chapters.items()),
        key=lambda p: p[0],
    )


def label_chapters_in_novel(db, novel_id: int, provider) -> list[str]:
    """v2.4：扫一个 novel 的所有 chapter，依次调 LLM 标情绪。

    返回按 chapter_no 升序的标签列表（与 _gather_chapter_texts 顺序一致）。
    失败章节的标签回退为"日常"（不计入高潮，蒸馏时不会拉高密度）。
    """
    chapters = _gather_chapter_texts(db, novel_id)
    out: list[str] = []
    for _cn, text in chapters:
        emo = classify_chapter_emotion(provider, text)
        out.append(emo or "日常")
    return out


def distill_avg_node_segments(
    chapter_emotions: list[str],
    *,
    min_avg: int = _MIN_AVG,
    max_avg: int = _MAX_AVG,
) -> int:
    """v2.4 蒸馏纯函数：章节情绪列表 → avg_node_segments。

    设计：
    - 高潮章占比 p = len(非日常+非过渡+非支线+非闲笔) / len(总章)
    - avg_node_segments = round(1 / p)（最简反推：高潮越密，节点越短）
    - 边界：p==0 → 全部日常，avg=MAX（最稀疏）；p==1 → 全部高潮，
      avg=MIN（最密集）
    - clamp [min_avg, max_avg] 与 timeline.MIN/MAX_MAIN_NODES 一致
    - 空列表回退默认 5（v2.1 起的 DEFAULT_AVG_NODE_SEGMENTS）

    测试用例：
    - 全部"日常" → p=0 → avg=12
    - 全部"爽" → p=1 → avg=3
    - 一半高潮 → p=0.5 → avg=2 → clamp 到 3
    - 1/10 高潮 → p=0.1 → avg=10
    """
    if not chapter_emotions:
        return 5  # DEFAULT_AVG_NODE_SEGMENTS，与 timeline 同步
    climax = sum(1 for e in chapter_emotions if e not in _NON_CLIMAX_EMOTIONS)
    p = climax / len(chapter_emotions)
    if p <= 0:
        avg = max_avg
    else:
        avg = round(1.0 / p)
    return max(min_avg, min(avg, max_avg))


def compute_plot_rhythm(
    db, project, novel_ids: list[int], provider
) -> dict:
    """v2.4：组合多本 novel 的章节情绪，蒸馏 + 落库 plot_rhythm。

    返回 dict（含 source_ids + version + chapter_emotions + avg_node_segments
    + branch_ratio + distilled_at），调用方写回 project.plot_rhythm。

    缓存指纹：source_ids + RHYTHM_VERSION。改 source 或 version → 重算。

    设计：
    - 多本合并：所有 novel 的章节情绪列表 concat
    - 空/novels 时：返回空 dict（plot_rhythm 写 None，回退到默认）
    - 容错：单章失败 → "日常"；不阻断整批蒸馏
    """
    if not novel_ids:
        return {}

    all_emotions: list[str] = []
    per_novel_count: dict[int, int] = {}
    for nid in novel_ids:
        emos = label_chapters_in_novel(db, nid, provider)
        all_emotions.extend(emos)
        per_novel_count[nid] = len(emos)

    if not all_emotions:
        return {}

    avg = distill_avg_node_segments(all_emotions)
    # branch_ratio 沿用 timeline 已有 distill（v2.1 设计）——这里复用
    from app.generation.timeline import distill_branch_ratio
    br = distill_branch_ratio(all_emotions)

    return {
        "version": RHYTHM_VERSION,
        "source_ids": list(novel_ids),
        "chapter_emotions": all_emotions,  # 落库便于审计/复用
        "chapter_count_per_novel": per_novel_count,
        "avg_node_segments": avg,
        "branch_ratio": br,
        "distilled_at": __import__("time").time(),
    }
