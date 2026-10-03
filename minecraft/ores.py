"""
minecraft/ores.py -- what the bridge's ore scan says is near, in words.

Pure logic over a WorldState: no input, no reads of its own. find_ores
(read-only) and mine_ore use it.

WHY SINGLE-PLAYER ONLY
    The scan lists ore buried in rock, which the player cannot see. On a
    server that is x-ray: against the rules of most servers, and a reason to
    be banned. The mod only scans when the client runs its own world and
    has not opened it to LAN -- LAN worlds count as multiplayer -- and this
    refuses unless the reading says so: a reading that does not say (an
    older jar) is refused too, not assumed.

NOTHING IS PROMISED THAT WAS NOT LISTED
    Every ore named comes from the scan's list, with its coordinates. When the
    list is incomplete -- a chunk not loaded, or more ore than it keeps -- the
    answer says there may be more, rather than "there is none".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from minecraft import digging

DEFAULT_RADIUS = 16
"""How far find_ores looks unless asked otherwise. The scan is a box -- 24
sideways, 32 down, 16 up -- so a radius is capped at its farthest reach."""

ORE_KINDS = ("coal", "iron", "copper", "gold", "redstone", "lapis",
             "diamond", "emerald", "quartz")
"""The kinds a request can name. "quartz" is nether_quartz_ore; "gold"
includes nether_gold_ore."""


@dataclass(frozen=True)
class Hit:
    name: str                  # iron_ore, deepslate_iron_ore ...
    position: tuple            # (x, y, z)
    distance: float            # from the player's eyes' column, in blocks
    depth: int                 # ore y minus feet y: negative is below
    exposed: bool
    fluid_near: bool


def kind_of(name: str) -> str:
    """The ore's kind: deepslate_iron_ore -> iron, nether_quartz_ore ->
    quartz, nether_gold_ore -> gold."""
    base = str(name).split(":")[-1]
    for prefix in ("deepslate_", "nether_"):
        if base.startswith(prefix):
            base = base[len(prefix):]
    if base.endswith("_ore"):
        base = base[:-4]
    return base


def wanted_kind(ore) -> str | None:
    """A request's ore, as a kind: "iron", "Iron ore", "deepslate_iron_ore"
    -> iron. None for no filter."""
    if ore is None or not str(ore).strip():
        return None
    text = "_".join(str(ore).strip().lower().split())
    return kind_of(text)


def refusal(state) -> str:
    """Why ore may not be looked for in this reading, or ""."""
    single = getattr(state, "singleplayer", None)
    if single is False:
        return ("I only look for ore in a single-player world. This is a "
                "server or a world opened to LAN -- LAN worlds count as "
                "multiplayer -- and finding ore inside rock there is x-ray, "
                "against the rules on most servers.")
    if single is not True:
        return ("I only look for ore in a single-player world, and the "
                "installed mod does not say whether this is one (it is "
                "older than this Jarvis). Finding ore inside rock on a "
                "server is x-ray, so I do not guess. Quit Minecraft, run "
                "install_mod.bat, then start Minecraft again.")
    if getattr(state, "ores", None) is None:
        return ("The ore scan has not finished yet -- it takes a second or "
                "two after the world loads. Ask again in a moment.")
    if getattr(state, "position", None) is None:
        return "I cannot read where you are, so I cannot say how far."
    return ""


def find(state, ore=None, radius=DEFAULT_RADIUS) -> list:
    """The listed ore of the wanted kind within `radius` of the player,
    nearest first. Call refusal() first."""
    scan = state.ores
    kind = wanted_kind(ore)
    limit = radius_used(state, radius)
    px, py, pz = state.position
    feet = math.floor(py)
    hits = []
    for found in scan.ores:
        if kind is not None and kind_of(found.name) != kind:
            continue
        distance = math.dist((px, py, pz), (found.x + 0.5, found.y + 0.5,
                                            found.z + 0.5))
        if distance > limit:
            continue
        hits.append(Hit(name=found.name, position=found.position,
                        distance=round(distance, 1),
                        depth=found.y - feet, exposed=found.exposed,
                        fluid_near=found.fluid_near))
    hits.sort(key=lambda h: (h.distance, h.position))
    return hits


def reach(scan) -> int:
    """The farthest the scan looks in any direction."""
    return max(scan.radius, scan.down, scan.up)


def radius_used(state, radius) -> int:
    return max(1, min(int(radius), reach(state.ores)))


def depth_words(depth: int) -> str:
    if depth == 0:
        return "level with your feet"
    if depth < 0:
        return f"{-depth} below your feet"
    return f"{depth} above your feet"


def line(n: int, hit: Hit) -> str:
    where = ", ".join(str(v) for v in hit.position)
    cover = ("exposed (air or fluid beside it)" if hit.exposed
             else "buried (it needs digging to)")
    text = (f"{n}. {' '.join(hit.name.split('_'))} at ({where}): "
            f"{hit.distance} blocks away, {depth_words(hit.depth)}, {cover}")
    if hit.fluid_near:
        text += ", water or lava within 2 blocks of it"
    return text + "."


def describe(state, hits, ore=None, radius=DEFAULT_RADIUS,
             shown: int = 10) -> str:
    """What find_ores says: the list, or that there is none, and whether
    there may be more."""
    kind = wanted_kind(ore)
    what = f"{kind} ore" if kind else "ore"
    limit = radius_used(state, radius)
    scan = state.ores
    capped = (f" (the scan reaches {scan.radius} blocks sideways, "
              f"{scan.down} down and {scan.up} up, so that is as far as I "
              f"look)" if int(radius) > reach(scan) else "")
    more = ""
    if not scan.complete:
        beyond = (f" beyond {scan.complete_within:.0f} blocks"
                  if scan.complete_within else "")
        more = (f" The scan is not complete -- a chunk was not loaded, or "
                f"there was more ore than it lists -- so there may be more"
                f"{beyond}.")
    if not hits:
        return (f"The scan lists no {what} within {limit} blocks of you"
                f"{capped}.{more}")
    head = (f"{len(hits)} {what} block(s) within {limit} blocks{capped}, "
            f"nearest first -- only what the mod's scan listed:")
    lines = [line(n + 1, hit) for n, hit in enumerate(hits[:shown])]
    if len(hits) > shown:
        lines.append(f"... and {len(hits) - shown} more.")
    return "\n".join([head] + lines) + (f"\n{more.strip()}" if more else "")


DROPS = {
    "coal": ("coal",), "iron": ("raw_iron",), "copper": ("raw_copper",),
    "gold": ("raw_gold", "gold_nugget"), "redstone": ("redstone",),
    "lapis": ("lapis_lazuli",), "diamond": ("diamond",),
    "emerald": ("emerald",), "quartz": ("quartz",),
}
"""What each kind of ore drops, for counting what mine_ore got from the
inventory."""


@dataclass(frozen=True)
class Choice:
    """The ore mine_ore goes for, where it will stand to mine it, and how
    much digging that is."""
    hit: Hit
    stand: tuple | None        # the feet cell to dig to; None: in reach now
    stairs: int
    blocks: int                # most it will break on the way

    def describe(self) -> str:
        where = ", ".join(str(v) for v in self.hit.position)
        ore = " ".join(self.hit.name.split("_"))
        head = (f"the {ore} at ({where}), {self.hit.distance} blocks away "
                f"and {depth_words(self.hit.depth)}")
        if self.stand is None:
            return f"{head}: it is exposed and within reach, so no digging"
        stand = ", ".join(str(v) for v in self.stand)
        return (f"{head}: a staircase of {self.stairs} stair(s) to "
                f"({stand}) beside it, breaking at most {self.blocks} "
                f"blocks, then the rest of the vein I can reach from there")


def stand_cell(feet, ore) -> tuple:
    """The cell to stand in to mine `ore`: beside it, on the player's side,
    with the ore at feet height."""
    dx, dz = ore[0] - feet[0], ore[2] - feet[2]
    if dx == 0 and dz == 0:
        return (ore[0] + 1, ore[1], ore[2])
    if abs(dx) >= abs(dz):
        return (ore[0] - (1 if dx > 0 else -1), ore[1], ore[2])
    return (ore[0], ore[1], ore[2] - (1 if dz > 0 else -1))


def dig_estimate(feet, stand) -> tuple:
    """(stairs, the most blocks they break): digging.blocks_needed."""
    return digging.blocks_needed(feet, stand)


def choose(state, ore=None, radius=DEFAULT_RADIUS, skip=(),
           within_reach=None, start=None, broken=0):
    """(the Choice, why others were passed over): the nearest listed ore of
    the wanted kind that can be reached safely within the limits, or None.

    Passed over: an ore with water or lava within 2 of it, one deeper than
    MAX_DEPTH below `start` or further than MAX_HORIZONTAL from it (the
    feet, unless a task under way began elsewhere), one that would take
    more than the MAX_BROKEN blocks left after `broken`, and those in
    `skip` (ore an earlier try found unsafe)."""
    passed = []
    feet = digging.feet_of(state.position)
    start = tuple(start) if start is not None else feet
    skipped = {tuple(c) for c in skip}
    for hit in find(state, ore, radius):
        where = ", ".join(str(v) for v in hit.position)
        if hit.position in skipped:
            continue
        if hit.fluid_near:
            passed.append(f"({where}): water or lava within 2 blocks of it")
            continue
        if hit.exposed and within_reach is not None \
                and within_reach(state.position, hit.position):
            return Choice(hit=hit, stand=None, stairs=0, blocks=0), passed
        stand = stand_cell(feet, hit.position)
        if start[1] - stand[1] > digging.MAX_DEPTH:
            passed.append(f"({where}): more than {digging.MAX_DEPTH} "
                          f"blocks down")
            continue
        if math.hypot(stand[0] - start[0], stand[2] - start[2]) \
                > digging.MAX_HORIZONTAL:
            passed.append(f"({where}): more than {digging.MAX_HORIZONTAL} "
                          f"blocks away")
            continue
        stairs, blocks = dig_estimate(feet, stand)
        if blocks + broken >= digging.MAX_BROKEN:
            passed.append(f"({where}): it would take about {blocks} blocks "
                          f"of digging, more than the "
                          f"{digging.MAX_BROKEN - broken} this task may "
                          f"still break")
            continue
        return Choice(hit=hit, stand=stand, stairs=stairs,
                      blocks=blocks), passed
    return None, passed


def none_chosen(state, ore=None, radius=DEFAULT_RADIUS, passed=(),
                skip=()) -> str:
    """Why choose() found nothing: the scan lists none, or every one listed
    was passed over -- each with its reason."""
    kind = wanted_kind(ore)
    what = f"{kind} ore" if kind else "ore"
    if not find(state, ore, radius):
        return (f"{describe(state, [], ore, radius)} So I am not digging "
                f"for any: I only go for ore the scan lists.")
    reasons = list(passed[:3])
    if len(passed) > 3:
        reasons.append(f"and {len(passed) - 3} more")
    if skip:
        reasons.append("the vein I stopped short of before")
    return (f"There is no {what} I can safely reach in the scan: "
            f"{'; '.join(reasons)}.")


def vein_of(state, position) -> set:
    """The listed ore joined to `position` face to face, of its kind: one
    vein, as far as the scan lists it."""
    listed = {hit.position: kind_of(hit.name) for hit in state.ores.ores}
    start = tuple(position)
    kind = listed.get(start)
    if kind is None:
        return {start}
    vein, todo = set(), [start]
    while todo:
        cell = todo.pop()
        if cell in vein:
            continue
        vein.add(cell)
        for d in digging.SIDES:
            side = (cell[0] + d[0], cell[1] + d[1], cell[2] + d[2])
            if listed.get(side) == kind and side not in vein:
                todo.append(side)
    return vein


__all__ = ["DEFAULT_RADIUS", "ORE_KINDS", "DROPS", "Hit", "Choice",
           "kind_of", "wanted_kind", "refusal", "find", "radius_used",
           "describe", "depth_words", "stand_cell", "dig_estimate",
           "choose", "none_chosen", "vein_of"]
