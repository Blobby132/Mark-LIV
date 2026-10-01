"""
Item 2: no gameplay input while a screen is open.

With a container screen open, a left click from attack or mine, a right
click from place, interact or use_item, a held right click from eat, and
the drop key all land in the GUI: a click on a slot moves a stack, a drop
key throws the hovered one. So every gameplay hold -- attack, mine,
place, interact, use_item, eat, drop -- is refused, nothing pressed, when
the bridge reports any screen open. The screen actions (gui_*) and
`inventory` are what work inside one, and are exempt.

And the task runner closes an allowed screen (inventory, crafting table)
that the task itself opened, when the task ends by cancel, timeout or
error: a screen left open would swallow the user's next keys too.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from minecraft import verification as verify_mod                    # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.state import WorldState                                 # noqa: E402
from minecraft.task_runner import (                                    # noqa: E402
    COMPLETED, Step, TaskRunner)
from test_minecraft_controller import FakeLocator, FakeProcess         # noqa: E402
from tests.gui_world import GuiWorld                                   # noqa: E402

HOLDS = (("attack", {"duration": 0.05}), ("mine", {"duration": 0.05}),
         ("place", {}), ("interact", {}), ("use_item", {"duration": 0.05}),
         ("eat", {"duration": 0.05}), ("drop", {}))


class ControllerTests(unittest.TestCase):

    def controller(self, screen):
        backend = FakeInputBackend()
        state = WorldState(screen=screen, source="bridge")
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0,
            held_item_probe=lambda: "cobblestone",
            entity_probe=lambda: ("zombie", "hostile"),
            gui_probe=lambda: state)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        return controller, backend

    def test_every_gameplay_hold_is_refused_with_a_screen_open(self):
        for screen in ("inventory", "crafting_table", "chest", "pause"):
            for action, params in HOLDS:
                with self.subTest(screen=screen, action=action):
                    controller, backend = self.controller(screen)
                    result = getattr(controller, action)(dict(params))
                    self.assertFalse(result.ok)
                    self.assertEqual(result.stopped_reason, "screen_open")
                    self.assertEqual(backend.events, [],
                                     "something was pressed")
                    self.assertIn(" ".join(screen.split("_")), result.error)
                    self.assertIn("inventory close", result.error)

    def test_with_no_screen_they_work_as_before(self):
        for action, params in HOLDS:
            with self.subTest(action=action):
                controller, backend = self.controller(None)
                result = getattr(controller, action)(dict(params))
                self.assertTrue(result.ok, f"{action}: {result.error}")

    def test_closing_the_screen_is_not_refused(self):
        controller, backend = self.controller("inventory")
        result = controller.inventory({"state": "close"})
        self.assertTrue(result.ok, result.error)
        self.assertTrue(backend.events)

    def test_movement_is_not_part_of_this_rule(self):
        controller, _backend = self.controller("inventory")
        result = controller.move({"direction": "forward", "duration": 0.05})
        self.assertNotEqual(result.stopped_reason, "screen_open")


class OpensThenStops:
    """A task that opens the inventory, then keeps looking about -- until
    whatever the test arranges ends it."""

    name = "opens_then_stops"
    goal = "open the inventory and dawdle"
    failed = False
    done_reason = "finished"
    watch_hostiles = False
    watch_health = False

    def __init__(self, raise_at=None, finish_at=None, close_first=False):
        self.raise_at = raise_at
        self.finish_at = finish_at
        self.close_first = close_first

    def plan(self, state, step_index, history):
        if step_index == self.raise_at:
            raise RuntimeError("a bug in the skill")
        if step_index == self.finish_at:
            return None
        if step_index == 0:
            return Step(action="inventory", params={"state": "open"},
                        expectation=verify_mod.screen_is("inventory"))
        return Step(action="gui_point", params={"dx": 5, "dy": 0},
                    expectation=verify_mod.pointer_moved())


class RunnerTests(unittest.TestCase):

    def run_task(self, world, skill, **runner_options):
        runner = TaskRunner(world, world, sleeper=lambda _s: None,
                            **runner_options)
        return runner, runner

    def test_a_cancelled_task_closes_the_screen_it_opened(self):
        world = GuiWorld({0: ("dirt", 1)})
        runner = TaskRunner(world, world, sleeper=lambda _s: None)
        original = world.gui_point

        def point_then_cancel(params):
            runner.cancel("the user said stop")
            return original(params)
        world.gui_point = point_then_cancel
        result = runner.run(OpensThenStops(), max_steps=10)
        self.assertIsNone(world.screen, "the screen was left open")
        self.assertIn("closed the inventory", result.reason)

    def test_a_task_that_times_out_closes_it(self):
        world = GuiWorld({0: ("dirt", 1)})
        ticks = iter(range(0, 10_000, 30))
        runner = TaskRunner(world, world, sleeper=lambda _s: None,
                            clock=lambda: float(next(ticks)))
        result = runner.run(OpensThenStops(), max_steps=40)
        self.assertIn("time", result.reason.lower())
        self.assertIsNone(world.screen, "the screen was left open")

    def test_a_skill_that_raises_closes_it(self):
        world = GuiWorld({0: ("dirt", 1)})
        runner = TaskRunner(world, world, sleeper=lambda _s: None)
        runner.run(OpensThenStops(raise_at=3), max_steps=10)
        self.assertIsNone(world.screen, "the screen was left open")

    def test_a_crash_in_the_runner_itself_still_closes_it(self):
        """Not the skill's plan (that is a FAILED result): something that
        escapes the runner -- here a skill whose report raises."""
        world = GuiWorld({0: ("dirt", 1)})
        runner = TaskRunner(world, world, sleeper=lambda _s: None)

        class BadReport(OpensThenStops):
            @property
            def done_reason(self):
                raise RuntimeError("the report broke")
        with self.assertRaises(RuntimeError):
            runner.run(BadReport(finish_at=2), max_steps=10)
        self.assertIsNone(world.screen, "the screen was left open")

    def test_a_screen_the_user_had_open_is_left_alone(self):
        world = GuiWorld({0: ("dirt", 1)})
        world.screen = "inventory"              # opened before the task
        runner = TaskRunner(world, world, sleeper=lambda _s: None)

        class Dawdle(OpensThenStops):
            def plan(self, state, step_index, history):
                if step_index >= 2:
                    raise RuntimeError("stop here")
                return Step(action="gui_point", params={"dx": 5, "dy": 0},
                            expectation=verify_mod.pointer_moved())
        runner.run(Dawdle(), max_steps=10)
        self.assertEqual(world.screen, "inventory")

    def test_a_task_that_finishes_is_not_second_guessed(self):
        """A skill that ends normally closes its own screens (craft_item,
        the hotbar fetch); the runner does not press anything after it."""
        world = GuiWorld({0: ("dirt", 1)})
        runner = TaskRunner(world, world, sleeper=lambda _s: None)
        result = runner.run(OpensThenStops(finish_at=2), max_steps=10)
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(world.screen, "inventory")


if __name__ == "__main__":
    unittest.main(verbosity=2)
