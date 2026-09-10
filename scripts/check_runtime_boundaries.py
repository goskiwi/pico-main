"""Targeted checks for the snapshot rewrite; uses real files/processes, no network."""
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pico import (
    FakeModelClient,
    ModelAction,
    Pico,
    PicoConfig,
    SessionStore,
    ToolCall,
    Workspace,
)
from pico.agent_loop import AgentLoop
from pico.completion import CompletionController
from pico.verification_service import VerificationService
from pico.contracts import FailureInfo, ToolOutcome
from pico.execution import ExecutionCancelled, ExecutionContext
from pico.mutations import content_revision


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pico-boundaries-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.agent = Pico.create(FakeModelClient([]), Workspace.build(self.root),
            session_store=SessionStore(self.root / '.pico/sessions'),
            config=PicoConfig(mode='auto', memory_enabled=False))
        AgentLoop(self.agent)._start_task('Fix this fixture')
        self.execution = ExecutionContext.root(max_seconds=10)

    def call(self, name, args):
        call = ToolCall(name, args)
        _, entry = self.agent.session.begin_tool_turn([call])
        self.agent.session.save()
        return self.agent.tools.execute_group([call], entry, self.execution,
                                              self.agent.tools.resolve_surface())[0]

    def test_recovery_before_and_after_publication(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                path = self.root / 'a.py'
                path.write_text('old')
                call = ToolCall('edit_file', {'path': 'a.py'})
                index, entry = self.agent.session.begin_tool_turn([call])
                self.agent.session.start_tool(entry, call.call_id)
                self.agent.session.mutations.append({'id':f'{index}:{call.call_id}',
                    'tool':'edit_file', 'path':'a.py', 'before_revision':content_revision(b'old'),
                    'after_revision':content_revision(b'new'), 'preimage_id':'', 'status':'prepared'})
                self.agent.session.save()
                if applied:
                    path.write_text('new')
                restored = self.agent.session.store.load(self.agent.session.id, self.root)
                self.assertEqual(restored.recover(), 1)
                result = restored.history[index]['results'][call.call_id]
                self.assertEqual(result['side_effect_state'], 'changed' if applied else 'none')
                self.assertEqual(result['affected_paths'], ['a.py'] if applied else [])
                restored.save()
                self.agent.session = restored

    def test_failed_read_does_not_clear_multi_path_uncertainty(self):
        s = self.agent.session
        s.add_unconfirmed('edit', 'edit_file', ['a.py', 'b.py'])
        bad = ToolOutcome('bad', 'read_file', 'error', 'failed', 'none', '',
            structured={'path':'a.py'}, failure=FailureInfo('read_failed', 'failed'))
        self.agent.tools._observe(bad)
        self.assertEqual(s.unconfirmed[0]['paths'], ['a.py','b.py'])
        (self.root / 'a.py').write_text('a')
        self.call('read_file', {'path':'a.py'})
        self.assertEqual(s.unconfirmed[0]['paths'], ['b.py'])

    def test_revoked_permissions_after_approval(self):
        self.agent.config = replace(self.agent.config, mode='code')
        def approve(*args):
            self.agent.config = replace(self.agent.config, mode='ask')
            return True
        self.agent.approval_handler = approve
        result = self.call('write_file', {'path':'a.py', 'content':'new'})
        self.assertEqual(result.status, 'rejected')
        self.assertFalse((self.root/'a.py').exists())

    def test_denial_survives_auto(self):
        self.agent.config = replace(self.agent.config, mode='code')
        self.agent.approval_handler = lambda *args: False
        args = {'path':'a.py','content':'new'}
        self.call('write_file', args)
        self.agent.config = replace(self.agent.config, mode='auto')
        self.assertEqual(self.call('write_file', args).failure.code, 'approval_denied')
        self.assertFalse((self.root/'a.py').exists())

    def test_command_partial_requires_verification(self):
        self.agent.config = replace(self.agent.config, mode='code')
        self.agent.approval_handler = lambda *args: True
        outcome = self.call('run_shell', {'command':'printf new > a.py'})
        self.assertEqual(outcome.side_effect_state, 'partial')
        decision = CompletionController(self.agent).check('done', self.execution)
        self.assertEqual(decision.error, 'verification_required')

    def test_write_after_drift_and_external_observation(self):
        original = self.agent.mutations.write
        def write(path, content):
            receipt = original(path, content)
            Path(path).write_text('external')
            return receipt
        with patch.object(self.agent.mutations, 'write', write):
            result = self.call('write_file', {'path':'a.py','content':'new'})
        self.assertEqual(result.side_effect_state, 'unknown')
        self.assertTrue(self.agent.session.unconfirmed)
        self.call('read_file', {'path':'a.py'})
        self.assertEqual(self.agent.session.unconfirmed, [])
        (self.root/'a.py').write_text('external-again')
        self.assertEqual(VerificationService(self.agent).drift(self.execution), ['a.py'])

    def test_edit_keeps_preimage_and_planned_after(self):
        (self.root/'a.py').write_text('old')
        self.call('read_file', {'path':'a.py'})
        result = self.call('edit_file', {'path':'a.py', 'old_text':'old','new_text':'new'})
        self.assertEqual(result.status, 'success')
        receipt = self.agent.session.mutations[-1]
        self.assertEqual(receipt['after_revision'], content_revision(b'new'))
        self.assertEqual(self.agent.artifacts.read_internal(self.agent.session.id,
                         receipt['preimage_id'])[1], b'old')

    def test_edit_rejects_legacy_revision_parameter(self):
        (self.root/'a.py').write_text('old')
        read = self.call('read_file', {'path':'a.py'})
        result = self.call('edit_file', {'path':'a.py', 'old_text':'old', 'new_text':'new',
                                       'expected_revision':read.structured['revision']})
        self.assertEqual(result.failure.code, 'invalid_arguments')
        self.assertEqual((self.root/'a.py').read_text(), 'old')

    def test_edit_requires_read_and_rejects_external_change(self):
        (self.root/'a.py').write_text('old')
        args = {'path':'a.py', 'old_text':'old', 'new_text':'new'}
        self.assertEqual(self.call('edit_file', args).failure.code, 'read_required')
        self.call('read_file', {'path':'a.py'})
        (self.root/'a.py').write_text('old plus user change')
        self.assertNotEqual(self.call('edit_file', args).status, 'success')
        self.assertEqual((self.root/'a.py').read_text(), 'old plus user change')
        self.call('read_file', {'path':'a.py'})
        self.assertEqual(self.call('edit_file', args).status, 'success')
        self.assertEqual((self.root/'a.py').read_text(), 'new plus user change')

    def test_consecutive_edits_use_internal_receipts(self):
        (self.root/'a.py').write_text('old')
        self.call('read_file', {'path':'a.py'})
        for before, after in [('old','next'), ('next','last')]:
            result = self.call('edit_file', {'path':'a.py', 'old_text':before, 'new_text':after})
            self.assertEqual(result.status, 'success')
        self.assertEqual((self.root/'a.py').read_text(), 'last')

    def test_edit_rechecks_version_after_approval(self):
        (self.root/'a.py').write_text('old')
        self.call('read_file', {'path':'a.py'})
        self.agent.config = replace(self.agent.config, mode='code')
        def approve(*args):
            (self.root/'a.py').write_text('user edit')
            return True
        self.agent.approval_handler = approve
        result = self.call('edit_file', {'path':'a.py','old_text':'old','new_text':'new'})
        self.assertNotEqual(result.status, 'success')
        self.assertEqual((self.root/'a.py').read_text(), 'user edit')

    def test_verify_available_without_unrestricted_command(self):
        self.agent.config = replace(self.agent.config, verification_command='true',
                                    allowed_write_paths=('a.py',))
        self.assertIn('verify', self.agent.tools.resolve_surface().names)
        self.assertNotIn('run_shell', self.agent.tools.resolve_surface().names)
        self.assertEqual(self.call('verify', {}).status, 'success')
        self.assertEqual(self.call('verify', {'command':'false'}).failure.code, 'invalid_arguments')
        self.agent.config = replace(self.agent.config, mode='ask')
        self.assertNotIn('verify', self.agent.tools.resolve_surface().names)

    def test_verify_permission_rechecked_after_approval(self):
        self.agent.config = replace(self.agent.config, mode='code', verification_command='true')
        def approve(*args):
            self.agent.config = replace(self.agent.config, allowed_tools=('read_file',))
            return True
        self.agent.approval_handler = approve
        result = self.call('verify', {})
        self.assertEqual(result.failure.code, 'permission_changed')
        self.assertEqual(self.agent.session.verification['status'], 'not_run')

    def test_verify_failure_repair_and_final_acceptance(self):
        self.agent.config = replace(self.agent.config, verification_command="test \"$(head -1 a.py)\" = good")
        self.agent.model_client.client.outputs = [
            ModelAction.tool('read_file', {'path':'a.py'}),
            ModelAction.tool('verify', {}),
            ModelAction.tool('edit_file', {'path':'a.py','old_text':'bad','new_text':'good'}),
            ModelAction.tool('verify', {}),
            ModelAction.final('Fixed; checks passed.'),
        ]
        (self.root/'a.py').write_text('bad')
        outcome = self.agent.ask('Fix a.py and check it before completion.')
        self.assertEqual(outcome.status, 'completed')
        results = [r for e in self.agent.session.history for r in e.get('results', {}).values()
                   if r['tool_name']=='verify']
        self.assertEqual([r['structured']['exit_code'] for r in results], [1,0])
        self.assertEqual(self.agent.session.verification['status'], 'passed')

    def test_edit_invalidates_passed_verification(self):
        self.agent.config = replace(self.agent.config, verification_command='true')
        self.call('write_file', {'path':'a.py','content':'old'})
        self.call('verify', {})
        self.call('edit_file', {'path':'a.py','old_text':'old','new_text':'new'})
        self.assertEqual(self.agent.session.verification['status'], 'stale')

    def test_no_acceptance_configuration_is_reported_honestly(self):
        self.call('write_file', {'path':'a.py','content':'new'})
        result = CompletionController(self.agent).check('done', self.execution)
        self.assertTrue(result.allowed)
        self.assertIn('no independent acceptance', result.detail)
        self.agent.session.task_policy['verification_floor'] = True
        self.assertEqual(CompletionController(self.agent).check('done', self.execution).error,
                         'verification_required')

    def test_verify_large_output_is_retrievable(self):
        import shlex
        command = shlex.join([sys.executable, '-c', 'print("X" * 9000); print("TAIL-MARKER")'])
        self.agent.config = replace(self.agent.config, verification_command=command)
        result = self.call('verify', {})
        self.assertEqual(result.status, 'success')
        self.assertIn('TAIL-MARKER', result.content)
        artifact = result.structured['artifact_id']
        full = self.agent.artifacts._read_verified(self.agent.session.id, artifact)[1]
        self.assertIn(b'X' * 9000, full)

    def test_verify_interruption_recovers_without_replay(self):
        self.agent.config = replace(self.agent.config, verification_command='true')
        with patch('pico.verification_service.verify_workspace', side_effect=ExecutionCancelled('stop')):
            with self.assertRaises(ExecutionCancelled):
                self.call('verify', {})
        loaded = self.agent.session.store.load(self.agent.session.id, self.root)
        self.assertGreater(loaded.recover(), 0)
        self.assertEqual(loaded.verification['status'], 'interrupted')
        self.assertTrue(loaded.unconfirmed)
        self.assertEqual(loaded.recover(), 0)

    def test_final_acceptance_runs_again_after_midtask_verify(self):
        self.agent.config = replace(self.agent.config, verification_command='true')
        from pico.verification import verify_workspace
        with patch('pico.verification_service.verify_workspace', wraps=verify_workspace) as verify:
            self.assertEqual(self.call('verify', {}).status, 'success')
            self.assertTrue(CompletionController(self.agent).check('done', self.execution).allowed)
            self.assertEqual(verify.call_count, 2)

    def test_final_feedback_is_persisted_before_last_state_check(self):
        self.agent.config = replace(self.agent.config, verification_command='true')
        self.call('write_file', {'path':'a.py','content':'new'})
        original = self.agent.session.store.save
        def save(session):
            if any(e.get('content','').startswith('Runtime verification:') for e in session.history):
                (self.root/'a.py').write_text('external')
            return original(session)
        with patch.object(self.agent.session.store, 'save', save):
            result = CompletionController(self.agent).check('done', self.execution)
        self.assertEqual(result.error, 'verification_stale')

    def test_verify_reports_command_side_effects(self):
        self.agent.config = replace(self.agent.config, verification_command='printf changed > extra.py')
        result = self.call('verify', {})
        self.assertNotEqual(result.status, 'success')
        self.assertEqual(result.side_effect_state, 'partial')
        self.assertIn('extra.py', result.affected_paths)

    def test_read_cancel_has_no_unknown_effect(self):
        (self.root/'a.py').write_text('a')
        def cancel(*args):
            raise ExecutionCancelled('cancelled')
        self.agent.tools.registry['read_file']['run'] = cancel
        with self.assertRaises(ExecutionCancelled):
            self.call('read_file', {'path':'a.py'})
        self.agent.session.recover()
        self.agent.session.save()
        self.assertEqual(self.agent.session.unconfirmed, [])

    def test_parallel_read_runners(self):
        (self.root/'a.py').write_text('a')
        barrier = threading.Barrier(2)
        original = self.agent.tools.registry['read_file']['run']
        def read(*args):
            barrier.wait(timeout=3)
            return original(*args)
        self.agent.tools.registry['read_file']['run'] = read
        calls = [ToolCall('read_file', {'path':'a.py'}) for _ in range(2)]
        _, entry = self.agent.session.begin_tool_turn(calls)
        results = self.agent.tools.execute_group(calls, entry, self.execution,
                                                self.agent.tools.resolve_surface())
        self.assertEqual([r.status for r in results], ['success','success'])
        self.assertEqual([r.tool_call_id for r in results], [c.call_id for c in calls])

    def test_nested_rules_precede_write(self):
        (self.root/'child').mkdir()
        (self.root/'child/AGENTS.md').write_text('Do not create child/a.py')
        self.agent.model_client.client.outputs = [ModelAction.tool('write_file',
            {'path':'child/a.py','content':'bad'}), ModelAction.final('I will not create the file.')]
        outcome = self.agent.ask('Inspect child directory conventions.')
        self.assertEqual(outcome.status, 'completed')
        self.assertFalse((self.root/'child/a.py').exists())
        results = [r for e in self.agent.session.history for r in e.get('results', {}).values()]
        self.assertEqual(results[-1]['failure']['code'], 'repository_instructions_changed')

    def test_failed_request_usage_is_unknown(self):
        result = self.agent.ask('read a file')
        self.assertEqual(result.status, 'stopped')
        self.assertFalse(result.metrics['usage_complete'])

    def test_session_identity_rejected(self):
        data = json.loads(self.agent.session.path.read_text())
        data['id'] = 'different'
        self.agent.session.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.agent.session.store.load(self.agent.session.id, self.root)

    def test_bad_compaction_does_not_commit(self):
        from pico.context_manager import ContextBudgetExceeded
        session = self.agent.session
        session.append_feedback('earlier ' * 100)
        session.observed = len(session.history)
        session.save()
        before = (session.summary, session.covered)
        with (patch.object(self.agent.context.compactor, 'plan', return_value=('long ' * 10000, 1)),
              self.assertRaises(ContextBudgetExceeded)):
            self.agent.context.build(self.agent.tools.resolve_surface(), self.execution, force=True)
        self.assertEqual((session.summary, session.covered), before)

    def test_verify_rechecks_after_persisting_result(self):
        self.agent.config = replace(self.agent.config, verification_command='true')
        self.call('write_file', {'path':'a.py','content':'new'})
        store = self.agent.session.store
        original = store.save
        def save(session):
            result = original(session)
            if session.verification['status'] == 'passed':
                (self.root/'a.py').write_text('user-edit')
            return result
        with patch.object(store, 'save', save):
            decision = CompletionController(self.agent).check('done', self.execution)
        self.assertEqual(decision.error, 'verification_stale')

    def test_current_snapshot_is_sent_each_turn(self):
        from pico.providers.clients import OpenAICompatibleModelClient
        requests = []
        class WireClient(OpenAICompatibleModelClient):
            def _request_response(self, payload, execution_context):
                requests.append(payload)
                name = 'read_file' if len(requests) == 1 else 'submit_final'
                args = {'path':'a.py','start_line':1,'end_line':200} if len(requests) == 1 else {'answer':'done'}
                return {'status':'completed', 'output':[{'type':'function_call', 'name':name,
                    'call_id':f'call{len(requests)}', 'arguments':json.dumps(args)}]}
        from pico.model_usage import MeteredClient
        (self.root/'a.py').write_text('WIRE-SNAPSHOT-742')
        self.agent.model_client = MeteredClient(WireClient('stub','http://127.0.0.1:1/v1','',None,1),
                                               self.agent.usage)
        result = self.agent.ask('Read a.py')
        self.assertEqual(result.status, 'completed')
        self.assertIn('WIRE-SNAPSHOT-742', json.dumps(requests[1]['input']))
        self.assertNotIn('WIRE-SNAPSHOT-742', json.dumps(requests[0]['input']))
        native = requests[1]['input']
        self.assertTrue(any(item.get('type') == 'function_call' for item in native))
        self.assertTrue(any(item.get('type') == 'function_call_output' for item in native))

    def test_runtime_policy_lists_the_same_tools_as_schema(self):
        surface = self.agent.tools.resolve_surface()
        instructions = self.agent.context._instructions(surface, self.execution)
        policy = json.loads(instructions.split('Runtime policy:\n')[1].split('\n\nCurrent workspace')[0])
        self.assertEqual(set(policy['tools']),{tool['name'] for tool in surface.action_tools})
        self.assertIn('submit_final',policy['tools'])

    def test_compacted_snapshot_reaches_wire(self):
        from pico.model_usage import MeteredClient
        from pico.providers.clients import OpenAICompatibleModelClient
        requests = []
        class WireClient(OpenAICompatibleModelClient):
            def _request_response(self, payload, execution_context):
                requests.append(payload)
                return {'status':'completed','output':[{'type':'function_call',
                    'name':'submit_final','call_id':'done','arguments':'{"answer":"done"}'}]}
        self.agent.model_client = MeteredClient(WireClient('stub','http://127.0.0.1:1/v1','',None,1),
                                               self.agent.usage)
        self.agent.config = replace(self.agent.config, provider_context_limit_tokens=8000,
                                    max_new_tokens=1000,compaction_reserve_tokens=2000,
                                    compaction_keep_recent_tokens=500)
        self.agent.session.append_feedback('OLD-RAW-MARKER ' * 7000)
        self.agent.session.observed = len(self.agent.session.history)
        cut = self.agent.session.observed
        self.agent.session.save()
        with patch.object(self.agent.context.compactor,'plan',return_value=('COMPACTED-FACTS',cut)):
            result = self.agent.ask('KEEP-CURRENT-REQUEST-742')
        self.assertEqual(result.status,'completed')
        transmitted = json.dumps(requests[0]['input'])
        self.assertIn('COMPACTED-FACTS',transmitted)
        self.assertIn('KEEP-CURRENT-REQUEST-742',transmitted)
        self.assertNotIn('OLD-RAW-MARKER',transmitted)

    def test_same_process_reconciles_before_new_ask(self):
        call = ToolCall('write_file',{'path':'a.py','content':'x'})
        self.agent.session.begin_tool_turn([call])
        self.agent.session.save()
        self.agent.model_client.client.outputs = [ModelAction.final('No writes required.')]
        self.agent.ask('Continue with an explanation only.')
        result = self.agent.session.history[1]['results'][call.call_id]
        self.assertEqual(result['execution_state'],'not_started')
        self.assertFalse((self.root/'a.py').exists())

    def test_successful_auxiliary_calls_are_counted(self):
        from pico.model_usage import MeteredClient, ModelUsage
        class Stub:
            def complete_action(self):
                self.last_completion_metadata = {'input_tokens':7,'cached_tokens':0,'output_tokens':3}
                return ModelAction.final('ok')
            def new_isolated_client(self):
                return Stub()
        usage = ModelUsage()
        model = MeteredClient(Stub(),usage)
        model.complete_action()
        model.new_isolated_client().complete_action()
        self.assertEqual(usage.snapshot()['input_tokens'],14)
        self.assertEqual(usage.snapshot()['model_responses'],2)
        self.assertTrue(usage.snapshot()['usage_complete'])

    def test_removed_child_interface_rejected(self):
        with self.assertRaises(TypeError):
            Pico.create(FakeModelClient([]),Workspace.build(self.root),
                session_store=SessionStore(self.root/'.pico/old-interface'),
                subagent_model_client_factory=lambda _: FakeModelClient([]))
        self.assertNotIn('integrate_child',self.agent.tools.resolve_surface().names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
