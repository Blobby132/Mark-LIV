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
from tests.support import dig_world as worlds
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
        # The shaft is under the stair's floor, not in the cells it passes
        # through: enclosed air there is an opening (R6) before R4 is asked.
        cells = hollow(rock(), (1, 58, 0), (1, 57, 0), (1, 56, 0))
        down = shape(FEET, "east", 1, 0, -1)
        checked, refusal = check_step(cells, FEET, down, progress())
        self.assertIsNone(checked)
        self.assertEqual(refusal.rule, "R4")
        self.assertIn("I would fall", refusal.why)
        with mock.patch.object(digging, "_r4_staircase", allow):
            checked, _ = check_step(cells, FEET, down, progress())
            self.assertIsNotNone(checked)

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

    # The player stands in cells this dig made, having come down from
    # further back: the cave touches only dug cells, so it is a new
    # opening. (Air the dig began in, or natural air the player stands in,
    # joins whatever air touches it: that is R6OpenAirTests.)
    DUG_HERE = {(0, 60, 0), (0, 61, 0)}
    CAME_FROM = (-3, 63, 0)

    def test_it_stops_at_the_opening_and_says_so(self):
        plan = plan_next(self.cave(), FEET, EAST_LEVEL,
                         progress(broken=3, cleared=self.DUG_HERE,
                                  start=self.CAME_FROM))
        self.assertEqual(plan.status, "opening")
        self.assertEqual(plan.opening["at"], (1, 61, 0))
        self.assertTrue(plan.opening["floor_solid"])
        self.assertTrue(plan.opening["walkable"])
        with mock.patch.object(digging, "_r6_opening", allow):
            self.assertEqual(plan_next(self.cave(), FEET, EAST_LEVEL,
                                       progress(broken=3)).status, "walk")

    def test_a_deep_drop_is_not_walkable(self):
        plan = plan_next(self.cave(depth=5), FEET, EAST_DOWN,
                         progress(broken=3, cleared=self.DUG_HERE,
                                  start=self.CAME_FROM))
        self.assertEqual(plan.status, "opening")
        self.assertFalse(plan.opening["walkable"])

    def test_air_joined_to_where_it_started_is_just_walked(self):
        """The cave touches the hole the player started in: it is the same
        air, not something the dig broke into."""
        for broken in (0, 3):
            with self.subTest(broken=broken):
                plan = plan_next(self.cave(), FEET, EAST_LEVEL,
                                 progress(broken=broken))
                self.assertEqual(plan.status, "walk")


def grid(world, feet):
    """The grid the mod would send with the feet at `feet`."""
    world.x, world.y, world.z = feet[0] + 0.5, float(feet[1]), feet[2] + 0.5
    return world.read().near.cells


def carry_out(world, start, goal, stairs, progress=None):
    """Dig `stairs` stairs with the planner alone, as the skill does: plan,
    break the cells, settle them, check the same stair again on the new
    reading (where the real stop came from), step. Returns the plans --
    the last one "opening" or "refused" if it stopped -- the feet, and
    the progress."""
    progress = progress or Progress(start=start)
    if progress.surface is None:
        progress.note_start(grid(world, start))
    feet, plans = tuple(start), []
    for _ in range(stairs):
        if feet == tuple(goal):
            break
        cells = grid(world, feet)
        progress.note_body(cells, feet)
        plan = plan_next(cells, feet, goal, progress)
        plans.append(plan)
        if plan.status not in ("dig", "walk"):
            break
        for cell in plan.step.clear:
            progress.swung[cell] = world.at(cell)[0]
            world.cells[cell] = AIR
        cells = grid(world, feet)
        progress.settle(cells)
        again = shape(feet, plan.step.direction, plan.step.dx, plan.step.dz,
                      plan.step.dy)
        checked, problem = check_step(cells, feet, again, progress)
        if checked == "opening":
            plans.append(digging.Plan("opening", "", opening=problem))
            break
        if checked is None:
            plans.append(digging.Plan("refused", problem.describe(),
                                      refusal=problem))
            break
        feet = plan.step.to
        progress.last_direction = plan.step.direction
    return plans, feet, progress


