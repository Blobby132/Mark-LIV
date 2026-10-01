"""
minecraft/skills/navigate.py -- getting about and looking about:
walk_forward, survey, find_block, and navigate_to (routing over the scanned
terrain). Moved here unchanged from skills.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft import action_spec, navigation as nav, verification as verify_mod
from minecraft import stuck as stuck_mod
from minecraft.state import UNKNOWN, NearbyBlock
from minecraft.task_runner import Step

from minecraft.skills.base import (
    CANNOT_SEE_TARGET, LOG_BLOCKS, _sweep_pixels, _tree_label,
)


@dataclass
class WalkForward:
    """Walk forward for roughly `seconds`, in bounded steps.

    A 6-second walk is not one 6-second key hold — the action limit forbids
    that, and rightly. It is four bounded moves with an observation between
    each, which is also what makes it interruptible: the guard runs before
    every one of them."""

    seconds: float = 3.0
    step_seconds: float = action_spec.MAX_MOVE_DURATION_S
    direction: str = "forward"

    name = "walk_forward"
    verifiable_with = ("position",)

    @property
    def goal(self) -> str:
        return f"walk {self.direction} for about {self.seconds:.0f}s"

    def plan(self, state, step_index: int, history: tuple):
        done = sum(r.action_result.get("actual_duration_ms", 0)
                   for r in history) / 1000.0
        remaining = self.seconds - done
        if remaining <= 0.05:
            return None
        return Step(
            action="move",
            params={"direction": self.direction,
                    "duration": min(remaining, self.step_seconds)},
            expectation=verify_mod.moved(min_distance=0.5),
            note=f"walk {self.direction} ({remaining:.1f}s left)",
        )


@dataclass
class Survey:
    """Turn all the way round in steps, reporting what is under the crosshair
    at each one.

    The honest answer to "look around and tell me what you see": the crosshair
    is the only place this version can identify a block, so a survey is a
    sequence of aimed readings rather than a scene description."""

    steps: int = 8
    delta_px: int | None = None

    name = "survey"
    goal = "look around and report what is there"
    verifiable_with = ("rotation",)

    def plan(self, state, step_index: int, history: tuple):
        if step_index >= self.steps:
            return None
        return Step(
            action="look",
            params={"dx": self.delta_px or _sweep_pixels(), "dy": 0},
            expectation=verify_mod.turned(min_degrees=2.0),
            note=f"turn {step_index + 1} of {self.steps}",
        )


@dataclass
class FindBlock:
    """Work out where a kind of block is, and point at it.

    TWO WAYS OF LOOKING, AND THEY ARE NOT EQUALLY GOOD
        With the bridge mod running this reads the terrain scan, picks the
        nearest matching block, and turns straight to it. It knows where the
        thing is before it moves the camera, so "no oak log nearby" is a
        statement about a scanned volume rather than about twelve aim points.

        Without the mod it falls back to what the previous version did: sweep
        the crosshair round and check each aim point. That finds a tree
        roughly ahead at eye level and misses one behind a hill, above, or
        past the crosshair's reach.

    THE RESULT SAYS WHICH ONE ANSWERED
        "The scan found no oak log within 10 blocks" and "the crosshair did
        not cross an oak log while I turned" are different claims about
        different things, and reporting the second as the first would be a
        lie about how hard it looked."""

    wanted: frozenset = LOG_BLOCKS
    steps: int = 12
    delta_px: int | None = None
    aim_tolerance_deg: float = 6.0

    name = "find_block"
    # Either field settles it: `surface` for the scan, `target_block` for the
    # crosshair. The skill needs one of them, not both, and says which it used.
    verifiable_with = ("surface", "target_block")

    _found: str = ""
    _missing: str = ""
    _located: object = None
    _aimed: bool = False

    @property
    def goal(self) -> str:
        return f"find one of: {', '.join(sorted(self.wanted)[:3])}…"

    @property
    def located(self):
        """The block the scan found, if the scan is what found it."""
        return self._located

    @property
    def done_reason(self) -> str:
        return self._found or self._missing or (
            "swept the view without the crosshair crossing one")

    @property
    def failed(self) -> bool:
        return not self._found or self._found == CANNOT_SEE_TARGET

    def plan(self, state, step_index: int, history: tuple):
        local = nav.LocalMap.from_state(state)
        crosshair_readable = state.confidence_of("target_block") != UNKNOWN

        # The crosshair is the stronger evidence when it is available: it says
        # what is actually under the aim point, not merely what is nearby.
        if crosshair_readable:
            name = getattr(state.target_block, "name", None)
            if name in self.wanted:
                self._found = f"the crosshair is on {name}"
                return None

        if local.usable:
            return self._aim_from_the_map(state, local, crosshair_readable)

        if not crosshair_readable:
            self._found = CANNOT_SEE_TARGET
            return None

        return self._sweep(step_index)

    def _aim_from_the_map(self, state, local, crosshair_readable):
        """Turn to the nearest match the scan reported."""
        if self._located is None:
            matches = nav.blocks_matching(state, self.wanted)
            if not matches:
                kinds = ", ".join(sorted(self.wanted)[:3])
                self._found = ""
                self._missing = (
                    f"the scan covered {local.known_columns} columns within "
                    f"{local.radius or '?'} blocks and found no {kinds}")
                return None
            self._located = min(matches,
                                key=lambda b: b.distance_to(state.position))

        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   self._located.position)
        if error > self.aim_tolerance_deg and not self._aimed:
            if dx == 0 and dy == 0:
                self._aimed = True
            else:
                return Step(
                    action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=1.0),
                    note=f"aim at {self._located.name} "
                         f"{self._located.position}",
                )

        # Facing it. With the crosshair readable the next iteration confirms
        # it properly; without, the honest claim is about the scan only.
        self._aimed = True
        where = self._located.position
        distance = self._located.distance_to(state.position)
        if crosshair_readable:
            under = getattr(state.target_block, "name", None)
            self._found = (
                f"the nearest {self._located.name} is at {where}, "
                f"{distance:.0f} blocks away — I am facing it, and the "
                f"crosshair is on {under or 'nothing'}")
        else:
            self._found = (
                f"the nearest {self._located.name} is at {where}, "
                f"{distance:.0f} blocks away, and I am now facing it. I "
                f"cannot read the crosshair, so this is the scan's answer, "
                f"not a confirmed aim")
        return None

    def _sweep(self, step_index: int):
        if step_index >= self.steps:
            return None
        return Step(
            action="look",
            params={"dx": self.delta_px or _sweep_pixels(), "dy": 0},
            expectation=verify_mod.turned(min_degrees=2.0),
            note=(f"sweep {step_index + 1} of {self.steps} looking for a "
                  f"target (no terrain scan — crosshair only)"),
        )



CANNOT_SEE_WORLD = (
    "I cannot read the world around me, so I have no map to navigate with. "
    "That needs the bridge mod running in Minecraft — run bridge_check.bat "
    "to see what it is reporting. I stopped rather than walk in a direction "
    "I picked at random."
)
"""Why a navigation skill gives up immediately.

