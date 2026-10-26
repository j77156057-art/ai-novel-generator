import json
from pathlib import Path
import pytest
from test_closed_inventory_v2 import ScopedReview, scoped_db
from app.agents.closed_inventory_v2 import inspect_inventory
from app.agents.diagnostic import run_diagnostic_task
from app.models import Generation


def test_v3_literal_surface_is_repaired_by_deterministic_reanchoring():
    """（契约已更新）字面词对不上 → **先确定性重锚**，修不成才拒。

    ## 为什么改

    旧行为：模型把字面词说错（'她手里的核对表' vs 正文里的 '核对表'）就整条拒掉，
    于是**那一段被判「未验证」** → `coverage_complete=False` → 覆盖永远闭不了 →
    整段验收被永久挡在门外。真实数据：414 条 mention 里 27 条如此（模型把段落/字面词
    说错的比例约 6.5%），闸门因此**结构上不可满足**。

    新行为：mention 被修回正文里**逐字存在**的那个词（同一段内），所以
    「每条 mention 都能在正文里逐字定位」这条契约**没有被放松**；被修的是
    「模型说的字面词一定对」这个本来就不可靠的前提。实体不丢，召回不降。
    """
    real=json.loads((Path(__file__).parent/'fixtures/closed_inventory_v2_source_mismatch.json').read_text(encoding='utf-8'))
    sid='G1P025'; text=real['request']['sources'][sid]
    sources={sid:dict(source_id='generation:10',quote=text,start=100,end=100+len(text)),
        'G1P026':dict(source_id='generation:10',quote='她拿起焊枪。',start=200,end=207),
        'C1S096':dict(source_id='generation:10' if False else 'contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Review(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    dict(source_id=sid,surface='她手里的核对表',canonical='核对表',state='actual',relation='held'),
                    dict(source_id=sid,surface='核对表',canonical='核对表',state='actual',relation='held'),
                    dict(source_id='G1P026',surface='焊枪',canonical='焊枪',state='actual',relation='held')]})
            if d['stage']=='verdicts':
                return json.dumps({'verdicts':[dict(candidate_id=c['id'],status='violated',reason='明确封闭清单以外现实新增',body_source_id=c['mention']['source_id'],authority_source_id=c['constraint']['source_id']) for c in d['candidates']]})
            return super().chat(system,user)
    r={'unresolved':[]}
    inspect_inventory(Review(),{'data':{'segments':[dict(segment_no=1,generation_id=10)],'sources':sources}},r,kinds=['object'],protocol=3)
    inv=r['inventory']
    # 被改写的字面词被修回逐字存在的 '核对表'，实体没有丢
    assert len(inv['mentions'])==3
    assert inv['rejected_mentions']==[]
    assert len(inv['reanchored_mentions'])==1
    fixed=inv['reanchored_mentions'][0]
    assert fixed['reanchored']['token']=='核对表'
    assert fixed['reanchored']['from_source_id']==sid and fixed['reanchored']['to_source_id']==sid
    # 修好之后每一�� mention 都仍然逐字可定位
    for m in inv['mentions']:
        assert m['surface'] in sources[m['source_id']]['quote']
    assert inv['dimensions']['object']['validated_source_ids']==['G1P025','G1P026']
    assert inv['coverage_complete'] is True
    m=inv['mentions'][0]
    assert m['surface']=='核对表' and m['citation']['quote']==text
    assert m['locations'][0]['start']==100+text.index('核对表')
    assert r['checks'][0]['status']=='contradicted'


