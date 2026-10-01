"""
SimWorld and TreeWorld: the simulated terrain most Minecraft task
tests walk, look and mine in; flat(), state_from() and run().

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/test_minecraft_navigation.py.
"""

from __future__ import annotations

import math

from minecraft import action_spec
from minecraft import navigation as nav
from minecraft import mining as mining_mod
from minecraft.controller import ActionResult
from minecraft.state import BlockRef, EXACT, ItemStack, NearbyBlock, WorldState
from minecraft.task_runner import DISPATCH, TaskRunner


# ── World fixtures ───────────────────────────────────────────────────────────

def flat(radius=8, y=63, name="grass_block"):
    """A flat clearing: every column in range, same height."""
    return [NearbyBlock(x, y, z, name, True)
            for x in range(-radius, radius + 1)
            for z in range(-radius, radius + 1)]


def state_from(surface, position=(0.5, 64.0, 0.5), rotation=(0.0, 0.0),
               notable=(), entities=(), radius=8):
    return WorldState(position=position, rotation=rotation,
                      surface=tuple(surface), notable_blocks=tuple(notable),
                      nearby_entities=tuple(entities), scan_radius=radius,
                      source="test", confidence=EXACT)


# ── A world that responds ────────────────────────────────────────────────────

class SimWorld:
    """A crude Minecraft: a heightmap, a player, and collision.

    Crude on purpose. It exists to answer one question — does following the
    skill's steps get the player there — and every simplification in it is a
    reason the real game may still disagree."""

    WALK_SPEED = 4.3

    def __init__(self, surface, position=(0.5, 64.0, 0.5), yaw=0.0):
        self.surface = list(surface)
        self.ground = {(b.x, b.z): b for b in self.surface}
        self.x, self.y, self.z = position
        self.yaw = yaw
        self.pitch = 0.0
        self.notable = ()
        self.blocked = 0
        self.reads = 0

    # -- state source --
    def read(self):
        self.reads += 1
        return state_from(self.surface, position=(self.x, self.y, self.z),
                          rotation=(self.yaw, self.pitch),
                          notable=self.notable)

    # -- controller --
    def _guard(self):
        return ""

    def _result(self, name, params, ms=200):
        return ActionResult(ok=True, action=name, requested=dict(params or {}),
                            actual_duration_ms=ms)

    def look(self, params):
        # The real controller runs every look through action_spec, which
        # clamps the delta. Without clamping here the simulation accepts
        # turns the game would never receive, and anything measuring its own
        # turns reads a sensitivity that does not exist.
        dx = self._clamp(params.get("dx", 0))
        dy = self._clamp(params.get("dy", 0))
        self.yaw = ((self.yaw + dx / nav.PIXELS_PER_DEGREE
                     + 180.0) % 360.0) - 180.0
        # Positive dy is mouse-down, which is looking down, which is
        # increasing pitch. Clamped as the game clamps it.
        self.pitch = max(-90.0, min(90.0,
                                    self.pitch + dy / nav.PIXELS_PER_DEGREE))
        return self._result("look", params)

    @staticmethod
    def _clamp(value):
        return max(-action_spec.MAX_LOOK_DELTA_PX,
                   min(value, action_spec.MAX_LOOK_DELTA_PX))

    # Minecraft lets you walk up 0.6 of a block — a slab, a path edge — and
    # not a full block. This simulation used to let the player walk straight
    # up full blocks, which is precisely the thing that fails in the real
    # game ("it walks into a block and I have to tell it to jump"), and so no
    # test could ever see that bug.
    STEP_HEIGHT = 0.6
    STRIDE = 0.1
    HALF_WIDTH = 0.3

    def move(self, params):
        seconds = float(params.get("duration", 0.5))
        self._walk(seconds * self.WALK_SPEED, jumping=False)
        return self._result("move", params, int(seconds * 1000))

    def move_and_jump(self, params):
        seconds = float(params.get("duration", 0.4))
        self._walk(min(seconds * self.WALK_SPEED, 1.6), jumping=True)
        return self._result("move_and_jump", params, int(seconds * 1000))

    def _walk(self, distance, jumping):
        """Walk in small strides and stop at the first column the body cannot
        enter.

        Stride by stride rather than teleporting to the end point: the old
        version checked only the column it landed in, so a long move could
        pass straight through a wall in the middle of it — and path
        smoothing makes long moves."""
        radians = math.radians(self.yaw)
        ux, uz = -math.sin(radians), math.cos(radians)
        rose = False
        travelled = 0.0
        while travelled < distance - 1e-9:
            stride = min(self.STRIDE, distance - travelled)
            nx, nz = self.x + ux * stride, self.z + uz * stride
            if getattr(self, "SLIDE", False) and not self._fits_at(
                    nx, nz, jumping and not rose):
                # Minecraft resolves collision one axis at a time: walking
                # into a wall at an angle slides along it. Opt-in, so the
                # worlds written before this keep their stricter rule.
                if self._fits_at(nx, self.z, jumping and not rose):
                    nz = self.z
                elif self._fits_at(self.x, nz, jumping and not rose):
                    nx = self.x
            # The body is 0.6 wide. A shoulder hits a wall a centre-line
            # misses, so every corner of the footprint has to fit.
            for ox in (-self.HALF_WIDTH, self.HALF_WIDTH):
                for oz in (-self.HALF_WIDTH, self.HALF_WIDTH):
                    corner = (math.floor(nx + ox), math.floor(nz + oz))
                    other = self.ground.get(corner)
                    if other is None:
                        self.blocked += 1
                        return
                    lift = (other.y + 1) - self.y
                    # A jump clears one block, not two: a shoulder against
                    # a two-high wall stops the move, jumping or not.
                    if lift > self.STEP_HEIGHT and not (
                            jumping and not rose and lift <= 1.0 + 1e-9):
                        self.blocked += 1
                        return
            column = (math.floor(nx), math.floor(nz))
            if column != (math.floor(self.x), math.floor(self.z)):
                block = self.ground.get(column)
                if block is None:
                    self.blocked += 1
                    return
                rise = (block.y + 1) - self.y
                room = block.clearance
                if room is not None and room < 2:
                    self.blocked += 1          # a player's head is in the way
                    return
                if rise > self.STEP_HEIGHT:
                    if not (jumping and not rose and rise <= 1.0 + 1e-9):
                        self.blocked += 1      # a full block: needs a jump
                        return
                    rose = True
                self.y = float(block.y + 1)
            if getattr(self, "SLIDE", False):
                # Held up by the highest block under any part of the body,
                # as in the game: walking off a wall top you stay on it
                # until the whole body is clear, then drop -- never into a
                # spot where a shoulder is inside the wall.
                support = max(self.ground[(math.floor(nx + ox),
                                           math.floor(nz + oz))].y + 1
                              for ox in (-self.HALF_WIDTH, self.HALF_WIDTH)
                              for oz in (-self.HALF_WIDTH, self.HALF_WIDTH))
                self.y = float(support)
            self.x, self.z = nx, nz
            travelled += stride

    def _fits_at(self, nx, nz, may_rise) -> bool:
        """Could the body's four corners stand at (nx, nz)?"""
        for ox in (-self.HALF_WIDTH, self.HALF_WIDTH):
            for oz in (-self.HALF_WIDTH, self.HALF_WIDTH):
                other = self.ground.get((math.floor(nx + ox),
                                         math.floor(nz + oz)))
                if other is None:
                    return False
                lift = (other.y + 1) - self.y
                if lift > self.STEP_HEIGHT and not (
                        may_rise and lift <= 1.0 + 1e-9):
                    return False
        return True

    def jump(self, params):
        # A jump on the spot goes up and comes straight back down: no
        # horizontal movement, which is why a separate walk-then-jump never
        # gets anyone onto a ledge.
        return self._result("jump", params, 100)

    def __getattr__(self, name):
        if name in DISPATCH:
            return lambda params: self._result(name, params)
        raise AttributeError(name)

    def distance_to(self, column):
        return math.dist((self.x, self.z), (column[0] + 0.5, column[1] + 0.5))


