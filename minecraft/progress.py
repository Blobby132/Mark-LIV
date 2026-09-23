"""
minecraft/progress.py — noticing that nothing is happening.

THE FAILURE THIS CATCHES
    Walk into a wall and every step reports honestly: the key went down, it
    was held for the time asked, no error. The action succeeded. The agent did
    not move. Repeat twenty times and the task ends at its step limit having
    done nothing, with a log full of successful actions.

    The step limit stops it eventually. That is a backstop, not a diagnosis:
    "I ran out of steps" tells the user nothing, while "I pressed forward four
    times and my position did not change" tells them there is a wall.

WHAT COUNTS AS PROGRESS
    Not "the action worked" — that is the trap above. Progress is a change in
    the state the action was SUPPOSED to change, which is exactly the state
    each verification already names. So this module reads the verification's
    own evidence rather than inventing a second idea of what matters: if
    `moved` was checking `position` and position is identical, nothing
    happened, whatever the input layer reports.

TWO KINDS OF STUCK
    Repeating with no change is one. The other is repeating with no ANSWER:
    when every verdict comes back UNVERIFIABLE, the agent is not learning
    anything and will never learn anything, because the thing that would tell
    it is unreadable. Swinging at a block forever because OCR is not installed
    is a different problem from swinging at bedrock, and they need different
    advice, so they are reported separately.

    The blind threshold is deliberately higher. An occasional unreadable frame
    is normal; a run of them is a setup problem.

WHY IT ONLY REPORTS
    Nothing here stops anything. It answers "is this going anywhere?" and the
    task runner decides. Keeping the judgement separate from the control flow
    is what lets a skill offer an alternative rather than simply failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minecraft import verification as verify_mod

STUCK_AFTER = 4
"""Identical action, identical relevant state, this many times in a row.

Four rather than two: mining genuinely takes several swings with no visible
change until the block breaks, and calling that stuck would make the most
common real task impossible."""

BLIND_AFTER = 6
"""Consecutive UNVERIFIABLE verdicts before saying so. Higher than STUCK_AFTER
because one unreadable observation is noise and six is a broken setup."""

NO_PROGRESS = "no_progress"
BLIND = "cannot_verify"


@dataclass
class _Attempt:
    action: str
    fingerprint: str
    status: str
    missing: tuple = ()      # fields the check wanted and could not read


@dataclass
class ProgressMonitor:
    """Watches a task's steps and says whether they are achieving anything."""

    stuck_after: int = STUCK_AFTER
    blind_after: int = BLIND_AFTER
    history: list = field(default_factory=list)

    # ── recording ────────────────────────────────────────────────────────────

    def record(self, action: str, verification: dict) -> None:
        """Note one step. `verification` is a Verification.as_dict()."""
        self.history.append(_Attempt(
            action=str(action or ""),
            fingerprint=_fingerprint(verification),
            status=str((verification or {}).get("status") or ""),
            missing=tuple((verification or {}).get("missing_fields") or ()),
        ))

    def reset(self) -> None:
        """Forget everything. Called when a plan changes, because a new
        approach should not inherit the old one's failure count."""
        self.history.clear()

    # ── judging ──────────────────────────────────────────────────────────────

    @property
    def repeats(self) -> int:
        """How many times the most recent action has repeated with the
        relevant state unchanged. 0 or 1 means there is nothing to see."""
        if not self.history:
            return 0
        last = self.history[-1]
        # No evidence means no evidence of non-progress. Blind steps all
        # fingerprint as empty and would otherwise match each other, reporting
        # "nothing changed" when the truth is "nothing was seen" -- a
        # different problem with different advice, handled by blind_streak.
        if not last.fingerprint:
            return 0
        count = 0
        for attempt in reversed(self.history):
            if (attempt.action != last.action
                    or attempt.fingerprint != last.fingerprint):
                break
            count += 1
        return count

    @property
    def blind_streak(self) -> int:
        count = 0
        for attempt in reversed(self.history):
            if attempt.status != verify_mod.UNVERIFIABLE:
                break
            count += 1
        return count

    @property
    def succeeded_recently(self) -> bool:
        """Any conclusive success in the current run of identical attempts.

        Guards the case where an action works every time and legitimately
        leaves the checked state looking the same."""
        return any(a.status == verify_mod.SUCCESS
                   for a in self.history[-self.stuck_after:])

    def stuck_reason(self) -> str:
        """'' while there is still reason to continue, else why not."""
        if self.succeeded_recently:
            return ""
        if self.repeats >= self.stuck_after:
            return NO_PROGRESS
        if self.blind_streak >= self.blind_after:
            return BLIND
        return ""

    @property
    def stuck(self) -> bool:
        return bool(self.stuck_reason())

    def explain(self) -> str:
        """A sentence for the user, naming the action rather than the code."""
        reason = self.stuck_reason()
        if not reason:
            return ""
        last = self.history[-1].action if self.history else "that"
        if reason == NO_PROGRESS:
            return (f"I tried to {last} {self.repeats} times and nothing "
                    f"changed. Something is in the way, or I am aiming at the "
                    f"wrong thing — I stopped rather than keep going.")
        # Two different things arrive here as UNVERIFIABLE, and they need
        # opposite explanations. A check that could not READ its fields is
        # blindness. A step with no check at all is the plan repeating itself
        # -- and blaming F3 for that sent a working bridge-mod setup looking
        # for a problem it did not have.
        streak = self.history[-self.blind_streak:]
        unread = sorted({name for attempt in streak
                         for name in attempt.missing})
        if not unread:
            return (f"I took {self.blind_streak} steps in a row that nothing "
                    f"was checking — my own plan going round in a loop, not "
                    f"a problem seeing the game — so I stopped.")
        return (f"I have taken {self.blind_streak} actions in a row without "
                f"being able to see whether any of them worked — I could not "
                f"read {', '.join(unread)} — so I stopped rather than keep "
                f"going blind. With the bridge mod running those come "
                f"straight from the game (bridge_check.bat says whether it "
                f"is); without it they need the F3 overlay and OCR.")

    def as_dict(self) -> dict:
        return {
            "steps_recorded": len(self.history),
            "repeats_without_change": self.repeats,
            "blind_streak": self.blind_streak,
            "stuck": self.stuck,
            "stuck_reason": self.stuck_reason(),
        }


def _fingerprint(verification: dict) -> str:
    """The relevant state, as a comparable string.

    Built from the verification's OWN `after` evidence — the fields its
    expectation named — so "relevant" means the same thing here as it does
    there, and a new expectation does not need a matching change in this file.

    A verdict with no evidence fingerprints as empty, which is deliberately
    NOT a match with any real reading: two blind steps should count as a blind
    streak, not as two identical states.

    "No evidence" has to mean what it says. An unverifiable check still
    reports the fields it WANTED -- `{"rotation": None}` -- so testing for an
    empty dict missed the case that matters and classified every blind step as
    "nothing changed". That is the wrong diagnosis with the wrong fix: it sent
    the user looking for a wall when the real problem was that nothing could
    be read at all. A mapping whose values are all None carries no evidence,
    whatever keys it has."""
    after = (verification or {}).get("after") or {}
    if not after or all(v is None for v in after.values()):
        return ""
    return repr(sorted((str(k), repr(v)) for k, v in after.items()))


__all__ = ["ProgressMonitor", "STUCK_AFTER", "BLIND_AFTER",
           "NO_PROGRESS", "BLIND"]
