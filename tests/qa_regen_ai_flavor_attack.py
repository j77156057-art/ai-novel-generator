"""重生成 AI 腔检测+告警（方案 b）对抗式验证。

独立 QA 视角，**不轻信实现方自述**，直接驱动真实 `regenerate_segment_stream`
+ 确定性 `CountingStubProvider`（记录 chat/summarize/stream 调用次数），断言：

  ① 阈值边界：恰好 5 命中必告警、恰好 4 命中必不告警（off-by-one 防护）。
  ② 核心不变量①：重生成**没重写** —— 落库 segment.text 一字节不差等于 stub 原始文本。
  ③ 核心不变量②：零额外 LLM 调用 —— regen 绝不调 `chat()`（方案 c 自动重写才会调）。
  ④ 告警信号可 JSON 序列化（无不可序列化对象混入）。
  ⑤ reasons 形态：非空 list 且元素均为 str。
  ⑥ 干净文本稳健：rewrite_info 为 None、text 不变、chat_call_count == 0。
  ⑦（best-effort）反向禁忌路径：经真实缓存路径注入 narrative.forbidden，
     禁忌词重复 2 次必告警、重复 1 次必不告警。

门禁单源：本文件即对抗验证唯一入口。不 mock 被测逻辑（仅确定性 stub provider 与
narrative 取数被替换，narrative 取数用真实缓存命中路径注入 forbidden，零 LLM）。
"""
from __future__ import annotations

import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    AI_FLAVOR_PATTERNS,
    regenerate_segment_stream,
)
from app.kg.build import merge_segment_into_kg  # noqa: E402
from app.models import Generation, Project  # noqa: E402
from app.style.narrative import narrative_fingerprint, resolve_tier  # noqa: E402


# 干净散文：不含任何 AI_FLAVOR_PATTERNS 串、也不含反向禁忌
CLEAN_TEXT = "林夜走在长街上。夜风微凉，街灯昏黄。他停步回望，身影被拉得很长。"

# 反向禁忌用词：刻意选 5 字生僻组合，不可能命中任何 AI_FLAVOR_PATTERNS，也不在散文里
FORBIDDEN_TERM = "紫水晶吊坠"


