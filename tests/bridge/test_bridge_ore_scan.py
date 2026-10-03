"""
The mod's ore finder (OreScan.java): what it reports, and what it costs.

`ores` is additive under schema /4, sent only in a single-player world:
{"origin", "radius" 24, "down" 32, "up" 16, "complete", "complete_within",
"ores": [{"name", "x", "y", "z", "exposed", "fluid_near"}, ...]} -- the
nearest 16 of each ore, the nearest 64 of those, nearest first, within
16 KB. `exposed`: one of the six neighbours is air or fluid. `fluid_near`:
water or lava within 2 cells. `complete`: every section of the volume was
in a loaded chunk and no ore was left out.

The cost rules: a section whose palette cannot hold an ore is skipped
unread; one step reads at most SLICE_CELLS cells (plus the rest of the
section it is in); a new pass starts at most once a second; only a
finished pass is published. The timing test runs the worst case -- every
section claims it may hold ore -- in the Java harness, where a cell read
is an array lookup: it measures the scan's own work, not the game's
getBlockState.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest

from tests.support.paths import JAVA_HARNESS_DIR, REPO_ROOT as ROOT

SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"
HARNESS = JAVA_HARNESS_DIR / "OreScanCheck.java"

SLICE_CELLS = 16_384
SECTION = 16 * 16 * 16
WORLD = "world -40 0 -40 40 90 40 80"     # stone up to y 79, air above
FEET = "0 64 0"


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's helper")
class OreScanTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name,
                        str(SRC / "OreScan.java"), str(HARNESS)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def ask(self, *lines):
        return subprocess.run(
            ["java", "-cp", self.build.name,
             "com.markliv.bridge.OreScanCheck"],
            input="\n".join(lines) + "\n", capture_output=True, text=True,
            check=True).stdout.splitlines()

    def test_a_world_opened_to_lan_is_not_single_player(self):
        """OreScan.singleplayer(integrated server, published): only a world
        the client runs for itself and has not opened to LAN."""
        out = self.ask("single 1 0", "single 1 1", "single 0 0",
                       "single 0 1")
        self.assertEqual(out, ["true", "false", "false", "false"])

    def scan(self, *setup):
        out = self.ask(WORLD, *setup, f"run {FEET}", "json")
        return json.loads(out[-1]), out[:-1]

    def ores(self, field):
        return {(o["x"], o["y"], o["z"]): o for o in field["ores"]}

    # ── what it reports ──────────────────────────────────────────────────

    def test_nothing_is_published_before_a_pass_finishes(self):
        out = self.ask(WORLD, "set 3 60 0 minecraft:iron_ore",
                       "palette all", f"step {FEET} 1000000", "json")
        self.assertEqual(out[-1], "null")

    def test_the_field(self):
        field, _ = self.scan("set 3 60 0 minecraft:iron_ore")
        self.assertEqual((field["radius"], field["down"], field["up"]),
                         (24, 32, 16))
        self.assertEqual(field["origin"], [0, 64, 0])
        self.assertTrue(field["complete"])
        self.assertIsNone(field["complete_within"])
        self.assertEqual(field["ores"], [
            {"name": "minecraft:iron_ore", "x": 3, "y": 60, "z": 0,
             "exposed": False, "fluid_near": False}])

    def test_exposed_means_air_or_fluid_beside_it(self):
        field, _ = self.scan(
            "set 3 60 0 minecraft:coal_ore", "set 4 60 0 air",
            "set -3 60 0 minecraft:coal_ore", "set -3 61 0 minecraft:water",
            "set 0 50 5 minecraft:coal_ore", "set 1 51 5 air")  # diagonal
        seen = self.ores(field)
        self.assertTrue(seen[(3, 60, 0)]["exposed"])
        self.assertTrue(seen[(-3, 60, 0)]["exposed"])
        self.assertFalse(seen[(0, 50, 5)]["exposed"],
                         "a diagonal is not a neighbour")

    def test_fluid_near_is_water_or_lava_within_two(self):
        field, _ = self.scan(
            "set 5 60 0 minecraft:gold_ore", "set 7 62 0 minecraft:lava",
            "set -5 60 0 minecraft:gold_ore", "set -8 60 0 minecraft:lava",
            "set 0 60 8 minecraft:gold_ore", "set 0 58 10 minecraft:water")
        seen = self.ores(field)
        self.assertTrue(seen[(5, 60, 0)]["fluid_near"])
        self.assertFalse(seen[(-5, 60, 0)]["fluid_near"], "three away")
        self.assertTrue(seen[(0, 60, 8)]["fluid_near"])

    def test_the_caps_keep_the_nearest(self):
        lines = []
        for n in range(20):                     # 20 of each, in a line
            for name, z in (("iron", 0), ("coal", 3), ("copper", 6),
                            ("gold", 9), ("redstone", 12)):
                lines.append(f"set {n + 1} 60 {z} minecraft:{name}_ore")
        field, _ = self.scan(*lines)
        ores = field["ores"]
        self.assertEqual(len(ores), 64)
        per_type = {}
        for o in ores:
            per_type[o["name"]] = per_type.get(o["name"], 0) + 1
        self.assertTrue(all(n <= 16 for n in per_type.values()), per_type)
        far = [((o["x"]) ** 2 + (o["y"] - 64) ** 2 + o["z"] ** 2) ** 0.5
               for o in ores]
        self.assertEqual(far, sorted(far), "not nearest first")
        self.assertFalse(field["complete"])
        self.assertLessEqual(max(far), field["complete_within"] + 1e-9,
                             "something kept is farther than one left out")
        self.assertNotIn({"name": "minecraft:iron_ore", "x": 20, "y": 60,
                          "z": 0, "exposed": False, "fluid_near": False},
                         ores, "the 17th iron ore was kept")

    def test_an_unloaded_chunk_makes_it_incomplete(self):
        field, _ = self.scan("set 3 60 0 minecraft:iron_ore", "unload 1 1")
        self.assertFalse(field["complete"])
        self.assertEqual(len(field["ores"]), 1)

    def test_the_byte_cap(self):
        lines = [f"set {x} {y} {z} minecraft:deepslate_lapis_ore"
                 for x in range(-20, 21, 3) for z in range(-20, 21, 3)
                 for y in (40, 50, 60)]
        field, _ = self.scan(*lines)
        self.assertLessEqual(len(json.dumps(field["ores"])), 16 * 1024)

    # ── what it costs ────────────────────────────────────────────────────

    def test_sections_with_no_ore_are_not_read(self):
        _field, out = self.scan()           # stone and air, no ore at all
        steps, most = (int(v) for v in out[-1].split())
        self.assertEqual(most, 0, "it read cells where no ore could be")
        self.assertEqual(steps, 1)

    def test_one_step_reads_a_bounded_slice(self):
        _field, out = self.scan("palette all")
        steps, most = (int(v) for v in out[-1].split())
        self.assertGreater(steps, 1, "the whole volume in one step")
        self.assertLessEqual(most, SLICE_CELLS + SECTION)

    def test_a_new_pass_starts_at_most_once_a_second(self):
        out = self.ask(WORLD, "set 3 60 0 minecraft:iron_ore",
                       f"step {FEET} 1000000",      # a whole pass: one step
                       f"step {FEET} 1000200",      # 200 ms on: nothing
                       f"step {FEET} 1000800",      # 800 ms on: nothing
                       f"step {FEET} 1001000")      # a second on: again
        lookups = [int(line.split()[0]) for line in out]
        self.assertGreater(lookups[0], 0)
        self.assertEqual(lookups[1:3], [0, 0])
        self.assertGreater(lookups[3], 0)

    def test_the_cost_in_the_harness(self):
        """The worst case: every section claims it may hold ore. Reported
        in the round's notes; the bound here is the 2 ms budget."""
        out = self.ask(WORLD, "scatter 7 400 minecraft:iron_ore",
                       "palette all", f"time {FEET} 400")
        average, worst, cells, passes = out[-1].split()
        self.assertGreater(int(passes), 0)
        self.assertLess(float(average), 2.0,
                        f"average {average} ms, worst {worst} ms, "
                        f"{cells} cells a pass")

class SingleplayerSourceTests(unittest.TestCase):
    """What the bridge asks before it calls a world single-player: a world
    opened to LAN still has the integrated server, so
    hasSingleplayerServer() alone said yes to it."""

    def test_the_bridge_asks_whether_the_world_is_published(self):
        source = (SRC / "MarkLivBridge.java").read_text(encoding="utf-8")
        self.assertIn("getSingleplayerServer()", source)
        self.assertIn("isPublished()", source)
        self.assertIn("OreScan.singleplayer(", source)
        self.assertNotIn("hasSingleplayerServer()", source,
                         "true for a LAN world too")


if __name__ == "__main__":
    unittest.main(verbosity=2)
