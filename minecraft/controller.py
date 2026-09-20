"""
minecraft/controller.py — bounded actions, and every way they can be stopped.

THE FAILURE THIS FILE PREVENTS
    JARVIS believes Minecraft has focus
    → the user alt-tabs
    → JARVIS is still holding W
    → JARVIS is typing into their email

    That is prevented mechanically, not by remembering to check. `_guard()`
    asks four questions — session live, process alive, window present, window
    foreground — and it is called before an action starts AND on every tick
    while a key is held. A held key therefore cannot outlive focus by more than
    one tick (~40ms).

FIVE INDEPENDENT STOPS
    No single mechanism has to be right:

      1. focus loss      — the guard, every tick, and the watcher between actions
      2. F12             — minecraft/emergency.py, its own thread, global on Windows
      3. deadman         — the supervisor releases anything held too long, even
                           if the action thread is wedged and never ticks again
      4. session expiry  — the session's clock, and the watcher
      5. process death   — the guard and the watcher
      plus atexit, and a `finally` on every action.

    Each of them ends at the same place: `ledger.release_all()`, which is
    idempotent, takes no arguments and cannot be refused.

HONEST RESULTS
    An action that was cut short says so. A 1.0s move that lost focus at 300ms
    returns ok=False, actual_duration_ms=300, stopped_reason="focus_lost".
    Every later phase — verification, replanning — is built on the assumption
    that this number is true, so it is never rounded up to what was asked for.
"""

from __future__ import annotations

import atexit
import threading
import time
from dataclasses import dataclass, field

from minecraft import action_spec, process as mc_process
from minecraft.emergency import EmergencyStopWatcher
from minecraft.errors import (
    EmergencyStop, InputBackendUnavailable, InvalidAction, MinecraftNotRunning,
    NoActiveSession, SessionExpired, WindowNotFocused, WindowNotFound,
)
from minecraft.input_backend import create_backend
from minecraft.ledger import InputLedger, MAX_HOLD_SECONDS
from minecraft.session import SessionManager
from minecraft.window import Locator

# How often a held action re-checks that the world is still as it was. 40ms is
# well under a Minecraft tick (50ms), so the worst case is that one game tick
# of movement happens after focus is lost.
TICK_SECONDS = 0.04

# The supervisor's own cadence. Slower: it only exists for the case where the
# action loop has stopped ticking at all.
SUPERVISOR_SECONDS = 0.2

# How long past MAX_HOLD_SECONDS the supervisor waits before force-releasing.
# Enough that a normal action finishing its own release is never pre-empted.
DEADMAN_GRACE_SECONDS = 0.5


@dataclass(frozen=True)
class ActionResult:
    """What actually happened. Built so success cannot be claimed by accident."""

    ok: bool
    action: str
    requested: dict = field(default_factory=dict)
    actual_duration_ms: int = 0
    stopped_reason: str | None = None
    window_focused_throughout: bool = False
    clamped: bool = False
    released: tuple = ()
    error_class: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "requested": self.requested,
            "actual_duration_ms": self.actual_duration_ms,
            "stopped_reason": self.stopped_reason,
            "window_focused_throughout": self.window_focused_throughout,
            "clamped": self.clamped,
            "released": list(self.released),
            "error_class": self.error_class,
            "error": self.error,
        }

    def describe(self) -> str:
        if self.ok:
            note = " (shortened to the limit)" if self.clamped else ""
            return f"{self.action}: done in {self.actual_duration_ms}ms{note}."
        if self.stopped_reason:
            return (f"{self.action}: stopped after {self.actual_duration_ms}ms "
                    f"— {self.stopped_reason}.")
        return f"{self.action} did not happen: {self.error}"


