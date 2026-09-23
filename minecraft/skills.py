"""
minecraft/skills.py — the interface for "a thing JARVIS knows how to do", and
a small number of real ones.

SCOPE, ON PURPOSE
    This is the architecture plus four working skills, not a library. The
    eventual list — collect_wood, craft_item, find_iron, build_structure,
    return_to_base — needs state this version cannot read (inventory, health,
    entity positions) and planning this version deliberately does not do.
    Writing them now would mean writing them against a state source that
    cannot feed them, and rewriting every one when the mod bridge lands.

    What is here is the seam they will plug into, exercised by four skills
    that work end to end today.

WHAT A SKILL IS
    An object with `name`, `goal`, and:

        plan(state, step_index, history) -> Step | None

    It is called once per iteration with the state just observed and every
    record so far. It returns the next `Step`, or None to say it is finished.
    It never touches the controller, the ledger or the backend — it describes
    an action and the runner decides whether that action is allowed.

WHY SKILLS ARE NOT LLM PROMPTS
    A skill is ordinary Python with a bounded decision. That is what makes the
    task loop testable without a model, and what stops a prompt injection in a
    Minecraft sign from becoming a plan. When a model does get to drive this
    (a later phase), it will choose WHICH skill to run and with what
    parameters — not what the skill does step by step.

EVERY SKILL DECLARES WHAT IT CANNOT VERIFY
    `verifiable_with` names the state fields a skill needs for its own success
    check to mean anything. A skill whose fields are unreadable still runs —
    the actions are real — but every step comes back UNVERIFIABLE and the task
    summary says so rather than claiming an achievement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from minecraft import action_spec, navigation as nav, verification as verify_mod
from minecraft import stuck as stuck_mod
from minecraft import aiming as aiming_mod
from minecraft import mining as mining_mod
from minecraft.state import EXACT, UNKNOWN
from minecraft.task_runner import Step

CANNOT_SEE_TARGET = (
    "I cannot read what is under the crosshair, so I have no way to find a "
    "log or to tell whether one broke. That needs the F3 overlay open and "
    "OCR installed — see core/ocr.py. I stopped rather than swing at "
    "nothing."
)
"""Why a target-dependent skill gives up immediately.

Sweeping the view twenty times looking for something you cannot see is not
perseverance, it is a loop with a step limit for a brake. The skills that
depend on reading the target check for it once and say what is missing."""

# Every mining step must ask for at least this long. Breaking an oak log by
# hand takes about three seconds of CONTINUOUS holding, and Minecraft discards
# progress the instant the button comes up -- so a skill asking for one second
# mines forever and breaks nothing.
#
# Named here, and asserted in the tests, because that is exactly what happened:
# the action limit was raised to allow a real mining hold and the skills were
# left asking for the old one second, which looks identical to the input not
# working at all.
MIN_USEFUL_MINE_S = 3.0

MAX_SKIPPED_TARGETS = 4

MAX_PICKUP_WALKS = 4
"""How many times collect_logs walks over to a dropped log before saying it
could not get it. A broken log drops an item where it fell, often a couple of
blocks from where the player stood to mine it -- out of pickup range."""

DROP_RADIUS = 4.0
"""Item entities this close (horizontally) to a block this task broke are
treated as its drops and fetched. Further than that, an item on the ground is
somebody else's business."""

_ARRIVED = object()
"""What _walk_towards returns when the walk is over and nothing is left to
step: the caller decides what arriving means, instead of a filler step."""
"""Logs to give up on (occluded, unreachable by aim) before the whole task
does. Enough to work round a tree's leaves; not so many that a task with a
real aiming problem burns its budget trying every log in the forest."""

SWEEP_DEGREES = 18.0
"""How far a blind sweep turns between looks.

IN DEGREES, NOT PIXELS, AND THAT IS THE POINT
    These skills used to turn a fixed number of PIXELS, which is not a
    quantity anyone can reason about: 100 pixels is 12 degrees on one
    machine and 50 degrees on another, and on the second one a sweep jumps
    straight past the tree it is looking for and reports there isn't one.

    Eighteen degrees is a little under the crosshair's useful width, so a
    full turn takes twenty looks and nothing gets skipped over."""


def _sweep_pixels() -> int:
    """SWEEP_DEGREES in pixels, using whatever the mouse has measured."""
    return max(1, int(round(SWEEP_DEGREES * nav.pixels_per_degree())))


def _mine_params(seconds, block) -> dict:
    """Parameters for a mining step, pinned to the block that was seen.

    When the crosshair reading carried a coordinate, it goes along as
    `expect_at`: the controller reads the crosshair again at the instant
    before the button goes down and presses nothing unless it is still on
    that block. Without a coordinate there is nothing to pin, and the step is
    what it always was."""
    params = {"duration": seconds}
    try:
        where = [int(block.x), int(block.y), int(block.z)]
    except (AttributeError, TypeError, ValueError):
        return params
    params["expect_at"] = where
    return params


def _crosshair_at(state):
    """((x, y, z), name) of what the crosshair is on, or None when it cannot
    say where -- unreadable, or on nothing."""
    block = getattr(state, "target_block", None)
    if block is None or state.confidence_of("target_block") == UNKNOWN:
        return None
    try:
        return (int(block.x), int(block.y), int(block.z)), block.name
    except (TypeError, ValueError):
        return None


