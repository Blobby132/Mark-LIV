"""
minecraft/ores.py -- what the bridge's ore scan says is near, in words.

Pure logic over a WorldState: no input, no reads of its own. find_ores
(read-only) and mine_ore use it.

WHY SINGLE-PLAYER ONLY
    The scan lists ore buried in rock, which the player cannot see. On a
    server that is x-ray: against the rules of most servers, and a reason to
    be banned. The mod only scans when the client runs its own world, and
    this refuses unless the reading says so -- a reading that does not say
    (an older jar) is refused too, not assumed.

NOTHING IS PROMISED THAT WAS NOT LISTED
    Every ore named comes from the scan's list, with its coordinates. When the
    list is incomplete -- a chunk not loaded, or more ore than it keeps -- the
    answer says there may be more, rather than "there is none".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

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
                "server, and finding ore inside rock on a server is x-ray "
                "-- against the rules on most of them.")
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


__all__ = ["DEFAULT_RADIUS", "ORE_KINDS", "Hit", "kind_of", "wanted_kind",
           "refusal", "find", "radius_used", "describe", "depth_words"]
