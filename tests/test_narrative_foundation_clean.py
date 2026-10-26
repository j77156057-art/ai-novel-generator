from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import NovelChunk, Project, RawNovel


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _dirty():
    return "一秒记住【站点.info】\n为您提供精彩小说阅读\n更新时间：2026-01-01\n句中无限大冒险全文阅读\n封禁之神小说5200\n正文仍然保留。"


def test_shared_consumption_cleaning():
    from app.ingestion.clean import clean_consumption_text
    cleaned, record = clean_consumption_text(_dirty())
    assert record["version"] >= 1
    assert "一秒记住" not in cleaned and "封禁之神小说5200" not in cleaned
    assert "正文仍然保留" in cleaned
    legal = "他重复说了三遍，为您提供精彩小说阅读是故事中的台词。"
    assert clean_consumption_text(legal)[0] == legal


def test_search_returns_clean_view():
    s = _db(); s.add(RawNovel(id=1, title="n")); s.add(NovelChunk(novel_id=1, chapter_no=1, idx=0, text=_dirty())); s.commit()
    from app.retrieval.search import weighted_search
    hit = weighted_search(s, 1, "正文", k=1)[0]
    assert "一秒记住" not in hit["text"] and "正文仍然保留" in hit["text"]


def test_exemplar_excludes_chunk_identity():
    s = _db(); s.add_all([RawNovel(id=1, title="n", genre="仙侠"), Project(id=1, title="p", genre="仙侠")])
    for i in (2, 1):
        s.add(NovelChunk(id=i, novel_id=1, chapter_no=1, idx=i, text=("正常段落内容。" * 60) + ("一秒记住【x.info】" if i == 1 else "")))
    s.commit()
    from app.style.exemplar import sample_style_exemplars
    out = sample_style_exemplars(s, s.get(Project, 1), [1], exclude_chunk_ids={1}, per_novel=2)
    assert out and all(x["chunk_id"] != 1 for x in out)
    assert all("一秒记住" not in x["text"] for x in out)


def test_narrative_chapters_clean_and_ordered():
    s = _db(); s.add(RawNovel(id=1, title="n")); s.add_all([
        NovelChunk(id=2, novel_id=1, chapter_no=2, idx=1, text="二章后"),
        NovelChunk(id=1, novel_id=1, chapter_no=1, idx=1, text="一章一秒记住【x.info】"),
        NovelChunk(id=3, novel_id=1, chapter_no=1, idx=0, text="一章先"),
    ]); s.commit()
    from app.style.narrative import _load_novel_chapters
    chapters = _load_novel_chapters(s, 1)
    assert [x[0] for x in chapters] == [1, 2]
    assert chapters[0][1] == "一章先一章"


def test_final_prompt_cleans_rag_and_exemplars():
    from app.generation.prompts import build_user_prompt
    prompt = build_user_prompt([], [_dirty()], [], 1000, style_exemplars=[{"novel_title":"n", "text":_dirty()}])
    assert "一秒记住" not in prompt and "更新时间" not in prompt
    assert "正文仍然保留" in prompt


def test_consumption_preserves_bytes_and_inline_neighbors():
    from app.ingestion.clean import clean_consumption_text
    legal = "  正文\r\n\r\n他又说。\r\n"
    assert clean_consumption_text(legal)[0] == legal
    raw = "前文。一秒记住【..info】，为您提供精彩小说阅读。后文。无限大冒险全文阅读继续。封禁之神小说5200结束。"
    cleaned, report = clean_consumption_text(raw)
    assert cleaned == "前文。后文。继续。结束。"
    assert report["removed_chars"] == len(raw) - len(cleaned)
    assert report["cleaned"]
    uncertain = "他说未知书名全文阅读可能是广告。"
    kept, record = clean_consumption_text(uncertain)
    assert kept == uncertain and record["unresolved"]


