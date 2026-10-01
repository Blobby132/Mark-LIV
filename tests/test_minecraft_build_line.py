"""
B4b: build_line -- a row or a column of blocks from a start cell, one
direction, up to MAX_LINE_BLOCKS, each placed and proven by place_block_at.

The report names every cell placed and every cell that failed, with why,
and checks the total against the inventory.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.skills import place_build as skills_place_build          # noqa: E402
from minecraft.state import NearbyBlock                             # noqa: E402
from minecraft.task_runner import TaskRunner                        # noqa: E402
from tests.build_world import BuildWorld                            # noqa: E402


def build(world, max_steps=45, **options):
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    options.setdefault("item", "cobblestone")
    skill = skills.create("build_line", **options)
    result = TaskRunner(world, world, sleeper=lambda _s: None).run(
        skill, max_steps=max_steps)
    return skill, result


class LineTests(unittest.TestCase):

    def test_a_row_on_the_ground(self):
        world = BuildWorld({0: ("cobblestone", 10)})
        skill, result = build(world, x=2, y=64, z=0, direction="east",
                              count=4)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual([c for c, _ in world.placed],
                         [(2, 64, 0), (3, 64, 0), (4, 64, 0), (5, 64, 0)])
        self.assertEqual(world.count("cobblestone"), 6)
        self.assertIn("placed 4 of 4", skill.done_reason)
        self.assertIn("(5, 64, 0)", skill.done_reason)
        self.assertIn("cobblestone 10 → 6", skill.done_reason)
        self.assertEqual(world.selected, 0)

    def test_a_column_is_two_high_from_the_ground(self):
        """The third block's top face is above the eye of a player standing
        on the ground: no way to reach it without jumping (B4d)."""
        world = BuildWorld({0: ("cobblestone", 10)})
        skill, _ = build(world, x=2, y=64, z=0, direction="up", count=3)
        self.assertEqual([c for c, _ in world.placed],
                         [(2, 64, 0), (2, 65, 0)])
        self.assertTrue(skill.failed)
        self.assertIn("placed 2 of 3", skill.done_reason)
        self.assertIn("(2, 66, 0)", skill.done_reason)

    def test_a_cell_in_the_way_is_skipped_and_named(self):
        world = BuildWorld({0: ("cobblestone", 10)})
        world.logs.append(NearbyBlock(3, 64, 0, "oak_log", True))
        skill, _ = build(world, x=2, y=64, z=0, direction="east", count=3)
        self.assertEqual([c for c, _ in world.placed],
                         [(2, 64, 0), (4, 64, 0)])
        self.assertIn("placed 2 of 3", skill.done_reason)
        self.assertIn("(3, 64, 0)", skill.done_reason)
        self.assertIn("oak log", skill.done_reason)

    def test_running_out_of_blocks_stops_it(self):
        world = BuildWorld({0: ("cobblestone", 2)})
        skill, _ = build(world, x=2, y=64, z=0, direction="east", count=4)
        self.assertEqual(len(world.placed), 2)
        self.assertIn("placed 2 of 4", skill.done_reason)
        self.assertIn("no cobblestone", skill.done_reason)
        self.assertIn("Not tried: (5, 64, 0)", skill.done_reason,
                      "no cell after the blocks ran out is worth trying")

    def test_three_failures_in_a_row_end_it(self):
        world = BuildWorld({0: ("cobblestone", 10)})
        for x in (3, 4, 5):
            world.logs.append(NearbyBlock(x, 64, 0, "oak_log", True))
        skill, _ = build(world, x=2, y=64, z=0, direction="east", count=5)
        self.assertEqual([c for c, _ in world.placed], [(2, 64, 0)])
        self.assertIn("3 cells in a row failed", skill.done_reason)
        self.assertIn("Not tried: (6, 64, 0)", skill.done_reason)

    def test_the_step_limit_is_reported_with_what_was_done(self):
        world = BuildWorld({0: ("cobblestone", 10)})
        skill, result = build(world, max_steps=6, x=2, y=64, z=0,
                              direction="east", count=6)
        self.assertGreaterEqual(len(world.placed), 1)
        self.assertIn(f"placed {len(world.placed)} of 6", skill.done_reason
                      + result.reason)

    def test_a_stop_right_after_a_place_still_counts_it(self):
        """The runner ends the task (the step limit here; danger the same
        way) on the step that placed a block: the report counts it."""
        probe = BuildWorld({0: ("cobblestone", 10)})
        _, first = build(probe, x=2, y=64, z=0, direction="east", count=1)
        steps = [r.step["action"] for r in first.records].index("place") + 1
        world = BuildWorld({0: ("cobblestone", 10)})
        skill, result = build(world, max_steps=steps, x=2, y=64, z=0,
                              direction="east", count=2)
        self.assertEqual(len(world.placed), 1)
        self.assertIn("placed 1 of 2", result.reason)


class RefusalTests(unittest.TestCase):

    def test_too_long(self):
        world = BuildWorld({0: ("cobblestone", 64)})
        skill, result = build(world, x=2, y=64, z=0, direction="east",
                              count=skills_place_build.MAX_LINE_BLOCKS + 1)
        self.assertTrue(skill.failed)
        self.assertEqual(result.steps_taken, 0)
        self.assertIn(str(skills_place_build.MAX_LINE_BLOCKS), skill.done_reason)

    def test_down_and_nonsense_directions(self):
        for direction in ("down", "sideways", ""):
            with self.subTest(direction=direction):
                world = BuildWorld({0: ("cobblestone", 10)})
                skill, result = build(world, x=2, y=64, z=0,
                                      direction=direction, count=2)
                self.assertTrue(skill.failed)
                self.assertEqual(result.steps_taken, 0)

    def test_not_enough_blocks_for_any(self):
        world = BuildWorld({0: ("dirt", 10)})
        skill, result = build(world, x=2, y=64, z=0, direction="east",
                              count=2)
        self.assertTrue(skill.failed)
        self.assertEqual(result.steps_taken, 0)
        self.assertIn("no cobblestone", skill.done_reason)


class RegistryTests(unittest.TestCase):

    def test_it_is_a_task_and_described(self):
        self.assertIn("build_line", skills.available())
        from actions import minecraft as mc_actions
        self.assertIn("build_line", mc_actions.TOOL["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
