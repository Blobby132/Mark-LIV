"""
Voice diagnostics: replies counted as replies, the answered-turn false
alarm, session transitions, the one-line summary and the timeline.

The counters exist to answer "where did what I said go". These pin down that
the answer they give is true: a normal answered turn is not reported as
ignored, a reply is one reply however many audio chunks it takes, and the
timeline shows the stages in the order they happened.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.voice_diagnostics import (                                # noqa: E402
    EVENT_LOG_SIZE, HEARD_IGNORED, SPEECH_FRAMES, UNANSWERED_AFTER_S,
    VoiceDiagnostics,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ReplyCountingTests(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def test_a_reply_is_counted_once_however_many_chunks_it_has(self):
        for _ in range(40):
            self.diag.response_started()
        self.assertEqual(self.diag.snapshot()["responses"], 1)
        self.diag.generation_complete()
        self.diag.response_started()
        self.assertEqual(self.diag.snapshot()["responses"], 2)

    def test_the_old_name_still_means_a_reply_started(self):
        self.diag.response()
        self.diag.response()
        self.assertEqual(self.diag.snapshot()["responses"], 1)

    def test_completion_is_counted_by_whichever_marker_arrives(self):
        self.diag.response_started()
        self.diag.generation_complete()
        self.diag.turn_complete("")
        self.assertEqual(self.diag.snapshot()["responses_completed"], 1,
                         "generation_complete then turn_complete is ONE reply")
        self.diag.response_started()
        self.diag.server_interrupted()
        self.assertEqual(self.diag.snapshot()["responses_completed"], 2)
        self.assertEqual(self.diag.snapshot()["server_interruptions"], 1)


class AnsweredTurnFalseAlarmTests(unittest.TestCase):
    """In the Live API the reply arrives BEFORE the turn_complete that closes
    it. Judging a turn only by what came afterwards reported every ordinary
    answer as 'heard but unanswered'."""

    def setUp(self):
        self.clock = Clock()
        self.diag = VoiceDiagnostics(_clock=self.clock)

    def test_a_reply_before_turn_complete_counts_as_an_answer(self):
        self.diag.input_transcript("what time is it")
        self.diag.response_started()
        self.diag.generation_complete()
        self.diag.turn_complete("what time is it")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertEqual(self.diag.check_unanswered(), "")
        self.assertEqual(self.diag.snapshot()["unanswered_turns"], 0)

    def test_a_tool_call_before_turn_complete_counts_as_an_answer(self):
        self.diag.input_transcript("jarvis walk forward")
        self.diag.tool_call(["minecraft_control"])
        self.diag.turn_complete("jarvis walk forward")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertEqual(self.diag.check_unanswered(), "")

    def test_words_with_nothing_before_or_after_are_still_reported(self):
        self.diag.input_transcript("jarvis mine some wood")
        self.diag.turn_complete("jarvis mine some wood")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertIn(HEARD_IGNORED, self.diag.check_unanswered())

    def test_one_answer_does_not_excuse_the_next_turn(self):
        self.diag.response_started()
        self.diag.turn_complete("first question")
        self.diag.turn_complete("second question, ignored")
        self.clock.advance(UNANSWERED_AFTER_S + 1)
        self.assertIn("second question", self.diag.check_unanswered())


class ProactiveHintTests(unittest.TestCase):
    """The advice must follow how proactive audio is actually configured."""

    def unanswered_with(self, proactive):
        clock = Clock()
        diag = VoiceDiagnostics(_clock=clock)
        diag.session_started(proactive_audio=proactive)
        diag.turn_complete("jarvis do something")
        clock.advance(UNANSWERED_AFTER_S + 1)
        return diag.check_unanswered()

    def test_on(self):
        self.assertIn("ON for this session", self.unanswered_with(True))

    def test_off_says_it_is_not_the_cause(self):
        text = self.unanswered_with(False)
        self.assertIn("not the cause", text)
        self.assertNotIn("set \"proactive_audio\": false", text)


class SessionTransitionTests(unittest.TestCase):

    def test_every_connect_after_the_first_is_a_reconnect(self):
        diag = VoiceDiagnostics(_clock=Clock())
        diag.session_started(resumed=False)
        self.assertEqual(diag.snapshot()["reconnects"], 0)
        diag.session_ended("ConnectionClosed")
        diag.session_started(resumed=False)       # a FRESH reconnect counts
        diag.session_ended("reconnect requested")
        diag.session_started(resumed=True)
        s = diag.snapshot()
        self.assertEqual(s["sessions"], 3)
        self.assertEqual(s["reconnects"], 2)
        self.assertEqual(s["session_ends"], 2)
        self.assertEqual(s["last_session_end_reason"], "reconnect requested")

    def test_go_away_cancellation_and_errors_are_counted(self):
        diag = VoiceDiagnostics(_clock=Clock())
        diag.go_away("10s")
        diag.tool_call_cancelled(["a", "b"])
        diag.error("receive", RuntimeError("socket closed"))
        s = diag.snapshot()
        self.assertEqual(s["go_aways"], 1)
        self.assertEqual(s["tool_call_cancellations"], 2)
        self.assertEqual(s["receive_errors"], 1)
        self.assertIn("socket closed", s["last_error"])


class SummaryLineTests(unittest.TestCase):

    def test_the_line_reads_in_pipeline_order(self):
        diag = VoiceDiagnostics(_clock=Clock())
        for _ in range(10):
            diag.frame_captured(0.0)
        diag.frame_gated("speaking")
        for _ in range(8):
            diag.frame_queued()
        diag.frame_dropped()
        for _ in range(7):
            diag.frame_sent()
        diag.input_transcript("hi")
        diag.response_started()
        diag.turn_complete("hi")
        line = diag.line()
        self.assertTrue(line.startswith(
            "mic=10 gated=1 queued=8 dropped=1 sent=7 transcripts=1 turns=1 "
            "responses=1"), line)

    def test_stage_ages_expose_a_stalled_stage(self):
        clock = Clock()
        diag = VoiceDiagnostics(_clock=clock)
        diag.frame_sent()
        clock.advance(40)
        diag.frame_queued()
        ages = diag.stage_ages()
        self.assertIn("queued 0.0s", ages)
        self.assertIn("sent 40.0s", ages)
        self.assertIn("transcript never", ages)


class TimelineTests(unittest.TestCase):

    def test_an_utterance_reads_as_a_sequence_of_stages(self):
        clock = Clock()
        diag = VoiceDiagnostics(_clock=clock)
        for _ in range(SPEECH_FRAMES):
            diag.frame_captured(0.5)
        clock.advance(1.0)
        diag.input_transcript("jarvis")
        diag.input_transcript("stop")
        diag.response_started()
        diag.turn_complete("jarvis stop")
        # "-  1.0s kind detail": the time column is padded for the HUD.
        kinds = [re.match(r"-\s*[\d.]+s (\S+)", line).group(1)
                 for line in diag.timeline()]
        self.assertEqual(kinds, ["speech_at_mic", "transcript",
                                 "response_started", "response_done",
                                 "turn_complete"])
        self.assertIn("jarvis stop", diag.timeline()[1],
                      "transcript pieces are joined into one entry")

    def test_a_burst_of_drops_is_one_entry_not_one_per_frame(self):
        diag = VoiceDiagnostics(_clock=Clock())
        for _ in range(50):
            diag.frame_dropped()
        timeline = diag.timeline()
        self.assertEqual(len(timeline), 1)
        self.assertIn("dropped 50", timeline[0])

    def test_the_timeline_is_bounded(self):
        diag = VoiceDiagnostics(_clock=Clock())
        for i in range(EVENT_LOG_SIZE * 3):
            diag.note("probe", str(i))
        self.assertEqual(len(diag.timeline(limit=10_000)), EVENT_LOG_SIZE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
