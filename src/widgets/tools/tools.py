# tools.py

from gi.repository import GObject, GLib, Gio, Gtk
from .. import activities, dialog, attachments, chat
from ...sql_manager import Instance as SQL, generate_uuid
import os, threading

class Property:
    def __init__(self, name:str, description:str, var_type:str, required:bool=False):
        self.name = name
        self.description = description
        self.var_type = var_type
        self.required = required

class Base(GObject.Object):
    display_name:str = ''
    icon_name:str = 'wrench-wide-symbolic'

    name:str = ''
    description:str = ''
    properties:list = []

    runnable:bool = True
    required_libraries:list = []

    def get_metadata(self) -> dict:
        properties = {}
        required_properties = []
        for p in self.properties:
            properties[p.name] = {
                'type': p.var_type,
                'description': p.description
            }
            if p.required and p.name not in required_properties:
                required_properties.append(p.name)

        metadata = {
            'type': 'function',
            'function': {
                'name': self.name,
                'description': self.description,
                'parameters': {
                    'type': 'object',
                    'properties': properties,
                    'required': required_properties
                }
            }
        }

        return metadata

class NoTool(Base):
    display_name:str = _('No Tool')
    icon_name:str = 'cross-large-symbolic'
    name:str = 'no_tool'
    runnable:bool = False

class AutoTool(Base):
    display_name:str = _('Auto Detect Tool')
    name:str = 'auto_tool'
    runnable:bool = False

class WebSearch(Base):
    display_name:str = _('Web Search')
    icon_name:str = 'globe-symbolic'

    name:str = 'web_search'
    description:str = 'Search for a term online using built-in web browser returning results'
    properties:list = [
        Property(
            name='search_term',
            description='The term to search online, be punctual and use the least possible amount of words to get general results',
            var_type='string',
            required=True
        )
    ]

    def on_search_finish(self, md_text:str):
        self.result = md_text

    def start_work(self, search_term:str, bot_message):
        page = activities.WebBrowser()
        chat_element = bot_message.get_ancestor(chat.Chat)
        activities.show_activity(
            page,
            bot_message.get_root(),
            not chat_element or not chat_element.chat_id
        )
        threading.Thread(target=page.automate_search, args=(self.on_search_finish, search_term, True), daemon=True).start()

    def run(self, arguments, messages, bot_message) -> tuple:
        self.result = 0 # 0=loading | "TEXT"=ok | None=error |
        search_term = arguments.get("search_term", "").strip()
        if not search_term:
            return "Error: Search term was not provided"

        GLib.idle_add(self.start_work, search_term, bot_message)
        while self.result == 0:
            continue

        if self.result:
            return self.result
        return "Error: No results found"

