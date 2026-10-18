"""v2.4：高潮密度语料蒸馏（avg_node_segments）测试。

覆盖：
- classify_chapter_emotion：单章情绪标注 + 容错（```json 包裹/非 JSON/
  缺键/异常/emotion 不在候选集/无文本/候选不在）
- distill_avg_node_segments 纯函数：所有边角 case
  - 空 → 默认 5
  - 全日常 → 12
  - 全高潮 → 3
  - 混合（1/2, 1/4, 1/10 高潮）
  - clamp 边界
- compute_plot_rhythm：
  - 多本合并
  - novel_ids 空 → {}
  - 单本无 chapter → {}
  - 失败章节 → 兜底"日常"
- engine._timeline_state_for 集成：
  - project.plot_rhythm.avg_node_segments 被使用（plan_timeline 实际收到蒸馏值）
  - plot_rhythm 缺/None → 回退默认 5
  - plot_rhythm.avg_node_segments 越界 → 回退默认 5

跑法：python tests/test_v24_rhythm.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base


PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{tag}  {label}: got={got!r} want={want!r}")


class FakeEmotionProvider:
    """可预设回复的假 provider。"""

    def __init__(self, reply: str = '{"emotion": "爽"}', raise_exc: bool = False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.call_count = 0

    def chat(self, system: str, user: str) -> str:
        self.call_count += 1
        if self.raise_exc:
            raise RuntimeError("mock LLM error")
        return self.reply


# ── 组 1：classify_chapter_emotion ──

def test_classify_chapter_emotion_returns():
    print("\n=== 组 1: classify_chapter_emotion ===")
    from app.generation.rhythm import classify_chapter_emotion

    p = FakeEmotionProvider('{"emotion": "爽"}')
    check("情绪「爽」", classify_chapter_emotion(p, "林悠然升职加薪。"), "爽")
    check("调 LLM 1 次", p.call_count, 1)

    # 所有候选
    for emo in ("爽", "虐", "甜", "怒", "悬疑", "日常", "过渡"):
        p = FakeEmotionProvider(f'{{"emotion": "{emo}"}}')
        check(f"情绪「{emo}」", classify_chapter_emotion(p, "x"), emo)

    # 容错
    p = FakeEmotionProvider('{"emotion": "nonsense"}')
    check("情绪不在候选集 → None", classify_chapter_emotion(p, "x"), None)

    p = FakeEmotionProvider('{"other": "爽"}')
    check("缺 emotion 键 → None", classify_chapter_emotion(p, "x"), None)

    p = FakeEmotionProvider('{"emotion": 123}')
    check("emotion 非 str → None", classify_chapter_emotion(p, "x"), None)

    p = FakeEmotionProvider("判断完成，爽")
    check("非 JSON → None", classify_chapter_emotion(p, "x"), None)

    p = FakeEmotionProvider('```json\n{"emotion": "虐"}\n```')
    check("```json 包裹 → 虐", classify_chapter_emotion(p, "x"), "虐")

    p = FakeEmotionProvider(raise_exc=True)
    check("provider 异常 → None", classify_chapter_emotion(p, "x"), None)

    p = FakeEmotionProvider('{"emotion": "爽"}')
    check("空文本 → None（不调 LLM）", classify_chapter_emotion(p, ""), None)
    check("空文本时 LLM 未被调", p.call_count, 0)

    p = FakeEmotionProvider('{"emotion": "爽"}')
    check("全空白文本 → None", classify_chapter_emotion(p, "   \n  "), None)
    check("全空白时 LLM 未被调", p.call_count, 0)


# ── 组 2：distill_avg_node_segments 纯函数 ──

def test_distill_avg_node_segments():
    print("\n=== 组 2: distill_avg_node_segments 纯函数 ===")
    from app.generation.rhythm import distill_avg_node_segments

    # 空 → 默认 5
    check("空 → 默认 5", distill_avg_node_segments([]), 5)

    # 全日常 → 12
    check("全日常 → 12（最稀疏）", distill_avg_node_segments(["日常"] * 10), 12)
    check("全日常（仅 1 章） → 12", distill_avg_node_segments(["日常"]), 12)

    # 全高潮 → 3
    check("全爽 → 3（最密集）", distill_avg_node_segments(["爽"] * 10), 3)
    check("混合高潮 10/10 → 3", distill_avg_node_segments(
        ["爽", "虐", "甜", "怒", "悬疑"] * 2
    ), 3)

    # 1/2 高潮 → avg = 2 → clamp 到 3
    check("一半高潮 → 3（clamp）",
          distill_avg_node_segments(["爽", "日常"] * 5), 3)

    # 1/4 高潮 → avg = 4
    check("1/4 高潮 → 4",
          distill_avg_node_segments(["爽"] + ["日常"] * 3), 4)

    # 1/5 高潮 → avg = 5
    check("1/5 高潮 → 5",
          distill_avg_node_segments(["爽"] + ["日常"] * 4), 5)

    # 1/10 高潮 → avg = 10
    check("1/10 高潮 → 10",
          distill_avg_node_segments(["爽"] + ["日常"] * 9), 10)

    # 1/20 高潮 → avg = 20 → clamp 到 12
    check("1/20 高潮 → 12（clamp 上限）",
          distill_avg_node_segments(["爽"] + ["日常"] * 19), 12)

    # 边界值
    check("3/10 高潮 → 3.33 → round 3", distill_avg_node_segments(["爽"] * 3 + ["日常"] * 7), 3)
    check("4/10 高潮 → 2.5 → round 2 → clamp 3", distill_avg_node_segments(["爽"] * 4 + ["日常"] * 6), 3)
    check("5/10 高潮 → 2 → clamp 3", distill_avg_node_segments(["爽"] * 5 + ["日常"] * 5), 3)

    # "过渡" 也算非高潮（同 _BRANCH_EMOTIONS 语义对齐）
    # 1 章爽 + 1 章过渡 = 2 章，1 章高潮，p=0.5, avg=round(2)=2 → clamp 3
    check("「过渡」算非高潮", distill_avg_node_segments(["爽", "过渡"]), 3)

    # 自定义 min/max
    check("自定义 min=2/max=5", distill_avg_node_segments(
        ["日常"] * 9, min_avg=2, max_avg=5
    ), 5)
    check("自定义 min=2/max=5 全高潮",
          distill_avg_node_segments(
              ["爽"] * 9, min_avg=2, max_avg=5
          ), 2)


# ── 组 3：compute_plot_rhythm（多本合并 + 失败兜底）──

def test_compute_plot_rhythm():
    print("\n=== 组 3: compute_plot_rhythm ===")
    from app.generation.rhythm import compute_plot_rhythm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project
    NovelChunk = app.models.NovelChunk

    s = Sess()
    p = Project(
        title="v2.4 测", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        reference_novel_ids=[1, 2],
    )
    s.add(p); s.commit(); s.refresh(p)

    # novel 1: 3 章，每章 1 chunk
    for cn in (1, 2, 3):
        s.add(NovelChunk(novel_id=1, chapter_no=cn, idx=0, text=f"第{cn}章文本"))
    # novel 2: 2 章
    for cn in (1, 2):
        s.add(NovelChunk(novel_id=2, chapter_no=cn, idx=0, text=f"第{cn}章文本"))
    s.commit()

    # 假 provider：每章标"爽"
    p_emo = FakeEmotionProvider('{"emotion": "爽"}')
    res = compute_plot_rhythm(s, p, [1, 2], p_emo)
    check("compute_plot_rhythm 返回 dict", isinstance(res, dict), True)
    check("version 字段", res.get("version"), 1)
    check("source_ids", res.get("source_ids"), [1, 2])
    check("avg_node_segments 5 章全高潮 → 3", res.get("avg_node_segments"), 3)
    check("chapter_emotions 长度 5（3+2）", len(res.get("chapter_emotions", [])), 5)
    check("chapter_count_per_novel novel 1", res.get("chapter_count_per_novel", {}).get(1), 3)
    check("chapter_count_per_novel novel 2", res.get("chapter_count_per_novel", {}).get(2), 2)
    check("branch_ratio = 0（全高潮）", res.get("branch_ratio"), 0.0)

    # 空 novel_ids
    res2 = compute_plot_rhythm(s, p, [], p_emo)
    check("空 novel_ids → {}", res2, {})

    # 单本无 chapter（不存在的 novel）
    res3 = compute_plot_rhythm(s, p, [999], p_emo)
    check("不存在的 novel_id → {}", res3, {})

    # 部分章节失败：每章前 1 次成功 + 1 次 raise
    class FlakyProvider(FakeEmotionProvider):
        def __init__(self):
            super().__init__(reply='{"emotion": "爽"}')
            self.n = 0

        def chat(self, system, user):
            self.n += 1
            if self.n == 2:  # 第 2 次失败
                raise RuntimeError("网络抖动")
            return self.reply

    p2 = FlakyProvider()
    res4 = compute_plot_rhythm(s, p, [1], p2)
    check("部分章节失败 → 不阻断", isinstance(res4, dict), True)
    check("失败章节标「日常」（兜底）", res4.get("chapter_emotions")[1], "日常")
    check("其他章节仍是「爽」", res4.get("chapter_emotions")[0], "爽")

    s.close()
    eng.dispose()


# ── 组 4：_timeline_state_for 集成 ──

def test_engine_uses_rhythm():
    print("\n=== 组 4: _timeline_state_for 用 plot_rhythm ===")
    from app.generation.engine import _timeline_state_for

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project

    s = Sess()
    p = Project(
        title="v2.4 集成", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
    )
    s.add(p); s.commit(); s.refresh(p)

    # 情况 1: plot_rhythm = None → 用默认 5
    class NoCallProvider:
        def chat(self, *a, **kw):
            raise RuntimeError("不应被调")
    p_no = NoCallProvider()
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    # 默认 avg=5 → total_segments=10，main_segments=8（10-2 支线），
    # n_main = clamp(8//5, 3, 12) = 3；seg_budget = 8 // 3 = 2
    check("plot_rhythm=None → n_main=3（默认 avg=5）", st.total_main, 3)
    check("plot_rhythm=None → seg_budget=2", st.nodes["n1"].segment_budget, 2)
    s.refresh(p)
    check("plot_rhythm=None 时未存 plot_timeline？存了（落库行为）", isinstance(p.plot_timeline, dict), True)
    s.close()

    # 情况 2: plot_rhythm.avg_node_segments=2（很密）
    s = Sess()
    p = Project(
        title="v2.4 集成 2", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        plot_rhythm={"version": 1, "avg_node_segments": 2, "branch_ratio": 0.2},
    )
    s.add(p); s.commit(); s.refresh(p)
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    # avg=2 → main=8, n_main=clamp(8//2, 3, 12)=4; seg_budget=8//4=2
    check("plot_rhythm.avg=2 → n_main=4", st.total_main, 4)
    check("plot_rhythm.avg=2 → seg_budget=2", st.nodes["n1"].segment_budget, 2)
    s.close()

    # 情况 3: plot_rhythm.avg_node_segments=10（很疏）
    s = Sess()
    p = Project(
        title="v2.4 集成 3", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        plot_rhythm={"version": 1, "avg_node_segments": 10, "branch_ratio": 0.2},
    )
    s.add(p); s.commit(); s.refresh(p)
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    # avg=10 → main=8, n_main=clamp(8//10, 3, 12)=3 (clamp 下限); seg=8//3=2
    check("plot_rhythm.avg=10 → n_main=3（clamp 下限）", st.total_main, 3)
    s.close()

    # 情况 4: plot_rhythm.avg_node_segments 过大（20）→ 仍用（plan_timeline 会
    # clamp 主干节点数到 [3,12]，所以这里只验 ≥ 1）
    s = Sess()
    p = Project(
        title="v2.4 集成 4", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        plot_rhythm={"version": 1, "avg_node_segments": 20, "branch_ratio": 0.2},
    )
    s.add(p); s.commit(); s.refresh(p)
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    # avg=20 → main=8, n_main=clamp(8//20, 3, 12)=3 (clamp 下限)
    check("plot_rhythm.avg=20 → n_main=3（plan_timeline clamp）", st.total_main, 3)
    s.close()

    # 情况 5: plot_rhythm.avg_node_segments 非 int → 回退默认
    s = Sess()
    p = Project(
        title="v2.4 集成 5", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        plot_rhythm={"version": 1, "avg_node_segments": "not an int", "branch_ratio": 0.2},
    )
    s.add(p); s.commit(); s.refresh(p)
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    check("plot_rhythm.avg 非 int → 回退默认 n_main=3", st.total_main, 3)
    s.close()

    # 情况 6: plot_rhythm.avg_node_segments = 0 或负 → 回退默认（保护性）
    s = Sess()
    p = Project(
        title="v2.4 集成 6", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
        plot_rhythm={"version": 1, "avg_node_segments": 0, "branch_ratio": 0.2},
    )
    s.add(p); s.commit(); s.refresh(p)
    st = _timeline_state_for(p, written_segments=0, provider=p_no, db=s)
    check("plot_rhythm.avg=0 → 回退默认 n_main=3", st.total_main, 3)
    s.close()

    eng.dispose()


# ── 跑全部 ──

if __name__ == "__main__":
    test_classify_chapter_emotion_returns()
    test_distill_avg_node_segments()
    test_compute_plot_rhythm()
    test_engine_uses_rhythm()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
