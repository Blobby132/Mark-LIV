"""
N7: the entity list says WHAT a dropped item is.

The bridge reported a dropped item as `minecraft:item` and nothing more, so
collect_logs' pickup could not tell the log it had just broken from a
sapling or an apple falling out of the decaying leaves beside it -- and
walked to whichever was nearest. The mod now adds, for item entities only,
`"item": {"name": ..., "count": ...}`.

Additive under schema /4, deliberately not a bump to /5: the reader refuses
any schema it does not know (SUPPORTED_SCHEMAS), so /5 would have made every
existing reader drop the bridge altogether. Older readers ignore the new
key; with an older jar the key is missing, EntityRef.item is None, and the
pickup falls back to every item near a break, as before.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import skills                                        # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA       # noqa: E402
from minecraft.state import EXACT, EntityRef, ItemStack, WorldState  # noqa: E402

SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"
JAR = ROOT / "mods" / "markliv-bridge-1.0.0.jar"


def read(entities):
    text = json.dumps({
        "schema": SCHEMA, "written_at_ms": 1_700_000_000_000, "in_game": True,
        "position": [0.5, 64.0, 0.5], "rotation": [0.0, 0.0],
        "nearby_entities": entities})
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: 1_700_000_000.0,
                                sleeper=lambda _s: None).read()


def dropped(name, x=0.5, z=1.5, count=1):
    return {"name": "minecraft:item", "distance": 1.0,
            "position": [x, 64.0, z], "category": "item",
            "item": {"name": f"minecraft:{name}", "count": count}}


class ReaderTests(unittest.TestCase):

    def test_the_stack_is_read(self):
        entity = read([dropped("oak_log", count=3)]).nearby_entities[0]
        self.assertEqual(entity.item, ItemStack(name="oak_log", count=3))

    def test_an_older_jar_leaves_it_unknown(self):
        old = dropped("oak_log")
        del old["item"]
        self.assertIsNone(read([old]).nearby_entities[0].item)

    def test_a_malformed_stack_is_unknown_not_invented(self):
        for bad in ("oak_log", {"count": 2}, {"name": 7}, None):
            with self.subTest(bad=bad):
                entity = dict(dropped("oak_log"), item=bad)
                self.assertIsNone(read([entity]).nearby_entities[0].item)

    def test_it_is_in_the_state_dict(self):
        entity = read([dropped("oak_log", count=2)]).nearby_entities[0]
        self.assertEqual(entity.as_dict()["item"],
                         {"slot": None, "name": "oak_log", "count": 2})


def item(name=None, x=0.5, z=1.5):
    stack = ItemStack(name=name, count=1) if name else None
    return EntityRef(name="item", distance=1.0, position=(x, 64.0, z),
                     category="item", hostile=False, item=stack)


class DropsTests(unittest.TestCase):

    def drops(self, *entities):
        skill = skills.create("collect_logs")
        skill._broken_at = [(0, 64, 0)]
        state = WorldState(position=(0.5, 64.0, 0.5),
                           nearby_entities=entities, source="bridge",
                           confidence=EXACT)
        return skill._drops(state)

    def test_a_sapling_is_not_walked_to(self):
        self.assertEqual(self.drops(item("oak_sapling"), item("apple")), [])

    def test_the_log_is_chosen_over_a_nearer_sapling(self):
        log = item("oak_log", z=2.5)
        self.assertEqual(self.drops(item("oak_sapling", z=1.0), log), [log])

    def test_unnamed_items_from_an_older_jar_are_still_picked_up(self):
        unknown = item()
        self.assertEqual(self.drops(unknown), [unknown])


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's helper")
class ModHelperTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SRC / "Nearest.java"),
                        str(SRC / "Kinds.java"),
                        str(ROOT / "tests" / "java" / "NearestCheck.java")],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def stack(self, name, count):
        out = subprocess.run(
            ["java", "-cp", self.build.name, "com.markliv.bridge.NearestCheck"],
            input=f"stack {name} {count}\n", capture_output=True, text=True,
            check=True).stdout.strip()
        return out

    def test_the_stack_is_json_the_reader_takes(self):
        text = self.stack("minecraft:oak_log", 3)
        self.assertEqual(json.loads(text),
                         {"name": "minecraft:oak_log", "count": 3})

    def test_a_name_that_is_not_a_registry_name_is_not_sent(self):
        self.assertEqual(self.stack('evil"name', 1), "null")


class CommittedJarTests(unittest.TestCase):

    def test_the_jar_sends_item_stacks(self):
        with zipfile.ZipFile(JAR) as jar:
            names = jar.namelist()
            kinds = jar.read(next(n for n in names
                                  if n.endswith("/Kinds.class")))
            bridge = jar.read(next(n for n in names
                                   if n.endswith("/MarkLivBridge.class")))
        self.assertIn(b"stackJson", kinds, "the jar predates item stacks")
        self.assertIn(b"stackJson", bridge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
