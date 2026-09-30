"""
eat_food: eating from the hotbar.

The bridge reports hunger and every inventory slot, and choosing a hotbar
slot and holding right click are both actions the controller already
allows. What had to be got right is judgement, not input:

  * what to eat -- nothing that makes you ill, not the golden apple, and not
    a steak when two points of hunger are missing;
  * where to eat -- holding right click with a chest, door or campfire under
    the crosshair uses THAT, so it looks up first and refuses if it cannot;
  * what it cannot do -- food outside the hotbar needs the inventory screen,
    and it says so instead of pretending;
  * and leaving the hand as it found it.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.controller import ActionResult                       # noqa: E402
from minecraft.state import (                                       # noqa: E402
    BlockRef, EXACT, EntityRef, ItemStack, UNKNOWN, WorldState,
)
from minecraft.task_runner import (                                 # noqa: E402
    COMPLETED, DISPATCH, STOPPED, TaskRunner,
)

EAT_SECONDS = 1.61          # 32 ticks


class Kitchen:
    """A player with a hotbar, hunger and a crosshair -- enough of the game
    to eat in, and to show what goes wrong when the click lands elsewhere."""

    def __init__(self, stacks, hunger=14.0, selected=0, target=None,
                 entity=None, health=20.0, losing_health=False, mobs=()):
        self.stacks = {slot: [name, count] for slot, (name, count)
                       in stacks.items()}
        self.hunger = hunger
        self.selected = selected
        self.target = target
        self.entity = entity
        self.pitch = 0.0
        self.health = health
        self.losing_health = losing_health
        self.mobs = tuple(mobs)
        self.used_instead = []            # what a right click used, not ate
        self.eaten = []

    # -- state source --
    def read(self):
        if self.losing_health:
            self.health = max(1.0, self.health - 2.0)     # starving
        inventory = tuple(ItemStack(slot=slot, name=name, count=count)
                          for slot, (name, count) in sorted(self.stacks.items())
                          if count > 0)
        return WorldState(hunger=self.hunger, health=self.health,
                          inventory=inventory, selected_slot=self.selected,
                          target_block=self.target, target_entity=self.entity,
                          rotation=(0.0, self.pitch),
                          nearby_entities=self.mobs,
                          source="bridge", confidence=EXACT)

    # -- controller --
    def _guard(self):
        return ""

    def _result(self, name, params):
        return ActionResult(ok=True, action=name,
                            requested=dict(params or {}),
                            actual_duration_ms=100)

    def hotbar_select(self, params):
        self.selected = int(params["slot"]) - 1
        return self._result("hotbar_select", params)

    def look(self, params):
        self.pitch += float(params.get("dy", 0)) / nav.pixels_per_degree()
        if self.pitch <= -30:
            self.target = None             # sky
            self.entity = None
        return self._result("look", params)

    def eat(self, params):
        if self.entity is not None:
            self.used_instead.append(self.entity.name)
            return self._result("eat", params)
        name = getattr(self.target, "name", None)
        if name and skills._interactive(name):
            self.used_instead.append(name)
            return self._result("eat", params)
        held = self.stacks.get(self.selected)
        if held and held[1] > 0 and float(params.get("duration", 0)) \
                >= EAT_SECONDS and self.hunger < 20:
            food = held[0]
            points = skills.FOODS.get(food) or \
                {"rotten_flesh": 4, "golden_apple": 4}.get(food, 0)
            held[1] -= 1
            self.hunger = min(20.0, self.hunger + points)
            self.eaten.append(food)
        return self._result("eat", params)

    def __getattr__(self, name):
        if name in DISPATCH:
            return lambda params: self._result(name, params)
        raise AttributeError(name)


def eat(kitchen, **options):
    nav.reset_calibration()
    runner = TaskRunner(kitchen, kitchen, sleeper=lambda _s: None)
    skill = skills.create("eat_food", **options)
    return skill, runner.run(skill)


class WhatItEats(unittest.TestCase):

    def test_it_eats_from_the_hotbar_and_puts_the_hand_back(self):
        kitchen = Kitchen({0: ("iron_axe", 1), 3: ("bread", 5)}, hunger=14)
        skill, result = eat(kitchen)
        self.assertEqual(result.status, COMPLETED, result.reason)
        self.assertEqual(kitchen.eaten, ["bread"])
        self.assertEqual(kitchen.stacks[3][1], 4)
        self.assertEqual(kitchen.hunger, 19)
        self.assertEqual(kitchen.selected, 0, "the axe was not put back")
        actions = [r.step["action"] for r in result.records]
        self.assertEqual(actions, ["hotbar_select", "eat", "hotbar_select"])

    def test_the_best_fit_is_chosen_not_the_biggest(self):
        kitchen = Kitchen({0: ("cooked_beef", 3), 1: ("apple", 3)}, hunger=16)
        eat(kitchen)
        self.assertEqual(kitchen.eaten, ["apple"], "a steak at 16 wastes 4")

    def test_when_everything_overshoots_the_smallest_is_chosen(self):
        kitchen = Kitchen({0: ("cooked_beef", 3), 1: ("bread", 3)}, hunger=18)
        eat(kitchen)
        self.assertEqual(kitchen.eaten, ["bread"])

    def test_count_eats_more_than_one_while_hungry(self):
        kitchen = Kitchen({2: ("bread", 5)}, hunger=6)
        skill, _ = eat(kitchen, count=2)
        self.assertEqual(kitchen.eaten, ["bread", "bread"])
        self.assertFalse(skill.failed)

    def test_it_stops_when_full_even_with_count_left(self):
        kitchen = Kitchen({2: ("cooked_beef", 5)}, hunger=12)
        eat(kitchen, count=3)
        self.assertEqual(kitchen.eaten, ["cooked_beef"])

    def test_full_is_not_a_failure_and_eats_nothing(self):
        kitchen = Kitchen({2: ("bread", 5)}, hunger=20)
        skill, result = eat(kitchen)
        self.assertEqual(kitchen.eaten, [])
        self.assertEqual(result.records, ())
        self.assertFalse(skill.failed)
        self.assertIn("not hungry", result.reason)


class WhatItWillNotEat(unittest.TestCase):

    def test_food_that_makes_you_ill_is_refused_and_named(self):
        kitchen = Kitchen({0: ("rotten_flesh", 8)}, hunger=6)
        skill, result = eat(kitchen)
        self.assertEqual(kitchen.eaten, [])
        self.assertTrue(skill.failed)
        self.assertIn("rotten flesh", result.reason)
        self.assertIn("Hunger", result.reason)

    def test_golden_apples_are_not_snacks(self):
        kitchen = Kitchen({0: ("golden_apple", 2)}, hunger=10)
        _, result = eat(kitchen)
        self.assertEqual(kitchen.eaten, [])
        self.assertIn("too valuable", result.reason)

    def test_food_outside_the_hotbar_is_named_not_reached_for(self):
        kitchen = Kitchen({20: ("baked_potato", 4)}, hunger=10)
        skill, result = eat(kitchen)
        self.assertEqual(result.records, (), "it tried to reach it anyway")
        self.assertTrue(skill.failed)
        self.assertIn("baked potato", result.reason)
        self.assertIn("move it to the hotbar", result.reason)

    def test_without_the_bridge_it_says_it_cannot_see(self):
        state = WorldState(source="f3", confidence=EXACT,
                           provenance={"inventory": UNKNOWN})
        skill = skills.create("eat_food")
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertTrue(skill.failed)
        self.assertIn("bridge mod", skill.done_reason)


class WhereItEats(unittest.TestCase):

    def test_a_chest_under_the_crosshair_is_looked_away_from_first(self):
        chest = BlockRef(name="chest", x=1, y=64, z=0)
        kitchen = Kitchen({0: ("bread", 3)}, hunger=10, target=chest)
        _, result = eat(kitchen)
        self.assertEqual(kitchen.used_instead, [], "it opened the chest")
        self.assertEqual(kitchen.eaten, ["bread"])
        notes = [r.step.get("note", "") for r in result.records]
        self.assertTrue(any(n.startswith("look up") for n in notes))

    def test_a_mob_under_the_crosshair_is_looked_away_from(self):
        cow = EntityRef(name="minecraft:cow", distance=2.0, hostile=False)
        kitchen = Kitchen({0: ("bread", 3)}, hunger=10, entity=cow)
        eat(kitchen)
        self.assertEqual(kitchen.used_instead, [])
        self.assertEqual(kitchen.eaten, ["bread"])

    def test_it_refuses_rather_than_click_something_it_cannot_avoid(self):
        class Ceiling(Kitchen):
            def look(self, params):            # a chest overhead, too
                self.pitch += float(params.get("dy", 0)) \
                    / nav.pixels_per_degree()
                return self._result("look", params)

        chest = BlockRef(name="chest", x=1, y=64, z=0)
        kitchen = Ceiling({0: ("bread", 3)}, hunger=10, target=chest)
        skill, result = eat(kitchen)
        self.assertEqual(kitchen.used_instead, [])
        self.assertEqual(kitchen.eaten, [])
        self.assertTrue(skill.failed)
        self.assertIn("chest", result.reason)

    def test_plain_ground_under_the_crosshair_is_fine(self):
        grass = BlockRef(name="grass_block", x=1, y=63, z=0)
        kitchen = Kitchen({0: ("bread", 3)}, hunger=10, target=grass)
        _, result = eat(kitchen)
        self.assertEqual(kitchen.eaten, ["bread"])
        self.assertFalse(any(r.step["action"] == "look"
                             for r in result.records))


class DangerWhileEating(unittest.TestCase):

    def test_starving_does_not_stop_the_meal_that_cures_it(self):
        kitchen = Kitchen({3: ("bread", 3)}, hunger=0, losing_health=True)
        _, result = eat(kitchen)
        self.assertNotEqual(result.status, STOPPED, result.reason)
        self.assertEqual(kitchen.eaten, ["bread"])

    def test_a_zombie_still_does(self):
        zombie = EntityRef(name="zombie", distance=2.0, hostile=True)
        kitchen = Kitchen({0: ("bread", 3)}, hunger=10, mobs=(zombie,))
        _, result = eat(kitchen)
        self.assertEqual(result.status, STOPPED)
        self.assertEqual(kitchen.eaten, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class HowLongItHolds(unittest.TestCase):
    """C8: every eat held right-click for MAX_EAT_DURATION_S, 2.0 seconds.
    A honey bottle takes 40 ticks -- exactly 2.0 seconds -- so it had no
    room at all for input latency or a server running slow, and dried kelp
    (16 ticks) was eaten twice per step. The hold is now sized to the food
    and the cap raised to 3 seconds."""

    SLOW_TPS = 16.0            # a busy server; 20 is the game's own rate
    LATENCY_S = 0.1            # the press reaching the game, the first tick

    def planned_hold(self, food):
        kitchen = Kitchen({0: (food, 5)}, hunger=4)
        skill = skills.create("eat_food")
        step = skill.plan(kitchen.read(), 0, ())
        self.assertEqual(step.action, "eat", step.note)
        return float(step.params["duration"])

    def test_every_food_is_finished_even_on_a_slow_server(self):
        for food in skills.FOODS:
            with self.subTest(food=food):
                use = skills.eat_ticks(food) / self.SLOW_TPS
                self.assertGreaterEqual(self.planned_hold(food),
                                        use + self.LATENCY_S)

    def test_no_food_is_eaten_twice_in_one_hold(self):
        for food in skills.FOODS:
            with self.subTest(food=food):
                twice = 2 * skills.eat_ticks(food) / 20.0
                self.assertLess(self.planned_hold(food), twice)

    def test_the_use_times_are_the_games(self):
        self.assertEqual(skills.eat_ticks("honey_bottle"), 40)
        self.assertEqual(skills.eat_ticks("dried_kelp"), 16)
        self.assertEqual(skills.eat_ticks("bread"), 32)

    def test_the_cap_is_about_three_seconds(self):
        from minecraft import action_spec
        self.assertEqual(action_spec.MAX_EAT_DURATION_S, 3.0)
        self.assertEqual(action_spec.parse_eat({"duration": 9}).duration, 3.0)
