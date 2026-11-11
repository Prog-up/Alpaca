from collections import deque
import tempfile
import unittest
from unittest.mock import Mock

from support import load_modules


class PromptQueueTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.queue = load_modules(directory.name, include_queue=True)[2]
        self.queue.threading = Mock()
        self.queue.message.Message = Mock(side_effect=lambda **kwargs: Mock(**kwargs))
        self.queue.dialog.show_toast = Mock()
        self.text = 'first prompt'
        self.window = Mock()
        self.chat = Mock(busy=False, active_message=None, agent_session=None, pending_prompts=deque(), chat_id='chat-id')
        self.chat.container = []
        self.chat.add_message.side_effect = self.chat.container.append
        self.window.get_current_chat.return_value = self.chat
        self.window.settings.get_boolean.side_effect = lambda key: key == 'queue-prompts'
        self.window.get_selected_model.return_value.get_name.return_value = 'first-model'
        self.instance = self.window.get_current_instance.return_value
        self.instance.instance_type = 'ollama:remote'
        self.buffer = self.window.global_footer.get_buffer.return_value
        self.buffer.get_text.side_effect = lambda *args: self.text
        self.buffer.delete.side_effect = lambda *args: setattr(self, 'text', '')
        self.window.global_footer.attachment_container.container = []

    def send(self, text, mode=0, tools=None):
        self.text = text
        self.queue.send_message(self.window, self.chat, mode, tools)

    def finish(self):
        self.chat.busy = False
        self.chat.active_message = None
        self.queue.drain(self.window, self.chat)

    def test_disabled_preserves_draft_and_attachments(self):
        self.chat.busy = True
        self.window.settings.get_boolean.side_effect = lambda key: False
        attachment = Mock()
        self.window.global_footer.attachment_container.container = [attachment]
        self.send('waiting')
        self.assertEqual(self.text, 'waiting')
        self.assertFalse(self.chat.pending_prompts)
        attachment.delete.assert_not_called()
        self.queue.message.Message.assert_not_called()

    def test_idle_prompt_reserves_chat_before_worker_starts(self):
        self.send('first')
        self.assertTrue(self.chat.busy)
        self.assertIs(self.chat.active_message, self.chat.container[-1])
        self.assertEqual(self.text, '')
        self.queue.threading.Thread.return_value.start.assert_called_once()

    def test_only_one_prompt_waits_and_second_queued_send_preserves_draft(self):
        self.send('first')
        self.send('second')
        self.send('third')
        self.assertEqual([item['text'] for item in self.chat.pending_prompts], ['second'])
        self.assertEqual(self.text, 'third')
        self.assertEqual(len(self.chat.container), 2)
        self.finish()
        self.chat.container[-2].block_container.set_content.assert_called_with('second')
        self.assertFalse(self.chat.pending_prompts)
        self.assertEqual(self.text, 'third')

    def test_full_slot_preserves_attachments(self):
        self.chat.busy = True
        self.send('queued')
        attachment = Mock()
        self.window.global_footer.attachment_container.container = [attachment]
        self.send('keep draft and attachment')
        self.assertEqual(self.text, 'keep draft and attachment')
        attachment.delete.assert_not_called()
        self.assertEqual(len(self.chat.pending_prompts), 1)

    def test_dispatch_preserves_new_composer_draft(self):
        self.chat.busy = True
        self.send('queued')
        self.text = 'unfinished new draft'
        self.finish()
        self.assertEqual(self.text, 'unfinished new draft')

    def test_snapshots_model_instance_tools_and_attachment(self):
        self.chat.busy = True
        attachment = Mock(file_name='note.txt', file_type='plain_text', file_content='original')
        self.window.global_footer.attachment_container.container = [attachment]
        tools = {'run_command': Mock()}
        self.send('queued', tools=tools)
        captured_tool = tools['run_command']
        tools.clear()
        attachment.file_content = 'changed'
        self.window.get_selected_model.return_value.get_name.return_value = 'different-model'
        self.window.get_current_instance.return_value = Mock(instance_type='other')
        self.finish()
        self.assertEqual(self.chat.active_message.author, 'first-model')
        self.chat.container[0].add_attachment.assert_called_once()
        self.assertEqual(self.chat.container[0].add_attachment.call_args.kwargs['content'], 'original')
        target = self.queue.threading.Thread.call_args.kwargs['target']
        target()
        self.instance.use_tools.assert_called_once_with(self.chat.active_message, 'first-model', {'run_command': captured_tool})

    def test_queue_belongs_to_original_chat_after_switch(self):
        self.chat.busy = True
        self.send('original chat')
        self.window.get_current_chat.return_value = Mock()
        self.window.global_footer.reset_mock()
        self.finish()
        self.chat.container[0].block_container.set_content.assert_called_with('original chat')
        self.window.global_footer.toggle_action_button.assert_not_called()
        self.window.global_footer.update_prompt_queue.assert_not_called()

    def test_system_prompts_drain_before_next_user_prompt(self):
        self.chat.busy = True
        self.send('system instructions', mode=1)
        self.send('user question')
        self.assertEqual(self.text, 'user question')
        self.finish()
        self.send('user question')
        self.assertEqual([item.mode for item in self.chat.container], [2, 0, 1])
        self.assertTrue(self.chat.busy)
        self.assertFalse(self.chat.pending_prompts)

    def test_edited_queued_goal_is_not_a_clarification_answer(self):
        from types import SimpleNamespace
        self.chat.agent_session = SimpleNamespace(controller=SimpleNamespace(phase='waiting_for_answer'))
        self.window.global_footer.queued_edit_chat = self.chat
        self.send('edited next task')
        self.assertEqual(self.chat.pending_prompts[0]['text'], 'edited next task')
        self.queue.message.Message.assert_not_called()

    def test_disabling_queue_prevents_followups(self):
        self.chat.busy = True
        self.send('queued')
        self.window.settings.get_boolean.side_effect = lambda key: False
        self.finish()
        self.assertFalse(self.chat.pending_prompts)
        self.queue.message.Message.assert_not_called()

    def test_detached_deleted_chat_does_not_dispatch(self):
        self.chat.busy = True
        self.send('queued')
        self.chat.busy = False
        self.queue.drain(None, self.chat)
        self.assertFalse(self.chat.pending_prompts)
        self.queue.message.Message.assert_not_called()

    def test_invalid_instance_does_not_consume_input(self):
        self.instance.instance_type = 'empty'
        self.send('keep this')
        self.assertEqual(self.text, 'keep this')
        self.queue.message.Message.assert_not_called()

    def test_invalid_model_does_not_consume_input(self):
        self.window.get_selected_model.return_value.get_name.return_value = None
        self.send('keep this')
        self.assertEqual(self.text, 'keep this')

    def test_stopped_worker_blocks_new_submission_until_finished(self):
        self.chat.active_message = Mock()
        self.send('keep this')
        self.assertEqual(self.text, 'keep this')
        self.queue.message.Message.assert_not_called()

    def test_start_failure_reports_and_cancels_queue(self):
        self.chat.finish_generation.side_effect = lambda *args: self.chat.pending_prompts.clear()
        self.instance.start.side_effect = RuntimeError('offline')
        self.send('first')
        self.send('queued')
        self.queue.threading.Thread.call_args.kwargs['target']()
        self.chat.finish_generation.assert_called_once_with(self.chat.active_message, False)
        self.assertFalse(self.chat.pending_prompts)


if __name__ == '__main__':
    unittest.main()
