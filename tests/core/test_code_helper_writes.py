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


if __name__ == "__main__":
    unittest.main(verbosity=2)
