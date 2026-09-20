"""
Regression tests for the specific holes Security Phase 2 closed.

Each test names the vulnerability it pins. They are written against the real
action modules rather than against a mock, because the bug in every case was
that a real call site forgot to use the check that already existed — so a test
that exercises the check directly would have passed before the fix too.

Some of these modules import optional packages (pyautogui, psutil, PIL). Where
one is missing the test skips rather than failing: a skip is honest, a green
tick for a test that never ran is not.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import capabilities as caps                            # noqa: E402


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _stub_playwright() -> None:
    """Let browser_control import without Playwright installed.

    Its capability resolution is pure logic over a params dict and has nothing
    to do with a browser, but the module-level import means the whole file is
    unimportable without the package — so these tests would silently skip on
    any machine that has not run `playwright install`, which is exactly the
    machine where a regression would go unnoticed."""
    import types
    if "playwright.async_api" in sys.modules:
        return
    root = types.ModuleType("playwright")
    api = types.ModuleType("playwright.async_api")

    class _Stub:                       # stands in for the type annotations
        pass

    api.async_playwright = lambda *a, **k: _Stub()
    api.BrowserContext = _Stub
    api.Page = _Stub
    api.Playwright = _Stub
    api.TimeoutError = type("TimeoutError", (Exception,), {})
    root.async_api = api
    sys.modules.setdefault("playwright", root)
    sys.modules.setdefault("playwright.async_api", api)


class _HomeSandbox(unittest.TestCase):
    """Points HOME at a temp directory so 'inside the home folder' means
    'inside this test', and the real home is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fake_home = Path(self._tmp.name) / "home"
        (self.fake_home / "Desktop").mkdir(parents=True)
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("private", encoding="utf-8")

        self._saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.fake_home)
        os.environ["USERPROFILE"] = str(self.fake_home)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


# ── file_controller.rename_file traversal ────────────────────────────────────

class TestRenameTraversal(_HomeSandbox):
    """`rename_file` validated `target` and then acted on
    `target.parent / new_name`, which it never checked. `new_name` is written
    by the model."""

    def setUp(self):
        super().setUp()
        self.fc = _try_import("actions.file_controller")
        if self.fc is None:
            self.skipTest("file_controller could not be imported here")
        importlib.reload(self.fc)
        self.fc._SAFE_ROOTS = [self.fake_home]

        self.victim = self.fake_home / "Desktop" / "notes.txt"
        self.victim.write_text("mine", encoding="utf-8")

    def test_dotdot_rename_is_refused(self):
        for attempt in ("../../../outside/owned.txt",
                        "../../outside/owned.txt",
                        "..\\..\\..\\outside\\owned.txt"):
            with self.subTest(attempt=attempt):
                result = self.fc.rename_file("desktop", name="notes.txt",
                                             new_name=attempt)
                self.assertTrue(self.victim.exists(),
                                "the file was moved out of the safe root")
                self.assertFalse((self.outside / "owned.txt").exists())
                self.assertTrue(
                    "not rename" in result.lower() or "not a name" in result.lower(),
                    f"unclear refusal: {result}")

    def test_a_rename_cannot_be_a_path(self):
        # A rename produces a name. On POSIX a backslash is a legal filename
        # character, so without this the file is quietly renamed to the literal
        # string "..\\..\\outside\\owned.txt".
        for attempt in ("sub/dir/name.txt", "..\\..\\outside\\owned.txt",
                        "a/b"):
            with self.subTest(attempt=attempt):
                result = self.fc.rename_file("desktop", name="notes.txt",
                                             new_name=attempt)
                self.assertTrue(self.victim.exists())
                self.assertIn("not a name", result.lower())

    def test_absolute_rename_is_refused(self):
        target = self.outside / "owned.txt"
        result = self.fc.rename_file("desktop", name="notes.txt",
                                     new_name=str(target))
        self.assertTrue(self.victim.exists())
        self.assertFalse(target.exists())
        self.assertTrue(
            "not rename" in result.lower() or "not a name" in result.lower(),
            f"unclear refusal: {result}")

    def test_an_ordinary_rename_still_works(self):
        result = self.fc.rename_file("desktop", name="notes.txt",
                                     new_name="renamed.txt")
        self.assertFalse(self.victim.exists())
        self.assertTrue((self.fake_home / "Desktop" / "renamed.txt").exists())
        self.assertIn("renamed", result.lower())

    def test_move_destination_is_revalidated(self):
        result = self.fc.move_file("desktop", name="notes.txt",
                                   destination=str(self.outside))
        self.assertTrue(self.victim.exists(), "move escaped the safe root")
        self.assertFalse((self.outside / "notes.txt").exists())
        self.assertTrue(
            "denied" in result.lower() or "outside" in result.lower(),
            f"move out of the safe root was not refused clearly: {result}")


