"""
Cells for the digging tests, and DigWorld: a voxel Minecraft to dig in.

Cells are the bridge's grid shape: {(x, y, z): (name, solid, fluid)}, short
names, fluid None or "water" / "lava" / "flowing_water" / "flowing_lava".
In a plain cells dict a cell left out is unknown -- exactly as the grid
leaves out a cell in an unloaded chunk.

DigWorld is both the state source and the controller for a TaskRunner. It
is crude on purpose, and every simplification is a way the real game may
disagree: blocks break if the hold is long enough for the held tool; a
block that falls drops into the cell opened under it; water and lava flow
one cell into an opened cell beside them; the player walks one stride at a
time, cannot pass a solid cell at feet or head height, falls when nothing
is under any corner of the body, and takes damage for every block fallen
past three. The near_blocks grid it reports is every cell from five below
the feet to three above within four, as the mod sends it.
"""

from __future__ import annotations

import math

from minecraft import mining as mining_mod
from minecraft import mod_bridge
from minecraft import navigation as nav
from minecraft import action_spec
from minecraft.controller import ActionResult
from minecraft.state import (BlockRef, EXACT, EntityRef, ItemStack,
                             NearSnapshot, OreHit, OreSnapshot, WorldState)
from minecraft.task_runner import DISPATCH

AIR = ("air", False, None)
STONE = ("stone", True, None)
WATER = ("water", False, "water")
LAVA = ("lava", False, "lava")


def block(name, solid=True, fluid=None):
    return (name, solid, fluid)


def rock(feet=(0, 60, 0), xs=(-8, 12), ys=(44, 72), zs=(-6, 6),
         name="stone"):
    """Solid `name` everywhere in the box, with air at the player's feet
    and head."""
    cells = {(x, y, z): (name, True, None)
             for x in range(xs[0], xs[1] + 1)
             for y in range(ys[0], ys[1] + 1)
             for z in range(zs[0], zs[1] + 1)}
    x, y, z = feet
    cells[(x, y, z)] = AIR
    cells[(x, y + 1, z)] = AIR
    return cells


def put(cells, cell, entry):
    cells[tuple(cell)] = entry
    return cells


def hollow(cells, *where):
    """Air at each cell given."""
    for cell in where:
        cells[tuple(cell)] = AIR
    return cells


DROPS = {
    "stone": "cobblestone", "deepslate": "cobbled_deepslate",
    "grass_block": "dirt", "coal_ore": "coal", "deepslate_coal_ore": "coal",
    "iron_ore": "raw_iron", "deepslate_iron_ore": "raw_iron",
    "copper_ore": "raw_copper", "gold_ore": "raw_gold",
    "diamond_ore": "diamond", "deepslate_diamond_ore": "diamond",
}

_FALLS = ("sand", "red_sand", "gravel")


