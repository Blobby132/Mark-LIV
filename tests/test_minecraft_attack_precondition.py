"""
B5b (1/2): `attack` with `expect_hostile` presses nothing unless the game
reports a hostile mob under the crosshair -- never a player, never a
passive mob, never nothing.

The bridge's target_entity gains `category` (additive under schema /4),
the controller an injected `entity_probe` -- like progress_probe and
held_item_probe -- and the check runs at the last moment before the
button goes down. Without the precondition, attack is what it was: the
model's own `attack` hits whatever is under the crosshair, as before.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft.action_spec import InvalidAction, parse_attack         # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.state import EntityRef, WorldState                      # noqa: E402
from test_minecraft_controller import FakeLocator, FakeProcess         # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "fabric-mod" / "src" / \
    "main" / "java" / "com" / "markliv" / "bridge" / "MarkLivBridge.java"


class AttackPreconditionTests(unittest.TestCase):

    def controller(self, probe):
        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0, entity_probe=probe)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        return controller, backend

    def test_a_hostile_mob_is_attacked(self):
        controller, backend = self.controller(lambda: ("zombie", "hostile"))
        result = controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["left"])
        self.assertEqual(backend.held, set())

    def test_a_player_is_never_attacked(self):
        controller, backend = self.controller(lambda: ("Steve", "player"))
        result = controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])
        self.assertIn("player", result.error)

    def test_a_passive_mob_is_never_attacked(self):
        controller, backend = self.controller(lambda: ("cow", "passive"))
        result = controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertEqual(backend.button_downs(), [])
        self.assertIn("cow", result.error)

    def test_nothing_under_the_crosshair_presses_nothing(self):
        controller, backend = self.controller(lambda: None)
        result = controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])

    def test_an_unknown_category_presses_nothing(self):
        controller, backend = self.controller(lambda: ("zombie", None))
        controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertEqual(backend.button_downs(), [])

    def test_no_probe_presses_nothing(self):
        controller, backend = self.controller(None)
        result = controller.attack({"duration": 0.1, "expect_hostile": True})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])

    def test_without_asking_the_precondition_is_on_anyway(self):
        """Item 1 made it the default for every attack (it used to be
        fight's alone, and a plain attack hit the cow)."""
        controller, backend = self.controller(lambda: ("cow", "passive"))
        result = controller.attack({"duration": 0.1})
        self.assertFalse(result.ok)
        self.assertEqual(backend.button_downs(), [])


class ParseTests(unittest.TestCase):

    def test_it_is_carried(self):
        spec = parse_attack({"duration": 0.1, "expect_hostile": True})
        self.assertTrue(spec.expect_hostile)
        self.assertTrue(spec.as_dict()["expect_hostile"])

    def test_only_true_is_a_precondition(self):
        for bad in (False, "yes", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidAction):
                    parse_attack({"expect_hostile": bad})

    def test_the_old_refusals_stand(self):
        for key in ("target", "block", "entity", "at", "position"):
            with self.assertRaises(InvalidAction):
                parse_attack({key: 1})


class ProbeTests(unittest.TestCase):

    def test_the_probe_reports_name_and_category(self):
        from actions import minecraft as mc_actions

        class Source:
            def read(self):
                return WorldState(target_entity=EntityRef(
                    name="zombie", category="hostile", hostile=True,
                    distance=2.0))
        with mock.patch.object(mc_actions, "_get_state_source",
                               return_value=Source()):
            self.assertEqual(mc_actions._entity_probe(),
                             ("zombie", "hostile"))

    def test_the_controller_gets_it(self):
        from actions import minecraft as mc_actions
        with mock.patch.object(mc_actions, "_controller", None), \
                mock.patch.object(mc_actions, "MinecraftController") as made:
            mc_actions._get_controller()
        self.assertIs(made.call_args.kwargs["entity_probe"],
                      mc_actions._entity_probe)

    def test_the_mod_reports_the_category_of_the_target(self):
        source = SRC.read_text()
        start = source.index("private String targetEntityJson")
        body = source[start:source.index("private String nearbyJson")]
        self.assertIn('"category"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
