"""
Aiming at an exact block, and never mining on an unconfirmed aim.

What these pin down:

  * aim_at_block / mine_block finish only when the GAME reports the crosshair
    on the requested (x, y, z) -- not when the angle arithmetic looks close;
  * a target that cannot be confirmed (hidden behind a leaf, unreadable
    crosshair) ends the task with nothing mined;
  * CollectLogs keeps correcting when the angle is small but the crosshair is
    on the wrong block, instead of stopping there;
  * the controller re-reads the crosshair at the instant it would press, and
    presses nothing if it is not on `expect_at`;
  * move_and_jump is reachable from the tool the model actually calls;
  * the tool description no longer states limits that stopped being true.

Everything runs against TreeWorld from the navigation tests: a simulated
player with a raycast crosshair that breaks only what it hits.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft import verification as verify_mod                    # noqa: E402
from minecraft.action_spec import parse_mine                        # noqa: E402
from minecraft.controller import MinecraftController                # noqa: E402
from minecraft.errors import InvalidAction                          # noqa: E402
from minecraft.input_backend import FakeInputBackend                # noqa: E402
from minecraft.state import (                                       # noqa: E402
    BlockRef, EXACT, INFERRED, NearbyBlock, WorldState,
)
from minecraft.task_runner import Step                              # noqa: E402

from tests.test_minecraft_controller import FakeLocator, FakeProcess  # noqa: E402
from tests.test_minecraft_navigation import TreeWorld, flat, run    # noqa: E402

LOG = NearbyBlock(2, 64, 0, "oak_log", True)
"""Two blocks east of a player standing at (0.5, 64, 0.5): within reach."""


def facing_away(world):
    """Point the simulated player well off the log, so aiming has work to do."""
    world.yaw = 0.0          # south, +Z; the log is east, +X
    world.pitch = 0.0
    return world


class AimAtBlockTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()
        nav.reset_calibration()

    def test_it_turns_until_the_game_confirms_the_exact_block(self):
        world = facing_away(TreeWorld(flat(), [LOG]))
        skill = skills.create("aim_at_block", x=2, y=64, z=0)
        result = run(world, skill, max_steps=20)

        self.assertFalse(skill.failed, skill.done_reason)
        hit = world.crosshair()
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], (2, 64, 0),
                         "it reported success with the crosshair elsewhere")
        self.assertIn("confirmed", skill.done_reason)
        looks = [r for r in result.records if r.step["action"] == "look"]
        self.assertTrue(looks, "the player was facing away; it had to turn")
        self.assertEqual(world.swings, 0, "aim_at_block must never mine")

    def test_mine_block_breaks_that_block_and_proves_it_by_coordinate(self):
        world = facing_away(TreeWorld(flat(), [LOG]))
        skill = skills.create("mine_block", x=2, y=64, z=0)
        result = run(world, skill, max_steps=20)

        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual([b.position for b in world.broken], [(2, 64, 0)])
        self.assertEqual(world.wrong_block_swings, 0)
        mines = [r for r in result.records if r.step["action"] == "mine"]
        self.assertEqual(len(mines), 1)
        self.assertEqual(mines[0].step["params"]["expect_at"], [2, 64, 0],
                         "the mine step must carry the coordinate so the "
                         "controller can check it at the moment it presses")
        self.assertEqual(mines[0].verification["status"], verify_mod.SUCCESS)

    def test_a_hidden_target_is_never_mined(self):
        """A leaf between the player and the log. The angle converges on the
        log; the crosshair lands on the leaf. That used to be good enough to
        swing. It must now end the task with nothing mined."""
        leaf = NearbyBlock(1, 65, 0, "oak_leaves", True)
        # The log is at eye level, and the leaf sits on the ray to it.
        log = NearbyBlock(2, 65, 0, "oak_log", True)
        world = facing_away(TreeWorld(flat(), [log], blocks=[leaf]))
        skill = skills.create("mine_block", x=2, y=65, z=0)
        run(world, skill, max_steps=25)

        self.assertTrue(skill.failed)
        self.assertEqual(world.swings, 0,
                         "it mined without the crosshair confirmed on the log")
        self.assertIn("mined nothing", skill.done_reason)
        self.assertEqual(world.broken, [])

    def test_an_unreadable_crosshair_stops_before_any_input(self):
        class Blind(TreeWorld):
            def read(self):
                state = TreeWorld.read(self)
                return WorldState(position=state.position,
                                  rotation=state.rotation,
                                  surface=state.surface, source="test",
                                  confidence=EXACT)

        world = facing_away(Blind(flat(), [LOG]))
        skill = skills.create("mine_block", x=2, y=64, z=0)
        result = run(world, skill, max_steps=10)
        self.assertEqual(result.records, (),
                         "it acted without being able to see the crosshair")
        self.assertTrue(skill.failed)
        self.assertIn("cannot read what the crosshair is on",
                      skill.done_reason)

    def test_mining_needs_the_bridge_not_an_ocr_reading(self):
        """F3 OCR can say what is under the crosshair; it is not the game's
        own confirmation, so it may aim by it but not mine by it."""
        state = WorldState(
            position=(0.5, 64.0, 0.5), rotation=(-90.0, 20.0),
            target_block=BlockRef(x=2, y=64, z=0, name="oak_log"),
            source="f3-overlay", confidence=INFERRED)
        skill = skills.create("mine_block", x=2, y=64, z=0)
        self.assertIsNone(skill.plan(state, 0, ()))
        self.assertIn("not from the bridge", skill.done_reason)

    def test_the_wrong_kind_of_block_is_not_mined(self):
        world = facing_away(TreeWorld(flat(), [LOG]))
        skill = skills.create("mine_block", x=2, y=64, z=0,
                              expected="birch_log")
        run(world, skill, max_steps=20)
        self.assertEqual(world.swings, 0)
        self.assertIn("not birch_log", skill.done_reason)

    def test_out_of_reach_is_refused_without_turning(self):
        far = NearbyBlock(7, 64, 0, "oak_log", True)
        world = facing_away(TreeWorld(flat(), [far]))
        skill = skills.create("mine_block", x=7, y=64, z=0)
        result = run(world, skill, max_steps=10)
        self.assertEqual(result.records, ())
        self.assertIn("out of reach", skill.done_reason)


class CollectLogsKeepsCorrectingTests(unittest.TestCase):
    """Within the angular tolerance but on the wrong block used to mean
    "give up on this log". It must mean "correct again"."""

    def test_small_angle_wrong_block_asks_for_another_correction(self):
        aiming_mod.SHARED.reset()
        log = NearbyBlock(2, 64, 0, "oak_log", True)
        # Looking almost straight at the point the skill itself aims for.
        position = (0.5, 64.0, 0.5)
        point = aiming_mod.target_point(log.position, position)
        yaw = aiming_mod.yaw_to(position, point) + 3.0
        pitch = aiming_mod.pitch_to(position, point)
        state = WorldState(
            position=position, rotation=(yaw, pitch),
            surface=tuple(flat()), notable_blocks=(log,), scan_radius=8,
            # ...but the crosshair is on the grass beside it.
            target_block=BlockRef(x=1, y=63, z=1, name="grass_block",
                                  face="up"),
            source="test", confidence=EXACT)
        skill = skills.create("collect_logs", count=1)
        _dx, _dy, error = nav.aim_at(position, (yaw, pitch), log.position)
        self.assertLess(error, skill.aim_tolerance_deg,
                        "the scenario must be inside the old tolerance")

        step = skill.plan(state, 0, ())
        self.assertIsInstance(step, Step)
        self.assertEqual(step.action, "look",
                         "inside the angular tolerance with the crosshair "
                         "off the log, it must correct again, not stop")

    def test_a_mine_step_is_pinned_to_the_log_under_the_crosshair(self):
        log = NearbyBlock(2, 64, 0, "oak_log", True)
        state = WorldState(
            position=(0.5, 64.0, 0.5), rotation=(-90.0, 10.0),
            surface=tuple(flat()), notable_blocks=(log,), scan_radius=8,
            target_block=BlockRef(x=2, y=64, z=0, name="oak_log",
                                  face="west"),
            source="test", confidence=EXACT)
        step = skills.create("collect_logs", count=1).plan(state, 0, ())
        self.assertEqual(step.action, "mine")
        self.assertEqual(step.params["expect_at"], [2, 64, 0])


class ControllerTargetPreconditionTests(unittest.TestCase):
    """The last check before the button goes down."""

    def controller(self, probe):
        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0, progress_probe=probe)
        controller.start_session(duration_s=0)
        return controller, backend

    def test_a_mismatch_presses_nothing(self):
        controller, backend = self.controller(
            lambda: ("oak_leaves", 1, 65, 0))
        result = controller.mine({"duration": 0.2, "expect_at": [2, 64, 0]})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])
        self.assertIn("oak_leaves", result.error)

    def test_an_unreadable_crosshair_presses_nothing(self):
        controller, backend = self.controller(lambda: None)
        result = controller.mine({"duration": 0.2, "expect_at": [2, 64, 0]})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])

    def test_air_under_the_crosshair_presses_nothing(self):
        controller, backend = self.controller(
            lambda: ("air", None, None, None))
        result = controller.mine({"duration": 0.2, "expect_at": [2, 64, 0]})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])

    def test_a_match_mines_and_releases(self):
        controller, backend = self.controller(lambda: ("oak_log", 2, 64, 0))
        result = controller.mine({"duration": 0.12, "expect_at": [2, 64, 0]})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["left"])
        self.assertEqual(backend.button_ups(), ["left"])
        self.assertEqual(backend.held, set())

    def test_without_expect_at_mining_behaves_as_before(self):
        controller, backend = self.controller(lambda: ("oak_log", 2, 64, 0))
        result = controller.mine({"duration": 0.12})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["left"])

    def test_a_malformed_expect_at_is_refused_not_ignored(self):
        for bad in ([1, 2], "2,64,0", [1, 2, "x"], 5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAction):
                    parse_mine({"expect_at": bad})

    def test_the_other_forbidden_mining_keys_are_still_refused(self):
        for key in ("target", "block", "at", "position", "until"):
            with self.subTest(key=key):
                with self.assertRaises(InvalidAction):
                    parse_mine({key: [1, 2, 3]})


class BrokeBlockAtMovementTests(unittest.TestCase):
    """The scan lists a capped set of blocks in scan order from the player's
    own block; after the player moves, a coordinate can drop out without
    breaking. That must not read as a break."""

    def state(self, position, notable, target=None, rotation=(0.0, 0.0)):
        return WorldState(position=position, rotation=rotation,
                          notable_blocks=tuple(notable),
                          target_block=target or BlockRef(name="air"),
                          source="test", confidence=EXACT)

    def test_absence_after_the_player_moved_is_not_a_break(self):
        log = NearbyBlock(5, 64, 0, "oak_log", True)
        before = self.state((0.5, 64.0, 0.5), [log])
        after = self.state((2.5, 64.0, 0.5), [])
        verdict = verify_mod.broke_block_at((5, 64, 0), "oak_log").check(
            before, after, delivered=True)
        self.assertNotEqual(verdict.status, verify_mod.SUCCESS,
                            verdict.reason)

    def test_absence_with_the_player_still_is_a_break(self):
        log = NearbyBlock(5, 64, 0, "oak_log", True)
        before = self.state((0.5, 64.0, 0.5), [log])
        after = self.state((0.6, 64.0, 0.4), [])
        verdict = verify_mod.broke_block_at((5, 64, 0), "oak_log").check(
            before, after, delivered=True)
        self.assertEqual(verdict.status, verify_mod.SUCCESS, verdict.reason)


class MoveAndJumpThroughTheToolTests(unittest.TestCase):
    """It was advertised to the model and refused when the model called it."""

    def setUp(self):
        import actions.minecraft as adapter
        self.adapter = adapter
        self.backend = FakeInputBackend()
        self.controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0)
        adapter._reset_for_tests(controller=self.controller)

    def tearDown(self):
        self.adapter._reset_for_tests()

    def test_it_needs_a_session(self):
        answer = self.adapter.minecraft_control(
            {"action": "move_and_jump", "direction": "forward"})
        self.assertEqual(self.backend.downs(), [])
        self.assertIn("session", answer.lower())

    def test_it_presses_exactly_forward_and_jump_and_releases_them(self):
        self.controller.start_session(duration_s=0)
        answer = self.adapter.minecraft_control(
            {"action": "move_and_jump", "direction": "forward",
             "duration": 0.1})
        self.assertIn("move_and_jump: done", answer)
        self.assertEqual(sorted(self.backend.downs()), ["space", "w"])
        self.assertEqual(sorted(self.backend.ups()), ["space", "w"])
        self.assertEqual(self.backend.held, set())

    def test_it_is_bounded_to_one_second(self):
        self.controller.start_session(duration_s=0)
        result = self.controller.move_and_jump(
            {"direction": "forward", "duration": 30})
        self.assertTrue(result.clamped)
        self.assertLessEqual(result.requested["duration"], 1.0)

    def test_it_is_in_the_controllers_own_vocabulary(self):
        self.controller.start_session(duration_s=0)
        result = self.controller.execute_action("move_and_jump",
                                                {"duration": 0.05})
        self.assertNotEqual(result.error_class, "InvalidAction", result.error)


class ToolDescriptionTests(unittest.TestCase):
    """What the model is told must match the code."""

    def setUp(self):
        import actions.minecraft as adapter
        self.text = adapter.TOOL["description"]
        self.properties = adapter.TOOL["parameters"]["properties"]

    def test_stale_limits_are_gone(self):
        everything = self.text + str(self.properties)
        for stale in ("limit of 20", "above 20", "up to 2.0 for every action",
                      "I cannot read the inventory", "One mine does NOT",
                      "degrees are NOT supported"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, everything)

    def test_the_real_limits_are_stated(self):
        from minecraft import action_spec
        from minecraft.task_runner import MAX_TASK_STEPS
        self.assertIn(str(MAX_TASK_STEPS), self.properties["max_steps"]
                      ["description"])
        self.assertIn(f"{action_spec.MAX_MINE_DURATION_S:g}", self.text)
        self.assertIn("mine 10.0", self.properties["duration"]["description"])

    def test_the_new_actions_and_tasks_are_listed(self):
        for word in ("move_and_jump", "aim_at_block", "mine_block",
                     "cancel_task", "task_status"):
            with self.subTest(word=word):
                self.assertIn(word, self.text + str(self.properties))
        self.assertIn("y", self.properties)


if __name__ == "__main__":
    unittest.main(verbosity=2)
