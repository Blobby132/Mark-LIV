"""
minecraft/navigation.py — where the ground is, and how to get across it.

WHAT THIS IS FOR
    Until now "find a tree" meant sweeping the crosshair and hoping one
    crossed it. The bridge can now report the surface around the player and
    the blocks worth walking to, which makes the question a different one: not
    "is a log in front of me" but "where is the nearest log and can I get
    there".

    This module answers that and nothing else. It reads a WorldState and
    produces coordinates. It does not press keys, does not know what a
    session is, and cannot reach the controller — a plan is data, and the
    thing that turns data into input is the ActionSpec layer, which validates
    it again.

THE MAP IS SPARSE, AND THAT IS THE POINT
    `WorldState.surface` is a list of blocks that were actually seen, not a
    grid. A column absent from it was not looked at, and this module treats
    that as impassable rather than as air.

    That is the difference between a planner that routes you round a hill and
    one that walks you off a cliff it never saw. Unknown is not empty, and
    every "is there ground here" question below returns one of three answers.

MOVEMENT RULES, DELIBERATELY SIMPLE
    Walk on solid ground. Step up at most one block, the way a player does
    without jumping. Drop at most three, which is the most Minecraft allows
    without damage. Diagonals need both orthogonal neighbours passable, or
    you clip a corner you cannot actually pass.

    No jumping across gaps, no swimming, no ladders, no scaffolding. Those
    are real Minecraft movement and they are not here, because a planner that
    produces a route it cannot walk is worse than one that admits it is
    stuck.

BOUNDED, LIKE EVERYTHING ELSE
    A* stops at MAX_NODES. The scan is only a few hundred columns so a real
    search never approaches it, and a bug that would otherwise spin does not
    get to.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

MAX_STEP_UP = 1
"""How far up a player walks without jumping."""

MAX_DROP = 3
"""The furthest fall Minecraft allows without damage."""

MAX_NODES = 4000
"""Ceiling on A* expansions. The scan is ~441 columns, so a genuine search
never comes close; this exists so a bug cannot spin."""

MAX_SMOOTHING = 12
"""How many waypoints ahead to consider collapsing into one move.

