"""
The pause between steps counts the time already spent waiting.

After every action the runner waits for a bridge snapshot taken at least
INPUT_SETTLE_MS after it -- on average about 150ms, since the mod publishes
every 200ms -- and THEN slept the whole observation interval on top. Measured
in a timed simulation, a walk round a wall spent 5.0 of its 7.8 seconds
waiting: ~560ms a step, of which the interval is 400.

The interval is a floor between one action ending and the next starting.
Time spent waiting for the snapshot is time in that gap, so it counts: the
runner now sleeps only what is left of the interval. The floor itself --
MIN_OBSERVATION_INTERVAL_S -- is unchanged, and a slow step still does not
"catch up": the action's own duration never counts towards the gap.
"""

from __future__ import annotations

import unittest


from minecraft import task_runner, verification as verify_mod   # noqa: E402
from minecraft.controller import ActionResult                    # noqa: E402
from minecraft.state import EXACT, WorldState                    # noqa: E402
from minecraft.task_runner import Step, TaskRunner               # noqa: E402

START = 1_700_000_000.0


class World:
    """A controller and a dated state source on one virtual clock.

    Each action takes ACTION_S. The bridge publishes every `publish_s`; its
    stamp is the time of the last publish, so the runner has to wait for the
    next one after each action, as it does against the real mod."""

    stamp_units = "epoch_ms"
    ACTION_S = 0.5

    def __init__(self, publish_s=0.2):
        self.t = START
        self.publish_s = publish_s
        self.x = 0.0
        self.actions = []              # (start, end) of each action

    # clock
    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += max(0.0, seconds)

    # state source
    def stamp(self):
        published = (self.t - START) // self.publish_s * self.publish_s
        return (START + published) * 1000.0

    def read(self):
        return WorldState(position=(self.x, 64.0, 0.0), health=20.0,
                          source="bridge", confidence=EXACT)

    # controller
    def _guard(self):
        return ""

    def move(self, params):
        start = self.t
        self.t += self.ACTION_S
        self.x += 1.0
        self.actions.append((start, self.t))
        return ActionResult(ok=True, action="move", requested=dict(params),
                            actual_duration_ms=int(self.ACTION_S * 1000))


class Walk:
    name = goal = "walk"

    def __init__(self, steps=6):
        self.steps = steps

    def plan(self, state, index, history):
        if index >= self.steps:
            return None
        return Step(action="move", params={"direction": "forward"},
                    expectation=verify_mod.moved(min_distance=0.1))


def gaps(publish_s=0.2, interval=task_runner.DEFAULT_OBSERVATION_INTERVAL_S):
    world = World(publish_s)
    runner = TaskRunner(world, world, interval_s=interval,
                        sleeper=world.sleep, clock=world.now,
                        wall_clock=world.now)
    runner.run(Walk())
    return [round(nxt[0] - prev[1], 3)
            for prev, nxt in zip(world.actions, world.actions[1:])]


class PacingTests(unittest.TestCase):

    def test_the_interval_is_still_a_floor(self):
        interval = task_runner.DEFAULT_OBSERVATION_INTERVAL_S
        for gap in gaps():
            self.assertGreaterEqual(gap, interval - 1e-6)

    def test_the_snapshot_wait_counts_towards_it(self):
        """Not interval + wait: the gap is the interval, give or take a
        poll."""
        interval = task_runner.DEFAULT_OBSERVATION_INTERVAL_S
        for gap in gaps():
            self.assertLessEqual(
                gap, interval + task_runner.FRESH_STATE_POLL_S + 1e-6,
                f"slept the whole interval after waiting: {gaps()}")

    def test_a_slow_bridge_adds_no_sleep_on_top(self):
        """When the wait for a fresh snapshot is already longer than the
        interval, there is nothing left to sleep."""
        interval = task_runner.MIN_OBSERVATION_INTERVAL_S
        for gap in gaps(publish_s=0.5, interval=interval):
            self.assertGreaterEqual(gap, interval - 1e-6)
            self.assertLessEqual(gap, 0.5 + task_runner.INPUT_SETTLE_MS / 1000
                                 + task_runner.FRESH_STATE_POLL_S + 1e-6)

    def test_the_floor_is_unchanged(self):
        self.assertEqual(task_runner.MIN_OBSERVATION_INTERVAL_S, 0.25)
        runner = TaskRunner(World(), World(), interval_s=0.0)
        self.assertEqual(runner._interval,
                         task_runner.MIN_OBSERVATION_INTERVAL_S)


if __name__ == "__main__":
    unittest.main(verbosity=2)
