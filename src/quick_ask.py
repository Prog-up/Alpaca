# quick_ask.py

"""
Handles the Quick Ask window
"""

import gi
from gi.repository import Adw, Gtk, Gdk, GLib, GtkSource, Gio, Spelling

from . import widgets as Widgets
from .sql_manager import Instance as SQL

@Gtk.Template(resource_path='/com/jeffser/Alpaca/quick_ask.ui')
class QuickAskWindow(Adw.ApplicationWindow):

    __gtype_name__ = 'AlpacaQuickAskWindow'

    chat = Gtk.Template.Child()
    save_button = Gtk.Template.Child()
    global_footer = Gtk.Template.Child()

    # tts
    message_dictated = None

    @Gtk.Template.Callback()
    def closing_app(self, element):
        self.chat.stop_message()
        if self._model_list_handler is not None:
            self.global_footer.model_selector.get_model().disconnect(self._model_list_handler)
            self._model_list_handler = None
        if not self.get_application().get_main_window().get_visible():
            # Use GLib.idle_add to ensure proper cleanup sequence
            GLib.idle_add(self.get_application().quit)

    @Gtk.Template.Callback()
    def save_chat(self, button):
        main_window = self.get_application().get_main_window()
        main_window.present()
        new_chat = main_window.get_chat_list_page().new_chat(self.chat.get_name())
        for message in list(self.chat.container):
            SQL.insert_or_update_message(message, force_chat_id=new_chat.chat_id)
        if self.chat.agent_session:
            self.chat.agent_session.pause()
            SQL.save_agent_task(new_chat.chat_id, self.chat.agent_session.controller.snapshot())
        new_chat.load_messages()
        GLib.idle_add(new_chat.row.get_parent().select_row, new_chat.row)
        self.close()

    def get_selected_model(self):
        item = self.global_footer.model_selector.get_selected_item()
        if item:
            return item.model

        return Widgets.models.text.FallbackModel

    def get_current_instance(self):
        return self.get_application().get_main_window().get_current_instance()

    def send_message(self, mode:int=0, available_tools:dict={}):
        Widgets.prompt_queue.send_message(self, self.chat, mode, available_tools)

    def write_and_send_message(self, message:str):
        buffer = self.global_footer.get_buffer()
        buffer.delete(buffer.get_start_iter(), buffer.get_end_iter())
        buffer.insert(buffer.get_start_iter(), message, len(message.encode('utf-8')))
        self.send_message()

    def get_current_chat(self) -> Gtk.Widget:
        return self.chat

    def select_default_model(self, *args):
        model_list = self.global_footer.model_selector.get_model()
        if len(model_list) == 0:
            self._default_model_selected = False
            return
        if self._default_model_selected:
            return
        default = self.get_current_instance().properties.get('default_model')
        for index, row in enumerate(model_list):
            if row.model.get_name() == default:
                self._default_model_selected = True
                self.global_footer.model_selector.set_selected(index)
                break

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.global_footer.model_manager_shortcut.set_visible(False)

        self.settings = Gio.Settings(schema_id="com.jeffser.Alpaca")
        self._default_model_selected = False
        self._model_list_handler = self.global_footer.model_selector.get_model().connect('items-changed', self.select_default_model)
        self.select_default_model()
        self.set_focus(self.global_footer.message_text_view)

        self.chat.set_visible_child_name('welcome-screen')
            
