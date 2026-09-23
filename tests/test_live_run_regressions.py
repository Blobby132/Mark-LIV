"""
Regressions from the first real run after background tasks landed.

What happened in the game, from the user's log:

  * "Find the nearest tree and break it" ran collect_logs with count=1. It
    broke a log -- the mine let go at 3.1s, when the block changed -- but the
    drop landed out of pickup range, the step was judged on the INVENTORY,
    and so it counted zero and broke the next log up. And the next. Three
    logs broken for one asked, and the task reported failure.
  * It then stood at the trunk, "arrived" by the walker's measure and "out of
    reach" by the skill's own (a different distance, from the feet), and
    spent six steps on filler looks until the progress monitor stopped it.
  * The monitor blamed "the F3 overlay and OCR" -- with the bridge mod
    reading the game perfectly well -- and JARVIS told the user it had
    "lost visibility".
  * The voice watchdog reported VOICE_PIPELINE_LOST_INPUT for utterances
    that were in fact answered: it timed a long sentence from its START
    (the transcript arrives at the END of the turn), and let loud audio
    while JARVIS itself was talking count as the user speaking.

Each test below reproduces one of those in simulation.
"""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.voice_diagnostics import (                                # noqa: E402
    LOST_INPUT_AFTER_S, SPEECH_FRAMES, VoiceDiagnostics,
)
from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft import verification as verify_mod                    # noqa: E402
from minecraft.progress import BLIND_AFTER, ProgressMonitor         # noqa: E402
from minecraft.state import EntityRef, NearbyBlock                  # noqa: E402

from tests.test_minecraft_navigation import TreeWorld, flat, run    # noqa: E402


def trunk(x=5, z=0, bottom=64, height=4):
    return [NearbyBlock(x, y, z, "oak_log", True)
            for y in range(bottom, bottom + height)]


class DropWorld(TreeWorld):
    """TreeWorld where a broken log DROPS, as in the real game.

    TreeWorld puts a broken log straight into the inventory. Minecraft does
    not: it spawns an item where the block was, which lands beside the trunk,
    and the player collects it only by walking within about a block of it.
    The item is reported the way the bridge reports it -- an entity of
    category "item" with a position."""

    PICKUP = 1.425
    """Minecraft collects items whose box meets the player's box grown by one
    block sideways: about 1.4 blocks either side, per axis -- a box, not a
    circle."""

    def __init__(self, *args, drop_under_trunk=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.drops = []
        self.drop_under_trunk = drop_under_trunk

    def mine(self, params):
        before = dict(self.inventory)
        broke_before = len(self.broken)
        result = TreeWorld.mine(self, params)
        if len(self.broken) > broke_before:
            log = self.broken[-1]
            self.inventory = before                 # not in the bag yet...
            if self.drop_under_trunk:
                # ...it fell into the gap it left, under the rest of the tree.
                self.drops.append((log.x + 0.5, 64.0, log.z + 0.5))
            else:
                # ...it fell to the ground on the far side of the trunk.
                self.drops.append((log.x + 1.5, 64.0, log.z + 0.5))
        self._collect()
        return result

    def _collect(self):
        for drop in list(self.drops):
            if abs(drop[0] - self.x) <= self.PICKUP \
                    and abs(drop[2] - self.z) <= self.PICKUP:
                self.drops.remove(drop)
                self.inventory["oak_log"] = self.inventory.get("oak_log", 0) + 1

    def _walk(self, distance, jumping):
        TreeWorld._walk(self, distance, jumping)
        self._collect()

    def read(self):
        self._collect()
        state = TreeWorld.read(self)
        items = tuple(
            EntityRef(name="item", category="item", hostile=False,
                      position=drop,
                      distance=math.dist(drop, (self.x, self.y, self.z)))
            for drop in self.drops)
        return dataclasses.replace(state, nearby_entities=items)


class CollectOneLogTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def test_one_log_asked_is_one_log_broken_and_then_picked_up(self):
        world = DropWorld(flat(), trunk(height=4), inventory={"oak_log": 0})
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=40)

        self.assertEqual(len(world.broken), 1,
                         f"asked for one log, broke {len(world.broken)}")
        self.assertEqual(world.inventory["oak_log"], 1,
                         f"the drop was never picked up: {skill.done_reason}")
        self.assertEqual(world.drops, [])
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertIn("collected 1 of 1", skill.done_reason)
        mines = [r for r in result.records if r.step["action"] == "mine"]
        self.assertEqual(mines[0].verification["status"], verify_mod.SUCCESS,
                         "the mine that broke the log must be judged a "
                         "success -- it did its job; the pickup is separate")

    def test_a_drop_under_the_rest_of_the_trunk_is_fetched_from_beside_it(self):
        """The case the real game produces most: the bottom log breaks, its
        drop sits in the trunk's own column under the logs still standing, and
        the map -- like the bridge's -- shows that column as the top of the
        tree, which nobody can walk into."""
        logs = trunk(height=4)                       # y 64..67 at (5, 0)
        surface = [b for b in flat() if (b.x, b.z) != (5, 0)]
        surface.append(NearbyBlock(5, 67, 0, "oak_log", True))
        world = DropWorld(surface, logs, inventory={"oak_log": 0},
                          drop_under_trunk=True)
        # Three blocks from the trunk: close enough to mine from where it
        # stands -- as in the real run -- and too far to pick anything up.
        world.x, world.z = 2.5, 0.5
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=40)

        actions = [r.step["action"] for r in result.records]
        mined_at = actions.index("mine")
        self.assertIn("move", actions[mined_at:],
                      "it never walked to the drop, so this test would not "
                      "notice the pickup path breaking")
        self.assertEqual(len(world.broken), 1)
        self.assertEqual(world.inventory["oak_log"], 1, skill.done_reason)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertNotEqual((math.floor(world.x), math.floor(world.z)),
                            (5, 0), "it walked into the trunk")

    def test_a_drop_that_cannot_be_fetched_does_not_start_more_breaking(self):
        class LostDrops(DropWorld):
            def read(self):
                self.drops.clear()          # fell in lava, despawned...
                return DropWorld.read(self)

        world = LostDrops(flat(), trunk(height=4), inventory={"oak_log": 0})
        skill = skills.create("collect_logs", count=1)
        run(world, skill, max_steps=40)

        self.assertEqual(len(world.broken), 1)
        self.assertTrue(skill.failed, "nothing reached the inventory")
        self.assertIn("broke 1 and collected 0 of 1", skill.done_reason)
        self.assertIn("cannot see the rest", skill.done_reason)


