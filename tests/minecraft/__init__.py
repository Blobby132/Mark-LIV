"""Tests for the Minecraft agent: minecraft/ and actions/minecraft.py."""

# This folder shares its name with the app's `minecraft` package. Discovery
# must start from the repository root -- pytest, or
# `python -m unittest discover -s tests -t .` -- which imports it as
# `tests.minecraft`. Started with `-s tests` alone, unittest would import it as
# `minecraft`, shadowing the app: say so rather than fail somewhere confusing.
if __name__ == "minecraft":  # pragma: no cover
    raise ImportError(
        "tests/minecraft was imported as 'minecraft', the app's own "
        "package name. "
        "Run from the repository root: "
        "python -m unittest discover -s tests -t .")
