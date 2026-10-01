"""
Tasks stop when the player is in danger.

A task runs for up to two minutes and holds attack for seconds at a time.
Nothing used to look up from the job between steps: a zombie walking up and
hitting the player went unnoticed and the chopping went on. The runner now
checks health and nearby hostile mobs after every step, and before the
first, and stops with a sentence saying why -- including what the task had
done by then.
"""

from __future__ import annotations

import dataclasses
import unittest


from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.danger import DangerWatch, HOSTILE_RADIUS            # noqa: E402
from minecraft.state import EntityRef, EXACT, NearbyBlock, WorldState  # noqa: E402
from minecraft.task_runner import STOPPED                           # noqa: E402

from tests.support.sim_world import TreeWorld, flat, run  # noqa: E402


def mob(name, distance, hostile=True):
    return EntityRef(name=name, distance=distance, hostile=hostile,
                     category="hostile" if hostile else "passive",
                     position=(0.0, 64.0, distance))


def seen(health=None, entities=()):
    return WorldState(health=health, nearby_entities=tuple(entities),
                      source="test", confidence=EXACT)


class DangerWatchTests(unittest.TestCase):

    def test_a_hostile_close_by_is_danger(self):
        reason = DangerWatch().check(seen(entities=[mob("minecraft:zombie",
                                                        3.2)]))
        self.assertEqual(reason, "a zombie is 3 blocks away")

    def test_a_hostile_further_off_is_not(self):
        far = HOSTILE_RADIUS + 3
        self.assertIsNone(DangerWatch().check(
            seen(entities=[mob("skeleton", far)])))

    def test_a_cow_is_not(self):
        self.assertIsNone(DangerWatch().check(
            seen(entities=[mob("cow", 1.0, hostile=False)])))

    def test_an_unclassified_mob_is_not_evidence(self):
        unknown = EntityRef(name="modded_thing", distance=1.0, hostile=None)
        self.assertIsNone(DangerWatch().check(seen(entities=[unknown])))

    def test_a_mob_in_a_cave_below_is_not(self):
        cave = EntityRef(name="zombie", distance=4.5, hostile=True,
                         position=(0.5, 60.0, 2.5))
        state = WorldState(position=(0.5, 64.0, 0.5),
                           nearby_entities=(cave,), source="test",
                           confidence=EXACT)
        self.assertIsNone(DangerWatch().check(state))

    def test_a_mob_on_the_same_level_is(self):
        level = EntityRef(name="zombie", distance=3.0, hostile=True,
                          position=(0.5, 65.0, 3.5))
        state = WorldState(position=(0.5, 64.0, 0.5),
                           nearby_entities=(level,), source="test",
                           confidence=EXACT)
        self.assertIsNotNone(DangerWatch().check(state))

    def test_losing_a_heart_is_danger(self):
        watch = DangerWatch()
        self.assertIsNone(watch.check(seen(health=20.0)))
        self.assertIsNone(watch.check(seen(health=19.0)), "half a heart")
        self.assertEqual(watch.check(seen(health=17.0)),
                         "I am taking damage (health 20 to 17 of 20)")

    def test_damage_is_measured_from_the_best_point_not_the_start(self):
        watch = DangerWatch()
        watch.check(seen(health=12.0))
        watch.check(seen(health=18.0))            # regenerated
        self.assertIn("18 to 15", watch.check(seen(health=15.0)))

    def test_hurt_and_a_mob_are_said_together(self):
        watch = DangerWatch()
        watch.check(seen(health=20.0))
        reason = watch.check(seen(health=14.0,
                                  entities=[mob("creeper", 2.0)]))
        self.assertIn("taking damage", reason)
        self.assertIn("creeper is 2 blocks away", reason)

    def test_a_skill_can_opt_out_of_the_health_half_only(self):
        watch = DangerWatch(watch_health=False)
        watch.check(seen(health=20.0))
        self.assertIsNone(watch.check(seen(health=4.0)))
        self.assertIsNotNone(watch.check(seen(entities=[mob("zombie", 2)])))


class Ambush(TreeWorld):
    """A tree to chop, and a zombie that walks up after a few steps."""

    def __init__(self, *args, arrives_after=3, **kwargs):
        super().__init__(*args, **kwargs)
        self.steps = 0
        self.arrives_after = arrives_after

    def _count(self):
        self.steps += 1

    def mine(self, params):
        self._count()
        return TreeWorld.mine(self, params)

    def look(self, params):
        self._count()
        return TreeWorld.look(self, params)

    def move(self, params):
        self._count()
        return TreeWorld.move(self, params)

    def read(self):
        state = TreeWorld.read(self)
        entities = ()
        if self.steps >= self.arrives_after:
            entities = (mob("minecraft:zombie", 2.5),)
        return dataclasses.replace(state, health=20.0,
                                   nearby_entities=entities)


class TasksStopInDangerTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def logs(self):
        return [NearbyBlock(4, y, 0, "oak_log", True) for y in range(64, 68)]

    def test_a_zombie_arriving_mid_task_stops_it(self):
        world = Ambush(flat(), self.logs(), inventory={"oak_log": 0},
                       arrives_after=3)
        skill = skills.create("fell_tree")
        result = run(world, skill, max_steps=45)
        self.assertEqual(result.status, STOPPED)
        self.assertIn("danger: a zombie is 2 blocks away", result.reason)
        self.assertIn("Before that", result.reason,
                      "what it had done by then went missing")
        self.assertLessEqual(world.steps, 4, "it carried on after seeing it")

    def test_it_does_not_start_next_to_a_hostile(self):
        world = Ambush(flat(), self.logs(), inventory={"oak_log": 0},
                       arrives_after=0)
        result = run(world, skills.create("collect_logs", count=1))
        self.assertEqual(result.status, STOPPED)
        self.assertEqual(world.steps, 0)
        self.assertIn("did not start", result.reason)

    def test_a_quiet_world_is_not_interrupted(self):
        world = Ambush(flat(), self.logs(), inventory={"oak_log": 0},
                       arrives_after=10_000)
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=45)
        self.assertNotEqual(result.status, STOPPED, result.reason)
        self.assertFalse(skill.failed, skill.done_reason)


class WalkingAwayTests(unittest.TestCase):
    """Walking is how you get away from a mob. Refusing to walk because one
    is close -- or stopping halfway -- would take away the one thing that
    helps, so navigate_to keeps going and says what is near when it ends."""

    def test_navigate_to_walks_away_from_a_zombie(self):
        from tests.support.sim_world import SimWorld

        class Chased(SimWorld):
            def read(self):
                return dataclasses.replace(
                    SimWorld.read(self), health=20.0,
                    nearby_entities=(mob("minecraft:zombie", 3.0),))

        world = Chased(flat())
        skill = skills.create("navigate_to", destination=(-6, 0))
        result = run(world, skill)
        self.assertNotEqual(result.status, STOPPED, result.reason)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertLess(world.distance_to((-6, 0)), 2.0)
        self.assertIn("Note: a zombie is 3 blocks away", result.reason)

    def test_a_task_that_walks_as_part_of_its_job_still_stops(self):
        world = Ambush(flat(), [NearbyBlock(6, 64, 0, "oak_log", True)],
                       inventory={"oak_log": 0}, arrives_after=1)
        result = run(world, skills.create("collect_logs", count=1))
        self.assertEqual(result.status, STOPPED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
