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
from minecraft import gui as gui_mod
from minecraft import building as building_mod
from minecraft import recipes as recipes_mod
from minecraft.state import EXACT, UNKNOWN, NearbyBlock
from minecraft.task_runner import MAX_TASK_STEPS, Step

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

MAX_LEAVES_PER_LOG = 3
"""Leaf blocks collect_logs will break to get at one log, before trying a
different log instead.

From a real run: the aim settled on four logs in turn, 0 degrees off each
time, and never once landed -- the canopy was between the eye and the trunk.
Leaves are what is usually in front of a log, and they break in a fraction
of a second by hand. Only a leaf NEARER than the log is broken, only at the
exact coordinate the crosshair reports, and never counted as a log."""

MAX_TREE_LOGS = 24
"""The most logs fell_tree will break. A tree has four to a dozen that can
be reached from the ground; this only stops a grove of touching trunks
turning one request into an afternoon."""

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


def _centre(position) -> tuple:
    return (position[0] + 0.5, position[1] + 0.5, position[2] + 0.5)


def _crosshair_text(state) -> str:
    """What the crosshair is on, for a person reading the step trail.

    An aim that reaches 0 degrees off and still does not land is only
    explicable by what it landed on INSTEAD -- a leaf, the next log, the
    ground -- and the trail used not to say."""
    seen = _crosshair_at(state)
    if seen is None:
        name = getattr(getattr(state, "target_block", None), "name", None)
        if name == "air" and state.confidence_of("target_block") != UNKNOWN:
            return "crosshair on nothing within reach"
        return "crosshair unreadable"
    position, name = seen
    return f"crosshair on {name} {position}"


# Blocks that count as "a tree" for FindBlock's default search. One
# definition, in navigation.py: there used to be a copy here too.
LOG_BLOCKS = nav.LOG_BLOCKS


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


class _HoldsTheRightTool:
    """Hold the best hotbar tool before each swing, and put the slot back
    once the skill has finished -- the way EatFood._restore does.

    Nothing chose a tool before: a log was chopped with whatever was held,
    and stone was held at for ten seconds by hand, breaking it eventually
    and dropping nothing. A skill using this implements `_plan` instead of
    `plan`, and asks `_tool_for` before each mine step.

    A tool only in the main inventory is fetched into the hotbar first
    (HotbarFetch), once per task: when nothing in the hotbar harvests the
    block, or when it saves FETCH_WORTH_S a block. While the inventory is
    open the fetch keeps its own, stricter, danger watch and the runner's
    stands down -- see `watch_hostiles`."""

    MAX_TOOL_SELECTS = 2
    _tool_slot_before = None      # the slot to go back to, once we switched
    _tool_restoring = False
    _plan_done = False
    _tool_tries = None            # {(block, slot): selects asked for}
    _fetch = None                 # the trip into the inventory, once made
    _fetch_note = ""              # why it was not made, or failed

    @property
    def watch_hostiles(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def watch_health(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    def _with_fetch(self, text: str) -> str:
        """`text`, after what the trip into the inventory did, if it moved
        anything -- the user's hotbar changed and should hear so."""
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {text}"
        return text

    def plan(self, state, step_index: int, history: tuple):
        if self._plan_done:
            return None
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step
            if self._fetch.failed:
                self._fetch_note = (
                    f"I tried to move the "
                    f"{' '.join(self._fetch.item.split('_'))} into the hotbar "
                    f"and stopped: {self._fetch.done_reason}.")
        step = self._plan(state, step_index, history)
        if step is None:
            return self._finish(state)
        return step

    def _finish(self, state):
        """The skill is done: put the slot back once, then nothing more.
        Its own plan is not asked again -- it might start over."""
        self._plan_done = True
        if self._tool_restoring or self._tool_slot_before is None \
                or getattr(state, "selected_slot", None) \
                == self._tool_slot_before:
            return None
        self._tool_restoring = True
        slot = self._tool_slot_before + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    def _tool_for(self, state, block):
        """None when ready to swing; a Step to take up the better tool first
        (select it, or begin fetching it from the main inventory); or a
        sentence: why not to swing at all."""
        choice = mining_mod.best_hotbar_tool(state, block)
        trip = self._fetch_for(state, choice)
        if trip is not None:
            return trip
        if choice.refusal:
            return " ".join(t for t in (choice.refusal, self._fetch_note) if t)
        if choice.slot is None:
            return None
        item = " ".join((choice.item or "tool").split("_"))
        tries = self._tool_tries if self._tool_tries is not None else {}
        self._tool_tries = tries
        key = (choice.block, choice.slot)
        if tries.get(key, 0) >= self.MAX_TOOL_SELECTS:
            current = getattr(state, "selected_slot", None)
            shown = "?" if current is None else current + 1
            return (f"I selected hotbar slot {choice.slot + 1} for the {item} "
                    f"{tries[key]} times and the game still shows slot "
                    f"{shown}; I stopped rather than mine {choice.block} "
                    f"with the wrong tool.")
        tries[key] = tries.get(key, 0) + 1
        if self._tool_slot_before is None \
                and getattr(state, "selected_slot", None) is not None:
            self._tool_slot_before = state.selected_slot
        slot = choice.slot + 1
        note = f"hold the {item} for the {choice.block} (hotbar slot {slot})"
        if choice.better_in_inventory:
            note += (f"; a {' '.join(choice.better_in_inventory.split('_'))} "
                     f"in the main inventory would be faster")
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot), note=note)

    def _fetch_for(self, state, choice):
        """The first step of a trip to fetch the better tool, or None: none
        is better, the hotbar's is nearly as good, a trip was already made,
        or it cannot be (then `_fetch_note` says why)."""
        stored = choice.better_in_inventory
        if not stored or self._fetch is not None:
            return None
        if choice.harvests:
            now = getattr(choice.estimate, "seconds", None)
            if now is None or choice.stored_seconds is None \
                    or now - choice.stored_seconds < FETCH_WORTH_S:
                return None
        self._fetch = HotbarFetch(item=stored,
                                  hotbar=hotbar_slot_to_fill(state))
        step = self._fetch.plan(state)
        if step is None:
            self._fetch_note = (f"I could not fetch it: "
                                f"{self._fetch.done_reason}.")
        return step


@dataclass
class BreakBlock(_HoldsTheRightTool):
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
        return self._with_fetch(self._reason)

    @property
    def failed(self) -> bool:
        """Anything short of a swing verified to have broken the block.

        Without this the runner reported every ending as "done" -- including
        "there was nothing there" and "I stopped rather than break the wrong
        block" -- and the assistant told the user the block was gone."""
        return not self._broken

    _reason: str = ""
    _broken: bool = False

    def _plan(self, state, step_index: int, history: tuple):
        block = state.target_block
        name = getattr(block, "name", None) if block else None

        # Gone: only on the word of a swing's own verification. The crosshair
        # reading air afterwards is not enough -- it read air in a real run
        # where there had never been a block, and "the block is gone" went
        # back to the user.
        mines = [r for r in history if r.step.get("action") == "mine"]
        if any(r.verification.get("status") == verify_mod.SUCCESS
               for r in mines):
            self._broken = True
            self._reason = "the block is gone"
            return None

        # Nothing there. Holding attack at air breaks nothing and would be
        # reported as trying; say so instead.
        if name in _AIR:
            if mines and mines[-1].verification.get("status") \
                    == verify_mod.UNVERIFIABLE:
                self._reason = ("the crosshair now sees nothing, but I could "
                                "not see what was there before the swing, so "
                                "I cannot say whether it broke")
            else:
                self._reason = ("there is no block under the crosshair within "
                                "reach, so there is nothing to break. For a "
                                "tree, collect_logs finds, walks to and aims "
                                "at a log by itself")
            return None

        # Aimed at the wrong thing. Stop rather than mine whatever happens to
        # be there: breaking the wrong block is worse than not breaking one.
        if self.expected and name and name != self.expected:
            self._reason = (f"the crosshair is on {name}, not "
                            f"{self.expected} — I stopped rather than break "
                            f"the wrong block")
            return None

        # Swings, not steps: taking up a tool is a step and not a swing.
        if len(mines) >= self.swings:
            self._reason = "ran out of swings"
            return None

        if name:
            ready = self._tool_for(state, name)
            if isinstance(ready, str):
                self._reason = ready
                return None
            if ready is not None:
                return ready

        return Step(
            action="mine",
            params=_mine_params(self.swing_seconds, block),
            expectation=verify_mod.block_broken(self.expected or None),
            note=f"swing {step_index + 1}",
        )


_AIR = frozenset({"air", "cave_air", "void_air"})


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


