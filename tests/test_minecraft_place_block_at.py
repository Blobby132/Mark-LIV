"""
B4a (2/2): place_block_at(x, y, z, item) -- one block into one exact cell.

It refuses a cell that holds a block or that the scan never saw empty,
places against a solid, non-interactive neighbour it knows of, stands in
reach and out of the cell, presses nothing until the game reports the
crosshair on that neighbour's face, and proves the result twice: the
crosshair on a block of that name at the cell, and the held stack one
smaller. Run end to end in tests/build_world.py.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import building                                      # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.state import (                                       # noqa: E402
    EXACT, NearbyBlock, WorldState)
from minecraft.task_runner import TaskRunner                        # noqa: E402
from tests.support.build_world import BuildWorld  # noqa: E402


def place(world, x, y, z, item="cobblestone"):
    nav.reset_calibration()
    aiming_mod.SHARED.reset()
    skill = skills.create("place_block_at", x=x, y=y, z=z, item=item)
    result = TaskRunner(world, world, sleeper=lambda _s: None).run(
        skill, max_steps=45)
    return skill, result


class PlacingTests(unittest.TestCase):

    def test_on_the_ground_beside_the_player(self):
        world = BuildWorld({0: ("cobblestone", 5)})
        skill, result = place(world, 2, 64, 0)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.placed, [((2, 64, 0), "cobblestone")])
        self.assertEqual(world.count("cobblestone"), 4)
        self.assertEqual(world.not_confirmed, 0)
        self.assertIn("5 to 4", skill.done_reason)

    def test_it_will_not_place_into_its_own_body(self):
        """The cell the player stands in: it steps out of it first."""
        world = BuildWorld({0: ("cobblestone", 5)})
        skill, result = place(world, 0, 64, 0)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.placed, [((0, 64, 0), "cobblestone")])
        self.assertFalse(building.body_overlaps(
            (world.x, world.y, world.z), (0, 64, 0)))

    def test_against_the_side_of_a_block(self):
        """Nothing below the cell -- a pillar beside it is the only thing
        to place against, on its south face."""
        world = BuildWorld({0: ("cobblestone", 5)},
                           placed=(((2, 64, 0), "cobblestone"),
                                   ((2, 65, 0), "cobblestone")))
        skill, result = place(world, 2, 65, 1)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.placed, [((2, 65, 1), "cobblestone")])
        self.assertIn("south", skill.done_reason)

    def test_the_cell_is_looked_at_again_just_before_pressing(self):
        """A berry bush appears under the cell while it aims. The scan
        cannot say which of the two blocks above the floor the bush is in,
        so the cell is no longer known to be free: nothing is pressed. (A
        jar without near_blocks: with it, the bush is known to be in the
        other cell, and placing is right.)"""
        world = BuildWorld({0: ("cobblestone", 5)},
                           placed=(((2, 64, 0), "cobblestone"),
                                   ((2, 65, 0), "cobblestone")))
        world.report_near = False
        original = world.look

        def look_then_bush(params):
            answer = original(params)
            world.blocks[(2, 64, 1)] = NearbyBlock(2, 64, 1,
                                                   "sweet_berry_bush", False)
            return answer
        world.look = look_then_bush
        skill, _ = place(world, 2, 65, 1)
        self.assertTrue(skill.failed)
        self.assertEqual(world.presses, 0)
        self.assertIn("no longer looks empty", skill.done_reason)

    def test_something_the_map_does_not_show_in_the_way(self):
        """Aimed dead on, and the crosshair is on something else: it tries
        once more from another spot rather than give up."""
        world = BuildWorld({0: ("cobblestone", 5)},
                           hidden=(((1, 65, 0), "stone"),))
        skill, result = place(world, 2, 64, 0)
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.placed, [((2, 64, 0), "cobblestone")])

    def test_into_grass_the_game_replaces(self):
        world = BuildWorld({0: ("dirt", 3)},
                           plants=(((2, 64, 0), "short_grass"),))
        skill, result = place(world, 2, 64, 0, item="dirt")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.placed, [((2, 64, 0), "dirt")])

    def test_from_the_main_inventory(self):
        world = BuildWorld({0: ("stone_pickaxe", 1), 20: ("oak_planks", 8)})
        skill, result = place(world, 2, 64, 0, item="oak_planks")
        self.assertFalse(skill.failed, f"{skill.done_reason} / {result.reason}")
        self.assertEqual(world.swaps, 1)
        self.assertEqual(world.placed, [((2, 64, 0), "oak_planks")])
        self.assertEqual(world.selected, 0, "the held slot was not put back")
        self.assertIsNone(world.screen)

    def test_creative_says_only_the_crosshair_proves_it(self):
        world = BuildWorld({0: ("cobblestone", 5)}, mode="creative")
        skill, _ = place(world, 2, 64, 0)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.count("cobblestone"), 5)
        self.assertIn("creative", skill.done_reason)

    def test_the_slot_is_put_back(self):
        world = BuildWorld({0: ("stone_pickaxe", 1), 4: ("cobblestone", 5)})
        skill, _ = place(world, 2, 64, 0)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.selected, 0)

    def test_a_block_the_stack_does_not_account_for_is_not_a_success(self):
        """The crosshair shows the block, but the stack did not go down --
        not proven, and said so."""
        world = BuildWorld({0: ("cobblestone", 5)})
        original = world.place

        def free_block(params):
            answer = original(params)
            if world.placed:
                world.stacks[0][1] = 5
            return answer
        world.place = free_block
        skill, _ = place(world, 2, 64, 0)
        self.assertTrue(skill.failed)
        self.assertIn("not down by one", skill.done_reason)

    def test_an_unproven_press_is_never_repeated(self):
        """Something stopped the block landing (in the game: a mob standing
        in the cell). One press, a report, no second press."""
        world = BuildWorld({0: ("cobblestone", 5)})

        def nothing_lands(params):
            world.presses += 1
            return world._done("place", params)
        world.place = nothing_lands
        skill, _ = place(world, 2, 64, 0)
        self.assertTrue(skill.failed)
        self.assertEqual(world.presses, 1)
        self.assertIn("did not press again", skill.done_reason)


class RefusalTests(unittest.TestCase):

    def assertRefused(self, world, cell, item, *words):
        skill, result = place(world, *cell, item=item)
        self.assertTrue(skill.failed)
        self.assertEqual(result.steps_taken, 0, "refused only after acting")
        self.assertEqual(world.presses, 0, "it pressed place")
        self.assertEqual(world.placed, [])
        for word in words:
            self.assertIn(word, skill.done_reason)
        return skill

    def test_a_cell_holding_a_block(self):
        self.assertRefused(BuildWorld({0: ("cobblestone", 5)}), (2, 63, 0),
                           "cobblestone", "grass block")

    def test_a_cell_nobody_saw_empty(self):
        self.assertRefused(BuildWorld({0: ("cobblestone", 5)}), (2, 70, 0),
                           "cobblestone", "cannot see")

    def test_a_cell_with_nothing_to_place_against(self):
        """Floating in the air: no known solid neighbour."""
        self.assertRefused(BuildWorld({0: ("cobblestone", 5)}), (2, 66, 0),
                           "cobblestone", "against")

    def test_only_a_chest_to_place_against(self):
        world = BuildWorld({0: ("cobblestone", 5)},
                           placed=(((2, 64, 0), "chest"),))
        self.assertRefused(world, (2, 65, 0), "cobblestone", "chest")

    def test_an_item_that_is_not_a_building_block(self):
        for item in ("torch", "sand", "lava_bucket", "crafting_table"):
            with self.subTest(item=item):
                self.assertRefused(BuildWorld({0: (item, 5)}), (2, 64, 0),
                                   item, "build with")

    def test_none_of_the_item(self):
        self.assertRefused(BuildWorld({0: ("dirt", 5)}), (2, 64, 0),
                           "cobblestone", "no cobblestone")


class CrosshairRuleTests(unittest.TestCase):
    """What counts as "on target" before the press: the reference block on
    the named face, or a plant in the cell itself. The same block on
    another face is another cell."""

    def skill(self):
        skill = skills.create("place_block_at", x=2, y=64, z=0,
                              item="cobblestone")
        skill._ref = building.Reference((2, 63, 0), "grass_block", "up")
        return skill

    def on(self, name, cell, face):
        from minecraft.state import BlockRef
        return WorldState(target_block=BlockRef(name=name, x=cell[0],
                                                y=cell[1], z=cell[2],
                                                face=face),
                          source="bridge", confidence=EXACT)

    def test_the_face_named(self):
        self.assertEqual(self.skill()._on_target(
            self.on("grass_block", (2, 63, 0), "up")), ((2, 63, 0), "up"))

    def test_another_face_of_the_same_block_is_not_on_target(self):
        for face in ("east", "north", "down"):
            self.assertIsNone(self.skill()._on_target(
                self.on("grass_block", (2, 63, 0), face)), face)

    def test_a_plant_in_the_cell_is(self):
        self.assertEqual(self.skill()._on_target(
            self.on("short_grass", (2, 64, 0), "up")), ((2, 64, 0), None))

    def test_a_block_in_the_cell_is_not(self):
        self.assertIsNone(self.skill()._on_target(
            self.on("stone", (2, 64, 0), "up")))


def column_state(*columns, notable=()):
    return WorldState(position=(0.5, 64.0, 0.5),
                      surface=tuple(NearbyBlock(*c) for c in columns),
                      notable_blocks=tuple(notable),
                      source="bridge", confidence=EXACT)


class CellRuleTests(unittest.TestCase):

    def verdict(self, state, cell, known=None):
        return building.what_is_at(nav.LocalMap.from_state(state), state,
                                   cell, known)

    def test_the_space_above_a_floor_is_empty_up_to_its_headroom(self):
        state = column_state((1, 63, 0, "grass_block", True, 3, None))
        self.assertEqual(self.verdict(state, (1, 64, 0))[0], building.EMPTY)
        self.assertEqual(self.verdict(state, (1, 66, 0))[0], building.EMPTY)
        self.assertEqual(self.verdict(state, (1, 67, 0))[0],
                         building.OCCUPIED, "what ended the headroom")
        self.assertEqual(self.verdict(state, (1, 68, 0))[0], building.UNKNOWN)
        self.assertEqual(self.verdict(state, (1, 62, 0))[0], building.UNKNOWN)
        self.assertEqual(self.verdict(state, (1, 63, 0)),
                         (building.OCCUPIED, "grass_block"))

    def test_above_the_counted_headroom_is_unknown(self):
        state = column_state((1, 63, 0, "grass_block", True, 4, None))
        self.assertEqual(self.verdict(state, (1, 67, 0))[0], building.EMPTY)
        self.assertEqual(self.verdict(state, (1, 68, 0))[0], building.UNKNOWN)

    def test_a_plant_or_a_bush(self):
        grass = column_state((1, 63, 0, "grass_block", True, 4, "short_grass"))
        self.assertEqual(self.verdict(grass, (1, 64, 0))[0], building.REPLACE)
        bush = column_state((1, 63, 0, "grass_block", True, 4,
                             "sweet_berry_bush"))
        self.assertEqual(self.verdict(bush, (1, 64, 0))[0], building.OCCUPIED)

    def test_an_unscanned_column_or_an_old_mod(self):
        state = column_state((1, 63, 0, "grass_block", True, None, None))
        self.assertEqual(self.verdict(state, (1, 64, 0))[0], building.UNKNOWN)
        self.assertEqual(self.verdict(state, (5, 64, 5))[0], building.UNKNOWN)

    def test_a_notable_block_and_a_placed_one(self):
        state = column_state((1, 63, 0, "grass_block", True, 4, None),
                             notable=(NearbyBlock(1, 64, 0, "oak_log", True),))
        self.assertEqual(self.verdict(state, (1, 64, 0)),
                         (building.OCCUPIED, "oak_log"))
        self.assertEqual(self.verdict(state, (1, 65, 0),
                                      {(1, 65, 0): "dirt"})[0],
                         building.OCCUPIED)

    def test_references_prefer_the_block_below(self):
        state = column_state((1, 63, 0, "grass_block", True, 4, None),
                             (2, 64, 0, "stone", True, 4, None))
        refs = building.references(nav.LocalMap.from_state(state), state,
                                   (1, 64, 0))
        self.assertEqual(refs[0], building.Reference((1, 63, 0),
                                                     "grass_block", "up"))
        self.assertEqual(refs[1], building.Reference((2, 64, 0), "stone",
                                                     "west"))

    def test_the_body(self):
        self.assertTrue(building.body_overlaps((0.5, 64.0, 0.5), (0, 65, 0)))
        self.assertTrue(building.body_overlaps((0.75, 64.0, 0.5), (1, 64, 0)),
                        "0.3 wide either side: x 0.45 to 1.05")
        self.assertFalse(building.body_overlaps((0.5, 64.0, 0.5), (1, 64, 0)))
        self.assertFalse(building.body_overlaps((0.5, 64.0, 0.5), (0, 66, 0)))

    def test_it_imports_only_pure_modules(self):
        tree = ast.parse((ROOT / "minecraft" / "building.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"__future__", "dataclasses", "math",
                                        "heapq"})


class RegistryTests(unittest.TestCase):

    def test_it_is_a_task_with_coordinates(self):
        self.assertIn("place_block_at", skills.available())
        from actions import minecraft as mc_actions
        self.assertIn("place_block_at", mc_actions.TOOL["description"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
