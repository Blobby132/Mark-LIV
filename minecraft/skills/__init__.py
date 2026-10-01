"""
minecraft/skills/ — the interface for "a thing JARVIS knows how to do", and
a small number of real ones.

WHERE THINGS ARE
    This package was minecraft/skills.py, split by what the skills do, with
    nothing changed but where the code lives. This module keeps the registry
    (BUILTIN_SKILLS, create, available), NOT_YET_POSSIBLE, and every name
    the old module had, so `skills.X` still means what it did.

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

from minecraft.skills.base import (  # noqa: F401
    CANNOT_SEE_TARGET, MIN_USEFUL_MINE_S, MAX_SKIPPED_TARGETS,
    MAX_LEAVES_PER_LOG, MAX_TREE_LOGS, MAX_PICKUP_WALKS, DROP_RADIUS, _ARRIVED,
    SWEEP_DEGREES, _sweep_pixels, _mine_params, _crosshair_at, _centre,
    _crosshair_text, LOG_BLOCKS, Skill, _tree_label, FOODS, UNSAFE_FOODS,
    INTERACTIVE_SUFFIXES, _interactive, _slot_of, _words, _learn_pointer,
)
from minecraft.skills.hotbar import (  # noqa: F401
    FETCH_WORTH_S, _KEEP_AT_HAND, _worth_keeping, hotbar_slot_to_fill,
    HotbarFetch,
)
from minecraft.skills.navigate import (  # noqa: F401
    WalkForward, Survey, FindBlock, CANNOT_SEE_WORLD, WALK_BLOCKS_PER_S,
    SPRINT_BLOCKS_PER_S, SPRINT_FROM_BLOCKS, YAW_TOLERANCE_DEG,
    REVALIDATE_EVERY, STALLS_BEFORE_OBSTACLE_CHECK, MAX_REROUTES,
    REROUTE_REFILL_BLOCKS, AVOID_FOR_REPLANS, CAUTIOUS_STEP_S,
    INPUT_STUCK_AFTER, MAX_STALLS, MAX_HOPS_ONTO, NavigateTo, nav_target,
    _last_move_distance, block_label,
)
from minecraft.skills.collect import (  # noqa: F401
    _HoldsTheRightTool, BreakBlock, _AIR, _Gatherer, CollectLogs, GATHERABLE,
    GATHER_ALIASES, exposed, open_face, liquid_beside, underfoot,
    CollectBlocks, _pickup_column, _under_the_trunk, FETCHED_DROP_DRIFT,
    _still_there, _drop_text, _item_total, _log_total, AimAtBlock, _mine_block,
)
from minecraft.skills.eat import (  # noqa: F401
    EAT_TICKS, EAT_SLOW_SERVER, EAT_LATENCY_S, eat_ticks, eat_seconds,
    MAX_HUNGER, EatFood,
)
from minecraft.skills.place_build import (  # noqa: F401
    PlaceBlock, MAX_PLACE_AIMS, MAX_PLACE_WALKS, PLACE_REACH_MARGIN,
    _place_from, _in_view, _can_place_here, _stand_for, _face_words,
    PlaceBlockAt, MAX_LINE_BLOCKS, MAX_FAILURES_IN_A_ROW, _LINE_DIRECTIONS,
    _cells_text, _Builder, BuildLine, _BLUEPRINTS, _LAST_BLUEPRINT,
    forget_blueprints, _cardinal, BuildBlueprint,
)
from minecraft.skills.craft import (  # noqa: F401
    _CLICK_ACTIONS, _carried_of, _held_name, CraftItem,
)
from minecraft.skills.combat import (  # noqa: F401
    FLEE_SAFE_DISTANCE, FLEE_KEEP_CLEAR, FLEE_REPLAN_EVERY, MAX_FLEE_SECONDS,
    _hostiles, _mob_words, Flee, FIGHT_MAX_SECONDS, FIGHT_RETREAT_HEALTH,
    FIGHT_START_HEALTH, FIGHT_WEAPON_SELECTS, FIGHT_RANGE, ENTITY_REACH,
    ATTACK_TAP_S, AIM_ON_BODY_DEG, MOB_HEIGHTS, NEVER_MELEE,
    best_hotbar_weapon, Fight, _mob_key,
)


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
                           "(design: docs/minecraft-jump-place.md).",
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
    "MIN_USEFUL_MINE_S",
    "Skill", "WalkForward", "Survey", "FindBlock", "BreakBlock",
    "PlaceBlock", "CollectLogs", "AimAtBlock",
    "BUILTIN_SKILLS", "create", "available", "LOG_BLOCKS", "NOT_YET_POSSIBLE",
]
