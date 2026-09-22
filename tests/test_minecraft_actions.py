"""
Tests for the Phase 3 action vocabulary: attack, use_item, hotbar, sneak,
sprint.

Two properties matter here and everything else is detail:

  1. Every action is bounded, and the bound cannot be raised by a parameter.
  2. Nothing reaches the game without a live session — and for the two that
     change the world, without a session that asked for that specifically.

The mouse-button tests get particular attention. A held left-click that
outlives focus is worse than a held W: it lands on whatever window is now in
front, and clicks things.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import action_spec                                      # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.errors import InvalidAction                             # noqa: E402
from minecraft.input_backend import (                                  # noqa: E402
    ALLOWED_BUTTONS, FakeInputBackend, validate_button,
)
from minecraft.ledger import InputLedger, button_token                 # noqa: E402
from minecraft.session import SessionManager                           # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from test_minecraft_controller import FakeLocator, FakeProcess         # noqa: E402


class _Case(unittest.TestCase):

    def setUp(self):
        self.backend = FakeInputBackend()
        self.locator = FakeLocator()
        self.sessions = SessionManager()
        self.controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=self.sessions, process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0)

    def tearDown(self):
        try:
            self.controller.stop("teardown")
        except Exception:
            pass

    def open(self, authorized=True, duration=60.0):
        return self.controller.start_session(duration_s=duration,
                                             authorized=authorized)

    def assert_nothing_held(self):
        self.assertEqual(self.controller.ledger.held(), frozenset())
        unmatched = [b for b in self.backend.button_downs()
                     if b not in self.backend.button_ups()]
        self.assertEqual(unmatched, [],
                         f"buttons pressed but never released: {unmatched}")


# ── Bounds ───────────────────────────────────────────────────────────────────

class TestActionsAreBounded(unittest.TestCase):

    def test_attack_is_clamped_to_its_maximum(self):
        spec = action_spec.parse_attack({"duration": 60})
        self.assertEqual(spec.duration, action_spec.MAX_ATTACK_DURATION_S)
        self.assertEqual(spec.requested_duration, 60)
        self.assertTrue(spec.clamped)

    def test_use_item_is_clamped_to_its_maximum(self):
        spec = action_spec.parse_use_item({"duration": 99})
        self.assertEqual(spec.duration, action_spec.MAX_USE_DURATION_S)
        self.assertTrue(spec.clamped)

    def test_sneak_and_sprint_are_clamped(self):
        for parse, ceiling in ((action_spec.parse_sneak,
                                action_spec.MAX_SNEAK_DURATION_S),
                               (action_spec.parse_sprint,
                                action_spec.MAX_SPRINT_DURATION_S)):
            with self.subTest(parse=parse.__name__):
                spec = parse({"duration": 30, "direction": "forward"})
                self.assertEqual(spec.duration, ceiling)
                self.assertTrue(spec.clamped)

    def test_no_parameter_raises_the_ceiling(self):
        """The bound is not negotiable. Anything that looks like an override
        must be ignored rather than honoured."""
        spec = action_spec.parse_attack({
            "duration": 10, "max_duration": 10, "unbounded": True,
            "force": True, "limit": 999,
        })
        self.assertEqual(spec.duration, action_spec.MAX_ATTACK_DURATION_S)

    def test_a_negative_or_zero_duration_is_refused(self):
        for bad in (0, -1, -0.5):
            with self.subTest(duration=bad):
                with self.assertRaises(InvalidAction):
                    action_spec.parse_attack({"duration": bad})

    def test_a_nonsense_duration_is_refused(self):
        for bad in ("soon", None, [1], {"a": 1}, True, float("inf"),
                    float("nan")):
            with self.subTest(duration=repr(bad)):
                with self.assertRaises(InvalidAction):
                    action_spec.parse_attack({"duration": bad})

    def test_every_bound_is_under_the_ledger_deadman(self):
        """Any action able to outlast the deadman timeout would be
        force-released mid-swing and reported as a failure it did not cause."""
        from minecraft.ledger import MAX_HOLD_SECONDS
        for name, value in action_spec.limits().items():
            if name.startswith("max_") and name.endswith("_duration_s"):
                with self.subTest(limit=name):
                    self.assertLess(value, MAX_HOLD_SECONDS)


class TestAttackTakesNoTarget(unittest.TestCase):

    def test_naming_a_target_is_refused_with_an_explanation(self):
        """Minecraft hits whatever is under the crosshair. Accepting a target
        would imply an aiming capability that does not exist, and the planner
        would stop aiming."""
        for key in ("target", "block", "entity", "at", "position"):
            with self.subTest(param=key):
                with self.assertRaises(InvalidAction) as caught:
                    action_spec.parse_attack({key: "oak_log"})
                self.assertIn("crosshair", str(caught.exception))

    def test_use_item_refuses_an_item_name(self):
        with self.assertRaises(InvalidAction):
            action_spec.parse_use_item({"item": "dirt"})


class TestHotbar(unittest.TestCase):

    def test_slots_one_to_nine_are_accepted(self):
        for slot in range(1, 10):
            with self.subTest(slot=slot):
                self.assertEqual(action_spec.parse_hotbar({"slot": slot}).slot,
                                 slot)

    def test_out_of_range_slots_are_refused_not_clamped(self):
        """Clamping is right for a duration and wrong here: asking for slot 12
        and silently getting slot 9 leaves the planner believing it holds
        something it does not, and every later action reasons from that."""
        for slot in (0, 10, 12, -1, 99):
            with self.subTest(slot=slot):
                with self.assertRaises(InvalidAction):
                    action_spec.parse_hotbar({"slot": slot})

    def test_a_missing_slot_is_refused(self):
        with self.assertRaises(InvalidAction):
            action_spec.parse_hotbar({})

    def test_a_non_numeric_slot_is_refused(self):
        for bad in ("first", None, [1], True):
            with self.subTest(slot=repr(bad)):
                with self.assertRaises(InvalidAction):
                    action_spec.parse_hotbar({"slot": bad})


class TestDirections(unittest.TestCase):

    def test_sprint_defaults_to_forward(self):
        self.assertEqual(action_spec.parse_sprint({}).detail["direction"],
                         "forward")

    def test_sneak_without_a_direction_is_crouching_in_place(self):
        spec = action_spec.parse_sneak({})
        self.assertEqual(spec.keys, (action_spec.SNEAK_KEY,))
        self.assertIsNone(spec.detail["direction"])

    def test_an_invalid_direction_is_refused(self):
        for parse in (action_spec.parse_sneak, action_spec.parse_sprint):
            with self.subTest(parse=parse.__name__):
                with self.assertRaises(InvalidAction):
                    parse({"direction": "upwards"})


# ── Mouse buttons ────────────────────────────────────────────────────────────

class TestButtonVocabulary(unittest.TestCase):

    def test_only_left_and_right_exist(self):
        self.assertEqual(ALLOWED_BUTTONS, frozenset({"left", "right"}))

    def test_middle_click_is_refused_with_a_reason(self):
        with self.assertRaises(InvalidAction):
            validate_button("middle")

    def test_an_invented_button_is_refused(self):
        for bad in ("x1", "thumb", "any", "", None, 3):
            with self.subTest(button=repr(bad)):
                with self.assertRaises(InvalidAction):
                    validate_button(bad)


class TestButtonsAreLedgered(unittest.TestCase):
    """A held button must be tracked exactly like a held key, because the
    failure mode is worse: a click that outlives focus lands on another
    window."""

    def test_a_held_button_appears_in_the_ledger(self):
        backend = FakeInputBackend()
        ledger = InputLedger(backend)
        ledger.hold_button("left")
        self.assertIn(button_token("left"), ledger.held())

    def test_release_all_releases_buttons_and_keys_together(self):
        backend = FakeInputBackend()
        ledger = InputLedger(backend)
        ledger.hold("w")
        ledger.hold_button("left")
        report = ledger.release_all()
        self.assertTrue(report.clean)
        self.assertEqual(ledger.held(), frozenset())
        self.assertEqual(backend.button_ups(), ["left"])
        self.assertEqual(backend.ups(), ["w"])

    def test_a_button_counts_towards_the_deadman_timeout(self):
        ledger = InputLedger(FakeInputBackend())
        ledger.hold_button("left")
        self.assertTrue(ledger.overdue(limit=-1.0))

    def test_holding_the_same_button_twice_records_one_entry(self):
        backend = FakeInputBackend()
        ledger = InputLedger(backend)
        ledger.hold_button("left")
        ledger.hold_button("left")
        self.assertEqual(len(ledger.held()), 1)
        self.assertEqual(backend.button_downs(), ["left"])


# ── Through the controller ───────────────────────────────────────────────────

class TestNothingActsWithoutASession(_Case):

    def test_every_new_action_is_refused_with_no_session(self):
        for name, params in (("attack", {"duration": 0.05}),
                             ("use_item", {"duration": 0.05}),
                             ("sneak", {"duration": 0.05}),
                             ("sprint", {"duration": 0.05}),
                             ("hotbar_select", {"slot": 3})):
            with self.subTest(action=name):
                result = getattr(self.controller, name)(params)
                self.assertFalse(result.ok)
                self.assertEqual(result.stopped_reason, "no_session")
        self.assertEqual(self.backend.button_downs(), [])
        self.assertEqual(self.backend.downs(), [])

    def test_attack_needs_an_authorized_session(self):
        self.open(authorized=False)
        result = self.controller.attack({"duration": 0.05})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "not_authorized")
        self.assertEqual(self.backend.button_downs(), [])


class TestActionsReleaseEverything(_Case):

    def test_attack_releases_the_button(self):
        self.open()
        result = self.controller.attack({"duration": 0.05})
        self.assertTrue(result.ok, result.error)
        self.assert_nothing_held()

    def test_sprint_releases_both_keys(self):
        self.open()
        result = self.controller.sprint({"duration": 0.05,
                                         "direction": "forward"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.controller.ledger.held(), frozenset())
        for key in ("ctrl", "w"):
            self.assertIn(key, self.backend.ups())

    def test_losing_focus_mid_attack_releases_the_button(self):
        """The one that matters. A click held past focus loss clicks on
        whatever is in front."""
        self.open()
        self.locator.foreground = False
        result = self.controller.attack({"duration": 1.0})
        self.assertFalse(result.ok)
        self.assert_nothing_held()

    def test_an_invalid_action_presses_nothing_at_all(self):
        self.open()
        with self.assertRaises(InvalidAction):
            self.controller.hotbar_select({"slot": 99})
        self.assertEqual(self.backend.downs(), [])
        self.assert_nothing_held()

    def test_hotbar_select_taps_the_number_key(self):
        self.open()
        result = self.controller.hotbar_select({"slot": 4})
        self.assertTrue(result.ok, result.error)
        self.assertIn("4", self.backend.downs())
        self.assert_nothing_held()

    def test_toggle_debug_overlay_taps_f3(self):
        self.open()
        result = self.controller.toggle_debug_overlay()
        self.assertTrue(result.ok, result.error)
        self.assertIn("f3", self.backend.downs())
        self.assert_nothing_held()


class TestPhase4Actions(_Case):
    """Mining, building, interacting, items and the inventory."""

    def test_every_new_action_works_in_one_authorized_session(self):
        self.open()
        for action, params in (("mine", {"duration": 0.05}),
                               ("place", {}), ("interact", {}),
                               ("eat", {"duration": 0.05}), ("drop", {}),
                               ("inventory", {"state": "open"}),
                               ("inventory", {"state": "close"})):
            with self.subTest(action=action):
                result = self.controller.execute_action(action, params)
                self.assertTrue(result.ok, result.error)
        self.assert_nothing_held()

    def test_mining_holds_the_attack_button(self):
        self.open()
        self.controller.mine({"duration": 0.05})
        self.assertEqual(self.backend.button_downs(), ["left"])
        self.assertEqual(self.backend.button_ups(), ["left"])

    def test_placing_uses_the_other_button_and_is_a_tap(self):
        self.open()
        result = self.controller.place({})
        self.assertEqual(self.backend.button_downs(), ["right"])
        self.assertLess(result.actual_duration_ms, 400)

    def test_placing_refuses_a_duration(self):
        """A held right-click places block after block as the view drifts —
        an agent asked for one block would build a trail of them."""
        self.open()
        with self.assertRaises(InvalidAction):
            self.controller.place({"duration": 2.0})
        self.assertEqual(self.backend.button_downs(), [])

    def test_dropping_a_whole_stack_is_not_available(self):
        self.open()
        with self.assertRaises(InvalidAction):
            self.controller.drop({"all": True})
        self.assertEqual(self.backend.downs(), [])

    def test_opening_and_closing_the_inventory_use_different_keys(self):
        """Close is ESC, not E again. If the inventory state were misread,
        a toggle would open it exactly when we wanted it shut; ESC does
        nothing when nothing is open, which fails harmlessly."""
        self.open()
        self.controller.inventory({"state": "open"})
        self.controller.inventory({"state": "close"})
        self.assertIn("e", self.backend.downs())
        self.assertIn("esc", self.backend.downs())

    def test_an_invalid_inventory_state_presses_nothing(self):
        self.open()
        with self.assertRaises(InvalidAction):
            self.controller.inventory({"state": "sideways"})
        self.assertEqual(self.backend.downs(), [])

    def test_mining_is_bounded_like_everything_else(self):
        spec = action_spec.parse_mine({"duration": 30})
        self.assertEqual(spec.duration, action_spec.MAX_MINE_DURATION_S)
        self.assertTrue(spec.clamped)

    def test_one_mine_can_actually_break_a_log(self):
        """The bug this replaces: mining was capped at 2.0s "so that swings
        had to be observed between", which assumed damage accumulates across
        swings. It does not — Minecraft resets breaking progress the instant
        the button comes up, so every short swing started from zero and
        nothing ever broke. It presented as hitting too weakly.

        An oak log needs about three continuous seconds by hand."""
        self.assertGreater(action_spec.MAX_MINE_DURATION_S, 3.0)
        self.assertGreaterEqual(action_spec.DEFAULT_MINE_DURATION_S, 3.0)

    def test_the_deadman_outlasts_the_longest_mine(self):
        """Otherwise the supervisor would cut every successful mine short and
        report it as a failure it caused itself."""
        from minecraft.ledger import MAX_HOLD_SECONDS
        self.assertGreater(MAX_HOLD_SECONDS, action_spec.MAX_MINE_DURATION_S)

    def test_only_mining_got_the_longer_bound(self):
        """Movement staying at two seconds is the point: mining's bound is set
        by the game, not by a general relaxation of caution."""
        self.assertEqual(action_spec.MAX_MOVE_DURATION_S, 2.0)
        self.assertEqual(action_spec.MAX_INTERACT_DURATION_S, 1.0)

    def test_losing_focus_mid_mine_releases_the_button(self):
        self.open()
        self.locator.foreground = False
        result = self.controller.mine({"duration": 2.0})
        self.assertFalse(result.ok)
        self.assert_nothing_held()


class TestMiningStopsWhenTheBlockGoes(unittest.TestCase):
    """Holding on after the block breaks wastes the rest of the budget and
    starts breaking whatever was revealed behind it."""

    def _controller(self, probe):
        self.backend = FakeInputBackend()
        self.locator = FakeLocator()
        controller = MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0, progress_probe=probe)
        controller.start_session(duration_s=120)
        return controller

    def test_it_releases_as_soon_as_the_target_changes(self):
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return "oak_log" if calls["n"] < 4 else "air"

        controller = self._controller(probe)
        try:
            result = controller.mine({"duration": 8.0})
            self.assertTrue(result.ok, result.error)
            # Far short of the eight seconds asked for.
            self.assertLess(result.actual_duration_ms, 3000)
            self.assertIn("stopped_early", result.requested)
        finally:
            controller.stop("test")

    def test_it_holds_the_full_time_when_nothing_changes(self):
        controller = self._controller(lambda: "bedrock")
        try:
            result = controller.mine({"duration": 0.3})
            self.assertTrue(result.ok, result.error)
            self.assertGreaterEqual(result.actual_duration_ms, 250)
            self.assertNotIn("stopped_early", result.requested)
        finally:
            controller.stop("test")

    def test_no_probe_means_a_plain_timed_hold(self):
        """Still correct, just wasteful — and the result does not pretend it
        stopped early."""
        controller = self._controller(None)
        try:
            result = controller.mine({"duration": 0.3})
            self.assertTrue(result.ok, result.error)
            self.assertNotIn("stopped_early", result.requested)
        finally:
            controller.stop("test")

    def test_a_probe_that_throws_does_not_break_the_mine(self):
        """It runs every 40ms inside the hold loop; an exception there would
        escape past the release."""
        def angry():
            raise RuntimeError("no state")

        controller = self._controller(angry)
        try:
            result = controller.mine({"duration": 0.3})
            self.assertTrue(result.ok, result.error)
            self.assertEqual(controller.ledger.held(), frozenset())
        finally:
            controller.stop("test")

    def test_the_button_still_comes_up_on_focus_loss_mid_mine(self):
        """The longer hold must not weaken the stop that matters most.

        Focus is dropped PART WAY THROUGH, not before — losing it beforehand
        is a pre-flight refusal that never presses anything, which proves
        nothing about releasing a button already down. That distinction is
        the whole point of the test, and getting it wrong the first time made
        it pass for the wrong reason."""
        controller = self._controller(lambda: "oak_log")
        try:
            original = self.locator.probe
            seen = {"n": 0}

            def lose_focus_midway():
                seen["n"] += 1
                if seen["n"] > 3:
                    self.locator.foreground = False
                return original()

            self.locator.probe = lose_focus_midway

            result = controller.mine({"duration": 8.0})
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_reason, "focus_lost")
            self.assertLess(result.actual_duration_ms, 2000)
            # The button went down and came back up.
            self.assertEqual(self.backend.button_downs(), ["left"])
            self.assertEqual(self.backend.button_ups(), ["left"])
            self.assertEqual(controller.ledger.held(), frozenset())
        finally:
            controller.stop("test")


class TestExecuteAction(_Case):
    """The single entry point the planner and the task runner both use."""

    def test_an_invented_action_is_refused_not_dispatched(self):
        self.open()
        for invented in ("press", "type", "key_down", "run", "chat",
                         "command", "exec", "", None):
            with self.subTest(action=invented):
                result = self.controller.execute_action(invented, {})
                self.assertFalse(result.ok)
                self.assertEqual(result.stopped_reason, "unknown_action")
        self.assertEqual(self.backend.downs(), [])
        self.assertEqual(self.backend.button_downs(), [])

    def test_the_action_table_holds_no_input_primitive(self):
        for forbidden in ("press", "type", "key_down", "key_up", "hotkey",
                          "send_keys", "button_down", "move_mouse_relative",
                          "chat", "command"):
            self.assertNotIn(forbidden, MinecraftController.ACTIONS)

    def test_every_action_in_the_table_exists(self):
        for action, method in MinecraftController.ACTIONS.items():
            with self.subTest(action=action):
                self.assertTrue(hasattr(self.controller, method))


class TestEmergencyStop(_Case):
    """F12's code path, under the name the rest of the system uses."""

    def test_emergency_stop_releases_everything(self):
        self.open()
        self.controller.ledger.hold("w")
        self.controller.ledger.hold_button("left")
        report = self.controller.emergency_stop()
        self.assertTrue(report["stopped"])
        self.assertTrue(report["clean"])
        self.assert_nothing_held()

    def test_emergency_stop_revokes_the_authorization(self):
        """One confirmation grants gameplay; F12 takes it back. The next
        action must need a fresh confirmation, not resume quietly."""
        self.open()
        self.controller.emergency_stop()
        result = self.controller.execute_action("mine", {"duration": 0.05})
        self.assertFalse(result.ok)
        self.assertEqual(self.backend.button_downs(), [])

    def test_emergency_stop_works_with_nothing_running(self):
        """It must never refuse, never raise, and never need a session."""
        report = self.controller.emergency_stop()
        self.assertTrue(report["stopped"])

    def test_emergency_stop_is_idempotent(self):
        self.open()
        for _ in range(3):
            self.assertTrue(self.controller.emergency_stop()["stopped"])


