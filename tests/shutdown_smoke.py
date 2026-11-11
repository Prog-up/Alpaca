"""Check native application lifecycle and interpreter shutdown in fresh processes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def child():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='alpaca-shutdown-test-') as directory:
        os.environ['GSETTINGS_SCHEMA_DIR'] = str(root / 'build/data')
        os.environ['GSETTINGS_BACKEND'] = 'memory'
        for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'):
            os.environ[key] = str(Path(directory) / key)
        import gc
        import gettext
        from unittest.mock import patch
        gettext.install('alpaca')
        sys.path.insert(0, str(root / 'build/install/share/Alpaca'))
        from gi.repository import Gio
        resource = Gio.Resource.load(str(root / 'build/src/alpaca.gresource'))
        resource._register()
        from alpaca import main
        from alpaca.sql_manager import Instance as SQL
        from alpaca.widgets import preferences
        from alpaca.widgets.instances.ollama_instances import BaseInstance
        from gi.repository import GLib
        settings = Gio.Settings.new('com.jeffser.Alpaca')
        settings.set_string('selected-instance', 'shutdown-test')
        settings.set_boolean('hide-on-close', False)
        SQL.insert_or_update_instance('shutdown-test', False, 'ollama',
                                      {'name': 'Shutdown Test', 'url': 'http://unused', 'default_model': 'test-model'})
        sys.argv = [sys.argv[0]]
        errors = []
        def activate(app, service=None):
            window = app.get_main_window()
            window.present()
            def preferences_open():
                prefs = preferences.PreferencesDialog()
                prefs.present(window)
                def quick_open():
                    prefs.force_close()
                    quick = app.create_quick_ask()
                    quick.present()
                    def close():
                        quick.close()
                        if 'onnxruntime' in sys.modules:
                            errors.append('Document conversion runtime was loaded at startup')
                        window.close()
                        return GLib.SOURCE_REMOVE
                    GLib.timeout_add(300, close)
                    return GLib.SOURCE_REMOVE
                GLib.timeout_add(300, quick_open)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(300, preferences_open)
        with patch.object(BaseInstance, 'get_local_models', return_value=[{'name': 'test-model', 'details': {}, 'capabilities': []}]), \
                patch.object(BaseInstance, 'get_available_models', return_value={}), \
                patch.object(main.AlpacaApplication, 'run_arguments', activate):
            app = main.AlpacaApplication('9.2.5')
            app.set_application_id('com.jeffser.Alpaca.ShutdownTest')
            app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)
            assert app.run([]) == 0
            assert not errors, errors
        gc.collect()
        print('Native startup → preferences → Quick Ask → close → garbage collection: PASS', flush=True)


if __name__ == '__main__':
    if '--child' in sys.argv:
        child()
    else:
        for _ in range(3):
            subprocess.run([sys.executable, '-X', 'faulthandler', '-W', 'ignore::DeprecationWarning',
                            str(Path(__file__).resolve()), '--child'], check=True, timeout=30)
