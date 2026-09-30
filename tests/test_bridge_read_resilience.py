"""
A torn or missing read of the bridge file is not the mod going away.

The mod replaces its file with an atomic rename five times a second, and on
Windows a read can land in the instant the file is being swapped: not
found, or half a file. One such read used to make available() False, and
_get_state_source() then fell back to OCR or an empty state -- for the whole
task, since the runner picks its source once. And every call read and
parsed the file twice (available(), then read()), with _target_probe doing
that every 40ms during a mine.
"""

from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import mod_bridge                                    # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA       # noqa: E402

NOW_MS = 1_700_000_000_000


def payload_text(written_ms=NOW_MS, x=1.5):
    return json.dumps({
        "schema": SCHEMA, "written_at_ms": written_ms, "in_game": True,
        "position": [x, 64.0, 0.5], "rotation": [0.0, 0.0],
        "health": 20.0, "hunger": 20,
    })


class Clock:
    def __init__(self, t=NOW_MS / 1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Flaky:
    """A reader that fails the first `fails` times -- as a read racing the
    mod's rename does -- then returns `text`."""

    def __init__(self, text, fails=0, error=FileNotFoundError):
        self.text, self.fails, self.error = text, fails, error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.fails > 0:
            self.fails -= 1
            if self.error is None:
                return self.text[: len(self.text) // 2]      # a torn read
            raise self.error("the file is being replaced")
        return self.text


def source(reader, clock=None):
    return ModBridgeStateSource(path="(test)", reader=reader,
                                clock=clock or Clock(),
                                sleeper=lambda _s: None)


class RetryTests(unittest.TestCase):

    def test_a_locked_file_mid_rename_is_retried(self):
        """On Windows a read racing the rename is a sharing violation."""
        reader = Flaky(payload_text(), fails=2, error=PermissionError)
        state = source(reader).read()
        self.assertFalse(state.is_empty, state.notes)
        self.assertEqual(state.position[0], 1.5)

    def test_a_missing_file_after_good_reads_is_retried(self):
        clock = Clock()
        reader = Flaky(payload_text())
        src = source(reader, clock)
        src.read()
        reader.fails = 2
        clock.t += 2.0             # past the last-good window: a real retry
        self.assertFalse(src.read().is_empty)

    def test_a_file_that_was_never_there_is_not_retried(self):
        """No mod installed: no pause on every check."""
        reader = Flaky(payload_text(), fails=10)
        self.assertTrue(source(reader).read().is_empty)
        self.assertEqual(reader.calls, 1)

    def test_a_torn_read_is_retried(self):
        reader = Flaky(payload_text(), fails=1, error=None)
        self.assertTrue(source(reader).available())

    def test_it_gives_up_after_a_few_tries(self):
        reader = Flaky(payload_text(), fails=10, error=PermissionError)
        self.assertTrue(source(reader).read().is_empty)
        self.assertLessEqual(reader.calls, 3)


class LastGoodTests(unittest.TestCase):

    def test_a_failure_within_a_second_of_a_good_read_uses_it(self):
        clock = Clock()
        reader = Flaky(payload_text())
        src = source(reader, clock)
        self.assertFalse(src.read().is_empty)
        reader.fails = 10
        clock.t += 0.5
        state = src.read()
        self.assertFalse(state.is_empty, "a hiccup read as the mod gone")
        self.assertTrue(src.available())

    def test_a_longer_outage_is_reported(self):
        clock = Clock()
        reader = Flaky(payload_text())
        src = source(reader, clock)
        src.read()
        reader.fails = 10
        clock.t += 1.5
        self.assertTrue(src.read().is_empty)


class ParseOnceTests(unittest.TestCase):

    def test_available_then_read_parses_once(self):
        src = source(Flaky(payload_text()))
        with unittest.mock.patch.object(mod_bridge.json, "loads",
                                        wraps=json.loads) as loads:
            src.available()
            src.read()
            src.read()
        self.assertEqual(loads.call_count, 1)

    def test_a_new_snapshot_is_parsed(self):
        reader = Flaky(payload_text(x=1.5))
        src = source(reader)
        self.assertEqual(src.read().position[0], 1.5)
        reader.text = payload_text(written_ms=NOW_MS + 200, x=2.5)
        self.assertEqual(src.read().position[0], 2.5)

    def test_a_reused_snapshot_reports_its_true_age(self):
        clock = Clock()
        src = source(Flaky(payload_text()), clock)
        src.read()
        clock.t += 0.15
        self.assertIn("150ms old", src.read().notes)


class StickySelectionTests(unittest.TestCase):
    """_get_state_source() must not trade the bridge for OCR on one bad
    read -- the runner keeps whatever it is given for the whole task."""

    def setUp(self):
        from actions import minecraft as mc_actions
        self.mc = mc_actions
        saved = (mc_actions._state_source, mc_actions._state_source_pinned,
                 getattr(mc_actions, "_bridge", None))

        def restore():
            (mc_actions._state_source, mc_actions._state_source_pinned,
             mc_actions._bridge) = saved
        self.addCleanup(restore)
        mc_actions._state_source = None
        mc_actions._state_source_pinned = False

        class Overlay:
            def __init__(inner, *a, **k):
                pass

            def available(inner):
                return True

        patcher = unittest.mock.patch.object(mc_actions,
                                             "DebugOverlayStateSource",
                                             Overlay)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = unittest.mock.patch.object(mc_actions, "_get_observer",
                                             lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_failed_availability_check_keeps_the_bridge(self):
        answers = iter([True, False, True])

        class Bridge(ModBridgeStateSource):
            def available(inner):
                return next(answers)

        self.mc._bridge = Bridge(path="(test)", reader=lambda: "")
        first = self.mc._get_state_source()
        second = self.mc._get_state_source()
        self.assertIsInstance(first, ModBridgeStateSource)
        self.assertIs(second, first, "one hiccup swapped the bridge for OCR")

    def test_one_bridge_object_is_kept_so_its_caches_last(self):
        self.mc._bridge = None
        with unittest.mock.patch.object(ModBridgeStateSource, "available",
                                        lambda self: True):
            first = self.mc._get_state_source()
            second = self.mc._get_state_source()
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
