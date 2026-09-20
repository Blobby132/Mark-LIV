"""
core/audit.py — a durable record of what the assistant actually did.

WHY
    There was no record at all. `player.write_log()` puts a line on the HUD,
    where it scrolls away in under a minute and is gone at the next restart, and
    everything else is `print()` into a console nobody is reading. So the honest
    answer to "did it send that message?", "what did it delete last Tuesday?" or
    "did I approve that?" was: nobody can tell.

    An assistant with this much reach needs to be answerable afterwards, not
    only careful beforehand. This is the afterwards.

WHAT IT REFUSES TO KNOW
    An audit log is a file full of the most sensitive moments of someone's day,
    written automatically, forever. So the format is a fixed set of fields and
    there is no way to put free content into it:

      - `record()` takes named arguments and nothing else. There is no
        `**extra`, no `data=`, no dict passthrough. A caller *cannot* hand it a
        message body, because there is no parameter that would accept one.
      - every string that does go in runs through `_redact()`, which strips
        API keys, tokens, bearer headers, cookies, passwords, long hex/base64
        runs and URL credentials, then truncates.
      - URLs keep scheme, host and one path segment. Query strings are where
        session tokens and reset links live, and they are dropped whole.
      - file *contents* are never a parameter. Typed text, message bodies,
        clipboard contents and page text are never a parameter.

    Paths are the one deliberate exception, and they are recorded relative to
    home (`~/Desktop/notes.txt`). A log that cannot say which file was deleted
    cannot answer the question it exists for. Paths outside home and temp are
    reduced to their last component, because those are the ones that describe
    the machine rather than the user's own work. The file is created 0600.

BOUNDED
    JSON Lines, size-capped, rotated, with a fixed number of kept generations —
    so the worst case is a known number of megabytes rather than a file that
    grows for a year. See MAX_BYTES / KEEP_GENERATIONS.

NEVER THE CAUSE OF A FAILURE
    Every public function swallows its own exceptions and returns a bool. A full
    disk, a read-only home directory or a permissions problem must not take down
    the assistant, and must certainly not abort the action being audited — a
    failed log entry about a successful deletion is bad; a crash between the
    deletion and its record is worse.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from core import safe_path

# ── Where, and how much ──────────────────────────────────────────────────────
#
# ~/.jarvis is where this app already keeps per-user runtime state (see
# actions/reminder.py). Deliberately not inside the repository: an audit log
# that a reinstall wipes is not much of an audit log.
DEFAULT_DIR = Path.home() / ".jarvis"
DEFAULT_FILENAME = "audit.jsonl"

MAX_BYTES = 2 * 1024 * 1024          # per file
KEEP_GENERATIONS = 3                 # audit.jsonl.1 … .3
# Worst case on disk: (KEEP_GENERATIONS + 1) * MAX_BYTES = 8 MiB.

# Per-field truncation. Long enough to identify something, short enough that a
# runaway string cannot fill the log with one entry.
_MAX_FIELD = 200
_MAX_PATH = 160


# ── Outcomes, as constants so call sites cannot drift ────────────────────────

ALLOWED = "allowed"           # permitted with no question asked
PENDING = "pending"           # waiting on the human at the HUD
APPROVED = "approved"         # the human pressed CONFIRM
CANCELLED = "cancelled"       # the human pressed CANCEL
EXPIRED = "expired"           # nobody answered in time
DENIED = "denied"             # policy said no; no question was asked
SUCCEEDED = "succeeded"       # ran, and the underlying operation worked
FAILED = "failed"             # ran, and it did not


# ── Redaction ────────────────────────────────────────────────────────────────

_SECRET_PATTERNS = (
    # Google API keys — the exact shape this app stores in config/api_keys.json.
    re.compile(r"AIza[0-9A-Za-z\-_]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{10,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{10,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{10,}=*"),
    # key=value / key: value for anything that names itself a secret.
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token|"
        r"secret|password|passwd|pwd|auth|authorization|cookie|session[_-]?id)"
        r"\s*[=:]\s*[^\s,;]+"
    ),
    # Bare high-entropy runs: hex digests and base64 blobs.
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
)

_REDACTED = "[redacted]"


def _redact(value, limit: int = _MAX_FIELD) -> str:
    """Scrub, flatten and truncate an arbitrary string.

    Conservative by design: a false positive costs a slightly less readable log
    line, a false negative writes someone's API key to disk."""
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return "[unprintable]"

    # Newlines would break the one-event-per-line format, and a multi-line
    # value is usually content rather than an identifier.
    text = " ".join(text.split())

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)

    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _redact_url(value) -> str:
    """Scheme, host and the first path segment. Nothing else.

    A URL's query string is where a password-reset token, a session id or a
    signed download link lives, so it never reaches the log — and neither does
    `user:pass@` credentials in the netloc."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except Exception:
        return _redact(raw, 60)

    if not parts.scheme and not parts.netloc:
        return _redact(raw, 60)

    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"

    segments = [s for s in (parts.path or "").split("/") if s]
    path = f"/{segments[0]}" if segments else ""
    tail = "/…" if len(segments) > 1 else ""
    query = "?…" if (parts.query or parts.fragment) else ""

    return _redact(f"{parts.scheme}://{host}{path}{tail}{query}", _MAX_FIELD)


def _redact_path(value) -> str:
    """A path, reduced to something identifying but not revealing.

    Inside home     → '~/Desktop/notes.txt'
    Inside temp     → '<temp>/upload.pdf'
    Anywhere else   → '<system>/hosts' (last component only)

    Not resolved through symlinks: resolving touches the disk and would record
    where a link points rather than what the user asked for."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            return _redact(raw, _MAX_PATH)

        for root, label in ((safe_path.temp_root(), "<temp>"),
                            (safe_path.home_root(), "~")):
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            return _redact(f"{label}/{relative.as_posix()}", _MAX_PATH)

        return _redact(f"<system>/{candidate.name}", _MAX_PATH)
    except Exception:
        return _redact(raw, _MAX_PATH)