# Blocks that count as "a tree" for FindBlock's default search.
LOG_BLOCKS = frozenset({
    "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
    "dark_oak_log", "mangrove_log", "cherry_log", "pale_oak_log",
})


class Skill(Protocol):
    """What the task runner needs. Three members and one method."""

    name: str
    goal: str
    verifiable_with: tuple

    def plan(self, state, step_index: int, history: tuple): ...


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


@dataclass
class BreakBlock:
    """Attack whatever is under the crosshair until it is gone.

    THE POINT OF THIS SKILL
        It is the smallest task where "I did the action" and "I achieved the
        goal" genuinely come apart. One bounded attack does not break an oak
        log; several do. The skill therefore cannot report success from its own
        actions — it has to look at the block again between swings, and it
        stops when the block is gone, not when it has swung enough times.

        If the target cannot be read at all, every swing comes back
        UNVERIFIABLE and the task ends at the step limit without ever claiming
        the block broke."""

    expected: str = ""
    swings: int = 8
    swing_seconds: float = action_spec.DEFAULT_MINE_DURATION_S

    name = "break_block"
    verifiable_with = ("target_block",)

    @property
    def goal(self) -> str:
        return f"break the {self.expected}" if self.expected else "break the block"

    @property
    def done_reason(self) -> str:
        return self._reason

    _reason: str = ""

    def plan(self, state, step_index: int, history: tuple):
        block = state.target_block
        name = getattr(block, "name", None) if block else None

        # Gone — either air or nothing under the crosshair at all.
        if step_index > 0 and name in (None, "air", "cave_air", "void_air"):
            self._reason = "the block is gone"
            return None

        # Aimed at the wrong thing. Stop rather than mine whatever happens to
        # be there: breaking the wrong block is worse than not breaking one.
        if self.expected and name and name != self.expected:
            self._reason = (f"the crosshair is on {name}, not "
                            f"{self.expected} — I stopped rather than break "
                            f"the wrong block")
            return None

        if step_index >= self.swings:
            self._reason = "ran out of swings"
            return None

        return Step(
            action="mine",
            params=_mine_params(self.swing_seconds, block),
            expectation=verify_mod.block_broken(self.expected or None),
            note=f"swing {step_index + 1}",
        )


@dataclass
class PlaceBlock:
    """Select a slot, then place one block against what the crosshair is on.

    Two steps rather than one because they fail differently: the wrong slot
    means the wrong block, and no valid surface means nothing happens at all.
    Reporting "placed" for either would be a lie of a different kind.

    HONEST LIMIT ON VERIFICATION
        Placement is confirmed by the targeted block changing — the new block
        is now what the crosshair sees. That works when aiming at the face a
        block lands on and fails when it lands out of view, so the result can
        be UNVERIFIABLE even when the block really was placed. Confirming it
        properly needs the inventory count, which is not on the F3 overlay."""

    slot: int = 1
    _placed: bool = False

    name = "place_block"
    verifiable_with = ("target_block",)

    @property
    def goal(self) -> str:
        return f"place the block in slot {self.slot}"

    @property
    def done_reason(self) -> str:
        return ("placed, as far as the crosshair can tell" if self._placed
                else "nothing was placed")

    def plan(self, state, step_index: int, history: tuple):
        if step_index == 0:
            return Step(action="hotbar_select", params={"slot": self.slot},
                        expectation=verify_mod.holding_slot(self.slot),
                        note=f"select slot {self.slot}")
        if step_index == 1:
            return Step(action="place", params={},
                        expectation=verify_mod.target_changed(),
                        note="place one block")
        self._placed = any(
            r.verification.get("status") == verify_mod.SUCCESS
            for r in history)
        return None



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

    name = "navigate_to"
    verifiable_with = ("position", "surface")

    _destination: tuple | None = None
    _path: object = None
    _walked: int = 0
    _stalls: int = 0
    _hopped: bool = False
    _reroutes: int = 0
    _obstacle: object = None
    _diagnosis: object = None
    _history: tuple = ()
    _avoid: set = field(default_factory=set)
    _aiming_at: float | None = None
    _skip_to: int = 0
    _seen_at: tuple | None = None
    _left_to_go: float | None = None
    _blind: bool = False
    _stopped: str = ""
    _arrived: bool = False

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
            return (f"arrived at ({where[0]}, {where[1]})" if where
                    else "arrived")
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
        # forever with the stall count pinned one below the limit.
        if action not in ("move", "jump"):
            return
        if last.verification.get("status") == verify_mod.SUCCESS:
            self._stalls = 0
            self._hopped = False
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
                                       avoid=self._avoid)
            self._walked = 0
            if not self._path.found:
                self._stopped = self._path.reason
                return None

        waypoint = self._next_waypoint(local, state)
        if waypoint is None:
            return None

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

        if self._stalls >= STALLS_BEFORE_OBSTACLE_CHECK:
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

        if ahead == "climb":
            # The route steps up a full block. Walking into it first and
            # THEN deciding to jump costs a step and a failed move every
            # time; the map already says it is there. Hop straight up.
            self._walked = max(self._walked + 1, self._skip_to + 1)
            return Step(
                action="move_and_jump",
                params={"direction": "forward",
                        "duration": min(action_spec.MAX_HOP_DURATION_S,
                                        max(0.35, gap / WALK_BLOCKS_PER_S))},
                expectation=verify_mod.moved(min_distance=0.3),
                note=(f"hop up onto ({waypoint[0]:.0f}, {waypoint[2]:.0f})"),
            )

        self._walked = max(self._walked + 1, self._skip_to + 1)
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

        if diagnosis.recovery in (stuck_mod.REROUTE, stuck_mod.WAIT):
            avoid = getattr(diagnosis.obstacle, "column", None)
            if diagnosis.recovery == stuck_mod.WAIT:
                # Mobs move. Routing round the column it is standing in is
                # both a sensible detour and, often, simply a pause while it
                # wanders off.
                avoid = heading
            return self._reroute(diagnosis, avoid=avoid)

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

    def _reroute(self, diagnosis, avoid=None):
        """Throw the route away and plan another, within a bound.

        The stall counter resets, because a new route is a genuinely new
        attempt rather than the same failing action offered again — which is
        the distinction the old replan got wrong and spun on."""
        if self._reroutes >= MAX_REROUTES:
            self._stopped = (
                f"{diagnosis.describe()} — and {MAX_REROUTES} attempts to go "
                f"another way did not get past it.")
            return None
        self._reroutes += 1
        if avoid is not None:
            try:
                self._avoid.add((int(avoid[0]), int(avoid[-1])))
            except (TypeError, IndexError, ValueError):
                pass
        self._path = None
        self._walked = 0
        self._stalls = 0
        self._hopped = False
        return None

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
                                            self._path.waypoints, index)
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