Bounded because the check costs a line trace per candidate, and because a
move longer than a couple of seconds is refused by the action spec anyway --
looking twenty blocks ahead would only ever produce a step that gets
shortened."""

MAX_PATH_LENGTH = 64
"""Longest route returned. A path longer than the scan radius would be
mostly invention anyway."""

# Blocks you should not stand on even though they are solid enough to walk
# over, and blocks that are not ground at all.
HAZARDS = frozenset({
    "lava", "flowing_lava", "fire", "soul_fire", "magma_block", "cactus",
    "sweet_berry_bush", "wither_rose", "powder_snow", "campfire",
    "soul_campfire",
})

LIQUIDS = frozenset({"water", "flowing_water", "lava", "flowing_lava"})

LOG_BLOCKS = frozenset({
    "oak_log", "birch_log", "spruce_log", "jungle_log", "acacia_log",
    "dark_oak_log", "mangrove_log", "cherry_log", "pale_oak_log",
})

ORE_SUFFIX = "_ore"

# Named categories a person might ask for, mapped to what counts.
CATEGORIES = {
    "log": LOG_BLOCKS,
    "wood": LOG_BLOCKS,
    "tree": LOG_BLOCKS,
    "stone": frozenset({"stone", "cobblestone", "andesite", "diorite",
                        "granite", "deepslate", "tuff"}),
    "dirt": frozenset({"dirt", "coarse_dirt", "rooted_dirt", "podzol"}),
    "grass": frozenset({"grass_block"}),
    "sand": frozenset({"sand", "red_sand"}),
    "water": frozenset({"water", "flowing_water"}),
    "crafting_table": frozenset({"crafting_table"}),
    "furnace": frozenset({"furnace", "blast_furnace", "smoker"}),
    "chest": frozenset({"chest", "trapped_chest", "barrel"}),
}


# ── The map ──────────────────────────────────────────────────────────────────

@dataclass
class LocalMap:
    """The ground around the player, as columns.

    Built once per plan from a WorldState. Cheap to build (a few hundred
    entries) and immutable in practice, so a path is planned against one
    consistent snapshot rather than a world shifting underneath it."""

    ground: dict = field(default_factory=dict)      # (x, z) -> (y, name, solid)
    origin: tuple | None = None                     # player (x, y, z)
    radius: int | None = None

    # ── building ─────────────────────────────────────────────────────────────

    @classmethod
    def from_state(cls, state) -> "LocalMap":
        ground: dict = {}
        for block in (getattr(state, "surface", None) or ()):
            # Last one wins; the bridge reports one surface per column, so a
            # duplicate means a malformed payload rather than a real choice.
            ground[(block.x, block.z)] = (block.y, block.name, block.solid)

        position = getattr(state, "position", None)
        origin = None
        if position is not None:
            try:
                origin = (int(math.floor(position[0])),
                          int(math.floor(position[1])),
                          int(math.floor(position[2])))
            except (TypeError, IndexError, ValueError):
                origin = None

        return cls(ground=ground, origin=origin,
                   radius=getattr(state, "scan_radius", None))

    @property
    def known_columns(self) -> int:
        return len(self.ground)

    @property
    def usable(self) -> bool:
        """Enough to plan with. An empty scan is not a flat world."""
        return bool(self.ground) and self.origin is not None

    # ── questions ────────────────────────────────────────────────────────────

    def is_known(self, x: int, z: int) -> bool:
        return (x, z) in self.ground

    def ground_at(self, x: int, z: int):
        """The y you would stand ON at this column, or None if unknown."""
        entry = self.ground.get((x, z))
        return None if entry is None else entry[0]

    def block_at(self, x: int, z: int):
        """The name of the surface block, or None if unknown."""
        entry = self.ground.get((x, z))
        return None if entry is None else entry[1]

    def standable(self, x: int, z: int) -> bool:
        """Can a player stand here at all?

        False for unknown columns. Routing over a column nobody looked at is
        how a planner walks you off a cliff it never saw."""
        entry = self.ground.get((x, z))
        if entry is None:
            return False
        _y, name, solid = entry
        if name in HAZARDS or name in LIQUIDS:
            return False
        # `solid` is None when the game did not say. Treated as standable
        # because the bridge only reports a column's surface when it found a
        # non-air block there — but a known hazard above overrides it.
        return solid is not False

    def step_cost(self, here: tuple, there: tuple):
        """Cost of moving between two adjacent columns, or None if you can't.

        The vertical rules live here rather than in the search, so "why
        can't I go that way" has one answer in one place."""
        y_here = self.ground_at(*here)
        y_there = self.ground_at(*there)
        if y_here is None or y_there is None:
            return None
        if not self.standable(*there):
            return None

        climb = y_there - y_here
        if climb > MAX_STEP_UP:
            return None
        if -climb > MAX_DROP:
            return None

        dx = abs(there[0] - here[0])
        dz = abs(there[1] - here[1])
        base = 1.414 if (dx and dz) else 1.0
        # Climbing costs more than flat ground: given two routes a player
        # would take the level one.
        return base + (0.5 if climb > 0 else 0.0)

    def line_is_walkable(self, start: tuple, end: tuple) -> bool:
        """Can you walk the straight line between two columns?

        Used to collapse a route into the few long moves a person would
        actually make. A* returns a chain of one-block hops because that is
        what the grid is made of; walking them one at a time spends a step
        per block and makes a fifteen-block stroll hit the task limit.

        Sampled at half-block intervals rather than by Bresenham, because
        what matters is the columns a moving player's body passes through,
        and a line clipping the corner of a column still has to survive it."""
        x0, z0 = start
        x1, z1 = end
        dx, dz = x1 - x0, z1 - z0
        steps = int(max(abs(dx), abs(dz)) * 2)
        if steps <= 0:
            return True

        previous = (x0, z0)
        for i in range(1, steps + 1):
            column = (int(round(x0 + dx * i / steps)),
                      int(round(z0 + dz * i / steps)))
            if column == previous:
                continue
            # Consecutive samples can be diagonal; step_cost applies the same
            # climb and drop rules the search itself used, so a smoothed line
            # can never be one the pathfinder would have refused.
            if self.step_cost(previous, column) is None:
                return False
            previous = column
        return True

    def neighbours(self, column: tuple):
        """Passable neighbours, with costs. Diagonals need both orthogonals
        passable — otherwise the route clips a corner you cannot walk through."""
        x, z = column
        straight = {}
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            target = (x + dx, z + dz)
            cost = self.step_cost(column, target)
            if cost is not None:
                straight[(dx, dz)] = (target, cost)

        for key, (target, cost) in straight.items():
            yield target, cost

        for dx, dz in ((1, 1), (1, -1), (-1, 1), (-1, -1)):
            if (dx, 0) not in straight or (0, dz) not in straight:
                continue
            target = (x + dx, z + dz)
            cost = self.step_cost(column, target)
            if cost is not None:
                yield target, cost


