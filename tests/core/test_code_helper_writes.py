"""
code_helper's writes: edit, optimize, write and build.

1. One confined writer. edit and optimize read through
   safe_path.resolve_within_any but wrote to Path(file_path) as given, so a
   relative path was read from the home folder and written beside the
   process's working directory, outside the safe-path check. Every write now
   goes through _write_confined, which resolves the path the way the reader
   does and writes to the resolved path.

The model is a fake: gemini.call returns whatever the test queued. HOME is
a temp directory, and the working directory is another one, so a write
that lands in the wrong place shows up there. google.genai is stubbed the
way test_code_helper_screen_debug.py does, in case a path imports it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ORIGINAL = "".join(f"def function_{i}():\n    return {i}\n\n"
                   for i in range(20))


def _fake_genai_modules():
    google = types.ModuleType("google")
    genai = types.ModuleType("google.genai")
    gtypes = types.ModuleType("google.genai.types")

    class Part:
        @staticmethod
        def from_bytes(data, mime_type):
            return ("image", mime_type, len(data))

    gtypes.Part = Part
    genai.types = gtypes
    google.genai = genai
    return {"google": google, "google.genai": genai,
            "google.genai.types": gtypes}


class _Sandbox(unittest.TestCase):

    def setUp(self):
        try:
            from actions import code_helper
        except Exception as exc:                          # pragma: no cover
            self.skipTest(f"code_helper could not be imported here: {exc}")
        self.ch = code_helper

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / "Desktop").mkdir(parents=True)
        self.cwd = root / "elsewhere"           # the process's working dir
        self.cwd.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        # The reader's roots are home and the system temp directory, and
        # this sandbox lives in the system temp directory: the temp root is
        # narrowed to its own folder, so `outside` is outside both.
        self.temp = root / "tmp"
        self.temp.mkdir()

        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)
        self.addCleanup(self._restore_env, saved)
        old_cwd = os.getcwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, old_cwd)

        self.replies = []
        self.prompts = []

        def fake_call(contents, **kwargs):
            self.prompts.append(contents)
            text = self.replies.pop(0) if self.replies else ""
            return types.SimpleNamespace(text=text)

        for patcher in (
                mock.patch.dict(sys.modules, _fake_genai_modules()),
                mock.patch.object(self.ch, "DESKTOP", self.home / "Desktop"),
                mock.patch.object(self.ch.safe_path, "temp_root",
                                  return_value=self.temp.resolve()),
                mock.patch.object(self.ch.gemini, "call",
                                  side_effect=fake_call)):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def project_file(self, content=ORIGINAL, name="app.py"):
        path = self.home / "project" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def run_action(self, **params):
        return self.ch.code_helper(params)


class ConfinedWriterTests(_Sandbox):

    def test_edit_with_a_relative_path_writes_the_file_it_read(self):
        path = self.project_file()
        edited = ORIGINAL + "# edited\n"
        self.replies.append(edited)
        self.run_action(action="edit", file_path="project/app.py",
                        description="add a comment at the end")
        self.assertEqual(path.read_text(encoding="utf-8"), edited.strip())
        self.assertFalse((self.cwd / "project").exists(),
                         "the edit was written beside the working directory")

    def test_optimize_with_a_relative_path_writes_the_file_it_read(self):
        path = self.project_file()
        optimized = ORIGINAL.replace("return", "return  ")
        self.replies.append(optimized)
        self.run_action(action="optimize", file_path="project/app.py")
        self.assertEqual(path.read_text(encoding="utf-8"), optimized.strip())
        self.assertFalse((self.cwd / "project").exists(),
                         "the result was written beside the working directory")

    def test_an_escape_attempt_writes_nothing(self):
        for attempt in ("../outside/evil.py", str(self.outside / "evil.py")):
            with self.subTest(path=attempt):
                written, message = self.ch._write_confined(attempt, "x = 1\n")
                self.assertIsNone(written)
                self.assertIn("inside your home folder", message)
                self.assertFalse((self.outside / "evil.py").exists())

    def test_the_writer_writes_where_the_reader_reads(self):
        written, _message = self.ch._write_confined("project/new.py", "y = 2\n")
        self.assertEqual(written, (self.home / "project" / "new.py").resolve())
        content, err = self.ch._read_file("project/new.py")
        self.assertEqual((content, err), ("y = 2\n", ""))

    def test_write_still_lands_on_the_desktop(self):
        self.replies.append("print('hello')")
        self.run_action(action="write", description="say hello")
        self.assertEqual(
            (self.home / "Desktop" / "jarvis_code.py").read_text(),
            "print('hello')")

    def test_build_writes_through_the_confined_writer(self):
        self.replies.append("print('built')")
        with mock.patch.object(self.ch, "_run_file",
                               return_value="Output:\nbuilt"), \
                mock.patch.object(self.ch, "_write_confined",
                                  wraps=self.ch._write_confined) as writer:
            self.run_action(action="build", description="print built")
        self.assertTrue(writer.called)
        self.assertEqual(
            (self.home / "Desktop" / "jarvis_code.py").read_text(),
            "print('built')")



def long_source(chars):
    """A Python file of `chars` characters, give or take a line, ending in
    a marker."""
    lines, size, i = [], 0, 0
    while size < chars - 20:
        line = f"value_{i} = {i}\n"
        lines.append(line)
        size += len(line)
        i += 1
    return "".join(lines) + "# END OF FILE\n"


class OptimizeCapTests(_Sandbox):
    """2. optimize sent the model code[:6000] and wrote the reply over the
    whole file, so everything after 6,000 characters was lost even after
    the user confirmed. It now refuses a file over the cap, saying how long
    it is, and sends the whole file below it."""

    def test_a_file_over_the_cap_is_left_byte_identical(self):
        source = long_source(self.ch.OPTIMIZE_MAX_CHARS + 4000)
        path = self.project_file(source)
        before = path.read_bytes()
        self.replies.append(source[:self.ch.OPTIMIZE_MAX_CHARS])
        answer = self.run_action(action="optimize", file_path="project/app.py")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.prompts, [], "the model was asked anyway")
        self.assertIn(f"{len(source):,}", answer)
        self.assertIn(f"{self.ch.OPTIMIZE_MAX_CHARS:,}", answer)

    def test_inline_code_over_the_cap_writes_nothing(self):
        source = long_source(self.ch.OPTIMIZE_MAX_CHARS + 10)
        self.replies.append("x = 1")
        self.run_action(action="optimize", code=source)
        self.assertFalse((self.home / "Desktop" / "jarvis_code.py").exists())

    def test_under_the_cap_the_model_sees_the_whole_file(self):
        source = long_source(self.ch.OPTIMIZE_MAX_CHARS - 200)
        self.assertLessEqual(len(source), self.ch.OPTIMIZE_MAX_CHARS)
        self.project_file(source)
        self.replies.append(source)
        self.run_action(action="optimize", file_path="project/app.py")
        self.assertIn("# END OF FILE", self.prompts[0])



class ReplySanityTests(_Sandbox):
    """3. edit and optimize wrote whatever the model returned. A reply that
    is empty, an explanation instead of code, an explanation wrapped
    round the code, or a fraction of the file now writes nothing, and says
    why."""

    PROSE = ("I have reviewed the code carefully and it looks correct.\n"
             "There is nothing that needs to change in this file.\n"
             "You may want to add tests for the edge cases.\n")
    MIXED = ("Here is the updated code with the change applied:\n\n"
             f"```python\n{ORIGINAL}# edited\n```\n\n"
             "This adds a comment at the end of the file.")

    def assert_refused(self, action, reply, *words):
        path = self.project_file()
        before = path.read_bytes()
        self.replies.append(reply)
        params = {"action": action, "file_path": "project/app.py"}
        if action == "edit":
            params["description"] = "add a comment at the end"
        answer = self.run_action(**params)
        self.assertEqual(path.read_bytes(), before,
                         f"{action} wrote the reply: {reply[:40]!r}")
        self.assertIn("did not change", answer)
        for word in words:
            self.assertIn(word, answer)

    def test_an_empty_reply_writes_nothing(self):
        for action in ("edit", "optimize"):
            with self.subTest(action=action):
                self.assert_refused(action, "   \n", "empty")

    def test_an_explanation_instead_of_code_writes_nothing(self):
        for action in ("edit", "optimize"):
            with self.subTest(action=action):
                self.assert_refused(action, self.PROSE, "explanation")

    def test_an_explanation_wrapped_round_the_code_writes_nothing(self):
        for action in ("edit", "optimize"):
            with self.subTest(action=action):
                self.assert_refused(action, self.MIXED, "explanation")

    def test_a_partial_file_writes_nothing(self):
        part = ORIGINAL[: int(len(ORIGINAL) * 0.4)]
        for action in ("edit", "optimize"):
            with self.subTest(action=action):
                self.assert_refused(action, part, f"{len(part.strip()):,}",
                                    f"{len(ORIGINAL):,}")

    def test_the_thresholds_are_named(self):
        self.assertEqual(self.ch.MIN_KEEP_RATIO, 0.5)
        self.assertEqual(self.ch.PROSE_LINE_SHARE, 0.5)

    def test_a_good_reply_is_written(self):
        path = self.project_file()
        reply = "```python\n" + ORIGINAL + "# edited\n```"
        self.replies.append(reply)
        self.run_action(action="edit", file_path="project/app.py",
                        description="add a comment at the end")
        self.assertEqual(path.read_text(), ORIGINAL + "# edited")

    def test_editing_prose_is_still_allowed(self):
        """The prose rule only applies when the file itself is code."""
        path = self.project_file(self.PROSE, name="NOTES.md")
        reply = self.PROSE + "Remember to update the changelog.\n"
        self.replies.append(reply)
        self.run_action(action="edit", file_path="project/NOTES.md",
                        description="add a reminder")
        self.assertEqual(path.read_text(), reply.strip())



class UndoAndConfirmationTests(_Sandbox):
    """4. No write had a backup or an undo, and the confirmation said only
    "Edit 'app.py'". edit, optimize and write now register an undo when they
    replace an existing file, and the confirmation says what will be
    replaced. build is left as it was."""

    # CRLF line endings and a non-ASCII byte: a text round trip would not
    # give these back exactly.
    CRLF = ORIGINAL.replace("\n", "\r\n") + "# caf\u00e9\r\n"

    def setUp(self):
        super().setUp()
        from core import undo
        self.undo = undo
        undo.clear()
        self.addCleanup(undo.clear)

    def desktop_file(self, content=CRLF):
        path = self.home / "Desktop" / "jarvis_code.py"
        path.write_bytes(content.encode("utf-8"))
        return path

    def replace(self, action):
        """Run `action` over an existing file; return (path, its bytes
        before, the reply)."""
        if action == "write":
            path = self.desktop_file()
            self.replies.append(ORIGINAL + "# written\n")
            params = {"action": "write", "description": "functions"}
        else:
            path = self.project_file(self.CRLF)
            self.replies.append(ORIGINAL + "# changed\n")
            params = {"action": action, "file_path": "project/app.py"}
            if action == "edit":
                params["description"] = "add a comment at the end"
        before = path.read_bytes()
        answer = self.run_action(**params)
        self.assertNotEqual(path.read_bytes(), before,
                            f"{action} did not write: {answer}")
        return path, before, answer

    def test_undo_restores_the_original_bytes(self):
        for action in ("edit", "optimize", "write"):
            with self.subTest(action=action):
                path, before, _answer = self.replace(action)
                self.assertTrue(self.undo.can_undo(),
                                f"{action} registered no undo")
                result = self.undo.undo_last()
                self.assertEqual(path.read_bytes(), before, result)

    def test_the_reply_says_it_can_be_undone(self):
        for action in ("edit", "optimize", "write"):
            with self.subTest(action=action):
                _path, before, answer = self.replace(action)
                self.assertIn(f"{len(before):,} bytes", answer)
                self.assertIn("undo", answer)
                self.undo.clear()

    def test_undo_leaves_a_file_changed_since_alone(self):
        path, _before, _answer = self.replace("edit")
        path.write_bytes(b"the user's own later work\n")
        result = self.undo.undo_last()
        self.assertEqual(path.read_bytes(), b"the user's own later work\n")
        self.assertIn("changed since", result)

    def test_a_file_too_large_to_keep_says_it_cannot_be_undone(self):
        with mock.patch.object(self.ch, "UNDO_MAX_BYTES", 100):
            _path, _before, answer = self.replace("edit")
        self.assertFalse(self.undo.can_undo())
        self.assertIn("cannot be undone", answer)

    def test_build_registers_no_undo(self):
        """build rewrites its file on every attempt; its behaviour and its
        CODE_EXEC confirmation are unchanged."""
        self.desktop_file()
        self.replies.append("print('built')")
        with mock.patch.object(self.ch, "_run_file",
                               return_value="Output:\nbuilt"):
            self.run_action(action="build", description="print built")
        self.assertFalse(self.undo.can_undo())
        self.assertEqual(self.ch._ch_capability({"action": "build"}),
                         self.ch.capabilities.CODE_EXEC)

    # The confirmation

    def guard(self, **params):
        return self.ch._ch_guard(params)

    def test_the_confirmation_says_how_much_it_replaces(self):
        size = len(self.project_file().read_bytes())
        for action in ("edit", "optimize"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.guard(action=action,
                               file_path="project/app.py")["summary"],
                    f"{action.title()} 'app.py' (replaces {size:,} bytes)")

    def test_write_names_the_file_it_will_replace(self):
        self.assertEqual(self.guard(action="write")["summary"],
                         "Write 'jarvis_code.py' (new file)")
        size = len(self.desktop_file().read_bytes())
        self.assertEqual(self.guard(action="write")["summary"],
                         f"Write 'jarvis_code.py' (replaces {size:,} bytes)")
        self.assertEqual(
            self.guard(action="optimize", code="x = 1")["summary"],
            f"Optimize 'jarvis_code.py' (replaces {size:,} bytes)")

    def test_the_confirmation_warns_when_it_cannot_be_undone(self):
        size = len(self.project_file().read_bytes())
        with mock.patch.object(self.ch, "UNDO_MAX_BYTES", 100):
            summary = self.guard(action="edit",
                                 file_path="project/app.py")["summary"]
        self.assertEqual(summary, f"Edit 'app.py' (replaces {size:,} bytes, "
                                  f"too large to undo)")

    def test_the_guard_never_raises(self):
        for path in ("../outside/evil.py", str(self.outside / "evil.py"), ""):
            with self.subTest(path=path):
                self.assertIn("summary",
                              self.guard(action="edit", file_path=path))
                self.assertIn("summary",
                              self.guard(action="write", output_path=path))

    def test_it_still_asks_first(self):
        """The undo is pushed after the write; the guard offers none, so
        FILE_WRITE stays a confirmation rather than becoming reversible."""
        guard = self.guard(action="edit", file_path="project/app.py")
        self.assertNotIn("undo", guard)
        self.assertNotIn("undo_provider", guard)



class OptimizeOutputPathTests(_Sandbox):
    """optimize with inline code works out where to save from output_path,
    and a path outside the home folder raised PathEscape out of the action,
    after the model had been asked. It is now refused with the writer's
    plain message, before the model is asked, and nothing is written."""

    def test_an_output_path_outside_home_is_refused_not_raised(self):
        for attempt in (str(self.outside / "owned.py"),
                        "../../outside/owned.py"):
            with self.subTest(output_path=attempt):
                self.replies.append("x = 1\n")
                answer = self.run_action(action="optimize", code="x = 1",
                                         output_path=attempt)
                self.assertIn("inside your home folder", answer)
                self.assertIn(attempt, answer)
                self.assertFalse((self.outside / "owned.py").exists())
                self.assertEqual(self.prompts, [], "the model was asked")

    def test_an_output_path_inside_home_still_works(self):
        self.replies.append("x = 1\ny = 2\n")
        self.run_action(action="optimize", code="x = 1\ny = 2",
                        output_path="optimized.py")
        self.assertEqual(
            (self.home / "Desktop" / "optimized.py").read_text(),
            "x = 1\ny = 2")


if __name__ == "__main__":
    unittest.main(verbosity=2)
