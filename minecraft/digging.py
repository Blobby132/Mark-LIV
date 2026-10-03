"""
minecraft/digging.py -- where a staircase may be dug, cell by cell.

Pure logic: it reads the cells the bridge reported around the player (the
`near_blocks` grid: every cell from five below the feet to three above,
fluids named) and returns the next step of a staircase -- the one to three
cells to clear, the cell to step into, and the floor that must stay -- or
a refusal naming the rule that stopped it. It presses nothing and keeps
nothing; the skill re-observes before every break and asks again.

THE CELLS ARE THE ONLY TRUTH
    A cell the bridge did not report is unknown, and unknown is never
    assumed to be stone: a cell to break, a floor to stand on, or a
    neighbour to check that is not in the grid refuses the step.

THE HARD RULES (each a function below, each with its own test, and a test
proving the dangerous step is taken if that one function is removed)
    R1  Break only natural, dig-through blocks (DIGGABLE, or an ore).
    R2  Never break a cell with water or lava among its six neighbours, or
        in it; never step into fluid. Every neighbour must be known.
    R3  Never break a cell with sand, gravel, concrete powder, an anvil or
        a dragon egg (FALLING) directly above the column being cleared.
    R4  Never the block underfoot, never the floor of the next step, never
        straight down or straight up: a staircase, at most one down (or
        up) per one forward, onto a known solid floor. The floor, and every
        cell the body goes through, is no hazard (HAZARDS, shared with the
        pathfinder in minecraft/blocks.py: magma, a campfire, powder snow,
        a cobweb ...), and no cactus is beside a cell the body goes
        through.
    R5  At most MAX_BROKEN blocks a task, MAX_DEPTH below where it began,
        MAX_HORIZONTAL from it (the runner's 45 steps a run besides).
    R6  When the tunnel opens into air it did not dig (a cave), stop there
        and report the opening: whether its floor is solid and how far the
        drop is.
    R7  No planned cell within LAVA_CLEARANCE of lava; a cell within it
        that is inside the grid's box but not reported (an unloaded chunk)
        refuses too. What lies outside the box -- four and five above the
        feet, since the grid reaches three; beyond four sideways, around a
        vein's ore further off -- is out of its view and not refused. Lava
        there cannot reach the dig without passing through a cell the grid
        does show: R2 has every neighbour of every cell broken known and
        dry, and lava beside air flows into it, into view.
    R8  Abort on health lost, fluid within 2 cells, a block appearing in a
        cleared cell, or the player's position changing unexpectedly
        (abort_reason). Hostile mobs are the task runner's danger watch,
        which digging never stands down; a pickaxe that cannot harvest the
        block is the skill's check before each swing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft.blocks import CONTACT_HAZARDS, HAZARDS

MAX_BROKEN = 40
MAX_DEPTH = 16
MAX_HORIZONTAL = 32
LAVA_CLEARANCE = 3
FLUID_ABORT_REACH = 2
MAX_SAFE_DROP = 3

DIGGABLE = frozenset({
    "stone", "deepslate", "tuff", "andesite", "diorite", "granite", "dirt",
    "grass_block", "coarse_dirt", "netherrack", "sandstone", "calcite",
})
"""Natural blocks a staircase may go through. Ores besides (any *_ore).
Anything else -- planks, bricks, glass, wool, containers, beds, doors,
rails, torches, signs, spawners, obsidian, bedrock, reinforced deepslate,
budding amethyst, suspicious sand and gravel, TNT, cobblestone -- is
refused: it is someone's build, a trap, or not worth the risk."""

FALLING = frozenset({
    "sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel",
    "anvil", "chipped_anvil", "damaged_anvil", "dragon_egg",
})
"""Blocks that fall when the block under them goes. Concrete powder too
(any *_concrete_powder)."""

DIRECTIONS = (("east", 1, 0), ("west", -1, 0), ("south", 0, 1),
              ("north", 0, -1))
