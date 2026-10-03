"""
minecraft/blocks.py -- block names more than one module judges by.

Names only: no imports, no functions, nothing that can act. The pathfinder
(navigation.py) and the dig planner (digging.py) both read HAZARDS from
here, so a block added once is avoided by both.
"""

from __future__ import annotations

HAZARDS = frozenset({
    "lava", "flowing_lava", "fire", "soul_fire", "magma_block", "cactus",
    "sweet_berry_bush", "wither_rose", "powder_snow", "campfire",
    "soul_campfire", "cobweb",
    # Hurts to land on; a floor of them is no floor.
    "pointed_dripstone", "lava_cauldron",
    # Somewhere else entirely, one step in.
    "nether_portal", "end_portal", "end_gateway",
    # Wakes the warden, or springs a temple's trap.
    "sculk_shrieker", "sculk_sensor", "calibrated_sculk_sensor", "tripwire",
})
"""Blocks not to stand on or walk into, even when solid enough to walk
over: they burn, prick, trap, teleport or wake something."""

CONTACT_HAZARDS = frozenset({"cactus"})
"""Blocks that hurt to brush against: none may be beside a cell the body
goes through."""
