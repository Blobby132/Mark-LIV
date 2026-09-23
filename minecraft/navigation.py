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

from minecraft import aiming

PLAYER_HEIGHT = 2
"""How many blocks of empty space a standing player needs."""

PLAYER_HALF_WIDTH = 0.3
"""Half of a player's 0.6-block width. A shoulder, not a point."""

MAX_REPORTED_CLEARANCE = 4
"""The bridge stops counting headroom here, so 4 means "at least 4"."""

JUMP_CLEARANCE = 3
"""And how many to jump: you rise a block, so your head needs one more."""

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
    "soul_campfire", "cobweb",
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

    # (x, z) -> (y, name, solid, clearance, cover)
    ground: dict = field(default_factory=dict)
    origin: tuple | None = None                     # player (x, y, z)
    radius: int | None = None

    # ── building ─────────────────────────────────────────────────────────────

    @classmethod
    def from_state(cls, state) -> "LocalMap":
        ground: dict = {}
        for block in (getattr(state, "surface", None) or ()):
            # Last one wins; the bridge reports one surface per column, so a
            # duplicate means a malformed payload rather than a real choice.
            ground[(block.x, block.z)] = (block.y, block.name, block.solid,
                                          block.clearance,
                                          getattr(block, "cover", None))

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

    def clearance_at(self, x: int, z: int):
        """Blocks of empty space above the ground here, or None if the bridge
        did not say. None is a third answer and is treated as such below: an
        older mod reports no clearance at all, and the world must not become
        impassable because of it."""
        entry = self.ground.get((x, z))
        return None if entry is None else entry[3]

    def fits(self, x: int, z: int) -> bool:
        """Can a player's whole BODY be here, not just their feet?

        Two blocks tall. This is the difference between a route that follows
        the ground correctly and one that walks you into a branch, a ledge or
        a doorway lintel — the ground was never the thing that stopped you.

        Unreported clearance is permitted, because refusing it would make
        every column impassable on an older mod. That is the one place here
        where unknown is not treated as impassable, and it is a deliberate
        trade: the alternative is a bridge upgrade silently required for the
        planner to work at all."""
        room = self.clearance_at(x, z)
        return room is None or room >= PLAYER_HEIGHT

    def block_at(self, x: int, z: int):
        """The name of the surface block, or None if unknown."""
        entry = self.ground.get((x, z))
        return None if entry is None else entry[1]

    def cover_at(self, x: int, z: int):
        """What a player standing here would be inside -- grass, a flower, a
        berry bush -- or None for empty space or an unreported column."""
        entry = self.ground.get((x, z))
        return None if entry is None or len(entry) < 5 else entry[4]

    def standable(self, x: int, z: int) -> bool:
        """Can a player stand here at all?

        False for unknown columns. Routing over a column nobody looked at is
        how a planner walks you off a cliff it never saw."""
        entry = self.ground.get((x, z))
        if entry is None:
            return False
        _y, name, solid = entry[0], entry[1], entry[2]
        if name in HAZARDS or name in LIQUIDS:
            return False
        # What you would be standing IN. Grass is fine; a berry bush or fire
        # is walked through, not over, and hurts all the same.
        if self.cover_at(x, z) in HAZARDS:
            return False
        # `solid` is None when the game did not say. Treated as standable
        # because the bridge only reports a column's surface when it found a
        # non-air block there — but a known hazard above overrides it.
        return solid is not False

    def can_rise(self, column: tuple) -> bool:
        """Is there room above this column to gain a block of height?"""
        room = self.clearance_at(*column)
        return room is None or room >= JUMP_CLEARANCE

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

        if not self.fits(*there):
            return None            # ground is fine, the player is not

        climb = y_there - y_here
        if climb > MAX_STEP_UP:
            return None
        if -climb > MAX_DROP:
            return None
        if climb > 0 and not self.can_rise(here):
            # Stepping up needs room above where you are STANDING as well as
            # where you are going. A one-block step under a two-block ceiling
            # is a headbutt.
            return None

        dx = abs(there[0] - here[0])
        dz = abs(there[1] - here[1])
        base = 1.414 if (dx and dz) else 1.0
        # Climbing costs more than flat ground: given two routes a player
        # would take the level one.
        return base + (0.5 if climb > 0 else 0.0)

    def body_columns(self, start, end, half_width: float = None,
                     stride: float = 0.1):
        """Every column the player's FOOTPRINT touches walking a straight
        line, in the order it first touches them.

        The player is not a point. They are 0.6 of a block wide, so a line
        that grazes the corner of a wall takes a shoulder into it — and they
        are rarely standing at the exact centre of a column, so a line traced
        between column centres is not the line they will walk. Both mistakes
        were here, and together they produced a route that looked clear and
        clipped the wall it had just come round."""
        hw = PLAYER_HALF_WIDTH if half_width is None else half_width
        try:
            x0, z0 = float(start[0]), float(start[-1])
            x1, z1 = float(end[0]), float(end[-1])
        except (TypeError, IndexError, ValueError):
            return []
        length = math.hypot(x1 - x0, z1 - z0)
        samples = max(1, int(math.ceil(length / stride)))
        ordered, seen = [], set()
        for i in range(samples + 1):
            t = i / samples
            cx, cz = x0 + (x1 - x0) * t, z0 + (z1 - z0) * t
            for ox in (-hw, hw):
                for oz in (-hw, hw):
                    column = (math.floor(cx + ox), math.floor(cz + oz))
                    if column not in seen:
                        seen.add(column)
                        ordered.append((column, (cx, cz)))
        return ordered

    def first_blocked(self, start, end, allow_climb: bool = False):
        """The first column along a straight walk the body cannot enter.

        Returns (column, reason) or None when the whole walk is clear.
        `start` and `end` are real (x, z) positions, not column indices.

        reason is one of: "unknown" (never scanned), "impassable" (hazard,
        liquid, no footing), "climb" (a full block up, which needs a jump),
        "wall" (more than one block up), "head" (no room for a body),
        "drop" (a fall too far to take).

        `allow_climb` permits ONE one-block rise, for a hop. A plain walk
        cannot climb a full block at all — Minecraft steps up 0.6 of a block,
        which is a slab, not a block — and treating a climb as walkable is
        what sent the player into ledges it then had to be told to jump."""
        try:
            here = (math.floor(float(start[0])), math.floor(float(start[-1])))
        except (TypeError, IndexError, ValueError):
            return (None, "unknown")
        feet = self.ground_at(*here)
        if feet is None:
            return (here, "unknown")

        climbed = False
        centre = here
        for column, (cx, cz) in self.body_columns(start, end):
            # The centre of the body decides the height it walks at; the
            # corners only have to fit around it.
            now_centre = (math.floor(cx), math.floor(cz))
            if now_centre != centre and self.is_known(*now_centre):
                ground = self.ground_at(*now_centre)
                if ground is not None and ground < feet:
                    if feet - ground > MAX_DROP:
                        return (now_centre, "drop")
                    feet = ground
                centre = now_centre

            if column == here:
                continue
            if not self.is_known(*column):
                return (column, "unknown")
            if not self.standable(*column):
                return (column, "impassable")

            ground = self.ground_at(*column)
            rise = ground - feet
            if rise > MAX_STEP_UP:
                return (column, "wall")
            if rise > 0:
                if not allow_climb or climbed:
                    return (column, "climb")
                if not self.can_rise((here[0], here[1])) or not self.fits(*column):
                    return (column, "head")
                climbed = True
                feet = ground
                continue

            # Room for a body at the height it is walking, not merely above
            # this column's own ground — a column that dips lower still has
            # to be clear up to the player's head.
            room = self.clearance_at(*column)
            if room is not None and room < MAX_REPORTED_CLEARANCE:
                if room < (feet - ground) + PLAYER_HEIGHT:
                    return (column, "head")
        return None

    def sight_is_clear(self, eye, point, skip=(), stride: float = 0.1):
        """Does a straight line from `eye` to `point` pass only through
        space the map says is open?

        Open means above a column's ground and, where the bridge measured
        it, below whatever caps that column's clearance -- a canopy, a
        ledge, a roof. Columns in `skip` (the viewer's own, the target's)
        are not checked, and an unscanned column is not open: a view nobody
        looked along is not a view."""
        try:
            x0, y0, z0 = (float(v) for v in eye[:3])
            x1, y1, z1 = (float(v) for v in point[:3])
        except (TypeError, ValueError):
            return False
        skip = set(skip)
        samples = max(1, int(math.ceil(
            math.dist((x0, y0, z0), (x1, y1, z1)) / stride)))
        for i in range(samples + 1):
            t = i / samples
            x, y, z = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, \
                z0 + (z1 - z0) * t
            column = (math.floor(x), math.floor(z))
            if column in skip:
                continue
            ground = self.ground_at(*column)
            if ground is None:
                return False
            if y < ground + 1:
                return False
            room = self.clearance_at(*column)
            if room is not None and room < MAX_REPORTED_CLEARANCE \
                    and y >= ground + 1 + room:
                return False
        return True

    def line_is_walkable(self, start, end) -> bool:
        """Can the player walk this straight line without jumping?

        Used to collapse a route into the few long moves a person would
        actually make. `start` should be the player's REAL position; a
        column index is accepted and treated as that column's centre."""
        return self.first_blocked(_centre_of(start), _centre_of(end)) is None

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


