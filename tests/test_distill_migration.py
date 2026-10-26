"""迁移纪律：写入隔离副本、指纹在目标库重算、映射不靠猜、回读与隔离都要成立。"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_migration_report_is_self_consistent():
    path = ROOT / 'docs' / 'authority-distill-migration-report.json'
    if not path.exists():
        return
    r = json.loads(path.read_text(encoding='utf-8'))
    assert r['passed'] is True, r
    # 现用库必须原封不动。
    assert r['target_unchanged'] is True
    assert r['target_before'] == r['target_after']
    # 指纹是在目标库上重算的。
    fp = r['fingerprint_recomputed_on_target']
    assert fp['content_hash'] and fp['source_ids_hash'] and fp['tier_signature']
    # 库里那行的内容与视图一致。
    assert r['readback']['profile_rules'] == r['readback']['rule_count']
    assert r['readback']['status'] == 'archived', '实验级产物不得自动可复用'
    assert r['readback']['characters_in_profile'] == 0, '不搬角色配置'
    # 消费路径隔离在迁移后仍然成立。
    iso = r['isolation_after_migration']
    assert iso['l2_names_in_output'] == []
    assert iso['evidence_markers'] == 0
    assert iso['voice_dimension_present'] is False
    # 引文在目标语料里必须仍然逐字成立（清洗语义一致性）。
    check = r['corpus_excerpt_survival']
    assert check['excerpts_lost_in_target'] == 0, check
    assert check['excerpts_still_verbatim_in_target'] == check['rules_with_located_excerpt']


def test_corpus_mapping_is_not_guessed():
    """映射要么被 chunk 指纹证明，要么明确标注未证明。"""
    path = ROOT / 'docs' / 'authority-distill-migration-report.json'
    if not path.exists():
        return
    mapping = json.loads(path.read_text(encoding='utf-8'))['corpus_mapping']
    assert 'mapping_proven' in mapping
    for novel in mapping['target_novels']:
        assert 'exact_match_with_isolated' in novel
    if not mapping['mapping_proven']:
        # 未证明时只能用候选 id，且必须留在报告里。
        assert mapping['mapped_source_ids']
        assert 'not' in mapping['note'] or '未证明' in mapping['note']