def stopped(plans):
    return [p for p in plans if p.status == "opening"]


class R6OpenAirTests(unittest.TestCase):
    """R6 stops at air the dig broke into -- a cave, a hollow -- and not at
    air that is already open: the space the dig began in, cells it has
    stood in, openings it was told to go through, and open sky at or above
    the starting level when the dig began under open sky -- with every
    natural air cell joined to those, through cells the dig did not break,
    inside the grid. From a real run: the first grass block broken, then
    "the tunnel opened into a cave" one cell over it, in the sky."""

    def test_the_reproduction(self):
        """Flat grass at y -1, air from 0 up, the player at (0, 0, 0),
        the goal (6, -6, 0): the first stair breaks (1, -1, 0); with it
        settled, the same stair was "a cave at (1, 1, 0)" -- the sky."""
        world = worlds.flat_grass()
        cells = grid(world, (0, 0, 0))
        done = Progress(start=(0, 0, 0))
        done.note_start(cells)
        first = plan_next(cells, (0, 0, 0), (6, -6, 0), done)
        self.assertEqual(first.status, "dig")
        self.assertEqual(first.step.clear, ((1, -1, 0),))
        cells[(1, -1, 0)] = AIR
        done.swung[(1, -1, 0)] = "grass_block"
        done.settle(cells)
        again = plan_next(cells, (0, 0, 0), (6, -6, 0), done)
        self.assertNotEqual(again.status, "opening", again.why)

    def test_a_flat_surface_descent(self):
        """No stop for the first four stairs, in or under the open air;
        then underground to the goal."""
        plans, feet, done = carry_out(worlds.flat_grass(), (0, 0, 0),
                                      (6, -6, 0), 10)
        self.assertEqual(stopped(plans), [], [p.why for p in plans])
        self.assertEqual(feet, (6, -6, 0))
        self.assertGreaterEqual(len(plans), 6)
        self.assertLess(feet[1], -2, "it never went underground")

    def test_an_enclosed_hollow_below_still_stops(self):
        world = worlds.flat_grass(hollow_at=((3, -3, 0), (3, -2, 0)))
        plans, feet, _ = carry_out(world, (0, 0, 0), (6, -6, 0), 10)
        self.assertEqual(len(stopped(plans)), 1, [p.why for p in plans])
        opening = stopped(plans)[0].opening
        self.assertEqual(opening["at"], (3, -2, 0))
        self.assertEqual(opening["below"], 3)
        self.assertEqual(feet, (2, -2, 0))

    def test_into_the_side_of_a_hill(self):
        """No stop at the hill's face -- the air in front of it is sky --
        and a stop at a hollow inside it."""
        plans, feet, _ = carry_out(worlds.hill(), (0, 0, 0), (8, 0, 0), 10)
        self.assertEqual(stopped(plans), [], [p.why for p in plans])
        self.assertEqual(feet, (8, 0, 0))
        hollow_hill = worlds.hill(hollow_at=((5, 0, 0), (5, 1, 0)))
        plans, feet, _ = carry_out(hollow_hill, (0, 0, 0), (8, 0, 0), 10)
        self.assertEqual(stopped(plans)[0].opening["at"], (5, 1, 0))
        self.assertEqual(feet, (4, 0, 0))

    def test_starting_in_a_tunnel(self):
        """Walking along the tunnel the dig began in is fine; a chamber
        behind its wall, reached by breaking the wall, is an opening."""
        plans, feet, _ = carry_out(worlds.tunnel(), (0, 60, 0), (6, 60, 0),
                                   10)
        self.assertEqual(stopped(plans), [])
        self.assertEqual(feet, (6, 60, 0))
        self.assertTrue(all(p.status == "walk" for p in plans))
        plans, feet, _ = carry_out(worlds.tunnel(chamber=True), (0, 60, 0),
                                   (3, 60, 3), 10)
        self.assertEqual(stopped(plans)[0].opening["at"], (3, 61, 2))
        self.assertEqual(feet, (3, 60, 1))

    def test_starting_inside_a_cave(self):
        """The cave the dig began in is not an opening; a separate hollow
        below it is."""
        plans, feet, _ = carry_out(worlds.in_cave(), (0, 60, 0), (6, 55, 0),
                                   12)
        self.assertEqual(stopped(plans), [], [p.why for p in plans])
        self.assertEqual(feet, (6, 55, 0))
        plans, feet, _ = carry_out(worlds.in_cave(pocket=True), (0, 60, 0),
                                   (6, 55, 0), 12)
        self.assertEqual(len(stopped(plans)), 1, [p.why for p in plans])
        self.assertIn(stopped(plans)[0].opening["at"],
                      ((5, 57, 0), (5, 56, 0)))

    def test_a_dig_that_begins_on_a_slope(self):
        """The real run's ground: grass at 63, then down to the east with
        open sky over it. Neither the first stairs nor the air over the
        slope further down is an opening."""
        start = worlds.SLOPE_START
        goal = (start[0] + 8, start[1] - 8, start[2])
        plans, feet, _ = carry_out(worlds.slope(), start, goal, 12)
        self.assertEqual(stopped(plans), [], [p.why for p in plans])
        self.assertEqual(feet, goal)

    def test_the_second_stop_in_the_log(self):
        """Asked again from (-617, 61, -124), further down the slope, with
        one block counted: it was "a cave at (-616, 62, -124)". That cell is
        below where the dig began (y 64) -- sky by the start level alone
        would still call it a cave -- but it is joined, through air no dig
        made, to the air the player stands in and to the sky over the
        start, so it is open air."""
        world = worlds.slope()
        done = Progress(start=worlds.SLOPE_START)
        done.note_start(grid(world, worlds.SLOPE_START))
        done.cleared.add((-619, 63, -124))
        done.broken = 1
        world.cells[(-619, 63, -124)] = AIR
        feet = (-617, 61, -124)
        cells = grid(world, feet)
        self.assertTrue(digging.is_air(cells[(-616, 62, -124)]))
        self.assertLess(62, worlds.SLOPE_START[1])
        plan = plan_next(cells, feet, (-612, 56, -124), done)
        self.assertNotEqual(plan.status, "opening", plan.why)
        self.assertIn(plan.status, ("dig", "walk"))

    # ── the rule's own tests ─────────────────────────────────────────────

    def caves(self):
        """The cases that must stop, each (world, start, goal, cell)."""
        return (
            (worlds.flat_grass(hollow_at=((3, -3, 0), (3, -2, 0))),
             (0, 0, 0), (6, -6, 0)),
            (worlds.hill(hollow_at=((5, 0, 0), (5, 1, 0))), (0, 0, 0),
             (8, 0, 0)),
            (worlds.tunnel(chamber=True), (0, 60, 0), (3, 60, 3)),
            (worlds.in_cave(pocket=True), (0, 60, 0), (6, 55, 0)),
        )

    def test_too_loose_and_the_cave_tests_fail(self):
        """open_air made to call every air cell open (never an opening):
        none of the real caves stops any more."""
        loose = lambda cells, progress, feet=None: frozenset(   # noqa: E731
            c for c, e in cells.items() if digging.is_air(e))
        for world, start, goal in self.caves():
            with self.subTest(start=start, goal=goal):
                self.assertTrue(stopped(carry_out(world, start, goal, 12)[0]))
        for world, start, goal in self.caves():
            with self.subTest(loose=goal), \
                    mock.patch.object(digging, "open_air", loose):
                self.assertEqual(stopped(carry_out(world, start, goal,
                                                   12)[0]), [])

    def test_too_strict_and_the_surface_tests_fail(self):
        """open_air made to know nothing is open: the flat descent stops in
        the sky, as in the real run."""
        with mock.patch.object(digging, "open_air",
                               lambda *a, **k: frozenset()):
            plans, _feet, _ = carry_out(worlds.flat_grass(), (0, 0, 0),
                                        (6, -6, 0), 10)
        self.assertEqual(stopped(plans)[0].opening["at"], (1, 1, 0))


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

    def test_an_unseen_cell_within_three_is_not_taken_for_rock(self):
        """A chunk not loaded leaves a hole in the grid: within three of a
        planned cell, R7 cannot rule out lava there."""
        cells = rock()
        del cells[(4, 60, 0)]
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R7")
        self.assertIn("cannot see (4, 60, 0)", plan.why)
        # R9 (five blocks) would refuse the same unseen cell: it is set
        # aside too, so this shows R7's own refusal.
        with mock.patch.object(digging, "_r7_lava", allow), \
                mock.patch.object(digging, "_r9_warden", allow):
            self.assertEqual(plan_next(cells, FEET, EAST_LEVEL,
                                       progress()).status, "dig")

    def test_outside_the_grid_is_outside_its_view(self):
        """The grid reaches three above the feet; R7's cube round the head
        cell reaches four. That layer is never reported, so it is not
        refused -- safe because lava there could only reach the dig
        through a cell the grid shows, and R2 has every neighbour of a
        broken cell known and dry. Pinned so a change is noticed; lava in
        the top layer the grid does show is refused."""
        cells = {c: e for c, e in rock().items() if c[1] <= FEET[1] + 3}
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.status, "dig")
        cells[(1, 63, 0)] = LAVA                     # the top seen layer
        plan = plan_next(cells, FEET, EAST_LEVEL, progress())
        self.assertEqual(plan.refusal.rule, "R7")
        self.assertIn("lava at (1, 63, 0)", plan.why)


