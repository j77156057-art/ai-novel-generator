"""伏笔名字收敛：把「账上已有的未回收伏笔」给抽取器，要求同名复用。

## 为什么要这条

落库是按名字去重的（`uq_kg_foreshadow = (project_id, name)`），但抽取器从前
**看不到已有的名字** ⇒ 每抽一轮，同一件事就多一个说法，账本无界增长：
实测 9 段正文攒出 **117 个名字**，切片把其中 97 条 active 全渲染进提示词，
**2,639 字**。

本文件钉住三件事：
1. 名单块只在**有名字**时出现；没名字时返回空串（提示词与从前逐字节相同）；
2. 提示词里写明「必须沿用原名字 / 只抽本段新埋下的」，且**示例里不能有可照抄的章号**
   （实测模型照抄示例 `planned_payoff_chapter: 50`，18 条全填 50）；
3. 批抽链路真的把名单放进 **system** 消息（放 user 里实测复用率不够）；
4. 名单只取**未回收**（planted / developing）的，已回收/已丢弃的不再要求沿用。
"""
from __future__ import annotations

import pytest


def test_known_foreshadow_block_lists_names():
    from app.kg.extractor_v3 import known_foreshadow_block

    blk = known_foreshadow_block(["断刀的来历", "水门被内应打开后的走向"])
    assert "断刀的来历" in blk
    assert "水门被内应打开后的走向" in blk
    assert "原名字" in blk


def test_known_foreshadow_block_is_empty_when_no_names():
    """首次抽取没有名单 ⇒ 提示词与从前**逐字节相同**（不搭车任何行为变化）。"""
    from app.kg.extractor_v3 import known_foreshadow_block

    assert known_foreshadow_block([]) == ""
    assert known_foreshadow_block(None) == ""
    assert known_foreshadow_block(["", "  "]) == ""


def test_extraction_prompt_has_no_copyable_chapter_number():
    """示例里**不能有具体章号** —— 模型会照抄（实测 18 条 planned_payoff=50）。"""
    from app.kg.extractor_v3 import _EXTRACT_SYSTEM

    assert '"planned_payoff_chapter": 50' not in _EXTRACT_SYSTEM
    assert '"planted_chapter": 1,' not in _EXTRACT_SYSTEM
    assert "原名字" in _EXTRACT_SYSTEM
    assert "只抽本段新埋下的" in _EXTRACT_SYSTEM
    assert "不要照抄" in _EXTRACT_SYSTEM


def test_batch_extraction_puts_known_names_in_system_message():
    """名单必须进 **system**（放 user 里实测复用率不够）。"""
    from app.kg.extractor_v3 import extract_batch

    calls: list[tuple[str, str]] = []

    class _Provider:
        def chat(self, system: str, user: str) -> str:
            calls.append((system, user))
            return "{}"

    extract_batch(_Provider(), [(1, "正文", 1)], known_foreshadows=["断刀的来历"])
    assert calls, "没调用 provider"
    system, user = calls[0]
    assert "断刀的来历" in system, "名单没进 system 消息"
    assert "断刀的来历" not in user, "名单不该重复塞进 user 消息"


@pytest.fixture()
def db_session():
    from app.db import Base, SessionLocal, engine
    import app.models  # noqa: F401

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_active_foreshadow_names_only_returns_unresolved(db_session):
    """只取 planted / developing；已回收/已丢弃的不再要求模型沿用。"""
    from app.kg.batch_worker import _active_foreshadow_names
    from app.models import KgForeshadow, Project

    project = Project(title="伏笔名单项目", target_length=3000,
                      generation_length=1200, genre="都市异能")
    db_session.add(project)
    db_session.flush()
    for name, status in (("甲线索", "planted"), ("乙线索", "developing"),
                         ("丙线索", "paid_off"), ("丁线索", "dropped")):
        db_session.add(KgForeshadow(project_id=project.id, name=name, status=status))
    db_session.commit()

    names = _active_foreshadow_names(db_session, project.id)
    assert "甲线索" in names and "乙线索" in names
    assert "丙线索" not in names and "丁线索" not in names