class TestFocusGrace(unittest.TestCase):
    """Confirming a session takes focus away from Minecraft — the dialog is a
    JARVIS window. Without a grace period the first action after every
    confirmation was refused, which made the feature unusable while the guard
    was technically right every time.

    Waiting is not relaxing: nothing is sent unless the guard passes."""

    def _controller(self, wait):
        self.backend = FakeInputBackend()
        self.locator = FakeLocator()
        return MinecraftController(
            backend=self.backend, locator=self.locator,
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=wait)

    def test_an_action_waits_for_focus_to_come_back(self):
        controller = self._controller(wait=2.0)
        try:
            controller.start_session(duration_s=60)
            self.locator.foreground = False

            # Focus returns after a few probes, as it would when the user
            # clicks back on the game.
            probes = {"n": 0}
            original = self.locator.probe

            def probe_then_focus():
                probes["n"] += 1
                if probes["n"] > 3:
                    self.locator.foreground = True
                return original()

            self.locator.probe = probe_then_focus
            result = controller.move({"direction": "forward",
                                      "duration": 0.05})
            self.assertTrue(result.ok, result.error)
            self.assertIn("w", self.backend.downs())
        finally:
            controller.stop("test")

    def test_nothing_is_sent_if_focus_never_returns(self):
        """The guard still has the final say — the wait only postpones it."""
        controller = self._controller(wait=0.3)
        try:
            controller.start_session(duration_s=60)
            self.locator.foreground = False
            result = controller.move({"direction": "forward",
                                      "duration": 0.5})
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_reason, "focus_lost")
            self.assertEqual(self.backend.downs(), [])
        finally:
            controller.stop("test")

    def test_conditions_waiting_cannot_fix_fail_immediately(self):
        """A missing session is not going to fix itself in four seconds, and
        pausing on it would only make the refusal slower."""
        import time as _time
        controller = self._controller(wait=5.0)
        started = _time.monotonic()
        result = controller.move({"direction": "forward", "duration": 0.05})
        elapsed = _time.monotonic() - started
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "no_session")
        self.assertLess(elapsed, 1.0, "it waited on a condition waiting "
                                      "cannot fix")

    def test_focus_lost_during_an_action_still_stops_it(self):
        """The grace period is only before an action starts. Once keys are
        down, losing focus must still stop within a tick."""
        controller = self._controller(wait=2.0)
        try:
            controller.start_session(duration_s=60)
            original = self.locator.probe
            calls = {"n": 0}

            def lose_focus_midway():
                calls["n"] += 1
                if calls["n"] > 2:
                    self.locator.foreground = False
                return original()

            self.locator.probe = lose_focus_midway
            result = controller.move({"direction": "forward", "duration": 2.0})
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_reason, "focus_lost")
            self.assertLess(result.actual_duration_ms, 2000)
            self.assertEqual(controller.ledger.held(), frozenset())
        finally:
            controller.stop("test")


if __name__ == "__main__":
    unittest.main()
