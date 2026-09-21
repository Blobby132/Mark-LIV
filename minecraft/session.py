"""
minecraft/session.py — the boundary a human consents to.

WHY A SESSION AND NOT PER-ACTION CONFIRMATION
    Walking to a tree is tens of keypresses. A confirmation in front of each
    one would be unusable, and an unusable safety control is worse than none —
    people turn it off, or learn to click through it without reading, which is
    the same thing.

    So the human makes ONE decision — "you may drive Minecraft for the next
    five minutes" — and that decision is what `minecraft.control_session`
    (CONFIRM) buys. Inside the session, `minecraft.move` and `minecraft.look`
    are ALLOW. Outside it they are refused before a key is touched.

WHAT MAKES THE BARGAIN HONEST
    The session is bounded in every direction a person would care about:

      - it expires on the clock, at most 300 seconds
      - it ends when Minecraft stops being the foreground window
      - it ends when the game process goes away
      - it ends on F12, or on `stop`, immediately
      - it cannot be extended; asking for more is a new confirmation

    And it is one at a time. There is no session stack, no nesting, and
    starting a second one while the first is live is refused rather than
    silently replacing it.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

MAX_SESSION_SECONDS = 300.0
"""Five minutes. Not a default that can be raised — `start()` clamps to it."""

DEFAULT_SESSION_SECONDS = 120.0
"""What you get if nobody says. Deliberately well under the maximum: most
things worth trying in this phase take under a minute, and a shorter default
means a forgotten session ends sooner."""

MIN_SESSION_SECONDS = 5.0


@dataclass
class Session:
    """One live grant. Do not construct directly — `SessionManager.start()`."""

    session_id: str
    owner: str
    started_at: float
    expires_at: float
    requested_seconds: float
    granted_seconds: float
    cancel: threading.Event = field(default_factory=threading.Event)
    ended_reason: str = ""
    allow_interaction: bool = False
    _ended: bool = False

    # ── what this grant covers ───────────────────────────────────────────────
    #
    # Movement and looking come with any session. Mining and placing do not:
    # they change the world, and undoing a mistake in survival means finding
    # the block again. So they need a session opened with interaction asked
    # for by name, and the confirmation for that session says so in as many
    # words.
    #
    # This flag, not the capability verdict, is what makes attack safe. See
    # MINECRAFT_ATTACK in core/capabilities.py.

    @property
    def active(self) -> bool:
        """Live right now: not cancelled, not ended, not expired."""
        return (not self._ended
                and not self.cancel.is_set()
                and time.monotonic() < self.expires_at)

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.expires_at

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def age(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "active": self.active,
            "granted_seconds": round(self.granted_seconds, 1),
            "requested_seconds": round(self.requested_seconds, 1),
            "remaining_seconds": round(self.remaining, 1),
            "age_seconds": round(self.age, 1),
            "ended_reason": self.ended_reason,
            "allow_interaction": self.allow_interaction,
        }


class SessionManager:
    """Holds the one session, if there is one.

    Every method is safe to call from any thread: the emergency hotkey watcher,
    the focus watcher and the action thread all end sessions."""

    def __init__(self):
        self._lock = threading.RLock()
        self._session: Session | None = None
        self._listeners: list = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, duration_s: float | None = None,
              owner: str = "user",
              allow_interaction: bool = False) -> Session:
        """Open a session. Raises RuntimeError if one is already live.

        Called from inside the broker's approved-run callback, so by the time
        this executes a human has pressed CONFIRM.

        `allow_interaction` defaults to False so that a caller who forgets to
        pass it gets the safe session, not the destructive one."""
        requested = float(duration_s if duration_s is not None
                          else DEFAULT_SESSION_SECONDS)
        granted = max(MIN_SESSION_SECONDS,
                      min(requested, MAX_SESSION_SECONDS))

        with self._lock:
            if self._session is not None and self._session.active:
                raise RuntimeError(
                    f"A Minecraft control session is already running with "
                    f"{self._session.remaining:.0f} seconds left."
                )
            now = time.monotonic()
            self._session = Session(
                session_id=uuid.uuid4().hex[:12],
                owner=str(owner)[:40],
                started_at=now,
                expires_at=now + granted,
                requested_seconds=requested,
                granted_seconds=granted,
                allow_interaction=bool(allow_interaction),
            )
            return self._session

    def end(self, reason: str = "ended") -> str:
        """End whatever is running. Idempotent; safe with nothing running.

        Sets the cancel event BEFORE marking the session ended, so an action
        thread checking `cancel.is_set()` sees the stop at the earliest
        possible moment."""
        with self._lock:
            session = self._session
            if session is None:
                return ""
            session.cancel.set()
            if not session._ended:
                session._ended = True
                session.ended_reason = reason
            ended_id = session.session_id
            self._session = None

        self._notify(ended_id, reason)
        return ended_id

    # ── queries ──────────────────────────────────────────────────────────────

    @property
    def current(self) -> Session | None:
        with self._lock:
            return self._session

    def is_active(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.active

    def require(self) -> Session:
        """The live session, or raise. Used by the controller before input."""
        from minecraft.errors import NoActiveSession, SessionExpired
        with self._lock:
            session = self._session
            if session is None:
                raise NoActiveSession(
                    "There is no Minecraft control session. Ask me to start "
                    "one and confirm it on screen first."
                )
            if session.expired:
                raise SessionExpired(
                    f"The Minecraft session ran out {abs(session.remaining):.0f} "
                    f"seconds ago. Start a new one if you still want me to play."
                )
            if not session.active:
                raise NoActiveSession(
                    f"The Minecraft session has ended"
                    f"{f' ({session.ended_reason})' if session.ended_reason else ''}."
                )
            return session

    def describe(self) -> str:
        with self._lock:
            session = self._session
        if session is None:
            return "No Minecraft control session."
        if not session.active:
            return f"Session {session.session_id} has ended ({session.ended_reason})."
        return (f"Session {session.session_id} is live, "
                f"{session.remaining:.0f}s remaining.")

    # ── observers ────────────────────────────────────────────────────────────

    def on_end(self, callback) -> None:
        """Be told when a session ends, whatever ended it. The controller
        registers here so it can release held keys on expiry."""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self, session_id: str, reason: str) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(session_id, reason)
            except Exception:
                pass          # a listener fault must not block the teardown


__all__ = ["Session", "SessionManager", "MAX_SESSION_SECONDS",
           "DEFAULT_SESSION_SECONDS", "MIN_SESSION_SECONDS"]
