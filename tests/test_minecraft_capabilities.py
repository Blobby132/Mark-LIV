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
        for capability in (core_caps.MINECRAFT_INVENTORY,
                           core_caps.MINECRAFT_CHAT,
                           core_caps.MINECRAFT_LAUNCH):
            with self.subTest(capability=capability):
                self.assertTrue(core_caps.is_known(capability))
                self.assertFalse(mc_phase.is_enabled(capability))
                self.assertTrue(mc_phase.why_disabled(capability))

    def test_a_disabled_capability_would_still_confirm_if_it_were_enabled(self):
        # Defence in depth: removing the phase gate must not silently make
        # these free.
        for capability in (core_caps.MINECRAFT_INVENTORY,
                           core_caps.MINECRAFT_LAUNCH,
                           core_caps.MINECRAFT_CHAT):
            with self.subTest(capability=capability):
                self.assertTrue(core_caps.requires_confirmation(capability))

    def test_stop_is_always_allowed(self):
        # A safety control that policy can refuse is not a safety control.
        self.assertEqual(core_caps.decision_for(core_caps.MINECRAFT_STOP),
                         core_caps.ALLOW)
        self.assertIn(core_caps.MINECRAFT_STOP, mc_phase.ENABLED)


class TestWorldChangingActionsNeedAGrant(unittest.TestCase):
    """The replacement for the CONFIRM verdict attack used to carry.

    In Phase 2, `minecraft.attack` was CONFIRM because it was unimplemented.
    Building it made a per-call confirmation actively harmful — breaking one
    log takes several swings, so CONFIRM meant a dialog per swing, and a
    dialog per swing teaches people to dismiss dialogs unread.

    So the consent moved to the session and got more specific. These tests
    assert that it actually moved, rather than evaporated: the verdict is now
    ALLOW, and the thing standing between a model and your world is
    `Session.allow_interaction`. If someone ever deletes that check, this
    fails."""

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

    def test_the_verdict_alone_does_not_make_attack_reachable(self):
        self.assertEqual(core_caps.decision_for(core_caps.MINECRAFT_ATTACK),
                         core_caps.ALLOW)
        controller, backend = self._controller()
        try:
            controller.start_session(duration_s=30)      # no grant asked for
            result = controller.attack({"duration": 0.05})
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_reason, "interaction_not_granted")
            # And no button was pressed at all — refused before the input,
            # not after it.
            self.assertEqual(backend.button_downs(), [])
        finally:
            controller.stop("test")

    def test_use_item_needs_the_same_grant(self):
        controller, backend = self._controller()
        try:
            controller.start_session(duration_s=30)
            result = controller.use_item({"duration": 0.05})
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_reason, "interaction_not_granted")
            self.assertEqual(backend.button_downs(), [])
        finally:
            controller.stop("test")

    def test_a_granted_session_allows_it_and_still_releases(self):
        controller, backend = self._controller()
        try:
            controller.start_session(duration_s=30, allow_interaction=True)
            result = controller.attack({"duration": 0.05})
            self.assertTrue(result.ok, result.error)
            self.assertEqual(backend.button_downs(), ["left"])
            self.assertEqual(backend.button_ups(), ["left"])
            self.assertEqual(controller.ledger.held(), frozenset())
        finally:
            controller.stop("test")

    def test_movement_does_not_need_the_grant(self):
        """The grant is about changing the world, not about control. Walking
        in a session that did not ask for interaction must still work."""
        controller, _ = self._controller()
        try:
            controller.start_session(duration_s=30)
            result = controller.move({"direction": "forward",
                                      "duration": 0.05})
            self.assertTrue(result.ok, result.error)
        finally:
            controller.stop("test")

    def test_the_default_session_does_not_grant_interaction(self):
        """A caller who forgets the flag gets the safe session."""
        from minecraft.session import SessionManager
        session = SessionManager().start(duration_s=30)
        self.assertFalse(session.allow_interaction)


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
