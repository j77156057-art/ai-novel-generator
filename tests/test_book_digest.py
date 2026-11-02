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
from app.models import DistillRun, NovelChunk, Project, RawNovel  # noqa: E402
from app.style.digest import (  # noqa: E402
    DIGEST_VERSION,
    MODE_AUTO,
    MODE_RETRIEVAL,
    MODE_WHOLE,
    TARGET_COMPRESSION,
    build_book_digest,
    choose_digest_mode,
    digest_caps,
    digest_content_hash,
    digest_plan,
    digest_summary,
    enforce_evidence,
    latest_digest,
    parse_digest,
    render_digest_text,
    save_digest,
    verify_digest,
)
from app.style.jobs import STATUS_SUCCESS  # noqa: E402
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
    """书超过**渠道声明能读多少字** ⇒ 切块抽（不是「超过某个写死的数」）。"""
    mode, reason = choose_digest_mode(1_600_000, provider=WholeProvider())
    assert mode == MODE_RETRIEVAL
    assert "超过" in reason


def test_auto_mode_whole_for_real_book_scale():
    """真书量级（66 万字）+ 声明能读 150 万字的渠道 ⇒ 整本一次投喂。

    2026-11-02 真跑前改的门槛：原来写死「书 ≤ 30 万字」，
    而本机最小的真书也有 43.6 万字 ⇒ 写死的门槛让整本档在真书上**永远不可达**。
    这条用例把那个事实钉住：门槛由渠道的真实能力推导，不由拍脑袋的数推导。
    """
    mode, reason = choose_digest_mode(663_125, provider=WholeProvider())
    assert mode == MODE_WHOLE
    assert "整本" in reason
    assert "渠道单次可读" in reason


def test_auto_mode_respects_absolute_ceiling_when_channel_overclaims():
    """渠道声明得离谱 ⇒ 绝对天花板兜住（否则一次请求会被静默截断）。"""
    class GreedyProvider:
        max_context_chars = 50_000_000

    mode, reason = choose_digest_mode(3_000_000, provider=GreedyProvider())
    assert mode == MODE_RETRIEVAL
    assert "绝对天花板" in reason


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


def test_enforce_evidence_reattributes_wrong_chapter():
    """引文是真的、只是挂错章 ⇒ **改章号、留条目**（不是丢掉）。

    2026-11-02 真跑（66 万字真书）实测：被丢的 34 条**逐条回查原文后发现
    全部是「引文逐字就在书里，只是章号挂了」**（偏移多为 ±1）。

    引文是**证据**，章号只是**指针**。指针错了应该修指针 ——
    把证据一起扔掉等于「因为目录页码错了，就说这本书没写过这段」。
    """
    chapters = {1: REAL_LINES[0], 2: REAL_LINES[1]}
    digest = {
        "timeline": [
            {"event": "挂错章", "chapter_no": 1,
             "evidence": [{"chapter_no": 2, "quote": "沈砚第一次杀人是在矿脉塌方那天"}]},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert len(kept["timeline"]) == 1
    assert stats["dropped"] == 0
    assert stats["reattributed"] == 1
    # 章号被就地改成了引文真正所在的那一章
    assert kept["timeline"][0]["evidence"][0]["chapter_no"] == 1


def test_enforce_evidence_drops_quote_far_from_claimed_chapter():
    """引文离它声称的章**太远** ⇒ 丢弃（那种情况多半不是同一处的事）。

    远处仍能找到的记成 `quote_far`（区别于 `quote_not_found` = 压根不是书里的话），
    这样界面能分开说「引文是真的只是挂得远」与「这句是编的」。
    """
    chapters = {1: REAL_LINES[0], 40: REAL_LINES[1]}
    digest = {
        "timeline": [
            {"event": "离得太远", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "老陶把矿灯递给他，说了句走吧，然后自己走在前面"}]},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert kept["timeline"] == []
    assert stats["dropped"] == 1
    assert stats["drop_reasons"].get("quote_far") == 1


def test_enforce_evidence_accepts_short_quote_when_it_is_verbatim():
    """短引文**逐字出现在它声称的那一章** ⇒ 算证据。

    2026-11-02 第四次真跑实测：被丢的 31 条里 25 条是「引文太短」，
    而那些短引文确实逐字出现在它声称的章里（一句三字断言落在第 17 章）。
    ⇒ 证据的强度取决于**能不能定位到那一章**，不取决于引文多长。
    """
    chapters = {1: "众人皆道：此处是水脉。"}
    digest = {
        "timeline": [
            {"event": "湖畔遇水脉", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "是水脉。"}]},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert len(kept["timeline"]) == 1
    assert stats["dropped"] == 0


def test_enforce_evidence_drops_empty_quote():
    """空引文 / 单字引文 ⇒ 当不了证据，但**原因要单独记**。"""
    chapters = {1: REAL_LINES[0]}
    digest = {
        "timeline": [
            {"event": "引文是空的", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "。"}]},
        ]
    }
    kept, stats = enforce_evidence(digest, chapters)
    assert kept["timeline"] == []
    assert stats["drop_reasons"].get("quote_too_short") == 1


