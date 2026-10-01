"""
B1: the right tool before mining.

Nothing chose a tool: hotbar_select appeared only in EatFood and PlaceBlock,
so a log was chopped with whatever happened to be held, and stone was held
at for ten seconds by hand -- breaking it eventually, dropping nothing.

mining.best_hotbar_tool(state, block) picks, from the HOTBAR, the item that
harvests the block (it drops) and breaks it fastest, keeping what is held on
a tie. The mining skills -- collect_logs (logs and the leaves in the way),
mine_block and break_block -- select it first, verify the slot, and put the
previous slot back when they finish, the way eat_food does. When nothing in
the hotbar can harvest the block they refuse before swinging, and say so --
and say when the tool that would is in the main inventory. (Since B3g they
fetch it from there first: tests/minecraft/test_minecraft_hotbar_fetch.py.)
"""

from __future__ import annotations

import unittest


from minecraft import mining as mining_mod                           # noqa: E402
from minecraft import skills, verification as verify_mod             # noqa: E402
from minecraft.state import (                                        # noqa: E402
    BlockRef, EXACT, ItemStack, WorldState)
from minecraft.task_runner import StepRecord                         # noqa: E402


def holding(selected, *stacks, target=None, position=(0.5, 64.0, 0.5)):
    """A state whose inventory is `stacks`: (slot, name[, count])."""
    inventory = tuple(ItemStack(slot=s[0], name=s[1],
                                count=s[2] if len(s) > 2 else 1)
                      for s in stacks)
    held = next((i for i in inventory if i.slot == selected), None)
    return WorldState(position=position, rotation=(0.0, 30.0),
                      inventory=inventory, selected_slot=selected,
                      held_item=held, on_ground=True, target_block=target,
                      source="bridge", confidence=EXACT)


class ChooserTests(unittest.TestCase):

    def test_the_fastest_tool_that_harvests(self):
        state = holding(0, (0, "dirt", 32), (1, "wooden_pickaxe"),
                        (2, "stone_pickaxe"))
        choice = mining_mod.best_hotbar_tool(state, "stone")
        self.assertEqual(choice.slot, 2)
        self.assertEqual(choice.item, "stone_pickaxe")
        self.assertTrue(choice.harvests)
        self.assertIsNone(choice.refusal)

    def test_an_axe_for_a_log(self):
        state = holding(0, (0, "dirt", 32), (3, "iron_axe"))
        self.assertEqual(mining_mod.best_hotbar_tool(state, "oak_log").slot, 3)

    def test_what_is_held_is_kept_when_it_is_as_good(self):
        state = holding(4, (2, "stone_shovel"), (4, "stone_shovel"))
        self.assertIsNone(mining_mod.best_hotbar_tool(state, "dirt").slot)

    def test_no_switch_for_a_trivial_saving(self):
        """Leaves break in a fraction of a second by hand; switching costs
        a step of its own."""
        state = holding(0, (0, "iron_axe"), (5, "iron_hoe"))
        self.assertIsNone(mining_mod.best_hotbar_tool(state,
                                                      "oak_leaves").slot)

    def test_a_log_by_hand_is_fine(self):
        state = holding(0, (0, "dirt", 32))
        choice = mining_mod.best_hotbar_tool(state, "oak_log")
        self.assertIsNone(choice.slot)
        self.assertIsNone(choice.refusal)

    def test_stone_by_hand_is_refused(self):
        state = holding(0, (0, "dirt", 32), (1, "iron_axe"))
        choice = mining_mod.best_hotbar_tool(state, "stone")
        self.assertFalse(choice.harvests)
        self.assertIn("drops nothing", choice.refusal)
        self.assertIn("pickaxe", choice.refusal)

    def test_iron_with_a_wooden_pickaxe_is_refused(self):
        state = holding(1, (1, "wooden_pickaxe"))
        refusal = mining_mod.best_hotbar_tool(state, "iron_ore").refusal
        self.assertIn("stone pickaxe or better", refusal)

    def test_a_tool_in_the_main_inventory_is_named(self):
        state = holding(0, (0, "dirt", 32), (14, "stone_pickaxe"))
        choice = mining_mod.best_hotbar_tool(state, "stone")
        self.assertEqual(choice.better_in_inventory, "stone_pickaxe")
        self.assertIn("stone pickaxe", choice.refusal)
        self.assertIn("not in the hotbar", choice.refusal)

    def test_a_faster_one_in_the_inventory_is_mentioned(self):
        state = holding(0, (0, "wooden_pickaxe"), (20, "diamond_pickaxe"))
        choice = mining_mod.best_hotbar_tool(state, "stone")
        self.assertIsNone(choice.refusal)
        self.assertEqual(choice.better_in_inventory, "diamond_pickaxe")

    def test_an_unknown_inventory_changes_nothing(self):
        state = WorldState(position=(0.5, 64.0, 0.5), selected_slot=0,
                           source="f3", confidence=EXACT)
        choice = mining_mod.best_hotbar_tool(state, "stone")
        self.assertIsNone(choice.slot)
        self.assertIsNone(choice.refusal)