@dataclass
class _Gatherer(_HoldsTheRightTool):
    """Find a block, get to it, mine it, pick up what it drops, repeat.

    The machinery collect_logs was built with -- a route to somewhere to
    stand, an aim the bridge confirms block by block, a hold sized by the
    break-time model with the right hotbar tool, and a walk over to the
    drops -- for any set of blocks. Parameterised by what a subclass
    supplies: which blocks to break (`_wanted_names`), which items are
    their drops and so count (`_drop_names`), what to call one (`_noun`),
    how to pick the next one (`_next_target`), and `count`. collect_logs,
    fell_tree and collect_blocks are thin subclasses."""

    count: int = 4
    sweep_steps: int = 10
    delta_px: int | None = None
    aim_tolerance_deg: float = 6.0
    max_aim_steps: int = 6
    whole_tree: bool = False
    """fell_tree: every log of one tree it can reach, then stop -- rather
    than a count of logs from wherever they are nearest."""

    name = "gather"
    verifiable_with = ("surface", "target_block", "inventory")

    # ── what a subclass supplies ─────────────────────────────────────────

    def _wanted_names(self) -> frozenset:
        """The blocks to break."""
        return LOG_BLOCKS

    def _drop_names(self) -> frozenset:
        """The items those blocks drop: what is counted and fetched."""
        return LOG_BLOCKS

    def _noun(self, plural: bool = False) -> str:
        return "logs" if plural else "log"

    def _broken_noun(self) -> str:
        """What the broken count counts, in the pickup report."""
        return "block(s)"

    def _clears_leaves(self) -> bool:
        """Break leaves in the way of the aim? Only round a tree."""
        return False

    def _next_target(self, state):
        """The next block to go for: the nearest wanted one there is a route
        to somewhere to stand by, not given up on."""
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()), reachable_only=True,
            exclude=self._skip)

    def _nearest_seen(self, state):
        """The nearest wanted block at all -- for saying why none will do."""
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()))

    def _none_found_note(self) -> str:
        return ""

    def _unreachable_text(self, seen) -> str:
        """Why the nearest one seen will not do."""
        return (f"I can see {block_label(seen)} at {seen.position} but no "
                f"walkable route to it.")

    def _may_break(self, state, position, name) -> bool:
        """May this block be broken at all? Asked of the block under the
        crosshair too, not only of the one chosen."""
        return True

    def _aim_face(self, state, target):
        """Which face to aim at, or None for the one turned towards us."""
        return None

    def _work_without_the_map(self, state):
        """No terrain scan: nothing to plan over."""
        self._walk_failed = (f"Collecting {self._noun(True)} needs the "
                             f"terrain scan from the bridge mod, to see "
                             f"where they are.")
        return None

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
    _in_the_way: str = ""
    _cleared: set = field(default_factory=set)
    _leaves_for: dict = field(default_factory=dict)
    _aim_errors: list = field(default_factory=list)
    _skip: set = field(default_factory=set)
    _last_estimate: object = None
    _walker: object = None
    _walker_for: tuple | None = None      # the log the walker is walking to
    _fetching_at: tuple | None = None     # where the drop being fetched was
    _walk_failed: str = ""
    _broken_at: list = field(default_factory=list)
    _pickup_walker: object = None
    _pickup_walks: int = 0
    _pickup_note: str = ""
    _fetch_baseline: int | None = None
    _fetching: str = ""
    # The tree being worked on: positions of its logs, and for every log
    # this task has seen, which tree it belongs to -- (kind, trunk column).
    _tree: set = field(default_factory=set)
    _tree_of: dict = field(default_factory=dict)
    _tree_done: bool = False
    _tree_left: tuple = ()

    @property
    def _done(self) -> int:
        return self._collected if self._can_count else self._broken

    @property
    def failed(self) -> bool:
        """Blind, or short of the count. Either way this is not a success,
        and the runner reports it as incomplete rather than done.

        Felling a tree has no count: it failed if it broke nothing, or broke
        logs it then could not pick up."""
        if self.whole_tree:
            return (self._blind or self._broken == 0
                    or (self._can_count and self._collected < self._broken))
        return self._blind or self._done < self.count

    @property
    def goal(self) -> str:
        if self.whole_tree:
            return "fell the tree"
        return f"break {self.count} log(s)"

    @property
    def done_reason(self) -> str:
        if self._blind:
            return CANNOT_SEE_TARGET
        how = (" (found by the terrain scan)" if self._used_the_map
               else " (found by sweeping the crosshair)")
        # Why it stopped breaking, and why drops were left, are different
        # facts; the second used to hide the first.
        trouble = " ".join(t for t in (self._walk_failed or self._aim_gave_up,
                                       self._pickup_note) if t)
        # Later logs skipped for reach overwrite the reason, and the first
        # one -- the log it pointed straight at and could not hit -- is the
        # one that explains the rest.
        if trouble and self._in_the_way and self._in_the_way not in trouble:
            trouble = f"{trouble} Before that: {self._in_the_way}"
        if trouble and self.failed:
            return self._with_fetch(f"{self._progress_text()}{how}. {trouble}")
        return self._with_fetch(f"{self._progress_text()}{how}")

    def _progress_text(self) -> str:
        what = self._noun(True)
        if self._can_count:
            return (f"broke {self._broken} block(s) and collected "
                    f"{self._collected} of {self.count} {what}, counted in "
                    f"the inventory")
        return (f"broke {self._broken} block(s) for {self.count} {what} — I "
                f"cannot see the inventory, so I am reporting blocks that "
                f"disappeared, not items picked up")

    # ── planning ─────────────────────────────────────────────────────────

    def _plan(self, state, step_index: int, history: tuple):
        local = nav.LocalMap.from_state(state)
        crosshair_readable = state.confidence_of("target_block") != UNKNOWN

        # Neither the scan nor the crosshair: every step would be a blind
        # swing and every verdict unverifiable. Say so on the first step
        # instead of spending the whole budget discovering it.
        if not local.usable and not crosshair_readable:
            self._blind = True
            return None

        self._count_progress(state, history)
        if self.whole_tree:
            if not local.usable:
                self._walk_failed = ("Felling a whole tree needs the terrain "
                                     "scan from the bridge mod, to tell one "
                                     "tree's logs from the next.")
                return None
            if self._tree_done or self._broken >= MAX_TREE_LOGS:
                return self._wind_up(state, local, history)
        else:
            if self._done >= self.count:
                return None

            # Enough broken. Never break more than was asked for: counting
            # only the inventory, a log that broke and fell out of pickup
            # range looked like no progress at all, and "collect one log"
            # broke three. What is left is fetching the drops -- which needs
            # the inventory to see.
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
            if self._tree_done or self._walk_failed:
                # Finished, or giving up on the rest -- either way what has
                # been broken is picked up first. Giving up used to walk away
                # from logs lying on the ground a few blocks off.
                return self._wind_up(state, local, history)
            # The map had nothing useful to add; fall through to the sweep,
            # which at least checks what is right in front of us.

        if not crosshair_readable:
            return None
        return self._work_without_the_map(state)

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
                  and r.verification.get("status") == verify_mod.SUCCESS
                  and not self._was_clearing(r)]
        self._broken = len(broken)
        self._broken_at = []
        for record in broken:
            where = (record.step.get("params") or {}).get("expect_at")
            if where:
                self._broken_at.append(tuple(where))
        if self._can_count:
            # Collected: what is actually in the bag, against where it started.
            if self._starting_logs is None:
                self._starting_logs = _item_total(state, self._drop_names())
            self._collected = _item_total(state, self._drop_names()) - self._starting_logs

    def _work_from_the_map(self, state, local, crosshair_readable,
                           history=()):
        """Next log: walk to it if it is far, aim and mine if it is close."""
        target = self._next_target(state)
        if target is None and self._tree_done:
            return None
        if target is None:
            seen = self._nearest_seen(state)
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
                    f"{local.radius or '?'} blocks and found no "
                    f"{self._noun(True)}{self._none_found_note()}.")
            else:
                self._walk_failed = self._unreachable_text(seen)
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

        # Aim at the chosen block, on the face turned towards us -- or the
        # one face that is open, for a block set in the ground.
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   target.position,
                                   face=self._aim_face(state, target))
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
                      f"{self.max_aim_steps}; {_crosshair_text(state)})"))

        # Pointed as well as we are going to get, and the crosshair is NOT on
        # a log. Leaves in front of it are broken on purpose, a few at most;
        # anything else -- the trunk's own edge, another block -- or an aim
        # that stopped converging, and holding attack now would break
        # whatever IS under the crosshair, which is exactly the thing not to
        # do. Try a different log instead.
        if converging is None and self._clears_leaves():
            clearing = self._clear_leaves(state, target)
            if clearing is not None:
                return clearing
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
            if not self._in_the_way:
                self._in_the_way = self._aim_gave_up
        self._skip.add(target.position)
        self._aim_target = None
        if len(self._skip) > MAX_SKIPPED_TARGETS:
            self._walk_failed = self._aim_gave_up
            return None
        return self._work_from_the_map(state, local, crosshair_readable,
                                       history)

    def _tally(self) -> str:
        if self.whole_tree:
            return f"{self._broken} broken from this tree so far"
        return f"{self._done}/{self.count}"

    def _wind_up(self, state, local, history):
        """Nothing more to break for now: pick up what fell, then stop.

        Not a one-way door. A tree that comes into view on the walk to a
        drop is still worth going for; only when there is nothing left to
        break does picking up become the last thing it does."""
        if self._can_count and self._collected < self._broken:
            return self._pick_up(state, local, history)
        return None

    def _clear_leaves(self, state, target):
        """A step breaking the leaf block between the eye and `target`, or
        None if the crosshair is not on one worth breaking."""
        seen = _crosshair_at(state)
        if seen is None:
            return None
        position, name = seen
        if not str(name).endswith("_leaves") or position in self._skip:
            return None
        cleared = self._leaves_for.get(target.position, 0)
        if cleared >= MAX_LEAVES_PER_LOG:
            return None
        if not aiming_mod.SHARED.within_reach(state.position, position):
            return None
        # The tool first, before anything below counts this leaf as cleared.
        ready = self._tool_for(state, name)
        if isinstance(ready, str):
            return None
        if ready is not None:
            return ready
        # In the way means NEARER than the log. A leaf behind it would mean
        # the aim is off, and breaking it would fix nothing.
        try:
            eye = (state.position[0], state.position[1] + aiming_mod.EYE_HEIGHT,
                   state.position[2])
        except (TypeError, IndexError):
            return None
        if aiming_mod.distance_to(eye, _centre(position)) >= \
                aiming_mod.distance_to(eye, _centre(target.position)):
            return None

        self._leaves_for[target.position] = cleared + 1
        self._cleared.add(position)
        # A fresh aim at the same log once the leaf is gone.
        self._aim_tries = 0
        self._aim_errors = []
        estimate = mining_mod.estimate_break_duration(name, state=state)
        return Step(action="mine",
                    params={"duration": self._mine_seconds(estimate),
                            "expect_at": list(position)},
                    expectation=verify_mod.broke_block_at(position, name),
                    note=(f"clear {name} at {position}, in the way of the "
                          f"{target.name} at {target.position} "
                          f"({cleared + 1} of {MAX_LEAVES_PER_LOG})"))

    def _was_clearing(self, record) -> bool:
        """Did this mine step break leaves in the way, not a log?"""
        where = (record.step.get("params") or {}).get("expect_at")
        try:
            return tuple(int(v) for v in where) in self._cleared
        except (TypeError, ValueError):
            return False

    def _log_under_crosshair(self, state):
        """The log the bridge says the crosshair is on, if it is within reach.

        Returns a NearbyBlock-like object with a position and a name, or
        None. Reach matters: a crosshair on a log eight blocks away is a
        perfectly good aim that breaks nothing."""
        target = getattr(state, "target_block", None)
        if target is None or getattr(target, "name", None) \
                not in self._wanted_names():
            return None
        try:
            position = (int(target.x), int(target.y), int(target.z))
        except (TypeError, ValueError):
            return None
        if position in self._skip:
            return None
        if not self._may_break(state, position, target.name):
            return None
        # Once a tree is started, a log of ANOTHER tree that happens to be
        # under the crosshair is not the next one: finishing this tree is.
        if self._tree and position not in self._tree:
            return None
        if not aiming_mod.SHARED.within_reach(state.position, position):
            return None
        return nav_target(position, target.name)

    def _mine(self, state, target):
        """Hold attack on a block the crosshair is CONFIRMED to be on --
        with the best hotbar tool for it in hand first."""
        ready = self._tool_for(state, target.name)
        if isinstance(ready, str):
            self._walk_failed = ready
            return None
        if ready is not None:
            return ready
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
                          f"({self._tally()}; {estimate.describe()})"))

    def _walk_towards(self, state, target, history=()):
        """Delegate the walking to the navigation skill.

        Delegated rather than reimplemented: a second movement loop would be a
        second set of stuck rules, a second idea of what counts as progress,
        and a second thing to get wrong. NavigateTo already refuses routes it
        cannot see and gives up honestly, and those are exactly the properties
        this needs.

        One walker per target. _next_log re-picks the nearest log every
        step, and a walker kept from an earlier pick walked to THAT log's
        column -- then "arrived", and the new log was written off as out of
        reach from anywhere I could stand. A new pick whose standing column
        is the same keeps the walk, and the stall count in it."""
        if self._walker is not None and self._walker_for != target.position \
                and nav.approach_column(state, target) \
                != self._walker.destination:
            self._walker = None
        self._walker_for = target.position
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
        drops = self._drops(state)
        drop = drops[0] if drops else None
        if drop is None:
            self._pickup_walker = None
            self._pickup_note = (
                f"I broke {self._broken} {self._broken_noun()} but only {self._collected} "
                f"reached the inventory, and I cannot see the rest lying "
                f"anywhere near where they fell.")
            return None
        where = _drop_text(drop)
        if not local.usable:
            self._pickup_note = (f"The dropped {self._noun()} is on the ground at "
                                 f"{where}, but I cannot see the terrain to "
                                 f"walk to it.")
            return None

        walker = self._pickup_walker
        if walker is not None and not _still_there(drops, self._fetching_at):
            # Gone before we got there: picked up on the way, merged into
            # another stack, or carried off. Walking on to where it was
            # fetches nothing and spends a walk; go for what is left.
            self._pickup_walker = None
            self._fetching_at = None
            return self._pick_up(state, local, history)
        if walker is None:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and I could not get to it after {self._pickup_walks} "
                    f"tries.")
                return None
            # Nearest first, but one that cannot be reached is no reason to
            # leave the others lying there.
            target = None
            for candidate in drops:
                target = _pickup_column(state, local, candidate)
                if target is not None:
                    drop, where = candidate, _drop_text(candidate)
                    break
            if target is None:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and there is nowhere I can stand close enough to pick "
                    f"it up.")
                return None
            column, within = target
            self._pickup_walks += 1
            walker = NavigateTo(destination=column, arrive_within=within)
            self._pickup_walker = walker
            self._fetch_baseline = _item_total(state, self._drop_names())
            self._fetching = where
            self._fetching_at = drop.position

        step = walker.plan(state, 0, history)
        if step is not None:
            return step
        self._pickup_walker = None
        # Picked up on the way: pickup happens on contact, often a step
        # before "arriving". Checking the bag against the moment AFTER that
        # reads as nothing gained -- the real run's "no more birch_log than
        # before", for an oak log that had just been collected.
        if self._fetch_baseline is not None \
                and _item_total(state, self._drop_names()) > self._fetch_baseline:
            self._fetch_baseline = None
            return self._pick_up(state, local, history)
        if walker.failed:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is at {where} and I could "
                    f"not walk there: {walker.done_reason}")
                return None
            return self._pick_up(state, local, history)
        # Standing on it. Pickup happens on contact within a tick or two;
        # one observed pause lets the inventory catch up -- and is CHECKED,
        # against the bag, so it is never a filler step.
        return Step(action="look", params={"dx": 1, "dy": 0},
                    expectation=verify_mod.collected(
                        self._drop_names(), label=self._noun(True)),
                    note=(f"standing where the dropped {self._noun()} is "
                          f"({self._fetching or where})"))

    def _could_be_a_drop(self, entity) -> bool:
        """A dropped item that is one of ours -- or might be, when an older
        jar does not say what the item is."""
        stack = getattr(entity, "item", None)
        return stack is None or getattr(stack, "name", None) \
            in self._drop_names()

    def _drops(self, state):
        """Logs on the ground near blocks this task broke, nearest first.

        Saplings, sticks and apples fall out of the leaves round a felled
        tree; walking to those collects nothing this task counts. A jar that
        says what each item is lets them be left; an older one does not, and
        then every item near a break is still worth a look."""
        items = [e for e in (getattr(state, "nearby_entities", None) or ())
                 if getattr(e, "category", None) == "item"
                 and getattr(e, "position", None) is not None
                 and self._could_be_a_drop(e)]
        if not items or state.position is None:
            return []
        anchors = self._broken_at or [tuple(state.position)]

        def near_a_break(entity):
            ex, ez = entity.position[0], entity.position[2]
            return any(math.hypot(ex - (a[0] + 0.5), ez - (a[2] + 0.5))
                       <= DROP_RADIUS for a in anchors)

        return sorted((e for e in items if near_a_break(e)),
                      key=lambda e: math.hypot(
                          e.position[0] - state.position[0],
                          e.position[2] - state.position[2]))

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


@dataclass
class CollectLogs(_Gatherer):
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

    name = "collect_logs"

    def _broken_noun(self) -> str:
        return "log(s)"

    def _clears_leaves(self) -> bool:
        return True

    def _nearest_seen(self, state):
        return nav.nearest_block(state, "log")

    def _none_found_note(self) -> str:
        return ". There may be a forest past that; I cannot see it"

    def _next_target(self, state):
        return self._next_log(state)

    def _work_without_the_map(self, state):
        return self._work_from_the_crosshair(state)

    def _next_log(self, state):
        """The next log to go for.

        From the tree already started, while it has one within reach of
        somewhere to stand. Only then another tree: the nearest, or the one
        under the crosshair. "Nearest log" on its own hopped between trees
        whenever a pickup walk left another trunk a little closer."""
        if self._tree:
            logs = nav.tree_logs(state, self._tree)
            self._adopt(logs)
            target = nav.nearest_of(state, logs, reachable_only=True,
                                    exclude=self._skip)
            if target is not None:
                return target
            if self.whole_tree:
                self._tree_done = True
                self._tree_left = logs
                return None
            self._tree = set()            # this one is done; the next tree

        start = self._log_under_crosshair(state)
        if start is None:
            start = nav.nearest_block(state, "log", reachable_only=True,
                                      exclude=self._skip)
        if start is None:
            return None
        self._adopt(nav.tree_logs(state, {start.position}) or (start,))
        return start

    def _adopt(self, logs) -> None:
        """Make `logs` the tree being worked on, keeping the name it was
        first given -- its trunk moves up as the bottom logs go."""
        if not logs:
            return
        label = next((self._tree_of[b.position] for b in logs
                      if b.position in self._tree_of), None)
        if label is None:
            lowest = min(logs, key=lambda b: b.y)
            label = (lowest.name, (lowest.x, lowest.z))
        for block in logs:
            self._tree.add(block.position)
            self._tree_of.setdefault(block.position, label)

    def _progress_text(self) -> str:
        if self.whole_tree:
            return self._tree_progress_text()
        if self._can_count:
            return (f"broke {self._broken} and collected {self._collected} "
                    f"of {self.count} log(s), counted in the inventory"
                    f"{self._trees_text()}")
        return (f"broke {self._broken} of {self.count} log(s){self._trees_text()}"
                f" — I cannot see the inventory, so I am reporting blocks "
                f"that disappeared, not items picked up")

    def _tree_progress_text(self) -> str:
        labels = [self._tree_of.get(tuple(w)) for w in self._broken_at]
        labels = [l for l in labels if l is not None]
        tree = _tree_label(labels[0]) if labels else "the tree"
        text = f"broke {self._broken} log(s) from {tree}"
        if self._can_count:
            text += (f" and collected {self._collected}, counted in the "
                     f"inventory")
        else:
            text += (" — I cannot see the inventory, so these are blocks "
                     "that disappeared, not items picked up")
        if self._tree_left:
            heights = sorted({b.y for b in self._tree_left})
            span = (f"y {heights[0]}" if len(heights) == 1
                    else f"y {heights[0]}–{heights[-1]}")
            text += (f"; {len(self._tree_left)} more log(s) of it are still "
                     f"standing ({span}) where I cannot reach or hit them")
        elif self._tree_done:
            text += "; none of it is left standing that I can see"
        return text

    def _trees_text(self) -> str:
        """Which trees the broken logs came from, so "why did you mine that
        tree" has a true answer rather than an invented one."""
        counts: dict = {}
        for where in self._broken_at:
            label = self._tree_of.get(tuple(where))
            if label is not None:
                counts[label] = counts.get(label, 0) + 1
        if not counts:
            return ""
        if len(counts) == 1:
            return f", all from {_tree_label(next(iter(counts)))}"
        parts = [f"{n} from {_tree_label(label)}"
                 for label, n in counts.items()]
        return f" — {', '.join(parts)}"

    def _work_from_the_crosshair(self, state):
        """The old behaviour, kept for when the mod is not running."""
        block = state.target_block
        name = getattr(block, "name", None) if block else None
        done = self._done

        if name in LOG_BLOCKS:
            ready = self._tool_for(state, name)
            if isinstance(ready, Step):
                return ready
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


# ── Gathering any block ──────────────────────────────────────────────────────

GATHERABLE = {
    # what is asked for: (blocks to break, items they drop, what to call it)
    "stone": (("stone",), ("cobblestone",), "cobblestone"),
    "deepslate": (("deepslate",), ("cobbled_deepslate",),
                  "cobbled deepslate"),
    "dirt": (("dirt", "grass_block"), ("dirt",), "dirt"),
    "sand": (("sand",), ("sand",), "sand"),
    "red_sand": (("red_sand",), ("red_sand",), "red sand"),
    "gravel": (("gravel",), ("gravel", "flint"), "gravel"),
    "coal": (("coal_ore", "deepslate_coal_ore"), ("coal",), "coal"),
    "iron": (("iron_ore", "deepslate_iron_ore"), ("raw_iron",), "raw iron"),
    "copper": (("copper_ore", "deepslate_copper_ore"), ("raw_copper",),
               "raw copper"),
}
"""collect_blocks' drop table. Success is counted in the third column's
items arriving in the inventory -- never in blocks disappearing: stone
broken by hand disappears and drops nothing."""

GATHER_ALIASES = {
    "cobblestone": "stone", "cobble": "stone", "cobbled_deepslate": "deepslate",
    "grass": "dirt", "grass_block": "dirt", "flint": "gravel",
    "coal_ore": "coal", "iron_ore": "iron", "raw_iron": "iron",
    "copper_ore": "copper", "raw_copper": "copper",
}


def exposed(local, block) -> bool:
    """Can `block` be hit without breaking another block first?

    Judged from the scan: it is the floor of its own column (open above);
    the open air over a neighbouring column reaches its height (a side in a
    cliff face or a cave wall); or it is the first solid block over its own
    column's headroom (a ceiling). Anything else may be buried, and taking
    it would mean digging -- which this does not do."""
    return open_face(local, block) is not None


def open_face(local, block, from_position=None):
    """The face of `block` that is open to the air, by the scan: "up" for
    the floor of its column, a side where the air over a neighbouring
    column reaches its height (the nearest to `from_position` when several
    are), "down" for a ceiling -- or None.

    A block set in the ground has one open face, the top. Aiming at the
    side turned towards the player -- the default -- aims into the grass
    beside it, and the crosshair lands on the grass."""
    try:
        x, y, z = int(block.x), int(block.y), int(block.z)
    except (AttributeError, TypeError, ValueError):
        return None
    ground = local.ground_at(x, z)
    if ground == y:
        return "up"
    sides = []
    for face, (nx, nz) in (("east", (x + 1, z)), ("west", (x - 1, z)),
                           ("south", (x, z + 1)), ("north", (x, z - 1))):
        floor = local.ground_at(nx, nz)
        if floor is None or floor >= y:
            continue
        room = local.clearance_at(nx, nz)
        if room is None or y <= floor + room:
            sides.append((face, (nx + 0.5, nz + 0.5)))
    if sides:
        if from_position is not None:
            sides.sort(key=lambda f: math.hypot(
                f[1][0] - from_position[0], f[1][1] - from_position[2]))
        return sides[0][0]
    if ground is not None:
        room = local.clearance_at(x, z)
        if room is not None and ground < y and y == ground + room + 1 \
                and room < nav.MAX_REPORTED_CLEARANCE:
            return "down"
    return None


