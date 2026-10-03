"""
Every hard rule of digging, end to end, in the named simulated worlds of
tests/support/dig_world.py: the skill, the runner and DigWorld together.

R1-R7: each world is run with the rule, and again with only that rule's
function removed. With it, the danger the world holds never happens;
without it, it does. So the world really reaches the rule, and the rule is
what kept the dig safe -- deleting it would be caught here as well as in
the planner's own tests (test_minecraft_digging.py).

R8: each abort, from a world that changes while the dig goes on -- health
lost, water appearing, a cave-in, a hostile mob, a fall, a pickaxe that
cannot harvest, the session ending -- and for the two only the skill
watches (fluid, cave-in), the same world without the check digs on.

Nothing here has run in the game: the worlds' gravity, flowing water,
falling gravel, line of sight and pickup range are DigWorld's.
"""

from __future__ import annotations

import unittest
from unittest import mock

from minecraft import aiming as aiming_mod
from minecraft import digging
from minecraft import navigation as nav
from minecraft import skills
from minecraft.skills import dig as dig_mod
from minecraft.task_runner import TaskRunner
from tests.support import dig_world as worlds
from tests.support.dig_world import AIR, block


def run(w, task, runs=4, max_steps=45, **options):
    """Run `task` until it ends or no longer says to ask again; the last
    skill and the runner's result."""
    skill = result = None
    for _ in range(runs):
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create(task, **options)
        result = TaskRunner(w, w, sleeper=lambda _s: None).run(
            skill, max_steps=max_steps)
        if "ask again" not in skill.done_reason:
            break
    return skill, result


def said(skill, result) -> str:
    return f"{result.reason} {skill.done_reason}"


def allow(*_args, **_kwargs):
    return None


def broken(w) -> list:
    return [cell for cell, _feet in w.broken]


def within(cell, other, reach) -> bool:
    return max(abs(a - b) for a, b in zip(cell, other)) <= reach


def beside(cell, other) -> bool:
    return sum(abs(a - b) for a, b in zip(cell, other)) == 1


class _Worlds(unittest.TestCase):

    def setUp(self):
        dig_mod.forget_digs()
        self.addCleanup(dig_mod.forget_digs)

    def with_and_without(self, rule_fn, build, task, harm, **options):
        """(skill, result, world) with the rule; the world without it.
        Asserts the harm only without."""
        w = build()
        skill, result = run(w, task, **options)
        self.assertFalse(harm(w), f"with {rule_fn}: {said(skill, result)}")
        dig_mod.forget_digs()
        bare = build()
        with mock.patch.object(digging, rule_fn, allow):
            run(bare, task, **options)
        self.assertTrue(harm(bare), f"without {rule_fn} the world did not "
                        f"reach the danger, so it does not test the rule")
        return skill, result, w


