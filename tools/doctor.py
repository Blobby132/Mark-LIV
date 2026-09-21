"""
MARK LIV — startup doctor.

Answers one question: why did the window close?

On Windows, double-clicking a .py file opens a console, runs it, and closes the
console the instant the process exits. If the process exits because of an
uncaught exception, the traceback is printed into a window that is already
gone. The user sees a flash and nothing else, which is the least actionable
failure mode there is.

This script does what that traceback would have done, only it survives long
enough to be read: it imports the things main.py imports, in the same order,
and reports the first one that fails along with the actual error and the
command that fixes it.

Deliberately dependency-free: it imports nothing outside the standard library
at module level, because the entire premise is that the dependencies might be
what is broken. Output is plain ASCII for the same reason — a legacy Windows
code page cannot render a check mark, and a UnicodeEncodeError inside the
diagnostic would be a poor joke.
"""

from __future__ import annotations

import importlib
import os
import platform
import sys
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

OK, FAIL, WARN, INFO = "[ ok ]", "[FAIL]", "[warn]", "      "

# Imports that main.py performs at module level. If one of these raises, the
# app dies before a window is ever created -- which is exactly the symptom
# this script exists to explain. (package_to_import, pip_name, what_it_is)
CRITICAL = [
    ("PyQt6.QtWidgets", "PyQt6",         "the window and HUD"),
    ("numpy",           "numpy",         "audio buffers and the avatar"),
    ("sounddevice",     "sounddevice",   "microphone and speaker I/O"),
    ("google.genai",    "google-genai",  "the Gemini Live connection"),
    ("psutil",          "psutil",        "process and system readouts"),
]

# Imported lazily, at the point of use, inside try/except. A missing one costs
# exactly one feature and never stops the app -- so these are warnings.
OPTIONAL = [
    ("playwright",  "playwright",     "browser automation"),
    ("cv2",         "opencv-python",  "camera and vision"),
    ("mss",         "mss",            "screen capture"),
    ("PIL",         "pillow",         "image handling"),
    ("requests",    "requests",       "web search"),
    ("fastapi",     "fastapi",        "the phone dashboard"),
    ("pyautogui",   "pyautogui",      "desktop input control"),
    ("pygetwindow", "pygetwindow",    "window focus (Minecraft needs this)"),
    ("pytesseract", "pytesseract",    "reading Minecraft's F3 overlay"),
]

# Files that must exist for this to be a complete checkout. Chosen to also
# catch the specific mistake of downloading the wrong branch: the core/ safety
# modules and minecraft/ exist only on the security branch.
REQUIRED_FILES = [
    ("main.py",                  "the app itself"),
    ("ui.py",                    "the HUD"),
    ("requirements.txt",         "the dependency list"),
    ("core/face_model.obj",      "the avatar mesh"),
    ("core/prompt.txt",          "the system prompt"),
    ("memory/memory_manager.py", "long-term memory"),
]

BRANCH_MARKERS = [
    ("core/permissions.py",  "the permission broker"),
    ("core/capabilities.py", "the capability policy table"),
    ("core/audit.py",        "the audit log"),
    ("minecraft/controller.py", "the Minecraft subsystem"),
]


def _rule(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 58 - len(title)))