def liquid_beside(state, position) -> str | None:
    """The water or lava touching `position` in the scan, or None. Breaking
    a block beside a source lets it flow -- onto the player, for lava."""
    x, y, z = position
    touching = {(x + 1, y, z), (x - 1, y, z), (x, y + 1, z), (x, y - 1, z),
                (x, y, z + 1), (x, y, z - 1)}
    for source in (getattr(state, "notable_blocks", None) or (),
                   getattr(state, "surface", None) or ()):
        for block in source:
            name = str(getattr(block, "name", ""))
            if (name in nav.LIQUIDS or "lava" in name or "water" in name) \
                    and block.position in touching:
                return " ".join(name.split("_"))
    return None


def underfoot(state, position) -> bool:
    """Is `position` under any part of the player's feet? The body is 0.6
    wide, so near a column's edge it stands on two or four of them."""
    try:
        px, py, pz = state.position
        x, y, z = position
    except (TypeError, ValueError):
        return True
    if y >= math.floor(py):
        return False
    half = nav.PLAYER_HALF_WIDTH
    columns = {(math.floor(px + dx), math.floor(pz + dz))
               for dx in (-half, half) for dz in (-half, half)}
    return (x, z) in columns


@dataclass
class CollectBlocks(_Gatherer):
    """collect_blocks: `count` of an item that comes from breaking blocks --
    cobblestone from stone, dirt, sand, gravel, coal, raw iron, raw copper.

    The gatherer collect_logs is built on, with GATHERABLE's drop table.
    Only blocks it can reach and that are exposed (it never digs), never
    one under the player's feet, and never one touching water or lava in
    the scan. The right hotbar tool first; a block nothing in the hotbar
    can harvest is refused before any swing. Done is counted from the drop
    in the inventory, so without the inventory it does not claim success."""

    target: str = "stone"
    _seen_state: object = None        # the state the candidates were judged in

    name = "collect_blocks"

    def _entry(self):
        key = "_".join(str(self.target or "").strip().lower().split())
        key = key.split(":")[-1]
        return GATHERABLE.get(GATHER_ALIASES.get(key, key))

    def _wanted_names(self) -> frozenset:
        entry = self._entry()
        return frozenset(entry[0]) if entry else frozenset()

    def _drop_names(self) -> frozenset:
        entry = self._entry()
        return frozenset(entry[1]) if entry else frozenset()

    def _noun(self, plural: bool = False) -> str:
        entry = self._entry()
        return entry[2] if entry else str(self.target)

    @property
    def goal(self) -> str:
        return f"collect {self.count} {self._noun(True)}"

    @property
    def failed(self) -> bool:
        # The drop in the inventory is the only proof that counts.
        return (self._blind or not self._can_count
                or self._collected < self.count)

    def _plan(self, state, step_index: int, history: tuple):
        if self._entry() is None:
            self._walk_failed = (
                f"I do not know how to collect {self.target!r}. I can "
                f"collect: {', '.join(sorted(GATHERABLE))}.")
            return None
        return super()._plan(state, step_index, history)

    def _may_break(self, state, position, name) -> bool:
        return not underfoot(state, position) \
            and liquid_beside(state, position) is None

    def _aim_face(self, state, target):
        return open_face(nav.LocalMap.from_state(state), target,
                         getattr(state, "position", None))

    def _candidates(self, state):
        self._seen_state = state
        local = nav.LocalMap.from_state(state)
        return [b for b in nav.blocks_matching(state, self._wanted_names())
                if b.position not in self._skip
                and self._may_break(state, b.position, b.name)
                and exposed(local, b)]

    def _next_target(self, state):
        return nav.nearest_of(state, self._candidates(state),
                              reachable_only=True)

    def _nearest_seen(self, state):
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()))

    def _unreachable_text(self, seen) -> str:
        why = ("it is not reachable without digging, it is under my feet, "
               "or it touches water or lava" if self._seen_state is None
               else self._unreachable_reason(self._seen_state, seen))
        return (f"The nearest {block_label(seen)} I can see, at "
                f"{seen.position}, will not do: {why}.")

    def _unreachable_reason(self, state, seen) -> str:
        if underfoot(state, seen.position):
            return "it is under my feet"
        wet = liquid_beside(state, seen.position)
        if wet:
            return f"it touches {wet}, which would flow in"
        if not exposed(nav.LocalMap.from_state(state), seen):
            return "it is not reachable without digging, and I do not dig"
        return "there is no walkable route to somewhere to stand by it"

    def _progress_text(self) -> str:
        what = self._noun(True)
        if self._can_count:
            return (f"broke {self._broken} block(s) and collected "
                    f"{self._collected} of {self.count} {what}, counted in "
                    f"the inventory")
        return (f"broke {self._broken} block(s), but I cannot see the "
                f"inventory, so I cannot say how much {what} arrived")


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
    # Logs still standing over the drop mean it is under the trunk. The scan
    # lists every log, so this does not depend on how the ground there was
    # read: a real run with an older mod routed into a trunk's column for a
    # pickup and walked into the tree.
    candidates = [(c, 0.35) for c in neighbours]
    if not _under_the_trunk(state, (cx, cz), drop.position[1]):
        candidates.insert(0, ((cx, cz), 0.6))
    for column, within in candidates:
        if column == here:
            return column, within
        if not local.standable(*column):
            continue
        if nav.find_path(state, column).found:
            return column, within
    return None


def _under_the_trunk(state, column, drop_y) -> bool:
    """Is there a log in this column above a drop lying at `drop_y`?"""
    for block in (getattr(state, "notable_blocks", None) or ()):
        if (block.name in LOG_BLOCKS and (block.x, block.z) == column
                and block.y >= math.floor(drop_y)):
            return True
    return False


def _tree_label(label) -> str:
    """("birch_log", (-39, -201)) as "the birch tree at (-39, -201)"."""
    name, (x, z) = label
    kind = " ".join(str(name).split("_"))
    if kind.endswith(" log"):
        kind = kind[:-4]
    return f"the {kind} tree at ({x}, {z})"


FETCHED_DROP_DRIFT = 1.5
"""How far a dropped item may move and still be the one being fetched.
Items slide a little after they fall; further than this, the walk to where
it was would no longer pick it up anyway."""


def _still_there(drops, where) -> bool:
    """Is the drop a pickup walk set out for still (about) where it was?"""
    if where is None:
        return True
    return any(math.hypot(d.position[0] - where[0], d.position[2] - where[2])
               <= FETCHED_DROP_DRIFT for d in drops)


def _drop_text(entity) -> str:
    try:
        x, y, z = entity.position
        return f"({x:.0f}, {y:.0f}, {z:.0f})"
    except (TypeError, ValueError):
        return "somewhere nearby"


def _item_total(state, names) -> int:
    """Every stack of any of `names` in the inventory, added up."""
    total = 0
    for stack in (state.inventory or ()):
        if getattr(stack, "name", None) in names:
            total += int(getattr(stack, "count", 0) or 0)
    return total


def _log_total(state) -> int:
    """Every kind of log in the inventory, added up."""
    return _item_total(state, LOG_BLOCKS)


# ── Eating ───────────────────────────────────────────────────────────────────

EAT_TICKS = {"dried_kelp": 16, "honey_bottle": 40}
"""How long the game takes to eat or drink these, in ticks. Every other food
takes 32."""

EAT_SLOW_SERVER = 1.25
"""Allowance for a server running behind: 16 ticks a second instead of 20."""

EAT_LATENCY_S = 0.25
"""The press reaching the game, and the tick the eating starts on."""


def eat_ticks(food) -> int:
    return EAT_TICKS.get(food, 32)


def eat_seconds(food) -> float:
    """How long to hold right-click to eat ONE `food`: its use time, with
    room for a slow server and the press arriving -- and short of a second
    one, which a flat 2 seconds ate of dried kelp."""
    wanted = eat_ticks(food) / 20.0 * EAT_SLOW_SERVER + EAT_LATENCY_S
    return min(action_spec.MAX_EAT_DURATION_S, wanted)


FOODS = {
    "apple": 4, "baked_potato": 5, "beetroot": 1, "beetroot_soup": 6,
    "bread": 5, "carrot": 3, "cooked_beef": 8, "cooked_chicken": 6,
    "cooked_cod": 5, "cooked_mutton": 6, "cooked_porkchop": 8,
    "cooked_rabbit": 5, "cooked_salmon": 6, "cookie": 2, "dried_kelp": 1,
    "glow_berries": 2, "golden_carrot": 6, "honey_bottle": 6,
    "melon_slice": 2, "mushroom_stew": 6, "potato": 1, "pumpkin_pie": 8,
    "rabbit_stew": 10, "sweet_berries": 2, "beef": 3, "porkchop": 3,
    "mutton": 2, "cod": 2, "salmon": 2, "rabbit": 3, "tropical_fish": 1,
}
"""What eat_food will choose, with the hunger each restores.

Left out on purpose: anything that harms or surprises -- rotten flesh,
spider eyes, poisonous potatoes, pufferfish and raw chicken make you ill,
chorus fruit teleports you, suspicious stew is a lottery -- and golden
apples, which are worth too much to eat just because you were peckish."""

UNSAFE_FOODS = {
    "rotten_flesh": "it gives Hunger", "spider_eye": "it poisons you",
    "poisonous_potato": "it can poison you", "pufferfish": "it poisons you",
    "chicken": "raw chicken can give Hunger",
    "chorus_fruit": "it teleports you", "suspicious_stew": "its effect is "
    "random", "golden_apple": "golden apples are too valuable to eat just "
    "for hunger", "enchanted_golden_apple": "it is far too valuable to eat "
    "just for hunger",
}
"""Edible, and never chosen: the reason is what the user is told."""

MAX_HUNGER = 20

INTERACTIVE_SUFFIXES = (
    "chest", "barrel", "crafting_table", "furnace", "smoker", "_door",
    "_trapdoor", "_fence_gate", "_bed", "lever", "_button", "anvil",
    "enchanting_table", "brewing_stand", "loom", "stonecutter", "grindstone",
    "cartography_table", "smithing_table", "fletching_table", "lectern",
    "note_block", "jukebox", "repeater", "comparator", "bell",
    "shulker_box", "hopper", "dispenser", "dropper", "beacon",
    "respawn_anchor", "composter", "cauldron", "flower_pot", "_sign",
    "crafter", "decorated_pot", "chiseled_bookshelf", "campfire", "cake",
    "daylight_detector", "trial_spawner", "vault",
)
"""Blocks that do something when right-clicked. Eating is holding right
click, and with one of these under the crosshair the click opens, toggles
or uses IT instead -- a chest opened, a door swung, food put on a campfire."""


def _interactive(name) -> bool:
    name = str(name or "")
    return any(name.endswith(suffix) for suffix in INTERACTIVE_SUFFIXES)


