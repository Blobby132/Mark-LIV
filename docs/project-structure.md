# 🗂️ Project Structure

What is in the repository, generated from `git ls-files`. Every path in the
tree below exists — `tests/core/test_repo_hygiene.py` checks it. The
Minecraft subsystem is mapped module by module in
[ARCHITECTURE.md](ARCHITECTURE.md).

```
Mark LIV/
├── main.py                   # Core loop — Gemini Live session, audio I/O, viseme extraction, tool dispatch
├── ui.py                     # PyQt6 HUD — avatar canvas, waveform, log panel, settings drawer, camera feed
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies, checks your Python)
├── requirements.txt          # Python dependencies, with per-OS markers
├── run_jarvis.bat            # Windows launcher — finds Python, installs what is missing, runs the doctor on a crash
├── install_mod.bat           # Copies the Minecraft bridge mod into your mods folder
├── doctor.bat                # Windows shortcut to tools/doctor.py
├── bridge_check.bat          # Windows shortcut to tools/bridge_check.py
├── gameplay_check.bat        # Windows shortcut to tools/minecraft_gameplay_check.py
├── pyproject.toml            # Test and lint settings only (no packaging)
├── readme.md                 # What Jarvis is, install, run, safety in brief — start here
├── LICENSE                   # Creative Commons BY-NC 4.0
├── .gitignore                # Keeps your API key, TLS key and memories out of the repository
├── .gitattributes            # Line endings: LF in the repository, CRLF for .bat files
├── plugins/                  # Drop-in skills — each self-describes via a PLUGIN dict + run()
│   ├── __init__.py
│   └── _template.py          # Copy this to write a new plugin — one file, drop in, done
├── actions/                  # Bundled skills — each self-describes via a TOOL dict + handler
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen & webcam capture for vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power (per-OS)
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher (per-OS name map)
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Developer task agent
│   ├── desktop.py            # Desktop and taskbar control
│   ├── minecraft.py          # The one tool that reaches the Minecraft subsystem
│   └── _minecraft_text.py    # The minecraft_control tool's long text (not an action itself)
├── memory/
│   ├── __init__.py
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
├── core/
│   ├── __init__.py
│   ├── prompt.txt            # All prompt wording — {tokens} are filled from the live system at startup
│   ├── avatar.py             # Avatar renderer — lighting, pose, expression, mouth (QPainter)
│   ├── avatar_mesh.py        # Head geometry — loads the face, generates skull/neck/rigs
│   ├── face_model.obj        # The face itself (MediaPipe canonical model, Apache-2.0, 25 KB)
│   ├── viseme.py             # Transcript → mouth shapes, fused with the audio's timing
│   ├── echo.py               # Tells your voice from the assistant's own echo; self-calibrating
│   ├── hotkey.py             # Push-to-talk chord — global on Windows, windowed fallback elsewhere
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   ├── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── action_loader.py      # Bundled-action engine — the built-in twin of plugin_loader
│   ├── capabilities.py       # The one permission table: every capability and its verdict
│   ├── permissions.py        # The broker that applies it to every tool call
│   ├── audit.py              # What was allowed, refused or confirmed, and why
│   ├── exec_safe.py          # The only way anything here starts a process
│   ├── safe_path.py          # Which paths file actions may touch
│   ├── voice_diagnostics.py  # Where your speech went: the `voice check` timeline
│   ├── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
│   ├── gemini.py             # One place where the assistant's one-shot Gemini calls are made
│   ├── installer.py          # Dependency auto-installer
│   ├── interrupts.py         # "stop" for things that are running, without the model
│   ├── llm_client.py         # Local LLM client
│   ├── ocr.py                # Optional text extraction from an image
│   ├── stt.py                # Speech-to-Text engines
│   └── tts.py                # Text-to-Speech engines
├── config/
│   ├── __init__.py
│   └── jarvis.ico            # The desktop-shortcut icon (ui.py rebuilds it if missing)
├── minecraft/                # Plays Minecraft Java inside a bounded session — see docs/ARCHITECTURE.md
│   └── skills/               # What a Minecraft task does: navigate, collect, eat, craft, build, combat
├── dashboard/                # The phone dashboard (HTTPS, encrypted)
│   ├── __init__.py
│   ├── server.py             # The dashboard's local HTTP server
│   └── static/               # Its two pages, app.html and login.html
├── tools/                    # Run by hand or by the .bat shortcuts
│   ├── bridge_check.py       # Is the companion mod talking to Jarvis?
│   ├── doctor.py             # Startup doctor — the first thing that fails, and the fix
│   ├── f3_check.py           # Can Jarvis read your Minecraft screen (the F3 route)?
│   ├── install_mod.py        # Put the bridge mod where Minecraft will find it
│   ├── minecraft_gameplay_check.py  # The gameplay checks that need a real game
│   └── minecraft_manual_check.py  # The interactive check that needs a real Minecraft
├── fabric-mod/               # Source of the read-only Minecraft bridge mod
├── mods/
│   └── markliv-bridge-1.0.0.jar  # The built bridge mod, committed so nobody needs a JDK
├── docs/                     # Everything not in the readme — start at docs/README.md
└── tests/
    ├── minecraft/            # The Minecraft agent
    ├── bridge/               # The bridge mod and its reader
    ├── core/                 # Permissions, capabilities, audit, installers, hygiene
    ├── voice/                # The voice path
    ├── dashboard/            # The phone dashboard
    ├── support/              # Shared fixtures — simulated worlds, fakes, paths
    └── __init__.py
```

## Created on first run (not in the repository)

These are made by the app on your machine and are git-ignored, so a clone
does not have them:

| Path | What |
|---|---|
| `config/api_keys.json` | API key, name, voice, colour, toggles — created on first launch |
| `config/certs/` | Self-signed TLS pair for the phone dashboard — generated locally |
| `memory/long_term.json` | Persistent store — sessions, monitors, identity |

Plugins you install go in `plugins/` beside `_template.py`.
