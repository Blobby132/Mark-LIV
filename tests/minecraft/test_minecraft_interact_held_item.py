"""
Item 3: `interact` right-clicks with whatever is held -- the same button
`place` uses -- and skipped the deny-list `place` has (A4). Reproduced:
with a lava bucket, flint and steel, TNT, a spawn egg or an ender pearl in
hand, interact pressed.

Now interact refuses what place refuses, except that an empty hand is
fine: an empty hand is how doors and chests are opened. A hand it cannot
see is refused. `use_item` stays the explicit way to use such items, but
pouring a lava, water or powder-snow bucket and starting a fire need the
item named (`expect_item`) -- by the user, through the model.

craft_item opens a crafting table with interact, and after chopping the
hand is often an axe: it now selects a safe slot first, and puts the old
one back when it is done.
"""

from __future__ import annotations

import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft import action_spec                                      # noqa: E402
from minecraft.action_spec import InvalidAction                        # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from tests.support.fakes import FakeLocator, FakeProcess  # noqa: E402
from tests.support.held_items import DENIED  # noqa: E402


def controller(held):
    backend = FakeInputBackend()
    c = MinecraftController(
        backend=backend, locator=FakeLocator(), process_module=FakeProcess(),
        start_watchers=False, focus_wait_s=0, held_item_probe=lambda: held)
    c.start_session(duration_s=0)
    return c, backend


class InteractTests(unittest.TestCase):

    def test_everything_place_refuses_interact_refuses(self):
        for name in DENIED:
            with self.subTest(item=name):
                c, backend = controller(name)
                result = c.interact({})
                c.stop("test")
                self.assertEqual(result.stopped_reason, "held_item")
                self.assertEqual(backend.events, [], f"{name} was used")
                self.assertIn(" ".join(name.split("_")), result.error)

    def test_an_empty_hand_opens_doors(self):
        for held in ("", "air"):
            c, backend = controller(held)
            result = c.interact({})
            c.stop("test")
            self.assertTrue(result.ok, result.error)
            self.assertEqual(backend.button_downs(), ["right"])

    def test_a_block_or_food_is_fine(self):
        for held in ("cobblestone", "bread", "stone_pickaxe", "iron_sword"):
            c, _backend = controller(held)
            result = c.interact({})
            c.stop("test")
            self.assertTrue(result.ok, f"{held}: {result.error}")

    def test_a_hand_it_cannot_see_is_refused(self):
        c, backend = controller(None)
        result = c.interact({})
        c.stop("test")
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertEqual(backend.events, [])


class UseItemTests(unittest.TestCase):

    def test_pouring_or_lighting_needs_the_item_named(self):
        for name in ("lava_bucket", "water_bucket", "powder_snow_bucket",
                     "flint_and_steel", "fire_charge"):
            with self.subTest(item=name):
                c, backend = controller(name)
                refused = c.use_item({"duration": 0.05})
                self.assertEqual(refused.stopped_reason, "held_item")
                self.assertIn("expect_item", refused.error)
                self.assertEqual(backend.events, [])
                named = c.use_item({"duration": 0.05, "expect_item": name})
                c.stop("test")
                self.assertTrue(named.ok, named.error)
                self.assertEqual(backend.button_downs(), ["right"])

    def test_naming_the_wrong_item_is_refused(self):
        c, backend = controller("lava_bucket")
        result = c.use_item({"duration": 0.05, "expect_item": "water_bucket"})
        c.stop("test")
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertIn("lava bucket", result.error)
        self.assertEqual(backend.events, [])

    def test_a_named_item_with_an_unseen_hand_is_refused(self):
        c, backend = controller(None)
        result = c.use_item({"duration": 0.05, "expect_item": "lava_bucket"})
        c.stop("test")
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertEqual(backend.events, [])

    def test_everything_else_is_used_as_before(self):
        for held in ("bow", "shield", "bread", None):
            c, _backend = controller(held)
            result = c.use_item({"duration": 0.05})
            c.stop("test")
            self.assertTrue(result.ok, f"{held}: {result.error}")

    def test_expect_item_is_checked_not_a_selector(self):
        spec = action_spec.parse_use_item({"expect_item":
                                           "minecraft:Lava_Bucket"})
        self.assertEqual(spec.as_dict()["expect_item"], "lava_bucket")
        with self.assertRaises(InvalidAction):
            action_spec.parse_use_item({"expect_item": ""})
        with self.assertRaises(InvalidAction):
            action_spec.parse_use_item({"item": "dirt"})


class CraftWithAnAxeInHandTests(unittest.TestCase):

    def test_it_selects_a_safe_slot_to_open_the_table_and_puts_it_back(self):
        from minecraft import aiming as aiming_mod, navigation as nav, skills
        from minecraft.task_runner import TaskRunner
        from tests.support.gui_world import GuiWorld
        world = GuiWorld({0: ("stone_axe", 1), 1: ("oak_planks", 5),
                          2: ("stick", 4)},
                         table=(1, 64, 0), yaw=-90.0)
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create("craft_item", item="wooden_pickaxe")
        result = TaskRunner(world, world, sleeper=lambda _s: None).run(
            skill, max_steps=45)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.count("wooden_pickaxe"), 1)
        self.assertEqual(world.used_with, [], "the axe was right-clicked")
        self.assertEqual(world.selected, 0, "the axe's slot was not put back")

    def test_the_slot_is_put_back_when_the_table_will_not_open(self):
        from minecraft import aiming as aiming_mod, navigation as nav, skills
        from minecraft.task_runner import TaskRunner
        from tests.support.gui_world import GuiWorld
        world = GuiWorld({0: ("stone_axe", 1), 1: ("oak_planks", 5),
                          2: ("stick", 4)},
                         table=(1, 64, 0), yaw=-90.0)
        original = world.interact

        def stuck_table(params):
            answer = original(params)
            world.screen = None                  # it never opens
            return answer
        world.interact = stuck_table
        nav.reset_calibration()
        aiming_mod.SHARED.reset()
        skill = skills.create("craft_item", item="wooden_pickaxe")
        TaskRunner(world, world, sleeper=lambda _s: None).run(skill,
                                                              max_steps=45)
        self.assertTrue(skill.failed)
        self.assertEqual(world.selected, 0, "the axe's slot was not put back")


class ConsentWordingTests(unittest.TestCase):
    """interact and use_item got narrower: the user reads it where they
    read what the confirmation covers."""

    def test_the_four_places(self):
        from actions import minecraft as mc_actions
        from minecraft import skills
        from minecraft.session import GRANT_SUMMARY
        self.assertIn("only when you name it", GRANT_SUMMARY)
        detail = mc_actions._mc_guard({"action": "start_session"})["detail"]
        self.assertIn("only when you name the item", detail)
        readme = (ROOT / "docs" / "minecraft" / "README.md").read_text(
            encoding="utf-8")
        start = readme.index("**What it deliberately cannot do.**")
        self.assertIn("expect_item", readme[start:start + 3000])
        self.assertIn("use_dangerous_items_unasked", skills.NOT_YET_POSSIBLE)
        self.assertIn("expect_item",
                      mc_actions.TOOL["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
