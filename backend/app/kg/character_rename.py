"""角色改名：先量影响面，再决定允许 / 拒绝。

用户口径（2026-11-11）：**影响前文的改名不允许**，只在角色刚出场、影响不大时允许。

判定源（Q1）
------------
- 唯一全文事实源是 `generations.text`：段级正文，每段一行；重生成是原地替换
  `text/summary`（`engine.regenerate_segment_stream`），不新增段行。
- v3 的「段级数据」是 `kg_mentions`：只有 `segment_no` + `context_window` 片段，
  是**抽取索引**而不是正文副本；`generation_id` 由 `batch_worker._seg_to_gen`
  逐字比对正文后才绑定。所以**不能**用 mention 表计数——抽取漏掉的提及在表里
  根本不存在。两者一致性：mention 是正文的子集，正文是唯一权威。
- `Generation.summary["text"]` 是前情提要，会进下一次 prompt；它由正文派生，
  本模块只**报告**它的命中数，不参与允许 / 拒绝判定（用户口径是正文）。

阈值（为什么是 3 不是 5）
--------------------------
参考语料《大奉打更人》49 个具名角色「首次出场章」的出现次数分布：
p25=2、p50=4、p75=11；只出现在一章的角色，该章次数同样集中在 1–4。
取 3 = **低于中位数的轻量出场**（45% 的角色在 1–3 次内完成首次亮相）：
一次引见 + 一两次动作/称呼。取 5 会越过中位数，把「已经写过两三个回合」的
角色也当成刚出场，而这些角色在最后一段里已经有多处需要返工的提及。
详细分布见 `.trae/documents/character-setting-edit-audit.md`。
"""
from __future__ import annotations

import copy

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kg.bible import RELATION_FROM_KEYS, RELATION_TO_KEYS, apply_bible_revision
from app.models import Generation, KgEntity, Project

#: 只出现在最后一段时允许的最大出现次数（依据见模块 docstring）。
MAX_LAST_SEGMENT_HITS = 3


def _segment_label(segs: list[int]) -> str:
    """段号列表 → 人话（连续压缩成区间）。"""
    if not segs:
        return "（无）"
    if len(segs) == 1:
        return str(segs[0])
    contiguous = segs == list(range(segs[0], segs[-1] + 1))
    if contiguous and len(segs) <= 20:
        return f"{segs[0]}–{segs[-1]}"
    if len(segs) <= 6:
        return "、".join(str(s) for s in segs)
    return f"{segs[0]}–{segs[-1]}（共 {len(segs)} 段）"


def _count_name(text: str, name: str, shadow_names: set[str]) -> int:
    """精确子串计数；但不把「更长的另一个角色名」里的片段算成本人。

    例：项目里同时有「李浩」和「李浩微」两个角色时，正文里的「李浩微」不能
    算作「李浩」的提及，否则 2 字名会被长名不断抬高计数。带称谓的
    「林夜兄」不在此列——它不是任何已知角色名，仍算作「林夜」的一次提及。

    只排除**其他角色**的更长的 canonical name；别名不参与排除（别名常是同一
    个人的另一种叫法，排除会漏计本人）。
    """
    shadows = [s for s in shadow_names if s != name and name in s and len(s) > len(name)]
    if not shadows:
        return text.count(name)
    spans: list[tuple[int, int]] = []
    for s in shadows:
        start = 0
        while True:
            i = text.find(s, start)
            if i < 0:
                break
            spans.append((i, i + len(s)))
            start = i + 1
    total = 0
    start = 0
    while True:
        i = text.find(name, start)
        if i < 0:
            break
        if not any(a <= i and i + len(name) <= b for a, b in spans):
            total += 1
        start = i + 1
    return total


def _other_character_names(db: Session, project_id: int, name: str) -> set[str]:
    """项目里**其他角色**的名字（bible + KG 实体），用于排除长名包含短名。"""
    out: set[str] = set()
    proj = db.get(Project, project_id)
    bible = proj.plot_bible if proj is not None and isinstance(proj.plot_bible, dict) else {}
    for c in bible.get("characters") or []:
        n = str((c.get("name") if isinstance(c, dict) else c) or "").strip()
        if n and n != name:
            out.add(n)
    for (n,) in db.execute(
        select(KgEntity.canonical_name).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == "character",
            KgEntity.canonical_name != name,
        )
    ):
        n = str(n or "").strip()
        if n:
            out.add(n)
    return out


