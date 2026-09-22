"""
Tests for minecraft/navigation.py and the navigate_to skill.

WHAT THESE ARE FOR

  1. An unknown column is not air. The single most dangerous confusion in
     this subsystem: treating "the scan did not reach there" as "that is
     empty and walkable" turns a missing reading into a confident walk off a
     cliff. Several tests here exist only to hold that line.

  2. Failing to find a route is a result, not an error to paper over. A
     destination behind a wall with no gap must come back as "no route",
     never as a path that goes through the wall or a hopeful heading.

  3. The skill reaches a real destination in a simulated world. Pathfinding
     that works on paper and a skill that gets somewhere are different
     claims, so SimWorld below actually moves a player in response to the
     steps the skill returns, with walls that block and a camera that turns.

  4. The planner never touches input. navigate_to returns Steps naming
     actions from the runner's fixed table; it has no controller, and the
     test asserts the actions it emits are all dispatchable.

WHAT THEY DO NOT PROVE
     Nothing here proves the real game behaves like SimWorld. Walk speed,
     collision and the mod's scan are approximated. These tests prove the
     logic is sound given a world that behaves as described; only playing
     the game proves the description is right.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import action_spec                                    # noqa: E402
from minecraft import navigation as nav                               # noqa: E402
from minecraft import skills, verification as verify_mod              # noqa: E402
from minecraft.controller import ActionResult                          # noqa: E402
from minecraft.state import (                                          # noqa: E402
    BlockRef, EXACT, NearbyBlock, EntityRef, UNKNOWN, WorldState, empty_state,
)
from minecraft.task_runner import DISPATCH, TaskRunner                 # noqa: E402


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


def wall_at(surface, x, height=66, gap_z=None):
    """Raise a column of blocks into a wall, optionally leaving one gap."""
    out = []
    for block in surface:
        if block.x == x and block.z != gap_z:
            out.append(NearbyBlock(block.x, height, block.z, "stone", True))
        else:
            out.append(block)
    return out


# ── The map ──────────────────────────────────────────────────────────────────

class LocalMapTests(unittest.TestCase):

    def test_unknown_column_is_not_walkable(self):
        """The line this whole module is built to hold."""
        local = nav.LocalMap.from_state(state_from(flat(radius=3)))
        self.assertTrue(local.standable(2, 2))
        self.assertFalse(local.is_known(50, 50))
        self.assertFalse(local.standable(50, 50))

    def test_unreadable_state_gives_an_unusable_map(self):
        local = nav.LocalMap.from_state(empty_state("nothing"))
        self.assertFalse(local.usable)
        self.assertEqual(local.known_columns, 0)
        self.assertFalse(local.standable(0, 0))

    def test_a_missing_column_is_omitted_not_guessed(self):
        """A hole in the scan stays a hole. The bridge omits columns with no
        solid block, and nothing downstream may fill them in."""
        surface = [b for b in flat(radius=3) if (b.x, b.z) != (1, 0)]
        local = nav.LocalMap.from_state(state_from(surface))
        self.assertFalse(local.is_known(1, 0))
        self.assertIsNone(local.ground_at(1, 0))

    def test_hazard_is_not_standable(self):
        surface = [b for b in flat(radius=3) if (b.x, b.z) != (1, 1)]
        surface.append(NearbyBlock(1, 63, 1, "lava", True))
        local = nav.LocalMap.from_state(state_from(surface))
        self.assertFalse(local.standable(1, 1))

    def test_step_up_of_one_is_allowed_and_two_is_not(self):
        surface = [b for b in flat(radius=3) if b.x not in (1, 2)]
        surface.append(NearbyBlock(1, 64, 0, "dirt", True))   # +1
        surface.append(NearbyBlock(2, 66, 0, "dirt", True))   # +2 from there
        local = nav.LocalMap.from_state(state_from(surface))
        self.assertIsNotNone(local.step_cost((0, 0), (1, 0)))
        self.assertIsNone(local.step_cost((1, 0), (2, 0)))

    def test_a_long_drop_is_refused(self):
        surface = [b for b in flat(radius=3) if b.x != 1]
        surface.append(NearbyBlock(1, 63 - nav.MAX_DROP - 1, 0, "stone", True))
        local = nav.LocalMap.from_state(state_from(surface))
        self.assertIsNone(local.step_cost((0, 0), (1, 0)))

    def test_diagonal_needs_both_orthogonals(self):
        """Minecraft will not let you cut a corner between two walls, and a
        planner that thinks otherwise plans a route the player cannot walk."""
        surface = [b for b in flat(radius=3) if (b.x, b.z) not in ((1, 0), (0, 1))]
        local = nav.LocalMap.from_state(state_from(surface))
        neighbours = {c for c, _cost in local.neighbours((0, 0))}
        self.assertNotIn((1, 1), neighbours)


# ── Pathfinding ──────────────────────────────────────────────────────────────

class PathfindingTests(unittest.TestCase):

    def test_straight_line_across_a_clearing(self):
        path = nav.find_path(state_from(flat()), (4, 0))
        self.assertTrue(path.found)
        self.assertEqual(path.waypoints[-1][0], 4)
        self.assertLessEqual(len(path.waypoints), 5)

    def test_routes_around_a_wall_through_its_gap(self):
        surface = wall_at(flat(), x=2, gap_z=0)
        path = nav.find_path(state_from(surface), (5, 4))
        self.assertTrue(path.found)
        crossings = [(x, z) for x, _y, z in path.waypoints if x == 2]
        self.assertTrue(crossings, "the route never crosses the wall line")
        for _x, z in crossings:
            self.assertEqual(z, 0, "the route went through solid stone")

    def test_a_walled_off_destination_is_reported_unreachable(self):
        surface = wall_at(flat(), x=2)          # no gap at all
        path = nav.find_path(state_from(surface), (5, 0))
        self.assertFalse(path.found)
        self.assertIn("route", path.reason.lower())
        self.assertEqual(path.waypoints, ())

    def test_a_destination_outside_the_scan_is_refused_not_guessed(self):
        path = nav.find_path(state_from(flat()), (400, 400))
        self.assertFalse(path.found)
        self.assertIn("see", path.reason.lower())

    def test_search_is_bounded(self):
        path = nav.find_path(state_from(flat(radius=8)), (8, 8), max_nodes=5)
        self.assertLessEqual(path.nodes_expanded, 6)
        self.assertFalse(path.found)

    def test_no_map_means_no_path(self):
        path = nav.find_path(empty_state("nothing"), (1, 1))
        self.assertFalse(path.found)


# ── Heading ──────────────────────────────────────────────────────────────────

class HeadingTests(unittest.TestCase):

    def setUp(self):
        nav.reset_calibration()
        self.addCleanup(nav.reset_calibration)

    def test_yaw_matches_minecraft_convention(self):
        """0 = south (+Z), 90 = west (-X), ±180 = north (-Z), -90 = east."""
        origin = (0.0, 64.0, 0.0)
        self.assertAlmostEqual(nav.yaw_to(origin, (0, 64, 5)), 0.0, places=3)
        self.assertAlmostEqual(nav.yaw_to(origin, (-5, 64, 0)), 90.0, places=3)
        self.assertAlmostEqual(abs(nav.yaw_to(origin, (0, 64, -5))), 180.0, places=3)
        self.assertAlmostEqual(nav.yaw_to(origin, (5, 64, 0)), -90.0, places=3)

    def test_difference_takes_the_short_way_round(self):
        self.assertAlmostEqual(nav.yaw_difference(170.0, -170.0), 20.0, places=3)
        self.assertAlmostEqual(nav.yaw_difference(-170.0, 170.0), -20.0, places=3)

    def test_look_delta_follows_the_yaw_it_is_asked_for(self):
        """Positive dx is mouse-right, and turning right increases yaw:
        facing south (0) and turning right faces west (90).

        DERIVED, NOT MEASURED — the pixels-per-degree figure and this sign
        both come from the documented convention rather than from watching
        the real game, which is why NavigateTo measures its first turn and
        flips the sign if the aim got worse. See _check_turn_direction."""
        self.assertGreater(nav.look_delta_for(0.0, 45.0), 0)
        self.assertLess(nav.look_delta_for(0.0, -45.0), 0)
        self.assertAlmostEqual(nav.look_delta_for(0.0, 10.0),
                               round(10 * nav.PIXELS_PER_DEGREE))


# ── World queries ────────────────────────────────────────────────────────────

class WorldQueryTests(unittest.TestCase):

    def setUp(self):
        self.log = NearbyBlock(4, 64, 3, "birch_log", True)
        self.far = NearbyBlock(-7, 64, -7, "oak_log", True)
        self.state = state_from(
            flat(), notable=(self.far, self.log),
            entities=(EntityRef(name="zombie", distance=9.0,
                                position=(9.0, 64.0, 0.0), category="hostile"),
                      EntityRef(name="cow", distance=3.0,
                                position=(3.0, 64.0, 0.0), category="passive")))

    def test_nearest_block_picks_the_near_one(self):
        self.assertIs(nav.nearest_block(self.state, "log"), self.log)

    def test_category_falls_back_to_a_literal_name(self):
        found = nav.blocks_in_category(self.state, "birch_log")
        self.assertEqual([b.name for b in found], ["birch_log"])

    def test_nearest_entity_filters_by_category(self):
        self.assertEqual(nav.nearest_entity(self.state, "hostile").name, "zombie")
        self.assertEqual(nav.nearest_entity(self.state, "passive").name, "cow")
        self.assertIsNone(nav.nearest_entity(self.state, "player"))

    def test_approach_column_is_beside_the_block_not_inside_it(self):
        column = nav.approach_column(self.state, self.log)
        self.assertIsNotNone(column)
        self.assertNotEqual(column, (self.log.x, self.log.z))
        self.assertLessEqual(max(abs(column[0] - self.log.x),
                                 abs(column[1] - self.log.z)), 1)

    def test_approach_column_is_none_when_the_block_is_cut_off(self):
        surface = wall_at(flat(), x=2)
        state = state_from(surface, notable=(self.log,))
        self.assertIsNone(nav.approach_column(state, self.log))

    def test_reachable_only_skips_a_nearer_unreachable_block(self):
        near_but_walled = NearbyBlock(4, 64, 0, "oak_log", True)
        surface = wall_at(flat(), x=2, gap_z=None)
        # Re-open the world on the near side only, so the far log is the one
        # with a route: the wall separates x>2 entirely.
        state = state_from(surface, notable=(near_but_walled, self.far))
        self.assertIs(nav.nearest_block(state, "log"), near_but_walled)
        self.assertIs(nav.nearest_block(state, "log", reachable_only=True),
                      self.far)

    def test_summarise_hides_the_voxels(self):
        summary = nav.summarise(self.state)
        self.assertEqual(summary["nearest_log"]["name"], "birch_log")
        self.assertEqual(summary["nearest_hostile"]["name"], "zombie")
        self.assertEqual(summary["columns_seen"], 17 * 17)
        text = repr(summary)
        self.assertLess(len(text), 1000,
                        "a summary that long is raw data with extra steps")

    def test_ores_are_found_by_suffix(self):
        state = state_from(flat(), notable=(
            NearbyBlock(1, 60, 1, "deepslate_diamond_ore", True),
            NearbyBlock(2, 60, 1, "some_modded_ore", True),
            NearbyBlock(3, 60, 1, "stone", True)))
        self.assertEqual({b.name for b in nav.ores(state)},
                         {"deepslate_diamond_ore", "some_modded_ore"})


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
            # The body is 0.6 wide. A shoulder hits a wall a centre-line
            # misses, so every corner of the footprint has to fit.
            for ox in (-self.HALF_WIDTH, self.HALF_WIDTH):
                for oz in (-self.HALF_WIDTH, self.HALF_WIDTH):
                    corner = (math.floor(nx + ox), math.floor(nz + oz))
                    other = self.ground.get(corner)
                    if other is None:
                        self.blocked += 1
                        return
                    if (other.y + 1) - self.y > self.STEP_HEIGHT \
                            and not (jumping and not rose):
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
            self.x, self.z = nx, nz
            travelled += stride

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


class NavigateToTests(unittest.TestCase):

    def test_walks_across_an_open_clearing(self):
        world = SimWorld(flat())
        result = run(world, skills.create("navigate_to", destination=(6, 0)))
        self.assertLess(world.distance_to((6, 0)), 2.0,
                        f"ended {world.distance_to((6, 0)):.1f} blocks away; "
                        f"{result.reason}")

    def test_walks_round_a_wall(self):
        world = SimWorld(wall_at(flat(), x=3, gap_z=0),
                         position=(0.5, 64.0, 4.5))
        result = run(world, skills.create("navigate_to", destination=(6, 4)))
        self.assertLess(world.distance_to((6, 4)), 2.5,
                        f"ended {world.distance_to((6, 4)):.1f} blocks away; "
                        f"{result.reason}")

    def test_walks_to_the_nearest_log(self):
        world = SimWorld(flat())
        world.notable = (NearbyBlock(5, 64, 5, "oak_log", True),)
        run(world, skills.create("navigate_to", target="log"))
        self.assertLess(math.dist((world.x, world.z), (5.5, 5.5)), 3.0)

    def test_an_unreachable_destination_stops_immediately(self):
        world = SimWorld(wall_at(flat(), x=2))
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        self.assertTrue(skill.failed)
        self.assertLessEqual(result.steps_taken, 1)
        self.assertLess(world.distance_to((0, 0)), 1.0,
                        "it walked somewhere despite having no route")

    def test_a_destination_it_cannot_see_is_refused(self):
        world = SimWorld(flat())
        skill = skills.create("navigate_to", destination=(300, 300))
        run(world, skill)
        self.assertTrue(skill.failed)
        self.assertIn("outside what I can see", skill.done_reason)

    def test_no_world_state_means_it_says_so_and_does_nothing(self):
        class Blind(SimWorld):
            def read(self):
                self.reads += 1
                return empty_state("no bridge")

        world = Blind(flat())
        skill = skills.create("navigate_to", destination=(4, 0))
        result = run(world, skill)
        self.assertTrue(skill.failed)
        self.assertEqual(skill.done_reason, skills.CANNOT_SEE_WORLD)
        self.assertEqual(result.steps_taken, 0)

    def test_it_never_asks_for_an_action_the_runner_does_not_know(self):
        """The planner boundary, for this skill specifically."""
        world = SimWorld(wall_at(flat(), x=3, gap_z=0))
        result = run(world, skills.create("navigate_to", destination=(6, 4)))
        actions = {r.step["action"] for r in result.records}
        self.assertTrue(actions)
        self.assertTrue(actions <= set(DISPATCH),
                        f"unknown actions: {actions - set(DISPATCH)}")

    def test_it_gives_up_when_it_stops_making_ground(self):
        """A player who cannot move — stuck in a boat, held by a mob, or a
        simulation that refuses to walk — must end the task, not spend the
        whole budget pressing W."""
        class Stuck(SimWorld):
            def move(self, params):
                self.blocked += 1
                return self._result("move", params, 200)

        world = Stuck(flat())
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        self.assertTrue(skill.failed)
        self.assertLess(result.steps_taken, 20,
                        "it kept walking into the same obstacle")
        self.assertTrue(skill.done_reason)

    def test_movement_steps_do_not_overshoot_the_waypoint(self):
        world = SimWorld(flat())
        result = run(world, skills.create("navigate_to", destination=(2, 0)))
        for record in result.records:
            if record.step["action"] == "move":
                seconds = record.step["params"]["duration"]
                self.assertLessEqual(
                    seconds * SimWorld.WALK_SPEED, 9.0,
                    "a single step crossing that far will sail past waypoints")

    def test_it_survives_a_backwards_mouse_sign(self):
        """The bug that made JARVIS spin in circles, made harmless.

        If positive dx turns the camera LEFT on this machine — a different
        sensitivity setting, a mod, an inverted axis — then every correction
        doubles the error and the agent turns forever. The skill measures its
        first turn and flips the sign, so a wrong convention costs one step
        rather than the whole task."""
        class Inverted(SimWorld):
            def look(self, params):
                params = dict(params)
                params["dx"] = -params.get("dx", 0)
                return SimWorld.look(self, params)

        world = Inverted(flat())
        skill = skills.create("navigate_to", destination=(5, 5))
        result = run(world, skill)
        self.assertLess(nav.calibration()["yaw_px_per_degree"], 0,
                        "it never measured the flip")
        self.assertFalse(skill.failed, result.reason)
        self.assertLess(world.distance_to((5, 5)), 2.0)

    def test_a_long_walk_says_how_far_it_got_and_resumes(self):
        """A route longer than one task's step budget is a normal outcome,
        not a failure. The result has to say where it stopped, because that
        is what makes running it again an obvious move rather than a guess."""
        surface = wall_at(flat(radius=8), x=3, gap_z=8)
        world = SimWorld(surface, position=(-6.5, 64.0, -6.5))
        first = skills.create("navigate_to", destination=(8, -8))
        run(world, first, max_steps=3)          # deliberately too few
        self.assertTrue(first.failed)
        self.assertIn("blocks short of", first.done_reason)
        self.assertIn("carries on", first.done_reason)
        stopped_at = math.dist((world.x, world.z), (8.5, -7.5))

        # Running it again picks up from the new position and gets closer.
        for _ in range(3):
            again = skills.create("navigate_to", destination=(8, -8))
            run(world, again, max_steps=3)
            if not again.failed:
                break
        self.assertLess(math.dist((world.x, world.z), (8.5, -7.5)),
                        stopped_at, "a second run made no ground")

    def test_a_straight_line_is_one_move_not_one_per_block(self):
        """The bug that made the step limit bite.

        A* returns one-block hops because that is what the grid is made of.
        Walking them individually spent a step per block, so an eleven-block
        stroll used nine steps of a twenty-step budget having done nothing
        interesting. A clear straight line is one move."""
        world = SimWorld(flat(radius=8))
        result = run(world, skills.create("navigate_to", destination=(8, 8)))
        moves = [r for r in result.records if r.step["action"] == "move"]
        self.assertLessEqual(len(moves), 3,
                             f"{len(moves)} moves to walk a straight line")
        self.assertLess(world.distance_to((8, 8)), 2.0)

    def test_smoothing_never_cuts_through_something_solid(self):
        """Collapsing waypoints must not relax the rules the search used.

        The whole value of the pathfinder is that it refuses routes a player
        cannot walk; a shortcut that ignores the wall would hand that back."""
        surface = wall_at(flat(radius=8), x=3, gap_z=8)
        local = nav.LocalMap.from_state(state_from(surface))
        # Straight across the wall line, away from the gap.
        self.assertFalse(local.line_is_walkable((0, 0), (6, 0)))
        # Along the open side.
        self.assertTrue(local.line_is_walkable((-6, 0), (-1, 0)))
        # Through the gap at z=8.
        self.assertTrue(local.line_is_walkable((1, 8), (5, 8)))

    def test_it_measures_the_mouse_instead_of_trusting_the_default(self):
        """A machine whose sensitivity is nothing like the default.

        SimWorld here turns a quarter as far per pixel as navigation.py
        assumes. Without calibration every heading undershoots and the walk
        spends its budget correcting; with it, the second turn is right."""
        class Heavy(SimWorld):
            SCALE = 4.0          # four times as many pixels per degree

            def look(self, params):
                # Clamp first, exactly as the real path does, THEN apply this
                # machine's heavier sensitivity to what actually arrives.
                params = {"dx": self._clamp(params.get("dx", 0)) / self.SCALE,
                          "dy": self._clamp(params.get("dy", 0)) / self.SCALE}
                return SimWorld.look(self, params)

        world = Heavy(flat(radius=8))
        skill = skills.create("navigate_to", destination=(6, 6))
        result = run(world, skill)
        self.addCleanup(nav.reset_calibration)
        self.assertAlmostEqual(nav.pixels_per_degree(),
                               nav.PIXELS_PER_DEGREE * Heavy.SCALE,
                               delta=8.0)
        self.assertFalse(skill.failed, result.reason)
        self.assertLess(world.distance_to((6, 6)), 2.0)

    def test_standing_off_the_map_is_a_different_answer_from_no_route(self):
        """The scan is centred on the player, so this should not happen. If
        it does, "no walkable route" would send everyone looking for a wall
        that is not there."""
        surface = [b for b in flat(radius=3) if (b.x, b.z) != (0, 0)]
        state = state_from(surface, position=(0.5, 64.0, 0.5))
        path = nav.find_path(state, (2, 2))
        self.assertFalse(path.found)
        self.assertIn("standing on", path.reason)

    def test_it_reports_arrival_only_when_it_arrived(self):
        world = SimWorld(flat())
        skill = skills.create("navigate_to", destination=(4, 0))
        run(world, skill)
        self.assertFalse(skill.failed)
        self.assertIn("arrived", skill.done_reason)


class TreeWorld(SimWorld):
    """SimWorld plus trees you can walk to, aim at and break.

    Mining only works when the player is within reach AND actually pointing
    at the log, because those are the two things that went wrong in real
    play: swinging from too far away, and swinging at the sky."""

    REACH = 4.5
    AIM_TOLERANCE = 25.0

    def __init__(self, surface, logs, **kwargs):
        super().__init__(surface, **kwargs)
        self.logs = list(logs)
        self.broken = []
        self.swings = 0

    @property
    def notable(self):
        return tuple(self.logs)

    @notable.setter
    def notable(self, value):
        self.logs = list(value)

    def mine(self, params):
        self.swings += 1
        seconds = float(params.get("duration", 0))
        here = (self.x, self.y, self.z)
        for log in list(self.logs):
            if log.distance_to(here) > self.REACH:
                continue
            _dx, _dy, error = nav.aim_at(here, (self.yaw, self.pitch),
                                         log.position)
            if error > self.AIM_TOLERANCE:
                continue
            if seconds < skills.MIN_USEFUL_MINE_S:
                break          # a tap does not break a log, by design
            self.logs.remove(log)
            self.broken.append(log)
            break
        return self._result("mine", params, int(seconds * 1000))


class CollectLogsWithAMapTests(unittest.TestCase):
    """The task the user actually ran, which used to turn on the spot."""

    def setUp(self):
        self.log = NearbyBlock(6, 64, 0, "oak_log", True)

    def test_it_walks_to_a_tree_instead_of_spinning(self):
        world = TreeWorld(flat(), [self.log])
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=40)
        self.assertEqual(len(world.broken), 1,
                         f"never broke the log. {skill.done_reason}")
        self.assertLess(math.dist((world.x, world.z), (6.5, 0.5)), 5.0,
                        "it never got near the tree")
        self.assertFalse(skill.failed, result.reason)

    def test_the_scan_is_what_confirms_the_log_broke(self):
        """Without an inventory, a log vanishing from the scan is the
        evidence — not the crosshair, which this world cannot read at all."""
        world = TreeWorld(flat(), [self.log])
        result = run(world, skills.create("collect_logs", count=1))
        verdicts = [r.verification for r in result.records
                    if r.step["action"] == "mine"]
        self.assertTrue(verdicts, "it never tried to mine")
        self.assertTrue(
            any(v["status"] == verify_mod.SUCCESS for v in verdicts),
            f"no mining step was ever confirmed: {verdicts}")

    def test_every_mining_step_holds_long_enough_to_break_a_log(self):
        """The bug that made it swing forever: Minecraft throws away breaking
        progress the moment the button comes up, so a one-second hold breaks
        nothing no matter how many times it repeats."""
        world = TreeWorld(flat(), [self.log])
        result = run(world, skills.create("collect_logs", count=1))
        holds = [r.step["params"]["duration"] for r in result.records
                 if r.step["action"] == "mine"]
        self.assertTrue(holds, "it never tried to mine")
        for seconds in holds:
            self.assertGreaterEqual(seconds, skills.MIN_USEFUL_MINE_S)

    def test_it_collects_more_than_one(self):
        logs = [NearbyBlock(5, 64, 0, "oak_log", True),
                NearbyBlock(-5, 64, 2, "birch_log", True),
                NearbyBlock(0, 64, 6, "spruce_log", True)]
        world = TreeWorld(flat(), logs)
        skill = skills.create("collect_logs", count=3)
        result = run(world, skill, max_steps=60)
        self.assertEqual(len(world.broken), 3,
                         f"broke {len(world.broken)}. {skill.done_reason}")
        self.assertFalse(skill.failed, result.reason)

    def test_it_gives_up_on_a_tree_it_can_see_but_cannot_walk_to(self):
        """The walking half must be able to stop the whole task.

        CollectLogs delegates movement to NavigateTo, so NavigateTo's stall
        detection has to reach it — if the walker is fed a blank history it
        never counts a stall and the task walks into the same wall until the
        step limit."""
        class Immovable(TreeWorld):
            def move(self, params):
                return self._result("move", params, 200)   # never moves

        world = Immovable(flat(), [NearbyBlock(8, 64, 0, "oak_log", True)])
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=40)
        self.assertTrue(skill.failed)
        self.assertEqual(world.broken, [])
        self.assertLess(result.steps_taken, 8,
                        "it kept walking at a tree it was never reaching")
        # Not merely "no closer": open ground and no movement is a specific
        # failure — the input is not landing — and saying so sends the user
        # to the window focus rather than looking for a wall that is not
        # there.
        self.assertIn("did not move", skill.done_reason)
        self.assertIn("window in front", skill.done_reason)

    def test_the_last_log_is_counted_even_at_the_step_limit(self):
        """Progress is tallied at the start of each plan(), so the final
        step's verdict lands after the last tally. Without a final
        accounting, a run that broke its fourth log reports three — which
        reads as a failure and is really an off-by-one."""
        logs = [NearbyBlock(4, 64, 0, "oak_log", True),
                NearbyBlock(-4, 64, 0, "oak_log", True)]
        world = TreeWorld(flat(), logs)
        skill = skills.create("collect_logs", count=2)
        # Exactly enough steps to break both and not one more.
        probe = run(TreeWorld(flat(), list(logs)),
                    skills.create("collect_logs", count=2), max_steps=45)
        result = run(world, skill, max_steps=probe.steps_taken)
        self.assertEqual(len(world.broken), 2)
        self.assertIn("2 of 2", skill.done_reason)

    def test_it_says_so_when_the_scan_shows_no_trees(self):
        world = TreeWorld(flat(), [])
        skill = skills.create("collect_logs", count=1)
        run(world, skill)
        self.assertTrue(skill.failed)
        self.assertIn("no logs", skill.done_reason)
        self.assertIn("terrain scan", skill.done_reason)
        self.assertEqual(world.swings, 0, "it swung at nothing")

    def test_it_does_not_claim_a_tree_it_cannot_reach(self):
        world = TreeWorld(wall_at(flat(), x=3),
                          [NearbyBlock(6, 64, 0, "oak_log", True)])
        skill = skills.create("collect_logs", count=1)
        run(world, skill)
        self.assertTrue(skill.failed)
        self.assertIn("route", skill.done_reason)
        self.assertEqual(world.broken, [])

    def test_it_falls_back_to_the_crosshair_without_a_scan(self):
        """No mod, no terrain — the old behaviour must still run."""
        class NoScan(TreeWorld):
            def read(self):
                self.reads += 1
                return WorldState(position=(self.x, self.y, self.z),
                                  rotation=(self.yaw, self.pitch),
                                  target_block=BlockRef(name="oak_log"),
                                  source="test", confidence=EXACT)

        world = NoScan(flat(), [self.log])
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill, max_steps=6)
        self.assertIn("sweeping the crosshair", skill.done_reason)
        self.assertTrue(any(r.step["action"] == "mine"
                            for r in result.records),
                        "the fallback never mined")


class FindBlockWithAMapTests(unittest.TestCase):

    def test_it_reports_where_the_block_is_not_just_that_it_saw_one(self):
        world = SimWorld(flat())
        world.notable = (NearbyBlock(5, 64, 5, "birch_log", True),)
        skill = skills.create("find_block")
        run(world, skill, max_steps=8)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertIn("birch_log", skill.done_reason)
        self.assertIn("(5, 64, 5)", skill.done_reason)
        self.assertIsNotNone(skill.located)

    def test_a_scanned_absence_is_reported_as_a_scanned_absence(self):
        """"I looked at 289 columns and there is no oak log" is a different
        claim from "the crosshair did not cross one", and the skill must not
        present the weaker evidence as the stronger."""
        world = SimWorld(flat())
        world.notable = ()
        skill = skills.create("find_block")
        run(world, skill, max_steps=8)
        self.assertTrue(skill.failed)
        self.assertIn("columns", skill.done_reason)
        self.assertNotIn("crosshair", skill.done_reason)


class SweepingAndCalibrationTests(unittest.TestCase):
    """Turning the right amount on a machine that is not mine."""

    def setUp(self):
        nav.reset_calibration()
        self.addCleanup(nav.reset_calibration)

    def test_a_sweep_is_measured_in_degrees_not_pixels(self):
        """100 pixels is 12 degrees on one machine and 50 on another. A sweep
        sized in pixels either crawls or jumps straight past the tree."""
        default = skills._sweep_pixels()
        self.assertAlmostEqual(default,
                               round(skills.SWEEP_DEGREES
                                     * nav.PIXELS_PER_DEGREE))
        nav.calibrate(400, 20.0)          # this machine: 20 px/degree
        self.assertAlmostEqual(skills._sweep_pixels(),
                               round(skills.SWEEP_DEGREES * 20.0))

    def test_the_runner_learns_the_mouse_from_any_turn(self):
        """Not just from navigating. A survey turns too, and the measurement
        is a fact about the hardware, not about one skill's plan."""
        class Heavy(SimWorld):
            SCALE = 3.0

            def look(self, params):
                params = {"dx": self._clamp(params.get("dx", 0)) / self.SCALE,
                          "dy": self._clamp(params.get("dy", 0)) / self.SCALE}
                return SimWorld.look(self, params)

        world = Heavy(flat())
        run(world, skills.create("survey", steps=3))
        self.assertAlmostEqual(nav.pixels_per_degree(),
                               nav.PIXELS_PER_DEGREE * Heavy.SCALE,
                               delta=4.0)

    def test_a_turn_that_did_not_happen_teaches_nothing(self):
        """A look that moved nothing would measure an infinite sensitivity
        and poison every later turn."""
        nav.calibrate(400, 0.0)
        nav.calibrate(400, 0.5)
        self.assertEqual(nav.pixels_per_degree(), nav.PIXELS_PER_DEGREE)

    def test_an_absurd_reading_is_discarded(self):
        self.assertIsNone(nav.calibrate(40, 1000.0))
        self.assertEqual(nav.pixels_per_degree(), nav.PIXELS_PER_DEGREE)

    def test_the_sweep_says_it_is_working_blind(self):
        """A person reading the log must be able to tell a crosshair sweep
        from navigating — they are the same action and a different world."""
        class NoScan(SimWorld):
            def read(self):
                return WorldState(position=(self.x, self.y, self.z),
                                  rotation=(self.yaw, self.pitch),
                                  target_block=BlockRef(name="stone"),
                                  source="test", confidence=EXACT)

        world = NoScan(flat())
        result = run(world, skills.create("collect_logs", count=1),
                     max_steps=3)
        notes = " ".join(r.step["note"] for r in result.records)
        self.assertIn("no terrain scan", notes)


