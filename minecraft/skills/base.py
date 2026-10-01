"""
minecraft/skills/base.py -- what more than one skill module uses.

The Skill protocol; the shared constants and crosshair helpers that used to
head skills.py; the food tables (eating chooses from them, the hotbar fetch
keeps food at hand by them); the check for blocks a right-click would use
instead of placing or eating; and the small inventory-screen helpers the
hotbar fetch and craft_item both use. Moved here unchanged from skills.py.
"""

from __future__ import annotations

from typing import Protocol

from minecraft import navigation as nav
from minecraft import gui as gui_mod
from minecraft.state import UNKNOWN


CANNOT_SEE_TARGET = (
    "I cannot read what is under the crosshair, so I have no way to find a "
    "log or to tell whether one broke. That needs the F3 overlay open and "
    "OCR installed — see core/ocr.py. I stopped rather than swing at "
    "nothing."
)
"""Why a target-dependent skill gives up immediately.

Sweeping the view twenty times looking for something you cannot see is not
perseverance, it is a loop with a step limit for a brake. The skills that
depend on reading the target check for it once and say what is missing."""

# Every mining step must ask for at least this long. Breaking an oak log by
# hand takes about three seconds of CONTINUOUS holding, and Minecraft discards
# progress the instant the button comes up -- so a skill asking for one second
# mines forever and breaks nothing.
#
# Named here, and asserted in the tests, because that is exactly what happened:
# the action limit was raised to allow a real mining hold and the skills were
# left asking for the old one second, which looks identical to the input not
# working at all.
MIN_USEFUL_MINE_S = 3.0

MAX_SKIPPED_TARGETS = 4

MAX_LEAVES_PER_LOG = 3
"""Leaf blocks collect_logs will break to get at one log, before trying a
different log instead.

From a real run: the aim settled on four logs in turn, 0 degrees off each
time, and never once landed -- the canopy was between the eye and the trunk.
Leaves are what is usually in front of a log, and they break in a fraction
of a second by hand. Only a leaf NEARER than the log is broken, only at the
exact coordinate the crosshair reports, and never counted as a log."""

MAX_TREE_LOGS = 24
"""The most logs fell_tree will break. A tree has four to a dozen that can
be reached from the ground; this only stops a grove of touching trunks
turning one request into an afternoon."""

MAX_PICKUP_WALKS = 4
"""How many times collect_logs walks over to a dropped log before saying it
could not get it. A broken log drops an item where it fell, often a couple of
blocks from where the player stood to mine it -- out of pickup range."""

DROP_RADIUS = 4.0
"""Item entities this close (horizontally) to a block this task broke are
treated as its drops and fetched. Further than that, an item on the ground is
somebody else's business."""

_ARRIVED = object()
"""What _walk_towards returns when the walk is over and nothing is left to
step: the caller decides what arriving means, instead of a filler step."""
"""Logs to give up on (occluded, unreachable by aim) before the whole task
does. Enough to work round a tree's leaves; not so many that a task with a
real aiming problem burns its budget trying every log in the forest."""

SWEEP_DEGREES = 18.0
"""How far a blind sweep turns between looks.

IN DEGREES, NOT PIXELS, AND THAT IS THE POINT
    These skills used to turn a fixed number of PIXELS, which is not a
    quantity anyone can reason about: 100 pixels is 12 degrees on one
    machine and 50 degrees on another, and on the second one a sweep jumps
    straight past the tree it is looking for and reports there isn't one.

    Eighteen degrees is a little under the crosshair's useful width, so a
    full turn takes twenty looks and nothing gets skipped over."""


def _sweep_pixels() -> int:
    """SWEEP_DEGREES in pixels, using whatever the mouse has measured."""
    return max(1, int(round(SWEEP_DEGREES * nav.pixels_per_degree())))


def _mine_params(seconds, block) -> dict:
    """Parameters for a mining step, pinned to the block that was seen.

    When the crosshair reading carried a coordinate, it goes along as
    `expect_at`: the controller reads the crosshair again at the instant
    before the button goes down and presses nothing unless it is still on
    that block. Without a coordinate there is nothing to pin, and the step is
    what it always was."""
    params = {"duration": seconds}
    try:
        where = [int(block.x), int(block.y), int(block.z)]
    except (AttributeError, TypeError, ValueError):
        return params
    params["expect_at"] = where
    return params


def _crosshair_at(state):
    """((x, y, z), name) of what the crosshair is on, or None when it cannot
    say where -- unreadable, or on nothing."""
    block = getattr(state, "target_block", None)
    if block is None or state.confidence_of("target_block") == UNKNOWN:
        return None
    try:
        return (int(block.x), int(block.y), int(block.z)), block.name
    except (TypeError, ValueError):
        return None


def _centre(position) -> tuple:
    return (position[0] + 0.5, position[1] + 0.5, position[2] + 0.5)


