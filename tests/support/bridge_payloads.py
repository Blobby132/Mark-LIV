"""
payload() and source(): a mod-bridge payload and a reader of it.

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/test_minecraft_mod_bridge.py.
"""

from __future__ import annotations

import json

from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA


NOW_MS = 1_700_000_000_000
NOW_S = NOW_MS / 1000.0


def payload(**overrides) -> dict:
    base = {
        "schema": SCHEMA,
        "written_at_ms": NOW_MS,
        "in_game": True,
        "position": [-142.5, 71.0, 305.2],
        "rotation": [12.7, -3.4],
        "health": 18.0,
        "max_health": 20.0,
        "hunger": 17,
        "on_ground": True,
        "selected_slot": 2,
        "held_item": {"slot": 2, "name": "minecraft:iron_axe", "count": 1},
        "inventory": [{"slot": 0, "name": "minecraft:oak_log", "count": 12}],
        "dimension": "minecraft:overworld",
        "time_of_day": 1200,
        "weather": "clear",
        "biome": "minecraft:forest",
        "light_level": 11,
        "target_block": {"name": "minecraft:oak_log", "x": -143, "y": 70,
                         "z": 306, "face": "north"},
        "target_entity": None,
        "nearby_entities": [{"name": "minecraft:cow", "distance": 7.4}],
    }
    base.update(overrides)
    return base


def source(data=None, raw=None, clock_s=NOW_S) -> ModBridgeStateSource:
    text = raw if raw is not None else json.dumps(
        data if data is not None else payload())
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: clock_s)