def _try_import(name: str):
    """Return None on success, or (kind, message) describing the failure.

    The distinction matters. A package that is absent is fixed by installing
    it; a package that is present but will not load is a broken install or a
    missing system library, and telling someone to re-run pip for that sends
    them round a loop that cannot terminate.
    """
    try:
        importlib.import_module(name)
        return None
    except ModuleNotFoundError as exc:
        # A missing *sub*-dependency is a broken install, not an absent package.
        root = name.split(".")[0]
        if getattr(exc, "name", None) in (name, root):
            return ("absent", f"{type(exc).__name__}: {exc}")
        return ("broken", f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        return ("broken", f"{type(exc).__name__}: {exc}")


def check_interpreter() -> list[str]:
    _rule("Python")
    problems: list[str] = []
    v = sys.version_info
    bits = 64 if sys.maxsize > 2**32 else 32

    print(f"{INFO} version    : {v.major}.{v.minor}.{v.micro} ({bits}-bit)")
    print(f"{INFO} executable : {sys.executable}")
    print(f"{INFO} OS         : {platform.system()} {platform.release()}")

    if (v.major, v.minor) < (3, 11):
        print(f"{FAIL} Python {v.major}.{v.minor} is too old -- 3.11 is the minimum.")
        problems.append(
            "Install Python 3.11-3.14 from python.org and tick "
            '"Add Python to PATH" on the first installer screen.'
        )
    elif (v.major, v.minor) > (3, 14):
        print(f"{WARN} Python {v.major}.{v.minor} is newer than 3.14, the newest tested.")
    else:
        print(f"{OK} Python {v.major}.{v.minor} is supported.")

    if bits == 32:
        print(f"{FAIL} This is 32-bit Python. PyQt6 ships no 32-bit wheels.")
        problems.append(
            "Uninstall 32-bit Python and install the 64-bit build from python.org."
        )
    return problems


def check_checkout() -> list[str]:
    _rule("Files")
    problems: list[str] = []

    missing = [(f, why) for f, why in REQUIRED_FILES if not (REPO / f).exists()]
    for f, why in missing:
        print(f"{FAIL} missing {f}  ({why})")
    if missing:
        problems.append(
            "This folder is not a complete copy of MARK LIV. Re-download it."
        )
    else:
        print(f"{OK} All core files present.")

    absent = [(f, why) for f, why in BRANCH_MARKERS if not (REPO / f).exists()]
    if absent:
        print(f"{WARN} This copy predates the security work -- missing:")
        for f, why in absent:
            print(f"{INFO}   {f}  ({why})")
        print(f"{INFO} It will still run, but without the permission broker,")
        print(f"{INFO} the audit log, or the Minecraft subsystem.")
    else:
        print(f"{OK} Security modules present (permissions, capabilities, audit).")
        print(f"{OK} Minecraft subsystem present.")
    return problems


def check_dependencies() -> list[str]:
    _rule("Dependencies needed to start")
    problems: list[str] = []
    broken: list[str] = []

    absent: list[str] = []
    for module, pip_name, what in CRITICAL:
        err = _try_import(module)
        if err is None:
            print(f"{OK} {pip_name:<14} {what}")
            continue
        kind, message = err
        print(f"{FAIL} {pip_name:<14} {what}")
        print(f"{INFO}   {message}")
        (absent if kind == "absent" else broken).append(pip_name)

    if absent:
        problems.append(
            f"Not installed: {', '.join(absent)}. Install them from this folder:\n"
            f'         "{sys.executable}" -m pip install -r requirements.txt'
        )
    if broken:
        problems.append(
            f"Installed but will not load: {', '.join(broken)}. This is a broken\n"
            "     install or a missing system library, so re-running pip alone may\n"
            "     not fix it. On Windows the usual causes are:\n"
            "       * a missing Visual C++ runtime -- install the x64 build from\n"
            "         https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
            "       * a half-finished install -- force a clean one:\n"
            f'         "{sys.executable}" -m pip install --force-reinstall '
            f'{" ".join(broken)}'
        )

    _rule("Optional features")
    for module, pip_name, what in OPTIONAL:
        err = _try_import(module)
        if err is None:
            print(f"{OK} {pip_name:<16} {what}")
        else:
            kind, _ = err
            note = "not installed" if kind == "absent" else "installed but broken"
            print(f"{WARN} {pip_name:<16} {what}  -- {note}")
    return problems


def check_app_modules() -> list[str]:
    """The app's own modules. These fail when it is launched from the wrong
    working directory, which produces the same instant-exit symptom."""
    _rule("Application modules")
    problems: list[str] = []

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))

    cwd = Path(os.getcwd()).resolve()
    if cwd != REPO:
        print(f"{WARN} Current directory is not the app folder.")
        print(f"{INFO}   you are in : {cwd}")
        print(f"{INFO}   app lives  : {REPO}")
        print(f"{INFO} Launch it with the working directory set to the app folder,")
        print(f"{INFO} or just use run_jarvis.bat, which handles this.")

    for module, what in (("ui", "the HUD"),
                         ("memory.memory_manager", "long-term memory"),
                         ("core.action_loader", "the action loader")):
        err = _try_import(module)
        if err is None:
            print(f"{OK} {module:<22} {what}")
        else:
            print(f"{FAIL} {module:<22} {what}")
            print(f"{INFO}   {err[1]}")
            problems.append(f"{module} could not be imported -- see the error above.")
    return problems


