"""Assistant 提案审批 + 版本回滚测试（ADR-2026-10-12 第 5 步第 4 步验收）。

覆盖 team-lead 派单列出的九项验收：

  (a) 提案创建后业务表**零变化**（Generation.text / Project.plot_bible /
      PromptOverride 均未动）；
  (b) 未 approve 直接 apply → 409，业务表零变化；
  (c) approve 后 apply 才写入，且产生 Version 快照（content_hash = sha256）；
  (d) 目标内容变化后 apply → 409 + 提案置 `stale`；
  (e) apply 中途失败 → **事务回滚**，Version 与业务表都不写（无半版本）；
  (f) restore 生成新 Version 且历史不丢（v0→v1→restore 得3 条 Version）；
  (g) apply / restore 不接受批量语义（`apply_all` / `proposal_ids` → 422）；
  (h) 越权工具调用仍 403（Assistant 无法触达写路径）；
  (i) 诊断与问答端点行为不回归（第 2/3 步既有能力仍可用）。

外加三项 patch 能力边界断言：
  (j) patch 白名单 / 黑名单（未知键、delete 类 op、AppSetting 目标一律拒）；
  (k) `type` 与 `target_kind` 必须一一对应（错配 → 422）；
  (l) 提示词生效来源可查（code_default → runtime_override → restore 回默认）。

不依赖真实 DB / 真实 LLM：内存 sqlite（Base.metadata.create_all）+
provider 单例隔离，与 test_assistant_diagnose.py 沿用同一套 fixture 约定。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

# ─── bootstrap ──────────────────────────────────────────────────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.agents import proposals as proposal_lib  # noqa: E402
from app.agents.prompt_overrides import PROMPT_KEYS, PromptKey  # noqa: E402
from app.agents.registry import reset_tool_registry  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.llm.registry import reset_provider_registry  # noqa: E402
from app.models import (  # noqa: E402
    AgentRun,
    EditProposal,
    Generation,
    Project,
    PromptOverride,
    Version,
)
from app.routers import assistant as assistant_router  # noqa: E402
from app.routers import assistant_proposals as proposals_router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402


# ====================================================================
# fixtures
# ====================================================================


@pytest.fixture(autouse=True)
def _offline_provider(monkeypatch):
    """把 Assistant provider 钉死成本地兜底实现，杜绝测试触网。

    不这么做的话，`/qa` 与 `/diagnose` 会走真实 `build_assistant_provider()`
    —— 环境里若配了 API key，测试就会真的发HTTP 请求（并卡满超时）。
    本文件对这两个端点只断言「响应结构仍完整」，不需要真实模型输出。
    """
    from app.llm.provider import LocalFallbackLLMProvider

    monkeypatch.setattr(
        llm_provider_mod, "build_assistant_provider", LocalFallbackLLMProvider
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后清空 provider / tool 注册表与内存库。"""
    reset_provider_registry()
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()
    reset_tool_registry()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
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
def project_id(db_session):
    proj = Project(
        title="提案测试项目",
        target_length=5000,
        mode="quick",
        status="created",
        plot_bible={"characters": [{"name": "张三", "role": "主角"}], "foreshadowing": ["铜钥匙"]},
    )
    db_session.add(proj)
    db_session.commit()
    db_session.refresh(proj)
    return proj.id


@pytest.fixture
def client():
    return TestClient(fastapi_app)


# ====================================================================
# helpers
# ====================================================================