@dataclass
class EatFood:
    """Eat from the hotbar until not hungry, or `count` items.

    FOOD IN THE MAIN INVENTORY IS FETCHED FIRST
        Choosing a hotbar slot is a key press; food anywhere else needs the
        inventory screen. With no food on the hotbar it moves the best food
        from the main inventory into it (HotbarFetch: one number-key swap,
        through the click gate) and eats that. Without a mod that reports
        screens, or in creative, it names the food and asks instead.

    RIGHT CLICK IS NOT ONLY EATING
        With a chest, door or furnace under the crosshair, holding right
        click uses that instead. It looks up at the sky first, and will not
        eat while something that reacts to a click is still in front of it.

    Health is not watched while it runs: a starving player loses health,
    which is the reason to eat, not a reason to stop. Hostile mobs still
    stop it. The slot that was selected before is selected again after."""

    count: int = 1

    name = "eat_food"
    verifiable_with = ("hunger", "inventory", "selected_slot")
    watch_health = False

    _previous_slot: int | None = None
    _eaten: int = 0
    _failed_bites: int = 0
    _looks: int = 0
    _restoring: bool = False
    _reason: str = ""
    _blind: bool = False
    _fetch: object = None

    MAX_LOOKS = 3

    @property
    def watch_hostiles(self) -> bool:
        """Handed to the fetch's own, stricter watch while the inventory is
        open -- it closes the screen before stopping; the runner cannot."""
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def goal(self) -> str:
        return "eat something" if self.count == 1 \
            else f"eat up to {self.count} things"

    @property
    def failed(self) -> bool:
        return self._blind or (self._eaten == 0 and not self._full)

    @property
    def done_reason(self) -> str:
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {self._reason}"
        return self._reason

    _full: bool = False

    def plan(self, state, step_index: int, history: tuple):
        if state.confidence_of("inventory") == UNKNOWN \
                or not isinstance(state.hunger, (int, float)) \
                or state.selected_slot is None:
            self._blind = True
            self._reason = ("eating needs the bridge mod: without it I "
                            "cannot see hunger or what is on the hotbar")
            return None

        eats = [r for r in history if r.step.get("action") == "eat"]
        self._eaten = sum(1 for r in eats if r.verification.get("status")
                          == verify_mod.SUCCESS)
        self._failed_bites = len(eats) - self._eaten
        if self._previous_slot is None:
            self._previous_slot = state.selected_slot
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step

        hunger = state.hunger
        finished = (self._eaten >= self.count or hunger >= MAX_HUNGER
                    or self._failed_bites >= 2)
        food = None if finished else self._choose(state)
        if food is None and not finished and self._fetch is None:
            stored = self._choose(state, slots=range(9, 36))
            if stored is not None:
                self._fetch = HotbarFetch(item=stored[1],
                                          hotbar=hotbar_slot_to_fill(state),
                                          hurt_is_expected=True)
                step = self._fetch.plan(state, step_index, history)
                if step is not None:
                    return step
        if food is None:
            if not finished:
                self._reason = self._no_food(state)
            elif hunger >= MAX_HUNGER and self._eaten == 0:
                self._full = True
                self._reason = (f"not hungry ({hunger:.0f}/20) — Minecraft "
                                f"does not let you eat when full")
            elif self._failed_bites >= 2 and self._eaten == 0:
                self._reason = ("I held the food but it was not eaten, "
                                "twice — something may be in the way of "
                                "the right click")
            else:
                self._reason = (f"ate {self._eaten}; hunger is now "
                                f"{hunger:.0f}/20")
            return self._restore(state)

        slot, name = food
        if state.selected_slot != slot:
            return Step(action="hotbar_select", params={"slot": slot + 1},
                        expectation=verify_mod.holding_slot(slot + 1),
                        note=f"hold the {name} (hotbar slot {slot + 1})")

        blocker = self._in_front(state)
        if blocker:
            if self._looks >= self.MAX_LOOKS:
                self._reason = (f"{blocker} is in front of me even looking "
                                f"up, and right-clicking would use it "
                                f"instead of eating")
                return self._restore(state)
            self._looks += 1
            return self._look_up(state, blocker)

        return Step(action="eat",
                    params={"duration": eat_seconds(name)},
                    expectation=verify_mod.ate(name),
                    note=(f"eat the {name} (hunger {hunger:.0f}/20)"))

    def _choose(self, state, slots=range(9)):
        """(slot, name) of the food to eat from `slots` -- the hotbar unless
        told otherwise -- or None.

        The most filling one that does not overshoot what is missing, or if
        every one would, the smallest -- a steak at 18 of 20 is mostly
        wasted, a cookie is not."""
        hotbar = [(s.slot, s.name) for s in (state.inventory or ())
                  if s.slot is not None and s.slot in slots
                  and s.name in FOODS and (s.count or 0) > 0]
        if not hotbar:
            return None
        missing = MAX_HUNGER - state.hunger
        fits = [f for f in hotbar if FOODS[f[1]] <= missing]
        if fits:
            return max(fits, key=lambda f: (FOODS[f[1]], -f[0]))
        return min(hotbar, key=lambda f: (FOODS[f[1]], f[0]))

    def _no_food(self, state) -> str:
        elsewhere = sorted({s.name for s in (state.inventory or ())
                            if s.name in FOODS and s.slot is not None
                            and s.slot > 8})
        risky = sorted({s.name for s in (state.inventory or ())
                        if s.slot is not None and 0 <= s.slot <= 8
                        and s.name in UNSAFE_FOODS})
        if risky and not elsewhere:
            return (f"the only food on the hotbar is "
                    f"{', '.join(' '.join(n.split('_')) for n in risky)}, "
                    f"which I will not eat on my own — "
                    f"{UNSAFE_FOODS[risky[0]]}")
        if elsewhere:
            why = self._fetch.done_reason if self._fetch is not None \
                else "no reason"
            return (f"there is no food on the hotbar. There is "
                    f"{', '.join(' '.join(n.split('_')) for n in elsewhere)} "
                    f"in the rest of the inventory, and I could not move it "
                    f"to the hotbar: {why}. Move it there and I can eat it")
        return "there is no food anywhere in the inventory"

    def _in_front(self, state) -> str:
        """What a right click would use instead of eating, or ""."""
        if getattr(state, "target_entity", None) is not None:
            name = getattr(state.target_entity, "name", None) or "a mob"
            return f"the {str(name).split(':')[-1]}"
        block = getattr(state, "target_block", None)
        name = getattr(block, "name", None)
        if name and _interactive(name):
            return f"a {' '.join(str(name).split('_'))}"
        return ""

    def _look_up(self, state, blocker):
        pitch = (state.rotation or (0.0, 0.0))[1] or 0.0
        wanted = -60.0                              # up at the sky
        dy = int(round((wanted - pitch) * nav.pixels_per_degree()))
        dy = max(-action_spec.MAX_LOOK_DELTA_PX, min(-40, dy))
        return Step(action="look", params={"dx": 0, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=(f"look up — {blocker} is under the crosshair and "
                          f"right-clicking it would not eat"))

    def _restore(self, state):
        """Put the slot back the way it was, once, then finish."""
        if self._restoring or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restoring = True
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")


# ── Crafting ─────────────────────────────────────────────────────────────────

_CLICK_ACTIONS = ("gui_click", "gui_swap")


def _slot_of(state, index):
    return gui_mod.slot_by_index(state, index)


def _carried_of(state):
    stack = getattr(state, "carried", None)
    if stack is None or not getattr(stack, "name", None):
        return None
    return stack.name, int(stack.count or 0)


def _words(name) -> str:
    return " ".join(str(name).split("_"))


def _learn_pointer(pointer, last) -> None:
    """Measure the pointer's gain from the move just made, if it was one."""
    if last is None or last.step.get("action") != "gui_point":
        return
    try:
        before = last.state_before["gui"]["cursor_px"]
        after = last.state_after["gui"]["cursor_px"]
        params = last.step.get("params") or {}
        pointer.observe((params.get("dx", 0), params.get("dy", 0)),
                        tuple(before), tuple(after))
    except (KeyError, TypeError):
        pass


@dataclass
class CraftItem:
    """craft_item: make `count` of `item` by clicking in the crafting grid.

    The 2x2 grid in the inventory, or a crafting table's 3x3 within reach.
    Each ingredient: the pointer brought onto its stack (closed loop on the
    pointer the game reports), a left click to pick the stack up, a right
    click per cell to put ONE in, a left click to put the rest back -- then
    a shift-click on the output, which with one of everything in the grid
    makes exactly one batch. Every click goes through the controller's gate
    (minecraft/gui.py) and every result is checked against the slots the
    game reports before the next; anything unexpected closes the screen and
    says what happened. The proof at the end is the inventory: the output
    up, the ingredients down.

    Its own danger watch, stricter than the runner's (a hostile within 8
    blocks, any health lost) -- the runner's would stop the task with the
    screen open; this one closes the screen first. See docs/minecraft-gui.md.
    """

    item: str = ""
    count: int = 1

    name = "craft_item"
    verifiable_with = ("inventory", "slots", "screen")
    watch_hostiles = False
    watch_health = False

    _phase: str = "check"
    _reason: str = ""
    _succeeded: bool = False
    _recipe: object = None
    _grid: int = 2
    _batches: int = 0
    _made: int = 0
    _start: dict = field(default_factory=dict)
    _ops: list = field(default_factory=list)
    _clicked: bool = False
    _pointer: object = field(default_factory=gui_mod.Pointer)
    _clicks: int = 0
    _peak: float | None = None
    _open_tries: int = 0
    _close_tries: int = 0
    _aim_tries: int = 0
    _table: object = None
    _walker: object = None
    _aborted: bool = False
    _still: int = 0              # pointer moves in a row that did not move it
    _placer: object = None
    _table_note: str = ""

    @property
    def goal(self) -> str:
        return f"craft {self.count} {_words(self.item)}"

    @property
    def failed(self) -> bool:
        return not self._succeeded

    @property
    def done_reason(self) -> str:
        reason = self._reason or "stopped before crafting anything"
        return f"{self._table_note}; {reason}" if self._table_note else reason

    # ── the loop ─────────────────────────────────────────────────────────

    def plan(self, state, step_index: int, history: tuple):
        last = history[-1] if history else None
        self._learn(last)
        if self._phase == "done":
            return None
        if self._phase == "closing":
            return self._closing(state)

        health = getattr(state, "health", None)
        if isinstance(health, (int, float)) and (
                self._peak is None or health > self._peak):
            self._peak = float(health)
        danger = gui_mod.danger_refusal(state, self._peak)
        if danger:
            return self._abort(state, f"I stopped crafting: {danger}.",
                               stash=False)
        # Keep the last steps for closing the screen: a task cut off by the
        # step limit would leave it open with the grid half full.
        if step_index >= MAX_TASK_STEPS - 3 and state.screen:
            return self._abort(state, "I ran out of steps before finishing.")

        if self._phase == "check":
            return self._check(state)
        if self._phase == "placing":
            return self._placing(state, step_index, history)
        if self._phase == "open":
            return self._open(state, history)
        return self._craft(state, last)

    def _stop(self, reason):
        self._reason = reason
        self._phase = "done"
        return None

    def _abort(self, state, reason, stash=True):
        """Stop: put away anything on the pointer if that is safe, close
        the screen, and say why."""
        self._aborted = True
        self._reason = reason
        self._ops = []
        self._clicked = False
        if not state.screen:
            self._phase = "done"
            return None
        if stash and _carried_of(state) is not None:
            free = self._free_slot(state)
            if free is not None:
                self._ops = [self._stash_op(state, free)]
                self._phase = "stashing"
                self._pointer.aim_at(None)
                return self._next_op_step(state)
        self._phase = "closing"
        return self._closing(state)

    def _closing(self, state):
        if not state.screen:
            self._phase = "done"
            self._finish(state)
            return None
        if self._close_tries >= 2:
            self._phase = "done"
            self._reason += " The screen would not close."
            return None
        self._close_tries += 1
        return Step(action="inventory", params={"state": "close"},
                    expectation=verify_mod.screen_closed(),
                    note="close the screen")

    def _finish(self, state):
        """The verdict, from the inventory: what the output and the
        ingredients did between the start and now."""
        if not self._start:
            return
        output = self._recipe.output
        before = self._start.get(output, 0)
        after = verify_mod._count_of(state, {output})
        gained = after - before
        used = []
        for tag in recipes_mod.ingredients(self._recipe):
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            was = self._start.get(("tag", tag), 0)
            now = verify_mod._count_of(state, names)
            if now != was:
                used.append(f"{_words(tag)} {was} → {now}")
        proof = (f"counted in the inventory: {output} {before} → {after}"
                 + (f"; {', '.join(used)}" if used else ""))
        if not self._aborted and gained >= self.count:
            self._succeeded = True
            self._reason = f"crafted {gained} {_words(output)} ({proof})."
        elif gained > 0:
            self._reason = (f"{self._reason} I crafted {gained} of "
                            f"{self.count} {_words(output)} before that "
                            f"({proof}).").strip()
        else:
            self._reason = (f"{self._reason} Nothing was crafted "
                            f"({proof}).").strip()

    # ── before anything ──────────────────────────────────────────────────

    def _check(self, state):
        recipe = recipes_mod.recipe_for(self.item)
        if recipe is None:
            return self._stop(
                f"I do not know how to craft {self.item!r}. I can craft: "
                f"{', '.join(sorted(recipes_mod.RECIPES))}.")
        self._recipe = recipe
        mode = getattr(state, "game_mode", None)
        if mode is None:
            return self._stop(
                "I cannot see the game's screens. Crafting needs the bridge "
                "mod, and a version that reports screens -- run "
                "install_mod.bat with Minecraft closed.")
        if mode not in gui_mod.CLICKABLE_MODES:
            return self._stop(f"The game is in {mode} mode, and I only craft "
                              f"in survival or adventure.")
        if state.inventory is None:
            return self._stop("I cannot see the inventory, so I cannot tell "
                              "what there is to craft with.")
        self.count = max(1, int(self.count or 1))
        self._batches = -(-self.count // recipe.count)
        short = []
        for tag, per_batch in recipes_mod.ingredients(recipe).items():
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            have = verify_mod._count_of(state, names)
            need = per_batch * self._batches
            if have < need:
                short.append(f"{need} {_words(tag)} (I have {have})")
            self._start[("tag", tag)] = have
        if short:
            return self._stop(f"To craft {self.count} {_words(recipe.output)}"
                              f" I need {', '.join(short)}.")
        self._start[recipe.output] = verify_mod._count_of(
            state, {recipe.output})
        self._grid = 2 if recipes_mod.fits(recipe, 2) else 3
        if self._grid == 3 and state.screen != "crafting_table":
            tables = nav.blocks_matching(state, {"crafting_table"})
            table = nav.nearest_of(state, tables, reachable_only=True) \
                or nav.nearest_of(state, [
                    t for t in tables if aiming_mod.SHARED.within_reach(
                        state.position, t.position)])
            if table is None:
                if verify_mod._count_of(state, {"crafting_table"}) > 0:
                    return self._place_table(state)
                return self._stop(
                    f"A {_words(recipe.output)} needs a crafting table's 3x3 "
                    f"grid, and I cannot see a crafting table I can get to. "
                    f"Place one near me (craft_item crafting_table makes "
                    f"one) and ask again.")
            self._table = table
        self._phase = "open"
        return self._open(state, ())

    # ── a table of its own ───────────────────────────────────────────────

    def _place_table(self, state):
        """No table in reach and one in the inventory: put it down beside
        the player with place_block_at -- the same proof as any placement --
        and use it."""
        local = nav.LocalMap.from_state(state)
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        spots = []
        for dx in range(-2, 3):
            for dz in range(-2, 3):
                column = (here[0] + dx, here[1] + dz)
                if column == here or max(abs(dx), abs(dz)) > 2:
                    continue
                ground = local.ground_at(*column)
                if ground is None:
                    continue
                cell = (column[0], ground + 1, column[1])
                verdict, _name = building_mod.what_is_at(local, state, cell)
                if verdict not in (building_mod.EMPTY, building_mod.REPLACE):
                    continue
                if not building_mod.references(local, state, cell,
                                               unusable=_interactive):
                    continue
                spots.append((abs(dx) + abs(dz), cell))
        if not spots:
            return self._stop(
                f"A {_words(self._recipe.output)} needs a crafting table, and "
                f"there is no clear spot beside me to put down the one I "
                f"have.")
        cell = min(spots)[1]
        self._placer = PlaceBlockAt(x=cell[0], y=cell[1], z=cell[2],
                                    item="crafting_table",
                                    allowed=frozenset({"crafting_table"}))
        self._phase = "placing"
        return self._placing(state, 0, ())

    def _placing(self, state, step_index, history):
        step = self._placer.plan(state, step_index, history)
        if step is not None:
            return step
        if not self._placer.placed:
            return self._stop(f"I could not put down the crafting table: "
                              f"{self._placer.done_reason}")
        cell = self._placer.cell
        self._table_note = (f"placed the crafting table at {cell} "
                            f"({self._placer.done_reason.split(': ', 1)[-1]})")
        self._table = NearbyBlock(*cell, "crafting_table", True)
        self._phase = "open"
        return self._open(state, history)

    # ── opening the grid ─────────────────────────────────────────────────

    def _open(self, state, history):
        wanted = "inventory" if self._grid == 2 else "crafting_table"
        if state.screen == wanted:
            self._phase = "craft"
            return self._craft(state, None)
        if state.screen:
            return self._abort(state, f"A {_words(state.screen)} screen is "
                                      f"open, not the one I need.")
        if self._open_tries >= 2:
            return self._stop(f"The {_words(wanted)} would not open.")
        if self._grid == 2:
            self._open_tries += 1
            return Step(action="inventory", params={"state": "open"},
                        expectation=verify_mod.screen_is("inventory"),
                        note="open the inventory (its 2x2 crafting grid)")
        table = self._table
        if not aiming_mod.SHARED.within_reach(state.position, table.position):
            if self._walker is None:
                column = nav.approach_column(state, table)
                if column is None:
                    return self._stop("There is nowhere to stand within "
                                      "reach of the crafting table.")
                self._walker = NavigateTo(destination=column)
            step = self._walker.plan(state, 0, history)
            if step is not None:
                return step
            if self._walker.failed:
                return self._stop(f"I could not get to the crafting table: "
                                  f"{self._walker.done_reason}")
            self._walker = None
            if not aiming_mod.SHARED.within_reach(state.position,
                                                  table.position):
                return self._stop("The crafting table is out of reach from "
                                  "where I can stand.")
        seen = _crosshair_at(state)
        if seen is not None and tuple(seen[0]) == tuple(table.position) \
                and seen[1] == "crafting_table":
            self._open_tries += 1
            return Step(action="interact", params={},
                        expectation=verify_mod.screen_is("crafting_table"),
                        note=f"use the crafting table at {table.position}")
        if self._aim_tries >= 8:
            return self._stop("I could not get the crosshair onto the "
                              "crafting table.")
        self._aim_tries += 1
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   table.position)
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.5),
                    note=f"aim at the crafting table ({error:.1f}° off)")

    # ── the clicks ───────────────────────────────────────────────────────

    def _learn(self, last):
        _learn_pointer(self._pointer, last)

    def _craft(self, state, last):
        if state.screen not in gui_mod.ALLOWED_SCREENS:
            return self._abort(state, "The screen closed under me.",
                               stash=False)
        if last is not None and last.step.get("action") == "gui_point":
            if last.verification.get("status") == verify_mod.FAILED:
                self._still += 1
            else:
                self._still = 0
            # Two moves that went nowhere: the pointer is not following.
            # Stop now, closing the screen -- not after the runner's own
            # stuck check, which would end the task with the screen open.
            if self._still >= 2:
                return self._abort(state, "The pointer did not move when I "
                                          "moved the mouse, twice.")
        if self._clicked:
            self._clicked = False
            if last is None or last.step.get("action") not in _CLICK_ACTIONS:
                return self._abort(state, "I lost track of my last click.")
            if last.verification.get("status") != verify_mod.SUCCESS:
                why = (last.action_result.get("error")
                       or last.verification.get("reason") or "no reason")
                return self._abort(state, f"A click did not do what it "
                                          f"should: {why}")
            op = self._ops.pop(0)
            if not self._ops or self._ops[0]["slot"] != op["slot"]:
                self._pointer.aim_at(None)
            if op["kind"] == "output":
                self._made += op["batches"]
        if not self._ops:
            if self._phase == "stashing":
                self._phase = "closing"
                return self._closing(state)
            if self._made >= self._batches:
                self._phase = "closing"
                return self._closing(state)
            built = self._build_ops(state)
            if isinstance(built, str):
                return self._abort(state, built)
            self._ops = built
        return self._next_op_step(state)

    def _next_op_step(self, state):
        op = self._ops[0]
        slot = _slot_of(state, op["slot"])
        if slot is None:
            return self._abort(state, f"The game no longer reports slot "
                                      f"{op['slot']}.")
        if op["kind"] == "output" and slot.item != self._recipe.output:
            return self._abort(state, (
                f"The grid should make {_words(self._recipe.output)} but the "
                f"output shows {_words(slot.item or 'nothing')}."))
        if not gui_mod.inside(state, slot):
            move = self._pointer.next_move(state, slot)
            if move is None:
                return self._abort(state, (
                    f"The pointer would not settle on the {op['what']} after "
                    f"{gui_mod.MAX_CORRECTIONS} corrections."))
            return Step(action="gui_point",
                        params={"dx": move[0], "dy": move[1]},
                        expectation=verify_mod.pointer_moved(),
                        note=f"point at the {op['what']}")
        if self._clicks >= gui_mod.MAX_GUI_CLICKS:
            return self._abort(state, f"I reached the limit of "
                                      f"{gui_mod.MAX_GUI_CLICKS} clicks.")
        self._clicks += 1
        self._clicked = True
        return Step(action="gui_click", params=op["params"],
                    expectation=op["expect"], note=op["note"])

    def _free_slot(self, state):
        for slot in (state.slots or ()):
            if slot.role in ("inventory", "hotbar") and slot.item is None:
                return slot
        return None

    def _stash_op(self, state, free):
        held = _carried_of(state)

        def check(before, after):
            if _carried_of(after) is None:
                return True, "nothing is left on the pointer."
            return False, "a stack is still on the pointer."
        return {"kind": "stash", "slot": free.i, "what": "empty slot",
                "params": {"slot": free.i, "button": "left"},
                "expect": verify_mod.gui_effect("put the stack away", check),
                "note": f"put the {_words(held[0])} on the pointer into an "
                        f"empty slot"}

    def _build_ops(self, state):
        """The clicks for one batch, from what the screen shows now -- or a
        sentence saying why there is no safe way to do it."""
        slots = state.slots or ()
        cells = sorted((s for s in slots if s.role == "craft_in"),
                       key=lambda s: s.i)
        output = next((s for s in slots if s.role == "craft_out"), None)
        if len(cells) != self._grid * self._grid or output is None:
            return "The crafting grid is not the shape I expected."
        busy = [s for s in cells if s.item]
        if busy:
            return (f"The crafting grid already has {_words(busy[0].item)} in "
                    f"it; I will not mix my recipe with what is there.")
        # Every remaining batch at once: that many in each cell, and one
        # shift-click on the output crafts exactly as many batches as the
        # grid holds. A cell holds 64.
        batches = min(self._batches - self._made, 64)
        wanted = {}
        for r, row in enumerate(self._recipe.pattern):
            for c, tag in enumerate(row):
                if tag is not None:
                    wanted.setdefault(tag, []).extend(
                        [cells[r * self._grid + c]] * batches)
        ops = []
        filled = {}
        for tag, demand in wanted.items():
            names = recipes_mod.TAGS.get(tag, frozenset({tag}))
            stacks = sorted((s for s in slots
                             if s.role in ("inventory", "hotbar")
                             and s.item in names and s.count > 0),
                            key=lambda s: -s.count)
            left = list(demand)
            for source in stacks:
                if not left:
                    break
                take = left[:source.count]
                left = left[source.count:]
                ops.extend(self._ingredient_ops(source, take, filled))
            if left:
                return f"I ran out of {_words(tag)} part-way."
        ops.append(self._output_op(output, batches))
        return ops

    def _ingredient_ops(self, source, cells, filled):
        """Pick up `source`, put one in each of `cells` (a cell repeated
        gets more than one), put the rest back. `filled` counts what each
        cell will hold, across stacks."""
        item, total = source.item, source.count

        def picked(before, after):
            slot = _slot_of(after, source.i)
            if _carried_of(after) == (item, total) and \
                    (slot is None or slot.item is None):
                return True, f"picked up {total} {_words(item)}."
            return False, (f"the pointer holds {_carried_of(after)}, not "
                           f"{total} {_words(item)}.")
        ops = [{"kind": "pickup", "slot": source.i, "what": f"{_words(item)} "
                f"stack", "params": {"slot": source.i, "button": "left"},
                "expect": verify_mod.gui_effect(f"pick up the "
                                                f"{_words(item)}", picked),
                "note": f"pick up the {_words(item)}"}]
        remaining = total
        for cell in cells:
            expected_left = remaining - 1
            filled[cell.i] = filled.get(cell.i, 0) + 1
            expected_in = filled[cell.i]

            def placed(before, after, cell=cell, expected_left=expected_left,
                       expected_in=expected_in):
                slot = _slot_of(after, cell.i)
                carried = _carried_of(after)
                left = carried[1] if carried and carried[0] == item else 0
                if slot is not None and slot.item == item \
                        and slot.count == expected_in \
                        and left == expected_left:
                    return True, (f"{expected_in} {_words(item)} in that "
                                  f"cell.")
                return False, (f"the cell holds {slot.count if slot else 0} "
                               f"{_words(slot.item or 'nothing') if slot else ''}"
                               f" and {left} are left on the pointer.")
            ops.append({"kind": "place", "slot": cell.i,
                        "what": "crafting cell",
                        "params": {"slot": cell.i, "button": "right"},
                        "expect": verify_mod.gui_effect(
                            f"put one {_words(item)} in the grid", placed),
                        "note": f"one {_words(item)} into the grid"})
            remaining = expected_left
        if remaining > 0:
            def returned(before, after, left=remaining):
                slot = _slot_of(after, source.i)
                if _carried_of(after) is None and slot is not None and \
                        slot.item == item and slot.count == left:
                    return True, f"put {left} {_words(item)} back."
                return False, "the rest did not go back where it came from."
            ops.append({"kind": "putback", "slot": source.i,
                        "what": f"{_words(item)}'s slot",
                        "params": {"slot": source.i, "button": "left"},
                        "expect": verify_mod.gui_effect(
                            f"put the rest of the {_words(item)} back",
                            returned),
                        "note": f"put the rest of the {_words(item)} back"})
        return ops

    def _output_op(self, output, batches):
        made, per = self._recipe.output, self._recipe.count * batches

        def crafted(before, after):
            gained = verify_mod._count_of(after, {made}) \
                - verify_mod._count_of(before, {made})
            if gained >= per:
                return True, f"{gained} {_words(made)} into the inventory."
            return False, (f"{gained} {_words(made)} reached the inventory, "
                           f"not {per}.")
        return {"kind": "output", "slot": output.i, "what": "output slot",
                "batches": batches,
                "params": {"slot": output.i, "button": "left", "shift": True},
                "expect": verify_mod.Expectation(
                    name="crafted", goal=f"take the {_words(made)}",
                    fields=("inventory",), predicate=crafted),
                "note": f"take the {_words(made)} (shift-click: {batches} "
                        f"batch{'es' if batches != 1 else ''})"}


