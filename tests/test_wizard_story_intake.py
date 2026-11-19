"""对话式创建：agent 主动追问 + 自主扩展（2026-11-14）。

## 被钉住的行为

用户只说「我要写一篇小说」时，引导不能直接套默认配置往下生产，而要先把
**题材 / 写法 / 核心冲突 / 主角 / 篇幅与更新** 问清；题材还会决定额外追问
（科幻问科技上限、仙侠问力量体系、历史问时代落点……）。

## 本文件守护的

  S1  只说「我要写一篇小说」→ 识别为原创，下一题是题材，不是默认开写。
  S2  「写个科幻悬疑」→ 题材可多选；追问计划切到科幻族（先问世界观）。
  S3  追问顺序随题材变化：悬疑先问冲突、仙侠先问力量体系、言情先问主角、
      历史先问时代落点；未知题材走通用集合。
  S4  模糊回答（随便/都行）在故事问题上给 2–3 个方向，不落默认、不推进。
  S5  参数问题（字数）的「随便」仍走原默认行为（不退化）。
  S6  「按默认来 / 别问了」直接补齐并推进到确认页，不把流程问死。
  S7  一句话给多个信息 → 一次性预填，后续跳过，不重复问。
  S8  端到端：一句话开始 → 最终 premise / target_length / 大纲都落到项目。
  S9  代码与提示词不含任何一本书的专名，换任何一本书都成立。
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
from app import wizard_flow as flow  # noqa: E402
from app import wizard_story as story  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import Project  # noqa: E402

KEYS = tuple(flow.GENRE_HINTS)
CTX = {"generation_length": 3000, "segments": []}


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


def _parse(step: str, text: str, answers: dict | None = None):
    return chat.parse(step, text, answers or {"branch": "original"}, dict(CTX), {})


def _project(db) -> Project:
    proj = Project(
        title="引导追问一次性项目",
        status="created",
        mode="quick",
        generation_length=3000,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _start(client: TestClient, project_id: int) -> dict:
    resp = client.post("/api/assistant/wizard/start", json={"project_id": project_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _say(client: TestClient, project_id: int, text: str) -> dict:
    resp = client.post(
        f"/api/assistant/wizard/{project_id}/say", json={"text": text}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# S1 / S2：从一句话开始
# ---------------------------------------------------------------------------


def test_bare_write_novel_is_original_and_asks_corpus():
    out = _parse("branch", "我要写一篇小说", {})
    assert out["kind"] == "answer", out
    assert out["value"] == flow.KIND_ORIGINAL, out

    answers = {"branch": flow.KIND_ORIGINAL}
    nxt = flow.next_step(flow.KIND_ORIGINAL, "branch", answers, dict(CTX))
    assert nxt == "corpus", f"下一题不是语料，而是 {nxt}"


def test_genre_word_without_novel_word_is_original():
    out = _parse("branch", "写个科幻悬疑", {})
    assert out["kind"] == "answer", out
    assert out["value"] == flow.KIND_ORIGINAL, out


def test_sci_fi_mystery_switches_plan_to_sci_fi_family():
    sig = story.read_signals("写个科幻悬疑", KEYS, tuple(flow.STYLE_HINTS))
    answers = story.prefill({}, sig)
    assert answers["genre"] == "科幻", answers
    assert answers["genre_tags"] == ["科幻", "悬疑"], answers

    plan = story.plan_steps(answers, KEYS)
    assert plan[0] == "world", f"科幻族应先问世界观/科技上限：{plan}"
    assert "power" in plan and "premise" in plan and "scale" in plan, plan
    assert plan[-1] == "scale", plan


# ---------------------------------------------------------------------------
# S3：题材改变追问顺序
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("genre", "first"),
    [
        ("悬疑", "premise"),
        ("仙侠", "power"),
        ("言情", "protagonist"),
        ("历史", "world"),
        ("都市", "premise"),
    ],
)
def test_plan_order_depends_on_genre(genre, first):
    plan = story.plan_steps({"genre": genre}, KEYS)
    assert plan[0] == first, f"{genre} 族的第一问应是 {first}：{plan}"


def test_unknown_genre_uses_generic_plan():
    plan = story.plan_steps({}, KEYS)
    assert plan[0] == "pov", plan
    assert set(plan) == {"pov", "premise", "protagonist", "world", "scale"}, plan


def test_world_question_text_is_genre_flavoured():
    sci = flow.build_question("world", {"branch": "original", "genre": "科幻"}, dict(CTX))
    mys = flow.build_question("world", {"branch": "original", "genre": "悬疑"}, dict(CTX))
    assert "科技" in sci["card"]["default"], sci
    assert "时间线" in mys["card"]["default"] or "线索" in mys["card"]["default"], mys


# ---------------------------------------------------------------------------
# S4 / S5：模糊回答
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["genre", "pov", "premise", "protagonist", "world", "power", "scale"])
def test_vague_answer_at_story_step_offers_options(step):
    out = _parse(step, "随便", {"branch": "original", "genre": "科幻"})
    assert out["kind"] == "clarify", f"{step} 步把「随便」当答案了：{out}"
    assert "没法当成答案" in out["message"], out["message"]
    assert "1." in out["message"] and "2." in out["message"], out["message"]


def test_vague_answer_at_length_still_uses_default():
    out = _parse("length", "随便", {"branch": "original", "genre": "悬疑"})
    assert out["kind"] == "answer", f"字数步的「随便」不该被追问：{out}"
    assert out["value"] == 3000, out


def test_vague_word_with_concrete_genre_is_not_vague():
    out = _parse("genre", "随便写个科幻", {"branch": "original"})
    assert out["kind"] == "answer", out
    assert out["value"] == "科幻", out


# ---------------------------------------------------------------------------
# S6：明确要求按默认来
# ---------------------------------------------------------------------------


def test_bypass_is_recognised_at_story_step():
    out = _parse("premise", "别问了", {"branch": "original", "genre": "科幻"})
    assert out["kind"] == "bypass", out


def test_bypass_fills_defaults_and_reaches_confirm(db_session):
    proj = _project(db_session)
    client = TestClient(fastapi_app)
    _start(client, proj.id)
    assert _say(client, proj.id, "我要写一篇小说")["session"]["step_key"] == "corpus"

    body = _say(client, proj.id, "别问了")
    assert body["mode"] == "answered", body
    session = body["session"]
    assert session["step_key"] == "confirm", session
    answers = session["answers"]
    required = ["genre"] + story.plan_steps(answers, KEYS)
    for step in required:
        assert step in answers, f"{step} 没有被默认补齐：{answers}"
    assert "scale" in (answers.get("_auto") or []), answers


# ---------------------------------------------------------------------------
# S7：一句话里的多个信息一次性吸收
# ---------------------------------------------------------------------------


def test_compound_sentence_prefills_and_skips_answered_steps(db_session):
    proj = _project(db_session)
    client = TestClient(fastapi_app)
    _start(client, proj.id)
    _say(client, proj.id, "我要写一篇小说")

    body = _say(
        client,
        proj.id,
        "写个科幻悬疑，第一人称，主角叫林深，30 万字，日更",
    )
    assert body["mode"] == "answered", body
    answers = body["session"]["answers"]
    assert answers["genre"] == "科幻", answers
    assert answers["pov"] == "第一人称", answers
    assert answers["protagonist"].startswith("林深"), answers
    assert "30 万字" in answers["scale"], answers
    auto = set(answers.get("_auto") or [])
    assert {"pov", "protagonist", "scale"} <= auto, answers
    # 还差世界观/核心冲突 → 下一题应是 world，而不是回到已经说过的题材/写法。
    assert body["session"]["step_key"] == "world", body["session"]
    assert "已记下" in (body.get("ack") or ""), body.get("ack")


# ---------------------------------------------------------------------------
# S8：端到端 —— 一句话开始，最终落到设定与大纲
# ---------------------------------------------------------------------------


def test_end_to_end_from_one_sentence_to_premise_and_bible(db_session):
    proj = _project(db_session)
    client = TestClient(fastapi_app)
    _start(client, proj.id)

    assert _say(client, proj.id, "我要写一篇小说")["session"]["step_key"] == "corpus"

    body = _say(
        client,
        proj.id,
        "写个科幻悬疑，第一人称，主角叫林深，30 万字，日更，"
        "世界观约束是近未来科技只比现实多一小步，"
        "金手指边界是用一次要付代价，"
        "核心冲突是他要查清一桩被压下去的旧案",
    )
    assert body["mode"] == "answered", body
    answers = body["session"]["answers"]
    assert answers["genre"] == "科幻", answers
    assert answers["genre_tags"] == ["科幻", "悬疑"], answers
    for step in ("pov", "premise", "protagonist", "world", "power", "scale"):
        assert step in answers, f"{step} 未被一句话吸收：{answers}"
        assert step in (answers.get("_auto") or []), f"{step} 没进 _auto：{answers}"
    # 故事信息已齐；语料在同一句按「不投喂」落库 → 直接进入风格参数。
    assert answers["corpus"] == [], answers
    assert body["session"]["step_key"] == "style", body["session"]
    assert _say(client, proj.id, "番茄")["session"]["step_key"] == "length"
    assert _say(client, proj.id, "3000 字")["session"]["step_key"] == "chapters"
    resp = client.post(
        f"/api/assistant/wizard/{proj.id}/answer",
        json={"step_key": "chapters", "value": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session"]["step_key"] == "outline"
    resp = client.post(
        f"/api/assistant/wizard/{proj.id}/answer",
        json={"step_key": "outline", "value": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["session"]["step_key"] == "confirm"

    resp = client.post(
        f"/api/assistant/wizard/{proj.id}/answer",
        json={"step_key": "confirm", "value": True},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]

    db_session.expire_all()
    saved = db_session.get(Project, proj.id)
    assert saved.genre == "科幻", saved.genre
    assert saved.target_length == 300000, saved.target_length
    assert saved.generation_length == 3000, saved.generation_length
    assert "【创作问答】" in (saved.premise or ""), saved.premise
    for must in ("题材：科幻", "写法：第一人称", "主角：林深", "核心冲突：", "篇幅与更新：30 万字"):
        assert must in (saved.premise or ""), f"premise 缺 {must}：{saved.premise}"
    assert result.get("bible"), result
    assert "premise" in (result.get("applied") or []), result
    assert "target_length" in (result.get("applied") or []), result


# ---------------------------------------------------------------------------
# S9：不写死任何一本书
# ---------------------------------------------------------------------------


def test_code_has_no_book_specific_names():
    root = os.path.abspath(os.path.join(_BACKEND, "app"))
    files = [
        os.path.join(root, "wizard_story.py"),
        os.path.join(root, "wizard_flow.py"),
        os.path.join(root, "wizard_chat.py"),
        os.path.join(root, "routers", "assistant_wizard.py"),
    ]
    forbidden = ("沈砚", "苏晚", "程野", "周明远", "顾清和", "大奉打更人")
    for path in files:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        for name in forbidden:
            assert name not in text, f"{os.path.basename(path)} 写死了 {name}"


def test_sufficiency_requires_all_plan_steps():
    answers = {"genre": "科幻"}
    assert not story.is_sufficient(answers, KEYS)
    for step in story.plan_steps(answers, KEYS):
        answers[step] = "x"
    assert story.is_sufficient(answers, KEYS)