class DigWorld:
    """Rock to dig in, with a player, a hotbar, and the bridge's view."""

    WALK_SPEED = 4.3
    EYE = 1.62
    REACH = 4.5
    HALF = 0.3
    STRIDE = 0.05
    PICKUP = 1.425

    def __init__(self, cells, position=(0.5, 60.0, 0.5), yaw=-90.0,
                 pitch=0.0, hotbar=("stone_pickaxe",), default=STONE,
                 unknown=(), health=20.0, singleplayer=True, grid=True):
        self.cells = dict(cells)
        self.default = default
        self.x, self.y, self.z = (float(v) for v in position)
        self.yaw, self.pitch = float(yaw), float(pitch)
        self.slots = {i: [name, 1] for i, name in enumerate(hotbar) if name}
        self.selected = 0
        self.health = float(health)
        self.unknown = set(unknown)
        self.singleplayer = singleplayer
        self.grid = grid
        self.drops = []                     # [name, (x, y, z)]
        self.broken = []                    # (cell, feet when broken)
        self.entered_fluid = []
        self.actions = 0
        self.script = {}                    # action number -> fn(world)
        self.reads = 0
        self.mobs = []                      # [name, (x, y, z)]: hostile
        self.falls = []                     # blocks fallen, each time
        self.visited = {self.feet()}        # every feet cell stood in
        self.guard = ""                     # e.g. "session_expired"

    # -- the world --
    def at(self, cell):
        return self.cells.get(tuple(cell), self.default)

    def solid(self, cell) -> bool:
        return bool(self.at(cell)[1])

    def feet(self) -> tuple:
        return (math.floor(self.x), math.floor(self.y), math.floor(self.z))

    def held(self):
        entry = self.slots.get(self.selected)
        return entry[0] if entry else None

    def count(self, name) -> int:
        return sum(c for n, c in self.slots.values() if n == name)

    # -- the state source --
    def read(self):
        self.reads += 1
        fx, fy, fz = self.feet()
        cells = {}
        if self.grid:
            for dy in range(-5, 4):
                for dz in range(-4, 5):
                    for dx in range(-4, 5):
                        cell = (fx + dx, fy + dy, fz + dz)
                        if cell not in self.unknown:
                            cells[cell] = self.at(cell)
        near = NearSnapshot(
            origin=(fx, fy, fz), radius=4, below=5, above=3,
            blocks=tuple(), cells=cells if self.grid else None,
            complete=(len(cells) == 729) if self.grid else None)
        hit = self.crosshair()
        target = (BlockRef(name=hit[1], x=hit[0][0], y=hit[0][1],
                           z=hit[0][2], face=hit[2])
                  if hit else BlockRef(name="air"))
        stacks = tuple(ItemStack(slot=i, name=n, count=c)
                       for i, (n, c) in sorted(self.slots.items()) if c)
        held = self.slots.get(self.selected)
        drops = tuple(EntityRef(name="item", category="item", hostile=False,
                                position=where,
                                distance=math.dist(where,
                                                   (self.x, self.y, self.z)),
                                item=ItemStack(name=name, count=1))
                      for name, where in self.drops)
        drops += tuple(EntityRef(name=name, category="monster", hostile=True,
                                 position=where,
                                 distance=math.dist(where,
                                                    (self.x, self.y, self.z)))
                       for name, where in self.mobs)
        return WorldState(
            position=(self.x, self.y, self.z), rotation=(self.yaw, self.pitch),
            health=self.health, hunger=20.0, inventory=stacks,
            selected_slot=self.selected,
            held_item=(ItemStack(slot=self.selected, name=held[0],
                                 count=held[1]) if held else None),
            target_block=target, nearby_entities=drops, near=near,
            on_ground=True, game_mode="survival",
            singleplayer=self.singleplayer,
            ores=self._ore_scan() if self.singleplayer else None,
            features=tuple(mod_bridge.REQUIRED_FEATURES),
            source="test", confidence=EXACT)

    def _ore_scan(self):
        """What the mod's ore scan would list: the placed ore within 24
        sideways, 32 down and 16 up, nearest first, with `exposed` and
        `fluid_near` worked out the same way."""
        fx, fy, fz = self.feet()
        hits = []
        for cell, entry in self.cells.items():
            if not entry[0].endswith("_ore"):
                continue
            dx, dy, dz = cell[0] - fx, cell[1] - fy, cell[2] - fz
            if abs(dx) > 24 or abs(dz) > 24 or dy < -32 or dy > 16:
                continue
            exposed = any(
                self.at((cell[0] + a, cell[1] + b, cell[2] + c))[0] == "air"
                or self.at((cell[0] + a, cell[1] + b, cell[2] + c))[2]
                for a, b, c in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                                (0, 0, 1), (0, 0, -1)))
            fluid = any(self.at((cell[0] + a, cell[1] + b, cell[2] + c))[2]
                        for a in range(-2, 3) for b in range(-2, 3)
                        for c in range(-2, 3))
            hits.append((dx * dx + dy * dy + dz * dz,
                         OreHit(name=entry[0], x=cell[0], y=cell[1],
                                z=cell[2], exposed=bool(exposed),
                                fluid_near=bool(fluid))))
        hits.sort(key=lambda h: (h[0], h[1].position))
        return OreSnapshot(origin=(fx, fy, fz), radius=24, down=32, up=16,
                           complete=True, ores=tuple(h[1] for h in hits))

    def crosshair(self):
        """(cell, name, face) of the first block the view ray meets within
        reach -- fluid is not something the crosshair stops on."""
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        ux = -math.sin(yaw) * math.cos(pitch)
        uy = -math.sin(pitch)
        uz = math.cos(yaw) * math.cos(pitch)
        ex, ey, ez = self.x, self.y + self.EYE, self.z
        previous = (math.floor(ex), math.floor(ey), math.floor(ez))
        distance = 0.0
        while distance <= self.REACH:
            cell = (math.floor(ex + ux * distance), math.floor(ey + uy * distance),
                    math.floor(ez + uz * distance))
            if cell != previous:
                entry = self.at(cell)
                if entry[0] not in ("air", "cave_air") and entry[2] is None:
                    return cell, entry[0], _entered(previous, cell)
                previous = cell
            distance += 0.01
        return None

    # -- the controller --
    def _guard(self):
        return self.guard

    def _result(self, name, params, ms=200, **extra):
        self.actions += 1
        hook = self.script.get(self.actions)
        if hook is not None:
            hook(self)
        return ActionResult(ok=extra.pop("ok", True), action=name,
                            requested=dict(params or {}),
                            actual_duration_ms=ms, **extra)

    def look(self, params):
        limit = action_spec.MAX_LOOK_DELTA_PX
        dx = max(-limit, min(int(params.get("dx", 0)), limit))
        dy = max(-limit, min(int(params.get("dy", 0)), limit))
        self.yaw = ((self.yaw + dx / nav.PIXELS_PER_DEGREE + 180.0) % 360.0) \
            - 180.0
        self.pitch = max(-90.0, min(90.0,
                                    self.pitch + dy / nav.PIXELS_PER_DEGREE))
        return self._result("look", params)

    def hotbar_select(self, params):
        self.selected = int(params.get("slot", 1)) - 1
        return self._result("hotbar_select", params, 50)

    def mine(self, params):
        seconds = float(params.get("duration", 0))
        hit = self.crosshair()
        expect = params.get("expect_at")
        if expect is not None and (hit is None
                                   or hit[0] != tuple(int(v) for v in expect)):
            return self._result(
                "mine", params, 0, ok=False,
                stopped_reason="target_not_confirmed",
                error_class="TargetNotConfirmed",
                error="the crosshair is not on that block; nothing pressed")
        if hit is not None:
            cell, name, _face = hit
            estimate = mining_mod.estimate_break_duration(
                name, held_item=self.held() or "")
            needed = estimate.seconds
            if needed is not None and seconds >= needed:
                self._break(cell, name, estimate.drops)
        return self._result("mine", params, int(seconds * 1000))

    def _break(self, cell, name, drops):
        self.broken.append((cell, self.feet()))
        self.cells[cell] = AIR
        if drops:
            self.drops.append([DROPS.get(name, name),
                               (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)])
        above = (cell[0], cell[1] + 1, cell[2])
        while self.at(above)[0] in _FALLS:           # it falls in
            self.cells[(above[0], above[1] - 1, above[2])] = self.at(above)
            self.cells[above] = AIR
            above = (above[0], above[1] + 1, above[2])
        for side in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, 0, 1),
                     (0, 0, -1)):
            near = self.at((cell[0] + side[0], cell[1] + side[1],
                            cell[2] + side[2]))
            if near[2] in ("water", "lava"):
                self.cells[cell] = (near[0], False, f"flowing_{near[2]}")
                break
        self._settle()

    def move(self, params):
        seconds = float(params.get("duration", 0.3))
        self._walk(seconds * self.WALK_SPEED, jumping=False)
        return self._result("move", params, int(seconds * 1000))

    def move_and_jump(self, params):
        seconds = float(params.get("duration", 0.4))
        self._walk(min(seconds * self.WALK_SPEED, 1.6), jumping=True)
        return self._result("move_and_jump", params, int(seconds * 1000))

    def jump(self, params):
        return self._result("jump", params, 100)

    def __getattr__(self, name):
        if name in DISPATCH:
            return lambda params: self._result(name, params)
        raise AttributeError(name)

    # -- the body --
    def _body_fits(self, x, y, z) -> bool:
        for ox in (-self.HALF, self.HALF):
            for oz in (-self.HALF, self.HALF):
                cx, cz = math.floor(x + ox), math.floor(z + oz)
                for cy in (math.floor(y), math.floor(y) + 1):
                    if self.solid((cx, cy, cz)):
                        return False
        return True

    def _supported(self) -> bool:
        below = math.floor(self.y) - 1
        return any(self.solid((math.floor(self.x + ox), below,
                               math.floor(self.z + oz)))
                   for ox in (-self.HALF, self.HALF)
                   for oz in (-self.HALF, self.HALF))

    def _walk(self, distance, jumping):
        radians = math.radians(self.yaw)
        ux, uz = -math.sin(radians), math.cos(radians)
        rose = False
        travelled = 0.0
        while travelled < distance - 1e-9:
            stride = min(self.STRIDE, distance - travelled)
            nx, nz = self.x + ux * stride, self.z + uz * stride
            if self._body_fits(nx, self.y, nz):
                self.x, self.z = nx, nz
            elif jumping and not rose and self._body_fits(self.x, self.y + 1,
                                                          self.z) \
                    and self._body_fits(nx, self.y + 1, nz):
                self.y += 1
                self.x, self.z = nx, nz
                rose = True
            else:
                break
            travelled += stride
            self._settle()
        self._settle()

    def _settle(self):
        """Fall until something is under the body; three blocks free."""
        fell = 0
        while not self._supported() and fell < 64:
            self.y -= 1
            fell += 1
        if fell:
            self.falls.append(fell)
        if fell > 3:
            self.health = max(0.0, self.health - (fell - 3))
        self.visited.add(self.feet())
        here = self.at(self.feet())
        if here[2] is not None:
            self.entered_fluid.append(self.feet())
            if "lava" in here[2]:
                self.health = max(0.0, self.health - 4)
        for drop in list(self.drops):
            where = drop[1]
            if abs(where[0] - self.x) <= self.PICKUP \
                    and abs(where[2] - self.z) <= self.PICKUP \
                    and abs(where[1] - (self.y + 0.5)) <= 2.0:
                self.drops.remove(drop)
                self._give(drop[0])

    def _give(self, name):
        for entry in self.slots.values():
            if entry[0] == name:
                entry[1] += 1
                return
        free = next(i for i in range(36) if i not in self.slots)
        self.slots[free] = [name, 1]


