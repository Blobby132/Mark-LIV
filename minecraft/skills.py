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
from minecraft.task_runner import Step

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

    _found: str = ""

    def plan(self, state, step_index: int, history: tuple):
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
    swing_seconds: float = 1.0

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

    WHAT IT CAN AND CANNOT COUNT
        It cannot count logs. Inventory contents are not on the F3 overlay, so
        "collect 4 logs" cannot be verified as four — only as four blocks
        observed to disappear. That is a weaker claim and it is the one the
        result makes: `logs_broken`, not `logs_collected`. An item that
        dropped out of reach still counts as broken and was never picked up.

        Confirming collection needs the mod bridge. Until then this reports
        what it saw rather than what it hopes."""

    count: int = 4
    sweep_steps: int = 10
    delta_px: int = 100

    name = "collect_logs"
    verifiable_with = ("target_block",)

    _broken: int = 0
    _last_target: str = ""

    @property
    def goal(self) -> str:
        return f"break {self.count} log(s)"

    @property
    def done_reason(self) -> str:
        return (f"broke {self._broken} of {self.count} log(s) — I cannot see "
                f"the inventory, so I am reporting blocks that disappeared, "
                f"not items picked up")

    def plan(self, state, step_index: int, history: tuple):
        # Count from the record, not from a local tally: a swing whose
        # verification says the block went is the only evidence that counts.
        self._broken = sum(
            1 for r in history
            if r.step.get("action") == "mine"
            and r.verification.get("status") == verify_mod.SUCCESS)
        if self._broken >= self.count:
            return None

        block = state.target_block
        name = getattr(block, "name", None) if block else None

        if name in LOG_BLOCKS:
            self._last_target = name
            return Step(action="mine", params={"duration": 1.0},
                        expectation=verify_mod.block_broken(name),
                        note=f"mine {name} ({self._broken}/{self.count})")

        # Nothing wooden under the crosshair: sweep the view looking for some.
        return Step(action="look", params={"dx": self.delta_px, "dy": 0},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=f"looking for a log ({self._broken}/{self.count})")

    def replan(self, state, reason, history):
        """Stuck: stop mining and look elsewhere.

        The commonest cause is a log that will not break because it is out of
        reach, which no number of extra swings fixes. Turning is a cheap,
        bounded alternative and the progress monitor gets reset, so the new
        approach is judged on its own."""
        if reason == "no_progress":
            return "sweep for a different log"
        return None


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
    "craft_item": "needs to read the inventory and the crafting grid, "
                  "neither of which is on the F3 overlay.",
    "count_inventory": "inventory contents are not on the F3 overlay. "
                       "`collect_logs` reports blocks broken instead, which "
                       "is a weaker and honest claim.",
    "find_iron": "needs to see the world, not just the block under the crosshair.",
    "mine_ore": "needs inventory counts to confirm what was collected.",
    "eat_food": "needs hunger and the inventory.",
    "build_structure": "needs inventory and reliable placement verification.",
    "navigate_to": "needs pathfinding over terrain this version cannot see.",
    "return_to_base": "needs stored waypoints and navigation.",
}


__all__ = [
    "Skill", "WalkForward", "Survey", "FindBlock", "BreakBlock",
    "PlaceBlock", "CollectLogs",
    "BUILTIN_SKILLS", "create", "available", "LOG_BLOCKS", "NOT_YET_POSSIBLE",
]