class HardRuleTests(_Worlds):

    def test_r1_a_mineshaft_is_not_dug_through(self):
        planks = {(2, 60, 0), (2, 61, 0)}
        skill, result, w = self.with_and_without(
            "_r1_diggable", worlds.mineshaft, "dig_to",
            lambda w: bool(planks & set(broken(w))), x=5, y=60, z=0)
        self.assertIn("R1", said(skill, result))
        self.assertIn("oak planks", said(skill, result))

    def test_r2_nothing_beside_water_is_broken(self):
        """The first stair's head cell has water on it. Only R2 stands
        there: R8's watch compares two readings, and there is only one yet.
        (After the first step R8's watch -- fluid within two cells of the
        body -- is wider than "beside a cell to break", so it stops a dig
        before R2 is reached; both are tested on their own in
        test_minecraft_digging.py.)"""
        water = [c for c, e in worlds.water_pocket().cells.items()
                 if e[2] == "water"]

        def harm(w):
            return any(beside(c, wet) for c in broken(w) for wet in water) \
                or bool(w.entered_fluid)
        skill, result, w = self.with_and_without(
            "_r2_fluid", worlds.water_pocket, "dig_to", harm, x=5, y=60, z=0)
        self.assertTrue(skill.failed)
        self.assertIn("water", said(skill, result))

    def test_r3_nothing_under_gravel_is_broken(self):
        def harm(w):
            return w.at((2, 63, 0))[0] != "gravel" \
                or w.at((2, 62, 0))[0] != "gravel"
        skill, result, _w = self.with_and_without(
            "_r3_falling", worlds.gravel_ceiling, "dig_to", harm,
            x=5, y=60, z=0)
        self.assertIn("R3", said(skill, result))
        self.assertIn("gravel", said(skill, result))

    def test_r4_no_stair_onto_a_missing_floor(self):
        skill, result, w = self.with_and_without(
            "_r4_staircase", worlds.hidden_shaft, "dig_to",
            lambda w: any(f > 1 for f in w.falls), x=4, y=56, z=0)
        self.assertIn("R4", said(skill, result))
        self.assertEqual(w.health, 20.0)
        for cell, feet in w.broken:
            self.assertNotEqual(cell, (feet[0], feet[1] - 1, feet[2]))

    def test_r4_no_stair_onto_a_hazard(self):
        skill, result, w = self.with_and_without(
            "_r4_hazards", worlds.magma_floor, "dig_to",
            lambda w: (1, 59, 0) in w.visited, x=4, y=56, z=0)
        self.assertIn("R4", said(skill, result))
        self.assertIn("magma block", said(skill, result))

    def test_r9_not_within_five_of_a_spawner(self):
        spawner = (3, 56, 0)
        skill, result, w = self.with_and_without(
            "_r9_warden", worlds.monster_room, "dig_to",
            lambda w: any(within(c, spawner, digging.WARDEN_REACH)
                          for c in broken(w)), x=5, y=60, z=0)
        self.assertIn("R9", said(skill, result))
        self.assertIn("monster room", said(skill, result))

    def test_r5_forty_blocks_a_task(self):
        """A 50-block tunnel. R5 is enforced twice: up front, the goal is
        refused before anything is broken; as the dig goes, no stair takes
        the count past 40. Each is shown on its own, and with both removed
        the dig goes past 40."""
        w = worlds.stone_volume()
        skill, result = run(w, "dig_to", x=25, y=60, z=0)
        self.assertIn("R5", said(skill, result))
        self.assertIn("furthest I may dig toward it is (20, 60, 0)",
                      said(skill, result))
        self.assertEqual(w.broken, [])
        no_goal_check = mock.patch.object(digging, "goal_refusal",
                                          lambda *a, **k: None)
        dig_mod.forget_digs()
        stair = worlds.stone_volume()
        with no_goal_check:
            skill, result = run(stair, "dig_to", runs=10, x=25, y=60, z=0)
        self.assertIn("R5", said(skill, result))
        self.assertEqual(len(stair.broken), digging.MAX_BROKEN)
        dig_mod.forget_digs()
        bare = worlds.stone_volume()
        with no_goal_check, mock.patch.object(digging, "_r5_limits", allow):
            run(bare, "dig_to", runs=10, x=25, y=60, z=0)
        self.assertGreater(len(bare.broken), digging.MAX_BROKEN)

    def test_r6_a_cave_is_reported_not_entered(self):
        cave = {(3, 60, 0), (4, 60, 0), (3, 60, 1), (4, 60, 1)}
        skill, result, w = self.with_and_without(
            "_r6_opening", worlds.cave_opening, "dig_to",
            lambda w: bool(cave & w.visited), runs=2, x=6, y=60, z=0)
        self.assertIn("opening at (3, 61, 0) (level with where I started)",
                      said(skill, result))
        self.assertIn("can be walked", said(skill, result))

    def test_r7_lava_three_away_and_another_ore_offered(self):
        lava = [(3, 54, 2), (3, 54, 3)]
        skill, result, w = self.with_and_without(
            "_r7_lava", worlds.lava_pocket, "mine_ore",
            lambda w: any(within(c, hot, digging.LAVA_CLEARANCE)
                          for c in broken(w) for hot in lava),
            ore="iron", count=3)
        self.assertIn("R7", said(skill, result))
        self.assertIn("(-8, 56, 0)", skill.done_reason)
        self.assertEqual(w.entered_fluid, [])