@dataclass
class CollectLogs:
    """Find a tree, get to it, mine it, repeat. The first genuinely useful goal.

    HOW IT LOOKS FOR A TREE DEPENDS ON WHAT IT CAN SEE
        With the terrain scan, it finds the nearest log in the scanned volume,
        checks there is a walkable route, walks there, aims at it and mines.
        That is the difference between looking for a tree and turning on the
        spot hoping one comes past — which is what the previous version did,
        and what it looked like it was doing.

        Without the scan it falls back to the crosshair sweep. Same skill,
        much weaker, and the report says which one ran.

    WHAT IT COUNTS DEPENDS ON WHAT CAN BE SEEN TOO
        With the inventory readable, "collect 4 logs" means four logs actually
        in the inventory — the real claim. Without it, only the block
        disappearing is observable, and that is a weaker thing: an item that
        fell in lava or landed out of reach was broken and never picked up.
        So the result says which claim it is making.

    ONE HOLD PER LOG, NOT SEVERAL TAPS
        Minecraft discards breaking progress the moment the button comes up,
        so a mining step asks for one continuous hold of at least
        MIN_USEFUL_MINE_S. A skill that asks for one second mines forever and
        breaks nothing, which is indistinguishable from broken input."""

    count: int = 4
    sweep_steps: int = 10
    delta_px: int | None = None
    aim_tolerance_deg: float = 6.0
    max_aim_steps: int = 6

    name = "collect_logs"
    verifiable_with = ("surface", "target_block", "inventory")

    _broken: int = 0
    _collected: int = 0
    _starting_logs: int | None = None
    _can_count: bool = False
    _last_target: str = ""
    _blind: bool = False
    _used_the_map: bool = False
    _aim_target: tuple | None = None
    _aim_tries: int = 0
    _aim_gave_up: str = ""
    _aim_errors: list = field(default_factory=list)
    _skip: set = field(default_factory=set)
    _last_estimate: object = None
    _walker: object = None
    _walk_failed: str = ""
    _broken_at: list = field(default_factory=list)
    _pickup_walker: object = None
    _pickup_walks: int = 0
    _pickup_note: str = ""

    @property
    def _done(self) -> int:
        return self._collected if self._can_count else self._broken

    @property
    def failed(self) -> bool:
        """Blind, or short of the count. Either way this is not a success,
        and the runner reports it as incomplete rather than done."""
        return self._blind or self._done < self.count

    @property
    def goal(self) -> str:
        return f"break {self.count} log(s)"

    @property
    def done_reason(self) -> str:
        if self._blind:
            return CANNOT_SEE_TARGET
        how = (" (found by the terrain scan)" if self._used_the_map
               else " (found by sweeping the crosshair)")
        trouble = (self._pickup_note or self._walk_failed
                   or self._aim_gave_up)
        if trouble and self._done < self.count:
            return f"{self._progress_text()}{how}. {trouble}"
        return f"{self._progress_text()}{how}"

    def _progress_text(self) -> str:
        if self._can_count:
            return (f"broke {self._broken} and collected {self._collected} "
                    f"of {self.count} log(s), counted in the inventory")
        return (f"broke {self._broken} of {self.count} log(s) — I cannot see "
                f"the inventory, so I am reporting blocks that disappeared, "
                f"not items picked up")

    # ── planning ─────────────────────────────────────────────────────────

    def plan(self, state, step_index: int, history: tuple):
        local = nav.LocalMap.from_state(state)
        crosshair_readable = state.confidence_of("target_block") != UNKNOWN

        # Neither the scan nor the crosshair: every step would be a blind
        # swing and every verdict unverifiable. Say so on the first step
        # instead of spending the whole budget discovering it.
        if not local.usable and not crosshair_readable:
            self._blind = True
            return None

        self._count_progress(state, history)
        if self._done >= self.count:
            return None

        # Enough broken. Never break more than was asked for: counting only
        # the inventory, a log that broke and fell out of pickup range looked
        # like no progress at all, and "collect one log" broke three. What is
        # left is fetching the drops -- which needs the inventory to see.
        if self._broken >= self.count:
            if not self._can_count:
                return None
            return self._pick_up(state, local, history)

        if local.usable:
            self._used_the_map = True
            step = self._work_from_the_map(state, local, crosshair_readable,
                                           history)
            if step is not None:
                return step
            if self._walk_failed:
                return None
            # The map had nothing useful to add; fall through to the sweep,
            # which at least checks what is right in front of us.

        if not crosshair_readable:
            return None
        return self._work_from_the_crosshair(state)

    def account_for(self, state, history) -> None:
        """Count the final step before reporting.

        The runner calls this when it runs out of steps. Progress is tallied
        at the start of each plan(), so the last step's verdict would
        otherwise never be counted — and a run that broke its fourth log
        would say it broke three."""
        self._count_progress(state, history)

    def _count_progress(self, state, history) -> None:
        self._can_count = state.confidence_of("inventory") != UNKNOWN
        # Broken: from the record, always. A mine step's verdict says whether
        # THAT block went, whatever the inventory later says about the drop.
        broken = [r for r in history
                  if r.step.get("action") == "mine"
                  and r.verification.get("status") == verify_mod.SUCCESS]
        self._broken = len(broken)
        self._broken_at = []
        for record in broken:
            where = (record.step.get("params") or {}).get("expect_at")
            if where:
                self._broken_at.append(tuple(where))
        if self._can_count:
            # Collected: what is actually in the bag, against where it started.
            if self._starting_logs is None:
                self._starting_logs = _log_total(state)
            self._collected = _log_total(state) - self._starting_logs

    def _work_from_the_map(self, state, local, crosshair_readable,
                           history=()):
        """Nearest log: walk to it if it is far, aim and mine if it is close."""
        target = nav.nearest_block(state, "log", reachable_only=True,
                                   exclude=self._skip)
        if target is None:
            seen = nav.nearest_block(state, "log")
            if seen is not None and seen.position in self._skip \
                    and self._aim_gave_up:
                # The logs left are ones this task already gave up aiming at.
                # Saying "no walkable route" here would be false — they are
                # reachable; the crosshair would not land on them.
                self._walk_failed = self._aim_gave_up
                return None
            if seen is None:
                self._walk_failed = (
                    f"The scan covered {local.known_columns} columns within "
                    f"{local.radius or '?'} blocks and found no logs. There "
                    f"may be a forest past that; I cannot see it.")
            else:
                self._walk_failed = (
                    f"I can see {block_label(seen)} at {seen.position} but "
                    f"no walkable route to it.")
            return None

        # Reach is measured the way the game does -- from the eyes, to the
        # face -- and the same way the crosshair gate below measures it. Two
        # different "in reach" tests is what left the last real run standing
        # at the trunk, "arrived" by one and "too far" by the other, turning
        # on the spot until it gave up.
        if not aiming_mod.SHARED.within_reach(state.position, target.position):
            step = self._walk_towards(state, target, history)
            if step is not _ARRIVED:
                return step
            # Walked as close as the ground allows and it is still out of
            # reach -- high up the trunk, or across something. Skip it.
            self._skip.add(target.position)
            self._aim_target = None
            self._aim_gave_up = (f"The {target.name} at {target.position} is "
                                 f"out of reach from anywhere I can stand "
                                 f"next to it.")
            if len(self._skip) > MAX_SKIPPED_TARGETS:
                self._walk_failed = self._aim_gave_up
                return None
            return self._work_from_the_map(state, local, crosshair_readable,
                                           history)

        # Close enough to hit.
        self._walker = None

        # The crosshair is the authority, not the angle. If the bridge says
        # it is ALREADY on a log within reach, that is the log to mine —
        # whichever one it is. A person standing at a trunk hits the log in
        # front of them, not the one the planner happened to pick first.
        under = self._log_under_crosshair(state)
        if under is not None:
            return self._mine(state, under)

        # Aim at the chosen log, on the face turned towards us.
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   target.position)
        if target.position != self._aim_target:
            self._aim_target = target.position
            self._aim_tries = 0
            self._aim_errors = []
        self._aim_errors.append(error)

        converging = stuck_mod.diagnose_aim(self._aim_errors)
        # Keep correcting until the BRIDGE says the crosshair is on a log --
        # not until the computed angle looks small. Reaching this line means
        # it is not (a log under the crosshair was mined above), and "within
        # six degrees, crosshair on a leaf" is precisely the case that used to
        # stop correcting and swing. The bound on tries, the convergence check
        # and a zero-pixel correction are what end it -- and ending it never
        # mines; see below.
        if ((dx or dy) and self._aim_tries < self.max_aim_steps
                and converging is None):
            self._aim_tries += 1
            return Step(
                action="look", params={"dx": dx, "dy": dy},
                expectation=verify_mod.turned(min_degrees=1.0),
                note=(f"aim at {target.name} {target.position} "
                      f"({error:.0f}° off, try {self._aim_tries} of "
                      f"{self.max_aim_steps})"))

        # Pointed as well as we are going to get, and the crosshair is NOT on
        # a log. Something is in front of it — a leaf, the trunk's own edge,
        # another block — or aiming stopped converging. Either way, holding
        # attack now would break whatever IS under the crosshair, which is
        # exactly the thing not to do. Try a different log instead.
        seen = getattr(state, "target_block", None)
        seen_name = getattr(seen, "name", None) or "nothing"
        if converging is not None:
            self._aim_gave_up = (f"I could not settle the crosshair on "
                                 f"{target.name} at {target.position}: "
                                 f"{converging.describe()}.")
        else:
            self._aim_gave_up = (
                f"I aimed at the {target.name} at {target.position} but the "
                f"crosshair lands on {seen_name} — something is in the way.")
        self._skip.add(target.position)
        self._aim_target = None
        if len(self._skip) > MAX_SKIPPED_TARGETS:
            self._walk_failed = self._aim_gave_up
            return None
        return self._work_from_the_map(state, local, crosshair_readable,
                                       history)

    def _log_under_crosshair(self, state):
        """The log the bridge says the crosshair is on, if it is within reach.

        Returns a NearbyBlock-like object with a position and a name, or
        None. Reach matters: a crosshair on a log eight blocks away is a
        perfectly good aim that breaks nothing."""
        target = getattr(state, "target_block", None)
        if target is None or getattr(target, "name", None) not in LOG_BLOCKS:
            return None
        try:
            position = (int(target.x), int(target.y), int(target.z))
        except (TypeError, ValueError):
            return None
        if position in self._skip:
            return None
        if not aiming_mod.SHARED.within_reach(state.position, position):
            return None
        return nav_target(position, target.name)

    def _mine(self, state, target):
        """Hold attack on a block the crosshair is CONFIRMED to be on."""
        self._last_target = target.name
        estimate = mining_mod.estimate_break_duration(target.name, state=state)
        self._last_estimate = estimate
        # The step is judged on what it did: did THIS block go. Whether its
        # drop reached the inventory is a separate question, answered by the
        # count and the pickup that follows -- judging a mine by the bag
        # called a broken log a failure and sent the task to break another.
        check = verify_mod.broke_block_at(target.position, target.name)
        # `expect_at` makes the controller read the crosshair once more at
        # the instant before the button goes down, and press nothing unless
        # it is still on this exact coordinate.
        return Step(action="mine",
                    params={"duration": self._mine_seconds(estimate),
                            "expect_at": list(target.position)},
                    expectation=check,
                    note=(f"mine {target.name} at {target.position} "
                          f"({self._done}/{self.count}; "
                          f"{estimate.describe()})"))

    def _walk_towards(self, state, target, history=()):
        """Delegate the walking to the navigation skill.

        Delegated rather than reimplemented: a second movement loop would be a
        second set of stuck rules, a second idea of what counts as progress,
        and a second thing to get wrong. NavigateTo already refuses routes it
        cannot see and gives up honestly, and those are exactly the properties
        this needs."""
        if self._walker is None:
            column = nav.approach_column(state, target)
            if column is None:
                self._walk_failed = (
                    f"I can see {block_label(target)} at {target.position} "
                    f"but there is nowhere next to it I can stand.")
                return None
            self._walker = NavigateTo(destination=column)

        # The real history, not an empty tuple: NavigateTo reads the last
        # record to decide whether it is stuck, and handing it a blank slate
        # every call would switch its stall detection off entirely. It
        # ignores records for actions it did not ask for, so mining steps in
        # the middle of the trail cost it nothing.
        step = self._walker.plan(state, 0, history)
        if step is None:
            if self._walker.failed:
                self._walk_failed = self._walker.done_reason
                return None
            self._walker = None
            return _ARRIVED
        return step

    # ── fetching the drops ───────────────────────────────────────────────

    def _pick_up(self, state, local, history):
        """Walk onto the logs this task broke, so they reach the inventory.

        A broken log drops an item where it falls, and the player only picks
        up what it walks within about a block of. Mining from a few blocks
        away -- which reach allows -- leaves the drop lying there. The bridge
        reports item entities with positions, so the drop can be walked to.
        Bounded: MAX_PICKUP_WALKS walks, then an honest account of where the
        rest are."""
        drop = self._nearest_drop(state)
        if drop is None:
            self._pickup_walker = None
            self._pickup_note = (
                f"I broke {self._broken} log(s) but only {self._collected} "
                f"reached the inventory, and I cannot see the rest lying "
                f"anywhere near where they fell.")
            return None
        where = _drop_text(drop)
        if not local.usable:
            self._pickup_note = (f"The dropped log is on the ground at "
                                 f"{where}, but I cannot see the terrain to "
                                 f"walk to it.")
            return None

        walker = self._pickup_walker
        if walker is None:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} log(s) and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and I could not get to it after {self._pickup_walks} "
                    f"tries.")
                return None
            target = _pickup_column(state, local, drop)
            if target is None:
                self._pickup_note = (
                    f"I broke {self._broken} log(s) and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and there is nowhere I can stand close enough to pick "
                    f"it up.")
                return None
            column, within = target
            self._pickup_walks += 1
            walker = NavigateTo(destination=column, arrive_within=within)
            self._pickup_walker = walker

        step = walker.plan(state, 0, history)
        if step is not None:
            return step
        self._pickup_walker = None
        if walker.failed:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} log(s) and picked up "
                    f"{self._collected}; the rest is at {where} and I could "
                    f"not walk there: {walker.done_reason}")
                return None
            return self._pick_up(state, local, history)
        # Standing on it. Pickup happens on contact within a tick or two;
        # one observed pause lets the inventory catch up -- and is CHECKED,
        # against the bag, so it is never a filler step.
        return Step(action="look", params={"dx": 1, "dy": 0},
                    expectation=verify_mod.collected(self._last_target
                                                     or "oak_log"),
                    note=f"standing where the dropped log is ({where})")

    def _nearest_drop(self, state):
        """The closest item on the ground near a block this task broke."""
        items = [e for e in (getattr(state, "nearby_entities", None) or ())
                 if getattr(e, "category", None) == "item"
                 and getattr(e, "position", None) is not None]
        if not items or state.position is None:
            return None
        anchors = self._broken_at or [tuple(state.position)]

        def near_a_break(entity):
            ex, ez = entity.position[0], entity.position[2]
            return any(math.hypot(ex - (a[0] + 0.5), ez - (a[2] + 0.5))
                       <= DROP_RADIUS for a in anchors)

        ours = [e for e in items if near_a_break(e)]
        if not ours:
            return None
        return min(ours, key=lambda e: math.hypot(
            e.position[0] - state.position[0],
            e.position[2] - state.position[2]))

    def _work_from_the_crosshair(self, state):
        """The old behaviour, kept for when the mod is not running."""
        block = state.target_block
        name = getattr(block, "name", None) if block else None
        done = self._done

        if name in LOG_BLOCKS:
            self._last_target = name
            # Judged on the block, never the bag -- see _mine.
            check = verify_mod.block_broken(name)
            return Step(action="mine",
                        params=_mine_params(self._mine_seconds(), block),
                        expectation=check,
                        note=f"mine {name} ({done}/{self.count})")

        # Nothing wooden under the crosshair: sweep the view looking for some.
        return Step(action="look",
                    params={"dx": self.delta_px or _sweep_pixels(), "dy": 0},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=(f"sweeping for a log ({done}/{self.count}) — no "
                          f"terrain scan, so I can only check the crosshair"))

    @staticmethod
    def _mine_seconds(estimate=None) -> float:
        """How long to hold, from the break-time model when there is one.

        Never below MIN_USEFUL_MINE_S when the estimate is missing, because
        Minecraft discards breaking progress the moment the button comes up
        and a too-short hold breaks nothing however often it is repeated.
        With an estimate the hold can be shorter — dirt with a shovel is a
        fifth of a second — and the controller still lets go early when the
        bridge sees the block go."""
        if estimate is None or not estimate.breakable:
            return max(MIN_USEFUL_MINE_S, action_spec.DEFAULT_MINE_DURATION_S)
        return max(0.25, estimate.hold_seconds(
            action_spec.MAX_MINE_DURATION_S))

    def replan(self, state, reason, history):
        """Stuck: stop mining this log and look for another.

        Only offered when the last thing tried was mining. Offering it while
        already sweeping hands back the action that is failing, which resets
        the stuck detector without changing anything — the loop that made this
        task spin twenty times. The runner caps replans as a second defence,
        but a replan that is not an alternative should not be offered at all.

        Blindness gets no alternative: turning does not make an unreadable
        screen readable."""
        if reason != "no_progress":
            return None
        last = history[-1].step.get("action") if history else ""
        if last != "mine":
            return None
        return "sweep for a different log"


