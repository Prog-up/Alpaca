"""GTK regression checks for startup, preferences, and stream failure handling."""

import os
from pathlib import Path
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='alpaca-ui-test-') as directory:
        os.environ['GSETTINGS_SCHEMA_DIR'] = str(root / 'build/data')
        os.environ['GSETTINGS_BACKEND'] = 'memory'
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            os.environ[key] = str(Path(directory) / key)
        import gettext
        import base64
        from datetime import datetime
        from io import BytesIO
        import sys
        import threading
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        gettext.install('alpaca')
        sys.path.insert(0, str(root / 'build/install/share/Alpaca'))
        from gi.repository import Gio
        resource = Gio.Resource.load(str(root / 'build/src/alpaca.gresource'))
        resource._register()
        from alpaca import main as application
        from gi.repository import Adw, GLib
        from alpaca.sql_manager import Instance as SQL
        from alpaca.widgets import attachments, blocks, dialog, message, preferences
        from alpaca.quick_ask import QuickAskWindow
        from PIL import Image
        from alpaca.widgets.instances.ollama_instances import BaseInstance
        from alpaca.widgets.models.manager import ModelManager
        from alpaca.window import AlpacaWindow
        Adw.init()

        settings = Gio.Settings(schema_id='com.jeffser.Alpaca')
        settings.set_string('selected-instance', 'ui-test')
        settings.set_boolean('queue-prompts', True)
        SQL.insert_or_update_instance('ui-test', False, 'ollama', {'name': 'UI Test', 'url': 'http://unused', 'default_model': 'test-model'})
        app = Adw.Application(application_id='com.jeffser.Alpaca.UITest', flags=Gio.ApplicationFlags.NON_UNIQUE)
        app.args = SimpleNamespace(new_chat=None, ask=None)

        def create_action(name, callback, shortcuts=None):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            app.add_action(action)
        app.create_action = create_action
        app.register(None)

        entered = threading.Event()
        release = threading.Event()
        info_entered = threading.Event()
        info_release = threading.Event()
        rendered = []

        def load_models(instance):
            assert threading.current_thread() is not threading.main_thread()
            entered.set()
            assert release.wait(10), 'Model request was not released'
            return [{'name': 'other-model', 'details': {}, 'capabilities': []},
                    {'name': 'test-model', 'details': {}}]

        def load_info(instance, model):
            assert threading.current_thread() is not threading.main_thread(), 'Model request blocks GTK'
            info_entered.set()
            assert info_release.wait(10), 'Model metadata request was not released'
            return {'capabilities': ['tools'], 'system': 'Test system prompt'}

        original_create = ModelManager.create_text_model

        def create_model(manager, *args, **kwargs):
            assert threading.current_thread() is threading.main_thread(), 'Model widgets created on worker'
            rendered.append(kwargs['model_name'])
            return original_create(manager, *args, **kwargs)

        context = GLib.MainContext.default()

        def pump_until(predicate, timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                if predicate():
                    return
                time.sleep(0.005)
            assert predicate(), 'Timed out waiting for GTK'

        with patch.object(BaseInstance, 'get_local_models', load_models), \
                patch.object(BaseInstance, 'get_model_info', load_info), \
                patch.object(BaseInstance, 'get_available_models', return_value={}), \
                patch.object(ModelManager, 'create_text_model', create_model):
            window = AlpacaWindow(application=app)
            app.get_main_window = lambda: window
            window.present()
            pump_until(entered.is_set)
            # Preferences must remain usable while a server request is blocked.
            app.lookup_action('preferences').activate(None)
            pump_until(lambda: isinstance(window.get_visible_dialog(), preferences.PreferencesDialog))
            prefs = window.get_visible_dialog()
            prefs.queue_prompts_switch.set_active(False)
            assert not settings.get_boolean('queue-prompts')
            prefs.activity_terminal_auto_execute.set_active(True)
            assert settings.get_boolean('activity-terminal-auto-execute')
            prefs.force_close()
            release.set()
            pump_until(lambda: rendered and window.instance_listbox.get_sensitive())
            assert rendered == ['other-model', 'test-model']
            pump_until(info_entered.is_set)
            loading_quick = QuickAskWindow(application=app)
            loading_quick.present()
            assert window.get_selected_model().get_name() == 'test-model'
            assert loading_quick.get_selected_model().get_name() == 'test-model'
            assert not info_release.is_set()
            info_release.set()
            pump_until(lambda: 'system' in window.get_selected_model().data)
            assert window.get_selected_model().data['system'] == 'Test system prompt'
            pump_until(loading_quick.global_footer.tool_selector.get_sensitive)
            loading_quick.close()
            app.create_quick_ask = lambda: QuickAskWindow(application=app)
            app.args.ask = 'Service prompt'
            sent = []
            with patch.object(QuickAskWindow, 'write_and_send_message', lambda quick, text: sent.append((quick, text))):
                started = time.monotonic()
                application.AlpacaService(app).Ask('Service prompt')
                assert time.monotonic() - started < 0.5, 'Quick Ask service blocks GTK'
                pump_until(lambda: sent)
                assert len(sent) == 1, sent
                sent[0][0].close()
            app.args.ask = None
            # The first row is also a valid configured default.
            current_instance = window.get_current_instance()
            current_instance.properties['default_model'] = 'other-model'
            window.get_chat_list_page().new_chat('First Row Default')
            pump_until(lambda: window.get_selected_model().get_name() == 'other-model')
            current_instance.properties['default_model'] = 'test-model'
            application.AlpacaService(app).Create('Service New Chat')
            assert window.get_current_chat().get_name() == 'Service New Chat'
            for _ in range(5):
                app.lookup_action('preferences').activate(None)
                pump_until(lambda: isinstance(window.get_visible_dialog(), preferences.PreferencesDialog))
                window.get_visible_dialog().force_close()
                pump_until(lambda: window.get_visible_dialog() is None)
        print('Startup renders models on main thread; preferences work during loading and repeated opens: PASS', flush=True)

        # The one-slot queue exposes complete text, restores attachments, and leaves
        # replacing an unsent draft undoable through the normal GtkSource buffer.
        settings.set_boolean('queue-prompts', True)
        queued_chat = window.get_current_chat()
        queued_chat.busy = True
        queued_chat.active_message = Mock()
        buffer = window.global_footer.get_buffer()
        queued_text = 'Edit this message: café 🦙\n' + 'Long queued content\n' * 20
        window.global_footer.attachment_container.add_attachment(attachments.Attachment(
            '-1', 'queued-note.txt', 'plain_text', 'Queued attachment content'))
        buffer.set_text(queued_text)
        window.send_message()
        assert window.global_footer.prompt_queue_label.get_text() == queued_text.strip()
        buffer.set_text('Unsent draft')
        window.global_footer.edit_queued_prompt()
        assert not queued_chat.pending_prompts
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == queued_text.strip()
        assert buffer.get_can_undo()
        buffer.undo()
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == 'Unsent draft'
        buffer.redo()
        assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == queued_text.strip()
        restored_attachments = list(window.global_footer.attachment_container.container)
        assert len(restored_attachments) == 1
        assert restored_attachments[0].file_content == 'Queued attachment content'
        restored_attachments[0].delete()
        queued_chat.stop_message()
        queued_chat.finish_generation(queued_chat.active_message)
        buffer.set_text('')
        print('One-slot queue shows complete Unicode text; Edit restores attachments and Undo preserves the previous draft: PASS', flush=True)

        # Restore history containing concurrent, deliberately stalled image downloads.
        window.get_chat_list_page().new_chat('Image History')
        history = window.get_current_chat()
        saved = message.Message(dt=datetime.now(), message_id='image-history', mode=1, author='test-model')
        history.add_message(saved)
        SQL.insert_or_update_message(saved, force_content='![red](https://test/red.png)\n![blue](https://test/blue.png)')
        attachment = saved.add_attachment('note', 'note.txt', 'plain_text', 'Saved attachment')
        SQL.insert_or_update_attachment(saved, attachment)
        history.container.remove(saved)
        image_release = threading.Event()
        image_requests = []
        render_threads = []
        real_render = blocks.text_to_block_list

        def render_blocks(content):
            render_threads.append(threading.current_thread())
            assert threading.current_thread() is threading.main_thread(), 'History widgets created on worker'
            return real_render(content)

        def download(url, **kwargs):
            assert threading.current_thread() is not threading.main_thread(), 'Image download blocks GTK'
            assert kwargs['timeout'] == (5, 15)
            image_requests.append(url)
            assert image_release.wait(10), 'Image download was not released'
            output = BytesIO()
            Image.new('RGB', (2, 2), 'red' if 'red' in url else 'blue').save(output, format='PNG')
            return SimpleNamespace(status_code=200, content=output.getvalue(), raise_for_status=lambda: None)

        app.get_main_window = lambda: window
        with patch.object(blocks, 'text_to_block_list', side_effect=render_blocks), \
                patch.object(attachments.requests, 'get', side_effect=download):
            history.load_messages()
            pump_until(lambda: len(image_requests) == 2)
            assert render_threads and all(t is threading.main_thread() for t in render_threads)
            quick = QuickAskWindow(application=app)
            quick.present()
            quick.global_footer.get_buffer().set_text('Quick Ask remains usable')
            heartbeat = []
            GLib.timeout_add(10, lambda: heartbeat.append(True) and False)
            pump_until(lambda: heartbeat)
            assert not image_release.is_set()
            image_release.set()
            restored = list(history.container)[0]
            images = [block for block in restored.block_container if isinstance(block, blocks.InlinePicture)]
            pump_until(lambda: len(images) == 2 and all(image.texture is not None for image in images))
            for image in images:
                with Image.open(BytesIO(base64.b64decode(image.content))) as decoded:
                    expected = (255, 0, 0) if 'red' in image.url else (0, 0, 255)
                    assert decoded.getpixel((0, 0))[:3] == expected
            pump_until(lambda: len(list(restored.attachment_container.container)) == 1)
            quick.chat.pending_prompts.append({'text': 'cancel on close'})
            quick.close()
            assert not quick.chat.pending_prompts
        print('History widgets stay on main thread; Quick Ask remains responsive during concurrent slow images: PASS', flush=True)

        # Simulate the incomplete Ollama stream seen immediately before the saved crash.
        settings.set_boolean('queue-prompts', True)
        window.get_chat_list_page().new_chat('Crash Regression')
        current_chat = window.get_current_chat()
        instance = window.get_current_instance()
        dialog_threads = []
        real_dialog = Adw.Dialog

        def build_error_dialog(*args, **kwargs):
            assert threading.current_thread() is threading.main_thread(), 'Error dialog constructed on worker'
            return real_dialog(*args, **kwargs)
        def failed_stream(**kwargs):
            yield SimpleNamespace(message=SimpleNamespace(thinking='Thinking fragment', content='', tool_calls=[]), done=False)
            yield SimpleNamespace(message=SimpleNamespace(thinking='', content='Partial answer', tool_calls=[]), done=False)
            raise RuntimeError('peer closed connection without sending complete message body')

        def observe_dialog(*args):
            dialog_threads.append(threading.current_thread())

        window.connect('notify::visible-dialog', observe_dialog)
        with patch.object(instance, 'get_model_info', return_value={'capabilities': []}), \
                patch.object(instance.client, 'chat', side_effect=failed_stream), \
                patch.object(Adw, 'Dialog', side_effect=build_error_dialog), \
                patch.object(dialog, 'show_notification', Mock()):
            window.global_footer.get_buffer().set_text('Trigger the stream failure')
            window.send_message()
            window.global_footer.get_buffer().set_text('This should be canceled')
            window.send_message()
            assert current_chat.pending_prompts
            pump_until(lambda: not current_chat.busy and current_chat.active_message is None)
            pump_until(lambda: window.get_visible_dialog() is not None)
            assert dialog_threads and all(t is threading.main_thread() for t in dialog_threads)
            assert not current_chat.pending_prompts
            bot = list(current_chat.container)[-1]
            assert 'Partial answer' in bot.get_content(), bot.get_content()
            assert bot.block_container.thinking_block is None
            assert any(attachment.file_type == 'thought' and 'Thinking fragment' in attachment.file_content
                       for attachment in bot.attachment_container.container)
            window.get_visible_dialog().force_close()
        print('Failed stream shows a real error dialog on main thread, preserves partial reply, and cancels queue: PASS', flush=True)

        # Deselecting the last instance must also release the loading controls.
        window.instance_listbox.unselect_all()
        pump_until(window.instance_listbox.get_sensitive)
        window.destroy()


if __name__ == '__main__':
    main()
