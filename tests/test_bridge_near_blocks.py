"""
B4d (2/2): the bridge reports `near_blocks` -- every non-air block in a
small box around the player -- so a finished structure can be checked
block by block, and a cell under a wall's top is no longer unknown.

Additive under schema /4: {"origin": [x, y, z] (the feet block), "radius",
"below", "above", "complete_within", "blocks": [[x, y, z, name, solid],
...]}. Nearest first, with quotas: solid blocks and passable ones (grass,
torches) separately. `complete_within` says how far the list is complete:
null when nothing was left out, else the distance of the nearest block
that was -- so a missing cell nearer than that is air, and beyond it
unknown. The selection and the JSON live in NearBlocks.java, free of
Minecraft types; these tests compile it with a harness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import building                                      # noqa: E402
from minecraft import navigation as nav                             # noqa: E402

SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"
HARNESS = ROOT / "tests" / "java" / "NearBlocksCheck.java"


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's helper")
class NearBlocksJavaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SRC / "Nearest.java"),
                        str(SRC / "Gui.java"), str(SRC / "Kinds.java"),
                        str(SRC / "NearBlocks.java"), str(HARNESS)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def ask(self, *lines):
        return subprocess.run(
            ["java", "-cp", self.build.name,
             "com.markliv.bridge.NearBlocksCheck"],
            input="\n".join(lines) + "\n", capture_output=True, text=True,
            check=True).stdout.splitlines()

    def test_the_box(self):
        out = self.ask("inbox 4 0 4", "inbox 5 0 0", "inbox 0 -1 0",
                       "inbox 0 -2 0", "inbox 0 4 0", "inbox 0 5 0")
        self.assertEqual(out, ["true", "false", "true", "false", "true",
                               "false"])

    def test_a_small_world_is_complete(self):
        out = self.ask("origin 10 64 -5",
                       "offer 10 63 -5 minecraft:grass_block true",
                       "offer 11 64 -5 minecraft:cobblestone true",
                       "offer 9 64 -5 minecraft:short_grass false",
                       "json")
        snapshot = json.loads(out[0])
        self.assertEqual(snapshot["origin"], [10, 64, -5])
        self.assertEqual((snapshot["radius"], snapshot["below"],
                          snapshot["above"]), (4, 1, 4))
        self.assertIsNone(snapshot["complete_within"])
        self.assertEqual(snapshot["blocks"][0],
                         [10, 63, -5, "minecraft:grass_block", True])
        self.assertIn([9, 64, -5, "minecraft:short_grass", False],
                      snapshot["blocks"])

    def test_past_the_quota_it_keeps_the_nearest_and_says_how_far(self):
        lines = ["origin 0 64 0"]
        for dx in range(-4, 5):
            for dz in range(-4, 5):
                for y in range(63, 69):
                    lines.append(f"offer {dx} {y} {dz} minecraft:stone true")
        out = self.ask(*lines, "json", "measure")
        snapshot = json.loads(out[0])
        kept = snapshot["blocks"]
        self.assertEqual(len(kept), 192)
        dropped = snapshot["complete_within"]
        self.assertIsNotNone(dropped)
        far = max(((b[0]) ** 2 + (b[1] - 64) ** 2 + b[2] ** 2) ** 0.5
                  for b in kept)
        self.assertLessEqual(far, dropped + 1e-9,
                             "something kept is farther than one left out")
        size = int(out[1])
        self.assertLess(size, 12000, f"{size} bytes at the full quota")

    def test_a_name_that_is_not_a_registry_name_is_not_sent(self):
        out = self.ask("origin 0 64 0", 'offer 1 64 0 bad"name true', "json")
        self.assertEqual(json.loads(out[0])["blocks"][0][3], None)


class ReaderTests(unittest.TestCase):

    def read(self, near):
        sys.path.insert(0, str(ROOT / "tests"))
        from test_minecraft_mod_bridge import payload, source
        data = payload(near_blocks=near)
        if near is None:
            del data["near_blocks"]
        return source(data).read()

    def snapshot(self, blocks, complete_within=None):
        return {"origin": [0, 64, 0], "radius": 4, "below": 1, "above": 4,
                "complete_within": complete_within, "blocks": blocks}

    def test_it_is_read(self):
        state = self.read(self.snapshot(
            [[0, 63, 0, "minecraft:grass_block", True],
             [1, 64, 0, "minecraft:short_grass", False]]))
        near = state.near
        self.assertEqual(near.origin, (0, 64, 0))
        self.assertEqual(near.radius, 4)
        self.assertEqual([(b.position, b.name, b.solid) for b in near.blocks],
                         [((0, 63, 0), "grass_block", True),
                          ((1, 64, 0), "short_grass", False)])

    def test_an_old_mod_reports_none(self):
        self.assertIsNone(self.read(None).near)

    def test_a_garbled_entry_distrusts_the_whole_snapshot(self):
        """Dropping one entry would make its cell read as air -- the one
        mistake that matters. A snapshot with a bad entry is not used."""
        state = self.read(self.snapshot([[0, 63, 0, "minecraft:stone", True],
                                         ["x", 1, 2, "minecraft:stone", True]]))
        self.assertIsNone(state.near)
        state = self.read(self.snapshot([[0, 63, 0, "minecraft:stone", True],
                                         [1, 2]]))
        self.assertIsNone(state.near)


class CellRuleTests(unittest.TestCase):
    """What the snapshot changes in building.what_is_at."""

    def verdict(self, state, cell):
        return building.what_is_at(nav.LocalMap.from_state(state), state,
                                   cell)

    def state(self, blocks, complete_within=None):
        from minecraft.state import EXACT, NearbyBlock, NearSnapshot, \
            WorldState
        near = NearSnapshot(origin=(0, 64, 0), radius=4, below=1, above=4,
                            complete_within=complete_within,
                            blocks=tuple(NearbyBlock(*b) for b in blocks))
        # The scan alone: a 2-high wall at (1, 0) makes its floor 65, so
        # (1, 64, 0) is under the floor -- unknown to the scan.
        surface = (NearbyBlock(1, 65, 0, "cobblestone", True, 4, None),
                   NearbyBlock(2, 63, 0, "grass_block", True, 4, None))
        return WorldState(position=(0.5, 64.0, 0.5), surface=surface,
                          near=near, source="bridge", confidence=EXACT)

    def test_a_block_under_a_wall_top_is_known(self):
        state = self.state([(1, 64, 0, "cobblestone", True),
                            (1, 65, 0, "cobblestone", True)])
        self.assertEqual(self.verdict(state, (1, 64, 0)),
                         (building.OCCUPIED, "cobblestone"))

    def test_a_cell_missing_from_a_complete_list_is_air(self):
        state = self.state([(1, 65, 0, "cobblestone", True)])
        self.assertEqual(self.verdict(state, (1, 64, 0))[0], building.EMPTY)

    def test_beyond_complete_within_the_scan_decides(self):
        state = self.state([(1, 65, 0, "cobblestone", True)],
                           complete_within=1.0)
        self.assertEqual(self.verdict(state, (1, 64, 0))[0],
                         building.UNKNOWN)

    def test_outside_the_box_the_scan_decides(self):
        state = self.state([])
        self.assertEqual(self.verdict(state, (2, 64, 0))[0], building.EMPTY,
                         "the scan's headroom over (2, 63, 0)")
        self.assertEqual(self.verdict(state, (6, 64, 0))[0],
                         building.UNKNOWN)

    def test_a_plant_in_the_list(self):
        state = self.state([(1, 64, 0, "short_grass", False)])
        self.assertEqual(self.verdict(state, (1, 64, 0))[0],
                         building.REPLACE)

    def test_a_listed_solid_block_is_a_reference(self):
        state = self.state([(1, 64, 0, "cobblestone", True)])
        self.assertEqual(building.solid_at(nav.LocalMap.from_state(state),
                                           state, (1, 64, 0)), "cobblestone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
