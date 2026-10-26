from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Project, RawNovel, NovelChunk
from app.generation.engine import build_context, _l2_character_names
from app.generation.prompts import build_system_prompt, build_user_prompt


def fixture():
    db = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(db.bind)
    p = Project(title='支线', reference_novel_ids=[1], premise='留在东院', summary=['东院前文']*4,
                plot_bible={'characters':[{'name':'张三'}]}, generation_length=1000)
    db.add_all([p, RawNovel(id=1,title='参考')])
    for ch in range(1,8):
        db.add(NovelChunk(novel_id=1,chapter_no=ch,idx=0,text=(f'张三第{ch}章'+('未来哨兵' if ch>5 else '过去原文'))*90))
    db.commit()
    return db,p


def final(db,p):
    ctx=build_context(db,p,profile='continuation')
    system=build_system_prompt(p.title,ctx['reminders'],None,p.plot_bible,None,ctx.get('style_narrative'),profile='continuation',character_names=_l2_character_names(p,ctx,'continuation'))
    user=build_user_prompt(ctx['prior_summaries'],ctx['rag_context'],p.outline,1000,p.plot_bible,p.premise,prior_blocks=ctx['prior_blocks'],rag_blocks=ctx.get('rag_blocks'),style_exemplars=ctx.get('style_exemplars'))
    return ctx,system+'\n'+user+'\n'+str(ctx['constraints'])


def test_branch_without_anchor_does_not_infer_original_progress():
    db,p=fixture()
    p.continuation_start_chapter=5
    ctx,prompt=final(db,p)
    assert '必须与之自然衔接' not in prompt
    assert not ctx['rag_items'] and not ctx['style_exemplars']
    assert ctx['visibility_diagnostics']['warnings']


def test_cutoff_applies_to_final_rag_exemplar_and_whole_l2(monkeypatch):
    import app.generation.engine as engine
    db,p=fixture()
    p.reference_cutoffs=[{'novel_id':1,'chapter':5}]
    p.story_authority='reference_anchor'
    p.style_narrative={'characters':[
        {'name':'张三','novel_id':1,'source_chapters':[2,6],'身份':'未来L2哨兵'},
        {'name':'李四','source_chapters':[1],'身份':'旧档案哨兵'}]}
    monkeypatch.setattr(engine,'_style_narrative',lambda db,p:p.style_narrative)
    ctx,prompt=final(db,p)
    assert '未来哨兵' not in prompt and '未来L2哨兵' not in prompt
    assert '过去原文' in prompt
    assert not ctx['style_narrative']['characters']
    assert {x['reason'] for x in ctx['visibility_diagnostics']['excluded']} >= {'future_source','missing_or_unselected_novel'}


def test_position_never_reads_next_chapter():
    from app.retrieval.strategies import retrieve_for_continuation
    db,p=fixture()
    hits=retrieve_for_continuation(db,1,5,k_pos=3,k_sem=0)
    assert hits and max(h['chapter_no'] for h in hits)<=5
    assert [(h['chapter_no'],h['idx']) for h in hits]==sorted((h['chapter_no'],h['idx']) for h in hits)


def test_project_api_visibility_validation():
    from app.schemas import ProjectCreate, ProjectPatch, ProjectOut
    from app.routers.projects import create_project, patch_project
    from fastapi import HTTPException
    import pytest
    db,p=fixture()
    body=ProjectCreate(title='API',reference_novel_ids=[1],story_authority='reference_anchor',reference_cutoffs=[{'novel_id':1,'chapter':5}])
    created=create_project(body,db)
    assert ProjectOut.model_validate(created).reference_cutoffs==[{'novel_id':1,'chapter':5}]
    for updates in ({'story_authority':None},{'story_authority':'bad'},{'reference_cutoffs':[{'novel_id':2,'chapter':5}]},{'reference_cutoffs':[{'novel_id':1,'chapter':99}]},{'reference_cutoffs':[{'novel_id':1,'chapter':0}]},{'reference_novel_ids':[]},{'reference_cutoffs':[{'novel_id':1,'chapter':5}]*2}):
        with pytest.raises((HTTPException, ValueError)):
            patch_project(created.id,ProjectPatch(**updates),db)
    cleared=patch_project(created.id,ProjectPatch(reference_cutoffs=None),db)
    assert cleared.reference_cutoffs is None


