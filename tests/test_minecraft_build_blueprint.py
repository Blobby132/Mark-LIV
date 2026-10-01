"""
B4c: build_blueprint -- a platform, a wall, or a hollow 3x3x3 shelter with
a doorway, built bottom-up from proven single placements.

Hard limits: at most 64 blocks; every cell within 6 blocks of where the task
began; only into air or plants the game replaces; only plain building
blocks. The plan is one sentence (the task's goal, which run_task's
'started' answer repeats). The report says exactly which cells were placed
and which failed, counted from the inventory. A task has 45 steps, so a
blueprint that needs more is carried on by asking again.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import building                                      # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.skills import place_build as skills_place_build          # noqa: E402
from minecraft.state import NearbyBlock                             # noqa: E402
from minecraft.task_runner import TaskRunner                        # noqa: E402
from tests.build_world import BuildWorld                            # noqa: E402


def build(world, **options):
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    options.setdefault("item", "cobblestone")
    skill = skills.create("build_blueprint", **options)
    result = TaskRunner(world, world, sleeper=lambda _s: None).run(
        skill, max_steps=45)
    return skill, result


class PlanTests(unittest.TestCase):

    def test_every_plan_is_buildable_bottom_up_and_bounded(self):
        for plan in building.PLANS:
            for facing in ("north", "south", "east", "west"):
                with self.subTest(plan=plan, facing=facing):
                    bp = building.blueprint(plan, (0, 64, 3), facing)
                    self.assertLessEqual(len(bp.cells),
                                         building.MAX_BLUEPRINT_BLOCKS)
                    self.assertEqual(len(set(bp.cells)), len(bp.cells))
                    self.assertTrue(building.supported_in_order(bp.cells, 63))
                    self.assertEqual(building.within(bp.cells, (0, 64, 0)),
                                     ())
                    layers = [c[1] for c in bp.cells]
                    self.assertEqual(layers, sorted(layers), "bottom-up")

    def test_the_shelter(self):
        bp = building.blueprint("shelter", (0, 64, 0), "south")
        cells = set(bp.cells)
        self.assertEqual(len(cells), 26)
        self.assertNotIn((0, 64, 0), cells, "hollow")
        self.assertNotIn((0, 65, 0), cells, "room to stand inside")
        self.assertNotIn((0, 64, 1), cells, "doorway")
        self.assertNotIn((0, 65, 1), cells, "doorway")
        self.assertIn((0, 66, 1), cells, "a roof over the door")
        self.assertEqual(sum(1 for c in cells if c[1] == 66), 9)
        self.assertTrue({(2, 64, -1), (2, 64, 0), (2, 64, 1)} <= cells,
                        "the step along the east wall")
        self.assertIn("doorway on the south side", bp.sentence)
        self.assertNotIn(".", bp.sentence.rstrip("."), "one sentence")

    def test_the_64_block_cap_holds_whatever_the_sizes_allow(self):
        from unittest import mock
        with mock.patch.object(building, "PLATFORM_SIZES", range(2, 10)):
            answer = building.blueprint("platform", (0, 64, 0), size=9)
        self.assertIsInstance(answer, str)
        self.assertIn("64", answer)

    def test_every_orientation_of_the_shelter_finishes(self):
        """In the simulation: each way the doorway can face, from a start
        three blocks off, asking again until it is done."""
        for x, z, facing in ((0, 3, "north"), (0, -3, "south"),
                             (3, 0, "west"), (-3, 0, "east"),
                             (0, 3, "south"), (0, 3, "east"),
                             (0, 3, "west")):
            with self.subTest(facing=facing, at=(x, z)):
                skills_place_build.forget_blueprints()
                world = BuildWorld({0: ("cobblestone", 40)})
                for _attempt in range(5):
                    skill, _ = build(world, plan="shelter", x=x, y=64, z=z,
                                     direction=facing)
                    if not skill.failed:
                        break
                self.assertFalse(skill.failed, skill.done_reason)
                self.assertEqual(len(world.placed), 26)

    def test_unknown_plans_and_sizes(self):
        self.assertIsInstance(building.blueprint("castle", (0, 64, 0)), str)
        self.assertIsInstance(building.blueprint("platform", (0, 64, 0),
                                                 size=9), str)
        self.assertIsInstance(building.blueprint("wall", (0, 64, 0),
                                                 facing="up"), str)


class BuildTests(unittest.TestCase):

    def setUp(self):
        skills_place_build.forget_blueprints()

    def test_a_platform(self):
        world = BuildWorld({0: ("cobblestone", 20)})
        skill, result = build(world, plan="platform", x=0, y=64, z=3)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual({c for c, _ in world.placed},
                         set(building.blueprint("platform", (0, 64, 3)).cells))
        self.assertIn("cobblestone 20 → 11", skill.done_reason)
        self.assertIn("all 9", skill.done_reason)

    def test_a_wall(self):
        world = BuildWorld({0: ("oak_planks", 20)})
        skill, result = build(world, plan="wall", x=0, y=64, z=3,
                              item="oak_planks")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(len(world.placed), 10)

    def test_a_shelter_over_more_than_one_task(self):
        world = BuildWorld({0: ("cobblestone", 30)})
        plan = set(building.blueprint("shelter", (0, 64, 3), "north").cells)
        reports = []
        for _attempt in range(4):
            skill, result = build(world, plan="shelter", x=0, y=64, z=3,
                                  direction="north")
            reports.append(skill.done_reason)
            if not skill.failed:
                break
            self.assertIn("ask again", skill.done_reason.lower())
        self.assertFalse(skill.failed, "\n".join(reports))
        self.assertEqual({c for c, _ in world.placed}, plan)
        self.assertEqual(len(world.placed), len(plan), "a cell placed twice")
        self.assertEqual(world.count("cobblestone"), 30 - 26)
        self.assertIn("all 26", reports[-1])
        for report in reports:
            self.assertIn("Counted in the inventory", report)

    def test_a_block_already_in_place_is_counted_not_refused(self):
        """Between two tasks a cell of the plan got its block -- the user
        put it there, or a press the first task could not prove. Asking
        again counts it as found, says so, and does not place it twice."""
        world = BuildWorld({0: ("cobblestone", 30)})
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create("build_blueprint", plan="wall", x=0, y=64, z=3,
                              item="cobblestone")
        TaskRunner(world, world, sleeper=lambda _s: None).run(skill,
                                                              max_steps=6)
        placed = {c for c, _ in world.placed}
        bp = building.blueprint("wall", (0, 64, 3), "north")
        spare = next(c for c in bp.cells if c not in placed and c[1] == 64)
        world.blocks[spare] = NearbyBlock(*spare, "cobblestone", True)
        skill, _ = build(world, plan="wall")
        self.assertIn("Already in place", skill.done_reason)
        self.assertIn(str(spare), skill.done_reason)
        self.assertNotIn(spare, [c for c, _ in world.placed])

    def test_a_finished_build_is_checked_block_by_block(self):
        world = BuildWorld({0: ("cobblestone", 20)})
        skill, _ = build(world, plan="platform", x=0, y=64, z=3)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertIn("Checked against the game's own list", skill.done_reason)
        self.assertIn("9 of 9 confirmed", skill.done_reason)

    def test_a_block_gone_since_is_found_and_put_back_on_the_list(self):
        world = BuildWorld({0: ("cobblestone", 20)})
        original = world.place

        def then_broken(params):
            answer = original(params)
            if len(world.placed) == 9:           # someone breaks the first
                del world.blocks[world.placed[0][0]]
            return answer
        world.place = then_broken
        skill, _ = build(world, plan="platform", x=0, y=64, z=3)
        gone = world.placed[0][0]
        self.assertTrue(skill.failed)
        self.assertIn(f"{gone} holds air", skill.done_reason)
        self.assertIn("Still to place", skill.done_reason)

    def test_without_the_snapshot_it_says_it_did_not_check(self):
        world = BuildWorld({0: ("cobblestone", 20)})
        world.report_near = False
        skill, _ = build(world, plan="platform", x=0, y=64, z=3)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertIn("Not checked block by block", skill.done_reason)

    def test_without_coordinates_it_builds_in_front(self):
        world = BuildWorld({0: ("cobblestone", 20)}, yaw=0.0)  # facing +z
        skill, result = build(world, plan="platform")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertTrue(all(c[2] > 0 for c, _ in world.placed),
                        "not in front of the player")


class RefusalTests(unittest.TestCase):

    def setUp(self):
        skills_place_build.forget_blueprints()

    def assertRefused(self, world, *words, **options):
        skill, result = build(world, **options)
        self.assertTrue(skill.failed)
        self.assertEqual(result.steps_taken, 0, "refused only after acting")
        self.assertEqual(world.placed, [])
        for word in words:
            self.assertIn(word, skill.done_reason)

    def test_too_far_from_where_it_began(self):
        self.assertRefused(BuildWorld({0: ("cobblestone", 30)}), "6 blocks",
                           plan="platform", x=0, y=64, z=7)

    def test_not_enough_blocks_for_the_whole_plan(self):
        self.assertRefused(BuildWorld({0: ("cobblestone", 10)}), "26",
                           plan="shelter", x=0, y=64, z=3)

    def test_something_in_the_way(self):
        world = BuildWorld({0: ("cobblestone", 30)})
        world.logs.append(NearbyBlock(1, 64, 3, "oak_log", True))
        self.assertRefused(world, "oak log", plan="platform", x=0, y=64, z=3)

    def test_uneven_ground(self):
        world = BuildWorld({0: ("cobblestone", 30)},
                           placed=(((1, 64, 3), "dirt"),))
        self.assertRefused(world, "flat", plan="platform", x=0, y=65, z=3)

    def test_not_a_building_block(self):
        self.assertRefused(BuildWorld({0: ("sand", 30)}), "plain blocks",
                           plan="platform", x=0, y=64, z=3, item="sand")

    def test_an_unknown_plan(self):
        self.assertRefused(BuildWorld({0: ("cobblestone", 30)}), "shelter",
                           plan="castle", x=0, y=64, z=3)


class WordingTests(unittest.TestCase):

    def test_the_goal_is_the_one_sentence_plan(self):
        skill = skills.create("build_blueprint", plan="shelter", x=0, y=64,
                              z=3, item="cobblestone", direction="north")
        self.assertIn("shelter", skill.goal)
        self.assertIn("cobblestone", skill.goal)

    def test_it_is_described_and_no_longer_impossible(self):
        from actions import minecraft as mc_actions
        text = mc_actions.TOOL["description"]
        self.assertIn("build_blueprint", text)
        self.assertIn("one sentence", text)
        self.assertIn("build_blueprint", skills.available())
        self.assertNotIn("build_structure", skills.NOT_YET_POSSIBLE)
        prompt = (Path(__file__).resolve().parent.parent / "core"
                  / "prompt.txt").read_text(encoding="utf-8")
        self.assertIn("build_blueprint", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
