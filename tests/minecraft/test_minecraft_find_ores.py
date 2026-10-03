"""
find_ores: what the bridge's ore scan lists, read-only, single-player only.

The mod reports `singleplayer` and, in a single-player world only, `ores`:
the nearest ore blocks within 24 sideways, 32 down and 16 up, buried ones
included, each `exposed` or not and with `fluid_near` (water or lava within
2 cells). find_ores lists them nearest first with the depth relative to the
player's feet. It presses nothing and needs no session. It refuses when the
mod does not say the world is single-player: finding ore inside rock on a
server is x-ray.

Also read here: the `near_blocks` grid (every cell of the box, fluids
named), which digging will use.
"""

from __future__ import annotations

import json
import time
import unittest

from minecraft import mod_bridge
from minecraft.controller import MinecraftController
from minecraft.input_backend import FakeInputBackend
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from tests.support.bridge_payloads import near_grid
from tests.support.fakes import FakeLocator, FakeProcess


def ore(name, x, y, z, exposed=False, fluid_near=False):
    return {"name": f"minecraft:{name}", "x": x, "y": y, "z": z,
            "exposed": exposed, "fluid_near": fluid_near}


ORES = [
    ore("coal_ore", 2, 64, 0, exposed=True),              # 2 away, level
    ore("iron_ore", 3, 60, 1),                            # buried, 4 below
    ore("deepslate_iron_ore", -4, 50, 3, fluid_near=True),
    ore("diamond_ore", 10, 40, -12),                      # ~29 away
    ore("copper_ore", 0, 70, 18, exposed=True),           # ~19 away, above
]


def payload(**extra):
    data = {"schema": SCHEMA, "written_at_ms": int(time.time() * 1000),
            "in_game": True, "position": [0.5, 64.0, 0.5],
            "rotation": [0.0, 0.0], "health": 20.0,
            "features": list(mod_bridge.REQUIRED_FEATURES),
            "singleplayer": True,
            "ores": {"origin": [0, 64, 0], "radius": 24, "down": 32,
                     "up": 16, "complete": True, "complete_within": None,
                     "ores": ORES}}
    data.update(extra)
    return data


def source_of(data):
    text = json.dumps(data)
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=time.time)


class ReaderTests(unittest.TestCase):

    def test_singleplayer(self):
        self.assertIs(source_of(payload()).read().singleplayer, True)
        self.assertIs(source_of(payload(singleplayer=False)).read()
                      .singleplayer, False)
        data = payload()
        del data["singleplayer"]
        self.assertIsNone(source_of(data).read().singleplayer,
                          "an older jar does not say")

    def test_the_ores_are_read(self):
        scan = source_of(payload()).read().ores
        self.assertEqual((scan.radius, scan.down, scan.up), (24, 32, 16))
        self.assertTrue(scan.complete)
        first = scan.ores[1]
        self.assertEqual((first.name, first.position, first.exposed,
                          first.fluid_near),
                         ("iron_ore", (3, 60, 1), False, False))

    def test_no_scan_is_none_not_empty(self):
        self.assertIsNone(source_of(payload(ores=None)).read().ores)
        data = payload()
        del data["ores"]
        self.assertIsNone(source_of(data).read().ores)

    def test_a_garbled_entry_is_dropped_and_the_list_called_incomplete(self):
        broken = payload()
        broken["ores"] = dict(broken["ores"],
                              ores=ORES + [{"name": "minecraft:gold_ore",
                                            "x": "far"}])
        scan = source_of(broken).read().ores
        self.assertEqual(len(scan.ores), len(ORES))
        self.assertFalse(scan.complete)


def grid_payload(cells):
    return near_grid(cells)


