# window.py
#
# Copyright 2024-2025 Jeffser
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Handles the main window
"""

import json, threading, os, re, gettext, shutil, logging, time, requests, sys, tempfile, importlib.util
import numpy as np

from datetime import datetime

from gi.repository import Adw, Gtk, Gdk, GLib, GtkSource, Gio, Spelling

from .sql_manager import generate_uuid, generate_numbered_name, prettify_model_name, Instance as SQL
from . import widgets as Widgets
from .constants import data_dir, source_dir, cache_dir, is_ollama_installed, IN_FLATPAK

logger = logging.getLogger(__name__)

@Gtk.Template(resource_path='/com/jeffser/Alpaca/window.ui')
class AlpacaWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'AlpacaWindow'

    localedir = os.path.join(source_dir, 'locale')

    gettext.bindtextdomain('com.jeffser.Alpaca', localedir)
    gettext.textdomain('com.jeffser.Alpaca')
    _ = gettext.gettext

    #Elements
    new_chat_splitbutton = Gtk.Template.Child()
    model_manager = Gtk.Template.Child()
    instance_manager_stack = Gtk.Template.Child()
    main_navigation_view = Gtk.Template.Child()
    split_view_overlay = Gtk.Template.Child()
    toast_overlay = Gtk.Template.Child()
    chat_bin = Gtk.Template.Child()
    chat_list_navigationview = Gtk.Template.Child()
    global_footer = Gtk.Template.Child()
    message_searchbar = Gtk.Template.Child()
    searchentry_messages = Gtk.Template.Child()

    file_filter_db = Gtk.Template.Child()

    banner = Gtk.Template.Child()

    instance_preferences_page = Gtk.Template.Child()
    instance_listbox = Gtk.Template.Child()
    last_selected_instance_row = None

    chat_split_view_overlay = Gtk.Template.Child()
    activity_manager = Gtk.Template.Child()
    chat_page = Gtk.Template.Child()
    small_breakpoint = Gtk.Template.Child()

    chat_searchbar = Gtk.Template.Child()

    @Gtk.Template.Callback()
    def chat_list_page_changed(self, navigationview, page=None):
        if self.chat_searchbar.get_search_mode():
            self.chat_searchbar.set_search_mode(False)
            previous_page = navigationview.get_previous_page(navigationview.get_visible_page())
            if previous_page:
                previous_page.on_search('')

    @Gtk.Template.Callback()
    def first_breakpoint_applied(self, bp):
        if len(self.activity_manager.tabview.get_pages()) == 0:
            self.chat_split_view_overlay.set_show_sidebar(False)

    @Gtk.Template.Callback()
    def add_instance(self, button, hide_ollama_managed:bool=False):
        def show_dialog(instance):
            Widgets.instances.InstancePreferencesDialog(instance).present(self)

        def selected(ins):
            instance = ins(
                instance_id=None,
                properties={}
            )

            if ins.instance_type == 'ollama:managed' and not is_ollama_installed():
                dialog = Widgets.instances.OllamaManager(instance)
                dialog.connect('closed', lambda *_: show_dialog(instance) if is_ollama_installed() else None)
                dialog.present(self)
            else:
                show_dialog(instance)

        options = {}
        instance_list = Widgets.instances.ollama_instances.BaseInstance.__subclasses__()
        if hide_ollama_managed:
            instance_list = instance_list[1:]
        if os.getenv('ALPACA_OLLAMA_ONLY', '0') != '1' and importlib.util.find_spec('openai'):
            instance_list += Widgets.instances.openai_instances.BaseInstance.__subclasses__()
        for ins_type in instance_list:
            options[ins_type.instance_type_display] = ins_type

        Widgets.dialog.simple_dropdown(
            parent = button.get_root(),
            heading = _("Add Instance"),
            body = _("Select a type of instance to add"),
            callback = lambda option, options=options: selected(options[option]),
            items = options.keys()
        )

    @Gtk.Template.Callback()
    def instance_changed(self, listbox, row):
        if not listbox.get_sensitive():
            return
        listbox.set_sensitive(False)
        previous_instance = self.last_selected_instance_row.instance if self.last_selected_instance_row else None
        instance = row.instance if row else Widgets.instances.Empty()
        self.last_selected_instance_row = row

        def finish_change(local_models, available_models):
            # A failed request may have deselected this instance in the meantime.
            selected_row = listbox.get_selected_row()
            if selected_row is not row:
                listbox.set_sensitive(True)
                self.instance_changed(listbox, selected_row)
                return
            self.model_manager.update_available_model_list(available_models)
            self.model_manager.update_added_model_list(local_models)
            self.get_chat_list_page().auto_select_model()
            if row:
                self.settings.set_string('selected-instance', instance.instance_id)
            for name in ('model_creator_existing', 'model_creator_gguf'):
                self.get_application().lookup_action(name).set_enabled(instance.instance_type in ('ollama', 'ollama:managed'))
            listbox.set_sensitive(True)

        def update_model_info(model_name, info):
            if self.get_current_instance() is not instance:
                return
            model = Widgets.models.text.list_from_selector().get(model_name)
            if not model:
                return
            model.data.update(info)
            for window in self.get_application().get_windows():
                footer = getattr(window, 'global_footer', None)
                if footer:
                    selected = footer.model_selector.get_selected_item()
                    if selected and selected.model is model:
                        footer.tool_selector.model_changed(footer.model_selector.selector)

        def change_instance():
            local_models, available_models = [], {}
            pending_info = []
            try:
                if previous_instance:
                    previous_instance.stop()
                if row:
                    instance.start()
                instance.local_models = None
                local_models = instance.get_local_models()
                available_models = instance.get_available_models()
                # Show the model list before fetching each model's metadata.
                # Provisional capabilities prevent network calls from GTK callbacks.
                for model in local_models:
                    if 'capabilities' not in model:
                        pending_info.append(model.get('name'))
                        model['capabilities'] = []
            except Exception as error:
                logger.exception('Could not load instance')
                Widgets.dialog.simple_error(self, _('Instance Error'), _('Could not retrieve added models'), str(error))
            finally:
                GLib.idle_add(finish_change, local_models, available_models)
            pending_info.sort(key=lambda name: name != instance.properties.get('default_model'))
            for model_name in pending_info:
                if self.last_selected_instance_row is not row:
                    break
                try:
                    info = instance.get_model_info(model_name)
                    if hasattr(info, 'model_dump'):
                        info = info.model_dump(exclude_none=True)
                    GLib.idle_add(update_model_info, model_name, info)
                except Exception:
                    logger.exception('Could not retrieve model metadata')
        threading.Thread(target=change_instance, daemon=True).start()

    @Gtk.Template.Callback()
    def closing_app(self, user_data):
        def close():
            for chat in list(Widgets.chat.loaded_chats):
                if chat.agent_session:
                    chat.agent_session.pause()
                    chat.pending_prompts.clear()
                else:
                    chat.stop_message()
            try:
                self.settings.set_string('default-chat', self.chat_bin.get_child().chat_id)
            except Exception as e:
                logger.warning(f"Could not save default chat: {e}")
            
            try:
                current_instance = self.get_current_instance()
                if hasattr(current_instance, 'stop'):
                    current_instance.stop()
            except Exception as e:
                logger.warning(f"Error stopping current instance: {e}")
            
            try:
                if Widgets.voice.message_dictated:
                    Widgets.voice.message_dictated.popup.tts_button.set_active(False)
            except Exception as e:
                logger.warning(f"Error stopping voice: {e}")

            try:
                for el in ("default-width", "default-height"):
                    self.settings.set_int(el, self.get_property(el))
                self.settings.set_boolean("maximized", self.get_property("maximized"))
            except Exception as e:
                logger.warning(f'Error saving window preferences: {e}')
            
            # Quit from the GLib main loop to avoid teardown races with worker threads
            GLib.idle_add(self.get_application().quit)

        def switch_to_hide():
            self.set_hide_on_close(True)
            self.close() #Recalls this function

        if self.get_hide_on_close():
            logger.info("Hiding app...")
        else:
            logger.info("Closing app...")
            is_chat_busy = any(chat.busy for chat in list(Widgets.chat.loaded_chats))
            is_model_downloading = any([el for el in list(self.model_manager.added_model_flowbox) if el.get_child().progressbar.get_visible()])
            if is_chat_busy or is_model_downloading:
                options = {
                    _('Cancel'): {'default': True},
                    _('Hide'): {'callback': switch_to_hide},
                    _('Close'): {'callback': close, 'appearance': 'destructive'},
                }
                Widgets.dialog.Options(
                    heading = _('Close Alpaca?'),
                    body = _('A task is currently in progress. Are you sure you want to close Alpaca?'),
                    close_response = list(options.keys())[0],
                    options = options,
                ).show(self)
                return True
            else:
                close()

    @Gtk.Template.Callback()
    def chat_search_changed(self, entry):
        self.get_chat_list_page().on_search(entry.get_text())

    @Gtk.Template.Callback()
    def message_search_changed(self, entry, current_chat=None):
        self.chat_bin.get_child().on_search(entry.get_text())

    def send_message(self, mode:int=0, available_tools:dict={}):
        Widgets.prompt_queue.send_message(self, self.chat_bin.get_child(), mode, available_tools)

    def get_selected_model(self):
        selected_item = self.global_footer.model_selector.get_selected_item()
        if selected_item:
            return selected_item.model
        else:
            return Widgets.models.text.FallbackModel

    def get_current_instance(self):
        if self.instance_listbox.get_selected_row():
            return self.instance_listbox.get_selected_row().instance
        else:
            return Widgets.instances.Empty()

    def on_chat_imported(self, file):
        if file:
            if os.path.isfile(os.path.join(cache_dir, 'import.db')):
                os.remove(os.path.join(cache_dir, 'import.db'))
            file.copy(Gio.File.new_for_path(os.path.join(cache_dir, 'import.db')), Gio.FileCopyFlags.OVERWRITE, None, None, None, None)
            chat_names = [tab.chat.get_name() for tab in list(self.get_chat_list_page().chat_list_box)]
            for chat in SQL.import_chat(os.path.join(cache_dir, 'import.db'), chat_names, self.get_chat_list_page().folder_id):
                self.get_chat_list_page().add_chat(
                    chat_name=chat[1],
                    chat_id=chat[0],
                    is_template=False,
                    mode=1
                )
            Widgets.dialog.show_toast(_("Chat imported successfully"), self)

    def toggle_searchbar(self):
        current_tag = self.main_navigation_view.get_visible_page_tag()

        searchbars = {
            'chat': self.message_searchbar,
            'model_manager': self.model_manager.searchbar
        }

        if searchbars.get(current_tag):
            searchbars.get(current_tag).set_search_mode(not searchbars.get(current_tag).get_search_mode())

    def get_chat_list_page(self):
        return self.chat_list_navigationview.get_visible_page()

    def push_or_pop(self, page_name:str):
        if self.main_navigation_view.get_visible_page().get_tag() != page_name:
            GLib.idle_add(self.main_navigation_view.push_by_tag, page_name)
        else:
            GLib.idle_add(self.main_navigation_view.pop_to_tag, 'chat')

    def open_available_model_page(self):
        self.main_navigation_view.push_by_tag('model_manager')
        self.model_manager.view_stack.set_visible_child_name('available_models')

    def prepare_screenshoter(self):
        #used to take screenshots of widgets for documentation
        widget = self.get_focus().get_parent()
        while True:
            if 'Alpaca' in repr(widget):
                break
            widget = widget.get_parent()

        widget.unparent()
        Adw.ApplicationWindow(
            width_request=640,
            height_request=10,
            content=widget
        ).present()

    def get_current_chat(self) -> Gtk.Widget:
        return self.chat_bin.get_child()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        actions = [[{
            'label': _('New Chat'),
            'callback': lambda: self.get_application().lookup_action('new_chat').activate(),
            'icon': 'chat-message-new-symbolic'
        },{
            'label': _('New Folder'),
            'callback': lambda: self.get_application().lookup_action('new_folder').activate(),
            'icon': 'folder-new-symbolic'
        }]]
        popover = Widgets.dialog.Popover(actions)
        popover.set_has_arrow(True)
        popover.set_halign(0)
        self.new_chat_splitbutton.set_popover(popover)

        self.set_focus(self.global_footer.message_text_view)

        self.settings = Gio.Settings(schema_id="com.jeffser.Alpaca")
        for el in ("default-width", "default-height", "maximized", "hide-on-close"):
            self.set_property(el, self.settings.get_value(el).unpack())

        # Zoom
        Widgets.preferences.set_zoom(Widgets.preferences.get_zoom())

        universal_actions = {
            'new_chat': [lambda *_: self.get_chat_list_page().new_chat(), ['<primary>n']],
            'new_folder': [lambda *_: self.get_chat_list_page().prompt_new_folder(), ['<primary>d']],
            'import_chat': [lambda *_: Widgets.dialog.simple_file(
                parent=self,
                file_filters=[self.file_filter_db],
                callback=self.on_chat_imported
            )],
            'duplicate_current_chat': [lambda *_: self.chat_bin.get_child().row.duplicate()],
            'delete_current_chat': [lambda *_: self.chat_bin.get_child().row.prompt_delete(), ['<primary>w']],
            'edit_current_chat': [lambda *_: self.chat_bin.get_child().row.prompt_edit(), ['F2']],
            'export_current_chat': [lambda *_: self.chat_bin.get_child().row.prompt_export()],
            'toggle_sidebar': [lambda *_: self.split_view_overlay.set_show_sidebar(not self.split_view_overlay.get_show_sidebar()), ['F9']],
            'toggle_search': [lambda *_: self.toggle_searchbar(), ['<primary>f']],
            'model_manager' : [lambda *_: self.push_or_pop('model_manager'), ['<primary>m']],
            'model_manager_available' : [lambda *_: self.open_available_model_page()],
            'instance_manager' : [lambda *_: self.push_or_pop('instance_manager'), ['<primary>i']],
            'add_model_by_name' : [lambda *i: Widgets.dialog.simple_entry(
                parent=self,
                heading=_('Pull Model'),
                body=_('Please enter the model name following this template: name:tag'),
                callback=lambda name: Widgets.models.basic.confirm_pull_model(window=self, model_name=name),
                entries={'placeholder': 'deepseek-r1:7b'}
            )],
            'reload_added_models': [lambda *_: GLib.idle_add(self.model_manager.update_added_model_list)],
            'start_quick_ask': [lambda *_: self.get_application().create_quick_ask().present(), ['<primary><alt>a']],
            'model_creator_existing': [lambda *_: Widgets.models.common.prompt_existing(self)],
            'model_creator_gguf': [lambda *_: Widgets.models.common.prompt_gguf(self)],
            'preferences': [lambda *_: Widgets.preferences.PreferencesDialog().present(self), ['<primary>comma']],
            'zoom_in': [lambda *_: Widgets.preferences.zoom_in(), ['<primary>plus']],
            'zoom_out': [lambda *_: Widgets.preferences.zoom_out(), ['<primary>minus']]
        }
        if os.getenv('ALPACA_ENABLE_SCREENSHOT_ACTION', '0') == '1':
            universal_actions['screenshoter'] = [lambda *_: self.prepare_screenshoter(), ['F3']]

        for action_name, data in universal_actions.items():
            self.get_application().create_action(action_name, data[0], data[1] if len(data) > 1 else None)

        def verify_powersaver_mode():
            self.banner.set_revealed(
                Gio.PowerProfileMonitor.dup_default().get_power_saver_enabled() and
                self.settings.get_value('powersaver-warning').unpack() and
                self.get_current_instance().instance_type == 'ollama:managed'
            )
        Gio.PowerProfileMonitor.dup_default().connect("notify::power-saver-enabled", lambda *_: verify_powersaver_mode())
        self.banner.connect('button-clicked', lambda *_: self.banner.set_revealed(False))

        #Chat History
        root_folder = Widgets.chat.Folder(show_bar=False)
        self.chat_list_navigationview.add(root_folder)
        root_folder.update()

        if len(SQL.get_instances()) == 0:
            self.main_navigation_view.replace([Widgets.guide.Guide()])
        else:
            self.main_navigation_view.replace_with_tags(['chat'])

        # Check if EOL flatpak extension is installed, if so tell the user to remove the thing
        if IN_FLATPAK and shutil.which('ollama'):
            Widgets.guide.show_EOL_flatpak_extension_dialog(self)
            Widgets.instances.update_instance_list(
                instance_listbox=self.instance_listbox,
                selected_instance_id=''
            )
        else:
            Widgets.instances.update_instance_list(
                instance_listbox=self.instance_listbox,
                selected_instance_id=self.settings.get_value('selected-instance').unpack()
            )