# ── Into the hotbar ──────────────────────────────────────────────────────────

FETCH_WORTH_S = 1.5
"""Seconds a tool in the main inventory must save on each block before a
mining task goes into the inventory for it -- about what the trip costs:
open, two or three pointer moves, the swap, close. A tool the hotbar has no
substitute for (stone, and nothing in the hotbar that drops it) is always
worth it."""

_KEEP_AT_HAND = frozenset({"bow", "crossbow", "shield", "trident", "torch",
                           "flint_and_steel", "fishing_rod", "ender_pearl",
                           "totem_of_undying", "shears"})


def _worth_keeping(name) -> bool:
    """A tool, a weapon, food, a bucket or the like: what a player keeps on
    the hotbar on purpose. A stack of blocks is not."""
    name = str(name or "").split(":")[-1]
    return (mining_mod.tool_from_item(name).kind is not None
            or name in FOODS or name in UNSAFE_FOODS
            or name in _KEEP_AT_HAND or name.endswith("_bucket"))


def hotbar_slot_to_fill(state) -> int:
    """The hotbar slot (0-8) a fetched stack goes into: the first empty
    one; else the last holding nothing worth keeping at hand; else the last
    that is not the one held. Whatever was there is swapped into the slot
    the fetched stack came from -- nothing is dropped."""
    held = getattr(state, "selected_slot", None)
    by_slot = {s.slot: s.name for s in (getattr(state, "inventory", None) or ())
               if s.slot is not None and 0 <= s.slot <= 8
               and (s.count or 0) > 0}
    for slot in range(9):
        if slot not in by_slot:
            return slot
    for slot in range(8, -1, -1):
        if slot != held and not _worth_keeping(by_slot[slot]):
            return slot
    return 8 if held != 8 else 7


@dataclass
class HotbarFetch:
    """Bring one stack from the main inventory into the hotbar, the way a
    player does: open the inventory, put the pointer on the stack, press the
    hotbar slot's number key -- the game swaps the two -- and close it.

    Not a task of its own: eat_food and the mining tasks run it when what
    they need is in the main inventory, and go on once it is done. One
    swap, never a stack on the pointer, so nothing can be dropped. The swap
    passes the controller's click gate (minecraft/gui.py) like every click,
    and is checked against the slots the game reports, then against the
    inventory once the screen is shut.

    While the screen is open it keeps craft_item's watch -- a hostile within
    8 blocks or any health lost closes the screen and stops it -- and its
    owner hands it the runner's watch for that time (`in_screen`), because
    the runner would stop the task with the screen left open. With
    `hurt_is_expected` (eating while starving loses health on its own) a
    trip stopped by health alone, with no hostile near, is made once more.
    """

    item: str = ""
    hotbar: int = 0                  # 0-8
    hurt_is_expected: bool = False

    _phase: str = "start"            # start, opening, point, closing, done
    _reason: str = ""
    _landed: bool = False
    _pointer: object = field(default_factory=gui_mod.Pointer)
    _peak: float | None = None
    _open_tries: int = 0
    _close_tries: int = 0
    _still: int = 0
    _swapped: bool = False
    _again: bool = False
    _retried: bool = False
    _source: int | None = None       # the menu slot the stack is taken from
    _displaced: str | None = None

    @property
    def in_screen(self) -> bool:
        """From the step that opens the inventory until it is seen shut."""
        return self._phase in ("opening", "point", "closing")

    @property
    def finished(self) -> bool:
        return self._phase == "done"

    @property
    def landed(self) -> bool:
        return self.finished and self._landed

    @property
    def failed(self) -> bool:
        return self.finished and not self._landed

    @property
    def done_reason(self) -> str:
        return self._reason

    def plan(self, state, step_index: int = 0, history: tuple = ()):
        last = history[-1] if history else None
        _learn_pointer(self._pointer, last)
        if self._phase == "done":
            return None
        if self._phase == "closing":
            return self._closing(state)
        health = getattr(state, "health", None)
        if isinstance(health, (int, float)) and (
                self._peak is None or health > self._peak):
            self._peak = float(health)
        if self._phase == "start":
            return self._start(state)
        danger = gui_mod.danger_refusal(state, self._peak)
        if danger:
            mob = gui_mod.danger_refusal(state)
            if mob is None and self.hurt_is_expected and not self._retried:
                self._retried = self._again = True
            return self._abort(state, danger)
        if step_index >= MAX_TASK_STEPS - 2 and state.screen:
            return self._abort(state, "I ran out of steps")
        if self._phase == "opening":
            if state.screen == "inventory":
                self._phase = "point"
            elif state.screen:
                return self._abort(state, f"a {_words(state.screen)} screen "
                                          f"opened, not the inventory")
            elif self._open_tries >= 2:
                return self._stop("the inventory would not open")
            else:
                return self._open_step()
        return self._point(state, last)

    def refusal(self, state) -> str | None:
        """Why the trip cannot start, before anything is pressed."""
        mode = getattr(state, "game_mode", None)
        if mode is None:
            return ("the bridge mod does not report the game's screens -- "
                    "run install_mod.bat with Minecraft closed for a version "
                    "that does")
        if mode not in gui_mod.CLICKABLE_MODES:
            return (f"the game is in {mode} mode, and I only click in screens "
                    f"in survival or adventure")
        if getattr(state, "screen", None):
            return f"a {_words(state.screen)} screen is already open"
        danger = gui_mod.danger_refusal(state)
        if danger:
            return f"I did not open the inventory: {danger}"
        return None

    def _start(self, state):
        problem = self.refusal(state)
        if problem:
            return self._stop(problem)
        self._phase = "opening"
        return self._open_step()

    def _open_step(self):
        self._open_tries += 1
        return Step(action="inventory", params={"state": "open"},
                    expectation=verify_mod.screen_is("inventory"),
                    note=f"open the inventory to move the {_words(self.item)} "
                         f"into hotbar slot {self.hotbar + 1}")

    def _stop(self, reason):
        self._reason = reason
        self._phase = "done"
        return None

    def _abort(self, state, reason):
        self._reason = reason
        self._landed = False
        if not getattr(state, "screen", None):
            return self._closed(state)
        self._phase = "closing"
        return self._closing(state)

    def _closing(self, state):
        if not getattr(state, "screen", None):
            return self._closed(state)
        if self._close_tries >= 2:
            self._landed = False
            self._again = False
            return self._stop(f"{self._reason}; the inventory would not "
                              f"close".lstrip("; "))
        self._close_tries += 1
        return Step(action="inventory", params={"state": "close"},
                    expectation=verify_mod.screen_closed(),
                    note="close the inventory")

    def _closed(self, state):
        """Shut: the verdict from the inventory itself -- or, after a trip
        stopped only by expected damage, the second trip."""
        if self._again:
            self._again = False
            self._phase = "start"
            self._peak = None
            self._open_tries = self._close_tries = self._still = 0
            self._source = None
            self._pointer.aim_at(None)
            return self._start(state)
        self._phase = "done"
        if not self._landed:
            return None
        here = next((s for s in (state.inventory or ())
                     if s.slot == self.hotbar and (s.count or 0) > 0), None)
        if here is None or here.name != self.item:
            self._landed = False
            self._reason = (f"the swap looked right in the screen, but with it "
                            f"shut hotbar slot {self.hotbar + 1} holds "
                            f"{_words(here.name) if here else 'nothing'}")
            return None
        self._reason = (f"moved the {_words(self.item)} into hotbar slot "
                        f"{self.hotbar + 1}")
        if self._displaced:
            self._reason += (f" (the {_words(self._displaced)} there went "
                             f"where it came from)")
        return None

    def _point(self, state, last):
        if state.screen != "inventory":
            return self._abort(state, "the inventory closed under me")
        if last is not None and last.step.get("action") == "gui_point":
            if last.verification.get("status") == verify_mod.FAILED:
                self._still += 1
            else:
                self._still = 0
            if self._still >= 2:
                return self._abort(state, "the pointer did not move when I "
                                          "moved the mouse, twice")
        if self._swapped:
            self._swapped = False
            if last is None or last.step.get("action") != "gui_swap":
                return self._abort(state, "I lost track of the swap")
            if last.verification.get("status") != verify_mod.SUCCESS:
                why = (last.action_result.get("error")
                       or last.verification.get("reason") or "no reason")
                return self._abort(state, f"the swap did not do what it "
                                          f"should: {why}")
            self._landed = True
            self._phase = "closing"
            return self._closing(state)
        slots = state.slots or ()
        row = sorted((s for s in slots if s.role == "hotbar"),
                     key=lambda s: s.i)
        if len(row) != 9:
            return self._abort(state, "the screen does not show the hotbar "
                                      "where I expect it")
        target = row[self.hotbar]
        source = _slot_of(state, self._source) if self._source is not None \
            else None
        if source is None or source.item != self.item:
            stacks = [s for s in slots if s.role == "inventory"
                      and s.item == self.item and (s.count or 0) > 0]
            if not stacks:
                return self._abort(state, f"I cannot see the "
                                          f"{_words(self.item)} in the "
                                          f"inventory screen")
            source = max(stacks, key=lambda s: (s.count, -s.i))
            self._source = source.i
            self._pointer.aim_at(source)
        if not gui_mod.inside(state, source):
            move = self._pointer.next_move(state, source)
            if move is None:
                return self._abort(state, (
                    f"the pointer would not settle on the "
                    f"{_words(self.item)} after {gui_mod.MAX_CORRECTIONS} "
                    f"corrections"))
            return Step(action="gui_point",
                        params={"dx": move[0], "dy": move[1]},
                        expectation=verify_mod.pointer_moved(),
                        note=f"point at the {_words(self.item)}")
        moved = (source.item, source.count)
        was = (target.item, target.count) if target.item else None
        source_i, target_i = source.i, target.i

        def swapped(before, after):
            there = _slot_of(after, target_i)
            back = _slot_of(after, source_i)
            now_there = (there.item, there.count) if there and there.item \
                else None
            now_back = (back.item, back.count) if back and back.item \
                else None
            if now_there == moved and now_back == was:
                return True, (f"the {_words(moved[0])} is in hotbar slot "
                              f"{self.hotbar + 1}.")
            return False, (f"hotbar slot {self.hotbar + 1} holds "
                           f"{now_there or 'nothing'} and the stack's old "
                           f"slot holds {now_back or 'nothing'}.")
        self._swapped = True
        self._displaced = target.item
        return Step(action="gui_swap",
                    params={"slot": source.i, "hotbar": self.hotbar + 1},
                    expectation=verify_mod.gui_effect(
                        f"swap the {_words(self.item)} into hotbar slot "
                        f"{self.hotbar + 1}", swapped),
                    note=f"press {self.hotbar + 1} over the "
                         f"{_words(self.item)}: it swaps into hotbar slot "
                         f"{self.hotbar + 1}")


# ── One block into one cell ──────────────────────────────────────────────────

MAX_PLACE_AIMS = 8
"""Corrections to get the crosshair onto the face before giving up."""

MAX_PLACE_WALKS = 2
"""Walks to a place to stand before giving up."""

PLACE_REACH_MARGIN = 0.3
"""How far inside the game's reach the face must be. The aim point is not
exactly where the ray will touch the face."""


def _place_from(position, cell, ref) -> bool:
    """Could a player whose feet are at `position` place into `cell`
    against `ref`: out of the cell, on the outer side of the face, and
    within reach of it?"""
    if position is None:
        return False
    eye = (position[0], position[1] + aiming_mod.EYE_HEIGHT, position[2])
    point = aiming_mod.target_point(ref.position, position, ref.face)
    return (not building_mod.body_overlaps(position, cell)
            and building_mod.faces_towards(eye, ref.position, ref.face)
            and aiming_mod.distance_to(eye, point)
            <= aiming_mod.REACH - PLACE_REACH_MARGIN)


