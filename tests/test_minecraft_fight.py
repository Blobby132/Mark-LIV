"""
B5b (2/2): fight -- opt-in: only when the user asks to fight.

It picks the nearest hostile mob (or the one named), walks into reach,
aims at the body's centre, and attacks in short taps -- each with
`expect_hostile`, so the controller presses nothing unless the game reports
a hostile under the crosshair at that moment. Never a player, never a
passive mob, never a creeper (it explodes: run instead). Below 8 health it
retreats with flee. Twenty seconds at most.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import action_spec, skills                           # noqa: E402
from minecraft.controller import ActionResult                       # noqa: E402
from minecraft.state import EntityRef                               # noqa: E402
from test_minecraft_flee import MobWorld                            # noqa: E402
from test_minecraft_navigation import run                           # noqa: E402


class Arena(MobWorld):
    """MobWorld with a crosshair that can land on a mob, and an attack
    that hurts what it lands on -- refused, as the controller refuses it,
    when the attack expects a hostile and the mob under the crosshair is
    not one."""

    REACH = 3.0
    HEIGHTS = {"zombie": 1.95, "cow": 1.4, "player": 1.8, "spider": 0.9,
               "creeper": 1.7}

    def __init__(self, mobs, mob_health=20.0, clock_step=0.3, **kwargs):
        super().__init__(mobs, **kwargs)
        self.mob_health = {i: mob_health for i in range(len(self.mobs))}
        self.hits = {}
        self.refused = 0
        self.clock = 1000.0
        self.clock_step = clock_step

    def _alive(self):
        return [(i, m) for i, m in enumerate(self.mobs)
                if self.mob_health.get(i, 0) > 0]

    def _under_crosshair(self):
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        view = (-math.sin(yaw) * math.cos(pitch), -math.sin(pitch),
                math.cos(yaw) * math.cos(pitch))
        eye = (self.x, self.y + self.EYE, self.z)
        best = None
        for i, (name, mx, mz, category) in self._alive():
            height = self.HEIGHTS.get(name, 1.8)
            centre = (mx, 64.0 + height / 2, mz)
            to = [c - e for c, e in zip(centre, eye)]
            d = math.sqrt(sum(v * v for v in to))
            if d > self.REACH + 0.5:
                continue
            along = sum(a * b for a, b in zip(to, view))
            if along <= 0:
                continue
            miss = math.sqrt(max(0.0, d * d - along * along))
            if miss <= max(0.3, height / 2):
                if best is None or d < best[0]:
                    best = (d, i)
        return None if best is None else best[1]

    def read(self):
        import dataclasses
        self.clock += self.clock_step
        state = super().read()
        alive = {i for i, _m in self._alive()}
        entities = tuple(e for i, e in enumerate(state.nearby_entities)
                         if i in alive)
        target = self._under_crosshair()
        seen = None
        if target is not None:
            name, mx, mz, category = self.mobs[target]
            seen = EntityRef(name=name, category=category,
                             hostile=category == "hostile",
                             position=(mx, 64.0, mz),
                             distance=math.dist((self.x, self.z), (mx, mz)))
        return dataclasses.replace(state, nearby_entities=entities,
                                   target_entity=seen,
                                   captured_at=self.clock)

    def attack(self, params):
        spec = action_spec.parse_attack(params)
        target = self._under_crosshair()
        if spec.expect_hostile and (
                target is None or self.mobs[target][3] != "hostile"):
            self.refused += 1
            return ActionResult(ok=False, action="attack",
                                requested=dict(params),
                                actual_duration_ms=0,
                                stopped_reason="target_not_confirmed",
                                error="not a hostile")
        if target is not None:
            self.hits[self.mobs[target][0]] = \
                self.hits.get(self.mobs[target][0], 0) + 1
            self.mob_health[target] -= 4.0
            mob = self.mobs[target]                 # knockback, as in game
            dx, dz = mob[1] - self.x, mob[2] - self.z
            d = math.hypot(dx, dz) or 1.0
            mob[1] += dx / d * 0.5
            mob[2] += dz / d * 0.5
        self._tick()
        return self._result("attack", params, 100)


def zombie(x, z):
    return ("zombie", x, z, "hostile")


def fight(world, max_steps=45, **options):
    skill = skills.create("fight", **options)
    return skill, run(world, skill, max_steps=max_steps)


class FightTests(unittest.TestCase):

    def test_a_zombie_is_fought_until_it_is_gone(self):
        world = Arena([zombie(4.5, 0.5)])
        skill, result = fight(world)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertGreaterEqual(world.hits.get("zombie", 0), 5)
        self.assertIn("gone", skill.done_reason)
        self.assertEqual(world.refused, 0)

    def test_attacks_are_spaced_for_the_cooldown(self):
        world = Arena([zombie(2.5, 0.5)])
        _skill, result = fight(world)
        actions = [r.step["action"] for r in result.records]
        for a, b in zip(actions, actions[1:]):
            self.assertFalse(a == b == "attack", "two swings back to back")

    def test_every_swing_asks_for_a_hostile(self):
        world = Arena([zombie(2.5, 0.5)])
        _skill, result = fight(world)
        swings = [r for r in result.records if r.step["action"] == "attack"]
        self.assertTrue(swings)
        for record in swings:
            self.assertIs(record.step["params"].get("expect_hostile"), True)
            self.assertLessEqual(record.step["params"]["duration"], 0.2)

    def test_a_cow_in_the_way_is_never_hit(self):
        """The cow stands in the line of fire, within reach: the crosshair
        finds the cow first. It must not swing -- not even to have the
        controller refuse it."""
        world = Arena([("cow", 1.5, 0.5, "passive"), zombie(2.6, 0.5)])
        fight(world, max_steps=12)
        self.assertEqual(world.hits.get("cow", 0), 0)
        self.assertEqual(world.refused, 0,
                         "it swung at the cow and relied on the controller")

    def test_a_named_creeper_is_refused_too(self):
        world = Arena([("creeper", 3.5, 0.5, "hostile")])
        skill, result = fight(world, target="creeper")
        self.assertEqual(result.steps_taken, 0)
        self.assertIn("flee", skill.done_reason)

    def test_a_player_is_never_a_target(self):
        world = Arena([("Steve", 2.5, 0.5, "player")])
        skill, result = fight(world)
        self.assertEqual(world.hits, {})
        self.assertEqual(result.steps_taken, 0)
        self.assertIn("no hostile", skill.done_reason)

    def test_below_eight_health_it_retreats(self):
        world = Arena([zombie(2.5, 0.5)], mob_health=1000.0, health=12.0,
                      hurt_per_step=1.0)
        skill, result = fight(world)
        self.assertIn("retreat", skill.done_reason)
        self.assertIn("health", skill.done_reason)
        swings_after = False
        hurt = False
        for record in result.records:
            if (record.state_before.get("health") or 20) < 8:
                hurt = True
            if hurt and record.step["action"] == "attack":
                swings_after = True
        self.assertFalse(swings_after, "it kept swinging below 8 health")

    def test_twenty_seconds_at_most(self):
        world = Arena([zombie(2.5, 0.5)], mob_health=1000.0, clock_step=2.0)
        skill, result = fight(world, seconds=60)
        self.assertLess(result.steps_taken, 15)
        self.assertIn("20s", skill.done_reason)

    def test_a_creeper_is_not_walked_up_to(self):
        world = Arena([("creeper", 3.5, 0.5, "hostile")])
        skill, result = fight(world)
        self.assertEqual(world.hits, {})
        self.assertEqual(result.steps_taken, 0)
        self.assertIn("creeper", skill.done_reason)
        self.assertIn("flee", skill.done_reason)

    def test_the_named_mob_is_the_target(self):
        world = Arena([zombie(2.5, 0.5), ("spider", -2.5, 0.5, "hostile")])
        fight(world, target="spider")
        self.assertGreater(world.hits.get("spider", 0), 0)
        self.assertEqual(world.hits.get("zombie", 0), 0)


class RegistryTests(unittest.TestCase):

    def test_it_is_described_as_opt_in(self):
        self.assertIn("fight", skills.available())
        from actions import minecraft as mc_actions
        text = mc_actions.TOOL["description"]
        self.assertIn("fight", text)
        self.assertIn("only when the user asks", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
