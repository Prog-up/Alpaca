"""Real GTK/VTE agent checks, with an optional live Ollama/SSH workflow."""

import os
from pathlib import Path
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    endpoint = os.environ.get('ALPACA_AGENT_TEST_OLLAMA')
    with tempfile.TemporaryDirectory(prefix='alpaca-agent-test-') as directory:
        os.environ['GSETTINGS_SCHEMA_DIR'] = str(root / 'build/data')
        os.environ['GSETTINGS_BACKEND'] = 'memory'
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            os.environ[key] = str(Path(directory) / key)
        import gettext
        import sys
        import threading
        import time
        from contextlib import ExitStack
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        gettext.install('alpaca')
        sys.path.insert(0, str(root / 'build/install/share/Alpaca'))
        from gi.repository import Gio
        resource = Gio.Resource.load(str(root / 'build/src/alpaca.gresource'))
        resource._register()
        from alpaca import main as application
        from gi.repository import Adw, GLib, Gtk
        from alpaca.sql_manager import Instance as SQL
        from alpaca.widgets import agent, activities, chat, dialog, message, preferences
        from alpaca.widgets.instances.ollama_instances import BaseInstance
        from alpaca.window import AlpacaWindow
        from alpaca.quick_ask import QuickAskWindow
        Adw.init()
        settings = Gio.Settings(schema_id='com.jeffser.Alpaca')
        model = os.environ.get('ALPACA_AGENT_TEST_MODEL', 'agent-fast:latest')
        settings.set_string('selected-instance', 'agent-test')
        settings.set_boolean('agent-mode', True)
        settings.set_boolean('queue-prompts', True)
        settings.set_boolean('activity-terminal-auto-execute', bool(endpoint))
        settings.set_boolean('agent-auto-start', bool(endpoint))
        settings.set_int('agent-max-seconds', 180)
        settings.set_int('activity-terminal-type', 0)
        if endpoint:
            target = os.environ['ALPACA_SSH_TEST_TARGET']
            user, host = target.rsplit('@', 1)
            settings.set_int('activity-terminal-type', 1)
            settings.set_string('activity-terminal-username', user)
            settings.set_string('activity-terminal-ip', host)
        SQL.insert_or_update_instance('agent-test', False, 'ollama',
                                      {'name': 'Agent Test', 'url': endpoint or 'http://unused',
                                       'default_model': model, 'num_ctx': 16384, 'temperature': 0})
        app = Adw.Application(application_id='com.jeffser.Alpaca.AgentTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.args = SimpleNamespace(new_chat=None, ask=None)
        def create_action(name, callback, shortcuts=None):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            app.add_action(action)
        app.create_action = create_action
        app.register(None)
        context = GLib.MainContext.default()
        def pump_until(predicate, timeout=20):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                if predicate():
                    return
                time.sleep(0.005)
            for current in list(chat.loaded_chats):
                if current.agent_session:
                    print('Timeout state:', current.agent_session.controller.snapshot(), flush=True)
            raise AssertionError('Timed out waiting for GTK: ' + str(predicate))
        def finished(current):
            return current.active_message is None and not current.busy
        def send(window, text):
            window.global_footer.get_buffer().set_text(text)
            window.send_message()
        def tool(name, **arguments):
            return {'function': {'name': name, 'arguments': arguments}}

        with ExitStack() as stack:
            stack.enter_context(patch.object(BaseInstance, 'get_local_models', return_value=[{'name': model, 'details': {}, 'capabilities': ['tools']}]))
            stack.enter_context(patch.object(BaseInstance, 'get_model_info', return_value={'capabilities': ['tools']}))
            stack.enter_context(patch.object(BaseInstance, 'get_available_models', return_value={}))
            stack.enter_context(patch.object(message.dialog, 'show_notification', Mock()))
            window = AlpacaWindow(application=app)
            app.get_main_window = lambda: window
            window.present()
            pump_until(lambda: window.instance_listbox.get_sensitive() and window.get_selected_model().get_name() == model)
            terminal_window = Gtk.Window(default_width=800, default_height=500)
            terminal_window.present()
            # Keep the real VTE page visible without obscuring the chat controls under test.
            stack.enter_context(patch.object(activities, 'show_activity', side_effect=lambda page, *args: terminal_window.set_child(page)))
            prefs = preferences.PreferencesDialog()
            assert prefs.agent_mode.get_active()
            prefs.agent_auto_start.set_active(False)
            assert not settings.get_boolean('agent-auto-start')
            prefs.agent_auto_start.set_active(bool(endpoint))
            prefs.agent_max_calls.set_value(20)
            assert settings.get_int('agent-max-calls') == 20
            prefs.force_close()

            if endpoint:
                live_goal = ('Verify the configured SSH container identity. Before making a plan, ask me '
                             'which username and Ubuntu version I expect. Run only read-only identity checks '
                             '(whoami, sudo -n whoami, cat /etc/os-release) and verify all three results.')
                send(window, live_goal)
                current = window.get_current_chat()
                heartbeat = []
                GLib.timeout_add(10, lambda: heartbeat.append(True) and False)
                pump_until(lambda: heartbeat)
                pump_until(lambda: finished(current), 180)
                state = current.agent_session.controller.snapshot()
                print('Live clarification:', state['phase'], state['questions'], flush=True)
                assert state['phase'] == 'waiting_for_answer', state
                task_id = state['id']
                send(window, 'Expected user: ubuntu. sudo -n whoami must return root. Ubuntu VERSION_ID must be 26.04. Verify all three and report the evidence. You may run those read-only commands automatically.')
                pump_until(lambda: finished(current), 180)
                state = current.agent_session.controller.snapshot()
                print('Live final phase:', state['phase'], flush=True)
                print('Plan:', state['steps'], flush=True)
                print('Results:', state['results'], flush=True)
                print('Summary:', state['summary'], state['verification'], flush=True)
                assert state['id'] == task_id
                assert state['phase'] == 'completed', state
                outputs = '\n'.join(item['output'] for item in state['results'])
                assert 'ubuntu' in outputs and 'root' in outputs and 'VERSION_ID="26.04"' in outputs, outputs
                assert all(item['exit_code'] == 0 for item in state['results'])
                print('Agent Fast → clarification → plan → real SSH/VTE execution → verified completion: PASS', flush=True)
            else:
                marker = Path(directory) / 'executed'
                held = threading.Event()
                release = threading.Event()
                batches = iter([
                    [tool('request_clarification', questions=['What output do you expect?'])],
                    [tool('publish_plan', requirements='Print agent-ok', steps=['Print and verify output'], completion_checks=['Output is agent-ok'])],
                    [tool('run_command', command='printf "agent-ok\\n"; touch ' + str(marker), explanation='Print and record execution', step=1)],
                    [tool('finish_task', summary='Verified agent-ok', verification=['The terminal returned agent-ok with status 0'])],
                    [tool('request_clarification', questions=['What should the second task do?'])],
                ])
                first = True
                def stream(**kwargs):
                    nonlocal first
                    assert threading.current_thread() is not threading.main_thread()
                    if first:
                        first = False
                        held.set()
                        assert release.wait(10)
                    yield SimpleNamespace(message=SimpleNamespace(content='', tool_calls=next(batches)))
                def fake_client(**kwargs):
                    client = Mock()
                    client.show.return_value = {'capabilities': ['tools']}
                    client.chat.side_effect = stream
                    return client
                stack.enter_context(patch.object(agent.ollama, 'Client', side_effect=fake_client))
                send(window, 'Print a test result')
                current = window.get_current_chat()
                pump_until(held.is_set)
                send(window, 'Second task')
                assert len(current.pending_prompts) == 1
                release.set()
                pump_until(lambda: finished(current))
                session = current.agent_session
                task_id = session.controller.data['id']
                assert session.controller.phase == 'waiting_for_answer'
                assert len(current.pending_prompts) == 1
                assert window.global_footer.prompt_queue_label.get_text() == 'Second task'
                window.global_footer.edit_queued_prompt()
                assert not current.pending_prompts
                assert window.global_footer.get_buffer().get_text(window.global_footer.get_buffer().get_start_iter(),
                                                                  window.global_footer.get_buffer().get_end_iter(), False) == 'Second task'
                send(window, 'Edited second task')
                assert current.pending_prompts[0]['text'] == 'Edited second task'
                assert session.controller.phase == 'waiting_for_answer'
                send(window, 'The expected output is agent-ok')
                pump_until(lambda: finished(current))
                assert session.controller.phase == 'waiting_for_start'
                assert session.controller.data['id'] == task_id
                assert not marker.exists()
                assert window.global_footer.agent_buttons['resume'].get_visible()
                window.global_footer.agent_action('resume')
                pump_until(lambda: isinstance(window.get_visible_dialog(), dialog.Entry))
                assert not marker.exists(), 'Command ran before terminal confirmation'
                window.get_visible_dialog().emit('response', 'Run')
                pump_until(lambda: current.agent_session is not session and finished(current))
                assert marker.exists()
                assert session.controller.phase == 'completed'
                assert session.controller.data['results'][0]['exit_code'] == 0
                assert current.agent_session.controller.data['goal'] == 'Edited second task'
                assert current.agent_session.controller.phase == 'waiting_for_answer'
                assert not current.pending_prompts
                assert SQL.get_agent_task(current.chat_id)['goal'] == 'Edited second task'
                print('Real GTK questions, plan review, terminal confirmation, exit status, and agent FIFO: PASS', flush=True)

                # Actual Quick Ask transfer persists a paused task and its outstanding questions.
                first = False
                batches = iter([[tool('request_clarification', questions=['Which project?'])]])
                quick = QuickAskWindow(application=app)
                quick.present()
                send(quick, 'Prepare a project')
                pump_until(lambda: finished(quick.chat))
                quick_id = quick.chat.agent_session.controller.data['id']
                quick.save_chat(None)
                pump_until(lambda: window.get_current_chat().agent_session is not None and
                           window.get_current_chat().agent_session.controller.data['id'] == quick_id)
                saved = window.get_current_chat()
                assert saved.agent_session.controller.phase == 'paused'
                saved.agent_session.resume(window)
                assert saved.agent_session.controller.phase == 'waiting_for_answer'
                assert SQL.get_agent_task(saved.chat_id)['questions'] == ['Which project?']
                print('Quick Ask save and restart recovery retain a paused task and questions: PASS', flush=True)

                # Cancel a real running VTE subprocess and verify its delayed action never runs.
                settings.set_boolean('agent-auto-start', True)
                settings.set_boolean('activity-terminal-auto-execute', True)
                window.get_chat_list_page().new_chat('Cancellation')
                cancel_marker = Path(directory) / 'should-not-exist'
                batches = iter([
                    [tool('publish_plan', requirements='Cancellation check', steps=['Wait'], completion_checks=['Wait finishes'])],
                    [tool('run_command', command='sleep 2; touch ' + str(cancel_marker), explanation='Wait for cancellation', step=1)],
                ])
                send(window, 'Test cancellation')
                canceled = window.get_current_chat()
                pump_until(lambda: canceled.agent_session.terminal is not None and
                           canceled.agent_session.terminal.global_page is not None and
                           canceled.agent_session.terminal.global_page.child_pid is not None)
                page = canceled.agent_session.terminal.global_page
                canceled.stop_message()
                pump_until(lambda: finished(canceled) and page.child_pid is None)
                deadline = time.monotonic() + 2.2
                pump_until(lambda: time.monotonic() >= deadline, 3)
                assert not cancel_marker.exists()
                assert canceled.agent_session.controller.phase == 'canceled'
                print('Stop terminates the real VTE process and prevents its delayed command: PASS', flush=True)

                window.get_chat_list_page().new_chat('Close Terminal')
                batches = iter([
                    [tool('publish_plan', requirements='Activity close check', steps=['Wait'], completion_checks=['Wait finishes'])],
                    [tool('run_command', command='sleep 2; touch ' + str(cancel_marker), explanation='Wait for activity close', step=1)],
                ])
                send(window, 'Test closing the active terminal')
                closed = window.get_current_chat()
                pump_until(lambda: closed.agent_session.terminal is not None and
                           closed.agent_session.terminal.global_page is not None and
                           closed.agent_session.terminal.global_page.child_pid is not None)
                page = closed.agent_session.terminal.global_page
                page.on_close()
                pump_until(lambda: finished(closed) and page.child_pid is None)
                assert closed.agent_session.controller.phase == 'paused'
                deadline = time.monotonic() + 2.2
                pump_until(lambda: time.monotonic() >= deadline, 3)
                assert not cancel_marker.exists()
                print('Closing the active terminal pauses the task without retrying the canceled command: PASS', flush=True)

            for current in list(chat.loaded_chats):
                current.stop_message()
            terminal_window.destroy()
            window.destroy()


if __name__ == '__main__':
    main()