def test_preview_is_read_only_and_excludes_future(monkeypatch):
    import app.routers.generation as router
    db,p=fixture()
    p.reference_cutoffs=[{'novel_id':1,'chapter':5}]
    p.outline=['东院调查']
    db.commit()
    monkeypatch.setattr(router,'_style_profile',lambda *a:None)
    def node(project,written,provider=None,db=None):
        assert provider is None and db is None, 'preview must never plan or persist'
        return None
    monkeypatch.setattr(router,'current_node_prompt',node)
    from app.routers.generation import GeneratePlanBody
    out=router.generate_plan(GeneratePlanBody(project_id=p.id,profile='continuation'),db)
    assert '未来哨兵' not in out['system_prompt']+out['user_prompt']
    assert out['visibility_diagnostics']['cutoffs']==[{'novel_id':1,'chapter':5}]


def test_sync_stream_and_regeneration_consume_filtered_narrative(monkeypatch):
    import app.generation.engine as e
    import pytest
    db,p=fixture()
    p.reference_cutoffs=[{'novel_id':1,'chapter':5}]
    p.style_narrative={'characters':[{'name':'张三','novel_id':1,'source_chapters':[6],'身份':'未来L2哨兵'}]}
    monkeypatch.setattr(e,'_style_narrative',lambda *a:p.style_narrative)
    monkeypatch.setattr(e,'_style_profile',lambda *a:None)
    monkeypatch.setattr(e,'_rollup_summary_tree',lambda *a:None)
    class Captured(Exception): pass
    class Provider:
        def chat(self,system,user,**kwargs):
            assert '未来L2哨兵' not in system and '未来哨兵' not in user
            assert '必须与之自然衔接' not in user
            assert kwargs['constraints']['characters']==['张三']
            raise Captured()
        def stream_chat(self,system,user,**kwargs):
            self.chat(system,user,**kwargs)
            yield ''
    with pytest.raises(Captured): e.generate_next_segment(db,p,provider=Provider(),profile='continuation')
    with pytest.raises(Captured): list(e.generate_next_segment_stream(db,p,provider=Provider(),profile='continuation'))
    system,user,_=e._build_generation_prompts(db,p,before_segment=2,profile='continuation')
    assert '未来L2哨兵' not in system


def test_rag_ambience_cannot_add_active_character():
    db,p=fixture()
    p.plot_bible={}
    p.style_narrative={'characters':[{'name':'王五','novel_id':1,'source_chapters':[1]}]}
    assert _l2_character_names(p,{'rag_items':[{'text':'王五在原书酒楼'}]},'continuation')==[]


def test_exemplar_sampling_uses_visible_pool():
    from app.style.exemplar import sample_style_exemplars
    from app.generation.visibility import VisibilityPolicy
    db,p=fixture()
    p.reference_cutoffs=[{'novel_id':1,'chapter':5}]
    policy=VisibilityPolicy(p,[1])
    samples=sample_style_exemplars(db,p,[1],visibility=policy)
    assert samples and all(h['chapter_no']<=5 for h in samples)


def test_context_never_distills_or_writes_on_read(monkeypatch):
    import app.style.narrative as narrative
    db,p=fixture()
    p.style_narrative=None
    def forbidden(*a,**k): raise AssertionError('read side attempted distillation')
    monkeypatch.setattr(narrative,'project_narrative_safe',forbidden)
    ctx=build_context(db,p,profile='continuation')
    assert ctx['style_narrative'] is None


def test_existing_generated_text_overrides_reference_authority():
    from app.models import Generation
    db,p=fixture()
    p.summary=[]
    p.story_authority='reference_anchor'
    p.reference_cutoffs=[{'novel_id':1,'chapter':5}]
    db.add(Generation(project_id=p.id,segment_no=1,text='东院支线正文'))
    db.commit()
    ctx,prompt=final(db,p)
    assert '必须与之自然衔接' not in prompt
    assert ctx['visibility_diagnostics']['authority']=='generated_branch'


def test_visible_l2_is_kept_and_multi_book_cutoffs_are_independent(monkeypatch):
    import app.generation.engine as e
    db,p=fixture()
    db.add(RawNovel(id=2,title='第二本'))
    db.add(NovelChunk(novel_id=2,chapter_no=2,idx=0,text='第二本未来哨兵'*90))
    db.commit()
    p.reference_novel_ids=[1,2]
    p.reference_cutoffs=[{'novel_id':1,'chapter':5},{'novel_id':2,'chapter':1}]
    from app.style.character_profile import normalize_character_profile
    verified=normalize_character_profile({'claims':[{'field':'身份','value':'可见L2身份',
        'quote':'张三是可见L2身份。','chapter':1,'status':'observed','kind':'identity'}]},
        '张三',sources=[(1,'张三是可见L2身份。')])
    verified.update(novel_id=1,source_chapters=[1,5])
    p.style_narrative={'characters':[verified]}
    ctx,prompt=final(db,p)
    assert '可见L2身份' in prompt and '第二本未来哨兵' not in prompt
    assert ctx['style_narrative']['characters']








