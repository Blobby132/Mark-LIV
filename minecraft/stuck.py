"""
minecraft/stuck.py — WHY it stopped, not just THAT it stopped.

THE PROBLEM
    `progress.py` notices that nothing is changing, and that is worth having
    as a backstop. But "I tried to move four times and nothing changed" is the
    same sentence for a one-block ledge, a three-block wall, a low branch, a
    cow standing in the doorway, a window that lost focus, and a route through
    ground that has since been dug out. Each of those wants a different
    response, and giving them all the generic one is how an agent presses W
    into a ledge it could have hopped.

    So this reads the terrain, the entities and the recent record, and names
    the situation. Every kind below comes with the recovery that fits it.

A DIAGNOSIS, NOT A DECISION
    Nothing here acts. It returns a kind and a recommended recovery, and the
    skill decides what to do with them — the same split as progress.py, for
    the same reason: judgement that can be tested without a controller.

THE KINDS
    VERTICAL_BLOCK       one block up, room to land          -> hop up
    COLLISION_STUCK      a wall, low ceiling, or hazard       -> re-route
    ENTITY_INTERFERENCE  a mob or player is in the way        -> wait, re-route
    UNKNOWN_TERRAIN      the next step was never scanned      -> observe again
    PATH_STALE           the route runs over changed ground   -> re-plan
    NO_ROUTE             nothing walkable reaches the goal    -> stop, say so
    INPUT_STUCK          nothing in the way, and no movement  -> stop, say so
    AIM_STUCK            corrections are not reducing error  -> stop aiming

    INPUT_STUCK is the one worth singling out. The scan says the way is clear,
    the key went down, and the player did not move. That is not a navigation
    problem and no amount of re-routing will fix it: the input is not reaching
    the game, the window lost focus, or the player is caught on something the
    scan cannot see. Saying so beats pressing forward until the limit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from minecraft import navigation as nav

VERTICAL_BLOCK = "vertical_block"
COLLISION_STUCK = "collision_stuck"
ENTITY_INTERFERENCE = "entity_interference"
UNKNOWN_TERRAIN = "unknown_terrain"
PATH_STALE = "path_stale"
NO_ROUTE = "no_route"
INPUT_STUCK = "input_stuck"
AIM_STUCK = "aim_stuck"

HOP = "hop"
REROUTE = "reroute"
WAIT = "wait"
OBSERVE = "observe"
REPLAN = "replan"
STOP = "stop"

RECOVERY = {
    VERTICAL_BLOCK: HOP,
    COLLISION_STUCK: REROUTE,
    ENTITY_INTERFERENCE: WAIT,
    UNKNOWN_TERRAIN: OBSERVE,
    PATH_STALE: REPLAN,
    NO_ROUTE: STOP,
    INPUT_STUCK: STOP,
    AIM_STUCK: STOP,
}

ENTITY_BLOCKING_RADIUS = 1.6
"""How close an entity must be, in the direction of travel, to be the reason.

