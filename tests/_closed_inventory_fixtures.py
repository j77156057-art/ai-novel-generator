"""闭集盘点行为测试的共享夹具（从 main 的 test_closed_inventory_v2 抽取）。

只保留 `ScopedReview` 与 `scoped_db`：v3/v4 用例 import 它们，但本地
`test_closed_inventory_v2.py` 是另一套单元级用例，没有这两个名字。
"""
from __future__ import annotations

import json

from _body_repair_fixtures import fixture

from app.models import Generation


class ScopedReview:
    def __init__(self, omit=False):
        self.calls = []
        self.omit = omit

    def chat(self, system, user):
        d = json.loads(user)
        self.calls.append(d)
        kind = d['dimension']
        if d['stage'] == 'constraints':
            return json.dumps({'constraints': [] if self.omit else [{
                'kind': kind, 'mode': 'closed',
                'entities': [{'canonical': '扳手', 'aliases': []}],
                'source_id': next(iter(d['sources'])), 'quote': '室内仅有扳手。',
            }]})
        if d['stage'] == 'mentions':
            return json.dumps({
                'covered_source_ids': list(d['sources']),
                'mentions': [{
                    'source_id': sid, 'canonical': '焊枪', 'quote': '焊枪',
                    'state': 'actual',
                } for sid in d['sources']],
            })
        return json.dumps({'verdicts': [{
            'candidate_id': c['id'], 'status': 'violated',
            'reason': '仅有扳手，新增焊枪',
        } for c in d['candidates']]})


def scoped_db():
    db, p = fixture()
    p.plot_timeline = {'planning': {'segment_contracts': {
        '1': {'authority': {'premise': '室内仅有扳手。'}}},
    }}
    db.add(Generation(
        project_id=p.id, segment_no=1, text='她拿起焊枪。\n\n她放下焊枪。',
    ))
    db.commit()
    return db, p
