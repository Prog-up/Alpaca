"""GTK integration for resumable agent tasks. Model requests run on workers."""

from copy import deepcopy
from datetime import datetime
import json
import os
import threading
import httpx
import ollama
from gi.repository import GLib
from ..agent import AgentController, AgentRunner, TERMINAL_STATES
from ..sql_manager import Instance as SQL, generate_uuid
from .main_thread import on_main_thread


def capture(settings, instance):
    return {'target': {'type': settings.get_int('activity-terminal-type'),
                       'username': settings.get_string('activity-terminal-username') or os.getenv('USER'),
                       'host': settings.get_string('activity-terminal-ip') or '127.0.0.1',
                       'working_directory': settings.get_string('agent-working-directory'),
                       'automatic_execution': settings.get_boolean('activity-terminal-auto-execute'),
                       'instance_url': instance.properties.get('url')},
            'auto_start': settings.get_boolean('agent-auto-start'),
            'max_calls': settings.get_int('agent-max-calls'),
            'max_seconds': settings.get_int('agent-max-seconds'),
            'instance_properties': deepcopy(instance.properties)}


def active(chat):
    return chat.agent_session is not None and chat.agent_session.controller.phase not in TERMINAL_STATES


def restore(chat):
    data = SQL.get_agent_task(chat.chat_id)
    if data and chat.agent_session is None:
        chat.agent_session = AgentSession(chat, data=data)


