"""
Danger is checked DURING a hold, not only between steps.

The task runner's danger watch looks after every step -- but a mine step
holds attack for up to ten seconds and a move for two, and nothing looked in
between. A zombie arriving a second into a long mine got nine more seconds.

The controller now takes an injected `hazard_probe(action)`, like
`progress_probe`: asked once per hold, it returns a check to call while the
keys are down (or None for no check). A non-empty answer lets go, and the
result says why. The game rules -- how close, how hurt, which actions --
live in actions/minecraft.py, and walking has no check at all, because
walking is how you get away.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource                  # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.session import SessionManager                           # noqa: E402
from minecraft.state import EntityRef, EXACT, WorldState               # noqa: E402
from test_minecraft_controller import FakeLocator, FakeProcess         # noqa: E402


class ControllerHoldTests(unittest.TestCase):

    def controller(self, hazard_probe):
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0,
            hazard_probe=hazard_probe)
        controller.start_session(duration_s=120)
        self.addCleanup(controller.stop, "test")
        return controller

    def test_a_mine_lets_go_when_danger_arrives(self):
        asked = []

        def probe(action):
            asked.append(action)
            started = time.monotonic()
            return lambda: ("a zombie is 2 blocks away"
                            if time.monotonic() - started > 0.2 else None)

        controller = self.controller(probe)
        result = controller.mine({"duration": 6.0})
        self.assertEqual(asked, ["mine"])
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "danger")
        self.assertIn("a zombie is 2 blocks away", result.error)
        self.assertLess(result.actual_duration_ms, 2000,
                        "it held on after the danger was seen")
        self.assertEqual(self.backend.held, set(), "something is still held")

    def test_danger_already_there_presses_nothing(self):
        controller = self.controller(
            lambda action: (lambda: "a creeper is 2 blocks away"))
        result = controller.mine({"duration": 2.0})
        self.assertEqual(result.stopped_reason, "danger")
        self.assertIn("did not start", result.error)
        self.assertEqual(self.backend.events, [], "it pressed something")

    def test_no_check_for_an_action_holds_as_asked(self):
        controller = self.controller(lambda action: None)
        result = controller.attack({"duration": 0.3})
        self.assertTrue(result.ok, result.error)
        self.assertGreaterEqual(result.actual_duration_ms, 250)

    def test_a_probe_that_breaks_is_no_danger_and_still_releases(self):
        def probe(action):
            def check():
                raise RuntimeError("bridge hiccup")
            return check

        controller = self.controller(probe)
        result = controller.move({"direction": "forward", "duration": 0.3})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.held, set())

    def test_a_probe_that_cannot_start_is_no_check(self):
        def probe(action):
            raise RuntimeError("no state")
        result = self.controller(probe).mine({"duration": 0.3})
        self.assertTrue(result.ok, result.error)


def seen(health=20.0, mob_at=None):
    mobs = ()
    if mob_at is not None:
        mobs = (EntityRef(name="minecraft:zombie", distance=mob_at,
                          hostile=True, category="hostile",
                          position=(0.0, 64.0, mob_at)),)
    return WorldState(position=(0.0, 64.0, 0.0), health=health,
                      nearby_entities=mobs, source="bridge", confidence=EXACT)


class HazardPolicyTests(unittest.TestCase):
    """actions/minecraft.py's rules for which holds stop, and when."""

    def setUp(self):
        from actions import minecraft as mc_actions
        self.mc = mc_actions
        self.states = [seen()]
        self.original = mc_actions._get_state_source

        class Source(ModBridgeStateSource):
            def __init__(inner):
                pass

            def read(inner):
                return self.states[0]

        mc_actions._get_state_source = lambda: Source()
        self.addCleanup(setattr, mc_actions, "_get_state_source",
                        self.original)

    def check(self, action, then):
        watch = self.mc._hazard_probe(action)
        if watch is None:
            return None
        self.states[0] = then
        return watch()

    def test_mining_stops_for_a_zombie_three_blocks_away(self):
        self.assertIn("zombie", self.check("mine", seen(mob_at=2.5)))
        self.assertIsNone(self.check("mine", seen(mob_at=4.5)))

    def test_mining_stops_for_a_lost_heart(self):
        self.assertIn("taking damage", self.check("mine", seen(health=18)))

    def test_walking_is_never_stopped(self):
        """Walking is how you get away. The old "looser" check stopped a
        move for a mob at 1.2 blocks or health at 15 -- refusing the step
        back from a zombie exactly when it was needed."""
        for action in ("move", "move_and_jump", "sprint", "sneak"):
            with self.subTest(action=action):
                self.assertIsNone(self.mc._hazard_probe(action))

    def test_fighting_is_not_stopped_for_the_fight(self):
        self.assertIsNone(self.mc._hazard_probe("attack"))
        self.assertIsNone(self.mc._hazard_probe("use_item"))

    def test_the_controller_is_built_with_it(self):
        import inspect
        source = inspect.getsource(self.mc._get_controller)
        self.assertIn("hazard_probe=_hazard_probe", source)


