"""
Walking across grass and up to trees.

From a real run on flat, open grassland: every "walk to that tree" stopped at
once -- "I can see a birch log at (21, 64, -201), but I cannot find a walkable
route to it from here", zero steps taken, over and over. Nothing was in the
way. The planner was right about what it was told; it was told the wrong
thing, in two ways:

  * The bridge reported each column's TOPMOST block. A grass tuft or a
    flower is a block with no collision, so a tufted column read as "no
    floor here" -- and a meadow is mostly tufts. The player's own neighbours
    were all "impassable", which is why it took zero steps.
  * Under a tree, the topmost block is the canopy. Every column next to a
    trunk looked like leaves four blocks up, and the only goal the planner
    would accept was a column touching the trunk.

The mod now reports the floor nearest the feet (checked in
test_bridge_floor_scan.py). These tests cover the Python side: an older jar's
grass read as the floor it grows on, the newer jar's report used as given,
what a player would be standing IN kept visible, and somewhere to stand a
little way out when nothing touching the tree will do.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minecraft import aiming as aiming_mod                          # noqa: E402
from minecraft import navigation as nav                             # noqa: E402
from minecraft import skills                                        # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource               # noqa: E402
from minecraft.state import NearbyBlock                             # noqa: E402

from tests.test_minecraft_navigation import (                       # noqa: E402
    SimWorld, _entered_face, flat, run, state_from,
)
from tests.test_live_run_regressions import LeafWorld               # noqa: E402

OLD = "markliv.minecraft.state/3"
NEW = "markliv.minecraft.state/4"
NOW_MS = 1_700_000_000_000
RADIUS = 8
TRUNK = (5, 0)


def read(schema, surface, notable=(), position=(0.5, 64.0, 0.5)):
    """A payload as the mod writes it, through the real parser."""
    payload = {
        "schema": schema, "written_at_ms": NOW_MS, "in_game": True,
        "position": list(position), "rotation": [0.0, 0.0],
        "scan": {"radius": RADIUS, "up": 4, "down": 5},
        "surface": surface, "notable_blocks": notable,
    }
    text = json.dumps(payload)
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: NOW_MS / 1000.0).read()


def trunk_logs(bottom=64, top=68):
    return [[TRUNK[0], y, TRUNK[1], "minecraft:oak_log"]
            for y in range(bottom, top + 1)]


def meadow_as_the_old_jar_saw_it():
    """Flat grass, tufted on every other column in a checkerboard -- so every
    step from any untufted column lands on a tuft -- and tufted all round the
    trunk. The old jar reported a tuft as the column's top: y 64, no
    collision."""
    surface = []
    for x in range(-RADIUS, RADIUS + 1):
        for z in range(-RADIUS, RADIUS + 1):
            if (x, z) == TRUNK:
                surface.append([x, 68, z, "minecraft:oak_log", True, 0])
            elif (x + z) % 2 == 0 or max(abs(x - TRUNK[0]),
                                         abs(z - TRUNK[1])) == 1:
                surface.append([x, 64, z, "minecraft:short_grass", False, 4])
            else:
                surface.append([x, 63, z, "minecraft:grass_block", True, 4])
    return surface


def in_ring(column, ring, centre=TRUNK):
    return max(abs(column[0] - centre[0]), abs(column[1] - centre[1])) == ring


class OldJarGrassTests(unittest.TestCase):
    """The jar the user has installed: fixed without reinstalling it."""

    def test_a_tuft_is_read_as_the_floor_it_grows_on(self):
        state = read(OLD, [[1, 64, 2, "minecraft:short_grass", False, 4]])
        block = state.surface[0]
        self.assertEqual((block.x, block.y, block.z), (1, 63, 2))
        self.assertEqual(block.cover, "short_grass")
        self.assertIsNot(block.solid, False)
        self.assertEqual(block.clearance, 4)

    def test_tall_grass_is_two_blocks_tall(self):
        state = read(OLD, [[1, 65, 2, "minecraft:tall_grass", False, 1]])
        block = state.surface[0]
        self.assertEqual(block.y, 63)
        self.assertEqual(block.clearance, 3, "its own two blocks are room")

    def test_a_vine_is_not_assumed_to_have_a_floor_under_it(self):
        """A vine can hang over a drop. Inventing a floor under it is how a
        route walks off a cliff."""
        state = read(OLD, [[1, 70, 2, "minecraft:vine", False, 4]])
        block = state.surface[0]
        self.assertEqual((block.y, block.name, block.solid),
                         (70, "vine", False))

    def test_the_meadow_from_the_run_has_a_route_to_its_tree(self):
        state = read(OLD, meadow_as_the_old_jar_saw_it(), trunk_logs())
        block = nav.nearest_block(state, "log", reachable_only=True)
        self.assertIsNotNone(block, "no walkable route across open grass")
        column = nav.approach_column(state, block)
        self.assertTrue(in_ring(column, 1), column)

    def test_it_walks_there(self):
        state = read(OLD, meadow_as_the_old_jar_saw_it(), trunk_logs())
        world = SimWorld(state.surface)
        world.notable = state.notable_blocks
        skill = skills.create("navigate_to", target="log")
        result = run(world, skill)
        self.assertFalse(skill.failed, skill.done_reason)
        moves = [r for r in result.records if r.step["action"] == "move"]
        self.assertTrue(moves, "zero steps, as in the run")
        self.assertLess(world.distance_to(TRUNK), 2.0)


def under_a_canopy(room=2, bushes=False):
    """A tree on flat grass, as the NEW jar reports it: under the leaves the
    floor is the grass, with `room` blocks before the canopy starts. The
    trunk's own column has no floor that fits a player, so the mod falls
    back to its topmost block and says there is no room on it."""
    surface = []
    for x in range(-RADIUS, RADIUS + 1):
        for z in range(-RADIUS, RADIUS + 1):
            ring = max(abs(x - TRUNK[0]), abs(z - TRUNK[1]))
            if ring == 0:
                surface.append([x, 68, z, "minecraft:oak_log", True, 0, None])
            elif ring <= 2:
                cover = ("minecraft:sweet_berry_bush"
                         if bushes and ring == 1 else None)
                surface.append([x, 63, z, "minecraft:grass_block", True,
                                room, cover])
            else:
                surface.append([x, 63, z, "minecraft:grass_block", True, 4,
                                "minecraft:short_grass" if (x + z) % 2
                                else None])
    return surface


class NewJarCanopyTests(unittest.TestCase):

    def setUp(self):
        aiming_mod.SHARED.reset()

    def test_the_new_jar_is_used_as_given(self):
        state = read(NEW, [[1, 64, 2, "minecraft:short_grass", False, 4,
                            None]])
        self.assertEqual(state.surface[0].y, 64,
                         "a /4 surface is already the floor; moving it "
                         "down again would sink the world a block")

    def test_cover_is_read(self):
        state = read(NEW, under_a_canopy(bushes=True), trunk_logs())
        covers = {b.cover for b in state.surface}
        self.assertIn("sweet_berry_bush", covers)
        self.assertIn("short_grass", covers)

    def test_a_tree_is_walked_up_to_under_its_leaves(self):
        state = read(NEW, under_a_canopy(), trunk_logs())
        world = SimWorld(state.surface)
        world.notable = state.notable_blocks
        skill = skills.create("navigate_to", target="log")
        run(world, skill)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertLess(world.distance_to(TRUNK), 2.0)

    def test_berry_bushes_are_walked_round_not_through(self):
        state = read(NEW, under_a_canopy(bushes=True), trunk_logs())
        local = nav.LocalMap.from_state(state)
        bushes = [(x, z) for (x, z) in local.ground
                  if local.cover_at(x, z) == "sweet_berry_bush"]
        self.assertEqual(len(bushes), 8)
        for column in bushes:
            self.assertFalse(local.standable(*column),
                             "standing in a berry bush hurts")

    def test_ringed_by_bushes_it_stands_further_out_within_reach(self):
        state = read(NEW, under_a_canopy(bushes=True), trunk_logs())
        log = nav.nearest_block(state, "log", reachable_only=True)
        self.assertIsNotNone(log, "nothing touching the trunk is safe, but "
                                  "the trunk is still reachable")
        column = nav.approach_column(state, log)
        self.assertTrue(in_ring(column, 2), column)
        local = nav.LocalMap.from_state(state)
        feet = (column[0] + 0.5, local.ground_at(*column) + 1,
                column[1] + 0.5)
        self.assertTrue(aiming_mod.SHARED.within_reach(feet, log.position))

    def test_a_log_seen_through_leaves_is_not_in_view(self):
        """Standing outside a canopy that comes down to head height, the
        lowest log is in reach and the leaves are in the way."""
        state = read(NEW, under_a_canopy(room=1), trunk_logs())
        local = nav.LocalMap.from_state(state)
        stand = (TRUNK[0] - 3, TRUNK[1])
        eye = (stand[0] + 0.5, 64 + aiming_mod.EYE_HEIGHT, stand[1] + 0.5)
        target = (TRUNK[0], 65, TRUNK[1])
        point = aiming_mod.target_point(target, (eye[0], 64, eye[2]))
        self.assertFalse(local.sight_is_clear(
            eye, point, skip=(stand, TRUNK)))
        # And with room under the leaves, the same line is clear.
        state = read(NEW, under_a_canopy(room=4), trunk_logs())
        self.assertTrue(nav.LocalMap.from_state(state).sight_is_clear(
            eye, point, skip=(stand, TRUNK)))


class ApproachRingTests(unittest.TestCase):

    def test_a_neighbour_is_still_preferred(self):
        state = state_from(flat(), notable=(NearbyBlock(5, 64, 0, "oak_log",
                                                        True),))
        column = nav.approach_column(state, state.notable_blocks[0])
        self.assertTrue(in_ring(column, 1), column)

    def test_out_of_reach_further_out_is_not_a_place_to_stand(self):
        """A log eight blocks up is not worth walking two columns out for;
        the neighbours are refused because they are water."""
        high = NearbyBlock(5, 72, 0, "oak_log", True)
        surface = [NearbyBlock(b.x, b.y, b.z, "water", False)
                   if in_ring((b.x, b.z), 1) else b for b in flat()]
        state = state_from(surface, notable=(high,))
        self.assertIsNone(nav.approach_column(state, high))

    def test_one_flood_agrees_with_the_path_search(self):
        state = read(NEW, under_a_canopy(bushes=True), trunk_logs())
        local = nav.LocalMap.from_state(state)
        reachable = nav.reachable_columns(local)
        for column in [(-3, 4), (7, 2), (5, 2), (4, 0), (6, 1)]:
            self.assertEqual(column in reachable,
                             nav.find_path(state, column).found, column)


class ShortOakWorld(LeafWorld):
    """A world kept as individual blocks and reported the way the new jar
    reports it.

    TreeWorld is a heightmap: a column's reported top is solid all the way
    down. Under a canopy that is exactly wrong -- there is air beneath the
    leaves, and breaking one changes what the next column looks like. So
    here the blocks are the truth, the crosshair is a ray through them, and
    the terrain scan is recomputed from them with the mod's rule (the floor
    nearest the feet with two free blocks above) on every read."""

    GROUND = 63

    def __init__(self, logs, leaves, **kwargs):
        super().__init__(flat(), logs, blocks=leaves, **kwargs)

    def solid_at(self, cell):
        if cell[1] <= self.GROUND:
            return "grass_block"
        for log in self.logs:
            if log.position == cell:
                return log.name
        block = self.blocks.get(cell)
        return block.name if block is not None else None

    def scan(self):
        feet = math.floor(self.y)
        out = []
        for x in range(-RADIUS, RADIUS + 1):
            for z in range(-RADIUS, RADIUS + 1):
                best = None
                for y in range(feet + 4, feet - 6, -1):
                    if (self.solid_at((x, y, z))
                            and not self.solid_at((x, y + 1, z))
                            and not self.solid_at((x, y + 2, z))):
                        distance = abs(y + 1 - feet)
                        if best is None or distance <= best[0]:
                            best = (distance, y)
                if best is None:
                    continue
                y = best[1]
                room = 0
                while room < 4 and not self.solid_at((x, y + 1 + room, z)):
                    room += 1
                out.append(NearbyBlock(x, y, z, self.solid_at((x, y, z)),
                                       True, room))
        return out

    def read(self):
        self.surface = self.scan()
        self.ground = {(b.x, b.z): b for b in self.surface}
        return LeafWorld.read(self)

    def crosshair(self):
        yaw, pitch = math.radians(self.yaw), math.radians(self.pitch)
        ux = -math.sin(yaw) * math.cos(pitch)
        uy = -math.sin(pitch)
        uz = math.cos(yaw) * math.cos(pitch)
        ex, ey, ez = self.x, self.y + self.EYE, self.z
        previous = (math.floor(ex), math.floor(ey), math.floor(ez))
        distance = 0.0
        while distance <= self.REACH:
            cell = (math.floor(ex + ux * distance),
                    math.floor(ey + uy * distance),
                    math.floor(ez + uz * distance))
            if cell != previous:
                name = self.solid_at(cell)
                if name is not None:
                    return cell, name, _entered_face(previous, cell)
                previous = cell
            distance += 0.02
        return None


def short_oak():
    """An oak with the shortest trunk the game grows -- four logs -- whose
    lowest leaves hang one block above the ground, at head height, two
    columns out all the way round. A third of oaks are this shape."""
    tx, tz = TRUNK
    logs = [NearbyBlock(tx, y, tz, "oak_log", True) for y in range(64, 68)]
    leaves = []
    for y, radius in ((65, 2), (66, 2), (67, 1), (68, 1)):
        for x in range(tx - radius, tx + radius + 1):
            for z in range(tz - radius, tz + radius + 1):
                if (x, z) == TRUNK and y < 68:
                    continue
                leaves.append(NearbyBlock(x, y, z, "oak_leaves", True))
    return logs, leaves


class ShortOakTests(unittest.TestCase):
    """Nowhere within two columns of a short oak has room to stand, and
    from three out its logs are behind leaves. It used to say "no walkable
    route"; it should walk up and cut its way in."""

    def setUp(self):
        aiming_mod.SHARED.reset()

    def test_it_finds_somewhere_to_stand(self):
        world = ShortOakWorld(*short_oak(), inventory={"oak_log": 0})
        state = world.read()
        log = nav.nearest_block(state, "log", reachable_only=True)
        self.assertIsNotNone(log, "a short oak is still 'unreachable'")
        self.assertTrue(in_ring(nav.approach_column(state, log), 3))

    def test_it_cuts_through_the_leaves_and_gets_a_log(self):
        world = ShortOakWorld(*short_oak(), inventory={"oak_log": 0})
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=45)
        self.assertEqual(len(world.broken), 1, skill.done_reason)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.wrong_block_swings, 0)
        self.assertTrue(world.leaves_broken)
        self.assertLessEqual(len(world.leaves_broken),
                             skills.MAX_LEAVES_PER_LOG)
        clears = [r for r in result.records
                  if r.step.get("note", "").startswith("clear ")]
        self.assertEqual(len(clears), len(world.leaves_broken))

    def test_a_stone_wall_is_still_not_seen_through(self):
        """Only leaves are let through: they are what collect_logs clears."""
        logs, leaves = short_oak()
        stone = [NearbyBlock(b.x, b.y, b.z, "stone", True) for b in leaves]
        world = ShortOakWorld(logs, stone, inventory={"oak_log": 0})
        self.assertIsNone(nav.nearest_block(world.read(), "log",
                                            reachable_only=True))


class OutdatedJarTests(unittest.TestCase):
    """Only the new jar sees under leaves. Someone still running the old one
    should be told, in the line every task already prints, not left to
    wonder why a tree has "no walkable route"."""

    @staticmethod
    def source(schema):
        text = json.dumps({"schema": schema, "written_at_ms": NOW_MS,
                           "in_game": True})
        return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                    clock=lambda: NOW_MS / 1000.0)

    def test_the_old_jar_is_named_in_the_task_log(self):
        from actions import minecraft as mc_actions
        source = self.source(OLD)
        self.assertTrue(source.outdated())
        label = mc_actions._source_label(source)
        self.assertIn("OLDER", label)
        self.assertIn("install_mod.bat", label)

    def test_the_current_jar_is_not(self):
        from actions import minecraft as mc_actions
        source = self.source(NEW)
        self.assertFalse(source.outdated())
        self.assertNotIn("OLDER", mc_actions._source_label(source))


if __name__ == "__main__":
    unittest.main(verbosity=2)
