"""
minecraft/skills/dig.py -- dig_to: a staircase to a cell; mine_ore: a
staircase to the nearest ore it can safely reach, then the vein.

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

mine_ore chooses from the ore scan (minecraft/ores.py: never ore it did not
list, never ore with water or lava beside it), digs to the cell beside it
as dig_to does, then breaks the ore of that vein it can reach from there --
each one under digging.check_break, the same rules -- and steps into open
cells to pick the drops up. What it got is counted from the inventory. A
rule that refuses the way stops it and offers the nearest other ore.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from minecraft import aiming as aiming_mod
from minecraft import digging
from minecraft import mining as mining_mod
from minecraft import ores as ores_mod
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

MAX_ORE_COUNT = 16
MAX_WALKS = 6
PICKUP_REACH = 1.4
"""How far, along x and along z, an item is picked up from the middle of the
cell the player stands in: the body's half-width (0.3) and the game's one
block of pickup range, less a little."""

_DIGS: dict = {}
"""Digs under way, by goal: their digging.Progress, so asking again
carries on with the same counts. In memory only."""

_MINES: dict = {}
"""mine_ore tasks under way, by the ore asked for: a _Mine. In memory
only."""


def forget_digs() -> None:
    _DIGS.clear()
    _MINES.clear()


def mine_under_way(ore) -> str:
    """The mine_ore task for `ore` that asking again carries on, in words;
    "" if there is none."""
    mine = _MINES.get(ores_mod.wanted_kind(ore))
    if mine is None or mine.ore is None:
        return ""
    what = f"{mine.kind} ore" if mine.kind else "ore"
    doing = (f"digging to {_words(mine.stand)} beside it"
             if mine.phase == "dig" and mine.stand is not None
             else "mining its vein")
    return (f"the {what} at {_words(mine.ore)}, {doing} "
            f"({mine.progress.broken} of {digging.MAX_BROKEN} blocks broken "
            f"so far)")


@dataclass
class _Mine:
    """One mine_ore task across the times it is asked: the ore, where to
    stand, the dig's Progress (so the limits count across asks), the
    inventory count it began with, the ore broken so far, and ore passed
    over as unsafe."""
    ore: tuple | None
    stand: tuple | None
    progress: object
    had: int
    kind: str | None
    phase: str = "dig"              # dig | vein
    mined: list = None
    skip: set = None


def _words(cell) -> str:
    return f"({cell[0]}, {cell[1]}, {cell[2]})"


def _say(name) -> str:
    return " ".join(str(name).split("_"))


def _grid_problem(state) -> str:
    near = getattr(state, "near", None)
    if near is None or near.cells is None:
        return ("I cannot see the blocks around and below you -- that "
                "needs the near_blocks grid from a current mod. Quit "
                "Minecraft, run install_mod.bat, then start it again.")
    if state.position is None:
        return "I cannot read where you are."
    return ""


def _goal_limits(goal, start) -> str:
    """R5 for the goal itself, before anything is broken."""
    if start[1] - goal[1] > digging.MAX_DEPTH:
        return (f"R5: {_words(goal)} is more than {digging.MAX_DEPTH} "
                f"blocks below where I started. Nothing was broken.")
    if ((goal[0] - start[0]) ** 2 + (goal[2] - start[2]) ** 2) ** 0.5 \
            > digging.MAX_HORIZONTAL:
        return (f"R5: {_words(goal)} is more than "
                f"{digging.MAX_HORIZONTAL} blocks from where I started. "
                f"Nothing was broken.")
    return ""


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
        problem = _grid_problem(state)
        if problem:
            return problem
        progress = _DIGS.get(goal)
        if progress is None:
            progress = digging.Progress(start=digging.feet_of(state.position))
        self._progress = progress
        problem = _goal_limits(goal, progress.start)
        if problem:
            return problem
        _DIGS[goal] = progress
        return ""

    def _plan(self, state, step_index: int, history: tuple):
        if not self._started:
            problem = self._begin(state)
            if problem:
                return self._stop(problem)
        if self._finished():
            return None
        self._learn(history)
        cells = getattr(getattr(state, "near", None), "cells", None)
        if cells is not None:
            self._settled(self._progress.settle(cells))
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
        return self._next(state, cells, feet)

    def _finished(self) -> bool:
        return self._arrived or bool(self._reason)

    def _settled(self, pairs) -> None:
        self._broke_now += len(pairs)

    def _arrive(self, state, cells, feet):
        """At the goal. dig_to is done; mine_ore goes on to the ore."""
        self._arrived = True
        _DIGS.pop(self._goal, None)
        return None

    def _next(self, state, cells, feet):
        """The next stair's next step, every rule checked on this reading."""
        if self._stair is not None and feet == self._stair.to:
            self._progress.last_direction = self._stair.direction
            self._stair = None
            self._step_tries = 0
        elif self._stair is not None and feet != self._stair_from:
            self._stair = None              # somewhere else: plan afresh

        if self._stair is None:
            plan = digging.plan_next(cells, feet, self._goal, self._progress)
            if plan.status == "done":
                return self._arrive(state, cells, feet)
            if plan.status == "opening":
                return self._stop(self._opening_text(plan.opening))
            if plan.status == "refused":
                return self._refused(f"I stopped digging -- {plan.why}.")
            self._stair, self._stair_from = plan.step, feet

        # Every rule again, on this reading, before anything is broken.
        stair = digging.shape(self._stair_from, self._stair.direction,
                              self._stair.dx, self._stair.dz, self._stair.dy)
        checked, problem = digging.check_step(cells, self._stair_from, stair,
                                              self._progress)
        if checked == "opening":
            return self._stop(self._opening_text(problem))
        if checked is None:
            return self._refused(f"I stopped digging -- "
                                 f"{problem.describe()}.")
        if checked.clear:
            return self._break(state, cells, checked.clear[0])
        return self._step_into(state, checked)

    def _refused(self, reason: str):
        """A hard rule refused the way on. mine_ore offers another ore."""
        return self._stop(reason)

    def account_for(self, state, history) -> None:
        """The runner's last look before it reports: settle the final
        swing, so a run that ends on a mine still counts it -- and asking
        again does not take that cell for a cave."""
        if self._progress is None:
            return
        self._learn(history)
        cells = getattr(getattr(state, "near", None), "cells", None)
        if cells is not None:
            self._settled(self._progress.settle(cells))
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