def test_enforce_evidence_records_drop_reasons():
    """丢弃要**记下原因**（模型在编 vs 引文写错，处置完全相反）。"""
    chapters = {1: REAL_LINES[0]}
    digest = {
        "timeline": [
            {"event": "编的", "chapter_no": 1,
             "evidence": [{"chapter_no": 1, "quote": "这句话原文里根本没有出现过"}]},
            {"event": "没证据", "chapter_no": 1},
        ]
    }
    _kept, stats = enforce_evidence(digest, chapters)
    assert stats["drop_reasons"].get("quote_not_found") == 1
    assert stats["drop_reasons"].get("no_evidence") == 1
    # 样本也要留（界面要能回答「为什么丢了这么多」）
    assert stats["drop_samples"]["quote_not_found"]


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


# ====================================================================
# 6. 复用判定：必须发生在**花钱之前**
# ====================================================================
#
# 旧顺序是「先 build（整本一次模型调用）→ 再看要不要复用」，
# 于是任务状态里那句「命中已有摘要，未重新抽取」是假的：模型跑完了、额度花了，
# 只是产物没保存。真跑 6 次这本真书时每次都付了一次全本调用的钱。
#
# 现在：digest_plan() 先把参数算出来（纯本地计算），拿它当复用键比对。


def test_digest_plan_costs_nothing(db_session):
    """算计划**不许调模型** —— 它是复用判定的前置步骤，一调就白花钱了。"""
    nid = _make_corpus(db_session)
    provider = WholeProvider()
    plan = digest_plan(db_session, nid, provider=provider, mode=MODE_WHOLE)
    assert provider.calls == 0, "算计划时调了模型"
    assert plan["mode"] == MODE_WHOLE
    assert plan["plan_key"] == {"mode": MODE_WHOLE, "target_ratio": TARGET_COMPRESSION}


def test_plan_key_changes_with_target_ratio(db_session):
    """压缩比目标变了 ⇒ 条目上限不同 ⇒ 复用键必须跟着变。"""
    nid = _make_corpus(db_session)
    a = digest_plan(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE, target_ratio=10)
    b = digest_plan(db_session, nid, provider=WholeProvider(), mode=MODE_WHOLE, target_ratio=5)
    assert a["plan_key"] != b["plan_key"], "改了压缩比目标，复用键却没变"


def _digest_job(monkeypatch, db_session, provider, nid, proj, **kwargs):
    """跑一次后台摘要任务（provider 换成假的那份），返回任务行。"""
    from app.style import narrative as narrative_mod
    from app.style.jobs import create_distill_run, run_book_digest_job

    monkeypatch.setattr(narrative_mod, "resolve_distill_provider", lambda: provider)
    run = create_distill_run(db_session, proj.id, "full", kind="book_digest", novel_id=nid)
    db_session.commit()
    run_book_digest_job(run.id, proj.id, nid, **kwargs)
    db_session.expire_all()
    return db_session.get(DistillRun, run.id)


def test_job_reuses_existing_digest_without_calling_model(db_session, monkeypatch):
    """同书同参数已有产物 ⇒ **一次模型调用都不该发生**。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    first = WholeProvider()
    digest = build_book_digest(db_session, nid, provider=first, mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    db_session.commit()

    second = WholeProvider()
    done = _digest_job(
        monkeypatch, db_session, second, nid, proj, mode=MODE_WHOLE, force=False
    )
    assert second.calls == 0, "命中已有产物却还是调了模型（钱白花了）"
    assert done.status == STATUS_SUCCESS
    assert "命中已有摘要" in (done.stage or "")


def test_job_recomputes_when_target_ratio_changed(db_session, monkeypatch):
    """改了压缩比目标 ⇒ 旧产物不能被复用（参数形同虚设是最难查的那种错）。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    first = WholeProvider()
    digest = build_book_digest(
        db_session, nid, provider=first, mode=MODE_WHOLE, target_ratio=10
    )
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    db_session.commit()

    second = WholeProvider()
    _digest_job(
        monkeypatch, db_session, second, nid, proj,
        mode=MODE_WHOLE, target_ratio=5, force=False,
    )
    assert second.calls == 1, "压缩比目标变了还复用旧产物"


def test_job_recomputes_when_digest_version_changed(db_session, monkeypatch):
    """产物结构版本变了 ⇒ 旧产物不是同一种东西，不能复用。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    first = WholeProvider()
    digest = build_book_digest(db_session, nid, provider=first, mode=MODE_WHOLE)
    row = save_digest(db_session, digest, digest_content_hash(db_session, nid))
    row.digest_version = DIGEST_VERSION + 1
    db_session.commit()

    second = WholeProvider()
    _digest_job(monkeypatch, db_session, second, nid, proj, mode=MODE_WHOLE, force=False)
    assert second.calls == 1, "产物版本变了还复用旧产物"


def test_job_force_always_recomputes(db_session, monkeypatch):
    """force=True ⇒ 无条件重算（人明确说了要重跑）。"""
    nid = _make_corpus(db_session)
    proj = _make_project(db_session, nid)
    first = WholeProvider()
    digest = build_book_digest(db_session, nid, provider=first, mode=MODE_WHOLE)
    save_digest(db_session, digest, digest_content_hash(db_session, nid))
    db_session.commit()

    second = WholeProvider()
    _digest_job(monkeypatch, db_session, second, nid, proj, mode=MODE_WHOLE, force=True)
    assert second.calls == 1, "force=True 还复用旧产物"
