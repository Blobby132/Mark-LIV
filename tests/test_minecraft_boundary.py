"""
Boundary tests — the Minecraft package must not become a way to control Windows.

"The Minecraft controller should know how to control Minecraft. It should NOT
know how to control Windows."

That is a property of the import graph, so it is tested as one. Every module in
`minecraft/` is parsed and every import, attribute access and call is checked
against an allowlist. A future edit that reaches for `subprocess` to shell out
to xdotool, or imports `actions.file_controller` for "just one thing", fails
here rather than in review.

Parsed rather than grepped: several of these modules legitimately mention
`subprocess` and `shell` in comments explaining what they deliberately do not
do, and a text search cannot tell an explanation from a call.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PACKAGE = Path("minecraft")
ADAPTER = Path("actions/minecraft.py")

# Modules this package may import. Anything else is a boundary violation.
#
#   stdlib          — the ordinary ones, plus ctypes for the Windows input API
#   core.*          — permissions, auditing, the capability table. The narrow
#                     interfaces the brief allows.
#   minecraft.*     — itself
ALLOWED_STDLIB = frozenset({
    "__future__", "atexit", "ctypes", "dataclasses", "io", "os", "platform",
    "threading", "time", "typing", "uuid", "json", "math", "enum", "re",
    # A priority queue, for the pathfinder. Pure computation, no reach.
    "heapq",
    # RGB to HSV, for the visual classifier. Pure arithmetic.
    "colorsys",
    # MappingProxyType, to freeze a WorldState's per-field provenance so a
    # snapshot cannot be edited after the fact.
    "types",
})

ALLOWED_CORE = frozenset({
    "core", "core.capabilities", "core.permissions", "core.audit",
    "core.confirm", "core.safe_path",
})

# Third-party that is fine: screen capture and image handling, both read-only
# and both already dependencies of the app.
ALLOWED_THIRD_PARTY = frozenset({"mss", "mss.tools", "PIL", "PIL.Image",
                                 "psutil", "pygetwindow"})

# Named explicitly so the failure message can say WHY, not just "not allowed".
FORBIDDEN = {
    "subprocess": "starting processes",
    "shutil": "moving and deleting files",
    "socket": "network access",
    "http": "network access",
    "urllib": "network access",
    "requests": "network access",
    "ftplib": "network access",
    "smtplib": "sending mail",
    "pickle": "arbitrary deserialisation",
    "pty": "terminal control",
    "webbrowser": "opening a browser",
    "pyautogui": "unrestricted global input",
    "pynput": "unrestricted global input",
    "keyboard": "unrestricted global input",
    "pyperclip": "the clipboard",
}


def _module_files():
    files = sorted(PACKAGE.glob("*.py"))
    assert files, "no modules found in minecraft/ — is the path right?"
    return files + [ADAPTER]


def _imports(tree) -> list:
    """(module_name, lineno) for every import in the tree, including the ones
    inside functions — a lazy import is still an import."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:                     # relative import
                out.append((f".{node.module or ''}", node.lineno))
            elif node.module:
                out.append((node.module, node.lineno))
    return out


def _root(name: str) -> str:
    return name.split(".", 1)[0]