def _crosshair_text(state) -> str:
    """What the crosshair is on, for a person reading the step trail.

    An aim that reaches 0 degrees off and still does not land is only
    explicable by what it landed on INSTEAD -- a leaf, the next log, the
    ground -- and the trail used not to say."""
    seen = _crosshair_at(state)
    if seen is None:
        name = getattr(getattr(state, "target_block", None), "name", None)
        if name == "air" and state.confidence_of("target_block") != UNKNOWN:
            return "crosshair on nothing within reach"
        return "crosshair unreadable"
    position, name = seen
    return f"crosshair on {name} {position}"


# Blocks that count as "a tree" for FindBlock's default search. One
# definition, in navigation.py: there used to be a copy here too.
LOG_BLOCKS = nav.LOG_BLOCKS


class Skill(Protocol):
    """What the task runner needs. Three members and one method."""

    name: str
    goal: str
    verifiable_with: tuple

    def plan(self, state, step_index: int, history: tuple): ...


def _tree_label(label) -> str:
    """("birch_log", (-39, -201)) as "the birch tree at (-39, -201)"."""
    name, (x, z) = label
    kind = " ".join(str(name).split("_"))
    if kind.endswith(" log"):
        kind = kind[:-4]
    return f"the {kind} tree at ({x}, {z})"


FOODS = {
    "apple": 4, "baked_potato": 5, "beetroot": 1, "beetroot_soup": 6,
    "bread": 5, "carrot": 3, "cooked_beef": 8, "cooked_chicken": 6,
    "cooked_cod": 5, "cooked_mutton": 6, "cooked_porkchop": 8,
    "cooked_rabbit": 5, "cooked_salmon": 6, "cookie": 2, "dried_kelp": 1,
    "glow_berries": 2, "golden_carrot": 6, "honey_bottle": 6,
    "melon_slice": 2, "mushroom_stew": 6, "potato": 1, "pumpkin_pie": 8,
    "rabbit_stew": 10, "sweet_berries": 2, "beef": 3, "porkchop": 3,
    "mutton": 2, "cod": 2, "salmon": 2, "rabbit": 3, "tropical_fish": 1,
}
"""What eat_food will choose, with the hunger each restores.

Left out on purpose: anything that harms or surprises -- rotten flesh,
spider eyes, poisonous potatoes, pufferfish and raw chicken make you ill,
chorus fruit teleports you, suspicious stew is a lottery -- and golden
apples, which are worth too much to eat just because you were peckish."""

UNSAFE_FOODS = {
    "rotten_flesh": "it gives Hunger", "spider_eye": "it poisons you",
    "poisonous_potato": "it can poison you", "pufferfish": "it poisons you",
    "chicken": "raw chicken can give Hunger",
    "chorus_fruit": "it teleports you", "suspicious_stew": "its effect is "
    "random", "golden_apple": "golden apples are too valuable to eat just "
    "for hunger", "enchanted_golden_apple": "it is far too valuable to eat "
    "just for hunger",
}
"""Edible, and never chosen: the reason is what the user is told."""

INTERACTIVE_SUFFIXES = (
    "chest", "barrel", "crafting_table", "furnace", "smoker", "_door",
    "_trapdoor", "_fence_gate", "_bed", "lever", "_button", "anvil",
    "enchanting_table", "brewing_stand", "loom", "stonecutter", "grindstone",
    "cartography_table", "smithing_table", "fletching_table", "lectern",
    "note_block", "jukebox", "repeater", "comparator", "bell",
    "shulker_box", "hopper", "dispenser", "dropper", "beacon",
    "respawn_anchor", "composter", "cauldron", "flower_pot", "_sign",
    "crafter", "decorated_pot", "chiseled_bookshelf", "campfire", "cake",
    "daylight_detector", "trial_spawner", "vault",
)
"""Blocks that do something when right-clicked. Eating is holding right
click, and with one of these under the crosshair the click opens, toggles
or uses IT instead -- a chest opened, a door swung, food put on a campfire."""


def _interactive(name) -> bool:
    name = str(name or "")
    return any(name.endswith(suffix) for suffix in INTERACTIVE_SUFFIXES)


def _slot_of(state, index):
    return gui_mod.slot_by_index(state, index)


def _words(name) -> str:
    return " ".join(str(name).split("_"))


def _learn_pointer(pointer, last) -> None:
    """Measure the pointer's gain from the move just made, if it was one."""
    if last is None or last.step.get("action") != "gui_point":
        return
    try:
        before = last.state_before["gui"]["cursor_px"]
        after = last.state_after["gui"]["cursor_px"]
        params = last.step.get("params") or {}
        pointer.observe((params.get("dx", 0), params.get("dy", 0)),
                        tuple(before), tuple(after))
    except (KeyError, TypeError):
        pass
