"""读工具的**分页**能力：长正文不能只给开头几百字。

## 为什么要有分页

ReAct 的观察有字符上限，一段长正文的尾部会被裁掉（真书第 2 段 3238 字 /
141 段，观察里只剩约 500 字）。没有分页口子的模型会**用同一组参数反复读**，
拿到同一段被裁过的内容，直到「连续 5 次相同调用」的防循环闸门把它掐掉。

实测（真书项目 2 + 真模型 MiniMax-M3）：agent 拿到了诊断结论（issue id 与
目标段落都对），也写出了改写草稿，却因为读不到后半段正文而反复重读，
最后**没能起草改写卡**。给了 start_char / max_chars 之后它能接着读。

要守住的：
1. 分页能接上（读第 2 页拿到的是第 1 页之后的内容，不是重复）；
2. 返回里说清「读到哪了、还剩多少」（模型才知道要不要继续读）；
3. 越界不炸（start_char 超过正文长度 → 空文本 + text_more=False）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Generation, Project  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    """每个用例前把表建好（conftest 只建库不建表）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


def _seed(db, text: str) -> int:
    proj = Project(title="分页读取测试", target_length=5000, mode="quick", status="created")
    db.add(proj)
    db.commit()
    db.refresh(proj)
    gen = Generation(project_id=proj.id, segment_no=1, chapter_range="1-1",
                     text=text, summary={"short": "摘要"})
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return proj.id


def _read(project_id: int, **kwargs):
    from app.agents.registry import _impl_read_generation_segments
    db = SessionLocal()
    try:
        return _impl_read_generation_segments(db=db, project_id=project_id, **kwargs)
    finally:
        db.close()


def test_the_first_page_comes_with_position_information():
    db = SessionLocal()
    try:
        pid = _seed(db, "甲" * 1000 + "乙" * 1000)
    finally:
        db.close()
    out = _read(pid, segment_no=1, max_chars=400)
    seg = out["segments"][0]
    assert seg["text"] == "甲" * 400
    assert seg["text_total"] == 2000
    assert seg["text_from"] == 0
    assert seg["text_to"] == 400
    assert seg["text_more"] is True


def test_the_second_page_continues_where_the_first_stopped():
    db = SessionLocal()
    try:
        pid = _seed(db, "甲" * 1000 + "乙" * 1000)
    finally:
        db.close()
    first = _read(pid, segment_no=1, max_chars=400)["segments"][0]
    second = _read(pid, segment_no=1, start_char=first["text_to"],
                   max_chars=700)["segments"][0]
    assert second["text_from"] == 400
    assert second["text"] == ("甲" * 600) + ("乙" * 100)
    assert second["text_more"] is True
    # 两页拼起来必须是原文的**连续前缀**，不是各自从头读
    assert first["text"] + second["text"] == ("甲" * 1000 + "乙" * 1000)[:1100]


def test_the_last_page_says_there_is_no_more():
    db = SessionLocal()
    try:
        pid = _seed(db, "甲" * 1000)
    finally:
        db.close()
    out = _read(pid, segment_no=1, start_char=900, max_chars=400)["segments"][0]
    assert out["text"] == "甲" * 100
    assert out["text_more"] is False


def test_reading_past_the_end_is_not_an_error():
    db = SessionLocal()
    try:
        pid = _seed(db, "甲" * 100)
    finally:
        db.close()
    out = _read(pid, segment_no=1, start_char=500)["segments"][0]
    assert out["text"] == ""
    assert out["text_more"] is False
    assert out["text_total"] == 100


def test_without_paging_the_whole_text_is_returned():
    db = SessionLocal()
    try:
        pid = _seed(db, "甲" * 1000)
    finally:
        db.close()
    out = _read(pid, segment_no=1)["segments"][0]
    assert out["text"] == "甲" * 1000
    assert out["text_from"] == 0
    assert out["text_more"] is False
