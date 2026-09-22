"""
minecraft/perception.py — what JARVIS thinks it is looking at, and how sure.

ONE ANSWER, WITH ITS SOURCE ATTACHED
    Several things can say what is in front of the player, and they are not
    equally trustworthy:

        exact_bridge            the game's own data, via the mod. Names the
                                block AND its coordinates. Authoritative.
        visual_high_confidence  the colours and texture at the screen centre
                                match one material clearly and nothing else.
        visual_low_confidence   they match, but not decisively.
        unknown                 nothing could tell.

    Every observation carries one of these, and the planner is expected to
    care: `may_destroy` is True only for exact_bridge. A visual guess can
    point the camera or suggest where to walk; it can never, on its own,
    authorise holding attack on something. "That looks like wood" is not
    evidence that the thing under the crosshair is the log you meant.

WHY VISION AT ALL, THEN
    Because the bridge is not always there — an older jar, a modpack the mod
    does not load in, the moment between launching and the first snapshot —
    and "I cannot see anything" is a worse answer than "that looks like
    stone, though I cannot be sure". And because it answers questions the
    bridge does not ask: what the whole view looks like, not just the one
    block under the crosshair.

WHAT THE VISUAL CLASSIFIER ACTUALLY IS
    Honest about its limits: it samples a patch at the screen centre and
    compares its colour statistics — hue, saturation, brightness and how
    much the brightness varies — against reference signatures of vanilla
    textures in daylight. It knows MATERIALS (wood, stone, dirt, grass,
    sand, water, leaves, lava, sky), not block ids, and it knows nothing
    about coordinates.

    It will be confused by resource packs, shaders, night, heavy biome tints
    and anything unusual. That is why its confidence is capped, why it
    reports categories rather than block names, and why it never authorises
    anything destructive. It has NOT been checked against real frames from
    your game; the tests use synthetic patches. Treat it as a hint.

NO INPUT
    This module reads state and images. It presses nothing.
"""

from __future__ import annotations

import colorsys
import io
import math
from dataclasses import dataclass

from minecraft.state import UNKNOWN

EXACT_BRIDGE = "exact_bridge"
VISUAL_HIGH = "visual_high_confidence"
VISUAL_LOW = "visual_low_confidence"
UNKNOWN_SOURCE = "unknown"

SOURCES = (EXACT_BRIDGE, VISUAL_HIGH, VISUAL_LOW, UNKNOWN_SOURCE)

VISUAL_CONFIDENCE_CEILING = 0.85
"""No visual reading is ever reported as more certain than this.

A colour match cannot be as good as the game's own data, and a number that
suggested otherwise would invite exactly the mistake this module is designed
to prevent."""

HIGH_CONFIDENCE_AT = 0.6

PATCH_FRACTION = 0.04
"""The sample is this fraction of the frame's shorter side, centred.

Small enough to be one block at a normal distance, large enough to average
over a texture's pattern rather than land on one dark pixel of bark."""


@dataclass(frozen=True)
class BlockObservation:
    """What is at a place, according to one source."""

    name: str | None
    position: tuple | None
    source: str
    confidence: float
    face: str | None = None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.source != UNKNOWN_SOURCE and self.name is not None

    @property
    def may_destroy(self) -> bool:
        """Is this good enough evidence to hold attack on?

        Only the bridge. It is the only source that knows WHICH block —
        coordinates and all — rather than what colour it is."""
        return (self.source == EXACT_BRIDGE and self.position is not None
                and self.name not in (None, "air"))

    def as_dict(self) -> dict:
        return {"name": self.name, "position": list(self.position)
                if self.position else None, "source": self.source,
                "confidence": round(self.confidence, 2), "face": self.face,
                "detail": self.detail}

    def describe(self) -> str:
        if not self.known:
            return f"I cannot tell what that is{': ' + self.detail if self.detail else ''}"
        where = f" at {self.position}" if self.position else ""
        if self.source == EXACT_BRIDGE:
            return f"{self.name}{where} (from the game itself)"
        sure = "probably" if self.source == VISUAL_HIGH else "possibly"
        return (f"{sure} {self.name} ({self.confidence:.0%} by colour — "
                f"a visual guess, not the game's data)")


# ── Visual ──────────────────────────────────────────────────────────────────
#
# Reference signatures: hue in degrees, saturation 0..1, value 0..1 and the
# spread of value across the patch. Averages of vanilla textures in
# daylight, rounded; grass, leaves and water are biome-tinted in game and
# their ranges below are correspondingly wide.

@dataclass(frozen=True)
class _Signature:
    name: str
    hue: float | None       # None: hue is noise for this material (greys)
    saturation: float
    value: float
    texture: float           # typical std-dev of value across the patch
    hue_tolerance: float = 25.0


SIGNATURES = (
    _Signature("wood",   hue=33,  saturation=0.52, value=0.43, texture=0.10),
    _Signature("birch",  hue=None, saturation=0.06, value=0.82, texture=0.18),
    _Signature("stone",  hue=None, saturation=0.03, value=0.49, texture=0.07),
    _Signature("dirt",   hue=25,  saturation=0.50, value=0.52, texture=0.08),
    _Signature("grass",  hue=95,  saturation=0.65, value=0.60, texture=0.09,
               hue_tolerance=35),
    _Signature("sand",   hue=45,  saturation=0.26, value=0.86, texture=0.05),
    _Signature("water",  hue=222, saturation=0.72, value=0.85, texture=0.05,
               hue_tolerance=30),
    _Signature("leaves", hue=100, saturation=0.72, value=0.40, texture=0.16,
               hue_tolerance=35),
    _Signature("lava",   hue=22,  saturation=0.90, value=0.85, texture=0.08,
               hue_tolerance=15),
    _Signature("sky",    hue=218, saturation=0.53, value=0.99, texture=0.01,
               hue_tolerance=25),
)

