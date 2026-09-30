"""
Dashboard hardening (dashboard/server.py).

The dashboard lets a phone on the same Wi-Fi talk to JARVIS. Each test here
is a finding from a review of how it opens the machine to that phone, and
what stops anything else getting in the same way.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dashboard import server                                        # noqa: E402

SOURCE = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")


class FirewallTests(unittest.TestCase):
    """On Windows the dashboard used to add, elevated: a rule allowing ALL
    inbound traffic to python.exe on every profile -- every Python program on
    the machine, not just this one -- an unscoped port rule, and a command
    flipping every Public network profile to Private."""

    def test_a_fresh_machine_gets_one_scoped_port_rule(self):
        plan = server._windows_firewall_plan(8000, existing=set())
        self.assertEqual(len(plan), 1)
        rule = plan[0]
        self.assertIn("localport=8000", rule)
        self.assertIn("profile=private", rule)
        self.assertIn("remoteip=localsubnet", rule)
        self.assertIn("protocol=TCP", rule)

    def test_no_rule_opens_python_itself(self):
        for existing in (set(), set(server._legacy_rule_names(8000))):
            for line in server._windows_firewall_plan(8000, existing):
                self.assertNotIn("program=", line)
        self.assertNotIn("program=", SOURCE)

    def test_network_profiles_are_never_changed(self):
        self.assertNotIn("Set-NetConnectionProfile", SOURCE)

    def test_the_old_broad_rules_are_removed_where_they_exist(self):
        legacy = set(server._legacy_rule_names(8000))
        plan = server._windows_firewall_plan(8000, existing=legacy)
        deletes = [line for line in plan if " delete rule " in line]
        self.assertEqual(len(deletes), 2)
        self.assertTrue(any("JARVIS Dashboard Python" in d for d in deletes))

    def test_nothing_to_do_once_configured(self):
        done = {server._port_rule_name(8000)}
        self.assertEqual(server._windows_firewall_plan(8000, done), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
