"""Agent 辅助语料清洗测试（第 10 步交付 1/2/3/4 验收）。

覆盖派单列出的验收项：

**交付 1（确定性检测，`app.agents.text_clean`）**
  (a) 各判据各至少 1 例：乱码替换符 / 非法控制符 / 编码残留 / 断字 / 单字成段 /
      连续超短段 / 相邻重复字 / 低频异常字；每条 finding 带 chunk_id / 字符区间 /
      可疑片段 / 判据名 / 严重级别；
  (b) 正常文本零假阳（正常对白、正当叠字不得被误报）。

**交付 2（clean-corpus 端点）**
  (c) 确定性检测 + 模型后置：规则没命中的分片不调模型；响应用 diagnosis 九字段 issue
      schema，含 answer / issues / confidence / agent_run_id / suggested_edits(before/after)。
  (d) provider 抛错 → 降级为纯确定性结果 + 明确声明模型未参与（model_participated=False）。

**交付 3（提案审批通道）**
  (e) clean-corpus **不写 `novel_chunks`**（前后逐字节 sha256 比对）；
  (f) apply 后 chunk 内容真变 + 产生 Version（kind=corpus_text）+ 向量失效；
  (g) `corpus_text_edit ↔ novel_chunk_text` 之外的类型组合被拒（422）；
  (h) 漂移 → stale + 409，不覆盖。
  (i) 受控批量应用三条护栏：通配符拒 / 超上限拒 / 一条 stale 则整批不写。

**交付 4（入库链路）**
  (j) `ingest_text` 把「待复核可疑点数量」写进 `clean_report.text_clean`（附抽样标注）。

不依赖真实网络：Assistant provider 由用例显式换成「炸锅器 / 记录器」。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

# ─── bootstrap ──────────────────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import proposals as proposal_lib  # noqa: E402
from app.agents import text_clean  # noqa: E402
from app.agents.registry import reset_tool_registry  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    EditProposal,
    Generation,
    NovelChunk,
    Project,
    RawNovel,
    Version,
)
from app.routers import assistant_clean as clean_router  # noqa: E402
from app.routers import assistant_proposals as proposals_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# provider 桩
# ====================================================================


class _BoomProvider:
    """chat 一律抛错：模拟 Assistant provider 不可用（降级路径）。"""

    model = "boom-assistant"

    def chat(self, system: str, user: str, constraints=None, **kwargs) -> str:  # noqa: ANN001
        raise RuntimeError("模拟 assistant provider 不可用")


class _JsonProvider:
    """返回预设文本（用于模型成功 / 过度改写两条路径）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.model = "json-assistant"
        self.seen: tuple[str, str] | None = None

    def chat(self, system: str, user: str, constraints=None, **kwargs) -> str:  # noqa: ANN001
        self.seen = (system, user)
        return self.reply


def _use_provider(provider) -> None:  # noqa: ANN001
    """把 Assistant 单例钉死成本用例的桩（get_assistant_provider 命中缓存）。"""
    reset_provider_registry()
    llm_provider_mod.reset_assistant_provider_cache()
    llm_provider_mod._ASSISTANT_PROVIDER = provider


# ====================================================================
# fixtures
# ====================================================================


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例：清 provider / tool 注册表与库；默认 Assistant provider = 炸锅器。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # 默认 Assistant provider 用炸锅器：走「模型未参与」的降级路径，
    # 需要模型成功的用例在用例内手动 _use_provider(_JsonProvider(...))。
    llm_provider_mod._ASSISTANT_PROVIDER = _BoomProvider()
    yield
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def project_id(db_session) -> int:
    proj = Project(title="清洗测试项目", target_length=5000, mode="quick",
                   status="created", generation_length=3000,
                   reference_novel_ids=[])
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


@pytest.fixture
def client():
    return TestClient(fastapi_app)


# ====================================================================
# 种子小工具
# ====================================================================


def _seed_novel(db, texts: list[str], title: str = "清洗测试语料"):
    """建一本语料 + 若干分片；返回 (novel_id, [chunk_id, ...])。"""
    novel = RawNovel(title=title, status="imported")
    db.add(novel)
    db.flush()
    ids: list[int] = []
    for i, t in enumerate(texts):
        chunk = NovelChunk(novel_id=novel.id, chapter_no=1, idx=i, text=t)
        db.add(chunk)
        db.flush()
        ids.append(int(chunk.id))
    db.commit()
    return int(novel.id), ids


