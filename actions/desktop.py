#desktop.py
import os
import sys
import json
import shutil
import subprocess
import tempfile
import platform
from pathlib import Path
from datetime import datetime

from core import capabilities, exec_safe, safe_path
from core.undo import push_undo

try:
    import pyautogui
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_api_key() -> str:
    path = _get_base_dir() / "config" / "api_keys.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]
    
def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

# ── Running code a language model wrote ──────────────────────────────────────
#
# WHAT WAS HERE BEFORE
#     `_build_sandbox()` built a dict of "allowed" names and ran the model's
#     Python with `exec(compile(code, ...), sandbox)`. The prompt told the model
#     there would be no subprocess, no deletion and no imports. None of that was
#     enforced, because a restricted globals dict is not a sandbox:
#
#         Path.__init__.__globals__["__builtins__"]["__import__"]("os").system(...)
#
#     is one expression, and every object left in that dict — Path, pyautogui,
#     ctypes, winreg — is a door to the same place. The rules were a request.
#
# WHAT REPLACES IT
#     The code is written to a file and run in a SEPARATE interpreter through
#     `core/exec_safe.py`: argv only, hard timeout, whole process group killed
#     when it expires, output capped.
#
#     Be clear about what that does and does not buy. The subprocess has a full
#     Python and can do anything the user can do — MORE than the dict pretended
#     to allow. What makes it defensible is that it is honest about that: the
#     capability is `code.execute`, which is CONFIRM, so a human reads a summary
#     and presses a button before any of it runs. The security boundary is the
#     person, and it always was; the difference is that now the code says so.
#
#     What the subprocess adds over the old `exec` is real: it cannot corrupt
#     the assistant's own interpreter, it cannot hang the executor thread
#     forever, and it can be killed.

_TASK_DIR = Path.home() / ".jarvis" / "desktop_tasks"
_TASK_TIMEOUT_S = 60.0
_MAX_CODE_CHARS = 20_000


def _code_digest(code: str, limit: int = 400) -> str:
    """A short, flattened preview of generated code for the confirmation banner.

    The user is being asked to approve running this, so they have to be able to
    see what it is. Long code is truncated rather than hidden."""
    flat = " ".join(str(code or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _write_task_script(code: str) -> Path:
    """Put the generated code somewhere contained, and return the path."""
    _TASK_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="task_", suffix=".py", dir=str(_TASK_DIR))
    os.close(fd)
    path = safe_path.resolve_within(_TASK_DIR, name)
    path.write_text(code, encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return path


def _execute_generated_code(code: str, player=None) -> str:
    """Run model-written Python in its own interpreter, with a deadline.

    Reached only after the broker has had a human approve `code.execute`."""
    if not code or code.strip() == "UNSAFE":
        return "I could not work out a safe way to do that, so I have not done it."
    if code.strip().startswith("ERROR:"):
        return f"I could not generate the steps for that: {code.strip()[:200]}"

    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1]).strip()

    if len(code) > _MAX_CODE_CHARS:
        return (f"The generated script is {len(code)} characters, which is far "
                f"more than a desktop task should need. I have not run it.")

    script = None
    try:
        script = _write_task_script(code)
        result = exec_safe.run(
            [exec_safe.python_executable(), "-I", str(script)],
            timeout=_TASK_TIMEOUT_S,
            cwd=str(_TASK_DIR),
        )
    except Exception as e:
        return f"Could not run the generated script: {e}"
    finally:
        if script is not None:
            try:
                script.unlink(missing_ok=True)
            except Exception:
                pass

    if result.timed_out:
        return (f"The task was still running after {int(_TASK_TIMEOUT_S)} seconds, "
                f"so I stopped it. Nothing further was done.")
    if result.error:
        return f"The task could not run: {result.summary()}"
    if not result.ok:
        detail = result.failure_detail(300)
        return (f"The task failed (exit code {result.returncode})."
                + (f" {detail}" if detail else ""))

    out = (result.stdout or "").strip()
    return out[:2000] if out else "Done."


