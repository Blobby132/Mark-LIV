"""
tools/install_mod.py — getting the new mod actually loaded.

A real update was lost: the installer ran with Minecraft open, and the game
went on running the old mod. The only symptom was JARVIS reporting the old
version afterwards. So it now refuses while Minecraft runs, saying why, and
judges a copy by its bytes -- two builds of this mod can be the same size.
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


from tools import install_mod                                       # noqa: E402


def quietly(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = fn(*args)
    return result, out.getvalue()


class InstallIntoTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.jar = root / "markliv-bridge-1.0.0.jar"
        self.jar.write_bytes(b"NEW build of the mod")
        self.mods = root / "game" / "mods"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_fresh_install_copies_the_jar(self):
        ok, text = quietly(install_mod.install_into, self.mods, self.jar)
        self.assertTrue(ok)
        self.assertEqual((self.mods / self.jar.name).read_bytes(),
                         self.jar.read_bytes())
        self.assertIn("Installed", text)

    def test_an_old_build_of_the_same_size_is_replaced(self):
        self.mods.mkdir(parents=True)
        old = self.mods / self.jar.name
        old.write_bytes(b"OLD build of the mod")         # same length
        self.assertEqual(old.stat().st_size, self.jar.stat().st_size)
        ok, _ = quietly(install_mod.install_into, self.mods, self.jar)
        self.assertTrue(ok)
        self.assertEqual(old.read_bytes(), self.jar.read_bytes())

    def test_a_copy_that_did_not_take_is_a_failure(self):
        """The size check passed a copy that had not happened."""
        self.mods.mkdir(parents=True)
        old = self.mods / self.jar.name
        old.write_bytes(b"OLD build of the mod")
        with unittest.mock.patch.object(install_mod.shutil, "copy2",
                                        lambda *_a, **_k: None):
            ok, text = quietly(install_mod.install_into, self.mods, self.jar)
        self.assertFalse(ok)
        self.assertIn("FAILED", text)

    def test_the_current_jar_is_left_alone(self):
        quietly(install_mod.install_into, self.mods, self.jar)
        ok, text = quietly(install_mod.install_into, self.mods, self.jar)
        self.assertTrue(ok)
        self.assertIn("Already up to date", text)


class RunningGameTests(unittest.TestCase):

    def run_main(self, *argv, pid=4321):
        with unittest.mock.patch.object(install_mod, "running_minecraft",
                                        return_value=pid), \
                unittest.mock.patch.object(install_mod, "install_into",
                                           return_value=True) as install, \
                unittest.mock.patch.object(sys, "argv",
                                           ["install_mod.py", *argv]):
            code, text = quietly(install_mod.main)
        return code, text, install

    def test_it_refuses_while_minecraft_runs(self):
        code, text, install = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Minecraft is running", text)
        self.assertIn("Quit it completely", text)
        install.assert_not_called()

    def test_into_a_named_folder_is_refused_too(self):
        code, _, install = self.run_main("--into", "C:/Games/mc")
        self.assertEqual(code, 1)
        install.assert_not_called()

    def test_listing_changes_nothing_so_it_is_allowed(self):
        code, text, _ = self.run_main("--list")
        self.assertNotIn("Minecraft is running", text)

    def test_anyway_overrides(self):
        code, text, install = self.run_main("--into", "C:/Games/mc",
                                            "--anyway")
        self.assertNotIn("Minecraft is running", text)
        install.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
