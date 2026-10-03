"""
minecraft/digging.py: the staircase planner and its hard rules.

Each rule has a test that it refuses, AND a test that with that one rule's
function removed (patched to allow everything) the same plan goes ahead --
so the refusal is that rule's doing, and deleting the rule would be caught.

The cells are the bridge grid's shape; a cell left out is unknown, and
unknown is never assumed to be stone.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from minecraft import digging
from minecraft.digging import Progress, plan_next, shape, check_step
from tests.support.dig_world import (AIR, LAVA, STONE, WATER, block, hollow,
                                     put, rock)

FEET = (0, 60, 0)
EAST_LEVEL = (4, 60, 0)          # a goal straight ahead, level
EAST_DOWN = (5, 55, 0)           # a goal down a staircase


def progress(broken=0, start=FEET, cleared=()):
    return Progress(start=start, broken=broken, cleared=set(cleared))


def allow(*_args, **_kwargs):
    return None


class HappyPathTests(unittest.TestCase):

    def walk(self, cells, goal, limit=40):
        """Follow the plans, clearing what each says, as the skill will."""
        feet, done = FEET, progress()
        trail = []
        for _ in range(limit):
            plan = plan_next(cells, feet, goal, done)
            if plan.status == "done":
                return trail, feet, done
            self.assertIn(plan.status, ("dig", "walk"), plan.why)
            step = plan.step
            self.assertNotIn((feet[0], feet[1] - 1, feet[2]), step.clear,
                             "it broke the block underfoot")
            self.assertNotIn(step.floor, step.clear)
            self.assertLessEqual(len(step.clear), 3)
            self.assertEqual(abs(step.dx) + abs(step.dz), 1,
                             "not one block across")
            for cell in step.clear:
                cells[cell] = AIR
                done.cleared.add(cell)
            done.broken += len(step.clear)
            done.last_direction = step.direction
            trail.append(step)
            feet = step.to
        self.fail("it never arrived")

    def test_a_staircase_down(self):
        trail, feet, done = self.walk(rock(), EAST_DOWN)
        self.assertEqual(feet, EAST_DOWN)
        self.assertEqual(len(trail), 5)
        self.assertTrue(all(s.dy == -1 for s in trail), "not a staircase")
        self.assertEqual(done.broken, 15)

    def test_a_level_tunnel(self):
        trail, feet, done = self.walk(rock(), EAST_LEVEL)
        self.assertEqual(feet, EAST_LEVEL)
        self.assertEqual(done.broken, 8)

    def test_one_to_three_cells_a_step(self):
        plan = plan_next(rock(), FEET, EAST_DOWN, progress())
        self.assertEqual(plan.status, "dig")
        self.assertEqual(plan.cells, ((1, 61, 0), (1, 60, 0), (1, 59, 0)),
                         "head, feet and below, top first")
        self.assertIn("(1, 59, 0)", plan.why)

    def test_the_shapes_never_touch_underfoot_or_the_next_floor(self):
        for name, dx, dz in digging.DIRECTIONS:
            for dy in (-1, 0, 1):
                step = shape(FEET, name, dx, dz, dy)
                with self.subTest(direction=name, dy=dy):
                    self.assertNotIn((0, 59, 0), step.passes)
                    self.assertNotIn(step.floor, step.passes)
                    self.assertEqual(step.to[1] - FEET[1], dy)


class R1DiggableTests(unittest.TestCase):

    def cells(self):
        cells = rock()
        for y in range(59, 63):                 # a column of planks ahead
            put(cells, (1, y, 0), block("oak_planks"))
        return cells

    def test_refused(self):
        plan = plan_next(self.cells(), FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "refused")
        self.assertEqual(plan.refusal.rule, "R1")
        self.assertIn("oak planks", plan.why)

    def test_without_the_rule_it_would_dig_through_planks(self):
        with mock.patch.object(digging, "_r1_diggable", allow):
            plan = plan_next(self.cells(), FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "dig")

    def test_the_list(self):
        for name in ("stone", "deepslate", "tuff", "andesite", "diorite",
                     "granite", "dirt", "grass_block", "coarse_dirt",
                     "netherrack", "sandstone", "calcite", "iron_ore",
                     "deepslate_diamond_ore", "nether_quartz_ore"):
            with self.subTest(name=name):
                self.assertTrue(digging.is_diggable(name))
        for name in ("oak_planks", "bricks", "glass", "white_wool", "chest",
                     "red_bed", "oak_door", "rail", "torch", "oak_sign",
                     "spawner", "obsidian", "bedrock",
                     "reinforced_deepslate", "budding_amethyst",
                     "suspicious_sand", "suspicious_gravel", "tnt",
                     "cobblestone", "gravel", "sand"):
            with self.subTest(name=name):
                self.assertFalse(digging.is_diggable(name))


class R2FluidTests(unittest.TestCase):

    def test_water_beside_a_cell_to_break(self):
        cells = put(rock(), (2, 61, 0), WATER)
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R2")
        self.assertIn("(2, 61, 0)", plan.why)
        with mock.patch.object(digging, "_r2_fluid", allow):
            self.assertEqual(plan_next(cells, FEET, EAST_LEVEL,
                                       progress()).status, "dig")

    def test_lava_beside_the_lowest_cell_of_a_stair(self):
        cells = put(rock(), (1, 59, 1), LAVA)
        plan = plan_next(cells, FEET, EAST_DOWN, progress())
        self.assertEqual(plan.refusal.rule, "R2")
        self.assertIn("(1, 59, 1)", plan.why)

    def test_never_step_into_water(self):
        cells = rock()
        for y in range(60, 63):
            put(cells, (1, y, 0), WATER)
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R2")
        self.assertIn("never dig or step into", plan.why)
        with mock.patch.object(digging, "_r2_fluid", allow):
            self.assertIn(plan_next(cells, FEET, EAST_LEVEL,
                                    progress()).status, ("dig", "walk"))

    def test_a_neighbour_it_cannot_see_refuses(self):
        cells = rock()
        del cells[(1, 61, 1)]                   # beside the head cell
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R2")
        self.assertIn("cannot see", plan.why)

    def test_flowing_and_waterlogged_count(self):
        for entry in (block("water", False, "flowing_water"),
                      block("oak_stairs", True, "water"),
                      block("lava", False, "flowing_lava")):
            with self.subTest(entry=entry):
                cells = put(rock(), (2, 61, 0), entry)
                self.assertEqual(plan_next(cells, FEET, EAST_LEVEL,
                                           progress()).refusal.rule, "R2")


class R3FallingTests(unittest.TestCase):

    def cells(self, name="gravel"):
        cells = rock()
        put(cells, (1, 62, 0), block(name))     # on top of the head cell
        return cells

    def test_refused(self):
        for name in ("gravel", "sand", "red_sand", "white_concrete_powder",
                     "anvil", "dragon_egg"):
            with self.subTest(name=name):
                plan = plan_next(self.cells(name), FEET, EAST_LEVEL,
                                 progress())
                self.assertEqual(plan.refusal.rule, "R3")
                self.assertIn("would fall", plan.why)

    def test_without_the_rule_gravel_would_come_down(self):
        with mock.patch.object(digging, "_r3_falling", allow):
            plan = plan_next(self.cells(), FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "dig")

    def test_unseen_above_refuses(self):
        cells = rock()
        del cells[(1, 62, 0)]
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertIn(plan.refusal.rule, ("R2", "R3"))
        self.assertEqual(plan.status, "refused")


class R4StaircaseTests(unittest.TestCase):

    def test_a_floor_that_is_not_there(self):
        cells = hollow(rock(), (1, 59, 0), (1, 58, 0), (1, 57, 0))
        plan = plan_next(cells, FEET, (4, 57, 0), progress())
        self.assertEqual(plan.refusal.rule, "R4")
        self.assertIn("I would fall", plan.why)
        with mock.patch.object(digging, "_r4_staircase", allow):
            self.assertEqual(plan_next(cells, FEET, (4, 57, 0),
                                       progress()).status, "dig")

    def test_a_floor_it_cannot_see(self):
        """Open air ahead, so nothing is broken (R2's neighbour check would
        catch an unseen floor under a cell to break first): the floor
        itself must be seen."""
        cells = hollow(rock(), (1, 61, 0), (1, 60, 0))
        del cells[(1, 59, 0)]
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R4")
        self.assertIn("cannot see the floor", plan.why)

    def test_never_underfoot_nor_straight_down(self):
        down = digging.Step("down", 0, 0, -1, ((0, 59, 0),), (), (0, 59, 0),
                            (0, 58, 0))
        checked, refusal = check_step(rock(), FEET, down, progress())
        self.assertIsNone(checked)
        self.assertEqual(refusal.rule, "R4")
        with mock.patch.object(digging, "_r4_staircase", allow):
            checked, refusal = check_step(rock(), FEET, down, progress())
        self.assertIsNotNone(checked, "without R4, it digs straight down")


class R5LimitTests(unittest.TestCase):

    def test_blocks_broken(self):
        plan = plan_next(rock(), FEET, EAST_LEVEL, progress(broken=39))
        self.assertEqual(plan.refusal.rule, "R5")
        self.assertIn("40", plan.why)
        with mock.patch.object(digging, "_r5_limits", allow):
            self.assertEqual(plan_next(rock(), FEET, EAST_LEVEL,
                                       progress(broken=39)).status, "dig")

    def test_depth(self):
        start = (0, 76, 0)                       # 16 above, already
        plan = plan_next(rock(), FEET, (4, 56, 0), progress(start=start))
        self.assertEqual(plan.refusal.rule, "R5")
        self.assertIn("16", plan.why)

    def test_distance(self):
        start = (-32, 60, 0)
        plan = plan_next(rock(), FEET, EAST_LEVEL, progress(start=start))
        self.assertEqual(plan.refusal.rule, "R5")
        self.assertIn("32", plan.why)


class R6CaveTests(unittest.TestCase):

    def cave(self, depth=0):
        cells = hollow(rock(), (1, 61, 0), (1, 60, 0), (2, 61, 0),
                       (2, 60, 0))
        for n in range(depth):
            hollow(cells, (1, 59 - n, 0))
        return cells

    def test_it_stops_at_the_opening_and_says_so(self):
        plan = plan_next(self.cave(), FEET, EAST_LEVEL,
                         progress(broken=3, cleared={(0, 60, 0)}))
        self.assertEqual(plan.status, "opening")
        self.assertEqual(plan.opening["at"], (1, 61, 0))
        self.assertTrue(plan.opening["floor_solid"])
        self.assertTrue(plan.opening["walkable"])
        with mock.patch.object(digging, "_r6_opening", allow):
            self.assertEqual(plan_next(self.cave(), FEET, EAST_LEVEL,
                                       progress(broken=3)).status, "walk")

    def test_a_deep_drop_is_not_walkable(self):
        plan = plan_next(self.cave(depth=5), FEET, EAST_DOWN,
                         progress(broken=3))
        self.assertEqual(plan.status, "opening")
        self.assertFalse(plan.opening["walkable"])

    def test_before_any_digging_open_air_is_just_walked(self):
        plan = plan_next(self.cave(), FEET, EAST_LEVEL, progress(broken=0))
        self.assertEqual(plan.status, "walk")


class R7LavaTests(unittest.TestCase):

    def cells(self):
        return put(rock(), (4, 60, 0), LAVA)    # three from (1, 60, 0)

    def test_refused(self):
        plan = plan_next(self.cells(), FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R7")
        self.assertIn("within 3", plan.why)
        with mock.patch.object(digging, "_r7_lava", allow):
            self.assertEqual(plan_next(self.cells(), FEET, EAST_LEVEL,
                                       progress()).status, "dig")

    def test_four_away_is_fine(self):
        cells = put(rock(), (5, 60, 0), LAVA)
        plan = plan_next(cells, FEET, (2, 60, 0), progress())
        self.assertEqual(plan.status, "dig")


def reading(health=20.0, position=(0.5, 60.0, 0.5), cells=None):
    near = SimpleNamespace(cells=cells if cells is not None else rock())
    return SimpleNamespace(health=health, position=position, near=near)


class R8AbortTests(unittest.TestCase):

    def test_health_lost(self):
        why = digging.abort_reason(reading(20), reading(18), progress())
        self.assertIn("lost health", why)
        with mock.patch.object(digging, "_r8_health",
                               lambda *a: ""):
            self.assertEqual(digging.abort_reason(
                reading(20), reading(18), progress()), "")

    def test_fluid_appeared_within_two(self):
        cells = put(rock(), (2, 61, 0), block("water", False,
                                              "flowing_water"))
        why = digging.abort_reason(reading(), reading(cells=cells),
                                   progress())
        self.assertIn("water or lava appeared", why)
        with mock.patch.object(digging, "_r8_fluid", lambda *a: ""):
            self.assertEqual(digging.abort_reason(
                reading(), reading(cells=cells), progress()), "")

    def test_a_cleared_cell_filled_again(self):
        cells = put(rock(), (1, 60, 0), block("gravel"))
        done = progress(cleared={(1, 60, 0)})
        why = digging.abort_reason(reading(), reading(cells=cells), done)
        self.assertIn("cave-in", why)
        with mock.patch.object(digging, "_r8_cave_in", lambda *a: ""):
            self.assertEqual(digging.abort_reason(
                reading(), reading(cells=cells), done), "")

    def test_something_fell_into_a_cell_just_swung_at(self):
        """The swing broke the stone, gravel fell into the gap, and the
        reading after shows gravel where stone was: a cave-in, even though
        the cell was never seen as air."""
        cells = put(rock(), (1, 60, 0), block("gravel"))
        done = progress()
        done.swung[(1, 60, 0)] = "stone"
        why = digging.abort_reason(reading(), reading(cells=cells), done)
        self.assertIn("fell into", why)

    def test_a_swing_seen_to_clear_is_counted_once(self):
        cells = hollow(rock(), (1, 60, 0))
        done = progress()
        done.swung[(1, 60, 0)] = "stone"
        self.assertEqual(done.settle(cells), 1)
        self.assertEqual(done.settle(cells), 0)
        self.assertEqual((done.broken, done.cleared), (1, {(1, 60, 0)}))

    def test_a_fall(self):
        why = digging.abort_reason(reading(), reading(position=(1.5, 57.0,
                                                                0.5)),
                                   progress(), expected_feet=(1, 59, 0))
        self.assertIn("fallen", why)
        with mock.patch.object(digging, "_r8_position", lambda *a: ""):
            self.assertEqual(digging.abort_reason(
                reading(), reading(position=(1.5, 57.0, 0.5)), progress(),
                expected_feet=(1, 59, 0)), "")

    def test_all_well(self):
        self.assertEqual(digging.abort_reason(
            reading(), reading(position=(1.5, 59.0, 0.5)), progress(),
            expected_feet=(1, 59, 0)), "")


class UnknownCellTests(unittest.TestCase):

    def test_a_cell_to_break_that_was_not_reported(self):
        cells = rock()
        del cells[(1, 60, 0)]
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "refused")

    def test_no_grid_at_all(self):
        plan = plan_next(None, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "refused")
        self.assertIn("older mod", plan.why)


class CheckBreakTests(unittest.TestCase):
    """One cell -- the next ore of a vein -- under the same rules."""

    def test_an_ore_beside_the_tunnel(self):
        cells = put(rock(), (1, 60, 0), block("iron_ore"))
        self.assertIsNone(digging.check_break(cells, FEET, (1, 60, 0),
                                              progress()))

    def test_each_rule(self):
        cases = {
            "R4": (rock(), (0, 59, 0)),
            "R1": (put(rock(), (1, 60, 0), block("chest")), (1, 60, 0)),
            "R2": (put(put(rock(), (1, 60, 0), block("iron_ore")),
                       (2, 60, 0), WATER), (1, 60, 0)),
            "R3": (put(put(rock(), (1, 60, 0), block("iron_ore")),
                       (1, 61, 0), block("gravel")), (1, 60, 0)),
            "R7": (put(put(rock(), (1, 60, 0), block("iron_ore")),
                       (4, 60, 0), LAVA), (1, 60, 0)),
        }
        for rule, (cells, cell) in cases.items():
            with self.subTest(rule=rule):
                refusal = digging.check_break(cells, FEET, cell, progress())
                self.assertEqual(refusal.rule, rule)
        refusal = digging.check_break(rock(), FEET, (1, 60, 0),
                                      progress(broken=40))
        self.assertEqual(refusal.rule, "R5")

    def test_stone_is_known(self):
        self.assertEqual(STONE, ("stone", True, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