def find_path(state, goal, max_nodes: int = MAX_NODES, avoid=None) -> Path:
    """A* from the player to a goal column.

    `goal` may be an (x, z) column or an (x, y, z) block — the y is ignored,
    because you walk to a column and the map decides its height."""
    local = LocalMap.from_state(state)
    if not local.usable:
        return Path(reason="I cannot see the ground around me — the bridge "
                           "reported no terrain, so there is nothing to plan "
                           "over.")
    # Columns a caller has learned the hard way are impassable. The scan says
    # they look fine; walking into them said otherwise, and experience beats
    # the map.
    blocked = frozenset(avoid or ())

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
            if neighbour in blocked:
                continue
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


def _centre_of(point):
    """A real (x, z) position, from either a position or a column index.

    Integers are column indices and mean the middle of that column; floats
    are already positions. The distinction matters: the player is almost
    never at a column's centre, and pretending they are is how a line that
    looks clear clips a corner."""
    try:
        x, z = point[0], point[-1]
    except (TypeError, IndexError):
        return point
    if isinstance(x, int) and isinstance(z, int):
        return (x + 0.5, z + 0.5)
    return (float(x), float(z))


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



# ── What is in the way ───────────────────────────────────────────────────────
#
# "It stopped moving" is not a diagnosis, and every cause below wants a
# different response. Telling them apart is the difference between an agent
# that hops a fence and one that presses W into it until the step limit.

