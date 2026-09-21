"""
minecraft/task_runner.py — observe, act, observe, verify. A bounded number of
times, and then stop no matter what.

THE LOOP

    Goal
     ↓
    Observe ──▶ state (before)
     ↓
    Plan ONE step        (the skill decides; it cannot emit keystrokes)
     ↓
    Execute              (dispatched through a fixed table of actions)
     ↓
    Observe ──▶ state (after)
     ↓
    Verify               (did the world change the way we wanted?)
     ↓
    Continue, or stop

WHY THE STEP LIMIT IS NOT A SAFETY VALVE OF LAST RESORT
    It is the primary one. Everything else here — the guard, the session, the
    deadman — bounds a single action. Only the step limit bounds the NUMBER of
    actions, and without it a skill with a subtly wrong success condition
    swings at the same block until the session expires. `MAX_TASK_STEPS` is
    checked before every step and cannot be raised by a parameter.

    A task that runs out does not fail: it returns `incomplete` with
    `reason="step_limit"` and the state it reached, which is a true and useful
    answer the caller can act on.

THE PLANNER CANNOT PRESS KEYS
    A skill returns a `Step` naming an action from `DISPATCH` and a dict of
    parameters. That is the entire vocabulary. There is no field on `Step` that
    reaches the keyboard, no passthrough to the backend, and an action name
    that is not a key of `DISPATCH` ends the task rather than being forwarded
    anywhere. Parameters are then validated a second time by the same
    `action_spec` parsers that guard a direct call.

    So the worst a badly-behaved planner can do is pick a legal action with
    legal parameters at a bad moment — which the guard, the session and the
    step limit all still bound.

OBSERVATION IS PACED, AND EACH ONE IS USED TWICE
    One observation per step, not two. The state a step is verified AGAINST
    becomes the state the next step is PLANNED from — they are the same moment,
    so reading it twice would cost a screen capture and, worse, open a gap
    between them in which the very change being looked for can happen
    unobserved.

    That gap is not hypothetical: with two reads per step, the last swing that
    actually breaks a block is verified against a frame taken before the block
    disappears, and the break is then seen only by the next planning read —
    where nothing is checking for it. The task completes having verified
    nothing. Sharing the observation is what makes the transition land inside
    a verification rather than between two of them.

    `MIN_OBSERVATION_INTERVAL_S` is the floor between steps, and it is a floor
    rather than a target: a slow step does not get to "catch up" by observing
    faster.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from minecraft import verification as verify_mod
from minecraft.errors import InvalidAction
from minecraft.state import empty_state

MAX_TASK_STEPS = 20
"""Hard ceiling on steps in one task. Not a parameter, not configurable from a
tool call: a caller may ask for FEWER, never more."""

MIN_OBSERVATION_INTERVAL_S = 0.25
"""The floor between steps. Minecraft runs at 20 ticks per second, so anything
under about 50ms observes the same tick twice and learns nothing from it."""

DEFAULT_OBSERVATION_INTERVAL_S = 0.4

# Task outcomes.
COMPLETED = "completed"
INCOMPLETE = "incomplete"
STOPPED = "stopped"
FAILED = "failed"

# The complete set of actions a skill may ask for. A name outside this table is
# not forwarded anywhere — see the module docstring.
DISPATCH = {
    "move":           "move",
    "look":           "look",
    "jump":           "jump",
    "attack":         "attack",
    "use_item":       "use_item",
    "sneak":          "sneak",
    "sprint":         "sprint",
    "hotbar_select":  "hotbar_select",
}

ALLOWED_ACTIONS = tuple(sorted(DISPATCH))


@dataclass(frozen=True)
class Step:
    """One thing a skill wants done, and how to tell whether it worked.

    `expectation` may be None, for a step nothing can check yet. That records
    the absence of a check rather than letting an unchecked step pass as a
    success — the runner marks it UNVERIFIABLE."""

    action: str
    params: dict = field(default_factory=dict)
    expectation: object = None
    note: str = ""

    def as_dict(self) -> dict:
        return {"action": self.action, "params": self.params,
                "note": self.note,
                "expectation": getattr(self.expectation, "name", None)}


@dataclass(frozen=True)
class StepRecord:
    """What happened on one iteration. The task's audit trail."""

    index: int
    step: dict
    delivered: bool
    action_result: dict
    verification: dict
    state_before: dict
    state_after: dict

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "step": self.step,
            "action_delivered": self.delivered,
            "action_result": self.action_result,
            "verification": self.verification,
            "state_before": self.state_before,
            "state_after": self.state_after,
        }


