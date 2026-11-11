"""State and native tool protocol checks without GTK or network dependencies."""

import importlib.util
from pathlib import Path
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('agent_under_test', Path(__file__).resolve().parents[1] / 'src/agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

PLAN = {'requirements': 'Check container identity', 'steps': ['Read identity'],
        'completion_checks': ['The user is ubuntu']}
COMMAND = {'command': 'whoami', 'explanation': 'Read identity', 'step': 1}


def call(name, arguments):
    return {'function': {'name': name, 'arguments': arguments}}


def client_for(*batches):
    client = Mock()
    client.chat.side_effect = [iter([SimpleNamespace(message=SimpleNamespace(content='', tool_calls=batch))])
                               for batch in batches]
    return client


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.changes = []
        self.controller = agent.AgentController('Check identity', 'agent-fast:latest', 'instance',
                                               {'type': 1, 'host': 'container'}, on_change=self.changes.append)
        self.execute = Mock(return_value={'exit_code': 0, 'output': 'ubuntu\n'})

    def plan(self, auto=False):
        self.controller.data['auto_start'] = auto
        return self.controller.handle('publish_plan', PLAN, self.execute)

    def runner(self, client, **kwargs):
        return agent.AgentRunner(self.controller, client, Mock(), self.execute, **kwargs)

    def test_clarify_answer_plan_review_execute(self):
        c = self.controller
        c.handle('request_clarification', {'questions': ['Which user?']}, self.execute)
        self.assertEqual(c.phase, 'waiting_for_answer')
        c.answer('ubuntu')
        self.assertEqual(c.data['messages'][-1]['content'], 'ubuntu')
        self.plan()
        self.assertEqual(c.phase, 'waiting_for_start')
        with self.assertRaises(ValueError):
            c.handle('run_command', COMMAND, self.execute)
        self.execute.assert_not_called()
        c.resume()
        c.handle('run_command', COMMAND, self.execute)
        c.handle('finish_task', {'summary': 'Identity verified', 'verification': ['whoami returned ubuntu']}, self.execute)
        self.assertEqual(c.phase, 'completed')

    def test_no_execution_before_plan(self):
        with self.assertRaises(ValueError):
            self.controller.handle('run_command', COMMAND, self.execute)
        self.execute.assert_not_called()

    def test_automatic_plan_runs_and_verifies(self):
        self.controller.data['auto_start'] = True
        client = client_for([call('publish_plan', PLAN)], [call('run_command', COMMAND)],
                            [call('finish_task', {'summary': 'Verified', 'verification': ['ubuntu']})])
        self.runner(client).run()
        self.assertEqual(self.controller.phase, 'completed')
        self.assertEqual(self.controller.data['results'][0]['exit_code'], 0)
        self.assertEqual(self.execute.call_count, 1)
        self.assertIn('ubuntu', client.chat.call_args.kwargs['messages'][-1]['content'])

    def test_missing_steps_cannot_finish(self):
        self.plan(True)
        with self.assertRaises(ValueError):
            self.controller.handle('finish_task', {'summary': 'Done', 'verification': ['yes']}, self.execute)

    def test_failed_command_cannot_be_reported_complete(self):
        self.plan(True)
        self.execute.return_value = {'exit_code': 1, 'output': 'failed'}
        self.controller.handle('run_command', COMMAND, self.execute)
        self.assertEqual(self.controller.data['steps'][0]['status'], 'failed')
        with self.assertRaises(ValueError):
            self.controller.handle('report_progress', {'step': 1, 'detail': 'Done'}, self.execute)
        self.execute.return_value = {'exit_code': 0, 'output': 'ubuntu'}
        self.controller.handle('run_command', COMMAND, self.execute)
        self.assertEqual(self.controller.data['steps'][0]['status'], 'completed')

    def test_validation(self):
        for name, args in [('request_clarification', {'questions': []}),
                           ('request_clarification', {'questions': ['a'] * 4}),
                           ('publish_plan', {**PLAN, 'requirements': ''}),
                           ('publish_plan', {**PLAN, 'steps': ['']}),
                           ('publish_plan', {**PLAN, 'completion_checks': []})]:
            with self.subTest(name=name, arguments=args), self.assertRaises(ValueError):
                self.controller.handle(name, args, self.execute)
        self.plan(True)
        for args in [{**COMMAND, 'step': True}, {**COMMAND, 'command': ''}, {**COMMAND, 'step': 2}]:
            with self.subTest(arguments=args), self.assertRaises(ValueError):
                self.controller.handle('run_command', args, self.execute)

    def test_target_and_snapshots_are_independent(self):
        target = {'host': 'container', 'type': 1}
        c = agent.AgentController('goal', 'model', 'instance', target)
        target['host'] = 'local'
        snapshot = c.snapshot()
        snapshot['target']['host'] = 'other'
        self.assertEqual(c.data['target']['host'], 'container')

    def test_edit_requires_new_plan(self):
        self.plan(True)
        self.controller.handle('run_command', COMMAND, self.execute)
        self.controller.suspend(editing=True)
        self.controller.answer('Use a different check')
        self.assertEqual(self.controller.phase, 'clarifying')
        self.assertEqual(self.controller.data['steps'], [])
        self.assertEqual(self.controller.data['results'], [])

    def test_resume_questions_waits_for_answer(self):
        self.controller.handle('request_clarification', {'questions': ['Which user?']}, self.execute)
        with self.assertRaises(ValueError):
            self.controller.resume()
        restored = agent.AgentController.restore(self.controller.snapshot())
        self.assertEqual(restored.phase, 'paused')
        restored.resume()
        self.assertEqual(restored.phase, 'waiting_for_answer')

    def test_restore_running_task_pauses_and_repairs_protocol(self):
        self.plan(True)
        self.controller.data['messages'].append({'role': 'assistant', 'tool_calls': [call('run_command', COMMAND)]})
        restored = agent.AgentController.restore(self.controller.snapshot())
        self.assertEqual(restored.phase, 'paused')
        self.assertEqual(restored.data['messages'][-1]['role'], 'tool')
        self.assertIn('interrupted', restored.data['messages'][-1]['content'])
        restored.resume()
        self.assertEqual(restored.phase, 'executing')

    def test_already_paused_questions_survive_save_and_restore(self):
        self.controller.handle('request_clarification', {'questions': ['Which project?']}, self.execute)
        self.controller.suspend()
        restored = agent.AgentController.restore(self.controller.snapshot())
        restored.resume()
        self.assertEqual(restored.phase, 'waiting_for_answer')
        self.assertEqual(restored.data['questions'], ['Which project?'])

    def test_limit_applies_inside_batch(self):
        self.plan(True)
        self.controller.data.update(max_calls=2, tool_calls=0)
        client = client_for([call('run_command', COMMAND)] * 4)
        self.runner(client).run()
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(self.controller.phase, 'paused')
        tools = [entry for entry in self.controller.data['messages'] if entry['role'] == 'tool']
        self.assertEqual(len(tools), 4)

    def test_repeated_invalid_tools_pause_without_execution(self):
        client = client_for([call('run_command', COMMAND)] * 5)
        self.runner(client).run()
        self.execute.assert_not_called()
        self.assertEqual(self.controller.phase, 'paused')
        self.assertEqual(self.controller.data['tool_calls'], 3)

    def test_questions_suspend_remaining_calls(self):
        self.plan(True)
        client = client_for([call('request_clarification', {'questions': ['Continue?']}), call('run_command', COMMAND)])
        self.runner(client).run()
        self.execute.assert_not_called()
        self.assertEqual(self.controller.phase, 'waiting_for_answer')
        tools = [entry for entry in self.controller.data['messages'] if entry['role'] == 'tool']
        self.assertEqual(len(tools), 2)

    def test_stop_interrupts_command_and_preserves_cancel(self):
        self.plan(True)
        entered = threading.Event()
        def execute(args, cancel):
            entered.set()
            self.assertTrue(cancel.wait(2))
            return {'exit_code': 0, 'output': 'late result'}
        self.execute.side_effect = execute
        worker = threading.Thread(target=self.runner(client_for([call('run_command', COMMAND)])).run)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.controller.suspend('canceled')
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller.phase, 'canceled')
        self.assertEqual(self.controller.data['results'], [])

    def test_deadline_interrupts_command(self):
        self.plan(True)
        self.controller.data['max_seconds'] = 0.05
        def execute(args, cancel):
            self.assertTrue(cancel.wait(2))
            return {'exit_code': -1, 'output': ''}
        self.execute.side_effect = execute
        self.runner(client_for([call('run_command', COMMAND)])).run()
        self.assertEqual(self.controller.phase, 'paused')
        self.assertIn('run limit', self.controller.data['summary'])

    def test_network_error_preserves_paused_or_fails_active(self):
        client = Mock()
        client.chat.side_effect = RuntimeError('Offline')
        self.runner(client).run()
        self.assertEqual(self.controller.phase, 'failed')
        self.assertEqual(self.controller.data['summary'], 'Offline')


if __name__ == '__main__':
    unittest.main()
