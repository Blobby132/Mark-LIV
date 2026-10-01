"""
B3f: craft_item -- crafting by clicking, every click checked against the
game's own report, run end to end in the simulated screen (gui_world.py).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import gui as gui_mod                                # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.task_runner import TaskRunner                        # noqa: E402
from tests.support.gui_world import GuiWorld, zombie  # noqa: E402


def craft(world, item, count=1, max_steps=200):
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    skill = skills.create("craft_item", item=item, count=count)
    runner = TaskRunner(world, world, sleeper=lambda _s: None)
    result = runner.run(skill, max_steps=max_steps)
    return skill, result


class TwoByTwoTests(unittest.TestCase):

    def test_planks_from_a_log(self):
        world = GuiWorld({0: ("oak_log", 3), 1: ("dirt", 10)})
        skill, result = craft(world, "oak_planks", count=4)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("oak_planks"), 4)
        self.assertEqual(world.count("oak_log"), 2)
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertIsNone(world.carried)
        self.assertEqual(world.refused, [], "the gate refused a click")
        self.assertIn("counted in the inventory", skill.done_reason)
        self.assertIn("oak_planks", skill.done_reason)

    def test_sticks_and_a_crafting_table(self):
        world = GuiWorld({0: ("birch_planks", 10)})
        skill, _ = craft(world, "stick", count=4)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("stick"), 4)
        skill, _ = craft(world, "crafting_table")
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("crafting_table"), 1)
        self.assertEqual(world.count("birch_planks"), 4)

    def test_more_than_one_batch(self):
        world = GuiWorld({3: ("spruce_log", 5)})
        skill, _ = craft(world, "spruce_planks", count=10)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("spruce_planks"), 12)
        self.assertEqual(world.count("spruce_log"), 2)

    def test_from_a_stack_in_the_main_inventory(self):
        world = GuiWorld({20: ("oak_log", 1)})
        skill, _ = craft(world, "oak_planks", count=4)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("oak_planks"), 4)


class ThreeByThreeTests(unittest.TestCase):

    def test_a_pickaxe_at_a_crafting_table(self):
        world = GuiWorld({0: ("oak_planks", 5), 1: ("stick", 4)},
                         table=(1, 64, 0), yaw=-90.0)
        skill, result = craft(world, "wooden_pickaxe")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.count("wooden_pickaxe"), 1)
        self.assertEqual(world.count("oak_planks"), 2)
        self.assertEqual(world.count("stick"), 2)
        self.assertIsNone(world.screen)

    def test_a_table_in_the_inventory_is_placed_and_used(self):
        """B3f with B4a: no table near, one held -- placed beside the
        player, proven like any placement, then opened."""
        from tests.support.build_world import BuildWorld
        world = BuildWorld({0: ("oak_planks", 5), 1: ("stick", 4),
                            2: ("crafting_table", 1)})
        skill, result = craft(world, "wooden_pickaxe")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual([n for _c, n in world.placed], ["crafting_table"])
        self.assertEqual(world.count("wooden_pickaxe"), 1)
        self.assertEqual(world.count("crafting_table"), 0)
        self.assertIsNone(world.screen)
        self.assertIn("placed the crafting table", skill.done_reason)

    def test_no_table_in_reach_is_refused_before_anything(self):
        world = GuiWorld({0: ("oak_planks", 5), 1: ("stick", 4)})
        skill, result = craft(world, "wooden_pickaxe")
        self.assertTrue(skill.failed)
        self.assertIn("crafting table", skill.done_reason)
        self.assertEqual(result.steps_taken, 0)


class RefusalTests(unittest.TestCase):

    def test_missing_ingredients_are_named(self):
        world = GuiWorld({0: ("oak_planks", 1)})
        skill, result = craft(world, "stick", count=4)
        self.assertTrue(skill.failed)
        self.assertIn("planks", skill.done_reason)
        self.assertEqual(result.steps_taken, 0)

    def test_an_unknown_item(self):
        world = GuiWorld({0: ("oak_planks", 1)})
        skill, _ = craft(world, "diamond_pickaxe")
        self.assertTrue(skill.failed)
        self.assertIn("wooden_pickaxe", skill.done_reason)

    def test_creative_mode_is_refused(self):
        world = GuiWorld({0: ("oak_log", 1)}, mode="creative")
        skill, result = craft(world, "oak_planks")
        self.assertTrue(skill.failed)
        self.assertIn("creative", skill.done_reason)
        self.assertEqual(world.clicks, 0)

    def test_a_hostile_close_by_is_refused_before_opening(self):
        world = GuiWorld({0: ("oak_log", 1)}, mobs=(zombie(6.0),))
        skill, result = craft(world, "oak_planks")
        self.assertTrue(skill.failed)
        self.assertIn("zombie", skill.done_reason)
        self.assertIsNone(world.screen)


class AbortTests(unittest.TestCase):

    def test_a_zombie_arriving_mid_craft_closes_the_screen(self):
        world = GuiWorld({0: ("oak_log", 3)})
        original = world.gui_click

        def click_then_zombie(params):
            result = original(params)
            if world.clicks == 2:
                world.mobs = [zombie(5.0)]
            return result
        world.gui_click = click_then_zombie
        skill, _ = craft(world, "oak_planks")
        self.assertTrue(skill.failed)
        self.assertIn("zombie", skill.done_reason)
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertIsNone(world.carried)
        self.assertEqual(world.count("oak_log") + world.count("oak_planks") // 4,
                         3, "an item went missing")

    def test_a_click_that_does_not_take_stops_it(self):
        """The game reports the second click's result: nothing happened (a
        lagging server, a dropped packet). Carrying on would put the next
        click's ingredients in the wrong place."""
        world = GuiWorld({0: ("oak_log", 3)})
        original = world.gui_click

        def lossy(params):
            if world.clicks == 1 and params.get("button") == "right":
                world.clicks += 1
                return world._done("gui_click", params)       # no effect
            return original(params)
        world.gui_click = lossy
        skill, _ = craft(world, "oak_planks")
        self.assertTrue(skill.failed)
        self.assertIn("did not do what it should", skill.done_reason)
        self.assertIsNone(world.screen)
        self.assertIsNone(world.carried)
        self.assertEqual(world.count("oak_log"), 3, "a log went missing")

    def test_a_pointer_that_will_not_settle_closes_and_says_so(self):
        world = GuiWorld({0: ("oak_log", 1)}, pointer_gain=lambda _p: 0.0)
        skill, _ = craft(world, "oak_planks")
        self.assertTrue(skill.failed)
        self.assertIn("pointer", skill.done_reason)
        self.assertIsNone(world.screen)
        self.assertEqual(world.clicks, 0)

    def test_the_click_cap(self):
        self.assertLessEqual(gui_mod.MAX_GUI_CLICKS, 64)


class RegistryTests(unittest.TestCase):

    def test_it_is_a_task_and_not_impossible_any_more(self):
        self.assertIn("craft_item", skills.available())
        self.assertNotIn("craft_item", skills.NOT_YET_POSSIBLE)
        from actions import minecraft as mc_actions
        self.assertIn("craft_item", mc_actions.TOOL["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
