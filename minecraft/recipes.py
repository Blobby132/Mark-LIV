"""
minecraft/recipes.py — the recipes craft_item knows. Data.

A shaped recipe is a grid of ingredient TAGS, rows top to bottom, None for
an empty cell, aligned to the grid's top-left corner. A tag is a set of
items any of which fits the cell: "planks" is every kind of plank. The
few helpers below only read the table.

Only what a fresh survival start needs: planks, sticks, a crafting table,
wooden and stone tools, a furnace, torches and a chest.
"""

from __future__ import annotations

from dataclasses import dataclass

_TREES = ("oak", "birch", "spruce", "jungle", "acacia", "dark_oak",
          "mangrove", "cherry", "pale_oak")
_FUNGI = ("crimson", "warped")


def _logs_of(species: str) -> frozenset:
    """Every block that makes `species` planks: its log or stem, its
    _wood or _hyphae, and their stripped forms."""
    if species in _FUNGI:
        parts = ("stem", "hyphae")
    else:
        parts = ("log", "wood")
    return frozenset({f"{species}_{p}" for p in parts}
                     | {f"stripped_{species}_{p}" for p in parts})


TAGS = {
    "planks": frozenset(f"{s}_planks" for s in _TREES + _FUNGI),
    "logs": frozenset().union(*(_logs_of(s) for s in _TREES + _FUNGI)),
    "stick": frozenset({"stick"}),
    "stone_tool_materials": frozenset({"cobblestone", "blackstone",
                                       "cobbled_deepslate"}),
    "stone_crafting_materials": frozenset({"cobblestone", "blackstone",
                                           "cobbled_deepslate"}),
    "coals": frozenset({"coal", "charcoal"}),
}
for _species in _TREES + _FUNGI:
    TAGS[f"{_species}_logs"] = _logs_of(_species)


@dataclass(frozen=True)
class Recipe:
    output: str
    count: int
    pattern: tuple            # rows of tags, None for an empty cell


_P, _S, _C = "planks", "stick", "stone_tool_materials"


def _tools(material: str, head: str) -> dict:
    return {
        f"{material}_pickaxe": Recipe(f"{material}_pickaxe", 1,
                                      ((head, head, head),
                                       (None, _S, None),
                                       (None, _S, None))),
        f"{material}_axe": Recipe(f"{material}_axe", 1,
                                  ((head, head), (head, _S), (None, _S))),
        f"{material}_sword": Recipe(f"{material}_sword", 1,
                                    ((head,), (head,), (_S,))),
        f"{material}_shovel": Recipe(f"{material}_shovel", 1,
                                     ((head,), (_S,), (_S,))),
        f"{material}_hoe": Recipe(f"{material}_hoe", 1,
                                  ((head, head), (None, _S), (None, _S))),
    }


RECIPES = {
    **{f"{s}_planks": Recipe(f"{s}_planks", 4, ((f"{s}_logs",),))
       for s in _TREES + _FUNGI},
    "stick": Recipe("stick", 4, ((_P,), (_P,))),
    "crafting_table": Recipe("crafting_table", 1, ((_P, _P), (_P, _P))),
    **_tools("wooden", _P),
    **_tools("stone", _C),
    "furnace": Recipe("furnace", 1,
                      (("stone_crafting_materials",) * 3,
                       ("stone_crafting_materials", None,
                        "stone_crafting_materials"),
                       ("stone_crafting_materials",) * 3)),
    "torch": Recipe("torch", 4, (("coals",), (_S,))),
    "chest": Recipe("chest", 1, ((_P, _P, _P), (_P, None, _P),
                                 (_P, _P, _P))),
}


def recipe_for(item):
    """The recipe for `item`, or None."""
    return RECIPES.get(str(item or "").split(":")[-1].strip().lower())


def size(recipe) -> tuple:
    """(width, height) of the pattern."""
    return (max(len(row) for row in recipe.pattern), len(recipe.pattern))


def fits(recipe, grid: int) -> bool:
    """Does it fit a grid `grid` cells square (2 for the inventory)?"""
    width, height = size(recipe)
    return width <= grid and height <= grid


def ingredients(recipe) -> dict:
    """{tag: how many} for one batch."""
    out = {}
    for row in recipe.pattern:
        for tag in row:
            if tag is not None:
                out[tag] = out.get(tag, 0) + 1
    return out


def planks_for(log):
    """The planks a log, wood, stem or hyphae block makes, or None."""
    name = str(log or "").split(":")[-1]
    for species in _TREES + _FUNGI:
        if name in TAGS[f"{species}_logs"]:
            return f"{species}_planks"
    return None


__all__ = ["Recipe", "RECIPES", "TAGS", "fits", "ingredients", "planks_for",
           "recipe_for", "size"]