def _pickup_column(state, local, drop):
    """Where to stand to pick `drop` up: (column, arrive_within), or None.

    The drop's own column first. But a log broken at the bottom of a trunk
    drops INTO the trunk's column, under the logs still standing above it,
    where nobody fits -- and Minecraft collects from a box about 1.4 blocks
    either side of the player, so standing in the next column over does it.
    Hence the neighbours, nearest to the player first, each only if there is
    a walkable route to it. Stopping closer to a neighbour's centre keeps the
    item inside that box."""
    try:
        cx = int(math.floor(drop.position[0]))
        cz = int(math.floor(drop.position[2]))
        px, pz = state.position[0], state.position[2]
    except (TypeError, IndexError, ValueError, AttributeError):
        return None
    here = (int(math.floor(px)), int(math.floor(pz)))
    neighbours = sorted(((cx + 1, cz), (cx - 1, cz), (cx, cz + 1), (cx, cz - 1)),
                        key=lambda c: math.hypot(c[0] + 0.5 - px,
                                                 c[1] + 0.5 - pz))
    for column, within in [((cx, cz), 0.6)] + [(c, 0.35) for c in neighbours]:
        if column == here:
            return column, within
        if not local.standable(*column):
            continue
        if nav.find_path(state, column).found:
            return column, within
    return None


