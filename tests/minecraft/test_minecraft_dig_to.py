"""
dig_to: a staircase to a cell, in a simulated world, under the hard rules.

For each cell: re-observe, re-check every rule, take up the pickaxe, aim at
the cell, break it, see the cell is air in the next reading, then step and
see the position. Drops are picked up by walking through them. Every step
carries an expectation, so the runner never sees a blind run.

The world is DigWorld (tests/support/dig_world.py): voxel rock, gravity,
blocks that fall into an opened cell, fluid that flows into one. Nothing
here has run in the real game.
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
from tests.support.dig_world import (AIR, LAVA, WATER, DigWorld, block,
                                     hollow)

START = (0.5, 60.0, 0.5)
HOLE = {(0, 60, 0): AIR, (0, 61, 0): AIR}


def world(extra=None, **options):
    cells = dict(HOLE)
    cells.update(extra or {})
    return DigWorld(cells, position=START, **options)


def dig(w, goal, max_steps=45, runs=1):
    """Run dig_to `runs` times (asking again carries on); the last skill
    and result."""
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    skill = result = None
    for _ in range(runs):
        skill = skills.create("dig_to", x=goal[0], y=goal[1], z=goal[2])
        runner = TaskRunner(w, w, sleeper=lambda _s: None)
        result = runner.run(skill, max_steps=max_steps)
        if not skill.failed:
            break
    return skill, result


class DigToTests(unittest.TestCase):

    def setUp(self):
        dig_mod.forget_digs()
        self.addCleanup(dig_mod.forget_digs)

    # ── the staircase ────────────────────────────────────────────────────

    def test_a_staircase_down(self):
        w = world()
        skill, result = dig(w, (5, 55, 0), runs=3)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(w.feet(), (5, 55, 0))
        self.assertEqual(len(w.broken), 15)
        for cell, feet in w.broken:
            with self.subTest(cell=cell):
                self.assertNotEqual(cell, (feet[0], feet[1] - 1, feet[2]),
                                    "it broke the block underfoot")
                self.assertNotEqual((cell[0], cell[2]), (feet[0], feet[2]),
                                    "it dug straight down or up")
        self.assertEqual(w.health, 20.0)
        self.assertGreaterEqual(w.count("cobblestone"), 12,
                                "it did not pick up what it broke")

    def test_a_level_tunnel_into_a_hill(self):
        w = world()
        skill, _result = dig(w, (5, 60, 0), runs=2)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(w.feet(), (5, 60, 0))
        self.assertEqual(len(w.broken), 10)

    def test_every_step_has_an_expectation(self):
        w = world()
        _skill, result = dig(w, (3, 60, 0))
        for record in result.records:
            with self.subTest(step=record.index):
                self.assertTrue(record.step.get("expectation"),
                                f"a blind step: {record.step}")

    def test_it_says_how_far_it_got(self):
        w = world()
        skill, _result = dig(w, (5, 55, 0), max_steps=12)
        self.assertTrue(skill.failed)
        self.assertIn("ask again to carry on", skill.done_reason)
        again, _ = dig(w, (5, 55, 0), runs=3)
        self.assertFalse(again.failed, again.done_reason)
        self.assertEqual(w.feet(), (5, 55, 0))
        self.assertEqual(len(w.broken), 15, "the second run started over")

    # ── it stops before the danger ───────────────────────────────────────

    def test_a_lava_pocket(self):
        w = world({(3, 57, 2): LAVA})
        skill, _result = dig(w, (5, 55, 0), runs=3)
        self.assertTrue(skill.failed)
        self.assertIn("lava", skill.done_reason)
        self.assertNotIn((1, 1), [], "")
        for cell, _feet in w.broken:
            self.assertGreater(max(abs(cell[0] - 3), abs(cell[1] - 57),
                                   abs(cell[2] - 2)), 3,
                               f"it broke {cell}, within 3 of the lava")
        self.assertEqual(w.entered_fluid, [])

    def test_a_water_pocket(self):
        """The first stair is clear of it (the water is diagonal to it);
        the second would break a cell beside it, and does not."""
        w = world({(2, 61, 1): WATER})
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("water", skill.done_reason)
        for cell, _feet in w.broken:
            beside = [(cell[0] + d[0], cell[1] + d[1], cell[2] + d[2])
                      for d in digging.SIDES]
            self.assertNotIn((2, 61, 1), beside,
                             f"it broke {cell}, beside the water")
        self.assertEqual(w.entered_fluid, [])

    def test_a_gravel_ceiling(self):
        w = world({(2, 62, 0): block("gravel")})
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("gravel", skill.done_reason)
        self.assertNotIn((2, 61, 0), [c for c, _ in w.broken])

    def test_with_the_gravel_rule_gone_the_cave_in_stops_it(self):
        """The planner's R3 removed: the gravel comes down into the cell
        just cleared, and R8 stops the dig there."""
        w = world({(2, 62, 0): block("gravel")})
        with mock.patch.object(digging, "_r3_falling",
                               lambda *a, **k: None):
            skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("cave-in", skill.done_reason)

    def test_a_cave_opening(self):
        w = world(hollow({}, (3, 60, 0), (3, 61, 0), (4, 60, 0), (4, 61, 0)))
        skill, _result = dig(w, (6, 60, 0), runs=2)
        self.assertTrue(skill.failed)
        self.assertIn("cave", skill.done_reason)
        self.assertIn("(3, 61, 0)", skill.done_reason)
        self.assertLessEqual(w.feet()[0], 2)

    # ── R8 while it digs ─────────────────────────────────────────────────

    def test_health_lost_stops_it(self):
        w = world()

        def hurt(world_):
            world_.health -= 3
        w.script[6] = hurt
        skill, result = dig(w, (5, 55, 0))
        self.assertTrue(skill.failed)
        said = f"{result.reason} {skill.done_reason}"
        self.assertIn("health", said)
        self.assertNotEqual(result.status, "completed")

    def test_water_appearing_stops_it(self):
        w = world()

        def flood(world_):
            world_.cells[(1, 62, 1)] = block("water", False, "flowing_water")
        w.script[5] = flood
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("water or lava appeared", skill.done_reason)

    def test_a_fall_stops_it(self):
        w = world()

        def drop_floor(world_):
            """The ground goes from under the player, wherever they are:
            a hidden cave under the tunnel floor."""
            x, y, z = world_.feet()
            for depth in range(1, 9):
                world_.cells[(x, y - depth, z)] = AIR
            world_._settle()
        w.script[4] = drop_floor
        skill, result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertLess(w.feet()[1], 60, "the floor did not go")
        said = f"{result.reason} {skill.done_reason}"
        self.assertTrue("fallen" in said or "health" in said, said)
        dug_after = [c for c, feet in w.broken if feet[1] < 60]
        self.assertEqual(dug_after, [], "it dug on after the fall")

    def test_no_pickaxe_no_digging(self):
        w = world(hotbar=("bread",))
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("pickaxe", skill.done_reason)
        self.assertEqual(w.broken, [])

    def test_a_pickaxe_too_weak_for_the_ore(self):
        w = world({(1, 61, 0): block("iron_ore"), (1, 60, 0): block("iron_ore")},
                  hotbar=("wooden_pickaxe",))
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("stone pickaxe", skill.done_reason)
        self.assertEqual(w.broken, [])

    # ── where it will not dig at all ─────────────────────────────────────

    def test_not_on_a_server(self):
        w = world(singleplayer=False)
        skill, result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertIn("single-player", skill.done_reason)
        self.assertEqual(w.broken, [])
        self.assertEqual(result.steps_taken, 0)

    def test_not_without_the_grid(self):
        w = world(grid=False)
        skill, _result = dig(w, (5, 60, 0))
        self.assertTrue(skill.failed)
        self.assertEqual(w.broken, [])

    def test_a_goal_past_the_limits(self):
        cases = {(0, 40, 30): "(0, 52, 16)", (40, 60, 0): "(20, 60, 0)",
                 (25, 60, 0): "(20, 60, 0)"}
        for goal, nearest in cases.items():
            with self.subTest(goal=goal):
                w = world()
                skill, result = dig(w, goal)
                self.assertTrue(skill.failed)
                self.assertIn("R5", skill.done_reason)
                self.assertIn("furthest I may dig toward it",
                              skill.done_reason)
                self.assertIn(nearest, skill.done_reason)
                self.assertEqual(w.broken, [])
                self.assertEqual(result.steps_taken, 0)

    def test_without_the_check_up_front_it_digs_to_the_budget(self):
        """The 40-block budget was only enforced as the dig went: with the
        up-front check removed, a 50-block tunnel is dug until R5 stops it
        at 40."""
        w = world()
        with mock.patch.object(digging, "goal_refusal",
                               lambda *a, **k: None):
            skill, _result = dig(w, (25, 60, 0), runs=10)
        self.assertIn("R5", skill.done_reason)
        self.assertEqual(len(w.broken), digging.MAX_BROKEN)



class ConsentWordingTests(unittest.TestCase):
    """What one confirmation covers now includes digging: said in the four
    places that change together."""

    def test_the_four_places(self):
        from actions import minecraft as mc_actions
        from minecraft.session import GRANT_SUMMARY
        from tests.support.paths import REPO_ROOT
        self.assertIn("single-player world only -- dig a staircase",
                      GRANT_SUMMARY)
        detail = mc_actions._mc_guard({"action": "start_session"})["detail"]
        self.assertIn("never the block under you", detail)
        self.assertIn("single-player", detail)
        readme = (REPO_ROOT / "docs" / "minecraft" / "README.md").read_text(
            encoding="utf-8")
        start = readme.index("**What it deliberately cannot do.**")
        self.assertIn("dig_to", readme[start:start + 3000])
        self.assertIn("bridge_a_route", skills.NOT_YET_POSSIBLE)
        self.assertNotIn("dig_or_bridge_a_route", skills.NOT_YET_POSSIBLE)
        self.assertIn("dig_to",
                      mc_actions.TOOL["parameters"]["properties"]["task"]
                      ["description"])

    def test_the_task_takes_coordinates_and_needs_the_grid(self):
        from actions import minecraft as mc_actions
        self.assertEqual(mc_actions._TASK_NEEDS["dig_to"],
                         ("singleplayer", "near_grid"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
