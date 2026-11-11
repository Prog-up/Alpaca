"""Dispatch UI-only callbacks from workers onto GTK's main thread."""

from functools import wraps
import threading
from gi.repository import GLib


def on_main_thread(function):
    @wraps(function)
    def dispatch(*args, **kwargs):
        if threading.current_thread() is threading.main_thread():
            return function(*args, **kwargs)

        def run():
            function(*args, **kwargs)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(run)
    return dispatch
