"""
DropWorld and LeafWorld: the worlds behind the live-run
regression tests (drops that land away, leaves in the way).

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/test_live_run_regressions.py.
"""

from __future__ import annotations

import dataclasses
import math

from minecraft import mining as mining_mod
from minecraft.state import EntityRef
from tests.support.sim_world import TreeWorld


class DropWorld(TreeWorld):
    """TreeWorld where a broken log DROPS, as in the real game.

    TreeWorld puts a broken log straight into the inventory. Minecraft does
    not: it spawns an item where the block was, which lands beside the trunk,
    and the player collects it only by walking within about a block of it.
    The item is reported the way the bridge reports it -- an entity of
    category "item" with a position."""

    PICKUP = 1.425
    """Minecraft collects items whose box meets the player's box grown by one
    block sideways: about 1.4 blocks either side, per axis -- a box, not a
    circle."""

    def __init__(self, *args, drop_under_trunk=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.drops = []
        self.drop_under_trunk = drop_under_trunk

    def mine(self, params):
        before = dict(self.inventory)
        broke_before = len(self.broken)
        result = TreeWorld.mine(self, params)
        if len(self.broken) > broke_before:
            log = self.broken[-1]
            self.inventory = before                 # not in the bag yet...
            if self.drop_under_trunk:
                # ...it fell into the gap it left, under the rest of the tree.
                self.drops.append((log.x + 0.5, 64.0, log.z + 0.5))
            else:
                # ...it fell to the ground on the far side of the trunk.
                self.drops.append((log.x + 1.5, 64.0, log.z + 0.5))
        self._collect()
        return result

    def _collect(self):
        for drop in list(self.drops):
            if abs(drop[0] - self.x) <= self.PICKUP \
                    and abs(drop[2] - self.z) <= self.PICKUP:
                self.drops.remove(drop)
                self.inventory["oak_log"] = self.inventory.get("oak_log", 0) + 1

    def _walk(self, distance, jumping):
        TreeWorld._walk(self, distance, jumping)
        self._collect()

    def read(self):
        self._collect()
        state = TreeWorld.read(self)
        items = tuple(
            EntityRef(name="item", category="item", hostile=False,
                      position=drop,
                      distance=math.dist(drop, (self.x, self.y, self.z)))
            for drop in self.drops)
        return dataclasses.replace(state, nearby_entities=items)


# ── The second run ───────────────────────────────────────────────────────────

class LeafWorld(TreeWorld):
    """TreeWorld whose leaves can be broken, as the game's can: hold attack
    on a leaf block long enough and it goes."""

    def mine(self, params):
        hit = self.crosshair()
        if hit is not None and hit[0] in self.blocks \
                and hit[1].endswith("_leaves"):
            self.swings += 1
            needed = mining_mod.estimate_break_duration(hit[1]).seconds
            if float(params.get("duration", 0)) >= needed:
                del self.blocks[hit[0]]
                self.leaves_broken.append(hit[0])
            return self._result("mine", params)
        return TreeWorld.mine(self, params)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.leaves_broken = []