class AimingConvergesTests(unittest.TestCase):
    """The failure that burned 43 steps looking at one log.

    It aimed at the same oak log forty times and never settled. Aiming was
    open loop: it computed a correction from an assumed mouse scale, sent it,
    and never checked what actually happened. Get the scale slightly high, or
    either axis' sign backwards, and the loop cannot converge — it overshoots,
    corrects, overshoots the other way, forever.
    """

    def setUp(self):
        nav.reset_calibration()
        self.addCleanup(nav.reset_calibration)

    def machine(self, yaw_scale=1.0, pitch_scale=1.0):
        """A TreeWorld whose mouse behaves unlike the assumed default."""
        outer = self

        class Machine(TreeWorld):
            def look(self, params):
                params = {
                    "dx": self._clamp(params.get("dx", 0)) * yaw_scale,
                    "dy": self._clamp(params.get("dy", 0)) * pitch_scale,
                }
                return SimWorld.look(self, params)

        # A log at eye level, two blocks away: needs both axes to be right.
        return Machine(flat(), [NearbyBlock(2, 64, 0, "oak_log", True)],
                       position=(0.5, 64.0, 0.5), yaw=180.0)

    def test_it_settles_on_a_machine_with_an_inverted_pitch(self):
        """Pitch had NO self-correction at all — the sign flag only ever
        covered yaw. An aim that is right sideways and upside down
        vertically never lands on the block."""
        world = self.machine(pitch_scale=-1.0)
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill)
        self.assertEqual(len(world.broken), 1,
                         f"never broke it. {skill.done_reason}")
        self.assertLess(nav.calibration()["pitch_px_per_degree"], 0,
                        "it never measured the inverted pitch")

    def test_it_settles_when_both_axes_are_inverted(self):
        world = self.machine(yaw_scale=-1.0, pitch_scale=-1.0)
        skill = skills.create("collect_logs", count=1)
        run(world, skill)
        self.assertEqual(len(world.broken), 1, skill.done_reason)

    def test_it_settles_when_the_mouse_is_far_more_sensitive(self):
        """The overshoot case. Four times the assumed movement per pixel
        means every full correction sails past by three times the error."""
        world = self.machine(yaw_scale=4.0, pitch_scale=4.0)
        skill = skills.create("collect_logs", count=1)
        run(world, skill)
        self.assertEqual(len(world.broken), 1, skill.done_reason)

    def test_aiming_does_not_eat_the_whole_budget(self):
        """Even unconvergeable, it must stop turning and say so. Forty
        looks at one log is not persistence, it is a loop with a step limit
        for a brake."""
        class Stubborn(TreeWorld):
            def look(self, params):
                # Turns a little, never towards anything useful.
                return SimWorld.look(self, {"dx": 30, "dy": 0})

        world = Stubborn(flat(), [NearbyBlock(2, 66, 0, "oak_log", True)],
                         position=(0.5, 64.0, 0.5))
        skill = skills.create("collect_logs", count=1)
        result = run(world, skill)
        looks = sum(1 for r in result.records if r.step["action"] == "look")
        self.assertLessEqual(looks, 12,
                             f"{looks} looks without settling")
        self.assertTrue(skill._aim_gave_up,
                        "it never admitted the aim was not settling")

    def test_it_reports_an_aim_it_never_settled(self):
        """Swinging anyway is fine. Reporting it as a clean hit is not."""
        class Stubborn(TreeWorld):
            def look(self, params):
                return SimWorld.look(self, {"dx": 30, "dy": 0})

        world = Stubborn(flat(), [NearbyBlock(2, 66, 0, "oak_log", True)],
                         position=(0.5, 64.0, 0.5))
        skill = skills.create("collect_logs", count=1)
        run(world, skill)
        self.assertTrue(skill.failed)
        self.assertIn("could not settle", skill.done_reason)


