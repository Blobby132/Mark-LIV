"""
A8: the dashboard's message layer is authenticated, and keyed properly.

It was AES-256-CBC with no MAC -- a flipped bit decrypted to different text
or a padding error, never a refusal -- under a key that was one SHA-256 of
the 8-character pairing key and a fixed salt, so a captured message could be
brute-forced offline at hash speed. CryptoJS came from a CDN, downloaded at
import time, with a redirect to the CDN when the local copy was missing.

Now:
  - AES-256-GCM, a fresh 12-byte nonce per message, the login token bound
    in as associated data, and a nonce never accepted twice;
  - the key from PBKDF2-HMAC-SHA256 with a random salt per login token and
    KDF_ITERATIONS rounds -- sent to the page with the token, so the page
    derives the same key with WebCrypto;
  - WebCrypto only exists in a secure context, so HTTPS is the default and
    the plain-HTTP fallback says plainly that nothing is encrypted rather
    than offering weaker crypto. Under HTTPS a plaintext command is refused:
    a leaked token alone cannot send one;
  - no CryptoJS, no CDN.
"""

from __future__ import annotations

import base64
import os
import unittest
import unittest.mock

from tests.support.paths import REPO_ROOT as ROOT

from dashboard import server                                        # noqa: E402

SOURCE = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
APP = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "dashboard" / "static" / "login.html").read_text(
    encoding="utf-8")


def _deps():
    try:
        import fastapi.testclient                                    # noqa: F401
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa
        return True
    except Exception:
        return False


def derive(pairing_key, salt_b64, iterations):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                      salt=base64.b64decode(salt_b64),
                      iterations=iterations).derive(pairing_key.encode())


def seal(key, token, text, nonce=None):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = nonce or os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, text.encode(),
                             f"{server.AAD_PREFIX}{token}".encode())
    return base64.b64encode(nonce + ct).decode()


@unittest.skipUnless(_deps(), "needs fastapi and cryptography")
class MessageLayerTests(unittest.TestCase):

    def setUp(self):
        from fastapi.testclient import TestClient
        patcher = unittest.mock.patch.object(server, "KDF_ITERATIONS", 1000)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tls(True)
        self.srv = server.DashboardServer()
        self.client = TestClient(self.srv.app)

    def tls(self, on):
        patcher = unittest.mock.patch.object(
            server.DashboardServer, "_ssl_enabled", staticmethod(lambda: on))
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self):
        pairing = self.srv.new_key()
        body = self.client.post("/login", json={"pin": pairing}).json()
        key = derive(pairing, body["salt"], body["iterations"])
        return body["token"], key

    def send(self, token, payload):
        return self.client.post("/api/command", json=payload,
                                headers={"Authorization": f"Bearer {token}"})

    def queued(self):
        out = []
        while not self.srv._command_queue.empty():
            out.append(self.srv._command_queue.get_nowait())
        return out

    def test_a_sealed_command_arrives(self):
        token, key = self.login()
        r = self.send(token, {"enc": seal(key, token, "open the door")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.queued(), ["open the door"])

    def test_a_flipped_bit_is_refused(self):
        token, key = self.login()
        raw = bytearray(base64.b64decode(seal(key, token, "say hello")))
        raw[-1] ^= 1
        r = self.send(token, {"enc": base64.b64encode(raw).decode()})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.queued(), [])

    def test_a_replayed_message_is_refused(self):
        token, key = self.login()
        sealed = seal(key, token, "say hello")
        self.assertEqual(self.send(token, {"enc": sealed}).status_code, 200)
        self.assertEqual(self.send(token, {"enc": sealed}).status_code, 400)
        self.assertEqual(self.queued(), ["say hello"])

    def test_a_message_sealed_for_another_login_is_refused(self):
        token, key = self.login()
        other, _ = self.login()
        r = self.send(other, {"enc": seal(key, token, "say hello")})
        self.assertEqual(r.status_code, 400)

    def test_every_login_has_its_own_salt(self):
        pairing = self.srv.new_key()
        a = self.client.post("/login", json={"pin": pairing}).json()
        b = self.client.post("/login",
                             json={"pin": self.srv.new_key()}).json()
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertEqual(len(base64.b64decode(a["salt"])), 16)

    def test_plaintext_is_refused_under_https(self):
        token, _ = self.login()
        r = self.send(token, {"text": "open the door"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("encrypted", r.json()["error"])
        self.assertEqual(self.queued(), [])

    def test_a_paired_device_gets_a_fresh_salt(self):
        pairing = self.srv.new_key()
        page = self.client.get(f"/auto-login?key={pairing}").text
        self.assertIn("jarvis_salt", page)
        device = page.split("jarvis_device_token','")[1].split("'")[0]
        body = self.client.post("/api/device-login",
                                json={"device_token": device}).json()
        key = derive(body["key"], body["salt"], body["iterations"])
        r = self.send(body["token"],
                      {"enc": seal(key, body["token"], "hello again")})
        self.assertEqual(r.status_code, 200, r.text)


@unittest.skipUnless(_deps(), "needs fastapi and cryptography")
class PlainHttpTests(unittest.TestCase):

    def test_plain_http_accepts_plaintext_and_says_so(self):
        from fastapi.testclient import TestClient
        with unittest.mock.patch.object(server.DashboardServer,
                                        "_ssl_enabled",
                                        staticmethod(lambda: False)), \
                unittest.mock.patch.object(server, "KDF_ITERATIONS", 1000):
            srv = server.DashboardServer()
            client = TestClient(srv.app)
            token = client.post("/login",
                                json={"pin": srv.new_key()}).json()["token"]
            r = client.post("/api/command", json={"text": "hello"},
                            headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(srv._command_queue.get_nowait(), "hello")


class KeyDerivationTests(unittest.TestCase):

    def test_it_is_a_real_kdf(self):
        self.assertGreaterEqual(server.KDF_ITERATIONS, 600_000)
        self.assertIn("PBKDF2HMAC", SOURCE)
        self.assertNotIn("_derive_key", SOURCE)
        self.assertNotIn("CBC", SOURCE)


class PageTests(unittest.TestCase):

    def test_the_page_uses_webcrypto_gcm(self):
        for needle in ("crypto.subtle", "AES-GCM", "PBKDF2", "isSecureContext",
                       "additionalData"):
            self.assertIn(needle, APP)
        self.assertNotIn("CryptoJS", APP)
        self.assertNotIn("crypto.js", APP)

    def test_plain_http_says_so_plainly(self):
        self.assertIn("NOT ENCRYPTED", APP)

    def test_the_login_pages_keep_the_salt(self):
        self.assertIn("jarvis_salt", LOGIN)
        self.assertIn("jarvis_iters", LOGIN)


class NoCdnTests(unittest.TestCase):

    def test_no_cdn_and_no_download(self):
        for needle in ("cdnjs", "RedirectResponse", "urlretrieve",
                       "crypto-js", "_ensure_crypto_js"):
            self.assertNotIn(needle, SOURCE)
        self.assertFalse((ROOT / "dashboard" / "static"
                          / "crypto-js.min.js").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
