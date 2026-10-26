"""build_distill_method_view.py

本文件原本是仓库里的一次性工具脚本，逻辑本身有测试覆盖，
因此降级为**测试辅助模块**放在 tests/helpers/ 下（随测试走，不参与运行时）。
被测的入口：EVIDENCE、VOICE_DIMENSION、split_rule
"""
from __future__ import annotations

import json
import os
import tempfile
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.style.character_profile import normalize_character_profile  # noqa: E402

ART = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'novel-scratch' / 'foundation-distill-v4')
VIEW_OUT = ROOT / '.cache' / 'authority-distill-method-view.json'
L2_OUT = ROOT / '.cache' / 'authority-distill-l2-clean.json'

VOICE_DIMENSION = '角色声音词典'
#: 规则形态：'<方法句>（证据：<原文>）'。外层收尾括号全角/半角都出现过，
#: 证据本身可能含 '）'（如《二十一疏》），所以用**行尾锚定 + 非贪婪**切，
#: 并且必须跨行匹配（规则是多行的）。
EVIDENCE = re.compile(r'（证据[:：]\s*([\s\S]+?)\s*[)）]\s*$')
CONTAMINATED_CLASSES = {'advertisement'}


def split_rule(text):
    """把规则切成（方法句, 证据, 是否成功切出）。多行、外层括号全/半角混用都要吃得下。"""
    match = EVIDENCE.search(text)
    if not match:
        return text.strip(), '', False
    return text[:match.start()].strip(), match.group(1).strip(), True


