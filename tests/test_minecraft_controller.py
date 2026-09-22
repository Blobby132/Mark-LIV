"""
Controller tests — bounded actions, and the guard that stops them.

Nothing here needs Minecraft, a window, or a keyboard. A fake locator decides
what the world looks like and a fake backend records what was asked of it, so
the thing under test is the guarding logic rather than the typing.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import action_spec                                 # noqa: E402
from minecraft.controller import MinecraftController              # noqa: E402
from minecraft.errors import InvalidAction                        # noqa: E402
from minecraft.input_backend import FakeInputBackend              # noqa: E402
from minecraft.session import SessionManager                      # noqa: E402
from minecraft.window import WindowInfo, WindowRect               # noqa: E402


class FakeLocator:
    """A window whose state the test sets directly."""

    def __init__(self, found=True, foreground=True, focus_known=True,
                 pid=4242, width=1920, height=1080):
        self.found = found
        self.foreground = foreground
        self.focus_known = focus_known
        self.pid = pid
        self.width = width
        self.height = height
        self.probes = 0

    def attach(self) -> WindowInfo:
        return self.probe()

    def probe(self) -> WindowInfo:
        self.probes += 1
        rect = (WindowRect(0, 0, self.width, self.height)
                if self.found else None)
        return WindowInfo(found=self.found, handle=1, title="Minecraft 1.21",
                          pid=self.pid, rect=rect,
                          foreground=self.foreground,
                          focus_known=self.focus_known,
                          detail="fake window")


class FakeProcess:
    """Stands in for minecraft/process.py."""

    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self, pid):
        return self.alive

    def find(self):
        from minecraft.process import ProcessInfo
        if self.alive:
            return ProcessInfo(running=True, pid=4242, name="javaw.exe",
                               matched_on="fake", detail="fake Minecraft")
        return ProcessInfo(running=False, detail="not running")


class _ControllerCase(unittest.TestCase):

    def setUp(self):
        self.backend = FakeInputBackend()
        self.locator = FakeLocator()
        self.process = FakeProcess()
        self.sessions = SessionManager()
        # Watchers off by default: the supervisor thread is tested explicitly
        # where it matters, and left out elsewhere so timing is deterministic.
        self.controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=self.sessions, process_module=self.process,
            start_watchers=False, focus_wait_s=0.0,
        )

    def tearDown(self):
        try:
            self.controller.stop("test teardown")
            self.controller._stop_watchers()
        except Exception:
            pass

    def open_session(self, duration=60.0):
        return self.controller.start_session(duration_s=duration)

    def assert_nothing_held(self, why=""):
        self.assertEqual(self.controller.ledger.held(), frozenset(),
                         why or "a key was left held")
        # And the backend agrees: every key that went down came back up.
        # Not equality — the ledger records before it presses, so a failed
        # key_down still produces a (harmless, deliberate) key_up.
        unmatched = [k for k in self.backend.downs()
                     if k not in self.backend.ups()]
        self.assertEqual(unmatched, [],
                         f"keys pressed but never released: {unmatched}")


# ── Nothing happens without a session ────────────────────────────────────────

class TestSessionRequired(_ControllerCase):

    def test_move_without_a_session_is_refused(self):
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "no_session")
        self.assertEqual(self.backend.events, [], "input was sent with no session")

    def test_jump_without_a_session_is_refused(self):
        result = self.controller.jump({})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.events, [])

    def test_look_without_a_session_is_refused(self):
        result = self.controller.look({"dx": 50})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.mouse_moves(), [])

    def test_move_after_the_session_ends_is_refused(self):
        self.open_session()
        self.controller.end_session("done")
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.events, [])


# ── Bounds ───────────────────────────────────────────────────────────────────

class TestBounds(_ControllerCase):

    def test_movement_is_clamped_to_the_maximum(self):
        self.open_session()
        started = time.monotonic()
        result = self.controller.move({"direction": "forward", "duration": 30})
        elapsed = time.monotonic() - started

        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.clamped)
        self.assertEqual(result.requested["requested_duration"], 30.0)
        self.assertEqual(result.requested["duration"],
                         action_spec.MAX_MOVE_DURATION_S)
        self.assertLess(elapsed, action_spec.MAX_MOVE_DURATION_S + 1.0,
                        "a 30s request actually held the key for 30s")

    def test_a_short_move_is_not_clamped(self):
        self.open_session()
        result = self.controller.move({"direction": "forward", "duration": 0.15})
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.clamped)

    def test_look_deltas_are_clamped(self):
        self.open_session()
        result = self.controller.look({"dx": 99999, "dy": -99999})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.clamped)
        self.assertEqual(self.backend.mouse_moves(),
                         [(action_spec.MAX_LOOK_DELTA_PX,
                           -action_spec.MAX_LOOK_DELTA_PX)])

    def test_session_duration_is_clamped(self):
        info = self.controller.start_session(duration_s=99999)
        self.assertLessEqual(info["session"]["granted_seconds"], 300.0)
        self.assertEqual(info["session"]["requested_seconds"], 99999.0)

    def test_degrees_are_refused_rather_than_guessed(self):
        self.open_session()
        with self.assertRaises(InvalidAction) as ctx:
            self.controller.look({"yaw": 90})
        self.assertIn("degree", str(ctx.exception).lower())
        self.assertEqual(self.backend.mouse_moves(), [])


# ── Keys always come back up ─────────────────────────────────────────────────

class TestKeysAreReleased(_ControllerCase):

    def test_a_normal_move_releases_its_key(self):
        self.open_session()
        result = self.controller.move({"direction": "forward", "duration": 0.15})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.downs(), ["w"])
        self.assertEqual(self.backend.ups(), ["w"])
        self.assert_nothing_held()

    def test_each_direction_uses_the_right_key(self):
        self.open_session()
        for direction, key in (("forward", "w"), ("back", "s"),
                               ("left", "a"), ("right", "d")):
            with self.subTest(direction=direction):
                self.backend.events.clear()
                self.controller.move({"direction": direction, "duration": 0.08})
                self.assertEqual(self.backend.downs(), [key])
                self.assertEqual(self.backend.ups(), [key])

    def test_jump_taps_space(self):
        self.open_session()
        result = self.controller.jump({})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.downs(), ["space"])
        self.assert_nothing_held()

    def test_repeated_moves_do_not_accumulate_held_keys(self):
        self.open_session()
        for _ in range(5):
            self.controller.move({"direction": "forward", "duration": 0.06})
        self.assert_nothing_held()
        self.assertEqual(len(self.backend.downs()), 5)
        self.assertEqual(len(self.backend.ups()), 5)

    def test_a_backend_failure_mid_action_still_releases(self):
        self.open_session()
        self.backend._fail_on = "key_down"
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assert_nothing_held("a failed key_down left the ledger populated")


# ── The guard ────────────────────────────────────────────────────────────────

class TestGuardStopsActions(_ControllerCase):

    def test_focus_loss_mid_move_stops_and_releases(self):
        self.open_session()

        def _steal_focus():
            time.sleep(0.15)
            self.locator.foreground = False

        threading.Thread(target=_steal_focus, daemon=True).start()
        result = self.controller.move({"direction": "forward", "duration": 2.0})

        self.assertFalse(result.ok, "a move that lost focus reported success")
        self.assertEqual(result.stopped_reason, "focus_lost")
        self.assertFalse(result.window_focused_throughout)
        self.assertLess(result.actual_duration_ms, 1500,
                        "the move kept going after focus was lost")
        self.assertGreater(result.actual_duration_ms, 50)
        self.assert_nothing_held("focus loss did not release the key")

    def test_the_reported_duration_is_what_actually_happened(self):
        self.open_session()

        def _steal_focus():
            time.sleep(0.3)
            self.locator.foreground = False

        threading.Thread(target=_steal_focus, daemon=True).start()
        result = self.controller.move({"direction": "forward", "duration": 1.0})

        # The brief's example: 300ms of a requested 1s.
        self.assertFalse(result.ok)
        self.assertEqual(result.requested["duration"], 1.0)
        self.assertLess(result.actual_duration_ms, 900)
        self.assertGreater(result.actual_duration_ms, 150)

    def test_process_death_mid_move_stops_and_releases(self):
        self.open_session()

        def _kill():
            time.sleep(0.12)
            self.process.alive = False

        threading.Thread(target=_kill, daemon=True).start()
        result = self.controller.move({"direction": "forward", "duration": 2.0})

        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "process_gone")
        self.assert_nothing_held()

    def test_window_disappearing_mid_move_stops_and_releases(self):
        self.open_session()

        def _close():
            time.sleep(0.12)
            self.locator.found = False

        threading.Thread(target=_close, daemon=True).start()
        result = self.controller.move({"direction": "forward", "duration": 2.0})

        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "window_gone")
        self.assert_nothing_held()

    def test_unknown_focus_is_treated_as_not_focused(self):
        # The Linux/macOS case: we cannot tell, so we refuse.
        self.locator.focus_known = False
        self.controller.start_session(duration_s=30)
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "focus_unknown")
        self.assertEqual(self.backend.events, [])

    def test_the_guard_runs_before_the_key_goes_down(self):
        self.open_session()
        self.locator.foreground = False
        result = self.controller.move({"direction": "forward", "duration": 0.5})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.events, [],
                         "a key was pressed before the guard ran")


# ── Session expiry ───────────────────────────────────────────────────────────

class TestSessionExpiry(_ControllerCase):

    def test_an_expired_session_refuses_movement(self):
        self.controller.start_session(duration_s=5)
        session = self.sessions.current
        session.expires_at = time.monotonic() - 1     # backdate
        result = self.controller.move({"direction": "forward", "duration": 0.2})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "session_expired")
        self.assertEqual(self.backend.events, [])

    def test_expiry_mid_move_stops_and_releases(self):
        self.controller.start_session(duration_s=60)
        session = self.sessions.current

        def _expire():
            time.sleep(0.12)
            session.expires_at = time.monotonic() - 1

        threading.Thread(target=_expire, daemon=True).start()
        result = self.controller.move({"direction": "forward", "duration": 2.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "session_expired")
        self.assert_nothing_held()

    def test_ending_a_session_releases_held_keys(self):
        self.open_session()
        self.controller.ledger.hold("w")
        self.assertTrue(self.controller.ledger.is_holding("w"))
        self.controller.end_session("test")
        self.assert_nothing_held("ending the session did not release keys")

    def test_the_manager_still_refuses_a_second_session(self):
        """The one-session invariant is unchanged; what changed is who handles
        the collision. Asserted here at the level that owns it."""
        self.open_session()
        with self.assertRaises(RuntimeError):
            self.sessions.start(duration_s=30)

    def test_asking_for_control_you_already_have_reuses_it(self):
        """This is the bug that cost a live session and wedged the controller.

        The manager raised, the adapter's catch-all called stop(), and stop()
        ended the session and set the sticky cancel flag — so a redundant
        request destroyed the working session and refused everything
        afterwards with a stale reason no confirmation could clear."""
        first = self.open_session()
        again = self.controller.start_session(duration_s=30)
        self.assertTrue(again["reused_existing"])
        self.assertEqual(again["session"]["session_id"],
                         first["session"]["session_id"])

    def test_a_redundant_request_leaves_the_controller_usable(self):
        """The property that actually failed for the user: after asking twice,
        moving still works."""
        self.open_session()
        self.controller.start_session(duration_s=30)
        result = self.controller.move({"direction": "forward",
                                       "duration": 0.05})
        self.assertTrue(result.ok, result.error)

    def test_asking_for_authorization_replaces_an_unauthorized_session(self):
        """A grant is not widened in place: the confirmation the user answered
        for the first session described a narrower one than they would end up
        with."""
        first = self.controller.start_session(duration_s=30,
                                              authorized=False)
        upgraded = self.controller.start_session(duration_s=30,
                                                 authorized=True)
        self.assertFalse(upgraded["reused_existing"])
        self.assertNotEqual(upgraded["session"]["session_id"],
                            first["session"]["session_id"])
        self.assertTrue(upgraded["session"]["authorized"])

    def test_release_inputs_does_not_end_the_session(self):
        """The distinction that stop() was wrongly standing in for."""
        self.open_session()
        self.controller.ledger.hold("w")
        report = self.controller.release_inputs()
        self.assertTrue(report["clean"])
        self.assert_nothing_held()
        self.assertTrue(self.controller.sessions.is_active())
        self.assertEqual(self.controller._guard(), "")


# ── Validation happens before any input ──────────────────────────────────────

class TestValidation(_ControllerCase):

    def test_an_unknown_direction_sends_nothing(self):
        self.open_session()
        for bad in ("up", "northwest", "", "forwards ", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAction):
                    self.controller.move({"direction": bad, "duration": 0.1})
        self.assertEqual(self.backend.events, [])

    def test_a_nonsense_duration_sends_nothing(self):
        self.open_session()
        for bad in ("soon", None, -1, 0, float("inf"), float("nan"), True):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAction):
                    self.controller.move({"direction": "forward", "duration": bad})
        self.assertEqual(self.backend.events, [])

    def test_a_zero_look_is_refused(self):
        self.open_session()
        with self.assertRaises(InvalidAction):
            self.controller.look({"dx": 0, "dy": 0})
        self.assertEqual(self.backend.mouse_moves(), [])

    def test_jump_takes_no_parameters(self):
        self.open_session()
        with self.assertRaises(InvalidAction):
            self.controller.jump({"duration": 10})
        self.assertEqual(self.backend.events, [])


if __name__ == "__main__":
    unittest.main()


# ── move_and_jump: a new input path, held to every existing rule ─────────────

class TestMoveAndJump(_ControllerCase):
    """Walking and jumping together is the only way onto a one-block ledge.
    It is also the one genuinely new way this session presses keys, so it
    gets the whole set of guarantees the older actions have."""

    def test_it_is_refused_without_a_session(self):
        result = self.controller.move_and_jump({"direction": "forward"})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.events, [], "input was sent with no session")

    def test_it_presses_exactly_a_movement_key_and_space(self):
        self.open_session()
        result = self.controller.move_and_jump({"direction": "forward",
                                                "duration": 0.15})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(sorted(self.backend.downs()), ["space", "w"])
        self.assert_nothing_held()

    def test_there_is_no_way_to_name_another_key(self):
        """Direction comes from the movement table; the jump key is a
        constant. A caller that tries to name a key gets refused, not
        obeyed."""
        self.open_session()
        for sneaky in ({"direction": "space"}, {"direction": "e"},
                       {"direction": "escape"}, {"direction": "/"}):
            with self.subTest(params=sneaky):
                self.backend.events.clear()
                # The same convention as `move`: bad parameters raise and
                # send nothing, and the adapter turns that into a refusal.
                with self.assertRaises(InvalidAction):
                    self.controller.move_and_jump(sneaky)
                self.assertEqual(self.backend.downs(), [])
        self.backend.events.clear()
        self.controller.move_and_jump({"direction": "forward", "key": "e",
                                       "keys": ["t", "/"], "duration": 0.1})
        self.assertEqual(sorted(self.backend.downs()), ["space", "w"],
                         "an extra parameter reached the keyboard")

    def test_it_is_bounded(self):
        self.open_session()
        started = time.monotonic()
        result = self.controller.move_and_jump({"direction": "forward",
                                                "duration": 30})
        self.assertLess(time.monotonic() - started, 1.6)
        self.assertTrue(result.clamped)
        self.assert_nothing_held()

    def test_focus_loss_stops_it_and_releases_both_keys(self):
        self.open_session()

        def _steal_focus():
            time.sleep(0.1)
            self.locator.foreground = False

        threading.Thread(target=_steal_focus, daemon=True).start()
        result = self.controller.move_and_jump({"direction": "forward",
                                                "duration": 1.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "focus_lost")
        self.assert_nothing_held("focus loss left a key down mid-hop")

    def test_a_backend_failure_mid_hop_still_releases(self):
        self.open_session()
        self.backend._fail_on = "key_down"
        result = self.controller.move_and_jump({"direction": "forward",
                                                "duration": 0.2})
        self.assertFalse(result.ok)
        self.assert_nothing_held()

    def test_it_needs_only_the_movement_grant(self):
        """Not a new permission: both keys are ones `move` and `jump` already
        press under the same capability."""
        from actions import minecraft as adapter
        from core import capabilities
        self.assertEqual(adapter._mc_capability({"action": "move_and_jump"}),
                         capabilities.MINECRAFT_MOVEMENT)
