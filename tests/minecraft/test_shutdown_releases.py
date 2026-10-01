"""
Shutting JARVIS down must not leave a key held in Minecraft.

`shutdown_jarvis` ended in os._exit(0), which skips atexit -- and atexit is
where the Minecraft controller releases its ledger. A task walking forward
when the user said "shut down" kept walking after JARVIS was gone.

main.py cannot be imported here (audio device, Gemini SDK, Qt), so as in
test_voice_main_paths the method is lifted out of its source and bound to a
stand-in; the registry it calls is the real core.interrupts.
"""

from __future__ import annotations

import ast
import asyncio
import types
import unittest

from tests.support.paths import REPO_ROOT as ROOT

from core import interrupts                                         # noqa: E402

MAIN = ROOT / "main.py"


def _lift(name):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == name:
                    namespace = {"asyncio": _NoWaitAsyncio(),
                                 "_interrupts": interrupts}
                    module = ast.Module(body=[item], type_ignores=[])
                    exec(compile(module, str(MAIN), "exec"), namespace)
                    return namespace[name]
    raise AssertionError(f"main.py has no async method {name}")


class _NoWaitAsyncio:
    @staticmethod
    async def sleep(_seconds):
        return None


class Held:
    """A registered thing that says it is idle but is holding W -- the case
    cancel_active alone would skip."""

    def __init__(self, events):
        self.events = events
        self.holding = True

    def is_active(self):
        return False

    def cancel(self, reason):
        self.holding = False
        self.events.append(("released", reason))


class ShutdownTests(unittest.TestCase):

    def setUp(self):
        self.events = []
        self.held = Held(self.events)
        interrupts.register("test-held", self.held.is_active,
                            self.held.cancel)
        self.addCleanup(interrupts.unregister, "test-held")

    def run_shutdown(self, save_fails=False):
        shutdown = _lift("_shutdown")
        fake = types.SimpleNamespace(session=None)

        async def save():
            if save_fails:
                raise OSError("disk full")
            self.events.append(("saved",))
        fake._save_session_summary = save

        def exit_process(code):
            self.events.append(("exit", code, self.held.holding))

        asyncio.run(types.MethodType(shutdown, fake)(exit_process))

    def test_everything_is_released_before_the_process_exits(self):
        self.run_shutdown()
        kinds = [e[0] for e in self.events]
        self.assertIn("released", kinds)
        self.assertEqual(kinds[-1], "exit")
        self.assertEqual(self.events[-1], ("exit", 0, False),
                         "the process exited with a key still held")

    def test_a_failing_save_still_exits_and_releases(self):
        """A6: an exception in _save_session_summary (or cancel_active)
        ended _shutdown early -- the process never exited, and nothing
        released what was held."""
        self.run_shutdown(save_fails=True)
        self.assertEqual(self.events[-1], ("exit", 0, False))

    def test_a_failing_cancel_active_still_exits(self):
        original = interrupts.cancel_active

        def boom(_reason):
            raise RuntimeError("a cancel callback broke")
        interrupts.cancel_active = boom
        self.addCleanup(setattr, interrupts, "cancel_active", original)
        self.run_shutdown()
        self.assertEqual(self.events[-1], ("exit", 0, False))

    def test_the_shutdown_branch_uses_it(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn("asyncio.create_task(self._shutdown())", source)
        self.assertNotIn("._exit(", source,
                         "a direct os._exit call skips the release")


class CancelAllTests(unittest.TestCase):

    def test_cancel_all_reaches_what_says_it_is_idle(self):
        events = []
        held = Held(events)
        interrupts.register("test-idle", held.is_active, held.cancel)
        self.addCleanup(interrupts.unregister, "test-idle")
        self.assertNotIn("test-idle", interrupts.cancel_active("x"))
        self.assertIn("test-idle", interrupts.cancel_all("bye"))
        self.assertFalse(held.holding)

    def test_one_failing_cancel_does_not_stop_the_rest(self):
        def boom(_reason):
            raise RuntimeError("broken")
        events = []
        held = Held(events)
        interrupts.register("test-boom", lambda: True, boom)
        interrupts.register("test-after", held.is_active, held.cancel)
        self.addCleanup(interrupts.unregister, "test-boom")
        self.addCleanup(interrupts.unregister, "test-after")
        interrupts.cancel_all("bye")
        self.assertFalse(held.holding)


if __name__ == "__main__":
    unittest.main(verbosity=2)