FLOOR_HAZARDS = ("cactus", "campfire", "soul_campfire", "magma_block",
                 "powder_snow", "sweet_berry_bush", "wither_rose", "cobweb",
                 "fire", "soul_fire")
"""The names the brief listed for the floor check. Each is in the one shared
HAZARDS (minecraft/blocks.py), which the planner and navigation both use."""

STAIR_FLOOR = (1, 58, 0)        # the floor of the first stair down, east


class FloorHazardTests(unittest.TestCase):
    """R4: what a step lands on, and every cell the body goes through, is no
    hazard -- solid and dry is not enough (magma burns, a campfire burns,
    powder snow swallows) -- and no cactus is beside the tunnel."""

    def plan(self, cells):
        return plan_next(cells, FEET, EAST_DOWN, progress())

    def test_one_definition(self):
        from minecraft import blocks, navigation
        self.assertIs(navigation.HAZARDS, blocks.HAZARDS)
        self.assertIs(digging.HAZARDS, blocks.HAZARDS)
        for name in FLOOR_HAZARDS:
            self.assertIn(name, blocks.HAZARDS)

    def test_blocks_is_constants_only(self):
        import ast
        import inspect
        from minecraft import blocks
        tree = ast.parse(inspect.getsource(blocks))
        for node in tree.body:
            with self.subTest(line=node.lineno):
                self.assertTrue(
                    isinstance(node, (ast.Assign, ast.Expr))
                    or (isinstance(node, ast.ImportFrom)
                        and node.module == "__future__"),
                    "minecraft/blocks.py holds names, nothing else")

    def test_each_hazard_as_the_floor(self):
        for name in FLOOR_HAZARDS:
            with self.subTest(floor=name):
                # Marked solid: the name alone must refuse it.
                cells = put(rock(), STAIR_FLOOR, block(name, True))
                plan = self.plan(cells)
                self.assertEqual(plan.status, "refused", plan.why)
                self.assertEqual(plan.refusal.rule, "R4")
                self.assertIn(" ".join(name.split("_")), plan.why)

    def test_each_hazard_in_the_body_s_way(self):
        """A hazard in a cell the body would pass through is refused as a
        hazard -- not as "not natural stone", which would hide why."""
        for name in FLOOR_HAZARDS:
            with self.subTest(cell=name):
                cells = put(rock(), (1, 60, 0), block(name, False))
                plan = plan_next(cells, FEET, EAST_LEVEL, progress())
                self.assertEqual(plan.status, "refused", plan.why)
                self.assertEqual(plan.refusal.rule, "R4")
                self.assertIn("hazard", plan.why)

    def test_a_cactus_beside_the_tunnel(self):
        """The level stair east would pass beside it: refused. The planner
        may find another stair, but none whose body brushes the cactus."""
        level = shape(FEET, "east", 1, 0, 0)
        for side in ((1, 60, 1), (1, 61, -1), (1, 62, 0)):
            with self.subTest(cactus=side):
                cells = put(rock(), side, block("cactus"))
                checked, refusal = check_step(cells, FEET, level, progress())
                self.assertIsNone(checked)
                self.assertEqual(refusal.rule, "R4")
                self.assertIn("cactus", refusal.why)
                plan = plan_next(cells, FEET, EAST_LEVEL, progress())
                if plan.step is not None:
                    for cell in plan.step.passes:
                        self.assertNotEqual(
                            sum(abs(a - b) for a, b in zip(cell, side)), 1,
                            f"{plan.why} brushes the cactus")

    def test_a_cactus_further_off_is_fine(self):
        cells = put(rock(), (1, 60, 2), block("cactus"))
        self.assertEqual(plan_next(cells, FEET, EAST_LEVEL,
                                   progress()).status, "dig")

    def test_plain_rock_is_fine(self):
        self.assertEqual(self.plan(rock()).status, "dig")

    def test_without_the_rule(self):
        """With only the hazard check removed, a magma floor and a cactus
        beside the tunnel are dug to: the refusals are its doing."""
        magma = put(rock(), STAIR_FLOOR, block("magma_block"))
        cactus = put(rock(), (1, 60, 1), block("cactus"))
        level = shape(FEET, "east", 1, 0, 0)
        with mock.patch.object(digging, "_r4_hazards", allow):
            self.assertEqual(self.plan(magma).status, "dig")
            checked, _ = check_step(cactus, FEET, level, progress())
            self.assertIsNotNone(checked)

    def test_walking_onto_one(self):
        """check_walk -- a step into open air, to pick up a drop -- has the
        same floor rule."""
        cells = put(hollow(rock(), (1, 60, 0), (1, 61, 0)), (1, 59, 0),
                    block("magma_block"))
        refusal = digging.check_walk(cells, FEET,
                                     shape(FEET, "east", 1, 0, 0))
        self.assertEqual(refusal.rule, "R4")
        self.assertIn("magma block", refusal.why)
        with mock.patch.object(digging, "_r4_hazards", allow):
            self.assertIsNone(digging.check_walk(
                cells, FEET, shape(FEET, "east", 1, 0, 0)))


