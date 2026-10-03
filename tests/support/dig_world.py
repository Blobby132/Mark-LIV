"""
Cells for the digging tests: a block of rock with a hollow for the player,
and helpers to put water, lava, gravel, caves and ore into it.

Cells are the bridge's grid shape: {(x, y, z): (name, solid, fluid)}, short
names, fluid None or "water" / "lava" / "flowing_water" / "flowing_lava".
A cell left out is unknown -- exactly as the grid leaves out a cell in an
unloaded chunk.
"""

from __future__ import annotations

AIR = ("air", False, None)
STONE = ("stone", True, None)
WATER = ("water", False, "water")
LAVA = ("lava", False, "lava")


def block(name, solid=True, fluid=None):
    return (name, solid, fluid)


def rock(feet=(0, 60, 0), xs=(-8, 12), ys=(44, 72), zs=(-6, 6),
         name="stone"):
    """Solid `name` everywhere in the box, with air at the player's feet
    and head."""
    cells = {(x, y, z): (name, True, None)
             for x in range(xs[0], xs[1] + 1)
             for y in range(ys[0], ys[1] + 1)
             for z in range(zs[0], zs[1] + 1)}
    x, y, z = feet
    cells[(x, y, z)] = AIR
    cells[(x, y + 1, z)] = AIR
    return cells


def put(cells, cell, entry):
    cells[tuple(cell)] = entry
    return cells


def hollow(cells, *where):
    """Air at each cell given."""
    for cell in where:
        cells[tuple(cell)] = AIR
    return cells


__all__ = ["AIR", "STONE", "WATER", "LAVA", "block", "rock", "put",
           "hollow"]
