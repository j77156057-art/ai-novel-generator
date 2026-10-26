from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Project, RawNovel, NovelChunk
from app.style import narrative as n


def test_project_cache_invalidates_when_actual_chunk_changes():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        novel = RawNovel(title='独立测试', clean_report={'pipeline_version': 'one'})
        db.add(novel); db.flush()
        chunk = NovelChunk(novel_id=novel.id, chapter_no=1, idx=0, text='原始正文')
        db.add(chunk)
        project = Project(title='测试', reference_novel_ids=[novel.id])
        db.add(project); db.commit()
        def extracted(*args, **kwargs):
            return {'narrative_version': n.NARRATIVE_VERSION, 'rules': {}, 'characters': [], 'n_chapters': 1}
        with patch.object(n, 'extract_narrative_profile', side_effect=extracted) as extract:
            first = n.project_narrative(db, project)
            n.project_narrative(db, project)
            assert extract.call_count == 1
            chunk.text = '清洗后的正文'; db.commit()
            second = n.project_narrative(db, project)
            assert extract.call_count == 2, 'actual chunk mutation reused stale project cache'
            assert first['fingerprint'] != second['fingerprint']
            novel.clean_report = {'pipeline_version': 'two'}; db.commit()
            n.project_narrative(db, project)
            assert extract.call_count == 3, 'cleaning pipeline change reused stale cache'


def test_library_rejects_old_narrative_version():
    from app.models import StyleDistill
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(StyleDistill(source_ids=[1], source_ids_hash=n._source_ids_hash([1]),
                           tier='full', tier_signature='sig', content_hash='hash',
                           narrative_version=n.NARRATIVE_VERSION-1, status='active',
                           style_narrative={'narrative_version': n.NARRATIVE_VERSION-1}))
        db.commit()
        assert n.find_style_distill(db,[1],'full','sig','hash') is None

