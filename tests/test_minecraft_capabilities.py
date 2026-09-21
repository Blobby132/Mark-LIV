"""
Capability tests — the central broker decides, and this phase only narrows.

The property these protect: `minecraft/capabilities.py` can refuse something
`core/capabilities.py` allows, and can never permit something it refuses.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audit, capabilities as core_caps, confirm, permissions  # noqa: E402
from minecraft import capabilities as mc_phase                           # noqa: E402


def _settle(timeout: float = 5.0) -> None:
    for thread in list(threading.enumerate()):
        if thread.name.startswith("confirm-") and thread.is_alive():
            thread.join(timeout=timeout)


class TestCentralTableOwnsTheVerdicts(unittest.TestCase):

    def test_the_phase_list_is_a_subset_of_the_central_table(self):
        declared = set(core_caps.in_namespace("minecraft"))
        self.assertTrue(mc_phase.ENABLED <= declared,
                        f"enabled capabilities not in the central table: "
                        f"{mc_phase.ENABLED - declared}")

    def test_the_phase_gate_cannot_widen_a_verdict(self):
        # Every enabled capability still gets its verdict from the core table,
        # and the phase module has no way to change one.
        for capability in mc_phase.ENABLED:
            with self.subTest(capability=capability):
                self.assertTrue(core_caps.is_known(capability))
        self.assertFalse(hasattr(mc_phase, "enable"))
        self.assertFalse(hasattr(mc_phase, "allow"))
        with self.assertRaises(AttributeError):
            mc_phase.ENABLED.add(core_caps.MINECRAFT_COMMAND)

    def test_the_declared_verdicts_are_what_the_brief_asked_for(self):
        expected = {
            core_caps.MINECRAFT_OBSERVE:         core_caps.ALLOW,
            core_caps.MINECRAFT_READ_STATE:      core_caps.ALLOW,
            core_caps.MINECRAFT_CONTROL_SESSION: core_caps.CONFIRM,
            core_caps.MINECRAFT_MOVE:            core_caps.ALLOW,
            core_caps.MINECRAFT_LOOK:            core_caps.ALLOW,
            core_caps.MINECRAFT_COMMAND:         core_caps.DENY,
        }
        for capability, verdict in expected.items():
            with self.subTest(capability=capability):
                self.assertEqual(core_caps.decision_for(capability), verdict)

    def test_commands_are_denied_and_cannot_be_enabled(self):
        self.assertEqual(core_caps.decision_for(core_caps.MINECRAFT_COMMAND),
                         core_caps.DENY)
        self.assertNotIn(core_caps.MINECRAFT_COMMAND, mc_phase.ENABLED)
        # Even if the phase gate were changed, DENY is DENY.
        self.assertFalse(
            core_caps.requires_confirmation(core_caps.MINECRAFT_COMMAND),
            "a DENY capability must not be offered as a confirmation")

    def test_the_unbuilt_capabilities_are_declared_but_not_enabled(self):
        # attack and use_item left this list in Phase 3 — they are built now.
        # What replaced their phase-gate protection is asserted below, in
        # TestWorldChangingActionsNeedAGrant.
        for capability in (core_caps.MINECRAFT_CHAT,
                           core_caps.MINECRAFT_LAUNCH):
            with self.subTest(capability=capability):
                self.assertTrue(core_caps.is_known(capability))
                self.assertFalse(mc_phase.is_enabled(capability))
                self.assertTrue(mc_phase.why_disabled(capability))

    def test_a_disabled_capability_would_still_confirm_if_it_were_enabled(self):
        # Defence in depth: removing the phase gate must not silently make
        # these free.
        for capability in (core_caps.MINECRAFT_LAUNCH,
                           core_caps.MINECRAFT_CHAT):
            with self.subTest(capability=capability):
                self.assertTrue(core_caps.requires_confirmation(capability))

    def test_stop_is_always_allowed(self):
        # A safety control that policy can refuse is not a safety control.
        self.assertEqual(core_caps.decision_for(core_caps.MINECRAFT_STOP),
                         core_caps.ALLOW)
        self.assertIn(core_caps.MINECRAFT_STOP, mc_phase.ENABLED)


class TestOneConfirmationCoversGameplay(unittest.TestCase):
    """The Phase 4 consent model, and the thing that makes it safe.

    There is exactly ONE confirmation in this namespace: minecraft.control.
    Approving it authorises a session, and that session's grant — not the
    capability verdicts — is what stands between a model and your world.

    Every gameplay verdict is ALLOW, so if `Session.covers` were ever deleted
    the whole subsystem would be wide open. These tests are what would notice."""

    def _controller(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_minecraft_controller import FakeLocator, FakeProcess
        from minecraft.controller import MinecraftController
        from minecraft.input_backend import FakeInputBackend
        from minecraft.session import SessionManager
        backend = FakeInputBackend()
        return MinecraftController(
            backend=backend, locator=FakeLocator(), sessions=SessionManager(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0.0), backend

    GAMEPLAY = [
        ("move", {"direction": "forward", "duration": 0.05}),
        ("jump", {}), ("look", {"dx": 30, "dy": 0}),
        ("sneak", {"duration": 0.05}), ("sprint", {"duration": 0.05}),
        ("attack", {"duration": 0.05}), ("mine", {"duration": 0.05}),
        ("place", {}), ("interact", {}), ("use_item", {}),
        ("eat", {"duration": 0.05}), ("drop", {}),
        ("hotbar_select", {"slot": 3}), ("inventory", {"state": "open"}),
    ]

    def test_one_confirmed_session_covers_every_gameplay_action(self):
        """The headline promise: approve once, then play."""
        controller, _ = self._controller()
        try:
            controller.start_session(duration_s=120)
            for action, params in self.GAMEPLAY:
                with self.subTest(action=action):
                    result = controller.execute_action(action, params)
                    self.assertTrue(result.ok,
                                    f"{action} refused: {result.error}")
        finally:
            controller.stop("test")

    def test_an_unauthorized_session_refuses_every_one_of_them(self):
        """And refuses before any input, not after."""
        controller, backend = self._controller()
        try:
            controller.sessions.start(duration_s=60, authorized=False)
            for action, params in self.GAMEPLAY:
                with self.subTest(action=action):
                    result = controller.execute_action(action, params)
                    self.assertFalse(result.ok)
                    self.assertEqual(result.stopped_reason, "not_authorized")
            self.assertEqual(backend.downs(), [])
            self.assertEqual(backend.button_downs(), [])
        finally:
            controller.stop("test")

    def test_the_gate_answers_before_parameters_are_validated(self):
        """An unauthorised caller gets one answer, whatever it passes.

        Validating first told them about a bad parameter instead of the
        missing authorisation — the less useful answer, and a way to probe the
        parameter rules of an action they may not take."""
        controller, backend = self._controller()
        try:
            controller.sessions.start(duration_s=60, authorized=False)
            for action, nonsense in (("place", {"duration": 99}),
                                     ("drop", {"all": True}),
                                     ("hotbar_select", {"slot": 42}),
                                     ("move", {"direction": "sideways"})):
                with self.subTest(action=action):
                    result = controller.execute_action(action, nonsense)
                    self.assertEqual(result.stopped_reason, "not_authorized")
            self.assertEqual(backend.downs(), [])
        finally:
            controller.stop("test")

    def test_the_grant_does_not_extend_to_chat_or_commands(self):
        """A session to play the game is not a session to talk to strangers
        or reach a command line. Membership, not a "minecraft." prefix."""
        from minecraft.session import SessionManager
        session = SessionManager().start(duration_s=60, authorized=True)
        for capability in (core_caps.MINECRAFT_CHAT,
                           core_caps.MINECRAFT_COMMAND,
                           core_caps.MINECRAFT_LAUNCH):
            with self.subTest(capability=capability):
                self.assertFalse(session.covers(capability))

    def test_the_default_session_is_not_authorized(self):
        """A caller who forgets the flag gets a session that can do nothing,
        rather than one that can do everything."""
        from minecraft.session import SessionManager
        session = SessionManager().start(duration_s=30)
        self.assertFalse(session.authorized)
        self.assertFalse(session.is_authorized())
        self.assertFalse(session.covers(core_caps.MINECRAFT_MOVEMENT))

    def test_an_expired_authorization_is_not_an_authorization(self):
        from minecraft.session import SessionManager
        session = SessionManager().start(duration_s=30, authorized=True)
        self.assertTrue(session.is_authorized())
        session.expires_at = 0.0
        self.assertFalse(session.is_authorized())
        self.assertFalse(session.covers(core_caps.MINECRAFT_MINING))

    def test_ending_the_session_revokes_the_authorization(self):
        controller, backend = self._controller()
        controller.start_session(duration_s=120)
        controller.stop("done")
        result = controller.execute_action("mine", {"duration": 0.05})
        self.assertFalse(result.ok)
        self.assertEqual(backend.button_downs(), [])

    def test_control_is_the_only_gameplay_confirmation(self):
        """If a second one ever appears, "approve once and play" is no longer
        true and this is where that gets noticed."""
        confirms = [c for c in core_caps.in_namespace("minecraft")
                    if core_caps.decision_for(c) == core_caps.CONFIRM]
        self.assertIn(core_caps.MINECRAFT_CONTROL, confirms)
        for capability in confirms:
            with self.subTest(capability=capability):
                self.assertNotIn(capability, mc_phase.ENABLED - {
                    core_caps.MINECRAFT_CONTROL},
                    f"{capability} is enabled AND confirms — that is a "
                    f"second confirmation during gameplay")


class TestSessionLifetime(unittest.TestCase):
    """A session that runs until stopped, and why that is still bounded."""

    def test_a_session_can_run_until_stopped(self):
        from minecraft.session import SessionManager, UNLIMITED
        session = SessionManager().start(duration_s=UNLIMITED,
                                         authorized=True)
        self.assertTrue(session.unlimited)
        self.assertFalse(session.expired)
        self.assertTrue(session.is_authorized())

    def test_an_unlimited_session_still_ends_on_stop(self):
        """The clock was never the only stop, and was always the weakest."""
        from minecraft.session import SessionManager, UNLIMITED
        manager = SessionManager()
        session = manager.start(duration_s=UNLIMITED, authorized=True)
        manager.end("stopped by the user")
        self.assertFalse(session.active)
        self.assertFalse(session.is_authorized())

    def test_an_unlimited_session_still_ends_on_cancel(self):
        from minecraft.session import SessionManager, UNLIMITED
        session = SessionManager().start(duration_s=UNLIMITED,
                                         authorized=True)
        session.cancel.set()
        self.assertFalse(session.active)

    def test_f12_still_revokes_an_unlimited_session(self):
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_minecraft_controller import FakeLocator, FakeProcess
        from minecraft.controller import MinecraftController
        from minecraft.input_backend import FakeInputBackend
        from minecraft.session import SessionManager, UNLIMITED

        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), sessions=SessionManager(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0.0)
        controller.start_session(duration_s=UNLIMITED)
        self.assertTrue(controller.execute_action(
            "mine", {"duration": 0.05}).ok)

        controller.emergency_stop("F12")
        self.assertFalse(controller.execute_action(
            "mine", {"duration": 0.05}).ok)

    def test_a_timed_session_still_expires(self):
        """Asking for a limit must still give one."""
        from minecraft.session import SessionManager
        session = SessionManager().start(duration_s=30, authorized=True)
        self.assertFalse(session.unlimited)
        session.expires_at = 0.0
        self.assertTrue(session.expired)
        self.assertFalse(session.is_authorized())

    def test_the_banner_says_which_kind_it_is(self):
        """Session-level consent is only better than per-action consent when
        the person knows what they agreed to — including how long it lasts."""
        from actions.minecraft import _mc_guard
        unlimited = _mc_guard({"action": "start_session"})
        self.assertIn("until you stop it", unlimited["summary"])
        self.assertIn("no timer", unlimited["detail"])

        timed = _mc_guard({"action": "start_session", "duration_s": 120})
        self.assertIn("120 seconds", timed["summary"])
        self.assertNotIn("no timer", timed["detail"])


class TestToolRefusesDisabledCapabilities(unittest.TestCase):
    """Through the real adapter, so the refusal is the one a model would get."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        audit.configure(path=Path(self._tmp.name) / "a.jsonl")
        permissions.install()
        confirm.bind(show=None, hide=None, log=None)
        confirm._pending = None
        from actions import minecraft as mc_action
        self.mc = mc_action

    def tearDown(self):
        confirm.bind(show=None, hide=None, log=None)
        confirm._pending = None
        audit.configure(path=audit.DEFAULT_DIR / audit.DEFAULT_FILENAME)
        self._tmp.cleanup()

    def test_command_is_refused_before_anything_happens(self):
        result = self.mc.minecraft_control({"action": "command",
                                            "text": "/gamemode creative"})
        self.assertIn("refused permanently", result)

    def test_every_unbuilt_action_is_refused_with_a_reason(self):
        for action in ("attack", "mine", "use_item", "place", "inventory",
                       "chat", "say", "launch"):
            with self.subTest(action=action):
                result = self.mc.minecraft_control({"action": action})
                self.assertTrue(len(result) > 20,
                                "a refusal with no explanation")
                self.assertNotIn("Traceback", result)

    def test_an_unknown_action_resolves_to_the_strictest_capability(self):
        self.assertEqual(self.mc._mc_capability({"action": "rm -rf /"}),
                         core_caps.MINECRAFT_COMMAND)
        self.assertEqual(core_caps.decision_for(
            self.mc._mc_capability({"action": "anything_at_all"})),
            core_caps.DENY)

    def test_observe_and_status_need_no_confirmation(self):
        for action in ("status", "observe", "read_state", "stop"):
            with self.subTest(action=action):
                capability = self.mc._mc_capability({"action": action})
                self.assertFalse(core_caps.requires_confirmation(capability))

    def test_starting_a_session_needs_confirmation(self):
        capability = self.mc._mc_capability({"action": "start_session"})
        self.assertTrue(core_caps.requires_confirmation(capability))

    def test_the_confirmation_banner_says_what_it_grants(self):
        hints = self.mc._mc_guard({"action": "start_session", "duration_s": 120})
        blob = " ".join(str(v) for v in hints.values()).lower()
        self.assertIn("minecraft", blob)
        self.assertIn("120", str(hints))
        for promise in ("alt-tab", "f12", "chat", "commands"):
            self.assertIn(promise, blob, f"the banner does not mention {promise}")

    def test_the_banner_duration_is_clamped_like_the_session(self):
        hints = self.mc._mc_guard({"action": "start_session", "duration_s": 99999})
        self.assertIn("300", str(hints["summary"]))


if __name__ == "__main__":
    unittest.main()
