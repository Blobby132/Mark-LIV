"""
Does this codebase run on the interpreter running these tests?

WHY THIS EXISTS
    "Can it run on Python 3.14?" took an afternoon to answer properly: install
    the interpreter, compile the tree, resolve the dependencies, import
    everything, find out that the one failure was an rc artifact rather than a
    real incompatibility. That is a reasonable afternoon to spend once. It is
    not a reasonable afternoon to spend again for 3.15, and it is not something
    anyone will remember to do before a release.

    So the parts of that investigation that CAN be automated are here. Run the
    suite under any interpreter and these tests answer the question for that
    interpreter — including the ones that do not exist yet.

WHAT IT CANNOT CHECK
    Whether third-party wheels exist for a given Python. That needs a network
    and a resolver, so it stays a manual step; `readme.md` records what was
    verified and when. These tests cover the half that is about our own code:
    syntax, removed standard-library modules, and the runtime patterns that
    have historically broken across versions.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

# Directories that are ours. Deliberately not a blanket rglob: a virtualenv or
# a vendored dependency under the repo would make this test about someone
# else's code.
OUR_CODE = ("core", "actions", "minecraft", "memory", "config", "dashboard",
            "plugins", "tools", "tests")
OUR_FILES = ("main.py", "ui.py", "setup.py")

# Modules GONE from the standard library, and when. Importing one of these is
# not a deprecation warning — it is an ImportError on that version and every
# version after it.
#
# `distutils` is the awkward one: removed from the stdlib in 3.12, but
# setuptools ships a shim, so it still imports on a machine that has
# setuptools. It belongs here anyway — code that relies on it is relying on a
# third-party package it never declared.
REMOVED_STDLIB = {
    # 3.12
    "distutils": "3.12", "imp": "3.12", "asynchat": "3.12",
    "asyncore": "3.12", "smtpd": "3.12",
    # 3.13 — PEP 594, "dead batteries"
    "aifc": "3.13", "audioop": "3.13", "cgi": "3.13", "cgitb": "3.13",
    "chunk": "3.13", "crypt": "3.13", "imghdr": "3.13", "mailcap": "3.13",
    "msilib": "3.13", "nis": "3.13", "nntplib": "3.13", "ossaudiodev": "3.13",
    "pipes": "3.13", "sndhdr": "3.13", "spwd": "3.13", "sunau": "3.13",
    "telnetlib": "3.13", "uu": "3.13", "xdrlib": "3.13", "lib2to3": "3.13",
    # 3.14
    "typing.io": "3.14", "typing.re": "3.14",
}

# Still present, still importable, and on the way out. Worth failing on now:
# the cost of not using them is nil, and the cost of finding out the release
# after they go is a broken install.
#
# Checked empirically rather than from memory — on 3.14 these emit a
# DeprecationWarning and still import, which is exactly why they are a separate
# list from the one above.
DEPRECATED_STDLIB = {
    "sre_compile": "use `re`", "sre_constants": "use `re`",
    "sre_parse": "use `re`",
}


def _python_files():
    seen = set()
    for name in OUR_CODE:
        directory = ROOT / name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            seen.add(path)
    for name in OUR_FILES:
        path = ROOT / name
        if path.is_file():
            seen.add(path)
    return sorted(seen)


def _parse(path: Path):
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                     filename=str(path))


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno


class TestParsesOnThisInterpreter(unittest.TestCase):

    def test_there_are_files_to_check(self):
        # A guard on the guard: a path change that made _python_files() empty
        # would turn every test below into a silent pass.
        self.assertGreater(len(_python_files()), 30)

    def test_every_file_parses(self):
        """New syntax is not the risk — removed syntax is. A construct that was
        legal in 3.11 and is an error in a later version shows up here."""
        failures = []
        for path in _python_files():
            try:
                _parse(path)
            except SyntaxError as e:
                failures.append(f"{path.relative_to(ROOT)}:{e.lineno}: {e.msg}")
        self.assertEqual(failures, [],
                         f"files that do not parse on Python "
                         f"{sys.version_info.major}.{sys.version_info.minor}:\n  "
                         + "\n  ".join(failures))

    def test_every_file_compiles_without_syntax_warnings(self):
        """PEP 765 (Python 3.14) warns about `return`, `break` and `continue`
        inside a `finally` block, because they silently swallow an in-flight
        exception. A warning today is an error eventually, and the behaviour it
        warns about is a real bug in a cleanup path."""
        import warnings
        failures = []
        for path in _python_files():
            source = path.read_text(encoding="utf-8", errors="replace")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(source, str(path), "exec")
                except SyntaxError as e:
                    failures.append(f"{path.relative_to(ROOT)}:{e.lineno}: {e.msg}")
                    continue
            for warning in caught:
                if issubclass(warning.category, SyntaxWarning):
                    failures.append(
                        f"{path.relative_to(ROOT)}:{getattr(warning, 'lineno', '?')}: "
                        f"{warning.message}")
        self.assertEqual(failures, [], "syntax warnings:\n  " + "\n  ".join(failures))


class TestNoRemovedStandardLibrary(unittest.TestCase):

    def test_no_removed_module_is_imported(self):
        offenders = []
        for path in _python_files():
            try:
                tree = _parse(path)
            except SyntaxError:
                continue          # reported by the test above
            for name, lineno in _imports(tree):
                root = name.split(".")[0]
                when = REMOVED_STDLIB.get(name) or REMOVED_STDLIB.get(root)
                if when:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno} imports {name} "
                        f"(removed in Python {when})")
        self.assertEqual(offenders, [],
                         "standard-library modules that no longer exist:\n  "
                         + "\n  ".join(offenders))

    def test_no_deprecated_module_is_imported(self):
        offenders = []
        for path in _python_files():
            try:
                tree = _parse(path)
            except SyntaxError:
                continue
            for name, lineno in _imports(tree):
                advice = DEPRECATED_STDLIB.get(name.split(".")[0])
                if advice:
                    offenders.append(
                        f"{path.relative_to(ROOT)}:{lineno} imports {name} "
                        f"(deprecated — {advice})")
        self.assertEqual(offenders, [],
                         "deprecated standard-library modules:\n  "
                         + "\n  ".join(offenders))

    def test_the_removed_list_is_accurate_for_this_interpreter(self):
        """Keeps the list above honest.

        Anything it claims was removed at or below the running version must
        genuinely be gone from the STANDARD LIBRARY here. Written empirically
        because memory is unreliable about this: an earlier draft of this file
        listed the `sre_*` modules as removed in 3.13, and they are merely
        deprecated — this test is what caught it.

        A module found outside the stdlib directory does not count as present:
        `distutils` resolves to setuptools' shim, which is a third-party
        package wearing a stdlib name."""
        import importlib.util
        import sysconfig

        stdlib_dir = sysconfig.get_paths().get("stdlib", "")
        current = sys.version_info[:2]
        wrong = []

        for module, when in REMOVED_STDLIB.items():
            if "." in module:
                continue                      # typing.io and friends
            major, minor = (int(part) for part in when.split("."))
            if current < (major, minor):
                continue                      # not removed yet on this version
            try:
                spec = importlib.util.find_spec(module)
            except (ImportError, ValueError, AttributeError):
                continue                      # genuinely gone
            if spec is None:
                continue
            origin = str(getattr(spec, "origin", "") or "")
            if stdlib_dir and origin.startswith(stdlib_dir):
                wrong.append(f"{module} is still in the stdlib on "
                             f"{current[0]}.{current[1]} ({origin})")
        self.assertEqual(wrong, [], f"stale entries: {wrong}")

    def test_the_deprecated_list_is_accurate_for_this_interpreter(self):
        """The mirror image: anything listed as merely deprecated must still
        be importable here. Once one of these actually goes, this fails and
        tells whoever is reading to move it into REMOVED_STDLIB."""
        import importlib.util
        gone = []
        for module in DEPRECATED_STDLIB:
            try:
                if importlib.util.find_spec(module) is None:
                    gone.append(module)
            except (ImportError, ValueError, AttributeError):
                gone.append(module)
        self.assertEqual(
            gone, [],
            f"these are listed as deprecated but are already removed on "
            f"Python {sys.version_info.major}.{sys.version_info.minor} — "
            f"move them to REMOVED_STDLIB: {gone}")


class TestAsyncioPatterns(unittest.TestCase):
    """`asyncio.get_event_loop()` stopped being forgiving.

    Through 3.11 it would create a loop if none existed. From 3.12 that was a
    DeprecationWarning, and on 3.14 it raises RuntimeError outside a running
    loop. Inside a coroutine it is still fine — but "inside a coroutine" is a
    property of where the call sits, which is exactly the kind of thing that
    stops being true when someone refactors."""

    FORGIVING = ("get_event_loop",)

    def test_get_event_loop_is_not_called_outside_a_coroutine(self):
        offenders = []
        for path in _python_files():
            try:
                tree = _parse(path)
            except SyntaxError:
                continue
            offenders.extend(self._scan(path, tree))
        self.assertEqual(
            offenders, [],
            "asyncio.get_event_loop() outside a running loop raises "
            "RuntimeError on Python 3.14. Use asyncio.get_running_loop() "
            "inside a coroutine, or asyncio.run()/new_event_loop() "
            "outside one:\n  " + "\n  ".join(offenders))

    def _scan(self, path, tree):
        found = []

        class Walker(ast.NodeVisitor):
            def __init__(self):
                self.async_depth = 0

            def visit_AsyncFunctionDef(self, node):
                self.async_depth += 1
                self.generic_visit(node)
                self.async_depth -= 1

            def visit_FunctionDef(self, node):
                # A plain def nested in an async def does NOT inherit the
                # running loop — it may be called from a thread.
                outer, self.async_depth = self.async_depth, 0
                self.generic_visit(node)
                self.async_depth = outer

            def visit_Call(self, node):
                func = node.func
                if (isinstance(func, ast.Attribute)
                        and func.attr in TestAsyncioPatterns.FORGIVING
                        and self.async_depth == 0):
                    found.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} "
                        f"{func.attr}()")
                self.generic_visit(node)

        Walker().visit(tree)
        return found


class TestVersionDeclarationsAgree(unittest.TestCase):
    """setup.py turns people away at the door, so what it says has to be true."""

    def test_setup_declares_a_sane_range(self):
        source = (ROOT / "setup.py").read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        found = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in ("MIN_PY", "MAX_PY"):
                    found[target.id] = ast.literal_eval(node.value)
        self.assertIn("MIN_PY", found)
        self.assertIn("MAX_PY", found)
        self.assertLessEqual(found["MIN_PY"], found["MAX_PY"])

    def test_this_interpreter_is_within_the_declared_range(self):
        """If the suite passes on an interpreter setup.py warns about, one of
        the two is wrong — and it is usually setup.py, lagging behind."""
        source = (ROOT / "setup.py").read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        declared = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id in ("MIN_PY", "MAX_PY"):
                    declared[target.id] = ast.literal_eval(node.value)

        current = sys.version_info[:2]
        self.assertGreaterEqual(
            current, declared["MIN_PY"],
            f"the tests are running on {current}, below setup.py's floor")
        if current > declared["MAX_PY"]:
            self.skipTest(
                f"Running on Python {current[0]}.{current[1]}, which is above "
                f"setup.py's tested ceiling {declared['MAX_PY'][0]}."
                f"{declared['MAX_PY'][1]}. If this whole suite passes, raise "
                f"MAX_PY and update the readme.")


if __name__ == "__main__":
    unittest.main()