def _in_view(local, position, cell, ref) -> bool:
    """Does the map show nothing between the eye at `position` and the
    face? A block placed a moment ago is in the map: a row is not built by
    looking through its own first block."""
    eye = (position[0], position[1] + aiming_mod.EYE_HEIGHT, position[2])
    point = aiming_mod.target_point(ref.position, position, ref.face)
    own = (math.floor(position[0]), math.floor(position[2]))
    return local.sight_is_clear(
        eye, point, skip=(own, (cell[0], cell[2]),
                          (ref.position[0], ref.position[2])))


def _can_place_here(state, cell, ref) -> bool:
    if not _place_from(state.position, cell, ref):
        return False
    return _in_view(nav.LocalMap.from_state(state), state.position, cell, ref)


def _stand_for(state, cell, ref, avoid=()):
    """The nearest column with a route to it from which `_place_from` holds
    and the map shows nothing between the eye and the face -- or None."""
    local = nav.LocalMap.from_state(state)
    if not local.usable:
        return None
    here = local.origin
    best = None
    for column in nav.reachable_columns(local):
        if column in avoid or abs(column[0] - cell[0]) > 4 \
                or abs(column[1] - cell[2]) > 4:
            continue
        if not local.standable(*column) or not local.fits(*column):
            continue
        feet = (column[0] + 0.5, local.ground_at(*column) + 1,
                column[1] + 0.5)
        if not _place_from(feet, cell, ref) \
                or not _in_view(local, feet, cell, ref):
            continue
        cost = (abs(column[0] - here[0]) + abs(column[1] - here[2]),
                column)
        if best is None or cost < best[0]:
            best = (cost, column)
    return None if best is None else best[1]


def _face_words(ref) -> str:
    side = {"up": "top", "down": "underside"}.get(ref.face,
                                                  f"{ref.face} face")
    return f"the {side} of the {_words(ref.name)} at {ref.position}"


@dataclass
class PlaceBlockAt:
    """place_block_at: put one `item` block into the cell (x, y, z).

    THE CELL FIRST
        Refused unless the scan saw the cell empty or holding a plant the
        game replaces (minecraft/building.py) -- never into a block, never
        into space nobody looked at, never with anything but a plain
        building block.
    AGAINST A KNOWN BLOCK, ON A NAMED FACE
        A block cannot float: it goes against a solid neighbour the scan or
        the task itself confirmed, never one a right-click would use instead
        (a chest, a door, a crafting table). It stands within reach of that
        face, on its outer side and out of the cell, aims, and presses
        nothing until the game reports the crosshair on that block and that
        face -- which the controller checks once more as it presses
        (expect_at, expect_face). A plant in the cell is under the
        crosshair instead, and the game puts the block in its place.
    PROVEN TWICE
        The crosshair on a block of that name at the cell AND the held stack
        one smaller. In creative the stack does not go down, and the report
        says the crosshair is the only proof.

    One press at most: a press whose result cannot be proven is reported,
    never repeated -- a second could put a block somewhere else. Used on its
    own and, cell by cell, by the build tasks, which pass the cells they
    placed as `known`."""

    x: int = 0
    y: int = 0
    z: int = 0
    item: str = ""
    known: dict = field(default_factory=dict)
    restore_slot: bool = True
    avoid: frozenset = frozenset()   # (x, z) columns never to stand in
    allowed: frozenset = building_mod.BUILDING_BLOCKS
    """What it may place. craft_item passes {"crafting_table"}: a table is
    a full block of its own name -- a fine thing to place, only never a
    thing to place AGAINST."""

    name = "place_block_at"
    verifiable_with = ("target_block", "inventory")

    _phase: str = "check"
    _reason: str = ""
    _placed: bool = False
    _ref: object = None
    _fetch: object = None
    _walker: object = None
    _walks: int = 0
    _aims: int = 0
    _misses: int = 0
    _selects: int = 0
    _previous_slot: int | None = None
    _restored: bool = False
    _tried: tuple = ()
    _fatal: bool = False         # no point trying another cell either

    @property
    def cell(self) -> tuple:
        return (int(self.x), int(self.y), int(self.z))

    @property
    def goal(self) -> str:
        return f"place {_words(self.item)} at {self.cell}"

    @property
    def failed(self) -> bool:
        return not self._placed

    @property
    def placed(self) -> bool:
        return self._placed

    @property
    def done_reason(self) -> str:
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {self._reason}"
        return self._reason

    @property
    def watch_hostiles(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def watch_health(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def fatal(self) -> bool:
        """Failed for a reason another cell would fail for too: nothing left
        to place, or the item could not be brought to hand."""
        return self._fatal

    def _stop(self, reason, placed=False, fatal=False):
        self._reason = reason
        self._placed = placed
        self._fatal = fatal
        self._phase = "restore"
        return None

    @property
    def finished(self) -> bool:
        return self._phase == "done"

    @property
    def awaiting_verdict(self) -> bool:
        return self._phase == "placed"

    def plan(self, state, step_index: int, history: tuple):
        last = history[-1] if history else None
        if self._phase == "done":
            return None
        if self._phase == "restore":
            return self._restore(state)
        step = self._step(state, step_index, history, last)
        if step is None and self._phase == "restore":
            return self._restore(state)
        return step

    def _restore(self, state):
        self._phase = "done"
        if not self.restore_slot or self._restored \
                or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restored = True
        self._phase = "restore"          # one more look, to finish
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    def _step(self, state, step_index, history, last):
        if self._phase == "placed":
            return self._judge(state, last)
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step
            if self._fetch.failed:
                return self._stop(f"The {_words(self.item)} is in the main "
                                  f"inventory and I could not move it to "
                                  f"the hotbar: {self._fetch.done_reason}.",
                                  fatal=True)
        if self._phase == "check":
            problem = self._check(state)
            if problem:
                return self._stop(problem, fatal=self._fatal)
            self._phase = "work"
            if self._previous_slot is None:
                self._previous_slot = state.selected_slot
        if self._walker is not None:
            step = self._walker.plan(state, step_index, history)
            if step is not None:
                return step
            failed = self._walker.failed
            why = self._walker.done_reason
            self._walker = None
            if failed and not self._here_will_do(state) \
                    and self._walks >= MAX_PLACE_WALKS:
                return self._stop(f"I could not get to a place to stand: "
                                  f"{why}")
        held = self._hold(state)
        if held is not None:
            return held
        if not self._here_will_do(state):
            return self._walk(state)
        return self._aim_or_place(state)

    def _here_will_do(self, state) -> bool:
        column = (math.floor(state.position[0]), math.floor(state.position[2]))
        return column not in self.avoid \
            and _can_place_here(state, self.cell, self._ref)

    # ── before anything ──────────────────────────────────────────────────

    def _check(self, state):
        item = building_mod.short(self.item)
        self.item = item
        if item not in self.allowed:
            return (f"I only build with plain blocks -- "
                    f"{', '.join(sorted(n for n in building_mod.BUILDING_BLOCKS if not n.endswith('_planks')))}"
                    f" and any planks -- not {_words(item) or 'nothing'}.")
        if state.inventory is None or state.position is None \
                or state.confidence_of("target_block") != EXACT:
            return ("Placing a block at a coordinate needs the bridge mod: "
                    "without it I cannot see the cell, the crosshair's "
                    "block and face, or the inventory.")
        if verify_mod._count_of(state, {item}) <= 0:
            self._fatal = True
            return f"There is no {_words(item)} in the inventory."
        local = nav.LocalMap.from_state(state)
        verdict, name = building_mod.what_is_at(local, state, self.cell,
                                                self.known)
        if verdict == building_mod.OCCUPIED:
            return (f"{self.cell} already holds "
                    f"{'a ' + _words(name) if name else 'a block'}; I only "
                    f"place into empty space or grass the game replaces.")
        if verdict == building_mod.UNKNOWN:
            return (f"I cannot see that {self.cell} is empty -- it is below "
                    f"the ground, above the headroom the scan measures, or "
                    f"outside it -- so I placed nothing.")
        refs = building_mod.references(local, state, self.cell, self.known,
                                       unusable=_interactive)
        if not refs:
            blocked = [n for n in (building_mod.solid_at(
                local, state, building_mod.offset(self.cell, d), self.known)
                for d, _f in building_mod.NEIGHBOURS) if n]
            if blocked:
                return (f"The only blocks next to {self.cell} are "
                        f"{', '.join(_words(b) for b in blocked)}; "
                        f"right-clicking would use them instead of placing "
                        f"against them.")
            return (f"There is nothing solid I know of next to {self.cell} "
                    f"to place against -- a block cannot float.")
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        for ref in refs:
            if (here not in self.avoid
                    and _can_place_here(state, self.cell, ref)) \
                    or _stand_for(state, self.cell, ref,
                                  avoid=self.avoid) is not None:
                self._ref = ref
                return None
        return (f"There is nowhere I can reach to stand and place at "
                f"{self.cell} -- every block next to it is out of reach or "
                f"out of sight from where I can walk.")

    # ── the block in hand ────────────────────────────────────────────────

    def _hold(self, state):
        """A step to get the item into the hand, or None when it is."""
        stacks = [s for s in (state.inventory or ())
                  if s.name == self.item and (s.count or 0) > 0
                  and s.slot is not None]
        selected = state.selected_slot
        if any(s.slot == selected for s in stacks):
            return None
        hotbar = [s for s in stacks if 0 <= s.slot <= 8]
        if hotbar:
            if self._selects >= 2:
                return self._stop(f"I selected the {_words(self.item)} twice "
                                  f"and the game still shows another slot.")
            self._selects += 1
            slot = max(hotbar, key=lambda s: (s.count, -s.slot)).slot + 1
            return Step(action="hotbar_select", params={"slot": slot},
                        expectation=verify_mod.holding_slot(slot),
                        note=f"hold the {_words(self.item)} (hotbar slot "
                             f"{slot})")
        if self._fetch is None and stacks:
            self._fetch = HotbarFetch(item=self.item,
                                      hotbar=hotbar_slot_to_fill(state))
            step = self._fetch.plan(state)
            if step is not None:
                return step
            return self._stop(f"The {_words(self.item)} is only in the main "
                              f"inventory and I could not move it to the "
                              f"hotbar: {self._fetch.done_reason}.",
                              fatal=True)
        return self._stop(f"There is no {_words(self.item)} left to hold.",
                          fatal=True)

    # ── where to stand ───────────────────────────────────────────────────

    def _walk(self, state):
        if self._walks >= MAX_PLACE_WALKS:
            return self._stop(f"I could not find a spot to stand within "
                              f"reach of {_face_words(self._ref)} and out of "
                              f"the way of {self.cell}.")
        here = building_mod.cell_of((math.floor(state.position[0]), 0,
                                     math.floor(state.position[2])))
        column = _stand_for(state, self.cell, self._ref,
                            avoid=set(self._tried) | {(here[0], here[2])}
                            | set(self.avoid))
        if column is None:
            return self._stop(f"There is nowhere I can walk to that reaches "
                              f"{_face_words(self._ref)} without standing "
                              f"in {self.cell}.")
        self._walks += 1
        self._tried += (column,)
        self._walker = NavigateTo(destination=column, arrive_within=0.4,
                                  keep_off=frozenset(self.avoid))
        step = self._walker.plan(state, 0, ())
        if step is None:
            self._walker = None
            return self._stop(f"I could not walk to {column}.")
        return step

    # ── aim, then one press ──────────────────────────────────────────────

    def _on_target(self, state):
        """The block to require under the crosshair as the button goes
        down, and its face (None: any face) -- or None, not on it yet."""
        seen = state.target_block
        try:
            where = (int(seen.x), int(seen.y), int(seen.z))
        except (AttributeError, TypeError, ValueError):
            return None
        face = str(getattr(seen, "face", "") or "").lower()
        if where == tuple(self._ref.position) and face == self._ref.face:
            return where, face
        if where == self.cell and building_mod.short(seen.name) \
                in building_mod.REPLACEABLE:
            return where, None
        return None

    def _aim_or_place(self, state):
        target = self._on_target(state)
        if target is not None:
            local = nav.LocalMap.from_state(state)
            verdict, name = building_mod.what_is_at(local, state, self.cell,
                                                    self.known)
            if verdict not in (building_mod.EMPTY, building_mod.REPLACE):
                return self._stop(f"{self.cell} no longer looks empty "
                                  f"({_words(name or 'something')} or "
                                  f"unknown); I did not press.")
            params = {"expect_at": list(target[0])}
            if target[1] is not None:
                params["expect_face"] = target[1]
            creative = getattr(state, "game_mode", None) == "creative"
            self._phase = "placed"
            return Step(action="place", params=params,
                        expectation=verify_mod.placed_block(
                            self.cell, self.item, state.selected_slot,
                            count_proof=not creative),
                        note=(f"place {_words(self.item)} at {self.cell} "
                              f"against {_face_words(self._ref)}"))
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   self._ref.position, face=self._ref.face)
        if self._aims >= MAX_PLACE_AIMS or (dx == 0 and dy == 0):
            # Aimed as well as it can be and still not on the face:
            # something the map did not show is in the way from here. Try
            # once more from somewhere else.
            if self._walks < MAX_PLACE_WALKS:
                self._aims = 0
                return self._walk(state)
            return self._stop(self._missed_text(state))
        self._aims += 1
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.3),
                    note=(f"aim at {_face_words(self._ref)} ({error:.1f}° "
                          f"off; correction {self._aims} of "
                          f"{MAX_PLACE_AIMS})"))

    def _missed_text(self, state):
        seen = state.target_block
        on = (f"{_words(seen.name)} at ({seen.x}, {seen.y}, {seen.z}), "
              f"{getattr(seen, 'face', None) or 'no'} face"
              if seen is not None and getattr(seen, "x", None) is not None
              else "nothing in reach")
        return (f"I could not get the crosshair onto {_face_words(self._ref)}"
                f" -- it is on {on}. Something may be in the way. I placed "
                f"nothing.")

    def _judge(self, state, last):
        if last is None or last.step.get("action") != "place":
            return self._stop("I lost track of the place step.")
        result = last.action_result or {}
        if result.get("stopped_reason") == "target_not_confirmed":
            # Nothing was pressed: the crosshair moved off at the last
            # moment. Aim again, twice at most.
            self._misses += 1
            if self._misses > 2:
                return self._stop("The crosshair kept slipping off the face "
                                  "as I went to press. I placed nothing.")
            self._phase = "work"
            return self._aim_or_place(state)
        if not result.get("ok", True):
            return self._stop(f"The place was refused: "
                              f"{result.get('error') or 'no reason given'}")
        reason = last.verification.get("reason", "")
        if last.verification.get("status") == verify_mod.SUCCESS:
            self.known[self.cell] = self.item
            return self._stop(f"placed {_words(self.item)} at {self.cell} "
                              f"against {_face_words(self._ref)}: {reason}",
                              placed=True)
        return self._stop(f"I pressed place once and could not prove a "
                          f"{_words(self.item)} went into {self.cell}: "
                          f"{reason} I did not press again.")


# ── Several blocks ───────────────────────────────────────────────────────────

MAX_LINE_BLOCKS = 16
"""The longest build_line. A task's 45 steps place roughly fifteen."""

MAX_FAILURES_IN_A_ROW = 3
"""Cells failed one after another before a build gives up on the rest."""

_LINE_DIRECTIONS = {"north": (0, 0, -1), "south": (0, 0, 1),
                    "west": (-1, 0, 0), "east": (1, 0, 0), "up": (0, 1, 0)}


def _cells_text(cells) -> str:
    return ", ".join(str(tuple(c)) for c in cells) or "none"


