"""normalize_voice_layer.py

本文件原本是仓库里的一次性工具脚本，逻辑本身有测试覆盖，
因此降级为**测试辅助模块**放在 tests/helpers/ 下（随测试走，不参与运行时）。
被测的入口：is_degenerate_voice、resolve_ambiguous_name
"""
from __future__ import annotations

import json
import os
import tempfile
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))

ART = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'novel-scratch' / 'foundation-distill-v4')
OUT = ROOT / '.cache' / 'authority-distill-voice-normalized.json'
VOICE_DIMENSION = '角色声音词典'
EV = re.compile(r'（证据[:：]\s*([\s\S]+?)\s*[)）]\s*$')
#: 模型自己说"判不出来"的空规则：自称/句长/语气/节奏/关系全是「未出现…无法判断」。
DEGENERATE = ('未出现可直接识别', '无法判断', '未出现直接对白', '未出现以', '无法建立')


def resolve_ambiguous_name(name, rule, corpus, ambiguous):
    """把歧义名解析到具体人：规则正文里出现的、以它为前缀的更长全名。

    「赵」这一条规则自己写着「以姓名"赵磐石"自称」——所以能从**规则自身**解析。
    解析出的全名必须在真实语料里出现过，才算数；解析不出来就保持歧义（不得猜）。

    Returns: (resolved_name, problem) —— problem 为 None 表示已解析或本无歧义。
    """
    if name not in ambiguous:
        return name, None
    quoted = re.findall(r'[以称](?:姓名)?[“"「]([^”"」]{2,8})[”"」]', rule)
    cands = [c for c in quoted if c.startswith(name) and c != name]
    cands += re.findall(re.escape(name) + r'[一-鿿]{1,3}', rule)
    cands = [c for c in cands if c != name and c in corpus]
    if not cands:
        return name, 'ambiguous_unresolvable'
    return max(cands, key=len), None


def is_degenerate_voice(rule):
    """模型自己说「判不出来」的空规则：自称/句长/语气/节奏/关系全是「未出现…无法判断」。"""
    body = rule.split('：', 1)[1] if '：' in rule else rule
    return any(token in body for token in DEGENERATE)


def main():
    # 名字审计要用隔离副本里的模型配置：**先**设 DATABASE_URL 再导入 app.db。
    os.environ['DATABASE_URL'] = 'sqlite:///' + (ART / 'clean.db').as_posix()
    os.environ.setdefault('EMBEDDING_PROVIDER', 'local')
    from app.db import SessionLocal
    from app.routers.settings import load_persisted_llm_config
    from app.llm.registry import get_provider_registry
    from app.llm.provider import LocalFallbackLLMProvider

    profile = json.loads((ART / 'v4-full-profile.json').read_text(encoding='utf-8'))
    l2_audit = profile.get('l2_audit') or []
    voices = (profile.get('rules') or {}).get(VOICE_DIMENSION) or []
    canonical = {c.get('name') for c in profile.get('characters') or [] if c.get('name')}
    ambiguous = {a.get('name') for a in l2_audit if a.get('status') == 'rejected'
                 and a.get('reason') == 'ambiguous_reference'}

    # 语料里出现过的候选全名（用于把「赵」这类前缀解析到具体人）。
    candidates = json.loads((ART / 'v4-probe-candidates.json').read_text(encoding='utf-8'))
    known_names = set(canonical)
    for item in candidates if isinstance(candidates, list) else []:
        if isinstance(item, dict):
            nm = item.get('name') or item.get('canonical')
            if isinstance(nm, str) and len(nm) >= 2:
                known_names.add(nm)
    # 真实语料：解析出的全名必须在里面出现过。
    import sqlite3
    con = sqlite3.connect((ART / 'clean.db').as_uri() + '?mode=ro', uri=True)
    corpus = ''.join(row[0] or '' for row in con.execute('select text from novel_chunks'))
    con.close()



    rows = []
    for v in voices:
        rule = (v or {}).get('rule') or ''
        name = rule.split('：')[0].strip()
        m = EV.search(rule)
        excerpt = (m.group(1) if m else '').strip()
        degenerate = is_degenerate_voice(rule)
        resolved, problem = resolve_ambiguous_name(name, rule, corpus, ambiguous)
        if name in canonical:
            kind = 'canonical'
        elif problem == 'ambiguous_unresolvable':
            kind = 'ambiguous_unresolvable'
        elif resolved != name:
            kind = 'ambiguous_resolved'
        else:
            kind = 'no_l2_profile'
        rows.append({
            'name': name, 'resolved_name': resolved, 'kind': kind,
            'degenerate': degenerate, 'excerpt': excerpt,
            'rule': rule,
            'keep': (not degenerate) and kind != 'ambiguous_unresolvable',
        })

    # 名字审计跑满：所有非规范名一起送审（原来只审前 max_names 个，skipped=true）。
    audit = None
    audit_note = None
    try:
        from app.style.narrative import _audit_character_names
        with SessionLocal() as db:
            load_persisted_llm_config(db)
        provider = get_provider_registry().get('distiller')
        if isinstance(provider, LocalFallbackLLMProvider):
            audit_note = 'distiller 是本地 fallback，未调用模型（不冒充真实审计）'
        else:
            pending = [r for r in rows if r['kind'] != 'canonical']
            audit = _audit_character_names(provider, pending, max_names=len(pending) or 1)
            audit['provider'] = {'class': type(provider).__name__,
                                 'model': getattr(provider, 'model', '')}
            audit['names_submitted'] = len(pending)
    except Exception as exc:  # noqa: BLE001
        audit_note = f'{type(exc).__name__}: {exc}'

    kept = [r for r in rows if r['keep']]
    result = {
        'artifact': str(ART),
        'mode': 'offline_normalization_plus_one_name_audit',
        'voice_rules': len(voices),
        'distinct_names': len({r['name'] for r in rows}),
        'kinds': dict(Counter(r['kind'] for r in rows)),
        'degenerate_rules': sum(1 for r in rows if r['degenerate']),
        'kept': len(kept),
        'dropped': len(rows) - len(kept),
        'drop_reasons': dict(Counter(
            ('degenerate' if r['degenerate'] else r['kind'])
            for r in rows if not r['keep'])),
        'name_audit': audit,
        'name_audit_note': audit_note,
        'rows': rows,
        'normalized_voice_layer': [
            {'name': r['resolved_name'], 'rule': r['rule'], 'excerpt': r['excerpt'],
             'origin_name': r['name'], 'kind': r['kind']} for r in kept],
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'written': str(OUT), 'voice_rules': len(voices),
                      'kinds': result['kinds'], 'degenerate': result['degenerate_rules'],
                      'kept': len(kept), 'dropped': result['dropped'],
                      'audit_checked': (audit or {}).get('checked'),
                      'audit_skipped': (audit or {}).get('skipped'),
                      'audit_suspicious': (audit or {}).get('suspicious'),
                      'audit_note': audit_note}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
