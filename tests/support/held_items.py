"""
DENIED: the held items place (and interact) refuse.

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/test_minecraft_place_held_item.py.
"""

from __future__ import annotations


DENIED = (
    "lava_bucket", "water_bucket", "powder_snow_bucket", "bucket",
    "milk_bucket", "axolotl_bucket", "flint_and_steel", "fire_charge", "tnt",
    "zombie_spawn_egg", "creeper_spawn_egg", "ender_pearl", "ender_eye",
    "splash_potion", "lingering_potion", "potion", "bow", "crossbow",
    "trident", "snowball", "egg", "brown_egg", "firework_rocket",
    "end_crystal", "experience_bottle", "wind_charge", "tnt_minecart",
    "fishing_rod", "iron_axe", "diamond_shovel", "wooden_hoe", "shears",
    "brush", "bone_meal",
)
