"""method 视图的构建纪律：切分、实体隔离、声音层隔离、出处可核。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'helpers'))

from build_distill_method_view import EVIDENCE, VOICE_DIMENSION, split_rule  # noqa: E402


def test_evidence_splitter_handles_multiline_and_mixed_brackets():
    """真实数据里踩过的三种形态：多行、全角收尾、半角收尾、证据内含 '）'。"""
    cases = [
        ('方法句（证据：原文）', '方法句', '原文'),
        ('方法句（证据：原文）', '方法句', '原文'),
        ('方法句（证据：当年的《二十一疏》)', '方法句', '当年的《二十一疏》'),
        ('方法句\n换行（证据：跨行的原文）', '方法句\n换行', '跨行的原文'),
    ]
    for raw, want_method, want_evidence in cases:
        method, evidence, ok = split_rule(raw)
        assert ok, raw
        assert method == want_method, (raw, method)
        assert evidence == want_evidence, (raw, evidence)


def test_rule_without_evidence_is_reported_as_not_separable():
    method, evidence, ok = split_rule('没有任何证据标记的规则')
    assert not ok and evidence == '' and method == '没有任何证据标记的规则'


def test_evidence_regex_is_line_aware():
    """旧写法用 '.' 匹配不到跨行证据 → 整条规则的引文会留在方法句里。"""
    assert EVIDENCE.search('方法（证据：\n跨行原文）')
    assert not EVIDENCE.search('方法（证据：没有收尾')


def test_method_view_artifact_carries_no_entity_voice_or_evidence():
    """已产出的 method 视图（若存在）必须自洽：不含专名、不含引文、不含声音层。"""
    path = ROOT / 'docs' / 'authority-distill-method-view.json'
    if not path.exists():
        return
    view = json.loads(path.read_text(encoding='utf-8'))
    l2_names = {'徐凤年', '徐骁', '王仙芝', '徐北枳', '曹长卿', '姜泥', '轩辕青锋', '陈芝豹'}
    for rule in view['rules']:
        assert rule['dimension'] != VOICE_DIMENSION, rule
        assert '（证据' not in rule['method'], rule
        for name in l2_names:
            assert name not in rule['method'], (name, rule)
    assert view['self_check']['rules_leaking_entity_or_evidence'] == 0
    # 每条规则都要能在出处表里找到（可被服务端逐字复核）。
    assert len(view['provenance']) == len(view['rules'])


def test_l2_clean_artifact_drops_the_defective_accepted_claims():
    path = ROOT / 'docs' / 'authority-distill-l2-clean.json'
    if not path.exists():
        return
    l2 = json.loads(path.read_text(encoding='utf-8'))
    assert l2['totals']['accepted_before'] == 17
    assert l2['totals']['accepted_now'] == 10
    assert l2['totals']['dropped_accepted'] == 7
    dropped = [d for role in l2['roles'] for d in role['dropped_from_persisted']
               if d['persisted'] == 'located_extract']
    reasons = {d['current'] for d in dropped}
    assert 'subject_not_actor' in reasons
    assert 'non_speech_context' in reasons