def record(action, before, after, status=verify_mod.SUCCESS, params=None):
    return StepRecord(index=0, step={"action": action,
                                     "params": dict(params or {}), "note": ""},
                      delivered=True, action_result={},
                      verification={"status": status},
                      state_before=before.as_dict(),
                      state_after=after.as_dict())


STONE_AT = (1, 64, 1)
ON_STONE = BlockRef(name="stone", x=1, y=64, z=1)


class MineBlockTests(unittest.TestCase):

    def skill(self):
        return skills.create("mine_block", x=1, y=64, z=1)

    def test_it_selects_the_pickaxe_mines_and_puts_the_slot_back(self):
        skill = self.skill()
        before = holding(0, (0, "dirt", 9), (2, "stone_pickaxe"),
                         target=ON_STONE)
        step = skill.plan(before, 0, ())
        self.assertEqual((step.action, step.params), ("hotbar_select",
                                                      {"slot": 3}))
        switched = holding(2, (0, "dirt", 9), (2, "stone_pickaxe"),
                           target=ON_STONE)
        history = (record("hotbar_select", before, switched),)
        step = skill.plan(switched, 1, history)
        self.assertEqual(step.action, "mine")
        mined = holding(2, (0, "dirt", 9), (2, "stone_pickaxe"))
        history += (record("mine", switched, mined),)
        step = skill.plan(mined, 2, history)
        self.assertEqual((step.action, step.params), ("hotbar_select",
                                                      {"slot": 1}))
        self.assertIsNone(skill.plan(mined, 3, history))
        self.assertFalse(skill.failed)

    def test_stone_by_hand_is_refused_before_swinging(self):
        skill = self.skill()
        state = holding(0, (0, "dirt", 9), target=ON_STONE)
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertTrue(skill.failed)
        self.assertIn("drops nothing", skill.done_reason)

    def test_a_select_that_does_not_take_is_not_retried_forever(self):
        skill = self.skill()
        state = holding(0, (0, "dirt", 9), (2, "stone_pickaxe"),
                        target=ON_STONE)
        history = ()
        steps = []
        for index in range(8):
            step = skill.plan(state, index, history)
            if step is None:
                break
            steps.append(step.action)
            history += (record(step.action, state, state,
                               status=verify_mod.FAILED),)
        self.assertLessEqual(steps.count("hotbar_select"), 2)
        self.assertIn("slot", skill.done_reason)


class BreakBlockTests(unittest.TestCase):

    def test_it_selects_the_pickaxe_first(self):
        skill = skills.create("break_block", expected="stone")
        state = holding(0, (0, "dirt", 9), (6, "iron_pickaxe"),
                        target=ON_STONE)
        step = skill.plan(state, 0, ())
        self.assertEqual((step.action, step.params), ("hotbar_select",
                                                      {"slot": 7}))

    def test_stone_by_hand_is_refused(self):
        skill = skills.create("break_block", expected="stone")
        state = holding(0, (0, "dirt", 9), target=ON_STONE)
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertIn("drops nothing", skill.done_reason)

    def test_switch_steps_are_not_swings(self):
        skill = skills.create("break_block", expected="stone", swings=1)
        before = holding(0, (0, "dirt", 9), (6, "iron_pickaxe"),
                         target=ON_STONE)
        skill.plan(before, 0, ())
        after = holding(6, (0, "dirt", 9), (6, "iron_pickaxe"),
                        target=ON_STONE)
        step = skill.plan(after, 1, (record("hotbar_select", before, after),))
        self.assertEqual(step.action, "mine")


class CollectLogsTests(unittest.TestCase):

    def test_the_axe_is_taken_up_for_the_log(self):
        from minecraft.state import NearbyBlock
        skill = skills.create("collect_logs")
        state = holding(0, (0, "dirt", 9), (4, "stone_axe"),
                        target=BlockRef(name="oak_log", x=1, y=64, z=1))
        step = skill._mine(state, NearbyBlock(1, 64, 1, "oak_log", True))
        self.assertEqual((step.action, step.params), ("hotbar_select",
                                                      {"slot": 5}))

    def test_it_puts_the_slot_back_when_it_finishes(self):
        skill = skills.create("collect_logs")
        skill._tool_slot_before = 0
        done = holding(4, (0, "dirt", 9), (4, "stone_axe"))
        step = skill._finish(done)
        self.assertEqual((step.action, step.params), ("hotbar_select",
                                                      {"slot": 1}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
