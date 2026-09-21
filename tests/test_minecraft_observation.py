"""
Observation tests — the Minecraft window, and never the whole desktop.

The property under test: when a window rectangle is known, the capture region
is exactly that rectangle. When it is not known, nothing is captured at all —
there is no full-screen fallback, because that fallback would fire precisely
in the confused situations where it does most harm.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft.errors import ObservationFailed                    # noqa: E402
from minecraft.observation import Observer                        # noqa: E402
from minecraft.state import (                                     # noqa: E402
    UNKNOWN, VisionStateSource, WorldState, empty_state,
)
from minecraft.window import WindowInfo, WindowRect               # noqa: E402


class RecordingGrabber:
    """Stands in for mss. Records the region it was asked for."""

    def __init__(self, fail_with=None):
        self.regions: list = []
        self._fail_with = fail_with

    def __call__(self, rect: WindowRect):
        self.regions.append(rect.as_mss_region())
        if self._fail_with:
            raise self._fail_with
        # A 2x2 PNG. Small enough to be free, real enough to be compressed.
        png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00"
               b"\x00\x02\x08\x02\x00\x00\x00\xfd\xd4\x9as\x00\x00\x00\x16"
               b"IDAT\x18Wc\xfc\xcf\xc0\xf0\x9f\x81\x81\x01\x13\x03\x03\x03"
               b"\x03\x00\x1a\xf4\x02\xfe\x8e\x8a\xc9\xad\x00\x00\x00\x00"
               b"IEND\xaeB`\x82")
        return png, rect.width, rect.height


class StubLocator:
    def __init__(self, info: WindowInfo):
        self.info = info
        self.pid = info.pid

    def attach(self):
        return self.info

    def probe(self):
        return self.info


def _window(found=True, foreground=True, left=100, top=200,
            width=1280, height=720) -> WindowInfo:
    rect = WindowRect(left, top, width, height) if found else None
    return WindowInfo(found=found, handle=7, title="Minecraft 1.21", pid=1234,
                      rect=rect, foreground=foreground, focus_known=True,
                      detail="stub")


class TestRegionCapture(unittest.TestCase):

    def test_only_the_window_rectangle_is_captured(self):
        grabber = RecordingGrabber()
        observer = Observer(StubLocator(_window(left=100, top=200,
                                                width=1280, height=720)),
                            grabber=grabber)
        observation = observer.capture()

        self.assertTrue(observation.ok, observation.error)
        self.assertEqual(grabber.regions,
                         [{"left": 100, "top": 200,
                           "width": 1280, "height": 720}])

    def test_the_region_follows_the_window(self):
        for left, top, width, height in ((0, 0, 854, 480),
                                         (1920, 0, 2560, 1440),
                                         (-1080, 300, 1280, 720)):
            with self.subTest(rect=(left, top, width, height)):
                grabber = RecordingGrabber()
                observer = Observer(
                    StubLocator(_window(left=left, top=top,
                                        width=width, height=height)),
                    grabber=grabber)
                observer.capture()
                self.assertEqual(grabber.regions[-1],
                                 {"left": left, "top": top,
                                  "width": width, "height": height})

    def test_the_full_desktop_is_never_captured(self):
        """The whole point. A region equal to the virtual screen origin with no
        bounds would be the old full-screen grab sneaking back in."""
        grabber = RecordingGrabber()
        observer = Observer(StubLocator(_window()), grabber=grabber)
        observer.capture()
        for region in grabber.regions:
            self.assertIn("width", region)
            self.assertIn("height", region)
            self.assertGreater(region["width"], 0)
            self.assertGreater(region["height"], 0)
            self.assertNotEqual(region, {"left": 0, "top": 0,
                                         "width": 0, "height": 0})

    def test_no_capture_happens_when_the_window_is_unknown(self):
        grabber = RecordingGrabber()
        observer = Observer(StubLocator(_window(found=False)), grabber=grabber)
        observation = observer.capture()

        self.assertFalse(observation.ok)
        self.assertEqual(grabber.regions, [],
                         "a frame was grabbed with no window rectangle")
        self.assertEqual(observation.error_class, "WindowNotFound")
        self.assertIsNone(observation.frame)

    def test_a_too_small_window_is_refused(self):
        grabber = RecordingGrabber()
        observer = Observer(StubLocator(_window(width=10, height=10)),
                            grabber=grabber)
        observation = observer.capture()
        self.assertFalse(observation.ok)
        self.assertEqual(grabber.regions, [])
        self.assertIn("too small", observation.error)


class TestObservationResult(unittest.TestCase):

    def test_a_successful_capture_reports_what_the_brief_asked_for(self):
        observer = Observer(StubLocator(_window()), grabber=RecordingGrabber())
        observation = observer.capture()

        self.assertTrue(observation.ok)
        self.assertIsNotNone(observation.frame)          # frame
        self.assertIsNotNone(observation.rect)           # window rectangle
        self.assertGreater(observation.timestamp, 0)     # timestamp
        self.assertTrue(observation.focused)             # focus state
        self.assertIn(observation.mime, ("image/jpeg", "image/png"))

    def test_focus_state_is_reported_honestly(self):
        observer = Observer(StubLocator(_window(foreground=False)),
                            grabber=RecordingGrabber())
        observation = observer.capture()
        self.assertTrue(observation.ok, "observing should work unfocused")
        self.assertFalse(observation.focused)
        self.assertIn("NOT focused", observation.describe())

    def test_the_frame_is_left_out_of_the_dict_by_default(self):
        observer = Observer(StubLocator(_window()), grabber=RecordingGrabber())
        payload = observer.capture().as_dict()
        self.assertNotIn("frame", payload,
                         "a JPEG must not land in a tool result or audit line")
        self.assertGreater(payload["size_bytes"], 0)

    def test_a_grab_failure_is_a_structured_error(self):
        observer = Observer(
            StubLocator(_window()),
            grabber=RecordingGrabber(fail_with=ObservationFailed("mss is missing")))
        observation = observer.capture()

        self.assertFalse(observation.ok)
        self.assertEqual(observation.error_class, "ObservationFailed")
        self.assertIn("mss is missing", observation.error)
        self.assertIsNone(observation.frame)

    def test_an_unexpected_grab_error_is_still_structured(self):
        observer = Observer(
            StubLocator(_window()),
            grabber=RecordingGrabber(fail_with=RuntimeError("X server gone")))
        observation = observer.capture()
        self.assertFalse(observation.ok)
        self.assertEqual(observation.error_class, "RuntimeError")
        self.assertIn("X server gone", observation.error)

    def test_a_locator_that_raises_is_handled(self):
        class ExplodingLocator:
            pid = 1

            def probe(self):
                raise OSError("window server unavailable")

        observation = Observer(ExplodingLocator()).capture()
        self.assertFalse(observation.ok)
        self.assertEqual(observation.error_class, "OSError")


class TestWorldState(unittest.TestCase):

    def test_a_fresh_state_knows_nothing_and_says_so(self):
        state = WorldState()
        self.assertTrue(state.is_empty)
        self.assertEqual(state.known_fields(), ())
        self.assertEqual(state.confidence, UNKNOWN)
        for field in ("position", "rotation", "health", "hunger", "inventory",
                      "selected_slot", "target_block", "target_entity",
                      "dimension", "time_of_day", "nearby_entities"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(state, field))

    def test_the_vision_source_does_not_guess(self):
        state = VisionStateSource().read()
        self.assertTrue(state.is_empty,
                        "the vision source invented state it cannot read")
        self.assertEqual(state.confidence, UNKNOWN)

    def test_the_description_admits_ignorance(self):
        text = VisionStateSource().read().describe().lower()
        self.assertIn("cannot read", text)
        self.assertIn("do not know", text)

    def test_provenance_is_always_present(self):
        state = VisionStateSource().read()
        self.assertTrue(state.source)
        self.assertIn(state.confidence, ("unknown", "inferred", "exact"))
        self.assertGreater(state.captured_at, 0)

    def test_known_fields_reflect_what_is_actually_set(self):
        state = WorldState(position=(10.0, 64.0, -30.0), health=18.0,
                           source="mod_bridge", confidence="exact")
        self.assertEqual(set(state.known_fields()), {"position", "health"})
        self.assertIn("hunger", state.unknown_fields())
        self.assertFalse(state.is_empty)
        self.assertIn("position", state.describe())

    def test_the_state_serialises_without_losing_the_unknowns(self):
        payload = VisionStateSource().read().as_dict()
        self.assertEqual(payload["known_fields"], [])
        self.assertIn("position", payload["unknown_fields"])
        self.assertIsNone(payload["health"])

    def test_empty_state_carries_its_reason(self):
        state = empty_state("no source connected")
        self.assertTrue(state.is_empty)
        self.assertIn("no source connected", state.notes)


class TestCaptureResolution(unittest.TestCase):
    """Two consumers with opposite requirements.

    A vision model wants a small frame; OCR wants every pixel. The F3 overlay
    is a small fixed-size font, so on a 2560-wide window the downscale halved
    it to around nine pixels tall and the JPEG quantiser smeared the rest.
    That failed in the worst way — the overlay is plainly visible on screen,
    so the obvious conclusion was that OCR was broken rather than that it had
    been handed a bad picture."""

    WIDTH, HEIGHT = 2576, 1408

    def _observer(self):
        def grab(rect):
            return b"\x89PNG-pretend", self.WIDTH, self.HEIGHT

        class Locator:
            def probe(self):
                rect = WindowRect(0, 0, TestCaptureResolution.WIDTH,
                                  TestCaptureResolution.HEIGHT)
                return WindowInfo(found=True, handle=1, title="Minecraft",
                                  pid=1, rect=rect, foreground=True,
                                  focus_known=True, detail="fake")

        return Observer(Locator(), grabber=grab)

    def test_uncompressed_capture_keeps_native_resolution(self):
        observation = self._observer().capture(compress=False)
        self.assertTrue(observation.ok)
        self.assertEqual((observation.width, observation.height),
                         (self.WIDTH, self.HEIGHT))
        self.assertEqual(observation.mime, "image/png")

    def test_compression_is_still_the_default(self):
        """Every existing caller is unchanged; only the text reader opts out."""
        import inspect
        signature = inspect.signature(Observer.capture)
        self.assertIs(signature.parameters["compress"].default, True)

    def test_the_overlay_source_asks_for_native_resolution(self):
        """The regression. If this reverts, the F3 reader silently goes back
        to reading a halved, JPEG-smeared picture of the text."""
        from minecraft.debug_overlay import DebugOverlayStateSource

        asked = {}

        class Recorder:
            def capture(self, compress=True):
                asked["compress"] = compress
                return Observation(ok=False, timestamp=0.0, focused=True,
                                   error="not a real capture")

        class Reader:
            available = True
            name = "test"
            def read_text(self, frame):
                return ""
            def describe(self):
                return "test"

        DebugOverlayStateSource(observer=Recorder(), reader=Reader()).read()
        self.assertIs(asked.get("compress"), False,
                      "the F3 reader must not be given a downscaled frame")


if __name__ == "__main__":
    unittest.main()