def _chunk_text_hashes(db, novel_id: int) -> dict[int, str]:
    """逐分片文本的**字节级** sha256（不用 repr(ORM)，避免内存地址噪声）。"""
    rows = db.scalars(
        select(NovelChunk).where(NovelChunk.novel_id == novel_id).order_by(NovelChunk.id)
    ).all()
    return {int(c.id): hashlib.sha256((c.text or "").encode("utf-8")).hexdigest()
            for c in rows}


def _clean_corpus(client, *, project_id=None, novel_id=None, **extra):
    payload = {"limit": 20, "dry_run": False}
    if project_id is not None:
        payload["project_id"] = project_id
    if novel_id is not None:
        payload["novel_id"] = novel_id
    payload.update(extra)
    return client.post("/api/assistant/clean-corpus", json=payload)


# ====================================================================
# (a) 确定性检测各判据
# ====================================================================


def test_detect_replacement_char():
    text = "正文开始\ufffd\ufffd然后继续。"
    findings = text_clean.detect_replacement_chars(text, chunk_id=7, novel_id=3)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "replacement_char"
    assert f.severity == "error"
    assert (f.start, f.end) == (4, 6)
    assert f.chunk_id == 7 and f.novel_id == 3
    assert text[f.start:f.end] == "\ufffd\ufffd"


def test_detect_illegal_control_char():
    findings = text_clean.detect_control_chars("abc\x07def")
    assert len(findings) == 1
    assert findings[0].rule == "illegal_control_char"
    assert findings[0].severity == "warn"
    # 换行/制表不算非法
    assert text_clean.detect_control_chars("a\tb\nc\r") == []


def test_detect_encoding_residue_marker_and_pua():
    findings = text_clean.detect_encoding_residue("正常文本锟斤拷继续")
    rules = {f.rule for f in findings}
    assert rules == {"encoding_residue"}
    assert any(f.severity == "error" and "锟斤拷" in f.fragment for f in findings)

    # 私用区 / 康熙部首 → 编码事故（warn）
    pua = text_clean.detect_encoding_residue("正文\ue000\u2f01结束")
    assert pua, "私用区/部首未检出"
    assert all(f.rule == "encoding_residue" for f in pua)
    assert any(f.severity == "warn" for f in pua)


def test_detect_broken_line_cut():
    text = "他推开门走进了那间堆满旧书的小屋然后\n径直走向窗边那张落了灰的木桌旁边。"
    findings = text_clean.detect_broken_lines(text)
    assert any(f.rule == "broken_line_cut" and f.severity == "warn" for f in findings)


def test_detect_single_char_and_ultra_short_run():
    text = "杀\n他就这样走了过去\n嗯\n啊\n呀"
    findings = text_clean.detect_broken_lines(text)
    rules = {f.rule for f in findings}
    assert "single_char_paragraph" in rules
    assert "ultra_short_paragraph_run" in rules
    # 仅提示类
    assert all(f.severity == "info" for f in findings
               if f.rule in {"single_char_paragraph", "ultra_short_paragraph_run"})


def test_detect_duplicate_adjacent():
    findings = text_clean.detect_duplicate_adjacent("他他走进来的的房间里站站")
    frags = {f.fragment for f in findings}
    assert "他他" in frags and "的的" in frags
    # 「站站」不在 watch 集合里 → 不报
    assert all("站站" not in f.fragment for f in findings)
    assert all(f.rule == "duplicate_adjacent_char" for f in findings)


def test_detect_rare_low_frequency_char():
    # 用扩展 A 区字符（GB2312 无法编码）造一个全语料只出现 1 次的生僻字
    filler = "风雪夜归人，陆见推开木门看向远方。" * 40  # > 500 字，凑 min_corpus_chars
    chunks = [(1, 100, filler + "他忽然想起一个字：㐀。")]
    findings = text_clean.detect_rare_low_frequency(chunks)
    rare = [f for f in findings if f.rule == "rare_char_low_frequency"]
    assert rare, "低频生僻字未检出"
    assert rare[0].chunk_id == 1 and rare[0].novel_id == 100
    assert rare[0].severity == "info"
    assert "㐀" in rare[0].fragment


