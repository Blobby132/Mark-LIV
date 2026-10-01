"""
navigate_to, three fixes found building a shelter (B4c):

1. A hop onto a lone raised block can carry past it. The walk turned round
   and hopped back past it the other way, and every hop moved, so no stall
   was ever counted: it went on until the task's step limit. Now it gives
   up after MAX_HOPS_ONTO hops at the same block and says why.
2. A player off the middle of a column, a shoulder over a wall's corner,
   walked straight at the next waypoint and scraped the wall every time --
   a route between column centres never meets that corner. Now it steps
   back to the middle of its column first.
3. `keep_off`: columns never routed through, for a structure being built
   (its wall tops are walkable, and no place to be).

Plus the simulation: a jump clears one block, so a shoulder against a
two-high wall stops the move, jumping or not.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import skills                                        # noqa: E402
from minecraft.state import NearbyBlock                             # noqa: E402
from test_minecraft_navigation import SimWorld, flat, run           # noqa: E402


def with_columns(raised):
    """flat(), with some columns' floors raised: {(x, z): y}."""
    out = []
    for block in flat():
        y = raised.get((block.x, block.z), block.y)
        out.append(NearbyBlock(block.x, y, block.z, "stone", True, 4))
    return out


class HopTests(unittest.TestCase):

    def test_overshooting_a_lone_block_ends_and_says_so(self):
        world = SimWorld(with_columns({(2, 0): 64}), position=(1.6, 64.0, 0.5),
                         yaw=-90.0)
        skill = skills.NavigateTo(destination=(2, 0), arrive_within=0.4)
        result = run(world, skill, max_steps=45)
        hops = [r for r in result.records
                if r.step["action"] == "move_and_jump"]
        self.assertLessEqual(len(hops), skills.MAX_HOPS_ONTO)
        self.assertLess(result.steps_taken, 45, "it ran to the step limit")
        if skill.failed:
            self.assertIn("hopped at the block", skill.done_reason)


class RecentreTests(unittest.TestCase):

    def test_off_the_middle_by_a_wall_corner_it_steps_back_first(self):
        world = SimWorld(with_columns({(1, 1): 65}), position=(0.5, 64.0, 0.85),
                         yaw=-90.0)
        skill = skills.NavigateTo(destination=(3, 0), arrive_within=0.5)
        result = run(world, skill, max_steps=20)
        self.assertFalse(skill.failed, skill.done_reason)
        moves = [r for r in result.records if r.step["action"] == "move"]
        self.assertIn("middle of this block", moves[0].step["note"])
        self.assertEqual(world.blocked, 0, "it walked into the wall")

    def test_in_the_middle_it_does_not(self):
        world = SimWorld(with_columns({(1, 1): 65}), position=(0.5, 64.0, 0.5),
                         yaw=-90.0)
        skill = skills.NavigateTo(destination=(3, 0), arrive_within=0.5)
        result = run(world, skill, max_steps=20)
        self.assertFalse(skill.failed, skill.done_reason)
        notes = " ".join(r.step["note"] for r in result.records)
        self.assertNotIn("middle of this block", notes)


class KeepOffTests(unittest.TestCase):

    def test_the_route_goes_round_kept_off_columns(self):
        world = SimWorld(flat(), position=(0.5, 64.0, 0.5), yaw=-90.0)
        skill = skills.NavigateTo(destination=(3, 0), arrive_within=0.5,
                                  keep_off=frozenset({(1, 0), (2, 0)}))
        visited = set()
        original = world._walk

        def tracking(distance, jumping):
            original(distance, jumping)
            visited.add((int(world.x // 1), int(world.z // 1)))
        world._walk = tracking
        run(world, skill, max_steps=30)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertFalse(visited & {(1, 0), (2, 0)}, visited)


class SimulationTests(unittest.TestCase):

    def test_a_jump_does_not_carry_a_shoulder_into_a_two_high_wall(self):
        world = SimWorld(with_columns({(1, 1): 65}), position=(0.5, 64.0, 0.85),
                         yaw=-90.0)
        world._walk(1.0, jumping=True)
        self.assertLess(world.x, 0.71, "a shoulder went into the wall")


if __name__ == "__main__":
    unittest.main(verbosity=2)