def _seed_segment(db, project_id: int, text: str = "原始正文：铜钥匙藏在门后。",
                  segment_no: int = 1) -> int:
    gen = Generation(
        project_id=project_id,
        segment_no=segment_no,
        chapter_range=f"{segment_no}-{segment_no}",
        text=text,
        summary={"short": f"第 {segment_no} 段摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen.id


def _business_snapshot(db, project_id: int) -> dict:
    """业务表的完整指纹：用于「提案创建 / 未 approve apply 后零变化」断言。"""
    proj = db.get(Project, project_id)
    gens = list(db.scalars(select(Generation)
                           .where(Generation.project_id == project_id)
                           .order_by(Generation.id)).all())
    overrides = list(db.scalars(select(PromptOverride)
                                .where(PromptOverride.project_id == project_id)).all())
    return {
        "plot_bible": json.dumps(proj.plot_bible, sort_keys=True, ensure_ascii=False),
        "generations": {g.id: (g.text, g.summary) for g in gens},
        "prompt_overrides": len(overrides),
    }


def _create_text_proposal(client, project_id: int, gen_id: int,
                          after_text: str = "改写后的正文：铜钥匙被张三取出。",
                          **extra) -> dict:
    payload = {
        "project_id": project_id,
        "type": "text_edit",
        "target_kind": "generation_text",
        "target_id": str(gen_id),
        "after_text": after_text,
        "reason": "修正与事实账本冲突的描述",
    }
    payload.update(extra)
    resp = client.post("/api/assistant/proposals", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve(client, proposal_id: int, actor: str = "tester") -> dict:
    resp = client.post(
        f"/api/assistant/proposals/{proposal_id}/approve",
        json={"action": "approve", "actor": actor},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _apply(client, proposal_id: int, actor: str = "tester") -> dict:
    resp = client.post(
        f"/api/assistant/proposals/{proposal_id}/apply",
        json={"action": "apply", "actor": actor},
    )
    return resp.status_code, (resp.json() if resp.headers.get("content-type", "")
                              .startswith("application/json") else resp.text)


def _restore(client, version_id: int, actor: str = "tester"):
    resp = client.post(
        f"/api/assistant/versions/{version_id}/restore",
        json={"action": "restore", "actor": actor},
    )
    return resp.status_code, (resp.json() if resp.headers.get("content-type", "")
                              .startswith("application/json") else resp.text)


def _proposal_row(db, proposal_id: int) -> EditProposal:
    return db.get(EditProposal, proposal_id)


# ====================================================================
# (a) 提案创建后业务表零变化
# ====================================================================


def test_create_proposal_does_not_touch_business_tables(client, db_session, project_id):
    """创建 pending 提案后，正文 / 剧情圣经 / 提示词覆写表全部零变化。"""
    gen_id = _seed_segment(db_session, project_id)
    before = _business_snapshot(db_session, project_id)

    payload = _create_text_proposal(client, project_id, gen_id)

    after = _business_snapshot(db_session, project_id)
    assert after == before, "提案创建不得改动任何业务表"

    # 提案本身落在 pending，且带上 diff / 影响范围 / 证据槽位
    assert payload["proposal"]["status"] == "pending"
    assert payload["proposal"]["version_id"] is None
    assert payload["proposal"]["diff"], "提案必须带 before/after diff"
    assert payload["proposal"]["impact"], "提案必须带影响范围描述"
    assert payload["proposal"]["base_content_hash"], "提案必须记录目标基线指纹"
    assert payload["next_action"] == "approve"


def test_create_proposal_creates_audit_run(client, db_session, project_id):
    """创建提案会补一条 AgentRun 审计行（role 硬约束为 assistant）。"""
    gen_id = _seed_segment(db_session, project_id)
    _create_text_proposal(client, project_id, gen_id)

    runs = list(db_session.scalars(select(AgentRun)
                                   .where(AgentRun.project_id == project_id)).all())
    assert runs, "提案创建应留下 AgentRun 审计行"
    assert all(r.role == "assistant" for r in runs)
    assert all(r.request_hash for r in runs)


def test_create_proposal_from_issue_uses_material_only(client, db_session, project_id):
    """`from_issue` 只贡献 reason / evidence 素材，改什么仍由请求体决定。"""
    from app.models import KgNode

    node = KgNode(
        project_id=project_id, type="plothole",
        props={"question": "铜钥匙究竟是谁的？", "status": "open"},
        first_chapter=3, last_chapter=3, source="generated",
    )
    db_session.add(node)
    gen_id = _seed_segment(db_session, project_id)
    db_session.commit()
    issue_id = "open_plothole_accumulation:open1"

    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id,
            "type": "text_edit",
            "target_kind": "generation_text",
            # 目标由用户指定 —— issue id 里不含任何目标信息
            "target_id": str(gen_id),
            "after_text": "补一段铜钥匙的线索",
            "from_issue": issue_id,
        },
    )
    assert resp.status_code == 200, resp.text
    proposal = resp.json()["proposal"]
    assert proposal["reason"], "应采用 issue.suggestion 作为 reason 默认值"
    assert proposal["evidence"], "应带上 issue 的证据"
    assert proposal["target_id"] == str(gen_id), "修改目标只来自请求体，不来自 issue"


def test_create_proposal_unknown_issue_returns_404(client, project_id):
    """引用不存在的 issue → 404（不悄悄生成无根据的提案）。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": "1",
            "after_text": "x", "from_issue": "no_such_rule:anchor",
        },
    )
    assert resp.status_code == 404


# ====================================================================
# (b) 未 approve 直接 apply 被拒
# ====================================================================


def test_apply_without_approve_is_rejected(client, db_session, project_id):
    """pending 提案直接 apply → 409，业务表零变化，无 Version。"""
    gen_id = _seed_segment(db_session, project_id)
    payload = _create_text_proposal(client, project_id, gen_id)
    before = _business_snapshot(db_session, project_id)

    code, body = _apply(client, payload["proposal"]["id"])

    assert code == 409
    assert "不允许动作" in json.dumps(body, ensure_ascii=False)
    assert _business_snapshot(db_session, project_id) == before
    assert db_session.scalar(select(func.count(Version.id))) == 0


def test_reject_is_terminal(client, db_session, project_id):
    """拒绝后不能再 approve / apply（终态）。"""
    gen_id = _seed_segment(db_session, project_id)
    payload = _create_text_proposal(client, project_id, gen_id)
    pid = payload["proposal"]["id"]

    assert client.post(f"/api/assistant/proposals/{pid}/reject",
                       json={"action": "reject", "actor": "t"}).status_code == 200
    assert client.post(f"/api/assistant/proposals/{pid}/approve",
                       json={"action": "approve"}).status_code == 409
    code, _ = _apply(client, pid)
    assert code == 409

    row = _proposal_row(db_session, pid)
    assert row.status == "rejected"
    assert db_session.scalar(select(func.count(Version.id))) == 0


def test_apply_after_reject_never_writes(client, db_session, project_id):
    """被拒提案永不写业务表（验收标准 5 前半）。"""
    gen_id = _seed_segment(db_session, project_id)
    payload = _create_text_proposal(client, project_id, gen_id)
    pid = payload["proposal"]["id"]
    before = _business_snapshot(db_session, project_id)

    client.post(f"/api/assistant/proposals/{pid}/reject", json={"action": "reject"})
    _approve_or_409(client, pid)
    code, _ = _apply(client, pid)

    assert code == 409
    assert _business_snapshot(db_session, project_id) == before


def _approve_or_409(client, proposal_id: int) -> None:
    """尝试 approve；409 视为预期（已被拒 / 已过期）。"""
    client.post(f"/api/assistant/proposals/{proposal_id}/approve",
                json={"action": "approve"})


# ====================================================================
# (c) approve 后 apply 才写入
# ====================================================================


def test_apply_after_approve_writes_and_snapshots(client, db_session, project_id):
    """approve → apply：正文被改写 + 产生 Version 快照（含 sha256 指纹）。"""
    gen_id = _seed_segment(db_session, project_id)
    original = db_session.get(Generation, gen_id).text
    payload = _create_text_proposal(client, project_id, gen_id)
    pid = payload["proposal"]["id"]

    approved = _approve(client, pid)
    assert approved["proposal"]["status"] == "approved"
    # approve 只改状态
    assert db_session.get(Generation, gen_id).text == original
    assert db_session.scalar(select(func.count(Version.id))) == 0

    code, result = _apply(client, pid)
    assert code == 200, result
    assert result["status"] == "applied"
    assert result["kind"] == "text"
    assert result["version_id"]

    db_session.expire_all()
    gen = db_session.get(Generation, gen_id)
    assert gen.text == "改写后的正文：铜钥匙被张三取出。"

    version = db_session.get(Version, result["version_id"])
    assert version.kind == "text"
    assert version.content_hash == hashlib.sha256(
        gen.text.encode("utf-8")
    ).hexdigest()
    assert version.snapshot_json["before"] == original
    assert version.is_restore is False

    row = _proposal_row(db_session, pid)
    assert row.status == "applied"
    assert row.version_id == version.id
    assert row.applied_at is not None


def test_double_apply_is_rejected(client, db_session, project_id):
    """重复 apply → 409（幂等：不会重复写 Version）。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)
    assert _apply(client, pid)[0] == 200

    code, _ = _apply(client, pid)
    assert code == 409
    assert db_session.scalar(select(func.count(Version.id))) == 1


def test_fact_resolution_updates_plot_bible(client, db_session, project_id):
    """fact_resolution 改剧情圣经（JSON 校验 + 高风险默认等级）。"""
    new_bible = {"characters": [{"name": "张三", "role": "主角"}],
                 "foreshadowing": [], "direction": "张三独自追查钥匙"}
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "fact_resolution",
            "target_kind": "project_plot_bible", "target_id": "plot_bible",
            "after_text": json.dumps(new_bible, ensure_ascii=False),
            "reason": "回收铜钥匙伏笔",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["risk_level"] == "high"
    pid = resp.json()["proposal"]["id"]

    _approve(client, pid)
    code, result = _apply(client, pid)
    assert code == 200, result
    assert result["kind"] == "bible"

    db_session.expire_all()
    proj = db_session.get(Project, project_id)
    assert proj.plot_bible["direction"] == "张三独自追查钥匙"


def test_fact_resolution_rejects_non_json(client, project_id):
    """plot_bible 覆写必须是合法 JSON 对象 → 否则 422，不写库。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "fact_resolution",
            "target_kind": "project_plot_bible", "target_id": "plot_bible",
            "after_text": "这不是 JSON",
        },
    )
    assert resp.status_code == 422


def test_prompt_edit_writes_runtime_override(client, db_session, project_id):
    """prompt_edit 只写运行时覆写表，不改 prompts.py 代码默认。"""
    from app.generation import prompts as prompts_mod

    default_system = prompts_mod.build_system_prompt(
        "提案测试项目", {"unresolved_plotholes": [], "character_constraints": []}
    )

    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "prompt_edit",
            "target_kind": "prompt_override", "target_id": PromptKey.STYLE,
            "after_text": "多写环境细节，少写内心独白。",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["risk_level"] == "low"
    pid = resp.json()["proposal"]["id"]

    # apply 之前：来源仍是 code_default
    active = client.get(f"/api/assistant/prompts/active?project_id={project_id}").json()
    assert active["has_override"] is False

    _approve(client, pid)
    code, result = _apply(client, pid)
    assert code == 200, result
    assert result["kind"] == "prompt"

    db_session.expire_all()
    rows = list(db_session.scalars(
        select(PromptOverride).where(PromptOverride.prompt_key == PromptKey.STYLE)
    ).all())
    assert len(rows) == 1 and rows[0].active is True
    assert rows[0].override_text == "多写环境细节，少写内心独白。"

    # 代码默认提示词逐字节未变（硬约束：不许改 prompts.py）
    assert prompts_mod.build_system_prompt(
        "提案测试项目", {"unresolved_plotholes": [], "character_constraints": []}
    ) == default_system

    active = client.get(f"/api/assistant/prompts/active?project_id={project_id}").json()
    style_block = next(b for b in active["blocks"] if b["prompt_key"] == PromptKey.STYLE)
    assert style_block["source"] == "runtime_override"
    assert style_block["text"] == "多写环境细节，少写内心独白。"


def test_unknown_prompt_key_is_rejected(client, project_id):
    """不可覆写的提示词键 → 422。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "prompt_edit",
            "target_kind": "prompt_override", "target_id": "system_prompt_whole",
            "after_text": "x",
        },
    )
    assert resp.status_code == 422


