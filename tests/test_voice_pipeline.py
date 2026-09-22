"""
Tests for core/voice_diagnostics.py and the audio path's loss accounting.

WHAT THESE ARE FOR
    One reported symptom: "the sound bars moved and JARVIS did nothing."
    The bars are drawn in the microphone callback, which runs before every
    gate and before the queue, so they prove the microphone works and nothing
    else. Between there and a reply an utterance can die in at least four
    places, and until now all four looked identical.

    These tests pin the accounting that tells them apart, and the one rule
    that matters most: a dropped frame is never silent.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.voice_diagnostics import (                                  # noqa: E402
    HEARD_IGNORED, LOST_INPUT, LOST_INPUT_AFTER_S, SPEECH_FRAMES,
    UNANSWERED_AFTER_S, VoiceDiagnostics,
)


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now
    def advance(self, seconds): self.now += seconds


def speaking(diag, frames=SPEECH_FRAMES + 2, level=0.5):
    for _ in range(frames):
        diag.frame_captured(level)


class CountingTests(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def test_a_dropped_frame_is_never_silent(self):
        """The whole point. `put_nowait` on a full queue used to raise inside
        a loop callback where nobody saw it, and the level meter kept
        bouncing regardless."""
        self.diag.frame_captured(0.4)
        self.diag.frame_dropped()
        self.assertEqual(self.diag.snapshot()["dropped_queue_full"], 1)
        self.assertIn("1 DROPPED", self.diag.describe())

    def test_every_gate_is_named_separately(self):
        """"It was gated" is not an answer — asleep, muted and push-to-talk
        have completely different fixes."""
        for gate in ("asleep", "speaking", "echo", "ptt", "muted"):
            self.diag.frame_gated(gate)
        gated = self.diag.snapshot()["gated"]
        self.assertEqual(sorted(gated.values()), [1, 1, 1, 1, 1])
        self.assertEqual(len(gated), 5)

    def test_an_unknown_gate_name_is_ignored_not_counted_wrongly(self):
        self.diag.frame_gated("something_else")
        self.assertEqual(sum(self.diag.snapshot()["gated"].values()), 0)

    def test_capture_is_counted_before_any_gate(self):
        """`captured` must mean what the device handed us, because that is
        what the level meter shows. If it were counted after the gates,
        "captured" and "the bars moved" would stop agreeing."""
        speaking(self.diag, frames=5)
        self.assertEqual(self.diag.snapshot()["captured"], 5)

    def test_the_queue_high_water_mark_is_kept(self):
        self.diag.frame_sent(depth=3)
        self.diag.frame_sent(depth=17)
        self.diag.frame_sent(depth=2)
        self.assertEqual(self.diag.snapshot()["queue_high_water"], 17)


class WatchdogTests(unittest.TestCase):
    """Which stage ate the utterance."""

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def test_nothing_is_reported_while_the_answer_could_still_arrive(self):
        speaking(self.diag)
        self.clock.advance(LOST_INPUT_AFTER_S - 0.5)
        self.assertEqual(self.diag.check_for_loss(), "")

    def test_a_transcript_closes_the_utterance(self):
        speaking(self.diag)
        self.diag.input_transcript("jarvis mine some wood")
        self.clock.advance(LOST_INPUT_AFTER_S * 3)
        self.assertEqual(self.diag.check_for_loss(), "")
        self.assertEqual(self.diag.snapshot()["lost_utterances"], 0)

    def test_frames_dropped_by_the_queue_are_blamed_on_the_queue(self):
        speaking(self.diag)
        self.diag.frame_dropped()
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        lost = self.diag.check_for_loss()
        self.assertIn(LOST_INPUT, lost)
        self.assertIn("DROPPED", lost)
        self.assertIn("never left this machine", lost)

    def test_frames_stopped_by_a_gate_are_blamed_on_the_gate(self):
        speaking(self.diag)
        for _ in range(5):
            self.diag.frame_gated("echo")
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        lost = self.diag.check_for_loss()
        self.assertIn("a gate swallowed it", lost)
        self.assertIn("echo tail 5", lost)

    def test_frames_queued_but_never_sent_are_blamed_on_the_socket(self):
        speaking(self.diag)
        for _ in range(5):
            self.diag.frame_queued()
        self.diag.send_error()
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        lost = self.diag.check_for_loss()
        self.assertIn("NONE", lost)
        self.assertIn("send loop is stuck", lost)

    def test_frames_that_reached_gemini_are_blamed_on_gemini(self):
        """The audio arrived and no transcript came back. Different problem,
        different fix, and it used to be indistinguishable from the rest."""
        speaking(self.diag)
        for _ in range(5):
            self.diag.frame_queued()
            self.diag.frame_sent()
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        lost = self.diag.check_for_loss()
        self.assertIn("reached Gemini", lost)
        self.assertIn("proactive audio", lost)

    def test_it_reports_a_lost_utterance_once_not_every_second(self):
        speaking(self.diag)
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        self.assertNotEqual(self.diag.check_for_loss(), "")
        self.assertEqual(self.diag.check_for_loss(), "")
        self.assertEqual(self.diag.snapshot()["lost_utterances"], 1)

    def test_a_reconnect_does_not_blame_the_server_for_a_dead_socket(self):
        speaking(self.diag)
        self.diag.reconnected()
        self.clock.advance(LOST_INPUT_AFTER_S + 1)
        self.assertEqual(self.diag.check_for_loss(), "")

    def test_quiet_room_tone_is_not_an_utterance(self):
        """The watchdog must not cry wolf at a fan or a keystroke."""
        for _ in range(50):
            self.diag.frame_captured(0.01)
        self.clock.advance(LOST_INPUT_AFTER_S * 2)
        self.assertEqual(self.diag.check_for_loss(), "")

    def test_one_loud_blip_is_not_an_utterance(self):
        for _ in range(SPEECH_FRAMES - 1):
            self.diag.frame_captured(0.9)
        self.clock.advance(LOST_INPUT_AFTER_S * 2)
        self.assertEqual(self.diag.check_for_loss(), "")


class HeardButIgnoredTests(unittest.TestCase):
    """The failure that survives fixing the audio path entirely."""

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def test_a_transcribed_turn_with_no_answer_is_reported(self):
        self.diag.turn_complete("jarvis mine some wood")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        problem = self.diag.check_unanswered()
        self.assertIn(HEARD_IGNORED, problem)
        self.assertIn("mine some wood", problem)
        self.assertIn("proactive_audio", problem)

    def test_an_answered_turn_is_not_reported(self):
        self.diag.turn_complete("what time is it")
        self.diag.response()
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertEqual(self.diag.check_unanswered(), "")

    def test_a_turn_answered_by_a_tool_call_is_not_reported(self):
        """A Minecraft command produces a tool call and often no speech at
        all. Calling that unanswered would report every successful command."""
        self.diag.turn_complete("jarvis walk forward")
        self.diag.tool_call()
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertEqual(self.diag.check_unanswered(), "")

    def test_a_turn_with_no_words_is_not_reported(self):
        """Turn boundaries arrive for JARVIS's own turns too."""
        self.diag.turn_complete("")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertEqual(self.diag.check_unanswered(), "")

    def test_it_cannot_retry_the_command_even_if_asked_to(self):
        """Re-sending is how one "move forward" becomes two, and in Minecraft
        that is a real action taken twice.

        Checked from the import graph, not the prose: this module holds no
        reference to the session, the queue or the audio, so there is nothing
        here that COULD replay an utterance. (Matching on the text "def send"
        was the first version of this test, and it matched `send_error`.)"""
        import ast
        import inspect
        from core import voice_diagnostics
        tree = ast.parse(inspect.getsource(voice_diagnostics))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"threading", "time", "dataclasses"},
                         "voice_diagnostics grew a way to reach the network")


class ProactiveAudioDefaultTests(unittest.TestCase):

    def test_it_is_off_by_default(self):
        """A feature that can silently discard a direct command should not be
        the default. It stays available for anyone who wants it."""
        from memory import config_manager
        real = config_manager.load_api_keys
        config_manager.load_api_keys = lambda: {}
        self.addCleanup(setattr, config_manager, "load_api_keys", real)
        self.assertFalse(config_manager.get_proactive_audio_enabled())

    def test_it_can_still_be_turned_on(self):
        from memory import config_manager
        real = config_manager.load_api_keys
        config_manager.load_api_keys = lambda: {"proactive_audio": True}
        self.addCleanup(setattr, config_manager, "load_api_keys", real)
        self.assertTrue(config_manager.get_proactive_audio_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)
