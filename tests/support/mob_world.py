"""
MobWorld: a simulated world with mobs that chase and hurt.

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/test_minecraft_flee.py.
"""

from __future__ import annotations

import math

from minecraft.state import EXACT, EntityRef, WorldState
from tests.support.sim_world import TreeWorld, flat


class MobWorld(TreeWorld):
    """A flat clearing with mobs that may walk towards the player."""

    SPRINT_SPEED = 5.6

    def __init__(self, mobs, chase=0.0, health=20.0, hurt_per_step=0.0,
                 radius=16, **kwargs):
        super().__init__(flat(radius=radius), [], **kwargs)
        self.mobs = [list(m) for m in mobs]   # [name, x, z, category]
        self.chase = chase                     # blocks a mob closes per step
        self.health = health
        self.hurt_per_step = hurt_per_step
        self.sprints = 0
        self.nearest_seen = []

    def _tick(self):
        for mob in self.mobs:
            dx, dz = self.x - mob[1], self.z - mob[2]
            d = math.hypot(dx, dz)
            if self.chase and d > 1.0:
                mob[1] += dx / d * min(self.chase, d - 1.0)
                mob[2] += dz / d * min(self.chase, d - 1.0)
        self.health = max(1.0, self.health - self.hurt_per_step)

    def read(self):
        base = TreeWorld.read(self)
        entities = []
        for name, mx, mz, category in self.mobs:
            entities.append(EntityRef(
                name=name, position=(mx, 64.0, mz), category=category,
                hostile=category == "hostile",
                distance=math.dist((self.x, self.z), (mx, mz))))
        hostile = [e.distance for e in entities if e.hostile]
        if hostile:
            self.nearest_seen.append(min(hostile))
        return WorldState(
            position=base.position, rotation=base.rotation,
            surface=base.surface, scan_radius=base.scan_radius,
            target_block=base.target_block, nearby_entities=tuple(entities),
            health=self.health, on_ground=True, source="bridge",
            confidence=EXACT)

    def move(self, params):
        result = TreeWorld.move(self, params)
        self._tick()
        return result

    def look(self, params):
        result = TreeWorld.look(self, params)
        self._tick()
        return result

    def sprint(self, params):
        self.sprints += 1
        seconds = float(params.get("duration", 0))
        self._walk(seconds * self.SPRINT_SPEED, jumping=False)
        self._tick()
        return self._result("sprint", params, int(seconds * 1000))


def zombie(x, z):
    return ("zombie", x, z, "hostile")
