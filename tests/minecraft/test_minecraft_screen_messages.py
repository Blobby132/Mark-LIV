"""
"No screen is open" and "the mod cannot report screens" are different.

From a real run with an older jar: inventory_close said "No screen is open,
so there is nothing to close" while the inventory was open -- the jar did
not report screens at all -- and craft_item said only "I cannot see the
game's screens". Both are now told apart, and craft_item names the field it
did not get.

The states here are read through the real bridge reader, from payloads
shaped like the old jar (no features list, no game_mode) and the current
one.
"""

from __future__ import annotations

import json
import unittest

from minecraft import mod_bridge, skills
from minecraft.controller import MinecraftController
from minecraft.input_backend import FakeInputBackend
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from minecraft.state import EXACT, WorldState
from tests.support.fakes import FakeLocator, FakeProcess

NOW_MS = 1_700_000_000_000


def read(features=None, **extra):
    data = {"schema": SCHEMA, "written_at_ms": NOW_MS, "in_game": True,
            "position": [0.5, 64.0, 0.5], "rotation": [0.0, 0.0],
            "health": 20.0, "hunger": 20,
            "inventory": [{"slot": 0, "name": "minecraft:oak_log",
                           "count": 4}]}
    if features is not None:
        data["features"] = list(features)
    data.update(extra)
    text = json.dumps(data)
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: NOW_MS / 1000.0).read()


OLD_JAR = dict()                                   # no list: before it
CURRENT = dict(features=mod_bridge.REQUIRED_FEATURES, game_mode="survival")


class InventoryCloseTests(unittest.TestCase):

    def close(self, state):
        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0, gui_probe=lambda: state)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        result = controller.inventory({"state": "close"})
        self.assertFalse(result.ok)
        self.assertEqual(backend.events, [], "ESC was pressed")
        return result.error

    def test_the_old_jar_is_not_reported_as_no_screen(self):
        """The real run: the inventory was open, and the jar could not say."""
        error = self.close(read(**OLD_JAR))
        self.assertNotIn("No screen is open", error)
        self.assertIn("cannot tell whether a screen is open", error)
        self.assertIn("older", error)
        self.assertIn("install_mod.bat", error)
        self.assertIn("pause menu", error)

    def test_a_current_jar_with_nothing_open_still_says_so(self):
        error = self.close(read(**CURRENT))
        self.assertIn("No screen is open", error)
        self.assertNotIn("older", error)


class CraftItemTests(unittest.TestCase):

    def check(self, state):
        skill = skills.create("craft_item", item="oak_planks")
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertTrue(skill.failed)
        return skill.done_reason

    def test_the_old_jar_is_named_with_the_field_it_lacks(self):
        reason = self.check(read(**OLD_JAR))
        self.assertIn("game_mode", reason)
        self.assertIn("older", reason)
        self.assertIn("install_mod.bat", reason)

    def test_a_current_jar_missing_game_mode_this_once_is_not_called_old(self):
        """A jar that lists the screens but sent no game_mode in this
        reading is not an old jar, and is not told to reinstall."""
        reason = self.check(read(features=mod_bridge.REQUIRED_FEATURES))
        self.assertIn("game_mode", reason)
        self.assertNotIn("older", reason)
        self.assertNotIn("install_mod.bat", reason)

    def test_no_bridge_at_all_names_the_field_and_the_mod(self):
        state = WorldState(position=(0.5, 64.0, 0.5), source="debug_overlay",
                           confidence=EXACT)
        reason = self.check(state)
        self.assertIn("game_mode", reason)
        self.assertIn("bridge mod", reason)
        self.assertNotIn("older", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
