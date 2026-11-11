Create a Python development environment with the system GObject bindings available:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install PyICU 'ollama==0.6.1' 'openai==1.84.0' pydbus html2text odfpy markitdown zstandard pandas
.venv/bin/python -m unittest discover -s tests -v
```

The unit tests use Python's standard library and replace GTK widgets and application
dependencies. They exercise the actual terminal modules, shell quoting, confirmation,
automatic execution, output capture, and terminal reuse without a graphical display.

To compile and validate the application resources, install the system Meson, Ninja,
Blueprint compiler (>= 0.18.0), GTK4, libadwaita, GtkSourceView 5, VTE GTK4, PyGObject,
gettext, desktop-file-utils, and AppStream development tools. Running the whole app
also requires the system WebKit 6.0, libspelling 1, GStreamer, libportal, Cairo,
Pillow, NumPy, BeautifulSoup, requests, and Matplotlib packages. Then run:

```sh
meson setup build --prefix "$PWD/build/install"
meson compile -C build
meson test -C build --print-errorlogs
glib-compile-schemas --strict data --targetdir=build/data
```

For a local installation under `build/install`, run `meson install -C build`, then
launch it with:

```sh
GSETTINGS_SCHEMA_DIR="$PWD/build/install/share/glib-2.0/schemas" \
  .venv/bin/python build/install/bin/alpaca
```

Optional speech and image models are not needed to develop or test the terminal.

Live integration tests require a graphical display and an SSH machine with key-based
authentication, passwordless sudo, and Ubuntu 26.04. They run the actual tool and VTE
runner on the GTK main loop, including `sudo apt-get update`, which refreshes package
indexes on that machine. They do not require an Ollama instance. To opt in:

```sh
GSETTINGS_SCHEMA_DIR="$PWD/build/data" GSETTINGS_BACKEND=memory \
  ALPACA_SSH_TEST_TARGET=ubuntu@10.52.171.194 \
  .venv/bin/python -m unittest discover -s tests -p test_terminal_ssh.py -v
```

Use `ALPACA_TEST_BUILD_DIR` if the Meson build directory differs from `build`. The
tests bypass the local SSH configuration, disable password prompts, and store known
hosts in a temporary directory. GSettings uses the memory backend so tests do not
change saved preferences.

In Alpaca, enable **Preferences → Activities → Terminal → Execute Commands
Automatically** to bypass terminal tool confirmation. The setting is off by default
and applies to both direct and SSH connections. It can also be set with:

```sh
gsettings set com.jeffser.Alpaca activity-terminal-auto-execute true
```

An installed application needs the updated schema and compiled UI resources before
the new preference can be used.

Enable **Preferences → General → Queue One Message While Generating** to queue one
waiting message per chat while a reply is running, keeping the model,
instance, tools, and attachments selected when they were queued. Enter or the Queue
Message button adds a prompt; Shift+Enter still inserts a newline. The composer
shows the full queued text with **Edit** and **Clear** buttons. Edit removes it from
the queue and restores its text and attachments to the composer. Send again to
queue the edited message with the current selections. Replacing a composer draft
through Edit is undoable with the normal text-view Undo. If the slot is occupied,
another send preserves the draft and attachments. Stop, generation errors, or disabling
the setting cancels pending prompts. Queues are held in memory until they run and do
not survive closing the application. The setting is off by default and works in the
main chat window and Quick Ask.

To check queueing against a real Ollama endpoint with the GTK interface, first build
and locally install the app as described above, then run:

```sh
ALPACA_QUEUE_TEST_OLLAMA=https://your-ollama-host:11434 \
  ALPACA_QUEUE_TEST_MODEL=qwen3.5:2b \
  .venv/bin/python -W ignore::DeprecationWarning tests/queue_smoke.py
```

This runs an initial response and an edited queued response, checks that an unsent draft is preserved,
checks preference bindings and Stop, and uses temporary data and in-memory settings.
It also opens the actual Quick Ask window and checks a live reply while the GTK
main loop continues handling events.

To check startup and preferences while model loading is blocked, and simulate a
failed response stream using real GTK dialogs, run:

```sh
.venv/bin/python -W ignore::DeprecationWarning tests/ui_smoke.py
```

This needs access to a graphical display but uses no network connection. It checks
that model widgets and error dialogs are handled on the main thread, that partial
responses survive a stream error, and that a failed generation cancels the queue.
It also uses temporary data and in-memory settings.
The GTK checks also restore chat history containing concurrent image requests,
hold both image and model metadata requests open, and verify that Quick Ask remains
usable until those requests finish.

The **Alpaca Quick Ask** desktop launcher opens the small chat window directly from
GNOME's application search. Alpaca's main desktop launcher also has a **Quick Ask**
action in its context menu. For a global shortcut, open **GNOME Settings → Keyboard
→ View and Customize Shortcuts → Custom Shortcuts**, add a shortcut named
**Alpaca Quick Ask**, and use `alpaca --quick-ask` as the command (or the absolute
path to the installed executable when it is outside the desktop session's PATH).
Assign **Ctrl+Alt+A** or another free combination. This works while other apps are
focused; the same shortcut inside Alpaca remains available without GNOME setup.

Agent tasks are described in [the workflow guide](../docs/agent-workflow-plan.md).
Enable **Preferences → General → Agent Tasks → Agent Mode** with an Ollama
model that supports tools. Plan approval and terminal command approval are
independent settings. Unfinished saved tasks restore paused and require Resume.

The agent controller tests cover phase gates, argument validation, failure
recovery, limits within tool batches, deadlines, cancellation, and interrupted
tool protocol recovery. To exercise the actual GTK controls, manual terminal
confirmation, FIFO goals, Quick Ask saving, and real VTE process cancellation:

```sh
.venv/bin/python -W ignore::DeprecationWarning tests/agent_smoke.py
```

For a live model-driven clarification → plan → SSH execution → verification run:

```sh
ALPACA_AGENT_TEST_OLLAMA=https://your-ollama-host:11434 \
  ALPACA_AGENT_TEST_MODEL=agent-fast:latest \
  ALPACA_SSH_TEST_TARGET=ubuntu@your-container \
  .venv/bin/python -W ignore::DeprecationWarning tests/agent_smoke.py
```

This checks `whoami`, `sudo -n whoami`, and Ubuntu `VERSION_ID=26.04`. It uses
temporary data and memory settings. Model behavior is nondeterministic; failures
print the saved controller state and actual terminal output for diagnosis.

To exercise native application startup and shutdown in three fresh processes:

```sh
.venv/bin/python tests/shutdown_smoke.py
```

This opens preferences and Quick Ask, closes through the normal window action,
and collects garbage before each process exits. Document converters load only
when conversion is requested, so startup and preferences do not initialize their
optional native inference runtime.