# ── file_processor archive extraction (tar slip) ─────────────────────────────

class TestArchiveExtraction(_HomeSandbox):
    """`shutil.unpack_archive` on Python 3.11 is `tarfile.extractall` with no
    filter, and the destination was model-supplied and unchecked."""

    def setUp(self):
        super().setUp()
        self.fp = _try_import("actions.file_processor")
        if self.fp is None:
            self.skipTest("file_processor could not be imported here")
        self.dest = self.fake_home / "unpacked"

    def _tar_with(self, name: str, data: bytes = b"pwned") -> Path:
        path = self.fake_home / "evil.tar"
        with tarfile.open(path, "w") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        return path

    def test_tar_slip_is_refused(self):
        archive = self._tar_with("../../outside/owned.txt")
        result = self.fp._process_archive(archive, "extract",
                                          {"destination": str(self.dest)})
        self.assertFalse((self.outside / "owned.txt").exists(),
                         "tar slip wrote outside the destination")
        self.assertIn("refused", result.lower())

    def test_absolute_tar_member_is_refused(self):
        archive = self._tar_with("/tmp/mark_liv_should_not_exist.txt")
        result = self.fp._process_archive(archive, "extract",
                                          {"destination": str(self.dest)})
        self.assertFalse(Path("/tmp/mark_liv_should_not_exist.txt").exists())
        self.assertIn("refused", result.lower())

    def test_tar_symlink_member_is_refused(self):
        path = self.fake_home / "evil.tar"
        with tarfile.open(path, "w") as tf:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        result = self.fp._process_archive(path, "extract",
                                          {"destination": str(self.dest)})
        self.assertIn("refused", result.lower())
        self.assertFalse((self.dest / "link").exists())

    def test_destination_outside_the_allowed_roots_is_refused(self):
        archive = self.fake_home / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "fine")
        # Outside home and outside the system temp dir. Refused by
        # containment before anything is written, so this is safe to assert
        # even when the test runs as root.
        result = self.fp._process_archive(archive, "extract",
                                          {"destination": "/etc/mark_liv_probe"})
        self.assertFalse(Path("/etc/mark_liv_probe").exists())
        self.assertIn("outside", result.lower())

    def test_an_ordinary_archive_still_extracts(self):
        archive = self.fake_home / "ok.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("a.txt", "fine")
            zf.writestr("sub/b.txt", "also fine")
        result = self.fp._process_archive(archive, "extract",
                                          {"destination": str(self.dest)})
        self.assertEqual((self.dest / "a.txt").read_text(), "fine")
        self.assertEqual((self.dest / "sub" / "b.txt").read_text(), "also fine")
        self.assertIn("extracted", result.lower())


# ── file_processor had no containment at all ─────────────────────────────────

