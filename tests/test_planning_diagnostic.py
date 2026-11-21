import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from test_diagnostic_subagent import ReadBoundaryTests

class PlanningTests(ReadBoundaryTests):
    def test_planning_read_is_scoped_null_safe_and_bound(self):
        from app.agents.diagnostic import read_capability
        from app.models import Project
        from sqlalchemy import event
        def guard(conn,cursor,sql,*args):
            self.assertNotIn(sql.lstrip().split()[0].upper(),{'INSERT','UPDATE','DELETE','CREATE','ALTER'})
        event.listen(self.engine,'before_cursor_execute',guard)
        a=read_capability(self.db,1,'inspect_planning_review',{})
        self.assertEqual(next(s['text'] for s in a['data']['sources'].values() if s['path']=='/premise'),'null')
        self.assertTrue(a['coverage']['complete'])
        with self.assertRaises(PermissionError): read_capability(self.db,1,'inspect_planning_review',{'novel_ids':[2]})
        event.remove(self.engine,'before_cursor_execute',guard)
        self.db.get(Project,1).premise='changed';self.db.commit()
        b=read_capability(self.db,1,'inspect_planning_review',{})
        self.assertNotEqual(a['input_fingerprint'],b['input_fingerprint'])

    def test_planning_semantics_requires_verified_locations(self):
        import json
        from app.agents.diagnostic import run_diagnostic_task
        from app.models import Project
        p=self.db.get(Project,1);p.premise='账册存在矛盾';self.db.commit()
        class Provider:
            model='unit'
            def chat(self,*args):
                sources=json.loads(args[1])['observations'][0]['data']['sources']
                sid=next(k for k,v in sources.items() if v['path']=='/premise')
                return json.dumps({'assessment':'unknown','reason':'缺少机制','findings':[
                    {'assessment':'unknown','reason':'仅泛称矛盾','source_ids':[sid]}]},ensure_ascii=False)
        a=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=Provider())
        self.assertEqual(a['status'],'completed')
        self.assertTrue(a['checks'][0]['citations'][0]['location_verified'])
        self.assertEqual(a['checks'][0]['status'],'unknown')
        self.assertTrue(a['proposed_actions'])
        p.premise='changed';self.db.commit()
        b=run_diagnostic_task(self.db,1,task_type='repair_verification',skill_id='planning_review',previous_run_id=a['run_id'],allow_experimental=True,provider=Provider())
        self.assertTrue(b['input_changed'])
        self.assertEqual(b['status'],'completed')
        self.assertIn('input_changed',b['unresolved'])

    def test_invalid_model_output_preserves_raw_and_category(self):
        from app.agents.diagnostic import run_diagnostic_task
        class BrokenJSON:
            model='unit'
            def chat(self,*args): return '{"assessment":'
        result=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=BrokenJSON())
        self.assertEqual(result['model_response_raw'],'{"assessment":')
        self.assertEqual(result['model_error_category'],'invalid_json')
        self.assertTrue(result['model_participated'])

    def test_source_reference_resolves_server_owned_quote_without_model_arithmetic(self):
        import json
        from app.agents.diagnostic import run_diagnostic_task
        class RefOnly:
            model='unit'
            def chat(self,*args):
                sources=json.loads(args[1])['observations'][0]['data']['sources']
                sid=next(k for k,v in sources.items() if v['path']=='/premise')
                return json.dumps({'assessment':'unknown','reason':'null input','findings':[{'assessment':'unknown','reason':'missing','source_ids':[sid]}]})
        result=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=RefOnly())
        self.assertEqual(result['status'],'completed')
        citation=result['checks'][0]['citations'][0]
        self.assertEqual(citation['quote'],'null')
        self.assertEqual((citation['start'],citation['end']),(0,4))
        self.assertTrue(citation['location_verified'])

    def test_planning_skill_missing_capability_and_invalid_identifier(self):
        from app.agents.diagnostic_skills import load_skill
        with self.assertRaisesRegex(ValueError,'missing_capability:inspect_planning_review'):
            load_skill('planning_review',1,allow_experimental=True,capabilities={})
        with self.assertRaisesRegex(ValueError,'invalid_skill_identifier'):
            load_skill('../planning_review',1,allow_experimental=True)

    def test_invented_source_id_remains_failed(self):
        import json
        from app.agents.diagnostic import run_diagnostic_task
        class Invented:
            model='unit'
            def chat(self,*args):
                return json.dumps({'assessment':'observed','reason':'claim','checks':[{'id':'causal_chain','status':'supported','reason':'claim','citations':[{'source_id':'/timeline/nodes/n3/action'}]}],'proposal':[]})
        result=run_diagnostic_task(self.db,1,task_type='planning_review',skill_id='planning_review',allow_experimental=True,provider=Invented())
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['model_error_category'],'source_location_mismatch')
        self.assertNotIn('checks',result)

if __name__=='__main__': unittest.main()
