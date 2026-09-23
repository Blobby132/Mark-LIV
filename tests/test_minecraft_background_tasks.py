"""
Minecraft tasks run in the background, and stop when told to.

What these pin down:

  * run_task returns at once while the task keeps running -- so the voice
    session that called it is free to hear the next thing, including "stop";
  * only one task runs at a time, and while it runs nothing else presses keys;
  * cancelling (spoken stop, cancel_task, a withdrawn tool call) stops the
    step already in progress, releases every key and button, and leaves the
    session open; `stop` still ends the session, and F12's path is untouched;
  * the finished task is reported back through `speak`, without overstating;
  * a session that was never authorised still presses nothing, task or not.

These use the real controller with the recording backend, and a state
source that reads nothing -- the subject is the scheduling and stopping, not
the game.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import interrupts                                         # noqa: E402
from minecraft.controller import MinecraftController, TICK_SECONDS  # noqa: E402
from minecraft.errors import TaskAlreadyRunning                     # noqa: E402
from minecraft.input_backend import FakeInputBackend                # noqa: E402
from minecraft.state import EXACT, WorldState                       # noqa: E402
from minecraft.task_runner import STOPPED, Step, TaskRunner         # noqa: E402
from minecraft.task_slot import TaskSlot                            # noqa: E402

from tests.test_minecraft_controller import FakeLocator, FakeProcess  # noqa: E402


class StillSource:
    """Reports a player standing still. Enough to plan and verify against."""
    name = "still"

    def read(self):
        return WorldState(position=(0.0, 64.0, 0.0), rotation=(0.0, 0.0),
                          source="test", confidence=EXACT)

    def available(self):
        return True


class HoldForever:
    """A skill that keeps asking to walk forward, two seconds at a time.

    Long enough that a test can only see it end by cancelling it."""
    name = "hold_forever"
    goal = "walk forward until stopped"
    verifiable_with = ("position",)

    def plan(self, state, step_index, history):
        return Step(action="move",
                    params={"direction": "forward", "duration": 2.0},
                    note="walk")


def controller(backend=None, authorized=True, session=True):
    backend = backend or FakeInputBackend()
    c = MinecraftController(backend=backend, locator=FakeLocator(),
                            process_module=FakeProcess(),
                            start_watchers=False, focus_wait_s=0)
    if session:
        c.start_session(duration_s=0, authorized=authorized)
    return c, backend


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TaskSlotTests(unittest.TestCase):

    def test_start_returns_while_the_task_is_still_running(self):
        c, backend = controller()
        slot = TaskSlot()
        runner = TaskRunner(c, StillSource(), sleeper=lambda _s: None)
        began = time.monotonic()
        job = slot.start(runner, HoldForever(), name="hold_forever")
        self.assertLess(time.monotonic() - began, 0.5,
                        "start() waited for the task")
        self.assertTrue(job.running)
        self.assertTrue(wait_until(lambda: backend.held == {"w"}),
                        "the task never pressed forward")
        slot.cancel("test over")
        slot.wait(3)

    def test_a_second_task_is_refused_not_queued_or_swapped(self):
        c, _backend = controller()
        slot = TaskSlot()
        first = slot.start(TaskRunner(c, StillSource(),
                                      sleeper=lambda _s: None),
                           HoldForever(), name="first")
        with self.assertRaises(TaskAlreadyRunning) as raised:
            slot.start(TaskRunner(c, StillSource(), sleeper=lambda _s: None),
                       HoldForever(), name="second")
        self.assertIn("first", str(raised.exception))
        self.assertIs(slot.current(), first)
        slot.cancel("test over")
        slot.wait(3)

    def test_cancel_stops_the_step_in_progress_and_releases_everything(self):
        c, backend = controller()
        slot = TaskSlot()
        finished = []
        job = slot.start(TaskRunner(c, StillSource(), sleeper=lambda _s: None),
                         HoldForever(), name="hold_forever",
                         on_done=finished.append)
        self.assertTrue(wait_until(lambda: backend.held == {"w"}))

        asked = time.monotonic()
        slot.cancel("you said stop")
        slot.wait(3)
        took = time.monotonic() - asked

        self.assertFalse(job.running)
        self.assertLess(took, 1.0, "the 2s hold ran on after the cancel")
        self.assertEqual(backend.held, set(), "a key was left down")
        self.assertEqual(finished, [job], "on_done must run exactly once")
        self.assertEqual(job.result.status, STOPPED)
        self.assertIn("you said stop", job.result.reason)
        self.assertTrue(c.sessions.is_active(),
                        "cancelling a task must not end the session")
        self.assertIsNone(slot.current(), "the slot was not freed")

    def test_a_finished_task_frees_the_slot_before_reporting(self):
        c, _backend = controller()
        slot = TaskSlot()
        seen_busy = []

        class OneStep:
            name = goal = "one step"
            verifiable_with = ()

            def plan(self, state, step_index, history):
                if step_index:
                    return None
                return Step(action="jump", params={}, note="hop")

        slot.start(TaskRunner(c, StillSource(), sleeper=lambda _s: None),
                   OneStep(), name="one_step",
                   on_done=lambda job: seen_busy.append(slot.busy()))
        slot.wait(3)
        self.assertEqual(seen_busy, [False])

    def test_f12_still_hard_stops_a_background_task(self):
        """The emergency stop does not go through the slot or the voice path
        at all -- it is the controller's own stop -- and it must still end a
        task running on another thread, release everything and end the
        session."""
        c, backend = controller()
        slot = TaskSlot()
        job = slot.start(TaskRunner(c, StillSource(), sleeper=lambda _s: None),
                         HoldForever(), name="hold_forever")
        self.assertTrue(wait_until(lambda: backend.held == {"w"}))

        asked = time.monotonic()
        c.emergency_stop("F12 pressed")
        job.done.wait(3)

        self.assertFalse(job.running)
        self.assertLess(time.monotonic() - asked, 1.0)
        self.assertEqual(backend.held, set())
        self.assertFalse(c.sessions.is_active(), "F12 must end the session")
        self.assertEqual(job.result.status, STOPPED)
        self.assertIn("F12", job.result.reason)

    def test_an_unauthorised_session_presses_nothing_in_a_task(self):
        c, backend = controller(authorized=False)
        slot = TaskSlot()
        job = slot.start(TaskRunner(c, StillSource(), sleeper=lambda _s: None),
                         HoldForever(), name="hold_forever")
        # Each step is refused by the session's grant; let a few go by.
        time.sleep(0.2)
        slot.cancel("test over")
        slot.wait(3)
        self.assertFalse(job.running)
        self.assertEqual(backend.downs(), [],
                         "a task pressed keys under an unauthorised session")


class ControllerCancelTests(unittest.TestCase):

    def test_cancel_current_stops_a_direct_hold_and_keeps_the_session(self):
        c, backend = controller()
        results = []
        worker = threading.Thread(target=lambda: results.append(
            c.move({"direction": "forward", "duration": 2.0})))
        worker.start()
        self.assertTrue(wait_until(lambda: backend.held == {"w"}))

        report = c.cancel_current("you said stop")
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(backend.held, set())
        self.assertFalse(report["session_ended"])
        self.assertTrue(c.sessions.is_active())
        self.assertEqual(results[0].stopped_reason, "cancelled")
        self.assertLess(results[0].actual_duration_ms, 1500)

    def test_an_action_started_after_a_cancel_runs_normally(self):
        """A cancel stops what is running, not what comes next -- otherwise
        one "stop" would silently refuse the user's next command."""
        c, backend = controller()
        c.cancel_current("earlier stop")
        result = c.move({"direction": "forward", "duration": 0.08})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.downs(), ["w"])

    def test_one_input_action_at_a_time(self):
        c, backend = controller()
        worker = threading.Thread(target=lambda: c.move(
            {"direction": "forward", "duration": 0.6}))
        worker.start()
        self.assertTrue(wait_until(lambda: backend.held == {"w"}))
        second = c.look({"dx": 50, "dy": 0})
        third = c.mine({"duration": 0.2})
        worker.join(2)
        self.assertEqual(second.stopped_reason, "busy")
        self.assertEqual(third.stopped_reason, "busy")
        self.assertEqual(backend.mouse_moves(), [])
        self.assertEqual(backend.button_downs(), [])

    def test_stop_still_ends_the_session(self):
        c, backend = controller()
        outcome = c.stop("F12 pressed")
        self.assertTrue(outcome["stopped"])
        self.assertFalse(c.sessions.is_active())
        self.assertEqual(backend.held, set())

    def test_a_task_scope_stops_the_step_it_wraps(self):
        c, backend = controller()
        cancel = threading.Event()
        results = []

        def step():
            with c.cancellable(cancel):
                results.append(c.move({"direction": "forward",
                                       "duration": 2.0}))

        worker = threading.Thread(target=step)
        worker.start()
        self.assertTrue(wait_until(lambda: backend.held == {"w"}))
        cancel.set()
        worker.join(2)
        self.assertEqual(results[0].stopped_reason, "cancelled")
        self.assertEqual(backend.held, set())
        self.assertLess(results[0].actual_duration_ms,
                        int((TICK_SECONDS * 10 + 0.5) * 1000))