A little over a block and a half: a mob standing in the next column. Further
than that and it is scenery, and blaming it would hide the real cause."""

MOVED_ENOUGH = 0.25
"""Horizontal movement below this is "did not move" for diagnosis purposes."""

AIM_NOT_IMPROVING = 3
"""Consecutive aim corrections that failed to reduce the error."""


@dataclass(frozen=True)
class Diagnosis:
    kind: str
    recovery: str
    detail: str = ""
    obstacle: object = None

    def describe(self) -> str:
        return {
            VERTICAL_BLOCK: "a one-block step in the way — hopping up",
            COLLISION_STUCK: "blocked — going round",
            ENTITY_INTERFERENCE: "something is standing in my way",
            UNKNOWN_TERRAIN: "the ground ahead was never scanned — looking again",
            PATH_STALE: "the route is out of date — planning again",
            NO_ROUTE: "there is no walkable way there",
            INPUT_STUCK: ("I pressed forward with nothing in the way and did "
                          "not move"),
            AIM_STUCK: "my corrections are not bringing the aim any closer",
        }.get(self.kind, self.kind) + (f": {self.detail}" if self.detail else "")


def diagnose_movement(state, heading_to=None, moved_by: float | None = None,
                      route_column=None) -> Diagnosis:
    """Name the reason a move gained no ground.

    `moved_by` is how far the player actually went on the failed step, if
    known. `route_column` is the next column the current route expects to
    stand on, used to tell a stale route from a real obstacle."""
    # Something standing in the way beats terrain: a cow in a clear corridor
    # reads as "nothing in the way" to the scan and is the actual cause.
    blocker = entity_in_the_way(state, heading_to)
    if blocker is not None:
        return Diagnosis(ENTITY_INTERFERENCE, WAIT,
                         detail=f"{blocker.name}, "
                                f"{blocker.distance:.1f} blocks away")

    seen = nav.obstacle_ahead(state, heading_to)

    if seen.kind == nav.UNSEEN:
        return Diagnosis(UNKNOWN_TERRAIN, OBSERVE, obstacle=seen,
                         detail=seen.describe())
    if seen.kind == nav.JUMPABLE:
        return Diagnosis(VERTICAL_BLOCK, HOP, obstacle=seen,
                         detail=seen.describe())
    if seen.needs_reroute:
        return Diagnosis(COLLISION_STUCK, REROUTE, obstacle=seen,
                         detail=seen.describe())

    if route_column is not None:
        local = nav.LocalMap.from_state(state)
        try:
            column = (int(route_column[0]), int(route_column[-1]))
        except (TypeError, IndexError, ValueError):
            column = None
        if column is not None and local.usable and not local.standable(*column):
            return Diagnosis(PATH_STALE, REPLAN,
                             detail=f"{column} is no longer somewhere to stand")

    # The scan says the way is clear. If we still did not move, the input is
    # not getting through or something unscanned is holding us.
    if moved_by is None or moved_by < MOVED_ENOUGH:
        return Diagnosis(INPUT_STUCK, STOP, obstacle=seen, detail=(
            "the scan shows open ground ahead. Either the input is not "
            "reaching Minecraft (is it the window in front?), or I am caught "
            "on something too thin for the scan to see — a fence post, a "
            "pane, a sign"))
    return Diagnosis(PATH_STALE, REPLAN, obstacle=seen,
                     detail="I moved, but not towards where I meant to go")


def entity_in_the_way(state, heading_to=None):
    """The nearest entity standing between the player and the next column."""
    position = getattr(state, "position", None)
    entities = getattr(state, "nearby_entities", None) or ()
    if position is None or not entities:
        return None
    direction = _direction(state, heading_to)
    if direction is None:
        return None

    best = None
    for entity in entities:
        where = getattr(entity, "position", None)
        if where is None:
            continue
        if getattr(entity, "category", None) == "item":
            continue                # dropped items do not block anything
        try:
            dx = where[0] - position[0]
            dz = where[2] - position[2]
        except (TypeError, IndexError):
            continue
        distance = math.hypot(dx, dz)
        if distance > ENTITY_BLOCKING_RADIUS or distance < 1e-6:
            continue
        # Ahead of us, not beside or behind.
        along = (dx * direction[0] + dz * direction[1]) / distance
        if along < 0.5:
            continue
        if best is None or distance < best[0]:
            best = (distance, entity)
    if best is None:
        return None

    entity = best[1]

    class _Seen:
        name = getattr(entity, "name", "something")
        distance = best[0]
    return _Seen()


def _direction(state, heading_to):
    position = getattr(state, "position", None)
    if heading_to is not None and position is not None:
        try:
            dx = float(heading_to[0]) + 0.5 - position[0]
            dz = float(heading_to[-1]) + 0.5 - position[2]
        except (TypeError, IndexError, ValueError):
            return None
        length = math.hypot(dx, dz)
        if length < 1e-6:
            return None
        return (dx / length, dz / length)
    rotation = getattr(state, "rotation", None)
    try:
        radians = math.radians(float(rotation[0]))
    except (TypeError, IndexError, ValueError):
        return None
    return (-math.sin(radians), math.cos(radians))


def diagnose_aim(errors) -> Diagnosis | None:
    """Is aiming converging? `errors` is the degrees-off before each
    correction, oldest first.

    Converging means each correction leaves less to correct. A run where it
    does not is a loop that will not settle on its own — a sign or scale that
    is wrong, a target that is moving, or a camera being fought by the
    player's own hand."""
    recent = [float(e) for e in errors if e is not None][-(AIM_NOT_IMPROVING + 1):]
    if len(recent) <= AIM_NOT_IMPROVING:
        return None
    worse = sum(1 for before, after in zip(recent, recent[1:])
                if after >= before * 0.9)
    if worse < AIM_NOT_IMPROVING:
        return None
    return Diagnosis(AIM_STUCK, STOP, detail=(
        f"the last {AIM_NOT_IMPROVING} corrections went "
        f"{' -> '.join(f'{e:.0f}°' for e in recent)}"))


__all__ = [
    "Diagnosis", "diagnose_movement", "diagnose_aim", "entity_in_the_way",
    "VERTICAL_BLOCK", "COLLISION_STUCK", "ENTITY_INTERFERENCE",
    "UNKNOWN_TERRAIN", "PATH_STALE", "NO_ROUTE", "INPUT_STUCK", "AIM_STUCK",
    "HOP", "REROUTE", "WAIT", "OBSERVE", "REPLAN", "STOP", "RECOVERY",
]
