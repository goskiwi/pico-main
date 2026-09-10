"""Targeted checks for the snapshot rewrite; uses real files/processes, no network."""
import json
import sys
import tempfile
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
    RunOutcome,
    SessionStore,
    ToolCall,
    Workspace,
)
from pico.agent_loop import AgentLoop
from pico.completion import CompletionController
from pico.contracts import FailureInfo, ToolOutcome
from pico.execution import ExecutionCancelled, ExecutionContext
from pico.mutations import content_revision
from pico.verification_service import VerificationService
from pico.workspace import WorkspaceObservation


class Boundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pico-boundaries-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.agent = Pico.create(FakeModelClient([]), Workspace.build(self.root),
            session_store=SessionStore(self.root / '.pico/sessions'),
            config=PicoConfig(mode='auto'))
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

    def test_rejected_edit_can_be_approved_on_retry_after_read(self):
        (self.root/'a.py').write_text('old')
        self.agent.config = replace(self.agent.config, mode='code')
        decisions = iter((False, True))
        approvals = []
        def approve(*args):
            approvals.append(args)
            return next(decisions)
        self.agent.approval_handler = approve
        self.call('read_file', {'path':'a.py'})
        edit = {'path':'a.py', 'old_text':'old', 'new_text':'new'}
        rejected = self.call('edit_file', edit)
        self.assertEqual(rejected.failure.code, 'approval_denied')
        self.assertEqual((self.root/'a.py').read_text(), 'old')
        self.call('read_file', {'path':'a.py'})
        accepted = self.call('edit_file', edit)
        self.assertEqual(accepted.status, 'success')
        self.assertEqual((self.root/'a.py').read_text(), 'new')
        self.assertEqual(len(approvals), 2)

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

    def test_ask_completion_does_not_run_project_verification(self):
        self.agent.config = replace(
            self.agent.config,
            mode='ask',
            verification_command='false',
        )
        result = CompletionController(self.agent).check('read-only answer', self.execution)
        self.assertTrue(result.allowed)
        self.assertEqual(result.detail, 'read-only answer')

    def test_ask_cannot_complete_a_write_task_awaiting_acceptance(self):
        self.agent.config = replace(self.agent.config, verification_command='false')
        self.call('write_file', {'path': 'a.py', 'content': 'new'})
        self.agent.config = replace(self.agent.config, mode='ask')
        result = CompletionController(self.agent).check('done', self.execution)
        self.assertFalse(result.allowed)
        self.assertEqual(result.error, 'verification_required')

    def test_transcript_append_and_checkpoint_size(self):
        session = self.agent.session
        session.append_feedback('large observation ' * 10000)
        session.save()
        transcript = session.store.transcript_path(session.id)
        original = transcript.read_bytes()
        session.append_feedback('next observation')
        session.save()
        self.assertTrue(transcript.read_bytes().startswith(original))
        self.assertEqual(len(transcript.read_bytes().splitlines()), len(session.history))
        self.assertLess(session.path.stat().st_size, 10000)
        self.assertEqual(session.store.load(session.id).history, session.history)

    def test_failed_checkpoint_drops_uncommitted_transcript_tail(self):
        session = self.agent.session
        transcript = session.store.transcript_path(session.id)
        original = transcript.read_bytes()
        session.append_feedback('not committed')
        with (patch('pico.session_store.atomic_write_json', side_effect=OSError('disk full')),
              self.assertRaises(OSError)):
            session.save()
        self.assertGreater(transcript.stat().st_size, len(original))
        # Simulate an additional torn final write before recovery.
        with transcript.open('ab') as handle:
            handle.write(b'{"kind":')
        restored = session.store.load(session.id)
        self.assertEqual(transcript.read_bytes(), original)
        self.assertNotIn('not committed', str(restored.history))

    def test_checkpoint_failure_after_file_write_recovers_without_replay(self):
        session = self.agent.session
        (self.root / 'a.py').write_text('old')
        call = ToolCall('edit_file', {'path': 'a.py'})
        index, entry = session.begin_tool_turn([call])
        session.start_tool(entry, call.call_id)
        session.mutations.append({
            'id': f'{index}:{call.call_id}', 'tool': 'edit_file', 'path': 'a.py',
            'before_revision': content_revision(b'old'),
            'after_revision': content_revision(b'new'), 'preimage_id': '', 'status': 'prepared',
        })
        session.save()
        (self.root / 'a.py').write_text('new')
        session.finish_tool(entry, call.call_id, ToolOutcome(
            call.call_id, 'edit_file', 'success', 'completed', 'changed', 'done',
            affected_paths=('a.py',), effect_scope='workspace').to_dict())
        with (patch('pico.session_store.atomic_write_json', side_effect=OSError('crash')),
              self.assertRaises(OSError)):
            session.save()
        restored = session.store.load(session.id)
        self.assertEqual(restored.recover(), 1)
        self.assertEqual(restored.history[index]['results'][call.call_id]['side_effect_state'], 'changed')
        restored.save()
        self.assertEqual(restored.store.load(restored.id).recover(), 0)
        self.assertEqual((self.root / 'a.py').read_text(), 'new')

    def test_transcript_read_search_do_not_grant_write_or_clear_uncertainty(self):
        session = self.agent.session
        session.append_user('Only edit auth.py')
        session.append_user('Correction: do not edit auth.py')
        session.add_unconfirmed('unknown-shell', 'run_shell')
        session.save()
        path = str(session.store.transcript_path(session.id))
        result = self.call('read_file', {'path': path, 'start_line': 1, 'end_line': 3})
        self.assertEqual(result.status, 'success')
        self.assertIn('Correction:', result.content)
        result = self.call('search', {'pattern': 'auth.py', 'path': path})
        self.assertEqual(result.status, 'success')
        self.assertIn('Correction:', result.content)
        self.assertFalse(session.unconfirmed[0]['observed'])
        for tool, args in (
            ('read_file', {'path': str(session.path)}),
            ('edit_file', {'path': path, 'old_text': 'auth.py', 'new_text': 'b.py'}),
            ('write_file', {'path': path, 'content': 'overwrite'}),
        ):
            self.assertNotEqual(self.call(tool, args).status, 'success')

    def test_summary_save_failure_keeps_previous_context(self):
        session = self.agent.session
        session.observed = len(session.history)
        session.save()
        with (patch('pico.session_store.atomic_write_json', side_effect=OSError('disk full')),
              self.assertRaises(OSError)):
            self.agent.context._commit_compaction('candidate', 1)
        self.assertEqual((session.summary, session.summary_end), ('', 0))

    def test_two_compactions_update_summary_and_keep_latest_correction(self):
        session = self.agent.session
        self.agent.config = replace(self.agent.config, compaction_keep_recent_tokens=128)
        session.append_user('Only change auth.py')
        for _ in range(8):
            session.append_feedback('old investigation ' * 300)
        session.append_user('Correction: change session.py instead')
        session.observed = len(session.history)
        session.save()
        first = FakeModelClient([ModelAction.tool('submit_compaction_summary', {
            'summary': '## Progress\nInvestigated authentication; current target is session.py.',
        })])
        with patch.object(self.agent.model_client.client, 'new_isolated_client',
                          return_value=first, create=True):
            self.agent.context.build(self.agent.tools.resolve_surface(), self.execution, force=True)
        first_end = session.summary_end
        for _ in range(8):
            session.append_feedback('new investigation ' * 300)
        correction = 'Correction again: explain only; do not edit files.'
        session.append_user(correction)
        session.observed = len(session.history)
        session.save()
        second = FakeModelClient([ModelAction.tool('submit_compaction_summary', {
            'summary': '## Progress\nInvestigation complete. User now requests explanation only.',
        })])
        with patch.object(self.agent.model_client.client, 'new_isolated_client',
                          return_value=second, create=True):
            _, messages = self.agent.context.build(
                self.agent.tools.resolve_surface(), self.execution, force=True)
        self.assertGreater(session.summary_end, first_end)
        source = json.loads(second.prompts[0])
        self.assertIn('session.py', source['previous_summary'])
        self.assertEqual(source['current_request'], correction)
        self.assertNotIn('old investigation', str(source['history']))
        user_text = [m.get('content') for m in messages if m.get('role') == 'user']
        self.assertEqual(user_text.count(correction), 1)
        self.assertEqual(session.store.load(session.id).history, session.history)

    def test_corrupt_committed_transcript_is_rejected_without_repair(self):
        path = self.agent.session.store.transcript_path(self.agent.session.id)
        original = path.read_bytes()
        path.write_bytes(b'!' + original[1:])
        with self.assertRaises(ValueError):
            self.agent.session.store.load(self.agent.session.id)
        self.assertEqual(path.read_bytes(), b'!' + original[1:])

    def test_verify_permission_rechecked_after_approval(self):
        self.agent.config = replace(self.agent.config, mode='code', verification_command='true')
        def approve(*args):
            self.agent.config = replace(self.agent.config, allowed_tools=('read_file',))
            return True
        self.agent.approval_handler = approve
        result = self.call('verify', {})
        self.assertEqual(result.failure.code, 'permission_changed')
        self.assertEqual(self.agent.session.verification['status'], 'not_run')

    def test_rejected_verification_can_be_approved_on_retry(self):
        self.agent.config = replace(
            self.agent.config,
            mode='code',
            verification_command='true',
        )
        decisions = iter((False, True))
        approvals = []
        def approve(*args):
            approvals.append(args)
            return next(decisions)
        self.agent.approval_handler = approve
        rejected = self.call('verify', {})
        self.assertEqual(rejected.failure.code, 'verification_denied')
        accepted = self.call('verify', {})
        self.assertEqual(accepted.status, 'success')
        self.assertEqual(len(approvals), 2)

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
        with patch('pico.verification_service.verify_workspace',
                   side_effect=ExecutionCancelled('stop')), \
                self.assertRaises(ExecutionCancelled):
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

    def test_cli_exposes_only_user_options_and_rejects_removed_flags(self):
        import contextlib
        import io

        from pico.cli import build_arg_parser
        parser = build_arg_parser()
        self.assertEqual(set(parser._option_string_actions),
                         {'-h','--help','--cwd','--resume','--mode','--model','--trace'})
        removed = ['--allow-write','--verify-command','--no-memory','--base-url',
                   '--temperature','--openai-timeout','--max-agent-turns',
                   '--max-parallel-tools','--max-new-tokens','--turn-timeout',
                   '--provider-context-limit','--compaction-reserve-tokens',
                   '--compaction-keep-recent-tokens','--summary-max-output-tokens',
                   '--allow-tool','--secret-env-name']
        for flag in removed:
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser.parse_args([flag])
                self.assertEqual(error.exception.code, 2)

    def test_cli_build_uses_runtime_defaults_and_environment(self):
        from pico.cli import build_agent, build_arg_parser
        env = {'PICO_OPENAI_API_BASE':'http://127.0.0.1:1/v1',
               'PICO_OPENAI_MODEL':'fixture-model', 'PICO_OPENAI_TEMPERATURE':'0.3',
               'PICO_OPENAI_TIMEOUT':'45', 'PICO_SECRET_ENV_NAMES':'CUSTOM_CREDENTIAL',
               'CUSTOM_CREDENTIAL':'fixture-secret-value'}
        with patch.dict('os.environ', env, clear=True), \
                patch('pico.cli.OpenAICompatibleModelClient', return_value=FakeModelClient([])) as client:
            agent = build_agent(build_arg_parser().parse_args(['--cwd',str(self.root)]))
            self.assertEqual(client.call_args.kwargs['temperature'], 0.3)
            self.assertEqual(client.call_args.kwargs['timeout'], 45)
            self.assertEqual(client.call_args.kwargs['model'], 'fixture-model')
            self.assertEqual(agent.config.max_agent_turns, PicoConfig().max_agent_turns)
            self.assertEqual(agent.config.allowed_write_paths, None)
            self.assertEqual(agent.redact_text('fixture-secret-value'), '<redacted>')
            self.assertIsNotNone(agent.approval_handler)

    def test_all_construction_paths_discover_project_verification(self):
        tests = self.root / 'tests'
        tests.mkdir()
        (tests / 'test_sample.py').write_text('def test_sample():\n    assert True\n')
        agent = Pico.create(
            FakeModelClient([]),
            Workspace.build(self.root),
            session_store=SessionStore(self.root / '.pico/discovery-sessions'),
            config=PicoConfig(mode='code'),
        )
        self.assertIn('-m pytest -q', agent.config.verification_command)

    def test_workspace_observation_keeps_file_level_facts_only(self):
        from dataclasses import fields
        self.assertEqual(
            [field.name for field in fields(WorkspaceObservation)],
            ['repository', 'head', 'status', 'status_lines', 'truncated'],
        )
        text = WorkspaceObservation(
            repository='git', head='branch main', status='dirty',
            status_lines=('UU conflict.py', ' M changed.py'), truncated=True,
        ).render(root='/project', logical_cwd='src')
        self.assertIn('startup_directory', text)
        self.assertIn('UU conflict.py', text)
        self.assertIn('Merge conflicts are present', text)
        self.assertIn('Change list truncated', text)
        self.assertNotIn('staged=', text)

    def test_workspace_observation_does_not_compute_diff_statistics(self):
        class Result:
            returncode = 0
            stop_reason = ''
            infrastructure_error = False

            def __init__(self, stdout):
                self.stdout = stdout

        def run(_runner, _execution, _root, args):
            if args[0] == 'symbolic-ref':
                return Result(b'main\n')
            if args[0] == 'rev-parse':
                return Result(b'0123456789abcdef\n')
            if args[0] == 'status':
                return Result(b' M changed.py\n')
            self.fail(f'unexpected workspace command: {args}')

        workspace = Workspace(self.root, self.root, 'git')
        with patch.object(Workspace, '_git', side_effect=run) as git:
            observed = workspace.observe(command_runner=object(),
                                         execution_context=self.execution)
        self.assertEqual(observed.status_lines, (' M changed.py',))
        self.assertEqual([call.args[3][0] for call in git.call_args_list],
                         ['symbolic-ref', 'rev-parse', 'status'])

    def test_final_diff_public_name_and_tool_runtime_export(self):
        import pico
        result = RunOutcome('session', 'completed', 'done',
                            final_diff={'changed_paths':['a.py']})
        self.assertEqual(result.changed_paths, ('a.py',))
        self.assertIn('final_diff', result.to_dict())
        self.assertEqual(result.to_dict()['changed_paths'], ['a.py'])
        self.assertNotIn('task_diff', result.to_dict())
        self.assertIs(pico.ToolRuntime, type(self.agent.tools))

    def test_agent_loop_returns_final_diff_artifact(self):
        (self.root/'a.py').write_text('old\n')
        self.agent.model_client.client.outputs = [
            ModelAction.tool('read_file', {'path':'a.py'}),
            ModelAction.tool('edit_file', {
                'path':'a.py', 'old_text':'old', 'new_text':'new',
            }),
            ModelAction.final('Updated a.py.'),
        ]
        result = self.agent.ask('Update a.py.')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.changed_paths, ('a.py',))
        artifact = result.final_diff['artifact_id']
        diff = self.agent.artifacts.read_internal(
            self.agent.session.id, artifact, expected_kind='final_workspace_diff'
        )[1]
        self.assertIn(b'-old', diff)
        self.assertIn(b'+new', diff)

    def test_restored_names_have_no_compatibility_aliases(self):
        from dataclasses import fields

        from pico import cli
        from pico.delegate import DelegateArgs
        from pico.providers.clients import OpenAICompatibleModelClient

        self.assertTrue(callable(cli._build_model_client))
        self.assertFalse(hasattr(cli, '_model_client'))
        self.assertTrue(callable(SessionStore.validate))
        self.assertFalse(hasattr(SessionStore, '_validate'))
        config_fields = {field.name for field in fields(PicoConfig)}
        self.assertNotIn('secret_env_names', config_fields)
        self.assertNotIn('memory_enabled', config_fields)
        self.assertNotIn('max_parallel_tools', config_fields)
        self.assertNotIn('denied', self.agent.session.loop_control)
        self.assertEqual(set(DelegateArgs.model_fields), {'task'})
        session_fields = {field.name for field in fields(type(self.agent.session))}
        self.assertNotIn('covered', session_fields)
        self.assertNotIn('history_archives', session_fields)
        self.assertNotIn('task_messages', session_fields)
        for client in (FakeModelClient([]), OpenAICompatibleModelClient(
                'stub', 'http://127.0.0.1:1/v1', '', None, 1)):
            for name in ('reset_action_session', 'record_action_results',
                         'projected_context_tokens'):
                self.assertFalse(hasattr(client, name))

    def test_latest_correction_and_transcript_survive_compaction(self):
        session = self.agent.session
        session.append_user('Only edit auth.py.')
        session.append_feedback('old observation ' * 100)
        session.append_user(
            'Correction: do not edit auth.py; edit session.py.',
        )
        session.observed = len(session.history)
        cut = len(session.history)
        session.save()
        transcript = session.store.transcript_path(session.id)
        before = transcript.read_bytes()
        messages = self.agent.context._input_text(
            summary='## Current Goal\nRepair session handling.',
            history_start=cut,
        )
        payload = json.dumps(messages, ensure_ascii=False)
        self.assertIn('Correction: do not edit auth.py; edit session.py.', payload)
        self.agent.context._commit_compaction(
            '## Current Goal\nRepair session handling.', cut
        )
        self.assertEqual(transcript.read_bytes(), before)
        restored = session.store.load(session.id, self.root)
        self.assertEqual(restored.history, session.history)
        self.assertEqual(restored.summary_end, cut)
        (self.root / 'after-archive.py').write_text('old')
        call = ToolCall('edit_file', {'path': 'after-archive.py'})
        index, entry = restored.begin_tool_turn([call])
        restored.start_tool(entry, call.call_id)
        restored.mutations.append(
            {
                'id': f'{index}:{call.call_id}',
                'tool': 'edit_file',
                'path': 'after-archive.py',
                'before_revision': content_revision(b'old'),
                'after_revision': content_revision(b'new'),
                'preimage_id': '',
                'status': 'prepared',
            }
        )
        restored.save()
        recovered = restored.store.load(restored.id, self.root)
        self.assertEqual(recovered.recover(), 1)
        result = recovered.history[index]['results'][call.call_id]
        self.assertEqual(result['side_effect_state'], 'none')

    def test_welcome_and_help_keep_original_interaction_without_config_noise(self):
        from pico.cli import HELP_DETAILS, build_welcome
        welcome = build_welcome(self.agent, 'fixture-model')
        for value in ('pico', 'local coding agent', 'WORKSPACE', 'fixture-model',
                      'MODE', 'SESSION'):
            self.assertIn(value, welcome)
        for value in ('VERIFY', 'MEMORY', 'ALLOW WRITE'):
            self.assertNotIn(value, welcome)
        for command in ('/help', '/state', '/session', '/reset', '/exit'):
            self.assertIn(command, HELP_DETAILS)

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

    def test_tool_group_runs_in_model_order(self):
        (self.root/'a.py').write_text('a')
        order = []
        original = self.agent.tools.registry['read_file']['run']
        def read(context, args):
            order.append(context.tool_call_id)
            return original(context, args)
        self.agent.tools.registry['read_file']['run'] = read
        calls = [ToolCall('read_file', {'path':'a.py'}) for _ in range(2)]
        _, entry = self.agent.session.begin_tool_turn(calls)
        results = self.agent.tools.execute_group(calls, entry, self.execution,
                                                self.agent.tools.resolve_surface())
        self.assertEqual(order, [call.call_id for call in calls])
        self.assertEqual([r.status for r in results], ['success','success'])
        self.assertEqual([r.tool_call_id for r in results], [c.call_id for c in calls])

    def test_read_and_write_emit_the_same_tool_lifecycle(self):
        (self.root/'a.py').write_text('old')
        events = []
        self.agent.trace = lambda kind, payload: events.append((kind, payload))
        self.call('read_file', {'path':'a.py'})
        self.call('edit_file', {'path':'a.py', 'old_text':'old', 'new_text':'new'})
        lifecycle = [
            (kind, payload['tool'])
            for kind, payload in events
            if kind in {'tool_started', 'tool_finished'}
        ]
        self.assertEqual(
            lifecycle,
            [
                ('tool_started', 'read_file'),
                ('tool_finished', 'read_file'),
                ('tool_started', 'edit_file'),
                ('tool_finished', 'edit_file'),
            ],
        )

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
        before = (
            session.summary,
            session.summary_end,
            list(session.history),
        )
        with (patch.object(self.agent.context.compactor, 'plan', return_value=('long ' * 10000, 1)),
              self.assertRaises(ContextBudgetExceeded)):
            self.agent.context.build(self.agent.tools.resolve_surface(), self.execution, force=True)
        self.assertEqual(
            (
                session.summary,
                session.summary_end,
                session.history,
            ),
            before,
        )

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
        self.assertIn('verification_available', policy)
        self.assertNotIn('verification_command', policy)

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
        self.assertEqual(self.agent.session.summary_end, cut)
        self.assertNotIn('OLD-RAW-MARKER', self.agent.session.path.read_text())
        self.assertIn('OLD-RAW-MARKER', self.agent.session.store.transcript_path(
            self.agent.session.id).read_text())

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
