"""
Tests for core/permissions.py — the broker.

The "work" every test hands to `guard()` is a local callable that appends to a
list. Nothing here deletes a file, starts a process, sends a message or changes
a setting: the broker's job is deciding, and that is what is under test.

A fake HUD stands in for the interface. It records the banner it was shown and
lets a test press CONFIRM or CANCEL, which is the only route by which an
approval can exist.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audit, capabilities as caps, confirm, permissions  # noqa: E402
from core import undo as undo_stack                                 # noqa: E402


class FakeHud:
    """Stands in for the Qt confirmation banner.

    `press_confirm()` is the only way anything gated runs — exactly as in the
    real app, where the call comes from a button handler."""

    def __init__(self):
        self.shown: list[tuple[str, str]] = []
        self.hidden = 0
        self.log_lines: list[str] = []

    def show(self, title, detail):
        self.shown.append((title, detail))

    def hide(self):
        self.hidden += 1

    def log(self, line):
        self.log_lines.append(line)

    # -- what a human's finger does --
    def press_confirm(self):
        confirm.resolve(True)
        _settle()

    def press_cancel(self):
        confirm.resolve(False)
        _settle()

    @property
    def last_title(self) -> str:
        return self.shown[-1][0] if self.shown else ""

    @property
    def last_detail(self) -> str:
        return self.shown[-1][1] if self.shown else ""


def _settle(timeout: float = 5.0) -> None:
    """Wait for confirm.py's worker thread to finish the approved work.

    `confirm.resolve()` runs the callable on a daemon thread named
    'confirm-<key>' so the Qt thread is never blocked by a shutdown. Tests have
    to wait for it or they race the assertion."""
    for thread in list(threading.enumerate()):
        if thread.name.startswith("confirm-") and thread.is_alive():
            thread.join(timeout=timeout)


class _BrokerCase(unittest.TestCase):

    def setUp(self):
        self.calls: list[str] = []
        self._tmp = tempfile.TemporaryDirectory()
        audit.configure(path=Path(self._tmp.name) / "audit.jsonl", enabled=True)
        permissions.install()
        # Headless by default: every test that wants a HUD says so.
        confirm.bind(show=None, hide=None, log=None)
        confirm._pending = None
        undo_stack.clear()

    def tearDown(self):
        confirm.bind(show=None, hide=None, log=None)
        confirm._pending = None
        confirm.set_observer(None)
        undo_stack.clear()
        audit.configure(path=audit.DEFAULT_DIR / audit.DEFAULT_FILENAME,
                        enabled=True)
        self._tmp.cleanup()

    def attach_hud(self) -> FakeHud:
        hud = FakeHud()
        confirm.bind(show=hud.show, hide=hud.hide, log=hud.log)
        permissions.install()
        return hud

    def work(self, label="done"):
        def _run():
            self.calls.append(label)
            return label
        return _run

    def audit_rows(self):
        return audit.recent(200)


# ── Low-risk work is not interrupted ─────────────────────────────────────────

class TestAllowedWork(_BrokerCase):

    def test_read_only_runs_without_a_hud_at_all(self):
        result = permissions.guard(
            "file_controller.list", caps.READ_ONLY,
            summary="List the desktop", run=self.work("listed"),
        )
        self.assertTrue(result.succeeded)
        self.assertTrue(result.ok)
        self.assertFalse(result.confirmation_required)
        self.assertEqual(self.calls, ["listed"])

    def test_no_banner_is_shown_for_read_only_work(self):
        hud = self.attach_hud()
        for capability in (caps.READ_ONLY, caps.FILE_READ,
                           caps.BROWSER_NAVIGATE, caps.APP_LAUNCH,
                           caps.SCREEN_CAPTURE, caps.INPUT_SYNTHETIC):
            with self.subTest(capability=capability):
                permissions.guard("x", capability, summary="s",
                                  run=self.work(capability))
        self.assertEqual(hud.shown, [], "a low-risk action asked for confirmation")
        self.assertEqual(len(self.calls), 6)

    def test_permission_check_without_work_reports_allowed(self):
        result = permissions.guard("x", caps.READ_ONLY, summary="s")
        self.assertTrue(result.allowed)
        self.assertEqual(result.outcome, permissions.ALLOWED)
        self.assertEqual(self.calls, [])


# ── Reversibility, demonstrated rather than claimed ──────────────────────────

class TestReversibility(_BrokerCase):

    def test_reversible_write_runs_immediately_and_registers_the_undo(self):
        hud = self.attach_hud()
        reversed_it: list[str] = []

        result = permissions.guard(
            "file_controller.write", caps.FILE_WRITE,
            summary="Write notes.txt", run=self.work("wrote"),
            undo=lambda: reversed_it.append("undone") or "put back",
            undo_label="wrote notes.txt",
        )

        self.assertTrue(result.succeeded)
        self.assertEqual(hud.shown, [], "a reversible write asked for confirmation")
        self.assertEqual(undo_stack.peek(), "wrote notes.txt")

        undo_stack.undo_last()
        self.assertEqual(reversed_it, ["undone"])

    def test_the_same_write_asks_when_no_undo_is_offered(self):
        hud = self.attach_hud()
        result = permissions.guard(
            "file_controller.write", caps.FILE_WRITE,
            summary="Overwrite notes.txt", run=self.work("wrote"),
        )
        self.assertTrue(result.pending)
        self.assertEqual(len(hud.shown), 1)
        self.assertEqual(self.calls, [], "work ran before anyone confirmed")

    def test_reversibility_cannot_be_claimed_with_a_non_callable(self):
        hud = self.attach_hud()
        for fake_undo in (True, "yes", 1, {"undo": True}):
            with self.subTest(fake_undo=fake_undo):
                confirm._pending = None
                hud.shown.clear()
                result = permissions.guard(
                    "file_controller.move", caps.FILE_MOVE,
                    summary="Move a file", run=self.work("moved"),
                    undo=fake_undo,
                )
                self.assertTrue(result.pending, "a fake undo bought a free pass")
                self.assertEqual(self.calls, [])

    def test_an_undo_is_not_registered_when_the_work_fails(self):
        def _boom():
            raise RuntimeError("disk full")
        result = permissions.guard(
            "file_controller.write", caps.FILE_WRITE,
            summary="Write notes.txt", run=_boom,
            undo=lambda: "put back", undo_label="wrote notes.txt",
        )
        self.assertTrue(result.failed)
        self.assertFalse(undo_stack.can_undo(),
                         "registered an undo for work that never happened")

    def test_reversibility_does_not_relax_a_flat_confirm(self):
        hud = self.attach_hud()
        result = permissions.guard(
            "file_controller.delete", caps.FILE_DELETE,
            summary="Delete notes.txt", run=self.work("deleted"),
            undo=lambda: "restored",
        )
        self.assertTrue(result.pending)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(hud.shown), 1)


# ── The gate ─────────────────────────────────────────────────────────────────

class TestConfirmation(_BrokerCase):

    def test_high_impact_work_waits_for_a_human(self):
        hud = self.attach_hud()
        result = permissions.guard(
            "send_message.send", caps.MESSAGE_SEND,
            summary="Send a WhatsApp message to Mum",
            detail="It cannot be unsent.",
            run=self.work("sent"),
        )

        self.assertTrue(result.pending)
        self.assertFalse(result.ok, "pending must never read as success")
        self.assertEqual(self.calls, [], "the message was sent before approval")
        self.assertEqual(hud.last_title, "Send a WhatsApp message to Mum")
        self.assertIn("CONFIRMATION_PENDING", result.message)

    def test_pressing_confirm_runs_the_work(self):
        hud = self.attach_hud()
        permissions.guard("send_message.send", caps.MESSAGE_SEND,
                          summary="Send a message", run=self.work("sent"))
        self.assertEqual(self.calls, [])
        hud.press_confirm()
        self.assertEqual(self.calls, ["sent"])

    def test_pressing_cancel_runs_nothing(self):
        hud = self.attach_hud()
        permissions.guard("send_message.send", caps.MESSAGE_SEND,
                          summary="Send a message", run=self.work("sent"))
        hud.press_cancel()
        self.assertEqual(self.calls, [])

    def test_a_second_request_is_refused_while_one_is_on_screen(self):
        hud = self.attach_hud()
        permissions.guard("a", caps.MESSAGE_SEND, summary="First",
                          run=self.work("first"))
        second = permissions.guard("b", caps.SYSTEM_POWER, summary="Second",
                                   run=self.work("second"))
        self.assertTrue(second.denied)
        self.assertIn("First", second.message)
        self.assertEqual(len(hud.shown), 1)

    def test_the_detail_reaches_the_banner(self):
        hud = self.attach_hud()
        permissions.guard("x", caps.SYSTEM_POWER, summary="Shut down",
                          detail="Unsaved work will be lost.",
                          run=self.work("off"))
        self.assertEqual(hud.last_detail, "Unsaved work will be lost.")


# ── Fail closed ──────────────────────────────────────────────────────────────

class TestHeadlessRefusal(_BrokerCase):

    def test_confirmation_without_an_interface_is_refused(self):
        # Nothing is bound in setUp, so there is no way to ask a human.
        result = permissions.guard(
            "file_controller.delete", caps.FILE_DELETE,
            summary="Delete notes.txt", run=self.work("deleted"),
        )
        self.assertTrue(result.denied)
        self.assertFalse(result.ok)
        self.assertEqual(self.calls, [], "ran without any human being asked")
        self.assertIn("no way for me to ask", result.message)

    def test_headless_refusal_applies_to_every_gated_capability(self):
        for capability in (caps.FILE_DELETE, caps.COMMAND_EXEC, caps.CODE_EXEC,
                           caps.PACKAGE_INSTALL, caps.SOFTWARE_INSTALL,
                           caps.MESSAGE_SEND, caps.BROWSER_SUBMIT,
                           caps.BROWSER_ACCOUNT, caps.SYSTEM_POWER,
                           caps.APP_LAUNCH_SHELL, caps.FILE_ARCHIVE_EXTRACT):
            with self.subTest(capability=capability):
                result = permissions.guard("x", capability, summary="s",
                                           run=self.work(capability))
                self.assertTrue(result.denied)
        self.assertEqual(self.calls, [])

    def test_low_risk_work_still_runs_headless(self):
        # Fail-closed must not mean fail-useless: an assistant with no HUD can
        # still answer questions.
        result = permissions.guard("x", caps.READ_ONLY, summary="s",
                                   run=self.work("read"))
        self.assertTrue(result.succeeded)

    def test_a_gated_capability_cannot_be_checked_into_permission(self):
        # No `run` means nothing to approve, so this must not come back allowed.
        result = permissions.guard("x", caps.FILE_DELETE, summary="s")
        self.assertTrue(result.denied)


class TestDenial(_BrokerCase):

    def test_unknown_capability_is_denied_even_with_a_hud(self):
        hud = self.attach_hud()
        result = permissions.guard("x", "not_a_real_capability",
                                   summary="Do something", run=self.work("ran"))
        self.assertTrue(result.denied)
        self.assertEqual(self.calls, [])
        self.assertEqual(hud.shown, [], "an unknown capability got a banner")

    def test_denied_capability_cannot_be_approved(self):
        hud = self.attach_hud()
        result = permissions.guard(
            "desktop.task", caps.CODE_EXEC_IN_PROCESS,
            summary="Run generated Python in this process", run=self.work("ran"),
        )
        self.assertTrue(result.denied)
        self.assertEqual(hud.shown, [], "a DENY capability was offered to a human")
        # And there is nothing pending that a stray resolve() could trigger.
        confirm.resolve(True)
        _settle()
        self.assertEqual(self.calls, [])


# ── Forged approval ──────────────────────────────────────────────────────────

class TestForgedApproval(_BrokerCase):

    def test_approval_shaped_parameters_are_stripped(self):
        params = {
            "action": "delete", "name": "notes.txt",
            "confirmed": "yes", "approved": True, "force": 1,
            "skip_confirmation": "true", "user_confirmed": "yes",
            "Override": True, "skip-confirm": "yes",
        }
        cleaned = permissions.strip_forged_approval(params)
        self.assertEqual(set(cleaned), {"action", "name"})
        self.assertEqual(cleaned["name"], "notes.txt")

    def test_legitimate_parameters_survive(self):
        params = {"action": "type_text", "press_enter": "true",
                  "value": "hello", "allow_list": ["a"]}
        self.assertEqual(permissions.strip_forged_approval(params), params)

    def test_forged_keys_are_reportable(self):
        self.assertEqual(
            permissions.had_forged_approval({"a": 1, "confirmed": "yes",
                                             "FORCE": True}),
            ["FORCE", "confirmed"],
        )
        self.assertEqual(permissions.had_forged_approval({"a": 1}), [])

    def test_guard_ignores_parameters_entirely(self):
        # The structural guarantee: guard() has no parameter through which a
        # model-written dict could influence the verdict.
        hud = self.attach_hud()
        import inspect
        signature = inspect.signature(permissions.guard)
        for forged in permissions.FORGED_APPROVAL_KEYS:
            self.assertNotIn(forged, signature.parameters)

        result = permissions.guard("x", caps.FILE_DELETE, summary="Delete",
                                   run=self.work("deleted"))
        self.assertTrue(result.pending)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(hud.shown), 1)

    def test_non_dict_parameters_are_handled(self):
        self.assertEqual(permissions.strip_forged_approval(None), {})
        self.assertEqual(permissions.strip_forged_approval("confirmed=yes"), {})
        self.assertEqual(permissions.had_forged_approval(None), [])


# ── Honest results ───────────────────────────────────────────────────────────

class TestResultHonesty(_BrokerCase):

    def test_a_raising_callable_is_a_failure(self):
        def _boom():
            raise OSError("permission denied")
        result = permissions.guard("x", caps.READ_ONLY, summary="Read a file",
                                   run=_boom)
        self.assertTrue(result.failed)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_class, "OSError")
        self.assertIn("permission denied", result.message)

    def test_verify_turns_a_quiet_non_event_into_a_failure(self):
        # The pattern this exists for: a function that returns normally without
        # having done anything.
        result = permissions.guard(
            "x", caps.READ_ONLY, summary="Create the file",
            run=lambda: "Created.", verify=lambda _value: False,
        )
        self.assertTrue(result.failed)
        self.assertIn("did not take effect", result.message)

    def test_verify_passing_leaves_it_a_success(self):
        result = permissions.guard(
            "x", caps.READ_ONLY, summary="Create the file",
            run=lambda: "Created.", verify=lambda value: value == "Created.",
        )
        self.assertTrue(result.succeeded)
        self.assertEqual(result.value, "Created.")

    def test_a_raising_verifier_is_a_failure_not_a_crash(self):
        def _bad_check(_value):
            raise RuntimeError("cannot stat")
        result = permissions.guard("x", caps.READ_ONLY, summary="Check",
                                   run=lambda: "ok", verify=_bad_check)
        self.assertTrue(result.failed)
        self.assertIn("could not be verified", result.message)

    def test_pending_is_not_ok_and_not_allowed(self):
        self.attach_hud()
        result = permissions.guard("x", caps.MESSAGE_SEND, summary="Send",
                                   run=self.work("sent"))
        self.assertFalse(result.ok)
        self.assertFalse(result.allowed)
        self.assertFalse(result.succeeded)
        self.assertTrue(result.pending)

    def test_failure_after_approval_is_reported_as_failure(self):
        hud = self.attach_hud()
        def _boom():
            raise RuntimeError("the app was not open")
        permissions.guard("send_message.send", caps.MESSAGE_SEND,
                          summary="Send a message", run=_boom)
        hud.press_confirm()
        # confirm.py logs whatever the runner returned; it must not say "Done."
        joined = " ".join(hud.log_lines)
        self.assertIn("the app was not open", joined)


# ── The audit trail ──────────────────────────────────────────────────────────

class TestAuditIntegration(_BrokerCase):

    def test_an_allowed_action_is_recorded(self):
        permissions.guard("file_controller.list", caps.READ_ONLY,
                          summary="List", target=str(Path.home() / "Desktop"),
                          run=self.work("listed"))
        rows = self.audit_rows()
        self.assertEqual([r["event"] for r in rows], ["permission", "result"])
        self.assertEqual(rows[0]["decision"], caps.ALLOW)
        self.assertFalse(rows[0]["confirmation_required"])
        self.assertEqual(rows[1]["outcome"], audit.SUCCEEDED)

    def test_a_denial_is_recorded(self):
        permissions.guard("x", "not_a_capability", summary="s",
                          run=self.work("ran"))
        row = self.audit_rows()[0]
        self.assertEqual(row["decision"], caps.DENY)
        self.assertEqual(row["note"], "unknown capability")

    def test_a_headless_refusal_is_recorded(self):
        permissions.guard("x", caps.FILE_DELETE, summary="Delete",
                          run=self.work("deleted"))
        row = self.audit_rows()[0]
        self.assertTrue(row["confirmation_required"])
        self.assertEqual(row["note"], "no confirmation surface")

    def test_approval_and_result_are_both_recorded(self):
        hud = self.attach_hud()
        permissions.guard("send_message.send", caps.MESSAGE_SEND,
                          summary="Send a message", run=self.work("sent"))
        hud.press_confirm()

        rows = self.audit_rows()
        events = [r["event"] for r in rows]
        self.assertIn("permission", events)
        self.assertIn("confirmation", events)
        self.assertIn("result", events)

        approval = next(r for r in rows if r["event"] == "confirmation")
        self.assertTrue(approval["approved"])
        self.assertEqual(approval["outcome"], audit.APPROVED)

        outcome = next(r for r in rows if r["event"] == "result")
        self.assertEqual(outcome["outcome"], audit.SUCCEEDED)
        self.assertTrue(outcome["approved"])

    def test_a_cancellation_is_recorded(self):
        hud = self.attach_hud()
        permissions.guard("x", caps.SYSTEM_POWER, summary="Shut down",
                          run=self.work("off"))
        hud.press_cancel()
        row = next(r for r in self.audit_rows() if r["event"] == "confirmation")
        self.assertEqual(row["outcome"], audit.CANCELLED)
        self.assertFalse(row["approved"])
        # And no result row, because nothing ran.
        self.assertNotIn("result", [r["event"] for r in self.audit_rows()])

    def test_the_audit_never_sees_the_content_of_an_action(self):
        hud = self.attach_hud()
        secret = "meet me at nine, the password is swordfish"
        permissions.guard(
            "send_message.send", caps.MESSAGE_SEND,
            summary="Send a message to Mum",       # no body, by construction
            run=lambda: f"sent: {secret}",         # even if the work returns it
        )
        hud.press_confirm()
        blob = audit.log_path().read_text(encoding="utf-8")
        self.assertNotIn("swordfish", blob)
        self.assertNotIn("meet me at nine", blob)

    def test_a_broken_audit_log_does_not_stop_the_work(self):
        audit.configure(path=Path("/proc/nonexistent/nope/audit.jsonl"))
        result = permissions.guard("x", caps.READ_ONLY, summary="s",
                                   run=self.work("ran"))
        self.assertTrue(result.succeeded)
        self.assertEqual(self.calls, ["ran"])


class TestPureQueries(_BrokerCase):

    def test_check_reports_the_effective_verdict(self):
        self.assertEqual(permissions.check(caps.READ_ONLY), caps.ALLOW)
        self.assertEqual(permissions.check(caps.FILE_DELETE), caps.CONFIRM)
        self.assertEqual(permissions.check("nope"), caps.DENY)
        self.assertEqual(
            permissions.check(caps.FILE_WRITE, reversible=True), caps.ALLOW
        )
        self.assertEqual(
            permissions.check(caps.FILE_WRITE), caps.CONFIRM_IF_IRREVERSIBLE
        )

    def test_check_has_no_side_effects(self):
        before = len(self.audit_rows())
        permissions.check(caps.FILE_DELETE)
        permissions.requires_confirmation(caps.MESSAGE_SEND)
        self.assertEqual(len(self.audit_rows()), before)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
