"""剧情圣经必须真的吃到参考语料的风格纹理 —— 而且是**脱敏后**的。

覆盖四个曾经静默降级的点：
1. `sample = ""` 从未被赋值 → 参考样本那段永远不出现；
2. 专名表实体全空却带着新鲜指纹 → 被当成有效表一直复用，脱敏是空操作；
3. `not table` 只看键不看实体 → 空表被当成「有表」→ 参考原文（带专名）直接进提示词；
4. 专名表不可用时没有兜底 → 应当不给原文（fail closed），并把原因说清楚。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db import Base, get_db
from app.models import NovelChunk, Project, RawNovel
from app.routers import projects, wizard

AD_LINE = "（每一个钟头上传一章，直到传完二十章！红票和收藏别忘了～）"
CLEAN_TEXT = "风沙从城墙上卷过来，擦过屋脊，把晒着的粗布掀开一角。沈砚没有去看，他只是把手按在门框上，等那阵风过去。"
STYLE_TEXT = "风沙刮过横水城城墙。沈砚抬头看了一眼天色，指尖在门框上敲了两下，才把话说完。"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        app = FastAPI()
        app.include_router(projects.router)
        app.include_router(wizard.router)
        app.dependency_overrides[get_db] = lambda: db
        novel = RawNovel(title='参考书', genre='玄幻')
        db.add(novel)
        db.flush()
        db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=0,
                           text=AD_LINE + "\n\n\n    " + STYLE_TEXT))
        db.add(NovelChunk(novel_id=novel.id, chapter_no=2, idx=1,
                           text=CLEAN_TEXT))
        p = Project(title='测试', premise='夜里核对旧档', outline=[],
                    reference_novel_ids=[novel.id], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        yield TestClient(app), db, p, novel.id


def _corpus_chars(db, novel_id):
    from app.models import NovelChunk
    rows = db.query(NovelChunk).filter_by(novel_id=novel_id).all()
    return len(chr(10).join((r.text or '') for r in rows))


def _bible_payload():
    return {'synopsis': '沈砚核对旧档', 'direction': '回桥纠错',
            'characters': [{'name': '沈砚', 'role': '修复员', 'motivation': '澄清责任'}],
            'relationships': [], 'conflicts': [], 'foreshadowing': []}


def _writer(captured, payload=None):
    class Writer:
        def chat(self, system, user, **kwargs):
            captured.append(user)
            return json.dumps(payload or _bible_payload(), ensure_ascii=False)
    return Writer()


def test_bible_prompt_carries_the_redacted_reference_sample(api, tmp_path, monkeypatch):
    client, db, p, novel_id = api
    from app.style.entity_store import save_table
    save_table(novel_id, {'characters': ['沈砚'], 'places': ['横水城'], 'factions': [],
                          'items': [], 'version': 2,
                          'source_chunks': 2, 'source_chars': _corpus_chars(db, novel_id)})
    seen = []
    monkeypatch.setattr(wizard, 'get_llm_provider', lambda: _writer(seen))
    r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
    assert r.status_code == 200
    prompt = seen[0]
    # ① 参考样本真的进去了（脱敏后的正文在）
    assert '风格样本' in prompt
    assert '横水城' not in prompt, '专名必须被换掉'
    assert '沈砚' not in prompt, '专名必须被换掉'
    # ② 广告残留被清洗掉了
    assert '红票和收藏' not in prompt
    # ③ 带残留告警的那片被整片跳过，样本来自干净的那片（不静默改字）
    assert '风沙刮过' not in prompt, '带广告残留的片段必须整片跳过'
    assert '风沙从城墙上卷过来' in prompt
    # ④ 脱敏是等长占位符，不是删句子
    assert '＃' in prompt
    # ④ 状态可见
    note = r.json().get('note') or ''
    assert '已脱敏' in note
    assert '跳过 1 片' in note, note


def test_bible_omits_raw_sample_when_entity_table_is_unusable(api, tmp_path, monkeypatch):
    """专名表不可用 → 不给原文（fail closed），并说清楚原因。"""
    client, db, p, novel_id = api
    from app.style.entity_store import save_table
    # 只有元数据、实体全空（旧版本判定失败留下的表）
    save_table(novel_id, {'characters': [], 'places': [], 'factions': [], 'items': [],
                          'version': 2, 'audited': 100, 'source_chunks': 2,
                          'source_chars': _corpus_chars(db, novel_id), 'novel_id': novel_id})
    seen = []
    monkeypatch.setattr(wizard, 'get_llm_provider', lambda: _writer(seen))
    r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
    assert r.status_code == 200
    prompt = seen[0]
    assert '参考语料风格样本' not in prompt, '没有可用专名表时不得给原文'
    assert '横水城' not in prompt
    assert '专名表不可用' in (r.json().get('note') or '')


def test_empty_entity_table_is_stale_but_judge_failed_one_is_not(tmp_path):
    from app.style.entity_store import entity_entries, is_stale
    fresh_meta = {'characters': [], 'places': [], 'factions': [], 'items': [],
                  'version': 2, 'audited': 4067, 'source_chunks': 5323, 'source_chars': 100}
    assert entity_entries(fresh_meta) == []
    assert is_stale(fresh_meta, chunks=5323, chars=100), '实体全空的表必须重建'
    marked = {**fresh_meta, 'judge_failed': True}
    assert not is_stale(marked, chunks=5323, chars=100), '已标记失败的表不要反复重建'


def test_original_exemplars_are_removed_when_entity_table_has_no_entries():
    """空表（只有元数据）不得被当成「有表」→ 原文样例必须撤掉。"""
    from app.generation.ctx_profile import apply_profile
    table = {'characters': [], 'places': [], 'factions': [], 'items': [],
             'version': 2, 'audited': 4067}
    warnings = []
    out = apply_profile({'style_exemplars': [{'text': '横水城的风沙'}],
                         'entity_table': table}, 'original', warnings=warnings)
    assert out['style_exemplars'] == []
    assert any('专名表' in w for w in warnings), warnings