# ====================================================================
# (d) 目标版本变化 → stale
# ====================================================================


def test_apply_after_target_changed_is_stale(client, db_session, project_id):
    """提案创建后目标被改过 → apply 返回 409 且提案置 stale，不覆盖新内容。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)

    # 模拟「用户在别处改了正文」
    gen = db_session.get(Generation, gen_id)
    gen.text = "用户手改的版本：铜钥匙在井里。"
    db_session.commit()
    current = gen.text

    code, body = _apply(client, pid)

    assert code == 409
    assert body["detail"]["error"] == "stale"
    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == current, "stale 不得覆盖新内容"
    assert _proposal_row(db_session, pid).status == "stale"
    assert db_session.scalar(select(func.count(Version.id))) == 0


def test_stale_proposal_cannot_be_reapproved(client, db_session, project_id):
    """stale 是终态：不能重新 approve，必须重新生成提案。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)
    gen = db_session.get(Generation, gen_id)
    gen.text = "又改了一次。"
    db_session.commit()
    assert _apply(client, pid)[0] == 409

    assert client.post(f"/api/assistant/proposals/{pid}/approve",
                       json={"action": "approve"}).status_code == 409
    assert _apply(client, pid)[0] == 409


def test_apply_after_external_change_matches_content_is_rejected(client, db_session,
                                                                  project_id):
    """目标被改成与 after 相同的内容 → 拒绝重复应用（无意义的 Version）。"""
    gen_id = _seed_segment(db_session, project_id)
    payload = _create_text_proposal(
        client, project_id, gen_id, after_text="目标文案"
    )
    pid = payload["proposal"]["id"]
    _approve(client, pid)

    gen = db_session.get(Generation, gen_id)
    gen.text = "目标文案"
    db_session.commit()

    # 基线哈希已变 → stale（先于「重复应用」判定触发）
    code, _ = _apply(client, pid)
    assert code == 409
    assert _proposal_row(db_session, pid).status == "stale"


# ====================================================================
# (e) 事务回滚：无半版本
# ====================================================================


def test_apply_rolls_back_when_version_write_fails(client, db_session, project_id,
                                                    monkeypatch):
    """Version 快照写入抛错 → 整个事务回滚：业务表与 versions 表都不留痕迹。"""
    gen_id = _seed_segment(db_session, project_id)
    original = db_session.get(Generation, gen_id).text
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟磁盘故障：Version 快照写不进去")

    monkeypatch.setattr(
        proposals_router.proposal_lib, "insert_version_row", _boom, raising=True
    )

    code, body = _apply(client, pid)
    assert code == 500

    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == original, "业务表必须回滚"
    assert db_session.scalar(select(func.count(Version.id))) == 0, "不得有半版本"
    assert _proposal_row(db_session, pid).status == "approved", "状态也不得已被推进"


def test_apply_rolls_back_when_target_write_fails(client, db_session, project_id,
                                                   monkeypatch):
    """业务表写入抛错 → Version 与业务表都不写。"""
    gen_id = _seed_segment(db_session, project_id)
    original = db_session.get(Generation, gen_id).text
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟业务表写入失败")

    monkeypatch.setattr(
        proposals_router.proposal_lib, "write_target", _boom, raising=True
    )
    code, _ = _apply(client, pid)
    assert code == 500

    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == original
    assert db_session.scalar(select(func.count(Version.id))) == 0


