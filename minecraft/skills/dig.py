"""
minecraft/skills/dig.py -- dig_to: a staircase to a cell.

Built on the gathering machinery (the best tool before each swing, the
crosshair-confirmed mine, the hotbar slot put back afterwards) and on
minecraft/digging.py, which says where a stair may go. For every cell:

  re-observe -> re-check every hard rule -> take up the best pickaxe and
  verify it -> aim at the cell -> break it (the controller presses only if
  the crosshair is on that cell) -> the cell is air in the next reading

and when a stair's cells are clear: face the way and step, and the feet are
in the expected cell in the next reading. Drops are picked up by walking
through them. Between every step the reading is compared with the last one
(digging.abort_reason, R8): health lost, fluid near, a cave-in or a fall
stops it at once. The runner's danger watch is never stood down.

Single-player only, like the ore finder; refused without the near_blocks
grid. A task has 45 steps; asking again with the same goal carries on from
where it stopped, with the same counts against the limits.
"""

from __future__ import annotations

from dataclasses import dataclass

from minecraft import digging
from minecraft import mining as mining_mod
from minecraft import navigation as nav
from minecraft import verification as verify_mod
from minecraft.skills.base import _crosshair_at
from minecraft.skills.collect import _Gatherer, _HoldsTheRightTool
from minecraft.task_runner import Step

YAW_FOR = {"south": 0.0, "west": 90.0, "north": 180.0, "east": -90.0}
"""Minecraft's yaw for facing each way: south is +z, east is +x."""

FACE_TOWARDS = {(1, 0, 0): "east", (-1, 0, 0): "west", (0, 1, 0): "up",
                (0, -1, 0): "down", (0, 0, 1): "south", (0, 0, -1): "north"}

MAX_AIM_STEPS = 4
MAX_SWINGS_PER_CELL = 3
MAX_STEP_TRIES = 3
FACE_FIRST_DEGREES = 12.0

_DIGS: dict = {}
"""Digs under way, by goal: their digging.Progress, so asking again
carries on with the same counts. In memory only."""


def forget_digs() -> None:
    _DIGS.clear()


def _words(cell) -> str:
    return f"({cell[0]}, {cell[1]}, {cell[2]})"


def _say(name) -> str:
    return " ".join(str(name).split("_"))