Without the terrain scan the only honest map is an empty one, and walking
forward on an empty map is not navigation, it is guessing with extra steps."""

# Roughly how fast a player walks, in blocks per second. Used only to size a
# movement step so it does not overshoot the next waypoint -- the verification
# measures what actually happened, so an inaccuracy here costs a step, not
# correctness.
WALK_BLOCKS_PER_S = 4.3

SPRINT_BLOCKS_PER_S = 5.6
"""Minecraft's sprint, for sizing a sprint step."""

SPRINT_FROM_BLOCKS = 3.0
"""A straight, clear stretch at least this long is sprinted, when the walk
asks for sprinting (flee). Shorter, and a sprint's run-up is not worth it."""

# How far off the desired heading before turning is worth a step of its own.
# Minecraft movement is forgiving; correcting a 5 degree error every step
# would spend the whole budget turning.
YAW_TOLERANCE_DEG = 12.0

# Waypoints to walk before re-reading the map and re-planning. The whole point
# of §5: the world moves, and a path computed eight observations ago is a
# guess about a world that has since changed.
REVALIDATE_EVERY = 3

# Consecutive movement steps that close no distance before trying a hop
# (a one-block step up, a fence, a slab edge), and before giving up.
STALLS_BEFORE_OBSTACLE_CHECK = 1
"""How many fruitless moves before asking what is in the way.

One. Looking is free — it reads the scan already in hand — and the previous
value of two meant walking into the same wall twice before wondering about
it."""

MAX_REROUTES = 3
"""How many times one task may throw its route away and try another.

Bounded because a re-route that keeps finding the same blocked way is a loop,
and because the honest answer after three is "I cannot get there", which is
more use than twenty more steps of trying."""

REROUTE_REFILL_BLOCKS = 3.0
"""Blocks of real progress -- closer to the destination, not merely moved --
after which the reroute budget is full again. The budget is for one
obstacle that keeps winning, not for a whole walk: a long walk past four
cows gave up at the fourth. Measured towards the goal so that a detour
which wanders away and back cannot buy itself another try."""

AVOID_FOR_REPLANS = 3
"""How many replans a column avoided because a MOB stood in it stays avoided.
Mobs move: a cow in a doorway was avoided for the rest of the walk, and when
the doorway was the only way the destination became "unreachable". Columns
avoided for something that does not move -- a fence post the scan cannot see
-- stay avoided for the whole task."""

CAUTIOUS_STEP_S = 0.3
"""The longest single move when the swept path shows something ahead.

About a block and a quarter of walking. Near an obstacle the next observation
is worth more than the distance, and a two-second stride into a ledge was
exactly what made it walk into things and stay there."""

