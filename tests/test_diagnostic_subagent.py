"""Behavioral acceptance of the trusted diagnostic boundary."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import unittest

class DiagnosticTests(unittest.TestCase):
    def test_trusted_manifest_and_missing_capability(self):
        from app.agents.diagnostic_skills import load_skill
        skill = load_skill('foundation_input_health', 1, allow_experimental=True)
        self.assertEqual(skill['status'], 'experimental')
        with self.assertRaisesRegex(ValueError, 'missing_capability'):
            load_skill('foundation_input_health', 1, allow_experimental=True, capabilities={})
        with self.assertRaises(ValueError):
            load_skill('../attachment', 1, allow_experimental=True)
        with self.assertRaisesRegex(ValueError, 'experimental'):
            load_skill('foundation_input_health', 1)

    def test_registry_discovery_and_invalid_manifest(self):
        import copy
        from app.agents.registry import get_tool_registry, ALLOWED_READ_ONLY_TOOLS
        from app.agents.diagnostic_skills import load_skill, validate_manifest, discover_skills
        self.assertIn('run_diagnostic_task',ALLOWED_READ_ONLY_TOOLS)
        self.assertTrue(get_tool_registry().resolve('run_diagnostic_task').read_only)
        self.assertEqual(discover_skills(),[])
        manifest=load_skill('foundation_input_health',1,allow_experimental=True)
        for change in [{'status':'validated','source_evidence':[]}, {'steps':[{'capability':'shell'}]}, {'allowed_actions':['exec']}, {'version':True}]:
            bad=copy.deepcopy(manifest);bad.update(change)
            with self.assertRaises(ValueError):validate_manifest(bad)

class ReadBoundaryTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.db import Base
        from app.models import Project, RawNovel, NovelChunk
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.db.add_all([RawNovel(id=1, title='fixture'), RawNovel(id=2, title='other'),
                        NovelChunk(id=1, novel_id=1, chapter_no=1, idx=0, text='小林说道：“我来核对账簿。”'),
                        Project(id=1, title='scope', reference_novel_ids=[1], style_narrative={
                            'characters': [{'name':'小林', 'novel_id':1, 'evidence':[
                                {'chapter':1, 'quote':'我来核对账簿', 'field':'说话方式', 'verification':'located_extract'},
                                {'chapter':1, 'quote':'不存在引文', 'verification':'source_mismatch'}]}]})])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_scope_quote_and_business_read_only(self):
        from app.agents.diagnostic import read_capability
        from sqlalchemy import event
        writes=[]
        def guard(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().split()[0].upper() in {'UPDATE','DELETE','INSERT','CREATE','ALTER'}:
                writes.append(statement)
                raise AssertionError('business write')
        event.listen(self.engine, 'before_cursor_execute', guard)
        result=read_capability(self.db, 1, 'read_distill_evidence', {'novel_ids':[1], 'character':'小林'})
        claims=result['data']['claims']
        self.assertEqual(claims[0]['quote_locations'][0]['start'],'小林说道：“我来核对账簿。”'.index('我来核对账簿'))
        self.assertEqual(claims[1]['quote_locations'],[])
        self.assertEqual(result['coverage']['returned'],2)
        with self.assertRaises(PermissionError):
            read_capability(self.db,1,'read_distill_evidence',{'novel_ids':[2]})
        for cap in ['inspect_timeline_state','inspect_narrative_binding']:
            self.assertIn('input_fingerprint', read_capability(self.db,1,cap,{}))
        self.assertEqual(writes,[])

    def test_run_logs_exact_evidence_scope_and_failed_manifest(self):
        import json
        from unittest.mock import patch
        from sqlalchemy import select,event
        from app.models import AgentRun,Evidence
        from app.agents.diagnostic import run_diagnostic_task
        writes=[]
        def guard(conn,cursor,statement,parameters,context,executemany):
            if statement.lstrip().split()[0].upper() in {'INSERT','UPDATE','DELETE'}:
                self.assertTrue('agent_runs' in statement.lower() or 'evidence' in statement.lower())
                writes.append(statement)
        event.listen(self.engine,'before_cursor_execute',guard)
        class P:
            model='unit'
            def chat(self,*args):return '{"assessment":"unknown","reason":"needs review"}'
        result=run_diagnostic_task(self.db,1,task_type='claim_review',character='小林',allow_experimental=True,provider=P())
        run=self.db.get(AgentRun,result['run_id'])
        self.assertEqual(json.loads(run.note),result)
        rows=self.db.scalars(select(Evidence).where(Evidence.run_id==run.id)).all()
        self.assertEqual(len(rows),len(result['observations']))
        for row,obs in zip(rows,result['observations']):
            stored=json.loads(row.snippet)
            self.assertEqual(stored['evidence_ref'],obs['evidence_ref'])
            self.assertEqual(stored['input_fingerprint'],obs['input_fingerprint'])
            self.assertEqual(float(row.score),1.0)
        with self.assertRaises(PermissionError):
            run_diagnostic_task(self.db,1,novel_ids=[2],allow_experimental=True,provider=P())
        with patch('app.agents.diagnostic.load_skill',side_effect=ValueError('missing_capability:future_outline')):
            failed=run_diagnostic_task(self.db,1,allow_experimental=True,provider=P())
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['missing_capabilities'],['future_outline'])
        self.assertFalse(failed['model_participated'])
        self.assertTrue(writes)

    def test_versioned_skill_extension_and_reverification(self):
        import copy
        from unittest.mock import patch
        from app.agents.diagnostic_skills import load_skill
        from app.agents.diagnostic import run_diagnostic_task
        manifest=copy.deepcopy(load_skill('foundation_input_health',1,allow_experimental=True))
        manifest.update(id='timeline_only',version=2,applicable_tasks=['timeline_review'])
        class P:
            model='unit'
            def chat(self,*args): return '{"assessment":"observed","reason":"local observation only"}'
        with patch('app.agents.diagnostic.load_skill',return_value=manifest) as loader:
            first=run_diagnostic_task(self.db,1,task_type='timeline_review',skill_id='timeline_only',skill_version=2,
                                      allow_experimental=True,provider=P())
            loader.assert_called_once_with('timeline_only',2,allow_experimental=True)
            self.assertEqual(first['skill']['version'],2)
            failed=run_diagnostic_task(self.db,1,task_type='claim_review',skill_id='timeline_only',skill_version=2,
                                       allow_experimental=True,provider=P())
            self.assertIn('task_not_applicable',failed['unresolved'])
            self.assertFalse(failed['model_participated'])
        first=run_diagnostic_task(self.db,1,task_type='timeline_review',allow_experimental=True,provider=P())
        again=run_diagnostic_task(self.db,1,task_type='repair_verification',previous_run_id=first['run_id'],
                                  allow_experimental=True,provider=P())
        self.assertFalse(again['input_changed'])
        self.assertIn('independent_semantic_acceptance_required',again['unresolved'])
        with self.assertRaisesRegex(ValueError,'clean_session'):
            self.db.get(__import__('app.models',fromlist=['Project']).Project,1).title='pending'
            run_diagnostic_task(self.db,1,allow_experimental=True,provider=P())

    def test_independent_role_failure_unknown_and_no_promotion(self):
        from app.agents.diagnostic import run_diagnostic_task
        class Provider:
            model='test-diagnostic'
            def chat(self, system, user):
                self.input=(system,user)
                return '{"assessment":"unknown","reason":"Only a local extract, no semantic proof"}'
        p=Provider()
        result=run_diagnostic_task(self.db,1,task_type='claim_review', character='小林',
                                   allow_experimental=True, provider=p)
        self.assertTrue(result['model_participated'])
        self.assertEqual(result['agent_role'],'diagnostic')
        self.assertEqual(result['provider_role'],'assistant')
        self.assertEqual(result['skill']['status'],'experimental')
        self.assertEqual(result['assessment'],'unknown')
        self.assertIn('quote_locations',p.input[1])
        self.assertNotIn('plot_timeline',p.input[1])
        class Broken:
            model='broken'
            def chat(self,*args): raise RuntimeError('secret must not leak')
        failed=run_diagnostic_task(self.db,1,task_type='timeline_review',allow_experimental=True,provider=Broken())
        self.assertEqual(failed['assessment'],'unknown')
        self.assertFalse(failed['model_participated'])
        self.assertNotIn('secret',str(failed))

if __name__ == '__main__': unittest.main()