# ── The named worlds ─────────────────────────────────────────────────────────
#
# Stone everywhere (DigWorld's default), the player standing at (0, 60, 0)
# in a two-high hole, a stone pickaxe in the hotbar. Each world is the one
# danger it is named for, placed on or beside the obvious route.

START = (0.5, 60.0, 0.5)
HOLE = {(0, 60, 0): AIR, (0, 61, 0): AIR}
VEIN = {(6, 56, 0): block("iron_ore"), (6, 56, 1): block("iron_ore"),
        (7, 56, 0): block("iron_ore")}
"""Three iron ore four below the feet and six east, joined face to face."""


def _world(*parts, **options):
    cells = dict(HOLE)
    for part in parts:
        cells.update(part)
    return DigWorld(cells, position=START, **options)


def stone_volume(**options):
    """Nothing but stone: the level tunnel and the staircase."""
    return _world(**options)


def buried_vein(**options):
    """VEIN, buried: no air beside any of it."""
    return _world(VEIN, **options)


def lava_pocket(**options):
    """VEIN, with two cells of lava under the staircase to it, and another
    iron ore far to the west."""
    return _world(VEIN, {(3, 54, 2): LAVA, (3, 54, 3): LAVA,
                         (-8, 56, 0): block("iron_ore")}, **options)


