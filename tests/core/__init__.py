"""Tests for core/: permissions, capabilities, audit, safe paths, safe
exec, confirmation, enforcement, vulnerabilities, lint, repo hygiene."""

# This folder shares its name with the app's `core` package. Discovery
# must start from the repository root -- pytest, or
# `python -m unittest discover -s tests -t .` -- which imports it as
# `tests.core`. Started with `-s tests` alone, unittest would import it as
# `core`, shadowing the app: say so rather than fail somewhere confusing.
if __name__ == "core":  # pragma: no cover
    raise ImportError(
        "tests/core was imported as 'core', the app's own "
        "package name. "
        "Run from the repository root: "
        "python -m unittest discover -s tests -t .")
