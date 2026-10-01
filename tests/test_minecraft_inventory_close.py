"""
Item 6: `inventory close` pressed ESC with no screen open. ESC with no
screen open is not harmless, as parse_inventory's docstring said: it opens
the pause menu (and in a single-player world, pauses the game), and the
next movement keys then go nowhere.

Now close is refused, nothing pressed, unless the bridge reports a screen
open at that moment -- a screen it cannot see is not assumed. Opening is
unchanged.
"""

from __future__ import annotations

import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft import action_spec                                      # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.state import WorldState                                 # noqa: E402
from tests.support.fakes import FakeLocator, FakeProcess  # noqa: E402

UNSET = object()


class CloseTests(unittest.TestCase):

    def controller(self, screen=None, probe=UNSET):
        backend = FakeInputBackend()
        if probe is UNSET:
            state = WorldState(screen=screen, source="bridge")
            probe = lambda: state                               # noqa: E731
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0, gui_probe=probe)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        return controller, backend

    def assertRefused(self, controller, backend, *words):
        result = controller.inventory({"state": "close"})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "no_screen")
        self.assertEqual(backend.events, [], "ESC was pressed")
        for word in words:
            self.assertIn(word, result.error)
        return result

    def test_no_screen_open_presses_nothing(self):
        controller, backend = self.controller(screen=None)
        self.assertRefused(controller, backend, "No screen is open",
                           "pause menu")

    def test_without_the_bridge_it_presses_nothing(self):
        controller, backend = self.controller(probe=None)
        self.assertRefused(controller, backend, "cannot see")

    def test_a_reading_that_fails_presses_nothing(self):
        controller, backend = self.controller(probe=lambda: None)
        self.assertRefused(controller, backend, "cannot see")

        def broken():
            raise OSError("bridge gone")
        controller, backend = self.controller(probe=broken)
        self.assertRefused(controller, backend, "cannot see")

    def test_an_open_screen_is_closed_with_esc(self):
        for screen in ("inventory", "crafting_table", "chest", "pause"):
            with self.subTest(screen=screen):
                controller, backend = self.controller(screen=screen)
                result = controller.inventory({"state": "close"})
                self.assertTrue(result.ok, result.error)
                self.assertEqual(backend.downs(),
                                 [action_spec.CLOSE_KEY])

    def test_opening_is_unchanged(self):
        controller, backend = self.controller(screen=None)
        result = controller.inventory({"state": "open"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.downs(), [action_spec.INVENTORY_KEY])


class WordingTests(unittest.TestCase):

    def test_it_no_longer_says_esc_does_nothing(self):
        text = action_spec.parse_inventory.__doc__
        self.assertNotIn("does nothing when nothing is open", text)
        self.assertIn("pause menu", text)

    def test_the_model_and_the_user_are_told(self):
        from actions import minecraft as mc_actions
        self.assertIn("pause menu", mc_actions.TOOL["description"])
        readme = (ROOT / "readme.md").read_text(encoding="utf-8")
        start = readme.index("**What it deliberately cannot do.**")
        self.assertIn("Press ESC with no screen open",
                      readme[start:start + 3000])


if __name__ == "__main__":
    unittest.main(verbosity=2)
