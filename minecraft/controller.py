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
from minecraft import gui as gui_mod
from minecraft.emergency import EmergencyStopWatcher
from core import capabilities as core_caps
from minecraft.errors import (
    EmergencyStop, InputBackendUnavailable, InvalidAction,
    MinecraftNotRunning, NoActiveSession, SessionExpired, WindowNotFocused,
    WindowNotFound,
)
from minecraft.input_backend import create_backend
from minecraft.ledger import InputLedger, MAX_HOLD_SECONDS
from minecraft.session import SessionManager
from minecraft.window import Locator

# How often a held action re-checks that the world is still as it was. 40ms is
# well under a Minecraft tick (50ms), so the worst case is that one game tick
# of movement happens after focus is lost.
TICK_SECONDS = 0.04

# How long the window may go unseen before the guard calls it gone: less than
# one hold tick, so a closed game still stops the very next tick, but longer
# than the same enumeration blip seen by two guard calls back to back.
WINDOW_MISS_SPAN_S = 0.03

# A miss more than this after the previous one starts a new run. Without it
# a blip, a long idle with nothing asking, and another blip read as one miss
# seconds long. Over two hold ticks, so a real close -- missed every 40ms
# while a key is held -- is still one run and still stops the next tick.
WINDOW_MISS_RUN_GAP_S = 0.1

# How often, during a hold, the injected hazard check is asked. The bridge
# publishes every 200ms; asking every tick would read the same snapshot five
# times over.
HAZARD_CHECK_SECONDS = 0.1

# The supervisor's own cadence. Slower: it only exists for the case where the
# action loop has stopped ticking at all.
SUPERVISOR_SECONDS = 0.2

# How long past MAX_HOLD_SECONDS the supervisor waits before force-releasing.
# Enough that a normal action finishing its own release is never pre-empted.
DEADMAN_GRACE_SECONDS = 0.5

_CHANGE_CONFIRMATIONS = 2
"""Consecutive probes that must agree the target changed before letting go.

One is not enough: the probe reads a file the game rewrites five times a
second, so a single differing sample can be stale, torn, or the crosshair
grazing a neighbour as the view settles. Two at 40ms apart costs 40ms of
over-mining and removes a whole class of "it held the button for one tick and
nothing broke"."""

FOCUS_WAIT_SECONDS = 4.0
"""How long an action will wait, BEFORE it starts, for Minecraft to come back
to the front.

WHY THIS IS NOT A HOLE IN THE FOCUS GUARD
    Confirming a control session necessarily takes focus away from Minecraft:
    the confirmation is a JARVIS window, and clicking it puts JARVIS in front.
    So the first action after a confirmation was ALWAYS refused with
    focus_lost, and the user saw a burst of commands and a character standing
    still. The guard was right every time and the feature was unusable.

    Waiting is not the same as relaxing. Nothing is sent while the window is
    not focused — the guard still has to pass before a single key goes down,
    and it is re-checked every tick for the whole duration afterwards. The
    only change is that a refusal a second too early becomes a short wait for
    the user to click back on the game.

    It applies only to focus_lost, and only before an action starts. A missing
    session, an expired one, a closed game or a lost window fail immediately
    as they always did, and focus lost DURING an action still stops it inside
    one tick."""


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



SCREEN_BLOCKED_ACTIONS = frozenset({"attack", "mine", "place", "interact",
                                    "use_item", "eat", "drop"})
"""Gameplay holds refused while the bridge reports a screen open: in a
container screen their clicks and keys land in the GUI. The screen actions
(gui_*) and `inventory` are what work inside one, and are not here."""


