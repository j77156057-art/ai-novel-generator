"""replay_method_view_isolation.py

本文件原本是仓库里的一次性回放脚本，逻辑本身有测试覆盖，
因此降级为**测试辅助模块**放在 tests/helpers/ 下（随测试走，不参与运行时）。
被测的入口：main（回放产出 isolation-replay 报告）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.style.narrative import describe_narrative  # noqa: E402

VIEW = ROOT / 'docs' / 'authority-distill-method-view.json'
VOICES = ROOT / 'docs' / 'authority-distill-voice-normalized.json'
OUT = ROOT / 'docs' / 'authority-distill-isolation-replay.json'


def main():
    view = json.loads(VIEW.read_text(encoding='utf-8'))
    voices = json.loads(VOICES.read_text(encoding='utf-8')) if VOICES.exists() else {}
    l2_names = {'陆长风', '陆崇', '楚惊蛰', '陆北枝', '裴长卿', '小宁', '赵青锋', '孟豹'}
    voice_names = {r.get('name') for r in (voices.get('rows') or []) if r.get('name')}

    # 视图 → 消费路径要的档案形状（按维度分组的规则字典）。
    by_dim = {}
    for rule in view['rules']:
        by_dim.setdefault(rule['dimension'], []).append({'rule': rule['method']})
    profile = {'rules': by_dim, 'characters': [], 'forbidden': {}}

    original = describe_narrative(profile, include_voice=False)
    with_voice = describe_narrative(profile, include_voice=True)
    # 声音层单独喂一次，确认「声音维度一进档案就会被消费」。
    voice_profile = dict(profile)
    voice_profile['rules'] = dict(by_dim)
    voice_profile['rules']['角色声音词典'] = [
        {'rule': r['rule']} for r in (voices.get('normalized_voice_layer') or [])[:20]]
    with_voice_layer = describe_narrative(voice_profile, include_voice=True)

    def hits(text, names):
        return sorted(n for n in names if n and n in text)

    report = {
        'view': str(VIEW),
        'original_text_chars': len(original),
        'original_text': original,
        'checks': {
            'l2_names_in_original': hits(original, l2_names),
            'voice_names_in_original': hits(original, voice_names),
            'evidence_markers_in_original': len(re.findall(r'（证据', original)),
            'voice_names_in_voice_layer_text': hits(with_voice_layer, voice_names),
            # 视图里根本没有声音维度 → include_voice 开关无从改变输出。
            # 这正是「去声音」的结构性保证，不是开关失灵。
            'view_has_voice_dimension': '角色声音词典' in by_dim,
            'voice_switch_changes_output': (with_voice != original),
        },
        'counts': {
            'rules_in_view': len(view['rules']),
            'dimensions': len(by_dim),
            'voice_names_known': len(voice_names),
        },
    }
    report['passed'] = (
        not report['checks']['l2_names_in_original']
        and not report['checks']['voice_names_in_original']
        and report['checks']['evidence_markers_in_original'] == 0
    )
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'written': str(OUT), 'passed': report['passed'],
                      'original_text_chars': report['original_text_chars'],
                      **report['checks']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
