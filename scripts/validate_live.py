"""Small, repeatable real-Responses acceptance cases in isolated fixture directories.

Supply credentials through the environment. Never writes credentials into reports.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pico import Pico, PicoConfig, SessionStore, ToolCall, Workspace
from pico.agent_loop import AgentLoop
from pico.cli import build_agent, build_arg_parser
from pico.config import load_project_env
from pico.execution import ExecutionContext
from pico.providers.clients import OpenAICompatibleModelClient
from pico.security import redact_text
from pico.trace import TracePrinter

FIXTURE = 'def total(price_cents, quantity):\n    return price_cents + quantity\n'
CHECKS = ('from pricing import total\nassert total(120, 3) == 360\n'
          'assert total(9, 0) == 0\nassert total(17, 1) == 17\nprint("3 acceptance checks passed")\n')
PROMPT = ('Fix total in pricing.py to multiply integer price_cents by quantity. '
          'Read the file first. Edit only pricing.py, preserve check.py and note.txt. '
          'Do not delegate this repair. Runtime will run the configured verifier.')


def dump(path, data):
    path.write_text(redact_text(json.dumps(data, indent=2, ensure_ascii=False)))


def client():
    return OpenAICompatibleModelClient(os.environ.get('PICO_OPENAI_MODEL', 'gpt-5.6-luna'),
        os.environ['PICO_OPENAI_API_BASE'], os.environ['PICO_OPENAI_API_KEY'], 0.2, 120)


def make(root, *, mode='auto', resume=None):
    config = PicoConfig(mode=mode, max_agent_turns=12, turn_timeout_seconds=300,
        memory_enabled=False, allowed_write_paths=None if mode=='ask' else ('pricing.py',),
        verification_command='' if mode=='ask' else shlex.quote(sys.executable)+' check.py')
    store = SessionStore(root/'.pico/sessions')
    kwargs = {'config':config,'trace':TracePrinter(sys.stdout)}
    return (Pico.resume(client(), Workspace.build(root), session=store.load(resume,root), **kwargs)
            if resume else Pico.create(client(), Workspace.build(root), session_store=store, **kwargs))


def crash_worker(root):
    agent = make(root)
    original = agent.tools._finish
    def finish(entry, call, outcome):
        if call.name == 'edit_file' and outcome.status == 'success':
            # Publication occurred, but the success result has not reached disk.
            os._exit(77)
        return original(entry,call,outcome)
    agent.tools._finish = finish
    outcome = agent.ask(PROMPT)
    dump(root/'worker-outcome.json',outcome.to_dict())
    return 1


def validate(case, root):
    root.mkdir(parents=True, exist_ok=False)
    (root/'pricing.py').write_text(FIXTURE)
    (root/'check.py').write_text(CHECKS)
    (root/'note.txt').write_text('Identifier: COPPER-742. Money unit: integer cents.\n')
    report = {'case':case,'workspace':str(root),'model':os.environ.get('PICO_OPENAI_MODEL'),
              'endpoint':os.environ['PICO_OPENAI_API_BASE']}
    try:
        if case == 'cli_read':
            args = build_arg_parser().parse_args(['--cwd',str(root),'--mode','ask',
                '--base-url',os.environ['PICO_OPENAI_API_BASE'],'--no-memory','--trace'])
            agent = build_agent(args)
            outcome = agent.ask('Read note.txt using read_file; report the identifier and money unit.')
            checks = {'answer_correct':'COPPER-742' in outcome.answer,
                      'source_unchanged':(root/'pricing.py').read_text()==FIXTURE}
        elif case == 'crash_resume':
            process = subprocess.run([sys.executable,__file__,'--worker',str(root)],
                cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=330,check=False)
            report['worker_exit'] = process.returncode
            report['worker_output'] = process.stdout + process.stderr
            if process.returncode != 77:
                raise RuntimeError('Worker did not reach injected post-write crash')
            store = SessionStore(root/'.pico/sessions')
            session_id = store.latest_active()
            before = (root/'pricing.py').read_bytes()
            agent = make(root,resume=session_id)
            recovered = [r for e in agent.session.history for r in e.get('results',{}).values()
                         if (r.get('failure') or {}).get('code') == 'interrupted']
            report['recovered_results'] = recovered
            outcome = agent.ask('Continue after interruption. Inspect pricing.py and verify the multiplication fix. '
                                'Preserve check.py and note.txt; do not replay an already-applied edit.')
            checks = {'crash_injected':True, 'recovery_observed_change':any(
                r['side_effect_state']=='changed' for r in recovered),
                'no_duplicate_edit':sum(c['name']=='edit_file' for e in agent.session.history
                                        for c in e.get('calls',()))==1,
                'content_preserved_on_resume':before==(root/'pricing.py').read_bytes()}
        elif case == 'compaction':
            agent = make(root,mode='ask')
            agent.config = replace(agent.config, provider_context_limit_tokens=28000,
                max_new_tokens=6000,compaction_reserve_tokens=8000,
                compaction_keep_recent_tokens=1500,summary_max_output_tokens=6000)
            AgentLoop(agent)._start_task('Historical fixture investigation')
            (root/'history.txt').write_text('Historical fixture data only.\n'+
                '\n'.join(f'entry {i}: stable informational text for a repository reading experiment.' for i in range(180)))
            execution = ExecutionContext.root(max_seconds=30)
            # Real read results create controlled history pressure, not simulated model successes.
            for _ in range(16):
                call = ToolCall('read_file',{'path':'history.txt','start_line':1,'end_line':200})
                _, entry = agent.session.begin_tool_turn([call])
                agent.tools.execute_group([call],entry,execution,agent.tools.resolve_surface())
            agent.session.observed = len(agent.session.history)
            agent.session.save()
            report['history_kind'] = 'controlled history of actual file reads; observed boundary seeded for compaction isolation'
            outcome = agent.ask('Read note.txt. Return COPPER-742 and explain the money unit. This request must remain intact.')
            checks = {'compaction_committed':agent.session.covered>0,
                      'answer_correct':'COPPER-742' in outcome.answer,
                      'current_request_preserved':agent.session.current_user_text().startswith('Read note.txt.')}
            report['covered'] = agent.session.covered
        elif case == 'delegate':
            agent = make(root)
            agent.config = replace(agent.config,verification_command='',allowed_write_paths=())
            outcome = agent.ask('Use delegate once for read-only investigation: ask the helper to read note.txt '
                                'and report its identifier and money unit. Do not read it yourself or edit files.')
            checks = {'answer_correct':'COPPER-742' in outcome.answer,
                      'source_unchanged':(root/'pricing.py').read_text()==FIXTURE}
        else:
            agent = make(root)
            outcome = agent.ask(PROMPT)
            checks = {}
        calls = [c['name'] for e in agent.session.history for c in e.get('calls',())]
        checks['completed'] = outcome.status == 'completed'
        checks['check_file_unchanged'] = (root/'check.py').read_text()==CHECKS
        if case in ('repair','crash_resume'):
            accept = subprocess.run([sys.executable,'check.py'],cwd=root,capture_output=True,text=True,timeout=10,check=False)
            report['independent_acceptance'] = {'exit':accept.returncode,'stdout':accept.stdout,'stderr':accept.stderr}
            checks['acceptance'] = accept.returncode==0
            checks['runtime_verified'] = outcome.verification=='passed'
        if case == 'delegate':
            checks['delegate_used'] = 'delegate' in calls
            checks['no_parent_edits'] = not set(calls)&{'write_file','edit_file','run_shell'}
        report.update(outcome=outcome.to_dict(),checks=checks,passed=all(checks.values()),
                      actual_tools=calls,session_path=str(agent.session.path))
    except Exception as exc:  # noqa: BLE001 - keep failed live runs in the report
        report.update(passed=False,error=f'{type(exc).__name__}: {exc}')
    dump(root/'result.json',report)
    print(json.dumps({'case':case,'passed':report['passed'],'error':report.get('error'),
                      'checks':report.get('checks')},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    load_project_env(Path(__file__).resolve().parents[1],boundary=Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['cli_read','repair','crash_resume','compaction','delegate'])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--worker',type=Path)
    args = parser.parse_args()
    if args.worker:
        raise SystemExit(crash_worker(args.worker.resolve()))
    if not args.case or not args.output:
        parser.error('--case and --output are required')
    raise SystemExit(validate(args.case,args.output.resolve()))