@dataclass
class MineOre(DigTo):
    """mine_ore: the nearest ore the scan lists that can be reached
    safely, a staircase to beside it, then that vein, `count` blocks."""

    ore: str | None = None
    count: int = 1
    radius: int = ores_mod.DEFAULT_RADIUS

    name = "mine_ore"
    verifiable_with = ("position", "near", "inventory")

    _mine: object = None
    _phase: str = "dig"             # dig | vein | pickup
    _done: bool = False
    _left: dict = None              # vein cells passed over -> why
    _walk: object = None            # a pickup stair under way
    _walk_from: tuple | None = None
    _walks: int = 0                 # stairs walked in the vein or to drops
    _offer: str = ""
    _radius: int = ores_mod.DEFAULT_RADIUS

    # ── what it says ─────────────────────────────────────────────────────

    @property
    def goal(self) -> str:
        return (f"mine {self._count()} {self._what()} block(s) from the "
                f"nearest vein I can reach safely")

    @property
    def failed(self) -> bool:
        return not self._done

    def _count(self) -> int:
        try:
            return max(1, min(int(self.count), MAX_ORE_COUNT))
        except (TypeError, ValueError):
            return 1

    def _kind(self):
        if self._mine is not None and self._mine.kind:
            return self._mine.kind
        return ores_mod.wanted_kind(self.ore)

    def _what(self) -> str:
        kind = self._kind()
        return f"{kind} ore" if kind else "ore"

    def _gained(self) -> str:
        """What the inventory gained, from the last reading: the only
        count of ore got that is reported."""
        if self._mine is None or self._last is None:
            return ""
        names = ores_mod.DROPS.get(self._mine.kind, ())
        if not names:
            return ""
        now = verify_mod._count_of(self._last, frozenset(names))
        gained = now - self._mine.had
        drops = " or ".join(_say(n) for n in names)
        if gained <= 0:
            return (f" Counted from the inventory: no more {drops} than "
                    f"when I started.")
        return (f" Counted from the inventory: {gained} more {drops} than "
                f"when I started.")

    def _report(self) -> str:
        mine = self._mine
        mined = list(mine.mined) if mine is not None else []
        where = ", ".join(_words(c) for c in mined[:6])
        took = (f"I mined {len(mined)} {self._what()} block(s)"
                f"{': ' + where if where else ''}{'...' if len(mined) > 6 else ''}.")
        counts = ""
        if mine is not None:
            counts = (f" I broke {self._broke_now} block(s) this time, "
                      f"{mine.progress.broken} of {digging.MAX_BROKEN} for "
                      f"this task.")
        left = ""
        if self._left:
            first = sorted(self._left.items())[:3]
            left = (f" I left {len(self._left)} more of the vein: "
                    + "; ".join(f"{_words(c)} -- {why}" for c, why in first)
                    + ".")
        lying = self._lying()
        if self._done:
            return f"{took}{self._gained()}{left}{lying}{counts}"
        if self._reason:
            before = (f" Before that: {took}{self._gained()}" if mined
                      else "")
            return f"{self._reason}{self._offer}{before}{counts}"
        return (f"I have not finished -- ask again to carry on. So far: "
                f"{took}{self._gained()}{counts}")

    def _lying(self) -> str:
        if self._last is None or self._mine is None:
            return ""
        far = self._drops_out_of_reach(self._last)
        if not far:
            return ""
        return (f" {len(far)} drop(s) are still on the ground, near "
                f"{_words(digging.feet_of(far[0].position))}: I could not "
                f"walk to them without more digging.")

    # ── choosing the ore ─────────────────────────────────────────────────

    def _begin(self, state):
        self._started = True
        self._swings = {}
        self._left = {}
        problem = ores_mod.refusal(state) or _grid_problem(state)
        if problem:
            return f"{problem} Nothing was broken."
        try:
            self._radius = int(self.radius)
        except (TypeError, ValueError):
            self._radius = ores_mod.DEFAULT_RADIUS
        radius = self._radius
        wanted = ores_mod.wanted_kind(self.ore)
        feet = digging.feet_of(state.position)
        mine = _MINES.get(wanted)
        if mine is None or mine.ore is None:
            skip = mine.skip if mine is not None else set()
            under_way = mine.progress if mine is not None else None
            choice, passed = ores_mod.choose(
                state, wanted, radius, skip=skip,
                within_reach=aiming_mod.SHARED.within_reach,
                start=under_way.start if under_way else None,
                broken=under_way.broken if under_way else 0)
            if choice is None:
                broke = (" Nothing was broken." if mine is None
                         or not mine.progress.broken else "")
                return (ores_mod.none_chosen(state, wanted, radius, passed,
                                             skip) + broke)
            if mine is None:
                mine = _Mine(ore=None, stand=None,
                             progress=digging.Progress(start=feet),
                             had=0, kind=None, mined=[], skip=set())
            mine.ore, mine.stand = choice.hit.position, choice.stand
            kind = ores_mod.kind_of(choice.hit.name)
            if kind != mine.kind:           # new, or "any ore" chose anew
                mine.kind, mine.mined = kind, []
                mine.had = verify_mod._count_of(
                    state, frozenset(ores_mod.DROPS.get(kind, ())))
            mine.phase = "dig" if choice.stand is not None else "vein"
        self._mine = mine
        self._progress = mine.progress
        _MINES[wanted] = mine
        self._phase = mine.phase
        if mine.stand is None:
            self._goal = feet
            return ""
        self.x, self.y, self.z = mine.stand
        self._goal = tuple(mine.stand)
        return _goal_limits(self._goal, mine.progress.start)

    def _finished(self) -> bool:
        return self._done or bool(self._reason)

    def _settled(self, pairs) -> None:
        super()._settled(pairs)
        for cell, was in pairs:
            if str(was).endswith("_ore") \
                    and ores_mod.kind_of(was) == self._mine.kind \
                    and cell not in self._mine.mined:
                self._mine.mined.append(cell)

    def _arrive(self, state, cells, feet):
        """Beside the ore: on to the vein."""
        self._phase = self._mine.phase = "vein"
        return self._next(state, cells, feet)

    def _refused(self, reason: str):
        """The way to this ore is refused: pass over its whole vein, and
        offer the nearest other ore the scan lists that is safe."""
        mine, state = self._mine, self._last
        if mine is not None and mine.ore is not None and state is not None:
            mine.skip |= ores_mod.vein_of(state, mine.ore)
            mine.ore = mine.stand = None
            mine.phase = "dig"
            other, _passed = ores_mod.choose(
                state, ores_mod.wanted_kind(self.ore), self._radius,
                skip=mine.skip, within_reach=aiming_mod.SHARED.within_reach,
                start=mine.progress.start, broken=mine.progress.broken)
            if other is not None:
                self._offer = (f" The nearest other {self._what()} I could "
                               f"go for is {other.describe()} -- ask me "
                               f"again and I will try that one.")
            else:
                self._offer = (f" There is no other {self._what()} in the "
                               f"scan I can safely reach, so I am leaving "
                               f"it.")
        return self._stop(reason)

    def _complete(self):
        self._done = True
        _MINES.pop(ores_mod.wanted_kind(self.ore), None)
        return None

    # ── the vein ─────────────────────────────────────────────────────────

    def _next(self, state, cells, feet):
        if self._phase == "dig":
            return super()._next(state, cells, feet)
        step = self._walking(state, cells, feet)
        if step is not None:
            return step
        if self._phase == "vein":
            step = self._vein(state, cells, feet)
            if step is not None or self._finished():
                return step
            self._phase = "pickup"
        return self._pickup(state, cells, feet)

    def _vein_cells(self, cells) -> list:
        """The ore of this vein the grid shows, joined face to face to the
        ore chosen or to one broken since."""
        mine = self._mine
        seeds = [tuple(mine.ore)] + [tuple(c) for c in mine.mined]
        seen, found, todo = set(), [], list(seeds)
        while todo:
            cell = todo.pop()
            if cell in seen:
                continue
            seen.add(cell)
            entry = cells.get(cell)
            if entry is None:
                continue
            ore = (str(entry[0]).endswith("_ore")
                   and ores_mod.kind_of(entry[0]) == mine.kind)
            if ore:
                found.append(cell)
            elif cell not in seeds:
                continue
            for d in digging.SIDES:
                todo.append((cell[0] + d[0], cell[1] + d[1], cell[2] + d[2]))
        return found

    def _vein(self, state, cells, feet):
        """The next ore of the vein: the nearest every rule allows that is
        in reach and in sight. If the rest is out of sight, one stair into
        a cell this task mined, towards it."""
        if len(self._mine.mined) >= self._count():
            return None
        eye = (state.position[0], state.position[1] + 1.62,
               state.position[2])

        def distance(cell):
            return sum((e - (c + 0.5)) ** 2 for e, c in zip(eye, cell))

        hidden = []
        for cell in sorted(self._vein_cells(cells),
                           key=lambda c: (distance(c), c)):
            refusal = digging.check_break(cells, feet, cell, self._progress)
            if refusal is not None:
                self._left[cell] = refusal.describe()
                continue
            face = self._face(cells, cell, feet)
            if face is None \
                    or not aiming_mod.SHARED.within_reach(state.position,
                                                          cell) \
                    or not _in_sight(cells, state.position, cell, face):
                self._left[cell] = "out of reach or out of sight from " \
                                   "where I could stand"
                hidden.append(cell)
                continue
            self._left.pop(cell, None)
            return self._break(state, cells, cell)
        if hidden:
            mined = self._progress.cleared

            def towards(to):
                return to in mined and any(
                    sum(abs(a - b) for a, b in zip(to, cell)) == 1
                    for cell in hidden)
            step = self._walk_to(state, cells, feet, towards)
            if step is not None:
                return step
        ore = tuple(self._mine.ore)
        if not self._mine.mined and ore in self._left:
            return self._refused(f"I stopped beside the {self._what()} at "
                                 f"{_words(ore)} without breaking it -- "
                                 f"{self._left[ore]}.")
        return None

    # ── walking: into the vein's hole, and to the drops ─────────────────

    def _walk_to(self, state, cells, feet, wanted):
        """One level stair to a cell `wanted` accepts, the one that breaks
        least: a walk into open air, or a stair with a head cell to clear.
        None if there is none, or after MAX_WALKS."""
        if self._walks >= MAX_WALKS:
            return None
        best = None
        for order, (name, dx, dz) in enumerate(digging.DIRECTIONS):
            step = digging.shape(feet, name, dx, dz, 0)
            if not wanted(step.to):
                continue
            checked = self._walkable(cells, feet, step)
            if checked is None:
                continue
            rank = (len(checked.clear), order)
            if best is None or rank < best[0]:
                best = (rank, checked)
        if best is None:
            return None
        self._walk, self._walk_from = best[1], feet
        return self._walking(state, cells, feet)

    def _walking(self, state, cells, feet):
        """The walk under way, re-checked on this reading: its next break or
        its step. None when there is none, or it is over."""
        if self._walk is None:
            return None
        if feet == self._walk.to:
            self._walk, self._step_tries = None, 0
            self._walks += 1
            return None
        if feet != self._walk_from:
            self._walk = None
            return None
        checked = self._walkable(cells, feet, self._walk)
        if checked is None:
            self._walk = None
            return None
        if checked.clear:
            return self._break(state, cells, checked.clear[0])
        return self._step_into(state, checked)

    def _walkable(self, cells, feet, step):
        """The stair checked for a walk: dug as a stair (one head cell at
        most), or walked into air already there. None if neither."""
        checked, _problem = digging.check_step(cells, feet, step,
                                               self._progress)
        if checked == "opening" or checked is None:
            if digging.check_walk(cells, feet, step) is None:
                return step
            return None
        if len(checked.clear) > 1:
            return None
        return checked

    # ── picking the drops up ─────────────────────────────────────────────

    def _ore_drops(self, state) -> list:
        names = set(ores_mod.DROPS.get(self._mine.kind, ()))
        here = state.position
        found = []
        for entity in (state.nearby_entities or ()):
            item = getattr(entity, "item", None)
            where = getattr(entity, "position", None)
            if item is None or where is None or item.name not in names:
                continue
            if abs(where[0] - here[0]) > 5 or abs(where[2] - here[2]) > 5 \
                    or abs(where[1] - here[1]) > 3:
                continue
            found.append(entity)
        return found

    @staticmethod
    def _picks_up(feet, where) -> bool:
        return (abs(where[0] - (feet[0] + 0.5)) <= PICKUP_REACH
                and abs(where[2] - (feet[2] + 0.5)) <= PICKUP_REACH
                and abs(where[1] - feet[1]) <= 2.0)

    def _drops_out_of_reach(self, state) -> list:
        if state.position is None:
            return []
        feet = digging.feet_of(state.position)
        return [d for d in self._ore_drops(state)
                if not self._picks_up(feet, d.position)]

    def _pickup(self, state, cells, feet):
        far = self._drops_out_of_reach(state)
        if far:
            step = self._walk_to(
                state, cells, feet,
                lambda to: any(self._picks_up(to, d.position) for d in far))
            if step is not None:
                return step
        return self._complete()


def _in_sight(cells, position, cell, face) -> bool:
    """Nothing but air between the eyes and the point aimed at on `cell`'s
    `face`, by the cells the grid reports: an unknown cell blocks."""
    eye = (position[0], position[1] + aiming_mod.EYE_HEIGHT, position[2])
    point = aiming_mod.target_point(cell, position, face)
    length = sum((p - e) ** 2 for p, e in zip(point, eye)) ** 0.5
    count = max(1, int(length / 0.05))
    for i in range(1, count + 1):
        t = i / count
        at = tuple(int(math.floor(e + (p - e) * t))
                   for e, p in zip(eye, point))
        if at == tuple(cell):
            return True
        if not digging.is_air(cells.get(at)):
            return False
    return True


__all__ = ["DigTo", "MineOre", "forget_digs", "mine_under_way"]
