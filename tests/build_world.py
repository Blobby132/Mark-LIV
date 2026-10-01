"""
A simulated world to build in: GuiWorld (screens, a hotbar, a crosshair)
where a right-click places the held block the way the game does.

  - The crosshair is TreeWorld's raycast; a placed block is a block it hits.
  - `place` goes through the same refusals the controller makes: the held
    item's deny-list, then `expect_at` / `expect_face` against the crosshair
    at the moment of pressing.
  - The block lands against the face under the crosshair -- or INTO the cell
    under it when that holds a plant the game replaces -- and only if that
    cell is free and not inside the player's body. The held stack goes down
    by one (not in creative).
  - The terrain scan is recomputed from the blocks as the mod computes it:
    per column, the floor nearest the feet with two blocks of room above,
    the headroom over it (counted to four) and the first thing in the space
    a player standing there would occupy.

Crude where crudeness is harmless: nothing falls, no entities block a
placement.
"""

from __future__ import annotations

import math

from minecraft import action_spec
from minecraft import building
from minecraft.state import NearbyBlock
from tests.gui_world import GuiWorld

MAX_CLEARANCE = 4
NEED = 2


class BuildWorld(GuiWorld):

    def __init__(self, items, placed=(), plants=(), **kwargs):
        super().__init__(items, **kwargs)
        self.base = {column: block.y for column, block in self.ground.items()}
        self.base_name = {column: block.name
                          for column, block in self.ground.items()}
        for cell, name in placed:
            self.blocks[tuple(cell)] = NearbyBlock(*cell, name, True)
        for cell, name in plants:
            self.blocks[tuple(cell)] = NearbyBlock(*cell, name, False)
        self.placed = []
        self.presses = 0
        self.not_confirmed = 0
        self._rescan()

    # ── what is where ───────────────────────────────────────────────────

    def _name_at(self, cell):
        block = self.blocks.get(tuple(cell))
        if block is not None:
            return block.name
        base = self.base.get((cell[0], cell[2]))
        if base is not None and cell[1] <= base:
            return self.base_name[(cell[0], cell[2])] if cell[1] == base \
                else "stone"
        return None

    def _solid(self, cell):
        """Has collision: a placed block, the ground -- not a plant, a
        bush or anything else added with solid=False."""
        block = self.blocks.get(tuple(cell))
        if block is not None:
            return block.solid is not False
        return self._name_at(cell) is not None

    def _rescan(self):
        feet = math.floor(self.y)
        surface = []
        for (x, z), base in self.base.items():
            top = max([base] + [c[1] for c in self.blocks if (c[0], c[2])
                                == (x, z)])
            best = None
            for y in range(base, top + 1):
                if not self._solid((x, y, z)):
                    continue
                room = 0
                while room < MAX_CLEARANCE and \
                        not self._solid((x, y + 1 + room, z)):
                    room += 1
                if room < NEED:
                    continue
                distance = abs((y + 1) - feet)
                if best is None or distance < best[0]:
                    best = (distance, y, room)
            if best is None:
                continue
            _d, y, room = best
            cover = None
            for up in range(1, NEED + 1):
                name = self._name_at((x, y + up, z))
                if name is not None:
                    cover = name
                    break
            surface.append(NearbyBlock(x, y, z, self._name_at((x, y, z)),
                                       True, room, cover))
        self.surface = surface
        self.ground = {(b.x, b.z): b for b in surface}

    def read(self):
        self._rescan()
        return super().read()

    # ── the controller ──────────────────────────────────────────────────

    def place(self, params):
        held = self.stacks.get(self.selected)
        refusal = action_spec.place_refusal(held[0] if held else "")
        if refusal:
            return self._done("place", params, ok=False, reason="held_item",
                              error=refusal)
        spec = action_spec.parse_place(params)
        hit = self.crosshair()
        if spec.expect_target is not None:
            wrong = hit is None or tuple(hit[0]) != spec.expect_target or (
                spec.expect_face is not None and hit[2] != spec.expect_face)
            if wrong:
                self.not_confirmed += 1
                return self._done("place", params, ok=False,
                                  reason="target_not_confirmed",
                                  error=f"the crosshair is on {hit}")
        self.presses += 1
        if hit is None or self.screen is not None:
            return self._done("place", params)
        cell, name, face = hit
        target = tuple(cell) if name in building.REPLACEABLE \
            else building.lands_at(cell, face)
        there = self._name_at(target)
        if (there is not None and there not in building.REPLACEABLE) \
                or building.body_overlaps((self.x, self.y, self.z), target):
            return self._done("place", params)
        self.blocks[target] = NearbyBlock(*target, held[0], True)
        self.placed.append((target, held[0]))
        if self.mode != "creative":
            held[1] -= 1
            if held[1] <= 0:
                del self.stacks[self.selected]
        self._rescan()
        return self._done("place", params)


__all__ = ["BuildWorld"]
