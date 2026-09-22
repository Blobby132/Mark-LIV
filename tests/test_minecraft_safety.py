"""
Safety tests — the key allowlist, the ledger, and every stop path.

The invariant under test throughout:

    No Minecraft input may remain held after the controller has stopped.

Each test breaks something different — the key name, the window, the process,
the clock, the action thread itself — and checks that the keys still come up.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import input_backend                               # noqa: E402
from minecraft.controller import MinecraftController              # noqa: E402
from minecraft.emergency import EmergencyStopWatcher              # noqa: E402
from minecraft.errors import (                                    # noqa: E402
    InputBackendUnavailable, InvalidAction,
)
from minecraft.input_backend import FakeInputBackend              # noqa: E402
from minecraft.ledger import InputLedger                          # noqa: E402
from minecraft.session import SessionManager                      # noqa: E402

from tests.test_minecraft_controller import FakeLocator, FakeProcess  # noqa: E402


# ── The key allowlist ────────────────────────────────────────────────────────

class TestKeyAllowlist(unittest.TestCase):
    """Not a filter that could be bypassed — an absence. There is no function
    in the backend that takes a keycode."""

    def setUp(self):
        self.backend = FakeInputBackend()

    def test_the_allowed_keys_are_exactly_the_brief(self):
        expected = {
            "w", "a", "s", "d", "space", "shift", "ctrl",
            "1", "2", "3", "4", "5", "6", "7", "8", "9",
            "e", "q", "f3", "f5", "esc",
        }
        self.assertEqual(set(input_backend.ALLOWED_KEYS), expected)

    def test_every_allowed_key_works(self):
        for key in sorted(input_backend.ALLOWED_KEYS):
            with self.subTest(key=key):
                self.backend.key_down(key)
                self.backend.key_up(key)

    def test_alt_is_rejected(self):
        with self.assertRaises(InvalidAction) as ctx:
            self.backend.key_down("alt")
        self.assertIn("alt", str(ctx.exception).lower())
        self.assertEqual(self.backend.events, [])

    def test_the_windows_key_is_rejected(self):
        for name in ("win", "windows", "super", "meta", "cmd"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidAction):
                    self.backend.key_down(name)
        self.assertEqual(self.backend.events, [])

    def test_alt_f4_is_rejected_in_every_spelling(self):
        for name in ("alt+f4", "alt-f4", "f4", "altf4", "ALT+F4"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidAction):
                    self.backend.key_down(name)
        self.assertEqual(self.backend.events, [])

    def test_key_combinations_are_rejected(self):
        for combo in ("ctrl+alt+delete", "ctrl+c", "shift+w", "ctrl+shift+esc"):
            with self.subTest(combo=combo):
                with self.assertRaises(InvalidAction):
                    self.backend.key_down(combo)
        self.assertEqual(self.backend.events, [])

    def test_arbitrary_keycodes_are_rejected(self):
        for bad in ("0x41", "vk_65", "65", "scancode:30", "\x00", "",
                    "keydown w; keydown alt", None, 65, 3.5, ["w"], {"key": "w"}):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAction):
                    self.backend.key_down(bad)
        self.assertEqual(self.backend.events, [])

    def test_chat_and_command_keys_are_rejected_with_a_reason(self):
        for name, word in (("t", "chat"), ("enter", "chat"), ("/", "command")):
            with self.subTest(name=name):
                with self.assertRaises(InvalidAction) as ctx:
                    self.backend.key_down(name)
                self.assertIn(word, str(ctx.exception).lower())

    def test_an_unavailable_backend_validates_before_refusing(self):
        # Order matters: a bad key must be an InvalidAction (nothing attempted)
        # rather than an availability error, so the planner learns the right
        # thing on a machine that could never have sent it anyway.
        backend = input_backend.UnavailableBackend("nope")
        with self.assertRaises(InvalidAction):
            backend.key_down("alt")
        with self.assertRaises(InputBackendUnavailable):
            backend.key_down("w")


# ── The ledger ───────────────────────────────────────────────────────────────

class TestLedger(unittest.TestCase):

    def setUp(self):
        self.backend = FakeInputBackend()
        self.ledger = InputLedger(self.backend)

    def test_release_all_on_an_empty_ledger_is_safe(self):
        report = self.ledger.release_all()
        self.assertTrue(report.already_empty)
        self.assertTrue(report.clean)
        self.assertEqual(self.backend.events, [])

    def test_release_all_is_idempotent(self):
        self.ledger.hold("w")
        first = self.ledger.release_all()
        second = self.ledger.release_all()
        third = self.ledger.release_all()
        self.assertEqual(first.released, ("w",))
        self.assertTrue(second.already_empty)
        self.assertTrue(third.already_empty)
        self.assertEqual(self.ledger.held(), frozenset())

    def test_release_all_releases_everything(self):
        for key in ("w", "shift", "space"):
            self.ledger.hold(key)
        report = self.ledger.release_all()
        self.assertEqual(set(report.released), {"w", "shift", "space"})
        self.assertEqual(self.ledger.held(), frozenset())
        self.assertEqual(set(self.backend.ups()), {"w", "shift", "space"})

    def test_holding_the_same_key_twice_owes_one_release(self):
        self.ledger.hold("w")
        self.ledger.hold("w")
        self.assertEqual(self.backend.downs(), ["w"])
        self.ledger.release_all()
        self.assertEqual(self.backend.ups(), ["w"])

    def test_a_failed_press_still_leaves_the_key_releasable(self):
        # Record-first: a key_down that raises leaves a ledger entry, so
        # release_all sends a redundant key-up. Wrong in the harmless
        # direction, on purpose.
        self.backend._fail_on = "key_down"
        with self.assertRaises(InputBackendUnavailable):
            self.ledger.hold("w")
        report = self.ledger.release_all()
        self.assertIn("w", report.released)
        self.assertEqual(self.ledger.held(), frozenset())

    def test_a_failed_release_is_reported_not_swallowed(self):
        self.ledger.hold("w")
        self.backend._fail_on = "key_up"
        report = self.ledger.release_all()
        self.assertFalse(report.clean, "a stuck key was reported as clean")
        self.assertIn("w", report.failed)
        # Dropped anyway, so the next release_all cannot loop on it forever.
        self.assertEqual(self.ledger.held(), frozenset())

    def test_release_all_is_thread_safe(self):
        for key in ("w", "a", "s", "d", "space"):
            self.ledger.hold(key)
        results = []

        def _release():
            results.append(self.ledger.release_all())

        threads = [threading.Thread(target=_release) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(self.ledger.held(), frozenset())
        released = [k for r in results for k in r.released]
        # Each key released exactly once across all the concurrent callers.
        self.assertEqual(sorted(released), ["a", "d", "s", "space", "w"])

    def test_overdue_reports_keys_held_too_long(self):
        self.ledger.hold("w")
        self.assertEqual(self.ledger.overdue(limit=10.0), ())
        self.assertEqual(self.ledger.overdue(limit=-1.0), ("w",))


# ── Stop paths ───────────────────────────────────────────────────────────────

class _StopCase(unittest.TestCase):

    def setUp(self):
        self.backend = FakeInputBackend()
        self.locator = FakeLocator()
        self.process = FakeProcess()
        self.sessions = SessionManager()
        self.controller = MinecraftController(
            backend=self.backend, locator=self.locator, sessions=self.sessions,
            process_module=self.process, start_watchers=False,
            focus_wait_s=0.0,
        )

    def tearDown(self):
        try:
            self.controller.stop("teardown")
            self.controller._stop_watchers()
        except Exception:
            pass

    def assert_released(self, why=""):
        self.assertEqual(self.controller.ledger.held(), frozenset(),
                         why or "input was left held")


class TestEmergencyStop(_StopCase):

    def test_stop_releases_everything_and_ends_the_session(self):
        self.controller.start_session(duration_s=60)
        self.controller.ledger.hold("w")
        self.controller.ledger.hold("shift")

        outcome = self.controller.stop("test")

        self.assertTrue(outcome["stopped"])
        self.assertEqual(set(outcome["released"]), {"w", "shift"})
        self.assertTrue(outcome["session_ended"])
        self.assertFalse(self.sessions.is_active())
        self.assert_released()

    def test_stop_is_safe_to_call_repeatedly(self):
        self.controller.start_session(duration_s=60)
        self.controller.ledger.hold("w")
        for _ in range(5):
            outcome = self.controller.stop("repeat")
            self.assertTrue(outcome["stopped"])
        self.assert_released()

    def test_stop_works_with_nothing_running(self):
        outcome = self.controller.stop("nothing to do")
        self.assertTrue(outcome["stopped"])
        self.assertEqual(outcome["released"], [])

    def test_stop_works_while_an_action_thread_is_wedged(self):
        """The one that matters: the planner is stuck, the action thread is
        blocked, and the user hits the stop. It must not wait for either."""
        self.controller.start_session(duration_s=60)
        wedged = threading.Event()

        def _wedge():
            self.controller.ledger.hold("w")
            wedged.set()
            time.sleep(30)          # never finishes within the test

        thread = threading.Thread(target=_wedge, daemon=True)
        thread.start()
        self.assertTrue(wedged.wait(timeout=5))

        started = time.monotonic()
        outcome = self.controller.stop("user pressed F12")
        elapsed = time.monotonic() - started

        self.assertIn("w", outcome["released"])
        self.assertLess(elapsed, 2.0, "stop waited for the wedged thread")
        self.assert_released()

    def test_the_emergency_watcher_triggers_a_stop(self):
        fired = []
        watcher = EmergencyStopWatcher(on_stop=fired.append)
        controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=SessionManager(), process_module=self.process,
            emergency=watcher, start_watchers=False, focus_wait_s=0.0,
        )
        controller.start_session(duration_s=60)
        controller.ledger.hold("w")

        watcher.trigger("F12 pressed")

        self.assertEqual(fired, ["F12 pressed"])
        # The default wiring goes through the controller; here the callback was
        # replaced, so assert the watcher itself fired and the API path works.
        controller.stop("after watcher")
        self.assertEqual(controller.ledger.held(), frozenset())

    def test_the_watcher_survives_a_raising_callback(self):
        def _explode(_reason):
            raise RuntimeError("callback exploded")

        watcher = EmergencyStopWatcher(on_stop=_explode)
        watcher.trigger("boom")          # must not raise
        self.assertEqual(watcher.fired, 1)

    def test_a_default_controller_wires_f12_to_a_real_stop(self):
        self.controller.start_session(duration_s=60)
        self.controller.ledger.hold("w")
        self.controller.emergency.trigger("F12 pressed")
        self.assert_released("the emergency watcher did not release keys")
        self.assertFalse(self.sessions.is_active())


class TestSupervisorDeadman(_StopCase):
    """The deadman exists for the case where the action loop never ticks
    again — so it is tested by holding a key behind the controller's back and
    letting the supervisor notice."""

    def test_the_supervisor_releases_an_overdue_key(self):
        controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=SessionManager(), process_module=self.process,
            start_watchers=True,
        )
        try:
            controller.start_session(duration_s=60)
            controller.ledger.hold("w")
            # Backdate the hold so it is already past the deadman limit.
            with controller.ledger._lock:
                controller.ledger._held["w"].since = time.monotonic() - 60

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not controller.ledger.is_holding():
                    break
                time.sleep(0.05)

            self.assertEqual(controller.ledger.held(), frozenset(),
                             "the deadman did not release an overdue key")
        finally:
            controller.stop("test done")
            controller._stop_watchers()

    def test_the_supervisor_releases_when_focus_is_lost_between_actions(self):
        controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=SessionManager(), process_module=self.process,
            start_watchers=True,
        )
        try:
            controller.start_session(duration_s=60)
            controller.ledger.hold("w")
            self.locator.foreground = False

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if not controller.ledger.is_holding():
                    break
                time.sleep(0.05)

            self.assertEqual(controller.ledger.held(), frozenset())
        finally:
            controller.stop("test done")
            controller._stop_watchers()


class TestProcessDeath(_StopCase):

    def test_process_death_refuses_further_input(self):
        self.controller.start_session(duration_s=60)
        self.process.alive = False
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "process_gone")
        self.assertEqual(self.backend.events, [])

    def test_process_death_mid_hold_releases(self):
        self.controller.start_session(duration_s=60)

        def _kill():
            time.sleep(0.1)
            self.process.alive = False

        threading.Thread(target=_kill, daemon=True).start()
        self.controller.move({"direction": "forward", "duration": 2.0})
        self.assert_released()


class TestHeadlessCannotStartASession(unittest.TestCase):
    """The broker's fail-closed behaviour, reached through the real tool."""

    def setUp(self):
        from core import audit, confirm, permissions
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        audit.configure(path=Path(self._tmp.name) / "a.jsonl")
        permissions.install()
        confirm.bind(show=None, hide=None, log=None)    # headless
        confirm._pending = None

    def tearDown(self):
        from core import audit, confirm
        confirm.bind(show=None, hide=None, log=None)
        confirm._pending = None
        audit.configure(path=audit.DEFAULT_DIR / audit.DEFAULT_FILENAME)
        self._tmp.cleanup()

    def test_starting_a_session_headless_is_refused(self):
        from core import capabilities, permissions
        started = []
        result = permissions.guard(
            "minecraft_control", capabilities.MINECRAFT_CONTROL_SESSION,
            summary="Let me control Minecraft",
            run=lambda: started.append("started"),
        )
        self.assertTrue(result.denied)
        self.assertEqual(started, [],
                         "a control session opened with no way to ask a human")