CLEAR = "clear"                  # nothing in the way; the stall is elsewhere
STEP_UP = "step_up"              # one block: walk up, or hop
JUMPABLE = "jumpable"            # one block with room to land: JUMP
WALL = "wall"                    # two or more: go round
HEAD_BLOCKED = "head_blocked"    # feet fit, body does not
DROP = "drop"                    # a fall, possibly a dangerous one
CLIFF = "cliff"                  # a fall too far to take on purpose
HAZARD = "hazard"                # lava, fire, cactus: never walk in
LIQUID = "liquid"                # water: passable but not planned over
UNSEEN = "unseen"                # outside the scan; observe again


@dataclass(frozen=True)
class Obstacle:
    """What is immediately ahead, and what to do about it."""

    kind: str
    column: tuple | None = None
    height: int = 0
    detail: str = ""

    @property
    def can_jump(self) -> bool:
        return self.kind == JUMPABLE

    @property
    def needs_reroute(self) -> bool:
        return self.kind in (WALL, HEAD_BLOCKED, CLIFF, HAZARD)

    @property
    def needs_observation(self) -> bool:
        return self.kind == UNSEEN

    def describe(self) -> str:
        where = f" at {self.column}" if self.column else ""
        return {
            CLEAR: "nothing in the way",
            STEP_UP: f"a one-block step{where}",
            JUMPABLE: f"a one-block obstacle{where} I can jump",
            WALL: f"a wall {self.height} blocks high{where}",
            HEAD_BLOCKED: f"headroom too low{where} — my feet fit, I do not",
            DROP: f"a {self.height}-block drop{where}",
            CLIFF: f"a drop of {self.height} blocks{where}, too far to take",
            HAZARD: f"{self.detail or 'something dangerous'}{where}",
            LIQUID: f"{self.detail or 'water'}{where}",
            UNSEEN: f"nothing scanned{where} — I need to look again",
        }.get(self.kind, self.kind) + (f" ({self.detail})"
                                       if self.detail and self.kind in
                                       (WALL, HEAD_BLOCKED) else "")


PROBE_DISTANCE = 1.2
"""How far ahead to sweep the body when asking what stopped it.

A little over a block: far enough to reach the column in front from anywhere
in the current one, short enough that it is about THIS step and not a wall
further down the route, which is the pathfinder's business."""