class MinecraftController:
    """Drives Minecraft within a session, and stops on anything unexpected.

    Every collaborator is injectable, because the tests run with no Minecraft,
    no window and no keyboard — and because the thing under test is the
    guarding, not the typing."""

    def __init__(self, backend=None, locator=None, sessions=None,
                 process_module=None, emergency=None, start_watchers=True):
        self._backend = backend if backend is not None else create_backend()
        self._locator = locator if locator is not None else Locator()
        self._sessions = sessions if sessions is not None else SessionManager()
        self._process = process_module if process_module is not None else mc_process
        self._ledger = InputLedger(self._backend)

        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._last_stop_reason = ""
        self._action_in_flight = ""

        self._emergency = emergency if emergency is not None else \
            EmergencyStopWatcher(on_stop=self._on_emergency)
        self._supervisor: threading.Thread | None = None
        self._supervisor_stop = threading.Event()
        self._watchers_enabled = start_watchers

        self._sessions.on_end(self._on_session_end)

        # Last line of defence. If the interpreter is going down with keys
        # held, they come up on the way out.
        atexit.register(self._atexit_release)

    # ── accessors ────────────────────────────────────────────────────────────

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    @property
    def ledger(self) -> InputLedger:
        return self._ledger

    @property
    def backend(self):
        return self._backend

    @property
    def emergency(self) -> EmergencyStopWatcher:
        return self._emergency

    # ── the guard ────────────────────────────────────────────────────────────

    def _guard(self) -> str:
        """'' when it is safe to send input, else the reason it is not.

        A string rather than an exception because this runs on every tick of
        every held key, and the tick path must not pay for exception
        construction or risk one escaping past the release."""
        if self._cancel.is_set():
            return self._last_stop_reason or "stop_requested"

        session = self._sessions.current
        if session is None:
            return "no_session"
        if session.cancel.is_set():
            return "session_cancelled"
        if session.expired:
            return "session_expired"
        if not session.active:
            return "session_inactive"

        if not self._process.is_alive(self._locator.pid):
            return "process_gone"

        info = self._locator.probe()
        if not info.found:
            return "window_gone"
        if not info.focus_known:
            return "focus_unknown"
        if not info.foreground:
            return "focus_lost"
        if info.rect is None or not info.rect.usable:
            return "window_unusable"
        return ""

    def _require_ready(self) -> None:
        """Turn a guard failure into the right exception, for the pre-flight
        check. During an action the string form is used instead."""
        reason = self._guard()
        if not reason:
            return
        error = {
            "no_session": NoActiveSession(
                "There is no Minecraft control session. Ask me to start one — "
                "you will need to confirm it on screen."),
            "session_expired": SessionExpired(
                "The Minecraft session has run out. Start another if you want "
                "me to keep playing."),
            "session_cancelled": EmergencyStop("session cancelled"),
            "session_inactive": NoActiveSession("The session has ended."),
            "stop_requested": EmergencyStop(self._last_stop_reason or "stopped"),
            "process_gone": MinecraftNotRunning(
                "Minecraft is not running any more."),
            "window_gone": WindowNotFound(
                "I cannot find the Minecraft window any more."),
            "focus_unknown": WindowNotFocused(
                "I cannot tell which window has focus on this platform, so I "
                "will not send input to Minecraft."),
            "focus_lost": WindowNotFocused(
                "Minecraft is not the active window, so I will not send it "
                "any input — it would go to whatever is in front instead."),
            "window_unusable": WindowNotFound(
                "The Minecraft window is too small to be the game right now."),
        }.get(reason, EmergencyStop(reason))
        # The guard already knew exactly which condition failed. Keep it, so
        # the result reports the real reason rather than one inferred from the
        # exception class.
        error.guard_reason = reason
        raise error

    # ── stopping ─────────────────────────────────────────────────────────────

    def stop(self, reason: str = "requested") -> dict:
        """Stop everything, now. Never raises, never refuses, idempotent.

        Reachable from the F12 watcher, the `stop` action, the supervisor, a
        session ending and `atexit`."""
        with self._lock:
            self._last_stop_reason = reason
            self._cancel.set()

        report = self._ledger.release_all()
        ended = self._sessions.end(reason)

        return {
            "stopped": True,
            "reason": reason,
            "released": list(report.released),
            "failed_to_release": list(report.failed),
            "session_ended": bool(ended),
            "clean": report.clean,
        }

    def _on_emergency(self, reason: str) -> None:
        self.stop(f"emergency stop ({reason})")

    def _on_session_end(self, _session_id: str, reason: str) -> None:
        """A session ending — for any cause — releases held keys.

        Registered as a session listener so expiry, which nothing else is
        watching for, still comes back here."""
        try:
            self._ledger.release_all()
        except Exception:
            pass

    def _atexit_release(self) -> None:                # pragma: no cover
        try:
            self._ledger.release_all()
        except Exception:
            pass

    # ── the supervisor ───────────────────────────────────────────────────────

    def _start_watchers(self) -> None:
        if not self._watchers_enabled:
            return
        self._emergency.start()
        if self._supervisor is not None and self._supervisor.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor = threading.Thread(
            target=self._supervise, name="minecraft-supervisor", daemon=True)
        self._supervisor.start()

    def _stop_watchers(self) -> None:
        self._supervisor_stop.set()
        thread, self._supervisor = self._supervisor, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        try:
            self._emergency.stop_watching()
        except Exception:
            pass

    def _supervise(self) -> None:
        """Independent of the action loop, which is the point.

        If an action thread hangs — a wedged backend call, a blocked probe —
        nothing in the action path will ever release its key. This thread will,
        and it also ends sessions that expired while nothing was happening."""
        while not self._supervisor_stop.is_set():
            try:
                overdue = self._ledger.overdue(MAX_HOLD_SECONDS
                                               + DEADMAN_GRACE_SECONDS)
                if overdue:
                    self.stop(f"deadman timeout: {', '.join(overdue)} held too long")
                elif self._ledger.is_holding():
                    reason = self._guard()
                    if reason:
                        self.stop(reason)
                else:
                    session = self._sessions.current
                    if session is not None and not session.active:
                        self._sessions.end("expired")
            except Exception:
                pass          # the supervisor must outlive every failure
            self._supervisor_stop.wait(SUPERVISOR_SECONDS)

    # ── sessions ─────────────────────────────────────────────────────────────

    def start_session(self, duration_s: float | None = None,
                      owner: str = "user") -> dict:
        """Open a control session. The broker has already confirmed by here.

        Attaching to the window first means a session cannot open against a
        game that is not there — the failure arrives before the grant rather
        than on the first movement."""
        info = self._locator.attach()
        if not info.found:
            raise WindowNotFound(info.detail or
                                 "I could not find the Minecraft window.")

        with self._lock:
            self._cancel.clear()
            self._last_stop_reason = ""

        session = self._sessions.start(duration_s=duration_s, owner=owner)
        self._start_watchers()

        return {
            "session": session.as_dict(),
            "window": info.as_dict(),
            "emergency_stop": self._emergency.describe(),
            "emergency_scope": self._emergency.scope,
            "input_backend": self._backend.describe(),
            "input_available": bool(getattr(self._backend, "available", False)),
        }

    def end_session(self, reason: str = "ended by request") -> dict:
        result = self.stop(reason)
        self._stop_watchers()
        return result

    # ── actions ──────────────────────────────────────────────────────────────

    def _hold_for(self, key: str, seconds: float, action: str,
                  requested: dict, clamped: bool) -> ActionResult:
        """Hold one key for up to `seconds`, re-checking the world every tick.

        The `finally` is the important line in this function: whatever happens
        — a guard failure, a backend error, a cancelled session, an exception
        nobody predicted — the key comes up before this returns."""
        started = time.monotonic()
        stopped_reason: str | None = None
        focused_throughout = True
        error_class = error = ""
        released: tuple = ()

        try:
            self._require_ready()
        except Exception as e:
            return ActionResult(
                ok=False, action=action, requested=requested,
                actual_duration_ms=0, stopped_reason=_reason_for(e),
                window_focused_throughout=False, clamped=clamped,
                error_class=type(e).__name__, error=str(e),
            )

        with self._lock:
            self._action_in_flight = action

        try:
            self._ledger.hold(key)
            deadline = started + min(seconds, MAX_HOLD_SECONDS)

            while time.monotonic() < deadline:
                reason = self._guard()
                if reason:
                    stopped_reason = reason
                    focused_throughout = reason != "focus_lost" and focused_throughout
                    if reason in ("focus_lost", "focus_unknown"):
                        focused_throughout = False
                    break
                remaining = deadline - time.monotonic()
                time.sleep(min(TICK_SECONDS, max(0.0, remaining)))

        except InputBackendUnavailable as e:
            error_class, error = type(e).__name__, str(e)
            stopped_reason = "input_unavailable"
        except Exception as e:                        # pragma: no cover
            error_class, error = type(e).__name__, str(e)
            stopped_reason = "error"
        finally:
            report = self._ledger.release_all()
            released = report.released
            if not report.clean:
                error_class = error_class or "ReleaseFailed"
                error = (error or
                         f"I could not release {', '.join(report.failed)}. "
                         f"Press those keys yourself to be sure they are up.")
            with self._lock:
                self._action_in_flight = ""

        elapsed_ms = int((time.monotonic() - started) * 1000)
        ok = stopped_reason is None and not error

        return ActionResult(
            ok=ok, action=action, requested=requested,
            actual_duration_ms=elapsed_ms, stopped_reason=stopped_reason,
            window_focused_throughout=focused_throughout and ok,
            clamped=clamped, released=released,
            error_class=error_class,
            error=error or (_explain(stopped_reason) if stopped_reason else ""),
        )

    def move(self, params: dict) -> ActionResult:
        spec = action_spec.parse_move(params or {})
        return self._hold_for(spec.key, spec.duration, "move",
                              spec.as_dict(), spec.clamped)

    def jump(self, params: dict | None = None) -> ActionResult:
        spec = action_spec.parse_jump(params or {})
        return self._hold_for(spec.key, spec.duration, "jump",
                              spec.as_dict(), spec.clamped)

    def look(self, params: dict) -> ActionResult:
        """One relative mouse delta. Nothing is held, so there is nothing to
        release — but the guard still runs, because a delta delivered to the
        wrong window moves someone else's cursor."""
        spec = action_spec.parse_look(params or {})
        started = time.monotonic()

        try:
            self._require_ready()
        except Exception as e:
            return ActionResult(
                ok=False, action="look", requested=spec.as_dict(),
                stopped_reason=_reason_for(e), clamped=spec.clamped,
                error_class=type(e).__name__, error=str(e),
            )

        try:
            self._ledger.move_mouse(spec.dx, spec.dy)
        except Exception as e:
            return ActionResult(
                ok=False, action="look", requested=spec.as_dict(),
                actual_duration_ms=int((time.monotonic() - started) * 1000),
                stopped_reason="input_unavailable", clamped=spec.clamped,
                error_class=type(e).__name__, error=str(e),
            )

        return ActionResult(
            ok=True, action="look", requested=spec.as_dict(),
            actual_duration_ms=int((time.monotonic() - started) * 1000),
            window_focused_throughout=True, clamped=spec.clamped,
        )

    # ── status ───────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Everything a planner or a person needs to know, without acting."""
        info = self._locator.probe()
        process_info = self._process.find()
        session = self._sessions.current

        return {
            "minecraft_running": process_info.running,
            "pid": process_info.pid,
            "process_detail": process_info.detail,
            "window": info.as_dict(),
            "controllable": info.controllable,
            "session": session.as_dict() if session else None,
            "session_active": self._sessions.is_active(),
            "holding": sorted(self._ledger.held()),
            "action_in_flight": self._action_in_flight,
            "input_backend": self._backend.describe(),
            "input_available": bool(getattr(self._backend, "available", False)),
            "emergency_stop": self._emergency.describe(),
            "emergency_scope": self._emergency.scope,
            "limits": action_spec.limits(),
        }