class GridReaderTests(unittest.TestCase):

    def near(self, cells):
        return source_of(payload(near_blocks=grid_payload(cells))).read().near

    def test_every_cell_is_known_air_included(self):
        near = self.near({})
        self.assertTrue(near.complete)
        self.assertTrue(near.covers((4, 59, -4)))
        self.assertEqual(near.name_at((0, 63, 0)), "stone")
        self.assertEqual(near.name_at((0, 65, 0)), "air")
        self.assertIsNone(near.block_at((0, 65, 0)))
        self.assertEqual(near.block_at((0, 63, 0)).name, "stone")

    def test_fluids_are_named(self):
        near = self.near({
            (1, 60, 0): ("minecraft:lava", False, "minecraft:lava"),
            (2, 61, 0): ("minecraft:water", False, "minecraft:flowing_water"),
            (3, 62, 0): ("minecraft:oak_stairs", True, "minecraft:water")})
        self.assertEqual(near.fluid_at((1, 60, 0)), "lava")
        self.assertEqual(near.fluid_at((2, 61, 0)), "flowing_water")
        self.assertEqual(near.fluid_at((3, 62, 0)), "water")
        self.assertIsNone(near.fluid_at((0, 63, 0)))

    def test_an_unknown_cell_is_not_covered(self):
        near = self.near({(4, 59, 4): None})
        self.assertFalse(near.complete)
        self.assertFalse(near.covers((4, 59, 4)))
        self.assertIsNone(near.name_at((4, 59, 4)))

    def test_a_grid_of_the_wrong_size_is_not_trusted(self):
        bad = grid_payload({})
        bad["grid"]["runs"][-1] -= 1
        near = source_of(payload(near_blocks=bad)).read().near
        self.assertIsNone(near.cells)

    def test_an_old_jar_has_no_grid(self):
        old = {"origin": [0, 64, 0], "radius": 4, "below": 1, "above": 4,
               "complete_within": None,
               "blocks": [[0, 63, 0, "minecraft:stone", True]]}
        near = source_of(payload(near_blocks=old)).read().near
        self.assertIsNone(near.cells)
        self.assertEqual(near.block_at((0, 63, 0)).name, "stone")


class FindOresTests(unittest.TestCase):

    def setUp(self):
        import actions.minecraft as adapter
        self.adapter = adapter
        self.data = payload()
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0)
        self.bridge = ModBridgeStateSource(
            path="(test)", reader=lambda: json.dumps(self.data),
            clock=time.time)
        adapter._reset_for_tests(controller=controller,
                                 state_source=self.bridge)
        self.addCleanup(adapter._reset_for_tests)

    def find(self, **params):
        return self.adapter.minecraft_control(dict(action="find_ores",
                                                   **params))

    def test_nearest_first_with_depth_and_what_is_around_it(self):
        answer = self.find()
        lines = [line for line in answer.splitlines()
                 if line.lstrip().startswith(("1.", "2.", "3.", "4."))]
        self.assertTrue(lines[0].lstrip().startswith("1. coal ore"), answer)
        self.assertIn("level with your feet", lines[0])
        self.assertIn("exposed", lines[0])
        self.assertIn("iron ore at (3, 60, 1)", lines[1])
        self.assertIn("4 below your feet", lines[1])
        self.assertIn("buried", lines[1])
        self.assertIn("deepslate iron ore", lines[2])
        self.assertIn("water or lava within 2", lines[2])
        self.assertNotIn("diamond", answer, "29 blocks away: outside 16")

    def test_one_kind_of_ore(self):
        answer = self.find(ore="iron")
        self.assertIn("iron ore at (3, 60, 1)", answer)
        self.assertIn("deepslate iron ore", answer)
        self.assertNotIn("coal", answer)
        self.assertNotIn("copper", answer)

    def test_the_radius(self):
        self.assertIn("copper ore", self.find(radius=20))
        self.assertNotIn("copper ore", self.find(radius=16))
        wide = self.find(radius=100)
        self.assertIn("diamond ore", wide)
        self.assertIn("24", wide, "the radius is capped at the scan's")

    def test_none_of_that_kind_says_so_and_invents_nothing(self):
        answer = self.find(ore="emerald")
        self.assertIn("no emerald ore", answer.lower())
        self.assertNotIn(" at (", answer)

    def test_an_incomplete_scan_says_there_may_be_more(self):
        self.data["ores"] = dict(self.data["ores"], complete=False,
                                 complete_within=12.5)
        self.assertIn("may be more", self.find())

    def test_a_server_is_refused_as_x_ray(self):
        self.data["singleplayer"] = False
        answer = self.find()
        self.assertIn("single-player", answer)
        self.assertIn("x-ray", answer)
        self.assertNotIn(" at (", answer)

    def test_an_older_jar_that_does_not_say_is_refused(self):
        del self.data["singleplayer"]
        answer = self.find()
        self.assertIn("single-player", answer)
        self.assertIn("install_mod.bat", answer)
        self.assertNotIn(" at (", answer)

    def test_a_scan_not_finished_yet(self):
        self.data["ores"] = None
        self.assertIn("not finished", self.find())

    def test_it_presses_nothing_and_needs_no_session(self):
        self.find()
        self.assertEqual(self.backend.events, [])
        from actions.minecraft import _mc_capability
        from core import capabilities as caps
        self.assertEqual(_mc_capability({"action": "find_ores"}),
                         caps.MINECRAFT_READ_STATE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
