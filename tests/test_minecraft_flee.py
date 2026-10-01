"""
B5a: flee -- get away from hostile mobs.

A* to the reachable scanned column furthest from every hostile (the route
keeps two blocks clear of each), sprinting on clear straight stretches,
re-planned as the mobs move, until the nearest hostile is over 12 blocks
away or time runs out. Never stopped by a mob being close or by damage:
that is what it is for (A1 made movement exempt from the hazard probe).
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import skills                                        # noqa: E402
from minecraft.state import EXACT, EntityRef, WorldState            # noqa: E402
from test_minecraft_navigation import TreeWorld, flat, run          # noqa: E402


class MobWorld(TreeWorld):
    """A flat clearing with mobs that may walk towards the player."""

    SPRINT_SPEED = 5.6

    def __init__(self, mobs, chase=0.0, health=20.0, hurt_per_step=0.0,
                 radius=16, **kwargs):
        super().__init__(flat(radius=radius), [], **kwargs)
        self.mobs = [list(m) for m in mobs]   # [name, x, z, category]
        self.chase = chase                     # blocks a mob closes per step
        self.health = health
        self.hurt_per_step = hurt_per_step
        self.sprints = 0
        self.nearest_seen = []

    def _tick(self):
        for mob in self.mobs:
            dx, dz = self.x - mob[1], self.z - mob[2]
            d = math.hypot(dx, dz)
            if self.chase and d > 1.0:
                mob[1] += dx / d * min(self.chase, d - 1.0)
                mob[2] += dz / d * min(self.chase, d - 1.0)
        self.health = max(1.0, self.health - self.hurt_per_step)

    def read(self):
        base = TreeWorld.read(self)
        entities = []
        for name, mx, mz, category in self.mobs:
            entities.append(EntityRef(
                name=name, position=(mx, 64.0, mz), category=category,
                hostile=category == "hostile",
                distance=math.dist((self.x, self.z), (mx, mz))))
        hostile = [e.distance for e in entities if e.hostile]
        if hostile:
            self.nearest_seen.append(min(hostile))
        return WorldState(
            position=base.position, rotation=base.rotation,
            surface=base.surface, scan_radius=base.scan_radius,
            target_block=base.target_block, nearby_entities=tuple(entities),
            health=self.health, on_ground=True, source="bridge",
            confidence=EXACT)

    def move(self, params):
        result = TreeWorld.move(self, params)
        self._tick()
        return result

    def look(self, params):
        result = TreeWorld.look(self, params)
        self._tick()
        return result

    def sprint(self, params):
        self.sprints += 1
        seconds = float(params.get("duration", 0))
        self._walk(seconds * self.SPRINT_SPEED, jumping=False)
        self._tick()
        return self._result("sprint", params, int(seconds * 1000))


def zombie(x, z):
    return ("zombie", x, z, "hostile")


class FleeTests(unittest.TestCase):

    def test_it_gets_more_than_twelve_blocks_away(self):
        world = MobWorld([zombie(3.5, 0.5)])
        skill = skills.create("flee")
        result = run(world, skill, max_steps=45)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertGreater(world.nearest_seen[-1], 12.0)
        self.assertIn("12", skill.done_reason)

    def test_it_runs_away_not_towards(self):
        world = MobWorld([zombie(3.5, 0.5)])
        run(world, skills.create("flee"), max_steps=45)
        self.assertLess(world.x, 0.5, "it ran towards the zombie's side")

    def test_a_mob_at_arms_length_does_not_stop_it_starting(self):
        """Every other task refuses to start with a hostile within 5. This
        one exists for exactly that moment."""
        world = MobWorld([zombie(1.5, 0.5)])
        skill = skills.create("flee")
        result = run(world, skill, max_steps=45)
        self.assertGreater(result.steps_taken, 0)
        self.assertNotIn("danger", result.reason.lower())

    def test_damage_does_not_stop_it(self):
        world = MobWorld([zombie(2.5, 0.5)], hurt_per_step=1.0)
        skill = skills.create("flee")
        result = run(world, skill, max_steps=45)
        self.assertGreater(result.steps_taken, 3)
        self.assertNotIn("danger", result.reason.lower())

    def test_it_sprints_on_open_ground(self):
        world = MobWorld([zombie(3.5, 0.5)])
        run(world, skills.create("flee"), max_steps=45)
        self.assertGreater(world.sprints, 0)

    def test_between_two_it_gets_away_from_both(self):
        world = MobWorld([zombie(4.5, 0.5), zombie(-3.5, 0.5)])
        track = []
        original = world.read

        def watched():
            state = original()
            track.append(sorted(e.distance for e in state.nearby_entities))
            return state
        world.read = watched
        skill = skills.create("flee")
        run(world, skill, max_steps=45)
        self.assertFalse(skill.failed, skill.done_reason)
        start = track[0]
        for later in track[1:]:
            self.assertGreaterEqual(later[0], start[0] - 0.5,
                                    "it went towards one of them")

    def test_cornered_it_says_so(self):
        """A clearing too small to get twelve blocks from a mob that
        follows: it ends, and says how close the mob still is."""
        world = MobWorld([zombie(3.5, 0.5)], chase=1.0, radius=5)
        skill = skills.create("flee")
        result = run(world, skill, max_steps=45)
        self.assertTrue(skill.failed)
        self.assertIn("zombie", skill.done_reason)
        self.assertIn("blocks away", skill.done_reason)

    def test_nothing_to_run_from(self):
        world = MobWorld([("cow", 3.5, 0.5, "passive")])
        skill = skills.create("flee")
        result = run(world, skill, max_steps=45)
        self.assertEqual(result.steps_taken, 0)
        self.assertFalse(skill.failed)
        self.assertIn("no hostile", skill.done_reason)


class RegistryTests(unittest.TestCase):

    def test_it_is_a_task_and_described(self):
        self.assertIn("flee", skills.available())
        from actions import minecraft as mc_actions
        self.assertIn("flee", mc_actions.TOOL["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