# ── The plan ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Path:
    """A route, or an honest account of why there isn't one."""

    waypoints: tuple = ()
    reason: str = ""
    nodes_expanded: int = 0
    truncated: bool = False

    @property
    def found(self) -> bool:
        return bool(self.waypoints)

    def __len__(self) -> int:
        return len(self.waypoints)

    @property
    def destination(self):
        return self.waypoints[-1] if self.waypoints else None

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "waypoints": [list(w) for w in self.waypoints],
            "length": len(self.waypoints),
            "reason": self.reason,
            "nodes_expanded": self.nodes_expanded,
            "truncated": self.truncated,
        }

    def describe(self) -> str:
        if not self.found:
            return f"No route: {self.reason}"
        end = self.destination
        note = " (shortened)" if self.truncated else ""
        return (f"A route of {len(self.waypoints)} steps to "
                f"({end[0]}, {end[1]}, {end[2]}){note}.")


def _heuristic(a: tuple, b: tuple) -> float:
    """Octile distance: the true cost on an 8-connected grid, so it never
    overestimates and A* stays optimal."""
    dx = abs(a[0] - b[0])
    dz = abs(a[1] - b[1])
    return (dx + dz) + (1.414 - 2) * min(dx, dz)


def find_path(state, goal, max_nodes: int = MAX_NODES) -> Path:
    """A* from the player to a goal column.

    `goal` may be an (x, z) column or an (x, y, z) block — the y is ignored,
    because you walk to a column and the map decides its height."""
    local = LocalMap.from_state(state)
    if not local.usable:
        return Path(reason="I cannot see the ground around me — the bridge "
                           "reported no terrain, so there is nothing to plan "
                           "over.")

    try:
        target = (int(goal[0]), int(goal[-1])) if len(goal) == 2 else \
                 (int(goal[0]), int(goal[2]))
    except (TypeError, IndexError, ValueError):
        return Path(reason=f"{goal!r} is not a position I can walk to.")

    start = (local.origin[0], local.origin[2])
    if start == target:
        return Path(reason="I am already there.")
    if not local.is_known(*start):
        # The scan is centred on the player, so this should not happen. When
        # it does, "no route" would be the wrong reason -- the search never
        # had a starting point. Saying which is the difference between "go
        # another way" and "the scan is broken".
        return Path(reason=f"the scan does not cover the ground I am "
                           f"standing on ({start[0]}, {start[1]}), so I "
                           f"cannot plan from here")
    if not local.is_known(*target):
        return Path(reason="that spot is outside what I can see, so I will "
                           "not guess a way to it")
    if not local.standable(*target):
        return Path(reason="there is nowhere to stand at that spot")

    open_heap = [(0.0, start)]
    came_from: dict = {}
    cost_so_far = {start: 0.0}
    expanded = 0

    while open_heap and expanded < max_nodes:
        _priority, current = heapq.heappop(open_heap)
        if current == target:
            return _build(local, came_from, current, expanded)
        expanded += 1

        for neighbour, step in local.neighbours(current):
            new_cost = cost_so_far[current] + step
            if new_cost < cost_so_far.get(neighbour, float("inf")):
                cost_so_far[neighbour] = new_cost
                came_from[neighbour] = current
                heapq.heappush(
                    open_heap,
                    (new_cost + _heuristic(neighbour, target), neighbour))

    if expanded >= max_nodes:
        return Path(reason="the search got too large before finding a way",
                    nodes_expanded=expanded)
    return Path(reason="there is no walkable route to it from here — "
                       "something impassable is in the way",
                nodes_expanded=expanded)


def _build(local: LocalMap, came_from: dict, end: tuple,
           expanded: int) -> Path:
    columns = [end]
    while columns[-1] in came_from:
        columns.append(came_from[columns[-1]])
    columns.reverse()
    columns = columns[1:]          # drop the column we are standing in

    truncated = len(columns) > MAX_PATH_LENGTH
    if truncated:
        columns = columns[:MAX_PATH_LENGTH]

    waypoints = tuple((x, local.ground_at(x, z) + 1, z) for x, z in columns)
    return Path(waypoints=waypoints, nodes_expanded=expanded,
                truncated=truncated)


