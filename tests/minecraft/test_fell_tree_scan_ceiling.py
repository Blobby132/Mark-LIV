"""
fell_tree must not call a tree gone when the scan cannot see its top.

From a real run with an older jar: fell_tree reported "none of it is left
standing" after one log while four more stood above. That jar reported
logs only up to 4 blocks above the feet (no "tree_up" in its scan), so once
the logs it could see were broken, the rest of the trunk was simply not in
the payload.

The world below is the simulated tree world, read through the real bridge
reader from a payload of the old jar's shape -- or the new jar's, whose
scan follows a trunk up to tree_up (12) above the feet.
"""

from __future__ import annotations

import json
import math
import time
import unittest

from minecraft import aiming as aiming_mod
from minecraft import mod_bridge, skills
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from minecraft.state import NearbyBlock
from tests.support.regression_worlds import DropWorld
from tests.support.sim_world import flat, run

NOW_MS = 1_700_000_000_000
OLD_SCAN_UP = 4


def trunk(x, z, bottom=64, top=67, name="oak_log"):
    return [NearbyBlock(x, y, z, name, True) for y in range(bottom, top + 1)]


def _mc(name):
    return f"minecraft:{name}"


class PayloadTreeWorld(DropWorld):
    """DropWorld whose every reading is a bridge payload, parsed by the
    real reader. `tree_up` None is the old jar: logs only up to 4 above the
    feet, no feature list."""

    def __init__(self, *args, tree_up=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree_up = tree_up

    def payload(self):
        state = DropWorld.read(self)
        feet = math.floor(self.y)
        up = self.tree_up if self.tree_up is not None else OLD_SCAN_UP
        scan = {"radius": 8, "up": OLD_SCAN_UP, "down": 5}
        if self.tree_up is not None:
            scan["tree_up"] = self.tree_up
        target = state.target_block
        data = {
            # Stamped now, as the mod stamps each write: a reading must be
            # newer than the action it is checked against.
            "schema": SCHEMA, "written_at_ms": int(time.time() * 1000),
            "in_game": True,
            "position": [self.x, self.y, self.z],
            "rotation": [self.yaw, self.pitch],
            "health": 20.0, "hunger": 20, "on_ground": True,
            "scan": scan,
            "surface": [[b.x, b.y, b.z, _mc(b.name), True]
                        for b in self.surface],
            "notable_blocks": [[b.x, b.y, b.z, _mc(b.name), True]
                               for b in self.logs if b.y <= feet + up],
            # Nothing under the crosshair is {"name": "minecraft:air"} with
            # null coordinates, as the mod sends it -- a reading, not a gap.
            "target_block": {"name": _mc(target.name), "x": target.x,
                             "y": target.y, "z": target.z,
                             "face": target.face},
            "inventory": [{"slot": s.slot, "name": _mc(s.name),
                           "count": s.count}
                          for s in (state.inventory or ())],
            "nearby_entities": [{"name": "minecraft:item", "category": "item",
                                 "position": list(e.position),
                                 "distance": e.distance}
                                for e in (state.nearby_entities or ())],
        }
        if self.tree_up is not None:
            data["features"] = list(mod_bridge.REQUIRED_FEATURES)
        return data

    def read(self):
        text = json.dumps(self.payload())
        return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                    clock=time.time).read()


class FellTreeCeilingTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def fell(self, logs, tree_up):
        world = PayloadTreeWorld(flat(), logs, inventory={"oak_log": 0},
                                 tree_up=tree_up)
        world.x, world.z = 2.5, 0.5
        skill = skills.create("fell_tree")
        run(world, skill, max_steps=60)
        standing = [b for b in world.logs if (b.x, b.z) == (4, 0)]
        return skill, world, standing

    def test_the_old_jar_does_not_call_a_tall_tree_gone(self):
        """The reproduction: a nine-log trunk, the old jar's scan."""
        skill, world, standing = self.fell(trunk(4, 0, top=72), tree_up=None)
        self.assertTrue(world.broken, skill.done_reason)
        self.assertTrue(standing, "the setup should leave logs standing")
        self.assertNotIn("none of it is left standing", skill.done_reason)
        self.assertIn("y 68", skill.done_reason)       # what it could see

    def test_the_new_jar_sees_what_is_still_standing(self):
        skill, world, standing = self.fell(trunk(4, 0, top=72), tree_up=12)
        self.assertTrue(standing, "the setup should leave logs standing")
        self.assertIn(f"{len(standing)} more log(s) of it are still standing",
                      skill.done_reason)

    def test_a_tree_under_the_ceiling_is_still_called_gone(self):
        """The guard is for a tree that reached the ceiling, not any tree."""
        skill, world, standing = self.fell(trunk(4, 0, top=67), tree_up=None)
        self.assertEqual(standing, [], skill.done_reason)
        self.assertIn("none of it is left standing", skill.done_reason)


class LogCeilingTests(unittest.TestCase):
    """WorldState.log_ceiling: the highest y logs are reported at."""

    def read(self, scan, y=64.0):
        text = json.dumps({"schema": SCHEMA, "written_at_ms": NOW_MS,
                           "in_game": True, "position": [0.5, y, 0.5],
                           "scan": scan})
        return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                    clock=lambda: NOW_MS / 1000.0).read()

    def test_old_and_new_jars(self):
        self.assertEqual(self.read({"radius": 10, "up": 4}).log_ceiling, 68)
        self.assertEqual(self.read({"radius": 10, "up": 4, "tree_up": 12}
                                   ).log_ceiling, 76)
        self.assertEqual(self.read({"radius": 10, "up": 4}, y=70.9
                                   ).log_ceiling, 74)

    def test_unknown_without_a_scan(self):
        self.assertIsNone(self.read(None).log_ceiling)
        self.assertIsNone(self.read({"radius": 10}).log_ceiling)


if __name__ == "__main__":
    unittest.main(verbosity=2)