def _ask_gemini_for_desktop_action(task: str) -> str:

    from google import genai as _genai

    desktop = str(_get_desktop())

    os_specific = ""
    if _OS == "Windows":
        os_specific = "- ctypes (Windows API calls, read-only)\n- winreg (registry READ only)"
    elif _OS == "Darwin":
        os_specific = "- subprocess is NOT available; use pyautogui or Path only"
    else:
        os_specific = "- subprocess is NOT available; use pyautogui or Path only"

    prompt = f"""You are a desktop automation assistant.
Current OS: {_OS}
Desktop path: {desktop}

Generate safe Python code to accomplish the task below.
Allowed modules ONLY:
- pyautogui (mouse, keyboard — if needed)
- pathlib.Path (file/folder inspection only, no deletion)
- shutil.copy2, shutil.copytree, shutil.disk_usage (NO move, NO rmtree)
- os_path (os.path equivalent, read-only)
- time.sleep
{os_specific}

Hard rules:
- NO file deletion (no unlink, no rmtree, no remove)
- NO subprocess calls
- NO exec() or eval() inside the code
- NO import statements (modules are pre-injected)
- NO file write operations except explicitly requested
- If task cannot be done safely with these tools, output exactly: UNSAFE

Output ONLY the Python code. No explanation, no markdown, no backticks.

Task: {task}"""

    try:
        from core import gemini
        response = gemini.call(prompt, tier=gemini.SMART, timeout_ms=30_000)
        if response is None:
            return "ERROR: every Gemini model on the ladder failed"
        code = (response.text or "").strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code  = "\n".join(lines[1:-1]).strip()
        return code
    except Exception as e:
        return f"ERROR: {e}"

def set_wallpaper(image_path: str) -> str:
    path = Path(image_path).expanduser().resolve()
    if not path.exists():
        return f"Image not found: {image_path}"
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
        return f"Unsupported format: {path.suffix}. Use jpg, png, bmp or webp."

    try:
        if _OS == "Windows":
            import ctypes
            if path.suffix.lower() in {".webp", ".png"}:
                try:
                    from PIL import Image
                    _fd, _bmp = tempfile.mkstemp(suffix=".bmp")
                    os.close(_fd)
                    bmp_path = Path(_bmp)
                    Image.open(path).convert("RGB").save(bmp_path, "BMP")
                    path = bmp_path
                except ImportError:
                    pass 
            ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)
            return f"Wallpaper set: {path.name}"

        elif _OS == "Darwin":
            script = (
                f'tell application "System Events" to tell every desktop to '
                f'set picture to POSIX file "{path}"'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
            return f"Wallpaper set: {path.name}"

        else:
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            uri = f"file://{path}"

            if "gnome" in desktop_env or "unity" in desktop_env:
                subprocess.run([
                    "gsettings", "set", "org.gnome.desktop.background",
                    "picture-uri", uri
                ], capture_output=True, timeout=15)
                subprocess.run([
                    "gsettings", "set", "org.gnome.desktop.background",
                    "picture-uri-dark", uri
                ], capture_output=True, timeout=15)

            elif "kde" in desktop_env:
                # KDE Plasma
                script = f"""
var allDesktops = desktops();
for (var i = 0; i < allDesktops.length; i++) {{
    d = allDesktops[i];
    d.wallpaperPlugin = "org.kde.image";
    d.currentConfigGroup = ["Wallpaper", "org.kde.image", "General"];
    d.writeConfig("Image", "file://{path}");
}}
"""
                subprocess.run(
                    ["qdbus", "org.kde.plasmashell", "/PlasmaShell",
                     "org.kde.PlasmaShell.evaluateScript", script],
                    capture_output=True, timeout=15)

            elif "xfce" in desktop_env:
                subprocess.run([
                    "xfconf-query", "-c", "xfce4-desktop",
                    "-p", "/backdrop/screen0/monitor0/workspace0/last-image",
                    "-s", str(path)
                ], capture_output=True, timeout=15)

            else:
                result = subprocess.run(
                    ["feh", "--bg-scale", str(path)],
                    capture_output=True, timeout=15)
                if result.returncode != 0:
                    return (
                        f"Could not set wallpaper automatically on {desktop_env}. "
                        f"Try manually or install 'feh'."
                    )

            return f"Wallpaper set: {path.name}"

    except Exception as e:
        return f"Could not set wallpaper: {e}"


def set_wallpaper_from_url(url: str) -> str:
    try:
        import urllib.request
        if not str(url).lower().startswith(("http://", "https://")):
            return "I can only download wallpapers over http or https."
        suffix = Path(url.split("?")[0]).suffix or ".jpg"
        _fd, _tmp = tempfile.mkstemp(suffix=suffix)
        os.close(_fd)
        tmp = Path(_tmp)
        # A timeout, because urlretrieve's default is to wait forever and this
        # runs on an executor thread that then never comes back.
        with urllib.request.urlopen(url, timeout=30) as response:
            tmp.write_bytes(response.read(30 * 1024 * 1024))
        result = set_wallpaper(str(tmp))
        try:
            tmp.unlink()
        except Exception:
            pass
        return result
    except Exception as e:
        return f"Could not download wallpaper: {e}"


def get_current_wallpaper() -> str:
    try:
        if _OS == "Windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop"
            )
            val, _ = winreg.QueryValueEx(key, "Wallpaper")
            winreg.CloseKey(key)
            return f"Current wallpaper: {val}"

        elif _OS == "Darwin":
            script = (
                'tell application "System Events" to get picture of desktop 1'
            )
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=15)
            return f"Current wallpaper: {result.stdout.strip()}"

        else:
            desktop_env = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
            if "gnome" in desktop_env or "unity" in desktop_env:
                result = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                    capture_output=True, text=True, timeout=15)
                return f"Current wallpaper: {result.stdout.strip()}"
            return "Wallpaper path retrieval not supported for this desktop environment."

    except Exception as e:
        return f"Could not get wallpaper: {e}"

