"""
mine_ore: the nearest listed ore it can safely reach, a staircase to beside
it, then the vein -- every hard rule of digging still applying -- and a
summary counted from the inventory.

In DigWorld (tests/support/dig_world.py), whose ore scan lists the ore
placed in it the way the mod's does. Nothing here has run in the game.
"""

from __future__ import annotations

import unittest

from minecraft import aiming as aiming_mod
from minecraft import navigation as nav
from minecraft import ores
from minecraft import skills
from minecraft.skills import dig as dig_mod
from minecraft.state import OreHit, OreSnapshot, WorldState
from minecraft.task_runner import TaskRunner
from tests.support.dig_world import AIR, LAVA, WATER, DigWorld, block

START = (0.5, 60.0, 0.5)
HOLE = {(0, 60, 0): AIR, (0, 61, 0): AIR}
VEIN = {(6, 56, 0): block("iron_ore"), (6, 56, 1): block("iron_ore"),
        (7, 56, 0): block("iron_ore")}


def world(extra=None, **options):
    cells = dict(HOLE)
    cells.update(extra or {})
    return DigWorld(cells, position=START, **options)


def mine(w, ore="iron", count=3, runs=3, **options):
    skill = result = None
    for _ in range(runs):
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create("mine_ore", ore=ore, count=count, **options)
        result = TaskRunner(w, w, sleeper=lambda _s: None).run(
            skill, max_steps=45)
        if "ask again" not in skill.done_reason:
            break
    return skill, result


class MineOreTests(unittest.TestCase):

    def setUp(self):
        dig_mod.forget_digs()
        self.addCleanup(dig_mod.forget_digs)

    def test_a_buried_vein(self):
        w = world(VEIN)
        skill, _result = mine(w)
        self.assertFalse(skill.failed, skill.done_reason)
        mined = {c for c, _f in w.broken} & set(VEIN)
        self.assertEqual(mined, set(VEIN), "it did not take the vein")
        self.assertGreaterEqual(w.count("raw_iron"), 2)
        self.assertIn(f"{w.count('raw_iron')} more raw iron",
                      skill.done_reason)
        for cell, feet in w.broken:
            self.assertNotEqual(cell, (feet[0], feet[1] - 1, feet[2]),
                                "it broke the block underfoot")

    def test_asking_again_carries_on_with_the_same_counts(self):
        from actions import minecraft as mc_actions
        once = world(VEIN)
        mine(once)
        whole = len(once.broken)
        dig_mod.forget_digs()
        w = world(VEIN)
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        first = skills.create("mine_ore", ore="iron", count=3)
        TaskRunner(w, w, sleeper=lambda _s: None).run(first, max_steps=20)
        self.assertTrue(first.failed)
        self.assertIn("ask again to carry on", first.done_reason)
        plan = mc_actions._mine_ore_plan(w.read(), {"ore": "iron"})
        self.assertIn("Plan: carry on with the iron ore at (6, 56, 0)", plan)
        again, _ = mine(w)
        self.assertFalse(again.failed, again.done_reason)
        self.assertEqual(len(w.broken), whole, "the second ask started over")
        self.assertIn(f"{len(w.broken)} of 40 for this task",
                      again.done_reason)
        self.assertEqual(dig_mod.mine_under_way("iron"), "")

    def test_count_stops_it(self):
        w = world(VEIN)
        skill, _result = mine(w, count=1)
        self.assertFalse(skill.failed, skill.done_reason)
        mined = {c for c, _f in w.broken} & set(VEIN)
        self.assertEqual(len(mined), 1)

    def test_an_exposed_ore_in_reach_needs_no_digging(self):
        w = world({(2, 60, 0): block("coal_ore"), (1, 60, 0): AIR,
                   (1, 61, 0): AIR})
        skill, _result = mine(w, ore="coal", count=1)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual([c for c, _f in w.broken], [(2, 60, 0)])
        self.assertEqual(w.count("coal"), 1)

    def test_an_ore_with_water_beside_it_is_passed_over(self):
        """The nearer ore has water on it: it is passed over for the vein,
        which is further off, and the dig never goes near the water."""
        near_water = {(3, 58, -4): block("iron_ore"), (3, 59, -4): WATER}
        w = world({**near_water, **VEIN})
        skill, _result = mine(w)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertNotIn((3, 58, -4), [c for c, _f in w.broken])
        self.assertEqual({c for c, _f in w.broken} & set(VEIN), set(VEIN))
        self.assertEqual(w.entered_fluid, [])

    def test_lava_on_the_way_stops_it_and_offers_another(self):
        other = {(-8, 56, 0): block("iron_ore")}
        w = world({**VEIN, **other, (3, 54, 2): LAVA})
        skill, _result = mine(w)
        self.assertTrue(skill.failed)
        self.assertIn("lava", skill.done_reason)
        self.assertIn("(-8, 56, 0)", skill.done_reason,
                      "it did not offer the other ore")
        for cell, _feet in w.broken:
            self.assertGreater(max(abs(cell[0] - 3), abs(cell[1] - 54),
                                   abs(cell[2] - 2)), 3)

    def test_gravel_on_the_ore_stops_it_beside_the_ore(self):
        """The way there is clear; the ore itself has gravel on it (R3),
        so it is left, and another ore is offered -- not "done, 0"."""
        w = world({(6, 56, 0): block("iron_ore"), (6, 57, 0): block("gravel"),
                   (-8, 56, 0): block("iron_ore")})
        skill, _result = mine(w)
        self.assertTrue(skill.failed)
        self.assertIn("without breaking it -- R3", skill.done_reason)
        self.assertIn("(-8, 56, 0)", skill.done_reason)
        self.assertNotIn((6, 56, 0), [c for c, _f in w.broken])
        self.assertEqual(w.at((6, 57, 0))[0], "gravel")

    def test_nothing_safe_says_so_plainly(self):
        w = world({(3, 58, 0): block("iron_ore"), (3, 59, 0): LAVA})
        skill, result = mine(w)
        self.assertTrue(skill.failed)
        self.assertIn("no iron ore I can safely reach", skill.done_reason)
        self.assertEqual(w.broken, [])
        self.assertEqual(result.steps_taken, 0)

    def test_it_never_promises_ore_the_scan_did_not_list(self):
        w = world(VEIN)
        skill, _result = mine(w, ore="diamond")
        self.assertTrue(skill.failed)
        self.assertIn("lists no diamond ore", skill.done_reason)
        self.assertEqual(w.broken, [])

    def test_not_on_a_server(self):
        w = world(VEIN, singleplayer=False)
        skill, _result = mine(w)
        self.assertTrue(skill.failed)
        self.assertIn("single-player", skill.done_reason)
        self.assertEqual(w.broken, [])

    def test_the_plan_names_ore_distance_depth_and_digging(self):
        w = world(VEIN)
        choice, _passed = ores.choose(w.read(), "iron", 16,
                                      within_reach=aiming_mod.SHARED
                                      .within_reach)
        text = choice.describe()
        self.assertIn("iron ore at (6, 56, 0)", text)
        self.assertIn("4 below your feet", text)
        self.assertIn("stair", text)
        self.assertIn("blocks", text)


