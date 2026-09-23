"""
The bridge mod's choice of floor in each terrain column.

From a real run: on flat, open ground, every "walk to that tree" failed with
"no walkable route". The mod reported the TOPMOST block of each column, and
under a tree that is the canopy -- so every column round a trunk looked like
a wall of leaves four blocks up, and the planner, correctly given what it was
told, found no way in.

The rule now lives in fabric-mod/.../ColumnScan.java, free of Minecraft
types. These tests compile it with the harness in tests/java and check it
against columns built by hand. They need a JDK (javac) and are skipped
without one; the Python side of the same failure is covered in
test_navigation_ground_cover.py.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge" / "ColumnScan.java"
HARNESS = ROOT / "tests" / "java" / "ColumnScanCheck.java"

# The mod's constants, which the harness is told rather than guessing.
SCAN_UP, SCAN_DOWN, MAX_CLEARANCE, PLAYER_HEIGHT = 4, 5, 4, 2
TOP = SCAN_UP + MAX_CLEARANCE          # dy of index 0
FIRST = MAX_CLEARANCE                  # index of dy = SCAN_UP
LAST = TOP + SCAN_DOWN                 # index of dy = -SCAN_DOWN
FEET = TOP                             # index of dy = 0


def index(dy: int) -> int:
    return TOP - dy


def dy_of(i: int):
    return None if i < 0 else TOP - i


def column(**blocks):
    """A column top down. Keywords are dy values spelled d0, d1, m1 (minus
    one)...; each maps to air/plant/fluid/anything-solid. Unlisted is air."""
    kinds = ["air"] * (LAST + 1)
    for key, kind in blocks.items():
        dy = -int(key[1:]) if key[0] == "m" else int(key[1:])
        kinds[index(dy)] = kind
    return kinds


def ground(*, top=-1, bottom=-SCAN_DOWN, kind="stone"):
    """Solid from dy=top down to the bottom of the scan."""
    return {(f"m{-dy}" if dy < 0 else f"d{dy}"): kind
            for dy in range(bottom, top + 1)}


def spell(dy: int) -> str:
    return f"m{-dy}" if dy < 0 else f"d{dy}"


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's ColumnScan")
class FloorScanTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SOURCE),
                        str(HARNESS)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def scan(self, *columns):
        lines = [" ".join([str(FEET), str(FIRST), str(LAST),
                           str(PLAYER_HEIGHT), str(MAX_CLEARANCE)] + kinds)
                 for kinds in columns]
        out = subprocess.run(
            ["java", "-cp", self.build.name,
             "com.markliv.bridge.ColumnScanCheck"],
            input="\n".join(lines) + "\n", capture_output=True, text=True,
            check=True).stdout.split("\n")
        results = []
        for row in out[:len(columns)]:
            floor, top, clearance, cover = (int(v) for v in row.split())
            results.append({"floor": dy_of(floor), "top": dy_of(top),
                            "clearance": clearance, "cover": dy_of(cover)})
        return results if len(results) > 1 else results[0]

    # ── the failure from the run ─────────────────────────────────────────

    def test_under_a_canopy_the_floor_is_the_ground_not_the_leaves(self):
        blocks = ground()
        blocks.update({spell(dy): "oak_leaves" for dy in range(2, 9)})
        got = self.scan(column(**blocks))
        self.assertEqual(got["floor"], -1,
                         "the canopy was reported as the floor again -- "
                         "every tree becomes unreachable")
        self.assertEqual(got["clearance"], 2)
        self.assertEqual(got["top"], SCAN_UP,
                         "the topmost block is the canopy: the old answer")

    def test_a_grass_tuft_sits_on_the_floor_and_is_reported_as_cover(self):
        blocks = ground()
        blocks["d0"] = "plant"
        got = self.scan(column(**blocks))
        self.assertEqual(got["floor"], -1)
        self.assertEqual(got["cover"], 0)
        self.assertEqual(got["clearance"], MAX_CLEARANCE)

    def test_tall_grass_is_cover_too(self):
        blocks = ground()
        blocks.update(d0="plant", d1="plant")
        got = self.scan(column(**blocks))
        self.assertEqual(got["floor"], -1)
        self.assertEqual(got["cover"], 0)

    # ── what must not change ─────────────────────────────────────────────

    def test_open_ground_is_unchanged(self):
        got = self.scan(column(**ground()))
        self.assertEqual((got["floor"], got["clearance"], got["cover"]),
                         (-1, MAX_CLEARANCE, None))

    def test_a_trunk_is_not_a_floor(self):
        blocks = ground()
        blocks.update({spell(dy): "oak_log" for dy in range(0, 5)})
        blocks.update({spell(dy): "oak_leaves" for dy in range(5, 9)})
        got = self.scan(column(**blocks))
        self.assertIsNone(got["floor"])
        self.assertEqual(got["top"], SCAN_UP,
                         "falls back to the topmost block, as before")
        self.assertEqual(got["clearance"], 0,
                         "and says there is no room on it")

    def test_a_canopy_too_low_to_stand_under_is_not_hidden(self):
        blocks = ground()
        blocks.update({spell(dy): "oak_leaves" for dy in range(1, 4)})
        got = self.scan(column(**blocks))
        self.assertEqual(got["floor"], 3,
                         "one block of room under the leaves is not room; "
                         "the only floor is on top of them")

    def test_a_riverbed_is_not_dry_floor(self):
        blocks = ground(top=-3, kind="sand")
        blocks.update(m2="fluid", m1="fluid")
        got = self.scan(column(**blocks))
        self.assertIsNone(got["floor"],
                          "water above the sand is not headroom")
        self.assertEqual(got["top"], -1, "the water's surface is reported")

    def test_equally_near_floors_prefer_the_lower_one(self):
        # stone at -3 with room up to 0; a ledge at +1 with room above it.
        blocks = ground(top=-3)
        blocks["d1"] = "stone"
        got = self.scan(column(**blocks))
        self.assertEqual(got["floor"], -3,
                         "a two-block drop is walkable; a two-block climb "
                         "is not")

    def test_in_a_cave_the_floor_is_under_the_feet_not_above_the_roof(self):
        blocks = ground()
        blocks.update({spell(dy): "stone" for dy in range(2, 9)})
        got = self.scan(column(**blocks))
        self.assertEqual((got["floor"], got["clearance"]), (-1, 2))

    def test_a_hazard_walked_through_is_reported(self):
        blocks = ground()
        blocks["d1"] = "plant"          # e.g. a vine or berry bush at the head
        got = self.scan(column(**blocks))
        self.assertEqual(got["cover"], 1)

    def test_nothing_in_range_reports_nothing(self):
        got = self.scan(column())
        self.assertEqual((got["floor"], got["top"]), (None, None))


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