def _drop_text(entity) -> str:
    try:
        x, y, z = entity.position
        return f"({x:.0f}, {y:.0f}, {z:.0f})"
    except (TypeError, ValueError):
        return "somewhere nearby"


def _log_total(state) -> int:
    """Every kind of log in the inventory, added up."""
    total = 0
    for stack in (state.inventory or ()):
        if getattr(stack, "name", None) in LOG_BLOCKS:
            total += int(getattr(stack, "count", 0) or 0)
    return total


# ── Registry ─────────────────────────────────────────────────────────────────

@dataclass
class AimAtBlock:
    """Put the crosshair on one exact block -- and, as `mine_block`, break it.

    THE CROSSHAIR IS THE PROOF, NOT THE ANGLE
        The angle arithmetic says where the camera SHOULD point; only the
        game can say what it IS pointing at, and the bridge reports exactly
        that: a block id at a coordinate. So this aims, looks, and corrects
        again until the bridge reports the crosshair on (x, y, z). A small
        angular error is not success -- the crosshair can be half a degree
        off and on the leaf in front of the log.

    NEVER MINES ON A GUESS
        Mining happens only once the crosshair is confirmed on the target,
        and the mine step carries the coordinate so the controller checks it
        once more at the instant it presses. If aiming does not converge,
        runs out of corrections, or the crosshair cannot be read at all, the
        task ends saying so -- having mined nothing.

    BOUNDED AND PLAIN
        It does not walk: a target out of reach is refused with the reason,
        and navigate_to is what gets closer. Success for `mine_block` is the
        block at that coordinate being gone (broke_block_at), not a button
        having been held."""

    x: int = 0
    y: int = 0
    z: int = 0
    mine: bool = False
    expected: str = ""
    max_aim_steps: int = 8
    swings: int = 2

    name = "aim_at_block"
    verifiable_with = ("target_block", "rotation")

    _aim_tries: int = 0
    _aim_errors: list = field(default_factory=list)
    _swings_done: int = 0
    _succeeded: bool = False
    _reason: str = ""

    @property
    def target(self) -> tuple:
        return (int(self.x), int(self.y), int(self.z))

    @property
    def goal(self) -> str:
        verb = "mine" if self.mine else "aim at"
        what = self.expected or "the block"
        return f"{verb} {what} at {self.target}"

    @property
    def failed(self) -> bool:
        return not self._succeeded

    @property
    def done_reason(self) -> str:
        return self._reason or "stopped before the crosshair was confirmed"

    def _stop(self, reason: str, succeeded: bool = False):
        self._reason = reason
        self._succeeded = succeeded
        return None

    def plan(self, state, step_index: int, history: tuple):
        target = self.target

        # The verdict of the mine step just taken, if that is what it was.
        last = history[-1] if history else None
        if last is not None and last.step.get("action") == "mine":
            verdict = last.verification.get("status")
            if verdict == verify_mod.SUCCESS:
                return self._stop(
                    f"broke the block at {target}: "
                    f"{last.verification.get('reason', '')}".rstrip(": "),
                    succeeded=True)
            self._swings_done += 1
            if last.action_result.get("stopped_reason") == \
                    "target_not_confirmed":
                # The controller refused at the last instant: the crosshair
                # had moved off. Aim again rather than count it as a swing.
                self._swings_done -= 1
            elif self._swings_done >= self.swings:
                return self._stop(
                    f"held attack on {target} {self._swings_done} time(s) and "
                    f"could not confirm it broke: "
                    f"{last.verification.get('reason', 'no evidence')}")

        seen = _crosshair_at(state)
        if seen is None and state.confidence_of("target_block") == UNKNOWN:
            return self._stop(
                "I cannot read what the crosshair is on, so I cannot confirm "
                "the aim — that needs the bridge mod. I did not aim blind and "
                "I mined nothing.")
        if self.mine and state.confidence_of("target_block") != EXACT:
            return self._stop(
                "The crosshair reading is not from the bridge mod, and I only "
                "mine on the game's own confirmation of the block. I mined "
                "nothing.")

        if seen is not None and seen[0] == target:
            name = seen[1]
            if self.expected and name != self.expected:
                return self._stop(
                    f"The crosshair is on {target}, but the block there is "
                    f"{name}, not {self.expected}. I did not mine it.")
            if not self.mine:
                return self._stop(f"the crosshair is on the {name} at "
                                  f"{target}, confirmed by the game.",
                                  succeeded=True)
            if not aiming_mod.SHARED.within_reach(state.position, target):
                return self._stop(
                    f"The {name} at {target} is under the crosshair but out "
                    f"of reach. Walk closer first (navigate_to).")
            estimate = mining_mod.estimate_break_duration(name, state=state)
            if not estimate.breakable:
                return self._stop(f"The {name} at {target} cannot be broken "
                                  f"({estimate.describe()}). I did not try.")
            return Step(
                action="mine",
                params={"duration": CollectLogs._mine_seconds(estimate),
                        "expect_at": list(target)},
                expectation=verify_mod.broke_block_at(target, name),
                note=(f"mine {name} at {target}, crosshair confirmed "
                      f"({estimate.describe()})"))

        # Not on it yet: correct, observe, correct again.
        if state.position is None or state.rotation is None:
            return self._stop("I cannot read where I am or which way I am "
                              "facing, so I cannot aim. I mined nothing.")
        if not aiming_mod.SHARED.within_reach(state.position, target):
            return self._stop(
                f"{target} is out of reach from here. Walk closer first "
                f"(navigate_to), then ask again. I mined nothing.")

        dx, dy, error = nav.aim_at(state.position, state.rotation, target)
        self._aim_errors.append(error)
        stuck = stuck_mod.diagnose_aim(self._aim_errors)
        on = (f"{seen[1]} at {seen[0]}" if seen is not None
              else "nothing within reach")
        if stuck is not None or self._aim_tries >= self.max_aim_steps \
                or (dx == 0 and dy == 0):
            why = (stuck.describe() if stuck is not None else
                   f"{self._aim_tries} corrections" if self._aim_tries
                   else "no correction left to make")
            return self._stop(
                f"I could not get the crosshair onto {target} ({why}); it is "
                f"on {on}, {error:.1f}° from the aim point. Something may be "
                f"in the way. I mined nothing.")

        self._aim_tries += 1
        return Step(
            action="look", params={"dx": dx, "dy": dy},
            expectation=verify_mod.turned(min_degrees=0.5),
            note=(f"aim at {target} ({error:.1f}° off, crosshair on {on}; "
                  f"correction {self._aim_tries} of {self.max_aim_steps})"))


