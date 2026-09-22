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

from minecraft import action_spec, verification as verify_mod
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
    """Turn until a block of a wanted kind is under the crosshair.

    HONEST LIMITS
        This does not "find a tree" the way a person does. It cannot see the
        scene; it can only identify the one block the crosshair is on. So it
        sweeps the view and checks each aim point, which finds a tree that is
        roughly ahead and at eye level, and misses one that is behind a hill,
        above, or beyond the crosshair's reach.

        Real scene understanding needs either a vision model looking at the
        frame or the mod bridge listing nearby blocks. Both are later phases.
        Reporting "no tree found" here means "none crossed the crosshair",
        which is what the result says."""

    wanted: frozenset = LOG_BLOCKS
    steps: int = 12
    delta_px: int = 120

    name = "find_block"
    verifiable_with = ("target_block",)

    @property
    def goal(self) -> str:
        return f"find one of: {', '.join(sorted(self.wanted)[:3])}…"

    @property
    def done_reason(self) -> str:
        return self._found or "swept the view without the crosshair "\
                              "crossing one"

    @property
    def failed(self) -> bool:
        return not self._found or self._found == CANNOT_SEE_TARGET

    _found: str = ""

    def plan(self, state, step_index: int, history: tuple):
        if state.confidence_of("target_block") == UNKNOWN:
            self._found = CANNOT_SEE_TARGET
            return None
        block = state.target_block
        name = getattr(block, "name", None) if block else None
        if name in self.wanted:
            self._found = f"the crosshair is on {name}"
            return None
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


@dataclass
class CollectLogs:
    """Find a tree, mine it, repeat. The first genuinely useful goal.

    WHAT IT COUNTS DEPENDS ON WHAT CAN BE SEEN
        With the bridge mod running, the inventory is readable and "collect 4
        logs" means four logs actually in the inventory — the real claim.

        Without it, only the block disappearing is observable, and that is a
        weaker thing: an item that fell in lava or landed out of reach was
        broken and never picked up. So the result says which claim it is
        making rather than quietly presenting one as the other."""

    count: int = 4
    sweep_steps: int = 10
    delta_px: int = 100

    name = "collect_logs"
    verifiable_with = ("target_block",)

    _broken: int = 0
    _collected: int = 0
    _starting_logs: int | None = None
    _can_count: bool = False
    _last_target: str = ""
    _blind: bool = False

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
        if self._can_count:
            return (f"collected {self._collected} of {self.count} log(s), "
                    f"counted in the inventory")
        return (f"broke {self._broken} of {self.count} log(s) — I cannot see "
                f"the inventory, so I am reporting blocks that disappeared, "
                f"not items picked up")

    def plan(self, state, step_index: int, history: tuple):
        # Nothing readable under the crosshair means this task is impossible,
        # not merely difficult: every step would be a blind swing and every
        # verdict unverifiable. Say so on the first step instead of spending
        # the whole budget discovering it.
        if state.confidence_of("target_block") == UNKNOWN:
            self._blind = True
            return None

        self._can_count = state.confidence_of("inventory") != UNKNOWN
        if self._can_count:
            # The real measure, when it is available: what is in the bag.
            if self._starting_logs is None:
                self._starting_logs = _log_total(state)
            self._collected = _log_total(state) - self._starting_logs
            done = self._collected
        else:
            # Count from the record: a swing whose verification says the block
            # went is the only evidence there is without an inventory.
            self._broken = sum(
                1 for r in history
                if r.step.get("action") == "mine"
                and r.verification.get("status") == verify_mod.SUCCESS)
            done = self._broken

        if done >= self.count:
            return None

        block = state.target_block
        name = getattr(block, "name", None) if block else None

        if name in LOG_BLOCKS:
            self._last_target = name
            check = (verify_mod.collected(name) if self._can_count
                     else verify_mod.block_broken(name))
            return Step(action="mine",
                        params={"duration": action_spec.DEFAULT_MINE_DURATION_S},
                        expectation=check,
                        note=f"mine {name} ({done}/{self.count})")

        # Nothing wooden under the crosshair: sweep the view looking for some.
        return Step(action="look", params={"dx": self.delta_px, "dy": 0},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=f"looking for a log ({done}/{self.count})")

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
    "find_iron": "needs to see the world, not just the block under the "
                 "crosshair. The bridge reports what you are looking AT, not "
                 "what is around you.",
    "eat_food": "hunger and the inventory are readable now; what is missing "
                "is choosing the right slot, which needs the hotbar mapped to "
                "what is in it.",
    "build_structure": "needs a plan and a placement order, not just the "
                       "ability to place one block.",
    "navigate_to": "needs pathfinding over terrain this version cannot see.",
    "return_to_base": "needs stored waypoints and navigation.",
}

NOW_POSSIBLE_WITH_THE_BRIDGE = (
    "inventory contents", "health", "hunger", "held item", "nearby entities",
    "exact position", "world time", "weather",
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
