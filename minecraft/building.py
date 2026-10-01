"""
minecraft/building.py — which cell may take a block, and what it goes
against.

Pure rules over what the bridge reports: the terrain scan's columns (a
floor, the passable space above it, what a player standing there would be
inside), the notable blocks, and the cells a task has itself placed and
verified. Nothing here presses anything.

A cell is only ever called empty when the scan saw it empty: the space
above a floor, up to the clearance it reports. Below a floor, above the
measured headroom, or in a column nobody looked at, it is UNKNOWN -- and a
block is never placed into unknown.

Only heapq, math and dataclasses may be imported here, like navigation.py.
"""

from __future__ import annotations

from dataclasses import dataclass

_WOODS = ("oak", "spruce", "birch", "jungle", "acacia", "dark_oak",
          "mangrove", "cherry", "pale_oak", "crimson", "warped", "bamboo")

BUILDING_BLOCKS = frozenset({
    "dirt", "coarse_dirt", "cobblestone", "mossy_cobblestone", "stone",
    "stone_bricks", "smooth_stone", "cobbled_deepslate", "deepslate_bricks",
    "andesite", "diorite", "granite", "polished_andesite",
    "polished_diorite", "polished_granite", "sandstone", "red_sandstone",
    "bricks", "mud_bricks", "blackstone", "netherrack",
} | {f"{wood}_planks" for wood in _WOODS})
"""What the build tasks place. Full, solid cubes whose block has the item's
own name -- so "a cobblestone block is now at that cell" is checkable from
the crosshair -- that nothing pulls down (sand and gravel fall), and that
do nothing when right-clicked."""

REPLACEABLE = frozenset({
    "air", "cave_air", "void_air", "short_grass", "grass", "tall_grass",
    "fern", "large_fern", "dead_bush", "bush", "short_dry_grass",
    "tall_dry_grass", "vine", "glow_lichen", "hanging_roots",
    "crimson_roots", "warped_roots", "nether_sprouts", "leaf_litter",
})
"""What a placed block may take the place of: air, and the plants the game
itself replaces when a block is placed into them. Not water or lava, and
not fire -- the game would, but a build task does not."""

EMPTY = "empty"
REPLACE = "replaceable"
OCCUPIED = "occupied"
UNKNOWN = "unknown"

MAX_CLEARANCE = 4
"""The furthest the mod counts headroom above a floor (MarkLivBridge)."""

_STAND_SPACE = 2
"""A standing player's height in blocks: the mod's `cover` is the first
non-air block in this much space above a floor."""

NEIGHBOURS = (
    ((0, -1, 0), "up"),        # the block below: place on its top
    ((1, 0, 0), "west"),       # the block east: its west face
    ((-1, 0, 0), "east"),
    ((0, 0, 1), "north"),
    ((0, 0, -1), "south"),
    ((0, 1, 0), "down"),       # the block above: its underside
)
"""Each neighbour of a cell -- as an offset from it -- with the face of that
neighbour which touches the cell. Below first: a block standing on another
is the ordinary case."""

FACE_OFFSETS = {
    "up": (0, 1, 0), "down": (0, -1, 0), "east": (1, 0, 0),
    "west": (-1, 0, 0), "south": (0, 0, 1), "north": (0, 0, -1),
}
"""Where a block placed against a face lands, relative to the face's
block."""


def short(name) -> str:
    return str(name or "").split(":")[-1].strip().lower()


def cell_of(position) -> tuple:
    return (int(position[0]), int(position[1]), int(position[2]))


def offset(cell, delta) -> tuple:
    return (cell[0] + delta[0], cell[1] + delta[1], cell[2] + delta[2])


def lands_at(block, face) -> tuple | None:
    """The cell a block placed against `face` of `block` goes into."""
    delta = FACE_OFFSETS.get(str(face or "").lower())
    return None if delta is None else offset(cell_of(block), delta)


def _notable_at(state, cell):
    for block in (getattr(state, "notable_blocks", None) or ()):
        try:
            if (int(block.x), int(block.y), int(block.z)) == cell:
                return block
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def _column(local, cell):
    entry = getattr(local, "ground", {}).get((cell[0], cell[2]))
    if entry is None:
        return None
    y, name, solid, clearance = entry[0], entry[1], entry[2], entry[3]
    cover = entry[4] if len(entry) > 4 else None
    return y, name, solid, clearance, cover