class TestFileProcessorContainment(_HomeSandbox):

    def setUp(self):
        super().setUp()
        self.fp = _try_import("actions.file_processor")
        if self.fp is None:
            self.skipTest("file_processor could not be imported here")

    def test_a_system_path_is_refused(self):
        # Note the allowed roots are home AND the system temp directory —
        # uploads land in temp, so a temp path is legitimately allowed. A
        # system path is not.
        for attempt in ("/etc/passwd", "/etc/shadow", "/etc/hostname"):
            with self.subTest(attempt=attempt):
                result = self.fp.file_processor({"file_path": attempt,
                                                 "action": "summarize"})
                self.assertIn("outside", result.lower())

    def test_traversal_out_of_home_is_refused(self):
        sneaky = str(self.fake_home / ".." / ".." / ".." / "etc" / "passwd")
        result = self.fp.file_processor({"file_path": sneaky, "action": "summarize"})
        self.assertIn("outside", result.lower())


# ── open_app: shell injection and false success ──────────────────────────────

class TestOpenAppInjection(unittest.TestCase):

    def setUp(self):
        self.oa = _try_import("actions.open_app")
        if self.oa is None:
            self.skipTest("open_app could not be imported here")

    def test_no_shell_is_used_anywhere_in_the_module(self):
        source = Path(self.oa.__file__).read_text(encoding="utf-8")
        code_lines = [
            line for line in source.splitlines()
            if "shell=True" in line
            and not line.strip().startswith("#")
            and "subprocess.Popen(app_name" not in line
            and 'f"start {app_name}"' not in line
        ]
        self.assertEqual(code_lines, [], f"shell=True is back: {code_lines}")

    def test_the_module_no_longer_imports_subprocess_directly(self):
        source = Path(self.oa.__file__).read_text(encoding="utf-8")
        self.assertNotIn("\nimport subprocess", source,
                         "open_app should go through core/exec_safe.py")

    def test_control_characters_are_stripped_from_the_app_name(self):
        # Two of the launch fallbacks TYPE this string into the Start Menu or
        # Spotlight, where a newline is an Enter key.
        for attempt in ("Spotify\nrm -rf ~", "Spotify\r\ncalc", "Calc\x00ulator"):
            with self.subTest(attempt=attempt):
                cleaned = self.oa._clean_app_name(attempt)
                self.assertNotIn("\n", cleaned)
                self.assertNotIn("\r", cleaned)
                self.assertNotIn("\x00", cleaned)

    def test_app_name_length_is_bounded(self):
        self.assertLessEqual(len(self.oa._clean_app_name("x" * 5000)), 80)

    def test_a_shell_is_a_different_capability_from_an_app(self):
        self.assertEqual(self.oa._capability({"app_name": "Spotify"}),
                         caps.APP_LAUNCH)
        for shell in ("cmd", "powershell", "bash", "terminal", "wt"):
            with self.subTest(shell=shell):
                self.assertEqual(self.oa._capability({"app_name": shell}),
                                 caps.APP_LAUNCH_SHELL)

    def test_a_missing_program_is_reported_as_not_opened(self):
        # The old code returned True after xdg-open regardless of exit code.
        result = self.oa.open_app({"app_name": "mark_liv_definitely_not_installed"})
        self.assertNotIn("Opened mark_liv", result)


# ── desktop.py: exec() of model output in this interpreter ───────────────────

