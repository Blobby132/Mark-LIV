"""
N9: collect_logs' walkers are for one target each.

_walk_towards built its NavigateTo once, for the first log it walked to,
and kept it while _next_log went on re-picking the nearest reachable log
every step. When the pick changed mid-walk, the walker still went to the
OLD log's column -- and on arriving there, the new log was checked for
reach, failed, and was written off as "out of reach from anywhere I can
stand next to it", which was not true.

The pickup walker had the same shape: a drop that vanished on the way
(merged into another stack, carried off by water, despawned) still got
walked to, spending one of MAX_PICKUP_WALKS on an empty spot.
"""

from __future__ import annotations

import unittest


from minecraft import navigation as nav                              # noqa: E402
from minecraft import skills                                         # noqa: E402
from minecraft.skills import base as skills_base                        # noqa: E402
from minecraft.state import (                                        # noqa: E402
    EXACT, EntityRef, ItemStack, NearbyBlock, WorldState)
from tests.support.sim_world import flat  # noqa: E402

EAST = NearbyBlock(6, 64, 0, "oak_log", True)
WEST = NearbyBlock(-6, 64, 0, "oak_log", True)


def state_at(x, z, logs=(EAST, WEST), entities=(), inventory=()):
    return WorldState(position=(x, 64.0, z), rotation=(270.0, 0.0),
                      surface=tuple(flat()), notable_blocks=tuple(logs),
                      nearby_entities=tuple(entities),
                      inventory=tuple(inventory), scan_radius=8,
                      source="test", confidence=EXACT)


class LogWalkerTests(unittest.TestCase):

    def test_a_new_target_gets_a_new_walk(self):
        skill = skills.create("collect_logs")
        start = state_at(0.5, 0.5)
        self.assertIsNotNone(skill._walk_towards(start, EAST))
        east_column = nav.approach_column(start, EAST)
        # Standing where the walk to EAST ends, asked to walk to WEST.
        there = state_at(east_column[0] + 0.5, east_column[1] + 0.5)
        step = skill._walk_towards(there, WEST)
        self.assertIsNot(step, skills_base._ARRIVED,
                         "the walk to the old log counted as arriving at "
                         "the new one")
        self.assertEqual(skill._walker._destination,
                         nav.approach_column(there, WEST))

    def test_the_same_target_keeps_its_walk(self):
        """No churn: the walker's stall count lives in it."""
        skill = skills.create("collect_logs")
        start = state_at(0.5, 0.5)
        skill._walk_towards(start, EAST)
        walker = skill._walker
        skill._walk_towards(state_at(1.5, 0.5), EAST)
        self.assertIs(skill._walker, walker)

    def test_the_next_log_up_the_same_trunk_keeps_its_walk(self):
        skill = skills.create("collect_logs")
        start = state_at(0.5, 0.5)
        skill._walk_towards(start, EAST)
        walker = skill._walker
        above = NearbyBlock(6, 65, 0, "oak_log", True)
        skill._walk_towards(state_at(1.5, 0.5, logs=(EAST, above)), above)
        self.assertIs(skill._walker, walker)


def log_drop(x, z=0.5):
    return EntityRef(name="item", distance=1.0, position=(x, 64.0, z),
                     category="item", hostile=False,
                     item=ItemStack(name="oak_log", count=1))


class PickupWalkerTests(unittest.TestCase):

    def fetching(self):
        skill = skills.create("collect_logs")
        skill._broken, skill._collected = 2, 0
        skill._broken_at = [(4, 64, 0), (-4, 64, 0)]
        state = state_at(0.5, 0.5, logs=(),
                         entities=(log_drop(4.5), log_drop(-3.5)),
                         inventory=(ItemStack(slot=0, name="oak_log",
                                              count=0),))
        local = nav.LocalMap.from_state(state)
        self.assertIsNotNone(skill._pick_up(state, local, ()))
        return skill, local

    def test_a_drop_that_vanished_is_not_walked_to(self):
        skill, local = self.fetching()
        first = skill._pickup_walker._destination
        # The one it was walking to is gone; the other is still there.
        remaining = log_drop(-3.5) if first[0] > 0 else log_drop(4.5)
        state = state_at(1.5, 0.5, logs=(), entities=(remaining,),
                         inventory=(ItemStack(slot=0, name="oak_log",
                                              count=0),))
        self.assertIsNotNone(skill._pick_up(state, local, ()))
        now = skill._pickup_walker._destination
        self.assertLess(abs(now[0] - remaining.position[0]), 2.0,
                        f"still walking to {first}, where nothing is left")

    def test_the_drop_it_is_walking_to_keeps_its_walk(self):
        skill, local = self.fetching()
        walker = skill._pickup_walker
        state = state_at(1.5, 0.5, logs=(),
                         entities=(log_drop(4.5), log_drop(-3.5)),
                         inventory=(ItemStack(slot=0, name="oak_log",
                                              count=0),))
        skill._pick_up(state, local, ())
        self.assertIs(skill._pickup_walker, walker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
