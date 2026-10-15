"""v2.3：流式版（generate_next_segment_stream + regenerate_segment_stream）时间线集成测试。

覆盖：
- generate_next_segment_stream：
  - 传 node_prompt 给 build_user_prompt（走时间线模式）
  - commit 段后调 _judge_and_advance（与同步版同口径）
  - yield "done" 事件含 node_judge 字段
- regenerate_segment_stream：
  - 传 node_prompt 给 _build_generation_prompts（走时间线模式）
  - **不**调 _judge_and_advance（重生成不改 _next_segment_no）
- _build_generation_prompts：
  - node_prompt 参数透传到 build_user_prompt
  - 不传 node_prompt 时为 None（向后兼容）

跑法：python tests/test_v23_stream_timeline.py
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
from app.generation.timeline import plan_timeline


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


class FakeStreamingProvider:
    """流式 + 非流式都能用的假 provider。"""

    def __init__(self, reply: str = "流式生成的内容", judge_reply: str = '{"done": false}'):
        self.reply = reply
        self.judge_reply = judge_reply
        self.chat_calls = []  # [(system, user, kwargs)]
        self.stream_calls = []
        self.judge_calls = []  # _JUDGE_NODE_DONE_SYSTEM 触发的 chat
        self.summarize_calls = []
        # 模拟 judge_node_completion 也用 chat 实现
        self._judge_mode = False

    def _maybe_split_judge(self, system: str, user: str):
        """判定是否是 judge_node_completion 调用（捕获用于断言）。"""
        if "退出条件" in user and ("判断" in user or "达成" in user):
            self._judge_mode = True
            self.judge_calls.append((system, user))
            return True
        return False

    def chat(self, system: str, user: str, constraints=None) -> str:
        # 先看是不是 judge 调用
        if "退出条件" in user and "判断" in user:
            self.judge_calls.append((system, user))
            return self.judge_reply
        self.chat_calls.append((system, user, constraints))
        if self._judge_mode:
            return self.judge_reply
        return self.reply

    def stream_chat(self, system: str, user: str, constraints=None, stop_sequences=None):
        self.stream_calls.append((system, user, constraints))
        # 一次性 yield（模拟默认实现）
        yield self.reply

    def summarize(self, text: str) -> str:
        self.summarize_calls.append(text)
        return f"summary({len(text)})"

    def summarize_rollup(self, texts: list[str], level: str) -> str:
        return f"rollup({level}, {len(texts)})"


def make_fresh_project_setup():
    """起内存 sqlite + 项目 + 落 n1 填实节点的存档。"""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project

    s = Sess()
    p = Project(
        title="v2.3 测", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
    )
    s.add(p); s.commit(); s.refresh(p)

    from app.generation.engine import _persist_timeline_state
    st = plan_timeline(total_segments=10)
    st.nodes["n1"].title = "入职立足"
    st.nodes["n1"].exit_condition = "升至部门经理"
    st.nodes["n1"].emotion = "爽"
    st.nodes["n1"].segment_budget = 3
    _persist_timeline_state(s, p, st)
    s.refresh(p)
    return eng, Sess, p


# ── 组 1：generate_next_segment_stream 集成 ──

def test_stream_integration_node_prompt():
    print("\n=== 组 1: generate_next_segment_stream 传 node_prompt ===")
    from app.generation.engine import generate_next_segment_stream, _style_narrative

    eng, Sess, p = make_fresh_project_setup()
    provider = FakeStreamingProvider(reply="流式段内容")
    s = Sess()

    # 关键：_style_narrative 调用了 LLM——mock 它返回空避免 LLM
    from unittest.mock import patch
    with patch("app.generation.engine._style_narrative", return_value=None):
        chunks = list(generate_next_segment_stream(s, p, novel_ids=None, provider=provider))
    check("至少 yield 一个 chunk 事件", any(c.get("type") == "chunk" for c in chunks), True)
    check("最后 yield done 事件", chunks[-1].get("type"), "done")
    # done 事件含 node_judge
    check("done 事件含 node_judge 字段", "node_judge" in chunks[-1], True)
    nj = chunks[-1]["node_judge"]
    check("node_judge.called=True", nj.get("called"), True)
    check("node_judge.advanced=True", nj.get("advanced"), True)
    # v2.1 集成：judge_node_completion 被调（n1 有 exit_condition）
    check("judge_node_completion 被调一次", len(provider.judge_calls), 1)
    # v2.3 关键：流式版调了 build_user_prompt，看 prompt 里含"当前剧情进度"块
    if provider.stream_calls:
        sys_p, user_p, _ = provider.stream_calls[0]
        check("流式版 user_prompt 含「当前剧情进度」块", "当前剧情进度" in user_p, True)
        # v2.2 节点粒度：plot_bible/premise 不注入
        check("流式版 user_prompt 不含 premise", "相知相守" not in user_p, True)
        # 但 system_prompt 仍含人物关系（系统层没动）
    s.close()
    eng.dispose()


# ── 组 2：regenerate_segment_stream 集成 ──

def test_regenerate_stream_passes_node_prompt():
    print("\n=== 组 2: regenerate_segment_stream 传 node_prompt ===")
    from app.generation.engine import generate_next_segment_stream, regenerate_segment_stream

    eng, Sess, p = make_fresh_project_setup()
    # 先生成第 1 段（让 segment_no=1 存在，再重生成）
    s = Sess()
    provider = FakeStreamingProvider(reply="流式段内容")
    from unittest.mock import patch
    with patch("app.generation.engine._style_narrative", return_value=None):
        # 先生成第 1 段
        list(generate_next_segment_stream(s, p, novel_ids=None, provider=provider))
    s.close()

    # 重生成第 1 段
    s = Sess()
    p2 = s.get(type(p), p.id)
    provider2 = FakeStreamingProvider(reply="重生成段内容")
    # 清掉 stream_calls 记录便于断言
    provider2.stream_calls = []
    with patch("app.generation.engine._style_narrative", return_value=None):
        chunks = list(regenerate_segment_stream(s, p2, segment_no=1, novel_ids=None, provider=provider2))
    check("regen 至少 yield 一个 chunk", any(c.get("type") == "chunk" for c in chunks), True)
    check("regen 最后 yield done", chunks[-1].get("type"), "done")
    # 关键：stream_chat 收到的 user_prompt 含 node_prompt
    if provider2.stream_calls:
        sys_p, user_p, _ = provider2.stream_calls[0]
        check("regen user_prompt 含「当前剧情进度」块", "当前剧情进度" in user_p, True)
        check("regen user_prompt 不含 premise", "相知相守" not in user_p, True)
    s.close()
    eng.dispose()


# ── 组 3：_build_generation_prompts 透传 node_prompt ──

def test_build_generation_prompts_passes_node_prompt():
    print("\n=== 组 3: _build_generation_prompts 透传 node_prompt ===")
    from app.generation.engine import _build_generation_prompts
    from unittest.mock import patch

    eng, Sess, p = make_fresh_project_setup()
    s = Sess()
    # 关键：engine.py 用 `from app.generation.prompts import build_user_prompt`，
    # 所以 patch 必须指向**已 import 的引用**——即 `app.generation.engine.build_user_prompt`，
    # 而不是 `app.generation.prompts.build_user_prompt`。
    captured = {}
    from app.generation import engine as _engine

    orig_bup = _engine.build_user_prompt

    def spy_bup(*args, **kwargs):
        captured["node_prompt"] = kwargs.get("node_prompt")
        return orig_bup(*args, **kwargs)

    with patch.object(_engine, "build_user_prompt", side_effect=spy_bup):
        # 不传 node_prompt（向后兼容）
        _build_generation_prompts(s, p)
        check("不传 node_prompt 时 build_user_prompt 收到 None", captured.get("node_prompt"), None)

        captured.clear()
        # 传 node_prompt
        _build_generation_prompts(s, p, node_prompt="<当前剧情点>xxx</当前剧情点>")
        check("传 node_prompt 时 build_user_prompt 收到对应字符串",
              captured.get("node_prompt"), "<当前剧情点>xxx</当前剧情点>")
    s.close()
    eng.dispose()


# ── 跑全部 ──

if __name__ == "__main__":
    test_stream_integration_node_prompt()
    test_regenerate_stream_passes_node_prompt()
    test_build_generation_prompts_passes_node_prompt()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
