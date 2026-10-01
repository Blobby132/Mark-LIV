"""
minecraft/mining.py — how long a block takes to break, and whether it did.

WHY A MODEL AND NOT A CONSTANT
    Every mine used to hold for the same four seconds. An oak log by hand
    takes three; stone by hand takes seven and a half; dirt with a shovel
    takes a fifth of a second. A single number is either too short for the
    hard blocks — and Minecraft throws away breaking progress the instant the
    button comes up, so too short means NOTHING breaks, however often it is
    repeated — or wastefully long for the soft ones.

    So this is Minecraft's own arithmetic, for the blocks and tools that
    matter, and it errs long. The hold also stops the moment the bridge sees
    the block go, so a generous estimate costs nothing when it is wrong in
    the safe direction.

THE FORMULA (Minecraft wiki, "Breaking")
    speed        = tool material multiplier if it is the RIGHT tool, else 1
                   + efficiency^2 + 1, if enchanted and the right tool
    divisor      = 30 if the tool can harvest the block, else 100
    damage/tick  = speed / hardness / divisor
    x 1/5        when not standing on the ground
    x 1/5        when underwater without Aqua Affinity
    ticks        = ceil(1 / damage_per_tick); seconds = ticks / 20

    Some blocks need a tool to DROP anything even though a hand still breaks
    them — stone, ores. Breaking them by hand uses the /100 divisor, which is
    why stone by hand is so slow, and yields nothing. That is reported rather
    than hidden: "I broke it" and "I got it" are different claims.

WHAT IS NOT MODELLED
    Haste, Mining Fatigue, Conduit Power, creative mode (instant), and every
    block not in the table. Unknown blocks get a conservative default and are
    labelled as a guess. None of these can cause a false success: success is
    decided by watching the block go, never by the estimate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

TICKS_PER_SECOND = 20.0

# Hardness, from the game. The blocks an agent actually meets on the surface
# and in the first layers down; anything else falls back to UNKNOWN_HARDNESS.
HARDNESS = {
    # wood
    "oak_log": 2.0, "birch_log": 2.0, "spruce_log": 2.0, "jungle_log": 2.0,
    "acacia_log": 2.0, "dark_oak_log": 2.0, "mangrove_log": 2.0,
    "cherry_log": 2.0, "pale_oak_log": 2.0,
    "oak_planks": 2.0, "birch_planks": 2.0, "spruce_planks": 2.0,
    "crafting_table": 2.5, "chest": 2.5, "barrel": 2.5,
    # earth
    "dirt": 0.5, "grass_block": 0.6, "coarse_dirt": 0.5, "podzol": 0.5,
    "rooted_dirt": 0.5, "mud": 0.5, "sand": 0.5, "red_sand": 0.5,
    "gravel": 0.6, "clay": 0.6, "farmland": 0.6, "snow_block": 0.2,
    "snow": 0.1,
    # stone
    "stone": 1.5, "cobblestone": 2.0, "andesite": 1.5, "diorite": 1.5,
    "granite": 1.5, "deepslate": 3.0, "cobbled_deepslate": 3.5,
    "tuff": 1.5, "calcite": 0.75, "sandstone": 0.8, "netherrack": 0.4,
    "obsidian": 50.0,
    # ores
    "coal_ore": 3.0, "iron_ore": 3.0, "copper_ore": 3.0, "gold_ore": 3.0,
    "redstone_ore": 3.0, "lapis_ore": 3.0, "diamond_ore": 3.0,
    "emerald_ore": 3.0, "deepslate_coal_ore": 4.5, "deepslate_iron_ore": 4.5,
    "deepslate_copper_ore": 4.5, "deepslate_gold_ore": 4.5,
    "deepslate_redstone_ore": 4.5, "deepslate_lapis_ore": 4.5,
    "deepslate_diamond_ore": 4.5, "deepslate_emerald_ore": 4.5,
    # plants: effectively instant
    "short_grass": 0.0, "tall_grass": 0.0, "fern": 0.0, "dandelion": 0.0,
    "poppy": 0.0, "oak_leaves": 0.2, "birch_leaves": 0.2,
    "spruce_leaves": 0.2, "jungle_leaves": 0.2, "acacia_leaves": 0.2,
    "dark_oak_leaves": 0.2,
    # never
    "bedrock": -1.0, "barrier": -1.0, "end_portal_frame": -1.0,
}

UNKNOWN_HARDNESS = 3.0
"""For blocks not in the table. Deliberately on the hard side: guessing long
costs a little time, guessing short breaks nothing at all."""

AXE, PICKAXE, SHOVEL, HOE, SWORD, SHEARS = (
    "axe", "pickaxe", "shovel", "hoe", "sword", "shears")

# Which tool each block wants.
_TOOL_BY_SUFFIX = (
    ("_log", AXE), ("_planks", AXE), ("_wood", AXE),
    ("_ore", PICKAXE), ("_leaves", HOE),
)
_TOOL_BY_NAME = {
    "crafting_table": AXE, "chest": AXE, "barrel": AXE,
    "stone": PICKAXE, "cobblestone": PICKAXE, "andesite": PICKAXE,
    "diorite": PICKAXE, "granite": PICKAXE, "deepslate": PICKAXE,
    "cobbled_deepslate": PICKAXE, "tuff": PICKAXE, "calcite": PICKAXE,
    "sandstone": PICKAXE, "netherrack": PICKAXE, "obsidian": PICKAXE,
    "dirt": SHOVEL, "grass_block": SHOVEL, "coarse_dirt": SHOVEL,
    "podzol": SHOVEL, "rooted_dirt": SHOVEL, "mud": SHOVEL, "sand": SHOVEL,
    "red_sand": SHOVEL, "gravel": SHOVEL, "clay": SHOVEL, "farmland": SHOVEL,
    "snow_block": SHOVEL, "snow": SHOVEL,
}

# Blocks that DROP nothing unless mined with a pickaxe (of sufficient tier).
_NEEDS_PICKAXE_TO_DROP = frozenset({
    "stone", "cobblestone", "andesite", "diorite", "granite", "deepslate",
    "cobbled_deepslate", "tuff", "calcite", "sandstone", "netherrack",
    "obsidian",
})

MATERIAL_SPEED = {
    "wooden": 2.0, "stone": 4.0, "copper": 5.0, "iron": 6.0,
    "diamond": 8.0, "netherite": 9.0, "golden": 12.0,
}
MATERIAL_TIER = {
    "wooden": 0, "golden": 0, "stone": 1, "copper": 1, "iron": 2,
    "diamond": 3, "netherite": 4,
}
_TIER_NEEDED = {
    "iron_ore": 1, "deepslate_iron_ore": 1, "copper_ore": 1,
    "deepslate_copper_ore": 1, "lapis_ore": 1, "deepslate_lapis_ore": 1,
    "gold_ore": 2, "deepslate_gold_ore": 2, "redstone_ore": 2,
    "deepslate_redstone_ore": 2, "diamond_ore": 2, "deepslate_diamond_ore": 2,
    "emerald_ore": 2, "deepslate_emerald_ore": 2, "obsidian": 3,
}

SAFETY_FACTOR = 1.3
SAFETY_MARGIN_S = 0.35
"""Added to every estimate. Latency between the key going down and the game
counting the first tick, plus the ticks lost to a slightly stale reading, are
real and not modelled. Long is safe — the hold stops when the block goes."""


def _short(name) -> str:
    text = str(name or "").strip().lower()
    return text.split(":", 1)[1] if ":" in text else text


@dataclass(frozen=True)
class Tool:
    kind: str | None          # axe, pickaxe, ... or None for a hand
    material: str | None      # wooden, stone, ... or None
    efficiency: int = 0

    @property
    def speed(self) -> float:
        return MATERIAL_SPEED.get(self.material or "", 1.0)

    @property
    def tier(self) -> int:
        return MATERIAL_TIER.get(self.material or "", -1)

    def describe(self) -> str:
        if self.kind is None:
            return "bare hands"
        return f"{self.material or 'unknown'} {self.kind}".strip()


def tool_from_item(item) -> Tool:
    """What the player is holding, as a Tool. Anything else is a hand."""
    name = _short(getattr(item, "name", item) if item is not None else "")
    for kind in (PICKAXE, SHOVEL, AXE, HOE, SWORD):
        suffix = "_" + kind
        if name.endswith(suffix):
            material = name[: -len(suffix)] or None
            return Tool(kind=kind, material=material)
    if name == SHEARS:
        return Tool(kind=SHEARS, material=None)
    return Tool(kind=None, material=None)


def preferred_tool(block) -> str | None:
    name = _short(block)
    if name in _TOOL_BY_NAME:
        return _TOOL_BY_NAME[name]
    for suffix, tool in _TOOL_BY_SUFFIX:
        if name.endswith(suffix):
            return tool
    return None


def can_harvest(block, tool: Tool) -> bool:
    """Will breaking it with this tool actually DROP the block?"""
    name = _short(block)
    needs_pick = name in _NEEDS_PICKAXE_TO_DROP or name.endswith("_ore")
    if not needs_pick:
        return True
    if tool.kind != PICKAXE:
        return False
    return tool.tier >= _TIER_NEEDED.get(name, 0)


@dataclass(frozen=True)
class BreakEstimate:
    block: str
    seconds: float | None      # None: cannot be broken at all
    tool: Tool
    right_tool: bool
    drops: bool
    known_block: bool
    notes: tuple = ()

    @property
    def breakable(self) -> bool:
        return self.seconds is not None

    def hold_seconds(self, ceiling: float) -> float:
        """How long to hold attack: the estimate, made generous, bounded."""
        if self.seconds is None:
            return 0.0
        return min(ceiling, self.seconds * SAFETY_FACTOR + SAFETY_MARGIN_S)

    def describe(self) -> str:
        if self.seconds is None:
            return f"{self.block} cannot be broken"
        parts = [f"{self.block} with {self.tool.describe()}: about "
                 f"{self.seconds:.1f}s"]
        if not self.known_block:
            parts.append("(a guess — this block is not in my table)")
        wanted = preferred_tool(self.block)
        if not self.right_tool and wanted:
            article = "an" if wanted[:1] in "aeiou" else "a"
            parts.append(f"(faster with {article} {wanted})")
        if not self.drops:
            parts.append("— and it will DROP NOTHING with this tool")
        parts.extend(self.notes)
        return " ".join(parts)


def estimate_break_duration(block, held_item=None, state=None,
                            on_ground: bool | None = None,
                            in_water: bool = False) -> BreakEstimate:
    """Seconds to break `block` with what is in hand.

    `state`, when given, supplies the held item and whether the player is on
    the ground. Missing information is assumed to be the FAVOURABLE case and
    the estimate is padded instead — assuming the unfavourable one would
    turn a three-second log into a fifteen-second one on a guess."""
    name = _short(getattr(block, "name", block))
    if held_item is None and state is not None:
        held_item = getattr(state, "held_item", None)
    if on_ground is None and state is not None:
        on_ground = getattr(state, "on_ground", None)
    tool = tool_from_item(held_item)
    notes = []

    known = name in HARDNESS
    hardness = HARDNESS.get(name, UNKNOWN_HARDNESS)
    if hardness < 0:
        return BreakEstimate(block=name, seconds=None, tool=tool,
                             right_tool=False, drops=False, known_block=True)

    wanted = preferred_tool(name)
    right_tool = wanted is not None and tool.kind == wanted
    drops = can_harvest(name, tool)

    if hardness == 0:
        return BreakEstimate(block=name, seconds=0.05, tool=tool,
                             right_tool=right_tool, drops=drops,
                             known_block=known)

    speed = tool.speed if right_tool else 1.0
    if right_tool and tool.efficiency > 0:
        speed += tool.efficiency ** 2 + 1
    per_tick = speed / hardness / (30.0 if drops else 100.0)
    if on_ground is False:
        per_tick /= 5.0
        notes.append("(slower: I am not on the ground)")
    if in_water:
        per_tick /= 5.0
        notes.append("(slower: underwater)")

    ticks = math.ceil(1.0 / per_tick)
    return BreakEstimate(block=name, seconds=ticks / TICKS_PER_SECOND,
                         tool=tool, right_tool=right_tool, drops=drops,
                         known_block=known, notes=tuple(notes))


# ── Which tool to hold ───────────────────────────────────────────────────────

HOTBAR_SLOTS = range(9)

SWITCH_WORTH_S = 0.5
"""Seconds a tool must save before it is worth a hotbar step of its own.
Leaves break in a third of a second by hand; switching to a hoe for each
one and back to the axe for the log would cost more than it saves."""

_TIER_WORDS = {0: "wooden", 1: "stone", 2: "iron", 3: "diamond", 4: "netherite"}


@dataclass(frozen=True)
class ToolChoice:
    """What to hold for one block, chosen from the hotbar.

    `slot` is the hotbar slot (0-8) to select, or None to keep what is held.
    `better_in_inventory` names an item in the MAIN inventory that would do
    better than anything in the hotbar -- it cannot be reached without the
    inventory screen."""
    block: str
    slot: int | None
    item: str | None
    harvests: bool
    estimate: "BreakEstimate | None" = None
    better_in_inventory: str | None = None
    inventory_known: bool = True

    @property
    def refusal(self) -> str | None:
        """Why not to swing at all, or None. Only for a block that drops
        nothing with anything in the hotbar -- the swing would break it for
        nothing, or not at all in the time allowed."""
        if self.harvests or not self.inventory_known:
            return None
        held = " ".join((self.item or "bare hands").split("_"))
        text = (f"Nothing in the hotbar can harvest {self.block}: it needs "
                f"{tool_needed(self.block)}, and breaking it with {held} "
                f"drops nothing. I did not swing.")
        if self.better_in_inventory:
            text += (f" There is a {' '.join(self.better_in_inventory.split('_'))}"
                     f" in your inventory, but not in the hotbar -- move it "
                     f"there and ask again.")
        return text


def tool_needed(block) -> str:
    """In words: what harvests `block`."""
    name = _short(block)
    tier = _TIER_NEEDED.get(name, 0)
    if tier <= 0:
        return "a pickaxe"
    return f"a {_TIER_WORDS.get(tier, 'better')} pickaxe or better"


def best_hotbar_tool(state, block) -> ToolChoice:
    """The hotbar item that harvests `block` (it drops) and breaks it
    fastest, preferring what is already held on a tie or a trivial saving.

    From the hotbar only: anything else is behind the inventory screen. An
    unreadable inventory changes nothing -- no switch, and no refusal on a
    guess."""
    name = _short(getattr(block, "name", block))
    inventory = getattr(state, "inventory", None)
    current = getattr(state, "selected_slot", None)
    held = getattr(state, "held_item", None)
    if inventory is None:
        held_name = _short(getattr(held, "name", "")) or None
        return ToolChoice(block=name, slot=None, item=held_name,
                          harvests=can_harvest(name, tool_from_item(held_name)),
                          inventory_known=False)

    by_slot = {}
    for stack in inventory:
        slot = getattr(stack, "slot", None)
        if slot is not None and (getattr(stack, "count", 0) or 0) > 0:
            by_slot[slot] = _short(stack.name)

    def judge(item):
        estimate = estimate_break_duration(name, held_item=item or "",
                                           state=state)
        seconds = estimate.seconds if estimate.seconds is not None \
            else float("inf")
        return estimate.drops, seconds, estimate

    hotbar = [(slot, by_slot.get(slot)) for slot in HOTBAR_SLOTS]
    if current is None or current not in HOTBAR_SLOTS:
        current = None
    now = judge(by_slot.get(current) if current is not None else None)

    best_slot, best = current, now
    for slot, item in hotbar:
        verdict = judge(item)
        better = (verdict[0] and not best[0]) or (
            verdict[0] == best[0]
            and verdict[1] < best[1] - SWITCH_WORTH_S)
        if better:
            best_slot, best = slot, verdict

    # The main inventory: worth naming when it beats the hotbar's best.
    stored = None
    for slot, item in sorted(by_slot.items()):
        if slot in HOTBAR_SLOTS:
            continue
        verdict = judge(item)
        if (verdict[0] and not best[0]) or (
                verdict[0] == best[0]
                and verdict[1] < best[1] - SWITCH_WORTH_S):
            if stored is None or verdict[1] < stored[1][1]:
                stored = (item, verdict)

    switch = best_slot is not None and best_slot != current
    chosen = by_slot.get(best_slot) if best_slot is not None else None
    return ToolChoice(block=name, slot=best_slot if switch else None,
                      item=chosen, harvests=bool(best[0]),
                      estimate=best[2],
                      better_in_inventory=stored[0] if stored else None)


# ── Is the crosshair on the RIGHT block? ─────────────────────────────────────

def crosshair_on(state, position, name: str | None = None) -> bool:
    """Is the block under the crosshair exactly this one?

    Coordinates, not angles. "The aim is within six degrees" is a statement
    about the camera; "the crosshair is on the oak log at (5, 71, -232)" is a
    statement about the block, and it is the one that decides whether
    holding attack breaks the log or the leaf next to it. The bridge can say
    which, so this asks it."""
    target = getattr(state, "target_block", None)
    if target is None:
        return False
    try:
        here = (int(target.x), int(target.y), int(target.z))
        wanted = (int(position[0]), int(position[1]), int(position[2]))
    except (TypeError, ValueError, IndexError, AttributeError):
        return False
    if here != wanted:
        return False
    return name is None or _short(target.name) == _short(name)


__all__ = [
    "estimate_break_duration", "BreakEstimate", "Tool", "tool_from_item",
    "preferred_tool", "can_harvest", "crosshair_on", "HARDNESS",
    "AXE", "PICKAXE", "SHOVEL", "HOE", "SWORD", "SHEARS",
    "SAFETY_FACTOR", "SAFETY_MARGIN_S",
]