class OpenAirTests(_Worlds):
    """R6 in a running dig: the open air over the ground is not a cave.
    From a real run that stopped after one grass block, "the tunnel opened
    into a cave" one cell above it, in the sky."""

    def test_down_from_flat_grass(self):
        w = worlds.flat_grass()
        skill, result = run(w, "dig_to", x=6, y=-6, z=0)
        self.assertFalse(skill.failed, said(skill, result))
        self.assertEqual(w.feet(), (6, -6, 0))
        self.assertNotIn("opening", said(skill, result))
        self.assertEqual(w.falls, [1] * 6, "a stair down is a one-block drop")

    def test_down_a_slope_like_the_log(self):
        w = worlds.slope()
        x, y, z = worlds.SLOPE_START
        skill, result = run(w, "dig_to", x=x + 8, y=y - 8, z=z)
        self.assertFalse(skill.failed, said(skill, result))
        self.assertEqual(w.feet(), (x + 8, y - 8, z))

    def test_into_the_side_of_a_hill_and_a_hollow_in_it(self):
        w = worlds.hill()
        skill, result = run(w, "dig_to", x=8, y=0, z=0)
        self.assertFalse(skill.failed, said(skill, result))
        w = worlds.hill(hollow_at=((5, 0, 0), (5, 1, 0)))
        skill, result = run(w, "dig_to", x=8, y=0, z=0)
        self.assertTrue(skill.failed)
        self.assertIn("(5, 1, 0)", said(skill, result))
        self.assertEqual(w.feet(), (4, 0, 0))

    def test_the_dig_records_what_it_stood_in(self):
        """The skill tells the planner whether it began under open sky, and
        which natural air the player stood in."""
        w = worlds.tunnel()
        skill, _result = run(w, "dig_to", x=6, y=60, z=0)
        self.assertFalse(skill.failed, skill.done_reason)
        done = skill._progress
        self.assertIs(done.surface, False)
        self.assertIn((3, 60, 0), done.walked)
        self.assertIn((3, 61, 0), done.walked)
        sky = worlds.flat_grass()
        skill, _ = run(sky, "dig_to", x=2, y=0, z=0)
        self.assertIs(skill._progress.surface, True)


class OpeningResumeTests(_Worlds):
    """After "opening ... it can be walked", asking for the same dig again
    used to hit the same stop with 0 steps. Now a walkable opening is gone
    through on foot, every other rule still applying, and the dig stops
    again only at the next new opening; one that cannot be walked is
    refused plainly. Each stop says where, how far below the start, and
    what asking again will do."""

    def once(self, w, task="dig_to", **options):
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create(task, **options)
        result = TaskRunner(w, w, sleeper=lambda _s: None).run(
            skill, max_steps=45)
        return skill, result

    def until_stopped(self, w, goal, runs=4):
        """Carry on through step limits (not through openings)."""
        for _ in range(runs):
            skill, result = self.once(w, x=goal[0], y=goal[1], z=goal[2])
            if "ask again to carry on" not in skill.done_reason:
                return skill, result
        return skill, result

    def test_the_stop_says_where_and_what_comes_next(self):
        w = worlds.cave_opening()
        skill, result = self.once(w, x=6, y=60, z=0)
        self.assertTrue(skill.failed)
        self.assertTrue(skill.done_reason.startswith(
            "I stopped digging: opening at (3, 61, 0) (level with where I "
            "started)"), skill.done_reason)
        self.assertIn("can be walked", skill.done_reason)
        self.assertIn("dig_to (6, 60, 0) again", skill.done_reason)
        self.assertIn("on through it on foot", skill.done_reason)
        self.assertIn("the next opening", skill.done_reason)
        self.assertNotIn("ask again", skill.done_reason)

    def test_asking_again_goes_through_on_foot(self):
        w = worlds.cave_opening()
        first, _ = self.once(w, x=6, y=60, z=0)
        self.assertIn("opening at (3, 61, 0)", first.done_reason)
        broken_before = len(w.broken)
        skill, result = self.until_stopped(w, (6, 60, 0))
        self.assertFalse(skill.failed, said(skill, result))
        self.assertEqual(w.feet(), (6, 60, 0))
        self.assertIn("I went on through the opening at (3, 61, 0) on foot",
                      skill.done_reason)
        cave = {(x, y, z) for x in (3, 4) for y in (60, 61) for z in (0, 1)}
        self.assertEqual(cave & set(broken(w)[broken_before:]), set())
        self.assertTrue({(5, 60, 0), (6, 60, 0)} <= set(broken(w)))

    def test_it_stops_again_at_the_next_opening(self):
        w = worlds.two_caves()
        first, _ = self.once(w, x=11, y=60, z=0)
        self.assertIn("opening at (3, 61, 0)", first.done_reason)
        second, _ = self.until_stopped(w, (11, 60, 0))
        self.assertTrue(second.failed)
        self.assertIn("opening at (8, 61, 0)", second.done_reason)
        self.assertIn("I went on through the opening at (3, 61, 0) on foot",
                      second.done_reason)
        self.assertEqual(w.feet(), (7, 60, 0))
        third, _ = self.until_stopped(w, (11, 60, 0))
        self.assertFalse(third.failed, third.done_reason)
        self.assertEqual(w.feet(), (11, 60, 0))

    def test_an_unwalkable_opening_is_refused_plainly(self):
        w = worlds.deep_cave()
        first, _ = self.once(w, x=6, y=60, z=0)
        self.assertIn("opening at (3, 61, 0)", first.done_reason)
        self.assertIn("a drop of more than 3", first.done_reason)
        self.assertIn("I will not walk into it", first.done_reason)
        where, broken_before = w.feet(), len(w.broken)
        again, result = self.once(w, x=6, y=60, z=0)
        self.assertTrue(again.failed)
        self.assertTrue(again.done_reason.startswith(
            "I did not go on: the opening at (3, 61, 0)"), again.done_reason)
        self.assertIn("still not safe to walk into", again.done_reason)
        self.assertIn("a drop of more than 3", again.done_reason)
        self.assertEqual(result.steps_taken, 0)
        self.assertEqual((w.feet(), len(w.broken)), (where, broken_before))

    def test_an_opening_below_the_start_says_how_far(self):
        w = worlds.flat_grass(hollow_at=((3, -3, 0), (3, -2, 0)))
        skill, result = self.until_stopped(w, (6, -6, 0))
        self.assertTrue(skill.done_reason.startswith(
            "I stopped digging: opening at (3, -2, 0) (3 blocks below where "
            "I started)"), skill.done_reason)

    def test_mine_ore_goes_through_too(self):
        w = worlds.cave_opening()
        w.cells[(7, 60, 0)] = block("iron_ore")
        first, _ = self.once(w, "mine_ore", ore="iron")
        self.assertIn("opening at (3, 61, 0)", first.done_reason)
        self.assertIn("mine_ore for iron ore again", first.done_reason)
        again, result = self.once(w, "mine_ore", ore="iron")
        self.assertFalse(again.failed, said(again, result))
        self.assertEqual(w.count("raw_iron"), 1)

    def test_without_going_through_it_stays_stuck(self):
        """The fix removed: asking again meets the same stop, as in the
        real run."""
        w = worlds.cave_opening()
        self.once(w, x=6, y=60, z=0)
        with mock.patch.object(dig_mod.DigTo, "_go_through",
                               lambda self, *a: None):
            again, result = self.once(w, x=6, y=60, z=0)
        self.assertIn("opening at (3, 61, 0)", again.done_reason)
        self.assertEqual(result.steps_taken, 0)