class Terminal(Base):
    display_name:str = _('Terminal')
    icon_name:str = 'terminal-symbolic'

    name:str = 'run_command'
    description:str = 'Run a shell command through the configured terminal connection, asking for confirmation unless automatic execution is enabled'
    properties:list = [
        Property(
            name='command',
            description='The command to run and its parameters',
            var_type='string',
            required=True
        ),
        Property(
            name='explanation',
            description='Explain in simple words what the command will do to the system, be clear and honest',
            var_type='string',
            required=True
        )
    ]

    required_libraries:list = ['gi.repository.Vte']

    global_page = None
    cancel_event = None
    connection = None
    automatic_execution = None

    def wait_for(self, event):
        if self.cancel_event is None:
            event.wait()
            return True
        while not event.wait(0.05):
            if self.cancel_event.is_set():
                return False
        return not self.cancel_event.is_set()

    def cancel(self):
        if self.global_page:
            GLib.idle_add(self.global_page.on_close)
        if getattr(self, 'confirmation_dialog', None):
            GLib.idle_add(self.confirmation_dialog.force_close)

    def run(self, arguments, messages, bot_message) -> str:
        self.last_exit_code = None
        self.last_output = ''
        self.last_cancelled = False
        self.confirmation_dialog = None
        if self.cancel_event is not None and self.cancel_event.is_set():
            self.last_cancelled = True
            return 'Command canceled'
        if not arguments.get('command'):
            return "Error: No command was provided"

        settings = Gio.Settings(schema_id="com.jeffser.Alpaca")
        error = None
        confirmed = threading.Event()
        automatic = settings.get_boolean('activity-terminal-auto-execute') if self.automatic_execution is None else self.automatic_execution
        self.current_command = arguments.get('command') if automatic else ""

        def dialog_confirmation(cmd:str, confirmation:bool):
            self.current_command = cmd if confirmation else ""
            confirmed.set()

        options = {
            _('Cancel'): {
                'callback': lambda cmd: dialog_confirmation(cmd, False)
            },
            _('Run'): {
                'appearance': 'suggested',
                'callback': lambda cmd: dialog_confirmation(cmd, True),
                'default': True
            }
        }
        def show_confirmation():
            nonlocal error
            try:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    confirmed.set()
                    return
                entry_dialog = dialog.Entry(
                    heading=_("Command Execution"),
                    body=arguments.get('explanation'),
                    close_response=list(options.keys())[0],
                    options=options,
                    entries={
                        'placeholder': _("Command"),
                        'css': ['black_background'],
                        'text': arguments.get('command')
                    }
                )
                self.confirmation_dialog = entry_dialog
                entry_dialog.show(bot_message.get_root())
            except Exception as e:
                error = 'Error: Could not confirm the command: {}'.format(e)
                confirmed.set()

        if not self.current_command:
            GLib.idle_add(show_confirmation)
            if not self.wait_for(confirmed):
                self.last_cancelled = True
                self.cancel()
                return 'Command canceled'

        if error:
            return error
        if not self.current_command:
            return 'The user chose not to execute the command'

        finished = threading.Event()
        result_text = ""

        def on_finish():
            nonlocal result_text, error
            try:
                result_text = self.global_page.get_text().strip('\n')
                self.last_output = result_text
                self.last_cancelled = getattr(self.global_page, 'closed', False) is True
                code = getattr(self.global_page, 'exit_code', None)
                self.last_exit_code = code if type(code) is int else None
            except Exception as e:
                error = 'Error: Could not read the command output: {}'.format(e)
            finally:
                finished.set()

        def start_command():
            nonlocal error
            try:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    finished.set()
                    return
                if not self.global_page or not self.global_page.get_root():
                    self.global_page = activities.Terminal(
                        language='auto',
                        code_getter=lambda: self.current_command,
                        close_callback=on_finish
                    )
                    chat_element = bot_message.get_ancestor(chat.Chat)
                    activities.show_activity(self.global_page, bot_message.get_root(), not chat_element or not chat_element.chat_id)
                self.global_page.close_callback = on_finish
                self.global_page.connection = self.connection
                self.global_page.reset(True, True)
                self.global_page.run()
            except Exception as e:
                error = 'Error: Could not execute the command: {}'.format(e)
                finished.set()

        GLib.idle_add(start_command)
        if not self.wait_for(finished):
            self.last_cancelled = True
            self.cancel()
            return 'Command canceled'

        if error:
            return error
        if not result_text:
            return "The command did not return any text"

        return '```\n{}\n```'.format(result_text)

class BackgroundRemover(Base):
    display_name:str = _('Background Remover')
    icon_name:str = 'image-missing-symbolic'

    name:str = 'background_remover'
    description:str = 'Requests the user to upload an image and automatically removes its background'

    required_libraries:list = ['rembg']

    def get_latest_image(self, messages, root) -> str:
        messages.reverse()
        for message in messages:
            if len(message.get('images', [])) > 0:
                return message.get('images')[0]
        self.image_requested = 0

        def on_attachment(file:Gio.File, remove_original:bool=False):
            if not file:
                self.image_requested = None
                return
            self.image_requested = attachments.extract_image(file.get_path(), root.settings.get_value('max-image-size').unpack())

        file_filter = Gtk.FileFilter()
        file_filter.add_pixbuf_formats()
        dialog.simple_file(
            parent = root,
            file_filters = [file_filter],
            callback = on_attachment
        )

        while self.image_requested == 0:
            continue

        return self.image_requested

    def on_save(self, data:str, bot_message):
        if data:
            attachment = bot_message.add_attachment(
                file_id=generate_uuid(),
                name=_('Output'),
                attachment_type='image',
                content=data
            )
            SQL.insert_or_update_attachment(bot_message, attachment)
            self.status = 1
        else:
            self.status = 2

    def on_close(self):
        self.status = 2

    def run(self, arguments, messages, bot_message) -> tuple:
        threading.Thread(target=bot_message.update_message, args=(_('Loading Image...') + '\n',), daemon=True).start()
        image_b64 = self.get_latest_image(messages, bot_message.get_root())
        if image_b64:
            self.status = 0 # 0 waiting, 1 finished, 2 canceled / empty image
            page = activities.BackgroundRemover(
                save_func=lambda data, bm=bot_message: self.on_save(data, bm),
                close_callback=self.on_close
            )
            chat_element = bot_message.get_ancestor(chat.Chat)
            GLib.idle_add(
                activities.show_activity,
                page,
                bot_message.get_root(),
                not chat_element or not chat_element.chat_id
            )
            page.load_image(image_b64)

            while self.status == 0:
                continue

            if self.status == 1:
                return "Background removed successfully"
            else:
                return "An error occurred"
        else:
            return "Error: User didn't attach an image"
        return "Error: Couldn't remove the background"
