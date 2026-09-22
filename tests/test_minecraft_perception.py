"""
Tests for minecraft/perception.py.

THE RULE THESE EXIST TO HOLD
    The bridge knows which block; vision knows what colour something is.
    Those are different kinds of evidence, and only one of them is good
    enough to hold attack on. `may_destroy` is True for exact bridge data and
    nothing else, and a visual reading never outranks the bridge.

WHAT THEY DO NOT PROVE
    The visual tests use synthetic patches filled with approximate vanilla
    texture colours plus noise. Real frames have lighting, shading, fog,
    particles, the hand and the hotbar in view, resource packs and shaders.
    Whether the classifier is any use on YOUR screen is a manual test.
"""

from __future__ import annotations

import io
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import perception as per                                # noqa: E402
from minecraft.state import BlockRef, EXACT, WorldState, empty_state   # noqa: E402

try:
    from PIL import Image
except ImportError:                                                    # pragma: no cover
    Image = None


def frame(rgb, noise=12, scale=1.0, size=(160, 120), seed=1):
    rnd = random.Random(seed)
    image = Image.new("RGB", size)
    pixels = image.load()
    for x in range(size[0]):
        for y in range(size[1]):
            pixels[x, y] = tuple(
                max(0, min(255, int(c * scale + rnd.randint(-noise, noise))))
                for c in rgb)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def bridge_state(name="oak_log", position=(5, 71, -232), face="north"):
    target = (BlockRef(name=name, x=position[0], y=position[1], z=position[2],
                       face=face) if position else BlockRef(name=name))
    return WorldState(position=(0.5, 64.0, 0.5), rotation=(0.0, 0.0),
                      target_block=target, source="test", confidence=EXACT)


class ProvenanceTests(unittest.TestCase):

    def test_the_bridge_is_exact_and_may_authorise_mining(self):
        seen = per.crosshair(bridge_state())
        self.assertEqual(seen.source, per.EXACT_BRIDGE)
        self.assertEqual(seen.position, (5, 71, -232))
        self.assertEqual(seen.confidence, 1.0)
        self.assertTrue(seen.may_destroy)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_vision_never_authorises_mining(self):
        """"That looks like wood" is not evidence that the thing under the
        crosshair is the log you meant."""
        seen = per.classify_frame(frame((109, 85, 50)))
        self.assertEqual(seen.name, "wood")
        self.assertIn(seen.source, (per.VISUAL_HIGH, per.VISUAL_LOW))
        self.assertFalse(seen.may_destroy)
        self.assertIsNone(seen.position, "vision cannot know coordinates")

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_the_bridge_wins_even_when_vision_disagrees(self):
        """Vision is a fallback, never a second opinion on the game's data."""
        stone_frame = frame((125, 125, 125))
        seen = per.crosshair(bridge_state("oak_log"), stone_frame)
        self.assertEqual(seen.name, "oak_log")
        self.assertEqual(seen.source, per.EXACT_BRIDGE)

    def test_looking_at_the_sky_is_an_answer_not_a_gap(self):
        seen = per.crosshair(bridge_state("minecraft:air", position=None))
        self.assertEqual(seen.source, per.EXACT_BRIDGE)
        self.assertEqual(seen.name, "air")
        self.assertFalse(seen.may_destroy, "there is nothing there to break")

    def test_no_bridge_and_no_frame_is_unknown(self):
        seen = per.crosshair(empty_state("nothing"), None)
        self.assertEqual(seen.source, per.UNKNOWN_SOURCE)
        self.assertFalse(seen.known)
        self.assertFalse(seen.may_destroy)

    @unittest.skipIf(Image is None, "Pillow not installed")
    def test_visual_confidence_is_capped(self):
        """A colour match cannot be as good as the game's own data, and a
        number suggesting otherwise invites the mistake this module exists
        to prevent."""
        for rgb in ((125, 125, 125), (120, 167, 255), (219, 207, 163)):
            seen = per.classify_frame(frame(rgb, noise=1))
            self.assertLessEqual(seen.confidence,
                                 per.VISUAL_CONFIDENCE_CEILING)

    def test_the_description_says_which_kind_of_answer_it_is(self):
        exact = per.crosshair(bridge_state()).describe()
        self.assertIn("from the game itself", exact)
        guess = per.BlockObservation("wood", None, per.VISUAL_LOW, 0.4)
        self.assertIn("visual guess", guess.describe())
        self.assertIn("possibly", guess.describe())


@unittest.skipIf(Image is None, "Pillow not installed")
class VisualClassifierTests(unittest.TestCase):
    """Synthetic patches of approximate vanilla colours. See the module
    docstring for what that does and does not show."""

    def check(self, rgb, expected, **kwargs):
        seen = per.classify_frame(frame(rgb, **kwargs))
        self.assertEqual(seen.name, expected,
                         f"{rgb} read as {seen.name} ({seen.detail})")
        return seen

    def test_the_common_materials(self):
        for rgb, expected, noise in (((109, 85, 50), "wood", 12),
                                     ((125, 125, 125), "stone", 18),
                                     ((134, 96, 67), "dirt", 18),
                                     ((95, 159, 53), "grass", 20),
                                     ((219, 207, 163), "sand", 12),
                                     ((63, 118, 228), "water", 12),
                                     ((207, 91, 20), "lava", 18),
                                     ((120, 167, 255), "sky", 2)):
            with self.subTest(expected=expected):
                self.check(rgb, expected, noise=noise)

    def test_darker_light_lowers_confidence_rather_than_changing_the_answer(self):
        day = self.check((109, 85, 50), "wood")
        dusk = self.check((109, 85, 50), "wood", scale=0.55, noise=8)
        self.assertLess(dusk.confidence, day.confidence)

    def test_something_unrecognisable_is_unknown_not_the_least_wrong_guess(self):
        seen = per.classify_frame(frame((220, 30, 200)))
        self.assertEqual(seen.source, per.UNKNOWN_SOURCE)
        self.assertIn("closest", seen.detail)

    def test_darkness_is_unknown(self):
        seen = per.classify_frame(frame((5, 5, 5), noise=2))
        self.assertEqual(seen.source, per.UNKNOWN_SOURCE)
        self.assertIn("dark", seen.detail)

    def test_a_grey_is_never_called_a_coloured_material(self):
        """The hue of a grey is rounding noise. Stone must not be read as
        wood because its average happened to lean warm."""
        seen = per.classify_frame(frame((128, 124, 120), noise=10))
        self.assertNotIn(seen.name, ("wood", "dirt", "sand", "lava"))

    def test_garbage_bytes_are_unknown(self):
        seen = per.classify_frame(b"not an image")
        self.assertEqual(seen.source, per.UNKNOWN_SOURCE)

    def test_only_the_centre_is_sampled(self):
        """The hotbar and the hand sit at the edges; the crosshair does not."""
        image = Image.new("RGB", (200, 200), (125, 125, 125))
        for x in range(200):
            for y in range(0, 60):
                image.putpixel((x, y), (63, 118, 228))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        self.assertEqual(per.classify_frame(buffer.getvalue()).name, "stone")


class BoundaryTests(unittest.TestCase):

    def test_perception_cannot_press_anything(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(per))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported - {"__future__"},
                         {"colorsys", "io", "math", "dataclasses",
                          "minecraft", "PIL"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