def test_prompt_override_rolls_back_together(client, db_session, project_id,
                                             monkeypatch):
    """提示词覆写与正文一样受事务保护：apply 失败不留 active 覆写。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "prompt_edit",
            "target_kind": "prompt_override", "target_id": PromptKey.CONSTRAINT,
            "after_text": "不要写超过三句的对话。",
        },
    )
    pid = resp.json()["proposal"]["id"]
    _approve(client, pid)

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟快照写入失败")

    monkeypatch.setattr(
        proposals_router.proposal_lib, "insert_version_row", _boom, raising=True
    )
    code, _ = _apply(client, pid)
    assert code == 500

    db_session.expire_all()
    assert db_session.scalar(select(func.count(PromptOverride.id))) == 0


# ====================================================================
# (f) restore 生成新 Version 且历史不丢
# ====================================================================


def test_restore_creates_new_version_and_keeps_history(client, db_session, project_id):
    """restore(v0) → 内容回到 v0；Version 从 1 条变 2 条，v1 快照一条不少。

    v0 快照由`insert_version_row` 直接构造，代表「提案落地前」的基线版本 ——
    真实使用中 v0 就是项目刚建好时的状态，回滚到它正是「撤销这次编辑」。
    """
    gen_id = _seed_segment(db_session, project_id)
    v0_text = db_session.get(Generation, gen_id).text

    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)
    code, applied = _apply(client, pid)
    assert code == 200
    v1_text = "改写后的正文：铜钥匙被张三取出。"
    assert applied["version_id"]
    assert db_session.get(Generation, gen_id).text == v1_text

    # 补一条 v0 基线快照（before=after=v0_text）
    baseline = proposal_lib.insert_version_row(
        db_session,
        proposal_id=pid,
        project_id=project_id,
        kind="text",
        snapshot={
            "target_kind": "generation_text", "target_id": str(gen_id),
            "proposal_type": "text_edit", "before": v0_text, "after": v0_text,
            "before_hash": proposal_lib.content_hash(v0_text),
            "after_hash": proposal_lib.content_hash(v0_text),
        },
        content_hash_value=proposal_lib.content_hash(v0_text),
        created_by="tester",
    )
    db_session.commit()

    code, restored = _restore(client, baseline.id)
    assert code == 200, restored
    assert restored["restored_from_version_id"] == baseline.id
    assert restored["version_id"] not in (baseline.id, applied["version_id"])
    assert restored["is_restore"] is True

    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == v0_text, "内容应回到 v0"
    # 历史完整：v1 快照与 baseline 快照都还在，另加一条 restore 快照
    versions = list(db_session.scalars(select(Version).order_by(Version.id)).all())
    assert len(versions) == 3
    assert [ver.is_restore for ver in versions] == [False, False, True]
    assert versions[0].snapshot_json["after"] == v1_text
    assert versions[2].snapshot_json["after"] == v0_text
    # 对应提案被置 rolled_back
    assert _proposal_row(db_session, pid).status == "rolled_back"


def test_full_lifecycle_v0_v1_v2_rollback_v3(client, db_session, project_id):
    """验收标准 6：v0 → v1 → v2 → 回滚为新版本，差异正确、历史可读。"""
    gen_id = _seed_segment(db_session, project_id)
    v0 = db_session.get(Generation, gen_id).text

    v1 = "第一版改写。"
    pid1 = _create_text_proposal(client, project_id, gen_id, after_text=v1)["proposal"]["id"]
    _approve(client, pid1)
    _, res1 = _apply(client, pid1)

    v2 = "第二版改写。"
    pid2 = _create_text_proposal(client, project_id, gen_id, after_text=v2)["proposal"]["id"]
    _approve(client, pid2)
    _, res2 = _apply(client, pid2)

    code, res3 = _restore(client, res1["version_id"])
    assert code == 200, res3

    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == v1

    versions = list(db_session.scalars(select(Version).order_by(Version.id)).all())
    assert len(versions) == 3
    assert [ver.snapshot_json["after"] for ver in versions] == [v1, v2, v1]
    assert versions[2].is_restore is True
    # v0 仍可从最早的 before 里读到 —— 历史没丢
    assert versions[0].snapshot_json["before"] == v0

    # 版本列表 + diff 端点可读
    listing = client.get(f"/api/assistant/versions?project_id={project_id}").json()
    assert listing["count"] == 3
    diff = client.get(
        f"/api/assistant/versions/diff?from_id={versions[0].id}&to_id={versions[1].id}"
    ).json()
    assert "第二版改写" in diff["diff"]

    # 回滚后可继续生成（新提案仍可正常走完流程）
    v4 = "回滚后继续修改。"
    pid4 = _create_text_proposal(client, project_id, gen_id, after_text=v4)["proposal"]["id"]
    _approve(client, pid4)
    assert _apply(client, pid4)[0] == 200


def test_restore_prompt_override_returns_to_code_default(client, db_session, project_id):
    """restore 一条「无覆写」时期的 prompt 快照 → 回到 code_default。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "prompt_edit",
            "target_kind": "prompt_override", "target_id": PromptKey.DIRECTIVE,
            "after_text": "结尾留一个动作收住。",
        },
    )
    pid = resp.json()["proposal"]["id"]
    _approve(client, pid)
    code, applied = _apply(client, pid)
    assert code == 200

    # 直接手工造一条「after 为空串」的 Version（=代码默认状态）
    base_version = proposal_lib.insert_version_row(
        db_session,
        proposal_id=pid,
        project_id=project_id,
        kind="prompt",
        snapshot={
            "target_kind": "prompt_override",
            "target_id": PromptKey.DIRECTIVE,
            "proposal_type": "prompt_edit",
            "before": "",
            "after": "",
            "before_hash": proposal_lib.content_hash(""),
            "after_hash": proposal_lib.content_hash(""),
        },
        content_hash_value=proposal_lib.content_hash(""),
        created_by="tester",
    )
    db_session.commit()

    code, restored = _restore(client, base_version.id)
    assert code == 200, restored
    db_session.expire_all()
    rows = list(db_session.scalars(
        select(PromptOverride)
        .where(PromptOverride.prompt_key == PromptKey.DIRECTIVE)
        .order_by(PromptOverride.id)).all())
    assert len(rows) == 2
    assert rows[0].active is False, "旧覆写降级而非删除"
    assert rows[1].active is True and rows[1].override_text == ""

    active = client.get(f"/api/assistant/prompts/active?project_id={project_id}").json()
    block = next(b for b in active["blocks"] if b["prompt_key"] == PromptKey.DIRECTIVE)
    assert block["source"] == "runtime_override"
    assert block["text"] == ""