def furthest_clear(local: "LocalMap", here: tuple, waypoints, start: int = 0,
                   look_ahead: int = MAX_SMOOTHING):
    """The furthest waypoint reachable from `here` in a straight line.

    This is what turns "a route of 14 one-block hops" into "walk that way for
    two seconds". It never invents a shortcut the pathfinder would have
    refused: every column on the line is checked with the same rules the
    search used, so smoothing can only ever collapse steps, never relax them.

    Returns (waypoint, index) or (None, start) when nothing is walkable --
    including the next one, which means the world changed and the caller
    should path again."""
    best = None
    best_index = start
    limit = min(len(waypoints), start + max(1, look_ahead))
    for index in range(start, limit):
        candidate = waypoints[index]
        column = (int(candidate[0]), int(candidate[2]))
        if not local.line_is_walkable(here, column):
            break
        best, best_index = candidate, index
    return best, best_index


# ── Heading ──────────────────────────────────────────────────────────────────
#
# All yaw arithmetic lives here. Minecraft's yaw is 0 at south (+Z) and
# increases clockwise from above: 90 is west (-X), 180 north (-Z), 270 east
# (+X). Getting that convention wrong sends an agent consistently ninety
# degrees off, which looks like a pathfinding bug and is not.

PIXELS_PER_DEGREE = 8.0
"""How much mouse movement turns the view one degree.

A CALIBRATION, NOT A CONSTANT
    The real figure depends on the player's mouse sensitivity setting, and
    nothing in the bridge reports it. Eight is a reasonable figure at default
    sensitivity and WILL be wrong for some people.

    That is survivable because it is used in a loop that re-reads the actual
    rotation after every turn: an overshoot becomes a smaller correction
    rather than a permanent error. It is written down here, once, so that when
    somebody calibrates it properly there is exactly one number to change."""


# The measured figure for THIS machine, learned from watching real turns.
# None until something has measured one.
_measured_px_per_degree = None

MIN_PX_PER_DEGREE = 0.5
MAX_PX_PER_DEGREE = 60.0


def pixels_per_degree() -> float:
    """What to actually use: the measured figure if there is one.

    PIXELS_PER_DEGREE is the starting guess. This is what replaces it once a
    real turn has been watched, which is the only way to know -- the figure
    depends on a sensitivity slider nothing in the bridge reports."""
    return _measured_px_per_degree or PIXELS_PER_DEGREE


def calibrate(pixels_sent: float, degrees_turned: float):
    """Learn the mouse scale from one observed turn, or ignore it.

    Returns the new figure, or None when the turn was not worth measuring.

    DELIBERATELY UNFUSSY
        Both readings are rounded (the game reports yaw to a few decimals,
        and the mouse moves in whole pixels), so a tiny turn measures
        terribly. Anything under a few degrees or a few dozen pixels is
        thrown away rather than averaged in.

        The result is clamped to a sane range, because the failure this
        guards against is not a slightly wrong number -- it is one absurd
        reading (a turn that coincided with the player moving their own
        mouse) permanently poisoning every later turn."""
    global _measured_px_per_degree
    if abs(degrees_turned) < 3.0 or abs(pixels_sent) < 40.0:
        return None
    measured = abs(pixels_sent) / abs(degrees_turned)
    if not MIN_PX_PER_DEGREE <= measured <= MAX_PX_PER_DEGREE:
        return None
    if _measured_px_per_degree is None:
        _measured_px_per_degree = measured
    else:
        # Blended, so one odd reading moves it rather than replacing it.
        _measured_px_per_degree = (_measured_px_per_degree * 0.5
                                   + measured * 0.5)
    return _measured_px_per_degree


def reset_calibration() -> None:
    """Forget what was measured. For tests, and for a fresh session."""
    global _measured_px_per_degree
    _measured_px_per_degree = None


def yaw_to(origin, target) -> float:
    """The yaw that faces from `origin` towards `target`."""
    dx = target[0] - origin[0]
    dz = target[-1] - origin[-1]
    return math.degrees(math.atan2(-dx, dz))