def test_v3_verdict_group_is_retried_with_the_error_fed_back():
    """裁决不合格 → **回灌纠错**再问一次 → 改对 → 整批不废。

    实测（真实模型）：20 条里整整一组**稳定地**漏掉两个端点回抄字段，
    连跑三轮审计都是同一组、同样错。原来的做法是整批判废 → 审计失败 →
    覆盖永远闭不了。现在把「哪里不合格」告诉模型，它就改对了。
    """
    from app.agents.closed_inventory_v2 import inspect_inventory
    sources={'G1P001':dict(source_id='generation:10',quote='她拿起焊枪。',start=0,end=7),
             'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Flaky:
        def __init__(self): self.calls=[]
        def chat(self,system,user):
            d=json.loads(user)
            self.calls.append((d.get('stage'), system, user))
            if d['stage']=='constraints':
                return json.dumps({'constraints':[{'kind':'object','mode':'closed',
                    'entities':[{'canonical':'扳手','aliases':[]}],'source_id':'C1S096',
                    'quote':'室内仅有扳手。'}]})
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    {'source_id':'G1P001','surface':'焊枪','canonical':'焊枪','state':'actual','relation':'held'}]})
            if d['stage']=='verdicts':
                cands=d['candidates']
                if len(self.calls)==3:      # 第一次：漏掉两个端点字段
                    return json.dumps({'verdicts':[{'candidate_id':c['id'],'status':'violated','reason':'新增现实物件'} for c in cands]})
                return json.dumps({'verdicts':[{'candidate_id':c['id'],'status':'violated','reason':'新增现实物件',
                    'body_source_id':c['body_source_id'],'authority_source_id':c['authority_source_id']} for c in cands]})
            return json.dumps({})
    r={'unresolved':[]}
    provider=Flaky()
    inspect_inventory(provider,{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r,
        protocol=3,kinds=['object'])
    stages=[s for s,_,_ in provider.calls]
    assert stages.count('verdicts')==2, stages
    # 第二次请求里带上了「上一次不合格」的说明
    _stage, retry_system, retry_user=[c for c in provider.calls if c[0]=='verdicts'][1]
    assert '纠错重试' in retry_system
    assert 'invalid_inventory_verdict' in retry_system
    assert '__retry__' in retry_user and 'invalid_inventory_verdict' in retry_user
    assert r['inventory']['coverage_complete'] is True
    assert r['checks'][0]['status']=='contradicted'


def test_v5_entity_kind_is_structured_and_the_status_is_derived_server_side():
    """协议 5：模型只回答「是不是独立新增实体」，**结论由服务端推导**。

    这是通用规则（部件/包装/材质/状态、权威已写明的陈设都不是新增实体），
    不是给某篇正文开的口子；同一输入在两次审计里不可能得到相反结论。
    """
    from app.agents.closed_inventory_v2 import inspect_inventory, derive_status
    # 服务端推导规则本身
    assert derive_status('part_of_authorized', {'mode': 'closed'}) == 'allowed'
    assert derive_status('in_authority_context', {'mode': 'closed'}) == 'allowed'
    assert derive_status('independent', {'mode': 'closed'}) == 'violated'
    assert derive_status('independent', {'mode': 'open'}) == 'allowed'
    assert derive_status('unclear', {'mode': 'closed'}) == 'unknown'
    # 「被授权物容纳」≠「授权物的部件」：夹在书页里的叶子在封闭清单下就是越界
    assert derive_status('contained_in_authorized', {'mode': 'closed'}) == 'violated'
    # 「根本不是个实体」（抽象词/语气词/在这一维度上不是这一类东西）→ 无从越界
    assert derive_status('not_an_entity', {'mode': 'closed'}) == 'allowed'

    sources={'G1P001':dict(source_id='generation:10',quote='她拿起焊枪。',start=0,end=7),
             'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Review(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='constraints':
                return json.dumps({'constraints':[{'kind':'object','mode':'closed',
                    'entities':[{'canonical':'扳手','aliases':[]}],'source_id':'C1S096',
                    'quote':'室内仅有扳手。'}]})
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    {'source_id':'G1P001','surface':'焊枪','canonical':'焊枪','state':'actual','relation':'held'}]})
            if d['stage']=='verdicts':
                cands=d['candidates']
                return json.dumps({'verdicts':[{'candidate_id':c['id'],
                    'entity_kind':'part_of_authorized','reason':'是授权物的一部分',
                    'body_source_id':c['body_source_id'],'authority_source_id':c['authority_source_id']}
                    for c in cands]})
            return json.dumps({})
    r={'unresolved':[]}
    inspect_inventory(Review(),{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r,
        kinds=['object'],protocol=5)
    inv=r['inventory']
    # 模型没给 status，服务端推导出 allowed → supported
    assert r['checks'][0]['status']=='supported'
    assert inv['candidates'][0]['verdict']['status']=='allowed'
    assert inv['candidates'][0]['verdict']['entity_kind']=='part_of_authorized'
    assert inv['coverage_complete'] is True


def test_v5_rejects_a_status_field_from_the_model():
    """协议 5：模型若自己下结论（输出 status），整批判废并纠错重试。"""
    from app.agents.closed_inventory_v2 import inspect_inventory
    sources={'G1P001':dict(source_id='generation:10',quote='她拿起焊枪。',start=0,end=7),
             'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Sloppy(ScopedReview):
        def __init__(self): self.calls=[]
        def chat(self,system,user):
            d=json.loads(user); self.calls.append(d.get('stage'))
            if d['stage']=='constraints':
                return json.dumps({'constraints':[{'kind':'object','mode':'closed',
                    'entities':[{'canonical':'扳手','aliases':[]}],'source_id':'C1S096',
                    'quote':'室内仅有扳手。'}]})
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    {'source_id':'G1P001','surface':'焊枪','canonical':'焊枪','state':'actual','relation':'held'}]})
            if d['stage']=='verdicts':
                cands=d['candidates']
                return json.dumps({'verdicts':[{'candidate_id':c['id'],'status':'violated',
                    'reason':'越界','body_source_id':c['body_source_id'],
                    'authority_source_id':c['authority_source_id']} for c in cands]})
            return json.dumps({})
    r={'unresolved':[]}
    provider=Sloppy()
    with pytest.raises(ValueError):
        inspect_inventory(provider,{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r,
            kinds=['object'],protocol=5)
    # 纠错重试发生过（裁决被重问），且提示里说清了要换成 entity_kind
    assert provider.calls.count('verdicts')>=2


def test_v5_checkpoint_key_follows_the_instructions(tmp_path):
    """改了判据/口径就不能复用旧缓存 —— 检查点键必须包含指令摘要。

    实测踩过：分类法改了，缓存里还是**旧判据**的裁决，于是同一物件
    在两次审计里得到相反结论。
    """
    from app.agents.closed_inventory_v2 import inspect_inventory
    sources={'G1P001':dict(source_id='generation:10',quote='她拿起焊枪。',start=0,end=7),
             'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Review(ScopedReview):
        def __init__(self): self.calls=[]
        def chat(self,system,user):
            d=json.loads(user); self.calls.append((d.get('stage'), system))
            if d['stage']=='constraints':
                return json.dumps({'constraints':[{'kind':'object','mode':'closed',
                    'entities':[{'canonical':'扳手','aliases':[]}],'source_id':'C1S096',
                    'quote':'室内仅有扳手。'}]})
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    {'source_id':'G1P001','surface':'焊枪','canonical':'焊枪','state':'actual','relation':'held'}]})
            if d['stage']=='verdicts':
                cands=d['candidates']
                return json.dumps({'verdicts':[{'candidate_id':c['id'],
                    'entity_kind':'part_of_authorized','reason':'是授权物的一部分',
                    'body_source_id':c['body_source_id'],'authority_source_id':c['authority_source_id']}
                    for c in cands]})
            return json.dumps({})
    ckpt = tmp_path / 'ck'
    r1={'unresolved':[]}
    inspect_inventory(Review(),{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r1,
        kinds=['object'],protocol=5,checkpoint_dir=str(ckpt))
    assert len(list(ckpt.glob('*.json')))>0
    # 同样的输入再跑一次 → 全部走缓存，**不再调模型**
    provider=Review()
    r2={'unresolved':[]}
    inspect_inventory(provider,{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r2,
        kinds=['object'],protocol=5,checkpoint_dir=str(ckpt))
    assert provider.calls==[], '同样的指令应命中检查点'
    # 指令变了 → 键变了 → 必须重新问模型
    import app.agents.closed_inventory_v2 as mod
    original = mod._VERDICT_INSTRUCTIONS_V5
    mod._VERDICT_INSTRUCTIONS_V5 = original + '（口径已改，必须重新判断）'
    try:
        provider2=Review()
        r3={'unresolved':[]}
        inspect_inventory(provider2,{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r3,
            kinds=['object'],protocol=5,checkpoint_dir=str(ckpt))
        assert provider2.calls, '指令变了就不能复用旧缓存'
    finally:
        mod._VERDICT_INSTRUCTIONS_V5 = original


def test_v3_placeholder_entity_does_not_invalidate_its_paragraph():
    """占位实体（模型说「不知道是谁」）不是一条实体提及 → 不进候选，**也不让该段作废**。

    实测：G1P008 是纯引号「要不要先记下封面的情况？」，模型给了
    canonical='未知说话者' / surface='我' —— 正文里两者都不存在。
    这不是「实体定位不到」，是模型**明说不知道是谁**：没有可裁决的实体，
    段落本身确实被扫过了。
    """
    from app.agents.closed_inventory_v2 import inspect_inventory
    sources={'G1P001':dict(source_id='generation:10',quote='“要不要先记下封面的情况？”',start=0,end=15),
             'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Review(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    dict(source_id='G1P001',surface='我',canonical='未知说话者',state='actual',relation='询问')]})
            return super().chat(system,user)
    r={'unresolved':[]}
    inspect_inventory(Review(),{'data':{'segments':[dict(segment_no=1,generation_id=10)],'sources':sources}},r,
        kinds=['person'],protocol=3)
    inv=r['inventory']
    assert inv['mentions']==[]
    assert inv['rejected_mentions'] and inv['rejected_mentions'][0]['reason']=='inventory_placeholder_entity'
    assert inv['dimensions']['person']['validated_source_ids']==['G1P001'], '占位实体不该让该段作废'
    assert inv['coverage_complete'] is True


def test_v3_unrepairable_mention_is_still_rejected_and_invalidates_its_paragraph():
    """修不成的（真幻觉：实体在正文里哪儿都没有）→ 仍然拒 → 该段仍未验证 → 覆盖不完整。

    这是保守兜底：宁可漏报，不可把「说不清来源」的东西算成已覆盖。
    """
    sid='G1P025'
    sources={sid:dict(source_id='generation:10',quote='她拿起焊枪。',start=100,end=107),
        'C1S096':dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)}
    class Review(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                    dict(source_id=sid,surface='不存在的词',canonical='不存在的词',state='actual',relation='held')]})
            return super().chat(system,user)
    r={'unresolved':[]}
    inspect_inventory(Review(),{'data':{'segments':[dict(segment_no=1,generation_id=10)],'sources':sources}},r,kinds=['object'],protocol=3)
    inv=r['inventory']
    assert inv['mentions']==[]
    assert inv['rejected_mentions'] and inv['rejected_mentions'][0]['reason']=='inventory_source_mismatch'
    assert inv['reanchored_mentions']==[]
    assert inv['dimensions']['object']['validated_source_ids']==[]
    assert inv['coverage_complete'] is False


def test_v3_occurrences_are_preserved_or_explicit():
    from app.agents.closed_inventory_v2 import locate_mention
    source=dict(source_id='generation:1',quote='剑碰到剑。',start=30,end=35)
    base=dict(source_id='G1P001',surface='剑',canonical='剑',state='metaphor',relation='compared')
    m=locate_mention(base,{'G1P001':source})
    assert [p['start'] for p in m['locations']]==[30,33]
    assert locate_mention(dict(base,occurrence=2),{'G1P001':source})['locations'][0]['start']==33
    with pytest.raises(ValueError,match='occurrence'):
        locate_mention(dict(base,occurrence=3),{'G1P001':source})
    with pytest.raises(ValueError,match='mismatch'):
        locate_mention(dict(base,surface='宝剑'),{'G1P001':source})


class LiteralReview(ScopedReview):
    def __init__(self, bad_endpoint=False, broken=False):
        super().__init__()
        self.bad_endpoint=bad_endpoint
        self.broken=broken

    def chat(self,system,user):
        d=json.loads(user)
        if d['stage']=='mentions':
            self.calls.append(d)
            if self.broken: return '{'
            return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[
                dict(source_id=sid,surface='焊枪',canonical='焊枪',state='hypothetical',relation='设想') for sid in d['sources']]})
        if d['stage']=='verdicts':
            self.calls.append(d)
            return json.dumps({'verdicts':[dict(candidate_id=c['id'],status='allowed',reason='独立判断非现实',
                body_source_id='G_UNKNOWN' if self.bad_endpoint else c['mention']['source_id'],
                authority_source_id=c['constraint']['source_id']) for c in d['candidates']]})
        return super().chat(system,user)


