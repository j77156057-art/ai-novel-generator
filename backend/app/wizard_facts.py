"""引导讲解的**可核查事实层**。

为什么单独一层：之前讲解文案把「14 维」「833 章约 42 分钟」写死在字符串里，
其中至少两处与代码/实测不一致（维度实际是 14 个提炼维度 + 1 个漂移维度 +
4 个反向禁忌；42 分钟对应的是 n_ch=764 那次实测，不是 833 章直接跑）。
模型只负责组织语言，**所有数字必须先在这里算出来**——这里没有的数，
提示词里也不许出现。

事实来源：
- 档位 / 维度：`app.style.narrative` 的 TIER_REGISTRY / NARRATIVE_DIMENSIONS /
  FORBIDDEN_DIMENSIONS（代码即事实）。
- 耗时：`docs/style-distillation-tiers.md` 里唯一一次计时
  （《大奉打更人》833 章语料、n_ch=764、42 分钟、glm-4-flash）。
  其它档位/章数一律标注为「按这一实测点外推」，不许说成实测。
- 语料章数 / 字数 / 已有档案：查当前项目与数据库。
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.style.narrative import (
    FORBIDDEN_DIMENSIONS,
    NARRATIVE_DIMENSIONS,
    SAMPLE_RATIO,
    TIER_REGISTRY,
    NarrativeTier,
    resolve_tier,
)

#: 文档里唯一一次带计时的实测点。**不要**把它当成「833 章 = 42 分钟」。
#:
#: 口径辨析（这一条以前被读错过）：文档写的是「采样 764 章（833 × 15% ≈ 125 章，
#: 分块后 764）」。对照语料 1 的记录（603 章、15% ≈ 90 章、分块后 267），
#: 764 / 267 都是**分块后的块数**，不是章数——块按约 1000 字切，两本语料的
#: 块数/章数比值不同（6.1 vs 3.0），正好对得上各自的平均章长。
#: 所以 42 分钟对应的是「5.1M 字语料的 15%（≈76.5 万字、764 块）」，
#: 折算约 3.3 秒/块；外推必须按**字数**，不能按章数。
MEASURED = {
    "novel": "《大奉打更人》",
    "corpus_chapters": 833,
    "corpus_chars": 5_100_000,
    "sampled_ratio": 0.15,
    "sampled_chapters": 125,
    "sampled_chars": int(5_100_000 * 0.15),
    "blocks": 764,
    "minutes": 42,
    "model": "glm-4-flash",
    "note": "42 分钟对应 15% 采样（≈125 章 / 76.5 万字 / 分块后 764 块），"
    "不是 833 章全书，也不是 764 章。",
}

#: 实测点的分钟/字，用于按字数外推（明确标注为估算）。
_MINUTES_PER_CHAR = MEASURED["minutes"] / MEASURED["sampled_chars"]

#: 运行时在提炼维度之外附加的一维（当前 13 个提炼维度 + 这 1 个）。
EXTRA_DIMENSION = "文风漂移分析"


def dimension_inventory() -> dict[str, Any]:
    """返回维度清单（含「为什么有人记成不止 13 维」的解释）。"""
    return {
        "extract": list(NARRATIVE_DIMENSIONS),
        "extract_count": len(NARRATIVE_DIMENSIONS),
        "runtime_extra": [EXTRA_DIMENSION],
        "forbidden": list(FORBIDDEN_DIMENSIONS),
        "forbidden_count": len(FORBIDDEN_DIMENSIONS),
        # 落库维度 = 提炼维度 + 漂移分析（docs 表格里的「维度数 15」）
        "stored_count": len(NARRATIVE_DIMENSIONS) + 1,
        # 全部维度键 = 落库维度 + 反向禁忌（19 个）
        "total_keys": len(NARRATIVE_DIMENSIONS) + 1 + len(FORBIDDEN_DIMENSIONS),
    }


def estimate_minutes(sampled_chars: int) -> dict[str, Any]:
    """按唯一实测点、按**待提炼字数**外推预计耗时（分钟）。"""
    chars = max(0, int(sampled_chars))
    minutes = max(1, math.ceil(chars * _MINUTES_PER_CHAR))
    return {
        "sampled_chars": chars,
        "minutes": minutes,
        "basis": (
            f"按文档实测点（{MEASURED['novel']}语料 {MEASURED['corpus_chars'] / 10000:.0f} 万字、"
            f"{MEASURED['sampled_ratio'] * 100:.0f}% 采样约 {MEASURED['sampled_chapters']} 章 / "
            f"{MEASURED['sampled_chars'] / 10000:.1f} 万字、分块后 {MEASURED['blocks']} 块、"
            f"{MEASURED['minutes']} 分钟、{MEASURED['model']}）按字数线性外推，"
            "不是该档实测值"
        ),
    }


def sampled_chapters_for(tier: NarrativeTier, total_chapters: int) -> int:
    """估算某档在某语料上会取多少章（与采样实现同口径）。

    采样覆盖只作用于按比例采样的档位（完整档）；固定 30 章的档位保持原口径，
    否则「采样改 200 章」会把平衡/快速也拖成 200 章，和档位定义矛盾。
    """
    total = max(0, int(total_chapters))
    if total <= 0:
        return 0
    if tier.sampling_mode == "percentage":
        cap = tier.sample_cap
        if cap is not None:
            return total if int(cap) <= 0 else min(total, int(cap))
        return min(total, max(10, int(total * (tier.sample_ratio or SAMPLE_RATIO))))
    return min(total, max(1, int(tier.fixed_chapters or 30)))


def sampled_chars_for(
    tier: NarrativeTier, total_chapters: int, total_chars: int
) -> int:
    """估算某档实际要过模型的字数（按采样章数等比例折算）。

    耗时与分块数（≈字数）线性相关，与章数无关——平均章长不同的两本书，
    同样 30 章的字数差一倍，耗时也差一倍。
    """
    total = max(0, int(total_chapters))
    chars = max(0, int(total_chars))
    if total <= 0 or chars <= 0:
        return 0
    sampled = sampled_chapters_for(tier, total)
    return int(chars * sampled / total)


def tier_facts(
    total_chapters: int,
    total_chars: int = 0,
    override: int | None = None,
) -> list[dict[str, Any]]:
    """三个档位在当前语料上的真实配置与预计耗时。

    ``override`` 是项目级采样覆盖；只叠到按比例采样的档位上。
    """
    out: list[dict[str, Any]] = []
    for key in ("full", "balanced", "fast"):
        tier = TIER_REGISTRY.get(key)
        if tier is None:
            continue
        if override is not None and tier.sampling_mode == "percentage":
            from app.style.narrative import tier_with_sampling

            tier = tier_with_sampling(tier, int(override))
        sampled = sampled_chapters_for(tier, total_chapters)
        sampled_chars = sampled_chars_for(tier, total_chapters, total_chars)
        est = estimate_minutes(sampled_chars)
        out.append(
            {
                "key": tier.key,
                "name": tier.name,
                "sampling_mode": tier.sampling_mode,
                "sample_ratio": tier.sample_ratio,
                "fixed_chapters": tier.fixed_chapters,
                "sample_cap": tier.sample_cap,
                # 卡片仍需要相对耗时标签；分钟数只从 estimate_minutes 取。
                "cost_hint": {"full": "最慢", "balanced": "中等", "fast": "最快"}.get(
                    tier.key, ""
                ),
                "dimension_count": tier.dimension_count,
                "dimensions": list(tier.dimensions),
                "normalize_character_names": tier.normalize_character_names,
                "fake_character_cleaning": tier.fake_character_cleaning,
                "voice_mode": tier.voice_mode,
                "sampled_chapters": sampled,
                "sampled_chars": sampled_chars,
                "estimated_minutes": est["minutes"],
                "estimate_basis": est["basis"],
            }
        )
    return out


def novel_stats(db: Session, ids: list[int]) -> list[dict[str, Any]]:
    """按 novel_id 统计真实章数与字数（来自 NovelChunk，不是文件名）。"""
    return _novel_stats(db, ids)


def _novel_stats(db: Session, ids: list[int]) -> list[dict[str, Any]]:
    """按 novel_id 统计真实章数与字数（来自 NovelChunk，不是文件名）。"""
    from app.models import NovelChunk, RawNovel

    if not ids:
        return []
    rows = db.execute(
        select(
            NovelChunk.novel_id,
            func.count(func.distinct(NovelChunk.chapter_no)),
            func.coalesce(func.sum(func.length(NovelChunk.text)), 0),
        )
        .where(NovelChunk.novel_id.in_(ids))
        .group_by(NovelChunk.novel_id)
    ).all()
    stats = {
        int(nid): {"chapters": int(n or 0), "chars": int(chars or 0)}
        for nid, n, chars in rows
    }
    titles: dict[int, str] = {}
    for novel in db.scalars(select(RawNovel).where(RawNovel.id.in_(ids))).all():
        titles[int(novel.id)] = str(novel.title or f"#{novel.id}")
    return [
        {"id": nid, "title": titles.get(nid, f"#{nid}"), **stats.get(nid, {"chapters": 0, "chars": 0})}
        for nid in ids
    ]


def archive_facts(project) -> dict[str, Any] | None:
    """项目里已有提炼档案的真实内容摘要。"""
    profile = getattr(project, "style_narrative", None)
    if not isinstance(profile, dict) or not profile:
        return None
    rules = profile.get("rules") or {}
    by_dim = {k: len(v) for k, v in rules.items() if isinstance(v, list)}
    forbidden = profile.get("forbidden") or {}
    rejected = profile.get("rejected")
    return {
        "tier": profile.get("tier"),
        "tier_name": profile.get("tier_name"),
        "n_chapters": profile.get("n_chapters"),
        "rule_count": sum(by_dim.values()),
        "dimension_count": len(by_dim),
        "dimensions": sorted(by_dim),
        "forbidden_count": (
            sum(len(v) for v in forbidden.values() if isinstance(v, list))
            if isinstance(forbidden, dict)
            else 0
        ),
        "rejected": rejected if isinstance(rejected, int) else len(rejected or []),
        "voice_checked": (profile.get("voice_name_audit") or {}).get("checked"),
        "failed_chunks": profile.get("failed_chunks") or 0,
    }


def distill_context(db: Session, project) -> dict[str, Any]:
    """提炼这一步讲解所需的全部事实（供 wizard_flow / wizard_chat 使用）。"""
    ids = [int(i) for i in (getattr(project, "reference_novel_ids", None) or [])]
    books = _novel_stats(db, ids)
    total_chapters = sum(b["chapters"] for b in books)
    total_chars = sum(b["chars"] for b in books)
    archive = archive_facts(project)
    override = getattr(project, "narrative_sample_chapters", None)
    tiers = tier_facts(total_chapters, total_chars, override)
    ctx = {
        "corpus_books": books,
        "corpus_total_chapters": total_chapters,
        "corpus_total_chars": total_chars,
        "archive": archive,
        "archive_exists": archive is not None,
        "last_tier": getattr(project, "narrative_tier", None),
        "sampling_override": override,
        "tiers": tiers,
        "dimension_inventory": dimension_inventory(),
        "measured": MEASURED,
    }
    # 全本口径的耗时：**不受采样覆盖影响**。讲解里说「全本 1528 章要多久」
    # 时不能拿被覆盖成 200 章的档位分钟数，那会把 84 分钟说成 11 分钟。
    ctx["full_corpus_minutes"] = estimate_minutes(total_chars)["minutes"]
    ctx["full_corpus_hours"] = round(ctx["full_corpus_minutes"] / 60, 1)
    ctx["fact_sheet"] = fact_sheet(ctx)
    return ctx


def fact_sheet(ctx: dict[str, Any]) -> str:
    """给模型看的硬事实清单（也是报告里的「解释可用事实清单」）。"""
    books = ctx.get("corpus_books") or []
    lines: list[str] = []
    if books:
        names = "、".join(
            f"《{b['title']}》{b['chapters']} 章 / {b['chars'] / 10000:.1f} 万字"
            for b in books
        )
        lines.append(f"- 当前项目投喂的语料：{names}")
        lines.append(f"- 语料合计：{ctx.get('corpus_total_chapters')} 章 / "
                     f"{(ctx.get('corpus_total_chars') or 0) / 10000:.1f} 万字")
    else:
        lines.append("- 当前项目没有投喂参考语料")
    lines.append(
        f"- 实测点：{MEASURED['novel']}语料 {MEASURED['corpus_chapters']} 章、"
        f"采样 {MEASURED['sampled_chapters']} 章、{MEASURED['minutes']} 分钟、"
        f"{MEASURED['model']}；其余档位/章数是按这一刻线性外推的估算"
    )
    for t in ctx.get("tiers") or []:
        if t["key"] == "full":
            mode = f"按语料 {int((t['sample_ratio'] or 0) * 100)}% 采样"
        else:
            mode = f"固定 {t['fixed_chapters']} 章"
        if t.get("sample_cap") is not None:
            mode = "全本（不抽样）" if int(t["sample_cap"]) <= 0 else f"最多 {t['sample_cap']} 章"
        lines.append(
            f"- {t['name']}：{mode}，本语料约 {t['sampled_chapters']} 章 / "
            f"{(t.get('sampled_chars') or 0) / 10000:.0f} 万字，"
            f"{t['dimension_count']} 个维度，预计约 {t['estimated_minutes']} 分钟；"
            f"角色名归一化={'开' if t['normalize_character_names'] else '关'}，"
            f"伪角色清洗={t['fake_character_cleaning']}"
        )
    dim = ctx.get("dimension_inventory") or dimension_inventory()
    lines.append(
        f"- 全本口径（不抽样、整本灌进去）预计约 {ctx.get('full_corpus_minutes')} 分钟"
        f"（约 {ctx.get('full_corpus_hours')} 小时；同样按实测点外推，不是实测值）"
    )
    lines.append(
        f"- 维度：{dim['extract_count']} 个提炼维度（NARRATIVE_DIMENSIONS）+ 1 个"
        f"「{EXTRA_DIMENSION}」= {dim['stored_count']} 个落库维度；另有 "
        f"{dim['forbidden_count']} 个反向禁忌维度，合计 {dim['total_keys']} 个键"
    )
    lines.append("- 提炼维度明细：" + "、".join(dim["extract"]))
    lines.append("- 反向禁忌维度：" + "、".join(dim["forbidden"]))
    if ctx.get("archive"):
        a = ctx["archive"]
        lines.append(
            f"- 已有档案：{a.get('tier_name') or a.get('tier')} 档，"
            f"n_chapters={a.get('n_chapters')}，规则 {a.get('rule_count')} 条 / "
            f"{a.get('dimension_count')} 个维度，反向禁忌 {a.get('forbidden_count')} 条，"
            f"剔除 {a.get('rejected')} 条"
        )
    else:
        lines.append("- 已有档案：无")
    if ctx.get("sampling_override") is not None:
        cap = int(ctx["sampling_override"])
        lines.append(
            "- 当前采样覆盖：" + ("全本" if cap <= 0 else f"最多 {cap} 章")
        )
    if ctx.get("last_tier"):
        lines.append(f"- 上次选过的档位：{ctx['last_tier']}")
    return "\n".join(lines)


def tier_by_key(key: str | None) -> NarrativeTier:
    """给动作层用：解析档位，未知抛 ValueError。"""
    return resolve_tier(key)
