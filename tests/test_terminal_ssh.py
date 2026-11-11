"""Opt-in tests using a real VTE terminal and an existing SSH test machine."""

import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from support import ROOT, load_modules


@unittest.skipUnless(os.environ.get('ALPACA_SSH_TEST_TARGET'), 'Set ALPACA_SSH_TEST_TARGET=user@host for live SSH tests')
class SSHIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        for name, version in [('Gtk', '4.0'), ('Gdk', '4.0'), ('Adw', '1'),
                              ('GtkSource', '5'), ('Vte', '3.91')]:
            gi.require_version(name, version)
        from gi.repository import Gio, GLib, Gtk
        cls.GLib, cls.Gtk = GLib, Gtk
        if not Gtk.init_check():
            raise RuntimeError('A graphical display is required for the VTE integration tests')
        build_dir = Path(os.environ.get('ALPACA_TEST_BUILD_DIR', ROOT / 'build'))
        cls.resource = Gio.Resource.load(str(build_dir / 'src/alpaca.gresource'))
        cls.resource._register()
        cls.directory = tempfile.TemporaryDirectory()
        cls.activity, cls.tools = load_modules(cls.directory.name, real_gi=True)
        cls.settings = Gio.Settings(schema_id='com.jeffser.Alpaca')
        user, host = os.environ['ALPACA_SSH_TEST_TARGET'].rsplit('@', 1)
        cls.settings.set_int('activity-terminal-type', 1)
        cls.settings.set_string('activity-terminal-username', user)
        cls.settings.set_string('activity-terminal-ip', host)
        cls.settings.set_boolean('activity-terminal-auto-execute', True)
        # Keep test authentication noninteractive and known hosts outside the user's home.
        cls.original_path = os.environ['PATH']
        ssh = shutil.which('ssh')
        wrapper_dir = Path(cls.directory.name) / 'bin'
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / 'ssh'
        import shlex
        wrapper.write_text('#!/bin/sh\nexec {} -F /dev/null -o BatchMode=yes -o ConnectTimeout=8 -o UserKnownHostsFile={} "$@"\n'.format(
            shlex.quote(ssh), shlex.quote(str(Path(cls.directory.name) / 'known_hosts'))))
        wrapper.chmod(0o755)
        os.environ['PATH'] = str(wrapper_dir) + ':' + cls.original_path

    @classmethod
    def tearDownClass(cls):
        os.environ['PATH'] = cls.original_path
        cls.directory.cleanup()
        cls.resource._unregister()

    def setUp(self):
        self.window = self.Gtk.Window(default_width=1200, default_height=800)
        self.window.present()
        self.addCleanup(self.window.destroy)
        self.tools.activities.show_activity = lambda page, root, *args: root.set_child(page)
        self.tools.dialog.Entry = Mock(side_effect=AssertionError('Automatic execution opened a confirmation dialog'))
        self.bot = Mock()
        self.bot.get_root.return_value = self.window
        self.bot.get_ancestor.return_value = None
        self.tool = self.tools.Terminal()

    def run_command(self, command, timeout=60):
        result = []
        errors = []

        def run():
            try:
                result.append(self.tool.run({'command': command, 'explanation': 'SSH integration test'}, [], self.bot))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + timeout
        context = self.GLib.MainContext.default()
        while worker.is_alive() and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.005)
        self.assertFalse(worker.is_alive(), 'Terminal command timed out: ' + command)
        if errors:
            raise errors[0]
        self.tools.dialog.Entry.assert_not_called()
        print('\nSSH command: {}\n{}'.format(command, result[0]), flush=True)
        return result[0]

    def test_identity_sudo_and_os(self):
        result = self.run_command('whoami; sudo -n whoami; cat /etc/os-release')
        self.assertIn('ubuntu\nroot\n', result)
        self.assertIn('VERSION_ID="26.04"', result)

    def test_single_command_and_page_reuse(self):
        result = self.run_command('uptime')
        self.assertIn('load average:', result)
        result = self.run_command('sudo -n whoami')
        self.assertIn('\nroot\n', result)
        self.assertNotIn('load average:', result)

    def test_multiline_quotes_and_heredoc(self):
        result = self.run_command('whoami\nsudo -n whoami\ncat <<\'EOF\'\nit\'s quoted: "hello"; $HOME\nEOF')
        self.assertIn('ubuntu\nroot\n', result)
        self.assertIn('it\'s quoted: "hello"; $HOME', result)

    def test_sudo_apt_update(self):
        result = self.run_command('sudo apt-get update; printf "APT_UPDATE_STATUS=%s\\n" "$?"', timeout=120)
        self.assertIn('APT_UPDATE_STATUS=0', result)

    def test_failure_has_real_exit_status(self):
        self.run_command('printf "FAILURE_CHECK\\n"; exit 7')
        self.assertEqual(self.tool.last_exit_code, 7)


if __name__ == '__main__':
    unittest.main()