class TestImportBoundary(unittest.TestCase):

    def test_no_forbidden_module_is_imported(self):
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                reason = FORBIDDEN.get(_root(name))
                if reason:
                    offenders.append(f"{path}:{lineno} imports {name} ({reason})")
        self.assertEqual(offenders, [],
                         "the Minecraft package reached outside its "
                         f"boundary:\n  " + "\n  ".join(offenders))

    def test_every_import_is_on_the_allowlist(self):
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                if name.startswith("minecraft") or name.startswith("."):
                    continue
                if name in ALLOWED_CORE or _root(name) == "minecraft":
                    continue
                if _root(name) in ALLOWED_STDLIB:
                    continue
                if name in ALLOWED_THIRD_PARTY or _root(name) in ALLOWED_THIRD_PARTY:
                    continue
                offenders.append(f"{path}:{lineno} imports {name}")
        self.assertEqual(
            offenders, [],
            "unreviewed imports in the Minecraft package. If one of these is "
            "genuinely needed, add it to the allowlist in this test and say "
            "why:\n  " + "\n  ".join(offenders))

    def test_no_other_action_module_is_imported(self):
        """`actions/minecraft.py` is the adapter and may be imported by the
        loader, but nothing in this package may reach sideways into another
        action — that is how a subsystem quietly acquires file deletion."""
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                if _root(name) != "actions":
                    continue
                if name in ("actions", "actions.minecraft"):
                    continue
                offenders.append(f"{path}:{lineno} imports {name}")
        self.assertEqual(offenders, [], f"sideways action imports: {offenders}")

    def test_core_imports_are_limited_to_the_narrow_interfaces(self):
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                if _root(name) != "core":
                    continue
                if name not in ALLOWED_CORE:
                    offenders.append(f"{path}:{lineno} imports {name}")
        self.assertEqual(
            offenders, [],
            "the Minecraft package may only use core's permission, audit and "
            f"capability interfaces:\n  " + "\n  ".join(offenders))

    def test_no_ocr_library_is_imported(self):
        """OCR is the one Phase 3 dependency that would have smuggled a
        subprocess back in.

        pytesseract runs the Tesseract binary via subprocess. Importing it
        anywhere under `minecraft/` would give this package a process spawn
        again, and the honest way to permit that would be to weaken the test
        above — so instead the reader lives in `core/ocr.py` and is injected.
        `minecraft/debug_overlay.py` declares the interface and imports no
        engine at all.

        This is a separate test from the allowlist so that adding an OCR
        library here requires deleting a test that says why not, rather than
        appending one word to a set."""
        engines = ("pytesseract", "tesseract", "easyocr", "paddleocr",
                   "cv2", "numpy")
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                if _root(name) in engines:
                    offenders.append(f"{path}:{lineno} imports {name}")
        self.assertEqual(
            offenders, [],
            "the Minecraft package imported an OCR/vision engine directly. "
            "Inject a reader from core/ocr.py instead:\n  "
            + "\n  ".join(offenders))

    def test_exec_safe_is_not_imported(self):
        """Even the safe subprocess wrapper is out of bounds here. It is the
        right tool elsewhere; in this package it would be a process-spawning
        capability that nothing in the design needs."""
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                if "exec_safe" in name:
                    offenders.append(f"{path}:{lineno}")
        self.assertEqual(offenders, [], f"exec_safe imported: {offenders}")


class TestNoExecutionPrimitives(unittest.TestCase):

    def test_no_call_to_exec_eval_or_compile(self):
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id in ("exec", "eval", "compile",
                                             "__import__")):
                    offenders.append(f"{path}:{node.lineno} {node.func.id}()")
        self.assertEqual(offenders, [], f"execution primitives: {offenders}")

    def test_no_os_system_or_popen(self):
        """Checks the receiver, not just the method name.

        `platform.system()` returns the name of the operating system and is
        used all over this package; `os.system()` runs a shell command. Only
        the second one is a boundary violation, so the test looks at what the
        attribute is being read from."""
        dangerous = {
            "os": ("system", "popen", "execv", "execve", "execvp", "spawnl",
                   "spawnv", "fork", "forkpty", "posix_spawn"),
            "subprocess": ("run", "Popen", "call", "check_output", "check_call"),
        }
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                receiver = node.func.value
                if not isinstance(receiver, ast.Name):
                    continue
                if node.func.attr in dangerous.get(receiver.id, ()):
                    offenders.append(
                        f"{path}:{node.lineno} {receiver.id}.{node.func.attr}()")
        self.assertEqual(offenders, [], f"process primitives: {offenders}")

    READ_MODES = frozenset({"r", "rb", "rt", "br", "tr"})

    def test_no_file_writes(self):
        """Reading is fine; writing is not something this package has any
        reason to do, and a write is how a subsystem starts being able to
        change the app around it.

        This used to forbid `open()` outright, which was the easy rule rather
        than the right one: it also banned reads, and the mod bridge's whole
        job is reading a file the game writes. So the mode is checked instead.
        A literal read mode passes; a write mode, a computed mode, or anything
        this cannot prove is a read, fails.

        Refusing what it cannot verify is the point. A mode built at runtime
        might be "w", and a check that assumed otherwise would be worse than
        no check at all."""
        offenders = []
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in (
                        "write_text", "write_bytes", "unlink", "rmdir",
                        "mkdir", "rename", "replace", "chmod", "makedirs",
                        "remove", "rmtree"):
                    offenders.append(f"{path}:{node.lineno} .{func.attr}()")
                if isinstance(func, ast.Name) and func.id == "open":
                    offenders.append(self._judge_open(path, node))
        offenders = [o for o in offenders if o]
        self.assertEqual(offenders, [], f"filesystem writes: {offenders}")

    def _judge_open(self, path, node):
        """'' when this open() is provably a read, else why it is not."""
        mode = None
        if len(node.args) >= 2:
            mode = node.args[1]
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value

        if mode is None:
            return ""                       # defaults to "r"
        if isinstance(mode, ast.Constant) and mode.value in self.READ_MODES:
            return ""
        shown = getattr(mode, "value", "<computed>")
        return (f"{path}:{node.lineno} open(mode={shown!r}) — only a literal "
                f"read mode is allowed here")


