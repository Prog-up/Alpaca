"""Load terminal modules without importing Alpaca's optional AI/media dependencies."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class Template:
    def __init__(self, **kwargs):
        pass

    def __call__(self, cls):
        return cls

    Child = staticmethod(Mock)
    Callback = staticmethod(lambda: lambda function: function)


def load_modules(data_dir, real_gi=False, include_queue=False):
    prefix = 'alpaca_terminal_tests'
    modules = {}
    for name in ['', '.widgets', '.widgets.activities', '.widgets.tools',
                 '.widgets.dialog', '.widgets.attachments', '.widgets.message',
                 '.widgets.chat', '.sql_manager', '.constants']:
        module = ModuleType(prefix + name)
        module.__path__ = []
        modules[module.__name__] = module

    constants = modules[prefix + '.constants']
    constants.data_dir = str(data_dir)
    constants.IN_FLATPAK = False
    sql = modules[prefix + '.sql_manager']
    sql.Instance = Mock()
    sql.generate_uuid = Mock()
    modules[prefix + '.widgets.chat'].Chat = object

    if not real_gi:
        gi = ModuleType('gi')
        repository = ModuleType('gi.repository')
        repository.Gtk = SimpleNamespace(Template=Template, Stack=object, ScrolledWindow=object)
        class TerminalStub:
            connect = Mock()
        repository.Vte = SimpleNamespace(Terminal=TerminalStub, Pty=Mock(), PtyFlags=SimpleNamespace(DEFAULT=0))
        repository.GLib = SimpleNamespace(idle_add=Mock(side_effect=lambda function, *args: function(*args)),
                                          SpawnFlags=SimpleNamespace(DEFAULT=0), Error=RuntimeError)
        repository.Gio = SimpleNamespace(Settings=Mock())
        repository.GObject = SimpleNamespace(Object=object)
        for name in ['Pango', 'Gdk', 'Adw', 'GtkSource']:
            setattr(repository, name, SimpleNamespace())
        gi.repository = repository
        modules['gi'] = gi
        modules['gi.repository'] = repository

    with patch.dict(sys.modules, modules):
        loaded = []
        sources = [('src/widgets/activities/terminal.py', '.widgets.activities.terminal'),
                   ('src/widgets/tools/tools.py', '.widgets.tools.tools')]
        if include_queue:
            sources.append(('src/widgets/prompt_queue.py', '.widgets.prompt_queue'))
        for path, name in sources:
            spec = importlib.util.spec_from_file_location(prefix + name, ROOT / path)
            module = importlib.util.module_from_spec(spec)
            module.__dict__['_'] = lambda text: text
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
        loaded[1].activities.Terminal = loaded[0].Terminal
    return loaded
