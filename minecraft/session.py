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

from core import capabilities as core_caps

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

UNLIMITED = 0.0
"""Ask for this duration to get a session that runs until it is stopped.

WHY AN UNBOUNDED SESSION IS STILL BOUNDED
    The clock was never the only limit, and it was always the weakest one --
    it protects you from forgetting, not from anything going wrong. Every
    mechanism that actually stops a runaway agent is untouched:

        F12                 any time, from anywhere, even mid-action
        "stop"              spoken, at any point
        focus loss          held input is released within one tick
        the game closing    the guard fails and everything stops
        the deadman         a key held too long comes up by itself
        JARVIS exiting      atexit releases whatever is down

    What expiry added on top of those was an automatic end to a session the
    user had stopped thinking about. That is worth having as the default, and
    it is genuinely annoying when you are mid-conversation about what to build
    -- which is a way of training people to re-approve without reading, the
    exact habit the one-confirmation design exists to avoid.

    So it is offered, it is never the default, and the banner says plainly
    that the session lasts until stopped."""

GAMEPLAY_CAPABILITIES = frozenset({
    core_caps.MINECRAFT_MOVEMENT,
    core_caps.MINECRAFT_LOOK,
    core_caps.MINECRAFT_COMBAT,
    core_caps.MINECRAFT_MINING,
    core_caps.MINECRAFT_BUILD,
    core_caps.MINECRAFT_ITEMS,
    core_caps.MINECRAFT_INVENTORY,
    core_caps.MINECRAFT_INTERACT,
    core_caps.MINECRAFT_TASK,
})
"""What ONE confirmation buys.

This frozenset is the authorization. It is defined here, in source, and there
is no function anywhere that adds to it at runtime -- so "what may a session
do" is a question answered by reading this file, not by inspecting state that
something could have changed.

Chat, launch and command are deliberately absent. A grant to play the game is
not a grant to talk to strangers on a server, start processes, or reach a
command line, and none of those becomes available because a session is open."""

GRANT_SUMMARY = (
    "walk, jump, sprint, sneak, look around, attack, mine and break blocks, "
    "place blocks, use and drop items, select hotbar slots, open and use the "
    "inventory, and interact with blocks and entities"
)
"""What the confirmation banner says the grant covers, in the user's words.

Kept next to the frozenset it describes so the two cannot drift: a capability
added above without a mention here would be a grant the user was never told
about, which is the failure mode that makes session-level consent worse than
per-action consent rather than better."""


@dataclass
class Session:
    """One live grant. Do not construct directly — `SessionManager.start()`."""

    session_id: str
    owner: str
    started_at: float
    expires_at: float | None          # None = runs until stopped
    requested_seconds: float
    granted_seconds: float
    cancel: threading.Event = field(default_factory=threading.Event)
    ended_reason: str = ""
    authorized: bool = False
    _ended: bool = False

    # ── what this grant covers ───────────────────────────────────────────────
    #
    # `authorized` is set only by SessionManager.start(), which the controller
    # calls only from inside the broker's approved-run callback -- so it is
    # true only after a human pressed CONFIRM on a banner that named
    # GRANT_SUMMARY. The model has no parameter that sets it and no path that
    # reaches it.
    #
    # This flag, not the capability verdicts, is what makes mining safe. The
    # table says an action is the kind of thing that may happen; the session
    # says it may happen to YOUR world, now, for the next few minutes.

    @property
    def unlimited(self) -> bool:
        """Runs until stopped. See UNLIMITED for why that is still bounded."""
        return self.expires_at is None

    @property
    def active(self) -> bool:
        """Live right now: not cancelled, not ended, not expired."""
        return (not self._ended
                and not self.cancel.is_set()
                and not self.expired)

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() >= self.expires_at

    def is_authorized(self) -> bool:
        """Live AND approved. Both, always — an expired authorisation is not
        an authorisation, and this is the single question every gameplay
        action asks before it touches an input."""
        return bool(self.authorized) and self.active

    def covers(self, capability: str) -> bool:
        """Does this session's grant extend to `capability`?

        Membership of GAMEPLAY_CAPABILITIES, not a prefix match on
        "minecraft.": a prefix would silently swallow every capability added
        to the namespace later, including chat and command, which is exactly
        the accident this guards."""
        return self.is_authorized() and capability in GAMEPLAY_CAPABILITIES

    @property
    def remaining(self) -> float:
        """Seconds left, or infinity for a session that runs until stopped.

        Infinity rather than None so that every caller comparing or formatting
        this keeps working — a None here would turn "how long left?" into a
        type check at a dozen call sites, and the one that got missed would be
        a crash in the middle of a live session."""
        if self.expires_at is None:
            return float("inf")
        return max(0.0, self.expires_at - time.monotonic())

    @property
    def age(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "owner": self.owner,
            "active": self.active,
            "unlimited": self.unlimited,
            "granted_seconds": (None if self.unlimited
                                else round(self.granted_seconds, 1)),
            "requested_seconds": round(self.requested_seconds, 1),
            "remaining_seconds": (None if self.unlimited
                                  else round(self.remaining, 1)),
            "age_seconds": round(self.age, 1),
            "ended_reason": self.ended_reason,
            "authorized": self.is_authorized(),
            "covers": sorted(GAMEPLAY_CAPABILITIES) if self.is_authorized()
                      else [],
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
              authorized: bool = False) -> Session:
        """Open a session. Raises RuntimeError if one is already live.

        Called from inside the broker's approved-run callback, so by the time
        this executes a human has pressed CONFIRM.

        `authorized` defaults to False so a caller who forgets it gets a
        session that can do nothing, rather than one that can do everything.
        The only caller that passes True is the controller, and only from
        inside the broker's approved-run callback."""
        requested = float(duration_s if duration_s is not None
                          else DEFAULT_SESSION_SECONDS)
        unlimited = requested <= 0
        granted = (float("inf") if unlimited
                   else max(MIN_SESSION_SECONDS,
                            min(requested, MAX_SESSION_SECONDS)))

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
                expires_at=None if unlimited else now + granted,
                requested_seconds=requested,
                granted_seconds=granted,
                authorized=bool(authorized),
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


__all__ = ["Session", "SessionManager", "GAMEPLAY_CAPABILITIES",
           "GRANT_SUMMARY", "MAX_SESSION_SECONDS", "UNLIMITED",
           "DEFAULT_SESSION_SECONDS", "MIN_SESSION_SECONDS"]