class TestDesktopNoInProcessExec(unittest.TestCase):

    def setUp(self):
        self.desktop = _try_import("actions.desktop")
        if self.desktop is None:
            self.skipTest("desktop could not be imported here")
        self.source = Path(self.desktop.__file__).read_text(encoding="utf-8")

    def test_no_exec_of_generated_code_remains(self):
        """Parsed, not grepped: the file legitimately contains the words
        'exec' and 'eval' inside the prompt it sends to Gemini and inside the
        comment explaining what was removed."""
        import ast as _ast
        offenders = []
        for node in _ast.walk(_ast.parse(self.source)):
            if not isinstance(node, _ast.Call):
                continue
            func = node.func
            if isinstance(func, _ast.Name) and func.id in ("exec", "eval",
                                                           "compile"):
                offenders.append(f"{func.id} at line {node.lineno}")
        self.assertEqual(offenders, [],
                         f"in-process execution is back: {offenders}")

    def test_the_sandbox_that_was_not_one_is_gone(self):
        self.assertFalse(hasattr(self.desktop, "_build_sandbox"),
                         "_build_sandbox still exists; a restricted globals "
                         "dict is not a sandbox")

    def test_in_process_execution_is_denied_by_policy(self):
        self.assertEqual(caps.decision_for(caps.CODE_EXEC_IN_PROCESS), caps.DENY)

    def test_generated_code_is_a_confirmed_capability(self):
        self.assertEqual(self.desktop._desktop_capability({"action": "task"}),
                         caps.CODE_EXEC)
        self.assertTrue(caps.requires_confirmation(caps.CODE_EXEC))

    def test_an_unrecognised_action_does_not_get_a_cheap_capability(self):
        # It falls through to "ask Gemini for code and run it", so it must be
        # priced as that.
        self.assertEqual(self.desktop._desktop_capability({"action": "wat"}),
                         caps.CODE_EXEC)

    def test_oversized_generated_code_is_refused(self):
        result = self.desktop._execute_generated_code("x = 1\n" * 20000)
        self.assertIn("not run it", result.lower())

    def test_the_unsafe_sentinel_is_honoured(self):
        self.assertIn("not done it", self.desktop._execute_generated_code("UNSAFE"))


# ── dev_agent: model-chosen paths and pip arguments ──────────────────────────

class TestDevAgentContainment(_HomeSandbox):

    def setUp(self):
        super().setUp()
        self.da = _try_import("actions.dev_agent")
        if self.da is None:
            self.skipTest("dev_agent could not be imported here")
        self.project = self.fake_home / "project"
        self.project.mkdir()

    def test_a_plan_cannot_write_outside_the_project(self):
        from core.safe_path import PathEscape
        for attempt in ("../../../outside/owned.py", "/etc/cron.d/evil",
                        "../escaped.py"):
            with self.subTest(attempt=attempt):
                with self.assertRaises(PathEscape):
                    self.da._safe_project_file(self.project, attempt)

    def test_an_ordinary_plan_path_is_fine(self):
        got = self.da._safe_project_file(self.project, "utils/helpers.py")
        self.assertEqual(got, self.project / "utils" / "helpers.py")

    def test_pip_flags_disguised_as_packages_are_rejected(self):
        for attempt in ("-i", "--index-url", "-e", "--find-links",
                        "-r requirements.txt", "; rm -rf ~", "--upgrade"):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.da._safe_requirement(attempt), "")

    def test_real_requirements_are_accepted(self):
        for ok in ("requests", "flask==2.0.1", "numpy>=1.20", "python-dateutil",
                   "uvicorn[standard]"):
            with self.subTest(ok=ok):
                self.assertEqual(self.da._safe_requirement(ok), ok)

    def test_a_crafted_traceback_cannot_choose_a_pip_flag(self):
        # _try_auto_install reads a package name out of program output.
        self.assertFalse(self.da._try_auto_install(
            "ModuleNotFoundError: No module named '-i'", self.project))


# ── code_helper: absolute-path writes ────────────────────────────────────────

class TestCodeHelperContainment(_HomeSandbox):

    def setUp(self):
        super().setUp()
        self.ch = _try_import("actions.code_helper")
        if self.ch is None:
            self.skipTest("code_helper could not be imported here")
        importlib.reload(self.ch)

    def test_an_absolute_path_outside_home_is_refused(self):
        from core.safe_path import PathEscape
        with self.assertRaises(PathEscape):
            self.ch._resolve_save_path(str(self.outside / "owned.py"), "python")

    def test_traversal_is_refused(self):
        from core.safe_path import PathEscape
        with self.assertRaises(PathEscape):
            self.ch._resolve_save_path("../../../../etc/profile", "python")

    def test_running_code_requires_confirmation(self):
        self.assertEqual(self.ch._ch_capability({"action": "run"}), caps.CODE_EXEC)
        self.assertEqual(self.ch._ch_capability({"action": "build"}), caps.CODE_EXEC)
        self.assertTrue(caps.requires_confirmation(caps.CODE_EXEC))

    def test_an_unrecognised_action_is_priced_as_execution(self):
        self.assertEqual(self.ch._ch_capability({"action": "???"}), caps.CODE_EXEC)