def test_production_retrieval_orders_and_aggregates_sources():
    from app.retrieval.strategies import retrieve_for_continuation, render_retrieval_blocks
    s = _db(); s.add(RawNovel(id=1, title="n"))
    for i, text in enumerate(["先", "重复片段", "重复片段", "后"], 1):
        s.add(NovelChunk(id=i, novel_id=1, chapter_no=1, idx=i, text=text))
    s.commit()
    hits = retrieve_for_continuation(s, 1, 1, k_pos=4)
    assert [h["idx"] for h in hits] == [1, 2, 4]
    assert len(hits[1]["sources"]) == 2
    assert all(h["novel_id"] == 1 and h["clean_report"] and h["raw_text"] for h in hits)
    body = render_retrieval_blocks(list(reversed(hits)))[0][1]
    assert body.index("先") < body.index("重复片段") < body.index("后")
    assert body.count("重复片段") == 1
    semantic = [{**hits[2], "role": "ambience"}, {**hits[0], "role": "ambience"}]
    body = render_retrieval_blocks(semantic)[0][1]
    assert body.index("后") < body.index("先")


def test_adjacent_overlap_and_intentional_repeat():
    from app.style.narrative import _load_novel_chapters
    s = _db(); s.add(RawNovel(id=1, title="n"))
    overlap = "有足够长度并且内容各异的确切重叠证据。" * 2
    texts = ["开头" + overlap, overlap + "结尾", "不要。", "不要。"]
    for i, text in enumerate(texts):
        s.add(NovelChunk(novel_id=1, chapter_no=1, idx=i, text=text))
    s.commit()
    assert _load_novel_chapters(s, 1)[0][1] == "开头" + overlap + "结尾不要。不要。"


def test_final_prompt_cleans_structured_blocks_without_metadata_leak():
    from app.generation.prompts import build_user_prompt
    prompt = build_user_prompt([], [], [], 1000,
        rag_blocks=[("参考", "正文前一秒记住【x.info】，为您提供精彩小说阅读。正文后")],
        style_exemplars=[{"text": "正文样例", "raw_text": "RAW_SENTINEL", "clean_report": {"removed": "REPORT_SENTINEL"}}])
    assert "正文前正文后" in prompt
    assert all(x not in prompt for x in ["一秒记住", "RAW_SENTINEL", "REPORT_SENTINEL"])


def test_overlap_preserves_gaps_full_duplicates_and_cross_chapter():
    from types import SimpleNamespace
    from app.retrieval.views import stitch_chunks
    overlap = "这是有意重复的一段话，需要保护其独立完整性。" * 2
    def row(i, text, chapter=1):
        return SimpleNamespace(id=i, novel_id=1, chapter_no=chapter, idx=i, text=text)
    for rows in ([row(1, overlap), row(2, overlap)],
                 [row(1, "前" + overlap), row(3, overlap + "后")],
                 [row(1, "前" + overlap), row(2, overlap + "后", chapter=2)]):
        text, report = stitch_chunks(rows)
        assert text == "".join(r.text for r in rows)
    assert stitch_chunks([row(1, overlap), row(2, overlap)])[1]["overlaps"][0]["status"] == "ambiguous_preserved"


def test_search_and_exemplar_duplicate_provenance():
    from app.retrieval.search import weighted_search
    from app.style.exemplar import sample_style_exemplars
    s = _db(); s.add_all([RawNovel(id=1, title="n"), Project(id=1, title="p")])
    text = "正常正文内容。" * 60
    for i in (1, 2):
        s.add(NovelChunk(id=i, novel_id=1, chapter_no=i, idx=i, text=text))
    s.commit()
    hits = weighted_search(s, 1, "正常", k=2)
    assert len(hits) == 1 and len(hits[0]["sources"]) == 2
    samples = sample_style_exemplars(s, s.get(Project, 1), [1], per_novel=2)
    assert len(samples) == 1 and len(samples[0]["sources"]) == 2


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
