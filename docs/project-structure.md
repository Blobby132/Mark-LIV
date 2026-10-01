# 🗂️ Project Structure

```
Mark LIV/
├── main.py                   # Core loop — Gemini Live session, audio I/O, viseme extraction, tool dispatch
├── ui.py                     # PyQt6 HUD — avatar canvas, waveform, log panel, settings drawer, camera feed
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies, checks your Python)
├── run_jarvis.bat            # Windows launcher — finds Python, installs what is missing, runs the doctor on a crash
├── install_mod.bat           # Copies the Minecraft bridge mod into your mods folder
├── doctor.bat, bridge_check.bat, gameplay_check.bat   # Windows shortcuts to the scripts in tools/
├── pyproject.toml            # Test and lint settings only (no packaging)
├── .gitignore                # Keeps your API key, TLS key and memories out of the repository
├── .gitattributes            # Line endings: LF in the repository, CRLF for .bat files
├── plugins/
│   ├── quiz.py               # Interactive quiz — JARVIS writes the questions, you answer on screen
│   ├── document_review.py    # Contracts and policies in plain language, ordered by what matters
│   ├── _google_core.py       # Shared OAuth for the Gmail/Calendar plugins (not a plugin itself)
│   ├── _printer_core.py      # Shared printer connectivity (not a plugin itself)
│   ├── _template.py          # Copy this to write a new plugin — one file, drop in, done
│   └── ...                   # Drop-in skills (each self-describes via a PLUGIN dict + run())
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
│   └── desktop.py            # Desktop and taskbar control
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── long_term.json        # Persistent store — created on first run
├── core/
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
│   └── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
├── minecraft/                # Plays Minecraft Java inside a bounded session — see docs/ARCHITECTURE.md
│   └── skills/               # What a Minecraft task does: navigate, collect, eat, craft, build, combat
├── dashboard/                # The phone dashboard (HTTPS, encrypted)
├── tools/                    # Doctor, bridge and gameplay checks, the mod installer
├── fabric-mod/               # Source of the read-only Minecraft bridge mod
├── mods/                     # The built bridge mod jar, committed so nobody needs a JDK
├── docs/                     # Everything not in the readme — start at docs/README.md
├── tests/                    # minecraft/, bridge/, core/, voice/, dashboard/, and support/ for shared fixtures
└── config/
    ├── api_keys.json         # API key, name, voice, colour, toggles — created on first launch (git-ignored)
    └── certs/                # Self-signed TLS pair for the phone dashboard — generated locally (git-ignored)
```