class WalkingAwayEndToEndTests(unittest.TestCase):
    """The real controller with the real _hazard_probe, and a zombie at
    arm's length: stepping back must press the keys; mining must not."""

    def setUp(self):
        from actions import minecraft as mc_actions
        self.mc = mc_actions
        zombie = seen(mob_at=1.0)

        class Source(ModBridgeStateSource):
            def __init__(inner):
                pass

            def read(inner):
                return zombie

        original = mc_actions._get_state_source
        mc_actions._get_state_source = lambda: Source()
        self.addCleanup(setattr, mc_actions, "_get_state_source", original)
        self.backend = FakeInputBackend()
        self.controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0,
            hazard_probe=mc_actions._hazard_probe)
        self.controller.start_session(duration_s=60)
        self.addCleanup(self.controller.stop, "test")

    def test_stepping_back_from_an_adjacent_zombie_is_pressed(self):
        result = self.controller.move({"direction": "back", "duration": 0.3})
        self.assertTrue(result.ok, result.error)
        self.assertNotEqual(result.stopped_reason, "danger")
        self.assertTrue(self.backend.events, "nothing was pressed")
        self.assertEqual(self.backend.held, set())

    def test_sprinting_away_is_pressed(self):
        result = self.controller.sprint({"direction": "back",
                                         "duration": 0.3})
        self.assertTrue(result.ok, result.error)

    def test_mining_next_to_it_still_is_not(self):
        result = self.controller.mine({"duration": 1.0})
        self.assertEqual(result.stopped_reason, "danger")
        self.assertEqual(self.backend.events, [])


class ProbeCostTests(unittest.TestCase):
    """A2: the check read _get_state_source() every 100ms inside the hold.
    On the OCR route that is a screenshot and OCR -- 300ms here -- which
    held up the 40ms focus-guard tick while keys were down, and the overlay
    reports no health or mobs, so the check could never fire there."""

    def setUp(self):
        from actions import minecraft as mc_actions
        self.mc = mc_actions
        original = mc_actions._get_state_source
        self.addCleanup(setattr, mc_actions, "_get_state_source", original)

    def use(self, source):
        self.resolved = 0

        def resolve():
            self.resolved += 1
            return source
        self.mc._get_state_source = resolve

    def test_no_check_without_the_bridge(self):
        class Overlay:
            def read(inner):
                time.sleep(0.3)
                return seen()
        self.use(Overlay())
        self.assertIsNone(self.mc._hazard_probe("mine"))

    def test_a_slow_source_does_not_delay_guard_ticks(self):
        class Overlay:
            def read(inner):
                time.sleep(0.3)
                return seen()
        self.use(Overlay())

        class Timed(FakeLocator):
            times: list = []

            def probe(inner):
                inner.times.append(time.monotonic())
                return FakeLocator.probe(inner)

        locator = Timed()
        controller = MinecraftController(
            backend=FakeInputBackend(), locator=locator,
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0,
            hazard_probe=self.mc._hazard_probe)
        controller.start_session(duration_s=60)
        self.addCleanup(controller.stop, "test")
        locator.times = []
        controller.mine({"duration": 0.8})
        gaps = [b - a for a, b in zip(locator.times, locator.times[1:])]
        self.assertLess(max(gaps), 0.15,
                        f"a guard tick waited {max(gaps) * 1000:.0f}ms")

    def test_the_bridge_is_resolved_once_per_hold(self):
        from minecraft.mod_bridge import ModBridgeStateSource

        class Bridge(ModBridgeStateSource):
            def __init__(inner):
                pass

            def read(inner):
                return seen()
        self.use(Bridge())
        check = self.mc._hazard_probe("mine")
        self.assertIsNotNone(check)
        for _ in range(5):
            check()
        self.assertEqual(self.resolved, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