class AdapterBackgroundTests(unittest.TestCase):
    """The tool the model calls."""

    def setUp(self):
        import actions.minecraft as adapter
        self.adapter = adapter
        self.controller, self.backend = controller()
        adapter._reset_for_tests(controller=self.controller,
                                 state_source=StillSource())
        self.said = []

    def tearDown(self):
        self.adapter._slot.cancel("test over")
        self.adapter._slot.wait(3)
        self.adapter._reset_for_tests()

    def start(self, seconds=30):
        return self.adapter.minecraft_control(
            {"action": "run_task", "task": "walk_forward",
             "seconds": seconds}, speak=self.said.append)

    def test_run_task_answers_started_at_once(self):
        began = time.monotonic()
        answer = self.start()
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertIn("Started walk_forward", answer)
        self.assertIn("NOT finished", answer)
        self.assertTrue(self.adapter._slot.busy())

    def test_other_input_is_refused_while_a_task_runs(self):
        self.start()
        self.assertTrue(wait_until(lambda: self.backend.held == {"w"}))
        for action in ({"action": "look", "dx": 40},
                       {"action": "mine"},
                       {"action": "move_and_jump"},
                       {"action": "run_task", "task": "survey"}):
            with self.subTest(action=action["action"]):
                answer = self.adapter.minecraft_control(action)
                # The adapter's own refusal, not the controller's busy slot:
                # between two steps the slot is free, and only this check
                # stops a direct command slipping in there.
                self.assertIn("it has the controls", answer)
        self.assertEqual(self.backend.mouse_moves(), [])
        self.assertEqual(self.backend.button_downs(), [])

    def test_reading_is_never_refused_while_a_task_runs(self):
        self.start()
        answer = self.adapter.minecraft_control({"action": "task_status"})
        self.assertIn("walk_forward task is running", answer)
        status = self.adapter.minecraft_control({"action": "status"})
        self.assertNotIn("still running", status.split("\n")[0])

    def test_cancel_task_stops_it_and_keeps_the_session(self):
        self.start()
        self.assertTrue(wait_until(lambda: self.backend.held == {"w"}))
        answer = self.adapter.minecraft_control({"action": "cancel_task"})
        self.adapter._slot.wait(3)
        self.assertIn("Cancelled the walk_forward task", answer)
        self.assertEqual(self.backend.held, set())
        self.assertTrue(self.controller.sessions.is_active())
        self.assertTrue(wait_until(lambda: self.said))
        self.assertIn("was stopped", self.said[0])
        self.assertIn("still open", self.said[0])

    def test_stop_cancels_the_task_and_ends_the_session(self):
        self.start()
        self.assertTrue(wait_until(lambda: self.backend.held == {"w"}))
        self.adapter.minecraft_control({"action": "stop"})
        self.adapter._slot.wait(3)
        self.assertFalse(self.adapter._slot.busy())
        self.assertEqual(self.backend.held, set())
        self.assertFalse(self.controller.sessions.is_active())

    def test_a_spoken_stop_reaches_the_task_through_the_registry(self):
        self.assertFalse(interrupts.any_active())
        self.start()
        self.assertTrue(wait_until(lambda: self.backend.held == {"w"}))
        self.assertIn("minecraft", interrupts.active_keys())
        self.assertEqual(interrupts.cancel_active("you said stop"),
                         ["minecraft"])
        self.adapter._slot.wait(3)
        self.assertEqual(self.backend.held, set())
        self.assertTrue(self.controller.sessions.is_active())
        self.assertFalse(interrupts.any_active())

    def test_a_withdrawn_tool_call_releases_minecraft_input(self):
        self.start()
        self.assertTrue(wait_until(lambda: self.backend.held == {"w"}))
        self.assertEqual(interrupts.cancel_for_tool(
            "minecraft_control", "withdrawn"), ["minecraft"])
        self.adapter._slot.wait(3)
        self.assertEqual(self.backend.held, set())
        self.assertEqual(interrupts.cancel_for_tool("web_search", "x"), [])

    def test_the_result_is_reported_without_overstating_it(self):
        answer = self.adapter.minecraft_control(
            {"action": "run_task", "task": "walk_forward", "seconds": 0.1},
            speak=self.said.append)
        self.assertIn("Started", answer)
        self.adapter._slot.wait(3)
        self.assertTrue(wait_until(lambda: self.said))
        report = self.said[0]
        self.assertTrue(report.startswith("[Minecraft task] walk_forward"))
        self.assertIn("do not claim more than this says", report)
        # The source never moves, so nothing about the walk was verified --
        # and the report must carry that caveat through to the model rather
        # than a bare "done".
        self.assertIn("could not verify", report)

    def test_no_session_is_refused_now_not_reported_later(self):
        self.controller.stop("ended")
        answer = self.start()
        self.assertIn("did not start the task", answer)
        self.assertFalse(self.adapter._slot.busy())
        self.assertEqual(self.said, [])