@dataclass
class _Builder:
    """Places a list of cells one at a time with PlaceBlockAt, sharing what
    it has placed (`known`) so each block can be placed against the last.

    A cell that fails is reported and the next is tried -- three failures
    in a row, or one that would fail every cell (no blocks left), end it.
    The report names every cell placed and every cell that failed, why, and
    what the inventory says about the total. Subclasses give `_targets`
    (the cells, in order) and may override `_next_cell`."""

    item: str = ""

    verifiable_with = ("target_block", "inventory")

    _phase: str = "prepare"
    _reason: str = ""
    _cells: list = field(default_factory=list)
    _done: set = field(default_factory=set)
    _placed: list = field(default_factory=list)
    _failed: list = field(default_factory=list)
    _known: dict = field(default_factory=dict)
    _current: object = None
    _start_count: int | None = None
    _creative: bool = False
    _previous_slot: int | None = None
    _restored: bool = False
    _in_a_row: int = 0
    _last_state: object = None
    _stopped: str = ""
    _lists_untried = True

    @property
    def failed(self) -> bool:
        return not self._cells or len(self._placed) < len(self._cells)

    @property
    def watch_hostiles(self) -> bool:
        return not self._in_screen()

    @property
    def watch_health(self) -> bool:
        return not self._in_screen()

    def _in_screen(self) -> bool:
        fetch = getattr(self._current, "_fetch", None)
        return fetch is not None and fetch.in_screen

    # ── the report ───────────────────────────────────────────────────────

    @property
    def done_reason(self) -> str:
        if self._reason:
            return self._reason
        return self._report()

    def _report(self) -> str:
        total = len(self._cells)
        text = (f"placed {len(self._placed)} of {total} "
                f"{_words(self.item)}: {_cells_text(self._placed)}.")
        if self._failed:
            text += " Failed: " + "; ".join(
                f"{tuple(c)} -- {why}" for c, why in self._failed) + "."
        left = [c for c in self._cells if c not in self._done]
        if left and self._lists_untried:
            text += f" Not tried: {_cells_text(left)}."
        if self._stopped:
            text += f" {self._stopped}"
        state = self._last_state
        if self._start_count is not None and state is not None \
                and getattr(state, "inventory", None) is not None:
            now = verify_mod._count_of(state, {self.item})
            text += (f" Counted in the inventory: {self.item} "
                     f"{self._start_count} → {now}")
            used = self._start_count - now
            if self._creative:
                text += " (creative: blocks are not used up)"
            elif used != len(self._placed):
                text += (f" -- {used} used, which does not match the "
                         f"{len(self._placed)} I proved")
            text += "."
        return text

    # ── the loop ─────────────────────────────────────────────────────────

    def account_for(self, state, history) -> None:
        """The runner stopped us (danger, the step limit) right after a
        place: judge it, so the report counts it."""
        self._last_state = state
        current = self._current
        if current is None or not current.awaiting_verdict or not history:
            return
        current.plan(state, len(history), tuple(history))
        self._record(current)

    def _record(self, current) -> None:
        cell = current.cell
        if cell in self._done:
            return
        self._done.add(cell)
        if current.placed:
            self._placed.append(cell)
            self._known[cell] = self.item
            self._in_a_row = 0
        else:
            self._failed.append((cell, current.done_reason))
            self._in_a_row += 1

    def plan(self, state, step_index: int, history: tuple):
        self._last_state = state
        if self._phase == "done":
            return None
        if self._phase == "restore":
            return self._restore(state)
        if self._phase == "prepare":
            problem = self._prepare(state)
            if problem:
                self._reason = problem
                self._phase = "done"
                return None
            self._phase = "build"
        while True:
            if self._current is not None:
                step = self._current.plan(state, step_index, history)
                if step is not None:
                    return step
                current, self._current = self._current, None
                self._record(current)
                if current.fatal:
                    self._stopped = "I stopped there: no other cell would do better."
                    return self._restore(state)
                if self._in_a_row >= MAX_FAILURES_IN_A_ROW:
                    self._stopped = (f"I stopped after {self._in_a_row} "
                                     f"cells in a row failed.")
                    return self._restore(state)
            if step_index >= MAX_TASK_STEPS - 2:
                self._stopped = ("I ran out of steps for this task; ask "
                                 "again to carry on.")
                return self._restore(state)
            cell = self._next_cell(state)
            if cell is None:
                self._finished(state)
                return self._restore(state)
            self._current = PlaceBlockAt(x=cell[0], y=cell[1], z=cell[2],
                                         item=self.item, known=self._known,
                                         restore_slot=False,
                                         avoid=self._keep_out())

    def _prepare(self, state) -> str | None:
        self.item = building_mod.short(self.item)
        if self.item not in building_mod.BUILDING_BLOCKS:
            return (f"I only build with plain blocks such as dirt, "
                    f"cobblestone, stone or planks -- not "
                    f"{_words(self.item) or 'nothing'}.")
        if state.inventory is None or state.position is None:
            return ("Building needs the bridge mod: without it I cannot see "
                    "the inventory, the cells or the crosshair.")
        problem = self._targets(state)
        if isinstance(problem, str):
            return problem
        have = verify_mod._count_of(state, {self.item})
        if have <= 0:
            return f"There is no {_words(self.item)} in the inventory."
        self._start_count = have
        self._creative = getattr(state, "game_mode", None) == "creative"
        self._previous_slot = state.selected_slot
        return None

    def _targets(self, state):
        """Fill self._cells, or return why not."""
        raise NotImplementedError

    def _finished(self, state) -> None:
        """Every cell has been tried. A hook for a last check."""

    def _keep_out(self) -> frozenset:
        """Columns never to stand in while building: the structure's own.
        Standing on a half-built wall or inside a half-built room is how a
        player ends up boxed in by what they are building."""
        return frozenset((c[0], c[2]) for c in self._cells)

    def _next_cell(self, state):
        for cell in self._cells:
            if cell not in self._done:
                return cell
        return None

    def _restore(self, state):
        self._phase = "done"
        if self._restored or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restored = True
        self._phase = "restore"
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")


@dataclass
class BuildLine(_Builder):
    """build_line: up to `count` blocks of `item` in a straight line from
    (x, y, z) -- north, south, east, west, or up. Each one by
    place_block_at, against the ground or the block before it.

    Up is a column, and from the ground a column is two blocks high: the
    third block goes on a top face above a standing player's eyes, which
    cannot be seen without jumping (B4d). It says so, by cell."""

    x: int = 0
    y: int = 0
    z: int = 0
    direction: str = ""
    count: int = 1

    name = "build_line"

    @property
    def goal(self) -> str:
        return (f"build a line of {self.count} {_words(self.item)} "
                f"{self.direction} from {(self.x, self.y, self.z)}")

    def _targets(self, state):
        step = _LINE_DIRECTIONS.get(str(self.direction or "").strip().lower())
        if step is None:
            return (f"A line goes north, south, east, west or up -- not "
                    f"{self.direction!r}.")
        try:
            count = int(self.count)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= MAX_LINE_BLOCKS:
            return (f"A line is 1 to {MAX_LINE_BLOCKS} blocks; ask for "
                    f"{MAX_LINE_BLOCKS} or fewer at a time.")
        start = (int(self.x), int(self.y), int(self.z))
        self._cells = [building_mod.offset(start, (step[0] * i, step[1] * i,
                                                   step[2] * i))
                       for i in range(count)]
        return None


_BLUEPRINTS: dict = {}
"""Blueprints under way, so asking again carries on: key -> {"cells": the
plan's cells, "placed": cells proven placed}. In memory only. A task has 45
steps and a shelter needs about seventy."""

_LAST_BLUEPRINT: dict = {}
"""(plan, item) -> the key of the last one started, for "carry on" without
coordinates."""


def forget_blueprints() -> None:
    _BLUEPRINTS.clear()
    _LAST_BLUEPRINT.clear()


def _cardinal(dx, dz) -> str:
    if abs(dx) > abs(dz):
        return "east" if dx > 0 else "west"
    return "south" if dz > 0 else "north"


@dataclass
class BuildBlueprint(_Builder):
    """build_blueprint: a named plan -- platform, wall, or a hollow 3x3x3
    shelter with a doorway -- built bottom-up, each block by
    place_block_at.

    THE LIMITS, CHECKED BEFORE ANYTHING IS PRESSED
        At most 64 blocks; every cell within 6 blocks of where the task
        began; every cell empty or holding grass the game replaces; flat
        ground under the bottom layer; plain building blocks only; and
        enough of them in the inventory for the whole plan.
    ONE SENTENCE
        The plan is one sentence (`goal`, which run_task's answer repeats):
        what, of which block, where, which way the door faces, how many.
    ORDER
        The lowest unfinished layer first. Within it, the first cell in the
        plan that can be placed from where it stands (its face in reach and
        in view), else the nearest that has something to go against,
        walking to it.
    NEVER INSIDE IT
        For walls and the shelter it never stands in, or routes through,
        the structure's own footprint -- the step aside, which is there to
        be stood on for the roof. Standing on a half-built wall or inside
        the half-built room is how a player boxes themselves in.
    MORE THAN ONE TASK
        45 steps place about fifteen blocks. The plan and what has been
        proven placed are kept in memory: asking again carries on, with the
        same limits checked for what is left. Every report says what this
        task placed and failed, and what is left."""

    design: str = ""             # the plan's name; `plan` is the method
    x: int | None = None
    y: int | None = None
    z: int | None = None
    direction: str | None = None
    size: int | None = None

    name = "build_blueprint"

    _key: tuple = None
    _blueprint: object = None
    _before: int = 0                # cells placed by earlier tasks
    _checked: str = ""
    _found: list = field(default_factory=list)
    _lists_untried = False          # "Still to place" says it

    @property
    def goal(self) -> str:
        if None not in (self.x, self.y, self.z) and self.direction:
            bp = building_mod.blueprint(self.design, (self.x, self.y, self.z),
                                        self.direction, self.size,
                                        building_mod.short(self.item))
            if not isinstance(bp, str):
                return f"build {bp.sentence}"
        return (f"build a {self.design or 'blueprint'} of "
                f"{_words(building_mod.short(self.item))}")

    @property
    def failed(self) -> bool:
        record = _BLUEPRINTS.get(self._key)
        return record is None or len(record["placed"]) \
            < len(record["cells"])

    def _report(self) -> str:
        record = _BLUEPRINTS.get(self._key)
        if record is None or self._blueprint is None:
            return super()._report()
        total = len(record["cells"])
        done = len(record["placed"])
        text = super()._report()
        head = f"{self._blueprint.sentence}. "
        if self._found:
            head += (f"Already in place when I started, though not proven "
                     f"by me: {_cells_text(self._found)}. ")
        if done >= total:
            return (f"{head}Built: all {total} placed. This task {text}"
                    f"{self._checked}")
        left = [c for c in record["cells"] if c not in record["placed"]]
        return (f"{head}{done} of {total} placed so far. This task "
                f"{text}{self._checked} Still to place: {_cells_text(left)}. "
                f"Ask again (build_blueprint {self._blueprint.plan}) to "
                f"carry on.")

    def _record(self, current) -> None:
        super()._record(current)
        record = _BLUEPRINTS.get(self._key)
        if record is not None and current.placed:
            record["placed"].add(current.cell)

    def _targets(self, state):
        item = self.item
        start = building_mod.cell_of((math.floor(state.position[0]),
                                      math.floor(state.position[1]),
                                      math.floor(state.position[2])))
        local = nav.LocalMap.from_state(state)
        key = self._resume_key()
        if key is None:
            made = self._new_plan(state, local, start)
            if isinstance(made, str):
                return made
            key = made
        record = _BLUEPRINTS[key]
        self._key = key
        self._blueprint = record["blueprint"]
        _LAST_BLUEPRINT[(self._blueprint.plan, item)] = key
        left = [c for c in record["cells"] if c not in record["placed"]]
        self._before = len(record["placed"])
        far = building_mod.within(left, start)
        if far:
            anchor = record["anchor"]
            return (f"Every cell has to be within "
                    f"{building_mod.MAX_BLUEPRINT_REACH} blocks of where I "
                    f"start, and {_cells_text(far[:3])} "
                    f"{'is' if len(far) == 1 else 'are'} not. Walk closer "
                    f"to {anchor} and ask again.")
        have = verify_mod._count_of(state, {item})
        if getattr(state, "game_mode", None) != "creative" and have < len(left):
            return (f"{self._blueprint.sentence[0].upper()}"
                    f"{self._blueprint.sentence[1:]} needs {len(left)} more "
                    f"{_words(item)}, and I have {have}.")
        for cell in left:
            verdict, name = building_mod.what_is_at(local, state, cell,
                                                    record["known"])
            if verdict == building_mod.OCCUPIED and name == item:
                # The scan shows the block already there: a press an
                # earlier task could not prove, most likely. Counted as
                # found, not as placed by this task.
                record["placed"].add(cell)
                record["known"][cell] = item
                self._found.append(cell)
            elif verdict == building_mod.OCCUPIED:
                return (f"{cell} holds {'a ' + _words(name) if name else 'a block'}"
                        f", in the way of the {self._blueprint.plan}. Pick "
                        f"another spot, or clear it first.")
        left = [c for c in left if c not in record["placed"]]
        self._cells = left
        self._known = record["known"]
        return None

    def _finished(self, state) -> None:
        """With the plan placed, check it against the game's own list of
        the blocks around the player (near_blocks, B4d), where it reaches.
        A cell that does not hold the block now is taken off the placed
        list -- asking again puts it back -- and the report names it."""
        record = _BLUEPRINTS.get(self._key)
        near = getattr(state, "near", None)
        if record is None or len(record["placed"]) < len(record["cells"]):
            return
        if near is None:
            self._checked = (" Not checked block by block: the bridge mod "
                             "does not report the blocks around me "
                             "(install_mod.bat updates it).")
            return
        seen, wrong, out_of_range = 0, [], 0
        for cell in record["cells"]:
            if not near.covers(cell):
                out_of_range += 1
                continue
            block = near.block_at(cell)
            name = building_mod.short(block.name) if block else "air"
            if name == self.item:
                seen += 1
            else:
                wrong.append((cell, name))
        text = (f" Checked against the game's own list of the blocks around "
                f"me: {seen} of {len(record['cells'])} confirmed")
        if out_of_range:
            text += f", {out_of_range} too far from where I stand to check"
        if wrong:
            text += ("; not as built: " + "; ".join(
                f"{c} holds {_words(n)}" for c, n in wrong))
            for cell, _name in wrong:
                record["placed"].discard(cell)
                record["known"].pop(cell, None)
        self._checked = text + "."

    def _keep_out(self) -> frozenset:
        """For walls and a shelter, the footprint -- every column inside
        the outline, the hollow too -- except the step, which is there to
        be stood on. A platform is a floor: standing on it is the point,
        and its middle cannot be reached from outside it."""
        bp = self._blueprint
        if bp is None:
            return super()._keep_out()
        steps = {(c[0], c[2]) for c, role in bp.roles if role == "step"}
        body = [c for c, role in bp.roles if role in ("wall", "roof")]
        if not body:
            return frozenset()
        xs = [c[0] for c in body]
        zs = [c[2] for c in body]
        return frozenset((x, z) for x in range(min(xs), max(xs) + 1)
                         for z in range(min(zs), max(zs) + 1)) - steps

    def _next_cell(self, state):
        """The lowest unfinished layer; in it, the first cell in the plan
        that can be placed from where it stands (its face in reach and in
        view -- a block that would hide another's face is caught by that
        view check, not by guessing an order), else the nearest one with
        something to go against, walking to it."""
        left = [c for c in self._cells if c not in self._done]
        if not left:
            return None
        layer = min(c[1] for c in left)
        here = [c for c in left if c[1] == layer]
        local = nav.LocalMap.from_state(state)
        ready = []
        for cell in here:
            refs = building_mod.references(local, state, cell, self._known,
                                           unusable=_interactive)
            if refs:
                ready.append((cell, refs))
        if not ready:
            return here[0]           # it fails, and the report says why
        pos = state.position
        inside = (math.floor(pos[0]), math.floor(pos[2])) in self._keep_out()
        for cell, refs in ready:
            if not inside and any(_can_place_here(state, cell, r)
                                  for r in refs):
                return cell
        return min((c for c, _refs in ready),
                   key=lambda c: (math.dist(
                       (pos[0], pos[1], pos[2]),
                       (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)), c))

    def _resume_key(self):
        """The blueprint under way to carry on, or None for a new one."""
        item = self.item
        if None in (self.x, self.y, self.z):
            key = _LAST_BLUEPRINT.get((str(self.design).lower(), item))
            record = _BLUEPRINTS.get(key)
            if record is not None and \
                    len(record["placed"]) < len(record["cells"]):
                return key
            return None
        for key, record in _BLUEPRINTS.items():
            if record["anchor"] == (int(self.x), int(self.y), int(self.z)) \
                    and record["blueprint"].plan == str(self.design).lower() \
                    and key[-1] == item \
                    and len(record["placed"]) < len(record["cells"]):
                return key
        return None

    def _new_plan(self, state, local, start):
        item = self.item
        if None in (self.x, self.y, self.z):
            yaw = math.radians((state.rotation or (0.0, 0.0))[0] or 0.0)
            ahead = _cardinal(-math.sin(yaw), math.cos(yaw))
            step = building_mod.FACE_OFFSETS[{"north": "north",
                                              "south": "south",
                                              "east": "east",
                                              "west": "west"}[ahead]]
            column = (start[0] + 3 * step[0], start[2] + 3 * step[2])
            ground = local.ground_at(*column)
            if ground is None:
                return ("I cannot see the ground three blocks in front of "
                        "me. Give me x, y and z from look_around.")
            anchor = (column[0], ground + 1, column[1])
        else:
            anchor = (int(self.x), int(self.y), int(self.z))
        facing = (str(self.direction).strip().lower() if self.direction
                  else _cardinal(start[0] - anchor[0], start[2] - anchor[2]))
        bp = building_mod.blueprint(self.design, anchor, facing, self.size,
                                    item)
        if isinstance(bp, str):
            return bp
        if not building_mod.supported_in_order(bp.cells, anchor[1] - 1):
            return f"The {bp.plan} cannot be built bottom-up."
        for cell in bp.cells:
            verdict, name = building_mod.what_is_at(local, state, cell)
            if verdict == building_mod.OCCUPIED:
                return (f"{cell} holds "
                        f"{'a ' + _words(name) if name else 'a block'}, in "
                        f"the way of the {bp.plan}. Pick another spot, or "
                        f"clear it first.")
        bottom = [c for c in bp.cells if c[1] == anchor[1]]
        for cell in bottom:
            ground = local.ground_at(cell[0], cell[2])
            if ground != anchor[1] - 1:
                return (f"The ground under the {bp.plan} is not flat: at "
                        f"{(cell[0], cell[2])} it is "
                        f"{'unseen' if ground is None else f'at y={ground}'}"
                        f", not y={anchor[1] - 1}. Pick flatter ground.")
        for cell in bp.cells:
            verdict, name = building_mod.what_is_at(local, state, cell)
            if verdict == building_mod.UNKNOWN:
                return (f"I cannot see that {cell} is empty, so I will not "
                        f"start the {bp.plan} there.")
        key = (bp.plan, anchor, facing, self.size, item)
        _BLUEPRINTS[key] = {"blueprint": bp, "cells": bp.cells,
                            "anchor": anchor, "placed": set(), "known": {}}
        return key