def check_ocr() -> list[str]:
    """OCR needs a program as well as a package, so "pip installed it" is not
    the same question as "does it work"."""
    _rule("Reading the Minecraft screen (optional)")
    try:
        from core import ocr
        reader = ocr.create_reader()
    except Exception as exc:
        print(f"{WARN} could not check: {type(exc).__name__}: {exc}")
        return []

    if getattr(reader, "available", False):
        print(f"{OK} {reader.describe()}")
        print(f"{INFO} Verify it against the real game with:")
        print(f"{INFO}   py tools\\f3_check.py")
    else:
        print(f"{WARN} Not set up. Minecraft control still works; JARVIS")
        print(f"{INFO} just cannot read your position or what you are")
        print(f"{INFO} looking at, and says so rather than guessing.")
        for line in reader.describe().splitlines():
            print(f"{INFO}   {line}")
    return []


def check_config() -> list[str]:
    _rule("Configuration")
    problems: list[str] = []
    cfg = REPO / "config"

    if not cfg.is_dir():
        print(f"{WARN} config/ does not exist yet -- it is created on first run.")
        return problems

    probe = cfg / ".doctor_write_test"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        print(f"{OK} config/ is writable.")
    except Exception as exc:
        print(f"{FAIL} config/ is not writable: {type(exc).__name__}: {exc}")
        problems.append(
            "The app cannot save your API key. Move the folder out of "
            "Program Files / OneDrive, or fix its permissions."
        )

    keys = cfg / "api_keys.json"
    if keys.exists() and keys.stat().st_size > 2:
        print(f"{OK} An API key is saved. (Its value is not read or shown here.)")
    else:
        print(f"{INFO} No API key saved yet -- the setup screen will ask on first run.")
        print(f"{INFO} Get a free one at https://aistudio.google.com/apikey")
    return problems


def main() -> int:
    print("=" * 66)
    print("  MARK LIV -- startup doctor")
    print("=" * 66)
    print(f"  checking: {REPO}")

    problems: list[str] = []
    for check in (check_interpreter, check_checkout, check_dependencies,
                  check_app_modules, check_ocr, check_config):
        try:
            problems += check()
        except Exception:
            print(f"\n{FAIL} the '{check.__name__}' check itself crashed:")
            traceback.print_exc()
            problems.append(f"{check.__name__} crashed -- see the traceback above.")

    _rule("Verdict")
    if not problems:
        print(f"{OK} Nothing wrong found. MARK LIV should start.")
        print(f"{INFO} If it still closes instantly, run it from PowerShell with:")
        print(f'{INFO}   py main.py')
        print(f"{INFO} and send me everything the window prints.")
    else:
        print(f"{FAIL} {len(problems)} problem(s) found:\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}\n")
    print("=" * 66)
    return 0 if not problems else 1


if __name__ == "__main__":
    code = main()
    # Double-clicked rather than run from a shell: hold the window open, or
    # this script vanishes exactly the way the app did.
    if platform.system() == "Windows" and "--no-pause" not in sys.argv:
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
