"""
dashboard/server.py — JARVIS Local HTTP Dashboard

HTTPS on port 8000 by default: a self-signed pair is made on first run
whenever `cryptography` is installed (_ensure_certs). Commands from the page
are also sealed at the application layer: AES-256-GCM, a fresh nonce per
message, under a key from PBKDF2-HMAC-SHA256 over the pairing key with a
random salt per login -- derived on the page with WebCrypto, which only
exists in a secure context. Without `cryptography` there is neither TLS nor
anything to decrypt with, and the page says plainly that nothing is
encrypted rather than offering weaker crypto. Nothing is loaded from a CDN.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import re
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

KEY_LENGTH = 8
"""Characters in a pairing key. Eight from 31 is about 8.5e11 keys; six was
8.9e8, which a script can walk through in the ten minutes a key lives when
nothing limits how fast it guesses."""


class LoginThrottle:
    """Failed pairing attempts, per client address and overall.

    Five failures from one address within five minutes lock that address out
    for five minutes. A ceiling across ALL addresses stops the same guessing
    being spread over many -- on a home network an attacker can take as many
    addresses as they like. A success clears that address's count."""

    def __init__(self, per_address: int = 5, overall: int = 50,
                 window_s: float = 300.0, lockout_s: float = 300.0,
                 clock=None):
        self.per_address, self.overall = per_address, overall
        self.window_s, self.lockout_s = window_s, lockout_s
        self._clock = clock or time.monotonic
        self._failures: dict = {}          # address -> [times]
        self._all: list = []
        self._locked_until: dict = {}      # address (or "*") -> time

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        self._all = [t for t in self._all if t > cutoff]
        for address in list(self._failures):
            kept = [t for t in self._failures[address] if t > cutoff]
            if kept:
                self._failures[address] = kept
            else:
                del self._failures[address]
        self._locked_until = {a: t for a, t in self._locked_until.items()
                              if t > now}

    def retry_after(self, address: str) -> float:
        """Seconds until this address may try again; 0 if it may now."""
        now = self._clock()
        self._prune(now)
        until = max(self._locked_until.get(address, 0.0),
                    self._locked_until.get("*", 0.0))
        return max(0.0, until - now)

    def failed(self, address: str) -> None:
        now = self._clock()
        self._prune(now)
        self._failures.setdefault(address, []).append(now)
        self._all.append(now)
        if len(self._failures[address]) >= self.per_address:
            self._locked_until[address] = now + self.lockout_s
        if len(self._all) >= self.overall:
            self._locked_until["*"] = now + self.lockout_s

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)


SESSION_TTL_S = 12 * 3600
"""How long a login lasts. A phone past it reconnects by itself through its
device token; a token copied off it stops working."""

DEVICE_TTL_S = 30 * 24 * 3600
"""How long a paired phone stays paired without scanning the QR code again."""


WS_AUTH_TIMEOUT_S = 5.0
"""How long a WebSocket may stay open before sending its token. Browsers
cannot set headers on a WebSocket, so the token is the first message rather
than a ?token= in the URL, where it ends up in every access log."""

TICKET_TTL_S = 60.0
"""A download ticket's life. A link the browser follows cannot carry a
header either; a one-time ticket that expires in a minute is harmless in a
log, where the login token was not."""


def _client_address(req) -> str:
    client = getattr(req, "client", None)
    return getattr(client, "host", None) or "unknown"


