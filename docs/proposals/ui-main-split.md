# Proposal: splitting ui.py and main.py

Status: a proposal. Nothing here is done. `ui.py` (5,434 lines) and
`main.py` (2,958) are the two files over the 2,000-line budget, and
`tests/core/test_repo_hygiene.py` holds them at those lengths: they may
shrink, never grow.

## Why it was not done in the reorganisation

Neither file can be imported where the reorganisation ran. PyQt6, numpy,
sounddevice and google-genai are not installed there, so even
`QT_QPA_PLATFORM=offscreen python -c "import ui"` fails at the first
import. A split that nothing has imported is a guess, and these two files
are the live voice path and the whole HUD. The cuts below are where to
start; each needs the checks under "How to test a cut" before it is made.

## ui.py: the seams

Lowest risk first. `ui.py` would become a package, `ui/`, whose
`__init__.py` re-exports `JarvisUI` so `from ui import JarvisUI` in
`main.py` does not change.

| Cut | Lines today | What moves | Watch for |
|---|---|---|---|
| `ui/theme.py` | 38–177 | `_base_dir`, `_read_full_config`, the palette class `C`, `apply_ui_accent`, `current_palette`, `retheme_all_widgets`, `qcol` | `C` is shared, mutable state: every widget reads it at paint time and `apply_ui_accent` rewrites it. It must stay one object, imported by name, never copied. |
| `ui/metrics.py` | 185–373 | `_nvml_gpu_windows`, `_SysMetrics` | Windows-only GPU probe; nothing here can run it. |
| `ui/widgets.py` | 378–1296, 2345–2428 | `HudCanvas` (537 lines), `MetricBar`, `LogWidget`, `FileDropZone`, `_DropCanvas`, `_CameraPreview`, `HueWheel`, `ClipboardPanel`, `_file_category`, `_fmt_size` | `HudCanvas` draws the avatar through `core/avatar.py`; keep the import order. |
| `ui/overlays.py` (or one per overlay) | 1366–2912 | `SetupOverlay`, `CustomizeOverlay`, `PluginManagerOverlay`, `_HudOverlay`, `ConfirmBanner`, `AudioDeviceOverlay`, `MemoryOverlay`, `PluginSettingsOverlay`, `RemoteKeyOverlay` | `ConfirmBanner` is the UI end of the confirmation gate (`core/confirm.py`): its signals must stay wired exactly as they are. |
| `MainWindow` mixins | 2915–5200 | 79 methods in one class. Group them into mixins that `MainWindow` inherits: camera (`_cam_loop`, `start_camera_stream`, ...), desktop integration (`_create_desktop_shortcut`, `_toggle_autostart`, ...), the quiz panel (`_build_quiz_panel`, `_quiz_*`), the panels and drawer builders (`_build_header`, `_build_left_panel`, ...), talk controls (wake word, push-to-talk, mute), and the overlay openers. | The riskiest cut: Qt signal connections made in `__init__` (179 lines) refer to methods by name, and object lifetimes depend on parents. Mixins keep `self`, so the method names do not change, but each mixin needs the attributes `__init__` creates. |
| stays in `ui/__init__.py` | 5203–5434 | `JarvisUI`, `_RootShim` | The facade `main.py` talks to. |

## main.py: the seams

| Cut | Lines today | What moves | Watch for |
|---|---|---|---|
| `core/tool_declarations.py` | 343–510 | `TOOL_DECLARATIONS` (data) | Byte-compare the list before and after, as was done for the Minecraft TOOL. |
| `core/live_prompt.py` | 238–318 | `_describe_tools`, `_describe_limits`, `_render_prompt`, `_get_api_key`, `_load_system_prompt` | The prompt is assembled from the live system; compare the rendered prompt before and after. |
| `core/live_audio.py` | 116–235, 327–341 | `_pcm_level`, `_pcm_visemes`, `_is_repeat_chunk`, `_clean_transcript` | Pure functions; unit-testable once numpy is installed. |
| `JarvisLive` mixins | 568–2941 | 57 methods in one class, by concern: the **voice pipeline** (`_listen_audio`, `_enqueue_audio`, `_send_realtime`, `_receive_audio`, `_play_audio`, `set_speaking`, push-to-talk, wake/sleep, `_report_voice_state`, `_run_voice_watchdog`); the **session loop** (`run`, `_build_config`, `_tuning_config`, `_handle_server_message`, reconnect and go-away handling, `_send_startup_briefing`); **tool dispatch** (`_execute_tool`, `_start_tool_calls`, `_run_tool_calls`, cancellation, `interrupt`, `_interrupt_now`, `_stop_running_actions`); **shutdown** (`_shutdown`, `_save_session_summary`); **background work** (system monitor, background monitor, proactive mode, the phone dashboard). | `tests/voice/` does not import `main.py` (it cannot: audio devices). It parses `main.py` and lifts `_enqueue_audio`, `_report_voice_state` and `_on_text_command` out of `JarvisLive` by name (`tests/support/voice_paths.py`, `_lift`). A method that moves to a mixin must still be found there: change `_lift` to read the new module in the same commit. |
| stays in `main.py` | 2943–2955 | `main()`, the imports that wire it together | `run_jarvis.bat` and the readme start `main.py`; the entry point does not move. |

## How to test a cut

For every cut, in its own commit:

1. **Verbatim.** Every top-level statement and every method appears exactly
   once in the new set of files, with no comment line lost -- the check
   the Minecraft splits used (by script, over the AST).
2. **Import smoke test** with the app's dependencies installed:
   `QT_QPA_PLATFORM=offscreen python -c "import ui; import main"` on
   Linux, and the same on Windows. On Windows, also construct `JarvisUI`
   with the offscreen platform and close it.
3. **The suite** -- pytest and `unittest discover -s tests -t .` -- on
   3.11 to 3.14, with the same counts as before.
4. **Lower the ceiling** in `OVER_BUDGET` to the new length.
5. **By hand, on Windows:** the HUD renders and the avatar moves; each
   overlay opens and closes; the confirmation banner appears for a
   shutdown request and the action runs only after CONFIRM; the camera
   preview; wake word and push-to-talk; a reconnect (pull the network);
   and closing the app leaves no key held in Minecraft.

## What cannot be tested in a headless container

The HUD's rendering, Qt signal timing, audio input and output devices, the
Gemini Live session and reconnection, the camera, the wake-word engine,
and everything Windows-only: the desktop shortcut, autostart, the NVML GPU
probe, global push-to-talk. The import smoke test needs the dependencies;
the rest needs a person at a Windows machine.
