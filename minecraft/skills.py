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

from dataclasses import dataclass
from typing import Protocol

from minecraft import action_spec, navigation as nav, verification as verify_mod
from minecraft.state import UNKNOWN
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
    delta_px: int = 150

    name = "survey"
    goal = "look around and report what is there"
    verifiable_with = ("rotation",)

    def plan(self, state, step_index: int, history: tuple):
        if step_index >= self.steps:
            return None
        return Step(
            action="look",
            params={"dx": self.delta_px, "dy": 0},
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
    delta_px: int = 120
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
            params={"dx": self.delta_px, "dy": 0},
            expectation=verify_mod.turned(min_degrees=2.0),
            note=f"sweep {step_index + 1} of {self.steps} looking for a target",
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
            params={"duration": self.swing_seconds},
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
STALLS_BEFORE_JUMP = 2
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
    _look_sign: int = 1
    _aiming_at: float | None = None
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
        """If a turn made the aim worse, turn the other way from now on.

        WHY THIS EXISTS
            How many pixels of mouse movement make a degree depends on the
            player's sensitivity setting, and which SIGN turns which way is a
            convention two layers apart — Minecraft's yaw and the operating
            system's mouse deltas — that nothing in the bridge confirms.

            Get the sign wrong and every correction doubles the error: the
            agent spins, forever, looking exactly like a pathfinding bug. So
            rather than trust the convention, the skill measures one turn and
            believes the measurement."""
        if self._aiming_at is None:
            return
        try:
            was = record.state_before["rotation"][0]
            now = record.state_after["rotation"][0]
        except (KeyError, TypeError, IndexError):
            return
        if was is None or now is None:
            return
        before = abs(nav.yaw_difference(was, self._aiming_at))
        after = abs(nav.yaw_difference(now, self._aiming_at))
        if after > before + 2.0:
            self._look_sign = -self._look_sign

    def _follow_path(self, state, local):
        if self._needs_new_path(state, local):
            self._path = nav.find_path(state, self._destination)
            self._walked = 0
            if not self._path.found:
                self._stopped = self._path.reason
                return None

        waypoint = self._next_waypoint()
        if waypoint is None:
            return None

        here = state.position
        desired = nav.yaw_to(here, waypoint)
        current = (state.rotation or (None, None))[0]
        if current is None:
            # Rotation unreadable: walking on a heading you cannot measure is
            # guessing. Say so rather than press forward hopefully.
            self._stopped = ("I cannot read which way I am facing, so I "
                             "cannot steer.")
            return None

        off_by = nav.yaw_difference(current, desired)
        if abs(off_by) > YAW_TOLERANCE_DEG:
            dx = self._look_sign * nav.look_delta_for(current, desired)
            self._aiming_at = desired
            return Step(
                action="look",
                params={"dx": dx, "dy": 0},
                expectation=verify_mod.turned(min_degrees=2.0),
                note=(f"turn {off_by:+.0f}° towards "
                      f"({waypoint[0]:.0f}, {waypoint[2]:.0f})"),
            )

        if self._stalls >= STALLS_BEFORE_JUMP and not self._hopped:
            # Steps with no ground gained, pointed the right way: the usual
            # cause is a one-block lip. One hop is cheap to try and tells us
            # something either way -- but only one, because a skill that hops
            # every time it is stuck never reaches the giving-up branch.
            self._hopped = True
            return Step(
                action="jump", params={},
                expectation=verify_mod.moved(min_distance=0.2),
                note="hop — the last moves got nowhere",
            )

        self._aiming_at = None
        self._walked += 1
        gap = self._gap_to(here, waypoint)
        return Step(
            action="move",
            params={"direction": "forward",
                    "duration": self._step_seconds(gap)},
            # Judged against the WAYPOINT, not the destination. Walking round
            # a wall means walking away from where you are going, and a check
            # that only ever asks "are you nearer the goal" calls every
            # correct detour a failure -- then the stall counter gives up
            # four steps into a route that was working.
            expectation=verify_mod.closer_to(
                (waypoint[0], waypoint[2]),
                min_gain=max(0.15, min(0.4, gap * 0.5))),
            note=(f"walk to ({waypoint[0]:.0f}, {waypoint[2]:.0f}) — "
                  f"{self._remaining(state):.0f} blocks to go"),
        )

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

    def _next_waypoint(self):
        """The first waypoint that is not the one we are standing on."""
        if self._path is None or not self._path.waypoints:
            return None
        index = min(self._walked, len(self._path.waypoints) - 1)
        return self._path.waypoints[index]

    @staticmethod
    def _gap_to(here, waypoint) -> float:
        try:
            return ((here[0] - waypoint[0]) ** 2
                    + (here[2] - waypoint[2]) ** 2) ** 0.5
        except (TypeError, IndexError):
            return 1.0

    def _step_seconds(self, gap: float) -> float:
        """Long enough to reach the waypoint, short enough not to sail past.

        Overshooting is the characteristic navigation bug: a two second hold
        crosses eight blocks, and a route made of one-block steps becomes a
        zigzag across the whole clearing."""
        wanted = gap / WALK_BLOCKS_PER_S
        return max(action_spec.MIN_MOVE_DURATION_S,
                   min(wanted, action_spec.MAX_MOVE_DURATION_S))

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
    delta_px: int = 100
    aim_tolerance_deg: float = 6.0
    reach: float = 4.0

    name = "collect_logs"
    verifiable_with = ("surface", "target_block", "inventory")

    _broken: int = 0
    _collected: int = 0
    _starting_logs: int | None = None
    _can_count: bool = False
    _last_target: str = ""
    _blind: bool = False
    _used_the_map: bool = False
    _walker: object = None
    _walk_failed: str = ""

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
        if self._walk_failed and self._done < self.count:
            return f"{self._progress_text()}{how}. {self._walk_failed}"
        return f"{self._progress_text()}{how}"

    def _progress_text(self) -> str:
        if self._can_count:
            return (f"collected {self._collected} of {self.count} log(s), "
                    f"counted in the inventory")
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

    def _count_progress(self, state, history) -> None:
        self._can_count = state.confidence_of("inventory") != UNKNOWN
        if self._can_count:
            # The real measure, when it is available: what is in the bag.
            if self._starting_logs is None:
                self._starting_logs = _log_total(state)
            self._collected = _log_total(state) - self._starting_logs
        else:
            # Count from the record: a swing whose verification says the block
            # went is the only evidence there is without an inventory.
            self._broken = sum(
                1 for r in history
                if r.step.get("action") == "mine"
                and r.verification.get("status") == verify_mod.SUCCESS)

    def _work_from_the_map(self, state, local, crosshair_readable,
                           history=()):
        """Nearest log: walk to it if it is far, aim and mine if it is close."""
        target = nav.nearest_block(state, "log", reachable_only=True)
        if target is None:
            seen = nav.nearest_block(state, "log")
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

        distance = target.distance_to(state.position)
        if distance > self.reach:
            return self._walk_towards(state, target, history)

        # Close enough to hit. Aim at it, then hold.
        self._walker = None
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   target.position)
        if error > self.aim_tolerance_deg and (dx or dy):
            return Step(action="look", params={"dx": dx, "dy": dy},
                        expectation=verify_mod.turned(min_degrees=1.0),
                        note=f"aim at {target.name} {target.position}")

        self._last_target = target.name
        # Best evidence first: the inventory if it is readable, then the block
        # vanishing from the scan, then the crosshair. Each is a weaker claim
        # than the one before it and the reason line says which was used.
        if self._can_count:
            check = verify_mod.collected(target.name)
        else:
            check = verify_mod.block_gone(target.position, target.name)
        return Step(action="mine",
                    params={"duration": self._mine_seconds()},
                    expectation=check,
                    note=f"mine {target.name} ({self._done}/{self.count})")

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
            self._walker = None        # arrived; mine on the next iteration
            return self._noop_look(state)
        return step

    def _noop_look(self, state):
        """A step that costs one iteration and changes nothing important.

        Returning None here would end the task at the moment it arrived at the
        tree. A small turn gives the loop one more pass to observe and then
        mine."""
        return Step(action="look", params={"dx": 1, "dy": 0},
                    expectation=None,
                    note="arrived at the tree")

    def _work_from_the_crosshair(self, state):
        """The old behaviour, kept for when the mod is not running."""
        block = state.target_block
        name = getattr(block, "name", None) if block else None
        done = self._done

        if name in LOG_BLOCKS:
            self._last_target = name
            check = (verify_mod.collected(name) if self._can_count
                     else verify_mod.block_broken(name))
            return Step(action="mine",
                        params={"duration": self._mine_seconds()},
                        expectation=check,
                        note=f"mine {name} ({done}/{self.count})")

        # Nothing wooden under the crosshair: sweep the view looking for some.
        return Step(action="look", params={"dx": self.delta_px, "dy": 0},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=f"looking for a log ({done}/{self.count})")

    @staticmethod
    def _mine_seconds() -> float:
        return max(MIN_USEFUL_MINE_S, action_spec.DEFAULT_MINE_DURATION_S)

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


def _log_total(state) -> int:
    """Every kind of log in the inventory, added up."""
    total = 0
    for stack in (state.inventory or ()):
        if getattr(stack, "name", None) in LOG_BLOCKS:
            total += int(getattr(stack, "count", 0) or 0)
    return total


# ── Registry ─────────────────────────────────────────────────────────────────

BUILTIN_SKILLS = {
    "walk_forward": WalkForward,
    "survey": Survey,
    "find_block": FindBlock,
    "break_block": BreakBlock,
    "place_block": PlaceBlock,
    "collect_logs": CollectLogs,
    "navigate_to": NavigateTo,
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
    "PlaceBlock", "CollectLogs",
    "BUILTIN_SKILLS", "create", "available", "LOG_BLOCKS", "NOT_YET_POSSIBLE",
]