# ── Configuration (tests point this somewhere disposable) ────────────────────

_lock = threading.Lock()
_path: Path = DEFAULT_DIR / DEFAULT_FILENAME
_max_bytes: int = MAX_BYTES
_keep: int = KEEP_GENERATIONS
_enabled: bool = True
_mirror = None                       # optional (str) -> None, e.g. the HUD log
_last_error: str = ""


def configure(path=None, *, max_bytes: int | None = None,
              keep: int | None = None, enabled: bool | None = None,
              mirror=None) -> None:
    """Point the log somewhere else, resize it, or turn it off.

    Called from tests and, optionally, once at startup. There is intentionally
    no way to reach this from a tool call: the model can neither redirect the
    audit log nor switch it off."""
    global _path, _max_bytes, _keep, _enabled, _mirror
    with _lock:
        if path is not None:
            _path = Path(path)
        if max_bytes is not None:
            _max_bytes = max(1024, int(max_bytes))
        if keep is not None:
            _keep = max(0, int(keep))
        if enabled is not None:
            _enabled = bool(enabled)
        if mirror is not None:
            _mirror = mirror


def log_path() -> Path:
    with _lock:
        return _path


def last_error() -> str:
    """Why the most recent write failed, or ''. For a diagnostics panel — the
    assistant should be able to say "I am not recording anything" if it isn't."""
    with _lock:
        return _last_error


# ── Writing ──────────────────────────────────────────────────────────────────

