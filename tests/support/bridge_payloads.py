"""
payload() and source(): a mod-bridge payload and a reader of it.

Shared by several test modules, so they import it from here instead of
from each other. payload() and source() moved here unchanged from
tests/bridge/test_minecraft_mod_bridge.py; near_grid() builds the
near_blocks grid the current jar sends.
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


def near_grid(cells=None, origin=(0, 64, 0), below=5, above=3, radius=4):
    """A near_blocks field with a grid, as the current jar sends it:
    {(x, y, z): (name, solid, fluid)} for the cells that matter, None for
    one not known; every other cell stone below the feet and air above."""
    cells = cells or {}
    palette, runs, index = [], [], {}
    for y in range(origin[1] - below, origin[1] + above + 1):
        for z in range(origin[2] - radius, origin[2] + radius + 1):
            for x in range(origin[0] - radius, origin[0] + radius + 1):
                entry = cells.get((x, y, z),
                                  ("minecraft:stone", True, None)
                                  if y < origin[1]
                                  else ("minecraft:air", False, None))
                if entry is None:
                    i = -1
                else:
                    key = json.dumps(list(entry))
                    if key not in index:
                        index[key] = len(palette)
                        palette.append(list(entry))
                    i = index[key]
                if runs and runs[-2] == i:
                    runs[-1] += 1
                else:
                    runs += [i, 1]
    return {"origin": list(origin), "radius": radius, "below": below,
            "above": above,
            "complete": all(v is not None for v in cells.values()),
            "complete_within": None, "blocks": [],
            "grid": {"order": "yzx", "palette": palette, "runs": runs}}


def source(data=None, raw=None, clock_s=NOW_S) -> ModBridgeStateSource:
    text = raw if raw is not None else json.dumps(
        data if data is not None else payload())
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: clock_s)
