"""
minecraft/observation.py — capture the game, and only the game.

THE RULE
    When the Minecraft window rectangle is known, capture that rectangle.
    Never the whole desktop.

    `actions/screen_processor.py` grabs the entire monitor, which is right for
    "JARVIS, what's on my screen?" and wrong here: a Minecraft frame that
    includes the user's email, their bank tab and whatever is on the second
    monitor is a privacy leak in every frame, forever, and it eventually goes
    to a model. Region capture is also smaller, faster and cheaper in tokens.

    If the rectangle is NOT known, this refuses. It does not fall back to a
    full-screen grab — that fallback is exactly the thing being avoided, and it
    would trigger in precisely the confused situations where it does most harm.

WHY THE COMPRESSION IS LOCAL
    `screen_processor._compress()` does the right thing, but importing it drags
    `actions/` — and numpy, and cv2 — into this package, which the import
    boundary in `tests/test_minecraft_boundary.py` forbids. The dimensions and
    quality here are copied from it so frames look the same as the rest of the
    app's; the fifteen lines of PIL are not worth the coupling.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass

from minecraft.errors import ObservationFailed
from minecraft.window import WindowRect

# Copied from actions/screen_processor.py so a Minecraft frame and a desktop
# frame are the same size and quality going into a model.
MAX_W = 1280
MAX_H = 720
JPEG_QUALITY = 82


@dataclass(frozen=True)
class Observation:
    """One look at the game.

    `ok` False always carries a reason. The frame is bytes or None and never a
    zero-length placeholder, so a caller cannot mistake a failed capture for a
    black screen."""

    ok: bool
    timestamp: float
    focused: bool
    rect: WindowRect | None = None
    frame: bytes | None = None
    mime: str = ""
    width: int = 0
    height: int = 0
    error_class: str = ""
    error: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.frame) if self.frame else 0

    def as_dict(self, include_frame: bool = False) -> dict:
        """Frame excluded by default: this dict goes into tool results and
        audit lines, and a JPEG does not belong in either."""
        out = {
            "ok": self.ok,
            "timestamp": self.timestamp,
            "focused": self.focused,
            "rect": self.rect.as_dict() if self.rect else None,
            "width": self.width,
            "height": self.height,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
        }
        if self.error:
            out["error_class"] = self.error_class
            out["error"] = self.error
        if include_frame and self.frame:
            out["frame"] = self.frame
        return out

    def describe(self) -> str:
        if not self.ok:
            return f"Could not capture Minecraft: {self.error}"
        focus = "focused" if self.focused else "NOT focused"
        return (f"Captured the Minecraft window ({self.width}x{self.height}, "
                f"{self.size_bytes // 1024} KB, {focus}).")


def _compress(raw_png: bytes, width: int, height: int) -> tuple:
    """PNG bytes to a right-sized JPEG. Falls back to the PNG unchanged."""
    try:
        from PIL import Image
    except Exception:
        return raw_png, "image/png", width, height

    try:
        image = Image.open(io.BytesIO(raw_png)).convert("RGB")
        image.thumbnail((MAX_W, MAX_H), Image.BILINEAR)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=False)
        return buffer.getvalue(), "image/jpeg", image.width, image.height
    except Exception:
        return raw_png, "image/png", width, height


class Observer:
    """Captures frames of one window.

    Takes a locator rather than finding the window itself, so the controller's
    view of which window is Minecraft and the observer's cannot drift apart —
    and so a test can supply a fake locator with no screen at all."""

    def __init__(self, locator, grabber=None):
        self._locator = locator
        # Injectable so tests never touch a real screen. None means mss.
        self._grabber = grabber

    def capture(self) -> Observation:
        """One frame of the Minecraft window. Never raises."""
        now = time.time()

        try:
            info = self._locator.probe()
        except Exception as e:
            return Observation(ok=False, timestamp=now, focused=False,
                               error_class=type(e).__name__,
                               error=f"Could not locate the window ({e}).")

        if not info.found or info.rect is None:
            return Observation(
                ok=False, timestamp=now, focused=False,
                error_class="WindowNotFound",
                error=info.detail or "The Minecraft window was not found.",
            )

        if not info.rect.usable:
            return Observation(
                ok=False, timestamp=now, focused=bool(info.foreground),
                rect=info.rect, error_class="WindowNotFound",
                error=(f"The Minecraft window is only "
                       f"{info.rect.width}x{info.rect.height}, which is too "
                       f"small to be the game — it may be minimised."),
            )

        try:
            raw, width, height = self._grab(info.rect)
        except ObservationFailed as e:
            return Observation(ok=False, timestamp=now,
                               focused=bool(info.foreground), rect=info.rect,
                               error_class="ObservationFailed", error=str(e))
        except Exception as e:
            return Observation(ok=False, timestamp=now,
                               focused=bool(info.foreground), rect=info.rect,
                               error_class=type(e).__name__,
                               error=f"Screen capture failed ({e}).")

        frame, mime, out_w, out_h = _compress(raw, width, height)
        return Observation(
            ok=True, timestamp=now, focused=bool(info.foreground),
            rect=info.rect, frame=frame, mime=mime,
            width=out_w, height=out_h,
        )

    def _grab(self, rect: WindowRect) -> tuple:
        """Region grab. The region is always the window — there is no code
        path here that captures a whole monitor."""
        if self._grabber is not None:
            return self._grabber(rect)

        try:
            import mss
            import mss.tools
        except Exception:
            raise ObservationFailed(
                "mss is not installed, so I cannot capture the screen. "
                "Run: pip install mss"
            )

        region = rect.as_mss_region()
        if region["width"] <= 0 or region["height"] <= 0:
            raise ObservationFailed(
                f"The window rectangle is {region['width']}x{region['height']}, "
                f"which cannot be captured."
            )

        with mss.mss() as sct:
            shot = sct.grab(region)
            return (mss.tools.to_png(shot.rgb, shot.size),
                    int(shot.size[0]), int(shot.size[1]))


__all__ = ["Observation", "Observer", "MAX_W", "MAX_H", "JPEG_QUALITY"]
