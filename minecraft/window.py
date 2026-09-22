"""
minecraft/window.py — which window, where is it, and does it have focus?

THE ONE QUESTION THAT MATTERS
    Synthetic input goes to whatever window has focus. Not to a window handle,
    not to a process — to whatever is in front when the key event arrives. So
    the failure this file exists to prevent is:

        JARVIS believes Minecraft has focus
        → the user alt-tabs to Slack
        → JARVIS is still holding W
        → JARVIS is now typing into Slack

    Everything here serves `is_foreground()`. The rectangle is for capturing
    only the game's pixels; the handle is for identifying the window across
    calls. But the focus check is the safety property, and it is re-asked on
    every tick of every held action rather than once at the start.

PLATFORM REALITY, STATED PLAINLY
    Windows: `GetForegroundWindow` plus `GetWindowThreadProcessId` gives a
    definitive answer, and it is cheap enough to poll. This is the supported
    platform for the prototype.

    Linux/macOS: `pygetwindow` does not implement the active-window query on
    either, and the alternatives are an X11 binding or an accessibility
    permission. `Locator.probe()` therefore reports `focus_known=False` on
    those platforms, and `minecraft/controller.py` treats an unknown focus
    state as NOT focused — so the prototype refuses to send input rather than
    guessing. That is a real limitation and it is reported, not papered over.
"""

from __future__ import annotations

import ctypes
import threading
import platform
from dataclasses import dataclass

from minecraft import process as mc_process

_OS = platform.system()
_IS_WINDOWS = _OS == "Windows"

# A window smaller than this is a splash screen, a launcher or a crash dialog,
# not a game the assistant should be driving.
MIN_WINDOW_W = 320
MIN_WINDOW_H = 240


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def usable(self) -> bool:
        return self.width >= MIN_WINDOW_W and self.height >= MIN_WINDOW_H

    def as_dict(self) -> dict:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}

    def as_mss_region(self) -> dict:
        """The shape `mss.grab()` wants. Region only — never the full screen."""
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


@dataclass(frozen=True)
class WindowInfo:
    """A snapshot. Every field is what was true at `probe()` time and may
    already be stale — which is why the controller re-probes rather than
    caching this."""

    found: bool
    handle: int | None = None
    title: str = ""
    pid: int | None = None
    rect: WindowRect | None = None
    foreground: bool = False
    focus_known: bool = True
    detail: str = ""

    @property
    def controllable(self) -> bool:
        """Safe to send input to right now.

        `focus_known` False collapses to False: an unknown focus state is
        treated as not-focused, because the cost of being wrong is typing into
        someone else's window."""
        return bool(
            self.found and self.rect is not None and self.rect.usable
            and self.focus_known and self.foreground
        )

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "title": self.title[:80],
            "pid": self.pid,
            "rect": self.rect.as_dict() if self.rect else None,
            "foreground": self.foreground,
            "focus_known": self.focus_known,
            "controllable": self.controllable,
            "detail": self.detail,
        }