SIDES = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1),
         (0, 0, -1))


# ── what a cell is ───────────────────────────────────────────────────────────

def is_diggable(name) -> bool:
    return name in DIGGABLE or str(name).endswith("_ore")


def is_falling(name) -> bool:
    return name in FALLING or str(name).endswith("_concrete_powder")


def has_fluid(entry) -> bool:
    """Water or lava in this cell, source or flowing, or a waterlogged
    block."""
    if entry is None:
        return False
    name, _solid, fluid = entry
    return fluid is not None or name in ("water", "lava")


def is_lava(entry) -> bool:
    if entry is None:
        return False
    name, _solid, fluid = entry
    return name == "lava" or fluid in ("lava", "flowing_lava")


def is_air(entry) -> bool:
    return entry is not None and entry[0] in ("air", "cave_air",
                                              "void_air") \
        and entry[2] is None


def _offset(cell, dx, dy, dz):
    return (cell[0] + dx, cell[1] + dy, cell[2] + dz)


def _words(cell) -> str:
    return f"({cell[0]}, {cell[1]}, {cell[2]})"


# ── the plan ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Refusal:
    rule: str                       # "R1".."R8", or "unknown"
    cell: tuple | None
    why: str

    def describe(self) -> str:
        return f"{self.rule}: {self.why}"


@dataclass(frozen=True)
class Step:
    """One stair: walk `direction`, `dy` down (-1), level (0) or up (1)."""
    direction: str
    dx: int
    dz: int
    dy: int
    passes: tuple                   # every cell the body goes through
    clear: tuple                    # those to break, top first
    to: tuple                       # the feet cell after the step
    floor: tuple                    # what it stands on then: never broken


@dataclass(frozen=True)
class Plan:
    status: str                     # dig | walk | done | refused | opening
    why: str
    step: Step | None = None
    refusal: Refusal | None = None
    opening: dict | None = None

    @property
    def cells(self) -> tuple:
        return self.step.clear if self.step is not None else ()


@dataclass
class Progress:
    """What one dig has done so far; the skill keeps it."""
    start: tuple                    # feet cell where it began
    broken: int = 0
    cleared: set = field(default_factory=set)
    last_direction: str | None = None
    # Cells swung at and not yet seen to go: cell -> the block that was
    # there. A later reading settles each one -- air: cleared and counted;
    # a different block: something fell in (R8).
    swung: dict = field(default_factory=dict)

    def settle(self, cells) -> list:
        """Move swung cells the reading shows as air into `cleared`, and
        count them. Returns [(cell, the block it was)] for those."""
        settled = []
        for cell in list(self.swung):
            if is_air(cells.get(cell)):
                was = self.swung.pop(cell)
                if cell not in self.cleared:
                    self.cleared.add(cell)
                    self.broken += 1
                    settled.append((cell, was))
        return settled


def feet_of(position) -> tuple:
    return (math.floor(position[0]), math.floor(position[1]),
            math.floor(position[2]))


def shape(feet, direction, dx, dz, dy) -> Step:
    """The cells one stair passes through, the floor it lands on."""
    x, y, z = feet
    nx, nz = x + dx, z + dz
    if dy < 0:          # down: head, feet and the cell below, then drop
        passes = ((nx, y + 1, nz), (nx, y, nz), (nx, y - 1, nz))
        to, floor = (nx, y - 1, nz), (nx, y - 2, nz)
    elif dy > 0:        # up: room to jump, then the two in front a step up
        passes = ((x, y + 2, z), (nx, y + 2, nz), (nx, y + 1, nz))
        to, floor = (nx, y + 1, nz), (nx, y, nz)
    else:
        passes = ((nx, y + 1, nz), (nx, y, nz))
        to, floor = (nx, y, nz), (nx, y - 1, nz)
    return Step(direction=direction, dx=dx, dz=dz, dy=dy, passes=passes,
                clear=(), to=to, floor=floor)