INPUT_STUCK_AFTER = 2
"""Stalls on open ground before concluding the input is not getting through.

Two. One can be a hiccup — a snapshot that lagged, a mob that nudged you.
Two, with the scan still showing clear ground, is a pattern, and pressing
forward a third time has never once fixed it."""

MAX_STALLS = 4

MAX_HOPS_ONTO = 3
"""Hops onto the same column before giving up. A hop onto a lone block can
carry past it, the walk turns round and hops back past it the other way --
every hop moves, so no stall is ever counted, and it went on until the
task's step limit."""


@dataclass
class NavigateTo:
    """Walk to a place, re-reading the map as you go.

    WHAT MAKES THIS DIFFERENT FROM `walk_forward`
        `walk_forward` presses a key for a while. This looks at the terrain,
        finds a route around what is in the way, turns to face the next
        waypoint, walks a measured amount, and then looks again — because the
        route it computed is a claim about a world that may have changed, and
        a plan executed without re-checking is how a bot walks into a lake.

    WHAT IT DOES NOT DO
        It does not press keys; it returns steps and the runner decides
        whether they are allowed. It does not dig, place blocks or bridge
        gaps — if there is no walkable route it says so rather than inventing
        one. It does not path beyond the scan radius: "I cannot see that far"
        is the honest answer to a destination 200 blocks away, not a reason to
        set off hopefully in its direction.

    GIVING UP IS A RESULT
        A destination behind a ravine, up a cliff, or outside the scan is
        unreachable, and the skill reports which. Walking into a wall until
        the step limit runs out would also "not succeed", but it would take
        twenty steps to say so and would leave the player somewhere else."""

    destination: tuple | None = None
    target: str = ""
    arrive_within: float = 1.5
    max_stalls: int = MAX_STALLS
    keep_off: frozenset = frozenset()
    """(x, z) columns never routed through -- a structure being built,
    whose wall tops are walkable but are no place to be."""
    sprint: bool = False
    """Sprint the long, clear, straight stretches (flee)."""

    name = "navigate_to"
    verifiable_with = ("position", "surface")
    # Walking is how you get away from a mob, or past one on the way home at
    # night: stopping it for danger -- or refusing to start -- would take
    # away the one thing that helps. It says what is close when it ends.
    # A task that walks as PART of its job (collect_logs) still stops.
    watch_health = False
    watch_hostiles = False

    _destination: tuple | None = None
    _path: object = None
    _walked: int = 0
    _hops: dict = field(default_factory=dict)   # column -> hops onto it
    _stalls: int = 0
    _hopped: bool = False
    _reroutes: int = 0
    _obstacle: object = None
    _diagnosis: object = None
    _history: tuple = ()
    _avoid: dict = field(default_factory=dict)   # column -> replans left
    _blocker_name: str = ""
    _rerouted_left: float | None = None   # distance to go at the last reroute
    _diagnosed_at: int = -1               # len(history) of the last diagnosis
    _blocked_by: str = ""
    _aiming_at: float | None = None
    _skip_to: int = 0
    _seen_at: tuple | None = None
    _left_to_go: float | None = None
    _blind: bool = False
    _stopped: str = ""
    _arrived: bool = False
    _goal_block: object = None

    @property
    def goal(self) -> str:
        if self.target:
            return f"walk to the nearest {self.target}"
        if self.destination:
            return f"walk to ({self.destination[0]}, {self.destination[-1]})"
        return "walk somewhere"

    @property
    def failed(self) -> bool:
        return not self._arrived

    @property
    def done_reason(self) -> str:
        if self._blind:
            return CANNOT_SEE_WORLD
        if self._stopped:
            return self._stopped
        if self._diagnosis is not None and not self._arrived:
            return (f"I stopped: {self._diagnosis.describe()}. "
                    f"{self._where_text(self._seen_at)} is as far as I got.")
        if self._arrived:
            where = self._destination
            text = (f"arrived at ({where[0]}, {where[1]})" if where
                    else "arrived")
            return text + self._how_close()
        # Running out of steps mid-route is a normal outcome for a long walk,
        # and "stopped" on its own is useless to anyone deciding what to do
        # next. Say where it got to and how much was left, so calling it
        # again is an obvious option rather than a guess.
        if self._destination and self._left_to_go is not None:
            return (f"I stopped {self._left_to_go:.0f} blocks short of "
                    f"({self._destination[0]}, {self._destination[1]}), at "
                    f"{self._where_text(self._seen_at)}. The route was fine; "
                    f"a task only gets so many steps. Running it again "
                    f"carries on from here.")
        return "stopped without reaching the destination"

    # ── planning ─────────────────────────────────────────────────────────

    def plan(self, state, step_index: int, history: tuple):
        local = nav.LocalMap.from_state(state)
        if not local.usable:
            self._blind = True
            return None

        if self._destination is None:
            self._destination = self._resolve(state, local)
            if self._destination is None:
                return None            # _stopped says why

        self._seen_at = tuple(state.position) if state.position else None
        self._left_to_go = self._remaining(state)

        if self._at_destination(state):
            self._arrived = True
            return None

        self._history = history
        self._note_progress(history)
        if self._stalls >= self.max_stalls:
            self._stopped = (
                f"I stopped after {self._stalls} moves that got me no closer. "
                f"Something is in the way that I cannot see well enough to "
                f"route around — I am at {self._where(state)} and the "
                f"destination is {self._destination}."
            )
            return None

        step = self._follow_path(state, local)
        if step is None and not self._stopped:
            self._stopped = ("I ran out of route before reaching the "
                             "destination.")
        return step

    # NO `replan`, deliberately. When the runner's progress monitor calls a
    # task stuck, the only alternative this skill has is to path again — and
    # it already does that by itself whenever the route stops being true.
    # Offering "try another route" here would hand back the action that is
    # failing and reset the stuck detector without changing anything, which
    # is precisely the loop that made collect_logs spin twenty times.

    # ── the pieces ───────────────────────────────────────────────────────

    def _resolve(self, state, local):
        """Settle on one destination column, once.

        Pinned rather than recomputed, because "the nearest log" changes as
        you walk towards a log, and a skill that re-picks every step walks
        the perpendicular bisector between two trees forever."""
        if self.target:
            block = nav.nearest_block(state, self.target, reachable_only=True)
            if block is None:
                seen = nav.nearest_block(state, self.target)
                if seen is None:
                    self._stopped = (
                        f"I cannot see any {self.target} within "
                        f"{local.radius or '?'} blocks. It may be there; it is "
                        f"not somewhere I can see.")
                else:
                    self._stopped = (
                        f"I can see {block_label(seen)} at "
                        f"{seen.position}, but I cannot find a walkable route "
                        f"to it from here.")
                return None
            column = nav.approach_column(state, block)
            if column is None:
                self._stopped = (f"I can see {block_label(block)} but there "
                                 f"is nowhere next to it I can stand.")
                return None
            self._goal_block = block
            if block.name in LOG_BLOCKS:
                # A tree is where its trunk stands, not wherever the nearest
                # log happens to be -- that was a branch at y 69.
                logs = nav.tree_logs(state, {block.position}) or (block,)
                self._goal_block = min(logs, key=lambda b: b.y)
            return column

        if self.destination is None:
            self._stopped = "No destination was given."
            return None
        try:
            column = (int(self.destination[0]),
                      int(self.destination[-1]))
        except (TypeError, ValueError, IndexError):
            self._stopped = (f"{self.destination!r} is not a destination I "
                             f"can read.")
            return None
        if not local.is_known(*column):
            self._stopped = (f"({column[0]}, {column[1]}) is outside what I "
                             f"can see, so I will not guess a way to it.")
            return None
        if not local.standable(*column):
            self._stopped = (f"({column[0]}, {column[1]}) is not somewhere I "
                             f"could stand.")
            return None
        return column

    def _how_close(self) -> str:
        """For a walk to a block: how far from it this ended, and, when that
        is not beside it, that it is as close as the ground allows.

        A real run answered "walk to the nearest tree" with "arrived, 0
        steps" -- it was already three blocks off, the nearest place it
        could stand, and nothing said so."""
        block = self._goal_block
        if block is None or self._seen_at is None:
            return ""
        try:
            gap = math.dist((self._seen_at[0], self._seen_at[2]),
                            (block.x + 0.5, block.z + 0.5))
        except (TypeError, IndexError, AttributeError):
            return ""
        if block.name in LOG_BLOCKS:
            text = (f", {gap:.0f} blocks from "
                    f"{_tree_label((block.name, (block.x, block.z)))}")
        else:
            text = f", {gap:.0f} blocks from {block_label(block)} at " \
                   f"{block.position}"
        if gap > 2.0:
            text += (" — as close as I can get: nowhere nearer to it has "
                     "room to stand, or I cannot reach it")
        return text

    def _at_destination(self, state) -> bool:
        try:
            x, _y, z = state.position
            return ((x - (self._destination[0] + 0.5)) ** 2
                    + (z - (self._destination[1] + 0.5)) ** 2) \
                <= self.arrive_within ** 2
        except (TypeError, ValueError):
            return False

    def _note_progress(self, history) -> None:
        """Watch the last step: did it get us anywhere, and did the turn go
        the way it was supposed to?"""
        if not history:
            return
        last = history[-1]
        action = last.step.get("action")

        if action == "look":
            self._check_turn_direction(last)
            return

        # A hop that achieves nothing is as stuck as a walk that achieves
        # nothing. Counting only walks let the skill alternate walk-hop-walk
        # forever with the stall count pinned one below the limit. The same
        # goes for move_and_jump, the step a route takes up a block: failed
        # hops were never counted, and a hop that could not get up was
        # offered forever.
        if action not in ("move", "jump", "move_and_jump"):
            return
        if last.verification.get("status") == verify_mod.SUCCESS:
            self._stalls = 0
            self._hopped = False
            if self._rerouted_left is not None and \
                    self._rerouted_left - self._left_to_go \
                    >= REROUTE_REFILL_BLOCKS:
                self._reroutes = 0
                self._rerouted_left = None
        else:
            self._stalls += 1

    def _check_turn_direction(self, record) -> None:
        """Watch one real turn and learn from it: which way, and how far.

        WHY THIS EXISTS
            How many pixels of mouse movement make a degree depends on the
            player's sensitivity slider, and which SIGN turns which way is a
            convention two layers apart — Minecraft's yaw and the operating
            system's mouse deltas — that nothing in the bridge reports.

            Guess the sign wrong and every correction doubles the error: the
            agent spins forever, looking exactly like a pathfinding bug.
            Guess the scale wrong and it overshoots every heading and spends
            its whole step budget correcting.

            So neither is guessed for longer than one turn. The first turn of
            a walk is a measurement, and everything after it uses what was
            measured on THIS machine."""
        if self._aiming_at is None:
            return
        try:
            was = record.state_before["rotation"][0]
            now = record.state_after["rotation"][0]
            asked = float(record.step["params"].get("dx", 0))
        except (KeyError, TypeError, IndexError, ValueError):
            return
        if was is None or now is None:
            return

        # What actually reached the game: the action spec clamps a big turn,
        # and measuring against the unclamped request would read as a
        # sensitivity several times too high.
        sent = max(-action_spec.MAX_LOOK_DELTA_PX,
                   min(asked, action_spec.MAX_LOOK_DELTA_PX))
        turned = nav.yaw_difference(was, now)
        wanted = nav.yaw_difference(was, self._aiming_at)

        if abs(wanted) < 3.0 or abs(turned) < 1.0:
            return
        # The measurement itself carries the sign: a machine that turns the
        # other way produces a negative scale, and the next correction
        # divides by it and points the right way. There is no separate
        # "inverted" flag here any more, because two mechanisms correcting
        # the same error get out of step and fight.
        nav.calibrate(sent, turned)

    def _follow_path(self, state, local):
        if self._needs_new_path(state, local):
            self._path = nav.find_path(state, self._destination,
                                       avoid=set(self._avoid)
                                       | set(self.keep_off))
            if not self._path.found and self._avoid:
                # Avoiding the mob's column leaves no way at all: the mob is
                # standing in the only way. Take it anyway -- it may move --
                # and if it does not, say that rather than "impassable".
                direct = nav.find_path(state, self._destination,
                                       avoid=set(self.keep_off))
                if direct.found:
                    self._blocked_by = self._blocker_name or "mob"
                    self._path = direct
            elif self._path.found:
                self._blocked_by = ""       # there is a way round it now
            self._age_avoids()
            self._walked = 0
            self._skip_to = 0       # it counted the old route's waypoints
            if not self._path.found:
                self._stopped = self._path.reason
                return None

        waypoint = self._next_waypoint(local, state)
        if waypoint is None:
            return None
        recentring = False
        middle = self._recentre(local, state, waypoint)
        if middle is not None:
            waypoint, recentring = middle, True

        here = state.position
        # The CENTRE of the waypoint's column. Waypoints are block
        # coordinates, which name a column's corner; aiming at the corner is
        # harmless from across a clearing and, standing inside the column,
        # points in whatever direction the corner happens to lie — which in a
        # gap in a wall was straight back into the wall.
        target = (waypoint[0] + 0.5, waypoint[1], waypoint[2] + 0.5)
        desired = nav.yaw_to(here, target)
        current = (state.rotation or (None, None))[0]
        if current is None:
            # Rotation unreadable: walking on a heading you cannot measure is
            # guessing. Say so rather than press forward hopefully.
            self._stopped = ("I cannot read which way I am facing, so I "
                             "cannot steer.")
            return None

        off_by = nav.yaw_difference(current, desired)
        if abs(off_by) > YAW_TOLERANCE_DEG:
            dx = nav.look_delta_for(current, desired)
            self._aiming_at = desired
            return Step(
                action="look",
                params={"dx": dx, "dy": 0},
                expectation=verify_mod.turned(min_degrees=2.0),
                note=(f"turn {off_by:+.0f}° towards "
                      f"({waypoint[0]:.0f}, {waypoint[2]:.0f})"),
            )

        if self._stalls >= STALLS_BEFORE_OBSTACLE_CHECK \
                and self._diagnosed_at != len(self._history):
            # Once per stall. A free re-plan leaves the stall counted and
            # comes straight back here; diagnosing the same stall again
            # would re-plan again, forever.
            self._diagnosed_at = len(self._history)
            blocked = self._handle_obstacle(state, waypoint, self._history)
            if blocked is not None:
                return blocked
            if self._stopped:
                return None
            if self._path is None:
                # The diagnosis threw the route away. Plan again now, from
                # this observation, rather than walking the old one.
                return self._follow_path(state, local)

        self._aiming_at = None
        gap = self._gap_to(here, target)
        ahead = self._look_ahead(local, here, target)

        if ahead == "climb" and not recentring:
            # The route steps up a full block. Walking into it first and
            # THEN deciding to jump costs a step and a failed move every
            # time; the map already says it is there. Hop straight up.
            if self._hop_spent((int(waypoint[0]), int(waypoint[2]))):
                return None
            self._walked = max(self._walked + 1, self._skip_to + 1)
            return Step(
                action="move_and_jump",
                params={"direction": "forward",
                        "duration": min(action_spec.MAX_HOP_DURATION_S,
                                        max(0.35, gap / WALK_BLOCKS_PER_S))},
                expectation=verify_mod.moved(min_distance=0.3),
                note=(f"hop up onto ({waypoint[0]:.0f}, {waypoint[2]:.0f})"),
            )

        if recentring:
            return Step(
                action="move",
                params={"direction": "forward",
                        "duration": self._step_seconds(gap, cautious=True)},
                expectation=verify_mod.moved(min_distance=0.05),
                note=("back to the middle of this block: from the edge the "
                      "way on scrapes a wall"))
        self._walked = max(self._walked + 1, self._skip_to + 1)
        if self.sprint and ahead is None and gap >= SPRINT_FROM_BLOCKS:
            return Step(
                action="sprint",
                params={"direction": "forward",
                        "duration": min(action_spec.MAX_SPRINT_DURATION_S,
                                        gap / SPRINT_BLOCKS_PER_S)},
                expectation=verify_mod.moved(min_distance=0.5),
                note=(f"sprint to ({waypoint[0]:.0f}, {waypoint[2]:.0f}) — "
                      f"{gap:.0f} blocks of clear ground"))
        return Step(
            action="move",
            params={"direction": "forward",
                    "duration": self._step_seconds(gap, cautious=ahead is not None)},
            # Judged against the WAYPOINT, not the destination. Walking round
            # a wall means walking away from where you are going, and a check
            # that only ever asks "are you nearer the goal" calls every
            # correct detour a failure -- then the stall counter gives up
            # four steps into a route that was working.
            expectation=verify_mod.closer_to(
                (target[0], target[2]),
                min_gain=max(0.15, min(0.4, gap * 0.5))),
            note=(f"walk to ({waypoint[0]:.0f}, {waypoint[2]:.0f}) — "
                  f"{self._remaining(state):.0f} blocks to go"),
        )

    @staticmethod
    def _look_ahead(local, here, target):
        """What the body would meet walking straight at the target.

        None for a clear walk, "climb" for a one-block step a hop clears, or
        another reason when something else is in the way — in which case the
        step is kept short so the next observation comes before the wall."""
        try:
            start = (float(here[0]), float(here[2]))
            end = (float(target[0]), float(target[2]))
        except (TypeError, IndexError, ValueError):
            return None
        blocked = local.first_blocked(start, end, allow_climb=False)
        if blocked is None:
            return None
        _column, reason = blocked
        if reason == "climb" and local.first_blocked(start, end,
                                                     allow_climb=True) is None:
            return "climb"
        return reason

    def _handle_obstacle(self, state, waypoint, history=()):
        """Stopped making ground: find out WHY, and answer that.

        "It stalled" is not a diagnosis. A one-block step, a three-block wall,
        a low branch, a cow in the doorway and a window that lost focus all
        stop you dead and want completely different responses — see
        minecraft/stuck.py for the eight kinds and why each gets its own.

        Returns a Step to try, or None to fall through to ordinary walking
        (with the route cleared, when the right answer is a new one)."""
        try:
            heading = (int(waypoint[0]), int(waypoint[2]))
        except (TypeError, IndexError, ValueError):
            heading = None
        route_column = heading
        diagnosis = stuck_mod.diagnose_movement(
            state, heading, moved_by=_last_move_distance(history),
            route_column=route_column)
        self._diagnosis = diagnosis
        self._obstacle = diagnosis.obstacle

        if diagnosis.recovery == stuck_mod.HOP:
            column = getattr(diagnosis.obstacle, "column", None)
            if column is not None and not self._hopped \
                    and self._hop_spent(tuple(column)[:2] if len(column) == 2
                                        else (column[0], column[-1])):
                return None
            if not self._hopped:
                # One block, with room to land on it. Walking and jumping
                # TOGETHER is the only thing that works: jump on the spot and
                # you come straight back down where you started, which is why
                # this used to need a person to say "jump" and still not get
                # up.
                self._hopped = True
                return Step(
                    action="move_and_jump",
                    params={"direction": "forward"},
                    expectation=verify_mod.moved(min_distance=0.3),
                    note=f"hop up — {diagnosis.obstacle.describe()}",
                )
            # Hopped already and still stuck: it is not the step it looked
            # like. Treat it as a wall and go round.
            return self._reroute(diagnosis, avoid=diagnosis.obstacle.column)

        if diagnosis.recovery == stuck_mod.GO_ROUND:
            # Round the MOB'S column -- never the heading, which is the
            # smoothed far waypoint and can be the destination itself.
            blocker = diagnosis.blocker
            avoid = getattr(blocker, "column", None)
            self._blocker_name = getattr(blocker, "name", "") or "mob"
            return self._reroute(diagnosis, avoid=avoid,
                                 for_replans=AVOID_FOR_REPLANS)

        if diagnosis.recovery == stuck_mod.REROUTE:
            return self._reroute(
                diagnosis, avoid=getattr(diagnosis.obstacle, "column", None))

        if diagnosis.recovery in (stuck_mod.OBSERVE, stuck_mod.REPLAN):
            # Unknown ground is not empty ground, and a route over changed
            # ground is not a route. Plan again from a fresh picture — the
            # runner has already taken one.
            return self._reroute(diagnosis, avoid=None)

        if diagnosis.recovery == stuck_mod.STOP:
            if diagnosis.kind == stuck_mod.INPUT_STUCK \
                    and self._stalls < INPUT_STUCK_AFTER:
                return None     # once can be a hiccup; twice is a pattern
            # Stop means stop. Returning a placeholder step here instead —
            # which the first version did — kept the task alive and repeated
            # "stopping" until the step limit.
            self._stopped = diagnosis.describe()
            return None
        return None

    def _reroute(self, diagnosis, avoid=None, for_replans=None):
        """Throw the route away and plan another, within a bound.

        The stall counter resets, because a new route is a genuinely new
        attempt rather than the same failing action offered again — which is
        the distinction the old replan got wrong and spun on.

        A re-plan that avoids nothing -- look again, the route is stale --
        is free: it goes round nothing, so it is not a try at getting past
        anything. It does not forgive the stall either, so MAX_STALLS still
        ends a loop of them."""
        if avoid is None:
            self._path = None
            self._walked = 0
            self._skip_to = 0       # it counted the old route's waypoints
            return None
        if self._reroutes >= MAX_REROUTES:
            if self._blocked_by:
                self._stopped = (
                    f"a {self._blocked_by} is blocking the only way, and it "
                    f"has not moved after {MAX_REROUTES} tries.")
            else:
                self._stopped = (
                    f"{diagnosis.describe()} — and {MAX_REROUTES} attempts "
                    f"to go another way did not get past it.")
            return None
        self._reroutes += 1
        self._rerouted_left = self._left_to_go
        self._avoid_for(avoid, for_replans)
        self._path = None
        self._walked = 0
        self._skip_to = 0       # it counted the old route's waypoints
        self._stalls = 0
        self._hopped = False
        return None

    def _avoid_for(self, column, for_replans=None) -> None:
        """Route round `column`: for the next `for_replans` replans, or for
        the rest of the task when None."""
        try:
            key = (int(column[0]), int(column[-1]))
        except (TypeError, IndexError, ValueError):
            return
        self._avoid[key] = for_replans

    def _age_avoids(self) -> None:
        """One replan has used the avoided columns; forget expired ones."""
        for column in list(self._avoid):
            if self._avoid[column] is None:
                continue                    # not a mob: avoided for good
            self._avoid[column] -= 1
            if self._avoid[column] <= 0:
                del self._avoid[column]

    def _hop_spent(self, column) -> bool:
        """Count a hop at `column`; True (and stopped, saying why) when
        there have been MAX_HOPS_ONTO already."""
        hops = self._hops.get(column, 0)
        if hops >= MAX_HOPS_ONTO:
            self._stopped = (
                f"I hopped at the block at ({column[0]}, {column[1]}) "
                f"{hops} times and kept landing past it or short of it -- a "
                f"single raised block is hard to land on.")
            return True
        self._hops[column] = hops + 1
        return False

    @staticmethod
    def _recentre(local, state, waypoint):
        """The column the player is in, as a waypoint, when they stand off
        its middle and the straight line from where they really are to the
        next waypoint is not walkable -- a shoulder over the corner of a
        wall, which a route between column centres never meets. Else None.
        Walking to the middle first does not count as reaching anything."""
        try:
            x, _y, z = (float(v) for v in state.position)
        except (TypeError, ValueError):
            return None
        mine = (math.floor(x), math.floor(z))
        if math.dist((x, z), (mine[0] + 0.5, mine[1] + 0.5)) < 0.15:
            return None
        ahead = (waypoint[0] + 0.5, waypoint[2] + 0.5)
        if (math.floor(ahead[0]), math.floor(ahead[1])) == mine \
                or local.first_blocked((x, z), ahead,
                                       allow_climb=True) is None:
            return None                 # clear, or only a step up to hop
        return (mine[0], waypoint[1], mine[1])

    def _needs_new_path(self, state, local) -> bool:
        """Re-plan when the plan is old, gone, or no longer true."""
        if self._path is None or not self._path.found:
            return True
        if self._walked >= REVALIDATE_EVERY:
            return True
        nxt = self._next_waypoint()
        if nxt is None:
            return True
        if not local.standable(int(nxt[0]), int(nxt[2])):
            return True            # the world changed under the route
        # Drifted off the route entirely (pushed by a mob, fell, slid).
        try:
            x, _y, z = state.position
            if ((x - nxt[0]) ** 2 + (z - nxt[2]) ** 2) ** 0.5 > 6.0:
                return True
        except (TypeError, ValueError):
            return True
        return False

    def _next_waypoint(self, local=None, state=None):
        """Where to actually walk next.

        Not simply "the next waypoint": A* hands back a chain of one-block
        hops, and walking them one at a time costs a step per block — which
        is what made a fifteen-block stroll hit the task limit having barely
        moved. When several waypoints lie on a clear straight line, this
        returns the furthest of them, and one two-second move replaces eight
        short ones.

        Falls back to the plain next waypoint when there is no map to check
        the line against."""
        if self._path is None or not self._path.waypoints:
            return None
        index = min(self._walked, len(self._path.waypoints) - 1)
        if local is None or state is None:
            return self._path.waypoints[index]

        # Already standing in this waypoint's column: it is behind us, not
        # ahead. Walking "to" the column you are in turns you towards its
        # centre, which from its edge can be any direction at all.
        try:
            mine = (math.floor(state.position[0]), math.floor(state.position[2]))
        except (TypeError, IndexError, ValueError):
            mine = None
        while (mine is not None and index < len(self._path.waypoints) - 1
               and (self._path.waypoints[index][0],
                    self._path.waypoints[index][2]) == mine):
            index += 1
            self._walked = index

        try:
            # The REAL position, not the column it is in. The player is almost
            # never at a column's centre, and a line traced from the centre is
            # not the line they will walk.
            here = (float(state.position[0]), float(state.position[2]))
        except (TypeError, IndexError, ValueError):
            return self._path.waypoints[index]

        far, far_index = nav.furthest_clear(local, here,
                                            self._path.waypoints, index,
                                            avoid=set(self._avoid)
                                            | set(self.keep_off))
        if far is None:
            return self._path.waypoints[index]
        # Remember how many waypoints this move consumes, so the next call
        # does not re-walk ground already covered.
        self._skip_to = far_index
        return far

    @staticmethod
    def _gap_to(here, waypoint) -> float:
        try:
            return ((here[0] - waypoint[0]) ** 2
                    + (here[2] - waypoint[2]) ** 2) ** 0.5
        except (TypeError, IndexError):
            return 1.0

    def _step_seconds(self, gap: float, cautious: bool = False) -> float:
        """Long enough to reach the waypoint, short enough not to sail past.

        Overshooting is the characteristic navigation bug: a two second hold
        crosses eight blocks, and a route made of one-block steps becomes a
        zigzag across the whole clearing.

        `cautious` when something is in the way ahead: the step is capped
        at about a block, so the next observation comes before the obstacle
        rather than after walking into it. Open ground gets the long stride;
        tight ground gets short ones."""
        wanted = gap / WALK_BLOCKS_PER_S
        ceiling = action_spec.MAX_MOVE_DURATION_S
        if cautious:
            ceiling = min(ceiling, CAUTIOUS_STEP_S)
        return max(action_spec.MIN_MOVE_DURATION_S, min(wanted, ceiling))

    def _remaining(self, state) -> float:
        try:
            x, _y, z = state.position
            return ((x - self._destination[0]) ** 2
                    + (z - self._destination[1]) ** 2) ** 0.5
        except (TypeError, ValueError):
            return 0.0

    def _where(self, state) -> str:
        return self._where_text(getattr(state, "position", None))

    @staticmethod
    def _where_text(position) -> str:
        try:
            x, y, z = position
            return f"({x:.0f}, {y:.0f}, {z:.0f})"
        except (TypeError, ValueError):
            return "somewhere I cannot read"


def nav_target(position, name):
    """A minimal block reference: a position and a name, nothing invented."""
    from minecraft.state import NearbyBlock
    return NearbyBlock(x=position[0], y=position[1], z=position[2],
                       name=str(name).split(":")[-1], solid=True)


def _last_move_distance(history):
    """How far the last movement step actually carried the player, or None.

    Horizontal only: falling is not progress, and the diagnosis cares about
    whether the player went where they were pointed."""
    for record in reversed(history or ()):
        if record.step.get("action") not in ("move", "move_and_jump"):
            continue
        try:
            before = record.state_before["position"]
            after = record.state_after["position"]
            return math.hypot(after[0] - before[0], after[2] - before[2])
        except (KeyError, TypeError, IndexError):
            return None
    return None


def block_label(block) -> str:
    """"oak_log" as "an oak log" — for sentences a person reads."""
    # " ".join(split()) rather than str.replace, which the boundary test
    # cannot tell apart from Path.replace — and it is right not to guess.
    name = " ".join(str(getattr(block, "name", "") or "something").split("_"))
    article = "an" if name[:1] in "aeiou" else "a"
    return f"{article} {name}"