R9_NAMES = ("sculk", "sculk_sensor", "calibrated_sculk_sensor",
            "sculk_shrieker", "sculk_catalyst", "reinforced_deepslate",
            "spawner")


class R9WardenTests(unittest.TestCase):
    """R9: no step whose cells come within five blocks of sculk, reinforced
    deepslate or a spawner -- an ancient city's warden, or a monster
    room. Within five is per axis, like R7: a block four across, two up
    and five along is within five although six and more away in a line."""

    level = shape(FEET, "east", 1, 0, 0)       # passes (1, 61, 0), (1, 60, 0)

    def check(self, cells):
        return check_step(cells, FEET, self.level, progress())

    def test_each_name_five_away(self):
        for name in R9_NAMES:
            with self.subTest(name=name):
                checked, refusal = self.check(put(rock(), (6, 60, 0),
                                                  block(name)))
                self.assertIsNone(checked)
                self.assertEqual(refusal.rule, "R9")
                self.assertIn(" ".join(name.split("_")), refusal.why)
                self.assertIn("this looks like an ancient city (warden) or "
                              "a monster room", refusal.why)

    def test_six_away_is_fine(self):
        checked, _ = self.check(put(rock(), (7, 60, 0), block("spawner")))
        self.assertIsNotNone(checked)

    def test_diagonally_within_five(self):
        """(5, 63, 5) is 4, 2 and 5 from the head cell (1, 61, 0) -- about
        6.7 in a straight line, but within five on every axis."""
        cells = put(rock(), (5, 63, 5), block("sculk_shrieker"))
        checked, refusal = self.check(cells)
        self.assertIsNone(checked)
        self.assertEqual(refusal.rule, "R9")
        plan = plan_next(cells, FEET, (5, 60, 0), progress())
        self.assertNotEqual(plan.status, "dig", plan.why)

    def test_the_floor_counts(self):
        """The floor of a stair down is two below the new feet: a spawner
        five below that is within five of a planned cell."""
        cells = put(rock(), (1, 53, 0), block("spawner"))
        checked, refusal = check_step(cells, FEET,
                                      shape(FEET, "east", 1, 0, -1),
                                      progress())
        self.assertIsNone(checked)
        self.assertEqual(refusal.rule, "R9")

    def test_an_unseen_cell_within_five_inside_the_grid(self):
        cells = rock()
        del cells[(5, 58, 2)]
        checked, refusal = self.check(cells)
        self.assertIsNone(checked)
        self.assertEqual(refusal.rule, "R9")
        self.assertIn("cannot see (5, 58, 2)", refusal.why)

    def test_outside_the_grid_is_outside_its_view(self):
        """Like R7: only cells inside the grid's box are looked at. The grid
        reaches three above the feet; a spawner five above is not seen."""
        cells = {c: e for c, e in rock().items() if c[1] <= FEET[1] + 3}
        checked, _ = self.check(cells)
        self.assertIsNotNone(checked)

    def test_a_vein_s_ore_and_a_walk_too(self):
        cells = put(put(rock(), (1, 60, 0), block("iron_ore")),
                    (5, 62, 3), block("spawner"))
        refusal = digging.check_break(cells, FEET, (1, 60, 0), progress())
        self.assertEqual(refusal.rule, "R9")
        open_ = put(hollow(rock(), (1, 60, 0), (1, 61, 0)), (4, 64, 3),
                    block("sculk"))
        refusal = digging.check_walk(open_, FEET, self.level)
        self.assertEqual(refusal.rule, "R9")

    def test_without_the_rule(self):
        cells = put(rock(), (5, 63, 5), block("sculk_shrieker"))
        with mock.patch.object(digging, "_r9_warden", allow):
            checked, _ = self.check(cells)
            self.assertIsNotNone(checked)
            self.assertEqual(plan_next(cells, FEET, (5, 60, 0),
                                       progress()).status, "dig")


