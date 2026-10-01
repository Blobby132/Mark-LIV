"""
Lint: pyflakes over every Python file in the repository.

pyflakes is a development tool, not a dependency of the app, so this skips
when it is not installed (`pip install pyflakes` to run it). The same rules
are configured for ruff in pyproject.toml.

pyflakes itself ignores `# noqa`; this test honours it, so a line kept on
purpose -- an import that is an availability check, say -- says so where
it stands, with the reason, and everything else must be clean.
"""

from __future__ import annotations

import io
import unittest

from tests.support.paths import REPO_ROOT as ROOT

SKIP_DIRS = {".git", ".venv", "venv", "env", "ENV", "build", "dist",
             "fabric-mod", "__pycache__", ".pytest_cache", ".ruff_cache"}


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
        undefined = [line for line in findings() if "undefined name" in line]
        self.assertEqual(undefined, [])

    def test_nothing_else_unless_marked_noqa(self):
        unmarked = []
        for line in findings():
            path, row = line.split(":", 2)[:2]
            source = (ROOT / path).read_text(encoding="utf-8").splitlines()
            if not row.isdigit() or "# noqa" not in source[int(row) - 1]:
                unmarked.append(line)
        self.assertEqual(unmarked, [])
