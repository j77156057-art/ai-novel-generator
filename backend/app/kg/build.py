"""知识图谱构建服务：从小说分片抽取并写入 kg_nodes / kg_edges。

图谱按「来源(source)」隔离，支持多张图共存于同一 project：
- source=f"novel:{id}"  → 某本参考语料的独立图谱（用户选多本时各自成图，不混）
- source="generated"    → 用户**生成后**自己小说的图谱
- source="all"          → 旧版「汇总库内全部语料合并一张图」（保留兼容，新流程不再使用）

每张图按 source 幂等重建（只清该 source 的旧图再写新图），互不污染。
"""

from sqlalchemy import select

from app.kg.extractor import RuleBasedExtractor, resolve_plotholes
from app.models import KgEdge, KgNode, NovelChunk, RawNovel


def _clear_graph_by_source(db, project_id: int, source: str) -> None:
    """只清该 source 下的旧图（节点 + 相关边），不影响其它来源的图。"""
    old = db.scalars(
        select(KgNode).where(KgNode.project_id == project_id, KgNode.source == source)
    ).all()
    ids = [n.id for n in old]
    if ids:
        db.query(KgEdge).filter(
            (KgEdge.from_id.in_(ids)) | (KgEdge.to_id.in_(ids))
        ).delete()
        db.query(KgNode).filter(KgNode.id.in_(ids)).delete()
        db.flush()


def _extract_from_chunks(chunks: list) -> object:
    result = RuleBasedExtractor().extract(chunks)
    resolve_plotholes(result, chunks)
    return result


def _write_result(db, project_id: int, result, source: str) -> dict:
    name_to_node: dict[str, int] = {}
    n_char = n_hole = n_rel = 0

    for name, c in result.characters.items():
        node = KgNode(
            project_id=project_id,
            type="character",
            props={"name": name, "mentions": c.mentions, "speaking": c.speaking},
            first_chapter=min(c.chapters) if c.chapters else None,
            last_chapter=max(c.chapters) if c.chapters else None,
            source=source,
        )
        db.add(node)
        db.flush()
        name_to_node[name] = node.id
        n_char += 1

    for ph in result.plotholes.values():
        node = KgNode(
            project_id=project_id,
            type="plothole",
            props={"cue": ph.cue, "question": ph.question, "status": ph.status},
            first_chapter=ph.first_chapter,
            last_chapter=ph.last_chapter,
            source=source,
        )
        db.add(node)
        n_hole += 1

    for (a, b), edge in result.relationships.items():
        aid, bid = name_to_node.get(a), name_to_node.get(b)
        if aid is None or bid is None:
            continue
        db.add(
            KgEdge(
                from_id=aid,
                to_id=bid,
                type="relationship",
                props={"relation": edge.relation, "chapters": sorted(edge.chapters)},
            )
        )
        n_rel += 1

    db.commit()
    return {
        "project_id": project_id,
        "source": source,
        "characters": n_char,
        "plotholes": n_hole,
        "relationships": n_rel,
    }


def build_graph(
    db,
    project_id: int,
    novel_id: int | None = None,
    all_corpora: bool = False,
) -> dict:
    """构建参考语料图谱。

    - all_corpora=True：汇总库内全部语料合并为 source="all" 一张图（旧版）。
    - novel_id 给定：只建该本语料的图，source=f"novel:{novel_id}"（新流程：多本各自成图）。
    - 都不给：取首本（旧版默认）。
    """
    if all_corpora:
        chunks = db.scalars(select(NovelChunk).order_by(NovelChunk.novel_id, NovelChunk.id)).all()
        if not chunks:
            return {"error": "库内无分片，请先导入语料"}
        source = "all"
    elif novel_id is not None:
        chunks = db.scalars(
            select(NovelChunk).where(NovelChunk.novel_id == novel_id)
        ).all()
        if not chunks:
            return {"error": f"novel {novel_id} 无分片"}
        source = f"novel:{novel_id}"
    else:
        novel = db.scalars(select(RawNovel).order_by(RawNovel.id)).first()
        if novel is None:
            return {"error": "无可用 novel，请先上传/种子语料"}
        novel_id = novel.id
        chunks = db.scalars(
            select(NovelChunk).where(NovelChunk.novel_id == novel_id)
        ).all()
        source = f"novel:{novel_id}"

    result = _extract_from_chunks(chunks)
    _clear_graph_by_source(db, project_id, source)
    stats = _write_result(db, project_id, result, source)
    stats["novel_id"] = novel_id
    stats["chunks"] = len(chunks)
    return stats


