"""Tests for the dashboard (dashboard/)."""

# This folder shares its name with the app's `dashboard` package. Discovery
# must start from the repository root -- pytest, or
# `python -m unittest discover -s tests -t .` -- which imports it as
# `tests.dashboard`. Started with `-s tests` alone, unittest would import it as
# `dashboard`, shadowing the app: say so rather than fail somewhere confusing.
if __name__ == "dashboard":  # pragma: no cover
    raise ImportError(
        "tests/dashboard was imported as 'dashboard', the app's own "
        "package name. "
        "Run from the repository root: "
        "python -m unittest discover -s tests -t .")
