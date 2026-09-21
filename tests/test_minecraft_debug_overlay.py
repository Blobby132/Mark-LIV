"""
Tests for minecraft/debug_overlay.py — the F3 parser.

These run against captured overlay text with no screen, no OCR engine and no
Minecraft. That is the whole reason the parser is a pure function: the part of
the pipeline that has to be provably right is the part that can be tested
without the game, and the part that cannot be tested here (pixels to text) is
behind an interface and is not trusted.

The samples below are real F3 layouts from different Minecraft versions, plus
deliberately damaged ones — because OCR output IS damaged text, and a parser
that only works on clean input is a parser that only works in its own tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import debug_overlay as f3                              # noqa: E402
from minecraft.state import UNKNOWN, INFERRED                          # noqa: E402


MODERN = """Minecraft 1.20.4 (1.20.4/vanilla)
122 fps T: 120 vsync fancy-clouds gpu:12% B: 2
Integrated server @ 2 ms ticks, 0 tx, 0 rx
C: 220/12000 (s) D: 12, pC: 000, pU: 00, aB: 33
E: 45/120, SD: 12
XYZ: -142.531 / 71.00000 / 305.219
Block: -143 71 305
Chunk: 1 7 1 in -9 4 19
Facing: south (Towards positive Z) (12.7 / -3.4)
Client Light: 11 (11 sky, 0 block)
Server Light: (11 sky, 0 block)
Biome: minecraft:forest
Local Difficulty: 2.25 // 0.00 (Day 14)

