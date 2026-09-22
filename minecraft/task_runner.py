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
from minecraft import action_spec
from minecraft import navigation as nav
from minecraft.errors import InvalidAction
from minecraft.progress import ProgressMonitor
from minecraft.state import empty_state

MAX_TASK_STEPS = 45
"""Hard ceiling on steps in one task. Not a parameter, not configurable from a
tool call: a caller may ask for FEWER, never more.

WHY IT MOVED FROM TWENTY
    Twenty was set when a task meant a few swings on the spot. A task that
    WALKS somewhere spends steps differently: turn, move, observe, repeat,
    and collecting four logs from four different trees is a dozen short
    journeys. Twenty made "collect some wood" end mid-job, reporting three of
    four, which reads as a failure and was really a budget.

    Raising it does not widen what a task may do. Every step is still one
    bounded action validated by `action_spec`, the guard still runs before
    each one, and MAX_TASK_SECONDS still ends the whole thing after two
    minutes -- which is the binding limit for anything that walks, and the
    one that actually protects the person."""

MAX_TASK_SECONDS = 120.0
"""Wall-clock ceiling on one task, independent of the step limit.

Both are needed and neither implies the other. Forty bounded moves is around
a minute; forty steps that each wait on a slow screen capture is several. A task that has been running for two minutes has outlived the
attention of whoever asked for it, whatever its step count says."""

MAX_REPLANS = 2
"""How many times one task may change approach.

Without a cap, replanning defeats the thing it is attached to: every stuck
verdict clears the progress history, so a skill that always offers an
"alternative" resets the detector forever and runs to the step limit anyway.
That is not hypothetical -- it is what collect_logs did, sweeping twenty times
for a log it could not see, because its replan handed back the sweep it was
already doing."""

MIN_OBSERVATION_INTERVAL_S = 0.25
"""The floor between steps. Minecraft runs at 20 ticks per second, so anything
under about 50ms observes the same tick twice and learns nothing from it."""

DEFAULT_OBSERVATION_INTERVAL_S = 0.4

# Task outcomes.
COMPLETED = "completed"
INCOMPLETE = "incomplete"
STOPPED = "stopped"
FAILED = "failed"

# Why an INCOMPLETE task stopped.
STEP_LIMIT = "step_limit"
TIME_LIMIT = "time_limit"
STUCK = "stuck"

