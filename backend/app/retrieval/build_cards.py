"""第一步：把一份蒸馏档案落成 L1–L5 路径 + 卡片（设计稿 §3、§5.12.1）。

## 硬约束（§5.12.1）

1. **结构层写死**：L2 场景 14 / L3 节拍 6 / L4 技法 8 来自 `catalog_paths.py`，
   不跟书变；计数层（每条路径多少条）随档案。
2. **0 条路径不列**：只物化有卡片的路径；空路径不写进卡表。
3. **按条数降序**：骨架渲染时排序；卡表本身保持档案顺序。
4. **<5 条并入上级**：只影响骨架渲染（`skeleton.py`），卡表仍保留原路径。
5. **缓存键 `distill_id + catalog_version`**：版本号由档案指纹 + 路径/词表/
   打分版本共同决定，任一变化都会生成新文件，不覆盖旧版本。

## 与现有打分的关系

四维打分不推翻：直接调用 `app.style.narrative.build_rule_score_cache`（同一套
`_score_rule` / `_HARD_FILTER_MASK`），卡片只是把分数与路径一起存下来。

`文笔量化审计` 是**观测**不是写作建议，`_HARD_FILTER_MASK` 会把整族判为
`FLAG_GENERIC`（实测 635/635）。按 2026-11-13 裁决，**不再豁免**：这一族不进
卡库，仍由 `_render_audit_block` 固定注入，所以 L1 `metric` 在本卡库中为 0。

`高频场景模板` 按设计稿 §8.2 是逐章剧情摘要，卡片刻意保留但统一标
`deprecated=True`；`search / browse / similar / skeleton` 默认不返回，
`rule` 回查仍可拿到。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.retrieval.catalog_config import SCENE_MATCHER
from app.retrieval.catalog_paths import (
    L1_LABELS,
    L1_METRIC,
    L1_ORDER,
    L1_STYLE,
    L2_LABELS,
    L3_LABELS,
    L4_LABELS,
    assign_l4,
    beat_key,
    join_path,
    l1_for_dim,
    path_depth,
    shape_for,
    strip_beat_prefix,
)
from app.retrieval.catalog_store import (
    SCHEMA_VERSION,
    card_id,
    catalog_path,
    save_catalog,
)

PUNCT_RE = re.compile(r"[\s，。、；：！？“”‘’\"'（）()【】\[\]—…·,.;:!?~～]+")
EVIDENCE_RE = re.compile(r"证据[：:]\s*(.+)$", re.S)
QUOTE_RE = re.compile(r"[“\"「][^”\"」]{2,}[”\"」]")

SUMMARY_MAX_CHARS = 30
SKIP_DIMS = {"文风漂移分析"}
#: 保留但不参与默认检索的维度（设计稿 §8.2：逐章剧情摘要，不是写作规则）。
DEPRECATED_DIMS = {"高频场景模板"}

#: 路径/词表/卡表语义版本。改 L2/L3/L4 表、L4 词表、硬过滤口径或失效标记时
#: 手动 +1，确保 `distill_id + catalog_version` 缓存键变化。
PATH_SCHEMA_VERSION = "paths-v3"


# --------------------------------------------------------------------------- #
# 档案读取（只读 SQLite，避免被其它进程的写锁挡住）
# --------------------------------------------------------------------------- #

def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _load_profile(conn: sqlite3.Connection, distill_id: int) -> tuple[dict, list[int], str]:
    row = conn.execute(
        "select id, source_ids, style_narrative from style_distills where id=?",
        (distill_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"style_distills 里没有 id={distill_id}")
    profile = json.loads(row["style_narrative"] or "{}")
    try:
        source_ids = list(json.loads(row["source_ids"] or "[]"))
    except Exception:  # noqa: BLE001 - 老档案可能是逗号串
        source_ids = [int(x) for x in str(row["source_ids"] or "").strip("[]").split(",") if x.strip()]
    return profile, source_ids, str(row["style_narrative"] or "")


def _load_control(conn: sqlite3.Connection, distill_id: int,
                  source_ids: list[int]) -> dict | None:
    """挑一份「不是本书」的 full 档案当对照组，复用 FLAG_GENERIC。"""
    rows = conn.execute(
        "select id, source_ids, style_narrative from style_distills "
        "where id<>? order by id desc", (distill_id,),
    ).fetchall()
    for row in rows:
        try:
            ids = list(json.loads(row["source_ids"] or "[]"))
        except Exception:  # noqa: BLE001
            continue
        if ids and not (set(ids) & set(source_ids)) and len(ids) >= 2:
            return json.loads(row["style_narrative"] or "{}")
    return None


# --------------------------------------------------------------------------- #
# 场景定位：证据 chunk ±1 → 章内最佳 3-chunk 窗口 → 规则正文
# --------------------------------------------------------------------------- #

class SceneResolver:
    def __init__(self, conn: sqlite3.Connection, novel_id: int,
                 meta: list[dict]) -> None:
        self.conn = conn
        self.novel_id = novel_id
        self.meta_by_range = {str(m.get("chapter_range")): m for m in meta or []}
        self.ranges = []
        for m in meta or []:
            rng = str(m.get("chapter_range") or "")
            m2 = re.match(r"^(\d+)\s*-\s*(\d+)$", rng)
            if m2:
                self.ranges.append((int(m2.group(1)), int(m2.group(2)), m))
        self._chunk: dict[int, sqlite3.Row] = {}
        self._chapter_chunks: dict[int, list[sqlite3.Row]] = {}
        self._ctx_scene: dict[int, str | None] = {}
        self._chapter_scene: dict[int, str | None] = {}

    def meta_for(self, item: dict) -> dict | None:
        rng = item.get("chapter_range")
        if rng and str(rng) in self.meta_by_range:
            return self.meta_by_range[str(rng)]
        ch = item.get("chapter_no")
        if isinstance(ch, int):
            for lo, hi, meta in self.ranges:
                if lo <= ch <= hi:
                    return meta
        return None

    def chunk(self, chunk_id: int | None) -> sqlite3.Row | None:
        if not chunk_id:
            return None
        cid = int(chunk_id)
        if cid not in self._chunk:
            self._chunk[cid] = self.conn.execute(
                "select id, chapter_no, idx, text from novel_chunks where id=?",
                (cid,),
            ).fetchone()
        return self._chunk[cid]

    def chapter_chunks(self, chapter_no: int) -> list[sqlite3.Row]:
        ch = int(chapter_no)
        if ch not in self._chapter_chunks:
            self._chapter_chunks[ch] = self.conn.execute(
                "select id, chapter_no, idx, text from novel_chunks "
                "where novel_id=? and chapter_no=? order by idx, id",
                (self.novel_id, ch),
            ).fetchall()
        return self._chapter_chunks[ch]

    def _context_scene_for_chunk(self, chunk: sqlite3.Row) -> str | None:
        cid = int(chunk["id"])
        if cid in self._ctx_scene:
            return self._ctx_scene[cid]
        rows = self.chapter_chunks(int(chunk["chapter_no"]))
        pos = next((i for i, r in enumerate(rows) if int(r["id"]) == cid), None)
        if pos is None:
            scene = None
        else:
            text = "".join(str(r["text"] or "") for r in rows[max(0, pos - 1):pos + 2])
            scene = SCENE_MATCHER.best(text)
        self._ctx_scene[cid] = scene
        return scene

    def _chapter_scene_for(self, chapter_no: int) -> str | None:
        ch = int(chapter_no)
        if ch in self._chapter_scene:
            return self._chapter_scene[ch]
        rows = self.chapter_chunks(ch)
        best_scene, best_score = None, 0
        for i in range(len(rows)):
            text = "".join(str(r["text"] or "") for r in rows[i:i + 3])
            scores = SCENE_MATCHER.scores(text)
            if not scores:
                continue
            scene, n = scores.most_common(1)[0]
            if n > best_score:
                best_scene, best_score = scene, n
        self._chapter_scene[ch] = best_scene
        return best_scene

    def scene_for(self, item: dict) -> tuple[str | None, str]:
        chunk = self.chunk(item.get("chunk_id"))
        if chunk is not None:
            scene = self._context_scene_for_chunk(chunk)
            if scene:
                return scene, "context"
            chapter_no = item.get("evidence_chapter_no") or chunk["chapter_no"]
            scene = self._chapter_scene_for(int(chapter_no)) if chapter_no else None
            if scene:
                return scene, "chapter"
        else:
            chapter_no = item.get("evidence_chapter_no")
            if chapter_no:
                scene = self._chapter_scene_for(int(chapter_no))
                if scene:
                    return scene, "chapter"
        scene = SCENE_MATCHER.best(str(item.get("rule") or ""))
        if scene:
            return scene, "rule_text"
        return None, "none"


# --------------------------------------------------------------------------- #
# 摘要 / 时间线
# --------------------------------------------------------------------------- #

def _names_for(meta: dict | None) -> list[str]:
    if not meta:
        return []
    out: list[str] = []
    for ch in meta.get("characters") or []:
        if not isinstance(ch, dict):
            continue
        out.append(str(ch.get("name") or ""))
        out.extend(str(a) for a in (ch.get("aliases") or []))
    return sorted({n for n in out if len(n) >= 2}, key=len, reverse=True)


def make_summary(text: str, names: list[str]) -> str:
    """按 §3.2 抽 30 字摘要：分句边界截断、不补写、去同书专名后仍可核对。"""
    body = strip_beat_prefix(text)
    body = EVIDENCE_RE.sub("", body).strip()
    body = body.rstrip("）) ")
    first = re.split(r"[，。；：！？]", body, maxsplit=1)[0].strip()
    if not first:
        return ""
    body_no_names = body
    candidate = first
    for name in names:
        body_no_names = body_no_names.replace(name, "")
        candidate = candidate.replace(name, "")
    candidate = re.sub(r"\s+", "", candidate).strip("，。；：！？（）()“”\"' ")
    if candidate and candidate in body_no_names:
        summary = candidate
    else:
        summary = first
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS]
    return summary.strip()


def make_timeline(meta: dict | None, chapter_no: int | None) -> tuple[str | None, dict | None]:
    """从块摘要里截 40–60 字的「何时/何地/发生了什么」，全部为原文子串。"""
    if not meta:
        return None, None
    summary = str(meta.get("summary") or "").strip()
    if not summary:
        return None, None
    body = summary
    desc = ""
    for sentence in re.split(r"[。！？]", body):
        sentence = sentence.strip()
        if not sentence:
            continue
        desc = sentence if not desc else desc + "。" + sentence
        if len(desc) >= 40:
            break
    if len(desc) > 60:
        desc = desc[:60]
    desc = desc.strip("。")
    if not desc:
        return None, None
    timeline_src = {
        "chapter_no": chapter_no,
        "chapter_range": meta.get("chapter_range"),
        "quote": summary,
    }
    return desc, timeline_src


def _flag_reasons(flags: int, flag_reasons: tuple[tuple[int, str], ...]) -> list[str]:
    return [name for bit, name in flag_reasons if flags & bit]


def derive_l4_lexicon(profile: dict, rules: dict) -> tuple[dict, dict]:
    """按本书蒸馏语料重建 L4 词表（换书时自动跟着换）。

    只取会挂 L4 的维度（technique / global / structural）的规则正文；角色名/
    别名从档案 chunk_meta 里汇总后排除。词表口径见 `retrieval/l4_lexicon.py`。
    """
    from app.retrieval.l4_lexicon import derive_lexicon

    names: set[str] = set()
    for meta in profile.get("chunk_meta") or []:
        names.update(_names_for(meta))

    texts: list[str] = []
    seen: set[str] = set()
    for dim, items in (rules or {}).items():
        if dim in SKIP_DIMS or shape_for(dim, l1_for_dim(dim)) is None:
            continue
        for item in items or []:
            text = str(item.get("rule") or "")
            if text and text not in seen:
                seen.add(text)
                texts.append(text)

    lexicon, report = derive_lexicon(texts, exclude=names)
    report["corpus_dimensions"] = sorted(
        dim for dim in (rules or {})
        if dim not in SKIP_DIMS and shape_for(dim, l1_for_dim(dim)) is not None
    )
    return lexicon, report


# --------------------------------------------------------------------------- #
# 建卡
# --------------------------------------------------------------------------- #

def build_cards(*, distill_id: int, db_path: Path) -> dict:
    started = time.perf_counter()
    conn = _connect(Path(db_path))
    try:
        profile, source_ids, raw_profile = _load_profile(conn, distill_id)
        if not source_ids:
            raise SystemExit(f"distill_id={distill_id} 没有 source_ids，无法确定 novel_id")
        novel_id = int(source_ids[0])
        control = _load_control(conn, distill_id, source_ids)
        resolver = SceneResolver(conn, novel_id, profile.get("chunk_meta") or [])

        # 延迟 import：narrative.py 体积大，只有建卡才需要。
        from app.style import narrative as N

        score_cache = N.build_rule_score_cache(profile, control)
        scores_map: dict[str, list[int]] = score_cache.get("scores") or {}
        notes: dict[str, str] = score_cache.get("notes") or {}
        audit = N._parse_audit_metrics(profile)
        weights = N.DEFAULT_RULE_SCORING.weights

        rules: dict[str, list] = profile.get("rules") or {}
        archive_rule_count = sum(len(v or []) for v in rules.values())
        built_at = datetime.now(timezone.utc).isoformat()

        # L4 词表按本书语料重建：换书后自动适配，不再依赖手写词表覆盖。
        from app.retrieval.l4_lexicon import lexicon_hash

        l4_lexicon, l4_report = derive_l4_lexicon(profile, rules)
        l4_lexicon_hash = lexicon_hash(l4_lexicon)
        # 旧词（本书 live 子集）计 2 分、派生新词计 1 分：新增词只补覆盖，
        # 不抢原本正确的主技法。权重口径与自训练标签保持一致。
        l4_seed = {c: tuple(v) for c, v in l4_report["seed_terms"].items()}

        cards: list[dict] = []
        discarded: dict[str, dict] = {}
        seen: dict[str, set[str]] = defaultdict(set)
        by_l1: Counter[str] = Counter()
        by_shape: Counter[str] = Counter()
        by_dim: Counter[str] = Counter()
        depth_dist: Counter[str] = Counter()
        level_counts: Counter[str] = Counter()
        l2_unassigned_shape: Counter[str] = Counter()
        l4_unassigned_shape: Counter[str] = Counter()
        scene_src: Counter[str] = Counter()
        hard_reasons: Counter[str] = Counter()
        hard_by_dim: Counter[str] = Counter()
        skipped: Counter[str] = Counter()
        duplicates = 0
        deprecated_by_dim: Counter[str] = Counter()
        l2_unassigned = l3_unassigned = l4_unassigned = 0

        for dim, items in rules.items():
            items = items or []
            if dim in SKIP_DIMS:
                skipped[dim] += len(items)
                continue
            l1 = l1_for_dim(dim)
            shape = shape_for(dim, l1)
            deciles = (
                N._cluster_deciles(items, dim, N.DEFAULT_RULE_SCORING)
                if dim == "角色声音词典" else {}
            )
            for pos, item in enumerate(items):
                text = str(item.get("rule") or "")
                if not text:
                    skipped[dim] += 1
                    continue
                key = card_id(dim, text)
                if key in seen[dim]:
                    duplicates += 1
                    continue
                compact = scores_map.get(N._rule_score_key(item, dim))
                if compact is None:
                    scored = N._score_rule(
                        item, dim,
                        deciles=deciles.get(pos, 1),
                        audit=audit,
                        indexes=None,
                        config=N.DEFAULT_RULE_SCORING,
                    )
                    compact = [
                        int(scored["execution"] * 100),
                        int(scored["distinctiveness"] * 100),
                        int(scored["evidence"] * 100),
                        int(scored["verifiability"] * 100),
                        int(scored["flags"]),
                        int(scored["deciles"]),
                    ]
                    if scored.get("note"):
                        notes[key] = scored["note"]
                flags = int(compact[4])
                reasons = _flag_reasons(flags, N._FLAG_REASONS)
                # 2026-11-13 裁决：量化审计不豁免，整族走硬过滤；
                # 它仍由 `_render_audit_block` 固定注入，不依赖卡库。
                if flags & N._HARD_FILTER_MASK:
                    hard_by_dim[dim] += 1
                    hard_reasons.update(reasons)
                    discarded[key] = {
                        "id": key,
                        "dim": dim,
                        "text": text,
                        "evidence": N._rule_evidence(item),
                        "chapter_no": item.get("chapter_no"),
                        "chunk_id": item.get("chunk_id"),
                        "evidence_chapter_no": item.get("evidence_chapter_no"),
                        "flags": flags,
                        "reasons": reasons,
                    }
                    continue

                # ---- 路径 ----
                l2 = l3 = l4 = None
                src = "none"
                tags: list[str] = []
                if shape:
                    l2, src = resolver.scene_for(item)
                    if shape == "structural":
                        if l2:
                            l3 = beat_key(text)
                            if l3:
                                l4, tags, _ = assign_l4(
                                    text, lexicon=l4_lexicon, seed_lexicon=l4_seed)
                        # 没有 L2 时不再往下挂 L3/L4：路径语法不允许跳过上级。
                    elif shape == "technique":
                        l4, tags, _ = assign_l4(
                            text, lexicon=l4_lexicon, seed_lexicon=l4_seed)
                    else:  # global
                        l2 = None
                        l4, tags, _ = assign_l4(
                            text, lexicon=l4_lexicon, seed_lexicon=l4_seed)
                path = join_path(l1, l2, l3, l4)
                depth = path_depth(path)
                by_l1[l1] += 1
                by_shape[shape or "none"] += 1
                by_dim[dim] += 1
                # 逻辑深度按「实际挂到哪一级」算，不按 path 字符串段数：
                # technique 的 `style/<l4>` 是 L4，不是 L2。
                deepest = "L4" if l4 else "L3" if l3 else "L2" if l2 else "L1"
                depth_dist[deepest] += 1
                level_counts["L1"] += 1
                if l2:
                    level_counts["L2"] += 1
                if l3:
                    level_counts["L3"] += 1
                if l4:
                    level_counts["L4"] += 1
                scene_src[src] += 1
                # 只有 structural / technique 有 L2；global 天生跨场景，
                # 把它的 6,937 条算成「未挂场景」会让统计翻几倍。
                if shape in ("structural", "technique") and not l2:
                    l2_unassigned += 1
                    l2_unassigned_shape[shape] += 1
                if shape == "structural" and l2 and not l3:
                    l3_unassigned += 1
                if shape in ("technique", "global") and not l4:
                    l4_unassigned += 1
                    l4_unassigned_shape[shape] += 1
                if shape == "structural" and l3 and not l4:
                    l4_unassigned += 1
                    l4_unassigned_shape[shape] += 1

                deprecated = dim in DEPRECATED_DIMS
                if deprecated:
                    deprecated_by_dim[dim] += 1

                meta = resolver.meta_for(item)
                summary = make_summary(text, _names_for(meta))
                timeline_desc, timeline_src = make_timeline(meta, item.get("chapter_no"))
                # 规则原文常以「（证据：……）」收尾，`_rule_evidence` 会留下右括号；
                # 去掉尾部标点，避免证据句与卡面正文对比时多一个字符。
                evidence = (N._rule_evidence(item) or "").rstrip("）) ").strip()
                if not evidence and "证据：" in text:
                    evidence = text.split("证据：", 1)[1].strip().rstrip("）) ").strip()

                total = (
                    weights[0] * compact[0]
                    + weights[1] * compact[1]
                    + weights[2] * compact[2]
                    + weights[3] * compact[3]
                ) / 100.0
                card = {
                    "id": key,
                    "distill_id": distill_id,
                    "novel_id": novel_id,
                    "dim": dim,
                    "shape": shape,
                    "path": path,
                    "l1": l1,
                    "l2": l2,
                    "l3": l3,
                    "l4": l4,
                    "depth": depth,
                    "technique_tags": tags,
                    "scene_tags": list(dict.fromkeys(
                        ([l2] if l2 else [])
                        + [str(s) for s in (meta or {}).get("secondary_scenes") or []]
                    ))[:3],
                    "chapter_no": item.get("chapter_no"),
                    "chapter_range": item.get("chapter_range"),
                    "evidence_chapter_no": item.get("evidence_chapter_no"),
                    "chunk_id": item.get("chunk_id"),
                    "summary": summary,
                    "text": text,
                    "evidence": evidence,
                    "has_quote_evidence": bool(QUOTE_RE.search(text)),
                    "timeline_desc": timeline_desc,
                    "timeline_src": timeline_src,
                    "scores": {
                        "execution": compact[0] / 100.0,
                        "distinctiveness": compact[1] / 100.0,
                        "evidence": compact[2] / 100.0,
                        "verifiability": compact[3] / 100.0,
                        "total": round(total, 3),
                    },
                    "flags": flags,
                    "flag_reasons": reasons,
                    "deciles": compact[5],
                    "verify_note": notes.get(key),
                    "deprecated": deprecated,
                    "deprecated_reason": (
                        "设计稿 §8.2：逐章剧情摘要，检索默认不返回，rule 可回查"
                        if deprecated else None
                    ),
                    "anchor": {
                        "chapter_no": item.get("chapter_no"),
                        "evidence_chapter_no": item.get("evidence_chapter_no"),
                        "chunk_id": item.get("chunk_id"),
                        "char_start": None,
                        "char_end": None,
                        "excerpt": evidence,
                        "method": "lazy",
                        "anchor_missing": False,
                        "located": False,
                    },
                    "topic_ids": [],
                    "created_at": built_at,
                }
                seen[dim].add(key)
                cards.append(card)

        path_counts = Counter(c["path"] for c in cards)
        worst_path, worst_count = ("", 0)
        if path_counts:
            worst_path, worst_count = max(path_counts.items(), key=lambda kv: kv[1])

        catalog_version = _catalog_version(
            distill_id=distill_id,
            source_ids=source_ids,
            profile=profile,
            raw_profile=raw_profile,
            l4_lexicon=l4_lexicon,
            l4_seed=l4_seed,
            l4_seed_weight=l4_report["params"]["seed_weight"],
        )
        stats = {
            "archive_rule_count": archive_rule_count,
            "card_count": len(cards),
            "discarded_count": len(discarded),
            "skipped_dimensions": dict(skipped),
            "by_l1": dict(by_l1),
            "by_shape": dict(by_shape),
            "by_dim": dict(by_dim),
            "depth_distribution": dict(depth_dist),
            "level_card_counts": dict(level_counts),
            "path_count": len(path_counts),
            "worst_path": {"path": worst_path, "count": worst_count},
            "l1_only_cards": depth_dist.get("L1", 0),
            "empty_path_count": 0,
            "scene_src": dict(scene_src),
            "l2_unassigned": l2_unassigned,
            "l2_unassigned_by_shape": dict(l2_unassigned_shape),
            "l3_unassigned": l3_unassigned,
            "l4_unassigned": l4_unassigned,
            "l4_unassigned_by_shape": dict(l4_unassigned_shape),
            "hard_filter_reasons": dict(hard_reasons),
            "hard_filtered_by_dim": dict(hard_by_dim),
            "deprecated_count": sum(deprecated_by_dim.values()),
            "deprecated_by_dim": dict(deprecated_by_dim),
            "active_card_count": len(cards) - sum(deprecated_by_dim.values()),
            "duplicates_removed": duplicates,
            "card_text_truncated": 0,
            "summary_max_chars": SUMMARY_MAX_CHARS,
            "max_card_chars": max((len(c["text"]) for c in cards), default=0),
            "catalog_version": catalog_version,
            "path_schema_version": PATH_SCHEMA_VERSION,
            "score_version": getattr(N, "RULE_SCORE_VERSION", None),
            "l4_lexicon_version": l4_report["version"],
            "l4_lexicon_hash": l4_lexicon_hash,
            "l4_lexicon_tokenizer": l4_report["tokenizer"],
            "l4_lexicon_corpus_texts": l4_report["texts"],
            "l4_lexicon_labeled": l4_report["labeled"],
            "l4_lexicon_terms": l4_report["terms"],
            "l4_lexicon_seed_live": l4_report["seed_live"],
            "l4_lexicon_seed_weight": l4_report["params"]["seed_weight"],
            "l4_lexicon_added": l4_report["added"],
            "build_seconds": round(time.perf_counter() - started, 2),
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "catalog_version": catalog_version,
            "distill_id": distill_id,
            "novel_id": novel_id,
            "source_ids": source_ids,
            "built_at": built_at,
            "path_schema_version": PATH_SCHEMA_VERSION,
            "stats": stats,
            "l4_lexicon": {c: list(v) for c, v in l4_lexicon.items()},
            "l4_lexicon_seed": {c: list(v) for c, v in l4_seed.items()},
            "cards": cards,
            "discarded": discarded,
        }
        out = save_catalog(payload)
        versioned = catalog_path(distill_id, versioned=True, catalog_version=catalog_version)
        stats["written_files"] = [str(out), str(versioned)]
        stats["file_bytes"] = {"latest": out.stat().st_size, "versioned": versioned.stat().st_size}
        return stats
    finally:
        conn.close()


def _catalog_version(*, distill_id: int, source_ids: list[int], profile: dict,
                     raw_profile: str,
                     l4_lexicon: dict | None = None,
                     l4_seed: dict | None = None,
                     l4_seed_weight: int = 2) -> str:
    h = hashlib.sha1()
    h.update(str(distill_id).encode())
    h.update(str(source_ids).encode())
    h.update(str(profile.get("fingerprint") or raw_profile[:2000]).encode())
    h.update(PATH_SCHEMA_VERSION.encode())
    h.update(SCHEMA_VERSION.encode())
    try:
        from app.style.narrative import RULE_SCORE_VERSION
        h.update(str(RULE_SCORE_VERSION).encode())
    except Exception:  # noqa: BLE001 - 版本号只用于缓存，不阻断建卡
        pass
    from app.retrieval.catalog_paths import L4_LEXICON, L3_LABELS, L2_LABELS
    l4 = l4_lexicon or L4_LEXICON
    h.update(json.dumps({
        "l2": sorted(L2_LABELS),
        "l3": sorted(L3_LABELS),
        "l4": {k: list(v) for k, v in sorted(l4.items())},
        # 旧词集合与权重同样决定分类结果，必须纳入缓存键。
        "l4_seed": {k: list(v) for k, v in sorted((l4_seed or {}).items())},
        "l4_seed_weight": l4_seed_weight,
    }, ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _default_db() -> Path:
    return Path(__file__).resolve().parents[2] / "novel_dev.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建 L1–L5 卡片索引")
    parser.add_argument("--distill-id", type=int, default=4)
    parser.add_argument("--db", type=Path, default=_default_db())
    parser.add_argument(
        "--lexicon-only", action="store_true",
        help="只推导并打印 L4 词表与报告，不写卡表（换书后先核对词表）",
    )
    args = parser.parse_args(argv)
    if args.lexicon_only:
        from app.retrieval.l4_lexicon import lexicon_added, lexicon_hash

        conn = _connect(Path(args.db))
        try:
            profile, source_ids, _ = _load_profile(conn, args.distill_id)
        finally:
            conn.close()
        lexicon, report = derive_l4_lexicon(profile, profile.get("rules") or {})
        print(json.dumps({
            "distill_id": args.distill_id,
            "source_ids": source_ids,
            "hash": lexicon_hash(lexicon),
            "lexicon": {c: list(v) for c, v in lexicon.items()},
            "added": lexicon_added(lexicon),
            "report": report,
        }, ensure_ascii=False, indent=2))
        return 0
    stats = build_cards(distill_id=args.distill_id, db_path=args.db)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
