"""助理记忆（`app.assistant_memory`）的定向测试。

守四件事：
1. **只对陈述句抽取**：问句/短句不触发，避免每问一次就多调一次模型；
2. 抽取结果解析是防御性的：非 JSON、超长、非字符串一律丢掉；
3. 长期记忆有上限且幂等（重复内容不追加第二条）；
4. 拼进提示词的内容有界（记忆 + 最近对话），且不会把当前问题重复一遍。

跑法：项目根 `python -m pytest tests/test_assistant_memory.py -q`。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import assistant_memory as am
from app.assistant_store import record_message
from app.db import Base
from app.models import Project
from app.routers.assistant_memory import delete_memory, list_memories


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def project_id(db) -> int:
    project = Project(title="记忆测试")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project.id


class _Provider:
    """只回一句话的假模型：记下调用次数，用来断言「该省的时候真的省了」。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


# ── 该不该抽取 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("这本书要一直用第一人称叙述", True),
        ("别写电话号码，也别写真实地址", True),
        # 问句不抽：查资料不会产生长期偏好
        ("第 3 段和第 1 段重复吗？", False),
        ("这一段为什么写得这么慢", False),
        ("帮我看看第 7 段的伏笔", False),
        # 太短的不抽
        ("继续", False),
    ],
)
def test_should_extract(text, expected):
    assert am.should_extract(text) is expected


# ── 解析模型输出 ────────────────────────────────────────────────────────


def test_parse_extracted_accepts_plain_and_fenced_json():
    assert am.parse_extracted('["用第一人称"]') == ["用第一人称"]
    assert am.parse_extracted('```json\n["别写电话号码"]\n```') == ["别写电话号码"]


def test_parse_extracted_rejects_garbage_and_oversize():
    assert am.parse_extracted("好的，我记住了") == []
    assert am.parse_extracted("[bad json") == []
    # 超过 40 字的一条直接丢掉（半句话记下来只会误导）
    assert am.parse_extracted('["' + "很长的记忆" * 10 + '"]') == []
    # 非字符串元素丢掉
    assert am.parse_extracted('["用第一人称", 42]') == ["用第一人称"]


def test_parse_extracted_caps_items_per_turn():
    raw = '["一","二二","三三三","四四四四"]'
    assert len(am.parse_extracted('["用第三人称叙写","别写电话号码","主角叫陈小满"]')) == 2
    assert len(am.parse_extracted(raw)) == 2


# ── 写入 / 去重 / 上限 ──────────────────────────────────────────────────


def test_remember_is_idempotent(db, project_id):
    row, created, reason = am.remember(db, project_id, "这本书用第一人称")
    assert created and reason == "created" and row is not None

    again, created_again, reason_again = am.remember(db, project_id, "这本书用第一人称")
    assert created_again is False and reason_again == "duplicate"
    assert again is not None and again.id == row.id
    assert len(am.list_memories(db, project_id)) == 1


def test_remember_refuses_when_full(db, project_id, monkeypatch):
    monkeypatch.setattr(am, "MAX_MEMORIES", 2)
    am.remember(db, project_id, "第一条记忆")
    am.remember(db, project_id, "第二条记忆")

    row, created, reason = am.remember(db, project_id, "第三条记忆")

    assert row is None and created is False and reason == "full"
    assert len(am.list_memories(db, project_id)) == 2


# ── 自动抽取 ────────────────────────────────────────────────────────────


def test_auto_extract_writes_items(db, project_id):
    provider = _Provider('["这本书用第一人称","别写电话号码"]')

    added = am.auto_extract(
        db, project_id,
        query="这本书要一直用第一人称，别写电话号码",
        answer="好的，之后都按这个来。",
        provider=provider,
    )

    assert [row.text for row in added] == ["这本书用第一人称", "别写电话号码"]
    assert [row.source for row in added] == ["auto", "auto"]
    assert provider.calls == 1


def test_auto_extract_skips_questions_without_calling_model(db, project_id):
    provider = _Provider('["不该被写入"]')

    added = am.auto_extract(
        db, project_id,
        query="第 3 段和第 1 段是不是重复了？",
        answer="不重复。",
        provider=provider,
    )

    assert added == []
    assert provider.calls == 0
    assert am.list_memories(db, project_id) == []


def test_auto_extract_does_not_duplicate_existing(db, project_id):
    am.remember(db, project_id, "这本书用第一人称")
    provider = _Provider('["这本书用第一人称"]')

    added = am.auto_extract(
        db, project_id,
        query="记住，这本书一直用第一人称",
        answer="好。",
        provider=provider,
    )

    assert added == []
    assert len(am.list_memories(db, project_id)) == 1


def test_auto_extract_survives_provider_error(db, project_id):
    class _Boom:
        def chat(self, system: str, user: str) -> str:
            raise RuntimeError("模型挂了")

    added = am.auto_extract(
        db, project_id,
        query="这本书要一直用第一人称",
        answer="好。",
        provider=_Boom(),
    )

    assert added == []


