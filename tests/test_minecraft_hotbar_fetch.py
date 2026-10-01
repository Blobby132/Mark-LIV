"""
B3g: moving food or a tool from the main inventory into the hotbar.

eat_food and the mining tasks stopped at the hotbar: food or the right
pickaxe in the main inventory was named, and the user was asked to move it.
They now move it themselves through the inventory screen -- open it, bring
the pointer onto the stack, press the hotbar number key (a swap with that
hotbar slot), close -- under the same click gate and abort rules as
craft_item. Run end to end in the simulated screen (gui_world.py).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import mining as mining_mod                          # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft import verification as verify_mod                    # noqa: E402
from minecraft.state import (                                       # noqa: E402
    EXACT, ItemStack, NearbyBlock, WorldState)
from minecraft.task_runner import (                                 # noqa: E402
    COMPLETED, STOPPED, Step, TaskRunner)
from tests.gui_world import GuiWorld, zombie                        # noqa: E402


def run(world, skill, max_steps=45):
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    runner = TaskRunner(world, world, sleeper=lambda _s: None)
    return runner.run(skill, max_steps=max_steps)


def eat(world, **options):
    skill = skills.create("eat_food", **options)
    return skill, run(world, skill)


class Quarry(GuiWorld):
    """GuiWorld with one block beside the player that breaks as the game
    breaks it: only with a hold as long as the HELD item needs, and dropping
    its item only when that item harvests it."""

    DROPS = {"stone": "cobblestone", "oak_log": "oak_log"}

    def __init__(self, items, block="stone", at=(1, 64, 0), **kwargs):
        super().__init__(items, blocks=(NearbyBlock(*at, block, True),),
                         yaw=-90.0, **kwargs)
        self.mined = []

    def mine(self, params):
        self.swings += 1
        hit = self.crosshair()
        if hit is None or hit[0] not in self.blocks:
            self.wrong_block_swings += 1
            return self._done("mine", params)
        held = self.stacks.get(self.selected)
        estimate = mining_mod.estimate_break_duration(
            hit[1], held_item=held[0] if held else "", on_ground=True)
        if float(params.get("duration", 0)) >= estimate.seconds:
            del self.blocks[hit[0]]
            self.mined.append((hit[1], held[0] if held else None))
            if estimate.drops:
                self._add_to_inventory(self.DROPS[hit[1]], 1)
        return self._done("mine", params)


def mine(world, x=1, y=64, z=0):
    skill = skills.create("mine_block", x=x, y=y, z=z)
    return skill, run(world, skill)


class EatFromTheMainInventoryTests(unittest.TestCase):

    def test_food_in_the_main_inventory_is_fetched_and_eaten(self):
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=10.0)
        skill, result = eat(world)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.eaten, ["bread"])
        self.assertEqual(world.swaps, 1)
        self.assertEqual(world.stacks.get(1), ["bread", 2],
                         "the bread should be in the first empty hotbar slot")
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertEqual(world.refused, [], "the gate refused a press")
        self.assertEqual(world.selected, 0, "the held slot was not put back")
        self.assertIn("hotbar slot 2", skill.done_reason)

    def test_food_already_on_the_hotbar_needs_no_trip(self):
        world = GuiWorld({3: ("apple", 1), 20: ("bread", 3)}, hunger=15.0)
        skill, _ = eat(world)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.eaten, ["apple"])
        self.assertEqual(world.swaps, 0)

    def test_a_full_hotbar_gives_up_a_block_not_a_tool(self):
        world = GuiWorld({0: ("wooden_pickaxe", 1), 1: ("stone_axe", 1),
                          2: ("cobblestone", 20), 3: ("wooden_shovel", 1),
                          4: ("dirt", 5), 5: ("torch", 4),
                          6: ("stone_pickaxe", 1), 7: ("oak_planks", 3),
                          8: ("stone_sword", 1), 20: ("bread", 3)},
                         hunger=10.0)
        skill, _ = eat(world)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.stacks.get(7), ["bread", 2])
        self.assertEqual(world.stacks.get(20), ["oak_planks", 3],
                         "the planks went where the bread was")
        self.assertIn("oak planks", skill.done_reason)

    def test_a_zombie_while_the_screen_is_open_closes_it(self):
        """Within the runner's own 5 blocks: the runner would stop the task
        with the screen open. The fetch closes it first."""
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=10.0)
        original = world.inventory

        def open_then_zombie(params):
            answer = original(params)
            if params.get("state") == "open":
                world.mobs = [zombie(4.0)]
            return answer
        world.inventory = open_then_zombie
        skill, result = eat(world)
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertEqual(world.eaten, [])
        self.assertEqual(world.swaps, 0)
        self.assertTrue(skill.failed)
        self.assertIn("zombie", f"{skill.done_reason} {result.reason}")

    def test_starving_damage_with_the_screen_open_is_retried_once(self):
        """At hunger 0 the game takes a point of health every four seconds.
        The screen rules close on any health lost; eating is the cure, so
        the trip is made once more."""
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=0.0,
                         health=10.0)
        original = world.gui_point
        hurt = []

        def starve_once(params):
            answer = original(params)
            if not hurt:
                hurt.append(True)
                world.health -= 1.0
            return answer
        world.gui_point = starve_once
        skill, result = eat(world)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.eaten, ["bread"])
        self.assertIsNone(world.screen)

    def test_hurt_on_every_try_gives_up_with_the_screen_closed(self):
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=0.0,
                         health=15.0)
        original = world.gui_point

        def starve(params):
            answer = original(params)
            world.health -= 1.0
            return answer
        world.gui_point = starve
        skill, _ = eat(world)
        self.assertTrue(skill.failed)
        self.assertIsNone(world.screen)
        self.assertEqual(world.swaps, 0)
        self.assertIn("health", skill.done_reason)

    def test_an_old_mod_gets_the_old_answer(self):
        """No game_mode in the reading: the jar predates screens. Nothing
        is opened; the food is named and the user asked to move it."""
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=10.0,
                         mode=None)
        skill, result = eat(world)
        self.assertTrue(skill.failed)
        self.assertIn("bread", skill.done_reason)
        self.assertIn("install_mod", skill.done_reason)
        self.assertEqual(result.steps_taken, 0)

    def test_creative_mode_is_not_clicked_in(self):
        world = GuiWorld({0: ("dirt", 10), 20: ("bread", 3)}, hunger=10.0,
                         mode="creative")
        skill, result = eat(world)
        self.assertTrue(skill.failed)
        self.assertIn("creative", skill.done_reason)
        self.assertEqual(result.steps_taken, 0)


class FetchAToolTests(unittest.TestCase):

    def test_a_pickaxe_in_the_main_inventory_is_fetched_for_stone(self):
        world = Quarry({0: ("dirt", 10), 14: ("stone_pickaxe", 1)})
        skill, result = mine(world)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.mined, [("stone", "stone_pickaxe")])
        self.assertEqual(world.swaps, 1)
        self.assertEqual(world.stacks.get(1), ["stone_pickaxe", 1])
        self.assertEqual(world.selected, 0, "the held slot was not put back")
        self.assertIsNone(world.screen)
        self.assertEqual(world.refused, [])

    def test_a_much_faster_axe_is_worth_the_trip(self):
        world = Quarry({0: ("dirt", 10), 14: ("stone_axe", 1)},
                       block="oak_log")
        skill, result = mine(world)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.mined, [("oak_log", "stone_axe")])
        self.assertEqual(world.swaps, 1)

    def test_a_small_saving_is_not(self):
        """A wooden pickaxe breaks stone in 1.15s, a stone one in 0.6s: the
        trip through the inventory costs more than the half second."""
        world = Quarry({0: ("wooden_pickaxe", 1), 14: ("stone_pickaxe", 1)})
        skill, _ = mine(world)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.mined, [("stone", "wooden_pickaxe")])
        self.assertEqual(world.swaps, 0)
        self.assertIsNone(world.screen)

    def test_a_failed_trip_refuses_and_says_why(self):
        world = Quarry({0: ("dirt", 10), 14: ("stone_pickaxe", 1)},
                       pointer_gain=lambda _p: 0.0)
        skill, _ = mine(world)
        self.assertTrue(skill.failed)
        self.assertEqual(world.swings, 0, "it swung without a pickaxe")
        self.assertIsNone(world.screen)
        self.assertIn("pickaxe", skill.done_reason)
        self.assertIn("pointer", skill.done_reason)

    def test_a_zombie_while_fetching_closes_the_screen_first(self):
        world = Quarry({0: ("dirt", 10), 14: ("stone_pickaxe", 1)})
        original = world.inventory

        def open_then_zombie(params):
            answer = original(params)
            if params.get("state") == "open":
                world.mobs = [zombie(4.0)]
            return answer
        world.inventory = open_then_zombie
        skill, result = mine(world)
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertEqual(world.swings, 0)
        self.assertTrue(skill.failed)
        self.assertIn("zombie", f"{skill.done_reason} {result.reason}")

    def test_without_screens_the_refusal_is_as_before(self):
        world = Quarry({0: ("dirt", 10), 14: ("stone_pickaxe", 1)},
                       mode=None)
        skill, _ = mine(world)
        self.assertTrue(skill.failed)
        self.assertEqual(world.swings, 0)
        self.assertIn("not in the hotbar", skill.done_reason)


def hotbar(selected, *stacks):
    inventory = tuple(ItemStack(slot=s, name=n, count=1) for s, n in stacks)
    return WorldState(inventory=inventory, selected_slot=selected,
                      source="bridge", confidence=EXACT)


class WhichHotbarSlotTests(unittest.TestCase):

    def test_an_empty_slot_first(self):
        state = hotbar(0, (0, "dirt"), (1, "stone"), (3, "torch"))
        self.assertEqual(skills.hotbar_slot_to_fill(state), 2)

    def test_then_the_last_that_holds_nothing_worth_keeping(self):
        state = hotbar(0, *[(i, n) for i, n in enumerate(
            ("dirt", "cobblestone", "iron_pickaxe", "bread", "oak_planks",
             "stone_sword", "torch", "water_bucket", "shield"))])
        self.assertEqual(skills.hotbar_slot_to_fill(state), 4)

    def test_never_the_held_slot_while_another_will_do(self):
        state = hotbar(4, *[(i, n) for i, n in enumerate(
            ("iron_pickaxe", "stone_axe", "bread", "apple", "dirt",
             "stone_sword", "bow", "shield", "water_bucket"))])
        self.assertNotEqual(skills.hotbar_slot_to_fill(state), 4)


class RunnerWatchTests(unittest.TestCase):
    """A skill may hand the danger watch to its own, stricter, check while
    a screen is open -- and must get it back afterwards. The runner reads
    the skill's flags at every step, not once at the start."""

    def test_the_flags_are_read_at_every_step(self):
        class Flip:
            name = "flip"
            goal = "turn three times"
            failed = False
            done_reason = "done"
            watch_hostiles = False

            def plan(self, state, step_index, history):
                if step_index == 2:
                    self.watch_hostiles = True
                if step_index >= 3:
                    return None
                return Step(action="look", params={"dx": 40, "dy": 0},
                            expectation=verify_mod.turned(min_degrees=0.1),
                            note="turn")
        world = GuiWorld({0: ("dirt", 1)}, mobs=(zombie(3.0),))
        result = run(world, Flip())
        self.assertEqual(result.status, STOPPED, result.reason)
        self.assertEqual(result.steps_taken, 3)

    def test_without_the_flip_it_completes(self):
        class Calm:
            name = "calm"
            goal = "turn"
            failed = False
            done_reason = "done"
            watch_hostiles = False

            def plan(self, state, step_index, history):
                if step_index >= 2:
                    return None
                return Step(action="look", params={"dx": 40, "dy": 0},
                            expectation=verify_mod.turned(min_degrees=0.1),
                            note="turn")
        world = GuiWorld({0: ("dirt", 1)}, mobs=(zombie(3.0),))
        self.assertEqual(run(world, Calm()).status, COMPLETED)


class WordingTests(unittest.TestCase):

    def test_the_tool_description_no_longer_says_it_cannot_reach(self):
        from actions import minecraft as mc_actions
        text = mc_actions.TOOL["description"]
        self.assertNotIn("cannot reach", text)
        self.assertIn("into the hotbar", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
