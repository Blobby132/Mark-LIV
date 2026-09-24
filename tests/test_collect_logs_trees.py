"""
One tree at a time, and saying which trees.

From the fifth real run, the first with the new bridge mod: "mine the rest
of the tree" ran collect_logs with a guessed count of five. It broke the
last low log of that tree, walked over to pick it up -- and from there a
different oak was a little nearer, so the next three logs came off that
one, and the fifth off a birch. The user asked "why are you mining
different trees?", and the assistant, with nothing in the task report to go
on, said it was the same tree.

So: logs are grouped into trees, collect_logs finishes the tree it started
before moving on, fell_tree takes one whole tree and stops, and the report
says which tree every log came from.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft import verification as verify_mod                   # noqa: E402
from minecraft.state import BlockRef, EXACT, NearbyBlock, WorldState  # noqa: E402

from tests.test_live_run_regressions import DropWorld               # noqa: E402
from tests.test_minecraft_navigation import flat, run               # noqa: E402


def trunk(x, z, bottom=64, top=67, name="oak_log"):
    return [NearbyBlock(x, y, z, name, True) for y in range(bottom, top + 1)]


def from_tree(blocks, column):
    return [b for b in blocks if (b.x, b.z) == column]


class TreeGroupingTests(unittest.TestCase):

    def state(self, logs):
        return WorldState(position=(0.5, 64.0, 0.5), notable_blocks=tuple(logs),
                          source="test", confidence=EXACT)

    def test_a_trunk_and_its_branches_are_one_tree(self):
        logs = trunk(5, 0) + [NearbyBlock(6, 68, 1, "oak_log", True)]
        tree = nav.tree_logs(self.state(logs), {(5, 64, 0)})
        self.assertEqual(len(tree), 5, "the diagonal branch was left out")

    def test_an_oak_against_a_birch_is_two_trees(self):
        logs = trunk(5, 0) + trunk(6, 0, name="birch_log")
        tree = nav.tree_logs(self.state(logs), {(5, 64, 0)})
        self.assertEqual({b.name for b in tree}, {"oak_log"})

    def test_a_trunk_with_its_bottom_log_gone_is_found_from_above(self):
        logs = trunk(5, 0, bottom=65)
        tree = nav.tree_logs(self.state(logs), {(5, 64, 0), (5, 65, 0)})
        self.assertEqual(len(tree), 3)


class OneTreeAtATimeTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def two_trees(self):
        """Tree A beside the player; tree B three blocks round from it, so
        that walking to A's drops leaves B's low logs nearer than A's high
        ones -- the situation from the run."""
        a, b = trunk(4, 0), trunk(4, 3)
        world = DropWorld(flat(), a + b, inventory={"oak_log": 0})
        world.x, world.z = 2.5, 0.5
        return world, a, b

    def test_it_finishes_the_tree_it_started_before_the_next(self):
        world, a, b = self.two_trees()
        skill = skills.create("collect_logs", count=5)
        run(world, skill, max_steps=45)
        order = [(log.x, log.z) for log in world.broken]
        self.assertGreaterEqual(len(order), 5, skill.done_reason)
        self.assertEqual(order[:4], [(4, 0)] * 4,
                         f"it switched trees part-way: {order}")
        self.assertEqual(order[4], (4, 3))

    def test_the_report_says_which_trees(self):
        world, a, b = self.two_trees()
        skill = skills.create("collect_logs", count=5)
        run(world, skill, max_steps=45)
        self.assertIn("4 from the oak tree at (4, 0)", skill.done_reason)
        self.assertIn("1 from the oak tree at (4, 3)", skill.done_reason)

    def test_one_tree_says_so(self):
        world = DropWorld(flat(), trunk(5, 0), inventory={"oak_log": 0})
        skill = skills.create("collect_logs", count=2)
        run(world, skill, max_steps=45)
        self.assertIn("all from the oak tree at (5, 0)", skill.done_reason)

    def test_another_trees_log_under_the_crosshair_is_not_taken(self):
        """Mid-tree, a log of the next tree in reach under the crosshair is
        not "the log in front of me" -- the rest of this tree is."""
        skill = skills.create("collect_logs", count=5)
        skill._adopt(tuple(trunk(4, 0)))
        state = WorldState(
            position=(3.5, 64.0, 2.5), rotation=(0.0, 0.0),
            target_block=BlockRef(name="oak_log", x=4, y=65, z=3),
            notable_blocks=tuple(trunk(4, 0) + trunk(4, 3)),
            source="test", confidence=EXACT)
        self.assertIsNone(skill._log_under_crosshair(state))


class FellTreeTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def test_it_takes_one_whole_tree_and_stops(self):
        tall = trunk(4, 0, top=71)            # the top ones are out of reach
        other = trunk(4, 4)
        world = DropWorld(flat(), tall + other, inventory={"oak_log": 0})
        world.x, world.z = 2.5, 0.5
        skill = skills.create("fell_tree")
        result = run(world, skill, max_steps=45)

        self.assertFalse(skill.failed, skill.done_reason)
        self.assertTrue(world.broken)
        self.assertEqual({(b.x, b.z) for b in world.broken}, {(4, 0)},
                         "it went on to another tree")
        self.assertEqual(len(world.logs), len(other) + (8 - len(world.broken)))
        self.assertEqual(world.inventory["oak_log"], len(world.broken),
                         "it did not pick up what it felled")
        self.assertIn(f"broke {len(world.broken)} log(s) from the oak tree "
                      f"at (4, 0)", skill.done_reason)
        left = [b for b in world.logs if (b.x, b.z) == (4, 0)]
        if left:
            self.assertIn(f"{len(left)} more log(s) of it are still standing",
                          skill.done_reason)
        self.assertEqual(result.status, "completed")

    def test_without_the_scan_it_says_it_cannot_tell_trees_apart(self):
        state = WorldState(target_block=BlockRef(name="oak_log", x=1, y=64,
                                                 z=0),
                           source="f3", confidence=EXACT)
        skill = skills.create("fell_tree")
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertTrue(skill.failed)
        self.assertIn("terrain scan", skill.done_reason)


class PickupCheckTests(unittest.TestCase):
    """From the same run: the pickup walk collected an oak log on the way,
    then the "standing where the dropped log is" check compared the bag
    against the moment AFTER that, counted only birch, and called it
    failed."""

    def setUp(self):
        aiming_mod.SHARED.reset()

    def test_a_drop_picked_up_on_the_way_is_not_a_failed_check(self):
        class SpreadDrops(DropWorld):
            """Two drops, three blocks apart, neither within reach of where
            the logs were mined from: the first is collected on the walk,
            and the second is still waiting -- as in the run."""
            SPOTS = [(6.5, 64.0, 0.5), (4.5, 64.0, 3.5)]

            def mine(self, params):
                broke = len(self.broken)
                result = DropWorld.mine(self, params)
                if len(self.broken) > broke and self.drops:
                    # Move the drop just made to its own spot.
                    self.drops[-1] = self.SPOTS[(len(self.broken) - 1)
                                                % len(self.SPOTS)]
                return result

        world = SpreadDrops(flat(), trunk(5, 0), inventory={"oak_log": 0})
        world.x, world.z = 2.5, 0.5
        skill = skills.create("collect_logs", count=2)
        result = run(world, skill, max_steps=45)
        self.assertEqual(world.inventory["oak_log"], 2, skill.done_reason)
        failed_checks = [
            r.step["note"] for r in result.records
            if r.step.get("note", "").startswith("standing where")
            and r.verification.get("status") == verify_mod.FAILED]
        self.assertEqual(failed_checks, [])

    def test_the_check_counts_any_kind_of_log(self):
        check = verify_mod.collected(skills.LOG_BLOCKS, label="logs")
        from minecraft.state import ItemStack
        before = WorldState(inventory=(ItemStack(slot=0, name="birch_log",
                                                 count=1),),
                            source="test", confidence=EXACT)
        after = WorldState(inventory=(ItemStack(slot=0, name="birch_log",
                                                count=1),
                                      ItemStack(slot=1, name="oak_log",
                                                count=1)),
                           source="test", confidence=EXACT)
        self.assertEqual(check.check(before, after).status, verify_mod.SUCCESS)


class ToolTextTests(unittest.TestCase):

    def test_every_task_is_listed_for_the_assistant(self):
        """A task the model is never told about is a task it replaces with
        a guess -- as "collect five logs" stood in for "the rest of the
        tree"."""
        from actions import minecraft as mc_actions
        listed = mc_actions.TOOL["parameters"]["properties"]["task"][
            "description"]
        for name in skills.available():
            self.assertIn(name, listed)
            self.assertIn(name, mc_actions.TOOL["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
