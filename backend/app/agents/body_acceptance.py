"""整段验收：把全局验收义务真正关闭到某一版正文上。

局部修复落地 ≠ 验收通过。`proposals.merge_pending_acceptance` 只把未解决义务**重新绑定**到
新正文哈希（不删除、不缩小），生成守卫继续拦截；本模块负责在正文定稿后，用一次覆盖完整的
独立复验把这份义务关闭。

诚实边界（重要）：
- `full_scope_coverage` 的证据**不是**一次模型调用能给的。它要求有一份针对**当前正文**的
  封闭清单审计，且该审计 `coverage_complete=True` / `global_quality_verified=True`。
  缺这份审计时本操作**不发模型请求**，直接返回 `coverage_incomplete` 并列出未覆盖范围。
- 顶层 `assessment` 只是「已观察」，**不等于通过**；通过与否由四个维度、每一条未解决检查的
  重新裁决、以及覆盖声明三者共同决定。
- 客户端不能提交验收结论：所有判断都来自服务端读取的正文、冻结契约与原诊断报告。
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import AgentRun, Evidence, Generation
from app.agents.body_evidence import paragraph_sources, safe_response, text_hash
from app.agents.diagnostic import digest

DIMENSIONS = ('time', 'place', 'participants', 'object_state')
DECISIONS = ('resolved', 'still_violated', 'unknown')

#: 库存级义务由**当前正文的完整审计**消解 —— 证据是哪一条，就只消解哪一条。
#: 消解不掉的一律留着继续阻塞（不删除、不缩小）。
#:
#: 注意：封闭清单审计**自己从不**声明 global_quality_verified（它的立场就是
#: 「定位到引用不等于证明语义正确、未选维度保持 unknown」）。因此全局质量的证据
#: 由**本次整段验收**提供（四个维度 + 逐条检查复核 + 覆盖声明），而库存只提供
#: 「覆盖完整」与「候选已全部裁决」这两项。
DISCHARGEABLE_BY_CURRENT_INVENTORY = {
    'inventory_partial_coverage': lambda inv: bool(inv.get('coverage_complete')),
    'inventory_partial_verdict_scope': lambda inv: bool((inv.get('verdict_scope') or {}).get('complete')),
}
#: 本次整段验收本身就是那场「语义复核」，它完成后这些才消解。
DISCHARGEABLE_BY_ACCEPTANCE = ('semantic_review_required', 'full_scope_acceptance_unverified')

ACCEPTANCE_SYSTEM = '''你是独立Assistant，做一次**整段验收**：对当前正文（不是局部片段）逐项核对冻结权威与已保存契约。
输出JSON：{"assessment":"observed","reason":"...","dimensions":[...],"check_decisions":[...],"coverage":{...}}。
顶层assessment必须是observed，只表示你已观察，不表示通过；supported/contradicted/unknown只用于dimensions与check_decisions内。
dimensions必须恰好四项各一次：{"dimension":"time|place|participants|object_state","assessment":"supported|contradicted|unknown","reason":"...","source_ids":[...]}。
**判定口径（重要）**：contradicted 只用于正文**明确与权威冲突**（正文写了一个与权威不同的值）；
正文**没有写出**某项事实（没写日期、没写地点全称、没声明在场者只有谁）属于**未确认**，判 unknown 而不是 contradicted；
正文与权威**一致且可从正文确认**时判 supported。
**必须结合 original_checks 里的封闭清单枚举结果判断**：它已经逐段枚举了正文里的全部人物/地点/物件提及
（并给出每一项的裁决），参与者与物件状态这两个维度应以它的枚举为准 —— 不要因为正文没有显式声明
「仅两位人物」就判 unknown：封闭清单逐段枚举后只有授权名单内的人物出现，本身就是证据。
**地点维度同样以它的枚举为准**：封闭清单逐段枚举后，正文里出现的地点都在授权地点的范围内
（哪怕正文只写简称、没写权威里的地点全称），就是与权威一致，判 supported ——
不要因为正文没写出地点全称就判 unknown（那会让正文永远过不了验收：正文不必把地名重复一遍）。
真正与权威冲突的地点会被封闭清单判成违例并挡住验收，不需要在这里再判一次。
check_decisions覆盖给定的每个未解决检查id各一次：{"check_id":"object_N011","status":"resolved|still_violated|unknown","reason":"...","source_ids":[...]}。
resolved表示该违例在当前正文中确已消除（或本就不是违例），必须给出当前正文中的source id；still_violated与unknown必须说明还缺什么。
coverage={"examined_source_ids":[...],"complete":true|false,"reason":"..."}：必须列出你实际逐段核对过的正文source id。
source_ids只能选本次提供的sources键，禁止quote和坐标；矛盾必须引用两端不同source id；无法定位双方时为unknown。
未检查的内容保持unknown，不得把未覆盖写成supported，不得凭上文推断未读段落。'''


def _rows(db, project_id):
    return list(db.scalars(select(Generation).where(Generation.project_id == project_id)
                           .order_by(Generation.segment_no, Generation.id)))


def body_fingerprint(rows):
    """当前正文指纹（不含契约）：验收义务绑定在它上面。"""
    return digest({'bodies': [(g.id, g.segment_no, text_hash(g.text or '')) for g in rows]})


def _inventory_report(report):
    """从一条诊断报告里取出封闭清单 observation。"""
    for obs in report.get('observations', []):
        if obs.get('capability') == 'read_closed_inventory':
            return obs
    return None


def find_current_inventory(db, project_id, rows):
    """找针对**当前正文**的封闭清单审计（最新一条）。找不到返回 None。"""
    want = {str(g.id): text_hash(g.text or '') for g in rows}
    if not want:
        return None
    runs = db.scalars(select(AgentRun).where(AgentRun.project_id == project_id,
                                             AgentRun.role == 'assistant')
                      .order_by(AgentRun.id.desc())).all()
    for run in runs:
        if not run.note:
            continue
        try:
            report = json.loads(run.note)
        except (TypeError, ValueError):
            continue
        if (report.get('skill') or {}).get('id') != 'closed_inventory':
            continue
        obs = _inventory_report(report)
        if not obs:
            continue
        texts = (obs.get('data') or {}).get('body_texts') or {}
        if set(texts) != set(want):
            continue
        if any(text_hash(texts.get(k) or '') != want[k] for k in want):
            continue
        return {'run_id': run.id, 'report': report, 'observation': obs}
    return None


def _mentioned_source_ids(inventory):
    """这份审计里被登记为「提及」的正文 source id 集合 = 覆盖要求的下限。"""
    ids = set()
    for key in ('mentions', 'candidates', 'rejected_mentions'):
        for item in (inventory.get(key) or []):
            if isinstance(item, dict):
                for field in ('source_id', 'body_source_id'):
                    value = item.get(field)
                    if isinstance(value, str) and value.startswith('G'):
                        ids.add(value)
    return ids


def validate_acceptance(answer, sources, open_check_ids, required_source_ids):
    """校验整段验收响应。返回 (checks, decisions, coverage, problems)。"""
    problems = []
    if not isinstance(answer, dict) or answer.get('assessment') != 'observed':
        return [], [], {}, ['invalid_top_level_assessment']
    dims = answer.get('dimensions')
    if not isinstance(dims, list) or len(dims) != len(DIMENSIONS):
        return [], [], {}, ['invalid_dimensions']
    checks = []
    seen = set()
    for item in dims:
        if (not isinstance(item, dict)
                or set(item) != {'dimension', 'assessment', 'reason', 'source_ids'}
                or item['dimension'] not in DIMENSIONS or item['dimension'] in seen
                or item['assessment'] not in ('supported', 'contradicted', 'unknown')
                or not isinstance(item['reason'], str) or not item['reason'].strip()
                or not isinstance(item['source_ids'], list)):
            return [], [], {}, ['invalid_dimensions']
        ids = item['source_ids']
        if (any(not isinstance(s, str) or s not in sources for s in ids)
                or len(set(ids)) != len(ids)
                or (item['assessment'] == 'supported' and not ids)
                or (item['assessment'] == 'contradicted' and len(ids) < 2)):
            return [], [], {}, ['invalid_dimension_citation']
        seen.add(item['dimension'])
        checks.append({'id': f"acceptance_{item['dimension']}", 'status': item['assessment'],
                       'reason': item['reason'],
                       'citations': [dict(sources[s], source_id=s,
                                          body_source_id=sources[s]['source_id'],
                                          location_verified=True) for s in ids]})
    decisions = []
    raw = answer.get('check_decisions')
    if not isinstance(raw, list):
        return checks, [], {}, ['invalid_check_decisions']
    answered = set()
    for item in raw:
        if (not isinstance(item, dict)
                or set(item) != {'check_id', 'status', 'reason', 'source_ids'}
                or item['check_id'] not in open_check_ids
                or item['check_id'] in answered
                or item['status'] not in DECISIONS
                or not isinstance(item['reason'], str) or not item['reason'].strip()
                or not isinstance(item['source_ids'], list)):
            return checks, [], {}, ['invalid_check_decisions']
        ids = item['source_ids']
        if (any(not isinstance(s, str) or s not in sources for s in ids)
                or len(set(ids)) != len(ids)
                or (item['status'] == 'resolved' and not ids)):
            return checks, [], {}, ['invalid_check_decision_citation']
        answered.add(item['check_id'])
        decisions.append({'check_id': item['check_id'], 'status': item['status'],
                          'reason': item['reason'], 'source_ids': ids})
    missing = set(open_check_ids) - answered
    if missing:
        problems.append('undecided_checks:' + ','.join(sorted(missing)))
    coverage = answer.get('coverage')
    if (not isinstance(coverage, dict) or set(coverage) != {'examined_source_ids', 'complete', 'reason'}
            or not isinstance(coverage.get('examined_source_ids'), list)
            or not isinstance(coverage.get('complete'), bool)
            or not isinstance(coverage.get('reason'), str)):
        return checks, decisions, {}, problems + ['invalid_coverage']
    if any(not isinstance(s, str) or s not in sources for s in coverage['examined_source_ids']):
        return checks, decisions, {}, problems + ['invalid_coverage_citation']
    uncovered = sorted(set(required_source_ids) - set(coverage['examined_source_ids']))
    if uncovered:
        problems.append('coverage_incomplete')
    return checks, decisions, coverage, problems


def planned_node_gaps(timeline: dict) -> tuple[list[dict], list[dict]]:
    """规划与正文的对照：**冲突**（阻塞）与**判不出/没写到**（开放项）分开。

    返回 (blocking, visible)：
      blocking —— 节点的退出条件被正文**反驳**（judge 判 contradicted）：
        正文写的与规划这一段的目标冲突。实测一篇正文三段全耗在第一个节点，
        正文却把下一个节点的事也写了，第一个节点的退出条件
        （「登记页仍为空白」）被正文反驳 —— 这是正文与规划的**真冲突**，
        必须报出来，不能靠其它维度 supported 就放过去。
      visible —— 节点有段但判不出证据（unknown / 没判定过），或计划里还有
        没写的节点：如实记为开放项，**不阻塞**（判不出不等于确认有问题；
        故事还没写完也不是正文的缺陷）。

    为什么整段验收需要这道门禁：验收只核对「正文与权威是否**冲突**」，
    不核对「规划里的节点是不是都写到了、达成了」。实测一篇正文三段全耗在
    第一个节点上、后面两个节点从未产生，验收依然判 accepted ——
    那不是「正文合格」，那是「只查了已经写出来的部分」。
    """
    timeline = timeline or {}
    nodes = timeline.get('nodes') or {}
    done = {str(x) for x in (timeline.get('done') or [])}
    seg_counts = timeline.get('seg_counts') or {}
    completion = timeline.get('completion') or {}
    blocking: list[dict] = []
    visible: list[dict] = []
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get('kind') not in (None, 'main'):
            continue
        segments = int(seg_counts.get(nid) or 0)
        verdict = (completion.get(nid) or {}).get('verdict')
        title = node.get('title') or ''
        if verdict == 'contradicted':
            blocking.append({
                'node_id': nid, 'title': title, 'segments_bound': segments,
                'verdict': verdict,
                'reason': f'正文与规划冲突：节点「{title}」的退出条件被正文反驳'
                          f'（这一节点写了 {segments} 段）',
            })
            continue
        if segments > 0 and verdict != 'supported':
            visible.append({
                'node_id': nid, 'title': title, 'segments_bound': segments,
                'verdict': verdict,
                'reason': f'节点「{title}」写了 {segments} 段，但退出条件没有正文证据'
                          f'（判定：{verdict or "未判定"}）—— 判不出，不当违例',
            })
    unwritten = [nid for nid, node in nodes.items()
                 if isinstance(node, dict) and node.get('kind') in (None, 'main')
                 and int(seg_counts.get(nid) or 0) == 0 and nid not in done]
    if unwritten:
        visible.append({
            'node_id': ','.join(sorted(unwritten)),
            'title': '、'.join(str((nodes.get(nid) or {}).get('title') or nid)
                               for nid in sorted(unwritten)),
            'segments_bound': 0, 'verdict': None,
            'reason': f'规划里还有 {len(unwritten)} 个节点没有任何正文覆盖：'
                      + '、'.join(sorted(unwritten))
                      + '（故事还没写到那里，不阻塞本版验收）',
        })
    return blocking, visible


def run_body_acceptance(db, project_id, *, base_content_hash, provider=None):
    """对**当前正文**做一次整段验收，并把全局验收义务绑定/关闭到这一版正文。

    返回一个报告 dict（不抛异常给路由层，由路由层决定状态码）：
      {'status': 'accepted' | 'blocked' | 'coverage_incomplete',
       'global_semantic_accepted': bool,
       'unresolved': [...], 'coverage': {...}, 'run_id': int|None, ...}
    """
    from app.models import Project
    project = db.get(Project, project_id)
    if project is None:
        raise PermissionError('project_not_found')
    rows = _rows(db, project_id)
    if not rows:
        raise ValueError('no_generation_to_accept')
    if base_content_hash and body_fingerprint(rows) != base_content_hash:
        raise ValueError('stale_body_fingerprint')
    timeline = dict(project.plot_timeline or {})
    # 规划 vs 正文：正文**反驳**了节点退出条件 → 阻塞项；判不出/没写到 → 开放项。
    plan_blocking, plan_visible = planned_node_gaps(timeline)
    pending = timeline.get('pending_acceptance') or {}
    diagnostic_run_id = pending.get('diagnostic_run_id')
    if not diagnostic_run_id:
        raise ValueError('no_pending_acceptance')
    origin = db.get(AgentRun, int(diagnostic_run_id))
    if origin is None or not origin.note:
        raise ValueError('no_pending_acceptance')
    origin_report = json.loads(origin.note)
    open_check_ids = []
    for item in (pending.get('unresolved') or []):
        if isinstance(item, str) and item.startswith('original_check:'):
            open_check_ids.append(item.split(':', 1)[1])
    open_check_ids = sorted(set(open_check_ids))

    # 覆盖证据：必须有一份针对当前正文的完整封闭清单审计。
    current = find_current_inventory(db, project_id, rows)
    if current is None:
        return {'status': 'coverage_incomplete', 'global_semantic_accepted': False,
                'unresolved': list(pending.get('unresolved') or [])
                + ['node_exit_conflict:' + gap['node_id'] for gap in plan_blocking],
                'plan_coverage': {'blocking': plan_blocking, 'visible': plan_visible},
                'coverage': {'complete': False,
                             'reason': '当前正文没有对应的封闭清单审计（正文已变，审计证据已失效）'},
                'required_source_ids': [], 'uncovered_source_ids': [], 'run_id': None}
    inventory = current['report'].get('inventory') or {}
    verdict_scope = inventory.get('verdict_scope') or {}
    unreviewed_dims = inventory.get('unreviewed_dimensions') or []
    # 覆盖证据的三条：来源全部验证、候选全部裁决、**没有未选维度**。
    # 缺任何一条都不发模型请求 —— 直接报告还差什么。
    if not (inventory.get('coverage_complete') and verdict_scope.get('complete') and not unreviewed_dims):
        return {'status': 'coverage_incomplete', 'global_semantic_accepted': False,
                'unresolved': list(pending.get('unresolved') or [])
                + ['node_exit_conflict:' + gap['node_id'] for gap in plan_blocking],
                'plan_coverage': {'blocking': plan_blocking, 'visible': plan_visible},
                'coverage': {'complete': False,
                             'reason': '当前正文的封闭清单审计未完成覆盖'
                                       '（coverage_complete / verdict_scope.complete / 未选维度）',
                             'coverage_complete': bool(inventory.get('coverage_complete')),
                             'verdict_scope_complete': bool(verdict_scope.get('complete')),
                             'unreviewed_dimensions': list(unreviewed_dims)},
                'required_source_ids': [], 'uncovered_source_ids': [], 'run_id': None}

    obs = current['observation']
    sources = dict(obs['data']['sources'])
    for g in rows:
        sources.update(paragraph_sources(g.text or '', f'generation:{g.id}',
                                         prefix=f"G{next(s.segment_no for s in rows if s.id == g.id)}P"))
    required = _mentioned_source_ids(inventory)
    body_ids = {sid for sid, s in sources.items() if s['source_id'].startswith('generation:')}
    required = (required & body_ids) | body_ids

    if provider is None:
        from app.routers.assistant_shared import _ensure_assistant_provider
        provider = _ensure_assistant_provider()
    from app.llm.provider import LocalFallbackLLMProvider
    if isinstance(provider, LocalFallbackLLMProvider):
        raise ValueError('local_fallback')

    report = {
        'project_id': project_id, 'agent_role': 'diagnostic', 'provider_role': 'assistant',
        'task_type': 'body_acceptance', 'skill': {'id': 'body_acceptance', 'version': 1,
                                                   'status': 'experimental'},
        'status': 'failed', 'assessment': 'unknown', 'checks': [], 'decisions': [],
        'unresolved': list(pending.get('unresolved') or [])
        + ['node_exit_conflict:' + gap['node_id'] for gap in plan_blocking],
        'plan_coverage': {'blocking': plan_blocking, 'visible': plan_visible},
        'acceptance': pending.get('acceptance') or (origin_report.get('acceptance') or []),
        'open_check_ids': open_check_ids, 'required_source_ids': sorted(required),
        'inventory_run_id': current['run_id'], 'diagnostic_run_id': int(diagnostic_run_id),
        'body_fingerprint': body_fingerprint(rows), 'model_participated': False,
        'model': getattr(provider, 'model', type(provider).__name__),
        'input_fingerprint': digest({'sources': sources, 'required': sorted(required),
                                     'open_check_ids': open_check_ids}),
    }
    try:
        payload = {'body': [{'generation_id': g.id, 'segment_no': g.segment_no, 'text': g.text or ''}
                            for g in rows],
                   'sources': sources, 'open_check_ids': open_check_ids,
                   'required_source_ids': sorted(required),
                   'original_checks': origin_report.get('checks', []),
                   'acceptance': report['acceptance']}
        # **有界纠错重试**：验收模型偶发返回结构不合规的 check_decisions /
        # coverage（实测整段验收因 `invalid_check_decisions` 直接失败）。
        # 把它回灌给它再问一次，比整次验收作废划算。
        raw = None
        answer = None
        checks, decisions, coverage = [], [], {}
        problems = ['acceptance_output_invalid']
        last_error = None
        truncated = False
        # 输出额度按**必须回抄的 id 数量**申请：验收要逐段回抄 coverage 清单，
        # 正文越长输出越长（实测 876 段 → 9.5KB），额度不够就被拦腰截断
        # （finish_reason=length）→ 验收失败。这里只是把额度申请对，
        # **回抄义务与校验口径一个字没改**。
        out_budget = min(32768, max(4096, 2048 + len(required) * 12))
        for attempt in range(3):
            attempt_payload = payload if attempt == 0 else dict(
                payload, __retry__={'attempt': attempt, 'error': last_error})
            system = ACCEPTANCE_SYSTEM
            if attempt > 0:
                system += (' —— 纠错重试：上一次的返回不合格（' + str(last_error) + '）。'
                           '请只输出要求的 JSON 对象：assessment、dimensions（四项各一次）、'
                           'check_decisions（每个 open_check_id 恰好一次，字段恰好是 '
                           'check_id/status/reason/source_ids）、coverage'
                           '（examined_source_ids/complete/reason）；不要输出任何别的键。')
            try:
                raw = provider.chat(system, json.dumps(attempt_payload, ensure_ascii=False),
                                    max_tokens=out_budget)
            except TypeError:
                # 假 provider / 旧签名没有 max_tokens 形参 → 按原样调用。
                raw = provider.chat(system, json.dumps(attempt_payload, ensure_ascii=False))
            report['model_participated'] = True
            report['model_response_raw'] = safe_response(raw)
            try:
                answer = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
                checks, decisions, coverage, problems = validate_acceptance(
                    answer, sources, open_check_ids, required)
                if not problems:
                    break
                last_error = ';'.join(problems)
            except Exception as exc:  # noqa: BLE001
                answer = None
                problems = [f'{type(exc).__name__}: {exc}']
                last_error = problems[0]
                # 响应被**输出上限**截断 → 不是「模型没答对」，是额度不够。
                # 这两种失败要分开说：前者要改口径，后者要调额度/缩短正文。
                if getattr(provider, 'last_finish_reason', None) == 'length':
                    truncated = True
                    problems.append('acceptance_output_truncated')
            if attempt == 2:
                break
        if answer is None:
            report['acceptance_needed_output_budget'] = out_budget
            if truncated:
                report['acceptance_failure_reason'] = (
                    f'响应被输出上限截断：正文 {len(required)} 段，需回抄 {len(required)} 个段落 id，'
                    f'本次已申请 {out_budget} token 仍不够 —— 要么调高该角色的 max_tokens，'
                    f'要么缩短正文，要么改 coverage 契约（只列未核对项）。')
                raise ValueError('acceptance_output_truncated')
            raise ValueError('acceptance_output_invalid')
        report['checks'] = checks
        report['decisions'] = decisions
        report['coverage'] = coverage
        report['problems'] = problems
        report['status'] = 'completed'
        carried = list(report['unresolved'])
        discharged = set()
        for item, ok in DISCHARGEABLE_BY_CURRENT_INVENTORY.items():
            if item in carried and ok(inventory):
                discharged.add(item)
        for item in DISCHARGEABLE_BY_ACCEPTANCE:
            if item in carried:
                discharged.add(item)
        for decision in decisions:
            if decision['status'] == 'resolved':
                discharged.add('original_check:' + decision['check_id'])
        unresolved = [item for item in carried if item not in discharged]
        # 当前正文的完整审计自身也是证据：它判出**违例**就挡住；判不出（unknown）
        # 不是「确认无问题」——如实记为开放项，不藏也不当成通过。
        open_items = []
        for check in (current['report'].get('checks') or []):
            if check.get('status') == 'contradicted':
                unresolved.append('audit_violation:' + str(check.get('id')))
            elif check.get('status') == 'unknown':
                open_items.append({'id': check.get('id'), 'reason': check.get('reason')})
        report['open_items'] = open_items
        for decision in decisions:
            if decision['status'] != 'resolved':
                unresolved.append('original_check:' + decision['check_id'])
        for check in checks:
            if check['status'] != 'supported':
                unresolved.append('acceptance_' + check['id'].split('_', 1)[1])
        for problem in problems:
            unresolved.append(problem)
        report['unresolved'] = list(dict.fromkeys(unresolved))
        report['discharged'] = sorted(discharged)
        accepted = (not report['unresolved']
                    and all(c['status'] == 'supported' for c in checks)
                    and all(d['status'] == 'resolved' for d in decisions)
                    and coverage.get('complete') is True)
        report['global_semantic_accepted'] = bool(accepted)
    except Exception as exc:  # noqa: BLE001
        report['model_error_category'] = type(exc).__name__
        # 异常**消息**也要留。只留类型名的话，报告里只有「RuntimeError」四个字，
        # 读报告的人分不出这是哪一种失败 —— 而这四种的处置完全不同：
        #   额度用尽（等）、限流（降并发）、输出被截断（调 max_tokens）、
        #   模型答得不合格（回灌重问）。
        # 「报告说不清为什么」和「报告说错了」一样坏。
        report['model_error_message'] = str(exc)[:500]
        # 截断与「答得不合格」是两种失败，要能分辨。
        failure = ('acceptance_output_truncated'
                   if str(exc) == 'acceptance_output_truncated' else 'acceptance_failed')
        report['unresolved'] = list(dict.fromkeys(report['unresolved'] + [failure]))
        report['global_semantic_accepted'] = False

    row = AgentRun(project_id=project_id, role='assistant',
                   status='success' if report['status'] == 'completed' else 'failed',
                   request_hash=report['input_fingerprint'],
                   model=report['model'], finished_at=datetime.now(timezone.utc))
    db.add(row)
    db.flush()
    report['run_id'] = row.id
    row.note = json.dumps(report, ensure_ascii=False)
    db.add(Evidence(run_id=row.id, kind='run', source='diagnostic', ref_id=str(row.id),
                    score=1.0, snippet=json.dumps({'input_fingerprint': report['input_fingerprint'],
                                                   'body_fingerprint': report['body_fingerprint']})))
    db.commit()
    db.refresh(row)

    if report['global_semantic_accepted']:
        timeline = dict(project.plot_timeline or {})
        timeline['pending_acceptance'] = {
            'state': 'accepted', 'unresolved': [], 'closed_check_ids': sorted(set(
                (pending.get('closed_check_ids') or []) + open_check_ids)),
            'acceptance': report['acceptance'],
            'inventory_fingerprint': (inventory.get('input_fingerprint')
                                      or pending.get('inventory_fingerprint')),
            'source_run_id': row.id, 'bound_body_hash': report['body_fingerprint'],
            'acceptance_run_id': row.id,
            'open_items': report.get('open_items') or [],
            'plan_coverage': report.get('plan_coverage') or {},
        }
        project.plot_timeline = timeline
        db.commit()
        db.refresh(project)
    report['status'] = 'accepted' if report['global_semantic_accepted'] else 'blocked'
    return report
