"""
minecraft/skills/ — the interface for "a thing JARVIS knows how to do", and
a small number of real ones.

WHERE THINGS ARE
    This package was minecraft/skills.py, split by what the skills do, with
    nothing changed but where the code lives. This module keeps the registry
    (BUILTIN_SKILLS, create, available), NOT_YET_POSSIBLE, and the public
    names listed in __all__: the skill classes and the few constants used
    outside the package. Anything else is imported from the submodule that
    defines it -- `from minecraft.skills import combat as skills_combat`.

        base         the Skill protocol and what several modules share
        hotbar       HotbarFetch: an item from the main inventory to the hotbar
        navigate     walk_forward, survey, find_block, navigate_to
        collect      break_block, collect_logs, fell_tree, collect_blocks,
                     aim_at_block, mine_block
        eat          eat_food
        craft        craft_item
        place_build  place_block, place_block_at, build_line, build_blueprint
        combat       flee, fight

    Each imports only from the ones above it in that list (craft also from
    place_build), so there is no import cycle. docs/ARCHITECTURE.md maps
    them and the safety layers around them.

SCOPE, ON PURPOSE
    This is the architecture plus four working skills, not a library. The
    eventual list — collect_wood, craft_item, find_iron, build_structure,
    return_to_base — needs state this version cannot read (inventory, health,
    entity positions) and planning this version deliberately does not do.
    Writing them now would mean writing them against a state source that
    cannot feed them, and rewriting every one when the mod bridge lands.

    What is here is the seam they will plug into, exercised by four skills
    that work end to end today.

WHAT A SKILL IS
    An object with `name`, `goal`, and:

        plan(state, step_index, history) -> Step | None

    It is called once per iteration with the state just observed and every
    record so far. It returns the next `Step`, or None to say it is finished.
    It never touches the controller, the ledger or the backend — it describes
    an action and the runner decides whether that action is allowed.

WHY SKILLS ARE NOT LLM PROMPTS
    A skill is ordinary Python with a bounded decision. That is what makes the
    task loop testable without a model, and what stops a prompt injection in a
    Minecraft sign from becoming a plan. When a model does get to drive this
    (a later phase), it will choose WHICH skill to run and with what
    parameters — not what the skill does step by step.

EVERY SKILL DECLARES WHAT IT CANNOT VERIFY
    `verifiable_with` names the state fields a skill needs for its own success
    check to mean anything. A skill whose fields are unreadable still runs —
    the actions are real — but every step comes back UNVERIFIABLE and the task
    summary says so rather than claiming an achievement.
"""

from __future__ import annotations

from minecraft.skills.base import FOODS, LOG_BLOCKS, MIN_USEFUL_MINE_S, Skill
from minecraft.skills.navigate import FindBlock, NavigateTo, Survey, WalkForward
from minecraft.skills.collect import (
    AimAtBlock, BreakBlock, CollectBlocks, CollectLogs, _mine_block,
)
from minecraft.skills.eat import EatFood
from minecraft.skills.place_build import (
    BuildBlueprint, BuildLine, PlaceBlock, PlaceBlockAt,
)
from minecraft.skills.craft import CraftItem
from minecraft.skills.combat import Fight, Flee


# ── Registry ─────────────────────────────────────────────────────────────────

BUILTIN_SKILLS = {
    "walk_forward": WalkForward,
    "survey": Survey,
    "find_block": FindBlock,
    "break_block": BreakBlock,
    "place_block": PlaceBlock,
    "place_block_at": PlaceBlockAt,
    "build_line": BuildLine,
    "flee": Flee,
    "fight": Fight,
    "build_blueprint": lambda plan="", **kwargs: BuildBlueprint(
        design=plan, **kwargs),
    "collect_logs": CollectLogs,
    "fell_tree": lambda **kwargs: CollectLogs(whole_tree=True, **kwargs),
    "collect_blocks": CollectBlocks,
    "eat_food": EatFood,
    "craft_item": CraftItem,
    "navigate_to": NavigateTo,
    "aim_at_block": AimAtBlock,
    "mine_block": _mine_block,
}


def create(name: str, **kwargs):
    """Build a skill by name.

    A fixed table, not a lookup by import path or class name: the point of the
    planner boundary is that the set of runnable things is decided in source,
    and a registry that could be extended at runtime would give that back."""
    factory = BUILTIN_SKILLS.get(str(name or "").strip().lower())
    if factory is None:
        raise KeyError(
            f"'{name}' is not a skill I have. Mine are: "
            f"{', '.join(sorted(BUILTIN_SKILLS))}."
        )
    return factory(**kwargs)


def available() -> tuple:
    return tuple(sorted(BUILTIN_SKILLS))


# Named here rather than in prose so the tool description, the status report
# and the manual check all quote the same list.
NOT_YET_POSSIBLE = {
    "find_iron": "the scan now reports ores it can see, so iron already "
                 "exposed in a cave wall within the scan radius is findable. "
                 "What is missing is getting to iron that is NOT exposed, "
                 "which means digging a shaft, lighting it, and not falling "
                 "into lava — none of which is built.",
    "attack_players_or_animals": "attack only ever hits a mob the game "
                                 "calls hostile: never a player, a pet, a "
                                 "villager or an animal. That is deliberate, "
                                 "and hunting animals for food is not built.",
    "use_dangerous_items_unasked": "a lava, water or powder-snow bucket, "
                                   "flint and steel and a fire charge are "
                                   "used only when the user names the item; "
                                   "interact never right-clicks with them.",
    "pillar_up_or_bridge": "building upwards past two blocks, or out over "
                           "a gap, needs a jump and a place timed inside "
                           "one hold, about 0.3s apart; the controller "
                           "does not schedule taps within a hold yet "
                           "(design: docs/minecraft/jump-place.md).",
    "return_to_base": "navigation exists now, but only within the scan "
                      "radius. Walking back to a base 300 blocks away needs "
                      "stored waypoints and route-finding across terrain not "
                      "currently visible, which is a different problem from "
                      "crossing a clearing.",
    "long_distance_travel": "the planner refuses any destination outside the "
                            "scan, on purpose. Travelling further means "
                            "planning to the edge of what is visible, "
                            "re-scanning, and planning again — that loop is "
                            "not built.",
    "dig_or_bridge_a_route": "the pathfinder only walks. It will not mine "
                             "through an obstacle or place blocks over a "
                             "gap, so a destination that needs either comes "
                             "back as unreachable rather than as a plan.",
}

NOW_POSSIBLE_WITH_THE_BRIDGE = (
    "inventory contents", "health", "hunger", "held item", "nearby entities",
    "exact position", "world time", "weather",
    "the terrain around the player", "where nearby blocks worth reaching are",
    "whether there is a walkable route to one",
)
"""What stopped being impossible when the mod arrived.

Kept as a list rather than folded into prose because these were each cited, in
this file and in the tool description, as the reason something could not be
done. A claim that stops being true should be retracted in the same place it
was made."""


__all__ = [
    # The registry.
    "BUILTIN_SKILLS", "create", "available",
    # The protocol and every registered skill class.
    "Skill", "WalkForward", "Survey", "FindBlock", "BreakBlock",
    "PlaceBlock", "PlaceBlockAt", "BuildLine", "BuildBlueprint",
    "CollectLogs", "CollectBlocks", "AimAtBlock", "NavigateTo", "EatFood",
    "CraftItem", "Flee", "Fight",
    # Constants used outside the package.
    "MIN_USEFUL_MINE_S", "LOG_BLOCKS", "FOODS", "NOT_YET_POSSIBLE",
    "NOW_POSSIBLE_WITH_THE_BRIDGE",
]