_PROTOTYPES_LOCK = threading.Lock()
_PROTOTYPES_READY = False
_WNDENUMPROC = None
_RECT_TYPE = None


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _ensure_prototypes():
    """Declare every user32 signature ONCE, and hand back the callback type.

    WHY ONCE, AND UNDER A LOCK
        `ctypes.windll.user32` is a cached object and its function pointers
        are shared, so assigning `.argtypes` mutates state every thread sees.
        Doing that per probe raced badly: `_guard()` runs from the action
        loop every 40ms, from the supervisor every 200ms and from the
        focus-wait loop, so one thread could call a function while another
        was mid-assignment. The call raised, the enumeration callback
        swallowed it, no windows were found, and the guard reported
        `window_gone`.

        Which surfaced as "the Minecraft window closed" in the middle of
        mining, on a window that was plainly still open -- intermittent,
        worst when both threads were busiest, and impossible to tell from a
        real crash. A correctness fix in one place became a liveness bug in
        another, which is the usual shape of this mistake.

    Returns the WNDENUMPROC type, which must also be created once: a fresh
    WINFUNCTYPE class per call would keep re-churning EnumWindows' argtypes
    for no reason."""
    global _PROTOTYPES_READY, _WNDENUMPROC
    if _PROTOTYPES_READY:
        return _WNDENUMPROC

    with _PROTOTYPES_LOCK:
        if _PROTOTYPES_READY:
            return _WNDENUMPROC

        user32 = ctypes.windll.user32
        enumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p,
                                      ctypes.c_void_p)

        _configure(user32, "GetForegroundWindow", [], ctypes.c_void_p)
        _configure(user32, "IsWindowVisible", [ctypes.c_void_p], ctypes.c_bool)
        _configure(user32, "GetWindowThreadProcessId",
                   [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)],
                   ctypes.c_ulong)
        _configure(user32, "GetWindowTextLengthW", [ctypes.c_void_p],
                   ctypes.c_int)
        _configure(user32, "GetWindowTextW",
                   [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int],
                   ctypes.c_int)
        _configure(user32, "GetWindowRect",
                   [ctypes.c_void_p, ctypes.POINTER(_RECT)], ctypes.c_bool)
        _configure(user32, "EnumWindows", [enumproc, ctypes.c_void_p],
                   ctypes.c_bool)

        _WNDENUMPROC = enumproc
        _PROTOTYPES_READY = True
        return _WNDENUMPROC


def _configure(library, name: str, argtypes, restype) -> None:
    """Declare one function's signature, tolerating a missing symbol.

    Tolerant because a missing user32 export should degrade to "I cannot tell"
    rather than crash the probe -- but a PRESENT function with the wrong
    signature is the failure this exists to prevent, and that one is silent."""
    try:
        function = getattr(library, name)
        function.argtypes = argtypes
        function.restype = restype
    except Exception:
        pass


def same_handle(a, b) -> bool:
    """Do these two window handles refer to the same window?

    Compared on the low 32 bits, which is correct rather than merely
    convenient: Windows HANDLE values are documented to be 32-bit significant
    and sign-extended when widened, precisely so 32- and 64-bit code can
    interoperate. So the low word IS the handle, and two values that agree
    there are the same window.

    Masking to 64 bits would NOT do: a handle returned through a signed 32-bit
    int comes back sign-extended, so 0xFFFE1234 arrives as
    0xFFFFFFFFFFFE1234, which differs from the real value in every high bit.
    That was the bug -- the same window comparing unequal to itself.

    The prototypes above should stop the truncation happening at all. This
    stays as the second line of defence, because the failure is silent and
    presents as user error."""
    if a is None or b is None:
        return False
    try:
        return (int(a) & 0xFFFFFFFF) == (int(b) & 0xFFFFFFFF)
    except (TypeError, ValueError):
        return False


# ── Windows implementation ───────────────────────────────────────────────────

def _probe_windows(pid: int | None) -> WindowInfo:
    user32 = ctypes.windll.user32
    WNDENUMPROC = _ensure_prototypes()

    found: list[tuple[int, str, int]] = []      # (hwnd, title, area)

    def _pid_of(hwnd) -> int:
        out = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(out))
        return int(out.value)

    def _title_of(hwnd) -> str:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value or ""

    def _enum(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if pid is not None and _pid_of(hwnd) != pid:
                return True
            rect = _RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < MIN_WINDOW_W or height < MIN_WINDOW_H:
                return True            # splash, tooltip, or a hidden helper
            found.append((int(hwnd), _title_of(hwnd), width * height))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_enum), 0)
    except Exception as e:
        return WindowInfo(found=False,
                          detail=f"Could not enumerate windows ({type(e).__name__}).")

    if not found:
        # Deliberately says "could not find" rather than "closed". The
        # difference matters: a closed game is final and a failed enumeration
        # is transient, and reporting the second as the first is what made a
        # thread race read as "the Minecraft window closed" mid-mine.
        return WindowInfo(
            found=False, pid=pid,
            detail="Minecraft is running but I could not find a game window "
                   "just now. It may still be starting up, or minimised.",
        )

    # The biggest visible window belonging to the process is the game; the
    # others are its helpers.
    found.sort(key=lambda item: item[2], reverse=True)
    hwnd, title, _area = found[0]

    rect = _RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    window_rect = WindowRect(left=int(rect.left), top=int(rect.top),
                             width=int(rect.right - rect.left),
                             height=int(rect.bottom - rect.top))

    try:
        foreground = same_handle(user32.GetForegroundWindow(), hwnd)
    except Exception:
        return WindowInfo(found=True, handle=hwnd, title=title, pid=pid,
                          rect=window_rect, foreground=False, focus_known=False,
                          detail="Could not read the foreground window.")

    return WindowInfo(
        found=True, handle=hwnd, title=title, pid=pid, rect=window_rect,
        foreground=foreground, focus_known=True,
        detail=("Minecraft has focus." if foreground
                else "Minecraft is open but another window has focus."),
    )


