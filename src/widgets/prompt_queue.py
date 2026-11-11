"""Capture prompts at send time and dispatch each chat's queue in order."""

from datetime import datetime
import threading
from gi.repository import GLib
from ..sql_manager import Instance as SQL, generate_uuid
from . import dialog, message


def update_queue(window, chat):
    if window and window.get_current_chat() is chat:
        window.global_footer.update_prompt_queue(chat)


def send_message(window, chat, mode=0, available_tools=None):
    footer = window.global_footer
    buffer = footer.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()
    if not text:
        return
    session = getattr(chat, 'agent_session', None)
    active_task = session is not None and session.controller.phase not in {'completed', 'failed', 'canceled'}
    editing_queued = getattr(footer, 'queued_edit_chat', None) is chat
    queue_send = chat.busy or (editing_queued and active_task)
    if queue_send and not window.settings.get_boolean('queue-prompts'):
        return
    if queue_send and chat.pending_prompts:
        dialog.show_toast(_('One message is already queued. Edit or clear it before queuing another.'), window)
        return
    # Wait for a stopped generation to finish unwinding before accepting new work.
    if not chat.busy and chat.active_message is not None:
        return
    answering = active_task and not queue_send
    instance = session.instance if answering and session.instance else window.get_current_instance()
    if not answering and (not instance or instance.instance_type == 'empty'):
        dialog.show_toast(_('Please select an instance before chatting'), window)
        return
    model = session.controller.data['model'] if answering else window.get_selected_model().get_name()
    if model is None:
        dialog.show_toast(_('Please select a model before chatting'), window)
        return

    attachments = list(footer.attachment_container.container)
    prompt = {
        'text': text, 'mode': mode, 'model': model, 'instance': instance,
        'tools': dict(available_tools or {}),
        'attachments': [{'name': attachment.file_name, 'type': attachment.file_type,
                         'content': attachment.file_content} for attachment in attachments]
    }
    if mode == 0 and (window.settings.get_boolean('agent-mode') or answering):
        if not answering and not instance.instance_type.startswith('ollama'):
            dialog.show_toast(_('Agent Mode requires an Ollama instance'), window)
            return
        from . import agent
        prompt['agent_config'] = agent.capture(window.settings, instance)
    buffer.delete(buffer.get_start_iter(), buffer.get_end_iter())
    for attachment in attachments:
        attachment.delete()
    footer.queued_edit_chat = None
    if queue_send:
        chat.pending_prompts.append(prompt)
        update_queue(window, chat)
    else:
        dispatch(window, chat, prompt)


def dispatch(window, chat, prompt):
    if chat.chat_id and window.get_current_chat() is chat and chat.row.get_parent():
        row = chat.row
        chat_list = row.get_parent()
        GLib.idle_add(chat_list.unselect_all)
        GLib.idle_add(chat_list.remove, row)
        GLib.idle_add(chat_list.prepend, row)
        GLib.idle_add(chat_list.select_row, row)
    user_message = message.Message(dt=datetime.now(), message_id=generate_uuid(), mode=prompt['mode'] * 2)
    chat.add_message(user_message)
    for data in prompt['attachments']:
        attachment = user_message.add_attachment(file_id=generate_uuid(), name=data['name'],
                                                 attachment_type=data['type'], content=data['content'])
        if chat.chat_id:
            SQL.insert_or_update_attachment(user_message, attachment)
    user_message.block_container.set_content(prompt['text'])
    GLib.idle_add(user_message.save)

    if prompt['mode'] == 1:
        chat.set_visible_child_name('content')
        return

    if 'agent_config' in prompt:
        from . import agent
        agent.dispatch(window, chat, prompt)
        return

    bot_message = message.Message(dt=datetime.now(), message_id=generate_uuid(), mode=1, author=prompt['model'])
    chat.add_message(bot_message)
    GLib.idle_add(bot_message.save)
    chat.busy = True
    chat.active_message = bot_message
    bot_message.block_container.show_generating_block()
    if window.get_current_chat() is chat:
        window.global_footer.toggle_action_button(False)

    def generate():
        try:
            instance = prompt['instance']
            instance.start()
            if prompt['tools']:
                instance.use_tools(bot_message, prompt['model'], prompt['tools'])
            else:
                instance.generate_message(bot_message, prompt['model'])
        except Exception as error:
            GLib.idle_add(dialog.show_toast, _('Message generation failed: {}').format(error), window)
            GLib.idle_add(chat.finish_generation, bot_message, False)

    GLib.idle_add(threading.Thread(target=generate, daemon=True).start)


def drain(window, chat):
    session = getattr(chat, 'agent_session', None)
    if session is not None and session.controller.phase not in {'completed', 'failed', 'canceled'}:
        update_queue(window, chat)
        return
    if not window or not window.settings.get_boolean('queue-prompts'):
        chat.pending_prompts.clear()
    while window and chat.pending_prompts and not chat.busy:
        prompt = chat.pending_prompts.popleft()
        dispatch(window, chat, prompt)
    update_queue(window, chat)
