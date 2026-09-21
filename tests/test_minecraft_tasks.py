"""
Tests for minecraft/task_runner.py and minecraft/skills.py.

THE TWO THINGS THAT MUST HOLD

  1. A task stops. Always, for several independent reasons, and never
     later than MAX_TASK_STEPS.
  2. A skill cannot press a key. It names an action from a fixed table and
     the runner validates it; there is no field on a Step that reaches the
     input backend, and an unknown action ends the task rather than being
     forwarded anywhere.

The second is the planner boundary. Everything bounding a single action — the
guard, the session, the deadman — is about ONE action. The step limit and the
dispatch table are the only things bounding a sequence of them, so they get
adversarial tests: skills that lie, skills that never finish, skills that try
to smuggle a keycode through.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import skills, task_runner, verification as verify_mod  # noqa: E402
from minecraft.controller import ActionResult                          # noqa: E402
from minecraft.state import BlockRef, INFERRED, WorldState, empty_state  # noqa: E402
from minecraft.task_runner import (                                    # noqa: E402
    COMPLETED, FAILED, INCOMPLETE, MAX_TASK_STEPS, STOPPED, Step, TaskRunner,
)


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


# ── Adversarial skills ───────────────────────────────────────────────────────

class Forever:
    """Never says it is done. The step limit is the only thing that stops it."""
    name = goal = "forever"
    def plan(self, state, index, history):
        return Step(action="jump", params={})


class Smuggler:
    """Tries to reach the keyboard through an action name."""
    name = goal = "smuggle"
    def __init__(self, action):
        self.action = action
    def plan(self, state, index, history):
        return Step(action=self.action, params={"key": "alt", "text": "rm -rf"})


class Broken:
    name = goal = "broken"
    def plan(self, state, index, history):
        raise RuntimeError("the skill exploded")


class Liar:
    """Returns something that is not a Step at all."""
    name = goal = "liar"
    def __init__(self, value):
        self.value = value
    def plan(self, state, index, history):
        return self.value


class Immediate:
    name = goal = "immediate"
    def plan(self, state, index, history):
        return None


# ── Bounding ─────────────────────────────────────────────────────────────────

class TestTasksAlwaysStop(unittest.TestCase):

    def test_an_endless_skill_hits_the_step_limit(self):
        controller = FakeController()
        result = runner(controller).run(Forever())
        self.assertEqual(result.status, INCOMPLETE)
        self.assertEqual(result.reason, "step_limit")
        self.assertEqual(result.steps_taken, MAX_TASK_STEPS)
        self.assertEqual(len(controller.calls), MAX_TASK_STEPS)

    def test_the_ceiling_cannot_be_raised_by_a_caller(self):
        """A tool parameter must not be able to buy more steps than the
        module allows — otherwise the limit is a suggestion."""
        for asked in (50, 999, 10 ** 9):
            with self.subTest(max_steps=asked):
                result = runner().run(Forever(), max_steps=asked)
                self.assertEqual(result.steps_taken, MAX_TASK_STEPS)

    def test_a_caller_may_ask_for_fewer(self):
        result = runner().run(Forever(), max_steps=3)
        self.assertEqual(result.steps_taken, 3)

    def test_a_nonsense_limit_falls_back_to_the_ceiling(self):
        for asked in ("lots", None, -5, 0):
            with self.subTest(max_steps=repr(asked)):
                result = runner().run(Forever(), max_steps=asked)
                self.assertLessEqual(result.steps_taken, MAX_TASK_STEPS)
                self.assertGreaterEqual(result.steps_taken, 1)

    def test_a_finished_skill_stops_immediately(self):
        controller = FakeController()
        result = runner(controller).run(Immediate())
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(controller.calls, [])


class TestExternalStopsEndTasks(unittest.TestCase):

    def test_cancellation_stops_before_the_next_step(self):
        controller = FakeController()
        r = runner(controller)
        r.cancel("user asked me to stop")
        result = r.run(Forever())
        self.assertEqual(result.status, STOPPED)
        self.assertIn("stop", result.reason)
        self.assertEqual(controller.calls, [])

    def test_focus_loss_stops_the_task(self):
        controller = FakeController(guard="focus_lost")
        result = runner(controller).run(Forever())
        self.assertEqual(result.status, STOPPED)
        self.assertEqual(result.reason, "focus_lost")
        self.assertEqual(controller.calls, [])

    def test_session_expiry_stops_the_task(self):
        controller = FakeController(guard="session_expired")
        result = runner(controller).run(Forever())
        self.assertEqual(result.status, STOPPED)
        self.assertEqual(result.reason, "session_expired")

    def test_the_game_closing_stops_the_task(self):
        controller = FakeController(guard="process_gone")
        result = runner(controller).run(Forever())
        self.assertEqual(result.status, STOPPED)

    def test_a_stop_part_way_through_keeps_the_steps_already_taken(self):
        """Focus is lost while planning step 2. That step still runs — the
        runner checks between steps, and the controller's own per-tick guard
        is what refuses an action already under way — and the task then stops
        at the top of step 3 with its first three records intact."""
        controller = FakeController()
        r = runner(controller)

        class StopsItself:
            name = goal = "stops"
            def plan(_self, state, index, history):
                if index == 2:
                    controller.guard_reason = "focus_lost"
                return Step(action="jump", params={})

        result = r.run(StopsItself())
        self.assertEqual(result.status, STOPPED)
        self.assertEqual(result.reason, "focus_lost")
        self.assertEqual(result.steps_taken, 3)
        self.assertLess(result.steps_taken, MAX_TASK_STEPS)


# ── The planner boundary ─────────────────────────────────────────────────────

class TestPlannerCannotReachTheKeyboard(unittest.TestCase):

    def test_an_unknown_action_is_refused_not_forwarded(self):
        for action in ("press", "type", "key_down", "hotkey", "run",
                       "exec", "screenshot", "chat", "command", ""):
            with self.subTest(action=action):
                controller = FakeController()
                result = runner(controller).run(Smuggler(action))
                self.assertEqual(result.status, FAILED)
                self.assertIn("not an action", result.reason)
                self.assertEqual(controller.calls, [])

    def test_the_dispatch_table_contains_no_raw_input_method(self):
        """If a name like `press` ever appears here, the boundary is gone."""
        for forbidden in ("press", "type", "key_down", "key_up", "hotkey",
                          "send_keys", "button_down", "move_mouse_relative",
                          "chat", "command", "run"):
            self.assertNotIn(forbidden, task_runner.DISPATCH)

    def test_every_dispatch_target_exists_on_the_controller(self):
        """A stale entry would be an AttributeError mid-task, with a key
        possibly already held."""
        from minecraft.controller import MinecraftController
        for action, method in task_runner.DISPATCH.items():
            with self.subTest(action=action):
                self.assertTrue(hasattr(MinecraftController, method))

    def test_extra_parameters_do_not_reach_the_backend_unvalidated(self):
        """A smuggled 'key' rides along in params, but the controller parses
        params through action_spec, which ignores anything it does not
        recognise. The test asserts the action still ran as itself."""
        controller = FakeController()
        result = runner(controller).run(Smuggler("jump"))
        self.assertEqual(result.status, INCOMPLETE)
        for name, _params in controller.calls:
            self.assertEqual(name, "jump")

    def test_a_skill_returning_a_non_step_is_refused(self):
        for value in ("move forward", {"action": "move"}, 42, ["move"],
                      object()):
            with self.subTest(value=type(value).__name__):
                controller = FakeController()
                result = runner(controller).run(Liar(value))
                self.assertEqual(result.status, FAILED)
                self.assertEqual(controller.calls, [])

    def test_non_dict_parameters_are_refused(self):
        class BadParams:
            name = goal = "bad"
            def plan(self, state, index, history):
                return Step(action="move", params="forward")
        controller = FakeController()
        result = runner(controller).run(BadParams())
        self.assertEqual(result.status, FAILED)
        self.assertEqual(controller.calls, [])

    def test_a_skill_that_raises_fails_the_task_without_acting(self):
        controller = FakeController()
        result = runner(controller).run(Broken())
        self.assertEqual(result.status, FAILED)
        self.assertIn("RuntimeError", result.reason)
        self.assertEqual(controller.calls, [])


# ── Verification inside a task ───────────────────────────────────────────────

class TestTasksVerify(unittest.TestCase):

    def _log_then_air(self, breaks_after: int):
        class Source:
            def __init__(self): self.n = 0
            def read(self):
                self.n += 1
                name = "oak_log" if self.n <= breaks_after else "air"
                return WorldState(target_block=BlockRef(x=1, y=2, z=3,
                                                        name=name),
                                  source="f3", confidence=INFERRED)
        return Source()

    def test_a_real_state_change_is_recorded_as_success(self):
        result = runner(FakeController(),
                        self._log_then_air(3)).run(
                            skills.BreakBlock(expected="oak_log"))
        self.assertEqual(result.status, COMPLETED)
        self.assertEqual(result.verified_steps, 1)
        statuses = [r.verification["status"] for r in result.records]
        self.assertIn(verify_mod.SUCCESS, statuses)
        self.assertIn(verify_mod.FAILED, statuses)

    def test_swings_that_did_not_break_it_are_recorded_as_failures(self):
        """Not as successes, and not hidden. The per-step trail is what makes
        "I swung four times and it broke on the fourth" a true sentence."""
        result = runner(FakeController(),
                        self._log_then_air(3)).run(
                            skills.BreakBlock(expected="oak_log"))
        # Three swings in total: two that left the log standing, and the
        # third whose "after" observation caught it disappearing.
        failures = [r for r in result.records
                    if r.verification["status"] == verify_mod.FAILED]
        self.assertEqual(len(failures), 2)
        self.assertEqual(len(result.records), 3)
        for record in failures:
            self.assertTrue(record.delivered,
                            "the swing WAS delivered — only the goal failed")

    def test_a_blind_task_never_claims_success(self):
        """No state source output at all: the actions happen, and the summary
        must say it could not verify any of them."""
        result = runner(FakeController(), StaticSource()).run(
            skills.WalkForward(seconds=4.0))
        self.assertEqual(result.verified_steps, 0)
        self.assertGreater(result.unverifiable_steps, 0)
        self.assertIn("could not verify", result.describe())

    def test_an_action_with_no_expectation_is_unverifiable_not_success(self):
        class NoCheck:
            name = goal = "nocheck"
            def plan(self, state, index, history):
                return None if index else Step(action="jump", params={})
        result = runner().run(NoCheck())
        self.assertEqual(result.records[0].verification["status"],
                         verify_mod.UNVERIFIABLE)

    def test_one_observation_per_step_not_two(self):
        """Sharing the observation between "after" and the next "before" is
        what lets a verification catch the transition, and halves the
        captures."""
        source = StaticSource()
        runner(FakeController(), source).run(Forever(), max_steps=5)
        self.assertEqual(source.reads, 6)      # one up front, one per step


# ── Skills ───────────────────────────────────────────────────────────────────

class TestSkills(unittest.TestCase):

    def test_the_registry_only_builds_known_skills(self):
        for bad in ("rm -rf", "shell", "", None, "chat"):
            with self.subTest(name=repr(bad)):
                with self.assertRaises(KeyError):
                    skills.create(bad)

    def test_every_builtin_skill_can_be_built_and_has_a_goal(self):
        for name in skills.available():
            with self.subTest(skill=name):
                built = skills.create(name)
                self.assertTrue(built.goal)
                self.assertTrue(built.verifiable_with)

    def test_walk_forward_splits_a_long_walk_into_bounded_steps(self):
        from minecraft import action_spec
        controller = FakeController()
        runner(controller).run(skills.WalkForward(seconds=6.0))
        self.assertGreater(len(controller.calls), 1)
        for _name, params in controller.calls:
            self.assertLessEqual(params["duration"],
                                 action_spec.MAX_MOVE_DURATION_S)

    def test_break_block_stops_rather_than_break_the_wrong_thing(self):
        """Aimed at stone while asked for a log: stopping is correct, and
        strictly better than mining whatever happens to be there."""
        source = StaticSource(WorldState(
            target_block=BlockRef(name="stone"), source="f3",
            confidence=INFERRED))
        controller = FakeController()
        result = runner(controller, source).run(
            skills.BreakBlock(expected="oak_log"))
        self.assertEqual(result.status, COMPLETED)
        self.assertIn("stone", result.reason)
        self.assertEqual(controller.calls, [],
                         "it swung at the wrong block")

    def test_find_block_reports_honestly_when_nothing_is_found(self):
        source = StaticSource(WorldState(
            target_block=BlockRef(name="grass_block"), source="f3",
            confidence=INFERRED))
        result = runner(FakeController(), source).run(
            skills.FindBlock(steps=3))
        self.assertEqual(result.status, COMPLETED)
        self.assertIn("without", result.reason)

    def test_find_block_stops_as_soon_as_the_target_is_under_the_crosshair(self):
        source = StaticSource(WorldState(
            target_block=BlockRef(name="birch_log"), source="f3",
            confidence=INFERRED))
        controller = FakeController()
        result = runner(controller, source).run(skills.FindBlock())
        self.assertEqual(result.status, COMPLETED)
        self.assertIn("birch_log", result.reason)
        self.assertEqual(controller.calls, [])

    def test_the_things_it_cannot_do_are_named_rather_than_attempted(self):
        for name in ("collect_wood", "craft_item", "build_structure",
                     "return_to_base"):
            with self.subTest(skill=name):
                self.assertIn(name, skills.NOT_YET_POSSIBLE)
                self.assertNotIn(name, skills.available())


if __name__ == "__main__":
    unittest.main()