# ── computer_control: PowerShell / AppleScript injection ─────────────────────

class TestFocusWindowInjection(unittest.TestCase):

    def setUp(self):
        self.cc = _try_import("actions.computer_control")
        if self.cc is None:
            self.skipTest("computer_control could not be imported here")

    def test_quote_and_statement_characters_are_stripped(self):
        dangerous = 'Notepad"); Start-Process calc; ("'
        cleaned = self.cc._clean_title(dangerous)
        for char in ('"', "'", ";", "$", "`", "&", "|", "(", ")", "\\"):
            self.assertNotIn(char, cleaned, f"{char!r} survived cleaning")

    def test_title_length_is_bounded(self):
        self.assertLessEqual(len(self.cc._clean_title("x" * 9000)), 120)

    def test_an_ordinary_title_survives(self):
        self.assertEqual(self.cc._clean_title("Untitled - Notepad"),
                         "Untitled - Notepad")


# ── send_message: false success and logged bodies ────────────────────────────

class TestSendMessageHonesty(unittest.TestCase):

    def setUp(self):
        self.sm = _try_import("actions.send_message")
        if self.sm is None:
            self.skipTest("send_message could not be imported here")
        self.source = Path(self.sm.__file__).read_text(encoding="utf-8")

    def test_it_no_longer_claims_delivery(self):
        # It types into a window and presses Enter; it never observes delivery.
        self.assertNotIn('f"Message sent to {receiver}', self.source)

    def test_the_message_body_is_not_printed(self):
        self.assertNotIn("{preview}", self.source)
        self.assertNotIn("message_text[:50]", self.source)

    def test_the_confirmation_banner_carries_no_message_body(self):
        hints = self.sm._sm_guard({"receiver": "Mum", "platform": "WhatsApp",
                                   "message_text": "the password is swordfish"})
        blob = " ".join(str(v) for v in hints.values())
        self.assertNotIn("swordfish", blob)
        self.assertIn("Mum", blob)

    def test_sending_requires_confirmation(self):
        self.assertEqual(self.sm.TOOL["capability"], caps.MESSAGE_SEND)
        self.assertTrue(caps.requires_confirmation(caps.MESSAGE_SEND))


# ── browser_control: acting as the signed-in user ────────────────────────────

class TestBrowserCapabilities(unittest.TestCase):

    def setUp(self):
        _stub_playwright()
        self.bc = _try_import("actions.browser_control")
        if self.bc is None:
            self.skipTest("browser_control could not be imported here")

    def test_reading_and_navigating_are_free(self):
        for action in ("get_text", "get_url", "scroll", "back", "reload"):
            with self.subTest(action=action):
                self.assertFalse(caps.requires_confirmation(
                    self.bc._bc_capability({"action": action})))
        self.assertEqual(self.bc._bc_capability({"action": "go_to"}),
                         caps.BROWSER_NAVIGATE)

    def test_filling_a_form_requires_confirmation(self):
        self.assertEqual(self.bc._bc_capability({"action": "fill_form"}),
                         caps.BROWSER_SUBMIT)
        self.assertTrue(caps.requires_confirmation(caps.BROWSER_SUBMIT))

    def test_pressing_enter_counts_as_submitting(self):
        self.assertEqual(
            self.bc._bc_capability({"action": "press", "key": "Enter"}),
            caps.BROWSER_SUBMIT)

    def test_submit_shaped_clicks_require_confirmation(self):
        for target in ("Buy now", "Place order", "Delete account", "Log out",
                       "Confirm payment", "Post"):
            with self.subTest(target=target):
                self.assertEqual(
                    self.bc._bc_capability({"action": "smart_click",
                                            "description": target}),
                    caps.BROWSER_SUBMIT)

    def test_an_ordinary_click_does_not(self):
        self.assertEqual(
            self.bc._bc_capability({"action": "smart_click",
                                    "description": "Next page"}),
            caps.BROWSER_NAVIGATE)