# ── Everywhere else ──────────────────────────────────────────────────────────

def _probe_fallback(pid: int | None) -> WindowInfo:
    """Best effort off Windows: geometry where pygetwindow can supply it, and
    an explicit "I do not know" for focus.

    pygetwindow has no active-window implementation on Linux and returns
    nothing useful on macOS, so rather than guessing, `focus_known` is False —
    which `controllable` turns into a refusal."""
    title = ""
    rect: WindowRect | None = None
    try:
        import pygetwindow as gw
        matches = [w for w in gw.getAllWindows()
                   if "minecraft" in str(getattr(w, "title", "")).lower()]
        if matches:
            win = max(matches, key=lambda w: (getattr(w, "width", 0)
                                              * getattr(w, "height", 0)))
            title = str(getattr(win, "title", ""))
            rect = WindowRect(left=int(getattr(win, "left", 0)),
                              top=int(getattr(win, "top", 0)),
                              width=int(getattr(win, "width", 0)),
                              height=int(getattr(win, "height", 0)))
    except Exception:
        pass

    return WindowInfo(
        found=rect is not None, title=title, pid=pid, rect=rect,
        foreground=False, focus_known=False,
        detail=(f"On {_OS} I cannot tell which window has focus without an X11 "
                f"binding or accessibility permission, so I will not send "
                f"input. Observation still works."),
    )


# ── The public surface ───────────────────────────────────────────────────────

class Locator:
    """Finds and re-checks the Minecraft window.

    A class rather than functions so tests can substitute a fake with the same
    two methods, and so the controller can hold one object whose `probe()` it
    trusts to be live rather than cached."""

    def __init__(self, pid: int | None = None):
        self._pid = pid

    @property
    def pid(self) -> int | None:
        return self._pid

    def attach(self) -> WindowInfo:
        """Find the process, then its window. Call once when a session opens."""
        info = mc_process.find()
        if not info.running:
            return WindowInfo(found=False, detail=info.detail)
        self._pid = info.pid
        return self.probe()

    def probe(self) -> WindowInfo:
        """Re-ask everything. Called before and during every input action.

        Never raises: a probe that throws during a held keypress would skip the
        release."""
        try:
            if _IS_WINDOWS:
                return _probe_windows(self._pid)
            return _probe_fallback(self._pid)
        except Exception as e:                        # pragma: no cover
            return WindowInfo(found=False, focus_known=False,
                              detail=f"Window probe failed ({type(e).__name__}).")


def describe() -> str:
    """One human sentence, for `status` and the manual check."""
    info = Locator().attach()
    if not info.found:
        return info.detail
    rect = info.rect.as_dict() if info.rect else {}
    return (f"'{info.title[:60]}' at {rect.get('width')}x{rect.get('height')} "
            f"({rect.get('left')},{rect.get('top')}). {info.detail}")


__all__ = ["WindowRect", "WindowInfo", "Locator", "describe", "same_handle",
           "MIN_WINDOW_W", "MIN_WINDOW_H"]
