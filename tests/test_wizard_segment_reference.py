"""引导「段数」步不再把改写指代当答案（2026-11-11）。

## 被钉住的缺陷

用户在 `chapters` 步说「把第 3 段写紧一点」时，`_match` 的 `first_number`
取到 3，被当成「这次写几段」作答并推进流程，`answers.chapters=3` 落库。
用户看到的是「好，这次连着写 3 段」——**话说了，但被吞了**。

## 判据

「第 N 段」= 指代已有/指定段落；「这次写 3 段 / 写 3 段 / 3 段」= 数量。
前者在 `chapters` / `length` 步（这两步问的正是数量/字数）以及任何带改写
动词的句子里，都必须走 clarify 并给出诚实回应，而不是静默吞掉。

## 本文件守护的

  B1-1  `chapters` 步「把第 3 段…」「第 2 段…」→ clarify，且不产生答案。
  B1-2  无正文时回应必须明确说「现在还没有可改写的正文」。
  B1-3  有正文时指到工作台助手重写路径；段号不存在时如实说明。
  B1-4  「这次写 3 段 / 写 3 段 / 3 段 / 写一段」仍正常作答。
  B1-5  `length` 步同类数字误用同样被拦，正常字数不受影响。
  B1-6  `continue_from` 纯段号仍作答；带改写动词则 clarify。
  B1-7  真实 `/say` 接口：不再落 `answers.chapters`、不推进步骤；
        再说「这次写 3 段」仍正常推进。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import wizard_chat as chat  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import AssistantWizardSession, Project  # noqa: E402


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


def _parse(step: str, text: str, segments: list[int] | None = None):
    return chat.parse(
        step,
        text,
        {"branch": "original"},
        {"segments": segments or []},
        {},
    )


# ---------------------------------------------------------------------------
# B1-1 / B1-2：改写指代不能被当成段数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "把第 3 段写紧一点",
        "把第 2 段重写，别让主角赢",
        "第 3 段要收得更快",
        "把第三段删掉两句话",
    ],
)
def test_chapters_step_segment_reference_is_clarified(text):
    out = _parse("chapters", text)
    assert out["kind"] == "clarify", (
        f"「{text}」被当成了段数答案并推进流程：{out}"
    )
    assert "这次写几段" in out["message"], out["message"]
    assert "现在还没有可改写的正文" in out["message"], (
        "无正文时没有说清「没得改」，用户会以为系统能改："
        f"{out['message']}"
    )


def test_chapters_step_rewrite_reference_with_existing_segments_points_to_workbench():
    out = _parse("chapters", "把第 3 段写紧一点", segments=[1, 2, 3])
    assert out["kind"] == "clarify"
    assert "第 3 段已经有正文" in out["message"], out["message"]
    assert "工作台助手" in out["message"], (
        "有正文时没有指到重写卡路径，用户不知道怎么改：" f"{out['message']}"
    )


def test_chapters_step_reference_to_missing_segment_says_no_text():
    out = _parse("chapters", "把第 5 段写紧一点", segments=[1, 2])
    assert out["kind"] == "clarify"
    assert "第 5 段还没有正文" in out["message"], out["message"]
    assert "第 1 段" in out["message"] and "第 2 段" in out["message"], (
        f"没有如实列出已有正文段号：{out['message']}"
    )


# ---------------------------------------------------------------------------
# B1-4：真正的段数作答不受影响
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这次写 3 段", 3),
        ("写 3 段", 3),
        ("3 段", 3),
        ("写一段", 1),
    ],
)
def test_chapters_step_quantity_answers_still_work(text, expected):
    out = _parse("chapters", text)
    assert out["kind"] == "answer", f"「{text}」没有被当成段数作答：{out}"
    assert out["value"] == expected, out


# ---------------------------------------------------------------------------
# B1-5：length 步同类问题
# ---------------------------------------------------------------------------


def test_length_step_segment_reference_is_clarified():
    out = _parse("length", "第 3 段写紧一点")
    assert out["kind"] == "clarify", f"length 步仍把段号当成字数：{out}"
    assert "每段多少字" in out["message"], out["message"]


def test_length_step_real_char_count_still_works():
    out = _parse("length", "3000 字")
    assert out["kind"] == "answer", out
    assert out["value"] == 3000, out


# ---------------------------------------------------------------------------
# B1-6：continue_from 的纯段号要放行
# ---------------------------------------------------------------------------


def test_continue_from_plain_segment_number_still_answers():
    out = _parse("continue_from", "接在第 3 段之后", segments=[1, 2, 3])
    assert out["kind"] == "answer", out
    assert out["value"] == 3, out


def test_continue_from_rewrite_reference_is_clarified():
    out = _parse("continue_from", "把第 3 段重写", segments=[1, 2, 3])
    assert out["kind"] == "clarify", out


# ---------------------------------------------------------------------------
# B1-7：真实 /say 接口（一次性项目）
# ---------------------------------------------------------------------------


def _make_project(db) -> Project:
    proj = Project(
        title="引导段数一次性项目",
        status="created",
        mode="quick",
        generation_length=3000,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _make_chapters_session(db, proj: Project) -> AssistantWizardSession:
    session = AssistantWizardSession(
        project_id=proj.id,
        kind="original",
        step_key="chapters",
        # corpus 必须已答（空列表 = 不投喂语料），否则 `_state` 的自愈会
        # 把会话退回第一个未答步骤，测不到段数这一步。
        answers={
            "branch": "original",
            "corpus": [],
            "genre": "悬疑",
            "style": "番茄",
            "length": 3000,
        },
        history=["branch", "corpus", "genre", "style", "length"],
        overrides={},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def test_say_does_not_swallow_rewrite_instruction_as_chapter_count(db_session):
    proj = _make_project(db_session)
    session = _make_chapters_session(db_session, proj)

    client = TestClient(fastapi_app)
    resp = client.post(
        f"/api/assistant/wizard/{proj.id}/say",
        json={"text": "把第 3 段写紧一点"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "clarify", body
    assert "现在还没有可改写的正文" in body["message"], body["message"]
    assert body["session"]["step_key"] == "chapters", "引导步骤被错误推进了"
    assert "chapters" not in (body["session"]["answers"] or {}), (
        f"改写指代被写进了 answers.chapters：{body['session']['answers']}"
    )

    # 再说真正的段数，必须正常落进 answers.chapters
    resp2 = client.post(
        f"/api/assistant/wizard/{proj.id}/say",
        json={"text": "这次写 3 段"},
    )
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert (body2["session"]["answers"] or {}).get("chapters") == 3, body2

    # 纯数量作答仍走正常作答路径并推进到下一步
    resp3 = client.post(
        f"/api/assistant/wizard/{proj.id}/say",
        json={"text": "3 段"},
    )
    assert resp3.status_code == 200, resp3.text
    body3 = resp3.json()
    assert body3["mode"] == "answered", body3
    assert (body3["session"]["answers"] or {}).get("chapters") == 3, body3
    assert body3["session"]["step_key"] != "chapters", "作答后仍停在段数步"
