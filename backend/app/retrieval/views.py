"""Auditable consumption views. Raw text is metadata, never a prompt body."""
from app.ingestion.clean import clean_consumption_text


def chunk_view(chunk, **fields):
    """Original raw text exists only in view metadata; removed spans are auditable."""
    text, report = clean_consumption_text(chunk.text or "")
    source = {"novel_id": chunk.novel_id, "chunk_id": chunk.id,
              "chapter_no": chunk.chapter_no, "idx": chunk.idx,
              "source": "reference", "clean_report": report}
    return {**source, "text": text, "raw_text": chunk.raw_text if chunk.raw_text is not None else chunk.text,
            "sources": [dict(source)], **fields}


def prepare_hits(hits):
    """Position order is chronological; other roles keep retrieval rank."""
    positions = sorted((h for h in hits if h.get("role") == "position"),
                       key=lambda h: (h.get("novel_id") or 0, h.get("chapter_no") or 0, h.get("idx") or 0))
    ordered = positions + [h for h in hits if h.get("role") != "position"]
    out, seen = [], {}
    for h in ordered:
        item = dict(h)
        item["text"], report = clean_consumption_text(item.get("text") or "")
        item.setdefault("clean_report", report)
        item["sources"] = list(item.get("sources") or [{k: item.get(k) for k in
            ("novel_id", "chunk_id", "chapter_no", "idx", "source", "clean_report")}])
        key = (item.get("role"), item["text"])
        if key in seen:
            seen[key]["sources"].extend(item["sources"])
        else:
            seen[key] = item
            out.append(item)
    return out


def stitch_chunks(rows):
    """Trim only substantial exact adjacent same-chapter suffix/prefix evidence.

    Full duplicate chunks and short repeats remain: they may be intentional.
    Returns text plus evidence, including ambiguous overlaps retained verbatim.
    """
    parts, reports, previous = [], [], None
    for row in rows:
        raw = row.text or ""
        trim = 0
        if (previous is not None and row.novel_id == previous.novel_id
                and row.chapter_no is not None and row.chapter_no == previous.chapter_no
                and row.idx is not None and previous.idx is not None and row.idx == previous.idx + 1):
            left = previous.text or ""
            overlap = next((n for n in range(min(len(left), len(raw)), 0, -1)
                            if left.endswith(raw[:n])), 0)
            if overlap:
                safe = overlap >= 32 and overlap < min(len(left), len(raw)) and len(set(raw[:overlap])) >= 8
                trim = overlap if safe else 0
                reports.append({"left_chunk_id": previous.id, "right_chunk_id": row.id,
                                "overlap_chars": overlap, "trimmed_chars": trim,
                                "status": "exact_adjacent_overlap" if safe else "ambiguous_preserved"})
        parts.append(raw[trim:])
        previous = row
    cleaned, report = clean_consumption_text("".join(parts))
    return cleaned, {"overlaps": reports, "clean_report": report}
