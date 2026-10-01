"""
docs/ARCHITECTURE.md maps every module in minecraft/ and minecraft/skills/
and names the safety layers. A module added without a line there, or a
layer dropped from it, fails here rather than leaving the map quietly wrong.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC = ROOT / "docs" / "ARCHITECTURE.md"

LAYERS = ("Permission gate", "Session", "Focus guard", "Ledger", "Deadman",
          "Hazard probe", "Click gate", "Danger watch")


class ArchitectureDocTests(unittest.TestCase):

    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_every_minecraft_module_is_mapped(self):
        for path in sorted((ROOT / "minecraft").glob("*.py")):
            if path.name == "__init__.py":
                continue
            with self.subTest(module=path.name):
                self.assertIn(f"`{path.name}`", self.text)

    def test_every_skills_module_is_mapped(self):
        for path in sorted((ROOT / "minecraft" / "skills").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertIn(f"`{path.name}`", self.text)

    def test_every_registered_skill_is_named(self):
        from minecraft import skills
        for name in skills.available():
            with self.subTest(skill=name):
                self.assertIn(f"`{name}`", self.text)

    def test_the_safety_layers_are_there(self):
        for layer in LAYERS:
            with self.subTest(layer=layer):
                self.assertRegex(self.text, rf"### \d+\. {layer}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
