"""
code_helper's screen_debug must never write a file.

screen_debug screenshots the screen and asks Gemini what error is on it.
Given a file_path it also sends the model that file -- only its first
4,000 characters -- and it used to take the first fenced code block of the
reply and write it over the file: plain write_text, no backup, no undo, no
confirmation, while the action is classed FILE_READ (allowed without
asking). A short snippet in the reply replaced a whole file.

Now it returns the model's analysis, suggested code included, and writes
nothing. Changing a file is `edit`: FILE_WRITE, with its confirmation.

google-genai is not installed where these tests run, and screen_debug
imports google.genai.types inside its try -- without it the function would
fail before reaching the old write and the test would pass for nothing. So
the module is stubbed, and the model is a fake that returns a snippet.
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from core import capabilities as caps

SNIPPET = "def broken():\n    return 'fixed'"
REPLY = ("The error is a NameError on line 3.\n\n"
         f"```python\n{SNIPPET}\n```\n\nThat should fix it.")


def _fake_genai_modules():
    """google, google.genai and google.genai.types, with Part.from_bytes."""
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


class ScreenDebugWritesNothingTests(unittest.TestCase):

    def setUp(self):
        try:
            from actions import code_helper
        except Exception as exc:                          # pragma: no cover
            self.skipTest(f"code_helper could not be imported here: {exc}")
        self.ch = code_helper

        # HOME is a temp directory: _read_file only reads inside it.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        (self.home / "Desktop").mkdir(parents=True)
        saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(self.home)
        self.addCleanup(self._restore_env, saved)

        self.screenshot = self.home / "Desktop" / "shot.png"
        self.calls = []

        def fake_call(contents, **kwargs):
            self.calls.append(contents)
            return types.SimpleNamespace(text=REPLY)

        for patcher in (
                mock.patch.dict(sys.modules, _fake_genai_modules()),
                mock.patch.object(self.ch, "_take_screenshot",
                                  side_effect=self._take_screenshot),
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

    def _take_screenshot(self):
        self.screenshot.write_bytes(b"\x89PNG\r\n\x1a\nnot a real image")
        return self.screenshot

    def _file(self, size):
        """A Python file of about `size` characters, ending in a marker."""
        path = self.home / "project" / "app.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"value_{i} = {i}  # line {i}\n"
                       for i in range(size // 24 + 1))
        content = (body + "# THE END OF THE FILE\n").encode("utf-8")
        path.write_bytes(content)
        return path, content

    def _run(self, path):
        return self.ch._screen_debug_action(
            "why does this crash?", str(path), player=None)

    def test_a_short_file_is_left_byte_identical(self):
        path, before = self._file(300)
        self.assertLess(len(before), 4000)
        self._run(path)
        self.assertEqual(path.read_bytes(), before)

    def test_a_file_longer_than_the_model_saw_is_left_byte_identical(self):
        """The reproduction: the model sees 4,000 characters of a longer
        file and answers with a two-line snippet. The whole file used to
        become those two lines."""
        path, before = self._file(9000)
        self.assertGreater(len(before), 4000)
        self._run(path)
        self.assertEqual(path.read_bytes(), before,
                         "screen_debug rewrote the file")

    def test_it_returns_the_analysis_and_the_suggested_code_only(self):
        path, _before = self._file(300)
        answer = self._run(path)
        self.assertEqual(answer, REPLY)
        self.assertIn(SNIPPET, answer)
        self.assertNotIn("saved", answer.lower())

    def test_the_model_was_actually_asked(self):
        """Guards the tests above: had the call never been made, a file
        left alone would prove nothing."""
        path, _before = self._file(300)
        self._run(path)
        self.assertEqual(len(self.calls), 1)
        prompt = self.calls[0][1]
        self.assertIn("THE END OF THE FILE", prompt)

    def test_nothing_new_appears_beside_the_file(self):
        path, _before = self._file(300)
        self._run(path)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                         ["app.py"])

    def test_it_stays_a_read(self):
        self.assertEqual(self.ch._ch_capability({"action": "screen_debug"}),
                         caps.FILE_READ)


if __name__ == "__main__":
    unittest.main(verbosity=2)