def test_clean_text_has_no_findings():
    """正常对白 + 正当叠字：逐分片判据零命中（防假阳）。"""
    text = (
        "风雪夜，陆见推开木门。\n"
        "“我回来了。”陆见说道。\n"
        "白光沉默不语，慢慢地抬头看向远方看看天色。\n"
        "“你终于来了。”他叹了口气。"
    )
    assert text_clean.scan_chunk(text) == []


def test_scan_corpus_is_deterministic():
    chunks = [(1, 9, "他他走了过来。\ufffd"), (2, 9, "正常的一句话。")]
    a = text_clean.scan_corpus(chunks)
    b = text_clean.scan_corpus(chunks)
    assert [f.to_dict() for f in a] == [f.to_dict() for f in b]
    assert {f.rule for f in a} >= {"duplicate_adjacent_char", "replacement_char"}


def test_deterministic_fix_only_for_safe_rules():
    # 可安全修正：删替换符 + 合并重复字
    fixed = text_clean.deterministic_fix("他他来了\ufffd。", {"duplicate_adjacent_char",
                                                          "replacement_char"})
    assert fixed == "他来了。"
    # 断字 / 低频字没有安全自动改法 → None
    assert text_clean.deterministic_fix("a\nb", {"broken_line_cut"}) is None


# ====================================================================
# (c)(d) clean-corpus 端点：检测 + 降级
# ====================================================================


def test_clean_corpus_reports_issues_and_degrades_without_model(client, db_session,
                                                               project_id):
    """provider 抛错 → 只返回确定性结果 + 明确声明模型未参与。"""
    novel_id, chunk_ids = _seed_novel(
        db_session, ["他他走进房间，看到门牌上写着「欢迎」\ufffd\ufffd随后坐下。"]
    )
    resp = _clean_corpus(client, project_id=project_id, novel_id=novel_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 九字段 issue schema
    assert body["issues"], "应命中可疑点"
    for issue in body["issues"]:
        for key in ("id", "type", "severity", "title", "claim", "suggestion",
                    "rule", "evidence"):
            assert key in issue
    assert body["findings"] == body["issues"]
    for key in ("answer", "confidence", "agent_run_id", "suggested_edits",
                "model_participated", "model_note"):
        assert key in body

    # 降级：模型未参与，且明确说出来
    assert body["model_participated"] is False
    assert "未做任何模型复核" in body["answer"] or "未参与" in body["answer"]

    # 仍有确定性建议修正（删替换符 + 合并他他），before/after 齐全
    assert body["suggested_edits"], "确定性修正应给出建议"
    edit = body["suggested_edits"][0]
    assert edit["source"] == "rule"
    assert edit["before"] and edit["after"] and edit["after"] != edit["before"]
    assert "\ufffd" not in edit["after"]
    assert "他他" not in edit["after"]

    # AgentRun 留痕，且 model 记 local_fallback（未过模型）
    run = db_session.get(AgentRun, body["agent_run_id"])
    assert run is not None and run.role == "assistant"
    assert run.model == "(local_fallback)"


def test_clean_corpus_does_not_call_model_when_no_findings(client, db_session,
                                                            project_id):
    """规则零命中 → 不调模型（provider 是炸锅器；若被调用请求会走降级分支）。"""
    novel_id, _ = _seed_novel(db_session, ["风雪夜，陆见推开木门，慢慢地走了进去。"])
    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()
    assert body["issues"] == []
    assert body["model_note"] == "no_eligible_chunks"
    assert "未调用" in body["answer"]


def test_clean_corpus_model_success_path(client, db_session, project_id):
    """模型复核成功（断字）：suggested_edit.source=model，before/after 可查。"""
    original = "他推开门走进了那间堆满旧书的小屋然后\n径直走向窗边那张落了灰的木桌旁边。"
    corrected = "他推开门走进了那间堆满旧书的小屋然后径直走向窗边那张落了灰的木桌旁边。"
    novel_id, chunk_ids = _seed_novel(db_session, [original])
    cid = chunk_ids[0]

    _use_provider(_JsonProvider(json.dumps(
        {"results": [{"chunk_id": cid, "corrected": corrected,
                      "note": "并回被切断的换行", "confidence": 0.8}]},
        ensure_ascii=False,
    )))
    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()

    assert body["model_participated"] is True
    edit = next(e for e in body["suggested_edits"] if e["source"] == "model")
    assert edit["before"] == original
    assert edit["after"] == corrected
    # 产出 pending 提案（仍未写正文）
    assert body["proposal_count"] >= 1


def test_clean_corpus_discards_model_over_edit(client, db_session, project_id):
    """模型大段改写 → 护栏丢弃（model_rejected），不产提案。"""
    original = "他推开门走进了那间堆满旧书的小屋然后\n径直走向窗边那张落了灰的木桌旁边。"
    novel_id, chunk_ids = _seed_novel(db_session, [original])
    cid = chunk_ids[0]

    _use_provider(_JsonProvider(json.dumps(
        {"results": [{"chunk_id": cid,
                      "corrected": "完全不相干的一大段全新内容，与原文毫无关系。",
                      "note": "我重写了一遍", "confidence": 0.99}]},
        ensure_ascii=False,
    )))
    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()

    assert body["model_rejected"], "过度改写应被护栏丢弃"
    assert all(e["source"] != "model" for e in body["suggested_edits"])
    assert body["proposal_count"] == 0


def test_clean_corpus_dry_run_creates_no_proposals(client, db_session, project_id):
    novel_id, _ = _seed_novel(db_session, ["他他走了\ufffd。"])
    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id,
                         dry_run=True).json()
    assert body["dry_run"] is True
    assert body["proposal_count"] == 0
    assert db_session.scalar(select(func.count(EditProposal.id))) == 0


