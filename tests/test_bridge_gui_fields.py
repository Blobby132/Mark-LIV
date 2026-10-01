"""
B3b: the bridge reports the open screen, its slots and the pointer.

Additive fields under schema /4, only while a screen is open: `screen`
{kind}, and for a container screen `gui` {scale, window_px, cursor_px},
`slots` [{i, role, x, y, item, count}] with each slot's centre in window
pixels, and `carried`; `game_mode` always. The arithmetic and the JSON live
in fabric-mod/.../Gui.java, free of Minecraft types, so these tests compile
it with a harness and check it -- including how big the payload gets.
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

from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA       # noqa: E402
from minecraft.state import GuiSlot, ItemStack                      # noqa: E402

SRC = ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv" \
    / "bridge"
HARNESS = ROOT / "tests" / "java" / "GuiCheck.java"
RESOURCES = ROOT / "fabric-mod" / "src" / "main" / "resources"
JAR = ROOT / "mods" / "markliv-bridge-1.0.0.jar"


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"),
                     "needs a JDK to compile the mod's GUI helper")
class GuiHelperTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        subprocess.run(["javac", "-d", cls.build.name, str(SRC / "Kinds.java"),
                        str(SRC / "Gui.java"), str(HARNESS)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def ask(self, *lines):
        out = subprocess.run(
            ["java", "-cp", self.build.name, "com.markliv.bridge.GuiCheck"],
            input="\n".join(lines) + "\n", capture_output=True, text=True,
            check=True).stdout.splitlines()
        return out

    def test_slot_roles(self):
        out = self.ask("role true false false 0", "role false true false 0",
                       "role false false true 4", "role false false true 20",
                       "role false false true 37", "role false false true 40",
                       "role false false false 3")
        self.assertEqual(out, ["craft_out", "craft_in", "hotbar", "inventory",
                               "armor", "offhand", "other"])

    def test_a_slot_centre_is_in_window_pixels(self):
        # leftPos 100 + slot x 8 + half a slot (8), at 3 pixels per unit.
        self.assertEqual(self.ask("centre 100 8 3.0"), ["348.0"])

    def test_the_slot_json_is_what_the_reader_takes(self):
        line = self.ask("slot 36 hotbar 348.0 420.5 minecraft:oak_log 12")[0]
        self.assertEqual(json.loads(line), {
            "i": 36, "role": "hotbar", "x": 348.0, "y": 420.5,
            "item": "minecraft:oak_log", "count": 12})
        empty = json.loads(self.ask("slot 1 craft_in 10.0 10.0 null 0")[0])
        self.assertIsNone(empty["item"])

    def test_a_name_that_is_not_a_registry_name_is_not_sent(self):
        line = self.ask('slot 1 craft_in 10.0 10.0 bad"name 1')[0]
        self.assertIsNone(json.loads(line)["item"])

    def test_the_view(self):
        line = self.ask("view 2.5 1920 1080 960.25 540.0")[0]
        self.assertEqual(json.loads(line), {"scale": 2.5,
                                            "window_px": [1920, 1080],
                                            "cursor_px": [960.25, 540.0]})

    def test_the_payload_stays_small(self):
        """A full crafting-table screen: 46 slots, every one holding an item
        with a long name. Measured, and capped."""
        size = int(self.ask("measure 46")[0])
        self.assertLess(size, 6000, f"{size} bytes for the GUI fields")
        capped = int(self.ask("count 500")[0])
        self.assertEqual(capped, 64, "the slot list is not capped")


def read(extra):
    text = json.dumps({"schema": SCHEMA, "written_at_ms": 1_700_000_000_000,
                       "in_game": True, "position": [0.5, 64.0, 0.5],
                       "rotation": [0.0, 0.0], **extra})
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: 1_700_000_000.0,
                                sleeper=lambda _s: None).read()


class ReaderTests(unittest.TestCase):

    def test_the_fields_are_read(self):
        state = read({
            "screen": {"kind": "crafting_table"},
            "gui": {"scale": 3.0, "window_px": [1920, 1080],
                    "cursor_px": [960.0, 540.0]},
            "slots": [{"i": 1, "role": "craft_in", "x": 300.0, "y": 200.0,
                       "item": "minecraft:oak_planks", "count": 1},
                      {"i": 0, "role": "craft_out", "x": 500.0, "y": 230.0,
                       "item": None, "count": 0}],
            "carried": {"name": "minecraft:stick", "count": 4},
            "game_mode": "survival"})
        self.assertEqual(state.screen, "crafting_table")
        self.assertEqual(state.gui.scale, 3.0)
        self.assertEqual(state.gui.cursor_px, (960.0, 540.0))
        self.assertEqual(state.slots[0], GuiSlot(i=1, role="craft_in", x=300.0,
                                                 y=200.0, item="oak_planks",
                                                 count=1))
        self.assertIsNone(state.slots[1].item)
        self.assertEqual(state.carried, ItemStack(name="stick", count=4))
        self.assertEqual(state.game_mode, "survival")

    def test_an_older_jar_reports_no_screen(self):
        state = read({})
        for name in ("screen", "gui", "slots", "carried", "game_mode"):
            self.assertIsNone(getattr(state, name))

    def test_malformed_slots_are_dropped(self):
        state = read({"screen": {"kind": "inventory"},
                      "slots": [{"i": "x"}, {"i": 2, "role": "hotbar",
                                             "x": 1.0, "y": 2.0}, 7]})
        self.assertEqual([s.i for s in state.slots], [2])

    def test_they_are_in_the_state_dict(self):
        state = read({"screen": {"kind": "inventory"}, "game_mode": "creative"})
        out = state.as_dict()
        self.assertEqual(out["screen"], "inventory")
        self.assertEqual(out["game_mode"], "creative")


class ModPackagingTests(unittest.TestCase):

    def test_the_access_widener_opens_only_the_screen_origin(self):
        widener = (RESOURCES / "markliv-bridge.accesswidener").read_text()
        lines = [l for l in widener.splitlines() if l and not l.startswith("#")]
        self.assertEqual(lines[0], "accessWidener v2 named")
        self.assertEqual(sorted(lines[1:]), sorted([
            "accessible field net/minecraft/client/gui/screens/inventory/"
            "AbstractContainerScreen leftPos I",
            "accessible field net/minecraft/client/gui/screens/inventory/"
            "AbstractContainerScreen topPos I"]))
        mod = json.loads((RESOURCES / "fabric.mod.json").read_text())
        self.assertEqual(mod["accessWidener"], "markliv-bridge.accesswidener")
        self.assertNotIn("fabric-api", json.dumps(mod.get("depends", {})))

    def test_the_jar_carries_them(self):
        with zipfile.ZipFile(JAR) as jar:
            names = jar.namelist()
            bridge = jar.read(next(n for n in names
                                   if n.endswith("/MarkLivBridge.class")))
            self.assertTrue(any(n.endswith("/Gui.class") for n in names))
            self.assertIn("markliv-bridge.accesswidener", names)
        for key in (b"screen", b"slots", b"carried", b"game_mode"):
            self.assertIn(key, bridge)


if __name__ == "__main__":
    unittest.main(verbosity=2)
