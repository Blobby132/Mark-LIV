"""
Where things are, for tests. Found from this file's own location, so a test
gives the same answer wherever it sits under tests/ -- and nothing depends
on the directory the tests were started from.
"""

from __future__ import annotations

from pathlib import Path

SUPPORT_DIR = Path(__file__).resolve().parent
TESTS_DIR = SUPPORT_DIR.parent
REPO_ROOT = TESTS_DIR.parent

__all__ = ["REPO_ROOT", "TESTS_DIR", "SUPPORT_DIR"]