@dataclass(frozen=True)
class TaskResult:
    """The outcome, with every step that led to it."""

    status: str
    goal: str
    reason: str = ""
    steps_taken: int = 0
    max_steps: int = MAX_TASK_STEPS
    records: tuple = ()
    final_state: dict = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status == COMPLETED

    @property
    def verified_steps(self) -> int:
        return sum(1 for r in self.records
                   if r.verification.get("status") == verify_mod.SUCCESS)

    @property
    def unverifiable_steps(self) -> int:
        return sum(1 for r in self.records
                   if r.verification.get("status") == verify_mod.UNVERIFIABLE)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "goal": self.goal,
            "reason": self.reason,
            "steps_taken": self.steps_taken,
            "max_steps": self.max_steps,
            "verified_steps": self.verified_steps,
            "unverifiable_steps": self.unverifiable_steps,
            "records": [r.as_dict() for r in self.records],
            "final_state": self.final_state,
        }

    def describe(self) -> str:
        """A sentence that does not overstate the outcome.

        In particular a task whose steps were all UNVERIFIABLE says so: it
        did the actions and cannot show they achieved anything, which is a
        different thing from success and reads differently on purpose."""
        head = {
            COMPLETED: f"{self.goal}: done",
            INCOMPLETE: f"{self.goal}: not finished",
            STOPPED: f"{self.goal}: stopped",
            FAILED: f"{self.goal}: failed",
        }.get(self.status, f"{self.goal}: {self.status}")

        detail = f" after {self.steps_taken} step(s)"
        if self.reason:
            detail += f" — {self.reason}"
        if self.completed and self.verified_steps == 0 and self.records:
            detail += (". Note: I could not verify any of it, so I am "
                       "reporting what I did, not what it achieved")
        elif self.unverifiable_steps:
            detail += (f". {self.unverifiable_steps} step(s) could not be "
                       f"verified")
        return head + detail + "."