class TestTheNamedIsolationRequirements(unittest.TestCase):
    """The isolation list, asserted one item at a time.

    Most of these are already implied by the allowlist above. They are spelled
    out separately because an allowlist failure reads as "unreviewed import"
    and these read as "Minecraft can now send messages" — and the second is
    the sentence that should appear in a diff."""

    def _all_imports(self):
        for path in _module_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, lineno in _imports(tree):
                yield path, name, lineno

    def test_no_shell_execution(self):
        offenders = [f"{p}:{n}" for p, name, n in self._all_imports()
                     if _root(name) in ("subprocess", "pty", "os")
                     and name in ("subprocess", "pty")]
        self.assertEqual(offenders, [])

    def test_no_browser_control(self):
        offenders = [f"{p}:{n} ({name})" for p, name, n in self._all_imports()
                     if _root(name) in ("webbrowser", "playwright", "selenium")
                     or name == "actions.browser_control"]
        self.assertEqual(offenders, [],
                         f"Minecraft can reach browser control: {offenders}")

    def test_no_messaging(self):
        offenders = [f"{p}:{n} ({name})" for p, name, n in self._all_imports()
                     if _root(name) in ("smtplib", "email", "twilio")
                     or name in ("actions.send_message", "actions.messaging")]
        self.assertEqual(offenders, [],
                         f"Minecraft can reach messaging: {offenders}")

    def test_no_arbitrary_filesystem_access(self):
        """`os` is allowed for `os.name` and path joins; `shutil`, `glob` and
        `pathlib` writes are not. The write check above covers the calls; this
        covers the imports that only exist to do bulk file work."""
        offenders = [f"{p}:{n} ({name})" for p, name, n in self._all_imports()
                     if _root(name) in ("shutil", "glob", "tempfile",
                                        "fileinput")]
        self.assertEqual(offenders, [])

    def test_minecraft_command_is_denied_in_the_central_table(self):
        from core import capabilities as core_caps
        from minecraft import capabilities as mc_phase
        self.assertEqual(core_caps.decision_for("minecraft.command"),
                         core_caps.DENY)
        self.assertNotIn(core_caps.MINECRAFT_COMMAND, mc_phase.ENABLED)

    def test_an_unknown_minecraft_action_resolves_to_the_denied_capability(self):
        """The fail-closed default. A sub-action nobody wrote must land on the
        one capability that can never be granted, not on a permissive one."""
        from actions.minecraft import _mc_capability
        for invented in ("give_me_diamonds", "op", "", "  ", "execute",
                         "run_command", None):
            with self.subTest(action=invented):
                self.assertEqual(_mc_capability({"action": invented}),
                                 "minecraft.command")


class TestTheOnlyWayInIsTheBroker(unittest.TestCase):

    def test_the_adapter_declares_a_capability_resolver(self):
        from actions import minecraft as adapter
        self.assertIn("capability", adapter.TOOL)
        self.assertTrue(callable(adapter.TOOL["capability"]))

    def test_the_adapter_loads_through_the_normal_discovery(self):
        """It must be a real discoverable action, not a special case wired into
        main.py — so it goes through the same enforcement as everything else."""
        from core.action_loader import discover_actions
        logs: list = []
        registry = discover_actions(Path("actions"), reserved_names=set(),
                                    logger=logs.append)
        self.assertTrue(registry.has("minecraft_control"),
                        f"minecraft_control did not load: "
                        f"{[l for l in logs if 'minecraft' in l]}")

    def test_the_planner_cannot_name_an_input_primitive(self):
        """The task runner dispatches only through a fixed table. A skill that
        returns an action outside it is refused, so the set of things a plan
        can contain is decided in source rather than by whatever produced the
        plan."""
        from minecraft import task_runner
        for forbidden in ("press", "type", "key_down", "key_up", "hotkey",
                          "send_keys", "button_down", "move_mouse_relative",
                          "chat", "command", "run", "exec", "eval"):
            with self.subTest(action=forbidden):
                self.assertNotIn(forbidden, task_runner.DISPATCH)

    def test_every_dispatchable_action_has_a_capability(self):
        """A task must not be able to reach an action the broker never rated.
        Every entry in the runner's table has to appear in the adapter's
        capability map, or a skill could take an unbrokered action."""
        from actions.minecraft import _CAPABILITY_BY_ACTION
        from minecraft import task_runner
        missing = [a for a in task_runner.DISPATCH
                   if a not in _CAPABILITY_BY_ACTION]
        self.assertEqual(missing, [],
                         f"dispatchable but unbrokered actions: {missing}")

    def test_the_package_exposes_no_general_input_function(self):
        """A `press(key)` or `type(text)` at package level would be the
        backdoor, however carefully the controller was written."""
        import minecraft
        for forbidden in ("press", "type", "typewrite", "hotkey", "click",
                          "key_down", "key_up", "send_keys", "run"):
            self.assertFalse(hasattr(minecraft, forbidden),
                             f"minecraft.{forbidden} is exposed at package level")


if __name__ == "__main__":
    unittest.main()
