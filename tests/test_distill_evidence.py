import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.style import narrative as n
from app.style.character_profile import (
    build_character_profile_prompt,
    normalize_character_profile,
    render_character_profile,
)


def test_ambiguous_alias_never_picks_arbitrary_full_name():
    assert n._normalize_character_name('欧阳', [('欧阳明', 8), ('欧阳青', 5)], {}) is None
    assert n._normalize_character_name('欧阳', [('欧阳明', 8), ('欧阳青', 5)], {'欧阳': '欧阳明'}) is None


def test_candidate_requires_person_evidence_and_disambiguation():
    chapters = [(1, '欧阳明说道：“走吧。”欧阳青摇头。欧阳家族齐聚。')]
    assert n._l2_candidate_reason('欧阳', chapters, ['欧阳明', '欧阳青']) == 'ambiguous_reference'
    assert n._l2_candidate_reason('欧阳明', chapters, ['欧阳明', '欧阳青']) is None
    assert n._l2_candidate_reason('苍山', [(1, '苍山门派坐落苍山之巅。')], ['苍山']) == 'no_person_evidence'


def test_claims_only_located_observed_extracts_enter_facts():
    raw = {'name': '林川', '身份': '天帝', '关键经历': ['毁灭世界'], 'claims': [
        {'field': '关键经历', 'value': '林川拿起灯。', 'quote': '林川拿起灯。', 'chapter': 1, 'status': 'observed', 'kind': 'action'},
        {'field': '关系网', 'value': '夫妻', 'quote': '林川拿起灯。', 'chapter': 1, 'status': 'inferred', 'kind': 'relationship'},
        {'field': '说话方式', 'value': '你来了', 'quote': '你来了', 'chapter': 2, 'status': 'observed', 'kind': 'speech'},
        {'field': '身份', 'value': '天帝', 'quote': '林川拿起灯。', 'chapter': 1, 'status': 'observed', 'kind': 'identity'},
    ]}
    profile = normalize_character_profile(raw, '林川', sources=[(1, '林川拿起灯。')])
    assert profile['关键经历'] == ['林川拿起灯。']
    assert profile['身份'] == '' and profile['关系网'] == {} and profile['说话方式'] == ''
    assert profile['evidence_summary']['accepted'] == 1
    assert profile['evidence_summary']['source_mismatch'] == 1
    assert profile['evidence_summary']['unverified'] == 3


def test_inner_thought_cannot_be_injected_as_speech():
    text = '林川心想：“你来了。”'
    raw = {'claims': [{'field': '说话方式', 'value': '你来了', 'quote': text, 'chapter': 1, 'status': 'observed', 'kind': 'thought'}]}
    profile = normalize_character_profile(raw, '林川', sources=[(1, text)])
    assert not profile['说话方式']


def test_known_model_format_variants_do_not_invent_evidence():
    text = '林川站起身。'
    raw = {'evidence': [{'field': '行为倾向', 'value': '「站起身」', 'quote': text, 'chapter': '第1章', 'status': 'observed', 'kind': 'action'}]}
    profile = normalize_character_profile(raw, '林川', sources=[(1, text)])
    assert profile['行为倾向'] == ['站起身']
    assert profile['evidence'][0]['quote'] == text
    assert profile['evidence_summary']['accepted'] == 1


def test_accusation_does_not_become_character_experience():
    text = '林川！你竟敢偷走宝剑！'
    raw = {'claims': [{'field': '关键经历', 'value': '偷走宝剑', 'quote': text, 'chapter': 1, 'status': 'observed', 'kind': 'action'}]}
    profile = normalize_character_profile(raw, '林川', sources=[(1, text)])
    assert not profile['关键经历']


def test_legacy_profile_preserved_but_labelled_unverified():
    profile = normalize_character_profile({'身份': '城主'}, '林川')
    assert profile['身份'] == '城主'
    assert render_character_profile(profile) == ''


def test_action_claim_requires_subject_to_be_the_actor():
    text = '一大一小经常死皮赖脸缠着小宁御剑飞行。'
    raw = {'claims': [{'subject': '小宁', 'predicate': '御剑飞行',
                      'field': '行为倾向', 'value': '一大一小经常死皮赖脸缠着小宁御剑飞行',
                      'quote': text, 'chapter': 962, 'status': 'observed', 'kind': 'action'}]}
    profile = normalize_character_profile(raw, '小宁', sources=[(962, text)])
    assert not profile['行为倾向']
    assert profile['evidence'][0]['verification'] == 'subject_not_actor'


def test_speech_style_claim_cannot_use_eye_action_as_speech():
    text = '赵青锋冷冷看了一眼，嘴角挂起讥讽笑意。'
    raw = {'claims': [{'subject': '赵青锋', 'predicate': '冷冷',
                      'field': '说话方式', 'value': '冷冷', 'quote': text,
                      'chapter': 172, 'status': 'observed', 'kind': 'speech'}]}
    profile = normalize_character_profile(raw, '赵青锋', sources=[(172, text)])
    assert not profile['说话方式']
    assert profile['evidence'][0]['verification'] == 'non_speech_context'


def test_prompt_requires_minimal_claim_provenance_and_statuses():
    system, _ = build_character_profile_prompt('小宁', ['小宁说道：“走。”'])
    assert 'subject,predicate,field,quote,source' in system
    assert 'observed/inferred/unknown' in system
