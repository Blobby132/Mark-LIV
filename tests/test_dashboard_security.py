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



class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ThrottleTests(unittest.TestCase):
    """POST /login, GET /auto-login and the device login had no limit on
    how fast a wrong key could be tried, and the key was six characters."""

    def test_five_failures_lock_an_address_out(self):
        clock = Clock()
        throttle = server.LoginThrottle(clock=clock)
        for _ in range(4):
            throttle.failed("10.0.0.9")
            self.assertEqual(throttle.retry_after("10.0.0.9"), 0)
        throttle.failed("10.0.0.9")
        self.assertGreater(throttle.retry_after("10.0.0.9"), 0)
        self.assertEqual(throttle.retry_after("10.0.0.7"), 0,
                         "one address locked out another")
        clock.t += 301
        self.assertEqual(throttle.retry_after("10.0.0.9"), 0)

    def test_guessing_spread_over_many_addresses_is_capped(self):
        throttle = server.LoginThrottle(clock=Clock())
        for i in range(50):
            throttle.failed(f"10.0.1.{i}")
        self.assertGreater(throttle.retry_after("10.0.2.1"), 0)

    def test_a_success_clears_the_count(self):
        throttle = server.LoginThrottle(clock=Clock())
        for _ in range(4):
            throttle.failed("10.0.0.9")
        throttle.succeeded("10.0.0.9")
        throttle.failed("10.0.0.9")
        self.assertEqual(throttle.retry_after("10.0.0.9"), 0)

    def test_old_failures_age_out(self):
        clock = Clock()
        throttle = server.LoginThrottle(clock=clock)
        for _ in range(4):
            throttle.failed("10.0.0.9")
        clock.t += 301
        throttle.failed("10.0.0.9")
        self.assertEqual(throttle.retry_after("10.0.0.9"), 0)

    def test_keys_are_longer(self):
        self.assertGreaterEqual(server.KEY_LENGTH, 8)


def _app_available():
    return getattr(server, "_DEPS_OK", False)


@unittest.skipUnless(_app_available(), "needs fastapi to exercise the routes")
class LoginRouteTests(unittest.TestCase):

    def setUp(self):
        from fastapi.testclient import TestClient
        self.clock = Clock()
        self.srv = server.DashboardServer()
        self.srv._throttle = server.LoginThrottle(clock=self.clock)
        self.client = TestClient(self.srv.app)

    def test_the_real_key_is_refused_while_locked_out(self):
        key = self.srv.new_key()
        self.assertEqual(len(key), server.KEY_LENGTH)
        for _ in range(5):
            r = self.client.post("/login", json={"pin": "WRONGKEY"})
            self.assertEqual(r.status_code, 401)
        r = self.client.post("/login", json={"pin": key})
        self.assertEqual(r.status_code, 429, "guessing was not slowed down")
        self.assertIn("Too many", r.json()["error"])
        self.clock.t += 301
        r = self.client.post("/login", json={"pin": key})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_the_qr_link_is_throttled_too(self):
        for _ in range(5):
            self.client.get("/auto-login", params={"key": "WRONGKEY"})
        r = self.client.get("/auto-login", params={"key": self.srv.new_key()})
        self.assertEqual(r.status_code, 429)

    def test_device_login_is_throttled_too(self):
        for _ in range(5):
            self.client.post("/api/device-login",
                             json={"device_token": "nope"})
        r = self.client.post("/api/device-login",
                             json={"device_token": "nope"})
        self.assertEqual(r.status_code, 429)

    def test_the_login_page_asks_for_the_right_length(self):
        page = self.client.get("/login").text
        self.assertIn(f'maxlength="{server.KEY_LENGTH}"', page)
        self.assertIn(f"const KEY_LENGTH = {server.KEY_LENGTH};", page)
        self.assertNotIn("__KEY_LENGTH__", page)



class TransportTests(unittest.TestCase):
    """The review asked for AES-GCM, or HTTPS by default when certs exist.
    The second is already so -- pinned here so it stays so. serve() makes a
    self-signed pair on first run (_ensure_certs) whenever `cryptography`
    is installed, and serves HTTPS whenever a pair exists; the AES layer
    needs that same package to decrypt at all, so the unauthenticated CBC
    layer only ever runs inside TLS."""

    def test_serve_makes_certs_before_choosing_the_scheme(self):
        body = SOURCE[SOURCE.index("async def serve(self)"):]
        self.assertLess(body.index("_ensure_certs()"),
                        body.index("use_ssl  = self._ssl_enabled()"))

    def test_urls_are_https_whenever_certs_exist(self):
        import unittest.mock
        srv = server.DashboardServer.__new__(server.DashboardServer)
        srv._ip = "192.168.1.5"
        with unittest.mock.patch.object(server.DashboardServer,
                                        "_ssl_enabled",
                                        staticmethod(lambda: True)):
            self.assertTrue(srv.get_url().startswith("https://"))


@unittest.skipUnless(_app_available(), "needs fastapi to exercise the routes")
class TokenLifetimeTests(unittest.TestCase):
    """Login tokens lived until the process did, and a paired phone stayed
    paired for ever unless someone called an endpoint with no button."""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.clock = Clock()
        self.srv = server.DashboardServer()
        self.srv._now = self.clock
        self.client = TestClient(self.srv.app)

    def login(self):
        key = self.srv.new_key()
        r = self.client.post("/login", json={"pin": key})
        return r.json()["token"]

    def wake(self, token):
        return self.client.post("/api/wake",
                                headers={"Authorization": f"Bearer {token}"})

    def test_a_login_token_expires(self):
        token = self.login()
        self.assertEqual(self.wake(token).status_code, 200)
        self.clock.t += server.SESSION_TTL_S + 1
        self.assertEqual(self.wake(token).status_code, 401)
        self.assertNotIn(token, self.srv._tokens, "never pruned")

    def test_expired_tokens_are_pruned_with_their_keys(self):
        self.login()
        self.clock.t += server.SESSION_TTL_S + 1
        self.login()
        self.assertEqual(len(self.srv._tokens), 1)
        self.assertEqual(len(self.srv._aes_cache), 1)

    def test_a_paired_device_expires(self):
        key = self.srv.new_key()
        page = self.client.get("/auto-login", params={"key": key}).text
        import re
        dev = re.search(r"jarvis_device_token','([^']+)'", page).group(1)
        r = self.client.post("/api/device-login", json={"device_token": dev})
        self.assertTrue(r.json()["ok"])
        self.clock.t += server.DEVICE_TTL_S + 1
        r = self.client.post("/api/device-login", json={"device_token": dev})
        self.assertEqual(r.status_code, 401)

    def test_revoking_signs_out_everyone_else(self):
        mine, other = self.login(), self.login()
        r = self.client.post("/api/revoke-devices",
                             headers={"Authorization": f"Bearer {mine}"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.wake(mine).status_code, 200)
        self.assertEqual(self.wake(other).status_code, 401,
                         "another phone kept its login")

    def test_the_page_has_the_control(self):
        page = self.client.get("/").text
        self.assertIn("doSignOutOthers", page)
        self.assertIn("/api/revoke-devices", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