@pytest.mark.parametrize("payload,code", [
    ({"limit": 20}, 422),                       # project_id / novel_id 都没给
    ({"project_id": 1, "limit": 0}, 422),       # limit 下界
    ({"project_id": 1, "limit": 99999}, 422),   # limit 上界
])
def test_clean_corpus_validation(client, project_id, payload, code):
    assert client.post("/api/assistant/clean-corpus", json=payload).status_code == code


def test_clean_corpus_unknown_ids_404(client, db_session, project_id):
    assert _clean_corpus(client, project_id=project_id, novel_id=987654).status_code == 404
    assert _clean_corpus(client, project_id=project_id,
                         chunk_ids=[987654]).status_code == 404


def test_clean_corpus_anchor_derivation_from_novel_only(client, db_session, project_id):
    """只给 novel_id → 用既有项目做审计锚点（agent_runs.project_id 非空）。"""
    novel_id, _ = _seed_novel(db_session, ["他他走了过来。"])
    body = _clean_corpus(client, novel_id=novel_id).json()
    assert body["scope"]["project_id"] == project_id


# ====================================================================
# (e)(f) 提案通道：不静默改正文 / apply 才写入
# ====================================================================


def test_clean_corpus_never_touches_novel_chunks(client, db_session, project_id):
    """clean-corpus 产物是 pending 提案，`novel_chunks` 逐字节零变化。"""
    novel_id, _ = _seed_novel(db_session, ["他他走了\ufffd。", "正常一句话。"])
    before = _chunk_text_hashes(db_session, novel_id)

    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()
    assert body["proposal_count"] >= 1

    after = _chunk_text_hashes(db_session, novel_id)
    assert after == before, "创建提案阶段绝不能改动 novel_chunks 正文"
    # 提案都在 pending
    rows = list(db_session.scalars(select(EditProposal)).all())
    assert rows and all(r.status == "pending" for r in rows)
    assert all(r.type == "corpus_text_edit" for r in rows)
    assert all(r.target_kind == "novel_chunk_text" for r in rows)