def _mine_block(**kwargs):
    """`mine_block`: AimAtBlock that breaks the block once it is confirmed."""
    kwargs["mine"] = True
    skill = AimAtBlock(**kwargs)
    skill.name = "mine_block"
    return skill


BUILTIN_SKILLS = {
    "walk_forward": WalkForward,
    "survey": Survey,
    "find_block": FindBlock,
    "break_block": BreakBlock,
    "place_block": PlaceBlock,
    "collect_logs": CollectLogs,
    "navigate_to": NavigateTo,
    "aim_at_block": AimAtBlock,
    "mine_block": _mine_block,
}


def create(name: str, **kwargs):
    """Build a skill by name.

    A fixed table, not a lookup by import path or class name: the point of the
    planner boundary is that the set of runnable things is decided in source,
    and a registry that could be extended at runtime would give that back."""
    factory = BUILTIN_SKILLS.get(str(name or "").strip().lower())
    if factory is None:
        raise KeyError(
            f"'{name}' is not a skill I have. Mine are: "
            f"{', '.join(sorted(BUILTIN_SKILLS))}."
        )
    return factory(**kwargs)


def available() -> tuple:
    return tuple(sorted(BUILTIN_SKILLS))


# Named here rather than in prose so the tool description, the status report
# and the manual check all quote the same list.
NOT_YET_POSSIBLE = {
    "craft_item": "the bridge mod reports the inventory but not the crafting "
                  "grid, and clicking recipe slots needs absolute mouse "
                  "positioning that is not built.",
    "find_iron": "the scan now reports ores it can see, so iron already "
                 "exposed in a cave wall within the scan radius is findable. "
                 "What is missing is getting to iron that is NOT exposed, "
                 "which means digging a shaft, lighting it, and not falling "
                 "into lava — none of which is built.",
    "eat_food": "hunger and the inventory are readable now; what is missing "
                "is choosing the right slot, which needs the hotbar mapped to "
                "what is in it.",
    "build_structure": "needs a plan and a placement order, not just the "
                       "ability to place one block.",
    "return_to_base": "navigation exists now, but only within the scan "
                      "radius. Walking back to a base 300 blocks away needs "
                      "stored waypoints and route-finding across terrain not "
                      "currently visible, which is a different problem from "
                      "crossing a clearing.",
    "long_distance_travel": "the planner refuses any destination outside the "
                            "scan, on purpose. Travelling further means "
                            "planning to the edge of what is visible, "
                            "re-scanning, and planning again — that loop is "
                            "not built.",
    "dig_or_bridge_a_route": "the pathfinder only walks. It will not mine "
                             "through an obstacle or place blocks over a "
                             "gap, so a destination that needs either comes "
                             "back as unreachable rather than as a plan.",
}

NOW_POSSIBLE_WITH_THE_BRIDGE = (
    "inventory contents", "health", "hunger", "held item", "nearby entities",
    "exact position", "world time", "weather",
    "the terrain around the player", "where nearby blocks worth reaching are",
    "whether there is a walkable route to one",
)
"""What stopped being impossible when the mod arrived.

Kept as a list rather than folded into prose because these were each cited, in
this file and in the tool description, as the reason something could not be
done. A claim that stops being true should be retracted in the same place it
was made."""


__all__ = [
    "MIN_USEFUL_MINE_S",
    "Skill", "WalkForward", "Survey", "FindBlock", "BreakBlock",
    "PlaceBlock", "CollectLogs", "AimAtBlock",
    "BUILTIN_SKILLS", "create", "available", "LOG_BLOCKS", "NOT_YET_POSSIBLE",
]
