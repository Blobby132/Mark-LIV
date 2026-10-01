"""
B2: collect_blocks -- any block, not only logs.

The gatherer collect_logs is built on, pointed at stone (for cobblestone),
dirt, sand, gravel and coal, iron and copper ore, with a small drop table:
success is counted from the DROP arriving in the inventory, never from the
block disappearing. It takes only blocks it can reach and that are exposed
-- it never digs -- never the block underfoot, and none that touch water or
lava in the scan. The right tool comes first (B1); a block nothing in the
hotbar can harvest is refused before any swing.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import mining as mining_mod                           # noqa: E402
from minecraft import navigation as nav                              # noqa: E402
from minecraft import skills                                         # noqa: E402
from minecraft.skills import collect as skills_collect                  # noqa: E402
from minecraft.state import (                                        # noqa: E402
    BlockRef, EXACT, EntityRef, ItemStack, NearbyBlock, WorldState)
from minecraft.task_runner import TaskRunner                         # noqa: E402
from tests.test_minecraft_navigation import TreeWorld                # noqa: E402

DROPS = {"stone": "cobblestone", "grass_block": "dirt", "dirt": "dirt",
         "sand": "sand", "coal_ore": "coal"}


class QuarryWorld(TreeWorld):
    """TreeWorld's crosshair over ground you can dig one block at a time.

    A HOTBAR: the selected slot's item decides how long a block takes and
    whether it drops anything, as in the game. A broken floor block lowers
    its column by one and drops its item beside it; an item within about a
    block and a half is picked up."""

    PICKUP = 1.425

    def __init__(self, columns, hotbar, selected=0, notable=(), **kwargs):
        surface = []
        for x in range(-8, 9):
            for z in range(-8, 9):
                name = columns.get((x, z), "grass_block")
                surface.append(NearbyBlock(x, 63, z, name, True, 4))
        super().__init__(surface, logs=list(notable), **kwargs)
        self.hotbar = {slot: list(item) for slot, item in hotbar.items()}
        self.selected = selected
        self.items = []                    # (x, y, z, name) on the ground
        self.dug = []
        self.under_feet_swings = 0

    def _sync(self):
        self.surface = list(self.ground.values())

    def _held(self):
        stack = self.hotbar.get(self.selected)
        return stack[0] if stack and stack[1] > 0 else None

    def hotbar_select(self, params):
        self.selected = int(params["slot"]) - 1
        return self._result("hotbar_select", params)

    def mine(self, params):
        self.swings += 1
        seconds = float(params.get("duration", 0))
        hit = self.crosshair()
        if hit is None:
            self.wrong_block_swings += 1
            return self._result("mine", params, int(seconds * 1000))
        (x, y, z), name, _face = hit
        if (x, z) == (math.floor(self.x), math.floor(self.z)):
            self.under_feet_swings += 1
        ground = self.ground.get((x, z))
        if ground is None or ground.y != y:
            self.wrong_block_swings += 1
            return self._result("mine", params, int(seconds * 1000))
        estimate = mining_mod.estimate_break_duration(
            name, held_item=self._held() or "", on_ground=True)
        if seconds >= estimate.seconds:
            self.ground[(x, z)] = NearbyBlock(x, y - 1, z, "stone", True, 4)
            self._sync()
            self.dug.append((x, y, z, name))
            if estimate.drops:
                self.items.append((x + 0.5, y, z + 0.5, DROPS[name]))
        self._collect()
        return self._result("mine", params, int(seconds * 1000))

    def _collect(self):
        for item in list(self.items):
            if abs(item[0] - self.x) <= self.PICKUP \
                    and abs(item[2] - self.z) <= self.PICKUP:
                self.items.remove(item)
                for stack in self.hotbar.values():
                    if stack[0] == item[3]:
                        stack[1] += 1
                        break
                else:
                    free = next(s for s in range(9, 36)
                                if s not in self.hotbar)
                    self.hotbar[free] = [item[3], 1]

    def _walk(self, distance, jumping):
        TreeWorld._walk(self, distance, jumping)
        self._collect()

    def count(self, name):
        return sum(s[1] for s in self.hotbar.values() if s[0] == name)

    def read(self):
        self._collect()
        hit = self.crosshair()
        target = (BlockRef(name=hit[1], x=hit[0][0], y=hit[0][1],
                           z=hit[0][2], face=hit[2])
                  if hit else BlockRef(name="air"))
        stacks = tuple(ItemStack(slot=slot, name=name, count=count)
                       for slot, (name, count) in sorted(self.hotbar.items())
                       if count > 0)
        held = next((s for s in stacks if s.slot == self.selected), None)
        items = tuple(EntityRef(
            name="item", category="item", hostile=False,
            position=(ix, iy, iz),
            distance=math.dist((self.x, self.z), (ix, iz)),
            item=ItemStack(name=name, count=1))
            for ix, iy, iz, name in self.items)
        return WorldState(
            position=(self.x, self.y, self.z), rotation=(self.yaw, self.pitch),
            surface=tuple(self.surface), notable_blocks=tuple(self.logs),
            target_block=target, scan_radius=8, inventory=stacks,
            selected_slot=self.selected, held_item=held,
            nearby_entities=items, on_ground=True,
            source="test", confidence=EXACT)


def run(world, skill, max_steps=60):
    nav.reset_calibration()
    from minecraft import aiming as aiming_mod
    aiming_mod.SHARED.reset()
    runner = TaskRunner(world, world, sleeper=lambda _s: None)
    return runner.run(skill, max_steps=max_steps)


STONE = {(3, 0): "stone", (3, 1): "stone", (4, 0): "stone", (4, 1): "stone"}


class QuarryTests(unittest.TestCase):

    def test_two_cobblestone_with_the_pickaxe_and_the_slot_put_back(self):
        world = QuarryWorld(STONE, {0: ("dirt", 5), 2: ("stone_pickaxe", 1)},
                            yaw=-90.0)
        skill = skills.create("collect_blocks", target="stone", count=2)
        result = run(world, skill)
        self.assertGreaterEqual(world.count("cobblestone"), 2,
                                f"{result.reason}")
        self.assertFalse(skill.failed, skill.done_reason)
        self.assertIn("counted in the inventory", skill.done_reason)
        self.assertEqual(world.selected, 0, "the slot was not put back")
        self.assertEqual(world.under_feet_swings, 0)
        self.assertEqual(world.wrong_block_swings, 0)
        self.assertLessEqual(len(world.dug), 3, "it broke more than needed")

    def test_stone_without_a_pickaxe_is_refused_before_any_swing(self):
        world = QuarryWorld(STONE, {0: ("dirt", 5), 1: ("iron_axe", 1)},
                            yaw=-90.0)
        skill = skills.create("collect_blocks", target="stone", count=2)
        run(world, skill)
        self.assertEqual(world.swings, 0)
        self.assertTrue(skill.failed)
        self.assertIn("pickaxe", skill.done_reason)

    def test_dirt_by_hand_is_fine(self):
        world = QuarryWorld({}, {0: ("oak_planks", 5)}, yaw=-90.0)
        skill = skills.create("collect_blocks", target="dirt", count=1)
        run(world, skill)
        self.assertGreaterEqual(world.count("dirt"), 1, skill.done_reason)
        self.assertEqual(world.under_feet_swings, 0)


def state(surface, notable=(), position=(0.5, 64.0, 0.5)):
    return WorldState(position=position, rotation=(0.0, 0.0),
                      surface=tuple(surface), notable_blocks=tuple(notable),
                      scan_radius=8, inventory=(), selected_slot=0,
                      source="test", confidence=EXACT)


def flat(columns=None, y=63):
    columns = columns or {}
    return [NearbyBlock(x, y, z, columns.get((x, z), "grass_block"), True, 4)
            for x in range(-8, 9) for z in range(-8, 9)]


class TargetChoiceTests(unittest.TestCase):

    def target(self, world_state, what="stone"):
        skill = skills.create("collect_blocks", target=what, count=1)
        return skill._next_target(world_state), skill

    def test_never_the_block_underfoot(self):
        chosen, _ = self.target(state(flat({(0, 0): "stone", (3, 0): "stone"})))
        self.assertEqual(chosen.position, (3, 63, 0))

    def test_not_the_block_under_any_part_of_the_feet(self):
        """Standing at x=0.8 the body overlaps column 1 as well."""
        chosen, _ = self.target(state(flat({(1, 0): "stone", (4, 0): "stone"}),
                                      position=(0.8, 64.0, 0.5)))
        self.assertEqual(chosen.position, (4, 63, 0))

    def test_looking_down_at_the_floor_does_not_mine_it(self):
        """The crosshair is the authority for which block to mine -- but not
        for whether to: the stone underfoot, under the crosshair and in
        reach, is still never broken."""
        base = state(flat({(0, 0): "stone", (3, 0): "stone"}))
        looking_down = WorldState(**{**base.__dict__,
                                     "target_block": BlockRef(
                                         name="stone", x=0, y=63, z=0,
                                         face="up")})
        skill = skills.create("collect_blocks", target="stone", count=1)
        self.assertIsNone(skill._log_under_crosshair(looking_down))

    def test_a_block_touching_water_is_left(self):
        surface = flat({(3, 0): "stone", (5, 5): "stone"})
        water = NearbyBlock(4, 63, 0, "water", False, 4)
        surface = [b for b in surface if (b.x, b.z) != (4, 0)] + [water]
        chosen, _ = self.target(state(surface))
        self.assertEqual(chosen.position, (5, 63, 5))

    def test_a_block_beside_lava_in_the_scan_is_left(self):
        lava = NearbyBlock(3, 62, 0, "lava", False)
        chosen, _ = self.target(state(flat({(3, 0): "stone"}), notable=(lava,)))
        self.assertIsNone(chosen)

    def test_a_buried_ore_is_not_reachable_without_digging(self):
        ore = NearbyBlock(3, 60, 0, "coal_ore", True)        # under the grass
        chosen, skill = self.target(state(flat(), notable=(ore,)), "coal")
        self.assertIsNone(chosen)
        self.assertIn("without digging",
                      skill._unreachable_text(skill._nearest_seen(
                          state(flat(), notable=(ore,)))))

    def test_an_ore_in_a_cliff_face_is_exposed(self):
        """Its column is a block higher than the next one over, and the air
        beside it reaches its height."""
        surface = flat({(3, 0): "stone"})
        surface = [NearbyBlock(b.x, 64, b.z, b.name, True, 4)
                   if b.x >= 3 else b for b in surface]
        ore = NearbyBlock(3, 64, 1, "iron_ore", True)
        exposed = skills_collect.exposed(nav.LocalMap.from_state(state(surface)),
                                 ore)
        self.assertTrue(exposed)
        buried = NearbyBlock(4, 62, 1, "iron_ore", True)
        self.assertFalse(skills_collect.exposed(
            nav.LocalMap.from_state(state(surface)), buried))


class TableTests(unittest.TestCase):

    def test_the_drop_table(self):
        for target, block, drop in (("stone", "stone", "cobblestone"),
                                    ("dirt", "grass_block", "dirt"),
                                    ("gravel", "gravel", "flint"),
                                    ("coal", "deepslate_coal_ore", "coal"),
                                    ("iron", "iron_ore", "raw_iron"),
                                    ("copper", "copper_ore", "raw_copper"),
                                    ("sand", "sand", "sand")):
            with self.subTest(target=target):
                skill = skills.create("collect_blocks", target=target)
                self.assertIn(block, skill._wanted_names())
                self.assertIn(drop, skill._drop_names())

    def test_aliases(self):
        for alias, canonical in (("cobblestone", "stone"), ("iron_ore", "iron"),
                                 ("raw_copper", "copper"), ("grass", "dirt")):
            with self.subTest(alias=alias):
                skill = skills.create("collect_blocks", target=alias)
                self.assertEqual(skill._drop_names(), skills.create(
                    "collect_blocks", target=canonical)._drop_names())

    def test_an_unknown_target_is_refused_with_the_list(self):
        skill = skills.create("collect_blocks", target="diamond")
        self.assertIsNone(skill.plan(state(flat()), 0, ()))
        self.assertTrue(skill.failed)
        self.assertIn("stone", skill.done_reason)

    def test_without_the_inventory_it_does_not_claim_success(self):
        skill = skills.create("collect_blocks", target="dirt", count=1)
        skill._broken = 1
        skill._can_count = False
        self.assertTrue(skill.failed)

    def test_it_is_a_tool_task(self):
        from actions import minecraft as mc_actions
        self.assertIn("collect_blocks", mc_actions.TOOL["description"])
        task = mc_actions.TOOL["parameters"]["properties"]["task"]
        self.assertIn("collect_blocks", task["description"])
        self.assertIn("collect_blocks", skills.available())


class CollectLogsIsUnchangedTests(unittest.TestCase):

    def test_collect_logs_and_fell_tree_are_gatherers(self):
        self.assertIsInstance(skills.create("collect_logs"), skills_collect._Gatherer)
        self.assertIsInstance(skills.create("fell_tree"), skills_collect._Gatherer)
        self.assertEqual(skills.create("collect_logs").name, "collect_logs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