def test_prompt_override_history_is_listed(client, db_session, project_id):
    """提示词覆写历史端点列出全部行（含失效行）。"""
    for i, text in enumerate(["第一版附加要求", "第二版附加要求"]):
        resp = client.post(
            "/api/assistant/proposals",
            json={
                "project_id": project_id, "type": "prompt_edit",
                "target_kind": "prompt_override", "target_id": PromptKey.STYLE,
                "after_text": text,
            },
        )
        pid = resp.json()["proposal"]["id"]
        _approve(client, pid)
        assert _apply(client, pid)[0] == 200

    listing = client.get(f"/api/assistant/prompts/versions?project_id={project_id}").json()
    assert listing["count"] == 2
    # 列表按 id 倒序（最新在前）：最新一条 active，其余降级但保留
    assert [row["active"] for row in listing["overrides"]] == [True, False]
    assert [row["text"] for row in listing["overrides"]] == [
        "第二版附加要求", "第一版附加要求",
    ]
    assert set(listing["prompt_keys"]) == set(PROMPT_KEYS)


# ====================================================================
# (g) 不接受批量语义
# ====================================================================


@pytest.mark.parametrize("field,value", [
    ("apply_all", True),
    ("proposal_ids", [1, 2]),
    ("ids", [1]),
    ("all", True),
    ("batch", True),
])
def test_apply_endpoint_rejects_batch_semantics(client, db_session, project_id, field,
                                                 value):
    """apply 请求体携带批量语义字段 → 422（StrictRequest.extra=forbid）。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)

    resp = client.post(
        f"/api/assistant/proposals/{pid}/apply",
        json={"action": "apply", field: value},
    )
    assert resp.status_code == 422
    db_session.expire_all()
    assert db_session.scalar(select(func.count(Version.id))) == 0


@pytest.mark.parametrize("field,value", [
    ("restore_all", True),
    ("version_ids", [1]),
    ("all", True),
])
def test_restore_endpoint_rejects_batch_semantics(client, db_session, project_id,
                                                   field, value):
    """restore 请求体携带批量语义字段 → 422。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)
    _, applied = _apply(client, pid)

    resp = client.post(
        f"/api/assistant/versions/{applied['version_id']}/restore",
        json={"action": "restore", field: value},
    )
    assert resp.status_code == 422


def test_action_field_is_required_and_must_match(client, db_session, project_id):
    """缺 action / action 与路径动作不符 → 422（不替调用方猜）。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]

    # 缺 action
    assert client.post(f"/api/assistant/proposals/{pid}/approve", json={}).status_code == 422
    # action 与路径动作不符
    assert client.post(f"/api/assistant/proposals/{pid}/approve",
                       json={"action": "apply"}).status_code == 422
    # reject 端点同理
    assert client.post(f"/api/assistant/proposals/{pid}/reject",
                       json={"action": "approve"}).status_code == 422


def test_apply_has_no_bulk_route(client, db_session, project_id):
    """路由表里不存在「应用全部待批」这类端点。"""
    paths = {
        getattr(r, "path", "") for r in fastapi_app.routes
        if getattr(r, "path", "").startswith("/api/assistant")
    }
    # —— 模式化负面断言（不只是硬编码名字）——
    # 本测试的意图是「不存在批量应用入口」。硬编码几个具体路径名挡不住
    # 换个叫法的批量端点：/proposals/bulk-apply、/proposals/apply-batch、
    # /proposals/apply_many、/versions/bulk-restore 都会漏过。故改为按
    # 批量语义**模式**扫描——任何路径只要带这些词就红。
    BULK_PATTERN = re.compile(
        r"bulk|batch|apply[-_]?all|restore[-_]?all|apply[-_]?many|"
        r"apply[-_]?siblings|proposal[-_]?ids|version[-_]?ids",
        re.IGNORECASE,
    )
    offenders = sorted(p for p in paths if BULK_PATTERN.search(p))
    assert not offenders, (
        f"路由表出现批量语义端点，违反「无批量应用入口」红线：{offenders}"
    )

    # 核心端点一个都不能少（第 5 步的快照）。不用穷举集合相等：
    # 后续每合法新增一个端点都会让穷举断言误红（runs / analyze-logs /
    # audit-* / invoke-generation 已经发生过一次）。正面用「核心子集仍在」
    # 守回归，负面用上面的模式扫描守红线——两者合起来比原先的集合相等
    # **更强**：集合相等只能挡住"多出来的路径"，模式扫描能挡住"换个叫法的
    # 同类端点"。
    core = {
        "/api/assistant/qa",
        "/api/assistant/diagnose",
        "/api/assistant/proposals",
        "/api/assistant/proposals/{proposal_id}",
        "/api/assistant/proposals/{proposal_id}/approve",
        "/api/assistant/proposals/{proposal_id}/reject",
        "/api/assistant/proposals/{proposal_id}/apply",
        "/api/assistant/versions",
        "/api/assistant/versions/diff",
        "/api/assistant/versions/{version_id}/restore",
        "/api/assistant/prompts/active",
        "/api/assistant/prompts/versions",
    }
    assert core <= paths, f"核心端点缺失：{core - paths}"


# ====================================================================
# (h) 越权仍 403 / Assistant 无法触达写路径
# ====================================================================


def test_write_tools_still_forbidden_for_assistant():
    """Assistant 工具注册表仍拒绝一切写工具（第 2 步不变量不回归）。"""
    from app.agents.registry import (
        FORBIDDEN_PREFIXES,
        AssistantToolRegistry,
        ToolSpec,
        invoke_tool,
    )

    reg = AssistantToolRegistry()
    with pytest.raises(PermissionError):
        reg.register(ToolSpec(name="write_text", description="x", read_only=False,
                              callable=lambda **k: {}))
    for prefix in FORBIDDEN_PREFIXES:
        with pytest.raises(PermissionError):
            reg.register(ToolSpec(name=f"{prefix}thing", description="x",
                                  read_only=True, callable=lambda **k: {}))
    with pytest.raises(PermissionError):
        invoke_tool("apply_proposal", None, project_id=1)

    from app.agents.registry import get_tool_registry

    for name in get_tool_registry().names():
        assert not name.startswith(("write", "commit", "update", "delete",
                                    "patch", "rollback", "apply"))


def test_diagnose_permission_error_still_403(db_session, monkeypatch, project_id):
    """诊断链路上出现 PermissionError → 仍 403 并把 run 记 failed（不降级）。

    与第 3 步 `test_role_lock_violation_fails_loud_not_degraded` 同一断言：
    角色锁被绕过是安全事件，绝不能降级成「规则没发现问题」。
    """
    def _boom(*args, **kwargs):
        raise PermissionError("Assistant 拒绝调用黑名单前缀工具 'write_x'")

    monkeypatch.setattr(
        assistant_router, "run_diagnostic_rules", _boom, raising=True
    )
    body = assistant_router.AssistantDiagnoseRequest(project_id=project_id, scope="all")
    with pytest.raises(HTTPException) as exc:
        assistant_router.assistant_diagnose(body=body, db=db_session)
    assert exc.value.status_code == 403

    run = db_session.scalars(select(AgentRun).order_by(AgentRun.id.desc())).first()
    assert run.status == "failed"
    assert run.model == "(blocked_by_registry)"


def test_assistant_cannot_approve_its_own_proposal(client, db_session, project_id):
    """Assistant 无法自行批准：approve 需要显式 actor + action，且不来自诊断响应。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]

    # 诊断响应里不带任何可写字段（更不带提案 id / action）
    diag = client.post("/api/assistant/diagnose",
                       json={"project_id": project_id, "scope": "all"}).json()
    blob = json.dumps(diag, ensure_ascii=False)
    for token in ("patch_json", "target_table", "proposal_id", "apply", "approve"):
        assert token not in blob, f"诊断响应泄漏写语义字段 {token}"

    # 未 approve 就 apply → 409
    assert _apply(client, pid)[0] == 409


