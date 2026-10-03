"""
A screen decision must come from a reading taken after the last key press.

From a real run: inventory_open returned ok, and an inventory_close right
after it was REFUSED "no_screen". The bridge publishes about five times a
second, so the close was judged on a snapshot from before the inventory
opened.

Now an action that opens or closes a screen (inventory, interact with a
crafting table) waits up to about a second for a reading newer than the
key press that shows the screen it expects, and says "opened", "closed" or
"not confirmed". Every refusal that depends on the screen -- close, the
gameplay holds refused while a screen is open, clicks -- is judged on a
reading newer than the last such action, or refused for want of one.

The game here applies a key one tick after it goes down; the bridge
publishes the game as it was LAG seconds ago, stamped with that time --
read through the real bridge reader.
"""

from __future__ import annotations

import json
import threading
import time
import unittest

from minecraft import mod_bridge
from minecraft import controller as controller_mod
from minecraft.controller import MinecraftController
from minecraft.input_backend import FakeInputBackend
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from tests.support.fakes import FakeLocator, FakeProcess

LAG = 0.2            # how far behind the game the published snapshot is
TICK = 0.05          # the game applies a key on its next tick


class Game:
    """The screen, over time: E opens the inventory, ESC closes any screen,
    right-click on a crafting table opens it -- each one tick later."""

    def __init__(self, crosshair="stone", obeys=True):
        self.changes = [(0.0, None)]
        self.crosshair = crosshair
        self.obeys = obeys
        self.frozen_at = None         # the mod stops publishing from here
        self.lock = threading.Lock()

    def press(self, what):
        if not self.obeys:
            return
        now = time.time() + TICK
        with self.lock:
            current = self.changes[-1][1]
            if what == "e" and current is None:
                self.changes.append((now, "inventory"))
            elif what == "esc" and current is not None:
                self.changes.append((now, None))
            elif what == "right" and current is None \
                    and self.crosshair == "crafting_table":
                self.changes.append((now, "crafting_table"))

    def screen_at(self, when):
        with self.lock:
            return [s for t, s in self.changes if t <= when][-1]

    def payload(self):
        taken = time.time() - LAG
        if self.frozen_at is not None:
            taken = min(taken, self.frozen_at)
        data = {"schema": SCHEMA, "written_at_ms": int(taken * 1000),
                "in_game": True, "position": [0.5, 64.0, 0.5],
                "health": 20.0, "game_mode": "survival",
                "features": list(mod_bridge.REQUIRED_FEATURES),
                "target_block": {"name": f"minecraft:{self.crosshair}",
                                 "x": 1, "y": 64, "z": 0, "face": "west"}}
        screen = self.screen_at(taken)
        if screen:
            data["screen"] = {"kind": screen}
        return data


class GameBackend(FakeInputBackend):
    def __init__(self, game):
        super().__init__()
        self.game = game
        self.pressed_at = {}

    def key_down(self, key):
        super().key_down(key)
        self.pressed_at[key] = time.time()
        self.game.press(key)

    def button_down(self, button):
        super().button_down(button)
        self.game.press(button)


class FreshnessTests(unittest.TestCase):

    def setUp(self):
        self.game = Game()
        self.make()

    def make(self):
        self.backend = GameBackend(self.game)
        self.reads = []                       # (when read, captured_at)

        def gui_probe():
            text = json.dumps(self.game.payload())
            state = ModBridgeStateSource(path="(test)", reader=lambda: text,
                                         clock=time.time).read()
            self.reads.append((time.time(), state.captured_at))
            return state

        def target_probe():
            return (self.game.crosshair, 1, 64, 0, "west")

        self.controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0, gui_probe=gui_probe,
            progress_probe=target_probe, held_item_probe=lambda: "")
        self.controller.start_session(duration_s=0)
        self.addCleanup(self.controller.stop, "test")

    def esc_presses(self):
        return self.backend.downs().count("esc")

    # ── the real run ─────────────────────────────────────────────────────

    def test_closing_right_after_opening_is_not_refused(self):
        opened = self.controller.inventory({"state": "open"})
        self.assertTrue(opened.ok, opened.error)
        self.assertIn("opened", opened.describe().lower())
        closed = self.controller.inventory({"state": "close"})
        self.assertTrue(closed.ok, closed.error)
        self.assertEqual(self.esc_presses(), 1)
        self.assertIn("closed", closed.describe().lower())

    def test_mining_right_after_closing_is_not_refused_as_screen_open(self):
        self.controller.inventory({"state": "open"})
        self.controller.inventory({"state": "close"})
        mined = self.controller.mine({"duration": 0.05})
        self.assertNotEqual(mined.stopped_reason, "screen_open", mined.error)

    # ── saying what happened ─────────────────────────────────────────────

    def test_an_open_the_game_ignored_says_not_confirmed(self):
        self.game.obeys = False
        opened = self.controller.inventory({"state": "open"})
        self.assertTrue(opened.ok, "the key was pressed")
        self.assertIn("not confirmed", opened.describe().lower())

    def test_interacting_with_a_crafting_table_says_opened(self):
        self.game.crosshair = "crafting_table"
        result = self.controller.interact({})
        self.assertTrue(result.ok, result.error)
        self.assertIn("opened", result.describe().lower())
        self.assertIn("crafting table", result.describe())

    def test_interacting_with_anything_else_does_not_wait(self):
        self.game.crosshair = "oak_door"
        began = time.monotonic()
        result = self.controller.interact({})
        self.assertTrue(result.ok, result.error)
        self.assertNotIn("confirmed", result.describe().lower())
        self.assertLess(time.monotonic() - began, LAG)

    # ── refusals judged on a newer reading ───────────────────────────────

    def test_a_close_refusal_is_judged_on_a_reading_after_the_open(self):
        self.game.obeys = False
        self.controller.inventory({"state": "open"})
        closed = self.controller.inventory({"state": "close"})
        self.assertFalse(closed.ok)
        self.assertEqual(self.esc_presses(), 0)
        self.assertIn("No screen is open", closed.error)
        judged_on = self.reads[-1][1]
        self.assertGreater(judged_on, self.backend.pressed_at["e"],
                           "refused on a snapshot from before the open")

    def test_a_bridge_that_stops_publishing_refuses_for_that_reason(self):
        self.controller.inventory({"state": "open"})
        self.game.frozen_at = time.time() - LAG       # no newer snapshot
        self.controller.inventory({"state": "close"})  # judged: no reading
        mined = self.controller.mine({"duration": 0.05})
        self.assertFalse(mined.ok)
        self.assertIn("newer", mined.error)
        self.assertEqual(self.backend.button_downs(), [],
                         "it mined on a stale picture")

    def test_the_wait_is_bounded(self):
        self.game.obeys = False
        self.game.frozen_at = time.time() - LAG
        began = time.monotonic()
        self.controller.inventory({"state": "open"})
        self.assertLess(time.monotonic() - began,
                        controller_mod.SCREEN_CONFIRM_SECONDS + 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