FILE_TYPE_MAP = {
    "Images":      {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
    "Documents":   {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                    ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
    "Videos":      {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
    "Music":       {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
    "Archives":    {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "Code":        {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                    ".cpp", ".java", ".cs", ".go", ".rs", ".sh", ".php"},
    "Executables": {".exe", ".msi", ".bat", ".cmd", ".sh", ".appimage", ".deb", ".rpm"},
}

_SKIP_EXTENSIONS = {
    "Windows": {".lnk", ".url"},
    "Darwin":  {".webloc"},
    "Linux":   {".desktop"},
}


def organize_desktop(mode: str = "by_type") -> str:
    """Sort loose desktop files into folders.

    Every move is journalled as it happens, so a failure halfway through
    reports what actually moved instead of raising out of a half-finished
    batch, and the whole thing can be taken back with one undo. Before this,
    an exception on file 30 of 50 left the desktop rearranged and returned
    nothing to say so."""
    desktop       = _get_desktop()
    skip_exts     = _SKIP_EXTENSIONS.get(_OS, set())
    moved, skipped, failed = [], [], []
    journal: list[tuple[Path, Path]] = []

    try:
        entries = sorted(desktop.iterdir())
    except Exception as e:
        return f"Could not read the desktop: {e}"

    for item in entries:
        try:
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.suffix.lower() in skip_exts:
                continue

            if mode == "by_date":
                mtime       = datetime.fromtimestamp(item.stat().st_mtime)
                folder_name = mtime.strftime("%Y-%m")
            else:
                ext         = item.suffix.lower()
                folder_name = "Others"
                for folder, exts in FILE_TYPE_MAP.items():
                    if ext in exts:
                        folder_name = folder
                        break

            target_dir = desktop / folder_name
            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            origin = item.resolve()
            shutil.move(str(item), str(new_path))
            journal.append((origin, new_path.resolve()))
            moved.append(f"{item.name} -> {folder_name}/")
        except Exception as e:
            # One unreadable or locked file must not abandon the rest, and must
            # not be silently dropped either.
            failed.append(f"{item.name}: {e}")

    if journal:
        push_undo(f"organized the desktop ({len(journal)} files)",
                  _undo_moves(tuple(journal)))

    result = f"Desktop organized ({mode}): {len(moved)} file(s) moved."
    if moved:
        result += "\n" + "\n".join(moved[:8])
        if len(moved) > 8:
            result += f"\n... and {len(moved) - 8} more."
    if skipped:
        result += f"\n{len(skipped)} file(s) skipped (a file of that name was already there)."
    if failed:
        result += (f"\n{len(failed)} file(s) could NOT be moved: "
                   + "; ".join(failed[:3]))
        if len(failed) > 3:
            result += f" and {len(failed) - 3} more."
    return result


def _undo_moves(entries: tuple):
    """Reverse a batch of moves, and say how much of it came back.

    Shared by organize and clean. Empty folders we created are removed; a
    folder the user has since put something into is left alone."""
    def _fn():
        restored = 0
        for origin, moved_to in entries:
            try:
                if moved_to.exists():
                    origin.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(moved_to), str(origin))
                    restored += 1
            except Exception as e:
                print(f"[desktop] undo: could not restore {moved_to.name}: {e}")
        for folder in {m.parent for _o, m in entries}:
            try:
                if folder.exists() and folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except Exception:
                pass
        if restored == len(entries):
            return f"All {restored} file(s) put back."
        return f"{restored} of {len(entries)} file(s) put back; the rest had already moved on."
    return _fn


def list_desktop() -> str:
    desktop = _get_desktop()
    items   = []
    for item in sorted(desktop.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            try:
                count = len(list(item.iterdir()))
            except PermissionError:
                count = "?"
            items.append(f"📁 {item.name}/ ({count} items)")
        else:
            size     = item.stat().st_size
            size_str = (
                f"{size / 1024:.1f} KB" if size < 1024 * 1024
                else f"{size / 1024 / 1024:.1f} MB"
            )
            items.append(f"📄 {item.name} ({size_str})")

    if not items:
        return "Desktop is empty."
    return f"Desktop ({len(items)} items):\n" + "\n".join(items)


def clean_desktop() -> str:
    """Archive every loose desktop file into one dated folder. Reversible."""
    desktop     = _get_desktop()
    skip_exts   = _SKIP_EXTENSIONS.get(_OS, set())
    today       = datetime.now().strftime("%Y-%m-%d")
    archive_dir = desktop / f"Desktop Archive {today}"

    try:
        archive_dir.mkdir(exist_ok=True)
        entries = sorted(desktop.iterdir())
    except Exception as e:
        return f"Could not prepare the archive folder: {e}"

    journal: list[tuple[Path, Path]] = []
    failed: list[str] = []

    for item in entries:
        try:
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.suffix.lower() in skip_exts:
                continue
            new_path = archive_dir / item.name
            if new_path.exists():
                continue
            origin = item.resolve()
            shutil.move(str(item), str(new_path))
            journal.append((origin, new_path.resolve()))
        except Exception as e:
            failed.append(f"{item.name}: {e}")

    if journal:
        push_undo(f"archived {len(journal)} desktop file(s)",
                  _undo_moves(tuple(journal)))

    result = f"Desktop cleaned: {len(journal)} file(s) archived to '{archive_dir.name}'."
    if failed:
        result += (f"\n{len(failed)} file(s) could NOT be archived: "
                   + "; ".join(failed[:3]))
    return result


def get_desktop_stats() -> str:
    desktop    = _get_desktop()
    files      = [i for i in desktop.iterdir() if i.is_file()]
    folders    = [i for i in desktop.iterdir() if i.is_dir()]
    total_size = sum(f.stat().st_size for f in files if f.exists())
    size_str   = (
        f"{total_size / 1024:.1f} KB" if total_size < 1024 * 1024
        else f"{total_size / 1024 / 1024:.1f} MB"
    )
    return (
        f"Desktop stats ({_OS}):\n"
        f"  Files   : {len(files)}\n"
        f"  Folders : {len(folders)}\n"
        f"  Size    : {size_str}\n"
        f"  Path    : {desktop}"
    )

def desktop_control(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    parameters:
        action : wallpaper | wallpaper_url | current_wallpaper |
                 organize  | clean | list | stats |
                 task (AI-powered)
        path   : image path for 'wallpaper'
        url    : image URL for 'wallpaper_url'
        mode   : 'by_type' or 'by_date' for 'organize'
        task   : natural language description for AI-powered actions
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    task   = params.get("task", "").strip()

    if player:
        player.write_log(f"[desktop] {action or task[:40]}")

    try:
        if action == "wallpaper":
            path = params.get("path", "")
            return set_wallpaper(path) if path else "No image path provided."

        elif action == "wallpaper_url":
            url = params.get("url", "")
            return set_wallpaper_from_url(url) if url else "No URL provided."

        elif action == "current_wallpaper":
            return get_current_wallpaper()

        elif action == "organize":
            return organize_desktop(params.get("mode", "by_type"))

        elif action == "clean":
            return clean_desktop()

        elif action == "list":
            return list_desktop()

        elif action == "stats":
            return get_desktop_stats()

        elif action == "task" or task:
            actual_task = task or params.get("description", "")
            if not actual_task:
                return "Please describe what you want to do on the desktop."

            print(f"[Desktop] Asking Gemini: {actual_task}")
            if player:
                player.write_log("[Desktop] Generating action...")

            code = _ask_gemini_for_desktop_action(actual_task)
            return _execute_generated_code(code, player=player)

        else:
            if action:
                code = _ask_gemini_for_desktop_action(action)
                return _execute_generated_code(code, player=player)
            return "No action or task specified."

    except Exception as e:
        print(f"[Desktop] Error: {e}")
        return f"Desktop control error: {e}"


# ── Capability resolution ────────────────────────────────────────────────────
#
# One tool, several very different risks: listing the desktop is free, moving
# fifty files is not, and running generated Python is the most consequential
# thing in this file. The resolver says which is which, per call.

def _desktop_capability(params: dict) -> str:
    action = str((params or {}).get("action", "")).lower().strip()
    task   = str((params or {}).get("task", "")).strip()

    if action in ("list", "stats", "current_wallpaper"):
        return capabilities.READ_ONLY
    if action in ("wallpaper", "wallpaper_url"):
        return capabilities.SYSTEM_SETTINGS
    if action in ("organize", "clean"):
        return capabilities.FILE_MOVE
    if action == "task" or task or action:
        # Anything else falls through to "ask Gemini for code and run it",
        # including an unrecognised action string — which is exactly the path
        # that must not be cheap to reach.
        return capabilities.CODE_EXEC
    return capabilities.READ_ONLY


def _desktop_guard(params: dict) -> dict:
    action = str((params or {}).get("action", "")).lower().strip()
    task   = str((params or {}).get("task", "")).strip()

    if action == "wallpaper":
        path = str((params or {}).get("path", ""))
        return {"summary": "Change the desktop wallpaper",
                "target": path,
                "undo_provider": _wallpaper_undo_provider}
    if action == "wallpaper_url":
        return {"summary": "Download an image and set it as the wallpaper",
                "url": str((params or {}).get("url", "")),
                "undo_provider": _wallpaper_undo_provider}
    if action == "organize":
        return {"summary": "Sort every loose file on the desktop into folders",
                "detail": "This moves files. You can tell me to undo it afterwards.",
                "target": str(_get_desktop())}
    if action == "clean":
        return {"summary": "Move every loose desktop file into one archive folder",
                "detail": "This moves files. You can tell me to undo it afterwards.",
                "target": str(_get_desktop())}
    if action == "task" or task or action not in ("list", "stats", "current_wallpaper"):
        wanted = task or (params or {}).get("description", "") or action
        return {
            "summary": "Run a generated script on this computer",
            "detail": (f"To do: {str(wanted)[:120]}. I will write Python for this "
                       f"and run it in a separate interpreter with a 60-second "
                       f"limit. It runs with your full user account."),
        }
    return {"summary": f"Desktop: {action}"}


def _wallpaper_undo_provider():
    """Capture the current wallpaper so the change can be taken back.

    Returns None when the current wallpaper cannot be read — on those
    platforms the change is not reversible, so the broker asks first instead
    of pretending an undo exists."""
    try:
        if _OS != "Windows":
            return None
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\\Desktop")
        previous, _ = winreg.QueryValueEx(key, "Wallpaper")
        winreg.CloseKey(key)
        if not previous or not Path(previous).exists():
            return None

        def _restore(path=previous):
            return set_wallpaper(str(path))
        return _restore
    except Exception:
        return None


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "desktop_control",
    "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"
            },
            "path": {
                "type": "STRING",
                "description": "Image path for wallpaper"
            },
            "url": {
                "type": "STRING",
                "description": "Image URL for wallpaper_url"
            },
            "mode": {
                "type": "STRING",
                "description": "by_type or by_date for organize"
            },
            "task": {
                "type": "STRING",
                "description": "Natural language desktop task"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": desktop_control,
    "capability": _desktop_capability,
    "guard": _desktop_guard,
}
