"""
minecraft/input_backend.py — the only keys this subsystem can press.

WHY NOT REUSE actions/computer_control.py
    That module can press any key, anywhere, with any modifier. Reusing it
    would make "JARVIS can walk forward in Minecraft" mean "JARVIS can send
    arbitrary keystrokes to Windows", which is exactly the conflation this
    package exists to prevent. So this backend has its own vocabulary, and the
    vocabulary is a frozen dict of sixteen keys. There is no function here that
    takes a keycode.

    ALT, the Windows key, F4, and every combination are not "blocked" by a
    filter that could be bypassed — they are absent. `key_down("alt")` raises
    `InvalidAction` because "alt" is not a key in `_SCANCODES`, the same way
    `key_down("format c:")` does.

WHY SCAN CODES RATHER THAN VIRTUAL KEYS
    Minecraft Java runs on GLFW, which reads hardware scan codes. Injecting a
    virtual-key event works for ordinary Windows applications and is unreliable
    for games; `KEYEVENTF_SCANCODE` is what actually moves the player. This is
    the difference between the prototype working and appearing to work while
    nothing happens on screen.

WHY RELATIVE MOUSE MOVEMENT
    While Minecraft has the cursor captured it does not read the cursor
    position at all — it reads deltas. `pyautogui.moveTo(x, y)` sets an
    absolute position and the camera does not move. So looking around uses
    `SendInput` with `MOUSEEVENTF_MOVE` and no `MOUSEEVENTF_ABSOLUTE` flag.

    There is one thing here I have NOT verified, and will not claim: whether
    Minecraft's "Raw Input" setting (on by default) accepts injected deltas on
    every driver and build. `tools/minecraft_manual_check.py` tests exactly
    that, first, and tells you if it does not work rather than leaving you to
    discover it mid-session.

OFF WINDOWS
    `UnavailableBackend` refuses every call with a reason. Sending input on
    Linux would mean shelling out to `xdotool`, and this package does not get a
    subprocess — see `tests/test_minecraft_boundary.py`. Refusing honestly is
    better than a backend that silently does nothing while the planner believes
    the player is walking.
"""

from __future__ import annotations

import ctypes
import platform
from typing import Protocol

from minecraft.errors import InputBackendUnavailable, InvalidAction

_IS_WINDOWS = platform.system() == "Windows"


# ── The vocabulary ───────────────────────────────────────────────────────────
#
# Set-1 hardware scan codes. This dict IS the allowlist: a key that is not a
# member cannot be pressed, because there is no other way to reach the backend.
_SCANCODES: dict[str, int] = {
    # Movement
    "w": 0x11, "a": 0x1E, "s": 0x1F, "d": 0x20,
    # Jump, sneak, sprint
    "space": 0x39, "shift": 0x2A, "ctrl": 0x1D,
    # Hotbar
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
    "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A,
    # Inventory, drop, debug overlay, perspective, menu
    "e": 0x12, "q": 0x10, "f3": 0x3D, "f5": 0x3F, "esc": 0x01,
}

ALLOWED_KEYS = frozenset(_SCANCODES)

# Names a planner might reasonably try that are refused on purpose, mapped to
# why — so the refusal teaches rather than just failing.
_EXPLAINED_REFUSALS = {
    "alt": "ALT is not available: alt-tab and alt-F4 are how a person takes "
           "control back, so I do not get to press it.",
    "tab": "TAB is not available for the same reason as ALT.",
    "f4": "F4 is not available — with ALT it closes the game.",
    "win": "The Windows key is not available: it leaves the game.",
    "windows": "The Windows key is not available: it leaves the game.",
    "cmd": "The command key is not available: it leaves the game.",
    "super": "The super key is not available: it leaves the game.",
    "meta": "The meta key is not available: it leaves the game.",
    "enter": "ENTER is not available yet — in Minecraft it opens chat, which "
             "is a separate capability that is not enabled.",
    "return": "ENTER is not available yet — it opens chat.",
    "t": "T is not available yet — it opens chat.",
    "slash": "'/' is not available: it opens the command line, which is "
             "refused permanently.",
    "/": "'/' is not available: it opens the command line, which is refused "
         "permanently.",
    "f11": "F11 is not available — toggling fullscreen would break the window "
           "tracking this depends on.",
}


def validate_key(key: str) -> str:
    """Normalise and check a key name, or raise `InvalidAction`.

    Called before anything reaches a backend, so a rejected key produces no
    input at all rather than a partial one."""
    if not isinstance(key, str):
        raise InvalidAction(f"A key must be a name like 'w', not "
                            f"{type(key).__name__}.")
    name = key.strip().lower()
    if not name:
        raise InvalidAction("No key given.")
    if name in _SCANCODES:
        return name

    if name in _EXPLAINED_REFUSALS:
        raise InvalidAction(_EXPLAINED_REFUSALS[name])
    if "+" in name or "-" in name and len(name) > 1:
        raise InvalidAction(
            f"'{key}' looks like a key combination. I can only press single "
            f"keys from a fixed list, so combinations are not available."
        )
    raise InvalidAction(
        f"'{key}' is not a key I can press. The ones I can are: "
        f"{', '.join(sorted(ALLOWED_KEYS))}."
    )


# ── The contract ─────────────────────────────────────────────────────────────

class InputBackend(Protocol):
    """What the controller needs. Deliberately four methods and no more —
    there is no `press_any`, no `type_text`, no `hotkey`."""

    name: str
    available: bool

    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def move_mouse_relative(self, dx: int, dy: int) -> None: ...
    def describe(self) -> str: ...