def test_v3_production_resumes_v2_success_without_repeating_calls(tmp_path):
    db,p=scoped_db()
    kwargs=dict(task_type='body_consistency',skill_id='closed_inventory',inventory_kinds=['object'],
        allow_experimental=True,segment_nos=[1])
    first=run_diagnostic_task(db,p.id,provider=ScopedReview(),skill_version=2,
        inventory_checkpoint_dir=str(tmp_path/'v2'),**kwargs)
    assert first['status']=='completed'
    provider=LiteralReview()
    r=run_diagnostic_task(db,p.id,provider=provider,skill_version=3,
        inventory_checkpoint_dir=str(tmp_path/'v3'),inventory_resume_checkpoint_dir=str(tmp_path/'v2'),
        inventory_verdict_source_ids=['G1P001'],**kwargs)
    assert r['status']=='completed',r
    assert [d['stage'] for d in provider.calls]==['verdicts']
    assert len(r['inventory']['resumed_checkpoints'])==2
    assert r['inventory']['verdict_scope']['complete'] is False
    assert r['inventory']['unreviewed_candidate_ids']==['N002']
    sent_candidate=provider.calls[0]['candidates'][0]
    assert sent_candidate['constraint']['authority_context']['quote']=='室内仅有扳手。'
    g=db.query(Generation).filter_by(project_id=p.id).one()
    g.text+='\n\n她想要焊枪。'; db.commit()
    provider=LiteralReview()
    r=run_diagnostic_task(db,p.id,provider=provider,skill_version=3,
        inventory_resume_checkpoint_dir=str(tmp_path/'v2'),**kwargs)
    assert [d['stage'] for d in provider.calls]==['constraints','mentions','verdicts']
    assert r['inventory']['resumed_checkpoints']==[]
    # A native v3 checkpoint is a previously sent source, including partial batches.
    cache=tmp_path/'native-v3'
    first=run_diagnostic_task(db,p.id,provider=LiteralReview(),skill_version=3,
        inventory_checkpoint_dir=str(cache),**kwargs)
    second=run_diagnostic_task(db,p.id,provider=LiteralReview(),skill_version=3,
        inventory_checkpoint_dir=str(cache),**kwargs)
    assert second['inventory']['dimensions']['object']['sent_source_ids']==first['inventory']['dimensions']['object']['sent_source_ids']


