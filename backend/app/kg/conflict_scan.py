"""生成文本 vs 原著层的三类冲突检测。

只读 canon，不写 canon。冲突按严重度给出处置建议：
- canon 有明确设定：rewrite（只重写冲突所在段，最多一次），仍冲突则 mark；
- canon 没有设定：mark（标红，进入人工/复核队列，不自动改）。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from app.kg import canon_source
from app.kg.generated_extract import (
    GENERIC_SUBJECTS,
    GENERATED_NODE_KINDS,
    extract_claims,
    parse_aliases,
    parse_attrs,
)

_CN_RANK = {"九": 9, "八": 8, "七": 7, "六": 6, "五": 5, "四": 4, "三": 3, "二": 2, "一": 1}
_REALM_DEPTH = {
    "炼精": 9, "炼气": 8, "炼神": 7, "铜皮": 6, "银骨": 5,
    "铁血": 4, "武夫": 4, "律者": 5,
}
_DEATH_MARKERS = ("已死", "死亡", "身死", "毙命", "被杀", "阵亡")
_DEATH_CONTEXT = ("已死", "尸体", "死讯", "灵堂", "出殡", "祭", "死亡", "身死")
_ACTIVE_VERBS = "说|道|问|笑|走|站|坐|点头|皱眉|看|听|喊|答|推|拉|抬手|睁眼"


def _blob(attrs: dict) -> str:
    return json.dumps(attrs or {}, ensure_ascii=False)


def _rank_label(text: str) -> str:
    match = re.search(rf"[{''.join(_CN_RANK)}]品[^，。；\"}}]{{0,6}}", text)
    return match.group(0) if match else ""


def _parse_rank(text: str) -> int | None:
    """从属性文本里取最强（数字最小）的境界/品级。"""
    ranks: list[int] = []
    for match in re.finditer(rf"([{''.join(_CN_RANK)}])品", text):
        ranks.append(_CN_RANK[match.group(1)])
    for realm, depth in _REALM_DEPTH.items():
        if realm in text:
            ranks.append(depth)
    return min(ranks) if ranks else None


def build_canon_facts(canon_nodes: list[dict],
                      canon_edges: list[dict] | None = None) -> dict:
    """把 canon 节点整理成按名字可查的事实表（别名也登记）。"""
    facts: dict[str, dict] = {}
    alias_map: dict[str, str] = {}
    for node in canon_nodes:
        name = node["name"]
        attrs = parse_attrs(node.get("attrs"))
        blob = _blob(attrs)
        alive = attrs.get("alive")
        if alive is False:
            dead = True
        elif alive is True:
            dead = False
        else:
            # 只看状态类字段，避免把 secrets 里提到“死亡”的活人误判为死人。
            state_text = " ".join(
                str(attrs.get(key) or "")
                for key in ("status", "state", "arc", "ending", "outcome")
            )
            dead = any(marker in state_text for marker in _DEATH_MARKERS)
        factions: set[str] = set()
        for key in ("faction", "affiliation", "belongs_to", "sect", "organization"):
            value = attrs.get(key)
            if isinstance(value, str) and value.strip():
                factions.add(value.strip())
            elif isinstance(value, list):
                factions.update(str(v).strip() for v in value if str(v).strip())
        rank = _parse_rank(blob)
        entry = {
            "name": name,
            "kind": node.get("kind"),
            "dead": dead,
            "factions": factions,
            "rank": rank,
            "rank_text": _rank_label(blob) or blob[:80],
        }
        # 同名跨 kind 时优先保留 character 的身份信息。
        if name not in facts or (node.get("kind") == "character"
                                 and facts[name].get("kind") != "character"):
            facts[name] = entry
        for form in [name] + parse_aliases(node.get("aliases")):
            if len(form) >= 2:
                alias_map.setdefault(form, name)

    # 只用节点属性里的势力，不用 belongs_to 边：历史图谱里该关系噪声很大，
    # 会让「许七安同时属于几十个势力」这种假事实掩盖真实冲突。
    # 仅当属性完全缺失时，才用高强度 member_of 边兜底。
    if canon_edges:
        for edge in canon_edges:
            rel = edge.get("rel_type") or edge.get("rel")
            if rel != "member_of" or float(edge.get("strength") or 0) < 0.8:
                continue
            src = edge.get("from_name") or edge.get("from")
            dst = edge.get("to_name") or edge.get("to")
            if src in facts and dst and not facts[src]["factions"]:
                facts[src]["factions"].add(dst)

    return {"by_name": facts, "alias": alias_map}


def _resolve(name: str, index: dict) -> dict | None:
    canonical = index["alias"].get(name, name)
    return index["by_name"].get(canonical)


def _death_conflicts(text: str, index: dict) -> list[dict]:
    out: list[dict] = []
    for entry in index["by_name"].values():
        if not entry["dead"]:
            continue
        name = entry["name"]
        pattern = re.compile(rf"{re.escape(name)}[^。！？\n]{{0,10}}(?:{_ACTIVE_VERBS})")
        for sentence in re.split(r"(?<=[。！？!?])", text or ""):
            if name not in sentence or not pattern.search(sentence):
                continue
            if any(marker in sentence for marker in _DEATH_CONTEXT):
                continue  # 「赵县令已死的消息」不算复活
            out.append({
                "conflict_type": "dead_revival",
                "subject": name,
                "canon_value": "已死",
                "generated_value": sentence.strip()[:200],
                "evidence": sentence.strip()[:200],
                "decision": "rewrite",
                "note": "原著层该角色已死亡；先重写该段，复检仍冲突则标红",
            })
            break
    return out


def _faction_conflicts(claims: list[dict], index: dict) -> list[dict]:
    out: list[dict] = []
    for claim in claims:
        if claim["type"] != "faction":
            continue
        entry = _resolve(claim["subject"], index)
        if entry is None:
            continue
        canon_factions = sorted(entry["factions"])
        claimed = claim["value"]
        if canon_factions and any(
                claimed in fac or fac in claimed for fac in canon_factions):
            continue
        known = bool(canon_factions)
        reported = bool(claim.get("reported"))
        out.append({
            "conflict_type": "faction_mismatch",
            "subject": claim["subject"],
            "canon_value": "、".join(canon_factions) or "（原著未标势力）",
            "generated_value": claimed,
            "evidence": claim["evidence"],
            "decision": "reported" if reported else ("rewrite" if known else "mark"),
            "note": ("转述/传言语境，保留原文，仅提示" if reported
                     else ("原著势力明确，按原著改写" if known
                           else "原著无势力基线，标红待核")),
        })
    return out


def _realm_conflicts(claims: list[dict], index: dict) -> list[dict]:
    out: list[dict] = []
    for claim in claims:
        if claim["type"] != "realm":
            continue
        entry = _resolve(claim["subject"], index)
        if entry is None:
            continue
        claim_rank = _parse_rank(claim["value"])
        canon_rank = entry["rank"]
        if claim_rank is None:
            continue
        if canon_rank is None:
            out.append({
                "conflict_type": "realm_regression",
                "subject": claim["subject"],
                "canon_value": "（原著无境界基线）",
                "generated_value": claim["value"],
                "evidence": claim["evidence"],
                "decision": "reported" if claim.get("reported") else "mark",
                "note": ("转述/传言语境，保留原文，仅提示" if claim.get("reported")
                         else "原著无境界基线；标红待核，不直接注入下一章"),
            })
            continue
        if claim_rank > canon_rank:  # 数字越大境界越低
            out.append({
                "conflict_type": "realm_regression",
                "subject": claim["subject"],
                "canon_value": f"{entry['rank_text']}（rank={canon_rank}）",
                "generated_value": claim["value"],
                "evidence": claim["evidence"],
                "decision": "reported" if claim.get("reported") else "rewrite",
                "note": ("转述/传言语境，保留原文，仅提示" if claim.get("reported")
                         else "生成境界低于原著；重写该段，复检仍冲突则标红"),
            })
    return out


def _death_claim_conflicts(claims: list[dict], index: dict) -> list[dict]:
    """canon 未判死的角色被写成死亡：转述记 reported，非转述记 mark。

    这里不依赖“已死角色复活”那条规则，避免活人被写死时静默漏掉。
    """
    out: list[dict] = []
    for claim in claims:
        if claim.get("type") != "death":
            continue
        if claim.get("subject_status") == "unresolved":
            continue
        entry = _resolve(claim["subject"], index)
        if entry is None or entry["dead"]:
            continue
        reported = bool(claim.get("reported"))
        out.append({
            "conflict_type": "death_claim",
            "subject": claim["subject"],
            "canon_value": "原著未判死",
            "generated_value": "已死",
            "evidence": claim["evidence"],
            "decision": "reported" if reported else "mark",
            "note": ("转述/传言中的死亡，保留原文，仅提示" if reported
                     else "非转述死亡断言，标红待人工确认"),
        })
    return out


def _unresolved_subject_conflicts(claims: list[dict]) -> list[dict]:
    """主语落不到 canon 实体的事件必须显式报出，不能静默丢。"""
    out: list[dict] = []
    for claim in claims:
        if claim.get("subject_status") != "unresolved":
            continue
        out.append({
            "conflict_type": "unresolved_subject",
            "subject": "unresolved",
            "canon_value": "",
            "generated_value": claim.get("raw_subject") or "",
            "evidence": claim.get("evidence") or "",
            "decision": "mark",
            "note": "主语不是 canon 实体；事件保留，标红待核",
        })
    return out


def scan_text(text: str, chapter: int | None, canon_nodes: list[dict],
              canon_edges: list[dict] | None = None) -> list[dict]:
    """对一章生成文本做三类冲突检测，返回去重后的冲突列表。"""
    index = build_canon_facts(canon_nodes, canon_edges)
    subject_names = [
        name for name, entry in index["by_name"].items()
        if len(name) >= 2 and name not in GENERIC_SUBJECTS
        and entry["kind"] in GENERATED_NODE_KINDS
    ]
    claims = extract_claims(text, subject_names)
    conflicts = _death_conflicts(text, index)
    conflicts += _death_claim_conflicts(claims, index)
    conflicts += _unresolved_subject_conflicts(claims)
    conflicts += _faction_conflicts(claims, index)
    conflicts += _realm_conflicts(claims, index)

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for item in conflicts:
        key = (item["conflict_type"], item["subject"], item["generated_value"])
        if key in seen:
            continue
        seen.add(key)
        item["chapter"] = chapter
        unique.append(item)
    return unique


def scan_polarity(edges: list[dict]) -> dict:
    """canon 内部极性审计：同一对实体的 ally / enemy 是否自相矛盾。

    两类口径写死在这里，**不要**再用「章节区间重叠」混算：

    - ``same_chapter_clash``：正反两条边的**章号集合有交集**
      （同一章里既写盟友又写敌人，多半是抽取把不同对象混到一对实体上）；
    - ``cross_chapter_flip``：**章号集合无交集**
      （先敌后友或先友后敌，属于剧情反转，可用于「曾…」表述）。

    交集判断用章号集合，不用「最早章–最晚章」区间：区间重叠会把
    「ch1–200 为敌、ch150 为友」这种真实反转误判成同章矛盾。
    """
    groups: dict[frozenset, dict[str, list[dict]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for edge in edges:
        rel = edge.get("rel") or edge.get("rel_type")
        if rel not in canon_source.POLARITY_RELS:
            continue
        src = edge.get("from") or edge.get("from_name")
        dst = edge.get("to") or edge.get("to_name")
        if not src or not dst:
            continue
        groups[frozenset((src, dst))][rel].append(edge)

    same: list[dict] = []
    cross: list[dict] = []
    for pair, by_rel in groups.items():
        if len(by_rel) < 2:  # 只有一个极性方向，不算冲突
            continue
        ally = sorted({ch for e in by_rel.get("ally", [])
                       for ch in canon_source.chapter_list(e.get("chapters"))})
        enemy = sorted({ch for e in by_rel.get("enemy", [])
                        for ch in canon_source.chapter_list(e.get("chapters"))})
        item = {
            "pair": sorted(pair),
            "ally_chapters": ally,
            "enemy_chapters": enemy,
            "overlap": sorted(set(ally) & set(enemy)),
        }
        (same if item["overlap"] else cross).append(item)

    same.sort(key=lambda x: x["pair"])
    cross.sort(key=lambda x: x["pair"])
    return {
        "same_chapter_clash": same,
        "cross_chapter_flip": cross,
        "counts": {
            "same_chapter_clash": len(same),
            "cross_chapter_flip": len(cross),
            "total": len(same) + len(cross),
        },
    }
