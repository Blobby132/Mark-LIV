"""
B3c: minecraft/gui.py -- when a click inside a screen may happen, and how
the pointer is brought to a slot. Pure rules; see docs/minecraft/gui.md.
"""

from __future__ import annotations

import ast
import time
import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft import gui                                           # noqa: E402
from minecraft.state import (                                       # noqa: E402
    EXACT, EntityRef, GuiSlot, GuiView, ItemStack, WorldState)

SCALE = 3.0
OUTPUT = GuiSlot(i=0, role="craft_out", x=500.0, y=300.0)
GRID = GuiSlot(i=1, role="craft_in", x=300.0, y=300.0, item="oak_log",
               count=1)
HOTBAR = GuiSlot(i=36, role="hotbar", x=300.0, y=600.0, item="oak_log",
                 count=3)


def screen(cursor=(500.0, 300.0), kind="inventory", mode="survival",
           health=20.0, mobs=(), captured_at=None, slots=(OUTPUT, GRID, HOTBAR),
           window=(1920, 1080), carried=None):
    return WorldState(
        position=(0.0, 64.0, 0.0), health=health, nearby_entities=tuple(mobs),
        screen=kind, game_mode=mode,
        gui=GuiView(scale=SCALE, window_px=window, cursor_px=cursor),
        slots=tuple(slots), carried=carried,
        captured_at=captured_at or time.time(),
        source="bridge", confidence=EXACT)


def zombie(at):
    return EntityRef(name="zombie", distance=at, hostile=True,
                     category="hostile", position=(at, 64.0, 0.0))


class ScreenRuleTests(unittest.TestCase):

    def test_the_two_allowed_screens(self):
        for kind in ("inventory", "crafting_table"):
            self.assertIsNone(gui.screen_refusal(screen(kind=kind)))

    def test_everything_else_is_refused(self):
        for kind in ("chest", "furnace", "pause", "other", None):
            with self.subTest(kind=kind):
                self.assertTrue(gui.screen_refusal(screen(kind=kind)))

    def test_creative_and_spectator_are_refused(self):
        for mode in ("creative", "spectator", None):
            with self.subTest(mode=mode):
                self.assertTrue(gui.screen_refusal(screen(mode=mode)))

    def test_an_older_jar_is_refused(self):
        state = WorldState(position=(0, 64, 0), source="bridge",
                           confidence=EXACT)
        self.assertIn("cannot see", gui.screen_refusal(state))


class ClickGateTests(unittest.TestCase):

    def test_the_cursor_on_the_slot(self):
        self.assertIsNone(gui.click_refusal(screen(cursor=(501, 302)), 0))

    def test_on_the_border_is_refused(self):
        """The rect is shrunk by one GUI unit each side: 8 units is the
        slot's edge, so 7.5 units out is past the inset."""
        edge = 500 + 7.5 * SCALE
        self.assertIn("not over slot", gui.click_refusal(
            screen(cursor=(edge, 300)), 0))

    def test_between_slots_is_refused(self):
        """Outside every slot a click drops the carried stack."""
        refusal = gui.click_refusal(screen(cursor=(400, 300),
                                           carried=ItemStack(name="stick",
                                                             count=4)), 0)
        self.assertIn("not over slot", refusal)

    def test_a_slot_the_game_did_not_report_is_refused(self):
        self.assertIn("slot 7", gui.click_refusal(screen(), 7))

    def test_near_the_window_edge_is_refused(self):
        edge = GuiSlot(i=5, role="hotbar", x=3.0, y=300.0)
        state = screen(cursor=(3.0, 300.0), slots=(edge,))
        self.assertIn("window", gui.click_refusal(state, 5))

    def test_a_hostile_within_eight_blocks_is_refused(self):
        self.assertIn("zombie", gui.click_refusal(
            screen(mobs=(zombie(6.0),)), 0))
        self.assertIsNone(gui.click_refusal(screen(mobs=(zombie(12.0),)), 0))

    def test_lost_health_is_refused(self):
        self.assertIn("health", gui.click_refusal(screen(health=17.0), 0,
                                                  health_floor=20.0))
        self.assertIsNone(gui.click_refusal(screen(health=20.0), 0,
                                            health_floor=20.0))

    def test_a_stale_reading_is_refused(self):
        old = screen(captured_at=time.time() - 3.0)
        self.assertIn("old", gui.click_refusal(old, 0))

    def test_the_screen_rule_applies_too(self):
        self.assertTrue(gui.click_refusal(screen(kind="chest"), 0))
        self.assertTrue(gui.click_refusal(screen(mode="creative"), 0))


class PointerTests(unittest.TestCase):
    """Closed loop on the reported cursor, with the gain measured -- the OS
    applies pointer acceleration inside screens."""

    def drive(self, gain, target=GRID, start=(900.0, 700.0)):
        cursor = list(start)
        pointer = gui.Pointer()
        moves = 0
        for _ in range(gui.MAX_CORRECTIONS + 1):
            state = screen(cursor=tuple(cursor))
            step = pointer.next_move(state, target)
            if step is None:
                return moves, True
            dx, dy = step
            self.assertLessEqual(abs(dx), gui.MAX_GUI_STEP_PX)
            self.assertLessEqual(abs(dy), gui.MAX_GUI_STEP_PX)
            before = tuple(cursor)
            # Acceleration: bigger moves go proportionally further.
            accel = gain(abs(dx) + abs(dy))
            cursor[0] += dx * accel
            cursor[1] += dy * accel
            pointer.observe((dx, dy), before, tuple(cursor))
            moves += 1
        return moves, gui.inside(screen(cursor=tuple(cursor)), target)

    def test_it_converges_with_one_to_one_movement(self):
        moves, arrived = self.drive(lambda _size: 1.0)
        self.assertTrue(arrived)
        self.assertLessEqual(moves, 3)

    def test_it_converges_through_pointer_acceleration(self):
        moves, arrived = self.drive(lambda size: 1.0 + min(size, 400) / 200)
        self.assertTrue(arrived)
        self.assertLessEqual(moves, gui.MAX_CORRECTIONS)

    def test_a_pointer_that_does_not_move_gives_up(self):
        pointer = gui.Pointer()
        state = screen(cursor=(900.0, 700.0))
        for _ in range(gui.MAX_CORRECTIONS):
            step = pointer.next_move(state, GRID)
            self.assertIsNotNone(step)
            pointer.observe(step, (900.0, 700.0), (900.0, 700.0))
        self.assertTrue(pointer.gave_up)
        self.assertIsNone(pointer.next_move(state, GRID))


class PurityTests(unittest.TestCase):

    def test_it_imports_only_pure_modules(self):
        tree = ast.parse((ROOT / "minecraft" / "gui.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported,
                             {"__future__", "heapq", "math", "dataclasses"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
