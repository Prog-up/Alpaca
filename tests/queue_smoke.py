"""Opt-in GTK/Ollama queue smoke test; see tests/README.md."""

import os
from pathlib import Path
import tempfile

def main():
    root = Path(__file__).resolve().parents[1]
    endpoint = os.environ.get('ALPACA_QUEUE_TEST_OLLAMA')
    if not endpoint:
        raise SystemExit('Set ALPACA_QUEUE_TEST_OLLAMA to opt into real generation tests')
    with tempfile.TemporaryDirectory(prefix='alpaca-queue-test-') as directory:
        os.environ['GSETTINGS_SCHEMA_DIR'] = str(root / 'build/data')
        os.environ['GSETTINGS_BACKEND'] = 'memory'
        for key in ['XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME']:
            os.environ[key] = str(Path(directory) / key)
        import gettext
        import sys
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock

        gettext.install('alpaca')
        sys.path.insert(0, str(root / 'build/install/share/Alpaca'))
        from gi.repository import Gio
        resource = Gio.Resource.load(str(root / 'build/src/alpaca.gresource'))
        resource._register()
        from alpaca import main
        from gi.repository import Adw, Gdk, GLib, Gtk
        Adw.init()
        from alpaca.widgets import chat, message, preferences, prompt_queue
        from alpaca.sql_manager import Instance as SQL
        from alpaca.widgets.instances.ollama_instances import Ollama

        settings = Gio.Settings(schema_id='com.jeffser.Alpaca')
        settings.set_boolean('queue-prompts', True)
        preferences_dialog = preferences.PreferencesDialog()
        assert preferences_dialog.queue_prompts_switch.get_active()
        preferences_dialog.queue_prompts_switch.set_active(False)
        assert not settings.get_boolean('queue-prompts')
        settings.set_boolean('queue-prompts', True)

        window = Gtk.Window(default_width=800, default_height=600, name='QueueTest')
        window.settings = settings
        window.global_footer = message.GlobalFooter()
        current_chat = chat.Chat(chat_id='queue-smoke', name='Queue Test')
        SQL.insert_or_update_chat(current_chat)
        window.get_current_chat = lambda: current_chat
        model_name = os.environ.get('ALPACA_QUEUE_TEST_MODEL', 'qwen3.5:2b')
        window.get_selected_model = lambda: SimpleNamespace(get_name=lambda: model_name)
        instance = Ollama('queue-smoke', {'url': endpoint,
                                         'override_parameters': True, 'temperature': 0, 'num_ctx': 4096,
                                         'keep_alive': 1, 'think': False})
        window.get_current_instance = lambda: instance
        window.send_message = lambda *args: prompt_queue.send_message(window, current_chat, *args)
        container = Gtk.Box(orientation=1)
        container.append(current_chat)
        container.append(window.global_footer)
        window.set_child(container)
        window.present()
        window.global_footer.send_callback = window.send_message
        window.global_footer.action_stack.set_sensitive(True)
        message.dialog.show_notification = Mock()
        errors = []
        message.dialog.simple_error = lambda **kwargs: errors.append(kwargs)

        def pump_until(predicate, timeout=120):
            context = GLib.MainContext.default()
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                time.sleep(0.005)
            assert predicate(), 'Timed out waiting for queue'
            assert not errors, errors

        buffer = window.global_footer.get_buffer()
        buffer.set_text('Respond with exactly FIRST.')
        window.send_message()
        assert current_chat.busy
        buffer.set_text('Respond with exactly SECOND.')
        window.global_footer.action_stack.queue_button.emit('clicked')
        buffer.set_text('Respond with exactly THIRD.')
        window.global_footer.message_text_view.enter_key_handler(None, Gdk.KEY_Return, 0, Gdk.ModifierType(0))
        assert len(current_chat.pending_prompts) == 1
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == 'Respond with exactly THIRD.'
        assert window.global_footer.prompt_queue_label.get_text() == 'Respond with exactly SECOND.'
        assert not window.global_footer.action_stack.queue_button.get_sensitive()
        assert window.global_footer.prompt_queue_box.get_visible()
        assert window.global_footer.action_stack.queue_button.get_visible()
        window.global_footer.edit_queued_prompt()
        assert not current_chat.pending_prompts
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == 'Respond with exactly SECOND.'
        assert window.global_footer.action_stack.queue_button.get_sensitive()
        buffer.set_text('Respond with exactly EDITEDSECOND.')
        window.send_message()
        assert len(current_chat.pending_prompts) == 1
        assert window.global_footer.prompt_queue_label.get_text() == 'Respond with exactly EDITEDSECOND.'
        buffer.set_text('Unsent draft remains here')
        pump_until(lambda: not current_chat.busy and current_chat.active_message is None)
        assert not current_chat.pending_prompts
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == 'Unsent draft remains here'
        messages = list(current_chat.container)
        assert len(messages) == 4, len(messages)
        for item in messages:
            print(item.mode, item.get_content(), flush=True)
        assert [item.mode for item in messages] == [0, 1, 0, 1]
        assert 'FIRST' in messages[1].get_content()
        assert 'EDITEDSECOND' in messages[3].get_content()
        assert not window.global_footer.prompt_queue_box.get_visible()
        assert [row[4] for row in SQL.get_messages(current_chat) if row[1] == 'user'] == [
            'Respond with exactly FIRST.', 'Respond with exactly EDITEDSECOND.']
        print('Real GTK + Ollama one-message queue, full preview, edit/requeue, preference binding, and draft preservation: PASS', flush=True)

        # Check cancellation without launching another request.
        current_chat.busy = True
        current_chat.active_message = Mock()
        current_chat.pending_prompts.append({'text': 'cancel me'})
        current_chat.stop_message()
        assert not current_chat.pending_prompts
        current_chat.finish_generation(current_chat.active_message)
        assert not current_chat.busy
        print('Stop clears queued prompts: PASS', flush=True)

        # Exercise the actual Quick Ask window with the same live instance.
        from alpaca.quick_ask import QuickAskWindow
        from alpaca.widgets.models import basic, text
        app = Adw.Application(application_id='com.jeffser.Alpaca.QuickAskTest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.args = SimpleNamespace(ask=None)
        app.get_main_window = lambda: window
        app.register(None)
        model = basic.BasicModelButton(model_name=model_name, instance=instance, data={'capabilities': [], 'details': {}})
        text.append_to_model_selector(model.row)
        quick = QuickAskWindow(application=app)
        quick.present()
        quick.global_footer.get_buffer().set_text('Respond with exactly QUICKOK.')
        quick.send_message()
        heartbeat = []
        GLib.timeout_add(10, lambda: heartbeat.append(True) and False)
        pump_until(lambda: heartbeat)
        pump_until(lambda: not quick.chat.busy and quick.chat.active_message is None)
        answer = list(quick.chat.container)[-1].get_content()
        assert 'QUICKOK' in answer, answer
        quick.close()
        print('Real Quick Ask window + live Ollama response with GTK heartbeat: PASS', flush=True)
        window.destroy()

if __name__ == '__main__':
    main()