def steps_left(feet, goal) -> int:
    """Stairs from `feet` to `goal`: each moves one across and at most one
    up or down, so a drop steeper than the run needs extra turns."""
    across = abs(goal[0] - feet[0]) + abs(goal[2] - feet[2])
    down = abs(goal[1] - feet[1])
    if down <= across:
        return across
    return down + (down - across) % 2


# ── the rules ────────────────────────────────────────────────────────────────

def _r1_diggable(cells, cell):
    name = cells[cell][0]
    if not is_diggable(name):
        return Refusal("R1", cell, f"{' '.join(name.split('_'))} at "
                       f"{_words(cell)} is not natural stone, earth or ore, "
                       f"and I only dig through those")
    return None


def _r2_fluid(cells, cell, breaking=True):
    """Never into fluid: the cell itself is not water or lava. And when it
    is to be broken, its six neighbours are known and none is fluid."""
    if has_fluid(cells.get(cell)):
        return Refusal("R2", cell, f"{_words(cell)} is water or lava; I "
                       f"never dig or step into either")
    if not breaking:
        return None
    for d in SIDES:
        side = _offset(cell, *d)
        if side not in cells:
            return Refusal("R2", cell, f"I cannot see {_words(side)}, next "
                           f"to {_words(cell)}, so I cannot rule out water "
                           f"or lava there")
        if has_fluid(cells[side]):
            return Refusal("R2", cell, f"{_words(cell)} has "
                           f"{cells[side][2] or cells[side][0]} beside it at "
                           f"{_words(side)}; breaking it would let it in")
    return None


def _r3_falling(cells, cell, clearing):
    """What sits on top of the column being cleared must not fall."""
    above = _offset(cell, 0, 1, 0)
    if above in clearing:
        return None                 # cleared too, top first: checked there
    if above not in cells:
        return Refusal("R3", cell, f"I cannot see what is above "
                       f"{_words(cell)}, so I cannot rule out sand or gravel "
                       f"falling in")
    name = cells[above][0]
    if is_falling(name):
        return Refusal("R3", cell, f"{' '.join(name.split('_'))} above "
                       f"{_words(cell)} would fall when it goes")
    return None


def _r4_staircase(step, feet, cells):
    """Never underfoot, never the next floor, never straight down or up;
    the floor stepped onto known, solid and dry."""
    underfoot = _offset(feet, 0, -1, 0)
    if underfoot in step.passes or step.floor in step.passes:
        return Refusal("R4", underfoot, "that would break the block I "
                       "stand on, or the one I would land on")
    if step.dx == 0 and step.dz == 0:
        return Refusal("R4", None, "I never dig straight down or up")
    floor = cells.get(step.floor)
    if floor is None:
        return Refusal("R4", step.floor, f"I cannot see the floor at "
                       f"{_words(step.floor)}, so I will not step there")
    if not floor[1] or has_fluid(floor):
        return Refusal("R4", step.floor, f"the floor at "
                       f"{_words(step.floor)} is "
                       f"{' '.join(floor[0].split('_'))}, not solid ground: "
                       f"I would fall")
    return None


def _r4_hazards(cells, step):
    """The floor stepped onto and every cell the body goes through are no
    hazard, and nothing that hurts to touch is beside those cells. Unknown
    cells are another rule's: this judges only what was reported."""
    for cell in step.passes:
        entry = cells.get(cell)
        if entry is not None and entry[0] in HAZARDS:
            return Refusal("R4", cell, f"{_words(cell)} is "
                           f"{' '.join(entry[0].split('_'))}, a hazard: I "
                           f"will not dig or walk into it")
    floor = cells.get(step.floor)
    if floor is not None and floor[0] in HAZARDS:
        return Refusal("R4", step.floor, f"the floor at "
                       f"{_words(step.floor)} is "
                       f"{' '.join(floor[0].split('_'))}, a hazard to stand "
                       f"on")
    for cell in step.passes:
        for d in SIDES:
            side = _offset(cell, *d)
            entry = cells.get(side)
            if entry is not None and entry[0] in CONTACT_HAZARDS:
                return Refusal("R4", side, f"a "
                               f"{' '.join(entry[0].split('_'))} at "
                               f"{_words(side)} is beside {_words(cell)}: it "
                               f"hurts to brush against")
    return None


