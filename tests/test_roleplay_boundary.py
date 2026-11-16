"""角色对话的**边界**：他只能知道到「故事进行到第几章」为止的事（2026-10-28）。

## 为什么要有这条

界面上有「故事进行到第几章」，项目里也存了 `reference_cutoffs`，
但**角色对话的检索根本没看它** —— 实测项目 #1 填了「第 1 章」，
问「老夫子性格怎么样」，证据来自**第 290 / 791 / 981 章**：
角色知道了用户还没读到的剧情 ⇒ **剧透**。

而且没有边界时，界面写着「没有边界就等于整本不可见 —— 他会『什么都不知道』」，
实际却是**整本都给**（与承诺相反）。

⇒ 修法：检索与时间线都按边界裁；**没有边界的书整本挡掉**（fail closed）。
"""
from __future__ import annotations

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.roleplay.bio import character_timeline  # noqa: E402
from app.roleplay.chat import _project_cutoffs  # noqa: E402
from app.roleplay.evidence import gather_evidence  # noqa: E402


class _Project:
    def __init__(self, cutoffs=None, novels=(1,)):
        self.id = 1
        self.title = "测试"
        self.reference_novel_ids = list(novels)
        self.reference_cutoffs = cutoffs


def test_project_cutoffs_reads_only_valid_entries():
    """只认「已选语料 + 正整数章号」这一种形状。"""
    proj = _Project(
        cutoffs=[
            {"novel_id": 1, "chapter": 12},
            {"novel_id": 1, "chapter": 0},      # 非法章号
            {"novel_id": 1, "chapter": "12"},    # 类型不对
            {"novel_id": 9, "chapter": 3},       # 不是这个项目选的语料
            {"chapter": 3},                      # 缺 novel_id
            "垃圾数据",
        ],
        novels=(1,),
    )
    assert _project_cutoffs(proj, [1]) == {1: 12}


def test_gather_evidence_without_cutoff_returns_nothing():
    """**没有边界 ⇒ 整本挡掉**（fail closed，与界面上的说明一致）。"""
    class _DB:
        def query(self, *a, **k):
            raise AssertionError("没有边界就不该查库")

    out = gather_evidence(_DB(), _Project(cutoffs=None), [1], "老夫子", k=6, cutoffs={})
    assert out == []


def test_gather_evidence_passes_chapter_limit_to_retrieval(monkeypatch):
    """有边界 ⇒ 只检索那本书、且只到边界章。

    证据走「参考书 / 用户自己写的正文」分流（`retrieve_for_character_auto`），
    所以这里 mock 的是它 —— 它把边界交给参考书那一侧。
    """
    seen: list[tuple] = []

    def _fake_auto(db, project, novel_ids, name, k=6, cutoffs=None):
        seen.append((tuple(novel_ids or []), name, k, dict(cutoffs or {})))
        return []

    import app.retrieval.strategies as strategies
    monkeypatch.setattr(strategies, "retrieve_for_character_auto", _fake_auto)

    out = gather_evidence(
        None, _Project(), [1, 2], "老夫子", k=3, cutoffs={1: 7},
    )
    assert out == []
    # 边界只给第 1 本；第 2 本没有边界 ⇒ 由 auto 版自己跳过（fail closed）
    assert seen == [((1, 2), "老夫子", 3, {1: 7})], seen


def test_character_timeline_respects_chapter_limit():
    """生平时间线也受边界约束 —— 越界的事件同样是剧透。"""
    class _Chunk:
        def __init__(self, chapter_no, text):
            self.chapter_no = chapter_no
            self.text = text
        def __init_subclass__(cls, **kw):
            pass

    class _Q:
        def filter(self, *a, **k):
            return self
        def order_by(self, *a, **k):
            return self
        def all(self):
            return [
                _Chunk(3, "老夫子拍桌子道：“荒诞！”"),
                _Chunk(9, "老夫子怒极，颤声道：“好一个丧家犬！”"),
            ]

    class _DB:
        def query(self, *a, **k):
            return _Q()

    tl = character_timeline(_DB(), 1, "老夫子", max_chapter=5)
    assert all(t["chapter_no"] <= 5 for t in tl), tl


def test_prompt_and_docs_state_the_boundary():
    """提示词里要说明「你只知道到这一章为止的事」。"""
    from app.roleplay.chat import SYSTEM_TEMPLATE

    assert "这一章" in SYSTEM_TEMPLATE or "边界" in SYSTEM_TEMPLATE


def test_original_character_evidence_ignores_reference_cutoff():
    """**原创角色**的证据来自用户自己写的正文 ⇒ 不受参考书边界约束。

    实测（2026-10-28）：先把证据只查参考书，于是原创角色（沈砚）永远 0 段证据
    —— 界面上「他依据的原文」是空的，而且零证据兜底会把他的回答也一起废掉。
    ⇒ 证据必须走「参考书 / 生成正文」分流（`retrieve_for_character_auto`），
    边界**只**加在参考书那一侧。
    """
    from app.roleplay.evidence import gather_evidence

    class _Gen:
        id = 1
        segment_no = 1
        text = "沈砚握紧短刃，沉声道：“守灯的人不会回头。”"
        summary = None

    class _Scalars:
        def all(self):
            return [_Gen()]

    class _DB:
        def scalars(self, *a, **k):
            return _Scalars()
        def scalar(self, *a, **k):
            return 0          # 参考书里没有他 ⇒ 走生成正文那一侧

    class _P:
        id = 1
        title = "测试"
        reference_novel_ids = [1]
        reference_cutoffs = [{"novel_id": 1, "chapter": 1}]

    srcs = gather_evidence(_DB(), _P(), [1], "沈砚", k=6, cutoffs={1: 1})
    assert srcs, "原创角色应当从用户自己写的正文里取到证据"
    assert "守灯的人不会回头" in srcs[0].text
    from app.roleplay.chat import NO_EVIDENCE_ANSWER, run_chat

    class _Provider:
        def __init__(self):
            self.called = False

        def chat(self, system, user):
            self.called = True
            return "模型编的答案"

    class _P:
        id = 1
        title = "测试"
        reference_novel_ids = [1]
        reference_cutoffs = [{"novel_id": 1, "chapter": 1}]

    provider = _Provider()
    out = run_chat(None, _P(), "老夫子", "你认得赵长安吗？", novel_ids=[1], k=6, provider=provider)
    assert provider.called is False, "零证据时不该调模型"
    assert out["has_evidence"] is False
    assert out["answer"] == NO_EVIDENCE_ANSWER
    assert "模型编的答案" not in out["answer"]