def what_is_at(local, state, cell, known=None) -> tuple:
    """(verdict, name) for one cell: EMPTY, REPLACE (a plant a block may
    take the place of), OCCUPIED, or UNKNOWN -- from the cells a task has
    placed (`known`: cell -> name), the notable blocks, then the scan.
    `local` is a navigation.LocalMap of `state`."""
    cell = cell_of(cell)
    near = getattr(state, "near", None)
    if near is not None and near.covers(cell):
        # The game's own list of every block around the player: the most
        # direct answer there is, and fresher than a task's own record.
        block = near.block_at(cell)
        if block is None:
            return EMPTY, "air"
        name = short(block.name)
        if block.solid is False:
            return (REPLACE if name in REPLACEABLE else OCCUPIED), name
        return OCCUPIED, name
    if known and cell in known:
        name = short(known[cell])
        return (EMPTY if name in ("air", "cave_air", "void_air")
                else REPLACE if name in REPLACEABLE else OCCUPIED), name
    notable = _notable_at(state, cell)
    if notable is not None:
        return OCCUPIED, short(notable.name)
    column = _column(local, cell)
    if column is None:
        return UNKNOWN, None
    floor, name, _solid, clearance, cover = column
    y = cell[1]
    if y == floor:
        return OCCUPIED, short(name)
    if y < floor or clearance is None:
        return UNKNOWN, None
    if y <= floor + clearance:
        if cover is not None and y <= floor + _STAND_SPACE:
            # The first non-air block in the two above the floor: it is in
            # this cell or the other one, and the scan does not say which.
            plant = short(cover)
            return (REPLACE if plant in REPLACEABLE else OCCUPIED), plant
        return EMPTY, "air"
    if y == floor + clearance + 1 and clearance < MAX_CLEARANCE:
        return OCCUPIED, None             # the block that ended the headroom
    return UNKNOWN, None


def solid_at(local, state, cell, known=None):
    """The name of a block known to be solid at `cell`, or None: one a task
    placed, a notable block, or a column's floor at exactly that height."""
    cell = cell_of(cell)
    near = getattr(state, "near", None)
    if near is not None and near.covers(cell):
        block = near.block_at(cell)
        if block is None or block.solid is not True:
            return None
        return short(block.name)
    if known and cell in known:
        name = short(known[cell])
        return None if name in REPLACEABLE else name
    notable = _notable_at(state, cell)
    if notable is not None:
        return short(notable.name)
    column = _column(local, cell)
    if column is None:
        return None
    floor, name, solid, _clearance, _cover = column
    if cell[1] == floor and solid is True:
        return short(name)
    return None


@dataclass(frozen=True)
class Reference:
    """The block a new block is placed against, and the face of it."""
    position: tuple
    name: str
    face: str


def references(local, state, cell, known=None, unusable=None) -> tuple:
    """Every neighbour of `cell` a block could be placed against, best
    first: solid, known, and not something a right-click would use instead
    (`unusable(name)` -> True for a chest, a door, a crafting table)."""
    cell = cell_of(cell)
    found = []
    for delta, face in NEIGHBOURS:
        where = offset(cell, delta)
        name = solid_at(local, state, where, known)
        if name is None or name in REPLACEABLE:
            continue
        if unusable is not None and unusable(name):
            continue
        found.append(Reference(position=where, name=name, face=face))
    return tuple(found)


PLAYER_HALF_WIDTH = 0.3
PLAYER_HEIGHT = 1.8


def body_overlaps(position, cell) -> bool:
    """Would a player whose feet are at `position` be inside `cell`? The
    game refuses to place a block into the player's own body."""
    try:
        px, py, pz = float(position[0]), float(position[1]), float(position[2])
    except (TypeError, IndexError, ValueError):
        return True
    x, y, z = cell_of(cell)
    eps = 1e-6
    return (px + PLAYER_HALF_WIDTH > x + eps
            and px - PLAYER_HALF_WIDTH < x + 1 - eps
            and py + PLAYER_HEIGHT > y + eps and py < y + 1 - eps
            and pz + PLAYER_HALF_WIDTH > z + eps
            and pz - PLAYER_HALF_WIDTH < z + 1 - eps)


def faces_towards(eye, block, face) -> bool:
    """Is the eye on the outside of that face -- could the face be seen?"""
    x, y, z = cell_of(block)
    face = str(face or "").lower()
    try:
        ex, ey, ez = float(eye[0]), float(eye[1]), float(eye[2])
    except (TypeError, IndexError, ValueError):
        return False
    return {"up": ey > y + 1, "down": ey < y, "east": ex > x + 1,
            "west": ex < x, "south": ez > z + 1, "north": ez < z}.get(
                face, False)


# ── Blueprints ───────────────────────────────────────────────────────────────

MAX_BLUEPRINT_BLOCKS = 64
"""No blueprint places more."""

MAX_BLUEPRINT_REACH = 6
"""Every cell within this many blocks (on each axis) of where the task
began."""

PLANS = ("platform", "wall", "shelter")

PLATFORM_SIZES = range(2, 6)
WALL_LENGTHS = range(2, 9)
WALL_HEIGHT = 2
"""From the ground a wall is two high: a third layer goes on top faces
above a standing player's eyes."""

_FACINGS = ("north", "south", "east", "west")


def _rotate(dx, dz, facing):
    """A canonical offset -- the front facing +z (south) -- turned so the
    front faces `facing`."""
    if facing == "north":
        return -dx, -dz
    if facing == "east":
        return dz, -dx
    if facing == "west":
        return -dz, dx
    return dx, dz


def _step_side(facing) -> str:
    """The side the shelter's step is on (where canonical +x ends up),
    named for the sentence."""
    return {"south": "east", "north": "west", "east": "north",
            "west": "south"}[facing]


