"""
C10: which blocks are logs, and which are hazards -- in one place.

There were two LOG_BLOCKS, one in navigation.py and one in skills.py, both
the nine overworld logs. The mod reports anything ending _log or _wood as a
"log", stripped ones included, and the Python side then dropped those and
never knew Nether stems at all: collect_logs in a crimson forest found no
trees.

One definition now, in navigation.py, in two sizes:

  LOG_BLOCKS    natural trunks -- the nine logs and the two Nether stems.
                What finding and felling a tree works on.
  WOOD_BLOCKS   everything log-like: + stripped logs and stems, _wood and
                hyphae. What the "wood" category finds -- walking to it,
                never felling it.

Not everything in one set, as the review suggested, because stripped logs
and _wood do not grow on trees: they are what cabins are built of, and
"fell the tree" must not take one apart.

HAZARDS gains blocks that hurt, trap or teleport a player walking into or
onto them.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.state import EXACT, NearbyBlock, WorldState          # noqa: E402
from tests.support.paths import JAVA_HARNESS_DIR

SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"


class OneDefinitionTests(unittest.TestCase):

    def test_skills_uses_the_navigation_constant(self):
        self.assertIs(skills.LOG_BLOCKS, nav.LOG_BLOCKS)

    def test_nether_trees_are_trees(self):
        for stem in ("crimson_stem", "warped_stem"):
            self.assertIn(stem, nav.LOG_BLOCKS)
            self.assertIn(stem, nav.CATEGORIES["tree"])

    def test_built_wood_is_wood_but_not_a_tree(self):
        for name in ("stripped_oak_log", "oak_wood", "stripped_birch_wood",
                     "crimson_hyphae", "stripped_warped_stem"):
            with self.subTest(name=name):
                self.assertIn(name, nav.WOOD_BLOCKS)
                self.assertIn(name, nav.CATEGORIES["wood"])
                self.assertNotIn(name, nav.LOG_BLOCKS,
                                 "a stripped-log cabin would be a tree")

    def test_every_log_is_wood(self):
        self.assertLessEqual(nav.LOG_BLOCKS, nav.WOOD_BLOCKS)

    def test_a_crimson_forest_has_a_nearest_tree(self):
        surface = [NearbyBlock(x, 63, z, "crimson_nylium", True, 4)
                   for x in range(-6, 7) for z in range(-6, 7)]
        stem = NearbyBlock(3, 64, 0, "crimson_stem", True)
        state = WorldState(position=(0.5, 64.0, 0.5), surface=tuple(surface),
                           notable_blocks=(stem,), scan_radius=6,
                           source="test", confidence=EXACT)
        self.assertEqual(nav.nearest_block(state, "log"), stem)


class HazardTests(unittest.TestCase):

    def test_new_hazards_are_not_walked_into(self):
        for name in ("pointed_dripstone", "lava_cauldron", "nether_portal",
                     "end_portal", "end_gateway", "sculk_shrieker",
                     "sculk_sensor", "calibrated_sculk_sensor", "tripwire"):
            with self.subTest(name=name):
                self.assertIn(name, nav.HAZARDS)
                floor = NearbyBlock(1, 63, 0, "stone", True, 4, cover=name)
                local = nav.LocalMap.from_state(WorldState(
                    position=(0.5, 64.0, 0.5), surface=(
                        NearbyBlock(0, 63, 0, "stone", True, 4), floor),
                    scan_radius=2, source="test", confidence=EXACT))
                self.assertFalse(local.standable(1, 0))


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's naming rules")
class ModKindsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SRC / "Nearest.java"),
                        str(SRC / "Kinds.java"),
                        str(JAVA_HARNESS_DIR / "NearestCheck.java")],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def kind(self, name):
        return subprocess.run(
            ["java", "-cp", self.build.name, "com.markliv.bridge.NearestCheck"],
            input=f"kind {name}\n", capture_output=True, text=True,
            check=True).stdout.strip()

    def test_nether_stems_are_reported_as_logs(self):
        for name in ("minecraft:crimson_stem", "minecraft:warped_stem",
                     "minecraft:crimson_hyphae"):
            with self.subTest(name=name):
                self.assertEqual(self.kind(name), "log")

    def test_other_stems_are_not_logs(self):
        for name in ("minecraft:mushroom_stem", "minecraft:melon_stem",
                     "minecraft:attached_pumpkin_stem"):
            with self.subTest(name=name):
                self.assertEqual(self.kind(name), "null")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CommittedJarTests(unittest.TestCase):

    def test_the_jar_reports_nether_stems(self):
        import zipfile
        with zipfile.ZipFile(ROOT / "mods" / "markliv-bridge-1.0.0.jar") as jar:
            kinds = jar.read(next(n for n in jar.namelist()
                                  if n.endswith("/Kinds.class")))
        self.assertIn(b"woodyStem", kinds, "the jar predates Nether stems")