class CountingStubProvider:
    """确定性假 provider，且**计数每种调用的次数**（对抗「误调 chat」红线）。

    - stream_chat 一次性 yield 整段文本（与既有流式测试同手法）
    - summarize 返固定串
    - chat 备齐且计数（regen 路径严禁调用；若被调说明误触发方案 c 自动重写）
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.chat_calls = 0
        self.summarize_calls = 0
        self.stream_calls = 0

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        self.chat_calls += 1
        return self.text

    def summarize(self, text: str) -> str:
        self.summarize_calls += 1
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        self.stream_calls += 1
        yield self.text


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


def _make_project(db, title: str = "QA 重生成 AI 腔对抗") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料直接放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _make_project_with_forbidden(db, forbidden: dict, title: str = "QA 反向禁忌路径") -> Project:
    """经**真实缓存命中路径**注入 narrative.forbidden，零 LLM、零语料加载。

    带 reference_novel_ids=[1] 让 _require_distill_ready / project_narrative 走到
    指纹校验分支；把 style_narrative 的 fingerprint 算成与代码同款（narrative_fingerprint
    + resolve_tier(project.narrative_tier)），从而命中项目缓存、直接返回 forbidden，
    既不调 LLM 也不全量加载语料（不触发方案 c）。
    """
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[1],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    cfg = resolve_tier(getattr(proj, "narrative_tier", None))  # None → 默认完整档
    fp = narrative_fingerprint([1], cfg)
    proj.style_narrative = {"fingerprint": fp, "forbidden": forbidden}
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project, segment_no: int, text: str) -> None:
    """按「生成」等价效果落库：写 Generation 行 + 走段级合并。"""
    db.add(
        Generation(
            project_id=project.id,
            segment_no=segment_no,
            text=text,
            summary={"text": f"段{segment_no}摘要"},
        )
    )
    db.commit()
    merge_segment_into_kg(db, project.id, text, segment_no, source="generated")


def _read_segment_text(db, project, segment_no: int) -> str:
    g = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id, Generation.segment_no == segment_no)
    ).one()
    return g.text


def _drive_regen(db, project, segment_no: int, text: str):
    """驱动一次重生成，返回 (done 事件, 用过的 CountingStubProvider)。"""
    provider = CountingStubProvider(text)
    events = list(
        regenerate_segment_stream(
            db, project, segment_no=segment_no, provider=provider
        )
    )
    done = [e for e in events if e.get("type") == "done"]
    assert done, f"重生成未产出 done 事件；实际事件：{events}"
    return done[0], provider


# ── ① 阈值边界：恰好 5 命中必告警，恰好 4 命中必不告警 ──────────────────────


def test_threshold_exact5_hits_alert(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit5 = AI_FLAVOR_PATTERNS[1] * 5  # count("突然之间") == 5 >= 5
    done, _ = _drive_regen(db_session, proj, 1, hit5)
    ri = done.get("rewrite_info")

    assert ri is not None, f"恰好 5 命中却无 rewrite_info 告警（off-by-one？）：{done}"
    assert ri.get("alert") is True, f"rewrite_info.alert 应为 True：{ri}"
    # 反证：恰好 4 命中不得告警（同模式相邻重复不会让计数错位）
    proj2 = _make_project(db_session)
    _seed_segment(db_session, proj2, 1, "林夜说道：“我回来了。”")
    hit4 = AI_FLAVOR_PATTERNS[1] * 4
    done4, _ = _drive_regen(db_session, proj2, 1, hit4)
    assert done4.get("rewrite_info") is None, (
        f"恰好 4 命中却误告警（off-by-one 反向）：{done4.get('rewrite_info')}"
    )


def test_threshold_exact4_no_alert(db_session):
    """单独用例再钉一遍恰好 4 命中不告警，且 text 未被改写、零 chat。"""
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit4 = AI_FLAVOR_PATTERNS[1] * 4
    done, provider = _drive_regen(db_session, proj, 1, hit4)
    ri = done.get("rewrite_info")

    assert ri is None, f"恰好 4 命中不应告警：{ri}"
    assert provider.chat_calls == 0, f"regen 不应调 chat：chat_calls={provider.chat_calls}"
    assert _read_segment_text(db_session, proj, 1) == hit4, "4 命中时 text 仍应原样落库"


# ── ② 核心不变量①：重生成确实没重写 —— text 一字节不差 ─────────────────────


def test_invariant_no_rewrite_text_unchanged(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit = AI_FLAVOR_PATTERNS[1] * 5  # 命中告警
    done, _ = _drive_regen(db_session, proj, 1, hit)

    assert done.get("rewrite_info") is not None, "前置：本次必须命中告警"
    stored = _read_segment_text(db_session, proj, 1)
    assert stored == hit, (
        f"误触发重写：落库 text 与 stub 原始文本不一致\n"
        f"  原始(len={len(hit)}): {hit!r}\n"
        f"  落库(len={len(stored)}): {stored!r}"
    )


# ── ③ 核心不变量②：零额外 LLM 调用 —— regen 绝不调 chat ────────────────────


def test_invariant_zero_chat_on_hit(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit = AI_FLAVOR_PATTERNS[1] * 5
    done, provider = _drive_regen(db_session, proj, 1, hit)

    assert done.get("rewrite_info") is not None, "前置：本次必须命中告警"
    assert provider.chat_calls == 0, (
        f"命中告警时仍调了 chat（疑似误触发方案 c 自动重写）：chat_calls={provider.chat_calls}"
    )


def test_invariant_zero_chat_on_clean(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    done, provider = _drive_regen(db_session, proj, 1, CLEAN_TEXT)

    assert done.get("rewrite_info") is None, "干净文本不应告警"
    assert provider.chat_calls == 0, (
        f"干净文本重生成也调了 chat：chat_calls={provider.chat_calls}"
    )


# ── ④ 告警信号可 JSON 序列化 ───────────────────────────────────────────────


def test_rewrite_info_json_serializable(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit = AI_FLAVOR_PATTERNS[1] * 5
    done, _ = _drive_regen(db_session, proj, 1, hit)
    ri = done.get("rewrite_info")
    assert ri is not None, "前置：本次必须命中告警"

    try:
        dumped = json.dumps(ri, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"rewrite_info 含不可序列化对象：{ri!r}") from exc
    assert isinstance(dumped, str) and dumped


# ── ⑤ reasons 形态：非空 list，元素均为 str ───────────────────────────────


def test_rewrite_info_reasons_shape(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    hit = AI_FLAVOR_PATTERNS[1] * 5
    done, _ = _drive_regen(db_session, proj, 1, hit)
    ri = done.get("rewrite_info")
    assert ri is not None, "前置：本次必须命中告警"

    reasons = ri.get("reasons")
    assert isinstance(reasons, list), f"reasons 应为 list：{reasons!r}"
    assert len(reasons) > 0, "命中时 reasons 必须非空"
    assert all(isinstance(r, str) for r in reasons), f"reasons 元素必须全为 str：{reasons!r}"


# ── ⑥ 干净文本稳健：rewrite_info None、text 不变、零 chat ──────────────────


def test_clean_text_robust(db_session):
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    done, provider = _drive_regen(db_session, proj, 1, CLEAN_TEXT)
    ri = done.get("rewrite_info")

    assert ri is None, f"干净文本不应触发告警：{ri}"
    assert _read_segment_text(db_session, proj, 1) == CLEAN_TEXT, "干净文本 text 应原样落库"
    assert provider.chat_calls == 0, f"干净文本不应调 chat：chat_calls={provider.chat_calls}"


# ── ⑦（best-effort）反向禁忌路径：经真实缓存命中注入 forbidden ──────────────


def test_forbidden_exact2_alerts_exact1_no_alert(db_session):
    forbidden = {"禁用转折词": [FORBIDDEN_TERM]}
    proj = _make_project_with_forbidden(db_session, forbidden)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    # 文本无 AI 腔模式，仅含禁忌词重复 2 次 → 应告警
    text2 = f"林夜握紧了{FORBIDDEN_TERM}。月光下，他举起{FORBIDDEN_TERM}。"
    assert text2.count(FORBIDDEN_TERM) == 2
    # 反证：重复 1 次 → 不告警
    text1 = f"林夜握紧了{FORBIDDEN_TERM}。月光洒在肩头。"
    assert text1.count(FORBIDDEN_TERM) == 1

    done2, provider2 = _drive_regen(db_session, proj, 1, text2)
    ri2 = done2.get("rewrite_info")
    assert ri2 is not None, f"禁忌词重复 2 次却未告警：{done2}"
    assert ri2.get("alert") is True, f"rewrite_info.alert 应为 True：{ri2}"
    assert any(FORBIDDEN_TERM in r for r in (ri2.get("reasons") or [])), (
        f"reasons 应包含禁忌词依据：{ri2.get('reasons')}"
    )
    # 红线：触发告警也绝不做自动重写 / 调 chat
    assert provider2.chat_calls == 0, f"禁忌告警也调了 chat：chat_calls={provider2.chat_calls}"
    assert _read_segment_text(db_session, proj, 1) == text2, "禁忌告警 text 应原样落库（不重写）"

    # 同 project 再跑一次：重复 1 次 → 不告警（且仍零 chat、text 不变）
    done1, provider1 = _drive_regen(db_session, proj, 1, text1)
    assert done1.get("rewrite_info") is None, f"禁忌词重复 1 次不应告警：{done1.get('rewrite_info')}"
    assert provider1.chat_calls == 0, f"禁忌 1 次也不应调 chat：chat_calls={provider1.chat_calls}"
    assert _read_segment_text(db_session, proj, 1) == text1, "禁忌 1 次 text 应原样落库"
