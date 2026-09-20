"""
minecraft/emergency.py — the stop that does not depend on anything working.

WHY THIS IS ITS OWN MODULE
    An emergency stop that lives inside the thing it stops is not an emergency
    stop. If the controller's action thread is wedged, or the planner is in a
    loop, or Gemini is mid-request, the user still has to be able to make the
    keys come up — and the path from their finger to `release_all()` must not
    pass through any of that.

    So: a dedicated thread, polling a key, holding a reference to one callback.
    It imports nothing from the controller. It makes no decisions. It does not
    ask permission — a safety control that policy can refuse is not a safety
    control.

WHY F12, AND WHAT ELSE ALREADY WORKS
    F12 is unbound in vanilla Minecraft, so pressing it does nothing to the
    game while the watcher sees it. It is configurable.

    But the mechanism the user already has, and already knows, is Alt-Tab: the
    moment Minecraft stops being the foreground window, `controller.py` sees it
    on its next tick and releases everything. That is the primary stop, it
    needs no new muscle memory, and it works even if this module never starts.
    F12 is for the case where the game is focused and the user wants it to stop
    without leaving.

PLATFORM
    Windows gets a real global hook — `GetAsyncKeyState` polled at 30Hz, the
    same mechanism `core/hotkey.py` already uses for push-to-talk, which is
    proven to work while another window has focus. Elsewhere there is no
    portable way to read global key state without a new dependency or an
    accessibility grant, so `scope` reports "unavailable" and the module says
    so rather than pretending. The other five stop paths still apply.
"""

from __future__ import annotations

import platform
import threading

_IS_WINDOWS = platform.system() == "Windows"

# Virtual-key codes, for the poller. Not scan codes — GetAsyncKeyState takes
# VKs, unlike the injection path in input_backend.py.
_VK = {
    "f12": 0x7B, "f11": 0x7A, "f10": 0x79, "f9": 0x78, "f8": 0x77,
    "pause": 0x13, "scrolllock": 0x91,
}

DEFAULT_KEY = "f12"
"""Unbound in vanilla Minecraft, so pressing it costs nothing in-game."""

_POLL_HZ = 30.0
_DEBOUNCE_S = 0.03


class EmergencyStopWatcher:
    """Polls one key and calls `on_stop()` when it goes down.

    `on_stop` must be cheap, must not raise, and must not depend on the
    controller being in a good state — in practice it is a closure that calls
    `release_all()` and ends the session."""

    def __init__(self, on_stop, key: str = DEFAULT_KEY):
        self._on_stop = on_stop
        self._key = str(key or DEFAULT_KEY).lower()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._scope = "unavailable"
        self._fired = 0

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def scope(self) -> str:
        """'global' when a system-wide hook is running, else 'unavailable'.

        Reported honestly in `status` so the user knows whether F12 will
        actually reach them while the game has focus."""
        return self._scope

    @property
    def key(self) -> str:
        return self._key

    @property
    def fired(self) -> int:
        return self._fired

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> str:
        """Begin watching. Returns the scope actually achieved.

        Safe to call when already running, and safe to call when the platform
        cannot support it."""
        self.stop_watching()
        self._stop.clear()

        if not (_IS_WINDOWS and self._can_poll()):
            self._scope = "unavailable"
            return self._scope

        self._scope = "global"
        self._thread = threading.Thread(target=self._loop,
                                        name="minecraft-estop", daemon=True)
        self._thread.start()
        return self._scope

    def stop_watching(self) -> None:
        """Stop the watcher itself. Does NOT trigger a stop."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def trigger(self, reason: str = "manual") -> None:
        """Fire the stop directly. Used by the `stop` action and by tests, so
        the same path is exercised whether a finger or an API asked."""
        self._fire(reason)

    # ── internals ────────────────────────────────────────────────────────────

    def _can_poll(self) -> bool:
        try:
            import ctypes
            ctypes.windll.user32.GetAsyncKeyState     # noqa: B018 — presence
            return self._key in _VK
        except Exception:
            return False

    def _fire(self, reason: str) -> None:
        self._fired += 1
        try:
            self._on_stop(reason)
        except Exception:
            # Nothing above this catches. An exception here would kill the
            # watcher thread and take the emergency stop with it.
            pass

    def _loop(self) -> None:                          # pragma: no cover
        import ctypes
        import time
        user32 = ctypes.windll.user32
        code = _VK[self._key]
        period = 1.0 / _POLL_HZ
        down_since = 0.0
        already_fired = False

        while not self._stop.is_set():
            try:
                down = bool(user32.GetAsyncKeyState(code) & 0x8000)
            except Exception:
                break                                  # session teardown
            now = time.monotonic()
            if down:
                if down_since == 0.0:
                    down_since = now
                elif not already_fired and (now - down_since) >= _DEBOUNCE_S:
                    already_fired = True
                    self._fire(f"{self._key.upper()} pressed")
            else:
                down_since = 0.0
                already_fired = False        # re-arm on release
            self._stop.wait(period)

        self._scope = "unavailable"

    def describe(self) -> str:
        if self._scope == "global":
            return (f"{self._key.upper()} will stop Minecraft control from "
                    f"anywhere, even while the game has focus.")
        return (f"A global {self._key.upper()} hotkey is not available on "
                f"{platform.system()}. Alt-Tab away from Minecraft to stop "
                f"instantly — that works on every platform — or ask me to stop.")


__all__ = ["EmergencyStopWatcher", "DEFAULT_KEY"]