def run(world, skill, max_steps=40):
    # The measured mouse scale is process-wide on purpose — one machine, one
    # sensitivity slider — so a test that measures it would otherwise change
    # the arithmetic every later test sees.
    nav.reset_calibration()
    runner = TaskRunner(world, world, sleeper=lambda _s: None)
    return runner.run(skill, max_steps=max_steps)


class TreeWorld(SimWorld):
    """SimWorld plus trees you can walk to, aim at and break.

    It has a CROSSHAIR. The real bridge reports the block the crosshair is
    on, with its coordinates and face, and the mining code now refuses to
    swing unless that block is the log it means to break. So this world
    raycasts from the eye along the view — through air, into the first log
    or the ground — and reports what it hits, the way the game does.

    Mining breaks what the crosshair is on, and only if the hold is at least
    as long as Minecraft needs for that block. Those are the two rules the
    real game enforces and the two things that went wrong in real play:
    swinging at the wrong thing, and letting go too soon."""

    REACH = 4.5
    EYE = 1.62

    def __init__(self, surface, logs, blocks=(), inventory=None, **kwargs):
        super().__init__(surface, **kwargs)
        self.logs = list(logs)
        self.blocks = {b.position: b for b in blocks}   # leaves etc.
        self.inventory = inventory                      # None: unreadable
        self.broken = []
        self.swings = 0
        self.wrong_block_swings = 0

    @property
    def notable(self):
        return tuple(self.logs)

    @notable.setter
    def notable(self, value):
        self.logs = list(value)

    def crosshair(self):
        """(position, name, face) of the first thing the view ray hits, or
        None for sky within reach."""
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        ux = -math.sin(yaw) * math.cos(pitch)
        uy = -math.sin(pitch)
        uz = math.cos(yaw) * math.cos(pitch)
        ex, ey, ez = self.x, self.y + self.EYE, self.z
        logs = {log.position: log for log in self.logs}
        previous = (math.floor(ex), math.floor(ey), math.floor(ez))
        distance = 0.0
        while distance <= self.REACH:
            px, py, pz = ex + ux * distance, ey + uy * distance, ez + uz * distance
            cell = (math.floor(px), math.floor(py), math.floor(pz))
            if cell != previous:
                face = _entered_face(previous, cell)
                if cell in logs:
                    return cell, logs[cell].name, face
                if cell in self.blocks:
                    return cell, self.blocks[cell].name, face
                ground = self.ground.get((cell[0], cell[2]))
                if ground is not None and cell[1] <= ground.y:
                    return cell, ground.name, face
                previous = cell
            distance += 0.02
        return None

    def read(self):
        state = SimWorld.read(self)
        hit = self.crosshair()
        target = (BlockRef(name=hit[1], x=hit[0][0], y=hit[0][1],
                           z=hit[0][2], face=hit[2])
                  if hit else BlockRef(name="air"))
        stacks = None
        if self.inventory is not None:
            stacks = tuple(ItemStack(slot=i, name=name, count=count)
                           for i, (name, count) in enumerate(
                               sorted(self.inventory.items())) if count)
        return WorldState(
            position=state.position, rotation=state.rotation,
            surface=state.surface, notable_blocks=tuple(self.logs),
            target_block=target, scan_radius=state.scan_radius,
            inventory=stacks, on_ground=True, source="test", confidence=EXACT)

    def mine(self, params):
        self.swings += 1
        seconds = float(params.get("duration", 0))
        hit = self.crosshair()
        log = None
        if hit is not None:
            log = next((l for l in self.logs if l.position == hit[0]), None)
        if log is None:
            self.wrong_block_swings += 1
        else:
            needed = mining_mod.estimate_break_duration(log.name).seconds
            if seconds >= needed:
                self.logs.remove(log)
                self.broken.append(log)
                if self.inventory is not None:
                    self.inventory[log.name] = self.inventory.get(log.name, 0) + 1
            # else: Minecraft throws the progress away when the button comes
            # up, and nothing breaks however many times it is repeated.
        return self._result("mine", params, int(seconds * 1000))


def _entered_face(previous, cell):
    """Which face of `cell` a ray coming from `previous` passed through."""
    dx, dy, dz = (previous[0] - cell[0], previous[1] - cell[1],
                  previous[2] - cell[2])
    if dy > 0:
        return "up"
    if dy < 0:
        return "down"
    if dx > 0:
        return "east"
    if dx < 0:
        return "west"
    if dz > 0:
        return "south"
    return "north"