class TestWindowProbeRobustness(unittest.TestCase):
    """A transient probe failure is not a closed game.

    This is the regression for a bug that read as "the Minecraft window
    closed" in the middle of mining, on a window that was plainly still open.
    The cause was re-declaring the ctypes prototypes on every probe: they live
    on shared function pointers, and `_guard` runs from the action loop every
    40ms, the supervisor every 200ms and the focus-wait loop, so one thread
    could call a function while another was mid-assignment. The call raised,
    the enumeration callback swallowed it, no windows were found, and the
    guard called that a closed game."""

    def _controller(self, locator):
        return MinecraftController(
            backend=FakeInputBackend(), locator=locator,
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0)

    def test_one_missed_probe_does_not_end_anything(self):
        locator = FakeLocator()
        controller = self._controller(locator)
        try:
            controller.start_session(duration_s=60)
            locator.found = False
            self.assertEqual(controller._guard(), "",
                             "a single miss must be tolerated")
        finally:
            controller.stop("test")

    def test_two_consecutive_misses_do_stop_it(self):
        """A real close never recovers, so patience must not be unlimited."""
        locator = FakeLocator()
        controller = self._controller(locator)
        try:
            controller.start_session(duration_s=60)
            locator.found = False
            controller._guard()
            self.assertEqual(controller._guard(), "window_gone")
        finally:
            controller.stop("test")

    def test_the_counter_resets_when_the_window_comes_back(self):
        locator = FakeLocator()
        controller = self._controller(locator)
        try:
            controller.start_session(duration_s=60)
            locator.found = False
            controller._guard()            # one miss
            locator.found = True
            self.assertEqual(controller._guard(), "")
            locator.found = False
            self.assertEqual(controller._guard(), "",
                             "the earlier miss should not still count")
        finally:
            controller.stop("test")