Targeted Block: -143, 70, 306
minecraft:oak_log
axis: y
"""

LOOKING_AT_SKY = """XYZ: 10.5 / 64.0 / -20.25
Block: 10 64 -20
Facing: west (Towards negative X) (90.0 / -45.0)
Biome: minecraft:plains
Client Light: 15 (15 sky, 0 block)
"""

# What OCR actually hands back: broken lines, lost punctuation, stray glyphs.
GARBLED = """M1necraft 1.20.4
XYZ: -142.531 / 71.00000 / 305.219
B1ock: ~143 7l 3O5
Fac1ng: souih
B1ome: m1necraft:f0rest
Cl1ent L1ght: 11 (11 sky O block)
"""

NOT_AN_OVERLAY = """You have died!
Score: 42
Respawn
Title Screen
"""


class TestOverlayDetection(unittest.TestCase):

    def test_a_real_overlay_is_recognised(self):
        self.assertTrue(f3.looks_like_overlay(MODERN))
        self.assertTrue(f3.looks_like_overlay(LOOKING_AT_SKY))

    def test_unrelated_screen_text_is_not_an_overlay(self):
        self.assertFalse(f3.looks_like_overlay(NOT_AN_OVERLAY))

    def test_empty_input_is_not_an_overlay(self):
        self.assertFalse(f3.looks_like_overlay(""))
        self.assertFalse(f3.looks_like_overlay(None))

    def test_one_stray_keyword_is_not_enough(self):
        """A sign, a chat message or a book could contain the word "biome".
        Building a state out of someone's text would be the worst kind of
        fabrication, so detection needs two independent markers."""
        self.assertFalse(f3.looks_like_overlay("the biome here is lovely"))


class TestParsingRealOverlays(unittest.TestCase):

    def test_modern_overlay_yields_every_supported_field(self):
        found = f3.parse_overlay(MODERN)
        self.assertEqual(found["position"], (-142.531, 71.0, 305.219))
        self.assertEqual(found["block_position"], (-143, 71, 305))
        self.assertEqual(found["facing"], "south")
        self.assertEqual(found["rotation"], (12.7, -3.4))
        self.assertEqual(found["biome"], "forest")
        self.assertEqual(found["light_level"], 11)

    def test_the_targeted_block_is_read_with_its_position_and_face(self):
        block = f3.parse_overlay(MODERN)["target_block"]
        self.assertEqual((block.x, block.y, block.z), (-143, 70, 306))
        self.assertEqual(block.name, "oak_log")
        self.assertEqual(block.face, "y")

    def test_the_biome_is_not_mistaken_for_the_targeted_block(self):
        """Both are namespaced `minecraft:` names and the biome comes first in
        the text. Taking the first match in the whole document would report
        the player is looking at a forest."""
        block = f3.parse_overlay(MODERN)["target_block"]
        self.assertEqual(block.name, "oak_log")
        self.assertNotEqual(block.name, "forest")

    def test_no_targeted_block_when_looking_at_the_sky(self):
        found = f3.parse_overlay(LOOKING_AT_SKY)
        self.assertNotIn("target_block", found)
        # But everything else still reads — an absent target is an answer,
        # not a failed parse.
        self.assertEqual(found["position"], (10.5, 64.0, -20.25))
        self.assertEqual(found["facing"], "west")

    def test_negative_and_decimal_coordinates_survive(self):
        found = f3.parse_overlay(MODERN)
        x, y, z = found["position"]
        self.assertLess(x, 0)
        self.assertAlmostEqual(y, 71.0)
        self.assertAlmostEqual(z, 305.219)


class TestDamagedInput(unittest.TestCase):
    """OCR output is damaged text. A field that cannot be read must go
    missing, never be half-read into a plausible wrong number."""

    def test_a_garbled_line_is_dropped_not_guessed(self):
        found = f3.parse_overlay(GARBLED)
        # "B1ock: ~143 7l 3O5" is unreadable; it must not appear at all.
        self.assertNotIn("block_position", found)
        # The line that survived OCR intact is still read.
        self.assertEqual(found["position"], (-142.531, 71.0, 305.219))

    def test_a_misspelled_label_does_not_match(self):
        found = f3.parse_overlay(GARBLED)
        self.assertNotIn("facing", found)      # "Fac1ng"
        self.assertNotIn("biome", found)       # "B1ome"

    def test_nothing_parses_out_of_unrelated_text(self):
        self.assertEqual(f3.parse_overlay(NOT_AN_OVERLAY), {})

    def test_empty_input_parses_to_nothing(self):
        self.assertEqual(f3.parse_overlay(""), {})
        self.assertEqual(f3.parse_overlay(None), {})

    def test_an_impossible_light_level_is_rejected(self):
        """0-15 is the entire range. 71 is a misread of another line, and
        accepting it would put a number nothing can produce into the state."""
        found = f3.parse_overlay("XYZ: 1 / 2 / 3\nFacing: north\n"
                                 "Client Light: 71 (sky)")
        self.assertNotIn("light_level", found)

    def test_a_partial_coordinate_is_not_completed(self):
        found = f3.parse_overlay("XYZ: 10.5 / 64.0\nFacing: north\nBiome: x")
        self.assertNotIn("position", found)


class TestStateSource(unittest.TestCase):

    class _Frame:
        ok = True
        frame = b"not-really-a-jpeg"
        error = ""
        timestamp = 1000.0

    class _Observer:
        def __init__(self, obs):
            self._obs = obs
            self.compress_asked = None
        def capture(self, compress=True):
            # Records what was asked for: the F3 reader must never be handed
            # a downscaled frame.
            self.compress_asked = compress
            return self._obs

    class _Reader:
        available = True
        name = "test"
        def __init__(self, text):
            self.text = text
        def read_text(self, frame):
            return self.text
        def describe(self):
            return "test reader"

    def _source(self, text, observation=None):
        return f3.DebugOverlayStateSource(
            observer=self._Observer(observation or self._Frame()),
            reader=self._Reader(text))

    def test_a_good_read_produces_inferred_provenance(self):
        state = self._source(MODERN).read()
        self.assertEqual(state.source, "f3-overlay")
        self.assertEqual(state.confidence_of("position"), INFERRED)
        self.assertEqual(state.confidence_of("target_block"), INFERRED)
        self.assertEqual(state.position, (-142.531, 71.0, 305.219))

    def test_fields_f3_cannot_supply_stay_unknown(self):
        """The important one. Health is not on the debug screen, and a source
        that filled it in from anything would be fabricating."""
        state = self._source(MODERN).read()
        for name in ("health", "hunger", "inventory", "selected_slot",
                     "held_item", "nearby_entities", "weather"):
            with self.subTest(field=name):
                self.assertIsNone(getattr(state, name))
                self.assertEqual(state.confidence_of(name), UNKNOWN)

    def test_a_closed_overlay_is_reported_as_such(self):
        state = self._source(NOT_AN_OVERLAY).read()
        self.assertTrue(state.is_empty)
        self.assertIn("F3", state.notes)

    def test_no_reader_means_unavailable_not_empty_success(self):
        source = f3.DebugOverlayStateSource(observer=self._Observer(self._Frame()))
        self.assertFalse(source.available())
        self.assertTrue(source.unavailable_reason())
        self.assertTrue(source.read().is_empty)

    def test_looking_at_the_sky_becomes_an_explicit_air_block(self):
        """The distinction the whole verification layer rests on.

        A readable overlay with no "Targeted Block" section means the crosshair
        hit nothing — a real reading. It must NOT arrive as None, which is
        reserved for "could not read", or breaking the last block in front of
        you could never be confirmed."""
        state = self._source(LOOKING_AT_SKY).read()
        self.assertIsNotNone(state.target_block)
        self.assertEqual(state.target_block.name, "air")
        self.assertEqual(state.confidence_of("target_block"), INFERRED)

    def test_an_unreadable_overlay_leaves_the_target_unknown(self):
        state = self._source(NOT_AN_OVERLAY).read()
        self.assertIsNone(state.target_block)
        self.assertEqual(state.confidence_of("target_block"), UNKNOWN)

    def test_a_failed_capture_does_not_raise(self):
        class Broken:
            def capture(self, compress=True):
                raise RuntimeError("no screen")
        source = f3.DebugOverlayStateSource(observer=Broken(),
                                            reader=self._Reader(MODERN))
        state = source.read()
        self.assertTrue(state.is_empty)
        self.assertIn("capture", state.notes.lower())

    def test_a_reader_that_throws_does_not_raise(self):
        class Angry:
            available = True
            name = "angry"
            def read_text(self, frame):
                raise ValueError("bad image")
            def describe(self):
                return "angry"
        source = f3.DebugOverlayStateSource(
            observer=self._Observer(self._Frame()), reader=Angry())
        self.assertTrue(source.read().is_empty)


class TestDeclaredLimits(unittest.TestCase):

    def test_the_fields_it_cannot_supply_are_named(self):
        for name in ("health", "hunger", "inventory"):
            self.assertIn(name, f3.CANNOT_SUPPLY)

    def test_supplies_and_cannot_supply_do_not_overlap(self):
        self.assertEqual(set(f3.SUPPLIES) & set(f3.CANNOT_SUPPLY), set())


if __name__ == "__main__":
    unittest.main()
