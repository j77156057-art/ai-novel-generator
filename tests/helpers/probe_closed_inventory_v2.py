"""probe_closed_inventory_v2.py

本文件原本是仓库里的一次性探针脚本，逻辑本身有测试覆盖，
因此降级为**测试辅助模块**放在 tests/helpers/ 下（随测试走，不参与运行时）。
被测的入口：probe_outcome
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'backend'))
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import AgentRun, Generation, Project
from app.agents.registry import invoke_tool
from app.agents.diagnostic import digest, run_diagnostic_task
from app.routers.settings import load_persisted_llm_config
from app.routers.assistant_shared import _ensure_assistant_provider


def fingerprint(path):
    stat = path.stat()
    return dict(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def probe_outcome(report):
    known = {'手机','手套'}
    found = any(any(token in q.get('quote','') for token in known)
                for check in report.get('violations',[]) for q in check.get('citations',[])
                if q.get('body_source_id','').startswith('generation:'))
    success = (report.get('status')=='completed' and report.get('inventory_coverage') is True
               and report.get('inventory_verdict_scope',{}).get('complete',True) is True
               and found and report.get('persisted_report_matches') is True and all(report.get(k) is True for k in
                   ('source_unchanged','input_unchanged','original_body_unchanged')))
    partial = (report.get('status')=='completed' and found and report.get('persisted_report_matches') is True
               and all(report.get(k) is True for k in ('source_unchanged','input_unchanged','original_body_unchanged')))
    return {'success':bool(success),'partial_verdict_verified':bool(partial),
            'probe_status':'completed' if success else 'partial_verdict' if partial else 'blocked'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input-db', type=Path, required=True)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--project-id', type=int, required=True)
    parser.add_argument('--skill-version', type=int, choices=[2,3,4], default=2)
    parser.add_argument('--previous-run-id', type=int)
    parser.add_argument('--resume-checkpoints', type=Path)
    parser.add_argument('--verdict-source-ids', nargs='+')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    report={'source_before':fingerprint(args.source_db), 'input_before':fingerprint(args.input_db),
            'source_db':str(args.source_db.resolve()),'input_db':str(args.input_db.resolve()),
            'writer_calls':0,'assistant_calls':0,'apply':False}
    target=args.output/'audit.db'
    with sqlite3.connect(args.input_db.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    db=Session(create_engine('sqlite:///'+target.as_posix()))
    try:
        load_persisted_llm_config(db)
        provider=_ensure_assistant_provider()
        from app.llm.provider import LocalFallbackLLMProvider
        if isinstance(provider,LocalFallbackLLMProvider): raise RuntimeError('assistant_local_fallback')
        report['role']={'role':'assistant','class':type(provider).__name__,'model':getattr(provider,'model','')}
        project=db.get(Project,args.project_id)
        generation=db.scalar(select(Generation).where(Generation.project_id==project.id,Generation.segment_no==1))
        before=generation.text
        contract=project.plot_timeline['planning']['segment_contracts']['1']
        report.update(body_hash=hashlib.sha256(before.encode()).hexdigest(), contract_hash=digest(contract))
        original=provider.chat
        def counted(system,user,**kwargs):
            report['assistant_calls']+=1
            index=report['assistant_calls']
            payload=json.loads(user)
            stage=payload['stage']
            (args.output/f'request-{index}-{stage}.json').write_text(json.dumps({'system':system,'payload':payload},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'call':index,'stage':stage}),flush=True)
            raw=original(system,user,**kwargs)
            (args.output/f'response-{index}-{stage}.json').write_text(json.dumps({'raw':raw},ensure_ascii=False),encoding='utf-8')
            return raw
        provider.chat=counted
        full=run_diagnostic_task(db,project.id,task_type='body_consistency',
            skill_id='closed_inventory',skill_version=args.skill_version,allow_experimental=True,segment_nos=[1],
            inventory_kinds=['object'],inventory_checkpoint_dir=str(args.output/'checkpoints'),
            inventory_split_truncated=False,
            inventory_resume_checkpoint_dir=str(args.resume_checkpoints) if args.resume_checkpoints else None,
            inventory_verdict_source_ids=args.verdict_source_ids,previous_run_id=args.previous_run_id)
        compact={'run_id':full['run_id'],'status':full['status']}
        (args.output/'tool.json').write_text(json.dumps(compact,ensure_ascii=False,indent=2),encoding='utf-8')
        full=json.loads(db.get(AgentRun,compact['run_id']).note)
        (args.output/'diagnosis.json').write_text(json.dumps(full,ensure_ascii=False,indent=2),encoding='utf-8')
        checks=full.get('checks',[])
        report.update(run_id=full['run_id'],status=full['status'],error=full.get('model_error_category'),
            evidence_refs=full['evidence_refs'],input_fingerprint=full.get('input_fingerprint'),
            dimensions=full.get('inventory',{}).get('dimensions'),
            unreviewed_dimensions=full.get('inventory',{}).get('unreviewed_dimensions'),
            global_quality_verified=False,
            coverage=full['coverage'],inventory_coverage=full.get('inventory',{}).get('coverage_complete'),
            checks=dict(Counter(c['status'] for c in checks)),unresolved=full['unresolved'],
            stages=dict(Counter(c['stage'] for c in full.get('inventory',{}).get('calls',[]))),
            violations=[c for c in checks if c['status']=='contradicted'],
            mentions=len(full.get('inventory',{}).get('mentions',[])))
        report['inventory_verdict_scope']=full.get('inventory',{}).get('verdict_scope')
        report['inventory_counts']=full.get('inventory',{}).get('counts')
        report['inventory_summary']=full.get('inventory',{}).get('summary')
        report['resumed_checkpoints']=full.get('inventory',{}).get('resumed_checkpoints',[])
        report['rejected_mentions']=full.get('inventory',{}).get('rejected_mentions',[])
        from app.models import Evidence
        evidence=db.scalars(select(Evidence).where(Evidence.run_id==full['run_id'])).all()
        report['persisted_report_matches']=json.loads(db.get(AgentRun,full['run_id']).note)==full
        report['persisted_evidence']=[json.loads(e.snippet) for e in evidence]
        (args.output/'evidence-readback.json').write_text(json.dumps(report['persisted_evidence'],ensure_ascii=False,indent=2),encoding='utf-8')
        db.refresh(generation)
        report['original_body_unchanged']=generation.text==before
    finally:
        db.close()
        report['source_after']=fingerprint(args.source_db)
        report['input_after']=fingerprint(args.input_db)
        report['source_unchanged']=report['source_before']==report['source_after']
        report['input_unchanged']=report['input_before']==report['input_after']
        report.update(probe_outcome(report))
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='violations'},ensure_ascii=False),flush=True)
    return 0 if report['success'] else 1

if __name__=='__main__': raise SystemExit(main())
