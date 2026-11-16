"""批 9 测试的最小夹具：一个「三段规划、节点带准入准出」的项目 + 假 provider。

从 main 的 `tests/test_causal_timeline.py` 里取出 `Reply` / `fixture` 两个 helper
（该文件本身的用例依赖 main 的规划链与 `_judge_and_advance`，不随本分支搬）。
这里只保留**夹具**，让 action_proposals / workflow / derived_rebuild 的测试能跑。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base  # noqa: E402
from app.generation.timeline import plan_timeline, state_to_dict  # noqa: E402
from app.models import Project  # noqa: E402


class Reply:
    """假 provider：按预设 JSON 回话，并记下每次提问。"""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def chat(self, system, user, **kwargs):
        self.calls.append(user)
        return json.dumps(self.data, ensure_ascii=False)


def fixture():
    """三段规划 + 同一个节点标题/准入/准出，落一个内存库项目。"""
    db = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(db.bind)
    st = plan_timeline(3)
    for node in st.nodes.values():
        node.title = "寻找钥匙"
        node.entry_condition = "门锁着，钥匙遗失"
        node.exit_condition = "找到钥匙"
    p = Project(
        title="钥匙", outline=["寻找钥匙"], target_length=3000,
        generation_length=1000, plot_timeline=state_to_dict(st),
    )
    db.add(p)
    db.commit()
    return db, p
