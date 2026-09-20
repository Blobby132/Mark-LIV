"""
minecraft/ — a bounded way to drive Minecraft Java, and nothing else.

WHAT THIS PACKAGE IS ALLOWED TO DO
    Look at the Minecraft window, and — inside a session a human confirmed —
    hold W/A/S/D for up to two seconds at a time, tap space, and move the mouse
    by a bounded delta.

WHAT IT CANNOT DO, STRUCTURALLY
    It has no subprocess, no shell, no filesystem writes, no network, and no
    key outside a frozen list of sixteen. Those are not filtered; they are
    absent, and `tests/test_minecraft_boundary.py` fails the build if one
    appears. The controller knows how to control Minecraft. It does not know
    how to control Windows.

THE SHAPE, AND WHERE IT IS GOING

    Gemini → ActionSpec → core/permissions → Session → Focus guard
           → Controller → Minecraft → Observation → WorldState → (verify)

    Phase 2 builds everything except the planner and a state source that can
    actually read anything. `state.StateSource` is the seam the next phases
    plug into — the F3 overlay first, a client mod after — so the planner that
    eventually sits on top never learns where a number came from.
"""

from minecraft.errors import (                                    # noqa: F401
    CapabilityDisabled, EmergencyStop, InputBackendUnavailable, InvalidAction,
    MinecraftError, MinecraftNotRunning, NoActiveSession, ObservationFailed,
    SessionExpired, WindowNotFocused, WindowNotFound,
)

__all__ = [
    "MinecraftError", "InvalidAction", "CapabilityDisabled",
    "MinecraftNotRunning", "WindowNotFound", "WindowNotFocused",
    "NoActiveSession", "SessionExpired", "InputBackendUnavailable",
    "EmergencyStop", "ObservationFailed",
]