# ====================================================================
# (i) 既有只读端点不回归
# ====================================================================


def test_qa_endpoint_still_works(client, project_id):
    """第 2 步的 /qa 端点行为不回归：仍返回 answer/evidence/confidence/agent_run_id。"""
    body = {"project_id": project_id, "query": "主角是谁？"}
    result = client.post("/api/assistant/qa", json=body).json()
    for key in ("answer", "evidence", "confidence", "agent_run_id"):
        assert key in result
    assert isinstance(result["evidence"], list)


def test_diagnose_endpoint_still_works(client, project_id):
    """第 3 步的 /diagnose 端点仍返回 issues[] 与 findings[] 别名。"""
    result = client.post("/api/assistant/diagnose",
                         json={"project_id": project_id, "scope": "all"}).json()
    for key in ("answer", "issues", "findings", "evidence", "confidence",
                "agent_run_id", "rules_evaluated"):
        assert key in result
    assert result["findings"] == result["issues"]
    # 规则条数 = 规则表长度（新增规则时这条跟着变，别写死数字）。
    from app.agents.diagnosis import RULE_ORDER
    assert len(result["rules_evaluated"]) == len(RULE_ORDER)


def test_readonly_endpoints_still_reject_write_fields(client, project_id):
    """既有端点仍拒绝写语义字段（StrictRequest 不回归）。"""
    assert client.post("/api/assistant/qa", json={
        "project_id": project_id, "query": "x", "patch_json": {},
    }).status_code == 422
    assert client.post("/api/assistant/diagnose", json={
        "project_id": project_id, "scope": "all", "target_table": "generations",
    }).status_code == 422


# ====================================================================
# (j)(k) patch 能力边界与 type↔target_kind 一一对应
# ====================================================================


@pytest.mark.parametrize("payload_patch", [
    {"fields": ["text"], "op": "delete"},
    {"fields": ["text"], "table": "app_settings"},
    {"fields": ["text"], "extra_key": 1},
    {"delete": True},
    {"fields": ["text"], "note": "顺手把 api_key 也换了"},
])
def test_patch_forbidden_shapes_rejected(project_id, payload_patch):
    """patch 黑名单 / 白名单：删除类、未知键、AppSetting 引用一律 ValueError。"""
    with pytest.raises(proposal_lib.PatchRejected):
        proposal_lib.validate_patch(
            proposal_type="text_edit",
            target_kind="generation_text",
            target_id="1",
            patch=payload_patch,
        )


def test_patch_rejects_forbidden_target_names(project_id):
    """target_id 指向禁止对象 → ValueError。"""
    for bad in ("app_settings", "llm.zhipu_api_key", "projects_delete"):
        with pytest.raises(proposal_lib.PatchRejected):
            proposal_lib.validate_patch(
                proposal_type="fact_resolution",
                target_kind="project_plot_bible",
                target_id=bad,
                patch=None,
            )


@pytest.mark.parametrize("ptype,tkind", [
    ("text_edit", "prompt_override"),
    ("prompt_edit", "generation_text"),
    ("fact_resolution", "generation_text"),
])
def test_type_and_target_kind_must_match(ptype, tkind):
    """type 与 target_kind 必须一一对应，错配 → ValueError（审计表不能自相矛盾）。"""
    with pytest.raises(proposal_lib.PatchRejected):
        proposal_lib.validate_patch(
            proposal_type=ptype, target_kind=tkind, target_id="1", patch=None
        )


def test_proposal_endpoint_rejects_type_mismatch(client, project_id):
    """端点层同样拒绝错配（422），不落库。"""
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "prompt_override", "target_id": PromptKey.STYLE,
            "after_text": "x",
        },
    )
    assert resp.status_code == 422


def test_no_op_proposal_is_rejected(client, db_session, project_id):
    """after == before 的空变更 → 422（不生成无意义提案）。"""
    gen_id = _seed_segment(db_session, project_id)
    current = db_session.get(Generation, gen_id).text
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": current,
        },
    )
    assert resp.status_code == 422


def test_target_from_other_project_is_404(client, db_session, project_id):
    """目标行不属于本项目 → 404（不跨项目写入）。"""
    other = Project(title="别的项目", target_length=5000, mode="quick", status="created")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    other_gen = _seed_segment(db_session, other.id)

    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(other_gen),
            "after_text": "x",
        },
    )
    assert resp.status_code == 404


def test_missing_project_returns_404(client):
    resp = client.get("/api/assistant/proposals?project_id=99999")
    assert resp.status_code == 404


def test_list_proposals_filters_by_status(client, db_session, project_id):
    gen_id = _seed_segment(db_session, project_id)
    p1 = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _create_text_proposal(client, project_id, gen_id, after_text="另一处改写")
    _approve(client, p1)

    pending = client.get(
        f"/api/assistant/proposals?project_id={project_id}&status=pending").json()
    approved = client.get(
        f"/api/assistant/proposals?project_id={project_id}&status=approved").json()
    assert pending["count"] == 1 and approved["count"] == 1
    assert pending["proposals"][0]["id"] != approved["proposals"][0]["id"]
    assert client.get(
        f"/api/assistant/proposals?project_id={project_id}&status=bogus").status_code == 422


