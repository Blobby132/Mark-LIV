"""
B3e: minecraft/recipes.py -- the recipes craft_item knows, as data.

Shaped patterns as grids of ingredient tags (None for an empty cell),
aligned top-left; tags name the sets of items that fit ("planks", "logs").
"""

from __future__ import annotations

import ast
import unittest

from tests.support.paths import REPO_ROOT as ROOT

from minecraft import recipes                                       # noqa: E402


class RecipeTableTests(unittest.TestCase):

    def test_the_requested_recipes_exist(self):
        for item in ("oak_planks", "spruce_planks", "crimson_planks", "stick",
                     "crafting_table", "wooden_pickaxe", "wooden_axe",
                     "wooden_sword", "wooden_shovel", "wooden_hoe",
                     "stone_pickaxe", "stone_axe", "stone_sword",
                     "stone_shovel", "stone_hoe", "furnace", "torch",
                     "chest"):
            with self.subTest(item=item):
                self.assertIsNotNone(recipes.recipe_for(item))

    def test_any_log_of_a_wood_makes_its_planks(self):
        planks = recipes.recipe_for("birch_planks")
        self.assertEqual(planks.count, 4)
        self.assertEqual(recipes.size(planks), (1, 1))
        tag = planks.pattern[0][0]
        for log in ("birch_log", "birch_wood", "stripped_birch_log"):
            self.assertIn(log, recipes.TAGS[tag])
        self.assertNotIn("oak_log", recipes.TAGS[tag])
        stem = recipes.TAGS[recipes.recipe_for("warped_planks").pattern[0][0]]
        self.assertIn("warped_stem", stem)

    def test_planks_for_a_log(self):
        self.assertEqual(recipes.planks_for("dark_oak_log"), "dark_oak_planks")
        self.assertEqual(recipes.planks_for("stripped_crimson_stem"),
                         "crimson_planks")
        self.assertIsNone(recipes.planks_for("stone"))

    def test_shapes(self):
        pick = recipes.recipe_for("wooden_pickaxe")
        self.assertEqual(pick.pattern, (("planks", "planks", "planks"),
                                        (None, "stick", None),
                                        (None, "stick", None)))
        self.assertEqual(recipes.size(pick), (3, 3))
        self.assertEqual(recipes.size(recipes.recipe_for("stick")), (1, 2))
        self.assertEqual(recipes.size(recipes.recipe_for("crafting_table")),
                         (2, 2))
        furnace = recipes.recipe_for("furnace")
        self.assertIsNone(furnace.pattern[1][1], "a furnace is a ring")
        self.assertEqual(recipes.ingredients(furnace),
                         {"stone_crafting_materials": 8})

    def test_what_fits_the_inventory_grid(self):
        for item in ("oak_planks", "stick", "crafting_table", "torch"):
            self.assertTrue(recipes.fits(recipes.recipe_for(item), 2), item)
        for item in ("wooden_pickaxe", "furnace", "chest", "stone_axe"):
            self.assertFalse(recipes.fits(recipes.recipe_for(item), 2), item)

    def test_tags(self):
        self.assertIn("oak_planks", recipes.TAGS["planks"])
        self.assertIn("cobbled_deepslate",
                      recipes.TAGS["stone_tool_materials"])
        self.assertIn("charcoal", recipes.TAGS["coals"])

    def test_ingredients_count_per_batch(self):
        self.assertEqual(recipes.ingredients(recipes.recipe_for(
            "wooden_pickaxe")), {"planks": 3, "stick": 2})

    def test_an_unknown_item(self):
        self.assertIsNone(recipes.recipe_for("diamond_pickaxe"))


class DataOnlyTests(unittest.TestCase):

    def test_it_imports_only_pure_modules(self):
        tree = ast.parse((ROOT / "minecraft" / "recipes.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertLessEqual(imported, {"__future__", "dataclasses"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
