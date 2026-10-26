"""扩展规划：在保存权威允许的范围内追加剧情点，并把它们写成新的段契约。

为什么要有这条路径（背景见 docs/plan-extension-design.md）：
    规划用尽后生成被拒（409 `plan_exhausted`）是**对的** —— 没有段契约就没有
    保存权威可核对，新写的正文只能自己编前情（实测：规划 3 个节点写出 4 段，
    第 4 段自己编出「复查结果/待查」，审计只能报 `missing_saved_authority`）。
    但只关门不出路，作者想继续写时无路可走。这条路径把新剧情点写进契约，
    让生成在**权威范围内**继续。

三条纪律（与正文那条链路同源）：
    1. 权威只读：不改 premise、不改已冻结契约、不改已写正文；
    2. 只追加：节点与契约都往后加，段号不复用，历史不动；
    3. 越界即失败：清单外物件/人物/地点 → **回灌让模型自改**，仍犯则拒绝；
       权威没有余量 → 显式说清差在哪，**不替作者编剧情**。

分工：模型只答「提什么新剧情点」，**结论一律由服务端推导**（余量、时间窗、
清单核对都在服务端），与协议 5 同一套纪律。
"""
from __future__ import annotations

import re

from app.generation.planning import CausalNode, model_json
from app.generation.timeline import PlotNode, build_node_prompt

#: 时间区间（自由文本里抽）。只认「HH:MM至HH:MM」这一种写法，
#: 抽不出就当**不可判定**（上层据此 fail closed，不猜）。
_TIME_RANGE = re.compile(
    r'(\d{1,2})\s*[:：]\s*(\d{2})\s*(?:至|到|-|—|~|－)\s*(\d{1,2})\s*[:：]\s*(\d{2})')

#: 一次扩展最多追加几个剧情点。
MAX_EXTEND_COUNT = 3

#: 提案次数上限（含一次「回灌自改」）。
MAX_PROPOSE_ATTEMPTS = 2

_SYSTEM = (
    '你是小说剧情规划编辑。任务：在**保存权威允许的范围内**续写剧情点，'
    '供后续分段写作按点推进。\n'
    '严格只输出 JSON {"nodes":[...]}。每个节点必须有以下字段：'
    'title,premise（事件前提）,motivation（人物为何行动）,action,result,cost,'
    'time,location,participants（字符串数组）,state_before,state_after；'
    '除participants外均为字符串。\n'
    '要求：\n'
    '1. 恰好输出 {count} 个节点，按故事顺序。\n'
    '2. time 必须落在【可写时段】里，且与已写剧情点的时间不重叠。\n'
    '3. **不得引入保存权威之外的物件、人物、地点**——要用的东西只能用权威里已有的。\n'
    '4. 不得写出故事结局；不得重复已写剧情点做过的事。\n'
    '5. 不编造已知事实；不确定的写 unknown，不适用的代价写 NA。'
)


def _fmt(minutes: int) -> str:
    return f'{minutes // 60:02d}:{minutes % 60:02d}'


