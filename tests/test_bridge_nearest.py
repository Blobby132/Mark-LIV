"""
The mod's capped lists keep what is NEAREST, per kind -- not what it found
first.

The terrain scan runs west to east and kept the first 64 notable blocks it
met. In a forest that was every one of them six or more blocks west: the
tree the player stood at was never reported, nor any log to the east, and
tree_logs() grouped partial trees from what was left. The same first-come
rule on the entity list let dropped items push a zombie off it.

fabric-mod/.../Nearest.java and Kinds.java hold the rules, free of Minecraft
types; these tests compile them with tests/java/NearestCheck.java. They need
a JDK and are skipped without one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"
HARNESS = ROOT / "tests" / "java" / "NearestCheck.java"

# The mod's own quotas, from MarkLivBridge.NOTABLE_QUOTAS.
NOTABLE = "log=160,ore=48,lava=24,water=24,station=16"


def forest():
    """A trunk of five logs every three blocks, and a pond to the west --
    offered in the scan's own order: x -10..10, then z, then top down."""
    logs = {(x, y, z) for x in range(-9, 10, 3) for z in range(-9, 10, 3)
            for y in range(0, 5)}
    water = {(x, -1, z) for x in range(-10, -5) for z in range(-3, 4)}
    order = []
    for x in range(-10, 11):
        for z in range(-10, 11):
            for y in range(4, -6, -1):
                p = (x, y, z)
                if p in logs:
                    order.append(("log", p))
                elif p in water:
                    order.append(("water", p))
    return order, logs, water


def label(p):
    return f"{p[0]}/{p[1]}/{p[2]}"


def unlabel(text):
    return tuple(int(v) for v in text.split("/"))


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's selection rules")
class NearestTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SRC / "Nearest.java"),
                        str(SRC / "Kinds.java"), str(HARNESS)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def drive(self, lines):
        out = subprocess.run(
            ["java", "-cp", self.build.name, "com.markliv.bridge.NearestCheck"],
            input="\n".join(lines) + "\n", capture_output=True, text=True,
            check=True).stdout.split("\n")
        return out

    def select(self, quotas, offers, first=()):
        lines = [f"quotas {quotas}"]
        if first:
            lines.append(f"first {','.join(first)}")
        lines += [f"offer {cat} {dist} {lab}" for cat, dist, lab in offers]
        lines.append("select")
        out = self.drive(lines)
        return out[:out.index("end")]

    # ── the forest from the bug ──────────────────────────────────────────

    def test_the_tree_you_stand_at_is_reported(self):
        order, logs, _water = forest()
        offers = [(cat, sum(v * v for v in p), label(p)) for cat, p in order]
        kept = [unlabel(t) for t in self.select(NOTABLE, offers)]
        self.assertIn((0, 0, 0), kept, "the nearest tree went unreported")
        east = [p for p in kept if p[0] > 0 and p in logs]
        self.assertGreater(len(east), 30, "east of the player is still blind")

    def test_it_keeps_exactly_the_nearest_logs(self):
        order, logs, _water = forest()
        offers = [(cat, sum(v * v for v in p), label(p)) for cat, p in order]
        kept = {unlabel(t) for t in self.select(NOTABLE, offers)}
        by_distance = sorted(logs, key=lambda p: sum(v * v for v in p))
        cutoff = sum(v * v for v in by_distance[159])
        for p in logs:
            if sum(v * v for v in p) < cutoff:
                self.assertIn(p, kept)

    def test_a_lake_does_not_crowd_out_the_logs(self):
        offers = [("water", 1, f"w{i}") for i in range(4000)]
        offers += [("log", 50, "far_log")]
        kept = self.select(NOTABLE, offers)
        self.assertIn("far_log", kept)
        self.assertEqual(sum(1 for t in kept if t.startswith("w")), 24)

    def test_nearest_first_and_ties_go_to_the_earlier(self):
        kept = self.select("log=2", [("log", 9, "c"), ("log", 4, "a"),
                                     ("log", 4, "b"), ("log", 1, "z")])
        self.assertEqual(kept, ["z", "a"])

    def test_named_kinds_come_first(self):
        kept = self.select("hostile=4,item=4",
                           [("item", 1, "stick"), ("hostile", 9, "zombie")],
                           first=("hostile",))
        self.assertEqual(kept, ["zombie", "stick"])

    def test_unknown_kinds_are_dropped(self):
        self.assertEqual(self.select("log=4", [("pumpkin", 1, "p")]), [])

    # ── entities ─────────────────────────────────────────────────────────

    ENTITIES = "hostile=16,player=8,passive=12,item=12,other=8"

    def test_a_zombie_behind_a_pile_of_drops_is_still_reported(self):
        """After felling a tree: eighteen items and six sheep, and a zombie
        that the level happened to list last. It used to be the 25th."""
        offers = [("item", 2.0, f"item{i}") for i in range(18)]
        offers += [("passive", 6.0, f"sheep{i}") for i in range(6)]
        offers += [("hostile", 2.5, "zombie")]
        kept = self.select(self.ENTITIES, offers, first=("hostile",))
        self.assertEqual(kept[0], "zombie", "hostiles are listed first")
        self.assertEqual(sum(1 for t in kept if t.startswith("item")), 12)

    def test_the_nearest_hostiles_are_the_ones_kept(self):
        offers = [("hostile", float(d), f"mob{d}") for d in range(30, 0, -1)]
        kept = self.select(self.ENTITIES, offers, first=("hostile",))
        self.assertEqual(kept[:3], ["mob1", "mob2", "mob3"])
        self.assertEqual(len(kept), 16)

    def test_unforeseen_entity_kinds_share_the_other_quota(self):
        out = self.drive([f"group {c}" for c in
                          ("hostile", "item", "misc", "creature")])
        self.assertEqual(out[:4], ["hostile", "item", "other", "other"])

    # ── what counts as notable ───────────────────────────────────────────

    def test_kinds(self):
        names = ["minecraft:oak_log", "minecraft:birch_wood",
                 "minecraft:iron_ore", "minecraft:lava", "minecraft:water",
                 "minecraft:crafting_table", "minecraft:stone"]
        out = self.drive([f"kind {n}" for n in names])
        self.assertEqual(out[:len(names)],
                         ["log", "log", "ore", "lava", "water", "station",
                          "null"])


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