def scan_name_scope(db: Session, project_id: int, name: str) -> dict:
    """量一个名字在已生成正文里的覆盖范围（逐段精确匹配，一次查询）。

    返回的 `allowed` 只按用户口径判定：从未出现，或只在最后一段且次数不超
    阈值。`summary_occurrences` 只报告，不影响判定。

    计数口径：精确子串，但排除「被更长的另一个角色名包住」的位置（李浩 vs
    李浩微）；带称谓形态（林夜兄）仍计入本人。
    """
    rows = db.execute(
        select(Generation.segment_no, Generation.text, Generation.summary)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no, Generation.id)
    ).all()
    shadows = _other_character_names(db, project_id, name)
    per_seg: dict[int, int] = {}
    summary_hits = 0
    for seg, text, summary in rows:
        n = _count_name(text or "", name, shadows)
        if n:
            per_seg[seg] = per_seg.get(seg, 0) + n
        if isinstance(summary, dict):
            body = summary.get("text")
            if body:
                summary_hits += _count_name(str(body), name, shadows)
    segments = sorted(per_seg)
    total = sum(per_seg.values())
    last_segment = rows[-1][0] if rows else None
    only_last = bool(segments) and all(s == last_segment for s in segments)
    if total == 0:
        allowed = True
    elif only_last and total <= MAX_LAST_SEGMENT_HITS:
        allowed = True
    else:
        allowed = False
    return {
        "source": "generations.text",
        "segment_count": len({row[0] for row in rows}),
        "last_segment": last_segment,
        "hits": [{"segment_no": s, "count": per_seg[s]} for s in segments],
        "segments": segments,
        "total": total,
        "only_last_segment": only_last,
        "summary_occurrences": summary_hits,
        "threshold": MAX_LAST_SEGMENT_HITS,
        "allowed": allowed,
    }


def _denial_receipt(old: str, new: str, scope: dict) -> tuple[str, list[str]]:
    """拒绝时的人话回执 + 替代路径。"""
    segs = scope["segments"]
    label = _segment_label(segs)
    if len(segs) >= 2:
        receipt = (
            f"「{old}」已经在第 {label} 段出现 {scope['total']} 次"
            f"（跨 {len(segs)} 段），改名会让前文对不上。"
        )
    else:
        receipt = (
            f"「{old}」虽然只出现在最后一段（第 {scope['last_segment']} 段），"
            f"但已经出现 {scope['total']} 次，超过上限 "
            f"{MAX_LAST_SEGMENT_HITS} 次——这个角色在这一段里已经写开了，"
            "改名会让这一段对不上。"
        )
    rewrite_where = f"第 {label} 段" if len(segs) >= 2 else f"第 {scope['last_segment']} 段"
    alternatives = [
        "接受现状：保留旧名，只改 role / motivation 等其它字段"
        "（这些字段会进入系统提示词，改名不是唯一手段）。",
        f"重写{rewrite_where}：把这 {scope['total']} 处「{old}」改成「{new}」后再改名"
        "（本工具不会自动改正文，正文是历史事实）。",
        f"把旧名登记为别名：v3 实体层支持 aliases（kg_entities.aliases），"
        f"旧名仍能在图谱/检索里解析到同一个人；但 bible 角色卡没有 aliases 字段，"
        f"系统提示词只读 bible 里的名字——所以这不是改名，不能替代上面两条。",
    ]
    return receipt, alternatives