def _spans(text: str) -> list[tuple[int, int]]:
    """从自由文本里抽出时间区间（分钟）。抽不出/跨天的都丢掉。"""
    out: list[tuple[int, int]] = []
    for match in _TIME_RANGE.finditer(text or ''):
        h1, m1, h2, m2 = (int(x) for x in match.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if end <= start:          # 跨天：不做假设
            continue
        out.append((start, end))
    return out


def _authority_span(authority: str) -> tuple[int, int] | None:
    """权威允许的时间范围（最早起点 → 最晚终点）。"""
    spans = _spans(authority)
    if not spans:
        return None
    return min(s for s, _ in spans), max(e for _, e in spans)


def _covered_spans(planning: dict, nodes: dict) -> list[tuple[int, int]]:
    """已写剧情点占用的时间区间。

    优先用因果节点的 `time` 字段（结构化），退回剧情点 entry_condition 里的
    时间区间（自由文本）。两者都抽不到就不算占用 —— 宁可少算，
    也不能把「不知道」当成「占用」。
    """
    covered: list[tuple[int, int]] = []
    for node in (planning.get('nodes') or []):
        if isinstance(node, dict) and node.get('time'):
            covered += _spans(str(node['time']))
    for node in (nodes or {}).values():
        if isinstance(node, dict) and node.get('entry_condition'):
            covered += _spans(str(node['entry_condition']))
    # 同一个时段会在因果节点与剧情点里各出现一次，去重后好读。
    return sorted(set(covered))


def _free_spans(span: tuple[int, int], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """权威范围里**还没被剧情点占用**的时段。"""
    start, end = span
    free: list[tuple[int, int]] = []
    cursor = start
    for c_start, c_end in sorted(covered):
        if c_end <= cursor or c_start >= end:
            continue
        if c_start > cursor:
            free.append((cursor, min(c_start, end)))
        cursor = max(cursor, c_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return [(s, e) for s, e in free if e > s]


def _authority_text(contracts: dict, project) -> str | None:
    """新剧情点要服从的**保存权威**。

    取值口径：优先用**项目当前的前提**（作者最新写下的话 —— 作者扩写了权威，
    扩展就该按新的算），退回最后一个已冻结契约里的那一份。

    已写段落不受影响：它们各自按**写下来那一刻**冻结的权威核对（契约不改）。
    """
    if project.premise:
        return str(project.premise)
    keys = sorted((k for k in (contracts or {}) if str(k).isdigit()),
                  key=lambda k: int(k), reverse=True)
    for key in keys:
        contract = contracts.get(key)
        if isinstance(contract, dict):
            premise = ((contract.get('authority') or {}).get('premise'))
            if premise:
                return str(premise)
    return None


def _written_segments(db, project) -> int:
    from app.models import Generation
    from sqlalchemy import select
    return len(list(db.scalars(select(Generation).where(
        Generation.project_id == project.id)).all()))


def _total_segments(project) -> int:
    per_segment = project.generation_length or 3000
    return max(int((project.target_length or 5000) // max(per_segment, 1)), 1)


def _propose(provider, authority: str, planning: dict, nodes: dict,
             free: list[tuple[int, int]], count: int, feedback: str | None):
    """让模型提新剧情点（结构与首轮规划**同一套 schema**）。"""
    done_nodes = []
    for node in (planning.get('nodes') or []):
        if isinstance(node, dict):
            done_nodes.append(f"- 「{node.get('title')}」{node.get('time')}｜"
                              f"{str(node.get('action'))[:80]}")
    known = []
    for node in (nodes or {}).values():
        if isinstance(node, dict):
            known.append(f"- 「{node.get('title')}」")
    user = (
        f'【保存权威】（唯一依据，不得超出）\n{authority}\n\n'
        f'【已写剧情点】（不许重复）\n' + ('\n'.join(done_nodes) or '（无）') + '\n\n'
        f'【可写时段】（新剧情点必须落在这里）\n' +
        '\n'.join(f'- {_fmt(s)}至{_fmt(e)}' for s, e in free) + '\n\n'
        f'【已经用过的剧情点标题】\n' + ('\n'.join(known) or '（无）') + '\n\n'
        f'输出 {count} 个节点的 JSON。')
    if feedback:
        user += '\n\n—— 纠正上一次不合格的输出 ——\n' + feedback
    raw, failures = model_json(
        provider, _SYSTEM.replace('{count}', str(count)), user,
        lambda text: _parse_nodes(text, count), attempts=MAX_PROPOSE_ATTEMPTS)
    return raw, failures


def _parse_nodes(text: str, count: int) -> list[dict]:
    import json
    payload = json.loads(text)
    nodes = payload.get('nodes')
    if not isinstance(nodes, list) or len(nodes) != count:
        raise ValueError('invalid node count')
    return [CausalNode.model_validate(node).model_dump() for node in nodes]


def _node_text(node: dict) -> str:
    """剧情点里所有会进提示与正文的内容（用来做清单核对）。"""
    parts = [node.get('title'), node.get('premise'), node.get('motivation'),
             node.get('action'), node.get('result'), node.get('state_before'),
             node.get('state_after'), node.get('location')]
    parts += list(node.get('participants') or [])
    return '\n'.join(str(p) for p in parts if p)


def _split_for_protocol(text: str, limit: int = 600) -> list[tuple[int, int, str]]:
    """把剧情点文本切成**协议能吃的段落**（每段 ≤ limit 字，带起止偏移）。

    封闭清单协议的 mentions 阶段对单个来源有长度上限（超了直接
    `inventory_paragraph_too_large` 整轮失败）。剧情点的字段动辄好几百字，
    所以按行切、行太长再按句号切 —— 切点保持原文偏移，引文仍然可逐字回查。
    """
    pieces: list[tuple[int, int, str]] = []
    offset = 0
    for line in (text or '').split('\n'):
        if not line.strip():
            offset += len(line) + 1
            continue
        start = offset
        while len(line) > limit:
            cut = max(line.rfind('。', 0, limit), line.rfind('，', 0, limit))
            if cut <= 0:
                cut = limit
            else:
                cut += 1
            pieces.append((start, start + cut, line[:cut]))
            start += cut
            line = line[cut:]
        if line.strip():
            pieces.append((start, start + len(line), line))
        offset += len(line) + 1
    return pieces


def _out_of_authority(provider, authority: str, node: dict) -> list[dict]:
    """把新剧情点丢进**同一套封闭清单协议**里跑一遍，返回越界项。

    复用正文审计那套协议（closed_inventory v5）：判定由服务端推导，
    模型只回答「它与授权物是什么关系」。这样「扩展出来的剧情点」与
    「写出来的正文」用的是**同一把尺**。
    """
    from app.agents.closed_inventory_v2 import inspect_inventory
    text = _node_text(node)
    sources = {
        'C1S001': {'source_id': 'contract:1', 'path': '/authority/premise',
                    'quote': authority, 'start': 0, 'end': len(authority)},
    }
    pieces = _split_for_protocol(text)
    for index, (start, end, quote) in enumerate(pieces, 1):
        sources[f'G900P{index:03d}'] = {'source_id': 'generation:900', 'path': '/body',
                                        'quote': quote, 'start': start, 'end': end}
    observation = {'data': {
        'sources': sources,
        'segments': [{'segment_no': 1, 'generation_id': 900,
                      'contract': {'authority': {'premise': authority}},
                      'text_hash': '', 'unknown': None}],
    }}
    report = {'unresolved': [], 'model_participated': False}
    inspect_inventory(provider, observation, report,
                      kinds=['object', 'person', 'place'], protocol=5)
    violations = []
    for candidate in (report['inventory'].get('candidates') or []):
        verdict = candidate.get('verdict') or {}
        if verdict.get('status') != 'violated':
            continue
        mention = candidate.get('mention') or {}
        violations.append({'entity': mention.get('canonical'),
                           'kind': mention.get('kind'),
                           'reason': str(verdict.get('reason'))[:200]})
    return violations


def extend_plan(db, project, provider, *, count: int = 1) -> dict:
    """在权威允许的范围内追加剧情点，写成新的段契约。

    返回 dict：`status` 决定成败；成功时带 `nodes` / `contracts` / `headroom`。
    失败时 `message` 说清差在哪（调用方据此给 409）。
    """
    count = max(1, min(int(count or 1), MAX_EXTEND_COUNT))
    timeline = project.plot_timeline or {}
    if not isinstance(timeline, dict):
        return {'status': 'no_plan', 'message': '这个项目没有时间线状态，无法扩展规划。'}
    planning = timeline.get('planning') or {}
    contracts = planning.get('segment_contracts') or {}
    nodes = timeline.get('nodes') or {}
    done = {str(x) for x in (timeline.get('done') or [])}

    planned = {str(c['node_id']) for c in (contracts or {}).values()
               if isinstance(c, dict) and c.get('node_id')}
    if not planned:
        planned = {str(k) for k in (nodes or {})}
    if not planned:
        return {'status': 'no_plan', 'message': '这个项目没有规划，不需要扩展。'}
    if not planned <= done:
        remaining = sorted(planned - done)
        return {'status': 'plan_not_exhausted',
                'message': f'规划还没写完（{len(remaining)} 个剧情点未写），不需要扩展。'}

    authority = _authority_text(contracts, project)
    if not authority:
        return {'status': 'no_saved_authority',
                'message': '没有保存权威，不能扩展规划：扩展出来的新剧情点没有可核对的依据。'}

    span = _authority_span(authority)
    if span is None:
        return {'status': 'no_authority_headroom',
                'message': '保存权威里没有可判定的时间范围，无法确认还有哪些剧情可以写。'}
    covered = _covered_spans(planning, nodes)
    free = _free_spans(span, covered)
    written = _written_segments(db, project)
    total = _total_segments(project)
    headroom = {
        'authority_span': f'{_fmt(span[0])}至{_fmt(span[1])}',
        'covered_spans': [f'{_fmt(s)}至{_fmt(e)}' for s, e in sorted(covered)],
        'free_spans': [f'{_fmt(s)}至{_fmt(e)}' for s, e in free],
        'written_segments': written, 'total_segments': total,
        'segments_left': max(total - written, 0),
    }
    if not free:
        return {'status': 'no_authority_headroom', 'headroom': headroom,
                'message': f'保存权威允许的时间范围（{headroom["authority_span"]}）'
                           f'已经被现有剧情点写满，没有能承载新剧情的余量。'
                           f'要继续写，请先扩展权威（写进新的时间范围或剧情），再扩展规划。'}
    if headroom['segments_left'] <= 0:
        return {'status': 'no_authority_headroom', 'headroom': headroom,
                'message': f'段数预算已用完（{written}/{total} 段），没有可写的段数余量。'}

    proposals, failures = _propose(provider, authority, planning, nodes, free, count, None)
    if not proposals:
        return {'status': 'proposal_failed', 'headroom': headroom, 'failures': failures,
                'message': '模型没有给出可用的新剧情点（格式或内容不合格）。'}

    # 服务端校验：时间窗 + 清单（模型答「提什么」，结论由服务端推导）。
    feedback = None
    problems: list[str] = []
    for attempt in range(MAX_PROPOSE_ATTEMPTS):
        accepted, problems = [], []
        for node in proposals:
            node_spans = _spans(str(node.get('time') or ''))
            if not node_spans:
                problems.append(f'「{node.get("title")}」的 time 没有可判定的时间区间')
                continue
            s, e = node_spans[0]
            if not any(fs <= s and e <= fe for fs, fe in free):
                problems.append(f'「{node.get("title")}」的 time（{_fmt(s)}至{_fmt(e)}）'
                                f'不在可写时段里')
                continue
            violations = _out_of_authority(provider, authority, node)
            if violations:
                items = '、'.join(f'{v["entity"]}（{v["kind"]}）' for v in violations)
                problems.append(f'「{node.get("title")}」引入了保存权威之外的'
                                f'物件/人物/地点：{items}。'
                                f'请改用权威里已有的东西承担同样的剧情功能。')
                continue
            accepted.append(node)
        if accepted:
            return _write_extension(db, project, accepted, authority, headroom, timeline,
                                    planning, contracts, nodes)
        if attempt + 1 < MAX_PROPOSE_ATTEMPTS:
            feedback = ('\n'.join(problems) +
                        '\n请重新输出这 %d 个节点，并严格满足上面的问题。' % count)
            proposals, more = _propose(provider, authority, planning, nodes, free, count, feedback)
            failures += more
            if not proposals:
                break
    detail = '；'.join(problems) if problems else '新剧情点不合格。'
    return {'status': 'out_of_authority_items', 'headroom': headroom, 'failures': failures,
            'message': '新剧情点越出了保存权威允许的范围，纠正后仍不合格：' + detail}


def _write_extension(db, project, accepted, authority, headroom, timeline,
                     planning, contracts, nodes):
    """把新剧情点写进**时间线状态 + 段契约**（只追加，不动历史）。"""
    from copy import deepcopy
    new_planning = dict(planning)
    causal_nodes = list(new_planning.get('nodes') or [])
    new_contracts = dict(contracts)
    new_nodes = dict(nodes)
    segment_numbers = [int(k) for k in new_contracts if str(k).isdigit()]
    next_segment = (max(segment_numbers) + 1) if segment_numbers else 1
    next_node_index = len([k for k in new_nodes if str(k).startswith('n')]) + 1

    added_nodes, added_contracts = [], []
    done_count = len([nid for nid in (nodes or {}) if nid in (timeline.get('done') or [])])
    main_progress = done_count / max(done_count + len(accepted), 1)
    for offset, node in enumerate(accepted):
        nid = f'n{next_node_index + offset}'
        entry = (f"时间：{node['time']}；地点：{node['location']}；"
                 f"参与者：{'、'.join(node['participants'])}；前提：{node['premise']}；"
                 f"动机：{node['motivation']}；起始状态：{node['state_before']}")
        exit_text = (f"行动：{node['action']}；结果：{node['result']}；"
                     f"代价：{node['cost']}；结束状态：{node['state_after']}")
        plot = PlotNode(nid, 'main', node['title'], '', entry, exit_text, segment_budget=1)
        new_nodes[nid] = {
            'id': nid, 'kind': 'main', 'title': node['title'], 'emotion': '',
            'entry_condition': entry, 'exit_condition': exit_text,
            'beats': [], 'opens': [], 'closes': [], 'segment_budget': 1,
            'parent': None, 'children': [],
        }
        causal_nodes.append(deepcopy(node))
        contract_key = str(next_segment + offset)
        new_contracts[contract_key] = {
            'node_id': nid,
            'prompt': build_node_prompt(plot, 0, main_progress, authority=authority),
            'authority': {'premise': authority,
                          'causal_node': deepcopy(node)},
        }
        added_nodes.append({'node_id': nid, 'title': node['title'], 'time': node['time']})
        added_contracts.append({'segment_no': next_segment + offset, 'node_id': nid})

    new_planning['segment_contracts'] = new_contracts
    new_planning['nodes'] = causal_nodes
    new_timeline = dict(timeline)
    new_timeline['nodes'] = new_nodes
    new_timeline['planning'] = new_planning
    new_timeline['current_node'] = added_nodes[0]['node_id']
    new_timeline['current_beat'] = 0
    new_timeline['seg_counts'] = dict(new_timeline.get('seg_counts') or {})
    project.plot_timeline = new_timeline
    db.commit()
    return {'status': 'extended', 'nodes': added_nodes, 'contracts': added_contracts,
            'headroom': headroom,
            'message': f'已在保存权威允许的范围内追加 {len(added_nodes)} 个剧情点并写入契约。'}