class TestProtototypesAreConfiguredOnce(unittest.TestCase):
    """The prototypes must be declared once, not per probe.

    Asserted structurally because the race itself cannot be reproduced off
    Windows: what matters is that `_probe_windows` does not mutate shared
    ctypes state every time it runs."""

    def test_probe_does_not_redeclare_prototypes(self):
        import inspect
        from minecraft import window

        source = inspect.getsource(window._probe_windows)
        self.assertNotIn("_configure(", source,
                         "_probe_windows must not re-declare prototypes; "
                         "they are shared across threads")
        self.assertIn("_ensure_prototypes()", source)

    def test_the_setup_is_guarded_by_a_lock_and_a_flag(self):
        import inspect
        from minecraft import window

        source = inspect.getsource(window._ensure_prototypes)
        self.assertIn("_PROTOTYPES_LOCK", source)
        self.assertIn("_PROTOTYPES_READY", source)

    def test_a_failed_enumeration_does_not_claim_the_game_closed(self):
        """Wording matters here: "closed" is final and sends the user to
        restart the game; "could not find just now" is transient."""
        import inspect
        from minecraft import window

        source = inspect.getsource(window._probe_windows)
        self.assertIn("could not find a game window", source)
        self.assertNotIn("has closed", source)


if __name__ == "__main__":
    unittest.main()
