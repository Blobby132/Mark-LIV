"""
Fakes for the controller's collaborators (window locator, process
check) and for the task runner's controller and state source.

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/minecraft/test_minecraft_controller.py, tests/minecraft/test_minecraft_tasks.py.
"""

from __future__ import annotations

from minecraft.window import WindowInfo, WindowRect
from minecraft.controller import ActionResult
from minecraft.state import empty_state
from minecraft.task_runner import TaskRunner


class FakeLocator:
    """A window whose state the test sets directly."""

    def __init__(self, found=True, foreground=True, focus_known=True,
                 pid=4242, width=1920, height=1080):
        self.found = found
        self.foreground = foreground
        self.focus_known = focus_known
        self.pid = pid
        self.width = width
        self.height = height
        self.probes = 0

    def attach(self) -> WindowInfo:
        return self.probe()

    def probe(self) -> WindowInfo:
        self.probes += 1
        rect = (WindowRect(0, 0, self.width, self.height)
                if self.found else None)
        return WindowInfo(found=self.found, handle=1, title="Minecraft 1.21",
                          pid=self.pid, rect=rect,
                          foreground=self.foreground,
                          focus_known=self.focus_known,
                          detail="fake window")


class FakeProcess:
    """Stands in for minecraft/process.py."""

    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self, pid):
        return self.alive

    def find(self):
        from minecraft.process import ProcessInfo
        if self.alive:
            return ProcessInfo(running=True, pid=4242, name="javaw.exe",
                               matched_on="fake", detail="fake Minecraft")
        return ProcessInfo(running=False, detail="not running")


class FakeController:
    """Records calls. Its `_guard` is what the runner consults between steps."""

    def __init__(self, guard=""):
        self.calls: list = []
        self.guard_reason = guard

    def _guard(self):
        return self.guard_reason

    def _record(self, name, params):
        self.calls.append((name, dict(params or {})))
        return ActionResult(ok=True, action=name, requested=dict(params or {}),
                            actual_duration_ms=500)

    def move(self, p): return self._record("move", p)
    def look(self, p): return self._record("look", p)
    def jump(self, p): return self._record("jump", p)
    def attack(self, p): return self._record("attack", p)
    def mine(self, p): return self._record("mine", p)
    def place(self, p): return self._record("place", p)
    def interact(self, p): return self._record("interact", p)
    def eat(self, p): return self._record("eat", p)
    def drop(self, p): return self._record("drop", p)
    def inventory(self, p): return self._record("inventory", p)
    def use_item(self, p): return self._record("use_item", p)
    def sneak(self, p): return self._record("sneak", p)
    def sprint(self, p): return self._record("sprint", p)
    def hotbar_select(self, p): return self._record("hotbar_select", p)


class StaticSource:
    def __init__(self, state=None):
        self.state = state if state is not None else empty_state("test")
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.state


def runner(controller=None, source=None):
    return TaskRunner(controller or FakeController(),
                      source or StaticSource(),
                      sleeper=lambda _s: None)
