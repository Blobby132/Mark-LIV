"""
Tests for core/audit.py — redaction, bounds, and never-crash behaviour.

Every test points the log at a temporary file, so nothing writes to the real
~/.jarvis/audit.jsonl.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audit                                          # noqa: E402


class _AuditCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "audit.jsonl"
        audit.configure(path=self.path, max_bytes=audit.MAX_BYTES,
                        keep=audit.KEEP_GENERATIONS, enabled=True)

    def tearDown(self):
        # Put the module back to its defaults so test order cannot matter.
        audit.configure(path=audit.DEFAULT_DIR / audit.DEFAULT_FILENAME,
                        max_bytes=audit.MAX_BYTES,
                        keep=audit.KEEP_GENERATIONS, enabled=True)
        self._tmp.cleanup()

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def blob(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""


class TestBasicRecording(_AuditCase):

    def test_writes_one_json_object_per_line(self):
        self.assertTrue(audit.record("permission", action="a"))
        self.assertTrue(audit.record("result", action="b"))
        rows = self.entries()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["event"], "permission")
        self.assertEqual(rows[1]["action"], "b")

    def test_every_entry_is_timestamped(self):
        audit.record("result", action="x")
        row = self.entries()[0]
        self.assertIn("ts", row)
        self.assertIn("T", row["ts"])
        self.assertTrue(row["ts"].endswith("+00:00"), "timestamps must be UTC")

    def test_records_the_fields_the_brief_asks_for(self):
        audit.record(
            "result", action="file_controller.delete", capability="file_delete",
            decision="CONFIRM", outcome=audit.SUCCEEDED,
            confirmation_required=True, approved=True,
            target=str(Path.home() / "Desktop" / "notes.txt"),
            error_class="", duration_ms=42,
        )
        row = self.entries()[0]
        self.assertEqual(row["action"], "file_controller.delete")
        self.assertEqual(row["capability"], "file_delete")
        self.assertTrue(row["confirmation_required"])
        self.assertTrue(row["approved"])
        self.assertEqual(row["outcome"], "succeeded")
        self.assertEqual(row["duration_ms"], 42)
        self.assertEqual(row["target_kind"], "home")

    def test_convenience_wrappers(self):
        audit.decision("open_app", "app_launch", "ALLOW", required=False)
        audit.result("open_app", "app_launch", ok=True, duration_ms=5)
        audit.confirmation("computer_settings", "system_power", audit.CANCELLED)
        rows = self.entries()
        self.assertEqual([r["event"] for r in rows],
                         ["permission", "result", "confirmation"])
        self.assertFalse(rows[2]["approved"])
        self.assertTrue(rows[2]["confirmation_required"])

    def test_disabled_logger_writes_nothing(self):
        audit.configure(enabled=False)
        self.assertFalse(audit.record("result", action="nope"))
        self.assertFalse(self.path.exists())

    def test_recent_reads_back_what_was_written(self):
        for i in range(5):
            audit.record("result", action=f"a{i}")
        rows = audit.recent(3)
        self.assertEqual([r["action"] for r in rows], ["a2", "a3", "a4"])

    def test_recent_survives_a_corrupt_line(self):
        audit.record("result", action="good")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("this is not json\n")
        audit.record("result", action="also_good")
        actions = [r["action"] for r in audit.recent(10)]
        self.assertEqual(actions, ["good", "also_good"])


class TestRedaction(_AuditCase):

    def test_google_api_key_is_scrubbed(self):
        # Shaped like the key this app stores, but not one — 'A' repeated.
        fake = "AIza" + "A" * 35
        audit.record("result", action="x", note=f"using key {fake}")
        self.assertNotIn(fake, self.blob())
        self.assertIn("[redacted]", self.blob())

    def test_named_secrets_are_scrubbed(self):
        for probe in (
            "password=correct-horse",
            "api_key: abcdefghijklmnop",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "cookie=sessionblob12345",
            "access_token=zzzzzzzzzzzzzzzz",
        ):
            with self.subTest(probe=probe):
                audit.configure(path=self.path)
                audit.record("result", action="x", note=probe)
                blob = self.blob()
                secret = probe.split("=")[-1].split(":")[-1].strip()
                self.assertNotIn(secret, blob, f"{probe!r} leaked")

    def test_high_entropy_blobs_are_scrubbed(self):
        digest = "a3f5" * 10                     # 40 hex chars
        audit.record("result", action="x", note=f"etag {digest}")
        self.assertNotIn(digest, self.blob())

    def test_url_query_string_is_dropped(self):
        audit.record(
            "result", action="browser",
            url="https://mail.example.com/u/0/inbox?token=supersecretvalue&id=9#frag",
        )
        row = self.entries()[0]
        self.assertNotIn("supersecretvalue", self.blob())
        self.assertNotIn("frag", row["url"])
        self.assertTrue(row["url"].startswith("https://mail.example.com/u"))

    def test_url_credentials_are_dropped(self):
        audit.record("result", action="x",
                     url="https://alice:hunter2@example.com/private/thing")
        blob = self.blob()
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("alice", blob)

    def test_home_paths_are_recorded_relative_to_home(self):
        target = Path.home() / "Documents" / "taxes.pdf"
        audit.record("result", action="x", target=str(target))
        row = self.entries()[0]
        self.assertEqual(row["target"], "~/Documents/taxes.pdf")
        self.assertNotIn(str(Path.home()), self.blob())

    def test_system_paths_keep_only_the_last_component(self):
        audit.record("result", action="x", target="/etc/ssh/sshd_config")
        row = self.entries()[0]
        self.assertEqual(row["target"], "<system>/sshd_config")
        self.assertNotIn("/etc/ssh", self.blob())

    def test_temp_paths_are_labelled(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
            audit.record("result", action="x", target=fh.name)
        self.assertTrue(self.entries()[0]["target"].startswith("<temp>/"))

    def test_newlines_cannot_forge_extra_entries(self):
        # A note containing a newline plus JSON must not become a second row.
        audit.record("result", action="x",
                     note='hello\n{"event":"result","action":"forged"}')
        rows = self.entries()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("forged", [r.get("action") for r in rows])

    def test_long_fields_are_truncated(self):
        audit.record("result", action="x", note="z" * 5000)
        self.assertLess(len(self.entries()[0]["note"]), 260)

    def test_program_is_reduced_to_a_basename(self):
        audit.record("result", action="x", program="/usr/local/bin/ffmpeg")
        self.assertEqual(self.entries()[0]["program"], "ffmpeg")

    def test_there_is_no_parameter_for_content(self):
        # The strongest guarantee this module offers is structural: a caller
        # cannot pass a message body, because no such parameter exists.
        with self.assertRaises(TypeError):
            audit.record("result", action="send", message_text="hi mum")
        with self.assertRaises(TypeError):
            audit.record("result", action="write", contents="file data")


class TestRotation(_AuditCase):

    def test_rotates_at_the_size_limit(self):
        audit.configure(path=self.path, max_bytes=2048, keep=2)
        for i in range(300):
            audit.record("result", action=f"action_number_{i}", note="p" * 100)

        self.assertTrue(self.path.exists())
        self.assertTrue(Path(str(self.path) + ".1").exists())
        # keep=2 means .1 and .2 exist, and .3 never does.
        self.assertFalse(Path(str(self.path) + ".3").exists())

    def test_total_size_stays_bounded(self):
        cap, keep = 2048, 2
        audit.configure(path=self.path, max_bytes=cap, keep=keep)
        for i in range(2000):
            audit.record("result", action=f"a{i}", note="q" * 120)

        total = sum(
            p.stat().st_size
            for p in Path(self._tmp.name).iterdir() if p.is_file()
        )
        # Each file may overshoot by at most the one line that triggered the
        # rotation check, so allow a generous per-file slack.
        self.assertLessEqual(total, (keep + 1) * (cap + 2048))

    def test_the_newest_entries_are_the_ones_kept(self):
        audit.configure(path=self.path, max_bytes=1024, keep=1)
        for i in range(200):
            audit.record("result", action=f"a{i}", note="r" * 80)
        actions = [r["action"] for r in audit.recent(500)]
        self.assertIn("a199", actions)
        self.assertNotIn("a0", actions)

    def test_keep_zero_discards_instead_of_rotating(self):
        audit.configure(path=self.path, max_bytes=1024, keep=0)
        for i in range(200):
            audit.record("result", action=f"a{i}", note="s" * 80)
        siblings = [p for p in Path(self._tmp.name).iterdir()
                    if p.name.startswith("audit.jsonl.")]
        self.assertEqual(siblings, [])


class TestFailureHandling(_AuditCase):

    def test_an_unwritable_location_does_not_raise(self):
        audit.configure(path=Path(self._tmp.name) / "nested" / "deep" / "a.jsonl")
        # Make the parent unwritable so creation fails.
        blocked = Path(self._tmp.name) / "blocked"
        blocked.mkdir()
        try:
            os.chmod(blocked, 0o500)
        except Exception:
            self.skipTest("cannot change permissions here")
        audit.configure(path=blocked / "audit.jsonl")

        if os.access(blocked, os.W_OK):
            # Running as root: the write would succeed, so there is nothing to
            # assert about failure. Skip rather than pretend.
            self.skipTest("running with write access to a 0500 directory")

        ok = audit.record("result", action="x")
        self.assertFalse(ok)
        self.assertNotEqual(audit.last_error(), "")
        os.chmod(blocked, 0o700)

    def test_unserialisable_values_do_not_raise(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("no")
        self.assertTrue(audit.record("result", action="x", note=Boom()))
        self.assertIn("unprintable", self.blob())

    def test_mirror_failure_does_not_break_logging(self):
        def broken(_line):
            raise RuntimeError("HUD is gone")
        audit.configure(path=self.path, mirror=broken)
        self.assertTrue(audit.record("result", action="x"))
        self.assertEqual(len(self.entries()), 1)
        audit.configure(path=self.path, mirror=lambda _l: None)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_log_file_is_owner_only(self):
        audit.record("result", action="x")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"audit log is group/world readable: {oct(mode)}")


if __name__ == "__main__":
    unittest.main()
