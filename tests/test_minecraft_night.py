"""
B5c: the night rule.

At dusk or at night, with hostile mobs around, the model is to warn the
user and offer a shelter (build_blueprint shelter, B4c) -- or to flee --
rather than keep working without a word. The tool description and
core/prompt.txt say so, and every task's result carries the fact it needs
to notice: a note when it is getting dark or is night and a hostile mob
is within DUSK_WATCH_RADIUS.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from minecraft import danger                                        # noqa: E402
from minecraft.state import EXACT, EntityRef, WorldState            # noqa: E402


def state(ticks, *mobs, dimension="overworld"):
    return WorldState(position=(0.5, 64.0, 0.5), time_of_day=ticks,
                      dimension=dimension,
                      nearby_entities=tuple(
                          EntityRef(name=n, distance=d, hostile=True,
                                    category="hostile",
                                    position=(d, 64.0, 0.5))
                          for n, d in mobs),
                      source="bridge", confidence=EXACT)


class DuskNoteTests(unittest.TestCase):

    def test_dusk_with_a_zombie_about(self):
        note = danger.dusk_note(state(12500, ("zombie", 14.0)))
        self.assertIn("getting dark", note)
        self.assertIn("zombie", note)
        self.assertIn("shelter", note)

    def test_night(self):
        self.assertIn("night", danger.dusk_note(state(18000,
                                                      ("skeleton", 9.0))))

    def test_daytime_is_quiet(self):
        self.assertIsNone(danger.dusk_note(state(6000, ("zombie", 4.0))))

    def test_no_hostile_near_is_quiet(self):
        self.assertIsNone(danger.dusk_note(state(15000)))
        self.assertIsNone(danger.dusk_note(state(15000, ("zombie", 40.0))))

    def test_an_unknown_time_is_quiet(self):
        self.assertIsNone(danger.dusk_note(state(None, ("zombie", 4.0))))


class DimensionTests(unittest.TestCase):
    """Item 4: only the overworld has days and nights. The world clock runs
    on in the Nether and the End, so a time of 15000 there said "it is
    night" to a player who has neither."""

    def test_the_nether_has_no_night(self):
        self.assertIsNone(danger.dusk_note(
            state(15000, ("zombified_piglin", 8.0), dimension="the_nether")))

    def test_the_end_has_no_night(self):
        self.assertIsNone(danger.dusk_note(
            state(15000, ("enderman", 8.0), dimension="the_end")))

    def test_an_unknown_dimension_is_quiet(self):
        """Not knowing where the player is, is not knowing it is night."""
        self.assertIsNone(danger.dusk_note(
            state(15000, ("zombie", 8.0), dimension=None)))

    def test_the_overworld_still_warns(self):
        self.assertIn("night", danger.dusk_note(
            state(15000, ("zombie", 8.0))))

    def test_look_around_says_no_night_in_the_nether(self):
        from actions import minecraft as mc_actions
        bits = mc_actions._player_bits(state(15000, dimension="the_nether"))
        self.assertFalse(any("night" in b or "dark" in b for b in bits),
                         bits)
        bits = mc_actions._player_bits(state(15000))
        self.assertTrue(any("night" in b for b in bits), bits)


class TaskResultTests(unittest.TestCase):

    def test_a_task_ending_at_dusk_says_so(self):
        from minecraft import skills
        from tests.support.mob_world import MobWorld, zombie
        from tests.support.sim_world import run
        import dataclasses

        class Dusk(MobWorld):
            def read(self):
                return dataclasses.replace(MobWorld.read(self),
                                           time_of_day=12600,
                                           dimension="overworld")
        world = Dusk([zombie(14.5, 0.5)])
        result = run(world, skills.create("walk_forward", seconds=0.5),
                     max_steps=5)
        self.assertIn("getting dark", result.reason)
        self.assertIn("shelter", result.reason)


class WordingTests(unittest.TestCase):

    def test_the_tool_description(self):
        from actions import minecraft as mc_actions
        text = mc_actions.TOOL["description"]
        self.assertIn("DUSK", text)
        self.assertIn("build_blueprint shelter", text)

    def test_the_prompt(self):
        prompt = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
        start = prompt.index("[MINECRAFT]")
        section = prompt[start:prompt.index("[", start + 1)]
        self.assertIn("dusk", section)
        self.assertIn("shelter", section)


if __name__ == "__main__":
    unittest.main(verbosity=2)