class SensitivityAndStaleStateTests(unittest.TestCase):
    """The two things that made aiming oscillate in the real game."""

    def setUp(self):
        nav.reset_calibration()
        self.addCleanup(nav.reset_calibration)

    def test_the_scale_comes_from_minecrafts_own_arithmetic(self):
        """Given the slider there is nothing to estimate:
        degrees = counts * 0.15 * (sensitivity * 0.6 + 0.2)**3 * 8"""
        self.assertAlmostEqual(nav.pixels_per_degree_at(1.0), 1.628, places=2)
        self.assertAlmostEqual(nav.pixels_per_degree_at(0.5), 6.667, places=2)
        self.assertIsNone(nav.pixels_per_degree_at(None))
        self.assertIsNone(nav.pixels_per_degree_at(1.5))
        self.assertIsNone(nav.pixels_per_degree_at("loud"))

    def test_a_reported_setting_beats_the_hardcoded_guess(self):
        self.assertEqual(nav.pixels_per_degree(), nav.PIXELS_PER_DEGREE)
        nav.use_sensitivity(1.0)
        self.assertAlmostEqual(nav.pixels_per_degree(), 1.628, places=2)

    def test_the_setting_gives_size_and_observation_gives_direction(self):
        """The slider cannot say which way the axis runs; only watching can.
        The magnitude should not be thrown away to learn the sign."""
        nav.use_sensitivity(1.0)
        nav.observe_turn(-400, 0, (0.0, 0.0), (50.0, 0.0))    # inverted yaw
        dx = nav.look_delta_for(0.0, 10.0)
        self.assertLess(dx, 0, "it did not learn the direction")
        self.assertAlmostEqual(abs(dx), 10 * 1.628, delta=1.0,
                               msg="it threw away the exact magnitude")

    def test_the_runner_adopts_the_setting_before_the_first_turn(self):
        world = SimWorld(flat())
        base = world.read

        def with_sensitivity():
            state = base()
            return WorldState(position=state.position, rotation=state.rotation,
                              surface=state.surface, scan_radius=8,
                              mouse_sensitivity=1.0,
                              source="test", confidence=EXACT)

        world.read = with_sensitivity
        run(world, skills.create("navigate_to", destination=(4, 0)))
        self.assertAlmostEqual(nav.calibration()["from_game_settings"],
                               1.628, places=2)

    def test_it_waits_for_a_snapshot_newer_than_the_action(self):
        """The stale-feedback bug.

        The bridge rewrites its file a few times a second and a look takes
        ten milliseconds, so reading straight after acting returns the
        picture from BEFORE the action about half the time — "turned 0.0
        degrees" for a turn that plainly happened. A feedback loop fed its
        own stale output oscillates, which is what forty looks at one log
        actually was."""
        class Slow(SimWorld):
            """Like the real mod: a snapshot published on its own clock.

            `read` returns the LAST PUBLISHED picture, not the live world —
            which is the whole point. Publishing here happens every third
            poll instead of every 200ms."""
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.publishes = 0
                self.polls = 0
                self.snapshot = None

            def _publish(self):
                self.publishes += 1
                self.snapshot = SimWorld.read(self)

            def stamp(self):
                self.polls += 1
                if self.polls % 3 == 0:
                    self._publish()
                return self.publishes

            def read(self):
                if self.snapshot is None:
                    self._publish()
                return self.snapshot

        world = Slow(flat())
        result = run(world, skills.create("navigate_to", destination=(4, 0)))
        self.assertGreater(world.polls, result.steps_taken,
                           "it never waited for a fresh snapshot")
        # A turn judged against a stale picture reads as no turn at all.
        # (Matching on the text "0.0 degrees" would also match "50.0
        # degrees", which is how this assertion first fooled me.)
        stale = [r.step["note"] for r in result.records
                 if r.step["action"] == "look"
                 and r.verification.get("status") == verify_mod.FAILED]
        self.assertEqual(stale, [],
                         f"judged a turn against a stale picture: {stale}")

    def test_a_source_that_cannot_date_itself_is_read_once(self):
        """OCR takes a fresh screenshot every time, so there is nothing to
        wait for and waiting would just be slower."""
        world = SimWorld(flat())
        self.assertFalse(hasattr(world, "stamp"))
        result = run(world, skills.create("navigate_to", destination=(3, 0)))
        self.assertTrue(result.records)