def _reason_for(error: Exception) -> str:
    """The guard vocabulary for a pre-flight exception.

    Prefers the reason the guard actually produced — two conditions can share
    an exception class (focus_lost and focus_unknown are both WindowNotFocused)
    and collapsing them loses the one piece of information a planner needs to
    decide whether retrying could ever work."""
    carried = getattr(error, "guard_reason", "")
    if carried:
        return carried
    return {
        NoActiveSession: "no_session",
        SessionExpired: "session_expired",
        WindowNotFocused: "focus_lost",
        WindowNotFound: "window_gone",
        MinecraftNotRunning: "process_gone",
        EmergencyStop: "stopped",
        InvalidAction: "invalid_action",
        InputBackendUnavailable: "input_unavailable",
    }.get(type(error), "refused")


def _explain(reason: str | None) -> str:
    """A sentence for a reason code, for the model to relay to the user."""
    return {
        "focus_lost": "Minecraft stopped being the active window, so I let go "
                      "of everything and stopped.",
        "focus_unknown": "I could not confirm Minecraft had focus, so I "
                         "stopped rather than risk sending keys elsewhere.",
        "process_gone": "Minecraft closed, so I let go of everything.",
        "window_gone": "The Minecraft window disappeared, so I stopped.",
        "window_unusable": "The Minecraft window is no longer a usable size.",
        "session_expired": "The control session ran out mid-action.",
        "session_cancelled": "The session was cancelled.",
        "session_inactive": "The session had already ended.",
        "no_session": "There was no active control session.",
        "stop_requested": "A stop was requested.",
        "stopped": "A stop was requested.",
        "input_unavailable": "I could not send input on this machine.",
        "deadman": "A key was held too long and was released automatically.",
    }.get(reason or "", "")


__all__ = ["MinecraftController", "ActionResult", "TICK_SECONDS",
           "SUPERVISOR_SECONDS", "DEADMAN_GRACE_SECONDS"]