class UnavailableBackend:
    """Used wherever input cannot be sent safely. Refuses, and says why.

    Not a no-op: a backend that accepted calls and did nothing would have the
    planner believe the player moved, and every verification step after that
    would be reasoning about a world that never changed."""

    name = "unavailable"
    available = False

    def __init__(self, reason: str):
        self._reason = reason

    def key_down(self, key: str) -> None:
        validate_key(key)
        raise InputBackendUnavailable(self._reason)

    def key_up(self, key: str) -> None:
        validate_key(key)
        raise InputBackendUnavailable(self._reason)

    def move_mouse_relative(self, dx: int, dy: int) -> None:
        raise InputBackendUnavailable(self._reason)

    def describe(self) -> str:
        return self._reason


# ── Windows: SendInput with scan codes and relative mouse deltas ─────────────

if _IS_WINDOWS:                                       # pragma: no cover
    _ULONG_PTR = (ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8
                  else ctypes.c_ulong)

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", _ULONG_PTR)]

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", _ULONG_PTR)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTUNION)]

    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_SCANCODE = 0x0008
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_EXTENDEDKEY = 0x0001
    _MOUSEEVENTF_MOVE = 0x0001

    # Scan codes that need the extended-key flag to be delivered correctly.
    _EXTENDED = frozenset()

    class WindowsInputBackend:
        """SendInput, scan codes, relative mouse. The supported backend."""

        name = "windows-sendinput"
        available = True

        def __init__(self):
            self._user32 = ctypes.windll.user32

        def _send_key(self, key: str, up: bool) -> None:
            name = validate_key(key)
            scan = _SCANCODES[name]
            flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_KEYUP if up else 0)
            if scan in _EXTENDED:
                flags |= _KEYEVENTF_EXTENDEDKEY
            event = _INPUT(type=_INPUT_KEYBOARD)
            event.union.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags,
                                         time=0, dwExtraInfo=0)
            sent = self._user32.SendInput(1, ctypes.byref(event),
                                          ctypes.sizeof(_INPUT))
            if sent != 1:
                raise InputBackendUnavailable(
                    f"Windows refused the key event for '{name}' "
                    f"(SendInput returned {sent}). This usually means another "
                    f"program is running as administrator and blocking input."
                )

        def key_down(self, key: str) -> None:
            self._send_key(key, up=False)

        def key_up(self, key: str) -> None:
            self._send_key(key, up=True)

        def move_mouse_relative(self, dx: int, dy: int) -> None:
            """Relative, never absolute.

            No MOUSEEVENTF_ABSOLUTE flag, so Windows treats dx/dy as mickeys of
            movement — which is what Minecraft reads while it has the cursor
            captured."""
            event = _INPUT(type=_INPUT_MOUSE)
            event.union.mi = _MOUSEINPUT(dx=int(dx), dy=int(dy), mouseData=0,
                                         dwFlags=_MOUSEEVENTF_MOVE, time=0,
                                         dwExtraInfo=0)
            sent = self._user32.SendInput(1, ctypes.byref(event),
                                          ctypes.sizeof(_INPUT))
            if sent != 1:
                raise InputBackendUnavailable(
                    f"Windows refused the mouse event (SendInput returned "
                    f"{sent})."
                )

        def describe(self) -> str:
            return ("Windows SendInput with hardware scan codes and relative "
                    "mouse deltas.")


# ── A backend that records instead of acting, for tests ──────────────────────

class FakeInputBackend:
    """Records what it was asked to do. Used by every test in this package.

    Lives in the package rather than in the tests so the controller tests and
    the manual check exercise the same contract, and so a test cannot quietly
    diverge from what a real backend accepts — `validate_key` runs here too."""

    name = "fake"

    def __init__(self, available: bool = True, fail_on: str = ""):
        self.available = available
        self.events: list[tuple] = []
        self.held: set[str] = set()
        self._fail_on = fail_on

    def key_down(self, key: str) -> None:
        name = validate_key(key)
        if not self.available:
            raise InputBackendUnavailable("fake backend is unavailable")
        if self._fail_on == "key_down":
            raise InputBackendUnavailable("fake key_down failure")
        self.events.append(("down", name))
        self.held.add(name)

    def key_up(self, key: str) -> None:
        name = validate_key(key)
        if self._fail_on == "key_up":
            self.events.append(("up_failed", name))
            raise InputBackendUnavailable("fake key_up failure")
        self.events.append(("up", name))
        self.held.discard(name)

    def move_mouse_relative(self, dx: int, dy: int) -> None:
        if not self.available:
            raise InputBackendUnavailable("fake backend is unavailable")
        self.events.append(("mouse", int(dx), int(dy)))

    def describe(self) -> str:
        return "Fake backend (records events, sends nothing)."

    # -- helpers the tests read --
    def downs(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "down"]

    def ups(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "up"]

    def mouse_moves(self) -> list[tuple[int, int]]:
        return [(e[1], e[2]) for e in self.events if e[0] == "mouse"]


def create_backend() -> InputBackend:
    """The right backend for this machine, or one that refuses and says why."""
    if _IS_WINDOWS:                                   # pragma: no cover
        try:
            return WindowsInputBackend()
        except Exception as e:
            return UnavailableBackend(
                f"Could not set up Windows input ({type(e).__name__})."
            )
    return UnavailableBackend(
        f"Sending input to Minecraft is only implemented on Windows. On "
        f"{platform.system()} it would need xdotool or an X11 binding, and "
        f"this package is deliberately not allowed to start subprocesses. "
        f"Observing the game still works."
    )


__all__ = [
    "ALLOWED_KEYS", "InputBackend", "UnavailableBackend", "FakeInputBackend",
    "create_backend", "validate_key",
]
