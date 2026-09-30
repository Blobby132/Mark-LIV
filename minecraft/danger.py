"""
minecraft/danger.py — should a running task stop because the player is in
danger?

WHY THIS EXISTS
    A task runs for up to two minutes, and a mining step holds the attack
    button for three seconds at a time. Nothing used to look up from the job
    in between: a zombie walking up and hitting the player was, as far as
    the task knew, nothing at all, and it went on chopping.

    A person at the keyboard would stop. This is the check that does the
    same, between every step, from things the bridge already reports: health
    and the mobs around the player.

WHAT IT DOES NOT DO
    Fight, flee or heal. Those are decisions, and they belong to the person
    or the assistant, told plainly what happened. Stopping and saying why is
    the whole of it.

It reads a WorldState and returns a sentence or None. It presses nothing.
"""

from __future__ import annotations

HOSTILE_RADIUS = 5.0
"""A hostile mob this close stops the task. A zombie covers five blocks in
about two seconds -- less than one mining step -- and a creeper starts its
fuse at three."""

LEVEL_BAND = 3.0
"""Vertical distance within which a close mob counts. A zombie in a cave four
blocks under the player is five blocks away in a straight line and harmless;
treating it as a threat would stop every task above an occupied cave."""

HURT_BY = 2.0
"""Health lost since the best point in the task that counts as being hurt:
one heart. Anything less is noise such as a single cactus prick."""


def nearest_hostile(state):
    """The closest mob the bridge calls hostile, or None.

    An entity whose category the bridge could not say is not treated as
    hostile -- and not as safe either; it is simply not evidence. One well
    above or below the player (see LEVEL_BAND) is not counted, when both
    heights are known."""
    feet = getattr(state, "position", None)
    near = [e for e in (getattr(state, "nearby_entities", None) or ())
            if getattr(e, "hostile", None) is True
            and getattr(e, "distance", None) is not None
            and _level_with(feet, getattr(e, "position", None))]
    return min(near, key=lambda e: e.distance) if near else None


def _level_with(feet, where) -> bool:
    try:
        return abs(float(where[1]) - float(feet[1])) <= LEVEL_BAND
    except (TypeError, IndexError, ValueError):
        return True            # heights unknown: judge by distance alone


def _mob_name(entity) -> str:
    name = str(getattr(entity, "name", None) or "hostile mob").split(":")[-1]
    return " ".join(name.split("_"))


class DangerWatch:
    """Remembers the best health seen during one task, and says when to stop.

    One per task: a new task starts with no memory of damage taken during
    the last one."""

    def __init__(self, watch_health: bool = True,
                 watch_hostiles: bool = True):
        self.watch_health = watch_health
        self.watch_hostiles = watch_hostiles
        self.peak = None

    def check(self, state) -> str | None:
        """A reason to stop now, or None."""
        hurt = self._hurt(state)
        mob = nearest_hostile(state) if self.watch_hostiles else None
        close = mob if mob is not None and mob.distance <= HOSTILE_RADIUS \
            else None
        if hurt and close:
            return (f"I am taking damage ({hurt}) and a {_mob_name(close)} "
                    f"is {close.distance:.0f} blocks away")
        if hurt:
            return f"I am taking damage ({hurt})"
        if close:
            return (f"a {_mob_name(close)} is {close.distance:.0f} blocks "
                    f"away")
        return None

    def _hurt(self, state) -> str:
        health = getattr(state, "health", None)
        if not self.watch_health or not isinstance(health, (int, float)):
            return ""
        if self.peak is None or health > self.peak:
            self.peak = float(health)
            return ""
        if self.peak - health >= HURT_BY:
            return f"health {self.peak:.0f} to {health:.0f} of 20"
        return ""


__all__ = ["DangerWatch", "HOSTILE_RADIUS", "HURT_BY", "LEVEL_BAND",
           "nearest_hostile"]
