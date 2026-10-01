"""
Lint: pyflakes over every Python file in the repository.

pyflakes is a development tool, not a dependency of the app, so this skips
when it is not installed (`pip install pyflakes` to run it). The same rules
are configured for ruff in pyproject.toml.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "venv", "env", "ENV", "build", "dist",
             "fabric-mod", "__pycache__", ".pytest_cache", ".ruff_cache"}

# Known before the reorganisation, fixed in its own commit: remove the
# entry with the fix, and never add one.
KNOWN_UNDEFINED = {"tests/test_minecraft_observation.py: 'Observation'"}

try:
    from pyflakes import api as pyflakes_api
    from pyflakes import reporter as pyflakes_reporter
except ImportError:                                   # pragma: no cover
    pyflakes_api = None


def python_files():
    for path in sorted(ROOT.rglob("*.py")):
        parts = set(path.relative_to(ROOT).parts[:-1])
        if not parts & SKIP_DIRS:
            yield path


def findings():
    """Every pyflakes message, as 'relative/path:line:col: message'."""
    out = io.StringIO()
    report = pyflakes_reporter.Reporter(out, out)
    for path in python_files():
        pyflakes_api.checkPath(str(path), report)
    root = str(ROOT) + "/"
    return [line.replace(root, "", 1)
            for line in out.getvalue().splitlines() if line.strip()]


@unittest.skipIf(pyflakes_api is None, "pyflakes is not installed")
class LintTests(unittest.TestCase):

    def test_no_undefined_names(self):
        undefined = []
        for line in findings():
            if "undefined name" not in line:
                continue
            path = line.split(":", 1)[0]
            name = line.rsplit("undefined name ", 1)[1]
            if f"{path}: {name}" not in KNOWN_UNDEFINED:
                undefined.append(line)
        self.assertEqual(undefined, [])