def test_v3_verdict_rejects_wrong_authority_body_endpoint():
    db,p=scoped_db()
    r=run_diagnostic_task(db,p.id,provider=LiteralReview(bad_endpoint=True),skill_version=3,
        task_type='body_consistency',skill_id='closed_inventory',inventory_kinds=['object'],
        allow_experimental=True,segment_nos=[1])
    assert r['status']=='failed'
    assert r['model_error_category']=='inventory_verdict_endpoint_mismatch'


@pytest.mark.parametrize('text,surface,state',[
    ('骑士没有佩剑。','剑','negated'),('技师设想使用焊枪。','焊枪','hypothetical'),
    ('她的目光像手术刀。','手术刀','metaphor'),('飞船未携带推进器。','推进器','negated')])
def test_v3_cross_genre_nonactual_mentions_keep_state(text,surface,state):
    from app.agents.closed_inventory_v2 import locate_mention
    m=locate_mention(dict(source_id='G1P001',surface=surface,canonical=surface,state=state,relation='文本提及'),
        {'G1P001':dict(source_id='generation:1',quote=text,start=0,end=len(text))})
    assert m['state']==state and m['citation']['quote']==text


def test_v3_failed_remaining_batch_still_produces_partial_verdict(tmp_path):
    db,p=scoped_db()
    kwargs=dict(task_type='body_consistency',skill_id='closed_inventory',inventory_kinds=['object'],
        allow_experimental=True,segment_nos=[1])
    # Retain only a successful constraint checkpoint; save the actual v2 batch in two sources.
    run_diagnostic_task(db,p.id,provider=ScopedReview(),skill_version=2,
        inventory_checkpoint_dir=str(tmp_path),**kwargs)
    # Full original input fingerprint is unchanged; force second batch failure via a direct bounded run.
    from app.agents.closed_inventory_v2 import inspect_inventory
    from app.agents.body_evidence import paragraph_sources
    body=paragraph_sources('她拿起焊枪。\n\n她想要焊枪。','generation:1',prefix='G1P')
    sources=dict(body,C1S096=dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8))
    class Once(LiteralReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions' and 'G1P002' in d['sources']:
                self.calls.append(d); return '{'
            return super().chat(system,user)
    r={'unresolved':[]}; provider=Once()
    inspect_inventory(provider,{'data':dict(segments=[dict(segment_no=1,generation_id=1)],sources=sources)},r,
        protocol=3,kinds=['object'],char_budget=22,split_truncated=False)
    assert r['checks'][0]['status']=='supported'
    assert r['inventory']['dimensions']['object']['validated_source_ids']==['G1P001']
    assert not r['inventory']['coverage_complete']
    assert [d['stage'] for d in provider.calls]==['constraints','mentions','mentions','verdicts']


def test_partial_probe_reports_reliable_verdict_without_full_success():
    from probe_closed_inventory_v2 import probe_outcome
    r=dict(status='completed',inventory_coverage=False,persisted_report_matches=True,
        source_unchanged=True,input_unchanged=True,original_body_unchanged=True,
        violations=[dict(citations=[dict(quote='林夏戴上棉质手套',body_source_id='generation:10')])])
    result=probe_outcome(r)
    assert result['success'] is False
    assert result['partial_verdict_verified'] is True
    assert result['probe_status']=='partial_verdict'
    r.update(inventory_coverage=True,inventory_verdict_scope={'complete':False})
    assert probe_outcome(r)['success'] is False


def test_v3_old_real_quote_raw_is_not_new_protocol_success():
    raw=json.loads((Path(__file__).parent/'fixtures/closed_inventory_v2_source_mismatch.json').read_text(encoding='utf-8'))
    sources={s:dict(source_id='generation:10',quote=t,start=0,end=len(t)) for s,t in raw['request']['sources'].items()}
    sources['C1S096']=dict(source_id='contract:1',path='/authority/premise',quote='室内仅有扳手。',start=0,end=8)
    class Replay(ScopedReview):
        def chat(self,system,user):
            if json.loads(user)['stage']=='mentions': return json.dumps(raw['response'])
            return super().chat(system,user)
    r={'unresolved':[]}
    inspect_inventory(Replay(),{'data':dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources)},r,
        protocol=3,kinds=['object'])
    assert r['inventory']['mentions']==[]
    assert r['inventory']['dimensions']['object']['validated_source_ids']==[]
    assert r['inventory']['coverage_complete'] is False
    assert len(r['inventory']['rejected_mentions'])==len(raw['response']['mentions'])
