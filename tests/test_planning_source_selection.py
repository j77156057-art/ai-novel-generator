"""Real planning shape, server-owned sources, bounded model contract."""
import json
from test_diagnostic_subagent import ReadBoundaryTests

class SourceSelectionTests(ReadBoundaryTests):
    def prepare(self):
        from app.models import Project
        p=self.db.get(Project,1)
        p.plot_timeline={'planning':{'nodes':[{'id':'n1','action':'核对账册','result':'发现矛盾'},
            {'id':'n2','action':'另一节点不应发送','result':'另一个结果'}]}}
        self.db.commit()

    def test_short_ids_are_stable_and_mapped_to_real_paths(self):
        from app.agents.diagnostic import read_capability
        self.prepare()
        a=read_capability(self.db,1,'inspect_planning_review',{})
        b=read_capability(self.db,1,'inspect_planning_review',{})
        self.assertEqual(a,b)
        sources=a['data']['sources']
        self.assertTrue(all(k.startswith('S') and k[1:].isdigit() for k in sources))
        source=next(s for s in sources.values() if s['path']=='/timeline/planning/nodes/0/action')
        self.assertEqual(source['text'],'核对账册')
        self.assertEqual(source['end'],len(source['text']))

    def test_compact_findings_select_enumerated_sources_in_one_node_batch(self):
        from app.agents.diagnostic import run_diagnostic_task
        self.prepare()
        class Provider:
            model='unit-real-shape'
            def chat(inner,system,user):
                payload=json.loads(user)
                sources=payload['observations'][0]['data']['sources']
                self.assertNotIn('另一节点不应发送',user)
                self.assertEqual(payload['dimension'],'evidence_responsibility')
                sid=next(k for k,v in sources.items() if v['text']=='核对账册')
                return json.dumps({'assessment':'unknown','reason':'未提供原始记录值',
                    'findings':[{'assessment':'unknown','reason':'仅说明核对，缺少具体冲突值','source_ids':[sid]}]})
        result=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',
            allow_experimental=True,provider=Provider())
        self.assertEqual(result['status'],'completed',result)
        c=result['checks'][0]['citations'][0]
        self.assertEqual(c['path'],'/timeline/planning/nodes/0/action')
        self.assertEqual(c['quote'],'核对账册')
        self.assertTrue(c['location_verified'])
        self.assertFalse(result['coverage'][0]['complete'])

    def test_captured_failed_responses_remain_failed_offline(self):
        from pathlib import Path
        from app.agents.diagnostic import run_diagnostic_task
        fixtures=json.loads((Path(__file__).parent/'fixtures/planning_failed_raw.json').read_text(encoding='utf-8'))
        for item in fixtures:
            if item['raw'] is None:
                continue  # Historical first truncation did not persist raw; never fabricate it.
            with self.subTest(item=item['name']):
                class Replay:
                    model='offline-original-failure'
                    def chat(self,*args): return item['raw']
                r=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=Replay())
                self.assertEqual(r['status'],'failed')
                self.assertEqual(r['model_response_raw'],item['raw'])
                self.assertEqual(r['model_error_category'],'source_location_mismatch')

    def test_accepted_real_response_replays_against_exact_source_table(self):
        from pathlib import Path
        from unittest.mock import patch
        from app.agents.diagnostic import run_diagnostic_task, digest
        fixture=json.loads((Path(__file__).parent/'fixtures/planning_accepted_real_shape.json').read_text(encoding='utf-8'))
        observation={'evidence_ref':'real-shape','capability':'inspect_planning_review','capability_version':1,
            'input_fingerprint':digest(fixture['sources']),'coverage':{'complete':False},'data':{'sources':fixture['sources']}}
        class Replay:
            model=fixture['model']
            def chat(self,*args): return fixture['raw']
        with patch('app.agents.diagnostic.read_capability',return_value=observation):
            r=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=Replay())
        self.assertEqual(r['status'],'completed')
        self.assertIn('semantic_review_required',r['unresolved'])
        self.assertEqual([c['status'] for c in r['checks']],['supported','unknown','unknown'])
        self.assertTrue(all(c['location_verified'] for check in r['checks'] for c in check['citations']))

    def test_unknown_short_id_fails_without_guessing(self):
        from app.agents.diagnostic import run_diagnostic_task
        class Provider:
            model='unit'
            def chat(self,*args):
                return json.dumps({'assessment':'unknown','reason':'missing','findings':[
                    {'assessment':'unknown','reason':'missing','source_ids':['S999999']} ]})
        r=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=Provider())
        self.assertEqual(r['status'],'failed')
        self.assertEqual(r['model_error_category'],'source_location_mismatch')