def scan(*hits, position=(0.5, 60.0, 0.5)):
    """A reading with only an ore scan: OreHit(name, x, y, z, exposed,
    fluid_near) each."""
    return WorldState(position=position, singleplayer=True,
                      ores=OreSnapshot(origin=(0, 60, 0), radius=24, down=32,
                                       up=16, complete=True,
                                       ores=tuple(OreHit(*h) for h in hits)))


class ChooseTests(unittest.TestCase):
    """ores.choose: the nearest listed ore that can be reached safely, and
    why each one before it was passed over."""

    def test_the_nearest_buried_one_and_where_to_stand(self):
        state = scan(("iron_ore", 6, 56, 0, False, False),
                     ("iron_ore", -9, 56, 0, False, False))
        choice, passed = ores.choose(state, "iron")
        self.assertEqual(choice.hit.position, (6, 56, 0))
        self.assertEqual(choice.stand, (5, 56, 0))
        self.assertEqual((choice.stairs, choice.blocks), (5, 14))
        self.assertEqual(passed, [])

    def test_exposed_and_in_reach_needs_no_digging(self):
        state = scan(("coal_ore", 2, 60, 0, True, False))
        choice, _ = ores.choose(state, "coal",
                                within_reach=lambda _p, _t: True)
        self.assertIsNone(choice.stand)
        self.assertIn("no digging", choice.describe())
        choice, _ = ores.choose(state, "coal")      # reach unknown: dig
        self.assertIsNotNone(choice.stand)

    def test_each_reason_to_pass_one_over(self):
        cases = {
            "water or lava": ("iron_ore", 2, 58, 0, False, True),
            "more than 16 blocks down": ("iron_ore", 2, 40, 0, False,
                                         False),
            "more than 32 blocks away": ("iron_ore", 2, 58, 1, False,
                                         False),
            "about 41 blocks": ("iron_ore", 13, 47, 0, False, False),
        }
        for why, hit in cases.items():
            with self.subTest(why=why):
                start = (-31, 60, 0) if "32" in why else None
                choice, passed = ores.choose(scan(hit), "iron", 32,
                                             start=start)
                self.assertIsNone(choice)
                self.assertEqual(len(passed), 1)
                self.assertIn(why, passed[0])

    def test_blocks_already_broken_count(self):
        state = scan(("iron_ore", 6, 56, 0, False, False))
        self.assertIsNotNone(ores.choose(state, "iron", broken=25)[0])
        choice, passed = ores.choose(state, "iron", broken=26)
        self.assertIsNone(choice)
        self.assertIn("more than the 14 this task may still break",
                      passed[0])

    def test_skip_and_the_vein(self):
        state = scan(("iron_ore", 6, 56, 0, False, False),
                     ("iron_ore", 7, 56, 0, False, False),
                     ("iron_ore", 7, 57, 0, False, False),
                     ("coal_ore", 6, 56, 1, False, False),
                     ("iron_ore", -8, 56, 0, False, False))
        vein = ores.vein_of(state, (6, 56, 0))
        self.assertEqual(vein, {(6, 56, 0), (7, 56, 0), (7, 57, 0)})
        choice, _ = ores.choose(state, "iron", skip=vein)
        self.assertEqual(choice.hit.position, (-8, 56, 0))

    def test_none_chosen_says_why(self):
        state = scan(("iron_ore", 2, 58, 0, False, True))
        _, passed = ores.choose(state, "iron")
        text = ores.none_chosen(state, "iron", 16, passed)
        self.assertIn("no iron ore I can safely reach", text)
        self.assertIn("(2, 58, 0): water or lava", text)
        text = ores.none_chosen(state, "gold", 16, [])
        self.assertIn("lists no gold ore", text)
        self.assertIn("only go for ore the scan lists", text)


