import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.style.character_profile import normalize_character_profile, render_character_profile
from app.generation.prompts import _character_profile_block


def test_unverified_legacy_is_not_prompt_content():
    entry = normalize_character_profile({'身份': '待核秘密城主', '关键经历': ['待核毁城']}, '林川')
    assert entry['身份'] == '待核秘密城主'  # retained for diagnostic review
    assert render_character_profile(entry) == ''
    for mode in ['continuation', 'roleplay', 'original']:
        assert _character_profile_block({'characters': [entry]}, profile=mode, character_names=['林川']) == ''


def test_located_facts_keep_diagnostics_out():
    entry = normalize_character_profile({'claims': [
        {'field': '关键经历', 'value': '拿起灯', 'quote': '林川拿起灯。', 'chapter': 1, 'status': 'observed', 'kind': 'action'},
        {'field': '身份', 'value': '待核秘密城主', 'quote': '不存在', 'chapter': 1, 'status': 'observed', 'kind': 'identity'},
    ]}, '林川', sources=[(1, '林川拿起灯。')])
    for mode in ['continuation', 'roleplay']:
        prompt = _character_profile_block({'characters': [entry]}, profile=mode, character_names=['林川'])
        assert '拿起灯' in prompt and '待核秘密城主' not in prompt and '不存在' not in prompt
    assert _character_profile_block({'characters': [entry]}, profile='original', character_names=['林川']) == ''