# ── Getting away ─────────────────────────────────────────────────────────────

FLEE_SAFE_DISTANCE = 12.0
"""Run until the nearest hostile mob is further than this."""

FLEE_KEEP_CLEAR = 2
"""Columns kept clear round each hostile mob on the way."""

FLEE_REPLAN_EVERY = 4
"""Steps on one leg before choosing again: the mobs move too."""

MAX_FLEE_SECONDS = 60.0


def _hostiles(state):
    return [e for e in (getattr(state, "nearby_entities", None) or ())
            if e.hostile is True and e.distance is not None
            and e.position is not None]


def _mob_words(entity) -> str:
    return _words(str(entity.name or "hostile mob").split(":")[-1])


@dataclass
class Flee:
    """flee: get away from hostile mobs.

    Walks -- sprinting where the ground ahead is straight and clear -- to
    the reachable column of the scan furthest from every hostile it can
    see, on a route that keeps two columns clear of each, and chooses again
    every few steps as they move. Done when the nearest is further than
    FLEE_SAFE_DISTANCE, or when `seconds` run out.

    NEVER STOPPED BY A MOB OR BY DAMAGE
        A mob close by and health going down are why it runs. The runner's
        danger watch is off for it (watch_hostiles, watch_health), and the
        controller has no hazard check on movement (A1)."""

    seconds: float = 30.0

    name = "flee"
    verifiable_with = ("position", "nearby_entities")
    watch_hostiles = False
    watch_health = False

    _start: float | None = None
    _walker: object = None
    _leg_steps: int = 0
    _reason: str = ""
    _safe: bool = False
    _done: bool = False
    _last: object = None            # the nearest hostile at the last look
    _reached: set = field(default_factory=set)

    @property
    def goal(self) -> str:
        return (f"get more than {FLEE_SAFE_DISTANCE:.0f} blocks from hostile "
                f"mobs")

    @property
    def failed(self) -> bool:
        return not self._safe

    @property
    def done_reason(self) -> str:
        if self._reason:
            return self._reason
        if self._last is not None:
            return (f"still running: a {_mob_words(self._last)} is "
                    f"{self._last.distance:.0f} blocks away")
        return "stopped before getting clear"

    def _stop(self, reason, safe=False):
        self._reason, self._safe, self._done = reason, safe, True
        return None

    def plan(self, state, step_index: int, history: tuple):
        if self._done:
            return None
        if getattr(state, "nearby_entities", None) is None:
            return self._stop("I cannot see the mobs around me -- that "
                              "needs the bridge mod -- so I cannot tell "
                              "which way is away.")
        hostiles = _hostiles(state)
        if not hostiles:
            return self._stop("there is no hostile mob in sight.", safe=True)
        nearest = min(hostiles, key=lambda e: e.distance)
        self._last = nearest
        if nearest.distance > FLEE_SAFE_DISTANCE:
            return self._stop(
                f"clear: the nearest hostile, a {_mob_words(nearest)}, is "
                f"{nearest.distance:.0f} blocks away -- more than "
                f"{FLEE_SAFE_DISTANCE:.0f}.", safe=True)
        if self._start is None:
            self._start = state.captured_at
        limit = min(float(self.seconds or 30.0), MAX_FLEE_SECONDS)
        if state.captured_at - self._start >= limit:
            return self._stop(
                f"time ran out after {limit:.0f}s: a {_mob_words(nearest)} "
                f"is still {nearest.distance:.0f} blocks away.")
        for _attempt in range(2):
            if self._walker is None or self._leg_steps >= FLEE_REPLAN_EVERY:
                walker = self._leg(state, hostiles)
                if isinstance(walker, str):
                    return self._stop(walker)
                self._walker, self._leg_steps = walker, 0
            step = self._walker.plan(state, step_index, history)
            if step is not None:
                self._leg_steps += 1
                return step
            # Arrived, or that way is shut: choose again, once.
            self._reached.add(self._walker._destination)
            self._walker = None
        return self._stop(
            f"there is nowhere further to run within what I can see: a "
            f"{_mob_words(nearest)} is {nearest.distance:.0f} blocks away.")

    def _leg(self, state, hostiles):
        """A NavigateTo to the best place to be, or why there is none."""
        local = nav.LocalMap.from_state(state)
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        mobs = [(e.position[0], e.position[2]) for e in hostiles]
        now = min(math.dist((state.position[0], state.position[2]), m)
                  for m in mobs)
        # Two columns clear of each mob -- less when one is closer than
        # that already, or the clear ring would wall the player in with it.
        ring = max(0.5, min(float(FLEE_KEEP_CLEAR), now - 1.0))
        keep = frozenset(
            (math.floor(mx) + dx, math.floor(mz) + dz)
            for mx, mz in mobs
            for dx in range(-FLEE_KEEP_CLEAR, FLEE_KEEP_CLEAR + 1)
            for dz in range(-FLEE_KEEP_CLEAR, FLEE_KEEP_CLEAR + 1)
            if math.dist((math.floor(mx) + dx + 0.5,
                          math.floor(mz) + dz + 0.5), (mx, mz)) <= ring)

        def clearance(column):
            centre = (column[0] + 0.5, column[1] + 0.5)
            return min(math.dist(centre, m) for m in mobs)
        candidates = sorted(
            (c for c in nav.reachable_columns(local)
             if c not in keep and c != here and c not in self._reached
             and local.standable(*c) and clearance(c) > now + 1.0),
            key=lambda c: (-clearance(c), abs(c[0] - here[0])
                           + abs(c[1] - here[1]), c))
        for column in candidates[:6]:
            if nav.find_path(state, column, avoid=set(keep)).found:
                return NavigateTo(destination=column, arrive_within=1.0,
                                  keep_off=keep, sprint=True)
        nearest = min(hostiles, key=lambda e: e.distance)
        return (f"there is no reachable ground further from the "
                f"{_mob_words(nearest)} within what I can see; it is "
                f"{nearest.distance:.0f} blocks away.")


# ── Fighting ─────────────────────────────────────────────────────────────────

FIGHT_MAX_SECONDS = 20.0
FIGHT_RETREAT_HEALTH = 8.0
FIGHT_RANGE = 16.0
"""Only mobs this close are fought."""
ENTITY_REACH = 3.0
"""How far a survival player can hit a mob."""
ATTACK_TAP_S = 0.1
AIM_ON_BODY_DEG = 4.0

MOB_HEIGHTS = {
    "zombie": 1.95, "husk": 1.95, "drowned": 1.95, "zombie_villager": 1.95,
    "skeleton": 1.99, "stray": 1.99, "bogged": 1.99,
    "wither_skeleton": 2.4, "spider": 0.9, "cave_spider": 0.5,
    "enderman": 2.9, "witch": 1.95, "slime": 1.04, "magma_cube": 1.04,
    "silverfish": 0.3, "endermite": 0.3, "pillager": 1.95,
    "vindicator": 1.95, "evoker": 1.95, "piglin": 1.95,
    "piglin_brute": 1.95, "zombified_piglin": 1.95, "blaze": 1.8,
    "hoglin": 1.4, "zoglin": 1.4, "phantom": 0.5, "guardian": 0.85,
    "breeze": 1.77,
}
"""Hit-box heights, to aim at the middle of the body, not the feet."""

NEVER_MELEE = {"creeper": "it explodes when it is close -- flee instead",
               "warden": "it is far stronger than anything I can hold -- "
                         "flee instead"}


@dataclass
class Fight:
    """fight -- opt-in: run only when the user asks to fight.

    The nearest hostile mob within FIGHT_RANGE (or the nearest of the kind
    named in `target`): walk into reach, aim at the middle of its body,
    and attack in short taps, every other step -- Minecraft's attack
    cooldown makes a swing at once after another a weak one.

    WHAT IT NEVER HITS
        Every swing carries `expect_hostile`: the controller presses
        nothing unless the game reports a hostile mob under the crosshair
        at that moment. A player or a passive mob is never chosen, and a
        player or passive mob stepping in front is never hit. A creeper or
        a warden is not walked up to at all.
    WHEN IT STOPS
        The mob is gone from sight (killed, most likely -- the bridge does
        not report a mob's health, so that is not proven); health below
        FIGHT_RETREAT_HEALTH, when it runs (flee); or FIGHT_MAX_SECONDS
        (20) are up. The runner's danger watch is off for it: a mob close
        by is the point, and it keeps its own health rule."""

    target: str = ""
    seconds: float = FIGHT_MAX_SECONDS

    name = "fight"
    verifiable_with = ("nearby_entities", "target_entity", "health")
    watch_hostiles = False
    watch_health = False

    _start: float | None = None
    _done: bool = False
    _won: bool = False
    _reason: str = ""
    _swings: int = 0
    _mob: object = None                  # EntityRef being fought
    _retreat: object = None              # a Flee once health is low
    _retreat_note: str = ""
    _last_was_swing: bool = False

    @property
    def goal(self) -> str:
        what = _words(self.target) if self.target else "the nearest hostile mob"
        return f"fight {what}"

    @property
    def failed(self) -> bool:
        return not self._won

    @property
    def done_reason(self) -> str:
        if self._retreat is not None:
            return f"{self._retreat_note} {self._retreat.done_reason}".strip()
        return self._reason or "stopped before fighting"

    def _stop(self, reason, won=False):
        self._reason, self._won, self._done = reason, won, True
        return None

    def plan(self, state, step_index: int, history: tuple):
        if self._done:
            return None
        last = history[-1] if history else None
        if last is not None and last.step.get("action") == "attack" \
                and last.action_result.get("ok", True):
            self._swings += 1
        if self._retreat is not None:
            return self._retreat.plan(state, step_index, history)
        if getattr(state, "nearby_entities", None) is None \
                or state.position is None or state.rotation is None:
            return self._stop("I cannot see the mobs or where I am facing -- "
                              "that needs the bridge mod -- so I did not "
                              "fight.")
        health = getattr(state, "health", None)
        if isinstance(health, (int, float)) and health < FIGHT_RETREAT_HEALTH:
            self._retreat_note = (
                f"I retreated at {health:.0f} health, after {self._swings} "
                f"swing(s){self._at_mob()}:")
            self._retreat = Flee(seconds=FIGHT_MAX_SECONDS)
            return self._retreat.plan(state, step_index, history)
        if self._start is None:
            self._start = state.captured_at
        limit = min(float(self.seconds or FIGHT_MAX_SECONDS),
                    FIGHT_MAX_SECONDS)
        if state.captured_at - self._start >= limit:
            return self._stop(f"time is up ({limit:.0f}s, the most I fight "
                              f"for): {self._swings} swing(s)"
                              f"{self._at_mob()}, and it is still there.")
        mob = self._choose(state)
        if isinstance(mob, str):
            return self._stop(mob, won=self._swings > 0)
        self._mob = mob
        if mob.distance > ENTITY_REACH - 0.3:
            self._last_was_swing = False
            column = (math.floor(mob.position[0]),
                      math.floor(mob.position[2]))
            walker = NavigateTo(destination=column, arrive_within=2.0)
            step = walker.plan(state, 0, ())
            if step is not None:
                return step
        point = (mob.position[0],
                 mob.position[1] + MOB_HEIGHTS.get(_mob_key(mob), 1.8) / 2,
                 mob.position[2])
        dx, dy, error = aiming_mod.SHARED.delta_for(
            state.rotation, aiming_mod.yaw_to(state.position, point),
            aiming_mod.pitch_to(state.position, point))
        seen = getattr(state, "target_entity", None)
        on_it = (seen is not None and seen.category == "hostile"
                 and _mob_key(seen) == _mob_key(mob))
        if on_it and not self._last_was_swing:
            self._last_was_swing = True
            return Step(action="attack",
                        params={"duration": ATTACK_TAP_S,
                                "expect_hostile": True},
                        expectation=verify_mod.knocked_back(_mob_key(mob)),
                        note=f"hit the {_mob_words(mob)} "
                             f"({mob.distance:.1f} blocks)")
        self._last_was_swing = False
        if dx == 0 and dy == 0:
            dx = 1                     # a breath between swings, on target
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.05),
                    note=(f"aim at the {_mob_words(mob)}'s body "
                          f"({error:.0f}° off)"))

    def _at_mob(self) -> str:
        return f" at the {_mob_words(self._mob)}" if self._mob else ""

    def _choose(self, state):
        """The mob to fight -- the one fought so far, if it is still here --
        or why there is none."""
        wanted = building_mod.short(self.target) if self.target else ""
        mobs = [e for e in (state.nearby_entities or ())
                if e.category == "hostile" and e.hostile is True
                and e.position is not None and e.distance is not None
                and e.distance <= FIGHT_RANGE
                and (not wanted or _mob_key(e) == wanted)]
        meleeable = [e for e in mobs if _mob_key(e) not in NEVER_MELEE]
        if not meleeable:
            if self._swings and self._mob is not None:
                return (f"the {_mob_words(self._mob)} is gone after "
                        f"{self._swings} swing(s) -- killed, most likely; "
                        f"the game does not tell me a mob's health.")
            if mobs:
                worst = min(mobs, key=lambda e: e.distance)
                return (f"the only hostile close by is a "
                        f"{_mob_words(worst)}, and I will not walk up to it: "
                        f"{NEVER_MELEE[_mob_key(worst)]}.")
            return (f"there is no hostile mob "
                    f"{'called ' + _words(wanted) + ' ' if wanted else ''}"
                    f"within {FIGHT_RANGE:.0f} blocks to fight.")
        if self._mob is not None:
            same = [e for e in meleeable
                    if _mob_key(e) == _mob_key(self._mob)]
            if same:
                return min(same, key=lambda e: math.dist(
                    e.position, self._mob.position))
        return min(meleeable, key=lambda e: e.distance)


def _mob_key(entity) -> str:
    return building_mod.short(getattr(entity, "name", "") or "")


# ── Registry ─────────────────────────────────────────────────────────────────

@dataclass
class AimAtBlock(_HoldsTheRightTool):
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
        return self._with_fetch(
            self._reason or "stopped before the crosshair was confirmed")

    def _stop(self, reason: str, succeeded: bool = False):
        self._reason = reason
        self._succeeded = succeeded
        return None

    def _plan(self, state, step_index: int, history: tuple):
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
            ready = self._tool_for(state, name)
            if isinstance(ready, str):
                return self._stop(ready)
            if ready is not None:
                return ready
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
    "place_block_at": PlaceBlockAt,
    "build_line": BuildLine,
    "flee": Flee,
    "fight": Fight,
    "build_blueprint": lambda plan="", **kwargs: BuildBlueprint(
        design=plan, **kwargs),
    "collect_logs": CollectLogs,
    "fell_tree": lambda **kwargs: CollectLogs(whole_tree=True, **kwargs),
    "collect_blocks": CollectBlocks,
    "eat_food": EatFood,
    "craft_item": CraftItem,
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
    "find_iron": "the scan now reports ores it can see, so iron already "
                 "exposed in a cave wall within the scan radius is findable. "
                 "What is missing is getting to iron that is NOT exposed, "
                 "which means digging a shaft, lighting it, and not falling "
                 "into lava — none of which is built.",
    "pillar_up_or_bridge": "building upwards past two blocks, or out over "
                           "a gap, needs a jump and a place timed inside "
                           "one hold, about 0.3s apart; the controller "
                           "does not schedule taps within a hold yet "
                           "(design: docs/minecraft-jump-place.md).",
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
