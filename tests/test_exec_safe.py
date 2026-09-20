"""
Tests for core/exec_safe.py.

The only program these start is this same Python interpreter, with `-c` and a
one-line script. Nothing is installed, nothing reaches the network, and no
system state is changed — but a subprocess wrapper cannot be tested honestly
without actually running a subprocess, and the exit-code and timeout behaviour
are exactly the parts that matter.
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import exec_safe                                      # noqa: E402
from core.exec_safe import UnsafeCommand                        # noqa: E402

PY = sys.executable


def py(code: str) -> list:
    """argv that runs one line of Python in a fresh interpreter.

    `-I` is isolated mode: no user site-packages, no PYTHONPATH, no current
    directory on sys.path — so these tests cannot be perturbed by whatever is
    installed on the machine running them."""
    return [PY, "-I", "-c", code]


class TestCallValidation(unittest.TestCase):
    """The shapes that must be refused before anything is spawned."""

    def test_command_string_is_rejected(self):
        with self.assertRaises(UnsafeCommand) as ctx:
            exec_safe.run("echo hello", timeout=5)
        self.assertIn("list of arguments", str(ctx.exception))

    def test_bytes_command_is_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run(b"echo hello", timeout=5)

    def test_shell_true_is_rejected_by_name(self):
        with self.assertRaises(UnsafeCommand) as ctx:
            exec_safe.run(["echo", "hi"], timeout=5, shell=True)
        self.assertIn("shell", str(ctx.exception).lower())

    def test_shell_false_is_also_rejected_as_an_argument(self):
        # Not a trick question: there is no shell parameter at all, so even the
        # "safe" value is a sign the caller thinks one exists.
        with self.assertRaises(UnsafeCommand):
            exec_safe.run(["echo", "hi"], timeout=5, shell=False)

    def test_spawn_rejects_shell_too(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.spawn(["echo", "hi"], shell=True)

    def test_other_unknown_kwargs_are_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run(["echo"], timeout=5, executable="/bin/sh")

    def test_empty_argv_is_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run([], timeout=5)

    def test_empty_program_name_is_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run(["   ", "x"], timeout=5)

    def test_missing_timeout_is_a_type_error(self):
        # Keyword-only with no default: forgetting it cannot compile into a hang.
        with self.assertRaises(TypeError):
            exec_safe.run([PY, "-c", "pass"])          # noqa: missing timeout

    def test_none_timeout_is_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run(py("pass"), timeout=None)

    def test_non_positive_timeout_is_rejected(self):
        for bad in (0, -1, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeCommand):
                    exec_safe.run(py("pass"), timeout=bad)

    def test_timeout_is_capped(self):
        # A huge timeout is quietly clamped rather than honoured.
        self.assertEqual(
            exec_safe._validate_timeout(10 ** 9), exec_safe.MAX_TIMEOUT_SECONDS
        )

    def test_non_string_argv_entries_are_rejected(self):
        for bad in ([PY, "-c", None], [PY, "-c", 5], [PY, b"-c", "pass"]):
            with self.subTest(bad=bad):
                with self.assertRaises(UnsafeCommand):
                    exec_safe.run(bad, timeout=5)

    def test_null_byte_in_argument_is_rejected(self):
        with self.assertRaises(UnsafeCommand):
            exec_safe.run([PY, "-c", "pass\x00"], timeout=5)

    def test_path_objects_are_accepted(self):
        r = exec_safe.run([Path(PY), "-I", "-c", "pass"], timeout=20)
        self.assertTrue(r.ok, r.stderr)


class TestOutcomes(unittest.TestCase):

    def test_success(self):
        r = exec_safe.run(py("print('hello')"), timeout=20)
        self.assertTrue(r.ok)
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout)
        self.assertEqual(r.error, "")
        self.assertFalse(r.timed_out)
        self.assertEqual(r.failure_detail(), "")

    def test_non_zero_exit_is_reported_honestly(self):
        r = exec_safe.run(py("import sys; sys.exit(3)"), timeout=20)
        self.assertFalse(r.ok)
        self.assertEqual(r.returncode, 3)
        # A non-zero exit is a result, not an error: the process ran fine.
        self.assertEqual(r.error, "")
        self.assertIn("code 3", r.summary())

    def test_stderr_is_captured_and_offered_as_the_reason(self):
        r = exec_safe.run(
            py("import sys; sys.stderr.write('it broke'); sys.exit(1)"), timeout=20
        )
        self.assertFalse(r.ok)
        self.assertIn("it broke", r.stderr)
        self.assertEqual(r.failure_detail(), "it broke")

    def test_crash_gives_non_zero_not_an_exception(self):
        r = exec_safe.run(py("raise SystemExit(9)"), timeout=20)
        self.assertFalse(r.ok)
        self.assertEqual(r.returncode, 9)

    def test_missing_program_is_a_result_not_an_exception(self):
        r = exec_safe.run(["mark_liv_no_such_program_xyz"], timeout=5)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "FileNotFoundError")
        self.assertIsNone(r.returncode)
        self.assertIn("not installed", r.stderr)

    def test_result_never_carries_the_argv(self):
        secret = "hunter2-not-a-real-secret"
        r = exec_safe.run(py(f"print('ok')  # {secret}"), timeout=20)
        blob = " ".join([r.program, r.summary(), r.error, str(r.argv_len)])
        self.assertNotIn(secret, blob)
        self.assertEqual(r.program, Path(PY).name)
        self.assertEqual(r.argv_len, 4)

    def test_stdin_is_delivered(self):
        r = exec_safe.run(
            py("import sys; print(sys.stdin.read().strip().upper())"),
            timeout=20, stdin_text="quiet",
        )
        self.assertTrue(r.ok, r.stderr)
        self.assertIn("QUIET", r.stdout)

    def test_cwd_is_honoured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            r = exec_safe.run(py("import os; print(os.getcwd())"),
                              timeout=20, cwd=tmp)
            self.assertTrue(r.ok, r.stderr)
            self.assertEqual(
                Path(r.stdout.strip()).resolve(), Path(tmp).resolve()
            )


class TestTimeout(unittest.TestCase):

    def test_timeout_is_enforced_and_reported(self):
        started = time.monotonic()
        r = exec_safe.run(py("import time; time.sleep(30)"), timeout=0.5)
        elapsed = time.monotonic() - started

        self.assertTrue(r.timed_out)
        self.assertTrue(r.killed)
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "timeout")
        self.assertIn("timed out", r.summary())
        # Generous ceiling: 0.5s limit + up to KILL_GRACE for TERM→KILL, and
        # some slack for a loaded CI box. The point is that it returns at all.
        self.assertLess(elapsed, 20, "run() did not return promptly after timeout")

    def test_process_is_actually_dead_afterwards(self):
        r = exec_safe.run(
            py("import os, time; print(os.getpid(), flush=True); time.sleep(30)"),
            timeout=1.0,
        )
        self.assertTrue(r.timed_out)
        pid_text = r.stdout.strip().split()
        if not pid_text:
            self.skipTest("child produced no pid before being killed")
        pid = int(pid_text[0])

        # Give the kill a moment to land, then check the pid is gone.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(_pid_alive(pid), "timed-out process survived")

    @unittest.skipIf(os.name == "nt", "process groups differ on Windows")
    def test_child_of_a_timed_out_process_is_killed_too(self):
        # The parent starts a long-lived grandchild and exits the sleep itself.
        # Without a process-group kill the grandchild would be orphaned.
        code = (
            "import subprocess, sys, time;"
            "p = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)']);"
            "print(p.pid, flush=True);"
            "time.sleep(30)"
        )
        r = exec_safe.run(py(code), timeout=1.5)
        self.assertTrue(r.timed_out)
        out = r.stdout.strip().split()
        if not out:
            self.skipTest("grandchild pid not reported in time")
        grandchild = int(out[0])

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not _pid_alive(grandchild):
                break
            time.sleep(0.1)
        self.assertFalse(_pid_alive(grandchild), "grandchild outlived the timeout")


class TestOutputBounds(unittest.TestCase):

    def test_output_is_capped_and_flagged(self):
        r = exec_safe.run(
            py("import sys; sys.stdout.write('x' * 200000)"),
            timeout=30, max_output_bytes=1000,
        )
        self.assertTrue(r.ok, r.stderr)
        self.assertTrue(r.truncated)
        self.assertLessEqual(len(r.stdout), 1000)

    def test_a_flooding_child_still_exits(self):
        # The reader keeps draining past the cap, so the child never blocks on a
        # full pipe. If it did, this would hit the timeout instead of exiting 0.
        r = exec_safe.run(
            py("import sys\n"
               "for _ in range(400):\n"
               "    sys.stdout.write('y' * 4096)\n"),
            timeout=30, max_output_bytes=2048,
        )
        self.assertTrue(r.ok, f"flooding child did not exit cleanly: {r.summary()}")
        self.assertTrue(r.truncated)


class TestHelpers(unittest.TestCase):

    def test_resolve_program_finds_the_interpreter(self):
        self.assertIsNotNone(exec_safe.resolve_program(Path(PY).name))

    def test_resolve_program_returns_none_for_nonsense(self):
        self.assertIsNone(exec_safe.resolve_program("mark_liv_no_such_program_xyz"))
        self.assertIsNone(exec_safe.resolve_program(""))
        self.assertIsNone(exec_safe.resolve_program(None))

    def test_python_executable_is_this_interpreter(self):
        self.assertEqual(exec_safe.python_executable(), sys.executable)

    def test_spawn_reports_a_missing_program(self):
        started, detail = exec_safe.spawn(["mark_liv_no_such_program_xyz"])
        self.assertFalse(started)
        self.assertIn("not installed", detail)

    def test_spawn_starts_something_real(self):
        started, _ = exec_safe.spawn(py("pass"))
        self.assertTrue(started)

    def test_spawned_processes_are_reaped(self):
        # spawn() never waits — that is the point — but the handles are kept and
        # collected, so a session that launches apps all day does not leave a
        # trail of zombies on POSIX.
        exec_safe.spawn(py("pass"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            exec_safe.reap()
            with exec_safe._spawned_lock:
                if not exec_safe._spawned:
                    break
            time.sleep(0.05)
        with exec_safe._spawned_lock:
            self.assertEqual(exec_safe._spawned, [], "spawned process was never reaped")

    def test_tracked_spawn_list_is_bounded(self):
        # A process that never exits must not make the list grow forever.
        with exec_safe._spawned_lock:
            exec_safe._spawned.clear()
        for _ in range(exec_safe._MAX_TRACKED_SPAWNS + 20):
            exec_safe._track(_FakeProc())
        with exec_safe._spawned_lock:
            self.assertLessEqual(len(exec_safe._spawned), exec_safe._MAX_TRACKED_SPAWNS)
            exec_safe._spawned.clear()


class _FakeProc:
    """Stands in for a process that is still running, without starting one."""

    def poll(self):
        return None


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        r = exec_safe.run(
            ["tasklist", "/FI", f"PID eq {pid}"], timeout=10
        )
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # On POSIX a dead-but-unreaped child answers signal 0 as a zombie. These
    # grandchildren are reaped by init, so this is accurate enough here.
    return True


if __name__ == "__main__":
    unittest.main()
