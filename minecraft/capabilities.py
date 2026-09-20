"""
minecraft/capabilities.py — which of the declared capabilities this phase will
actually attempt.

WHY THIS IS NOT A SECOND POLICY TABLE
    The verdicts live in `core/capabilities.py` and nowhere else. This file
    holds one frozenset, and it can only ever SUBTRACT from that table: a
    capability absent from ENABLED is refused here, before the broker is even
    asked. Nothing in this file can turn a CONFIRM into an ALLOW, or make a
    DENY reachable — there is no code path from here that grants anything.

    That asymmetry is the whole design. A phase gate that could widen
    permissions would be a second security system, which is the thing the
    previous phase spent its time removing.

WHY HAVE IT AT ALL
    `minecraft.attack` has a verdict in the central table because it will exist
    one day, and declaring it now means the refusal is explicit and testable
    rather than an unknown name that happens to fail closed. But the code to
    do it safely is not written, and a capability whose implementation does not
    exist should fail loudly rather than reach a half-built handler.
"""

from __future__ import annotations

from core import capabilities as core_caps

PHASE = "2"
"""Bumped when a later phase enables more. Recorded in the audit log so a log
line can be read against the code that produced it."""

ENABLED = frozenset({
    core_caps.MINECRAFT_OBSERVE,
    core_caps.MINECRAFT_READ_STATE,
    core_caps.MINECRAFT_CONTROL_SESSION,
    core_caps.MINECRAFT_MOVE,
    core_caps.MINECRAFT_LOOK,
    core_caps.MINECRAFT_STOP,
})
"""What Phase 2 can attempt: look at the game, describe what it knows, open a
bounded session, walk, turn, and stop.

Deliberately absent: attack, use_item, inventory, chat, command, launch."""

DISABLED_REASON = {
    core_caps.MINECRAFT_ATTACK:
        "Mining and fighting are not built yet — they need verified state to "
        "know whether the swing landed.",
    core_caps.MINECRAFT_USE_ITEM:
        "Placing and using items is not built yet.",
    core_caps.MINECRAFT_INVENTORY:
        "Inventory handling is not built yet — it needs a real state source to "
        "know what is in which slot.",
    core_caps.MINECRAFT_CHAT:
        "Chat is not built yet. On a server it reaches other people, so it "
        "will need its own confirmation when it is.",
    core_caps.MINECRAFT_COMMAND:
        "Slash commands are refused permanently, not postponed. There is no "
        "route from this subsystem to a command line.",
    core_caps.MINECRAFT_LAUNCH:
        "Starting the game is not built yet. Open Minecraft yourself and I "
        "will attach to it.",
}


def is_enabled(capability: str) -> bool:
    """Is this capability one the current phase will attempt?"""
    return capability in ENABLED


def why_disabled(capability: str) -> str:
    """A sentence explaining the refusal, for the model to relay.

    Falls back to a generic line rather than an empty string: a refusal with no
    reason is the kind of thing that gets retried in a loop."""
    if capability in ENABLED:
        return ""
    return DISABLED_REASON.get(
        capability,
        f"'{capability}' is not something I can do in this version.",
    )


def enabled_summary() -> str:
    """One line for a status report."""
    return ", ".join(sorted(c.split(".", 1)[-1] for c in ENABLED))


__all__ = ["PHASE", "ENABLED", "DISABLED_REASON",
           "is_enabled", "why_disabled", "enabled_summary"]
