"""
Item 1: every attack is hostile-only. Raw `attack` pressed whatever was
under the crosshair -- a player, a pet, a villager, a cow. Now the check
B5b added for fight (`expect_hostile`) is the default for every attack:
the controller presses nothing unless the injected entity probe reports a
mob the game calls hostile, at the last moment before the button goes down.

There is no opt-out. Nothing needs one: blocks are broken with `mine`, the
only task that attacks (fight) wants hostiles, and hunting animals is not
built. An opt-out could never cover players anyway.
"""

from __future__ import annotations

import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft.action_spec import InvalidAction, parse_attack         # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from tests.support.fakes import FakeLocator, FakeProcess  # noqa: E402


class DefaultTests(unittest.TestCase):

    def controller(self, probe):
        backend = FakeInputBackend()
        controller = MinecraftController(
            backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0, entity_probe=probe)
        controller.start_session(duration_s=0)
        self.addCleanup(controller.stop, "test")
        return controller, backend

    def assertRefused(self, seen, *words):
        controller, backend = self.controller(lambda: seen)
        result = controller.attack({"duration": 0.1})
        self.assertFalse(result.ok, f"{seen} was attacked")
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [], "something was pressed")
        for word in words:
            self.assertIn(word, result.error)

    def test_a_player_is_refused(self):
        self.assertRefused(("Steve", "player"), "Steve", "player")

    def test_a_pet_wolf_is_refused(self):
        self.assertRefused(("wolf", "passive"), "wolf")

    def test_a_villager_is_refused(self):
        # Villagers are MobCategory MISC: the bridge sends "misc".
        self.assertRefused(("villager", "misc"), "villager")

    def test_a_cow_is_refused(self):
        self.assertRefused(("cow", "passive"), "cow")

    def test_a_block_or_nothing_is_refused(self):
        self.assertRefused(None, "not on a mob")

    def test_a_zombie_is_attacked(self):
        controller, backend = self.controller(lambda: ("zombie", "hostile"))
        result = controller.attack({"duration": 0.1})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(backend.button_downs(), ["left"])
        self.assertEqual(backend.held, set())

    def test_no_entity_probe_means_refuse(self):
        controller, backend = self.controller(None)
        result = controller.attack({"duration": 0.1})
        self.assertEqual(result.stopped_reason, "target_not_confirmed")
        self.assertEqual(backend.button_downs(), [])


class NoOptOutTests(unittest.TestCase):

    def test_the_check_is_on_without_asking(self):
        self.assertTrue(parse_attack({"duration": 0.1}).expect_hostile)

    def test_it_cannot_be_turned_off(self):
        for value in (False, 0, "no", None):
            with self.subTest(value=value):
                with self.assertRaises(InvalidAction):
                    parse_attack({"expect_hostile": value})

    def test_asking_for_it_explicitly_still_works(self):
        self.assertTrue(parse_attack({"expect_hostile": True}).expect_hostile)


class ConsentWordingTests(unittest.TestCase):
    """What one confirmation covers got narrower: say so where the user
    reads it."""

    def test_the_grant_says_hostile_mobs(self):
        from minecraft.session import GRANT_SUMMARY
        self.assertIn("attack hostile mobs", GRANT_SUMMARY)

    def test_the_banner_says_never_a_player(self):
        from actions import minecraft as mc_actions
        detail = mc_actions._mc_guard({"action": "start_session"})["detail"]
        self.assertIn("never attack a player", detail)

    def test_the_readme_and_the_tool_say_so(self):
        readme = (ROOT / "docs" / "minecraft" / "README.md").read_text(
            encoding="utf-8")
        start = readme.index("**What it deliberately cannot do.**")
        self.assertIn("Hit a player", readme[start:start + 2000])
        from actions import minecraft as mc_actions
        self.assertIn("ONLY a hostile mob", mc_actions.TOOL["description"])

    def test_not_yet_possible_names_it(self):
        from minecraft import skills
        self.assertIn("attack_players_or_animals", skills.NOT_YET_POSSIBLE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
