import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from support import load_modules


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.activity, self.tool_module = load_modules(self.directory.name)
        self.settings = Mock()
        self.values = {'activity-terminal-type': 1, 'activity-terminal-username': 'ubuntu',
                       'activity-terminal-ip': '10.52.171.194'}
        self.settings.get_value.side_effect = lambda key: SimpleNamespace(unpack=lambda: self.values[key])
        self.settings.get_boolean.return_value = False
        self.activity.Gio.Settings.return_value = self.settings
        self.page = self.activity.Terminal('auto', lambda: self.command)
        self.command = 'whoami'

    def remote_command(self):
        argv = shlex.split(self.page.prepare_script()[0])
        self.assertEqual(argv[:-1], ['ssh', '-o', 'LogLevel=ERROR', '-o', 'StrictHostKeyChecking=no',
                                    '-t', '--', 'ubuntu@10.52.171.194'])
        remote_argv = shlex.split(argv[-1])
        self.assertEqual(remote_argv[:2], ['bash', '-c'])
        return remote_argv[2]

    def test_ssh_preserves_compound_and_multiline_commands(self):
        for command in ['uptime', 'clear;sudo whoami', 'whoami\nsudo whoami',
                        'printf "%s\\n" "it\'s quoted" "$HOME" `whoami`',
                        'cat <<\'EOF\'\nquotes \' " ; $HOME\nEOF',
                        'false || echo recovered; true && echo done']:
            with self.subTest(command=command):
                self.command = command
                self.assertEqual(self.remote_command(), command)

    def test_local_shell_does_not_execute_remote_suffix(self):
        marker = Path(self.directory.name) / 'local-marker'
        self.command = 'whoami; touch {}'.format(shlex.quote(str(marker)))
        wrapper_dir = Path(self.directory.name) / 'bin'
        wrapper_dir.mkdir()
        ssh = wrapper_dir / 'ssh'
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        ssh.chmod(0o755)
        result = subprocess.run(['bash', '-c', ';\n'.join(self.page.prepare_script())],
                                env={**os.environ, 'PATH': str(wrapper_dir) + ':' + os.environ['PATH']},
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertIn('touch', result.stdout)
        self.assertFalse(marker.exists())

    def test_destination_is_one_argument(self):
        self.values['activity-terminal-username'] = 'user;echo injected'
        self.values['activity-terminal-ip'] = 'host $(echo injected)'
        argv = shlex.split(self.page.prepare_script()[0])
        self.assertEqual(argv[-2], 'user;echo injected@host $(echo injected)')

    def test_direct_commands_keep_quotes(self):
        self.values['activity-terminal-type'] = 0
        self.command = 'printf "%s\\n" "two words"; printf "%s" "it\'s fine"'
        result = subprocess.run(['bash', '-c', ';\n'.join(self.page.prepare_script())],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stdout, "two words\nit's fine")

    def test_flatpak_passes_script_as_one_argument(self):
        self.values['activity-terminal-type'] = 0
        self.activity.IN_FLATPAK = True
        self.activity.commands['bash'] = ['flatpak-spawn --host env TERM=xterm-256color script -q -c {script} /dev/null']
        self.command = 'printf "%s" "it\'s fine"; echo "$HOME"'
        argv = shlex.split(self.page.prepare_script()[0])
        self.assertEqual(argv[-2], self.command)
        self.assertEqual(argv[-1], '/dev/null')

    def test_run_uses_prepared_script(self):
        pty = self.activity.Vte.Pty.new_sync.return_value
        self.page.set_pty = Mock()
        self.page.run()
        self.assertEqual(pty.spawn_async.call_args.args[1], ['bash', '-c', ';\n'.join(self.page.prepare_script())])

    def test_connection_snapshot_survives_settings_change(self):
        self.page.connection = {'type': 1, 'username': 'ubuntu', 'host': 'container'}
        self.values.update({'activity-terminal-type': 0, 'activity-terminal-ip': 'local'})
        argv = shlex.split(self.page.prepare_script()[0])
        self.assertEqual(argv[-2], 'ubuntu@container')

    def test_working_directory_is_quoted_and_contains_compound_command(self):
        directory = Path(self.directory.name) / "project's directory"
        directory.mkdir()
        self.page.connection = {'type': 0, 'working_directory': str(directory)}
        self.command = 'pwd; printf "done\\n"'
        result = subprocess.run(['bash', '-c', ';\n'.join(self.page.prepare_script())],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stdout, str(directory) + '\ndone\n')

    def test_child_exit_retains_real_status(self):
        self.page.close_callback = Mock()
        self.page.child_exited(self.page, 127 << 8)
        self.assertEqual(self.page.exit_code, 127)
        self.assertIsNone(self.page.child_pid)
        self.page.close_callback.assert_called_once()

    def test_async_spawn_failure_completes(self):
        pty = self.activity.Vte.Pty.new_sync.return_value
        self.page.set_pty = Mock()
        self.page.watch_child = Mock()
        self.page.feed = Mock()
        self.page.close_callback = Mock()
        pty.spawn_finish.side_effect = RuntimeError('spawn failed')
        self.page.run()
        pty.spawn_async.call_args.args[-2](pty, None, None)
        self.page.close_callback.assert_called_once()
        self.page.feed.assert_called_once_with(b'Error: spawn failed\r\n')

    def make_tool(self, automatic=True, output='ubuntu\nroot\n'):
        self.settings.get_boolean.return_value = automatic
        self.tool = self.tool_module.Terminal()
        self.bot_message = Mock()
        self.bot_message.get_ancestor.return_value = None
        self.terminal_page = Mock()
        self.terminal_page.get_text.return_value = output
        self.terminal_page.run.side_effect = lambda: self.terminal_page.close_callback()
        self.tool_module.activities.Terminal = Mock(return_value=self.terminal_page)
        self.tool_module.activities.show_activity = Mock()
        self.tool_module.dialog.Entry = Mock()

    def run_tool(self, command='whoami', explanation='Get the username'):
        return self.tool.run({'command': command, 'explanation': explanation}, [], self.bot_message)

    def test_automatic_execution_skips_confirmation_and_clear(self):
        self.make_tool()
        self.assertEqual(self.run_tool('whoami; sudo whoami'), '```\nubuntu\nroot\n```')
        self.tool_module.dialog.Entry.assert_not_called()
        self.assertEqual(self.tool_module.activities.Terminal.call_args.kwargs['code_getter'](), 'whoami; sudo whoami')
        self.terminal_page.reset.assert_called_once_with(True, True)

    def test_manual_confirmation_can_edit_command(self):
        self.make_tool(automatic=False)
        self.tool_module.dialog.Entry.side_effect = lambda **kwargs: SimpleNamespace(
            show=lambda root: kwargs['options']['Run']['callback']('sudo whoami'))
        self.run_tool()
        self.assertEqual(self.tool.current_command, 'sudo whoami')
        self.terminal_page.run.assert_called_once()

    def test_manual_cancel_does_not_execute(self):
        self.make_tool(automatic=False)
        self.tool_module.dialog.Entry.side_effect = lambda **kwargs: SimpleNamespace(
            show=lambda root: kwargs['options']['Cancel']['callback']('whoami'))
        self.assertEqual(self.run_tool(), 'The user chose not to execute the command')
        self.terminal_page.run.assert_not_called()

    def test_last_output_line_is_preserved(self):
        self.make_tool(output='ubuntu')
        self.assertEqual(self.run_tool(), '```\nubuntu\n```')

    def test_empty_output(self):
        self.make_tool(output='\n\n')
        self.assertEqual(self.run_tool(), 'The command did not return any text')

    def test_reused_page_gets_current_completion_callback(self):
        self.make_tool()
        self.run_tool()
        self.terminal_page.get_text.return_value = 'second\n'
        self.assertEqual(self.run_tool('echo second'), '```\nsecond\n```')
        self.assertEqual(self.tool_module.activities.Terminal.call_count, 1)
        self.assertEqual(self.terminal_page.reset.call_count, 2)

    def test_launch_error_returns(self):
        self.make_tool()
        self.terminal_page.run.side_effect = RuntimeError('spawn failed')
        self.assertEqual(self.run_tool(), 'Error: Could not execute the command: spawn failed')

    def test_confirmation_error_returns(self):
        self.make_tool(automatic=False)
        self.tool_module.dialog.Entry.side_effect = RuntimeError('dialog failed')
        self.assertEqual(self.run_tool(), 'Error: Could not confirm the command: dialog failed')

    def test_output_error_returns(self):
        self.make_tool()
        self.terminal_page.get_text.side_effect = RuntimeError('output failed')
        self.assertEqual(self.run_tool(), 'Error: Could not read the command output: output failed')

    def test_missing_command(self):
        self.make_tool()
        self.assertEqual(self.run_tool(''), 'Error: No command was provided')
        self.terminal_page.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