def water_pocket(**options):
    """Still water lying on the rock just above the first cells east: the
    head cell of the first stair has water on top of it."""
    return _world({(1, 62, 0): WATER, (2, 62, 0): WATER,
                   (1, 62, 1): WATER}, **options)


def gravel_ceiling(**options):
    """Gravel two cells along the level tunnel east, just above its head
    height."""
    return _world({(2, 62, 0): block("gravel"), (3, 62, 0): block("gravel"),
                   (2, 63, 0): block("gravel")}, **options)


def cave_opening(**options):
    """A cave two and three cells east, its floor level with the feet."""
    return _world(hollow({}, (3, 60, 0), (3, 61, 0), (4, 60, 0), (4, 61, 0),
                         (3, 60, 1), (3, 61, 1), (4, 60, 1), (4, 61, 1)),
                  **options)


def floating_ore(**options):
    """Coal ore hanging in the middle of a cave four to six east: exposed,
    out of reach, with nothing under the cell beside it for a long way."""
    cave = hollow({}, *[(x, y, z) for x in range(4, 7)
                       for y in range(55, 64) for z in range(-1, 2)])
    cave[(5, 60, 0)] = block("coal_ore")
    return _world(cave, **options)


def mineshaft(**options):
    """Planks -- a buried mineshaft -- across the level tunnel east."""
    return _world({(2, 60, 0): block("oak_planks"),
                   (2, 61, 0): block("oak_planks")}, **options)


def hidden_shaft(**options):
    """Air three deep under the first stair down east: the floor it would
    land on is not there."""
    return _world({(1, 58, 0): AIR, (1, 57, 0): AIR, (1, 56, 0): AIR},
                  **options)


def magma_floor(**options):
    """Magma under the first stair down east: solid and dry, and it burns
    whoever stands on it."""
    return _world({(1, 58, 0): block("magma_block")}, **options)


WORLDS = {
    "stone_volume": stone_volume, "buried_vein": buried_vein,
    "lava_pocket": lava_pocket, "water_pocket": water_pocket,
    "gravel_ceiling": gravel_ceiling, "cave_opening": cave_opening,
    "floating_ore": floating_ore, "mineshaft": mineshaft,
    "hidden_shaft": hidden_shaft, "magma_floor": magma_floor,
}


def _entered(previous, cell) -> str:
    dx, dy, dz = (previous[0] - cell[0], previous[1] - cell[1],
                  previous[2] - cell[2])
    if dy > 0:
        return "up"
    if dy < 0:
        return "down"
    if dx > 0:
        return "east"
    if dx < 0:
        return "west"
    return "south" if dz > 0 else "north"


__all__ = ["AIR", "STONE", "WATER", "LAVA", "DROPS", "block", "rock", "put",
           "hollow", "DigWorld", "START", "HOLE", "VEIN", "WORLDS",
           "stone_volume", "buried_vein", "lava_pocket", "water_pocket",
           "gravel_ceiling", "cave_opening", "floating_ore", "mineshaft",
           "hidden_shaft", "magma_floor"]