class StartedReplyTests(unittest.TestCase):
    """The model is told to say the plan first; the started reply repeats
    it, from the same choice the task makes."""

    def test_the_started_reply_carries_the_plan(self):
        from actions import minecraft as mc_actions
        w = world(VEIN)
        text = mc_actions._mine_ore_plan(w.read(), {"ore": "iron"})
        self.assertIn("Plan:", text)
        self.assertIn("iron ore at (6, 56, 0)", text)

    def test_no_plan_no_start(self):
        from actions import minecraft as mc_actions
        w = world(VEIN)
        text = mc_actions._mine_ore_plan(w.read(), {"ore": "diamond"})
        self.assertNotIn("Plan:", text)
        self.assertIn("lists no diamond ore", text)

    def test_what_one_confirmation_covers_says_so(self):
        from actions import minecraft as mc_actions
        from minecraft.session import GRANT_SUMMARY
        from tests.support.paths import REPO_ROOT
        self.assertIn("to the nearest ore the scan lists", GRANT_SUMMARY)
        detail = mc_actions._mc_guard({"action": "start_session"})["detail"]
        self.assertIn("mine ore, and only in a single-player world", detail)
        self.assertIn("only for ore the scan lists", detail)
        readme = (REPO_ROOT / "docs" / "minecraft" / "README.md").read_text(
            encoding="utf-8")
        start = readme.index("**What it deliberately cannot do.**")
        self.assertIn("mine_ore", readme[start:start + 3000])
        self.assertIn("light_a_tunnel", skills.NOT_YET_POSSIBLE)
        self.assertNotIn("find_iron", skills.NOT_YET_POSSIBLE)
        self.assertIn("mine_ore", skills.available())
        self.assertEqual(mc_actions._TASK_NEEDS["mine_ore"],
                         ("singleplayer", "ores", "near_grid"))

    def test_the_tool_text(self):
        from actions import minecraft as mc_actions
        description = mc_actions.TOOL["description"]
        self.assertIn("mine_ore", description)
        self.assertIn("never promise ore", description.lower())
        task = mc_actions.TOOL["parameters"]["properties"]["task"]
        self.assertIn("mine_ore", task["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
