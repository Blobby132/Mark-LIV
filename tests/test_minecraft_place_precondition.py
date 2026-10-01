"""
B4a (1/2): `place` presses nothing until the crosshair is on the block AND
the face the new block should go against.

A right-click places the held block against the face under the crosshair:
the same block, a different face, is a different cell. So place takes the
same kind of precondition mine has -- `expect_at` [x, y, z] -- plus
`expect_face`, and the controller checks both from the progress probe at
the last moment before the button goes down. A precondition can only make
a place refuse, never send it anywhere else. The A4 deny-list still runs.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft.action_spec import InvalidAction, parse_place          # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.state import BlockRef, WorldState                       # noqa: E402
from test_minecraft_controller import FakeLocator, FakeProcess         # noqa: E402


class PlacePreconditionTests(unittest.TestCase):

    def controller(self, probe, held="cobblestone"):
        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0, progress_probe=probe,
            held_item_probe=lambda: held)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        return controller, backend

    def test_the_right_block_and_face_places(self):
        controller, backend = self.controller(
            lambda: ("grass_block", 1, 63, 0, "up"))
        result = controller.place({"expect_at": [1, 63, 0],
                                   "expect_face": "up"})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["right"])
        self.assertEqual(backend.held, set())

    def test_the_wrong_face_presses_nothing(self):
        controller, backend = self.controller(
            lambda: ("grass_block", 1, 63, 0, "east"))
        result = controller.place({"expect_at": [1, 63, 0],
                                   "expect_face": "up"})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])
        self.assertIn("east", result.error)

    def test_the_wrong_block_presses_nothing(self):
        controller, backend = self.controller(
            lambda: ("grass_block", 2, 63, 0, "up"))
        result = controller.place({"expect_at": [1, 63, 0],
                                   "expect_face": "up"})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])

    def test_a_reading_without_a_face_presses_nothing(self):
        controller, backend = self.controller(
            lambda: ("grass_block", 1, 63, 0))
        result = controller.place({"expect_at": [1, 63, 0],
                                   "expect_face": "up"})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])
        self.assertIn("face", result.error)

    def test_a_coordinate_alone_still_checks_the_block(self):
        controller, backend = self.controller(
            lambda: ("short_grass", 1, 64, 0, "north"))
        result = controller.place({"expect_at": [1, 64, 0]})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["right"])

    def test_the_held_item_is_still_checked_first(self):
        controller, backend = self.controller(
            lambda: ("grass_block", 1, 63, 0, "up"), held="lava_bucket")
        result = controller.place({"expect_at": [1, 63, 0],
                                   "expect_face": "up"})
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertEqual(backend.button_downs(), [])

    def test_without_a_precondition_place_behaves_as_before(self):
        controller, backend = self.controller(lambda: None)
        result = controller.place({})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["right"])

    def test_mining_still_reads_four_or_five(self):
        controller, backend = self.controller(
            lambda: ("oak_log", 2, 64, 0, "west"))
        result = controller.mine({"duration": 0.12, "expect_at": [2, 64, 0]})
        self.assertTrue(result.ok, result.error)


class ParseTests(unittest.TestCase):

    def test_both_are_carried(self):
        spec = parse_place({"expect_at": [1, 63, 0], "expect_face": "UP"})
        self.assertEqual(spec.expect_target, (1, 63, 0))
        self.assertEqual(spec.as_dict()["expect_face"], "up")

    def test_a_face_that_does_not_exist_is_refused(self):
        with self.assertRaises(InvalidAction):
            parse_place({"expect_at": [1, 63, 0], "expect_face": "sideways"})

    def test_a_face_without_a_block_is_refused(self):
        with self.assertRaises(InvalidAction):
            parse_place({"expect_face": "up"})

    def test_a_malformed_coordinate_is_refused(self):
        with self.assertRaises(InvalidAction):
            parse_place({"expect_at": [1, 2]})

    def test_the_old_refusals_stand(self):
        for key in ("duration", "count", "times", "block", "item"):
            with self.assertRaises(InvalidAction):
                parse_place({key: 1})


class ProbeTests(unittest.TestCase):

    def test_the_probe_reports_the_face(self):
        from actions import minecraft as mc_actions

        class Source:
            def read(self):
                return WorldState(target_block=BlockRef(
                    name="stone", x=1, y=63, z=0, face="up"))
        with mock.patch.object(mc_actions, "_get_state_source",
                               return_value=Source()):
            self.assertEqual(mc_actions._target_probe(),
                             ("stone", 1, 63, 0, "up"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
