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

# The Java harness the bridge tests compile against the mod's source.
# Temporary fallback: tests/java until the move commit puts it in
# tests/support/java; the fallback goes right after.
JAVA_HARNESS_DIR = (SUPPORT_DIR / "java" if (SUPPORT_DIR / "java").is_dir()
                    else TESTS_DIR / "java")

__all__ = ["REPO_ROOT", "TESTS_DIR", "SUPPORT_DIR", "JAVA_HARNESS_DIR"]