class MinecraftController:
    """Drives Minecraft within a session, and stops on anything unexpected.

    Every collaborator is injectable, because the tests run with no Minecraft,
    no window and no keyboard — and because the thing under test is the
    guarding, not the typing."""

    def __init__(self, backend=None, locator=None, sessions=None,
                 process_module=None, emergency=None, start_watchers=True,
                 focus_wait_s: float = FOCUS_WAIT_SECONDS,
                 progress_probe=None, hazard_probe=None, clock=None,
                 held_item_probe=None, gui_probe=None, entity_probe=None):
        self._backend = backend if backend is not None else create_backend()
        self._locator = locator if locator is not None else Locator()
        self._sessions = sessions if sessions is not None else SessionManager()
        self._process = process_module if process_module is not None else mc_process
        self._ledger = InputLedger(self._backend)

        self._lock = threading.RLock()
        self._cancel = threading.Event()
        # When the current run of probes that could not see the window
        # began, or None. A run shorter than WINDOW_MISS_SPAN_S is tolerated;
        # see _guard.
        self._clock = clock if clock is not None else time.monotonic
        self._first_miss_at = None
        self._last_miss_at = None
        self._last_stop_reason = ""
        self._action_in_flight = ""

        # Cancelling the CURRENT action without ending the session: what a
        # spoken "stop" or a withdrawn tool call means. Each action records the
        # epoch it started in and stops the moment it changes, so a cancel
        # reaches exactly the actions already running -- never one that starts
        # afterwards, and never the session itself. See cancel_current().
        self._abort_epoch = 0
        self._abort_reason = ""
        # A running task's own cancel flag, checked with the guard while one of
        # its steps holds input. Closes the gap between the runner checking
        # for a cancel and the step it had already decided on pressing keys.
        self._task_cancel: threading.Event | None = None
        # One input action at a time. Tasks now run off the voice thread, so a
        # direct command and a task step could otherwise both hold keys, and
        # two holders of one ledger each release the other's inputs early.
        # Acquired without waiting: a second action is refused, not queued.
        self._input_slot = threading.Lock()

        self._emergency = emergency if emergency is not None else \
            EmergencyStopWatcher(on_stop=self._on_emergency)
        self._supervisor: threading.Thread | None = None
        self._supervisor_stop = threading.Event()
        self._watchers_enabled = start_watchers
        # Injectable so the tests that assert a focus refusal do not each
        # spend the grace period waiting for a fake window to come forward.
        self._focus_wait_s = max(0.0, float(focus_wait_s))

        # A callable returning something comparable that changes when the
        # thing being mined changes. A plain callable rather than a state
        # source: this package stays input-only, and the controller should
        # not learn how to read the game just to know when to stop pressing.
        self._progress_probe = progress_probe

        # `hazard_probe(action)` -> a check to call while that action's keys
        # are down, returning a sentence when the player is in danger, or
        # None for no check. Injected for the same reason: the controller
        # lets go, the caller decides what danger is.
        self._hazard_probe = hazard_probe
        # Asked before `place`: the held item's name, "" for an empty hand,
        # None when it cannot be read. Injected like the others; without it,
        # `place` refuses, because it cannot see what it would right-click.
        self._held_item_probe = held_item_probe
        # Asked before every pointer move and click in a screen: a fresh
        # bridge reading (screen, slots, pointer, health, mobs), or None.
        # minecraft/gui.py's rules decide from it; without it, no click.
        self._gui_probe = gui_probe
        self._gui_health_peak = None
        # Asked before an attack that expects a hostile: the crosshair's
        # entity as (name, category), or None. Without it, such an attack
        # presses nothing.
        self._entity_probe = entity_probe

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
            # Tolerate a BLIP. Enumerating windows can fail transiently --
            # it did, spectacularly, when the ctypes prototypes were being
            # re-declared from several threads at once -- and treating a
            # blip as "the game closed" ends the task and costs the user a
            # fresh confirmation for something that never happened.
            #
            # Measured in time, not in calls. This runs from the hold loop,
            # the supervisor and the runner, sometimes back to back, so
            # "the second miss" could be the same blip seen twice a few
            # microseconds apart. A real close never recovers, so misses
            # spanning WINDOW_MISS_SPAN_S -- less than one 40ms hold tick --
            # still stop everything: patience for a blip, not a reason to
            # keep pressing keys at a window that is gone.
            now = self._clock()
            with self._lock:
                if self._first_miss_at is None or (
                        self._last_miss_at is not None
                        and now - self._last_miss_at > WINDOW_MISS_RUN_GAP_S):
                    self._first_miss_at = now        # a new run of misses
                self._last_miss_at = now
                gone = now - self._first_miss_at >= WINDOW_MISS_SPAN_S
            return "window_gone" if gone else ""
        with self._lock:
            self._first_miss_at = None
            self._last_miss_at = None
        if not info.focus_known:
            return "focus_unknown"
        if not info.foreground:
            return "focus_lost"
        if info.rect is None or not info.rect.usable:
            return "window_unusable"
        return ""

    def _probe(self):
        """The current value of whatever the caller said to watch, or None.

        Never raises and never blocks for long: this runs inside the hold
        loop, and an exception here would escape past the release."""
        if self._progress_probe is None:
            return None
        try:
            return self._progress_probe()
        except Exception:
            return None

    def _hazard_for(self, action: str):
        """This hold's danger check, or None. Never raises."""
        if self._hazard_probe is None:
            return None
        try:
            check = self._hazard_probe(action)
        except Exception:
            return None
        return check if callable(check) else None

    @staticmethod
    def _danger(check) -> str:
        """A danger sentence, or "". A check that fails is no danger: the
        hold is still bounded by its duration, the deadman and the guard."""
        try:
            return str(check() or "")
        except Exception:
            return ""

    def _await_focus(self, seconds: float | None = None,
                     epoch: int | None = None) -> str:
        """Wait briefly for Minecraft to come back to the front.

        Returns the guard's verdict when it stops waiting. Only `focus_lost`
        is waited on: every other refusal is a condition that waiting cannot
        fix, and pausing on those would just make failure slower.

        See FOCUS_WAIT_SECONDS for why this does not weaken the guard."""
        if seconds is None:
            seconds = self._focus_wait_s
        deadline = time.monotonic() + max(0.0, seconds)
        reason = self._guard()
        while reason == "focus_lost" and time.monotonic() < deadline:
            if self._cancel.is_set():
                break
            if epoch is not None and self._aborted(epoch):
                break
            time.sleep(TICK_SECONDS)
            reason = self._guard()
        return reason

    def _aborted(self, epoch: int) -> str:
        """Why the action that started in `epoch` should stop, or ''.

        Separate from `_guard` on purpose. The guard's failures are safety
        events -- the supervisor answers them by ending the session -- while
        this is a person saying "stop that", which releases the keys and
        leaves the session they approved exactly as it was."""
        if self._abort_epoch != epoch:
            return "cancelled"
        task_cancel = self._task_cancel
        if task_cancel is not None and task_cancel.is_set():
            return "cancelled"
        return ""

    def _cancelled_result(self, action: str, requested: dict,
                          clamped: bool, elapsed_ms: int = 0,
                          released: tuple = ()) -> ActionResult:
        return ActionResult(
            ok=False, action=action, requested=requested,
            actual_duration_ms=elapsed_ms, stopped_reason="cancelled",
            window_focused_throughout=False, clamped=clamped,
            released=released, error_class="Cancelled",
            error=(f"Cancelled: {self._abort_reason or 'stop requested'}. "
                   f"Every key and button is released; the session is "
                   f"still open."),
        )

    def _busy_result(self, action: str, requested: dict,
                     clamped: bool) -> ActionResult:
        running = self._action_in_flight or "another action"
        return ActionResult(
            ok=False, action=action, requested=requested,
            stopped_reason="busy", clamped=clamped, error_class="Busy",
            error=(f"A Minecraft action ({running}) is still running. I "
                   f"pressed nothing; one action at a time."),
        )

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
                "Minecraft is not the active window. I waited a few seconds "
                "for it to come to the front and it did not, so I sent "
                "nothing — the keys would have gone to whatever is in front "
                "instead. Click on the Minecraft window and ask me again."),
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

    def release_inputs(self) -> dict:
        """Let go of everything, without ending the session or cancelling.

        The difference from `stop()` matters. `stop()` is for a safety event:
        it sets the sticky cancel flag and ends the session, and nothing works
        again until a new session is opened. That is right for F12 and for
        focus loss, and badly wrong as a response to a recoverable error —
        using it there turns "that request did not make sense" into "this
        controller is dead until you notice why".

        This is the version for the second case: the keys come up, the session
        survives, and the next action can proceed."""
        report = self._ledger.release_all()
        return {"released": list(report.released),
                "failed_to_release": list(report.failed),
                "clean": report.clean}

    def cancel_current(self, reason: str = "cancelled") -> dict:
        """Stop whatever action is running now, and keep the session.

        What "stop" means when a person says it to a player mid-task: let go,
        stand still, and wait -- not "revoke the permission I gave you". The
        running action stops inside one tick, every input comes up, and the
        next command works without a new confirmation.

        Never refused and never blocks, like every other stop here. It is not
        a replacement for F12 or `stop()`: those end the session, and remain
        the hard stop."""
        with self._lock:
            self._abort_epoch += 1
            self._abort_reason = str(reason or "cancelled")[:120]
        report = self._ledger.release_all()
        return {
            "cancelled": True,
            "reason": self._abort_reason,
            "released": list(report.released),
            "failed_to_release": list(report.failed),
            "session_ended": False,
            "clean": report.clean,
        }

    def cancellable(self, event: threading.Event) -> "_TaskCancelScope":
        """Tie the actions run inside a `with` block to a task's cancel flag.

        The task runner wraps each step in this, so cancelling the task stops
        the step already in progress rather than only preventing the next."""
        return _TaskCancelScope(self, event)

    @property
    def busy(self) -> bool:
        """An input action is running right now."""
        return self._input_slot.locked()

    def emergency_stop(self, reason: str = "emergency stop") -> dict:
        """Stop now. Never refused, never confirmed, callable from anywhere.

        The same code path as F12 and as every automatic stop, exposed under
        the name the rest of the system uses for it. It takes no capability
        and asks no permission on purpose: a safety control that policy can
        decline is not a safety control."""
        return self.stop(reason)

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
                      owner: str = "user",
                      authorized: bool = True) -> dict:
        """Open a control session. The broker has already confirmed by here.

        Attaching to the window first means a session cannot open against a
        game that is not there — the failure arrives before the grant rather
        than on the first movement.

        ASKING TWICE IS NOT AN ERROR
            A model that has lost track and asks for control it already has is
            an ordinary thing to happen, and it used to be catastrophic: the
            underlying manager raised, the adapter's catch-all treated it as an
            unexpected failure and called stop(), and stop() ended the live
            session AND set the sticky cancel flag. So a redundant request
            destroyed the working session and wedged the controller — every
            later action was refused with a stale error string, and no amount
            of confirming could clear it because only start_session clears that
            flag.

            So a request that a live session already satisfies now returns that
            session. The one case that still needs a new one is a request for
            interaction when the current grant does not include it: grants are
            not widened in place, because the confirmation the user answered
            described a narrower session than the one they would end up with."""
        info = self._locator.attach()
        if not info.found:
            raise WindowNotFound(info.detail or
                                 "I could not find the Minecraft window.")

        existing = self._sessions.current
        reused = False
        if existing is not None and existing.active:
            if authorized and not existing.authorized:
                self._sessions.end("replaced by an authorised session")
            else:
                reused = True

        with self._lock:
            self._cancel.clear()
            self._last_stop_reason = ""

        if reused:
            session = existing
        else:
            session = self._sessions.start(duration_s=duration_s, owner=owner,
                                           authorized=authorized)
        self._start_watchers()

        return {
            "session": session.as_dict(),
            "reused_existing": reused,
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
        """One key. Kept as the narrow case of `_hold_inputs`."""
        return self._hold_inputs((key,), (), seconds, action, requested,
                                 clamped)

    def _hold_inputs(self, keys: tuple, buttons: tuple, seconds: float,
                     action: str, requested: dict, clamped: bool,
                     stop_when_changed: bool = False,
                     expect_target: tuple | None = None,
                     expect_face: str | None = None,
                     expect_hostile: bool = False) -> ActionResult:
        """Hold keys and/or mouse buttons for up to `seconds`, re-checking the
        world every tick.

        The `finally` is the important line in this function: whatever happens
        — a guard failure, a backend error, a cancelled session, an exception
        nobody predicted — everything comes up before this returns. That now
        includes mouse buttons, which matter more than keys: a left-click that
        outlives focus lands on whatever window is in front.

        Every input goes down through the ledger before the timing loop starts,
        so a failure part-way through a multi-key hold still has every earlier
        input recorded and therefore released.

        `expect_target`, when given, is the (x, y, z) the progress probe must
        report BEFORE anything is pressed. Mining a block the crosshair is not
        confirmed to be on is how the wrong thing gets broken, so a mismatch,
        or a probe that cannot say, refuses the action with nothing pressed.
        `expect_face` adds which face of it -- placing against the wrong face
        puts the block in the wrong cell."""
        # One input action at a time, refused rather than queued -- see
        # _input_slot. Outside the try below on purpose: an action that never
        # got the slot must not release inputs another action is holding.
        if not self._input_slot.acquire(blocking=False):
            return self._busy_result(action, requested, clamped)
        try:
            return self._hold_inputs_owned(keys, buttons, seconds, action,
                                           requested, clamped,
                                           stop_when_changed, expect_target,
                                           expect_face, expect_hostile)
        finally:
            self._input_slot.release()

    def _hold_inputs_owned(self, keys, buttons, seconds, action, requested,
                           clamped, stop_when_changed, expect_target,
                           expect_face=None, expect_hostile=False):
        """`_hold_inputs`, once this thread owns the input slot."""
        started = time.monotonic()
        epoch = self._abort_epoch
        stopped_reason: str | None = None
        focused_throughout = True
        error_class = error = ""
        released: tuple = ()

        self._await_focus(epoch=epoch)

        try:
            self._require_ready()
        except Exception as e:
            return ActionResult(
                ok=False, action=action, requested=requested,
                actual_duration_ms=0, stopped_reason=_reason_for(e),
                window_focused_throughout=False, clamped=clamped,
                error_class=type(e).__name__, error=str(e),
            )

        if self._aborted(epoch):
            return self._cancelled_result(action, requested, clamped)

        if action in SCREEN_BLOCKED_ACTIONS:
            screen = self._open_screen()
            if screen:
                return ActionResult(
                    ok=False, action=action, requested=requested,
                    stopped_reason="screen_open", clamped=clamped,
                    error_class="ScreenOpen",
                    error=(f"A {' '.join(str(screen).split('_'))} screen is "
                           f"open, so a {action} now would land in the "
                           f"screen, not the world -- a click moves a "
                           f"stack, the drop key throws one. Nothing was "
                           f"pressed. Close it first (inventory close)."),
                )

        if action == "inventory_close":
            problem = self._close_refusal()
            if problem:
                return ActionResult(
                    ok=False, action=action, requested=requested,
                    stopped_reason="no_screen", clamped=clamped,
                    error_class="NoScreenOpen", error=problem,
                )

        if expect_hostile:
            mismatch = self._entity_refusal()
            if mismatch:
                return ActionResult(
                    ok=False, action=action, requested=requested,
                    stopped_reason="target_not_confirmed", clamped=clamped,
                    error_class="TargetNotConfirmed", error=mismatch,
                )

        if expect_target is not None:
            mismatch = self._target_mismatch(expect_target, expect_face)
            if mismatch:
                return ActionResult(
                    ok=False, action=action, requested=requested,
                    stopped_reason="target_not_confirmed", clamped=clamped,
                    error_class="TargetNotConfirmed", error=mismatch,
                )

        with self._lock:
            self._action_in_flight = action

        # Danger already present: press nothing at all.
        hazard = self._hazard_for(action)
        danger = self._danger(hazard) if hazard is not None else ""
        if danger:
            with self._lock:
                self._action_in_flight = ""
            return ActionResult(
                ok=False, action=action, requested=requested,
                actual_duration_ms=0, stopped_reason="danger",
                clamped=clamped, error_class="Danger",
                error=f"I did not start: {danger}.",
            )
        next_hazard_check = time.monotonic() + HAZARD_CHECK_SECONDS

        baseline = self._probe() if stop_when_changed else None
        finished_early = False
        changed_for = 0

        try:
            for key in keys:
                self._ledger.hold(key)
            for button in buttons:
                self._ledger.hold_button(button)
            deadline = started + min(seconds, MAX_HOLD_SECONDS)

            while time.monotonic() < deadline:
                reason = self._guard()
                if reason:
                    stopped_reason = reason
                    focused_throughout = reason != "focus_lost" and focused_throughout
                    if reason in ("focus_lost", "focus_unknown"):
                        focused_throughout = False
                    break

                # A person said stop, or the task this step belongs to was
                # cancelled. Checked every tick like the guard, and answered
                # the same way -- everything comes up in the `finally` -- but
                # without ending the session. See _aborted.
                if self._aborted(epoch):
                    stopped_reason = "cancelled"
                    break

                # Danger, checked while the keys are down: a mine holds for
                # up to ten seconds, and a zombie arriving one second in used
                # to get the other nine. Every HAZARD_CHECK_SECONDS rather than
                # every tick -- the bridge only publishes five times a second.
                if hazard is not None and time.monotonic() >= next_hazard_check:
                    next_hazard_check = time.monotonic() + HAZARD_CHECK_SECONDS
                    danger = self._danger(hazard)
                    if danger:
                        stopped_reason = "danger"
                        break

                # Mining: let go once the target has changed. Holding on past
                # that wastes the rest of the budget and starts breaking
                # whatever was revealed behind it.
                #
                # TWO readings, not one. The probe reads a file the game
                # rewrites five times a second, so a single differing sample
                # can be a torn or momentarily stale read, or the crosshair
                # grazing a neighbouring block as the head settles. Releasing
                # on that would cut the hold to one tick and break nothing --
                # the same symptom as not mining at all, which this code has
                # already worn once.
                if stop_when_changed and baseline is not None:
                    current = self._probe()
                    if current is not None and current != baseline:
                        changed_for += 1
                        if changed_for >= _CHANGE_CONFIRMATIONS:
                            finished_early = True
                            break
                    else:
                        changed_for = 0

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
        if stopped_reason == "cancelled" and not error:
            return self._cancelled_result(action, requested, clamped,
                                          elapsed_ms, released)
        if stopped_reason == "danger" and not error:
            error = f"I let go: {danger}."
        ok = stopped_reason is None and not error
        if finished_early:
            requested = dict(requested)
            requested["stopped_early"] = "the target changed"

        return ActionResult(
            ok=ok, action=action, requested=requested,
            actual_duration_ms=elapsed_ms, stopped_reason=stopped_reason,
            window_focused_throughout=focused_throughout and ok,
            clamped=clamped, released=released,
            error_class=error_class,
            error=error or (_explain(stopped_reason) if stopped_reason else ""),
        )

    def _target_mismatch(self, expected, face=None) -> str:
        """'' when the probe confirms the crosshair is on `expected` (x, y, z)
        -- and on `face` of it, when one is given -- otherwise a sentence
        saying what it is on instead.

        Read at the last moment before input goes down, so the check is about
        where the crosshair IS, not where it was when the step was planned."""
        try:
            want = (int(expected[0]), int(expected[1]), int(expected[2]))
        except (TypeError, ValueError, IndexError):
            return f"{expected!r} is not a block coordinate I can check."
        seen = self._probe()
        if seen is None:
            return (f"I cannot read what the crosshair is on, so I pressed "
                    f"nothing: I could not confirm it is on {want}.")
        try:
            name, where = seen[0], (int(seen[1]), int(seen[2]), int(seen[3]))
        except (TypeError, ValueError, IndexError):
            return (f"The crosshair reading was incomplete, so I could not "
                    f"confirm it is on {want}; nothing was pressed.")
        if where != want:
            return (f"The crosshair is on {name} at {where}, not on {want}. "
                    f"Nothing was pressed.")
        if face is not None:
            seen_face = seen[4] if len(seen) > 4 else None
            if not seen_face:
                return (f"The crosshair is on {want}, but the reading does "
                        f"not say which face, and the {face} face is the one "
                        f"meant. Nothing was pressed.")
            if str(seen_face).lower() != face:
                return (f"The crosshair is on the {seen_face} face of {want}, "
                        f"not the {face} face. Nothing was pressed.")
        return ""

    def _open_screen(self) -> str:
        """The kind of screen the bridge reports open, or ''. '' too when
        there is no bridge reading: nothing is reported open."""
        if self._gui_probe is None:
            return ""
        try:
            state = self._gui_probe()
        except Exception:
            return ""
        return str(getattr(state, "screen", None) or "")

    def _close_refusal(self) -> str:
        """'' when the bridge reports a screen open now; otherwise why ESC
        is not pressed. ESC with no screen open is not harmless: it opens
        the pause menu. A screen it cannot see is not assumed."""
        state = None
        if self._gui_probe is not None:
            try:
                state = self._gui_probe()
            except Exception:
                state = None
        if state is None:
            return ("I cannot see whether a screen is open -- that needs the "
                    "bridge mod -- and ESC with none open brings up the "
                    "pause menu. Nothing was pressed.")
        if not getattr(state, "screen", None):
            lacks = getattr(state, "lacks", None)
            if callable(lacks) and lacks("gui"):
                # Not "no screen": this jar does not report screens at all,
                # so one may well be open.
                return ("I cannot tell whether a screen is open: the "
                        "installed mod is older than this Jarvis and does "
                        "not report screens -- and ESC with none open "
                        "brings up the pause menu. Nothing was pressed. "
                        "Quit Minecraft, run install_mod.bat, then start "
                        "Minecraft again.")
            return ("No screen is open, so there is nothing to close -- and "
                    "ESC with none open brings up the pause menu. Nothing "
                    "was pressed.")
        return ""

    def _entity_refusal(self) -> str:
        """'' when the entity probe reports a hostile mob under the
        crosshair; otherwise why not. Never a player, never a passive mob,
        never something it cannot name."""
        if self._entity_probe is None:
            return ("I cannot read what the crosshair is on, so I did not "
                    "attack. Nothing was pressed.")
        try:
            seen = self._entity_probe()
        except Exception:
            seen = None
        if not seen:
            return ("The crosshair is not on a mob, and I only attack "
                    "hostile mobs. Nothing was pressed.")
        name, category = (tuple(seen) + (None, None))[:2]
        if category != "hostile":
            what = category or "not known to be hostile"
            return (f"The crosshair is on {name or 'something'}, which is "
                    f"{what}; I only attack hostile mobs. Nothing was "
                    f"pressed.")
        return ""

    def move(self, params: dict) -> ActionResult:
        refusal = self._require_authorized(core_caps.MINECRAFT_MOVEMENT,
                                           "move")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_move(params or {})
        return self._hold_for(spec.key, spec.duration, "move",
                              spec.as_dict(), spec.clamped)

    def move_and_jump(self, params: dict | None = None) -> ActionResult:
        """Walk and jump together, to get onto a one-block ledge.

        Needs the movement capability and nothing else: it presses a movement
        key and the jump key, both of which `move` and `jump` already press
        under that same grant. It is not a new permission and cannot reach a
        key those two cannot."""
        refusal = self._require_authorized(core_caps.MINECRAFT_MOVEMENT,
                                           "move_and_jump")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_move_and_jump(params or {})
        return self._hold_inputs(spec.keys, (), spec.duration,
                                 "move_and_jump", spec.as_dict(), spec.clamped)

    def jump(self, params: dict | None = None) -> ActionResult:
        refusal = self._require_authorized(core_caps.MINECRAFT_MOVEMENT,
                                           "jump")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_jump(params or {})
        return self._hold_for(spec.key, spec.duration, "jump",
                              spec.as_dict(), spec.clamped)

    def _require_authorized(self, capability: str,
                            action: str) -> ActionResult | None:
        """The one gate every gameplay action passes.

        Returns None when the session's grant covers this capability, or the
        refusal to hand straight back. Checked here rather than in the adapter
        so it holds for every caller including the task runner — a skill
        cannot mine its way around a session that was never authorised.

        A missing or dead session says nothing here: `_require_ready` gives
        the better answer ("start a session" rather than "your session does
        not cover this"), and sending someone to fix a grant on a session that
        does not exist wastes their time."""
        session = self._sessions.current
        if session is None or not session.active:
            return None

        if session.covers(capability):
            return None

        return ActionResult(
            ok=False, action=action, requested={},
            stopped_reason="not_authorized",
            error_class="InteractionNotGranted",
            error=(f"This session does not cover {capability}. Ask me to "
                   f"start a Minecraft session and confirm it — one "
                   f"confirmation covers all ordinary gameplay."),
        )

    def _gameplay(self, capability: str, action: str, parse,
                  params: dict | None) -> ActionResult:
        """Authorise, then validate, then hold whatever the spec names.

        One function for every gameplay primitive, so there is exactly one
        place where authorisation happens and exactly one place where inputs
        go down — which is what makes "did anything skip the gate?" a question
        you can answer by reading twenty lines.

        The order matters. Parsing first meant an unauthorised session was
        told about a bad parameter instead of about the missing authorisation,
        which is both the less useful answer and a way to probe the parameter
        rules of an action you may not take. The gate speaks first."""
        refusal = self._require_authorized(capability, action)
        if refusal is not None:
            return refusal
        spec = parse(params or {})
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 action, spec.as_dict(), spec.clamped)

    def mine(self, params: dict | None = None) -> ActionResult:
        """Hold attack until the block changes, or until the time runs out.

        WHY THIS ONE ACTION WATCHES WHILE IT WORKS
            Minecraft resets breaking progress the instant the button comes
            up. Every other action here can be a fixed hold because nothing
            in the game cares whether it was continuous; mining is the
            exception, and a mine implemented as repeated short swings does
            damage that is thrown away between each one. It looks exactly
            like hitting too weakly.

            So the hold runs long, and a probe watches the target so it can
            stop the moment the block goes rather than spending the whole
            budget. Without a probe it is a plain timed hold -- still
            correct, just wasteful, and the result says which it was."""
        refusal = self._require_authorized(core_caps.MINECRAFT_MINING, "mine")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_mine(params or {})
        return self._hold_inputs((), spec.buttons, spec.duration, "mine",
                                 spec.as_dict(), spec.clamped,
                                 stop_when_changed=True,
                                 expect_target=spec.expect_target)

    def place(self, params: dict | None = None) -> ActionResult:
        """One tap of the use button -- which does what the held item does.

        So the held item is checked first, through the injected
        `held_item_probe`: a lava bucket, flint and steel, an ender pearl or
        anything else on action_spec's deny-list is refused before anything
        is pressed, and so is a hand that cannot be read. Authorisation and
        parameters are checked first, in the same order as every action."""
        refusal = self._require_authorized(core_caps.MINECRAFT_BUILD, "place")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_place(params or {})
        problem = action_spec.place_refusal(self._held_item())
        if problem:
            return ActionResult(
                ok=False, action="place", requested=spec.as_dict(),
                actual_duration_ms=0, stopped_reason="held_item",
                error_class="HeldItemRefused", error=problem)
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 "place", spec.as_dict(), spec.clamped,
                                 expect_target=spec.expect_target,
                                 expect_face=spec.expect_face)

    def _held_item(self):
        """The held item's name, "" for an empty hand, None if unknown.
        Never raises: a probe that fails is an unknown hand."""
        if self._held_item_probe is None:
            return None
        try:
            held = self._held_item_probe()
        except Exception:
            return None
        return None if held is None else str(held)

    def interact(self, params: dict | None = None) -> ActionResult:
        """Right-click to open or use a block -- with `place`'s deny-list on
        the held item (an empty hand allowed), checked before pressing."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INTERACT,
                                           "interact")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_interact(params or {})
        problem = action_spec.interact_refusal(self._held_item())
        if problem:
            return self._held_item_refused("interact", spec, problem)
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 "interact", spec.as_dict(), spec.clamped)

    def _held_item_refused(self, action, spec, problem) -> ActionResult:
        return ActionResult(
            ok=False, action=action, requested=spec.as_dict(),
            actual_duration_ms=0, stopped_reason="held_item",
            error_class="HeldItemRefused", error=problem)

    def eat(self, params: dict | None = None) -> ActionResult:
        return self._gameplay(core_caps.MINECRAFT_ITEMS, "eat",
                              action_spec.parse_eat, params)

    def drop(self, params: dict | None = None) -> ActionResult:
        return self._gameplay(core_caps.MINECRAFT_ITEMS, "drop",
                              action_spec.parse_drop, params)

    def inventory(self, params: dict | None = None) -> ActionResult:
        """Open or close the inventory. Authorised as one capability; the
        spec decides which key, so the action name is resolved after the
        gate rather than before it."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INVENTORY,
                                           "inventory")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_inventory(params or {})
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 spec.action, spec.as_dict(), spec.clamped)

    def attack(self, params: dict | None = None) -> ActionResult:
        """Hold the attack button -- only with a hostile mob under the
        crosshair, as the entity probe reports it at the moment of pressing.
        Never a player, a pet, a villager, an animal, a block or nothing.

        Deliberately says nothing about whether anything broke — that is
        `minecraft/verification.py`'s job, and conflating the two is how an
        agent ends up reporting a tree felled that is still standing."""
        refusal = self._require_authorized(core_caps.MINECRAFT_COMBAT,
                                           "attack")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_attack(params or {})
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 "attack", spec.as_dict(), spec.clamped,
                                 expect_hostile=spec.expect_hostile)

    def use_item(self, params: dict | None = None) -> ActionResult:
        """Hold the use button with what is held -- the explicit way to use
        an item. A bucket of lava, water or powder snow, flint and steel and
        a fire charge are used only when named (`expect_item`)."""
        refusal = self._require_authorized(core_caps.MINECRAFT_ITEMS,
                                           "use_item")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_use_item(params or {})
        named = (spec.detail or {}).get("expect_item")
        problem = action_spec.use_item_refusal(self._held_item(), named)
        if problem:
            return self._held_item_refused("use_item", spec, problem)
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 "use_item", spec.as_dict(), spec.clamped)

    def sneak(self, params: dict | None = None) -> ActionResult:
        return self._gameplay(core_caps.MINECRAFT_MOVEMENT, "sneak",
                              action_spec.parse_sneak, params)

    def sprint(self, params: dict | None = None) -> ActionResult:
        return self._gameplay(core_caps.MINECRAFT_MOVEMENT, "sprint",
                              action_spec.parse_sprint, params)

    def hotbar_select(self, params: dict | None = None) -> ActionResult:
        """Tap a number key. The shortest action there is, and still guarded:
        a number key delivered to the wrong window types a digit into it --
        and one pressed while a screen is open, with the pointer over a slot,
        swaps that slot with the hotbar. So not while a screen is open."""
        refusal = self._require_authorized(core_caps.MINECRAFT_ITEMS,
                                           "hotbar_select")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_hotbar(params or {})
        state = self._gui_state() if self._gui_probe is not None else None
        if state is not None and getattr(state, "screen", None):
            return self._gui_refused(
                "hotbar_select", spec.as_dict(),
                f"a screen is open ({state.screen}), and a number key there "
                f"would move items, not select a slot")
        return self._hold_inputs((spec.key,), (), action_spec.JUMP_TAP_S,
                                 "hotbar_select", spec.as_dict(), spec.clamped)

    # ── inside a screen ──────────────────────────────────────────────────────

    def _gui_state(self):
        """A fresh reading for the screen rules, or None. Never raises. Also
        keeps the best health seen since the screen opened, for the rule
        that health must not drop while clicking."""
        if self._gui_probe is None:
            return None
        try:
            state = self._gui_probe()
        except Exception:
            return None
        if state is None or not getattr(state, "screen", None):
            self._gui_health_peak = None
            return state
        health = getattr(state, "health", None)
        if isinstance(health, (int, float)):
            if self._gui_health_peak is None or health > self._gui_health_peak:
                self._gui_health_peak = float(health)
        return state

    @staticmethod
    def _gui_refused(action, requested, why) -> ActionResult:
        return ActionResult(ok=False, action=action, requested=requested,
                            actual_duration_ms=0, stopped_reason="gui_gate",
                            error_class="GuiRefused",
                            error=f"I did not press anything: {why}.")

    def gui_point(self, params: dict | None = None) -> ActionResult:
        """Move the pointer inside an open, allowed screen. Nothing else:
        it does not click, and outside an allowed screen it does nothing."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INVENTORY,
                                           "gui_point")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_gui_point(params or {})
        state = self._gui_state()
        problem = gui_mod.screen_refusal(state) if state is not None else \
            "I cannot read the screen (no bridge reading)"
        if problem:
            return self._gui_refused("gui_point", spec.as_dict(), problem)
        if not self._input_slot.acquire(blocking=False):
            return self._busy_result("gui_point", spec.as_dict(),
                                     spec.clamped)
        try:
            return self._look_owned(spec, "gui_point")
        finally:
            self._input_slot.release()

    def gui_click(self, params: dict | None = None) -> ActionResult:
        """One click on a NAMED slot, only when the game reports the pointer
        over it -- and every other rule in minecraft/gui.py -- from a
        reading taken immediately before pressing."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INVENTORY,
                                           "gui_click")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_gui_click(params or {})
        return self._gui_press(spec, spec.detail["slot"])

    def gui_swap(self, params: dict | None = None) -> ActionResult:
        """A number key over a named slot: swaps it with that hotbar slot.
        The same gate as a click -- the key acts on the slot under the
        pointer, so the pointer must be on the slot meant."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INVENTORY,
                                           "gui_swap")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_gui_swap(params or {})
        return self._gui_press(spec, spec.detail["slot"])

    def _gui_press(self, spec, slot) -> ActionResult:
        state = self._gui_state()
        if state is None:
            return self._gui_refused(spec.action, spec.as_dict(),
                                     "I cannot read the screen (no bridge "
                                     "reading)")
        problem = gui_mod.click_refusal(state, slot,
                                        health_floor=self._gui_health_peak)
        if problem:
            return self._gui_refused(spec.action, spec.as_dict(), problem)
        return self._hold_inputs(spec.keys, spec.buttons, spec.duration,
                                 spec.action, spec.as_dict(), spec.clamped)

    def toggle_debug_overlay(self) -> ActionResult:
        """Tap F3.

        Needed because reading state requires the overlay to be open, and
        asking the user to press it themselves every time makes the whole
        state pipeline feel broken. It is an input action like any other, so
        it needs a live, authorised session and passes the same guard. The
        grant it needs is the inventory's -- it toggles a game screen, as
        opening the inventory does."""
        refusal = self._require_authorized(core_caps.MINECRAFT_INVENTORY,
                                           "toggle_debug_overlay")
        if refusal is not None:
            return refusal
        return self._hold_inputs(("f3",), (), action_spec.JUMP_TAP_S,
                                 "toggle_debug_overlay", {}, False)

    def look(self, params: dict) -> ActionResult:
        """One relative mouse delta. Nothing is held, so there is nothing to
        release — but the guard still runs, because a delta delivered to the
        wrong window moves someone else's cursor."""
        refusal = self._require_authorized(core_caps.MINECRAFT_LOOK, "look")
        if refusal is not None:
            return refusal
        spec = action_spec.parse_look(params or {})
        if not self._input_slot.acquire(blocking=False):
            return self._busy_result("look", spec.as_dict(), spec.clamped)
        try:
            return self._look_owned(spec)
        finally:
            self._input_slot.release()

    def _look_owned(self, spec, action: str = "look") -> ActionResult:
        """`look` (or `gui_point`), once this thread owns the input slot."""
        started = time.monotonic()
        epoch = self._abort_epoch

        self._await_focus(epoch=epoch)

        try:
            self._require_ready()
        except Exception as e:
            return ActionResult(
                ok=False, action=action, requested=spec.as_dict(),
                stopped_reason=_reason_for(e), clamped=spec.clamped,
                error_class=type(e).__name__, error=str(e),
            )

        if self._aborted(epoch):
            return self._cancelled_result(action, spec.as_dict(),
                                          spec.clamped)

        try:
            self._ledger.move_mouse(spec.dx, spec.dy)
        except Exception as e:
            return ActionResult(
                ok=False, action=action, requested=spec.as_dict(),
                actual_duration_ms=int((time.monotonic() - started) * 1000),
                stopped_reason="input_unavailable", clamped=spec.clamped,
                error_class=type(e).__name__, error=str(e),
            )

        return ActionResult(
            ok=True, action=action, requested=spec.as_dict(),
            actual_duration_ms=int((time.monotonic() - started) * 1000),
            window_focused_throughout=True, clamped=spec.clamped,
        )

    # ── one way in ───────────────────────────────────────────────────────────

    ACTIONS = {
        "move": "move", "jump": "jump", "look": "look",
        "move_and_jump": "move_and_jump",
        "sneak": "sneak", "sprint": "sprint",
        "attack": "attack", "mine": "mine",
        "place": "place", "interact": "interact",
        "use_item": "use_item", "eat": "eat", "drop": "drop",
        "hotbar_select": "hotbar_select", "inventory": "inventory",
        "gui_point": "gui_point", "gui_click": "gui_click",
        "gui_swap": "gui_swap",
    }
    """Every gameplay primitive, by name. The complete list.

    This IS the vocabulary: `execute_action` looks a name up here and calls
    nothing else, so a planner that invents an action gets a refusal rather
    than a method call. There is no entry that takes a key, a keycode or a
    screen coordinate."""

    def execute_action(self, action: str, params: dict | None = None):
        """Run one named primitive.

        The single entry point the planner and the task runner both use, so
        the set of things either can do is this dict and nothing else."""
        name = str(action or "").strip().lower()
        method = self.ACTIONS.get(name)
        if method is None:
            return ActionResult(
                ok=False, action=name or "(none)", requested=dict(params or {}),
                stopped_reason="unknown_action",
                error_class="InvalidAction",
                error=(f"'{action}' is not a Minecraft action I have. Mine "
                       f"are: {', '.join(sorted(self.ACTIONS))}."),
            )
        return getattr(self, method)(params or {})

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


