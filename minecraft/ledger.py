"""
minecraft/ledger.py — what is JARVIS holding down right now?

THE INVARIANT
    No Minecraft input may remain held after the controller has stopped.

    Everything else in this package is a way of getting to that sentence. A
    held key is the one piece of state that outlives the code holding it: a
    crashed action thread, a raised exception, an expired session, a killed
    process — none of those release W. The operating system keeps it down until
    something explicitly sends the key-up.

    So the set of held keys lives here, apart from the controller, behind a
    reentrant lock, and `release_all()` is callable from any thread at any time
    including from inside a signal path or an `atexit` hook.

RECORD FIRST, THEN PRESS
    `hold()` adds the key to the ledger BEFORE asking the backend to press it.
    That ordering is deliberate. If the press fails, the ledger has an entry
    for a key that is not really down, and `release_all()` sends a harmless
    redundant key-up. If it were the other way round and the process died
    between the press and the record, the key would be held with nothing
    remembering it — which is the failure this file exists to prevent.

    Wrong in the harmless direction, on purpose.

RELEASE IS IDEMPOTENT AND BOUNDED
    `release_all()` can be called repeatedly, concurrently, and on an empty
    ledger. It tries each key twice, then drops it from the ledger regardless
    and reports the failure — because a ledger that retries forever is a
    shutdown path that never finishes, and a silent drop is a lie about a key
    that may still be down.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# A key held longer than this is a bug, not an instruction. The controller
# enforces per-action limits; this is the backstop for the case where the
# controller itself is wedged, checked by the supervisor in controller.py.
MAX_HOLD_SECONDS = 2.5

_RELEASE_ATTEMPTS = 2


@dataclass
class _Held:
    key: str
    since: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.since


@dataclass(frozen=True)
class ReleaseReport:
    """What `release_all()` actually managed to do.

    `failed` non-empty is the case nobody wants to think about: a key this
    process believes is still down that the OS would not let go. It is
    reported rather than swallowed so the controller can say so out loud."""

    released: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    already_empty: bool = False

    @property
    def clean(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict:
        return {"released": list(self.released), "failed": list(self.failed),
                "already_empty": self.already_empty}


class InputLedger:
    """The record of what is currently held, and the only thing that releases it.

    One instance per controller. The lock is reentrant because `release_all()`
    is reachable from inside a callback that already holds it (focus watcher →
    stop → release)."""

    def __init__(self, backend):
        self._backend = backend
        self._held: dict[str, _Held] = {}
        self._lock = threading.RLock()

    # ── state ────────────────────────────────────────────────────────────────

    def held(self) -> frozenset:
        with self._lock:
            return frozenset(self._held)

    def is_holding(self, key: str = "") -> bool:
        with self._lock:
            if key:
                return key in self._held
            return bool(self._held)

    def oldest_hold_age(self) -> float:
        """Seconds since the longest-standing held key went down, or 0.0.

        The supervisor thread reads this to enforce the deadman timeout."""
        with self._lock:
            if not self._held:
                return 0.0
            return max(entry.age for entry in self._held.values())

    def overdue(self, limit: float = MAX_HOLD_SECONDS) -> tuple:
        """Keys held longer than `limit`. Should always be empty."""
        with self._lock:
            return tuple(sorted(k for k, e in self._held.items()
                                if e.age > limit))

    # ── acting ───────────────────────────────────────────────────────────────

    def hold(self, key: str) -> None:
        """Press and record. Recorded first — see the module docstring.

        Re-holding a key already down is a no-op rather than a second press:
        the OS would treat it as auto-repeat, and the ledger would still have
        one entry, so the two would disagree about how many releases are owed."""
        with self._lock:
            if key in self._held:
                return
            self._held[key] = _Held(key=key)
        # Outside the lock: a slow backend must not block release_all().
        try:
            self._backend.key_down(key)
        except Exception:
            # The entry stays. release_all() will send a redundant key-up,
            # which is harmless, and the caller sees the exception.
            raise

    def release(self, key: str) -> bool:
        """Release one key. True if the backend accepted it."""
        with self._lock:
            present = key in self._held
        if not present:
            return True
        ok = self._release_one(key)
        with self._lock:
            self._held.pop(key, None)
        return ok

    def release_all(self) -> ReleaseReport:
        """Release everything. Safe to call any number of times, from anywhere.

        This is the function the whole package is built around, so it takes no
        arguments, needs no session, checks no permission and cannot be
        refused. It is reachable from the emergency hotkey, the focus watcher,
        the deadman supervisor, an exception handler and `atexit`."""
        with self._lock:
            keys = sorted(self._held)
            if not keys:
                return ReleaseReport(already_empty=True)

        released, failed = [], []
        for key in keys:
            if self._release_one(key):
                released.append(key)
            else:
                failed.append(key)

        with self._lock:
            # Dropped either way: a ledger that keeps unreleasable keys turns
            # every later release_all() into the same doomed retry.
            for key in keys:
                self._held.pop(key, None)

        return ReleaseReport(released=tuple(released), failed=tuple(failed))

    def _release_one(self, key: str) -> bool:
        for _attempt in range(_RELEASE_ATTEMPTS):
            try:
                self._backend.key_up(key)
                return True
            except Exception:
                continue
        return False

    # ── mouse ────────────────────────────────────────────────────────────────

    def move_mouse(self, dx: int, dy: int) -> None:
        """Mouse movement is not held, so it is not ledgered — it is a one-shot
        delta with nothing to release. It goes through here anyway so that all
        input has one route to the backend and a test has one place to watch."""
        self._backend.move_mouse_relative(int(dx), int(dy))


__all__ = ["InputLedger", "ReleaseReport", "MAX_HOLD_SECONDS"]
