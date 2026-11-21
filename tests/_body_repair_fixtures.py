"""正文修复行为测试的本地夹具。

main 的同类夹具在 `test_causal_timeline.py` 里，但那份测试依赖更新的时间线
引擎 API（judge_node_completion 的 source_id 参数等），本分支还没有。
这里只保留 fixture/Reply，不携带时间线断言。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.generation.timeline import plan_timeline, state_to_dict
from app.models import Project


class Reply:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def chat(self, system, user, **kwargs):
        self.calls.append(user)
        return json.dumps(self.data, ensure_ascii=False)


def fixture():
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