def rename_character(
    db: Session,
    project_id: int,
    old_name: str,
    new_name: str,
    *,
    note: str | None = None,
) -> dict:
    """给 bible 角色改名；影响面超阈值时返回 ok=False 的回执（不写库）。

    允许时：改 `plot_bible.characters[].name`（同时改 bible 里以旧名为端点的
    关系）+ 同步 `kg_entities` 的 canonical_name，并把旧名登记为 aliases，
    让旧名在检索 / 图谱里仍解析到同一个人。

    失败（参数非法 / 目标不存在 / 图谱撞名）抛 ValueError，由 router 转 400。
    """
    old = (old_name or "").strip()
    new = (new_name or "").strip()
    if not old or not new:
        raise ValueError("旧名和新名都不能为空")
    if old == new:
        raise ValueError("新旧名字相同，无需改名")
    proj = db.get(Project, project_id)
    if proj is None:
        raise ValueError(f"project {project_id} 不存在")

    bible = proj.plot_bible if isinstance(proj.plot_bible, dict) else {}
    chars = bible.get("characters")
    if not isinstance(chars, list):
        raise ValueError("bible 里没有 characters 数组，无法定位要改名的角色")
    idxs = [
        i for i, c in enumerate(chars)
        if isinstance(c, dict) and str(c.get("name") or "").strip() == old
    ]
    if not idxs:
        raise ValueError(f"bible 的角色里没有「{old}」（名字需要完全一致）")
    if len(idxs) > 1:
        raise ValueError(f"bible 里有 {len(idxs)} 个「{old}」，先手动去重再改名")
    if any(
        isinstance(c, dict) and str(c.get("name") or "").strip() == new
        for c in chars
    ):
        raise ValueError(f"bible 里已经有叫「{new}」的角色，换一个名字")

    scope = scan_name_scope(db, project_id, old)
    if not scope["allowed"]:
        receipt, alternatives = _denial_receipt(old, new, scope)
        return {
            "ok": False,
            "decision": "denied",
            "old_name": old,
            "new_name": new,
            "scope": scope,
            "receipt": receipt,
            "alternatives": alternatives,
            "bible": None,
            "entity": None,
        }

    # —— KG 实体（Q3）：先确认能安全改，再和 bible 同一个事务落库 ——
    ents = db.scalars(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == "character",
            KgEntity.canonical_name == old,
        )
    ).all()
    if len(ents) > 1:
        raise ValueError(
            f"图谱里有 {len(ents)} 条同名角色实体（可能来自不同书），"
            "无法确定改哪一条；请先在 KG 里清理后再改名"
        )
    ent = ents[0] if ents else None
    clash = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == "character",
            KgEntity.canonical_name == new,
        )
    )
    if clash is not None and (ent is None or clash.id != ent.id):
        raise ValueError(
            f"图谱里已经有叫「{new}」的角色实体（#{clash.id}），"
            "改名会撞成两个同名来源；请先合并或换一个名字"
        )
    if ent is not None:
        aliases = [
            str(a).strip() for a in (ent.aliases or []) if str(a).strip()
        ]
        aliases = [a for a in aliases if a not in (old, new)]
        ent.canonical_name = new
        # 旧名进 aliases：旧段重抽 / 按旧名反查仍指向这一条。
        ent.aliases = list(dict.fromkeys(aliases + [old]))[:20]
        db.flush()  # 唯一键冲突在这里暴露，早于 bible 提交

    new_bible = copy.deepcopy(bible)
    new_bible["characters"][idxs[0]]["name"] = new
    rel_changed = 0
    for rel in new_bible.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for key in RELATION_FROM_KEYS + RELATION_TO_KEYS:
            if str(rel.get(key) or "").strip() == old:
                rel[key] = new
                rel_changed += 1
    revision = apply_bible_revision(
        db, project_id, new_bible,
        note=(note or "").strip() or f"角色改名：{old} → {new}",
    )

    parts = [f"已改名：「{old}」→「{new}」。"]
    if scope["total"] == 0:
        parts.append("旧名没有在任何已生成正文里出现过。")
    else:
        parts.append(
            f"旧名只出现在最后一段（第 {scope['last_segment']} 段）"
            f"{scope['total']} 次，在阈值 {MAX_LAST_SEGMENT_HITS} 次以内。"
        )
    if ent is not None:
        parts.append(
            f"图谱实体 #{ent.id} 已同步改名，旧名登记为别名"
            "（检索 / 图谱里旧名仍指向同一个人）。"
        )
    else:
        parts.append("图谱里没有这个名字的实体（手动添加的角色），无需联动。")
    if rel_changed:
        parts.append(f"bible 里以旧名为端点的关系同步更新了 {rel_changed} 处。")
    if scope["summary_occurrences"]:
        parts.append(
            f"注意：前情提要（摘要）里还有 {scope['summary_occurrences']} 处旧名；"
            "摘要与正文一样不会自动改写，重生成相关段时才会重建。"
        )
    return {
        "ok": True,
        "decision": "allowed",
        "old_name": old,
        "new_name": new,
        "scope": scope,
        "receipt": "".join(parts),
        "alternatives": [],
        "bible": revision,
        "entity": {
            "updated": ent is not None,
            "entity_id": ent.id if ent is not None else None,
            "canonical_name": new,
            "aliases": list(ent.aliases or []) if ent is not None else [],
        },
    }