def test_auto_extract_without_provider_is_noop(db, project_id):
    assert am.auto_extract(
        db, project_id, query="这本书要一直用第一人称", answer="好。", provider=None
    ) == []


def test_merge_candidates_uses_model_to_dedupe_synonyms():
    """回填历史时会出现「写作使用第一人称 / 写作口径：第一人称」这类同义条目。"""
    provider = _Provider('["写作使用第一人称"]')

    merged = am.merge_candidates(
        ["写作使用第一人称"], ["写作口径：第一人称", "写作使用第一人称"], provider
    )

    assert merged == ["写作使用第一人称"]
    assert provider.calls == 1


def test_merge_candidates_falls_back_when_model_does_not_return_json():
    provider = _Provider("我觉得这些都挺重要的")

    merged = am.merge_candidates([], ["乙条记忆内容", "甲条记忆内容", "乙条记忆内容"], provider)

    assert merged == ["乙条记忆内容", "甲条记忆内容"]


def test_merge_candidates_drops_synonym_even_if_model_keeps_it():
    """模型漏判时的确定性兜底：同义改写不能再占一行。"""
    provider = _Provider('["对话使用第一人称","这本书用第一人称","这书采用第一人称"]')

    merged = am.merge_candidates(
        ["对话使用第一人称"], ["这本书用第一人称", "这书采用第一人称"], provider
    )

    assert merged == ["对话使用第一人称"]


def test_merge_candidates_keeps_distinct_facts_sharing_a_prefix():
    """`主角叫陈小满` 与 `主角叫王大勇` 只共享 3 个字，不能被当成同一条。"""
    provider = _Provider("[]")

    merged = am.merge_candidates(
        ["主角叫陈小满"], ["主角叫王大勇"], provider
    )

    # 模型回了空数组（= 认为无新增），这里只验证兜底判重不会误杀
    assert am._looks_same("主角叫陈小满", "主角叫王大勇") is False
    assert merged == []


# ── 提示词拼装 ──────────────────────────────────────────────────────────


def test_prompt_block_contains_memory_and_recent_dialog(db, project_id):
    am.remember(db, project_id, "这本书用第一人称")
    record_message(db, project_id, role="user", kind="plain", text="上一段写了什么？")
    record_message(db, project_id, role="assistant", kind="plain", text="写的是送药。")

    block = am.prompt_block(db, project_id, exclude_text="这一句是新问题")

    assert "【长期记忆】" in block and "这本书用第一人称" in block
    assert "【最近对话】" in block and "用户：上一段写了什么？" in block
    assert "助手：写的是送药。" in block
    assert "这一句是新问题" not in block


def test_prompt_block_drops_dialog_when_not_referential(db, project_id):
    """不需要指代的句子不带上文：弱模型会模仿上一条回复（实测过的坑）。"""
    am.remember(db, project_id, "这本书用第一人称")
    record_message(db, project_id, role="user", kind="plain", text="忘掉第一人称")
    record_message(db, project_id, role="assistant", kind="plain", text="已删除。")

    block = am.prompt_block(
        db, project_id, query="这本书以后都按第一人称写，别写电话号码。"
    )

    assert "【长期记忆】" in block
    assert "【最近对话】" not in block


def test_prompt_block_keeps_dialog_for_referential_query(db, project_id):
    record_message(db, project_id, role="user", kind="plain", text="第 3 段写了什么？")
    record_message(db, project_id, role="assistant", kind="plain", text="写的是送药。")

    block = am.prompt_block(db, project_id, query="那这一段呢")

    assert "【最近对话】" in block and "助手：写的是送药。" in block


def test_prompt_block_skips_duplicate_current_question(db, project_id):
    """重复提问时，最后一条 user 消息就是当前问题，不能拼两遍。"""
    record_message(db, project_id, role="user", kind="plain", text="第 3 段重复吗？")
    record_message(db, project_id, role="assistant", kind="plain", text="不重复。")

    block = am.prompt_block(db, project_id, exclude_text="第 3 段重复吗？")

    assert block.count("第 3 段重复吗？") == 0


def test_prompt_block_is_bounded(db, project_id, monkeypatch):
    monkeypatch.setattr(am, "PROMPT_BLOCK_CHARS", 80)
    am.remember(db, project_id, "很长的记忆" * 20)

    block = am.prompt_block(db, project_id)

    assert len(block) <= 80 + len("\n…（记忆已截断）")
    assert block.endswith("（记忆已截断）")


# ── HTTP 层（路由函数直调，不启服务） ──────────────────────────────────


def test_router_list_and_delete(db, project_id):
    row, _, _ = am.remember(db, project_id, "这本书用第一人称")

    assert list_memories(project_id, db)["count"] == 1

    after = delete_memory(row.id, project_id, db)
    assert after["count"] == 0