MAX_MATCH_DISTANCE = 0.55
"""Beyond this the nearest signature is not a match, just the least wrong."""


def _hue_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def _distance(signature: _Signature, hue, saturation, value, texture) -> float:
    """How unlike a signature a patch is. 0 is identical.

    Brightness is weighted lightly: the same log is much darker at dusk or in
    shade, and a classifier that leaned on brightness would change its mind
    with the time of day. Hue carries the most weight, but only when the
    colour is saturated enough for hue to mean anything — the hue of a grey
    is rounding noise."""
    d = 0.0
    if signature.hue is not None:
        if saturation < 0.12:
            d += 0.6                       # a grey cannot be a coloured thing
        else:
            d += 0.9 * min(1.0, _hue_gap(hue, signature.hue)
                           / (signature.hue_tolerance * 2.0))
    else:
        d += 1.2 * max(0.0, saturation - 0.12)
    d += 0.6 * abs(saturation - signature.saturation)
    d += 0.2 * abs(value - signature.value)
    d += 0.8 * abs(texture - signature.texture)
    return d


def patch_features(frame: bytes, fraction: float = PATCH_FRACTION):
    """(hue_deg, saturation, value, texture) of the centre of a frame, or None.

    Uses PIL only. Averages RGB first and converts once, which is both faster
    and more stable than averaging hues (a hue average across the 0/360 seam
    lands on cyan)."""
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return None
    try:
        image = Image.open(io.BytesIO(frame)).convert("RGB")
    except Exception:
        return None
    width, height = image.size
    side = max(4, int(min(width, height) * fraction))
    left = max(0, width // 2 - side // 2)
    top = max(0, height // 2 - side // 2)
    patch = image.crop((left, top, left + side, top + side))

    stat = ImageStat.Stat(patch)
    r, g, b = (channel / 255.0 for channel in stat.mean[:3])
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)

    grey = ImageStat.Stat(patch.convert("L"))
    texture = (grey.stddev[0] / 255.0) if grey.stddev else 0.0
    return hue * 360.0, saturation, value, texture


def classify_frame(frame: bytes) -> BlockObservation:
    """What the centre of the frame looks like, by colour and texture."""
    if not frame:
        return BlockObservation(None, None, UNKNOWN_SOURCE, 0.0,
                                detail="no frame")
    features = patch_features(frame)
    if features is None:
        return BlockObservation(None, None, UNKNOWN_SOURCE, 0.0,
                                detail="the frame could not be read")
    hue, saturation, value, texture = features

    if value < 0.08:
        return BlockObservation(None, None, UNKNOWN_SOURCE, 0.0,
                                detail="too dark to tell")

    scored = sorted((_distance(s, hue, saturation, value, texture), s.name)
                    for s in SIGNATURES)
    best, name = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else best + 1.0

    if best > MAX_MATCH_DISTANCE:
        return BlockObservation(None, None, UNKNOWN_SOURCE, 0.0,
                                detail=f"nothing I recognise (closest: "
                                       f"{name})")

    closeness = 1.0 - best / MAX_MATCH_DISTANCE
    margin = min(1.0, (runner_up - best) / max(best, 0.05))
    confidence = min(VISUAL_CONFIDENCE_CEILING,
                     closeness * (0.5 + 0.5 * margin))
    source = VISUAL_HIGH if confidence >= HIGH_CONFIDENCE_AT else VISUAL_LOW
    return BlockObservation(name, None, source, confidence,
                            detail=f"hue {hue:.0f}°, saturation "
                                   f"{saturation:.2f}, runner-up "
                                   f"{scored[1][1]}")


# ── The unified view ────────────────────────────────────────────────────────

def from_bridge(state) -> BlockObservation | None:
    """The block under the crosshair, from the game's own data, or None.

    None when the bridge did not report it — which is different from the
    bridge reporting air, and the difference is kept: looking at the sky is
    an answer, not a gap."""
    if state is None or state.confidence_of("target_block") == UNKNOWN:
        return None
    block = state.target_block
    if block is None:
        return None
    name = str(getattr(block, "name", "") or "").split(":")[-1] or None
    try:
        position = (int(block.x), int(block.y), int(block.z))
    except (TypeError, ValueError):
        position = None
    return BlockObservation(name=name, position=position, source=EXACT_BRIDGE,
                            confidence=1.0, face=getattr(block, "face", None))


def crosshair(state=None, frame: bytes | None = None) -> BlockObservation:
    """What is under the crosshair, from the best source available.

    The bridge wins whenever it has an answer. Vision is consulted only when
    it does not — never to second-guess it, because the game's own data
    cannot be improved on by looking at its pixels."""
    exact = from_bridge(state)
    if exact is not None:
        return exact
    if frame:
        return classify_frame(frame)
    return BlockObservation(None, None, UNKNOWN_SOURCE, 0.0,
                            detail="no bridge data and no frame")


__all__ = [
    "BlockObservation", "crosshair", "from_bridge", "classify_frame",
    "patch_features", "EXACT_BRIDGE", "VISUAL_HIGH", "VISUAL_LOW",
    "UNKNOWN_SOURCE", "SOURCES", "VISUAL_CONFIDENCE_CEILING", "SIGNATURES",
]
