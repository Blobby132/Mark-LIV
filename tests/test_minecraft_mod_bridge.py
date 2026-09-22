"""
Tests for minecraft/mod_bridge.py.

The bridge is the first source whose numbers are good enough to act on without
checking, which makes the ways it can be WRONG the important thing. A stale
file describing a world that no longer exists looks exactly like a fresh one,
and is the failure most likely to hurt: everything downstream is built to
trust `exact`.

No test here touches a real file or a real Minecraft.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft.mod_bridge import (                                     # noqa: E402
    MAX_AGE_SECONDS, ModBridgeStateSource, SCHEMA, state_file_path,
)
from minecraft.state import EXACT, UNKNOWN                             # noqa: E402

NOW_MS = 1_700_000_000_000
NOW_S = NOW_MS / 1000.0


def payload(**overrides) -> dict:
    base = {
        "schema": SCHEMA,
        "written_at_ms": NOW_MS,
        "in_game": True,
        "position": [-142.5, 71.0, 305.2],
        "rotation": [12.7, -3.4],
        "health": 18.0,
        "max_health": 20.0,
        "hunger": 17,
        "on_ground": True,
        "selected_slot": 2,
        "held_item": {"slot": 2, "name": "minecraft:iron_axe", "count": 1},
        "inventory": [{"slot": 0, "name": "minecraft:oak_log", "count": 12}],
        "dimension": "minecraft:overworld",
        "time_of_day": 1200,
        "weather": "clear",
        "biome": "minecraft:forest",
        "light_level": 11,
        "target_block": {"name": "minecraft:oak_log", "x": -143, "y": 70,
                         "z": 306, "face": "north"},
        "target_entity": None,
        "nearby_entities": [{"name": "minecraft:cow", "distance": 7.4}],
    }
    base.update(overrides)
    return base


def source(data=None, raw=None, clock_s=NOW_S) -> ModBridgeStateSource:
    text = raw if raw is not None else json.dumps(
        data if data is not None else payload())
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: clock_s)


class TestReadingGoodState(unittest.TestCase):

    def test_everything_arrives_as_exact(self):
        """The whole point. These were `inferred` at best from OCR, and most
        of them were not available at all."""
        state = source().read()
        self.assertEqual(state.source, "mod-bridge")
        for field in ("position", "rotation", "health", "hunger", "inventory",
                      "selected_slot", "held_item", "target_block",
                      "dimension", "biome", "nearby_entities"):
            with self.subTest(field=field):
                self.assertEqual(state.confidence_of(field), EXACT)

    def test_the_namespace_is_stripped_from_vanilla_names(self):
        """A planner comparing against 'oak_log' should not have to know
        about 'minecraft:'."""
        state = source().read()
        self.assertEqual(state.target_block.name, "oak_log")
        self.assertEqual(state.biome, "forest")
        self.assertEqual(state.dimension, "overworld")
        self.assertEqual(state.held_item.name, "iron_axe")

    def test_a_modded_name_keeps_its_namespace(self):
        """Dropping it would make two different blocks look identical."""
        state = source(payload(target_block={
            "name": "create:andesite_casing", "x": 1, "y": 2, "z": 3,
            "face": "up"})).read()
        self.assertEqual(state.target_block.name, "create:andesite_casing")

    def test_the_inventory_survives_with_counts(self):
        stack = source().read().inventory[0]
        self.assertEqual((stack.name, stack.count, stack.slot),
                         ("oak_log", 12, 0))

    def test_an_empty_inventory_is_a_reading_not_a_gap(self):
        """Empty and unreadable are different answers, and collapsing them
        would make "I have no logs" indistinguishable from "I cannot see"."""
        state = source(payload(inventory=[])).read()
        self.assertEqual(state.inventory, ())
        self.assertEqual(state.confidence_of("inventory"), EXACT)

    def test_yaw_becomes_a_cardinal_direction(self):
        for yaw, expected in ((0.0, "south"), (90.0, "west"),
                              (180.0, "north"), (270.0, "east"),
                              (-90.0, "east"), (359.0, "south")):
            with self.subTest(yaw=yaw):
                state = source(payload(rotation=[yaw, 0.0])).read()
                self.assertEqual(state.facing, expected)


class TestStaleDataIsNotData(unittest.TestCase):
    """A file on disk outlives the process that wrote it. If Minecraft closed,
    this file sits there looking perfectly valid and describing a world that
    no longer exists."""

    def test_an_old_reading_is_discarded(self):
        stale = source(clock_s=NOW_S + MAX_AGE_SECONDS + 1)
        self.assertFalse(stale.available())
        self.assertTrue(stale.read().is_empty)

    def test_the_reason_says_the_game_is_probably_closed(self):
        stale = source(clock_s=NOW_S + 60)
        self.assertIn("closed", stale.read().notes)

    def test_a_fresh_reading_is_kept(self):
        fresh = source(clock_s=NOW_S + 0.4)
        self.assertTrue(fresh.available())
        self.assertFalse(fresh.read().is_empty)

    def test_a_missing_timestamp_is_treated_as_infinitely_old(self):
        """Failing closed: an unreadable age must not pass as "just written"."""
        data = payload()
        del data["written_at_ms"]
        self.assertTrue(source(data).read().is_empty)

    def test_a_nonsense_timestamp_is_treated_as_infinitely_old(self):
        for bad in ("soon", None, [1], {}):
            with self.subTest(written_at_ms=repr(bad)):
                self.assertTrue(source(payload(written_at_ms=bad)).read().is_empty)


class TestRefusingWhatItCannotTrust(unittest.TestCase):

    def test_a_different_schema_is_refused_rather_than_guessed(self):
        """A different schema is a different contract. Reading it anyway is
        the fabrication this subsystem refuses everywhere else."""
        self.assertTrue(source(payload(schema="something.else/9")).read().is_empty)

    def test_missing_schema_is_refused(self):
        data = payload()
        del data["schema"]
        self.assertTrue(source(data).read().is_empty)

    def test_malformed_json_does_not_raise(self):
        self.assertTrue(source(raw="{not json at all").read().is_empty)

    def test_a_json_array_is_refused(self):
        self.assertTrue(source(raw="[1,2,3]").read().is_empty)

    def test_a_reader_that_throws_does_not_raise(self):
        class Angry(ModBridgeStateSource):
            pass
        angry = Angry(path="(test)",
                      reader=lambda: (_ for _ in ()).throw(OSError("gone")))
        self.assertTrue(angry.read().is_empty)

    def test_being_in_a_menu_is_reported_as_such(self):
        """Not as a player standing at the origin with no health."""
        state = source(payload(in_game=False)).read()
        self.assertTrue(state.is_empty)
        self.assertIn("not in a world", state.notes)


class TestMalformedFieldsBecomeUnknown(unittest.TestCase):
    """The mod is trusted to be honest, not to be bug-free. A broken field
    must become None -- which the provenance rule marks unknown -- rather than
    a zero that looks like a measurement."""

    def test_a_broken_position_is_unknown_not_the_origin(self):
        for bad in ([1, 2], "here", None, [1, 2, "x"], []):
            with self.subTest(position=repr(bad)):
                state = source(payload(position=bad)).read()
                self.assertIsNone(state.position)
                self.assertEqual(state.confidence_of("position"), UNKNOWN)

    def test_a_broken_field_does_not_poison_the_rest(self):
        state = source(payload(health="fine", position=[1.0, 2.0, 3.0])).read()
        self.assertIsNone(state.health)
        self.assertEqual(state.position, (1.0, 2.0, 3.0))
        self.assertEqual(state.confidence_of("position"), EXACT)

    def test_a_nameless_block_is_no_block(self):
        state = source(payload(target_block={"x": 1, "y": 2, "z": 3})).read()
        self.assertIsNone(state.target_block)

    def test_unreadable_inventory_entries_are_dropped_not_invented(self):
        state = source(payload(inventory=[
            {"slot": 0, "name": "minecraft:oak_log", "count": 3},
            {"slot": 1},
            "nonsense",
        ])).read()
        self.assertEqual(len(state.inventory), 1)
        self.assertEqual(state.inventory[0].name, "oak_log")

    def test_looking_at_nothing_arrives_as_air(self):
        """The mod sends an explicit air block rather than omitting it, so
        "looking at sky" stays distinguishable from "could not tell"."""
        state = source(payload(target_block={
            "name": "minecraft:air", "x": None, "y": None, "z": None,
            "face": None})).read()
        self.assertEqual(state.target_block.name, "air")
        self.assertEqual(state.confidence_of("target_block"), EXACT)


class TestWhereItLooks(unittest.TestCase):

    def test_the_path_is_computed_not_discovered(self):
        """Both sides derive the same path from the environment. Hunting for
        a .minecraft directory would make a wrong guess indistinguishable from
        the mod not running."""
        path = state_file_path()
        self.assertTrue(path)
        self.assertIn("minecraft_state.json", path)

    def test_an_override_wins(self):
        import os
        from minecraft.mod_bridge import ENV_OVERRIDE
        original = os.environ.get(ENV_OVERRIDE)
        os.environ[ENV_OVERRIDE] = "/tmp/somewhere/else.json"
        try:
            self.assertEqual(state_file_path(), "/tmp/somewhere/else.json")
        finally:
            if original is None:
                os.environ.pop(ENV_OVERRIDE, None)
            else:
                os.environ[ENV_OVERRIDE] = original

    def test_a_missing_file_says_what_to_install(self):
        missing = ModBridgeStateSource(path="/definitely/not/here.json")
        self.assertFalse(missing.available())
        reason = missing.unavailable_reason()
        self.assertIn("markliv-bridge.jar", reason)
        self.assertIn("mods folder", reason)


if __name__ == "__main__":
    unittest.main()