class AgentSession:
    def __init__(self, chat, prompt=None, data=None):
        self.chat = chat
        self.revision = -1
        self.instance = None
        self.properties = None
        self.terminal = None
        self.client = None
        if data:
            self.controller = AgentController.restore(data, self.on_change)
        else:
            self.instance = prompt['instance']
            config = prompt['agent_config']
            self.properties = config['instance_properties']
            self.controller = AgentController(prompt['text'], prompt['model'], self.instance.instance_id,
                                              config['target'], auto_start=config['auto_start'],
                                              max_calls=config['max_calls'], max_seconds=config['max_seconds'],
                                              on_change=self.on_change)
            self.add_attachments(self.controller.data['messages'][0], prompt['attachments'])
        self.on_change(self.controller.snapshot())

    @staticmethod
    def add_attachments(entry, attachments):
        for attachment in attachments:
            if attachment['type'] == 'image':
                entry.setdefault('images', []).append(attachment['content'])
            else:
                entry['content'] += '\n\n{}:\n{}'.format(attachment['name'], attachment['content'])

    @on_main_thread
    def on_change(self, data):
        if self.chat.agent_session not in (None, self):
            return
        if data['revision'] < self.revision:
            return
        self.revision = data['revision']
        SQL.save_agent_task(self.chat.chat_id, data)
        root = self.chat.get_root() or self.chat.row.get_root()
        if root and root.get_current_chat() is self.chat:
            root.global_footer.update_agent(self.chat)

    def resolve_instance(self, window):
        if self.properties is not None:
            return
        current = window.get_current_instance()
        data = self.controller.data
        if current and current.instance_id == data['instance_id']:
            self.instance = current
            self.properties = deepcopy(current.properties)
        else:
            for row in SQL.get_instances():
                if row['id'] == data['instance_id'] and row['type'].startswith('ollama'):
                    from .instances import ollama_instances
                    for candidate in vars(ollama_instances).values():
                        if isinstance(candidate, type) and getattr(candidate, 'instance_type', None) == row['type']:
                            self.instance = candidate(row['id'], row['properties'])
                            self.properties = deepcopy(self.instance.properties)
                            break
                    break
        if self.properties is None:
            raise ValueError(_('The task’s instance is no longer available. Start a new task.'))
        if self.properties.get('url') != data['target'].get('instance_url'):
            self.properties = None
            raise ValueError(_('The instance connection changed. Start a new task with the new connection.'))

    def submit(self, window, prompt):
        if self.controller.phase in {'waiting_for_answer', 'paused', 'waiting_for_start'}:
            if self.controller.phase == 'waiting_for_start':
                self.controller.suspend(editing=True)
            self.controller.answer(prompt['text'])
            self.add_attachments(self.controller.data['messages'][-1], prompt['attachments'])
        self.launch(window)

    def launch(self, window):
        from . import message, dialog
        if self.chat.active_message is not None:
            return
        try:
            self.resolve_instance(window)
        except ValueError as error:
            dialog.show_toast(str(error), window)
            self.controller.suspend()
            return
        bot = message.Message(dt=datetime.now(), message_id=generate_uuid(), mode=1,
                              author=self.controller.data['model'])
        self.chat.add_message(bot)
        self.chat.busy = True
        self.chat.active_message = bot
        bot.block_container.show_generating_block()
        GLib.idle_add(bot.save)
        if window.get_current_chat() is self.chat:
            window.global_footer.toggle_action_button(False)
            window.global_footer.update_agent(self.chat)

        def run():
            try:
                if self.controller.cancel_event.is_set():
                    return
                self.instance.start()
                api = (self.properties.get('api') or '').strip()
                self.client = ollama.Client(host=self.properties.get('url'),
                                            headers={'Authorization': 'Bearer ' + api} if api else {},
                                            verify=not self.properties.get('allow_self_signed_ssl', False),
                                            timeout=httpx.Timeout(60, connect=10))
                info = self.client.show(self.controller.data['model'])
                if 'tools' not in info.get('capabilities', []):
                    raise ValueError(_('Agent Mode requires an Ollama model with tool support.'))
                options = {'temperature': self.properties.get('temperature', 0.2),
                           'num_ctx': self.properties.get('num_ctx', 16384)}
                AgentRunner(self.controller, self.client, bot.update_message,
                            lambda args, cancel: self.execute(args, cancel, bot), options).run()
            except Exception as error:
                if not self.controller.cancel_event.is_set():
                    self.controller.fail(error)
                    bot.update_message(_('Agent error: {}').format(error))
            finally:
                if self.client:
                    try:
                        self.client._client.close()
                    except Exception:
                        pass
                    self.client = None
                bot.finish_generation(success=self.controller.phase not in {'failed', 'canceled'})
        GLib.idle_add(threading.Thread(target=run, daemon=True).start)

    def execute(self, arguments, cancel, bot):
        from .tools.tools import Terminal
        if self.terminal is None:
            self.terminal = Terminal()
        self.terminal.connection = deepcopy(self.controller.data['target'])
        self.terminal.automatic_execution = self.controller.data['target']['automatic_execution']
        self.terminal.cancel_event = cancel
        text = self.terminal.run(arguments, [], bot)
        if not self.terminal.current_command:
            self.controller.suspend()
        result = {'exit_code': self.terminal.last_exit_code,
                  'output': (self.terminal.last_output or text)[-16000:],
                  'command': self.terminal.current_command,
                  'canceled': self.terminal.last_cancelled}
        if self.terminal.last_cancelled:
            self.controller.suspend()
            # A child-exited callback may still arrive after a canceled wait.
            self.terminal = None

        def show_result():
            attachment = bot.add_attachment(generate_uuid(), _('Command Result'), 'tool',
                                             '```\n{}\n```\n\n{}'.format(arguments['command'], json.dumps(result, ensure_ascii=False)))
            if self.chat.chat_id:
                SQL.insert_or_update_attachment(bot, attachment)
        GLib.idle_add(show_result)
        return result

    def pause(self, editing=False):
        self.controller.suspend(editing=editing)
        self.chat.busy = False
        if self.terminal:
            self.terminal.cancel()

    def stop(self):
        self.controller.suspend('canceled')
        if self.terminal:
            self.terminal.cancel()

    def resume(self, window):
        if self.chat.active_message is not None:
            return
        self.controller.resume()
        if self.controller.phase != 'waiting_for_answer':
            self.launch(window)
        else:
            window.global_footer.update_agent(self.chat)


def dispatch(window, chat, prompt):
    if not active(chat):
        chat.agent_session = AgentSession(chat, prompt=prompt)
        chat.agent_session.on_change(chat.agent_session.controller.snapshot())
    chat.agent_session.submit(window, prompt)