class TaskRunner:
    """Runs one skill against a live controller, and stops for any reason.

    Holds no state between tasks: a runner is cheap, and a fresh one per task
    means a cancelled task cannot leave a flag set that affects the next."""

    def __init__(self, controller, state_source, observer=None,
                 interval_s: float = DEFAULT_OBSERVATION_INTERVAL_S,
                 sleeper=None):
        self._controller = controller
        self._state_source = state_source
        self._observer = observer
        self._interval = max(MIN_OBSERVATION_INTERVAL_S, float(interval_s))
        # Injectable so tests do not spend real seconds waiting.
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._cancel = threading.Event()

    def cancel(self, reason: str = "cancelled") -> None:
        """Ask the running task to stop at the next step boundary.

        Deliberately NOT an immediate interrupt: the controller's own stops
        handle an in-flight action, and this only has to prevent the NEXT one.
        A cancel that tore down a running action would race with the release
        path, which is the one thing that must never be raced."""
        self._reason = reason
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── the loop ─────────────────────────────────────────────────────────────

    def run(self, skill, max_steps: int = MAX_TASK_STEPS) -> TaskResult:
        goal = getattr(skill, "goal", getattr(skill, "name", "task"))
        limit = self._resolve_limit(max_steps)

        records: list = []
        # The one observation before the loop. Every later one comes from a
        # step's "after" and is reused as the next step's "before".
        state = self._read_state()

        for index in range(limit):
            blocked = self._stop_reason()
            if blocked:
                return self._result(STOPPED, goal, blocked, records, state)

            try:
                step = skill.plan(state, index, tuple(records))
            except Exception as e:
                return self._result(
                    FAILED, goal,
                    f"the skill could not decide what to do next "
                    f"({type(e).__name__}: {e})", records, state)

            if step is None:
                return self._result(
                    COMPLETED, goal,
                    getattr(skill, "done_reason", "") or "the skill is finished",
                    records, state)

            problem = self._reject(step)
            if problem:
                return self._result(FAILED, goal, problem, records, state)

            record, state = self._execute(index, step, state)
            records.append(record)

            self._sleep(self._interval)

        return self._result(
            INCOMPLETE, goal, "step_limit", records, state)

    # ── one step ─────────────────────────────────────────────────────────────

    def _execute(self, index: int, step: Step, before):
        """Run one step and judge it.

        Returns the record AND the state observed afterwards, which the caller
        keeps as the next step's starting point — see the module docstring on
        why that observation is shared rather than repeated."""
        method = getattr(self._controller, DISPATCH[step.action])

        try:
            result = method(dict(step.params or {}))
            delivered = bool(getattr(result, "ok", False))
            result_dict = result.as_dict()
        except InvalidAction as e:
            delivered = False
            result_dict = {"ok": False, "action": step.action,
                           "error_class": "InvalidAction", "error": str(e)}
        except Exception as e:                        # pragma: no cover
            delivered = False
            result_dict = {"ok": False, "action": step.action,
                           "error_class": type(e).__name__, "error": str(e)}

        after = self._read_state()

        if step.expectation is None:
            checked = verify_mod.unverifiable(
                step.note or step.action,
                "no check exists for this action yet.", delivered)
        else:
            checked = step.expectation.check(before, after, delivered)

        return StepRecord(
            index=index, step=step.as_dict(), delivered=delivered,
            action_result=result_dict, verification=checked.as_dict(),
            state_before=before.as_dict(), state_after=after.as_dict(),
        ), after

    # ── helpers ──────────────────────────────────────────────────────────────

    def _resolve_limit(self, requested) -> int:
        """A caller may ask for fewer steps than the ceiling, never more."""
        try:
            asked = int(requested)
        except (TypeError, ValueError):
            asked = MAX_TASK_STEPS
        return max(1, min(asked, MAX_TASK_STEPS))

    def _stop_reason(self) -> str:
        """Everything that should end a task between steps.

        Checked here as well as inside each action because a task is a
        sequence: an expired session must stop the NEXT step, not merely fail
        it, or the runner spends its whole step budget failing."""
        if self._cancel.is_set():
            return getattr(self, "_reason", "cancelled")
        guard = self._controller._guard()
        if guard:
            return guard
        return ""

    def _read_state(self):
        try:
            return self._state_source.read()
        except Exception as e:
            return empty_state(f"state source failed ({type(e).__name__})")

    def _reject(self, step) -> str:
        """Validate a planned step before anything is dispatched."""
        if not isinstance(step, Step):
            return (f"the skill returned {type(step).__name__}, not a Step. "
                    f"I will not act on that.")
        if step.action not in DISPATCH:
            return (f"'{step.action}' is not an action I can take. "
                    f"Allowed: {', '.join(ALLOWED_ACTIONS)}.")
        if not isinstance(step.params, dict):
            return f"the parameters for '{step.action}' were not a dictionary."
        return ""

    def _result(self, status, goal, reason, records, state) -> TaskResult:
        return TaskResult(
            status=status, goal=goal, reason=reason,
            steps_taken=len(records), max_steps=MAX_TASK_STEPS,
            records=tuple(records), final_state=state.as_dict(),
        )


__all__ = [
    "TaskRunner", "TaskResult", "Step", "StepRecord",
    "MAX_TASK_STEPS", "MIN_OBSERVATION_INTERVAL_S",
    "DEFAULT_OBSERVATION_INTERVAL_S", "DISPATCH", "ALLOWED_ACTIONS",
    "COMPLETED", "INCOMPLETE", "STOPPED", "FAILED",
]