def _r5_limits(step, progress, breaking):
    start = progress.start
    if progress.broken + breaking > MAX_BROKEN:
        return Refusal("R5", None, f"that would be more than {MAX_BROKEN} "
                       f"blocks broken in one task")
    if start[1] - step.to[1] > MAX_DEPTH:
        return Refusal("R5", step.to, f"that is more than {MAX_DEPTH} "
                       f"blocks below where I started")
    if math.hypot(step.to[0] - start[0], step.to[2] - start[2]) \
            > MAX_HORIZONTAL:
        return Refusal("R5", step.to, f"that is more than {MAX_HORIZONTAL} "
                       f"blocks from where I started")
    return None


def _r6_opening(cells, step, progress):
    """Air this dig did not make, once it has dug: a cave. Stop there."""
    if progress.broken == 0:
        return None
    found = [c for c in step.passes
             if is_air(cells.get(c)) and c not in progress.cleared]
    if not found:
        return None
    drop, floor_solid = 0, False
    below = _offset(step.to, 0, -1, 0)
    while drop <= MAX_SAFE_DROP + 1:
        entry = cells.get(below)
        if entry is None:
            floor_solid = False
            drop = None
            break
        if not is_air(entry):
            floor_solid = bool(entry[1]) and not has_fluid(entry)
            break
        drop += 1
        below = _offset(below, 0, -1, 0)
    walkable = floor_solid and drop is not None and drop <= MAX_SAFE_DROP
    return {"at": found[0], "floor_solid": floor_solid, "drop": drop,
            "walkable": walkable}


def _box(cells):
    """((min x, max x), (min y, max y), (min z, max z)) of the cells
    reported, or None for none: the grid's view."""
    if not cells:
        return None
    xs, ys, zs = zip(*cells)
    return (min(xs), max(xs)), (min(ys), max(ys)), (min(zs), max(zs))


def _r7_lava(cells, cell):
    reach = LAVA_CLEARANCE
    box = _box(cells)
    unseen = None
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 1):
            for dz in range(-reach, reach + 1):
                near = _offset(cell, dx, dy, dz)
                entry = cells.get(near)
                if is_lava(entry):
                    return Refusal("R7", cell, f"lava at {_words(near)} is "
                                   f"within {reach} blocks of "
                                   f"{_words(cell)}")
                if entry is None and unseen is None and box is not None \
                        and all(lo <= v <= hi
                                for v, (lo, hi) in zip(near, box)):
                    unseen = near
    if unseen is not None:
        return Refusal("R7", cell, f"I cannot see {_words(unseen)}, within "
                       f"{reach} blocks of {_words(cell)}, so I cannot rule "
                       f"out lava there")
    return None


def check_step(cells, feet, step, progress):
    """(the step with what to clear, None) or (None, the Refusal), or
    ("opening", details) when it reaches a cave (R6)."""
    for cell in step.passes:
        if cell not in cells:
            return None, Refusal("unknown", cell, f"I cannot see "
                                 f"{_words(cell)}, and I never assume "
                                 f"rock I have not seen")
    clear = tuple(c for c in step.passes
                  if not is_air(cells[c]) and not has_fluid(cells[c]))
    clearing = set(clear)
    for cell in step.passes:
        refusal = _r2_fluid(cells, cell, breaking=cell in clearing)
        if refusal is not None:
            return None, refusal
    # A cave is reported, floor and drop included, before the floor rule
    # refuses the step for it: R6 only ever stops, so it may go first.
    opening = _r6_opening(cells, step, progress)
    if opening is not None:
        return "opening", opening
    refusal = _r4_hazards(cells, step) or _r4_staircase(step, feet, cells)
    if refusal is not None:
        return None, refusal
    for cell in clear:
        for rule in (_r1_diggable(cells, cell),
                     _r3_falling(cells, cell, clearing)):
            if rule is not None:
                return None, rule
    for cell in step.passes + (step.floor,):
        refusal = _r7_lava(cells, cell)
        if refusal is not None:
            return None, refusal
    refusal = _r5_limits(step, progress, len(clear))
    if refusal is not None:
        return None, refusal
    return Step(step.direction, step.dx, step.dz, step.dy, step.passes,
                clear, step.to, step.floor), None