class GoalLimitTests(unittest.TestCase):
    """R5 before anything is broken: a goal deeper than MAX_DEPTH, further
    than MAX_HORIZONTAL, or needing more than the blocks a task has left is
    refused, saying the limit and the furthest cell toward the goal that
    every limit allows (along the straight line from the feet)."""

    def refuse(self, goal, feet=FEET, broken=0, start=FEET):
        return digging.goal_refusal(start, feet, goal, broken)

    def test_within_every_limit(self):
        for goal in ((5, 55, 0), (8, 52, 0), (19, 60, 0), (-6, 54, 4)):
            with self.subTest(goal=goal):
                self.assertIsNone(self.refuse(goal))

    def test_too_deep(self):
        refusal = self.refuse((2, 40, 1))
        self.assertEqual(refusal.rule, "R5")
        self.assertIn("20 blocks below where I started", refusal.why)
        self.assertIn("at most 16 down (to y 44)", refusal.why)
        self.assertIn("furthest I may dig toward it is (1, 47, 0)",
                      refusal.why)

    def test_too_far(self):
        refusal = self.refuse((40, 60, 0))
        self.assertIn("40 blocks across", refusal.why)
        self.assertIn("at most 32 across", refusal.why)
        self.assertIn("is (20, 60, 0)", refusal.why, "32 across is 64 "
                      "blocks: the budget brings it to 20")
        diagonal = self.refuse((30, 60, 30))
        self.assertIn("42 blocks across", diagonal.why)
        self.assertIn("is (10, 60, 10)", diagonal.why)

    def test_more_blocks_than_a_task_may_break(self):
        refusal = self.refuse((25, 60, 0))
        self.assertIn("about 50 blocks", refusal.why)
        self.assertIn("40 a task may break", refusal.why)
        self.assertIn("(20, 60, 0)", refusal.why)

    def test_what_is_broken_already_counts(self):
        self.assertIsNone(self.refuse((12, 60, 0), broken=16))
        refusal = self.refuse((12, 60, 0), broken=17)
        self.assertIn("23 left", refusal.why)
        self.assertIn("(11, 60, 0)", refusal.why)

    def test_the_nearest_allowed_is_allowed(self):
        for goal, broken in (((2, 40, 1), 0), ((40, 60, 0), 0),
                             ((30, 60, 30), 0), ((25, 60, 0), 0),
                             ((12, 60, 0), 17), ((20, 30, 25), 5)):
            with self.subTest(goal=goal):
                near = digging.nearest_allowed(FEET, FEET, goal, broken)
                self.assertIsNone(self.refuse(near, broken=broken))


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
        self.assertEqual(done.settle(cells), [((1, 60, 0), "stone")])
        self.assertEqual(done.settle(cells), [])
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