def obstacle_ahead(state, heading_to=None) -> Obstacle:
    """Classify whatever is between the player and where they are going.

    Sweeps the player's actual footprint from their actual position, a
    little over a block towards `heading_to` (or along their facing). An
    earlier version looked at the single column straight ahead of the
    column centre — and so, having clipped the corner of a wall on a
    diagonal, it looked at open ground and concluded nothing was in the way.
    """
    local = LocalMap.from_state(state)
    if not local.usable:
        return Obstacle(UNSEEN, detail="no terrain scan")

    position = getattr(state, "position", None)
    try:
        px, pz = float(position[0]), float(position[2])
    except (TypeError, IndexError, ValueError):
        return Obstacle(UNSEEN, detail="I cannot read my own position")

    direction = _heading_vector(state, (px, pz), heading_to)
    if direction is None:
        return Obstacle(UNSEEN, detail="I cannot tell which way I am facing")
    if direction == (0.0, 0.0):
        return Obstacle(CLEAR)

    probe = (px + direction[0] * PROBE_DISTANCE,
             pz + direction[1] * PROBE_DISTANCE)

    # First as a plain walk. If that is blocked by a single step up, check
    # whether a hop would clear it — that is the one-block case that used to
    # need a person to say "jump".
    blocked = local.first_blocked((px, pz), probe, allow_climb=False)
    if blocked is None:
        return Obstacle(CLEAR)
    column, reason = blocked
    if column is None:
        return Obstacle(UNSEEN, detail="I cannot place myself on the map")

    name = local.block_at(*column)
    here = (math.floor(px), math.floor(pz))
    feet = local.ground_at(*here)
    ground = local.ground_at(*column)
    height = (ground - feet) if (ground is not None and feet is not None) else 0

    if reason == "unknown":
        return Obstacle(UNSEEN, column=column)
    if reason == "impassable":
        cover = local.cover_at(*column)
        if cover in HAZARDS:
            return Obstacle(HAZARD, column=column, detail=_readable(cover))
        if name in HAZARDS:
            return Obstacle(HAZARD, column=column, detail=_readable(name))
        if name in LIQUIDS:
            return Obstacle(LIQUID, column=column, detail=_readable(name))
        return Obstacle(WALL, column=column, height=max(height, 1),
                        detail=_readable(name))
    if reason == "drop":
        return Obstacle(CLIFF, column=column, height=-height)
    if reason == "wall":
        return Obstacle(WALL, column=column, height=height)
    if reason == "head":
        room = local.clearance_at(*column)
        return Obstacle(HEAD_BLOCKED, column=column,
                        detail=f"{room} blocks of room" if room is not None
                        else "")
    if reason == "climb":
        hop = local.first_blocked((px, pz), probe, allow_climb=True)
        if hop is None:
            return Obstacle(JUMPABLE, column=column, height=height)
        hop_column, hop_reason = hop
        if hop_reason == "head":
            return Obstacle(HEAD_BLOCKED, column=hop_column,
                            detail="no room to jump up")
        return Obstacle(WALL, column=hop_column, height=height)
    return Obstacle(UNSEEN, column=column)


def _heading_vector(state, here, heading_to):
    """Unit (dx, dz) from the player towards a column, or along the facing."""
    if heading_to is not None:
        try:
            tx = float(heading_to[0]) + 0.5
            tz = float(heading_to[-1]) + 0.5
        except (TypeError, IndexError, ValueError):
            return None
        dx, dz = tx - here[0], tz - here[1]
        length = math.hypot(dx, dz)
        if length < 1e-6:
            return (0.0, 0.0)
        return (dx / length, dz / length)
    rotation = getattr(state, "rotation", None)
    try:
        radians = math.radians(float(rotation[0]))
    except (TypeError, IndexError, ValueError):
        return None
    return (-math.sin(radians), math.cos(radians))


def _readable(name) -> str:
    """"oak_log" as "oak log".

    Written with split/join rather than str.replace because the boundary test
    parses for `.replace()` — it cannot tell a string method from
    `Path.replace`, which renames a file, and it is right to refuse to guess."""
    return " ".join(str(name or "").split("_"))


# ── Heading ──────────────────────────────────────────────────────────────────
#
# All yaw arithmetic lives here. Minecraft's yaw is 0 at south (+Z) and
# increases clockwise from above: 90 is west (-X), 180 north (-Z), 270 east
# (+X). Getting that convention wrong sends an agent consistently ninety
# degrees off, which looks like a pathfinding bug and is not.