class OreWorldTests(_Worlds):

    def test_the_buried_vein(self):
        w = worlds.buried_vein()
        skill, _result = run(w, "mine_ore", ore="iron", count=3)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(set(broken(w)) & set(worlds.VEIN),
                         set(worlds.VEIN))
        self.assertEqual(w.count("raw_iron"), 3)
        self.assertLessEqual(len(w.broken), digging.MAX_BROKEN)

    def test_the_floating_ore(self):
        """Out of reach in a cave: the staircase stops at the cave wall and
        says so. It does not walk out over the drop or fall."""
        w = worlds.floating_ore()
        skill, result = run(w, "mine_ore", ore="coal")
        self.assertTrue(skill.failed)
        self.assertIn("cave", said(skill, result))
        self.assertEqual(w.falls, [])
        self.assertTrue(all(x < 4 for x, _y, _z in w.visited))
        self.assertEqual(w.count("coal"), 0)


class AbortTests(_Worlds):
    """R8: the world changes during the dig, and the dig stops."""

    def dig(self, w, script, task="dig_to", **options):
        w.script.update(script)
        if not options:
            options = dict(x=5, y=55, z=0)
        return run(w, task, runs=1, **options)

    def test_health_lost(self):
        w = worlds.stone_volume()

        def hurt(world):
            world.health -= 2
        skill, result = self.dig(w, {6: hurt})
        self.assertTrue(skill.failed)
        self.assertIn("health", said(skill, result).lower())

    def test_water_appearing_and_without_the_check(self):
        def flood(world):
            x, y, z = world.feet()
            world.cells[(x + 1, y + 2, z + 1)] = block(
                "water", False, "flowing_water")
        w = worlds.stone_volume()
        skill, result = self.dig(w, {5: flood})
        self.assertIn("water or lava appeared", said(skill, result))
        after = len(w.broken)
        bare = worlds.stone_volume()
        dig_mod.forget_digs()
        with mock.patch.object(digging, "_r8_fluid", lambda *a: ""):
            self.dig(bare, {5: flood})
        self.assertGreater(len(bare.broken), after,
                           "without the check it did not dig on")

    def test_a_cave_in_and_without_the_check(self):
        def cave_in(world):
            for cell, _feet in world.broken[:1]:
                world.cells[cell] = block("gravel")
        w = worlds.stone_volume()
        skill, result = self.dig(w, {9: cave_in})
        self.assertIn("cave-in", said(skill, result))
        bare = worlds.stone_volume()
        dig_mod.forget_digs()
        with mock.patch.object(digging, "_r8_cave_in", lambda *a: ""):
            again, _ = self.dig(bare, {9: cave_in})
        self.assertNotIn("cave-in", again.done_reason)
        self.assertGreater(len(bare.broken), len(w.broken))

    def test_sculk_coming_into_view_and_without_the_check(self):
        """Sculk turns up four behind the player mid-dig. R8 looks before
        the next plan, so it is what stops the dig -- and it still does
        with R9 removed; with both removed, the dig goes on. (Anything in
        the grid's view within five of the body is also within five of
        the next stair, so R9 would refuse that stair too: R8 is the
        backstop, and the one that names what came into view.)"""
        def sculk(world):
            x, y, z = world.feet()
            world.cells[(x - 4, y, z)] = block("sculk")
        w = worlds.stone_volume()
        skill, result = self.dig(w, {6: sculk}, x=6, y=60, z=0)
        self.assertIn("came within 5 blocks", said(skill, result))
        self.assertIn("ancient city (warden)", said(skill, result))
        dig_mod.forget_digs()
        alone = worlds.stone_volume()
        with mock.patch.object(digging, "_r9_warden", allow):
            again, result = self.dig(alone, {6: sculk}, x=6, y=60, z=0)
        self.assertIn("came within 5 blocks", said(again, result))
        dig_mod.forget_digs()
        bare = worlds.stone_volume()
        with mock.patch.object(digging, "_r9_warden", allow), \
                mock.patch.object(digging, "_r8_warden", lambda *a: ""):
            self.dig(bare, {6: sculk}, x=6, y=60, z=0)
        self.assertGreater(len(bare.broken), len(w.broken),
                           "without both checks it did not dig on")

    def test_a_hostile_mob(self):
        w = worlds.stone_volume()

        def zombie(world):
            world.mobs.append(["zombie", (world.x + 2, world.y,
                                          world.z + 1)])
        skill, result = self.dig(w, {6: zombie})
        self.assertTrue(skill.failed)
        self.assertIn("zombie", said(skill, result))
        stopped_at = len(w.broken)
        self.assertLessEqual(stopped_at, 3)

    def test_a_fall(self):
        w = worlds.stone_volume()

        def drop_floor(world):
            x, y, z = world.feet()
            for depth in range(1, 9):
                world.cells[(x, y - depth, z)] = AIR
            world._settle()
        skill, result = self.dig(w, {4: drop_floor}, x=5, y=60, z=0)
        self.assertTrue(skill.failed)
        text = said(skill, result)
        self.assertTrue("fallen" in text or "health" in text, text)
        self.assertEqual([c for c, f in w.broken if f[1] < 60], [])

    def test_a_pickaxe_that_cannot_harvest_the_ore(self):
        w = worlds.buried_vein(hotbar=("wooden_pickaxe",))
        skill, result = run(w, "mine_ore", ore="iron")
        self.assertTrue(skill.failed)
        self.assertIn("stone pickaxe", said(skill, result))
        self.assertEqual(set(broken(w)) & set(worlds.VEIN), set())

    def test_the_session_ending(self):
        w = worlds.stone_volume()

        def expire(world):
            world.guard = "session_expired"
        skill, result = self.dig(w, {5: expire})
        self.assertTrue(skill.failed)
        self.assertIn("session_expired", said(skill, result))
        self.assertLessEqual(w.actions, 5)


class EveryWorldTests(unittest.TestCase):

    def test_each_world_is_built_and_named(self):
        for name, build in worlds.WORLDS.items():
            with self.subTest(world=name):
                w = build()
                self.assertEqual(w.at(w.feet())[0], "air")
                self.assertTrue(w._supported(), "it starts in mid-air")
                self.assertTrue(build.__doc__)
                self.assertIs(w.read().singleplayer, True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
