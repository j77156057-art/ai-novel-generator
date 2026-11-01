"""全书摘要（BookDigest）：抽取档位、证据强制、三项验收、落库与消费。

## 为什么这组测试的重量全压在「证据」上

摘要最容易出的事故不是「压缩得不够狠」，而是**看起来很像摘要的编造**：
时间线读起来顺、角色读起来像那么回事 —— 但那些事件在原文里根本不存在。

所以本组测试钉住三件事：

1. **证据强制**：引文必须逐字出现在它声称的那一章里，否则整条丢弃；
2. **三项验收**：压缩比 / 主线覆盖 / 角色覆盖能自动判，且**判得过**；
3. **消费端的边界**：摘要进上下文时必须 respect 「故事进行到第几章」，
   没有边界的书整本不放行（fail closed，与检索链同口径）。

绝不真调 LLM：全部用确定性假 provider。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import NovelChunk, Project, RawNovel  # noqa: E402
from app.style.digest import (  # noqa: E402
    MODE_AUTO,
    MODE_RETRIEVAL,
    MODE_WHOLE,
    TARGET_COMPRESSION,
    build_book_digest,
    choose_digest_mode,
    digest_caps,
    digest_content_hash,
    digest_summary,
    enforce_evidence,
    latest_digest,
    parse_digest,
    render_digest_text,
    save_digest,
    verify_digest,
)
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures / 工具
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


#: 原文里真实存在的两句（引文必须逐字命中它们）。
REAL_LINES = [
    "沈砚第一次杀人是在矿脉塌方那天，他握着镐没敢松手。",
    "老陶把矿灯递给他，说了句走吧，然后自己走在前面。",
]


class WholeProvider:
    """假 provider：声明能吃下整本，返回一份**带真引文**的摘要。"""

    max_context_chars = 1_500_000
    distill_block_chars = 20_000

    def __init__(self, payload: str | None = None) -> None:
        self.calls = 0
        self.payload = payload

    def chat(self, system: str, user: str, max_tokens: int | None = None, **kwargs) -> str:
        self.calls += 1
        if self.payload is not None:
            return self.payload
        return (
            '{"timeline": [{"seq": 1, "chapter_no": 1, '
            '"in_story_time": "开篇", "event": "沈砚在矿脉塌方里第一次杀人", '
            '"actors": ["沈砚"], "weight": 0.9, '
            '"evidence": [{"chapter_no": 1, "quote": "沈砚第一次杀人是在矿脉塌方那天"}]}], '
            '"characters": [{"name": "沈砚", "role": "矿工", "arc": "从矿工到执旗", '
            '"key_events": [1], "relations": [], "voice": {"avg_len": 6, "habits": ["话少"]}, '
            '"evidence": [{"chapter_no": 1, "quote": "他握着镐没敢松手"}]}], '
            '"reactions": [{"character": "沈砚", "trigger": "被当众羞辱", '
            '"says": "短句、不辩解", "does": "转身离开", '
            '"evidence": [{"chapter_no": 2, "quote": "老陶把矿灯递给他"}]}]}'
        )


class LiarProvider(WholeProvider):
    """假 provider：引文全是编的（原文里没有）。"""

    def chat(self, system: str, user: str, max_tokens: int | None = None, **kwargs) -> str:
        self.calls += 1
        return (
            '{"timeline": [{"seq": 1, "chapter_no": 1, "event": "沈砚屠了半个矿场", '
            '"actors": ["沈砚"], '
            '"evidence": [{"chapter_no": 1, "quote": "这句话原文里根本没有出现过"}]}], '
            '"characters": [], "reactions": []}'
        )


def _make_corpus(db, n_chapters: int = 4) -> int:
    novel = RawNovel(title="摘要测试语料", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(n_chapters):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=i + 1,
                idx=i,
                text=REAL_LINES[i % len(REAL_LINES)] + "他停了很久才继续往前走。",
            )
        )
    db.commit()
    return novel.id


def _make_project(db, novel_id: int, cutoffs: list[dict] | None = None) -> Project:
    proj = Project(
        title="摘要测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        reference_novel_ids=[novel_id],
        story_authority="reference_anchor",
        reference_cutoffs=cutoffs,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


# ====================================================================
# 1. 抽取档位：为什么是这一档，必须说得出理由
# ====================================================================


def test_auto_mode_without_channel_capability_uses_retrieval():
    """渠道没声明能读多少字 ⇒ 不敢整本投喂（假设错了就是静默截断）。"""
    mode, reason = choose_digest_mode(100_000, provider=None)
    assert mode == MODE_RETRIEVAL
    assert "未声明" in reason


def test_auto_mode_with_capability_and_small_book_uses_whole():
    """书不大 + 渠道声明能吃下 ⇒ 整本一次投喂（跨章问题才答得出来）。"""
    mode, reason = choose_digest_mode(100_000, provider=WholeProvider())
    assert mode == MODE_WHOLE
    assert "整本" in reason


def test_auto_mode_falls_back_when_book_too_large():
    mode, reason = choose_digest_mode(500_000, provider=WholeProvider())
    assert mode == MODE_RETRIEVAL
    assert "超过" in reason


def test_explicit_mode_is_honored():
    mode, reason = choose_digest_mode(500_000, provider=WholeProvider(),
                                      requested=MODE_WHOLE)
    assert mode == MODE_WHOLE
    assert reason == "用户指定"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        choose_digest_mode(1000, provider=None, requested="whatever")


def test_caps_scale_with_book_size():
    """条目上限随书变大（压缩比要稳定）。"""
    small = digest_caps(100_000)
    big = digest_caps(1_000_000)
    assert big.timeline >= small.timeline
    assert big.characters >= small.characters
    assert big.reactions >= small.reactions


# ====================================================================
# 2. 解析：模型输出的噪声要能剥掉，真错误要报出来
# ====================================================================


def test_parse_digest_strips_code_fence():
    raw = '```json\n{"timeline": [], "characters": [], "reactions": []}\n```'
    parsed = parse_digest(raw)
    assert parsed["timeline"] == []


def test_parse_digest_rejects_garbage():
    with pytest.raises(ValueError):
        parse_digest("这不是 JSON")


def test_parse_digest_rejects_non_object():
    with pytest.raises(ValueError):
        parse_digest("[1, 2, 3]")


# ====================================================================
# 3. 证据强制：没证据的条目直接丢
# ====================================================================


def test_enforce_evidence_drops_fabricated_entries():
    """引文在原文里不存在 ⇒ 整条丢弃。"""
    chapters = {1: REAL_LINES[0], 2: REAL_LINES[1]}
    digest = {
        "timeline": [
            {"event": "真的", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "沈砚第一次杀人是在矿脉塌方那天"}]},
            {"event": "编的", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "这句话原文里根本没有出现过"}]},
            {"event": "没证据", "chapter_no": 1},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert [e["event"] for e in kept["timeline"]] == ["真的"]
    assert stats["dropped"] == 2
    assert stats["kept"] == 1
    assert stats["dropped_ratio"] > 0.5


def test_enforce_evidence_rejects_wrong_chapter():
    """引文是真的，但挂在**错的章**上 ⇒ 同样丢弃（归属错了就是编）。"""
    chapters = {1: REAL_LINES[0], 2: REAL_LINES[1]}
    digest = {
        "timeline": [
            {"event": "挂错章", "chapter_no": 1,
             "evidence": [{"chapter_no": 2, "quote": "沈砚第一次杀人是在矿脉塌方那天"}]},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert kept["timeline"] == []
    assert stats["dropped"] == 1


def test_enforce_evidence_accepts_truncated_quote():
    """模型把引文截断了几个字 ⇒ 仍然算数（前缀命中即可）。"""
    chapters = {1: REAL_LINES[0]}
    digest = {
        "timeline": [
            {"event": "截断引文", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "沈砚第一次杀人是在矿脉"}]},
        ]
    }
    kept, _ = enforce_evidence(digest, chapters)
    assert len(kept["timeline"]) == 1


# ====================================================================
# 4. 三项验收
# ====================================================================


def test_verify_digest_reports_all_three_checks():
    chapters = {1: REAL_LINES[0], 2: REAL_LINES[1]}
    digest = {
        "timeline": [{"event": "沈砚第一次杀人", "chapter_no": 1,
                      "actors": ["沈砚"],
                      "evidence": [{"chapter_no": 1, "quote": "沈砚第一次杀人是在矿脉塌方那天"}]}],
        "characters": [{"name": "沈砚", "role": "矿工",
                        "evidence": [{"chapter_no": 1, "quote": "他握着镐没敢松手"}]}],
        "reactions": [],
    }
    report = verify_digest(
        digest, chapters, chars_in=10_000,
        mainline_candidates=["沈砚第一次杀人是在矿脉塌方那天"],
        character_names=["沈砚"],
    )
    assert set(report["checks"]) == {"compression", "mainline_coverage", "character_coverage"}
    assert report["checks"]["character_coverage"]["value"] == 1.0
    assert report["checks"]["mainline_coverage"]["value"] == 1.0
    assert report["checks"]["compression"]["ok"] is True


def test_verify_digest_flags_missing_character():
    """摘要里没有某个高频角色 ⇒ 角色覆盖不达标。"""
    chapters = {1: REAL_LINES[0]}
    digest = {"timeline": [], "characters": [], "reactions": []}
    report = verify_digest(
        digest, chapters, chars_in=10_000,
        mainline_candidates=[], character_names=["沈砚", "老陶"],
    )
    assert report["checks"]["character_coverage"]["value"] == 0.0
    assert report["checks"]["character_coverage"]["ok"] is False
    assert report["passed"] is False


def test_verify_digest_flags_evidence_loss():
    """成片缺证据 ⇒ 整次判失败。"""
    chapters = {1: REAL_LINES[0]}
    digest = {"timeline": [], "characters": [], "reactions": []}
    report = verify_digest(
        digest, chapters, chars_in=10_000, mainline_candidates=[], character_names=[],
        evidence_stats={"kept": 1, "dropped": 5, "dropped_ratio": 0.83},
    )
    assert report["checks"]["evidence"]["ok"] is False
    assert report["passed"] is False


def test_render_digest_text_is_readable():
    digest = {
        "timeline": [{"event": "沈砚第一次杀人", "chapter_no": 1, "in_story_time": "开篇",
                      "actors": ["沈砚"]}],
        "characters": [{"name": "沈砚", "role": "矿工", "arc": "从矿工到执旗",
                        "relations": [{"name": "老陶", "kind": "师徒"}],
                        "voice": {"habits": ["话少"]}}],
        "reactions": [{"character": "沈砚", "trigger": "被当众羞辱",
                       "says": "短句", "does": "转身离开"}],
    }
    text = render_digest_text(digest)
    assert "【故事时间线】" in text
    assert "【主要角色】" in text
    assert "【情境反应】" in text
    assert "师徒" in text


# ====================================================================
# 5. 端到端：抽取 → 证据强制 → 落库
# ====================================================================


def test_build_book_digest_keeps_only_evidenced_entries(db_session):
    nid = _make_corpus(db_session)
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    assert digest["mode"] == MODE_WHOLE
    assert digest["timeline"], "有真引文的条目应当留下"
    assert digest["stats"]["dropped"] == 0
    assert digest["verify"]["checks"]["evidence"]["ok"] is True


def test_build_book_digest_drops_lies(db_session):
    """整本摘要全是编的 ⇒ 条目全被丢掉（产物为空，如实反映）。"""
    nid = _make_corpus(db_session)
    digest = build_book_digest(db_session, nid, provider=LiarProvider(), mode=MODE_WHOLE)
    assert digest["timeline"] == []
    assert digest["stats"]["dropped"] == 1
    assert digest["verify"]["checks"]["evidence"]["ok"] is False


def test_save_and_load_digest(db_session):
    nid = _make_corpus(db_session)
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    content_hash = digest_content_hash(db_session, nid)
    row = save_digest(db_session, digest, content_hash)
    assert row.id > 0
    again = save_digest(db_session, digest, content_hash)
    assert again.id == row.id, "同输入必须幂等（不能长出第二行）"
    found = latest_digest(db_session, nid)
    assert found is not None
    summary = digest_summary(found)
    assert summary["novel_id"] == nid
    assert summary["mode"] == MODE_WHOLE


# ====================================================================
# 6. 端点
# ====================================================================


def test_digest_endpoints_lifecycle(db_session, monkeypatch):
    """提交 → 跑完 → 查得到 → 验收查得到。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    provider = WholeProvider()
    monkeypatch.setattr(
        "app.style.narrative.resolve_distill_provider", lambda: provider
    )
    client = TestClient(fastapi_app)
    r = client.post(f"/api/projects/{proj.id}/digests", json={"novel_id": nid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["job_id"] > 0

    got = client.get(f"/api/books/{nid}/digest")
    assert got.status_code == 200, got.text
    assert got.json()["mode"] == MODE_WHOLE
    assert got.json()["text"]

    verify = client.get(f"/api/books/{nid}/digest/verify")
    assert verify.status_code == 200, verify.text
    assert "compression" in verify.json()["checks"]


def test_digest_endpoint_404_when_absent(db_session):
    nid = _make_corpus(db_session)
    client = TestClient(fastapi_app)
    assert client.get(f"/api/books/{nid}/digest").status_code == 404
    assert client.get(f"/api/books/{nid}/digest/verify").status_code == 404


def test_digest_endpoint_rejects_unknown_mode(db_session):
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(f"/api/projects/{proj.id}/digests",
                    json={"novel_id": nid, "mode": "whatever"})
    assert r.status_code == 400, r.text


def test_digest_endpoint_rejects_unknown_field(db_session):
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    client = TestClient(fastapi_app)
    r = client.post(f"/api/projects/{proj.id}/digests",
                    json={"novel_id": nid, "modee": "whole"})
    assert r.status_code == 422, r.text


# ====================================================================
# 7. 消费：边界是硬约束
# ====================================================================


def test_digest_block_is_empty_in_original_path(db_session):
    """原创路径不注入摘要（摘要里是剧情，塞进去模型会照着写）。"""
    from app.generation.digest_context import digest_block

    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    assert digest_block(db_session, proj, [nid], profile="original") == ""


def test_digest_block_respects_cutoff(db_session):
    """超出「故事进行到第几章」的条目不进上下文。"""
    from app.generation.digest_context import digest_block

    nid = _make_corpus(db_session, 4)
    proj = _make_project(db_session, nid, cutoffs=[{"novel_id": nid, "chapter": 1}])
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    block = digest_block(db_session, proj, [nid], profile="continuation")
    assert block, "边界内的条目应当出现"
    assert "第 2 章" not in block
    assert "老陶" not in block, "第 2 章的反应条目越界了"


def test_digest_block_is_empty_without_cutoff(db_session):
    """没有边界的书整本不放行（fail closed，与检索链同口径）。"""
    from app.generation.digest_context import digest_block

    nid = _make_corpus(db_session, 4)
    proj = _make_project(db_session, nid)
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    assert digest_block(db_session, proj, [nid], profile="continuation") == ""


def test_digest_character_block_gives_reactions(db_session):
    """角色对话：摘要给出「他面对这种事会怎么反应」。"""
    from app.generation.digest_context import digest_character_block

    nid = _make_corpus(db_session, 4)
    proj = _make_project(db_session, nid, cutoffs=[{"novel_id": nid, "chapter": 4}])
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    block = digest_character_block(
        db_session, proj, [nid], "沈砚", cutoffs={nid: 4}
    )
    assert "被当众羞辱" in block
    assert "沈砚" in block


def test_retrieve_digest_entries_is_bounded(db_session):
    """摘要检索：越界的条目不返回；没有边界则一条都不返回。"""
    from app.generation.digest_context import retrieve_digest_entries

    nid = _make_corpus(db_session, 4)
    digest = build_book_digest(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))

    hits = retrieve_digest_entries(db_session, nid, "杀人 矿脉", cutoff=4)
    assert hits
    assert all(h["chapter_no"] <= 4 for h in hits)

    assert retrieve_digest_entries(db_session, nid, "杀人", cutoff=None) == []
