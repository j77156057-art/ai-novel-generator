"""裁决阶段并发化：**结果必须与串行逐字节一致**。

审计是整条闭环里最慢的一步 —— 一篇五段正文要几百次模型调用，串行要一个多小时。
实测通道本身支持并发（4 路并发墙钟约为串行的 0.45 倍），慢的是我们的代码
把组一个一个发。所以把「组之间彼此独立」的裁决阶段改成并发。

纪律（也是这个测试要守住的）：
  worker 只做「请求 + 校验」，**所有状态合并都在主线程按原顺序做** →
  候选编号（N001…）、检查项顺序、calls 顺序都不变，
  下游（修复链路、整段验收）按这些 id 引用，不能变。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.agents.closed_inventory_v2 import (  # noqa: E402
    _audit_concurrency, inspect_inventory,
)

PREMISE = '室内仅有书本。'
PARAGRAPHS = 45


def _observation(paragraphs=PARAGRAPHS):
    sources = {'C1S096': {'source_id': 'contract:1', 'path': '/authority/premise',
                          'quote': PREMISE, 'start': 0, 'end': len(PREMISE)}}
    offset = 0
    for i in range(1, paragraphs + 1):
        text = f'她拿起金印{i}。'
        sources[f'G1P{i:03d}'] = {'source_id': 'generation:1', 'quote': text,
                                   'start': offset, 'end': offset + len(text)}
        offset += len(text) + 1
    return {'data': {'segments': [{'segment_no': 1, 'generation_id': 1}], 'sources': sources}}


class _Provider:
    """按请求内容作答的假 provider（只看 payload，与并发无关）。"""

    def __init__(self):
        self.stages = []

    def chat(self, system, user):
        data = json.loads(user)
        stage = data.get('stage')
        self.stages.append(stage)
        if stage == 'constraints':
            sid = next(iter(data['sources']))
            return json.dumps({'constraints': [{
                'kind': data['dimension'], 'mode': 'closed',
                'entities': [{'canonical': '书本', 'aliases': []}],
                'source_id': sid, 'quote': PREMISE}]})
        if stage == 'mentions':
            mentions = []
            for sid in data['sources']:
                if not sid.startswith('G'):
                    continue
                idx = int(sid[3:])
                mentions.append({'source_id': sid, 'surface': f'金印{idx}',
                                 'canonical': f'金印{idx}', 'state': 'actual',
                                 'relation': '持有'})
            return json.dumps({'covered_source_ids': list(data['sources']),
                               'mentions': mentions})
        verdicts = []
        for cand in data['candidates']:
            verdicts.append({'candidate_id': cand['id'], 'entity_kind': 'independent',
                             'reason': '清单外新增', 'body_source_id': cand['body_source_id'],
                             'authority_source_id': cand['authority_source_id']})
        return json.dumps({'verdicts': verdicts})


def _run(concurrency):
    os.environ['CLOSED_INVENTORY_CONCURRENCY'] = str(concurrency)
    provider = _Provider()
    report = {'unresolved': []}
    inspect_inventory(provider, _observation(), report, kinds=['object'], protocol=8)
    return report, provider


def test_parallel_verdicts_match_sequential_exactly():
    """并发跑出来的报告必须与串行**逐字段一致**。"""
    seq_report, seq_provider = _run(1)
    par_report, par_provider = _run(4)

    # 调用量一致
    assert seq_provider.stages.count('verdicts') == par_provider.stages.count('verdicts')
    assert par_provider.stages.count('verdicts') >= 3, '这个夹具必须有多组裁决才谈得上并发'

    # 报告核心逐字段一致
    assert seq_report['checks'] == par_report['checks']
    assert seq_report['inventory']['candidates'] == par_report['inventory']['candidates']
    assert seq_report['inventory']['constraints'] == par_report['inventory']['constraints']
    assert seq_report['inventory']['mentions'] == par_report['inventory']['mentions']
    assert seq_report['inventory']['coverage_complete'] == par_report['inventory']['coverage_complete']
    assert seq_report['inventory']['calls'] == par_report['inventory']['calls']
    assert seq_report['unresolved'] == par_report['unresolved']

    # 候选编号与检查项顺序不能变（下游按 id 引用）
    assert [c['id'] for c in seq_report['inventory']['candidates']] == \
           [c['id'] for c in par_report['inventory']['candidates']]
    assert [c['id'] for c in seq_report['checks']] == [c['id'] for c in par_report['checks']]
    assert all(c['status'] == 'contradicted' for c in par_report['checks'])


def test_concurrency_knob_is_bounded_and_read_from_env():
    os.environ['CLOSED_INVENTORY_CONCURRENCY'] = '1'
    assert _audit_concurrency() == 1
    os.environ['CLOSED_INVENTORY_CONCURRENCY'] = '4'
    assert _audit_concurrency() == 4
    os.environ['CLOSED_INVENTORY_CONCURRENCY'] = '999'
    assert _audit_concurrency() <= 8
    os.environ['CLOSED_INVENTORY_CONCURRENCY'] = 'nonsense'
    assert _audit_concurrency() >= 1
    os.environ.pop('CLOSED_INVENTORY_CONCURRENCY', None)
    assert _audit_concurrency() >= 1