def test_apply_corpus_text_edit_changes_text_and_writes_version(client, db_session,
                                                               project_id):
    novel_id, chunk_ids = _seed_novel(db_session, ["他他走了\ufffd。"])
    cid = chunk_ids[0]
    original = db_session.get(NovelChunk, cid).text

    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()
    pid = body["proposals"][0]["id"]

    # 未 approve 直接 apply → 409，正文零变化
    assert client.post(f"/api/assistant/proposals/{pid}/apply",
                       json={"action": "apply"}).status_code == 409
    assert db_session.get(NovelChunk, cid).text == original

    assert client.post(f"/api/assistant/proposals/{pid}/approve",
                       json={"action": "approve", "actor": "t"}).status_code == 200
    resp = client.post(f"/api/assistant/proposals/{pid}/apply",
                       json={"action": "apply", "actor": "t"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["status"] == "applied"
    assert out["kind"] == "corpus_text"

    db_session.expire_all()
    chunk = db_session.get(NovelChunk, cid)
    assert chunk.text != original
    assert "\ufffd" not in chunk.text and "他他" not in chunk.text
    # 文本一变，旧向量失效（置空）
    assert chunk.embedding is None

    version = db_session.get(Version, out["version_id"])
    assert version.kind == "corpus_text"
    assert version.snapshot_json["before"] == original
    assert version.snapshot_json["after"] == chunk.text


def test_corpus_text_edit_type_must_match_target_kind(client, db_session, project_id):
    novel_id, chunk_ids = _seed_novel(db_session, ["他他走了。"])
    cid = chunk_ids[0]
    # corpus_text_edit 只能作用于 novel_chunk_text
    bad1 = client.post("/api/assistant/proposals", json={
        "project_id": project_id, "type": "corpus_text_edit",
        "target_kind": "generation_text", "target_id": str(cid), "after_text": "x",
    })
    assert bad1.status_code == 422
    # text_edit 不能作用于 novel_chunk_text
    bad2 = client.post("/api/assistant/proposals", json={
        "project_id": project_id, "type": "text_edit",
        "target_kind": "novel_chunk_text", "target_id": str(cid), "after_text": "x",
    })
    assert bad2.status_code == 422


def test_corpus_text_edit_target_id_must_be_int(client, db_session, project_id):
    resp = client.post("/api/assistant/proposals", json={
        "project_id": project_id, "type": "corpus_text_edit",
        "target_kind": "novel_chunk_text", "target_id": "not-an-int",
        "after_text": "x",
    })
    assert resp.status_code == 422


def test_corpus_text_edit_empty_after_is_rejected(client, db_session, project_id):
    novel_id, chunk_ids = _seed_novel(db_session, ["他他走了。"])
    resp = client.post("/api/assistant/proposals", json={
        "project_id": project_id, "type": "corpus_text_edit",
        "target_kind": "novel_chunk_text", "target_id": str(chunk_ids[0]),
        "after_text": "   ",
    })
    assert resp.status_code == 422


def test_corpus_text_edit_drift_is_stale(client, db_session, project_id):
    novel_id, chunk_ids = _seed_novel(db_session, ["他他走了\ufffd。"])
    cid = chunk_ids[0]
    pid = _clean_corpus(client, project_id=project_id,
                        novel_id=novel_id).json()["proposals"][0]["id"]
    client.post(f"/api/assistant/proposals/{pid}/approve",
                json={"action": "approve", "actor": "t"})

    # 别处改了该分片 → 漂移
    chunk = db_session.get(NovelChunk, cid)
    chunk.text = "用户手改的版本，绝不能丢"
    db_session.commit()

    resp = client.post(f"/api/assistant/proposals/{pid}/apply",
                       json={"action": "apply", "actor": "t"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "stale"
    db_session.expire_all()
    assert db_session.get(NovelChunk, cid).text == "用户手改的版本，绝不能丢"
    assert db_session.get(EditProposal, pid).status == "stale"


# ====================================================================
# (i) 受控批量应用三条护栏
# ====================================================================


def _make_two_approved(client, db_session, project_id) -> tuple[int, int, list[int], int]:
    """造两个分片各一条 pending 提案并批准；返回 (novel_id, ?, chunk_ids, ...)."""
    novel_id, chunk_ids = _seed_novel(
        db_session, ["他他走了\ufffd。", "的的来了\ufffd。"]
    )
    body = _clean_corpus(client, project_id=project_id, novel_id=novel_id).json()
    pids = [p["id"] for p in body["proposals"]]
    assert len(pids) == 2
    for pid in pids:
        assert client.post(f"/api/assistant/proposals/{pid}/approve",
                           json={"action": "approve", "actor": "t"}).status_code == 200
    return novel_id, chunk_ids, pids, project_id  # type: ignore[return-value]


@pytest.mark.parametrize("payload", [
    {"action": "apply", "all": True},
    {"action": "apply", "status": "pending"},
    {"action": "apply", "project_id": 1},
    {"action": "apply"},
    {"action": "apply", "proposal_ids": []},
])
def test_apply_selected_rejects_wildcard_and_empty(client, project_id, payload):
    resp = client.post("/api/assistant/proposals/apply-selected", json=payload)
    assert resp.status_code == 422, resp.text


def test_apply_selected_rejects_over_cap(client, project_id):
    n = proposal_lib.MAX_BULK_APPLY + 1
    resp = client.post("/api/assistant/proposals/apply-selected",
                       json={"action": "apply", "proposal_ids": list(range(1, n + 1))})
    assert resp.status_code == 422
    assert str(proposal_lib.MAX_BULK_APPLY) in resp.json()["detail"]


def test_apply_selected_happy_path(client, db_session, project_id):
    novel_id, chunk_ids, pids, _ = _make_two_approved(client, db_session, project_id)
    before = _chunk_text_hashes(db_session, novel_id)

    resp = client.post("/api/assistant/proposals/apply-selected",
                       json={"action": "apply", "proposal_ids": pids, "actor": "t"})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["ok"] is True and len(out["applied"]) == 2

    db_session.expire_all()
    after = _chunk_text_hashes(db_session, novel_id)
    assert after != before
    for cid in chunk_ids:
        assert "\ufffd" not in db_session.get(NovelChunk, cid).text
    # 每条都写 Version
    assert db_session.scalar(select(func.count(Version.id))) == 2
    assert all(v.kind == "corpus_text"
               for v in db_session.scalars(select(Version)).all())


def test_apply_selected_one_stale_blocks_whole_batch(client, db_session, project_id):
    """一条 stale → 整批不写：全部 chunk 保持原样、零 Version、逐条报告。"""
    novel_id, chunk_ids, pids, _ = _make_two_approved(client, db_session, project_id)
    before = _chunk_text_hashes(db_session, novel_id)

    # 让第一条的目标漂移
    chunk = db_session.get(NovelChunk, chunk_ids[0])
    chunk.text = "外部改动，导致第一条提案 stale"
    db_session.commit()

    resp = client.post("/api/assistant/proposals/apply-selected",
                       json={"action": "apply", "proposal_ids": pids, "actor": "t"})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["ok"] is False
    assert len(detail["stale"]) == 1
    assert detail["stale"][0]["proposal_id"] == pids[0]

    # 整批零写入：两条都没有被 apply（第二条保持 approved），零 Version
    db_session.expire_all()
    after = _chunk_text_hashes(db_session, novel_id)
    # 第一条是外部改动（本来就不是提案的 after），第二条必须仍是原文
    assert after[chunk_ids[1]] == before[chunk_ids[1]]
    assert db_session.scalar(select(func.count(Version.id))) == 0
    assert db_session.get(EditProposal, pids[0]).status == "stale"
    assert db_session.get(EditProposal, pids[1]).status == "approved"


def test_apply_selected_missing_proposal_404(client, project_id):
    resp = client.post("/api/assistant/proposals/apply-selected",
                       json={"action": "apply", "proposal_ids": [987654]})
    assert resp.status_code == 404


# ====================================================================
# (j) 入库链路：待复核计数写进 clean_report
# ====================================================================


def test_ingest_records_pending_review_count(db_session):
    from app.ingestion.store import ingest_text

    text = "正文内容若干他他走了过来，看到门牌写着欢迎。\ufffd\ufffd随后坐下休息。"
    res = ingest_text(db_session, text, title="脏文本.txt", platform="upload")

    novel = db_session.get(RawNovel, res["novel_id"])
    report = novel.clean_report
    assert isinstance(report, dict)
    assert "text_clean" in report
    tc = report["text_clean"]
    assert tc["pending_review_count"] >= 1
    assert tc["scanned_chunks"] >= 1
    assert "by_rule" in tc and tc["by_rule"]
    assert isinstance(tc["sampled"], bool)


def test_ingest_clean_text_reports_zero_pending(db_session):
    from app.ingestion.store import ingest_text

    res = ingest_text(db_session, "风雪夜，陆见推开木门，慢慢地走了进去。",
                      title="干净.txt", platform="upload")
    novel = db_session.get(RawNovel, res["novel_id"])
    tc = novel.clean_report["text_clean"]
    assert tc["pending_review_count"] == 0
