"""
Regression tests for core/confirm.py's pre-existing contract.

Phase 1 added two functions to this module (`is_available`, `set_observer`) and
three notification calls. Nothing else was touched, and
`actions/computer_settings.py` — its only caller today — must keep working
exactly as it did. These tests pin the old behaviour so a later phase cannot
quietly change it.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import confirm                                        # noqa: E402


def _settle(timeout: float = 5.0) -> None:
    for thread in list(threading.enumerate()):
        if thread.name.startswith("confirm-") and thread.is_alive():
            thread.join(timeout=timeout)


class _ConfirmCase(unittest.TestCase):

    def setUp(self):
        self.shown: list = []
        self.hidden = 0
        self.logs: list = []
        self.ran: list = []
        confirm.set_observer(None)
        confirm._pending = None
        confirm.bind(show=self._show, hide=self._hide, log=self.logs.append)

    def tearDown(self):
        confirm.bind(show=None, hide=None, log=None)
        confirm.set_observer(None)
        confirm._pending = None

    def _show(self, title, detail):
        self.shown.append((title, detail))

    def _hide(self):
        self.hidden += 1


class TestExistingBehaviour(_ConfirmCase):

    def test_request_returns_immediately_and_does_not_run_the_work(self):
        sentence = confirm.request(
            key="shutdown", title="Shut down", detail="Unsaved work is lost.",
            run=lambda: self.ran.append("ran") or "off",
        )
        self.assertIn("[CONFIRMATION_PENDING]", sentence)
        self.assertIn("Shut down", sentence)
        self.assertEqual(self.ran, [], "request() ran the work by itself")
        self.assertEqual(self.shown, [("Shut down", "Unsaved work is lost.")])

    def test_resolve_true_runs_the_work_off_the_calling_thread(self):
        confirm.request(key="k", title="T", detail="D",
                        run=lambda: self.ran.append("ran") or "done")
        confirm.resolve(True)
        _settle()
        self.assertEqual(self.ran, ["ran"])
        self.assertEqual(self.hidden, 1)

    def test_resolve_false_runs_nothing(self):
        confirm.request(key="k", title="T", detail="D",
                        run=lambda: self.ran.append("ran"))
        confirm.resolve(False)
        _settle()
        self.assertEqual(self.ran, [])
        self.assertTrue(any("Cancelled" in line for line in self.logs))

    def test_resolve_with_nothing_pending_is_harmless(self):
        confirm.resolve(True)
        _settle()
        self.assertEqual(self.ran, [])

    def test_pending_title_reports_what_is_waiting(self):
        self.assertEqual(confirm.pending_title(), "")
        confirm.request(key="k", title="Restart this computer", detail="",
                        run=lambda: None)
        self.assertEqual(confirm.pending_title(), "Restart this computer")
        confirm.resolve(False)
        self.assertEqual(confirm.pending_title(), "")

    def test_an_expired_request_does_not_run(self):
        confirm.request(key="k", title="T", detail="D",
                        run=lambda: self.ran.append("ran"))
        # Backdate the pending entry past the timeout.
        confirm._pending.at = time.monotonic() - confirm.TIMEOUT_SECONDS - 1
        confirm.resolve(True)
        _settle()
        self.assertEqual(self.ran, [], "an expired confirmation still ran")
        self.assertTrue(any("expired" in line.lower() for line in self.logs))

    def test_a_failing_work_callable_does_not_escape(self):
        confirm.request(key="k", title="T", detail="D",
                        run=lambda: (_ for _ in ()).throw(RuntimeError("nope")))
        confirm.resolve(True)          # must not raise
        _settle()
        self.assertTrue(any("failed" in line.lower() for line in self.logs))

    def test_with_no_interface_bound_it_refuses_rather_than_acting(self):
        confirm.bind(show=None, hide=None, log=None)
        sentence = confirm.request(key="k", title="Delete everything", detail="",
                                   run=lambda: self.ran.append("ran"))
        self.assertIn("cannot confirm", sentence.lower())
        self.assertEqual(self.ran, [])

    def test_a_failing_show_callback_leaves_nothing_pending(self):
        def _broken(_title, _detail):
            raise RuntimeError("Qt is gone")
        confirm.bind(show=_broken, hide=self._hide, log=self.logs.append)
        sentence = confirm.request(key="k", title="T", detail="D",
                                   run=lambda: self.ran.append("ran"))
        self.assertIn("Nothing was done", sentence)
        self.assertEqual(confirm.pending_title(), "")
        self.assertEqual(self.ran, [])


class TestAdditions(_ConfirmCase):

    def test_is_available_tracks_the_bound_interface(self):
        self.assertTrue(confirm.is_available())
        confirm.bind(show=None, hide=None, log=None)
        self.assertFalse(confirm.is_available())

    def test_observer_sees_every_ending(self):
        seen: list = []
        confirm.set_observer(lambda key, title, outcome: seen.append(outcome))

        confirm.request(key="a", title="A", detail="", run=lambda: "x")
        confirm.resolve(True)
        _settle()

        confirm.request(key="b", title="B", detail="", run=lambda: "x")
        confirm.resolve(False)

        confirm.request(key="c", title="C", detail="", run=lambda: "x")
        confirm._pending.at = time.monotonic() - confirm.TIMEOUT_SECONDS - 1
        confirm.resolve(True)

        self.assertEqual(seen, ["approved", "cancelled", "expired"])

    def test_a_replaced_confirmation_is_reported_as_superseded(self):
        seen: list = []
        confirm.set_observer(lambda key, title, outcome: seen.append((key, outcome)))
        confirm.request(key="first", title="First", detail="", run=lambda: "x")
        confirm.request(key="second", title="Second", detail="", run=lambda: "x")
        self.assertEqual(seen, [("first", "superseded")])
        # And the second one is what a press now answers.
        self.assertEqual(confirm.pending_title(), "Second")

    def test_a_broken_observer_cannot_break_a_confirmation(self):
        def _broken(*_args):
            raise RuntimeError("observer exploded")
        confirm.set_observer(_broken)
        confirm.request(key="k", title="T", detail="", run=lambda: self.ran.append("ran"))
        confirm.resolve(True)
        _settle()
        self.assertEqual(self.ran, ["ran"])


if __name__ == "__main__":
    unittest.main()
