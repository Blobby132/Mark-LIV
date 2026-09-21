"""
Tests for stuck detection.

THE BUG CLASS THIS EXISTS FOR
    Walk into a wall. Every action reports honestly: the key went down, it was
    held for the time asked, no error. The action succeeded. Nothing moved.
    Repeat twenty times and the task ends at its step limit with a log full of
    successes and no progress.

    "Ran out of steps" is a backstop, not a diagnosis. These tests are about
    producing the diagnosis instead.

THE DISTINCTION THAT MATTERS MOST
    Repeating with no CHANGE and repeating with no ANSWER are different
    problems needing different advice. A wall means aim somewhere else; an
    unreadable screen means install OCR. Conflating them sends the user off
    fixing the wrong thing, so they are separate verdicts and the tests keep
    them separate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import verification as verify_mod                       # noqa: E402
from minecraft.progress import (                                       # noqa: E402
    BLIND, BLIND_AFTER, NO_PROGRESS, ProgressMonitor, STUCK_AFTER,
)


def failed_at(position) -> dict:
    return {"status": verify_mod.FAILED, "after": {"position": position}}


def succeeded_at(position) -> dict:
    return {"status": verify_mod.SUCCESS, "after": {"position": position}}


def blind() -> dict:
    """A REAL unverifiable verdict, produced by the verifier.

    This used to be a hand-written `{"after": {}}`, and that fabrication is
    what let a live bug through: `Expectation.check` reports the fields it
    WANTED even when it could not read them, so a real blind verdict carries
    `{"rotation": None}` and the monitor classified every one of them as
    "nothing changed". The user's agent spun in circles for twenty steps
    while the detector built to stop exactly that said it was fine.

    Building the payload through the real code path is the difference between
    testing the monitor and testing my idea of the monitor."""
    from minecraft.state import empty_state
    return verify_mod.turned(2.0).check(empty_state(), empty_state(),
                                        delivered=True).as_dict()


class TestNoProgress(unittest.TestCase):

    def test_the_same_action_with_no_change_is_stuck(self):
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("move", failed_at((10.0, 64.0, 5.0)))
        self.assertTrue(monitor.stuck)
        self.assertEqual(monitor.stuck_reason(), NO_PROGRESS)

    def test_it_waits_before_deciding(self):
        """Mining genuinely takes several swings with no visible change. A
        threshold of two would make the commonest real task impossible."""
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER - 1):
            monitor.record("mine", failed_at((1.0, 2.0, 3.0)))
        self.assertFalse(monitor.stuck)

    def test_state_that_keeps_changing_is_never_stuck(self):
        monitor = ProgressMonitor()
        for step in range(STUCK_AFTER * 3):
            monitor.record("move", failed_at((float(step), 64.0, 0.0)))
        self.assertFalse(monitor.stuck)

    def test_a_recent_success_clears_the_suspicion(self):
        """An action that works every time can legitimately leave the checked
        state looking identical — a door that is already open, say."""
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("interact", succeeded_at((1.0, 2.0, 3.0)))
        self.assertFalse(monitor.stuck)

    def test_switching_action_restarts_the_count(self):
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER - 1):
            monitor.record("move", failed_at((1.0, 2.0, 3.0)))
        monitor.record("jump", failed_at((1.0, 2.0, 3.0)))
        self.assertFalse(monitor.stuck)

    def test_the_explanation_names_the_action_not_the_code(self):
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("move", failed_at((1.0, 2.0, 3.0)))
        text = monitor.explain()
        self.assertIn("move", text)
        self.assertIn("nothing changed", text)


class TestBlind(unittest.TestCase):
    """Repeating with no ANSWER. A different problem from a wall."""

    def test_a_run_of_unverifiable_verdicts_is_reported_separately(self):
        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER):
            monitor.record("mine", blind())
        self.assertTrue(monitor.stuck)
        self.assertEqual(monitor.stuck_reason(), BLIND)

    def test_blind_steps_are_not_counted_as_identical_states(self):
        """They all carry no evidence, so they would otherwise match each
        other and be reported as "nothing changed" — which would send the user
        looking for a wall when the real problem is that OCR is not
        installed."""
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("mine", blind())
        self.assertNotEqual(monitor.stuck_reason(), NO_PROGRESS)

    def test_a_verdict_naming_fields_it_could_not_read_carries_no_evidence(self):
        """The regression. An unverifiable check still reports which fields it
        wanted — `{"rotation": None}` — so testing for an empty dict missed
        the case that actually happens."""
        payload = blind()
        self.assertTrue(payload["after"],
                        "a real blind verdict does name its fields")
        self.assertTrue(all(v is None for v in payload["after"].values()))

        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER):
            monitor.record("look", payload)
        self.assertEqual(monitor.stuck_reason(), BLIND)
        self.assertEqual(monitor.repeats, 0)

    def test_the_blind_threshold_is_higher_than_the_stuck_one(self):
        """One unreadable observation is noise; a run of them is a setup
        problem. Being quicker to call "wall" than "blind" is deliberate."""
        self.assertGreater(BLIND_AFTER, STUCK_AFTER)

    def test_one_readable_verdict_breaks_the_streak(self):
        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER - 1):
            monitor.record("mine", blind())
        monitor.record("mine", failed_at((1.0, 2.0, 3.0)))
        self.assertEqual(monitor.blind_streak, 0)

    def test_the_explanation_points_at_the_real_fix(self):
        monitor = ProgressMonitor()
        for _ in range(BLIND_AFTER):
            monitor.record("mine", blind())
        text = monitor.explain().lower()
        self.assertIn("see whether", text)
        self.assertIn("f3", text)


class TestHousekeeping(unittest.TestCase):

    def test_a_fresh_monitor_is_not_stuck(self):
        monitor = ProgressMonitor()
        self.assertFalse(monitor.stuck)
        self.assertEqual(monitor.explain(), "")
        self.assertEqual(monitor.repeats, 0)

    def test_reset_forgets_everything(self):
        """A new approach is judged on its own, not on the failure count of
        the one it replaced."""
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("move", failed_at((1.0, 2.0, 3.0)))
        self.assertTrue(monitor.stuck)
        monitor.reset()
        self.assertFalse(monitor.stuck)
        self.assertEqual(monitor.repeats, 0)

    def test_a_malformed_verification_does_not_raise(self):
        monitor = ProgressMonitor()
        for junk in ({}, {"status": None}, {"after": None},
                     {"after": {"x": object()}}):
            with self.subTest(payload=repr(junk)[:40]):
                monitor.record("move", junk)
        self.assertIsInstance(monitor.as_dict(), dict)

    def test_as_dict_reports_the_numbers_behind_the_verdict(self):
        monitor = ProgressMonitor()
        for _ in range(STUCK_AFTER):
            monitor.record("move", failed_at((1.0, 2.0, 3.0)))
        payload = monitor.as_dict()
        self.assertTrue(payload["stuck"])
        self.assertEqual(payload["stuck_reason"], NO_PROGRESS)
        self.assertEqual(payload["repeats_without_change"], STUCK_AFTER)


if __name__ == "__main__":
    unittest.main()
