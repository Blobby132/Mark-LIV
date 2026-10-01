"""
minecraft/skills/aim.py -- aim_at_block, and mine_block: put the crosshair
on one exact block (and break it). Moved here unchanged from collect.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from minecraft import navigation as nav, verification as verify_mod
from minecraft import stuck as stuck_mod
from minecraft import aiming as aiming_mod
from minecraft import mining as mining_mod
from minecraft.state import EXACT, UNKNOWN
from minecraft.task_runner import Step

from minecraft.skills.base import _crosshair_at

from minecraft.skills.collect import _HoldsTheRightTool
from minecraft.skills.collect_logs import CollectLogs


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