def _rotate_locked(path: Path) -> None:
    """audit.jsonl → .1 → .2 → .3, oldest discarded. Caller holds the lock."""
    if _keep <= 0:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return

    oldest = path.with_suffix(path.suffix + f".{_keep}")
    try:
        oldest.unlink(missing_ok=True)
    except Exception:
        pass

    for generation in range(_keep - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{generation}")
        dst = path.with_suffix(path.suffix + f".{generation + 1}")
        if src.exists():
            try:
                src.replace(dst)
            except Exception:
                pass

    try:
        path.replace(path.with_suffix(path.suffix + ".1"))
    except Exception:
        pass


def _write_locked(line: str) -> None:
    """Append one line, rotating first if it would push the file over. Caller
    holds the lock."""
    _path.parent.mkdir(parents=True, exist_ok=True)

    try:
        size = _path.stat().st_size
    except FileNotFoundError:
        size = 0

    if size and size + len(line) > _max_bytes:
        _rotate_locked(_path)

    # os.open with 0o600 so the file is owner-only from the moment it exists —
    # chmod after the fact leaves a window where it is world-readable.
    fd = os.open(_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
    except BaseException:
        try:
            os.close(fd)
        except Exception:
            pass
        raise


def record(
    event: str,
    *,
    action: str = "",
    capability: str = "",
    decision: str = "",
    outcome: str = "",
    confirmation_required: bool | None = None,
    approved: bool | None = None,
    target: str = "",
    target_kind: str = "",
    url: str = "",
    program: str = "",
    error_class: str = "",
    error: str = "",
    duration_ms: int | None = None,
    note: str = "",
) -> bool:
    """Append one event. Returns True if it reached the disk.

    Every parameter is named and every string is redacted. There is no
    parameter for file contents, message bodies or typed text, and adding one
    would be the wrong fix for whatever made it seem necessary.

    `target` is a filesystem path (reduced by `_redact_path`); `url` is a URL
    (reduced to scheme/host/first segment). `note` is a short free string for a
    human — redacted and truncated like everything else, and not a place to put
    what the user said."""
    global _last_error

    if not _enabled:
        return False

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": _redact(event, 40),
    }

    if action:
        entry["action"] = _redact(action, 60)
    if capability:
        entry["capability"] = _redact(capability, 40)
    if decision:
        entry["decision"] = _redact(decision, 30)
    if outcome:
        entry["outcome"] = _redact(outcome, 30)
    if confirmation_required is not None:
        entry["confirmation_required"] = bool(confirmation_required)
    if approved is not None:
        entry["approved"] = bool(approved)
    if target:
        entry["target"] = _redact_path(target)
    if target_kind:
        entry["target_kind"] = _redact(target_kind, 30)
    elif target:
        entry["target_kind"] = safe_path.classify_path(target)
    if url:
        entry["url"] = _redact_url(url)
    if program:
        entry["program"] = _redact(Path(str(program)).name, 60)
    if error_class:
        entry["error_class"] = _redact(error_class, 60)
    if error:
        entry["error"] = _redact(error, _MAX_FIELD)
    if duration_ms is not None:
        try:
            entry["duration_ms"] = max(0, int(duration_ms))
        except Exception:
            pass
    if note:
        entry["note"] = _redact(note, _MAX_FIELD)

    try:
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    except Exception as e:
        with _lock:
            _last_error = f"{type(e).__name__}: could not serialise entry"
        return False

    try:
        with _lock:
            _write_locked(line)
            _last_error = ""
        _mirror_line(entry)
        return True
    except Exception as e:
        # The whole point of this clause: a broken audit log must never be the
        # reason an action fails or the app dies.
        with _lock:
            _last_error = f"{type(e).__name__}"
        return False


def _mirror_line(entry: dict) -> None:
    """Optionally echo a one-line summary to the HUD's activity log."""
    mirror = _mirror
    if mirror is None:
        return
    try:
        bits = [entry.get("action") or entry.get("event", "")]
        if entry.get("outcome"):
            bits.append(entry["outcome"])
        if entry.get("target"):
            bits.append(entry["target"])
        mirror("AUDIT: " + " — ".join(b for b in bits if b))
    except Exception:
        pass


# ── Reading back ─────────────────────────────────────────────────────────────

def recent(limit: int = 50) -> list[dict]:
    """The most recent entries, newest last. Current generation only.

    For a diagnostics view, and for tests. Never raises: an unreadable or
    half-written log returns what could be parsed."""
    try:
        with _lock:
            path = _path
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []

    out: list[dict] = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


# ── Convenience wrappers ─────────────────────────────────────────────────────
#
# Thin, and named for the thing that happened rather than for the fields they
# fill in, so call sites read as sentences.

def decision(action: str, capability: str, verdict: str, *,
             required: bool, target: str = "", url: str = "",
             note: str = "") -> bool:
    """The broker reached a verdict about a capability."""
    return record("permission", action=action, capability=capability,
                  decision=verdict, confirmation_required=required,
                  target=target, url=url, note=note)


def result(action: str, capability: str, *, ok: bool, target: str = "",
           url: str = "", program: str = "", error_class: str = "",
           error: str = "", duration_ms: int | None = None,
           approved: bool | None = None) -> bool:
    """An action finished, one way or the other."""
    return record("result", action=action, capability=capability,
                  outcome=SUCCEEDED if ok else FAILED,
                  target=target, url=url, program=program,
                  error_class=error_class, error=error,
                  duration_ms=duration_ms, approved=approved)


def confirmation(action: str, capability: str, outcome_name: str, *,
                 target: str = "", note: str = "") -> bool:
    """A human answered — or failed to answer — a confirmation banner."""
    return record("confirmation", action=action, capability=capability,
                  outcome=outcome_name,
                  approved=(outcome_name == APPROVED),
                  confirmation_required=True, target=target, note=note)


__all__ = [
    "record", "decision", "result", "confirmation", "recent",
    "configure", "log_path", "last_error",
    "ALLOWED", "PENDING", "APPROVED", "CANCELLED", "EXPIRED", "DENIED",
    "SUCCEEDED", "FAILED",
    "MAX_BYTES", "KEEP_GENERATIONS",
]