def yaw_difference(current: float, desired: float) -> float:
    """Shortest signed turn from one yaw to another, in degrees.

    Wrapped to (-180, 180] so turning from 179 to -179 is two degrees rather
    than three hundred and fifty-eight."""
    return (desired - current + 180.0) % 360.0 - 180.0



EYE_HEIGHT = 1.62
"""Where a standing player's eyes are above their feet.

`WorldState.position` is the feet. Aiming from the feet at a block two above
you points the crosshair over its top, which is how an agent stands in front
of a tree and mines the air behind it."""


def pitch_to(origin, target, eye_height: float = EYE_HEIGHT) -> float:
    """The pitch that looks from `origin` (feet) at `target` (a block).

    Minecraft's pitch is negative looking up and positive looking down, which
    is the opposite of the sign most people expect and worth stating once
    here rather than rediscovering per caller. Aimed at the middle of the
    block, not its corner."""
    try:
        dx = (target[0] + 0.5) - origin[0]
        dy = (target[1] + 0.5) - (origin[1] + eye_height)
        dz = (target[2] + 0.5) - origin[2]
    except (TypeError, IndexError):
        return 0.0
    flat = math.hypot(dx, dz)
    if flat < 1e-6:
        return -90.0 if dy > 0 else 90.0
    return math.degrees(math.atan2(-dy, flat))


def aim_at(position, rotation, target,
           px_per_degree: float | None = None) -> tuple:
    """Mouse (dx, dy) that points the crosshair at a block, and the error
    in degrees that remains to be corrected.

    Returns (dx, dy, error_degrees). The error is what a caller checks
    against a tolerance -- there is no point spending a step on a two degree
    correction the game does not care about."""
    try:
        yaw_now, pitch_now = float(rotation[0]), float(rotation[1])
    except (TypeError, IndexError, ValueError):
        return 0, 0, 180.0

    yaw_wanted = yaw_to(position, target)
    pitch_wanted = pitch_to(position, target)
    dyaw = yaw_difference(yaw_now, yaw_wanted)
    dpitch = pitch_wanted - pitch_now

    scale = pixels_per_degree() if px_per_degree is None else px_per_degree
    return (int(round(dyaw * scale)), int(round(dpitch * scale)),
            math.hypot(dyaw, dpitch))


def look_delta_for(current_yaw: float, desired_yaw: float,
                   px_per_degree: float | None = None) -> int:
    """Mouse dx that turns from one yaw to another.

    Positive dx turns right, which is increasing yaw in Minecraft's
    convention."""
    scale = pixels_per_degree() if px_per_degree is None else px_per_degree
    return int(round(yaw_difference(current_yaw, desired_yaw) * scale))


__all__ = [
    "LocalMap", "Path", "find_path",
    "yaw_to", "yaw_difference", "look_delta_for", "PIXELS_PER_DEGREE",
    "pitch_to", "aim_at", "EYE_HEIGHT",
    "pixels_per_degree", "calibrate", "reset_calibration",
    "line_is_walkable" if False else "furthest_clear", "MAX_SMOOTHING",
    "MAX_STEP_UP", "MAX_DROP", "MAX_NODES", "MAX_PATH_LENGTH",
    "HAZARDS", "LIQUIDS", "LOG_BLOCKS", "CATEGORIES",
]


# ── World queries ────────────────────────────────────────────────────────────
#
# The questions a planner actually asks, answered deterministically from a
# WorldState. They exist so nothing above this line ever reasons about raw
# block lists -- an LLM handed a few hundred coordinates will invent a
# relationship between them, and these functions are what it gets instead.

def blocks_matching(state, names) -> tuple:
    """Every observed block whose name is in `names`.

    Searches both lists the bridge provides: `notable_blocks` names the things
    worth walking to, and `surface` is the ground, which is where grass and
    stone actually live."""
    wanted = frozenset(names or ())
    if not wanted:
        return ()
    seen = {}
    for source in (getattr(state, "notable_blocks", None) or (),
                   getattr(state, "surface", None) or ()):
        for block in source:
            if block.name in wanted:
                seen[block.position] = block
    return tuple(seen.values())


def blocks_in_category(state, category: str) -> tuple:
    """Blocks matching a named category such as "log" or "stone"."""
    names = CATEGORIES.get(str(category or "").strip().lower())
    if names is None:
        # An unrecognised category is treated as a literal block name, so
        # "iron_ore" works without needing an entry in the table.
        literal = str(category or "").strip().lower()
        return blocks_matching(state, {literal}) if literal else ()
    return blocks_matching(state, names)