class TheModelsViewOfTheWorldTests(unittest.TestCase):
    """What the LLM actually receives.

    §10 of the brief: the model must not be handed raw voxel JSON. These
    check the adapter's answer is a sentence with coordinates in it, and that
    an absent scan comes back as "I cannot see" rather than as a world with
    nothing in it."""

    def setUp(self):
        from actions import minecraft as adapter
        self.adapter = adapter
        self.addCleanup(adapter._reset_for_tests)

    def _source(self, state):
        class Source:
            def read(self_inner):
                return state
        self.adapter._reset_for_tests(state_source=Source())

    def test_it_upgrades_to_the_bridge_when_the_mod_comes_up(self):
        """The ordinary sequence: JARVIS, then Minecraft, then the mod.

        The overlay is very often available BEFORE the bridge is. An earlier
        version kept whatever it had picked for as long as that source still
        worked, so it would pick the overlay and never notice the mod — an
        assistant with the mod running that quietly could not see the
        terrain, sweeping the crosshair and reporting no trees."""
        from minecraft.mod_bridge import ModBridgeStateSource
        from minecraft.debug_overlay import DebugOverlayStateSource

        mod_is_up = {"yes": False}
        real_bridge_available = ModBridgeStateSource.available
        real_overlay_available = DebugOverlayStateSource.available

        ModBridgeStateSource.available = lambda self: mod_is_up["yes"]
        DebugOverlayStateSource.available = lambda self: True
        self.addCleanup(setattr, ModBridgeStateSource, "available",
                        real_bridge_available)
        self.addCleanup(setattr, DebugOverlayStateSource, "available",
                        real_overlay_available)
        self.adapter._reset_for_tests()

        first = self.adapter._get_state_source()
        self.assertIsInstance(first, DebugOverlayStateSource,
                              "should start on the overlay")

        mod_is_up["yes"] = True            # the player starts the modded game
        second = self.adapter._get_state_source()
        self.assertIsInstance(second, ModBridgeStateSource,
                              "it never noticed the mod came up")

    def test_the_log_says_which_reader_answered(self):
        """Twelve `look` steps in a row is a crosshair sweep and a
        navigation failure looks identical in a list of actions. One line
        naming the reader is the difference between a readable log and a
        guess."""
        from minecraft.mod_bridge import ModBridgeStateSource
        from minecraft.debug_overlay import DebugOverlayStateSource
        self.assertIn("terrain",
                      self.adapter._source_label(ModBridgeStateSource()))
        overlay = self.adapter._source_label(
            DebugOverlayStateSource(observer=None, reader=None))
        self.assertIn("NO terrain", overlay)
        self.assertIn("cannot navigate", overlay)

    def test_look_around_names_what_is_near(self):
        self._source(state_from(
            flat(), notable=(NearbyBlock(5, 64, 5, "oak_log", True),),
            entities=(EntityRef(name="creeper", distance=7.0,
                                position=(7.0, 64.0, 0.0),
                                category="hostile"),)))
        answer = self.adapter.minecraft_control({"action": "look_around"})
        self.assertIn("oak_log", answer)
        self.assertIn("(5, 64, 5)", answer)
        self.assertIn("creeper", answer)

    def test_look_around_says_unknown_rather_than_empty(self):
        """An empty scan and a bare plain read the same in the numbers. They
        are not the same thing, and only one of them is safe to act on."""
        self._source(empty_state("no bridge"))
        answer = self.adapter.minecraft_control({"action": "look_around"})
        self.assertIn("cannot see the world", answer)
        self.assertNotIn("nearest", answer)

    def test_look_around_needs_no_session(self):
        from core import capabilities
        self.assertEqual(
            self.adapter._mc_capability({"action": "look_around"}),
            capabilities.MINECRAFT_READ_STATE)

    def test_a_destination_is_never_invented(self):
        self.assertIsNone(self.adapter._destination_from({}))
        half = self.adapter._destination_from({"x": 3})
        self.assertIsInstance(half, str)
        self.assertIn("both x and z", half)
        self.assertEqual(self.adapter._destination_from({"x": "3", "z": "-4"}),
                         (3, -4))

    def test_navigate_to_without_a_destination_asks_instead_of_guessing(self):
        self._source(state_from(flat()))

        class Controller:
            def _guard(self):
                return ""

        answer = self.adapter._run_task(Controller(),
                                        {"task": "navigate_to"})
        self.assertIn("Where to?", answer)

    def test_the_tool_description_lists_the_new_task(self):
        self.assertIn("navigate_to", self.adapter.TOOL["description"])
        self.assertIn("look_around", self.adapter.TOOL["description"])
        properties = self.adapter.TOOL["parameters"]["properties"]
        self.assertIn("x", properties)
        self.assertIn("target", properties)

    def test_the_description_no_longer_claims_the_inventory_is_unreadable(self):
        """It was true before the mod. Leaving it in would be a lie that had
        once been true, which is the kind this repository cares about."""
        text = self.adapter.TOOL["description"]
        self.assertNotIn("I cannot read the inventory at all", text)
        self.assertIn("An unknown block is not air", text)