def pixels_per_degree_at(sensitivity) -> float | None:
    """Minecraft's own arithmetic, from the mouse sensitivity slider.

    THE NUMBER, NOT A GUESS AT THE NUMBER
        Minecraft turns the view by

            degrees = counts * 0.15 * (sensitivity * 0.6 + 0.2) ** 3 * 8

        so given the slider there is nothing to estimate. The mod reports it,
        and this converts it. A default-ish 47% slider works out near 7.4
        pixels per degree; 100% is 1.63, more than four times finer -- which
        is why a single hardcoded constant overshot by a factor of five for
        anyone who had turned their sensitivity up.

    Returns None when the value is not a usable slider position, so the
    caller falls back to measuring instead of trusting a bad reading."""
    try:
        slider = float(sensitivity)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= slider <= 1.0:
        return None
    degrees_per_count = 0.15 * (slider * 0.6 + 0.2) ** 3 * 8.0
    if degrees_per_count <= 1e-6:
        return None
    return 1.0 / degrees_per_count


# ── Heading ──────────────────────────────────────────────────────────────────
#
# All of it lives in `minecraft/aiming.py` now. These names stay as thin
# aliases because navigation is where callers expect to find "which way is
# that", and because one module owning the arithmetic is the entire point --
# two copies of a sign convention is how an agent ends up turning the wrong
# way in one code path and the right way in another.

PIXELS_PER_DEGREE = aiming.DEFAULT_PIXELS_PER_DEGREE
EYE_HEIGHT = aiming.EYE_HEIGHT
AIM_DAMPING = aiming.DAMPING

yaw_to = aiming.yaw_to
yaw_difference = aiming.yaw_difference
pitch_to = aiming.pitch_to
pixels_per_degree_at = aiming.pixels_per_degree_at
target_point = aiming.target_point


def pixels_per_degree() -> float:
    return aiming.SHARED.pixels_per_degree()


def use_sensitivity(sensitivity):
    return aiming.SHARED.use_sensitivity(sensitivity)


def observe_turn(sent_dx, sent_dy, before_rotation, after_rotation) -> None:
    aiming.SHARED.observe(sent_dx, sent_dy, before_rotation, after_rotation)


def calibrate(pixels_sent, degrees_turned):
    """Learn the yaw scale alone, for callers that only turn sideways."""
    before = aiming.SHARED.yaw_scale
    aiming.SHARED.yaw_scale = aiming._blend(before, pixels_sent, degrees_turned)
    if aiming.SHARED.yaw_scale is None or aiming.SHARED.yaw_scale == before:
        return None
    return abs(aiming.SHARED.yaw_scale)


def reset_calibration() -> None:
    aiming.SHARED.reset()


def calibration() -> dict:
    return aiming.SHARED.as_dict()


def look_delta_for(current_yaw: float, desired_yaw: float,
                   px_per_degree: float | None = None) -> int:
    if px_per_degree is not None:
        return int(round(yaw_difference(current_yaw, desired_yaw)
                         * px_per_degree))
    return aiming.SHARED.delta_for_yaw(current_yaw, desired_yaw)


def aim_at(position, rotation, target, px_per_degree=None, face=None) -> tuple:
    """(dx, dy, error) that points the crosshair at a block."""
    if px_per_degree is not None:
        scratch = aiming.MouseAim(yaw_scale=px_per_degree,
                                  pitch_scale=px_per_degree, damping=1.0)
        return scratch.aim_at(position, rotation, target, face)
    return aiming.SHARED.aim_at(position, rotation, target, face)