# ── No shell anywhere in actions/ ────────────────────────────────────────────

class TestNoShellInActions(unittest.TestCase):
    """A property of the tree, not of one file — so a future edit that brings
    `shell=True` back fails here rather than in review.

    Parsed with `ast` rather than grepped, because several of these files now
    carry comments explaining the shell call they replaced, and a text search
    cannot tell an explanation from a call."""

    @staticmethod
    def _calls(tree):
        import ast as _ast
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                yield node

    def _action_trees(self):
        import ast as _ast
        for path in sorted(Path("actions").glob("*.py")):
            source = path.read_text(encoding="utf-8", errors="replace")
            yield path, _ast.parse(source)

    def test_no_call_passes_shell_true(self):
        import ast as _ast
        offenders = []
        for path, tree in self._action_trees():
            for call in self._calls(tree):
                for kw in call.keywords:
                    if kw.arg != "shell":
                        continue
                    value = kw.value
                    if isinstance(value, _ast.Constant) and value.value is False:
                        continue          # explicitly False is fine
                    offenders.append(f"{path.name}:{getattr(call, 'lineno', '?')}")
        self.assertEqual(offenders, [], f"shell= passed to a call: {offenders}")

    def test_no_os_system_call(self):
        import ast as _ast
        offenders = []
        for path, tree in self._action_trees():
            for call in self._calls(tree):
                func = call.func
                if (isinstance(func, _ast.Attribute) and func.attr == "system"
                        and isinstance(func.value, _ast.Name)
                        and func.value.id == "os"):
                    offenders.append(f"{path.name}:{call.lineno}")
        self.assertEqual(offenders, [], f"os.system found: {offenders}")

    def test_the_high_risk_files_go_through_exec_safe(self):
        # These are the files that actually start programs. If one of them
        # stops importing exec_safe, it has gone back to raw subprocess.
        for name in ("open_app.py", "dev_agent.py", "code_helper.py",
                     "desktop.py", "file_processor.py", "computer_control.py"):
            with self.subTest(name=name):
                source = (Path("actions") / name).read_text(encoding="utf-8")
                self.assertIn("exec_safe", source,
                              f"{name} no longer uses core/exec_safe.py")


if __name__ == "__main__":
    unittest.main()


class TestNoUnboundedBlockingCalls(unittest.TestCase):
    """Every blocking subprocess call in the shipped code has a deadline.

    72 of them did not. Each one ran on an executor thread, so a child that
    never returned took that thread with it for the rest of the session — the
    assistant simply stopped responding, with nothing in any log to say why.
    A tree-wide property rather than a per-file test, so a new call added
    without a timeout fails here."""

    FILES = (sorted(Path("actions").glob("*.py"))
             + [Path("core/installer.py"), Path("core/llm_client.py"),
                Path("core/tts.py"), Path("core/wake_word.py")])

    def test_every_blocking_subprocess_call_has_a_timeout(self):
        import ast as _ast
        offenders = []
        for path in self.FILES:
            if not path.exists():
                continue
            tree = _ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in _ast.walk(tree):
                if not (isinstance(node, _ast.Call)
                        and isinstance(node.func, _ast.Attribute)):
                    continue
                if not (isinstance(node.func.value, _ast.Name)
                        and node.func.value.id == "subprocess"):
                    continue
                # Popen does not block, so it needs no timeout — it needs the
                # caller not to wait on it, which is a different property.
                if node.func.attr not in ("run", "call", "check_output",
                                          "check_call"):
                    continue
                if not any(kw.arg == "timeout" for kw in node.keywords):
                    offenders.append(f"{path.as_posix()}:{node.lineno}")
        self.assertEqual(offenders, [],
                         f"blocking subprocess calls with no timeout: {offenders}")