@dataclass
class DigTo(_HoldsTheRightTool):
    """dig_to: dig a staircase until the feet are at (x, y, z)."""

    x: int | None = None
    y: int | None = None
    z: int | None = None

    name = "dig_to"
    verifiable_with = ("position", "near")

    _goal: tuple | None = None
    _progress: object = None
    _stair: object = None           # digging.Step being dug
    _stair_from: tuple | None = None
    _last: object = None            # the previous reading, for R8
    _expect: tuple | None = None    # where the feet should be now
    _aim_cell: tuple | None = None
    _aim_tries: int = 0
    _step_tries: int = 0
    _swings: dict = None
    _broke_now: int = 0
    _reason: str = ""
    _arrived: bool = False
    _opening: dict | None = None
    _started: bool = False

    # ── what it says ─────────────────────────────────────────────────────

    @property
    def goal(self) -> str:
        where = (self.x, self.y, self.z)
        return f"dig a staircase to {_words(where)}"

    @property
    def failed(self) -> bool:
        return not self._arrived

    @property
    def done_reason(self) -> str:
        return self._with_fetch(self._report())

    def _report(self) -> str:
        done = self._progress
        counts = ""
        if done is not None:
            counts = (f" I broke {self._broke_now} block(s) this time, "
                      f"{done.broken} of {digging.MAX_BROKEN} for this dig")
            if self._last is not None:
                feet = digging.feet_of(self._last.position)
                counts += (f", and I am at {_words(feet)}, "
                           f"{digging.steps_left(feet, self._goal)} stair(s) "
                           f"from {_words(self._goal)}")
            counts += "."
        if self._arrived:
            return f"I dug down to {_words(self._goal)}.{counts}"
        if self._reason:
            return f"{self._reason}{counts}"
        return (f"I have not reached "
                f"{_words(self._goal or (self.x, self.y, self.z))} yet -- ask "
                f"again to carry on.{counts}")

    # ── the loop ─────────────────────────────────────────────────────────

    def _stop(self, reason: str):
        self._reason = reason
        return None

    def _begin(self, state):
        self._started = True
        self._swings = {}
        try:
            goal = (int(self.x), int(self.y), int(self.z))
        except (TypeError, ValueError):
            return "dig_to needs x, y and z: the cell to stand in at the end."
        self._goal = goal
        if getattr(state, "singleplayer", None) is not True:
            return ("I only dig in a single-player world, and this one is "
                    "not, or the installed mod does not say. Nothing was "
                    "broken.")
        near = getattr(state, "near", None)
        if near is None or near.cells is None:
            return ("I cannot see the blocks around and below you -- that "
                    "needs the near_blocks grid from a current mod. Quit "
                    "Minecraft, run install_mod.bat, then start it again.")
        if state.position is None:
            return "I cannot read where you are."
        feet = digging.feet_of(state.position)
        progress = _DIGS.get(goal)
        if progress is None:
            progress = digging.Progress(start=feet)
        self._progress = progress
        start = progress.start
        if start[1] - goal[1] > digging.MAX_DEPTH:
            return (f"R5: {_words(goal)} is more than {digging.MAX_DEPTH} "
                    f"blocks below where I started. Nothing was broken.")
        if ((goal[0] - start[0]) ** 2 + (goal[2] - start[2]) ** 2) ** 0.5 \
                > digging.MAX_HORIZONTAL:
            return (f"R5: {_words(goal)} is more than "
                    f"{digging.MAX_HORIZONTAL} blocks from where I started. "
                    f"Nothing was broken.")
        _DIGS[goal] = progress
        return ""

    def _plan(self, state, step_index: int, history: tuple):
        if not self._started:
            problem = self._begin(state)
            if problem:
                return self._stop(problem)
        if self._arrived or self._reason:
            return None
        self._learn(history)
        cells = getattr(getattr(state, "near", None), "cells", None)
        if cells is not None:
            self._broke_now += self._progress.settle(cells)
        if self._last is not None:
            why = digging.abort_reason(self._last, state, self._progress,
                                       self._expect)
            if why:
                return self._stop(f"I stopped digging: {why}.")
        self._last = state
        cells = getattr(getattr(state, "near", None), "cells", None)
        if cells is None or state.position is None:
            return self._stop("I lost sight of the blocks around me, so I "
                              "stopped digging.")
        feet = digging.feet_of(state.position)
        self._expect = feet

        if self._stair is not None and feet == self._stair.to:
            self._progress.last_direction = self._stair.direction
            self._stair = None
            self._step_tries = 0
        elif self._stair is not None and feet != self._stair_from:
            self._stair = None              # somewhere else: plan afresh

        if self._stair is None:
            plan = digging.plan_next(cells, feet, self._goal, self._progress)
            if plan.status == "done":
                self._arrived = True
                _DIGS.pop(self._goal, None)
                return None
            if plan.status == "opening":
                return self._stop(self._opening_text(plan.opening))
            if plan.status == "refused":
                return self._stop(f"I stopped digging -- {plan.why}.")
            self._stair, self._stair_from = plan.step, feet

        # Every rule again, on this reading, before anything is broken.
        stair = digging.shape(self._stair_from, self._stair.direction,
                              self._stair.dx, self._stair.dz, self._stair.dy)
        checked, problem = digging.check_step(cells, self._stair_from, stair,
                                              self._progress)
        if checked == "opening":
            return self._stop(self._opening_text(problem))
        if checked is None:
            return self._stop(f"I stopped digging -- {problem.describe()}.")
        if checked.clear:
            return self._break(state, cells, checked.clear[0])
        return self._step_into(state, checked)

    def account_for(self, state, history) -> None:
        """The runner's last look before it reports: settle the final
        swing, so a run that ends on a mine still counts it -- and asking
        again does not take that cell for a cave."""
        if self._progress is None:
            return
        self._learn(history)
        cells = getattr(getattr(state, "near", None), "cells", None)
        if cells is not None:
            self._broke_now += self._progress.settle(cells)
        self._last = state

    def _learn(self, history) -> None:
        """Note a swing that did not clear its cell. Counting what broke is
        Progress.settle's, from the reading itself."""
        if not history:
            return
        last = history[-1]
        if last.step.get("action") != "mine":
            return
        where = (last.step.get("params") or {}).get("expect_at")
        if not where:
            return
        cell = tuple(int(v) for v in where)
        if last.verification.get("status") == verify_mod.SUCCESS:
            self._aim_cell = None
        else:
            self._swings[cell] = self._swings.get(cell, 0) + 1

    def _break(self, state, cells, cell):
        name = cells[cell][0]
        if self._swings.get(cell, 0) >= MAX_SWINGS_PER_CELL:
            return self._stop(f"I swung at the {_say(name)} at {_words(cell)} "
                              f"{self._swings[cell]} times and it is still "
                              f"there, so I stopped.")
        ready = self._tool_for(state, name)
        if isinstance(ready, str):
            return self._stop(f"I stopped digging: {ready}")
        if ready is not None:
            return ready
        choice = mining_mod.best_hotbar_tool(state, name)
        if not choice.harvests:
            return self._stop(
                f"I stopped digging: nothing in the hotbar can harvest the "
                f"{_say(name)} at {_words(cell)} -- it needs "
                f"{mining_mod.tool_needed(name)}.")
        seen = _crosshair_at(state)
        if seen is not None and tuple(seen[0]) == cell:
            estimate = mining_mod.estimate_break_duration(name, state=state)
            self._progress.swung[cell] = name
            return Step(
                action="mine",
                params={"duration": _Gatherer._mine_seconds(estimate),
                        "expect_at": list(cell)},
                expectation=verify_mod.cell_cleared(cell, name),
                note=(f"dig {_say(name)} at {_words(cell)} "
                      f"({self._progress.broken} of {digging.MAX_BROKEN}; "
                      f"{estimate.describe()})"))
        if cell != self._aim_cell:
            self._aim_cell, self._aim_tries = cell, 0
        if self._aim_tries >= MAX_AIM_STEPS:
            on = seen[1] if seen else "nothing"
            return self._stop(f"I could not put the crosshair on the "
                              f"{_say(name)} at {_words(cell)} (it is on "
                              f"{_say(on)}), so I stopped rather than break "
                              f"something else.")
        self._aim_tries += 1
        face = self._face(cells, cell, digging.feet_of(state.position))
        dx, dy, error = nav.aim_at(state.position, state.rotation, cell,
                                   face=face)
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.5),
                    note=(f"aim at {_say(name)} {_words(cell)}, {face} face "
                          f"({error:.0f} degrees off)"))

    @staticmethod
    def _face(cells, cell, feet):
        """The face of `cell` to aim at: open to the air, turned towards
        the player's eyes."""
        eye = (feet[0] + 0.5, feet[1] + 1.62, feet[2] + 0.5)
        centre = (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)
        best, score = None, None
        for normal, name in FACE_TOWARDS.items():
            side = (cell[0] + normal[0], cell[1] + normal[1],
                    cell[2] + normal[2])
            if not digging.is_air(cells.get(side)):
                continue
            towards = sum(n * (e - c) for n, e, c in zip(normal, eye, centre))
            if score is None or towards > score:
                best, score = name, towards
        return best

    def _step_into(self, state, checked):
        to = checked.to
        self._step_tries += 1
        if self._step_tries > MAX_STEP_TRIES:
            return self._stop(f"I could not step into {_words(to)} after "
                              f"{MAX_STEP_TRIES} tries, so I stopped.")
        yaw = (state.rotation or (0.0, 0.0))[0] or 0.0
        want = YAW_FOR[checked.direction]
        off = nav.yaw_difference(yaw, want)
        if abs(off) > FACE_FIRST_DEGREES:
            return Step(action="look",
                        params={"dx": nav.look_delta_for(yaw, want),
                                "dy": 0},
                        expectation=verify_mod.turned(min_degrees=1.0),
                        note=f"face {checked.direction} to step")
        here = state.position
        gap = ((to[0] + 0.5 - here[0]) ** 2
               + (to[2] + 0.5 - here[2]) ** 2) ** 0.5
        self._expect = to
        action = "move_and_jump" if checked.dy > 0 else "move"
        down = " and down" if checked.dy < 0 else ""
        up = " and up" if checked.dy > 0 else ""
        return Step(action=action,
                    params={"direction": "forward",
                            "duration": round(max(0.15, gap / 4.3), 2)},
                    expectation=verify_mod.in_cell(to),
                    note=f"step {checked.direction}{down}{up} into "
                         f"{_words(to)}")

    def _opening_text(self, opening) -> str:
        at = opening["at"]
        if opening["walkable"]:
            ground = ("its floor is solid, a drop of "
                      f"{opening['drop']}" if opening["drop"]
                      else "its floor is solid and level")
            return (f"I stopped digging: the tunnel opened into a cave at "
                    f"{_words(at)}; {ground}, so it can be walked from here.")
        drop = ("a drop I cannot see the bottom of" if opening["drop"] is None
                else f"a drop of {opening['drop']}")
        return (f"I stopped digging: the tunnel opened into a cave at "
                f"{_words(at)}, with {drop} -- I will not walk into it.")


__all__ = ["DigTo", "forget_digs"]