class NavigationHonestyTests(unittest.TestCase):
    """The claims the brief says must never be made."""

    def test_the_aiming_module_cannot_press_a_key_either(self):
        """navigation delegates its angle maths to `minecraft.aiming`, so the
        boundary has to hold there too or it has simply moved."""
        import ast
        import inspect
        from minecraft import aiming
        tree = ast.parse(inspect.getsource(aiming))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"}, {"math", "dataclasses"},
                         "aiming.py grew an import it should not have")

    def test_the_module_imports_nothing_that_can_press_a_key(self):
        """Checked against the import graph, not the prose. The module's own
        docstring says it cannot reach the controller, and a substring search
        would be satisfied by that sentence rather than by the fact."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(nav))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        # `minecraft.aiming` is allowed and is the point: the angle
        # arithmetic lives in one module, not copied into this one. It is
        # checked by its own test that it cannot reach input either.
        self.assertEqual(imported - {"__future__"},
                         {"heapq", "math", "dataclasses", "minecraft"},
                         "navigation.py grew an import it should not have")

    def test_the_skill_holds_no_controller(self):
        skill = skills.create("navigate_to", destination=(1, 1))
        for name in vars(skill):
            self.assertNotIn("controller", name)

    def test_an_unknown_column_is_reported_unknown_not_air(self):
        state = state_from(flat(radius=2))
        self.assertFalse(nav.is_known(state, 9, 9))
        self.assertFalse(nav.is_walkable(state, 9, 9))
        self.assertFalse(nav.reachable(state, (9, 9)))

    def test_closer_to_fails_when_the_move_gained_nothing(self):
        before = state_from(flat(), position=(0.0, 64.0, 0.0))
        after = state_from(flat(), position=(0.0, 64.0, 0.1))
        verdict = verify_mod.closer_to((10, 0)).check(before, after)
        self.assertEqual(verdict.status, verify_mod.FAILED)

    def test_closer_to_is_unverifiable_without_a_position(self):
        blind = empty_state("no bridge")
        verdict = verify_mod.closer_to((10, 0)).check(blind, blind)
        self.assertEqual(verdict.status, verify_mod.UNVERIFIABLE)
        self.assertIn("position", verdict.missing_fields)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── Movement under honest physics ────────────────────────────────────────────

def step_up(clearance=4, height=64, from_x=3):
    """Flat ground, then everything from x=from_x onwards one block higher."""
    return [b if b.x < from_x
            else NearbyBlock(b.x, height, b.z, "dirt", True, clearance)
            for b in flat()]


def flat_with_room(clearance):
    return [NearbyBlock(b.x, b.y, b.z, b.name, b.solid, clearance)
            for b in flat()]


class SimulationIsHonestTests(unittest.TestCase):
    """The simulation must fail where the real game fails.

    It used to let the player walk straight up full blocks, which is the one
    thing the real game does not allow and the exact thing that went wrong in
    it. A fake that is easier than the real thing hides precisely the bugs it
    was built to find."""

    def test_a_full_block_cannot_be_walked_up(self):
        world = SimWorld(step_up(), yaw=-90.0)            # facing +X
        world.move({"duration": 2.0})
        self.assertLess(world.x, 3.0, "walked up a full block without jumping")

    def test_a_hop_gets_onto_it(self):
        world = SimWorld(step_up(), position=(2.5, 64.0, 0.5), yaw=-90.0)
        world.move_and_jump({"duration": 0.4})
        self.assertGreaterEqual(world.x, 3.0)
        self.assertEqual(world.y, 65.0)

    def test_a_jump_on_the_spot_goes_nowhere(self):
        world = SimWorld(step_up(), position=(2.5, 64.0, 0.5), yaw=-90.0)
        world.jump({})
        self.assertEqual((world.x, world.y), (2.5, 64.0))

    def test_a_long_move_cannot_pass_through_a_wall(self):
        """The old version only checked the column it landed in."""
        world = SimWorld(wall_at(flat(), x=3), yaw=-90.0)
        world.move({"duration": 2.0})                     # ~8.6 blocks
        self.assertLess(world.x, 3.0, "passed through the wall")


class MovementTests(unittest.TestCase):

    def test_it_hops_a_one_block_step_without_being_told(self):
        """The reported bug: it walked into a block and had to be told to
        jump. The map already says the step is there, so it hops straight
        up — walking and jumping together, which is the only thing that
        works."""
        world = SimWorld(step_up())
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        actions = [r.step["action"] for r in result.records]
        self.assertIn("move_and_jump", actions)
        self.assertNotIn("jump", actions, "a jump on the spot goes nowhere")
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertLess(world.distance_to((6, 0)), 1.5)

    def test_it_does_not_try_to_hop_a_two_block_wall(self):
        world = SimWorld(wall_at(flat(), x=3, height=65, gap_z=4),
                         position=(0.5, 64.0, 0.5))
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        hops = [r for r in result.records
                if r.step["action"] == "move_and_jump"]
        self.assertEqual(hops, [], "tried to hop a two-block wall")
        self.assertLess(world.distance_to((6, 0)), 2.0, skill.done_reason)

    def test_a_low_ceiling_is_refused_not_walked_into(self):
        """Feet fit, body does not. A planner that only knew the ground used
        to route straight under a branch."""
        world = SimWorld(step_up(clearance=1))
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        self.assertTrue(skill.failed)
        self.assertEqual(result.steps_taken, 0, "it walked somewhere anyway")

    def test_it_routes_round_a_low_branch(self):
        """Ground that is fine to stand on, one block of room above it."""
        surface = [b if not (b.x == 3 and b.z != 4)
                   else NearbyBlock(b.x, b.y, b.z, b.name, True, 1)
                   for b in flat()]
        world = SimWorld(surface)
        skill = skills.create("navigate_to", destination=(6, 0))
        result = run(world, skill)
        self.assertFalse(skill.failed, skill.done_reason)
        path_columns = [(x, z) for x, _y, z in (skill._path.waypoints
                                                 if skill._path else ())]
        for x, z in path_columns:
            if x == 3:
                self.assertEqual(z, 4, "routed under the branch")

    def test_it_does_not_walk_off_a_cliff(self):
        surface = [b if b.x < 3 else NearbyBlock(b.x, 50, b.z, "stone", True, 4)
                   for b in flat()]
        world = SimWorld(surface)
        skill = skills.create("navigate_to", destination=(6, 0))
        run(world, skill)
        self.assertTrue(skill.failed)
        self.assertEqual(world.y, 64.0, "it went over the edge")

    def test_it_comes_through_a_gap_without_clipping_the_wall(self):
        """The player is 0.6 wide and rarely at a column's centre. A line
        traced between centres looked clear and clipped the corner of the
        wall it had just come round."""
        world = SimWorld(wall_at(flat(), x=3, gap_z=0),
                         position=(0.5, 64.0, 4.5))
        skill = skills.create("navigate_to", destination=(6, 4))
        result = run(world, skill)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertLess(result.steps_taken, 15)
        self.assertEqual(world.blocked, 0,
                         f"walked into the wall {world.blocked} times")

    def test_a_shoulder_hits_a_wall_a_centre_line_misses(self):
        """The player is 0.6 wide. A line along z=0.8 never enters column
        (3, 1) — but a shoulder 0.3 to the side reaches z=1.1, which does.
        A planner treating the player as a point would call it clear."""
        surface = [b if (b.x, b.z) != (3, 1)
                   else NearbyBlock(3, 66, 1, "stone", True, 4)
                   for b in flat()]
        local = nav.LocalMap.from_state(state_from(surface))
        self.assertFalse(local.line_is_walkable((2.5, 0.8), (4.5, 0.8)),
                         "the shoulder clips the wall and the line said clear")
        self.assertTrue(local.line_is_walkable((2.5, 0.5), (4.5, 0.5)),
                        "down the middle of the column should be fine")

    def test_it_walks_past_a_pillar_without_clipping_it(self):
        surface = [b if (b.x, b.z) != (3, 1)
                   else NearbyBlock(3, 66, 1, "stone", True, 4)
                   for b in flat()]
        world = SimWorld(surface, position=(0.5, 64.0, 0.9))
        skill = skills.create("navigate_to", destination=(6, 1))
        run(world, skill)
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertEqual(world.blocked, 0,
                         f"walked into the pillar {world.blocked} times")

    def test_moves_are_short_near_an_obstacle_and_long_in_the_open(self):
        world = SimWorld(flat())
        result = run(world, skills.create("navigate_to", destination=(7, 0)))
        longest = max(r.step["params"]["duration"] for r in result.records
                      if r.step["action"] == "move")
        self.assertGreater(longest, 1.0, "open ground should get a long stride")

        world = SimWorld(step_up(), position=(1.5, 64.0, 0.5))
        result = run(world, skills.create("navigate_to", destination=(6, 0)))
        before_step = [r.step["params"]["duration"] for r in result.records
                       if r.step["action"] == "move"
                       and r.state_before["position"][0] < 3.0]
        for seconds in before_step:
            self.assertLessEqual(seconds, 0.35,
                                 "a long stride straight into the step")


class StuckDiagnosisTests(unittest.TestCase):
    """Eight kinds of stuck, each with its own recovery."""

    def world(self, surface=None, entities=()):
        return state_from(surface or flat(), entities=entities)

    def test_a_one_block_step_is_a_hop(self):
        from minecraft import stuck
        state = state_from(step_up(from_x=1))
        diagnosis = stuck.diagnose_movement(state, (3, 0), moved_by=0.0)
        self.assertEqual(diagnosis.kind, stuck.VERTICAL_BLOCK)
        self.assertEqual(diagnosis.recovery, stuck.HOP)

    def test_a_wall_is_a_reroute(self):
        from minecraft import stuck
        state = state_from(wall_at(flat(), x=1))
        diagnosis = stuck.diagnose_movement(state, (3, 0), moved_by=0.0)
        self.assertEqual(diagnosis.kind, stuck.COLLISION_STUCK)
        self.assertEqual(diagnosis.recovery, stuck.REROUTE)

    def test_a_mob_in_the_way_is_not_blamed_on_the_terrain(self):
        from minecraft import stuck
        cow = EntityRef(name="cow", distance=1.0, position=(1.5, 64.0, 0.5),
                        category="passive")
        state = state_from(flat(), entities=(cow,))
        diagnosis = stuck.diagnose_movement(state, (3, 0), moved_by=0.0)
        self.assertEqual(diagnosis.kind, stuck.ENTITY_INTERFERENCE)

    def test_a_dropped_item_is_not_in_the_way(self):
        from minecraft import stuck
        item = EntityRef(name="oak_log", distance=1.0,
                         position=(1.5, 64.0, 0.5), category="item")
        state = state_from(flat(), entities=(item,))
        diagnosis = stuck.diagnose_movement(state, (3, 0), moved_by=0.0)
        self.assertNotEqual(diagnosis.kind, stuck.ENTITY_INTERFERENCE)

    def test_a_mob_behind_is_not_in_the_way(self):
        from minecraft import stuck
        cow = EntityRef(name="cow", distance=1.0, position=(-0.5, 64.0, 0.5),
                        category="passive")
        state = state_from(flat(), entities=(cow,))
        diagnosis = stuck.diagnose_movement(state, (3, 0), moved_by=0.0)
        self.assertNotEqual(diagnosis.kind, stuck.ENTITY_INTERFERENCE)

    def test_unscanned_ground_means_look_again(self):
        from minecraft import stuck
        surface = [b for b in flat() if b.x != 1]
        diagnosis = stuck.diagnose_movement(state_from(surface), (3, 0),
                                            moved_by=0.0)
        self.assertEqual(diagnosis.kind, stuck.UNKNOWN_TERRAIN)
        self.assertEqual(diagnosis.recovery, stuck.OBSERVE)

    def test_open_ground_and_no_movement_is_the_input(self):
        """Not a navigation problem, and no amount of re-routing fixes it."""
        from minecraft import stuck
        diagnosis = stuck.diagnose_movement(state_from(flat()), (3, 0),
                                            moved_by=0.0)
        self.assertEqual(diagnosis.kind, stuck.INPUT_STUCK)
        self.assertEqual(diagnosis.recovery, stuck.STOP)
        self.assertIn("window in front", diagnosis.describe())

    def test_a_stale_route_is_replanned(self):
        from minecraft import stuck
        surface = [b if (b.x, b.z) != (5, 0)
                   else NearbyBlock(5, 63, 0, "lava", True, 4) for b in flat()]
        diagnosis = stuck.diagnose_movement(state_from(surface), (5, 0),
                                            moved_by=1.0, route_column=(5, 0))
        self.assertEqual(diagnosis.kind, stuck.PATH_STALE)

    def test_aim_that_is_not_converging_is_named(self):
        from minecraft import stuck
        self.assertEqual(stuck.diagnose_aim([40, 38, 41, 39, 42]).kind,
                         stuck.AIM_STUCK)
        self.assertIsNone(stuck.diagnose_aim([40, 20, 9, 4, 2]))

    def test_every_kind_has_a_recovery(self):
        from minecraft import stuck
        for kind in (stuck.VERTICAL_BLOCK, stuck.COLLISION_STUCK,
                     stuck.ENTITY_INTERFERENCE, stuck.UNKNOWN_TERRAIN,
                     stuck.PATH_STALE, stuck.NO_ROUTE, stuck.INPUT_STUCK,
                     stuck.AIM_STUCK):
            self.assertIn(kind, stuck.RECOVERY)

    def test_the_stuck_module_cannot_press_anything(self):
        import ast
        import inspect
        from minecraft import stuck
        tree = ast.parse(inspect.getsource(stuck))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"math", "dataclasses", "minecraft"})


class ClearanceParsingTests(unittest.TestCase):

    def test_clearance_is_parsed_from_schema_3(self):
        from minecraft.mod_bridge import _terrain_block
        block = _terrain_block([1, 63, 2, "minecraft:grass_block", True, 1])
        self.assertEqual(block.clearance, 1)

    def test_an_old_mod_without_clearance_is_not_impassable(self):
        """None means the bridge did not say. Treating it as zero would make
        the whole world impassable on an older jar."""
        from minecraft.mod_bridge import _terrain_block
        block = _terrain_block([1, 63, 2, "minecraft:grass_block", True])
        self.assertIsNone(block.clearance)
        local = nav.LocalMap.from_state(state_from([block]))
        self.assertTrue(local.fits(1, 2))

    def test_the_schema_versions_are_all_still_read(self):
        from minecraft import mod_bridge
        for version in (1, 2, 3):
            self.assertIn(f"markliv.minecraft.state/{version}",
                          mod_bridge.SUPPORTED_SCHEMAS)