class _TextChunk:
    """把一段纯文本包装成抽取器可消费的 chunk 形状。"""

    __slots__ = ("text", "chapter_no", "idx", "novel_id")

    def __init__(self, text: str, chapter_no: int | None, idx: int, novel_id=None) -> None:
        self.text = text
        self.chapter_no = chapter_no
        self.idx = idx
        self.novel_id = novel_id


def _split_text_to_chunks(text: str, chunk_chars: int = 1500) -> list[_TextChunk]:
    """把生成文本按长度切成带伪章节号的 chunk，供规则抽取。"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[_TextChunk] = []
    buf = ""
    idx = 0
    chapter = 1
    for p in paragraphs:
        if buf and len(buf) + len(p) > chunk_chars:
            chunks.append(_TextChunk(buf, chapter, idx))
            idx += 1
            buf = ""
            chapter += 1
        buf = (buf + "\n" + p) if buf else p
    if buf:
        chunks.append(_TextChunk(buf, chapter, idx))
    return chunks


def build_graph_from_text(
    db, project_id: int, text: str, source: str = "generated"
) -> dict:
    """从**生成的小说文本**建图谱（生成后才有的知识图谱）。

    注意：这是「手动全量重建」（先清该 source 再重写）。**每段生成后的自动更新**应调用
    merge_graph_from_text()——它做增量 upsert，不清空已有事实。
    """
    if not text or not text.strip():
        return {"error": "无生成文本可建图"}
    chunks = _split_text_to_chunks(text)
    result = _extract_from_chunks(chunks)
    _clear_graph_by_source(db, project_id, source)
    stats = _write_result(db, project_id, result, source)
    stats["chunks"] = len(chunks)
    return stats


def extract_from_text(text: str, segment_no: int = 1):
    """把「一段文本」切成 chunk 并跑规则抽取，返回 (ExtractionResult, chunks)。

    抽出来做公共入口，是为了让**图谱写入与账本写入共用同一次抽取**：
    两者吃的是同一份 ExtractionResult，跑两遍纯属浪费（真实语料单次抽取约 16s，
    虽然单段文本很小，但这是不该存在的重复计算，也会让两条链路的结果漂移）。
    """
    chunks = _split_text_to_chunks(text)
    for c in chunks:
        c.chapter_no = segment_no
    return _extract_from_chunks(chunks), chunks


def merge_graph_from_text(
    db, project_id: int, text: str, source: str = "generated", segment_no: int = 1
) -> dict:
    """增量合并：把「新生成的一段」抽取的事实并入已有图谱，**不清空、不重建**。

    这是图谱进入生成闭环的关键：旧实现中 build_graph_from_text 只在用户手动点「建图」
    时调用，且每次清空重建 —— 生成过程里图谱既不增量更新、也不被下一次生成可靠使用。
    """
    if not text or not text.strip():
        return {"merged": False, "reason": "空文本"}
    result, _ = extract_from_text(text, segment_no)
    return merge_result_into_graph(db, project_id, result, source, segment_no)


def merge_result_into_graph(
    db, project_id: int, result, source: str = "generated", segment_no: int = 1,
    *, commit: bool = True,
) -> dict:
    """把已抽取好的结果写进图谱（增量 upsert，不清空）。

    upsert 语义：
    - 角色：同名已存在 → 累加 mentions/speaking、扩展章节范围；不存在 → 新增
    - 坑：同一 question 已存在 → 跳过（避免每段重复登记）；不存在 → 新增
    - 关系：同 (from,to,relation) 已存在 → 跳过；否则新增
    章节号统一取 segment_no（一段 = 一章），保证 last_chapter 单调递增不回退。

    `commit=False` 时只 flush——供重建原语把整段重建收进一个事务。
    """
    existing = db.scalars(
        select(KgNode).where(KgNode.project_id == project_id, KgNode.source == source)
    ).all()
    char_by_name: dict[str, KgNode] = {}
    hole_by_q: dict[str, KgNode] = {}
    for n in existing:
        props = n.props or {}
        if n.type == "character" and props.get("name"):
            char_by_name[props["name"]] = n
        elif n.type == "plothole" and props.get("question"):
            hole_by_q[props["question"]] = n

    n_new_char = n_upd_char = n_new_hole = n_new_rel = 0
    name_to_node: dict[str, int] = {}

    for name, c in result.characters.items():
        node = char_by_name.get(name)
        if node is None:
            node = KgNode(
                project_id=project_id,
                type="character",
                props={"name": name, "mentions": c.mentions, "speaking": c.speaking},
                first_chapter=segment_no,
                last_chapter=segment_no,
                source=source,
            )
            db.add(node)
            db.flush()
            n_new_char += 1
        else:
            props = dict(node.props or {})
            props["mentions"] = (props.get("mentions") or 0) + c.mentions
            props["speaking"] = (props.get("speaking") or 0) + c.speaking
            node.props = props
            node.first_chapter = min(
                [x for x in (node.first_chapter, segment_no) if x is not None] or [segment_no]
            )
            node.last_chapter = max(
                [x for x in (node.last_chapter, segment_no) if x is not None] or [segment_no]
            )
            n_upd_char += 1
        name_to_node[name] = node.id

    for ph in result.plotholes.values():
        if ph.question in hole_by_q:
            continue
        db.add(
            KgNode(
                project_id=project_id,
                type="plothole",
                props={"cue": ph.cue, "question": ph.question, "status": ph.status},
                first_chapter=segment_no,
                last_chapter=segment_no,
                source=source,
            )
        )
        n_new_hole += 1

    # 关系去重：同 (from,to,relation) 已存在则跳过
    node_ids = [n.id for n in existing] + list(name_to_node.values())
    existing_edges = set()
    if node_ids:
        rows = db.query(KgEdge).filter(
            (KgEdge.from_id.in_(node_ids)) | (KgEdge.to_id.in_(node_ids))
        ).all()
        for e in rows:
            existing_edges.add((e.from_id, e.to_id, (e.props or {}).get("relation")))

    for (a, b), edge in result.relationships.items():
        aid, bid = name_to_node.get(a), name_to_node.get(b)
        if aid is None or bid is None:
            continue
        if (aid, bid, edge.relation) in existing_edges:
            continue
        db.add(
            KgEdge(
                from_id=aid,
                to_id=bid,
                type="relationship",
                props={"relation": edge.relation, "chapters": sorted(edge.chapters)},
            )
        )
        n_new_rel += 1

    if commit:
        db.commit()
    else:
        db.flush()
    return {
        "merged": True,
        "project_id": project_id,
        "source": source,
        "segment_no": segment_no,
        "new_characters": n_new_char,
        "updated_characters": n_upd_char,
        "new_plotholes": n_new_hole,
        "new_relationships": n_new_rel,
    }


def _segment_pairs(segments) -> list[tuple[int, str]]:
    """校验正文段列表形状：必须是 `(int 段号, str 正文)` 的序列。"""
    if segments is None or not isinstance(segments, (list, tuple)):
        raise ValueError("segments_required")
    pairs: list[tuple[int, str]] = []
    for item in segments:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("invalid_segments")
        segment_no, text = item
        if isinstance(segment_no, bool) or not isinstance(segment_no, int):
            raise ValueError("invalid_segments")
        if not isinstance(text, str):
            raise ValueError("invalid_segments")
        pairs.append((segment_no, text))
    return pairs


def rebuild_generated_graph(db, project_id: int, segments, *, commit: bool = True) -> dict:
    """按当前正文全量重建生成侧知识图谱（source='generated'）。

    `segments`：当前生成分支的正文段列表 `[(segment_no, text), ...]`。

    与 `build_graph_from_text` 的区别：那条路把拼接正文按字数重新切块，
    章节号是伪章节号（1,2,3…），**段号信息全丢**；这里按段逐段抽取，
    章节号就是段号，与生成期的增量路径（`extract_from_text` +
    `merge_result_into_graph`）完全同口径。

    只清 `source='generated'` 的图，参考语料图（`novel:{id}`）不受影响。

    `commit=False` 时只 flush——供 `derived_rebuild` 把重建收进同一个事务。
    """
    pairs = _segment_pairs(segments)
    _clear_graph_by_source(db, project_id, "generated")
    for segment_no, text in pairs:
        if not text.strip():
            continue
        result, _ = extract_from_text(text, segment_no)
        merge_result_into_graph(
            db, project_id, result, "generated", segment_no, commit=False
        )
    if commit:
        db.commit()
    else:
        db.flush()

    nodes = db.scalars(
        select(KgNode).where(KgNode.project_id == project_id, KgNode.source == "generated")
    ).all()
    node_ids = [n.id for n in nodes]
    edges = (
        db.scalars(
            select(KgEdge).where(
                (KgEdge.from_id.in_(node_ids)) & (KgEdge.to_id.in_(node_ids))
            )
        ).all()
        if node_ids
        else []
    )
    return {
        "project_id": project_id,
        "source": "generated",
        "characters": sum(1 for n in nodes if n.type == "character"),
        "plotholes": sum(1 for n in nodes if n.type == "plothole"),
        "relationships": len(edges),
        "segments": len(pairs),
    }
