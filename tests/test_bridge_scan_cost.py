"""
C11: the terrain scan does each block's expensive work once.

The scan reads some 6,000 blocks (21 x 21 columns x 14 rows) five times a
second on the game's own thread. For every non-air block it computed the
collision shape twice -- once for `collides`, again inside `passable` -- and
built the block's registry name as a fresh string, then ran the name
through Kinds.notable. The names and kinds are properties of the block
TYPE, of which a scan sees a few dozen.

Not timed: there is no game to run here. What is checked is the structure
the saving comes from, in the source the jar is built from.
"""

from __future__ import annotations

import re
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv"
          / "bridge" / "MarkLivBridge.java").read_text(encoding="utf-8")


def method(name):
    """The body of the method DECLARED as `name`, not a call to it."""
    start = re.search(r"(?:private|static|public)[^;{=]*\b" + name + r"\(",
                      SOURCE).start()
    depth, i = 0, SOURCE.index("{", start)
    for j in range(i, len(SOURCE)):
        depth += {"{": 1, "}": -1}.get(SOURCE[j], 0)
        if depth == 0:
            return SOURCE[start:j + 1]
    raise AssertionError(name)


class ScanCostTests(unittest.TestCase):

    def test_one_collision_shape_per_block(self):
        scan = method("scanTerrain")
        self.assertEqual(scan.count("getCollisionShape"), 1)
        self.assertNotIn("passable(level", scan,
                         "passable() computes the collision shape again")

    def test_names_and_kinds_come_from_a_per_block_cache(self):
        scan = method("scanTerrain")
        self.assertNotIn("BuiltInRegistries.BLOCK", scan,
                         "a registry name is built for every block read")
        self.assertNotIn("Kinds.notable", scan)
        self.assertRegex(SOURCE, r"IdentityHashMap<\s*Block\b")

    def test_the_jar_has_the_cache(self):
        with zipfile.ZipFile(ROOT / "mods" / "markliv-bridge-1.0.0.jar") as jar:
            compiled = jar.read(next(n for n in jar.namelist()
                                     if n.endswith("/MarkLivBridge.class")))
        self.assertIn(b"blockInfo", compiled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
