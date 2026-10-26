"""声音层规范化的纪律：歧义名解析、空规则剔除、名字审计跑满。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'helpers'))

from normalize_voice_layer import (  # noqa: E402
    is_degenerate_voice, resolve_ambiguous_name,
)

AMBIGUOUS = {'赵', '慕容'}


def test_ambiguous_name_resolved_from_the_rule_itself():
    """「赵」那条规则自己写着「以姓名"赵磐石"自称」——能从规则自身解析。"""
    rule = '赵： 自称「以姓名“赵磐石”自称；内心认同强硬自我立场时使用“老子”」 口头禅「真是可笑至极」'
    corpus = '赵磐石站在门口。'
    resolved, problem = resolve_ambiguous_name('赵', rule, corpus, AMBIGUOUS)
    assert resolved == '赵磐石'
    assert problem is None


def test_ambiguous_name_not_guessed_when_unresolvable():
    """解析不出来就保持歧义，不得猜（慕容那条解析不出）。"""
    rule = '慕容： 自称「本段未出现可直接识别的自称。」'
    resolved, problem = resolve_ambiguous_name('慕容', rule, '慕容桐皇来了。', AMBIGUOUS)
    assert resolved == '慕容'
    assert problem == 'ambiguous_unresolvable'


def test_candidate_must_exist_in_the_corpus():
    """规则里出现但语料里没有的名字不算数。"""
    rule = '赵： 以姓名“赵磐石”自称'
    resolved, problem = resolve_ambiguous_name('赵', rule, '语料里没有这个角色。', AMBIGUOUS)
    assert resolved == '赵'
    assert problem == 'ambiguous_unresolvable'


def test_non_ambiguous_name_is_untouched():
    resolved, problem = resolve_ambiguous_name('陆长风', '陆长风： 自称「我」', '陆长风', AMBIGUOUS)
    assert (resolved, problem) == ('陆长风', None)


def test_degenerate_voice_rule_is_detected():
    """模型自己说「判不出来」的空规则必须被认出来。"""
    assert is_degenerate_voice(
        '慕容： 自称「本段未出现可直接识别的自称。」 句长未形成对白句式特征 语气未出现直接对白，无法判断。')
    assert not is_degenerate_voice('陆长风： 自称「我」 句长偏短，语气清冷。')


def test_voice_normalization_artifact_is_self_consistent():
    path = ROOT / 'docs' / 'authority-distill-voice-normalized.json'
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['voice_rules'] == 178
    assert data['kinds']['canonical'] == 7
    assert data['kinds']['ambiguous_resolved'] >= 1
    assert data['degenerate_rules'] >= 2
    # 名字审计必须跑满（不再 skipped）：不是「只审前 36 个」。
    audit = data.get('name_audit') or {}
    assert audit.get('skipped') is False, audit
    assert audit.get('checked', 0) > 0
    # 保留的规则必须都有可用的名字（没有歧义名、没有空规则）。
    for row in data['normalized_voice_layer']:
        assert row['name'] and row['kind'] != 'ambiguous_unresolvable'
        assert '无法判断' not in row['rule']
