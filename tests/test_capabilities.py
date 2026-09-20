"""
Tests for core/capabilities.py — the policy table itself.

These assert the *shape* of the policy (fail-closed, immutable, complete) and
the handful of verdicts that the brief named explicitly. They deliberately do
not assert every row: the table is meant to be edited, and a test that simply
restates it would only ever break in step with it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import capabilities as caps                           # noqa: E402


class TestFailClosed(unittest.TestCase):

    def test_unknown_capability_is_denied(self):
        for unknown in (
            "delete_everything",
            "file_delte",                 # a typo, the realistic case
            "read_only ",                 # trailing space
            "READ_ONLY",                  # wrong case
            "",
        ):
            with self.subTest(unknown=unknown):
                self.assertEqual(caps.decision_for(unknown), caps.DENY)
                self.assertFalse(caps.is_known(unknown))

    def test_non_string_capability_is_denied(self):
        for weird in (None, 42, object(), ["file_delete"], {"x": 1}, True):
            with self.subTest(weird=type(weird).__name__):
                self.assertEqual(caps.decision_for(weird), caps.DENY)

    def test_a_capability_invented_at_runtime_stays_denied(self):
        # The failure mode this guards: a new action file naming a capability
        # nobody added to the table, and getting a free pass for it.
        self.assertEqual(caps.decision_for("minecraft_place_block"), caps.DENY)


class TestPolicyIsNotEditable(unittest.TestCase):

    def test_policy_mapping_rejects_assignment(self):
        with self.assertRaises(TypeError):
            caps.POLICY["file_delete"] = caps.ALLOW

    def test_policy_mapping_rejects_deletion(self):
        with self.assertRaises(TypeError):
            del caps.POLICY["file_delete"]

    def test_policy_has_no_setter_function(self):
        # There must be no public way to change a verdict at runtime. If one is
        # ever added, this test is the place that argues about it.
        setters = [
            name for name in dir(caps)
            if not name.startswith("_")
            and callable(getattr(caps, name))
            and any(word in name.lower()
                    for word in ("set", "update", "configure", "override",
                                 "register", "add", "allow_"))
        ]
        self.assertEqual(setters, [], f"capabilities exposes mutators: {setters}")

    def test_all_capabilities_is_immutable(self):
        self.assertIsInstance(caps.ALL_CAPABILITIES, frozenset)


class TestTableIntegrity(unittest.TestCase):

    def test_every_verdict_is_a_known_verdict(self):
        for capability, verdict in caps.POLICY.items():
            with self.subTest(capability=capability):
                self.assertIn(verdict, caps.VERDICTS)

    def test_every_public_capability_constant_is_in_the_table(self):
        # Catches a constant defined but never given a verdict, which would
        # silently become DENY at the first call site that used it.
        missing = []
        for name in dir(caps):
            if name.startswith("_") or name in {
                "ALLOW", "CONFIRM", "CONFIRM_IF_IRREVERSIBLE", "DENY",
            }:
                continue
            value = getattr(caps, name)
            if isinstance(value, str) and name.isupper() and value.islower():
                if value not in caps.POLICY:
                    missing.append(name)
        self.assertEqual(missing, [], f"capabilities with no policy row: {missing}")

    def test_the_categories_the_brief_named_all_exist(self):
        required = [
            caps.READ_ONLY, caps.FILE_READ, caps.FILE_WRITE, caps.FILE_DELETE,
            caps.FILE_MOVE, caps.COMMAND_EXEC, caps.CODE_EXEC,
            caps.PACKAGE_INSTALL, caps.SOFTWARE_INSTALL,
            caps.BROWSER_NAVIGATE, caps.BROWSER_SUBMIT, caps.MESSAGE_SEND,
            caps.SYSTEM_SETTINGS, caps.SYSTEM_POWER,
        ]
        for capability in required:
            with self.subTest(capability=capability):
                self.assertTrue(caps.is_known(capability))


class TestVerdicts(unittest.TestCase):

    def test_read_only_work_never_asks(self):
        for capability in (caps.READ_ONLY, caps.FILE_READ,
                           caps.BROWSER_NAVIGATE, caps.SCREEN_CAPTURE):
            with self.subTest(capability=capability):
                self.assertEqual(caps.decision_for(capability), caps.ALLOW)
                self.assertFalse(caps.requires_confirmation(capability))

    def test_high_impact_work_always_asks(self):
        for capability in (caps.FILE_DELETE, caps.COMMAND_EXEC, caps.CODE_EXEC,
                           caps.PACKAGE_INSTALL, caps.SOFTWARE_INSTALL,
                           caps.MESSAGE_SEND, caps.BROWSER_SUBMIT,
                           caps.BROWSER_ACCOUNT, caps.SYSTEM_POWER,
                           caps.APP_LAUNCH_SHELL, caps.FILE_ARCHIVE_EXTRACT):
            with self.subTest(capability=capability):
                self.assertTrue(caps.requires_confirmation(capability))

    def test_in_process_code_execution_is_denied_outright(self):
        # Not CONFIRM. There is no approval that makes exec()-ing model output
        # inside this interpreter a sound idea.
        self.assertEqual(caps.decision_for(caps.CODE_EXEC_IN_PROCESS), caps.DENY)

    def test_confirmation_still_required_for_high_impact_when_reversible(self):
        # Claiming reversibility must not open a flat CONFIRM. Deleting a file
        # "reversibly" is still deleting a file.
        for capability in (caps.FILE_DELETE, caps.MESSAGE_SEND,
                           caps.SYSTEM_POWER, caps.COMMAND_EXEC):
            with self.subTest(capability=capability):
                self.assertTrue(
                    caps.requires_confirmation(capability, reversible=True)
                )

    def test_reversible_relaxes_only_the_conditional_verdict(self):
        for capability in (caps.FILE_WRITE, caps.FILE_MOVE, caps.SYSTEM_SETTINGS):
            with self.subTest(capability=capability):
                self.assertEqual(
                    caps.decision_for(capability), caps.CONFIRM_IF_IRREVERSIBLE
                )
                self.assertTrue(caps.requires_confirmation(capability))
                self.assertFalse(
                    caps.requires_confirmation(capability, reversible=True)
                )

    def test_deny_is_never_a_confirmation_question(self):
        self.assertFalse(caps.requires_confirmation(caps.CODE_EXEC_IN_PROCESS))
        self.assertFalse(caps.requires_confirmation("nonsense"))


class TestNamespaces(unittest.TestCase):
    """Capability names are namespaced so a whole subsystem can be reasoned
    about as a group — and so a planned one (minecraft.*) can be added without
    inventing a second security framework for it."""

    def test_every_capability_is_namespaced(self):
        unnamespaced = [c for c in caps.ALL_CAPABILITIES if "." not in c]
        self.assertEqual(unnamespaced, [],
                         f"capabilities with no namespace: {unnamespaced}")

    def test_the_expected_namespaces_exist(self):
        for prefix in ("file", "code", "browser", "computer", "messaging",
                       "app", "info", "net"):
            with self.subTest(prefix=prefix):
                self.assertTrue(caps.in_namespace(prefix),
                                f"no capabilities under {prefix}.*")

    def test_namespace_extraction(self):
        self.assertEqual(caps.namespace("file.delete"), "file")
        self.assertEqual(caps.namespace("minecraft.observe"), "minecraft")
        self.assertEqual(caps.namespace("nodots"), "")
        self.assertEqual(caps.namespace(None), "")

    def test_in_namespace_returns_only_that_namespace(self):
        files = caps.in_namespace("file")
        self.assertIn(caps.FILE_DELETE, files)
        self.assertNotIn(caps.BROWSER_SUBMIT, files)
        for capability in files:
            self.assertTrue(capability.startswith("file."))

    def test_an_unused_namespace_is_empty_rather_than_an_error(self):
        # Asking about a namespace nothing uses must return nothing, not raise.
        self.assertEqual(caps.in_namespace("nothing_uses_this"), ())
        self.assertEqual(caps.in_namespace(""), ())

    def test_the_minecraft_namespace_is_populated_and_still_governed(self):
        # This assertion used to be `== ()`, which was right when the namespace
        # was designed and unbuilt. The subsystem exists now, so the useful
        # property is not that it is empty but that every capability in it went
        # through the same table as everything else.
        minecraft = caps.in_namespace("minecraft")
        self.assertTrue(minecraft, "the minecraft namespace is empty")
        for capability in minecraft:
            with self.subTest(capability=capability):
                self.assertIn(caps.decision_for(capability), caps.VERDICTS)
                self.assertTrue(caps.is_known(capability))
        # And the one that must never be reachable, is not.
        self.assertEqual(caps.decision_for("minecraft.command"), caps.DENY)

    def test_a_future_namespace_cannot_be_registered_at_runtime(self):
        # A subsystem added later declares its capabilities in the table, as a
        # source change. There is no registration API, and adding one would
        # reopen exactly the hole this layer closed.
        self.assertFalse(hasattr(caps, "register"))
        self.assertFalse(hasattr(caps, "add_namespace"))
        with self.assertRaises(TypeError):
            caps.POLICY["minecraft.move"] = caps.ALLOW


class TestPluginDefault(unittest.TestCase):

    def test_an_undeclared_plugin_is_not_assumed_harmless(self):
        self.assertEqual(caps.decision_for(caps.PLUGIN_UNCLASSIFIED), caps.CONFIRM)
        self.assertTrue(caps.requires_confirmation(caps.PLUGIN_UNCLASSIFIED))


if __name__ == "__main__":
    unittest.main()
