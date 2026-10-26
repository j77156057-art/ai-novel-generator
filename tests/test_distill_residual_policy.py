import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.ingestion.clean import clean_consumption_text


def test_exact_site_tags_mask_only_their_own_span():
    source = '他收藏天下武学。<strong>小说txt下载</strong>她收藏铜钱。<长-风>文学众人离去。'
    text, report = clean_consumption_text(source)
    assert text == '他收藏天下武学。她收藏铜钱。众人离去。'
    assert all(source[s['start']:s['end']] == s['text'] for s in report['removed_spans'])
    assert any(x['marker'] == '收藏' for x in report['unresolved'])


def test_bare_domain_and_literary_collection_preserved():
    source = '收藏天下武学秘笈。甲.info乙（..info好看的小说'
    text, report = clean_consumption_text(source)
    assert text == source
    assert report['unresolved']