def plan_next(cells, feet, goal, progress) -> Plan:
    """The next stair toward `goal` (a feet cell), or why not.

    Only stairs that bring the goal closer are tried, nearest first, the
    current direction preferred; the first one every rule allows is the
    plan. If none is allowed, the refusal is the best one's."""
    if cells is None:
        return Plan("refused", "the bridge does not report the cells around "
                    "you (an older mod)", refusal=Refusal(
                        "unknown", None, "no near_blocks grid"))
    feet = tuple(feet)
    if feet == tuple(goal):
        return Plan("done", f"standing at {_words(feet)}")
    before = steps_left(feet, goal)
    candidates = []
    for order, (name, dx, dz) in enumerate(DIRECTIONS):
        for dy in (-1, 0, 1):
            step = shape(feet, name, dx, dz, dy)
            after = steps_left(step.to, goal)
            if after >= before:
                continue
            same = 0 if name == progress.last_direction else 1
            candidates.append((after, same, abs(dy - _sign(goal[1] - feet[1])),
                               order, step))
    candidates.sort(key=lambda c: c[:4])
    first_refusal = None
    for *_rank, step in candidates:
        checked, problem = check_step(cells, feet, step, progress)
        if checked == "opening":
            return Plan("opening", f"the tunnel opens into a cave at "
                        f"{_words(problem['at'])}", step=step,
                        opening=problem)
        if checked is not None:
            what = ("dig " + ", ".join(_words(c) for c in checked.clear)
                    if checked.clear else "walk")
            return Plan("dig" if checked.clear else "walk",
                        f"{what}, then step {checked.direction}"
                        f"{' and down' if step.dy < 0 else ''}"
                        f"{' and up' if step.dy > 0 else ''} to "
                        f"{_words(checked.to)}", step=checked)
        if first_refusal is None:
            first_refusal = problem
    if first_refusal is None:
        first_refusal = Refusal("R4", None, "no staircase step brings the "
                                "goal closer")
    return Plan("refused", first_refusal.describe(), refusal=first_refusal)


def _sign(v) -> int:
    return (v > 0) - (v < 0)


def check_break(cells, feet, cell, progress):
    """May this one cell be broken from where the player stands (a vein's
    next ore)? None, or the Refusal. The same rules as a stair: never the
    floor underfoot, natural blocks only, no fluid beside it, nothing that
    falls above it, no lava near, within the task's limits."""
    cell = tuple(cell)
    if cell == _offset(tuple(feet), 0, -1, 0):
        return Refusal("R4", cell, "that is the block I stand on")
    if cell not in cells:
        return Refusal("unknown", cell, f"I cannot see {_words(cell)}")
    if progress.broken + 1 > MAX_BROKEN:
        return Refusal("R5", cell, f"that would be more than {MAX_BROKEN} "
                       f"blocks broken in one task")
    for rule in (_r1_diggable(cells, cell), _r2_fluid(cells, cell),
                 _r3_falling(cells, cell, set()), _r7_lava(cells, cell)):
        if rule is not None:
            return rule
    return None


