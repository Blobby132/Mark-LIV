"""
minecraft/verification.py — did that actually work?

THE DISTINCTION THIS MODULE EXISTS TO ENFORCE

    action delivered   the input reached the game
    goal accomplished  the world changed the way we wanted

    They are not the same, and an agent that conflates them is confidently
    wrong in exactly the situations that matter. Holding the attack button for
    two seconds against an oak log is a delivered action and an unbroken log;
    reporting "I chopped the tree" is a lie built out of a true fact.

    `ActionResult.ok` answers the first question and is produced by the
    controller. `Verification` answers the second and is produced here, by
    comparing state from BEFORE the action with state from AFTER it. Nothing
    can produce a SUCCESS without two observations.

THREE OUTCOMES, NOT TWO

    SUCCESS       the expected change was observed
    FAILED        the expected change was NOT observed
    UNVERIFIABLE  we cannot see the field that would settle it

    The third is the one that keeps the rest honest. If the F3 overlay is shut,
    or OCR is not installed, or the field simply is not on the debug screen
    (health, inventory), then "did the block break?" has no answer available —
    and answering FAILED would be as much of a fabrication as answering
    SUCCESS. An agent that knows the difference can go and open the overlay. An
    agent told FAILED just swings again.

WHY EXPECTATIONS DECLARE THEIR FIELDS
    Each expectation names the WorldState fields it needs. That is what makes
    UNVERIFIABLE mechanical rather than a judgement call: if a required field
    is unknown in either snapshot, the verdict is UNVERIFIABLE before the
    predicate is ever run. A predicate therefore never sees a None it has to
    interpret, and cannot accidentally read "None != 'oak_log'" as success.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft.state import UNKNOWN, WorldState

SUCCESS = "success"
FAILED = "failed"
UNVERIFIABLE = "unverifiable"

STATUSES = (SUCCESS, FAILED, UNVERIFIABLE)

# Treated as "nothing there" when read from the target-block slot.
_AIR_NAMES = frozenset({"air", "cave_air", "void_air"})


@dataclass(frozen=True)
class Verification:
    """The verdict, and enough of the evidence to argue with it."""

    status: str
    goal: str
    reason: str = ""
    delivered: bool = False
    missing_fields: tuple = ()
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == SUCCESS

    @property
    def conclusive(self) -> bool:
        """Whether this verdict settles anything. UNVERIFIABLE does not."""
        return self.status in (SUCCESS, FAILED)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "goal": self.goal,
            "reason": self.reason,
            "action_delivered": self.delivered,
            "goal_accomplished": self.succeeded,
            "missing_fields": list(self.missing_fields),
            "before": self.before,
            "after": self.after,
        }

    def describe(self) -> str:
        if self.status == SUCCESS:
            return f"{self.goal}: confirmed — {self.reason}"
        if self.status == FAILED:
            delivered = ("The action was delivered but the world did not "
                         "change." if self.delivered else
                         "The action did not reach the game.")
            return f"{self.goal}: NOT confirmed — {self.reason} {delivered}"
        return (f"{self.goal}: I cannot tell — {self.reason} "
                f"I will not call that a success.")


@dataclass(frozen=True)
class Expectation:
    """What should have changed, and what must be legible to judge it."""

    name: str
    goal: str
    fields: tuple
    predicate: object          # (before, after) -> (bool, str)

    def check(self, before: WorldState, after: WorldState,
              delivered: bool = False) -> Verification:
        missing = _unreadable(before, after, self.fields)
        if missing:
            return Verification(
                status=UNVERIFIABLE, goal=self.goal, delivered=delivered,
                missing_fields=missing,
                reason=(f"I cannot read {', '.join(missing)} right now, and "
                        f"that is what would settle it."),
                before=_slim(before, self.fields),
                after=_slim(after, self.fields),
            )

        ok, detail = self.predicate(before, after)
        return Verification(
            status=SUCCESS if ok else FAILED, goal=self.goal,
            delivered=delivered, reason=detail,
            before=_slim(before, self.fields),
            after=_slim(after, self.fields),
        )


def _unreadable(before: WorldState, after: WorldState,
                fields: tuple) -> tuple:
    """Fields that are unknown in either snapshot.

    Both, not just the later one: a change is a comparison, and comparing
    against an unknown starting point is not evidence of anything."""
    missing = []
    for name in fields:
        if (before.confidence_of(name) == UNKNOWN
                or after.confidence_of(name) == UNKNOWN):
            missing.append(name)
    return tuple(missing)


def _slim(state: WorldState, fields: tuple) -> dict:
    """Just the fields under discussion, so a verdict carries its evidence
    without dragging a whole WorldState into every log line."""
    out = {}
    for name in fields:
        value = getattr(state, name, None)
        out[name] = value.as_dict() if hasattr(value, "as_dict") else value
    return out


def _distance(a: tuple, b: tuple) -> float:
    try:
        return math.dist(tuple(a)[:3], tuple(b)[:3])
    except Exception:
        return 0.0


def _block_name(state: WorldState) -> str | None:
    block = state.target_block
    return getattr(block, "name", None) if block else None


# ── The expectations ─────────────────────────────────────────────────────────

def moved(min_distance: float = 0.5) -> Expectation:
    """The player is somewhere else.

    A threshold rather than inequality: F3 positions carry decimals that move
    slightly from sprinting momentum or a mob nudging you, and treating a
    3cm drift as "I walked forward" would make every move look successful."""
    def predicate(before, after):
        moved_by = _distance(before.position, after.position)
        if moved_by >= min_distance:
            return True, f"moved {moved_by:.2f} blocks."
        return False, (f"moved only {moved_by:.2f} blocks, which is less than "
                       f"the {min_distance} that counts as moving.")

    return Expectation(name="moved", goal="move",
                       fields=("position",), predicate=predicate)


def stayed_within(max_distance: float = 0.5) -> Expectation:
    """The player did NOT move — for verifying that a blocked path is blocked
    rather than assuming the walk worked."""
    def predicate(before, after):
        moved_by = _distance(before.position, after.position)
        if moved_by <= max_distance:
            return True, f"stayed put ({moved_by:.2f} blocks)."
        return False, f"moved {moved_by:.2f} blocks, which is further than expected."

    return Expectation(name="stayed_within", goal="stay put",
                       fields=("position",), predicate=predicate)


def turned(min_degrees: float = 5.0) -> Expectation:
    """The camera moved. The check behind "does injected mouse input work"."""
    def predicate(before, after):
        try:
            yaw_before, pitch_before = before.rotation[:2]
            yaw_after, pitch_after = after.rotation[:2]
        except Exception:
            return False, "the rotation values were not a (yaw, pitch) pair."
        # Yaw wraps at ±180, so 179 -> -179 is a 2 degree turn, not 358.
        dyaw = abs((yaw_after - yaw_before + 180.0) % 360.0 - 180.0)
        dpitch = abs(pitch_after - pitch_before)
        turn = max(dyaw, dpitch)
        if turn >= min_degrees:
            return True, f"turned {turn:.1f} degrees."
        return False, (f"turned {turn:.1f} degrees, less than the "
                       f"{min_degrees} that counts as turning.")

    return Expectation(name="turned", goal="look",
                       fields=("rotation",), predicate=predicate)


def block_broken(expected_name: str | None = None) -> Expectation:
    """The block under the crosshair is gone.

    Gone means the target now reads air. A DIFFERENT block also counts —
    breaking a log reveals whatever was behind it — but only when the original
    was what we meant to break.

    Note that a `target_block` of None never reaches the predicate: None means
    the field could not be read, so `Expectation.check` has already returned
    UNVERIFIABLE. "Looking at nothing" arrives as an explicit air BlockRef —
    see DebugOverlayStateSource._build."""
    goal = f"break {expected_name}" if expected_name else "break the block"

    def predicate(before, after):
        was = _block_name(before)
        now = _block_name(after)

        if was is None or was in _AIR_NAMES:
            return False, "there was no block under the crosshair to begin with."
        if expected_name and was != expected_name:
            return False, (f"the block under the crosshair was {was}, not "
                           f"{expected_name}, so nothing was broken as asked.")
        if now in _AIR_NAMES:
            return True, f"{was} is gone — the crosshair now sees nothing."
        if now != was:
            return True, f"{was} is gone — {now} is behind it."
        return False, f"{was} is still there."

    return Expectation(name="block_broken", goal=goal,
                       fields=("target_block",), predicate=predicate)


def looking_at(name: str) -> Expectation:
    """The crosshair is on a particular block. Used to confirm aim BEFORE
    swinging, which is what stops an agent mining the wrong thing."""
    def predicate(before, after):
        now = _block_name(after)
        if now == name:
            return True, f"the crosshair is on {name}."
        if now is None:
            return False, "the crosshair is not on any block."
        return False, f"the crosshair is on {now}, not {name}."

    return Expectation(name="looking_at", goal=f"look at {name}",
                       fields=("target_block",), predicate=predicate)


def target_changed() -> Expectation:
    """Anything at all under the crosshair is different."""
    def predicate(before, after):
        was, now = _block_name(before), _block_name(after)
        if was != now:
            return True, f"the target changed from {was} to {now}."
        return False, f"the target is still {was}."

    return Expectation(name="target_changed", goal="change what I am looking at",
                       fields=("target_block",), predicate=predicate)


def holding_slot(slot: int) -> Expectation:
    """The selected hotbar slot.

    Honest about its own impossibility today: `selected_slot` is not on the F3
    overlay, so with the current sources this always returns UNVERIFIABLE. It
    exists so that hotbar selection is verifiable the moment the mod bridge
    lands, without the task runner changing at all."""
    def predicate(before, after):
        # WorldState.selected_slot is 0-8; the player-facing hotbar is 1-9.
        if after.selected_slot == slot - 1:
            return True, f"slot {slot} is selected."
        return False, (f"slot {after.selected_slot + 1} is selected, "
                       f"not {slot}.")

    return Expectation(name="holding_slot", goal=f"select hotbar slot {slot}",
                       fields=("selected_slot",), predicate=predicate)


def collected(item: str, at_least: int = 1) -> Expectation:
    """More of `item` in the inventory than before.

    The check that was impossible until the bridge existed, and the one that
    matters most: every other way of confirming a mined block watches it
    disappear, which is not the same as picking it up. An item that fell in
    lava, landed out of reach, or despawned was still broken and never
    collected, and only a count can tell those apart."""
    def predicate(before, after):
        was = _count_of(before, item)
        now = _count_of(after, item)
        gained = now - was
        if gained >= at_least:
            return True, f"picked up {gained} {item} ({was} to {now})."
        if gained > 0:
            return False, (f"only picked up {gained} {item}, not "
                           f"{at_least} ({was} to {now}).")
        return False, f"no more {item} than before ({now})."

    return Expectation(name="collected", goal=f"collect {at_least} {item}",
                       fields=("inventory",), predicate=predicate)


def _count_of(state: WorldState, item: str) -> int:
    """How many of `item` the inventory holds, across every stack."""
    total = 0
    for stack in (state.inventory or ()):
        if getattr(stack, "name", None) == item:
            total += int(getattr(stack, "count", 0) or 0)
    return total


def unverifiable(goal: str, reason: str,
                 delivered: bool = False) -> Verification:
    """A verdict for an action with no expectation attached.

    Used by the task runner for actions nothing can check yet, so that the
    absence of a check is recorded as an absence rather than quietly passing
    as a success."""
    return Verification(status=UNVERIFIABLE, goal=goal, reason=reason,
                        delivered=delivered)


__all__ = [
    "Verification", "Expectation",
    "SUCCESS", "FAILED", "UNVERIFIABLE", "STATUSES",
    "moved", "stayed_within", "turned", "block_broken", "looking_at",
    "target_changed", "holding_slot", "collected", "unverifiable",
]
