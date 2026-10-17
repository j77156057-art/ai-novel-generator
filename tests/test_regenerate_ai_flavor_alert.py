"""重生成路径零成本 AI 腔检测 + 告警（方案 b）回归测试。

对应评估文档 §4.2 ③ 方案 b：重生成（`regenerate_segment_stream`）生成后**只检测 + 告警**，
不自动重写（自动重写 B 路会全量加载语料，属方案 c，本次不做）。done 事件须携带
`rewrite_info` 告警信号：命中时 `alert:true` 且 `rewritten_chars:null`；未命中则为 None。

复用 `test_regenerate_kg_replay.py` 的 harness（project + 已有 segment + StubProvider）。
零 LLM 调用、不加载语料：StubProvider 确定性返回，project 无参考语料（forbidden 为空）。

测试真实调用 `regenerate_segment_stream`，不 mock 被测逻辑。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    AI_FLAVOR_PATTERNS,
    regenerate_segment_stream,
)
from app.kg.build import merge_segment_into_kg  # noqa: E402
from app.models import Generation, Project  # noqa: E402


# 干净散文：不含任何 AI_FLAVOR_PATTERNS 串、也不含反向禁忌
CLEAN_TEXT = "林夜走在长街上。夜风微凉，街灯昏黄。他停步回望，身影被拉得很长。"


class StubProvider:
    """确定性假 provider：stream_chat 返回完整正文，summarize 返回固定串，chat 备齐以防属性缺失。

    不实现 `summarize_rollup`——让 engine 的 `_rollup_fn` 回落到 LLMProvider 的
    确定性兜底（与既有流式测试同一手法）。regen 不调 chat，但 stub 要完整以免属性缺失。
    """

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
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


def _make_project(db, title: str = "重生成 AI 腔告警测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料直接放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed_segment(db, project, segment_no: int, text: str) -> None:
    """按「生成」的等价效果落库：写 Generation 行 + 走段级合并。"""
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


def _collect_done(db, project, segment_no: int, text: str) -> dict:
    """驱动重生成并取回 type=="done" 的事件。"""
    events = list(
        regenerate_segment_stream(
            db, project, segment_no=segment_no, provider=StubProvider(text)
        )
    )
    done = [e for e in events if e.get("type") == "done"]
    assert done, "重生成未产出 done 事件"
    return done[0]


# ---------------------------------------------------------------------------
# 用例 1（命中）：同一 AI 腔模式重复 ≥6 次 → done.rewrite_info 告警
# ---------------------------------------------------------------------------


def test_regen_ai_flavor_alert_when_hit(db_session):
    """重生成文本含同一 AI 腔模式重复 ≥6 次 → done.rewrite_info 不为 None 且是告警形态。

    project 无 narrative（forbidden 为空，避免反向禁忌干扰）。断言：
    rewrite_info 不为 None、alert 为 True、rewritten_chars 为 None、reasons 非空。
    方案 b 只检测 + 告警、不做自动重写（rewritten_chars 恒为 None）。
    """
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    # 取 AI_FLAVOR_PATTERNS 中真实存在的串重复 6 次构造命中文本（count=6 ≥ 5）
    hit_text = AI_FLAVOR_PATTERNS[1] * 6

    done = _collect_done(db_session, proj, 1, hit_text)
    rewrite_info = done.get("rewrite_info")

    assert rewrite_info is not None, f"命中 AI 腔却无 rewrite_info 告警：{done}"
    assert rewrite_info.get("alert") is True, f"rewrite_info.alert 应为 True：{rewrite_info}"
    assert rewrite_info.get("rewritten_chars") is None, (
        f"方案 b 不自动重写，rewritten_chars 应为 None：{rewrite_info}"
    )
    assert rewrite_info.get("reasons"), f"命中时应给出 reasons：{rewrite_info}"
    assert rewrite_info.get("original_chars") == len(hit_text), (
        f"original_chars 应等于原文长度：{rewrite_info}"
    )


# ---------------------------------------------------------------------------
# 用例 2（干净）：普通散文不含任何 AI 腔模式 → done.rewrite_info 为 None
# ---------------------------------------------------------------------------


def test_regen_ai_flavor_no_alert_when_clean(db_session):
    """重生成文本为不含任何 AI 腔模式的普通散文 → done.rewrite_info 为 None。"""
    proj = _make_project(db_session)
    _seed_segment(db_session, proj, 1, "林夜说道：“我回来了。”")

    done = _collect_done(db_session, proj, 1, CLEAN_TEXT)
    assert done.get("rewrite_info") is None, (
        f"干净文本不应触发 rewrite_info 告警：{done.get('rewrite_info')}"
    )