def check_walk(cells, feet, step):
    """May the player walk this one stair without breaking anything -- into
    air that is already there, to pick up a drop? None, or the Refusal.
    Every cell known; nothing to break and no fluid in the way (R2); the
    floor solid, dry, and not the one underfoot (R4); no lava near (R7).
    A cave is entered only on foot like this, never dug into."""
    for cell in step.passes + (step.floor,):
        if cell not in cells:
            return Refusal("unknown", cell, f"I cannot see {_words(cell)}")
    for cell in step.passes:
        refusal = _r2_fluid(cells, cell, breaking=False)
        if refusal is not None:
            return refusal
    refusal = _r4_hazards(cells, step)
    if refusal is not None:
        return refusal
    for cell in step.passes:
        if not is_air(cells[cell]):
            return Refusal("walk", cell, f"{_words(cell)} is not open, and "
                           f"walking breaks nothing")
    refusal = _r4_staircase(step, feet, cells)
    if refusal is not None:
        return refusal
    for cell in step.passes + (step.floor,):
        refusal = _r7_lava(cells, cell)
        if refusal is not None:
            return refusal
    return None


# ── R8: when to stop at once ─────────────────────────────────────────────────

def _r8_health(before, after):
    hb, ha = getattr(before, "health", None), getattr(after, "health", None)
    if isinstance(hb, (int, float)) and isinstance(ha, (int, float)) \
            and ha < hb:
        return f"I lost health ({hb:g} to {ha:g})"
    return ""


def _r8_fluid(cells, feet):
    reach = FLUID_ABORT_REACH
    for dx in range(-reach, reach + 1):
        for dy in range(-reach, reach + 2):          # feet and head
            for dz in range(-reach, reach + 1):
                cell = _offset(feet, dx, dy, dz)
                if has_fluid(cells.get(cell)):
                    return (f"water or lava appeared at {_words(cell)}, "
                            f"within {reach} blocks")
    return ""


def _r8_cave_in(cells, progress):
    """A block where this dig made air -- or a different block in a cell
    swung at than the one that was there: it fell in."""
    for cell in sorted(progress.cleared):
        entry = cells.get(cell)
        if entry is not None and not is_air(entry):
            return (f"{' '.join(entry[0].split('_'))} appeared at "
                    f"{_words(cell)}, a cell I had cleared -- a cave-in")
    for cell, was in sorted(progress.swung.items()):
        entry = cells.get(cell)
        if entry is not None and not is_air(entry) and entry[0] != was:
            return (f"{' '.join(entry[0].split('_'))} fell into "
                    f"{_words(cell)}, where I had just broken the "
                    f"{' '.join(str(was).split('_'))} -- a cave-in")
    return ""


def _r8_position(feet, expected_feet):
    if feet[1] < expected_feet[1] or \
            abs(feet[0] - expected_feet[0]) + \
            abs(feet[2] - expected_feet[2]) > 1:
        return (f"I am at {_words(feet)}, not {_words(expected_feet)} "
                f"where I should be -- I may have fallen")
    return ""


def abort_reason(before, after, progress, expected_feet=None) -> str:
    """Why a dig must stop now, or "". Compares the reading before a step
    with the one after it."""
    why = _r8_health(before, after)
    if why:
        return why
    cells = getattr(getattr(after, "near", None), "cells", None)
    position = getattr(after, "position", None)
    if cells is not None and position is not None:
        why = (_r8_fluid(cells, feet_of(position))
               or _r8_cave_in(cells, progress))
        if why:
            return why
    if expected_feet is not None and position is not None:
        return _r8_position(feet_of(position), tuple(expected_feet))
    return ""


__all__ = ["MAX_BROKEN", "MAX_DEPTH", "MAX_HORIZONTAL", "LAVA_CLEARANCE",
           "FLUID_ABORT_REACH", "MAX_SAFE_DROP", "DIGGABLE", "FALLING",
           "Refusal", "Step", "Plan", "Progress", "feet_of", "shape",
           "steps_left", "check_step", "plan_next", "check_break", "check_walk",
           "HAZARDS", "CONTACT_HAZARDS",
           "abort_reason", "is_diggable", "is_falling", "has_fluid",
           "is_lava", "is_air"]