# The complete set of actions a skill may ask for. A name outside this table is
# not forwarded anywhere — see the module docstring.
DISPATCH = {
    "move":           "move",
    "look":           "look",
    "jump":           "jump",
    "sneak":          "sneak",
    "sprint":         "sprint",
    "attack":         "attack",
    "mine":           "mine",
    "place":          "place",
    "interact":       "interact",
    "use_item":       "use_item",
    "eat":            "eat",
    "drop":           "drop",
    "hotbar_select":  "hotbar_select",
    "inventory":      "inventory",
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
    progress: dict = field(default_factory=dict)

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
            "progress": self.progress,
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
            detail += f" — {self.reason.rstrip('.')}"
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
                 sleeper=None, max_seconds: float = MAX_TASK_SECONDS,
                 clock=None):
        self._controller = controller
        self._state_source = state_source
        self._observer = observer
        self._interval = max(MIN_OBSERVATION_INTERVAL_S, float(interval_s))
        # Injectable so tests do not spend real seconds waiting.
        self._sleep = sleeper if sleeper is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._max_seconds = max(1.0, min(float(max_seconds), MAX_TASK_SECONDS))
        self._cancel = threading.Event()
        self.progress = ProgressMonitor()

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
        self.progress.reset()
        replans = 0
        deadline = self._clock() + self._max_seconds

        # The one observation before the loop. Every later one comes from a
        # step's "after" and is reused as the next step's "before".
        state = self._read_state()

        for index in range(limit):
            blocked = self._stop_reason()
            if blocked:
                return self._result(STOPPED, goal, blocked, records, state)

            if self._clock() >= deadline:
                return self._result(INCOMPLETE, goal, TIME_LIMIT, records,
                                    state)

            try:
                step = skill.plan(state, index, tuple(records))
            except Exception as e:
                return self._result(
                    FAILED, goal,
                    f"the skill could not decide what to do next "
                    f"({type(e).__name__}: {e})", records, state)

            if step is None:
                # None means "no further step". It does NOT mean success: a
                # skill that has discovered it cannot do the job at all
                # returns None too, and reporting that as completed is the
                # exact overstatement this whole layer exists to prevent.
                reason = (getattr(skill, "done_reason", "")
                          or "the skill is finished")
                finished = COMPLETED
                if getattr(skill, "failed", False):
                    finished = INCOMPLETE
                return self._result(finished, goal, reason, records, state)

            problem = self._reject(step)
            if problem:
                return self._result(FAILED, goal, problem, records, state)

            record, state = self._execute(index, step, state)
            records.append(record)
            self._learn_the_mouse(record)
            self.progress.record(step.action, record.verification)

            # Stuck: the same action, the same relevant state, over and over.
            # The step limit would catch this eventually; catching it here
            # means the answer is "there is a wall" rather than "I ran out of
            # steps", and it gives a skill the chance to try something else.
            if self.progress.stuck:
                explanation = self.progress.explain()
                alternative = (self._replan(skill, state, tuple(records))
                               if replans < MAX_REPLANS else None)
                if alternative is None:
                    return self._result(INCOMPLETE, goal,
                                        f"{STUCK}: {explanation}",
                                        records, state)
                replans += 1
                self.progress.reset()

            self._sleep(self._interval)

        # Out of steps. Give the skill one last look at the record before it
        # is asked what happened: it counts its progress at the START of each
        # plan(), so without this the final step's verdict is never counted
        # and a task that broke its fourth log reports three.
        self._account(skill, state, records)
        reason = STEP_LIMIT
        summary = getattr(skill, "done_reason", "")
        if summary:
            reason = f"{STEP_LIMIT}: {summary}"
        return self._result(INCOMPLETE, goal, reason, records, state)

    @staticmethod
    def _learn_the_mouse(record) -> None:
        """Measure this machine's mouse sensitivity from any turn.

        WHY IT LIVES HERE AND NOT IN A SKILL
            How many pixels make a degree is a fact about the person's
            hardware and settings, not about any particular skill's plan.
            Every skill that turns produces the same evidence, and putting
            the measurement here means a skill gets the benefit without
            having to remember to ask for it.

            It is a measurement, not a decision: the skills still choose
            where to look, and `action_spec` still bounds what is sent."""
        if record.step.get("action") != "look":
            return
        try:
            asked = float(record.step.get("params", {}).get("dx", 0))
            was = record.state_before["rotation"][0]
            now = record.state_after["rotation"][0]
        except (KeyError, TypeError, IndexError, ValueError):
            return
        if was is None or now is None:
            return
        sent = max(-action_spec.MAX_LOOK_DELTA_PX,
                   min(asked, action_spec.MAX_LOOK_DELTA_PX))
        nav.calibrate(sent, nav.yaw_difference(was, now))

    @staticmethod
    def _account(skill, state, records) -> None:
        """Let a skill count the last step before it reports.

        Optional: a skill without `account_for` is unaffected. It exists
        because "how many did I get" is answered from the record, and the
        record gains one more entry after the last plan() call."""
        hook = getattr(skill, "account_for", None)
        if not callable(hook):
            return
        try:
            hook(state, tuple(records))
        except Exception:
            # A skill that cannot tally is still a skill that ran. Its own
            # report will be stale, which is better than losing the records.
            pass

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

    def _replan(self, skill, state, history):
        """Give the skill one chance to change approach when it is stuck.

        Optional: a skill with no `replan` is simply not asked, and the task
        ends. That is the right default — silently continuing a strategy that
        demonstrably is not working is how an agent burns its whole budget.

        A skill that replans gets its progress history cleared, so the next
        approach is judged on its own rather than inheriting the failure count
        of the one before."""
        replan = getattr(skill, "replan", None)
        if not callable(replan):
            return None
        try:
            return replan(state, self.progress.stuck_reason(), history)
        except Exception:
            return None

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
            progress=self.progress.as_dict(),
        )


__all__ = [
    "TaskRunner", "TaskResult", "Step", "StepRecord",
    "MAX_TASK_STEPS", "MAX_TASK_SECONDS", "MIN_OBSERVATION_INTERVAL_S",
    "DEFAULT_OBSERVATION_INTERVAL_S", "DISPATCH", "ALLOWED_ACTIONS",
    "COMPLETED", "INCOMPLETE", "STOPPED", "FAILED",
    "STEP_LIMIT", "TIME_LIMIT", "STUCK", "MAX_REPLANS",
]
