"""
minecraft/skills/collect_blocks.py -- collect_blocks: stone, dirt, sand,
gravel, ores, only where a block is exposed and safe to break. Moved here
unchanged from collect.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from minecraft import navigation as nav

from minecraft.skills.navigate import block_label

from minecraft.skills.collect import _Gatherer


# ── Gathering any block ──────────────────────────────────────────────────────

GATHERABLE = {
    # what is asked for: (blocks to break, items they drop, what to call it)
    "stone": (("stone",), ("cobblestone",), "cobblestone"),
    "deepslate": (("deepslate",), ("cobbled_deepslate",),
                  "cobbled deepslate"),
    "dirt": (("dirt", "grass_block"), ("dirt",), "dirt"),
    "sand": (("sand",), ("sand",), "sand"),
    "red_sand": (("red_sand",), ("red_sand",), "red sand"),
    "gravel": (("gravel",), ("gravel", "flint"), "gravel"),
    "coal": (("coal_ore", "deepslate_coal_ore"), ("coal",), "coal"),
    "iron": (("iron_ore", "deepslate_iron_ore"), ("raw_iron",), "raw iron"),
    "copper": (("copper_ore", "deepslate_copper_ore"), ("raw_copper",),
               "raw copper"),
}
"""collect_blocks' drop table. Success is counted in the third column's
items arriving in the inventory -- never in blocks disappearing: stone
broken by hand disappears and drops nothing."""

GATHER_ALIASES = {
    "cobblestone": "stone", "cobble": "stone", "cobbled_deepslate": "deepslate",
    "grass": "dirt", "grass_block": "dirt", "flint": "gravel",
    "coal_ore": "coal", "iron_ore": "iron", "raw_iron": "iron",
    "copper_ore": "copper", "raw_copper": "copper",
}


def exposed(local, block) -> bool:
    """Can `block` be hit without breaking another block first?

    Judged from the scan: it is the floor of its own column (open above);
    the open air over a neighbouring column reaches its height (a side in a
    cliff face or a cave wall); or it is the first solid block over its own
    column's headroom (a ceiling). Anything else may be buried, and taking
    it would mean digging -- which this does not do."""
    return open_face(local, block) is not None


def open_face(local, block, from_position=None):
    """The face of `block` that is open to the air, by the scan: "up" for
    the floor of its column, a side where the air over a neighbouring
    column reaches its height (the nearest to `from_position` when several
    are), "down" for a ceiling -- or None.

    A block set in the ground has one open face, the top. Aiming at the
    side turned towards the player -- the default -- aims into the grass
    beside it, and the crosshair lands on the grass."""
    try:
        x, y, z = int(block.x), int(block.y), int(block.z)
    except (AttributeError, TypeError, ValueError):
        return None
    ground = local.ground_at(x, z)
    if ground == y:
        return "up"
    sides = []
    for face, (nx, nz) in (("east", (x + 1, z)), ("west", (x - 1, z)),
                           ("south", (x, z + 1)), ("north", (x, z - 1))):
        floor = local.ground_at(nx, nz)
        if floor is None or floor >= y:
            continue
        room = local.clearance_at(nx, nz)
        if room is None or y <= floor + room:
            sides.append((face, (nx + 0.5, nz + 0.5)))
    if sides:
        if from_position is not None:
            sides.sort(key=lambda f: math.hypot(
                f[1][0] - from_position[0], f[1][1] - from_position[2]))
        return sides[0][0]
    if ground is not None:
        room = local.clearance_at(x, z)
        if room is not None and ground < y and y == ground + room + 1 \
                and room < nav.MAX_REPORTED_CLEARANCE:
            return "down"
    return None


def liquid_beside(state, position) -> str | None:
    """The water or lava touching `position` in the scan, or None. Breaking
    a block beside a source lets it flow -- onto the player, for lava."""
    x, y, z = position
    touching = {(x + 1, y, z), (x - 1, y, z), (x, y + 1, z), (x, y - 1, z),
                (x, y, z + 1), (x, y, z - 1)}
    for source in (getattr(state, "notable_blocks", None) or (),
                   getattr(state, "surface", None) or ()):
        for block in source:
            name = str(getattr(block, "name", ""))
            if (name in nav.LIQUIDS or "lava" in name or "water" in name) \
                    and block.position in touching:
                return " ".join(name.split("_"))
    return None


def underfoot(state, position) -> bool:
    """Is `position` under any part of the player's feet? The body is 0.6
    wide, so near a column's edge it stands on two or four of them."""
    try:
        px, py, pz = state.position
        x, y, z = position
    except (TypeError, ValueError):
        return True
    if y >= math.floor(py):
        return False
    half = nav.PLAYER_HALF_WIDTH
    columns = {(math.floor(px + dx), math.floor(pz + dz))
               for dx in (-half, half) for dz in (-half, half)}
    return (x, z) in columns