# ====================================================================
# 状态机与常量的静态不变量
# ====================================================================


def test_state_machine_shape():
    """状态机形状：pending 单向流、无环、除回滚边外无路径回到 pending。"""
    assert proposal_lib.PROPOSAL_STATUSES == (
        "pending", "approved", "rejected", "applied", "stale", "rolled_back",
    )
    transitions = proposal_lib.ALLOWED_TRANSITIONS
    assert transitions["pending"] == frozenset({"approve", "reject"})
    assert transitions["approved"] == frozenset({"apply"})
    for terminal in ("rejected", "applied", "stale", "rolled_back"):
        assert transitions[terminal] == frozenset(), f"{terminal} 应为终态"
    # 不存在指向 pending 的边（不可能「退回待批」）
    for status, actions in transitions.items():
        for action in actions:
            assert status == "pending" or action != "reset"


def test_state_machine_rejects_illegal_transitions():
    """非法转移 → InvalidTransition。"""
    with pytest.raises(proposal_lib.InvalidTransition):
        proposal_lib.validate_action("pending", "apply")
    with pytest.raises(proposal_lib.InvalidTransition):
        proposal_lib.validate_action("stale", "approve")
    with pytest.raises(proposal_lib.InvalidTransition):
        proposal_lib.validate_action("applied", "apply")
    proposal_lib.validate_action("pending", "approve")  # 合法，不抛


def test_content_hash_is_order_stable_for_dicts():
    """dict 目标（plot_bible）的哈希与键序无关 —— 否则漂移检测会被噪声触发。"""
    a = {"characters": [1, 2], "direction": "x"}
    b = {"direction": "x", "characters": [1, 2]}
    assert proposal_lib.content_hash(a) == proposal_lib.content_hash(b)
    assert proposal_lib.content_hash(None) == proposal_lib.content_hash("")


def test_diff_is_produced_for_changed_text():
    before = "第一行\n第二行"
    after = "第一行\n改过的第二行"
    diff = proposal_lib.unified_diff(before, after)
    assert "改过的第二行" in diff
    assert proposal_lib.unified_diff("same", "same") == ""

# ====================================================================
# P0 回归：漂移检测不得被请求体 scope 关闭
#
# 这组用例存在的理由（不该被删，也不该被「改成只测 happy path」）：
# `scope` 是**文档化的公开字段**，任何合法调用者都能传。旧实现把用户 scope
# 的键无差别覆盖进 scope_payload，配合 `if expected_hash and ...` 的 falsy
# 短路，等于把「目标漂移即拒绝」这条不变量的开关交给了请求体 —— 传
# `"scope": {"base_content_hash": ""}` 就能让所有人手改的内容被静默覆盖。
# ====================================================================


@pytest.mark.parametrize("scope_value", ["", None, 0, False, "\n", "   "])
def test_scope_cannot_disable_drift_detection(client, db_session, project_id,
                                               scope_value):
    """伪造/清空 base_content_hash 的提案在建提案阶段就被拒（422）。

    服务端独占键一旦被请求体命中即 fail loud，不允许走到 apply 阶段。
    """
    gen_id = _seed_segment(db_session, project_id)
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": "攻击者的内容",
            "scope": {"base_content_hash": scope_value},
        },
    )
    assert resp.status_code == 422, f"={scope_value!r} 竟被接受：{resp.text}"
    assert "服务端计算" in resp.json()["detail"]
    assert db_session.scalar(select(func.count(EditProposal.id))) == 0


def test_real_life_scenario_hand_edit_is_never_silently_overwritten(
        client, db_session, project_id):
    """P0 原始复现：手改正文后 apply 必须 409，内容保持用户手改结果。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)

    gen = db_session.get(Generation, gen_id)
    gen.text = "用户亲手改写的重要内容，绝不能丢"
    db_session.commit()

    code, body = _apply(client, pid)
    assert code == 409
    assert body["detail"]["error"] == "stale"
    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == "用户亲手改写的重要内容，绝不能丢"
    assert db_session.scalar(select(func.count(Version.id))) == 0


def test_stored_baseline_is_always_nonempty(client, db_session, project_id):
    """正常提案入库的 base_content_hash 必须是非空 sha256（fail loud 的前置）。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    row = _proposal_row(db_session, pid)
    assert row.scope_json["base_content_hash"]
    assert len(row.scope_json["base_content_hash"]) == 64  # sha256 hex


def test_missing_baseline_fails_loud_on_apply(db_session, project_id):
    """expected_hash 缺失（None / 空串）→ apply 直接报错，不当作「无需检测」跳过。

    旧实现 `if expected_hash and ...` 会静默放行。**此处必须对内存中
    已篡改的提案生效**，确保删掉 fail-loud 就会红。
    """
    gen_id = _seed_segment(db_session, project_id)
    proposal = proposal_lib.build_proposal(
        db_session,
        run_id=_open_run_for_test(db_session, project_id),
        project_id=project_id,
        proposal_type="text_edit",
        target_kind="generation_text",
        target_id=str(gen_id),
        after_text="未经漂移检测就想写入的内容",
        reason="test",
    )
    proposal.status = "approved"  # 只测漂移检查，状态机由别的用例覆盖
    db_session.commit()
    pid = proposal.id

    for broken in (None, "", "   "):
        proposal.scope_json = dict(proposal.scope_json)
        proposal.scope_json["base_content_hash"] = broken
        db_session.commit()
        with pytest.raises(proposal_lib.ProposalError) as exc:
            proposal_lib.apply_proposal(db_session, proposal=proposal, actor="t")
        assert "base_content_hash" in str(exc.value)
        # 关键：不能落到 TargetDrifted 分支之外的「成功写入」
        db_session.expire_all()
        assert db_session.get(Generation, gen_id).text != "未经漂移检测就想写入的内容"
        assert db_session.scalar(select(func.count(Version.id))) == 0
        # 提案状态不得被推进
        assert _proposal_row(db_session, pid).status == "approved"


def test_missing_baseline_fails_loud_via_http(client, db_session, project_id):
    """HTTP 层同样 fail loud：篡改已入库提案的基线后 apply 返回 4xx，不 200。"""
    gen_id = _seed_segment(db_session, project_id)
    pid = _create_text_proposal(client, project_id, gen_id)["proposal"]["id"]
    _approve(client, pid)
    row = _proposal_row(db_session, pid)
    row.scope_json = dict(row.scope_json)
    row.scope_json["base_content_hash"] = ""
    db_session.commit()

    code, _ = _apply(client, pid)
    assert code in (400, 409, 422), f"缺基线竟返回 {code}"
    db_session.expire_all()
    assert db_session.get(Generation, gen_id).text == "原始正文：铜钥匙藏在门后。"
    assert db_session.scalar(select(func.count(Version.id))) == 0