@dataclass(frozen=True)
class Blueprint:
    plan: str
    cells: tuple              # absolute cells, bottom layer first
    sentence: str             # the plan, in one sentence, for the user
    roles: tuple = ()         # (cell, role) pairs: wall, roof, step, floor


def blueprint(plan, anchor, facing="south", size=None, item="block"):
    """The named plan's cells around `anchor` -- the centre of its
    footprint, at the level a player standing on the ground occupies --
    or a sentence saying why not. Bottom layer first; within a layer the
    builder chooses the order as it goes."""
    plan = str(plan or "").strip().lower()
    facing = str(facing or "south").strip().lower()
    if facing not in _FACINGS:
        return f"The front faces north, south, east or west -- not {facing!r}."
    ax, ay, az = cell_of(anchor)
    words = " ".join(str(item).split("_"))
    cells, roles = [], []

    def add(dx, dy, dz, role):
        rx, rz = _rotate(dx, dz, facing)
        cell = (ax + rx, ay + dy, az + rz)
        cells.append(cell)
        roles.append((cell, role))

    if plan == "platform":
        n = 3 if size is None else int(size)
        if n not in PLATFORM_SIZES:
            return (f"A platform is {PLATFORM_SIZES[0]} to "
                    f"{PLATFORM_SIZES[-1]} blocks across.")
        low = -(n // 2)
        for dx in range(low, low + n):
            for dz in range(low, low + n):
                add(dx, 0, dz, "floor")
        sentence = (f"a {n} by {n} platform of {words}, one block high, "
                    f"centred on {(ax, ay, az)} -- {len(cells)} blocks")
    elif plan == "wall":
        n = 5 if size is None else int(size)
        if n not in WALL_LENGTHS:
            return (f"A wall is {WALL_LENGTHS[0]} to {WALL_LENGTHS[-1]} "
                    f"blocks long.")
        low = -(n // 2)
        for dy in range(WALL_HEIGHT):
            for dx in range(low, low + n):
                add(dx, dy, 0, "wall")
        runs = "east to west" if facing in ("north", "south") \
            else "north to south"
        sentence = (f"a wall of {words} {n} long and {WALL_HEIGHT} high, "
                    f"running {runs} through {(ax, ay, az)} -- "
                    f"{len(cells)} blocks")
    elif plan == "shelter":
        if size not in (None, 3):
            return "The shelter comes in one size: 3 by 3 by 3."
        ring = [(dx, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1)
                if (dx, dz) != (0, 0)]
        door = (0, 1)
        for dy in (0, 1):
            for dx, dz in ring:
                if (dx, dz) != door:
                    add(dx, dy, dz, "wall")
            if dy == 0:
                # A porch the length of one side wall, to stand on for the
                # roof: a hop onto a single block from beside it carries
                # past it, and the full side catches it whichever way.
                for dz in (-1, 0, 1):
                    add(2, 0, dz, "step")
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                add(dx, 2, dz, "roof")
        sentence = (f"a hollow 3 by 3 by 3 shelter of {words} centred on "
                    f"{(ax, ay, az)}: walls two high with a doorway on the "
                    f"{facing} side, a roof, and a three-block step along "
                    f"the {_step_side(facing)} wall to stand on for the roof "
                    f"-- {len(cells)} blocks")
    else:
        return (f"I know these plans: {', '.join(PLANS)} -- not "
                f"{plan or 'nothing'}.")
    if len(cells) > MAX_BLUEPRINT_BLOCKS:
        return (f"That is {len(cells)} blocks; a blueprint places at most "
                f"{MAX_BLUEPRINT_BLOCKS}.")
    return Blueprint(plan=plan, cells=tuple(cells), sentence=sentence,
                     roles=tuple(roles))


def supported_in_order(cells, ground_y) -> bool:
    """Bottom-up, does every cell have something to be placed against
    when its turn comes: the ground (at `ground_y`, under the bottom layer)
    or a cell earlier in the list?"""
    placed = set()
    for cell in cells:
        below_is_ground = cell[1] - 1 == ground_y
        if not below_is_ground and not any(
                offset(cell, delta) in placed for delta, _f in NEIGHBOURS):
            return False
        placed.add(cell)
    return True


def within(cells, start, reach=MAX_BLUEPRINT_REACH):
    """The cells more than `reach` blocks from `start` on any axis."""
    sx, sy, sz = cell_of(start)
    return tuple(c for c in cells
                 if max(abs(c[0] - sx), abs(c[1] - sy), abs(c[2] - sz))
                 > reach)


__all__ = ["BUILDING_BLOCKS", "EMPTY", "FACE_OFFSETS", "NEIGHBOURS",
           "OCCUPIED", "REPLACE", "REPLACEABLE", "Reference", "UNKNOWN",
           "body_overlaps", "cell_of", "faces_towards", "lands_at",
           "references", "solid_at", "what_is_at", "Blueprint", "PLANS",
           "MAX_BLUEPRINT_BLOCKS", "MAX_BLUEPRINT_REACH", "blueprint",
           "supported_in_order", "within"]