__all__ = [
    "LocalMap", "Path", "find_path",
    "yaw_to", "yaw_difference", "look_delta_for", "PIXELS_PER_DEGREE",
    "pitch_to", "aim_at", "EYE_HEIGHT",
    "pixels_per_degree", "calibrate", "reset_calibration",
    "observe_turn", "calibration", "AIM_DAMPING",
    "pixels_per_degree_at", "use_sensitivity",
    "line_is_walkable" if False else "furthest_clear", "MAX_SMOOTHING",
    "MAX_STEP_UP", "MAX_DROP", "MAX_NODES", "MAX_PATH_LENGTH",
    "HAZARDS", "LIQUIDS", "LOG_BLOCKS", "CATEGORIES",
    "Obstacle", "obstacle_ahead", "PLAYER_HEIGHT", "JUMP_CLEARANCE",
    "CLEAR", "STEP_UP", "JUMPABLE", "WALL", "HEAD_BLOCKED", "DROP",
    "CLIFF", "HAZARD", "LIQUID", "UNSEEN",
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


def nearest_block(state, category: str, reachable_only: bool = False,
                  exclude=None):
    """The closest block of a category, or None.

    `reachable_only` runs the pathfinder for each candidate, nearest first,
    and returns the first one there is actually a route to. That costs a
    search per candidate, which is why it is off by default: "what is nearby"
    and "where can I get to" are different questions and only the caller
    knows which it is asking."""
    position = getattr(state, "position", None)
    if position is None:
        return None

    skip = set(exclude or ())
    candidates = sorted((b for b in blocks_in_category(state, category)
                         if b.position not in skip),
                        key=lambda b: b.distance_to(position))
    if not reachable_only:
        return candidates[0] if candidates else None

    # One flood from the player answers "can I get there" for every
    # candidate at once, instead of a search per column per candidate.
    local = LocalMap.from_state(state)
    reachable = reachable_columns(local)
    for block in candidates:
        if _approach(local, reachable, block) is not None:
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


APPROACH_RINGS = 3
"""How far out from a block to look for somewhere to stand and reach it.

One ring -- the eight columns touching it -- was the first design. Round a
tree those are all under the canopy, and under a low canopy there is not
room to stand, so "walk to the tree" failed for every short oak. Reach is
4.5 blocks from the eyes: two or three columns out is still close enough to
hit a trunk, and is where a person would stand."""


def reachable_columns(local: LocalMap, max_nodes: int = MAX_NODES):
    """Every column there is a walkable route to from where the player
    stands -- by the same rules find_path uses, since it walks the same
    neighbours. Empty when the map is unusable or does not cover the
    player's own column."""
    if not local.usable:
        return frozenset()
    start = (local.origin[0], local.origin[2])
    if not local.is_known(*start):
        return frozenset()
    seen = {start}
    frontier = [start]
    expanded = 0
    while frontier and expanded < max_nodes:
        current = frontier.pop()
        expanded += 1
        for neighbour, _cost in local.neighbours(current):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return frozenset(seen)


def _can_touch(local: LocalMap, column: tuple, block) -> bool:
    """Could a player standing at `column` hit `block`?

    In reach by the same eye-to-face measure the mining skills use, and with
    nothing the map knows of in between -- a wall, or the leaves of the very
    tree being approached. Reach through a wall is not reach."""
    ground = local.ground_at(*column)
    if ground is None:
        return False
    feet = (column[0] + 0.5, ground + 1, column[1] + 0.5)
    try:
        target = (int(block.x), int(block.y), int(block.z))
    except (AttributeError, TypeError, ValueError):
        return False
    if not aiming.SHARED.within_reach(feet, target):
        return False
    eye = (feet[0], feet[1] + aiming.EYE_HEIGHT, feet[2])
    return local.sight_is_clear(eye, aiming.target_point(target, feet),
                                skip=(column, (target[0], target[2])))


def _approach(local: LocalMap, reachable, block):
    """The nearest place to stand for `block`: touching it if possible,
    otherwise up to APPROACH_RINGS columns out and still within reach."""
    if not local.usable:
        return None
    try:
        bx, bz = int(block.x), int(block.z)
    except (AttributeError, TypeError, ValueError):
        return None

    here = (local.origin[0], local.origin[2])
    for ring in range(1, APPROACH_RINGS + 1):
        columns = [(bx + dx, bz + dz)
                   for dx in range(-ring, ring + 1)
                   for dz in range(-ring, ring + 1)
                   if max(abs(dx), abs(dz)) == ring]
        columns.sort(key=lambda c: abs(c[0] - here[0]) + abs(c[1] - here[1]))
        for column in columns:
            if column not in reachable or not local.standable(*column):
                continue
            # Next to it counts whatever the height: the skill measures
            # reach again when it gets there, and says so if a log is too
            # high. Further out is only worth walking to if it is in reach.
            if ring > 1 and not _can_touch(local, column, block):
                continue
            return column
    return None


def approach_column(state, block, max_nodes: int = MAX_NODES):
    """A standable column near `block` that there is a route to, or None.

    You cannot path INTO a block -- a log occupies the space you would stand
    in. So the goal is somewhere beside it: a neighbour if one can be
    reached, otherwise a column a little further out that is still within
    reach. Returning None is the honest answer to "can I get to that tree",
    and is what stops a skill setting off towards something across a
    ravine."""
    local = LocalMap.from_state(state)
    return _approach(local, reachable_columns(local, max_nodes), block)


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
    "nearest_entity", "approach_column", "reachable_columns",
    "is_walkable", "is_known",
    "reachable", "summarise",
]
