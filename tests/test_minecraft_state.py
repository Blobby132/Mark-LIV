"""
Tests for the WorldState contract — the "do not fabricate" rule.

The single most dangerous failure in this subsystem is not a crash. It is a
state object that looks complete and is not: a planner acting on a confident
wrong health value walks into lava, and every verification downstream is
reasoning about a world that does not exist.

So these tests are mostly about what must NOT happen.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft.state import (                                          # noqa: E402
    BlockRef, CONFIDENCE_LEVELS, EXACT, INFERRED, ItemStack, UNKNOWN,
    WorldState, empty_state,
)


class TestUnknownStaysUnknown(unittest.TestCase):

    def test_a_fresh_state_knows_nothing(self):
        state = WorldState()
        self.assertTrue(state.is_empty)
        self.assertEqual(state.known_fields(), ())
        for name in state.unknown_fields():
            self.assertEqual(state.confidence_of(name), UNKNOWN)

    def test_claiming_confidence_in_a_missing_value_is_rejected(self):
        """The core invariant. A source cannot say "health is exact" while
        health is None — that is precisely the fabrication this guards."""
        state = WorldState(provenance={"health": EXACT, "position": EXACT})
        self.assertEqual(state.confidence_of("health"), UNKNOWN)
        self.assertEqual(state.confidence_of("position"), UNKNOWN)

    def test_a_present_value_keeps_its_declared_provenance(self):
        state = WorldState(position=(1.0, 2.0, 3.0), health=20.0,
                           confidence=INFERRED,
                           provenance={"position": EXACT})
        self.assertEqual(state.confidence_of("position"), EXACT)
        # health had no entry, so it inherits the state-level confidence.
        self.assertEqual(state.confidence_of("health"), INFERRED)

    def test_a_nonsense_provenance_level_is_discarded(self):
        state = WorldState(position=(1, 2, 3), confidence=INFERRED,
                           provenance={"position": "very sure indeed"})
        self.assertIn(state.confidence_of("position"), CONFIDENCE_LEVELS)
        self.assertEqual(state.confidence_of("position"), INFERRED)

    def test_a_nonsense_state_confidence_degrades_to_unknown(self):
        state = WorldState(position=(1, 2, 3), confidence="definitely")
        self.assertEqual(state.confidence_of("position"), UNKNOWN)

    def test_unlisted_field_names_are_unknown_rather_than_an_error(self):
        state = WorldState(position=(1, 2, 3), confidence=EXACT)
        self.assertEqual(state.confidence_of("favourite_colour"), UNKNOWN)


class TestSnapshotsAreImmutable(unittest.TestCase):

    def test_a_state_cannot_be_edited(self):
        state = WorldState(health=20.0)
        with self.assertRaises(Exception):
            state.health = 1.0

    def test_provenance_cannot_be_edited_after_the_fact(self):
        """Otherwise a caller could upgrade its own confidence in a value
        after reading it, which is the same fabrication by another route."""
        state = WorldState(position=(1, 2, 3), confidence=INFERRED)
        with self.assertRaises(TypeError):
            state.provenance["position"] = EXACT

    def test_mutating_the_source_dict_afterwards_does_not_leak_in(self):
        claimed = {"position": INFERRED}
        state = WorldState(position=(1, 2, 3), confidence=INFERRED,
                           provenance=claimed)
        claimed["position"] = EXACT
        self.assertEqual(state.confidence_of("position"), INFERRED)


class TestIntrospection(unittest.TestCase):

    def test_fields_at_least_filters_by_confidence(self):
        state = WorldState(position=(1, 2, 3), biome="plains",
                           confidence=INFERRED,
                           provenance={"position": EXACT})
        self.assertIn("position", state.fields_at_least(EXACT))
        self.assertNotIn("biome", state.fields_at_least(EXACT))
        self.assertIn("biome", state.fields_at_least(INFERRED))

    def test_fields_at_least_with_a_bad_level_returns_nothing(self):
        state = WorldState(position=(1, 2, 3), confidence=EXACT)
        self.assertEqual(state.fields_at_least("extremely"), ())

    def test_known_and_unknown_fields_partition_the_whole_set(self):
        state = WorldState(position=(1, 2, 3), health=20.0, confidence=EXACT)
        known, unknown = set(state.known_fields()), set(state.unknown_fields())
        self.assertEqual(known & unknown, set())
        self.assertEqual(known | unknown, set(WorldState._FIELDS))

    def test_the_new_phase_3_fields_exist_and_start_unknown(self):
        state = WorldState()
        for name in ("facing", "biome", "weather", "held_item", "light_level"):
            with self.subTest(field=name):
                self.assertIn(name, WorldState._FIELDS)
                self.assertIsNone(getattr(state, name))


class TestDescription(unittest.TestCase):

    def test_an_empty_state_says_so_plainly(self):
        text = empty_state("no source attached").describe()
        self.assertIn("cannot read", text.lower())
        self.assertIn("no source attached", text)

    def test_a_populated_state_reports_per_field_confidence(self):
        state = WorldState(position=(1.0, 2.0, 3.0), source="f3",
                           confidence=INFERRED)
        text = state.describe()
        self.assertIn("position", text)
        self.assertIn(INFERRED, text)

    def test_a_populated_state_still_names_what_it_does_not_know(self):
        """So the model relays the gap instead of implying completeness."""
        state = WorldState(position=(1.0, 2.0, 3.0), source="f3",
                           confidence=INFERRED)
        self.assertIn("health", state.describe())

    def test_as_dict_omits_unknown_entries_from_provenance(self):
        state = WorldState(position=(1, 2, 3), confidence=INFERRED)
        provenance = state.as_dict()["provenance"]
        self.assertIn("position", provenance)
        self.assertNotIn("health", provenance)

    def test_nested_objects_serialise(self):
        state = WorldState(
            target_block=BlockRef(x=1, y=2, z=3, name="stone"),
            inventory=(ItemStack(slot=0, name="dirt", count=64),),
            confidence=EXACT)
        payload = state.as_dict()
        self.assertEqual(payload["target_block"]["name"], "stone")
        self.assertEqual(payload["inventory"][0]["count"], 64)


if __name__ == "__main__":
    unittest.main()