class StopWordTests(unittest.TestCase):

    def test_whole_words_only(self):
        for text in ("stop", "Jarvis STOP!", "please halt", "cancel that",
                     "abort", "freeze"):
            with self.subTest(text=text):
                self.assertTrue(interrupts.is_stop_request(text))
        for text in ("unstoppable", "bus stops", "cancellation", "",
                     "mine some wood"):
            with self.subTest(text=text):
                self.assertFalse(interrupts.is_stop_request(text))

    def test_nothing_happens_when_nothing_is_running(self):
        calls = []
        interrupts.register("probe", lambda: False, calls.append)
        try:
            self.assertEqual(interrupts.cancel_active("stop"), [])
            self.assertEqual(calls, [])
        finally:
            interrupts.unregister("probe")

    def test_a_failing_entry_does_not_stop_the_others(self):
        calls = []

        def broken(_reason):
            raise RuntimeError("boom")

        interrupts.register("a_broken", lambda: True, broken)
        interrupts.register("b_fine", lambda: True, calls.append)
        try:
            self.assertEqual(interrupts.cancel_active("stop"), ["b_fine"])
            self.assertEqual(calls, ["stop"])
        finally:
            interrupts.unregister("a_broken")
            interrupts.unregister("b_fine")


if __name__ == "__main__":
    unittest.main(verbosity=2)