class OutOfReachTests(unittest.TestCase):

    def test_a_log_too_high_to_hit_is_skipped_not_circled(self):
        """Only log is six blocks up: reachable to walk under, never to hit.
        It must say so, promptly, without filler steps."""
        aiming_mod.SHARED.reset()
        world = TreeWorld(flat(), [NearbyBlock(5, 70, 0, "oak_log", True)])
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=40)

        self.assertEqual(world.swings, 0)
        self.assertTrue(skill.failed)
        self.assertIn("out of reach", skill.done_reason)
        notes = [r.step.get("note", "") for r in result.records]
        self.assertFalse([n for n in notes if "arrived at the tree" in n],
                         "filler looks are back")
        self.assertNotIn("stuck", result.reason)
        self.assertNotIn("F3", result.reason)


class ProgressMessageTests(unittest.TestCase):

    def test_unchecked_steps_are_called_a_loop_not_blindness(self):
        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER):
            monitor.record("look", verify_mod.unverifiable(
                "arrived", "no check exists for this action yet.").as_dict())
        text = monitor.explain()
        self.assertIn("loop", text)
        self.assertNotIn("F3", text)
        self.assertNotIn("OCR", text)

    def test_real_blindness_names_the_fields_it_could_not_read(self):
        from minecraft.state import empty_state
        payload = verify_mod.turned().check(empty_state(), empty_state(),
                                            True).as_dict()
        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER):
            monitor.record("look", payload)
        text = monitor.explain()
        self.assertIn("rotation", text)
        self.assertIn("bridge", text)


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class VoiceWatchdogFalseAlarmTests(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def loud(self, seconds, gate=None):
        """Loud frames at the real 64 ms cadence, queued or gated."""
        for _ in range(int(seconds / 0.064)):
            self.diag.frame_captured(0.5)
            if gate:
                self.diag.frame_gated(gate)
            else:
                self.diag.frame_queued()
                self.diag.frame_sent()
            self.clock.advance(0.064)

    def watch(self, seconds):
        found = []
        for _ in range(int(seconds / 0.5)):
            self.clock.advance(0.5)
            problem = self.diag.check_for_loss()
            if problem:
                found.append(problem)
        return found

    def test_a_long_sentence_is_not_lost_while_its_transcript_is_coming(self):
        """The server sends the transcript at the END of the turn. A five
        second sentence used to be declared lost one second after it ended."""
        self.loud(5.0)
        found = self.watch(1.5)
        self.diag.input_transcript("I confirmed. Can you look around and "
                                   "find the nearest tree and break it?")
        found += self.watch(LOST_INPUT_AFTER_S + 2)
        self.assertEqual(found, [])
        self.assertEqual(self.diag.snapshot()["lost_utterances"], 0)

    def test_jarvis_talking_is_not_the_user_talking(self):
        self.loud(4.0, gate="speaking")
        self.assertEqual(self.watch(LOST_INPUT_AFTER_S + 2), [])
        self.assertFalse(any("speech_at_mic" in line
                             for line in self.diag.timeline()))

    def test_sound_that_was_answered_was_not_lost(self):
        self.loud(1.0)
        self.diag.response_started()      # JARVIS answered something
        self.assertEqual(self.watch(LOST_INPUT_AFTER_S + 2), [])

    def test_unanswered_loud_sound_is_reported_without_claiming_it_was_speech(self):
        for _ in range(SPEECH_FRAMES + 4):
            self.diag.frame_captured(0.5)
            self.diag.frame_queued()
            self.diag.frame_sent()
        found = self.watch(LOST_INPUT_AFTER_S + 1)
        self.assertEqual(len(found), 1)
        self.assertEqual(self.diag.last_loss_kind, "no_transcript")
        self.assertIn("not speech", found[0])
        self.assertIn("game audio", found[0])
        self.assertIn("no_transcript=1", self.diag.line())


if __name__ == "__main__":
    unittest.main(verbosity=2)