@dataclass
class CollectBlocks(_Gatherer):
    """collect_blocks: `count` of an item that comes from breaking blocks --
    cobblestone from stone, dirt, sand, gravel, coal, raw iron, raw copper.

    The gatherer collect_logs is built on, with GATHERABLE's drop table.
    Only blocks it can reach and that are exposed (it never digs), never
    one under the player's feet, and never one touching water or lava in
    the scan. The right hotbar tool first; a block nothing in the hotbar
    can harvest is refused before any swing. Done is counted from the drop
    in the inventory, so without the inventory it does not claim success."""

    target: str = "stone"
    _seen_state: object = None        # the state the candidates were judged in

    name = "collect_blocks"

    def _entry(self):
        key = "_".join(str(self.target or "").strip().lower().split())
        key = key.split(":")[-1]
        return GATHERABLE.get(GATHER_ALIASES.get(key, key))

    def _wanted_names(self) -> frozenset:
        entry = self._entry()
        return frozenset(entry[0]) if entry else frozenset()

    def _drop_names(self) -> frozenset:
        entry = self._entry()
        return frozenset(entry[1]) if entry else frozenset()

    def _noun(self, plural: bool = False) -> str:
        entry = self._entry()
        return entry[2] if entry else str(self.target)

    @property
    def goal(self) -> str:
        return f"collect {self.count} {self._noun(True)}"

    @property
    def failed(self) -> bool:
        # The drop in the inventory is the only proof that counts.
        return (self._blind or not self._can_count
                or self._collected < self.count)

    def _plan(self, state, step_index: int, history: tuple):
        if self._entry() is None:
            self._walk_failed = (
                f"I do not know how to collect {self.target!r}. I can "
                f"collect: {', '.join(sorted(GATHERABLE))}.")
            return None
        return super()._plan(state, step_index, history)

    def _may_break(self, state, position, name) -> bool:
        return not underfoot(state, position) \
            and liquid_beside(state, position) is None

    def _aim_face(self, state, target):
        return open_face(nav.LocalMap.from_state(state), target,
                         getattr(state, "position", None))

    def _candidates(self, state):
        self._seen_state = state
        local = nav.LocalMap.from_state(state)
        return [b for b in nav.blocks_matching(state, self._wanted_names())
                if b.position not in self._skip
                and self._may_break(state, b.position, b.name)
                and exposed(local, b)]

    def _next_target(self, state):
        return nav.nearest_of(state, self._candidates(state),
                              reachable_only=True)

    def _nearest_seen(self, state):
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()))

    def _unreachable_text(self, seen) -> str:
        why = ("it is not reachable without digging, it is under my feet, "
               "or it touches water or lava" if self._seen_state is None
               else self._unreachable_reason(self._seen_state, seen))
        return (f"The nearest {block_label(seen)} I can see, at "
                f"{seen.position}, will not do: {why}.")

    def _unreachable_reason(self, state, seen) -> str:
        if underfoot(state, seen.position):
            return "it is under my feet"
        wet = liquid_beside(state, seen.position)
        if wet:
            return f"it touches {wet}, which would flow in"
        if not exposed(nav.LocalMap.from_state(state), seen):
            return "it is not reachable without digging, and I do not dig"
        return "there is no walkable route to somewhere to stand by it"

    def _progress_text(self) -> str:
        what = self._noun(True)
        if self._can_count:
            return (f"broke {self._broken} block(s) and collected "
                    f"{self._collected} of {self.count} {what}, counted in "
                    f"the inventory")
        return (f"broke {self._broken} block(s), but I cannot see the "
                f"inventory, so I cannot say how much {what} arrived")