def _too_many(retry_after: float) -> str:
    minutes = max(1, int(retry_after // 60) + (1 if retry_after % 60 else 0))
    return (f"Too many wrong keys. Try again in {minutes} minute"
            f"{'s' if minutes != 1 else ''}.")

# ── AES-256-GCM, keyed by PBKDF2 ─────────────────────────────────────────────
KDF_ITERATIONS = 600_000
"""PBKDF2-HMAC-SHA256 rounds turning the pairing key into an AES key. The
pairing key is 8 characters; one SHA-256 of it (the old key) let a captured
message be brute-forced offline at hash speed. Sent to the page with the
salt, so the page derives the same key with WebCrypto."""

AAD_PREFIX = "jarvis-command|"
"""Associated data is this plus the login token: a message sealed for one
login is refused under another."""

_NONCE_BYTES = 12
_SEEN_NONCES = 1024
"""Nonces remembered per login, so a captured message cannot be replayed."""


def _derive_aes_key(pairing_key: str, salt: bytes,
                    iterations: int | None = None) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=iterations or KDF_ITERATIONS
                      ).derive(pairing_key.encode("utf-8"))


def _open_gcm(aes_key: bytes, token: str, sealed_b64: str) -> tuple:
    """(nonce, text) from base64(nonce[12] ‖ ciphertext ‖ tag). Raises on
    anything tampered with, sealed under another key, or for another login."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(sealed_b64, validate=True)
    if len(raw) < _NONCE_BYTES + 16:
        raise ValueError("too short")
    nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    text = AESGCM(aes_key).decrypt(nonce, sealed,
                                   f"{AAD_PREFIX}{token}".encode("utf-8"))
    return nonce, text.decode("utf-8")


def _port_rule_name(port: int) -> str:
    return f"JARVIS Dashboard Port {port} (private LAN)"


def _legacy_rule_names(port: int) -> tuple:
    """Rules earlier versions created, which are broader than they should be:
    an unscoped port rule, and a program rule that let ANY inbound traffic
    reach python.exe on every network profile -- every Python program on the
    machine, not just this dashboard."""
    return (f"JARVIS Dashboard Port {port}", "JARVIS Dashboard Python")


def _windows_firewall_plan(port: int, existing: set) -> list:
    """The netsh commands to run, given which rule names already exist.

    One inbound rule: this TCP port, private networks only, from the local
    subnet only -- a phone on the same Wi-Fi, nothing else. The old broad
    rules are removed if present. What this never does any more is change a
    network's profile: flipping a Public network to Private weakens the
    firewall for everything on it, and is the user's decision to make."""
    lines = []
    for name in _legacy_rule_names(port):
        if name in existing:
            lines.append(f'netsh advfirewall firewall delete rule name="{name}"')
    rule = _port_rule_name(port)
    if rule not in existing:
        lines.append(
            f'netsh advfirewall firewall add rule name="{rule}" '
            f'protocol=TCP dir=in localport={port} action=allow '
            f'profile=private remoteip=localsubnet')
    return lines


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        if _network_is_public():
            # Not changed for you: a Public profile keeps the firewall
            # strict for everything on this network, and the rule below
            # only applies to Private ones.
            print("[Dashboard] This network is set to Public, so phones on "
                  "it cannot reach the dashboard.")
            print("[Dashboard] If it is your home network, set it to Private "
                  "in Windows Settings > Network & internet.")

        existing = {name for name in
                    (_port_rule_name(port),) + _legacy_rule_names(port)
                    if _netsh_rule_exists(name)}
        commands = _windows_firewall_plan(port, existing)
        if not commands:
            return  # already configured, and nothing broad left behind

        # Build a .bat file — netsh only, runs fast when elevated
        bat_lines = ["@echo off"] + commands

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do




# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._tokens: dict[str, float]    = {}   # auth_token → expires at
        self._now                         = time.time
        self._tickets: dict[str, tuple]   = {}   # ticket → (filename, expires)
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        # auth_token → {"salt": bytes, "key": bytes | None, "seen": dict}
        self._token_crypto: dict[str, dict] = {}
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._throttle                    = LoginThrottle()
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(KEY_LENGTH))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    # ── tokens ───────────────────────────────────────────────────────────

    def _issue_token(self, session_key: str) -> str:
        """A new login token, good for SESSION_TTL_S."""
        self._prune()
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = self._now() + SESSION_TTL_S
        self._token_keys[tok] = session_key
        # A salt per login. The key itself is derived on first use, off the
        # event loop: KDF_ITERATIONS rounds take a noticeable moment.
        self._token_crypto[tok] = {"salt": secrets.token_bytes(16),
                                   "key": None, "seen": {}}
        return tok

    def _kdf_params(self, tok: str) -> dict:
        """What the page needs to derive this login's key."""
        salt = self._token_crypto[tok]["salt"]
        return {"salt": base64.b64encode(salt).decode("ascii"),
                "iterations": KDF_ITERATIONS}

    def _token_ok(self, tok: str) -> bool:
        expires = self._tokens.get(tok or "")
        if expires is None:
            return False
        if expires <= self._now():
            self._drop_token(tok)
            return False
        return True

    def _drop_token(self, tok: str) -> None:
        self._tokens.pop(tok, None)
        self._token_keys.pop(tok, None)
        self._token_crypto.pop(tok, None)

    def _prune(self) -> None:
        """Forget expired tokens and paired devices, and the keys only they
        used. Tokens used to live until the process did."""
        now = self._now()
        for tok, expires in list(self._tokens.items()):
            if expires <= now:
                self._drop_token(tok)
        for dev, info in list(self._device_sessions.items()):
            if info.get("expires", 0) <= now:
                del self._device_sessions[dev]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        """The text of a sealed command, or None: wrong key, another
        login's message, anything altered, or a nonce already seen. Blocking
        (the first call derives the key) -- call it off the event loop."""
        sk = self._token_keys.get(token)
        crypto = self._token_crypto.get(token)
        if not sk or crypto is None:
            return None
        try:
            if crypto["key"] is None:
                crypto["key"] = _derive_aes_key(sk, crypto["salt"])
            nonce, text = _open_gcm(crypto["key"], token, enc_b64)
        except Exception:
            return None
        seen = crypto["seen"]
        if nonce in seen:
            return None                                   # a replay
        seen[nonce] = True
        while len(seen) > _SEEN_NONCES:
            del seen[next(iter(seen))]
        return text

    async def _command_text(self, token: str, payload: dict):
        """(text, error). Sealed commands are opened; a plaintext one is
        accepted only over plain HTTP, where the page cannot seal (WebCrypto
        needs a secure context) and says so. Under HTTPS a token alone --
        leaked from a log, say -- cannot send a command."""
        enc = payload.get("enc", "")
        if enc:
            text = await asyncio.to_thread(self._decrypt, token, enc)
            if text is None:
                return None, "Decryption failed"
            return text.strip(), None
        if self._ssl_enabled():
            return None, "Commands must be encrypted over HTTPS"
        return (payload.get("text") or "").strip(), None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return self._token_ok(tok)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html.replace(
                "__KEY_LENGTH__", str(KEY_LENGTH)))

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            address = _client_address(req)
            wait = self._throttle.retry_after(address)
            if wait:
                return JSONResponse({"ok": False, "error": _too_many(wait)},
                                    status_code=429,
                                    headers={"Retry-After": str(int(wait) + 1)})
            try:
                body = await req.json()
            except Exception:
                body = {}
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                self._throttle.succeeded(address)
                del self._pending_keys[entered]          # one-time use
                tok = self._issue_token(entered)
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok,
                                     **self._kdf_params(tok)})
            self._throttle.failed(address)
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(req: Request, key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            address = _client_address(req)
            wait = self._throttle.retry_after(address)
            if wait:
                return HTMLResponse(f"<p>{_too_many(wait)}</p>", status_code=429)
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                self._throttle.failed(address)
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            self._throttle.succeeded(address)
            tok     = self._issue_token(key)
            kdf     = self._kdf_params(tok)
            dev_tok = secrets.token_urlsafe(32)
            self._device_sessions[dev_tok] = {
                "session_key": key, "expires": self._now() + DEVICE_TTL_S}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  sessionStorage.setItem('jarvis_salt','{kdf["salt"]}');
  sessionStorage.setItem('jarvis_iters','{kdf["iterations"]}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            address = _client_address(req)
            wait = self._throttle.retry_after(address)
            if wait:
                return JSONResponse({"ok": False, "error": _too_many(wait)},
                                    status_code=429)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            self._prune()
            if not dev_tok or dev_tok not in self._device_sessions:
                self._throttle.failed(address)
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = self._issue_token(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key,
                                 **self._kdf_params(tok)})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Sign out every other device: all paired-device tokens, and
            every login except the one asking. A lost phone otherwise kept
            its login until the token's lifetime ran out."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            mine = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            count = len(self._device_sessions)
            self._device_sessions.clear()
            others = [t for t in self._tokens if t != mine]
            for tok in others:
                self._drop_token(tok)
            self._prune()
            return JSONResponse({"ok": True, "revoked": count,
                                 "signed_out": len(others)})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            text, error = await self._command_text(token, body)
            if error:
                return JSONResponse({"error": error}, status_code=400)
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        async def _authenticate(websocket: WebSocket) -> str | None:
            """Accept, then require {"type": "auth", "token": ...} first.
            Returns the token, or closes the socket and returns None."""
            await websocket.accept()
            try:
                first = await asyncio.wait_for(websocket.receive_text(),
                                               WS_AUTH_TIMEOUT_S)
                import json as _json
                msg = _json.loads(first)
                tok = str(msg.get("token", "")).strip() \
                    if msg.get("type") == "auth" else ""
            except Exception:
                tok = ""
            if not self._token_ok(tok):
                try:
                    await websocket.close(code=4001)
                except Exception:
                    pass
                return None
            return tok

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket):
            if await _authenticate(websocket) is None:
                return
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.post("/api/download-ticket")
        async def download_ticket(req: Request):
            """A one-time, one-file, one-minute ticket for GET /uploads/.
            Asked for with the Authorization header, so the login token
            never appears in a URL."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                body = {}
            name = re.sub(r'[/\\]', '', str(body.get("name", "")))
            if not name:
                return JSONResponse({"error": "Which file?"}, status_code=400)
            now = self._now()
            self._tickets = {t: v for t, v in self._tickets.items()
                             if v[1] > now}
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = (name, now + TICKET_TTL_S)
            return JSONResponse({"ok": True, "ticket": ticket})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, ticket: str = ""):
            # A browser <a download> cannot send a header, so this takes a
            # one-time ticket from /api/download-ticket -- never the token.
            safe = re.sub(r'[/\\]', '', filename)
            entry = self._tickets.pop(ticket.strip(), None)
            if entry is None or entry[1] <= self._now() or entry[0] != safe:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket):
            tok = await _authenticate(websocket)
            if tok is None:
                return
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "command":
                        t, _error = await self._command_text(tok, data)
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_running_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_running_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