class _TaskCancelScope:
    """`with controller.cancellable(event):` -- see that method.

    A small class rather than contextlib, which this package does not import
    (tests/minecraft/test_minecraft_boundary.py keeps the import list short on
    purpose)."""

    def __init__(self, controller: "MinecraftController", event):
        self._controller = controller
        self._event = event
        self._previous = None

    def __enter__(self):
        self._previous = self._controller._task_cancel
        self._controller._task_cancel = self._event
        return self

    def __exit__(self, *_exc):
        self._controller._task_cancel = self._previous
        return False


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
        "not_authorized": "This session is not authorised for that. One "
                          "confirmation covers all ordinary gameplay — ask me "
                          "to start a Minecraft session.",
        "focus_lost": "Minecraft is not the active window. Click on the game "
                      "and ask me again — I will not send keys to whatever is "
                      "in front instead.",
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
        "cancelled": "Cancelled on request. Every input is released and the "
                     "session is still open.",
        "busy": "Another Minecraft action was still running, so this one "
                "pressed nothing.",
        "target_not_confirmed": "The crosshair was not confirmed on the "
                                "intended block, so nothing was pressed.",
        "danger": "I let go because the player was in danger.",
    }.get(reason or "", "")


__all__ = ["MinecraftController", "ActionResult", "TICK_SECONDS",
           "SUPERVISOR_SECONDS", "DEADMAN_GRACE_SECONDS",
           "FOCUS_WAIT_SECONDS"]