def main():
    profile = json.loads((ART / 'v4-full-profile.json').read_text(encoding='utf-8'))
    proposals = json.loads((ART / 'v4-review-proposals.json').read_text(encoding='utf-8'))
    candidates = json.loads((ART / 'v4-probe-candidates.json').read_text(encoding='utf-8'))

    con = sqlite3.connect((ART / 'clean.db').as_uri() + '?mode=ro', uri=True)
    chunks = {row[0]: (row[1] or '') for row in
              con.execute('select id, text from novel_chunks')}
    con.close()
    joined = '\n\n'.join(chunks.values())

    # 参考书专名：L2 规范名 + 候选名（过滤掉过短的噪声）。
    l2_names = {c.get('name') for c in profile.get('characters') or [] if c.get('name')}
    cand_names = set()
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, dict):
            name = item.get('name') or item.get('canonical')
            if isinstance(name, str) and len(name) >= 2:
                cand_names.add(name)
    entity_names = l2_names | cand_names

    # 污染窗口：按 chunk_id 收集被判定为广告残留的区间。
    contaminated = defaultdict(list)
    for p in proposals:
        if not isinstance(p, dict) or p.get('classification') not in CONTAMINATED_CLASSES:
            continue
        contaminated[p.get('chunk_id')].append((p.get('start') or 0, p.get('end') or 0, p.get('quote') or ''))

    def in_contaminated(chunk_id, start, end):
        """引文落点与广告残留区间是否相交（按定位坐标判，不按文字内容猜）。"""
        for s, e, _text in contaminated.get(chunk_id, ()):
            if s is None or e is None:
                continue
            if start < e and s < end:
                return True
        return False

    # 引文定位：quote 在哪个 chunk。
    def locate(quote):
        if not quote:
            return None, None
        for cid, text in chunks.items():
            idx = text.find(quote)
            if idx >= 0:
                return cid, idx
        return None, None

    def _norm(s):
        s = re.sub(r'\s+', '', s)
        s = re.sub(r'[“”"‘’\'`]', '"', s)
        s = re.sub(r'[—–－~～]', '-', s)
        s = re.sub(r'[（(【\[]', '(', s)
        s = re.sub(r'[）)】\]]', ')', s)
        s = re.sub(r'[，,、；;：:！!？?。.！]', ',', s)
        return s

    # 归一化语料**只算一次**（否则 189 条断链 × 5285 块 = 上亿次字符操作）。
    # 用**整条语料**（块与块拼接）而不是逐块：引文可能跨块边界。
    normalized_corpus = _norm('\n\n'.join(chunks.values()))

    def locate_normalized(quote):
        """归一化后能不能找到：空白统一、标点统一（引号/破折号/省略号等）。

        用来区分「引文其实在语料里，只是标点或空白不同（或跨块）」与「语料里根本没有这段」。
        """
        if not quote:
            return False
        target = _norm(quote)
        if len(target) < 4:
            return False
        return target in normalized_corpus

    view = {
        'artifact': str(ART),
        'mode': 'offline_rebuild_from_existing_distillation_no_model_calls',
        'policy': {
            'voice_layer': '角色声音词典维度整维剔除（参考书声音不是可迁移方法）',
            'entities': '规则文本含参考书专名即剔除（实体事实不是可迁移方法）',
            'evidence': '视图内不携带引文；引文只作为服务端可核的出处标记',
            'contamination': '引文落在广告残留窗口内的规则剔除',
            'broken_citation': '引文在真实语料里逐字定位不到的规则剔除',
            'pattern_rules': '无内嵌引文的模式/统计类规则保留，但标记 provenance=pattern_unverified',
        },
        'counts': {},
        'rules': [],
        'provenance': [],
        'excluded': [],
    }
    kept = Counter()
    excluded = Counter()
    recovery = Counter()
    for dim, rules in (profile.get('rules') or {}).items():
        for rule in rules or []:
            text = (rule or {}).get('rule') or ''
            method_text, evidence, separated = split_rule(text)
            m = separated
            reason = None
            if dim == VOICE_DIMENSION:
                reason = 'voice_layer'
            elif not m and '证据' in text:
                reason = 'evidence_not_separable'
            else:
                # 实体过滤只看**方法句**：引文里的专名随引文一起去掉，
                # 不该因为证据里出现人名就把一条干净的方法整条丢掉。
                hit = sorted(n for n in entity_names if n in method_text)
                if hit:
                    reason = 'reference_entity:' + hit[0]
                elif evidence:
                    cid, idx = locate(evidence)
                    if cid is None:
                        reason = 'broken_citation'
                        if locate_normalized(evidence):
                            recovery['normalized_match'] += 1
                            view['excluded'].append({'dimension': dim, 'reason': 'broken_citation_normalized_match',
                                                     'rule': text[:160], 'chapter_no': (rule or {}).get('chapter_no'),
                                                     'excerpt': evidence[:100]})
                        else:
                            recovery['absent'] += 1
                    elif in_contaminated(cid, idx, idx + len(evidence)):
                        reason = 'contaminated_evidence'
            if reason:
                excluded[reason.split(':')[0]] += 1
                view['excluded'].append({'dimension': dim, 'reason': reason, 'rule': text[:160],
                                         'chapter_no': (rule or {}).get('chapter_no')})
                continue
            view['rules'].append({
                'dimension': dim,
                'method': method_text,
                'provenance_kind': 'verbatim_evidence' if evidence else 'pattern_unverified',
                'chapter_no': (rule or {}).get('chapter_no'),
            })
            # 出处单独存：可迁入的规则正文**不带引文**，但每条仍可被服务端逐字复核。
            view['provenance'].append({
                'index': len(view['rules']) - 1,
                'dimension': dim,
                'excerpt': evidence,
                'located': bool(evidence),
            })
            kept[dim] += 1

    # 自检：可迁入部分不得含参考书专名、不得夹带引文、不得是声音层。
    leaked = [r for r in view['rules']
              if any(n in r['method'] for n in l2_names) or '（证据' in r['method']]
    view['self_check'] = {
        'kept_rules': len(view['rules']),
        'rules_leaking_entity_or_evidence': len(leaked),
        'voice_rules_in_view': sum(1 for r in view['rules'] if r['dimension'] == VOICE_DIMENSION),
    }
    if leaked:
        raise SystemExit('method view leaks entity/evidence: ' + json.dumps(leaked[:3], ensure_ascii=False))

    view['counts'] = {
        'source_rules': sum(len(v or []) for v in (profile.get('rules') or {}).values()),
        'kept': len(view['rules']),
        'excluded': len(view['excluded']),
        'kept_per_dimension': dict(sorted(kept.items(), key=lambda kv: -kv[1])),
        'excluded_reasons': dict(excluded),
        'broken_citation_recovery': dict(recovery),
        'pattern_unverified': sum(1 for r in view['rules']
                                  if r['provenance_kind'] == 'pattern_unverified'),
    }
    VIEW_OUT.write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding='utf-8')

    # L2：按当前守卫重审，产出干净档案（缺陷结论被剔除，剔除原因留档）。
    l2 = {'artifact': str(ART), 'mode': 'offline_revalidation_with_current_guards',
          'roles': [], 'totals': {}}
    persisted = Counter()
    current = Counter()
    for entry in profile.get('characters') or []:
        name = entry.get('name')
        sources = []
        for w in entry.get('source_windows') or []:
            if not isinstance(w, dict):
                continue
            q = w.get('quote') or ''
            sources.append((w.get('chapter'), q.split('\n', 1)[1] if '\n' in q else ''))
        claims = [{'field': e.get('field'), 'kind': e.get('kind'), 'quote': e.get('quote'),
                   'value': e.get('value'), 'status': e.get('status'), 'subject': name,
                   'predicate': e.get('predicate') or '', 'chapter': e.get('chapter'),
                   'source': {'chapter': e.get('chapter')}}
                  for e in (entry.get('evidence') or []) if isinstance(e, dict)]
        rebuilt = normalize_character_profile({'claims': claims}, name, sources=sources) if claims else \
            dict(entry, evidence_status='no_claims')
        new_ev = rebuilt.get('evidence') or []
        for e in (entry.get('evidence') or []):
            persisted[e.get('verification')] += 1
        for e in new_ev:
            current[e.get('verification')] += 1
        dropped = [{'field': e.get('field'), 'kind': e.get('kind'), 'quote': (e.get('quote') or '')[:120],
                    'persisted': e.get('verification'), 'current': n.get('verification')}
                   for e, n in zip([x for x in (entry.get('evidence') or []) if isinstance(x, dict)], new_ev)
                   if e.get('verification') != n.get('verification')]
        l2['roles'].append({
            'name': name,
            'fields': {k: rebuilt.get(k) for k in ('身份', '说话方式', '行为倾向', '性格底线',
                                                   '关系网', '关键经历')},
            'evidence_status': rebuilt.get('evidence_status'),
            'evidence_summary': rebuilt.get('evidence_summary'),
            'evidence': new_ev,
            'dropped_from_persisted': dropped,
        })
    l2['totals'] = {'persisted_verdicts': dict(persisted), 'current_verdicts': dict(current),
                    'accepted_before': persisted.get('located_extract', 0),
                    'accepted_now': current.get('located_extract', 0),
                    'dropped_accepted': sum(1 for r in l2['roles']
                                            for d in r['dropped_from_persisted']
                                            if d['persisted'] == 'located_extract')}
    L2_OUT.write_text(json.dumps(l2, ensure_ascii=False, indent=2), encoding='utf-8')

    print(json.dumps({'view': str(VIEW_OUT), 'l2': str(L2_OUT),
                      'source_rules': view['counts']['source_rules'],
                      'kept': view['counts']['kept'], 'excluded': view['counts']['excluded'],
                      'excluded_reasons': view['counts']['excluded_reasons'],
                      'pattern_unverified': view['counts']['pattern_unverified'],
                      'l2_accepted_before': l2['totals']['accepted_before'],
                      'l2_accepted_now': l2['totals']['accepted_now'],
                      'l2_dropped_accepted': l2['totals']['dropped_accepted']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
