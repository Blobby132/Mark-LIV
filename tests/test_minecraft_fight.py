"""
B5b (2/2): fight -- opt-in: only when the user asks to fight.

It picks the nearest hostile mob (or the one named), walks into reach,
aims at the body's centre, and attacks in short taps -- each with
`expect_hostile`, so the controller presses nothing unless the game reports
a hostile under the crosshair at that moment. Never a player, never a
passive mob, never a creeper (it explodes: run instead). Below 8 health it
retreats with flee. Twenty seconds at most.

Item 5: reproduced -- an enderman, a zombified piglin, a piglin, a ghast and
the rest were walked up to and hit; a fight started at any health; and it
swung with whatever was in hand, a block of dirt included. Now every mob
in NEVER_MELEE is refused with its reason, a fight does not start below
FIGHT_START_HEALTH, and it takes up the best sword (else axe) in the hotbar
before the first swing and puts the old slot back when it ends.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import action_spec, skills                           # noqa: E402
from minecraft.skills import combat as skills_combat                    # noqa: E402
from minecraft.controller import ActionResult                       # noqa: E402
from minecraft.state import EntityRef, ItemStack                    # noqa: E402
from tests.support.mob_world import MobWorld  # noqa: E402
from tests.support.sim_world import run  # noqa: E402


class Arena(MobWorld):
    """MobWorld with a crosshair that can land on a mob, and an attack
    that hurts what it lands on -- refused, as the controller refuses it,
    when the attack expects a hostile and the mob under the crosshair is
    not one."""

    REACH = 3.0
    HEIGHTS = {"zombie": 1.95, "cow": 1.4, "player": 1.8, "spider": 0.9,
               "creeper": 1.7}

    def __init__(self, mobs, mob_health=20.0, clock_step=0.3, hotbar=None,
                 selected=0, **kwargs):
        super().__init__(mobs, **kwargs)
        self.mob_health = {i: mob_health for i in range(len(self.mobs))}
        self.hotbar = dict(hotbar or {})       # slot -> item name
        self.selected = selected
        self.held_at_hits = []
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
        inventory = tuple(ItemStack(slot=slot, name=name, count=1)
                          for slot, name in sorted(self.hotbar.items()))
        held = next((i for i in inventory if i.slot == self.selected), None)
        return dataclasses.replace(state, nearby_entities=entities,
                                   target_entity=seen,
                                   inventory=inventory,
                                   selected_slot=self.selected,
                                   held_item=held,
                                   captured_at=self.clock)

    def hotbar_select(self, params):
        self.selected = int(params["slot"]) - 1
        return self._result("hotbar_select", params)

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
            self.held_at_hits.append(self.hotbar.get(self.selected))
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


NEVER_FOUGHT = ("creeper", "warden", "enderman", "zombified_piglin",
                "piglin", "piglin_brute", "ravager", "wither",
                "ender_dragon", "ghast", "elder_guardian", "guardian",
                "shulker", "evoker", "hoglin", "blaze", "wither_skeleton")


class NeverMeleeTests(unittest.TestCase):
    """Item 5: mobs a melee fight with a hand-held weapon goes wrong with --
    neutral ones that bring the rest down on you, bosses, and those that
    hit from out of reach. Refused before a step, each with its reason."""

    def test_each_is_refused_with_a_reason(self):
        for name in NEVER_FOUGHT:
            with self.subTest(mob=name):
                world = Arena([(name, 3.5, 0.5, "hostile")])
                skill, result = fight(world)
                self.assertEqual(result.steps_taken, 0)
                self.assertEqual(world.hits, {})
                self.assertIn(" ".join(name.split("_")), skill.done_reason)
                self.assertIn(skills_combat.NEVER_MELEE[name], skill.done_reason)

    def test_named_or_nearest_alike(self):
        world = Arena([("enderman", 3.5, 0.5, "hostile")])
        skill, result = fight(world, target="enderman")
        self.assertEqual(result.steps_taken, 0)

    def test_every_reason_says_something(self):
        for name in NEVER_FOUGHT:
            self.assertGreater(len(skills_combat.NEVER_MELEE.get(name, "")), 15,
                               name)

    def test_a_zombie_next_to_an_enderman_is_still_fought(self):
        world = Arena([("enderman", 6.5, 0.5, "hostile"), zombie(2.5, 0.5)])
        fight(world)
        self.assertGreater(world.hits.get("zombie", 0), 0)
        self.assertEqual(world.hits.get("enderman", 0), 0)


class HealthFloorTests(unittest.TestCase):

    def test_below_the_floor_it_does_not_start(self):
        world = Arena([zombie(2.5, 0.5)], health=skills_combat.FIGHT_START_HEALTH - 1)
        skill, result = fight(world)
        self.assertEqual(result.steps_taken, 0)
        self.assertEqual(world.hits, {})
        self.assertIn("health", skill.done_reason)

    def test_at_the_floor_it_does(self):
        world = Arena([zombie(2.5, 0.5)], health=skills_combat.FIGHT_START_HEALTH)
        fight(world)
        self.assertGreater(world.hits.get("zombie", 0), 0)

    def test_health_it_cannot_read_does_not_start_one(self):
        world = Arena([zombie(2.5, 0.5)], health=None)
        skill, result = fight(world)
        self.assertEqual(result.steps_taken, 0)
        self.assertIn("cannot read my health", skill.done_reason)

    def test_the_floor_is_above_the_retreat(self):
        self.assertGreater(skills_combat.FIGHT_START_HEALTH,
                           skills_combat.FIGHT_RETREAT_HEALTH)


class WeaponTests(unittest.TestCase):

    def test_the_best_sword_is_taken_up_and_the_slot_put_back(self):
        world = Arena([zombie(2.5, 0.5)],
                      hotbar={0: "dirt", 2: "stone_sword", 3: "iron_sword",
                              5: "diamond_axe"})
        skill, result = fight(world)
        self.assertTrue(world.held_at_hits)
        self.assertEqual(set(world.held_at_hits), {"iron_sword"})
        actions = [r.step["action"] for r in result.records]
        self.assertLess(actions.index("hotbar_select"),
                        actions.index("attack"))
        self.assertEqual(world.selected, 0, "the slot was not put back")

    def test_an_axe_when_there_is_no_sword(self):
        world = Arena([zombie(2.5, 0.5)],
                      hotbar={0: "dirt", 1: "wooden_axe", 4: "iron_axe"})
        fight(world)
        self.assertEqual(set(world.held_at_hits), {"iron_axe"})
        self.assertEqual(world.selected, 0)

    def test_held_already_nothing_to_change(self):
        world = Arena([zombie(2.5, 0.5)], hotbar={0: "iron_sword"})
        _skill, result = fight(world)
        self.assertNotIn("hotbar_select",
                         [r.step["action"] for r in result.records])

    def test_no_weapon_fights_bare_handed_and_says_so(self):
        world = Arena([zombie(2.5, 0.5)], hotbar={0: "dirt"})
        skill, _result = fight(world)
        self.assertGreater(world.hits.get("zombie", 0), 0)
        self.assertIn("no sword or axe", skill.done_reason)

    def test_the_slot_is_put_back_after_a_retreat(self):
        world = Arena([zombie(2.5, 0.5)], mob_health=1000.0, health=14.0,
                      hurt_per_step=1.0, hotbar={0: "dirt", 2: "iron_sword"})
        skill, _result = fight(world)
        self.assertIn("retreat", skill.done_reason)
        self.assertEqual(world.selected, 0, "the slot was not put back")


class RegistryTests(unittest.TestCase):

    def test_it_is_described_as_opt_in(self):
        self.assertIn("fight", skills.available())
        from actions import minecraft as mc_actions
        text = mc_actions.TOOL["description"]
        self.assertIn("fight", text)
        self.assertIn("only when the user asks", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