def _open_run_for_test(db_session, project_id: int) -> int:
    """给 lib 层直调用例补一条 AgentRun（绕过 router 的自动开 run）。"""
    run = AgentRun(
        project_id=project_id, role="assistant", status="success",
        started_at=proposal_lib._now_utc(), finished_at=proposal_lib._now_utc(),
        request_hash=proposal_lib.request_hash({"test": True}),
        model="(test)",
    )
    db_session.add(run)
    db_session.flush()
    return int(run.id)


# ====================================================================
# P1 回归：审批依据（diff / impact / target_snapshot）不得被伪造
# ====================================================================


@pytest.mark.parametrize("key", ["diff", "impact", "target_snapshot"])
def test_approval_material_cannot_be_forged(client, db_session, project_id, key):
    """伪造 diff / impact / target_snapshot → 422，服务端算出的真实值不被覆盖。"""
    gen_id = _seed_segment(db_session, project_id)
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": "整段替换，影响很大但伪装成小改动",
            "scope": {key: "仅修正一个错别字"},
        },
    )
    assert resp.status_code == 422, resp.text
    assert key in resp.json()["detail"]


def test_server_computed_diff_and_impact_are_real(client, db_session, project_id):
    """真实 diff/impact 由服务端产出，且反映实际改动规模（非乐观描述）。"""
    gen_id = _seed_segment(db_session, project_id)
    payload = _create_text_proposal(
        client, project_id, gen_id, after_text="完全不同的一段新正文内容",
        scope={"note": "无害的用户元数据应当被保留"},
    )
    proposal = payload["proposal"]
    # 用户元数据保留
    row = _proposal_row(db_session, proposal["id"])
    assert row.scope_json["note"] == "无害的用户元数据应当被保留"
    # 服务端值：diff 含真实新增行
    assert "完全不同的一段新正文内容" in proposal["diff"]
    assert "generations.text" in proposal["impact"]
    # impact 必须是服务端模板，不是用户传入的东西
    assert proposal["impact"] == proposal_lib._describe_impact(
        "generation_text", str(gen_id)
    )


def test_proposal_detail_endpoint_reports_real_diff(client, db_session, project_id):
    """GET /proposals/{id} 返回的 diff 与创建时一致（无二次污染通道）。"""
    gen_id = _seed_segment(db_session, project_id)
    created = _create_text_proposal(client, project_id, gen_id)["proposal"]
    detail = client.get(
        f"/api/assistant/proposals/{created['id']}").json()["proposal"]
    assert detail["diff"] == created["diff"]
    assert detail["impact"] == created["impact"]
    assert detail["base_content_hash"] == created["base_content_hash"]


# ====================================================================
# P2 回归：跨项目 run_id 必须被拒
# ====================================================================


def test_cross_project_run_id_is_rejected(client, db_session, project_id):
    """拿别的项目的 AgentRun.id 当本项目的 run_id → 409，且不落提案。"""
    other = Project(title="另一个项目", target_length=5000, mode="quick",
                    status="created")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    foreign_run = AgentRun(
        project_id=other.id, role="assistant", status="success",
        started_at=proposal_lib._now_utc(), finished_at=proposal_lib._now_utc(),
        request_hash=proposal_lib.request_hash({"foreign": True}), model="(test)",
    )
    db_session.add(foreign_run)
    db_session.commit()
    db_session.refresh(foreign_run)

    gen_id = _seed_segment(db_session, project_id)
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": "试图把别的项目的 run 挂过来",
            "run_id": foreign_run.id,
        },
    )
    assert resp.status_code == 409, resp.text
    assert "不属于当前项目" in resp.json()["detail"]
    assert db_session.scalar(select(func.count(EditProposal.id))) == 0


def test_same_project_run_id_is_accepted(client, db_session, project_id):
    """同项目的 run_id 仍可用（防止修复把正常路径一起堵死）。"""
    own_run = _open_run_for_test(db_session, project_id)
    gen_id = _seed_segment(db_session, project_id)
    db_session.commit()
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": "用本项目的 run 是合法的", "run_id": own_run,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["run_id"] == own_run


def test_missing_run_id_still_404(client, db_session, project_id):
    """不存在的 run_id 仍是 404（P2 修复不得吞掉原有 404 语义）。"""
    gen_id = _seed_segment(db_session, project_id)
    resp = client.post(
        "/api/assistant/proposals",
        json={
            "project_id": project_id, "type": "text_edit",
            "target_kind": "generation_text", "target_id": str(gen_id),
            "after_text": "x", "run_id": 987654,
        },
    )
    assert resp.status_code == 404


# ====================================================================
# 不变量：服务端独占键集合不可被随意增删
# ====================================================================


def test_server_computed_scope_keys_are_a_closed_set():
    """审批依据和局部验收范围必须由服务端产出。

    `local_apply_authorized` / `global_acceptance_state` 是「局部修复准入」这条转移的判据：
    前者是「目标违例已解决、范围外逐字不变」的布尔结论，后者是**被重新绑定**到新正文的
    全局验收义务（未解决清单 + 已关闭清单 + 绑定指纹）。两者都只能由服务端算出 ——
    放进这个集合是为了让请求体伪造直接 422/PatchRejected。
    """
    assert proposal_lib.SERVER_COMPUTED_SCOPE_KEYS == {
        "base_content_hash", "target_snapshot", "diff", "impact",
        "verification_acceptance", "verification_scope",
        "semantic_accepted", "verification_run_id", "confirmation_hash", "repair_result",
        "local_apply_authorized", "global_acceptance_state",
    }


def test_build_proposal_never_reads_reserved_keys_from_scope(db_session, project_id):
    """lib 层直接调用同样拒绝（防有人绕过 router 传脏 scope）。"""
    gen_id = _seed_segment(db_session, project_id)
    with pytest.raises(proposal_lib.PatchRejected):
        proposal_lib.build_proposal(
            db_session,
            run_id=_open_run_for_test(db_session, project_id),
            project_id=project_id,
            proposal_type="text_edit",
            target_kind="generation_text",
            target_id=str(gen_id),
            after_text="x",
            reason="t",
            scope={"base_content_hash": "forged", "diff": "", "impact": ""},
        )
    db_session.rollback()