def ores(state) -> tuple:
    """Any ore, by suffix, so a modded ore is found without a table entry."""
    out = []
    for block in (getattr(state, "notable_blocks", None) or ()):
        if block.name.endswith(ORE_SUFFIX):
            out.append(block)
    return tuple(out)


def nearest_block(state, category: str, reachable_only: bool = False):
    """The closest block of a category, or None.

    `reachable_only` runs the pathfinder for each candidate, nearest first,
    and returns the first one there is actually a route to. That costs a
    search per candidate, which is why it is off by default: "what is nearby"
    and "where can I get to" are different questions and only the caller
    knows which it is asking."""
    position = getattr(state, "position", None)
    if position is None:
        return None

    candidates = sorted(blocks_in_category(state, category),
                        key=lambda b: b.distance_to(position))
    if not reachable_only:
        return candidates[0] if candidates else None

    for block in candidates:
        if approach_column(state, block) is not None:
            return block
    return None


def nearest_entity(state, category: str | None = None):
    """The closest entity, optionally of one category (hostile / passive /
    player / item)."""
    entities = getattr(state, "nearby_entities", None) or ()
    if category:
        wanted = str(category).strip().lower()
        entities = [e for e in entities if e.category == wanted]
    known = [e for e in entities if e.distance is not None]
    if not known:
        return None
    return min(known, key=lambda e: e.distance)


def approach_column(state, block, max_nodes: int = MAX_NODES):
    """A standable column next to `block` that there is a route to, or None.

    You cannot path INTO a block -- a log occupies the space you would stand
    in. So the goal is one of its neighbours, and the nearest reachable one
    wins. Returning None is the honest answer to "can I get to that tree",
    and is what stops a skill setting off towards something across a ravine."""
    local = LocalMap.from_state(state)
    if not local.usable:
        return None

    try:
        bx, bz = int(block.x), int(block.z)
    except (AttributeError, TypeError, ValueError):
        return None

    around = [(bx + dx, bz + dz)
              for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1),
                             (1, 1), (1, -1), (-1, 1), (-1, -1))]
    around.sort(key=lambda c: abs(c[0] - local.origin[0])
                + abs(c[1] - local.origin[2]))

    for column in around:
        if not local.standable(*column):
            continue
        if column == (local.origin[0], local.origin[2]):
            return column
        if find_path(state, column, max_nodes=max_nodes).found:
            return column
    return None


def is_walkable(state, x: int, z: int) -> bool:
    """Can a player stand at this column? False for unknown columns."""
    return LocalMap.from_state(state).standable(int(x), int(z))


def is_known(state, x: int, z: int) -> bool:
    """Did the scan actually look here? Distinct from walkable: a column can
    be known and impassable, or unknown and perfectly fine."""
    return LocalMap.from_state(state).is_known(int(x), int(z))


def reachable(state, goal) -> bool:
    return find_path(state, goal).found


def summarise(state) -> dict:
    """What is around, in the terms a planner and a person both use.

    This is what the LLM sees instead of several hundred coordinates."""
    position = getattr(state, "position", None)
    local = LocalMap.from_state(state)

    out = {
        "position": list(position) if position else None,
        "columns_seen": local.known_columns,
        "scan_radius": getattr(state, "scan_radius", None),
    }

    for label in ("log", "stone", "water", "crafting_table"):
        block = nearest_block(state, label)
        if block is not None:
            out[f"nearest_{label}"] = {
                "name": block.name,
                "position": list(block.position),
                "distance": round(block.distance_to(position), 1)
                if position else None,
            }

    for label in ("hostile", "passive"):
        entity = nearest_entity(state, label)
        if entity is not None:
            out[f"nearest_{label}"] = {
                "name": entity.name,
                "distance": round(entity.distance, 1),
                "position": list(entity.position) if entity.position else None,
            }

    found_ores = ores(state)
    if found_ores:
        out["ores_seen"] = sorted({b.name for b in found_ores})
    return out


__all__ += [
    "blocks_matching", "blocks_in_category", "ores", "nearest_block",
    "nearest_entity", "approach_column", "is_walkable", "is_known",
    "reachable", "summarise",
]
