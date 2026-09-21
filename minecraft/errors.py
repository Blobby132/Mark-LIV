"""
minecraft/errors.py — every way this subsystem can refuse or fail, named.

Named classes rather than bare strings because the planner that eventually sits
on top of this has to tell the difference between "you asked for something
impossible" (fix the plan), "the window went away" (re-observe, maybe recover)
and "the user hit F12" (stop, do not retry). A single generic exception would
flatten all three into a retry loop.
"""

from __future__ import annotations


class MinecraftError(Exception):
    """Base for everything in this package."""


# ── Things the caller asked for that cannot be honoured ──────────────────────

class InvalidAction(MinecraftError):
    """The requested action, or one of its parameters, is not in the vocabulary.

    A planner asking for `alt+f4`, a 30-second key hold, or an action name that
    does not exist gets this — and no input is sent. Distinct from a failure:
    nothing was attempted."""


class CapabilityDisabled(MinecraftError):
    """The capability exists in the policy table but this phase does not enable
    it. `minecraft.attack` today: declared, refused before any input."""


# ── Things about the world that are not true ─────────────────────────────────

class MinecraftNotRunning(MinecraftError):
    """No Minecraft process could be found."""


class WindowNotFound(MinecraftError):
    """The process is there but its window could not be located — starting up,
    minimised on some platforms, or a launcher window rather than the game."""


class WindowNotFocused(MinecraftError):
    """Minecraft is not the foreground window, so input aimed at it would land
    somewhere else. The single most important refusal in this package."""


class NoActiveSession(MinecraftError):
    """Input was requested with no live control session. The session is the
    human's consent; without it there is nothing authorising a keypress."""


class SessionExpired(MinecraftError):
    """The session's clock ran out."""


# ── Things that went wrong while acting ──────────────────────────────────────

class InputBackendUnavailable(MinecraftError):
    """There is no way to send input on this platform or in this environment.

    Raised rather than degraded, because the alternative — accepting the call
    and doing nothing — would have the planner believe the player moved."""


class EmergencyStop(MinecraftError):
    """A stop was requested: F12, focus loss, the process dying, or the API.

    Not a bug. It carries `reason` so the result can say which of those it was,
    and so a planner can tell "the user took over" apart from "this failed"."""

    def __init__(self, reason: str = "requested"):
        super().__init__(f"Minecraft control stopped: {reason}")
        self.reason = reason


class ObservationFailed(MinecraftError):
    """A frame could not be captured. Structured rather than a None frame the
    caller might not check."""


class InteractionNotGranted(MinecraftError):
    """Mining or placing was attempted in a session that did not ask for it.

    Separate from NoActiveSession because the fix is different: there IS a
    session, it simply does not cover changing the world. The remedy is to
    start one that does, which means another confirmation naming what it
    allows -- not silently upgrading the one already open."""


__all__ = [
    "MinecraftError", "InteractionNotGranted", "InvalidAction", "CapabilityDisabled",
    "MinecraftNotRunning", "WindowNotFound", "WindowNotFocused",
    "NoActiveSession", "SessionExpired", "InputBackendUnavailable",
    "EmergencyStop", "ObservationFailed",
]