class R8WardenTests(unittest.TestCase):
    """R8 for R9's blocks: one comes within five of the body while
    digging -- revealed by a step, or there all along just outside what
    the grid showed -- and the dig stops and says so."""

    def test_one_appears(self):
        for name in R9_NAMES:
            with self.subTest(name=name):
                after = reading(cells=put(rock(), (4, 56, 3), block(name)))
                why = digging.abort_reason(reading(), after, progress())
                self.assertIn(" ".join(name.split("_")), why)
                self.assertIn("ancient city (warden) or a monster room", why)

    def test_six_away_does_not_stop_it(self):
        after = reading(cells=put(rock(), (6, 60, 0), block("spawner")))
        self.assertEqual(digging.abort_reason(reading(), after, progress()),
                         "")

    def test_without_the_check(self):
        after = reading(cells=put(rock(), (4, 56, 3), block("spawner")))
        with mock.patch.object(digging, "_r8_warden", lambda *a: ""):
            self.assertEqual(digging.abort_reason(reading(), after,
                                                  progress()), "")


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


class CheckWalkTests(unittest.TestCase):
    """One stair on foot into air already there -- to pick up a drop. It
    breaks nothing, so only the rules about where a foot may go apply."""

    def east(self):
        return shape(FEET, "east", 1, 0, 0)

    def test_into_an_open_pocket(self):
        cells = hollow(rock(), (1, 60, 0), (1, 61, 0))
        self.assertIsNone(digging.check_walk(cells, FEET, self.east()))

    def test_each_rule(self):
        pocket = (1, 60, 0), (1, 61, 0)
        cases = {
            "walk": rock(),
            "R2": put(hollow(rock(), (1, 61, 0)), (1, 60, 0), WATER),
            "R4": put(hollow(rock(), *pocket), (1, 59, 0), AIR),
            "R7": put(hollow(rock(), *pocket), (3, 61, 1), LAVA),
            "unknown": {c: e for c, e in hollow(rock(), *pocket).items()
                        if c != (1, 59, 0)},
        }
        for rule, cells in cases.items():
            with self.subTest(rule=rule):
                refusal = digging.check_walk(cells, FEET, self.east())
                self.assertIsNotNone(refusal)
                self.assertEqual(refusal.rule, rule)

    def test_without_each_rule(self):
        """The R2, R4 and R7 cases go ahead with that rule's function
        removed: each refusal is the rule's own."""
        pocket = (1, 60, 0), (1, 61, 0)
        cases = {
            "_r2_fluid": put(hollow(rock(), (1, 61, 0)), (1, 60, 0),
                             block("water", False, "water")),
            "_r4_staircase": put(hollow(rock(), *pocket), (1, 59, 0), AIR),
            "_r7_lava": put(hollow(rock(), *pocket), (3, 61, 1), LAVA),
        }
        for rule, cells in cases.items():
            with self.subTest(rule=rule):
                if rule == "_r2_fluid":
                    # The water cell is not air either: count it as open so
                    # only R2 stands between it and a step.
                    with mock.patch.object(digging, rule, allow), \
                            mock.patch.object(digging, "is_air",
                                              lambda e: e is not None
                                              and not e[1]):
                        self.assertIsNone(digging.check_walk(
                            cells, FEET, self.east()))
                    continue
                with mock.patch.object(digging, rule, allow):
                    self.assertIsNone(digging.check_walk(cells, FEET,
                                                         self.east()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
